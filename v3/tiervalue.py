"""Stage 2 of the tier engines: what every spot would earn and cost
(owner, 2026-09-26: "We're going to build things from the ground up.
Focus on building" — the design is v3/TIERS.md, stage 2).

READ-ONLY. Nothing here places, moves or cancels an order, and nothing
here reads the exchange: it works from the books the streams and the
families already keep, the tier fairs of stage 1, the program ledger,
and the meter's own record of what the exchange paid.

For every tier market with a fresh book, each side, each candidate
price, a day:
- reward: our share of the side's pool by the exchange's arithmetic
  (score = size x df^ticks inside the Target Size window; a side under
  its Target Size pays nobody), nothing on a market's first day in its
  program;
- fill cost: fills a day x shares x the loss a share. Fills a day are
  measured on paper spots per (tier, side, distance from the touch,
  queue ahead), starting from a prior worth one day; the loss a share
  is what filled paper spots lost against the midpoint an hour later,
  per tier and side, pulled toward the market's own, never under his
  fill floor, plus his concession rule past the fair (2026-09-12: the
  concession is what a fill loses unwound midway between the fair and
  the side's current price);
- capital: collateral x his cost of capital.
value = reward - fill cost - capital.

The best SET of orders: the money goes out in SLICE_USD slices, each to
the (market, side, price) whose next slice adds the most value a dollar.
The reward share saturates — our share is our score over the side's —
so each side stops by itself; a side under its Target Size may be
carried over it in one step when that pays. No market takes more than
MARKET_CAP_FRAC of the pot. Each tier runs to the whole pot, and a
prefix of that run is the plan at any smaller budget (a side stopping at
its first slice that no longer fits), which
gives, from one pass: each tier alone with the pot; the design's split
(each tier's share = its value alone over the sum); and the joint best
split (the tiers' runs merged by value a dollar).

Paper spots: one per side at the side's best price, followed for
SPOT_H_S. Filled when the other side reaches its price (certain), a
trade prints through it (a taker went past us), or a trade prints AT
its price with nothing left ahead of it in line — anything that leaves
the level may have been ahead of us, so the queue ahead only ever
shrinks. Tier 4's stream carries no trade prints, so there only the
first counts and its fills are a floor. Every spot's fill odds are
written down when it is laid and graded when it ends."""

from __future__ import annotations

import heapq
import json
import math
import time

from .tierfair import MID_TRUST_SPREAD, SLOW_TIERS, TIERS

POT_USD = 1000.0              # the tier engines' money (owner, 2026-09-24: "assume $1,000")
MARKET_CAP_FRAC = 0.10        # no market takes more than this share of the pot
SLICE_USD = 5.0               # money goes out this many dollars at a time
GAP_CUSHION = 0.05            # a side carried over its Target Size is carried this much past it
CAND_TICKS = 3                # candidates: the touch and this many ticks behind it...
CAND_BACK_C = 0.06            # ...and every resting level within this far behind the touch
CAND_MAX = 8
CAND_MAX_SLOW = 4             # Tier 4: the nearest four
PLAN_FAST_S = 10.0            # Tiers 1-3 are planned this often
PLAN_SLOW_S = 60.0            # Tier 4 this often
PLAN_BOOK_MAX_S = 300.0       # a book older than this is not planned on
SPOT_H_S = 3600.0             # a paper spot is followed this long
SPOT_STALE_S = 120.0          # a book older than this accrues a spot no exposure
MAX_GAP_S = 300.0             # nor does a gap between looks longer than this
SLOW_CHECK_S = 10.0           # Tier 4's spots are looked at this often
SPOT_MAX_SLOW = 800           # at most this many Tier 4 spots at once
MARKOUT_H = (600, 3600)       # a filled spot's loss is read this long after its fill
MARKOUT_SLACK_S = 300.0
DAY_S = 86400.0
# fills a day before anything is measured: the fill model's own priors
# (fillmodel.PRIOR_HAZARD_PER_DAY), a front-of-line order a little more
# and a deep line ahead of us half
HAZ_PRIOR = {"front": 0.5, "touch": 0.35, "near": 0.15, "mid": 0.07, "far": 0.03}
HAZ_DEEP = 0.5
HAZ_PRIOR_S = DAY_S           # the prior is worth one day of exposure
DEEP_Q = 0.25                 # a line ahead of this share of the Target Size is deep
LOSS_PRIOR = 0.02             # the loss a share on a fill before any is measured ($)
LOSS_PRIOR_N = 20.0           # ...worth this many fills
LOSS_MARKET_N = 5.0           # a market's own losses are pulled toward its tier's by this many
LOSS_HALF_LIFE_S = 3 * DAY_S
LOSS_M_KEEP = 3000            # markets' own loss records kept (the newest), for the save
COC_DAY = 0.005               # his cost of capital, a dollar a day (the focus page's)
FILL_FLOOR = 0.02             # his fill floor (the focus page's)
PAGE_TOP = 12                 # the page lists this many of a tier's orders
VERSION = "value-2026-09-26"


def dist_bucket(side: str, px: float, touch: float | None, tick: float) -> str:
    """How far behind the side's best price (ours netted out) `px` sits."""
    if touch is None:
        return "front"
    d = (touch - px) if side == "BUY" else (px - touch)
    eps = tick / 2.0
    if d < -eps:
        return "front"
    if d <= eps:
        return "touch"
    if d <= 0.01 + eps:
        return "near"
    if d <= 0.03 + eps:
        return "mid"
    return "far"


