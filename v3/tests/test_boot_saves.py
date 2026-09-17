"""A save made before the first cycle builds on the restored state
(owner, 2026-09-17, "The meter is busted"): a focus-page tap during a
45-minute boot had saved a three-key fragment over the 72-key state on
GitHub, and a restart would have restored every switch off and the
bonds, journals and pay records empty."""
import inspect
import types
import unittest

from v3.main import Monitor


def _mon(last_state, restored):
    m = types.SimpleNamespace(last_state=last_state, restored_state=restored)
    m._base_state = types.MethodType(Monitor._base_state, m)
    return m


class TestASaveBeforeTheFirstCycle(unittest.TestCase):
    RESTORED = {"master_switch": {"on": True}, "bonds": {"budget": 1000.0},
                "fam_politics": {"orders": {}}, "saved_at": 1.0}

    def test_builds_on_the_restored_state(self):
        m = _mon(None, dict(self.RESTORED))
        st = m._base_state()
        st["focus"] = {"fairs": {}}
        st["saved_at"] = 2.0
        self.assertEqual(st["master_switch"], {"on": True})     # the switches survive
        self.assertEqual(st["bonds"], {"budget": 1000.0})       # so does the ledger
        self.assertEqual(m.restored_state["saved_at"], 1.0)     # a copy — the original is untouched

    def test_the_last_full_state_wins_once_there_is_one(self):
        m = _mon({"saved_at": 5.0, "bonds": {"budget": 2000.0}}, dict(self.RESTORED))
        self.assertEqual(m._base_state()["bonds"], {"budget": 2000.0})

    def test_nothing_restored_is_an_empty_base(self):
        self.assertEqual(_mon(None, {})._base_state(), {})
        self.assertEqual(_mon(None, None)._base_state(), {})

    def test_no_save_path_builds_on_nothing_any_more(self):
        src = inspect.getsource(Monitor)
        self.assertNotIn("dict(self.last_state) if self.last_state else {}", src)
        self.assertNotIn("if self.last_state else {\"saved_at\": 0}", src)
        self.assertGreaterEqual(src.count("self._base_state()"), 5)
