"""Stage 3 of the tier engines: paper trading (owner, 2026-09-26 "We need
to go back to building the bigger 4 tier earning machine").

Read-only: an engine per tier decides what it would rest, fills its
paper orders off the real tape, holds positions, rests one exit per
position and keeps its books; a paper twin of every real tender order is
scored against what really filled. Nothing here touches an order.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

from v3.family import FamilyOrder
from v3.programs import Program
from v3.scoring import Book
from v3.tests.test_tiervalue import A, B, C, T1P, T2P, T0, Fam
from v3.tierfair import TierFair
from v3.tierpaper import (ACTIONS_PER_MIN, AVG_HALF_S, HOLD_PRIOR_S, SHADOW_GRACE_S,
                          AvgBooks, TierPaper)
from v3.tiervalue import DAY_S, MARKET_CAP_FRAC, POT_USD, TierValue, side_share


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def rig(prints=None, tender=()):
    fam = Fam()
    clk = Clock()
    tf = TierFair(fam, clock=clk)
    tv = TierValue(tf, fam, prints=prints, clock=clk)
    tp = TierPaper(tv, tf, fam, tender_ids=lambda: set(tender), clock=clk)
    return fam, tf, tv, tp, clk


def fund(tp, t1=500.0, t2=300.0):
    """Give the tiers money by hand (the split's own test is apart)."""
    tp.shares = {"t1": 0.5, "t2": 0.3, "t3": 0.2, "t4": 0.0, "t4c": 0.0}
    tp.split_day = "2026-09-26"
    tp.tiers["t1"].money = t1
    tp.tiers["t2"].money = t2


class TestTheAveragedBook(unittest.TestCase):
    def test_a_level_there_part_of_the_time_counts_for_that_part(self):
        ab = AvgBooks()
        bids_on = [(0.50, 1000.0), (0.49, 1000.0)]
        bids_off = [(0.49, 1000.0)]
        ab.update("m", bids_on, [], T0)
        t = T0
        for k in range(1800):                         # on half the time, for an hour
            t += 2.0
            ab.update("m", bids_on if k % 2 else bids_off, [], t)
        lv = dict(ab.levels("m", "BUY", bids_on))
        self.assertAlmostEqual(lv[0.49], 1000.0, delta=1.0)
        self.assertAlmostEqual(lv[0.50], 500.0, delta=30.0)

    def test_nothing_better_than_the_live_touch(self):
        ab = AvgBooks()
        ab.update("m", [(0.55, 1000.0)], [], T0)
        ab.update("m", [(0.50, 1000.0)], [], T0 + 10)
        lv = ab.levels("m", "BUY", [(0.50, 1000.0)])
        self.assertEqual([p for p, _q in lv], [0.50])

    def test_the_half_life(self):
        ab = AvgBooks()
        ab.update("m", [(0.50, 1000.0)], [], T0)
        ab.update("m", [(0.50, 0.0)], [], T0 + AVG_HALF_S)
        self.assertAlmostEqual(dict(ab.levels("m", "BUY", []))[0.50], 500.0)


class TestTheMoney(unittest.TestCase):
    def test_each_tier_gets_its_value_over_the_sum_once_a_day(self):
        fam, tf, tv, tp, clk = rig()
        tv._view = {"ok": True, "tiers": {"t1": {"alone": {"value": 30.0}},
                                          "t2": {"alone": {"value": 10.0}},
                                          "t3": {"alone": {"value": 0.0}},
                                          "t4": {"alone": {"value": -2.0}},
                                          "t4c": {"alone": {"value": 0.0}}}}
        tp._split(T0)
        self.assertAlmostEqual(tp.tiers["t1"].money, 750.0)
        self.assertAlmostEqual(tp.tiers["t2"].money, 250.0)
        self.assertEqual(tp.tiers["t4"].money, 0.0)
        # the values move; the money stands until midnight ET
        tv._view["tiers"]["t2"]["alone"]["value"] = 90.0
        tp._split(T0 + 3600)
        self.assertAlmostEqual(tp.tiers["t1"].money, 750.0)
        tp._split(T0 + DAY_S)
        self.assertAlmostEqual(tp.tiers["t2"].money, 750.0)

    def test_nothing_runs_before_stage_two_has_planned(self):
        fam, tf, tv, tp, clk = rig()
        fam.add(A, T1P, [(0.50, 800.0)], [(0.55, 800.0)])
        tp.tick(T0)
        self.assertEqual(tp.shares, {})
        self.assertEqual(tp.tiers["t1"].orders, {})


class TestTheEntries(unittest.TestCase):
    def setUp(self):
        self.fam, self.tf, self.tv, self.tp, self.clk = rig()
        self.fam.add(A, T1P, [(0.50, 800.0)], [(0.55, 800.0)], pool=400.0)
        self.fam.add(B, T1P, [(0.40, 800.0)], [(0.44, 800.0)], pool=400.0)
        fund(self.tp)

    def test_it_rests_within_its_money_and_the_market_cap(self):
        self.tp.tick(T0)
        pt = self.tp.tiers["t1"]
        self.assertTrue(pt.orders)
        per = {}
        for o in pt.orders.values():
            c = o["qty"] * (o["px"] if o["side"] == "BUY" else 1 - o["px"])
            per[o["slug"]] = per.get(o["slug"], 0.0) + c
            # never at or through the other side
            book = self.fam.cache.any_age(o["slug"])
            if o["side"] == "BUY":
                self.assertLess(o["px"], book.asks[0][0])
            else:
                self.assertGreater(o["px"], book.bids[0][0])
        self.assertLessEqual(sum(per.values()), pt.money + 1e-6)
        self.assertTrue(all(v <= MARKET_CAP_FRAC * POT_USD + 1e-6 for v in per.values()))

    def test_a_first_day_market_gets_nothing(self):
        self.fam.terms.joined_at[A] = T0 - 60.0
        self.fam.terms.joined_at[B] = T0 - 60.0
        self.tp.tick(T0)
        self.assertEqual(self.tp.tiers["t1"].orders, {})

    def test_the_reward_it_claims_accrues_by_the_exchanges_arithmetic(self):
        self.tp.tick(T0)
        pt = self.tp.tiers["t1"]
        self.clk.t = T0 + 60
        for s in (A, B):
            b = self.fam.cache.any_age(s)
            self.fam.put(s, b.bids, b.asks, T0 + 60)
        want = 0.0
        by = {}
        for o in pt.orders.values():
            by.setdefault((o["slug"], o["side"]), {})[o["px"]] = o["qty"]
        for (s, sd), ours in by.items():
            b = self.fam.cache.any_age(s)
            lv = self.tp.avg.levels(s, sd, b.bids if sd == "BUY" else b.asks)
            sh, _ok, _g = side_share(sd, lv, 0.01, 0.5, 1000.0, ours)
            want += 400.0 / 2 / 2 * sh * 60 / DAY_S
        r0 = pt.day["reward"]
        self.tp.tick(T0 + 60)
        self.assertAlmostEqual(pt.day["reward"] - r0, want, places=6)

    def test_a_market_leaving_the_tier_takes_its_entries(self):
        self.tp.tick(T0)
        pt = self.tp.tiers["t1"]
        self.assertTrue(any(o["slug"] == A for o in pt.orders.values()))
        del self.fam.terms.current[A]
        self.tf._mk = None
        self.clk.t = T0 + 30
        self.tf.tick(T0 + 30)
        self.tp.tick(T0 + 30)
        self.assertFalse(any(o["slug"] == A for o in pt.orders.values()))


class TestFillsPositionsAndExits(unittest.TestCase):
    def setUp(self):
        self.fam, self.tf, self.tv, self.tp, self.clk = rig()
        # both sides hold their 1,000 Target Size
        self.fam.add(A, T1P, [(0.50, 1200.0)], [(0.55, 1200.0)], pool=400.0)
        fund(self.tp)
        self.tp.tick(T0)
        self.pt = self.tp.tiers["t1"]
        self.bid = next(o for o in self.pt.orders.values() if o["side"] == "BUY")

    def test_a_crossed_bid_is_a_long_with_one_exit_sized_to_it(self):
        px, q = self.bid["px"], self.bid["qty"]
        self.fam.put(A, [(0.45, 800.0)], [(px, 300.0), (0.55, 800.0)], T0 + 5)
        self.tf.cur[A] = {"mid": 0.48, "spread": 0.03, "fair": 0.48}
        self.tp.tick(T0 + 5)
        self.assertAlmostEqual(self.pt.pos[A][0], q)
        self.assertAlmostEqual(self.pt.pos[A][1], px)
        self.assertEqual(self.pt.day["fills"], 1)
        # next look: one exit, the whole lot, on the ask side
        self.fam.put(A, [(0.45, 800.0)], [(0.52, 300.0), (0.55, 800.0)], T0 + 6)
        self.tp.tick(T0 + 6)
        exits = [o for o in self.pt.orders.values() if o["kind"] == "exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["side"], "SELL")
        self.assertAlmostEqual(exits[0]["qty"], q)
        # and no new bid adds to it past the tier's money
        held = self.pt.collateral_held()
        entries = sum(o["qty"] * o["px"] for o in self.pt.orders.values()
                      if o["kind"] == "entry" and o["side"] == "BUY")
        self.assertLessEqual(held + entries, self.pt.money + 1e-6)

    def test_the_lot_is_never_offered_twice(self):
        # an ask entry rests beside the bid; the bid fills; the ask side is
        # the exit's now, so the entry there comes off and only the exit
        # offers the lot
        self.assertTrue(any(o["side"] == "SELL" and o["kind"] == "entry"
                            for o in self.pt.orders.values()))
        px = self.bid["px"]
        self.fam.put(A, [(0.45, 800.0)], [(px, 300.0), (0.55, 800.0)], T0 + 5)
        for k in range(5, 40):
            if k > 5:
                self.fam.put(A, [(0.45, 800.0)], [(0.52, 300.0), (0.55, 1200.0)], T0 + k)
            self.tp.tick(T0 + k)
            asks = [o for o in self.pt.orders.values() if o["side"] == "SELL"]
            self.assertTrue(all(o["kind"] == "exit" for o in asks), asks)
            self.assertAlmostEqual(sum(o["qty"] for o in asks), self.pt.pos[A][0])

    def test_the_exit_filling_closes_the_lot_and_books_the_gain(self):
        px, q = self.bid["px"], self.bid["qty"]
        self.fam.put(A, [(0.45, 800.0)], [(px, 300.0), (0.55, 800.0)], T0 + 5)
        self.tp.tick(T0 + 5)
        self.fam.put(A, [(0.45, 800.0)], [(0.52, 300.0), (0.55, 800.0)], T0 + 6)
        self.tp.tick(T0 + 6)
        ex = next(o for o in self.pt.orders.values() if o["kind"] == "exit")
        self.fam.put(A, [(ex["px"], 900.0)], [(0.60, 300.0)], T0 + 7)
        self.tp.tick(T0 + 7)
        self.assertNotIn(A, self.pt.pos)
        self.assertAlmostEqual(self.pt.day["realized"], q * (ex["px"] - px), places=6)

    def test_a_short_and_its_cover(self):
        pt = self.pt
        pt.orders.clear()
        pt.orders["x"] = {"slug": A, "side": "SELL", "px": 0.60, "qty": 10.0, "t0": T0,
                          "kind": "entry", "ahead": 0.0, "tseen": 0.0}
        self.tp._fill(pt, "x", pt.orders["x"], T0)
        self.assertEqual(pt.pos[A], [-10.0, 0.60])
        self.assertAlmostEqual(pt.collateral_held(), 4.0)
        pt.orders["y"] = {"slug": A, "side": "BUY", "px": 0.55, "qty": 10.0, "t0": T0,
                          "kind": "exit", "ahead": 0.0, "tseen": 0.0}
        self.tp._fill(pt, "y", pt.orders["y"], T0)
        self.assertNotIn(A, pt.pos)
        self.assertAlmostEqual(pt.day["realized"], 0.5)

    def test_a_fill_is_graded_for_its_loss_like_a_paper_spot(self):
        px = self.bid["px"]
        self.fam.put(A, [(0.45, 800.0)], [(px, 300.0), (0.55, 800.0)], T0 + 5)
        self.tp.tick(T0 + 5)
        self.assertTrue(any(m["slug"] == A and m["tier"] == "t1" for m in self.tv.marks))


class TestTheCostOfMoving(unittest.TestCase):
    def test_a_side_stays_unless_the_new_orders_are_worth_the_move(self):
        fam, tf, tv, tp, clk = rig()
        fam.add(A, T1P, [(0.50, 800.0)], [(0.55, 800.0)], pool=400.0)
        fund(tp)
        tp.tick(T0)
        pt = tp.tiers["t1"]
        before = {k: dict(v) for k, v in pt.orders.items()}
        # a ten-share change of someone else's size a tick back: the
        # plan moves by a hair, not worth a place and a cancel
        fam.put(A, [(0.50, 800.0), (0.49, 10.0)], [(0.55, 800.0)], T0 + 1)
        tp.tick(T0 + 1)
        self.assertEqual(set(pt.orders), set(before))

    def test_no_more_than_the_action_budget_a_minute(self):
        fam, tf, tv, tp, clk = rig()
        for i in range(40):
            fam.add(f"ushrewc-ushr-x{i}-2026-11-03-dem", T1P, [(0.50, 800.0)], [(0.55, 800.0)],
                    pool=400.0)
        fund(tp, t1=4000.0)
        tp.tick(T0)
        pt = tp.tiers["t1"]
        self.assertLessEqual(len(pt.acts), ACTIONS_PER_MIN)
        self.assertGreater(pt.day["skipped"], 0)
        self.assertGreater(pt.act_price, 0.002)       # the budget binding prices an action


class TestTheRealOrdersTwins(unittest.TestCase):
    def setUp(self):
        self.pr = {}
        self.fam, self.tf, self.tv, self.tp, self.clk = rig(
            prints=lambda s: self.pr.get(s), tender=("R1",))
        self.fam.add(A, T1P, [(0.50, 500.0), (0.49, 800.0)], [(0.52, 800.0)])
        fund(self.tp)
        self.fam.orders["R1"] = FamilyOrder(id="R1", market=A, side="BUY", price=0.49,
                                            qty=40.0, intent="ORDER_INTENT_BUY_LONG",
                                            placed_ts=T0, purpose="focus")
        self.fam.put(A, [(0.50, 500.0), (0.49, 840.0)], [(0.52, 800.0)], T0)
        self.fam.fills = []
        self.tp._shadows(T0, self.tv._ours())

    def test_both_filled(self):
        self.fam.put(A, [(0.48, 800.0)], [(0.49, 300.0)], T0 + 5)
        self.tp._shadows(T0 + 5, self.tv._ours())
        del self.fam.orders["R1"]
        self.fam.fills.append({"ts": T0 + 5, "oid": "R1", "market": A, "side": "BUY",
                               "qty": 40.0, "px": 0.49})
        self.tp._shadows(T0 + 6, self.tv._ours())
        self.tp._shadows(T0 + 6 + SHADOW_GRACE_S, self.tv._ours())
        self.assertEqual(self.tp.shadow_tally["t1"]["both"], 1)

    def test_the_real_order_filled_where_the_tape_rule_saw_nothing(self):
        del self.fam.orders["R1"]
        self.fam.fills.append({"ts": T0 + 5, "oid": "R1", "market": A, "side": "BUY",
                               "qty": 40.0, "px": 0.49})
        self.tp._shadows(T0 + 6, self.tv._ours())
        self.tp._shadows(T0 + 6 + SHADOW_GRACE_S, self.tv._ours())
        self.assertEqual(self.tp.shadow_tally["t1"]["real_only"], 1)

    def test_the_twin_filled_and_the_real_order_did_not(self):
        self.fam.put(A, [(0.48, 800.0)], [(0.49, 300.0)], T0 + 5)
        self.tp._shadows(T0 + 5, self.tv._ours())
        del self.fam.orders["R1"]                          # cancelled, no fill booked
        self.tp._shadows(T0 + 6, self.tv._ours())
        self.tp._shadows(T0 + 6 + SHADOW_GRACE_S, self.tv._ours())
        self.assertEqual(self.tp.shadow_tally["t1"]["paper_only"], 1)

    def test_our_own_real_order_is_not_counted_ahead_of_its_twin(self):
        sh = self.tp.shadows["R1"]
        self.assertEqual(sh["ahead"], 800.0)               # the 840 less its own 40


class TestTheDay(unittest.TestCase):
    def test_midnight_writes_the_day_down(self):
        fam, tf, tv, tp, clk = rig()
        fam.add(A, T1P, [(0.50, 800.0)], [(0.55, 800.0)], pool=400.0)
        fund(tp)
        tp.tick(T0)
        pt = tp.tiers["t1"]
        pt.day["reward"] = 3.0
        pt.day["capital"] = 0.5
        pt.day["realized"] = -1.0
        day0 = pt.day["day"]
        tp._roll_day(pt, T0 + DAY_S)
        self.assertEqual(pt.days[-1]["day"], day0)
        self.assertAlmostEqual(pt.days[-1]["net"], 1.5)
        self.assertNotEqual(pt.day["day"], day0)


class TestTheSave(unittest.TestCase):
    def test_it_comes_back_as_it_was(self):
        fam, tf, tv, tp, clk = rig()
        fam.add(A, T1P, [(0.50, 800.0)], [(0.55, 800.0)], pool=400.0)
        fund(tp)
        tp.tick(T0)
        pt = tp.tiers["t1"]
        pt.pos[A] = [12.0, 0.5]
        pt.hold[(A, "BUY")] = 900.0
        tp.shadow_tally["t1"] = {"both": 2, "paper_only": 1, "real_only": 0, "neither": 3,
                                 "lag_s": 4.0}
        d = json.loads(json.dumps(tp.to_dict()))
        _f, _t, _v, tp2, _c = rig()
        tp2.restore(d)
        self.assertEqual(tp2.shares, tp.shares)
        self.assertEqual(tp2.tiers["t1"].orders, pt.orders)
        self.assertEqual(tp2.tiers["t1"].pos, {A: [12.0, 0.5]})
        self.assertEqual(tp2.tiers["t1"].hold[(A, "BUY")], 900.0)
        self.assertEqual(tp2.tiers["t1"].money, pt.money)
        self.assertEqual(tp2.shadow_tally["t1"]["neither"], 3)


class TestItTouchesNothing(unittest.TestCase):
    def test_a_full_run_never_reaches_the_desk(self):
        fam, tf, tv, tp, clk = rig()
        fam.add(A, T1P, [(0.50, 800.0)], [(0.55, 800.0)], pool=400.0)
        fam.add(C, T2P, [(0.30, 2000.0)], [(0.33, 2000.0)], pool=450.0, target=1500.0)
        for k in range(30):
            clk.t = T0 + k
            fam.put(A, [(0.50, 800.0)], [(0.55, 800.0)], T0 + k)
            tf.tick(T0 + k)
            tv.tick(T0 + k)
            tp.tick(T0 + k)                          # the fixture's desk raises if touched
        self.assertTrue(tp.view()["ok"])


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
        self.assertIsNotNone(mon.tierpaper)
        self.assertIs(mon.tierfair.paper, mon.tierpaper)
        pol = mon.families["politics"]
        pol.universe[A] = {"event_n": 2, "name": "Georgia Senate — dem"}
        pol.terms.current[A] = Program(pool=1125.0, target=25000.0, df=0.3, status="active",
                                       pid=T1P, event_n=2)
        pol.cache.put(A, Book(bids=((0.50, 30000.0),), asks=((0.53, 30000.0),), tick=0.01,
                              fetched_at=time.time()))
        now = time.time()
        mon.tierfair.tick(now)
        mon.tiervalue.tick(now)
        mon.tierpaper.tick(now)
        mon.tierfair._freeze(now)
        j = json.loads(mon.tiers_json())
        self.assertIn("paper", j)
        self.assertIn("tierpaper", mon._state(now, {}))


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
window._tOpen = open;
try {
  const out = tRender(t);
  if (!out.includes('Paper trading') || !out.includes('stage 3 (paper)')) throw new Error('no stage 3');
  if (!out.includes('paper orders')) throw new Error('no paper list');
  console.log('OK');
} catch (e) { console.log('THREW: ' + e.message); process.exit(1); }
"""


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class TestThePageRenders(unittest.TestCase):
    def test_the_tiers_page_shows_stage_three(self):
        from v3.web import TIERS_JS
        fam, tf, tv, tp, clk = rig()
        tf.value, tf.paper = tv, tp
        fam.add(A, T1P, [(0.50, 800.0)], [(0.55, 800.0)], pool=400.0)
        tf.tick(T0)
        tv.tick(T0)
        tp.tick(T0)
        tp.tiers["t1"].pos[B] = [5.0, 0.4]
        tf._freeze(T0)
        with tempfile.TemporaryDirectory() as td:
            hp, jp, pp = (os.path.join(td, n) for n in ("h.js", "p.js", "p.json"))
            for path, mode, data in ((hp, "w", HARNESS), (jp, "w", TIERS_JS),
                                     (pp, "wb", tf.payload_json)):
                with open(path, mode) as f:
                    f.write(data)
            r = subprocess.run(["node", hp, jp, pp], capture_output=True, text=True, timeout=20)
            self.assertIn("OK", r.stdout + r.stderr, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
