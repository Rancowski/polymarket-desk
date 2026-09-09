"""Kalshi som gratis prissignal. Ingen Kalshi-konto, ingen ordre der."""
from __future__ import annotations

import logging
import re
import time
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
        if re.search(r"25\s*\+|more than 25|>\s*25|25\s*or more", t) or re.search(
            r"(?<![\d.])50\+?\s*(bps|bp|basis)", t
        ):
            tags.add("25plus")
        elif re.search(r"(?<![\d.])25\s*(bps|bp|basis)", t):
            tags.add("25")
        if re.search(r"(?<![\d.])50\+?\s*(bps|bp|basis)", t):
            tags.add("50")
        if re.search(r"\b(cut|decrease|lower|easing)\b", t) or re.search(r"\beast\b", t):
            tags.add("cut")
        if re.search(r"(?<![\d.])0\s*(bps|bp)", t):
            tags.add("hold")
        elif re.search(r"\b(hike|increase|raise|hikes|increases)\b", t):
            tags.add("hike")
        if re.search(r"\b(unchanged|hold|pause)\b", t) or "no change" in t:
            tags.add("hold")
        if re.search(r"above\s+\d", t) or "funds rate" in t or "fed funds" in t:
            tags.add("funds")
    if "bitcoin" in t or re.search(r"\bbtc\b", t):
        tags.add("btc")
    if "ethereum" in t or re.search(r"\beth\b", t):
        tags.add("eth")
    if "solana" in t or re.search(r"\bsol\b", t):
        tags.add("sol")
    if "trump" in t:
        tags.add("trump")
    if "harris" in t:
        tags.add("harris")
    if any(x in t for x in ("election", "electoral", "president", "senate", "congress", "governor", "mayor")):
        tags.add("election")
    if "shutdown" in t:
        tags.add("shutdown")
    if "cpi" in t or "inflation" in t:
        tags.add("cpi")
    if re.search(r"\bgdp\b", t):
        tags.add("gdp")
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


_MON_NUM = {
    "january": "01", "jan": "01", "february": "02", "feb": "02",
    "march": "03", "mar": "03", "april": "04", "apr": "04",
    "may": "05", "june": "06", "jun": "06", "july": "07", "jul": "07",
    "august": "08", "aug": "08", "september": "09", "sep": "09", "sept": "09",
    "october": "10", "oct": "10", "november": "11", "nov": "11",
    "december": "12", "dec": "12",
}


def _dates(text: str) -> set[str]:
    """ISO dates from titles and tickers (2026-09-17, 26SEP17, Sep 17 2026)."""
    t = _human_text(text).lower()
    found: set[str] = set()
    for y, m, d in re.findall(r"(20\d{2})[-/](\d{2})[-/](\d{2})", t):
        found.add(f"{y}-{m}-{d}")
    for yy, mon, dd in re.findall(
        r"(?<![0-9])([12][0-9])(jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)(\d{2})(?![0-9])",
        t,
    ):
        mm = _MON_NUM.get(mon, "00")
        found.add(f"{2000 + int(yy):04d}-{mm}-{dd}")
    for mon, dd, y in re.findall(
        r"(jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)[a-z]*\s+(\d{1,2}),?\s+(20\d{2})",
        t,
    ):
        mm = _MON_NUM.get(mon, "00")
        found.add(f"{y}-{mm}-{int(dd):02d}")
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
    "KXBTCMAX",
    "KXBTCMAXY",
    "KXBTCMAX150",
    "KXETH",
    "KXETHMAX",
    "KXTRUMP",
    "KXHARRIS",
    "KXCPI",
    "KXCPIYOY",
    "KXGPD",
    "KXGDP",
    "KXPAYROLLS",
    "KXRATECUTCOUNT",
    "KXGOVSHUT",
    "KXGOVTSHUTDOWN",
    "KXGOVSHUTLENGTH",
    "CONTROLH",
    "KXSENATE",
    "KXSENATEMID",
    "PRES",
    "KXPRESNOMD",
    "KXPRESNOMR",
    "KXPRESPARTY",
    "POPVOTE",
)

