from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.config import settings

log = logging.getLogger("store")

BUY_SOURCES = frozenset(
    {"grok", "kalshi", "complement", "partition", "maker", "stats", "tape"}
)


def _real_buy_source(src: Any, detail: Any = None) -> str | None:
    """Opening-fill source. Partition only if the fill itself was a partition ticket."""
    got = normalize_source(src)
    if got not in BUY_SOURCES:
        return None
    if got == "partition" and not str(detail or "").lower().startswith("partition "):
        return None
    return got


VALID_SOURCES = frozenset(
    {
        "grok",
        "kalshi",
        "complement",
        "stats",
        "partition",
        "maker",
        "tape",
        "exit_stop",
        "exit_take",
        "exit_trail",
        "exit_kalshi",
        "redeem",
        "resolve",
        "flatten",
    }
)

_SIDE_MAP = {
    "YES": "BUY_YES",
    "NO": "BUY_NO",
    "BUY": "BUY_YES",
    "BUY_YES": "BUY_YES",
    "BUY_NO": "BUY_NO",
    "SELL": "SELL_YES",
    "SELL_YES": "SELL_YES",
    "SELL_NO": "SELL_NO",
    "REDEEM": "REDEEM",
    "REDEEM_YES": "REDEEM_YES",
    "REDEEM_NO": "REDEEM_NO",
    "RESOLVE": "RESOLVE",
    "RESOLVE_YES": "RESOLVE_YES",
    "RESOLVE_NO": "RESOLVE_NO",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_ts(raw: Any) -> str | None:
    """Normalize a timestamp to UTC isoformat. None if unusable."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            v = float(raw)
            if v > 1e12:
                v /= 1000.0
            if v > 1e9:
                return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return None
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.isoformat()
    except ValueError:
        return None


def is_open_seat(p: dict | None) -> bool:
    """Shown seat: size>0 (not ≈0) and mid>0.01. Ghosts are not seats."""
    if not p:
        return False
    try:
        shares = float(p.get("shares") or p.get("size") or 0)
    except (TypeError, ValueError):
        return False
    if shares <= 1e-6:
        return False
    try:
        mid = float(
            p.get("cur_price")
            or p.get("curPrice")
            or p.get("currPrice")
            or p.get("current_price")
            or 0
        )
    except (TypeError, ValueError):
        mid = 0.0
    return mid > 0.01


def normalize_side(side: Any) -> str:
    s = str(side or "").upper().replace(" ", "_")
    return _SIDE_MAP.get(s, s)


def normalize_source(src: Any) -> str | None:
    if src is None:
        return None
    s = str(src).strip().lower()
    if not s or s in {"ukjent", "unknown", "none", "null"}:
        return None
    return s if s in VALID_SOURCES else None


def _raw_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {"_text": raw}
    return {}


def _num_ok(raw: Any) -> bool:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return False
    return 0.0 < v < 1.0


def kalshi_fields_ok(
    ticker: Any = None,
    kalshi_mid: Any = None,
    pm_mid: Any = None,
    raw: Any = None,
) -> bool:
    data = _raw_dict(raw) if raw is not None else {}
    tick = str(ticker or data.get("kalshi_ticker") or data.get("ticker") or "").strip()
    if not tick or tick in {"—", "-", "none", "null"}:
        return False
    kmid = kalshi_mid if kalshi_mid is not None else data.get("kalshi_mid")
    pmid = pm_mid if pm_mid is not None else data.get("pm_mid")
    return _num_ok(kmid) and _num_ok(pmid)


def infer_fill_source(side: Any, raw: Any) -> tuple[str | None, str | None]:
    """Evidence from raw/thesis only. source=kalshi only with ticker + both mids."""
    data = _raw_dict(raw)
    thesis = str(
        data.get("source_detail") or data.get("thesis") or data.get("reason") or ""
    ).strip()[:160]
    explicit = normalize_source(data.get("source"))
    su = normalize_side(side)
    low = thesis.lower()
    if explicit == "kalshi":
        tick = data.get("kalshi_ticker") or data.get("ticker")
        if not kalshi_fields_ok(tick, data.get("kalshi_mid"), data.get("pm_mid"), data):
            explicit = None
        else:
            q = data.get("question")
            if not q:
                explicit = None
            else:
                from agent.kalshi import pair_ok

                ok, _why = pair_ok(
                    str(q),
                    str(tick),
                    str(data.get("title") or ""),
                    data.get("kalshi_mid"),
                    data.get("pm_mid"),
                )
                if not ok:
                    explicit = None
    if explicit:
        return explicit, thesis or None
    if su.startswith("REDEEM") or data.get("redeem"):
        return "redeem", thesis or "redeem_ok"
    if su.startswith("RESOLVE") or data.get("resolve"):
        return "resolve", thesis or "resolved"
    sell = su.startswith("SELL")
    if sell:
        if "kalshi" in low:
            return "exit_kalshi", thesis or None
        if "trail" in low:
            return "exit_trail", thesis or None
        if "stopp" in low or "stop-tap" in low or "kamp >3t" in low:
            return "exit_stop", thesis or None
        if "ta gevinst" in low or low.startswith("ta ") or " ≥0.98" in low:
            return "exit_take", thesis or None
        if "flatten" in low or "hedge-rollback" in low or "død sports" in low or "trim" in low:
            return "flatten", thesis or None
        return None, thesis or None
    if "sum-til-én" in low or "event-sett" in low or low.startswith("complement "):
        return "complement", thesis or None
    if "favorite_near" in low or low.startswith("stats "):
        return "stats", thesis or None
    if low.startswith("maker "):
        return "maker", thesis or None
    if "låst utfall" in low:
        return "tape", thesis or None
    if kalshi_fields_ok(raw=data) and data.get("question") and (
        "kalshi-bekreftelse" in low or data.get("kalshi_ticker")
    ):
        from agent.kalshi import pair_ok

        ok, _why = pair_ok(
            str(data.get("question")),
            str(data.get("kalshi_ticker") or data.get("ticker") or ""),
            str(data.get("title") or ""),
            data.get("kalshi_mid"),
            data.get("pm_mid"),
        )
        if ok:
            return "kalshi", thesis or None
    if " | kalshi " in low or low.startswith("grok ") or "edge_net=" in low or "no kalshi" in low:
        return "grok", thesis or None
    if "kalshi" in low:
        return "grok" if (data.get("grok_p") not in (None, "") or "p=" in low) else None, thesis or None
    return None, thesis or None


class Store:
    def __init__(self, path: Path | None = None) -> None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = path or settings.data_dir / "desk.db"
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()
        self.backfill_resolved_closes()
        self.backfill_resolve_timestamps()

    def _init(self) -> None:
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    condition_id TEXT,
                    question TEXT,
                    side TEXT,
                    mid REAL,
                    p_hat REAL,
                    edge_net REAL,
                    action TEXT,
                    reason TEXT,
                    payload TEXT
                );
                CREATE TABLE IF NOT EXISTS positions (
                    condition_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    question TEXT,
                    category TEXT,
                    event_key TEXT,
                    token_id TEXT,
                    shares REAL,
                    avg_cost REAL,
                    opened_ts TEXT,
                    last_ts TEXT,
                    status TEXT,
                    PRIMARY KEY (condition_id, side)
                );
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    condition_id TEXT,
                    side TEXT,
                    price REAL,
                    size REAL,
                    cost REAL,
                    dry_run INTEGER,
                    raw TEXT
                );
                CREATE TABLE IF NOT EXISTS pnl_marks (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    bankroll REAL,
                    equity REAL
                );
                CREATE TABLE IF NOT EXISTS api_costs (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    usd REAL,
                    model TEXT,
                    tokens INTEGER
                );
                CREATE TABLE IF NOT EXISTS meta (
                    k TEXT PRIMARY KEY,
                    v TEXT
                );
                """
            )
            self.conn.commit()
            self._migrate_positions()
            self._ensure_column("positions", "current_value", "REAL")
            self._ensure_column("positions", "cur_price", "REAL")
            self._ensure_column("positions", "outcome", "TEXT")
            self._ensure_column("positions", "entry_source", "TEXT")
            self._ensure_column("positions", "entry_detail", "TEXT")
            self._migrate_fills()
            self._purge_ghost_fills()

    def _migrate_positions(self) -> None:
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='positions'"
        ).fetchone()
        sql = (row["sql"] if row else "") or ""
        if "PRIMARY KEY (condition_id, side)" in sql.replace(" ", ""):
            return
        if "PRIMARY KEY(condition_id, side)" in sql.replace(" ", ""):
            return
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS positions_v2 (
                condition_id TEXT NOT NULL,
                side TEXT NOT NULL,
                question TEXT,
                category TEXT,
                event_key TEXT,
                token_id TEXT,
                shares REAL,
                avg_cost REAL,
                opened_ts TEXT,
                last_ts TEXT,
                status TEXT,
                PRIMARY KEY (condition_id, side)
            );
            INSERT OR REPLACE INTO positions_v2
                (condition_id, side, question, category, event_key, token_id, shares, avg_cost, opened_ts, last_ts, status)
            SELECT condition_id, COALESCE(NULLIF(side,''),'YES'), question, category, event_key, token_id,
                   shares, avg_cost, opened_ts, last_ts, status FROM positions;
            DROP TABLE positions;
            ALTER TABLE positions_v2 RENAME TO positions;
            """
        )
        self.conn.commit()

    def _ensure_column(self, table: str, col: str, decl: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {str(r[1]) for r in rows}
        if col not in names:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            self.conn.commit()

    def _migrate_fills(self) -> None:
        for col, decl in (
            ("question", "TEXT"),
            ("token_id", "TEXT"),
            ("source", "TEXT"),
            ("source_detail", "TEXT"),
            ("grok_p", "REAL"),
            ("grok_conf", "TEXT"),
            ("edge_net", "REAL"),
            ("kalshi_ticker", "TEXT"),
            ("kalshi_mid", "REAL"),
            ("pm_mid", "REAL"),
            ("gap_c", "REAL"),
            ("cycle_id", "TEXT"),
        ):
            self._ensure_column("fills", col, decl)
        self._backfill_fill_attribution()
        self._backfill_position_entry()

    def _backfill_fill_attribution(self) -> None:
        try:
            cur = self.conn.execute(
                """
                SELECT id, side, raw, source, source_detail, question, token_id,
                       kalshi_ticker, kalshi_mid, pm_mid FROM fills
                """
            )
            for row in cur.fetchall():
                data = _raw_dict(row["raw"])
                detail = (row["source_detail"] or "").strip() or None
                question = (row["question"] or "").strip() or None
                token = (row["token_id"] or "").strip() or None
                tick = row["kalshi_ticker"] or data.get("kalshi_ticker") or data.get("ticker")
                kmid = row["kalshi_mid"] if row["kalshi_mid"] is not None else data.get("kalshi_mid")
                pmid = row["pm_mid"] if row["pm_mid"] is not None else data.get("pm_mid")
                src = normalize_source(row["source"])
                if src == "kalshi" and not kalshi_fields_ok(tick, kmid, pmid, data):
                    src = None
                inf, inf_detail = infer_fill_source(
                    row["side"],
                    {**data, "source": src, "source_detail": detail, "kalshi_ticker": tick, "kalshi_mid": kmid, "pm_mid": pmid},
                )
                if not src:
                    src = inf
                if src == "kalshi" and not kalshi_fields_ok(tick, kmid, pmid, data):
                    src = inf if inf and inf != "kalshi" else None
                if not detail:
                    detail = inf_detail
                if not question:
                    q = data.get("question")
                    question = str(q).strip() if q else None
                if not token:
                    t = data.get("token_id")
                    token = str(t).strip() if t else None
                if src != normalize_source(row["source"]) or detail or question or token:
                    self.conn.execute(
                        """
                        UPDATE fills SET
                            source=?,
                            source_detail=COALESCE(NULLIF(source_detail,''), ?),
                            question=COALESCE(NULLIF(question,''), ?),
                            token_id=COALESCE(NULLIF(token_id,''), ?)
                        WHERE id=?
                        """,
                        (src, detail, question, token, row["id"]),
                    )
            self.conn.commit()
        except Exception:
            pass

    def _backfill_position_entry(self) -> None:
        try:
            cur = self.conn.execute(
                """
                SELECT condition_id, side, source, source_detail
                FROM fills
                WHERE source IS NOT NULL AND source != ''
                ORDER BY id ASC
                """
            )
            first: dict[tuple[str, str], tuple[str, str | None]] = {}
            for row in cur.fetchall():
                su = normalize_side(row["side"])
                if not su.startswith("BUY_"):
                    continue
                src = _real_buy_source(row["source"], row["source_detail"])
                if not src:
                    continue
                yn = su[4:] or "YES"
                key = (str(row["condition_id"] or ""), yn)
                if key[0] and key not in first:
                    first[key] = (src, row["source_detail"])
            self.conn.execute("UPDATE positions SET entry_source=''")
            for (cid, side), (src, detail) in first.items():
                self.conn.execute(
                    """
                    UPDATE positions SET
                        entry_source=?,
                        entry_detail=COALESCE(NULLIF(?, ''), entry_detail)
                    WHERE condition_id=? AND UPPER(COALESCE(side,'YES'))=?
                    """,
                    (src, detail, cid, side),
                )
            self.conn.commit()
        except Exception:
            pass

    def _raw_is_matched(self, raw: Any) -> bool:
        data: Any = raw
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"_text": raw}
        if not isinstance(data, dict):
            data = {}
        order = data.get("order")
        if isinstance(order, dict):
            merged = {**data, **order}
        else:
            merged = dict(data)
            if isinstance(order, str):
                merged["_text"] = (merged.get("_text") or "") + " " + order
        status = str(merged.get("status") or "").lower()
        taking = str(merged.get("takingAmount") if merged.get("takingAmount") is not None else "").strip()
        making = str(merged.get("makingAmount") if merged.get("makingAmount") is not None else "").strip()
        empty = {"", "0", "0.0", "none", "null"}
        if merged.get("closed_dust"):
            return False
        if merged.get("redeem") or merged.get("resolve"):
            return True
        if status in {"live", "open", "resting", "unmatched", "cancelled", "canceled"} and taking.lower() in empty and making.lower() in empty:
            return False
        if status in {"matched", "filled"}:
            try:
                if float(taking or 0) > 0 or float(making or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
            if merged.get("redeem") or merged.get("resolve"):
                return True
            return False
        try:
            if float(taking) > 0 or float(making) > 0:
                return True
        except (TypeError, ValueError):
            pass
        text = str(merged.get("_text") or json.dumps(merged, default=str)).lower()
        if "'status': 'live'" in text or '"status": "live"' in text or '"status":"live"' in text:
            return False
        if any(x in text for x in ("'matched'", '"matched"', "'filled'", '"filled"')):
            return True
        return False

    def _purge_ghost_fills(self) -> None:
        try:
            cur = self.conn.execute("SELECT id, raw, dry_run FROM fills")
            drop: list[int] = []
            for row in cur.fetchall():
                if int(row["dry_run"] or 0) == 1:
                    continue
                if not self._raw_is_matched(row["raw"]):
                    drop.append(int(row["id"]))
            for fid in drop:
                self.conn.execute("DELETE FROM fills WHERE id=?", (fid,))
            if drop:
                self.conn.commit()
        except Exception:
            pass

    def float_meta(self, key: str) -> float | None:
        raw = self.get_meta(key, "")
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        return val

    def save_snapshot(self, cash: float, equity: float, mtm: float = 0.0) -> None:
        self.set_meta("last_cash", f"{cash:.4f}")
        self.set_meta("last_equity", f"{equity:.4f}")
        self.set_meta("last_mtm", f"{mtm:.4f}")
        self.set_meta("desk_cash", f"{cash:.4f}")
        self.set_meta("desk_equity", f"{equity:.4f}")

    def log_decision(self, **row: Any) -> None:
        payload = row.pop("payload", {})
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO decisions (ts, condition_id, question, side, mid, p_hat, edge_net, action, reason, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    row.get("condition_id"),
                    row.get("question"),
                    row.get("side"),
                    row.get("mid"),
                    row.get("p_hat"),
                    row.get("edge_net"),
                    row.get("action"),
                    row.get("reason"),
                    json.dumps(payload, default=str),
                ),
            )
            self.conn.commit()

    def upsert_position(self, **row: Any) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO positions (condition_id, question, category, event_key, side, token_id, shares, avg_cost, current_value, cur_price, outcome, opened_ts, last_ts, status, entry_source, entry_detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(condition_id, side) DO UPDATE SET
                    shares=excluded.shares,
                    avg_cost=excluded.avg_cost,
                    current_value=excluded.current_value,
                    cur_price=excluded.cur_price,
                    outcome=COALESCE(excluded.outcome, positions.outcome),
                    last_ts=excluded.last_ts,
                    status=excluded.status,
                    token_id=excluded.token_id,
                    question=COALESCE(excluded.question, positions.question),
                    category=COALESCE(excluded.category, positions.category),
                    entry_source=COALESCE(NULLIF(positions.entry_source,''), excluded.entry_source),
                    entry_detail=COALESCE(NULLIF(positions.entry_detail,''), excluded.entry_detail)
                """,
                (
                    row["condition_id"],
                    row.get("question"),
                    row.get("category"),
                    row.get("event_key"),
                    row.get("side"),
                    row.get("token_id"),
                    row.get("shares", 0),
                    row.get("avg_cost", 0),
                    row.get("current_value"),
                    row.get("cur_price"),
                    row.get("outcome"),
                    row.get("opened_ts", utc_now()),
                    utc_now(),
                    row.get("status", "open"),
                    row.get("entry_source"),
                    row.get("entry_detail"),
                ),
            )
            self.conn.commit()

    def positions(self, status: str = "open") -> list[dict]:
        with self._lock:
            cur = self.conn.execute("SELECT * FROM positions WHERE status=?", (status,))
            return [dict(r) for r in cur.fetchall()]

    def opening_source(self, condition_id: str, side: str | None = None) -> str | None:
        """Source of the first BUY fill. Never invent partition."""
        cid = str(condition_id or "").strip()
        yn = str(side or "YES").upper()
        if not cid:
            return None
        with self._lock:
            cur = self.conn.execute(
                "SELECT side, source, source_detail FROM fills WHERE condition_id=? ORDER BY id ASC",
                (cid,),
            )
            rows = cur.fetchall()
        for row in rows:
            su = normalize_side(row["side"])
            if not su.startswith("BUY_"):
                continue
            if (su[4:] or "YES") != yn:
                continue
            src = _real_buy_source(row["source"], row["source_detail"])
            if src:
                return src
        return None

    def has_close_fill(self, condition_id: str, side: str | None = None) -> bool:
        cid = str(condition_id or "").strip()
        yn = str(side or "YES").upper()
        if not cid:
            return False
        with self._lock:
            cur = self.conn.execute(
                "SELECT side, raw, dry_run FROM fills WHERE condition_id=?",
                (cid,),
            )
            rows = cur.fetchall()
        for row in rows:
            su = normalize_side(row["side"])
            if su in {f"RESOLVE_{yn}", "RESOLVE", f"REDEEM_{yn}", "REDEEM"}:
                return True
            if su == f"SELL_{yn}" or (su == "SELL" and yn == "YES"):
                if int(row["dry_run"] or 0) == 0 and self._raw_is_matched(row["raw"]):
                    return True
        return False

    def _leg_has_live_buy(self, condition_id: str, side: str | None) -> bool:
        cid = str(condition_id or "").strip()
        yn = str(side or "YES").upper()
        with self._lock:
            cur = self.conn.execute(
                "SELECT side, dry_run FROM fills WHERE condition_id=?",
                (cid,),
            )
            rows = cur.fetchall()
        for row in rows:
            su = normalize_side(row["side"])
            if su.startswith("BUY_") and (su[4:] or "YES") == yn and int(row["dry_run"] or 0) == 0:
                return True
        return False

    def record_resolution(self, pos: dict, *, source: str, proceeds: float) -> bool:
        """One close fill for a vanished or worthless seat. proceeds=0 if worthless."""
        cid = str(pos.get("condition_id") or "").strip()
        yn = str(pos.get("side") or "YES").upper()
        if yn not in {"YES", "NO"}:
            yn = "YES"
        if not cid or self.has_close_fill(cid, yn):
            return False
        try:
            shares = float(pos.get("shares") or 0)
        except (TypeError, ValueError):
            shares = 0.0
        try:
            avg = float(pos.get("avg_cost") or 0)
        except (TypeError, ValueError):
            avg = 0.0
        if shares <= 0 and avg <= 0:
            return False
        proceeds_f = max(0.0, float(proceeds or 0))
        px = (proceeds_f / shares) if shares > 0 else 0.0
        src = "redeem" if source == "redeem" else "resolve"
        side = f"REDEEM_{yn}" if src == "redeem" else f"RESOLVE_{yn}"
        paper = not self._leg_has_live_buy(cid, yn)
        closed_at = self._resolution_ts(pos)
        if not closed_at:
            log.warning(
                "resolve fill skip now() stamp %s %s",
                yn,
                str(pos.get("question") or cid)[:60],
            )
            return False
        self.add_fill(
            condition_id=cid,
            side=side,
            price=round(px, 4),
            size=shares,
            cost=round(proceeds_f, 4),
            dry_run=paper,
            question=pos.get("question"),
            token_id=pos.get("token_id"),
            source=src,
            source_detail="worthless" if proceeds_f <= 0 else src,
            cycle_id=self.get_meta("cycle_id") or None,
            ts=closed_at,
            raw={
                src: True,
                "status": "matched",
                "takingAmount": str(proceeds_f),
                "question": pos.get("question"),
                "source": src,
                "closed_at": closed_at,
            },
        )
        log.info(
            "%s fill %s %s shares=%.4f proceeds=%.2f cost=%.2f",
            src,
            yn,
            str(pos.get("question") or "")[:60],
            shares,
            proceeds_f,
            shares * avg,
        )
        return True

    def backfill_resolved_closes(self) -> int:
        """Import vanished losers still sitting as closed/closed_dust without a close fill."""
        if self.get_meta("resolve_backfill_v1", ""):
            return 0
        n = 0
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM positions WHERE status IN ('closed', 'closed_dust')"
            )
            rows = [dict(r) for r in cur.fetchall()]
        for row in rows:
            if self.record_resolution(row, source="resolve", proceeds=0.0):
                n += 1
        self.set_meta("resolve_backfill_v1", utc_now())
        if n:
            log.info("backfill resolved closes: %s", n)
        return n

    def _first_buy_ts(self, condition_id: str, side: str | None) -> str | None:
        cid = str(condition_id or "").strip()
        yn = str(side or "YES").upper()
        if yn.startswith("BUY_"):
            yn = yn[4:]
        if not cid:
            return None
        with self._lock:
            cur = self.conn.execute(
                "SELECT ts, side FROM fills WHERE condition_id=? ORDER BY id ASC",
                (cid,),
            )
            rows = cur.fetchall()
            pos = self.conn.execute(
                "SELECT opened_ts, last_ts FROM positions WHERE condition_id=? AND UPPER(COALESCE(side,'YES'))=?",
                (cid, yn),
            ).fetchone()
        for row in rows:
            su = normalize_side(row["side"])
            if su.startswith("BUY_") and (su[4:] or "YES") == yn:
                got = _parse_iso_ts(row["ts"])
                if got:
                    return got
        if pos:
            for key in ("opened_ts", "last_ts"):
                got = _parse_iso_ts(pos[key])
                if got:
                    return got
        return None

    def _resolution_ts(self, pos: dict) -> str:
        """closed_at from data-api or matching buy. Never wall-clock now() when history exists."""
        for key in (
            "closed_at",
            "closedTime",
            "endDate",
            "end_date",
            "resolved_at",
            "resolvedAt",
        ):
            got = _parse_iso_ts(pos.get(key))
            if got:
                return got
        data = _raw_dict(pos.get("raw"))
        for key in ("closed_at", "closedTime", "endDate", "end_date", "resolved_at"):
            got = _parse_iso_ts(data.get(key))
            if got:
                return got
        buy = self._first_buy_ts(str(pos.get("condition_id") or ""), pos.get("side"))
        if buy:
            return buy
        for key in ("opened_ts", "last_ts"):
            got = _parse_iso_ts(pos.get(key))
            if got:
                return got
        return None

    def backfill_resolve_timestamps(self) -> int:
        """Rewrite imported RESOLVE_* fill ts to original closed_at / matching buy."""
        if self.get_meta("resolve_ts_v2", ""):
            return 0
        n = 0
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT id, condition_id, side, ts, raw, source
                FROM fills
                WHERE side LIKE 'RESOLVE%' OR IFNULL(source,'')='resolve'
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
        now = datetime.now(timezone.utc)
        for row in rows:
            data = _raw_dict(row.get("raw"))
            su = normalize_side(row.get("side"))
            yn = (su[8:] or "YES") if su.startswith("RESOLVE_") else "YES"
            pos = {
                "condition_id": row.get("condition_id"),
                "side": yn,
                "closed_at": data.get("closed_at"),
                "raw": data,
            }
            ts = self._resolution_ts(pos)
            old = _parse_iso_ts(row.get("ts"))
            if not ts:
                continue
            try:
                ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                if abs((now - ts_dt).total_seconds()) < 120:
                    continue
            except ValueError:
                continue
            if ts == old and _parse_iso_ts(data.get("closed_at")) == ts:
                continue
            data["closed_at"] = ts
            with self._lock:
                self.conn.execute(
                    "UPDATE fills SET ts=?, raw=? WHERE id=?",
                    (ts, json.dumps(data, default=str), row["id"]),
                )
                self.conn.commit()
            n += 1
        self.set_meta("resolve_ts_v2", utc_now())
        if n:
            log.info("backfill resolve timestamps: %s", n)
        return n

    def n_resolves_stamped_now(self, window_s: float = 120.0) -> int:
        """RESOLVE fills whose ts is wall-clock now — imported rows must be 0."""
        now = datetime.now(timezone.utc)
        n = 0
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT ts, raw FROM fills
                WHERE side LIKE 'RESOLVE%' OR IFNULL(source,'')='resolve'
                """
            )
            rows = list(cur.fetchall())
        for row in rows:
            data = _raw_dict(row["raw"])
            ts = _parse_iso_ts(data.get("closed_at")) or _parse_iso_ts(row["ts"])
            if not ts:
                n += 1
                continue
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except ValueError:
                n += 1
                continue
            if abs((now - dt).total_seconds()) <= window_s:
                n += 1
        return n

    def deposited_since(self) -> datetime | None:
        raw = self.get_meta("deposited_ts", "")
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts
        except ValueError:
            return None

    def close_position(self, condition_id: str, side: str | None = None) -> None:
        with self._lock:
            if side:
                self.conn.execute(
                    "UPDATE positions SET status='closed', shares=0, last_ts=? WHERE condition_id=? AND side=?",
                    (utc_now(), condition_id, side),
                )
            else:
                self.conn.execute(
                    "UPDATE positions SET status='closed', shares=0, last_ts=? WHERE condition_id=?",
                    (utc_now(), condition_id),
                )
            self.conn.commit()

    def _dust_key(self, condition_id: str, side: str | None) -> str:
        return f"dust:{condition_id}:{str(side or 'YES').upper()}"

    def is_dust(self, condition_id: str, side: str | None) -> bool:
        return bool(self.get_meta(self._dust_key(condition_id, side), ""))

    def close_dust(self, condition_id: str, side: str | None = None) -> None:
        with self._lock:
            if side:
                self.conn.execute(
                    "UPDATE positions SET status='closed_dust', shares=0, last_ts=? WHERE condition_id=? AND side=?",
                    (utc_now(), condition_id, side),
                )
            else:
                self.conn.execute(
                    "UPDATE positions SET status='closed_dust', shares=0, last_ts=? WHERE condition_id=?",
                    (utc_now(), condition_id),
                )
            self.conn.commit()
        now = utc_now()
        self.set_meta(self._dust_key(condition_id, side), now)
        self.set_meta(f"dust_close:{condition_id}", now)

    def dust_close_fresh(self, condition_id: str, side: str | None = None, window_s: float = 1800) -> bool:
        raw = self.get_meta(f"dust_close:{condition_id}", "") or self.get_meta(self._dust_key(condition_id, side), "")
        if not raw:
            return False
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).total_seconds() < window_s
        except (TypeError, ValueError):
            return True

    def clear_dust(self, condition_id: str, side: str | None) -> None:
        self.set_meta(self._dust_key(condition_id, side), "")

    def add_fill(self, **row: Any) -> None:
        raw = row.get("raw", {})
        data = _raw_dict(raw)
        side = normalize_side(row.get("side"))
        src = normalize_source(row.get("source") if row.get("source") is not None else data.get("source"))
        detail = row.get("source_detail")
        if detail is None:
            detail = data.get("source_detail") or data.get("thesis") or data.get("reason")
        if detail is not None:
            detail = str(detail).strip()[:160] or None
        if not src:
            inf, inf_detail = infer_fill_source(side, {**data, "source": src, "source_detail": detail})
            src = inf
            if not detail:
                detail = inf_detail
        kalshi_ticker = row.get("kalshi_ticker") if "kalshi_ticker" in row else data.get("kalshi_ticker")
        kalshi_mid = row.get("kalshi_mid") if "kalshi_mid" in row else data.get("kalshi_mid")
        pm_mid = row.get("pm_mid") if "pm_mid" in row else data.get("pm_mid")
        question = row.get("question") or data.get("question")
        if src == "kalshi":
            from agent.kalshi import pair_ok

            legal = kalshi_fields_ok(kalshi_ticker, kalshi_mid, pm_mid, {**data, "source_detail": detail}) and bool(question) and pair_ok(
                str(question),
                str(kalshi_ticker or ""),
                str(data.get("title") or ""),
                kalshi_mid,
                pm_mid,
            )[0]
            if not legal:
                inf, inf_detail = infer_fill_source(
                    side,
                    {**data, "source": None, "source_detail": detail, "kalshi_ticker": kalshi_ticker, "kalshi_mid": kalshi_mid, "pm_mid": pm_mid, "question": question},
                )
                src = inf if inf and inf != "kalshi" else None
                if not detail:
                    detail = inf_detail
        token_id = row.get("token_id") or data.get("token_id")
        grok_p = row.get("grok_p") if "grok_p" in row else data.get("grok_p")
        grok_conf = row.get("grok_conf") if "grok_conf" in row else data.get("grok_conf")
        edge_net = row.get("edge_net") if "edge_net" in row else data.get("edge_net")
        gap_c = row.get("gap_c") if "gap_c" in row else data.get("gap_c")
        cycle_id = row.get("cycle_id") if row.get("cycle_id") is not None else data.get("cycle_id")
        ts = _parse_iso_ts(row.get("ts")) or utc_now()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO fills (
                    ts, condition_id, question, token_id, side, price, size, cost, dry_run, raw,
                    source, source_detail, grok_p, grok_conf, edge_net,
                    kalshi_ticker, kalshi_mid, pm_mid, gap_c, cycle_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    row.get("condition_id"),
                    question,
                    token_id,
                    side,
                    row.get("price"),
                    row.get("size"),
                    row.get("cost"),
                    1 if row.get("dry_run") else 0,
                    json.dumps(raw if raw is not None else {}, default=str),
                    src,
                    detail,
                    grok_p,
                    grok_conf,
                    edge_net,
                    kalshi_ticker,
                    kalshi_mid,
                    pm_mid,
                    gap_c,
                    cycle_id,
                ),
            )
            self.conn.commit()

    def mark_equity(self, bankroll: float, equity: float) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO pnl_marks (ts, bankroll, equity) VALUES (?, ?, ?)",
                (utc_now(), bankroll, equity),
            )
            self.conn.commit()

    def equity_change_since(self, hours: float, current: float) -> float:
        hist = self.equity_history(400)
        if not hist:
            return 0.0
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        chosen = float(hist[0]["equity"])
        for row in hist:
            try:
                ts = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts.timestamp() <= cutoff:
                    chosen = float(row["equity"])
            except ValueError:
                continue
        return current - chosen

    def latest_mark(self) -> dict | None:
        with self._lock:
            cur = self.conn.execute(
                "SELECT ts, bankroll, equity FROM pnl_marks ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def grouped_rejects(self, limit: int = 20) -> list[tuple[str, int]]:
        """Last N reject/skip reasons, grouped by reason text."""
        rows = self.recent_decisions(120)
        picked: list[str] = []
        for r in rows:
            if str(r.get("action") or "") not in {"reject", "skip"}:
                continue
            picked.append(str(r.get("reason") or "—")[:90])
            if len(picked) >= limit:
                break
        counts: dict[str, int] = {}
        for reason in picked:
            counts[reason] = counts.get(reason, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

    def leak_fills_since(self, sources: tuple[str, ...], since_ts: str) -> list[dict]:
        """Fills tagged grok/stats after freeze_ts. Empty since_ts = all such fills."""
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT ts, question, side, source, source_detail, cost, dry_run
                FROM fills WHERE source IN ({})
                ORDER BY id DESC LIMIT 50
                """.format(",".join("?" * len(sources))),
                tuple(sources),
            )
            rows = [dict(r) for r in cur.fetchall()]
        if not since_ts:
            return rows
        out = []
        for r in rows:
            ts = str(r.get("ts") or "")
            if ts >= since_ts:
                out.append(r)
        return out

    def recent_decisions(self, limit: int = 80) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT ts, condition_id, question, side, mid, p_hat, edge_net, action, reason
                FROM decisions ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def _decorate_fill(self, row: dict) -> dict:
        raw = row.get("raw")
        src = normalize_source(row.get("source"))
        detail = (row.get("source_detail") or "").strip() or None
        if src == "kalshi" and not kalshi_fields_ok(
            row.get("kalshi_ticker"), row.get("kalshi_mid"), row.get("pm_mid"), raw
        ):
            src = None
        if not src or not detail:
            inf, inf_detail = infer_fill_source(row.get("side"), raw)
            if not src:
                src = inf
            if src == "kalshi" and not kalshi_fields_ok(
                row.get("kalshi_ticker"), row.get("kalshi_mid"), row.get("pm_mid"), raw
            ):
                src = inf if inf and inf != "kalshi" else None
            if not detail:
                detail = inf_detail
        if not row.get("question"):
            q = _raw_dict(raw).get("question")
            if q:
                row["question"] = q
        row["side"] = normalize_side(row.get("side"))
        row["source"] = src
        row["source_detail"] = detail
        row.pop("raw", None)
        return row

    def recent_fills(self, limit: int = 40, real_only: bool = False) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT ts, condition_id, question, token_id, side, price, size, cost, dry_run, raw,
                       source, source_detail, grok_p, grok_conf, edge_net,
                       kalshi_ticker, kalshi_mid, pm_mid, gap_c, cycle_id
                FROM fills ORDER BY id DESC LIMIT ?
                """,
                (max(limit * 4, 80) if real_only else limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        out: list[dict] = []
        for row in rows:
            if real_only:
                if int(row.get("dry_run") or 0) == 1:
                    continue
                if not self._raw_is_matched(row.get("raw")):
                    continue
            out.append(self._decorate_fill(row))
            if len(out) >= limit:
                break
        return out

    def equity_history(self, limit: int = 60) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(
                "SELECT ts, bankroll, equity FROM pnl_marks ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            cur = self.conn.execute("SELECT v FROM meta WHERE k=?", (key,))
            row = cur.fetchone()
        return str(row["v"]) if row and row["v"] is not None else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, str(value)),
            )
            self.conn.commit()

    def deposited_usd(self, equity_fallback: float) -> float:
        """User-editable start equity. Never auto-fill from cash/equity."""
        _ = equity_fallback
        raw = self.get_meta("deposited_usd", "")
        try:
            val = float(raw)
            if val >= 1:
                return val
        except (TypeError, ValueError):
            pass
        return 0.0

    def first_sane_equity(self, fallback: float) -> float:
        with self._lock:
            cur = self.conn.execute(
                "SELECT equity FROM pnl_marks WHERE equity >= 20 ORDER BY id ASC LIMIT 1"
            )
            row = cur.fetchone()
        if row:
            return float(row["equity"])
        return float(fallback or 0)

    def mark_bad_market(self, condition_id: str, reason: str = "") -> None:
        if not condition_id:
            return
        self.set_meta(f"bad:{condition_id}", reason or "1")

    def is_bad_market(self, condition_id: str) -> bool:
        if not condition_id:
            return False
        raw = self.get_meta(f"bad:{condition_id}")
        if not raw:
            return False
        low = raw.lower()
        if any(x in low for x in ("order_type", "unexpected keyword", "typeerror")):
            return False
        return True

    def xai_prepaid_usd(self) -> float:
        raw = self.get_meta("xai_prepaid_usd", "")
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return float(settings.xai_prepaid_usd or 0)

    def kalshi_tickers_for(self, condition_id: str) -> list[str]:
        cid = str(condition_id or "").strip()
        if not cid:
            return []
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT kalshi_ticker, source_detail FROM fills
                WHERE condition_id=? ORDER BY id DESC LIMIT 12
                """,
                (cid,),
            )
            rows = cur.fetchall()
        out: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for raw in (row["kalshi_ticker"], row["source_detail"]):
                for t in re.findall(r"\b(KX[A-Z0-9-]{5,})\b", str(raw or ""), re.I):
                    u = t.upper()
                    if u not in seen:
                        seen.add(u)
                        out.append(u)
        return out

    def first_buy_shares(self, condition_id: str, side: str | None) -> float | None:
        cid = str(condition_id or "").strip()
        yn = str(side or "YES").upper()
        if yn.startswith("BUY_"):
            yn = yn[4:]
        if not cid:
            return None
        want = {f"BUY_{yn}", yn}
        with self._lock:
            cur = self.conn.execute(
                "SELECT side, size FROM fills WHERE condition_id=? ORDER BY id ASC",
                (cid,),
            )
            rows = cur.fetchall()
        for row in rows:
            su = normalize_side(row["side"])
            if su in {f"BUY_{yn}", yn} or (su.startswith("BUY_") and su[4:] == yn):
                try:
                    sz = float(row["size"] or 0)
                except (TypeError, ValueError):
                    sz = 0.0
                if sz > 0:
                    return sz
        return None

    def buy_fill_count(self, condition_id: str, side: str | None) -> int:
        cid = str(condition_id or "").strip()
        yn = str(side or "YES").upper()
        if yn.startswith("BUY_"):
            yn = yn[4:]
        n = 0
        with self._lock:
            cur = self.conn.execute(
                "SELECT side FROM fills WHERE condition_id=?",
                (cid,),
            )
            rows = cur.fetchall()
        for row in rows:
            su = normalize_side(row["side"])
            if su in {f"BUY_{yn}", yn} or (su.startswith("BUY_") and su[4:] == yn):
                n += 1
        return n

    def fill_count(self) -> int:
        with self._lock:
            cur = self.conn.execute("SELECT COUNT(*) AS n FROM fills")
            row = cur.fetchone()
        return int(row["n"] if row else 0)

    def live_fill_count(self) -> int:
        with self._lock:
            cur = self.conn.execute("SELECT raw FROM fills WHERE dry_run=0")
            rows = cur.fetchall()
        return sum(1 for row in rows if self._raw_is_matched(row["raw"]))

    def sync_open_positions(self, live: list[dict]) -> None:
        """Replace local open with data-api seats. Resolve vanished / mid≤0.02."""
        cleaned = []
        dead: list[dict] = []
        for r in live:
            cid = str(r.get("condition_id") or "").strip()
            if not cid:
                continue
            try:
                mid = float(r.get("cur_price") or 0)
            except (TypeError, ValueError):
                mid = 0.0
            redeemable = bool(r.get("redeemable") or r.get("closed"))
            if redeemable and mid <= 0.02:
                dead.append(r)
                continue
            cleaned.append(r)
        live_keys = {(str(r.get("condition_id")), str(r.get("side") or "YES").upper()) for r in cleaned}
        local_open = self.positions("open")
        for row in local_open:
            key = (str(row.get("condition_id")), str(row.get("side") or "YES").upper())
            if key not in live_keys:
                self.record_resolution(row, source="resolve", proceeds=0.0)
                self.close_position(str(row.get("condition_id") or ""), row.get("side"))
        for r in dead:
            self.record_resolution(r, source="redeem", proceeds=0.0)
            self.close_position(str(r.get("condition_id") or ""), r.get("side"))
        for r in cleaned:
            self.upsert_position(**r)

    def first_mark(self) -> dict | None:
        with self._lock:
            cur = self.conn.execute(
                "SELECT ts, bankroll, equity FROM pnl_marks ORDER BY id ASC LIMIT 1"
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def position_cost(self, p: dict) -> float:
        return float(p.get("shares") or 0) * float(p.get("avg_cost") or 0)

    def shown_seats(self, open_pos: list[dict]) -> list[dict]:
        """Seats shown in Posisjoner: size>0 and mid>0.01."""
        return [p for p in (open_pos or []) if is_open_seat(p)]

    def markedet_sum(self, open_pos: list[dict]) -> float:
        return sum(self.position_mtm(p) for p in self.shown_seats(open_pos))

    def note_period_anchors(self, equity: float) -> dict:
        """Persist UTC-midnight equity, 7d trail, and peak. Call before save_snapshot."""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        eq = float(equity or 0)
        stored_day = self.get_meta("day_anchor_date", "")
        if stored_day != today:
            if not stored_day:
                anchor = eq
            else:
                prev = self.float_meta("last_equity")
                anchor = float(prev) if prev is not None else eq
            self.set_meta("day_anchor_date", today)
            self.set_meta("day_anchor_equity", f"{anchor:.4f}")
        deposited = self.deposited_usd(0.0)
        dep_ref = self.get_meta("deposited_usd", "")
        if self.get_meta("peak_deposited_ref", "") != dep_ref:
            peak = max(deposited if deposited >= 1 else 0.0, eq)
            self.set_meta("peak_deposited_ref", dep_ref)
        else:
            saved = self.float_meta("peak_equity") or 0.0
            peak = max(deposited if deposited >= 1 else 0.0, saved, eq)
        self.set_meta("peak_equity", f"{peak:.4f}")
        self._append_eq_trail(now.timestamp(), eq)
        day_anchor = self.float_meta("day_anchor_equity")
        if day_anchor is None:
            day_anchor = eq
        daily = eq - float(day_anchor)
        week_base = self.equity_hours_ago(7 * 24)
        week = (eq - week_base) if week_base is not None else 0.0
        dd = eq - float(peak or 0)
        self.set_meta("daily_pnl", f"{daily:.4f}")
        self.set_meta("week_pnl", f"{week:.4f}")
        self.set_meta("dd_from_peak", f"{dd:.4f}")
        return {
            "daily_pnl": daily,
            "week_pnl": week,
            "dd_from_peak": dd,
            "peak": float(peak or 0),
            "day_anchor": float(day_anchor),
            "week_base": week_base,
        }

    def _append_eq_trail(self, ts: float, equity: float) -> None:
        try:
            trail = json.loads(self.get_meta("eq_trail", "[]") or "[]")
        except json.JSONDecodeError:
            trail = []
        if not isinstance(trail, list):
            trail = []
        point = {"t": float(ts), "e": round(float(equity), 4)}
        if trail:
            try:
                last_t = float(trail[-1].get("t") or 0)
            except (TypeError, ValueError, AttributeError):
                last_t = 0.0
            if ts - last_t < 3600:
                trail[-1] = point
            else:
                trail.append(point)
        else:
            trail.append(point)
        cutoff = ts - 8 * 24 * 3600
        cleaned = []
        for p in trail:
            if not isinstance(p, dict):
                continue
            try:
                if float(p.get("t") or 0) >= cutoff:
                    cleaned.append({"t": float(p["t"]), "e": float(p["e"])})
            except (TypeError, ValueError, KeyError):
                continue
        self.set_meta("eq_trail", json.dumps(cleaned))

    def equity_hours_ago(self, hours: float) -> float | None:
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        try:
            trail = json.loads(self.get_meta("eq_trail", "[]") or "[]")
        except json.JSONDecodeError:
            trail = []
        chosen = None
        for p in trail:
            if not isinstance(p, dict):
                continue
            try:
                t = float(p.get("t") or 0)
                e = float(p.get("e"))
            except (TypeError, ValueError):
                continue
            if t <= cutoff:
                chosen = e
        return chosen

    def position_mtm(self, p: dict) -> float:
        """Live value = data-api mark. Include winners at 1.0. Never fall back to cost."""
        shares = float(p.get("shares") or 0)
        cv = p.get("current_value")
        try:
            if cv not in (None, "") and float(cv) > 0:
                return float(cv)
        except (TypeError, ValueError):
            pass
        cur = p.get("cur_price")
        try:
            if cur not in (None, "") and float(cur) > 0.01:
                return shares * float(cur)
        except (TypeError, ValueError):
            pass
        return 0.0

    def split_cash_equity(self, available: float, open_pos: list[dict]) -> tuple[float, float, float, float]:
        """cash = available to trade. equity = cash + MTM. Never deposited − cost."""
        open_cost = sum(self.position_cost(p) for p in open_pos)
        open_mtm = sum(self.position_mtm(p) for p in open_pos)
        cash = max(0.0, float(available or 0))
        equity = cash + open_mtm
        return cash, equity, open_cost, open_mtm

    def portfolio_stats(self, equity: float, bankroll: float, open_pos: list[dict]) -> dict:
        start = self.deposited_usd(0.0)
        shown = self.shown_seats(open_pos)
        open_cost = sum(self.position_cost(p) for p in shown)
        i_markedet = sum(self.position_mtm(p) for p in shown)
        cash = max(0.0, float(bankroll or 0))
        equity = float(equity or 0)
        if start < 1:
            start = 0.0
        total = equity - start if start >= 1 else 0.0
        total_pct = (total / start) if start >= 1 else 0.0
        day_anchor = self.float_meta("day_anchor_equity")
        daily = (equity - float(day_anchor)) if day_anchor is not None else 0.0
        week_base = self.equity_hours_ago(7 * 24)
        week = (equity - week_base) if week_base is not None else 0.0
        saved_peak = self.float_meta("peak_equity") or 0.0
        peak = max(start if start >= 1 else 0.0, saved_peak, equity)
        dd_usd = equity - peak if peak else 0.0
        max_dd_pct = (dd_usd / peak) if peak else 0.0
        day_base = float(day_anchor) if day_anchor is not None else 0.0
        xai_total = self.api_spend(hours=None)
        return {
            "start_equity": round(start, 2) if start >= 1 else 0.0,
            "equity": round(equity, 2),
            "total": round(total, 2),
            "total_pct": round(total_pct, 4),
            "day": round(daily, 2),
            "day_pct": round(daily / day_base, 4) if day_base else 0.0,
            "week": round(week, 2),
            "week_pct": round(week / float(week_base), 4) if week_base else 0.0,
            "max_dd_pct": round(max_dd_pct, 4),
            "max_dd_usd": round(dd_usd, 2),
            "trades": self.live_fill_count(),
            "open_cost": round(open_cost, 2),
            "open_mtm": round(i_markedet, 2),
            "i_markedet": round(i_markedet, 2),
            "cash": round(cash, 2),
            "xai_total": round(xai_total, 4),
            "xai_day": round(self.api_spend(hours=24), 4),
            "xai_prepaid": round(self.xai_prepaid_usd(), 2),
            "deposited": round(start, 2) if start >= 1 else 0.0,
            "after_xai": round(total - xai_total, 2),
            "daily_pnl": round(daily, 2),
            "dd_from_peak": round(dd_usd, 2),
            "top_rejects": self.top_rejects(8),
        }

    def add_api_cost(self, usd: float, model: str = "", tokens: int = 0) -> None:
        if usd <= 0:
            return
        with self._lock:
            self.conn.execute(
                "INSERT INTO api_costs (ts, usd, model, tokens) VALUES (?, ?, ?, ?)",
                (utc_now(), usd, model, tokens),
            )
            self.conn.commit()

    def api_spend(self, hours: float | None = None) -> float:
        with self._lock:
            if hours is None:
                cur = self.conn.execute("SELECT COALESCE(SUM(usd), 0) AS s FROM api_costs")
            else:
                cutoff = (datetime.now(timezone.utc).timestamp() - hours * 3600)
                cur = self.conn.execute("SELECT ts, usd FROM api_costs")
                total = 0.0
                for row in cur.fetchall():
                    try:
                        ts = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts.timestamp() >= cutoff:
                            total += float(row["usd"])
                    except ValueError:
                        continue
                return total
            row = cur.fetchone()
        return float(row["s"] if row else 0)

    def top_rejects(self, limit: int = 8) -> list[dict]:
        rows = self.recent_decisions(200)
        cutoff = datetime.now(timezone.utc).timestamp() - 2 * 24 * 3600
        counts: dict[str, int] = {}
        for row in rows:
            if row.get("action") not in {"reject", "skip"}:
                continue
            reason = (row.get("reason") or "ukjent")[:80]
            try:
                ts = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts.timestamp() < cutoff:
                    continue
            except ValueError:
                pass
            counts[reason] = counts.get(reason, 0) + 1
        ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
        return [{"reason": k, "n": v} for k, v in ranked]

    def _matched_fills(self) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT ts, condition_id, question, token_id, side, price, size, cost, dry_run, raw,
                       source, source_detail, grok_p, grok_conf, edge_net,
                       kalshi_ticker, kalshi_mid, pm_mid, gap_c, cycle_id
                FROM fills ORDER BY id ASC
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
        out: list[dict] = []
        for row in rows:
            if int(row.get("dry_run") or 0) == 1:
                continue
            if not self._raw_is_matched(row.get("raw")):
                continue
            out.append(self._decorate_fill(row))
        return out

    def attribution_stats(self, open_pos: list[dict] | None = None) -> dict:
        open_pos = open_pos if open_pos is not None else self.positions("open")
        shown = self.shown_seats(open_pos)
        deposited = self.deposited_usd(0.0)
        open_cost = sum(self.position_cost(p) for p in shown)
        open_mtm = sum(self.position_mtm(p) for p in shown)
        unrealized = open_mtm - open_cost
        seats = len(shown)
        open_pct = (open_cost / deposited) if deposited >= 1 else 0.0
        fills = self._matched_fills()
        now = datetime.now(timezone.utc)
        groups: dict[tuple[str, str], dict] = {}

        def _parse_ts(raw: Any) -> datetime | None:
            try:
                ts = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return ts
            except ValueError:
                return None

        def _leg(side: Any) -> tuple[str, str]:
            s = normalize_side(side)
            if s.startswith("REDEEM"):
                return "redeem", (s[7:] or "YES")
            if s.startswith("RESOLVE"):
                return "redeem", (s[8:] or "YES")
            if s.startswith("SELL_"):
                return "sell", s[5:] or "YES"
            if s.startswith("BUY_"):
                return "buy", s[4:] or "YES"
            return "buy", s or "YES"

        def _src_bucket(src: Any) -> str | None:
            s = str(src or "").lower()
            if s in {"grok", "kalshi", "complement", "partition", "maker"}:
                return s
            if s in {"exit_stop", "exit_take", "exit_trail", "exit_kalshi", "flatten"}:
                return "exits"
            return None

        for f in fills:
            cid = str(f.get("condition_id") or "")
            direction, yn = _leg(f.get("side"))
            key = (cid, yn)
            g = groups.setdefault(
                key,
                {
                    "buy_cost": 0.0,
                    "buy_n": 0,
                    "sell_proceeds": 0.0,
                    "sell_n": 0,
                    "first_ts": None,
                    "last_ts": None,
                    "entry_source": None,
                    "via_sell": False,
                    "via_redeem": False,
                },
            )
            ts = _parse_ts(f.get("ts"))
            su = normalize_side(f.get("side"))
            if su.startswith("RESOLVE") or str(f.get("source") or "").lower() == "resolve":
                closed = _parse_ts(_raw_dict(f.get("raw")).get("closed_at"))
                if closed:
                    ts = closed
            if ts and (g["first_ts"] is None or ts < g["first_ts"]):
                g["first_ts"] = ts
            if ts and (g["last_ts"] is None or ts > g["last_ts"]):
                g["last_ts"] = ts
            try:
                cost = float(f.get("cost") or 0)
            except (TypeError, ValueError):
                cost = 0.0
            src = f.get("source")
            if direction == "buy":
                g["buy_cost"] += cost
                g["buy_n"] += 1
                real = _real_buy_source(src, f.get("source_detail"))
                if not g["entry_source"] and real:
                    g["entry_source"] = real
            elif direction == "redeem":
                g["sell_proceeds"] += cost
                g["sell_n"] += 1
                g["via_redeem"] = True
            else:
                g["sell_proceeds"] += cost
                g["sell_n"] += 1
                g["via_sell"] = True

        open_keys = {
            (str(p.get("condition_id")), str(p.get("side") or "YES").upper()) for p in open_pos
        }
        since = self.deposited_since()
        closed: list[dict] = []
        for key, g in groups.items():
            if key in open_keys:
                continue
            if g["sell_n"] <= 0 and not g["via_redeem"]:
                continue
            realized = g["sell_proceeds"] - g["buy_cost"]
            hold_h = None
            if g["first_ts"] and g["last_ts"]:
                hold_h = (g["last_ts"] - g["first_ts"]).total_seconds() / 3600.0
            if since is not None:
                mark_ts = g["last_ts"] or g["first_ts"]
                if mark_ts is None or mark_ts < since:
                    continue
            closed.append(
                {
                    "realized": realized,
                    "hold_h": hold_h,
                    "close_ts": g["last_ts"],
                    "entry_source": g["entry_source"],
                    "via_sell": g["via_sell"],
                }
            )

        def _window(hours: float | None) -> dict:
            rows = closed
            if hours is not None:
                cutoff = now.timestamp() - hours * 3600
                rows = [
                    r
                    for r in rows
                    if r["close_ts"] is not None and r["close_ts"].timestamp() >= cutoff
                ]
            n = len(rows)
            pnl = sum(float(r["realized"]) for r in rows)
            wins = [r for r in rows if r["realized"] > 0]
            losses = [r for r in rows if r["realized"] < 0]
            holds = [r["hold_h"] for r in rows if r["hold_h"] is not None]
            return {
                "n": n,
                "realized": round(pnl, 2),
                "n_win": len(wins),
                "n_loss": len(losses),
                "usd_win": round(sum(r["realized"] for r in wins), 2),
                "usd_loss": round(abs(sum(r["realized"] for r in losses)), 2),
                "win_pct": round(len(wins) / n, 4) if n else 0.0,
                "expectancy": round(pnl / n, 4) if n else 0.0,
                "avg_hold_h": round(sum(holds) / len(holds), 2) if holds else None,
            }

        all_s = _window(None)
        d24 = _window(24)
        d7 = _window(24 * 7)
        by_source = {
            k: {"n": 0, "bought": 0.0, "realized": 0.0}
            for k in ("grok", "kalshi", "complement", "partition", "maker", "exits")
        }
        for f in fills:
            direction, _yn = _leg(f.get("side"))
            bucket = _src_bucket(f.get("source"))
            if not bucket:
                continue
            try:
                cost = float(f.get("cost") or 0)
            except (TypeError, ValueError):
                cost = 0.0
            if direction == "buy" and bucket != "exits":
                by_source[bucket]["n"] += 1
                by_source[bucket]["bought"] += cost
            elif direction != "buy" and bucket == "exits":
                by_source["exits"]["n"] += 1
                by_source["exits"]["bought"] += cost
        for r in closed:
            b = _src_bucket(r.get("entry_source"))
            if b in {"grok", "kalshi", "complement", "partition", "maker"}:
                by_source[b]["realized"] += r["realized"]
            if r.get("via_sell"):
                by_source["exits"]["realized"] += r["realized"]
        for k, v in by_source.items():
            v["bought"] = round(v["bought"], 2)
            v["realized"] = round(v["realized"], 2)
        return {
            "realized": all_s["realized"],
            "realized_24h": d24["realized"],
            "realized_7d": d7["realized"],
            "unrealized": round(unrealized, 2),
            "win_rate": {
                "n_win": all_s["n_win"],
                "n_loss": all_s["n_loss"],
                "n_closed": all_s["n"],
                "pct": all_s["win_pct"],
                "usd_win": all_s["usd_win"],
                "usd_loss": all_s["usd_loss"],
                "n_win_24h": d24["n_win"],
                "n_loss_24h": d24["n_loss"],
                "n_closed_24h": d24["n"],
                "n_win_7d": d7["n_win"],
                "n_loss_7d": d7["n_loss"],
                "n_closed_7d": d7["n"],
            },
            "expectancy": all_s["expectancy"],
            "expectancy_24h": d24["expectancy"],
            "expectancy_7d": d7["expectancy"],
            "by_source": by_source,
            "avg_hold_h": all_s["avg_hold_h"],
            "open_risk": {
                "usd": round(open_cost, 2),
                "pct": round(open_pct, 4),
                "seats": seats,
                "mtm": round(open_mtm, 2),
            },
            "deposited": round(deposited, 2) if deposited >= 1 else 0.0,
            "header_pnl": round(unrealized + all_s["realized"], 2),
            "footnote": (
                f"Realized er kun lukkede fills ({all_s['realized']:+.2f}). "
                f"Header er MTM vs innskutt = realized + åpen uPnL ({unrealized:+.2f})."
            ),
        }

