"""A cover's fill is booked (owner, 2026-09-24 "Master off, then fix covers").

A cover buys a short back: it is intent SELL_SHORT and rests on the BID,
so its fill RAISES the net position. Reconcile matched a vanished or
shrunken order to the position's move by `intent == BUY_LONG`, which
read every cover as a sale expecting the net to FALL — so no cover's
fill ever matched its delta. The order went to limbo, the ones the
exchange's trade list did not name were ruled silent cancels, and the
short came off the books only by the feed's "exchange wins" snap, with
no fill in the journal: nothing learned, no close on the card, the
tender's exit guard unconfirmed. Reconcile now takes the sign from the
book side, the same test _on_fill books by.
"""
import unittest

from v3.family import FamilyOrder, _fill_sign
from v3.intents import BUY_LONG, BUY_SHORT, SELL_LONG, SELL_SHORT, rest_side
from v3.tests.test_family import Rig

M = "ewc-usgub-ia-2026-11-03-dem"


def order(r, oid, intent, qty, price, purpose="sell"):
    rec = FamilyOrder(id=oid, market=M, side=rest_side(intent), price=price,
                      qty=qty, intent=intent, placed_ts=r.now - 600.0,
                      purpose=purpose, why="t")
    r.fam.orders[oid] = rec
    return rec


def held(r, qty, per):
    r.fam.inventory[M] = {"qty": qty, "cost": round(qty * per, 4)}
    r.fam.positions_seen[M] = qty


def booked(r, oid):
    return sum(float(x["qty"]) for x in r.fam.fills if x.get("oid") == oid)


class TestTheSignIsTheBookSide(unittest.TestCase):
    def test_every_intent(self):
        for intent, sign in ((BUY_LONG, 1.0), (SELL_SHORT, 1.0),
                             (BUY_SHORT, -1.0), (SELL_LONG, -1.0)):
            rec = FamilyOrder(id="x", market=M, side=rest_side(intent),
                              price=0.5, qty=1.0, intent=intent,
                              placed_ts=0.0, purpose="earn")
            self.assertEqual(_fill_sign(rec), sign, intent)


