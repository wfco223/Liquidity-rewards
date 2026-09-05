"""The rewards check's progress bar (owner, 2026-09-05): of the markets
we estimated earned something on a day, how many has the exchange
posted a row for."""

import os
import tempfile
import unittest

from v3.estimator import et_day
from v3.main import Monitor


class TestPostingProgress(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(self.dir.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        self.mon = Monitor()

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def test_the_bar_counts_appearances_against_estimates(self):
        now = 1_788_600_000.0
        today, yday = et_day(now), et_day(now - 86400.0)
        self.mon.mkt_claim_day = {
            f"{today}|m1": 1.2, f"{today}|m2": 0.4, f"{today}|m3": 0.0,   # m3: nothing accrued
            f"{today}|m4": 2.0,
            f"{yday}|m1": 0.9, f"{yday}|m5": 0.3,
        }
        agg = {
            f"{today}|m1": {"date": today, "market": "m1", "usd": 1.0, "paid": 0.0,
                            "status": {"PENDING"}},
            f"{today}|m2": {"date": today, "market": "m2", "usd": 0.5, "paid": 0.5,
                            "status": {"PAID"}},
            f"{today}|m9": {"date": today, "market": "m9", "usd": 0.1, "paid": 0.1,
                            "status": {"PAID"}},                       # not estimated
            f"{yday}|m1": {"date": yday, "market": "m1", "usd": 0.8, "paid": 0.8,
                           "status": {"PAID"}},
            f"{yday}|m5": {"date": yday, "market": "m5", "usd": 0.2, "paid": 0.2,
                           "status": {"PAID", "SKIPPED"}},
        }
        p = self.mon._posting_progress(agg, now)
        self.assertEqual([x["day"] for x in p], [today, yday])
        t = p[0]
        self.assertEqual((t["expected"], t["appeared"], t["pct"]), (3, 2, 67))
        self.assertEqual((t["pending"], t["paid"], t["extra"]), (1, 1, 1))
        y = p[1]
        self.assertEqual((y["expected"], y["appeared"], y["pct"]), (2, 2, 100))
        self.assertEqual((y["pending"], y["paid"], y["extra"]), (0, 2, 0))

    def test_nothing_estimated_and_nothing_posted_is_no_bar(self):
        self.mon.mkt_claim_day = {}
        self.assertEqual(self.mon._posting_progress({}, 1_788_600_000.0), [])

    def test_posted_but_nothing_estimated_has_no_percentage(self):
        now = 1_788_600_000.0
        today = et_day(now)
        self.mon.mkt_claim_day = {}
        agg = {f"{today}|m9": {"date": today, "market": "m9", "usd": 0.1,
                               "paid": 0.1, "status": {"PAID"}}}
        p = self.mon._posting_progress(agg, now)
        self.assertEqual(len(p), 1)
        self.assertIsNone(p[0]["pct"])
        self.assertEqual(p[0]["extra"], 1)


if __name__ == "__main__":
    unittest.main()
