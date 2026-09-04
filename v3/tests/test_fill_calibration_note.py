"""The hourly fill-calibration note (owner, 2026-09-04): per order type,
over the hours the model watched, bond fills and exits shown but never
scored, and no drift call until it has watched six hours."""

import unittest

from v3.main import Monitor
from v3.tests.test_family import Rig


def _seed(fam, now, exp_earn, exp_manual, fills, hours=24):
    fam.exp_fills.clear()
    per = max(hours, 1)
    for h in range(hours):
        key = str(int((now - h * 3600.0) // 3600) * 3600)
        fam.exp_fills[key] = {"earn": exp_earn / per, "manual": exp_manual / per}
    fam.fills = [{"ts": now - 100.0 - i, "purpose": p, "market": "m",
                  "side": "BUY", "qty": 1.0, "px": 0.5}
                 for i, p in enumerate(fills)]


class TestTheNote(unittest.TestCase):
    def test_scored_per_type_bonds_and_exits_shown_not_scored(self):
        r = Rig()
        now = r.now
        _seed(r.fam, now, 12.0, 30.0,
              ["earn"] * 30 + ["manual"] * 8 + ["hand"] * 5
              + ["bond"] * 66 + ["sell"] * 24 + ["backfill"] * 39)
        line = Monitor.fill_calibration_line("politics", r.fam, now)
        self.assertIn("engine orders expected 12.0 fills over the last 24h, "
                      "got 30  <-- DRIFTING", line)
        # over-prediction drifts too: 30 expected > 2 * 13 + 2
        self.assertIn("your hand orders expected 30.0, got 13  <-- DRIFTING",
                      line)
        self.assertIn("bond program 66 (directed by you, not scored)", line)
        self.assertIn("exits 24 (the unwinding working, not scored)", line)
        self.assertIn("39 found only in the record (not scored)", line)
        self.assertEqual(line.count("DRIFTING"), 2)

    def test_the_drift_line_is_two_x_plus_two(self):
        r = Rig()
        _seed(r.fam, r.now, 30.0, 0.0, ["earn"] * 14)   # 30 = 2*14 + 2 exactly
        self.assertNotIn("DRIFTING",
                         Monitor.fill_calibration_line("politics", r.fam, r.now))

    def test_in_range_is_quiet(self):
        r = Rig()
        _seed(r.fam, r.now, 25.0, 0.0, ["earn"] * 30)
        line = Monitor.fill_calibration_line("politics", r.fam, r.now)
        self.assertIn("expected 25.0 fills over the last 24h, got 30", line)
        self.assertNotIn("DRIFTING", line)
        self.assertNotIn("hand orders", line)

    def test_a_fresh_boot_grades_only_what_it_watched(self):
        r = Rig()
        now = r.now
        _seed(r.fam, now, 2.0, 0.0, [], hours=2)
        # a fill from before the watch began does not count against it
        r.fam.fills.append({"ts": now - 5 * 3600.0, "purpose": "earn"})
        r.fam.fills.append({"ts": now - 600.0, "purpose": "earn"})
        line = Monitor.fill_calibration_line("politics", r.fam, now)
        self.assertRegex(line, r"over the last [12]h, got 1")
        self.assertNotIn("DRIFTING", line)     # under six hours watched

    def test_nothing_to_say_is_none(self):
        r = Rig()
        r.fam.exp_fills.clear()
        r.fam.fills = []
        self.assertIsNone(Monitor.fill_calibration_line("politics", r.fam, r.now))


if __name__ == "__main__":
    unittest.main()
