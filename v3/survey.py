"""Read-only survey: where could a cent of risk hold a real share?

The thing that makes college football work is not a fat reward pool. It
is that a qualifying wall parked up at 99c carries the side over Target
Size while the touch itself holds single digits of shares — so an order
of FORTY-ONE CENTS takes 13% of the side's score. NBA's pools are 2.3x
richer and pay 17x less, because its touch holds hundreds of thousands
of contracts and our share is 0.02%.

So the question this asks of a market is never "how big is the pool".
It is: how much rests near the touch, and what would our own small size
be worth against it (owner, 2026-08-31: "We're getting a high share with
low order size (risk). So that wouldn't quite be an apples to apples
comparison").

Pure arithmetic on a book and a program row — no IO, no orders.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field

from .scoring import estimate_join

# Owner, 2026-08-31: "we'll probably want to stay out of live events
# until I have a way of quoting them better." A market whose game has
# started prices off play, not off a line — and the buffer keeps us out
# of the hour before the whistle too, when the book turns over as the
# lines firm up. Re-checked on every pass, because a market that was
# quiet this morning goes live at kickoff.
LIVE_BUFFER_S = 3600.0


def _epoch(v) -> float | None:
    """The exchange sends times as ISO strings; some feeds send epochs."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f / 1000.0 if f > 1e11 else f     # milliseconds or seconds
    try:
        s = str(v).replace("Z", "+00:00")
        return dt.datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return None


def is_live_event(market: dict, now: float,
                  buffer_s: float = LIVE_BUFFER_S) -> bool:
    """Is this market's event under way, or about to be?

    Reads eventStartTime, which the incentives listing carries on every
    row, or gameStartTime from a market object — so the check costs no
    extra call. A market with neither has no in-play phase: a futures or
    politics market trades the same way all day, and is never live.
    """
    if not isinstance(market, dict):
        return False
    start = _epoch(market.get("eventStartTime")
                   or market.get("gameStartTime"))
    if start is None:
        return False
    return now >= start - buffer_s


# The standing rule, and the reason a blind sampler needs one: it will
# wander into everything the exchange lists (owner's scope rule, and
# 2026-08-31: "at 67,569 markets a blind sampler will wander into
# everything"). Matched against the exchange's own category and
# subcategory rather than guessed from slugs.
BANNED_CATEGORIES = ("econ", "economics", "economy", "macro", "inflation",
                     "fed", "rates", "cpi")


def category_banned(row: dict) -> bool:
    for k in ("category", "subcategory"):
        v = str(row.get(k) or "").strip().lower()
        if v and any(b in v for b in BANNED_CATEGORIES):
            return True
    return False


def group_of(row: dict) -> str:
    """What to call a market's KIND on the leaderboard.

    The exchange labels every incentives row with a category, a
    subcategory and a product — "sports / basketball / moneyline" — which
    beats the slug-prefix guess and is the discriminator for the weekly
    game markets the prefix heuristic could never separate from futures
    (owner, 2026-08-31). Falls back to the prefix when a row carries no
    labels.
    """
    parts = [str(row.get(k) or "").strip().lower()
             for k in ("category", "subcategory", "instrumentProduct")]
    parts = [p for p in parts if p]
    if parts:
        return "/".join(parts)
    return slug_group(str(row.get("marketSlug") or ""))


# The incentives row, slimmed to what the survey reads (owner, 2026-09-02,
# after the memory graph: a raw row is ~4.5 KB of Python, and 28,081 of
# them held all day — twice over while refetching — were the biggest
# thing in the 1 GB box). group_of, category_banned, is_live_event and
# pick_period/program_from_period read exactly these and nothing else.
from sys import intern as _intern  # noqa: E402 — belongs with its use

ROW_KEEP = ("marketSlug", "category", "subcategory", "instrumentProduct",
            "instrumentState", "eventStartTime", "gameStartTime")
PERIOD_KEEP = ("programId", "status", "rewardPool", "targetSize",
               "discountFactor", "start", "end", "period")
# label strings repeat across thousands of rows ("sports", "LIVE", one
# programId per family): interned, every row points at one copy
_SHARED = ("category", "subcategory", "instrumentProduct",
           "instrumentState", "programId", "status", "period")
_NUMERIC = ("rewardPool", "targetSize", "discountFactor")


