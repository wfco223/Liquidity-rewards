"""The scan (owner, 2026-10-05: "have a button for me to scan for new
places to place orders. Give me a pop up with the politics end points.
Then show me the results of the scan and let me pick markets to track").

The pop-up lists the exchange's politics reward programs — every program
paying on a market discovery found under the politics and elections tags,
with its market count and its pool. He ticks the ones to scan. The scan
then, on its own thread:

1. re-reads the reward terms of those markets (the signed incentives api,
   never the throttled gateway);
2. reads their books over the websocket — a connection carries ten
   subscriptions of 200 markets, so up to three connections open for the
   scan and close when it is done — with a few gateway reads, one try
   each, for markets the stream did not deliver;
3. prices a new order on each side the way his market card does: his
   stake joining the side's best price, the meter's arithmetic, and that
   per dollar it ties up.

It READS ONLY. Nothing here places, moves or cancels an order; a market
he picks is added to his tracked list and nothing more.
"""

from __future__ import annotations

import json
import re
import threading
import time

from v3.books import BookCache
from v3.estimator import BOOK_MAX_AGE

SCAN_MAX = 6000            # markets one scan reads (three connections of 2,000)
SCAN_CONN = 2000           # a connection: ten subscriptions of 200
SCAN_CHUNK = 200
SCAN_WAIT_S = 45.0         # the longest the stream is given to deliver
SCAN_QUIET_S = 8.0         # ...or this long with nothing new arriving
GATEWAY_FILL = 60          # stragglers read through the gateway, one try each
TERMS_CHUNK = 400
SCAN_SHOW = 200            # rows a results page carries
NOT_POLITICS = ("macro", "econ", "fed", "cpi", "nfl", "cfb", "nba", "nhl", "mlb", "golf",
                "culture")
WORDS = {"bop": "balance of power", "gov": "governor", "seatcounts": "seat counts",
         "tossup": "toss-up"}


def group_key(pid: str) -> str:
    """A program's family, its date stamp taken off: the exchange re-issues
    a program under a new date and it is still the same program to him."""
    return re.sub(r"_?\d{8}$", "", str(pid or ""))


def group_label(key: str) -> str:
    w = [x for x in key.split("_") if x]
    if not w:
        return "no name"
    head, rest = [w[0].capitalize()], w[1:]
    if rest and re.fullmatch(r"t\d", rest[0]):
        head.append(f"Tier {rest[0][1]}")
        rest = rest[1:]
    words = " ".join(WORDS.get(x, x) for x in rest)
    return " ".join(head) + (f" · {words}" if words else "")


def ws_books(cache: BookCache, slugs: list[str], key_id: str, secret_key: str,
             progress=None, wait_s: float = SCAN_WAIT_S, quiet_s: float = SCAN_QUIET_S) -> str:
    """Books for `slugs` over the websocket, as fast as the exchange sends
    its snapshots; every connection closes when it returns. Returns a
    note on what did not come ("" when nothing to say)."""
    try:
        import asyncio
        import websockets
    except ImportError:
        return "the websocket library is not installed"
    from v3.api import auth_headers
    from v3.ws import WS_MAX_SUBS, WS_PATH, WS_URL, Stream

    want = set(slugs)
    got: set[str] = set()
    t0 = time.time()
    last_new = [t0]
    notes: list[str] = []

    def done() -> bool:
        t = time.time()
        return (got >= want or t - t0 > wait_s
                or (bool(got) and t - last_new[0] > quiet_s))

    async def one(i: int, part: list[str]) -> None:
        st = Stream(cache, lambda: part, key_id, secret_key, shard=i, cap=len(part),
                    chunk=SCAN_CHUNK, lite=False, max_subs=WS_MAX_SUBS, name=f"scan-{i}")
        headers = auth_headers(key_id, secret_key, "GET", WS_PATH)
        try:
            conn = websockets.connect(WS_URL, additional_headers=headers)
        except TypeError:
            conn = websockets.connect(WS_URL, extra_headers=headers)
        try:
            async with conn as ws:
                for req in st._requests(part):
                    await ws.send(json.dumps(req))
                while not done():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    s = st.apply_frame(raw)
                    if s and s in want and s not in got:
                        got.add(s)
                        last_new[0] = time.time()
                        if progress:
                            progress(len(got))
        except Exception as e:  # noqa: BLE001 — the others carry on
            notes.append(f"connection {i + 1}: {str(e)[:120]}")
        if st.refused:
            n = sum(k for k, _w in st.refused.values())
            notes.append(f"connection {i + 1}: {n} markets refused "
                         f"({next(iter(st.refused.values()))[1][:80]})")

    async def main() -> None:
        parts = [slugs[i:i + SCAN_CONN] for i in range(0, len(slugs), SCAN_CONN)]
        await asyncio.gather(*(one(i, p) for i, p in enumerate(parts)))

    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        notes.append(str(e)[:160])
    return "; ".join(notes)


