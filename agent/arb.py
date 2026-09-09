"""Mekaniske kanter: sum-til-én og kjent/låst utfall. Ingen Grok, ingen X."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from agent.config import settings
from agent.risk import (
    CORE_PCT,
    DEPLOYED_MAX,
    HARD_NAME_PCT,
    MAX_SPORTS,
    Ticket,
    is_near_resolution,
    is_sports,
    parse_end,
    size_ticket,
    sizing_base,
)

log = logging.getLogger("arb")

# Etter fee: krev minst ~2 ¢ per sett
COMPLEMENT_MAX_ASK_SUM = 0.985
EVENT_MAX_ASK_SUM = 0.970
LOCKED_YES = 0.88
LOCKED_NO = 0.12


_parse_end = parse_end


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

    def _size_base(self, bankroll: float, equity: float = 0.0) -> float:
        try:
            deposited = float(self.store.deposited_usd(bankroll) or 0)
        except Exception:
            deposited = 0.0
        return sizing_base(deposited, equity or bankroll)

    def _leg(self, cost: float, ask_sz: float, size_base: float, cash: float, target_pct: float) -> tuple[float, float, str]:
        open_cost = sum(float(p["shares"]) * float(p["avg_cost"]) for p in self.store.positions("open"))
        powder = max(0.0, DEPLOYED_MAX * size_base - open_cost)
        return size_ticket(
            target_pct=target_pct,
            size_base=size_base,
            cost=cost,
            ask_size=ask_sz,
            cash=cash,
            name_room=HARD_NAME_PCT * size_base,
            deployed_room=powder,
        )

    def scan(self, markets: list[dict], bankroll: float, equity: float = 0.0) -> list[Ticket]:
        from agent.risk import Risk

        open_pos = self.store.positions("open")
        deposited = self._size_base(bankroll, equity)
        block = Risk(self.store).buys_blocked(equity or deposited, deposited)
        if block:
            log.info("Arb: hopper kjøp (%s)", block)
            return []
        open_ids = {p["condition_id"] for p in open_pos}
        sports_pos = [p for p in open_pos if is_sports(p)]
        sports_n = len(sports_pos)
        sports_halt = bool(Risk(self.store).sports_blocked(equity or deposited, deposited, cash=bankroll))
        at_cap = len(open_pos) >= settings.max_open_positions
        tickets: list[Ticket] = []
        tickets.extend(self._complements(markets, bankroll, open_ids, sports_n, sports_halt, at_cap))
        taken = {t.condition_id for t in tickets}
        if not at_cap:
            tickets.extend(self._locked(markets, bankroll, open_ids | taken, sports_n, sports_halt))
            taken = {t.condition_id for t in tickets}
            tickets.extend(self._kalshi_gap(markets, bankroll, open_ids | taken, sports_n, sports_halt))
        log.info("Arb: %s ben (complement/låst/kalshi-bekreftelse)", len(tickets))
        return tickets

    def _skip_sports(
        self, market: dict, sports_n: int, sports_halt: bool, tickets: list[Ticket], need: int = 1
    ) -> bool:
        if not is_sports(market):
            return False
        if sports_halt:
            return True
        n = sports_n + sum(
            1
            for t in tickets
            if is_sports({"question": t.question, "category": t.category, "event_key": t.event_key})
        )
        return n + need > MAX_SPORTS

    def _complements(
        self,
        markets: list[dict],
        bankroll: float,
        open_ids: set[str],
        sports_n: int = 0,
        sports_halt: bool = False,
        at_cap: bool = False,
    ) -> list[Ticket]:
        out: list[Ticket] = []
        size_base = self._size_base(bankroll)
        cash = bankroll
        for m in markets:
            cid = m.get("condition_id")
            if not cid or cid in open_ids:
                continue
            if at_cap and cid not in open_ids:
                continue
            if len(self.store.positions("open")) + len(out) + 2 > settings.max_open_positions:
                break
            if self._skip_sports(m, sports_n, sports_halt, out, need=2):
                continue
            yes_m = float(m.get("yes_mid") or 0)
            no_m = float(m.get("no_mid") or (1 - yes_m if yes_m else 0))
            if yes_m <= 0.02 or no_m <= 0.02:
                continue
            if (yes_m + no_m) > 0.995:
                continue
            yb = self._book(m, "yes")
            nb = self._book(m, "no")
            near, _ = is_near_resolution(m, yb)
            if near:
                continue
            yask = float(yb.get("best_ask") or 1)
            nask = float(nb.get("best_ask") or 1)
            if yask + nask > COMPLEMENT_MAX_ASK_SUM or yask <= 0.01 or nask <= 0.01:
                continue
            ysz = float(yb.get("ask_size") or 0)
            nsz = float(nb.get("ask_size") or 0)
            half = CORE_PCT[1] / 2
            y_usd, y_sh, ywhy = self._leg(yask, ysz, size_base, cash, half)
            n_usd, n_sh, nwhy = self._leg(nask, nsz, size_base, cash, half)
            if ywhy or nwhy:
                continue
            thesis = f"sum-til-én YES+NO ask {yask+nask:.3f}"
            out.append(_ticket(m, "YES", m["yes_token"], yb, yask, y_sh, thesis))
            out.append(_ticket(m, "NO", m["no_token"], nb, nask, n_sh, thesis))
            open_ids.add(cid)
            if len(out) >= 4:
                break
        return out

    def _event_sets(self, markets: list[dict], bankroll: float, open_ids: set[str], sports_n: int = 0, sports_halt: bool = False) -> list[Ticket]:
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
            if any(self._skip_sports(r, sports_n, sports_halt, out) for r in rows):
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

    def _locked(self, markets: list[dict], bankroll: float, open_ids: set[str], sports_n: int = 0, sports_halt: bool = False) -> list[Ticket]:
        now = datetime.now(timezone.utc)
        out: list[Ticket] = []
        size_base = self._size_base(bankroll)
        for m in markets:
            cid = m.get("condition_id")
            if not cid or cid in open_ids:
                continue
            if self._skip_sports(m, sports_n, sports_halt, out):
                continue
            if is_sports(m):
                continue
            end = _parse_end(m.get("end_date"))
            if not end:
                continue
            elapsed = (now - end).total_seconds()
            if elapsed < 90 * 60 or elapsed > 72 * 3600:
                continue
            yes = float(m.get("yes_mid") or m.get("mid") or 0.5)
            if LOCKED_NO < yes < LOCKED_YES:
                continue
            side = "YES" if yes >= LOCKED_YES else "NO"
            book = self._book(m, "yes" if side == "YES" else "no")
            token = m.get("yes_token") if side == "YES" else m.get("no_token")
            cost = float(book.get("best_ask") or (yes if side == "YES" else max(0.01, 1.0 - yes)))
            if cost <= 0.05 or cost >= 0.98:
                continue
            if side == "YES" and cost < LOCKED_YES - 0.04:
                continue
            ask_sz = float(book.get("ask_size") or 0)
            usd, shares, why = self._leg(cost, ask_sz, size_base, bankroll, CORE_PCT[1])
            if why:
                continue
            hours = (now - end).total_seconds() / 3600
            thesis = f"låst utfall {side} mid={yes:.2f} slutt for {hours:.0f}t siden"
            out.append(_ticket(m, side, token, book, cost, shares, thesis))
            open_ids.add(cid)
            if len(out) >= 4:
                break
        return out

    def _kalshi_gap(self, markets: list[dict], bankroll: float, open_ids: set[str], sports_n: int = 0, sports_halt: bool = False) -> list[Ticket]:
        """Kalshi confirmation ≥ 4 ¢ on a mapped pair. Not locked arb."""
        out: list[Ticket] = []
        size_base = self._size_base(bankroll)
        for m in markets:
            cid = m.get("condition_id")
            ks = m.get("kalshi") or {}
            if not cid or cid in open_ids or not ks:
                continue
            if self._skip_sports(m, sports_n, sports_halt, out):
                continue
            gap = float(ks.get("gap") or 0)
            if abs(gap) < 0.04:
                continue
            # gap = poly_yes - kalshi_yes. Poly dyr YES → kjøp NO.
            side = "NO" if gap > 0 else "YES"
            book = self._book(m, "yes" if side == "YES" else "no")
            token = m.get("yes_token") if side == "YES" else m.get("no_token")
            cost = float(book.get("best_ask") or 0)
            if cost < 0.22 or cost > 0.82:
                continue
            ask_sz = float(book.get("ask_size") or 0)
            usd, shares, why = self._leg(cost, ask_sz, size_base, bankroll, CORE_PCT[1])
            if why:
                continue
            thesis = f"Kalshi-bekreftelse {ks.get('yes')} vs Poly {float(m.get('yes_mid') or 0):.2f} gap={gap:+.2f} → {side}"
            out.append(_ticket(m, side, token, book, cost, shares, thesis))
            open_ids.add(cid)
            if len(out) >= 3:
                break
        return out
