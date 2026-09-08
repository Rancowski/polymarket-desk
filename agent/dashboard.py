"""Lokal Fjord-dashboard. Ingen nøkler ut av PCen."""
from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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


def _state() -> dict[str, Any]:
    desk = _desk
    mark = desk.store.latest_mark() if desk else None
    open_pos = desk.store.positions("open") if desk else []
    locked = sum(float(p.get("shares") or 0) * float(p.get("avg_cost") or 0) for p in open_pos)
    bankroll = float(mark["bankroll"]) if mark else settings.paper_bankroll_usd
    equity = float(mark["equity"]) if mark else bankroll + locked
    halt = settings.halt_file.exists()
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
        "equity_history": desk.store.equity_history(48) if desk else [],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        if args and str(args[0]).startswith("GET /api/state"):
            return
        log.info(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        raw = json.dumps(payload, default=str).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            html = (WEB / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/api/state":
            self._json(200, _state())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
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
            if _desk.busy:
                self._json(409, {"ok": False, "reason": "syklus kjører"})
                return
            result = _desk.cycle()
            self._json(200, result)
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
    log.info("Dashboard mobil: http://%s:%s  (samme WiFi)", ip, port)
