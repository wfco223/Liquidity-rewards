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
from v3.tierfair import (BETA_HORIZON, BETA_MIN_N, BETA_SHRINK_N, BOOK_STALE_S,
                         FAIR_DEPTH_MIN, FAIR_VERSION, GRADE_HORIZONS, SLOW_EVERY_S,
                         SLOW_PAGE_ROWS, SLOW_SNAP_S, SNAP_S,
                         WEIGHT_MIN_N, Beta, TierFair, _falling_fit, depth_price, tier_of)

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
        # the $2 program is a tier of its own (owner, 2026-09-25 "both $5 and $2")
        self.assertEqual(tier_of("politics_t4_coverage_20260924"), "t4c")
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

    def test_until_an_input_proves_itself_the_fair_is_the_midpoint(self):
        # owner, 2026-09-25 "Start the fair from the midpoint": Silver at
        # 70c and the linked market at 63c move nothing until they have
        # shown the midpoint follows them
        fam = Fam()
        pair(fam, dem_px=(0.62, 0.64), rep_px=(0.36, 0.38))
        tf = TierFair(fam, silver={DEM: 0.70}.get, clock=lambda: T0)
        tf.tick(T0)
        r = tf.cur[DEM]
        self.assertEqual(r["base"], "midpoint")
        self.assertAlmostEqual(r["fair"], 0.63, places=6)
        self.assertEqual(r["moves"], {})
        self.assertTrue(all(v == 0.0 for v in r["weights"].values()))
        self.assertGreater(r["conf"], 0.0)

    def test_with_no_midpoint_to_start_from_the_inputs_are_averaged(self):
        # a touch wider than MID_TRUST_SPREAD is no midpoint
        fam = Fam()
        pair(fam, dem_px=(0.50, 0.76), rep_px=(0.24, 0.50))
        tf = TierFair(fam, silver={DEM: 0.70}.get, clock=lambda: T0)
        tf.tick(T0)
        r = tf.cur[DEM]
        self.assertEqual(r["base"], "inputs")
        vals = list(r["inputs"].values())
        self.assertAlmostEqual(r["fair"], sum(vals) / len(vals), places=6)

    def test_a_proven_input_moves_the_fair_by_its_share(self):
        fam = Fam()
        pair(fam, dem_px=(0.62, 0.64), rep_px=(0.36, 0.38))
        tf = TierFair(fam, silver={DEM: 0.70}.get, clock=lambda: T0)
        bt = Beta()
        for _ in range(200):                  # the midpoint went half of Silver's distance
            bt.add(0.07, 0.035, T0)
        tf.betas[("t2", BETA_HORIZON, "silver")] = bt
        tf.tick(T0)
        r = tf.cur[DEM]
        share = 0.5 * 200 / (200 + BETA_SHRINK_N)
        self.assertAlmostEqual(r["weights"]["silver"], round(share, 4), places=4)
        self.assertAlmostEqual(r["fair"], 0.63 + share * 0.07, places=6)
        self.assertAlmostEqual(r["moves"]["silver"], share * 0.07, places=5)


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
        # and until it has proven itself the fair IS the midpoint...
        self.assertEqual(g["fair"]["mae_c"], g["mid"]["mae_c"])
        # ...while the tally learns how far the midpoint followed it
        bt = tf.betas[("t2", 3600, "silver")]
        self.assertGreater(bt.n, 0.0)
        self.assertGreater(bt.sxy, 0.0)

    def test_a_wide_touch_is_not_graded(self):
        fam = Fam()
        pair(fam, dem_px=(0.40, 0.80), rep_px=(0.20, 0.60))
        tf = TierFair(fam, clock=lambda: T0)
        tf.tick(T0)
        tf.tick(T0 + SNAP_S)
        self.assertEqual(tf.snaps, {})

    def test_weights_follow_the_measured_errors(self):
        # the weighted average stands only where there is no midpoint
        fam = Fam()
        pair(fam, dem_px=(0.50, 0.76), rep_px=(0.24, 0.50))
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


class TestTheShare(unittest.TestCase):
    def test_nothing_under_the_minimum_then_shrunk(self):
        bt = Beta()
        for _ in range(int(BETA_MIN_N) - 1):
            bt.add(0.02, 0.02, T0)
        self.assertEqual(bt.share(), 0.0)
        bt.add(0.02, 0.02, T0)
        n = BETA_MIN_N
        self.assertAlmostEqual(bt.share(), n / (n + BETA_SHRINK_N), places=6)

    def test_never_backwards_never_past_the_whole_distance(self):
        wrong, over = Beta(), Beta()
        for _ in range(100):
            wrong.add(0.02, -0.02, T0)        # the midpoint went the other way
            over.add(0.02, 0.05, T0)          # it went further than the input said
        self.assertEqual(wrong.share(), 0.0)
        self.assertAlmostEqual(over.share(), 100 / (100 + BETA_SHRINK_N), places=6)


