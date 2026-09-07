from __future__ import annotations

import logging
from typing import Any

from agent.config import settings
from agent.risk import Ticket
from agent.store import Store

log = logging.getLogger("exec")


class Executor:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._client = None

    def _live_client(self):
        if self._client is not None:
            return self._client
        if not settings.private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY mangler for live")
        from py_clob_client.client import ClobClient

        kwargs: dict[str, Any] = {
            "host": settings.clob_host,
            "key": settings.private_key,
            "chain_id": settings.chain_id,
        }
        try:
            self._client = ClobClient(
                settings.clob_host,
                key=settings.private_key,
                chain_id=settings.chain_id,
                signature_type=settings.signature_type,
                funder=settings.funder or None,
            )
        except TypeError:
            self._client = ClobClient(**kwargs)

        if settings.poly_api_key and settings.poly_api_secret:
            try:
                from py_clob_client.clob_types import ApiCreds

                creds = ApiCreds(
                    api_key=settings.poly_api_key,
                    api_secret=settings.poly_api_secret,
                    api_passphrase=settings.poly_api_passphrase,
                )
                if hasattr(self._client, "set_api_creds"):
                    self._client.set_api_creds(creds)
            except Exception as exc:
                log.warning("Kunne ikke sette API-creds direkte: %s", exc)
        else:
            derive = getattr(self._client, "create_or_derive_api_creds", None) or getattr(
                self._client, "create_or_derive_api_key", None
            )
            if derive:
                creds = derive()
                if hasattr(self._client, "set_api_creds"):
                    self._client.set_api_creds(creds)
                log.info("API-creds derivert. Lagre POLY_API_KEY/SECRET/PASSPHRASE i .env")
        return self._client

    def bankroll(self) -> float:
        if settings.dry_run or not settings.private_key:
            return settings.paper_bankroll_usd
        try:
            client = self._live_client()
            if hasattr(client, "get_balance_allowance"):
                try:
                    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

                    bal = client.get_balance_allowance(
                        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                    )
                    if isinstance(bal, dict):
                        raw = bal.get("balance") or bal.get("collateral") or 0
                        return float(raw) / (1e6 if float(raw) > 10000 else 1)
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Live balanse feilet, bruker paper-bankroll: %s", exc)
        return settings.paper_bankroll_usd

    def submit(self, ticket: Ticket) -> dict:
        payload = {
            "condition_id": ticket.condition_id,
            "question": ticket.question,
            "side": ticket.side,
            "token_id": ticket.token_id,
            "price": ticket.limit_price,
            "shares": ticket.shares,
            "usd": ticket.size_usd,
            "edge_net": ticket.edge_net,
            "thesis": ticket.thesis,
        }
        if settings.dry_run:
            log.info(
                "PAPER BUY %s %s @ %s size=%s usd=%.2f edge=%.3f",
                ticket.side,
                ticket.question[:60],
                ticket.limit_price,
                ticket.shares,
                ticket.size_usd,
                ticket.edge_net,
            )
            self.store.add_fill(
                condition_id=ticket.condition_id,
                side=ticket.side,
                price=ticket.limit_price,
                size=ticket.shares,
                cost=ticket.size_usd,
                dry_run=True,
                raw=payload,
            )
            self.store.upsert_position(
                condition_id=ticket.condition_id,
                question=ticket.question,
                category=ticket.category,
                event_key=ticket.event_key,
                side=ticket.side,
                token_id=ticket.token_id,
                shares=ticket.shares,
                avg_cost=ticket.limit_price,
                status="open",
            )
            return {"status": "paper", "ticket": payload}

        client = self._live_client()
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY

        args = OrderArgs(
            token_id=ticket.token_id,
            price=float(ticket.limit_price),
            size=float(ticket.shares),
            side=BUY,
        )
        signed = None
        if hasattr(client, "create_and_post_order"):
            signed = client.create_and_post_order(args)
        elif hasattr(client, "create_order"):
            order = client.create_order(args)
            signed = client.post_order(order) if hasattr(client, "post_order") else order
        else:
            raise RuntimeError("SDK mangler create/post order")

        log.info("LIVE ORDER %s", signed)
        self.store.add_fill(
            condition_id=ticket.condition_id,
            side=ticket.side,
            price=ticket.limit_price,
            size=ticket.shares,
            cost=ticket.size_usd,
            dry_run=False,
            raw={"order": str(signed), **payload},
        )
        self.store.upsert_position(
            condition_id=ticket.condition_id,
            question=ticket.question,
            category=ticket.category,
            event_key=ticket.event_key,
            side=ticket.side,
            token_id=ticket.token_id,
            shares=ticket.shares,
            avg_cost=ticket.limit_price,
            status="open",
        )
        return {"status": "live", "response": signed, "ticket": payload}
