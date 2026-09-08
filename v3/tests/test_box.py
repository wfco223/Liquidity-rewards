"""The box under the app (2026-09-07): what the container reports and
how long the collector ran, read without ever raising."""

import gc
import unittest

from v3.box import GcClock, box_stats, freeze_heap


class TestTheBox(unittest.TestCase):
    def test_box_stats_never_raise_and_carry_the_load(self):
        b = box_stats()
        self.assertIsInstance(b, dict)
        self.assertIn("cpus", b)
        for k in ("mem_mb", "mem_max_mb", "throttled_s_total", "cpu_quota"):
            if k in b and b[k] is not None:
                self.assertGreaterEqual(float(b[k]), 0.0)

    def test_the_gc_clock_counts_collections_since_the_last_take(self):
        c = GcClock()
        c.hook()
        try:
            c.take()
            gc.collect()
            t = c.take()
            self.assertGreaterEqual(t["runs"], 1)
            self.assertGreaterEqual(t["gen2"], 1)
            self.assertGreaterEqual(t["s"], 0.0)
            self.assertEqual(c.take()["runs"], 0)
        finally:
            gc.callbacks.remove(c._cb)

    def test_deep_mb_weighs_a_container_and_shares_what_it_has_seen(self):
        from v3.box import deep_mb
        big = {i: [str(i)] * 3 for i in range(2000)}
        mb = deep_mb(big)
        self.assertGreater(mb, 0.1)
        seen = set()
        first = deep_mb(big, seen=seen)
        again = deep_mb(big, seen=seen)          # already counted: weighs nothing new
        self.assertGreater(first, 0.1)
        self.assertLess(again, 0.01)
        self.assertLess(deep_mb(big, budget=10), first)   # the budget bounds the walk

    def test_trimming_the_heap_reports_before_and_after_or_nothing(self):
        from v3.box import trim_heap
        t = trim_heap()
        if t is not None:
            self.assertGreaterEqual(t["before"], 0.0)
            self.assertGreaterEqual(t["after"], 0.0)

    def test_freezing_the_heap_reports_what_froze(self):
        n = freeze_heap()
        self.assertGreater(n, 0)
        gc.unfreeze()


if __name__ == "__main__":
    unittest.main()