class TestACoverFillIsBooked(unittest.TestCase):
    def test_a_cover_gone_with_the_short_is_booked_at_once(self):
        r = Rig()
        held(r, -184.0, 0.81)
        order(r, "C1", SELL_SHORT, 184.0, 0.78)
        r.fam.reconcile([], {M: (0.0, 0.0)}, r.now)
        self.assertAlmostEqual(booked(r, "C1"), 184.0)
        self.assertNotIn("C1", r.fam.gone_pending)
        self.assertNotIn(M, r.fam.inventory)
        self.assertEqual(r.fam.silent_cancels, 0)
        fills = [e for e in r.fam.log if e.get("event") == "fill"]
        self.assertEqual([(e["side"], e["qty"]) for e in fills], [("BUY", 184.0)])

    def test_a_cover_that_filled_part_way_books_the_part(self):
        r = Rig()
        held(r, -184.0, 0.81)
        order(r, "C1", SELL_SHORT, 184.0, 0.78)
        live = [{"id": "C1", "market": M, "side": "BUY", "price": 0.78,
                 "size": 84.0}]
        r.fam.reconcile(live, {M: (-84.0, -84.0 * 0.81)}, r.now)
        self.assertAlmostEqual(booked(r, "C1"), 100.0)
        self.assertAlmostEqual(r.fam.orders["C1"].qty, 84.0)
        self.assertAlmostEqual(r.fam.inventory[M]["qty"], -84.0)
        self.assertFalse([e for e in r.fam.log
                          if e.get("event") == "size_shrunk_no_fill"])

    def test_a_cover_in_limbo_is_booked_when_the_feed_catches_up(self):
        r = Rig()
        held(r, -184.0, 0.81)
        order(r, "C1", SELL_SHORT, 184.0, 0.78)
        # the open list drops it before the feed moves
        r.fam.reconcile([], {M: (-184.0, -184.0 * 0.81)}, r.now)
        self.assertIn("C1", r.fam.gone_pending)
        self.assertAlmostEqual(booked(r, "C1"), 0.0)
        r.now += 60.0
        r.fam.reconcile([], {M: (0.0, 0.0)}, r.now)
        self.assertAlmostEqual(booked(r, "C1"), 184.0)
        self.assertNotIn("C1", r.fam.gone_pending)
        self.assertEqual(r.fam.silent_cancels, 0)

    def test_a_cover_is_not_booked_on_a_move_the_other_way(self):
        r = Rig()
        held(r, -184.0, 0.81)
        order(r, "C1", SELL_SHORT, 184.0, 0.78)
        # the short GREW: that is no fill of a bid
        r.fam.reconcile([], {M: (-250.0, -250.0 * 0.81)}, r.now)
        self.assertAlmostEqual(booked(r, "C1"), 0.0)
        self.assertIn("C1", r.fam.gone_pending)

    def test_a_cancelled_cover_is_still_a_silent_cancel(self):
        r = Rig()
        held(r, -184.0, 0.81)
        order(r, "C1", SELL_SHORT, 184.0, 0.78)
        r.fam.reconcile([], {M: (-184.0, -184.0 * 0.81)}, r.now)
        r.now += 400.0
        r.fam.reconcile([], {M: (-184.0, -184.0 * 0.81)}, r.now)
        self.assertAlmostEqual(booked(r, "C1"), 0.0)
        self.assertNotIn("C1", r.fam.gone_pending)
        self.assertEqual(r.fam.silent_cancels, 1)

    def test_the_close_is_learned_by_the_fill_model(self):
        r = Rig()
        held(r, -100.0, 0.20)
        order(r, "C1", SELL_SHORT, 100.0, 0.23)
        seen = []
        real = r.fam.fillmodel.observe_round_trip
        r.fam.fillmodel.observe_round_trip = (
            lambda m, loss, n: (seen.append((m, round(loss, 4), n)),
                                real(m, loss, n)))
        r.fam.reconcile([], {M: (0.0, 0.0)}, r.now)
        self.assertEqual(seen, [(M, 0.03, 100.0)])


class TestTheOtherIntentsAreUnchanged(unittest.TestCase):
    def test_a_bid_that_opened_a_long(self):
        r = Rig()
        order(r, "B1", BUY_LONG, 50.0, 0.40, purpose="earn")
        r.fam.reconcile([], {M: (50.0, 20.0)}, r.now)
        self.assertAlmostEqual(booked(r, "B1"), 50.0)
        self.assertAlmostEqual(r.fam.inventory[M]["qty"], 50.0)

    def test_a_sale_of_the_lot(self):
        r = Rig()
        held(r, 80.0, 0.60)
        order(r, "S1", SELL_LONG, 80.0, 0.62)
        r.fam.reconcile([], {M: (0.0, 0.0)}, r.now)
        self.assertAlmostEqual(booked(r, "S1"), 80.0)
        self.assertNotIn(M, r.fam.inventory)

    def test_an_ask_that_opened_a_short(self):
        r = Rig()
        order(r, "A1", BUY_SHORT, 30.0, 0.55, purpose="earn")
        live = [{"id": "A1", "market": M, "side": "SELL", "price": 0.55,
                 "size": 10.0}]
        r.fam.reconcile(live, {M: (-20.0, -11.0)}, r.now)
        self.assertAlmostEqual(booked(r, "A1"), 20.0)
        self.assertAlmostEqual(r.fam.inventory[M]["qty"], -20.0)

    def test_a_sale_is_not_booked_on_a_rise(self):
        r = Rig()
        held(r, 80.0, 0.60)
        order(r, "S1", SELL_LONG, 80.0, 0.62)
        r.fam.reconcile([], {M: (120.0, 72.0)}, r.now)
        self.assertAlmostEqual(booked(r, "S1"), 0.0)
        self.assertIn("S1", r.fam.gone_pending)


if __name__ == "__main__":
    unittest.main()
