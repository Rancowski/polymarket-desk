from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from agent.config import FEE_RATE, settings
from agent.store import Store

log = logging.getLogger("risk")


@dataclass
class Ticket:
    condition_id: str
    question: str
    category: str
    event_key: str
    side: str  # YES or NO
    token_id: str
    mid: float
    best_bid: float
    best_ask: float
    spread: float
    p_hat: float
    edge_gross: float
    edge_net: float
    confidence: str
    thesis: str
    limit_price: float
    size_usd: float
    shares: float
    source: str = ""
    source_detail: str = ""
    grok_p: float | None = None
    grok_conf: str | None = None
    kalshi_ticker: str | None = None
    kalshi_mid: float | None = None
    pm_mid: float | None = None
    gap_c: float | None = None
    cycle_id: str | None = None


def taker_fee_rate(category: str) -> float:
    return FEE_RATE.get(category, FEE_RATE["other"])


def expected_taker_fee_frac(price: float, category: str) -> float:
    """Fee as fraction of notional ≈ feeRate * (1-p) for buying at p."""
    p = min(0.99, max(0.01, price))
    rate = taker_fee_rate(category)
    # fee per share = rate * p * (1-p); notional per share = p
    return rate * (1.0 - p) if p > 0 else rate


def kelly_usd(p_hat: float, cost: float, bankroll: float) -> float:
    if cost >= 0.99 or cost <= 0.01:
        return 0.0
    edge = p_hat - cost
    if edge <= 0:
        return 0.0
    f_star = edge / (1.0 - cost)
    return max(0.0, settings.kelly_fraction * f_star * bankroll)


SPORTS_HINTS = (
    "cs2",
    "counter-strike",
    "lol",
    "league of legends",
    "league-of-legends",
    "dota",
    "valorant",
    "nba",
    "nfl",
    "mlb",
    "nhl",
    "ufc",
    "mma",
    "atp",
    "wta",
    "us open",
    "u.s. open",
    "australian open",
    "french open",
    "roland garros",
    "wimbledon",
    "grand slam",
    "atp finals",
    "alcaraz",
    "sinner",
    "djokovic",
    "swiatek",
    "soccer",
    "football",
    "tennis",
    "esport",
    "bundesliga",
    "premier league",
    "la liga",
    "serie a",
    "ligue 1",
    "champions league",
    "dortmund",
    "hockey",
    "cricket",
    "baseball",
    "basketball",
    "golf",
    "win on 20",
    "to win",
    "match winner",
    "gamerlegion",
    "furia",
    "forti",
    "map 1",
    "map 2",
    "map 3",
    "bo3",
    "bo5",
    " vs ",
    "-vs-",
)


def _live_bid(book: dict | None, pos: dict) -> float:
    """Ekte bud. Aldri avg_cost. Syntetisk bok teller som 0."""
    book = book or {}
    if not book.get("synthetic"):
        try:
            bid = float(book.get("best_bid") or 0)
        except (TypeError, ValueError):
            bid = 0.0
        if bid > 0:
            return bid
    try:
        cur = float(pos.get("cur_price") or 0)
    except (TypeError, ValueError):
        cur = 0.0
    if 0 < cur < 0.99:
        return cur
    try:
        shares = float(pos.get("shares") or 0)
        cv = float(pos.get("current_value") or 0)
        if shares > 0 and cv > 0:
            implied = cv / shares
            if 0 < implied < 0.99:
                return implied
    except (TypeError, ValueError):
        pass
    return 0.0


def is_sports(row: dict) -> bool:
    cat = str(row.get("category") or "").lower()
    if cat == "sports":
        return True
    blob = " ".join(
        str(row.get(k) or "")
        for k in ("event_key", "question", "eventSlug", "slug")
    ).lower()
    return any(h in blob for h in SPORTS_HINTS)


TOURNEY_HINTS = (
    "us open",
    "u.s. open",
    "australian open",
    "french open",
    "roland garros",
    "wimbledon",
    "grand slam",
    "atp finals",
    "win the us",
    "win the australian",
    "win the french",
)
_NAME_DROP = {
    "will", "the", "open", "wins", "winner", "match", "tennis", "slam", "grand",
    "australian", "french", "wimbledon", "roland", "garros", "finals", "title",
    "champion", "championship", "versus", "beat", "over", "after", "meeting",
    "carlos", "year", "this", "that", "from", "with", "into",
}


