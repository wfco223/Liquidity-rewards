"""Fill probability, fill cost, and their continuous calibration.

The owner's directive (2026-08-19): the engine should evaluate the cost
of filling at each price against the probability of a fill, estimate the
earnings an order collects before it is filled or pulled, keep those
predictions continuously calibrated, and pick the price that maximizes
the goal. This module is the prediction half; the engine does the
choosing.

**Fill probability.** A resting bid at price p fills when the market
trades down to p — observable as the best ask reaching p. So the model
watches every touch move the book feed already delivers and, for each
distance-behind-the-touch bucket, accrues exposure time and counts
crossings. Fills are modeled as a Poisson hazard per bucket:

    p_fill(within t) = 1 - exp(-hazard x t)

Buckets pool by family (senate seats / house seats / other) because a
single market yields too few events; a modest prior keeps day-one
estimates sane and real observation swamps it within days. Our own
orders' actual fills are the ground truth that grades this proxy.

**Fill cost.** Getting filled is adverse selection: the market was
moving through you. Cost per share = how far beyond your price the
market goes, measured by marking each real fill against the touch
mid an hour later — an EWMA per family, seeded at two ticks. On top of
that, resting past fair costs the excess immediately: a bid above fair
pays (price - fair) the moment it fills, and symmetrically for asks.

**Earning time.** Expected earnings before the order ends =
earn_rate x scoring fraction (how much of its resting life an order of
ours actually spends inside the window — an EWMA per family from our
own orders, seeded at 0.8).

Everything here persists, so calibration continues across restarts, and
everything is surfaced on the pages — a prediction nobody can inspect
is not calibrated, it is just confident.
"""

from __future__ import annotations

import math

DIST_BUCKETS = (0, 1, 2, 3)      # ticks behind the touch; 3 = "3 or more"
DAY_S = 86400.0

# Prior hazards per bucket, in fills per day, with this much prior
# exposure (seconds). Deliberately unremarkable numbers: the touch gets
# hit sometimes, three ticks back rarely. Real observation replaces them
# within days.
PRIOR_HAZARD_PER_DAY = {0: 0.35, 1: 0.15, 2: 0.07, 3: 0.03}
# A mispriced order is bait: until data proves otherwise, assume it fills
# fast. Each tick resting past fair value scales the PRIOR hazard up by
# this much; observed survival in the cell can still prove it down, and
# evidence moving the fair itself shrinks the measured mispricing
# (owner approved 2026-08-21).
BAIT_PER_TICK = 1.0
PRIOR_EXPOSURE_S = DAY_S

MARKDOWN_SEED = 0.02             # $/share adverse move on a fill, to start
OFFLOAD_SEED_DAYS = 2.0          # fill -> fully offloaded, until measured
OFFLOAD_ALPHA = 0.25             # EWMA weight per completed offload
MARKDOWN_ALPHA = 0.2             # EWMA weight of each new observed fill
SCORING_FRAC_SEED = 0.8
SCORING_FRAC_ALPHA = 0.1
MAX_OBS_GAP_S = 300.0            # don't accrue exposure across dead spells

# Our own fills are the ground truth (owner, 2026-09-04: "fit the touch
# hazard from our own fills per hour rested"). The crossing proxy counts
# a level being CLEARED — the opposite touch reaching it in a 20-second
# sample — which never sees the taker who eats part of the touch,
# including us, first in the queue, nor anything between samples. The
# 2026-09-04 audit: engine orders at the touch filled 2.7x what the
# proxy said; the owner's deep bids filled half of it. So the proxy is
# now only the PRIOR: each (family, side, distance) cell also accrues
# the seconds our own orders actually rested there and the fills they
# took, and once that exposure passes OWN_PRIOR_S the own-fill rate
# carries the estimate. A week of order-time: with 500 orders resting
# politics banks that in twenty minutes, a family with five orders in
# a day and a half — the proxy holds exactly as long as the family's
# own evidence is thin.
OWN_PRIOR_S = 7 * DAY_S
# Fresh orders fill fastest (same audit: a governor order fills 1.33/day
# in its first hour, 0.16/day after a day — the model averaged the two).
# The age factor rescales the hazard by the family's own fill rate at
# that age relative to its all-ages rate, shrunk toward 1 with this
# many pseudo-fills so a handful of events cannot swing it.
AGE_PRIOR_FILLS = 5.0


