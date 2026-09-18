"""Owner, 2026-09-18 ("Yes, ship both"), after the morning's wipe and
the fills that followed it:

(A) the exchange's position feed lags a fill by a read or more. Eleven
times that day a cover filled, the feed still showed the old short for
one read, the book snapped back to it ("exchange wins"), and a minute
later purged it again; in one of those windows the engine rested a
second cover on the phantom short and it filled (Louisiana senate dem,
2 @ 11c). For FEED_LAG_GRACE_S after a fill we booked, the book stands.

(B) a fill that FLIPS a position through zero opens the other side, so
the new side's basis is that fill's price. Carried through, the 2028
Dwayne Johnson lot flipped long -> short through 1c sales, the short
kept the long's basis, and the dead-short step-up — bounded to 5 ticks
over "what the short sold for" — bought 15 back at 50c. And the step-up
never runs on close-out ground at all."""
import unittest

from v3.family import FEED_LAG_GRACE_S, FamilyOrder
from v3.intents import BUY_LONG, SELL_LONG
from v3.tests.test_family import A, Rig


def _cover(r, qty=10.0, px=0.2):
    return FamilyOrder(id="cov1", market=A, side="BUY", price=px, qty=qty,
                       intent=BUY_LONG, placed_ts=r.now - 600.0,
                       purpose="sell", why="an exit")


class TestTheFeedDoesNotOverwriteAFreshFill(unittest.TestCase):
    def rig(self):
        r = Rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": -10.0, "cost": -2.0}
        r.fam.positions_seen[A] = -10.0
        return r

    def test_within_the_grace_the_book_stands(self):
        r = self.rig()
        r.fam._on_fill(_cover(r), 10.0, r.now)        # the cover filled: flat
        self.assertNotIn(A, r.fam.inventory)
        # the feed still shows the short one read later
        r.fam.reconcile([], {A: (-10.0, 2.0)}, r.now + 20.0)
        self.assertNotIn(A, r.fam.inventory)
        lag = [e for e in r.fam.log if e.get("event") == "feed_lag"]
        self.assertEqual(len(lag), 1)
        self.assertIn("the book stands", lag[0]["note"])
        self.assertFalse([e for e in r.fam.log
                          if e.get("event") == "inventory_corrected"])
        # said once a window, not once a cycle
        r.fam.reconcile([], {A: (-10.0, 2.0)}, r.now + 80.0)
        self.assertEqual(len([e for e in r.fam.log
                              if e.get("event") == "feed_lag"]), 1)
        self.assertNotIn(A, r.fam.inventory)

    def test_after_the_grace_the_exchange_wins_as_before(self):
        r = self.rig()
        r.fam._on_fill(_cover(r), 10.0, r.now)
        r.fam.reconcile([], {A: (-10.0, 2.0)}, r.now + FEED_LAG_GRACE_S + 1.0)
        self.assertEqual(r.fam.inventory[A]["qty"], -10.0)
        self.assertTrue([e for e in r.fam.log
                         if e.get("event") == "inventory_corrected"])

    def test_with_no_fill_in_the_window_the_exchange_wins_at_once(self):
        r = Rig()
        r.add_market(A)
        r.fam.positions_seen[A] = 0.0                  # a market we track
        r.fam.reconcile([], {A: (-10.0, 2.0)}, r.now + 20.0)
        self.assertEqual(r.fam.inventory[A]["qty"], -10.0)
        self.assertFalse([e for e in r.fam.log if e.get("event") == "feed_lag"])

    def test_a_fill_that_does_not_move_the_book_needs_no_grace(self):
        # feed and book agree: nothing to hold, nothing to say
        r = self.rig()
        r.fam._on_fill(_cover(r, qty=4.0), 4.0, r.now)
        self.assertEqual(r.fam.inventory[A]["qty"], -6.0)
        r.fam.reconcile([], {A: (-6.0, 1.2)}, r.now + 20.0)
        self.assertEqual(r.fam.inventory[A]["qty"], -6.0)
        self.assertFalse([e for e in r.fam.log if e.get("event") == "feed_lag"])

    def test_the_stamp_survives_a_restart(self):
        r = self.rig()
        r.fam._on_fill(_cover(r), 10.0, r.now)
        d = r.fam.to_dict()
        self.assertAlmostEqual(d["fill_at"][A], r.now)
        r2 = Rig()
        r2.now = r.now
        r2.add_market(A)
        r2.fam.restore(d)
        r2.fam.reconcile([], {A: (-10.0, 2.0)}, r.now + 20.0)
        self.assertNotIn(A, r2.fam.inventory)


class TestAFlipTakesTheFillsPrice(unittest.TestCase):
    def test_long_to_short_through_a_sale(self):
        r = Rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": 30.0, "cost": 13.5}       # 45c a share
        sale = FamilyOrder(id="s1", market=A, side="SELL", price=0.01, qty=45.0,
                           intent=SELL_LONG, placed_ts=r.now - 60.0,
                           purpose="manual", why="his")
        r.fam._on_fill(sale, 45.0, r.now)
        inv = r.fam.inventory[A]
        self.assertEqual(inv["qty"], -15.0)
        self.assertAlmostEqual(inv["cost"], -0.15)              # sold at 1c
        self.assertAlmostEqual(inv["cost"] / inv["qty"], 0.01)

    def test_short_to_long_through_a_buy(self):
        r = Rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": -10.0, "cost": -2.0}      # sold at 20c
        buy = FamilyOrder(id="b1", market=A, side="BUY", price=0.3, qty=25.0,
                          intent=BUY_LONG, placed_ts=r.now - 60.0,
                          purpose="earn", why="t")
        r.fam._on_fill(buy, 25.0, r.now)
        inv = r.fam.inventory[A]
        self.assertEqual(inv["qty"], 15.0)
        self.assertAlmostEqual(inv["cost"], 4.5)                # bought at 30c

    def test_a_reduce_keeps_the_average_cost(self):
        r = Rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": 30.0, "cost": 13.5}
        sale = FamilyOrder(id="s2", market=A, side="SELL", price=0.5, qty=10.0,
                           intent=SELL_LONG, placed_ts=r.now - 60.0,
                           purpose="sell", why="an exit")
        r.fam._on_fill(sale, 10.0, r.now)
        inv = r.fam.inventory[A]
        self.assertEqual(inv["qty"], 20.0)
        self.assertAlmostEqual(inv["cost"], 8.5)


class TestNoStepUpOnCloseOutGround(unittest.TestCase):
    def test_a_dead_buyback_on_close_out_ground_is_not_stepped_up(self):
        from v3.tests.test_flatten import TestDeadShortStepUp
        t = TestDeadShortStepUp()
        r, slug = t._rig()
        r.fam.cfg.liquidate_tokens = ("vmc-ussemov-ga",)      # A is close-out ground
        self.assertTrue(r.fam._liquidating(slug))
        buybacks = [o for o in r.fam.orders.values()
                    if o.market == slug and o.side == "BUY"
                    and o.purpose == "sell"]
        self.assertTrue(buybacks)                              # the cover still rests
        for o in buybacks:
            o.live_est = 0.0                                   # measured: earning nothing
        r.fam.last_action.clear()
        r.cycle()
        r.fam.last_action.clear()
        r.cycle()
        self.assertFalse([e for e in r.fam.log
                          if e.get("event") == "dead_short_stepup"])
        self.assertFalse([w for w in r.fam.wind_down
                          if w.get("kind") == "short step-up"])


if __name__ == "__main__":
    unittest.main()
