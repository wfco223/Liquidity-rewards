"""The boot hold (owner, 2026-09-24 "Build a boot hold").

A push to the deploy branch starts a new container while the old one is
still running, and the old one is stopped only once the new one answers.
So the new copy read the state before the old copy's stop save could
exist: every deploy since the stop save was built came back from "a
periodic save" (09-24: the 17:12:53Z save, restored at 17:14:39Z, 90 s
after the merge), and for the overlap both copies could trade. A new
container that did not restore a stop save now serves its page, trades
nothing and refuses every tap, while it watches the state branch; the
old copy's stop save landing restarts it in place to restore that save.
"""
import os
import signal
import tempfile
import threading
import unittest
from unittest import mock

from v3 import main as main_mod
from v3.main import BOOT_HOLD_ENV, BOOT_HOLD_POLL_S, BOOT_HOLD_S, Monitor, is_stop_save
from v3.state import StateStore
from v3.web import WebServer

SV0 = 1_790_269_973.4          # the periodic save the 17:14:39Z boot restored


def periodic(at):
    return {"saved_at": at, "build": "92c9cee8"}


def stop_save(at):
    return {"saved_at": at, "build": "92c9cee8",
            "last_stop": {"at": round(at, 1), "why": "signal 15", "build": "92c9cee8"}}


class FakeStore:
    """The state branch as the hold sees it: `heads[i]` is the head at
    poll i (the last one repeats), `states` what each head holds."""

    def __init__(self, heads, states):
        self.token = "t"
        self.heads = list(heads)
        self.states = dict(states)
        self.polls = 0
        self.loads = 0
        self.saves = 0
        self.local_found = False
        self.last_source = "remote"

    def remote_head(self):
        h = self.heads[min(self.polls, len(self.heads) - 1)]
        self.polls += 1
        return h

    def load_remote(self):
        self.loads += 1
        return self.states.get(self.heads[min(self.polls - 1, len(self.heads) - 1)])

    def save_soon(self, *a, **k):
        self.saves += 1
        return True

    def wait_remote(self, timeout=0):
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "s.json")
        os.environ["V3_STATE_PATH"] = self.path
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        os.environ.pop(BOOT_HOLD_ENV, None)
        self.mon = Monitor()
        self.t = [SV0 + 110.0]
        self.mon._hold_clock = lambda: self.t[0]
        self.mon._hold_sleep = lambda s: self.t.__setitem__(0, self.t[0] + s)
        self.execs = []
        self.mon._exec = lambda path, argv, env: self.execs.append((path, argv, env))

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN", BOOT_HOLD_ENV):
            os.environ.pop(k, None)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self.dir.cleanup()

    def new_container(self, store):
        self.mon.store = store
        self.mon.boot_restore = {"from": "a periodic save — the stop save did not land",
                                 "saved_at": SV0, "age_s": 106.2, "stop": None,
                                 "source": "remote", "disk": False, "after_hold": None}


class TestWhenTheBootHolds(Base):
    def test_a_new_container_that_restored_a_periodic_save_holds(self):
        self.new_container(FakeStore(["h0"], {"h0": periodic(SV0)}))
        self.assertTrue(self.mon._boot_hold_due())

    def test_no_hold_after_the_stop_save(self):
        self.new_container(FakeStore(["h0"], {}))
        self.mon.boot_restore["from"] = "the stop save"
        self.assertFalse(self.mon._boot_hold_due())

    def test_no_hold_in_the_same_container(self):
        # the launcher restarted 3.0 after a crash: the disk has the
        # state and the old process is gone
        self.new_container(FakeStore(["h0"], {}))
        self.mon.boot_restore["disk"] = True
        self.assertFalse(self.mon._boot_hold_due())

    def test_no_hold_on_a_fresh_state_or_without_the_branch(self):
        st = FakeStore(["h0"], {})
        self.new_container(st)
        self.mon.boot_restore["saved_at"] = 0.0
        self.assertFalse(self.mon._boot_hold_due())
        self.new_container(st)
        st.token = ""
        self.assertFalse(self.mon._boot_hold_due())

    def test_one_hold_a_boot(self):
        self.new_container(FakeStore(["h0"], {}))
        os.environ[BOOT_HOLD_ENV] = "the old copy's stop save, after 40s"
        self.assertFalse(self.mon._boot_hold_due())
        # and the restarted boot says what it waited for
        self.mon.store = StateStore(self.path, token="")
        self.mon._restore()
        self.assertEqual(self.mon.boot_restore["after_hold"],
                         "the old copy's stop save, after 40s")