def ahead_of(side: str, levels, px: float, tick: float) -> float:
    """Shares a new order at `px` would sit behind: every level better
    than it and the level itself."""
    eps = tick / 2.0
    if side == "BUY":
        return sum(q for p, q in levels if p > px - eps)
    return sum(q for p, q in levels if p < px + eps)


def side_share(side: str, levels, tick: float, df: float, target: float,
               ours: dict) -> tuple[float, bool, float]:
    """Our share of a side's pool with `ours` ({price: shares}) resting on
    it among `levels` (everyone else's, best first): the exchange's
    arithmetic, the level reading scoring.py acts on — a level the walk
    to Target Size reaches scores whole. Returns (share, the side holds
    its Target Size, shares the side is short of it)."""
    tick = tick or 0.01
    eps = tick / 2.0
    buy = side == "BUY"
    mine = sorted(((float(p), float(q)) for p, q in ours.items() if q > 0),
                  key=lambda x: -x[0] if buy else x[0])
    # one walk down the two lists at once, best price first
    rows: list[tuple[float, float, float]] = []       # (price, everyone, ours)
    i = j = 0
    n, m = len(levels), len(mine)
    while i < n or j < m:
        if j >= m:
            p, q = levels[i]
            rows.append((float(p), float(q), 0.0))
            i += 1
            continue
        if i >= n:
            rows.append((mine[j][0], mine[j][1], mine[j][1]))
            j += 1
            continue
        lp = float(levels[i][0])
        mp = mine[j][0]
        if abs(lp - mp) < eps:
            rows.append((lp, float(levels[i][1]) + mine[j][1], mine[j][1]))
            i += 1
            j += 1
        elif (lp > mp) if buy else (lp < mp):
            rows.append((lp, float(levels[i][1]), 0.0))
            i += 1
        else:
            rows.append((mp, mine[j][1], mine[j][1]))
            j += 1
    if not rows:
        return 0.0, False, float(target or 0.0)
    total = 0.0
    for _p, q, _o in rows:
        total += q
    if target and total < target - 1e-9:
        return 0.0, False, float(target) - total
    best = rows[0][0]
    cum = denom = sc = 0.0
    for p, q, o in rows:
        w = df ** round(abs(best - p) / tick)
        denom += q * w
        sc += o * w
        cum += q
        if target and cum >= target - 1e-9:
            break
    return (sc / denom if denom > 0 else 0.0), True, 0.0


def window_denom(levels, tick: float, df: float, target: float) -> float:
    """Everyone else's score inside the Target Size window, from their own
    best price."""
    if not levels:
        return 0.0
    tick = tick or 0.01
    best = float(levels[0][0])
    cum = d = 0.0
    for p, q in levels:
        d += float(q) * df ** round(abs(best - float(p)) / tick)
        cum += float(q)
        if target and cum >= target - 1e-9:
            break
    return d


def candidates(side: str, own, other, tick: float, back_c: float = CAND_BACK_C,
               n_max: int = CAND_MAX) -> list[float]:
    """Prices worth a look, nearest first: a tick inside the touch where
    the spread leaves room, the touch, CAND_TICKS behind it, and every
    resting level within `back_c` behind — never at or through the other
    side (post-only)."""
    tick = tick or 0.01
    sign = 1.0 if side == "BUY" else -1.0
    touch = float(own[0][0]) if own else None
    opp = float(other[0][0]) if other else None
    if touch is None and opp is None:
        return []
    start = touch if touch is not None else opp - sign * tick
    out: list[float] = []
    if touch is not None and opp is not None and abs(opp - touch) > 1.5 * tick:
        out.append(touch + sign * tick)
    for k in range(0, CAND_TICKS + 1):
        out.append(start - k * sign * tick)
    for p, _q in own or ():
        back = (start - float(p)) * sign
        if CAND_TICKS * tick + 1e-9 < back <= back_c + 1e-9:
            out.append(float(p))
    res = []
    for p in out:
        p = round(p, 4)
        if not (0.001 <= p <= 0.999):
            continue
        if opp is not None and ((p >= opp - 1e-9) if side == "BUY" else (p <= opp + 1e-9)):
            continue
        if p not in res:
            res.append(p)
    return res[:n_max]


class Tally:
    """A decaying signed mean: the loss a share on a fill."""

    __slots__ = ("n", "s", "at")

    def __init__(self, n=0.0, s=0.0, at=0.0):
        self.n, self.s, self.at = float(n), float(s), float(at)

    def add(self, x: float, now: float) -> None:
        if self.at:
            k = 0.5 ** (max(now - self.at, 0.0) / LOSS_HALF_LIFE_S)
            self.n *= k
            self.s *= k
        self.at = now
        self.n += 1.0
        self.s += float(x)

    def mean(self) -> float | None:
        return self.s / self.n if self.n > 0 else None

    def to_list(self) -> list:
        return [round(self.n, 4), round(self.s, 6), round(self.at, 1)]


class Step:
    """One slice (or one carry over the Target Size) of a plan."""

    __slots__ = ("key", "tier", "slug", "side", "px", "qty", "cost", "dr", "df_", "dk")

    def __init__(self, key, tier, slug, side, px, qty, cost, dr, df_, dk):
        self.key, self.tier, self.slug, self.side = key, tier, slug, side
        self.px, self.qty, self.cost = px, qty, cost
        self.dr, self.df_, self.dk = dr, df_, dk

    @property
    def value(self) -> float:
        return self.dr - self.df_ - self.dk


