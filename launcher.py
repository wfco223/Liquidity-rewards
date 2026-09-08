#!/usr/bin/env python3
"""Runs 1.0 and 2.0 side by side in the one container (one $5 app —
owner's decision, no second subscription).

1.0's monitor keeps the public HTTP port and stays the front door; its
/v2/* route forwards to the 2.0 process on localhost. 2.0 runs
read-only — it has no code path to an order endpoint in this phase.

A child that exits is restarted with backoff (5s doubling to 60s,
reset after ten healthy minutes). SIGTERM stops both and exits, so
platform redeploys stay clean. 2.0 is skipped, with a line saying so,
when V2_ENABLED=0 or the exchange keys are missing — 1.0 alone must
keep working no matter what.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HEALTHY_RESET_S = 600.0
BACKOFF_MIN_S, BACKOFF_MAX_S = 5.0, 60.0


def children() -> dict[str, list[str]]:
    procs = {}
    if os.environ.get("V1_ENABLED", "0") == "0":
        # RETIRED by default (owner, 2026-08-21: "get the important bits of
        # 1.0 ported over so we can kill it"). 3.0 is the front door now.
        # Set V1_ENABLED=1 to resurrect the old monitor and its /map.
        print("launcher: 1.0 retired — V1_ENABLED=1 resurrects it", flush=True)
    else:
        procs["1.0"] = [sys.executable, "-u",
                        os.path.join(HERE, "live", "monitor.py")]
    if os.environ.get("V2_ENABLED", "0") == "0":
        # RETIRED by default (owner, 2026-08-21: "go ahead and start the
        # ports") — 3.0 carries the payout watcher now. Set V2_ENABLED=1
        # to resurrect the seats engine and its pages.
        print("launcher: 2.0 retired — V2_ENABLED=1 resurrects it", flush=True)
    elif not (os.environ.get("POLYMARKET_KEY_ID") and os.environ.get("POLYMARKET_SECRET_KEY")):
        print("launcher: 2.0 skipped — exchange keys not set", flush=True)
    else:
        procs["2.0"] = [sys.executable, "-u", "-m", "v2.main"]
    if os.environ.get("V3_ENABLED", "1") == "0":
        print("launcher: 3.0 disabled by V3_ENABLED=0", flush=True)
    elif not (os.environ.get("POLYMARKET_KEY_ID") and os.environ.get("POLYMARKET_SECRET_KEY")):
        print("launcher: 3.0 skipped — exchange keys not set", flush=True)
    else:
        procs["3.0"] = [sys.executable, "-u", "-m", "v3.main"]
    return procs


def main() -> int:
    cmds = children()
    procs: dict[str, subprocess.Popen] = {}
    started: dict[str, float] = {}
    backoff: dict[str, float] = {n: BACKOFF_MIN_S for n in cmds}
    stopping = False

    def stop(signum, frame):  # noqa: ARG001
        nonlocal stopping
        stopping = True
        for p in procs.values():
            if p.poll() is None:
                p.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def start(name: str) -> None:
        print(f"launcher: starting {name}", flush=True)
        procs[name] = subprocess.Popen(cmds[name], cwd=HERE)
        started[name] = time.time()

    for name in cmds:
        start(name)

    while not stopping:
        time.sleep(5)
        for name, p in list(procs.items()):
            if p.poll() is None:
                if time.time() - started[name] > HEALTHY_RESET_S:
                    backoff[name] = BACKOFF_MIN_S
                continue
            print(f"launcher: {name} exited with {p.returncode}; "
                  f"restarting in {backoff[name]:.0f}s", flush=True)
            # how it ended, for the next boot to report (2026-09-08: the
            # app restarted every 12-35 minutes with nobody deploying;
            # -9 is the platform's kill, -15 a stop, 1 a crash)
            try:
                import json
                with open(os.environ.get("V3_EXIT_PATH",
                                         os.path.join(HERE, "v3_last_exit.json")), "w") as f:
                    json.dump({"name": name, "returncode": p.returncode,
                               "at": time.time(),
                               "uptime_s": round(time.time() - started[name], 1)}, f)
            except OSError:
                pass
            time.sleep(backoff[name])
            backoff[name] = min(backoff[name] * 2, BACKOFF_MAX_S)
            if not stopping:
                start(name)

    for p in procs.values():
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
