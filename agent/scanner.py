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
