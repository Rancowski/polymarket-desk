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
    ) -> tuple[Ticket | None, str]:
        halt = self.halted()
        if halt:
            return None, halt

        if estimate.get("skip"):
            return None, estimate.get("skip_reason") or "brain skip"
        conf = str(estimate.get("confidence") or "medium").lower()
        p_yes = float(estimate["p_yes"])
        mid = float(book.get("mid") or market.get("mid") or 0.5)
        disagreement = abs(p_yes - mid)
        if conf == "low" and disagreement < 0.08:
            return None, "confidence=low uten stor uenighet"
        spread = float(book.get("spread") or 0)
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
        edge_net = edge_gross - (spread / 2.0) - fee_frac - settings.model_haircut
        if edge_net < settings.min_net_edge:
            return None, f"edge_net {edge_net:.3f} < {settings.min_net_edge}"
        # Favoritt-sone (Thorp/Kelly): dyr kontrakt krever mer edge
        if cost >= 0.82 and edge_net < max(settings.min_net_edge * 2, 0.06):
            return None, f"favoritt-sone kost {cost:.2f} krever mer edge"

        open_pos = self.store.positions("open")
        if any(p["condition_id"] == market["condition_id"] for p in open_pos):
            return None, "allerede i markedet"
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

        cap = settings.max_position_pct * bankroll
        remaining_event = max(0.0, cap - same_event_cost)
        remaining_cat = max(0.0, settings.max_category_pct * bankroll - cat_cost)
        sized = min(kelly_usd(p_hat, cost, bankroll), cap, remaining_event, remaining_cat)
        if sized < 5:
            return None, f"size {sized:.2f} for liten"

        shares = sized / cost if cost > 0 else 0
        if book_sz and shares > book_sz / settings.min_book_multiple:
            shares = book_sz / settings.min_book_multiple
            sized = shares * cost
            if sized < 5:
                return None, "bok for tynn etter cap"

        daily = self.store.equity_change_since(24, equity)
        weekly = self.store.equity_change_since(24 * 7, equity)
        if daily < -settings.daily_loss_halt_pct * bankroll:
            return None, "daglig tap-stopp"
        if weekly < -settings.weekly_loss_halt_pct * bankroll:
            return None, "ukentlig tap-stopp"

        # Kryss spread når kanten er reell, ellers nær mid
        if edge_net >= 0.05:
            limit = round(min(0.99, max(0.01, cost)), 2)
        else:
            limit = round(min(cost, max(0.01, mid + 0.01)), 2)
        limit = min(0.99, max(0.01, limit))

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
            thesis=estimate.get("thesis", ""),
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
    ) -> tuple[dict | None, str]:
        """Selg når edge er borte, p̂ har falt gjennom kost, eller uRealisert ≤ −25 %."""
        shares = float(pos.get("shares") or 0)
        avg = float(pos.get("avg_cost") or 0)
        if shares <= 0 or avg <= 0:
            return None, "tom posisjon"
        mid = float(book.get("mid") or avg)
        best_bid = float(book.get("best_bid") or mid)
        if best_bid <= 0:
            return None, "ingen bud"
        pnl_pct = (best_bid - avg) / avg
        if pnl_pct <= -0.25:
            return self._exit_ticket(pos, book, shares, best_bid, f"stopp-tap {pnl_pct:.1%}"), "ok"

        if not estimate or estimate.get("skip"):
            return None, "ingen fersk estimat"

        p_yes = float(estimate["p_yes"])
        side = str(pos.get("side") or "YES").upper()
        p_hat = p_yes if side == "YES" else 1.0 - p_yes
        edge = p_hat - avg
        if p_hat < avg:
            return self._exit_ticket(pos, book, shares, best_bid, f"p_hat {p_hat:.2f} < kost {avg:.2f}"), "ok"
        if edge < settings.min_net_edge / 2:
            return self._exit_ticket(pos, book, shares, best_bid, f"edge borte {edge:.3f}"), "ok"
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

