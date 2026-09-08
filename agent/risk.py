from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

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


def clip_usd(p_hat: float, cost: float, bankroll: float, cap: float) -> float:
    """Små kontoer: Kelly på 2 ¢ kant er <$5 og ble avvist. Ta et fillbart klipp i stedet."""
    kelly = kelly_usd(p_hat, cost, bankroll)
    if kelly <= 0:
        return 0.0
    min_clip = min(cap, max(8.0, 0.04 * bankroll))
    return min(cap, max(kelly, min_clip))


class Risk:
    def __init__(self, store: Store) -> None:
        self.store = store

    def halted(self) -> str | None:
        if settings.halt_file.exists():
            return f"HALT-fil finnes: {settings.halt_file}"
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
        halt = self.halted()
        if halt:
            return None, halt
        cid = market.get("condition_id") or ""
        if self.store.is_bad_market(cid):
            return None, "CLOB-blacklist"
        if estimate.get("skip"):
            return None, estimate.get("skip_reason") or "brain skip"
        conf = str(estimate.get("confidence") or "medium").lower()
        p_yes = float(estimate["p_yes"])
        mid = float(book.get("mid") or market.get("mid") or 0.5)
        if mid >= 0.94 or mid <= 0.06:
            return None, "nær resolusjon"
        if is_sports(market) and (mid >= 0.88 or mid <= 0.12):
            return None, "sports nær avgjort"
        disagreement = abs(p_yes - mid)
        if conf == "low" and disagreement < (0.04 if probe else 0.05):
            return None, "confidence=low uten stor uenighet"
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
        if edge_net < (0.0 if probe else need):
            return None, f"edge_net {edge_net:.3f} < {need}"
        if cost <= 0.15 or cost >= 0.85:
            return None, "nær resolusjon"
        if is_sports(market) and (cost <= 0.22 or cost >= 0.78):
            return None, "sports ekstrem-pris"

        open_pos = self.store.positions("open")
        if any(p["condition_id"] == market["condition_id"] for p in open_pos):
            return None, "allerede i markedet"
        if is_sports(market) and any(is_sports(p) for p in open_pos):
            return None, "maks 1 sports-posisjon"
        if len(open_pos) >= settings.max_open_positions:
            return None, "max 3 open positions"

        event = market.get("event_key") or market["condition_id"]
        same_event_cost = sum(
            float(p["shares"]) * float(p["avg_cost"])
            for p in open_pos
            if p.get("event_key") == event
        )
        if same_event_cost > 0.5:
            return None, "ett event ett ticket"
        cat_cost = sum(
            float(p["shares"]) * float(p["avg_cost"])
            for p in open_pos
            if p.get("category") == market["category"]
        )

        deposited = self.store.deposited_usd(0.0)
        size_base = bankroll
        if deposited >= 1:
            size_base = min(bankroll, deposited)
        sports = is_sports(market)
        ks = market.get("kalshi") or {}
        if sports:
            floor_pct, cap_pct = 0.04, 0.06
        elif abs(float(ks.get("gap") or 0)) >= 0.04 or str(market.get("category") or "") in {
            "economics", "finance", "crypto", "politics", "geopolitics",
        }:
            floor_pct, cap_pct = 0.10, 0.12
        else:
            floor_pct, cap_pct = 0.06, 0.10
        cap = (0.06 if probe else cap_pct) * size_base
        remaining_event = max(0.0, cap - same_event_cost)
        remaining_cat = max(0.0, settings.max_category_pct * size_base - cat_cost)
        hard = min(cap, remaining_event, remaining_cat, bankroll)
        sized = clip_usd(p_hat, cost, size_base, hard)
        if not probe and sized > 0:
            sized = min(hard, max(floor_pct * size_base, sized))
        if probe:
            sized = min(max(sized, 8.0), 12.0, hard)
        if sized < 5 or hard < 5:
            return None, f"size {sized:.2f} for liten (bankroll {bankroll:.0f})"

        shares = sized / cost if cost > 0 else 0
        if shares < 5:
            shares = 5.0
            sized = shares * cost
            if sized > hard:
                return None, "min 5 andeler over cap"
        if book_sz and shares > book_sz / settings.min_book_multiple:
            shares = book_sz / settings.min_book_multiple
            sized = shares * cost
            if sized < 5 or shares < 5:
                return None, "bok for tynn etter cap"

        daily = self.store.equity_change_since(24, equity)
        weekly = self.store.equity_change_since(24 * 7, equity)
        if daily < -settings.daily_loss_halt_pct * bankroll:
            return None, "daglig tap-stopp"
        if weekly < -settings.weekly_loss_halt_pct * bankroll:
            return None, "ukentlig tap-stopp"

        # Kryss ask så ordren fylles (GTC mid+1¢ blir ofte liggende)
        limit = round(min(0.99, max(0.01, cost)), 2)
        if probe:
            limit = round(min(0.99, cost + 0.01), 2)

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
        bid = _live_bid(book, pos)
        missing = book_bid <= 0
        if missing and bid <= 0.03:
            return self._exit_ticket(pos, book, shares, 0.001, "død bok (mangler bud) → tick 0.001"), "ok"
        if bid <= 0.03:
            return self._exit_ticket(pos, book, shares, 0.001, f"død bud {bid:.4f} → tick 0.001"), "ok"
        if force_reason:
            return self._exit_ticket(pos, book, shares, max(bid, 0.001), force_reason), "ok"
        counterparts = [
            p for p in self.store.positions("open")
            if p.get("condition_id") == cid and str(p.get("side") or "").upper() != side
        ]
        if counterparts:
            return None, "complement-hold"
        pnl_pct = (bid - avg) / avg if avg else 0.0
        mid = float((book or {}).get("mid") or bid)

        hwm_key = f"hwm:{cid}:{side}"
        try:
            hwm = float(self.store.get_meta(hwm_key) or 0)
        except (TypeError, ValueError):
            hwm = 0.0
        if bid > hwm:
            hwm = bid
            self.store.set_meta(hwm_key, f"{hwm:.4f}")
        if sports and (hwm >= avg * 1.20 or pnl_pct >= 0.20) and hwm > 0 and bid <= hwm * 0.92:
            return self._exit_ticket(
                pos, book, shares, bid, f"sports trail −8% fra topp {hwm:.3f} → {bid:.3f}"
            ), "ok"

        if sports and (bid <= avg * 0.88 or pnl_pct <= -0.12):
            return self._exit_ticket(pos, book, shares, bid, f"sports stopp-tap {pnl_pct:.1%} bid {bid:.3f}"), "ok"
        if not sports and pnl_pct <= -0.25:
            return self._exit_ticket(pos, book, shares, bid, f"stopp-tap {pnl_pct:.1%} bid {bid:.3f}"), "ok"
        if sports and bid >= 0.88:
            return self._exit_ticket(pos, book, shares, bid, f"sports ta gevinst bid {bid:.3f}"), "ok"
        if not sports and bid >= 0.93:
            return self._exit_ticket(pos, book, shares, bid, f"ta gevinst bid {bid:.3f}"), "ok"

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
        if sports and mid < 0.15 and (hours_open >= 3 or (hours_left is not None and float(hours_left) < -3)):
            return self._exit_ticket(
                pos, book, shares, bid, f"kamp >3t og mid {mid:.3f}<0.15"
            ), "ok"

        ks = kalshi or {}
        k_yes = float(ks.get("yes") or 0)
        if 0.02 < k_yes < 0.98:
            k_hat = k_yes if side == "YES" else 1.0 - k_yes
            if k_hat + 0.06 < avg:
                return self._exit_ticket(pos, book, shares, bid, f"Kalshi mot oss {k_hat:.2f} < kost {avg:.2f}"), "ok"

        if estimate and not estimate.get("skip"):
            try:
                p_yes = float(estimate["p_yes"])
            except (TypeError, ValueError, KeyError):
                p_yes = None
            if p_yes is not None:
                p_hat = p_yes if side == "YES" else 1.0 - p_yes
                faded_to_mid = abs(p_hat - mid) < 0.02 and abs(mid - avg) < 0.03
                if p_hat + 0.03 <= avg and not faded_to_mid:
                    return self._exit_ticket(
                        pos, book, shares, bid, f"p_hat {p_hat:.2f} ≥3c under kost {avg:.2f}"
                    ), "ok"
        return None, f"hold bid {bid:.3f} pnl {pnl_pct:.1%}"

    def _exit_ticket(self, pos: dict, book: dict, shares: float, price: float, reason: str) -> dict:
        px = float(price or 0)
        tick = 0.001 if px < 0.10 else 0.01
        if px < tick:
            px = tick
        else:
            px = int(px / tick) * tick
        px = round(max(tick, min(0.99, px)), 4)
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
        }

