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
        # «25» kun for 25 bps/bp — aldri fordi tittelen har 4.25 %.
        if re.search(r"(?<![\d.])25\+?\s*(bps|bp|basis)", t):
            tags.add("25")
        if re.search(r"(?<![\d.])50\+?\s*(bps|bp|basis)", t):
            tags.add("50")
        if re.search(r"\b(cut|decrease|lower|easing)\b", t) or re.search(r"\beast\b", t):
            tags.add("cut")
        if re.search(r"\b(hike|increase|raise|hikes|increases)\b", t):
            tags.add("hike")
        if re.search(r"\b(unchanged|hold|pause)\b", t) or "no change" in t:
            tags.add("hold")
        if re.search(r"above\s+\d", t) or "funds rate" in t or "fed funds" in t:
            tags.add("funds")
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
    if any(x in t for x in ("us open", "atp", "wimbledon")):
        tags.add("tennis")
    tags |= {f"y{y}" for y in re.findall(r"20\d{2}", t)}
    return tags


_MONTH_ALIAS = {
    "january": "january", "jan": "january",
    "february": "february", "feb": "february",
    "march": "march", "mar": "march",
    "april": "april", "apr": "april",
    "may": "may",
    "june": "june", "jun": "june",
    "july": "july", "jul": "july",
    "august": "august", "aug": "august",
    "september": "september", "sep": "september", "sept": "september",
    "october": "october", "oct": "october",
    "november": "november", "nov": "november",
    "december": "december", "dec": "december",
}
_NUM_MONTH = {
    "01": "january", "02": "february", "03": "march", "04": "april",
    "05": "may", "06": "june", "07": "july", "08": "august",
    "09": "september", "10": "october", "11": "november", "12": "december",
}


def _human_text(text: str) -> str:
    """Strip 0x condition ids / long hex so 2025 inside a cid is not a year."""
    t = text or ""
    t = re.sub(r"0x[0-9a-fA-F]{8,}", " ", t)
    t = re.sub(r"\b[0-9a-fA-F]{32,}\b", " ", t)
    return t


def _years(text: str) -> set[int]:
    """20xx from titles, and YY before a month in tickers (26SEP → 2026). Not 25 bps."""
    t = _human_text(text)
    found: set[int] = set()
    for y in re.findall(r"20\d{2}", t):
        yi = int(y)
        if 2020 <= yi <= 2035:
            found.add(yi)
    low = t.lower()
    for yy, _mon in re.findall(
        r"(?<![0-9])([12][0-9])(jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)",
        low,
    ):
        yi = 2000 + int(yy)
        if 2020 <= yi <= 2035:
            found.add(yi)
    return found


def _months(text: str) -> set[str]:
    """Full names, ticker codes (26SEP, SEP17), and 2026-09. sep ≡ september."""
    t = _human_text(text).lower()
    found: set[str] = set()
    for full in (
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ):
        if re.search(rf"\b{full}\b", t):
            found.add(full)
    for m in re.findall(
        r"(?<![a-z])(jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)(?![a-z])",
        t,
    ):
        found.add(_MONTH_ALIAS.get(m, m))
    for mm in re.findall(r"20\d{2}[-/]?(0[1-9]|1[0-2])", t):
        found.add(_NUM_MONTH.get(mm[-2:], mm))
    return found


