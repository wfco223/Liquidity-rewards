"""Bonds: the tax reserve earning like a bond (owner, 2026-09-02).

The spec and its corrections, in bonds.py's docstring. Here: the
nightly Silver check proposes at both ends and drops silently; the
owner alone adds; the engine keeps quoting bond markets but exits only
its own non-bond stock; the bond ledger is the module's own; a held
bond's order sits behind the touch keeping 60% of the best reward and
never under cost; a minnow in front is led down by a decoy and taken
at its price; a hand sale is never reinvested; one ping per $100 bought;
nothing is bought where no bond sale of ours rests.
"""

import calendar
import copy
import unittest

from v3.bonds import (DANCE_MAX_MOVES, DANCE_WAIT_S, DECOY_QTY, HIGH_ODDS,
                      KEEP_FRACTION, LOW_ODDS, MINNOW_MAX, PING_EVERY_USD,
                      Bonds, scan_due, side_for)
from v3.family import FamilyConfig, FamilyOrder
from v3.intents import BUY_LONG, BUY_SHORT, SELL_LONG, SELL_SHORT
from v3.scoring import Book
from v3.tests.test_family import LIVE_PROG, Rig

AL = "usgubewc-usgub-al-2026-11-03-rep"     # Silver 99.4 for YES: a YES bond
TN = "usgubewc-usgub-tn-2026-11-03-rep"     # Silver 99.1
ALD = "usgubewc-usgub-al-2026-11-03-dem"    # Silver 0.6 for YES: a NO bond
GA = "usgubewc-usgub-ga-2026-11-03-rep"     # Silver 71: not a bond


def yes_book(now, bid=0.98, ask=0.99, bid_q=50.0, ask_q=300.0):
    return Book(bids=((bid, bid_q), (0.50, 20000.0)),
                asks=((ask, ask_q), (0.999, 20000.0)),
                tick=0.01, fetched_at=now)


def no_book(now, bid=0.01, ask=0.02, bid_q=400.0, ask_q=60.0):
    return Book(bids=((bid, bid_q), (0.001, 20000.0)),
                asks=((ask, ask_q), (0.60, 20000.0)),
                tick=0.01, fetched_at=now)


def minnow_book(now, minnows=5.0, ask=0.90, extra=()):
    # a few rewards-seeking shares at the touch, the qualifying wall
    # nine ticks back at 99c where it barely weighs
    asks = tuple(sorted(((ask, minnows),) + tuple(extra) + ((0.99, 20000.0),)))
    return Book(bids=((0.88, 50.0), (0.50, 20000.0)), asks=asks,
                tick=0.01, fetched_at=now)


class Base(unittest.TestCase):
    def setUp(self):
        self.r = Rig(cfg=FamilyConfig(name="Politics", tag="POL",
                                      known_ground=True, capital_usd=0.0))
        self.odds = {AL: 0.994, TN: 0.991, ALD: 0.006, GA: 0.71}
        self.pings = []
        self.b = Bonds(self.r.fam, self.r.exchange,
                       lambda s: self.odds.get(s), clock=lambda: self.r.now,
                       alert=lambda t, m: self.pings.append((t, m)))
        for s in (AL, TN, GA):
            self.seed(s, yes_book(self.r.now))
        self.seed(ALD, no_book(self.r.now))
        self.r.fam.refresh_terms(self.r.exchange, self.r.now)
        self.now = self.r.now

    def seed(self, slug, book):
        self.r.fam.universe[slug] = {"event_n": 1, "name": slug}
        self.r.exchange.books[slug] = book
        self.r.cache.put(slug, book)
        self.r.exchange.prog_raw[slug] = copy.deepcopy(LIVE_PROG)

    def exch(self, slug, qty, yes_px):
        """The exchange's position: qty > 0 long YES, < 0 short YES."""
        self.r.positions[slug] = (qty, qty * yes_px)
        self.r.fam.inventory[slug] = {"qty": qty, "cost": qty * yes_px}

    def bond(self, slug, side, qty, yes_px):
        """A bond in the ledger AND on the exchange."""
        self.exch(slug, qty if side == "YES" else -qty, yes_px)
        cost = qty * (yes_px if side == "YES" else 1.0 - yes_px)
        self.b._book_lot(slug, side, qty, cost)

    def positions(self):
        return dict(self.r.positions)

    def orders(self, slug, side=None, decoy=None):
        out = []
        for o in self.r.fam.orders.values():
            if o.market != slug or (side is not None and o.side != side):
                continue
            is_decoy = str(o.why or "").startswith("bond decoy")
            if decoy is not None and is_decoy != decoy:
                continue
            out.append(o)
        return out


