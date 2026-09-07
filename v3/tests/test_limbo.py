"""An order the open-order list leaves out for one read comes back as
the SAME order, not the owner's (RI Senate rep, 2026-09-06: the bond's
exit vanished for a read, came back, was adopted as "the owner's own
order", and the bond sized around it — "no exit resting")."""

import unittest

from v3.tests.test_family import A, Rig


class TestBackFromLimbo(unittest.TestCase):
    def test_an_order_that_vanishes_for_a_read_keeps_its_record(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        ours = {oid: o for oid, o in r.fam.orders.items() if o.market == A}
        self.assertTrue(ours)
        oid, rec = next(iter(ours.items()))
        purpose, why = rec.purpose, rec.why
        self.assertNotEqual(purpose, "manual")
        # the list leaves it out for one read: the record goes to limbo
        stash = r.exchange.live.pop(oid)
        r.cycle()
        self.assertNotIn(oid, r.fam.orders)
        self.assertIn(oid, r.fam.gone_pending)
        # and shows it again: the same order, its record back
        r.exchange.live[oid] = stash
        r.cycle()
        self.assertIn(oid, r.fam.orders)
        self.assertNotIn(oid, r.fam.gone_pending)
        back = r.fam.orders[oid]
        self.assertEqual((back.purpose, back.why), (purpose, why))    # not "the owner's own order"
        self.assertIn("order_back", [e["event"] for e in r.fam.log])
        self.assertNotIn("owner_orders_seen", [e["event"] for e in r.fam.log])


if __name__ == "__main__":
    unittest.main()