def _blob(row: dict) -> str:
    return " ".join(
        str(row.get(k) or "")
        for k in ("question", "outcome", "event_key", "eventSlug", "slug")
    ).lower()


def is_tournament(row: dict) -> bool:
    b = _blob(row)
    return any(h in b for h in TOURNEY_HINTS)


def is_match_market(row: dict) -> bool:
    b = f" {_blob(row)} "
    return " vs " in b or " versus " in b or "-vs-" in b


def _name_tokens(row: dict) -> set[str]:
    toks = re.findall(r"[a-z]{4,}", _blob(row))
    return {t for t in toks if t not in _NAME_DROP and t not in {h.replace(" ", "") for h in TOURNEY_HINTS}}


def _pos_cost(p: dict) -> float:
    return float(p.get("shares") or 0) * float(p.get("avg_cost") or 0)


def same_player_conflicts(open_pos: list) -> dict[tuple, str]:
    """Tournament YES on X vs match NO on X (and reverse). Flatten cheaper/smaller leg."""
    force: dict[tuple, str] = {}
    rows = [p for p in open_pos if is_sports(p) or is_tournament(p)]
    for i, a in enumerate(rows):
        ka = "match" if is_match_market(a) else ("tournament" if is_tournament(a) else "")
        if not ka:
            continue
        na = _name_tokens(a)
        if not na:
            continue
        for b in rows[i + 1 :]:
            kb = "match" if is_match_market(b) else ("tournament" if is_tournament(b) else "")
            if {ka, kb} != {"tournament", "match"}:
                continue
            shared = na & _name_tokens(b)
            if not shared:
                continue
            sa = str(a.get("side") or "YES").upper()
            sb = str(b.get("side") or "YES").upper()
            if sa == sb:
                continue
            victim = a if _pos_cost(a) <= _pos_cost(b) else b
            key = (str(victim.get("condition_id")), str(victim.get("side") or "YES"))
            who = " ".join(sorted(shared)[:3])
            force[key] = f"motsier {who} — flatten minste"
    return force


def ticket_player_conflict(market: dict, side: str, open_pos: list) -> str | None:
    probe = {
        "question": market.get("question"),
        "outcome": market.get("outcome") or side,
        "event_key": market.get("event_key"),
        "category": market.get("category"),
        "side": side,
    }
    fake = [{**p} for p in open_pos] + [probe]
    hits = same_player_conflicts(fake)
    if hits:
        return next(iter(hits.values()))
    return None


def _row_upnl(p: dict) -> float:
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


PRIMARY_CATS = {"economics", "finance", "crypto", "politics", "geopolitics"}
PRIMARY_HINTS = (
    "fed", "fomc", "federal reserve", "interest rate", "cpi",
    "bitcoin", "btc", "ethereum", "eth ", " eth",
    "trump",
)
SPORTS_PX = (0.22, 0.82)
DEPLOYED_MAX = 0.75
EQUITY_SPORTS_HALT = 0.70
MAX_SPORTS = 4
HARD_NAME_PCT = 0.18
EVENT_COST_PCT = 0.25
CASH_SPORTS_MIN = 0.15
SPORTS_PCT = (0.06, 0.08)
CORE_PCT = (0.10, 0.14)
HALF_CORE_PCT = (0.05, 0.07)
MIN_NOTIONAL_PCT = 0.05
DEPTH_USE_PCT = 0.50
CASH_USE_PCT = 0.90
DUST_VALUE_PCT = 0.001  # 0.1 % of sizing base
VENUE_MIN_SHARES = 5.0  # CLOB share minimum, not a dollar floor
REUP_MIN_PNL = 0.10


def is_primary(row: dict) -> bool:
    if is_sports(row):
        return False
    cat = str(row.get("category") or "").lower()
    if cat in PRIMARY_CATS:
        return True
    blob = f"{row.get('question') or ''} {row.get('event_key') or ''} {row.get('slug') or ''}".lower()
    return any(h in blob for h in PRIMARY_HINTS)


