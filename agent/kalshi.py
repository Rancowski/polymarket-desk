"""Kalshi som gratis prissignal. Ingen Kalshi-konto, ingen ordre der."""
from __future__ import annotations

import logging
import re
from typing import Any

import requests

log = logging.getLogger("kalshi")
HOST = "https://external-api.kalshi.com/trade-api/v2"
_STOP = {
    "will", "the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "by",
    "be", "is", "at", "after", "before", "win", "vs", "versus", "game",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOP}


def _yes_px(row: dict) -> float:
    for key in ("yes_ask_dollars", "last_price_dollars", "yes_bid_dollars"):
        try:
            v = float(row.get(key) or 0)
            if 0 < v < 1:
                return v
        except (TypeError, ValueError):
            continue
    return 0.0


def fetch_open(limit: int = 200) -> list[dict]:
    try:
        r = requests.get(
            f"{HOST}/markets",
            params={"limit": limit, "status": "open"},
            timeout=12,
        )
        r.raise_for_status()
        rows = (r.json() or {}).get("markets") or []
        out = []
        for row in rows:
            title = str(row.get("title") or row.get("yes_sub_title") or "")
            px = _yes_px(row)
            if not title or not px:
                continue
            out.append(
                {
                    "title": title,
                    "ticker": row.get("ticker"),
                    "yes": px,
                    "tokens": _tokens(title),
                }
            )
        log.info("Kalshi: %s åpne markeder", len(out))
        return out
    except Exception as exc:
        log.warning("Kalshi-henting feilet: %s", exc)
        return []


def attach(markets: list[dict], kalshi: list[dict] | None = None) -> int:
    """Koble beste Kalshi-treff på Polymarket-spørsmål. Returnerer antall treff."""
    if kalshi is None:
        kalshi = fetch_open()
    if not kalshi:
        return 0
    hits = 0
    for m in markets:
        qtok = _tokens(m.get("question") or "")
        if len(qtok) < 3:
            continue
        best = None
        best_n = 0
        for k in kalshi:
            n = len(qtok & k["tokens"])
            if n > best_n:
                best_n = n
                best = k
        need = max(4, min(6, len(qtok) // 2 + 1))
        if not best or best_n < need:
            continue
        denom = max(len(qtok), len(best["tokens"]), 1)
        if best_n / denom < 0.35:
            continue
        poly = float(m.get("yes_mid") or m.get("mid") or 0)
        gap = round(poly - float(best["yes"]), 3)
        years = set(re.findall(r"20\d{2}", m.get("question") or ""))
        if years and not years.issubset(set(re.findall(r"20\d{2}", best["title"]))):
            continue
        m["kalshi"] = {
            "title": best["title"][:90],
            "ticker": best["ticker"],
            "yes": round(float(best["yes"]), 3),
            "gap": gap,
            "overlap": best_n,
        }
        hits += 1
    log.info("Kalshi: %s treff mot Polymarket-batch", hits)
    return hits
