"""Mekaniske kanter: sum-til-én og kjent/låst utfall. Ingen Grok, ingen X."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from agent.config import SKIP_QUESTION_PATTERNS, settings
from agent.risk import (
    CORE_PCT,
    DEPLOYED_MAX,
    EVENT_COST_PCT,
    HARD_NAME_PCT,
    MAX_SPORTS,
    SPORTS_PCT,
    Ticket,
    hours_to_end,
    is_in_play_tape,
    is_map_bo,
    is_near_resolution,
    is_sports,
    parse_end,
    size_ticket,
    sizing_base,
)

log = logging.getLogger("arb")

STATS_ENABLED = False
COMPLEMENT_MAX_ASK_SUM = 0.975
PARTITION_ASK_MAX = 0.97
PARTITION_BID_FLAT = 1.03
PARTITION_BID_DONE = 1.02
KALSHI_GAP_MIN = 0.06
LEG_SPREAD_MAX = 0.04
MAKER_SPREAD_MIN = 0.06
EDGE_TARGET = 0.16
MAKER_PCT = (0.06, 0.08)
EVENT_MAX_ASK_SUM = 0.970
LOCKED_YES = 0.88
LOCKED_NO = 0.12


def complement_edge(yask: float, nask: float) -> bool:
    return yask > 0 and nask > 0 and (yask + nask) <= COMPLEMENT_MAX_ASK_SUM


def kalshi_should_buy(pair_ok_flag: bool, gap: float, ask: float, spread: float) -> bool:
    return (
        bool(pair_ok_flag)
        and abs(float(gap)) >= KALSHI_GAP_MIN
        and 0.18 <= float(ask) <= 0.82
        and float(spread) <= LEG_SPREAD_MAX
    )


def _tape_skip(m: dict) -> bool:
    q = str(m.get("question") or "").lower()
    if any(p in q for p in SKIP_QUESTION_PATTERNS):
        return True
    if is_in_play_tape(m) or is_map_bo(m):
        return True
    return False


def partition_complete(rows: list[dict]) -> bool:
    if len(rows) < 2:
        return False
    texts = [str(r.get("question") or "").lower() for r in rows]
    blob = " ".join(texts)
    if any("draw" in t or " tie" in f" {t} " or "uavgjort" in t for t in texts) and len(rows) >= 3:
        return True
    from agent.kalshi import _fed_want

    wants = {_fed_want(t) for t in texts}
    wants.discard(None)
    if len(wants) >= 3 and (wants & {"H0", "H25", "H26", "C25", "C26"}):
        return True
    mids: list[float] = []
    for r in rows:
        try:
            mids.append(float(r.get("yes_mid") or r.get("mid") or 0))
        except (TypeError, ValueError):
            continue
    s = sum(mids)
    if len(rows) == 2 and 0.94 <= s <= 1.06:
        return True
    if len(rows) >= 3 and 0.90 <= s <= 1.10:
        return True
    _ = blob
    return False


_parse_end = parse_end


def _finishing(m: dict) -> bool:
    """Don't farm in-play/collapsed books. mid≥0.90 only with a clean Kalshi pair."""
    try:
        mid = float(m.get("yes_mid") or m.get("mid") or 0)
    except (TypeError, ValueError):
        mid = 0.0
    if mid <= 0:
        return True
    hours = m.get("hours_left")
    try:
        h = float(hours) if hours is not None and hours != "" else None
    except (TypeError, ValueError):
        h = None
    if is_sports(m) and h is not None and h < 6:
        return True
    if mid >= 0.90:
        ks = m.get("kalshi") or {}
        if not (ks.get("ticker") and 0 < float(ks.get("yes") or 0) < 1):
            return True
    return False


