from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

from agent.config import settings

log = logging.getLogger("brain")

SYSTEM = """Du er sannsynlighetsanalytiker for binære Polymarket-markeder.
Du får KUN gratis markedsdata: Polymarket (resolusjon, mid, spread, dybde, volum,
likviditet, 1d-historikk, siste prints, søsken i samme event) og ev. CoinGecko-spot for crypto.
Ingen web. Ingen X.

Oppgave: estimer P(YES slik resolusjonskilden definerer det) og finn intern feilprising.

Jakte edge i:
- Søskenmarkeder som ikke summerer (~1.0 for uttømmende utfall)
- Bred spread / tynn bok vs mid
- Resolusjonstekst vs hva mid antyder
- Tid til slutt + already-moved price_change_1d
Ikke finn på nyheter du ikke har. Mangler live-info: hold deg nær mid med mindre mikrostrukturen er feil.

confidence=high ved klar intern inkonsistens. medium ved rimelig signal. low hvis bare støy.
skip=true bare hvis uleselig eller allerede avgjort.
Aldri 0 eller 1. p i [0.02, 0.98].
Svar KUN gyldig JSON-array. Ingen markdown.

{
  "condition_id": "...",
  "p_yes": 0.0-1.0,
  "confidence": "low"|"medium"|"high",
  "thesis": "en setning om Polymarket-signalet",
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

    def estimate(self, markets: list[dict]) -> dict[str, dict]:
        if not markets:
            return {}
        key = (settings.xai_api_key or "").strip()
        if not key:
            log.warning("XAI_API_KEY mangler — hopper over estimat")
            return {}
        if not key.startswith("xai-"):
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
                }
            )
        user = (
            "Estimer P(YES) fra Polymarket-feltene. Marker intern edge i thesis.\n"
            + json.dumps(payload_markets, ensure_ascii=False)
        )
        models = [settings.grok_model, "grok-4.5", "grok-4"]
        seen: set[str] = set()
        last_err = ""
        for model in models:
            if not model or model in seen:
                continue
            seen.add(model)
            try:
                text = self._call(key, model, user)
                if text:
                    return self._parse(text, markets)
            except Exception as exc:
                last_err = str(exc)
                log.warning("xAI %s feilet: %s", model, last_err[:300])
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
            timeout=90,
        )
        if not r.ok:
            log.error("xAI HTTP %s: %s", r.status_code, r.text[:400])
            r.raise_for_status()
        return _response_text(r.json())

    def _parse(self, content: str, markets: list[dict]) -> dict[str, dict]:
        rows = _extract_json(content)
        out: dict[str, dict] = {}
        for row in rows:
            cid = str(row.get("condition_id") or "")
            if not cid:
                continue
            try:
                p = float(row.get("p_yes"))
            except (TypeError, ValueError):
                continue
            p = min(0.98, max(0.02, p))
            out[cid] = {
                "p_yes": p,
                "confidence": str(row.get("confidence") or "medium").lower(),
                "thesis": str(row.get("thesis") or ""),
                "skip": bool(row.get("skip")),
                "skip_reason": str(row.get("skip_reason") or ""),
            }
        log.info("Brain: estimat for %s/%s markeder (ingen live-søk)", len(out), len(markets))
        return out
