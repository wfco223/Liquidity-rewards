"""ONE earned-today number.

1.0 kept three — the plain integration, the high-frequency one, and a
sparse fallback — and they disagreed because nobody decided which was
authoritative. Worse, the plain sampler was WOKEN by our own order
activity, so it sampled at exactly the moments our book looked best and
read high.

2.0's estimator:

* samples on its own clock. There is deliberately NO kick API on this
  class — nothing that places, moves or cancels orders can make it
  sample. Whoever schedules it calls sample() on a fixed or Poisson
  interval, full stop.
* integrates rate x elapsed into "earned today", with the elapsed time
  capped so a dead spell can't be billed at the last known rate.
* refuses to accrue when too few books are fresh — the stale seconds are
  banked and published instead, so a dead feed shows up as "X minutes
  unmeasured", never as invented earnings.
* reads terms from the ONE TermsStore. There is no second cache to go
  stale against.
* rolls the day at midnight Eastern (the exchange's reward day) and
  keeps each closed day so estimate-vs-paid is checkable per day.

NO correction factor. The number is the arithmetic on real inputs —
wrong output means a wrong input (owner's standing instruction).
"""

from __future__ import annotations

import datetime as dt

from .books import BookCache
from .scoring import Book, score_resting
from .terms import TermsStore

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # no tz database: fixed EDT offset, same fallback as 1.0
    ET = dt.timezone(dt.timedelta(hours=-4), "ET")

BOOK_MAX_AGE = 180.0   # a book older than this doesn't get scored
MAX_GAP_S = 300.0      # longest interval one sample may bill for
MIN_FRESH = 0.5        # book-freshness quorum below which nothing accrues
HISTORY_DAYS = 30


