from __future__ import annotations

import logging
import threading
import time
import traceback
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.arb import Arb
from agent.brain import Brain
from agent.config import SKIP_QUESTION_PATTERNS, settings
from agent.executor import Executor
from agent.risk import (
    MAX_SPORTS,
    Risk,
    dust_cutoff,
    is_fdv_pin,
    is_in_play_tape,
    is_map_bo,
    is_match_market,
    is_sports,
    resolved_state,
    same_player_conflicts,
    sizing_base,
)
from agent.scanner import Scout
from agent.kalshi import _pm_family, extract_tickers, keep_fed_h25, pair_ok
from agent.store import Store

log = logging.getLogger("desk")
GROK_BATCH_N = 15
_KX_TICKER_RE = re.compile(
    r"\b(?:KX[A-Z0-9-]{4,}|CONTROLH-[A-Z0-9-]+|PRES-[A-Z0-9-]+|KXSENATE[A-Z0-9-]*)\b",
    re.I,
)


def _scrub_exit_reason(reason: str) -> str:
    """Exit logs must not print a rejected Kalshi ticker."""
    t = _KX_TICKER_RE.sub("", reason or "")
    t = re.sub(r"\s{2,}", " ", t)
    t = re.sub(r"\s+[—-]\s+", " — ", t)
    return t.strip(" -—") or (reason or "")
REJECT_KEYS = (
    "confidence=low",
    "edge_net",
    "cs_live",
    "mid_extreme",
    "short_horizon",
    "near-res",
)
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
    """Why this name is not in the Grok-15. None = eligible.

    cs_live = sports in-play tape (map/BO/handicap or hours < 6). Not 'vs' and not <48h.
    short_horizon = hours_left < 6 or 5/15-min crypto. 8–48h is NEAR, not short.
    mid_extreme for the batch = mid < 0.12 or mid > 0.88.
    """
    if m.get("_open_only"):
        return "short_horizon"
    q = (m.get("question") or "").lower()
    if any(p in q for p in SKIP_QUESTION_PATTERNS):
        return "short_horizon"
    try:
        mid = float(m.get("yes_mid") or m.get("mid") or 0)
    except (TypeError, ValueError):
        mid = 0.0
    if mid <= 0 or mid < 0.12 or mid > 0.88:
        return "mid_extreme"
    h = _hours(m)
    if h is not None and h < 6:
        return "short_horizon"
    if is_in_play_tape(m) or is_map_bo(m):
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


def _vol24(m: dict) -> float:
    try:
        return float(m.get("volume_24h") or 0)
    except (TypeError, ValueError):
        return 0.0


def _mid_band(m: dict, lo: float = 0.25, hi: float = 0.85) -> bool:
    try:
        mid = float(m.get("yes_mid") or m.get("mid") or 0)
    except (TypeError, ValueError):
        mid = 0.0
    return lo <= mid <= hi


def _grok_bucket(m: dict) -> str | None:
    """A = 8–48h named non-map (incl. 0.58–0.85 favorites), B = 48h–7d, C = preferred >7d."""
    if is_fdv_pin(m) or is_map_bo(m) or is_in_play_tape(m):
        return None
    h = _hours(m)
    if h is not None and 8 <= h <= 48 and _vol24(m) > 0 and _mid_band(m, 0.25, 0.85):
        return "A"
    if h is not None and 48 < h <= 7 * 24 and _mid_band(m, 0.25, 0.82):
        return "B"
    if (h is None or h > 7 * 24) and _grok_prefer(m) and _mid_band(m, 0.25, 0.82):
        return "C"
    return None


def _is_tennis_row(m: dict) -> bool:
    blob = f"{m.get('question') or ''} {m.get('event_key') or ''} {m.get('category') or ''}".lower()
    return any(x in blob for x in ("challenger", "exhibition", "tennis", " atp", "atp ", "wta"))


def _is_tourney_longshot(m: dict) -> bool:
    blob = f"{m.get('question') or ''} {m.get('event_key') or ''}".lower()
    try:
        mid = float(m.get("yes_mid") or m.get("mid") or 0)
    except (TypeError, ValueError):
        mid = 0.0
    if mid >= 0.40:
        return False
    return any(x in blob for x in ("win the", "to win", "winner of", "lift the", "champion"))


