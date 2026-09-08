from __future__ import annotations

import logging
from typing import Any

from agent.config import settings
from agent.risk import Ticket
from agent.store import Store

log = logging.getLogger("exec")


def _as_float(value: Any) -> float:
    if value is None or value is False:
        return 0.0
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _parse_balance(raw: Any) -> float:
    """CLOB returnerer pUSD i wei (1e6) eller allerede i dollar."""
    if raw is None:
        return 0.0
    data: Any = raw
    if not isinstance(data, dict):
        if hasattr(raw, "balance"):
            data = {"balance": getattr(raw, "balance")}
        else:
            data = getattr(raw, "__dict__", None) or {}
    if not isinstance(data, dict):
        return 0.0
    candidate: Any = (
        data.get("balance")
        or data.get("collateral")
        or data.get("available")
    )
    if candidate in (None, "", 0, "0") and isinstance(data.get("balances"), dict):
        candidate = data["balances"].get("COLLATERAL")
    wei = _as_float(candidate)
    if wei <= 0:
        return 0.0
    # 221.52 pUSD kommer som 221520000.
    return wei / 1e6 if wei >= 1000 else wei


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
        parsed = 0.0
        try:
            client = self._live_client()
            if hasattr(client, "get_balance_allowance"):
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

                try:
                    params = BalanceAllowanceParams(
                        asset_type=AssetType.COLLATERAL,
                        signature_type=settings.signature_type,
                    )
                except TypeError:
                    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                bal = client.get_balance_allowance(params)
                log.info("Balanse raw=%s", bal)
                parsed = _parse_balance(bal)
        except Exception as exc:
            log.warning("Live balanse feilet: %s", exc)
        if parsed > 0:
            log.info("Live bankroll=%.2f pUSD", parsed)
            return parsed
        log.warning(
            "CLOB sa 0 pUSD (du har sannsynligvis feil POLYMARKET_FUNDER — "
            "bruk innskuddsadressen under Cash/Deposit, ikke Profile «API use only»). "
            "Bruker PAPER_BANKROLL_USD=%.2f",
            settings.paper_bankroll_usd,
        )
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
        try:
            from py_clob_client.clob_types import PartialCreateOrderOptions

            options = PartialCreateOrderOptions(tick_size="0.01")
            if hasattr(client, "create_and_post_order"):
                signed = client.create_and_post_order(args, options)
            else:
                order = client.create_order(args, options)
                signed = client.post_order(order)
        except (TypeError, Exception):
            if hasattr(client, "create_and_post_order"):
                signed = client.create_and_post_order(args)
            elif hasattr(client, "create_order"):
                order = client.create_order(args)
                signed = client.post_order(order) if hasattr(client, "post_order") else order
            else:
                raise RuntimeError("SDK mangler create/post order")
        log.info("LIVE ORDER %s", signed)
        err = ""
        if isinstance(signed, dict):
            err = str(signed.get("error") or signed.get("errorMsg") or signed.get("msg") or "")
        if err and "success" not in err.lower():
            raise RuntimeError(f"CLOB avviste ordre: {err[:240]}")
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

    def sell(self, order: dict) -> dict:
        payload = {**order}
        if settings.dry_run:
            log.info(
                "PAPER SELL %s %s @ %s size=%s (%s)",
                order.get("side"),
                str(order.get("question") or "")[:60],
                order.get("limit_price"),
                order.get("shares"),
                order.get("reason"),
            )
            self.store.add_fill(
                condition_id=order.get("condition_id"),
                side=f"SELL_{order.get('side')}",
                price=order.get("limit_price"),
                size=order.get("shares"),
                cost=order.get("size_usd"),
                dry_run=True,
                raw=payload,
            )
            self.store.close_position(order["condition_id"], order.get("side"))
            return {"status": "paper_sell", "ticket": payload}

        client = self._live_client()
        from py_clob_client.clob_types import OrderArgs

        try:
            from py_clob_client.order_builder.constants import SELL
            side = SELL
        except Exception:
            side = "SELL"
        args = OrderArgs(
            token_id=order["token_id"],
            price=float(order["limit_price"]),
            size=float(order["shares"]),
            side=side,
        )
        signed = None
        if hasattr(client, "create_and_post_order"):
            signed = client.create_and_post_order(args)
        elif hasattr(client, "create_order"):
            placed = client.create_order(args)
            signed = client.post_order(placed) if hasattr(client, "post_order") else placed
        else:
            raise RuntimeError("SDK mangler create/post order")
        log.info("LIVE SELL %s", signed)
        self.store.add_fill(
            condition_id=order.get("condition_id"),
            side=f"SELL_{order.get('side')}",
            price=order.get("limit_price"),
            size=order.get("shares"),
            cost=order.get("size_usd"),
            dry_run=False,
            raw={"order": str(signed), **payload},
        )
        self.store.close_position(order["condition_id"], order.get("side"))
        return {"status": "live_sell", "response": signed, "ticket": payload}
