"""Fjord-dashboard. Token-beskyttet når DASHBOARD_TOKEN er satt."""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from agent.config import settings
from agent.version import release as git_release

log = logging.getLogger("dash")
WEB = Path(__file__).resolve().parent / "web"
_desk = None
_xai_cache: dict[str, Any] = {"ts": 0.0, "val": None}


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


def _xai_remaining(spent: float) -> float | None:
    """Aldri blokker /api/state på xAI HTTP. Prepaid i meta er kilden."""
    if settings.xai_prepaid_usd > 0:
        return max(0.0, settings.xai_prepaid_usd - spent)
    key = settings.xai_management_key
    if not key:
        return None
    now = time.time()
    if now - float(_xai_cache.get("ts") or 0) < 120:
        return _xai_cache.get("val")
    try:
        r = requests.get(
            f"https://management-api.x.ai/v1/billing/teams/{settings.xai_team_id or 'default'}/prepaid/balance",
            headers={"Authorization": f"Bearer {key}"},
            timeout=2,
        )
        val = None
        if r.ok:
            data = r.json() if isinstance(r.json(), dict) else {}
            for k in ("balance_usd", "available_usd", "balance"):
                if k in data:
                    val = float(data[k])
                    break
            if val is None and "cents" in data:
                val = float(data["cents"]) / 100.0
            if val is None and "balance_cents" in data:
                val = float(data["balance_cents"]) / 100.0
        _xai_cache["ts"] = now
        _xai_cache["val"] = val
        return val
    except Exception:
        _xai_cache["ts"] = now
        return _xai_cache.get("val")


def _version() -> dict[str, Any]:
    html = WEB / "index.html"
    mtime = html.stat().st_mtime if html.exists() else 0.0
    iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat() if mtime else None
    return {
        "release": git_release(),
        "index_mtime": iso,
        "index_mtime_unix": mtime,
    }


def _update_status() -> dict[str, Any]:
    path = settings.data_dir / "update.status"
    log_path = settings.data_dir / "update.log"
    st: dict[str, Any] = {"ok": None, "running": False, "ts": None, "reason": "", "log_tail": ""}
    if path.exists():
        try:
            st.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            pass
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            st["log_tail"] = "\n".join(lines[-12:])
        except Exception:
            pass
    return st


def _positions_payload(open_pos: list, last_cycle: dict | None) -> list[dict]:
    hints = {}
    kalshi_by = {}
    for row in (last_cycle or {}).get("exit_log") or []:
        q = (row.get("question") or "")[:80]
        hints[q] = row
        cid = row.get("condition_id")
        if cid:
            hints[f"{cid}:{row.get('side') or ''}"] = row
    for row in (last_cycle or {}).get("kalshi_log") or []:
        cid = row.get("condition_id")
        if cid:
            kalshi_by[str(cid)] = row
    out = []
    for p in open_pos:
        cost = float(p.get("shares") or 0) * float(p.get("avg_cost") or 0)
        cv = p.get("current_value")
        try:
            mtm = float(cv) if cv not in (None, "") else None
        except (TypeError, ValueError):
            mtm = None
        upnl = round(mtm - cost, 2) if mtm is not None else None
        q = (p.get("question") or "")[:80]
        cid = p.get("condition_id")
        hint = hints.get(f"{cid}:{p.get('side') or ''}") or hints.get(q) or {}
        out.append(
            {
                "question": p.get("question"),
                "outcome": p.get("outcome"),
                "side": p.get("side"),
                "shares": p.get("shares"),
                "avg_cost": p.get("avg_cost"),
                "cur_price": p.get("cur_price"),
                "current_value": mtm,
                "cost": round(cost, 2),
                "upnl": upnl,
                "category": p.get("category"),
                "opened_ts": p.get("opened_ts"),
                "last_ts": p.get("last_ts"),
                "exit_action": hint.get("action"),
                "exit_reason": hint.get("reason"),
                "kalshi_ticker": (kalshi_by.get(str(cid or "")) or {}).get("ticker"),
                "kalshi_yes": (kalshi_by.get(str(cid or "")) or {}).get("kalshi"),
                "kalshi_gap": (kalshi_by.get(str(cid or "")) or {}).get("gap"),
            }
        )
    return out


