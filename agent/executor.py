from __future__ import annotations

import logging
from typing import Any

import requests

from agent.config import settings
from agent.risk import Ticket, is_sports
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


def _tif(sdk: str) -> Any:
    if sdk == "v2":
        from py_clob_client_v2 import OrderType
    else:
        from py_clob_client.clob_types import OrderType
    tif = getattr(OrderType, "FAK", None) or getattr(OrderType, "FOK", None)
    if tif is None:
        log.warning("SDK mangler FAK/FOK — nekter GTC som hviler og blir logget som fill")
    return tif


def _place_limit(client: Any, args: Any, tick_s: str, neg: bool, sdk: str = "v1") -> Any:
    import inspect

    tif = _tif(sdk)
    if tif is None:
        raise RuntimeError("CLOB SDK mangler FAK — avviser ordre i stedet for GTC")
    if sdk == "v2":
        from py_clob_client_v2 import PartialCreateOrderOptions
    else:
        from py_clob_client.clob_types import PartialCreateOrderOptions

    options = PartialCreateOrderOptions(tick_size=tick_s, neg_risk=neg)
    fn = client.create_and_post_order
    names = list(inspect.signature(fn).parameters)
    log.info("CLOB create_and_post_order(%s) tick=%s neg=%s sdk=%s tif=%s", names, tick_s, neg, sdk, tif)
    attempts = (
        lambda: fn(args, options, tif),
        lambda: fn(args, options, order_type=tif),
        lambda: fn(args, order_type=tif, options=options),
        lambda: fn(order=args, options=options, order_type=tif),
    )
    last_type: Exception | None = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last_type = exc
            continue
    raise RuntimeError(f"CLOB create_and_post_order tok ikke FAK order_type: {last_type}")


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


def _as_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    if hasattr(raw, "__dict__"):
        data = {k: v for k, v in vars(raw).items() if not k.startswith("_")}
        if data:
            return data
    return {}


def _order_filled(signed: Any) -> tuple[bool, dict]:
    """Kun matched/takingAmount > 0 er fill. status=live med tom takingAmount er ikke en posisjon."""
    data = _as_dict(signed)
    status = str(data.get("status") or "").lower()
    taking = str(data.get("takingAmount") if data.get("takingAmount") is not None else "").strip()
    making = str(data.get("makingAmount") if data.get("makingAmount") is not None else "").strip()
    if status in {"live", "open", "resting", "unmatched", "cancelled", "canceled"}:
        if taking in {"", "0", "0.0"} and making in {"", "0", "0.0"}:
            return False, data
    if status in {"matched", "filled"}:
        return True, data
    if taking not in {"", "0", "0.0"} or making not in {"", "0", "0.0"}:
        return True, data
    return False, data


def _infer_category(p: dict) -> str:
    row = {
        "question": p.get("title") or p.get("question") or "",
        "event_key": p.get("eventSlug") or p.get("event_key") or "",
        "category": p.get("category") or "",
        "eventSlug": p.get("eventSlug") or "",
        "slug": p.get("slug") or "",
    }
    if is_sports(row):
        return "sports"
    blob = " ".join(str(row.get(k) or "") for k in ("event_key", "question", "slug")).lower()
    for key in ("crypto", "politics", "finance", "economics", "geopolitics", "tech"):
        if key in blob:
            return key
    return str(p.get("eventSlug") or "other")[:40]


def _side_and_label(p: dict) -> tuple[str, str, str]:
    outcome = str(p.get("outcome") or "").strip()
    title = str(p.get("title") or "")
    idx = p.get("outcomeIndex")
    ou = outcome.upper()
    if ou in {"YES", "Y", "1"}:
        return "YES", title[:160], "YES"
    if ou in {"NO", "N", "0"} or ou.startswith("NO "):
        return "NO", title[:160], "NO"
    side = "YES"
    if idx is not None:
        try:
            side = "NO" if int(idx) == 1 else "YES"
        except (TypeError, ValueError):
            side = "YES"
    elif " vs " in title.lower() and outcome:
        right = title.lower().split(" vs ", 1)[-1]
        if right.startswith(outcome.lower()[:4]):
            side = "NO"
    label = f"{outcome} · {title[:140]}" if outcome else title[:160]
    return side, label, outcome or side