class Side:
    """One side of one market as the plan sees it: everyone else's
    levels, the candidates, and what the plan has put there so far."""

    __slots__ = ("tier", "slug", "side", "levels", "tick", "df", "target", "pool",
                 "touch", "cands", "ours", "share", "h", "l", "coc", "first_day")

    def __init__(self, tier, slug, side, levels, other, tick, df, target, pool,
                 haz, loss, coc, first_day, n_max, cands=None):
        self.tier, self.slug, self.side = tier, slug, side
        self.levels = levels
        self.tick = tick or 0.01
        self.df, self.target, self.pool = float(df), float(target), float(pool)
        self.touch = float(levels[0][0]) if levels else None
        self.cands = (list(cands) if cands is not None
                      else candidates(side, levels, other, self.tick, n_max=n_max))
        self.ours: dict[float, float] = {}
        self.share = 0.0
        # everyone else's book is fixed for the plan, so each price's
        # fills a day and loss a share are worked out once
        self.h = {px: haz(side, px, levels, self.touch) for px in self.cands}
        self.l = {px: loss(px) for px in self.cands}
        self.coc = coc
        self.first_day = first_day

    def cps(self, px: float) -> float:
        """Collateral a share: a bid pays its price, an ask the rest."""
        return px if self.side == "BUY" else 1.0 - px

    def _eval(self, px: float, qty: float, alone: bool = False) -> Step:
        """What `qty` more at `px` adds: on top of what the plan already
        put on this side, or with `alone` as the side's only order."""
        ours = {} if alone else dict(self.ours)
        base = 0.0 if alone else self.share
        ours[px] = ours.get(px, 0.0) + qty
        sh, _ok, _gap = side_share(self.side, self.levels, self.tick, self.df,
                                   self.target, ours)
        dr = 0.0 if self.first_day else self.pool * (sh - base)
        cost = qty * self.cps(px)
        dfill = self.h[px] * qty * self.l[px]
        dk = cost * self.coc
        v = dr - dfill - dk
        return Step(v / cost if cost > 0 else -1e9, self.tier, self.slug, self.side,
                    px, qty, cost, dr, dfill, dk)

    def next_step(self, room: float) -> Step | None:
        """The best next slice (or carry over the Target Size) that fits
        in `room` dollars, by value a dollar; None when none adds value."""
        best = None
        _sh, ok, gap = side_share(self.side, self.levels, self.tick, self.df,
                                  self.target, self.ours)
        for px in self.cands:
            c = self.cps(px)
            if c <= 0:
                continue
            tries = [SLICE_USD / c]
            if not ok and gap > 0:
                # carry the side over its Target Size in one step
                tries.append(gap * (1.0 + GAP_CUSHION) + 1.0)
            for q in tries:
                q = math.floor(q * 100.0) / 100.0
                if q <= 0 or q * c > room + 1e-9:
                    continue
                st = self._eval(px, q)
                if best is None or st.key > best.key:
                    best = st
        if best is None or best.value <= 0:
            return None
        return best

    def apply(self, st: Step) -> None:
        self.ours[st.px] = self.ours.get(st.px, 0.0) + st.qty
        self.share, _ok, _gap = side_share(self.side, self.levels, self.tick, self.df,
                                           self.target, self.ours)


def run_greedy(sides: list[Side], budget: float, cap: float) -> list[Step]:
    """Money out in slices, each to the side whose next slice adds the most
    value a dollar, until the budget is spent or nothing adds value. No
    market past `cap`. The steps in the order taken."""
    heap = []
    spent_m: dict[str, float] = {}
    for i, sd in enumerate(sides):
        st = sd.next_step(min(cap, budget))
        if st is not None:
            heap.append((-st.key, i, st))
    heapq.heapify(heap)
    out: list[Step] = []
    spent = 0.0
    while heap and spent < budget - 1e-9:
        _k, i, st = heapq.heappop(heap)
        sd = sides[i]
        room_m = cap - spent_m.get(sd.slug, 0.0)
        room = min(room_m, budget - spent)
        if st.cost > room + 1e-9:
            # it no longer fits (the market's other side took the room):
            # this side's best that does, if any
            st = sd.next_step(room)
            if st is not None:
                heapq.heappush(heap, (-st.key, i, st))
            continue
        sd.apply(st)
        spent += st.cost
        spent_m[sd.slug] = spent_m.get(sd.slug, 0.0) + st.cost
        out.append(st)
        nxt = sd.next_step(min(cap - spent_m[sd.slug], budget - spent))
        if nxt is not None:
            heapq.heappush(heap, (-nxt.key, i, nxt))
    return out


def take(steps, budget: float, cap: float) -> list[Step]:
    """The plan at `budget` from a run made with a budget at least as big
    and the same `cap`: the steps in order, a side stopping at the first
    of its steps that no longer fits (its later steps assumed it)."""
    spent = 0.0
    per_m: dict[str, float] = {}
    blocked: set = set()
    out = []
    for st in steps:
        sid = (st.slug, st.side)
        if sid in blocked:
            continue
        m = per_m.get(st.slug, 0.0)
        if m + st.cost > cap + 1e-9 or spent + st.cost > budget + 1e-9:
            blocked.add(sid)
            continue
        per_m[st.slug] = m + st.cost
        spent += st.cost
        out.append(st)
    return out


def summary(steps) -> dict:
    orders: dict[tuple, float] = {}
    r = f = k = c = 0.0
    for st in steps:
        r += st.dr
        f += st.df_
        k += st.dk
        c += st.cost
        key = (st.slug, st.side, st.px)
        orders[key] = orders.get(key, 0.0) + st.qty
    return {"value": round(r - f - k, 2), "reward": round(r, 2), "fills": round(f, 2),
            "capital": round(k, 2), "coll": round(c, 2), "orders": len(orders),
            "markets": len({s for s, _sd, _p in orders})}


