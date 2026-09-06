"""The survey exists because pool size is the wrong question.

Owner, 2026-08-31: "We're getting a high share with low order size
(risk). So that wouldn't quite be an apples to apples comparison." A
$50 probe would have flattered NBA, where $50 buys far more shares than
in a 30c politics market. The measure is share of a side per dollar at
risk, priced at the size we actually use.
"""

import unittest

from v3.programs import Program
from v3.scoring import Book
from v3.survey import (LIVE_BUFFER_S, QUALIFY_TARGET_MULT,
                       PrefixStat, Sampler,
                       category_banned, group_of, is_live_event,
                       kind_of, leaderboard, probe_side,
                       summarise, to_csv)


def book(bids, asks, tick=0.01):
    return Book(bids=tuple(bids), asks=tuple(asks), tick=tick,
                fetched_at=1000.0)


class TestKind(unittest.TestCase):
    def test_the_kind_is_everything_before_the_date(self):
        self.assertEqual(kind_of("aachc-cfb-wins-2026-11-28-txst-5pt5wins"),
                         "aachc-cfb-wins")
        self.assertEqual(kind_of("tec-nba-westconf-2027-05-29-w-uta"),
                         "tec-nba-westconf")
        self.assertEqual(kind_of("aqc-nfl-2027-01-10-playoffq-ari"), "aqc-nfl")

    def test_a_slug_with_no_date_is_its_own_kind(self):
        self.assertEqual(kind_of("enwc-uspres-nom-rep-elomus"),
                         "enwc-uspres-nom-rep-elomus")


class TestProbe(unittest.TestCase):
    """A thin touch behind a qualifying wall is the cfb shape; a fat
    touch is NBA's, however rich the pool."""

    def test_a_thin_touch_behind_a_wall_takes_a_real_share(self):
        # 6 shares at the touch, a 20k wall out at 99c carrying the side
        # over Target Size — one share should be worth a large slice
        b = book(bids=[(0.30, 6.0), (0.29, 4.0), (0.01, 20000.0)], asks=[])
        prog = Program(pool=100.0, target=7500.0, df=0.25, event_n=2)
        p = probe_side(b, prog, "BUY", side_pool=25.0)
        self.assertTrue(p.qualifies, p.note)
        self.assertEqual(p.touch_size, 6.0)
        self.assertGreater(p.share, 0.05)
        # and it cost 30 cents to get it
        self.assertAlmostEqual(p.risk_usd, 0.30, places=4)
        self.assertGreater(p.share_per_dollar, 0.15)

    def test_a_fat_touch_takes_almost_nothing_however_rich_the_pool(self):
        # NBA's shape: the same probe against 500k resting at the touch
        b = book(bids=[(0.30, 500_000.0)], asks=[])
        prog = Program(pool=200.0, target=10000.0, df=0.25, event_n=1)
        p = probe_side(b, prog, "BUY", side_pool=100.0)
        self.assertTrue(p.qualifies)
        self.assertLess(p.share, 0.001)
        # the pool is FOUR TIMES richer and the order is worth far less
        self.assertLess(p.est_day, 0.25)

    def test_a_side_under_target_size_pays_nobody(self):
        b = book(bids=[(0.30, 6.0)], asks=[])
        prog = Program(pool=100.0, target=7500.0, df=0.25, event_n=1)
        p = probe_side(b, prog, "BUY", side_pool=50.0)
        self.assertFalse(p.qualifies)
        self.assertEqual(p.share, 0.0)
        self.assertEqual(p.est_day, 0.0)
        self.assertIn("Target Size", p.note)

    def test_an_empty_side_is_reported_not_crashed(self):
        p = probe_side(book(bids=[], asks=[(0.5, 3.0)]), Program(pool=1.0, target=1.0, df=0.5),
                       "BUY", side_pool=1.0)
        self.assertEqual(p.note, "side is empty")
        self.assertEqual(p.share_per_dollar, 0.0)

    def test_near_size_counts_only_what_scores(self):
        b = book(bids=[(0.30, 5.0), (0.29, 7.0), (0.10, 900.0)], asks=[])
        p = probe_side(b, Program(pool=10.0, target=1.0, df=0.5, event_n=1),
                       "BUY", side_pool=5.0)
        self.assertEqual(p.touch_size, 5.0)
        self.assertEqual(p.near_size, 12.0)      # the 10c pile is far back
        self.assertEqual(p.total_size, 912.0)


