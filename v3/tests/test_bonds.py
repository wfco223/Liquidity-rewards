"""Bonds: the tax reserve earning like a bond (owner, 2026-09-02).

"Designate any politics market where the Nate Silver gives >99% odds
of an outcome as a bond market... give me a list of the cheapest bond
positions with information on what a resting sell order would earn.
Before adding a new market to the bond list ask me first... Once I'm
in the positions they should automatically [have] a sell order placed
so that they are earning. Then when the sale goes through I'll want
the money reinvested in the cheapest market where it can earn rewards."
"""

import unittest

from v3.bonds import BOND_ODDS, Bonds
from v3.family import FamilyConfig, FamilyOrder
from v3.intents import SELL_LONG
from v3.scoring import Book
from v3.tests.test_family import LIVE_PROG, A, B, Rig

AL = "usgubewc-usgub-al-2026-11-03-rep"     # Silver 99.4 for the GOP
TN = "usgubewc-usgub-tn-2026-11-03-rep"     # Silver 99.1
GA = "usgubewc-usgub-ga-2026-11-03-rep"     # Silver 71: not a bond


def bond_book(now, bid=0.98, ask=0.99, bid_q=50.0, ask_q=300.0):
    return Book(bids=((bid, bid_q), (0.50, 20000.0)),
                asks=((ask, ask_q), (0.999, 20000.0)),
                tick=0.01, fetched_at=now)


class Base(unittest.TestCase):
    def setUp(self):
        self.r = Rig(cfg=FamilyConfig(name="Politics", tag="POL",
                                      known_ground=True, capital_usd=0.0))
        self.odds = {AL: 0.994, TN: 0.991, GA: 0.71}
        self.alerts = []
        self.b = Bonds(self.r.fam, self.r.exchange,
                       lambda s: self.odds.get(s),
                       alert=lambda t, m: self.alerts.append((t, m)),
                       clock=lambda: self.r.now)
        for s in (AL, TN, GA):
            self.r.fam.universe[s] = {"event_n": 1, "name": s}
            self.r.exchange.books[s] = bond_book(self.r.now)
            self.r.cache.put(s, bond_book(self.r.now))
            import copy
            self.r.exchange.prog_raw[s] = copy.deepcopy(LIVE_PROG)
        # terms for the three, read through the family's own path
        self.r.fam.refresh_terms(self.r.exchange, self.r.now)
        self.now = self.r.now

    def hold(self, slug, qty, cost_px):
        self.r.positions[slug] = (qty, qty * cost_px)
        self.r.fam.inventory[slug] = {"qty": qty, "cost": qty * cost_px}

    def positions(self):
        return dict(self.r.positions)


class TestTheList(Base):
    def test_silver_proposes_and_only_the_owner_adds(self):
        new = self.b.scan(self.now, force=True)
        self.assertEqual(set(new), {AL, TN})          # GA at 71% is not
        self.assertEqual(self.b.approved, {})           # nothing added by itself
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("99%+", self.alerts[0][1])
        r = self.b.approve(AL, self.now)
        self.assertTrue(r["ok"])
        self.assertIn(AL, self.b.approved)
        self.assertNotIn(AL, self.b.proposed)
        # the engine now treats it as frozen ground
        self.assertTrue(self.r.fam._frozen(AL))
        self.assertFalse(self.r.fam.enterable(AL))
        self.assertFalse(self.r.fam._frozen(TN))

    def test_under_the_bar_cannot_be_added_even_by_hand(self):
        r = self.b.approve(GA, self.now)
        self.assertFalse(r["ok"])
        self.assertIn("71.0%", r["note"])
        self.assertNotIn(GA, self.b.approved)

    def test_ignore_is_remembered_and_reversible(self):
        self.b.scan(self.now, force=True)
        self.b.ignore(TN, self.now)
        self.assertNotIn(TN, self.b.proposed)
        self.assertEqual(self.b.scan(self.now + 1, force=True), [])   # not again
        self.b.unignore(TN)
        self.assertIn(TN, self.b.scan(self.now + 2, force=True))

    def test_a_listed_market_that_slips_is_flagged_not_dropped(self):
        self.b.approve(AL, self.now)
        self.odds[AL] = 0.984
        self.b.scan(self.now + 1, force=True)
        self.assertIn(AL, self.b.approved)
        v = self.b.view(self.now)
        row = next(r for r in v["rows"] if r["market"] == AL)
        self.assertTrue(row["flag"])
        self.assertAlmostEqual(row["odds"], 0.984)

    def test_the_scan_is_paced(self):
        self.assertEqual(set(self.b.scan(self.now)), {AL, TN})  # first call runs
        before = dict(self.b.proposed)
        self.odds[GA] = 0.995
        self.assertEqual(self.b.scan(self.now + 60), [])  # ...the next waits
        self.assertEqual(self.b.proposed, before)
        self.assertIn(GA, self.b.scan(self.now + 700))