def sizing_base(deposited: float, equity: float = 0.0) -> float:
    """Deposited if set, else equity. Never a hardcoded dollar book."""
    try:
        dep = float(deposited or 0)
    except (TypeError, ValueError):
        dep = 0.0
    if dep > 0:
        return dep
    try:
        return max(0.0, float(equity or 0))
    except (TypeError, ValueError):
        return 0.0


def min_notional(base: float) -> float:
    return max(0.0, MIN_NOTIONAL_PCT * float(base or 0))


def dust_cutoff(base: float) -> float:
    return max(0.0, DUST_VALUE_PCT * float(base or 0))


def size_ticket(
    *,
    target_pct: float,
    size_base: float,
    cost: float,
    ask_size: float,
    cash: float,
    name_room: float,
    deployed_room: float,
) -> tuple[float, float, str]:
    """Percent-only size. Returns (usd, shares, skip_reason). Never a stub below 5% of base."""
    if size_base <= 0 or cost <= 0:
        return 0.0, 0.0, "ingen sizing-base"
    floor = min_notional(size_base)
    depth_usd = max(0.0, float(ask_size or 0) * cost)
    if depth_usd < floor:
        return 0.0, 0.0, "bok tynn (<5% dybde)"
    cap18 = HARD_NAME_PCT * size_base
    room = min(
        cap18,
        max(0.0, name_room),
        max(0.0, deployed_room),
        CASH_USE_PCT * max(0.0, cash),
        DEPTH_USE_PCT * depth_usd,
    )
    usd = min(target_pct * size_base, room)
    if usd < floor:
        if room >= floor:
            usd = floor
        else:
            return 0.0, 0.0, "under 5% og ikke rom for bump"
    shares = usd / cost
    if shares < VENUE_MIN_SHARES:
        bumped = VENUE_MIN_SHARES * cost
        if bumped < floor or bumped > room:
            return 0.0, 0.0, "venue-min andeler under 5% eller over cap"
        shares = VENUE_MIN_SHARES
        usd = bumped
    return usd, shares, ""


def parse_end(raw: Any) -> datetime | None:
    """Parse Polymarket end dates. None if missing/unparsed — never treat as due now."""
    if raw is None or raw is False or raw == "":
        return None
    if isinstance(raw, datetime):
        dt = raw
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        if 1e9 < ts < 2e10:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        return None
    text = str(raw).strip()
    if not text or text.lower() in {"none", "null", "undefined"}:
        return None
    text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def hours_to_end(market: dict) -> float | None:
    try:
        h = market.get("hours_left")
        if h is not None and h != "":
            return float(h)
    except (TypeError, ValueError):
        pass
    dt = parse_end(
        market.get("end_date") or market.get("endDate") or market.get("endDateIso")
    )
    if dt is None:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0


def event_hours_to_end(market: dict) -> float | None:
    """One clock per event: use the furthest parsed sibling so a bad short date cannot split Fed hike/hold/cut."""
    hours: list[float] = []
    h = hours_to_end(market)
    if h is not None:
        hours.append(h)
    for s in market.get("siblings") or []:
        if not isinstance(s, dict):
            continue
        sh = hours_to_end(s)
        if sh is not None:
            hours.append(sh)
    if not hours:
        return None
    return max(hours)


def is_near_resolution(market: dict, book: dict | None = None) -> tuple[bool, str]:
    """Near-res only with time+mid, or 94c lottery on a live book. Missing end_date is not near-res."""
    book = book or {}
    try:
        mid = float(book.get("mid") or market.get("mid") or market.get("yes_mid") or 0)
    except (TypeError, ValueError):
        mid = 0.0
    try:
        live_bid = float(book.get("best_bid") or 0)
    except (TypeError, ValueError):
        live_bid = 0.0
    live = (not book.get("synthetic")) and live_bid > 0
    hours = event_hours_to_end(market)
    if hours is None:
        log.info("no_end %s", (market.get("question") or "")[:80])
        if live and mid >= 0.94:
            return True, "nær resolusjon"
        return False, "no_end"
    if hours <= 72 and (mid >= 0.90 or mid <= 0.10):
        return True, "nær resolusjon"
    if mid >= 0.94 and live:
        return True, "nær resolusjon"
    return False, ""


