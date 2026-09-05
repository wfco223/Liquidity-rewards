"""No money to deploy — the families too (owner, 2026-09-05: "put a no
money sell gate on politics and cfb"): nothing new that buys, the
resting orders stay and keep earning, exits keep working."""

import unittest

from v3.family import FamilyOrder
from v3.intents import SELL_LONG
from v3.tests.test_family import A, Rig


class TestTheFamilyMoneyGate(unittest.TestCase):
    def cycle(self, r, money_out, advance=60.0):
        r.now += advance
        return r.fam.cycle(r.now, r.exchange.open_orders(), r.positions,
                           r.exchange, r.switch, money_out=money_out)

    def test_nothing_new_is_bought_while_the_money_is_out(self):
        r = Rig()
        r.add_market(A)
        self.cycle(r, False)
        n0 = len(r.fam.orders)
        self.assertGreater(n0, 0)                          # it entered
        # the money is out: the book stays as it is, nothing new
        s = self.cycle(r, True)
        self.assertEqual(s["mode"], "no money — exits only")
        self.assertEqual(set(r.fam.orders), set(r.exchange.live))
        self.assertEqual(len(r.fam.orders), n0)
        self.assertIn("money_out", [e["event"] for e in r.fam.log])
        for _ in range(3):
            self.cycle(r, True, advance=900.0)
        self.assertEqual(len(r.fam.orders), n0)             # still nothing new
        # money is back: buying resumes
        s = self.cycle(r, False)
        self.assertNotEqual(s.get("mode"), "no money — exits only")
        self.assertIn("money_back", [e["event"] for e in r.fam.log])

    def test_exits_still_rest_with_the_money_out(self):
        r = Rig()
        r.add_market(A)
        self.cycle(r, False)
        # no ask of ours rests, and stock lands: the seller must still
        # rest an exit for it while the money is out
        for oid, o in list(r.fam.orders.items()):
            if o.side == "SELL":
                del r.fam.orders[oid]
                r.exchange.live.pop(oid, None)
        r.positions[A] = (10.0, 4.4)
        r.fam.inventory[A] = {"qty": 10.0, "cost": 4.4}
        r.fam.positions_seen[A] = 10.0
        for _ in range(3):
            self.cycle(r, True)
        exits = [o for o in r.fam.orders.values() if o.purpose == "sell"]
        self.assertTrue(exits)                                  # rested with the money out
        self.assertTrue(any(o.market == A and o.side == "SELL" and o.intent == SELL_LONG
                            for o in exits))                    # the stock's own exit
        self.assertTrue(all(o.id in r.exchange.live for o in exits))

    def test_a_manual_order_is_untouched_either_way(self):
        r = Rig()
        r.add_market(A)
        self.cycle(r, False)
        r.exchange.live["hand"] = {"id": "hand", "market": A, "side": "BUY", "price": 0.40,
                                   "size": 5.0, "intent": "ORDER_INTENT_BUY_LONG"}
        r.fam.orders["hand"] = FamilyOrder(id="hand", market=A, side="BUY", price=0.40,
                                           qty=5.0, intent="ORDER_INTENT_BUY_LONG",
                                           placed_ts=r.now, purpose="manual",
                                           why="the owner's own order")
        self.cycle(r, True)
        self.assertIn("hand", r.fam.orders)
        self.assertIn("hand", r.exchange.live)


if __name__ == "__main__":
    unittest.main()