T4C = "politics_t4_coverage_20260924"
T45 = "midterms_t4_house_winners_20260924"


def mov(fam, n, cache=None, at=T0, bid=0.20, ask=0.24):
    """n margin-of-victory buckets of one House district, $2 tier 4."""
    slugs = []
    for i in range(n):
        slug = f"vmc-ushrmov-tx-14-2026-11-03-rgte{i}"
        fam.universe[slug] = {"event_n": n, "name": f"TX-14 House Election Margin of Victory — rgte{i}"}
        fam.terms.current[slug] = Program(pool=2.0, target=2000.0, df=0.4, status="active",
                                          pid=T4C, event_n=n)
        (cache or fam.cache).put(slug, Book(bids=((bid, 3000.0),), asks=((ask, 3000.0),),
                                            tick=0.01, fetched_at=at))
        slugs.append(slug)
    return slugs


class TestTierFour(unittest.TestCase):
    """Owner, 2026-09-25: "Can you focus on getting the tier 4 markets
    identified and monitored both $5 and $2". The $5 house winners and
    the $2 coverage program are tiers of their own, their books come
    from the Tier 4 monitor's own stream store, and they are worked out
    every SLOW_EVERY_S rather than every second."""

    def test_both_programs_are_identified(self):
        fam = Fam()
        store = BookCache()
        fam.add("ushrewc-ushr-ak-al-2026-11-03-bilhil", "AK-AL House Election Winner — bilhil",
                T45, [(0.30, 3000.0)], [(0.34, 3000.0)], event_n=3, target=2000.0)
        mov(fam, 3, cache=store)
        tf = TierFair(fam, clock=lambda: T0, extra_cache=store)
        mk = tf.markets()
        self.assertEqual(sorted({t for t, _p in mk.values()}), ["t4", "t4c"])
        tf.tick(T0)
        self.assertEqual({r["tier"] for r in tf.cur.values()}, {"t4", "t4c"})
        page = json.loads(tf.payload_json)
        t4c = [t for t in page["tiers"] if t["key"] == "t4c"][0]
        self.assertEqual((t4c["markets"], t4c["with_book"], t4c["with_fair"]), (3, 3, 3))
        self.assertTrue(t4c["slow"])

    def test_the_monitors_store_is_read_and_the_freshest_book_wins(self):
        fam = Fam()
        store = BookCache()
        slugs = mov(fam, 2, cache=store, bid=0.20, ask=0.24)
        tf = TierFair(fam, clock=lambda: T0, extra_cache=store)
        tf.tick(T0)
        self.assertAlmostEqual(tf.cur[slugs[0]]["mid"], 0.22, places=6)
        # an older book in the family's cache does not win over it...
        fam.cache.put(slugs[0], Book(bids=((0.40, 3000.0),), asks=((0.44, 3000.0),),
                                     tick=0.01, fetched_at=T0 - 60))
        tf.tick(T0 + SLOW_EVERY_S)
        self.assertAlmostEqual(tf.cur[slugs[0]]["mid"], 0.22, places=6)
        # ...a newer one does
        fam.cache.put(slugs[0], Book(bids=((0.40, 3000.0),), asks=((0.44, 3000.0),),
                                     tick=0.01, fetched_at=T0 + 15))
        tf.tick(T0 + 2 * SLOW_EVERY_S)
        self.assertAlmostEqual(tf.cur[slugs[0]]["mid"], 0.42, places=6)

    def test_worked_out_every_ten_seconds_not_every_second(self):
        fam = Fam()
        pair(fam)                                   # a Tier 2 pair: every second
        slugs = mov(fam, 2)
        tf = TierFair(fam, clock=lambda: T0)
        tf.tick(T0)
        for s_ in [slugs[0], DEM]:
            b = (0.50, 0.54) if s_ == slugs[0] else (0.70, 0.72)
            fam.cache.put(s_, Book(bids=((b[0], 5000.0),), asks=((b[1], 5000.0),),
                                   tick=0.01, fetched_at=T0 + 1))
        tf.tick(T0 + 1)
        self.assertAlmostEqual(tf.cur[DEM]["mid"], 0.71, places=6)       # moved at once
        self.assertAlmostEqual(tf.cur[slugs[0]]["mid"], 0.22, places=6)  # held
        tf.tick(T0 + SLOW_EVERY_S)
        self.assertAlmostEqual(tf.cur[slugs[0]]["mid"], 0.52, places=6)

    def test_written_down_every_ten_minutes(self):
        fam = Fam()
        slugs = mov(fam, 2)
        tf = TierFair(fam, clock=lambda: T0)
        t = T0
        for _ in range(12):                         # twelve minutes of ticks, a minute apart
            for s_ in slugs:
                fam.cache.put(s_, Book(bids=((0.20, 3000.0),), asks=((0.24, 3000.0),),
                                       tick=0.01, fetched_at=t))
            tf.tick(t)
            t += SNAP_S
        self.assertEqual(len(tf.snaps[slugs[0]]), 2)   # at 0 and at 10 minutes

    def test_the_page_lists_a_hundred_of_each_most_recently_traded_first(self):
        fam = Fam()
        slugs = mov(fam, SLOW_PAGE_ROWS + 20)
        traded = slugs[-1]
        prints = {traded: (0.22, T0 - 30, True)}
        tf = TierFair(fam, prints=prints.get, clock=lambda: T0,
                      t4_status=lambda: {"subscribed": 5838, "connections": "2/2"})
        tf.tick(T0)
        page = json.loads(tf.payload_json)
        rows = [r for r in page["rows"] if r["tier"] == "t4c"]
        self.assertEqual(len(rows), SLOW_PAGE_ROWS)
        self.assertIn(traded, {r["market"] for r in rows})
        t4c = [t for t in page["tiers"] if t["key"] == "t4c"][0]
        self.assertEqual((t4c["markets"], t4c["rows_shown"]), (SLOW_PAGE_ROWS + 20, SLOW_PAGE_ROWS))
        self.assertEqual(page["t4_stream"]["subscribed"], 5838)


