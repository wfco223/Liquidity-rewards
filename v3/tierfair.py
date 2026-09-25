"""Stage 1 of the tier engines: a fair for every midterm-tier market,
re-estimated every second, shown, and graded against where the price
goes (owner, 2026-09-25: "We have to get dynamic fairs. Things are
changing fast" ... "Yes" to stage 1; the design is v3/TIERS.md).

READ-ONLY. Nothing here places, moves or cancels an order, and nothing
here reads the exchange: it scores the books the stream and the
families already keep, the stream's last trade prices, the linked
markets of each event, and Silver's model.

The inputs, each a price for the YES side:
- book: the size-weighted price of the first FAIR_DEPTH_FRAC of the
  Target Size on each side (at least FAIR_DEPTH_MIN shares), the
  midpoint of the two — one small order at the touch cannot move it;
- print: the last trade price the stream saw change, within
  PRINT_MAX_AGE_S;
- linked: what the event's other markets imply — for outcomes that
  exclude each other, one less the sum of the others' book prices; for
  a ladder of "at least N" markets, the book prices made to fall as N
  rises (the closest falling fit);
- silver: Silver's model where it has a number.
The fair STARTS FROM THE TOUCH MIDPOINT and moves off it only by what
an input has shown it adds (owner, 2026-09-25 "Start the fair from the
midpoint", after the first build — the inputs' average, each weighted
by one over its own error — missed by 2.20c an hour ahead where the
plain midpoint missed by 0.13c, and did better in 1 of 97 markets).
For each input, per tier, the tally learns how much of its distance
from the midpoint the midpoint itself moves an hour later (the least-
squares share, between 0 and 1, shrunk toward 0 until it has many
readings, and 0 under BETA_MIN_N); the fair is the midpoint plus the
average of the proven inputs' shares of their distance. An input that
has proven nothing moves nothing, so the fair can only differ from the
midpoint where an input has earned it. Where the touch is wider than
MID_TRUST_SPREAD there is no midpoint to start from, and the fair is
the inputs' average weighted as before (one over each input's measured
error). The confidence (± cents) is the fair's own measured error an
hour ahead in the tier, the touch's half-width and how much the fair
moved in the last ten minutes.

Grading: every SNAP_S each market's fair, its inputs, his fair (the
focus page's, when set) and the plain touch midpoint are written down;
GRADE_HORIZONS later each is scored against the touch midpoint then —
the plain midpoint of then being the "nothing changes" forecast the
fair has to beat. A touch wider than MID_TRUST_SPREAD is no price to
grade against and is skipped. Errors decay with ERR_HALF_LIFE_S so the
weights follow the market."""

from __future__ import annotations

import json
import math
import re
import time
from collections import deque

TIERS = (("t1", "Tier 1", "midterms_t1_"),
         ("t2", "Tier 2", "midterms_t2_"),
         ("t3", "Tier 3", "midterms_t3_"),
         ("t4", "Tier 4", "midterms_t4_"))
INPUTS = ("book", "print", "linked", "silver")

FAIR_DEPTH_FRAC = 0.10        # the book input walks this share of the Target Size a side
FAIR_DEPTH_MIN = 100.0        # ...and never fewer shares than this
PRINT_MAX_AGE_S = 6 * 3600.0  # a last trade older than this is no input
BOOK_STALE_S = 600.0          # a book older than this gives no fair and is not graded
SNAP_S = 60.0                 # how often each market's fair is written down for grading
GRADE_HORIZONS = (600, 3600)  # graded this many seconds ahead
GRADE_SLACK_S = 180.0         # a reading is graded within this long past its horizon, else dropped
MID_TRUST_SPREAD = 0.10       # a touch wider than this is no price to grade against
WEIGHT_MIN_N = 30.0           # graded readings an input needs before its weight is its own
ERR_HALF_LIFE_S = 2 * 86400.0
BETA_MIN_N = 30.0             # graded readings an input needs before it may move the fair
BETA_SHRINK_N = 100.0         # its share is shrunk by n / (n + this): more readings, more trust
BETA_HORIZON = 3600           # the shares are learned against the midpoint this far ahead
# the fair's grade before 2026-09-25 was the inputs' average; the one
# after starts from the midpoint — its errors are kept apart
FAIR_VERSION = "mid-2026-09-25"
PRIOR_ERR = 0.02              # the error every input is assumed to have until measured ($)
VOL_WINDOW_S = 600.0
VOL_SAMPLE_S = 10.0           # the fair is kept for the volatility this often (memory)
PAGE_S = 5.0                  # the page's frozen view is rebuilt this often


