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
        self.last_error = None
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
        try:
            from agent.kalshi import attach as kalshi_attach
            kalshi_n = kalshi_attach(markets)
        except Exception as exc:
            log.warning("Kalshi: %s", exc)
            kalshi_n = 0
        arb_tickets = self.arb.scan(markets, bankroll)
        arb_n = 0
        failed_events: set[str] = set()
        pending_hedge = None
        for ticket in arb_tickets:
            if ticket.event_key in failed_events:
                continue
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
                if "sum-til-én" in (ticket.thesis or "") or "event-sett" in (ticket.thesis or ""):
                    pending_hedge = ticket if pending_hedge is None else None
                else:
                    pending_hedge = None
            except Exception as exc:
                log.exception("Arb-ordre feilet")
                failed_events.add(ticket.event_key)
                if pending_hedge and pending_hedge.event_key == ticket.event_key:
                    try:
                        self.exec.sell(
                            {
                                "condition_id": pending_hedge.condition_id,
                                "question": pending_hedge.question,
                                "side": pending_hedge.side,
                                "token_id": pending_hedge.token_id,
                                "shares": pending_hedge.shares,
                                "limit_price": max(0.01, pending_hedge.best_bid or pending_hedge.limit_price),
                                "size_usd": pending_hedge.size_usd,
                                "reason": "hedge-rollback",
                            }
                        )
                    except Exception:
                        log.exception("Hedge-rollback feilet")
                    pending_hedge = None
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
                mid = float(m.get("yes_mid") or m.get("mid") or 0)
                if m.get("yes_token"):
                    m["book"] = self.scout.book(m["yes_token"], fallback_mid=mid)
                if m.get("no_token"):
                    no_mid = float(m.get("no_mid") or (1 - mid if mid else 0))
                    m["no_book"] = self.scout.book(m["no_token"], fallback_mid=no_mid)
            except Exception as exc:
                log.warning("Bok-feil %s: %s", m.get("question", "")[:40], exc)
                m["book"] = self.scout._synthetic(float(m.get("yes_mid") or 0))

        self.scout.enrich(batch)

        try:
            estimates = self.brain.estimate(batch)
            usage = getattr(self.brain, "last_usage", {}) or {}
            if usage.get("usd"):
                self.store.add_api_cost(float(usage["usd"]), str(usage.get("model") or ""), int(usage.get("tokens") or 0))
            n_blend = 0
            for m in batch:
                ks = m.get("kalshi") or {}
                k_yes = float(ks.get("yes") or 0)
                if not (0.02 < k_yes < 0.98):
                    continue
                cid = m["condition_id"]
                est = estimates.get(cid) or {}
                p = est.get("p_yes")
                blended = round(0.55 * k_yes + 0.45 * float(p), 4) if p is not None else k_yes
                estimates[cid] = {
                    **est,
                    "p_yes": blended,
                    "skip": False,
                    "confidence": est.get("confidence") or "medium",
                    "thesis": ((est.get("thesis") or "") + f" | Kalshi {k_yes} gap {ks.get('gap')}").strip(" |"),
                }
                n_blend += 1
            if n_blend:
                log.info("Kalshi blend på %s markeder", n_blend)
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
                book = m.get("book") or self.scout.book(
                    m["yes_token"], fallback_mid=float(m.get("yes_mid") or m.get("mid") or 0)
                )
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

        if accepted == 0 and arb_n == 0 and self.store.live_fill_count() == 0:
            test = self._pipeline_ticket(markets, bankroll)
            if test:
                try:
                    result = self.exec.submit(test)
                    accepted += 1
                    self.last_error = None
                    self.store.log_decision(
                        condition_id=test.condition_id,
                        question=test.question,
                        side=test.side,
                        mid=test.mid,
                        p_hat=test.p_hat,
                        edge_net=test.edge_net,
                        action=result.get("status"),
                        reason=test.thesis,
                        payload=result,
                    )
                    log.info("Pipeline-test %s usd=%.2f", test.question[:50], test.size_usd)
                except Exception as exc:
                    log.exception("Pipeline-test feilet")
                    self.last_error = str(exc)
                    self.store.log_decision(
                        condition_id=test.condition_id,
                        question=test.question,
                        action="error",
                        reason=str(exc),
                    )

        log.info("Syklus ferdig. Grok-tickets: %s arb: %s kalshi: %s exits: %s", accepted, arb_n, kalshi_n, exits)
        self.last_cycle = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "halted": False,
            "scanned": len(markets),
            "estimated": len(estimates),
            "accepted": accepted,
            "arb": arb_n,
            "kalshi": kalshi_n,
            "rejected": rejected,
            "exits": exits,
            "bankroll": bankroll,
            "equity": equity,
        }
        return {"ok": True, **self.last_cycle}

    def _pipeline_ticket(self, markets: list, bankroll: float):
        from agent.risk import Ticket

        best = None
        best_liq = -1.0
        for m in markets:
            cid = m.get("condition_id") or ""
            if self.store.is_bad_market(cid):
                continue
            book = m.get("book") or {}
            ask = float(book.get("best_ask") or 0)
            spread = float(book.get("spread") or 1)
            token = str(m.get("yes_token") or "")
            if not token.isdigit():
                continue
            if not (0.25 <= ask <= 0.75) or spread > 0.05:
                continue
            liq = float(m.get("liquidity") or 0)
            if liq <= best_liq:
                continue
            shares = 10.0
            best_liq = liq
            best = Ticket(
                condition_id=cid,
                question=m.get("question") or "",
                category=m.get("category") or "other",
                event_key=m.get("event_key") or cid,
                side="YES",
                token_id=token,
                mid=float(book.get("mid") or ask),
                best_bid=float(book.get("best_bid") or ask),
                best_ask=ask,
                spread=spread,
                p_hat=ask,
                edge_gross=0.0,
                edge_net=0.0,
                confidence="low",
                thesis="pipeline-test første live-fill",
                limit_price=round(ask, 2),
                size_usd=round(shares * ask, 2),
                shares=shares,
            )
        return best

    def run_forever(self) -> None:
        log.info("Desk kjører. DRY_RUN=%s interval=%ss", settings.dry_run, settings.loop_seconds)
        while True:
            try:
                self.cycle()
            except Exception:
                log.error("Syklus krasjet:\n%s", traceback.format_exc())
            time.sleep(settings.loop_seconds)