class Spot:
    """A paper order: laid at a side's best price and followed."""

    __slots__ = ("tier", "slug", "side", "px", "qty", "ahead", "t0", "last", "tseen",
                 "pf", "loss", "tick")

    def __init__(self, tier, slug, side, px, qty, ahead, t0, tseen, pf, loss, tick):
        self.tier, self.slug, self.side = tier, slug, side
        self.px, self.qty, self.ahead = px, qty, ahead
        self.t0 = self.last = t0
        self.tseen = tseen
        self.pf, self.loss, self.tick = pf, loss, tick


class TierValue:
    """`tf` is the tier fairs (its markets, books and fairs); `fam` the
    politics family (the pools, the first-day record, our own orders);
    `pay()` gives (the meter's estimate by "day|market", what the
    exchange paid by "day|market"); `coc()` and `floor()` his cost of
    capital and fill floor from the focus page."""

    def __init__(self, tf, fam, pay=None, coc=None, floor=None, prints=None, clock=None):
        self.tf = tf
        self.fam = fam
        self.pay = pay or (lambda: ({}, {}))
        self.coc = coc or (lambda: COC_DAY)
        self.floor = floor or (lambda: FILL_FLOOR)
        self.prints = prints or (lambda s: None)
        self._clock = clock or time.time
        # (tier, side, dist, queue) -> [exposure seconds, fills]
        self.cells: dict[tuple, list[float]] = {}
        # (tier, side, horizon) -> Tally of the loss a share; slug -> Tally at an hour
        self.loss_t: dict[tuple, Tally] = {}
        self.loss_m: dict[str, Tally] = {}
        # tier -> [spots graded, fill odds summed, fills seen, squared error]
        self.grades: dict[str, list[float]] = {}
        self.spots: dict[tuple, Spot] = {}
        self.marks: list[dict] = []            # filled spots awaiting their loss
        self.plans: dict[str, list[Step]] = {}  # tier -> its run to the pot
        self.rows: dict[str, dict] = {}         # slug -> the page's line, once a plan
        self._ctx: dict[str, dict] = {}         # slug -> the book and terms it was planned on
        self._planned: dict[str, dict] = {}     # tier -> (slug, side, px) -> shares
        self.meta: dict[str, dict] = {}         # tier -> sides, fresh books
        self.last_fast = self.last_slow = self.last_check_slow = 0.0
        self.plan_s: dict[str, float] = {}
        self.plan_at = 0.0
        self.error = ""
        self._view: dict = {"ok": False, "note": "stage 2 has not planned yet"}

    # -- the numbers ------------------------------------------------------------

    def _key(self, tier, side, px, levels, touch, tick, target) -> tuple:
        dist = dist_bucket(side, px, touch, tick)
        q = ahead_of(side, levels, px, tick) if dist != "front" else 0.0
        deep = "deep" if target and q >= DEEP_Q * target else "thin"
        return (tier, side, dist, deep)

    def hazard(self, key: tuple) -> float:
        """Fills a day at a cell: measured, from a prior worth a day."""
        _t, _s, dist, deep = key
        r0 = HAZ_PRIOR.get(dist, 0.03) * (HAZ_DEEP if deep == "deep" else 1.0)
        exp_s, fills = self.cells.get(key, (0.0, 0.0))
        p_days = HAZ_PRIOR_S / DAY_S
        return (fills + r0 * p_days) / (exp_s / DAY_S + p_days)

    def loss_ps(self, tier: str, side: str, slug: str) -> float:
        """The loss a share on a fill: the tier's measured an hour after,
        from a 2c prior, pulled toward the market's own; never under his
        floor."""
        t = self.loss_t.get((tier, side, 3600))
        n_t, m_t = (t.n, t.mean() or 0.0) if t else (0.0, 0.0)
        lt = (n_t * m_t + LOSS_PRIOR_N * LOSS_PRIOR) / (n_t + LOSS_PRIOR_N)
        m = self.loss_m.get(f"{slug}|{side}")
        if m is not None and m.n > 0:
            lt = (m.n * (m.mean() or 0.0) + LOSS_MARKET_N * lt) / (m.n + LOSS_MARKET_N)
        try:
            fl = float(self.floor())
        except Exception:  # noqa: BLE001
            fl = FILL_FLOOR
        return max(lt, fl)

    def _coc(self) -> float:
        try:
            return float(self.coc())
        except Exception:  # noqa: BLE001
            return COC_DAY

    def _ours(self) -> dict[str, dict]:
        """Our own resting orders by market — the tender's and the
        engine's — to take out of the book: the tier engine starts from
        nothing. His hand's (purpose "manual") stay in as company."""
        out: dict[str, dict] = {}
        for _ in range(2):
            try:
                recs = list(dict(self.fam.orders).values())
                break
            except RuntimeError:                  # changed while read: once more
                recs = []
        for r in recs:
            if getattr(r, "purpose", "") == "manual":
                continue
            d = out.setdefault(r.market, {})
            k = (r.side, round(float(r.price), 4))
            d[k] = d.get(k, 0.0) + float(r.qty)
        return out

    @staticmethod
    def _net(levels, mine: dict, side: str) -> list:
        if not mine:
            return [(float(p), float(q)) for p, q in levels]
        out = []
        for p, q in levels:
            q2 = float(q) - mine.get((side, round(float(p), 4)), 0.0)
            if q2 > 1e-9:
                out.append((float(p), q2))
        return out

    def _books(self, slug: str, now: float, mine: dict, max_age: float):
        bk = self.tf._book(slug)
        if bk is None or now - float(bk.fetched_at or 0.0) > max_age:
            return None
        return (self._net(bk.bids, mine, "BUY"), self._net(bk.asks, mine, "SELL"),
                float(bk.tick or 0.01))

    # -- the plan ---------------------------------------------------------------

    def _mk(self) -> dict:
        """The tier markets: the fairs' own ground, rebuilt every 10 s,
        rather than a walk of the whole ledger each time."""
        mk = getattr(self.tf, "_mk", None)
        return mk if mk is not None else self.tf.markets()

    def _sides(self, tier_keys, now: float, mine_all: dict) -> tuple[list[Side], dict]:
        """Every side worth planning on, and the ground each tier's markets
        stand on (kept so the page can price a market the plan skipped)."""
        mk = self._mk()
        try:
            first = set(self.fam.terms.joined_today(now))
        except Exception:  # noqa: BLE001
            first = set()
        coc = self._coc()
        out: list[Side] = []
        meta: dict[str, dict] = {}
        cap = MARKET_CAP_FRAC * POT_USD
        for slug in [s_ for s_, c_ in self._ctx.items() if c_["tier"] in tier_keys]:
            del self._ctx[slug]
        for slug, (tier, prog) in mk.items():
            if tier not in tier_keys:
                continue
            m = meta.setdefault(tier, {"markets": 0, "fresh": 0, "sides": 0, "first_day": 0,
                                       "prints": 0})
            m["markets"] += 1
            nb = self._books(slug, now, mine_all.get(slug) or {}, PLAN_BOOK_MAX_S)
            if nb is None:
                continue
            m["fresh"] += 1
            if self.fam.cache.trade_seen.get(slug) if hasattr(self.fam, "cache") else None:
                m["prints"] += 1
            bids, asks, tick = nb
            try:
                pool = self.fam._side_pool(slug, prog)
            except Exception:  # noqa: BLE001
                pool = None
            if not pool or not prog.df or not prog.target:
                continue
            fd = slug in first
            m["first_day"] += int(fd)
            fair = (self.tf.cur.get(slug) or {}).get("fair")
            n_max = CAND_MAX_SLOW if tier in SLOW_TIERS else CAND_MAX
            self._ctx[slug] = {"tier": tier, "bids": bids, "asks": asks, "tick": tick,
                               "prog": prog, "pool": pool, "fd": fd, "fair": fair,
                               "n_max": n_max}
            slow = tier in SLOW_TIERS
            if fd and slow:
                continue              # a first day pays nothing: nothing to plan
            for side, own, other in (("BUY", bids, asks), ("SELL", asks, bids)):
                cands = candidates(side, own, other, tick, n_max=n_max)
                if not cands:
                    continue
                # Tier 4's ~12,000 sides are screened on a bound first;
                # Tiers 1-3 keep every side, so each gets its paper spot
                if slow and not self._may_pay(side, own, cands, tick, prog, pool, coc):
                    continue
                sd = Side(tier, slug, side, own, other, tick, prog.df, prog.target, pool,
                          self._haz_fn(tier, tick, prog.target), self._loss_fn(
                              tier, side, slug, fair, own), coc, fd, n_max, cands=cands)
                out.append(sd)
                m["sides"] += 1
        return out, meta

    @staticmethod
    def _may_pay(side, own, cands, tick, prog, pool, coc) -> bool:
        """Whether any step on this side could beat the capital it ties up,
        on a bound no step can exceed: a slice's share is at most
        q / (q + df x (everyone else's window score - q)) — improving the
        touch a tick discounts the others by df, and our size can push at
        most q of theirs out of the window — and a carry over the Target
        Size claims at most the whole pool. Fills only cost more."""
        cps = [(px if side == "BUY" else 1.0 - px) for px in cands]
        cps = [c for c in cps if c > 0]
        if not cps:
            return False
        c_min = min(cps)
        target = float(prog.target)
        d0 = window_denom(own, tick, float(prog.df), target)
        q = SLICE_USD / c_min
        dr = pool * q / (q + float(prog.df) * max(d0 - q, 0.0))
        if dr / SLICE_USD - coc > 0:
            return True
        total = sum(float(qq) for _p, qq in own)
        if total < target:
            gap_cost = (target - total) * c_min
            if gap_cost <= MARKET_CAP_FRAC * POT_USD and pool / max(gap_cost, 1e-9) - coc > 0:
                return True
        return False

    def _haz_fn(self, tier, tick, target):
        def f(side, px, levels, touch):
            return self.hazard(self._key(tier, side, px, levels, touch, tick, target))
        return f

    def _loss_fn(self, tier, side, slug, fair, own):
        base = self.loss_ps(tier, side, slug)
        cur = float(own[0][0]) if own else None

        def f(px):
            conc = 0.0
            if fair is not None:
                past = (px - fair) if side == "BUY" else (fair - px)
                if past > 0:
                    unwind = (float(fair) + (cur if cur is not None else px)) / 2.0
                    conc = max((px - unwind) if side == "BUY" else (unwind - px), 0.0)
            return base + conc
        return f

    def plan(self, tier_keys, now: float, mine_all: dict | None = None) -> None:
        mine_all = self._ours() if mine_all is None else mine_all
        t0 = time.time()
        sides, meta = self._sides(tier_keys, now, mine_all)
        cap = MARKET_CAP_FRAC * POT_USD
        by_tier: dict[str, list[Side]] = {}
        for sd in sides:
            by_tier.setdefault(sd.tier, []).append(sd)
        for tier in tier_keys:
            ss = by_tier.get(tier, [])
            self.plans[tier] = run_greedy(ss, POT_USD, cap)
            self.meta[tier] = meta.get(tier, {"markets": 0, "fresh": 0, "sides": 0,
                                              "first_day": 0})
            self._lay_spots(tier, ss, now, mine_all)
            self._rows(tier, ss)
        self.plan_s["slow" if set(tier_keys) & set(SLOW_TIERS) else "fast"] = round(
            time.time() - t0, 3)
        self.plan_at = now

    def _rows(self, tier: str, sides: list[Side]) -> None:
        """What the tier's run put on each market, for the page's line."""
        planned: dict[tuple, float] = {}
        for st in take(self.plans.get(tier, []), POT_USD, MARKET_CAP_FRAC * POT_USD):
            k = (st.slug, st.side, st.px)
            planned[k] = planned.get(k, 0.0) + st.qty
        self._planned[tier] = planned
        for slug in [s_ for s_, r in self.rows.items() if r.get("tier") == tier]:
            del self.rows[slug]

    def row(self, slug: str) -> dict | None:
        """The page's line for a market: each side's best single slice
        (priced alone, as the side's only order of ours) and what the
        tier's run put there. Worked out when the page asks, once a plan."""
        if slug in self.rows:
            return self.rows[slug]
        ctx = self._ctx.get(slug)
        if ctx is None:
            return None
        tier = ctx["tier"]
        prog = ctx["prog"]
        row: dict = {"tier": tier}
        if ctx["fd"]:
            row["first_day"] = True
        coc = self._coc()
        for side, own, other in (("BUY", ctx["bids"], ctx["asks"]),
                                 ("SELL", ctx["asks"], ctx["bids"])):
            cands = candidates(side, own, other, ctx["tick"], n_max=ctx["n_max"])
            if not cands:
                continue
            sd = Side(tier, slug, side, own, other, ctx["tick"], prog.df, prog.target,
                      ctx["pool"], self._haz_fn(tier, ctx["tick"], prog.target),
                      self._loss_fn(tier, side, slug, ctx["fair"], own), coc, ctx["fd"],
                      ctx["n_max"], cands=cands)
            best = None
            for px in cands:
                c = sd.cps(px)
                if c <= 0:
                    continue
                st = sd._eval(px, math.floor(SLICE_USD / c * 100.0) / 100.0, alone=True)
                if best is None or st.key > best.key:
                    best = st
            if best is not None:
                h = sd.h[best.px]
                row[side] = {"px": best.px, "qty": round(best.qty, 2),
                             "reward": round(best.dr, 4), "fills": round(best.df_, 4),
                             "capital": round(best.dk, 4), "value": round(best.value, 4),
                             "pf1h": round(1.0 - math.exp(-h / 24.0), 4),
                             "loss_c": round(sd.l[best.px] * 100.0, 2)}
            mine = [(px, round(q, 2)) for (s_, sde, px), q in
                    (self._planned.get(tier) or {}).items() if s_ == slug and sde == side]
            if mine:
                row.setdefault("plan", {})[side] = sorted(mine, reverse=(side == "BUY"))
        self.rows[slug] = row
        return row

    # -- paper spots ---------------------------------------------------------------

    def _lay_spots(self, tier: str, sides: list[Side], now: float, mine_all: dict) -> None:
        firsts: dict[tuple, Step] = {}
        for st in self.plans.get(tier, []):
            firsts.setdefault((st.slug, st.side), st)
        slow = tier in SLOW_TIERS
        n_slow = sum(1 for sp in self.spots.values() if sp.tier in SLOW_TIERS)
        for sd in sides:
            sid = (sd.slug, sd.side)
            if sid in self.spots:
                continue
            st = firsts.get(sid)
            if st is None:
                if slow or not sd.cands:
                    continue          # Tier 4: only where the plan would rest
                px = sd.cands[1] if len(sd.cands) > 1 and sd.touch is not None and \
                    abs(sd.cands[0] - sd.touch) > sd.tick / 2 else sd.cands[0]
                qty = math.floor(SLICE_USD / max(sd.cps(px), 0.01) * 100.0) / 100.0
            else:
                px, qty = st.px, st.qty
            if slow:
                if n_slow >= SPOT_MAX_SLOW:
                    continue
                n_slow += 1
            key = self._key(tier, sd.side, px, sd.levels, sd.touch, sd.tick, sd.target)
            h = self.hazard(key)
            pf = 1.0 - math.exp(-h * SPOT_H_S / DAY_S)
            level = sum(q for p, q in sd.levels if abs(p - px) < sd.tick / 2.0)
            ts = self.fam.cache.trade_seen.get(sd.slug) if hasattr(self.fam, "cache") else None
            self.spots[sid] = Spot(tier, sd.slug, sd.side, px, qty, level, now,
                                   max(ts) if ts else 0.0, pf, sd.l.get(px, LOSS_PRIOR),
                                   sd.tick)

    def check_spots(self, now: float, slow: bool, mine_all: dict | None = None) -> None:
        mine_all = self._ours() if mine_all is None else mine_all
        for sid, sp in list(self.spots.items()):
            if (sp.tier in SLOW_TIERS) != slow:
                continue
            self._check(sid, sp, now, mine_all.get(sp.slug) or {})

    def _check(self, sid, sp: Spot, now: float, mine: dict) -> None:
        nb = self._books(sp.slug, now, mine, SPOT_STALE_S)
        dt = min(max(now - sp.last, 0.0), MAX_GAP_S)
        sp.last = now
        filled = False
        if nb is not None:
            bids, asks, tick = nb
            eps = tick / 2.0
            own, other = (bids, asks) if sp.side == "BUY" else (asks, bids)
            # the other side at or through our price: a taker came to us
            crossed = bool(other) and ((other[0][0] <= sp.px + eps) if sp.side == "BUY"
                                       else (other[0][0] >= sp.px - eps))
            # anything that left our level may have been ahead of us
            at_level = sum(q for p, q in own if abs(p - sp.px) < eps)
            sp.ahead = min(sp.ahead, at_level)
            through = at = False
            ts = (self.fam.cache.trade_seen.get(sp.slug) if hasattr(self.fam, "cache")
                  else None)
            if ts and max(ts) > sp.tseen + 1e-6:
                sp.tseen = max(ts)
                pr = None
                try:
                    pr = self.prints(sp.slug)
                except Exception:  # noqa: BLE001
                    pr = None
                if pr:
                    x = float(pr[0])
                    through = (x < sp.px - eps) if sp.side == "BUY" else (x > sp.px + eps)
                    at = abs(x - sp.px) <= eps
            filled = crossed or through or (at and sp.ahead <= 1e-9)
            touch = float(own[0][0]) if own else None
            key = self._key(sp.tier, sp.side, sp.px, own, touch, tick,
                            self._target(sp.slug))
            c = self.cells.setdefault(key, [0.0, 0.0])
            c[0] += dt
            if filled:
                c[1] += 1.0
        if filled:
            self._end(sid, sp, 1.0, now)
            self.marks.append({"tier": sp.tier, "slug": sp.slug, "side": sp.side,
                               "px": sp.px, "ts": now, "done": []})
        elif now - sp.t0 >= SPOT_H_S:
            self._end(sid, sp, 0.0, now)

    def _target(self, slug: str) -> float:
        prog = self.fam.terms.current.get(slug) if hasattr(self.fam, "terms") else None
        return float(getattr(prog, "target", 0.0) or 0.0)

    def _end(self, sid, sp: Spot, got: float, now: float) -> None:
        self.spots.pop(sid, None)
        g = self.grades.setdefault(sp.tier, [0.0, 0.0, 0.0, 0.0])
        g[0] += 1.0
        g[1] += sp.pf
        g[2] += got
        g[3] += (sp.pf - got) ** 2

    def grade_marks(self, now: float) -> None:
        """A filled spot's loss a share, read against the midpoint
        MARKOUT_H after its fill: a bid lost what the price fell below
        it, an ask what it rose above."""
        keep = []
        for m in self.marks:
            for h in MARKOUT_H:
                if h in m["done"] or now - m["ts"] < h:
                    continue
                m["done"].append(h)
                if now - m["ts"] > h + MARKOUT_SLACK_S:
                    continue
                r = self.tf.cur.get(m["slug"]) or {}
                mid = r.get("mid")
                if mid is None or float(r.get("spread") or 1.0) > MID_TRUST_SPREAD:
                    continue
                lost = (m["px"] - mid) if m["side"] == "BUY" else (mid - m["px"])
                self.loss_t.setdefault((m["tier"], m["side"], h), Tally()).add(lost, now)
                if h == 3600:
                    self.loss_m.setdefault(f"{m['slug']}|{m['side']}", Tally()).add(lost, now)
            if len(m["done"]) < len(MARKOUT_H):
                keep.append(m)
        self.marks = keep[-5000:]

    # -- the clock ---------------------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        mine_all = self._ours()
        fast = tuple(k for k, _n, _p in TIERS if k not in SLOW_TIERS)
        self.check_spots(now, slow=False, mine_all=mine_all)
        if now - self.last_check_slow >= SLOW_CHECK_S:
            self.last_check_slow = now
            self.check_spots(now, slow=True, mine_all=mine_all)
        self.grade_marks(now)
        planned = False
        if now - self.last_fast >= PLAN_FAST_S:
            self.last_fast = now
            self.plan(fast, now, mine_all)
            planned = True
        if now - self.last_slow >= PLAN_SLOW_S:
            self.last_slow = now
            self.plan(SLOW_TIERS, now, mine_all)
            planned = True
        if planned:
            self._view = self._build_view(now)
        if len(self.loss_m) > LOSS_M_KEEP:
            for k in sorted(self.loss_m, key=lambda k: self.loss_m[k].at)[
                    :len(self.loss_m) - LOSS_M_KEEP]:
                del self.loss_m[k]

    # -- the page ----------------------------------------------------------------

    def _pay(self) -> dict:
        """What the exchange paid against the meter's estimate for our real
        orders, per tier, on the days the tier programs have run — only
        markets posted that day, so a day still posting is not read low."""
        try:
            claims, paid = self.pay()
        except Exception:  # noqa: BLE001
            return {}
        mk = self._mk()
        out: dict[str, dict] = {}
        for k, est in list((claims or {}).items()):
            try:
                day, slug = k.split("|", 1)
            except ValueError:
                continue
            tp = mk.get(slug)
            if tp is None or not est or est <= 0:
                continue
            tier, prog = tp
            if day < str(getattr(prog, "start", "") or "")[:10]:
                continue          # before this program: another program's pay
            o = out.setdefault(tier, {"est": 0.0, "paid": 0.0, "markets": 0, "days": set(),
                                      "unposted_est": 0.0})
            pk = f"{day}|{slug}"
            if pk in (paid or {}):
                o["est"] += float(est)
                o["paid"] += float(paid[pk])
                o["markets"] += 1
                o["days"].add(day)
            else:
                o["unposted_est"] += float(est)
        for t, o in out.items():
            o["days"] = sorted(o["days"])
            o["ratio"] = round(o["paid"] / o["est"], 3) if o["est"] > 0 else None
            o["est"], o["paid"] = round(o["est"], 2), round(o["paid"], 2)
            o["unposted_est"] = round(o["unposted_est"], 2)
        return out

    def _build_view(self, now: float) -> dict:
        cap = MARKET_CAP_FRAC * POT_USD
        alone = {t: summary(take(self.plans.get(t, []), POT_USD, cap))
                 for t, _n, _p in TIERS}
        pos = {t: max(a["value"], 0.0) for t, a in alone.items()}
        tot = sum(pos.values())
        split = {}
        for t, _n, _p in TIERS:
            share = pos[t] / tot if tot > 0 else 0.0
            b = round(POT_USD * share, 2)
            split[t] = dict(summary(take(self.plans.get(t, []), b, cap)),
                            share=round(share, 4), budget=b)
        seqs = [self.plans.get(t, []) for t, _n, _p in TIERS]
        joint_steps = take(heapq.merge(*seqs, key=lambda s: -s.key), POT_USD, cap)
        joint = summary(joint_steps)
        jt: dict[str, dict] = {}
        for t, _n, _p in TIERS:
            jt[t] = summary([s for s in joint_steps if s.tier == t])
        joint["by_tier"] = jt
        pay = self._pay()
        tiers = {}
        for t, name, _p in TIERS:
            g = self.grades.get(t, [0.0, 0.0, 0.0, 0.0])
            lt = {}
            for sd in ("BUY", "SELL"):
                for h in MARKOUT_H:
                    x = self.loss_t.get((t, sd, h))
                    if x is not None and x.n > 0:
                        lt[f"{sd}|{h}"] = {"n": round(x.n, 1),
                                           "c": round((x.mean() or 0.0) * 100.0, 2)}
            top_steps = take(self.plans.get(t, []), split[t]["budget"], cap)
            orders: dict[tuple, list[float]] = {}
            for st in top_steps:
                o = orders.setdefault((st.slug, st.side, st.px), [0.0, 0.0, 0.0])
                o[0] += st.qty
                o[1] += st.value
                o[2] += st.cost
            top = sorted(orders.items(), key=lambda kv: -kv[1][1])[:PAGE_TOP]
            haz = {}
            for (tt, sd, dist, deep), (exp_s, fills) in self.cells.items():
                if tt == t:
                    haz[f"{sd}|{dist}|{deep}"] = {"days": round(exp_s / DAY_S, 2),
                                                 "fills": fills,
                                                 "per_day": round(self.hazard((tt, sd, dist, deep)), 3)}
            p = pay.get(t) or {}
            tiers[t] = {"name": name, "meta": self.meta.get(t, {}),
                        "alone": alone[t], "split": split[t],
                        "spots": {"active": sum(1 for sp in self.spots.values() if sp.tier == t),
                                  "graded": g[0], "pred": round(g[1], 2), "seen": g[2],
                                  "brier": round(g[3] / g[0], 4) if g[0] else None,
                                  "floor_only": t in SLOW_TIERS},
                        "loss": lt, "hazard": haz, "pay": p,
                        "top": [{"market": s, "name": self._name(s), "side": sd,
                                 "px": px, "qty": round(v[0], 2), "value": round(v[1], 2),
                                 "coll": round(v[2], 2)}
                                for (s, sd, px), v in top]}
        return {"ok": True, "stage": 2, "read_only": True, "version": VERSION,
                "pot": POT_USD, "market_cap": cap, "slice": SLICE_USD,
                "coc_day": self._coc(), "fill_floor": self.loss_floor(),
                "tiers": tiers, "joint": joint, "plan_at": self.plan_at,
                "plan_s": dict(self.plan_s), "error": self.error,
                "marks_waiting": len(self.marks)}

    def loss_floor(self) -> float:
        try:
            return float(self.floor())
        except Exception:  # noqa: BLE001
            return FILL_FLOOR

    def _name(self, slug: str) -> str:
        return str((self.fam.universe.get(slug) or {}).get("name") or slug)

    def view(self) -> dict:
        return self._view

    # -- persistence ----------------------------------------------------------------

    def to_dict(self) -> dict:
        return {"version": VERSION,
                "cells": {"|".join(k): [round(v[0], 1), v[1]] for k, v in self.cells.items()},
                "loss_t": {f"{t}|{s}|{h}": x.to_list() for (t, s, h), x in self.loss_t.items()},
                "loss_m": {k: x.to_list() for k, x in self.loss_m.items()},
                "grades": {t: [round(x, 4) for x in g] for t, g in self.grades.items()}}

    def restore(self, d: dict) -> None:
        for k, v in (d.get("cells") or {}).items():
            parts = tuple(k.split("|"))
            if len(parts) == 4:
                try:
                    self.cells[parts] = [float(v[0]), float(v[1])]
                except (TypeError, ValueError, IndexError):
                    continue
        for k, v in (d.get("loss_t") or {}).items():
            try:
                t, s, h = k.split("|")
                self.loss_t[(t, s, int(h))] = Tally(*v)
            except (TypeError, ValueError):
                continue
        for k, v in (d.get("loss_m") or {}).items():
            try:
                self.loss_m[k] = Tally(*v)
            except (TypeError, ValueError):
                continue
        for t, g in (d.get("grades") or {}).items():
            try:
                self.grades[t] = [float(x) for x in g][:4]
            except (TypeError, ValueError):
                continue

    def payload(self) -> bytes:
        try:
            return json.dumps(self._view).encode()
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "note": f"{type(e).__name__}: {e}"[:160]}).encode()