def et_day(now: float) -> str:
    return dt.datetime.fromtimestamp(now, tz=dt.timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def top_up_book(book: Book, own_orders) -> Book:
    """Guarantee our own resting size is present at its price level before
    scoring. Books and order snapshots are seconds apart; scoring a book
    that predates our order yields nonsense ("you 3 ticks from best"
    while we ARE the best). Ported from 1.0's self-consistency pass."""
    want: dict[tuple[str, float], float] = {}
    for o in own_orders:
        want[(o["side"], o["price"])] = want.get((o["side"], o["price"]), 0.0) + o["size"]
    if not want:
        return book
    sides = {"BUY": list(book.bids), "SELL": list(book.asks)}
    changed = False
    for (side, px), sz in want.items():
        levels = sides[side]
        have = sum(q for lp, q in levels if abs(lp - px) < 1e-9)
        if have < sz - 1e-9:
            levels[:] = [(lp, q) for lp, q in levels if abs(lp - px) >= 1e-9]
            levels.append((px, sz))
            changed = True
    if not changed:
        return book
    return Book(
        bids=tuple(sorted(sides["BUY"], key=lambda x: -x[0])),
        asks=tuple(sorted(sides["SELL"], key=lambda x: x[0])),
        tick=book.tick, fetched_at=book.fetched_at,
    )


class Estimator:
    def __init__(self):
        self.day: str | None = None
        self.earned = 0.0
        self.per_market: dict[str, float] = {}
        self.rate = 0.0                      # $/day from the latest sample
        self.market_rates: dict[str, float] = {}
        self.market_shares: dict[str, float] = {}
        self.market_pools: dict[str, float] = {}
        self.samples = 0
        self.covered_s = 0.0                 # seconds actually billed today
        self.stale_s = 0.0                   # seconds refused for staleness today
        # Owner, 2026-08-24: "Shouldn't there be thousands of estimates
        # ... Shouldn't they catch any changes in my share?" They do —
        # 4,320 a day at the 20s clock, each off a book under 180s old.
        # Which means a persistent error is a BIAS in the share
        # arithmetic, identical in every sample, and averaging cannot
        # touch it. So bank the ingredients of the comparison that
        # settles it: our share and the pool it was measured against,
        # both weighted by the seconds they were actually in force.
        # Once the exchange pays, realized share = paid / pool-seconds,
        # and computed-vs-realized share is readable per market with no
        # theory in between.
        self.share_s: dict[str, float] = {}   # share x seconds live
        self.pool_s: dict[str, float] = {}    # side pool $/day x seconds live
        self.live_s: dict[str, float] = {}    # seconds this market was billed
        self.last_ts: float | None = None
        self.history: list[dict] = []        # closed days
        # the owner's v1 graph: one dot per sample, raw, rolling across
        # day closes. Written ONLY by the independent sampler clock —
        # orders can never touch it.
        self.dots: list = []
        self.repairs: list[str] = []          # one-time corrections already applied
        self.adjustments: list[dict] = []     # today's corrections: {day, amount, why}

    # -- the one entry point -------------------------------------------------

    def sample(self, now: float, orders: list[dict], books: BookCache,
               terms: TermsStore, side_pool=None, verified_at: float | None = None) -> dict:
        """Score the resting book and advance the integral. `orders` is the
        normalized open-order list; `side_pool(slug, prog)` supplies the
        divisor-confirmed daily side pool (None = hold the estimate, the
        3.0 integrity rule — a market with an unconfirmed divisor accrues
        nothing rather than a guess)."""
        day = et_day(now)
        if self.day is None:
            self.day = day
        elif day != self.day:
            self._close_day()
            self.day = day

        # elapsed time since the previous sample, billed at the PREVIOUS
        # rate (the rate that was actually in force over that interval).
        # A gap longer than one sample may bill for is a blackout — the
        # exchange's maintenance, a stuck loop — and nothing was earned
        # in it (owner, 2026-09-11, the graph plateaued across a 100-min
        # maintenance: "Make the earning estimate during the period of
        # maintenance 0"): it bills nothing, banks as unmeasured time,
        # and the graph gets two zero dots so it reads as zero, not as
        # the last rate carried forward
        dt_s = 0.0
        if self.last_ts is not None:
            gap = max(now - self.last_ts, 0.0)
            if gap > MAX_GAP_S:
                self.stale_s += gap
                self.dots.append([round(self.last_ts + 20.0, 1), 0.0, 0])
                self.dots.append([round(now - 20.0, 1), 0.0, 0])
            else:
                dt_s = gap
        self.last_ts = now

        # the exchange out of reach: the orders in hand are unverified
        # for longer than one sample may bill for (`verified_at` is the
        # last time the open list was read back). Nothing is earned on
        # orders the exchange may have cancelled — its maintenance of
        # 2026-09-11 cancelled every one, the records stood, the books
        # read fresh, and $111 was billed across the outage (owner:
        # "the estimate of earnings of today still includes the period
        # of maintenance"). The interval banks as unmeasured, the graph
        # reads zero.
        if verified_at is not None and now - verified_at > MAX_GAP_S:
            if dt_s:
                self.stale_s += dt_s
            self.market_rates, self.market_shares, self.market_pools = {}, {}, {}
            self.rate = 0.0
            self.dots.append([round(now, 1), 0.0, 0])
            del self.dots[:-2880]
            self.samples += 1
            return self.snapshot(now)

        by_market: dict[str, list[dict]] = {}
        for o in orders:
            if o.get("market"):
                by_market.setdefault(o["market"], []).append(o)

        considered = [m for m in by_market if terms.get(m) is not None]
        fresh = [m for m in considered
                 if books.fresh(m, BOOK_MAX_AGE, now) is not None]
        fresh_set = set(fresh)
        # Per-market accrual (owner, 2026-08-21 evening: "CFB still says
        # it's only earned 5 cents today" — the old all-or-nothing
        # freshness quorum threw away whole minutes when most of a big
        # family was unwatched, refusing to count even the markets it
        # COULD see). Each market with a fresh book bills its own rate;
        # only the unwatched ones bank stale time. covered/stale are
        # coverage-weighted seconds, so the phone still shows how much
        # of the family the meter actually sees.
        frac = (len(fresh) / len(considered)) if considered else 1.0
        if dt_s:
            self._bill(now, fresh_set, dt_s=dt_s)
            self.covered_s += dt_s * frac
            self.stale_s += dt_s * (1.0 - frac)

        rates: dict[str, float] = {}
        shares: dict[str, float] = {}
        pools: dict[str, float] = {}
        for m in fresh:
            prog = terms.get(m)
            if not prog.is_live():
                continue
            book = top_up_book(books.fresh(m, BOOK_MAX_AGE, now), by_market[m])
            pool = side_pool(m, prog) if side_pool is not None else None
            if pool is None:
                continue          # divisor unconfirmed: no number, not a guess
            for o in by_market[m]:
                s = score_resting(o["side"], o["price"], o["size"], book,
                                  df=prog.df, target=prog.target,
                                  daily_side_pool=pool)
                if s.est_day:
                    rates[m] = rates.get(m, 0.0) + s.est_day
                    shares[m] = shares.get(m, 0.0) + (s.share or 0.0)
                    pools[m] = pool
        self.market_rates = rates
        self.market_shares = shares
        self.market_pools = pools
        self.rate = sum(rates.values())
        self.dots.append([round(now, 1), round(self.rate, 2), len(fresh)])
        del self.dots[:-2880]                # ~16h at the 20s clock
        self.samples += 1
        return self.snapshot(now)

    def _bill(self, now: float, fresh_set, dt_s: float | None = None) -> None:
        """Advance the integral over one interval, at the rates that were
        in force across it, for the markets whose books were fresh.

        The share and the pool are banked over the SAME interval and
        under the SAME freshness test as the money. That is what makes
        the later comparison honest: computed share and realized share
        are then measured over exactly the same seconds."""
        if dt_s is None:
            dt_s = min(max(now - (self.last_ts or now), 0.0), MAX_GAP_S)
            self.last_ts = now
        if dt_s <= 0:
            return
        for m, r in self.market_rates.items():
            if m not in fresh_set:
                continue
            self.earned += r * dt_s / 86400.0
            self.per_market[m] = (self.per_market.get(m, 0.0)
                                  + r * dt_s / 86400.0)
            self.share_s[m] = (self.share_s.get(m, 0.0)
                               + self.market_shares.get(m, 0.0) * dt_s)
            self.pool_s[m] = (self.pool_s.get(m, 0.0)
                              + self.market_pools.get(m, 0.0) * dt_s)
            self.live_s[m] = self.live_s.get(m, 0.0) + dt_s

    def repair_blackout(self, lo: float, hi: float, key: str, why: str) -> float:
        """One-time: take a blackout the meter billed back out of the day
        — what the graph shows earned between `lo` and `hi` comes off
        the day's total (the per-market split scaled with it), the graph
        reads zero across it, and the span banks as unmeasured. Applied
        once per `key`; the correction is recorded for the page."""
        if key in self.repairs:
            return 0.0
        self.repairs.append(key)
        billed = 0.0
        for a, b in zip(self.dots, self.dots[1:]):
            t0, t1 = float(a[0]), float(b[0])
            if t1 <= lo or t0 >= hi or t1 - t0 > MAX_GAP_S:
                continue
            billed += float(a[1]) * (min(t1, hi) - max(t0, lo)) / 86400.0
        billed = min(billed, self.earned)
        if billed < 0.005:                       # nothing worth a line on the page
            return 0.0
        scale = (self.earned - billed) / self.earned if self.earned > 0 else 0.0
        self.earned -= billed
        self.per_market = {m: v * scale for m, v in self.per_market.items()}
        for d in self.dots:
            if lo <= float(d[0]) <= hi:
                d[1], d[2] = 0.0, 0
        span = hi - lo
        self.covered_s = max(self.covered_s - span, 0.0)
        self.stale_s += span
        self.adjustments.append({"day": self.day, "amount": round(billed, 2), "why": why})
        return billed

    def calibration(self) -> dict[str, dict]:
        """Per market: the share we computed, time-weighted, and the pool
        it was measured against. Paired with what the exchange actually
        paid, `paid / pool_day_seconds` is the REALIZED share, and the
        ratio of the two is the estimator's bias with no model in the
        middle."""
        out = {}
        for m, secs in self.live_s.items():
            if secs <= 0:
                continue
            out[m] = {
                "share": round(self.share_s.get(m, 0.0) / secs, 6),
                "pool_day": round(self.pool_s.get(m, 0.0) / secs, 6),
                "live_h": round(secs / 3600.0, 3),
                # what the pool actually offered over the hours we were live
                "pool_live": round(self.pool_s.get(m, 0.0) / 86400.0, 6),
            }
        return out

    # -- bookkeeping ------------------------------------------------------------

    def _close_day(self) -> None:
        self.history.append({
            "day": self.day, "earned": round(self.earned, 4),
            "samples": self.samples, "covered_s": round(self.covered_s, 1),
            "stale_s": round(self.stale_s, 1),
            "per_market": {m: round(v, 4) for m, v in sorted(
                self.per_market.items(), key=lambda kv: -kv[1])[:50]},
            # the day's share/pool measurement, kept so a payout that
            # lands five days later can still be graded against what we
            # actually computed while the day was running
            "calibration": self.calibration(),
            "adjustments": list(self.adjustments),
        })
        self.adjustments = []
        del self.history[:-HISTORY_DAYS]
        self.earned = 0.0
        self.per_market = {}
        self.samples = 0
        self.covered_s = 0.0
        self.stale_s = 0.0
        self.share_s = {}
        self.pool_s = {}
        self.live_s = {}

    def snapshot(self, now: float) -> dict:
        return {
            "day": self.day, "earned": round(self.earned, 4),
            "rate": round(self.rate, 4),
            "market_rates": {m: round(v, 4) for m, v in self.market_rates.items()},
            "per_market": {m: round(v, 4) for m, v in self.per_market.items()},
            "samples": self.samples, "covered_s": round(self.covered_s, 1),
            "stale_s": round(self.stale_s, 1), "ts": now,
            "adjustments": list(self.adjustments),
        }

    # -- persistence --------------------------------------------------------------

    def to_dict(self) -> dict:
        d = self.snapshot(self.last_ts or 0.0)
        d["history"] = self.history
        d["last_ts"] = self.last_ts
        d["dots"] = self.dots
        # the day's share measurement must survive a restart, or a
        # deploy mid-afternoon silently resets the sample the whole
        # calibration depends on
        d["share_s"] = {m: round(v, 4) for m, v in self.share_s.items()}
        d["pool_s"] = {m: round(v, 4) for m, v in self.pool_s.items()}
        d["live_s"] = {m: round(v, 1) for m, v in self.live_s.items()}
        d["repairs"] = list(self.repairs)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Estimator":
        e = cls()
        e.day = d.get("day")
        e.earned = d.get("earned") or 0.0
        e.per_market = dict(d.get("per_market") or {})
        e.rate = d.get("rate") or 0.0
        e.market_rates = dict(d.get("market_rates") or {})
        e.samples = d.get("samples") or 0
        e.covered_s = d.get("covered_s") or 0.0
        e.stale_s = d.get("stale_s") or 0.0
        e.last_ts = d.get("last_ts")
        e.history = list(d.get("history") or [])
        e.dots = list(d.get("dots") or [])
        e.share_s = dict(d.get("share_s") or {})
        e.pool_s = dict(d.get("pool_s") or {})
        e.live_s = dict(d.get("live_s") or {})
        e.repairs = list(d.get("repairs") or [])
        e.adjustments = list(d.get("adjustments") or [])
        return e
