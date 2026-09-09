from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.config import settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                INSERT INTO positions (condition_id, question, category, event_key, side, token_id, shares, avg_cost, current_value, cur_price, outcome, opened_ts, last_ts, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    category=COALESCE(excluded.category, positions.category)
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
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO fills (ts, condition_id, side, price, size, cost, dry_run, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    row.get("condition_id"),
                    row.get("side"),
                    row.get("price"),
                    row.get("size"),
                    row.get("cost"),
                    1 if row.get("dry_run") else 0,
                    json.dumps(row.get("raw", {}), default=str),
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

    def recent_fills(self, limit: int = 40, real_only: bool = False) -> list[dict]:
        with self._lock:
            cur = self.conn.execute(
                """
                SELECT ts, condition_id, side, price, size, cost, dry_run, raw
                FROM fills ORDER BY id DESC LIMIT ?
                """,
                (max(limit * 4, 80) if real_only else limit,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        if real_only:
            out = []
            for row in rows:
                if int(row.get("dry_run") or 0) == 1:
                    continue
                if not self._raw_is_matched(row.get("raw")):
                    continue
                row.pop("raw", None)
                out.append(row)
                if len(out) >= limit:
                    break
            return out
        for row in rows:
            row.pop("raw", None)
        return rows[:limit]

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

