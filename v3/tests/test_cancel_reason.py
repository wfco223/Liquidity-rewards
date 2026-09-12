"""Owner, 2026-09-12: "Do the cancel reason read." An order that vanished
from the open list without our cancel is looked up in the exchange's
activity record, and the exchange's own state and cancel reason for it
are logged and counted (five batches of ~20 orders left the list at
once that day with nothing said about why)."""

import unittest

from v3.tests.test_family import A, Rig


def _vanish_one(r):
    """Rest an order, then have the exchange drop it without our cancel;
    cycle until the family books the silent cancel."""
    r.add_market(A)
    r.cycle()
    oid = next(iter(r.fam.orders))
    rec = r.fam.orders[oid]
    r.exchange.live.pop(oid, None)
    for _ in range(4):
        r.cycle(advance=120.0)
        if any(l.get("event") == "silent_cancel" and l.get("id") == oid for l in r.fam.log):
            break
    return oid, rec


class TestTheCancelReasonRead(unittest.TestCase):

    def test_a_vanished_order_gets_the_exchanges_own_reason(self):
        r = Rig()
        oid, rec = _vanish_one(r)
        self.assertIn(oid, r.fam.reason_queue)
        r.exchange.activity_rows = [{
            "type": "ACTIVITY_TYPE_ORDER",
            "order": {"id": oid, "marketSlug": A, "state": "ORDER_STATE_CANCELED",
                      "unsolicitedCancelReason": "UNSOLICITED_CXL_REASON_INSUFFICIENT_MARGIN",
                      "cumQuantity": 0},
        }]
        r.cycle(advance=120.0)
        self.assertNotIn(oid, r.fam.reason_queue)
        ev = [l for l in r.fam.log if l.get("event") == "cancel_reason" and l.get("id") == oid]
        self.assertEqual(len(ev), 1)
        self.assertIn("ORDER_STATE_CANCELED", ev[0]["note"])
        self.assertIn("INSUFFICIENT_MARGIN", ev[0]["note"])
        self.assertEqual(ev[0]["market"], A)
        self.assertEqual(r.fam.cancel_reasons.get("UNSOLICITED_CXL_REASON_INSUFFICIENT_MARGIN"), 1)
        self.assertTrue(any(l.get("event") == "activity_types" for l in r.fam.log))

    def test_an_unspecified_reason_reads_as_the_state_alone(self):
        r = Rig()
        oid, rec = _vanish_one(r)
        r.exchange.activity_rows = [{
            "type": "ACTIVITY_TYPE_TRADE",
            "trade": {"id": "t1", "aggressorExecution": {
                "id": "x1", "order": {"id": oid, "state": "ORDER_STATE_FILLED",
                                      "unsolicitedCancelReason": "UNSOLICITED_CXL_REASON_UNDEFINED",
                                      "cumQuantity": 12.5}}},
        }]
        r.cycle(advance=120.0)
        ev = [l for l in r.fam.log if l.get("event") == "cancel_reason" and l.get("id") == oid]
        self.assertEqual(len(ev), 1)
        self.assertIn("ORDER_STATE_FILLED", ev[0]["note"])
        self.assertIn("no reason field set", ev[0]["note"])
        self.assertIn("12.5 filled", ev[0]["note"])
        self.assertEqual(r.fam.cancel_reasons.get("ORDER_STATE_FILLED"), 1)

    def test_a_failing_record_read_is_logged_once_and_never_breaks_the_cycle(self):
        r = Rig()
        oid, rec = _vanish_one(r)
        r.exchange.activities_fail = True
        for _ in range(5):
            r.cycle(advance=120.0)
        fails = [l for l in r.fam.log if l.get("event") == "reason_read_failed"]
        self.assertEqual(len(fails), 1)
        self.assertIn(oid, r.fam.reason_queue)          # still waiting for an answer

    def test_an_order_the_record_never_shows_is_given_up_after_six_reads(self):
        r = Rig()
        oid, rec = _vanish_one(r)
        for _ in range(8):
            r.cycle(advance=120.0)
        self.assertNotIn(oid, r.fam.reason_queue)
        ev = [l for l in r.fam.log if l.get("event") == "cancel_reason" and l.get("id") == oid]
        self.assertEqual(len(ev), 1)
        self.assertIn("in neither the open list nor the newest", ev[0]["note"])
        self.assertEqual(r.fam.cancel_reasons.get("not in the record"), 1)

    def test_the_open_lists_own_row_answers_when_the_feed_cannot(self):
        # 2026-09-12: the activity feed carries ACTIVITY_TYPE_TRADE
        # alone, so an order cancelled without trading is never in it
        # and all 16 queued ids read "not in the record". The open list
        # keeps a finished order for a while with its state and reason
        # (owner, "Yes to those").
        r = Rig()
        oid, rec = _vanish_one(r)
        r.exchange.raw_rows = [{
            "id": oid, "marketSlug": A, "state": "ORDER_STATE_CANCELED",
            "unsolicitedCancelReason": "UNSOLICITED_CXL_REASON_INSUFFICIENT_MARGIN",
            "cumQuantity": 0,
        }]
        r.cycle(advance=120.0)
        self.assertNotIn(oid, r.fam.reason_queue)
        ev = [l for l in r.fam.log if l.get("event") == "cancel_reason" and l.get("id") == oid]
        self.assertEqual(len(ev), 1)
        self.assertIn("the open list lists it as ORDER_STATE_CANCELED", ev[0]["note"])
        self.assertIn("INSUFFICIENT_MARGIN", ev[0]["note"])
        self.assertEqual(r.fam.cancel_reasons.get("UNSOLICITED_CXL_REASON_INSUFFICIENT_MARGIN"), 1)

    def test_a_failing_open_list_read_never_breaks_the_cycle(self):
        r = Rig()
        oid, rec = _vanish_one(r)
        r.exchange.raw_fail = True
        for _ in range(3):
            r.cycle(advance=120.0)
        fails = [l for l in r.fam.log if l.get("event") == "reason_read_failed"]
        self.assertEqual(len(fails), 1)
        self.assertIn("open list", fails[0]["note"])
        self.assertIn(oid, r.fam.reason_queue)       # still waiting for an answer

    def test_the_queue_and_the_counts_survive_a_restart(self):
        r = Rig()
        oid, rec = _vanish_one(r)
        r.fam.cancel_reasons["UNSOLICITED_CXL_REASON_X"] = 3
        d = r.fam.to_dict()
        r2 = Rig()
        r2.fam.restore(d)
        self.assertIn(oid, r2.fam.reason_queue)
        self.assertEqual(r2.fam.cancel_reasons.get("UNSOLICITED_CXL_REASON_X"), 3)


if __name__ == "__main__":
    unittest.main()