class TestTheBand(unittest.TestCase):
    def test_both_ends_are_bonds(self):
        self.assertEqual(side_for(0.994), "YES")
        self.assertEqual(side_for(HIGH_ODDS), "YES")
        self.assertEqual(side_for(0.006), "NO")
        self.assertEqual(side_for(LOW_ODDS), "NO")
        self.assertIsNone(side_for(0.985))
        self.assertIsNone(side_for(None))

    def test_the_check_is_nightly(self):
        t = calendar.timegm((2026, 9, 3, 6, 59, 0, 0, 0, 0))
        self.assertIsNone(scan_due(t, ""))
        self.assertEqual(scan_due(t + 120, ""), "2026-09-03")
        self.assertIsNone(scan_due(t + 120, "2026-09-03"))
        self.assertEqual(scan_due(t + 86400 + 120, "2026-09-03"), "2026-09-04")

    def test_the_owners_numbers(self):
        self.assertEqual(KEEP_FRACTION, 0.6)
        self.assertEqual(PING_EVERY_USD, 100.0)
        self.assertEqual(DECOY_QTY, 10.0)
        self.assertEqual(MINNOW_MAX, 25.0)
        self.assertEqual(DANCE_WAIT_S, 7200.0)
        self.assertEqual(DANCE_MAX_MOVES, 3)


class TestTheList(Base):
    def test_silver_proposes_both_ends_and_only_the_owner_adds(self):
        new = self.b.scan(self.now, force=True)
        self.assertEqual(set(new), {AL, TN, ALD})
        self.assertEqual(self.b.proposed[ALD]["side"], "NO")
        self.assertEqual(self.b.approved, {})
        self.assertTrue(self.b.approve(AL, self.now)["ok"])
        self.assertTrue(self.b.approve(ALD, self.now)["ok"])
        self.assertEqual(self.b.approved[ALD]["side"], "NO")
        self.assertEqual(self.pings, [])                       # silent

    def test_proposals_sit_newest_first(self):
        self.b.scan(self.now, force=True)
        self.odds[GA] = 0.995
        self.b.scan(self.now + 3600, force=True)
        self.assertEqual(self.b.view(self.now + 3600)["proposed"][0]["market"], GA)

    def test_inside_the_band_cannot_be_added_even_by_hand(self):
        r = self.b.approve(GA, self.now)
        self.assertFalse(r["ok"])
        self.assertIn("71.0%", r["note"])

    def test_a_listed_market_that_leaves_the_band_drops_by_itself(self):
        self.b.approve(AL, self.now)
        self.bond(AL, "YES", 100.0, 0.98)
        self.odds[AL] = 0.984
        self.b.scan(self.now + 1, force=True)
        self.assertNotIn(AL, self.b.approved)
        v = self.b.view(self.now + 1, self.positions())
        self.assertEqual(v["dropped"][0]["market"], AL)
        self.assertEqual(v["dropped"][0]["held"], 100.0)
        self.assertEqual(self.pings, [])

    def test_the_engine_keeps_quoting_and_exits_only_its_own_stock(self):
        self.b.approve(AL, self.now)
        self.assertFalse(self.r.fam._frozen(AL))
        self.assertTrue(self.r.fam.enterable(AL))
        self.bond(AL, "YES", 100.0, 0.98)
        self.assertEqual(self.r.fam.bond_qty, {AL: 100.0})   # what _sell subtracts

    def test_ignore_is_remembered_and_reversible(self):
        self.b.scan(self.now, force=True)
        self.b.ignore(TN, self.now)
        self.assertNotIn(TN, self.b.scan(self.now + 1, force=True))
        self.b.unignore(TN)
        self.assertIn(TN, self.b.scan(self.now + 2, force=True))

    def test_the_scan_runs_once_a_night_on_its_own(self):
        t = calendar.timegm((2026, 9, 3, 7, 5, 0, 0, 0, 0))
        self.assertEqual(set(self.b.scan(t)), {AL, TN, ALD})
        self.odds[GA] = 0.995
        self.assertEqual(self.b.scan(t + 3600), [])
        self.assertIn(GA, self.b.scan(t + 86400))


