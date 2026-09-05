"""Graduation on the owner's approval (2026-09-05: "reset the graduated
markets for politics. They can accumulate and only graduate on my
approval"): the rule names candidates, his tap graduates one, the
proven pool is exactly what he approved."""

import os
import tempfile
import unittest

from v3.main import Monitor

A = "usgubewc-usgub-al-2026-11-03-rep"
B = "usgubewc-usgub-tn-2026-11-03-rep"
C = "usgubewc-usgub-ga-2026-11-03-rep"


class TestGraduation(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(self.dir.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        self.mon = Monitor()
        self.fam = self.mon.families["politics"]
        self.assertGreater(self.fam.cfg.proven_usd, 0)      # politics graduates
        for s in (A, B, C):
            self.fam.universe[s] = {"event_n": 1, "name": s}
        # the exchange's payouts: A and B met the bar, C did not
        self.fam.recent_paid = {A: (2.10, 5), B: (1.25, 3), C: (0.40, 6)}

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def test_the_rule_names_candidates_and_nothing_graduates_by_itself(self):
        self.mon._refresh_graduation(self.fam, 1000.0)
        self.assertEqual(set(self.fam.grad_candidates), {A, B})
        self.assertEqual(self.fam.proven, set())               # reset: nothing until he says
        self.assertEqual(self.fam.grad_candidates[A]["avg"], 2.1)
        self.assertEqual(self.fam.grad_candidates[A]["days"], 5)
        since = self.fam.grad_candidates[A]["since"]
        self.mon._refresh_graduation(self.fam, 5000.0)          # accumulates, keeps its date
        self.assertEqual(self.fam.grad_candidates[A]["since"], since)

    def test_his_tap_graduates_and_removes(self):
        self.mon._refresh_graduation(self.fam, 1000.0)
        r = self.mon.graduate("politics", A, True)
        self.assertTrue(r["ok"], r["note"])
        self.assertEqual(self.fam.proven, {A})
        self.assertNotIn(A, self.fam.grad_candidates)
        self.assertEqual(self.mon.last_state["fam_politics"]["graduated"], [A])   # persisted
        self.mon._refresh_graduation(self.fam, 2000.0)          # the cycle keeps his choice
        self.assertEqual(self.fam.proven, {A})
        self.assertEqual(set(self.fam.grad_candidates), {B})
        r = self.mon.graduate("politics", A, False)
        self.assertTrue(r["ok"])
        self.assertEqual(self.fam.proven, set())
        self.mon._refresh_graduation(self.fam, 3000.0)
        self.assertEqual(set(self.fam.grad_candidates), {A, B})  # a candidate again
        self.assertFalse(self.mon.graduate("politics", "not-a-market", True)["ok"])
        self.assertFalse(self.mon.graduate("golf", A, True)["ok"])
        self.assertTrue(self.mon.graduate("politics", C, True)["ok"])   # his call, bar or not
        self.assertIn(C, self.fam.proven)

    def test_the_switch_card_shows_both_lists(self):
        self.mon._refresh_graduation(self.fam, 1000.0)
        self.mon.graduate("politics", B, True)
        s = self.mon._family_switch_state("politics")
        g = s["graduation"]
        self.assertEqual([x["market"] for x in g["graduated"]], [B])
        self.assertEqual(g["graduated"][0]["days"], 3)
        self.assertEqual([x["market"] for x in g["candidates"]], [A])
        self.assertEqual((g["bar_usd"], g["days"], g["pool_usd"]), (1.0, 3, 150.0))

    def test_a_restart_keeps_what_he_approved(self):
        self.mon._refresh_graduation(self.fam, 1000.0)
        self.mon.graduate("politics", A, True)
        d = self.fam.to_dict()
        from v3.family import Family
        fam2 = Family.__new__(Family)
        fam2.__dict__.update(self.fam.__dict__)      # same config and plumbing
        fam2.graduated = set()
        fam2.proven = set()
        fam2.restore(d)
        self.assertEqual(fam2.graduated, {A})
        self.assertEqual(fam2.proven, {A})


if __name__ == "__main__":
    unittest.main()
