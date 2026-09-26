"""Stage 2 of the tier engines: the value (owner, 2026-09-26 "We're going
to build things from the ground up. Focus on building").

Read-only: for every tier market, each side and each candidate price,
the reward an order would claim, the fills it would take and the money
it ties up; the best set of orders for $1,000 across the tiers; paper
orders followed on the tape to measure the fills; the meter's estimate
graded against the exchange's pay. Nothing here touches an order.
"""
import json
import math
import os
import random
import shutil
import subprocess
import tempfile
import time
import unittest

from v3.books import BookCache
from v3.family import FamilyOrder
from v3.programs import Program
from v3.scoring import Book, estimate_join
from v3.terms import TermsStore
from v3.tierfair import TierFair
from v3.tiervalue import (DAY_S, GAP_CUSHION, HAZ_PRIOR, LOSS_PRIOR, LOSS_PRIOR_N,
                          MARKET_CAP_FRAC, POT_USD, SLICE_USD, SPOT_H_S, Side, Step,
                          TierValue, candidates, run_greedy, side_share, summary, take)

T0 = 1_790_300_000.0
T1P = "midterms_t1_control_bop_tossup_senate_20260924"
T2P = "midterms_t2_competitive_senate_gov_seatcounts_20260924"
T4P = "midterms_t4_house_winners_20260924"
A = "ussewc-usse-ga-2026-11-03-dem"
B = "ussewc-usse-ga-2026-11-03-rep"
C = "ushrewc-ushr-tx-34-2026-11-03-dem"
W = "ushrewc-ushr-ak-al-2026-11-03-bilhil"


class NoDesk:
    def __getattr__(self, name):
        raise AssertionError(f"stage 2 touched the desk ({name})")


class Fam:
    def __init__(self):
        self.universe = {}
        self.terms = TermsStore()
        self.cache = BookCache()
        self.desk = NoDesk()
        self.orders = {}

    def _side_pool(self, slug, prog):
        return prog.pool / max(prog.event_n, 1) / 2.0

    def add(self, slug, pid, bids, asks, pool=1000.0, target=1000.0, df=0.5, at=T0,
            tick=0.01, start="2026-09-24T02:00:00Z"):
        self.universe[slug] = {"event_n": 2, "name": f"{slug} — x"}
        self.terms.current[slug] = Program(pool=pool, target=target, df=df, status="active",
                                           pid=pid, event_n=2, start=start)
        self.put(slug, bids, asks, at, tick)

    def put(self, slug, bids, asks, at, tick=0.01):
        self.cache.put(slug, Book(bids=tuple(bids), asks=tuple(asks), tick=tick, fetched_at=at))


def make(prints=None, pay=None, **kw):
    fam = Fam()
    tf = TierFair(fam, clock=lambda: T0)
    tv = TierValue(tf, fam, pay=pay, prints=prints, clock=lambda: T0, **kw)
    return fam, tf, tv


def side(levels, other=(), pool=10.0, target=1000.0, df=0.5, haz=0.1, loss=0.02,
         coc=0.005, first_day=False, slug="m", tier="t1", sd="BUY"):
    return Side(tier, slug, sd, list(levels), list(other), 0.01, df, target, pool,
                lambda *_a: haz, lambda _px: loss, coc, first_day, 8)


