"""Tests for the alert discipline and state persistence."""

import base64
import gzip
import json
import os
import tempfile
import time
import unittest

from v3.alerts import Alerts
from v3.state import StateStore


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_alerts():
    sent = []
    clock = FakeClock()
    a = Alerts(topic="t", post=lambda title, msg, pri: sent.append((title, msg)),
               clock=clock)
    return a, sent, clock


class TestAlerts(unittest.TestCase):
    def test_duplicate_alert_held_inside_the_repeat_window(self):
        a, sent, clock = make_alerts()
        self.assertTrue(a.notify("Rate dropped", "now $100/day"))
        clock.t += 60
        self.assertFalse(a.notify("Rate dropped", "now $100/day"))
        clock.t += 1800
        self.assertTrue(a.notify("Rate dropped", "now $100/day"))
        self.assertEqual(len(sent), 2)

    def test_global_floor_holds_different_alerts_too(self):
        # A message carrying a changing number defeats dedupe — the floor
        # is what stops one push per minute.
        a, sent, clock = make_alerts()
        a.notify("Rate dropped", "now $100/day")
        clock.t += 60
        self.assertFalse(a.notify("Rate dropped", "now $99/day"))
        clock.t += 300
        self.assertTrue(a.notify("Rate dropped", "now $98/day"))

    def test_money_events_skip_the_floor(self):
        a, sent, clock = make_alerts()
        a.notify("Rate dropped", "now $100/day")
        clock.t += 10
        self.assertTrue(a.notify("Order filled", "scc-x BUY 10 @ 44c"))
        clock.t += 10
        self.assertTrue(a.notify("LP rewards paid", "$6.15 for 2026-08-16"))
        clock.t += 10
        self.assertTrue(a.notify("Reward pool changed", "scc-x $100 -> $50"))

    def test_held_alerts_are_named_on_the_next_push(self):
        a, sent, clock = make_alerts()
        a.notify("A", "1")
        clock.t += 10
        a.notify("B", "2")          # held: floor
        clock.t += 10
        a.notify("C", "3")          # held: floor
        clock.t += 300
        a.notify("D", "4")
        self.assertIn("held back: B, C", sent[-1][1])

    def test_every_decision_is_logged(self):
        a, sent, clock = make_alerts()
        a.notify("A", "1")
        clock.t += 10
        a.notify("A", "1")
        self.assertEqual([e["sent"] for e in a.log], [True, False])
        self.assertEqual(a.log[1]["why"], "same alert again")


class TestStateLocal(unittest.TestCase):
    def test_local_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            st = StateStore(os.path.join(d, "state.json"), token="")
            self.assertTrue(st.save_local({"saved_at": 5, "earned": 1.23}))
            self.assertEqual(st.load_local()["earned"], 1.23)

    def test_no_token_means_local_only_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            st = StateStore(os.path.join(d, "s.json"), token="")
            self.assertFalse(st.save_remote({"saved_at": 1}))
            self.assertIn("local only", st.last_error)
            self.assertIsNone(st.load_remote())


class FakeGh:
    """Routes GitHub API calls; records them; serves a stored state blob."""

    def __init__(self):
        self.calls = []
        self.stored = None
        self.ref_exists = True

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append((method, url))

        class R:
            status_code = 200
            text = ""
            content = b""

            def json(self):
                return self._j

        r = R()
        if "/git/blobs" in url:
            self.stored = base64.b64decode(json["content"])
            r._j = {"sha": "blobsha"}
        elif "/git/trees" in url:
            r._j = {"sha": "treesha"}
        elif "/git/commits" in url:
            r._j = {"sha": "commitsha"}
        elif "/git/refs/heads/" in url and method == "PATCH":
            if not self.ref_exists:
                r.status_code = 404
            r._j = {}
        elif url.endswith("/git/refs") and method == "POST":
            self.ref_exists = True
            r._j = {}
        elif "/contents/state.json" in url:
            if self.stored is None:
                r.status_code = 404
            r.content = self.stored or b""
            r._j = {}
        return r


