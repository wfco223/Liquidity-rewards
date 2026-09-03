"""Bonds: the tax reserve earning like a bond (owner, 2026-09-02).

The spec: "Designate any politics market where the Nate Silver gives
>99% odds of an outcome as a bond market. Then on a separate page in
the app, give me a list of the cheapest bond positions with information
on what a resting sell order would earn. Before adding a new market to
the bond list ask me first. Once I'm in the positions they should
automatically [have] a sell order placed so that they are earning."

And the corrections, same day, in order:
- Checked once a night, silently; proposals up top for him to add; a
  market that leaves the band drops off by itself; both ends count —
  99%+ for YES is a YES bond, 1% or under is a NO bond (buy NO: a short
  of YES at 1-2c).
- The engine keeps quoting bond markets; it only never touches a bond
  order, and never rests its own exits on the BOND shares.
- The resting order sits behind the touch where its size still earns:
  the farthest slot back that keeps KEEP_FRACTION of the best reward,
  never past cost; it never chases forward and moves back only when it
  has become the touch.
- "I will leave the money out of my account. When there is a great
  opportunity the bond engine makes the purchase and I top off the
  money for the engine." A deploy budget he sets and tops up; a ping
  only after $100 of purchases.
- "Remove all of the information from shares held from non-bond
  purchases. I only want to know for a market what the bond purchases
  are." So this module keeps its OWN ledger of bond shares per market
  (what it bought, less what its orders sold), and the exchange
  position is only a cap on what it may sell.
- "The sniper is not anything cheaper than 98.5, it's purchasing
  anything that is in the way of our sell orders collecting rewards.
  And the way to do it is gradually lead the minnow down until it is
  priced more cheaply. Then snap and buy their shares, adding them to
  our total in that market." So: when a small order (a minnow) rests
  IN FRONT of our earning order, a small DECOY order of ours JOINS it
  at its price and waits two hours for it to move; each time it moves
  the decoy joins it again. When it stays put for the wait, the decoy
  comes off and its shares are taken at its price; when it moves more
  than three times, reaches the far touch, or crosses under our cost,
  it is taken at once. They join the bond, and our main order has one
  competitor fewer.
- "The sniper should only work where I have bond sales resting." So
  nothing is bought anywhere a bond order of ours is not resting: no
  automatic entry into new ground, no price bar. A bond starts with
  the owner buying by hand and counting the shares in from the page.

Bond-LIKE across many such markets; on any one the price is the
market's own odds that the lot goes to zero, and the page says so.
Proceeds and the budget are a ledger here, not the exchange's cash. A
sale counts only when OUR order gave up that much while the position
moved — a hand sale is never reinvested. Every take lifts the touch
through the desk's second carved exception (taker="bond": owner's rail,
never past the touch, never more than it shows). Nothing places unless
the bonds switch on /switch is ON.
"""

from __future__ import annotations

import math
import time

from .family import FamilyOrder, slug_days_out
from .intents import BUY_LONG, BUY_SHORT, SELL_LONG, SELL_SHORT

HIGH_ODDS = 0.99            # YES bond: Silver's odds for YES at or above
LOW_ODDS = 0.01             # NO bond: Silver's odds for YES at or below
PRICE_CAP = 0.995           # never pay more than this per dollar of bond
MONEY_MIN_USD = 5.0         # money below this waits
PING_EVERY_USD = 100.0      # a phone ping per this much bought
KEEP_FRACTION = 0.6         # the resting slot keeps this much of the best reward
BEHIND_MAX_TICKS = 8
MOVE_COOLDOWN_S = 1800.0    # a move back happens at most this often
DECOY_QTY = 10.0            # the decoy that leads a minnow down
MINNOW_MAX = 25.0           # a level in front this small is a minnow to lead
DANCE_WAIT_S = 2 * 3600.0   # after each decoy move, how long the minnow gets to move again
DANCE_MAX_MOVES = 3         # a minnow that moves more than this is taken at once
SCAN_HOUR_UTC = 7           # 3 am Eastern: the nightly Silver check
LOG_KEEP = 200
DROPPED_KEEP = 30


def side_for(odds: float | None) -> str | None:
    """Which side is the bond at these odds, if any."""
    if odds is None:
        return None
    if odds >= HIGH_ODDS:
        return "YES"
    if odds <= LOW_ODDS:
        return "NO"
    return None


def scan_due(now: float, last_day: str, hour: int = SCAN_HOUR_UTC) -> str | None:
    """The night's scan: once per UTC day, after its hour."""
    t = time.gmtime(now)
    day = time.strftime("%Y-%m-%d", t)
    if day == last_day or t.tm_hour < hour:
        return None
    return day