_SERIES_CACHE: tuple[float, list[str]] = (0.0, [])
_SERIES_META: list[dict] = []
_FETCHED_N: dict[str, int] = {}
_TAPE_FREQ = ("fifteen_min", "five_min", "five min", "15m", "5m", "1-minute", "1 min")
_TAPE_TICK = re.compile(r"(15M|5M|1H)$", re.I)


def _rows_to_out(rows: list) -> list[dict]:
    out = []
    for row in rows:
        if str(row.get("market_type") or "binary") not in {"binary", ""}:
            continue
        title = str(row.get("title") or row.get("yes_sub_title") or row.get("subtitle") or "")
        ticker = str(row.get("ticker") or "")
        low = f"{title} {ticker}".lower()
        if any(x in low for x in ("parlay", "combo", "same game", "sgp", "multivariate", "which of the following")):
            continue
        if any(x in low for x in ("15 min", "15-minute", "5 min", "5-minute", "5m ", "15m ", "next 15", "next 5")):
            continue
        if _TAPE_TICK.search(ticker):
            continue
        px = _yes_px(row)
        if not title or not px:
            continue
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
                "dates": _dates(blob),
            }
        )
    return out


def _series_is_tape(row: dict) -> bool:
    tick = str(row.get("ticker") or "")
    freq = str(row.get("frequency") or "").lower()
    title = str(row.get("title") or "").lower()
    cat = str(row.get("category") or "").lower()
    if cat == "sports":
        return True
    if any(x in freq for x in _TAPE_FREQ) or "fifteen" in freq:
        return True
    if _TAPE_TICK.search(tick):
        return True
    if any(x in title for x in ("15 min", "5 min", "15-minute", "5-minute", "up or down", "up/down")):
        return True
    return False


def _discover_series(host: str) -> list[str]:
    """Core Fed/elections/crypto plus high-volume extras. Keep full meta for name lookup."""
    global _SERIES_CACHE, _SERIES_META
    now = time.time()
    ts, cached = _SERIES_CACHE
    if cached and now - ts < 600 and _SERIES_META:
        return cached
    found: list[tuple[float, str]] = []
    seen: set[str] = set(SERIES)
    meta: list[dict] = []
    for cat in ("Politics", "Elections", "Crypto", "Economics"):
        try:
            r = requests.get(
                f"{host}/series",
                params={"category": cat, "include_volume": True},
                timeout=12,
            )
            if not r.ok:
                continue
            for row in r.json().get("series") or []:
                tick = str(row.get("ticker") or "").strip()
                title = str(row.get("title") or "")
                if not tick or _series_is_tape(row):
                    continue
                try:
                    vol = float(row.get("volume_fp") or row.get("volume") or 0)
                except (TypeError, ValueError):
                    vol = 0.0
                meta.append({"ticker": tick, "title": title, "tokens": _tokens(f"{tick} {title}"), "vol": vol})
                if tick in seen:
                    continue
                seen.add(tick)
                found.append((vol, tick))
        except Exception as exc:
            log.warning("Kalshi series %s: %s", cat, exc)
    found.sort(reverse=True)
    extra = [t for _v, t in found[:16]]
    out = list(dict.fromkeys(list(SERIES) + extra))
    _SERIES_META = meta
    _SERIES_CACHE = (now, out)
    log.info("Kalshi serier %s (fed/valg/crypto + %s oppdaget, meta %s)", len(out), len(extra), len(meta))
    return out


def _series_prefix(ticker: str) -> str:
    return str(ticker or "").split("-")[0]


def _pm_family(q: str) -> str:
    t = (q or "").lower()
    if "tweet" in t:
        return "tweets"
    if "fdv" in t or "fully diluted" in t or "one day after launch" in t:
        return "fdv"
    if "hormuz" in t:
        return "hormuz"
    if re.search(r"\bdsa\b", t) or "democratic socialists" in t:
        return "dsa"
    if any(x in t for x in ("fed", "fomc", "federal reserve")):
        return "fed"
    if "lula" in t or ("brazil" in t and "president" in t):
        return "br_pres"
    if "brazil" in t and any(x in t for x in ("senate", "congress", "deput", "chamber", "legislature")):
        return "br_leg"
    if any(x in t for x in ("united russia", "new people", "a just russia", "ldpr", "state duma")):
        return "ru_party"
    if "bitcoin" in t or re.search(r"\bbtc\b", t):
        return "btc"
    if "ethereum" in t or re.search(r"\beth\b", t):
        return "eth"
    if any(x in t for x in ("senate", "house", "congress", "president", "presidential", "electoral")):
        if "brazil" in t:
            return "br_pres" if "president" in t else "br_leg"
        if "russia" in t:
            return "ru_party"
        return "us_election"
    return "other"