def _slim(src: dict, keep: tuple) -> dict:
    out: dict = {}
    for k in keep:
        v = src.get(k)
        if v is None or v == "":
            continue
        if k in _NUMERIC:
            try:
                v = float(v)
            except (TypeError, ValueError):
                pass
        elif k in _SHARED and isinstance(v, str):
            v = _intern(v)
        out[k] = v
    return out


def compact_row(row: dict) -> dict:
    """The seven row fields and eight period fields the survey uses;
    empty values dropped, labels shared, pool figures as floats — a
    slim row costs a slim row."""
    out = _slim(row, ROW_KEEP)
    tps = row.get("timePeriods")
    if isinstance(tps, list):
        out["timePeriods"] = [_slim(tp, PERIOD_KEEP)
                              for tp in tps if isinstance(tp, dict)]
    return out

# how far from the touch still carries real scoring weight. Past this the
# discount factor has usually crushed a level's contribution to nothing.
NEAR_TICKS = 3

# the order we would actually place. cfb's real orders are a fraction of
# a share to two shares, median 41c at risk — so that, not an invented
# round number, is what the survey prices (owner, 2026-08-31).
PROBE_QTY = 1.0


def kind_of(slug: str) -> str:
    """The market KIND, so a survey can report what sorts of market a
    tag actually holds. Slugs run <kind>-<date>-<who>, e.g.
    aachc-cfb-wins-2026-11-28-txst-5pt5wins, so everything before the
    first four-digit year is the kind."""
    parts = str(slug).split("-")
    out: list[str] = []
    for p in parts:
        if len(p) == 4 and p.isdigit() and 2000 <= int(p) <= 2100:
            break
        out.append(p)
    return "-".join(out) or str(slug)


# A slug like astatc-cfb-akron-wake names one FIXTURE, not a kind of
# market — Akron vs Wake Forest, 67 markets of its own. Left at that
# grain the frame produces a stratum per game, each waiting days for a
# turn, and the board never ranks anything (owner, 2026-08-31: "just
# try and keep it going so I can get some data"). Two segments is the
# market type: astatc-cfb, aachc-cfb, tec-nba, vmc-ussep.
SLUG_GROUP_SEGMENTS = 2


def slug_group(slug: str) -> str:
    """The market TYPE from a slug, coarse enough to accumulate."""
    return "-".join(kind_of(slug).split("-")[:SLUG_GROUP_SEGMENTS])


# Owner, 2026-09-01: "make it so my orders buy 125% of the target
# size". A side resting exactly AT Target Size flips on and off as
# other people's orders come and go, and BELOW the line the whole side
# pays nobody — so being one share short costs the entire day's reward
# for everyone on it, not a proportional slice. A quarter over the line
# is headroom against that.
QUALIFY_TARGET_MULT = 1.25


def wall_price(bs: str, tick: float) -> float:
    """Where a qualifying wall rests: the last tick inside the price
    bounds on its side — 99.9c for an ask wall (99c on a whole-cent
    book), 0.1c for a bid wall (1c). It carries the side over Target
    Size from where it will never trade."""
    import math
    tick = tick or 0.01
    if bs == "SELL":
        return round(math.floor(0.999 / tick + 1e-9) * tick, 3)
    return round(math.ceil(0.001 / tick - 1e-9) * tick, 3)


def wall_collateral(bs: str, px: float, qty: float) -> float:
    """Buying power a wall of `qty` at `px` holds: a short ask holds
    (1 - price) a share, a bid holds the price."""
    return qty * ((1.0 - px) if bs == "SELL" else px)


# A stratum needs MIN_SAMPLES scored sides to rank, which is half that
# many markets. One holding fewer can NEVER rank however long it runs,
# so it is merged into its parent rather than left collecting draws it
# can do nothing with: cul/awd/tac -> cul/awd -> cul.
MIN_MARKETS = 6


def parent_of(group: str) -> str | None:
    """One level coarser, or None at the top."""
    sep = "/" if "/" in group else "-"
    parts = [p for p in group.split(sep) if p]
    if len(parts) <= 1:
        return None
    return sep.join(parts[:-1])