class TestReport(unittest.TestCase):
    def test_kinds_rank_by_share_per_dollar_not_by_pool(self):
        rows = [
            {"kind": "fat-pool", "qualifies": 1, "share_pct": 0.02,
             "est_day_usd": 0.01, "share_per_dollar": 0.06},
            {"kind": "thin-touch", "qualifies": 1, "share_pct": 13.0,
             "est_day_usd": 0.98, "share_per_dollar": 31.0},
        ]
        out = summarise(rows)["kinds"]
        self.assertEqual(out[0]["kind"], "thin-touch")

    def test_csv_has_a_header_and_survives_commas(self):
        b = book(bids=[(0.30, 6.0)], asks=[])
        r = probe_side(b, Program(pool=1.0, target=1.0, df=0.5, event_n=1),
                       "BUY", 1.0).row("a,b", "kind")
        text = to_csv([r])
        self.assertTrue(text.startswith("market,kind,side,"))
        self.assertEqual(len(text.strip().split("\n")), 2)
        self.assertNotIn("a,b", text)            # the comma was neutralised


class TestLiveEvents(unittest.TestCase):
    """Owner, 2026-08-31: "we'll probably want to stay out of live
    events until I have a way of quoting them better." Re-checked every
    pass, because a market that is quiet in the morning goes live at
    kickoff."""

    NOW = 1_788_200_000.0

    def test_a_game_already_started_is_live(self):
        self.assertTrue(is_live_event(
            {"gameStartTime": self.NOW - 600}, self.NOW))

    def test_the_hour_before_kickoff_is_live_too(self):
        self.assertTrue(is_live_event(
            {"gameStartTime": self.NOW + 600}, self.NOW))
        self.assertFalse(is_live_event(
            {"gameStartTime": self.NOW + LIVE_BUFFER_S + 60}, self.NOW))

    def test_a_market_with_no_game_never_goes_live(self):
        # futures and politics trade the same way all day
        self.assertFalse(is_live_event({"slug": "x"}, self.NOW))
        self.assertFalse(is_live_event({"gameStartTime": None}, self.NOW))
        self.assertFalse(is_live_event({}, self.NOW))

    def test_it_reads_the_exchanges_time_formats(self):
        iso = "2026-09-01T00:00:00Z"
        self.assertFalse(is_live_event({"gameStartTime": iso}, 1_788_100_000.0))
        self.assertTrue(is_live_event({"gameStartTime": iso}, 1_788_300_000.0))
        # milliseconds, as some feeds send
        self.assertTrue(is_live_event(
            {"gameStartTime": self.NOW * 1000}, self.NOW))
        # junk is not a reason to skip a market
        self.assertFalse(is_live_event({"gameStartTime": "soon"}, self.NOW))


class TestSampling(unittest.TestCase):
    """The guarantee he asked for: random within prefix, every prefix
    gets its turn, seeded so a run can be audited."""

    def pop(self):
        return (["big-2026-01-01-%d" % i for i in range(200)]
                + ["small-2026-01-01-%d" % i for i in range(3)])

    def test_a_tiny_prefix_is_sampled_as_often_as_a_huge_one(self):
        # uniform over MARKETS would draw 'small' about 1 time in 68
        s = Sampler(seed=1)
        s.load(self.pop())
        got = s.next_batch(20)
        n_small = sum(1 for x in got if x.startswith("small"))
        self.assertGreaterEqual(n_small, 9, "round robin should alternate")

    def test_the_draw_within_a_prefix_is_not_the_api_order(self):
        s = Sampler(seed=7)
        s.load(self.pop())
        got = [x for x in s.next_batch(40) if x.startswith("big")]
        in_order = ["big-2026-01-01-%d" % i for i in range(len(got))]
        self.assertNotEqual(got, in_order)

    def test_no_market_repeats_until_its_prefix_is_exhausted(self):
        s = Sampler(seed=3)
        s.load(["small-2026-01-01-%d" % i for i in range(3)])
        first = s.next_batch(3)
        self.assertEqual(len(set(first)), 3, "drew the same market twice")

    def test_the_same_seed_reproduces_the_run(self):
        a, b = Sampler(seed=42), Sampler(seed=42)
        a.load(self.pop())
        b.load(self.pop())
        self.assertEqual(a.next_batch(25), b.next_batch(25))
        c = Sampler(seed=43)
        c.load(self.pop())
        self.assertNotEqual(a.next_batch(25), c.next_batch(25))

    def test_the_state_reports_the_seed_and_the_frame(self):
        s = Sampler(seed=99)
        s.load(self.pop())
        st = s.state()
        self.assertEqual(st["seed"], 99)
        self.assertEqual(st["population"], 203)
        self.assertEqual(st["prefixes"], 2)