class Scanner:
    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.book_source = ws_books       # tests hand in their own
        self.state = "idle"               # idle | running | done | error
        self.step = ""
        self.done_n = 0
        self.total = 0
        self.started = 0.0
        self.finished = 0.0
        self.stake = 0.0
        self.keys: list[str] = []
        self.rows: list[dict] = []
        self.counts: dict = {}
        self.note = ""

    # -- the pop-up ------------------------------------------------------------

    def groups(self) -> list[dict]:
        """The politics programs paying now on markets discovery found,
        biggest pool first."""
        from .app import is_econ_market
        app = self.app
        by: dict[str, dict] = {}
        for s in list(app.universe):
            if is_econ_market(s):
                continue
            p = app.terms.get(s)
            if p is None or not p.is_live() or not p.pid:
                continue
            k = group_key(p.pid)
            if not k or k.startswith(NOT_POLITICS):
                continue
            g = by.setdefault(k, {"key": k, "label": group_label(k), "n": 0,
                                  "pool": 0.0, "target": 0.0})
            g["n"] += 1
            g["pool"] = max(g["pool"], float(p.pool or 0))
            g["target"] = max(g["target"], float(p.target or 0))
        return sorted(by.values(), key=lambda g: (-g["pool"], g["label"]))

    def _pick(self, keys: list[str]) -> list[str]:
        from .app import is_econ_market
        app = self.app
        want = set(keys)
        out = []
        for s in list(app.universe):
            p = app.terms.get(s)
            if (p is not None and p.is_live() and group_key(p.pid) in want
                    and not is_econ_market(s)):
                out.append((-(app.side_pool(s, p) or 0.0), s))
        return [s for _k, s in sorted(out)]

    # -- running ---------------------------------------------------------------

    def start(self, keys, stake) -> dict:
        keys = [str(k) for k in (keys or []) if str(k)]
        known = {g["key"] for g in self.groups()}
        keys = [k for k in keys if k in known]
        if not keys:
            return {"ok": False, "note": "pick at least one"}
        stake = self.app.stake_of(stake)
        with self.lock:
            if self.state == "running":
                return {"ok": False, "note": f"a scan is already running ({self.step})"}
            self.state, self.step, self.done_n, self.total = "running", "starting", 0, 0
            self.started, self.finished, self.note = self.app.clock(), 0.0, ""
            self.stake, self.keys = stake, keys
        threading.Thread(target=self._run, args=(keys, stake), daemon=True,
                         name="scan").start()
        return {"ok": True, "note": "scanning"}

    def _run(self, keys: list[str], stake: float) -> None:
        app = self.app
        try:
            slugs = self._pick(keys)
            dropped = max(len(slugs) - SCAN_MAX, 0)
            slugs = slugs[:SCAN_MAX]
            self.step, self.total, self.done_n = "reading reward terms", len(slugs), 0
            bad = 0
            for i in range(0, len(slugs), TERMS_CHUNK):
                if not app.refresh_terms(app.clock(), slugs[i:i + TERMS_CHUNK]):
                    bad += 1
                self.done_n = min(i + TERMS_CHUNK, len(slugs))
            slugs = [s for s in slugs if (p := app.terms.get(s)) is not None and p.is_live()]
            self.step, self.total, self.done_n = "reading books", len(slugs), 0
            cache = BookCache()
            ws_note = self.book_source(cache, slugs, app.client.key_id, app.client.secret_key,
                                       progress=lambda n: setattr(self, "done_n", n))
            now = app.clock()
            for s in slugs:                  # what the app already holds fresh
                if cache.any_age(s) is None and (b := app.cache.fresh(s, BOOK_MAX_AGE, now)):
                    cache.put(s, b)
            missing = [s for s in slugs if cache.any_age(s) is None]
            read = 0
            for s in missing[:GATEWAY_FILL]:
                if app.client.gateway_hold() > 0:
                    break
                try:
                    cache.put(s, app.client.book(s, timeout=8.0, tries=1))
                    read += 1
                except Exception as e:  # noqa: BLE001
                    if getattr(e, "status", None) == 429:
                        break
            self.done_n = sum(1 for s in slugs if cache.any_age(s) is not None)
            self.step = "pricing"
            now = app.clock()
            first = set(app.terms.joined_today(now))
            from .app import classify
            rows, no_book = [], 0
            for s in slugs:
                b = cache.any_age(s)
                if b is None:
                    no_book += 1
                    continue
                p = app.terms.get(s)
                pool = app.side_pool(s, p) if p is not None else None
                kind, st = classify(s)
                rows.append({
                    "m": s, "name": app.label(s), "kind": kind, "st": st, "ev": app.event_of(s),
                    "pool": pool, "target": p.target if p is not None else None,
                    "bid": app.potential(s, "BUY", b, p, pool, stake, first=s in first),
                    "ask": app.potential(s, "SELL", b, p, pool, stake, first=s in first),
                })
            paying = sum(1 for r in rows if _best(r, "day") > 0)
            notes = []
            if dropped:
                notes.append(f"{dropped:,} markets past the first {SCAN_MAX:,} (smallest pools) "
                             f"were not read — pick fewer programs to reach them")
            if bad:
                notes.append(f"{bad} terms reads failed — those markets kept their old terms")
            if no_book:
                notes.append(f"{no_book:,} markets sent no book")
            if ws_note:
                notes.append(ws_note)
            with self.lock:
                self.rows = rows
                self.counts = {"markets": len(slugs), "books": len(rows), "no_book": no_book,
                               "paying": paying, "gateway": read, "dropped": dropped}
                self.note = "; ".join(notes)
                self.state, self.step, self.finished = "done", "", app.clock()
            app.note(f"scan: {len(slugs)} markets, {len(rows)} books, {paying} would pay"
                     + (f" — {self.note}" if self.note else ""))
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.state, self.step, self.note = "error", "", str(e)[:200]
                self.finished = app.clock()
            app.note(f"scan: {e}")

    # -- what the page reads -----------------------------------------------------

    def brief(self) -> dict:
        return {"state": self.state, "step": self.step, "done": self.done_n,
                "total": self.total, "finished": round(self.finished, 1)}

    def view(self, sort: str = "pct", limit: int = SCAN_SHOW) -> dict:
        app = self.app
        rows = [r for r in self.rows if _best(r, "day") > 0]
        if sort in ("bid_day", "ask_day", "bid_pct", "ask_pct"):
            sd, k = sort.split("_")
            sd = "bid" if sd == "bid" else "ask"
            rows.sort(key=lambda r: -(r[sd].get(k) or 0.0))
        elif sort == "day":
            rows.sort(key=lambda r: -_best(r, "day"))
        else:
            rows.sort(key=lambda r: -_best(r, "pct"))
        shown = rows[:max(int(limit or SCAN_SHOW), 1)]
        mine = set(app.order_markets()) | set(app.held())
        return {
            "ok": True, **self.brief(), "started": round(self.started, 1),
            "stake": self.stake, "keys": list(self.keys), "counts": dict(self.counts),
            "note": self.note, "groups": self.groups(), "of": len(rows),
            "rows": [{**r, "w": r["m"] in app.watch, "mine": r["m"] in mine} for r in shown],
        }


def _best(r: dict, k: str) -> float:
    return max((r.get(sd) or {}).get(k) or 0.0 for sd in ("bid", "ask"))
