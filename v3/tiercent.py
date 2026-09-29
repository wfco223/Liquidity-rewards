"""The 1-cent report (owner, 2026-09-28: "Just do the minimal amount to
earn rewards 1 cent per day. Just to see how many actually get filled.
Then ramp it up. And that means at different price levels within markets
as well." ... "Don't you need to know how many shares are resting?" ...
"The nearest you should sit to the midpoint is the closest necessary to
earn rewards. But then you can offer further back from there until it is
no longer possible to earn rewards." ... "On some tier 4 markets, the bid
and the ask must be sufficiently close to earn rewards." ... "Yes").

READ-ONLY. Nothing here places, moves or cancels an order, and nothing
here reads the exchange: it works from the live books the streams keep
and the program records the families already read.

For every tier market with a fresh book, each side:
- a side under its Target Size pays nobody: it is reported with how many
  shares it is short, and no level is listed;
- otherwise every price from the side's best price (joined, never
  improved on — joining earns the same weight with less fill risk) back,
  one tick at a time, to the last price inside the Target Size window
  (past it nothing earns): the shares an order there needs to earn one
  cent a day, and the money it ties up. The shares come from the book as
  it rests: an order's score is df^(ticks behind the best price) x size,
  over everyone's score in the window, times the side's pool.
The spread rule some Tier 4 programs carry is not in our program reader,
so every field the exchange's program record carries is kept here, per
program, and each market's spread is kept beside its ladder — the rule is
read from the source before any order depends on it."""

from __future__ import annotations

import math
import time

from .tierfair import SLOW_TIERS, TIERS
from .tiervalue import side_share

CENT_EVERY_S = 600.0       # the report is worked out this often
CENT_BOOK_MAX_S = 300.0    # a book older than this is not reported on
CENT_MAX_TICKS = 20        # at most this many prices a side
CENT = 0.01                  # a day: the smallest amount worth earning
QTY_MAX = 20000.0            # the desk's largest order
PAGE_ROWS_SLOW = 60          # Tier 4 markets kept in full in the report (the cheapest)
VERSION = "cent-2026-09-29"
SPREAD_EPS = 1e-6            # a price exactly at the band's edge is inside it


def window_edge(levels, target: float) -> int:
    """How many levels, best first, the Target Size window takes in: the
    walk stops at the level where the running size reaches the target
    (that level scores whole). The whole side when it never does."""
    cum = 0.0
    for i, (_p, q) in enumerate(levels):
        cum += float(q)
        if target and cum >= target - 1e-9:
            return i + 1
    return len(levels)


def cent_ladder(side: str, levels, tick: float, df: float, target: float,
                pool: float, max_ticks: int = CENT_MAX_TICKS) -> dict:
    """The shares that earn a cent a day at each price a side pays at.

    `levels` is everyone's resting size, best first. Returns
    {"under": shares short of the Target Size} for a side that pays
    nobody, else {"rows": [(ticks back, price, shares, collateral)],
    "edge": the last price that earns, "window": shares in the window}.
    A price where a cent a day would take more than the desk's largest
    order is left out."""
    tick = tick or 0.01
    total = sum(float(q) for _p, q in levels)
    if not levels or (target and total < target - 1e-9):
        # pays nobody until someone brings it to the Target Size: what
        # that takes at the side's own best price, and as a wall at the
        # far edge of the book (1c bid, 99c ask) — which only pays if no
        # spread rule stops it
        gap = round(max(float(target) - total, 0.0), 2)
        best = float(levels[0][0]) if levels else None
        c_best = (None if best is None
                  else round(gap * (best if side == "BUY" else 1.0 - best), 4))
        return {"under": gap, "rows": [], "best": best, "coll_best": c_best,
                "coll_wall": round(gap * 0.01, 4)}
    n = window_edge(levels, target)
    win = levels[:n]
    best = float(win[0][0])
    edge = float(win[-1][0])
    sign = 1.0 if side == "BUY" else -1.0
    # everyone's score in the window, from the side's own best price
    denom = sum(float(q) * df ** round(abs(best - float(p)) / tick) for p, q in win)
    s = CENT / pool if pool > 0 else 1.0
    rows = []
    if s < 1.0:
        k_edge = round(abs(best - edge) / tick)
        for k in range(0, min(k_edge, max_ticks - 1) + 1):
            px = round(best - sign * k * tick, 4)
            if not (0.001 <= px <= 0.999):
                break
            w = df ** k
            # our share q.w / (denom + q.w) = s  ->  q = s.denom / (w (1 - s)).
            # Our own size can only push deeper levels out of the window,
            # which lowers the others' score: the cent is then reached
            # with fewer shares, never more — so where it does, the
            # smallest size is found on the exchange's arithmetic itself
            q = s * denom / (w * (1.0 - s))
            if q > QTY_MAX:
                continue
            q = _exact(side, levels, tick, df, target, pool, px, q)
            coll = q * (px if side == "BUY" else 1.0 - px)
            rows.append((k, px, q, round(coll, 4)))
    return {"rows": rows, "edge": edge, "window": round(sum(float(q) for _p, q in win), 2),
            "ticks_to_edge": round(abs(best - edge) / tick)}