@dataclass
class SideProbe:
    """What one side of one book would be worth to us."""

    side: str
    touch_px: float | None = None
    touch_size: float = 0.0        # shares resting AT the touch
    near_size: float = 0.0         # within NEAR_TICKS of it
    total_size: float = 0.0        # the whole side, walls included
    target: float = 0.0
    qualifies: bool = False        # cumulative size reaches Target Size
    share: float = 0.0             # what PROBE_QTY at the touch would score
    est_day: float = 0.0           # that share x the side's daily pool
    risk_usd: float = 0.0          # what those shares would cost us
    note: str = ""

    @property
    def share_per_dollar(self) -> float:
        """Share of a side bought per dollar put at risk. Kept as a
        secondary column: it says how cheaply we can own a side, but it
        is blind to whether that side pays anything."""
        return (self.share / self.risk_usd) if self.risk_usd > 1e-9 else 0.0

    @property
    def yield_per_dollar(self) -> float:
        """Dollars earned a day per dollar at risk — what we are
        actually buying.

        Share alone missed the pool and pool alone missed the share;
        this is their product over the risk, and it is directly
        comparable across categories priced differently. College
        football, the one that works, runs a median of 0.16 (owner,
        2026-08-31: "Number 1 was the concern I had").
        """
        return (self.est_day / self.risk_usd) if self.risk_usd > 1e-9 else 0.0

    def row(self, slug: str, kind: str) -> dict:
        return {"market": slug, "kind": kind, "side": self.side,
                "touch_px": round(self.touch_px, 4) if self.touch_px else "",
                "touch_size": round(self.touch_size, 2),
                "near_size": round(self.near_size, 2),
                "total_size": round(self.total_size, 2),
                "target": round(self.target, 0),
                "qualifies": int(self.qualifies),
                "share_pct": round(self.share * 100.0, 4),
                "est_day_usd": round(self.est_day, 4),
                "risk_usd": round(self.risk_usd, 4),
                "share_per_dollar": round(self.share_per_dollar, 4),
                "yield_per_dollar": round(self.yield_per_dollar, 4),
                "note": self.note}


def probe_side(book, prog, side: str, side_pool: float | None,
               qty: float = PROBE_QTY) -> SideProbe:
    """Score one side of one market for a PROBE_QTY-share join at the
    touch. `prog` is a programs.Program; `side_pool` is what that side
    competes for per day, already divided by the markets in the event
    and by the two sides."""
    p = SideProbe(side=side, target=float(getattr(prog, "target", 0.0) or 0.0))
    levels = [(px, q) for px, q in book.side(side) if q > 1e-9]
    if not levels:
        p.note = "side is empty"
        return p
    p.touch_px = levels[0][0]
    p.touch_size = levels[0][1]
    p.total_size = sum(q for _px, q in levels)
    tick = book.tick or 0.01
    near_lo = p.touch_px - NEAR_TICKS * tick
    near_hi = p.touch_px + NEAR_TICKS * tick
    p.near_size = sum(q for px, q in levels if near_lo - 1e-9 <= px <= near_hi + 1e-9)
    j = estimate_join(side, levels, tick, float(getattr(prog, "df", 0.0) or 0.0),
                      p.target, p.touch_px, qty)
    p.qualifies = bool(j.qualifies)
    p.share = float(j.share) if (j.qualifies and j.in_window) else 0.0
    if side_pool is not None:
        p.est_day = p.share * side_pool
    # what the shares would cost: a bid pays its price, an ask offers
    # stock we would have to hold, valued the same way
    p.risk_usd = qty * p.touch_px
    if not p.qualifies:
        p.note = "side under Target Size — pays nobody"
    elif p.share <= 0.0:
        p.note = "outside the scoring window"
    return p


