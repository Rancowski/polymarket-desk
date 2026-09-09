from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timezone

from agent.arb import Arb
from agent.brain import Brain
from agent.config import SKIP_QUESTION_PATTERNS, settings
from agent.executor import Executor
from agent.risk import (
    MAX_SPORTS,
    Risk,
    dust_cutoff,
    is_sports,
    resolved_state,
    same_player_conflicts,
    sizing_base,
)
from agent.scanner import Scout
from agent.store import Store

log = logging.getLogger("desk")
GROK_BATCH_N = 15
_ESPORT_TITLE = (
    "counter-strike", "counter strike", "cs2", "cs 2", " cs ",
    "dota", "league of legends", "league-of-legends", "valorant",
)
_LIVE_TAPE = (
    "bo1", "bo2", "bo3", "bo5", "bo7", "best of",
    "map 1", "map 2", "map 3", "map 4", "map 5",
    "handicap", "spread", "over/under", "o/u",
    " -1.5", "+1.5", " -2.5", "+2.5", " -3.5", "+3.5",
)
_TOURNEY_WIN = ("to win", "win the", "winner of", "champion", "lift the")
_PREFER = (
    "election", "elect ", "president", "senate", "congress", "parliament",
    "bill", " act", "legislation",
    "fed", "fomc", "ecb", "bank of england", "central bank", "interest rate",
    "oscar", "award", "grammy", "emmy", "golden globe", "nobel",
)


def _hours(m: dict) -> float | None:
    raw = m.get("hours_left")
    try:
        if raw is None or raw == "":
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _grok_drop_reason(m: dict) -> str | None:
    """Why this name is not in the Grok-15. None = eligible."""
    if m.get("_open_only"):
        return "short_horizon"
    q = (m.get("question") or "").lower()
    blob = f"{q} {m.get('event_key') or ''} {m.get('category') or ''}".lower()
    if any(p in q for p in SKIP_QUESTION_PATTERNS):
        return "short_horizon"
    try:
        mid = float(m.get("yes_mid") or m.get("mid") or 0)
    except (TypeError, ValueError):
        mid = 0.0
    if mid <= 0.15 or mid >= 0.85:
        return "mid_extreme"
    h = _hours(m)
    if h is not None and h < 6:
        return "short_horizon"
    esport = any(x in blob for x in _ESPORT_TITLE)
    tourney = any(x in q for x in _TOURNEY_WIN)
    if esport and not (tourney and h is not None and h > 7 * 24):
        return "cs_live"
    if any(x in blob for x in _LIVE_TAPE):
        return "cs_live"
    return None


def _grok_eligible(m: dict) -> bool:
    return _grok_drop_reason(m) is None


def _grok_prefer(m: dict) -> bool:
    blob = f"{m.get('question') or ''} {m.get('event_key') or ''} {m.get('category') or ''}".lower()
    h = _hours(m)
    if any(x in blob for x in _PREFER):
        return True
    cat = str(m.get("category") or "")
    if cat == "crypto" and (h is None or h > 7 * 24):
        return True
    if cat in {"geopolitics", "politics"} and h is not None and h > 7 * 24:
        return True
    return False