def tier_of(pid: str | None) -> str | None:
    pid = str(pid or "")
    for key, _name, prefix in TIERS:
        if pid.startswith(prefix):
            return key
    return None


def depth_price(levels, depth: float) -> tuple[float | None, float]:
    """The size-weighted price of the first `depth` shares, best first,
    and the shares it found (less than `depth` on a thin side)."""
    need = float(depth)
    got = notional = 0.0
    for px, qty in levels or ():
        take = min(float(qty), need - got)
        if take <= 0:
            break
        notional += take * float(px)
        got += take
    if got <= 0:
        return None, 0.0
    return notional / got, got


def _falling_fit(points: list[tuple[float, float]]) -> dict[float, float]:
    """Pool-adjacent-violators: the closest sequence to `points` (sorted
    by threshold) that never rises as the threshold rises."""
    blocks: list[list[float]] = []            # [sum, count, first index]
    xs = [p[0] for p in points]
    for i, (_x, y) in enumerate(points):
        blocks.append([y, 1.0, i])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] < blocks[-1][0] / blocks[-1][1]:
            s, c, _ = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
    out: dict[float, float] = {}
    for bi, (s, c, i0) in enumerate(blocks):
        i1 = blocks[bi + 1][2] if bi + 1 < len(blocks) else len(points)
        for j in range(int(i0), int(i1)):
            out[xs[j]] = s / c
    return out


def _outcome(name: str, slug: str) -> str:
    return (name.split(" — ", 1)[1] if " — " in name else slug.rsplit("-", 1)[-1]).strip()


def _event(name: str, slug: str) -> str:
    return name.split(" — ", 1)[0].strip() if " — " in name else slug.rsplit("-", 1)[0]


_GTE = re.compile(r"^gte(\d+(?:\.\d+)?)$")


class Stat:
    """A decaying tally of errors: count, absolute and squared."""

    __slots__ = ("n", "abs", "sq", "at")

    def __init__(self, n=0.0, a=0.0, sq=0.0, at=0.0):
        self.n, self.abs, self.sq, self.at = float(n), float(a), float(sq), float(at)

    def add(self, err: float, now: float) -> None:
        if self.at:
            k = 0.5 ** (max(now - self.at, 0.0) / ERR_HALF_LIFE_S)
            self.n *= k
            self.abs *= k
            self.sq *= k
        self.at = now
        self.n += 1.0
        self.abs += abs(err)
        self.sq += err * err

    def mae(self) -> float | None:
        return self.abs / self.n if self.n > 0 else None

    def rmse(self) -> float | None:
        return math.sqrt(self.sq / self.n) if self.n > 0 else None

    def to_list(self) -> list:
        return [round(self.n, 3), round(self.abs, 6), round(self.sq, 8), round(self.at, 1)]


class Beta:
    """A decaying least-squares tally of how far the midpoint moved
    against how far an input sat from it: sum(d x move) / sum(d x d) is
    the share of the input's distance the midpoint went on to cover."""

    __slots__ = ("n", "sxy", "sxx", "at")

    def __init__(self, n=0.0, sxy=0.0, sxx=0.0, at=0.0):
        self.n, self.sxy, self.sxx, self.at = float(n), float(sxy), float(sxx), float(at)

    def add(self, d: float, move: float, now: float) -> None:
        if self.at:
            k = 0.5 ** (max(now - self.at, 0.0) / ERR_HALF_LIFE_S)
            self.n *= k
            self.sxy *= k
            self.sxx *= k
        self.at = now
        self.n += 1.0
        self.sxy += d * move
        self.sxx += d * d

    def share(self) -> float:
        """0 until BETA_MIN_N readings; then the least-squares share,
        held between 0 and 1 (an input that points the wrong way is not
        used backwards) and shrunk toward 0 by n / (n + BETA_SHRINK_N)."""
        if self.n < BETA_MIN_N or self.sxx <= 1e-12:
            return 0.0
        b = min(max(self.sxy / self.sxx, 0.0), 1.0)
        return b * self.n / (self.n + BETA_SHRINK_N)

    def to_list(self) -> list:
        return [round(self.n, 3), round(self.sxy, 10), round(self.sxx, 10), round(self.at, 1)]


