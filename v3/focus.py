"""The focus tender (owner, 2026-09-10).

"With the dramatic rise in the rewards pools of some of the markets, we
need to put our focus on them." Every politics market whose paying
program is FOCUS_POOL_MIN_USD a day per event or more — the midterms
tiers (T1 $1,500, T2 $600, T3 $250) and the elections boost ($300) —
leaves the slow engine and the bond list and is worked here:

* a pass every FOCUS_CYCLE_S on a thread of its own, outside the
  minute-long family cycle ("They need to be outside of whatever causes
  the engine to update slowly");
* the program watch: the focus markets' terms re-read every
  FOCUS_TERMS_S and the whole politics universe walked FOCUS_TERMS_SLICE
  slugs a pass, so a new boosted program is on the page and on his
  phone within minutes of appearing;
* the list sorted by the expected value of an entry of 10% of his
  buying power at the optimal price ("The focus list should be sorted
  by expected value of an entry of 10% of my buying power at an optimal
  price");
* every order in every focus market — his, the engine's, the tender's —
  on the page with place, cancel, move and resize ("For each market
  (whether I'm in it or not), I need to be able to place, cancel, move,
  resize, change price, etc both on existing and new orders");
* his fair price per market is the tender's number ("I need to set
  fair prices that the dedicated tender of these markets can use to
  update them similar to how the bond one currently does"): where he
  has set one the tender rests and keeps one order a side by expected
  value — the reward claim, less the fill's expected cost, less the
  cost of the capital tied up — never past his fair; where he has not,
  the market is shown with Silver's number as a suggestion and nothing
  is tended ("Yes for 5");
* the money at risk — collateral x fill odds, the politics ceiling's
  own measure — stays under FOCUS_LOSS_CAP_USD across the tender's
  orders ("For 6 expected loss of 1000"). "The fill cost for politics
  has been high recently. Keep that in mind": the fill cost never
  reads under FOCUS_FILL_COST_MIN a share.

Alaska governor is on the ground here ("Add Alaska gov") though the
engine still avoids it. The balance-of-power books stay his own hand's
(2026-08-22): shown, controllable, never tended. Held ground (state
races, county winners) is shown but not tended until he opens it.
"""

from __future__ import annotations

import json
import math
import threading
import time

from .family import FamilyOrder
from .intents import BUY_LONG, BUY_SHORT, capital_at_risk
from .programs import pool_days
from .scoring import estimate_join
from .terms import TermsStore

FOCUS_POOL_MIN_USD = 250.0      # a program paying this a day per event is boosted
FOCUS_ALLOW_TOKENS = ("usgub-ak",)   # avoided ground the tender may still work
FOCUS_CYCLE_S = 15.0
FOCUS_BOOK_MAX_AGE_S = 30.0     # a focus market's book older than this is read again
FOCUS_BOOK_READS = 12           # ...this many a pass at most, oldest first
FOCUS_BOOK_READS_BOOT = 150     # a market with no book at all is read at once,
                                # this many a pass (owner, 2026-09-10: "The
                                # start up time for focus has to be very short")
FOCUS_ACT_AGE_S = 45.0          # no order rests or moves on a book older than this
FOCUS_TERMS_S = 600.0           # the focus markets' terms re-read this often
FOCUS_TERMS_SLICE = 200         # the politics universe walked this many slugs a pass
FOCUS_LOSS_CAP_USD = 1000.0     # expected loss resting, all the tender's orders
FOCUS_STAKE_FRAC = 0.10         # the list's entry: 10% of his buying power
FOCUS_COC_DAY = 0.005           # cost of capital: per dollar tied up, a day
FOCUS_FILL_COST_MIN = 0.02      # $/share a fill's cost never reads under
FOCUS_FILL_COST_MAX = 0.25      # ...nor over (a broken measure must not read as $2)
FOCUS_PF_FLOOR = 0.05           # fill odds never charge the cap under this
FOCUS_BEHIND_MAX = 6            # candidate slots out to this many ticks behind the touch
FOCUS_KEEP = 0.80               # a resting order stays while it keeps this much of the best EV
# the sizes tried at each price, as fractions of the stake (owner,
# 2026-09-10: "A bid over fair value is fine as long as it is
# appropriately sized for the risk and rewards it can earn"): the reward
# claim saturates with size, the fill's cost does not, so past fair the
# best order is often a smaller one
FOCUS_SIZE_FRACS = (1.0, 0.7, 0.5, 0.35, 0.25, 0.15, 0.1)
FOCUS_MOVE_COOLDOWN_S = 300.0   # an entry moves at most this often
FOCUS_EXIT_COOLDOWN_S = 60.0    # an exit follows the touch and the lot within a minute
                                # (owner, 2026-09-10: "Exit orders should never be held
                                # and don't need to ramp up. They can always be placed")
# an order whose expected value reads under zero comes off only after
# it has read so for this long (2026-09-10 14:00-14:50Z: on the
# tenth-cent seat-count and balance-of-power books the touch flickers
# by a tenth, the order reads three ticks behind for one pass, and the
# Senate lte45 ask was pulled 21 times and re-rested 19 in an hour)
FOCUS_WEAK_DWELL_S = 300.0
# after an entry of the tender's fills, the rest of the order comes off
# and nothing new rests on that side for FOCUS_REFILL_WAIT_S; then the
# side re-enters at FOCUS_REFILL_FLOOR of the stake and ramps back to
# full size by FOCUS_REFILL_SCALE_S after the fill (owner, 2026-09-10:
# "The stand off after a fill for a side should be 15 minutes and when
# entering size should be scaled down until the two hour window has
# passed"; the House dem control market had filled eleven times in
# fifteen minutes while the tender re-rested at full size)
FOCUS_REFILL_WAIT_S = 15 * 60.0
FOCUS_REFILL_SCALE_S = 2 * 3600.0
FOCUS_REFILL_FLOOR = 0.25
FOCUS_BP_EVERY_S = 60.0
FOCUS_BP_WINDOW_S = 1800.0      # the stake follows the HIGHEST buying-power read of
                                # the last half hour: a fill's dip must not pull every
                                # order and re-rest it a minute later (13:43-13:46Z)
FOCUS_VANISH_WAIT_S = 600.0     # an order gone from the open list is a fill only when
                                # the family's journal says so; the list left the TX
                                # governor bid out for one read (13:35Z) and the tender
                                # took it for a fill, held the side and pulled the
                                # restored order
MINE_IDS_KEEP = 600
FOCUS_ACTIONS_PER_PASS = 8      # places, moves and pulls a pass
FOCUS_MAX_ORDERS = 40           # the tender's own orders, all markets
# the stream's 200 subscriptions (owner, 2026-09-10: "we'll have to
# dramatically reduce its websocket budget"): the focus markets seat
# first, up to FOCUS_WS_CAP; the old engine's whole list — bonds,
# football, unboosted politics — fits in the ENGINE_WS_CAP behind them
FOCUS_WS_CAP = 160
ENGINE_WS_CAP = 40
LOG_KEEP = 300
EVENTS_KEEP = 60
PURPOSE = "focus"


def _r4(x: float) -> float:
    return round(float(x), 4)


