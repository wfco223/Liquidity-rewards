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
import re
import time

from . import risk
from .intents import BUY_LONG, BUY_SHORT
from .tierfair import SLOW_TIERS, TIERS
from .tiervalue import side_share

CENT_EVERY_S = 600.0       # the report is worked out this often
CENT_BOOK_MAX_S = 300.0    # a book older than this is not reported on
CENT_MAX_TICKS = 20        # at most this many prices a side
CENT = 0.01                  # a day: the smallest amount worth earning
QTY_MAX = 20000.0            # the desk's largest order
PAGE_ROWS_SLOW = 60          # Tier 4 markets kept in full in the report (the cheapest)
VERSION = "cent-2026-09-29b"
MARGIN_KEEP = 432            # margin checks kept: three days at one a run
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


# -- negative risk (owner, 2026-09-29: "We also need to account for negative
# risk and mutually exclusive outcomes" ... "Sure") ---------------------------
#
# The exchange names every market "<event> — <outcome>", and the markets of
# one event are its outcomes (the name groups match its own event size on
# every tier market but one). Where only one outcome can resolve Yes, orders
# and positions across them cannot all lose: priced over who wins, as
# v3/risk.py prices the engine's book. Grouping is by the event's NAME, not
# the slug: the slug's last token splits a margin bracket like "d0-10", and
# 5,027 Tier 4 coverage markets read as 4,109 slug groups against 594 events.
# Only a group that can be CLASSIFIED is netted: exact outcomes (parties,
# candidates, margin brackets, seat counts); nested thresholds ("over 2.5
# million", gte/lt on anything but a seat count) can resolve Yes together
# and stay at plain collateral, as does a "wins" beside that side's brackets.

_THRESH = re.compile(r"^(gt|lt|gte|lte)\d")
_BUCKET = re.compile(r"^([dri])(\d+-\d+|gte\d+)$")


def event_of(slug: str, name: str | None) -> tuple[str, str]:
    """(the event, the outcome) from the exchange's name, else the slug."""
    if name and " — " in name:
        ev, tok = name.split(" — ", 1)
        return ev.strip(), tok.strip()
    return risk.race_key(slug), risk.rung_token(slug)


def group_kind(tokens) -> str | None:
    """How a group of outcomes resolves: "numeric" (seat-count rungs, the
    count sweep), "categorical" (one of them wins), or None (not known to
    be exclusive — priced plain)."""
    toks = set(tokens)
    if toks and all(risk.numeric_rung(t) for t in toks):
        return "numeric"
    if any(_THRESH.match(t) for t in toks):
        return None
    for p in "dri":
        if p + "win" in toks and any(_BUCKET.match(t) and t[0] == p for t in toks):
            return None
    return "categorical"


def _loss(leg, yes: bool) -> float:
    x = leg.loss_if(yes)
    # a resting order gets no credit for a gain: nothing obliges the
    # market to fill it (v3/risk.py's rule); a held position's gain is real
    return x if leg.firm else max(x, 0.0)


def netted(legs, names) -> float:
    """The worst case of a set of legs with negative risk netted, each
    event swept over its outcomes; a group of one market, or one not known
    to be exclusive, is the sum of each market's own worst case."""
    groups: dict[str, list] = {}
    for leg in legs:
        ev, tok = event_of(leg.market, names.get(leg.market))
        groups.setdefault(ev, []).append((leg, tok))
    total = 0.0
    for items in groups.values():
        tok_of = {leg.market: tok for leg, tok in items}
        kind = group_kind(tok_of.values()) if len(tok_of) > 1 else None
        if kind is None:
            by_mkt: dict[str, list] = {}
            for leg, _t in items:
                by_mkt.setdefault(leg.market, []).append(leg)
            for ls in by_mkt.values():
                total += max(max(sum(_loss(l, y) for l in ls) for y in (True, False)), 0.0)
            continue
        worst = 0.0
        if kind == "numeric":
            marks: set[int] = set()
            for t in tok_of.values():
                n = int(t[3:]) if t.startswith(("gte", "lte")) else int(t)
                marks.update((n - 1, n, n + 1))
            for k in marks:
                worst = max(worst, sum(_loss(l, risk.rung_pays(tok_of[l.market], k))
                                       for l, _t in items))
        else:
            for winner in list(tok_of) + [None]:
                worst = max(worst, sum(_loss(l, l.market == winner) for l, _t in items))
        total += max(worst, 0.0)
    return round(total, 4)


def plain(legs) -> float:
    """The same legs with every one at its own collateral."""
    return round(sum(l.stake for l in legs), 4)


def _order_leg(slug: str, side: str, px: float, q: float):
    return risk.leg_for_order(slug, BUY_LONG if side == "BUY" else BUY_SHORT, px, q)


def _risk_view(sets: dict, names: dict) -> dict:
    """{set: {"n", "plain", "netted"}} for each probe set's legs."""
    return {k: {"n": len(v), "plain": round(plain(v), 2), "netted": round(netted(v, names), 2)}
            for k, v in sets.items()}


