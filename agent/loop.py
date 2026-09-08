from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timezone

from agent.arb import Arb
from agent.brain import Brain
from agent.config import settings
from agent.executor import Executor
from agent.risk import Risk
from agent.scanner import Scout
from agent.store import Store

log = logging.getLogger("desk")


class Desk:
    """To roller i én prosess: Scout+Brain vurderer, Risk+Executor handler."""

    def __init__(self) -> None:
        self.store = Store()
        self.scout = Scout()
        self.brain = Brain()
        self.arb = Arb(self.scout, self.store)
        self.risk = Risk(self.store)
        self.exec = Executor(self.store)
        self.cycle_lock = threading.Lock()
        self.busy = False
        self.last_error: str | None = None
        self.last_cycle: dict | None = None

    def cycle(self) -> dict:
        if not self.cycle_lock.acquire(blocking=False):
            return {"ok": False, "reason": "syklus kjører allerede"}
        self.busy = True
        try:
            return self._cycle()
        except Exception as exc:
            self.last_error = str(exc)
            log.exception("Syklus krasjet")
            return {"ok": False, "reason": str(exc)}
        finally:
            self.busy = False
            self.cycle_lock.release()

    def _cycle(self) -> dict:
        halt = self.risk.halted()
        if halt:
            log.warning("Stoppet: %s", halt)
            self.last_cycle = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "halted": True,
                "scanned": 0,
                "estimated": 0,
                "accepted": 0,
                "rejected": 0,
                "reason": halt,
            }
            return {"ok": True, "halted": True}

        bankroll = self.exec.bankroll()
        open_pos = self.store.positions("open")
        locked = sum(float(p["shares"]) * float(p["avg_cost"]) for p in open_pos)
        equity = bankroll + locked
        self.store.mark_equity(bankroll, equity)
        log.info(
            "Syklus start dry_run=%s bankroll=%.2f equity=%.2f open=%s",
            settings.dry_run,
            bankroll,
            equity,
            len(open_pos),
        )

        markets = self.scout.fetch()
        arb_tickets = self.arb.scan(markets, bankroll)
        arb_n = 0
        for ticket in arb_tickets:
            try:
                result = self.exec.submit(ticket)
                arb_n += 1
                self.store.log_decision(
                    condition_id=ticket.condition_id,
                    question=ticket.question,
                    side=ticket.side,
                    mid=ticket.mid,
                    p_hat=ticket.p_hat,
                    edge_net=ticket.edge_net,
                    action=result.get("status"),
                    reason=ticket.thesis,
                    payload=result,
                )
                bankroll = max(0.0, bankroll - ticket.size_usd)
            except Exception as exc:
                log.exception("Arb-ordre feilet")
                self.store.log_decision(
                    condition_id=ticket.condition_id,
                    question=ticket.question,
                    action="error",
                    reason=str(exc),
                )
        if arb_n:
            open_pos = self.store.positions("open")
            locked = sum(float(p["shares"]) * float(p["avg_cost"]) for p in open_pos)
            equity = bankroll + locked

        by_id = {m["condition_id"]: m for m in markets}
        for pos in open_pos:
            cid = pos.get("condition_id")
            if cid and cid not in by_id:
                by_id[cid] = {
                    "condition_id": cid,
                    "question": pos.get("question") or "",
                    "description": "",
                    "end_date": None,
                    "category": pos.get("category") or "other",
                    "event_key": pos.get("event_key") or cid,
                    "yes_token": pos.get("token_id") if pos.get("side") == "YES" else "",
                    "no_token": pos.get("token_id") if pos.get("side") == "NO" else "",
                    "yes_mid": float(pos.get("avg_cost") or 0.5),
                    "mid": float(pos.get("avg_cost") or 0.5),
                    "liquidity": 0,
                    "_open_only": True,
                }
        ranked = [m for m in markets if not m.get("_open_only")]
        extras = [m for m in by_id.values() if m.get("_open_only")]
        batch = extras + ranked
        batch = batch[: max(settings.estimate_batch, len(extras))]
        if not batch:
            log.info("Ingen markeder passerte filter")
            self.last_cycle = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "halted": False,
                "scanned": 0,
                "estimated": 0,
                "accepted": 0,
                "rejected": 0,
                "exits": 0,
                "arb": arb_n,
                "bankroll": bankroll,
                "equity": equity,
            }
            return {"ok": True, "scanned": 0}

        for m in batch:
            try:
                if m.get("yes_token"):
                    m["book"] = self.scout.book(m["yes_token"])
                if m.get("no_token"):
                    try:
                        m["no_book"] = self.scout.book(m["no_token"])
                    except Exception:
                        m["no_book"] = {}
            except Exception as exc:
                log.warning("Bok-feil %s: %s", m.get("question", "")[:40], exc)
                m["book"] = {}

        self.scout.enrich(batch)

        try:
            estimates = self.brain.estimate(batch)
            self.last_error = None
        except Exception as exc:
            log.exception("Brain krasjet: %s", exc)
            self.last_error = str(exc)
            return {"ok": False, "reason": str(exc)}

        exits = 0
        for pos in list(open_pos):
            token = pos.get("token_id")
            if not token:
                continue
            try:
                pbook = self.scout.book(token)
            except Exception as exc:
                self.store.log_decision(
                    condition_id=pos.get("condition_id"),
                    question=pos.get("question"),
                    action="skip",
                    reason=f"exit-bok: {exc}",
                )
                continue
            ticket_ex, why = self.risk.evaluate_exit(
                pos, pbook, estimates.get(pos.get("condition_id")), bankroll
            )
            if not ticket_ex:
                continue
            try:
                result = self.exec.sell(ticket_ex)
                exits += 1
                self.store.log_decision(
                    condition_id=pos.get("condition_id"),
                    question=pos.get("question"),
                    side=pos.get("side"),
                    action=result.get("status"),
                    reason=ticket_ex.get("reason") or why,
                    payload=result,
                )
            except Exception as exc:
                log.exception("Salg feilet")
                self.store.log_decision(
                    condition_id=pos.get("condition_id"),
                    question=pos.get("question"),
                    action="error",
                    reason=str(exc),
                )

        open_pos = self.store.positions("open")
        locked = sum(float(p["shares"]) * float(p["avg_cost"]) for p in open_pos)

        accepted = 0
        rejected = 0
        for m in batch:
            if m.get("_open_only"):
                continue
            est = estimates.get(m["condition_id"])
            if not est:
                self.store.log_decision(
                    condition_id=m["condition_id"],
                    question=m["question"],
                    action="skip",
                    reason="ingen estimat",
                )
                rejected += 1
                continue
            try:
                book = m.get("book") or self.scout.book(m["yes_token"])
            except Exception as exc:
                self.store.log_decision(
                    condition_id=m["condition_id"],
                    question=m["question"],
                    action="skip",
                    reason=f"bok-feil: {exc}",
                )
                rejected += 1
                continue
            ticket, reason = self.risk.evaluate(m, book, est, bankroll, equity)
            if not ticket:
                rejected += 1
                self.store.log_decision(
                    condition_id=m["condition_id"],
                    question=m["question"],
                    mid=book.get("mid"),
                    p_hat=est.get("p_yes"),
                    action="reject",
                    reason=reason,
                    payload=est,
                )
                continue
            try:
                result = self.exec.submit(ticket)
                accepted += 1
                self.store.log_decision(
                    condition_id=ticket.condition_id,
                    question=ticket.question,
                    side=ticket.side,
                    mid=ticket.mid,
                    p_hat=ticket.p_hat,
                    edge_net=ticket.edge_net,
                    action=result.get("status"),
                    reason=ticket.thesis,
                    payload=result,
                )
                bankroll = max(0.0, bankroll - ticket.size_usd)
                equity = bankroll + locked + ticket.size_usd
                locked += ticket.size_usd
            except Exception as exc:
                log.exception("Ordre feilet")
                self.last_error = str(exc)
                self.store.log_decision(
                    condition_id=ticket.condition_id,
                    question=ticket.question,
                    action="error",
                    reason=str(exc),
                )
        log.info("Syklus ferdig. Grok-tickets: %s arb: %s exits: %s", accepted, arb_n, exits)
        self.last_cycle = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "halted": False,
            "scanned": len(markets),
            "estimated": len(estimates),
            "accepted": accepted,
            "arb": arb_n,
            "rejected": rejected,
            "exits": exits,
            "bankroll": bankroll,
            "equity": equity,
        }
        return {"ok": True, **self.last_cycle}

    def run_forever(self) -> None:
        log.info("Desk kjører. DRY_RUN=%s interval=%ss", settings.dry_run, settings.loop_seconds)
        while True:
            try:
                self.cycle()
            except Exception:
                log.error("Syklus krasjet:\n%s", traceback.format_exc())
            time.sleep(settings.loop_seconds)
