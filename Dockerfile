FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt tzdata
COPY track_rewards.py scan_markets.py launcher.py ./
# scan_markets.py was missing from the image the whole 1.0 era — monitor.py
# imports it inside a silent try/except, so golf discovery quietly never ran
# in production. It ships now.
COPY live ./live
COPY v2 ./v2
COPY v3 ./v3
# the two race tables the map scores against, as a last-resort fallback:
# the monitor prefers the CDN, then the daily copy on main, then these
COPY data/silver_senate_races.csv data/silver_gov_races.csv ./data/
# one container, both versions: 1.0 keeps the public port and forwards
# /v2/* to the read-only 2.0 process (see launcher.py and v2/DESIGN.md)
# glibc keeps one malloc arena per busy thread and holds freed memory in
# them; with the state serialised across threads every minute the
# process grew 5 MB a minute and was killed near 500 MB (2026-09-08).
# Two arenas, and the monitor trims the heap every cycle (v3/box.py).
ENV MALLOC_ARENA_MAX=2
# 2026-09-09: with the trim in place, glibc's own account showed Python
# using 29 MB while resident memory climbed past 430 MB — the rest was
# Python's small-object arenas, handed out by the megabyte and given
# back only when a whole megabyte empties, so every burst (a programs
# read, the survey frame, a discovery pass) raised the high-water mark
# for good. Routing small objects through the system allocator lets the
# per-cycle trim hand that memory back too (owner yes, 2026-09-09).
# Withdrawn the same morning: under it the process sat flat at ~205 MB
# after each trim, but the peak WITHIN a cycle grew (295 MB at 06:42Z,
# 707 MB at 08:14Z) and the monitor went silent at 08:16Z — no state
# save, no hourly publish — which is what a boot that dies at its first
# cycle's peak looks like. The trim-per-cycle and two arenas stay.
# ENV PYTHONMALLOC=malloc
CMD ["python", "launcher.py"]