class TestLeaderboard(unittest.TestCase):
    def probe(self, share, risk):
        p = probe_side(book(bids=[(risk, 50.0)], asks=[]),
                       Program(pool=1.0, target=1.0, df=0.5, event_n=1),
                       "BUY", 1.0)
        p.share, p.risk_usd, p.qualifies = share, risk, True
        return p

    def test_a_prefix_is_not_ranked_on_too_few_samples(self):
        st = PrefixStat(prefix="lucky")
        st.record(self.probe(0.14, 0.47), 1000.0)     # the AFC South outlier
        out = leaderboard({"lucky": st}, min_samples=12)
        self.assertEqual(out["ranked"], [])
        self.assertEqual(out["sampling"][0]["prefix"], "lucky")

    def test_ranking_uses_the_median_not_the_max(self):
        st = PrefixStat(prefix="mixed")
        for _ in range(14):
            st.record(self.probe(0.0001, 0.5), 1000.0)   # mostly awful
        st.record(self.probe(0.90, 0.01), 1000.0)        # one jackpot
        row = leaderboard({"mixed": st}, min_samples=12)["ranked"][0]
        self.assertLess(row["median_spd"], 1.0, "a single outlier won")

    def test_old_samples_fall_out_of_the_window(self):
        st = PrefixStat(prefix="drifty")
        for _ in range(PrefixStat.KEEP + 30):
            st.record(self.probe(0.5, 0.5), 1000.0)
        self.assertEqual(len(st.spd), PrefixStat.KEEP)


class TestIncentivesMetadata(unittest.TestCase):
    """The docs (owner supplied, 2026-08-31) settled what the listing
    carries: category, subcategory, instrumentProduct and
    eventStartTime on every row. That replaces a per-market detail call
    and a slug-prefix guess."""

    def test_the_group_is_the_exchanges_own_labels(self):
        row = {"marketSlug": "aec-nba-bos-nyk-2026-04-01",
               "category": "Sports", "subcategory": "Basketball",
               "instrumentProduct": "moneyline"}
        self.assertEqual(group_of(row), "sports/basketball/moneyline")

    def test_the_product_separates_games_from_futures(self):
        # the thing the slug prefix could never do
        a = group_of({"marketSlug": "x-2026-01-01", "category": "sports",
                      "subcategory": "football", "instrumentProduct": "moneyline"})
        b = group_of({"marketSlug": "x-2026-01-01", "category": "sports",
                      "subcategory": "football", "instrumentProduct": "futures"})
        self.assertNotEqual(a, b)

    def test_an_unlabelled_row_falls_back_to_the_market_type(self):
        # coarse on purpose: at fixture grain every game was a stratum,
        # 67 markets each, waiting days for a turn (owner, 2026-08-31)
        self.assertEqual(group_of({"marketSlug": "aachc-cfb-wins-2026-11-28-x"}),
                         "aachc-cfb")
        self.assertEqual(group_of({"marketSlug": "astatc-cfb-akron-wake"}),
                         "astatc-cfb")
        self.assertEqual(group_of({"marketSlug": "astatc-cfb-col-gtech"}),
                         "astatc-cfb")

    def test_event_start_time_drives_the_live_check(self):
        now = 1_788_200_000.0
        self.assertTrue(is_live_event({"eventStartTime": now - 60}, now))
        self.assertFalse(is_live_event(
            {"eventStartTime": now + LIVE_BUFFER_S + 60}, now))

    def test_econ_is_kept_out_by_the_exchanges_category(self):
        self.assertTrue(category_banned({"category": "Economics"}))
        self.assertTrue(category_banned({"category": "sports",
                                         "subcategory": "CPI"}))
        self.assertFalse(category_banned({"category": "sports",
                                          "subcategory": "basketball"}))
        self.assertFalse(category_banned({}))

    def test_strata_can_be_supplied_rather_than_derived(self):
        s = Sampler(seed=5)
        s.load([("a-2026-01-01-1", "sports/football/moneyline"),
                ("b-2026-01-01-2", "sports/football/moneyline"),
                ("c-2026-01-01-3", "politics/senate/winner")])
        self.assertEqual(s.state()["prefixes"], 2)


