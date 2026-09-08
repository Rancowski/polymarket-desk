#!/usr/bin/env python3
"""Oppdater handelsparametre i .env uten å røre nøkler."""
from __future__ import annotations

import sys
from pathlib import Path

PATCH = {
    "MAX_POSITION_PCT": "0.12",
    "MIN_NET_EDGE": "0.012",
    "MODEL_HAIRCUT": "0",
    "KELLY_FRACTION": "0.25",
    "MAX_OPEN_POSITIONS": "12",
    "MAX_CATEGORY_PCT": "0.40",
    "DAILY_LOSS_HALT_PCT": "0.06",
    "WEEKLY_LOSS_HALT_PCT": "0.15",
    "MIN_LIQUIDITY_USD": "1500",
    "MIN_VOLUME_24H_USD": "500",
    "MIN_BOOK_MULTIPLE": "3",
    "MAX_SPREAD": "0.08",
    "LOOP_SECONDS": "900",
    "ESTIMATE_BATCH": "12",
    "LIVE_SEARCH": "false",
}


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else ".env")
    if not path.exists():
        raise SystemExit(f"mangler {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in PATCH:
            out.append(f"{key}={PATCH[key]}")
            seen.add(key)
        else:
            out.append(line)
    missing = [k for k in PATCH if k not in seen]
    if missing:
        out.append("")
        out.append("# === Aggressiv profil (auto) ===")
        for k in missing:
            out.append(f"{k}={PATCH[k]}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("Patchet", path, ":", ", ".join(PATCH))


if __name__ == "__main__":
    main()