class TestTheExchangesArithmetic(unittest.TestCase):
    def test_one_order_scores_as_scoring_py_does(self):
        rnd = random.Random(7)
        for _ in range(300):
            sd = rnd.choice(("BUY", "SELL"))
            tick = rnd.choice((0.01, 0.001))
            base = rnd.uniform(0.2, 0.8)
            step = tick * rnd.randint(1, 4)
            lv = [(round(base - k * step if sd == "BUY" else base + k * step, 3),
                   float(rnd.choice((10, 100, 500, 3000)))) for k in range(rnd.randint(1, 8))]
            target = float(rnd.choice((500, 2000, 10000)))
            df = rnd.choice((0.2, 0.4, 0.5))
            px = round(lv[rnd.randrange(len(lv))][0] + rnd.choice((0, 1, -1)) * tick, 3)
            qty = float(rnd.choice((5, 50, 400, 3000)))
            j = estimate_join(sd, lv, tick, df, target, px, qty)
            sh, ok, _gap = side_share(sd, lv, tick, df, target, {px: qty})
            self.assertEqual(ok, j.qualifies)
            self.assertAlmostEqual(sh, j.share if j.qualifies else 0.0, places=9)

    def test_several_levels_of_ours_add_their_scores(self):
        lv = [(0.50, 1000.0), (0.49, 1000.0)]
        sh, ok, _ = side_share("BUY", lv, 0.01, 0.5, 1500.0, {0.50: 100.0, 0.49: 100.0})
        self.assertTrue(ok)
        # 1,100 at the touch (weight 1) and 1,100 a tick back (weight 0.5):
        # ours 100 + 50 of 1,100 + 550
        self.assertAlmostEqual(sh, 150.0 / 1650.0)

    def test_a_side_under_its_target_pays_nobody_until_we_carry_it(self):
        lv = [(0.50, 100.0)]
        self.assertEqual(side_share("BUY", lv, 0.01, 0.5, 1000.0, {}), (0.0, False, 900.0))
        sh, ok, _ = side_share("BUY", lv, 0.01, 0.5, 1000.0, {0.50: 950.0})
        self.assertTrue(ok)
        self.assertAlmostEqual(sh, 950.0 / 1050.0)


class TestTheCandidates(unittest.TestCase):
    def test_inside_the_touch_the_near_ticks_and_the_resting_levels(self):
        bids = [(0.50, 10.0), (0.49, 10.0), (0.45, 10.0), (0.40, 10.0)]
        asks = [(0.53, 10.0)]
        self.assertEqual(candidates("BUY", bids, asks, 0.01),
                         [0.51, 0.50, 0.49, 0.48, 0.47, 0.45])
        self.assertEqual(candidates("SELL", asks, bids, 0.01),
                         [0.52, 0.53, 0.54, 0.55, 0.56])

    def test_never_at_or_through_the_other_side(self):
        bids = [(0.50, 10.0)]
        asks = [(0.51, 10.0)]
        self.assertEqual(candidates("BUY", bids, asks, 0.01), [0.50, 0.49, 0.48, 0.47])
        self.assertEqual(candidates("BUY", [], asks, 0.01), [0.50, 0.49, 0.48, 0.47])


class TestTheValue(unittest.TestCase):
    def test_reward_less_fills_less_capital(self):
        sd = side([(0.50, 990.0)], [(0.53, 500.0)], pool=40.0, haz=2.0, loss=0.03)
        st = sd._eval(0.50, 10.0, alone=True)
        self.assertAlmostEqual(st.dr, 40.0 * 10.0 / 1000.0)
        self.assertAlmostEqual(st.df_, 2.0 * 10.0 * 0.03)
        self.assertAlmostEqual(st.dk, 10.0 * 0.50 * 0.005)
        self.assertAlmostEqual(st.value, st.dr - st.df_ - st.dk)
        self.assertAlmostEqual(st.key, st.value / st.cost)

    def test_an_ask_ties_up_the_rest_of_the_dollar(self):
        sd = side([(0.30, 990.0)], [(0.25, 500.0)], sd="SELL")
        self.assertAlmostEqual(sd._eval(0.30, 10.0).cost, 7.0)

    def test_a_first_day_pays_nothing(self):
        sd = side([(0.50, 990.0)], [(0.53, 500.0)], pool=40.0, first_day=True)
        self.assertEqual(sd._eval(0.50, 10.0).dr, 0.0)
        self.assertIsNone(sd.next_step(1000.0))


