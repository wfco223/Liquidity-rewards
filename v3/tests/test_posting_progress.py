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
        self.mon.posting_last = {today: now - 600.0, yday: now - 600.0}
        p = self.mon._posting_progress(agg, now)
        self.assertEqual([x["day"] for x in p], [today])       # yesterday is fully posted: no bar
        t = p[0]
        self.assertEqual((t["expected"], t["appeared"], t["pct"]), (3, 2, 67))
        self.assertEqual((t["pending"], t["paid"], t["extra"]), (1, 1, 1))

    def test_only_days_actively_posting_get_a_bar(self):
        # owner, 2026-09-11: "Only show progress bars for days that are
        # actively being posted, where a new row has appeared" — today
        # has no row yet, four days back is fully posted: no bar for
        # either; three days back, one of three in: a bar
        now = 1_788_600_000.0
        today = et_day(now)
        d3, d4 = et_day(now - 3 * 86400.0), et_day(now - 4 * 86400.0)
        self.mon.mkt_claim_day = {
            f"{today}|m1": 1.0,
            f"{d3}|m1": 1.0, f"{d3}|m2": 1.0, f"{d3}|m3": 1.0,     # three days back: 1 of 3 posted
            f"{d4}|m1": 1.0, f"{d4}|m2": 1.0,                       # four days back: fully posted
        }
        agg = {
            f"{d3}|m1": {"date": d3, "market": "m1", "usd": 1.0, "paid": 1.0, "status": {"PAID"}},
            f"{d4}|m1": {"date": d4, "market": "m1", "usd": 1.0, "paid": 1.0, "status": {"PAID"}},
            f"{d4}|m2": {"date": d4, "market": "m2", "usd": 1.0, "paid": 1.0, "status": {"PAID"}},
        }
        self.mon.posting_last = {d3: now - 3600.0, d4: now - 60.0}
        p = self.mon._posting_progress(agg, now)
        self.assertEqual([x["day"] for x in p], [d3])
        self.assertEqual((p[0]["expected"], p[0]["appeared"], p[0]["pct"]), (3, 1, 33))
        # the same day with its last new row two days ago: the exchange
        # never posts every market we claimed, so time says it is done
        self.mon.posting_last = {d3: now - 2 * 86400.0}
        self.assertEqual(self.mon._posting_progress(agg, now), [])

    def test_a_new_row_marks_its_day_as_posting(self):
        now = 1_788_600_000.0
        d3 = et_day(now - 3 * 86400.0)
        self.mon.rewards_seen = {f"{d3}|m1": 1.0}
        agg = {f"{d3}|m1": {"date": d3, "market": "m1", "usd": 1.0, "paid": 1.0, "status": {"PAID"}},
               f"{d3}|m2": {"date": d3, "market": "m2", "usd": 0.5, "paid": 0.5, "status": {"PAID"}}}
        self.mon._mark_posting(agg, now)
        self.assertEqual(self.mon.posting_last, {d3: now})          # m2 is new
        self.mon.rewards_seen = {f"{d3}|m1": 1.0, f"{d3}|m2": 0.5}
        self.mon.posting_last = {}
        self.mon._mark_posting(agg, now + 60.0)
        self.assertEqual(self.mon.posting_last, {})                  # nothing new: no mark

    def test_nothing_estimated_and_nothing_posted_is_no_bar(self):
        self.mon.mkt_claim_day = {}
        self.assertEqual(self.mon._posting_progress({}, 1_788_600_000.0), [])

    def test_posted_but_nothing_estimated_has_no_bar(self):
        now = 1_788_600_000.0
        today = et_day(now)
        self.mon.mkt_claim_day = {}
        agg = {f"{today}|m9": {"date": today, "market": "m9", "usd": 0.1,
                               "paid": 0.1, "status": {"PAID"}}}
        self.assertEqual(self.mon._posting_progress(agg, now), [])   # nothing claimed: no bar


class TestTheRunningGrade(unittest.TestCase):
    """owner, 2026-09-11: "For the paid/estimated number can you only
    consider the rows for markets that have been posted already? So I
    get a sense as I'm going how high or low I'm running"."""

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

    def test_the_ratio_counts_only_the_markets_posted_so_far(self):
        day = "2026-09-10"
        # four markets estimated, two posted so far (one of them paid
        # under its claim), one posted that we never estimated
        self.mon.mkt_claim_day = {f"{day}|m1": 10.0, f"{day}|m2": 5.0, f"{day}|m3": 20.0,
                                  f"{day}|m4": 0.0}
        self.mon.paid_seen = {f"{day}|m1": 12.0, f"{day}|m2": 3.0, f"{day}|m9": 0.7}
        self.mon._rebuild_paid_by_day()
        self.mon.actuals_by_day = {day: 15.7}
        rows = {r["day"]: r for r in self.mon._grades()}
        g = rows[day]
        self.assertEqual(g["actual"], 15.7)                     # the whole day's postings
        self.assertEqual((g["posted_n"], g["est_n"]), (2, 3))    # m4 claimed nothing
        self.assertEqual((g["posted_paid"], g["posted_est"]), (15.0, 15.0))
        self.assertEqual(g["ratio_posted"], 1.0)                 # not 15.7 / 35
        self.assertEqual(g["extra_paid"], 0.7)

    def test_a_day_with_nothing_posted_carries_no_running_grade(self):
        day = "2026-09-10"
        self.mon.mkt_claim_day = {f"{day}|m1": 10.0}
        self.mon.paid_seen = {}
        self.mon._rebuild_paid_by_day()
        self.mon.actuals_by_day = {}
        rows = [r for r in self.mon._grades() if r["day"] == day]
        self.assertEqual(rows, [])                               # no estimate history, nothing posted
        self.mon.actuals_by_day = {"2026-09-09": 4.0}            # a posted day we never claimed
        g = [r for r in self.mon._grades() if r["day"] == "2026-09-09"][0]
        self.assertNotIn("ratio_posted", g)


if __name__ == "__main__":
    unittest.main()