def resolved_state(pos: dict, book: dict | None = None, market: dict | None = None) -> str | None:
    """winner | loser | None. Resolved winners must be redeemed, never FAK/dust."""
    book = book or {}
    market = market or {}
    try:
        mid = float(pos.get("cur_price") or book.get("mid") or market.get("mid") or 0)
    except (TypeError, ValueError):
        mid = 0.0
    try:
        bid = float(book.get("best_bid") or 0)
    except (TypeError, ValueError):
        bid = 0.0
    if book.get("synthetic"):
        bid = 0.0
    redeemable = bool(pos.get("redeemable") or market.get("redeemable"))
    closed = bool(market.get("closed") or market.get("resolved") or pos.get("closed"))
    if redeemable or (mid >= 0.99 and bid <= 0.02) or (closed and mid >= 0.90):
        if mid <= 0.05 and not redeemable:
            return "loser"
        return "winner"
    if closed and mid <= 0.05:
        return "loser"
    if mid <= 0.01 and bid <= 0.01 and closed:
        return "loser"
    return None


def clip_usd(p_hat: float, cost: float, bankroll: float, cap: float) -> float:
    """Kelly clip in percent space. Floor is 5% of bankroll, never a dollar stub."""
    kelly = kelly_usd(p_hat, cost, bankroll)
    if kelly <= 0 or bankroll <= 0:
        return 0.0
    floor = min(cap, MIN_NOTIONAL_PCT * bankroll)
    return min(cap, max(kelly, floor))