class TestTheBestSet(unittest.TestCase):
    def test_a_side_stops_by_itself_when_the_share_saturates(self):
        # $3 a side: each slice a tick inside the touch claims less than
        # the one before (ours over ours plus the 1,000 a tick back), and
        # at ~74 shares the next no longer beats its fills and capital
        sd = side([(0.50, 1000.0)], [(0.52, 1000.0)], pool=3.0, haz=0.1)
        steps = run_greedy([sd], POT_USD, 100.0)
        spent = sum(s.cost for s in steps)
        self.assertGreater(spent, 0)
        self.assertLess(spent, 100.0)                  # well short of the cap
        self.assertTrue(all(s.value > 0 for s in steps))
        self.assertIsNone(sd.next_step(1000.0))        # the next slice adds nothing

    def test_it_picks_the_price_the_money_is_worth_most_at(self):
        # a touch crowded with 5,000 and nothing a tick back: the slices
        # go a tick inside the touch while the spread allows it
        sd = side([(0.50, 5000.0)], [(0.54, 1000.0)], pool=50.0, target=2000.0, haz=0.05)
        steps = run_greedy([sd], POT_USD, 100.0)
        self.assertEqual(steps[0].px, 0.51)

    def test_the_money_spreads_to_a_second_market(self):
        a = side([(0.50, 1000.0)], [(0.52, 1000.0)], pool=40.0, slug="a")
        b = side([(0.50, 1000.0)], [(0.52, 1000.0)], pool=40.0, slug="b")
        steps = run_greedy([a, b], POT_USD, 100.0)
        per = {}
        for s in steps:
            per[s.slug] = per.get(s.slug, 0.0) + s.cost
        self.assertEqual(set(per), {"a", "b"})
        self.assertLess(abs(per["a"] - per["b"]), SLICE_USD + 1e-9)

    def test_no_market_past_its_cap_and_no_plan_past_the_budget(self):
        rich = [side([(0.50, 1000.0)], [(0.52, 1000.0)], pool=5000.0, slug=f"m{i}")
                for i in range(20)]
        steps = run_greedy(rich, 300.0, 100.0)
        per = {}
        for s in steps:
            per[s.slug] = per.get(s.slug, 0.0) + s.cost
        self.assertLessEqual(sum(per.values()), 300.0 + 1e-6)
        self.assertTrue(all(v <= 100.0 + 1e-6 for v in per.values()))

    def test_a_side_short_of_its_target_is_carried_over_in_one_step(self):
        sd = side([(0.50, 950.0)], [(0.53, 500.0)], pool=50.0, haz=0.05)
        steps = run_greedy([sd], POT_USD, 100.0)
        self.assertTrue(steps)
        first = steps[0]
        self.assertGreaterEqual(first.qty, 50.0 * (1.0 + GAP_CUSHION))
        self.assertGreater(first.dr, 0.0)
        self.assertGreater(sd.share, 0.0)

    def test_a_smaller_budget_is_a_prefix_of_the_run(self):
        def build():
            return [side([(0.50, 800.0 + 300 * i)], [(0.53, 900.0)], pool=20.0 + 7 * i,
                         slug=f"m{i}") for i in range(6)]
        full = run_greedy(build(), POT_USD, 100.0)
        for b in (25.0, 60.0, 140.0):
            direct = run_greedy(build(), b, 100.0)
            self.assertAlmostEqual(summary(take(full, b, 100.0))["value"],
                                   summary(direct)["value"], places=2)


class TestTheSplit(unittest.TestCase):
    def test_the_designs_split_and_the_joint_best(self):
        fam, tf, tv = make()
        t1 = [side([(0.50, 1000.0)], [(0.52, 1000.0)], pool=300.0, slug=f"a{i}", tier="t1")
              for i in range(8)]
        t2 = [side([(0.50, 1000.0)], [(0.52, 1000.0)], pool=60.0, slug=f"b{i}", tier="t2")
              for i in range(4)]
        cap = MARKET_CAP_FRAC * POT_USD
        tv.plans = {"t1": run_greedy(t1, POT_USD, cap), "t2": run_greedy(t2, POT_USD, cap)}
        v = tv._build_view(T0)
        a1, a2 = v["tiers"]["t1"]["alone"]["value"], v["tiers"]["t2"]["alone"]["value"]
        s1 = v["tiers"]["t1"]["split"]["share"]
        self.assertAlmostEqual(s1, a1 / (a1 + a2), places=3)
        self.assertEqual(v["tiers"]["t3"]["split"]["budget"], 0.0)
        design = sum(v["tiers"][t]["split"]["value"] for t in ("t1", "t2"))
        self.assertGreaterEqual(v["joint"]["value"], design - 0.01)
        self.assertLessEqual(v["joint"]["coll"], POT_USD + 1e-6)
        self.assertGreater(v["joint"]["by_tier"]["t1"]["coll"],
                           v["joint"]["by_tier"]["t2"]["coll"])


