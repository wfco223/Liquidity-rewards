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
  money for the engine." A deploy budget; a ping only after $100 of
  purchases. Since 2026-09-03 the budget follows what he owes in taxes
  (the pay page's 22% of everything paid), less what this engine has
  spent of it, unless he types a fixed figure.
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
ENTER_MAX_LEVELS = 20       # the owner's entry sweeps at most this many levels
LADDER_SHOW = 8             # entry-side levels the page shows to enter at
SHARE_KEEP_DAYS = 7         # a day's reward split is held this long, then booked
FILL_WAIT_S = 8.0           # how long a take's fill is awaited in the trade record
TRIM_GRACE_S = 300.0        # a fresh lot gets this long for the position feed to show it
HOLD_ENGINE_S = 600.0       # after clearing our orders out of a take's way, the engine waits this long
CLEAR_WAIT_S = 5.0          # how long a cleared order gets to leave the open-order list
RECONFIRM_S = 3600.0        # a booked fill is re-checked against the record this long
MORE_SHARE = 0.30           # the buy-more order rests only where it captures this much of its side
DOTS_KEEP = 2880            # bond earning-rate samples kept for the graph


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
    def __init__(self, fam, client, fair, clock=None, alert=None, tax_owed=None,
                 paid=None, sleep=None):
        self.fam = fam                  # the politics family
        self.client = client
        self.fair = fair                # slug -> Silver's YES odds, or None
        self._clock = clock or time.time
        self._sleep = sleep or time.sleep
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
        # the budget follows what he owes in taxes (owner, 2026-09-03:
        # "set the budget to whatever I currently owe in taxes") unless
        # he types a fixed figure; tax_owed() -> {owed, gross, rate} or None
        self.budget_mode: str = "tax"
        self.tax_owed = tax_owed or (lambda: None)
        # earnings (owner, 2026-09-03: "the overall earnings from the
        # bonds which is the profit from sales and the liquidity rewards
        # payments ... differentiate the liquidity payments from engine
        # resting orders"). Rewards are paid per MARKET, and the engine
        # quotes bond markets too, so each cycle samples the bond orders'
        # share of what all our orders in the market are measured to
        # earn; a day's payment splits by the day's average share.
        self.paid = paid or (lambda day, slug: None)   # (day, slug) -> usd paid
        self.realized: float = 0.0            # profit on sales by our orders
        self.sold_usd: float = 0.0
        self.share_day: dict[str, dict[str, list]] = {}   # day -> slug -> [sum, n]
        self.rewards_booked: float = 0.0      # bond share of days folded away
        self.engine_booked: float = 0.0       # the engine's share, same days
        self.paid_booked: float = 0.0
        self.rewards_by_market: dict[str, float] = {}
        # the exchange is the source of truth for holdings (owner,
        # 2026-09-03: "nothing should be making up holdings or
        # transactions"): a take books only what the trade record shows
        # filled under its order id, and the ledger is trimmed to the
        # position feed whenever the feed shows less
        self.lot_ts: dict[str, float] = {}     # slug -> last booking time
        self.exch_max: dict[str, float] = {}   # slug -> most the exchange ever showed
        # every booked fill by order id, so it can be re-checked against
        # the record for an hour (the 2026-09-03 Hawaii take: five
        # executions read as one — the ledger said 1, the exchange 5)
        self.fill_book: dict[str, dict] = {}   # oid -> {slug, side, qty, px, ts, open}
        # buying more (owner, 2026-09-03): a second resting order on the
        # thin side buys more bond, up to an amount he sets per market —
        # defaulting to his first purchase there — at the cheapest price
        # that still captures MORE_SHARE of its side's earnings, else
        # not at all; it moves when it no longer captures that, and is
        # pulled when no price inside the cap can. Reset when the market
        # is no longer held.
        self.more_cap: dict[str, dict] = {}    # slug -> {usd, by, first}
        self.moved_more_at: dict[str, float] = {}
        self._more_note: dict[str, str] = {}
        self.dots: list = []                   # [ts, $/day of every bond order] for the graph
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
        """What a share of this bond cost, commissions in, from the
        ledger — the figure shown and the one profit is measured from."""
        lot = self.lots.get(slug) or {}
        q = abs(float(lot.get("qty") or 0.0))
        c = float(lot.get("cost") or 0.0) + float(lot.get("fees") or 0.0)
        return round(c / q, 4) if q > 0.005 and c > 0 else 0.0

    def price_basis(self, slug: str, side: str) -> float:
        """The average price paid per share, commissions out: the
        exit's floor (holding at cost is fine)."""
        lot = self.lots.get(slug) or {}
        q = abs(float(lot.get("qty") or 0.0))
        c = float(lot.get("cost") or 0.0)
        return round(c / q, 4) if q > 0.005 and c > 0 else 0.0

    def _yes_px_floor(self, slug: str, side: str) -> float:
        """The YES price our earning order must not cross: what a share
        cost (YES) or fetched (NO), the price paid, from the ledger."""
        cb = self.price_basis(slug, side)
        if cb <= 0:
            return 0.0
        return cb if side == "YES" else round(1.0 - cb, 4)

    @staticmethod
    def _snap_up(px: float, tick: float) -> float:
        return round(math.ceil(px / tick - 1e-9) * tick, 4)

    @staticmethod
    def _snap_down(px: float, tick: float) -> float:
        return round(math.floor(px / tick + 1e-9) * tick, 4)

    def _own_at(self, slug: str, book_side: str, px: float) -> float:
        """Shares of OURS resting at a level — engine, hand and bond
        alike. We cannot buy our own orders (owner, 2026-09-02)."""
        return sum(o.qty for o in list(self.fam.orders.values())
                   if o.market == slug and o.side == book_side
                   and abs(o.price - px) < 1e-9)

    def _others(self, slug: str, book_side: str, book) -> list:
        """The far side's levels net of our own orders, best first."""
        out = []
        for p, q in book.side(book_side):
            avail = q - self._own_at(slug, book_side, p)
            if avail > 1e-9:
                out.append((p, avail))
        return out

    def _mark_engine(self) -> None:
        """The engine keeps quoting bond markets; it just never rests its
        own exits on the BOND shares (and never touches a bond order)."""
        self.fam.bond_qty = {s: float(l.get("qty") or 0.0)
                             for s, l in self.lots.items()
                             if abs(float(l.get("qty") or 0.0)) > 0.005}

    def _book_lot(self, slug: str, side: str, qty: float, usd: float,
                  ref: str = "", fee: float = 0.0) -> None:
        """A lot is booked only from something the exchange confirmed —
        `ref` is the filled order's id, or "adopt" for shares the owner
        counted in himself. A lot with no such backing is dropped at
        restore (the Hawaii case, 2026-09-03)."""
        self.lot_ts[slug] = self._clock()
        fresh = slug not in self.lots
        lot = self.lots.setdefault(slug, {"qty": 0.0, "cost": 0.0, "fills": [], "fees": 0.0})
        lot["fees"] = round(float(lot.get("fees") or 0.0) + fee, 4)
        if fresh and slug not in self.more_cap:
            # his first purchase here is the default amount for buying
            # more, and its price is the most the buy-more order ever
            # pays (owner, 2026-09-03: "the buy price cap is also the
            # price I originally bought at. Not 99.5. I never want to
            # buy that high"). Kept in YES terms.
            per = usd / qty if qty > 0.005 else 0.0
            px0 = per if side == "YES" else round(1.0 - per, 4)
            self.more_cap[slug] = {"usd": round(usd, 2), "by": "default",
                                   "first": str(ref or "adopt"),
                                   "px": round(px0, 4)}
        lot["qty"] = round(lot["qty"] + (qty if side == "YES" else -qty), 4)
        lot["cost"] = round(lot["cost"] + usd, 4)
        if ref:
            lot.setdefault("fills", []).append(str(ref))
            del lot["fills"][:-50]
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
        fee = round(float(lot.get("fees") or 0.0) * take / q, 4)
        lot["qty"] = round(lot["qty"] - (take if side == "YES" else -take), 4)
        lot["cost"] = round(lot["cost"] - usd, 4)
        lot["fees"] = round(float(lot.get("fees") or 0.0) - fee, 4)
        usd = round(usd + fee, 4)
        if abs(lot["qty"]) < 0.005:
            self.lots.pop(slug, None)
        self._mark_engine()
        return usd

    def _money(self) -> float:
        self._follow_tax()
        return self.cash + self.budget

    def _follow_tax(self) -> None:
        """In tax mode the budget is what he owes, less what this engine
        has spent of it since the mode began."""
        if self.budget_mode != "tax":
            return
        t = self.tax_owed()
        if not t:
            return
        owed = float(t.get("owed") or 0.0)
        self.budget = round(max(owed - self.spent, 0.0), 4)

    def _budget_now(self) -> float:
        self._follow_tax()
        return self.budget

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

    def _reconfirm(self, now: float) -> None:
        """Fills booked within RECONFIRM_S are read again from the trade
        record; a lot is corrected to the exchange's running total for
        that order, up or down, at its average price. The record is the
        truth; a first read can be early."""
        for oid, f in self.fill_book.items():
            if f.get("open") and oid not in self.fam.orders:
                f["open"] = False               # gone: one last hour of checks
                f["ts"] = round(now, 1)
        due = {oid: f for oid, f in self.fill_book.items()
               if f.get("open") or now - float(f.get("ts") or 0.0) <= RECONFIRM_S}
        if not due:
            return
        try:
            acts = self.client.recent_trades(limit=200)
        except Exception:  # noqa: BLE001 — try again next cycle
            return
        for oid, f in due.items():
            got = self._record_of(oid, acts)
            if got is None:
                continue
            shares, avg, _ = got
            booked = float(f.get("qty") or 0.0)
            fee = self._fee_of(oid, acts)
            fee_booked = float(f.get("fee") or 0.0)
            if abs(shares - booked) < 0.005 and abs(fee - fee_booked) < 0.005:
                continue
            slug, side = f["slug"], f["side"]
            px = avg or float(f.get("px") or 0.0)
            cost_per = px if side == "YES" else round(1.0 - px, 4)
            f["fee"] = fee
            if abs(shares - booked) < 0.005:
                # only the fee changed
                lot = self.lots.get(slug)
                if lot is not None:
                    lot["fees"] = round(float(lot.get("fees") or 0.0) + fee - fee_booked, 4)
                    if fee > fee_booked:
                        self._pay(round(fee - fee_booked, 4))
                continue
            if shares > booked:
                extra = round(shares - booked, 4)
                usd = round(extra * cost_per, 4)
                self._book_lot(slug, side, extra, usd, fee=round(fee - fee_booked, 4))
                usd = round(usd + (fee - fee_booked), 4)
                self._pay(usd)
                self._ping_maybe(usd)
            else:
                gone = round(booked - shares, 4)
                cost = self._unbook_lot(slug, side, gone)
                self.spent = round(max(self.spent - cost, 0.0), 4)
                if self.budget_mode != "tax":
                    self.budget = round(self.budget + cost, 4)
                self._follow_tax()
            f["qty"] = shares
            f["px"] = px
            self._log(event=("bought_more" if f.get("open") else "fill_corrected"),
                      market=slug, side=side, order_id=oid, qty=shares, price=px,
                      note=f"the record shows {shares:g} filled on this order; "
                           f"the ledger had {booked:g}")

    def _trim_to_exchange(self, positions: dict | None, now: float) -> None:
        """The position feed is the truth for holdings: a lot the feed
        shows less of is cut to what it shows. Shares the exchange NEVER
        showed were never bought, so their cost goes back to the money;
        shares it once showed and now does not were sold by hand, and
        that money is his, not the bond ledger's. A fresh lot gets
        TRIM_GRACE_S for the feed to catch up, and an empty feed (no
        positions at all while the engine holds stock) is not believed."""
        if not positions:
            return
        for slug in list(self.lots):
            lot = self.lots.get(slug) or {}
            q = float(lot.get("qty") or 0.0)
            side = "YES" if q >= 0 else "NO"
            ledger = abs(q)
            exch = self.exchange_held(slug, side, positions)
            before = self.exch_max.get(slug, 0.0)
            self.exch_max[slug] = max(before, exch)
            if ledger <= 0.005 or exch + 0.005 >= ledger:
                continue
            if now - self.lot_ts.get(slug, 0.0) < TRIM_GRACE_S:
                continue
            removed = round(ledger - exch, 4)
            never_seen = round(max(ledger - max(before, exch), 0.0), 4)
            cost = self._unbook_lot(slug, side, removed)
            refund = round(cost * min(never_seen / removed, 1.0), 4) if removed > 0 else 0.0
            if refund > 0:
                self.spent = round(max(self.spent - refund, 0.0), 4)
                if self.budget_mode != "tax":
                    self.budget = round(self.budget + refund, 4)
            self._follow_tax()
            self._log(event="trimmed_to_exchange", market=slug, side=side,
                      qty=removed, cost=round(cost, 2), refund=round(refund, 2),
                      note=f"the exchange shows {exch:g}, the ledger had "
                           f"{ledger:g}; {never_seen:g} of those it never "
                           f"showed at all")
        self._mark_engine()

    # ------------------------------------------------------------ earnings

    @staticmethod
    def _day(ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(ts))

    def _sample_share(self, now: float) -> None:
        """Once a cycle: in each bond market, the bond orders' share of
        what ALL our resting orders there are measured to earn (the
        family's live $/day per order; by size when nothing measures)."""
        day = self._day(now)
        for slug in set(self.approved) | set(self.lots):
            ours = [o for o in list(self.fam.orders.values()) if o.market == slug]
            if not ours:
                continue
            bond = [o for o in ours if o.purpose == "bond"]
            tot = sum(o.live_est or 0.0 for o in ours)
            if tot > 1e-9:
                share = sum(o.live_est or 0.0 for o in bond) / tot
            else:
                tq = sum(o.qty for o in ours)
                share = sum(o.qty for o in bond) / tq if tq > 1e-9 else 0.0
            rec = self.share_day.setdefault(day, {}).setdefault(slug, [0.0, 0])
            rec[0] += share
            rec[1] += 1

    def _attributed(self) -> tuple[dict, float, float, float]:
        """(bond rewards by market, bond total, engine total, paid total)
        over the days still held, from the exchange's own payments."""
        by: dict[str, float] = {}
        bond = eng = paid = 0.0
        for day, mk in self.share_day.items():
            for slug, (ssum, n) in mk.items():
                p = self.paid(day, slug)
                if p is None or n <= 0:
                    continue
                sh = ssum / n
                by[slug] = by.get(slug, 0.0) + p * sh
                bond += p * sh
                eng += p * (1.0 - sh)
                paid += p
        return by, bond, eng, paid

    def _fold_rewards(self, now: float) -> None:
        """Days older than SHARE_KEEP_DAYS are booked at what the
        exchange has paid by then and dropped."""
        cut = self._day(now - SHARE_KEEP_DAYS * 86400)
        for day in [d for d in self.share_day if d < cut]:
            for slug, (ssum, n) in self.share_day[day].items():
                p = self.paid(day, slug)
                if p is None or n <= 0:
                    continue
                sh = ssum / n
                self.rewards_booked = round(self.rewards_booked + p * sh, 4)
                self.engine_booked = round(self.engine_booked + p * (1.0 - sh), 4)
                self.paid_booked = round(self.paid_booked + p, 4)
                self.rewards_by_market[slug] = round(
                    self.rewards_by_market.get(slug, 0.0) + p * sh, 4)
            del self.share_day[day]

    def _earned(self) -> dict:
        _, bond, eng, paid = self._attributed()
        rewards = self.rewards_booked + bond
        return {"total": round(self.realized + rewards, 2),
                "sales": round(self.realized, 2),
                "sold_usd": round(self.sold_usd, 2),
                "rewards": round(rewards, 2),
                "engine": round(self.engine_booked + eng, 2),
                "paid": round(self.paid_booked + paid, 2)}

    def _market_rewards(self, slug: str) -> float:
        by, *_ = self._attributed()
        return self.rewards_by_market.get(slug, 0.0) + by.get(slug, 0.0)

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
        self._book_lot(slug, side, qty, round(qty * per, 4), ref="adopt")
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
        self.budget_mode = "fixed"
        self._log(event="budget_set", usd=self.budget)
        return {"ok": True, "note": f"deploy budget set to ${self.budget:,.2f}"}

    def set_more_cap(self, slug: str, amount) -> dict:
        """The most the buy-more order may rest for in this market, in
        dollars of bond. Set by him; the default is his first purchase."""
        if slug not in self.approved:
            return {"ok": False, "note": "not on the bond list"}
        try:
            usd = float(amount)
        except (TypeError, ValueError):
            return {"ok": False, "note": "dollars, please"}
        if usd < 0 or usd > 100000:
            return {"ok": False, "note": "$0 to $100,000"}
        cur = self.more_cap.get(slug) or {}
        self.more_cap[slug] = {"usd": round(usd, 2), "by": "owner",
                               "first": cur.get("first", ""),
                               "px": cur.get("px", self._first_px(slug))}
        self._log(event="more_cap_set", market=slug, usd=round(usd, 2))
        return {"ok": True, "note": f"buying more here up to ${usd:,.2f}"}

    def follow_tax(self) -> dict:
        """The budget goes back to following what he owes in taxes."""
        self.budget_mode = "tax"
        self.spent = 0.0
        self._follow_tax()
        self._log(event="budget_follows_tax", usd=self.budget)
        return {"ok": True, "note": f"budget follows taxes owed: ${self.budget:,.2f}"}

    # ------------------------------------------------------------ the money

    def cycle(self, now: float, positions: dict, on: bool) -> dict:
        """Once a cycle, after the family has run: count sales, keep
        every held bond earning, work the minnows, enter new ground.
        Places nothing unless the bonds switch is on."""
        self.scan(now)
        self._follow_tax()
        self._fold_rewards(now)
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
                    self.realized = round(self.realized + proceeds - cost, 4)
                    self.sold_usd = round(self.sold_usd + proceeds, 4)
                    self._log(event="sold", market=slug, side=side,
                              qty=round(sold, 2), price=px,
                              proceeds=round(proceeds, 2),
                              gain=round(proceeds - cost, 2),
                              cash=round(self.cash, 2))
        self._reconfirm(now)
        self._trim_to_exchange(positions, now)
        for s in list(self.more_cap):
            if abs(float((self.lots.get(s) or {}).get("qty") or 0.0)) < 0.005:
                self.more_cap.pop(s, None)       # no longer held: the default resets
        if on:
            for slug in sorted(self.approved):
                side = self.approved[slug]["side"]
                r = self._keep_earning(slug, side, positions, now)
                if r:
                    placed.append(r)
                r = self._work_minnows(slug, side, positions, now)
                if r:
                    placed.append(r)
                r = self._keep_buying(slug, side, positions, now)
                if r:
                    placed.append(r)
        rate = sum(o.live_est or 0.0 for o in list(self.fam.orders.values())
                   if o.purpose == "bond")
        self.dots.append([round(now, 1), round(rate, 2)])
        del self.dots[:-DOTS_KEEP]
        self._sample_share(now)
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
            # nothing earns anywhere (a wall at the touch): sit at the
            # FARTHEST slot, the most profitable price, not at the touch
            # — the Tennessee lesson (2026-09-03): an exit resting at
            # the touch AT COST filled in 13 minutes for a gain of $0
            px, est, ticks = scored[-1]
            return px, est, ticks, 1.0
        for px, est, ticks in reversed(scored):
            if est >= KEEP_FRACTION * best - 1e-12:
                return px, est, ticks, est / best
        px, est, ticks = scored[0]
        return px, est, ticks, 1.0

    def _bound(self, slug: str, side: str, tick: float) -> float:
        """The YES price the exit may not cross: the price paid, on the
        tick. Holding at cost is fine (owner, 2026-09-03: "never mind on
        the new price floor. We may not be able to earn rewards that
        way. Holding at cost is fine"); the commission a take paid is
        in the cost SHOWN and in the profit math, not in this floor."""
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
        bs, intent = self.earn(side)
        if self.held(slug, side) < 1.0:
            # nothing in the ledger: an earn order or decoy of ours still
            # resting here is stale (a lot trimmed away) — it comes off
            for o in self._orders(slug, bs):
                r = self.fam.desk.cancel(o.id, slug, initiator="owner")
                if r.ok:
                    self.fam.orders.pop(o.id, None)
                    self._log(event="earn_pulled", market=slug, price=o.price,
                              qty=o.qty, note="no bond shares held here")
            return None
        held = min(self.held(slug, side),
                   self.exchange_held(slug, side, positions))
        if held < 1.0:
            return None
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

    # -- buying more ---------------------------------------------------------

    def _first_px(self, slug: str) -> float:
        """The YES price of his original purchase here — the buy-more
        order's price cap. The average price paid stands in for a lot
        booked before the price was kept."""
        cap = self.more_cap.get(slug) or {}
        if cap.get("px"):
            return float(cap["px"])
        side = (self.approved.get(slug) or {}).get("side") or (
            "YES" if float((self.lots.get(slug) or {}).get("qty") or 0) >= 0 else "NO")
        pb = self.price_basis(slug, side)
        if pb <= 0:
            return 0.0
        return pb if side == "YES" else round(1.0 - pb, 4)

    def _more_orders(self, slug: str) -> list[FamilyOrder]:
        return [o for o in list(self.fam.orders.values())
                if o.purpose == "bond" and o.market == slug
                and str(o.why or "").startswith("bond more")]

    def _share_at(self, slug: str, far: str, book, px: float,
                  qty: float) -> tuple[float, float]:
        """(share of the side's score, $/day) for `qty` of ours resting
        at `px` on `far`, on this book net of every order of ours."""
        from .scoring import estimate_join
        prog = self.fam.terms.get(slug)
        if prog is None or not prog.is_live():
            return 0.0, 0.0
        pool = self.fam._side_pool(slug, prog)
        tick = book.tick or 0.01
        mine: dict[float, float] = {}
        for o in list(self.fam.orders.values()):
            if o.market == slug and o.side == far:
                mine[round(o.price, 4)] = mine.get(round(o.price, 4), 0.0) + o.qty
        levels = [(p, q - mine.get(round(p, 4), 0.0)) for p, q in book.side(far)]
        levels = [(p, q) for p, q in levels if q > 1e-9]
        j = estimate_join(far, levels, tick, float(prog.df), float(prog.target), px, qty)
        if not (j.qualifies and j.in_window):
            return 0.0, 0.0
        return float(j.share), (float(j.share) * pool if pool else 0.0)

    def _more_slot(self, slug: str, side: str, book, cap_usd: float):
        """The cheapest bond price, never dearer than his original
        purchase here and inside the spread, where up to cap_usd of
        ours captures MORE_SHARE of its side: (price, qty, share, est)
        or None."""
        far, _ = self.entry(side)
        tick = book.tick or 0.01
        bids, asks = book.bids, book.asks
        px0 = self._first_px(slug)
        if px0 <= 0:
            return None
        if side == "YES":
            hi = min((asks[0][0] - tick) if asks else 0.99, px0)
            lo = ((bids[0][0] if bids else hi) - BEHIND_MAX_TICKS * tick)
            n = max(int(round((hi - lo) / tick)), 0)
            cands = [self._snap_down(lo + i * tick, tick) for i in range(n + 1)]
            cands = [p for p in cands if 0.001 <= p <= min(px0, PRICE_CAP, 0.999)]
            cands.sort()                                   # cheapest first
        else:
            lo = max((bids[0][0] + tick) if bids else 0.01, px0)
            hi = ((asks[0][0] if asks else lo) + BEHIND_MAX_TICKS * tick)
            n = max(int(round((hi - lo) / tick)), 0)
            cands = [self._snap_up(lo + i * tick, tick) for i in range(n + 1)]
            cands = [p for p in cands if max(0.001, px0, 1.0 - PRICE_CAP) <= p <= 0.999]
            cands.sort(reverse=True)                       # cheapest NO first
        for px in list(dict.fromkeys(cands)):
            cost = px if side == "YES" else round(1.0 - px, 4)
            qty = float(math.floor(cap_usd / cost)) if cost > 0 else 0.0
            if qty < 1.0:
                continue
            share, est = self._share_at(slug, far, book, px, qty)
            if share + 1e-9 >= MORE_SHARE:
                return px, qty, share, est
        return None

    def _pull_more(self, slug: str, why: str) -> None:
        for o in self._more_orders(slug):
            r = self.fam.desk.cancel(o.id, slug, initiator="owner")
            if r.ok:
                self.fam.orders.pop(o.id, None)
                f = self.fill_book.get(o.id)
                if f is not None:
                    f["open"] = False
                    f["ts"] = round(self._clock(), 1)
                self._log(event="more_pulled", market=slug, price=o.price,
                          qty=o.qty, note=why)

    def _keep_buying(self, slug: str, side: str, positions: dict,
                     now: float) -> dict | None:
        """The buy-more order (owner, 2026-09-03): rests at the cheapest
        price that captures MORE_SHARE of its side, sized to the cap;
        moves when it no longer captures that, on the cooldown; pulled
        when no price inside the cap can, or when nothing is held."""
        cap = self.more_cap.get(slug) or {}
        cap_usd = float(cap.get("usd") or 0.0)
        cur = self._more_orders(slug)
        if self.held(slug, side) < 1.0 or cap_usd < 1.0:
            if cur:
                self._pull_more(slug, "nothing held here" if cap_usd >= 1.0
                                else "buy-more amount is zero")
            return None
        book = self.fam.cache.fresh(slug, 120.0, now)
        if book is None:
            return None
        far, intent = self.entry(side)
        pos = float((positions.get(slug) or (0.0, 0.0))[0])
        if cur:
            o = cur[0]
            cost = o.price if side == "YES" else round(1.0 - o.price, 4)
            want_qty = float(math.floor(cap_usd / cost)) if cost > 0 else 0.0
            share, _est = self._share_at(slug, far, book, o.price, o.qty)
            if share + 1e-9 >= MORE_SHARE and abs(o.qty - want_qty) < 1.0:
                return None
            if now - self.moved_more_at.get(slug, 0.0) < MOVE_COOLDOWN_S:
                return None
            slot = self._more_slot(slug, side, book, cap_usd)
            if slot is None:
                self._pull_more(slug, f"no price inside the cap captures "
                                      f"{MORE_SHARE:.0%} of the side now")
                return None
            if abs(slot[0] - o.price) < 1e-9 and abs(slot[1] - o.qty) < 1.0:
                return None
            self._pull_more(slug, "moving")
        slot = self._more_slot(slug, side, book, cap_usd)
        if slot is None:
            note = f"no price inside the cap captures {MORE_SHARE:.0%} of its side"
            if self._more_note.get(slug) != note:
                self._more_note[slug] = note
                self._log(event="more_none", market=slug, note=note)
            return None
        px, qty, share, est = slot
        r = self.fam.desk.place_resting(slug, far, px, qty, net_position=pos,
                                        initiator="owner", intent=intent)
        if not (r.ok and r.order_id):
            self._log(event="more_refused", market=slug, note=r.note[:120])
            return None
        px = r.price or px
        self.fam.orders[r.order_id] = FamilyOrder(
            id=r.order_id, market=slug, side=far, price=px, qty=qty,
            intent=(r.intent or intent), placed_ts=now, purpose="bond",
            why=f"bond more: buying up to ${cap_usd:,.2f} more, never dearer "
                f"than his first price — {share:.0%} of the "
                f"{'bid' if far == 'BUY' else 'ask'} side at {px * 100:g}c")
        self.fill_book[r.order_id] = {"slug": slug, "side": side, "qty": 0.0,
                                      "px": px, "ts": round(now, 1), "open": True}
        self.moved_more_at[slug] = now
        self._more_note.pop(slug, None)
        self._log(event=("more_moved" if cur else "more_rested"), market=slug,
                  side=side, price=px, qty=qty, share=round(share, 3),
                  est=round(est, 4))
        return {"market": slug, "bond": side, "side": far, "price": px,
                "qty": qty, "more": True}

    # -- the sniper ----------------------------------------------------------

    def _front(self, slug: str, side: str, book):
        """The nearest order of OTHERS sitting between the touch and our
        main order, any size: (price, size) or None."""
        bs, _ = self.earn(side)
        main = self._orders(slug, bs, decoy=False)
        if not main:
            return None
        cur = main[0].price
        tick = book.tick or 0.01
        for p, q_others in self._others(slug, bs, book):
            ahead = (p < cur - tick / 2) if side == "YES" else (p > cur + tick / 2)
            if not ahead:
                break
            return (p, q_others)
        return None

    def _minnow_in_front(self, slug: str, side: str, book):
        """The nearest small order sitting between the touch and our
        main order (in the way of its rewards): (price, size) or None."""
        f = self._front(slug, side, book)
        return f if (f is not None and f[1] <= MINNOW_MAX) else None

    def held_markets(self) -> list[str]:
        """Where the ledger holds bond shares — the markets he is in."""
        return sorted(s for s, l in self.lots.items()
                      if abs(float(l.get("qty") or 0.0)) > 0.005)

    def _calc(self, slug: str, side: str, book, held: float) -> dict | None:
        """The earning math on this book, shown on the page (owner,
        2026-09-03: "show me the calculations for how much it's
        earning"): an order's share of its side's score x the side's
        daily pool, which is the program pool ÷ markets in the event ÷ 2
        sides — the exchange's own arithmetic."""
        from .scoring import estimate_join
        prog = self.fam.terms.get(slug)
        if book is None or prog is None:
            return None
        ebs, _ = self.earn(side)
        pool = self.fam._side_pool(slug, prog)
        n = (self.fam.universe.get(slug) or {}).get("event_n")
        levels = [(p, q) for p, q in book.side(ebs) if q > 1e-9]
        tick = book.tick or 0.01
        orders = []
        for o in self._orders(slug, ebs):
            lv = [(p, q - o.qty if abs(p - o.price) < tick / 2 else q)
                  for p, q in levels]
            lv = [(p, q) for p, q in lv if q > 1e-9]
            j = estimate_join(ebs, lv, tick, float(prog.df), float(prog.target),
                              o.price, o.qty)
            ok = bool(j.qualifies and j.in_window)
            orders.append({"price": o.price, "qty": o.qty,
                           "share": round(j.share, 4), "qualifies": ok,
                           "ticks": int(j.ticks),
                           "est": round(j.share * pool, 4) if (ok and pool) else 0.0,
                           "decoy": str(o.why or "").startswith("bond decoy")})
        touch = None
        if levels:
            mine = {round(o.price, 4): o.qty for o in self._orders(slug, ebs)}
            lv = [(p, q - mine.get(round(p, 4), 0.0)) for p, q in levels]
            lv = [(p, q) for p, q in lv if q > 1e-9]
            tp = levels[0][0]
            j = estimate_join(ebs, lv, tick, float(prog.df), float(prog.target),
                              tp, max(held, 1.0))
            ok = bool(j.qualifies and j.in_window)
            touch = {"price": tp, "share": round(j.share, 4), "qualifies": ok,
                     "est": round(j.share * pool, 4) if (ok and pool) else 0.0}
        return {"side": ebs, "pool_day": round(float(prog.daily_pool or 0.0), 2),
                "event_n": n,
                "side_pool": (round(pool, 4) if pool is not None else None),
                "target": float(prog.target), "df": float(prog.df),
                "side_size": round(sum(q for _, q in levels), 1),
                "orders": orders, "touch": touch}

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
            r = self._snap(slug, side, book, m_px, m_q, positions, now)
            return None if (r and r.get("retry")) else r
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
        far = "SELL" if side == "YES" else "BUY"
        blocked, cleared = self._clear_way(slug, far, px, now)
        if blocked:
            self._block_note = blocked
            self._log(event="take_blocked", market=slug, side=side, price=px,
                      note=blocked[:160])
            return None
        if cleared:
            # the book we priced from still lists what we just pulled:
            # read it again and take only what OTHERS still show there
            try:
                book = self.client.book(slug, fetched_at=now)
                self.fam.cache.put(slug, book)
            except Exception as e:  # noqa: BLE001
                self._log(event="take_stopped", market=slug,
                          note=f"could not re-read the book after clearing: {str(e)[:80]}")
                return None
            px2, _c2, size2 = self._take_price(side, book, slug)
            if px2 is None or abs(px2 - px) > 1e-9 or size2 < 1.0:
                return {"retry": True, "qty": 0.0, "usd": 0.0}   # the level moved: price it again
            qty = float(min(qty, math.floor(size2)))
            if qty < 1.0:
                return None
        # a taker order fills and never rests, so it is not verified as
        # resting (the dump learned this): the fill shows up in the
        # position feed and the exchange's trade record
        r = self.fam.desk.place_resting(slug, bs, px, qty, net_position=pos,
                                        initiator="owner", intent=intent,
                                        taker="bond", verify=False)
        if not (r.ok and r.order_id):
            self._log(event="snap_refused", market=slug, note=r.note[:120])
            return None
        # the exchange's trade record says what filled, at what price;
        # the ledger books THAT and nothing else (owner, 2026-09-03)
        filled, fill_px, fee, note = self._filled(r.order_id, qty, (r.price or px), slug)
        if filled < 0.01:
            self._log(event="take_unfilled", market=slug, side=side, price=px,
                      qty=qty, order_id=r.order_id,
                      note=f"the exchange shows no fill for this order"
                           f"{'; ' + note if note else ''}")
            return None
        qty = filled
        px = fill_px
        cost = px if side == "YES" else round(1.0 - px, 4)
        # on the book as ours so the fill is journaled as a bond purchase
        self.fam.orders[r.order_id] = FamilyOrder(
            id=r.order_id, market=slug, side=bs, price=px,
            qty=qty, intent=(r.intent or intent), placed_ts=now, purpose="bond",
            why=f"bond: took {qty:g} {side} at {px * 100:g}c — it was in the "
                f"way of our resting order")
        usd = round(qty * cost, 4)
        self._book_lot(slug, side, qty, usd, ref=r.order_id, fee=fee)
        self.fill_book[r.order_id] = {"slug": slug, "side": side, "qty": qty,
                                      "px": px, "fee": fee, "ts": round(now, 1)}
        usd = round(usd + fee, 4)                 # the commission is money spent
        self._pay(usd)
        self._log(event="snapped", market=slug, side=side, price=px, qty=qty,
                  cost=round(usd, 2), fee=round(fee, 4), cash=round(self.cash, 2),
                  budget=round(self.budget, 2), order_id=r.order_id,
                  **({"note": note} if note else {}))
        self._ping_maybe(usd)
        # the decoy has done its job
        self._pull_decoys(slug, side)
        self.dance.pop(slug, None)
        return {"market": slug, "bond": side, "side": bs, "price": px,
                "qty": qty, "taken": True, "usd": round(usd, 2)}

    @staticmethod
    def _px_of(*cands) -> float | None:
        for c in cands:
            if isinstance(c, dict):
                c = c.get("value")
            if c in (None, ""):
                continue
            try:
                return float(c)
            except (TypeError, ValueError):
                continue
        return None

    def _filled(self, order_id: str, want: float, posted_px: float,
                slug: str) -> tuple[float, float, float, str]:
        """What the exchange's trade record shows filled under this
        order id: (shares, average price, note). Polled for FILL_WAIT_S
        because the feed lags a placement by a few seconds. An order
        that shows resting instead of filled is cancelled — a take was
        never meant to rest. Nothing is assumed."""
        shares, avg, fee, note = 0.0, posted_px, 0.0, ""
        for attempt in range(int(FILL_WAIT_S) + 1):
            if attempt:
                self._sleep(1.0)
            try:
                acts = self.client.recent_trades(limit=50)
                got = self._record_of(order_id, acts)
            except Exception as e:  # noqa: BLE001 — the record is the truth; keep asking
                note = f"trade record: {str(e)[:80]}"
                continue
            if got is not None:
                shares, avg, note = got
                fee = self._fee_of(order_id, acts)
                if not avg:
                    avg = posted_px
                    note = note or "price taken from the order (the record gave none)"
            if shares + 1e-9 >= want:
                break
        if shares + 1e-9 < want:
            # not (fully) filled: if it is resting, pull it — a take
            # must not sit on the book as a bid nobody asked for
            try:
                for o in self.client.open_orders():
                    if str(o.get("id") or "") == order_id:
                        r = self.fam.desk.cancel(order_id, slug, initiator="owner")
                        note = (f"{o.get('size')} rested instead of filling — "
                                f"cancelled ({'ok' if r.ok else r.note[:60]})")
                        break
            except Exception as e:  # noqa: BLE001
                note = f"open orders: {str(e)[:80]}"
        return shares, avg, fee, note

    def _fee_of(self, order_id: str, activities) -> float:
        """The commissions the exchange collected on one order, summed
        over its executions (one per execution id). Positive is a fee
        (a take); a maker fill's rebate comes back negative."""
        seen: dict[str, float] = {}
        for a in activities or []:
            t = a.get("trade") or {}
            for exk in ("passiveExecution", "aggressorExecution"):
                ex = t.get(exk) or {}
                o = ex.get("order") or {}
                if str(o.get("id") or "") != order_id:
                    continue
                xid = str(ex.get("id") or "") or f"{t.get('id')}/{exk}"
                seen[xid] = self._px_of(ex.get("commissionNotionalCollected")) or 0.0
        return round(sum(seen.values()), 4)

    def _record_of(self, order_id: str, activities) -> tuple[float, float, str] | None:
        """(filled shares, average price, note) for one order from the
        exchange's activity rows, or None when no row names it. Each row
        is one execution; the ORDER inside carries the exchange's own
        running total (cumQuantity) and average price (avgPx) — that is
        what is read, so five executions never count as one. Rows with
        no such total fall back to the executions' own share counts,
        one per execution id."""
        cum = 0.0
        avg = None
        by_exec: dict[str, tuple[float, float | None]] = {}
        found = False
        for a in activities or []:
            t = a.get("trade") or {}
            for exk in ("passiveExecution", "aggressorExecution"):
                ex = t.get(exk) or {}
                o = ex.get("order") or {}
                if str(o.get("id") or "") != order_id:
                    continue
                found = True
                c = self._px_of(o.get("cumQuantity")) or 0.0
                if c > cum:
                    cum = c
                    avg = self._px_of(o.get("avgPx")) or avg
                xid = str(ex.get("id") or "") or f"{t.get('id')}/{exk}"
                by_exec[xid] = (self._px_of(ex.get("lastShares")) or 0.0,
                                self._px_of(ex.get("lastPx"), o.get("price")))
        if not found:
            return None
        if cum > 0:
            return round(cum, 4), (round(avg, 4) if avg else 0.0), ""
        shares = round(sum(q for q, _ in by_exec.values()), 4)
        usd = sum(q * (px or 0.0) for q, px in by_exec.values())
        return shares, (round(usd / shares, 4) if shares > 0 and usd > 0 else 0.0), \
            "no running total in the record; executions summed"

    def _clear_way(self, slug: str, far: str, px: float, now: float) -> str | None:
        """Before a take: our own orders on the side it hits, at or
        better than its price, would be matched first — the exchange
        will not fill us against ourselves, and the take sits (the
        Hawaii case, 2026-09-03: the engine's 3-share cover bid at 7c
        was in the way of a 7c sell). The engine's and the bond's own
        orders there are pulled and the engine is held off the market
        for HOLD_ENGINE_S; a hand order in the way is never touched —
        the take is refused and says which order. Returns the refusal
        note and whether anything was cleared: (note, cleared)."""
        def better(p):
            return p <= px + 1e-9 if far == "SELL" else p >= px - 1e-9
        in_way = [o for o in list(self.fam.orders.values())
                  if o.market == slug and o.side == far and better(o.price)]
        hand = [o for o in in_way if o.purpose == "manual"]
        if hand:
            h = hand[0]
            return (f"your own order {h.id} ({h.qty:g} @ {h.price * 100:g}c) rests "
                    f"in the way — the exchange would match you against "
                    f"yourself; move it first"), False
        if not in_way:
            return None, False
        engine = [o for o in in_way if o.purpose != "bond"]
        if engine:
            self.fam.hold_until[slug] = now + HOLD_ENGINE_S
        gone = []
        for o in in_way:
            r = self.fam.desk.cancel(o.id, slug, initiator="owner")
            if r.ok:
                self.fam.orders.pop(o.id, None)
                gone.append(o.id)
            else:
                return f"could not clear our {o.purpose} order {o.id}: {r.note[:80]}", True
        # wait for the exchange to show them gone before the take
        for attempt in range(int(CLEAR_WAIT_S) + 1):
            try:
                still = {str(o.get("id") or "") for o in self.client.open_orders()}
            except Exception:  # noqa: BLE001
                still = set()
            if not (still & set(gone)):
                break
            if attempt == int(CLEAR_WAIT_S):
                return (f"our cleared order{'s' if len(gone) > 1 else ''} "
                        f"{', '.join(gone)} still show open — not taking into "
                        f"our own order"), True
            self._sleep(1.0)
        self._log(event="cleared", market=slug, side=far, price=px,
                  qty=round(sum(o.qty for o in in_way), 2), orders=gone,
                  note=("our own orders in the take's way pulled"
                        + (f"; engine held off here until "
                           f"{time.strftime('%H:%M', time.gmtime(now + HOLD_ENGINE_S))}Z"
                           if engine else "")))
        return None, True

    def _take_price(self, side: str, book, slug: str) -> tuple[float | None, float, float]:
        """(YES price to take, bond cost per dollar, size OTHERS show) for
        opening a bond of this side at the best level that is not ours."""
        far = "SELL" if side == "YES" else "BUY"
        lv = self._others(slug, far, book)
        if not lv:
            return None, 0.0, 0.0
        p, q = lv[0]
        return p, (p if side == "YES" else round(1.0 - p, 4)), q

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

    # ------------------------------------------------------------ the owner's entry

    def enter(self, slug: str, limit_px, now: float,
              positions: dict | None = None) -> dict:
        """The owner's own entry (2026-09-02: "the initial purchases
        should be made by me so let me see the books in the bond market
        and give me the choice to enter at various prices, snapping up
        all the currently resting sale orders at or below that price"):
        sweep the entry side from the touch out to his price, each level
        taken at its own price and never more than it shows, until the
        money runs out. Every lot joins the ledger."""
        meta = self.approved.get(slug)
        if meta is None:
            return {"ok": False, "note": "not on the bond list"}
        side = meta["side"]
        try:
            limit_px = float(limit_px)
        except (TypeError, ValueError):
            return {"ok": False, "note": "which price?"}
        if not (0.001 <= limit_px <= 0.999):
            return {"ok": False, "note": "price must be 0.1c to 99.9c"}
        if self._money() < MONEY_MIN_USD:
            return {"ok": False, "note": "no money to buy with — set the deploy "
                                         "budget first"}
        bought = usd = 0.0
        lots = 0
        last = None
        self._block_note = ""
        for _ in range(ENTER_MAX_LEVELS):
            try:
                book = self.client.book(slug, fetched_at=now)
            except Exception as e:  # noqa: BLE001
                self._log(event="enter_stopped", market=slug,
                          note=f"could not read the book: {str(e)[:80]}")
                break
            self.fam.cache.put(slug, book)
            px, cost, size = self._take_price(side, book, slug)
            if px is None or size < 1.0:
                break
            past = ((side == "YES" and px > limit_px + 1e-9)
                    or (side == "NO" and px < limit_px - 1e-9))
            if past:
                break
            if last is not None and abs(px - last[0]) < 1e-9 and size >= last[1] - 1e-9:
                break                   # the book did not move: do not re-buy the same level
            last = (px, size)
            pos = positions if positions is not None else {
                slug: (float((self.fam.inventory.get(slug) or {}).get("qty") or 0.0), 0.0)}
            r = self._snap(slug, side, book, px, size, pos, now)
            if not r:
                break
            if r.get("retry"):
                last = None                 # the book changed under us: price it again
                continue
            bought += r["qty"]
            usd += r["usd"]
            lots += 1
            if self._money() < MONEY_MIN_USD:
                break
        if bought <= 0.005:
            if getattr(self, "_block_note", ""):
                return {"ok": False, "note": f"nothing was bought — {self._block_note}"}
            return {"ok": False, "note": "nothing was bought — nothing resting "
                                         "at or inside that price, or no money"}
        self._log(event="entered", market=slug, side=side, qty=round(bought, 2),
                  usd=round(usd, 2), lots=lots, limit=limit_px)
        return {"ok": True,
                "note": f"bought {bought:g} {side} in {lots} lot"
                        f"{'s' if lots != 1 else ''} for ${usd:,.2f} "
                        f"({usd / bought * 100:.1f}c per dollar of bond); "
                        f"${self._money():,.2f} left to deploy"}

    # ------------------------------------------------------------ the page

    def _row(self, slug: str, meta: dict, now: float,
             positions: dict | None) -> dict:
        side = meta["side"]
        book = self.fam.cache.any_age(slug)
        bid = book.bids[0][0] if book is not None and book.bids else None
        ask = book.asks[0][0] if book is not None and book.asks else None
        px, cost, size = (self._take_price(side, book, slug) if book is not None
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
        front = self._front(slug, side, book) if book is not None else None
        minnow = front if (front is not None and front[1] <= MINNOW_MAX) else None
        ladder = []
        if book is not None:
            cum_q = cum_usd = 0.0
            far = "SELL" if side == "YES" else "BUY"
            for p, q in self._others(slug, far, book)[:LADDER_SHOW]:
                c = p if side == "YES" else round(1.0 - p, 4)
                cum_q += q
                cum_usd += q * c
                ladder.append({"px": p, "cost": round(c, 4), "qty": round(q, 1),
                               "cum_qty": round(cum_q, 1),
                               "cum_usd": round(cum_usd, 2)})
        return {
            "ladder": ladder,
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
            "rewards": round(self._market_rewards(slug), 2),
            "earn": earn,
            "earn_order": ([{"price": o.price, "qty": o.qty,
                             "est": round(o.live_est or 0.0, 4)}
                            for o in self._orders(slug, ebs, decoy=False)]
                           or None),
            "decoy": ([{"price": o.price, "qty": o.qty}
                       for o in self._orders(slug, ebs, decoy=True)] or None),
            "minnow": ({"price": minnow[0], "qty": round(minnow[1], 1)}
                       if minnow else None),
            "front": ({"price": front[0], "qty": round(front[1], 1)}
                      if front else None),
            "calc": (self._calc(slug, side, book, held)
                     if (held > 0.005 or self._orders(slug)) else None),
            "dance": self.dance.get(slug),
            "stale": (book is None or now - book.fetched_at > 600.0),
            "slot": self.slot.get(slug),
            "hold_until": (self.fam.hold_until.get(slug)
                           if self.fam.hold_until.get(slug, 0.0) > now else None),
            "more": self._more_view(slug, side, book, held),
            "floor": self._floor_view(slug, side, book),
        }

    def _floor_view(self, slug: str, side: str, book) -> float | None:
        """The lowest bond price the exit will sell at — the price paid,
        on the tick — shown so a sale is never a surprise."""
        if self.cost_basis(slug, side) <= 0:
            return None
        tick = (book.tick if book is not None else None) or 0.01
        b = self._bound(slug, side, tick)
        return round(b if side == "YES" else 1.0 - b, 4)

    def _more_view(self, slug: str, side: str, book, held: float) -> dict | None:
        cap = self.more_cap.get(slug)
        if held < 0.005 or cap is None:
            return None
        far, _ = self.entry(side)
        px0 = self._first_px(slug)
        out = {"cap_usd": round(float(cap.get("usd") or 0.0), 2),
               "by": cap.get("by", "default"), "order": None, "slot": None,
               "cap_px": (round(px0 if side == "YES" else 1.0 - px0, 4) if px0 > 0 else None)}
        cur = self._more_orders(slug)
        if cur:
            o = cur[0]
            share, est = (self._share_at(slug, far, book, o.price, o.qty)
                          if book is not None else (0.0, 0.0))
            out["order"] = {"price": o.price, "qty": o.qty,
                            "share": round(share, 4), "est": round(est, 4)}
        elif book is not None and out["cap_usd"] >= 1.0:
            slot = self._more_slot(slug, side, book, out["cap_usd"])
            if slot:
                out["slot"] = {"price": slot[0], "qty": slot[1],
                               "share": round(slot[2], 4), "est": round(slot[3], 4)}
        return out

    def live_rows(self, now: float, positions: dict | None = None) -> dict:
        """The live line's payload: the rows of the markets he is in
        (bond shares held, or a bond order resting), keyed by market.
        Books come from the cache the exchange's stream feeds."""
        out = {}
        for slug, meta in list(self.approved.items()):
            if self.held(slug, meta["side"]) > 0.005 or self._orders(slug):
                out[slug] = self._row(slug, meta, now, positions)
        return out

    def view(self, now: float, positions: dict | None = None) -> dict:
        rows = [self._row(slug, meta, now, positions)
                for slug, meta in list(self.approved.items())]
        # the markets he is in first, largest at cost first; then the
        # rest cheapest per dollar (owner, 2026-09-03: "take the markets
        # I'm actually in and put them at the top")
        rows.sort(key=lambda r: (0 if r["qty"] > 0.005 else 1,
                                 -(r["qty"] * (r["cost_px"] or 0.0)),
                                 r["cost"] is None, r["cost"] or 1.0))
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
                "budget": round(self._budget_now(), 2), "spent": round(self.spent, 2),
                "budget_mode": self.budget_mode, "tax": self.tax_owed() or None,
                "money": round(self._money(), 2),
                "earned": self._earned(),
                "unpinged": round(self.unpinged, 2),
                "held_cost": held_cost,
                "high": HIGH_ODDS, "low": LOW_ODDS, "price_cap": PRICE_CAP,
                "keep": KEEP_FRACTION,
                "dance_wait_s": DANCE_WAIT_S, "minnow_max": MINNOW_MAX,
                "more_share": MORE_SHARE,
                "scan_day": self.scan_day, "scan_hour_utc": SCAN_HOUR_UTC,
                "log": self.log[-12:]}

    # ------------------------------------------------------------ persistence

    def to_dict(self) -> dict:
        return {"approved": self.approved, "proposed": self.proposed,
                "ignored": self.ignored, "dropped": self.dropped,
                "lots": self.lots,
                "cash": round(self.cash, 4),
                "budget": round(self.budget, 4), "spent": round(self.spent, 4),
                "budget_mode": self.budget_mode,
                "unpinged": round(self.unpinged, 4),
                "realized": round(self.realized, 4), "sold_usd": round(self.sold_usd, 4),
                "share_day": self.share_day,
                "rewards_booked": round(self.rewards_booked, 4),
                "engine_booked": round(self.engine_booked, 4),
                "paid_booked": round(self.paid_booked, 4),
                "rewards_by_market": self.rewards_by_market,
                "lot_ts": self.lot_ts, "exch_max": self.exch_max,
                "fill_book": self.fill_book,
                "more_cap": self.more_cap, "moved_more_at": self.moved_more_at,
                "dots": self.dots[-DOTS_KEEP:],
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
        self.lots = {}
        unbooked: list[dict] = []
        for k, v in (d.get("lots") or {}).items():
            lot = {"qty": float(v.get("qty") or 0.0),
                   "cost": float(v.get("cost") or 0.0),
                   "fees": float(v.get("fees") or 0.0),
                   "fills": [str(x) for x in (v.get("fills") or [])]}
            if not lot["fills"]:
                # booked by the old code on the assumption a take filled:
                # nothing the exchange confirmed backs it (owner,
                # 2026-09-03). Dropped; what it charged goes back.
                spent = float(d.get("spent") or 0.0)
                d["spent"] = round(max(spent - lot["cost"], 0.0), 4)
                if str(d.get("budget_mode") or "tax") != "tax":
                    d["budget"] = round(float(d.get("budget") or 0.0) + lot["cost"], 4)
                unbooked.append({"event": "unbooked_unconfirmed", "market": str(k),
                                 "qty": abs(lot["qty"]), "cost": round(lot["cost"], 2),
                                 "note": "no exchange-confirmed fill backs this lot; "
                                         "dropped and its cost returned",
                                 "ts": round(self._clock(), 1)})
                continue
            self.lots[str(k)] = lot
        self.cash = float(d.get("cash") or 0.0)
        self.budget = float(d.get("budget") or 0.0)
        self.spent = float(d.get("spent") or 0.0)
        self.budget_mode = str(d.get("budget_mode") or "tax")
        self.unpinged = float(d.get("unpinged") or 0.0)
        self.realized = float(d.get("realized") or 0.0)
        self.sold_usd = float(d.get("sold_usd") or 0.0)
        self.share_day = {str(day): {str(k): [float(v[0]), int(v[1])]
                                     for k, v in (mk or {}).items()}
                          for day, mk in (d.get("share_day") or {}).items()}
        self.rewards_booked = float(d.get("rewards_booked") or 0.0)
        self.engine_booked = float(d.get("engine_booked") or 0.0)
        self.paid_booked = float(d.get("paid_booked") or 0.0)
        self.rewards_by_market = {str(k): float(v) for k, v
                                  in (d.get("rewards_by_market") or {}).items()}
        self.lot_ts = {str(k): float(v) for k, v in (d.get("lot_ts") or {}).items()}
        self.exch_max = {str(k): float(v) for k, v in (d.get("exch_max") or {}).items()}
        self.fill_book = {str(k): dict(v) for k, v in (d.get("fill_book") or {}).items()}
        self.more_cap = {str(k): dict(v) for k, v in (d.get("more_cap") or {}).items()}
        self.moved_more_at = {str(k): float(v) for k, v
                              in (d.get("moved_more_at") or {}).items()}
        self.dots = [list(x) for x in (d.get("dots") or [])][-DOTS_KEEP:]
        # a lot booked before fills were kept by order id: seed the book
        # from the lot itself so the next cycle re-checks it (the Hawaii
        # lot of 2026-09-03: 1 booked, 5 filled)
        for slug, lot in self.lots.items():
            ids = [x for x in lot.get("fills") or [] if x != "adopt"]
            if len(ids) == 1 and ids[0] not in self.fill_book:
                q = abs(float(lot.get("qty") or 0.0))
                side = "YES" if float(lot.get("qty") or 0.0) >= 0 else "NO"
                per = float(lot.get("cost") or 0.0) / q if q > 0.005 else 0.0
                px = per if side == "YES" else round(1.0 - per, 4)
                self.fill_book[ids[0]] = {"slug": slug, "side": side, "qty": q,
                                          "px": round(px, 4), "ts": round(self._clock(), 1)}
        self.scan_day = str(d.get("scan_day") or "")
        self._earn_seen = {str(k): float(v) for k, v in (d.get("earn_seen") or {}).items()}
        self._earn_px = {str(k): float(v) for k, v in (d.get("earn_px") or {}).items()}
        self._exch_seen = {str(k): float(v) for k, v in (d.get("exch_seen") or {}).items()}
        self.slot = {str(k): dict(v) for k, v in (d.get("slot") or {}).items()}
        self.moved_at = {str(k): float(v) for k, v in (d.get("moved_at") or {}).items()}
        self.dance = {str(k): dict(v) for k, v in (d.get("dance") or {}).items()}
        self.log = list(d.get("log") or [])
        self.log.extend(unbooked)
        del self.log[:-LOG_KEEP]
        self._mark_engine()
