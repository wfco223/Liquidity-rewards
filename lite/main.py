"""Start the simple app: the page first (so the platform's health check
answers at once), then the saved state, the reads and the meter."""

from __future__ import annotations

import signal
import sys
import threading
import time

from .app import (DISCOVER_S, SAMPLE_S, UPKEEP_S, App)
from .web import serve


def main() -> int:
    app = App()
    serve(app)
    app.restore()
    app.note(f"started — restored from {app.restored}")

    def stop(signum, frame):  # noqa: ARG001
        app.note(f"stop signal {signum} — saving")
        app.shutdown_save(f"signal {signum}")
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    app.refresh_positions(time.time())
    app.sample_once()
    app.start_streams()
    app._loop("sampler", SAMPLE_S, app.sample_once)
    app._loop("upkeep", UPKEEP_S, app.upkeep_once)
    app._loop("discover", DISCOVER_S, app.run_discover)
    while True:
        threading.Event().wait(3600)


if __name__ == "__main__":
    sys.exit(main())