class TestThePlanOnTheBooks(unittest.TestCase):
    def test_our_own_orders_come_out_of_the_book_his_hands_stay(self):
        fam, tf, tv = make()
        fam.add(A, T1P, [(0.50, 800.0)], [(0.55, 800.0)])
        fam.orders["x"] = FamilyOrder(id="x", market=A, side="BUY", price=0.50, qty=300.0,
                                      intent="ORDER_INTENT_BUY_LONG", placed_ts=T0,
                                      purpose="earn")
        fam.orders["h"] = FamilyOrder(id="h", market=A, side="SELL", price=0.55, qty=200.0,
                                      intent="ORDER_INTENT_SELL_SHORT", placed_ts=T0,
                                      purpose="manual")
        bids, asks, _tick = tv._books(A, T0, tv._ours().get(A), 300.0)
        self.assertEqual(bids, [(0.50, 500.0)])
        self.assertEqual(asks, [(0.55, 800.0)])

    def test_the_plan_runs_reads_nothing_and_touches_no_order(self):
        fam, tf, tv = make()
        fam.add(A, T1P, [(0.50, 800.0)], [(0.55, 800.0)])
        fam.add(B, T1P, [(0.45, 800.0)], [(0.49, 800.0)])
        fam.add(C, T2P, [(0.30, 2000.0)], [(0.33, 2000.0)], pool=450.0, target=1500.0)
        tf.tick(T0)
        tv.tick(T0)
        v = tv.view()
        self.assertTrue(v["ok"])
        self.assertEqual(v["tiers"]["t1"]["meta"]["fresh"], 2)
        self.assertGreater(v["joint"]["coll"], 0.0)
        r = tv.row(A)
        self.assertIn("BUY", r)
        self.assertIn("pf1h", r["BUY"])
        self.assertEqual(len(tv.spots), 6)             # one paper order a side

    def test_an_old_book_is_not_planned_on(self):
        fam, tf, tv = make()
        fam.add(A, T1P, [(0.50, 800.0)], [(0.55, 800.0)], at=T0 - 1000.0)
        tv.plan(("t1",), T0)
        self.assertEqual(tv.meta["t1"]["fresh"], 0)
        self.assertEqual(tv.plans["t1"], [])

    def test_a_first_day_market_is_planned_nothing_but_still_watched(self):
        fam, tf, tv = make()
        fam.add(A, T1P, [(0.50, 800.0)], [(0.55, 800.0)])
        fam.terms.joined_at[A] = T0 - 60.0
        tv.plan(("t1",), T0)
        self.assertEqual(tv.plans["t1"], [])
        self.assertTrue(tv.row(A).get("first_day"))
        self.assertIn((A, "BUY"), tv.spots)            # its fills are still measured

    def test_tier_four_skips_a_side_that_cannot_beat_its_capital(self):
        fam, tf, tv = make()
        # $5 a day an event: a side of 20,000 shares at 50c can never
        # pay a $5 slice its 0.5% a day
        fam.add(W, T4P, [(0.50, 20000.0)], [(0.52, 20000.0)], pool=5.0, target=2000.0)
        tv.plan(("t4", "t4c"), T0)
        self.assertEqual(tv.meta["t4"]["sides"], 0)
        self.assertIsNotNone(tv.row(W))                # the page can still price it


