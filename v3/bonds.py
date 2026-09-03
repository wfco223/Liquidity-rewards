"""Bonds: the tax reserve earning like a bond (owner, 2026-09-02).

"Designate any politics market where the Nate Silver gives >99% odds of
an outcome as a bond market. Then on a separate page in the app, give
me a list of the cheapest bond positions with information on what a
resting sell order would earn. Before adding a new market to the bond
list ask me first. Once I'm in the positions they should automatically
[have] a sell order placed so that they are earning. Then when the
sale goes through I'll want the money reinvested in the cheapest
market where it can earn rewards."

And the corrections, same day:
- "Once per day in the night. No notification. When I go to the
  screen, any new markets can be up top for me to add. Automatically
  remove markets that fall outside the 99% range. Also include
  anything that has less than a 1% chance." — so both ends: a market
  Silver puts at 99%+ is a YES bond (buy YES at 98-99c); one at 1% or
  under is a NO bond (buy NO, which on this exchange is opening a
  short of YES at 1-2c). The list is checked once a night, silently.
- "The engine does not need to ignore these markets, only the orders
  I place... it can operate business as normal otherwise." — so no
  frozen ground. The engine keeps quoting bond markets; it just never
  touches a bond order, and never rests its own exits on bond stock.
- "The order could be very large >1000 shares. So we can afford to be
  patient... Minnows may move down to try and get more rewards, but
  then I can just snap them up." — so a bond ask is rested once and
  left alone; nothing chases the touch.
- "The goal would be to take a resting order for proceeds rather than
  place and potentially have that capital be used elsewhere." — so
  reinvestment LIFTS the touch (the second carved exception to
  post-only, owner's rail only, never past the touch, never more than
  it shows) instead of resting a bid that leaves cash for the engine's
  fills to eat.

What this is and is not. Bought at 98-99c a bond pays the last cent
or two at resolution — November 3 for most — and, held, it is not
buying power, so no engine fill can reach it. Bond-LIKE across many
such markets; on any one the price is the market's own odds that the
lot goes to zero, and the page says so on every row. Sale proceeds are
a ledger here, not the exchange's cash: a sale counts only when OUR
bond order gave up that much while the position moved, so the owner
selling by hand to pay the taxes is never reinvested behind his back.
Nothing here places an order unless the bonds switch on /switch is ON.
"""

from __future__ import annotations

import math
import time

from .family import FamilyOrder, slug_days_out
from .intents import BUY_LONG, BUY_SHORT, SELL_LONG, SELL_SHORT

HIGH_ODDS = 0.99            # YES bond: Silver's odds for YES at or above
LOW_ODDS = 0.01             # NO bond: Silver's odds for YES at or below
PRICE_CAP = 0.995           # never pay more than this per dollar of bond
SNIPE_MAX_DEFAULT = 0.985   # the owner's bar for a "great opportunity",
                            # per dollar of bond; he sets it from the page