class TestTheHold(Base):
    def test_the_old_copys_stop_save_restarts_this_one_in_place(self):
        store = FakeStore(["h0", "h0", "h1", "h1", "h2"],
                          {"h0": periodic(SV0), "h1": periodic(SV0 + 60.0),
                           "h2": stop_save(SV0 + 75.0)})
        self.new_container(store)
        self.mon._boot_hold()
        self.assertEqual(len(self.execs), 1)
        path, argv, env = self.execs[0]
        self.assertEqual(argv[1:4], ["-u", "-m", "v3.main"])
        self.assertIn("stop save", env[BOOT_HOLD_ENV])
        self.assertLess(self.t[0] - (SV0 + 110.0), 60.0)
        self.assertEqual(store.saves, 0)                    # wrote nothing
        notes = " ".join(self.mon.errors)
        self.assertIn("still running", notes)

    def test_a_stop_save_already_on_the_branch_is_taken_at_once(self):
        # it landed between this boot's read and the hold's first look
        store = FakeStore(["h2"], {"h2": stop_save(SV0 + 50.0)})
        self.new_container(store)
        self.mon._boot_hold()
        self.assertEqual(len(self.execs), 1)
        self.assertEqual(store.polls, 1)

    def test_the_old_copys_last_save_is_taken_when_no_stop_save_comes(self):
        store = FakeStore(["h0", "h1"], {"h0": periodic(SV0), "h1": periodic(SV0 + 60.0)})
        self.new_container(store)
        self.mon._boot_hold()
        self.assertEqual(len(self.execs), 1)
        self.assertIn("last save", self.execs[0][2][BOOT_HOLD_ENV])
        self.assertGreaterEqual(self.t[0] - (SV0 + 110.0), BOOT_HOLD_S)

    def test_nothing_newer_goes_on_after_five_minutes(self):
        store = FakeStore(["h0"], {"h0": periodic(SV0)})
        self.new_container(store)
        self.mon._boot_hold()
        self.assertEqual(self.execs, [])
        self.assertIsNone(self.mon.boot_hold)
        waited = self.t[0] - (SV0 + 110.0)
        self.assertGreaterEqual(waited, BOOT_HOLD_S)
        self.assertLess(waited, BOOT_HOLD_S + BOOT_HOLD_POLL_S + 1)
        self.assertEqual(self.mon.boot_restore["hold"]["outcome"], "nothing newer — went on")
        self.assertEqual(store.loads, 1)           # the head never moved: one download

    def test_an_older_stop_save_is_not_the_one(self):
        store = FakeStore(["h0", "hx"], {"h0": periodic(SV0), "hx": stop_save(SV0 - 600.0)})
        self.new_container(store)
        self.mon._boot_hold()
        self.assertEqual(self.execs, [])

    def test_nothing_holds_where_there_is_no_reason(self):
        store = FakeStore(["h1"], {"h1": stop_save(SV0 + 50.0)})
        self.new_container(store)
        self.mon.boot_restore["disk"] = True
        self.mon._boot_hold()
        self.assertEqual(store.polls, 0)
        self.assertEqual(self.execs, [])

    def test_the_page_says_what_it_waits_for(self):
        seen = []
        store = FakeStore(["h0"], {"h0": periodic(SV0)})
        self.new_container(store)
        real = self.mon._hold_sleep
        self.mon._hold_sleep = lambda s: (seen.append((dict(self.mon.boot_stage),
                                                       dict(self.mon.boot_hold))), real(s))
        self.mon._boot_hold()
        stage, hold = seen[0]
        self.assertIn("old copy", stage["stage"])
        self.assertTrue(hold["until"] > hold["since"])
        self.assertNotIn("old copy", self.mon.boot_stage["stage"])   # cleared after