def _ticker_family(ticker: str, title: str = "") -> str:
    tick = (ticker or "").upper()
    pfx = tick.split("-")[0]
    if pfx.startswith("KXFED") or "FEDDECISION" in tick or "FEDHIKE" in tick or "FEDFUNDS" in tick or pfx in {"KXFOMC"}:
        return "fed"
    if pfx.startswith("KXBTC"):
        return "btc"
    if pfx.startswith("KXETH"):
        return "eth"
    if "BRSEN" in tick:
        return "br_leg"
    if pfx.startswith("KXBR") and "PRES" in tick:
        return "br_pres"
    if pfx.startswith("KXBR"):
        return "br_other"
    if "TWEET" in tick:
        return "tweets"
    if "HORMUZ" in tick:
        return "hormuz"
    if "FDV" in tick:
        return "fdv"
    if pfx in {"PRES", "CONTROLH", "KXSENATE", "KXSENATEMID", "KXPRESPARTY", "POPVOTE", "KXPRESNOMD", "KXPRESNOMR"}:
        return "us_election"
    if pfx.startswith("SENATE") or pfx.startswith("HOUSE") or pfx.startswith("PRESPARTY") or pfx.startswith("KXPRES"):
        return "us_election"
    return "other"


def _wanted_series(q: str) -> list[str]:
    fam = _pm_family(q)
    t = (q or "").lower()
    if fam == "fed":
        return ["KXFEDDECISION", "KXFED", "KXFEDHIKE", "KXFEDFUNDS", "KXFOMC"]
    if fam == "us_election":
        out: list[str] = []
        if "senate" in t:
            out.extend(["KXSENATE", "KXSENATEMID"])
        if "house" in t or "congress" in t:
            out.append("CONTROLH")
        if "president" in t or "presidential" in t:
            out.extend(["PRES", "KXPRESPARTY"])
        return list(dict.fromkeys(out))
    if fam == "btc":
        return ["KXBTC", "KXBTCMAX", "KXBTCMAXY", "KXBTCMAX150"]
    if fam == "eth":
        return ["KXETH", "KXETHMAX"]
    if fam == "tweets":
        return ["KXELONTWEETS"]
    if fam == "hormuz":
        return ["KXHORMUZNORM"]
    if fam == "br_leg":
        return ["KXBRSENMOSTSEATS"]
    return []


def _outcome_tags(text: str) -> set[str]:
    t = (text or "").lower()
    tags: set[str] = set()
    if re.search(r"(?<![\d.])25\s*(bps|bp|basis)|hike 25|25 bps", t) and "more than 25" not in t and "25+" not in t:
        tags.add("hike25")
    if re.search(r"25\s*\+|more than 25|>\s*25|50\s*(bps|bp)", t):
        tags.add("hike25+")
    if "no change" in t or re.search(r"\b(hold|pause|unchanged)\b", t) or re.search(r"(?<![\d.])0\s*(bps|bp)", t):
        tags.add("hold")
    if re.search(r"\b(cut|cuts|decrease|easing)\b", t):
        tags.add("cut25")
    if "senate" in t:
        tags.add("senate")
    if re.search(r"\bhouse\b", t):
        tags.add("house")
    if re.search(r"\b(republican|gop|r-)\b", t):
        tags.add("gop")
    if re.search(r"\b(democrat|democratic|d-)\b", t):
        tags.add("dem")
    return tags


