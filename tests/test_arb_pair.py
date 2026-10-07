"""Complement pair + partition set tickets. pair_ok fixtures stay rejected."""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent.arb import EDGE_TARGET, Arb, kalshi_should_buy, ticket_log_payload
from agent.config import settings as cfg
from agent.executor import Executor
from agent.kalshi import pair_ok, pair_ok_selfcheck
from agent.loop import Desk
from agent.risk import Risk, Ticket
from agent.store import Store

EQ = 131.0
CASH = 109.0
SET_CAP = EDGE_TARGET * EQ  # 20.96


class FakeStore:
    def __init__(
        self,
        deposited: float = 1000.0,
        open_pos: list | None = None,
        meta: dict | None = None,
    ) -> None:
        self._deposited = deposited
        self._open = list(open_pos or [])
        self._meta = dict(meta or {})

    def deposited_usd(self, default: float = 0.0) -> float:
        return self._deposited

    def positions(self, status: str = "open") -> list:
        _ = status
        return list(self._open)

    def get_meta(self, key: str, default: str = "") -> str:
        val = self._meta.get(key)
        if val is None:
            return default
        return str(val)

    def set_meta(self, key: str, value: str) -> None:
        self._meta[key] = value

    def float_meta(self, key: str) -> float | None:
        val = self._meta.get(key)
        if val is None or val == "":
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    def equity_hours_ago(self, hours: float) -> float | None:
        _ = hours
        return self.float_meta("week_anchor_equity")


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


def _kalshi_mkt(pm: float, k_yes: float, spread: float = 0.02, ask: float | None = None) -> dict:
    cost = float(ask if ask is not None else pm)
    book = _book(cost, spread=spread)
    return {
        "condition_id": "fed-h25",
        "question": "Fed hike 25 bps after the September 2026 meeting",
        "category": "economics",
        "event_key": "fed-sep-2026",
        "yes_token": "yes-fed",
        "no_token": "no-fed",
        "book": book,
        "no_book": _book(max(0.02, round(1.0 - cost, 4)), spread=spread),
        "yes_mid": pm,
        "mid": pm,
        "hours_left": 720,
        "kalshi": {
            "ticker": "KXFEDDECISION-26SEP-H25",
            "title": "Fed decision 25bp",
            "yes": k_yes,
            "pm_yes": pm,
        },
    }


def test_kalshi_gap_6c_zero_tickets():
    assert kalshi_should_buy(True, 0.06, 0.40, 0.02) is False
    arb = _arb()
    tickets = arb._kalshi_gap([_kalshi_mkt(0.40, 0.46, spread=0.02, ask=0.40)], CASH, set())
    assert tickets == []
    print("FIXTURE kalshi gap 0.06 spread 0.02 ->", len(tickets), "tickets")


def test_kalshi_gap_12c_ticket_real_edge():
    arb = _arb()
    tickets = arb._kalshi_gap([_kalshi_mkt(0.40, 0.52, spread=0.02, ask=0.40)], CASH, set())
    assert len(tickets) == 1, tickets
    t = tickets[0]
    assert t.source == "kalshi"
    assert abs(t.edge_net - 0.10) < 0.015
    assert abs(t.p_hat - 0.52) < 1e-6
    assert t.p_hat != 0.99
    assert t.confidence == "mechanical"
    pay = ticket_log_payload(t)
    assert "gap" in pay or "gap_c" in pay
    assert pay.get("edge_net") != 0.05
    print("FIXTURE kalshi gap 0.12 -> edge_net", t.edge_net, "p_hat", t.p_hat)


def test_complement_ask_sum_096_real_edge():
    arb = _arb()
    tickets = arb._complements(
        [_binary("c96", 0.46, 0.50)],
        bankroll=CASH,
        open_ids=set(),
        equity=EQ,
    )
    assert len(tickets) == 2, tickets
    for t in tickets:
        assert t.edge_locked is not None
        assert abs(float(t.edge_locked) - 0.04) < 0.011
        assert abs(t.edge_net - 0.03) < 0.011
        assert abs(t.p_hat - 0.04) > 0.01
        assert t.p_hat != 0.99
        assert t.confidence == "mechanical"
        pay = ticket_log_payload(t)
        assert "edge_locked" in pay
        assert abs(pay["ask_sum"] - 0.96) < 0.011
        assert pay.get("p_hat") in (None, 0, 0.0) or "p_hat" not in pay
        assert pay.get("edge_net") != 0.05
    print(
        "FIXTURE complement ask_sum 0.96 edge_locked",
        tickets[0].edge_locked,
        "edge_net",
        tickets[0].edge_net,
    )


