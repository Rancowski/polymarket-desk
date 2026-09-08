"""Mekaniske kanter: sum-til-én og kjent/låst utfall. Ingen Grok, ingen X."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from agent.config import settings
from agent.risk import Ticket, _row_upnl, is_sports

log = logging.getLogger("arb")

# Etter fee: krev minst ~2 ¢ per sett
COMPLEMENT_MAX_ASK_SUM = 0.982
EVENT_MAX_ASK_SUM = 0.970
LOCKED_YES = 0.88
LOCKED_NO = 0.12


def _parse_end(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _ticket(market: dict, side: str, token: str, book: dict, cost: float, shares: float, thesis: str) -> Ticket:
    mid = float(book.get("mid") or cost)
    return Ticket(
        condition_id=market["condition_id"],
        question=market.get("question") or "",
        category=market.get("category") or "other",
        event_key=market.get("event_key") or market["condition_id"],
        side=side,
        token_id=token,
        mid=mid,
        best_bid=float(book.get("best_bid") or cost),
        best_ask=float(book.get("best_ask") or cost),
        spread=float(book.get("spread") or 0),
        p_hat=0.99 if side == "YES" else 0.99,
        edge_gross=max(0.0, 1.0 - cost) if side == "YES" else max(0.0, cost),
        edge_net=0.05,
        confidence="high",
        thesis=thesis,
        limit_price=round(min(0.99, max(0.01, cost)), 2),
        size_usd=round(shares * cost, 2),
        shares=round(shares, 2),
    )


class Arb:
    def __init__(self, scout: Any, store: Any) -> None:
        self.scout = scout
        self.store = store

    def _book(self, market: dict, which: str) -> dict:
        key = "book" if which == "yes" else "no_book"
        if market.get(key):
            return market[key]
        token = market.get("yes_token") if which == "yes" else market.get("no_token")
        if not token:
            return {}
        try:
            data = self.scout.book(token)
        except Exception:
            return {}
        if not data or data.get("synthetic"):
            return {}
        market[key] = data
        return data

    def scan(self, markets: list[dict], bankroll: float) -> list[Ticket]:
        open_pos = self.store.positions("open")
        open_ids = {p["condition_id"] for p in open_pos}
        sports_pos = [p for p in open_pos if is_sports(p)]
        sports_n = len(sports_pos)
        sports_losers = sum(1 for p in sports_pos if _row_upnl(p) < -0.25)
        at_cap = len(open_pos) >= settings.max_open_positions
        tickets: list[Ticket] = []
        tickets.extend(self._complements(markets, bankroll, open_ids, sports_n, sports_losers, at_cap))
        taken = {t.condition_id for t in tickets}
        if not at_cap:
            tickets.extend(self._locked(markets, bankroll, open_ids | taken, sports_n, sports_losers))
            taken = {t.condition_id for t in tickets}
            tickets.extend(self._kalshi_gap(markets, bankroll, open_ids | taken, sports_n, sports_losers))
            taken = {t.condition_id for t in tickets}
            tickets.extend(self._event_sets(markets, bankroll, open_ids | taken, sports_n, sports_losers))
        log.info("Arb: %s ben (complement/låst/kalshi/stige)", len(tickets))
        return tickets

    def _skip_sports(self, market: dict, sports_n: int, sports_losers: int, tickets: list[Ticket]) -> bool:
        if not is_sports(market):
            return False
        n = sports_n + sum(
            1
            for t in tickets
            if is_sports({"question": t.question, "category": t.category, "event_key": t.event_key})
        )
        if n >= 3 and sports_losers >= 3:
            return True
        return False

    def _complements(
        self,
        markets: list[dict],
        bankroll: float,
        open_ids: set[str],
        sports_n: int = 0,
        sports_losers: int = 0,
        at_cap: bool = False,
    ) -> list[Ticket]:
        out: list[Ticket] = []
        cap = settings.max_position_pct * bankroll
        for m in markets:
            cid = m.get("condition_id")
            if not cid or cid in open_ids:
                continue
            if at_cap and cid not in open_ids:
                continue
            if self._skip_sports(m, sports_n, sports_losers, out):
                continue
            if is_sports(m):
                ym = float(m.get("yes_mid") or 0)
                if ym <= 0.22 or ym >= 0.78:
                    continue
            yes_m = float(m.get("yes_mid") or 0)
            no_m = float(m.get("no_mid") or (1 - yes_m if yes_m else 0))
            if yes_m <= 0.02 or no_m <= 0.02:
                continue
            if (yes_m + no_m) > 0.995:
                continue
            yb = self._book(m, "yes")
            nb = self._book(m, "no")
            yask = float(yb.get("best_ask") or 1)
            nask = float(nb.get("best_ask") or 1)
            if yask + nask > COMPLEMENT_MAX_ASK_SUM or yask <= 0.01 or nask <= 0.01:
                continue
            ysz = float(yb.get("ask_size") or 0)
            nsz = float(nb.get("ask_size") or 0)
            budget = min(cap, bankroll * 0.08)
            shares = budget / max(0.02, yask + nask)
            if ysz:
                shares = min(shares, ysz / max(2, settings.min_book_multiple))
            if nsz:
                shares = min(shares, nsz / max(2, settings.min_book_multiple))
            if shares * (yask + nask) < 6:
                continue
            thesis = f"sum-til-én YES+NO ask {yask+nask:.3f}"
            out.append(_ticket(m, "YES", m["yes_token"], yb, yask, shares, thesis))
            out.append(_ticket(m, "NO", m["no_token"], nb, nask, shares, thesis))
            open_ids.add(cid)
            if len(out) >= 6:
                break
        return out

    def _event_sets(self, markets: list[dict], bankroll: float, open_ids: set[str], sports_n: int = 0, sports_losers: int = 0) -> list[Ticket]:
        groups: dict[str, list[dict]] = {}
        for m in markets:
            key = m.get("event_key") or ""
            if key:
                groups.setdefault(key, []).append(m)
        out: list[Ticket] = []
        cap = settings.max_position_pct * bankroll
        for _key, rows in groups.items():
            if len(rows) < 3:
                continue
            if any(r.get("condition_id") in open_ids for r in rows):
                continue
            if any(self._skip_sports(r, sports_n, sports_losers, out) for r in rows):
                continue
            mids = [float(r.get("yes_mid") or 0) for r in rows]
            if not (0.86 <= sum(mids) <= 1.14):
                continue
            books = [self._book(r, "yes") for r in rows]
            asks = [float(b.get("best_ask") or 1) for b in books]
            if any(a <= 0.01 or a >= 0.99 for a in asks):
                continue
            total = sum(asks)
            if total > EVENT_MAX_ASK_SUM:
                continue
            sizes = [float(b.get("ask_size") or 0) for b in books]
            budget = min(cap, bankroll * 0.10)
            shares = budget / max(0.05, total)
            for sz in sizes:
                if sz:
                    shares = min(shares, sz / max(2, settings.min_book_multiple))
            if shares * total < 10:
                continue
            thesis = f"event-sett ask-sum {total:.3f} ({len(rows)} utfall)"
            for row, book, ask in zip(rows, books, asks):
                out.append(_ticket(row, "YES", row["yes_token"], book, ask, shares, thesis))
                open_ids.add(row["condition_id"])
            if len(out) >= 12:
                break
        return out

    def _locked(self, markets: list[dict], bankroll: float, open_ids: set[str], sports_n: int = 0, sports_losers: int = 0) -> list[Ticket]:
        now = datetime.now(timezone.utc)
        out: list[Ticket] = []
        cap = settings.max_position_pct * bankroll
        for m in markets:
            cid = m.get("condition_id")
            if not cid or cid in open_ids:
                continue
            if self._skip_sports(m, sports_n, sports_losers, out):
                continue
            if is_sports(m):
                continue
            end = _parse_end(m.get("end_date"))
            if not end or (now - end).total_seconds() < 90 * 60:
                continue
            yes = float(m.get("yes_mid") or m.get("mid") or 0.5)
            if LOCKED_NO < yes < LOCKED_YES:
                continue
            side = "YES" if yes >= LOCKED_YES else "NO"
            book = self._book(m, "yes" if side == "YES" else "no")
            token = m.get("yes_token") if side == "YES" else m.get("no_token")
            cost = float(book.get("best_ask") or (yes if side == "YES" else max(0.01, 1.0 - yes)))
            if cost <= 0.01 or cost >= 0.99:
                continue
            if side == "YES" and cost < LOCKED_YES - 0.04:
                continue
            if side == "NO" and cost < (1 - LOCKED_NO) - 0.04:
                # buying NO should be cheap if YES is ~0.10 → NO ask ~0.90
                pass
            if side == "YES" and cost > 0.98:
                continue
            ask_sz = float(book.get("ask_size") or 0)
            usd = min(cap, bankroll * 0.08)
            shares = usd / cost
            if ask_sz:
                shares = min(shares, ask_sz / max(2, settings.min_book_multiple))
            if shares * cost < 8:
                continue
            hours = (now - end).total_seconds() / 3600
            thesis = f"låst utfall {side} mid={yes:.2f} slutt for {hours:.0f}t siden"
            out.append(_ticket(m, side, token, book, cost, shares, thesis))
            open_ids.add(cid)
            if len(out) >= 4:
                break
        return out

    def _kalshi_gap(self, markets: list[dict], bankroll: float, open_ids: set[str], sports_n: int = 0, sports_losers: int = 0) -> list[Ticket]:
        """Poly vs Kalshi ≥ 4 ¢: kjøp den billige siden på Polymarket (signal, ikke locked arb)."""
        out: list[Ticket] = []
        deposited = 0.0
        try:
            deposited = float(self.store.deposited_usd(bankroll) or 0)
        except Exception:
            deposited = 0.0
        size_base = deposited if deposited >= 1 else bankroll
        cap = min(0.12 * size_base, settings.max_position_pct * size_base)
        for m in markets:
            cid = m.get("condition_id")
            ks = m.get("kalshi") or {}
            if not cid or cid in open_ids or not ks:
                continue
            if self._skip_sports(m, sports_n, sports_losers, out):
                continue
            gap = float(ks.get("gap") or 0)
            if abs(gap) < 0.04:
                continue
            # gap = poly_yes - kalshi_yes. Poly dyr YES → kjøp NO.
            side = "NO" if gap > 0 else "YES"
            book = self._book(m, "yes" if side == "YES" else "no")
            token = m.get("yes_token") if side == "YES" else m.get("no_token")
            cost = float(book.get("best_ask") or 0)
            if cost < 0.22 or cost > 0.88:
                continue
            ask_sz = float(book.get("ask_size") or 0)
            usd = min(cap, max(0.06 * size_base, min(0.10 * size_base, cap)))
            shares = usd / cost if cost else 0
            if ask_sz:
                shares = min(shares, ask_sz / max(2, settings.min_book_multiple))
            if shares * cost < 8:
                continue
            thesis = f"Kalshi {ks.get('yes')} vs Poly {float(m.get('yes_mid') or 0):.2f} gap={gap:+.2f} → {side}"
            out.append(_ticket(m, side, token, book, cost, shares, thesis))
            open_ids.add(cid)
            if len(out) >= 3:
                break
        return out
