"""Monitor-level interlock: arming writes the floor request instantly, and
no desk will place until both acknowledgements are in."""

import json
import os
import tempfile
import unittest

from v3 import floor
from v3.main import Monitor


class TestMonitorFloor(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        p = self.dir.name
        os.environ["V3_STATE_PATH"] = os.path.join(p, "state.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(p, "floor.json")
        os.environ["V1_ACK_PATH"] = os.path.join(p, "a1.json")
        os.environ["V2_ACK_PATH"] = os.path.join(p, "a2.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"     # pure master semantics here
        os.environ["V1_ENABLED"] = "1"
        os.environ["V2_ENABLED"] = "1"
        self.mon = Monitor()

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V1_ACK_PATH",
                  "V2_ACK_PATH", "V3_FLATTEN", "V1_ENABLED", "V2_ENABLED"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def read_floor(self):
        with open(os.environ["V3_FLOOR_PATH"]) as f:
            return json.load(f)

    def test_arming_master_requests_the_floor_immediately(self):
        self.mon.switch_tap("arm", "master")
        self.mon.switch_tap("confirm", "master")
        self.assertTrue(self.mon.master.on)
        self.assertTrue(self.read_floor()["want"])
        self.mon.switch_tap("off", "master")
        self.assertFalse(self.read_floor()["want"])

    def test_desks_stay_shut_until_both_acks(self):
        self.mon.switch_tap("arm", "master")
        self.mon.switch_tap("confirm", "master")
        self.mon.switch_tap("arm", "politics")
        self.mon.switch_tap("confirm", "politics")
        pol = self.mon.families["politics"]
        self.assertFalse(pol.desk.switch_on())       # no acks yet
        floor.ack("v1", True)
        floor.ack("v2", True)
        self.mon._floor_ok = self.mon.floor.acked()  # what cycle() does
        self.assertTrue(pol.desk.switch_on())
        floor.ack("v2", False)                       # 2.0 came back
        self.mon._floor_ok = self.mon.floor.acked()
        self.assertFalse(pol.desk.switch_on())

    def test_every_family_is_present_and_off(self):
        self.assertEqual(set(self.mon.families),
                         {"politics", "cfb", "nfl", "nba", "gameday"})
        for sw in self.mon.switches.values():
            self.assertFalse(sw.on)
        self.assertFalse(self.mon.master.on)


if __name__ == "__main__":
    unittest.main()
