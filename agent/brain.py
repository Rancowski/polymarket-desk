from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

from agent.config import settings

log = logging.getLogger("brain")

SYSTEM = """You are picking trades on binary Polymarket markets, not writing a caution memo.
You only get free tape: Polymarket book (mid, spread, depth, volume, last trade,
YES+NO sum, siblings, time to resolution, 1d history) and optional CoinGecko spot.
Kalshi is another venue on the SAME contract. Gap ≥ 4¢: pull p_yes hard toward Kalshi.
No web. No X. No live search.

For each name: YES or NO or SKIP.
SKIP only if you have no read or the market is noise.
confidence=low = you truly do not know. It is not the default.
confidence=medium = a real directional view with some uncertainty — this MUST be allowed to trade.
confidence=high = clean read.
Prefer the 8–48h names in the batch when two views are similar.
Return at least one non-SKIP when the batch contains ≥3 names in mid 0.22–0.78
with hours_left 8–48h, unless every one is in-play tape.

Hunt edge in:
- Kalshi vs Polymarket (same event, different price)
- Sibling markets that do not sum to ~1
- Resolution text vs mid
- Crypto vs CoinGecko spot
Do not call live sport/esport "resolved" just because mid moved. skip=true only if the
match looks finished (mid > 0.90 or < 0.10 without Kalshi support).
Do not invent news. If you lack info, stay near mid with confidence=medium, not low.

Never 0 or 1. p in [0.02, 0.98].
Reply with ONLY a valid JSON array.

{
  "condition_id": "...",
  "p_yes": 0.0-1.0,
  "confidence": "low"|"medium"|"high",
  "thesis": "one sentence",
  "skip": false,
  "skip_reason": ""
}
"""


def _extract_json(text: str) -> Any:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    return json.loads(text)


def _response_text(data: dict) -> str:
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


