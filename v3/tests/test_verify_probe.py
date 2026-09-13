"""The meter's "the orders are still resting" clock does not wait for the
cycle.

Owner, 2026-09-13. The clock was stamped at the cycle's open-list read, and
the cycle is not a clock: that day's laps ran 6 to 15.6 minutes (bonds 239 s,
the families 432 s on the 16:12Z lap), so the stamp went past even the
widened ten-minute window and the meter billed nothing across a perfectly
healthy run — one 20-second tick in six read as an outage. The sampler now
reads the open list itself every two minutes, on the signed trade api rather
than the throttled gateway, and the window is the owner's own five minutes
again (2026-09-11). A probe that fails, or that answers "nothing resting"
while we hold records, does not stamp — a real outage still bills nothing.
"""

import os
import tempfile
import time
import unittest

from v3.main import Monitor, VERIFY_PROBE_S, VERIFY_PROBE_TIMEOUT_S


class FakeOpenList:
    """Stands in for client.open_orders_raw and records how it was called."""

    def __init__(self, rows=None, boom=False):
        self.rows, self.boom = rows if rows is not None else [{"id": "A"}], boom
        self.calls: list = []

    def __call__(self, *a, **kw):
        self.calls.append(kw)
        if self.boom:
            raise RuntimeError("read timed out")
        return self.rows


class TestTheVerifyProbe(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        p = self.dir.name
        os.environ["V3_STATE_PATH"] = os.path.join(p, "state.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(p, "floor.json")
        os.environ["GITHUB_TOKEN"] = ""
        self.mon = Monitor()

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def hold_a_record(self):
        fam = self.mon.families["politics"]
        fam.orders["oid"] = object()

    def test_a_stale_stamp_is_refreshed_by_the_probe(self):
        self.hold_a_record()
        fake = FakeOpenList()
        self.mon.client.open_orders_raw = fake
        now = time.time()
        self.mon._verified_at = now - 700.0       # a 11.7-minute lap
        self.assertTrue(self.mon._verify_probe(now))
        self.assertGreaterEqual(self.mon._verified_at, now - 1.0)
        # one try, a short timeout: the probe never waits out a retry ladder
        self.assertEqual(fake.calls, [{"tries": 1,
                                       "timeout": VERIFY_PROBE_TIMEOUT_S}])

    def test_no_stamp_at_all_probes_at_once(self):
        fake = FakeOpenList()
        self.mon.client.open_orders_raw = fake
        self.assertIsNone(getattr(self.mon, "_verified_at", None))
        self.assertTrue(self.mon._verify_probe(time.time()))
        self.assertEqual(len(fake.calls), 1)

    def test_a_fresh_stamp_is_left_alone(self):
        fake = FakeOpenList()
        self.mon.client.open_orders_raw = fake
        now = time.time()
        self.mon._verified_at = now - (VERIFY_PROBE_S - 10.0)
        self.assertFalse(self.mon._verify_probe(now))
        self.assertEqual(fake.calls, [])          # no read, no load

    def test_a_failed_probe_does_not_stamp(self):
        # the outage rule (owner, 2026-09-11): no word from the exchange,
        # nothing bills — the stamp must stay where it was
        self.hold_a_record()
        self.mon.client.open_orders_raw = FakeOpenList(boom=True)
        now = time.time()
        self.mon._verified_at = now - 700.0
        self.assertFalse(self.mon._verify_probe(now))
        self.assertAlmostEqual(self.mon._verified_at, now - 700.0, places=3)

    def test_nothing_resting_against_our_records_does_not_stamp(self):
        # the 2026-09-11 maintenance shape: every order cancelled while the
        # records stood and the books read fresh, and $111 was billed
        self.hold_a_record()
        self.mon.client.open_orders_raw = FakeOpenList(rows=[])
        now = time.time()
        self.mon._verified_at = now - 700.0
        self.assertFalse(self.mon._verify_probe(now))
        self.assertAlmostEqual(self.mon._verified_at, now - 700.0, places=3)

    def test_nothing_resting_and_nothing_on_the_books_is_in_reach(self):
        # a boot, or a flat account: an empty list IS the truth and the
        # exchange plainly answered, so the clock is honest
        self.mon.client.open_orders_raw = FakeOpenList(rows=[])
        now = time.time()
        self.mon._verified_at = now - 700.0
        self.assertTrue(self.mon._verify_probe(now))
        self.assertGreaterEqual(self.mon._verified_at, now - 1.0)

    def test_the_probe_never_touches_the_throttled_gateway(self):
        # every 429 of 2026-09-13 was a gateway book read; the open list is
        # the signed trade api, so the probe costs the books nothing
        import inspect
        from v3 import api
        src = inspect.getsource(api.Client.open_orders_raw)
        self.assertIn("TRADE_API", src)
        self.assertNotIn("GATEWAY", src)


if __name__ == "__main__":
    unittest.main()
