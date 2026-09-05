"""Owner, 2026-09-05: "Bond markets ... do not count towards the politics
budget." Orders in the bond side's markets answer to the bond budget:
they are outside the family's expected-risk and worst-day ceilings and
are never trimmed for them."""

import unittest

from v3.tests.test_family import A, Rig


class TestBondMarketsAndTheCeiling(unittest.TestCase):
    def test_orders_in_a_bond_market_do_not_count(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        mine = [o for o in r.fam.orders.values() if o.market == A]
        self.assertTrue(mine)
        spent, gross = r.fam.family_spent(), r.fam.family_gross()
        self.assertGreater(spent, 0.0)
        self.assertGreater(gross, 0.0)
        r.fam.bond_markets = {A}                              # the bond side lists it
        self.assertEqual(r.fam.family_spent(), 0.0)
        self.assertEqual(r.fam.family_gross(), 0.0)

    def test_the_trim_never_reaches_into_a_bond_market(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        ids = {o.id for o in r.fam.orders.values() if o.market == A}
        r.fam.bond_markets = {A}
        r.fam.cfg.capital_usd = 0.01                          # the ceiling is over, on paper
        r.cycle()
        self.assertTrue(ids <= set(r.fam.orders))            # nothing of theirs was trimmed
        self.assertNotIn("trim", [e["event"] for e in r.fam.log])


if __name__ == "__main__":
    unittest.main()