class TestTheLedger(Base):
    def test_only_bond_purchases_count_as_held(self):
        self.b.approve(AL, self.now)
        self.exch(AL, 500.0, 0.92)               # the engine's own stock
        self.assertEqual(self.b.held(AL, "YES"), 0.0)
        v = self.b.view(self.now, self.positions())
        row = v["rows"][0]
        self.assertEqual(row["qty"], 0.0)
        self.assertEqual(row["uncounted"], 500.0)
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertEqual(self.orders(AL, "SELL"), [])        # no order on engine stock

    def test_the_owner_can_count_his_shares_in(self):
        self.b.approve(AL, self.now)
        self.exch(AL, 500.0, 0.92)
        r = self.b.adopt(AL, 300.0, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertEqual(self.b.held(AL, "YES"), 300.0)
        self.assertAlmostEqual(self.b.cost_basis(AL, "YES"), 0.92)
        self.assertFalse(self.b.adopt(AL, 300.0, self.positions())["ok"])  # only 200 left

    def test_the_order_never_exceeds_what_the_exchange_shows(self):
        self.b.approve(AL, self.now)
        self.b._book_lot(AL, "YES", 400.0, 400 * 0.98)   # ledger says 400
        self.exch(AL, 250.0, 0.98)                       # exchange says 250
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertEqual(self.orders(AL, "SELL")[0].qty, 250.0)


class TestThePage(Base):
    def test_rows_are_cheapest_per_dollar_first(self):
        for s in (AL, TN, ALD):
            self.b.approve(s, self.now)
        self.r.cache.put(TN, yes_book(self.now, bid=0.97, ask=0.98))
        v = self.b.view(self.now)
        self.assertEqual([r["market"] for r in v["rows"]], [TN, AL, ALD])
        tn = v["rows"][0]
        self.assertEqual(tn["cost"], 0.98)
        self.assertAlmostEqual(tn["yield"], (1 - 0.98) / 0.98, places=4)
        self.assertIsNotNone(tn["earn"])
        self.assertEqual(v["keep"], 0.6)


class TestTheRestingOrder(Base):
    def test_switch_off_places_nothing(self):
        self.b.approve(AL, self.now)
        self.bond(AL, "YES", 1500.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=False)
        self.assertEqual(self.r.exchange.live, {})

    def test_a_big_lot_sits_behind_the_touch_keeping_most_of_the_reward(self):
        self.b.approve(AL, self.now)
        self.r.cache.put(AL, minnow_book(self.now))
        self.bond(AL, "YES", 1500.0, 0.89)
        out = self.b.cycle(self.now, self.positions(), on=True)
        ask = self.orders(AL, "SELL", decoy=False)[0]
        self.assertEqual((ask.purpose, ask.intent), ("bond", SELL_LONG))
        self.assertEqual(ask.qty, 1500.0)
        # at 60% kept it sits three ticks back (0.2^3 x 1500 = 12 vs 5)
        self.assertAlmostEqual(ask.price, 0.93)
        self.assertEqual(out["placed"][0]["ticks"], 3)
        self.assertGreaterEqual(self.b.slot[AL]["keep"], 0.6)

    def test_never_under_cost(self):
        self.b.approve(AL, self.now)
        self.r.cache.put(AL, minnow_book(self.now))
        self.bond(AL, "YES", 1500.0, 0.945)
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertGreaterEqual(self.orders(AL, "SELL")[0].price, 0.95 - 1e-9)

    def test_a_held_no_bond_gets_a_cover_bid_never_above_what_it_sold_at(self):
        self.b.approve(ALD, self.now)
        self.bond(ALD, "NO", 1000.0, 0.01)
        self.b.cycle(self.now, self.positions(), on=True)
        bids = self.orders(ALD, "BUY")
        self.assertEqual(len(bids), 1)
        self.assertEqual((bids[0].purpose, bids[0].intent), ("bond", SELL_SHORT))
        self.assertLessEqual(bids[0].price, 0.01 + 1e-9)
        self.assertEqual(bids[0].qty, 1000.0)


class TestTheSniper(Base):
    """Owner: "purchasing anything that is in the way of our sell orders
    collecting rewards"; then: "the decoy joins the minnow instead of
    beating it. Each time the decoy moves wait 2 hours to see if the
    minnow will move again, then, if not kill the decoy and snap up the
    minnow. If the minnow moves more than 3 times or reaches the touch
    or below cost, snap it up immediately." Minnows are 25 shares or
    fewer."""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)
        self.r.cache.put(AL, minnow_book(self.now, minnows=0.0, ask=0.99))
        self.bond(AL, "YES", 1500.0, 0.90)
        self.b.cycle(self.now, self.positions(), on=True)   # rests the main ask
        self.main = self.orders(AL, "SELL", decoy=False)[0]

    def book_with_minnow(self, px, q=20.0, t=0.0, bid=0.88):
        # our main ask (and any decoy) stay on the book; a minnow sits in front
        mine = [(o.price, o.qty) for o in self.orders(AL, "SELL")]
        asks = [(px, q)] + mine + [(0.99, 20000.0)]
        merged = {}
        for p, qq in asks:
            merged[round(p, 4)] = merged.get(round(p, 4), 0.0) + qq
        d = Book(bids=((bid, 50.0), (0.50, 20000.0)),
                 asks=tuple(sorted((p, qq) for p, qq in merged.items() if qq > 0)),
                 tick=0.01, fetched_at=self.now + t)
        self.r.cache.put(AL, d)
        return d

    def cyc(self, t):
        return self.b.cycle(self.now + t, self.positions(), on=True)

    def test_the_decoy_joins_the_minnow_at_its_own_price(self):
        m_px = round(self.main.price - 0.01, 2)
        self.book_with_minnow(m_px, 20.0, 60)
        out = self.cyc(60)
        decoys = self.orders(AL, "SELL", decoy=True)
        self.assertEqual(len(decoys), 1)
        self.assertAlmostEqual(decoys[0].price, m_px)          # joins, not beats
        self.assertEqual(decoys[0].qty, DECOY_QTY)
        self.assertTrue(out["placed"][0]["decoy"])
        self.assertEqual(self.b.dance[AL]["moves"], 0)
        self.assertIn(self.main.id, self.r.fam.orders)
        v = self.b.view(self.now + 60, self.positions())
        self.assertEqual(v["rows"][0]["dance"]["px"], m_px)

    def test_a_minnow_that_stays_put_for_two_hours_is_taken(self):
        m_px = round(self.main.price - 0.01, 2)
        self.book_with_minnow(m_px, 20.0, 60)
        self.cyc(60)
        self.book_with_minnow(m_px, 20.0, 60 + 3600)          # still there
        self.cyc(60 + 3600)
        self.assertEqual([o for o in self.r.fam.orders.values() if o.side == "BUY"], [])
        self.book_with_minnow(m_px, 20.0, 60 + 7200 + 5)      # two hours on
        out = self.cyc(60 + 7200 + 5)
        takes = [o for o in self.r.fam.orders.values() if o.side == "BUY"]
        self.assertEqual(len(takes), 1)
        self.assertAlmostEqual(takes[0].price, m_px)           # at ITS price
        self.assertEqual(takes[0].qty, 20.0)
        self.assertTrue(out["placed"][0]["taken"])
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])   # decoy killed
        self.assertNotIn(AL, self.b.dance)
        self.assertEqual(self.b.held(AL, "YES"), 1520.0)

    def test_each_move_restarts_the_clock_and_the_decoy_follows(self):
        m1 = round(self.main.price - 0.01, 2)
        self.book_with_minnow(m1, 20.0, 60)
        self.cyc(60)
        m2 = round(m1 - 0.01, 2)
        self.book_with_minnow(m2, 20.0, 60 + 5400)              # moved at 1.5h
        self.cyc(60 + 5400)
        d = self.orders(AL, "SELL", decoy=True)
        self.assertEqual(len(d), 1)
        self.assertAlmostEqual(d[0].price, m2)
        self.assertEqual(self.b.dance[AL]["moves"], 1)
        # 1.5h after the FIRST join is not two hours after the second
        self.book_with_minnow(m2, 20.0, 60 + 7300)
        self.cyc(60 + 7300)
        self.assertEqual([o for o in self.r.fam.orders.values() if o.side == "BUY"], [])

    def test_a_fourth_move_is_taken_at_once(self):
        px = round(self.main.price - 0.01, 2)
        t = 60
        for i in range(4):                                     # join, then 3 moves
            self.book_with_minnow(round(px - 0.01 * i, 2), 20.0, t)
            self.cyc(t)
            t += 600
        self.assertEqual(self.b.dance[AL]["moves"], 3)
        self.assertEqual([o for o in self.r.fam.orders.values() if o.side == "BUY"], [])
        self.book_with_minnow(round(px - 0.04, 2), 20.0, t)   # the fourth move
        self.cyc(t)
        takes = [o for o in self.r.fam.orders.values() if o.side == "BUY"]
        self.assertEqual(len(takes), 1)
        self.assertAlmostEqual(takes[0].price, round(px - 0.04, 2))

    def test_reaching_the_far_touch_is_taken_at_once(self):
        # a minnow one tick over the best bid has nowhere left to go
        self.book_with_minnow(0.95, 20.0, 60, bid=0.94)
        self.cyc(60)
        takes = [o for o in self.r.fam.orders.values() if o.side == "BUY"]
        self.assertEqual(len(takes), 1)
        self.assertAlmostEqual(takes[0].price, 0.95)

    def test_under_our_cost_is_taken_at_once_and_never_joined(self):
        self.book_with_minnow(0.89, 20.0, 60)                  # cost is 90c
        self.cyc(60)
        takes = [o for o in self.r.fam.orders.values() if o.side == "BUY"]
        self.assertEqual(len(takes), 1)
        self.assertAlmostEqual(takes[0].price, 0.89)
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])

    def test_a_note_only_after_a_hundred_dollars(self):
        for i in range(6):
            self.book_with_minnow(0.89, 20.0, 60 * (i + 1))
            self.cyc(60 * (i + 1))
        self.assertEqual(len(self.pings), 1)                   # 6 x $17.80 = $106.80
        self.assertIn("$106.80", self.pings[0][1])
        self.assertAlmostEqual(self.b.unpinged, 0.0)

    def test_no_minnow_means_no_decoy_and_a_stale_decoy_is_pulled(self):
        m_px = round(self.main.price - 0.01, 2)
        self.book_with_minnow(m_px, 20.0, 60)
        self.cyc(60)
        self.assertEqual(len(self.orders(AL, "SELL", decoy=True)), 1)
        self.book_with_minnow(m_px, 0.0, 120)                  # the minnow left
        self.cyc(120)
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])
        self.assertNotIn(AL, self.b.dance)

    def test_more_than_25_shares_in_front_is_not_a_minnow(self):
        self.book_with_minnow(round(self.main.price - 0.01, 2), 26.0, 60)
        self.cyc(60)
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])
        self.book_with_minnow(round(self.main.price - 0.01, 2), 25.0, 120)
        self.cyc(120)
        self.assertEqual(len(self.orders(AL, "SELL", decoy=True)), 1)


