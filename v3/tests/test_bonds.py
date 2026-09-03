"""Bonds: the tax reserve earning like a bond (owner, 2026-09-02).

The spec and its corrections, in bonds.py's docstring. Here: the
nightly Silver check proposes at both ends and drops silently; the
owner alone adds; the engine keeps quoting bond markets but never
rests its exits on bond stock or touches a bond order; a held bond
gets one resting order at or above cost and it is left alone; a sale
by our order is reinvested by TAKING the touch of the cheapest listed
market whose earning side pays; a hand sale is never reinvested.
"""

import calendar
import copy
import unittest

from v3.bonds import HIGH_ODDS, LOW_ODDS, Bonds, scan_due, side_for
from v3.family import FamilyConfig
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
    # a 1% market: the NO bond is a short of YES at the 1c bid
    return Book(bids=((bid, bid_q), (0.001, 20000.0)),
                asks=((ask, ask_q), (0.60, 20000.0)),
                tick=0.01, fetched_at=now)


class Base(unittest.TestCase):
    def setUp(self):
        self.r = Rig(cfg=FamilyConfig(name="Politics", tag="POL",
                                      known_ground=True, capital_usd=0.0))
        self.odds = {AL: 0.994, TN: 0.991, ALD: 0.006, GA: 0.71}
        self.b = Bonds(self.r.fam, self.r.exchange,
                       lambda s: self.odds.get(s), clock=lambda: self.r.now)
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

    def hold(self, slug, qty, yes_px):
        """qty > 0 long YES; qty < 0 short YES (a NO bond)."""
        self.r.positions[slug] = (qty, qty * yes_px)
        self.r.fam.inventory[slug] = {"qty": qty, "cost": qty * yes_px}

    def positions(self):
        return dict(self.r.positions)

    def orders(self, slug, side=None):
        return [o for o in self.r.fam.orders.values()
                if o.market == slug and (side is None or o.side == side)]


class TestTheBand(unittest.TestCase):
    def test_both_ends_are_bonds(self):
        self.assertEqual(side_for(0.994), "YES")
        self.assertEqual(side_for(HIGH_ODDS), "YES")
        self.assertEqual(side_for(0.006), "NO")
        self.assertEqual(side_for(LOW_ODDS), "NO")
        self.assertIsNone(side_for(0.985))
        self.assertIsNone(side_for(0.02))
        self.assertIsNone(side_for(None))

    def test_the_check_is_nightly(self):
        t = calendar.timegm((2026, 9, 3, 6, 59, 0, 0, 0, 0))
        self.assertIsNone(scan_due(t, ""))                     # before 07Z
        self.assertEqual(scan_due(t + 120, ""), "2026-09-03")
        self.assertIsNone(scan_due(t + 120, "2026-09-03"))     # done tonight
        self.assertIsNone(scan_due(t + 6 * 3600, "2026-09-03"))
        self.assertEqual(scan_due(t + 86400 + 120, "2026-09-03"), "2026-09-04")


class TestTheList(Base):
    def test_silver_proposes_both_ends_and_only_the_owner_adds(self):
        new = self.b.scan(self.now, force=True)
        self.assertEqual(set(new), {AL, TN, ALD})              # GA at 71% is not
        self.assertEqual(self.b.proposed[ALD]["side"], "NO")
        self.assertEqual(self.b.approved, {})                   # nothing by itself
        r = self.b.approve(AL, self.now)
        self.assertTrue(r["ok"])
        self.assertEqual(self.b.approved[AL]["side"], "YES")
        r = self.b.approve(ALD, self.now)
        self.assertTrue(r["ok"])
        self.assertEqual(self.b.approved[ALD]["side"], "NO")
        self.assertNotIn(AL, self.b.proposed)

    def test_no_notification_is_sent(self):
        self.assertFalse(hasattr(self.b, "alert"))

    def test_proposals_sit_newest_first(self):
        self.b.scan(self.now, force=True)
        self.odds[GA] = 0.995
        self.b.scan(self.now + 3600, force=True)
        v = self.b.view(self.now + 3600)
        self.assertEqual(v["proposed"][0]["market"], GA)

    def test_inside_the_band_cannot_be_added_even_by_hand(self):
        r = self.b.approve(GA, self.now)
        self.assertFalse(r["ok"])
        self.assertIn("71.0%", r["note"])

    def test_a_listed_market_that_leaves_the_band_drops_by_itself(self):
        self.b.approve(AL, self.now)
        self.hold(AL, 100.0, 0.98)
        self.odds[AL] = 0.984
        self.b.scan(self.now + 1, force=True)
        self.assertNotIn(AL, self.b.approved)
        self.assertIn(AL, self.b.dropped)
        v = self.b.view(self.now + 1, self.positions())
        self.assertEqual(v["dropped"][0]["market"], AL)
        self.assertEqual(v["dropped"][0]["held"], 100.0)     # still held, shown
        # the engine still leaves that stock alone
        self.assertIn(AL, self.r.fam.bond_markets)

    def test_the_engine_keeps_quoting_bond_markets(self):
        self.b.approve(AL, self.now)
        self.assertFalse(self.r.fam._frozen(AL))
        self.assertTrue(self.r.fam.enterable(AL))
        self.assertIn(AL, self.r.fam.bond_markets)       # but rests no exits there

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
        self.assertEqual(self.b.scan(t + 3600), [])           # not until tomorrow
        self.assertIn(GA, self.b.scan(t + 86400))