def _state() -> dict[str, Any]:
    desk = _desk
    mark = desk.store.latest_mark() if desk else None
    open_pos = desk.store.positions("open") if desk else []
    snap_cash = desk.store.float_meta("last_cash") if desk else None
    if mark:
        raw_cash = float(mark["bankroll"])
    elif snap_cash is not None:
        raw_cash = snap_cash
    else:
        raw_cash = settings.paper_bankroll_usd if settings.dry_run else 0.0
    halt = settings.halt_file.exists()
    st: dict[str, Any] = {}
    bankroll = raw_cash
    equity = raw_cash
    if desk:
        try:
            st = desk.store.portfolio_stats(raw_cash, raw_cash, open_pos)
            bankroll = float(st.get("cash") if st.get("cash") is not None else raw_cash)
            equity = float(st.get("equity") if st.get("equity") is not None else bankroll)
            deposited = float(st.get("deposited") or 0)
            if deposited >= 1:
                st["total"] = round(equity - deposited, 2)
                st["total_pct"] = round(st["total"] / deposited, 4)
                st["after_xai"] = round(st["total"] - float(st.get("xai_total") or 0), 2)
        except Exception as exc:
            log.exception("portfolio_stats: %s", exc)
    spent = float((st or {}).get("xai_total") or 0)
    prepaid = float((st or {}).get("xai_prepaid") or 0)
    remaining = max(0.0, prepaid - spent) if prepaid > 0 else _xai_remaining(spent)
    fills = desk.store.recent_fills(30, real_only=not settings.dry_run) if desk else []
    return {
        "dry_run": settings.dry_run,
        "halted": halt,
        "busy": bool(desk and desk.busy),
        "running": bool(desk) and not halt,
        "bankroll": bankroll,
        "equity": equity,
        "open": len(open_pos),
        "max_open": settings.max_open_positions,
        "loop_seconds": settings.loop_seconds,
        "signature_type": settings.signature_type,
        "min_net_edge": settings.min_net_edge,
        "max_position_pct": settings.max_position_pct,
        "last_error": desk.last_error if desk else None,
        "last_cycle": desk.last_cycle if desk else None,
        "positions": _positions_payload(open_pos, desk.last_cycle if desk else None),
        "decisions": desk.store.recent_decisions(60) if desk else [],
        "fills": fills,
        "equity_history": desk.store.equity_history(120) if desk else [],
        "stats": st,
        "xai_remaining": remaining,
        "auth_required": bool(settings.dashboard_token),
        "rules": {
            "live": not settings.dry_run,
            "loop_min": round(settings.loop_seconds / 60, 1),
            "min_net_edge": settings.min_net_edge,
            "max_pos_pct": settings.max_position_pct,
            "kelly": settings.kelly_fraction,
            "max_open": settings.max_open_positions,
            "grok": settings.grok_model,
            "kalshi": True,
            "sig": settings.signature_type,
            "batch": settings.estimate_batch,
        },
        "version": _version(),
        "update": _update_status(),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        if args and str(args[0]).startswith("GET /api/state"):
            return
        log.info(fmt, *args)

    def _ok_token(self, value: str | None) -> bool:
        want = settings.dashboard_token
        return bool(want) and value == want

    def _authorized(self) -> bool:
        if not settings.dashboard_token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and self._ok_token(auth[7:].strip()):
            return True
        if self._ok_token(self.headers.get("X-Dashboard-Token")):
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        if "desk_token" in cookie and self._ok_token(cookie["desk_token"].value):
            return True
        q = parse_qs(urlparse(self.path).query)
        if self._ok_token((q.get("token") or [None])[0]):
            return True
        return False

    def _send(self, code: int, body: bytes, content_type: str, extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra or []:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        raw = json.dumps(payload, default=str).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)
        token_q = (q.get("token") or [None])[0]
        if path in {"/", "/index.html"}:
            extra = []
            if self._ok_token(token_q):
                extra.append(("Set-Cookie", f"desk_token={token_q}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"))
            html = (WEB / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8", extra)
            return
        if not self._authorized():
            self._json(401, {"error": "token required"})
            return
        if path == "/api/state":
            self._json(200, _state())
            return
        if path == "/api/version":
            self._json(200, _version())
            return
        if path == "/api/update":
            self._json(200, _update_status())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._do_post()
        except Exception as exc:
            log.exception("POST %s", self.path)
            try:
                self._json(500, {"ok": False, "reason": str(exc)})
            except Exception:
                pass

    def _do_post(self) -> None:
        if not self._authorized():
            self._json(401, {"error": "token required"})
            return
        path = urlparse(self.path).path
        if path == "/api/off":
            settings.halt_file.write_text("halt\n", encoding="utf-8")
            log.warning("HALT fra dashboard")
            self._json(200, {"ok": True, "halted": True})
            return
        if path == "/api/on":
            if settings.halt_file.exists():
                settings.halt_file.unlink()
            log.info("HALT fjernet fra dashboard")
            self._json(200, {"ok": True, "halted": False})
            return
        if path == "/api/once":
            if _desk is None:
                self._json(500, {"ok": False, "reason": "desk ikke klar"})
                return
            if settings.halt_file.exists():
                self._json(400, {"ok": False, "reason": "Agenten er stoppet. Trykk Slå på først."})
                return
            if not _desk.begin_cycle_async():
                self._json(409, {"ok": False, "reason": "En syklus kjører allerede — vent til den er ferdig."})
                return
            self._json(200, {"ok": True, "started": True})
            return
        if path == "/api/update":
            root = settings.halt_file.parent
            script = root / "deploy" / "update.sh"
            if not script.exists():
                self._json(500, {"ok": False, "reason": "deploy/update.sh mangler"})
                return
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            log_path = settings.data_dir / "update.log"
            status_path = settings.data_dir / "update.status"
            staged = Path("/tmp/polymarket-desk-update.sh")
            log.warning("Kodeoppdatering fra dashboard")
            try:
                shutil.copy2(script, staged)
                staged.chmod(0o755)
            except Exception as exc:
                self._json(500, {"ok": False, "reason": f"kunne ikke stage update.sh: {exc}"})
                return
            status_path.write_text(
                json.dumps(
                    {
                        "ok": None,
                        "running": True,
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "reason": "henter kode",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            def _run() -> None:
                env = os.environ.copy()
                env["ROOT"] = str(root)
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(f"\n==== dashboard {datetime.now(timezone.utc).isoformat()} ====\n")
                    fh.flush()
                    try:
                        if shutil.which("systemd-run"):
                            cmd = [
                                "systemd-run",
                                "--no-block",
                                "--collect",
                                f"--setenv=ROOT={root}",
                                "/bin/bash",
                                str(staged),
                            ]
                            subprocess.run(cmd, cwd=str(root), stdout=fh, stderr=subprocess.STDOUT, check=False)
                            return
                        proc = subprocess.run(
                            ["bash", str(staged)],
                            cwd=str(root),
                            stdout=fh,
                            stderr=subprocess.STDOUT,
                            env=env,
                            start_new_session=True,
                        )
                        if proc.returncode != 0:
                            status_path.write_text(
                                json.dumps(
                                    {
                                        "ok": False,
                                        "running": False,
                                        "ts": datetime.now(timezone.utc).isoformat(),
                                        "reason": f"exit {proc.returncode}",
                                    }
                                )
                                + "\n",
                                encoding="utf-8",
                            )
                    except FileNotFoundError:
                        status_path.write_text(
                            json.dumps(
                                {
                                    "ok": False,
                                    "running": False,
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                    "reason": "bash mangler",
                                }
                            )
                            + "\n",
                            encoding="utf-8",
                        )
                    except Exception as exc:
                        log.exception("update.sh")
                        status_path.write_text(
                            json.dumps(
                                {
                                    "ok": False,
                                    "running": False,
                                    "ts": datetime.now(timezone.utc).isoformat(),
                                    "reason": str(exc),
                                }
                            )
                            + "\n",
                            encoding="utf-8",
                        )

            threading.Thread(target=_run, daemon=True, name="desk-update").start()
            self._json(200, {"ok": True, "reason": "henter kode og restarter", "log": str(log_path)})
            return
        if path == "/api/meta":
            if _desk is None:
                self._json(500, {"ok": False, "reason": "desk ikke klar"})
                return
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            try:
                payload = json.loads(raw or "{}")
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "reason": "ugyldig json"})
                return
            if "deposited_usd" in payload:
                val = float(payload["deposited_usd"])
                if val < 1:
                    self._json(400, {"ok": False, "reason": "innskutt må være ≥ 1"})
                    return
                _desk.store.set_meta("deposited_usd", f"{val:.2f}")
            if "xai_prepaid_usd" in payload:
                val = float(payload["xai_prepaid_usd"])
                if val < 0:
                    self._json(400, {"ok": False, "reason": "xAI-kreditt kan ikke være negativ"})
                    return
                _desk.store.set_meta("xai_prepaid_usd", f"{val:.2f}")
            log.info("Meta oppdatert fra dashboard")
            self._json(200, {"ok": True, "state": _state()})
            return
        self._json(404, {"error": "not found"})


def start_in_thread(desk: Any) -> None:
    global _desk
    _desk = desk
    host = "0.0.0.0"
    port = settings.dashboard_port
    httpd = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    ip = lan_ip()
    log.info("Dashboard PC:    http://127.0.0.1:%s", port)
    log.info("Dashboard nett:  http://%s:%s", ip, port)
    if settings.dashboard_token:
        log.info("Dashboard er token-beskyttet (DASHBOARD_TOKEN)")
    else:
        log.warning("DASHBOARD_TOKEN er tom. Sett den før du åpner porten på en server.")