class TestEntryAndSales(Base):
    def test_nothing_is_bought_where_no_bond_sale_of_ours_rests(self):
        # owner, 2026-09-02: "the sniper should only work where I have
        # bond sales resting" — a listed market with a cheap touch and
        # money in hand is NOT entered by itself, and a minnow-sized
        # order there draws no decoy either
        self.b.approve(AL, self.now)
        self.b.approve(TN, self.now)
        self.b.set_budget(500.0)
        self.r.cache.put(TN, yes_book(self.now, bid=0.97, ask=0.98, ask_q=20.0))
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertEqual(self.r.exchange.live, {})
        self.assertEqual(self.b.budget, 500.0)
        self.assertEqual(self.b.lots, {})

    def test_a_sale_by_our_order_leaves_the_ledger_and_the_cash_waits(self):
        self.b.approve(AL, self.now)
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        ask = self.orders(AL, "SELL")[0]
        self.r.exchange.live.pop(ask.id, None)          # the exchange filled it
        self.r.fam.orders.pop(ask.id, None)
        self.exch(AL, 0.0, 0.0)
        self.r.fam.inventory.pop(AL, None)
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(self.b.held(AL, "YES"), 0.0)
        self.assertAlmostEqual(self.b.cash, 100 * ask.price, places=2)
        self.assertEqual(self.b.lots, {})

    def test_a_hand_sale_with_our_order_untouched_is_not_counted(self):
        self.b.approve(AL, self.now)
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        self.exch(AL, 60.0, 0.98)                       # 40 sold by hand
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(self.b.cash, 0.0)
        self.assertEqual(self.b.held(AL, "YES"), 100.0)  # the ledger keeps its count

    def test_persistence_round_trip(self):
        self.b.approve(AL, self.now)
        self.b.approve(ALD, self.now)
        self.bond(AL, "YES", 50.0, 0.98)
        self.b.set_budget(250.0)
        self.b.unpinged = 33.0
        self.b.dance[AL] = {"px": 0.97, "moves": 1, "since": 5.0}
        d = self.b.to_dict()
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        b2.restore(d)
        self.assertEqual(b2.held(AL, "YES"), 50.0)
        self.assertEqual((b2.budget, b2.unpinged), (250.0, 33.0))
        self.assertEqual(b2.dance[AL]["moves"], 1)
        self.assertEqual(self.r.fam.bond_qty, {AL: 50.0})


