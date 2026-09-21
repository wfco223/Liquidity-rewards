"""A stock exit resting one tick under the model fair is not stranded
(owner, 2026-09-21 "Yes, let them rest under fair").

2026-09-21, 14:45-15:30Z: on six books the placer rested the exit one
tick under Silver's fair (98c on a 90/91 book priced at 98.8c) because
the touch gave away against the model, and the stranded rule — touch +
2 ticks alone — cancelled it a cycle later; the placer rested it at 98c
again. 23 rounds each in 45 minutes, 138 cancels, the lots off the book
half the time. The bound now allows the placer's own slot."""

import unittest

from v3.family import FamilyOrder
from v3.intents import SELL_LONG
from v3.scoring import Book
from v3.tests.test_family import A, Rig


def _nm(now):
    """New Mexico governor dem, 15:30Z: 90/91, Silver 98.78c."""
    return Book(bids=((0.90, 2579.0), (0.02, 60000.0)),
                asks=((0.91, 5486.0), (0.99, 60000.0)),
                tick=0.01, fetched_at=now)


def _sells(r, market):
    return [o for o in r.fam.orders.values()
            if o.market == market and o.side == "SELL" and o.purpose == "sell"]


def _stranded(r):
    return [l for l in r.fam.log if l.get("event") == "stranded_exit_repriced"]


class TestExitUnderFairStays(unittest.TestCase):
    def _rig(self, fair):
        r = Rig()
        r.add_market(A, book=_nm(r.now))
        r.fam.fairs = lambda slug: fair if slug == A else None
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        r.fam.inventory[A] = {"qty": 20.0, "cost": 17.8}     # 89c basis
        r.positions[A] = (20.0, 17.8)
        r.fam.last_action.clear()
        return r

    def test_the_exit_rests_under_fair_and_is_not_cancelled(self):
        r = self._rig(0.9878)
        r.cycle()
        sells = _sells(r, A)
        self.assertEqual(len(sells), 1, [o.price for o in sells])
        self.assertAlmostEqual(sells[0].price, 0.98, places=3)
        oid = sells[0].id
        for _ in range(3):
            r.fam.last_action.clear()
            r.cycle()
        self.assertIn(oid, r.fam.orders, "the exit was cancelled")
        self.assertEqual(_stranded(r), [])
        sells = _sells(r, A)
        self.assertEqual(len(sells), 1)
        self.assertAlmostEqual(sells[0].price, 0.98, places=3)

    def test_an_exit_past_the_under_fair_slot_is_still_stranded(self):
        # fair 95c: the placer's slot is 94c, the bound 96c; a 98c
        # leftover is past it and comes off, re-rested at the slot
        r = self._rig(0.95)
        r.exchange.live["OLD"] = {"id": "OLD", "market": A, "side": "SELL",
                                  "price": 0.98, "size": 20.0,
                                  "intent": SELL_LONG}
        r.fam.orders["OLD"] = FamilyOrder(
            id="OLD", market=A, side="SELL", price=0.98, qty=20.0,
            intent=SELL_LONG, placed_ts=r.now - 7200.0, purpose="sell",
            live_est=0.0)
        # the mover is throttled by the side's cooldown; the stranded
        # rule is the backstop and answers to no cooldown
        r.fam._mark(A, "SELL", r.now)
        r.cycle()
        self.assertNotIn("OLD", r.fam.orders)
        self.assertEqual(len(_stranded(r)), 1)
        r.fam.last_action.clear()
        r.cycle()
        sells = _sells(r, A)
        self.assertEqual(len(sells), 1, [o.price for o in sells])
        self.assertAlmostEqual(sells[0].price, 0.94, places=3)

    def test_without_a_fair_the_touch_bound_stands(self):
        # no model: a 98c exit on a 90/91 book is stranded as before
        r = self._rig(None)
        r.exchange.live["OLD"] = {"id": "OLD", "market": A, "side": "SELL",
                                  "price": 0.98, "size": 20.0,
                                  "intent": SELL_LONG}
        r.fam.orders["OLD"] = FamilyOrder(
            id="OLD", market=A, side="SELL", price=0.98, qty=20.0,
            intent=SELL_LONG, placed_ts=r.now - 7200.0, purpose="sell",
            live_est=0.0)
        r.fam._mark(A, "SELL", r.now)
        r.cycle()
        self.assertNotIn("OLD", r.fam.orders)
        self.assertEqual(len(_stranded(r)), 1)
        r.fam.last_action.clear()
        r.cycle()
        sells = _sells(r, A)
        self.assertEqual(len(sells), 1, [o.price for o in sells])
        self.assertAlmostEqual(sells[0].price, 0.91, places=3)


if __name__ == "__main__":
    unittest.main()