class TestThePaperOrders(unittest.TestCase):
    def setUp(self):
        self.pr = {}
        self.fam, self.tf, self.tv = make(prints=lambda s: self.pr.get(s))
        # the touch 50c holds 500 ahead of a paper bid that joins it
        self.fam.add(A, T1P, [(0.50, 500.0), (0.49, 800.0)], [(0.51, 800.0)])
        self.tv.plan(("t1",), T0)
        self.sp = self.tv.spots[(A, "BUY")]

    def test_the_bid_joins_the_touch_behind_the_line(self):
        self.assertEqual(self.sp.px, 0.50)
        self.assertEqual(self.sp.ahead, 500.0)
        self.assertGreater(self.sp.pf, 0.0)

    def test_the_other_side_reaching_the_price_fills_it(self):
        self.fam.put(A, [(0.49, 800.0)], [(0.50, 300.0)], T0 + 5)
        self.tv.check_spots(T0 + 5, slow=False)
        self.assertNotIn((A, "BUY"), self.tv.spots)
        self.assertEqual(self.tv.grades["t1"][2], 1.0)
        self.assertEqual(len(self.tv.marks), 1)

    def test_a_trade_through_the_price_fills_it(self):
        self.fam.cache.note_trade(A, T0 + 5)
        self.pr[A] = (0.48, T0 + 5, True)
        self.tv.check_spots(T0 + 5, slow=False)
        self.assertNotIn((A, "BUY"), self.tv.spots)

    def test_a_trade_at_the_price_fills_it_only_with_nobody_left_ahead(self):
        self.fam.cache.note_trade(A, T0 + 5)
        self.pr[A] = (0.50, T0 + 5, True)
        self.tv.check_spots(T0 + 5, slow=False)
        self.assertIn((A, "BUY"), self.tv.spots)       # 500 were ahead of it
        # the line at 50c goes (filled or pulled — either way it was ahead)
        self.fam.put(A, [(0.49, 800.0)], [(0.51, 800.0)], T0 + 6)
        self.tv.check_spots(T0 + 6, slow=False)
        self.assertEqual(self.tv.spots[(A, "BUY")].ahead, 0.0)
        self.fam.cache.note_trade(A, T0 + 7)
        self.tv.check_spots(T0 + 7, slow=False)
        self.assertNotIn((A, "BUY"), self.tv.spots)

    def test_without_trade_prints_only_a_crossing_counts(self):
        # a trade price the stream never printed (no trade_seen): nothing
        self.pr[A] = (0.40, T0 + 5, True)
        self.tv.check_spots(T0 + 5, slow=False)
        self.assertIn((A, "BUY"), self.tv.spots)

    def test_it_ends_after_the_hour_unfilled_and_is_graded(self):
        for k in range(1, 13):
            self.fam.put(A, [(0.50, 500.0), (0.49, 800.0)], [(0.51, 800.0)], T0 + k * 300)
            self.tv.check_spots(T0 + k * 300, slow=False)
        self.assertNotIn((A, "BUY"), self.tv.spots)
        n, pred, seen, _sq = self.tv.grades["t1"]
        self.assertEqual((n, seen), (2.0, 0.0))        # both sides graded, neither filled
        self.assertGreater(pred, 0.0)
        # an hour of exposure at the touch, behind a thin line
        # (500 ahead of a 1,000 Target Size is a deep line)
        self.assertAlmostEqual(self.tv.cells[("t1", "BUY", "touch", "deep")][0], 3600.0)

    def test_an_old_book_accrues_no_exposure(self):
        self.tv.check_spots(T0 + SPOT_H_S / 2, slow=False)   # the book is from T0
        self.assertEqual(self.tv.cells.get(("t1", "BUY", "touch", "deep")), None)

    def test_a_fills_loss_is_read_an_hour_later(self):
        self.fam.put(A, [(0.49, 800.0)], [(0.50, 300.0)], T0 + 5)
        self.tv.check_spots(T0 + 5, slow=False)
        self.tf.cur[A] = {"mid": 0.46, "spread": 0.02}
        self.tv.grade_marks(T0 + 5 + 600)
        self.tf.cur[A] = {"mid": 0.45, "spread": 0.02}
        self.tv.grade_marks(T0 + 5 + 3600)
        self.assertAlmostEqual(self.tv.loss_t[("t1", "BUY", 600)].mean(), 0.04)
        self.assertAlmostEqual(self.tv.loss_t[("t1", "BUY", 3600)].mean(), 0.05)
        self.assertEqual(self.tv.marks, [])


