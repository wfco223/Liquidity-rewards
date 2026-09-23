"""The stop signal saves the state before the process dies (owner,
2026-09-22 "Yes, ship it", fix D). Every deploy had lost the orders
the tender placed since the last cycle's save; the next boot found
them on the open list and recorded them as the owner's hand orders,
and on the seat books the tender rested its own beside them."""

import json
import os
import signal
import tempfile
import time
import unittest

from v3.family import FamilyOrder
from v3.intents import BUY_LONG
from v3.main import Monitor

AL = "usgubewc-usgub-al-2026-11-03-rep"


class TestShutdownSave(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "s.json")
        os.environ["V3_STATE_PATH"] = self.path
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        self.mon = Monitor()
        self.fam = self.mon.families["politics"]
        self.fam.universe[AL] = {"event_n": 1, "name": AL}

    def tearDown(self):
        from v3.orders import OrderDesk
        OrderDesk.halted = None        # a class-wide flag: never leak it
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def _rest(self, oid="T1"):
        self.fam.orders[oid] = FamilyOrder(
            id=oid, market=AL, side="BUY", price=0.44, qty=100.0, intent=BUY_LONG,
            placed_ts=time.time(), purpose="focus")

    def test_nothing_restored_writes_nothing(self):
        self.mon.last_state = None
        self.mon.restored_state = {}
        self._rest()
        self.assertFalse(self.mon.shutdown_save("test"))
        self.assertFalse(os.path.exists(self.path))

    def test_the_live_orders_reach_the_file(self):
        self.mon.last_state = {"saved_at": 1.0, "build": "x", "boots": [],
                               "fam_politics": {}, "sw_politics": {}, "grades": [1, 2]}
        self._rest()
        self.assertTrue(self.mon.shutdown_save("test"))
        with open(self.path) as f:
            st = json.load(f)
        self.assertIn("T1", st["fam_politics"]["orders"])
        self.assertGreater(st["saved_at"], 1.0)
        self.assertEqual(st["build"], "x")               # the base state kept
        self.assertEqual(st["grades"], [1, 2])
        for k in ("focus", "bonds", "sweep", "maint", "desk_cancelled",
                  "master_switch", "sw_bonds", "sw_focus", "audit"):
            self.assertIn(k, st, k)

    def test_the_handler_saves_and_exits(self):
        self.mon.last_state = {"saved_at": 1.0, "build": "x", "boots": [],
                               "fam_politics": {}}
        self._rest("T2")
        handler = self.mon._install_stop_handlers()
        self.assertIs(signal.getsignal(signal.SIGTERM), handler)
        with self.assertRaises(SystemExit):
            handler(signal.SIGTERM, None)
        with open(self.path) as f:
            st = json.load(f)
        self.assertIn("T2", st["fam_politics"]["orders"])


    # -- fix E (owner, 2026-09-22 "Yes, ship it") -----------------------

    def test_after_the_stop_nothing_places_or_cancels(self):
        from v3.orders import OrderDesk
        self.mon.last_state = {"saved_at": 1.0, "build": "x", "boots": [],
                               "fam_politics": {}}
        self.mon.shutdown_save("test")
        self.assertTrue(OrderDesk.halted)
        desk = self.fam.desk
        r = desk.place_resting(AL, "BUY", 0.44, 10.0)
        self.assertFalse(r.ok)
        self.assertIn("stopping", r.note)
        c = desk.cancel("ANY", AL)
        self.assertFalse(c.ok)
        self.assertIn("stopping", c.note)

    def test_the_stop_save_carries_its_own_record(self):
        self.mon.last_state = {"saved_at": 1.0, "build": "x", "boots": [],
                               "fam_politics": {}}
        self.mon.shutdown_save("signal 15")
        with open(self.path) as f:
            st = json.load(f)
        ls = st["last_stop"]
        self.assertAlmostEqual(ls["at"], st["saved_at"], places=0)
        self.assertEqual(ls["why"], "signal 15")
        self.assertTrue(ls["cycle_lock"])

    def _boot_from(self, st):
        from v3.orders import OrderDesk
        OrderDesk.halted = None
        with open(self.path, "w") as f:
            json.dump(st, f)
        return Monitor()

    def test_the_boot_says_it_came_back_from_the_stop_save(self):
        now = time.time() - 30.0
        m = self._boot_from({"saved_at": now, "build": "x", "boots": [],
                             "last_stop": {"at": round(now, 1), "why": "signal 15"}})
        self.assertEqual(m.boot_restore["from"], "the stop save")

    def test_the_boot_says_when_the_stop_save_did_not_land(self):
        m = self._boot_from({"saved_at": time.time() - 600.0, "build": "x", "boots": []})
        self.assertIn("did not land", m.boot_restore["from"])
        st = m._state(time.time(), {})
        self.assertIn("did not land", st["boot_restore"]["from"])


if __name__ == "__main__":
    unittest.main()
