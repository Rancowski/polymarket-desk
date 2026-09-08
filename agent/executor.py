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


def _tick_literal(raw: Any) -> str:
    text = str(raw or "0.01").strip()
    for lit in ("0.0001", "0.001", "0.01", "0.1"):
        if text == lit or abs(float(text) - float(lit)) < 1e-9:
            return lit
    return "0.01"


def _place_limit(client: Any, args: Any, tick_s: str, neg: bool) -> Any:
    """Kall create_and_post_order med den signaturen denne SDK-versjonen faktisk har."""
    import inspect

    from py_clob_client.clob_types import PartialCreateOrderOptions

    options = PartialCreateOrderOptions(tick_size=tick_s, neg_risk=neg)
    fn = client.create_and_post_order
    names = list(inspect.signature(fn).parameters)
    log.info("CLOB create_and_post_order(%s) tick=%s neg=%s", names, tick_s, neg)
    if "order_type" in names:
        from py_clob_client.clob_types import OrderType

        return fn(args, options, OrderType.GTC)
    try:
        return fn(args, options)
    except TypeError:
        return fn(args)


def _amount_size(price: float, size: float) -> float:
    """CLOB krever at price*size i 1e6-enheter går opp. 10 andeler på tick-pris er trygt."""
    import math

    px = max(0.01, min(0.99, float(price)))
    p_int = int(round(px * 10000))
    if p_int <= 0:
        return max(5.0, round(size, 2))
    maker_step = 1_000_000 // math.gcd(p_int, 1_000_000)
    step = maker_step * 100 // math.gcd(maker_step, 100)
    units = (int(round(float(size) * 10000)) // step) * step or step
    out = round(units / 10000, 4)
    return max(5.0, out)


def _quantize(price: float, tick: float) -> float:
    tick = tick if tick > 0 else 0.01
    steps = round(float(price) / tick)
    px = steps * tick
    px = min(1.0 - tick, max(tick, px))
    decimals = len(_tick_literal(tick).split(".")[-1])
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
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY

        token = str(ticket.token_id or "").strip()
        if not token or not token.isdigit():
            self.store.mark_bad_market(ticket.condition_id, "bad token")
            raise RuntimeError("ugyldig token_id")

        tick_s = "0.01"
        neg = False
        min_sz = 5.0
        try:
            if hasattr(client, "get_order_book"):
                ob = client.get_order_book(token)
                data = ob if isinstance(ob, dict) else getattr(ob, "__dict__", {}) or {}
                tick_s = _tick_literal(data.get("tick_size") or getattr(ob, "tick_size", None))
                neg = bool(data.get("neg_risk") if "neg_risk" in data else getattr(ob, "neg_risk", False))
                min_sz = float(data.get("min_order_size") or getattr(ob, "min_order_size", None) or 5)
        except Exception as exc:
            log.warning("get_order_book: %s", exc)
        try:
            if hasattr(client, "get_tick_size"):
                tick_s = _tick_literal(client.get_tick_size(token) or tick_s)
        except Exception:
            pass
        try:
            if hasattr(client, "get_neg_risk"):
                neg = bool(client.get_neg_risk(token))
        except Exception:
            pass

        tick_f = float(tick_s)
        price = _quantize(float(ticket.limit_price), tick_f)
        size = _amount_size(price, max(min_sz, float(ticket.shares), 10.0))
        log.info("CLOB buy px=%s sz=%s tick=%s neg=%s token=%s…", price, size, tick_s, neg, token[:14])
        args = OrderArgs(token_id=token, price=price, size=size, side=BUY)
        last_err: Exception | None = None
        signed = None
        for nflag in (neg, (not neg)):
            try:
                signed = _place_limit(client, args, tick_s, nflag)
                last_err = None
                log.info("LIVE ORDER ok neg=%s %s", nflag, signed)
                break
            except TypeError as exc:
                last_err = exc
                log.warning("SDK-signatur: %s", exc)
            except Exception as exc:
                last_err = exc
                log.warning("Ordre avvist neg=%s tick=%s: %s", nflag, tick_s, exc)
                if "invalid order" in str(exc).lower() or "invalid" in str(exc).lower():
                    continue
                break
        if last_err is not None:
            msg = str(last_err)
            if "unexpected keyword" not in msg and "order_type" not in msg and "TypeError" not in type(last_err).__name__:
                self.store.mark_bad_market(ticket.condition_id, msg[:120])
            raise RuntimeError(f"CLOB-ordre feilet: {msg}") from last_err
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
            raw={"order": str(signed), **payload, "tick": tick_s, "neg_risk": neg},
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
