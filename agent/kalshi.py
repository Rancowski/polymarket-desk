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


def _theme(text: str) -> set[str]:
    t = (text or "").lower()
    tags: set[str] = set()
    if any(x in t for x in ("fed", "fomc", "federal reserve")):
        tags.add("fed")
        if "25" in t:
            tags.add("25")
        if "50" in t:
            tags.add("50")
        if any(x in t for x in ("cut", "decrease", "lower", "ease")):
            tags.add("cut")
        if any(x in t for x in ("hike", "increase", "raise")):
            tags.add("hike")
        if any(x in t for x in ("no change", "unchanged", "hold", "pause")):
            tags.add("hold")
    if "bitcoin" in t or re.search(r"\bbtc\b", t):
        tags.add("btc")
    if "ethereum" in t or re.search(r"\beth\b", t):
        tags.add("eth")
    if "trump" in t:
        tags.add("trump")
    if "harris" in t:
        tags.add("harris")
    if "israel" in t and any(x in t for x in ("airspace", "air space", "idf")):
        tags.add("israel-air")
    tags |= {f"y{y}" for y in re.findall(r"20\d{2}", t)}
    return tags


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
                    "theme": _theme(title),
                }
            )
        log.info("Kalshi: %s åpne markeder", len(out))
        return out
    except Exception as exc:
        log.warning("Kalshi-henting feilet: %s", exc)
        return []


def attach(markets: list[dict], kalshi: list[dict] | None = None) -> int:
    if kalshi is None:
        kalshi = fetch_open()
    if not kalshi:
        return 0
    hits = 0
    for m in markets:
        q = m.get("question") or ""
        qtok = _tokens(q)
        qtheme = _theme(q)
        best = None
        best_score = 0.0
        for k in kalshi:
            n = len(qtok & k["tokens"])
            t = len(qtheme & k["theme"])
            score = n + 3 * t
            distinctive = qtheme & k["theme"] & {
                "fed", "btc", "eth", "trump", "harris", "israel-air",
            }
            theme_ok = t >= 2 and bool(distinctive)
            tok_need = 3 if distinctive else 4
            if not theme_ok and n < tok_need:
                continue
            if score > best_score:
                best_score = score
                best = k
        if not best:
            continue
        years = set(re.findall(r"20\d{2}", q))
        kyears = set(re.findall(r"20\d{2}", best["title"]))
        if years and kyears and not years.issubset(kyears):
            continue
        poly = float(m.get("yes_mid") or m.get("mid") or 0)
        gap = round(poly - float(best["yes"]), 3)
        m["kalshi"] = {
            "title": best["title"][:90],
            "ticker": best["ticker"],
            "yes": round(float(best["yes"]), 3),
            "gap": gap,
            "overlap": int(best_score),
        }
        hits += 1
    log.info("Kalshi: %s treff mot Polymarket-batch", hits)
    return hits