def family_of(slug: str) -> str:
    """Learning pool. One market rarely yields enough touch events, so
    hazards and costs pool across markets that trade alike."""
    parts = slug.split("-")
    if slug.startswith("scc-senate-gop-"):
        return "senate-seats"
    if slug.startswith("scc-hrep-rep-"):
        return "house-seats"
    if slug.startswith("vmc-"):
        return "margins"
    if any("usgub" in p for p in parts):
        return "governor"
    if any(p.startswith("usse") for p in parts):
        return "senate"
    if "2028" in parts:
        return "slate-2028"
    return "other"


# A wall of resting contracts in front of an order is protection: the
# taker has to eat through it first. Scaled by Target Size — the
# exchange's own declared depth unit for the market, so the same ratio
# means the same thing on a 2,000-contract book and a 20,000 one — and
# FLOORED, because a wall can vanish in a single print and an order
# sized as if a fill were impossible is exactly the risky spend the
# owner warned against.
SHIELD_FLOOR = 0.25


def shield_discount(shield: float, target: float) -> float:
    """Multiplier on the learned hazard for depth ahead of our price.
    No wall -> 1.0 (unchanged). A wall of one Target Size -> 0.5.
    Five -> the 0.25 floor."""
    if shield <= 0 or target <= 0:
        return 1.0
    return max(SHIELD_FLOOR, 1.0 / (1.0 + shield / target))


def _bucket(ticks_back: int) -> int:
    return min(max(int(ticks_back), 0), DIST_BUCKETS[-1])


AGE_BUCKETS = ((0, "0-1h", 3600), (1, "1-6h", 6 * 3600),
               (2, "6-24h", 24 * 3600), (3, "1d+", float("inf")))
TOD_BANDS = ((9, 17, "day"), (17, 23, "evening"), (23, 9, "night"))


def age_bucket(age_s: float) -> int:
    for idx, _label, hi in AGE_BUCKETS:
        if age_s < hi:
            return idx
    return 3


def tod_band(ts: float) -> str:
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        h = _dt.datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).hour
    except Exception:  # noqa: BLE001
        h = _dt.datetime.utcfromtimestamp(ts).hour - 4
    h %= 24
    for lo, hi, name in TOD_BANDS:
        if lo < hi and lo <= h < hi:
            return name
        if lo > hi and (h >= lo or h < hi):
            return name
    return "day"