def test_evaluate_exit_ignores_p_hat():
    store = FakeStore(deposited=131.0)
    risk = Risk(store)
    pos = {
        "condition_id": "hold-1",
        "question": "Will the bill pass the Senate 2026?",
        "category": "politics",
        "event_key": "hold-1",
        "side": "YES",
        "shares": 20.0,
        "avg_cost": 0.40,
        "cur_price": 0.40,
        "p_hat": 0.99,
        "edge_locked": 0.04,
        "token_id": "tok-hold",
    }
    book = _book(0.42, spread=0.02)
    book["best_bid"] = 0.40
    book["mid"] = 0.41
    ticket, why = risk.evaluate_exit(
        pos,
        book,
        {"p_hat": 0.99, "p_yes": 0.99, "edge_locked": 0.04, "confidence": "high"},
        131.0,
    )
    assert ticket is None, (ticket, why)
    assert "hold" in str(why).lower()
    print("FIXTURE evaluate_exit edge_locked=0.04 bid unchanged ->", why)


def _seed_open(store: Store, cid: str, *, dry_run: bool, cost: float = 10.0, shares: float = 25.0) -> None:
    px = cost / shares
    store.add_fill(
        condition_id=cid,
        side="BUY_YES",
        price=px,
        size=shares,
        cost=cost,
        dry_run=dry_run,
        question="Paper ghost fixture",
        token_id="tok-" + cid,
        source="complement",
        source_detail="complement YES+NO 0.90",
        raw={"status": "matched", "takingAmount": str(cost)},
    )
    store.upsert_position(
        condition_id=cid,
        question="Paper ghost fixture",
        category="politics",
        event_key=cid,
        side="YES",
        token_id="tok-" + cid,
        shares=shares,
        avg_cost=px,
        cur_price=px,
        status="open",
        entry_source="complement",
    )


def test_paper_absent_one_cycle_stays_open(tmp_path):
    store = Store(tmp_path / "ghost1.db")
    try:
        _seed_open(store, "pg1", dry_run=True, cost=10.0, shares=25.0)
        before = store.attribution_stats()["realized"]
        store.sync_open_positions([])
        open_ids = {p["condition_id"] for p in store.positions("open")}
        assert "pg1" in open_ids
        assert store.attribution_stats()["realized"] == before
        fills = store.recent_fills(20)
        assert not any(str(f.get("source") or "") in {"resolve", "paper_ghost"} for f in fills)
        assert not store.has_close_fill("pg1", "YES")
        print("FIXTURE paper miss 1 -> still open realized", before)
    finally:
        store.conn.close()


def test_paper_absent_two_cycles_paper_ghost_flat(tmp_path):
    store = Store(tmp_path / "ghost2.db")
    try:
        _seed_open(store, "pg2", dry_run=True, cost=10.0, shares=25.0)
        before = store.attribution_stats()["realized"]
        store.sync_open_positions([])
        store.sync_open_positions([])
        open_ids = {p["condition_id"] for p in store.positions("open")}
        assert "pg2" not in open_ids
        assert store.attribution_stats()["realized"] == before
        fills = store.recent_fills(20)
        ghosts = [f for f in fills if str(f.get("source") or "") == "paper_ghost"]
        assert len(ghosts) == 1, fills
        assert abs(float(ghosts[0].get("cost") or 0) - 10.0) < 1e-6
        assert not any(str(f.get("source") or "") == "resolve" for f in fills)
        print("FIXTURE paper miss 2 -> paper_ghost proceeds", ghosts[0].get("cost"), "realized", before)
    finally:
        store.conn.close()


def _paper_settings():
    return replace(
        cfg,
        dry_run=True,
        xai_api_key="xai-test-key-not-real",
        halt_file=Path("C:/no-such-halt-polymarket-desk"),
    )