class TestTheMeasuredNumbers(unittest.TestCase):
    def test_the_hazard_starts_from_a_prior_worth_a_day(self):
        fam, tf, tv = make()
        key = ("t1", "BUY", "touch", "thin")
        self.assertAlmostEqual(tv.hazard(key), HAZ_PRIOR["touch"])
        tv.cells[key] = [DAY_S, 2.0]
        self.assertAlmostEqual(tv.hazard(key), (2.0 + HAZ_PRIOR["touch"]) / 2.0)

    def test_the_loss_a_share_from_its_prior_and_the_markets_own(self):
        fam, tf, tv = make()
        self.assertAlmostEqual(tv.loss_ps("t1", "BUY", A), LOSS_PRIOR)
        for _ in range(20):
            tv.loss_t.setdefault(("t1", "BUY", 3600), __import__(
                "v3.tiervalue", fromlist=["Tally"]).Tally()).add(0.06, T0)
        lt = (20 * 0.06 + LOSS_PRIOR_N * LOSS_PRIOR) / (20 + LOSS_PRIOR_N)
        self.assertAlmostEqual(tv.loss_ps("t1", "BUY", A), lt)
        # never under his floor
        fam2, tf2, tv2 = make(floor=lambda: 0.05)
        self.assertAlmostEqual(tv2.loss_ps("t1", "BUY", A), 0.05)


class TestThePayCheck(unittest.TestCase):
    def test_only_posted_markets_and_only_since_the_program_began(self):
        claims = {f"2026-09-25|{A}": 10.0, f"2026-09-25|{C}": 5.0,
                  f"2026-09-26|{A}": 8.0, f"2026-09-20|{A}": 50.0}
        paid = {f"2026-09-25|{A}": 9.0, f"2026-09-20|{A}": 1.0}
        fam, tf, tv = make(pay=lambda: (claims, paid))
        fam.add(A, T1P, [(0.50, 800.0)], [(0.55, 800.0)])
        fam.add(C, T2P, [(0.30, 800.0)], [(0.33, 800.0)])
        p = tv._pay()
        self.assertEqual((p["t1"]["est"], p["t1"]["paid"], p["t1"]["ratio"]), (10.0, 9.0, 0.9))
        self.assertEqual(p["t1"]["unposted_est"], 8.0)
        self.assertEqual(p["t2"]["markets"], 0)
        self.assertEqual(p["t2"]["unposted_est"], 5.0)


class TestTheSave(unittest.TestCase):
    def test_what_it_measured_comes_back(self):
        fam, tf, tv = make()
        tv.cells[("t2", "SELL", "near", "deep")] = [1234.5, 3.0]
        from v3.tiervalue import Tally
        tv.loss_t[("t2", "SELL", 3600)] = Tally(4.0, 0.12, T0)
        tv.loss_m[f"{C}|SELL"] = Tally(1.0, 0.03, T0)
        tv.grades["t2"] = [5.0, 1.2, 2.0, 0.7]
        d = json.loads(json.dumps(tv.to_dict()))
        _f, _t, tv2 = make()
        tv2.restore(d)
        self.assertEqual(tv2.cells, tv.cells)
        self.assertAlmostEqual(tv2.loss_t[("t2", "SELL", 3600)].mean(), 0.03)
        self.assertAlmostEqual(tv2.loss_m[f"{C}|SELL"].mean(), 0.03)
        self.assertEqual(tv2.grades["t2"], [5.0, 1.2, 2.0, 0.7])


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
        mon = Monitor()
        self.assertIsNotNone(mon.tiervalue)
        self.assertIs(mon.tierfair.value, mon.tiervalue)
        pol = mon.families["politics"]
        pol.universe[A] = {"event_n": 2, "name": "Georgia Senate — dem"}
        pol.terms.current[A] = Program(pool=1125.0, target=25000.0, df=0.3, status="active",
                                       pid=T1P, event_n=2)
        pol.cache.put(A, Book(bids=((0.50, 30000.0),), asks=((0.53, 30000.0),), tick=0.01,
                              fetched_at=time.time()))
        now = time.time()
        mon.tierfair.tick(now)
        mon.tiervalue.tick(now)
        mon.tierfair._freeze(now)
        j = json.loads(mon.tiers_json())
        self.assertTrue(j["value"]["ok"])
        self.assertIn("val", j["rows"][0])
        st = mon._state(now, {})
        self.assertIn("tiervalue", st)
        self.assertEqual(mon.tiervalue.coc(), mon.focus.coc_day)