class TestYieldRanking(unittest.TestCase):
    """Owner, 2026-08-31: "Number 1 was the concern I had." Share per
    dollar says how cheaply a side can be owned and nothing about
    whether it pays. Ranking on it made MLB look 1300x worse than cfb
    when on yield it is within a whisker."""

    def probe(self, share, pool, risk):
        p = probe_side(book(bids=[(risk, 50.0)], asks=[]),
                       Program(pool=1.0, target=1.0, df=0.5, event_n=1),
                       "BUY", 1.0)
        p.share, p.risk_usd, p.qualifies = share, risk, True
        p.est_day = share * pool
        return p

    def test_yield_is_earnings_over_risk(self):
        p = self.probe(0.10, 8.0, 0.40)     # 10% of an $8 side for 40c
        self.assertAlmostEqual(p.est_day, 0.80, places=4)
        self.assertAlmostEqual(p.yield_per_dollar, 2.0, places=4)
        self.assertAlmostEqual(p.share_per_dollar, 0.25, places=4)

    def test_a_cheap_side_that_pays_nothing_does_not_win(self):
        # half a side for a penny, but the side is worth 1c a day
        cheap = PrefixStat(prefix="cheap-and-worthless")
        # a fifth of a side for a dollar, but the side pays $20 a day
        rich = PrefixStat(prefix="pays-real-money")
        for _ in range(14):
            cheap.record(self.probe(0.50, 0.01, 0.01), 1000.0)
            rich.record(self.probe(0.20, 20.0, 1.00), 1000.0)
        out = leaderboard({"cheap-and-worthless": cheap,
                           "pays-real-money": rich}, min_samples=12)
        # share per dollar would crown the worthless one, 50 against 0.2
        self.assertGreater(cheap.row()["median_spd"],
                           rich.row()["median_spd"])
        # yield puts the money first
        self.assertEqual(out["ranked"][0]["prefix"], "pays-real-money")

    def test_the_row_carries_both_measures(self):
        st = PrefixStat(prefix="x")
        for _ in range(12):
            st.record(self.probe(0.10, 8.0, 0.40), 1000.0)
        row = st.row()
        self.assertAlmostEqual(row["median_ypd"], 2.0, places=3)
        self.assertAlmostEqual(row["median_spd"], 0.25, places=3)