class TestThePageAndTheSave(unittest.TestCase):
    def test_the_page_is_read_only_and_lists_the_tiers(self):
        fam = Fam()
        pair(fam)
        tf = TierFair(fam, clock=lambda: T0)
        tf.tick(T0)
        page = json.loads(tf.payload_json)
        self.assertTrue(page["read_only"])
        self.assertEqual([t["key"] for t in page["tiers"]], ["t1", "t2", "t3", "t4", "t4c"])
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
        self.assertEqual(back.betas[("t2", 3600, "silver")].to_list(),
                         tf.betas[("t2", 3600, "silver")].to_list())

    def test_a_save_from_the_old_fair_drops_only_the_fairs_grade(self):
        fam = Fam()
        pair(fam)
        tf = TierFair(fam, silver={DEM: 0.70}.get, clock=lambda: T0)
        TestTheGrading().run_hour(fam, tf, 0.70)
        old = json.loads(json.dumps(tf.to_dict()))
        old.pop("version")
        old.pop("betas")
        back = TierFair(fam, clock=lambda: T0)
        back.restore(old)
        g = back.grade_view("t2")["3600"]
        self.assertNotIn("fair", g)            # a different fair: not carried over
        self.assertIn("mid", g)
        self.assertIn("silver", g)
        self.assertFalse(any("fair" in d for d in back.mstats.values()))
        self.assertEqual(tf.to_dict()["version"], FAIR_VERSION)


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

    def test_tier_four_has_its_own_stream_and_its_own_store(self):
        from v3.main import T4_STREAM_SHARDS, Monitor
        mon = Monitor()
        pol = mon.families["politics"]
        self.assertEqual(len(mon.t4_streams), T4_STREAM_SHARDS)
        self.assertTrue(all(not st.lite and st.max_subs == 10 and st.chunk == 200
                            for st in mon.t4_streams))
        self.assertFalse(set(map(id, mon.t4_streams)) & set(map(id, mon.streams)))
        self.assertIsNot(mon.t4cache, pol.cache)          # the engine never sees it
        self.assertTrue(all(st.cache is mon.t4cache for st in mon.t4_streams))
        self.assertIs(mon.tierfair.extra_cache, mon.t4cache)
        cov = "vmc-ushrmov-tx-14-2026-11-03-rgte35"
        win = "ushrewc-ushr-ak-al-2026-11-03-bilhil"
        t3 = "ushrewc-ushr-tx-34-2026-11-03-dem"
        for slug, pid in ((cov, T4C), (win, T45),
                          (t3, "midterms_t3_coverage_gov_senate_house_districts_20260924")):
            pol.universe[slug] = {"event_n": 2, "name": slug}
            pol.terms.current[slug] = Program(pool=2.0, target=2000.0, df=0.4,
                                              status="active", pid=pid, event_n=2)
        self.assertEqual(mon._t4_slugs(), [win, cov])      # the $5 first; tier 3 is not here
        st = mon.t4_stream_status()
        self.assertEqual((st["wanted"], st["subscribed"], st["refused"]), (2, 0, 0))
        mon.t4cache.put(cov, Book(bids=((0.20, 3000.0),), asks=((0.24, 3000.0),),
                                  tick=0.01, fetched_at=T0))
        self.assertEqual(mon.t4_stream_status()["books"], 1)
        self.assertIn("ws_t4", mon._state(T0, {}))


if __name__ == "__main__":
    unittest.main()