class TestThePage(Base):
    def test_rows_are_cheapest_per_dollar_first_with_yield_and_earnings(self):
        self.b.approve(AL, self.now)
        self.b.approve(TN, self.now)
        self.b.approve(ALD, self.now)
        self.r.cache.put(TN, yes_book(self.now, bid=0.97, ask=0.98))
        v = self.b.view(self.now)
        self.assertEqual([r["market"] for r in v["rows"]], [TN, AL, ALD])
        tn, al, ald = v["rows"]
        self.assertEqual(tn["cost"], 0.98)                    # the YES ask
        self.assertAlmostEqual(tn["yield"], (1 - 0.98) / 0.98, places=4)
        self.assertAlmostEqual(tn["annual"], tn["yield"] * 365 / tn["days"],
                               places=3)
        self.assertEqual(ald["bond"], "NO")
        self.assertEqual(ald["cost"], 0.99)                   # 1 - the YES bid
        self.assertIsNotNone(tn["earn"])
        self.assertIn("est_day", tn["earn"])


class TestTheMoney(Base):
    def test_switch_off_places_nothing(self):
        self.b.approve(AL, self.now)
        self.hold(AL, 1500.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=False)
        self.assertEqual(self.r.exchange.live, {})

    def test_a_held_yes_bond_gets_one_ask_at_the_touch_and_keeps_it(self):
        self.b.approve(AL, self.now)
        self.hold(AL, 1500.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        asks = self.orders(AL, "SELL")
        self.assertEqual(len(asks), 1)
        self.assertEqual((asks[0].purpose, asks[0].intent), ("bond", SELL_LONG))
        self.assertEqual(asks[0].qty, 1500.0)
        self.assertAlmostEqual(asks[0].price, 0.99)
        # minnows undercut to 98c: we are patient, the ask stays put
        self.r.cache.put(AL, yes_book(self.now + 700, bid=0.97, ask=0.98))
        self.r.now += 700
        self.b.cycle(self.r.now, self.positions(), on=True)
        asks = self.orders(AL, "SELL")
        self.assertEqual(len(asks), 1)
        self.assertAlmostEqual(asks[0].price, 0.99)

    def test_the_ask_never_rests_under_cost(self):
        self.b.approve(AL, self.now)
        self.hold(AL, 100.0, 0.985)         # paid 98.5c; the touch is 98c
        self.r.cache.put(AL, yes_book(self.now, bid=0.97, ask=0.98))
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertAlmostEqual(self.orders(AL, "SELL")[0].price, 0.99)

    def test_a_held_no_bond_gets_a_cover_bid_never_above_what_it_sold_at(self):
        self.b.approve(ALD, self.now)
        self.hold(ALD, -1000.0, 0.01)       # short 1000 YES at 1c = NO at 99c
        self.b.cycle(self.now, self.positions(), on=True)
        bids = self.orders(ALD, "BUY")
        self.assertEqual(len(bids), 1)
        self.assertEqual((bids[0].purpose, bids[0].intent), ("bond", SELL_SHORT))
        self.assertAlmostEqual(bids[0].price, 0.01)
        self.assertEqual(bids[0].qty, 1000.0)

    def test_a_sale_by_our_ask_is_reinvested_by_taking_the_cheapest_touch(self):
        self.b.approve(AL, self.now)
        self.b.approve(TN, self.now)
        self.r.cache.put(TN, yes_book(self.now, bid=0.97, ask=0.98, ask_q=120.0))
        self.hold(AL, 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        ask = self.orders(AL, "SELL")[0]
        # the exchange fills our ask: order gone, position flat
        self.r.exchange.live.pop(ask.id, None)
        self.r.fam.orders.pop(ask.id, None)
        self.r.positions[AL] = (0.0, 0.0)
        self.r.fam.inventory.pop(AL, None)
        out = self.b.cycle(self.now + 60, self.positions(), on=True)
        takes = [o for o in self.r.fam.orders.values() if o.side == "BUY"]
        self.assertEqual(len(takes), 1)
        self.assertEqual(takes[0].market, TN)                # cheaper per dollar
        self.assertEqual((takes[0].purpose, takes[0].intent), ("bond", BUY_LONG))
        self.assertAlmostEqual(takes[0].price, 0.98)          # AT the ask, not under it
        self.assertEqual(takes[0].qty, 101.0)                 # 99 / 0.98, whole shares
        self.assertTrue(out["placed"][0]["taken"])
        self.assertAlmostEqual(self.b.cash, 99.0 - 101 * 0.98, places=2)

    def test_taking_never_exceeds_what_the_touch_shows(self):
        self.b.approve(TN, self.now)
        self.r.cache.put(TN, yes_book(self.now, bid=0.97, ask=0.98, ask_q=20.0))
        self.b.cash = 500.0
        self.b.cycle(self.now, self.positions(), on=True)
        takes = [o for o in self.r.fam.orders.values() if o.side == "BUY"]
        self.assertEqual(takes[0].qty, 20.0)

    def test_a_hand_sale_with_our_ask_untouched_is_not_reinvested(self):
        self.b.approve(AL, self.now)
        self.hold(AL, 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        self.r.positions[AL] = (60.0, 58.8)     # the owner sold 40 by hand
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(self.b.cash, 0.0)

    def test_persistence_round_trip(self):
        self.b.approve(AL, self.now)
        self.b.approve(ALD, self.now)
        self.b.ignore(GA, self.now)
        self.b.cash = 12.5
        self.b.scan_day = "2026-09-03"
        d = self.b.to_dict()
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        b2.restore(d)
        self.assertEqual(set(b2.approved), {AL, ALD})
        self.assertEqual(b2.approved[ALD]["side"], "NO")
        self.assertEqual(set(b2.ignored), {GA})
        self.assertEqual(b2.cash, 12.5)
        self.assertEqual(b2.scan_day, "2026-09-03")
        self.assertEqual(self.r.fam.bond_markets, {AL, ALD})


class TestTheDesksSecondCarveOut(Base):
    """Owner, 2026-09-02: take the touch for proceeds. Owner's rail
    only, never past the touch, never more than it shows, and nothing
    else may cross."""

    def test_only_the_owner_may_take_and_never_past_the_touch(self):
        d = self.r.fam.desk
        r = d.place_resting(AL, "BUY", 0.99, 10.0, intent=BUY_LONG,
                            initiator="auto", taker="bond")
        self.assertFalse(r.ok)
        self.assertIn("owner", r.note)
        # the ask is 98c on this book; a 99c bid would pay past the touch
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
        r = d.place_resting(ALD, "SELL", 0.01, 401.0, intent=BUY_SHORT,
                            initiator="owner", taker="bond")
        self.assertFalse(r.ok)

    def test_nothing_else_may_cross_under_the_bond_flag(self):
        d = self.r.fam.desk
        r = d.place_resting(AL, "SELL", 0.98, 10.0, intent=SELL_LONG,
                            net_position=10.0, initiator="owner", taker="bond")
        self.assertFalse(r.ok)
        self.assertIn("only open a bond", r.note)


class TestEngineHandsOff(unittest.TestCase):
    def test_bond_orders_are_treated_like_the_owners_hand(self):
        import inspect
        from v3 import family
        src = inspect.getsource(family)
        self.assertNotIn('rec.purpose == "manual" or self._frozen', src)
        self.assertGreaterEqual(src.count('"bond"'), 10)
        self.assertIn("if slug in self.bond_markets:", src)


if __name__ == "__main__":
    unittest.main()