class Focus:
    """`fam` is the politics family (its universe, cache, desk, orders
    and fill model are the ground); `bonds` gets the boosted markets
    handed to it; `fair` is the model's number (Silver) as a
    suggestion; `switch_on` says whether the tender may place;
    `buying_power` reads the account's free money."""

    def __init__(self, fam, client, bonds=None, fair=None, alert=None,
                 clock=None, switch_on=None, buying_power=None):
        self.fam, self.client, self.bonds = fam, client, bonds
        self.model_fair = fair or (lambda s: None)
        self.alert = alert or (lambda t, m: None)
        self._clock = clock or time.time
        self.switch_on = switch_on or (lambda: False)
        self._bp_fn = buying_power
        self.lock = threading.RLock()
        self.terms = TermsStore()
        self.fairs: dict[str, float] = {}        # his fair, YES price
        self.stakes: dict[str, float] = {}       # his stake per side, $
        self.paused: set[str] = set()
        # markets he took off his hand's list (owner, 2026-09-10 "Give me
        # a button to take a market off of the hand tended list"): the
        # tender may work them though the engine's avoid list names them
        self.released: set[str] = set()
        self.coc_day = FOCUS_COC_DAY
        self.fill_floor = FOCUS_FILL_COST_MIN
        self.loss_cap = FOCUS_LOSS_CAP_USD
        self.log: list[dict] = []
        self.events: list[dict] = []
        self.first_seen: dict[str, float] = {}
        self.moved_at: dict[str, float] = {}
        self.filled_at: dict[str, float] = {}     # slug|side -> when an entry last filled
        self.weak_since: dict[str, float] = {}    # slug|side -> reading under zero since
        self._last_mine: dict[str, tuple] = {}    # id -> (slug, side, qty) of the tender's entries
        self._gone_by_me: set[str] = set()        # ids the tender itself cancelled or replaced
        self._vanished: dict[str, tuple] = {}     # id -> (slug, side, qty, since): awaiting the journal
        self._journaled: set[str] = set()         # journal rows already counted (oid|ts)
        self.mine_ids: list[str] = []             # every order id the tender ever placed (bounded)
        self._bp_reads: list[tuple] = []          # (ts, bp) over the last window
        self.markets: list[str] = []
        self.rows: dict[str, dict] = {}
        self.last_terms_own = 0.0
        self._rotor = 0
        self._bp: tuple[float, float] | None = None
        self.last_pass = 0.0
        self.pass_s = 0.0
        self.books_read = 0
        self.note = ""
        self._blocked_noted = 0.0
        self.payload_json = b'{"ok":false,"note":"the first pass has not run yet"}'

    # -- plumbing --------------------------------------------------------------

    def _log(self, **kw) -> None:
        kw.setdefault("ts", round(self._clock(), 1))
        self.log.append(kw)
        del self.log[:-LOG_KEEP]

    def _label(self, slug: str) -> str:
        try:
            return self.fam._label(slug)
        except Exception:  # noqa: BLE001
            return slug

    def buying_power(self, now: float) -> float | None:
        if self._bp is not None and now - self._bp[1] < FOCUS_BP_EVERY_S:
            return self._bp[0]
        if self._bp_fn is None:
            return None
        try:
            v = self._bp_fn()
        except Exception:  # noqa: BLE001 — unknown, not zero
            return self._bp[0] if self._bp is not None else None
        try:
            val = float(v) if v is not None else None
        except (TypeError, ValueError):
            val = None
        if val is None:
            return self._bp[0] if self._bp is not None else None
        self._bp = (val, now)
        self._bp_reads = [(ts, v) for ts, v in self._bp_reads
                          if now - ts <= FOCUS_BP_WINDOW_S] + [(now, val)]
        return val

    def stake_bp(self, now: float) -> float | None:
        """The buying power the stake follows: the highest read of the
        last FOCUS_BP_WINDOW_S."""
        bp = self.buying_power(now)
        vals = [v for ts, v in self._bp_reads if now - ts <= FOCUS_BP_WINDOW_S]
        if bp is not None:
            vals.append(bp)
        return max(vals) if vals else None

    def stake(self, slug: str, bp: float | None) -> tuple[float, str]:
        s = self.stakes.get(slug)
        if s is not None:
            return float(s), "set by you"
        if bp is None:
            return 0.0, "buying power unknown"
        return round(FOCUS_STAKE_FRAC * bp, 2), f"{FOCUS_STAKE_FRAC * 100:g}% of buying power"

    # -- the ground ------------------------------------------------------------

    def is_boosted(self, slug: str, prog=None) -> bool:
        prog = self.terms.get(slug) if prog is None else prog
        if prog is None or not prog.is_live() or not prog.pool:
            return False
        return float(prog.pool) / pool_days(prog, slug) >= FOCUS_POOL_MIN_USD

    def _allowed(self, slug: str) -> bool:
        return slug in self.released or any(t in slug for t in FOCUS_ALLOW_TOKENS)

    def by_hand(self, slug: str) -> bool:
        """On his hand's list: the engine avoids it and he has not
        released it to the tender."""
        return bool(self.fam._avoided(slug)) and not self._allowed(slug)

    def why_not_tended(self, slug: str) -> str | None:
        """None when the tender may rest here; else the plain reason."""
        fam = self.fam
        if slug in self.paused:
            return "paused by you"
        if self.fairs.get(slug) is None:
            return "no fair set — shown only"
        if fam.held_ground(slug):
            return "held ground — open it on the orders page first"
        if any(t in slug for t in (fam.cfg.freeze_tokens or ())):
            return "frozen — hands off (owner, 2026-08-24)"
        if any(t in slug for t in (fam.cfg.liquidate_tokens or ())):
            return "close-out ground — the tender rests nothing new"
        if self.by_hand(slug):
            return "your own hand's book — not tended"
        return None

    def seed(self, now: float) -> None:
        """Boot: the family's terms stand in until the tender's own
        reads land, the ground is claimed before the first family
        cycle can act on it, and the page lists the markets at once
        (books and plans follow on the first pass, seconds later)."""
        with self.lock:
            for slug, prog in list(self.fam.terms.current.items()):
                if slug not in self.terms.current:
                    self.terms.current[slug] = prog
                    self.terms.updated_at[slug] = float(self.fam.terms.updated_at.get(slug) or 0.0)
            self.refresh_markets(now, quiet=True)
            self._claim_orders()
            self._plan_all(now, {}, None)
            self.note = "starting — the books are being read"
            self._freeze(now, None, bool(self.switch_on()))

    def refresh_markets(self, now: float, quiet: bool = False) -> list[str]:
        out = []
        for slug, prog in list(self.terms.current.items()):
            if slug in self.fam.universe and self.is_boosted(slug, prog):
                out.append(slug)
        out.sort()
        new = [s for s in out if s not in self.first_seen]
        for s in new:
            self.first_seen[s] = now
            if not quiet:
                prog = self.terms.get(s)
                ev = {"ts": round(now, 1), "market": s, "name": self._label(s),
                      "pool_day": round(float(prog.pool) / pool_days(prog, s), 2),
                      "pid": prog.pid}
                self.events.append(ev)
                del self.events[:-EVENTS_KEEP]
                self._log(event="new_boosted", market=s, pool_day=ev["pool_day"], pid=prog.pid)
        if new and not quiet:
            names = ", ".join(self._label(s)[:40] for s in new[:6])
            more = f" +{len(new) - 6} more" if len(new) > 6 else ""
            self.alert("3.0 focus: new boosted market" + ("s" if len(new) > 1 else ""),
                       f"{names}{more} — on the focus page now")
        gone = [s for s in self.markets if s not in out]
        for s in gone:
            self._log(event="left_focus", market=s,
                      note="its program no longer pays the focus bar")
        self.markets = out
        # the ground is the tender's: the engine places, pulls and
        # reprices nothing here; the bonds hand the markets over
        try:
            self.fam.freeze_dyn = set(out)
        except Exception:  # noqa: BLE001
            pass
        if self.bonds is not None:
            try:
                self.bonds.focus_out = set(out)
            except Exception:  # noqa: BLE001
                pass
        return out

    # -- the program watch -----------------------------------------------------

    def _refresh_terms(self, now: float, force: bool = False) -> None:
        batch: list[str] = []
        if force or now - self.last_terms_own >= FOCUS_TERMS_S:
            self.last_terms_own = now
            batch += list(self.markets)
        uni = sorted(self.fam.universe)
        if uni and not force and self.last_pass > 0.0:
            # the universe walk waits for the second pass: the first is
            # for the focus markets' own terms and books
            lo = self._rotor % len(uni)
            take = FOCUS_TERMS_SLICE
            batch += uni[lo:lo + take] + uni[:max(0, lo + take - len(uni))]
            self._rotor = (lo + take) % len(uni)
        batch = list(dict.fromkeys(s for s in batch if s))
        if not batch:
            return
        try:
            raw = self.client.programs(batch)
        except Exception as e:  # noqa: BLE001 — aged terms beat none
            self._log(event="terms_error", error=str(e)[:80])
            return
        live_before = sum(1 for s in batch if self.terms.get(s) is not None)
        got = sum(1 for s in batch if raw.get(s))
        if got == 0 and live_before >= 3:
            self._log(event="terms_suspect", asked=len(batch), live=live_before,
                      note="no program came back for markets that had live ones — no data")
            return
        for s in batch:
            raw.setdefault(s, {})
        sizes = {s: int((self.fam.universe.get(s) or {}).get("event_n")
                        or self.fam.event_n_seen.get(s) or 0) or 1
                 for s in batch}
        changes = self.terms.refresh(raw, sizes, now=now)
        for c in changes:
            if c.field in ("pool", "program_gone", "program_new") and (
                    c.slug in self.markets or self.is_boosted(c.slug)):
                self._log(event="terms_change", market=c.slug, field=c.field,
                          old=c.old, new=c.new)

    # -- books -------------------------------------------------------------------

    def _refresh_books(self, now: float) -> int:
        due = []
        unread = 0
        for slug in self.markets:
            age = self.fam.cache.age(slug, now)
            if age > FOCUS_BOOK_MAX_AGE_S:
                due.append((-age, slug))
                if age == float("inf"):
                    unread += 1
        n = 0
        # a market with no book at all (boot, or newly boosted) is read
        # now, all of them: the page must not wait a pass per dozen
        cap = FOCUS_BOOK_READS_BOOT if unread else FOCUS_BOOK_READS
        for _, slug in sorted(due)[:cap]:
            try:
                book = self.client.book(slug, fetched_at=now)
            except Exception:  # noqa: BLE001 — next pass
                continue
            self.fam.cache.put(slug, book)
            n += 1
        self.books_read = n
        return n

    # -- orders here -------------------------------------------------------------

    def _orders(self, slug: str, side: str | None = None) -> list[FamilyOrder]:
        return [o for o in list(self.fam.orders.values())
                if o.market == slug and (side is None or o.side == side)]

    def _is_mine(self, o: FamilyOrder) -> bool:
        """The tender's own order, by id first: the family re-labels a
        tender exit "sell" and rewrites its reason once a fill makes it
        reduce the position (13:36Z, the Senate control market), and
        the tender must still know its own."""
        return (o.id in self._mine_set or o.purpose == PURPOSE
                or str(o.why or "").startswith("focus "))

    @property
    def _mine_set(self) -> set[str]:
        return set(self.mine_ids)

    def _claim_id(self, oid: str) -> None:
        if oid and oid not in self.mine_ids:
            self.mine_ids.append(oid)
            del self.mine_ids[:-MINE_IDS_KEEP]

    def _mine(self, slug: str, side: str | None = None) -> list[FamilyOrder]:
        return [o for o in self._orders(slug, side) if self._is_mine(o)]

    def _forget(self, oid: str) -> None:
        """The tender cancelled or replaced this id: not a fill, and not
        the tender's any more (the open list lags a cancel by a read
        and the family adopts the ghost as the owner's; it drops when
        the list catches up — the tender must not cancel it twice)."""
        self._gone_by_me.add(oid)
        self._last_mine.pop(oid, None)
        self._vanished.pop(oid, None)
        if oid in self.mine_ids:
            self.mine_ids.remove(oid)

    def _journal_fills(self, oid: str, since: float) -> float:
        """Shares the family's fill journal books to this order since
        `since` — the one record that separates a fill from an order
        the open list merely left out for a read."""
        got = 0.0
        for row in list(getattr(self.fam, "fills", None) or [])[-400:]:
            if str(row.get("oid") or "") != oid:
                continue
            ts = float(row.get("ts") or 0.0)
            key = f"{oid}|{ts}"
            if ts < since - 120.0 or key in self._journaled:
                continue
            self._journaled.add(key)
            got += float(row.get("qty") or 0.0)
        if len(self._journaled) > 2000:
            self._journaled = set(list(self._journaled)[-1000:])
        return got

    @staticmethod
    def _exit_order(o: FamilyOrder) -> bool:
        return o.purpose == "sell" or str(o.why or "").startswith("focus exit")

    def _note_fills(self, now: float) -> None:
        """A tender entry that shrank or vanished is a FILL only when the
        family's journal books shares to it; then the side waits
        FOCUS_REFILL_WAIT_S before any new entry. An exit's fill is
        the position leaving — it holds nothing (owner, 2026-09-10:
        "Exit orders should never be held"). An order the open list
        left out for a read comes back untouched (the family restores
        its record); a silent cancel holds nothing."""
        cur = {o.id: o for o in list(self.fam.orders.values())
               if self._is_mine(o) and o.market in self.markets}
        for oid, rec in list(self._last_mine.items()):
            slug, side, qty = rec[0], rec[1], rec[2]
            was_exit = bool(rec[3]) if len(rec) > 3 else False
            if oid in self._gone_by_me:
                continue
            o = cur.get(oid)
            if o is None:
                self._vanished.setdefault(oid, (slug, side, qty, now, was_exit))
            elif o.qty < qty - 0.5:
                self._vanished.setdefault(oid, (slug, side, qty - o.qty, now, was_exit))
        for oid, rec in list(self._vanished.items()):
            slug, side, qty, since = rec[0], rec[1], rec[2], rec[3]
            was_exit = bool(rec[4]) if len(rec) > 4 else False
            got = self._journal_fills(oid, since)
            if got >= 0.5:
                if was_exit:
                    self._log(event="exit_filled", market=slug, side=side, qty=round(got, 2),
                              note="the position left — the side is not held")
                else:
                    self.filled_at[f"{slug}|{side}"] = now
                    self._log(event="filled", market=slug, side=side, qty=round(got, 2),
                              note=f"an entry filled — nothing new rests on this side for "
                                   f"{FOCUS_REFILL_WAIT_S / 60:g} min")
                self._vanished.pop(oid, None)
            elif oid in cur and cur[oid].qty >= qty - 0.5 and now - since < FOCUS_VANISH_WAIT_S:
                # back at full size: the list had left it out for a read
                self._vanished.pop(oid, None)
            elif now - since > FOCUS_VANISH_WAIT_S:
                self._vanished.pop(oid, None)     # gone for good, unbooked: not a fill
        self._last_mine = {oid: (o.market, o.side, o.qty, self._exit_order(o))
                           for oid, o in cur.items()}
        self._gone_by_me = {i for i in self._gone_by_me if i in cur}

    def _claim_orders(self) -> None:
        """The engine's orders on focus ground become the tender's; the
        bonds' exits become plain exits. His own stay his."""
        for o in list(self.fam.orders.values()):
            if o.market not in self.markets:
                continue
            if o.purpose in ("earn", "probe", "revive"):
                was = o.purpose
                o.purpose = PURPOSE
                o.why = f"inherited from the engine (was {was})"
                o.pinned = False
            elif o.purpose == "bond":
                o.purpose = "sell"
                o.why = "inherited from the bonds — an exit"

    @staticmethod
    def _who(o: FamilyOrder) -> str:
        if o.purpose == PURPOSE:
            return "tender"
        if o.purpose == "manual":
            return "you"
        if o.purpose == "sell":
            return "exit"
        return o.purpose

    # -- the math ----------------------------------------------------------------

    def _levels_net(self, slug: str, side: str, book) -> list:
        """The side without our own orders (every purpose), so a plan
        never credits itself; an order placed after the book was read
        is not in it and is not subtracted."""
        tick = book.tick or 0.01
        raw = list(book.side(side))
        read_at = float(getattr(book, "fetched_at", 0.0) or 0.0)
        for o in self._orders(slug, side):
            if read_at and float(o.placed_ts or 0.0) >= read_at - 1e-6:
                continue
            raw = [(p, (q - o.qty) if abs(p - o.price) < tick / 2 else q) for p, q in raw]
        return [(p, q) for p, q in raw if q > 1e-9]

    def _score(self, slug: str, side: str, book, prog, pool: float,
               fair: float | None, px: float, qty: float, levels: list,
               is_exit: bool = False) -> dict:
        """One order's numbers, a day: the reward claim, the fill odds,
        the fill's expected cost (or an exit's expected gain past
        fair), the cost of the capital, and the expected value."""
        tick = book.tick or 0.01
        j = estimate_join(side, levels, tick, float(prog.df), float(prog.target), px, qty)
        est = j.share * pool if (j.qualifies and j.in_window) else 0.0
        touch = levels[0][0] if levels else None
        if touch is None:
            ticks = 0
        else:
            d = (touch - px) if side == "BUY" else (px - touch)
            ticks = max(int(round(d / tick)), 0)
        closer = sum(q for p, q in levels
                     if ((p > px + 1e-9) if side == "BUY" else (p < px - 1e-9)))
        fm = getattr(self.fam, "fillmodel", None)
        # past his fair (an entry only): the concession is a certain
        # cost at fill, charged in full on top of the measured markdown,
        # and a mispriced order is assumed to fill faster (the fill
        # model's bait) until its own record says otherwise
        conc = 0.0
        if not is_exit and fair is not None:
            conc = max((px - fair) if side == "BUY" else (fair - px), 0.0)
        conc_ticks = int(round(conc / tick)) if conc > 0 else 0
        try:
            pf = float(fm.p_fill(slug, side, ticks, shield=closer, target=float(prog.target),
                                 bait=float(conc_ticks)))
        except Exception:  # noqa: BLE001 — the prior stands in
            pf = {0: 0.5, 1: 0.3, 2: 0.15}.get(ticks, 0.08)
        pf = min(max(pf, 0.0), 1.0)
        if is_exit:
            gain_ps = ((px - fair) if side == "SELL" else (fair - px)) if fair is not None else 0.0
            loss = -pf * qty * gain_ps            # a fill past fair is a gain
            coll = coc = risk = 0.0
            fc = 0.0
        else:
            cost_ps = px if side == "BUY" else 1.0 - px
            coll = cost_ps * qty
            try:
                base = float(fm.fill_cost(slug, side, px, None))
            except Exception:  # noqa: BLE001
                base = self.fill_floor
            fc = min(max(base, self.fill_floor), FOCUS_FILL_COST_MAX) + conc
            loss = pf * qty * fc
            coc = coll * self.coc_day
            risk = coll * max(pf, FOCUS_PF_FLOOR)
        ev = est - loss - coc
        return {"px": _r4(px), "qty": round(qty, 2), "est": round(est, 4),
                "pf": round(pf, 4), "fc": round(fc, 4), "loss": round(loss, 4),
                "coll": round(coll, 2), "coc": round(coc, 4), "ev": round(ev, 4),
                "risk": round(risk, 2), "ticks": ticks, "share": round(float(j.share), 4),
                "conc": round(conc, 4)}

    def _cands(self, side: str, book, fair: float | None,
               bound: bool = False, improve: bool = True) -> list[float]:
        """Candidate prices, nearest first: a tick inside the touch when
        the spread allows, the touch, out to FOCUS_BEHIND_MAX behind it,
        and the slot a tick inside his fair. With `bound` (an exit)
        nothing past his fair; an entry may sit past it (owner,
        2026-09-10: "A bid over fair value is fine as long as it is
        appropriately sized for the risk and rewards it can earn") —
        the concession is charged in _score and the size chosen with
        the price."""
        tick = book.tick or 0.01
        own = book.side(side)
        other = book.side("SELL" if side == "BUY" else "BUY")
        touch = own[0][0] if own else None
        opp = other[0][0] if other else None
        sign = 1.0 if side == "BUY" else -1.0
        if touch is None and opp is None:
            return []
        start = touch if touch is not None else opp - sign * tick
        out = []
        if improve and touch is not None and opp is not None and abs(opp - touch) > 1.5 * tick:
            out.append(touch + sign * tick)       # improve by a tick, never cross
        for k in range(0, FOCUS_BEHIND_MAX + 1):
            out.append(start - k * sign * tick)
        out = [round(p, 4) for p in out]
        if fair is not None:
            edge = fair - tick if side == "BUY" else fair + tick
            if bound:
                # an exit's bound is inclusive: at the price itself is fine
                edge = fair
                out = [p for p in out
                       if ((p <= edge + 1e-9) if side == "BUY" else (p >= edge - 1e-9))]
            # the slot a tick inside his fair is always a candidate
            b = round(edge, 4)
            if 0.001 <= b <= 0.999 and b not in out and (
                    (opp is None) or ((b < opp - 1e-9) if side == "BUY" else (b > opp + 1e-9))):
                out.append(b)
        out = [p for p in out if 0.001 <= p <= 0.999
               and (opp is None or ((p < opp - 1e-9) if side == "BUY" else (p > opp + 1e-9)))]
        return list(dict.fromkeys(out))

    def _entry_plan(self, slug: str, side: str, book, prog, pool: float,
                    fair: float | None, stake: float) -> dict | None:
        """The best resting order of `stake` dollars on this side, or
        None with no price within the fair bound."""
        if stake < 1.0:
            return None
        levels = self._levels_net(slug, side, book)
        best = None
        for px in self._cands(side, book, fair):
            cost_ps = px if side == "BUY" else 1.0 - px
            if cost_ps <= 0:
                continue
            tried: set[float] = set()
            for frac in FOCUS_SIZE_FRACS:
                qty = float(math.floor(stake * frac / cost_ps))
                if qty < 1.0 or qty in tried:
                    continue
                tried.add(qty)
                s = self._score(slug, side, book, prog, pool, fair, px, qty, levels)
                if best is None or s["ev"] > best["ev"] + 1e-9:
                    best = s
        return best

    def _exit_plan(self, slug: str, side: str, book, prog, pool: float,
                   fair: float | None, qty: float, basis: float | None) -> dict | None:
        """Where `qty` held shares exit on this side: at the touch, or
        the nearest slot behind it that is not under the position's
        own cost (his fair standing in when the cost is unknown).
        Exits join the touch, never sit inside it, and never sell under
        cost (2026-09-10: exits bounded by fair sat eight ticks behind
        the touch while the tender kept opening more at the touch)."""
        if qty < 1.0:
            return None
        levels = self._levels_net(slug, side, book)
        bound = basis if basis is not None else fair
        cands = self._cands(side, book, bound, bound=bound is not None, improve=False)
        if not cands:
            return None
        px = cands[0]                             # nearest the touch first
        out = self._score(slug, side, book, prog, pool, fair, px, qty, levels, is_exit=True)
        out["basis"] = basis
        return out

    # -- the pass ----------------------------------------------------------------

    def cycle(self, now: float, positions: dict | None, on: bool) -> dict:
        t0 = time.time()
        with self.lock:
            self._refresh_terms(now)
            self.refresh_markets(now)
            self._claim_orders()
            self._note_fills(now)            # before the plans: a fill holds its side
            self._refresh_books(now)
            bp = self.stake_bp(now)
            positions = positions or {}
            self._plan_all(now, positions, bp)
            acted = self._tend(now, positions, on) if on else 0
            if not on:
                self.note = "the focus switch is off — showing, not tending"
            elif self._blocked():
                # the desk's breaker (2026-09-10, 12:23Z: the third
                # address of the day read as a VPN and the tender sat
                # silent with a +$141/day plan): say so on the page
                self.note = ("the exchange refuses this server's orders as a VPN "
                             "— nothing rests or moves until a Deploy tap gives "
                             "it a new address; your own taps still try")
                if now - self._blocked_noted > 900.0:
                    self._blocked_noted = now
                    self._log(event="blocked", note="placements refused as a VPN — "
                                                    "the tender waits for a new address")
            else:
                self.note = ""
            self.last_pass = now
            self.pass_s = round(time.time() - t0, 2)
            self._freeze(now, bp, on)
        return {"markets": len(self.markets), "acted": acted}

    def _plan_all(self, now: float, positions: dict, bp: float | None) -> None:
        rows: dict[str, dict] = {}
        for slug in self.markets:
            rows[slug] = self._row(slug, now, positions, bp)
        self.rows = rows

    def _row(self, slug: str, now: float, positions: dict, bp: float | None) -> dict:
        prog = self.terms.get(slug)
        fair = self.fairs.get(slug)
        silver = None
        try:
            silver = self.model_fair(slug)
        except Exception:  # noqa: BLE001
            pass
        stake, stake_src = self.stake(slug, bp)
        net, cost = (positions.get(slug) or (0.0, 0.0))[:2] if positions.get(slug) else (0.0, 0.0)
        net, cost = float(net or 0.0), float(cost or 0.0)
        book = self.fam.cache.any_age(slug)
        age = self.fam.cache.age(slug, now)
        row = {"market": slug, "name": self._label(slug),
               "fair": fair, "silver": silver, "stake": stake, "stake_src": stake_src,
               "paused": slug in self.paused,
               "by_hand": self.by_hand(slug),
               "released": slug in self.released,
               "not_tended": self.why_not_tended(slug),
               "held": bool(self.fam.held_ground(slug)),
               "position": ({"qty": round(net, 2), "cost": round(cost, 2),
                             "cost_px": round(abs(cost / net), 4) if abs(net) > 0.005 else None}
                            if abs(net) > 0.005 else None),
               "first_seen": self.first_seen.get(slug, 0.0),
               "orders": [], "sides": {}, "ev": None, "book": None, "prog": None}
        if prog is not None:
            row["prog"] = {"pool_day": round(float(prog.pool) / pool_days(prog, slug), 2),
                           "target": float(prog.target), "df": float(prog.df),
                           "pid": prog.pid, "n": int(prog.event_n or 1)}
        pool = self.fam._side_pool(slug, prog) if prog is not None else None
        if pool:
            row["prog"]["side_pool"] = round(pool, 2)
        if book is not None:
            row["book"] = {"bid": book.bids[0][0] if book.bids else None,
                           "bid_q": round(book.bids[0][1], 1) if book.bids else 0.0,
                           "ask": book.asks[0][0] if book.asks else None,
                           "ask_q": round(book.asks[0][1], 1) if book.asks else 0.0,
                           "age_s": round(age, 1) if age != float("inf") else None,
                           "tick": book.tick,
                           # the book itself (owner, 2026-09-10: "I need to
                           # be able to see the book on focus markets")
                           "bids": [[p, round(q, 1)] for p, q in book.bids[:8]],
                           "asks": [[p, round(q, 1)] for p, q in book.asks[:8]]}
        # every order here, with what it measures on this book
        for o in self._orders(slug):
            d = {"id": o.id, "side": o.side, "price": o.price, "qty": o.qty,
                 "purpose": o.purpose, "who": self._who(o), "why": (o.why or "")[:120],
                 "age_s": round(now - float(o.placed_ts or now), 0)}
            if book is not None and prog is not None and pool:
                levels = self._levels_net(slug, o.side, book)
                is_exit = o.purpose == "sell" or (
                    (o.side == "SELL" and net > 0.005) or (o.side == "BUY" and net < -0.005))
                s = self._score(slug, o.side, book, prog, pool, fair, o.price, o.qty,
                                levels, is_exit=is_exit)
                d.update(est=s["est"], pf=s["pf"], ev=s["ev"], risk=s["risk"],
                         ticks=s["ticks"], exit=is_exit)
                o.live_est = s["est"]
                o.live_pf = s["pf"]
                o.live_ev = s["ev"]
            row["orders"].append(d)
        row["orders"].sort(key=lambda d: (d["side"], -d["price"]))
        # the entry of 10% of buying power at the optimal price, each side
        # (the list's sort key), and what the tender itself would rest
        # under its own rules: the refill wait, the position bound, the
        # exit of what is held
        best_ev = None
        row["tend"] = {}
        basis = None
        if abs(net) > 0.005 and cost > 0:
            per = cost / abs(net)                 # the exchange's cost a share
            basis = round(per if net > 0 else 1.0 - per, 4)
        if book is not None and prog is not None and pool and age <= 3600.0:
            for side in ("BUY", "SELL"):
                use_fair = fair if fair is not None else silver
                plan = self._entry_plan(slug, side, book, prog, pool, use_fair, stake)
                if plan is None:
                    row["sides"][side] = {"note": ("nothing earns on this side"
                                                   if stake >= 1.0 else "no stake")}
                    continue
                plan["fair_used"] = ("yours" if fair is not None
                                     else "silver" if silver is not None else "none")
                row["sides"][side] = plan
                if best_ev is None or plan["ev"] > best_ev:
                    best_ev = plan["ev"]
            xs = "SELL" if net > 0.005 else "BUY" if net < -0.005 else None
            for side in ("BUY", "SELL"):
                if side == xs:
                    continue
                key = f"{slug}|{side}"
                since_fill = now - self.filled_at.get(key, 0.0)
                wait = FOCUS_REFILL_WAIT_S - since_fill
                if wait > 0:
                    at = time.strftime("%H:%M", time.gmtime(self.filled_at[key]))
                    row["tend"][side] = {"note": f"an entry filled at {at}Z — nothing new on "
                                                 f"this side for {wait / 60:.0f} min more",
                                         "hold": True}
                    continue
                room = stake
                adds = (side == "BUY" and net > 0.005) or (side == "SELL" and net < -0.005)
                if adds:
                    held_coll = cost                  # the exchange's collateral held here
                    room = stake - held_coll
                    if room < 1.0:
                        row["tend"][side] = {"note": f"holding ${held_coll:,.0f} here already "
                                                     f"— no entry that adds to it", "hold": True}
                        continue
                scale = 1.0
                if since_fill < FOCUS_REFILL_SCALE_S:
                    # back in at a quarter of the stake, ramping to the
                    # full size by two hours after the fill — applied to
                    # the room the position bound leaves, not the whole stake
                    ramp = (since_fill - FOCUS_REFILL_WAIT_S) / max(
                        FOCUS_REFILL_SCALE_S - FOCUS_REFILL_WAIT_S, 1.0)
                    scale = min(max(FOCUS_REFILL_FLOOR + (1.0 - FOCUS_REFILL_FLOOR) * ramp,
                                    FOCUS_REFILL_FLOOR), 1.0)
                    room = room * scale
                if fair is None:
                    row["tend"][side] = {"note": "no fair set", "hold": True}
                    continue
                plan = self._entry_plan(slug, side, book, prog, pool, fair, room)
                if plan and scale < 1.0:
                    plan["scale"] = round(scale, 2)
                    plan["scale_note"] = (f"{scale * 100:.0f}% of the stake — filled "
                                          f"{since_fill / 60:.0f} min ago, full size at 2 h")
                row["tend"][side] = plan if plan else {"note": "nothing earns on this side"}
            # the exit of what is held: at the touch, never under cost
            if xs is not None:
                others = sum(o.qty for o in self._orders(slug, xs) if not self._is_mine(o))
                q = float(math.floor(abs(net) - others))
                xp = (self._exit_plan(slug, xs, book, prog, pool, fair, q, basis)
                      if q >= 1.0 else None)
                row["exit"] = ({"side": xs, **xp} if xp else
                               {"side": xs, "note": ("your own orders already offer the lot"
                                                     if q < 1.0 else "no price at or past cost on this book")})
                row["tend"][xs] = dict(row["exit"], exit=True) if xp else row["exit"]
        elif book is None:
            row["note"] = "no book read yet"
        elif prog is None:
            row["note"] = "no terms read yet"
        elif not pool:
            row["note"] = "event size unconfirmed — no estimate"
        row["ev"] = best_ev
        return row

    # -- tending -----------------------------------------------------------------

    def risk_used(self) -> float:
        tot = 0.0
        for o in list(self.fam.orders.values()):
            if o.purpose != PURPOSE or o.market not in self.markets:
                continue
            pf = float(o.live_pf) if o.live_pf is not None else 1.0
            tot += capital_at_risk(o.intent, o.price, o.qty) * max(pf, FOCUS_PF_FLOOR)
        return round(tot, 2)

    def _blocked(self) -> bool:
        """The desk's placement breaker: the exchange refused the last
        placement from this address as a VPN."""
        h = getattr(self.fam.desk, "health", None)
        try:
            return bool(h is not None and h.blocked())
        except Exception:  # noqa: BLE001
            return False

    def _tend(self, now: float, positions: dict, on: bool) -> int:
        """One order a side a market where he has set a fair: rested at
        the best EV slot, kept while it keeps FOCUS_KEEP of the best,
        moved on the cooldown, pulled when nothing earns. The loss cap
        binds across markets, best EV first."""
        actions = FOCUS_ACTIONS_PER_PASS
        blocked = self._blocked()
        # over the cap: the weakest tender orders come off first
        used = self.risk_used()
        if used > self.loss_cap + 1e-9:
            mine = sorted((o for o in list(self.fam.orders.values())
                           if o.purpose == PURPOSE and o.market in self.markets),
                          key=lambda o: (o.live_ev if o.live_ev is not None else 0.0))
            for o in mine:
                if used <= self.loss_cap or actions <= 0:
                    break
                r = self.fam.desk.cancel(o.id, o.market, initiator="auto")
                if r.ok:
                    self.fam.orders.pop(o.id, None)
                    self._forget(o.id)
                    used -= capital_at_risk(o.intent, o.price, o.qty) * max(
                        float(o.live_pf if o.live_pf is not None else 1.0), FOCUS_PF_FLOOR)
                    actions -= 1
                    self._log(event="pull", market=o.market, side=o.side, price=o.price,
                              qty=o.qty, why=f"over the ${self.loss_cap:,.0f} loss cap")
        wants: list[tuple[float, str, str, dict, bool]] = []
        for slug in self.markets:
            row = self.rows.get(slug) or {}
            if row.get("not_tended"):
                # not the tender's to work: its own orders here come off
                for o in self._mine(slug):
                    if actions <= 0:
                        break
                    r = self.fam.desk.cancel(o.id, slug, initiator="auto")
                    if r.ok:
                        self.fam.orders.pop(o.id, None)
                        self._forget(o.id)
                        actions -= 1
                        self._log(event="pull", market=slug, side=o.side, price=o.price,
                                  qty=o.qty, why=row["not_tended"])
                continue
            for side in ("BUY", "SELL"):
                plan = (row.get("tend") or {}).get(side) or {}
                is_exit = bool(plan.get("exit"))
                note, hold = plan.get("note"), bool(plan.get("hold"))
                plan = plan if plan.get("px") else None
                wants.append((plan["ev"] if plan else -1.0, slug, side,
                              plan or {"note": note, "hold": hold}, is_exit))
        wants.sort(key=lambda t: -t[0])
        for ev, slug, side, plan, is_exit in wants:
            if actions <= 0:
                break
            mine = self._mine(slug, side)
            # one order a side: extras come off
            while len(mine) > 1 and actions > 0:
                o = mine.pop()
                r = self.fam.desk.cancel(o.id, slug, initiator="auto")
                if r.ok:
                    self.fam.orders.pop(o.id, None)
                    self._forget(o.id)
                    actions -= 1
                    self._log(event="pull", market=slug, side=side, price=o.price,
                              qty=o.qty, why="one order a side")
            cur = mine[0] if mine else None
            if not plan or not plan.get("px") or (plan["ev"] <= 0.0 and not is_exit):
                # a resting order is judged by ITS OWN expected value, not
                # by today's plan at a stake that moved (13:43-13:46Z: the
                # balance-of-power ask was pulled and re-rested three times
                # in three minutes on buying-power dips); a hold — a fill,
                # a position past the stake, no fair — always pulls it
                own_ev = float(cur.live_ev) if (cur is not None and cur.live_ev is not None) else None
                key = f"{slug}|{side}"
                if (cur is not None and not (plan or {}).get("hold")
                        and own_ev is not None and own_ev > 0.0):
                    self.weak_since.pop(key, None)
                    continue
                if cur is not None and not (plan or {}).get("hold"):
                    # under zero: only a reading that has held for the
                    # dwell pulls it (a tenth-cent flicker does not)
                    since = self.weak_since.setdefault(key, now)
                    if now - since < FOCUS_WEAK_DWELL_S:
                        continue
                if cur is not None:
                    r = self.fam.desk.cancel(cur.id, slug, initiator="auto")
                    if r.ok:
                        self.fam.orders.pop(cur.id, None)
                        self._forget(cur.id)
                        self.weak_since.pop(key, None)
                        actions -= 1
                        self._log(event="pull", market=slug, side=side, price=cur.price,
                                  qty=cur.qty,
                                  why=(plan.get("note") or "nothing earns on this side"
                                       if not plan or not plan.get("px")
                                       else f"expected value {plan['ev']:+.2f}/day for "
                                            f"{FOCUS_WEAK_DWELL_S / 60:.0f} min"))
                continue
            book = self.fam.cache.fresh(slug, FOCUS_ACT_AGE_S, now)
            if book is None or blocked:
                continue
            key = f"{slug}|{side}"
            self.weak_since.pop(key, None)
            if cur is None:
                if not is_exit and used + plan["risk"] > self.loss_cap + 1e-9:
                    continue                      # the cap: the best EV got in first
                n_mine = sum(1 for o in list(self.fam.orders.values()) if o.purpose == PURPOSE)
                if n_mine >= FOCUS_MAX_ORDERS and not is_exit:
                    continue                      # an exit is never held back
                r = self.fam.desk.place_resting(slug, side, plan["px"], plan["qty"],
                                                net_position=float((positions.get(slug) or (0.0,))[0] or 0.0),
                                                initiator="auto")
                actions -= 1
                rested = plan["qty"] if r.ok else (r.resting_qty if r.order_id and r.resting_qty >= 1.0 else 0.0)
                if r.order_id and rested >= 1.0:
                    self._claim_id(r.order_id)
                    self.fam.orders[r.order_id] = FamilyOrder(
                        id=r.order_id, market=slug, side=side, price=(r.price or plan["px"]),
                        qty=rested, intent=r.intent, placed_ts=now, purpose=PURPOSE,
                        why=self._why(plan, is_exit), est_day=plan["est"],
                        live_est=plan["est"], live_pf=plan["pf"], live_ev=plan["ev"])
                    self.moved_at[key] = now
                    self._last_mine[r.order_id] = (slug, side, rested, is_exit)
                    if not is_exit:
                        used += plan["risk"]
                    self._log(event="rested", market=slug, side=side, price=(r.price or plan["px"]),
                              qty=rested, est=plan["est"], pf=plan["pf"], ev=plan["ev"],
                              exit=is_exit, note=("trimmed by the exchange" if not r.ok else ""))
                else:
                    self._log(event="refused", market=slug, side=side, price=plan["px"],
                              qty=plan["qty"], note=r.note[:140])
                continue
            # a resting order: keep, resize or move
            same_px = abs(cur.price - plan["px"]) < 1e-9
            size_ok = abs(cur.qty - plan["qty"]) <= max(1.0, 0.10 * plan["qty"])
            if same_px and size_ok:
                continue
            cur_ev = float(cur.live_ev if cur.live_ev is not None else 0.0)
            keeps = same_px or cur_ev >= FOCUS_KEEP * plan["ev"] - 1e-9
            if keeps and size_ok:
                continue
            cooldown = FOCUS_EXIT_COOLDOWN_S if is_exit else FOCUS_MOVE_COOLDOWN_S
            if now - self.moved_at.get(key, 0.0) < cooldown:
                continue
            cur_risk = 0.0
            if not is_exit:
                # a resize up is new money at risk: the cap binds it too
                cur_risk = capital_at_risk(cur.intent, cur.price, cur.qty) * max(
                    float(cur.live_pf if cur.live_pf is not None else 1.0), FOCUS_PF_FLOOR)
                if used - cur_risk + plan["risk"] > self.loss_cap + 1e-9:
                    continue
            r = self.fam.desk.reprice(
                {"id": cur.id, "market": slug, "side": side, "price": cur.price,
                 "size": cur.qty, "intent": cur.intent},
                plan["px"], plan["qty"], initiator="auto", keep_trimmed=True)
            actions -= 1
            if not (r.ok or (r.order_id and r.resting_qty >= 1.0)):
                self._log(event="move_refused", market=slug, side=side, price=plan["px"],
                          qty=plan["qty"], note=r.note[:140])
                continue
            rested = plan["qty"] if r.ok else r.resting_qty
            if r.two_orders:
                cur.why = "cancel failed during a move — retrying"
            else:
                self.fam.orders.pop(cur.id, None)
            self._forget(cur.id)
            self._claim_id(r.order_id)
            self.fam.orders[r.order_id] = FamilyOrder(
                id=r.order_id, market=slug, side=side, price=(r.price or plan["px"]),
                qty=rested, intent=cur.intent, placed_ts=now, purpose=PURPOSE,
                why=self._why(plan, is_exit), est_day=plan["est"],
                live_est=plan["est"], live_pf=plan["pf"], live_ev=plan["ev"])
            self.moved_at[key] = now
            self._last_mine[r.order_id] = (slug, side, rested, is_exit)
            if not is_exit:
                used += plan["risk"] - cur_risk
            self._log(event="moved", market=slug, side=side, price=(r.price or plan["px"]),
                      qty=rested, was=cur.price, est=plan["est"], pf=plan["pf"], ev=plan["ev"],
                      exit=is_exit)
        return FOCUS_ACTIONS_PER_PASS - actions

    @staticmethod
    def _why(plan: dict, is_exit: bool) -> str:
        kind = "exit" if is_exit else "entry"
        return (f"focus {kind}: ~${plan['est']:.2f}/day, fill odds {plan['pf'] * 100:.0f}%/day, "
                f"EV {plan['ev']:+.2f}/day")

    # -- his taps ------------------------------------------------------------------

    def set_fair(self, slug: str, cents) -> dict:
        with self.lock:
            if slug not in self.markets and slug not in self.fam.universe:
                return {"ok": False, "note": "not a market the focus knows"}
            if cents in (None, "", "-"):
                had = self.fairs.pop(slug, None)
                self._log(event="fair_cleared", market=slug)
                return {"ok": True, "note": ("fair cleared — shown only now" if had is not None
                                             else "no fair was set")}
            try:
                px = round(float(cents) / 100.0, 4)
            except (TypeError, ValueError):
                return {"ok": False, "note": "the fair goes in cents, like 46.5"}
            if not (0.001 <= px <= 0.999):
                return {"ok": False, "note": "fair must be 0.1c to 99.9c"}
            self.fairs[slug] = px
            self.paused.discard(slug)
            self._log(event="fair_set", market=slug, fair=px)
            why = self.why_not_tended(slug)
            return {"ok": True, "note": f"fair set at {px * 100:g}c — "
                                        + (f"not tended: {why}" if why else "the tender works from it now")}

    def set_stake(self, slug: str, usd) -> dict:
        with self.lock:
            if usd in (None, "", "-"):
                self.stakes.pop(slug, None)
                self._log(event="stake_cleared", market=slug)
                return {"ok": True, "note": f"stake back to {FOCUS_STAKE_FRAC * 100:g}% of buying power"}
            try:
                v = float(usd)
            except (TypeError, ValueError):
                return {"ok": False, "note": "the stake goes in dollars"}
            if v < 0:
                return {"ok": False, "note": "the stake cannot be negative"}
            self.stakes[slug] = round(v, 2)
            self._log(event="stake_set", market=slug, usd=v)
            return {"ok": True, "note": f"stake ${v:,.2f} a side here"}

    def pause(self, slug: str, on: bool) -> dict:
        with self.lock:
            if on:
                self.paused.add(slug)
                n = self.pull(slug, "paused by you").get("n", 0)
                self._log(event="paused", market=slug)
                return {"ok": True, "note": f"paused — {n} tender order{'s' if n != 1 else ''} pulled; "
                                            "your own orders stay"}
            self.paused.discard(slug)
            self._log(event="resumed", market=slug)
            return {"ok": True, "note": "resumed — the tender works it again next pass"}

    def release(self, slug: str, on: bool) -> dict:
        """His tap: a market comes off his hand's list and the tender may
        work it (with a fair set); or goes back, the tender's orders
        pulled and his own left alone."""
        with self.lock:
            if slug not in self.markets and slug not in self.fam.universe:
                return {"ok": False, "note": "not a market the focus knows"}
            if on:
                if not self.fam._avoided(slug):
                    return {"ok": False, "note": "this market is not on your hand's list"}
                self.released.add(slug)
                self._log(event="released", market=slug,
                          note="off the hand's list — the tender may work it")
                why = self.why_not_tended(slug)
                return {"ok": True, "note": "off your hand's list — "
                                            + (f"not tended yet: {why}" if why
                                               else "the tender works it from your fair")}
            if slug not in self.released:
                return {"ok": False, "note": "this market was never released"}
            self.released.discard(slug)
            n = self.pull(slug, "back on your hand's list").get("n", 0)
            self._log(event="unreleased", market=slug)
            return {"ok": True, "note": f"back on your hand's list — {n} tender order"
                                        f"{'s' if n != 1 else ''} pulled; your own stay"}

    def pull(self, slug: str, why: str = "pulled by you") -> dict:
        with self.lock:
            n = 0
            for o in self._mine(slug):
                r = self.fam.desk.cancel(o.id, slug, initiator="owner")
                if r.ok:
                    self.fam.orders.pop(o.id, None)
                    self._forget(o.id)
                    n += 1
                    self._log(event="pull", market=slug, side=o.side, price=o.price,
                              qty=o.qty, why=why)
            return {"ok": True, "n": n, "note": f"{n} tender order{'s' if n != 1 else ''} pulled"}

    def place(self, slug: str, side: str, cents, qty, net: float = 0.0) -> dict:
        """His own order here, by his tap: bypasses the switches, keeps
        every other rail, and the tender leaves it alone."""
        with self.lock:
            side = str(side or "").upper()
            if side not in ("BUY", "SELL"):
                return {"ok": False, "note": "side must be BUY or SELL"}
            try:
                px = round(float(cents) / 100.0, 4)
                q = round(float(qty), 2)
            except (TypeError, ValueError):
                return {"ok": False, "note": "price in cents and a share count, please"}
            if q < 0.01:
                return {"ok": False, "note": "how many shares?"}
            if not self.fam.knows(slug):
                return {"ok": False, "note": "not a market this family knows"}
            r = self.fam.desk.place_resting(slug, side, px, q, net_position=net,
                                            initiator="owner", verify=True)
            rested = q if r.ok else (r.resting_qty if r.order_id and r.resting_qty >= 0.01 else 0.0)
            if r.order_id and rested >= 0.01:
                self.fam.orders[r.order_id] = FamilyOrder(
                    id=r.order_id, market=slug, side=side, price=(r.price or px), qty=rested,
                    intent=r.intent, placed_ts=self._clock(), purpose="manual",
                    why="placed by you on the focus page")
                self._log(event="his_place", market=slug, side=side, price=(r.price or px), qty=rested)
                return {"ok": True, "note": r.note + (f" — {rested:g} resting" if not r.ok else ""),
                        "order_id": r.order_id}
            self._log(event="his_place_refused", market=slug, side=side, price=px, qty=q,
                      note=r.note[:140])
            return {"ok": False, "note": r.note}

    def cancel(self, slug: str, order_id: str) -> dict:
        with self.lock:
            rec = self.fam.orders.get(str(order_id or ""))
            if rec is None:
                return {"ok": False, "note": "not an order 3.0 tracks — it may already be gone"}
            r = self.fam.desk.cancel(rec.id, rec.market, initiator="owner")
            if r.ok:
                self.fam.orders.pop(rec.id, None)
                self._forget(rec.id)
                self._log(event="his_cancel", market=rec.market, side=rec.side,
                          price=rec.price, qty=rec.qty, was=self._who(rec))
            return {"ok": r.ok, "note": r.note}

    def move(self, slug: str, order_id: str, cents=None, qty=None) -> dict:
        """His move or resize of any order here — his own, the engine's
        or the tender's. What he touches becomes his: the tender leaves
        it where he put it."""
        with self.lock:
            rec = self.fam.orders.get(str(order_id or ""))
            if rec is None:
                return {"ok": False, "note": "not an order 3.0 tracks — it may already be gone"}
            try:
                new_px = round(float(cents) / 100.0, 4) if cents not in (None, "") else rec.price
                new_q = round(float(qty), 2) if qty not in (None, "") else rec.qty
            except (TypeError, ValueError):
                return {"ok": False, "note": "price in cents and a share count, please"}
            if abs(new_px - rec.price) < 1e-9 and abs(new_q - rec.qty) < 1e-9:
                return {"ok": False, "note": "nothing to change"}
            r = self.fam.desk.reprice(
                {"id": rec.id, "market": rec.market, "side": rec.side, "price": rec.price,
                 "size": rec.qty, "intent": rec.intent},
                new_px, new_q if abs(new_q - rec.qty) > 1e-9 else None,
                initiator="owner", keep_trimmed=True)
            if not (r.ok or (r.order_id and r.resting_qty >= 0.01)):
                return {"ok": False, "note": r.note}
            rested = new_q if r.ok else r.resting_qty
            if r.two_orders:
                rec.why = "cancel failed during a move — retrying"
            else:
                self.fam.orders.pop(rec.id, None)
            self._forget(rec.id)
            self.fam.orders[r.order_id] = FamilyOrder(
                id=r.order_id, market=rec.market, side=rec.side, price=(r.price or new_px),
                qty=rested, intent=rec.intent, placed_ts=self._clock(), purpose="manual",
                why="moved by you on the focus page — the tender leaves it alone")
            self._log(event="his_move", market=rec.market, side=rec.side, was=rec.price,
                      price=(r.price or new_px), qty=rested, of=self._who(rec))
            return {"ok": True, "note": r.note + (f" — {rested:g} resting" if not r.ok else ""),
                    "order_id": r.order_id}

    def set_number(self, which: str, value) -> dict:
        with self.lock:
            try:
                v = float(value)
            except (TypeError, ValueError):
                return {"ok": False, "note": "a number, please"}
            if which == "coc":
                if not (0.0 <= v <= 10.0):
                    return {"ok": False, "note": "cost of capital in % a day, 0 to 10"}
                self.coc_day = round(v / 100.0, 5)
                note = f"cost of capital {v:g}% a day"
            elif which == "floor":
                if not (0.0 <= v <= 25.0):
                    return {"ok": False, "note": "fill cost floor in cents a share, 0 to 25"}
                self.fill_floor = round(v / 100.0, 4)
                note = f"fill cost never reads under {v:g}c a share"
            elif which == "cap":
                if not (0.0 <= v <= 100000.0):
                    return {"ok": False, "note": "the loss cap in dollars"}
                self.loss_cap = round(v, 2)
                note = f"expected loss cap ${v:,.0f}"
            else:
                return {"ok": False, "note": f"unknown number {which}"}
            self._log(event="number", which=which, value=v)
            return {"ok": True, "note": note}

    def scan_now(self, now: float) -> dict:
        with self.lock:
            self._refresh_terms(now, force=True)
            before = set(self.markets)
            self.refresh_markets(now)
            new = [s for s in self.markets if s not in before]
            return {"ok": True, "note": f"terms re-read for {len(self.markets)} focus markets"
                                        + (f" — {len(new)} new" if new else "")}

    # -- the page ----------------------------------------------------------------

    def view(self, now: float, bp: float | None, on: bool) -> dict:
        rows = sorted(self.rows.values(),
                      key=lambda r: (-(r["ev"] if r.get("ev") is not None else -1e9), r["name"]))
        stake, src = self.stake("-", bp)
        return {"ok": True, "at": round(now, 1), "pass_s": self.pass_s,
                "n": len(self.markets), "bp": bp, "stake": stake, "stake_src": src,
                "loss_cap": self.loss_cap, "risk_used": self.risk_used(),
                "coc_day": self.coc_day, "fill_floor": self.fill_floor,
                "on": bool(on), "note": self.note, "blocked": self._blocked(),
                "tended": sum(1 for r in rows if not r.get("not_tended")),
                "mine": sum(1 for o in list(self.fam.orders.values())
                            if o.purpose == PURPOSE and o.market in self.markets),
                "books_read": self.books_read,
                "pool_min": FOCUS_POOL_MIN_USD,
                "events": list(reversed(self.events[-20:])),
                "log": list(reversed(self.log[-40:])),
                "rows": rows}

    def _freeze(self, now: float, bp: float | None, on: bool) -> None:
        try:
            self.payload_json = json.dumps(self.view(now, bp, on)).encode()
        except Exception as e:  # noqa: BLE001 — a stale page beats none
            self.note = f"page freeze failed: {e}"

    def refreeze(self) -> None:
        with self.lock:
            now = self._clock()
            self._freeze(now, self._bp[0] if self._bp else None, bool(self.switch_on()))

    # -- persistence ---------------------------------------------------------------

    def to_dict(self) -> dict:
        return {"fairs": dict(self.fairs), "stakes": dict(self.stakes),
                "paused": sorted(self.paused), "released": sorted(self.released),
                "coc_day": self.coc_day,
                "fill_floor": self.fill_floor, "loss_cap": self.loss_cap,
                "first_seen": dict(self.first_seen), "moved_at": dict(self.moved_at),
                "filled_at": dict(self.filled_at), "mine_ids": list(self.mine_ids[-MINE_IDS_KEEP:]),
                "events": self.events[-EVENTS_KEEP:], "log": self.log[-LOG_KEEP:]}

    def restore(self, d: dict) -> None:
        if not d:
            return
        self.fairs = {str(k): float(v) for k, v in (d.get("fairs") or {}).items()}
        self.stakes = {str(k): float(v) for k, v in (d.get("stakes") or {}).items()}
        self.paused = {str(s) for s in (d.get("paused") or [])}
        self.released = {str(s) for s in (d.get("released") or [])}
        self.coc_day = float(d.get("coc_day") or FOCUS_COC_DAY)
        self.fill_floor = float(d.get("fill_floor") if d.get("fill_floor") is not None
                                else FOCUS_FILL_COST_MIN)
        self.loss_cap = float(d.get("loss_cap") or FOCUS_LOSS_CAP_USD)
        self.first_seen = {str(k): float(v) for k, v in (d.get("first_seen") or {}).items()}
        self.moved_at = {str(k): float(v) for k, v in (d.get("moved_at") or {}).items()}
        self.filled_at = {str(k): float(v) for k, v in (d.get("filled_at") or {}).items()}
        self.mine_ids = [str(x) for x in (d.get("mine_ids") or [])][-MINE_IDS_KEEP:]
        self.events = list(d.get("events") or [])[-EVENTS_KEEP:]
        self.log = list(d.get("log") or [])[-LOG_KEEP:]
