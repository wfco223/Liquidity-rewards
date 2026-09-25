"""Stage 1 of the tier engines: the tier fairs (owner, 2026-09-25).

Read-only: a fair for every midterm-tier market from the book at depth,
the last trade, the event's linked markets and Silver, graded against
the midpoint later. Nothing here touches an order or reads the exchange.
"""
import json
import os
import tempfile
import unittest

from v3.books import BookCache
from v3.programs import Program
from v3.scoring import Book
from v3.terms import TermsStore
from v3.tierfair import (BOOK_STALE_S, FAIR_DEPTH_MIN, GRADE_HORIZONS, SNAP_S,
                         WEIGHT_MIN_N, TierFair, _falling_fit, depth_price, tier_of)

T0 = 1_790_300_000.0
DEM = "ushrewc-ushr-tx-34-2026-11-03-dem"
REP = "ushrewc-ushr-tx-34-2026-11-03-rep"
LADDER = [f"scc-hrep-rep-2026-11-03-gte{n}" for n in (200, 205, 210)]
OTHER = "ewc-usse-nh-2026-11-03-dem"          # a market outside the midterm tiers


def prog(pid, target=15000.0, event_n=2):
    return Program(pool=450.0, target=target, df=0.25, status="active", pid=pid,
                   event_n=event_n)


class NoDesk:
    def __getattr__(self, name):
        raise AssertionError(f"the tier fairs touched the desk ({name})")


class Fam:
    def __init__(self):
        self.universe = {}
        self.terms = TermsStore()
        self.cache = BookCache()
        self.desk = NoDesk()
        self.orders = {}

    def add(self, slug, name, pid, bids, asks, event_n=2, target=15000.0, at=T0):
        self.universe[slug] = {"event_n": event_n, "name": name}
        self.terms.current[slug] = prog(pid, target, event_n)
        self.cache.put(slug, Book(bids=tuple(bids), asks=tuple(asks), tick=0.01, fetched_at=at))


def pair(fam, dem_px=(0.62, 0.64), rep_px=(0.36, 0.38), at=T0):
    t2 = "midterms_t2_competitive_senate_gov_seatcounts_20260924"
    fam.add(DEM, "TX-34 House Election Winner — dem", t2,
            [(dem_px[0], 5000.0)], [(dem_px[1], 5000.0)], at=at)
    fam.add(REP, "TX-34 House Election Winner — rep", t2,
            [(rep_px[0], 5000.0)], [(rep_px[1], 5000.0)], at=at)


class TestTheTiers(unittest.TestCase):
    def test_a_market_is_in_the_tier_its_program_names(self):
        self.assertEqual(tier_of("midterms_t1_control_bop_tossup_senate_20260924"), "t1")
        self.assertEqual(tier_of("midterms_t2_competitive_senate_gov_seatcounts_20260924"), "t2")
        self.assertEqual(tier_of("midterms_t3_coverage_gov_senate_house_districts_20260924"), "t3")
        self.assertEqual(tier_of("midterms_t4_house_winners_20260924"), "t4")
        self.assertIsNone(tier_of("politics_t4_coverage_20260924"))
        self.assertIsNone(tier_of("politics_low_20260924"))

    def test_only_tier_markets_are_read(self):
        fam = Fam()
        pair(fam)
        fam.add(OTHER, "New Hampshire Senate — dem", "politics_low_20260924",
                [(0.8, 100.0)], [(0.9, 100.0)])
        tf = TierFair(fam, clock=lambda: T0)
        self.assertEqual(set(tf.markets()), {DEM, REP})


class TestTheBookAtDepth(unittest.TestCase):
    def test_one_small_order_cannot_move_it(self):
        # a 5-share bid at 70c in front of 5,000 at 62c
        levels = [(0.70, 5.0), (0.62, 5000.0)]
        px, got = depth_price(levels, 1500.0)
        self.assertAlmostEqual(got, 1500.0)
        self.assertAlmostEqual(px, (5 * 0.70 + 1495 * 0.62) / 1500, places=6)
        self.assertLess(px, 0.621)

    def test_a_thin_side_uses_what_it_has(self):
        px, got = depth_price([(0.40, 50.0)], 1500.0)
        self.assertAlmostEqual(px, 0.40)
        self.assertAlmostEqual(got, 50.0)
        self.assertEqual(depth_price([], 100.0), (None, 0.0))

    def test_the_depth_is_a_share_of_the_target(self):
        fam = Fam()
        pair(fam)
        tf = TierFair(fam, clock=lambda: T0)
        tf.tick(T0)
        self.assertEqual(tf.cur[DEM]["depth"], max(FAIR_DEPTH_MIN, 1500.0))

    def test_a_stale_book_gives_no_fair(self):
        fam = Fam()
        pair(fam, at=T0 - BOOK_STALE_S - 5)
        tf = TierFair(fam, clock=lambda: T0)
        tf.tick(T0)
        self.assertEqual(tf.cur, {})


