"""A move must be worth something (owner, 2026-09-24 "Yes, ship it").

The book log of 2026-09-23 showed the covers flipping on other traders'
size wobbling: Pennsylvania-01 rep 57c<->60c as someone's ~5,500 at 61c
crossed the 10,000 target (each move worth $0.002 a day), Florida
governor dem 21c<->27c as ~4,700 came and went at 26c ($0.06-0.27 a
day). An order now moves to a new price only when the new slot beats
where it rests by FOCUS_MOVE_MIN_GAIN a day; one past his fair with no
company still moves at once. And a ghost is not netted at a price where
our own order rests again (Pennsylvania-01 at 23:09Z: 6 shares came off
a level where only our 3 rested). The launcher gives 3.0 STOP_WAIT_S to
save and exit on a stop, and the save waits 2 s for each lock.
"""
import unittest
from unittest import mock

import launcher
from v3 import focus as focus_mod
from v3 import main as main_mod
from v3.scoring import Book
from v3.tests.test_focus import NC, Base


class TestAMoveMustBeWorthSomething(Base):
    def resting(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        bids = self.mine(NC, "BUY")
        self.assertEqual(len(bids), 1)
        return bids[0]

    def forced(self, o, cur_ev, plan_ev, bare=False):
        """One pass with the plan pinned a tick behind the resting bid,
        at the same size, worth `plan_ev`, while the resting bid reads
        `cur_ev` by its own numbers."""
        real = self.f._plan_all

        def plan_all(now, positions, bp):
            real(now, positions, bp)
            p = dict(self.f.rows[NC]["tend"]["BUY"])
            p.update(px=round(o.price - 0.01, 2), qty=o.qty, ev=plan_ev)
            self.f.rows[NC]["tend"]["BUY"] = p
        with mock.patch.object(self.f, "_plan_all", side_effect=plan_all), \
                mock.patch.object(self.f, "_own_ev", return_value=cur_ev), \
                mock.patch.object(self.f, "_own_bare", return_value=bare):
            self.tick(advance=focus_mod.FOCUS_MOVE_COOLDOWN_S + 1.0)
        return self.mine(NC, "BUY")

    def test_a_gain_under_the_minimum_moves_nothing(self):
        o = self.resting()
        # FOCUS_KEEP alone would move it: 0.00 is under 80% of 0.10
        after = self.forced(o, 0.0, focus_mod.FOCUS_MOVE_MIN_GAIN - 0.15)
        self.assertEqual([b.id for b in after], [o.id])
        self.assertAlmostEqual(after[0].price, o.price)
        self.assertFalse([e for e in self.f.log if e.get("event") == "moved"])

    def test_a_gain_over_the_minimum_still_moves(self):
        o = self.resting()
        after = self.forced(o, 0.0, focus_mod.FOCUS_MOVE_MIN_GAIN + 0.25)
        self.assertEqual(len(after), 1)
        self.assertAlmostEqual(after[0].price, round(o.price - 0.01, 2))
        mv = [e for e in self.f.log if e.get("event") == "moved"]
        self.assertTrue(mv)
        self.assertIn("behind it", mv[-1]["why"])

    def test_past_his_fair_with_no_company_still_moves_at_once(self):
        o = self.resting()
        after = self.forced(o, 0.0, 0.05, bare=True)
        self.assertEqual(len(after), 1)
        self.assertAlmostEqual(after[0].price, round(o.price - 0.01, 2))
        mv = [e for e in self.f.log if e.get("event") == "moved"]
        self.assertEqual(mv[-1]["why"], "past your fair with no company")


class TestTheGhostWhereOurOrderRestsAgain(Base):
    def test_no_ghost_is_netted_at_the_price_our_order_rests_at(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        o = self.mine(NC, "BUY")[0]
        # the order it replaced at this same price, cancelled a minute ago
        self.f.departed[f"{NC}|BUY"] = [[o.price, o.qty, self.r.now - 60.0]]
        book = Book(bids=((o.price, 500.0 + o.qty), (0.02, 60000.0)),
                    asks=((0.47, 300.0), (0.98, 60000.0)),
                    tick=0.01, fetched_at=float(o.placed_ts) + 1.0)
        lv = dict(self.f._levels_net(NC, "BUY", book))
        self.assertAlmostEqual(lv[o.price], 500.0)          # ours off, the others' left whole

    def test_a_ghost_elsewhere_is_still_netted(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        o = self.mine(NC, "BUY")[0]
        away = round(o.price - 0.01, 2)
        self.f.departed[f"{NC}|BUY"] = [[away, 40.0, self.r.now - 60.0]]
        book = Book(bids=((o.price, 500.0 + o.qty), (away, 540.0), (0.02, 60000.0)),
                    asks=((0.47, 300.0), (0.98, 60000.0)),
                    tick=0.01, fetched_at=float(o.placed_ts) + 1.0)
        lv = dict(self.f._levels_net(NC, "BUY", book))
        self.assertAlmostEqual(lv[away], 500.0)


class TestTheStopGetsTimeToSave(unittest.TestCase):
    def test_the_save_waits_two_seconds_a_lock(self):
        self.assertEqual(main_mod.SHUTDOWN_LOCK_S, 2.0)

    def test_the_launcher_waits_its_stop_window_before_a_kill(self):
        self.assertEqual(launcher.STOP_WAIT_S, 45.0)
        waits = []

        class Proc:
            returncode = None

            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                waits.append(timeout)
                return 0

            def kill(self):
                raise AssertionError("killed inside the window")

        handlers = {}

        def sig(signum, fn):
            handlers[signum] = fn

        def sleep(_s):
            handlers[launcher.signal.SIGTERM](launcher.signal.SIGTERM, None)

        with mock.patch.object(launcher, "children", return_value={"3.0": ["x"]}), \
                mock.patch.object(launcher.subprocess, "Popen", return_value=Proc()), \
                mock.patch.object(launcher.signal, "signal", side_effect=sig), \
                mock.patch.object(launcher.time, "sleep", side_effect=sleep):
            self.assertEqual(launcher.main(), 0)
        self.assertEqual(len(waits), 1)
        self.assertGreater(waits[0], 40.0)
        self.assertLessEqual(waits[0], launcher.STOP_WAIT_S)


if __name__ == "__main__":
    unittest.main()
