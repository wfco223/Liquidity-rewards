"""A basis the exchange's cost field set is ESTIMATED: shown, never
paged as a loss, never learned from (owner, 2026-09-21 "Yes repair the
fill model").

2026-09-21: House control rep, 235 shares bought at 11.2c, was seeded
from the feed at $105.21 of "cost"; its sales at 10c paged $237.58 of
losses in a day (real: under $5) and taught the fill model 41c a share
at the maximum weight, four times. The pools read 13-22c a share
against measured markdowns of 0.4-2.3c and the tender rested little."""

import unittest

from v3.family import FamilyOrder
from v3.fillmodel import TRIP_REPAIR, FillModel, family_of
from v3.intents import SELL_LONG
from v3.tests.test_family import A, Rig, politics_book


def _sell(r, qty, px):
    rec = FamilyOrder(id=f"x{qty:g}{px:g}", market=A, side="SELL", price=px, qty=qty,
                      intent=SELL_LONG, placed_ts=r.now, purpose="sell")
    r.fam.orders[rec.id] = rec
    return rec


class TestEstimatedBasis(unittest.TestCase):
    def _seeded(self):
        r = Rig()
        r.add_market(A, book=politics_book(r.now))
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        # the feed shows 60 shares we never bought, "cost" $51.00 (85c)
        r.positions[A] = (60.0, 51.0)
        r.fam.last_action.clear()
        r.cycle()
        return r

    def test_a_position_the_feed_seeds_is_estimated(self):
        r = self._seeded()
        inv = r.fam.inventory[A]
        self.assertEqual(inv["qty"], 60.0)
        self.assertTrue(inv.get("est"))

    def test_its_close_pages_no_loss_and_teaches_nothing(self):
        r = self._seeded()
        fm = r.fam.fillmodel
        n0 = fm.trip_n.get(family_of(A), 0)
        r.alerts.clear()
        r.fam._on_fill(_sell(r, 60.0, 0.45), 60.0, r.now)      # "$24 lost"
        self.assertEqual(fm.trip_n.get(family_of(A), 0), n0)
        self.assertFalse([a for a in r.alerts if "closed at a loss" in a[0]], r.alerts)
        notes = [e for e in r.fam.log if e.get("event") == "fill_no_page"
                 and "estimated" in (e.get("note") or "")]
        self.assertTrue(notes)
        self.assertNotIn(A, r.fam.inventory)

    def test_our_own_fill_is_a_real_basis_and_its_loss_is_learned(self):
        r = Rig()
        fm = r.fam.fillmodel
        r.fam.inventory[A] = {"qty": 10.0, "cost": 6.0}         # ours: 60c
        self.assertFalse(r.fam.inventory[A].get("est"))
        r.fam._on_fill(_sell(r, 10.0, 0.40), 10.0, r.now)        # $2.00 lost
        self.assertEqual(fm.trip_n.get(family_of(A), 0), 1)
        self.assertTrue([a for a in r.alerts if "closed at a loss" in a[0]])

    def test_a_flip_through_zero_clears_the_flag(self):
        r = Rig()
        r.fam.inventory[A] = {"qty": 10.0, "cost": 8.5, "est": True}
        r.fam._on_fill(_sell(r, 15.0, 0.40), 15.0, r.now)
        inv = r.fam.inventory[A]
        self.assertAlmostEqual(inv["qty"], -5.0)
        self.assertAlmostEqual(inv["cost"], -2.0)                # this fill's price
        self.assertFalse(inv.get("est"))

    def test_the_feed_correction_keeps_our_basis_but_seeds_an_estimate_from_nothing(self):
        r = Rig()
        r.add_market(A, book=politics_book(r.now))
        r.cycle()
        r.fam.inventory[A] = {"qty": 10.0, "cost": 6.0}
        r.fam.positions_seen[A] = 10.0
        r.positions[A] = (12.0, 99.0)          # the feed: 12, a cost field of its own
        r.fam.last_action.clear()
        r.now += 600.0
        r.cycle()
        inv = r.fam.inventory[A]
        self.assertEqual(inv["qty"], 12.0)
        self.assertAlmostEqual(inv["cost"], 7.2)                 # our 60c a share, scaled
        self.assertFalse(inv.get("est"))

    def test_restore_flags_a_basis_outside_the_price_range(self):
        r = Rig()
        d = r.fam.to_dict()
        d["inventory"] = {A: {"qty": 9.0, "cost": 79.61}}       # House control rep, 09-21
        r2 = Rig()
        r2.add_market(A)
        r2.fam.restore(d)
        self.assertTrue(r2.fam.inventory[A].get("est"))

    def test_the_repair_tag_drops_the_stored_trip_costs_once(self):
        fm = FillModel.from_dict({"trip_repair": "basis-sign-2026-09-12",
                                  "trip_cost": {"other": 0.217, "governor": 0.13},
                                  "trip_n": {"other": 114, "governor": 74}})
        self.assertEqual(fm.trip_cost, {})
        self.assertEqual(fm.trip_n, {})
        self.assertEqual(fm.trip_repair, TRIP_REPAIR)
        fm2 = FillModel.from_dict(fm.to_dict())
        self.assertEqual(fm2.trip_repair, TRIP_REPAIR)


if __name__ == "__main__":
    unittest.main()
