"""Run TradePulse's background workers without the web server (independent deployment).

    python -m tradepulse.worker              # PRIMARY: Redis tail + reconciler (09:00/09:05) + scheduler +
                                             #          Telegram polling + processor + notifier
    python -m tradepulse.worker --replica    # extra capacity: processor + notifier only

Run exactly ONE primary. Replicas are safe to add: the ingest stream uses a consumer group and every
event/notification write is idempotent. Broker reads are capped globally (Redis slot) whatever runs.
"""

import argparse
import logging
import signal

from .config import settings
from .runtime import Runtime


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replica", "--no-bot", dest="replica", action="store_true",
                    help="processor + notifier only (no stream reader, schedules or Telegram polling)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    rt = Runtime(settings)
    rt.start_workers(primary=not args.replica)
    signal.signal(signal.SIGTERM, lambda *_: rt.stop.set())
    try:
        while not rt.stop.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    rt.shutdown()


if __name__ == "__main__":
    main()
