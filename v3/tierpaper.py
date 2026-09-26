"""Stage 3 of the tier engines: paper trading (owner, 2026-09-26: "We
need to go back to building the bigger 4 tier earning machine" — the
design is v3/TIERS.md, stage 3).

READ-ONLY. Nothing here places, moves or cancels a real order, and
nothing here reads the exchange. Each tier has an engine that decides,
every second (Tier 4 every ten), what it WOULD rest, keeps those paper
orders, fills them off the real tape, holds the paper positions, rests
one exit per position, and keeps its paper books: the reward its orders
would have claimed, the capital they tied up, and what the fills made or
lost marked to the midpoint.

How it decides:
- Money: the $1,000 pot is split across the tiers once each ET day at
  midnight (and at the first plan after a boot), each tier's share its
  expected value alone (stage 2) over the sum. A tier spends its share
  less what its paper positions hold.
- Values come from stage 2's arithmetic (tiervalue.Side: reward share,
  fills a day x shares x the loss a share, capital), but on a
  TIME-AVERAGED book: others' size at each price is averaged over
  AVG_HALF_S, so a level that appears and vanishes counts for the part
  of the time it is there — both as company and as competition for the
  reward. The live book decides what can rest where (never at or
  through the other side) and when a paper order fills.
- The tier's money is spread across its markets every ALLOC_S by stage
  2's greedy; between spreads, a side whose book moved is re-planned on
  its own share of the money.
- A side changes its orders only when the new set beats the old by more
  than moving costs: the gain a day, over how long a side's set has
  measured to stay best (HOLD_PRIOR_S until measured), against the
  actions it takes at the action's price. Actions are rationed by
  ACTIONS_PER_MIN a tier (the exchange limits how fast we may place and
  cancel); when the budget binds, the best move it had to skip sets the
  price of an action for the next minute. No fixed minutes or dollar
  gates beyond that.
- One exit per paper position, sized to what is held, at the price with
  the best reward plus gain on its fill against the fair; exits go first
  in the action budget.

The check on the paper fills (owner, 2026-09-26: "How will you know how
well it is estimating when everything is read only"): every real order of
the tender's gets a paper twin at the same price and size, filled by the
same tape rule; when the real order leaves the book (or has rested
SHADOW_H_S), the twin is scored against what really happened — both
filled, paper only, real only, neither."""

from __future__ import annotations

import json
import math
import time
from collections import deque

from .terms import et_day_start
from .tierfair import MID_TRUST_SPREAD, SLOW_TIERS, TIERS
from .tiervalue import (DAY_S, MARKET_CAP_FRAC, POT_USD, SLICE_USD, Side, candidates,
                        run_greedy, side_share, tape_fill)

TICK_FAST_S = 1.0             # Tiers 1-3 decide this often
TICK_SLOW_S = 10.0            # Tier 4 this often
ALLOC_FAST_S = 10.0           # a tier's money is spread across its markets this often
ALLOC_SLOW_S = 60.0
AVG_HALF_S = 300.0            # the time-averaged book's half-life
BOOK_MAX_S = 120.0            # no decision and no fill on a book older than this
MAX_GAP_S = 300.0             # nothing accrues across a gap longer than this
OVERLAP_S = 3.0               # a move rests the new order this long before the old comes off
ACTIONS_PER_MIN = 30          # a tier's place-and-cancel budget, a rolling minute
ACTION_FLOOR = 0.002          # an action's price at the least ($)
HOLD_PRIOR_S = 600.0          # how long a side's new set stays best, until measured
HOLD_ALPHA = 0.2
SAME_QTY = 0.02               # a set within 2% of the shares at the same prices is the same set
EXIT_MIN_QTY = 0.01
REPLAN_MAX = 12               # sides re-planned between spreads in one look, the stalest first
SHADOW_H_S = 6 * 3600.0       # a real order still resting this long is scored as it stands
SHADOW_GRACE_S = 600.0        # a real order gone is scored this long after, when its fill is booked
BOOT_SPLIT_S = 900.0          # after a boot the split is refined this long, once a minute
SPLIT_EVERY_S = 60.0
LOG_KEEP = 400
DAYS_KEEP = 14
VERSION = "paper-2026-09-26"


def _day(now: float) -> str:
    import datetime as _dt
    return _dt.datetime.utcfromtimestamp(et_day_start(now) + 12 * 3600).strftime("%Y-%m-%d")


