from __future__ import annotations

import argparse
import logging
import sys

from agent.config import live_forbidden, settings
from agent.dashboard import start_in_thread
from agent.kalshi import pair_ok_selfcheck
from agent.loop import Desk


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main() -> None:
    setup_logging()
    pair_ok_selfcheck()
    p = argparse.ArgumentParser(description="Autonom Polymarket-desk")
    p.add_argument("cmd", nargs="?", default="run", choices=["run", "once", "status"])
    args = p.parse_args()
    block = live_forbidden()
    if args.cmd in {"run", "once"} and block:
        print(block, file=sys.stderr)
        raise SystemExit(2)
    desk = Desk()
    if args.cmd == "status":
        pos = desk.store.positions("open")
        print(f"DRY_RUN={settings.dry_run} open={len(pos)} halt={settings.halt_file.exists()}")
        print(desk.qa_report())
        for row in pos:
            print(
                f"- {row['side']} {row['question'][:70]} shares={row['shares']} avg={row['avg_cost']}"
            )
        return
    if args.cmd == "once":
        desk.cycle()
        print(desk.qa_report())
        return
    start_in_thread(desk)
    desk.run_forever()


if __name__ == "__main__":
    main()