class Bonds:
    def __init__(self, fam, client, fair, clock=None, alert=None):
        self.fam = fam                  # the politics family
        self.client = client
        self.fair = fair                # slug -> Silver's YES odds, or None
        self._clock = clock or time.time
        self.alert = alert or (lambda title, msg: None)
        self.approved: dict[str, dict] = {}   # slug -> {added, odds, side}
        self.proposed: dict[str, dict] = {}   # slug -> {odds, side, since}
        self.ignored: dict[str, float] = {}   # slug -> ts
        self.dropped: dict[str, dict] = {}    # slug -> {odds, side, ts}
        # the bond ledger: what this module bought, less what its orders
        # sold — signed YES shares (a NO bond is negative) and dollars
        self.lots: dict[str, dict] = {}       # slug -> {qty, cost}
        self.cash: float = 0.0                # sale proceeds awaiting use
        self.budget: float = 0.0              # the owner's deploy money
        self.spent: float = 0.0               # from the budget, since set
        self.unpinged: float = 0.0            # bought since the last ping
        self.moved_at: dict[str, float] = {}
        self.slot: dict[str, dict] = {}
        self.dance: dict[str, dict] = {}      # slug -> {px, moves, since}
        self.scan_day: str = ""
        self.log: list[dict] = []
        self._earn_seen: dict[str, float] = {}
        self._earn_px: dict[str, float] = {}

    # ------------------------------------------------------------ helpers

    def _log(self, **kw) -> None:
        kw["ts"] = round(self._clock(), 1)
        self.log.append(kw)
        del self.log[:-LOG_KEEP]

    @staticmethod
    def entry(side: str) -> tuple[str, str]:
        """(book side, intent) that OPENS a bond of this side."""
        return ("BUY", BUY_LONG) if side == "YES" else ("SELL", BUY_SHORT)

    @staticmethod
    def earn(side: str) -> tuple[str, str]:
        """(book side, intent) of the resting order that earns while the
        bond waits — and closes it if it fills."""
        return ("SELL", SELL_LONG) if side == "YES" else ("BUY", SELL_SHORT)

    def _orders(self, slug: str, book_side: str | None = None,
                decoy: bool | None = None) -> list[FamilyOrder]:
        out = []
        for o in list(self.fam.orders.values()):
            if o.purpose != "bond" or o.market != slug:
                continue
            if book_side is not None and o.side != book_side:
                continue
            is_decoy = str(o.why or "").startswith("bond decoy")
            if decoy is not None and is_decoy != decoy:
                continue
            out.append(o)
        return out

    def held(self, slug: str, side: str) -> float:
        """Bond shares held here, from the ledger — never the exchange's
        whole position (owner, 2026-09-02)."""
        q = float((self.lots.get(slug) or {}).get("qty") or 0.0)
        return max(q, 0.0) if side == "YES" else max(-q, 0.0)

    def exchange_held(self, slug: str, side: str, positions: dict | None) -> float:
        """What the exchange says is there on this side — the cap on
        what the bond may sell."""
        if positions is not None and slug in positions:
            pos = float((positions.get(slug) or (0.0, 0.0))[0])
        else:
            pos = float((self.fam.inventory.get(slug) or {}).get("qty") or 0.0)
        return max(pos, 0.0) if side == "YES" else max(-pos, 0.0)

    def cost_basis(self, slug: str, side: str) -> float:
        """What a dollar of this bond cost, from the ledger."""
        lot = self.lots.get(slug) or {}
        q = abs(float(lot.get("qty") or 0.0))
        c = float(lot.get("cost") or 0.0)
        return round(c / q, 4) if q > 0.005 and c > 0 else 0.0

    def _yes_px_floor(self, slug: str, side: str) -> float:
        """The YES price our earning order must not cross: what a share
        cost (YES) or fetched (NO), from the ledger."""
        cb = self.cost_basis(slug, side)
        if cb <= 0:
            return 0.0
        return cb if side == "YES" else round(1.0 - cb, 4)

    @staticmethod
    def _snap_up(px: float, tick: float) -> float:
        return round(math.ceil(px / tick - 1e-9) * tick, 4)

    @staticmethod
    def _snap_down(px: float, tick: float) -> float:
        return round(math.floor(px / tick + 1e-9) * tick, 4)

    def _mark_engine(self) -> None:
        """The engine keeps quoting bond markets; it just never rests its
        own exits on the BOND shares (and never touches a bond order)."""
        self.fam.bond_qty = {s: float(l.get("qty") or 0.0)
                             for s, l in self.lots.items()
                             if abs(float(l.get("qty") or 0.0)) > 0.005}

    def _book_lot(self, slug: str, side: str, qty: float, usd: float) -> None:
        lot = self.lots.setdefault(slug, {"qty": 0.0, "cost": 0.0})
        lot["qty"] = round(lot["qty"] + (qty if side == "YES" else -qty), 4)
        lot["cost"] = round(lot["cost"] + usd, 4)
        if abs(lot["qty"]) < 0.005:
            self.lots.pop(slug, None)
        self._mark_engine()

    def _unbook_lot(self, slug: str, side: str, qty: float) -> float:
        """A sale by our order: shares leave the ledger at their average
        cost. Returns the dollars they cost."""
        lot = self.lots.get(slug)
        if not lot:
            return 0.0
        q = abs(float(lot.get("qty") or 0.0))
        take = min(qty, q)
        if q <= 0.005 or take <= 0.005:
            return 0.0
        usd = round(float(lot.get("cost") or 0.0) * take / q, 4)
        lot["qty"] = round(lot["qty"] - (take if side == "YES" else -take), 4)
        lot["cost"] = round(lot["cost"] - usd, 4)
        if abs(lot["qty"]) < 0.005:
            self.lots.pop(slug, None)
        self._mark_engine()
        return usd

    def _money(self) -> float:
        return self.cash + self.budget

    def _pay(self, usd: float) -> float:
        """Proceeds first, then the budget. Returns what came from the
        budget."""
        from_cash = min(self.cash, usd)
        self.cash = round(self.cash - from_cash, 4)
        from_budget = round(usd - from_cash, 4)
        self.budget = round(max(self.budget - from_budget, 0.0), 4)
        self.spent = round(self.spent + from_budget, 4)
        return from_budget

    def _ping_maybe(self, usd: float) -> None:
        """One ping per PING_EVERY_USD bought, not per purchase."""
        self.unpinged = round(self.unpinged + usd, 4)
        if self.unpinged + 1e-9 >= PING_EVERY_USD:
            self.alert("Bond engine bought",
                       f"${self.unpinged:,.2f} of bonds since the last note; "
                       f"${self.budget:,.2f} of the deploy budget left, "
                       f"${self.cash:,.2f} of proceeds waiting")
            self.unpinged = 0.0

    # ------------------------------------------------------------ the list

    def scan(self, now: float, force: bool = False) -> list[str]:
        """Once a night (or on the page's button): Silver's odds propose
        new markets and drop listed ones that left the band. Silent."""
        if not force:
            day = scan_due(now, self.scan_day)
            if day is None:
                return []
            self.scan_day = day
        new: list[str] = []
        pool = set(self.fam.universe) | set(self.fam.inventory) | set(self.approved)
        for slug in sorted(pool):
            p = self.fair(slug)
            s = side_for(p)
            if slug in self.approved:
                meta = self.approved[slug]
                if p is not None:
                    meta["odds"] = round(p, 4)
                if p is not None and s != meta.get("side"):
                    self.dropped[slug] = {"odds": round(p, 4),
                                          "side": meta.get("side"),
                                          "ts": round(now, 1)}
                    for k in sorted(self.dropped,
                                    key=lambda k: self.dropped[k]["ts"])[:-DROPPED_KEEP]:
                        self.dropped.pop(k, None)
                    del self.approved[slug]
                    self._log(event="dropped", market=slug, odds=round(p, 4))
                continue
            if p is None or slug in self.ignored:
                continue
            if s is None:
                self.proposed.pop(slug, None)
                continue
            if slug not in self.proposed:
                self.proposed[slug] = {"odds": round(p, 4), "side": s,
                                       "since": round(now, 1)}
                new.append(slug)
            else:
                self.proposed[slug].update(odds=round(p, 4), side=s)
        if new:
            self._log(event="proposed", n=len(new), slugs=new[:6])
        return new

    def approve(self, slug: str, now: float) -> dict:
        p = self.fair(slug)
        s = side_for(p)
        if p is None:
            return {"ok": False, "note": "Silver has no odds for this market"}
        if s is None:
            return {"ok": False,
                    "note": f"Silver puts YES at {p:.1%} — inside 1% to 99%, "
                            f"not a bond"}
        self.approved[slug] = {"added": round(now, 1), "odds": round(p, 4),
                               "side": s}
        self.proposed.pop(slug, None)
        self.ignored.pop(slug, None)
        self.dropped.pop(slug, None)
        self._log(event="approved", market=slug, odds=round(p, 4), side=s)
        return {"ok": True, "note": f"added as a {s} bond — Silver {p:.1%} "
                                    f"for YES"}

    def ignore(self, slug: str, now: float) -> dict:
        self.proposed.pop(slug, None)
        self.ignored[slug] = round(now, 1)
        self._log(event="ignored", market=slug)
        return {"ok": True, "note": "ignored — it will not be proposed again"}

    def unignore(self, slug: str) -> dict:
        if self.ignored.pop(slug, None) is None:
            return {"ok": False, "note": "not on the ignore list"}
        return {"ok": True, "note": "back in the running — the next scan "
                                    "may propose it"}

    def remove(self, slug: str, now: float) -> dict:
        meta = self.approved.pop(slug, None)
        if meta is None:
            return {"ok": False, "note": "not on the bond list"}
        self.dropped[slug] = {"odds": meta.get("odds"), "side": meta.get("side"),
                              "ts": round(now, 1), "by": "owner"}
        self._log(event="removed", market=slug)
        return {"ok": True, "note": "removed — a bond order still resting "
                                    "stays yours; the engine leaves it alone"}

    def adopt(self, slug: str, qty, positions: dict | None = None) -> dict:
        """Count shares the owner already holds here as bond shares, at
        the family's cost basis for them — his call, from the page."""
        meta = self.approved.get(slug)
        if meta is None:
            return {"ok": False, "note": "not on the bond list"}
        side = meta["side"]
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            return {"ok": False, "note": "how many shares?"}
        room = self.exchange_held(slug, side, positions) - self.held(slug, side)
        if qty <= 0 or qty > room + 1e-9:
            return {"ok": False,
                    "note": f"the exchange shows {room:g} {side} shares here "
                            f"not yet counted as bond"}
        inv = self.fam.inventory.get(slug) or {}
        q = float(inv.get("qty") or 0.0)
        c = float(inv.get("cost") or 0.0)
        yes_px = abs(c / q) if abs(q) > 0.005 and c else 0.0
        per = yes_px if side == "YES" else (1.0 - yes_px if yes_px else 0.0)
        self._book_lot(slug, side, qty, round(qty * per, 4))
        self._log(event="adopted", market=slug, side=side, qty=qty,
                  cost=round(qty * per, 2))
        return {"ok": True, "note": f"counting {qty:g} {side} shares as bond "
                                    f"at {per * 100:.1f}c"}

    def set_budget(self, amount) -> dict:
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return {"ok": False, "note": "budget must be a number"}
        if amount < 0 or amount > 100000:
            return {"ok": False, "note": "budget must be $0 to $100,000"}
        self.budget = round(amount, 2)
        self.spent = 0.0
        self._log(event="budget_set", usd=self.budget)
        return {"ok": True, "note": f"deploy budget set to ${self.budget:,.2f}"}

    # ------------------------------------------------------------ the money

    def cycle(self, now: float, positions: dict, on: bool) -> dict:
        """Once a cycle, after the family has run: count sales, keep
        every held bond earning, work the minnows, enter new ground.
        Places nothing unless the bonds switch is on."""
        self.scan(now)
        self._mark_engine()
        placed: list[dict] = []
        # sales: our earning order gave up shares and the ledger shrinks
        for slug in sorted(set(self.approved) | set(self.lots)):
            side = (self.approved.get(slug) or {}).get("side") or (
                "YES" if float((self.lots.get(slug) or {}).get("qty") or 0) >= 0
                else "NO")
            bs, _ = self.earn(side)
            seen = self._earn_seen.get(slug, 0.0)
            now_q = sum(o.qty for o in self._orders(slug, bs))
            gave = max(seen - now_q, 0.0)
            if gave > 0.005 and self.held(slug, side) > 0.005:
                # confirm against the exchange: the position must have
                # moved that way too, else the order was cancelled
                exch = self.exchange_held(slug, side, positions)
                prev = self._exch_seen.get(slug) if hasattr(self, "_exch_seen") else None
                if prev is not None and exch < prev - 0.005:
                    sold = min(gave, prev - exch, self.held(slug, side))
                    px = self._earn_px.get(slug, 0.0)
                    proceeds = sold * (px if side == "YES" else (1.0 - px))
                    cost = self._unbook_lot(slug, side, sold)
                    self.cash = round(self.cash + proceeds, 4)
                    self._log(event="sold", market=slug, side=side,
                              qty=round(sold, 2), price=px,
                              proceeds=round(proceeds, 2),
                              gain=round(proceeds - cost, 2),
                              cash=round(self.cash, 2))
        if on:
            for slug in sorted(self.approved):
                side = self.approved[slug]["side"]
                r = self._keep_earning(slug, side, positions, now)
                if r:
                    placed.append(r)
                r = self._work_minnows(slug, side, positions, now)
                if r:
                    placed.append(r)
        if not hasattr(self, "_exch_seen"):
            self._exch_seen = {}
        for slug in set(self.approved) | set(self.lots):
            side = (self.approved.get(slug) or {}).get("side") or (
                "YES" if float((self.lots.get(slug) or {}).get("qty") or 0) >= 0
                else "NO")
            bs, _ = self.earn(side)
            main = self._orders(slug, bs, decoy=False)
            self._earn_seen[slug] = sum(o.qty for o in main)
            if main:
                self._earn_px[slug] = main[0].price
            self._exch_seen[slug] = self.exchange_held(slug, side, positions)
        return {"placed": placed, "cash": round(self.cash, 2)}

    # -- the resting order ----------------------------------------------------

    def _best_slot(self, slug: str, side: str, book, qty: float,
                   bound: float, start: float | None = None):
        """The farthest slot behind the touch that still keeps
        KEEP_FRACTION of the best reward on offer, never past `bound`.
        Returns (price, est_day, ticks_behind, keep) or None."""
        from .scoring import estimate_join
        prog = self.fam.terms.get(slug)
        tick = book.tick or 0.01
        bs, _ = self.earn(side)
        raw = list(book.side(bs))
        for o in self._orders(slug, bs):
            raw = [(p, (q - o.qty) if abs(p - o.price) < tick / 2 else q)
                   for p, q in raw]
        levels = [(p, q) for p, q in raw if q > 1e-9]
        pool = self.fam._side_pool(slug, prog) if prog is not None else None
        own = book.side(bs)
        touch = start if start is not None else (own[0][0] if own else None)
        if side == "YES":
            origin = max(touch if touch is not None else bound, bound)
            cands = [self._snap_up(origin + i * tick, tick)
                     for i in range(BEHIND_MAX_TICKS + 1)]
            cands = [p for p in cands if p <= 0.999
                     and not (book.bids and p <= book.bids[0][0] + 1e-9)]
        else:
            origin = min(touch if touch is not None else bound, bound)
            cands = [self._snap_down(origin - i * tick, tick)
                     for i in range(BEHIND_MAX_TICKS + 1)]
            cands = [p for p in cands if p >= 0.001
                     and not (book.asks and p >= book.asks[0][0] - 1e-9)]
        cands = list(dict.fromkeys(cands))
        if not cands:
            return None
        scored = []
        for px in cands:
            est = 0.0
            if prog is not None and pool:
                j = estimate_join(bs, levels, tick, float(prog.df),
                                  float(prog.target), px, qty)
                est = j.share * pool if (j.qualifies and j.in_window) else 0.0
            ref = touch if touch is not None else px
            scored.append((px, est, int(round(abs(px - ref) / tick))))
        best = max(e for _, e, _ in scored)
        if best <= 0:
            px, est, ticks = scored[0]
            return px, est, ticks, 1.0
        for px, est, ticks in reversed(scored):
            if est >= KEEP_FRACTION * best - 1e-12:
                return px, est, ticks, est / best
        px, est, ticks = scored[0]
        return px, est, ticks, 1.0

    def _bound(self, slug: str, side: str, tick: float) -> float:
        px = self._yes_px_floor(slug, side)
        if side == "YES":
            return self._snap_up(px, tick) if px > 0 else 0.0
        return self._snap_down(px, tick) if px > 0 else 0.999

    def _keep_earning(self, slug: str, side: str, positions: dict,
                      now: float) -> dict | None:
        """The bond's main resting order, sized to the ledger (and never
        more than the exchange shows), at its best slot, kept there. It
        never chases forward; it moves back, on a cooldown, only when
        it has become the touch."""
        held = min(self.held(slug, side),
                   self.exchange_held(slug, side, positions))
        if held < 1.0:
            return None
        bs, intent = self.earn(side)
        book = self.fam.cache.fresh(slug, 120.0, now)
        if book is None:
            return None
        tick = book.tick or 0.01
        bound = self._bound(slug, side, tick)
        main = self._orders(slug, bs, decoy=False)
        decoys = self._orders(slug, bs, decoy=True)
        resting = sum(o.qty for o in main) + sum(o.qty for o in decoys)
        pos = float((positions.get(slug) or (0.0, 0.0))[0])
        if main:
            cur = main[0]
            own = book.side(bs)
            touch = own[0][0] if own else None
            at_front = touch is not None and (
                (side == "YES" and cur.price <= touch + 1e-9)
                or (side == "NO" and cur.price >= touch - 1e-9))
            if (at_front and not decoys
                    and now - self.moved_at.get(slug, 0.0) >= MOVE_COOLDOWN_S):
                slot = self._best_slot(slug, side, book, cur.qty, bound,
                                       start=cur.price)
                if slot and ((side == "YES" and slot[0] > cur.price + tick / 2)
                             or (side == "NO" and slot[0] < cur.price - tick / 2)):
                    r = self.fam.desk.reprice(
                        {"id": cur.id, "market": slug, "side": bs,
                         "price": cur.price, "size": cur.qty,
                         "intent": cur.intent}, slot[0], initiator="owner")
                    if r.ok and r.order_id:
                        if not r.two_orders:
                            self.fam.orders.pop(cur.id, None)
                        self.fam.orders[r.order_id] = FamilyOrder(
                            id=r.order_id, market=slug, side=bs,
                            price=(r.price or slot[0]), qty=cur.qty,
                            intent=cur.intent, placed_ts=now, purpose="bond",
                            why="bond: moved back behind the touch — "
                                "still earning, selling slower")
                        self.moved_at[slug] = now
                        self.slot[slug] = {"px": (r.price or slot[0]),
                                           "ticks": slot[2],
                                           "keep": round(slot[3], 3),
                                           "est": round(slot[1], 4)}
                        self._log(event="earn_moved_back", market=slug,
                                  side=side, price=(r.price or slot[0]),
                                  qty=cur.qty, ticks=slot[2])
                        return {"market": slug, "bond": side, "side": bs,
                                "price": (r.price or slot[0]), "qty": cur.qty,
                                "moved": True}
        qty = float(math.floor(held - resting))
        if qty < 1.0:
            return None
        slot = self._best_slot(slug, side, book, qty, bound)
        if slot is None:
            return None
        want, est, ticks, keep = slot
        r = self.fam.desk.place_resting(slug, bs, want, qty, net_position=pos,
                                        initiator="owner", intent=intent)
        if not (r.ok and r.order_id):
            self._log(event="earn_refused", market=slug, note=r.note[:120])
            return None
        self.fam.orders[r.order_id] = FamilyOrder(
            id=r.order_id, market=slug, side=bs, price=(r.price or want),
            qty=qty, intent=r.intent or intent, placed_ts=now, purpose="bond",
            why=(f"bond: resting {ticks} tick{'s' if ticks != 1 else ''} "
                 f"behind the touch, keeping {keep:.0%} of the best reward"
                 if ticks else "bond: resting at the touch — it earns while "
                               "it waits"))
        self.moved_at[slug] = now
        self.slot[slug] = {"px": (r.price or want), "ticks": ticks,
                           "keep": round(keep, 3), "est": round(est, 4)}
        self._log(event="earn_rested", market=slug, side=side,
                  price=(r.price or want), qty=qty, ticks=ticks)
        return {"market": slug, "bond": side, "side": bs,
                "price": (r.price or want), "qty": qty, "ticks": ticks}

    # -- the sniper ----------------------------------------------------------

    def _minnow_in_front(self, slug: str, side: str, book):
        """The nearest small order sitting between the touch and our
        main order (in the way of its rewards): (price, size) or None."""
        bs, _ = self.earn(side)
        main = self._orders(slug, bs, decoy=False)
        if not main:
            return None
        cur = main[0].price
        tick = book.tick or 0.01
        mine = {round(o.price, 4): o.qty for o in self._orders(slug, bs)}
        for p, q in book.side(bs):
            ahead = (p < cur - tick / 2) if side == "YES" else (p > cur + tick / 2)
            if not ahead:
                break
            q_others = q - mine.get(round(p, 4), 0.0)
            if q_others <= 1e-9:
                continue
            return (p, q_others) if q_others <= MINNOW_MAX else None
        return None

    def _work_minnows(self, slug: str, side: str, positions: dict,
                      now: float) -> dict | None:
        """The dance (owner, 2026-09-02): a decoy JOINS the minnow in
        front of our order at its own price and waits DANCE_WAIT_S for
        it to move again. Each time the minnow moves the decoy joins it
        again and the clock restarts. When the minnow stays put for the
        wait, the decoy comes off and the minnow's shares are taken at
        its price. When the minnow moves more than DANCE_MAX_MOVES
        times, reaches the far touch, or crosses under our cost, it is
        taken at once. No minnow in front: no decoy, no dance."""
        book = self.fam.cache.fresh(slug, 120.0, now)
        if book is None:
            return None
        bs, intent = self.earn(side)
        tick = book.tick or 0.01
        decoys = self._orders(slug, bs, decoy=True)
        # the sniper works only where a bond sale of ours is resting
        # (owner, 2026-09-02) — no resting order, no minnow, no decoy
        minnow = (self._minnow_in_front(slug, side, book)
                  if self._orders(slug, bs, decoy=False) else None)
        if minnow is None:
            self._pull_decoys(slug, side)
            self.dance.pop(slug, None)
            return None
        if self._money() < MONEY_MIN_USD:
            return None                            # nothing to snap with
        m_px, m_q = minnow
        st = self.dance.get(slug)
        moved = st is not None and abs(m_px - st["px"]) > tick / 2
        moves = (st["moves"] + 1 if (st and moved) else (st["moves"] if st else 0))
        bound = self._bound(slug, side, tick)
        if side == "YES":
            at_far_touch = bool(book.bids) and m_px <= book.bids[0][0] + tick + 1e-9
            past_cost = bound > 0 and m_px < bound - 1e-9
        else:
            at_far_touch = bool(book.asks) and m_px >= book.asks[0][0] - tick - 1e-9
            past_cost = m_px > bound + 1e-9
        stayed = st is not None and not moved and now - st["since"] >= DANCE_WAIT_S
        why = ("moved more than %d times" % DANCE_MAX_MOVES if moves > DANCE_MAX_MOVES
               else "reached the far touch" if at_far_touch
               else "under our cost" if past_cost
               else "stayed put for the wait" if stayed else None)
        if why:
            self._log(event="dance_over", market=slug, why=why, moves=moves,
                      minnow_px=m_px)
            return self._snap(slug, side, book, m_px, m_q, positions, now)
        if st is not None and not moved:
            return None                            # the clock is running
        # (re)join the minnow at its price
        held = min(self.held(slug, side), self.exchange_held(slug, side, positions))
        qty = float(min(DECOY_QTY, math.floor(held)))
        if qty < 1.0:
            return None
        pos = float((positions.get(slug) or (0.0, 0.0))[0])
        if decoys:
            d = decoys[0]
            r = self.fam.desk.reprice(
                {"id": d.id, "market": slug, "side": bs, "price": d.price,
                 "size": d.qty, "intent": d.intent}, m_px, initiator="owner")
            if not (r.ok and r.order_id):
                return None
            if not r.two_orders:
                self.fam.orders.pop(d.id, None)
            qty = d.qty
        else:
            r = self.fam.desk.place_resting(slug, bs, m_px, qty, net_position=pos,
                                            initiator="owner", intent=intent)
            if not (r.ok and r.order_id):
                self._log(event="decoy_refused", market=slug, note=r.note[:120])
                return None
        self.fam.orders[r.order_id] = FamilyOrder(
            id=r.order_id, market=slug, side=bs, price=(r.price or m_px),
            qty=qty, intent=(r.intent or intent), placed_ts=now, purpose="bond",
            why=f"bond decoy: joined the {m_q:g} in front at {m_px * 100:g}c, "
                f"move {moves}")
        self.dance[slug] = {"px": m_px, "moves": moves, "since": round(now, 1)}
        self._log(event="decoy", market=slug, side=side, price=(r.price or m_px),
                  minnow_px=m_px, minnow_q=round(m_q, 1), moves=moves)
        return {"market": slug, "bond": side, "side": bs,
                "price": (r.price or m_px), "qty": qty, "decoy": True,
                "moves": moves}

    def _pull_decoys(self, slug: str, side: str) -> None:
        for d in self._orders(slug, self.earn(side)[0], decoy=True):
            r = self.fam.desk.cancel(d.id, slug, initiator="owner")
            if r.ok:
                self.fam.orders.pop(d.id, None)
                self._log(event="decoy_pulled", market=slug, price=d.price)

    def _snap(self, slug: str, side: str, book, px: float, size: float,
              positions: dict, now: float) -> dict | None:
        """Take the minnow's shares at its price: they join the bond."""
        cost = px if side == "YES" else round(1.0 - px, 4)
        money = self._money()
        qty = float(min(math.floor(money / cost) if cost > 0 else 0,
                        math.floor(size)))
        if qty < 1.0:
            return None
        bs, intent = self.entry(side)
        pos = float((positions.get(slug) or (0.0, 0.0))[0])
        r = self.fam.desk.place_resting(slug, bs, px, qty, net_position=pos,
                                        initiator="owner", intent=intent,
                                        taker="bond")
        if not (r.ok and r.order_id):
            self._log(event="snap_refused", market=slug, note=r.note[:120])
            return None
        # on the book as ours so the fill is journaled as a bond purchase
        self.fam.orders[r.order_id] = FamilyOrder(
            id=r.order_id, market=slug, side=bs, price=(r.price or px),
            qty=qty, intent=(r.intent or intent), placed_ts=now, purpose="bond",
            why=f"bond: took {qty:g} {side} at {px * 100:g}c — it was in the "
                f"way of our resting order")
        usd = round(qty * cost, 4)
        self._book_lot(slug, side, qty, usd)
        self._pay(usd)
        self._log(event="snapped", market=slug, side=side, price=px, qty=qty,
                  cost=round(usd, 2), cash=round(self.cash, 2),
                  budget=round(self.budget, 2))
        self._ping_maybe(usd)
        # the decoy has done its job
        self._pull_decoys(slug, side)
        self.dance.pop(slug, None)
        return {"market": slug, "bond": side, "side": bs, "price": px,
                "qty": qty, "taken": True, "usd": round(usd, 2)}

    def _take_price(self, side: str, book) -> tuple[float | None, float, float]:
        """(YES price to take, bond cost per dollar, size showing) for
        opening a bond of this side at the touch."""
        if side == "YES":
            if not book.asks:
                return None, 0.0, 0.0
            a, q = book.asks[0]
            return a, a, q
        if not book.bids:
            return None, 0.0, 0.0
        b, q = book.bids[0]
        return b, round(1.0 - b, 4), q

    def _probe(self, slug: str, book, prog, book_side: str, qty: float | None = None):
        from . import survey as sv
        pool = self.fam._side_pool(slug, prog)
        if pool is None:
            return None
        try:
            return sv.probe_side(book, prog, book_side, pool,
                                 **({"qty": qty} if qty else {}))
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ the page

    def view(self, now: float, positions: dict | None = None) -> dict:
        rows = []
        for slug, meta in self.approved.items():
            side = meta["side"]
            book = self.fam.cache.any_age(slug)
            bid = book.bids[0][0] if book is not None and book.bids else None
            ask = book.asks[0][0] if book is not None and book.asks else None
            px, cost, size = (self._take_price(side, book) if book is not None
                              else (None, 0.0, 0.0))
            held = self.held(slug, side)
            exch = self.exchange_held(slug, side, positions)
            days = slug_days_out(slug, now)
            ytr = ((1.0 - cost) / cost) if cost else None
            ann = (ytr * 365.0 / max(days, 1)) if (ytr is not None and days) else None
            prog = self.fam.terms.get(slug)
            ebs, _ = self.earn(side)
            earn = None
            if book is not None and prog is not None and prog.is_live():
                p = self._probe(slug, book, prog, ebs, qty=max(held, 1.0))
                if p is not None:
                    earn = {"est_day": round(p.est_day, 4),
                            "share": round(p.share, 4),
                            "qualifies": bool(p.qualifies), "note": p.note}
            minnow = self._minnow_in_front(slug, side, book) if book is not None else None
            rows.append({
                "market": slug, "bond": side, "odds": meta.get("odds"),
                "bid": bid, "ask": ask,
                "cost": (round(cost, 4) if cost else None),
                "size": round(size, 1),
                "days": days,
                "yield": (round(ytr, 4) if ytr is not None else None),
                "annual": (round(ann, 4) if ann is not None else None),
                "qty": round(held, 2),
                "cost_px": self.cost_basis(slug, side),
                "uncounted": round(max(exch - held, 0.0), 2),
                "earn": earn,
                "earn_order": ([{"price": o.price, "qty": o.qty,
                                 "est": round(o.live_est or 0.0, 4)}
                                for o in self._orders(slug, ebs, decoy=False)]
                               or None),
                "decoy": ([{"price": o.price, "qty": o.qty}
                           for o in self._orders(slug, ebs, decoy=True)] or None),
                "minnow": ({"price": minnow[0], "qty": round(minnow[1], 1)}
                           if minnow else None),
                "dance": self.dance.get(slug),
                "stale": (book is None or now - book.fetched_at > 600.0),
                "slot": self.slot.get(slug),
            })
        rows.sort(key=lambda r: (r["cost"] is None, r["cost"] or 1.0))
        proposed = [{"market": s, "odds": m.get("odds"), "bond": m.get("side"),
                     "since": m.get("since")}
                    for s, m in sorted(self.proposed.items(),
                                       key=lambda kv: -(kv[1].get("since") or 0))]
        dropped = [{"market": s, "odds": m.get("odds"), "bond": m.get("side"),
                    "ts": m.get("ts"), "by": m.get("by", "silver"),
                    "held": round(self.held(s, m.get("side") or "YES"), 2)}
                   for s, m in sorted(self.dropped.items(),
                                      key=lambda kv: -(kv[1].get("ts") or 0))[:10]]
        held_cost = round(sum(float(l.get("cost") or 0.0)
                              for l in self.lots.values()), 2)
        return {"rows": rows, "proposed": proposed, "dropped": dropped,
                "ignored": sorted(self.ignored),
                "cash": round(self.cash, 2),
                "budget": round(self.budget, 2), "spent": round(self.spent, 2),
                "unpinged": round(self.unpinged, 2),
                "held_cost": held_cost,
                "high": HIGH_ODDS, "low": LOW_ODDS, "price_cap": PRICE_CAP,
                "keep": KEEP_FRACTION,
                "scan_day": self.scan_day, "scan_hour_utc": SCAN_HOUR_UTC,
                "log": self.log[-12:]}

    # ------------------------------------------------------------ persistence

    def to_dict(self) -> dict:
        return {"approved": self.approved, "proposed": self.proposed,
                "ignored": self.ignored, "dropped": self.dropped,
                "lots": self.lots,
                "cash": round(self.cash, 4),
                "budget": round(self.budget, 4), "spent": round(self.spent, 4),
                "unpinged": round(self.unpinged, 4),
                "scan_day": self.scan_day,
                "earn_seen": self._earn_seen, "earn_px": self._earn_px,
                "exch_seen": getattr(self, "_exch_seen", {}),
                "slot": self.slot, "moved_at": self.moved_at,
                "dance": self.dance,
                "log": self.log[-LOG_KEEP:]}

    def restore(self, d: dict) -> None:
        self.approved = {str(k): dict(v) for k, v in (d.get("approved") or {}).items()}
        self.proposed = {str(k): dict(v) for k, v in (d.get("proposed") or {}).items()}
        self.ignored = {str(k): float(v) for k, v in (d.get("ignored") or {}).items()}
        self.dropped = {str(k): dict(v) for k, v in (d.get("dropped") or {}).items()}
        self.lots = {str(k): {"qty": float(v.get("qty") or 0.0),
                              "cost": float(v.get("cost") or 0.0)}
                     for k, v in (d.get("lots") or {}).items()}
        self.cash = float(d.get("cash") or 0.0)
        self.budget = float(d.get("budget") or 0.0)
        self.spent = float(d.get("spent") or 0.0)
        self.unpinged = float(d.get("unpinged") or 0.0)
        self.scan_day = str(d.get("scan_day") or "")
        self._earn_seen = {str(k): float(v) for k, v in (d.get("earn_seen") or {}).items()}
        self._earn_px = {str(k): float(v) for k, v in (d.get("earn_px") or {}).items()}
        self._exch_seen = {str(k): float(v) for k, v in (d.get("exch_seen") or {}).items()}
        self.slot = {str(k): dict(v) for k, v in (d.get("slot") or {}).items()}
        self.moved_at = {str(k): float(v) for k, v in (d.get("moved_at") or {}).items()}
        self.dance = {str(k): dict(v) for k, v in (d.get("dance") or {}).items()}
        self.log = list(d.get("log") or [])
        self._mark_engine()
