from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timezone

from agent.arb import Arb
from agent.brain import Brain
from agent.config import settings
from agent.executor import Executor
from agent.risk import Risk, is_sports
from agent.scanner import Scout
from agent.store import Store

log = logging.getLogger("desk")


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

    def _trim_reasons(self, open_pos: list) -> dict[tuple, str]:
        """Only flatten overflow above 12. Do not cut extra sports."""
        force: dict[tuple, str] = {}
        cap = settings.max_open_positions
        if len(open_pos) <= cap:
            return force
        ranked = sorted(open_pos, key=self._upnl)  # worst first
        for p in ranked[: len(open_pos) - cap]:
            key = (str(p.get("condition_id")), str(p.get("side") or "YES"))
            force[key] = "maks 12 — trim dårligste"
        return force

    def _market_stubs(self, open_pos: list) -> dict:
        by_id: dict = {}
        for pos in open_pos:
            cid = pos.get("condition_id")
            if not cid or cid in by_id:
                continue
            by_id[cid] = {
                "condition_id": cid,
                "question": pos.get("question") or "",
                "description": "",
                "end_date": None,
                "category": pos.get("category") or "other",
                "event_key": pos.get("event_key") or cid,
                "yes_token": pos.get("token_id") if pos.get("side") == "YES" else "",
                "no_token": pos.get("token_id") if pos.get("side") == "NO" else "",
                "yes_mid": float(pos.get("cur_price") or pos.get("avg_cost") or 0.5),
                "mid": float(pos.get("cur_price") or pos.get("avg_cost") or 0.5),
                "liquidity": 0,
                "_open_only": True,
            }
        return by_id

    def _run_exits(self, open_pos: list, estimates: dict, by_id: dict) -> tuple[int, list]:
        """Flatten on live bid. Always log hold | selling | sold | reject. Independent of Grok."""
        sold = 0
        log_rows: list[dict] = []
        force = self._trim_reasons(open_pos)
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
                pbook = self.scout.book(str(token))
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
            try:
                result = self.exec.sell(ticket_ex)
                status = str(result.get("status") or "")
                reason = ticket_ex.get("reason") or why
                if status in {"live_sell", "paper_sell"}:
                    action = "sold"
                    sold += 1
                elif status in {"resting_sell", "resting"}:
                    action = "reject"
                    err = ""
                    resp = result.get("response") or {}
                    if isinstance(resp, dict):
                        err = str(resp.get("error") or resp.get("errorMsg") or resp.get("msg") or "")
                    reason = f"FAK unmatched{(': ' + err) if err else ''} · {reason}"
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

    def _cycle(self) -> dict:
        halt = self.risk.halted()
        self.last_error = None
        bankroll, equity, open_pos = self._refresh_portfolio()
        by_open = self._market_stubs(open_pos)
        try:
            from agent.kalshi import attach as kalshi_attach_open
            kalshi_attach_open(list(by_open.values()))
        except Exception as exc:
            log.warning("Kalshi (åpne): %s", exc)
        exits, exit_log = self._run_exits(open_pos, {}, by_open)
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
                "kalshi": 0,
                "xai_usd": 0.0,
                "exits": exits,
                "exit_log": exit_log,
                "reason": halt,
                "bankroll": bankroll,
                "equity": equity,
            }
            return {"ok": True, "halted": True}

        self._cycle_i += 1
        run_grok = self._cycle_i % 3 == 1
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
            from agent.kalshi import attach as kalshi_attach
            kalshi_n = kalshi_attach(markets)
        except Exception as exc:
            log.warning("Kalshi: %s", exc)
            kalshi_n = 0
        arb_tickets = self.arb.scan(markets, bankroll)
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
        for cid, stub in self._market_stubs(open_pos).items():
            by_id.setdefault(cid, stub)
        ranked = [m for m in markets if not m.get("_open_only")]

        def _prio(m: dict) -> tuple:
            ks = m.get("kalshi") or {}
            gap = abs(float(ks.get("gap") or 0))
            mid = float(m.get("yes_mid") or m.get("mid") or 0.5)
            yes = float(m.get("yes_mid") or 0)
            no = float(m.get("no_mid") or (1 - yes if yes else 0))
            comp = yes + no if yes else float(m.get("complement") or 0)
            resid = max(0.0, 0.982 - comp) if 0 < comp < 1.2 else 0.0
            locked = 0 if mid >= 0.88 or mid <= 0.12 else 1
            vol = float(m.get("volume_24h") or m.get("liquidity") or 0)
            return (-gap, -resid, locked, -vol)

        ranked.sort(key=_prio)
        extras = [m for m in by_id.values() if m.get("_open_only")]
        try:
            from agent.kalshi import attach as kalshi_attach_open
            kalshi_attach_open(extras)
        except Exception:
            pass
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

        for m in extras:
            _take(m)
        if remaining >= 0.15:
            for m in ranked:
                gap = abs(float((m.get("kalshi") or {}).get("gap") or 0))
                if gap >= 0.04:
                    _take(m)
            for m in ranked:
                if is_sports(m):
                    continue
                cat = str(m.get("category") or "")
                if cat not in {"economics", "finance", "crypto", "politics", "geopolitics"}:
                    continue
                ks = m.get("kalshi") or {}
                gap = abs(float(ks.get("gap") or 0))
                chg = abs(float(m.get("price_change_1d") or 0))
                spread = float((m.get("book") or {}).get("spread") or 0)
                if gap >= 0.02 or chg >= 0.03 or spread >= 0.03:
                    _take(m)
            if remaining >= 0.50:
                for m in ranked:
                    if is_sports(m):
                        continue
                    _take(m)
                    if len(batch) >= max(settings.estimate_batch, len(extras)):
                        break
        batch = batch[: max(settings.estimate_batch, len(extras))]
        if not batch:
            log.info("Ingen markeder passerte filter")
            self.last_cycle = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "halted": False,
                "scanned": len(markets),
                "estimated": 0,
                "accepted": 0,
                "rejected": 0,
                "exits": exits,
                "exit_log": exit_log,
                "arb": arb_n,
                "kalshi": kalshi_n,
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
            if remaining < 0.05 and extras:
                batch = extras
            if not run_grok:
                estimates = {}
                log.info("Hopper Grok (syklus %s, neste om %s)", self._cycle_i, 3 - (self._cycle_i % 3))
            else:
                estimates = self.brain.estimate(batch) if (remaining >= 0.02 or extras) else {}
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
            more, log2 = self._run_exits(open_pos, estimates, by_id)
            exits += more
            exit_log.extend(log2)
            open_pos = self.store.positions("open")
        locked = sum(float(p["shares"]) * float(p["avg_cost"]) for p in open_pos)

        accepted = 0
        rejected = 0
        if not run_grok:
            batch = []
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
            "estimated": 0 if not run_grok else len(estimates),
            "grok": 0 if not run_grok else len(estimates),
            "accepted": accepted,
            "arb": arb_n,
            "kalshi": kalshi_n,
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
