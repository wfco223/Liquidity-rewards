"""The engine feeds the fill model with its own orders (owner,
2026-09-04): resting time accrues per distance cell and per age as the
orders sit, a fill lands in the cell it ended, bond orders are left out,
and the family banks what the model expected hour by hour."""

import unittest

from v3.family import FamilyOrder
from v3.fillmodel import family_of
from v3.intents import BUY_LONG, SELL_LONG
from v3.tests.test_family import A, Rig


class TestRestingFeedsTheModel(unittest.TestCase):
    def test_resting_time_and_a_fill_land_in_the_same_cell(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        bid = next(o for o in r.fam.orders.values()
                   if o.side == "BUY" and o.purpose == "earn")
        fm = r.fam.fillmodel
        self.assertEqual(fm.own_obs, {})          # nothing rested yet
        r.cycle()                                  # the first look stamps
        self.assertEqual(bid.seen_ts, r.now)
        self.assertEqual(fm.own_obs, {})
        r.cycle()
        r.cycle()
        fam = family_of(A)
        cells = {k: v for k, v in fm.own_obs.items()
                 if k.startswith(f"{fam}|BUY|")}
        self.assertTrue(cells, fm.own_obs)
        rested = sum(v[0] for v in cells.values())
        self.assertAlmostEqual(rested, 120.0, 3)   # two 60s cycles since
        self.assertEqual(sum(v[1] for v in cells.values()), 0.0)
        # the ask rests too, so age exposure is at least the bid's
        self.assertGreaterEqual(sum(v[0] for v in fm.age_fit.values()), 120.0)
        self.assertEqual(bid.seen_ts, r.now)
        # a slow cycle or a restart counts in full, up to an hour
        r.cycle(advance=2 * 3600.0)
        self.assertAlmostEqual(sum(v[0] for v in cells.values()),
                               120.0 + 3600.0, 3)
        exp, since = r.fam.expected_fills_24h(r.now)
        self.assertGreater(exp.get("earn", 0.0), 0.0)
        self.assertLessEqual(since, r.now)
        # the fill: the order leaves the book, the position appears
        del r.exchange.live[bid.id]
        r.positions[A] = (bid.qty, bid.qty * bid.price)
        r.cycle()
        cell = fm.own_obs[fm._key(fam, "BUY", bid.ticks_last)]
        self.assertEqual(cell[1], 1.0)
        self.assertEqual(sum(v[1] for v in fm.age_fit.values()), 1.0)
        self.assertEqual(fm.age_fit[f"{fam}|1"][1], 1.0)   # two hours old: 1-6h

    def test_bond_and_exit_orders_are_not_evidence(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        fm = r.fam.fillmodel
        fm.own_obs.clear()
        fm.age_fit.clear()
        r.fam.exp_fills.clear()
        book = r.exchange.books[A]
        bid_px, ask_px = book.bids[0][0], book.asks[0][0]
        r.exchange.live["BND"] = {"id": "BND", "market": A, "side": "BUY",
                                  "price": bid_px, "size": 5.0,
                                  "intent": BUY_LONG}
        r.fam.orders["BND"] = FamilyOrder(
            id="BND", market=A, side="BUY", price=bid_px, qty=5.0,
            intent=BUY_LONG, placed_ts=r.now, purpose="bond")
        r.positions[A] = (5.0, 5.0 * bid_px)
        r.fam.inventory[A] = {"qty": 5.0, "cost": 5.0 * bid_px}
        r.exchange.live["X"] = {"id": "X", "market": A, "side": "SELL",
                                "price": ask_px, "size": 5.0,
                                "intent": SELL_LONG}
        r.fam.orders["X"] = FamilyOrder(
            id="X", market=A, side="SELL", price=ask_px, qty=5.0,
            intent=SELL_LONG, placed_ts=r.now, purpose="sell")
        r.cycle()
        r.cycle()
        r.cycle()
        self.assertEqual(fm.own_obs, {})
        self.assertEqual(fm.age_fit, {})
        exp, _since = r.fam.expected_fills_24h(r.now)
        self.assertNotIn("sell", exp)
        # the bond bid's odds are still banked, so the note can show
        # the program's fills beside them without scoring them
        self.assertIn("bond", exp)
        del r.exchange.live["BND"]
        r.positions[A] = (10.0, 10.0 * bid_px)
        r.cycle()
        self.assertEqual(fm.own_obs, {})           # the bond fill: not evidence

    def test_the_expectation_survives_a_restart_and_ages_out(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        r.cycle()          # stamps
        r.cycle()          # banks
        saved = r.fam.to_dict()
        self.assertTrue(saved["exp_fills"])
        r2 = Rig()
        r2.fam.restore(saved)
        self.assertEqual(r2.fam.exp_fills, r.fam.exp_fills)
        exp, _ = r2.fam.expected_fills_24h(r.now)
        self.assertGreater(exp.get("earn", 0.0), 0.0)
        exp_later, _ = r2.fam.expected_fills_24h(r.now + 3 * 86400.0)
        self.assertEqual(exp_later, {})

    def test_the_old_orders_carry_no_last_look(self):
        rec = FamilyOrder(**{k: v for k, v in {
            "id": "O", "market": A, "side": "BUY", "price": 0.4,
            "qty": 1.0, "intent": BUY_LONG, "placed_ts": 1.0,
            "purpose": "earn"}.items()
            if k in FamilyOrder.__dataclass_fields__})
        self.assertEqual(rec.ticks_last, 0)
        self.assertEqual(rec.seen_ts, 0.0)


if __name__ == "__main__":
    unittest.main()
