"""Complement pair + partition set tickets. pair_ok fixtures stay rejected."""
from __future__ import annotations

from agent.arb import Arb
from agent.kalshi import pair_ok, pair_ok_selfcheck


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


def _arb() -> Arb:
    return Arb(FakeScout(), FakeStore())


def test_complement_yes_no_pair_same_shares():
    arb = _arb()
    tickets = arb._complements([_binary("c1", 0.40, 0.50)], bankroll=1000, open_ids=set())
    sides = sorted(t.side for t in tickets)
    assert len(tickets) == 2, tickets
    assert sides == ["NO", "YES"]
    assert tickets[0].shares == tickets[1].shares
    assert tickets[0].shares > 0
    assert all(t.source == "complement" for t in tickets)
    print("FIXTURE complement 0.40+0.50 ->", len(tickets), "tickets shares", tickets[0].shares)


def test_complement_no_wide_spread_zero():
    arb = _arb()
    tickets = arb._complements(
        [_binary("c2", 0.40, 0.50, nspread=0.08)],
        bankroll=1000,
        open_ids=set(),
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
    tickets = arb._partition(markets, 1000, set(), 0, False, [])
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
    tickets = arb._partition(markets, 1000, set(), 0, False, [])
    assert len(tickets) == 3, tickets
    shares = {t.shares for t in tickets}
    assert len(shares) == 1
    assert all(t.source == "partition" for t in tickets)
    print("FIXTURE 3-way ask sum 0.96 ->", len(tickets), "tickets shares", tickets[0].shares)


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