class TestATapDuringTheHold(Base):
    def test_it_does_nothing_and_says_so(self):
        web = WebServer(self.mon)
        before = self.mon.master.on
        self.mon.boot_hold = {"since": 0.0, "until": main_mod.time.time() + 120.0}
        out = web.handle_op({"op": "switch_off", "which": "master"})
        self.assertFalse(out["ok"])
        self.assertIn("Starting up", out["note"])
        self.assertIn("2 more min", out["note"])
        self.assertEqual(self.mon.master.on, before)
        self.mon.boot_hold = None
        out = web.handle_op({"op": "nope"})
        self.assertIn("unknown op", out["note"])


class Stop(BaseException):
    pass


class TestRunHoldsFirst(Base):
    def test_nothing_starts_before_the_hold_returns(self):
        order = []

        class Web:
            def __init__(self, mon):
                self.port = 0

            def start(self):
                order.append("web")

        class Thread:
            def __init__(self, target=None, daemon=None, name=None, args=()):
                self.name = name

            def start(self):
                order.append(self.name)

        class St:
            def start(self):
                order.append("stream")

        self.mon.streams = [St()]
        self.mon._boot_hold = lambda: order.append("hold")
        self.mon._install_stop_handlers = lambda: order.append("stop_handlers")

        def cycle():
            order.append("cycle")
            raise Stop()
        self.mon.cycle = cycle
        with mock.patch("v3.web.WebServer", Web), \
                mock.patch.object(main_mod.threading, "Thread", Thread):
            with self.assertRaises(Stop):
                self.mon.run()
        self.assertEqual(order[:2], ["web", "hold"])
        for later in ("sampler", "focus", "stop_handlers", "stream", "cycle"):
            self.assertGreater(order.index(later), order.index("hold"), later)


class TestTheStoreSaysWhereItsCopyCameFrom(unittest.TestCase):
    def test_disk_and_branch(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            st = StateStore(path, token="")
            self.assertIsNone(st.load_best())
            self.assertFalse(st.local_found)
            st.save_local({"saved_at": 5.0})
            self.assertEqual(st.load_best()["saved_at"], 5.0)
            self.assertTrue(st.local_found)
            self.assertEqual(st.last_source, "local")

    def test_the_branch_head_and_a_newer_branch_copy(self):
        import gzip
        import json

        class R:
            def __init__(self, code, body=None, content=b""):
                self.status_code = code
                self._b = body
                self.content = content
                self.text = ""

            def json(self):
                return self._b

        class Sess:
            def request(self, method, url, **k):
                if "/git/ref/heads/" in url:
                    return R(200, {"object": {"sha": "abc123"}})
                if "/contents/state.json" in url:
                    return R(200, content=gzip.compress(json.dumps(
                        {"saved_at": 9.0}).encode()))
                return R(404)

        with tempfile.TemporaryDirectory() as d:
            st = StateStore(os.path.join(d, "s.json"), token="t", session=Sess())
            self.assertEqual(st.remote_head(), "abc123")
            self.assertEqual(st.load_best()["saved_at"], 9.0)
            self.assertFalse(st.local_found)
            self.assertEqual(st.last_source, "remote")


class TestTheStopStamp(unittest.TestCase):
    def test_is_stop_save(self):
        self.assertTrue(is_stop_save(stop_save(100.0)))
        self.assertFalse(is_stop_save(periodic(100.0)))
        self.assertFalse(is_stop_save(None))
        late = stop_save(100.0)
        late["saved_at"] = 160.0          # a cycle saved over a stop's stamp
        self.assertFalse(is_stop_save(late))


if __name__ == "__main__":
    unittest.main()
