from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.config import settings


VALID_SOURCES = frozenset(
    {
        "grok",
        "kalshi",
        "complement",
        "tape",
        "exit_stop",
        "exit_take",
        "exit_trail",
        "exit_kalshi",
        "redeem",
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
    "REDEEM_YES": "REDEEM",
    "REDEEM_NO": "REDEEM",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    if explicit == "kalshi" and not kalshi_fields_ok(raw=data):
        explicit = None
    if explicit:
        return explicit, thesis or None
    if su == "REDEEM" or data.get("redeem"):
        return "redeem", thesis or "redeem_ok"
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
    if "låst utfall" in low:
        return "tape", thesis or None
    if kalshi_fields_ok(raw=data) and (
        "kalshi-bekreftelse" in low or explicit == "kalshi" or data.get("kalshi_ticker")
    ):
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
                yn = su[4:] or "YES"
                key = (str(row["condition_id"] or ""), yn)
                if key[0] and key not in first:
                    first[key] = (str(row["source"]), row["source_detail"])
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
        if merged.get("redeem"):
            return True
        if status in {"live", "open", "resting", "unmatched", "cancelled", "canceled"} and taking.lower() in empty and making.lower() in empty:
            return False
        if status in {"matched", "filled"}:
            try:
                if float(taking or 0) > 0 or float(making or 0) > 0:
                    return True
            except (TypeError, ValueError):
                pass
            if merged.get("redeem"):
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
        self.set_meta(self._dust_key(condition_id, side), utc_now())

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
        if src == "kalshi" and not kalshi_fields_ok(kalshi_ticker, kalshi_mid, pm_mid, {**data, "source_detail": detail}):
            inf, inf_detail = infer_fill_source(
                side,
                {**data, "source": None, "source_detail": detail, "kalshi_ticker": kalshi_ticker, "kalshi_mid": kalshi_mid, "pm_mid": pm_mid},
            )
            src = inf if inf and inf != "kalshi" else None
            if not detail:
                detail = inf_detail
        question = row.get("question") or data.get("question")
        token_id = row.get("token_id") or data.get("token_id")
        grok_p = row.get("grok_p") if "grok_p" in row else data.get("grok_p")
        grok_conf = row.get("grok_conf") if "grok_conf" in row else data.get("grok_conf")
        edge_net = row.get("edge_net") if "edge_net" in row else data.get("edge_net")
        gap_c = row.get("gap_c") if "gap_c" in row else data.get("gap_c")
        cycle_id = row.get("cycle_id") if row.get("cycle_id") is not None else data.get("cycle_id")
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
                    utc_now(),
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
        raw = self.get_meta("deposited_usd", "")
        try:
            val = float(raw)
            if val >= 1:
                return val
        except (TypeError, ValueError):
            pass
        first = self.first_sane_equity(equity_fallback)
        if first >= 20 and equity_fallback >= 20:
            self.set_meta("deposited_usd", f"{first:.2f}")
            return first
        return first if first >= 1 else 0.0

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
        """Erstatt lokale open med det Polymarket faktisk viser."""
        cleaned = []
        for r in live:
            cid = str(r.get("condition_id") or "").strip()
            if not cid:
                continue
            cleaned.append(r)
        live_keys = {(str(r.get("condition_id")), str(r.get("side") or "YES").upper()) for r in cleaned}
        with self._lock:
            cur = self.conn.execute("SELECT condition_id, side FROM positions WHERE status='open'")
            for row in cur.fetchall():
                key = (str(row["condition_id"]), str(row["side"] or "YES").upper())
                if key not in live_keys:
                    self.conn.execute(
                        "UPDATE positions SET status='closed', shares=0, last_ts=? WHERE condition_id=? AND side=?",
                        (utc_now(), row["condition_id"], row["side"]),
                    )
            self.conn.commit()
        for r in cleaned:
            cid = str(r.get("condition_id") or "")
            side = str(r.get("side") or "YES")
            if self.is_dust(cid, side):
                try:
                    cur = float(r.get("cur_price") or 0)
                except (TypeError, ValueError):
                    cur = 0.0
                try:
                    mtm = float(r.get("current_value") or 0)
                except (TypeError, ValueError):
                    mtm = 0.0
                shares = float(r.get("shares") or 0)
                if mtm <= 0 and shares and cur:
                    mtm = shares * cur
                try:
                    dep = float(self.get_meta("deposited_usd") or 0)
                except (TypeError, ValueError):
                    dep = 0.0
                dust_cut = 0.001 * dep if dep > 0 else 0.0
                if (dust_cut > 0 and mtm < dust_cut) or cur <= 0.01:
                    continue
                self.clear_dust(cid, side)
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

    def position_mtm(self, p: dict) -> float:
        """Live value = shares * mid. Never fall back to cost (that double-counts vs available cash)."""
        shares = float(p.get("shares") or 0)
        cost = self.position_cost(p)
        cur = p.get("cur_price")
        try:
            if cur not in (None, "") and 0 < float(cur) < 0.99:
                return shares * float(cur)
        except (TypeError, ValueError):
            pass
        cv = p.get("current_value")
        try:
            if cv not in (None, "") and abs(float(cv) - cost) > 0.05:
                return float(cv)
        except (TypeError, ValueError):
            pass
        return 0.0

    def split_cash_equity(self, clob_cash: float, open_pos: list[dict]) -> tuple[float, float, float, float]:
        deposited = 0.0
        try:
            deposited = float(self.get_meta("deposited_usd") or 0)
        except (TypeError, ValueError):
            deposited = 0.0
        open_cost = sum(self.position_cost(p) for p in open_pos)
        open_mtm = sum(self.position_mtm(p) for p in open_pos)
        if deposited >= 1 and open_cost > 1 and abs(float(clob_cash) - deposited) < 3:
            cash = max(0.0, deposited - open_cost)
        else:
            cash = max(0.0, float(clob_cash or 0))
        equity = cash + open_mtm
        return cash, equity, open_cost, open_mtm

    def portfolio_stats(self, equity: float, bankroll: float, open_pos: list[dict]) -> dict:
        hist = self.equity_history(400)
        start = self.deposited_usd(0.0)
        cash, equity, open_cost, open_mtm = self.split_cash_equity(bankroll, open_pos)
        bankroll = cash
        if start < 1:
            start = 0.0
        total = equity - start if start >= 1 else 0.0
        total_pct = (total / start) if start >= 1 else 0.0
        now = datetime.now(timezone.utc)

        def _sane(eq: float) -> bool:
            if eq < 1:
                return False
            # Ghost spike: cash+cost (~239) while deposited is 221.
            if start >= 1 and abs(eq - start) / start > 0.08:
                return False
            if open_cost > 1 and abs(eq - (bankroll + open_cost)) < 0.6:
                return False
            return True

        def _at(hours: float) -> float:
            cutoff = now.timestamp() - hours * 3600
            chosen = start if start >= 1 else equity
            for row in hist:
                try:
                    ts = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    eq = float(row["equity"])
                    if not _sane(eq):
                        continue
                    if ts.timestamp() <= cutoff:
                        chosen = eq
                except ValueError:
                    continue
            return chosen

        day_base = _at(24)
        week_base = _at(24 * 7)
        day = equity - day_base
        week = equity - week_base
        peak = start if start > 0 else equity
        max_dd = 0.0
        max_dd_usd = 0.0
        prev = peak
        for row in hist:
            eq = float(row["equity"])
            if not _sane(eq):
                continue
            if prev and abs(eq - prev) / max(prev, 1) > 0.12:
                continue
            peak = max(peak, eq)
            dd_usd = eq - peak
            if peak and dd_usd < max_dd_usd:
                max_dd_usd = dd_usd
                max_dd = dd_usd / peak
            prev = eq
        xai_total = self.api_spend(hours=None)
        return {
            "start_equity": round(start, 2) if start >= 1 else 0.0,
            "equity": round(equity, 2),
            "total": round(total, 2),
            "total_pct": round(total_pct, 4),
            "day": round(day, 2),
            "day_pct": round(day / day_base, 4) if day_base else 0.0,
            "week": round(week, 2),
            "week_pct": round(week / week_base, 4) if week_base else 0.0,
            "max_dd_pct": round(max_dd, 4),
            "max_dd_usd": round(max_dd_usd, 2),
            "trades": self.live_fill_count(),
            "open_cost": round(open_cost, 2),
            "open_mtm": round(open_mtm, 2),
            "cash": round(bankroll, 2),
            "xai_total": round(xai_total, 4),
            "xai_day": round(self.api_spend(hours=24), 4),
            "xai_prepaid": round(self.xai_prepaid_usd(), 2),
            "deposited": round(start, 2) if start >= 1 else 0.0,
            "after_xai": round(total - xai_total, 2),
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
        deposited = self.deposited_usd(0.0)
        open_cost = sum(self.position_cost(p) for p in open_pos)
        open_mtm = sum(self.position_mtm(p) for p in open_pos)
        unrealized = open_mtm - open_cost
        seats = len(open_pos)
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
            if s == "REDEEM":
                return "redeem", "YES"
            if s.startswith("SELL_"):
                return "sell", s[5:] or "YES"
            if s.startswith("BUY_"):
                return "buy", s[4:] or "YES"
            return "buy", s or "YES"

        def _src_bucket(src: Any) -> str | None:
            s = str(src or "").lower()
            if s in {"grok", "kalshi", "complement"}:
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
                if not g["entry_source"] and _src_bucket(src) in {"grok", "kalshi", "complement"}:
                    g["entry_source"] = src
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
            wins = [r for r in rows if r["realized"] > 0.004]
            losses = [r for r in rows if r["realized"] < -0.004]
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
            for k in ("grok", "kalshi", "complement", "exits")
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
            if b in {"grok", "kalshi", "complement"}:
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