class Risk:
    def __init__(self, store: Store) -> None:
        self.store = store

    def halted(self) -> str | None:
        if settings.halt_file.exists():
            return f"HALT-fil finnes: {settings.halt_file}"
        return None

    def buys_blocked(self, equity: float, deposited: float) -> str | None:
        """HALT-fil only. No daily freeze, no 85% desk stop."""
        _ = (equity, deposited)
        return self.halted()

    def sports_blocked(self, equity: float, deposited: float, cash: float | None = None) -> str | None:
        """No category buy-bans. Seat caps live in evaluate()."""
        _ = (equity, deposited, cash)
        return None

    def evaluate(
        self,
        market: dict,
        book: dict,
        estimate: dict,
        bankroll: float,
        equity: float,
        min_edge: float | None = None,
        probe: bool = False,
    ) -> tuple[Ticket | None, str]:
        deposited = self.store.deposited_usd(0.0)
        block = self.buys_blocked(equity, deposited)
        if block:
            return None, block
        cid = market.get("condition_id") or ""
        if self.store.is_bad_market(cid):
            return None, "CLOB-blacklist"
        if estimate.get("skip"):
            return None, estimate.get("skip_reason") or "brain skip"
        conf = str(estimate.get("confidence") or "medium").lower()
        p_yes = float(estimate["p_yes"])
        mid = float(book.get("mid") or market.get("mid") or 0.5)
        near, near_why = is_near_resolution(market, book)
        if near:
            return None, near_why
        if near_why == "no_end":
            log.info("no_end — hopper nær-res, andre filter %s", (market.get("question") or "")[:60])
        if is_sports(market) and (mid >= 0.88 or mid <= 0.12):
            return None, "sports nær avgjort"
        ks_pair = market.get("kalshi") or {}
        clean_kalshi = bool(ks_pair.get("ticker")) and 0 < float(ks_pair.get("yes") or 0) < 1
        if conf == "low":
            return None, "confidence=low"
        if mid >= 0.90 and not clean_kalshi:
            return None, "mid≥0.90 uten Kalshi-par"
        spread = float(book.get("spread") or 0)
        if book.get("synthetic"):
            return None, "syntetisk bok"
        if (market.get("no_book") or {}).get("synthetic"):
            return None, "syntetisk bok"
        if spread > settings.max_spread:
            return None, f"spread {spread:.3f} > max"

        yes_edge = p_yes - mid
        no_edge = (1.0 - p_yes) - (1.0 - mid)
        if yes_edge >= no_edge:
            side = "YES"
            token = market["yes_token"]
            p_hat = p_yes
            edge_gross = yes_edge
            best_ask = float(book.get("best_ask") or mid)
            best_bid = float(book.get("best_bid") or mid)
            cost = best_ask
            book_sz = float(book.get("ask_size") or 0)
        else:
            side = "NO"
            token = market["no_token"]
            p_hat = 1.0 - p_yes
            edge_gross = no_edge
            nb = market.get("no_book") or {}
            best_ask = float(nb.get("best_ask") or max(0.01, 1.0 - float(book.get("best_bid") or mid)))
            best_bid = float(nb.get("best_bid") or max(0.01, 1.0 - float(book.get("best_ask") or mid)))
            cost = best_ask
            book_sz = float(nb.get("ask_size") or book.get("bid_size") or 0)
            if nb.get("spread") is not None:
                spread = float(nb["spread"])

        fee_frac = expected_taker_fee_frac(cost, market["category"])
        extra = 0.015 if is_sports(market) else 0.0
        edge_gross = p_hat - cost
        edge_net = edge_gross - fee_frac - settings.model_haircut - extra
        need = settings.min_net_edge if min_edge is None else min_edge
        if is_sports(market):
            need = max(need, 0.022)
        if not clean_kalshi:
            yask = float(book.get("best_ask") or cost)
            nb = market.get("no_book") or {}
            nask = float(nb.get("best_ask") or 0)
            if nask <= 0:
                nask = max(0.01, 1.0 - float(book.get("best_bid") or mid))
            if yask + nask >= 0.985:
                return None, f"YES+NO ask {yask + nask:.3f} ≥ 0.985"
        if edge_net < (0.0 if probe else need):
            return None, f"edge_net {edge_net:.3f} < {need}"
        sports = is_sports(market) or is_tournament(market)
        open_pos = self.store.positions("open")
        if sports and (cost <= SPORTS_PX[0] or cost >= SPORTS_PX[1]):
            return None, "sports ekstrem-pris"
        event = market.get("event_key") or market["condition_id"]
        same_cid = [p for p in open_pos if p.get("condition_id") == cid]
        same_side = [p for p in same_cid if str(p.get("side") or "").upper() == side]
        hedge = bool(same_cid) and not same_side
        reup = False
        if same_side:
            if sports:
                return None, "ingen påfyll sports"
            held = same_side[0]
            held_cost = float(held.get("shares") or 0) * float(held.get("avg_cost") or 0)
            upnl = _row_upnl(held)
            ks = market.get("kalshi") or {}
            k_yes = float(ks.get("yes") or 0)
            named = bool(ks.get("ticker"))
            kalshi_add = (
                named
                and side == "YES"
                and 0.20 <= cost <= 0.80
                and k_yes >= cost + 0.05
            )
            if kalshi_add:
                reup = True
            elif held_cost <= 0 or upnl < REUP_MIN_PNL * held_cost:
                return None, "aldri average down"
            else:
                reup = True
        if len(open_pos) >= settings.max_open_positions and not hedge and not reup:
            return None, "max 10 åpne (kun hedge/påfyll)"
        sports_pos = [p for p in open_pos if is_sports(p)]
        if sports and len(sports_pos) >= MAX_SPORTS:
            return None, "maks 4 sports"
        same_event_cost = sum(
            float(p["shares"]) * float(p["avg_cost"])
            for p in open_pos
            if p.get("event_key") == event
        )
        cat_cost = sum(
            float(p["shares"]) * float(p["avg_cost"])
            for p in open_pos
            if p.get("category") == market["category"]
        )
        open_cost = sum(float(p["shares"]) * float(p["avg_cost"]) for p in open_pos)

        size_base = sizing_base(deposited, equity)
        longshot = cost <= 0.28
        cheap_sports = (sports or is_tournament(market)) and cost < 0.40
        live_sport = sports and is_match_market(market)
        if live_sport and conf == "medium":
            return None, "cs_live"
        if sports or longshot or cheap_sports:
            _floor_pct, cap_pct = SPORTS_PCT
        elif conf == "medium":
            _floor_pct, cap_pct = HALF_CORE_PCT
        else:
            _floor_pct, cap_pct = CORE_PCT
        cap = min(cap_pct * size_base, HARD_NAME_PCT * size_base)
        remaining_event = max(0.0, EVENT_COST_PCT * size_base - same_event_cost)
        remaining_cat = max(0.0, settings.max_category_pct * size_base - cat_cost)
        powder = max(0.0, DEPLOYED_MAX * size_base - open_cost)
        sized, shares, skip_sz = size_ticket(
            target_pct=cap_pct,
            size_base=size_base,
            cost=cost,
            ask_size=book_sz,
            cash=bankroll,
            name_room=min(remaining_event, remaining_cat, cap),
            deployed_room=powder,
        )
        if skip_sz:
            return None, skip_sz

        # Kryss ask så ordren fylles (GTC mid+1¢ blir ofte liggende)
        limit = round(min(0.99, max(0.01, cost)), 2)
        if probe:
            limit = round(min(0.99, cost + 0.01), 2)

        ks = market.get("kalshi") or {}
        k_ticker = str(ks.get("ticker") or "").strip() or None
        try:
            k_yes = float(ks.get("yes") or 0)
        except (TypeError, ValueError):
            k_yes = 0.0
        try:
            gap = float(ks.get("gap")) if ks.get("gap") not in (None, "") else None
        except (TypeError, ValueError):
            gap = None
        gap_c = round(gap * 100.0, 2) if gap is not None else None
        detail = f"grok p={p_hat:.2f} ask={cost:.2f} edge_net={edge_net:.2f}"
        if k_ticker:
            detail = f"{detail} kalshi {k_ticker}"
        ticket = Ticket(
            condition_id=market["condition_id"],
            question=market["question"],
            category=market["category"],
            event_key=event,
            side=side,
            token_id=token,
            mid=mid,
            best_bid=best_bid,
            best_ask=best_ask,
            spread=spread,
            p_hat=p_hat,
            edge_gross=edge_gross,
            edge_net=edge_net,
            confidence=conf,
            thesis=("probe " if probe else "") + estimate.get("thesis", ""),
            limit_price=limit,
            size_usd=sized,
            shares=round(shares, 2),
            source="grok",
            source_detail=detail,
            grok_p=p_yes,
            grok_conf=conf,
            kalshi_ticker=k_ticker,
            kalshi_mid=k_yes if k_ticker and 0 < k_yes < 1 else None,
            pm_mid=mid,
            gap_c=gap_c if k_ticker else None,
            cycle_id=self.store.get_meta("cycle_id") or None,
        )
        return ticket, "ok"

    def evaluate_exit(
        self,
        pos: dict,
        book: dict,
        estimate: dict | None,
        bankroll: float,
        kalshi: dict | None = None,
        force_reason: str | None = None,
        market: dict | None = None,
    ) -> tuple[dict | None, str]:
        shares = float(pos.get("shares") or 0)
        avg = float(pos.get("avg_cost") or 0)
        if shares <= 0 or avg <= 0:
            return None, "tom posisjon"
        cid = pos.get("condition_id")
        side = str(pos.get("side") or "YES").upper()
        sports = is_sports(pos)
        book_bid = 0.0
        if book and not book.get("synthetic"):
            try:
                book_bid = float(book.get("best_bid") or 0)
            except (TypeError, ValueError):
                book_bid = 0.0
        live = book_bid
        missing = book_bid <= 0
        mark = live if live > 0 else _live_bid(book, pos)
        value = shares * mark
        state = resolved_state(pos, book, market)
        if state == "winner":
            return None, "resolved — redeem"
        if state == "loser":
            return self._exit_ticket(
                pos, book, shares, 0.0, "resolved loser — close locally",
                kind="resolved_loser", best_bid=book_bid,
            ), "ok"
        try:
            book_mid = float((book or {}).get("mid") or 0)
        except (TypeError, ValueError):
            book_mid = 0.0
        hwm_key = f"hwm:{cid}:{side}"
        try:
            hwm = float(self.store.get_meta(hwm_key) or 0)
        except (TypeError, ValueError):
            hwm = 0.0
        if live > hwm:
            hwm = live
            self.store.set_meta(hwm_key, f"{hwm:.4f}")
        peak = max(live, mark, book_mid, hwm)
        # Peak ≥ 0.98: redeem or FAK at bid ≥ 0.98. Never dump 0.999 → 0.40.
        if peak >= 0.98 or mark >= 0.99:
            redeemable = bool(
                pos.get("redeemable")
                or (market or {}).get("redeemable")
                or (market or {}).get("closed")
                or (market or {}).get("resolved")
            )
            if redeemable or (mark >= 0.99 and live <= 0.02) or (book_mid >= 0.99 and live <= 0.02):
                if live < 0.98:
                    return None, "resolved — redeem"
            if live >= 0.98:
                return self._exit_ticket(
                    pos, book, shares, live,
                    f"ta {live:.3f} ≥0.98",
                    kind="tp", best_bid=live,
                ), "ok"
            return None, f"hold peak {peak:.3f} — ikke dump bid {live:.3f}"
        dep = self.store.deposited_usd(0.0)
        base = sizing_base(dep, bankroll)
        dust_cut = dust_cutoff(base)
        # Dust/dead SPORTS only — never Fed, never a mid≈1 winner, never a live bid ≥ 0.02.
        if sports and live < 0.02 and mark < 0.90 and (
            missing or book_bid <= 0.01 or mark <= 0.01 or (dust_cut > 0 and value < dust_cut)
        ):
            px = min(0.01, book_bid) if book_bid > 0 else 0.01
            return self._exit_ticket(
                pos, book, shares, px,
                f"død sports bid={book_bid:.4f} mark={mark:.4f} val={value:.4f}",
                kind="dust",
                best_bid=book_bid,
            ), "ok"
        if force_reason:
            if peak >= 0.98:
                return None, f"hold peak {peak:.3f} — ikke trim-dump ({force_reason})"
            if book_bid < 0.02:
                return None, f"hold trim — ingen live bud ({force_reason})"
            return self._exit_ticket(
                pos, book, shares, book_bid, force_reason, kind="stop", best_bid=book_bid
            ), "ok"
        counterparts = [
            p for p in self.store.positions("open")
            if p.get("condition_id") == cid and str(p.get("side") or "").upper() != side
        ]
        if counterparts:
            return None, "complement-hold"
        pnl_pct = (live - avg) / avg if avg and live > 0 else 0.0
        mid = float((book or {}).get("mid") or live or mark)

        trail_px = hwm * 0.92 if hwm > 0 else 0.0
        armed = hwm > 0 and avg > 0 and (hwm >= avg * 1.18 or pnl_pct >= 0.18)
        if sports and live >= 0.02 and armed and live <= trail_px:
            return self._exit_ticket(
                pos, book, shares, live,
                f"sports trail −8% fra topp {hwm:.3f} (≤{trail_px:.3f}) bud {live:.3f}",
                kind="tp", best_bid=live,
            ), "ok"

        if sports and live > 0 and (live <= avg * 0.85 or live <= 0.03):
            return self._exit_ticket(
                pos, book, shares, live, f"sports stopp-tap {pnl_pct:.1%} bid {live:.3f}",
                kind="stop", best_bid=live,
            ), "ok"
        if sports and live <= 0:
            return None, f"hold tom/resolved bok bid {live:.4f} — ikke FAK"
        if not sports and live >= 0.02 and pnl_pct <= -0.18:
            return self._exit_ticket(
                pos, book, shares, live, f"stopp-tap {pnl_pct:.1%} bid {live:.3f}",
                kind="stop", best_bid=live,
            ), "ok"
        if not sports and live >= 0.02 and (live >= 0.92 or pnl_pct >= 0.22):
            return self._exit_ticket(
                pos, book, shares, live, f"ta gevinst {pnl_pct:.1%} bid {live:.3f}",
                kind="tp", best_bid=live,
            ), "ok"

        hours_open = 0.0
        raw_ts = pos.get("opened_ts") or pos.get("last_ts")
        if raw_ts:
            try:
                ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                hours_open = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
            except ValueError:
                hours_open = 0.0
        hours_left = None
        if market:
            hours_left = market.get("hours_left")
        if sports and live >= 0.02 and mid < 0.15 and (hours_open >= 3 or (hours_left is not None and float(hours_left) < -3)):
            return self._exit_ticket(
                pos, book, shares, live, f"kamp >3t og mid {mid:.3f}<0.15",
                kind="stop", best_bid=live,
            ), "ok"

        ks = kalshi or {}
        k_yes = float(ks.get("yes") or 0)
        ticker = str(ks.get("ticker") or "")
        if sports:
            k_yes = 0.0
            ticker = ""
        if not sports and ticker:
            # Kalshi YES → our side. Never compare k_yes to a NO mark.
            k_hat = k_yes if side == "YES" else 1.0 - k_yes
            pm_hat = live if live > 0 else mark
            against_mid = k_hat <= pm_hat - 0.07
            against_cost = k_hat + 0.07 < avg
            crash = k_hat < 0.02 and pm_hat > 0.40
            if live >= 0.02 and (against_mid or against_cost or crash):
                return self._exit_ticket(
                    pos, book, shares, live,
                    f"Kalshi {k_hat:.2f} vs PM {pm_hat:.2f} kost {avg:.2f} — selg",
                    kind="stop", best_bid=live,
                ), "ok"

        if estimate and not estimate.get("skip") and live >= 0.02:
            try:
                p_yes = float(estimate["p_yes"])
            except (TypeError, ValueError, KeyError):
                p_yes = None
            if p_yes is not None:
                p_hat = p_yes if side == "YES" else 1.0 - p_yes
                faded_to_mid = abs(p_hat - mid) < 0.02 and abs(mid - avg) < 0.03
                if p_hat + 0.05 <= avg and not faded_to_mid:
                    return self._exit_ticket(
                        pos, book, shares, live, f"p_hat {p_hat:.2f} ≥5c under kost {avg:.2f}",
                        kind="stop", best_bid=live,
                    ), "ok"
        return None, f"hold bid {live:.3f} pnl {pnl_pct:.1%}"

    def _exit_ticket(
        self,
        pos: dict,
        book: dict,
        shares: float,
        price: float,
        reason: str,
        kind: str = "stop",
        best_bid: float = 0.0,
    ) -> dict:
        live = float(best_bid or 0)
        px = float(price or 0)
        if kind != "dust" and kind != "resolved_loser" and live >= 0.10:
            tick = 0.01
            px = math.floor(live / tick + 1e-12) * tick
            px = round(min(0.99, px), 2)
        elif kind == "dust" or px < 0.10:
            tick = 0.001 if px < 0.10 or kind == "dust" else 0.01
            if px < tick:
                px = tick
            else:
                px = math.floor(px / tick + 1e-12) * tick
            px = round(max(tick, min(0.99, px)), 4)
        else:
            tick = 0.01
            src = live if live >= 0.02 else px
            px = math.floor(src / tick + 1e-12) * tick
            px = round(max(tick, min(0.99, px)), 2)
        low = str(reason or "").lower()
        if kind == "dust":
            src_name = "flatten"
        elif kind == "tp" and "trail" in low:
            src_name = "exit_trail"
        elif kind == "tp":
            src_name = "exit_take"
        elif "kalshi" in low:
            src_name = "exit_kalshi"
        elif "flatten" in low or "trim" in low or "motsier" in low:
            src_name = "flatten"
        else:
            src_name = "exit_stop"
        return {
            "condition_id": pos["condition_id"],
            "question": pos.get("question"),
            "category": pos.get("category"),
            "event_key": pos.get("event_key"),
            "side": pos.get("side"),
            "token_id": pos.get("token_id"),
            "shares": round(shares, 2),
            "limit_price": px,
            "size_usd": round(shares * px, 4),
            "reason": reason,
            "action": "sell",
            "kind": kind,
            "best_bid": float(best_bid or 0),
            "dust": kind == "dust",
            "mark": float(pos.get("cur_price") or 0),
            "value": round(shares * float(pos.get("cur_price") or px), 4),
            "source": src_name,
            "source_detail": str(reason or "")[:160],
        }