def margin_check(inv, row: dict | None, feed_n: int, names: dict, now: float) -> dict:
    """Our held positions priced plain and netted, beside what the
    exchange's balance row says it holds as margin. `inv` is
    [(slug, qty, cost, estimated)]."""
    legs = []
    n_est = skipped = 0
    for slug, qty, cost, est in inv:
        q = float(qty or 0.0)
        if abs(q) < 1e-9:
            continue
        avg = float(cost or 0.0) / q
        if not (0.0 <= avg <= 1.0):
            skipped += 1
            continue
        n_est += int(bool(est))
        if q > 0:
            legs.append(risk.Leg(slug, True, q, q * avg, firm=True))
        else:
            legs.append(risk.Leg(slug, False, -q, -q * (1.0 - avg), firm=True))
    row = row or {}
    out = {"at": round(now, 1), "n": len(legs), "feed_n": int(feed_n), "est": n_est,
           "skipped": skipped, "plain": round(plain(legs), 2),
           "netted": round(netted(legs, names), 2)}
    for k, k2 in (("marginRequirement", "mr"), ("openOrders", "oo"), ("buyingPower", "bp"),
                  ("currentBalance", "bal")):
        try:
            out[k2] = round(float(row[k]), 2)
        except (KeyError, TypeError, ValueError):
            out[k2] = None
    return out


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

    def __init__(self, tf, fam, raws=None, margin=None, clock=None):
        self.tf = tf
        self.fam = fam
        # what we hold and the exchange's balance row, for the margin check:
        # {"inv": [(slug, qty, cost, est)], "row": {...}, "feed_n": int,
        #  "names": {slug: name}}
        self.margin = margin or (lambda: None)
        self.margin_hist: list[dict] = []
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
        uni = getattr(self.fam, "universe", {}) or {}
        names = {slug: str((uni.get(slug) or {}).get("name") or "") for slug in mk}
        # each tier's probe sets as order legs, for the negative-risk view
        sets = {k: {"nearest": [], "le1": [], "le10": []} for k, _n, _p in TIERS}
        events: dict[str, set] = {k: set() for k, _n, _p in TIERS}
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
            events[tier].add(event_of(slug, names.get(slug))[0])
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
                for i, r_ in enumerate(lad.get("rows") or []):
                    leg = _order_leg(slug, side, r_[1], r_[2])
                    if leg is None:
                        continue
                    if i == 0:
                        sets[tier]["nearest"].append(leg)
                    if r_[3] <= 1.0:
                        sets[tier]["le1"].append(leg)
                    if r_[3] <= 10.0:
                        sets[tier]["le10"].append(leg)
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
                    for key, lx in (("A_nearest", la),
                                    ("B_nearest", lad if spread <= ms + SPREAD_EPS else None)):
                        if lx and lx.get("rows"):
                            r_ = lx["rows"][0]
                            leg = _order_leg(slug, side, r_[1], r_[2])
                            if leg is not None:
                                sets[tier].setdefault(key, []).append(leg)
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
        # negative risk: each tier's probe sets, plain and netted
        for key, t in tiers.items():
            t["events"] = len(events[key])
            t["risk"] = _risk_view(sets[key], names)
        every = {}
        for key in ("nearest", "le1", "le10"):
            every[key] = [leg for k in sets for leg in sets[k].get(key, [])]
        risk_all = _risk_view(every, names)
        margin = self._margin(now)
        self.error = ""
        self.report = {"ok": True, "version": VERSION, "at": round(now, 1),
                       "took_s": round(time.time() - t0, 2),
                       "cent": CENT, "every_s": CENT_EVERY_S, "tiers": tiers, "markets": rows,
                       "programs": programs, "risk_all": risk_all, "margin": margin,
                       "margin_hist": list(self.margin_hist)}

    def _margin(self, now: float) -> dict | None:
        """This run's margin check, kept for P31."""
        try:
            m = self.margin()
        except Exception as e:  # noqa: BLE001 — a readout
            return {"error": f"{type(e).__name__}: {e}"[:120]}
        if not m:
            return None
        c = margin_check(m.get("inv") or [], m.get("row"), m.get("feed_n") or 0,
                         m.get("names") or {}, now)
        if c.get("mr") is not None:
            self.margin_hist.append(c)
            del self.margin_hist[:-MARGIN_KEEP]
        return c

    def restore(self, d: dict) -> None:
        """The margin checks carry across a restart; the rest is redone."""
        hist = (d or {}).get("margin_hist") or []
        self.margin_hist = [h for h in hist if isinstance(h, dict)][-MARGIN_KEEP:]

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
        r.pop("margin_hist", None)
        r["error"] = self.error
        r["programs"] = {pid: {"row_keys": p.get("row_keys"), "period_keys": p.get("period_keys"),
                               "reads": p.get("reads"), "extra": p.get("extra")}
                         for pid, p in (self.report.get("programs") or {}).items()}
        return r

    def to_dict(self) -> dict:
        return self.report