class TestTheLinkedMarkets(unittest.TestCase):
    def test_a_pair_implies_one_less_the_other(self):
        fam = Fam()
        pair(fam, dem_px=(0.62, 0.64), rep_px=(0.30, 0.32))
        tf = TierFair(fam, clock=lambda: T0)
        tf.tick(T0)
        self.assertAlmostEqual(tf.cur[DEM]["inputs"]["linked"], 1 - 0.31, places=6)
        self.assertAlmostEqual(tf.cur[REP]["inputs"]["linked"], 1 - 0.63, places=6)

    def test_no_link_without_every_outcome(self):
        fam = Fam()
        pair(fam)
        fam.universe[DEM]["event_n"] = 3          # a third outcome we do not have
        tf = TierFair(fam, clock=lambda: T0)
        tf.tick(T0)
        self.assertNotIn("linked", tf.cur[DEM]["inputs"])

    def test_an_at_least_ladder_is_made_to_fall(self):
        fit = _falling_fit([(200, 0.50), (205, 0.55), (210, 0.30)])
        self.assertAlmostEqual(fit[200], 0.525)
        self.assertAlmostEqual(fit[205], 0.525)
        self.assertAlmostEqual(fit[210], 0.30)
        fam = Fam()
        t2 = "midterms_t2_competitive_senate_gov_seatcounts_20260924"
        for slug, (b, a) in zip(LADDER, ((0.49, 0.51), (0.54, 0.56), (0.29, 0.31))):
            fam.add(slug, f"2026 Midterms: Republican House Seats? — {slug.rsplit('-', 1)[1]}",
                    t2, [(b, 5000.0)], [(a, 5000.0)], event_n=12)
        tf = TierFair(fam, clock=lambda: T0)
        tf.tick(T0)
        self.assertAlmostEqual(tf.cur[LADDER[0]]["inputs"]["linked"], 0.525, places=6)
        self.assertAlmostEqual(tf.cur[LADDER[2]]["inputs"]["linked"], 0.30, places=6)


class TestTheOtherInputs(unittest.TestCase):
    def test_a_recent_print_and_silver_count_an_old_one_does_not(self):
        fam = Fam()
        pair(fam)
        prints = {DEM: (0.66, T0 - 60, True), REP: (0.30, T0 - 7 * 3600, True)}
        tf = TierFair(fam, prints=prints.get, silver={DEM: 0.70}.get, clock=lambda: T0)
        tf.tick(T0)
        self.assertEqual(tf.cur[DEM]["inputs"]["print"], 0.66)
        self.assertEqual(tf.cur[DEM]["inputs"]["silver"], 0.70)
        self.assertNotIn("print", tf.cur[REP]["inputs"])

    def test_a_first_frame_price_of_unknown_age_is_no_print(self):
        fam = Fam()
        pair(fam)
        tf = TierFair(fam, prints={DEM: (0.66, T0, False)}.get, clock=lambda: T0)
        tf.tick(T0)
        self.assertNotIn("print", tf.cur[DEM]["inputs"])

    def test_equal_weights_until_measured(self):
        fam = Fam()
        pair(fam, dem_px=(0.62, 0.64), rep_px=(0.36, 0.38))
        tf = TierFair(fam, silver={DEM: 0.70}.get, clock=lambda: T0)
        tf.tick(T0)
        r = tf.cur[DEM]
        vals = list(r["inputs"].values())
        self.assertAlmostEqual(r["fair"], sum(vals) / len(vals), places=6)
        self.assertGreater(r["conf"], 0.0)