class Brain:
    """Grok på Polymarket-mikrostruktur. Ingen X/web-søk (kostnad)."""

    def __init__(self) -> None:
        self.last_usage = {"usd": 0.0, "tokens": 0, "model": ""}
        self.last_skip = ""

    def estimate(self, markets: list[dict]) -> dict[str, dict]:
        self.last_usage = {"usd": 0.0, "tokens": 0, "model": ""}
        self.last_skip = ""
        if not markets:
            self.last_skip = "filter"
            return {}
        key = (settings.xai_api_key or "").strip()
        if not key:
            self.last_skip = "timeout/missing key"
            log.warning("XAI_API_KEY mangler — hopper over estimat")
            return {}
        if not key.startswith("xai-"):
            self.last_skip = "filter"
            log.error("XAI_API_KEY ser feil ut. Hopper over estimat.")
            return {}
        payload_markets = []
        for m in markets:
            book = m.get("book") or {}
            payload_markets.append(
                {
                    "condition_id": m["condition_id"],
                    "question": m["question"],
                    "description": (m.get("description") or "")[:700],
                    "end_date": m.get("end_date"),
                    "category": m.get("category"),
                    "yes_mid": round(float(m.get("yes_mid") or m.get("mid") or 0.5), 3),
                    "no_mid": round(float(m.get("no_mid") or 0), 3),
                    "complement": m.get("complement"),
                    "hours_left": m.get("hours_left"),
                    "last_trade": m.get("last_trade"),
                    "liquidity": int(m.get("liquidity") or 0),
                    "volume_24h": int(m.get("volume_24h") or 0),
                    "price_change_1d": m.get("price_change_1d"),
                    "spread": round(float(book.get("spread") or 0), 4),
                    "best_bid": round(float(book.get("best_bid") or 0), 3),
                    "best_ask": round(float(book.get("best_ask") or 0), 3),
                    "bid_size": round(float(book.get("bid_size") or 0), 1),
                    "ask_size": round(float(book.get("ask_size") or 0), 1),
                    "history": m.get("history") or {},
                    "prints": m.get("prints") or [],
                    "siblings": m.get("siblings") or [],
                    "spot": m.get("spot") or {},
                    "kalshi": m.get("kalshi") or {},
                    "open_position": bool(m.get("_open_only")),
                    "bucket": m.get("_grok_bucket") or "",
                }
            )
        n_near = 0
        for m in markets:
            try:
                h = float(m.get("hours_left")) if m.get("hours_left") not in (None, "") else None
                mid = float(m.get("yes_mid") or m.get("mid") or 0)
            except (TypeError, ValueError):
                h, mid = None, 0.0
            if h is not None and 8 <= h <= 48 and 0.22 <= mid <= 0.78:
                n_near += 1
        user = (
            "Pick trades from these Polymarket fields. Mark the edge in thesis.\n"
            f"Names in 8–48h with mid 0.22–0.78: {n_near}. "
            "If that count is ≥3, return at least one non-SKIP with confidence medium or high "
            "unless every one is in-play tape. confidence=low is not the default.\n"
            + json.dumps(payload_markets, ensure_ascii=False)
        )
        models = [settings.grok_model, "grok-4.5", "grok-4"]
        seen: set[str] = set()
        last_err = ""
        usd = 0.0
        tokens_n = 0
        used = ""
        for model in models:
            if not model or model in seen:
                continue
            seen.add(model)
            try:
                text, cost, tokens = self._call(key, model, user)
                usd += cost
                tokens_n += tokens
                used = model
                self.last_usage = {"usd": usd, "tokens": tokens_n, "model": used}
                if text:
                    parsed = self._parse(text, markets)
                    if not parsed:
                        self.last_skip = "parse"
                        log.warning("Grok parse tom for %s navn", len(markets))
                    return parsed
                self.last_skip = "parse"
            except Exception as exc:
                last_err = str(exc)
                low = last_err.lower()
                if "timeout" in low or "timed out" in low:
                    self.last_skip = "timeout"
                else:
                    self.last_skip = f"timeout/http {last_err[:80]}"
                log.warning("xAI %s feilet: %s", model, last_err[:300])
        self.last_usage = {"usd": usd, "tokens": tokens_n, "model": used}
        self.last_skip = self.last_skip or (f"timeout/http {last_err[:80]}" if last_err else "timeout")
        log.error("Ingen Grok-modell svarte. Siste feil: %s", last_err[:400])
        return {}

    def _call(self, key: str, model: str, user: str) -> str:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers=headers,
            json=body,
            timeout=40,
        )
        if not r.ok:
            log.error("xAI HTTP %s: %s", r.status_code, r.text[:400])
            r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        ticks = usage.get("cost_in_usd_ticks") or 0
        cost = float(ticks) / 10_000_000_000 if ticks else 0.0
        if not cost:
            # fallback grok-4.6 listpris hvis feltet mangler
            inn = float(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            out = float(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            cost = inn / 1_000_000 * 2.0 + out / 1_000_000 * 6.0
        tokens = int(usage.get("total_tokens") or 0)
        log.info("xAI %s kost=$%.4f tokens=%s", model, cost, tokens)
        return _response_text(data), cost, tokens

    def _parse(self, content: str, markets: list[dict]) -> dict[str, dict]:
        try:
            rows = _extract_json(content)
        except Exception:
            self.last_skip = "parse"
            log.warning("Grok JSON parse feilet")
            return {}
        if not isinstance(rows, list):
            rows = [rows] if isinstance(rows, dict) else []
        known = {str(m.get("condition_id")): m for m in markets}
        by_q = {(m.get("question") or "")[:80].lower(): str(m.get("condition_id")) for m in markets}
        out: dict[str, dict] = {}
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            cid = str(row.get("condition_id") or "")
            if cid not in known:
                q = str(row.get("question") or "")[:80].lower()
                cid = by_q.get(q) or (list(known.keys())[i] if i < len(known) else "")
            if not cid:
                continue
            try:
                p = float(row.get("p_yes"))
            except (TypeError, ValueError):
                continue
            p = min(0.98, max(0.02, p))
            raw_conf = row.get("confidence")
            if raw_conf is None or str(raw_conf).strip() == "":
                conf = "medium"
            else:
                conf = str(raw_conf).strip().lower()
            if conf not in {"low", "medium", "high"}:
                conf = "medium"
            out[cid] = {
                "p_yes": p,
                "thesis": str(row.get("thesis") or ""),
                "confidence": conf,
                "skip": bool(row.get("skip")),
                "skip_reason": str(row.get("skip_reason") or ""),
            }
        log.info("Brain: estimat for %s/%s markeder (ingen live-søk)", len(out), len(markets))
        return out