def _exact(side, levels, tick, df, target, pool, px, q_hi) -> float:
    """The smallest size at `px` whose share of the side earns a cent,
    given that `q_hi` (the size on the window without us) does. The share
    only grows with the size, so a bisection finds it."""
    def earns(q):
        return side_share(side, levels, tick, df, target, {px: q})[0] * pool >= CENT

    hi = q_hi
    share = side_share(side, levels, tick, df, target, {px: q_hi})[0]
    if share * pool > CENT * 1.001:
        lo = 0.0
        for _ in range(24):
            mid = (lo + hi) / 2.0
            if earns(mid):
                hi = mid
            else:
                lo = mid
            if hi - lo < 0.005:
                break
    # on the desk's grid of a hundredth of a share: the least that earns
    q = max(math.ceil(hi * 100.0 - 1e-6) / 100.0, 0.01)
    for _ in range(3):
        if q - 0.01 >= 0.01 and earns(q - 0.01):
            q = round(q - 0.01, 2)
        else:
            break
    return q


def _ctr() -> dict:
    """The counters one reading of the spread rule keeps per tier."""
    return {"sides_paying": 0, "sides_under": 0, "sides_off": 0, "levels": 0,
            "coll_nearest": 0.0, "pool_paying": 0.0, "le1": [0, 0.0], "le10": [0, 0.0]}


def _tally(c: dict, lad: dict | None, pool: float) -> None:
    """Count one side under a reading: None is a side the rule shuts."""
    if lad is None:
        c["sides_off"] += 1
        return
    rows = lad.get("rows") or []
    if rows:
        c["sides_paying"] += 1
        c["pool_paying"] += pool
        c["levels"] += len(rows)
        c["coll_nearest"] += rows[0][3]
        for r in rows:
            for key, lim in (("le1", 1.0), ("le10", 10.0)):
                if r[3] <= lim:
                    c[key][0] += 1
                    c[key][1] += r[3]
    elif "under" in lad:
        c["sides_under"] += 1


def _round_ctr(c: dict) -> None:
    c["coll_nearest"] = round(c["coll_nearest"], 2)
    c["pool_paying"] = round(c["pool_paying"], 2)
    for k in ("le1", "le10"):
        c[k][1] = round(c[k][1], 2)


def within(side: str, levels, tick: float, df: float, target: float, pool: float,
           mid: float, ms: float) -> dict:
    """Reading A of the program's maxSpread: an order farther than `ms`
    from the midpoint neither scores nor counts toward the Target Size.
    The side is walked on the levels inside the band alone, and a price
    outside it is never listed. The best price is the level nearest the
    midpoint, so the band never moves it."""
    band = [(p, q) for p, q in levels if abs(float(p) - mid) <= ms + SPREAD_EPS]
    lad = cent_ladder(side, band, tick, df, target, pool)
    if lad.get("rows"):
        lad["rows"] = [r for r in lad["rows"] if abs(r[1] - mid) <= ms + SPREAD_EPS]
    return lad