def _strikes(text: str) -> set[int]:
    """Dollar levels only. Never years (2026) or dates."""
    t = (text or "").lower().replace(",", "")
    found: set[int] = set()
    for n, suf in re.findall(r"\$?\s*(\d+(?:\.\d+)?)\s*(k|m)?", t):
        try:
            v = float(n)
        except ValueError:
            continue
        if suf == "k":
            v *= 1000
        elif suf == "m":
            v *= 1_000_000
        iv = int(round(v))
        if 1900 <= iv <= 2035:
            continue
        if iv < 500:
            continue
        found.add(iv)
    m = re.search(r"-T(\d{3,})$", (text or "").upper())
    if m:
        n = int(m.group(1))
        if n >= 500 and not (1900 <= n <= 2035):
            found.add(n)
    return found


def _md(text: str) -> set[str]:
    t = _human_text(text).lower()
    out: set[str] = set()
    for d in _dates(text):
        out.add(d[5:])
    for mon, dd in re.findall(
        r"(jan|feb|mar|apr|may|jun|jul|aug|sept|sep|oct|nov|dec)[a-z]*\s+(\d{1,2})\b",
        t,
    ):
        mm = _MON_NUM.get(mon)
        if mm:
            out.add(f"{mm}-{int(dd):02d}")
    return out


def _strike_close(a: set[int], b: set[int], tol: float = 0.02) -> bool:
    if not a or not b:
        return False
    for x in a:
        for y in b:
            if x <= 0 or y <= 0:
                continue
            if abs(x - y) / max(x, y) <= tol:
                return True
    return False


def extract_tickers(text: str) -> list[str]:
    found = re.findall(r"\b(KX[A-Z0-9-]{5,}|PRES|CONTROLH|KXSENATE[A-Z0-9-]*)\b", text or "", re.I)
    return [t.upper() for t in found]


def pair_ok(
    pm_q: str,
    ticker: str,
    k_title: str = "",
    k_yes: float | None = None,
    pm_yes: float | None = None,
) -> tuple[bool, str]:
    """Hard gate. Family + strike/date/outcome + live mids. No fuzzy senate→Brazil."""
    tick = str(ticker or "").strip()
    if not tick:
        return False, "ingen ticker"
    pf = _pm_family(pm_q)
    tf = _ticker_family(tick, k_title)
    if pf in {"", "other"} or tf in {"", "other"}:
        return False, f"ulik familie {pf or '—'} vs {tf or '—'} ({tick})"
    if pf != tf:
        return False, f"familie {pf} ≠ {tf} ({tick})"
    if pf == "fed":
        want = _fed_want(pm_q)
        suf = _fed_suffix(tick)
        if not str(tick).upper().startswith("KXFED"):
            return False, f"ikke KXFED* ({tick})"
        if not want or not suf or want != suf:
            return False, f"fed {suf or '—'} ≠ {want or '—'}"
    if pf in {"btc", "eth"}:
        q_strike = _strikes(pm_q)
        k_strike = _strikes(f"{tick} {k_title}")
        if not _strike_close(q_strike, k_strike, 0.02):
            return False, f"strike mismatch {sorted(q_strike)[:3]} vs {sorted(k_strike)[:3]}"
        qmd, kmd = _md(pm_q), _md(f"{tick} {k_title}")
        if qmd and kmd and not (qmd & kmd):
            return False, "ulik session-dato"
    if k_yes is not None and pm_yes is not None:
        try:
            ky = float(k_yes)
            py = float(pm_yes)
        except (TypeError, ValueError):
            return False, "ugyldig mid"
        pinned = (ky <= 0.01 or ky >= 0.99) and (py <= 0.01 or py >= 0.99)
        if not pinned and not (0.01 < ky < 0.99 and 0.01 < py < 0.99):
            return False, f"mid utenfor (0.01,0.99) k={ky:.3f} pm={py:.3f}"
    return True, ""


def keep_fed_h25(pm_q: str, ticker: str) -> bool:
    tick = str(ticker or "").upper()
    return (
        _pm_family(pm_q) == "fed"
        and "KXFEDDECISION" in tick
        and _fed_suffix(tick) == "H25"
        and _fed_want(pm_q) == "H25"
    )