def _as_prob(raw: Any) -> float:
    """Kalshi yes as dollars (0.45) or cents (45). Never treat 25 as 0.25 of 4.25%."""
    if raw is None or raw is False or raw == "":
        return 0.0
    try:
        v = float(str(raw).replace("%", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return 0.0
    if 0.001 <= v <= 0.999:
        return v
    if 1.5 < v <= 100:
        if abs(v - round(v)) > 0.051:
            return 0.0  # 4.25 is a rate, not 4 ¢
        v = round(v) / 100.0
        return v if 0.001 <= v <= 0.999 else 0.0
    if 100 < v <= 1000:
        v = v / 1000.0
        return v if 0.001 <= v <= 0.999 else 0.0
    return 0.0


def _yes_px(row: dict) -> float:
    bid = _as_prob(row.get("yes_bid_dollars")) or _as_prob(row.get("yes_bid"))
    ask = _as_prob(row.get("yes_ask_dollars")) or _as_prob(row.get("yes_ask"))
    last = _as_prob(row.get("last_price_dollars")) or _as_prob(row.get("last_price"))
    if bid and ask:
        return (bid + ask) / 2.0
    return ask or bid or last or _as_prob(row.get("yes_price")) or _as_prob(row.get("previous_yes_ask"))


HOSTS = (
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",
)


SERIES = (
    "KXFEDHIKE",
    "KXFEDDECISION",
    "KXFED",
    "KXFEDFUNDS",
    "KXFOMC",
    "KXBTC",
    "KXBTCD",
    "KXBTCMAX",
    "KXETH",
    "KXETHD",
    "KXETHMAX",
    "KXTRUMP",
    "KXHARRIS",
    "KXCPI",
    "KXGPD",
)


def _rows_to_out(rows: list) -> list[dict]:
    out = []
    for row in rows:
        if str(row.get("market_type") or "binary") not in {"binary", ""}:
            continue
        title = str(row.get("title") or row.get("yes_sub_title") or row.get("subtitle") or "")
        low = title.lower()
        if any(x in low for x in ("parlay", "combo", "same game", "sgp", "multivariate", "which of the following")):
            continue
        px = _yes_px(row)
        if not title or not px:
            continue
        ticker = str(row.get("ticker") or "")
        blob = f"{title} {ticker}"
        out.append(
            {
                "title": title,
                "ticker": ticker,
                "yes": px,
                "tokens": _tokens(title),
                "theme": _theme(title),
                "years": _years(blob),
                "months": _months(blob),
            }
        )
    return out


def fetch_open(limit: int = 400) -> list[dict]:
    last_exc: Exception | None = None
    collected: list[dict] = []
    seen: set[str] = set()
    for host in HOSTS:
        host_n = 0
        for series in SERIES:
            cursor = None
            series_n = 0
            try:
                for _page in range(6):
                    params: dict[str, Any] = {
                        "limit": 200,
                        "status": "open",
                        "series_ticker": series,
                    }
                    if cursor:
                        params["cursor"] = cursor
                    r = requests.get(f"{host}/markets", params=params, timeout=12)
                    if r.status_code == 429:
                        log.warning("Kalshi 429 på %s — stopper serier", series)
                        break
                    if r.status_code >= 400:
                        break
                    data = r.json() or {}
                    rows = data.get("markets") or []
                    chunk = _rows_to_out(rows)
                    for item in chunk:
                        t = str(item.get("ticker") or item["title"])
                        if t in seen:
                            continue
                        seen.add(t)
                        collected.append(item)
                        host_n += 1
                        series_n += 1
                    log.info("Kalshi %s: %s rader, %s pris", series, len(rows), len(chunk))
                    cursor = data.get("cursor") or data.get("next_cursor") or ""
                    if not rows or not str(cursor).strip() or len(rows) < 200:
                        break
            except Exception as ext:
                last_exc = ext
                log.warning("Kalshi %s: %s", series, ext)
        if host_n:
            break
    log.info("Kalshi totalt %s markeder med pris (kun navngitte serier)", len(collected))
    if not collected and last_exc:
        log.warning("Kalshi-henting feilet: %s", last_exc)
    return collected[:limit]


def _pick(q: str, kalshi: list[dict]) -> tuple[dict | None, str]:
    """Comparable named contract only. Returns (row, skip_why)."""
    qtok = _tokens(_human_text(q))
    qtheme = _theme(_human_text(q))
    qmonths = _months(q)
    qyears = _years(q)
    best = None
    best_score = 0.0
    month_conflict = False
    year_conflict = False
    for k in kalshi:
        n = len(qtok & k["tokens"])
        t = len(qtheme & k["theme"])
        score = n + 3 * t
        distinctive = qtheme & k["theme"] & {
            "fed", "btc", "eth", "trump", "harris", "israel-air", "tennis",
        }
        bps = qtheme & k["theme"] & {"25", "50", "hike", "cut", "hold"}
        fed_ok = "fed" in distinctive and (bool(bps) or t >= 2)
        theme_ok = fed_ok or (t >= 2 and bool(distinctive)) or (t >= 1 and n >= 3 and bool(distinctive))
        tok_need = 3 if distinctive else 4
        if not theme_ok and n < tok_need:
            continue
        ticker = str(k.get("ticker") or "")
        title = str(k.get("title") or "")
        # Ticker/series month is source of truth (26SEP). Title month is fallback only.
        km = _months(ticker) or k.get("months") or _months(title)
        ky = _years(ticker) or k.get("years") or _years(title)
        if qmonths and km and not (qmonths & km):
            month_conflict = True
            continue
        if qyears and ky and not (qyears & ky):
            year_conflict = True
            continue
        if qmonths and km and (qmonths & km):
            score += 8
        if qyears and ky and (qyears & ky):
            score += 8
        if score > best_score:
            best_score = score
            best = k
    if not best:
        if year_conflict and not month_conflict:
            return None, "ulikt år"
        if month_conflict:
            return None, "ulik måned"
        return None, "ingen sammenlignbar kontrakt"
    bticker = str(best.get("ticker") or "")
    btitle = str(best.get("title") or "")
    kyears = _years(bticker) or best.get("years") or _years(btitle)
    if qyears and kyears and not (qyears & kyears):
        return None, "ulikt år"
    km = _months(bticker) or best.get("months") or _months(btitle)
    if qmonths and km and not (qmonths & km):
        return None, "ulik måned"
    qth, kth = qtheme, best["theme"]
    if ("25" in qth) != ("25" in kth):
        return None, "25 bps mismatch"
    if ("50" in qth) != ("50" in kth):
        return None, "50 bps mismatch"
    if ("funds" in kth and "hike" in qth) or ("funds" in qth and "hike" in kth):
        return None, "funds vs hike"
    for tag in ("hike", "cut", "hold"):
        if tag in qth or tag in kth:
            if tag not in qth or tag not in kth:
                return None, f"{tag} mismatch"
    best = {**best, "overlap": int(best_score)}
    return best, ""


def _named_target(m: dict) -> bool:
    if m.get("_open_only"):
        return True
    cat = str(m.get("category") or "").lower()
    if cat in {"economics", "finance", "crypto", "politics", "geopolitics"}:
        return True
    blob = f"{m.get('question') or ''} {m.get('event_key') or ''}".lower()
    return any(x in blob for x in ("fed", "fomc", "bitcoin", "btc", "ethereum", "eth", "trump"))


def compare(markets: list[dict], kalshi: list[dict] | None = None) -> tuple[int, list[dict]]:
    """Attach Kalshi and return (hits, compare-log). Log every named-series attempt."""
    from agent.risk import is_sports

    if kalshi is None:
        kalshi = fetch_open()
    if not kalshi:
        return 0, []
    hits = 0
    logs: list[dict] = []
    for m in markets:
        q = m.get("question") or ""
        if not q:
            continue
        if is_sports(m):
            continue
        want = _named_target(m)
        slug = str(m.get("event_key") or "")
        blob = q
        if slug and not slug.startswith("0x") and len(slug) < 96 and " " not in slug[:2]:
            blob = f"{q} {slug}"
        best, why = _pick(blob, kalshi)
        poly = float(m.get("yes_mid") or m.get("mid") or 0)
        k_yes = float(best["yes"]) if best else 0.0
        if not best or k_yes <= 0:
            if want:
                logs.append(
                    {
                        "condition_id": m.get("condition_id"),
                        "question": q[:90],
                        "ticker": "",
                        "pm": round(poly, 3),
                        "kalshi": None,
                        "gap": None,
                        "action": "skip",
                        "why": why if not best else "kalshi_yes=0",
                    }
                )
            continue
        gap = round(poly - k_yes, 3)
        payload = {
            "title": str(best["title"])[:90],
            "ticker": best.get("ticker"),
            "yes": round(k_yes, 3),
            "gap": gap,
            "overlap": int(best.get("overlap") or 0),
        }
        m["kalshi"] = payload
        hits += 1
        action, reason = "skip", f"gap {gap:+.2f} < 4c"
        side = str(m.get("side") or "").upper()
        avg = float(m.get("avg_cost") or 0)
        k_yes = float(best["yes"])
        if 0.02 < k_yes < 0.98 and avg > 0 and side in {"YES", "NO"}:
            k_hat = k_yes if side == "YES" else 1.0 - k_yes
            if k_hat + 0.07 < avg:
                action, reason = "sell", f"Kalshi {k_hat:.2f} ≥7c mot kost {avg:.2f}"
            elif abs(gap) >= 0.04:
                action, reason = "skip", "allerede inne"
            else:
                action, reason = "skip", f"gap {gap:+.2f} < 4c"
        elif abs(gap) >= 0.04:
            cheap = "NO" if gap > 0 else "YES"
            action, reason = "buy", f"bekreftelse {cheap} gap {gap:+.2f}"
        logs.append(
            {
                "condition_id": m.get("condition_id"),
                "question": q[:90],
                "ticker": payload["ticker"],
                "title": payload["title"],
                "pm": round(poly, 3),
                "kalshi": payload["yes"],
                "gap": gap,
                "action": action,
                "why": reason,
            }
        )
    log.info("Kalshi: %s treff / %s sammenligninger mot Polymarket", hits, len(logs))
    return hits, logs


def attach(markets: list[dict], kalshi: list[dict] | None = None) -> int:
    n, _ = compare(markets, kalshi)
    return n