def _attach_builder_code(args: Any) -> Any:
    if hasattr(args, "builder_code") and getattr(args, "builder_code", None) is None:
        try:
            args.builder_code = ""
        except Exception:
            pass
    if not hasattr(args, "builder_code"):
        try:
            args.builder_code = ""
        except Exception:
            pass
    return args


class Executor:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._client = None
        self._sdk = "v1"

    def _attach_creds(self, client: Any, v2: bool) -> None:
        if settings.poly_api_key and settings.poly_api_secret:
            try:
                if v2:
                    from py_clob_client_v2 import ApiCreds
                else:
                    from py_clob_client.clob_types import ApiCreds
                creds = ApiCreds(
                    api_key=settings.poly_api_key,
                    api_secret=settings.poly_api_secret,
                    api_passphrase=settings.poly_api_passphrase,
                )
                if hasattr(client, "set_api_creds"):
                    client.set_api_creds(creds)
            except Exception as exc:
                log.warning("API-creds: %s", exc)
            return
        derive = getattr(client, "create_or_derive_api_creds", None) or getattr(
            client, "create_or_derive_api_key", None
        )
        if derive:
            creds = derive()
            if hasattr(client, "set_api_creds"):
                client.set_api_creds(creds)
            log.info("API-creds derivert")

    def _live_client(self):
        if self._client is not None:
            return self._client
        if not settings.private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY mangler for live")
        funder = settings.funder or None
        wanted = int(settings.signature_type or 3)
        # v1 avviser signatureType=3 lokalt med "Invalid order inputs".
        # Nye Polymarket-kontoer (email/deposit) MÅ bruke v2 + POLY_1271.
        try:
            from py_clob_client_v2 import ClobClient as C2

            st: Any = wanted
            try:
                from py_clob_client_v2 import SignatureTypeV2

                st = {
                    0: SignatureTypeV2.EOA,
                    1: SignatureTypeV2.POLY_PROXY,
                    2: SignatureTypeV2.POLY_GNOSIS_SAFE,
                    3: SignatureTypeV2.POLY_1271,
                }.get(wanted, SignatureTypeV2.POLY_1271)
            except Exception:
                pass
            client = C2(
                host=settings.clob_host,
                chain_id=settings.chain_id,
                key=settings.private_key,
                signature_type=st,
                funder=funder,
            )
            self._attach_creds(client, v2=True)
            self._client = client
            self._sdk = "v2"
            log.info("CLOB v2 klar signature_type=%s funder=%s", wanted, (funder or "")[:12])
            return client
        except Exception as exc:
            log.warning("CLOB v2 feilet (%s) — v1 uten type 3", exc)

        from py_clob_client.client import ClobClient as C1

        # v1 order-builder godtar bare 0/1/2. 3 → Invalid order inputs.
        for st in (1, 2, 0):
            try:
                client = C1(
                    settings.clob_host,
                    key=settings.private_key,
                    chain_id=settings.chain_id,
                    signature_type=st,
                    funder=funder,
                )
                self._attach_creds(client, v2=False)
                self._client = client
                self._sdk = "v1"
                log.info("CLOB v1 klar signature_type=%s", st)
                return client
            except Exception as exc:
                log.warning("CLOB v1 sig %s: %s", st, exc)
        raise RuntimeError("Kunne ikke lage CLOB-klient")

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
        snap = self.store.float_meta("last_cash")
        if snap is not None and snap > 0:
            log.warning("CLOB sa 0 pUSD — bruker siste snapshot cash=%.2f", snap)
            return snap
        log.warning(
            "CLOB sa 0 pUSD (du har sannsynligvis feil POLYMARKET_FUNDER — "
            "bruk innskuddsadressen under Cash/Deposit, ikke Profile «API use only»). "
            "Bruker PAPER_BANKROLL_USD=%.2f",
            settings.paper_bankroll_usd,
        )
        return settings.paper_bankroll_usd if settings.dry_run else 0.0

    def cancel_open(self) -> int:
        """Fjern hvilende GTC som aldri fyltes (forrige «live» uten fill)."""
        if settings.dry_run or not settings.private_key:
            return 0
        n = 0
        try:
            client = self._live_client()
            for name in ("cancel_all", "cancel_all_orders"):
                fn = getattr(client, name, None)
                if callable(fn):
                    fn()
                    log.info("CLOB %s kjørt", name)
                    return 1
            get = getattr(client, "get_orders", None) or getattr(client, "get_open_orders", None)
            cancel = getattr(client, "cancel", None) or getattr(client, "cancel_order", None)
            if get and cancel:
                orders = get() or []
                if isinstance(orders, dict):
                    orders = orders.get("orders") or orders.get("data") or []
                for o in orders:
                    oid = None
                    if isinstance(o, dict):
                        oid = o.get("id") or o.get("orderID") or o.get("order_id")
                    if oid:
                        try:
                            cancel(oid)
                            n += 1
                        except Exception:
                            pass
                log.info("Kansellerte %s åpne CLOB-ordre", n)
        except Exception as exc:
            log.warning("cancel_open: %s", exc)
        return n

    def fetch_live_positions(self) -> list[dict] | None:
        """Sannhet fra Polymarket. None = henting feilet, ikke tøm lokalt."""
        funder = (settings.funder or "").strip()
        if not funder:
            return None
        headers = {"User-Agent": "polymarket-desk/1.0"}
        urls = [
            f"https://data-api.polymarket.com/positions?user={funder}&sizeThreshold=0.01",
            f"https://gamma-api.polymarket.com/positions?user={funder}",
        ]
        for url in urls:
            try:
                r = requests.get(url, headers=headers, timeout=12)
                if r.status_code != 200:
                    continue
                data = r.json()
                rows = data if isinstance(data, list) else (data.get("positions") or data.get("data") or [])
                out = []
                for p in rows:
                    size = float(p.get("size") or p.get("shares") or 0)
                    if size < 0.01:
                        continue
                    cid = str(p.get("conditionId") or p.get("condition_id") or "").strip()
                    if not cid:
                        continue
                    side, label, outcome = _side_and_label(p)
                    avg = float(p.get("avgPrice") or p.get("avg_price") or 0)
                    cur = float(p.get("curPrice") or p.get("currPrice") or 0)
                    api_val = p.get("currentValue")
                    mtm = float(api_val) if api_val not in (None, "") else (size * cur if cur else 0)
                    out.append(
                        {
                            "condition_id": cid,
                            "question": label,
                            "outcome": outcome,
                            "category": _infer_category(p),
                            "event_key": str(p.get("eventSlug") or p.get("conditionId") or cid),
                            "side": side,
                            "token_id": str(p.get("asset") or p.get("token_id") or ""),
                            "shares": size,
                            "avg_cost": avg,
                            "cur_price": cur if cur else None,
                            "current_value": mtm if mtm else None,
                            "status": "open",
                        }
                    )
                log.info("Live posisjoner fra API: %s", len(out))
                return out
            except Exception as exc:
                log.warning("positions %s: %s", url.split("/")[2], exc)
        return None

    def fetch_position_value(self) -> float | None:
        funder = (settings.funder or "").strip()
        if not funder:
            return None
        try:
            r = requests.get(
                f"https://data-api.polymarket.com/value?user={funder}",
                headers={"User-Agent": "polymarket-desk/1.0"},
                timeout=8,
            )
            if not r.ok:
                return None
            data = r.json()
            if isinstance(data, list) and data:
                return float(data[0].get("value") or 0)
            if isinstance(data, dict) and "value" in data:
                return float(data.get("value") or 0)
        except Exception as exc:
            log.warning("portfolio value: %s", exc)
        return None

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

        if getattr(ticket, "synthetic", False):
            raise RuntimeError("live-kjøp avvist: syntetisk bok")
        if is_sports({"question": ticket.question, "category": ticket.category, "event_key": ticket.event_key}):
            if not (0.22 < float(ticket.limit_price) < 0.78):
                raise RuntimeError("sports ekstrem-pris")
            if any(is_sports(p) for p in self.store.positions("open")):
                raise RuntimeError("maks 1 sports-posisjon")
        client = self._live_client()
        sdk = getattr(self, "_sdk", "v1")
        if sdk == "v2":
            from py_clob_client_v2 import OrderArgs, Side

            side = Side.BUY
        else:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY

            side = BUY

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
        price = _quantize(min(0.99, price + tick_f), tick_f)
        size = _amount_size(price, max(min_sz, float(ticket.shares), 10.0))
        log.info("CLOB buy px=%s sz=%s tick=%s neg=%s token=%s…", price, size, tick_s, neg, token[:14])
        try:
            args = OrderArgs(token_id=token, price=price, size=size, side=side, builder_code="")
        except TypeError:
            args = OrderArgs(token_id=token, price=price, size=size, side=side)
        _attach_builder_code(args)
        last_err: Exception | None = None
        signed = None
        for nflag in (neg, (not neg)):
            try:
                signed = _place_limit(client, args, tick_s, nflag, sdk=sdk)
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
            if "restricted in your region" in msg.lower() or "geoblock" in msg.lower():
                raise RuntimeError(
                    "Geoblokk: Hetzner-IP er i Tyskland. Polymarket avviser ordre derfra. "
                    "Flytt VPS til Helsinki (Finland). Norge og Finland er tillatt."
                ) from last_err
            if "unexpected keyword" not in msg and "order_type" not in msg and "TypeError" not in type(last_err).__name__:
                self.store.mark_bad_market(ticket.condition_id, msg[:120])
            raise RuntimeError(f"CLOB-ordre feilet: {msg}") from last_err
        data = _as_dict(signed)
        err = str(data.get("error") or data.get("errorMsg") or data.get("msg") or "")
        ok = data.get("success", True)
        if ok is False or (err and "success" not in err.lower()):
            raise RuntimeError(f"CLOB avviste ordre: {err[:240]}")
        filled, data = _order_filled(signed if data else signed)
        if not filled:
            log.info("Ordre umatchet (ikke fill) %s", data.get("orderID") or signed)
            return {"status": "resting", "response": data or signed, "ticket": payload}
        taking = _as_float(data.get("takingAmount"))
        fill_size = taking if taking > 0 else size
        fill_px = price
        self.store.add_fill(
            condition_id=ticket.condition_id,
            side=ticket.side,
            price=fill_px,
            size=fill_size,
            cost=round(fill_px * fill_size, 2),
            dry_run=False,
            raw={"order": data or str(signed), "status": data.get("status"), "takingAmount": data.get("takingAmount"), **payload, "tick": tick_s, "neg_risk": neg},
        )
        self.store.upsert_position(
            condition_id=ticket.condition_id,
            question=ticket.question,
            category=ticket.category,
            event_key=ticket.event_key,
            side=ticket.side,
            token_id=ticket.token_id,
            shares=fill_size,
            avg_cost=fill_px,
            current_value=round(fill_px * fill_size, 4),
            status="open",
        )
        return {"status": "live", "response": data or signed, "ticket": payload}

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
        sdk = getattr(self, "_sdk", "v1")
        token = str(order.get("token_id") or "").strip()
        price = float(order["limit_price"])
        size = float(order["shares"])
        if sdk == "v2":
            from py_clob_client_v2 import OrderArgs, Side

            side = Side.SELL
        else:
            from py_clob_client.clob_types import OrderArgs

            try:
                from py_clob_client.order_builder.constants import SELL

                side = SELL
            except Exception:
                side = "SELL"
        tick_s, neg = _clob_meta(token)
        tick_f = float(tick_s)
        if price < 0.10 and tick_f > 0.001:
            tick_s, tick_f = "0.001", 0.001
        px0 = _quantize(min(price, 0.99), tick_f)
        last_signed: Any = None
        data: dict = {}
        for drop in (0, 1, 2):
            attempt = max(tick_f, round(px0 - drop * tick_f, 6))
            attempt = _quantize(attempt, tick_f)
            try:
                args = OrderArgs(token_id=token, price=attempt, size=size, side=side, builder_code="")
            except TypeError:
                args = OrderArgs(token_id=token, price=attempt, size=size, side=side)
            _attach_builder_code(args)
            try:
                signed = _place_limit(client, args, tick_s, neg, sdk=sdk)
            except Exception as exc:
                log.warning("SELL %s @ %s: %s", (order.get("question") or "")[:40], attempt, exc)
                last_signed = {"error": str(exc)}
                continue
            log.info("LIVE SELL try @ %s %s", attempt, signed)
            filled, data = _order_filled(signed)
            last_signed = signed
            if filled:
                self.store.add_fill(
                    condition_id=order.get("condition_id"),
                    side=f"SELL_{order.get('side')}",
                    price=attempt,
                    size=size,
                    cost=round(attempt * size, 4),
                    dry_run=False,
                    raw={
                        "order": data or str(signed),
                        "status": data.get("status"),
                        "takingAmount": data.get("takingAmount"),
                        **payload,
                    },
                )
                self.store.close_position(order["condition_id"], order.get("side"))
                return {"status": "live_sell", "response": data or signed, "ticket": payload}
        log.info("Salg umatchet etter FAK-retry — ingen fill")
        return {"status": "resting_sell", "response": data or last_signed, "ticket": payload}