class Desk:
    """To roller i én prosess: Scout+Brain vurderer, Risk+Executor handler."""

    def __init__(self) -> None:
        self.store = Store()
        self.scout = Scout()
        self.brain = Brain()
        self.arb = Arb(self.scout, self.store)
        self.risk = Risk(self.store)
        self.exec = Executor(self.store)
        self.cycle_lock = threading.Lock()
        self.busy = False
        self.last_error: str | None = None
        self.last_cycle: dict | None = None
        self._cycle_i = 0
        threading.Thread(target=self._bootstrap_portfolio, daemon=True, name="desk-boot").start()

    def begin_cycle_async(self) -> bool:
        if not self.cycle_lock.acquire(blocking=False):
            return False
        self.busy = True

        def _run() -> None:
            try:
                self._cycle()
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("Syklus krasjet")
            finally:
                self.busy = False
                self.cycle_lock.release()

        threading.Thread(target=_run, daemon=True, name="desk-once").start()
        return True

    def cycle(self) -> dict:
        if not self.cycle_lock.acquire(blocking=False):
            return {"ok": False, "reason": "syklus kjører allerede"}
        self.busy = True
        try:
            return self._cycle()
        except Exception as exc:
            self.last_error = str(exc)
            log.exception("Syklus krasjet")
            return {"ok": False, "reason": str(exc)}
        finally:
            self.busy = False
            self.cycle_lock.release()

    def _bootstrap_portfolio(self) -> None:
        try:
            self._refresh_portfolio()
        except Exception as exc:
            log.warning("bootstrap portfolio: %s", exc)

    def _refresh_portfolio(self) -> tuple[float, float, list]:
        try:
            self.exec.cancel_open()
        except Exception as exc:
            log.warning("cancel_open: %s", exc)
        try:
            live_pos = self.exec.fetch_live_positions()
            if live_pos is not None:
                self.store.sync_open_positions(live_pos)
        except Exception as exc:
            log.warning("sync posisjoner: %s", exc)
        bankroll = self.exec.bankroll()
        open_pos = self.store.positions("open")
        for p in open_pos:
            token = str(p.get("token_id") or "")
            if not token:
                continue
            try:
                book = self.scout.book(token)
            except Exception:
                continue
            if not book or book.get("synthetic"):
                continue
            mid = float(book.get("mid") or book.get("best_bid") or 0)
            if not (0 < mid < 0.99):
                continue
            p["cur_price"] = mid
            p["current_value"] = float(p.get("shares") or 0) * mid
            try:
                self.store.upsert_position(**p)
            except Exception:
                pass
        open_pos = self.store.positions("open")
        api_mtm = None
        try:
            api_mtm = self.exec.fetch_position_value()
        except Exception:
            api_mtm = None
        live_flags: dict = {}
        try:
            raw_live = self.exec.fetch_live_positions()
            if raw_live:
                live_flags = {
                    (str(r.get("condition_id")), str(r.get("side") or "YES").upper()): r
                    for r in raw_live
                }
        except Exception:
            live_flags = {}
        for p in open_pos:
            fl = live_flags.get((str(p.get("condition_id")), str(p.get("side") or "YES").upper())) or {}
            for k in ("redeemable", "neg_risk", "closed"):
                if fl.get(k) is not None:
                    p[k] = fl.get(k)
        cash, equity, open_cost, open_mtm = self.store.split_cash_equity(bankroll, open_pos)
        if api_mtm is not None and api_mtm > 0 and abs(api_mtm - open_cost) > 0.05:
            open_mtm = api_mtm
            equity = cash + open_mtm
        bankroll = cash
        self.store.mark_equity(bankroll, equity)
        self.store.save_snapshot(bankroll, equity, open_mtm)
        log.info(
            "Portfolio cash=%.2f mtm=%.2f cost=%.2f equity=%.2f open=%s",
            bankroll,
            open_mtm,
            open_cost,
            equity,
            len(open_pos),
        )
        return bankroll, equity, open_pos

    def _upnl(self, p: dict) -> float:
        cost = float(p.get("shares") or 0) * float(p.get("avg_cost") or 0)
        try:
            mtm = float(p.get("current_value") or 0)
        except (TypeError, ValueError):
            mtm = 0.0
        if mtm <= 0:
            try:
                mtm = float(p.get("shares") or 0) * float(p.get("cur_price") or 0)
            except (TypeError, ValueError):
                mtm = 0.0
        return mtm - cost

    def _trim_reasons(self, open_pos: list, equity: float = 0.0) -> dict[tuple, str]:
        force: dict[tuple, str] = {}
        deposited = 0.0
        try:
            deposited = float(self.store.deposited_usd(0.0) or 0)
        except (TypeError, ValueError):
            deposited = 0.0
        sports = [p for p in open_pos if is_sports(p)]
        force.update(same_player_conflicts(open_pos))
        if len(sports) > MAX_SPORTS:
            extra = sorted(sports, key=self._upnl)[: len(sports) - MAX_SPORTS]
            for p in extra:
                key = (str(p.get("condition_id")), str(p.get("side") or "YES"))
                force[key] = "maks 4 sports — trim"
        remaining = [
            p for p in open_pos
            if (str(p.get("condition_id")), str(p.get("side") or "YES")) not in force
        ]
        cap = settings.max_open_positions
        if len(remaining) > cap:
            ranked = sorted(remaining, key=self._upnl)
            for p in ranked[: len(remaining) - cap]:
                key = (str(p.get("condition_id")), str(p.get("side") or "YES"))
                force[key] = "maks 10 — trim dårligste"
        return force

    def _market_stubs(self, open_pos: list) -> dict:
        by_id: dict = {}
        for pos in open_pos:
            cid = pos.get("condition_id")
            if not cid or cid in by_id:
                continue
            side = str(pos.get("side") or "YES").upper()
            try:
                held = float(pos.get("cur_price") or pos.get("avg_cost") or 0.5)
            except (TypeError, ValueError):
                held = 0.5
            yes_mid = held if side != "NO" else (round(1.0 - held, 4) if 0 < held < 1 else 0.5)
            by_id[cid] = {
                "condition_id": cid,
                "question": pos.get("question") or "",
                "description": "",
                "end_date": None,
                "category": pos.get("category") or "other",
                "event_key": pos.get("event_key") or cid,
                "yes_token": pos.get("token_id") if side == "YES" else "",
                "no_token": pos.get("token_id") if side == "NO" else "",
                "yes_mid": yes_mid,
                "mid": yes_mid,
                "cur_price": held,
                "avg_cost": float(pos.get("avg_cost") or 0),
                "side": side,
                "shares": float(pos.get("shares") or 0),
                "liquidity": 0,
                "_open_only": True,
            }
        return by_id

    def _position_book(self, pos: dict) -> dict:
        token = str(pos.get("token_id") or "")
        if not token:
            return {}
        try:
            book = self.scout.book(str(token), require_two_sided=False)
        except Exception:
            return {}
        if not book or book.get("synthetic"):
            return {"best_bid": 0, "mid": 0, "best_ask": 0, "spread": 0, "synthetic": True}
        return book

    def _redeem_cooldown(self, cid: str) -> bool:
        raw = self.store.get_meta(f"redeem_err:{cid}", "")
        if not raw:
            return False
        try:
            return (time.time() - float(raw)) < 1800
        except (TypeError, ValueError):
            return False

    def _run_redeems(self, open_pos: list, by_id: dict) -> tuple[int, list]:
        """CTF redeem winners before any FAK. Losers close locally. Once per condition_id."""
        n = 0
        log_rows: list[dict] = []
        winners: dict[str, list] = {}
        logged: set[str] = set()

        def _row(pos: dict, action: str, reason: str) -> dict:
            return {
                "question": (pos.get("question") or "")[:80],
                "condition_id": pos.get("condition_id"),
                "side": pos.get("side"),
                "action": action,
                "reason": reason,
            }

        for pos in list(open_pos):
            cid = str(pos.get("condition_id") or "")
            if not cid:
                continue
            book = self._position_book(pos)
            mkt = by_id.get(cid) or {}
            state = resolved_state(pos, book, mkt)
            if state == "loser":
                self.store.close_position(cid, pos.get("side"))
                try:
                    self.store.clear_dust(cid, str(pos.get("side") or "YES"))
                except Exception:
                    pass
                why = "resolved loser — close locally, ikke FAK"
                if cid not in logged:
                    self.store.log_decision(
                        condition_id=cid,
                        question=pos.get("question"),
                        side=pos.get("side"),
                        action="closed",
                        reason=why,
                    )
                    logged.add(cid)
                log_rows.append(_row(pos, "closed", why))
                n += 1
                continue
            if state == "winner":
                winners.setdefault(cid, []).append(pos)

        ready: list[dict] = []
        for cid, rows in winners.items():
            if self.store.get_meta(f"redeem_ok:{cid}", ""):
                self.store.close_position(cid)
                continue
            if self._redeem_cooldown(cid):
                why = "redeem_err cooldown 30m"
                if cid not in logged:
                    self.store.log_decision(
                        condition_id=cid,
                        question=rows[0].get("question"),
                        side=rows[0].get("side"),
                        action="redeem_err",
                        reason=why,
                    )
                    logged.add(cid)
                log_rows.append(_row(rows[0], "redeem_err", why))
                continue
            ready.append(rows[0])

        def _ok(cid: str, rows: list, result: dict) -> None:
            nonlocal n
            status = str(result.get("status") or "redeem_ok")
            txh = str(result.get("tx") or "")
            why = f"{status} tx={txh[:18] if txh else '—'}"
            self.store.set_meta(f"redeem_err:{cid}", "")
            if cid not in logged:
                self.store.log_decision(
                    condition_id=cid,
                    question=rows[0].get("question"),
                    side=rows[0].get("side"),
                    action="redeem_ok",
                    reason=why,
                )
                logged.add(cid)
            log_rows.append(_row(rows[0], "redeem_ok", why))
            n += 1
            log.info("redeem_ok %s %s", (rows[0].get("question") or "")[:50], why)

        def _err(cid: str, rows: list, exc: Exception) -> None:
            why = f"redeem_err {exc}"[:220]
            self.store.set_meta(f"redeem_err:{cid}", f"{time.time():.0f}")
            if cid not in logged:
                self.store.log_decision(
                    condition_id=cid,
                    question=rows[0].get("question"),
                    side=rows[0].get("side"),
                    action="redeem_err",
                    reason=why,
                )
                logged.add(cid)
            log_rows.append(_row(rows[0], "redeem_err", why))
            log.warning("redeem_err %s %s", cid[:16], why)

        if ready:
            try:
                results = self.exec.redeem_batch(ready)
                failed: list[dict] = []
                for pos in ready:
                    cid = str(pos.get("condition_id"))
                    result = results.get(cid) or {}
                    st = str(result.get("status") or "")
                    if st in {"redeem_ok", "paper_redeem"}:
                        _ok(cid, winners.get(cid) or [pos], result)
                    else:
                        failed.append(pos)
                if not results:
                    failed = list(ready)
                for pos in failed:
                    cid = str(pos.get("condition_id"))
                    try:
                        result = self.exec.redeem(pos)
                        st = str(result.get("status") or "")
                        if st in {"redeem_ok", "paper_redeem"}:
                            _ok(cid, winners.get(cid) or [pos], result)
                        else:
                            _err(cid, winners.get(cid) or [pos], RuntimeError(st or "ukjent"))
                    except Exception as exc:
                        _err(cid, winners.get(cid) or [pos], exc)
            except Exception as exc:
                log.warning("redeem_batch: %s — prøver per condition_id", exc)
                for pos in ready:
                    cid = str(pos.get("condition_id"))
                    try:
                        result = self.exec.redeem(pos)
                        st = str(result.get("status") or "")
                        if st in {"redeem_ok", "paper_redeem"}:
                            _ok(cid, winners.get(cid) or [pos], result)
                        else:
                            _err(cid, winners.get(cid) or [pos], RuntimeError(st or "ukjent"))
                    except Exception as one:
                        _err(cid, winners.get(cid) or [pos], one)
        return n, log_rows

    def _run_exits(self, open_pos: list, estimates: dict, by_id: dict, equity: float = 0.0) -> tuple[int, list]:
        """Flatten on live bid. Always log hold | selling | sold | reject. Independent of Grok."""
        sold = 0
        log_rows: list[dict] = []
        force = self._trim_reasons(open_pos, equity=equity)
        for pos in list(open_pos):
            q = (pos.get("question") or "")[:80]
            cid = pos.get("condition_id")
            side = pos.get("side")
            token = pos.get("token_id")

            def _row(action: str, reason: str) -> dict:
                return {
                    "question": q,
                    "condition_id": cid,
                    "side": side,
                    "action": action,
                    "reason": reason,
                }

            try:
                mark_pre = float(pos.get("cur_price") or 0)
            except (TypeError, ValueError):
                mark_pre = 0.0
            if mark_pre >= 0.90:
                try:
                    self.store.clear_dust(str(cid or ""), str(side or "YES"))
                except Exception:
                    pass
            elif self.store.is_dust(str(cid or ""), str(side or "YES")):
                why = "closed_dust — hopper FAK"
                self.store.log_decision(
                    condition_id=cid,
                    question=pos.get("question"),
                    side=side,
                    action="hold",
                    reason=why,
                )
                log_rows.append(_row("hold", why))
                continue
            if not token:
                why = "mangler token_id"
                self.store.log_decision(
                    condition_id=cid,
                    question=pos.get("question"),
                    side=side,
                    action="hold",
                    reason=why,
                )
                log_rows.append(_row("hold", why))
                continue
            try:
                pbook = self.scout.book(str(token), require_two_sided=False)
            except Exception as exc:
                pbook = {}
                self.store.log_decision(
                    condition_id=cid,
                    question=pos.get("question"),
                    action="skip",
                    reason=f"exit-bok: {exc}",
                )
            if pbook.get("synthetic"):
                pbook = {"best_bid": 0, "mid": 0, "best_ask": 0, "spread": 0}
            key = (str(cid), str(side or "YES"))
            mkt = by_id.get(cid or "") or {}
            ticket_ex, why = self.risk.evaluate_exit(
                pos,
                pbook,
                estimates.get(cid or ""),
                0.0,
                kalshi=mkt.get("kalshi"),
                force_reason=force.get(key),
                market=mkt,
            )
            if not ticket_ex:
                self.store.log_decision(
                    condition_id=cid,
                    question=pos.get("question"),
                    side=side,
                    action="hold",
                    reason=why,
                )
                log_rows.append(_row("hold", why))
                continue
            if str(ticket_ex.get("kind") or "") == "resolved_loser":
                self.store.close_position(str(cid or ""), side)
                try:
                    self.store.clear_dust(str(cid or ""), str(side or "YES"))
                except Exception:
                    pass
                why = ticket_ex.get("reason") or "resolved loser — close locally"
                self.store.log_decision(
                    condition_id=cid,
                    question=pos.get("question"),
                    side=side,
                    action="closed",
                    reason=why,
                )
                log_rows.append(_row("closed", why))
                sold += 1
                continue
            try:
                result = self.exec.sell(ticket_ex)
                status = str(result.get("status") or "")
                reason = ticket_ex.get("reason") or why
                book_bid = float(ticket_ex.get("best_bid") or result.get("best_bid") or 0)
                try:
                    mark = float(pos.get("cur_price") or 0)
                except (TypeError, ValueError):
                    mark = 0.0
                value = float(pos.get("shares") or 0) * mark
                if status in {"live_sell", "paper_sell"}:
                    action = "sold"
                    sold += 1
                elif mark < 0.90 and (
                    status == "unmatched_dust"
                    or (
                        ticket_ex.get("dust")
                        and status in {"resting_sell", "resting"}
                        and (value < dust_cutoff(sizing_base(self.store.deposited_usd(0.0), equity)) or mark <= 0.01)
                    )
                ):
                    px = result.get("attempt_px")
                    self.store.close_dust(str(cid or ""), str(side or "YES"))
                    action = "closed_dust"
                    sold += 1
                    reason = f"closed_dust FAK unmatched @{px} bid={book_bid:.4f} · {reason}"
                elif status in {"resting_sell", "resting"}:
                    action = "reject"
                    err = ""
                    resp = result.get("response") or {}
                    if isinstance(resp, dict):
                        err = str(resp.get("error") or resp.get("errorMsg") or resp.get("msg") or "")
                    attempt_px = result.get("attempt_px")
                    reason = (
                        f"FAK unmatched bid={book_bid:.3f}"
                        f"{(' try@' + str(attempt_px)) if attempt_px not in (None, '') else ''}"
                        f"{(': ' + err) if err else ''} · {reason}"
                    )
                else:
                    action = "selling"
                self.store.log_decision(
                    condition_id=cid,
                    question=pos.get("question"),
                    side=side,
                    action=action,
                    reason=reason,
                    payload=result,
                )
                log_rows.append(_row(action, reason))
            except Exception as exc:
                log.exception("Salg feilet")
                self.store.log_decision(
                    condition_id=cid,
                    question=pos.get("question"),
                    action="reject",
                    reason=str(exc),
                )
                log_rows.append(_row("reject", str(exc)))
        return sold, log_rows

    def _log_kalshi(self, rows: list) -> None:
        seen: set[str] = set()
        for row in rows:
            cid = str(row.get("condition_id") or "")
            key = cid or str(row.get("question") or "")
            if key in seen:
                continue
            seen.add(key)
            raw_gap = row.get("gap")
            raw_k = row.get("kalshi")
            ticker = str(row.get("ticker") or "")
            try:
                k_yes = float(raw_k) if raw_k not in (None, "") else 0.0
            except (TypeError, ValueError):
                k_yes = 0.0
            try:
                gap = float(raw_gap) if raw_gap not in (None, "") else 0.0
            except (TypeError, ValueError):
                gap = 0.0
            unpaired = not ticker or k_yes <= 0
            self.store.log_decision(
                condition_id=cid or None,
                question=row.get("question"),
                mid=row.get("pm"),
                p_hat=None if unpaired else raw_k,
                edge_net=None if unpaired else gap,
                action=f"kalshi-{row.get('action') or 'skip'}",
                reason=(
                    f"{ticker or '—'} skip {row.get('why') or 'unpaired'}"
                    if unpaired
                    else (
                        f"{ticker} PM {float(row.get('pm') or 0):.2f} "
                        f"Kalshi {k_yes:.2f} gap {gap:+.2f} "
                        f"{row.get('why') or ''}"
                    )
                ).strip(),
                payload=row,
            )

    def _cycle(self) -> dict:
        halt = self.risk.halted()
        self.last_error = None
        bankroll, equity, open_pos = self._refresh_portfolio()
        from agent.kalshi import compare as kalshi_compare, fetch_open as kalshi_fetch
        kalshi_rows: list = []
        kalshi_log: list = []
        try:
            kalshi_rows = kalshi_fetch()
        except Exception as exc:
            log.warning("Kalshi fetch: %s", exc)
        by_open = self._market_stubs(open_pos)
        redeems, redeem_log = self._run_redeems(open_pos, by_open)
        if redeems:
            try:
                bankroll, equity, open_pos = self._refresh_portfolio()
                by_open = self._market_stubs(open_pos)
            except Exception as exc:
                log.warning("post-redeem sync: %s", exc)
                open_pos = self.store.positions("open")
        try:
            _n, log1 = kalshi_compare(list(by_open.values()), kalshi_rows)
            kalshi_log.extend(log1)
        except Exception as exc:
            log.warning("Kalshi (åpne): %s", exc)
        self._log_kalshi(kalshi_log)
        exits, exit_log = self._run_exits(open_pos, {}, by_open, equity=equity)
        exit_log = redeem_log + exit_log
        exits = redeems + exits
        open_pos = self.store.positions("open")
        if halt:
            log.warning("Stoppet: %s", halt)
            self.last_cycle = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "halted": True,
                "scanned": 0,
                "estimated": 0,
                "accepted": 0,
                "rejected": 0,
                "kalshi": len(kalshi_log),
                "kalshi_log": kalshi_log,
                "xai_usd": 0.0,
                "exits": exits,
                "exit_log": exit_log,
                "reason": halt,
                "bankroll": bankroll,
                "equity": equity,
            }
            return {"ok": True, "halted": True}

        self._cycle_i += 1
        run_grok = True
        log.info(
            "Syklus start dry_run=%s bankroll=%.2f equity=%.2f open=%s grok=%s n=%s",
            settings.dry_run,
            bankroll,
            equity,
            len(open_pos),
            run_grok,
            self._cycle_i,
        )

        markets = self.scout.fetch()
        try:
            _n2, log2 = kalshi_compare(markets, kalshi_rows)
            seen_k = {str(r.get("condition_id") or r.get("question")) for r in kalshi_log}
            new_k = [r for r in log2 if str(r.get("condition_id") or r.get("question")) not in seen_k]
            kalshi_log.extend(new_k)
            self._log_kalshi(new_k)
        except Exception as exc:
            log.warning("Kalshi: %s", exc)
        kalshi_n = len(kalshi_log)
        arb_tickets = self.arb.scan(markets, bankroll, equity=equity)
        arb_n = 0
        failed_events: set[str] = set()
        pending_hedge = None
        for ticket in arb_tickets:
            if ticket.event_key in failed_events:
                continue
            try:
                result = self.exec.submit(ticket)
                arb_n += 1
                self.store.log_decision(
                    condition_id=ticket.condition_id,
                    question=ticket.question,
                    side=ticket.side,
                    mid=ticket.mid,
                    p_hat=ticket.p_hat,
                    edge_net=ticket.edge_net,
                    action=result.get("status"),
                    reason=ticket.thesis,
                    payload=result,
                )
                if result.get("status") in {"live", "paper"}:
                    bankroll = max(0.0, bankroll - ticket.size_usd)
                if "sum-til-én" in (ticket.thesis or "") or "event-sett" in (ticket.thesis or ""):
                    pending_hedge = ticket if pending_hedge is None else None
                else:
                    pending_hedge = None
            except Exception as exc:
                log.exception("Arb-ordre feilet")
                failed_events.add(ticket.event_key)
                if pending_hedge and pending_hedge.event_key == ticket.event_key:
                    try:
                        self.exec.sell(
                            {
                                "condition_id": pending_hedge.condition_id,
                                "question": pending_hedge.question,
                                "side": pending_hedge.side,
                                "token_id": pending_hedge.token_id,
                                "shares": pending_hedge.shares,
                                "limit_price": max(0.01, pending_hedge.best_bid or pending_hedge.limit_price),
                                "size_usd": pending_hedge.size_usd,
                                "reason": "hedge-rollback",
                            }
                        )
                    except Exception:
                        log.exception("Hedge-rollback feilet")
                    pending_hedge = None
                self.store.log_decision(
                    condition_id=ticket.condition_id,
                    question=ticket.question,
                    action="error",
                    reason=str(exc),
                )
        if arb_n:
            open_pos = self.store.positions("open")

        by_id = {m["condition_id"]: m for m in markets}
        for cid, stub in by_open.items():
            by_id.setdefault(cid, stub)
            if stub.get("kalshi") and not (by_id.get(cid) or {}).get("kalshi"):
                by_id[cid]["kalshi"] = stub["kalshi"]
        for cid, stub in self._market_stubs(open_pos).items():
            by_id.setdefault(cid, stub)
        vol_ranked = sorted(
            [m for m in markets if not m.get("_open_only")],
            key=lambda m: -float(m.get("volume_24h") or m.get("liquidity") or 0),
        )
        eligible: list = []
        logged_drop = 0
        for m in vol_ranked:
            why = _grok_drop_reason(m)
            if why:
                if logged_drop < GROK_BATCH_N:
                    self.store.log_decision(
                        condition_id=m.get("condition_id"),
                        question=m.get("question"),
                        mid=m.get("mid") or m.get("yes_mid"),
                        action="skip",
                        reason=f"grok-drop {why}",
                    )
                    logged_drop += 1
                continue
            eligible.append(m)
        eligible.sort(
            key=lambda m: (
                0 if _grok_prefer(m) else 1,
                -float(m.get("volume_24h") or m.get("liquidity") or 0),
            )
        )
        prepaid = self.store.xai_prepaid_usd()
        spent = self.store.api_spend(hours=None)
        remaining = max(0.0, prepaid - spent) if prepaid > 0 else 1.0
        seen: set[str] = set()
        batch: list = []

        def _take(m: dict) -> None:
            cid = m.get("condition_id")
            if not cid or cid in seen:
                return
            seen.add(cid)
            batch.append(m)

        if remaining >= 0.15:
            for m in eligible:
                _take(m)
                if len(batch) >= GROK_BATCH_N:
                    break
        batch = batch[:GROK_BATCH_N]
        if not batch:
            log.info("Ingen markeder passerte filter")
            self.last_cycle = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "halted": False,
                "scanned": len(markets),
                "estimated": 0,
                "grok": 0,
                "accepted": 0,
                "rejected": 0,
                "exits": exits,
                "exit_log": exit_log,
                "arb": arb_n,
                "kalshi": kalshi_n,
                "kalshi_log": kalshi_log,
                "bankroll": bankroll,
                "equity": equity,
                "xai_usd": 0.0,
            }
            return {"ok": True, "scanned": 0}

        for m in batch:
            try:
                mid = float(m.get("yes_mid") or m.get("mid") or 0)
                if m.get("yes_token"):
                    m["book"] = self.scout.book(m["yes_token"], fallback_mid=mid)
                if m.get("no_token"):
                    no_mid = float(m.get("no_mid") or (1 - mid if mid else 0))
                    m["no_book"] = self.scout.book(m["no_token"], fallback_mid=no_mid)
            except Exception as exc:
                log.warning("Bok-feil %s: %s", m.get("question", "")[:40], exc)
                m["book"] = {}
            if (m.get("book") or {}).get("synthetic"):
                m["book"] = {}
            if (m.get("no_book") or {}).get("synthetic"):
                m["no_book"] = {}

        self.scout.enrich(batch)

        xai_cycle = 0.0
        try:
            if remaining < 0.02:
                estimates = {}
                log.info("Hopper Grok — xAI-budsjett tomt")
            else:
                estimates = self.brain.estimate(batch) if batch else {}
                log.info("Grok-batch %s navn (syklus %s)", len(batch), self._cycle_i)
            usage = getattr(self.brain, "last_usage", {}) or {}
            xai_cycle = float(usage.get("usd") or 0)
            if xai_cycle:
                self.store.add_api_cost(xai_cycle, str(usage.get("model") or ""), int(usage.get("tokens") or 0))
            n_blend = 0
            for m in batch:
                ks = m.get("kalshi") or {}
                k_yes = float(ks.get("yes") or 0)
                if not (0.02 < k_yes < 0.98):
                    continue
                cid = m["condition_id"]
                est = estimates.get(cid) or {}
                p = est.get("p_yes")
                gap = abs(float(ks.get("gap") or 0))
                w_k = 0.70 if gap >= 0.04 else 0.55
                blended = round(w_k * k_yes + (1 - w_k) * float(p), 4) if p is not None else k_yes
                estimates[cid] = {
                    **est,
                    "p_yes": blended,
                    "skip": False,
                    "confidence": "high" if gap >= 0.04 else (est.get("confidence") or "medium"),
                    "thesis": ((est.get("thesis") or "") + f" | Kalshi {k_yes:.2f} (w={w_k}) gap {ks.get('gap')}").strip(" |"),
                }
                n_blend += 1
            if n_blend:
                log.info("Kalshi blend på %s markeder", n_blend)
            self.last_error = None
        except Exception as exc:
            log.exception("Brain krasjet: %s", exc)
            self.last_error = str(exc)
            self.last_cycle = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "halted": False,
                "scanned": len(markets),
                "estimated": 0,
                "accepted": 0,
                "arb": arb_n,
                "kalshi": kalshi_n,
                "kalshi_log": kalshi_log,
                "rejected": 0,
                "exits": exits,
                "exit_log": exit_log,
                "xai_usd": round(xai_cycle, 4),
                "bankroll": bankroll,
                "equity": equity,
                "reason": str(exc),
            }
            return {"ok": False, "reason": str(exc), **self.last_cycle}

        open_pos = self.store.positions("open")
        if estimates:
            more, log2 = self._run_exits(open_pos, estimates, by_id, equity=equity)
            exits += more
            exit_log.extend(log2)
            open_pos = self.store.positions("open")
        locked = sum(float(p["shares"]) * float(p["avg_cost"]) for p in open_pos)

        accepted = 0
        rejected = 0
        for m in batch:
            if m.get("_open_only"):
                continue
            est = estimates.get(m["condition_id"])
            if not est:
                self.store.log_decision(
                    condition_id=m["condition_id"],
                    question=m["question"],
                    action="skip",
                    reason="ingen estimat",
                )
                rejected += 1
                continue
            try:
                book = m.get("book") or self.scout.book(
                    m["yes_token"], fallback_mid=float(m.get("yes_mid") or m.get("mid") or 0)
                )
            except Exception as exc:
                self.store.log_decision(
                    condition_id=m["condition_id"],
                    question=m["question"],
                    action="skip",
                    reason=f"bok-feil: {exc}",
                )
                rejected += 1
                continue
            ticket, reason = self.risk.evaluate(m, book, est, bankroll, equity)
            if not ticket:
                rejected += 1
                self.store.log_decision(
                    condition_id=m["condition_id"],
                    question=m["question"],
                    mid=book.get("mid"),
                    p_hat=est.get("p_yes"),
                    action="reject",
                    reason=reason,
                    payload=est,
                )
                continue
            try:
                result = self.exec.submit(ticket)
                accepted += 1
                self.store.log_decision(
                    condition_id=ticket.condition_id,
                    question=ticket.question,
                    side=ticket.side,
                    mid=ticket.mid,
                    p_hat=ticket.p_hat,
                    edge_net=ticket.edge_net,
                    action=result.get("status"),
                    reason=ticket.thesis,
                    payload=result,
                )
                if result.get("status") in {"live", "paper"}:
                    bankroll = max(0.0, bankroll - ticket.size_usd)
                    equity = bankroll + locked + ticket.size_usd
                    locked += ticket.size_usd
            except Exception as exc:
                log.exception("Ordre feilet")
                self.last_error = str(exc)
                self.store.log_decision(
                    condition_id=ticket.condition_id,
                    question=ticket.question,
                    action="error",
                    reason=str(exc),
                )

        try:
            bankroll, equity, _ = self._refresh_portfolio()
        except Exception:
            pass
        log.info("Syklus ferdig. Grok-tickets: %s arb: %s kalshi: %s exits: %s", accepted, arb_n, kalshi_n, exits)
        self.last_cycle = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "halted": False,
            "scanned": len(markets),
            "estimated": len(estimates),
            "grok": len(estimates),
            "accepted": accepted,
            "arb": arb_n,
            "kalshi": kalshi_n,
            "kalshi_log": kalshi_log,
            "rejected": rejected,
            "exits": exits,
            "exit_log": exit_log,
            "xai_usd": round(xai_cycle, 4),
            "bankroll": bankroll,
            "equity": equity,
        }
        return {"ok": True, **self.last_cycle}

    def run_forever(self) -> None:
        log.info("Desk kjører. DRY_RUN=%s interval=%ss", settings.dry_run, settings.loop_seconds)
        while True:
            try:
                self.cycle()
            except Exception:
                log.error("Syklus krasjet:\n%s", traceback.format_exc())
            time.sleep(settings.loop_seconds)
