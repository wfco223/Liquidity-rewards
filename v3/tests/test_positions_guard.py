"""A position READ is not the position feed (2026-09-04 04:20 ET: a
read came back missing 114 of 171 held positions, showed them again a
minute later, and the bond ledger wrote every holding off in between).

The guard re-reads a short list once, keeps missing markets at their
last value, and lets a market go only after it has been missing on
POS_GONE_READS reads spanning POS_GONE_S."""

import types
import unittest

from v3.api import ApiError
from v3.main import Monitor

AL = "usgubewc-usgub-al-2026-11-03-rep"
TN = "usgubewc-usgub-tn-2026-11-03-rep"
GA = "usgubewc-usgub-ga-2026-11-03-rep"


class FakeClient:
    def __init__(self):
        self.reads: list = []          # what each positions_net() call returns
        self.positions_read = {"pages": 1, "n": 0, "eof": True}

    def positions_net(self):
        if not self.reads:
            raise ApiError("no read queued")
        return self.reads.pop(0)


class TestTheGuard(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.notes = []
        self.stub = types.SimpleNamespace(
            POS_GONE_READS=Monitor.POS_GONE_READS, POS_GONE_S=Monitor.POS_GONE_S,
            client=self.client, _note=self.notes.append)
        self.full = {AL: (100.0, 98.0), TN: (-50.0, -47.5), GA: (5.0, 3.0)}
        self.guard = lambda fresh, now: Monitor._guard_positions(self.stub, fresh, now)

    def test_a_full_read_is_taken_as_is(self):
        self.assertEqual(self.guard(self.full, 0.0), self.full)
        self.assertEqual(self.notes, [])

    def test_a_short_read_is_read_again_and_the_fuller_read_wins(self):
        self.guard(self.full, 0.0)
        self.client.reads.append(self.full)                 # the re-read is whole
        got = self.guard({GA: (5.0, 3.0)}, 60.0)
        self.assertEqual(got, self.full)
        self.assertEqual(self.notes, [])                     # nothing was missing after all

    def test_markets_still_missing_keep_their_last_value(self):
        self.guard(self.full, 0.0)
        self.client.reads.append({GA: (5.0, 3.0)})           # the re-read is short too
        got = self.guard({GA: (5.0, 3.0)}, 60.0)
        self.assertEqual(got, self.full)
        self.assertEqual(len(self.notes), 1)
        self.assertIn("2 of 3 held markets missing", self.notes[0])
        self.assertIn("2 kept at their last value", self.notes[0])

    def test_an_empty_read_keeps_everything(self):
        self.guard(self.full, 0.0)
        self.client.reads.append({})
        self.assertEqual(self.guard({}, 60.0), self.full)

    def test_missing_three_reads_over_five_minutes_is_gone(self):
        self.guard(self.full, 0.0)
        short = {TN: (-50.0, -47.5), GA: (5.0, 3.0)}
        for t in (60.0, 120.0, 180.0):
            self.client.reads.append(short)
            self.assertEqual(self.guard(short, t)[AL], (100.0, 98.0))
        self.client.reads.append(short)
        got = self.guard(short, 400.0)                      # 4th read, 340s: gone
        self.assertNotIn(AL, got)
        self.assertIn("now taken as gone", self.notes[-1])
        self.assertIn(AL, self.notes[-1])
        # and a later full read simply brings it back
        self.assertEqual(self.guard(self.full, 460.0), self.full)

    def test_a_market_that_comes_back_resets_its_count(self):
        self.guard(self.full, 0.0)
        short = {TN: (-50.0, -47.5), GA: (5.0, 3.0)}
        for t in (60.0, 120.0):
            self.client.reads.append(short)
            self.guard(short, t)
        self.guard(self.full, 180.0)                         # back
        for t in (240.0, 300.0):
            self.client.reads.append(short)
            self.assertIn(AL, self.guard(short, t))          # the count starts over
        self.assertEqual(self.stub._pos_missing[AL][0], 2)

    def test_a_failed_re_read_still_keeps_the_last_values(self):
        self.guard(self.full, 0.0)
        got = self.guard({GA: (5.0, 3.0)}, 60.0)             # the re-read raises
        self.assertEqual(got, self.full)
        self.assertTrue(any("re-read failed" in n for n in self.notes))

    def test_a_position_that_really_changed_size_is_taken_at_once(self):
        self.guard(self.full, 0.0)
        smaller = dict(self.full)
        smaller[AL] = (40.0, 39.2)                            # 60 sold: named, just smaller
        self.assertEqual(self.guard(smaller, 60.0)[AL], (40.0, 39.2))
        self.assertEqual(self.notes, [])


if __name__ == "__main__":
    unittest.main()
