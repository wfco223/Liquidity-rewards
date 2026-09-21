"""The focus list says what a qualifying wall would add (owner,
2026-09-21 "Show me in the list of focus markets which qualifying
orders I can place to boost earnings")."""

import unittest

from v3 import politics
from v3.bonds import Bonds
from v3.family import FamilyOrder
from v3.focus import Focus
from v3.intents import BUY_SHORT
from v3.scoring import Book
from v3.tests.test_family import Rig
from v3.tests.test_focus import NC, T1, wide_book


def thin_ask_book(now):
    """The ask side under the 25,000 target, the bid side over it."""
    return Book(bids=((0.44, 300.0), (0.43, 500.0), (0.02, 60000.0)),
                asks=((0.47, 300.0), (0.48, 500.0)),
                tick=0.01, fetched_at=now)


class TestWallBoost(unittest.TestCase):
    def setUp(self):
        self.r = Rig(cfg=politics.config())
        self.b = Bonds(self.r.fam, self.r.exchange, lambda s: None,
                       clock=lambda: self.r.now, sleep=lambda s: None)
        self.f = Focus(self.r.fam, self.r.exchange, self.b,
                       fair=lambda s: None, alert=lambda t, m: None,
                       clock=lambda: self.r.now, switch_on=lambda: True,
                       buying_power=lambda: 2000.0)
        self.r.add_market(NC, thin_ask_book(self.r.now), event="nc", prog=T1)
        self.r.switch = False
        self.r.cycle()
        self.f.seed(self.r.now)
        self.r.switch = True
        self.f.set_fair(NC, 45.0)

    def test_an_unqualified_side_shows_what_a_wall_would_add(self):
        row = self.f._row(NC, self.r.now, {}, 2000.0)
        self.assertFalse(row["qual"]["ask"][2])
        self.assertTrue(row["qual"]["bid"][2])
        self.assertIn("ask", row["boost"])
        self.assertNotIn("bid", row["boost"])
        b = row["boost"]["ask"]
        self.assertGreater(b["usd_day"], 0.0, b)
        self.assertEqual(b["px"], 0.99)
        self.assertGreater(b["gap"], 0)
        self.assertTrue(any("new entry" in p for p in b["parts"]), b)
        self.assertAlmostEqual(row["boost_day"], b["usd_day"])
        # without the wall the tender's own plan on that side earns nothing
        self.assertEqual(row["sides"]["SELL"].get("est", 0.0), 0.0)

    def test_a_resting_order_on_the_side_is_what_the_wall_lifts(self):
        rec = FamilyOrder(id="T1", market=NC, side="SELL", price=0.47, qty=100.0,
                          intent=BUY_SHORT, placed_ts=self.r.now, purpose="focus")
        self.r.fam.orders[rec.id] = rec
        self.f.mine_ids.append(rec.id)
        row = self.f._row(NC, self.r.now, {}, 2000.0)
        b = row["boost"]["ask"]
        self.assertGreater(b["usd_day"], 0.0, b)
        self.assertTrue(any("100 @ 47c" in p for p in b["parts"]), b)
        resting = [o for o in row["orders"] if o["id"] == "T1"][0]
        self.assertEqual(resting.get("est", 0.0), 0.0)        # $0.00 a day today

    def test_a_qualified_market_has_no_boost(self):
        self.r.add_market(NC, wide_book(self.r.now), event="nc", prog=T1)
        row = self.f._row(NC, self.r.now, {}, 2000.0)
        self.assertEqual(row["boost"], {})
        self.assertEqual(row["boost_day"], 0.0)


if __name__ == "__main__":
    unittest.main()
