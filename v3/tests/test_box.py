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

    def test_freezing_the_heap_reports_what_froze(self):
        n = freeze_heap()
        self.assertGreater(n, 0)
        gc.unfreeze()


if __name__ == "__main__":
    unittest.main()
