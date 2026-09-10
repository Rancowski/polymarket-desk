from __future__ import annotations

import logging
import math
import re
import time
from typing import Any

import requests

from agent.config import settings
from agent.risk import Ticket, is_sports
from agent.store import Store, kalshi_fields_ok, normalize_side, normalize_source

log = logging.getLogger("exec")


def _num(value: Any) -> float | None:
    if value is None or value is False or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fill_attr(src: Any, extra: dict | None = None) -> dict:
    """Attribution fields for add_fill. Unknown → omitted (stored as null)."""
    extra = extra or {}
    if isinstance(src, Ticket):
        data = {
            "question": src.question,
            "token_id": src.token_id,
            "source": src.source,
            "source_detail": src.source_detail or src.thesis,
            "grok_p": src.grok_p,
            "grok_conf": src.grok_conf,
            "edge_net": src.edge_net,
            "kalshi_ticker": src.kalshi_ticker,
            "kalshi_mid": src.kalshi_mid,
            "pm_mid": src.pm_mid if src.pm_mid is not None else src.mid,
            "gap_c": src.gap_c,
            "cycle_id": src.cycle_id,
            "side": f"BUY_{src.side}" if str(src.side or "").upper() in {"YES", "NO"} else src.side,
        }
    else:
        data = dict(src or {})
        side = str(data.get("side") or "")
        data["side"] = normalize_side(side if side.upper().startswith(("SELL", "REDEEM", "BUY")) else f"SELL_{side}")
        data["source"] = data.get("source")
        data["source_detail"] = data.get("source_detail") or data.get("reason") or data.get("thesis")
        data["question"] = data.get("question")
        data["token_id"] = data.get("token_id")
    data.update(extra)
    out = {
        "question": data.get("question"),
        "token_id": data.get("token_id"),
        "source": normalize_source(data.get("source")),
        "source_detail": (str(data.get("source_detail")).strip()[:160] if data.get("source_detail") else None),
        "grok_p": _num(data.get("grok_p")),
        "grok_conf": data.get("grok_conf") or None,
        "edge_net": _num(data.get("edge_net")),
        "kalshi_ticker": data.get("kalshi_ticker") or None,
        "kalshi_mid": _num(data.get("kalshi_mid")),
        "pm_mid": _num(data.get("pm_mid")),
        "gap_c": _num(data.get("gap_c")),
        "cycle_id": data.get("cycle_id") or None,
        "side": normalize_side(data.get("side")),
    }
    if out["source"] == "kalshi":
        from agent.kalshi import pair_ok

        q = data.get("question")
        if not kalshi_fields_ok(out.get("kalshi_ticker"), out.get("kalshi_mid"), out.get("pm_mid")) or not q:
            out["source"] = "grok" if out.get("grok_p") is not None else None
        elif not pair_ok(str(q), str(out.get("kalshi_ticker") or ""), str(data.get("title") or ""), out.get("kalshi_mid"), out.get("pm_mid"))[0]:
            out["source"] = "grok" if out.get("grok_p") is not None else None
    return out


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