class TestStateRemote(unittest.TestCase):
    def test_save_is_a_parentless_forced_commit_and_loads_back(self):
        with tempfile.TemporaryDirectory() as d:
            gh = FakeGh()
            st = StateStore(os.path.join(d, "s.json"), repo="o/r", token="tok",
                            session=gh)
            self.assertTrue(st.save_remote({"saved_at": 9, "earned": 2.5}))
            kinds = [u.split("o/r")[-1] for _, u in gh.calls]
            self.assertEqual(kinds, ["/git/blobs", "/git/trees", "/git/commits",
                                     "/git/refs/heads/v3-state"])
            self.assertEqual(json.loads(gzip.decompress(gh.stored))["earned"], 2.5)
            self.assertEqual(st.load_remote()["earned"], 2.5)

    def test_missing_branch_is_created(self):
        with tempfile.TemporaryDirectory() as d:
            gh = FakeGh()
            gh.ref_exists = False
            st = StateStore(os.path.join(d, "s.json"), repo="o/r", token="tok",
                            session=gh)
            self.assertTrue(st.save_remote({"saved_at": 1}))
            self.assertEqual(gh.calls[-1], ("POST", "https://api.github.com/repos/o/r/git/refs"))

    def test_boot_takes_the_newer_copy(self):
        with tempfile.TemporaryDirectory() as d:
            gh = FakeGh()
            st = StateStore(os.path.join(d, "s.json"), repo="o/r", token="tok",
                            session=gh)
            st.save_local({"saved_at": 100, "src": "local"})
            st.save_remote({"saved_at": 200, "src": "remote"})
            self.assertEqual(st.load_best()["src"], "remote")
            st.save_local({"saved_at": 300, "src": "local"})
            self.assertEqual(st.load_best()["src"], "local")

    def test_a_tap_s_save_returns_at_once_and_the_newest_snapshot_wins(self):
        # 2026-09-07: a tap's save of the whole state ran inside the web
        # request — long enough for the phone to read "unreachable" while
        # the sale had gone through. Now the snapshot is taken at once and
        # uploaded behind; two taps before the upload finishes: the newest
        # snapshot goes up, the older is dropped.
        import threading

        class GatedGh(FakeGh):
            def __init__(self):
                super().__init__()
                self.gate = threading.Event()
                self.blobs = 0

            def request(self, method, url, json=None, headers=None, timeout=None):
                if "/git/blobs" in url:
                    self.blobs += 1
                    if self.blobs == 1:
                        self.gate.wait(5.0)          # the first upload is slow
                return super().request(method, url, json=json, headers=headers, timeout=timeout)

        with tempfile.TemporaryDirectory() as d:
            gh = GatedGh()
            st = StateStore(os.path.join(d, "s.json"), repo="o/r", token="tok",
                            session=gh)
            t0 = time.time()
            self.assertTrue(st.save_remote_soon({"saved_at": 1}))
            self.assertLess(time.time() - t0, 1.0)                # back before the upload
            for _ in range(100):                                   # the worker is inside the slow call
                if gh.blobs == 1:
                    break
                time.sleep(0.01)
            self.assertEqual(gh.blobs, 1)
            st.save_remote_soon({"saved_at": 2})
            st.save_remote_soon({"saved_at": 3})
            gh.gate.set()
            self.assertTrue(st.wait_remote(5.0))
            self.assertEqual(gh.blobs, 2)                          # 1, then the newest: 3
            self.assertEqual(json.loads(gzip.decompress(gh.stored))["saved_at"], 3)
            self.assertEqual(st.dropped, 1)
            self.assertEqual(st.last_error, "")

    def test_a_tap_s_save_without_a_token_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            st = StateStore(os.path.join(d, "s.json"), token="")
            self.assertFalse(st.save_remote_soon({"saved_at": 1}))
            self.assertIn("GITHUB_TOKEN", st.last_error)

    def test_remote_save_throttles(self):
        with tempfile.TemporaryDirectory() as d:
            gh = FakeGh()
            clock = FakeClock()
            st = StateStore(os.path.join(d, "s.json"), repo="o/r", token="tok",
                            session=gh, clock=clock)
            self.assertTrue(st.maybe_save_remote({"saved_at": 1}))
            clock.t += 30
            self.assertFalse(st.maybe_save_remote({"saved_at": 2}))
            clock.t += 120
            self.assertTrue(st.maybe_save_remote({"saved_at": 3}))


if __name__ == "__main__":
    unittest.main()