REINVEST_MIN_USD = 5.0      # money below this waits
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
        self.alert = alert or (lambda title, msg: None)   # takes only —
                                                          # money moved
        # the owner's money, the sniper's way (owner, 2026-09-02: "I will
        # leave the money out of my account. When there is a great
        # opportunity the bond engine makes the purchase and I top off the
        # money for the engine"): a deploy BUDGET he sets and tops up, a
        # price bar for what counts as great, and what was spent since
        self.budget: float = 0.0
        self.snipe_max: float = SNIPE_MAX_DEFAULT
        self.spent: float = 0.0
        self.approved: dict[str, dict] = {}   # slug -> {added, odds, side}
        self.proposed: dict[str, dict] = {}   # slug -> {odds, side, since}
        self.ignored: dict[str, float] = {}   # slug -> ts
        self.dropped: dict[str, dict] = {}    # slug -> {odds, side, ts}
        self.cash: float = 0.0                # proceeds awaiting reinvestment
        self.pos_seen: dict[str, float] = {}
        self.side_seen: dict[str, str] = {}
        self.scan_day: str = ""
        self.log: list[dict] = []
        self._earn_seen: dict[str, float] = {}   # slug -> size our earn orders showed
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

    def _orders(self, slug: str, book_side: str | None = None) -> list[FamilyOrder]:
        return [o for o in list(self.fam.orders.values())
                if o.purpose == "bond" and o.market == slug
                and (book_side is None or o.side == book_side)]

    def held(self, slug: str, side: str, positions: dict | None = None) -> float:
        """Shares of this bond held: long YES for a YES bond, the short
        of YES for a NO bond."""
        if positions is not None and slug in positions:
            pos = float((positions.get(slug) or (0.0, 0.0))[0])
        else:
            pos = float((self.fam.inventory.get(slug) or {}).get("qty") or 0.0)
        return max(pos, 0.0) if side == "YES" else max(-pos, 0.0)

    def _yes_px(self, slug: str) -> float:
        """The YES price the position was done at, per share."""
        inv = self.fam.inventory.get(slug) or {}
        q = float(inv.get("qty") or 0.0)
        c = float(inv.get("cost") or 0.0)
        return abs(c / q) if abs(q) > 0.005 and c else 0.0

    def cost_basis(self, slug: str, side: str) -> float:
        """What a dollar of this bond cost: the YES price paid, or one
        minus the YES price the short was sold at."""
        px = self._yes_px(slug)
        if px <= 0:
            return 0.0
        return px if side == "YES" else round(1.0 - px, 4)

    @staticmethod
    def _snap_up(px: float, tick: float) -> float:
        return round(math.ceil(px / tick - 1e-9) * tick, 4)

    @staticmethod
    def _snap_down(px: float, tick: float) -> float:
        return round(math.floor(px / tick + 1e-9) * tick, 4)

    def _mark_engine(self) -> None:
        """The engine keeps quoting bond markets; it just never rests its
        own exits on bond stock or touches a bond order."""
        self.fam.bond_markets = set(self.approved) | set(self.dropped)

    # ------------------------------------------------------------ the list

    def scan(self, now: float, force: bool = False) -> list[str]:
        """Once a night (or on the page's button): Silver's odds propose
        new markets, and drop listed ones that left the band. Silent —
        the page shows what changed. Returns the newly proposed slugs."""
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
                    # outside the band now: off the list by itself (owner,
                    # 2026-09-02); a position still held keeps its bond
                    # order, and the engine still leaves that alone
                    self.dropped[slug] = {"odds": round(p, 4),
                                          "side": meta.get("side"),
                                          "ts": round(now, 1)}
                    for k in sorted(self.dropped, key=lambda k: self.dropped[k]["ts"])[:-DROPPED_KEEP]:
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
        self._mark_engine()
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
        self._mark_engine()
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
        self._mark_engine()
        self._log(event="removed", market=slug)
        return {"ok": True, "note": "removed — a bond order still resting "
                                    "stays yours; the engine leaves it alone"}

    def set_budget(self, amount: float) -> dict:
        """The money the sniper may deploy before the owner tops up.
        Setting it resets 'spent since'."""
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return {"ok": False, "note": "budget must be a number"}
        if amount < 0 or amount > 100000:
            return {"ok": False, "note": "budget must be $0 to $100,000"}
        self.budget = round(amount, 2)
        self.spent = 0.0
        self._log(event="budget_set", usd=self.budget)
        return {"ok": True, "note": f"deploy budget set to ${self.budget:,.2f} "
                                    f"— the engine takes great prices up "
                                    f"to that, then waits for a top-up"}

    def set_max(self, px: float) -> dict:
        """The bar for a great price, per dollar of bond."""
        try:
            px = float(px)
        except (TypeError, ValueError):
            return {"ok": False, "note": "price must be a number"}
        if not (0.5 <= px <= PRICE_CAP):
            return {"ok": False,
                    "note": f"price must be 50c to {PRICE_CAP * 100:g}c"}
        self.snipe_max = round(px, 4)
        self._log(event="max_set", price=self.snipe_max)
        return {"ok": True, "note": f"takes anything at {px * 100:g}c or "
                                    f"better per dollar of bond"}

    # ------------------------------------------------------------ the money

    def cycle(self, now: float, positions: dict, on: bool) -> dict:
        """Once a cycle, after the family has run: count sales, give
        every held bond its resting order, reinvest proceeds. Places
        nothing unless the bonds switch is on."""
        self.scan(now)
        self._mark_engine()
        placed: list[dict] = []
        for slug in sorted(set(self.approved) | set(self.pos_seen)):
            side = (self.approved.get(slug) or {}).get("side") or self.side_seen.get(slug)
            if not side:
                continue
            self.side_seen[slug] = side
            now_h = self.held(slug, side, positions)
            prev = self.pos_seen.get(slug)
            if prev is not None and now_h < prev - 0.005:
                gone = prev - now_h
                gave = self._earn_gave(slug, side, gone)
                if gave > 0.005:
                    px = self._earn_px.get(slug) or self._yes_px(slug)
                    proceeds = gave * (px if side == "YES" else (1.0 - px))
                    self.cash = round(self.cash + proceeds, 4)
                    self._log(event="sold", market=slug, side=side,
                              qty=round(gave, 2), price=px,
                              proceeds=round(proceeds, 2),
                              cash=round(self.cash, 2))
            self.pos_seen[slug] = now_h
        if on:
            for slug in sorted(self.approved):
                side = self.approved[slug]["side"]
                r = self._keep_earning(slug, side, positions, now)
                if r:
                    placed.append(r)
            r = self._reinvest(now, positions)
            if r:
                placed.append(r)
        for slug in set(self.approved) | set(self.pos_seen):
            side = self.side_seen.get(slug) or (self.approved.get(slug) or {}).get("side")
            if side:
                self._remember_earn(slug, side)
        return {"placed": placed, "cash": round(self.cash, 2)}

    def _remember_earn(self, slug: str, side: str) -> None:
        bs, _ = self.earn(side)
        orders = self._orders(slug, bs)
        self._earn_seen[slug] = sum(o.qty for o in orders)
        if orders:
            self._earn_px[slug] = orders[0].price

    def _earn_gave(self, slug: str, side: str, gone: float) -> float:
        bs, _ = self.earn(side)
        seen = self._earn_seen.get(slug, 0.0)
        now_q = sum(o.qty for o in self._orders(slug, bs))
        return min(max(seen - now_q, 0.0), gone)

    def _keep_earning(self, slug: str, side: str, positions: dict,
                      now: float) -> dict | None:
        """A held bond gets ONE resting order and keeps it. YES: an ask
        at the touch or at cost, whichever is higher. NO: a cover bid at
        the touch or at the price the short was sold, whichever is
        lower. Nothing chases the touch afterwards (owner: "we can
        afford to be patient")."""
        held = self.held(slug, side, positions)
        if held < 1.0:
            return None
        bs, intent = self.earn(side)
        resting = sum(o.qty for o in self._orders(slug, bs))
        qty = float(math.floor(held - resting))
        if qty < 1.0:
            return None
        book = self.fam.cache.fresh(slug, 120.0, now)
        if book is None:
            return None
        tick = book.tick or 0.01
        px_done = self._yes_px(slug)
        if side == "YES":
            floor = self._snap_up(px_done, tick) if px_done > 0 else 0.0
            touch = book.asks[0][0] if book.asks else 0.99
            want = min(max(touch, floor), 0.999)
            if book.bids and want <= book.bids[0][0] + 1e-9:
                want = self._snap_up(book.bids[0][0] + tick, tick)
        else:
            cap = self._snap_down(px_done, tick) if px_done > 0 else 0.999
            touch = book.bids[0][0] if book.bids else 0.01
            want = max(min(touch, cap), 0.001)
            if book.asks and want >= book.asks[0][0] - 1e-9:
                want = self._snap_down(book.asks[0][0] - tick, tick)
        pos = float((positions.get(slug) or (0.0, 0.0))[0])
        r = self.fam.desk.place_resting(slug, bs, want, qty, net_position=pos,
                                        initiator="owner", intent=intent)
        if not (r.ok and r.order_id):
            self._log(event="earn_refused", market=slug, note=r.note[:120])
            return None
        self.fam.orders[r.order_id] = FamilyOrder(
            id=r.order_id, market=slug, side=bs, price=(r.price or want),
            qty=qty, intent=r.intent or intent, placed_ts=now, purpose="bond",
            why=("bond: the resting ask earns while it waits" if side == "YES"
                 else "bond: the resting cover bid earns while it waits"))
        self._log(event="earn_rested", market=slug, side=side,
                  price=(r.price or want), qty=qty)
        return {"market": slug, "bond": side, "side": bs,
                "price": (r.price or want), "qty": qty}

    def _take_price(self, side: str, book) -> tuple[float | None, float, float]:
        """(YES price to take, bond cost per dollar, size showing) for
        opening a bond of this side right now."""
        if side == "YES":
            if not book.asks:
                return None, 0.0, 0.0
            a, q = book.asks[0]
            return a, a, q
        if not book.bids:
            return None, 0.0, 0.0
        b, q = book.bids[0]
        return b, round(1.0 - b, 4), q

    def _reinvest(self, now: float, positions: dict) -> dict | None:
        """The sniper (owner, 2026-09-02). Money = sale proceeds plus the
        deploy budget he tops up. When a listed market shows a GREAT
        price — at or under his bar, per dollar of bond — it LIFTS that
        touch: a limit at the touch, never past it, never more than it
        shows, cheapest market first, one take a cycle. The rewards-
        seeking shares others rest there become his bond, and his own
        resting order has one competitor fewer. Proceeds are spent
        before the budget; a take pings the phone so he can top up."""
        money = self.cash + self.budget
        if money < REINVEST_MIN_USD:
            return None
        best = None
        for slug, meta in self.approved.items():
            side = meta["side"]
            book = self.fam.cache.fresh(slug, 120.0, now)
            if book is None:
                continue
            px, cost, size = self._take_price(side, book)
            if px is None or cost <= 0 or size < 1.0:
                continue
            if cost > min(self.snipe_max, PRICE_CAP) + 1e-9:
                continue                    # not a great price
            prog = self.fam.terms.get(slug)
            if prog is None or not prog.is_live():
                continue
            ebs, _ = self.earn(side)
            probe = self._probe(slug, book, prog, ebs)
            if probe is None or not probe.qualifies or probe.est_day <= 0:
                continue
            if best is None or cost < best[0]:
                best = (cost, slug, side, px, size, book)
        if best is None:
            return None
        cost, slug, side, px, size, book = best
        qty = float(min(math.floor(money / cost), math.floor(size)))
        if qty < 1.0:
            return None
        bs, intent = self.entry(side)
        pos = float((positions.get(slug) or (0.0, 0.0))[0])
        r = self.fam.desk.place_resting(slug, bs, px, qty, net_position=pos,
                                        initiator="owner", intent=intent,
                                        taker="bond")
        if not (r.ok and r.order_id):
            self._log(event="take_refused", market=slug, note=r.note[:120])
            return None
        self.fam.orders[r.order_id] = FamilyOrder(
            id=r.order_id, market=slug, side=bs, price=(r.price or px),
            qty=qty, intent=r.intent or intent, placed_ts=now, purpose="bond",
            why=f"bond: took the {side} at the touch — a great price")
        usd = round(qty * cost, 4)
        from_cash = min(self.cash, usd)
        self.cash = round(self.cash - from_cash, 4)
        from_budget = round(usd - from_cash, 4)
        self.budget = round(max(self.budget - from_budget, 0.0), 4)
        self.spent = round(self.spent + from_budget, 4)
        self._log(event="took", market=slug, side=side, price=(r.price or px),
                  qty=qty, cost=round(usd, 2), cash=round(self.cash, 2),
                  budget=round(self.budget, 2))
        if from_budget > 0.005:
            self.alert("Bond engine bought",
                       f"{qty:g} {side} at {(r.price or px) * 100:g}c in "
                       f"{slug[:40]} — ${usd:,.2f}; ${self.budget:,.2f} of "
                       f"the deploy budget left")
        return {"market": slug, "bond": side, "side": bs,
                "price": (r.price or px), "qty": qty, "taken": True,
                "usd": round(usd, 2)}

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
            held = self.held(slug, side, positions)
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
            rows.append({
                "market": slug, "bond": side, "odds": meta.get("odds"),
                "bid": bid, "ask": ask,
                "cost": (round(cost, 4) if cost else None),
                "size": round(size, 1),
                "days": days,
                "yield": (round(ytr, 4) if ytr is not None else None),
                "annual": (round(ann, 4) if ann is not None else None),
                "qty": round(held, 2),
                "cost_px": round(self.cost_basis(slug, side), 4),
                "earn": earn,
                "earn_order": ([{"price": o.price, "qty": o.qty,
                                 "est": round(o.live_est or 0.0, 4)}
                                for o in self._orders(slug, ebs)] or None),
                "entry_order": ([{"price": o.price, "qty": o.qty}
                                 for o in self._orders(slug, self.entry(side)[0])]
                                or None),
                "stale": (book is None or now - book.fetched_at > 600.0),
            })
        rows.sort(key=lambda r: (r["cost"] is None, r["cost"] or 1.0))
        proposed = [{"market": s, "odds": m.get("odds"), "bond": m.get("side"),
                     "since": m.get("since")}
                    for s, m in sorted(self.proposed.items(),
                                       key=lambda kv: -(kv[1].get("since") or 0))]
        dropped = [{"market": s, "odds": m.get("odds"), "bond": m.get("side"),
                    "ts": m.get("ts"), "by": m.get("by", "silver"),
                    "held": round(self.held(s, m.get("side") or "YES", positions), 2)}
                   for s, m in sorted(self.dropped.items(),
                                      key=lambda kv: -(kv[1].get("ts") or 0))[:10]]
        held_cost = sum(r["qty"] * (r["cost_px"] or 0) for r in rows)
        return {"rows": rows, "proposed": proposed, "dropped": dropped,
                "ignored": sorted(self.ignored),
                "cash": round(self.cash, 2),
                "budget": round(self.budget, 2), "spent": round(self.spent, 2),
                "snipe_max": self.snipe_max,
                "held_cost": round(held_cost, 2),
                "high": HIGH_ODDS, "low": LOW_ODDS, "price_cap": PRICE_CAP,
                "scan_day": self.scan_day, "scan_hour_utc": SCAN_HOUR_UTC,
                "log": self.log[-12:]}

    # ------------------------------------------------------------ persistence

    def to_dict(self) -> dict:
        return {"approved": self.approved, "proposed": self.proposed,
                "ignored": self.ignored, "dropped": self.dropped,
                "cash": round(self.cash, 4),
                "budget": round(self.budget, 4), "spent": round(self.spent, 4),
                "snipe_max": self.snipe_max,
                "pos_seen": self.pos_seen, "side_seen": self.side_seen,
                "scan_day": self.scan_day,
                "earn_seen": self._earn_seen, "earn_px": self._earn_px,
                "log": self.log[-LOG_KEEP:]}

    def restore(self, d: dict) -> None:
        self.approved = {str(k): dict(v) for k, v in (d.get("approved") or {}).items()}
        self.proposed = {str(k): dict(v) for k, v in (d.get("proposed") or {}).items()}
        self.ignored = {str(k): float(v) for k, v in (d.get("ignored") or {}).items()}
        self.dropped = {str(k): dict(v) for k, v in (d.get("dropped") or {}).items()}
        self.cash = float(d.get("cash") or 0.0)
        self.budget = float(d.get("budget") or 0.0)
        self.spent = float(d.get("spent") or 0.0)
        self.snipe_max = float(d.get("snipe_max") or SNIPE_MAX_DEFAULT)
        self.pos_seen = {str(k): float(v) for k, v in (d.get("pos_seen") or {}).items()}
        self.side_seen = {str(k): str(v) for k, v in (d.get("side_seen") or {}).items()}
        self.scan_day = str(d.get("scan_day") or "")
        self._earn_seen = {str(k): float(v) for k, v in (d.get("earn_seen") or {}).items()}
        self._earn_px = {str(k): float(v) for k, v in (d.get("earn_px") or {}).items()}
        self.log = list(d.get("log") or [])
        self._mark_engine()
