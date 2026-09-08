from __future__ import annotations

import json
import logging
from typing import Any

import requests

from agent.config import SKIP_QUESTION_PATTERNS, settings

log = logging.getLogger("scout")


def _parse_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            return [value]
    return []


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _category(raw: dict) -> str:
    tags = raw.get("tags") or []
    labels = []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, dict):
                labels.append(str(t.get("slug") or t.get("label") or "").lower())
            else:
                labels.append(str(t).lower())
    blob = " ".join(labels + [str(raw.get("category") or "").lower()])
    for key in (
        "geopolitics",
        "politics",
        "crypto",
        "sports",
        "finance",
        "economics",
        "tech",
        "culture",
        "weather",
        "mentions",
    ):
        if key in blob:
            return key
    return "other"


def _event_key(raw: dict) -> str:
    return str(raw.get("eventSlug") or raw.get("groupItemTitle") or raw.get("questionID") or raw.get("conditionId") or "")


class Scout:
    """Henter likvide, handelbare markeder. Ingen LLM her."""

    def fetch(self, limit: int = 80) -> list[dict]:
        params = {
            "closed": "false",
            "limit": limit,
            "offset": 0,
            "order": "volume24hr",
            "ascending": "false",
        }
        url = f"{settings.gamma_host}/markets"
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        rows = r.json()
        out: list[dict] = []
        for raw in rows:
            if not raw.get("enableOrderBook", True):
                continue
            if raw.get("closed") or not raw.get("active", True):
                continue
            question = str(raw.get("question") or "")
            qlow = question.lower()
            if any(p in qlow for p in SKIP_QUESTION_PATTERNS):
                continue
            tokens = _parse_list(raw.get("clobTokenIds"))
            outcomes = _parse_list(raw.get("outcomes"))
            prices = _parse_list(raw.get("outcomePrices"))
            if len(tokens) < 2 or len(outcomes) < 2:
                continue
            liq = _num(raw.get("liquidityNum") or raw.get("liquidity"))
            vol = _num(raw.get("volume24hr") or raw.get("volume24hrClob"))
            if liq < settings.min_liquidity_usd or vol < settings.min_volume_24h_usd:
                continue
            yes_px = _num(prices[0] if prices else 0)
            no_px = _num(prices[1] if len(prices) > 1 else max(0.0, 1 - yes_px))
            mid = yes_px if 0 < yes_px < 1 else 0.5
            item = {
                "condition_id": raw.get("conditionId") or raw.get("condition_id"),
                "question": question,
                "description": (raw.get("description") or "")[:1500],
                "slug": raw.get("slug"),
                "end_date": raw.get("endDate") or raw.get("endDateIso"),
                "category": _category(raw),
                "event_key": _event_key(raw),
                "liquidity": liq,
                "volume_24h": vol,
                "price_change_1d": _num(raw.get("oneDayPriceChange") or raw.get("oneHourPriceChange")),
                "last_trade": _num(raw.get("lastTradePrice")),
                "yes_token": str(tokens[0]),
                "no_token": str(tokens[1]),
                "yes_label": str(outcomes[0]),
                "no_label": str(outcomes[1]),
                "yes_mid": yes_px,
                "no_mid": no_px,
                "mid": mid,
                "url": f"https://polymarket.com/market/{raw.get('slug')}",
            }
            if item["condition_id"]:
                out.append(item)
        by_event: dict[str, list[dict]] = {}
        for item in out:
            by_event.setdefault(item["event_key"], []).append(
                {"q": (item["question"] or "")[:90], "yes": round(float(item.get("yes_mid") or 0), 3)}
            )
        for item in out:
            key = item["event_key"]
            q = (item["question"] or "")[:90]
            item["siblings"] = [s for s in by_event.get(key, []) if s["q"] != q][:8]
        log.info("Scout: %s kandidater etter filter", len(out))
        return out

    def book(self, token_id: str) -> dict:
        r = requests.get(
            f"{settings.clob_host}/book",
            params={"token_id": token_id},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        best_bid = _num(bids[0]["price"]) if bids else 0.0
        best_ask = _num(asks[0]["price"]) if asks else 1.0
        bid_sz = sum(_num(b.get("size")) for b in bids[:5])
        ask_sz = sum(_num(a.get("size")) for a in asks[:5])
        spread = max(0.0, best_ask - best_bid) if best_bid and best_ask else 1.0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else best_ask or best_bid
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "mid": mid,
            "bid_size": bid_sz,
            "ask_size": ask_sz,
        }

    def enrich(self, markets: list[dict]) -> None:
        """Gratis Polymarket + CoinGecko. Ingen X."""
        crypto = any((m.get("category") or "") == "crypto" for m in markets)
        spots = self._coingecko() if crypto else {}
        seen_events: set[str] = set()
        for m in markets:
            if spots:
                m["spot"] = spots
            token = m.get("yes_token") or ""
            if token:
                m["history"] = self._history(token)
            cid = m.get("condition_id")
            if cid:
                m["prints"] = self._prints(str(cid))
            ev = m.get("event_key") or ""
            if ev and ev not in seen_events:
                seen_events.add(ev)
                extra = self._event_markets(ev)
                if extra:
                    existing = {s.get("q") for s in (m.get("siblings") or [])}
                    existing.add((m.get("question") or "")[:90])
                    more = [s for s in extra if s.get("q") not in existing]
                    m["siblings"] = (m.get("siblings") or []) + more[:8]
        log.info("Enrich: historikk/prints på %s markeder spot=%s", len(markets), bool(spots))

    def _history(self, token_id: str) -> dict:
        try:
            r = requests.get(
                f"{settings.clob_host}/prices-history",
                params={"market": token_id, "interval": "1d", "fidelity": 60},
                timeout=8,
            )
            r.raise_for_status()
            hist = (r.json() or {}).get("history") or r.json()
            if not isinstance(hist, list) or len(hist) < 2:
                return {}
            first = _num(hist[0].get("p") if isinstance(hist[0], dict) else hist[0])
            last = _num(hist[-1].get("p") if isinstance(hist[-1], dict) else hist[-1])
            return {"n": len(hist), "from": round(first, 3), "to": round(last, 3), "chg": round(last - first, 3)}
        except Exception as exc:
            log.debug("history %s: %s", token_id[:12], exc)
            return {}

    def _prints(self, condition_id: str) -> list[dict]:
        try:
            r = requests.get(
                "https://data-api.polymarket.com/trades",
                params={"market": condition_id, "limit": 5},
                timeout=8,
            )
            r.raise_for_status()
            payload = r.json()
            rows = payload if isinstance(payload, list) else payload.get("trades") or []
            out = []
            for t in rows[:5]:
                out.append(
                    {
                        "side": t.get("side") or t.get("outcome"),
                        "px": round(_num(t.get("price")), 3),
                        "sz": round(_num(t.get("size") or t.get("amount")), 1),
                    }
                )
            return out
        except Exception as exc:
            log.debug("prints %s: %s", condition_id[:12], exc)
            return []

    def _event_markets(self, slug: str) -> list[dict]:
        if not slug or " " in slug:
            return []
        try:
            r = requests.get(
                f"{settings.gamma_host}/events",
                params={"slug": slug, "limit": 1},
                timeout=8,
            )
            r.raise_for_status()
            payload = r.json()
            rows = payload if isinstance(payload, list) else []
            if not rows and isinstance(payload, dict):
                rows = [payload]
            ev = rows[0] if rows else None
            if not ev:
                return []
            out = []
            for raw in ev.get("markets") or []:
                q = str(raw.get("question") or "")[:90]
                prices = _parse_list(raw.get("outcomePrices"))
                yes = _num(prices[0] if prices else 0)
                out.append({"q": q, "yes": round(yes, 3)})
            return out
        except Exception:
            return []

    def _coingecko(self) -> dict:
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin,ethereum,solana", "vs_currencies": "usd"},
                timeout=8,
            )
            r.raise_for_status()
            data = r.json()
            return {k: v.get("usd") for k, v in data.items() if isinstance(v, dict)}
        except Exception as exc:
            log.debug("coingecko: %s", exc)
            return {}

