"""The box under the app (2026-09-07: "there is like a 5 minute delay
when I do anything" — cycles of two to four minutes, a state build
that took 114 s on the box and 0.2 s off it). What the container
itself reports: memory used against its limit, kills for memory, CPU
time held back by the quota, the load; and how long Python's garbage
collector took. Read-only, never raises, works with cgroup v2 and v1."""

from __future__ import annotations

import gc
import os
import time

CG2 = "/sys/fs/cgroup"
CG1_MEM = "/sys/fs/cgroup/memory"
CG1_CPU = "/sys/fs/cgroup/cpu"


def _read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, ValueError):
        return None


def _kv(text: str | None) -> dict:
    out: dict = {}
    for ln in (text or "").splitlines():
        parts = ln.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def _mb(v) -> float | None:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    if v <= 0 or v >= 1 << 60:               # "max" / no limit
        return None
    return round(v / 1048576, 1)


def box_stats() -> dict:
    """One reading of the container's limits and counters (cumulative
    where the kernel keeps them so; the cycle takes deltas)."""
    out: dict = {}
    # memory
    if os.path.exists(f"{CG2}/memory.current"):
        out["mem_mb"] = _mb(_read(f"{CG2}/memory.current"))
        out["mem_max_mb"] = _mb(_read(f"{CG2}/memory.max"))
        out["mem_high_mb"] = _mb(_read(f"{CG2}/memory.high"))
        ev = _kv(_read(f"{CG2}/memory.events"))
        out["oom_kills"] = ev.get("oom_kill", 0)
        out["mem_high_hits"] = ev.get("high", 0)
        out["mem_max_hits"] = ev.get("max", 0)
    elif os.path.exists(f"{CG1_MEM}/memory.usage_in_bytes"):
        out["mem_mb"] = _mb(_read(f"{CG1_MEM}/memory.usage_in_bytes"))
        out["mem_max_mb"] = _mb(_read(f"{CG1_MEM}/memory.limit_in_bytes"))
        st = _kv(_read(f"{CG1_MEM}/memory.stat"))
        out["oom_kills"] = _kv(_read(f"{CG1_MEM}/memory.oom_control")).get("oom_kill", 0)
        out["mem_max_hits"] = int(_read(f"{CG1_MEM}/memory.failcnt") or 0) if _read(f"{CG1_MEM}/memory.failcnt") else 0
        if "total_rss" in st:
            out["mem_rss_mb"] = _mb(st["total_rss"])
    # cpu quota and throttling
    if os.path.exists(f"{CG2}/cpu.stat"):
        st = _kv(_read(f"{CG2}/cpu.stat"))
        out["nr_throttled"] = st.get("nr_throttled", 0)
        out["throttled_s_total"] = round(st.get("throttled_usec", 0) / 1e6, 1)
        mx = (_read(f"{CG2}/cpu.max") or "").split()
        if len(mx) == 2 and mx[0] != "max":
            try:
                out["cpu_quota"] = round(int(mx[0]) / int(mx[1]), 2)
            except ValueError:
                pass
    elif os.path.exists(f"{CG1_CPU}/cpu.stat"):
        st = _kv(_read(f"{CG1_CPU}/cpu.stat"))
        out["nr_throttled"] = st.get("nr_throttled", 0)
        out["throttled_s_total"] = round(st.get("throttled_time", 0) / 1e9, 1)
        try:
            q = int(_read(f"{CG1_CPU}/cpu.cfs_quota_us") or -1)
            p = int(_read(f"{CG1_CPU}/cpu.cfs_period_us") or 100000)
            if q > 0 and p > 0:
                out["cpu_quota"] = round(q / p, 2)
        except ValueError:
            pass
    try:
        out["load1"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    try:
        out["cpus"] = len(os.sched_getaffinity(0))
    except (OSError, AttributeError):
        out["cpus"] = os.cpu_count()
    return out


class GcClock:
    """How long the garbage collector has been running, by generation,
    since the last take. Hooks gc.callbacks once."""

    def __init__(self):
        self._t0 = 0.0
        self.seconds = 0.0
        self.runs = 0
        self.gen2 = 0
        self._hooked = False

    def hook(self) -> None:
        if not self._hooked:
            gc.callbacks.append(self._cb)
            self._hooked = True

    def _cb(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._t0 = time.time()
        elif phase == "stop":
            self.seconds += max(time.time() - self._t0, 0.0)
            self.runs += 1
            if info.get("generation") == 2:
                self.gen2 += 1

    def take(self) -> dict:
        out = {"s": round(self.seconds, 2), "runs": self.runs, "gen2": self.gen2,
               "objects": len(gc.get_objects()) if self.gen2 or self.runs > 50 else None}
        self.seconds = 0.0
        self.runs = 0
        self.gen2 = 0
        return out


def deep_mb(obj, budget: int = 300_000, seen: set | None = None) -> float:
    """Roughly how many MB a container and everything hanging off it
    hold, walking at most `budget` objects (a 300k walk keeps its
    bookkeeping under ~20 MB on a small box). Shared `seen` keeps two
    containers from counting the same objects twice."""
    import sys
    seen = set() if seen is None else seen
    total = 0
    stack = [obj]
    n = 0
    while stack and n < budget:
        o = stack.pop()
        i = id(o)
        if i in seen:
            continue
        seen.add(i)
        n += 1
        try:
            total += sys.getsizeof(o)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(o, dict):
            stack.extend(o.keys())
            stack.extend(o.values())
        elif isinstance(o, (list, tuple, set, frozenset)):
            stack.extend(o)
        elif hasattr(o, "__dict__") and not isinstance(o, type):
            stack.append(o.__dict__)
    return round(total / 1048576, 2)


def trim_heap() -> dict | None:
    """Hand the C allocator's freed memory back to the box (glibc's
    malloc_trim). 2026-09-08: resident memory climbed 5 MB a minute
    while the count of Python objects stayed flat, and the box killed
    the process near 500 MB every 20-35 minutes — the 12 MB state
    serialised every minute across several threads leaves holes the
    allocator keeps. Returns {before, after} in MB, or None where there
    is no glibc."""
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6")
    except (OSError, AttributeError):
        return None
    before = _rss_mb()
    try:
        libc.malloc_trim(0)
    except Exception:  # noqa: BLE001
        return None
    return {"before": before, "after": _rss_mb()}


def _rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(float(line.split()[1]) / 1024.0, 1)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def freeze_heap() -> int:
    """After a restore: the long-lived heap leaves the collector's
    scans (gc.freeze), so a full collection no longer walks hundreds
    of megabytes of state on every busy stretch. Returns what froze."""
    gc.collect()
    gc.freeze()
    return gc.get_freeze_count()