class TestThePage(Base):
    def test_rows_are_cheapest_first_with_the_yield_and_the_sell_estimate(self):
        self.b.approve(AL, self.now)
        self.b.approve(TN, self.now)
        self.r.cache.put(TN, bond_book(self.now, bid=0.97, ask=0.98))
        v = self.b.view(self.now)
        self.assertEqual([r["market"] for r in v["rows"]], [TN, AL])
        tn = v["rows"][0]
        self.assertEqual(tn["ask"], 0.98)
        self.assertAlmostEqual(tn["yield"], (1 - 0.98) / 0.98, places=4)
        self.assertGreater(tn["days"], 0)
        self.assertAlmostEqual(tn["annual"], tn["yield"] * 365 / tn["days"],
                               places=3)
        self.assertIsNotNone(tn["sell"])
        self.assertIn("est_day", tn["sell"])
        self.assertEqual(v["cash"], 0.0)


class TestTheMoney(Base):
    def test_switch_off_places_nothing(self):
        self.b.approve(AL, self.now)
        self.hold(AL, 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=False)
        self.assertEqual(self.r.exchange.live, {})

    def test_a_held_bond_gets_a_resting_ask_at_the_touch(self):
        self.b.approve(AL, self.now)
        self.hold(AL, 100.0, 0.98)
        out = self.b.cycle(self.now, self.positions(), on=True)
        asks = [o for o in self.r.fam.orders.values()
                if o.market == AL and o.side == "SELL"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0].purpose, "bond")
        self.assertEqual(asks[0].qty, 100.0)
        self.assertAlmostEqual(asks[0].price, 0.99)      # the ask touch
        self.assertEqual(out["placed"][0]["side"], "SELL")
        # a second cycle does not double up
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(len([o for o in self.r.fam.orders.values()
                              if o.market == AL]), 1)

    def test_the_ask_never_rests_under_cost(self):
        self.b.approve(AL, self.now)
        self.hold(AL, 100.0, 0.985)         # paid 98.5c; the touch is 98c
        self.r.cache.put(AL, bond_book(self.now, bid=0.97, ask=0.98))
        self.b.cycle(self.now, self.positions(), on=True)
        ask = next(o for o in self.r.fam.orders.values() if o.market == AL)
        self.assertAlmostEqual(ask.price, 0.99)     # cost rounded UP onto the grid

    def test_a_sale_by_our_ask_is_counted_and_reinvested_cheapest_first(self):
        self.b.approve(AL, self.now)
        self.b.approve(TN, self.now)
        self.r.cache.put(TN, bond_book(self.now, bid=0.97, ask=0.98))
        self.hold(AL, 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        ask = next(o for o in self.r.fam.orders.values() if o.market == AL)
        # the exchange fills our ask: the order is gone, the position is flat
        self.r.exchange.live.pop(ask.id, None)
        self.r.fam.orders.pop(ask.id, None)
        self.r.positions[AL] = (0.0, 0.0)
        self.r.fam.inventory.pop(AL, None)
        out = self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertAlmostEqual(self.b.cash + self.b._committed(), 99.0, places=2)
        bids = [o for o in self.r.fam.orders.values() if o.side == "BUY"]
        self.assertEqual(len(bids), 1)
        self.assertEqual(bids[0].market, TN)               # the cheaper ask
        self.assertEqual(bids[0].purpose, "bond")
        self.assertAlmostEqual(bids[0].price, 0.97)        # joins the bid
        self.assertEqual(bids[0].qty, 102.0)               # 99 / 0.97, whole shares
        self.assertEqual(out["placed"][0]["side"], "BUY")

    def test_a_hand_sale_with_our_ask_untouched_is_not_reinvested(self):
        self.b.approve(AL, self.now)
        self.hold(AL, 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        # the owner sells 40 by hand; our 100-share ask still rests
        self.r.positions[AL] = (60.0, 58.8)
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(self.b.cash, 0.0)

    def test_persistence_round_trip(self):
        self.b.approve(AL, self.now)
        self.b.ignore(GA, self.now)
        self.b.cash = 12.5
        d = self.b.to_dict()
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        b2.restore(d)
        self.assertEqual(set(b2.approved), {AL})
        self.assertEqual(set(b2.ignored), {GA})
        self.assertEqual(b2.cash, 12.5)
        self.assertIn(AL, self.r.fam.freeze_dyn)


class TestEngineHandsOff(unittest.TestCase):
    def test_bond_orders_are_never_repriced_or_pulled(self):
        # the exemption sites treat purpose "bond" like the owner's hand
        import inspect
        from v3 import family
        src = inspect.getsource(family)
        self.assertNotIn('rec.purpose == "manual" or self._frozen', src)
        self.assertIn('("manual", "bond")', src)
        self.assertGreaterEqual(src.count('"bond"'), 8)


if __name__ == "__main__":
    unittest.main()