class TestStrataStayUsable(unittest.TestCase):
    """Owner, 2026-08-31: "just try and keep it going so I can get some
    data." A stratum per fixture, or one holding three markets, both
    mean the board never ranks anything."""

    def test_fixtures_collapse_into_one_market_type(self):
        s = Sampler(seed=1)
        s.load([(f"astatc-cfb-{a}-{b}-2026-01-01-{i}",
                 group_of({"marketSlug": f"astatc-cfb-{a}-{b}-2026-01-01-{i}"}))
                for a, b in (("akron", "wake"), ("col", "gtech"),
                             ("merri", "del"))
                for i in range(9)])
        self.assertEqual(list(s.all), ["astatc-cfb"])
        self.assertEqual(len(s.all["astatc-cfb"]), 27)

    def test_a_stratum_too_small_to_rank_merges_upward(self):
        s = Sampler(seed=1)
        s.load([(f"m{i}", "cul/awd/tac") for i in range(3)]
               + [(f"n{i}", "cul/awd/tpoyc") for i in range(3)])
        self.assertEqual(list(s.all), ["cul/awd"])
        self.assertEqual(s.state()["merged"], 2)
        self.assertEqual(s.state()["too_small"], 0)

    def test_it_merges_again_if_the_parent_is_also_too_small(self):
        s = Sampler(seed=1)
        s.load([("a", "cul/awd/tac"), ("b", "cul/movies/x"),
                ("c", "cul/music/y"), ("d", "cul/tv/z"),
                ("e", "cul/game/w"), ("f", "cul/book/v")])
        self.assertEqual(list(s.all), ["cul"])

    def test_a_top_level_stratum_is_left_alone_even_if_small(self):
        # nothing to merge into; it stays and simply never ranks
        s = Sampler(seed=1)
        s.load([("a", "solo"), ("b", "solo")])
        self.assertEqual(list(s.all), ["solo"])
        self.assertEqual(s.state()["too_small"], 1)

    def test_the_state_reports_the_shape(self):
        s = Sampler(seed=1)
        s.load([(f"m{i}", "big-one") for i in range(40)])
        st = s.state()
        self.assertEqual(st["biggest"], 40)
        self.assertEqual(st["prefixes"], 1)


class TestTournamentPools(unittest.TestCase):
    """Owner, 2026-08-31: "golf may be on a tournament time scale so I
    think we would have to divide by the number of days between the
    listing date and the conclusion." rewardPool is the total for the
    PERIOD; read as daily it overstates by the length of the event."""

    def test_an_open_ended_pool_is_already_daily(self):
        p = Program(pool=100.0, target=1.0, df=0.2,
                    start="2026-07-28T00:00:00Z", end="")
        self.assertIsNone(p.period_days)
        self.assertAlmostEqual(p.daily_pool, 100.0)

    def test_a_tournament_pool_is_spread_over_its_days(self):
        p = Program(pool=3000.0, target=1.0, df=0.2,
                    start="2026-03-28T04:00:00Z", end="2026-04-01T21:00:00Z")
        self.assertAlmostEqual(p.period_days, 4.7083, places=3)
        self.assertAlmostEqual(p.daily_pool, 637.17, places=1)

    def test_a_period_under_a_day_is_never_inflated(self):
        # dividing by 0.25 would QUADRUPLE it; the divisor floors at one
        p = Program(pool=50.0, target=1.0, df=0.2,
                    start="2026-03-28T04:00:00Z", end="2026-03-28T10:00:00Z")
        self.assertAlmostEqual(p.daily_pool, 50.0)

    def test_junk_dates_fall_back_to_the_raw_pool(self):
        for a, b in (("", "2026-04-01T21:00:00Z"), ("soon", "later"),
                     ("2026-04-01T21:00:00Z", "2026-03-28T04:00:00Z")):
            p = Program(pool=80.0, target=1.0, df=0.2, start=a, end=b)
            self.assertAlmostEqual(p.daily_pool, 80.0)

    def test_the_probe_prices_a_tournament_per_day(self):
        # the same book scored against a 5-day pool and a daily one
        b = book(bids=[(0.30, 6.0), (0.01, 20000.0)], asks=[])
        five = Program(pool=500.0, target=7500.0, df=0.25, event_n=1,
                       start="2026-04-01T00:00:00Z", end="2026-04-06T00:00:00Z")
        one = Program(pool=100.0, target=7500.0, df=0.25, event_n=1)
        a = probe_side(b, five, "BUY", five.daily_pool / 2.0)
        c = probe_side(b, one, "BUY", one.daily_pool / 2.0)
        self.assertAlmostEqual(a.est_day, c.est_day, places=6)