def _fetch_one_series(host: str, series: str, collected: list[dict], seen: set[str], pages: int = 2) -> int:
    n = 0
    cursor = None
    try:
        for _page in range(pages):
            params: dict[str, Any] = {"limit": 200, "status": "open", "series_ticker": series}
            if cursor:
                params["cursor"] = cursor
            r = requests.get(f"{host}/markets", params=params, timeout=12)
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
                n += 1
                _FETCHED_N[series] = _FETCHED_N.get(series, 0) + 1
            log.info("Kalshi %s: %s rader, %s pris", series, len(rows), len(chunk))
            cursor = data.get("cursor") or data.get("next_cursor") or ""
            if not rows or not str(cursor).strip() or len(rows) < 200:
                break
    except Exception as exc:
        log.warning("Kalshi %s: %s", series, exc)
    return n


def expand_catalog(collected: list[dict], markets: list[dict], host: str | None = None) -> list[dict]:
    """Fetch extra series that match open PM titles (Senate/House/Lula/Musk/…)."""
    if not markets:
        return collected
    from agent.risk import is_sports

    host = host or HOSTS[0]
    seen = {str(i.get("ticker") or i.get("title")) for i in collected}
    have = {_series_prefix(str(i.get("ticker") or "")) for i in collected}
    want: list[str] = []
    for m in markets:
        if is_sports(m):
            continue
        q = str(m.get("question") or "")
        want.extend(_wanted_series(q))
    extra = []
    for tick in dict.fromkeys(want):
        if tick in have:
            continue
        extra.append(tick)
        if len(extra) >= 12:
            break
    added = 0
    for series in extra:
        added += _fetch_one_series(host, series, collected, seen, pages=2)
        have.add(series)
    if extra:
        log.info("Kalshi extra lookup %s serier, +%s kontrakter", extra, added)
    return collected


def fetch_open(limit: int = 400) -> list[dict]:
    global _FETCHED_N
    _FETCHED_N = {}
    last_exc: Exception | None = None
    collected: list[dict] = []
    seen: set[str] = set()
    for host in HOSTS:
        host_n = 0
        series_list = _discover_series(host)
        for series in series_list:
            cursor = None
            series_n = 0
            try:
                pages = 6 if str(series).startswith("KXFED") else 2
                for _page in range(pages):
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
                        _FETCHED_N[str(series)] = _FETCHED_N.get(str(series), 0) + 1
                    log.info("Kalshi %s: %s rader, %s pris", series, len(rows), len(chunk))
                    cursor = data.get("cursor") or data.get("next_cursor") or ""
                    if not rows or not str(cursor).strip() or len(rows) < 200:
                        break
            except Exception as ext:
                last_exc = ext
                log.warning("Kalshi %s: %s", series, ext)
        if host_n:
            break
    log.info("Kalshi totalt %s markeder med pris (politikk/crypto/fed)", len(collected))
    if not collected and last_exc:
        log.warning("Kalshi-henting feilet: %s", last_exc)
    return collected[:limit]


_FED_SUFFIX_RE = re.compile(r"-(H26|H25|H0|C26|C25)(?:\b|$|-)", re.I)
_FED_WANT_LABEL = {
    "H25": "hike25",
    "H26": "hike25+",
    "H0": "hold",
    "C25": "cut25",
    "C26": "cut25+",
}


def _fed_suffix(ticker: str) -> str | None:
    m = _FED_SUFFIX_RE.search(str(ticker or "").upper())
    return m.group(1) if m else None


def _fed_want(text: str) -> str | None:
    """Map PM Fed text to Kalshi decision suffix. H26 is >25bps, not hike 25."""
    t = (text or "").lower()
    if not any(x in t for x in ("fed", "fomc", "federal reserve")):
        return None
    plus = bool(
        re.search(
            r"25\s*\+|50\s*\+|more than 25|>\s*25|>\s*50|25\s*or more|50\s*or more|"
            r"(?<![\d.])50\+?\s*(bps|bp)|(?<![\d.])50\b",
            t,
        )
    )
    has25 = bool(re.search(r"(?<![\d.])25(\s*\+)?\s*(bps|bp|basis)|(?<![\d.])25\b", t))
    is_cut = bool(re.search(r"\b(cut|cuts|decrease|lower|easing)\b", t))
    is_hike = bool(re.search(r"\b(hike|hikes|increase|increases|raise|raises)\b", t))
    is_hold = (
        "no change" in t
        or "unchanged" in t
        or bool(re.search(r"\b(hold|pause)\b", t))
        or bool(re.search(r"(?<![\d.])0\s*(bps|bp)", t))
    )
    if is_hold and not (is_cut or (is_hike and has25 and not re.search(r"(?<![\d.])0\s*(bps|bp)", t))):
        return "H0"
    if is_cut:
        return "C26" if plus else "C25"
    if is_hike:
        return "H26" if plus else "H25"
    if is_hold:
        return "H0"
    return None


