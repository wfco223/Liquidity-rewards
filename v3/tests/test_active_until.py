"""The owner's say over a family's game window (2026-09-04: "Cfb can go
active until 5:00 pm eastern today"): a clock time today, in ET, set
from the switch page; until then the family rests as in resting hours."""

import datetime as dt
import os
import tempfile
import time
import unittest

from v3.family import ET
from v3.main import Monitor


class TestTheClockTime(unittest.TestCase):
    def test_a_time_today_in_et(self):
        now = dt.datetime(2026, 9, 4, 8, 15, tzinfo=ET).timestamp()     # a Friday morning
        ts = Monitor._et_today("17:00", now)
        self.assertEqual(dt.datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d %H:%M"),
                         "2026-09-04 17:00")
        self.assertEqual(Monitor._et_today("5:07", now),
                         dt.datetime(2026, 9, 4, 5, 7, tzinfo=ET).timestamp())
        self.assertIsNone(Monitor._et_today("25:00", now))
        self.assertIsNone(Monitor._et_today("soon", now))
        self.assertIsNone(Monitor._et_today("", now))

    def test_a_time_tomorrow_or_on_a_date(self):
        # owner, 2026-09-05: "set the college football to be active
        # until a time tomorrow"
        now = dt.datetime(2026, 9, 5, 22, 30, tzinfo=ET).timestamp()     # Saturday night
        tomorrow = dt.datetime(2026, 9, 6, 17, 0, tzinfo=ET).timestamp()
        self.assertEqual(Monitor._et_at("17:00 tomorrow", now), tomorrow)
        self.assertEqual(Monitor._et_at("tomorrow 17:00", now), tomorrow)
        self.assertEqual(Monitor._et_at("2026-09-06 17:00", now), tomorrow)
        self.assertEqual(Monitor._et_at("17:00 today", now),
                         dt.datetime(2026, 9, 5, 17, 0, tzinfo=ET).timestamp())
        self.assertIsNone(Monitor._et_at("tomorrow", now))
        self.assertIsNone(Monitor._et_at("17:00 someday", now))
        self.assertIsNone(Monitor._et_at("2026-13-01 17:00", now))


class TestTheTap(unittest.TestCase):
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

    def test_set_and_clear(self):
        fam = self.mon.families["cfb"]
        # a time later today, whatever the hour the test runs at
        later = dt.datetime.fromtimestamp(time.time() + 3600.0, ET)
        hhmm = later.strftime("%H:%M")
        if later.date() != dt.datetime.now(ET).date():
            self.skipTest("too close to midnight ET for a same-day time")
        r = self.mon.set_active_until("cfb", hhmm)
        self.assertTrue(r["ok"], r["note"])
        self.assertGreater(fam.active_until, time.time())
        self.assertIn("stays active until", r["note"])
        sv = self.mon.public_state()["switch_view"]["cfb"]
        self.assertEqual(sv["active_until"], fam.active_until)
        self.assertTrue(sv["has_window"])
        self.assertEqual(self.mon.last_state["fam_cfb"]["active_until"], fam.active_until)
        r = self.mon.set_active_until("cfb", "")
        self.assertTrue(r["ok"])
        self.assertEqual(fam.active_until, 0.0)

    def test_tomorrow_from_the_switch_page(self):
        fam = self.mon.families["cfb"]
        r = self.mon.set_active_until("cfb", "17:00 tomorrow")
        self.assertTrue(r["ok"], r["note"])
        self.assertIn("5:00 PM ET tomorrow", r["note"])
        at = dt.datetime.fromtimestamp(fam.active_until, ET)
        self.assertEqual(at.date(), dt.datetime.now(ET).date() + dt.timedelta(days=1))
        self.assertEqual((at.hour, at.minute), (17, 0))
        self.assertGreater(fam.active_until, time.time())
        # a week and more out is refused: the window re-decides week to week
        far = (dt.datetime.now(ET).date() + dt.timedelta(days=9)).isoformat()
        self.assertFalse(self.mon.set_active_until("cfb", f"{far} 17:00")["ok"])

    def test_a_past_time_and_a_bad_family_are_refused(self):
        earlier = dt.datetime.fromtimestamp(time.time() - 3600.0, ET)
        if earlier.date() != dt.datetime.now(ET).date():
            self.skipTest("too close to midnight ET for a same-day time")
        r = self.mon.set_active_until("cfb", earlier.strftime("%H:%M"))
        self.assertFalse(r["ok"])
        self.assertIn("passed", r["note"])
        self.assertFalse(self.mon.set_active_until("golf", "17:00")["ok"])
        self.assertFalse(self.mon.set_active_until("cfb", "five")["ok"])


if __name__ == "__main__":
    unittest.main()
