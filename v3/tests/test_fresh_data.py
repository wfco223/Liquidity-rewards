"""Owner, 2026-09-05: "I just want the program to have up to date data."
Three ways the data went stale or blind that evening, closed:
a short events feed wiped the market list (every estimate read $0.00);
an empty terms read counted as every program gone (the whole book
pulled); bond books were not re-read before acting."""

import unittest

from v3.tests.test_family import LIVE_PROG, A, B, Rig

C = "vmc-ussemov-ga-2026-11-03-d0-3"


class TestTheMarketListSurvivesAShortFeed(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        for s in (A, B, C):
            self.r.add_market(s)
        self.r.cycle()
        self.assertEqual(set(self.r.fam.universe), {A, B, C})

    def rediscover(self):
        self.r.fam.last_discover = 0.0
        self.r.cycle()

    def test_an_empty_feed_keeps_the_last_full_list(self):
        self.r.exchange.events = []                          # a 200 with nothing in it
        self.rediscover()
        self.assertEqual(set(self.r.fam.universe), {A, B, C})
        self.assertIn("discover_partial", [e["event"] for e in self.r.fam.log])
        self.assertIsNotNone(self.r.fam._side_pool(A, self.r.fam.terms.get(A)))
        # and it tries again soon, not in six hours
        self.assertLess(self.r.fam.last_discover, self.r.now - self.r.fam.cfg.discover_s + 1200)

    def test_a_short_feed_is_folded_in_not_swapped_in(self):
        self.r.exchange.events = [ev for ev in self.r.exchange.events
                                  if any(m["slug"] == A for m in ev["markets"])]
        self.rediscover()                                    # 1 of 3 came back: short
        self.assertEqual(set(self.r.fam.universe), {A, B, C})

    def test_a_full_feed_still_drops_a_market_that_closed(self):
        self.r.add_market("vmc-ussemov-ga-2026-11-03-d4-8")
        self.r.add_market("vmc-ussemov-ga-2026-11-03-r5-9")
        self.rediscover()
        self.assertEqual(len(self.r.fam.universe), 5)
        self.r.exchange.events = [ev for ev in self.r.exchange.events
                                  if not any(m["slug"] == C for m in ev["markets"])]
        self.rediscover()                                    # 4 of 5: a full feed, C closed
        self.assertNotIn(C, self.r.fam.universe)
        # the divisor it last had still serves a bond held there
        self.assertIsNotNone(self.r.fam._side_pool(C, self.r.fam.terms.get(A)))
        self.assertEqual(self.r.fam.event_n_seen[C], 1)


class TestAnEmptyTermsReadIsNoVerdict(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        for s in (A, B, C):
            self.r.add_market(s)
        self.r.cycle()
        self.assertTrue(self.r.exchange.live)
        self.assertTrue(all(self.r.fam.terms.get(s) is not None for s in (A, B, C)))

    def test_no_program_for_any_live_market_changes_nothing(self):
        n = len(self.r.exchange.live)
        self.r.exchange.prog_raw = {}                         # the read comes back empty
        self.r.cycle(advance=self.r.fam.cfg.terms_active_s + 1)
        self.assertEqual(len(self.r.exchange.live), n)        # nothing pulled
        self.assertEqual(self.r.fam.known_dead, set())
        self.assertIn("terms_suspect", [e["event"] for e in self.r.fam.log])
        self.assertTrue(all(self.r.fam.terms.get(s) is not None for s in (A, B, C)))

    def test_one_program_ending_while_the_others_read_live_is_still_gone(self):
        del self.r.exchange.prog_raw[C]
        self.r.cycle(advance=self.r.fam.cfg.terms_active_s + 1)
        self.assertIn(C, self.r.fam.known_dead)
        self.assertTrue(self.r.fam._dead_here(C))
        self.assertNotIn(A, self.r.fam.known_dead)
        self.assertIsNotNone(self.r.fam.terms.get(A))


if __name__ == "__main__":
    unittest.main()