def _pick(q: str, kalshi: list[dict]) -> tuple[dict | None, str]:
    """Attach only a hard-legal pair. No fuzzy token score."""
    fam = _pm_family(q)
    wanted = _wanted_series(q)
    last_why = "ingen lovlig par"
    best = None
    best_score = -1.0
    fed_wrong: list[tuple[str, str, str]] = []
    for k in kalshi:
        ticker = str(k.get("ticker") or "")
        title = str(k.get("title") or "")
        try:
            k_yes = float(k.get("yes") or 0)
        except (TypeError, ValueError):
            k_yes = 0.0
        ok, why = pair_ok(q, ticker, title, k_yes if k_yes else None, None)
        if not ok:
            last_why = why
            if fam == "fed" and _fed_suffix(ticker) and _fed_want(q) and _fed_suffix(ticker) != _fed_want(q):
                fed_wrong.append((_fed_suffix(ticker) or "", ticker, title))
            continue
        score = 1.0
        if fam in {"btc", "eth"}:
            qs, ks = _strikes(q), _strikes(f"{ticker} {title}")
            if qs and ks:
                dist = min(abs(x - y) for x in qs for y in ks)
                score = 50.0 - dist / 1000.0
        if fam == "fed" and _fed_suffix(ticker) == _fed_want(q):
            score += 20
        qmd, kmd = _md(q), _md(f"{ticker} {title}")
        if qmd and kmd and (qmd & kmd):
            score += 5
        if score > best_score:
            best_score = score
            best = k
    if not best:
        if fam == "fed" and fed_wrong:
            suf, tick, title = fed_wrong[0]
            label = _FED_WANT_LABEL.get(_fed_want(q) or "", _fed_want(q) or "")
            why = f"wrong ticker {suf} ≠ {label}"
            log.info("kalshi-skip %s | %s | %s", tick, title[:70], why)
            return {"title": title[:90], "ticker": tick, "yes": 0.0, "_skip": True}, why
        labels = wanted or [fam]
        counts = [f"{s}:{_FETCHED_N.get(s, 0)}" for s in labels[:6]]
        return None, f"{last_why} ({fam}; {' '.join(counts)})"
    best = {**best, "overlap": int(best_score)}
    if fam == "fed":
        best["fed_suffix"] = _fed_want(q)
    return best, ""


def _named_target(m: dict) -> bool:
    if m.get("_open_only"):
        return True
    cat = str(m.get("category") or "").lower()
    if cat in {"economics", "finance", "crypto", "politics", "geopolitics"}:
        return True
    blob = f"{m.get('question') or ''} {m.get('event_key') or ''}".lower()
    return any(
        x in blob
        for x in (
            "fed", "fomc", "bitcoin", "btc", "ethereum", "eth", "solana", "trump",
            "election", "senate", "congress", "president", "governor", "mayor",
            "shutdown", "cpi", "inflation", "gdp", "payroll", "harris", "nominee",
            "lula", "musk", "senate", "house",
        )
    )