class TestTheGrading(unittest.TestCase):
    def run_hour(self, fam, tf, move_to):
        """Readings every minute for an hour and ten minutes while the
        dem book moves toward `move_to`."""
        t = T0
        tf.tick(t)
        end = T0 + max(GRADE_HORIZONS) + 10 * 60
        while t < end:
            t += SNAP_S
            frac = min((t - T0) / 3600.0, 1.0)
            mid = 0.63 + (move_to - 0.63) * frac
            fam.cache.put(DEM, Book(bids=((mid - 0.01, 5000.0),), asks=((mid + 0.01, 5000.0),),
                                    tick=0.01, fetched_at=t))
            fam.cache.put(REP, Book(bids=((1 - mid - 0.01, 5000.0),), asks=((1 - mid + 0.01, 5000.0),),
                                    tick=0.01, fetched_at=t))
            tf.tick(t)
        return t

    def test_fair_and_midpoint_are_graded_an_hour_later(self):
        fam = Fam()
        pair(fam)
        tf = TierFair(fam, silver={DEM: 0.70}.get, clock=lambda: T0)
        self.run_hour(fam, tf, 0.70)
        g = tf.grade_view("t2")["3600"]
        self.assertIn("fair", g)
        self.assertIn("mid", g)
        self.assertIn("silver", g)
        # Silver was right about where the price went, so it misses least
        self.assertLess(g["silver"]["mae_c"], g["mid"]["mae_c"])
        self.assertLess(g["fair"]["mae_c"], g["mid"]["mae_c"])

    def test_a_wide_touch_is_not_graded(self):
        fam = Fam()
        pair(fam, dem_px=(0.40, 0.80), rep_px=(0.20, 0.60))
        tf = TierFair(fam, clock=lambda: T0)
        tf.tick(T0)
        tf.tick(T0 + SNAP_S)
        self.assertEqual(tf.snaps, {})

    def test_weights_follow_the_measured_errors(self):
        fam = Fam()
        pair(fam)
        tf = TierFair(fam, silver={DEM: 0.70}.get, clock=lambda: T0)
        from v3.tierfair import Stat
        good, bad = Stat(), Stat()
        for _ in range(int(WEIGHT_MIN_N) + 5):
            good.add(0.005, T0)
            bad.add(0.05, T0)
        tf.stats[("t2", 3600, "silver")] = good
        tf.stats[("t2", 3600, "book")] = bad
        tf.tick(T0)
        w = tf.cur[DEM]["weights"]
        self.assertGreater(w["silver"], w["book"])


class TestThePageAndTheSave(unittest.TestCase):
    def test_the_page_is_read_only_and_lists_the_tiers(self):
        fam = Fam()
        pair(fam)
        tf = TierFair(fam, clock=lambda: T0)
        tf.tick(T0)
        page = json.loads(tf.payload_json)
        self.assertTrue(page["read_only"])
        self.assertEqual([t["key"] for t in page["tiers"]], ["t1", "t2", "t3", "t4"])
        t2 = page["tiers"][1]
        self.assertEqual((t2["markets"], t2["with_fair"]), (2, 2))
        self.assertEqual({r["market"] for r in page["rows"]}, {DEM, REP})

    def test_saved_and_restored(self):
        fam = Fam()
        pair(fam)
        tf = TierFair(fam, silver={DEM: 0.70}.get, clock=lambda: T0)
        TestTheGrading().run_hour(fam, tf, 0.70)
        back = TierFair(fam, clock=lambda: T0)
        back.restore(json.loads(json.dumps(tf.to_dict())))
        self.assertEqual(back.grade_view("t2"), tf.grade_view("t2"))


class TestTheStreamKeepsTheLastTrade(unittest.TestCase):
    def test_a_change_is_a_print_the_first_frame_is_not(self):
        from v3.ws import Stream
        st = Stream(BookCache(), lambda: [], "k", "s")

        def lite(ltp, oi):
            return json.dumps({"marketDataLite": {"marketSlug": DEM, "lastTradePx": {"value": str(ltp)},
                                                  "openInterest": str(oi),
                                                  "bestBid": {"value": "0.62"},
                                                  "bestAsk": {"value": "0.64"}}})
        st.apply_frame(lite(0.63, 100))
        self.assertFalse(st.last_trade[DEM][2])
        st.apply_frame(lite(0.65, 120))
        self.assertEqual(st.last_trade[DEM][0], 0.65)
        self.assertTrue(st.last_trade[DEM][2])


class TestTheAppRunsIt(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(self.dir.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def test_wired_saved_and_served(self):
        from v3.main import Monitor
        from v3.web import WebServer, PAGES
        mon = Monitor()
        self.assertIsNotNone(mon.tierfair)
        mon.tierfair.tick()
        self.assertTrue(json.loads(mon.tiers_json())["ok"])
        self.assertIn("/tiers", PAGES)
        self.assertIsNone(mon._last_print("nothing"))
        WebServer(mon)                     # builds with the route in place


if __name__ == "__main__":
    unittest.main()
