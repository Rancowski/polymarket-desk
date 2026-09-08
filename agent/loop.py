from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timezone

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
        if not markets:
            log.info("Ingen markeder passerte filter")
            self.last_cycle = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "halted": False,
                "scanned": 0,
                "estimated": 0,
                "accepted": 0,
                "rejected": 0,
                "bankroll": bankroll,
                "equity": equity,
            }
            return {"ok": True, "scanned": 0}

        batch = markets[: settings.estimate_batch]
        try:
            estimates = self.brain.estimate(batch)
            self.last_error = None
        except Exception as exc:
            log.exception("Brain krasjet: %s", exc)
            self.last_error = str(exc)
            return {"ok": False, "reason": str(exc)}

        accepted = 0
        rejected = 0
        for m in batch:
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
                book = self.scout.book(m["yes_token"])
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
        log.info("Syklus ferdig. Nye tickets: %s", accepted)
        self.last_cycle = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "halted": False,
            "scanned": len(markets),
            "estimated": len(estimates),
            "accepted": accepted,
            "rejected": rejected,
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