def compare(markets: list[dict], kalshi: list[dict] | None = None) -> tuple[int, list[dict]]:
    """Attach Kalshi and return (hits, compare-log). Log every named-series attempt."""
    from agent.risk import is_sports

    if kalshi is None:
        kalshi = fetch_open()
    kalshi = expand_catalog(kalshi or [], markets)
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
        if best and best.get("_skip"):
            if want:
                logs.append(
                    {
                        "condition_id": m.get("condition_id"),
                        "question": q[:90],
                        "ticker": best.get("ticker") or "",
                        "title": best.get("title") or "",
                        "pm": round(poly, 3),
                        "kalshi": None,
                        "gap": None,
                        "action": "skip",
                        "why": why,
                    }
                )
            continue
        k_yes = float(best["yes"]) if best else 0.0
        if best and not best.get("_skip"):
            ok, why2 = pair_ok(blob, str(best.get("ticker") or ""), str(best.get("title") or ""), k_yes, poly)
            if not ok:
                if want:
                    logs.append(
                        {
                            "condition_id": m.get("condition_id"),
                            "question": q[:90],
                            "ticker": "",
                            "pm": round(poly, 3),
                            "pm_yes": round(poly, 3),
                            "kalshi": None,
                            "kalshi_yes": None,
                            "gap": None,
                            "action": "skip",
                            "why": why2,
                        }
                    )
                continue
        if not best:
            if want:
                logs.append(
                    {
                        "condition_id": m.get("condition_id"),
                        "question": q[:90],
                        "ticker": "",
                        "pm": round(poly, 3),
                        "pm_yes": round(poly, 3),
                        "kalshi": None,
                        "kalshi_yes": None,
                        "gap": None,
                        "action": "skip",
                        "why": why,
                    }
                )
            continue
        side = str(m.get("side") or "").upper()
        held = 0.0
        try:
            held = float(m.get("cur_price") or m.get("avg_cost") or 0)
        except (TypeError, ValueError):
            held = 0.0
        # Always compare Kalshi to the OPEN side. NO → 1 - kalshi_yes. Never YES vs our NO.
        if side == "NO":
            k_hat = 1.0 - k_yes
            pm_hat = held if 0 < held < 1 else (1.0 - poly if 0 < poly < 1 else poly)
        elif side == "YES":
            k_hat = k_yes
            pm_hat = held if 0 < held < 1 else poly
        else:
            k_hat = k_yes
            pm_hat = poly
        gap = round(pm_hat - k_hat, 3)
        gap_yes = round(poly - k_yes, 3) if poly and k_yes else gap
        payload = {
            "title": str(best["title"])[:90],
            "ticker": best.get("ticker"),
            "yes": round(k_yes, 3),
            "pm_yes": round(poly, 3),
            "gap_yes": gap_yes,
            "side": side or "YES",
            "side_px": round(k_hat, 3),
            "gap": gap,
            "overlap": int(best.get("overlap") or 0),
        }
        m["kalshi"] = payload
        hits += 1
        side_lab = side or "YES"
        action, reason = "skip", f"PM {side_lab} {pm_hat:.2f} Kalshi {side_lab} {k_hat:.2f} gap {gap:+.2f}"
        avg = float(m.get("avg_cost") or 0)
        if avg > 0 and side in {"YES", "NO"}:
            if k_hat <= pm_hat - 0.07 or k_hat + 0.07 < avg or (k_hat < 0.02 and pm_hat > 0.40):
                action, reason = "sell", f"Kalshi {side_lab} {k_hat:.2f} vs our {side_lab} {pm_hat:.2f} (≥7c mot)"
            elif k_hat >= pm_hat + 0.05:
                action, reason = "buy", f"Kalshi {side_lab} {k_hat:.2f} ≥ our {side_lab} {pm_hat:.2f}+5c"
            elif abs(gap) >= 0.04:
                action, reason = "skip", "allerede inne"
        elif k_hat >= pm_hat + 0.05:
            action, reason = "buy", f"bekreftelse {side_lab} gap {gap:+.2f}"
        elif pm_hat >= k_hat + 0.05:
            cheap = "NO" if not side else ("YES" if side == "NO" else "NO")
            action, reason = "buy", f"bekreftelse {cheap} gap {gap:+.2f}"
        logs.append(
            {
                "condition_id": m.get("condition_id"),
                "question": q[:90],
                "ticker": payload["ticker"],
                "title": payload["title"],
                "pm": round(poly, 3),
                "pm_yes": round(poly, 3),
                "kalshi": round(k_yes, 3),
                "kalshi_yes": round(k_yes, 3),
                "gap": gap_yes,
                "side_gap": gap,
                "action": action,
                "why": reason,
            }
        )
    log.info("Kalshi: %s treff / %s sammenligninger mot Polymarket", hits, len(logs))
    return hits, logs


def attach(markets: list[dict], kalshi: list[dict] | None = None) -> int:
    n, _ = compare(markets, kalshi)
    return n
