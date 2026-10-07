"""Complement pair + partition set tickets. pair_ok fixtures stay rejected."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent.arb import EDGE_TARGET, Arb
from agent.config import settings as cfg
from agent.executor import Executor
from agent.kalshi import pair_ok, pair_ok_selfcheck
from agent.risk import Ticket
from agent.store import Store

EQ = 131.0
CASH = 109.0
SET_CAP = EDGE_TARGET * EQ  # 20.96


class FakeStore:
    def __init__(self, deposited: float = 1000.0, open_pos: list | None = None) -> None:
        self._deposited = deposited
        self._open = list(open_pos or [])

    def deposited_usd(self, default: float = 0.0) -> float:
        return self._deposited

    def positions(self, status: str = "open") -> list:
        _ = status
        return list(self._open)


class FakeScout:
    def book(self, token_id: str, fallback_mid=None, require_two_sided: bool = True) -> dict:
        _ = (token_id, fallback_mid, require_two_sided)
        return {}


def _book(ask: float, spread: float = 0.02, ask_size: float = 5000.0) -> dict:
    bid = max(0.01, round(ask - spread, 4))
    return {
        "best_ask": ask,
        "best_bid": bid,
        "spread": spread,
        "ask_size": ask_size,
        "bid_size": ask_size,
        "mid": round((ask + bid) / 2, 4),
        "synthetic": False,
    }


def _binary(cid: str, yask: float, nask: float, nspread: float = 0.02) -> dict:
    return {
        "condition_id": cid,
        "question": "Will the bill pass the Senate 2026?",
        "category": "politics",
        "event_key": cid,
        "yes_token": f"yes-{cid}",
        "no_token": f"no-{cid}",
        "book": _book(yask),
        "no_book": _book(nask, spread=nspread),
        "yes_mid": yask,
        "mid": yask,
        "hours_left": 720,
    }


def _outcome(cid: str, event: str, ask: float, siblings: list, event_n: int) -> dict:
    return {
        "condition_id": cid,
        "question": f"Who wins the race: {cid}?",
        "category": "politics",
        "event_key": event,
        "yes_token": f"yes-{cid}",
        "no_token": f"no-{cid}",
        "book": _book(ask),
        "yes_mid": ask,
        "mid": ask,
        "hours_left": 720,
        "event_n": event_n,
        "siblings": siblings,
    }


def _maker_market() -> dict:
    book = _book(0.54, spread=0.08)
    book["mid"] = 0.50
    return {
        "condition_id": "maker-1",
        "question": "Will it rain in London tomorrow?",
        "category": "weather",
        "event_key": "maker-1",
        "yes_token": "yes-maker-1",
        "no_token": "no-maker-1",
        "book": book,
        "no_book": dict(book),
        "yes_mid": 0.50,
        "mid": 0.50,
        "hours_left": 24,
        "volume_24h": 10_000,
    }


def _arb() -> Arb:
    return Arb(FakeScout(), FakeStore())


def _set_cost(tickets: list) -> float:
    return round(sum(float(t.size_usd) for t in tickets), 2)


def test_complement_yes_no_pair_same_shares():
    arb = _arb()
    tickets = arb._complements(
        [_binary("c1", 0.40, 0.50)],
        bankroll=CASH,
        open_ids=set(),
        equity=EQ,
    )
    sides = sorted(t.side for t in tickets)
    assert len(tickets) == 2, tickets
    assert sides == ["NO", "YES"]
    assert tickets[0].shares == tickets[1].shares
    assert tickets[0].shares > 0
    assert all(t.source == "complement" for t in tickets)
    cost = _set_cost(tickets)
    assert cost <= SET_CAP + 1e-6
    assert cost <= CASH
    print("FIXTURE complement 0.40+0.50 ->", len(tickets), "tickets shares", tickets[0].shares, "cost", cost)


def test_complement_no_wide_spread_zero():
    arb = _arb()
    tickets = arb._complements(
        [_binary("c2", 0.40, 0.50, nspread=0.08)],
        bankroll=CASH,
        open_ids=set(),
        equity=EQ,
    )
    assert tickets == []
    print("FIXTURE complement NO spread 0.08 ->", len(tickets), "tickets")


def test_partition_missing_sibling_zero():
    arb = _arb()
    event = "race-2026"
    sibs = [
        {"q": "Who wins the race: b?", "condition_id": "b"},
        {"q": "Who wins the race: c?", "condition_id": "c"},
    ]
    markets = [
        _outcome("a", event, 0.30, sibs, 3),
        _outcome("b", event, 0.33, sibs, 3),
    ]
    tickets = arb._partition(markets, CASH, set(), 0, False, [], equity=EQ)
    assert tickets == []
    print("FIXTURE 3-way missing sibling ->", len(tickets), "tickets")


def test_partition_three_way_ask_sum():
    arb = _arb()
    event = "race-2026"
    markets = [
        _outcome("a", event, 0.30, [{"q": "Who wins the race: b?"}, {"q": "Who wins the race: c?"}], 3),
        _outcome("b", event, 0.33, [{"q": "Who wins the race: a?"}, {"q": "Who wins the race: c?"}], 3),
        _outcome("c", event, 0.33, [{"q": "Who wins the race: a?"}, {"q": "Who wins the race: b?"}], 3),
    ]
    tickets = arb._partition(markets, CASH, set(), 0, False, [], equity=EQ)
    assert len(tickets) == 3, tickets
    shares = {t.shares for t in tickets}
    assert len(shares) == 1
    assert all(t.source == "partition" for t in tickets)
    cost = _set_cost(tickets)
    assert cost <= SET_CAP + 1e-6
    assert cost <= CASH
    print("FIXTURE 3-way ask sum 0.96 ->", len(tickets), "tickets shares", tickets[0].shares, "cost", cost)


def test_pair_ok_fixtures_reject_motpart_year_pin():
    pair_ok_selfcheck()
    motpart, w1 = pair_ok(
        "Democratic Party control the House after the 2026 Midterms",
        "CONTROLH-2026-R",
        "Republicans control House",
        0.15,
        0.88,
    )
    year, w2 = pair_ok(
        "Trump out as President by September 30",
        "KXPRESPARTY-2032-R",
        "Republican presidential party 2032",
        0.40,
        0.42,
    )
    pin, w3 = pair_ok(
        "Bitcoin above $76000 on Sep 9",
        "KXBTC-26SEP0907-T87299",
        "Bitcoin T87299",
        0.01,
        1.00,
    )
    assert motpart is False, w1
    assert year is False, w2
    assert pin is False, w3
    print("FIXTURE pair_ok motpart/year/pin reject", w1, w2, w3)


def test_complement_sized_off_equity_and_cash():
    arb = _arb()
    tickets = arb._complements(
        [_binary("c1", 0.40, 0.50)],
        bankroll=CASH,
        open_ids=set(),
        equity=EQ,
    )
    assert len(tickets) == 2
    assert tickets[0].shares == tickets[1].shares
    cost = _set_cost(tickets)
    assert cost <= SET_CAP + 1e-6
    assert cost <= CASH
    assert cost < 50
    assert abs(cost - 160) > 1
    print("SIZE 0.40+0.50 equity=131 cash=109 cost", cost, "shares", tickets[0].shares)


def test_complement_cash_15_caps_or_zero():
    arb = _arb()
    tickets = arb._complements(
        [_binary("c1", 0.40, 0.50)],
        bankroll=15.0,
        open_ids=set(),
        equity=EQ,
    )
    if tickets:
        assert len(tickets) == 2
        assert tickets[0].shares == tickets[1].shares
        cost = _set_cost(tickets)
        assert cost <= 15.0 + 1e-6
        print("SIZE cash=15 cost", cost, "shares", tickets[0].shares)
    else:
        print("SIZE cash=15 -> 0 tickets")


def test_maker_shaped_scan_zero_tickets():
    arb = _arb()
    calls = []

    def _spy(*a, **k):
        calls.append(True)
        return []

    arb._makers = _spy
    with patch("agent.risk.Risk.buys_blocked", return_value=None):
        tickets = arb.scan([_maker_market()], CASH, equity=EQ)
    assert tickets == []
    assert calls == []
    assert _arb()._makers([_maker_market()], CASH, set()) == []
    assert _arb()._locked([_maker_market()], CASH, set()) == []
    print("FIXTURE maker-shaped scan -> 0 tickets, _makers not called")


def _buy_ticket(source: str) -> Ticket:
    return Ticket(
        condition_id="blk-1",
        question="Blocked source fixture",
        category="politics",
        event_key="blk-1",
        side="YES",
        token_id="123",
        mid=0.40,
        best_bid=0.39,
        best_ask=0.40,
        spread=0.01,
        p_hat=0.99,
        edge_gross=0.60,
        edge_net=0.05,
        confidence="high",
        thesis="fixture",
        limit_price=0.40,
        size_usd=10.0,
        shares=25.0,
        source=source,
    )


def test_submit_maker_and_tape_blocked(tmp_path):
    db = tmp_path / "desk.db"
    store = Store(db)
    ex = Executor(store)
    no_halt = replace(cfg, halt_file=Path("C:/no-such-halt-polymarket-desk"))
    try:
        with patch("agent.executor.settings", no_halt):
            for src in ("maker", "tape"):
                result = ex.submit(_buy_ticket(src))
                assert result.get("status") == "blocked", result
                assert "source" in str(result.get("reason") or "").lower() or src in str(result)
        fills = store.recent_fills(40)
        assert fills == [], fills
        assert store.positions("open") == []
        print("FIXTURE submit maker/tape blocked, fills", len(fills))
    finally:
        store.conn.close()
