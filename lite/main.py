"""Start the simple app: the page first (so the platform's health check
answers at once), then the saved state, the books, and only then the
meter — a sample taken before any book is cached would bill the restart
gap at nothing and dip the graph to zero on every deploy."""

from __future__ import annotations

import signal
import sys
import threading
import time

from .app import RECORDS_S, SAMPLE_S, UPKEEP_S, App
from .web import serve


def main() -> int:
    app = App()
    serve(app)

    def stop(signum, frame):  # noqa: ARG001
        app.note(f"stop signal {signum} — saving")
        app.shutdown_save(f"signal {signum}")
        sys.exit(0)

    # before the restore: a stop during it saves nothing (App.save waits
    # for the restore) and exits, rather than being killed mid-read
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    app.restore()
    app.note(f"started — restored from {app.restored}")
    app._read_orders()
    app.refresh_positions(time.time())
    app.start_streams()
    app.refresh_books(time.time())
    app._loop("sampler", SAMPLE_S, app.sample_once, first_delay=SAMPLE_S)
    app._loop("upkeep", UPKEEP_S, app.upkeep_once)
    app._loop("records", RECORDS_S, app.records_once)
    threading.Thread(target=app.discover_loop, daemon=True, name="discover").start()
    while True:
        threading.Event().wait(3600)


if __name__ == "__main__":
    sys.exit(main())