def _hold_pos() -> dict:
    return {
        "condition_id": "hold-halt",
        "question": "Will the bill pass the Senate 2026?",
        "category": "politics",
        "event_key": "hold-halt",
        "side": "YES",
        "shares": 20.0,
        "avg_cost": 0.40,
        "cur_price": 0.40,
        "token_id": "tok-hold-halt",
    }


def test_daily_halt_7pct_blocks_scan_exit_callable():
    store = FakeStore(deposited=100.0, meta={"day_anchor_equity": 100.0})
    paper = _paper_settings()
    with (
        patch("agent.risk.settings", paper),
        patch("agent.risk.live_forbidden", return_value=None),
    ):
        arb = Arb(FakeScout(), store)
        tickets = arb.scan([_binary("c1", 0.40, 0.50)], CASH, equity=93.0)
        assert tickets == []
        risk = Risk(store)
        ticket, why = risk.evaluate_exit(_hold_pos(), _book(0.42, spread=0.02), None, 93.0)
    assert ticket is None or isinstance(ticket, dict)
    assert why
    print("FIXTURE daily halt 7pct scan", len(tickets), "exit", why)


def test_daily_halt_1pct_complement_not_blocked():
    store = FakeStore(deposited=100.0, meta={"day_anchor_equity": 100.0})
    paper = _paper_settings()
    with (
        patch("agent.risk.settings", paper),
        patch("agent.risk.live_forbidden", return_value=None),
    ):
        arb = Arb(FakeScout(), store)
        tickets = arb.scan([_binary("c1", 0.40, 0.50)], CASH, equity=99.0)
    assert len(tickets) == 2, tickets
    assert all(t.source == "complement" for t in tickets)
    print("FIXTURE daily halt 1pct complement", len(tickets), "tickets")


def test_paper_cycle_does_not_call_xai(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key-not-real")
    caplog.set_level(logging.INFO)
    db = tmp_path / "cycle.db"
    store = Store(db)
    paper = _paper_settings()
    markets = [_binary("c1", 0.40, 0.50)]
    with (
        patch("agent.loop.settings", paper),
        patch("agent.risk.settings", paper),
        patch("agent.executor.settings", paper),
        patch("agent.brain.settings", paper),
        patch("agent.loop.live_forbidden", return_value=None),
        patch("agent.risk.live_forbidden", return_value=None),
        patch("agent.loop.Store", return_value=store),
        patch("agent.loop.threading.Thread"),
        patch("agent.kalshi.fetch_open", return_value=[]),
        patch("agent.kalshi.compare", return_value=(0, [])),
    ):
        desk = Desk()
        desk.store = store
        desk.risk = Risk(store)
        desk.arb = Arb(desk.scout, store)
        desk.exec = Executor(store)
        desk.scout.fetch = lambda limit=150: markets
        desk.exec.cancel_open = lambda: None
        desk.exec.fetch_pm_snapshot = lambda force=False: {
            "available": CASH,
            "positions": [],
            "mtm": 0.0,
            "portfolio": EQ,
        }
        desk._cycle_i = 5
        with (
            patch.object(desk.brain, "estimate", wraps=None) as est,
            patch.object(desk.brain, "_call") as call,
            patch("agent.brain.requests.post") as post,
        ):
            est.side_effect = AssertionError("xAI estimate must not be called in paper")
            call.side_effect = AssertionError("xAI _call must not be called in paper")
            post.side_effect = AssertionError("xAI requests.post must not be called in paper")
            result = desk.cycle()
    assert result.get("ok") is True, result
    assert est.call_count == 0
    assert call.call_count == 0
    assert post.call_count == 0
    text = caplog.text
    assert "grok=off" in text
    assert "run_grok=True" not in text
    print("FIXTURE paper cycle xAI not called grok=off")
    store.conn.close()


def test_live_absent_one_cycle_no_resolve(tmp_path):
    store = Store(tmp_path / "ghost_live.db")
    try:
        _seed_open(store, "lv1", dry_run=False, cost=10.0, shares=25.0)
        store.sync_open_positions([])
        open_ids = {p["condition_id"] for p in store.positions("open")}
        assert "lv1" in open_ids
        fills = store.recent_fills(20)
        assert not any(str(f.get("source") or "") in {"resolve", "paper_ghost"} for f in fills)
        assert not store.has_close_fill("lv1", "YES")
        print("FIXTURE live miss 1 -> still open, no resolve")
    finally:
        store.conn.close()