def summarise(rows: list[dict]) -> dict:
    """A few numbers per kind, so a tag's market types can be compared
    without reading hundreds of rows."""
    by: dict[str, dict] = {}
    for r in rows:
        k = by.setdefault(r["kind"], {"kind": r["kind"], "sides": 0,
                                      "qualified": 0, "shares": [],
                                      "est": 0.0, "spd": []})
        k["sides"] += 1
        k["qualified"] += int(r["qualifies"])
        if r["qualifies"]:
            k["shares"].append(r["share_pct"])
            k["est"] += r["est_day_usd"]
            k["spd"].append(r["share_per_dollar"])
    out = []
    for k in by.values():
        sh = sorted(k["shares"])
        spd = sorted(k["spd"])
        out.append({
            "kind": k["kind"], "sides": k["sides"],
            "qualified": k["qualified"],
            "median_share_pct": round(sh[len(sh) // 2], 3) if sh else 0.0,
            "best_share_pct": round(sh[-1], 3) if sh else 0.0,
            "est_day_usd": round(k["est"], 2),
            "median_share_per_dollar":
                round(spd[len(spd) // 2], 3) if spd else 0.0,
        })
    out.sort(key=lambda r: -r["median_share_per_dollar"])
    return {"kinds": out}


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


@dataclass
class PrefixStat:
    """A prefix's running evidence. Recent samples only — books change,
    and a prefix that was juicy last month must not sit at the top for
    ever. The window IS the decay."""

    prefix: str
    markets: int = 0            # distinct markets drawn
    sides: int = 0              # sides scored
    qualified: int = 0
    live_skipped: int = 0
    ypd: list = field(default_factory=list)   # $/day per $1 at risk
    spd: list = field(default_factory=list)
    share: list = field(default_factory=list)
    touch: list = field(default_factory=list)
    est: list = field(default_factory=list)
    best: list = field(default_factory=list)   # the markets worth looking at
    last_ts: float = 0.0

    KEEP = 80
    KEEP_BEST = 5                   # samples per prefix before the oldest fall out

    def record(self, p: "SideProbe", now: float, slug: str = "") -> None:
        """One scored side. `slug` is kept for the best few so the board
        can name a market to go and look at — a leaderboard of market
        kinds is no use if you cannot get from it to an order (owner,
        2026-08-31: "Can you give the slugs of the top programs")."""
        self.sides += 1
        self.last_ts = now
        if not p.qualifies:
            return
        self.qualified += 1
        for lst, v in ((self.ypd, p.yield_per_dollar),
                       (self.spd, p.share_per_dollar),
                       (self.share, p.share * 100.0),
                       (self.touch, p.touch_size),
                       (self.est, p.est_day)):
            lst.append(v)
            del lst[:-self.KEEP]
        if slug:
            self.best = [b for b in self.best if b["market"] != slug]
            self.best.append({
                "market": slug, "side": p.side,
                "ypd": round(p.yield_per_dollar, 4),
                "share_pct": round(p.share * 100.0, 3),
                "touch": round(p.touch_size, 1),
                "px": round(p.touch_px or 0.0, 4),
                "est_day": round(p.est_day, 4), "ts": round(now, 1)})
            self.best.sort(key=lambda b: -b["ypd"])
            del self.best[self.KEEP_BEST:]

    def row(self) -> dict:
        return {"prefix": self.prefix, "markets": self.markets,
                "sides": self.sides, "qualified": self.qualified,
                "live_skipped": self.live_skipped,
                "n": len(self.ypd),
                "median_ypd": round(_median(self.ypd), 4),
                "median_spd": round(_median(self.spd), 3),
                "median_share_pct": round(_median(self.share), 3),
                "median_touch": round(_median(self.touch), 0),
                "median_est_day": round(_median(self.est), 4),
                "best": list(self.best),
                "last_ts": round(self.last_ts, 1)}


# Below this many scored sides a prefix's median is noise, so it is not
# ranked at all — it is listed as still sampling. The AFC South market
# (one share at the touch, 14% share) is exactly the single lucky draw
# that would otherwise crown a prefix (owner, 2026-08-31).
MIN_SAMPLES = 12


def leaderboard(stats: dict, min_samples: int = MIN_SAMPLES) -> dict:
    """Ranked on the MEDIAN dollars a day per dollar at risk — never
    the max, and never before there is enough evidence to mean anything.

    Ranking on share per dollar was the earlier mistake: it says how
    cheaply a side can be owned while saying nothing about whether the
    side pays. MLB sits within a whisker of college football on yield
    and nowhere near it on share (owner, 2026-08-31).
    """
    rows = [s.row() for s in stats.values()]
    ranked = [r for r in rows if r["n"] >= min_samples]
    young = [r for r in rows if r["n"] < min_samples]
    ranked.sort(key=lambda r: -r["median_ypd"])
    young.sort(key=lambda r: -r["n"])
    return {"ranked": ranked, "sampling": young,
            "min_samples": min_samples}


class Sampler:
    """Stratified round-robin over prefixes, uniformly random within
    each one.

    Uniform over MARKETS would be the obvious choice and the wrong one:
    a prefix holding 8,000 markets would swamp the sample while one
    holding 20 went years without a draw, which is the opposite of a
    prefix leaderboard. So every prefix takes its turn, and which of its
    markets represents it is a random draw without replacement until the
    prefix is exhausted, then reshuffled.

    The seed is kept and reported so any run can be reproduced and
    audited — no hand-picking (owner, 2026-08-31: "so that we can
    guarantee that you'd being random with how you are sampling").
    """

    def __init__(self, seed: int = 0):
        self.seed = int(seed)
        self.rng = random.Random(self.seed)
        self.pools: dict[str, list[str]] = {}
        self.all: dict[str, list[str]] = {}
        self.order: list[str] = []
        self.cursor = 0
        self.passes = 0

    def load(self, items) -> None:
        """Group the population into strata. Takes plain slugs, or
        (slug, group) pairs so the exchange's own category labels can be
        the stratum instead of a slug prefix. Adding markets later keeps
        pools that are part-drawn, so a refresh does not restart the
        sampling."""
        for it in items:
            if isinstance(it, (tuple, list)) and len(it) == 2:
                s, k = str(it[0]), str(it[1])
            else:
                s, k = str(it), kind_of(str(it))
            bucket = self.all.setdefault(k, [])
            if s not in bucket:
                bucket.append(s)
        self.merged = self._merge_small()
        for k, v in self.all.items():
            if k not in self.pools:
                self.pools[k] = self._shuffled(v)
        for gone in [k for k in self.pools if k not in self.all]:
            self.pools.pop(gone, None)
        self.order = sorted(self.all)

    def _merge_small(self, min_markets: int = MIN_MARKETS) -> int:
        """Fold strata too small to ever rank into their parent.

        A stratum needs MIN_SAMPLES scored sides, so a stratum holding
        three markets can never get there however long it runs — it just
        keeps taking turns in the round robin and doing nothing with
        them. Merging up a level, repeatedly, gives it a chance instead
        (owner, 2026-08-31)."""
        merged = 0
        while True:
            small = [k for k, v in self.all.items()
                     if len(v) < min_markets and parent_of(k)]
            if not small:
                return merged
            moved = False
            for k in small:
                par = parent_of(k)
                if par is None or par == k:
                    continue
                bucket = self.all.setdefault(par, [])
                for slug in self.all.pop(k):
                    if slug not in bucket:
                        bucket.append(slug)
                self.pools.pop(k, None)
                self.pools.pop(par, None)      # reshuffle the grown parent
                merged += 1
                moved = True
            if not moved:
                return merged

    def _shuffled(self, slugs: list[str]) -> list[str]:
        out = list(slugs)
        self.rng.shuffle(out)
        return out

    def next_batch(self, k: int) -> list[str]:
        """The next k markets to probe: one prefix at a time, round
        robin, so every prefix gains evidence at the same rate."""
        out: list[str] = []
        if not self.order:
            return out
        for _ in range(k * max(len(self.order), 1)):
            if len(out) >= k:
                break
            pref = self.order[self.cursor % len(self.order)]
            self.cursor += 1
            pool = self.pools.get(pref) or []
            if not pool:                      # exhausted — reshuffle it
                pool = self.pools[pref] = self._shuffled(self.all.get(pref, []))
                self.passes += 1
                if not pool:
                    continue
            out.append(pool.pop())
        return out

    def state(self) -> dict:
        sizes = sorted((len(v) for v in self.all.values()), reverse=True)
        return {"seed": self.seed, "prefixes": len(self.all),
                "merged": getattr(self, "merged", 0),
                "biggest": sizes[0] if sizes else 0,
                "too_small": sum(1 for n in sizes if n < MIN_MARKETS),
                "population": sum(len(v) for v in self.all.values()),
                "left_this_pass": sum(len(v) for v in self.pools.values()),
                "passes": self.passes}


CSV_COLUMNS = ("market", "kind", "side", "touch_px", "touch_size",
               "near_size", "total_size", "target", "qualifies",
               "share_pct", "est_day_usd", "risk_usd", "share_per_dollar",
               "note")


def to_csv(rows: list[dict]) -> str:
    lines = [",".join(CSV_COLUMNS)]
    for r in rows:
        lines.append(",".join(str(r.get(c, "")).replace(",", " ")
                              for c in CSV_COLUMNS))
    return "\n".join(lines) + "\n"