class TierFair:
    """`fam` is the politics family (its program ledger, market list and
    book cache); `prints(slug)` gives the stream's (price, when, printed)
    or None; `silver(slug)` Silver's number or None; `his_fairs()` the
    focus page's fairs, for comparison only."""

    def __init__(self, fam, prints=None, silver=None, his_fairs=None, clock=None):
        self.fam = fam
        self.prints = prints or (lambda s: None)
        self.silver = silver or (lambda s: None)
        self.his_fairs = his_fairs or (lambda: {})
        self._clock = clock or time.time
        self.cur: dict[str, dict] = {}                 # slug -> the latest reading
        self.snaps: dict[str, deque] = {}              # slug -> readings awaiting their grade
        self.hist: dict[str, deque] = {}               # slug -> (ts, fair) for the volatility
        # (tier, horizon, name) -> Stat; name is an input, "fair", "mid" or "his"
        self.stats: dict[tuple, Stat] = {}
        self.mstats: dict[str, dict[str, Stat]] = {}   # slug -> {"fair","mid"} at an hour
        # (tier, horizon, input) -> Beta: the share of its distance from
        # the midpoint the midpoint moved
        self.betas: dict[tuple, Beta] = {}
        self.last_snap = 0.0
        self.last_page = 0.0
        self.ticks = 0
        self.tick_s = 0.0
        self.error = ""
        self.payload_json = b'{"ok":false,"note":"the tier fairs have not run yet"}'

    # -- the ground -------------------------------------------------------------

    def markets(self) -> dict[str, tuple[str, object]]:
        """slug -> (tier, program) for every live market in a midterm tier."""
        out = {}
        uni = self.fam.universe
        for slug, prog in list(self.fam.terms.current.items()):
            t = tier_of(getattr(prog, "pid", ""))
            if t and slug in uni:
                out[slug] = (t, prog)
        return out

    def _groups(self, mk: dict) -> dict[str, list[str]]:
        g: dict[str, list[str]] = {}
        for slug in mk:
            name = str((self.fam.universe.get(slug) or {}).get("name") or slug)
            g.setdefault(_event(name, slug), []).append(slug)
        return g

    # -- inputs ------------------------------------------------------------------

    def _book_input(self, slug: str, prog, now: float) -> dict | None:
        book = self.fam.cache.any_age(slug)
        if book is None or not book.bids or not book.asks:
            return None
        age = now - float(book.fetched_at or 0.0)
        depth = max(FAIR_DEPTH_MIN, FAIR_DEPTH_FRAC * float(getattr(prog, "target", 0.0) or 0.0))
        b, bq = depth_price(book.bids, depth)
        a, aq = depth_price(book.asks, depth)
        if b is None or a is None:
            return None
        bb, ba = float(book.bids[0][0]), float(book.asks[0][0])
        return {"value": (a + b) / 2.0, "half": max(a - b, 0.0) / 2.0,
                "bid_d": b, "ask_d": a, "thin": bq < depth - 1e-9 or aq < depth - 1e-9,
                "depth": depth, "mid": (bb + ba) / 2.0, "spread": ba - bb,
                "age": age, "stale": age > BOOK_STALE_S}

    def _linked(self, slug: str, group: list[str], books: dict) -> float | None:
        uni = self.fam.universe
        names = {s: str((uni.get(s) or {}).get("name") or s) for s in group}
        outs = {s: _outcome(names[s], s) for s in group}
        n_event = int((uni.get(slug) or {}).get("event_n") or len(group))
        gte = {s: _GTE.match(outs[s]) for s in group}
        if all(gte.values()):
            pts = sorted((float(gte[s].group(1)), books[s]["value"])
                         for s in group if books.get(s) and not books[s]["stale"])
            if len(pts) < 3 or not books.get(slug) or books[slug]["stale"]:
                return None
            fit = _falling_fit(pts)
            return fit.get(float(gte[slug].group(1)))
        if len(group) != n_event or len(group) < 2:
            return None                    # not every outcome of the event is here
        others = [s for s in group if s != slug]
        if not all(books.get(s) and not books[s]["stale"] for s in others):
            return None
        v = 1.0 - sum(books[s]["value"] for s in others)
        return min(max(v, 0.001), 0.999)

    def _weight(self, tier: str, name: str) -> float:
        st = self.stats.get((tier, 3600, name))
        if st is None or st.n < WEIGHT_MIN_N:
            return 1.0 / (PRIOR_ERR ** 2)
        r = st.rmse() or PRIOR_ERR
        return 1.0 / (max(r, 0.002) ** 2)

    def _share(self, tier: str, name: str) -> float:
        bt = self.betas.get((tier, BETA_HORIZON, name))
        return bt.share() if bt is not None else 0.0

    def _fair_err(self, tier: str) -> float:
        st = self.stats.get((tier, 3600, "fair"))
        if st is None or st.n < WEIGHT_MIN_N:
            return PRIOR_ERR
        return st.rmse() or PRIOR_ERR

    # -- the second ------------------------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        t0 = time.time()
        mk = self.markets()
        books = {s: self._book_input(s, p, now) for s, (t, p) in mk.items()}
        groups = self._groups(mk)
        his = {}
        try:
            his = dict(self.his_fairs() or {})
        except Exception:  # noqa: BLE001
            his = {}
        cur: dict[str, dict] = {}
        for ev, group in groups.items():
            for slug in group:
                tier, prog = mk[slug]
                bk = books.get(slug)
                if bk is None or bk["stale"]:
                    continue
                inputs: dict[str, float] = {"book": bk["value"]}
                pr = None
                try:
                    pr = self.prints(slug)
                except Exception:  # noqa: BLE001
                    pr = None
                if pr and len(pr) >= 3 and pr[2] and now - float(pr[1]) <= PRINT_MAX_AGE_S:
                    inputs["print"] = float(pr[0])
                lk = self._linked(slug, group, books)
                if lk is not None:
                    inputs["linked"] = lk
                try:
                    sv = self.silver(slug)
                except Exception:  # noqa: BLE001
                    sv = None
                if sv is not None and 0.0 < float(sv) < 1.0:
                    inputs["silver"] = float(sv)
                mid = bk["mid"]
                shares: dict[str, float] = {}
                moves: dict[str, float] = {}
                if bk["spread"] <= MID_TRUST_SPREAD:
                    # from the midpoint, moved only by what has proven itself
                    base = "midpoint"
                    for k, v in inputs.items():
                        sh = self._share(tier, k)
                        shares[k] = round(sh, 4)
                        if sh > 0.0:
                            moves[k] = sh * (v - mid)
                    fair = mid + (sum(moves.values()) / len(moves) if moves else 0.0)
                    fair = min(max(fair, 0.001), 0.999)
                    dis = math.sqrt(self._fair_err(tier) ** 2 + (bk["spread"] / 2.0) ** 2)
                else:
                    # no midpoint to start from: the inputs' average, each
                    # weighted by one over its own measured error
                    base = "inputs"
                    ws = {k: self._weight(tier, k) for k in inputs}
                    tot = sum(ws.values())
                    fair = sum(ws[k] * v for k, v in inputs.items()) / tot
                    fair = min(max(fair, 0.001), 0.999)
                    shares = {k: round(ws[k] / tot, 3) for k in ws}
                    dis = math.sqrt(sum(ws[k] * (v - fair) ** 2 for k, v in inputs.items()) / tot)
                h = self.hist.setdefault(slug, deque())
                if not h or now - h[-1][0] >= VOL_SAMPLE_S:
                    h.append((now, fair))
                while h and now - h[0][0] > VOL_WINDOW_S:
                    h.popleft()
                vol = 0.0
                if len(h) >= 3:
                    vals = [x for _, x in h]
                    m = sum(vals) / len(vals)
                    vol = math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals))
                conf = (math.sqrt(dis ** 2 + vol ** 2) if base == "midpoint"
                        else math.sqrt(dis ** 2 + bk["half"] ** 2 + vol ** 2))
                cur[slug] = {"tier": tier, "event": ev, "fair": fair, "conf": conf,
                             "inputs": inputs, "base": base,
                             # base "midpoint": each input's proven share of its
                             # distance; base "inputs": each input's weight
                             "weights": shares,
                             "moves": {k: round(v, 5) for k, v in moves.items()},
                             "mid": bk["mid"], "spread": bk["spread"],
                             "bid_d": bk["bid_d"], "ask_d": bk["ask_d"], "depth": bk["depth"],
                             "thin": bk["thin"], "book_age": bk["age"],
                             "his": his.get(slug)}
        self.cur = cur
        for s in [s for s in self.hist if s not in mk]:
            del self.hist[s]
        if now - self.last_snap >= SNAP_S:
            self.last_snap = now
            self._grade(now, books)
            self._snap(now)
        self.ticks += 1
        self.tick_s = round(time.time() - t0, 4)
        if now - self.last_page >= PAGE_S:
            self.last_page = now
            self._freeze(now, mk, books)

    def _snap(self, now: float) -> None:
        for slug, r in self.cur.items():
            if r["spread"] > MID_TRUST_SPREAD:
                continue      # graded in pairs: the fair only where the midpoint is a price too
            d = self.snaps.setdefault(slug, deque())
            d.append({"ts": now, "tier": r["tier"], "fair": r["fair"], "mid": r["mid"],
                      "inputs": dict(r["inputs"]), "his": r.get("his"),
                      "done": set()})
            while d and now - d[0]["ts"] > max(GRADE_HORIZONS) + GRADE_SLACK_S:
                d.popleft()

    def _grade(self, now: float, books: dict) -> None:
        for slug, d in list(self.snaps.items()):
            bk = books.get(slug)
            target = None
            if bk is not None and not bk["stale"] and bk["spread"] <= MID_TRUST_SPREAD:
                target = bk["mid"]
            for snap in d:
                for h in GRADE_HORIZONS:
                    if h in snap["done"]:
                        continue
                    age = now - snap["ts"]
                    if age < h:
                        continue
                    snap["done"].add(h)
                    if target is None or age > h + GRADE_SLACK_S:
                        continue                       # no price to grade against
                    tier = snap["tier"]

                    def add(name, val):
                        if val is None:
                            return
                        self.stats.setdefault((tier, h, name), Stat()).add(float(val) - target, now)
                        self.stats.setdefault(("all", h, name), Stat()).add(float(val) - target, now)
                    add("fair", snap["fair"])
                    add("mid", snap["mid"])
                    add("his", snap["his"])
                    for k, v in snap["inputs"].items():
                        add(k, v)
                    if snap["mid"] is not None:
                        # how much of each input's distance from the
                        # midpoint the midpoint went on to cover
                        move = target - float(snap["mid"])
                        for k, v in snap["inputs"].items():
                            dev = float(v) - float(snap["mid"])
                            self.betas.setdefault((tier, h, k), Beta()).add(dev, move, now)
                            self.betas.setdefault(("all", h, k), Beta()).add(dev, move, now)
                    if h == 3600:
                        ms = self.mstats.setdefault(slug, {})
                        ms.setdefault("fair", Stat()).add(snap["fair"] - target, now)
                        if snap["mid"] is not None:
                            ms.setdefault("mid", Stat()).add(snap["mid"] - target, now)
            while d and all(h in d[0]["done"] for h in GRADE_HORIZONS):
                d.popleft()
            if not d:
                del self.snaps[slug]

    # -- the page ----------------------------------------------------------------

    def grade_view(self, tier: str) -> dict:
        out = {}
        for h in GRADE_HORIZONS:
            row = {}
            for name in ("fair", "mid", "his") + INPUTS:
                st = self.stats.get((tier, h, name))
                if st is not None and st.n > 0:
                    row[name] = {"mae_c": round(st.mae() * 100, 2), "n": round(st.n, 1)}
                bt = self.betas.get((tier, h, name))
                if bt is not None and name in row:
                    row[name]["share"] = round(bt.share(), 4)
            out[str(h)] = row
        return out

    def view(self, now: float, mk: dict | None = None, books: dict | None = None) -> dict:
        mk = self.markets() if mk is None else mk
        tiers = []
        for key, name, prefix in TIERS:
            ms = [s for s, (t, _p) in mk.items() if t == key]
            progs = [mk[s][1] for s in ms]
            pools = sorted({float(getattr(p, "pool", 0) or 0) for p in progs})
            targets = sorted({float(getattr(p, "target", 0) or 0) for p in progs})
            tiers.append({"key": key, "name": name, "prefix": prefix,
                          "markets": len(ms),
                          "with_fair": sum(1 for s in ms if s in self.cur),
                          "with_book": sum(1 for s in ms if (books or {}).get(s)),
                          "pool_day": pools, "target": targets,
                          "grade": self.grade_view(key)})
        rows = []
        for slug, r in sorted(self.cur.items(), key=lambda kv: (kv[1]["tier"], kv[1]["event"], kv[0])):
            ms = self.mstats.get(slug) or {}
            rows.append({"market": slug,
                         "name": str((self.fam.universe.get(slug) or {}).get("name") or slug),
                         "tier": r["tier"], "event": r["event"],
                         "fair": round(r["fair"], 4), "conf": round(r["conf"], 4),
                         "mid": round(r["mid"], 4), "spread": round(r["spread"], 4),
                         "bid_d": round(r["bid_d"], 4), "ask_d": round(r["ask_d"], 4),
                         "depth": round(r["depth"]), "thin": r["thin"],
                         "book_age": round(r["book_age"], 1),
                         "inputs": {k: round(v, 4) for k, v in r["inputs"].items()},
                         "weights": r["weights"], "base": r.get("base"),
                         "moves": r.get("moves") or {}, "his": r.get("his"),
                         "g1h": {k: round(st.mae() * 100, 2) for k, st in ms.items() if st.n > 0},
                         "g1h_n": round((ms.get("fair") or Stat()).n, 1)})
        return {"ok": True, "now": now, "read_only": True,
                "note": "stage 1 — fairs only; nothing here places or moves an order",
                "version": FAIR_VERSION,
                "tiers": tiers, "all": self.grade_view("all"), "rows": rows,
                "ticks": self.ticks, "tick_s": self.tick_s, "error": self.error,
                "horizons": list(GRADE_HORIZONS), "mid_trust_spread": MID_TRUST_SPREAD}

    def _freeze(self, now: float, mk=None, books=None) -> None:
        try:
            self.payload_json = json.dumps(self.view(now, mk, books)).encode()
        except Exception as e:  # noqa: BLE001 — the page keeps its last good view
            self.error = f"page: {type(e).__name__}: {e}"[:160]

    # -- persistence ----------------------------------------------------------

    def to_dict(self) -> dict:
        return {"version": FAIR_VERSION,
                "stats": {f"{t}|{h}|{n}": st.to_list() for (t, h, n), st in self.stats.items()},
                "mstats": {s: {k: st.to_list() for k, st in d.items()}
                           for s, d in self.mstats.items()},
                "betas": {f"{t}|{h}|{n}": bt.to_list() for (t, h, n), bt in self.betas.items()}}

    def restore(self, d: dict) -> None:
        # a save from before the fair started at the midpoint graded a
        # different fair: its "fair" errors are dropped, the inputs', the
        # midpoint's and his are the same measures and are kept
        same = d.get("version") == FAIR_VERSION
        for k, v in (d.get("stats") or {}).items():
            try:
                t, h, n = k.split("|", 2)
                if n == "fair" and not same:
                    continue
                self.stats[(t, int(h), n)] = Stat(*v)
            except (ValueError, TypeError):
                continue
        for s, dd in (d.get("mstats") or {}).items():
            self.mstats[s] = {k: Stat(*v) for k, v in (dd or {}).items()
                              if same or k != "fair"}
        for k, v in (d.get("betas") or {}).items():
            try:
                t, h, n = k.split("|", 2)
                self.betas[(t, int(h), n)] = Beta(*v)
            except (ValueError, TypeError):
                continue
