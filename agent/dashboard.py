"""Fjord-dashboard. Token-beskyttet når DASHBOARD_TOKEN er satt."""
from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from agent.config import settings

log = logging.getLogger("dash")
WEB = Path(__file__).resolve().parent / "web"
_desk = None


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
    key = settings.xai_management_key
    if key:
        try:
            r = requests.get(
                f"https://management-api.x.ai/v1/billing/teams/{settings.xai_team_id or 'default'}/prepaid/balance",
                headers={"Authorization": f"Bearer {key}"},
                timeout=6,
            )
            if r.ok:
                data = r.json() if isinstance(r.json(), dict) else {}
                for k in ("balance_usd", "available_usd", "balance"):
                    if k in data:
                        return float(data[k])
                if "cents" in data:
                    return float(data["cents"]) / 100.0
                if "balance_cents" in data:
                    return float(data["balance_cents"]) / 100.0
        except Exception:
            pass
    if settings.xai_prepaid_usd > 0:
        return max(0.0, settings.xai_prepaid_usd - spent)
    return None


def _state() -> dict[str, Any]:
    desk = _desk
    mark = desk.store.latest_mark() if desk else None
    open_pos = desk.store.positions("open") if desk else []
    locked = sum(float(p.get("shares") or 0) * float(p.get("avg_cost") or 0) for p in open_pos)
    bankroll = float(mark["bankroll"]) if mark else settings.paper_bankroll_usd
    equity = float(mark["equity"]) if mark else bankroll + locked
    halt = settings.halt_file.exists()
    st: dict[str, Any] = {}
    if desk:
        try:
            st = desk.store.portfolio_stats(equity, bankroll, open_pos)
            if st.get("cash") is not None:
                bankroll = float(st["cash"])
            if st.get("open_cost") is not None:
                equity = bankroll + float(st["open_cost"])
                st["total"] = round(equity - float(st.get("deposited") or st.get("start_equity") or equity), 2)
                start = float(st.get("deposited") or st.get("start_equity") or 0) or equity
                st["total_pct"] = round(st["total"] / start, 4) if start else 0
        except Exception as exc:
            log.exception("portfolio_stats: %s", exc)
    spent = float((st or {}).get("xai_total") or 0)
    prepaid = float((st or {}).get("xai_prepaid") or 0)
    remaining = max(0.0, prepaid - spent) if prepaid > 0 else _xai_remaining(spent)
    return {
        "dry_run": settings.dry_run,
        "halted": halt,
        "busy": bool(desk and desk.busy),
        "running": True,
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
        "positions": [
            {
                "question": p.get("question"),
                "side": p.get("side"),
                "shares": p.get("shares"),
                "avg_cost": p.get("avg_cost"),
                "category": p.get("category"),
                "last_ts": p.get("last_ts"),
            }
            for p in open_pos
        ],
        "decisions": desk.store.recent_decisions(60) if desk else [],
        "fills": desk.store.recent_fills(30) if desk else [],
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
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
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
            if _desk.busy:
                self._json(409, {"ok": False, "reason": "En syklus kjører allerede — vent til den er ferdig."})
                return
            _desk.busy = True

            def _run() -> None:
                try:
                    _desk.cycle()
                finally:
                    _desk.busy = False

            threading.Thread(target=_run, daemon=True, name="desk-once").start()
            self._json(200, {"ok": True, "started": True})
            return
        if path == "/api/update":
            script = settings.halt_file.parent / "deploy" / "update.sh"
            if not script.exists():
                self._json(500, {"ok": False, "reason": "deploy/update.sh mangler"})
                return
            log.warning("Kodeoppdatering fra dashboard")
            subprocess.Popen(
                ["bash", str(script)],
                cwd=str(settings.halt_file.parent),
                start_new_session=True,
            )
            self._json(200, {"ok": True, "reason": "henter kode og restarter"})
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
