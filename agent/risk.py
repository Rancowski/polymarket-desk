from __future__ import annotations

import logging
from dataclasses import dataclass

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
        if market.get("category") == "sports" and (mid >= 0.88 or mid <= 0.12):
            return None, "sports nær avgjort"
        disagreement = abs(p_yes - mid)
        if conf == "low" and disagreement < (0.04 if probe else 0.05):
            return None, "confidence=low uten stor uenighet"
        spread = float(book.get("spread") or 0)
        if book.get("synthetic"):
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
        extra = 0.015 if market.get("category") == "sports" else 0.0
        edge_gross = p_hat - cost
        edge_net = edge_gross - fee_frac - settings.model_haircut - extra
        need = settings.min_net_edge if min_edge is None else min_edge
        if market.get("category") == "sports":
            need = max(need, 0.022)
        if edge_net < (0.0 if probe else need):
            return None, f"edge_net {edge_net:.3f} < {need}"
        if cost <= 0.15 or cost >= 0.85:
            return None, "nær resolusjon"
        if market.get("category") == "sports" and (cost <= 0.22 or cost >= 0.78):
            return None, "sports ekstrem-pris"

        open_pos = self.store.positions("open")
        if any(p["condition_id"] == market["condition_id"] for p in open_pos):
            return None, "allerede i markedet"
        if market.get("category") == "sports" and any((p.get("category") or "") == "sports" for p in open_pos):
            return None, "maks 1 sports-posisjon"
        if len(open_pos) >= settings.max_open_positions:
            return None, "max open positions"

        event = market.get("event_key") or market["condition_id"]
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

        cap = (0.06 if probe else settings.max_position_pct) * bankroll
        remaining_event = max(0.0, cap - same_event_cost)
        remaining_cat = max(0.0, settings.max_category_pct * bankroll - cat_cost)
        hard = min(cap, remaining_event, remaining_cat)
        sized = clip_usd(p_hat, cost, bankroll, hard)
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
    ) -> tuple[dict | None, str]:
        shares = float(pos.get("shares") or 0)
        avg = float(pos.get("avg_cost") or 0)
        if shares <= 0 or avg <= 0:
            return None, "tom posisjon"
        cid = pos.get("condition_id")
        side = str(pos.get("side") or "YES").upper()
        counterparts = [
            p for p in self.store.positions("open")
            if p.get("condition_id") == cid and str(p.get("side") or "").upper() != side
        ]
        if counterparts:
            return None, "complement-hold"
        mid = float(book.get("mid") or avg)
        best_bid = float(book.get("best_bid") or mid)
        if best_bid <= 0:
            return None, "ingen bud"
        pnl_pct = (best_bid - avg) / avg
        sports = (pos.get("category") or "") == "sports"

        if sports and pnl_pct <= -0.15:
            return self._exit_ticket(pos, book, shares, best_bid, f"sports stopp-tap {pnl_pct:.1%}"), "ok"
        if not sports and pnl_pct <= -0.25:
            return self._exit_ticket(pos, book, shares, best_bid, f"stopp-tap {pnl_pct:.1%}"), "ok"
        if sports and (best_bid >= 0.88 or pnl_pct >= 0.22):
            return self._exit_ticket(pos, book, shares, best_bid, f"sports ta gevinst {pnl_pct:.1%}"), "ok"
        if not sports and best_bid >= 0.93:
            return self._exit_ticket(pos, book, shares, best_bid, "nær resolusjon — ta gevinst"), "ok"

        ks = kalshi or {}
        k_yes = float(ks.get("yes") or 0)
        if 0.02 < k_yes < 0.98:
            k_hat = k_yes if side == "YES" else 1.0 - k_yes
            if k_hat + 0.06 < avg:
                return self._exit_ticket(pos, book, shares, best_bid, f"Kalshi mot oss {k_hat:.2f} < kost {avg:.2f}"), "ok"

        if not estimate or estimate.get("skip"):
            return None, "hold uten fersk estimat"

        p_yes = float(estimate["p_yes"])
        p_hat = p_yes if side == "YES" else 1.0 - p_yes
        if p_hat < avg - 0.03:
            return self._exit_ticket(pos, book, shares, best_bid, f"p_hat {p_hat:.2f} < kost {avg:.2f}"), "ok"
        return None, "hold"

    def _exit_ticket(self, pos: dict, book: dict, shares: float, price: float, reason: str) -> dict:
        px = round(min(0.99, max(0.01, price)), 2)
        return {
            "condition_id": pos["condition_id"],
            "question": pos.get("question"),
            "category": pos.get("category"),
            "event_key": pos.get("event_key"),
            "side": pos.get("side"),
            "token_id": pos.get("token_id"),
            "shares": round(shares, 2),
            "limit_price": px,
            "size_usd": round(shares * px, 2),
            "reason": reason,
            "action": "sell",
        }