def _ticket(
    market: dict,
    side: str,
    token: str,
    book: dict,
    cost: float,
    shares: float,
    thesis: str,
    source: str = "tape",
    source_detail: str | None = None,
    kalshi_ticker: str | None = None,
    kalshi_mid: float | None = None,
    pm_mid: float | None = None,
    gap_c: float | None = None,
    tif: str = "FAK",
) -> Ticket:
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
        source=source,
        source_detail=(source_detail or thesis)[:160],
        kalshi_ticker=kalshi_ticker,
        kalshi_mid=kalshi_mid,
        pm_mid=pm_mid if pm_mid is not None else mid,
        gap_c=gap_c,
        tif=tif,
    )


class Arb:
    def __init__(self, scout: Any, store: Any) -> None:
        self.scout = scout
        self.store = store
        self.n_blocked_spread = 0
        self.n_complement = 0
        self.n_kalshi_clean = 0
        self.n_partition = 0
        self.n_maker = 0

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

        self.n_blocked_spread = 0
        self.n_complement = 0
        self.n_kalshi_clean = 0
        self.n_partition = 0
        self.n_maker = 0
        open_pos = self.store.positions("open")
        size_base = self._size_base(bankroll, equity)
        block = Risk(self.store).buys_blocked(equity or size_base, size_base)
        if block:
            log.info("Arb: hopper kjøp (%s)", block)
            return []
        open_cost = sum(float(p["shares"]) * float(p["avg_cost"]) for p in open_pos)
        if size_base > 0 and open_cost >= DEPLOYED_MAX * size_base:
            log.info("Arb: deployed ≥ %.0f%% — only exits/redeem", DEPLOYED_MAX * 100)
            return []
        open_ids = {p["condition_id"] for p in open_pos}
        sports_pos = [p for p in open_pos if is_sports(p)]
        sports_n = len(sports_pos)
        sports_halt = bool(Risk(self.store).sports_blocked(equity or size_base, size_base, cash=bankroll))
        at_cap = len(open_pos) >= settings.max_open_positions
        tickets: list[Ticket] = []
        tickets.extend(self._complements(markets, bankroll, open_ids, sports_n, sports_halt, at_cap))
        taken = {t.condition_id for t in tickets}
        if not at_cap:
            tickets.extend(self._kalshi_gap(markets, bankroll, open_ids | taken, sports_n, sports_halt))
            taken = {t.condition_id for t in tickets}
            tickets.extend(self._partition(markets, bankroll, open_ids | taken, sports_n, sports_halt, open_pos))
            taken = {t.condition_id for t in tickets}
            tickets.extend(self._favorites(markets, bankroll, open_ids | taken, sports_n, sports_halt))
            taken = {t.condition_id for t in tickets}
            tickets.extend(self._makers(markets, bankroll, open_ids | taken))
        self.n_complement = sum(1 for t in tickets if t.source == "complement")
        self.n_kalshi_clean = sum(1 for t in tickets if t.source == "kalshi")
        self.n_partition = sum(1 for t in tickets if t.source == "partition")
        self.n_maker = sum(1 for t in tickets if t.source == "maker")
        log.info(
            "Arb: n_complement=%s n_kalshi_clean=%s n_partition=%s n_blocked_spread=%s n_maker=%s",
            self.n_complement,
            self.n_kalshi_clean,
            self.n_partition,
            self.n_blocked_spread,
            self.n_maker,
        )
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
        held_side = {
            (str(p.get("condition_id")), str(p.get("side") or "YES").upper())
            for p in self.store.positions("open")
        }
        for m in markets:
            cid = m.get("condition_id")
            if not cid:
                continue
            if at_cap:
                break
            if _tape_skip(m):
                continue
            if not (m.get("yes_token") and m.get("no_token")):
                continue
            yb = self._book(m, "yes")
            nb = self._book(m, "no")
            if not yb or not nb or yb.get("synthetic") or nb.get("synthetic"):
                continue
            yask = float(yb.get("best_ask") or 0)
            nask = float(nb.get("best_ask") or 0)
            if not complement_edge(yask, nask):
                continue
            yspread = float(yb.get("spread") or 0)
            nspread = float(nb.get("spread") or 0)
            legs: list[tuple[str, str, dict, float, float]] = []
            if yask <= nask:
                order = (("YES", m["yes_token"], yb, yask, yspread), ("NO", m["no_token"], nb, nask, nspread))
            else:
                order = (("NO", m["no_token"], nb, nask, nspread), ("YES", m["yes_token"], yb, yask, yspread))
            for side, token, book, cost, spr in order:
                if (cid, side) in held_side:
                    continue
                if spr > LEG_SPREAD_MAX:
                    self.n_blocked_spread += 1
                    continue
                legs.append((side, token, book, cost, float(book.get("ask_size") or 0)))
            if not legs:
                continue
            per = EDGE_TARGET / max(1, len(legs))
            ok_legs: list[Ticket] = []
            for side, token, book, cost, sz in legs:
                usd, shares, why = self._leg(cost, sz, size_base, cash, per)
                if why:
                    continue
                thesis = f"complement {side} ask {cost:.3f} YES+NO {yask+nask:.3f}"
                ok_legs.append(_ticket(m, side, token, book, cost, shares, thesis, source="complement"))
            if not ok_legs:
                continue
            out.extend(ok_legs)
            for t in ok_legs:
                held_side.add((cid, t.side))
            if len(out) >= 6:
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
                out.append(_ticket(row, "YES", row["yes_token"], book, ask, shares, thesis, source="complement"))
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
            if _finishing(m):
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
            out.append(_ticket(m, side, token, book, cost, shares, thesis, source="tape"))
            open_ids.add(cid)
            if len(out) >= 4:
                break
        return out

    def _kalshi_gap(self, markets: list[dict], bankroll: float, open_ids: set[str], sports_n: int = 0, sports_halt: bool = False) -> list[Ticket]:
        """Clean Kalshi pair. |gap|≥6c, ask 0.18–0.82, spread ≤0.04."""
        out: list[Ticket] = []
        size_base = self._size_base(bankroll)
        open_pos = self.store.positions("open")
        held_side = {
            str(p.get("condition_id")): str(p.get("side") or "YES").upper()
            for p in open_pos
        }
        open_qs = [str(p.get("question") or "") for p in open_pos]
        for m in markets:
            cid = m.get("condition_id")
            ks = m.get("kalshi") or {}
            ticker = str(ks.get("ticker") or "").strip()
            if not cid or not ticker:
                continue
            if cid in open_ids or str(cid) in held_side:
                continue
            if _tape_skip(m):
                continue
            from agent.kalshi import fed_seat_taken, pair_ok

            q = str(m.get("question") or "")
            pm = float(m.get("yes_mid") or m.get("mid") or ks.get("pm_yes") or 0)
            k_yes = float(ks.get("yes") or 0)
            ok_pair, _why = pair_ok(q, ticker, str(ks.get("title") or ""), k_yes, pm)
            if not ok_pair:
                continue
            fed_why = fed_seat_taken(open_qs, q)
            if fed_why:
                continue
            if self._skip_sports(m, sports_n, sports_halt, out):
                continue
            if not (0 < k_yes < 1 and 0 < pm < 1):
                continue
            gap = pm - k_yes
            if k_yes >= pm + KALSHI_GAP_MIN:
                side = "YES"
            elif gap >= KALSHI_GAP_MIN:
                side = "NO"
            else:
                continue
            book = self._book(m, "yes" if side == "YES" else "no")
            if not book or book.get("synthetic"):
                continue
            spread = float(book.get("spread") or 0)
            cost = float(book.get("best_ask") or 0)
            if spread > LEG_SPREAD_MAX:
                self.n_blocked_spread += 1
                continue
            if not kalshi_should_buy(True, gap if side == "NO" else (k_yes - pm), cost, spread):
                continue
            token = m.get("yes_token") if side == "YES" else m.get("no_token")
            ask_sz = float(book.get("ask_size") or 0)
            usd, shares, why = self._leg(cost, ask_sz, size_base, bankroll, EDGE_TARGET)
            if why:
                continue
            ticker = str(ks.get("ticker") or "")
            gap_c = round(gap * 100.0, 1)
            thesis = (
                f"Kalshi-bekreftelse {k_yes:.2f} vs Poly {pm:.2f} gap={gap:+.2f} → {side}"
            )
            detail = f"kalshi {ticker or '—'} gap {gap_c:+.0f}c vs {side}"
            out.append(
                _ticket(
                    m,
                    side,
                    token,
                    book,
                    cost,
                    shares,
                    thesis,
                    source="kalshi",
                    source_detail=detail,
                    kalshi_ticker=ticker or None,
                    kalshi_mid=k_yes if 0 < k_yes < 1 else None,
                    pm_mid=pm,
                    gap_c=gap_c,
                )
            )
            open_ids.add(cid)
            open_qs.append(q)
            if len(out) >= 3:
                break
        return out

    def _favorites(
        self,
        markets: list[dict],
        bankroll: float,
        open_ids: set[str],
        sports_n: int = 0,
        sports_halt: bool = False,
    ) -> list[Ticket]:
        """Stub. stats_enabled=false — no favorite_near tickets."""
        if not STATS_ENABLED:
            return []
        out: list[Ticket] = []
        size_base = self._size_base(bankroll)
        open_pos = list(self.store.positions("open"))
        open_qs = [str(p.get("question") or "") for p in open_pos]
        event_cost: dict[str, float] = {}
        for p in open_pos:
            ek = str(p.get("event_key") or p.get("condition_id") or "")
            event_cost[ek] = event_cost.get(ek, 0.0) + float(p.get("shares") or 0) * float(p.get("avg_cost") or 0)
        for m in markets:
            cid = m.get("condition_id")
            if not cid or cid in open_ids:
                continue
            q = str(m.get("question") or "").lower()
            if any(p in q for p in SKIP_QUESTION_PATTERNS):
                continue
            if is_in_play_tape(m) or is_map_bo(m):
                continue
            h = hours_to_end(m)
            if h is None or h < 8 or h > 48:
                continue
            try:
                yes_mid = float(m.get("yes_mid") or m.get("mid") or 0)
            except (TypeError, ValueError):
                yes_mid = 0.0
            if yes_mid >= 0.58:
                side, traded = "YES", yes_mid
            else:
                side, traded = "NO", (1.0 - yes_mid if 0 < yes_mid < 1 else 0.0)
            if traded < 0.58 or traded > 0.82:
                continue
            try:
                vol = float(m.get("volume_24h") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            if vol <= 0:
                continue
            if self._skip_sports(m, sports_n, sports_halt, out):
                continue
            from agent.kalshi import fed_seat_taken

            fed_why = fed_seat_taken(open_qs, str(m.get("question") or ""))
            if fed_why:
                continue
            book = self._book(m, "yes" if side == "YES" else "no")
            if not book or book.get("synthetic"):
                continue
            spread = float(book.get("spread") or 0)
            if spread > settings.max_spread:
                continue
            cost = float(book.get("best_ask") or 0)
            if cost < 0.58 or cost > 0.82:
                continue
            token = m.get("yes_token") if side == "YES" else m.get("no_token")
            if not token:
                continue
            event = str(m.get("event_key") or cid)
            room_evt = max(0.0, EVENT_COST_PCT * size_base - event_cost.get(event, 0.0))
            pct = SPORTS_PCT[1] if is_sports(m) else CORE_PCT[1]
            ask_sz = float(book.get("ask_size") or 0)
            open_cost = sum(float(p["shares"]) * float(p["avg_cost"]) for p in self.store.positions("open"))
            powder = max(0.0, DEPLOYED_MAX * size_base - open_cost)
            usd, shares, why = size_ticket(
                target_pct=pct,
                size_base=size_base,
                cost=cost,
                ask_size=ask_sz,
                cash=bankroll,
                name_room=min(HARD_NAME_PCT * size_base, room_evt),
                deployed_room=powder,
            )
            if why:
                log.info("stats skip %s %s", (m.get("question") or "")[:48], why)
                continue
            thesis = f"favorite_near {side} mid={traded:.2f} {h:.0f}h"
            out.append(
                _ticket(
                    m,
                    side,
                    token,
                    book,
                    cost,
                    shares,
                    thesis,
                    source="stats",
                    source_detail=thesis,
                    pm_mid=yes_mid,
                )
            )
            open_ids.add(cid)
            open_qs.append(str(m.get("question") or ""))
            event_cost[event] = event_cost.get(event, 0.0) + usd
            if len(out) >= 4:
                break
        return out

    def annotate_partitions(self, markets: list[dict]) -> None:
        """Stamp s_ask / s_bid / partition_complete on each market in a group."""
        groups: dict[str, list[dict]] = {}
        for m in markets:
            key = str(m.get("event_key") or "")
            if key:
                groups.setdefault(key, []).append(m)
        for key, rows in groups.items():
            complete = partition_complete(rows)
            s_ask = 0.0
            s_bid = 0.0
            labels: list[str] = []
            ok_books = True
            for r in rows:
                yb = self._book(r, "yes")
                if not yb or yb.get("synthetic"):
                    ok_books = False
                    continue
                s_ask += float(yb.get("best_ask") or 0)
                s_bid += float(yb.get("best_bid") or 0)
                labels.append((r.get("question") or "")[:40])
            for r in rows:
                r["partition_complete"] = bool(complete and ok_books and len(rows) >= 2)
                r["s_ask"] = round(s_ask, 4)
                r["s_bid"] = round(s_bid, 4)
                r["partition_labels"] = labels

    def _partition(
        self,
        markets: list[dict],
        bankroll: float,
        open_ids: set[str],
        sports_n: int,
        sports_halt: bool,
        open_pos: list,
    ) -> list[Ticket]:
        self.annotate_partitions(markets)
        out: list[Ticket] = []
        size_base = self._size_base(bankroll)
        groups: dict[str, list[dict]] = {}
        for m in markets:
            key = str(m.get("event_key") or "")
            if key:
                groups.setdefault(key, []).append(m)
        held_yes = {
            str(p.get("condition_id"))
            for p in open_pos
            if str(p.get("side") or "YES").upper() == "YES"
        }
        for key, rows in groups.items():
            complete = bool(rows and rows[0].get("partition_complete"))
            s_ask = float(rows[0].get("s_ask") or 0) if rows else 0.0
            s_bid = float(rows[0].get("s_bid") or 0) if rows else 0.0
            labels = (rows[0].get("partition_labels") if rows else None) or []
            log.info(
                "partition %s complete=%s n=%s S_ask=%.3f S_bid=%.3f | %s",
                key[:40],
                complete,
                len(rows),
                s_ask,
                s_bid,
                " ; ".join(labels)[:160],
            )
            if not complete:
                continue
            if any(_tape_skip(r) for r in rows):
                continue
            if s_ask <= 0 or s_ask > PARTITION_ASK_MAX:
                continue
            missing = [r for r in rows if str(r.get("condition_id") or "") not in open_ids and str(r.get("condition_id") or "") not in held_yes]
            if not missing:
                continue
            if any(self._skip_sports(r, sports_n, sports_halt, out) for r in missing):
                continue
            per = EDGE_TARGET / max(1, len(missing))
            for r in missing:
                yb = self._book(r, "yes")
                if not yb or yb.get("synthetic"):
                    continue
                if float(yb.get("spread") or 0) > LEG_SPREAD_MAX:
                    self.n_blocked_spread += 1
                    continue
                cost = float(yb.get("best_ask") or 0)
                if cost <= 0.01 or cost >= 0.99:
                    continue
                token = r.get("yes_token")
                if not token:
                    continue
                usd, shares, why = self._leg(cost, float(yb.get("ask_size") or 0), size_base, bankroll, per)
                if why:
                    continue
                thesis = f"sum_ask_lt_1 S_ask={s_ask:.3f} n={len(rows)}"
                out.append(_ticket(r, "YES", token, yb, cost, shares, thesis, source="partition", source_detail=thesis))
                open_ids.add(str(r.get("condition_id")))
        return out

    def partition_flatten(self, markets: list[dict], open_pos: list) -> dict[tuple, str]:
        """S_bid ≥ 1.03 → sell richest held YES until flat / S_bid < 1.02."""
        self.annotate_partitions(markets)
        force: dict[tuple, str] = {}
        groups: dict[str, list[dict]] = {}
        for m in markets:
            key = str(m.get("event_key") or "")
            if key:
                groups.setdefault(key, []).append(m)
        by_cid = {str(m.get("condition_id")): m for m in markets}
        for p in open_pos:
            if str(p.get("side") or "YES").upper() != "YES":
                continue
            cid = str(p.get("condition_id") or "")
            m = by_cid.get(cid)
            if not m or not m.get("partition_complete"):
                continue
            s_bid = float(m.get("s_bid") or 0)
            if s_bid < PARTITION_BID_FLAT:
                continue
            ek = str(m.get("event_key") or "")
            held = [
                x
                for x in open_pos
                if str(x.get("event_key") or "") == ek and str(x.get("side") or "YES").upper() == "YES"
            ]
            if not held:
                continue
            richest = max(held, key=lambda r: float(r.get("shares") or 0) * float(r.get("cur_price") or r.get("avg_cost") or 0))
            force[(str(richest.get("condition_id")), "YES")] = f"sum_bid_gt_1 S_bid={s_bid:.3f}"
        return force

    def _makers(self, markets: list[dict], bankroll: float, open_ids: set[str]) -> list[Ticket]:
        """Join bid when paid to wait. GTC, max 2. Not a view."""
        out: list[Ticket] = []
        size_base = self._size_base(bankroll)
        for m in markets:
            if len(out) >= 2:
                break
            cid = m.get("condition_id")
            if not cid or cid in open_ids:
                continue
            if _tape_skip(m):
                continue
            h = hours_to_end(m)
            if h is None or h > 72:
                continue
            try:
                mid = float(m.get("yes_mid") or m.get("mid") or 0)
            except (TypeError, ValueError):
                mid = 0.0
            if not (0.25 <= mid <= 0.75):
                continue
            yb = self._book(m, "yes")
            if not yb or yb.get("synthetic"):
                continue
            spread = float(yb.get("spread") or 0)
            if spread < MAKER_SPREAD_MIN:
                continue
            if mid <= 0.5:
                side, token, book = "YES", m.get("yes_token"), yb
            else:
                nb = self._book(m, "no")
                if not nb or nb.get("synthetic"):
                    continue
                side, token, book = "NO", m.get("no_token"), nb
            bid = float(book.get("best_bid") or 0)
            if bid < 0.18 or bid > 0.82:
                continue
            if not token:
                continue
            usd, shares, why = self._leg(bid, float(book.get("bid_size") or 0), size_base, bankroll, MAKER_PCT[1])
            if why:
                continue
            thesis = f"maker join bid {bid:.3f} spread {spread:.3f}"
            t = _ticket(m, side, token, book, bid, shares, thesis, source="maker", source_detail=thesis, tif="GTC")
            t.limit_price = round(bid, 2)
            out.append(t)
            open_ids.add(cid)
        return out