class TierCent:
    """`tf` is the tier fairs (its markets and books), `fam` the politics
    family (the pools, the first-day record, the program records)."""

    def __init__(self, tf, fam, raws=None, clock=None):
        self.tf = tf
        self.fam = fam
        # the program records' raw fields: the family's ledger and the
        # tender's each read the exchange (TermsStore.raw_seen)
        self.raws = raws or (lambda: [getattr(fam.terms, "raw_seen", {})])
        self._clock = clock or time.time
        self.last = 0.0
        self.report: dict = {"ok": False, "note": "the 1-cent report has not run yet"}
        self.error = ""

    def tick(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        if now - self.last < CENT_EVERY_S:
            return
        self.last = now
        t0 = time.time()
        mk = getattr(self.tf, "_mk", None) or self.tf.markets()
        try:
            first = set(self.fam.terms.joined_today(now))
        except Exception:  # noqa: BLE001
            first = set()
        tiers = {k: {"markets": 0, "fresh": 0, "no_pool": 0, "sides_paying": 0,
                     "sides_under": 0, "sides_empty": 0, "levels": 0,
                     "coll_nearest": 0.0, "coll_all": 0.0, "pool_paying": 0.0,
                     "pool_under": 0.0, "coll_under_wall": 0.0,
                     # levels whose cent ties up at most $1 / $10: [count, money]
                     "le1": [0, 0.0], "le10": [0, 0.0],
                     "first_day": 0, "spreads": {}} for k, _n, _p in TIERS}
        rows: dict[str, dict] = {}
        slow_rows: list[tuple] = []
        programs = self._programs(mk)
        for slug, (tier, prog) in mk.items():
            t = tiers[tier]
            t["markets"] += 1
            # the program's own spread rule (owner, 2026-09-28: "On some tier
            # 4 markets, the bid and the ask must be sufficiently close to earn
            # rewards"): Tier 4's records carry maxSpread, 0.06 on 09-28
            ms = self._max_spread(programs, getattr(prog, "pid", ""))
            if ms is not None:
                t.setdefault("max_spread", [])
                if ms not in t["max_spread"]:
                    t["max_spread"].append(ms)
                t.setdefault("A", _ctr())
                t.setdefault("B", _ctr())
                t.setdefault("no_mid", 0)
            bk = self.tf._book(slug)
            if bk is None or now - float(bk.fetched_at or 0.0) > CENT_BOOK_MAX_S:
                continue
            t["fresh"] += 1
            try:
                pool = self.fam._side_pool(slug, prog)
            except Exception:  # noqa: BLE001
                pool = None
            if not pool or not prog.df or not prog.target:
                t["no_pool"] += 1
                continue
            fd = slug in first
            t["first_day"] += int(fd)
            bids = [(float(p), float(q)) for p, q in bk.bids]
            asks = [(float(p), float(q)) for p, q in bk.asks]
            spread = (round(asks[0][0] - bids[0][0], 4) if bids and asks else None)
            sb = ("none" if spread is None else "<=1c" if spread <= 0.0105
                  else "<=2c" if spread <= 0.0205 else "<=5c" if spread <= 0.0505
                  else "<=10c" if spread <= 0.1005 else ">10c")
            t["spreads"][sb] = t["spreads"].get(sb, 0) + 1
            row = {"tier": tier, "spread": spread, "pool_side": round(pool, 4),
                   "target": prog.target, "df": prog.df, "tick": bk.tick,
                   "pid": getattr(prog, "pid", ""), "first_day": fd}
            cost_near = cost_all = 0.0
            for side, lv in (("BUY", bids), ("SELL", asks)):
                lad = cent_ladder(side, lv, bk.tick, float(prog.df), float(prog.target), pool)
                if lad.get("rows"):
                    t["sides_paying"] += 1
                    t["pool_paying"] += pool
                    t["levels"] += len(lad["rows"])
                    cost_near += lad["rows"][0][3]
                    cost_all += sum(r[3] for r in lad["rows"])
                    for r_ in lad["rows"]:
                        for key, lim in (("le1", 1.0), ("le10", 10.0)):
                            if r_[3] <= lim:
                                t[key][0] += 1
                                t[key][1] += r_[3]
                elif "under" in lad:
                    t["sides_under"] += 1
                    t["sides_empty"] += int(not lv)
                    t["pool_under"] += pool
                    t["coll_under_wall"] += lad["coll_wall"]
                row[side] = lad
                if ms is not None:
                    # reading A: every order within maxSpread of the midpoint;
                    # reading B: the book's bid-ask gap within maxSpread. A
                    # book with one side has neither a midpoint nor a gap
                    if spread is None:
                        _tally(t["A"], None, pool)
                        _tally(t["B"], None, pool)
                        continue
                    mid = (bids[0][0] + asks[0][0]) / 2.0
                    la = within(side, lv, bk.tick, float(prog.df), float(prog.target),
                                pool, mid, ms)
                    _tally(t["A"], la, pool)
                    _tally(t["B"], lad if spread <= ms + SPREAD_EPS else None, pool)
                    row.setdefault("A", {"mid": round(mid, 4)})[side] = la
                    row["B"] = spread <= ms + SPREAD_EPS
            if ms is not None:
                row["ms"] = ms
                t["no_mid"] += int(spread is None)
            t["coll_nearest"] += cost_near
            t["coll_all"] += cost_all
            row["coll_nearest"] = round(cost_near, 2)
            row["coll_all"] = round(cost_all, 2)
            if tier in SLOW_TIERS:
                slow_rows.append((cost_near if cost_near > 0 else 1e9, slug, row))
            else:
                rows[slug] = row
        # Tier 4: the cheapest markets kept in full, the rest in the totals
        for _c, slug, row in sorted(slow_rows, key=lambda x: (x[0], x[1]))[:PAGE_ROWS_SLOW]:
            rows[slug] = row
        for t in tiers.values():
            for k in ("coll_nearest", "coll_all", "pool_paying", "pool_under",
                      "coll_under_wall"):
                t[k] = round(t[k], 2)
            for k in ("le1", "le10"):
                t[k][1] = round(t[k][1], 2)
            for k in ("A", "B"):
                if k in t:
                    _round_ctr(t[k])
        self.error = ""
        self.report = {"ok": True, "version": VERSION, "at": round(now, 1),
                       "took_s": round(time.time() - t0, 2),
                       "cent": CENT, "every_s": CENT_EVERY_S, "tiers": tiers, "markets": rows,
                       "programs": programs}

    @staticmethod
    def _max_spread(programs: dict, pid: str) -> float | None:
        """The program's maxSpread as its record carries it, or None."""
        v = ((programs.get(pid) or {}).get("period") or {}).get("maxSpread")
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    def _programs(self, mk: dict) -> dict:
        """Every field the exchange's program record carried, per tier
        program — whatever our reader does not use, the spread rule
        included if the exchange sends one."""
        raw: dict = {}
        for d in self.raws() or []:
            for pid, r in list((d or {}).items()):
                if pid not in raw or float(r.get("at") or 0) > float(raw[pid].get("at") or 0):
                    raw[pid] = r
        pids = {getattr(p, "pid", "") for _t, p in mk.values()}
        return {pid: raw[pid] for pid in sorted(pids) if pid in raw}

    def view(self) -> dict:
        r = dict(self.report)
        # the page does not need every market's ladder, nor every
        # program's example values: the field names answer the question
        r.pop("markets", None)
        r["error"] = self.error
        r["programs"] = {pid: {"row_keys": p.get("row_keys"), "period_keys": p.get("period_keys"),
                               "reads": p.get("reads"), "extra": p.get("extra")}
                         for pid, p in (self.report.get("programs") or {}).items()}
        return r

    def to_dict(self) -> dict:
        return self.report