HARNESS = r"""
const el = () => ({style:{}, innerHTML:'', textContent:'', appendChild(){}, remove(){}});
global.window = {scrollY:0, addEventListener(){}, scrollTo(){}};
global.document = {getElementById: () => el(), createElement: () => el(), body:{appendChild(){}}};
global.esc = s => String(s == null ? '' : s);
global.usd = x => '$' + Number(x || 0).toFixed(2);
global.pc = x => Math.round(Number(x || 0) * 100) + 'c';
global.hdrs = () => ({});
global.fetch = () => ({then: function(){ return this; }, catch: function(){ return this; }});
global.setInterval = () => 0;
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
const t = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
window._t = t;
const open = {};
(t.tiers || []).forEach(tr => { open['t-' + tr.key] = true; });
(t.rows || []).forEach(r => { open['m-' + r.market] = true; });
window._tOpen = open;
try {
  const out = tRender(t);
  if (!out.includes('What $1,000 would earn') || !out.includes('stage 2:')) throw new Error('no stage 2');
  if (!out.includes('best bid $5')) throw new Error('no row value');
  console.log('OK');
} catch (e) { console.log('THREW: ' + e.message); process.exit(1); }
"""


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class TestThePageRenders(unittest.TestCase):
    def test_the_tiers_page_shows_stage_two(self):
        from v3.web import TIERS_JS
        fam, tf, tv = make()
        tf.value = tv
        fam.add(A, T1P, [(0.50, 800.0)], [(0.55, 800.0)])
        fam.add(C, T2P, [(0.30, 2000.0)], [(0.33, 2000.0)], pool=450.0, target=1500.0)
        tf.tick(T0)
        tv.tick(T0)
        tf._freeze(T0)
        with tempfile.TemporaryDirectory() as td:
            hp, jp, pp = (os.path.join(td, n) for n in ("h.js", "p.js", "p.json"))
            for path, mode, data in ((hp, "w", HARNESS), (jp, "w", TIERS_JS),
                                     (pp, "wb", tf.payload_json)):
                with open(path, mode) as f:
                    f.write(data)
            r = subprocess.run(["node", hp, jp, pp], capture_output=True, text=True, timeout=20)
            self.assertIn("OK", r.stdout + r.stderr, r.stdout + r.stderr)


class TestItIsLightEnough(unittest.TestCase):
    def test_six_thousand_tier_four_markets_plan_in_seconds(self):
        rnd = random.Random(3)
        fam, tf, tv = make()
        pid = "politics_t4_coverage_20260924"
        for i in range(6000):
            mid = rnd.uniform(0.05, 0.95)
            bids = [(round(mid - 0.01 * k - 0.01, 2), float(rnd.choice((10, 200, 1500))))
                    for k in range(rnd.randint(1, 8))]
            asks = [(round(mid + 0.01 * k + 0.01, 2), float(rnd.choice((10, 200, 1500))))
                    for k in range(rnd.randint(1, 8))]
            fam.add(f"vmc-x-{i}", pid, [b for b in bids if b[0] > 0],
                    [a for a in asks if a[0] < 1], pool=2.0, target=2000.0, df=0.4)
        t = time.time()
        tv.plan(("t4", "t4c"), T0)
        self.assertLess(time.time() - t, 20.0)


if __name__ == "__main__":
    unittest.main()
