"""A hand move whose cancel failed (owner, 2026-09-05: "I'm currently
resting more sell orders than I have shares to sell"): the original
stays tracked — not deleted, not re-adopted as a hand order — and the
cancel is retried every cycle, switch on or off."""

import os
import tempfile
import unittest

from v3.family import FamilyOrder
from v3.intents import SELL_LONG
from v3.main import Monitor
from v3.orders import OrderResult
from v3.tests.test_family import A, Rig


class TestTheMoveEndpoint(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(self.dir.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        self.mon = Monitor()
        self.fam = self.mon.families["politics"]
        self.fam.orders["o1"] = FamilyOrder(
            id="o1", market=A, side="SELL", price=0.95, qty=210.0,
            intent=SELL_LONG, placed_ts=1.0, purpose="bond", why="bond: exit")

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def test_a_failed_cancel_keeps_the_original_tracked(self):
        self.fam.desk.reprice = lambda existing, new_price, new_qty=None, initiator="auto": (
            OrderResult(ok=True, order_id="o2", price=new_price, two_orders=True,
                        note="replacement resting (id o2) but the original o1 "
                             "failed to cancel — two orders on the book"))
        r = self.mon.order_op("move", "o1", price=0.94)
        self.assertTrue(r["ok"])
        self.assertIn("o2", self.fam.orders)
        self.assertIn("o1", self.fam.orders)                        # not deleted
        self.assertEqual(self.fam.orders["o1"].why,
                         "cancel failed during a move — retrying")
        self.assertEqual(self.fam.orders["o1"].price, 0.95)

    def test_a_clean_move_drops_the_original(self):
        self.fam.desk.reprice = lambda existing, new_price, new_qty=None, initiator="auto": (
            OrderResult(ok=True, order_id="o2", price=new_price, note="repriced"))
        r = self.mon.order_op("move", "o1", price=0.94)
        self.assertTrue(r["ok"])
        self.assertNotIn("o1", self.fam.orders)
        self.assertIn("o2", self.fam.orders)


class TestTheRetry(unittest.TestCase):
    def test_the_zombie_dies_with_the_switch_off(self):
        r = Rig(switch=False)
        r.add_market(A)
        r.cycle()                                                   # discovery
        r.exchange.live["z1"] = {"id": "z1", "market": A, "side": "SELL",
                                 "price": 0.95, "size": 210.0, "intent": SELL_LONG}
        r.fam.orders["z1"] = FamilyOrder(
            id="z1", market=A, side="SELL", price=0.95, qty=210.0, intent=SELL_LONG,
            placed_ts=r.now, purpose="bond",
            why="cancel failed during a move — retrying")
        r.cycle()
        self.assertNotIn("z1", r.exchange.live)                     # cancelled
        self.assertNotIn("z1", r.fam.orders)                        # and forgotten


if __name__ == "__main__":
    unittest.main()