def _place_limit(client: Any, args: Any, tick_s: str, neg: bool, sdk: str = "v1", tif_name: str = "FAK") -> Any:
    import inspect

    if sdk == "v2":
        from py_clob_client_v2 import OrderType, PartialCreateOrderOptions
    else:
        from py_clob_client.clob_types import OrderType, PartialCreateOrderOptions
    tif = getattr(OrderType, str(tif_name or "FAK").upper(), None)
    if tif is None and str(tif_name).upper() == "GTC":
        tif = getattr(OrderType, "GTC", None)
    if tif is None:
        tif = _tif(sdk)
    if tif is None:
        raise RuntimeError("CLOB SDK mangler FAK — avviser ordre i stedet for GTC")
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
    """BUY only. CLOB krever at price*size i 1e6-enheter går opp. Min 5 andeler."""
    px = max(0.01, min(0.99, float(price)))
    p_int = int(round(px * 10000))
    if p_int <= 0:
        return max(5.0, round(size, 2))
    maker_step = 1_000_000 // math.gcd(p_int, 1_000_000)
    step = maker_step * 100 // math.gcd(maker_step, 100)
    units = (int(round(float(size) * 10000)) // step) * step or step
    out = round(units / 10000, 4)
    return max(5.0, out)


def _floor_shares(n: float, places: int = 4) -> float:
    scale = 10 ** places
    return math.floor(max(0.0, float(n)) * scale + 1e-12) / scale


def sell_shares_cap(booked: float, on_chain: float | None) -> float:
    """min(booked, floor(on_chain * 1e4) / 1e4). Never round a sell up."""
    booked_f = _floor_shares(booked, 4)
    if on_chain is None:
        return booked_f
    return min(booked_f, _floor_shares(on_chain, 4))


def sell_size_ladder(shares_sell: float) -> list[float]:
    """[1.00, 0.75, 0.50, 0.25] × shares_sell. Sizes < 5 stay. Strictly decreasing."""
    cap = _floor_shares(shares_sell, 4)
    if cap <= 0:
        return []
    out: list[float] = []
    for frac in (1.0, 0.75, 0.50, 0.25):
        sz = _floor_shares(cap * frac, 4)
        if out and sz >= out[-1]:
            sz = _floor_shares(out[-1] - 0.0001, 4)
        if sz <= 0 or sz in out:
            continue
        out.append(sz)
    return out


def sell_is_dust(shares: float, live_bid: float, deposited: float = 0.0) -> bool:
    if shares < 1.0:
        return True
    notional = float(shares) * float(live_bid or 0)
    if notional < 1.0:
        return True
    if deposited > 0 and notional < 0.001 * deposited:
        return True
    return False


def plan_sell(
    booked: float,
    on_chain: float | None,
    live_bid: float,
    deposited: float = 0.0,
) -> dict:
    """Pure sell plan. No CLOB. Used by sell() and the 2.56-share fixture."""
    shares_sell = sell_shares_cap(booked, on_chain)
    ladder = sell_size_ladder(shares_sell)
    dust = sell_is_dust(shares_sell, live_bid, deposited)
    return {
        "shares_sell": shares_sell,
        "ladder": ladder,
        "dust": dust,
        "notional": round(shares_sell * float(live_bid or 0), 6),
        "fak": (not dust) and bool(ladder),
    }


_BAL_VS_RE = re.compile(r"balance\s+(\d+(?:\.\d+)?)\s+vs\s+order", re.I)


def _parse_clob_token_balance(msg: str) -> float | None:
    m = _BAL_VS_RE.search(msg or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def _sell_amount_size(price: float, size: float, cap: float) -> float:
    """Floor size to CLOB step. Never round up, never min-5, never above cap."""
    cap_f = _floor_shares(min(float(size), float(cap)), 4)
    if cap_f <= 0:
        return 0.0
    px = max(0.01, min(0.99, float(price)))
    p_int = int(round(px * 10000))
    if p_int <= 0:
        return cap_f
    maker_step = 1_000_000 // math.gcd(p_int, 1_000_000)
    step = maker_step * 100 // math.gcd(maker_step, 100)
    units = (int(math.floor(cap_f * 10000 + 1e-12)) // step) * step
    if units <= 0:
        return 0.0
    return min(cap_f, round(units / 10000, 4))


def _quantize(price: float, tick: float) -> float:
    tick = tick if tick > 0 else 0.01
    steps = round(float(price) / tick)
    px = steps * tick
    px = min(1.0 - tick, max(tick, px))
    decimals = len(_tick_literal(tick).split(".")[-1])
    return round(px, decimals)


def _floor_tick(price: float, tick: float) -> float:
    """Round down to the market tick. Does not clamp a live bid down to 0.01."""
    tick = tick if tick > 0 else 0.01
    if float(price) <= 0:
        return 0.0
    steps = math.floor(float(price) / tick + 1e-12)
    px = steps * tick
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
    """Kun takingAmount/makingAmount > 0 er fill. status=matched uten size telles ikke."""
    data = _as_dict(signed)
    taking = str(data.get("takingAmount") if data.get("takingAmount") is not None else "").strip()
    making = str(data.get("makingAmount") if data.get("makingAmount") is not None else "").strip()
    try:
        if float(taking or 0) > 0 or float(making or 0) > 0:
            return True, data
    except (TypeError, ValueError):
        pass
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
                            "redeemable": bool(p.get("redeemable")),
                            "neg_risk": bool(p.get("negativeRisk") or p.get("negRisk") or p.get("neg_risk")),
                            "closed": bool(p.get("closed") or p.get("resolved")),
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
            attr = _fill_attr(ticket, {"cycle_id": ticket.cycle_id or self.store.get_meta("cycle_id") or None})
            self.store.add_fill(
                condition_id=ticket.condition_id,
                side=attr["side"],
                price=ticket.limit_price,
                size=ticket.shares,
                cost=ticket.size_usd,
                dry_run=True,
                raw=payload,
                **{k: v for k, v in attr.items() if k != "side"},
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
                entry_source=attr.get("source"),
                entry_detail=attr.get("source_detail"),
            )
            return {"status": "paper", "ticket": payload}

        if getattr(ticket, "synthetic", False):
            raise RuntimeError("live-kjøp avvist: syntetisk bok")
        src = str(getattr(ticket, "source", "") or "")
        if src not in {"complement", "partition", "maker"} and is_sports({"question": ticket.question, "category": ticket.category, "event_key": ticket.event_key}):
            if not (0.18 < float(ticket.limit_price) < 0.82):
                raise RuntimeError("sports ekstrem-pris")
            sports_pos = [p for p in self.store.positions("open") if is_sports(p)]
            if len(sports_pos) >= 4:
                raise RuntimeError("maks 4 sports")
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
        tif_name = str(getattr(ticket, "tif", None) or ("GTC" if src == "maker" else "FAK")).upper()
        if tif_name != "GTC" and src not in {"complement", "kalshi", "partition"}:
            price = _quantize(min(0.99, price + tick_f), tick_f)
        size = _amount_size(price, max(min_sz, float(ticket.shares), 10.0))
        log.info("CLOB buy px=%s sz=%s tick=%s neg=%s token=%s tif=%s…", price, size, tick_s, neg, token[:14], tif_name)
        try:
            args = OrderArgs(token_id=token, price=price, size=size, side=side, builder_code="")
        except TypeError:
            args = OrderArgs(token_id=token, price=price, size=size, side=side)
        _attach_builder_code(args)
        last_err: Exception | None = None
        signed = None
        for nflag in (neg, (not neg)):
            try:
                signed = _place_limit(client, args, tick_s, nflag, sdk=sdk, tif_name=tif_name)
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
        attr = _fill_attr(ticket, {"cycle_id": ticket.cycle_id or self.store.get_meta("cycle_id") or None})
        self.store.add_fill(
            condition_id=ticket.condition_id,
            side=attr["side"],
            price=fill_px,
            size=fill_size,
            cost=round(fill_px * fill_size, 2),
            dry_run=False,
            raw={"order": data or str(signed), "status": data.get("status"), "takingAmount": data.get("takingAmount"), **payload, "tick": tick_s, "neg_risk": neg, "source": attr.get("source"), "source_detail": attr.get("source_detail")},
            **{k: v for k, v in attr.items() if k != "side"},
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
            entry_source=attr.get("source"),
            entry_detail=attr.get("source_detail"),
        )
        return {"status": "live", "response": data or signed, "ticket": payload}

    def _conditional_balance(self, token_id: str) -> float | None:
        """On-chain ERC-1155 size for this token. None = fetch failed, do not invent."""
        token = str(token_id or "").strip()
        if not token or settings.dry_run:
            return None
        try:
            client = self._live_client()
            if hasattr(client, "get_balance_allowance"):
                from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

                try:
                    params = BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token,
                        signature_type=settings.signature_type,
                    )
                except TypeError:
                    try:
                        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token)
                    except TypeError:
                        params = None
                if params is not None:
                    bal = client.get_balance_allowance(params)
                    parsed = _parse_balance(bal)
                    if parsed > 0:
                        return parsed
        except Exception as exc:
            log.warning("token balance %s: %s", token[:14], exc)
        return None

    def sell(self, order: dict) -> dict:
        payload = {**order}
        token = str(order.get("token_id") or "").strip()
        booked = float(order.get("shares") or 0)
        dust_kind = bool(order.get("dust") or order.get("kind") == "dust")
        book_bid = float(order.get("best_bid") or 0)
        mark = float(order.get("mark") or 0)
        if mark >= 0.90 and book_bid <= 0.01:
            raise RuntimeError("resolved winner — redeem, ikke FAK")
        if str(order.get("kind") or "") == "resolved_loser":
            raise RuntimeError("resolved loser — close locally")
        live = book_bid if book_bid > 0 else 0.0
        if live < 0.02:
            return {
                "status": "no_bid",
                "response": {"error": "ingen live bud"},
                "ticket": payload,
                "attempt_px": None,
                "best_bid": live,
            }
        on_chain = None if settings.dry_run else self._conditional_balance(token)
        try:
            deposited = float(self.store.deposited_usd(0.0) or 0)
        except Exception:
            deposited = 0.0
        plan = plan_sell(booked, on_chain, live, deposited)
        shares_sell = float(plan["shares_sell"])
        size_ladder = list(plan["ladder"])
        payload = {**payload, "shares": shares_sell, "size_usd": round(shares_sell * live, 4)}
        if plan["dust"] or not size_ladder:
            log.info(
                "SELL dust_close shares=%.4f bid=%.3f notional=%.4f (booked=%.4f on_chain=%s)",
                shares_sell,
                live,
                plan["notional"],
                booked,
                on_chain,
            )
            return {
                "status": "dust_close",
                "response": {"error": "dust_close — ikke FAK"},
                "ticket": payload,
                "attempt_px": None,
                "best_bid": live,
                "shares": shares_sell,
                "notional": plan["notional"],
            }

        def _record_paper() -> dict:
            log.info(
                "PAPER SELL %s %s @ %s size=%s (%s)",
                order.get("side"),
                str(order.get("question") or "")[:60],
                order.get("limit_price"),
                shares_sell,
                order.get("reason"),
            )
            attr = _fill_attr(order, {"cycle_id": order.get("cycle_id") or self.store.get_meta("cycle_id") or None})
            self.store.add_fill(
                condition_id=order.get("condition_id"),
                side=attr["side"] or f"SELL_{order.get('side')}",
                price=order.get("limit_price"),
                size=shares_sell,
                cost=round(float(order.get("limit_price") or live) * shares_sell, 4),
                dry_run=True,
                raw=payload,
                **{k: v for k, v in attr.items() if k != "side"},
            )
            leave = order.get("leave_shares")
            try:
                leave_f = float(leave) if leave not in (None, "") else 0.0
            except (TypeError, ValueError):
                leave_f = 0.0
            if leave_f > 0:
                self.store.upsert_position(
                    condition_id=order.get("condition_id"),
                    question=order.get("question"),
                    side=order.get("side"),
                    token_id=order.get("token_id"),
                    shares=leave_f,
                    avg_cost=order.get("avg_cost") or order.get("mark"),
                    status="open",
                )
            else:
                self.store.close_position(order["condition_id"], order.get("side"))
            return {"status": "paper_sell", "ticket": payload}

        if settings.dry_run:
            return _record_paper()

        client = self._live_client()
        sdk = getattr(self, "_sdk", "v1")
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
        tick_f = float(tick_s) if tick_s else 0.01
        if tick_f <= 0.001 + 1e-12 or tick_f < 0.01:
            tick_f = 0.01
            tick_s = "0.01"
        else:
            tick_s = _tick_literal(tick_f)
        attempts: list[float] = []
        for drop in (0, 1):
            attempt = _floor_tick(live - drop * tick_f, tick_f)
            if attempt < 0.01:
                continue
            if attempt not in attempts:
                attempts.append(attempt)
        if not attempts:
            attempts.append(_floor_tick(live, 0.01))
        last_signed: Any = None
        data: dict = {}
        last_attempt = attempts[0] if attempts else live
        last_sz = size_ladder[0]
        failed_sz = shares_sell + 1.0
        cap = shares_sell

        def _bal(msg: str) -> bool:
            m = (msg or "").lower()
            return "not enough" in m or "insufficient" in m or "balance" in m

        for attempt in attempts:
            last_attempt = attempt
            sliced = False
            for sz in size_ladder:
                if sz >= failed_sz:
                    continue
                sz_use = _sell_amount_size(attempt, sz, cap)
                if sz_use <= 0 or sz_use >= failed_sz:
                    continue
                last_sz = sz_use
                try:
                    args = OrderArgs(token_id=token, price=attempt, size=sz_use, side=side, builder_code="")
                except TypeError:
                    args = OrderArgs(token_id=token, price=attempt, size=sz_use, side=side)
                _attach_builder_code(args)
                try:
                    signed = _place_limit(client, args, tick_s, neg, sdk=sdk)
                except Exception as exc:
                    log.warning("SELL %s @ %s sz=%s: %s", (order.get("question") or "")[:40], attempt, sz_use, exc)
                    last_signed = {"error": str(exc)}
                    if _bal(str(exc)):
                        sliced = True
                        failed_sz = sz_use
                        parsed = _parse_clob_token_balance(str(exc))
                        if parsed is not None:
                            cap = min(cap, sell_shares_cap(sz_use, parsed))
                        log.warning("SELL slice — ikke nok balance, neste < %.4f", failed_sz)
                        continue
                    break
                log.info("LIVE SELL try @ %s sz=%s %s", attempt, sz_use, signed)
                filled, data = _order_filled(signed)
                last_signed = signed
                err = str((data or {}).get("error") or (data or {}).get("errorMsg") or "")
                if err and _bal(err):
                    sliced = True
                    failed_sz = sz_use
                    parsed = _parse_clob_token_balance(err)
                    if parsed is not None:
                        cap = min(cap, sell_shares_cap(sz_use, parsed))
                    log.warning("SELL slice — CLOB balance %s", err[:160])
                    continue
                if not filled:
                    break
                order = {**order, "shares": sz_use}
                attr = _fill_attr(order, {"cycle_id": order.get("cycle_id") or self.store.get_meta("cycle_id") or None})
                self.store.add_fill(
                    condition_id=order.get("condition_id"),
                    side=attr["side"] or f"SELL_{order.get('side')}",
                    price=attempt,
                    size=sz_use,
                    cost=round(attempt * sz_use, 4),
                    dry_run=False,
                    raw={
                        "order": data or str(signed),
                        "status": data.get("status"),
                        "takingAmount": data.get("takingAmount"),
                        **payload,
                        "source": attr.get("source"),
                        "source_detail": attr.get("source_detail"),
                    },
                    **{k: v for k, v in attr.items() if k != "side"},
                )
                leave = order.get("leave_shares")
                try:
                    leave_f = float(leave) if leave not in (None, "") else 0.0
                except (TypeError, ValueError):
                    leave_f = 0.0
                if leave_f > 0:
                    self.store.upsert_position(
                        condition_id=order.get("condition_id"),
                        question=order.get("question"),
                        side=order.get("side"),
                        token_id=order.get("token_id"),
                        shares=leave_f,
                        avg_cost=order.get("avg_cost") or order.get("mark"),
                        status="open",
                    )
                else:
                    self.store.close_position(order["condition_id"], order.get("side"))
                return {"status": "live_sell", "response": data or signed, "ticket": payload}
            if sliced:
                # Try remaining strictly-smaller sizes at this price already happened.
                # Next price only if a smaller size is still above dust.
                continue
        leftover = min(cap, last_sz if last_sz > 0 else shares_sell)
        if sell_is_dust(leftover, live, deposited) or dust_kind:
            log.info("SELL dust_close after unmatched leftover=%.4f bid=%.3f", leftover, live)
            return {
                "status": "dust_close",
                "response": data or last_signed,
                "ticket": payload,
                "attempt_px": last_attempt,
                "best_bid": book_bid,
                "shares": leftover,
                "notional": round(leftover * live, 6),
            }
        log.info("Salg umatchet etter FAK-retry — ingen fill")
        return {
            "status": "resting_sell",
            "response": data or last_signed,
            "ticket": payload,
            "attempt_px": last_attempt,
            "best_bid": book_bid,
        }

    def fetch_market_flags(self, condition_id: str) -> dict:
        cid = str(condition_id or "").strip()
        if not cid:
            return {}
        try:
            r = requests.get(
                f"{settings.gamma_host}/markets",
                params={"condition_ids": cid},
                headers={"User-Agent": "polymarket-desk/1.0"},
                timeout=8,
            )
            if not r.ok:
                return {}
            rows = r.json() or []
            if isinstance(rows, dict):
                rows = rows.get("data") or rows.get("markets") or [rows]
            m = rows[0] if rows else {}
            return {
                "closed": bool(m.get("closed") or m.get("resolved")),
                "neg_risk": bool(m.get("negRisk") or m.get("enableNegRisk")),
                "redeemable": bool(m.get("closed") or m.get("resolved")),
            }
        except Exception as exc:
            log.warning("gamma flags %s: %s", cid[:16], exc)
            return {}

    def _cid_hex(self, cid: str) -> str:
        h = str(cid or "").strip().lower()
        if h.startswith("0x"):
            h = h[2:]
        return "0x" + h.zfill(64)

    def _encode_redeem(self, condition_id: str, neg_risk: bool, shares: float) -> tuple[str, str]:
        from eth_abi import encode
        from eth_utils import keccak, to_checksum_address

        cid = self._cid_hex(condition_id)
        cid_b = bytes.fromhex(cid[2:])
        pusd = to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
        adapter = to_checksum_address(
            "0xadA2005600Dec949baf300f4C6120000bDB6eAab"
            if neg_risk
            else "0xAdA100Db00Ca00073811820692005400218FcE1f"
        )
        parent = b"\x00" * 32
        sel = keccak(text="redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
        args = encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [pusd, parent, cid_b, [1, 2]],
        )
        return adapter, "0x" + (sel + args).hex()

    def _encode_ctf_approval(self, operator: str) -> tuple[str, str]:
        from eth_abi import encode
        from eth_utils import keccak, to_checksum_address

        ctf = to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
        op = to_checksum_address(operator)
        sel = keccak(text="setApprovalForAll(address,bool)")[:4]
        args = encode(["address", "bool"], [op, True])
        return ctf, "0x" + (sel + args).hex()

    def _builder_config(self):
        key = settings.builder_api_key or settings.poly_api_key
        secret = settings.builder_secret or settings.poly_api_secret
        phrase = settings.builder_passphrase or settings.poly_api_passphrase
        if not (key and secret and phrase):
            return None
        from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig

        return BuilderConfig(
            local_builder_creds=BuilderApiKeyCreds(key=key, secret=secret, passphrase=phrase)
        )

    def _relayer(self):
        if not settings.private_key:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY mangler for redeem")
        from py_builder_relayer_client.client import RelayClient
        from py_builder_relayer_client.models import RelayerTxType

        builder = self._builder_config()
        wanted = int(settings.signature_type or 3)
        tx_type = {
            1: RelayerTxType.PROXY,
            2: RelayerTxType.SAFE,
        }.get(wanted, RelayerTxType.PROXY)
        return RelayClient(
            settings.relayer_url or "https://relayer-v2.polymarket.com",
            int(settings.chain_id or 137),
            settings.private_key,
            builder,
            tx_type,
        ), wanted

    def _wallet_calls(self, positions: list[dict]) -> list:
        from py_builder_relayer_client.models import DepositWalletCall

        seen: set[str] = set()
        adapters: dict[str, str] = {}
        redeems: list[tuple[str, str]] = []
        for pos in positions:
            cid = str(pos.get("condition_id") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            flags = self.fetch_market_flags(cid)
            neg = bool(pos.get("neg_risk") or flags.get("neg_risk"))
            adapter, calldata = self._encode_redeem(cid, neg, float(pos.get("shares") or 0))
            adapters[adapter] = adapter
            redeems.append((adapter, calldata))
        calls = []
        for adapter in adapters:
            ctf, appr = self._encode_ctf_approval(adapter)
            calls.append(DepositWalletCall(target=ctf, value="0", data=appr))
        for adapter, calldata in redeems:
            calls.append(DepositWalletCall(target=adapter, value="0", data=calldata))
        return calls

    def _tx_hash(self, resp: Any, waited: Any) -> str:
        txh = ""
        if isinstance(waited, dict):
            txh = str(waited.get("transactionHash") or waited.get("transaction_hash") or "")
        elif waited is not None:
            txh = str(
                getattr(waited, "transaction_hash", "")
                or getattr(waited, "transactionHash", "")
                or ""
            )
        if not txh and hasattr(resp, "transaction_hash"):
            txh = str(resp.transaction_hash or "")
        if not txh and hasattr(resp, "hash"):
            txh = str(resp.hash or "")
        return txh

    def _submit_relayer(self, calls: list, metadata: str) -> tuple[Any, str]:
        from py_builder_relayer_client.builder.deposit_wallet import build_deposit_wallet_batch_request
        from py_builder_relayer_client.models import DepositWalletTransactionArgs, Transaction

        client, wanted = self._relayer()
        if wanted in {0, 3} and hasattr(client, "execute_deposit_wallet_batch"):
            nonce_payload = client.get_nonce(client.signer.address(), "WALLET") or {}
            if not isinstance(nonce_payload, dict):
                nonce_payload = {}
            nonce = str(nonce_payload.get("nonce") or "0")
            deadline = str(int(time.time()) + 240)
            wallet = (settings.funder or "").strip()
            if not wallet:
                wallet = client.get_expected_deposit_wallet()
            if client.builder_config is not None:
                resp = client.execute_deposit_wallet_batch(calls, wallet, nonce, deadline)
            elif settings.relayer_api_key:
                args = DepositWalletTransactionArgs(
                    from_address=client.signer.address(),
                    chain_id=int(settings.chain_id or 137),
                    wallet_address=wallet,
                    nonce=nonce,
                    deadline=deadline,
                    calls=calls,
                )
                body = build_deposit_wallet_batch_request(
                    signer=client.signer, args=args, config=client.contract_config
                ).to_dict()
                body["metadata"] = metadata
                url = (settings.relayer_url or "https://relayer-v2.polymarket.com").rstrip("/")
                r = requests.post(
                    f"{url}/submit",
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                        "RELAYER_API_KEY": settings.relayer_api_key,
                        "RELAYER_API_KEY_ADDRESS": (
                            settings.relayer_api_key_address or client.signer.address()
                        ),
                    },
                    timeout=30,
                )
                if r.status_code >= 400:
                    raise RuntimeError(f"relayer {r.status_code}: {r.text[:240]}")
                data = r.json() if r.content else {}

                class _Resp:
                    transaction_id = data.get("transactionID") or data.get("transaction_id")
                    transaction_hash = data.get("transactionHash") or data.get("transaction_hash") or ""
                    hash = transaction_hash

                    def wait(self_inner):
                        if not self_inner.transaction_id:
                            return data
                        return client.poll_until_state(
                            transaction_id=self_inner.transaction_id,
                            states=["STATE_MINED", "STATE_CONFIRMED"],
                            fail_state="STATE_FAILED",
                            max_polls=30,
                        )

                resp = _Resp()
            else:
                raise RuntimeError(
                    "redeem trenger POLYMARKET_BUILDER_API_KEY eller RELAYER_API_KEY "
                    "(POLY_API_KEY brukes som builder-fallback hvis satt)"
                )
        else:
            txs = [Transaction(to=c.target, data=c.data, value=c.value) for c in calls]
            if client.builder_config is None:
                raise RuntimeError("redeem trenger builder-nøkkel for proxy/safe relayer")
            resp = client.execute(txs, metadata)
        waited = None
        try:
            waited = resp.wait() if hasattr(resp, "wait") else None
        except Exception as exc:
            log.warning("redeem wait: %s", exc)
        txh = self._tx_hash(resp, waited)
        if waited is None and not txh:
            raise RuntimeError("redeem relayer timeout/failed uten tx")
        if isinstance(waited, dict) and str(waited.get("state") or "").upper() == "STATE_FAILED":
            raise RuntimeError(f"redeem on-chain failed tx={txh[:18]}")
        return resp, txh

    def _record_redeem(self, pos: dict, txh: str, neg: bool, paper: bool) -> None:
        cid = str(pos.get("condition_id") or "")
        shares = float(pos.get("shares") or 0)
        try:
            mark = float(pos.get("cur_price") or 0)
        except (TypeError, ValueError):
            mark = 0.0
        px = 1.0 if mark >= 0.5 else 0.0
        already = self.store.get_meta(f"redeem_ok:{cid}", "")
        if not already:
            self.store.add_fill(
                condition_id=cid,
                side="REDEEM",
                price=px,
                size=shares,
                cost=round(shares * px, 4),
                dry_run=paper,
                question=pos.get("question"),
                token_id=pos.get("token_id"),
                source="redeem",
                source_detail="redeem_ok",
                cycle_id=self.store.get_meta("cycle_id") or None,
                raw={
                    "redeem": True,
                    "tx": txh,
                    "neg_risk": neg,
                    "takingAmount": str(shares),
                    "status": "matched",
                    "source": "redeem",
                    "question": pos.get("question"),
                },
            )
            self.store.set_meta(f"redeem_ok:{cid}", str(int(time.time())))
        self.store.close_position(cid)
        try:
            self.store.clear_dust(cid, pos.get("side"))
        except Exception:
            pass

    def redeem(self, pos: dict) -> dict:
        """On-chain CTF redeem via proxy/relayer. Winners → pUSD. Never FAK."""
        results = self.redeem_batch([pos])
        cid = str(pos.get("condition_id") or "")
        return results.get(cid) or next(iter(results.values()), {"status": "redeem_err", "condition_id": cid})

    def redeem_batch(self, positions: list[dict]) -> dict[str, dict]:
        """Redeem unique condition_ids in one relayer batch. Adapter wraps to pUSD."""
        out: dict[str, dict] = {}
        unique: list[dict] = []
        seen: set[str] = set()
        for pos in positions:
            cid = str(pos.get("condition_id") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            unique.append(pos)
        if not unique:
            return out
        if settings.dry_run:
            for pos in unique:
                cid = str(pos.get("condition_id"))
                q = str(pos.get("question") or "")[:60]
                log.info("PAPER REDEEM %s", q)
                self._record_redeem(pos, "", bool(pos.get("neg_risk")), True)
                out[cid] = {"status": "paper_redeem", "condition_id": cid}
            return out
        calls = self._wallet_calls(unique)
        if not calls:
            raise RuntimeError("ingen redeem-calls")
        labels = ",".join(str(p.get("question") or "")[:24] for p in unique[:4])
        _resp, txh = self._submit_relayer(calls, f"redeem {labels}")
        for pos in unique:
            cid = str(pos.get("condition_id"))
            flags = self.fetch_market_flags(cid)
            neg = bool(pos.get("neg_risk") or flags.get("neg_risk"))
            q = str(pos.get("question") or "")[:60]
            log.info("REDEEM ok %s tx=%s", q, txh[:18] if txh else "?")
            self._record_redeem(pos, txh, neg, False)
            out[cid] = {"status": "redeem_ok", "tx": txh, "condition_id": cid, "neg_risk": neg}
        return out
