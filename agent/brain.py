from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

from agent.config import settings

log = logging.getLogger("brain")

SYSTEM = """Du er sannsynlighetsanalytiker for binære prediction markets på Polymarket.
Oppgave: estimer P(YES inntreffer slik resolusjonskilden definerer det), ikke hva som «burde» skje.

Regler:
- Vær konservativ. Hvis informasjonen er tynn, sett confidence=low og p nær markedet.
- Ikke jakt edge. De fleste markeder er OK priset.
- Ta hensyn til resolusjonstekst, tid, base rates og nyhetsbildet du kjenner.
- Aldri 0 eller 1. Hold p i [0.02, 0.98].
- Svar KUN gyldig JSON-array. Ingen markdown.

Hvert element:
{
  "condition_id": "...",
  "p_yes": 0.0-1.0,
  "confidence": "low"|"medium"|"high",
  "thesis": "en setning",
  "skip": false,
  "skip_reason": ""
}
"""


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    return json.loads(text)


class Brain:
    def estimate(self, markets: list[dict]) -> dict[str, dict]:
        if not markets:
            return {}
        if not settings.xai_api_key:
            log.warning("XAI_API_KEY mangler — hopper over estimat")
            return {}
        payload_markets = [
            {
                "condition_id": m["condition_id"],
                "question": m["question"],
                "description": m.get("description", "")[:600],
                "end_date": m.get("end_date"),
                "category": m.get("category"),
                "yes_mid": round(m.get("yes_mid") or m.get("mid") or 0.5, 3),
                "liquidity": int(m.get("liquidity") or 0),
            }
            for m in markets
        ]
        body = {
            "model": settings.grok_model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": "Estimer disse markedene. Markedspris er kun referanse, ikke fasit.\n"
                    + json.dumps(payload_markets, ensure_ascii=False),
                },
            ],
        }
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.xai_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=120,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"][content]
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
                "confidence": str(row.get("confidence") or "low").lower(),
                "thesis": str(row.get("thesis") or ""),
                "skip": bool(row.get("skip")),
                "skip_reason": str(row.get("skip_reason") or ""),
            }
        log.info("Brain: estimat for %s/%s markeder", len(out), len(markets))
        return out
