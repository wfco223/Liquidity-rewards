"""Bond orders charge no engine ceiling (owner, 2026-09-04, "Yes to #1"):
the program is his money on his caps, like his hand orders. On
2026-09-04 a $300 Illinois bond bid in a market not yet graduated put
$114 of expected risk on the engine's $250 search ceiling and the
trimmer cut the politics book from 50 orders to 11."""

import unittest

from v3.family import FamilyOrder
from v3.intents import BUY_LONG
from v3.tests.test_family import A, B, Rig


def _bond_bid(r, oid, market, price, qty):
    r.exchange.live[oid] = {"id": oid, "market": market, "side": "BUY",
                            "price": price, "size": qty, "intent": BUY_LONG}
    rec = FamilyOrder(id=oid, market=market, side="BUY", price=price,
                      qty=qty, intent=BUY_LONG, placed_ts=r.now,
                      purpose="bond", why="bond more: buying up to $300",
                      live_pf=0.38)
    r.fam.orders[oid] = rec
    return rec


class TestBondOrdersAreTheOwners(unittest.TestCase):
    def test_no_expected_or_gross_charge(self):
        r = Rig()
        r.add_market(A)
        r.add_market(B)
        base_spent = r.fam.family_spent()
        base_gross = r.fam.family_gross()
        _bond_bid(r, "BND", B, 0.94, 319.0)          # $300 of collateral
        self.assertAlmostEqual(r.fam.family_spent(), base_spent, 6)
        self.assertAlmostEqual(r.fam.family_gross(), base_gross, 6)
        self.assertTrue(r.fam._owner_exit(r.fam.orders["BND"]))
        self.assertTrue(r.fam._owner_exit(FamilyOrder(
            id="M", market=A, side="BUY", price=0.1, qty=1.0,
            intent=BUY_LONG, placed_ts=r.now, purpose="manual")))
        self.assertFalse(r.fam._owner_exit(FamilyOrder(
            id="E", market=A, side="BUY", price=0.1, qty=1.0,
            intent=BUY_LONG, placed_ts=r.now, purpose="earn")))

    def test_a_big_bond_bid_does_not_trim_the_engine_s_book(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        earn = [o for o in r.fam.orders.values() if o.purpose == "earn"]
        self.assertTrue(earn)
        # a bond bid worth more than the whole family ceiling
        _bond_bid(r, "BND", A, 0.94, 319.0)
        r.fam.orders["BND"].live_pf = 0.9
        r.cycle()
        r.cycle()
        self.assertFalse(any(l.get("event") == "trim" for l in r.fam.log))
        self.assertTrue([o for o in r.fam.orders.values()
                         if o.purpose == "earn"])


if __name__ == "__main__":
    unittest.main()