def _build_grok_batch(eligible: list) -> tuple[list, dict[str, int]]:
    """Fill GROK_BATCH_N from A then B then C. Cap tennis/challenger and tournament longshots at 2."""
    buckets: dict[str, list] = {"A": [], "B": [], "C": []}
    for m in eligible:
        b = _grok_bucket(m)
        if b:
            buckets[b].append(m)
    for key in buckets:
        buckets[key].sort(key=lambda row: -_vol24(row))
    batch: list = []
    seen: set[str] = set()
    counts = {"A": 0, "B": 0, "C": 0}
    tennis_n = 0
    open_n = 0
    for key in ("A", "B", "C"):
        for m in buckets[key]:
            cid = str(m.get("condition_id") or "")
            if not cid or cid in seen:
                continue
            if _is_tennis_row(m):
                if tennis_n >= 2:
                    continue
                tennis_n += 1
            if _is_tourney_longshot(m):
                if open_n >= 2:
                    continue
                open_n += 1
            seen.add(cid)
            batch.append(m)
            counts[key] += 1
            if len(batch) >= GROK_BATCH_N:
                return batch, counts
    return batch, counts


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
        self._cycle_id = ""
        self._bought = 0
        self._reject_counts = {k: 0 for k in REJECT_KEYS}
        from agent.kalshi import pair_ok_selfcheck

        pair_ok_selfcheck()
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

    def _illegal_pair_flatten(self, open_pos: list, by_id: dict) -> dict[tuple, str]:
        """FAK-sell legs whose live/stored Kalshi ticker fails pair_ok, or kalshi without ticker."""
        force: dict[tuple, str] = {}
        for p in open_pos:
            q = str(p.get("question") or "")
            cid = str(p.get("condition_id") or "")
            side = str(p.get("side") or "YES")
            if not cid:
                continue
            live = (by_id.get(cid) or {}).get("kalshi") or {}
            title = str(live.get("title") or "")
            try:
                ky = float(live.get("yes") or 0) or None
            except (TypeError, ValueError):
                ky = None
            try:
                py = float(live.get("pm_yes") or p.get("cur_price") or 0) or None
            except (TypeError, ValueError):
                py = None

            def _legal(tick: str) -> tuple[bool, str]:
                t = str(tick or "").strip()
                if not t:
                    return False, "ingen ticker"
                if keep_fed_h25(q, t):
                    return True, ""
                # Pin is a buy gate, not a flatten reason (Fed 26SEP stays).
                ok, why = pair_ok(q, t, title, ky, None)
                if not ok and why == "pm pin ikke kalshi-buy":
                    return True, ""
                return ok, why

            live_tick = str(live.get("ticker") or "").strip()
            if live_tick:
                ok, why = _legal(live_tick)
                if not ok:
                    force[(cid, side)] = f"ulovlig par — {why}"
                continue
            stored: list[str] = []
            stored.extend(extract_tickers(str(p.get("entry_detail") or "")))
            try:
                stored.extend(self.store.kalshi_tickers_for(cid))
            except Exception:
                pass
            stored = list(dict.fromkeys(t for t in stored if t))
            if stored:
                if any(_legal(t)[0] for t in stored):
                    continue
                t0 = stored[0]
                force[(cid, side)] = f"ulovlig par — {_legal(t0)[1]}"
                continue
            if str(p.get("entry_source") or "") == "kalshi":
                force[(cid, side)] = "kalshi uten gyldig ticker — flatten"
        by_cid: dict[str, list] = {}
        for p in open_pos:
            cid = str(p.get("condition_id") or "")
            if cid:
                by_cid.setdefault(cid, []).append(p)
        for cid, rows in by_cid.items():
            if len(rows) < 2:
                continue
            def _opened(r: dict) -> str:
                return str(r.get("opened_ts") or r.get("last_ts") or "")
            keep = sorted(rows, key=_opened)[0]
            keep_side = str(keep.get("side") or "YES")
            for r in rows:
                rs = str(r.get("side") or "YES")
                if rs == keep_side:
                    continue
                force[(cid, rs)] = "stacked fills — behold eldste lot"
        for p in open_pos:
            cid = str(p.get("condition_id") or "")
            side = str(p.get("side") or "YES")
            stub = by_id.get(cid) or {}
            try:
                yes_mid = float(stub.get("yes_mid") or stub.get("mid") or 0)
            except (TypeError, ValueError):
                yes_mid = 0.0
            if side == "NO" and yes_mid >= 0.95:
                force[(cid, side)] = f"short pin PM YES {yes_mid:.2f}"
            if side == "YES" and 0 < yes_mid <= 0.05:
                force[(cid, side)] = f"short pin PM YES {yes_mid:.2f}"
            try:
                n_buy = self.store.buy_fill_count(cid, side)
                first = self.store.first_buy_shares(cid, side)
                cur = float(p.get("shares") or 0)
            except Exception:
                n_buy, first, cur = 0, None, 0.0
            if n_buy >= 2 and first and cur > first + 0.5:
                force[(cid, side)] = "stacked fills — selg påfyll"
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

    def _reject_bucket(self, reason: str) -> str | None:
        r = (reason or "").lower()
        if reason in self._reject_counts:
            return reason
        if "confidence=low" in r:
            return "confidence=low"
        if "edge_net" in r:
            return "edge_net"
        if "cs_live" in r:
            return "cs_live"
        if "mid_extreme" in r:
            return "mid_extreme"
        if "short_horizon" in r:
            return "short_horizon"
        if "nær resolusjon" in r or "near-res" in r or "nær avgjort" in r:
            return "near-res"
        return None

    def _bump_reject(self, reason: str) -> None:
        bucket = self._reject_bucket(reason)
        if bucket:
            self._reject_counts[bucket] = self._reject_counts.get(bucket, 0) + 1

    def _finish_cycle(self, **kwargs: Any) -> dict:
        sold = kwargs.get("sold")
        if sold is None:
            sold = kwargs.get("exits", 0)
        base = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "halted": False,
            "scanned": 0,
            "estimated": 0,
            "grok": 0,
            "accepted": 0,
            "rejected": 0,
            "arb": 0,
            "kalshi": 0,
            "kalshi_log": [],
            "xai_usd": 0.0,
            "exits": 0,
            "exit_log": [],
            "bought": self._bought,
            "sold": sold,
            "redeem_ok": 0,
            "reject_counts": dict(self._reject_counts),
            "cycle_id": self._cycle_id,
            "bankroll": 0,
            "equity": 0,
        }
        base.update(kwargs)
        if "sold" not in kwargs:
            base["sold"] = sold
        self.last_cycle = base
        return base

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

    def _run_exits(self, open_pos: list, estimates: dict, by_id: dict, equity: float = 0.0, extra_force: dict | None = None) -> tuple[int, list]:
        """Flatten on live bid. Always log hold | selling | sold | reject. Independent of Grok."""
        sold = 0
        log_rows: list[dict] = []
        force = self._trim_reasons(open_pos, equity=equity)
        force.update(self._illegal_pair_flatten(open_pos, by_id))
        if extra_force:
            force.update(extra_force)
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
                    "reason": _scrub_exit_reason(reason),
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
            elif self.store.is_dust(str(cid or ""), str(side or "YES")) or self.store.dust_close_fresh(
                str(cid or ""), str(side or "YES")
            ):
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
            if mkt.get("yes_token") and not mkt.get("book"):
                try:
                    mkt["book"] = self.scout.book(str(mkt.get("yes_token")), require_two_sided=False)
                except Exception:
                    pass
            if mkt.get("no_token") and not mkt.get("no_book"):
                try:
                    mkt["no_book"] = self.scout.book(str(mkt.get("no_token")), require_two_sided=False)
                except Exception:
                    pass
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
                if "stacked" in str(ticket_ex.get("reason") or force.get(key) or ""):
                    first = self.store.first_buy_shares(str(cid or ""), str(side or "YES"))
                    cur = float(pos.get("shares") or 0)
                    if first and cur > first + 0.5:
                        extra = round(cur - first, 2)
                        ticket_ex = {
                            **ticket_ex,
                            "shares": extra,
                            "size_usd": round(extra * float(ticket_ex.get("limit_price") or 0), 4),
                            "leave_shares": first,
                            "reason": ticket_ex.get("reason") or "stacked fills — selg påfyll",
                        }
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
                elif status == "dust_close":
                    fam = _pm_family(str(pos.get("question") or ""))
                    cid_s = str(cid or "")
                    if fam == "fed":
                        action = "hold"
                        reason = f"ikke dust Fed · {_scrub_exit_reason(reason)}"
                    elif self.store.dust_close_fresh(cid_s, str(side or "YES")):
                        action = "hold"
                        reason = "dust_close cooldown 30m"
                    else:
                        self.store.close_dust(cid_s, str(side or "YES"))
                        action = "dust_close"
                        sold += 1
                        notion = result.get("notional")
                        reason = (
                            f"dust_close shares={result.get('shares') or pos.get('shares')} "
                            f"bid={book_bid:.3f} notional={notion} · {_scrub_exit_reason(reason)}"
                        )
                elif status == "no_bid":
                    mkt = by_id.get(cid or "") or {}
                    state = resolved_state(pos, pbook, mkt)
                    redeemable = bool(
                        pos.get("redeemable")
                        or mkt.get("redeemable")
                        or mkt.get("closed")
                        or mkt.get("resolved")
                        or pos.get("closed")
                    )
                    fam = _pm_family(str(pos.get("question") or ""))
                    if state == "loser":
                        self.store.close_position(str(cid or ""), side)
                        try:
                            self.store.clear_dust(str(cid or ""), str(side or "YES"))
                        except Exception:
                            pass
                        action = "closed"
                        sold += 1
                        reason = f"closed ingen live bud — loser · {reason}"
                    elif state == "winner" or redeemable or mark >= 0.99:
                        cid_s = str(cid or "")
                        if self._redeem_cooldown(cid_s):
                            action = "redeem_err"
                            reason = "redeem_err cooldown 30m"
                        else:
                            try:
                                result_r = self.exec.redeem(pos)
                                st = str(result_r.get("status") or "")
                                if st in {"redeem_ok", "paper_redeem"}:
                                    action = "redeem_ok"
                                    sold += 1
                                    reason = f"{st} ingen live bud · {reason}"
                                    self.store.set_meta(f"redeem_err:{cid_s}", "")
                                else:
                                    action = "redeem_err"
                                    reason = f"redeem_err {st} · {reason}"
                                    self.store.set_meta(f"redeem_err:{cid_s}", f"{time.time():.0f}")
                            except Exception as exc:
                                action = "redeem_err"
                                reason = f"redeem_err {exc}"[:220]
                                self.store.set_meta(f"redeem_err:{cid_s}", f"{time.time():.0f}")
                    elif fam == "fed":
                        action = "hold"
                        reason = f"ingen live bud — Fed, ikke dust · {reason}"
                    else:
                        self.store.close_dust(str(cid or ""), str(side or "YES"))
                        action = "closed_dust"
                        sold += 1
                        reason = f"closed_dust ingen live bud · {reason}"
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
                reason = _scrub_exit_reason(reason)
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
        self._cycle_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.store.set_meta("cycle_id", self._cycle_id)
        self._bought = 0
        self._reject_counts = {k: 0 for k in REJECT_KEYS}
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
        n_redeem_ok = sum(1 for r in redeem_log if r.get("action") == "redeem_ok")
        if halt:
            log.warning("Stoppet: %s", halt)
            self._finish_cycle(
                halted=True,
                scanned=0,
                estimated=0,
                grok=0,
                accepted=0,
                rejected=0,
                kalshi=len(kalshi_log),
                kalshi_log=kalshi_log,
                xai_usd=0.0,
                exits=redeems,
                sold=0,
                redeem_ok=n_redeem_ok,
                exit_log=redeem_log,
                reason=halt,
                bankroll=bankroll,
                equity=equity,
            )
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
        by_id = {m["condition_id"]: m for m in markets if m.get("condition_id")}
        for cid, stub in by_open.items():
            by_id.setdefault(cid, stub)
            if stub.get("kalshi") and not (by_id.get(cid) or {}).get("kalshi"):
                by_id[cid]["kalshi"] = stub["kalshi"]
        try:
            self.arb.annotate_partitions(markets)
            for m in markets:
                cid = m.get("condition_id")
                if cid and cid in by_id:
                    by_id[cid]["partition_complete"] = m.get("partition_complete")
                    by_id[cid]["s_ask"] = m.get("s_ask")
                    by_id[cid]["s_bid"] = m.get("s_bid")
                    if m.get("book"):
                        by_id[cid]["book"] = m.get("book")
                    if m.get("no_book"):
                        by_id[cid]["no_book"] = m.get("no_book")
        except Exception as exc:
            log.warning("partition annotate: %s", exc)
        part_force = {}
        try:
            part_force = self.arb.partition_flatten(markets, open_pos)
        except Exception as exc:
            log.warning("partition flatten: %s", exc)
        sold_n, exit_log = self._run_exits(open_pos, {}, by_id, equity=equity, extra_force=part_force)
        exit_log = redeem_log + exit_log
        exits = redeems + sold_n
        open_pos = self.store.positions("open")
        bankroll, equity, open_pos = bankroll, equity, open_pos
        arb_tickets = self.arb.scan(markets, bankroll, equity=equity)
        arb_n = 0
        src_fill = {"stats": 0, "kalshi": 0, "complement": 0, "partition": 0, "maker": 0, "grok": 0}
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
                    self._bought += 1
                    src = str(ticket.source or "")
                    if src in src_fill:
                        src_fill[src] += 1
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
                                "source": "flatten",
                                "source_detail": "hedge-rollback",
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

        xai_cycle = 0.0
        grok_n = 0
        run_grok = (self._cycle_i % 6 == 0) and len(open_pos) <= 3
        if run_grok:
            try:
                prepaid = self.store.xai_prepaid_usd()
                spent = self.store.api_spend(hours=None)
                remaining = max(0.0, prepaid - spent) if prepaid > 0 else 1.0
                if remaining >= 0.02:
                    sample = [m for m in markets if not m.get("_open_only")][:3]
                    estimates = self.brain.estimate(sample) if sample else {}
                    grok_n = len(estimates)
                    usage = getattr(self.brain, "last_usage", {}) or {}
                    xai_cycle = float(usage.get("usd") or 0)
                    if xai_cycle:
                        self.store.add_api_cost(
                            xai_cycle,
                            str(usage.get("model") or ""),
                            int(usage.get("tokens") or 0),
                        )
                    for cid, est in estimates.items():
                        q = next((m.get("question") for m in sample if m.get("condition_id") == cid), cid)
                        self.store.log_decision(
                            condition_id=cid,
                            question=q,
                            p_hat=est.get("p_yes"),
                            action="grok-log",
                            reason="log-only conf=%s skip=%s" % (est.get("confidence"), est.get("skip")),
                            payload=est,
                        )
                    log.info("Grok log-only n=%s cycle=%s seats=%s", grok_n, self._cycle_i, len(open_pos))
            except Exception as exc:
                log.warning("Grok log-only: %s", exc)

        try:
            bankroll, equity, _ = self._refresh_portfolio()
        except Exception:
            pass
        n_comp = src_fill.get("complement", 0)
        n_k = src_fill.get("kalshi", 0)
        n_part = src_fill.get("partition", 0)
        n_maker = src_fill.get("maker", 0)
        n_block = int(getattr(self.arb, "n_blocked_spread", 0) or 0)
        log.info(
            "Syklus ferdig. n_complement=%s n_kalshi_clean=%s n_partition=%s n_blocked_spread=%s n_maker=%s kjoept=%s sold=%s",
            n_comp,
            n_k,
            n_part,
            n_block,
            n_maker,
            self._bought,
            sold_n,
        )
        self._finish_cycle(
            scanned=len(markets),
            estimated=grok_n,
            grok=grok_n,
            accepted=self._bought,
            arb=arb_n,
            kalshi=kalshi_n,
            kalshi_log=kalshi_log,
            rejected=0,
            exits=exits,
            sold=sold_n,
            redeem_ok=n_redeem_ok,
            exit_log=exit_log,
            xai_usd=round(xai_cycle, 4),
            bankroll=bankroll,
            equity=equity,
            stats=0,
            complement=n_comp,
            partition=n_part,
            maker=n_maker,
            n_complement=n_comp,
            n_kalshi_clean=n_k,
            n_partition=n_part,
            n_blocked_spread=n_block,
            by_source=src_fill,
        )
        return {"ok": True, **self.last_cycle}

    def run_forever(self) -> None:
        log.info("Desk kjører. DRY_RUN=%s interval=%ss", settings.dry_run, settings.loop_seconds)
        while True:
            try:
                self.cycle()
            except Exception:
                log.error("Syklus krasjet:\n%s", traceback.format_exc())
            time.sleep(settings.loop_seconds)
