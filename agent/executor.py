from __future__ import annotations

import logging
from typing import Any

import requests

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


def _tick_str(tick: float) -> str:
    if tick >= 0.1:
        return "0.1"
    if tick >= 0.01:
        return "0.01"
    if tick >= 0.001:
        return "0.001"
    return "0.0001"


def _quantize(price: float, tick: float) -> float:
    tick = tick if tick > 0 else 0.01
    steps = round(float(price) / tick)
    px = steps * tick
    px = min(1.0 - tick, max(tick, px))
    decimals = len(_tick_str(tick).split(".")[-1])
    return round(px, decimals)


def _clob_meta(token_id: str) -> tuple[float, bool]:
    tick, neg = 0.01, False
    try:
        r = requests.get(
            f"{settings.clob_host}/tick-size",
            params={"token_id": token_id},
            timeout=8,
        )
        if r.ok:
            data = r.json() or {}
            tick = float(data.get("minimum_tick_size") or data.get("tick_size") or 0.01)
    except Exception:
        pass
    try:
        r = requests.get(
            f"{settings.clob_host}/neg-risk",
            params={"token_id": token_id},
            timeout=8,
        )
        if r.ok:
            data = r.json() or {}
            neg = bool(data.get("neg_risk"))
    except Exception:
        pass
    return tick, neg


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
        from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
        from py_clob_client.order_builder.constants import BUY

        token = str(ticket.token_id or "")
        if not token:
            raise RuntimeError("mangler token_id")
        tick, neg = _clob_meta(token)
        tick_s = _tick_str(tick)
        if hasattr(client, "get_tick_size"):
            try:
                raw_tick = client.get_tick_size(token)
                if raw_tick:
                    tick_s = str(raw_tick)
                    tick = float(raw_tick)
            except Exception:
                pass
        if hasattr(client, "get_neg_risk"):
            try:
                neg = bool(client.get_neg_risk(token))
            except Exception:
                pass
        fee = 0
        if hasattr(client, "get_fee_rate_bps"):
            try:
                fee = int(client.get_fee_rate_bps(token) or 0)
            except Exception:
                pass
        price = _quantize(float(ticket.limit_price), tick)
        size = round(max(5.0, float(ticket.shares)), 2)
        log.info("Post ordre token=%s px=%s sz=%s tick=%s neg=%s fee=%s", token[:18], price, size, tick, neg, fee)
        kwargs: dict[str, Any] = {
            "token_id": token,
            "price": price,
            "size": size,
            "side": BUY,
        }
        try:
            args = OrderArgs(**kwargs, fee_rate_bps=fee)
        except TypeError:
            args = OrderArgs(**kwargs)
        signed = None
        last_err: Exception | None = None
        order_type = None
        try:
            from py_clob_client.clob_types import OrderType
            order_type = OrderType.GTC
        except Exception:
            order_type = None
        for nflag in (neg, (not neg)):
            try:
                options = PartialCreateOrderOptions(tick_size=tick_s, neg_risk=nflag)
                if hasattr(client, "create_and_post_order"):
                    if order_type is not None:
                        try:
                            signed = client.create_and_post_order(args, options, order_type)
                        except TypeError:
                            signed = client.create_and_post_order(args, options)
                    else:
                        signed = client.create_and_post_order(args, options)
                else:
                    order = client.create_order(args, options)
                    signed = client.post_order(order)
                last_err = None
                log.info("LIVE ORDER ok neg=%s %s", nflag, signed)
                break
            except Exception as exc:
                last_err = exc
                log.warning("Ordre avvist neg=%s: %s", nflag, exc)
        if last_err is not None:
            self.store.mark_bad_market(ticket.condition_id, str(last_err)[:120])
            raise RuntimeError(f"Invalid/avvist ordre: {last_err}") from last_err
        if isinstance(signed, dict):
            err = str(signed.get("error") or signed.get("errorMsg") or signed.get("msg") or "")
            ok = signed.get("success", True)
            if ok is False or (err and "success" not in err.lower()):
                raise RuntimeError(f"CLOB avviste ordre: {err[:240]}")
        self.store.add_fill(
            condition_id=ticket.condition_id,
            side=ticket.side,
            price=price,
            size=size,
            cost=round(price * size, 2),
            dry_run=False,
            raw={"order": str(signed), **payload, "tick": tick},
        )
        self.store.upsert_position(
            condition_id=ticket.condition_id,
            question=ticket.question,
            category=ticket.category,
            event_key=ticket.event_key,
            side=ticket.side,
            token_id=ticket.token_id,
            shares=size,
            avg_cost=price,
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