class AvgBooks:
    """Others' size at each price, averaged over time: a level that is
    there a third of the time counts a third. Kept to the live book's
    range — nothing better than the live touch, which is gone."""

    def __init__(self):
        self.b: dict[str, dict] = {}

    def update(self, slug: str, bids, asks, now: float) -> None:
        e = self.b.get(slug)
        if e is None:
            self.b[slug] = {"BUY": {round(float(p), 4): float(q) for p, q in bids},
                            "SELL": {round(float(p), 4): float(q) for p, q in asks},
                            "at": now}
            return
        k = 0.5 ** (max(now - e["at"], 0.0) / AVG_HALF_S)
        for side, lv in (("BUY", bids), ("SELL", asks)):
            d = e[side]
            cur = {round(float(p), 4): float(q) for p, q in lv}
            for p in set(d) | set(cur):
                v = d.get(p, 0.0) * k + cur.get(p, 0.0) * (1.0 - k)
                if v < 0.5:
                    d.pop(p, None)
                else:
                    d[p] = v
        e["at"] = now

    def levels(self, slug: str, side: str, live) -> list:
        """The averaged levels of a side, best first, none better than the
        live touch; the live book where nothing is averaged yet."""
        e = self.b.get(slug)
        if e is None:
            return [(float(p), float(q)) for p, q in live]
        d = e[side]
        buy = side == "BUY"
        out = sorted(d.items(), reverse=buy)
        if live:
            t = float(live[0][0])
            if buy:
                out = [(p, q) for p, q in out if p <= t + 1e-9]
            else:
                out = [(p, q) for p, q in out if p >= t - 1e-9]
        return out

    def drop(self, keep: set) -> None:
        for s in [s for s in self.b if s not in keep]:
            del self.b[s]


class PaperTier:
    """One tier's paper engine: its money, orders, positions and books."""

    def __init__(self, key: str):
        self.key = key
        self.money = 0.0
        self.orders: dict[str, dict] = {}
        self.pos: dict[str, list] = {}          # slug -> [shares (+ long / - short), avg price]
        self.alloc: dict[tuple, float] = {}      # (slug, side) -> money the spread gave it
        self.seen: dict[str, float] = {}         # slug -> the book's stamp last decided on
        self.hold: dict[tuple, float] = {}       # (slug, side) -> measured seconds a set stays best
        self.changed: dict[tuple, float] = {}    # (slug, side) -> when its set last changed
        self.acts: deque = deque()
        self.act_price = ACTION_FLOOR
        self.skipped_best = 0.0
        self.waiting_money = 0
        self.last = 0.0
        self.last_alloc = 0.0
        self.n = 0
        self.log: deque = deque(maxlen=LOG_KEEP)
        self.day = self._new_day("", 0.0)
        self.days: list[dict] = []

    @staticmethod
    def _new_day(day: str, unreal: float) -> dict:
        return {"day": day, "reward": 0.0, "capital": 0.0, "realized": 0.0, "fills": 0,
                "actions": 0, "moves": 0, "skipped": 0, "pred": 0.0,
                "unreal0": unreal,
                "secs": 0.0}

    def oid(self) -> str:
        self.n += 1
        return f"P{self.key}-{self.n}"

    def collateral_held(self) -> float:
        c = 0.0
        for q, avg in self.pos.values():
            c += q * avg if q > 0 else -q * (1.0 - avg)
        return c

    def to_dict(self) -> dict:
        return {"money": round(self.money, 2), "n": self.n,
                "orders": self.orders,
                "pos": {s: [round(q, 4), round(a, 6)] for s, (q, a) in self.pos.items()},
                "hold": {f"{s}|{sd}": round(h, 1) for (s, sd), h in self.hold.items()},
                "day": self.day, "days": self.days[-DAYS_KEEP:],
                "log": list(self.log)[-100:]}

    def restore(self, d: dict) -> None:
        self.money = float(d.get("money") or 0.0)
        self.n = int(d.get("n") or 0)
        self.orders = {k: dict(v) for k, v in (d.get("orders") or {}).items()}
        self.pos = {s: [float(v[0]), float(v[1])] for s, v in (d.get("pos") or {}).items()}
        for k, h in (d.get("hold") or {}).items():
            try:
                s, sd = k.rsplit("|", 1)
                self.hold[(s, sd)] = float(h)
            except (ValueError, TypeError):
                continue
        if isinstance(d.get("day"), dict):
            self.day = dict(self._new_day("", 0.0), **d["day"])
        self.days = list(d.get("days") or [])[-DAYS_KEEP:]
        self.log.extend(d.get("log") or [])