class TestTheOwnersEntry(Base):
    """Owner, 2026-09-02: "the initial purchases should be made by me so
    let me see the books in the bond market and give me the choice to
    enter at various prices, snapping up all the currently resting sale
    orders at or below that price."""

    class Sweeping:
        """A book that loses each level as it is taken."""

        def __init__(self, asks, now):
            self.asks, self.now = list(asks), now
            self.books = {}

        def book(self, slug, fetched_at=None):
            return Book(bids=((0.90, 50.0), (0.50, 20000.0)),
                        asks=tuple(self.asks) + ((0.999, 20000.0),),
                        tick=0.01, fetched_at=fetched_at or self.now)

        def post(self, *a, **k):
            # the take fills: the touch level is gone from the book
            self.asks.pop(0)
            return {"id": "T%d" % (10 - len(self.asks))}

        def __getattr__(self, name):
            return lambda *a, **k: []

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)
        self.sweep = self.Sweeping([(0.95, 20.0), (0.96, 30.0), (0.97, 40.0),
                                    (0.98, 500.0)], self.now)
        self.b.client = self.sweep
        self.r.fam.desk.client = self.sweep
        self.r.cache.put(AL, self.sweep.book(AL))

    def test_the_page_shows_the_ladder_to_enter_at(self):
        v = self.b.view(self.now, self.positions())
        lad = v["rows"][0]["ladder"]
        self.assertEqual([l["px"] for l in lad][:4], [0.95, 0.96, 0.97, 0.98])
        self.assertEqual(lad[1]["cum_qty"], 50.0)
        self.assertAlmostEqual(lad[1]["cum_usd"], 20 * 0.95 + 30 * 0.96, places=2)
        self.assertEqual(v["money"], 1000.0)

    def test_entering_at_a_price_sweeps_everything_at_or_inside_it(self):
        r = self.b.enter(AL, 0.97, self.now, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertEqual(self.b.held(AL, "YES"), 90.0)          # 20 + 30 + 40
        self.assertAlmostEqual(self.b.cost_basis(AL, "YES"),
                               (20 * 0.95 + 30 * 0.96 + 40 * 0.97) / 90, places=4)
        self.assertEqual(self.sweep.asks, [(0.98, 500.0)])      # the 98s untouched
        self.assertAlmostEqual(self.b.budget, 1000 - (19 + 28.8 + 38.8), places=2)
        self.assertIn("90 YES in 3 lots", r["note"])

    def test_the_money_is_the_limit(self):
        self.b.set_budget(40.0)
        r = self.b.enter(AL, 0.98, self.now, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertEqual(self.b.held(AL, "YES"), 20.0 + 21.0)   # 20 @ 95c, then 21 @ 96c
        self.assertLess(self.b.budget, 5.0)

    def test_nothing_inside_the_price_means_nothing_bought(self):
        r = self.b.enter(AL, 0.94, self.now, self.positions())
        self.assertFalse(r["ok"])
        self.assertEqual(self.b.held(AL, "YES"), 0.0)
        self.assertEqual(self.b.budget, 1000.0)

    def test_only_listed_markets_and_only_with_money(self):
        self.assertFalse(self.b.enter(GA, 0.97, self.now)["ok"])
        self.b.set_budget(0.0)
        self.assertIn("budget", self.b.enter(AL, 0.97, self.now)["note"])


class TestTheDesksSecondCarveOut(Base):
    def test_only_the_owner_may_take_and_never_past_the_touch(self):
        d = self.r.fam.desk
        r = d.place_resting(AL, "BUY", 0.99, 10.0, intent=BUY_LONG,
                            initiator="auto", taker="bond")
        self.assertFalse(r.ok)
        self.assertIn("owner", r.note)
        self.r.cache.put(AL, yes_book(self.now, bid=0.97, ask=0.98))
        r = d.place_resting(AL, "BUY", 0.99, 10.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertFalse(r.ok)
        self.assertIn("never worse than the touch", r.note)
        self.r.cache.put(AL, yes_book(self.now))
        r = d.place_resting(AL, "BUY", 0.99, 301.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertFalse(r.ok)
        self.assertIn("exceeds", r.note)
        r = d.place_resting(AL, "BUY", 0.99, 300.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertTrue(r.ok, r.note)

    def test_a_no_bond_takes_the_bid(self):
        d = self.r.fam.desk
        r = d.place_resting(ALD, "SELL", 0.01, 400.0, intent=BUY_SHORT,
                            initiator="owner", taker="bond")
        self.assertTrue(r.ok, r.note)
        self.assertFalse(d.place_resting(ALD, "SELL", 0.01, 401.0,
                                         intent=BUY_SHORT, initiator="owner",
                                         taker="bond").ok)

    def test_nothing_else_may_cross_under_the_bond_flag(self):
        r = self.r.fam.desk.place_resting(AL, "SELL", 0.98, 10.0,
                                          intent=SELL_LONG, net_position=10.0,
                                          initiator="owner", taker="bond")
        self.assertFalse(r.ok)
        self.assertIn("only open a bond", r.note)


class TestEngineHandsOff(unittest.TestCase):
    def test_bond_orders_are_treated_like_the_owners_hand(self):
        import inspect
        from v3 import family
        src = inspect.getsource(family)
        self.assertNotIn('rec.purpose == "manual" or self._frozen', src)
        self.assertGreaterEqual(src.count('"bond"'), 10)
        self.assertIn("self.bond_qty.get(slug, 0.0)", src)


if __name__ == "__main__":
    unittest.main()


class TestOurOwnOrdersAreNotForSale(Base):
    """Owner, 2026-09-02: "we can't buy our own orders, so exclude orders
    that the engine places. For instance on this Hawai'i governor market
    we are selling 3 shares at the touch so the actually available
    shares should be 5.\""""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)

    def ours(self, px, qty, oid="E1", purpose="sell"):
        self.r.fam.orders[oid] = FamilyOrder(
            id=oid, market=AL, side="SELL", price=px, qty=qty,
            intent=SELL_LONG, placed_ts=self.now, purpose=purpose)

    def hawaii(self):
        self.bk = Book(bids=((0.97, 50.0), (0.50, 20000.0)),
                       asks=((0.98, 8.0), (0.99, 20000.0)),
                       tick=0.01, fetched_at=self.now)
        self.r.cache.put(AL, self.bk)
        self.ours(0.98, 3.0)

    def test_eight_showing_three_ours_is_five(self):
        self.hawaii()
        row = self.b.view(self.now, self.positions())["rows"][0]
        self.assertAlmostEqual(float(row["size"]), 5.0)
        self.assertEqual(row["ladder"][0]["px"], 0.98)
        self.assertAlmostEqual(row["ladder"][0]["qty"], 5.0)
        self.assertAlmostEqual(row["ladder"][0]["cum_qty"], 5.0)
        self.assertAlmostEqual(row["ladder"][0]["cum_usd"], 5 * 0.98, places=2)
        px, cost, size = self.b._take_price("YES", self.bk, AL)
        self.assertEqual((px, size), (0.98, 5.0))

    def test_a_level_that_is_all_ours_is_not_on_the_ladder(self):
        self.r.cache.put(AL, Book(bids=((0.97, 50.0), (0.50, 20000.0)),
                                  asks=((0.98, 3.0), (0.99, 300.0)),
                                  tick=0.01, fetched_at=self.now))
        self.ours(0.98, 3.0)
        row = self.b.view(self.now, self.positions())["rows"][0]
        self.assertEqual([l["px"] for l in row["ladder"]], [0.99])
        self.assertEqual(row["cost"], 0.99)
        self.assertAlmostEqual(float(row["size"]), 300.0)

    def test_a_sweep_skips_a_level_that_is_only_ours(self):
        sweep = TestTheOwnersEntry.Sweeping(
            [(0.95, 20.0), (0.96, 30.0), (0.97, 40.0), (0.98, 500.0)], self.now)
        mine = {0.95}

        def post(*a, **k):
            # a take fills against OTHERS: the first level not ours goes
            for i, (p, q) in enumerate(sweep.asks):
                if p not in mine:
                    sweep.asks.pop(i)
                    break
            return {"id": "T%d" % len(sweep.asks)}

        sweep.post = post
        self.b.client = sweep
        self.r.fam.desk.client = sweep
        self.r.cache.put(AL, sweep.book(AL))
        self.ours(0.95, 20.0)
        r = self.b.enter(AL, 0.97, self.now, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertEqual(self.b.held(AL, "YES"), 70.0)          # 30 + 40, not our 20
        self.assertEqual(sweep.asks, [(0.95, 20.0), (0.98, 500.0)])

    def test_the_desk_refuses_a_take_of_our_own_level(self):
        self.hawaii()
        d = self.r.fam.desk
        r = d.place_resting(AL, "BUY", 0.98, 6.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertFalse(r.ok)
        self.assertIn("exceeds", r.note)                       # others show 5
        r = d.place_resting(AL, "BUY", 0.98, 5.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertTrue(r.ok, r.note)
        # a level that is all ours is not the touch for a take
        self.r.cache.put(AL, Book(bids=((0.97, 50.0), (0.50, 20000.0)),
                                  asks=((0.98, 3.0), (0.99, 300.0)),
                                  tick=0.01, fetched_at=self.now))
        r = d.place_resting(AL, "BUY", 0.98, 3.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertFalse(r.ok)
        r = d.place_resting(AL, "BUY", 0.99, 300.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertTrue(r.ok, r.note)


class TestTheMinnowCheckNetsAllOurOrders(Base):
    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)
        self.r.cache.put(AL, minnow_book(self.now, minnows=0.0, ask=0.99))
        self.bond(AL, "YES", 1500.0, 0.90)
        self.b.cycle(self.now, self.positions(), on=True)
        self.main = self.orders(AL, "SELL", decoy=False)[0]
        self.m_px = round(self.main.price - 0.01, 2)
        self.r.fam.orders["E1"] = FamilyOrder(
            id="E1", market=AL, side="SELL", price=self.m_px, qty=5.0,
            intent=SELL_LONG, placed_ts=self.now, purpose="sell")

    def book(self, others, t):
        merged = {}
        for p, q in [(o.price, o.qty) for o in self.orders(AL, "SELL")] + [
                (self.m_px, others), (0.99, 20000.0)]:
            merged[round(p, 4)] = merged.get(round(p, 4), 0.0) + q
        self.r.cache.put(AL, Book(
            bids=((0.88, 50.0), (0.50, 20000.0)),
            asks=tuple(sorted((p, q) for p, q in merged.items() if q > 0)),
            tick=0.01, fetched_at=self.now + t))

    def test_our_engine_order_in_front_is_not_a_minnow(self):
        self.book(0.0, 60)                       # the 5 in front are ours
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])
        self.assertNotIn(AL, self.b.dance)
        self.assertIsNone(self.b.view(self.now + 60, self.positions())["rows"][0]["minnow"])

    def test_only_the_others_shares_count_as_the_minnow(self):
        self.book(3.0, 60)                       # 8 showing, 5 ours
        self.b.cycle(self.now + 60, self.positions(), on=True)
        decoys = self.orders(AL, "SELL", decoy=True)
        self.assertEqual(len(decoys), 1)
        self.assertAlmostEqual(decoys[0].price, self.m_px)
        self.book(3.0, 61)                       # the book now shows the decoy too
        v = self.b.view(self.now + 61, self.positions())["rows"][0]
        self.assertAlmostEqual(v["minnow"]["qty"], 3.0)


class TestTheBudgetFollowsTaxes(Base):
    """Owner, 2026-09-03: "set the budget to whatever I currently owe in
    taxes." The budget follows what he owes, less what the engine has
    spent of it, unless he types a fixed figure."""

    def setUp(self):
        super().setUp()
        self.tax = {"owed": 220.0, "gross": 1000.0, "rate": 0.22}
        self.b.tax_owed = lambda: self.tax

    def test_the_budget_is_what_he_owes_less_what_was_spent(self):
        self.assertEqual(self.b.budget_mode, "tax")
        self.b.cycle(self.now, self.positions(), on=False)
        self.assertEqual(self.b.budget, 220.0)
        self.tax["owed"] = 264.0                        # more paid, more owed
        self.assertEqual(self.b.view(self.now)["budget"], 264.0)
        self.b._pay(64.0)
        self.assertEqual((self.b.budget, self.b.spent), (200.0, 64.0))
        self.b.cycle(self.now + 1, self.positions(), on=False)
        self.assertEqual(self.b.budget, 200.0)          # spent stays spent
        v = self.b.view(self.now + 1)
        self.assertEqual(v["budget_mode"], "tax")
        self.assertEqual(v["tax"]["gross"], 1000.0)
        self.assertEqual(v["money"], 200.0)

    def test_a_fixed_budget_overrides_and_follow_returns(self):
        self.b.set_budget(50.0)
        self.assertEqual(self.b.budget_mode, "fixed")
        self.b.cycle(self.now, self.positions(), on=False)
        self.assertEqual(self.b.budget, 50.0)
        r = self.b.follow_tax()
        self.assertTrue(r["ok"])
        self.assertEqual((self.b.budget_mode, self.b.budget), ("tax", 220.0))

    def test_no_pay_data_yet_means_no_budget(self):
        self.b.tax_owed = lambda: None
        self.b.approve(AL, self.now)
        self.b.cycle(self.now, self.positions(), on=False)
        self.assertEqual(self.b.budget, 0.0)
        self.assertIn("budget", self.b.enter(AL, 0.97, self.now)["note"])

    def test_old_state_without_the_mode_follows_taxes(self):
        d = self.b.to_dict()
        d.pop("budget_mode", None)
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        b2.restore(d)
        self.assertEqual(b2.budget_mode, "tax")
        self.b.set_budget(50.0)
        b3 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        b3.restore(self.b.to_dict())
        self.assertEqual((b3.budget_mode, b3.budget), ("fixed", 50.0))


class TestEarnings(Base):
    """Owner, 2026-09-03: "the bonds page should reflect the overall
    earnings from the bonds which is the profit from sales and the
    liquidity rewards payments ... it should try and differentiate the
    liquidity payments from engine resting orders.\""""

    def setUp(self):
        super().setUp()
        self.paid = {}
        self.b.paid = lambda day, slug: self.paid.get((day, slug))
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)

    def test_profit_on_a_sale_by_our_order_is_counted(self):
        self.bond(AL, "YES", 100.0, 0.90)
        self.b.cycle(self.now, self.positions(), on=True)
        ask = self.orders(AL, "SELL")[0]
        self.r.exchange.live.pop(ask.id, None)          # the exchange filled it
        self.r.fam.orders.pop(ask.id, None)
        self.exch(AL, 0.0, 0.0)
        self.r.fam.inventory.pop(AL, None)
        self.b.cycle(self.now + 60, self.positions(), on=True)
        e = self.b.view(self.now + 60)["earned"]
        self.assertAlmostEqual(e["sales"], 100 * (ask.price - 0.90), places=2)
        self.assertAlmostEqual(e["sold_usd"], 100 * ask.price, places=2)
        self.assertAlmostEqual(e["total"], e["sales"], places=2)
        self.assertEqual(e["rewards"], 0.0)

    def test_a_days_payment_splits_by_measured_share_with_the_engine(self):
        self.bond(AL, "YES", 100.0, 0.90)
        self.b.cycle(self.now, self.positions(), on=True)   # day 1: bond only
        bond = self.orders(AL, "SELL")[0]
        bond.live_est = 1.0
        self.r.fam.orders["E1"] = FamilyOrder(
            id="E1", market=AL, side="BUY", price=0.90, qty=50.0,
            intent=BUY_LONG, placed_ts=self.now, purpose="earn", live_est=3.0)
        t2 = self.now + 86400
        self.b.cycle(t2, self.positions(), on=True)         # day 2: 1 of 4
        d2 = Bonds._day(t2)
        self.assertAlmostEqual(self.b.share_day[d2][AL][0] / self.b.share_day[d2][AL][1], 0.25)
        self.assertEqual(self.b.view(t2)["earned"]["rewards"], 0.0)   # unpaid yet
        self.paid[(d2, AL)] = 4.0
        e = self.b.view(t2 + 60)["earned"]
        self.assertAlmostEqual(e["rewards"], 1.0, places=2)
        self.assertAlmostEqual(e["engine"], 3.0, places=2)
        self.assertAlmostEqual(e["paid"], 4.0, places=2)
        self.assertAlmostEqual(e["total"], 1.0, places=2)
        row = self.b.view(t2 + 60)["rows"][0]
        self.assertAlmostEqual(row["rewards"], 1.0, places=2)

    def test_a_bond_alone_in_its_market_gets_the_whole_payment(self):
        self.bond(AL, "YES", 100.0, 0.90)
        self.b.cycle(self.now, self.positions(), on=True)
        self.paid[(Bonds._day(self.now), AL)] = 2.5
        e = self.b.view(self.now + 60)["earned"]
        self.assertAlmostEqual(e["rewards"], 2.5, places=2)
        self.assertEqual(e["engine"], 0.0)

    def test_old_days_fold_into_the_booked_total_and_persist(self):
        self.bond(AL, "YES", 100.0, 0.90)
        self.b.cycle(self.now, self.positions(), on=True)
        d1 = Bonds._day(self.now)
        self.paid[(d1, AL)] = 2.0
        later = self.now + 10 * 86400
        self.b.cycle(later, self.positions(), on=True)
        self.assertNotIn(d1, self.b.share_day)
        self.assertAlmostEqual(self.b.rewards_booked, 2.0, places=2)
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        b2.restore(self.b.to_dict())                     # no paid map at all
        v = b2.view(later)
        self.assertAlmostEqual(v["earned"]["rewards"], 2.0, places=2)
        self.assertAlmostEqual(v["rows"][0]["rewards"], 2.0, places=2)