class TestBestMarkets(unittest.TestCase):
    """Owner, 2026-08-31: "Can you give the slugs of the top programs."
    A leaderboard of market kinds is no use if you cannot get from it to
    an actual order. The cycling survey kept only aggregates."""

    def probe(self, share, pool, risk):
        p = probe_side(book(bids=[(risk, 50.0)], asks=[]),
                       Program(pool=1.0, target=1.0, df=0.5, event_n=1),
                       "BUY", 1.0)
        p.share, p.risk_usd, p.qualifies = share, risk, True
        p.est_day, p.touch_px, p.touch_size = share * pool, risk, 40.0
        return p

    def test_the_best_markets_are_kept_with_their_slugs(self):
        st = PrefixStat(prefix="geo/treaty/dipcc")
        st.record(self.probe(0.02, 8.0, 0.20), 1000.0, "dipcc-ukraine-2026")
        row = st.row()
        self.assertEqual(row["best"][0]["market"], "dipcc-ukraine-2026")
        self.assertAlmostEqual(row["best"][0]["ypd"], 0.8, places=3)
        self.assertEqual(row["best"][0]["side"], "BUY")

    def test_only_the_best_few_are_kept(self):
        st = PrefixStat(prefix="x")
        for i in range(20):
            st.record(self.probe(0.001 * (i + 1), 8.0, 0.20), 1000.0, f"m{i}")
        self.assertEqual(len(st.best), PrefixStat.KEEP_BEST)
        # the strongest survived, the weakest did not
        self.assertEqual(st.best[0]["market"], "m19")
        self.assertNotIn("m0", [b["market"] for b in st.best])

    def test_a_market_seen_twice_is_not_listed_twice(self):
        st = PrefixStat(prefix="x")
        st.record(self.probe(0.01, 8.0, 0.20), 1000.0, "same")
        st.record(self.probe(0.02, 8.0, 0.20), 2000.0, "same")
        self.assertEqual(len(st.best), 1)
        self.assertAlmostEqual(st.best[0]["ypd"], 0.8, places=3)

    def test_a_side_that_does_not_qualify_is_never_listed(self):
        st = PrefixStat(prefix="x")
        p = self.probe(0.01, 8.0, 0.20)
        p.qualifies = False
        st.record(p, 1000.0, "under-target")
        self.assertEqual(st.best, [])


class TestQualifyHeadroom(unittest.TestCase):
    """Owner, 2026-09-01: "make it so my orders buy 125% of the target
    size." Sitting exactly ON the line means one other trader pulling
    drops the side under it, and under the line the whole side pays
    nobody — the cost of being a share short is the entire day."""

    def test_the_multiplier_is_a_quarter_over(self):
        self.assertAlmostEqual(QUALIFY_TARGET_MULT, 1.25)

    def test_the_wall_rests_at_the_far_edge_of_its_side(self):
        from v3.survey import wall_collateral, wall_price
        self.assertAlmostEqual(wall_price("SELL", 0.01), 0.99)
        self.assertAlmostEqual(wall_price("SELL", 0.001), 0.999)
        self.assertAlmostEqual(wall_price("BUY", 0.01), 0.01)
        self.assertAlmostEqual(wall_price("BUY", 0.001), 0.001)
        self.assertAlmostEqual(wall_price("SELL", 0.005), 0.995)
        self.assertAlmostEqual(wall_price("BUY", 0.005), 0.005)
        # either wall holds the same buying power a share
        self.assertAlmostEqual(wall_collateral("SELL", 0.99, 1000.0), 10.0)
        self.assertAlmostEqual(wall_collateral("BUY", 0.01, 1000.0), 10.0)

    def test_the_goal_is_target_times_the_multiplier(self):
        self.assertAlmostEqual(7500 * QUALIFY_TARGET_MULT, 9375)

    def test_a_side_already_over_target_still_has_a_gap(self):
        # the case the old code called done: at Target Size exactly
        target, resting = 7500.0, 7500.0
        self.assertEqual(target - resting, 0.0)          # old: nothing to do
        self.assertAlmostEqual(target * QUALIFY_TARGET_MULT - resting, 1875.0)

    def test_it_stops_once_the_headroom_is_there(self):
        target, resting = 7500.0, 9400.0
        self.assertLess(target * QUALIFY_TARGET_MULT - resting, 0)

    def test_the_extra_shares_cost_a_cent_each_at_99c(self):
        # 25% of 7,500 shares at 99c is collateral of (1 - 0.99) each
        extra = 7500 * (QUALIFY_TARGET_MULT - 1.0)
        self.assertAlmostEqual(extra * (1.0 - 0.99), 18.75, places=2)


if __name__ == "__main__":
    unittest.main()