class TierPaper:
    """`tv` is stage 2 (its books, hazards, losses and plans), `tf` the tier
    fairs, `fam` the politics family (the real orders and their fills),
    `tender_ids()` the tender's order ids."""

    def __init__(self, tv, tf, fam, tender_ids=None, clock=None):
        self.tv, self.tf, self.fam = tv, tf, fam
        self.tender_ids = tender_ids or (lambda: set())
        self._clock = clock or time.time
        self.tiers = {k: PaperTier(k) for k, _n, _p in TIERS}
        self.avg = AvgBooks()
        self.shares: dict[str, float] = {}
        self.split_day = ""
        self.split_at = 0.0
        self.split_why = ""
        self._boot_at = self._clock()
        self.shadows: dict[str, dict] = {}
        self.shadow_tally: dict[str, dict] = {}
        self.shadow_recent: deque = deque(maxlen=60)
        self.error = ""
        self.tick_s = 0.0

    # -- the money --------------------------------------------------------------

    def _split(self, now: float) -> None:
        """Each tier's share of the pot: its value alone over the sum, set at
        midnight ET (and at the first plan after a boot). A plan where every
        tier reads nothing is no plan to split on (2026-09-26 18:45Z: the
        first plan after the boot ran before the books had arrived, every
        tier read $0, and every tier got $0 until midnight); after a boot
        the split is refined once a minute for BOOT_SPLIT_S, as the books
        fill in."""
        day = _day(now)
        split = bool(self.shares) and sum(self.shares.values()) > 0
        if split and self.split_day == day:
            if not (self.split_why == "the first plan after a boot"
                    and now - self._boot_at <= BOOT_SPLIT_S
                    and now - self.split_at >= SPLIT_EVERY_S):
                return
        v = self.tv.view() if hasattr(self.tv, "view") else {}
        if not v.get("ok"):
            return
        alone = {t: max(float(((v.get("tiers") or {}).get(t) or {}).get("alone", {})
                              .get("value") or 0.0), 0.0) for t, _n, _p in TIERS}
        tot = sum(alone.values())
        if tot <= 0:
            return                    # nothing planned with value yet: wait for the books
        new = {t: alone[t] / tot for t in alone}
        if split and self.split_day == day and all(
                abs(new[t] - self.shares.get(t, 0.0)) < 0.01 for t in new):
            self.split_at = now
            return                    # the refinement moved nothing worth a line
        self.shares = new
        why = ("midnight ET" if self.split_day and self.split_day != day
               else "the first plan after a boot")
        self.split_day, self.split_at, self.split_why = day, now, why
        for t, pt in self.tiers.items():
            pt.money = round(POT_USD * self.shares.get(t, 0.0), 2)
            pt.log.append({"ts": round(now, 1), "ev": "money",
                           "note": f"{self.shares.get(t, 0.0) * 100:.1f}% of the pot = "
                                   f"${pt.money:.2f} ({why})"})

    # -- the clock ----------------------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        t0 = time.time()
        self._split(now)
        if not self.shares:
            return
        mine_all = self.tv._ours()
        mk = self.tv._mk()
        by_tier: dict[str, list] = {}
        for slug, (tier, prog) in mk.items():
            by_tier.setdefault(tier, []).append((slug, prog))
        for t, pt in self.tiers.items():
            slow = t in SLOW_TIERS
            if now - pt.last < (TICK_SLOW_S if slow else TICK_FAST_S) - 1e-6:
                continue
            try:
                self._run(pt, by_tier.get(t, []), now, mine_all)
            except Exception as e:  # noqa: BLE001 — one tier never stops the others
                self.error = f"{t}: {type(e).__name__}: {e}"[:160]
        try:
            self._shadows(now, mine_all)
        except Exception as e:  # noqa: BLE001
            self.error = f"shadows: {type(e).__name__}: {e}"[:160]
        self.avg.drop(set(mk))
        self.tick_s = round(time.time() - t0, 4)

    def _run(self, pt: PaperTier, markets: list, now: float, mine_all: dict) -> None:
        slow = pt.key in SLOW_TIERS
        dt = min(max(now - pt.last, 0.0), MAX_GAP_S) if pt.last else 0.0
        pt.last = now
        self._roll_day(pt, now)
        alloc_due = now - pt.last_alloc >= (ALLOC_SLOW_S if slow else ALLOC_FAST_S)
        active = {o["slug"] for o in pt.orders.values()} | set(pt.pos)
        coc = self.tv._coc()
        try:
            first = set(self.fam.terms.joined_today(now))
        except Exception:  # noqa: BLE001
            first = set()
        books: dict[str, tuple] = {}
        ground = {slug for slug, _p in markets}
        # a market that left the tier: its entries come off; a position
        # held there keeps its exit (on the market's program, if any)
        for oid in [i for i, o in pt.orders.items()
                    if o.get("kind") == "entry" and o["slug"] not in ground]:
            o = pt.orders.pop(oid)
            pt.log.append({"ts": round(now, 1), "ev": "entry", "market": o["slug"],
                           "side": o["side"], "from": [[o["px"], o["qty"]]], "to": [],
                           "why": "the market left the tier"})
        extra = []
        for slug in pt.pos:
            if slug not in ground:
                prog = self.fam.terms.current.get(slug) if hasattr(self.fam, "terms") else None
                if prog is not None:
                    extra.append((slug, prog))
        for slug, prog in list(markets) + extra:
            if slow and not alloc_due and slug not in active:
                continue          # Tier 4: only its own markets between spreads
            nb = self.tv._books(slug, now, mine_all.get(slug) or {}, BOOK_MAX_S)
            if nb is None:
                continue
            bids, asks, tick = nb
            self.avg.update(slug, bids, asks, now)
            bk = self.tf._book(slug)
            books[slug] = (bids, asks, tick, prog, float(getattr(bk, "fetched_at", 0.0) or 0.0))
        # 1. the tape fills what it reaches
        for oid, o in list(pt.orders.items()):
            b = books.get(o["slug"])
            if b is None:
                continue
            bids, asks, tick = b[0], b[1], b[2]
            own, other = (bids, asks) if o["side"] == "BUY" else (asks, bids)
            pp, o["tseen"] = self.tv.new_print(o["slug"], float(o.get("tseen") or 0.0))
            filled, o["ahead"] = tape_fill(o["side"], o["px"], float(o["ahead"]), own, other,
                                           tick, pp)
            if filled:
                self._fill(pt, oid, o, now)
        # a position's exit side takes no entry: the lot is offered once,
        # by its exit (an entry there would open the other side on top)
        exit_sides = {(slug, "SELL" if q > 0 else "BUY") for slug, (q, _a) in pt.pos.items()
                      if abs(q) >= EXIT_MIN_QTY}
        for oid in [i for i, o in pt.orders.items()
                    if o.get("kind") == "entry" and (o["slug"], o["side"]) in exit_sides]:
            o = pt.orders.pop(oid)
            pt.acts.append(now)
            pt.day["actions"] += 1
            pt.log.append({"ts": round(now, 1), "ev": "entry", "market": o["slug"],
                           "side": o["side"], "from": [[o["px"], o["qty"]]], "to": [],
                           "why": "that side is the exit's now"})
        for k in exit_sides:
            pt.alloc.pop(k, None)
        # 2. what the orders earned and cost since the last look
        if dt > 0:
            self._accrue(pt, books, dt, coc, first)
            pt.day["pred"] += self._pred_rate(pt.key) * dt / DAY_S
            pt.day["secs"] += dt
        # 3. the decisions
        while pt.acts and now - pt.acts[0] > 60.0:
            pt.acts.popleft()
        if not pt.acts:
            pt.act_price = ACTION_FLOOR
        moves: list[tuple] = []
        moves += self._exits(pt, books, now, coc, first)
        moves += self._entries(pt, books, now, coc, first, alloc_due, ground)
        self._do(pt, moves, now)

    # -- books --------------------------------------------------------------------

    def _roll_day(self, pt: PaperTier, now: float) -> None:
        day = _day(now)
        if pt.day.get("day") == day:
            return
        un = self._unreal(pt)
        if pt.day.get("day"):
            d = dict(pt.day)
            d["unreal"] = round(un - float(d.get("unreal0") or 0.0), 4)
            d["net"] = round(d["reward"] - d["capital"] + d["realized"] + d["unreal"], 4)
            pt.days.append({k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()})
            pt.days = pt.days[-DAYS_KEEP:]
        pt.day = PaperTier._new_day(day, un)

    def _mid(self, slug: str) -> float | None:
        r = self.tf.cur.get(slug) or {}
        m = r.get("mid")
        if m is not None and float(r.get("spread") or 1.0) <= MID_TRUST_SPREAD:
            return float(m)
        return None

    def _unreal(self, pt: PaperTier) -> float:
        u = 0.0
        for slug, (q, avg) in pt.pos.items():
            m = self._mid(slug)
            if m is not None:
                u += q * (m - avg)
        return u

    def _pred_rate(self, tier: str) -> float:
        """What stage 2 says this tier's money earns a day now."""
        v = self.tv.view() if hasattr(self.tv, "view") else {}
        return float((((v.get("tiers") or {}).get(tier) or {}).get("split") or {})
                     .get("value") or 0.0)

    def _accrue(self, pt: PaperTier, books: dict, dt: float, coc: float, first: set) -> None:
        by_side: dict[tuple, dict] = {}
        for o in pt.orders.values():
            d = by_side.setdefault((o["slug"], o["side"]), {})
            d[o["px"]] = d.get(o["px"], 0.0) + o["qty"]
            if o.get("kind") == "entry":
                c = o["px"] if o["side"] == "BUY" else 1.0 - o["px"]
                pt.day["capital"] += o["qty"] * c * coc * dt / DAY_S
        for (slug, side), ours in by_side.items():
            b = books.get(slug)
            if b is None or slug in first:
                continue
            bids, asks, tick, prog, _at = b
            try:
                pool = self.fam._side_pool(slug, prog)
            except Exception:  # noqa: BLE001
                pool = None
            if not pool:
                continue
            lv = self.avg.levels(slug, side, bids if side == "BUY" else asks)
            sh, _ok, _gap = side_share(side, lv, tick, float(prog.df), float(prog.target), ours)
            pt.day["reward"] += pool * sh * dt / DAY_S
        pt.day["capital"] += pt.collateral_held() * coc * dt / DAY_S

    def _fill(self, pt: PaperTier, oid: str, o: dict, now: float) -> None:
        del pt.orders[oid]
        q, px = float(o["qty"]), float(o["px"])
        cur = pt.pos.get(o["slug"]) or [0.0, 0.0]
        Q, avg = cur
        realized = 0.0
        if o["side"] == "BUY":
            if Q >= 0:
                avg = (Q * avg + q * px) / (Q + q)
                Q += q
            else:
                close = min(q, -Q)
                realized = close * (avg - px)
                Q += close
                rem = q - close
                if rem > 1e-9:
                    Q, avg = rem, px
        else:
            if Q <= 0:
                avg = (-Q * avg + q * px) / (-Q + q)
                Q -= q
            else:
                close = min(q, Q)
                realized = close * (px - avg)
                Q -= close
                rem = q - close
                if rem > 1e-9:
                    Q, avg = -rem, px
        if abs(Q) < 1e-6:
            pt.pos.pop(o["slug"], None)
        else:
            pt.pos[o["slug"]] = [round(Q, 6), avg]
        pt.day["realized"] += realized
        pt.day["fills"] += 1
        pt.log.append({"ts": round(now, 1), "ev": "filled", "market": o["slug"],
                       "side": o["side"], "px": px, "qty": round(q, 2), "kind": o.get("kind"),
                       "realized": round(realized, 4), "held": round(Q, 2)})
        # its loss a share is read an hour later, like the paper spots'
        self.tv.marks.append({"tier": pt.key, "slug": o["slug"], "side": o["side"],
                              "px": px, "ts": now, "done": []})

    # -- the decisions ---------------------------------------------------------------

    def _side(self, pt, slug, side, b, coc, first, n_max=None, screen=False):
        bids, asks, tick, prog, _at = b
        own_live, other = (bids, asks) if side == "BUY" else (asks, bids)
        try:
            pool = self.fam._side_pool(slug, prog)
        except Exception:  # noqa: BLE001
            pool = None
        if not pool or not prog.df or not prog.target:
            return None
        lv = self.avg.levels(slug, side, own_live)
        cands = candidates(side, own_live, other, tick,
                           n_max=n_max or (4 if pt.key in SLOW_TIERS else 8))
        if not cands:
            return None
        if screen and not self.tv._may_pay(side, lv, cands, tick, prog, pool, coc):
            return None           # no step here could beat its capital
        fair = (self.tf.cur.get(slug) or {}).get("fair")
        return Side(pt.key, slug, side, lv, other, tick, prog.df, prog.target, pool,
                    self.tv._haz_fn(pt.key, tick, prog.target),
                    self.tv._loss_fn(pt.key, side, slug, fair, own_live), coc,
                    slug in first, n_max or 8, cands=cands)

    def _value(self, sd: Side, ours: dict) -> float:
        """A set of orders on one side, a day: reward less fills less capital."""
        if not ours:
            return 0.0
        sh, _ok, _gap = side_share(sd.side, sd.levels, sd.tick, sd.df, sd.target, ours)
        r = 0.0 if sd.first_day else sd.pool * sh
        f = k = 0.0
        haz = self.tv._haz_fn(sd.tier, sd.tick, sd.target)
        for px, q in ours.items():
            h = sd.h.get(px)
            if h is None:
                h = haz(sd.side, px, sd.levels, sd.touch)
            lp = sd.l.get(px)
            if lp is None:
                lp = sd.l[sd.cands[0]] if sd.cands else 0.02
            f += h * q * lp
            k += q * sd.cps(px) * sd.coc
        return r - f - k

    def _current(self, pt, slug, side, kind) -> dict:
        d: dict[float, float] = {}
        for o in pt.orders.values():
            if o["slug"] == slug and o["side"] == side and o.get("kind") == kind:
                d[o["px"]] = d.get(o["px"], 0.0) + o["qty"]
        return d

    @staticmethod
    def _same(a: dict, b: dict) -> bool:
        if set(a) != set(b):
            return False
        return all(abs(a[p] - b[p]) <= SAME_QTY * max(a[p], b[p]) for p in a)

    def _exits(self, pt, books, now, coc, first) -> list:
        """One exit per paper position, sized to what is held, at the price
        whose reward plus gain on its fill against the fair is best."""
        out = []
        for slug, (Q, avg) in list(pt.pos.items()):
            b = books.get(slug)
            if b is None or abs(Q) < EXIT_MIN_QTY:
                continue
            side = "SELL" if Q > 0 else "BUY"
            sd = self._side(pt, slug, side, b, coc, first)
            if sd is None:
                continue
            fair = (self.tf.cur.get(slug) or {}).get("fair")
            fair = float(fair) if fair is not None else (self._mid(slug) or avg)
            qty = abs(Q)
            best = None
            for px in sd.cands:
                sh, _ok, _gap = side_share(side, sd.levels, sd.tick, sd.df, sd.target, {px: qty})
                r = 0.0 if sd.first_day else sd.pool * sh
                gain = (px - fair) if side == "SELL" else (fair - px)
                v = r + sd.h[px] * qty * gain
                if best is None or v > best[0]:
                    best = (v, px)
            if best is None:
                continue
            want = {best[1]: qty}
            cur = self._current(pt, slug, side, "exit")
            if self._same(cur, want):
                continue
            if abs(sum(cur.values()) - qty) > SAME_QTY * qty:
                # the lot is not offered, or offered past what is held:
                # re-laid at once, never weighed
                out.append(("exit", slug, side, cur, want, float("inf"), sd))
                continue
            v_cur = 0.0
            if cur:
                for px, q in cur.items():
                    sh, _ok, _gap = side_share(side, sd.levels, sd.tick, sd.df, sd.target, {px: q})
                    gain = (px - fair) if side == "SELL" else (fair - px)
                    h = sd.h.get(px, sd.h[sd.cands[0]])
                    v_cur += (0.0 if sd.first_day else sd.pool * sh) + h * q * gain
            out.append(("exit", slug, side, cur, want, best[0] - v_cur, sd))
        return out

    def _entries(self, pt, books, now, coc, first, alloc_due, ground=None) -> list:
        cap = MARKET_CAP_FRAC * POT_USD
        free = max(pt.money - pt.collateral_held(), 0.0)
        targets: dict[tuple, tuple] = {}
        if alloc_due:
            pt.last_alloc = now
            sides = []
            for slug, b in books.items():
                if ground is not None and slug not in ground:
                    continue          # a position's market outside the tier: its exit only
                for side in ("BUY", "SELL"):
                    if (pt.pos.get(slug) or [0.0])[0] > 0 and side == "SELL":
                        continue      # that side is the exit's
                    if (pt.pos.get(slug) or [0.0])[0] < 0 and side == "BUY":
                        continue
                    sd = self._side(pt, slug, side, b, coc, first,
                                    screen=pt.key in SLOW_TIERS)
                    if sd is not None:
                        sides.append(sd)
            steps = run_greedy(sides, free, cap)
            alloc: dict[tuple, float] = {}
            want: dict[tuple, dict] = {}
            for st in steps:
                k = (st.slug, st.side)
                alloc[k] = alloc.get(k, 0.0) + st.cost
                want.setdefault(k, {})
                want[k][st.px] = want[k].get(st.px, 0.0) + st.qty
            pt.alloc = alloc
            by = {(sd.slug, sd.side): sd for sd in sides}
            keys = set(want) | {(o["slug"], o["side"]) for o in pt.orders.values()
                                if o.get("kind") == "entry"}
            for k in keys:
                sd = by.get(k)
                if sd is None and k[0] in books:
                    sd = self._side(pt, k[0], k[1], books[k[0]], coc, first)
                targets[k] = (want.get(k, {}), sd)
            for slug, b in books.items():
                pt.seen[slug] = b[4]
        else:
            # between spreads: a side whose book moved is re-planned alone,
            # on the money the spread gave it — the stalest first, at most
            # REPLAN_MAX a look (the rest wait for the next second)
            due = [(pt.seen.get(slug, 0.0), slug, side, money)
                   for (slug, side), money in list(pt.alloc.items())
                   if slug in books and books[slug][4] > pt.seen.get(slug, 0.0) + 1e-9]
            done_m: set = set()
            for _seen, slug, side, money in sorted(due)[:REPLAN_MAX]:
                sd = self._side(pt, slug, side, books[slug], coc, first)
                done_m.add(slug)
                if sd is None:
                    continue
                steps = run_greedy([sd], money + 1e-6, cap)
                w: dict[float, float] = {}
                for st in steps:
                    w[st.px] = w.get(st.px, 0.0) + st.qty
                targets[(slug, side)] = (w, sd)
            for slug in done_m:
                pt.seen[slug] = books[slug][4]
        out = []
        for (slug, side), (want, sd) in targets.items():
            cur = self._current(pt, slug, side, "entry")
            if self._same(cur, want) or sd is None:
                continue
            gain_day = self._value(sd, want) - self._value(sd, cur)
            out.append(("entry", slug, side, cur, want, gain_day, sd))
        return out

    @staticmethod
    def _coll(side: str, ours: dict) -> float:
        return sum(q * (p if side == "BUY" else 1.0 - p) for p, q in ours.items())

    def _do(self, pt, moves: list, now: float) -> None:
        """Changes worth their cost, best first, within the action budget
        and the tier's money: exits first (they take no money), then
        entries by what they gain over their cost. A side the spread no
        longer funds is given up when its money is wanted elsewhere."""
        scored = []
        droppable = []
        for kind, slug, side, cur, want, gain_day, sd in moves:
            n_act = len(cur) + len(want)
            if n_act == 0:
                continue
            h_s = pt.hold.get((slug, side), HOLD_PRIOR_S)
            overlap = 0.0
            if sd is not None:
                for px, q in want.items():
                    hz = sd.h.get(px)
                    if hz is not None:
                        overlap += hz * q * sd.l.get(px, 0.02) * OVERLAP_S / DAY_S
            cost = n_act * pt.act_price + overlap
            gain = gain_day * h_s / DAY_S if math.isfinite(gain_day) else float("inf")
            if gain <= cost:
                if kind == "entry" and not want and cur:
                    # the spread funds it no longer, but it earns: kept until
                    # its money is wanted, then given up, weakest first
                    v = -gain_day                      # its own value a day
                    droppable.append((v / max(self._coll(side, cur), 1e-9), slug, side, cur))
                continue
            scored.append((0 if kind == "exit" else 1, -(gain - cost), n_act, kind, slug, side,
                           cur, want, gain_day, gain, cost))
        scored.sort(key=lambda x: (x[0], x[1]))
        droppable.sort()
        free = max(pt.money - pt.collateral_held(), 0.0)
        used = sum(self._coll(o["side"], {o["px"]: o["qty"]}) for o in pt.orders.values()
                   if o.get("kind") == "entry")
        skipped_best = 0.0
        waiting = 0
        for (_pri, _neg, n_act, kind, slug, side, cur, want, gain_day, gain, cost) in scored:
            if len(pt.acts) + n_act > ACTIONS_PER_MIN:
                pt.day["skipped"] += 1
                if math.isfinite(gain):
                    skipped_best = max(skipped_best, (gain - cost) / n_act + pt.act_price)
                continue
            if kind == "entry":
                need = used + self._coll(side, want) - self._coll(side, cur) - free
                while need > 1e-6 and droppable and len(pt.acts) + n_act + len(
                        droppable[0][3]) <= ACTIONS_PER_MIN:
                    _v, ds, dsd, dcur = droppable.pop(0)
                    self._apply(pt, "entry", ds, dsd, dcur, {}, now, None, 0.0,
                                "given up for money wanted elsewhere")
                    used -= self._coll(dsd, dcur)
                    need -= self._coll(dsd, dcur)
                if need > 1e-6:
                    waiting += 1                     # its money is held by positions
                    continue
                used += self._coll(side, want) - self._coll(side, cur)
            self._apply(pt, kind, slug, side, cur, want, now, gain_day, cost, "")
        if skipped_best > 0:
            pt.act_price = max(ACTION_FLOOR, skipped_best)
        pt.waiting_money = waiting

    def _apply(self, pt, kind, slug, side, cur, want, now, gain_day, cost, why) -> None:
        n_act = len(cur) + len(want)
        for o_id in [i for i, o in pt.orders.items()
                     if o["slug"] == slug and o["side"] == side and o.get("kind") == kind]:
            del pt.orders[o_id]
        b_levels = self._live_levels(slug, side, now)
        for px, q in want.items():
            ahead = sum(qq for pp, qq in b_levels if abs(pp - px) < 1e-6)
            oid = pt.oid()
            pt.orders[oid] = {"slug": slug, "side": side, "px": px, "qty": round(q, 4),
                              "t0": round(now, 1), "kind": kind, "ahead": ahead,
                              "tseen": self.tv.last_print_at(slug)}
        for _ in range(n_act):
            pt.acts.append(now)
        pt.day["actions"] += n_act
        pt.day["moves"] += 1
        k = (slug, side)
        last = pt.changed.get(k)
        if last is not None and cur:
            h = pt.hold.get(k, HOLD_PRIOR_S)
            pt.hold[k] = (1 - HOLD_ALPHA) * h + HOLD_ALPHA * (now - last)
        pt.changed[k] = now
        pt.log.append({"ts": round(now, 1), "ev": kind, "market": slug, "side": side,
                       "from": sorted(cur.items()),
                       "to": sorted((p, round(q, 2)) for p, q in want.items()),
                       "gain_day": (round(gain_day, 4) if gain_day is not None
                                    and math.isfinite(gain_day) else None),
                       "cost": round(cost, 4), "why": why})

    def _live_levels(self, slug: str, side: str, now: float) -> list:
        nb = self.tv._books(slug, now, (self.tv._ours().get(slug) or {}), BOOK_MAX_S)
        if nb is None:
            return []
        return nb[0] if side == "BUY" else nb[1]

    # -- the check against real orders -----------------------------------------------

    def _shadows(self, now: float, mine_all: dict) -> None:
        """A paper twin for every real order of the tender's, filled by the
        same tape rule, scored against what the real order did."""
        try:
            ids = set(self.tender_ids() or ())
        except Exception:  # noqa: BLE001
            ids = set()
        try:
            live = {k: v for k, v in dict(self.fam.orders).items() if k in ids}
        except RuntimeError:
            return
        mk = self.tv._mk()
        for oid, r in live.items():
            if oid in self.shadows or r.market not in mk:
                continue
            nb = self.tv._books(r.market, now, mine_all.get(r.market) or {}, BOOK_MAX_S)
            own = (nb[0] if r.side == "BUY" else nb[1]) if nb else []
            ahead = sum(q for p, q in own if abs(p - r.price) < 1e-6)
            self.shadows[oid] = {"slug": r.market, "tier": mk[r.market][0], "side": r.side,
                                 "px": float(r.price), "qty": float(r.qty), "t0": now,
                                 "ahead": ahead, "tseen": self.tv.last_print_at(r.market),
                                 "paper_at": None, "gone_at": None}
        for oid, sh in list(self.shadows.items()):
            if sh["gone_at"] is None and oid not in live:
                sh["gone_at"] = now
            if sh["paper_at"] is None and sh["gone_at"] is None:
                nb = self.tv._books(sh["slug"], now, mine_all.get(sh["slug"]) or {}, BOOK_MAX_S)
                if nb is not None:
                    bids, asks, tick = nb
                    own, other = (bids, asks) if sh["side"] == "BUY" else (asks, bids)
                    pp, sh["tseen"] = self.tv.new_print(sh["slug"], float(sh["tseen"]))
                    filled, sh["ahead"] = tape_fill(sh["side"], sh["px"], float(sh["ahead"]),
                                                    own, other, tick, pp)
                    if filled:
                        sh["paper_at"] = now
            done = ((sh["gone_at"] is not None and now - sh["gone_at"] >= SHADOW_GRACE_S)
                    or now - sh["t0"] >= SHADOW_H_S)
            if done:
                self._score_shadow(oid, sh, now)

    def _score_shadow(self, oid: str, sh: dict, now: float) -> None:
        real_at = None
        for f in list(getattr(self.fam, "fills", []) or [])[-600:]:
            if f.get("oid") == oid and float(f.get("ts") or 0.0) >= sh["t0"] - 60.0:
                real_at = float(f["ts"])
                break
        paper = sh["paper_at"] is not None
        real = real_at is not None
        what = ("both" if paper and real else "paper_only" if paper
                else "real_only" if real else "neither")
        t = self.shadow_tally.setdefault(sh["tier"], {"both": 0, "paper_only": 0,
                                                      "real_only": 0, "neither": 0,
                                                      "lag_s": 0.0})
        t[what] += 1
        if paper and real:
            t["lag_s"] += sh["paper_at"] - real_at
        self.shadow_recent.append({"ts": round(now, 1), "market": sh["slug"],
                                   "tier": sh["tier"], "side": sh["side"], "px": sh["px"],
                                   "qty": round(sh["qty"], 2), "what": what,
                                   "rested_min": round(((sh["gone_at"] or now) - sh["t0"]) / 60.0, 1)})
        del self.shadows[oid]

    # -- the page -------------------------------------------------------------------

    def view(self) -> dict:
        now = self._clock()
        tiers = {}
        for t, name, _p in TIERS:
            pt = self.tiers[t]
            d = pt.day
            un = self._unreal(pt) - float(d.get("unreal0") or 0.0)
            net = d["reward"] - d["capital"] + d["realized"] + un
            orders = sorted(pt.orders.values(), key=lambda o: (o["slug"], o["side"], o["px"]))
            tiers[t] = {"name": name, "money": pt.money, "share": self.shares.get(t, 0.0),
                        "orders": len(pt.orders),
                        "entries": sum(1 for o in orders if o.get("kind") == "entry"),
                        "exits": sum(1 for o in orders if o.get("kind") == "exit"),
                        "coll_orders": round(sum(o["qty"] * (o["px"] if o["side"] == "BUY"
                                                             else 1 - o["px"])
                                                 for o in orders if o.get("kind") == "entry"), 2),
                        "coll_held": round(pt.collateral_held(), 2),
                        "positions": [{"market": s, "name": self._name(s), "qty": round(q, 2),
                                       "avg": round(a, 4), "mid": self._mid(s)}
                                      for s, (q, a) in sorted(pt.pos.items())][:30],
                        "today": {"reward": round(d["reward"], 4), "capital": round(d["capital"], 4),
                                  "realized": round(d["realized"], 4), "unreal": round(un, 4),
                                  "net": round(net, 4), "pred": round(d["pred"], 4),
                                  "fills": d["fills"], "actions": d["actions"],
                                  "moves": d["moves"], "skipped": d["skipped"],
                                  "hours": round(d["secs"] / 3600.0, 2)},
                        "waiting_money": pt.waiting_money,
                        "days": pt.days[-7:], "act_price": round(pt.act_price, 4),
                        "acts_min": len(pt.acts),
                        "list": [{"market": o["slug"], "name": self._name(o["slug"]),
                                  "side": o["side"], "px": o["px"], "qty": round(o["qty"], 2),
                                  "kind": o.get("kind")} for o in orders][:40],
                        "log": list(pt.log)[-12:],
                        "shadow": self.shadow_tally.get(t) or {}}
        return {"ok": bool(self.shares), "stage": 3, "read_only": True, "version": VERSION,
                "pot": POT_USD, "split_day": self.split_day, "split_why": self.split_why,
                "actions_per_min": ACTIONS_PER_MIN, "tiers": tiers,
                "shadows_open": len(self.shadows),
                "shadow_recent": list(self.shadow_recent)[-15:],
                "error": self.error, "tick_s": self.tick_s, "now": now}

    def _name(self, slug: str) -> str:
        return str((self.fam.universe.get(slug) or {}).get("name") or slug)

    # -- persistence -----------------------------------------------------------------

    def to_dict(self) -> dict:
        return {"version": VERSION, "shares": self.shares, "split_day": self.split_day,
                "split_why": self.split_why,
                "tiers": {t: pt.to_dict() for t, pt in self.tiers.items()},
                "shadow_tally": self.shadow_tally,
                "shadow_recent": list(self.shadow_recent)}

    def restore(self, d: dict) -> None:
        self.shares = {k: float(v) for k, v in (d.get("shares") or {}).items()}
        self.split_day = str(d.get("split_day") or "")
        self.split_why = str(d.get("split_why") or "")
        for t, td in (d.get("tiers") or {}).items():
            if t in self.tiers and isinstance(td, dict):
                self.tiers[t].restore(td)
        self.shadow_tally = {t: dict(v) for t, v in (d.get("shadow_tally") or {}).items()}
        self.shadow_recent.extend(d.get("shadow_recent") or [])

    def payload(self) -> bytes:
        try:
            return json.dumps(self.view()).encode()
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "note": f"{type(e).__name__}: {e}"[:160]}).encode()