class FillModel:
    def __init__(self):
        # (family, side, bucket) -> [exposure_seconds, crossings]
        self.obs: dict[str, list[float]] = {}
        # market -> (bid, ask, tick, ts) — last touch seen
        self._last: dict[str, tuple] = {}
        self.markdown: dict[str, float] = {}       # family -> $/share EWMA
        self.marks_n: dict[str, int] = {}          # family -> graded fills count
        self.scoring_frac: dict[str, float] = {}   # family -> EWMA 0..1
        # INSTRUMENTS, collecting only (owner-approved 2026-08-21): the
        # order-age survival question ("is a week-old order safer than an
        # hour-old one?") and the day/night question. Nothing acts on
        # these until the data speaks and the owner turns them on.
        self.age_obs: dict[str, list[float]] = {}   # fam|bucket -> [sec, fills]
        # the owner's fill-cost equation (2026-08-21): a fill's true cost
        # is the offload loss MINUS what the stock earns while the exit
        # rests. This learns how long offloading actually takes.
        self.offload_days: dict[str, float] = {}    # family -> EWMA days
        self.offload_n: dict[str, int] = {}
        # which resting approach pays (owner, 2026-08-21: "Collect some
        # data on a variety of approaches to see which pays off more
        # over time"): seconds rested and estimate-dollars accrued per
        # (family, side, distance bucket). COLLECT ONLY for now.
        self.approach_obs: dict[str, list[float]] = {}  # fam|side|b -> [sec, $sec/day]
        self.tod_obs: dict[str, list[float]] = {}   # fam|side|band -> [sec, crossings]
        # our own orders' resting time and fills, by distance and by
        # age (owner, 2026-09-04) — what the hazard is fit from now
        self.own_obs: dict[str, list[float]] = {}   # fam|side|b -> [sec, fills]
        self.age_fit: dict[str, list[float]] = {}   # fam|agebucket -> [sec, fills]

    @staticmethod
    def _key(family: str, side: str, bucket: int) -> str:
        return f"{family}|{side}|{bucket}"

    # -- learning from the feed ------------------------------------------------

    def observe_touch(self, slug: str, bid: float | None, ask: float | None,
                      tick: float, now: float) -> None:
        """One touch sample. For every distance bucket on each side, accrue
        the elapsed exposure; count a crossing when the market reached that
        depth (a bid d ticks behind the old best bid filled if the new best
        ask came down to it; symmetrically for asks)."""
        prev = self._last.get(slug)
        self._last[slug] = (bid, ask, tick, now)
        if prev is None:
            return
        pbid, pask, ptick, pts = prev
        dt = now - pts
        if dt <= 0 or dt > MAX_OBS_GAP_S:
            return
        fam = family_of(slug)
        band = tod_band(now)
        for side, ref, opp in (("BUY", pbid, ask), ("SELL", pask, bid)):
            if ref is not None:
                tk = f"{fam}|{side}|{band}"
                tcell = self.tod_obs.setdefault(tk, [0.0, 0.0])
                tcell[0] += dt
                if opp is not None and (
                        opp <= ref + 1e-12 if side == "BUY"
                        else opp >= ref - 1e-12):
                    tcell[1] += 1.0
            if ref is None:
                continue
            for b in DIST_BUCKETS:
                level = ref - b * ptick if side == "BUY" else ref + b * ptick
                if not (0.0 < level < 1.0):
                    continue
                k = self._key(fam, side, b)
                cell = self.obs.setdefault(k, [0.0, 0.0])
                cell[0] += dt
                if opp is not None and (
                        opp <= level + 1e-12 if side == "BUY"
                        else opp >= level - 1e-12):
                    cell[1] += 1.0

    def observe_order_age(self, slug: str, age_s: float, dt_s: float) -> None:
        k = f"{family_of(slug)}|{age_bucket(age_s)}"
        cell = self.age_obs.setdefault(k, [0.0, 0.0])
        cell[0] += dt_s

    def observe_fill_age(self, slug: str, age_s: float) -> None:
        k = f"{family_of(slug)}|{age_bucket(age_s)}"
        cell = self.age_obs.setdefault(k, [0.0, 0.0])
        cell[1] += 1.0

    def observe_approach(self, slug: str, side: str, ticks_back: int,
                         dt_s: float, est_day: float) -> None:
        k = f"{family_of(slug)}|{side}|{_bucket(ticks_back)}"
        cell = self.approach_obs.setdefault(k, [0.0, 0.0])
        cell[0] += dt_s
        cell[1] += (est_day or 0.0) * dt_s

    def observe_rest(self, slug: str, side: str, ticks_back: int,
                     age_s: float, dt_s: float) -> None:
        """Seconds one of our own orders just spent resting, at this
        distance behind its touch and at this age. Together with
        observe_own_fill this is the hazard's evidence."""
        if dt_s <= 0:
            return
        fam = family_of(slug)
        cell = self.own_obs.setdefault(self._key(fam, side, _bucket(ticks_back)),
                                       [0.0, 0.0])
        cell[0] += dt_s
        acell = self.age_fit.setdefault(f"{fam}|{age_bucket(age_s)}", [0.0, 0.0])
        acell[0] += dt_s

    def observe_own_fill(self, slug: str, side: str, ticks_back: int,
                         age_s: float) -> None:
        fam = family_of(slug)
        cell = self.own_obs.setdefault(self._key(fam, side, _bucket(ticks_back)),
                                       [0.0, 0.0])
        cell[1] += 1.0
        acell = self.age_fit.setdefault(f"{fam}|{age_bucket(age_s)}", [0.0, 0.0])
        acell[1] += 1.0

    def observe_offload(self, slug: str, days: float) -> None:
        fam = family_of(slug)
        cur = self.offload_days.get(fam, OFFLOAD_SEED_DAYS)
        self.offload_days[fam] = round(cur * (1 - OFFLOAD_ALPHA)
                                       + max(days, 0.0) * OFFLOAD_ALPHA, 3)
        self.offload_n[fam] = self.offload_n.get(fam, 0) + 1

    def expected_offload_days(self, slug: str) -> float:
        return self.offload_days.get(family_of(slug), OFFLOAD_SEED_DAYS)

    def observe_fill_mark(self, slug: str, side: str, fill_price: float,
                          mid_later: float) -> float:
        """Grade a real fill against the touch mid about an hour later.
        Adverse move = how far past our price the market stands (never
        negative — a fill that bounced back cost nothing extra)."""
        adverse = max((fill_price - mid_later) if side == "BUY"
                      else (mid_later - fill_price), 0.0)
        fam = family_of(slug)
        cur = self.markdown.get(fam, MARKDOWN_SEED)
        self.markdown[fam] = round(cur * (1 - MARKDOWN_ALPHA)
                                   + adverse * MARKDOWN_ALPHA, 4)
        self.marks_n[fam] = self.marks_n.get(fam, 0) + 1
        return adverse

    def observe_scoring(self, slug: str, in_window: bool) -> None:
        fam = family_of(slug)
        cur = self.scoring_frac.get(fam, SCORING_FRAC_SEED)
        self.scoring_frac[fam] = round(cur * (1 - SCORING_FRAC_ALPHA)
                                       + (1.0 if in_window else 0.0)
                                       * SCORING_FRAC_ALPHA, 4)

    # -- predictions ------------------------------------------------------------

    def crossing_hazard_per_day(self, family: str, side: str, ticks_back: int,
                                bait: float = 0.0) -> float:
        """The book-feed proxy: how often the opposite touch reached this
        depth. The prior for the own-fill estimate below."""
        b = _bucket(ticks_back)
        cell = self.obs.get(self._key(family, side, b), [0.0, 0.0])
        prior = (PRIOR_HAZARD_PER_DAY[b] * (1.0 + BAIT_PER_TICK * bait)
                 * PRIOR_EXPOSURE_S / DAY_S)
        return ((cell[1] + prior) / (cell[0] + PRIOR_EXPOSURE_S)) * DAY_S

    def hazard_per_day(self, family: str, side: str, ticks_back: int,
                       bait: float = 0.0) -> float:
        """Fills per day for one of our orders at this depth: our own
        fills over our own resting time in the cell, with the crossing
        proxy standing in for OWN_PRIOR_S of exposure until the cell has
        lived that long itself."""
        b = _bucket(ticks_back)
        cross = self.crossing_hazard_per_day(family, side, b, bait=bait)
        cell = self.own_obs.get(self._key(family, side, b), [0.0, 0.0])
        return ((cell[1] + cross * OWN_PRIOR_S / DAY_S)
                / (cell[0] + OWN_PRIOR_S)) * DAY_S

    def age_multiplier(self, family: str, age_s: float) -> float:
        """How much faster (or slower) this family's orders fill at this
        age than at any age. 1.0 until the family has fills."""
        cells = [(k, v) for k, v in self.age_fit.items()
                 if k.startswith(family + "|")]
        sec = sum(v[0] for _k, v in cells)
        fills = sum(v[1] for _k, v in cells)
        if sec <= 0 or fills <= 0:
            return 1.0
        rate = fills / sec
        s, n = self.age_fit.get(f"{family}|{age_bucket(age_s)}", [0.0, 0.0])
        return (n + AGE_PRIOR_FILLS) / (rate * s + AGE_PRIOR_FILLS)

    def age_factor(self, family: str, age_s: float,
                   horizon_s: float = DAY_S) -> float:
        """The age multiplier averaged over the order's NEXT horizon_s
        of life: a fresh order spends its first hour at the fresh rate,
        then ages through the slower buckets."""
        if horizon_s <= 0:
            return self.age_multiplier(family, age_s)
        acc = 0.0
        t = max(age_s, 0.0)
        end = t + horizon_s
        for _idx, _label, hi in AGE_BUCKETS:
            if t >= end:
                break
            if t >= hi:
                continue
            seg_end = min(hi, end)
            acc += self.age_multiplier(family, t) * (seg_end - t)
            t = seg_end
        return acc / horizon_s

    def p_fill(self, slug: str, side: str, ticks_back: int,
               horizon_s: float = DAY_S, shield: float = 0.0,
               target: float = 0.0, bait: float = 0.0,
               age_s: float = 0.0) -> float:
        """Chance of a fill over the horizon. `shield` is the contracts a
        taker must consume before reaching us — direct evidence against a
        fill that the distance-only hazard cannot see (owner, 2026-08-19:
        "the size of the walls is also evidence that an order won't get
        filled"). `bait` is ticks resting past fair value — a mispriced
        order is assumed to fill fast until data proves otherwise.
        `age_s` is how long the order has already rested: fresh orders
        fill fastest (owner, 2026-09-04)."""
        fam = family_of(slug)
        h = self.hazard_per_day(fam, side, ticks_back, bait=bait)
        h *= shield_discount(shield, target)
        h *= self.age_factor(fam, age_s, horizon_s)
        return 1.0 - math.exp(-h * horizon_s / DAY_S)

    def fill_cost(self, slug: str, side: str, price: float,
                  fair: float | None, exit_rate_ps: float = 0.0,
                  ignorance: float = 0.0) -> float:
        """$/share the fill is expected to cost, the owner's equation
        (2026-08-21): the offload loss (the calibrated adverse markdown
        plus anything conceded past fair) MINUS what the stock earns per
        share while its exit rests, over the measured time an offload
        takes. Can go negative where exits earn more than the fill
        loses — which is exactly a fill worth taking."""
        fam = family_of(slug)
        soft = self.markdown.get(fam, MARKDOWN_SEED)
        conc = max(ignorance, 0.0)
        if fair is not None:
            excess = (price - fair) if side == "BUY" else (fair - price)
            conc += max(excess, 0.0)
        # The exit credit may offset the markdown ONLY — never the
        # concession past fair NOR the ignorance premium, which is the
        # same concession estimated blind (UCLA, 2026-08-21: a no-model
        # book showed fill cost 0.0c ten ticks into the spread because
        # the credit ate the premium). Twice
        # burned (2026-08-21): the Rock — a negative cost made fills
        # look profitable; then Florida — the family-average credit
        # swallowed a 9c KNOWN overpay against a model-grounded 10.2c
        # fair, and 100 shares were bought at 19c showing fill cost
        # $0.00. An overpay is certain money lost at fill; the credit
        # is an unproven claim. Certain loss can't be paid for with an
        # unproven claim.
        credit = exit_rate_ps * self.expected_offload_days(slug)
        return round(conc + max(soft - credit, 0.0), 4)

    def scoring_fraction(self, slug: str) -> float:
        return self.scoring_frac.get(family_of(slug), SCORING_FRAC_SEED)

    # -- reporting & persistence ------------------------------------------------

    def summary(self) -> dict:
        out: dict = {"hazards": {}, "markdown": self.markdown,
                     "marks_n": self.marks_n, "scoring_frac": self.scoring_frac,
                     "prior_per_day": dict(PRIOR_HAZARD_PER_DAY)}
        fams = ({k.split("|")[0] for k in self.obs}
                | {k.split("|")[0] for k in self.own_obs}
                | {"senate-seats", "house-seats"})
        for fam in sorted(fams):
            for side in ("BUY", "SELL"):
                row = {}
                for b in DIST_BUCKETS:
                    cell = self.obs.get(self._key(fam, side, b))
                    own = self.own_obs.get(self._key(fam, side, b))
                    row[b] = {"per_day": round(self.hazard_per_day(fam, side, b), 4),
                              "crossing_per_day": round(
                                  self.crossing_hazard_per_day(fam, side, b), 4),
                              "hours_observed": round((cell or [0.0])[0] / 3600, 1),
                              "crossings": int((cell or [0.0, 0.0])[1]),
                              "hours_rested": round((own or [0.0])[0] / 3600, 1),
                              "own_fills": int((own or [0.0, 0.0])[1])}
                out["hazards"][f"{fam} {side}"] = row
        out["age"] = {}
        for fam in sorted(fams):
            row = {}
            for idx, label, _hi in AGE_BUCKETS:
                s, n = self.age_fit.get(f"{fam}|{idx}", [0.0, 0.0])
                row[label] = {"mult": round(self.age_multiplier(
                                  fam, 0.0 if idx == 0 else
                                  AGE_BUCKETS[idx - 1][2]), 3),
                              "hours_rested": round(s / 3600, 1),
                              "fills": int(n)}
            out["age"][fam] = row
        return out

    def to_dict(self) -> dict:
        return {"obs": {k: [round(v[0], 1), v[1]] for k, v in self.obs.items()},
                "markdown": self.markdown, "marks_n": self.marks_n,
                "scoring_frac": self.scoring_frac,
                "offload_days": self.offload_days,
                "offload_n": self.offload_n,
                "approach_obs": {k: [round(v[0], 1), round(v[1], 2)]
                                 for k, v in self.approach_obs.items()},
                "age_obs": {k: [round(v[0], 1), v[1]]
                            for k, v in self.age_obs.items()},
                "tod_obs": {k: [round(v[0], 1), v[1]]
                            for k, v in self.tod_obs.items()},
                "own_obs": {k: [round(v[0], 1), v[1]]
                            for k, v in self.own_obs.items()},
                "age_fit": {k: [round(v[0], 1), v[1]]
                            for k, v in self.age_fit.items()}}

    @classmethod
    def from_dict(cls, d: dict) -> "FillModel":
        m = cls()
        m.own_obs = {k: [float(v[0]), float(v[1])]
                     for k, v in (d.get("own_obs") or {}).items()}
        m.age_fit = {k: [float(v[0]), float(v[1])]
                     for k, v in (d.get("age_fit") or {}).items()}
        m.obs = {k: [float(v[0]), float(v[1])] for k, v in (d.get("obs") or {}).items()}
        m.markdown = dict(d.get("markdown") or {})
        m.marks_n = dict(d.get("marks_n") or {})
        m.scoring_frac = dict(d.get("scoring_frac") or {})
        m.offload_days = dict(d.get("offload_days") or {})
        m.offload_n = dict(d.get("offload_n") or {})
        m.approach_obs = {k: [float(v[0]), float(v[1])]
                          for k, v in (d.get("approach_obs") or {}).items()}
        m.age_obs = {k: [float(v[0]), float(v[1])]
                     for k, v in (d.get("age_obs") or {}).items()}
        m.tod_obs = {k: [float(v[0]), float(v[1])]
                     for k, v in (d.get("tod_obs") or {}).items()}
        return m
