from __future__ import annotations

import argparse
import logging
import sys

from agent.config import settings
from agent.dashboard import start_in_thread
from agent.loop import Desk


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main() -> None:
    setup_logging()
    p = argparse.ArgumentParser(description="Autonom Polymarket-desk")
    p.add_argument("cmd", nargs="?", default="run", choices=["run", "once", "status"])
    args = p.parse_args()
    desk = Desk()
    if args.cmd == "status":
        pos = desk.store.positions("open")
        print(f"DRY_RUN={settings.dry_run} open={len(pos)} halt={settings.halt_file.exists()}")
        for row in pos:
            print(
                f"- {row['side']} {row['question'][:70]} shares={row['shares']} avg={row['avg_cost']}"
            )
        return
    if args.cmd == "once":
        desk.cycle()
        return
    start_in_thread(desk)
    desk.run_forever()


if __name__ == "__main__":
    main()
