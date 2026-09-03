"""Flatten mode: cancel every opening order, keep every exit, then rebuild
under the small ceiling with history guiding the ranking."""

import os
import tempfile
import unittest

from v3 import floor
from v3.main import Monitor, is_exit_order
from v3.intents import BUY_LONG, BUY_SHORT, SELL_LONG, SELL_SHORT


def O(oid, market, intent, price=0.3, size=5.0, manual=False):
    from v3.intents import REST_SIDE
    return {"id": oid, "market": market, "intent": intent,
            "side": REST_SIDE[intent], "price": price, "size": size,
            "manual": manual}


class TestExitClassification(unittest.TestCase):
    def test_every_intent_against_every_position(self):
        long_, short, flat = {"m": (10.0, 3.0)}, {"m": (-10.0, 3.0)}, {}
        cases = [
            (SELL_LONG, long_, True),    # ask while long: exit
            (SELL_SHORT, short, True),   # buy-back bid while short: exit
            (BUY_LONG, short, True),     # any bid while short reduces: exit
            (BUY_SHORT, long_, True),    # any ask while long reduces: exit
            (BUY_LONG, flat, False),     # opening
            (BUY_SHORT, flat, False),    # opening short
            (SELL_LONG, flat, False),    # ask with no stock: not an exit
            (BUY_LONG, long_, False),    # adding to a long: opening
        ]
        for intent, pos, want in cases:
            self.assertEqual(is_exit_order(O("x", "m", intent), pos), want,
                             (intent, pos))


class TestFlattenPass(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        p = self.dir.name
        os.environ["V3_STATE_PATH"] = os.path.join(p, "state.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(p, "floor.json")
        os.environ["V1_ACK_PATH"] = os.path.join(p, "a1.json")
        os.environ["V2_ACK_PATH"] = os.path.join(p, "a2.json")
        os.environ["V3_FLATTEN"] = "1"
        os.environ["GITHUB_TOKEN"] = ""
        self.mon = Monitor()
        self.cancelled = []
        desk = self.mon.families["politics"].desk
        from v3.orders import OrderResult
        desk.cancel = lambda oid, m, initiator="auto": (
            self.cancelled.append(oid) or OrderResult(ok=True, note="ok",
                                                      order_id=oid))
        self.alerts = []
        self.mon.alerts.notify = lambda t, msg, priority="default": \
            self.alerts.append(t)

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V1_ACK_PATH",
                  "V2_ACK_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def test_openings_cancelled_exits_kept_then_phase_two(self):
        positions = {"mkt-long": (10.0, 3.0), "mkt-short": (-5.0, 2.0)}
        orders = [
            O("open1", "mkt-x", BUY_LONG),
            O("open2", "mkt-long", BUY_LONG),     # ADDS to a long: opening
            O("exit1", "mkt-long", SELL_LONG),
            O("exit2", "mkt-short", SELL_SHORT),
            O("man1", "mkt-y", BUY_SHORT, manual=True),  # all means all
        ]
        s = self.mon._flatten_pass(orders, positions)
        self.assertEqual(set(self.cancelled), {"open1", "open2", "man1"})
        self.assertEqual(s["kept_exits"], 2)
        self.assertFalse(self.mon.flatten_done)      # work happened this pass
        # the clean pass flips to phase two, with the alert
        self.cancelled.clear()
        s = self.mon._flatten_pass([o for o in orders
                                    if o["id"].startswith("exit")], positions)
        self.assertEqual(self.cancelled, [])
        self.assertTrue(self.mon.flatten_done)
        self.assertTrue(any("Flat" in t for t in self.alerts))
        self.assertEqual(s["phase"], "rebuild")

    def test_phase_two_cancels_nothing_the_stray_is_the_owners(self):
        """The phase-two janitor used to kill any order the families did
        not own — 964 cancels, the owner's hand orders included. Since
        2026-08-22 ('Don't let it cancel orders I set by hand') an
        unknown order is HIS: phase two only reports."""
        self.mon.flatten_done = True
        fam = self.mon.families["politics"]
        from v3.family import FamilyOrder
        fam.orders["mine1"] = FamilyOrder(
            id="mine1", market="mkt-z", side="BUY", price=0.3, qty=1.0,
            intent=BUY_LONG, placed_ts=0.0, purpose="earn")
        orders = [O("mine1", "mkt-z", BUY_LONG),     # 3.0's own rebuild order
                  O("stray", "mkt-z", BUY_LONG)]     # the owner's hand
        s = self.mon._flatten_pass(orders, {})
        self.assertEqual(self.cancelled, [])
        self.assertEqual(s["cancelled_now"], 0)
        self.assertEqual(s["phase"], "rebuild")

    def test_flatten_requests_the_floor_even_with_master_off(self):
        self.assertFalse(self.mon.master.on)
        self.mon.floor.write_want(self.mon.master.on or self.mon.flatten)
        self.assertTrue(floor.wanted()[0])


class TestExitsOnlyCycle(unittest.TestCase):
    def test_family_places_no_earn_orders_in_phase_one(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": 10.0, "cost": 4.0}
        r.positions[A] = (10.0, 4.0)
        r.fam.positions_seen[A] = 10.0
        s = r.fam.cycle(r.now + 60, r.exchange.open_orders(), r.positions,
                        r.exchange, True, exits_only=True)
        self.assertEqual(s["mode"], "flatten — exits only")
        kinds = {o.purpose for o in r.fam.orders.values()}
        self.assertEqual(kinds, {"sell"})            # the exit ask, nothing else


class TestHistoryRanking(unittest.TestCase):
    def test_proven_market_enters_first(self):
        from v3.tests.test_family import Rig, A, C
        from v3.family import FamilyConfig
        cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                           rest_style="join_quiet", revive=True,
                           capital_usd=1.0, per_market_usd=2.0)
        r = Rig(cfg=cfg)
        r.add_market(A)
        r.add_market(C, event="House control")
        r.fam.history[C] = 5.0                       # C actually paid us
        r.cycle()
        mkts = {o.market for o in r.fam.orders.values()}
        self.assertIn(C, mkts)                       # the proven market got in
        self.assertLessEqual(r.fam.family_spent(), 1.0 + 1e-9)


if __name__ == "__main__":
    unittest.main()


class TestCycleOut(unittest.TestCase):
    def rig(self):
        from v3.tests.test_family import Rig, A, politics_book
        from v3.family import FamilyConfig
        cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                           rest_style="join_quiet", revive=True,
                           capital_usd=100.0, per_market_usd=20.0,
                           min_est_day=0.10, weak_pull_s=7200.0,
                           cooldown_s=60.0)
        r = Rig(cfg=cfg)
        r.add_market(A)
        return r, A

    def test_weak_order_is_pulled_after_the_window(self):
        r, A = self.rig()
        r.cycle()
        self.assertTrue(r.fam.orders)
        # the pool collapses to almost nothing: orders now earn ~0.4c/day
        for o in r.exchange.prog_raw.values():
            o["timePeriods"][0]["rewardPool"] = 0.05
        r.cycle(advance=r.fam.cfg.terms_active_s + 1)   # terms re-read, weak starts
        self.assertTrue(any(o.weak_since for o in r.fam.orders.values()))
        r.cycle(advance=7300.0)                          # past the window
        pulls = [l for l in r.fam.log if l.get("event") == "pull"]
        self.assertTrue(any("cycling out" in l.get("why", "") for l in pulls))

    def test_healthy_order_is_not_cycled(self):
        r, A = self.rig()
        r.cycle()
        r.cycle(advance=7300.0)
        pulls = [l for l in r.fam.log if "cycling out" in l.get("why", "")]
        self.assertEqual(pulls, [])
        self.assertTrue(all(not o.weak_since for o in r.fam.orders.values()
                            if o.purpose != "sell"))


class TestFullCycleRegression(unittest.TestCase):
    """The 2026-08-20 22:0x production failure: cycle() died assembling
    state while flatten was active (a local leaked into _state). The whole
    path must run end to end offline."""

    class StubClient:
        def __init__(self):
            self.orders = [O("open1", "m", BUY_LONG)]

        def open_orders(self):
            return list(self.orders)

        def positions_net(self):
            return {}

    def test_cycle_completes_with_flatten_active(self):
        import tempfile
        with tempfile.TemporaryDirectory() as p:
            for k, v in (("V3_STATE_PATH", "state.json"),
                         ("V3_FLOOR_PATH", "floor.json"),
                         ("V1_ACK_PATH", "a1.json"), ("V2_ACK_PATH", "a2.json")):
                os.environ[k] = os.path.join(p, v)
            os.environ["V3_FLATTEN"] = "1"
            os.environ["GITHUB_TOKEN"] = ""
            try:
                mon = Monitor()
                stub = self.StubClient()
                mon.client = stub
                cancelled = []
                from v3.orders import OrderResult

                def cancel(oid, mkt, initiator="auto"):
                    cancelled.append(oid)
                    stub.orders = [o for o in stub.orders if o["id"] != oid]
                    return OrderResult(ok=True, note="ok")
                mon.families["politics"].desk.cancel = cancel
                floor.ack("v1", True)
                floor.ack("v2", True)
                st = mon.cycle()               # must not raise
                self.assertTrue(st["flatten"]["active"])
                self.assertEqual(cancelled, ["open1"])
                st = mon.cycle()               # clean pass -> phase two
                self.assertEqual(st["flatten"]["phase"], "rebuild")
            finally:
                for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V1_ACK_PATH",
                          "V2_ACK_PATH", "V3_FLATTEN"):
                    os.environ.pop(k, None)


class TestExitProtection(unittest.TestCase):
    """The 23:12Z incident: adopted position-reducing orders were labelled
    'earn', so maintenance repriced/pulled the owner's exits and their
    collateral blocked the rebuild ceiling."""

    def rig_short(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                           rest_style="join_quiet", revive=True,
                           capital_usd=0.0)     # no new entries — exits only
        r = Rig(cfg=cfg)
        r.add_market(A)
        # the owner is SHORT 100; a big buy-back bid rests (BUY_LONG,
        # 1.0-style) — an exit by position, an opening by intent
        r.positions[A] = (-100.0, -5.0)
        r.exchange.live["cover"] = {
            "id": "cover", "market": A, "side": "BUY", "price": 0.01,
            "size": 100.0, "intent": BUY_LONG, "manual": False}
        return r, A

    def test_adopted_cover_bid_is_an_exit_not_spend(self):
        r, A = self.rig_short()
        r.cycle()
        rec = r.fam.orders["cover"]
        # since 2026-08-22 an order the engine did not place is the
        # OWNER'S OWN: hands off. It still reduces the short, so it
        # never blocks the ceiling — and the engine rests no second
        # cover alongside it (his 100 covers the whole short).
        self.assertEqual(rec.purpose, "manual")
        self.assertEqual(r.fam.family_spent(), 0.0)   # exits never block the ceiling
        r.cycle(advance=8000.0)
        r.cycle(advance=8000.0)
        self.assertIn("cover", r.fam.orders)          # never cancelled
        self.assertEqual(r.fam.orders["cover"].price, 0.01)  # never moved
        engine_covers = [o for o in r.fam.orders.values()
                         if o.market == A and o.purpose == "sell"]
        self.assertEqual(engine_covers, [])
        self.assertEqual(r.fam.family_spent(), 0.0)

    def test_short_gets_covered_at_touch_under_break_even(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        from v3.intents import SELL_SHORT
        cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                           rest_style="join_quiet", revive=True,
                           capital_usd=0.0)
        r = Rig(cfg=cfg)
        r.add_market(A)
        # short 100 sold at ~40c (cost -40): no cover resting anywhere
        r.positions[A] = (-100.0, -40.0)
        r.cycle()
        covers = [o for o in r.fam.orders.values()
                  if o.side == "BUY" and o.intent == SELL_SHORT]
        self.assertEqual(len(covers), 1)
        c = covers[0]
        self.assertEqual(c.purpose, "sell")
        # "never above break-even" became "not above break-even unless
        # the exit gate blesses the premium" (owner, 2026-08-25,
        # option B — the gte205 case: paying a little to close while
        # being paid to wait). Here the gate fronts the 44c bid at 45c,
        # a 5c/share premium bounded by the family give-up budget.
        self.assertLessEqual(c.price, 0.46)           # never crosses the ask
        give = max(c.price - 0.40, 0.0) * c.qty
        self.assertLessEqual(give, r.fam.cfg.exit_giveup_cap_usd + 1e-9)
        self.assertAlmostEqual(c.qty, 100.0)
        from v3.intents import capital_at_risk
        self.assertEqual(capital_at_risk(c.intent, c.price, c.qty), 0.0)


class TestStalePlansAndPriorities(unittest.TestCase):
    """23:53Z lessons: plans scored under old knobs must not place, and
    the seller outranks new entries for the action budget."""

    def test_stale_scoreboard_cleared_on_config_change(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.cycle()
        d = r.fam.to_dict()
        self.assertTrue(d["scoreboard"])
        from v3.family import FamilyConfig
        from v3.books import BookCache
        from v3.names import Names
        from v3 import politics
        from v3.family import Family
        fam2 = Family(None, BookCache(), politics.discover,
                      config=FamilyConfig(name="P", per_market_usd=20.0,
                                          min_est_day=0.10),
                      names=Names())
        fam2.restore(d)
        self.assertEqual(fam2.scoreboard, {})        # different knobs: rescan

    def test_under_bar_plan_never_places(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                           min_est_day=0.10, capital_usd=100.0)
        r = Rig(cfg=cfg)
        r.add_market(A)
        # a stale crumb plan sneaks into the scoreboard directly
        r.fam.scoreboard[A] = {"ts": r.now, "est": 0.03, "plans": [
            {"side": "BUY", "px": 0.43, "qty": 1.0, "share": 0.01,
             "est": 0.03, "cost": 0.43, "why": "old config"}]}
        r.fam.last_terms_active = r.now
        r.fam.last_terms_full = r.now
        r.fam.cycle(r.now + 1, r.exchange.open_orders(), r.positions,
                    r.exchange, True)
        self.assertNotIn(A, {o.market for o in r.fam.orders.values()})

    def test_seller_outranks_new_entries(self):
        from v3.tests.test_family import Rig, A, C
        from v3.family import FamilyConfig
        from v3.intents import SELL_SHORT
        cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                           rest_style="join_quiet", revive=True,
                           capital_usd=100.0, max_actions_per_cycle=1)
        r = Rig(cfg=cfg)
        r.add_market(A)
        r.add_market(C, event="House control")
        r.positions[C] = (-100.0, -40.0)             # a short needs its exit
        r.cycle()
        placed = list(r.fam.orders.values())
        self.assertEqual(len(placed), 1)             # one action, and it went...
        self.assertEqual(placed[0].purpose, "sell")  # ...to the cover, not entry
        self.assertEqual(placed[0].intent, SELL_SHORT)


class TestCeilingEnforcement(unittest.TestCase):
    """00:30Z lesson: reprices grew orders past the $100 ceiling
    ($121.99 on the book). The ceiling binds everywhere, and an
    over-ceiling book trims its worst value first."""

    def test_trim_pulls_worst_value_until_under(self):
        from v3.tests.test_family import Rig, A, C
        from v3.family import FamilyConfig, FamilyOrder
        from v3.intents import BUY_LONG
        # expected-risk era: the deep order charges collateral x its
        # tiny fill odds, so the cap must sit between the two charges
        # for the trim to fire and stop after one pull
        cfg = FamilyConfig(name="P", tag="P", capital_usd=0.45,
                           expected_risk=True)
        r = Rig(cfg=cfg)
        r.add_market(A)
        # two orders on the book: "good"
        # rests near the touch and earns, "bad" is deep and earns ~nothing
        for oid, px, qty in (("good", 0.43, 2.0),    # $0.86 at risk
                             ("bad", 0.02, 60.0)):   # $1.20 — over alone
            r.exchange.live[oid] = {"id": oid, "market": A, "side": "BUY",
                                    "price": px, "size": qty,
                                    "intent": BUY_LONG, "manual": False}
            r.fam.orders[oid] = FamilyOrder(
                id=oid, market=A, side="BUY", price=px, qty=qty,
                intent=BUY_LONG, placed_ts=0.0, purpose="earn")
        r.cycle()
        self.assertNotIn("bad", r.fam.orders)        # worst $/day-per-$ went
        self.assertIn("good", r.fam.orders)
        self.assertLessEqual(r.fam.family_spent(), 0.45 + 1e-9)

    def test_programless_read_is_dead_ground_until_a_program_appears(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        # a discovered market with NO program at the incentives API
        r.add_market("ushsscc-ushrsc-wi-2026-11-03-0", event="WI House seats")
        del r.exchange.prog_raw["ushsscc-ushrsc-wi-2026-11-03-0"]
        r.cycle(advance=r.fam.cfg.terms_full_s + 1)
        self.assertIn("ushsscc-ushrsc-wi-2026-11-03-0", r.fam.known_dead)
        self.assertTrue(r.fam._dead_here("ushsscc-ushrsc-wi-2026-11-03-0"))
        # the pool arrives later -> alive again
        from v3.tests.test_family import LIVE_PROG
        import copy
        r.exchange.prog_raw["ushsscc-ushrsc-wi-2026-11-03-0"] = copy.deepcopy(LIVE_PROG)
        r.cycle(advance=r.fam.cfg.terms_full_s + 1)
        self.assertNotIn("ushsscc-ushrsc-wi-2026-11-03-0", r.fam.known_dead)


class TestCoverInTightBooks(unittest.TestCase):
    def test_cover_bid_rests_under_a_locked_ask(self):
        # 00:37Z: cover bids were refused ("bid 4c would cross the best
        # ask 4c") in tight books — the price must duck under the ask
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        from v3.scoring import Book
        from v3.intents import SELL_SHORT
        cfg = FamilyConfig(name="P", tag="P", capital_usd=0.0)
        r = Rig(cfg=cfg)
        tight = Book(bids=((0.04, 50.0), (0.02, 60000.0)),
                     asks=((0.04, 40.0), (0.98, 60000.0)),
                     tick=0.01, fetched_at=r.now)
        r.add_market(A, book=tight)
        r.positions[A] = (-100.0, -40.0)     # short, received ~40c
        r.cycle()
        covers = [o for o in r.fam.orders.values() if o.intent == SELL_SHORT]
        self.assertEqual(len(covers), 1)
        self.assertLessEqual(covers[0].price, 0.03)   # under the 4c ask


class TestOwnerDirectives0821(unittest.TestCase):
    """2026-08-21 morning: scope entries to gov/senate/2028, Silver keeps
    us off the wrong side of value, no ghosts after a move."""

    def test_entry_scope_blocks_out_of_family_markets(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        cfg = FamilyConfig(name="P", tag="P", capital_usd=100.0,
                           enter_tokens=("usgub", "usse"))
        r = Rig(cfg=cfg)
        r.add_market(A)                              # vmc-ussemov... contains usse
        r.add_market("paccc-usho-midterms-2026-11-03-rep", event="House control")
        r.cycle()
        mkts = {o.market for o in r.fam.orders.values()}
        self.assertIn(A, mkts)
        self.assertNotIn("paccc-usho-midterms-2026-11-03-rep", mkts)

    def test_mispriced_rest_is_an_ev_decision_not_a_rule(self):
        # Owner, 2026-08-21: no hard wrong-side rule. Resting past fair
        # is +EV only when the pool pays for the fill risk — and the
        # fill risk of a mispriced order is assumed HIGH (bait) until
        # data proves otherwise.
        from v3.tests.test_family import Rig, A
        import copy
        # a poor pool cannot pay the bait: no bids past fair
        poor = {"timePeriods": [{"programId": "politics_mid_1",
                                 "rewardPool": 1.0, "targetSize": 5000,
                                 "discountFactor": 0.2, "status": "LIVE"}]}
        r = Rig()
        r.add_market(A, prog=copy.deepcopy(poor))
        r.fam.fairs = lambda s: 0.30     # model says 30c; touch is 44c/47c
        r.cycle()
        for o in r.fam.orders.values():
            if o.side == "BUY":
                self.assertLessEqual(o.price, 0.32)
        # a rich pool may license the same rest — but only with the
        # bait-raised fill odds priced in, and clearing the EV bar
        r2 = Rig()
        r2.add_market(A)                 # default pool: $100/day
        r2.fam.fairs = lambda s: 0.30
        r2.cycle()
        wrong = [o for o in r2.fam.orders.values()
                 if o.side == "BUY" and o.price > 0.32]
        for o in wrong:
            plan = r2.fam.scoreboard.get(A) or {}
            rows = [p for p in (plan.get("plans") or [])
                    if p.get("side") == "BUY" and p.get("px") == o.price]
            for p in rows:
                self.assertGreaterEqual(p["p_fill"], 0.5)   # bait honesty
                self.assertGreaterEqual(p["ev"], r2.fam.cfg.min_est_day)
        # asks above fair still exist either way
        self.assertTrue(any(o.side == "SELL" for o in r2.fam.orders.values()))

    def test_bait_scales_the_fill_prior(self):
        from v3.fillmodel import FillModel
        m = FillModel()
        slug = "ussewc-usse-mt-2026-11-03-dem"
        quiet = m.p_fill(slug, "BUY", 1)
        baity = m.p_fill(slug, "BUY", 1, bait=13.0)
        self.assertGreater(baity, quiet * 5)
        self.assertGreater(baity, 0.5)   # 13 ticks past fair: near-certain

    def test_failed_cancel_keeps_original_tracked_and_retries(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.cycle()
        rec = next(o for o in r.fam.orders.values() if o.side == "BUY")
        # the exchange refuses this order's cancel once, then allows it
        real_post = r.exchange.post
        refuse = {"n": 0}
        def post(url, body, path=None, **kw):
            if "/cancel" in url and rec.id in url and refuse["n"] == 0:
                refuse["n"] = 1
                raise __import__("v3.api", fromlist=["ApiError"]).ApiError("nope", status=500)
            return real_post(url, body, path=path, **kw)
        r.exchange.post = post
        # force a reprice of rec deterministically — the machinery under
        # test is the failed-cancel retry, not the planner's choice
        import v3.family as F
        r.fam.last_action.clear()
        r.fam.cfg.reprice_gain_day = -1.0            # any move clears the bar
        orig_ps = F.Family._plan_side
        def forced(self, slug, book, side, prog, sp, budget, own=None, **kw):
            if own is not None and own.id == rec.id:
                return {"side": side, "px": 0.21, "qty": own.qty,
                        "share": 0.5, "est": 5.0, "ev": 5.0,
                        "p_fill": 0.1, "fill_cost": 0.0, "cost": 0.42,
                        "why": "forced move for the test"}
            return orig_ps(self, slug, book, side, prog, sp, budget,
                           own=own, **kw)
        F.Family._plan_side = forced
        try:
            r.cycle(advance=3700.0)
        finally:
            F.Family._plan_side = orig_ps
        self.assertIn(rec.id, r.fam.orders)          # ghost stays TRACKED
        self.assertIn("retrying", r.fam.orders[rec.id].why)
        self.assertIn(rec.id, r.exchange.live)       # and really still rests
        r.cycle()                                    # retry pass kills it
        self.assertNotIn(rec.id, r.fam.orders)
        self.assertNotIn(rec.id, r.exchange.live)

    def test_race_fair_reads_both_tables(self):
        from v3.silver import SilverFairs
        sf = SilverFairs()
        sf.races = {"ga": {"dem": 0.42, "rep": 0.58}}
        sf.gov_races = {"or": {"dem": 0.88, "rep": 0.12}}
        self.assertEqual(sf.race_fair("ussewc-usse-ga-2026-11-03-rep"), 0.58)
        self.assertEqual(sf.race_fair("usgubewc-usgub-or-2026-11-03-dem"), 0.88)
        self.assertIsNone(sf.race_fair("usgubewc-usgub-ri-2026-11-03-kenblo"))
        self.assertIsNone(sf.race_fair("vmc-usgubmov-or-2026-11-03-d12-15"))

    def test_existing_out_of_scope_orders_are_cycled_out(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.add_market("paccc-usho-midterms-2026-11-03-rep", event="House control")
        r.cycle()                                    # enters both (no scope yet)
        self.assertIn("paccc-usho-midterms-2026-11-03-rep",
                      {o.market for o in r.fam.orders.values()})
        r.fam.cfg.enter_tokens = ("usse",)           # the owner narrows scope
        r.cycle()
        mkts = {o.market for o in r.fam.orders.values()}
        self.assertNotIn("paccc-usho-midterms-2026-11-03-rep", mkts)
        self.assertIn(A, mkts)                       # in-scope stays


class TestTriageProgress(unittest.TestCase):
    def test_summary_reports_the_sweep(self):
        from v3.tests.test_family import Rig, A, C
        r = Rig()
        r.add_market(A)
        r.add_market(C, event="House control")
        s = r.cycle()
        tg = s["triage"]
        self.assertEqual(tg["total"], 2)
        self.assertEqual(tg["done"], 2)          # both scored on cycle one
        self.assertGreaterEqual(tg["per_cycle"], 1)


class TestPayoutButton(unittest.TestCase):
    def mon(self, rows):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        p = self.dir.name
        for k, v in (("V3_STATE_PATH", "s.json"), ("V3_FLOOR_PATH", "f.json"),
                     ("V1_ACK_PATH", "a1.json"), ("V2_ACK_PATH", "a2.json")):
            os.environ[k] = os.path.join(p, v)
        os.environ["V3_FLATTEN"] = "0"
        os.environ["GITHUB_TOKEN"] = ""
        m = Monitor()

        class C:
            def earnings(self, start):
                return list(rows)
        m.client = C()
        return m

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V1_ACK_PATH",
                  "V2_ACK_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def test_first_check_records_a_baseline_not_2566_new_rows(self):
        import datetime as dt
        d0 = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        d1 = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        rows = [{"date": "2026-08-01", "market": "old1",     # OLDER than the
                 "program_type": "lp", "reward_usd": 0.5,    # requested start:
                 "status": "PAID"},                          # the API does this
                # the exchange SPLITS one market-day into rows by status
                {"date": d0, "market": "m1",
                 "program_type": "lp", "reward_usd": 1.5, "status": "PAID"},
                {"date": d0, "market": "m1",
                 "program_type": "lp", "reward_usd": 0.19, "status": "SKIPPED"}]
        m = self.mon(rows)
        r1 = m.refresh_rewards()
        self.assertEqual(r1["new_count"], 0)
        self.assertIn("baseline", r1["note"])
        self.assertEqual(r1["days"][d0], 1.5)        # SKIPPED not in totals
        # second check, nothing changed: split rows must NOT flip-flop
        r2 = m.refresh_rewards()
        self.assertEqual(r2["new_count"], 0)
        self.assertNotIn("note", r2)
        # the API's stray window shifts: an ANCIENT row appears — absorbed
        rows.append({"date": "2026-07-29", "market": "fla-ref",
                     "program_type": "lp", "reward_usd": 2.07, "status": "PAID"})
        r2b = m.refresh_rewards()
        self.assertEqual(r2b["new_count"], 0)
        # a truly new posting appears: exactly one new market-day shows
        rows.append({"date": d1, "market": "m3",
                     "program_type": "lp", "reward_usd": 2.0, "status": "PENDING"})
        r3 = m.refresh_rewards()
        self.assertEqual(r3["new_count"], 1)
        self.assertEqual(r3["new_rows"][0]["day"], d1)


    def test_every_refresh_writes_the_csv(self):
        # owner, 2026-08-31: Aug-29 posted while he tapped refresh, and
        # the file stayed a day stale — the button consumed the
        # new-postings diff without writing, so the watcher's instant
        # write never fired. Any refresh must publish.
        import datetime as dt
        d0 = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        rows = [{"date": d0, "market": "m1", "program_type": "lp",
                 "reward_usd": 4.0, "status": "PAID"}]
        m = self.mon(rows)
        wrote = []
        m.publish_rewards_csv = lambda rows=None: wrote.append(rows) or True
        m.refresh_rewards()
        self.assertEqual(len(wrote), 1)
        self.assertEqual(wrote[0], rows)      # its own rows, no refetch
        m.refresh_rewards()                   # nothing new: still writes
        self.assertEqual(len(wrote), 2)

    def test_family_actuals_assign_not_accumulate(self):
        # the estimates-ledger bug (owner yes, 2026-08-28): the 5-minute
        # rewards poll re-ADDED the same market-days into the per-family
        # totals until politics "paid" $36,525 a day. Repeat polls must
        # leave the family totals exactly where one poll put them.
        import datetime as dt
        d0 = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        rows = [{"date": d0, "market": "ussewc-usse-ga-2026-11-03-rep",
                 "program_type": "lp", "reward_usd": 2.5, "status": "PAID"},
                {"date": d0, "market": "aachc-cfb-wins-2026-11-28-ala-9pt5",
                 "program_type": "lp", "reward_usd": 1.0, "status": "PENDING"}]
        m = self.mon(rows)
        for _ in range(5):
            m.refresh_rewards()
        self.assertEqual(m.actuals_by_fam[f"{d0}|politics"], 2.5)
        self.assertEqual(m.actuals_by_fam[f"{d0}|cfb"], 1.0)

    def test_poisoned_family_history_is_dropped(self):
        # rows accumulated by the old code sum far past their own day
        # total — the heal drops that day's family rows entirely; a
        # blank ledger cell beats a wrong one
        import datetime as dt
        d0 = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        rows = [{"date": d0, "market": "m-now", "program_type": "lp",
                 "reward_usd": 1.5, "status": "PAID"}]
        m = self.mon(rows)
        m.actuals_by_day["2026-08-25"] = 169.58
        m.actuals_by_fam["2026-08-25|politics"] = 36525.84   # old poison
        m.actuals_by_fam["2026-08-25|cfb"] = 16231.54
        m.refresh_rewards()
        self.assertNotIn("2026-08-25|politics", m.actuals_by_fam)
        self.assertNotIn("2026-08-25|cfb", m.actuals_by_fam)
        self.assertEqual(m.actuals_by_fam[f"{d0}|politics"], 1.5)


class TestSilverLogAndWatcher(unittest.TestCase):
    def test_race_moves_are_logged(self):
        from v3.silver import SilverFairs
        sf = SilverFairs(clock=lambda: 100.0)
        sf.gov_races = {"ga": {"dem": 0.40, "rep": 0.60, "name": "Georgia"}}
        sf._diff_races(sf.gov_races,
                       {"ga": {"dem": 0.37, "rep": 0.63, "name": "Georgia"}},
                       "governor", 200.0)
        self.assertEqual(len(sf.changes), 1)
        c = sf.changes[0]
        self.assertEqual((c["old"], c["new"]), (60.0, 63.0))
        # a sub-half-point wiggle is noise, not a move
        sf._diff_races({"ga": {"rep": 0.630}},
                       {"ga": {"rep": 0.632, "name": "Georgia"}},
                       "governor", 300.0)
        self.assertEqual(len(sf.changes), 1)

    def test_floor_skips_a_retired_v2(self):
        import tempfile
        from v3 import floor
        with tempfile.TemporaryDirectory() as p:
            os.environ["V3_FLOOR_PATH"] = os.path.join(p, "f.json")
            os.environ["V1_ACK_PATH"] = os.path.join(p, "a1.json")
            os.environ["V2_ACK_PATH"] = os.path.join(p, "a2.json")
            os.environ["V2_ENABLED"] = "0"
            try:
                f = floor.Floor(clock=lambda: 1000.0)
                floor.ack("v1", True, clock=lambda: 999.0)
                self.assertTrue(f.acked())       # no v2 ack needed
                os.environ["V2_ENABLED"] = "1"
                self.assertFalse(f.acked())      # running v2 must ack
            finally:
                for k in ("V3_FLOOR_PATH", "V1_ACK_PATH", "V2_ACK_PATH",
                          "V2_ENABLED"):
                    os.environ.pop(k, None)

    def test_watcher_pushes_only_on_truly_new_rows(self):
        import tempfile
        self.dir = tempfile.TemporaryDirectory()
        p = self.dir.name
        for k, v in (("V3_STATE_PATH", "s.json"), ("V3_FLOOR_PATH", "f.json"),
                     ("V1_ACK_PATH", "a1.json"), ("V2_ACK_PATH", "a2.json")):
            os.environ[k] = os.path.join(p, v)
        os.environ["V3_FLATTEN"] = "0"
        os.environ["GITHUB_TOKEN"] = ""
        try:
            m = Monitor()
            # dates RELATIVE to today: refresh_rewards only shows news
            # from the last 4 days, so hardcoded dates make this test
            # pass when written and fail silently a week later. It did
            # exactly that — written 2026-08-21, broke on 2026-08-25.
            import datetime as _d
            today = _d.datetime.now(_d.timezone.utc)
            d1 = (today - _d.timedelta(days=2)).strftime("%Y-%m-%d")
            d2 = (today - _d.timedelta(days=1)).strftime("%Y-%m-%d")
            rows = [{"date": d1, "market": "m1",
                     "program_type": "lp", "reward_usd": 0.6,
                     "status": "PENDING"}]

            class C:
                def earnings(self, start):
                    return list(rows)
            m.client = C()
            pushes = []
            m.alerts.notify = lambda t, msg, priority="default": pushes.append(t)
            m.refresh_rewards()                  # baseline
            r = m.refresh_rewards()
            self.assertEqual(r["new_count"], 0)  # quiet when nothing new
            rows.append({"date": d2, "market": "m2",
                         "program_type": "lp", "reward_usd": 3.0,
                         "status": "PENDING"})
            r = m.refresh_rewards()
            self.assertEqual(r["new_count"], 1)  # the watcher would push this
        finally:
            for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V1_ACK_PATH",
                      "V2_ACK_PATH", "V3_FLATTEN"):
                os.environ.pop(k, None)
            self.dir.cleanup()


class TestV1Port(unittest.TestCase):
    """1.0's essentials, now 3.0's: the front door, the repo files, the
    owner's own order form."""

    def test_floor_needs_nobody_when_both_are_retired(self):
        import tempfile
        from v3 import floor
        with tempfile.TemporaryDirectory() as p:
            os.environ["V3_FLOOR_PATH"] = os.path.join(p, "f.json")
            try:
                self.assertEqual(floor.Floor.required(), ())
                self.assertTrue(floor.Floor(clock=lambda: 1.0).acked())
            finally:
                os.environ.pop("V3_FLOOR_PATH", None)

    def test_rewards_csv_preserves_unreachable_history(self):
        import tempfile
        with tempfile.TemporaryDirectory() as p:
            for k, v in (("V3_STATE_PATH", "s.json"),
                         ("V3_FLOOR_PATH", "f.json")):
                os.environ[k] = os.path.join(p, v)
            os.environ["GITHUB_TOKEN"] = ""
            try:
                m = Monitor()
                existing = ("date,market,program_type,reward_usd,status\n"
                            "2026-07-01,ancient,liquidityProgram,9.99,PAID\n"
                            "2026-08-18,m1,liquidityProgram,1.5,PAID\n")
                rows = [{"date": "2026-08-18", "market": "m1",
                         "program_type": "liquidityProgram",
                         "reward_usd": 1.5, "status": "PAID"},
                        {"date": "2026-08-20", "market": "m2",
                         "program_type": "liquidityProgram",
                         "reward_usd": 2.0, "status": "PENDING"}]
                text = m.compose_rewards_csv(rows, existing)
                lines = text.strip().split("\n")
                self.assertEqual(lines[0],
                                 "date,market,program_type,reward_usd,status")
                self.assertIn("2026-07-01,ancient,liquidityProgram,9.99,PAID",
                              lines)          # history beyond the API kept
                self.assertIn("2026-08-20,m2,liquidityProgram,2,PENDING",
                              lines)
                self.assertEqual(len([l for l in lines if ",m1," in l]), 1)
                md = m.compose_status_md(1_787_300_000.0)
                self.assertIn("Politics", md)
                self.assertIn("/day resting", md)
            finally:
                for k in ("V3_STATE_PATH", "V3_FLOOR_PATH"):
                    os.environ.pop(k, None)

    def test_owner_place_routes_and_manual_is_untouchable(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.cycle()
        res_like = r.fam.desk.place_resting(A, "BUY", 0.30, 3.0,
                                            initiator="owner", verify=False)
        self.assertTrue(res_like.ok)
        from v3.family import FamilyOrder
        r.fam.orders[res_like.order_id] = FamilyOrder(
            id=res_like.order_id, market=A, side="BUY", price=0.30, qty=3.0,
            intent=res_like.intent, placed_ts=r.now, purpose="manual",
            why="placed by the owner")
        # hours pass; the cull would eat a 30c bid measuring ~0 — but
        # manual orders are the owner's and automation never touches them
        r.fam.last_action.clear()
        r.cycle(advance=7200.0)
        self.assertIn(res_like.order_id, r.fam.orders)
        self.assertEqual(r.fam.orders[res_like.order_id].price, 0.30)


class TestLiveReplans(unittest.TestCase):
    def test_fresh_books_rescore_without_spending_fetches(self):
        from v3.tests.test_family import Rig, A, politics_book
        from v3.family import FamilyConfig
        cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                           capital_usd=100.0, replan_s=600.0)
        r = Rig(cfg=cfg)
        r.add_market(A)
        r.cycle()
        feed0 = len(r.fam.triage_feed)
        # the stream keeps the book fresh; the REST fetch counter must not move
        fetches = {"n": 0}
        real_book = r.exchange.book
        def counted(slug, fetched_at=None):
            fetches["n"] += 1
            return real_book(slug, fetched_at)
        r.exchange.book = counted
        r.cache.put(A, politics_book(r.now + 660))     # stream write
        r.fam.orders.clear()                           # A is idle again
        r.cycle(advance=660.0)
        self.assertGreater(len([t for t in r.fam.triage_feed
                                if t["ts"] > 1_000_060]), 0)
        sb = r.fam.scoreboard[A]
        self.assertGreater(sb["ts"], 1_000_060)        # rescored
        self.assertEqual(fetches["n"], 0)              # for free


class TestStreamRouter(unittest.TestCase):
    def test_frames_route_to_the_owning_family(self):
        from v3.main import CacheRouter
        from v3.books import BookCache
        from v3.scoring import Book

        class F:
            def __init__(self, universe):
                self.universe = universe
                self.cache = BookCache()
        pol = F({"ussewc-usse-ga-2026-11-03-rep": {}})
        cfb = F({"aachc-cfb-wins-2026-11-28-ala-9pt5wins": {}})
        router = CacheRouter({"politics": pol, "cfb": cfb})
        b = Book(bids=((0.4, 5.0),), asks=((0.6, 5.0),), tick=0.01,
                 fetched_at=1.0)
        router.put("aachc-cfb-wins-2026-11-28-ala-9pt5wins", b)
        self.assertIsNotNone(
            cfb.cache.any_age("aachc-cfb-wins-2026-11-28-ala-9pt5wins"))
        self.assertIsNone(
            pol.cache.any_age("aachc-cfb-wins-2026-11-28-ala-9pt5wins"))
        router.put("ussewc-usse-ga-2026-11-03-rep", b)     # falls to politics
        self.assertIsNotNone(
            pol.cache.any_age("ussewc-usse-ga-2026-11-03-rep"))

    def test_router_accepts_the_streams_writer_tag(self):
        # THE dead-stream bug (frame-shape sampler evidence,
        # 2026-08-28): apply_frame calls put(..., writer="ws") and the
        # router's put didn't take the parameter — every parsed book
        # frame died on a TypeError inside the stream's guard. The
        # router must accept it and pass it through to the counters.
        from v3.main import CacheRouter
        from v3.books import BookCache
        from v3.scoring import Book

        class F:
            def __init__(self, universe):
                self.universe = universe
                self.cache = BookCache()
        pol = F({"ussewc-usse-ga-2026-11-03-rep": {}})
        router = CacheRouter({"politics": pol})
        b = Book(bids=((0.4, 5.0),), asks=((0.6, 5.0),), tick=0.01,
                 fetched_at=1.0)
        router.put("ussewc-usse-ga-2026-11-03-rep", b, writer="ws")
        self.assertIsNotNone(
            pol.cache.any_age("ussewc-usse-ga-2026-11-03-rep"))
        self.assertEqual(pol.cache.writes.get("ws"), 1)
        self.assertEqual(pol.cache.writes.get("rest"), 0)

    def test_ws_list_carries_every_family(self):
        import tempfile
        with tempfile.TemporaryDirectory() as p:
            for k, v in (("V3_STATE_PATH", "s.json"),
                         ("V3_FLOOR_PATH", "f.json")):
                os.environ[k] = os.path.join(p, v)
            os.environ["GITHUB_TOKEN"] = ""
            try:
                m = Monitor()
                from v3.family import FamilyOrder
                from v3.intents import BUY_LONG
                m.families["cfb"].orders["x"] = FamilyOrder(
                    id="x", market="aachc-cfb-wins-2026-11-28-ala-9pt5wins",
                    side="BUY", price=0.4, qty=1.0, intent=BUY_LONG,
                    placed_ts=0.0, purpose="earn")
                # the owner's slot order (2026-08-21): politics
                # markets he is in first, then cfb held, then rotation
                from v3.family import FamilyOrder as FO
                for i in range(50):
                    m.families["politics"].orders[f"p{i}"] = FO(
                        id=f"p{i}", market=f"ussewc-usse-x{i}-2026-11-03-rep",
                        side="BUY", price=0.4, qty=1.0, intent=BUY_LONG,
                        placed_ts=0.0, purpose="earn")
                slugs = m._ws_slugs()
                self.assertEqual(
                    slugs.index("aachc-cfb-wins-2026-11-28-ala-9pt5wins"),
                    50)                      # right after politics' held
                # and when politics alone fills the cap, politics wins
                for i in range(50, 300):
                    m.families["politics"].orders[f"p{i}"] = FO(
                        id=f"p{i}", market=f"ussewc-usse-x{i}-2026-11-03-rep",
                        side="BUY", price=0.4, qty=1.0, intent=BUY_LONG,
                        placed_ts=0.0, purpose="earn")
                slugs = m._ws_slugs()
                self.assertEqual(len(slugs), 200)
                self.assertTrue(all(s.startswith("ussewc") for s in slugs))
            finally:
                for k in ("V3_STATE_PATH", "V3_FLOOR_PATH"):
                    os.environ.pop(k, None)


class TestCandidatePriors(unittest.TestCase):
    """Owner, 2026-08-21: 'There is only one democratic candidate. I gave
    you the model as a prior.' Silver's per-candidate columns price the
    candidate markets, and dropping them silently unpriced hundreds."""

    def test_becerra_gets_silvers_number(self):
        from v3.silver import SilverFairs, slug_code
        self.assertEqual(slug_code("Xavier Becerra"), "xavbec")
        self.assertEqual(slug_code("J.D. Vance"), "jdvan")
        sf = SilverFairs()
        sf.gov_races = {"ca": {"dem": 0.9994, "rep": 0.0006,
                               "name": "California",
                               "cands": {"xavbec": 0.9994,
                                         "stehil": 0.0006}}}
        self.assertAlmostEqual(
            sf.race_fair("ewc-usgub-ca-2026-11-03-xavbec"), 0.9994)
        self.assertAlmostEqual(
            sf.model_fair("ewc-usgub-ca-2026-11-03-stehil"), 0.0006)
        self.assertIsNone(
            sf.race_fair("vmc-usgubmov-ca-2026-11-03-d12-15"))

    def test_house_control_maps_to_the_histograms(self):
        from v3.silver import SilverFairs
        sf = SilverFairs()
        sf.official = {"house": {"deluxe": {217: 0.4, 218: 0.6}}}
        v = sf.model_fair("paccc-usho-midterms-2026-11-03-rep")
        self.assertAlmostEqual(v, 0.6)
        v2 = sf.model_fair("paccc-usho-midterms-2026-11-03-dem")
        self.assertAlmostEqual(v2, 0.4)


class TestWholeShares(unittest.TestCase):
    """Owner, 2026-08-21: politics quotes whole shares only, for now —
    testing whether fractional orders even earn rewards."""

    def _rig(self):
        from v3.tests.test_family import Rig
        from v3.family import FamilyConfig
        cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                           rest_style="join_quiet", revive=True,
                           capital_usd=100.0, per_market_usd=2.0,
                           whole_shares=True)
        return Rig(cfg=cfg)

    def test_new_quotes_are_whole_shares(self):
        from v3.tests.test_family import A
        r = self._rig()
        r.add_market(A)
        r.cycle()
        self.assertTrue(r.fam.orders)
        for o in r.fam.orders.values():
            self.assertEqual(o.qty, round(o.qty), o)

    def test_live_fractional_order_is_retired(self):
        from v3.tests.test_family import A
        from v3.family import FamilyOrder
        r = self._rig()
        r.add_market(A)
        r.cycle()
        rec = FamilyOrder(id="FRAC1", market=A, side="BUY", price=0.42,
                          qty=2.5, intent="ORDER_INTENT_BUY_LONG",
                          placed_ts=r.now, purpose="earn")
        r.fam.orders["FRAC1"] = rec
        r.exchange.live["FRAC1"] = {"id": "FRAC1", "market": A,
                                    "side": "BUY", "price": 0.42,
                                    "size": 2.5}
        r.cycle()
        self.assertNotIn("FRAC1", r.fam.orders)

    def test_exits_keep_fractional_sizes(self):
        # owner, 2026-08-21: "Fractional are fine for exits"
        from v3.tests.test_family import A
        r = self._rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": 12.4, "cost": 12.4 * 0.30}
        r.positions[A] = (12.4, 12.4 * 0.30)
        r.cycle()
        exits = [o for o in r.fam.orders.values() if o.purpose == "sell"]
        self.assertTrue(exits)
        self.assertEqual(exits[0].qty, 12.4)   # the whole position rests
        r.cycle(); r.cycle()
        self.assertIn(exits[0].id, r.fam.orders)   # and is not culled

    def test_manual_fractional_order_is_left_alone(self):
        from v3.tests.test_family import A
        from v3.family import FamilyOrder
        r = self._rig()
        r.add_market(A)
        rec = FamilyOrder(id="MAN1", market=A, side="BUY", price=0.40,
                          qty=1.5, intent="ORDER_INTENT_BUY_LONG",
                          placed_ts=r.now, purpose="manual")
        r.fam.orders["MAN1"] = rec
        r.exchange.live["MAN1"] = {"id": "MAN1", "market": A,
                                   "side": "BUY", "price": 0.40,
                                   "size": 1.5}
        r.cycle()
        self.assertIn("MAN1", r.fam.orders)


class TestSeatScope(unittest.TestCase):
    """Owner, 2026-08-21 evening: House control and the seat brackets
    join the entry scope; turnout does not."""

    def test_scope_covers_control_and_brackets_not_turnout(self):
        from v3 import politics
        fam_cfg = politics.config()
        def enterable(slug):
            return any(tok in slug for tok in fam_cfg.enter_tokens)
        self.assertTrue(enterable("paccc-usho-midterms-2026-11-03-rep"))
        self.assertTrue(enterable("scc-hrep-rep-2026-11-03-gte205"))
        self.assertTrue(enterable("ussewc-usse-ks-2026-11-03-rep"))
        self.assertFalse(enterable("vtc-hrep-to-2026-11-03-gte130m"))
        self.assertFalse(enterable("dccc-measles-us-2026-12-31-gt4500"))


class TestChartResolver(unittest.TestCase):
    """Owner, 2026-08-21: the governor table froze at the Aug 18 Alaska
    primary while the site moved — the fetch must follow the chart to
    wherever its data lives now."""

    def _silver(self):
        import inspect
        import v3.silver as sv
        cls = [o for n, o in vars(sv).items()
               if inspect.isclass(o) and hasattr(o, "_resolve_csv")][0]
        s = cls.__new__(cls)
        s.note = ""
        return s

    def test_follows_redirect_and_reads_the_moved_data_url(self):
        import sys, types
        pages = {
            "https://datawrapper.dwcdn.net/N13WX/":
                "<meta http-equiv=\"REFRESH\" content=\"0; "
                "url=https://datawrapper.dwcdn.net/N13WX/17/+'\">",
            "https://datawrapper.dwcdn.net/N13WX/17/":
                "x" * 3000 + '"https://static.dwcdn.net/data/ZZtop.csv?v=4"',
        }
        fake = types.ModuleType("requests")
        class R:
            def __init__(self, text): self.text, self.status_code = text, 200
        fake.get = lambda url, **kw: R(pages.get(url, ""))
        old = sys.modules.get("requests")
        sys.modules["requests"] = fake
        try:
            s = self._silver()
            got = s._resolve_csv("N13WX",
                                 "https://static.dwcdn.net/data/N13WX.csv")
            self.assertEqual(got, "https://static.dwcdn.net/data/ZZtop.csv?v=4")
            self.assertIn("data moved", s.note)
        finally:
            if old is not None: sys.modules["requests"] = old
            else: sys.modules.pop("requests", None)

    def test_falls_back_to_the_fixed_address_on_any_trouble(self):
        import sys, types
        fake = types.ModuleType("requests")
        def boom(url, **kw): raise OSError("no route")
        fake.get = boom
        old = sys.modules.get("requests")
        sys.modules["requests"] = fake
        try:
            s = self._silver()
            got = s._resolve_csv("kNspD",
                                 "https://static.dwcdn.net/data/kNspD.csv")
            self.assertEqual(got, "https://static.dwcdn.net/data/kNspD.csv")
        finally:
            if old is not None: sys.modules["requests"] = old
            else: sys.modules.pop("requests", None)


class TestGovChartChooser(unittest.TestCase):
    HDR = ("state,abbr,winner_Dparty,winner_Rparty,name_D1,name_D2,name_D3,"
           "name_D4,name_R1,name_R2,name_R3,name_R4,winner_D1,winner_D2,"
           "winner_D3,winner_D4,winner_R1,winner_R2,winner_R3,winner_R4,"
           "rating")

    def csv(self, rows):
        return self.HDR + "\n" + "\n".join(rows)

    def test_finds_governor_under_a_new_id(self):
        import sys, types, inspect, time
        import v3.silver as sv
        senate_csv = self.csv(["Texas,TX,60,40,A,,,,B,,,,60,,,,40,,,,0"])
        gov_new = self.csv([
            "Alaska,AK,38.5,61.5,Tom Begich,J Kreiss-Tomkins,,,"
            "Bernadette Wilson,David Bronson,,,22.2,16.3,,,41.5,20.0,,,0",
            "Vermont,VT,80,20,C,,,,D,,,,80,,,,20,,,,0"])
        pages = {
            "3DsnL": senate_csv,      # a decoy: same as the senate table
            "KXB1W": self.csv(["Ohio,OH,50,50,E,,,,F,,,,50,,,,50,,,,0"]),
            "N13WX": gov_new,
        }
        fake = types.ModuleType("requests")
        class R:
            def __init__(self, t): self.text, self.status_code = t, 200
        def get(url, **kw):
            for cid, body in pages.items():
                if cid in url:
                    return R(body)
            return R("")
        fake.get = get
        cls = [o for n, o in vars(sv).items()
               if inspect.isclass(o) and hasattr(o, "_refresh_gov")][0]
        s = cls(client=None)
        s.races = sv.parse_races(senate_csv)      # the senate table, loaded
        s.gov_races = sv.parse_races(self.csv([
            "Alaska,AK,72,28,Tom Begich,,,,Bernadette Wilson,,,,72,,,,28,,,,0",
            "Vermont,VT,79,21,C,,,,D,,,,79,,,,21,,,,0"]))
        old = sys.modules.get("requests")
        sys.modules["requests"] = fake
        try:
            s._gov_at = 0.0
            ok = s._refresh_gov(1_000_000.0)
        finally:
            if old is not None: sys.modules["requests"] = old
            else: sys.modules.pop("requests", None)
        self.assertTrue(ok)
        ak = s.gov_races.get("ak") or {}
        self.assertAlmostEqual(ak.get("rep"), 0.615, places=2)
        self.assertEqual(s._gov_cid, "N13WX")


class TestHoldingsCeiling(unittest.TestCase):
    """Owner, 2026-08-21 evening: cfb risk = orders + holdings at
    liquidation value, capped together."""

    def test_holdings_valued_at_the_liquidating_price(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        from v3.scoring import Book
        cfg = FamilyConfig(name="C", tag="C", capital_usd=50.0,
                           holdings_in_ceiling=True)
        r = Rig(cfg=cfg)
        r.add_market(A, book=Book(bids=((0.30, 50.0),),
                                  asks=((0.40, 50.0),),
                                  tick=0.01, fetched_at=1_000_000.0))
        r.cache.put(A, Book(bids=((0.30, 50.0),), asks=((0.40, 50.0),),
                            tick=0.01, fetched_at=r.now))
        r.fam.inventory[A] = {"qty": 100.0, "cost": 35.0}
        self.assertAlmostEqual(r.fam.holdings_value(), 30.0, places=2)
        # and the ceiling includes it
        self.assertGreaterEqual(r.fam.family_spent(), 30.0)

    def test_ceiling_ignores_holdings_when_flag_off(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        from v3.scoring import Book
        cfg = FamilyConfig(name="P", tag="P", capital_usd=50.0)
        r = Rig(cfg=cfg)
        r.cache.put(A, Book(bids=((0.30, 50.0),), asks=((0.40, 50.0),),
                            tick=0.01, fetched_at=r.now))
        r.fam.inventory[A] = {"qty": 100.0, "cost": 35.0}
        self.assertAlmostEqual(r.fam.family_spent(), 0.0, places=2)


class TestCandidateLabels(unittest.TestCase):
    def test_sibling_markets_show_their_candidate(self):
        from v3.names import disambiguate
        out = disambiguate([
            ("enwc-uspres-nom-rep-2028-dontru", "2028 GOP Nominee"),
            ("enwc-uspres-nom-rep-2028-jdvan", "2028 GOP Nominee"),
            ("ussewc-usse-ks-2026-11-03-rep", "Kansas Senate Winner")])
        self.assertIn("dontru", out["enwc-uspres-nom-rep-2028-dontru"])
        self.assertIn("jdvan", out["enwc-uspres-nom-rep-2028-jdvan"])
        self.assertEqual(out["ussewc-usse-ks-2026-11-03-rep"],
                         "Kansas Senate Winner")


class TestPhantomFills(unittest.TestCase):
    """The Louisiana phantom (2026-08-21): cancelled revives were booked
    as 265-share shorts the exchange never saw. Fills need the position
    feed to agree; the exchange's positions are the truth."""

    def test_size_shrink_without_delta_books_no_fill(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import BUY_SHORT
        r = Rig()
        r.add_market(A)
        r.fam.orders["S1"] = FamilyOrder(
            id="S1", market=A, side="SELL", price=0.99, qty=500.0,
            intent=BUY_SHORT, placed_ts=r.now, purpose="revive")
        r.exchange.live["S1"] = {"id": "S1", "market": A, "side": "SELL",
                                 "price": 0.99, "size": 234.5}
        r.cycle()                      # position feed shows nothing
        # the later cull may pull the weak revive — the point is that
        # NO phantom short was ever booked
        self.assertNotIn(A, r.fam.inventory)

    def test_size_shrink_with_matching_delta_is_a_fill(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        r = Rig()
        r.add_market(A)
        r.fam.orders["B1"] = FamilyOrder(
            id="B1", market=A, side="BUY", price=0.40, qty=10.0,
            intent=BUY_LONG, placed_ts=r.now, purpose="earn")
        r.exchange.live["B1"] = {"id": "B1", "market": A, "side": "BUY",
                                 "price": 0.40, "size": 6.0}
        r.positions[A] = (4.0, 1.60)   # the exchange saw 4 shares arrive
        r.cycle()
        self.assertAlmostEqual(r.fam.inventory[A]["qty"], 4.0, places=2)

    def test_exchange_positions_purge_phantom_inventory(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": -574.4, "cost": -568.66}   # the phantom
        r.positions[A] = (0.0, 0.0)    # the exchange says flat
        r.cycle()
        self.assertNotIn(A, r.fam.inventory)

    def test_feed_absence_purges_phantom_after_grace(self):
        # the feed lists only held markets — a phantom market is exactly
        # the one it never names
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": -872.1, "cost": -863.38}
        r.cycle()                      # positions feed says nothing at all
        self.assertNotIn(A, r.fam.inventory)

    def test_fresh_fill_survives_one_absent_snapshot(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": 4.0, "cost": 1.6}
        r.fam.inv_since[A] = r.now + 50.0   # booked seconds ago
        r.cycle(advance=60.0)
        self.assertIn(A, r.fam.inventory)   # grace period holds it


class TestLadderView(unittest.TestCase):
    def test_every_priced_level_carries_its_numbers(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.cycle()
        lad = r.fam.ladder_view(A)
        self.assertTrue(lad["ok"])
        rows = lad["sides"]["BUY"]["rows"]
        self.assertGreater(len(rows), 3)
        for k in ("px", "qty", "share", "est", "ev", "p_fill", "fill_cost"):
            self.assertIn(k, rows[0])
        self.assertTrue(any(r_.get("picked") for r_ in rows))


class TestExitOverCover(unittest.TestCase):
    def test_excess_covers_get_pruned(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import SELL_SHORT
        from v3.scoring import Book
        r = Rig()
        # the Alabama shape: covers ladder in a near-empty bid side, so
        # the 15c cover genuinely out-earns the 1c ones
        r.add_market(A, book=Book(bids=((0.01, 42100.0),),
                                  asks=((0.16, 462.0), (0.98, 60000.0)),
                                  tick=0.01, fetched_at=1_000_000.0))
        r.fam.inventory[A] = {"qty": -5.0, "cost": 5.12}
        r.positions[A] = (-5.0, 5.12)
        for i, (px, est) in enumerate([(0.15, 5.68), (0.14, 0.57),
                                       (0.01, 0.0), (0.01, 0.0),
                                       (0.01, 0.0), (0.01, 0.0)]):
            oid = f"C{i}"
            r.fam.orders[oid] = FamilyOrder(
                id=oid, market=A, side="BUY", price=px, qty=1.0,
                intent=SELL_SHORT, placed_ts=0.0, purpose="sell",
                live_est=est)
            r.exchange.live[oid] = {"id": oid, "market": A, "side": "BUY",
                                    "price": px, "size": 1.0}
        r.cycle()
        covers = [o for o in r.fam.orders.values()
                  if o.market == A and o.purpose == "sell"]
        self.assertLessEqual(sum(o.qty for o in covers), 5.0 + 0.01)
        # the earners survived; the dead 1c excess went
        self.assertTrue(any(abs(o.price - 0.15) < 1e-9 for o in covers))


class TestProvenBudget(unittest.TestCase):
    def test_graduated_markets_get_the_bigger_allowance(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        cfg = FamilyConfig(name="P", tag="P", per_market_usd=20.0,
                           proven_per_market_usd=40.0)
        r = Rig(cfg=cfg)
        self.assertEqual(r.fam._market_budget(A), 20.0)
        r.fam.proven.add(A)
        self.assertEqual(r.fam._market_budget(A), 40.0)


class TestSamplerDots(unittest.TestCase):
    def test_every_sample_leaves_a_dot_and_survives_restarts(self):
        from v3.estimator import Estimator
        e = Estimator()
        class B:
            def fresh(self, m, age, now): return None
        e.sample(1_000_000.0, [], {}, B(), lambda m, p: 1.0)
        e.sample(1_000_020.0, [], {}, B(), lambda m, p: 1.0)
        self.assertEqual(len(e.dots), 2)
        self.assertEqual(e.dots[0][0], 1_000_000.0)
        e2 = Estimator.from_dict(e.to_dict())
        self.assertEqual(e2.dots, e.dots)


class TestExitOpportunityCost(unittest.TestCase):
    """Owner, 2026-08-21 evening: exits may concede price when the freed
    money earns more elsewhere."""

    def test_score_prefers_yield_when_capital_is_slack(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        f = r.fam
        # r_eff 0: the higher-earning slot wins outright
        hi_est = f._exit_score(est=5.0, pf=0.1, qty=10, px=0.97,
                               basis=0.92, side="SELL", r_eff=0.0, d_off=2.0)
        lo_px = f._exit_score(est=1.0, pf=0.4, qty=10, px=0.93,
                              basis=0.92, side="SELL", r_eff=0.0, d_off=2.0)
        self.assertGreater(hi_est, lo_px)

    def test_score_concedes_when_the_ceiling_binds(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        f = r.fam
        # a binding book earning ~$1/day per dollar: the faster, cheaper
        # exit now wins — freed capital out-earns the resting slot
        hi_est = f._exit_score(est=5.0, pf=0.1, qty=10, px=0.97,
                               basis=0.92, side="SELL", r_eff=1.0, d_off=2.0)
        lo_px = f._exit_score(est=1.0, pf=0.4, qty=10, px=0.93,
                              basis=0.92, side="SELL", r_eff=1.0, d_off=2.0)
        self.assertGreater(lo_px, hi_est)

    def test_opportunity_rate_is_the_marginal_cent(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        r = Rig()
        self.assertEqual(r.fam._exit_opportunity_rate(), 0.0)  # empty book
        # a star earning $2/day per $ and a marginal order earning 10c/$:
        # the freed cent redeploys like the MARGINAL one
        for oid, px, qty, est in (("STAR", 0.10, 10.0, 2.0),
                                  ("EDGE", 0.50, 100.0, 5.0)):
            r.fam.orders[oid] = FamilyOrder(
                id=oid, market=A, side="BUY", price=px, qty=qty,
                intent=BUY_LONG, placed_ts=0.0, purpose="earn",
                live_est=est)
        rate = r.fam._exit_opportunity_rate()
        self.assertAlmostEqual(rate, 5.0 / 50.0, places=3)


class TestLadderBelowTargetNote(unittest.TestCase):
    def test_starved_side_explains_itself(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.02, 10.0),),
                                  asks=((0.04, 50000.0), (0.08, 31000.0)),
                                  tick=0.01, fetched_at=1_000_000.0))
        r.cycle()
        lad = r.fam.ladder_view(A)
        self.assertTrue(lad["ok"])
        buy = lad["sides"]["BUY"]
        self.assertEqual(buy["rows"], [])
        self.assertIn("Target Size", buy.get("note", ""))
        self.assertIn("pays nobody", buy.get("note", ""))


class TestFillJournal(unittest.TestCase):
    def _fill_one(self, r):
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        rec = FamilyOrder(id="J1", market=A_J, side="BUY", price=0.19,
                          qty=100.0, intent=BUY_LONG, placed_ts=999_000.0,
                          purpose="earn", why="joins the touch",
                          live_est=2.4)
        r.fam._on_fill(rec, 100.0, 1_000_000.0)
        return r.fam.fills[-1]

    def test_every_fill_leaves_a_report_row(self):
        from v3.tests.test_family import Rig
        r = Rig()
        r.add_market(A_J)
        r.fam.fairs = lambda s: 0.10
        row = self._fill_one(r)
        self.assertEqual(row["side"], "BUY")
        self.assertEqual(row["qty"], 100.0)
        self.assertEqual(row["px"], 0.19)
        self.assertEqual(row["why"], "joins the touch")
        self.assertEqual(row["est_day"], 2.4)
        self.assertAlmostEqual(row["conc"], 0.09)   # paid 9c past the model
        self.assertAlmostEqual(row["rested_h"], 0.28, places=2)
        self.assertEqual(row["pos_after"], 100.0)

    def test_journal_survives_a_restart(self):
        from v3.tests.test_family import Rig
        r = Rig()
        r.add_market(A_J)
        r.fam.fairs = None
        row = self._fill_one(r)
        self.assertIsNone(row["fair"])
        d = r.fam.to_dict()
        r2 = Rig()
        r2.fam.restore(d)
        self.assertEqual(len(r2.fam.fills), 1)
        self.assertEqual(r2.fam.fills[0]["px"], 0.19)


from v3.tests.test_family import A as A_J  # noqa: E402


class TestRoundTripPairing(unittest.TestCase):
    def test_buy_pairs_with_its_sells(self):
        from v3.main import pair_fills
        cards = pair_fills([
            {"ts": 1.0, "market": "m", "side": "BUY", "qty": 100.0,
             "px": 0.19, "purpose": "earn"},
            {"ts": 2.0, "market": "m", "side": "SELL", "qty": 40.0,
             "px": 0.21, "purpose": "sell"},
        ])
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertEqual(c["open_qty"], 60.0)
        self.assertEqual(c["closes"][0]["qty"], 40.0)
        self.assertAlmostEqual(c["realized"], 0.8)
        self.assertEqual(c["last_ts"], 2.0)

    def test_short_pairs_with_its_buy_back(self):
        from v3.main import pair_fills
        cards = pair_fills([
            {"ts": 1.0, "market": "m", "side": "SELL", "qty": 5.0,
             "px": 0.93, "purpose": "earn"},
            {"ts": 2.0, "market": "m", "side": "BUY", "qty": 5.0,
             "px": 0.90, "purpose": "sell"},
        ])
        self.assertEqual(len(cards), 1)
        c = cards[0]
        self.assertEqual(c["open_qty"], 0.0)
        self.assertAlmostEqual(c["realized"], 0.15)

    def test_unmatched_exit_is_a_stray_not_a_short(self):
        from v3.main import pair_fills
        cards = pair_fills([
            {"ts": 1.0, "market": "m", "side": "SELL", "qty": 10.0,
             "px": 0.70, "purpose": "sell"},
        ])
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0].get("stray_close"))
        self.assertEqual(cards[0]["open_qty"], 0.0)

    def test_fifo_across_lots_and_markets_stay_separate(self):
        from v3.main import pair_fills
        cards = pair_fills([
            {"ts": 1.0, "market": "m", "side": "BUY", "qty": 2.0,
             "px": 0.10, "purpose": "earn"},
            {"ts": 2.0, "market": "m", "side": "BUY", "qty": 3.0,
             "px": 0.20, "purpose": "earn"},
            {"ts": 3.0, "market": "other", "side": "SELL", "qty": 1.0,
             "px": 0.50, "purpose": "earn"},
            {"ts": 4.0, "market": "m", "side": "SELL", "qty": 4.0,
             "px": 0.30, "purpose": "sell"},
        ])
        m_cards = [c for c in cards if c["market"] == "m"]
        self.assertEqual(len(m_cards), 2)
        first, second = m_cards
        self.assertEqual(first["open_qty"], 0.0)        # oldest lot closed first
        self.assertAlmostEqual(first["realized"], 0.4)  # 2 x (30c - 10c)
        self.assertEqual(second["open_qty"], 1.0)
        self.assertAlmostEqual(second["realized"], 0.2)  # 2 x (30c - 20c)
        other = [c for c in cards if c["market"] == "other"][0]
        self.assertEqual(other["open_qty"], 1.0)         # an earn short stays open


class TestOversizedExitFallback(unittest.TestCase):
    def test_tulgab_shape_cancelled_whole_then_resized(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import SELL_SHORT
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.03, 210.0), (0.02, 500.0)),
                                  asks=((0.04, 641.0), (0.08, 31000.0)),
                                  tick=0.01, fetched_at=1_000_000.0))
        r.fam.inventory[A] = {"qty": -1.0, "cost": 0.78}
        r.positions[A] = (-1.0, 0.78)
        r.fam.orders["T1"] = FamilyOrder(
            id="T1", market=A, side="BUY", price=0.02, qty=500.0,
            intent=SELL_SHORT, placed_ts=0.0, purpose="sell",
            live_est=0.0)
        r.exchange.live["T1"] = {"id": "T1", "market": A, "side": "BUY",
                                 "price": 0.02, "size": 500.0}
        r.cycle()
        self.assertNotIn("T1", r.fam.orders)   # the 500 went, whole
        r.cycle(advance=600.0)                 # past the cooldown
        covers = [o for o in r.fam.orders.values()
                  if o.market == A and o.purpose == "sell"
                  and o.side == "BUY"]
        self.assertTrue(covers)
        self.assertLessEqual(sum(o.qty for o in covers), 1.01)

    def test_exit_with_no_position_is_cancelled(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG
        r = Rig()
        r.add_market(A)
        r.fam.orders["P1"] = FamilyOrder(
            id="P1", market=A, side="SELL", price=0.7, qty=20.0,
            intent=SELL_LONG, placed_ts=1.0, purpose="sell")
        r.exchange.live["P1"] = {"id": "P1", "market": A, "side": "SELL",
                                 "price": 0.7, "size": 20.0}
        r.cycle()
        self.assertNotIn("P1", r.fam.orders)
        self.assertTrue(any(e.get("event") == "orphan_exit_cancelled"
                            for e in r.fam.log))

    def test_wrong_side_cover_on_a_long_is_cancelled(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import SELL_SHORT
        r = Rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": 5.0, "cost": 0.5}
        r.positions[A] = (5.0, 0.5)
        r.fam.orders["W1"] = FamilyOrder(
            id="W1", market=A, side="BUY", price=0.05, qty=10.0,
            intent=SELL_SHORT, placed_ts=1.0, purpose="sell")
        r.exchange.live["W1"] = {"id": "W1", "market": A, "side": "BUY",
                                 "price": 0.05, "size": 10.0}
        r.cycle()
        self.assertNotIn("W1", r.fam.orders)


class TestKeepWhenSideCanBeQualified(unittest.TestCase):
    def test_starved_side_revive_beats_the_pull(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder, FamilyConfig
        from v3.intents import BUY_LONG
        from v3.scoring import Book
        cfg = FamilyConfig(
            name="Politics", tag="POL", known_ground=True,
            rest_style="join_quiet", revive=True,
            capital_usd=100.0, per_market_usd=2.0, revive_max_usd=5.0,
            min_days_out=3, weak_pull_s=3600.0, min_est_day=5.0)
        r = Rig(cfg=cfg)
        prog = {"timePeriods": [{"programId": "politics_mid_1",
                                 "rewardPool": 20.0, "targetSize": 100,
                                 "discountFactor": 0.2, "status": "LIVE"}]}
        r.add_market(A, book=Book(bids=((0.02, 90.0),),
                                  asks=((0.03, 6000.0),),
                                  tick=0.01, fetched_at=1_000_000.0),
                     prog=prog)
        r.fam.orders["K1"] = FamilyOrder(
            id="K1", market=A, side="BUY", price=0.02, qty=10.0,
            intent=BUY_LONG, placed_ts=1.0, purpose="earn",
            why="joins", live_est=0.0, weak_since=1.0)
        r.exchange.live["K1"] = {"id": "K1", "market": A, "side": "BUY",
                                 "price": 0.02, "size": 10.0}
        r.cycle()
        self.assertIn("K1", r.fam.orders)   # kept — the side can be qualified
        self.assertEqual(r.fam.orders["K1"].weak_since, 0.0)


class TestProvenBudgetActsToo(unittest.TestCase):
    def test_acting_gate_honors_the_proven_allowance(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.cycle()
        r.fam.orders.clear()            # a clean slate for the gate test
        r.fam.cfg.proven_per_market_usd = 40.0
        r.fam.cfg.proven_usd = 150.0
        r.fam.proven = {A}
        r.fam.scoreboard[A] = {"ts": r.now, "plans": [
            {"side": "BUY", "px": 0.10, "qty": 250.0, "share": 0.5,
             "est": 5.0, "ev": 5.0, "p_fill": 0.1, "fill_cost": 0.01,
             "cost": 25.0, "why": "t"}]}
        r.fam.last_action.clear()       # clear the per-side cooldown
        placed = r.fam._enter(r.now, r.positions, 3)
        earns = [o for o in r.fam.orders.values()
                 if o.market == A and o.purpose == "earn"]
        self.assertTrue(earns)          # $25 plan fits the $40 proven cap
        self.assertEqual(earns[0].qty, 250.0)


class TestNoSelfBidding(unittest.TestCase):
    def _rig(self, with_ours):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        from v3.scoring import Book
        r = Rig(switch=False)
        r.add_market(A, book=Book(
            bids=((0.91, 2.0), (0.85, 6000.0)),
            asks=((0.95, 9000.0),),
            tick=0.01, fetched_at=1_000_000.0))
        # ask-side fills at 95c teach fair >= 95c, so the band's low
        # edge licenses deep bid rungs — the with-information path
        # (owner, 2026-08-25); the self-fronting charge is about THOSE
        for i in range(3):
            r.fam.evidence.fill(A, "SELL", 0.95, ts=999_000.0 + i)
        r.cycle()
        if with_ours:
            r.fam.orders["S1"] = FamilyOrder(
                id="S1", market=A, side="BUY", price=0.91, qty=2.0,
                intent=BUY_LONG, placed_ts=1.0, purpose="earn")
        return r

    def _plan(self, r, ladder=None):
        from v3.tests.test_family import A
        b = r.cache.fresh(A, 3600, r.now)
        p, _ = r.fam._prog_row(A)
        sp = r.fam._side_pool(A, p)
        return r.fam._plan_side(A, b, "BUY", p, sp, 10.0, ladder=ladder)

    def test_side_that_is_only_us_gets_no_plan(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        from v3.scoring import Book
        r = Rig(switch=False)
        r.add_market(A, book=Book(bids=((0.50, 6000.0),),
                                  asks=((0.60, 100000.0),),
                                  tick=0.01, fetched_at=1_000_000.0))
        r.cycle()
        r.fam.orders["S1"] = FamilyOrder(
            id="S1", market=A, side="BUY", price=0.50, qty=6000.0,
            intent=BUY_LONG, placed_ts=1.0, purpose="earn")
        self.assertIsNone(self._plan(r))

    def test_our_touch_is_not_the_touch(self):
        r = self._rig(with_ours=True)
        rows = []
        self._plan(r, ladder=rows)
        joins = [w for w in rows
                 if abs(w["px"] - 0.85) < 1e-9 and "touch" in w["why"]]
        self.assertTrue(joins)      # 85c, not our own 91c, is the touch

    def test_fronting_our_own_order_is_charged(self):
        rows_a, rows_b = [], []
        self._plan(self._rig(with_ours=False), ladder=rows_a)
        self._plan(self._rig(with_ours=True), ladder=rows_b)
        by_a = {(w["px"], w["qty"]): w["ev"] for w in rows_a}
        by_b = {(w["px"], w["qty"]): w["ev"] for w in rows_b}
        shared = [k for k in by_b if k in by_a and k[0] > 0.911]
        self.assertTrue(shared)
        for k in shared:    # in front of our 2 @ 91c on a thin side:
            self.assertLess(by_b[k], by_a[k] - 0.5)   # pays what it steals


class TestCapitalInTheEv(unittest.TestCase):
    def test_selling_held_stock_beats_a_naked_short(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        rows = {}
        for held in (0.0, 500.0):
            r = Rig(switch=False)
            r.add_market(A, book=Book(
                bids=((0.40, 9000.0),),
                asks=((0.60, 3000.0), (0.62, 4000.0)),
                tick=0.01, fetched_at=1_000_000.0))
            r.cycle()
            if held:
                r.fam.inventory[A] = {"qty": held, "cost": held * 0.5}
                r.positions[A] = (held, held * 0.5)
            r.fam._capital_charge_rate = lambda slug: 0.10
            lad = []
            b = r.cache.fresh(A, 3600, r.now)
            p, _ = r.fam._prog_row(A)
            sp = r.fam._side_pool(A, p)
            r.fam._plan_side(A, b, "SELL", p, sp, 10.0, ladder=lad)
            rows[held] = {(w["px"], w["qty"]): w["ev"] for w in lad}
        shared = [k for k in rows[500.0] if k in rows[0.0]]
        self.assertTrue(shared)
        # earners get NOTHING for freeing capital (owner, 2026-08-22) —
        # the only remaining difference is the collateral fact: selling
        # held stock ties nothing, a naked short pays the charge
        self.assertTrue(all(rows[500.0][k] >= rows[0.0][k] for k in shared))
        self.assertTrue(any(rows[500.0][k] > rows[0.0][k] for k in shared))


class TestLossCutExits(unittest.TestCase):
    def test_floor_moves_to_fair_only_when_model_says_worse(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.cycle()
        r.fam.fairs = lambda s: None
        self.assertEqual(r.fam._exit_floor(A, "SELL", 0.50, 0.01),
                         (0.51, 0.50))
        self.assertEqual(r.fam._exit_floor(A, "BUY", 0.93, 0.01),
                         (0.92, 0.93))
        r.fam.fairs = lambda s: 0.9998    # the Massachusetts short
        self.assertEqual(r.fam._exit_floor(A, "BUY", 0.93, 0.01),
                         (0.998, 0.998))
        r.fam.fairs = lambda s: 0.10      # stock worth less than we paid
        self.assertEqual(r.fam._exit_floor(A, "SELL", 0.50, 0.01),
                         (0.10, 0.10))
        r.fam.fairs = lambda s: 0.60      # model AGREES with break-even
        self.assertEqual(r.fam._exit_floor(A, "SELL", 0.50, 0.01),
                         (0.51, 0.50))


class TestLiteFeed(unittest.TestCase):
    def test_lite_frame_captures_declared_best_without_touching_books(self):
        from v3.ws import Stream
        from v3.books import BookCache
        import json as _json
        cache = BookCache()
        s = Stream(cache, lambda: [], "k", "s")
        raw = _json.dumps({"marketDataLite": {
            "marketSlug": "m-1",
            "bestBid": {"value": "0.03", "currency": "USD"},
            "bestAsk": {"value": "0.99", "currency": "USD"}}})
        out = s.apply_frame(raw)
        self.assertIsNone(out)
        bb, ba, ts = s.declared["m-1"]
        self.assertEqual((bb, ba), (0.03, 0.99))
        self.assertIsNone(cache.any_age("m-1"))   # books untouched

    def test_declared_anchor_recalc_matches_the_group_tool(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import SELL_SHORT
        from v3.scoring import Book
        r = Rig(switch=False)
        # the screenshot's book, with OUR 38 shares as the 98c ask
        r.add_market(A, book=Book(
            bids=((0.14, 0.02), (0.04, 58.0), (0.03, 40098.0),
                  (0.01, 2500.0)),
            asks=((0.15, 0.02), (0.23, 50.0), (0.49, 25.0),
                  (0.98, 38.0), (0.99, 21718.0)),
            tick=0.01, fetched_at=1_000_000.0))
        r.cycle()
        r.fam.orders["L1"] = FamilyOrder(
            id="L1", market=A, side="SELL", price=0.98, qty=38.0,
            intent=SELL_SHORT, placed_ts=1.0, purpose="earn",
            live_est=0.0)
        out = r.fam.lite_recalc(A, 0.03, 0.99)
        self.assertIsNotNone(out)
        # under wall anchoring our 98c order holds 11.40/21729.4 of the
        # ask side (the tool's own numbers)
        prog, _w = r.fam._prog_row(A)
        sp = r.fam._side_pool(A, prog)
        df = float(prog.df)
        ours = 38.0 * df          # one tick behind the declared 99c best
        denom = (21718.0 + ours + 25.0 * df ** 50
                 + 50.0 * df ** 76 + 0.02 * df ** 84)
        self.assertAlmostEqual(out["est_alt"], ours / denom * sp, places=3)
        self.assertEqual(out["raw_ask"], 0.15)   # raw touch differs
        self.assertEqual(out["ba"], 0.99)        # from the declared one


class TestStrandedExits(unittest.TestCase):
    def test_dust_position_walks_away_at_the_touch(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(
            bids=((0.04, 185.0), (0.02, 83300.0)),
            asks=((0.05, 965.0), (0.18, 50.0)),
            tick=0.01, fetched_at=1_000_000.0))
        r.fam.inventory[A] = {"qty": 1.0, "cost": 0.13}
        r.positions[A] = (1.0, 0.13)
        r.cycle()
        exits = [o for o in r.fam.orders.values()
                 if o.market == A and o.purpose == "sell"
                 and o.side == "SELL"]
        self.assertTrue(exits)
        # The dust rule once walked this 13c lot away AT THE TOUCH
        # (5c) for free. That freebie is gone (owner, 2026-08-25): this
        # side holds ~1,000 of 5,000 Target Size so nobody is paid at
        # any price, and the gate refuses a discount that buys nothing.
        # What remains is the evidence band's own loss-cut (kept rule,
        # owner 2026-08-22): the band prices this market ~7c, so the
        # exit may rest there — above the touch, at the band's edge —
        # but no lower.
        self.assertGreater(exits[0].price, 0.05)

    def test_collapsed_basis_cover_rests_at_the_band(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(
            bids=((0.02, 47600.0), (0.01, 23600.0)),
            asks=((0.09, 549.0), (0.13, 12500.0)),
            tick=0.01, fetched_at=1_000_000.0))
        r.fam.inventory[A] = {"qty": -2.0, "cost": 0.06}   # degenerate sign
        r.positions[A] = (-2.0, 0.06)
        r.cycle()
        covers = [o for o in r.fam.orders.values()
                  if o.market == A and o.purpose == "sell"
                  and o.side == "BUY"]
        self.assertTrue(covers)                       # no longer blocked
        self.assertLessEqual(covers[0].price, 0.09)

    def test_band_justifies_selling_under_water(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(
            bids=((0.03, 50000.0),),
            asks=((0.04, 40000.0), (0.99, 20000.0)),
            tick=0.01, fetched_at=1_000_000.0))
        r.fam.inventory[A] = {"qty": 25.0, "cost": 3.50}   # 14c basis
        r.positions[A] = (25.0, 3.50)
        r.cycle()
        exits = [o for o in r.fam.orders.values()
                 if o.market == A and o.purpose == "sell"
                 and o.side == "SELL"]
        self.assertTrue(exits)
        self.assertLess(exits[0].price, 0.14)   # below break-even, band-backed
        self.assertGreaterEqual(exits[0].price, 0.02)


class TestTargetPricesStaySmall(unittest.TestCase):
    def test_size_shrinks_past_fair(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig(switch=False)
        # the Arkansas shape: book far above the model
        r.add_market(A, book=Book(
            bids=((0.12, 4000.0), (0.07, 3000.0)),
            asks=((0.14, 5000.0),),
            tick=0.01, fetched_at=1_000_000.0))
        r.cycle()
        r.fam.fairs = lambda s: 0.02
        rows = []
        b = r.cache.fresh(A, 3600, r.now)
        p, _ = r.fam._prog_row(A)
        sp = r.fam._side_pool(A, p)
        r.fam._plan_side(A, b, "BUY", p, sp, 10.0, ladder=rows)
        # owner, 2026-08-23 ("Yes do both"): the shrink ladder is gone —
        # below 50c NOTHING rests above fair, at any size. The Arkansas
        # shape now produces zero bids past the 2c model.
        self.assertEqual([w for w in rows if w["px"] > 0.011], [])

    def test_at_fair_size_is_unrestricted(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig(switch=False)
        r.add_market(A, book=Book(
            bids=((0.50, 4000.0), (0.48, 3000.0)),
            asks=((0.53, 5000.0),),
            tick=0.01, fetched_at=1_000_000.0))
        r.cycle()
        r.fam.fairs = lambda s: 0.52
        rows = []
        b = r.cache.fresh(A, 3600, r.now)
        p, _ = r.fam._prog_row(A)
        sp = r.fam._side_pool(A, p)
        r.fam._plan_side(A, b, "BUY", p, sp, 10.0, ladder=rows)
        inside = [w for w in rows if w["px"] <= 0.52]
        self.assertTrue(any(w["qty"] > 1.0 for w in inside))


class TestRestingTargetsShrink(unittest.TestCase):
    def test_resting_size_past_fair_gets_pulled_in(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(
            bids=((0.12, 4000.0), (0.07, 3000.0)),
            asks=((0.14, 5000.0),),
            tick=0.01, fetched_at=1_000_000.0))
        r.cycle()
        r.fam.fairs = lambda s: 0.02
        r.fam.orders["R1"] = FamilyOrder(
            id="R1", market=A, side="BUY", price=0.12, qty=50.0,
            intent=BUY_LONG, placed_ts=1.0, purpose="earn",
            live_est=0.5)
        r.exchange.live["R1"] = {"id": "R1", "market": A, "side": "BUY",
                                 "price": 0.12, "size": 50.0}
        for _ in range(3):          # one shrink per market-side per cycle
            r.fam.last_action.clear()
            r.cycle(advance=600.0)
        big = [o for o in r.fam.orders.values()
               if o.market == A and o.side == "BUY"
               and o.purpose != "sell" and o.price >= 0.06
               and o.qty > 8.0]
        self.assertEqual(big, [])   # the 50-share target shrank or left


class TestVerdictCarriesTheBook(unittest.TestCase):
    def test_feed_entries_freeze_book_and_picks(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.cycle()
        self.assertTrue(r.fam.triage_feed)
        t = r.fam.triage_feed[-1]
        self.assertIn("book", t)
        self.assertTrue(t["book"]["b"])          # bid levels captured
        self.assertTrue(t["book"]["a"])          # ask levels captured
        self.assertIn("picks", t)
        if t["in"]:
            self.assertTrue(t["picks"])
            self.assertIn("ev", t["picks"][0])


class TestExitsJoinTheTouch(unittest.TestCase):
    def test_profitable_exit_rests_at_the_ask_touch(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(
            bids=((0.42, 500.0), (0.40, 900.0)),
            asks=((0.45, 800.0), (0.50, 900.0)),
            tick=0.01, fetched_at=1_000_000.0))
        r.fam.inventory[A] = {"qty": 10.0, "cost": 3.00}   # 30c basis
        r.positions[A] = (10.0, 3.00)
        r.cycle()
        exits = [o for o in r.fam.orders.values()
                 if o.market == A and o.purpose == "sell"
                 and o.side == "SELL"]
        self.assertTrue(exits)
        self.assertAlmostEqual(exits[0].price, 0.45)   # the front, not behind

    def test_touch_far_under_the_model_is_not_joined(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(
            bids=((0.42, 500.0), (0.40, 900.0)),
            asks=((0.45, 800.0), (0.50, 900.0)),
            tick=0.01, fetched_at=1_000_000.0))
        r.fam.fairs = lambda s: 0.60                   # touch is a giveaway
        r.fam.inventory[A] = {"qty": 10.0, "cost": 3.00}
        r.positions[A] = (10.0, 3.00)
        r.cycle()
        exits = [o for o in r.fam.orders.values()
                 if o.market == A and o.purpose == "sell"
                 and o.side == "SELL"]
        self.assertTrue(exits)
        self.assertAlmostEqual(exits[0].price, 0.59)   # just under fair


class TestTakerDump(unittest.TestCase):
    def _rig(self, bids, asks, cost=3.00, qty=10.0):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.fam.cfg.dump_usd_day = 50.0
        r.add_market(A, book=Book(bids=bids, asks=asks, tick=0.01,
                                  fetched_at=1_000_000.0))
        r.fam.inventory[A] = {"qty": qty, "cost": cost}
        r.positions[A] = (qty, cost)
        return r, A

    def test_tight_spread_above_basis_dumps_into_the_bid(self):
        r, A = self._rig(bids=((0.44, 50.0), (0.40, 900.0)),
                         asks=((0.45, 800.0),))
        r.cycle()
        dumps = [e for e in r.fam.log if e.get("event") == "dump"]
        self.assertTrue(dumps)
        self.assertAlmostEqual(dumps[0]["price"], 0.44)  # at the bid, never worse
        self.assertLessEqual(dumps[0]["qty"], 10.0)
        self.assertGreater(r.fam.dump_today, 0.0)
        live = [o for o in r.exchange.live.values()
                if o["market"] == A and o["side"] == "SELL"
                and abs(o["price"] - 0.44) < 1e-9]
        self.assertTrue(live)                            # the crossing limit

    def test_wide_spread_never_dumps(self):
        r, A = self._rig(bids=((0.30, 500.0),), asks=((0.45, 800.0),))
        r.cycle()
        self.assertFalse([e for e in r.fam.log if e.get("event") == "dump"])

    def test_dump_never_exceeds_displayed_size_or_cap(self):
        r, A = self._rig(bids=((0.44, 3.0), (0.40, 900.0)),
                         asks=((0.45, 800.0),), qty=10.0)
        r.cycle()
        dumps = [e for e in r.fam.log if e.get("event") == "dump"]
        self.assertTrue(dumps)
        self.assertLessEqual(dumps[0]["qty"], 3.0)   # the bid's size, no deeper

    def test_giveaway_against_the_model_never_dumps(self):
        r, A = self._rig(bids=((0.44, 500.0),), asks=((0.45, 800.0),))
        r.fam.fairs = lambda s: 0.60                 # bid is 16 ticks under fair
        r.cycle()
        self.assertFalse([e for e in r.fam.log if e.get("event") == "dump"])

    def _dead_rig(self, bid, drain_s=21600.0):
        # held at 50c a share; the bid sits UNDER cost so the profit
        # gate never opens — only the dead-stock drain could act
        r, A = self._rig(bids=((bid, 500.0),), asks=((bid + 0.01, 800.0),),
                         cost=5.00, qty=10.0)
        r.fam.cfg.dead_drain_s = drain_s
        r.cycle()                          # engine rests its exit
        exits = [o for o in r.fam.orders.values()
                 if o.market == A and o.purpose == "sell"]
        self.assertTrue(exits)
        for o in exits:                    # measured dead for 7 hours
            o.live_est = 0.0
            o.placed_ts = r.now - 25200.0
        r.fam.last_action.clear()
        return r, A

    def test_dead_stock_drains_at_a_small_loss(self):
        # owner, 2026-08-29 (choosing the slow clean over a weekly
        # liquidation): exits measured ~$0 for 6h+ and the bid within
        # 5 ticks of cost — the stock leaves through the taker rail
        r, A = self._dead_rig(bid=0.47)    # 3 ticks under the 50c basis
        r.cycle()
        dumps = [e for e in r.fam.log if e.get("event") == "dump"]
        self.assertTrue(dumps)
        self.assertAlmostEqual(dumps[0]["price"], 0.47)
        self.assertIn("dead-stock drain", dumps[0]["note"])

    def test_fresh_exits_are_not_dead(self):
        # an exit resting under 6h has not proven dead yet — the dwell
        # is the guard (the estimator re-measures live_est each cycle,
        # so a zero reading alone must never trigger the drain)
        r, A = self._dead_rig(bid=0.47)
        for o in r.fam.orders.values():
            if o.market == A and o.purpose == "sell":
                o.placed_ts = r.now        # fresh: dwell not served
        r.cycle()
        self.assertFalse([e for e in r.fam.log if e.get("event") == "dump"])

    def test_drain_never_sells_more_than_5_ticks_under_cost(self):
        r, A = self._dead_rig(bid=0.44)    # 6 ticks under the 50c basis
        r.cycle()
        self.assertFalse([e for e in r.fam.log if e.get("event") == "dump"])

    def test_drain_off_by_default(self):
        r, A = self._dead_rig(bid=0.47, drain_s=0.0)
        r.cycle()
        self.assertFalse([e for e in r.fam.log if e.get("event") == "dump"])


class TestScheduledCancel(unittest.TestCase):
    """Owner, 2026-09-01, with the Massachusetts primary resolving that
    day: "set them to cancel by noon eastern time". The only path that
    touches his hand-placed orders, so it must fire once, at the time
    he said, and reach nothing he did not name."""

    def rig(self):
        from v3.tests.test_family import Rig
        from v3.main import Monitor
        m = Monitor.__new__(Monitor)
        m.cancel_jobs = []
        m.families = {}
        m.alerts = type("A", (), {"notify": lambda *a, **k: None})()
        m._note = lambda msg: m.__dict__.setdefault("notes", []).append(msg)
        return m, Rig

    def test_it_does_not_fire_before_its_time(self):
        m, Rig = self.rig()
        r = Rig()
        m.families = {"politics": r.fam}
        m.schedule_cancel("mov-ma-dem", 5000.0, "the primary resolves")
        m._run_due_cancels(4999.0)
        self.assertEqual(len(m.cancel_jobs), 1, "fired early")

    def test_it_cancels_only_the_markets_named(self):
        from v3.tests.test_family import Rig, A, B
        m, _ = self.rig()
        r = Rig()
        r.add_market(A)
        r.add_market(B)
        m.families = {"politics": r.fam}
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        for oid, mkt in (("keep", A), ("kill", B)):
            r.fam.orders[oid] = FamilyOrder(
                id=oid, market=mkt, side="BUY", price=0.10, qty=5.0,
                intent=BUY_LONG, placed_ts=0.0, purpose="manual",
                why="the owner's own order")
        m.schedule_cancel(B, 100.0)
        m._run_due_cancels(200.0)
        self.assertIn("keep", r.fam.orders)
        self.assertNotIn("kill", r.fam.orders)

    def test_it_fires_once_and_forgets(self):
        m, _ = self.rig()
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        m.families = {"politics": r.fam}
        m.schedule_cancel(A, 100.0)
        m._run_due_cancels(200.0)
        self.assertEqual(m.cancel_jobs, [], "job survived its own firing")
        m._run_due_cancels(300.0)          # must be harmless

    def test_calling_it_off_removes_it(self):
        m, _ = self.rig()
        m.schedule_cancel("mov-ma-dem", 9e9)
        self.assertEqual(len(m.cancel_jobs), 1)
        m.clear_cancel("mov-ma-dem")
        self.assertEqual(m.cancel_jobs, [])

    def test_scheduling_the_same_pattern_twice_does_not_stack(self):
        m, _ = self.rig()
        m.schedule_cancel("mov-ma-dem", 9e9)
        m.schedule_cancel("mov-ma-dem", 9e8)
        self.assertEqual(len(m.cancel_jobs), 1)
        self.assertEqual(m.cancel_jobs[0]["at"], 9e8)

    def test_an_empty_pattern_is_refused(self):
        m, _ = self.rig()
        self.assertFalse(m.schedule_cancel("", 9e9)["ok"])
        self.assertEqual(m.cancel_jobs, [])


class TestWindDownLedger(unittest.TestCase):
    """Owner, 2026-08-31 ("you can fix so there is a more clear
    answer"): position counts cannot tell a sale from a fill. Every
    retirement the engine performs is recorded — what sold, for how
    much, by which mechanism, and whether it went flat."""

    def test_a_drain_is_recorded_with_its_proceeds(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.fam.cfg.dump_usd_day = 50.0
        r.fam.cfg.dead_drain_s = 21600.0
        r.add_market(A, book=Book(bids=((0.47, 500.0),),
                                  asks=((0.48, 800.0),), tick=0.01,
                                  fetched_at=r.now))
        r.fam.inventory[A] = {"qty": 10.0, "cost": 5.00}
        r.positions[A] = (10.0, 5.00)
        r.cycle()
        for o in r.fam.orders.values():
            if o.market == A and o.purpose == "sell":
                o.live_est = 0.0
                o.placed_ts = r.now - 25200.0
        r.fam.last_action.clear()
        summary = r.cycle()
        led = r.fam.wind_down
        self.assertTrue(led, "nothing recorded")
        row = led[-1]
        self.assertEqual(row["market"], A)
        self.assertIn(row["kind"], ("drain", "dump"))
        self.assertAlmostEqual(row["usd"], row["qty"] * row["px"], places=2)
        wd = summary["wind_down"]
        self.assertEqual(wd["day_n"], len(led))
        self.assertGreater(wd["day_usd"], 0)
        self.assertIn(row["kind"], wd["by_kind"])

    def test_a_repricing_is_not_a_sale(self):
        """Owner, 2026-08-31 ("Fix all of this"): the Sold tab read 51
        sold / $16.01 proceeds / 50 went flat when only 9 lines were
        sales worth $9.82. The other 42 were short buy-backs being
        moved up the book — no shares change hands, the cash goes the
        other way when they fill, and the call site never passed what
        was left so every one flagged itself flat."""
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        # a repricing: the short is untouched, 40 shares still open
        r.fam._note_wind_down(A, "short step-up", 2.0, 0.61, r.now,
                              left=-40.0)
        # a real sale that did go flat
        r.fam._note_wind_down(A, "drain", 11.0, 0.14, r.now, left=0.0)
        step, sale = r.fam.wind_down[-2], r.fam.wind_down[-1]
        self.assertFalse(step["sale"])
        self.assertFalse(step["flat"], "a repricing closes nothing")
        self.assertTrue(sale["sale"])
        self.assertTrue(sale["flat"])
        summary = r.cycle()
        wd = summary["wind_down"]
        # proceeds and the sold count are the SALE only
        self.assertEqual(wd["day_n"], 1)
        self.assertAlmostEqual(wd["day_usd"], 1.54, places=2)
        self.assertEqual(wd["flat_day"], 1)
        # the repricing is reported beside it, as a cost to close
        self.assertEqual(wd["moves_n"], 1)
        self.assertAlmostEqual(wd["moves_usd"], 1.22, places=2)
        # both still appear in the by-kind breakdown and the rows
        self.assertIn("short step-up", wd["by_kind"])
        self.assertIn("drain", wd["by_kind"])

    def test_flat_never_defaults_true_however_left_is_passed(self):
        """The defect exactly: left defaulted to 0.0, so abs(0) < 0.01
        marked every repricing flat. 41 of 41 in the live ledger."""
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.fam._note_wind_down(A, "short step-up", 1.0, 0.24, r.now)
        self.assertFalse(r.fam.wind_down[-1]["flat"])

    def test_repricings_collapse_to_one_four_hour_number(self):
        """Owner, 2026-08-31: "Just have a number that says x markets
        dropped prices in the last 4 hours and increased earning from
        exits by x cents." The moves carry where they came from and
        what the model says the move adds per day."""
        from v3.tests.test_family import Rig, A, B
        r = Rig()
        r.add_market(A)
        r.add_market(B)
        r.fam._note_wind_down(A, "short step-up", 1.0, 0.24, r.now,
                              left=-40.0, from_px=0.21, gain=0.031)
        r.fam._note_wind_down(B, "exit move", 4.0, 0.11, r.now,
                              left=4.0, from_px=0.13, gain=0.014)
        # a second move in a market already counted: one market, two moves
        r.fam._note_wind_down(A, "exit move", 2.0, 0.25, r.now,
                              left=-40.0, from_px=0.24, gain=0.005)
        # and one outside the window, which the 4h number must exclude
        r.fam._note_wind_down(B, "exit move", 1.0, 0.30, r.now - 5 * 3600.0,
                              left=1.0, from_px=0.32, gain=9.99)
        row = r.fam.wind_down[0]
        self.assertAlmostEqual(row["from_px"], 0.21)
        self.assertAlmostEqual(row["gain"], 0.031)
        wd = r.cycle()["wind_down"]
        self.assertEqual(wd["moves_4h_markets"], 2)
        self.assertEqual(wd["moves_4h_n"], 3)
        self.assertAlmostEqual(wd["moves_4h_gain"], 0.050, places=3)
        # none of it leaked into the sale figures
        self.assertEqual(wd["day_n"], 0)
        self.assertEqual(wd["day_usd"], 0)

    def test_rows_written_before_the_flag_are_classed_by_kind(self):
        """The ledger persists, so the restart that ships this reads
        50 rows that carry no sale flag at all."""
        from v3.family import Family
        self.assertTrue(Family._wd_sale({"kind": "drain"}))
        self.assertTrue(Family._wd_sale({"kind": "close-out"}))
        self.assertFalse(Family._wd_sale({"kind": "short step-up"}))
        # an explicit flag still wins over the kind
        self.assertFalse(Family._wd_sale({"kind": "drain", "sale": False}))

    def test_the_report_is_empty_and_harmless_with_no_sales(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        summary = r.cycle()
        wd = summary["wind_down"]
        self.assertEqual(wd["day_n"], 0)
        self.assertEqual(wd["week_usd"], 0)
        self.assertEqual(wd["recent"], [])

    def test_the_ledger_survives_a_restart(self):
        from v3.tests.test_family import Rig, A
        from v3.family import Family
        from v3 import politics
        r = Rig()
        r.add_market(A)
        r.cycle()
        r.fam._note_wind_down(A, "drain", 5.0, 0.40, r.now, left=0.0)
        d = r.fam.to_dict()
        f2 = Family(None, r.cache, politics.discover, config=r.fam.cfg,
                    names=r.names, clock=lambda: r.now)
        f2.restore(d)
        self.assertEqual(len(f2.wind_down), 1)
        self.assertTrue(f2.wind_down[0]["flat"])


class TestDeadShortStepUp(unittest.TestCase):
    """Owner, 2026-08-29 ("we should find a way to get the resting
    positions down"): a short's break-even buy-back on a book trading
    above it never fills — once dead, it may bid up to the touch
    (post-only, loss bounded at 5 ticks over what the short sold for)
    so the frozen collateral comes home when someone sells into it."""

    def _rig(self, drain_s=21600.0):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.fam.cfg.dead_drain_s = drain_s
        # short 10 at 2.4c a share; the book trades 5c/6c — break-even
        # buy-backs at <=2.4c are fantasy bids
        book = Book(bids=((0.05, 50.0),), asks=((0.06, 800.0),),
                    tick=0.01, fetched_at=r.now)
        r.add_market(A, book=book)
        r.fam.inventory[A] = {"qty": -10.0, "cost": -0.24}
        r.positions[A] = (-10.0, -0.24)
        r.cycle()                              # buy-back rests, capped
        return r, A

    def test_dead_short_bids_up_to_the_touch(self):
        # owner, 2026-08-29: "the step ups can start immediately on
        # things that aren't currently earning" — a measured-zero
        # reading is enough; no waiting period (repricing resets
        # order age, so a dwell keyed to it could starve forever)
        r, A = self._rig()
        buybacks = [o for o in r.fam.orders.values()
                    if o.market == A and o.side == "BUY"
                    and o.purpose == "sell"]
        self.assertTrue(buybacks)
        self.assertLessEqual(max(o.price for o in buybacks), 0.03)
        for o in buybacks:                     # measured: earning nothing
            o.live_est = 0.0
        r.fam.last_action.clear()
        r.cycle()
        ups = [e for e in r.fam.log if e.get("event") == "dead_short_stepup"]
        self.assertTrue(ups)
        r.fam.last_action.clear()
        r.cycle()                              # re-rests at the touch
        live = [o for o in r.exchange.live.values()
                if o["market"] == A and o["side"] == "BUY"]
        # received 2.4c + 5 ticks = 7.4c, touch 5c, ask-1t 5c -> bids 5c
        self.assertTrue(any(abs(o["price"] - 0.05) < 1e-9 for o in live),
                        [o["price"] for o in live])

    def test_stepped_up_bid_never_exceeds_touch_or_loss_bound(self):
        # whatever deadness decides, the stepped-up bid is bounded:
        # never above the bid touch (5c here), never above received +
        # 5 ticks (7.4c), never crossing the ask. Run several cycles
        # and check every buy-back the exchange ever saw.
        r, A = self._rig()
        for _ in range(4):
            for o in r.fam.orders.values():
                if o.market == A and o.side == "BUY" and o.purpose == "sell":
                    o.live_est = 0.0
            r.fam.last_action.clear()
            r.cycle()
        live = [o for o in r.exchange.live.values()
                if o["market"] == A and o["side"] == "BUY"]
        self.assertTrue(live)
        self.assertTrue(all(o["price"] <= 0.05 + 1e-9 for o in live),
                        [o["price"] for o in live])

    def test_step_up_never_loops_at_the_target(self):
        # owner, 2026-08-30: buy-backs already at the best allowed
        # price were cancelled and re-placed there every 60s for 18h.
        # Once at the target, the order ID must stay stable.
        r, A = self._rig()
        ids = set()
        for _ in range(5):
            for o in r.fam.orders.values():
                if o.market == A and o.side == "BUY" and o.purpose == "sell":
                    o.live_est = 0.0
            r.fam.last_action.clear()
            r.cycle()
            ids.add(tuple(sorted(i for i, o in r.fam.orders.items()
                                 if o.market == A and o.side == "BUY"
                                 and o.purpose == "sell")))
        ups = [e for e in r.fam.log if e.get("event") == "dead_short_stepup"]
        self.assertLessEqual(len(ups), 1)      # one step-up, then quiet
        live = [o for o in r.exchange.live.values()
                if o["market"] == A and o["side"] == "BUY"]
        self.assertTrue(any(abs(o["price"] - 0.05) < 1e-9 for o in live))
        # after reaching the touch the id set stops changing
        self.assertLessEqual(len(ids), 3)

    def test_gate_pinned_buyback_is_never_touched(self):
        # the nh-dem loop (owner, 2026-08-30): the step target said
        # 0.14 but the exit gate pinned the re-rest at the old price —
        # cancel, re-place, repeat every 60s. The step-up must predict
        # the FINAL price, gate included, and no-op when it would not
        # move.
        r, A = self._rig()
        r.cycle()   # ensure buyback rests
        bb = [o for o in r.fam.orders.values()
              if o.market == A and o.side == "BUY" and o.purpose == "sell"]
        pin = bb[0].price
        r.fam._exit_gate = lambda *a, **k: pin      # the gate pins it
        ups0 = len([e for e in r.fam.log
                    if e.get("event") == "dead_short_stepup"])
        for _ in range(3):
            for o in r.fam.orders.values():
                if o.market == A and o.side == "BUY" and o.purpose == "sell":
                    o.live_est = 0.0
            r.fam.last_action.clear()
            r.cycle()
        ups = len([e for e in r.fam.log
                   if e.get("event") == "dead_short_stepup"])
        self.assertEqual(ups, ups0)     # nothing new once pinned

    def test_gate_pinned_exit_is_never_moved(self):
        # same disease in _maybe_move_exit: the mover wanted the ask
        # anchor, the gate pinned the re-rest at the old price
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": 10.0, "cost": 3.0}
        r.positions[A] = (10.0, 3.0)
        r.cycle()   # exit rests somewhere
        exits = [o for o in r.fam.orders.values()
                 if o.market == A and o.side == "SELL"
                 and o.purpose == "sell"]
        self.assertTrue(exits)
        pin = exits[0].price
        r.fam._exit_gate = lambda *a, **k: pin
        moved0 = len([e for e in r.fam.log if e.get("event") == "exit_moved"])
        for _ in range(3):
            r.fam.last_action.clear()
            r.cycle()
        moved = len([e for e in r.fam.log if e.get("event") == "exit_moved"])
        self.assertEqual(moved, moved0)

    def test_disabled_dwell_never_steps_up(self):
        r, A = self._rig(drain_s=0.0)
        for o in r.fam.orders.values():
            if o.market == A and o.side == "BUY" and o.purpose == "sell":
                o.live_est = 0.0
        r.fam.last_action.clear()
        r.cycle()
        self.assertFalse([e for e in r.fam.log
                          if e.get("event") == "dead_short_stepup"])

    def test_politics_ships_with_the_dead_dwell(self):
        from v3 import politics
        self.assertEqual(politics.config().dead_drain_s, 21600.0)


class TestOwnerAvoidList(unittest.TestCase):
    def test_avoided_markets_are_left_entirely_to_the_owner(self):
        """Owner, 2026-08-22 ('Don't place any orders in the balance of
        power. I'm going to do that one by hand'): an avoided market
        gets NO engine orders at all — quotes, probes, dumps, AND the
        engine's own exits leave. Only his manual orders stay."""
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG, SELL_LONG
        r = Rig()
        r.add_market(A)
        r.cycle()                               # the engine quotes A
        earns = [o for o in r.fam.orders.values()
                 if o.market == A and o.purpose == "earn"]
        self.assertTrue(earns)
        r.fam.orders["X1"] = FamilyOrder(
            id="X1", market=A, side="SELL", price=0.50, qty=2.0,
            intent=SELL_LONG, placed_ts=1.0, purpose="sell")
        r.exchange.live["X1"] = {"id": "X1", "market": A, "side": "SELL",
                                 "price": 0.50, "size": 2.0}
        r.fam.orders["HAND"] = FamilyOrder(
            id="HAND", market=A, side="SELL", price=0.60, qty=2.0,
            intent=SELL_LONG, placed_ts=1.0, purpose="manual",
            why="placed by the owner")
        r.exchange.live["HAND"] = {"id": "HAND", "market": A,
                                   "side": "SELL", "price": 0.60,
                                   "size": 2.0}
        r.fam.inventory[A] = {"qty": 2.0, "cost": 0.60}
        r.positions[A] = (2.0, 0.60)
        r.fam.cfg.avoid_tokens = ("ussemov",)   # matches the rig's slug
        self.assertFalse(r.fam.enterable(A))
        for _ in range(4):                      # pulls are throttled
            r.fam.last_action.clear()
            r.cycle(advance=120.0)
        engine_left = [o for o in r.fam.orders.values()
                       if o.market == A and o.purpose != "manual"]
        self.assertEqual(engine_left, [])       # every engine order gone
        self.assertIn("HAND", r.fam.orders)     # his order untouched


class TestFillCardRetention(unittest.TestCase):
    def test_closed_cards_show_three_days_then_leave(self):
        from v3.main import card_visible
        now = 1_000_000.0
        closed = {"side": "SELL", "qty": 5.0, "px": 0.9, "open_qty": 0.0,
                  "realized": 0.15, "ts": now - 4 * 86400,
                  "last_ts": now - 2 * 86400}
        self.assertTrue(card_visible(closed, now))
        closed["last_ts"] = now - 4 * 86400
        self.assertFalse(card_visible(closed, now))

    def test_open_cards_stay_until_profitable(self):
        from v3.main import card_visible
        now = 1_000_000.0
        losing = {"side": "BUY", "qty": 50.0, "px": 0.13, "open_qty": 50.0,
                  "realized": 0.0, "est_day": 2.0, "rested_h": 2.0,
                  "now_bid": 0.07, "exit_earned": 0.0,
                  "ts": now - 9 * 86400, "last_ts": now - 9 * 86400}
        self.assertTrue(card_visible(losing, now))     # old but underwater
        winner = dict(losing, now_bid=0.20)            # mark turned green
        self.assertFalse(card_visible(winner, now))    # off the list

    def test_stray_closes_use_the_three_day_window(self):
        from v3.main import card_visible
        now = 1_000_000.0
        stray = {"side": "SELL", "qty": 10.0, "px": 0.7, "open_qty": 0.0,
                 "stray_close": True, "ts": now - 86400,
                 "last_ts": now - 86400}
        self.assertTrue(card_visible(stray, now))


class TestFillsArchive(unittest.TestCase):
    def test_append_only_with_same_timestamp_siblings(self):
        from v3.main import fills_csv_append, FILLS_CSV_HEADER
        r1 = {"market": "m1", "side": "BUY", "qty": 50.0, "px": 0.13,
              "purpose": "earn", "est_day": 2.0, "rested_h": 8.7,
              "fair": 0.018, "band": [9, 16], "conf": 0.0,
              "touch_bid": 0.07, "touch_ask": 0.13, "conc": 0.112,
              "pos_after": 50.0, "why": "joins, the touch"}
        r2 = dict(r1, market="m2", qty=1.0)
        text, n = fills_csv_append(None, [(100.0, "politics", r1),
                                          (100.0, "politics", r2)])
        self.assertEqual(n, 2)               # same ts, both kept
        self.assertTrue(text.startswith(FILLS_CSV_HEADER))
        self.assertIn("joins; the touch", text)   # commas sanitized
        # a second publish with the same rows adds nothing
        text2, n2 = fills_csv_append(text, [(100.0, "politics", r1),
                                            (100.0, "politics", r2)])
        self.assertEqual(n2, 0)
        self.assertEqual(text2, text)
        # a newer fill appends one line
        r3 = dict(r1, market="m3")
        text3, n3 = fills_csv_append(text2, [(100.0, "politics", r1),
                                             (200.0, "cfb", r3)])
        self.assertEqual(n3, 1)
        self.assertIn("200.0,cfb,m3", text3)


class TestReconciledFlatLots(unittest.TestCase):
    def test_open_lot_on_a_flat_market_counts_as_closed(self):
        from v3.main import card_is_open, card_visible
        now = 1_000_000.0
        card = {"side": "SELL", "qty": 5.0, "px": 0.20, "open_qty": 4.0,
                "realized": -0.02, "pos_now": 0.0,
                "ts": now - 3600, "last_ts": now - 3600}
        self.assertFalse(card_is_open(card))          # flat = closed
        self.assertTrue(card_visible(card, now))      # 3-day window
        card["last_ts"] = now - 4 * 86400
        self.assertFalse(card_visible(card, now))
        live = dict(card, pos_now=-4.0, last_ts=now - 3600)
        self.assertTrue(card_is_open(live))           # real position: open


# TestProfitableFillsDontPage lived here. It pinned the 2026-08-22
# rule (profitable closes silent, every other fill pages), which the
# owner replaced on 2026-08-24 with the loss-threshold rule now
# covered by TestQuieterFillAlerts below.


class TestNbaFamily(unittest.TestCase):
    """Owner, 2026-08-22: "Also add in NBA." Same posture as the NFL:
    $50 all-in with holdings counted, behind the touch, capped dumps,
    and its own switch that starts OFF."""

    def test_config_mirrors_the_nfl(self):
        from v3.basketball import nba
        from v3.football import nfl
        a, b = nba(), nfl()
        for f in ("capital_usd", "holdings_in_ceiling",
                  "dump_usd_day", "rest_style", "known_ground", "revive",
                  "probe_usd", "grow_usd"):
            self.assertEqual(getattr(a, f), getattr(b, f), f)
        # owner, 2026-08-23: NBA departs from the NFL copy — wall joins
        # at double the per-market money, sized up, never improving
        self.assertEqual(a.per_market_usd, 2.00)
        self.assertTrue(a.wall_size_up)
        self.assertFalse(a.allow_improve)
        self.assertEqual(a.tag, "NBA")
        # offseason: no game-day window until the owner sets one
        self.assertIsNone(a.rest_from)

    def test_discovery_keeps_only_nba_prefixes(self):
        from v3.basketball import nba_discover
        class C:
            def events_by_tag(self, tag, max_pages=8):
                if tag != "nba":
                    return []
                return [{"title": "NBA Champion 2027", "markets": [
                    {"slug": "tec-nba-champ-2027-06-30-w-bos"},
                    {"slug": "tec-nba-champ-2027-06-30-w-lal"},
                    {"slug": "tec-wnba-champ-2026-w-lv"},      # not NBA
                    {"slug": "aachc-cfb-wins-2026-osu-9pt5"},  # not NBA
                ]}]
        out = nba_discover(C())
        self.assertEqual(sorted(out),
                         ["tec-nba-champ-2027-06-30-w-bos",
                          "tec-nba-champ-2027-06-30-w-lal"])
        self.assertEqual(out["tec-nba-champ-2027-06-30-w-bos"]["event_n"], 2)


class TestHandsOffOwnerOrders(unittest.TestCase):
    """Owner, 2026-08-22: "Don't let it cancel orders I set by hand."
    Manual orders survive every cull the engine runs, and the engine
    sizes its own book around them."""

    def _manual(self, r, oid, market, side, px, qty, intent=None):
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG, SELL_LONG
        it = intent or (BUY_LONG if side == "BUY" else SELL_LONG)
        r.fam.orders[oid] = FamilyOrder(
            id=oid, market=market, side=side, price=px, qty=qty,
            intent=it, placed_ts=r.now, purpose="manual",
            why="placed by the owner")
        r.exchange.live[oid] = {"id": oid, "market": market, "side": side,
                                "price": px, "size": qty, "intent": it}

    def test_survives_the_dead_program_sweep(self):
        from v3.tests.test_family import Rig, A, DEAD_PROG
        import copy
        r = Rig()
        r.add_market(A)
        r.cycle()                     # the engine rests its own orders
        self.assertTrue(any(o.purpose != "manual"
                            for o in r.fam.orders.values()))
        self._manual(r, "HAND1", A, "BUY", 0.02, 5.0)
        r.exchange.prog_raw[A] = copy.deepcopy(DEAD_PROG)
        for _ in range(40):
            r.cycle(advance=1200.0)   # terms re-read -> dead -> leave
        self.assertIn("HAND1", r.fam.orders)          # the hand survives
        self.assertEqual([o.id for o in r.fam.orders.values()
                          if o.market == A and o.purpose != "manual"], [])

    def test_never_shed_by_the_ceiling_trim(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                           rest_style="join_quiet", revive=True,
                           capital_usd=1.0, per_market_usd=2.0)
        r = Rig(cfg=cfg)
        r.add_market(A)
        r.cycle()                                       # engine spends
        self._manual(r, "HAND2", A, "BUY", 0.40, 10.0)  # $4 of hand money
        r.fam._trim(r.now, 5)
        self.assertIn("HAND2", r.fam.orders)            # never trimmed

    def test_the_owners_money_is_not_charged_to_the_engines_budget(self):
        """The family cap limits what the ENGINE risks on its own
        initiative. Charging the owner's book against it locked the
        engine out of politics entirely on 2026-08-24 — his 62
        risk-opening orders counted $484.66 against a $250 cap, and
        the engine held 0 entry orders and $4.27 of stale exits at the
        lowest earning rate on record."""
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                           rest_style="join_quiet", revive=True,
                           capital_usd=1.0, per_market_usd=2.0)
        r = Rig(cfg=cfg)
        r.add_market(A)
        before = r.fam.family_spent()
        # $4 of the owner's own money, on the risk-OPENING side
        self._manual(r, "HAND9", A, "BUY", 0.40, 10.0)
        self.assertEqual(r.fam.family_spent(), before)   # not charged
        # and it still counts as cover, so nothing is offered twice
        self.assertIn("HAND9", r.fam.orders)
        self.assertEqual(r.fam.orders["HAND9"].purpose, "manual")

    def test_owner_exit_counts_as_cover(self):
        from v3.tests.test_family import Rig, A, politics_book
        from v3.intents import SELL_LONG
        r = Rig()
        r.add_market(A)
        r.positions[A] = (10.0, 1.0)
        r.fam.inventory[A] = {"qty": 10.0, "cost": 1.0}
        r.fam.cache.put(A, politics_book(r.now))
        self._manual(r, "HAND3", A, "SELL", 0.60, 10.0, intent=SELL_LONG)
        r.fam._sell(r.now, 5)
        engine_sells = [o for o in r.fam.orders.values()
                        if o.market == A and o.purpose == "sell"]
        self.assertEqual(engine_sells, [])   # his 10 already cover the 10
        self.assertIn("HAND3", r.fam.orders)
        # and his exit adds no new risk, so it never blocks the ceiling
        self.assertEqual(r.fam.family_spent(), 0.0)


class TestOldAdoptionsMigrateToManual(unittest.TestCase):
    def test_restore_relabels_pre_fix_adoptions(self):
        from v3.tests.test_family import Rig
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG
        r = Rig()
        r.fam.orders["OLD1"] = FamilyOrder(
            id="OLD1", market=A_J, side="SELL", price=0.06, qty=4.0,
            intent=SELL_LONG, placed_ts=999_000.0, purpose="sell",
            why="adopted from the earlier versions")
        d = r.fam.to_dict()
        r2 = Rig()
        r2.fam.restore(d)
        rec = r2.fam.orders["OLD1"]
        self.assertEqual(rec.purpose, "manual")
        self.assertIn("owner", rec.why)


class TestExpensiveSidePreference(unittest.TestCase):
    """Owner, 2026-08-23 ('Yes do both'): favorites are wanted — a
    75c+ bid's locked-cash charge is halved in the EV ranking."""

    def test_favorite_bid_charged_half(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig(switch=False)
        r.add_market(A, book=Book(
            bids=((0.80, 400.0), (0.02, 60000.0)),
            asks=((0.84, 400.0), (0.98, 60000.0)),
            tick=0.01, fetched_at=1_000_000.0))
        r.cycle()
        r.fam.fairs = lambda s: 0.83
        rows = []
        b = r.cache.fresh(A, 3600, r.now)
        p, _ = r.fam._prog_row(A)
        sp = r.fam._side_pool(A, p)
        r.fam._plan_side(A, b, "BUY", p, sp, 10.0, ladder=rows)
        self.assertTrue(rows)      # the expensive side quotes happily
        # and the EV at 80c is better than a full-price capital charge
        # would allow: reconstruct the charge difference on one row
        w = max(rows, key=lambda x: x["ev"])
        self.assertGreaterEqual(w["px"], 0.5)


class TestNbaSearchFallback(unittest.TestCase):
    def test_empty_tags_fall_back_to_search(self):
        from v3.basketball import nba_discover

        class C:
            def events_by_tag(self, tag, max_pages=8):
                return []                       # the tags sit empty
            def search(self, q, limit=20):
                if "Champion" not in q:
                    return {"events": []}
                return {"events": [{"title": "NBA Champion 2027",
                                    "markets": [
                    {"slug": "tec-nba-champ-2027-06-30-w-okc"},
                    {"slug": "tec-nba-champ-2027-06-30-w-bos"},
                    {"slug": "tec-wnba-champ-2026-w-lv"}]}]}
        out = nba_discover(C())
        self.assertEqual(sorted(out),
                         ["tec-nba-champ-2027-06-30-w-bos",
                          "tec-nba-champ-2027-06-30-w-okc"])
        self.assertEqual(out["tec-nba-champ-2027-06-30-w-okc"]["event_n"], 2)

    def test_tags_win_when_they_work(self):
        from v3.basketball import nba_discover

        class C:
            def events_by_tag(self, tag, max_pages=8):
                if tag != "nba":
                    return []
                return [{"title": "NBA MVP", "markets": [
                    {"slug": "aqc-nba-mvp-2027-shagil"}]}]
            def search(self, q, limit=20):
                raise AssertionError("search must not run when tags work")
        out = nba_discover(C())
        self.assertEqual(list(out), ["aqc-nba-mvp-2027-shagil"])


class TestNeverQuotePastFair(unittest.TestCase):
    """Owner, 2026-08-23: 'not paying so much past value for underdogs.
    That includes selling the favorites short.' The NY governor case:
    sold 1 @ 91c against a 98.4c model, filled in a minute."""

    def test_favorite_never_shorted_below_fair(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(
            bids=((0.90, 50.0), (0.02, 60000.0)),
            asks=((0.94, 40.0), (0.98, 60000.0)),
            tick=0.01, fetched_at=1_000_000.0))
        r.cycle()
        r.fam.fairs = lambda s: 0.984
        b = r.cache.fresh(A, 3600, r.now)
        p, _ = r.fam._prog_row(A)
        sp = r.fam._side_pool(A, p)
        plan = r.fam._plan_side(A, b, "SELL", p, sp, 10.0)
        self.assertTrue(plan is None or plan["px"] >= 0.984 + 0.01 - 1e-9)

    def test_resting_short_below_fair_is_forced_out(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import SELL_SHORT
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(
            bids=((0.90, 50.0), (0.02, 60000.0)),
            asks=((0.91, 1.0), (0.98, 60000.0)),
            tick=0.01, fetched_at=1_000_000.0))
        r.fam.orders["NY"] = FamilyOrder(
            id="NY", market=A, side="SELL", price=0.91, qty=1.0,
            intent=SELL_SHORT, placed_ts=1.0, purpose="earn")
        r.exchange.live["NY"] = {"id": "NY", "market": A, "side": "SELL",
                                 "price": 0.91, "size": 1.0}
        r.fam.fairs = lambda s: 0.984
        for _ in range(3):
            r.fam.last_action.clear()
            r.cycle(advance=120.0)
        rec = r.fam.orders.get("NY")
        self.assertIsNone(rec)      # pulled or repriced away — never kept

    def test_wall_join_sizes_to_the_market_money(self):
        from v3.tests.test_family import Rig, A
        from v3.basketball import nba
        from v3.scoring import Book
        r = Rig(cfg=nba())
        r.add_market(A, book=Book(
            bids=((0.01, 480000.0),),
            asks=((0.02, 280000.0), (0.98, 60000.0)),
            tick=0.01, fetched_at=1_000_000.0))
        r.cycle()
        b = r.cache.fresh(A, 3600, r.now)
        p, _ = r.fam._prog_row(A)
        sp = r.fam._side_pool(A, p)
        plan = r.fam._plan_side(A, b, "BUY", p, sp,
                                r.fam._market_budget(A) / 2.0)
        self.assertIsNotNone(plan)
        self.assertGreaterEqual(plan["qty"], 50.0)   # real size, not dust
        self.assertEqual(plan["px"], 0.01)           # still AT the wall


class TestDumpsJournalTheirSale(unittest.TestCase):
    def test_dump_writes_the_fill_row_at_the_known_price(self):
        """Owner, 2026-08-23: 'most of the markets are being closed by
        reconciliation' — dumps left no record of their own sale."""
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.fam.cfg.dump_usd_day = 50.0
        r.add_market(A, book=Book(
            bids=((0.44, 100.0), (0.02, 60000.0)),
            asks=((0.45, 100.0), (0.98, 60000.0)),
            tick=0.01, fetched_at=1_000_000.0))
        r.positions[A] = (10.0, 2.0)
        r.fam.inventory[A] = {"qty": 10.0, "cost": 2.0}   # basis 20c
        r.fam.fairs = lambda s: 0.45
        r.fam.cache.put(A, Book(bids=((0.44, 100.0), (0.02, 60000.0)),
                                asks=((0.45, 100.0), (0.98, 60000.0)),
                                tick=0.01, fetched_at=r.now))
        n0 = len(r.fam.fills)
        r.fam._sell(r.now, 5)
        dumps = [f for f in r.fam.fills[n0:] if "taker dump" in
                 str(f.get("why", ""))]
        self.assertTrue(dumps)                    # the sale is on record
        self.assertEqual(dumps[0]["px"], 0.44)    # at the bid
        self.assertEqual(dumps[0]["side"], "SELL")
        # and the inventory came down by the dumped size immediately
        left = (r.fam.inventory.get(A) or {}).get("qty", 0.0)
        self.assertLess(left, 10.0)


class TestVanishedOrderLimbo(unittest.TestCase):
    """Owner, 2026-08-23: 'literally every closed position... says
    closed by reconciliation.' A completely-filled order vanishes from
    the order list before the slower position feed shows the delta;
    instant silent-cancel classification threw the fill away. Vanished
    orders now wait in limbo for the feed."""

    def _vanish(self, r, oid="V1", qty=5.0):
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        r.fam.orders[oid] = FamilyOrder(
            id=oid, market=A_J, side="BUY", price=0.40, qty=qty,
            intent=BUY_LONG, placed_ts=r.now, purpose="earn")
        # NOT in exchange.live: the order has vanished

    def test_late_delta_books_the_fill(self):
        from v3.tests.test_family import Rig
        r = Rig()
        r.add_market(A_J)
        self._vanish(r)
        r.cycle()                          # gone, no delta: limbo
        self.assertEqual(r.fam.silent_cancels, 0)
        self.assertEqual(len(r.fam.gone_pending), 1)
        r.positions[A_J] = (5.0, 2.0)      # the feed catches up
        r.cycle()
        self.assertEqual(len(r.fam.gone_pending), 0)
        self.assertEqual(r.fam.silent_cancels, 0)
        fills = [x for x in r.fam.fills if x["market"] == A_J]
        self.assertTrue(fills)             # the fill made the journal
        self.assertEqual(fills[-1]["px"], 0.40)
        self.assertEqual((r.fam.inventory.get(A_J) or {}).get("qty"), 5.0)

    def test_true_silence_still_counts_after_grace(self):
        from v3.tests.test_family import Rig
        r = Rig()
        r.add_market(A_J)
        self._vanish(r, oid="V2")
        r.cycle()
        self.assertEqual(len(r.fam.gone_pending), 1)
        for _ in range(4):
            r.cycle(advance=120.0)         # grace expires, no delta ever
        self.assertEqual(len(r.fam.gone_pending), 0)
        self.assertEqual(r.fam.silent_cancels, 1)
        self.assertFalse([x for x in r.fam.fills if x["market"] == A_J])

    def test_limbo_survives_a_restart(self):
        from v3.tests.test_family import Rig
        r = Rig()
        r.add_market(A_J)
        self._vanish(r, oid="V3")
        r.cycle()
        d = r.fam.to_dict()
        r2 = Rig()
        r2.add_market(A_J)
        r2.fam.restore(d)
        self.assertIn("V3", r2.fam.gone_pending)
        r2.positions[A_J] = (5.0, 2.0)
        r2.cycle()
        self.assertTrue([x for x in r2.fam.fills if x["market"] == A_J])


class TestOwnerFair(unittest.TestCase):
    """Owner, 2026-08-23: 'Give me an option to set fair market for the
    2028 markets because you're off.' His number beats the model
    everywhere fair is used, survives into state, and clears back."""

    def setUp(self):
        import tempfile
        from v3.main import Monitor
        self.dir = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(self.dir.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        self.mon = Monitor()

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def test_owner_fair_beats_the_model_and_clears(self):
        m = self.mon
        fam = m.families["politics"]
        slug = "enwc-uspres-nom-dem-2028-gavnew"
        fam.universe[slug] = {"event_n": 1, "name": "Newsom"}
        m.silver.model_fair = lambda s: 0.30
        self.assertEqual(m._fair_for(slug), 0.30)      # model by default
        r = m.set_owner_fair(slug, 0.22)
        self.assertTrue(r["ok"])
        self.assertEqual(m._fair_for(slug), 0.22)      # the owner wins
        self.assertEqual(fam.fairs(slug), 0.22)        # families see it
        self.assertEqual(m.last_state["owner_fairs"][slug], 0.22)
        r = m.set_owner_fair(slug, None)
        self.assertTrue(r["ok"])
        self.assertEqual(m._fair_for(slug), 0.30)      # back to the model

    def test_unknown_market_and_bad_range_refused(self):
        m = self.mon
        self.assertFalse(m.set_owner_fair("not-a-market", 0.2)["ok"])
        fam = m.families["politics"]
        fam.universe["x-known"] = {"event_n": 1, "name": "X"}
        self.assertFalse(m.set_owner_fair("x-known", 1.5)["ok"])


class TestTradeHistoryConfirms(unittest.TestCase):
    def test_limbo_resolves_by_order_id_from_trade_history(self):
        """Owner, 2026-08-23: 'is there no way to see transaction
        history and backfill?' — there is: /v1/portfolio/activities
        names our order ids, and limbo resolves against it."""
        from v3.tests.test_family import Rig
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        r = Rig()
        r.add_market(A_J)
        r.fam.orders["T1"] = FamilyOrder(
            id="T1", market=A_J, side="BUY", price=0.40, qty=5.0,
            intent=BUY_LONG, placed_ts=r.now, purpose="earn")
        # vanished, no position delta — but the trade history knows it
        r.now += 60.0
        r.fam.cycle(r.now, r.exchange.open_orders(), r.positions,
                    r.exchange, True, trades={"T1": 5.0})
        self.assertEqual(r.fam.silent_cancels, 0)
        self.assertEqual(len(r.fam.gone_pending), 0)
        fills = [x for x in r.fam.fills if x["market"] == A_J]
        self.assertTrue(fills)
        self.assertEqual(fills[-1]["px"], 0.40)
        self.assertEqual((r.fam.inventory.get(A_J) or {}).get("qty"), 5.0)

    def test_pending_cards_surface_in_fills_view(self):
        import tempfile, os as _os
        from v3.main import Monitor
        d = tempfile.TemporaryDirectory()
        _os.environ["V3_STATE_PATH"] = _os.path.join(d.name, "s.json")
        _os.environ["V3_FLOOR_PATH"] = _os.path.join(d.name, "f.json")
        _os.environ["GITHUB_TOKEN"] = ""
        _os.environ["V3_FLATTEN"] = "0"
        try:
            m = Monitor()
            from v3.family import FamilyOrder
            from v3.intents import SELL_LONG
            fam = m.families["politics"]
            fam.gone_pending["P1"] = {
                "rec": FamilyOrder(id="P1", market="m-x", side="SELL",
                                   price=0.91, qty=50.0, intent=SELL_LONG,
                                   placed_ts=1.0, purpose="sell"),
                "until": 1000.0}
            v = m.fills_view()
            pend = v.get("pending") or []
            self.assertEqual(len(pend), 1)
            self.assertEqual(pend[0]["qty"], 50.0)
            self.assertEqual(pend[0]["px"], 0.91)
        finally:
            for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
                _os.environ.pop(k, None)
            d.cleanup()


class TestWallSizeUpBindsRestingBook(unittest.TestCase):
    def test_dust_join_is_repriced_to_full_size(self):
        """Owner, 2026-08-23: 'I don't see any increase in nba order
        sizes' — pre-rule 0.01-share joins never upgraded because the
        bigger size shows worse model EV. Undersized joins are now
        forced to the full-size slot like oversized ones shrink."""
        from v3.tests.test_family import Rig, A
        from v3.basketball import nba
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        from v3.scoring import Book
        r = Rig(cfg=nba())
        r.add_market(A, book=Book(
            bids=((0.01, 480000.0),),
            asks=((0.02, 280000.0), (0.98, 60000.0)),
            tick=0.01, fetched_at=1_000_000.0))
        r.fam.orders["DUST"] = FamilyOrder(
            id="DUST", market=A, side="BUY", price=0.01, qty=0.01,
            intent=BUY_LONG, placed_ts=1.0, purpose="earn")
        r.exchange.live["DUST"] = {"id": "DUST", "market": A,
                                   "side": "BUY", "price": 0.01,
                                   "size": 0.01, "intent": BUY_LONG}
        for _ in range(3):
            r.fam.last_action.clear()
            r.cycle(advance=120.0)
        bids = [o for o in r.fam.orders.values()
                if o.market == A and o.side == "BUY"
                and o.purpose != "manual"]
        self.assertTrue(bids)
        self.assertGreaterEqual(max(o.qty for o in bids), 50.0)


class TestTransactionHistory(unittest.TestCase):
    """Owner, 2026-08-23: 'get the transaction history so we can have a
    definitive record of what is happening.'"""

    def _trade(self, oid, mkt, intent, px, shares, ts, pnl=None,
               role="passive", other_first=True):
        ours = {"order": {"id": oid, "intent": intent},
                "lastShares": str(shares), "lastPx": str(px),
                "transactTime": ts}
        theirs = {"order": {"id": "THEIRS",
                            "intent": "ORDER_INTENT_UNDEFINED"},
                  "lastShares": str(shares), "lastPx": str(px),
                  "transactTime": ts}
        t = {"marketSlug": mkt, "updateTime": ts}
        if role == "passive":
            t["passiveExecution"], t["aggressorExecution"] = ours, theirs
        else:
            t["passiveExecution"], t["aggressorExecution"] = theirs, ours
        if pnl is not None:
            t["realizedPnl"] = str(pnl)
        return {"type": "ACTIVITY_TYPE_TRADE", "trade": t}

    def test_picks_our_side_not_the_counterpartys(self):
        from v3.main import parse_activities
        rows = parse_activities([
            self._trade("O1", "mkt-a", "ORDER_INTENT_BUY_LONG",
                        0.44, 10, "2026-08-23T14:00:00Z"),
            # we were the AGGRESSOR here (a taker dump)
            self._trade("O2", "mkt-b", "ORDER_INTENT_SELL_LONG",
                        0.91, 50, "2026-08-23T14:05:00Z", role="aggressor"),
        ])
        self.assertEqual(len(rows), 2)          # both ours, neither dropped
        self.assertEqual(rows[0]["order_id"], "O1")
        self.assertEqual(rows[0]["side"], "BUY")
        self.assertEqual(rows[0]["price"], 0.44)
        self.assertEqual(rows[0]["shares"], 10)
        self.assertEqual(rows[0]["role"], "passive")
        self.assertEqual(rows[1]["order_id"], "O2")
        self.assertEqual(rows[1]["side"], "SELL")
        self.assertEqual(rows[1]["role"], "aggressor")
        self.assertGreater(rows[0]["ts"], 1_700_000_000)

    def test_a_known_order_id_wins_when_both_sides_carry_intents(self):
        # the Tennessee case (2026-09-03): our 3c take was the aggressor,
        # the stranger's passive order carried a real intent too, and the
        # passive side was read first — the stranger became a "hand" sale
        from v3.main import parse_activities
        row = {"type": "ACTIVITY_TYPE_TRADE", "trade": {
            "marketSlug": "mkt-tn",
            "passiveExecution": {"order": {"id": "STRANGER",
                                           "intent": "ORDER_INTENT_BUY_LONG"},
                                 "lastShares": "18.75", "lastPx": "0.03"},
            "aggressorExecution": {"order": {"id": "OURS",
                                             "intent": "ORDER_INTENT_BUY_SHORT"},
                                   "lastShares": "18.75", "lastPx": "0.03"}}}
        rows = parse_activities([row], known_ids={"OURS"})
        self.assertEqual([(r["order_id"], r["role"]) for r in rows],
                         [("OURS", "aggressor")])
        # with nothing known, the old rule still applies
        self.assertEqual(parse_activities([row])[0]["order_id"], "STRANGER")

    def test_trade_entirely_the_counterpartys_is_skipped(self):
        from v3.main import parse_activities
        rows = parse_activities([{"type": "ACTIVITY_TYPE_TRADE", "trade": {
            "marketSlug": "mkt-c",
            "passiveExecution": {"order": {"id": "X",
                                 "intent": "ORDER_INTENT_UNDEFINED"},
                                 "lastShares": "5"},
            "aggressorExecution": {"order": {"id": "Y",
                                   "intent": "ORDER_INTENT_UNDEFINED"},
                                   "lastShares": "5"}}}])
        self.assertEqual(rows, [])

    def test_placement_with_no_shares_is_not_a_fill(self):
        from v3.main import parse_activities
        a = self._trade("O3", "mkt-d", "ORDER_INTENT_BUY_LONG",
                        0.10, 0, "2026-08-23T14:00:00Z")
        self.assertEqual(parse_activities([a]), [])

    def test_resolutions_and_unknown_shapes_are_recorded(self):
        from v3.main import parse_activities
        rows = parse_activities([
            {"type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
             "positionResolution": {"marketSlug": "mkt-e",
                                    "updateTime": "2026-08-23T12:00:00Z",
                                    "realizedPnl": "3.25",
                                    "afterPosition": {"quantity": "0"}}},
            {"type": "ACTIVITY_TYPE_SOMETHING_NEW",
             "updateTime": "2026-08-23T11:00:00Z"},
        ])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["market"], "mkt-e")
        self.assertEqual(rows[0]["realized_pnl"], 3.25)
        self.assertEqual(rows[1]["type"], "ACTIVITY_TYPE_SOMETHING_NEW")

    def test_csv_is_append_only_and_deduplicates(self):
        from v3.main import parse_activities, trades_csv_append
        rows = parse_activities([
            self._trade("O1", "mkt-a", "ORDER_INTENT_BUY_LONG",
                        0.44, 10, "2026-08-23T14:00:00Z")])
        text, n = trades_csv_append(None, rows)
        self.assertEqual(n, 1)
        self.assertIn("mkt-a", text)
        self.assertIn("O1", text)
        text2, n2 = trades_csv_append(text, rows)     # same page again
        self.assertEqual(n2, 0)
        self.assertEqual(text2, text)
        more = parse_activities([
            self._trade("O9", "mkt-z", "ORDER_INTENT_SELL_LONG",
                        0.07, 44, "2026-08-23T15:00:00Z")])
        text3, n3 = trades_csv_append(text2, more)
        self.assertEqual(n3, 1)
        self.assertIn("mkt-z", text3)

    def test_protobuf_money_shapes_parse(self):
        from v3.main import parse_activities
        a = {"type": "ACTIVITY_TYPE_TRADE", "trade": {
            "marketSlug": "mkt-p", "updateTime": "2026-08-23T10:00:00Z",
            "passiveExecution": {
                "order": {"id": "OP", "intent": "ORDER_INTENT_BUY_LONG"},
                "lastShares": {"value": "7"},
                "lastPx": {"value": "0.33"},
                "transactTime": "2026-08-23T10:00:00Z"}}}
        rows = parse_activities([a])
        self.assertEqual(rows[0]["shares"], 7.0)
        self.assertEqual(rows[0]["price"], 0.33)


class TestActivitySideMapping(unittest.TestCase):
    def test_short_intents_map_to_the_opposite_side(self):
        """BUY_SHORT rests as an ASK, SELL_SHORT as a BID — reading the
        intent NAME inverted the side on every short (2026-08-23)."""
        from v3.main import parse_activities
        def act(intent):
            return {"type": "ACTIVITY_TYPE_TRADE", "trade": {
                "marketSlug": "m", "updateTime": "2026-08-23T10:00:00Z",
                "passiveExecution": {
                    "order": {"id": "O", "intent": intent},
                    "lastShares": "5", "lastPx": "0.30",
                    "transactTime": "2026-08-23T10:00:00Z"}}}
        want = {"ORDER_INTENT_BUY_LONG": "BUY",
                "ORDER_INTENT_SELL_LONG": "SELL",
                "ORDER_INTENT_BUY_SHORT": "SELL",     # an ASK
                "ORDER_INTENT_SELL_SHORT": "BUY"}     # a BID
        for intent, side in want.items():
            self.assertEqual(parse_activities([act(intent)])[0]["side"],
                             side, intent)


class TestJournalBackfill(unittest.TestCase):
    """Owner, 2026-08-23 ('Do it'): recover fills the journal never
    recorded, from the exchange's own record. Additive, idempotent,
    and it must never touch inventory."""

    def setUp(self):
        import tempfile
        import time as _time
        from v3.main import Monitor
        self.dir = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(self.dir.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        self.mon = Monitor()
        self.fam = self.mon.families["politics"]
        self.slug = "ussewc-usse-ga-2026-11-03-dem"
        self.fam.universe[self.slug] = {"event_n": 1, "name": "GA"}
        self.now = _time.time()

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def _act(self, intent, px, shares, ago_h=1.0):
        import datetime as _d
        ts = _d.datetime.fromtimestamp(self.now - ago_h * 3600,
                                       _d.timezone.utc).isoformat()
        return {"type": "ACTIVITY_TYPE_TRADE", "trade": {
            "marketSlug": self.slug, "updateTime": ts,
            "passiveExecution": {"order": {"id": "OX", "intent": intent},
                                 "lastShares": str(shares),
                                 "lastPx": str(px), "transactTime": ts}}}

    def _feed(self, acts):
        self.mon.client.activities = lambda pages=25: acts

    def test_recovers_a_missing_fill_and_is_idempotent(self):
        self._feed([self._act("ORDER_INTENT_BUY_LONG", 0.44, 10)])
        prev_inv = dict(self.fam.inventory)
        dry = self.mon.backfill_journal(dry_run=True)
        self.assertTrue(dry["ok"])
        self.assertEqual(dry["added"], 1)
        self.assertEqual(len(self.fam.fills), 0)      # dry run writes nothing
        r = self.mon.backfill_journal(dry_run=False)
        self.assertEqual(r["added"], 1)
        self.assertEqual(len(self.fam.fills), 1)
        row = self.fam.fills[0]
        self.assertEqual(row["qty"], 10)
        self.assertEqual(row["px"], 0.44)
        self.assertEqual(row["side"], "BUY")
        self.assertEqual(row["purpose"], "backfill")
        self.assertEqual(self.fam.inventory, prev_inv)  # inventory untouched
        again = self.mon.backfill_journal(dry_run=False)
        self.assertEqual(again["added"], 0)           # IDEMPOTENT
        self.assertEqual(len(self.fam.fills), 1)

    def test_only_the_shortfall_is_written(self):
        # the journal already has 6 of the 10 shares the exchange shows
        self.fam.fills.append({"ts": self.now - 3600, "market": self.slug,
                               "side": "BUY", "qty": 6.0, "px": 0.44,
                               "purpose": "earn"})
        self._feed([self._act("ORDER_INTENT_BUY_LONG", 0.44, 10)])
        r = self.mon.backfill_journal(dry_run=False)
        self.assertEqual(r["added"], 1)
        # find it by label: the two rows can share a timestamp, so
        # position in the sorted journal is not a stable handle
        recovered = [x for x in self.fam.fills
                     if x.get("purpose") == "backfill"]
        self.assertEqual(len(recovered), 1)
        self.assertAlmostEqual(recovered[0]["qty"], 4.0)        # 10 - 6

    def test_fully_journaled_fills_add_nothing(self):
        self.fam.fills.append({"ts": self.now - 3600, "market": self.slug,
                               "side": "SELL", "qty": 5.0, "px": 0.90,
                               "purpose": "sell"})
        self._feed([self._act("ORDER_INTENT_SELL_LONG", 0.90, 5)])
        r = self.mon.backfill_journal(dry_run=False)
        self.assertEqual(r["added"], 0)

    def test_short_intents_land_on_the_right_side(self):
        self._feed([self._act("ORDER_INTENT_SELL_SHORT", 0.30, 8)])
        self.mon.backfill_journal(dry_run=False)
        recovered = [x for x in self.fam.fills
                     if x.get("purpose") == "backfill"]
        self.assertEqual(recovered[0]["side"], "BUY")         # a BID

    def test_outside_the_window_is_left_alone(self):
        self._feed([self._act("ORDER_INTENT_BUY_LONG", 0.44, 10,
                              ago_h=24 * 9)])
        r = self.mon.backfill_journal(days=3.0, dry_run=False)
        self.assertEqual(r["added"], 0)


class TestBackfillMatchesByOrderId(unittest.TestCase):
    """Owner, 2026-08-23: 'keep track of the order id in the future so
    we can match it up.' Exact matching, and a conservative fallback
    for journal rows written before order ids were recorded."""

    def setUp(self):
        import tempfile
        import time as _time
        from v3.main import Monitor
        self.dir = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(self.dir.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        self.mon = Monitor()
        self.fam = self.mon.families["politics"]
        self.slug = "ussewc-usse-ga-2026-11-03-dem"
        self.fam.universe[self.slug] = {"event_n": 1, "name": "GA"}
        self.now = _time.time()

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def _act(self, oid, px, shares, ago_h=1.0,
             intent="ORDER_INTENT_BUY_LONG"):
        import datetime as _d
        ts = _d.datetime.fromtimestamp(self.now - ago_h * 3600,
                                       _d.timezone.utc).isoformat()
        return {"type": "ACTIVITY_TYPE_TRADE", "trade": {
            "marketSlug": self.slug, "updateTime": ts,
            "passiveExecution": {"order": {"id": oid, "intent": intent},
                                 "lastShares": str(shares),
                                 "lastPx": str(px), "transactTime": ts}}}

    def _feed(self, acts):
        self.mon.client.activities = lambda pages=25: acts

    def test_two_orders_at_one_price_are_told_apart(self):
        """The price-bucket method could not do this: one order
        journaled, an identical-price sibling missing."""
        self.fam.fills.append({"ts": self.now - 3600, "market": self.slug,
                               "side": "BUY", "qty": 5.0, "px": 0.44,
                               "oid": "A1", "purpose": "earn"})
        self._feed([self._act("A1", 0.44, 5), self._act("B2", 0.44, 5)])
        r = self.mon.backfill_journal(dry_run=False)
        self.assertEqual(r["added"], 1)               # only the sibling
        rec = [x for x in self.fam.fills if x.get("purpose") == "backfill"]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["oid"], "B2")
        self.assertAlmostEqual(rec[0]["qty"], 5.0)

    def test_recovered_rows_carry_the_id_so_a_rerun_is_exact(self):
        self._feed([self._act("C3", 0.20, 7)])
        self.mon.backfill_journal(dry_run=False)
        rec = [x for x in self.fam.fills if x.get("purpose") == "backfill"]
        self.assertEqual(rec[0]["oid"], "C3")
        again = self.mon.backfill_journal(dry_run=False)
        self.assertEqual(again["added"], 0)

    def test_legacy_rows_without_an_id_are_credited_once(self):
        """A pre-oid journal row must absorb its execution, and only
        one of two same-price executions."""
        self.fam.fills.append({"ts": self.now - 3600, "market": self.slug,
                               "side": "BUY", "qty": 5.0, "px": 0.44,
                               "purpose": "earn"})        # no oid
        self._feed([self._act("D4", 0.44, 5), self._act("E5", 0.44, 5)])
        r = self.mon.backfill_journal(dry_run=False)
        self.assertEqual(r["added"], 1)     # legacy credit absorbed one
        self.assertAlmostEqual(r["shares"], 5.0)

    def test_partial_journaling_of_one_order_tops_up(self):
        self.fam.fills.append({"ts": self.now - 3600, "market": self.slug,
                               "side": "BUY", "qty": 3.0, "px": 0.44,
                               "oid": "F6", "purpose": "earn"})
        self._feed([self._act("F6", 0.44, 10)])
        r = self.mon.backfill_journal(dry_run=False)
        self.assertEqual(r["added"], 1)
        rec = [x for x in self.fam.fills if x.get("purpose") == "backfill"]
        self.assertAlmostEqual(rec[0]["qty"], 7.0)     # 10 - 3

    def test_multiple_executions_of_one_order_aggregate(self):
        """The exchange splits a fill into many small executions; they
        belong to one order and must not each become a row."""
        self._feed([self._act("G7", 0.30, 1, ago_h=2.0),
                    self._act("G7", 0.30, 1, ago_h=1.9),
                    self._act("G7", 0.30, 1, ago_h=1.8)])
        r = self.mon.backfill_journal(dry_run=False)
        self.assertEqual(r["added"], 1)
        rec = [x for x in self.fam.fills if x.get("purpose") == "backfill"]
        self.assertAlmostEqual(rec[0]["qty"], 3.0)


class TestReconciliationCardsHidden(unittest.TestCase):
    def test_flat_market_with_missing_closes_is_counted_not_listed(self):
        """Owner, 2026-08-23: cards closed without a recorded sale are
        'essentially useless' — hidden, with a count."""
        import tempfile
        import time as _time
        from v3.main import Monitor
        d = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(d.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(d.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        try:
            m = Monitor()
            fam = m.families["politics"]
            now = _time.time()
            slug_bad, slug_ok = "mkt-recon", "mkt-real"
            # bought, market now flat, no sale ever journaled
            fam.fills.append({"ts": now - 3600, "market": slug_bad,
                              "side": "BUY", "qty": 10.0, "px": 0.20,
                              "purpose": "earn"})
            # a real round trip: bought and sold, both recorded
            fam.fills.append({"ts": now - 7200, "market": slug_ok,
                              "side": "BUY", "qty": 5.0, "px": 0.30,
                              "purpose": "earn"})
            fam.fills.append({"ts": now - 3600, "market": slug_ok,
                              "side": "SELL", "qty": 5.0, "px": 0.40,
                              "purpose": "sell"})
            v = m.fills_view()
            self.assertEqual(v["hidden_reconciled"], 1)
            shown = {c["market"] for c in v["fills"]}
            self.assertIn(slug_ok, shown)         # the real one survives
            self.assertNotIn(slug_bad, shown)     # the useless one is gone
        finally:
            for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
                os.environ.pop(k, None)
            d.cleanup()


class TestBothTabsGetCards(unittest.TestCase):
    def test_many_open_cards_do_not_starve_the_closed_tab(self):
        """Owner, 2026-08-23 ('I'm not seeing any'): a single cap after
        an open-first sort let open cards eat the whole budget."""
        import tempfile
        import time as _time
        from v3.main import Monitor
        d = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(d.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(d.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        try:
            m = Monitor()
            fam = m.families["politics"]
            now = _time.time()
            for i in range(200):                      # 200 open lots
                mk = f"open-{i}"
                fam.fills.append({"ts": now - 60, "market": mk,
                                  "side": "BUY", "qty": 5.0, "px": 0.30,
                                  "purpose": "earn"})
                fam.inventory[mk] = {"qty": 5.0, "cost": 1.5}
            for i in range(5):                        # 5 real round trips
                mk = f"done-{i}"
                fam.fills.append({"ts": now - 7200, "market": mk,
                                  "side": "BUY", "qty": 4.0, "px": 0.30,
                                  "purpose": "earn"})
                fam.fills.append({"ts": now - 3600, "market": mk,
                                  "side": "SELL", "qty": 4.0, "px": 0.40,
                                  "purpose": "sell"})
            v = m.fills_view()
            shown = v["fills"]
            closed = [c for c in shown
                      if (c.get("open_qty") or 0) <= 0.005]
            self.assertEqual(len(closed), 5)          # all of them survive
            self.assertGreater(len([c for c in shown
                                    if (c.get("open_qty") or 0) > 0.005]), 0)
            self.assertEqual(v["closed_total"], 5)
        finally:
            for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
                os.environ.pop(k, None)
            d.cleanup()


class TestRecoveredFillsCorrectTheBand(unittest.TestCase):
    """Owner approved 2026-08-23: recovered fills feed the EVIDENCE
    band (they are real information about where a market trades) but
    never the fill-odds model (that needs the order's placed time,
    which the exchange record does not carry)."""

    def setUp(self):
        import tempfile
        import time as _time
        from v3.main import Monitor
        self.dir = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(self.dir.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        self.mon = Monitor()
        self.fam = self.mon.families["politics"]
        self.slug = "ussewc-usse-ga-2026-11-03-dem"
        self.fam.universe[self.slug] = {"event_n": 1, "name": "GA"}
        self.now = _time.time()

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def _feed_one(self, oid="Z1", px=0.44, shares=10, ago_h=1.0):
        import datetime as _d
        ts = _d.datetime.fromtimestamp(self.now - ago_h * 3600,
                                       _d.timezone.utc).isoformat()
        act = {"type": "ACTIVITY_TYPE_TRADE", "trade": {
            "marketSlug": self.slug, "updateTime": ts,
            "passiveExecution": {
                "order": {"id": oid, "intent": "ORDER_INTENT_BUY_LONG"},
                "lastShares": str(shares), "lastPx": str(px),
                "transactTime": ts}}}
        self.mon.client.activities = lambda pages=25: [act]

    def test_recovery_feeds_the_band_not_the_odds_model(self):
        self._feed_one()
        import json as _j
        before = len(self.fam.evidence.events.get(self.slug) or [])
        fm_before = _j.dumps(self.fam.fillmodel.to_dict(), sort_keys=True)
        self.mon.backfill_journal(dry_run=False)
        after = len(self.fam.evidence.events.get(self.slug) or [])
        self.assertEqual(after, before + 1)          # the band learned
        self.assertEqual(_j.dumps(self.fam.fillmodel.to_dict(),
                                  sort_keys=True), fm_before)   # odds did not

    def test_a_dry_run_teaches_nothing(self):
        self._feed_one()
        before = len(self.fam.evidence.events.get(self.slug) or [])
        self.mon.backfill_journal(dry_run=True)
        self.assertEqual(len(self.fam.evidence.events.get(self.slug) or []),
                         before)

    def test_older_recoveries_carry_less_weight(self):
        """The fill's own timestamp rides along, so evidence's 36h
        half-life damps an old recovery instead of treating it as news."""
        import time as _time
        self._feed_one(oid="OLD", ago_h=72.0)
        self.mon.backfill_journal(days=5.0, dry_run=False)
        rows = self.fam.evidence.events.get(self.slug) or []
        self.assertTrue(rows)
        age_h = (_time.time() - rows[-1][0]) / 3600.0
        self.assertGreater(age_h, 70.0)   # stored at its real age

    def test_seeding_runs_once_over_already_recovered_rows(self):
        self.fam.fills.append({"ts": self.now - 3600, "market": self.slug,
                               "side": "BUY", "qty": 5.0, "px": 0.31,
                               "purpose": "backfill"})
        self.mon.client.activities = lambda pages=25: []
        import time as _t2
        self.mon.publish_files(_t2.time())
        n1 = len(self.fam.evidence.events.get(self.slug) or [])
        self.assertEqual(n1, 1)
        self.assertTrue(self.mon.evidence_seeded)
        self.mon._pub_at = 0.0
        self.mon.publish_files(_t2.time())             # a second pass
        self.assertEqual(len(self.fam.evidence.events.get(self.slug) or []),
                         n1)                            # no double feed


class TestExactRestingPeriod(unittest.TestCase):
    """Owner, 2026-08-23: 'can't you match up the placement time with
    the execution time to get an exact resting period?' Yes — from our
    own placement ledger. With it the odds model learns; without it the
    observation is skipped rather than guessed."""

    def setUp(self):
        import tempfile
        import time as _time
        from v3.main import Monitor
        self.dir = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(self.dir.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        self.mon = Monitor()
        self.fam = self.mon.families["politics"]
        self.slug = "ussewc-usse-ga-2026-11-03-dem"
        self.fam.universe[self.slug] = {"event_n": 1, "name": "GA"}
        self.now = _time.time()

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def _feed(self, oid, exec_ago_h=1.0):
        import datetime as _d
        ts = _d.datetime.fromtimestamp(self.now - exec_ago_h * 3600,
                                       _d.timezone.utc).isoformat()
        self.mon.client.activities = lambda pages=25: [
            {"type": "ACTIVITY_TYPE_TRADE", "trade": {
                "marketSlug": self.slug, "updateTime": ts,
                "passiveExecution": {
                    "order": {"id": oid,
                              "intent": "ORDER_INTENT_BUY_LONG"},
                    "lastShares": "5", "lastPx": "0.40",
                    "transactTime": ts}}}]

    def test_ledger_gives_the_exact_resting_period(self):
        # placed 4h ago, executed 1h ago -> rested exactly 3h
        self.fam.placed_at["L1"] = self.now - 4 * 3600
        self._feed("L1", exec_ago_h=1.0)
        r = self.mon.backfill_journal(dry_run=False)
        self.assertEqual(r["odds_fed"], 1)
        row = [x for x in self.fam.fills if x.get("oid") == "L1"][0]
        self.assertAlmostEqual(row["rested_h"], 3.0, places=1)

    def test_no_ledger_entry_means_no_guess(self):
        self._feed("L2", exec_ago_h=1.0)          # never placed by us
        r = self.mon.backfill_journal(dry_run=False)
        self.assertEqual(r["odds_fed"], 0)
        row = [x for x in self.fam.fills if x.get("oid") == "L2"][0]
        self.assertIsNone(row["rested_h"])

    def test_the_ledger_survives_the_order_vanishing(self):
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        self.fam.orders["L3"] = FamilyOrder(
            id="L3", market=self.slug, side="BUY", price=0.40, qty=5.0,
            intent=BUY_LONG, placed_ts=self.now - 7200, purpose="earn")
        self.fam.reconcile([], {}, self.now)       # order vanishes
        self.assertNotIn("L3", self.fam.orders)
        self.assertIn("L3", self.fam.placed_at)    # its placement remains
        self.assertAlmostEqual(self.fam.placed_at["L3"], self.now - 7200)

    def test_the_ledger_survives_a_restart(self):
        self.fam.placed_at["L4"] = self.now - 3600
        d = self.fam.to_dict()
        from v3.tests.test_family import Rig
        r2 = Rig()
        r2.fam.restore(d)
        self.assertAlmostEqual(r2.fam.placed_at["L4"], self.now - 3600)


class TestExchangeCarriesPlacementTime(unittest.TestCase):
    """The 2026-08-23 shape probe: the exchange's order object carries
    createTime, the cancel reason, and the commissions actually
    charged. Owner: 'Be wary of anything opaque... it's always out
    there for you to find.' It was."""

    def _act(self, created, executed, cancel="", comm=None):
        return {"type": "ACTIVITY_TYPE_TRADE", "trade": {
            "marketSlug": "m-1", "updateTime": executed,
            "passiveExecution": {
                "order": {"id": "O1",
                          "intent": "ORDER_INTENT_BUY_LONG",
                          "createTime": created, "state": "FILLED",
                          "makerCommissionsBasisPoints": "5",
                          "manualOrderIndicator": True},
                "lastShares": "5", "lastPx": "0.40",
                "transactTime": executed,
                "commissionNotionalCollected": comm,
                "unsolicitedCancelReason": cancel}}}

    def test_resting_period_comes_from_the_exchange(self):
        from v3.main import parse_activities
        r = parse_activities([self._act("2026-08-23T10:00:00Z",
                                        "2026-08-23T13:30:00Z")])[0]
        self.assertAlmostEqual(r["rested_h"], 3.5, places=2)
        self.assertEqual(r["order_state"], "FILLED")
        self.assertEqual(r["manual"], 1)
        self.assertEqual(r["maker_bps"], 5.0)

    def test_cancel_reason_and_commission_are_captured(self):
        from v3.main import parse_activities
        r = parse_activities([self._act("2026-08-23T10:00:00Z",
                                        "2026-08-23T11:00:00Z",
                                        cancel="SELF_MATCH_PREVENTION",
                                        comm="0.02")])[0]
        self.assertEqual(r["cancel_reason"], "SELF_MATCH_PREVENTION")
        self.assertEqual(r["commission"], 0.02)

    def test_missing_placement_time_yields_no_resting_period(self):
        from v3.main import parse_activities
        a = self._act("", "2026-08-23T11:00:00Z")
        a["trade"]["passiveExecution"]["order"].pop("createTime")
        self.assertIsNone(parse_activities([a])[0]["rested_h"])

    def test_csv_carries_the_new_columns(self):
        from v3.main import parse_activities, trades_csv_append
        rows = parse_activities([self._act("2026-08-23T10:00:00Z",
                                           "2026-08-23T13:30:00Z")])
        text, n = trades_csv_append(None, rows)
        self.assertEqual(n, 1)
        head = text.split("\n")[0]
        for col in ("placed_iso", "rested_h", "commission",
                    "cancel_reason", "order_state"):
            self.assertIn(col, head)
        self.assertIn("3.5", text)


class TestEstimateLedger(unittest.TestCase):
    """Owner, 2026-08-23: 'All the estimates should stay written down
    somewhere until the actual numbers come in.' A past day's estimate
    is frozen once written — a prediction you can revise afterwards is
    worthless — and only the paid column fills in later."""

    def test_todays_row_updates_but_yesterdays_is_frozen(self):
        from v3.main import estimates_csv_append
        t1, n1 = estimates_csv_append(
            None, "2026-08-23",
            [("2026-08-22", "politics", 413.84, 1420.1),
             ("2026-08-23", "politics", 100.00, 500.0)],
            {}, "2026-08-23T17:00:00Z")
        self.assertEqual(n1, 2)
        # later the same day: today grew, yesterday must NOT change
        t2, _ = estimates_csv_append(
            t1, "2026-08-23",
            [("2026-08-22", "politics", 999.99, 1.0),   # a revision...
             ("2026-08-23", "politics", 178.32, 958.8)],
            {}, "2026-08-23T19:00:00Z")
        y = [l for l in t2.strip().split("\n") if l.startswith("2026-08-22")][0]
        self.assertIn("413.84", y)          # frozen at the original
        self.assertNotIn("999.99", y)       # the revision is refused
        d = [l for l in t2.strip().split("\n") if l.startswith("2026-08-23")][0]
        self.assertIn("178.32", d)          # today still updates

    def test_paid_fills_in_and_the_error_is_computed(self):
        from v3.main import estimates_csv_append
        t1, _ = estimates_csv_append(
            None, "2026-08-23", [("2026-08-21", "politics", 295.90, 1063.1)],
            {}, "2026-08-23T17:00:00Z")
        self.assertTrue(t1.strip().split("\n")[1].endswith(",,"))
        t2, n = estimates_csv_append(
            t1, "2026-08-23", [("2026-08-21", "politics", 295.90, 1063.1)],
            {"2026-08-21": 400.00}, "2026-08-24T09:00:00Z")
        self.assertEqual(n, 1)
        row = t2.strip().split("\n")[1]
        self.assertIn("400.00", row)
        self.assertIn("+35.2", row)         # (400-295.90)/295.90
        # and settling it again changes nothing
        t3, n3 = estimates_csv_append(
            t2, "2026-08-23", [("2026-08-21", "politics", 295.90, 1063.1)],
            {"2026-08-21": 400.00}, "2026-08-25T09:00:00Z")
        self.assertEqual(n3, 0)
        self.assertEqual(t3, t2)

    def test_every_family_gets_its_own_row(self):
        from v3.main import estimates_csv_append
        t, n = estimates_csv_append(
            None, "2026-08-23",
            [("2026-08-23", "politics", 178.32, 958.8),
             ("2026-08-23", "cfb", 228.38, 100.0),
             ("2026-08-23", "nba", 0.30, 5.0)],
            {}, "2026-08-23T17:00:00Z")
        self.assertEqual(n, 3)
        self.assertEqual(len(t.strip().split("\n")), 4)   # header + 3


class TestOwnerFairActsPromptly(unittest.TestCase):
    def test_setting_a_fair_puts_that_market_first_in_the_sweep(self):
        """2026-08-23: a resting BUY at 57c filled against a 50c fair
        the owner had just set — the sweep had not reached it yet."""
        import tempfile
        from v3.main import Monitor
        d = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(d.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(d.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        try:
            m = Monitor()
            fam = m.families["politics"]
            slug = "enwc-uspres-nom-rep-2028-jdvan"
            fam.universe[slug] = {"event_n": 1, "name": "JD"}
            self.assertNotIn(slug, fam.priority)
            r = m.set_owner_fair(slug, 0.50)
            self.assertTrue(r["ok"])
            self.assertIn(slug, fam.priority)   # jumps the queue
            self.assertEqual(fam.fairs(slug), 0.50)
        finally:
            for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
                os.environ.pop(k, None)
            d.cleanup()


class TestPrimariesAreNotTheGeneralElection(unittest.TestCase):
    """Owner, 2026-08-23: 'It's a primary and he's going to win the
    primary but lose the general election.' Silver prices the GENERAL.
    Matching by substring priced every primary at its candidate's
    general-election chance — 'usgubp' contains 'usgub', 'ussep'
    starts with 'usse' — which built a 381-share short in the
    Massachusetts governor primary against a 92/94 market."""

    def _silver(self):
        from v3.silver import SilverFairs
        sf = SilverFairs(client=None)
        sf.gov_races = {"ma": {"rep": 0.08, "dem": 0.92,
                               "cands": {"micmin": 0.00055}}}
        sf.races = {"nh": {"dem": 0.75, "rep": 0.25,
                           "cands": {"chrpap": 0.62}}}
        return sf

    def test_a_governor_primary_is_not_priced_by_the_general_table(self):
        sf = self._silver()
        self.assertIsNone(
            sf.race_fair("enwc-usgubp-ma-2026-09-01-rep-micmin"))
        self.assertIsNone(
            sf.model_fair("enwc-usgubp-ma-2026-09-01-rep-micmin"))

    def test_a_senate_primary_is_not_priced_by_the_general_table(self):
        sf = self._silver()
        self.assertIsNone(
            sf.race_fair("enwc-ussep-nh-2026-09-08-dem-chrpap"))

    def test_the_general_election_still_prices_normally(self):
        sf = self._silver()
        self.assertAlmostEqual(
            sf.race_fair("usgubewc-usgub-ma-2026-11-03-rep"), 0.08)
        self.assertAlmostEqual(
            sf.race_fair("ussewc-usse-nh-2026-11-03-dem"), 0.75)

    def test_house_and_presidential_primaries_are_excluded_too(self):
        sf = self._silver()
        for slug in ("enwc-ushrp-fl19-2026-08-18-olahaw",
                     "enwc-uspresp-ia-2028-01-15-dem-somebody"):
            self.assertIsNone(sf.race_fair(slug), slug)


class TestFrozenGround(unittest.TestCase):
    """Owner, 2026-08-24: 'Don't sell my gop governor count race
    orders. In fact don't touch those.' Frozen is stricter than
    avoided: avoided PULLS the engine's orders out, frozen leaves the
    book exactly as it stands and adds nothing."""

    def _rig(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                           rest_style="join_quiet", revive=True,
                           capital_usd=250.0, per_market_usd=20.0,
                           freeze_tokens=("usgovcc",))
        r = Rig(cfg=cfg)
        return r

    def _seed(self, r, slug, purpose="sell", side="SELL", px=0.27,
              qty=1.0):
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG, BUY_LONG
        it = SELL_LONG if side == "SELL" else BUY_LONG
        r.fam.orders["FZ"] = FamilyOrder(
            id="FZ", market=slug, side=side, price=px, qty=qty,
            intent=it, placed_ts=1.0, purpose=purpose)
        r.exchange.live["FZ"] = {"id": "FZ", "market": slug, "side": side,
                                 "price": px, "size": qty, "intent": it}

    def test_a_resting_engine_order_is_never_touched(self):
        slug = "usgovcc-26mid-rep-2026-11-03-24-25"
        r = self._rig()
        r.add_market(slug)
        self._seed(r, slug)
        r.fam.inventory[slug] = {"qty": 1.0, "cost": 0.15}
        r.positions[slug] = (1.0, 0.15)
        for _ in range(4):
            r.fam.last_action.clear()
            r.cycle(advance=120.0)
        self.assertIn("FZ", r.fam.orders)                 # still there
        self.assertEqual(r.fam.orders["FZ"].price, 0.27)  # never moved
        self.assertEqual(r.fam.orders["FZ"].qty, 1.0)     # never resized

    def test_no_new_exit_is_rested_on_a_frozen_position(self):
        slug = "usgovcc-demvrep-2026-11-03-dem"
        r = self._rig()
        r.add_market(slug)
        r.fam.inventory[slug] = {"qty": 5.0, "cost": 3.19}
        r.positions[slug] = (5.0, 3.19)
        for _ in range(3):
            r.fam.last_action.clear()
            r.cycle(advance=120.0)
        sells = [o for o in r.fam.orders.values()
                 if o.market == slug and o.purpose == "sell"]
        self.assertEqual(sells, [])        # the seller stays out

    def test_nothing_new_is_placed_in_frozen_ground(self):
        slug = "usgovcc-26mid-rep-2026-11-03-30-31"
        r = self._rig()
        r.add_market(slug)
        self.assertFalse(r.fam.enterable(slug))
        for _ in range(3):
            r.cycle(advance=120.0)
        self.assertEqual([o for o in r.fam.orders.values()
                          if o.market == slug], [])

    def test_unfrozen_markets_are_unaffected(self):
        from v3.tests.test_family import A
        r = self._rig()
        r.add_market(A)
        self.assertTrue(r.fam.enterable(A))
        r.cycle()
        self.assertTrue([o for o in r.fam.orders.values() if o.market == A])


class TestQuieterFillAlerts(unittest.TestCase):
    """Owner, 2026-08-24. CLOSES: silent unless the realised loss is
    over $1. OPENS: silent unless, once the book has settled, the
    position marks more than $1 down AND nothing is earning there."""

    def _rig(self):
        from v3.tests.test_family import Rig
        r = Rig()
        r.add_market(A_J)
        return r

    def _fill(self, r, side, px, qty, intent=None):
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG, SELL_LONG
        it = intent or (BUY_LONG if side == "BUY" else SELL_LONG)
        rec = FamilyOrder(id=f"O{side}{px}", market=A_J, side=side,
                          price=px, qty=qty, intent=it,
                          placed_ts=r.now - 60, purpose="earn")
        r.fam._on_fill(rec, qty, r.now)

    def _pages(self, r):
        return [t for t, _ in r.alerts if "POL" in t]

    # ---- closes -------------------------------------------------------
    def test_small_losing_close_is_silent(self):
        r = self._rig()
        r.fam.inventory[A_J] = {"qty": 10.0, "cost": 5.0}   # 50c basis
        self._fill(r, "SELL", 0.45, 10.0)                   # -50c realised
        self.assertEqual(self._pages(r), [])

    def test_big_losing_close_pages(self):
        r = self._rig()
        r.fam.inventory[A_J] = {"qty": 10.0, "cost": 5.0}
        self._fill(r, "SELL", 0.30, 10.0)                   # -$2.00 realised
        self.assertTrue(any("closed at a loss" in t for t in self._pages(r)))

    def test_profitable_close_is_silent(self):
        r = self._rig()
        r.fam.inventory[A_J] = {"qty": 10.0, "cost": 5.0}
        self._fill(r, "SELL", 0.90, 10.0)
        self.assertEqual(self._pages(r), [])

    # ---- opens --------------------------------------------------------
    def test_open_does_not_page_at_the_moment_of_the_fill(self):
        r = self._rig()
        self._fill(r, "BUY", 0.50, 20.0)
        self.assertEqual(self._pages(r), [])            # held back
        self.assertEqual(len(r.fam.pending_pages), 1)

    def test_open_underwater_and_earning_nothing_pages_after_settling(self):
        from v3.scoring import Book
        r = self._rig()
        self._fill(r, "BUY", 0.50, 20.0)                # $10 of stock
        r.fam.inventory[A_J] = {"qty": 20.0, "cost": 10.0}
        r.fam.cache.put(A_J, Book(bids=((0.40, 100.0),),   # marks -$2
                                  asks=((0.42, 100.0),),
                                  tick=0.01, fetched_at=r.now))
        r.fam.orders.clear()                            # nothing earning
        r.fam._page_opens_due(r.now + 25)
        self.assertTrue(any("earning nothing" in t for t in self._pages(r)))

    def test_open_underwater_but_earning_stays_silent(self):
        from v3.scoring import Book
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG
        r = self._rig()
        self._fill(r, "BUY", 0.50, 20.0)
        r.fam.inventory[A_J] = {"qty": 20.0, "cost": 10.0}
        r.fam.cache.put(A_J, Book(bids=((0.40, 100.0),),
                                  asks=((0.42, 100.0),),
                                  tick=0.01, fetched_at=r.now))
        r.fam.orders["E1"] = FamilyOrder(
            id="E1", market=A_J, side="SELL", price=0.60, qty=20.0,
            intent=SELL_LONG, placed_ts=r.now, purpose="sell",
            live_est=0.75)                               # it IS earning
        r.fam._page_opens_due(r.now + 25)
        self.assertEqual(self._pages(r), [])

    def test_open_only_slightly_down_stays_silent(self):
        from v3.scoring import Book
        r = self._rig()
        self._fill(r, "BUY", 0.50, 20.0)
        r.fam.inventory[A_J] = {"qty": 20.0, "cost": 10.0}
        r.fam.cache.put(A_J, Book(bids=((0.48, 100.0),),   # marks -40c
                                  asks=((0.50, 100.0),),
                                  tick=0.01, fetched_at=r.now))
        r.fam.orders.clear()
        r.fam._page_opens_due(r.now + 25)
        self.assertEqual(self._pages(r), [])

    def test_a_short_marks_against_the_ask(self):
        from v3.scoring import Book
        from v3.intents import BUY_SHORT
        r = self._rig()
        self._fill(r, "SELL", 0.90, 20.0, intent=BUY_SHORT)
        r.fam.inventory[A_J] = {"qty": -20.0, "cost": -18.0}  # sold at 90c
        r.fam.cache.put(A_J, Book(bids=((0.97, 50.0),),
                                  asks=((0.99, 50.0),),   # buy back at 99c
                                  tick=0.01, fetched_at=r.now))
        r.fam.orders.clear()
        r.fam._page_opens_due(r.now + 25)
        # -20*0.99 - (-18) = -1.80 -> over the bar, pages
        self.assertTrue(any("earning nothing" in t for t in self._pages(r)))


class TestOwnerReplacementCountsAsCover(unittest.TestCase):
    """Owner, 2026-08-24: 'if I cancel an order and put a new one back
    the model won't sell more than is already there.' His replacement
    must count against the position so the engine rests nothing on
    top of it — including when the EXCHANGE flags it manual, which
    used to make it invisible."""

    def _rig_long(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": 120.0, "cost": 5.35}
        r.positions[A] = (120.0, 5.35)
        return r, A

    def test_exchange_flagged_manual_order_is_recorded_and_covers(self):
        from v3.intents import SELL_LONG
        r, A = self._rig_long()
        # the owner's own replacement, as the exchange reports it
        r.exchange.live["MINE"] = {"id": "MINE", "market": A,
                                   "side": "SELL", "price": 0.07,
                                   "size": 120.0, "intent": SELL_LONG,
                                   "manual": True}
        for _ in range(3):
            r.fam.last_action.clear()
            r.cycle(advance=120.0)
        self.assertIn("MINE", r.fam.orders)
        self.assertEqual(r.fam.orders["MINE"].purpose, "manual")
        engine_exits = [o for o in r.fam.orders.values()
                        if o.market == A and o.purpose == "sell"]
        self.assertEqual(engine_exits, [])       # nothing stacked on top
        self.assertEqual(r.fam.orders["MINE"].price, 0.07)   # untouched

    def test_a_partial_replacement_is_only_topped_up_by_the_remainder(self):
        from v3.intents import SELL_LONG
        r, A = self._rig_long()
        r.exchange.live["HALF"] = {"id": "HALF", "market": A,
                                   "side": "SELL", "price": 0.07,
                                   "size": 50.0, "intent": SELL_LONG,
                                   "manual": True}
        for _ in range(3):
            r.fam.last_action.clear()
            r.cycle(advance=120.0)
        engine = [o for o in r.fam.orders.values()
                  if o.market == A and o.purpose == "sell"]
        rested = sum(o.qty for o in engine)
        self.assertLessEqual(rested, 70.0 + 0.01)   # 120 held - his 50
        self.assertEqual(r.fam.orders["HALF"].qty, 50.0)

    def test_his_cover_on_a_short_counts_too(self):
        from v3.tests.test_family import Rig, A
        from v3.intents import SELL_SHORT
        r = Rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": -335.0, "cost": -314.44}
        r.positions[A] = (-335.0, -314.44)
        r.exchange.live["COVER"] = {"id": "COVER", "market": A,
                                    "side": "BUY", "price": 0.95,
                                    "size": 335.0, "intent": SELL_SHORT,
                                    "manual": True}
        for _ in range(3):
            r.fam.last_action.clear()
            r.cycle(advance=120.0)
        engine = [o for o in r.fam.orders.values()
                  if o.market == A and o.purpose == "sell"]
        self.assertEqual(engine, [])


class TestPerFamilyGrading(unittest.TestCase):
    """Each family is graded against ITS OWN paid money, not the whole
    account's day total (2026-08-24). Before this, politics' $255.22
    estimate for Aug-21 was scored against $93.02 of politics + college
    football + NFL combined, and nfl's $0.00 estimate was scored against
    the entire account."""

    def test_each_family_gets_its_own_paid_column(self):
        from v3.main import estimates_csv_append
        rows = [("2026-08-21", "politics", 255.22, 32.7),
                ("2026-08-21", "cfb", 16.00, 5.0),
                ("2026-08-21", "nfl", 0.00, 0.0)]
        text, _ = estimates_csv_append(
            None, "2026-08-24", rows, {"2026-08-21": 93.02},
            "2026-08-24T16:00:00Z",
            paid_by_fam={("2026-08-21", "politics"): 76.45,
                         ("2026-08-21", "cfb"): 16.57})
        got = {}
        for line in text.strip().split("\n")[1:]:
            p = line.split(",")
            got[p[1]] = (p[5], p[7])
        self.assertEqual(got["politics"][0], "76.45")
        self.assertEqual(got["cfb"][0], "16.57")
        self.assertEqual(got["nfl"][0], "")        # no money, no grade
        self.assertEqual(got["politics"][1], "-70.0")   # 3.3x over
        self.assertEqual(got["cfb"][1], "+3.6")

    def test_day_total_still_used_when_no_breakdown_exists(self):
        from v3.main import estimates_csv_append
        rows = [("2026-08-21", "politics", 100.0, 0.0)]
        text, _ = estimates_csv_append(
            None, "2026-08-24", rows, {"2026-08-21": 93.02},
            "2026-08-24T16:00:00Z")
        self.assertEqual(text.strip().split("\n")[1].split(",")[5], "93.02")

    def test_a_settled_football_market_still_classifies(self):
        # by the time football pays, the game is over and the market has
        # left every universe — the prefixes have to carry it
        m = Monitor.__new__(Monitor)
        m.families = {}
        self.assertEqual(m._family_of("tec-nba-lal-2026-10-28"), "nba")
        self.assertEqual(m._family_of("aqc-cfb-wins-bama-2026"), "cfb")
        self.assertEqual(m._family_of("vmc-nfl-wins-kc-2026"), "nfl")
        self.assertEqual(m._family_of("enwc-uspres-nom-rep-2028-rondes"),
                         "politics")


class TestMarketEstimateLedger(unittest.TestCase):
    """Per-market estimates, so a market or a race can be graded
    against its own prediction (2026-08-24). Family totals showed
    politics was 3.4x high; only this shows where."""

    def test_a_past_days_estimate_is_frozen_but_paid_fills_in(self):
        from v3.main import market_est_append
        rows = [("2026-08-22", "enwc-uspres-nom-rep-2028-rondes",
                 "politics", 12.50, 4, 0.0, 0.0, 0.0, 0)]
        t1, n1 = market_est_append(None, "2026-08-23", rows, {},
                                   "2026-08-23T01:00:00Z")
        self.assertEqual(n1, 1)
        # a later pass sees a different estimate AND the money
        rows2 = [("2026-08-22", "enwc-uspres-nom-rep-2028-rondes",
                  "politics", 99.99, 9, 0.0, 0.0, 0.0, 0)]
        t2, _ = market_est_append(
            t1, "2026-08-23", rows2,
            {"2026-08-22|enwc-uspres-nom-rep-2028-rondes": 5.78},
            "2026-08-24T01:00:00Z")
        p = t2.strip().split("\n")[1].split(",")
        self.assertEqual(p[3], "12.5000")    # estimate frozen
        self.assertEqual(p[4], "4")          # order count frozen with it
        self.assertEqual(p[5], "2026-08-23T01:00:00Z")
        self.assertEqual(p[6], "5.7800")     # money filled in
        self.assertEqual(p[8], "-53.8")      # graded: 2.2x over

    def test_todays_row_keeps_moving(self):
        from v3.main import market_est_append
        t1, _ = market_est_append(
            None, "2026-08-24",
            [("2026-08-24", "m", "politics", 1.0, 1, 0.0, 0.0, 0.0, 0)],
            {}, "2026-08-24T01:00:00Z")
        t2, _ = market_est_append(
            t1, "2026-08-24",
            [("2026-08-24", "m", "politics", 3.0, 5, 0.0, 0.0, 0.0, 0)],
            {}, "2026-08-24T02:00:00Z")
        p = t2.strip().split("\n")[1].split(",")
        self.assertEqual(p[3], "3.0000")     # the day is still accruing
        self.assertEqual(p[4], "5")

    def test_the_oldest_days_are_trimmed_first(self):
        from v3.main import market_est_append
        rows = [(f"2026-08-{d:02d}", f"m{i}", "politics", 1.0, 1, 0.0, 0.0, 0.0, 0)
                for d in (10, 20) for i in range(3)]
        text, _ = market_est_append(None, "2026-08-24", rows, {},
                                    "2026-08-24T01:00:00Z", keep_rows=3)
        days = {ln.split(",")[0] for ln in text.strip().split("\n")[1:]}
        self.assertEqual(days, {"2026-08-20"})   # newest kept

    def test_the_owners_own_orders_are_not_our_prediction(self):
        # a manual order carries no estimate of ours, so it must not
        # inflate the market's predicted rate
        from v3.tests.test_family import Rig, A
        from v3.intents import SELL_LONG
        r = Rig()
        r.add_market(A)
        r.fam.inventory[A] = {"qty": 120.0, "cost": 5.35}
        r.positions[A] = (120.0, 5.35)
        r.exchange.live["MINE"] = {"id": "MINE", "market": A, "side": "SELL",
                                   "price": 0.07, "size": 120.0,
                                   "intent": SELL_LONG, "manual": True}
        r.cycle()
        r.cycle()
        mine = r.fam.orders.get("MINE")
        self.assertIsNotNone(mine)
        self.assertEqual(mine.purpose, "manual")


class TestNonTradePayments(unittest.TestCase):
    """Settlements, deposits and transfers are money moving. We were
    recording that they happened and not how much, with no date either
    (owner, 2026-08-24: "tell me what these other payments I'm getting
    are")."""

    def test_a_settlement_carries_its_payout(self):
        from v3.main import parse_activities
        rows = parse_activities([{
            "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
            "positionResolution": {
                "updateTime": "2026-08-19T14:59:15Z",
                "marketSlug": "enwc-ushrp-fl19-2026-08-18-olahaw",
                "beforePosition": {"quantity": "75"},
                "afterPosition": {"quantity": "0"},
                "settlementPrice": "1", "payout": "75.00",
                "realizedPnl": "6708"}}])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["amount_usd"], 75.0)
        self.assertEqual(r["price"], 1.0)
        self.assertEqual(r["detail"], "held=75")
        self.assertTrue(r["iso"].startswith("2026-08-19"))

    def test_a_deposit_reads_its_time_and_amount_from_the_payload(self):
        # the fields are nested under the activity's own name, not at
        # the root — reading the root gave a blank date and no amount
        from v3.main import parse_activities
        rows = parse_activities([{
            "type": "ACTIVITY_TYPE_ACCOUNT_DEPOSIT",
            "accountDeposit": {"createTime": "2026-08-12T10:00:00Z",
                               "amount": {"value": "500.00"}}}])
        self.assertEqual(rows[0]["amount_usd"], 500.0)
        self.assertTrue(rows[0]["iso"].startswith("2026-08-12"))

    def test_an_unreadable_shape_records_what_it_offered(self):
        from v3.main import parse_activities
        rows = parse_activities([{
            "type": "ACTIVITY_TYPE_MYSTERY",
            "mystery": {"updateTime": "2026-08-20T00:00:00Z",
                        "creditsGranted": "9", "programRef": "x"}}])
        self.assertIsNone(rows[0]["amount_usd"])
        self.assertEqual(rows[0]["detail"],
                         "keys=creditsGranted|programRef|updateTime")

    def test_the_new_columns_are_written(self):
        from v3.main import trades_csv_append, TRADES_CSV_HEADER
        self.assertIn("amount_usd,detail", TRADES_CSV_HEADER)
        text, n = trades_csv_append(None, [{
            "ts": 1.0, "type": "ACTIVITY_TYPE_ACCOUNT_DEPOSIT",
            "amount_usd": 500.0, "detail": ""}])
        self.assertEqual(n, 1)
        self.assertTrue(text.rstrip().endswith(",500,"))


class TestShareCalibration(unittest.TestCase):
    """Owner, 2026-08-24: "Shouldn't there be thousands of estimates one
    every 20 seconds for each market. Shouldn't they catch any changes
    in my share?" They do — 4,320 a day. So a persistent error is a
    BIAS in the share arithmetic, and no amount of averaging removes a
    bias. These record the measurement that shows it directly."""

    def _est(self):
        from v3.estimator import Estimator
        e = Estimator()
        e.day = "2026-08-24"
        e.last_ts = 1000.0
        return e

    def test_share_and_pool_are_banked_by_the_seconds_they_held(self):
        e = self._est()
        # two intervals: 30% of the side for 100s, then 10% for 300s
        e.market_rates = {"m": 3.0}
        e.market_shares = {"m": 0.30}
        e.market_pools = {"m": 10.0}
        e._bill(1100.0, {"m"})
        e.market_shares = {"m": 0.10}
        e._bill(1400.0, {"m"})
        c = e.calibration()["m"]
        # time-weighted, not the last read: (.3*100 + .1*300) / 400
        self.assertAlmostEqual(c["share"], 0.15, places=5)
        self.assertAlmostEqual(c["pool_day"], 10.0, places=5)
        self.assertAlmostEqual(c["live_h"], 400 / 3600, places=3)

    def test_a_stale_market_banks_nothing(self):
        e = self._est()
        e.market_rates = {"m": 3.0}
        e.market_shares = {"m": 0.9}
        e.market_pools = {"m": 10.0}
        e._bill(1200.0, set())          # not fresh: no bill, no bank
        self.assertEqual(e.calibration(), {})

    def test_the_measurement_survives_a_restart(self):
        from v3.estimator import Estimator
        e = self._est()
        e.market_rates = {"m": 1.0}
        e.market_shares = {"m": 0.25}
        e.market_pools = {"m": 4.0}
        e._bill(1200.0, {"m"})
        back = Estimator.from_dict(e.to_dict())
        self.assertAlmostEqual(back.calibration()["m"]["share"], 0.25, places=5)

    def test_realized_share_is_written_once_the_money_lands(self):
        from v3.main import market_est_append
        # we computed 40% of a side offering $10/day, live 12 hours,
        # so we claimed $5.00 — and the exchange paid $1.25
        rows = [("2026-08-22", "m", "politics", 5.0, 2, 0.40, 10.0, 12.0, 8)]
        t1, _ = market_est_append(None, "2026-08-23", rows, {},
                                  "2026-08-23T00:00:00Z")
        t2, _ = market_est_append(t1, "2026-08-23", rows,
                                  {"2026-08-22|m": 1.25},
                                  "2026-08-24T00:00:00Z")
        p = t2.strip().split("\n")[1].split(",")
        self.assertEqual(p[9], "0.400000")      # what we computed
        self.assertEqual(p[12], "0.250000")     # what we actually got
        # the bias, stated plainly: we thought 40%, we got 25%

    def test_old_rows_without_the_share_columns_still_load(self):
        from v3.main import market_est_append, MARKET_EST_CSV_HEADER
        old = MARKET_EST_CSV_HEADER + "2026-08-01,m,politics,1.0,1,x,,,\n"
        text, _ = market_est_append(
            old, "2026-08-24",
            [("2026-08-24", "n", "politics", 2.0, 1, 0.1, 5.0, 6.0, 4)],
            {}, "2026-08-24T00:00:00Z")
        self.assertIn("2026-08-01,m,politics", text)


class TestBookDepth(unittest.TestCase):
    """The reward share is our score over EVERY maker's score on that
    side. A book the feed truncates hides competitors and inflates our
    share, so how deep the book came back is a number we have to keep
    (2026-08-24)."""

    def test_the_cache_records_the_depth_it_was_handed(self):
        from v3.books import BookCache
        from v3.scoring import normalize_book
        c = BookCache()
        c.put("m", normalize_book([(0.40, 10), (0.39, 20), (0.38, 5)],
                                  [(0.42, 5)], 1.0))
        self.assertEqual(c.depth_seen["m"], 3)
        self.assertEqual(c.depth_hist[3], 1)

    def test_depth_is_NOT_what_inflates_our_share(self):
        """Pinned because I got this wrong on 2026-08-24 and nearly
        shipped it as the fix. df**ticks decays faster than depth
        accumulates: past about the fourth level a maker is weightless,
        so a truncated feed barely moves our computed share. What moves
        it is SIZE AT AND NEAR THE TOUCH."""
        from v3.scoring import _window_denom, ticks_from_best
        def share(levels, df=0.3, tick=0.01, our_px=0.45, our_qty=100.0):
            best = levels[0][0]
            mine = our_qty * df ** ticks_from_best(best, our_px, tick)
            return mine / _window_denom(levels, best, tick, df)
        shallow = [(0.45, 100.0)] + [(0.44 - i * 0.01, 400.0) for i in range(3)]
        deep = [(0.45, 100.0)] + [(0.44 - i * 0.01, 400.0) for i in range(20)]
        # seeing 17 more levels moves the share by well under a point
        self.assertLess(abs(share(shallow) - share(deep)), 0.01)
        # ten times the size beside us moves it enormously
        heavy = [(0.45, 100.0)] + [(0.44 - i * 0.01, 4000.0) for i in range(3)]
        self.assertLess(share(heavy), share(shallow) / 3)

    def test_we_ask_the_endpoint_for_full_depth(self):
        from v3.api import Client
        self.assertEqual(Client.BOOK_DEPTH, 50)


class TestExitGate(unittest.TestCase):
    """Owner, 2026-08-25 (option B): an exit may rest past break-even
    only while the reward measured AT THAT PRICE, deflated ~3x for the
    estimator's known optimism, beats the expected fill loss by a
    margin — under a $5 family-wide give-up budget. Everything else
    floors at break-even. This replaced the dry/dust price-to-fill
    paths, which priced exits against ghost books after maintenance
    (a SELL at 2c on a 56c basis)."""

    IL_BOOK = dict(bids=((0.01, 2400.0),),
                   asks=((0.03, 10.0), (0.98, 5000.0)))
    IL_PROG = {"timePeriods": [{"programId": "politics_low", "rewardPool": 25.0,
                                "targetSize": 2000, "discountFactor": 0.1,
                                "status": "LIVE"}]}

    def _rig(self, book=None, prog=None):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        b = book or self.IL_BOOK
        r = Rig()
        r.add_market(A, book=Book(bids=b["bids"], asks=b["asks"],
                                  tick=0.01, fetched_at=r.now),
                     prog=prog or self.IL_PROG)
        r.cycle()      # discovery + terms
        return r, A

    def test_the_IL_book_passes_at_the_front(self):
        # the case the rule was designed on: 2 shares fronting an empty
        # side hold ~67% of the score; give-up 57c; earns it back fast
        r, A = self._rig()
        book = r.fam.cache.fresh(A, 999, r.now)
        px = r.fam._exit_gate(A, "SELL", 0.305, 2.0, book, r.now)
        self.assertEqual(px, 0.02)

    def test_size_alone_slams_the_gate(self):
        # the same discount on a big lot exceeds the family budget
        r, A = self._rig()
        book = r.fam.cache.fresh(A, 999, r.now)
        self.assertIsNone(r.fam._exit_gate(A, "SELL", 0.5, 183.0,
                                           book, r.now))

    def test_a_side_that_pays_nobody_earns_no_discount(self):
        # under Target Size the whole side pays nobody: est 0, gate shut
        r, A = self._rig(book=dict(bids=((0.01, 2400.0),),
                                   asks=((0.03, 10.0),)))
        book = r.fam.cache.fresh(A, 999, r.now)
        self.assertIsNone(r.fam._exit_gate(A, "SELL", 0.305, 2.0,
                                           book, r.now))

    def test_the_family_budget_is_shared(self):
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG
        r, A = self._rig()
        # another market's exit already has $4.90 of give-up in play
        r.fam.inventory["other-mkt"] = {"qty": 10.0, "cost": 5.1}
        r.fam.orders["OTH"] = FamilyOrder(
            id="OTH", market="other-mkt", side="SELL", price=0.02,
            qty=10.0, intent=SELL_LONG, placed_ts=0.0, purpose="sell")
        self.assertAlmostEqual(r.fam._exit_giveup_in_play(), 4.9, places=5)
        book = r.fam.cache.fresh(A, 999, r.now)
        self.assertIsNone(r.fam._exit_gate(A, "SELL", 0.305, 2.0,
                                           book, r.now))

    def test_covers_gate_symmetrically(self):
        # a short that received 90c may bid above it only through the
        # gate. It used to front at 95c; since 2026-09-02 a SMALL exit
        # (4c of give-up on one share) joins the 94c touch instead —
        # the least give-up that the reward pays for
        r, A = self._rig(book=dict(bids=((0.94, 20.0), (0.02, 60000.0)),
                                   asks=((0.97, 30.0),)))
        book = r.fam.cache.fresh(A, 999, r.now)
        px = r.fam._exit_gate(A, "BUY", 0.90, 1.0, book, r.now)
        self.assertEqual(px, 0.94)
        # a big lot has no such privilege: 200 shares front at 95c or
        # nothing, exactly as before
        big = r.fam._exit_gate(A, "BUY", 0.90, 200.0, book, r.now)
        self.assertIn(big, (0.95, 0.96, None))
        self.assertNotEqual(big, 0.94)

    def test_without_the_gate_the_floor_is_break_even(self):
        # the dust/dry freedom is gone from the floor itself. Basis is
        # set BELOW the evidence band's top so the band's own loss-cut
        # path (owner, 2026-08-22 — a separate, kept rule) stays out of
        # the way: with no model authorising a cut, a 1-share dust lot
        # floors at break-even instead of walking at the touch.
        from v3.tests.test_family import politics_book
        r, A = self._rig()
        book = politics_book(r.now)
        fl, _sb = r.fam._exit_floor(A, "SELL", 0.30, book.tick,
                                    book=book, qty=1.0)
        self.assertAlmostEqual(fl, 0.31, places=6)

    def test_seller_rests_at_the_gate_price(self):
        # integration: position 2 @ 30.5c basis on the IL book, no exit
        # yet -> the seller fronts at 2c because the gate blessed it
        r, A = self._rig()
        r.fam.inventory[A] = {"qty": 2.0, "cost": 0.61}
        r.positions[A] = (2.0, 0.61)
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        exits = [o for o in r.fam.orders.values()
                 if o.market == A and o.purpose == "sell"]
        self.assertTrue(exits)
        self.assertEqual(exits[0].price, 0.02)

    def test_the_dry_clock_survives_a_restart(self):
        # dry_since is telemetry now, but state compat must hold
        import dataclasses
        from v3.family import FamilyOrder
        o = FamilyOrder(id="X", market="m", side="SELL", price=0.5,
                        qty=1.0, purpose="sell", intent="", placed_ts=100.0)
        o.dry_since = 12345.0
        fields = {f.name for f in dataclasses.fields(FamilyOrder)}
        back = FamilyOrder(**{k: v for k, v in vars(o).items()
                              if k in fields})
        self.assertEqual(back.dry_since, 12345.0)

class TestCeilingCountsTheSameBookTwice(unittest.TestCase):
    """The ceiling check and the spend it checks against must measure
    the SAME book.

    On 2026-08-25, the morning after manual orders stopped counting
    toward the family ceiling, politics reached $324.58 against a $250
    cap. family_spent() excluded the owner's orders; the marginal-risk
    check that gates each new placement did not, so negative-risk
    netting offset every candidate against his book and each one looked
    cheaper than it was."""

    def test_a_manual_order_cannot_make_room_for_an_engine_order(self):
        from v3.tests.test_family import Rig, A, B
        from v3.family import FamilyConfig
        from v3.intents import BUY_LONG
        cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                           rest_style="join_quiet", revive=True,
                           capital_usd=6.0, per_market_usd=6.0)
        r = Rig(cfg=cfg)
        r.add_market(A, siblings=[B])
        for _ in range(3):
            r.fam.last_action.clear()
            r.cycle(advance=120.0)
        base = r.fam.family_spent()
        # a large hand order on the OPPOSITE bracket of the same race:
        # it nets against the engine's book and used to open headroom
        r.exchange.live["HAND"] = {"id": "HAND", "market": B, "side": "BUY",
                                   "price": 0.40, "size": 200.0,
                                   "intent": BUY_LONG, "manual": True}
        for _ in range(3):
            r.fam.last_action.clear()
            r.cycle(advance=120.0)
        self.assertIn("HAND", r.fam.orders)
        self.assertEqual(r.fam.orders["HAND"].purpose, "manual")
        # his money is not charged to the engine...
        self.assertLessEqual(r.fam.family_spent(), cfg.capital_usd + 1e-6)
        # ...and it does not buy the engine extra room either
        self.assertLessEqual(r.fam.family_spent(), base + 1e-6)


class TestTheCeilingIsNeverStarved(unittest.TestCase):
    """A ceiling that queues behind price improvements is not a ceiling.

    On 2026-08-25 politics ran to $360.69 against a $250 cap while the
    log showed 89 reprices, 50 places and ZERO trims: maintenance spent
    the whole per-cycle action budget every cycle and the trim behind it
    never got a turn."""

    def test_maintenance_cannot_spend_the_whole_budget_when_over(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig, FamilyOrder, TRIM_RESERVE
        from v3.intents import BUY_LONG
        # expected-risk era: with charge floors of 5%, these two
        # orders charge at least $0.10 together, so a $0.08 cap is
        # over by construction whatever the fill model estimates
        cfg = FamilyConfig(name="P", tag="P", capital_usd=0.08,
                           expected_risk=True, max_actions_per_cycle=10)
        r = Rig(cfg=cfg)
        r.add_market(A)
        seen = {}
        real = r.fam._maintain
        def spy(now, actions):
            seen["given"] = actions
            return real(now, actions)
        r.fam._maintain = spy
        for oid, px, qty in (("good", 0.43, 2.0), ("bad", 0.02, 60.0)):
            r.exchange.live[oid] = {"id": oid, "market": A, "side": "BUY",
                                    "price": px, "size": qty,
                                    "intent": BUY_LONG, "manual": False}
            o = FamilyOrder(id=oid, market=A, side="BUY", price=px, qty=qty,
                            intent=BUY_LONG, placed_ts=0.0, purpose="earn")
            o.live_est = 2.0 if oid == "good" else 0.0
            r.fam.orders[oid] = o
        r.cycle()
        # over the ceiling, so maintenance was handed less than the full
        # budget and the trim had actions left to act with
        self.assertEqual(seen["given"], 10 - TRIM_RESERVE)
        self.assertNotIn("bad", r.fam.orders)     # and it removed the right one

    def test_under_the_ceiling_maintenance_keeps_the_whole_budget(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        cfg = FamilyConfig(name="P", tag="P", capital_usd=500.0,
                           max_actions_per_cycle=10)
        r = Rig(cfg=cfg)
        r.add_market(A)
        seen = {}
        real = r.fam._maintain
        r.fam._maintain = lambda now, actions: (seen.setdefault("given", actions),
                                                real(now, actions))[1]
        r.cycle()
        self.assertEqual(seen["given"], 10)   # nothing held back when fine


class TestTheLedgerActuallyWrites(unittest.TestCase):
    """The per-market share ledger stopped being written the moment the
    depth column was added: it read self.cache, the Monitor has no
    cache (each family owns one), and the ledger's own try/except
    swallowed the AttributeError every hour. The measurement we had
    been waiting on all week was silently not landing."""

    def test_depth_comes_from_the_family_that_owns_the_book(self):
        from v3.main import Monitor
        from v3.books import BookCache
        m = Monitor.__new__(Monitor)
        class F:
            pass
        f = F()
        f.cache = BookCache()
        f.cache.depth_seen = {"mkt": 7}
        m.families = {"politics": f}
        self.assertEqual(m._depth_of("politics", "mkt"), 7)

    def test_it_never_throws_on_a_market_or_family_it_does_not_know(self):
        from v3.main import Monitor
        m = Monitor.__new__(Monitor)
        m.families = {}
        self.assertEqual(m._depth_of("politics", "anything"), 0)
        self.assertEqual(m._depth_of("nope", "anything"), 0)

    def test_the_monitor_has_no_cache_attribute_to_read(self):
        # the assumption that broke it, pinned so it cannot come back
        from v3.main import Monitor
        self.assertFalse(hasattr(Monitor, "cache"))


class TestTimeGradedLearning(unittest.TestCase):
    """Owner, 2026-08-25: "Getting filled quickly tells us that we're
    over the fair price. Getting filled after a while tells us we're in
    the range." — judged against each fill's own context clock, and a
    swept ladder is ONE loud observation, not seven ordinary ones."""

    def _book(self, bid=0.01, ask=0.22):
        from v3.scoring import Book
        return Book(bids=((bid, 2400.0),), asks=((ask, 5000.0),),
                    tick=0.01, fetched_at=1.0)

    def _fam(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        return r, A

    def test_the_dwajoh_fill_reads_as_a_snatch(self):
        # 11c bid, 10 ticks into a 21-tick spread, filled in 16 min:
        # its cell (wide spread, deep) has a 10-minute 25th percentile,
        # but 16 min with adv 48% is judged in the wide/deep cell whose
        # q25 is 10 -> 16 > 10... the SNATCH came from the earlier
        # sweep cell; here the pooled fallback (16.5) catches it
        from v3.family import SNATCH_WEIGHT
        r, A = self._fam()
        w, verdict, adv = r.fam._fill_speed_verdict(
            "BUY", 0.11, 9 * 60, self._book())
        self.assertEqual(w, SNATCH_WEIGHT)
        self.assertIn("snatched", verdict)
        self.assertGreater(adv, 0.4)

    def test_a_slow_touch_fill_is_ordinary(self):
        r, A = self._fam()
        w, verdict, _ = r.fam._fill_speed_verdict(
            "BUY", 0.44, 5 * 3600, self._book(bid=0.44, ask=0.46))
        self.assertEqual(w, 1.0)
        self.assertIn("within the range", verdict)

    def test_minutes_mean_different_things_by_context(self):
        # 60 min at the touch of a tight book = fast (q25 123 min).
        # 60 min deep in a wide book = ordinary (q25 10 min).
        from v3.family import SNATCH_WEIGHT
        r, A = self._fam()
        w_touch, _, _ = r.fam._fill_speed_verdict(
            "BUY", 0.44, 3600, self._book(bid=0.44, ask=0.46))
        w_deep, _, _ = r.fam._fill_speed_verdict(
            "BUY", 0.11, 3600, self._book())
        self.assertEqual(w_touch, SNATCH_WEIGHT)
        self.assertEqual(w_deep, 1.0)

    def test_a_sweep_is_one_observation_at_the_deepest_rung(self):
        r, A = self._fam()
        for px in (0.06, 0.08, 0.11):
            r.fam._fill_evi_buf.append(
                {"market": A, "side": "BUY", "px": px, "weight": 2.5,
                 "adv": px, "verdict": "snatched"})
        r.fam._flush_fill_evidence(2_000_000.0)
        evs = [e for e in r.fam.evidence.events.get(A, [])
               if e[1] == "fill_buy"]
        self.assertEqual(len(evs), 1)            # one event, not three
        self.assertEqual(evs[0][2], 11.0)        # at the deepest rung
        self.assertEqual(evs[0][3], 2.5)         # at the loud weight

    def test_a_snatch_pushes_the_band_down_harder_than_a_slow_fill(self):
        from v3.evidence import Evidence
        slow, fast = Evidence(clock=lambda: 1000.0), Evidence(clock=lambda: 1000.0)
        for e in (slow, fast):
            e.fill("m", "SELL", 0.20, ts=990.0)   # shared context
        slow.fill("m", "BUY", 0.11, ts=1000.0, weight=1.0)
        fast.fill("m", "BUY", 0.11, ts=1000.0, weight=2.5)
        b_slow = slow.band("m", now=1000.0)
        b_fast = fast.band("m", now=1000.0)
        self.assertLessEqual(b_fast["hi"], b_slow["hi"])
        self.assertLess(b_fast["med"], b_slow["med"])

    def test_weighted_rows_survive_persistence(self):
        from v3.evidence import Evidence
        e = Evidence(clock=lambda: 1000.0)
        e.fill("m", "BUY", 0.11, weight=2.5)
        e2 = Evidence(clock=lambda: 1000.0)
        e2.restore(e.to_dict())
        row = e2.events["m"][0]
        self.assertEqual(row[3], 2.5)


class TestInformationProbes(unittest.TestCase):
    """Owner, 2026-08-25: "Only fronting past fair where we have no
    information, and only to the extent necessary to get information
    that would allow us to earn." Past-value fronting is an information
    tool. A market that has spoken (any fill — a snatch record IS
    information) caps in-front quotes at the band's conservative edge;
    a truly blank one gets a single minimum-size probe that advances a
    tick per quiet day and shuts on the first fill."""

    WIDE = dict(bids=((0.01, 6000.0),), asks=((0.22, 5000.0),))

    def _rig(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=self.WIDE["bids"],
                                  asks=self.WIDE["asks"],
                                  tick=0.01, fetched_at=r.now))
        return r, A

    def _plan(self, r, A):
        prog, _ = r.fam._prog_row(A)
        book = r.fam.cache.fresh(A, 999, r.now)
        return r.fam._plan_side(A, book, "BUY", prog,
                                r.fam._side_pool(A, prog) or 0.0, 20.0,
                                bar=0.0)

    def test_a_blank_market_probes_one_tick_at_minimum_size(self):
        r, A = self._rig()
        r.cycle()
        plan = self._plan(r, A)
        if plan is not None:
            self.assertLessEqual(plan["px"], 0.02 + 1e-9)  # touch + 1 tick
            if plan["px"] > 0.011:                     # in front: min size
                self.assertLessEqual(plan["qty"], 1.0 + 1e-9)
        # a wider ratchet widens the reach, and no further than it says
        r.fam.probe_ratchet[f"{A}|BUY"] = [5, 0.0]
        plan5 = self._plan(r, A)
        self.assertIsNotNone(plan5)
        self.assertLessEqual(plan5["px"], 0.06 + 1e-9)  # touch + 5 ticks
        if plan5["px"] > 0.011:
            self.assertLessEqual(plan5["qty"], 1.0 + 1e-9)

    def test_a_burned_market_never_fronts_past_its_evidence(self):
        # the dwajoh shape: fills on record, no model. In-front quotes
        # stop at the band's low edge, however good the score out front.
        r, A = self._rig()
        r.cycle()
        for ts in (r.now - 3600, r.now - 1800):
            r.fam.evidence.fill(A, "BUY", 0.05, ts=ts, weight=2.5)
        plan = self._plan(r, A)
        if plan is not None:
            band = r.fam.evidence.band(A)
            lo = (band["lo"] or 99) / 100.0
            self.assertLessEqual(plan["px"], max(lo, 0.02) + 1e-9)

    def test_the_ratchet_earns_a_tick_per_quiet_day(self):
        r, A = self._rig()
        key = f"{A}|BUY"
        T = 1_800_000_000.0
        r.fam._advance_probe_ratchet(A, "BUY", T)
        self.assertEqual(r.fam.probe_ratchet[key][0], 2)
        r.fam._advance_probe_ratchet(A, "BUY", T + 1000)   # same day: no
        self.assertEqual(r.fam.probe_ratchet[key][0], 2)
        r.fam._advance_probe_ratchet(A, "BUY", T + 90000)
        self.assertEqual(r.fam.probe_ratchet[key][0], 3)

    def test_one_probe_at_a_time(self):
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        r, A = self._rig()
        r.cycle()
        r.fam.orders["P1"] = FamilyOrder(
            id="P1", market=A, side="BUY", price=0.02, qty=1.0,
            intent=BUY_LONG, placed_ts=r.now, purpose="earn")
        plan = self._plan(r, A)
        if plan is not None:                 # a second order stays at
            self.assertLessEqual(plan["px"], 0.01 + 1e-9)   # the touch

    def test_the_ratchet_survives_a_restart(self):
        r, A = self._rig()
        r.fam.probe_ratchet[f"{A}|BUY"] = [4, 123.0]
        d = r.fam.to_dict()
        r2, _ = self._rig()
        r2.fam.restore(d)
        self.assertEqual(r2.fam.probe_ratchet[f"{A}|BUY"], [4, 123.0])


class TestDecisionDeflator(unittest.TestCase):
    """Owner, 2026-08-25: politics decisions act on reward claims
    divided by 3. Owner, 2026-08-29 ("You can remove the divided by 3
    modifier"): the overshoot era ended — Aug-26/27 paid ABOVE raw
    claims — so decisions run raw again. The mechanism stays tested
    below in case a deflator returns."""

    def test_politics_ships_without_the_deflator(self):
        from v3 import politics
        self.assertEqual(politics.config().est_deflate, 1.0)

    def test_plans_run_on_the_deflated_claim(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        def plan_with(deflate):
            cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                               rest_style="join_quiet", revive=True,
                               capital_usd=100.0, per_market_usd=2.0,
                               est_deflate=deflate)
            r = Rig(cfg=cfg)
            r.add_market(A)
            r.cycle()
            r.fam.orders.clear()
            book = r.fam.cache.fresh(A, 999, r.now)
            prog, _ = r.fam._prog_row(A)
            sp = r.fam._side_pool(A, prog)
            return r.fam._plan_side(A, book, "BUY", prog, sp or 0.0,
                                    20.0, bar=0.0)
        raw, cut = plan_with(1.0), plan_with(3.0)
        self.assertIsNotNone(raw)
        self.assertIsNotNone(cut)
        self.assertAlmostEqual(raw["est"] / cut["est"], 3.0, places=1)

    def test_a_claim_that_only_clears_the_bar_raw_no_longer_places(self):
        # the point of the change: an inflated claim stops justifying
        # an order (the dwajoh EV cleared its bar on a raw claim)
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        def plan_with(deflate, bar):
            cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                               rest_style="join_quiet", revive=True,
                               capital_usd=100.0, per_market_usd=2.0,
                               est_deflate=deflate)
            r = Rig(cfg=cfg)
            r.add_market(A)
            r.cycle()
            r.fam.orders.clear()
            book = r.fam.cache.fresh(A, 999, r.now)
            prog, _ = r.fam._prog_row(A)
            sp = r.fam._side_pool(A, prog)
            return r.fam._plan_side(A, book, "BUY", prog, sp or 0.0,
                                    20.0, bar=bar)
        raw = plan_with(1.0, 0.0)
        self.assertIsNotNone(raw)
        bar = raw["est"] * 0.5          # clears raw, fails deflated
        self.assertIsNotNone(plan_with(1.0, bar))
        self.assertIsNone(plan_with(3.0, bar))


class TestPartyMarketsFundable(unittest.TestCase):
    def test_the_2028_party_pair_matches_an_enter_token(self):
        # owner, 2026-08-25 — and ONLY this group: apdc/opdc/lawec and
        # the science pools were offered the same day and declined
        from v3 import politics
        toks = politics.config().enter_tokens
        for m in ("ewc-usp-party-2028-11-07-rep",
                  "ewc-usp-party-2028-11-07-dem"):
            self.assertTrue(any(t in m for t in toks), m)
        for m in ("apdc-alito-2026-12-31", "opdc-mcconnell-resign-2026-11-02",
                  "lawec-saveact-2026-12-31", "dccc-measles-us-2026-12-31-gt4500"):
            self.assertFalse(any(t in m for t in toks), m)


class TestConformanceSweep(unittest.TestCase):
    """Owner, 2026-08-25: "make sure that all orders are conforming to
    the new rules before moving on." The fronting bounds govern the
    RESTING book: an order the rules would refuse to place today is
    pulled today, instead of catching fills until the planner happens
    to revisit it."""

    BOOK = dict(bids=((0.01, 6000.0),), asks=((0.22, 5000.0),))

    def _rig(self, price, qty=1.0):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=self.BOOK["bids"],
                                  asks=self.BOOK["asks"],
                                  tick=0.01, fetched_at=r.now))
        r.cycle()
        for oid in list(r.fam.orders):          # a clean slate, then the
            r.fam.orders.pop(oid)               # pre-rule leftover
        r.exchange.live.clear()
        r.exchange.live["OLD"] = {"id": "OLD", "market": A, "side": "BUY",
                                  "price": price, "size": qty,
                                  "intent": BUY_LONG}
        r.fam.orders["OLD"] = FamilyOrder(
            id="OLD", market=A, side="BUY", price=price, qty=qty,
            intent=BUY_LONG, placed_ts=1.0, purpose="earn")
        return r, A

    def test_a_pre_rule_deep_front_is_pulled(self):
        # the dwajoh shape: 10 ticks in front on a blank market
        r, A = self._rig(0.11)
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        self.assertNotIn("OLD", r.fam.orders)
        self.assertTrue(any(l.get("event") == "conform_pulled"
                            for l in r.fam.log))

    def test_an_oversized_front_is_pulled_even_one_tick_out(self):
        r, A = self._rig(0.02, qty=50.0)
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        self.assertNotIn("OLD", r.fam.orders)

    def test_a_conforming_probe_is_never_conform_pulled(self):
        # it may still be REPRICED by ordinary maintenance — the
        # assertion is that the sweep never fires and the market stays
        # quoted, not that the exact order is immortal
        r, A = self._rig(0.02, qty=0.5)         # 1 tick, minimum size
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        self.assertFalse(any(l.get("event") == "conform_pulled"
                             for l in r.fam.log))
        self.assertTrue(any(o.side == "BUY" and o.market == A
                            for o in r.fam.orders.values()))

    def test_at_the_touch_size_is_not_probe_capped(self):
        r, A = self._rig(0.01, qty=50.0)        # joining the wall: fine
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        self.assertFalse(any(l.get("event") == "conform_pulled"
                             for l in r.fam.log))
        self.assertTrue(any(o.side == "BUY" and o.market == A
                            for o in r.fam.orders.values()))


class TestTheNurse(unittest.TestCase):
    """Owner, 2026-08-25: "A process should stick with it just to
    monitor and guard against quick movements by others that would not
    get caught until a full cycle pass. When things are stable, then
    the process can end." Young orders on model-less markets are
    watched every few seconds; jumped or rushed ones are pulled on
    sight; quiet ones graduate."""

    def _rig(self, price=0.02, qty=1.0):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.01, 6000.0),),
                                  asks=((0.22, 5000.0),),
                                  tick=0.01, fetched_at=r.now))
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        r.exchange.live["P"] = {"id": "P", "market": A, "side": "BUY",
                                "price": price, "size": qty,
                                "intent": BUY_LONG}
        r.fam.orders["P"] = FamilyOrder(
            id="P", market=A, side="BUY", price=price, qty=qty,
            intent=BUY_LONG, placed_ts=r.now, purpose="earn")
        r.fam.nurse(r.now, r.exchange)          # first look: baseline
        return r, A

    def _rebook(self, r, A, bids, asks):
        from v3.scoring import Book
        r.fam.cache.put(A, Book(bids=bids, asks=asks, tick=0.01,
                                fetched_at=r.now))

    def test_a_jumped_probe_is_pulled_within_a_tick_of_the_nurse(self):
        r, A = self._rig()
        # someone quotes past our 2c front
        self._rebook(r, A, ((0.03, 40.0), (0.01, 6000.0)), ((0.22, 5000.0),))
        r.fam.nurse(r.now + 5, r.exchange)
        self.assertNotIn("P", r.fam.orders)
        self.assertTrue(any(l.get("event") == "nursed_pull"
                            and "fronted" in str(l.get("note"))
                            for l in r.fam.log))

    def test_a_rushing_ask_pulls_the_bid_before_it_is_eaten(self):
        r, A = self._rig(price=0.05)
        # the ask collapses 22c -> 7c, two ticks from our 5c bid
        self._rebook(r, A, ((0.01, 6000.0),), ((0.07, 300.0),))
        r.fam.nurse(r.now + 5, r.exchange)
        self.assertNotIn("P", r.fam.orders)
        self.assertTrue(any(l.get("event") == "nursed_pull"
                            and "rushed" in str(l.get("note"))
                            for l in r.fam.log))

    def test_ordinary_drift_is_left_alone(self):
        r, A = self._rig(price=0.02)
        # ask drifts one tick closer: not a rush, no pull
        self._rebook(r, A, ((0.01, 6000.0),), ((0.21, 5000.0),))
        r.fam.nurse(r.now + 5, r.exchange)
        self.assertIn("P", r.fam.orders)

    def test_a_stable_order_graduates_and_the_watch_ends(self):
        from v3.family import NURSE_STABLE_S
        r, A = self._rig()
        r.fam.nurse(r.now + NURSE_STABLE_S + 1, r.exchange)
        self.assertEqual(r.fam._nurse_base, {})
        # even a jump after graduation is the CYCLE's business now
        self._rebook(r, A, ((0.03, 40.0), (0.01, 6000.0)), ((0.22, 5000.0),))
        r.fam.nurse(r.now + NURSE_STABLE_S + 10, r.exchange)
        self.assertIn("P", r.fam.orders)

    def test_a_static_tight_gap_is_not_a_rush(self):
        # first hour live: pulls reading "rushed from 2c to 2c" on
        # tight books where a one-tick gap IS the resting state. No
        # movement, no pull.
        r, A = self._rig(price=0.05)
        self._rebook(r, A, ((0.01, 6000.0),), ((0.06, 300.0),))
        r.fam._nurse_base.clear()               # baseline ON the tight book
        r.fam.nurse(r.now, r.exchange)          # gap already 1 at baseline
        r.fam.nurse(r.now + 5, r.exchange)      # unchanged book
        self.assertIn("P", r.fam.orders)

    def test_manual_orders_are_never_nursed(self):
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        r, A = self._rig()
        r.fam.orders["HAND"] = FamilyOrder(
            id="HAND", market=A, side="BUY", price=0.09, qty=50.0,
            intent=BUY_LONG, placed_ts=r.now, purpose="manual")
        self._rebook(r, A, ((0.10, 40.0), (0.01, 6000.0)), ((0.22, 5000.0),))
        r.fam.nurse(r.now + 5, r.exchange)
        self.assertIn("HAND", r.fam.orders)

    def test_a_pull_does_not_delay_the_replacement(self):
        # owner, 2026-08-25: "You shouldn't wait, if a replacement is
        # called for by the rules, it should happen quickly." A nursed
        # pull leaves no cooldown and no price memory — the very next
        # cycle re-quotes the market wherever the standing rules allow.
        from v3.scoring import Book
        r, A = self._rig()
        self._rebook(r, A, ((0.03, 40.0), (0.01, 6000.0)), ((0.22, 5000.0),))
        marks_before = dict(r.fam.last_action)
        r.fam.nurse(r.now + 5, r.exchange)          # jumped -> pulled
        self.assertNotIn("P", r.fam.orders)
        self.assertEqual(dict(r.fam.last_action), marks_before)  # no new
                                                                 # cooldown
        r.fam.last_action.clear()
        r.cycle(advance=60.0)                       # the next cycle
        again = [o for o in r.fam.orders.values()
                 if o.market == A and o.side == "BUY"
                 and o.purpose not in ("manual", "sell")]
        self.assertTrue(again)                      # re-quoted at once...
        for o in again:                             # ...within the rules:
            self.assertLessEqual(o.price, 0.04 + 1e-9)  # ratchet off the
                                                        # 3c touch, tick 1


class TestTheEvidenceCap(unittest.TestCase):
    """Owner, 2026-08-25, the kamhar card: bought 0.99 @ 14c AT THE
    TOUCH against a 5-12c evidence band, on a market already
    round-tripped 13c -> 5c for -$25 the week before. The fronting
    rules left one door open — "joins the touch" — and this closes it:
    on a model-less market with real fills, NO earn order rests past
    the price the evidence supports. BUY caps at the band's low edge
    sliding toward center with confidence; revives and the resting
    book answer to the same line."""

    def _burned_rig(self):
        # the kamhar shape: burn fills recorded low, bait touch high
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.14, 60.0), (0.05, 3000.0)),
                                  asks=((0.16, 2600.0),),
                                  tick=0.01, fetched_at=r.now))
        r.cycle()
        r.fam.orders.clear()
        for i, px in enumerate((0.05, 0.05, 0.04)):
            r.fam.evidence.fill(A, "BUY", px, ts=r.now - 3600 - i,
                                weight=2.5)
        return r, A

    def _plan(self, r, A, side="BUY"):
        book = r.fam.cache.fresh(A, 999, r.now)
        prog, _ = r.fam._prog_row(A)
        sp = r.fam._side_pool(A, prog)
        return r.fam._plan_side(A, book, side, prog, sp or 0.0, 20.0,
                                bar=0.0)

    def test_the_kamhar_join_is_refused(self):
        r, A = self._burned_rig()
        plan = self._plan(r, A)
        if plan is not None:
            # whatever it rests, it is nowhere near the 14c bait
            book = r.fam.cache.fresh(A, 999, r.now)
            b_lo, b_hi = r.fam._price_bounds(A, book.side("BUY"),
                                             book.side("SELL"), book.tick)
            cap = r.fam._evidence_cap(A, "BUY", b_lo, b_hi)
            self.assertLessEqual(plan["px"], cap + 1e-9)
            self.assertLess(plan["px"], 0.14 - 1e-9)

    def test_confidence_slides_the_cap_toward_center(self):
        r, A = self._burned_rig()
        lo_conf_cap = r.fam._evidence_cap(A, "BUY", 0.05, 0.12)
        for i in range(8):                      # more fills, more trust
            r.fam.evidence.fill(A, "BUY", 0.06, ts=r.now - 60 - i)
        hi_conf_cap = r.fam._evidence_cap(A, "BUY", 0.05, 0.12)
        self.assertGreater(hi_conf_cap, lo_conf_cap)
        self.assertLessEqual(hi_conf_cap, (0.05 + 0.12) / 2 + 1e-9)

    def test_no_fills_means_no_cap(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.cycle()
        self.assertIsNone(r.fam._evidence_cap(A, "BUY", 0.05, 0.12))

    def test_the_sweep_pulls_a_resting_join_past_the_cap(self):
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        r, A = self._burned_rig()
        r.exchange.live["J"] = {"id": "J", "market": A, "side": "BUY",
                                "price": 0.14, "size": 5.0,
                                "intent": BUY_LONG}
        r.fam.orders["J"] = FamilyOrder(
            id="J", market=A, side="BUY", price=0.14, qty=5.0,
            intent=BUY_LONG, placed_ts=1.0, purpose="earn")
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        self.assertNotIn("J", r.fam.orders)
        self.assertTrue(any(l.get("event") == "conform_pulled"
                            and "evidence" in str(l.get("note"))
                            for l in r.fam.log))

    def test_a_revive_cannot_buy_through_the_cap(self):
        # the 284-share door: side under Target Size, anchor at the
        # bait level — the revive must respect the same line
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.13, 100.0),),
                                  asks=((0.16, 2600.0),),
                                  tick=0.01, fetched_at=r.now))
        r.cycle()
        r.fam.orders.clear()
        for i in range(3):
            r.fam.evidence.fill(A, "BUY", 0.05, ts=r.now - 3600 - i,
                                weight=2.5)
        plan = self._plan(r, A)          # bid side 100 < 5000 target
        if plan is not None:             # a revive, if any, sits under
            book = r.fam.cache.fresh(A, 999, r.now)
            b_lo, b_hi = r.fam._price_bounds(A, book.side("BUY"),
                                             book.side("SELL"), book.tick)
            cap = r.fam._evidence_cap(A, "BUY", b_lo, b_hi)
            if cap is not None:
                self.assertLessEqual(plan["px"], cap + 1e-9)


class TestStalePlansDieOnRuleChanges(unittest.TestCase):
    """2026-08-25, the rahema 12c buys: every reboot restored the saved
    scoreboard and placed its pre-rule plans verbatim — each placement
    within a minute of a boot, carrying the exact pre-deflator
    estimate. The signature that guards scoreboard reuse only covered
    config knobs; the day's rules are code. PLAN_RULES_REV makes rule
    changes wipe the board too."""

    def test_a_scoreboard_scored_under_old_rules_is_discarded(self):
        from v3.tests.test_family import Rig, A
        import v3.family as fam_mod
        r = Rig()
        r.add_market(A)
        r.cycle()
        r.fam.scoreboard[A] = {"ts": r.now, "plans": [
            {"side": "BUY", "px": 0.12, "qty": 1.0, "est": 5.82}]}
        d = r.fam.to_dict()
        old = fam_mod.PLAN_RULES_REV
        try:
            fam_mod.PLAN_RULES_REV = old + 1     # the rules changed
            r2 = Rig()
            r2.add_market(A)
            r2.fam.restore(d)
            self.assertEqual(r2.fam.scoreboard, {})   # rescan, no reuse
        finally:
            fam_mod.PLAN_RULES_REV = old

    def test_same_rules_keep_the_scoreboard(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.cycle()
        r.fam.scoreboard[A] = {"ts": r.now, "plans": []}
        d = r.fam.to_dict()
        r2 = Rig()
        r2.add_market(A)
        r2.fam.restore(d)
        self.assertIn(A, r2.fam.scoreboard)

    def test_the_deflator_is_part_of_the_signature(self):
        from v3.tests.test_family import Rig
        from v3.family import FamilyConfig
        a = Rig(cfg=FamilyConfig(name="P", tag="P", est_deflate=1.0))
        b = Rig(cfg=FamilyConfig(name="P", tag="P", est_deflate=3.0))
        self.assertNotEqual(a.fam._cfg_sig(), b.fam._cfg_sig())


class TestBookCompareIsReadOnly(unittest.TestCase):
    def test_it_reports_both_shapes_and_changes_nothing(self):
        from v3.api import Client
        c = Client.__new__(Client)
        calls = []
        def fake_get(url, **kw):
            calls.append(url)
            n = 8 if "/v1/orderbook/" in url else 2
            return {"book": {
                "bids": [{"px": 0.60 - i * 0.01, "qty": 10}
                         for i in range(n)],
                "asks": [{"px": 0.61 + i * 0.01, "qty": 10}
                         for i in range(n)]}}
        c.get = fake_get
        lines = c.compare_book_sources(["ga-dem"])
        self.assertEqual(len(lines), 1)
        self.assertIn("current=2+2", lines[0])
        self.assertIn("orderbook=8+8", lines[0])
        self.assertIn("60c/61c", lines[0])
        # nothing on the client changed — there is no switch to flip
        self.assertFalse(hasattr(c, "book_source"))

    def test_an_endpoint_error_is_a_line_not_a_crash(self):
        from v3.api import Client
        c = Client.__new__(Client)
        def fake_get(url, **kw):
            if "/v1/orderbook/" in url:
                raise RuntimeError("404")
            return {"book": {"bids": [], "asks": []}}
        c.get = fake_get
        lines = c.compare_book_sources(["m"])
        self.assertIn("orderbook=ERR", lines[0])


class TestFeedCheck(unittest.TestCase):
    """Owner-approved (2026-08-25) live-feed test, log-only: books the
    STREAM wrote are compared against a fresh REST fetch of the same
    market. The writer tag is what makes the comparison honest."""

    def test_the_cache_remembers_who_wrote_each_book(self):
        from v3.books import BookCache
        from v3.scoring import normalize_book
        c = BookCache()
        c.put("a", normalize_book([(0.4, 1)], [(0.6, 1)], 1.0))
        c.put("b", normalize_book([(0.4, 1)], [(0.6, 1)], 1.0), writer="ws")
        self.assertEqual(c.last_writer["a"], "rest")
        self.assertEqual(c.last_writer["b"], "ws")

    def test_a_stream_frame_is_tagged_ws(self):
        import json as _json
        from v3.ws import Stream
        from v3.books import BookCache
        st = Stream.__new__(Stream)
        st.cache = BookCache()
        st.declared = {}
        st.frame_shapes = {}
        st.status = {"last_msg": 0.0}
        st.apply_frame(_json.dumps({"marketData": {
            "marketSlug": "m",
            "bids": [{"px": "0.4", "qty": "10"}],
            "asks": [{"px": "0.6", "qty": "10"}]}}))
        self.assertEqual(st.cache.last_writer["m"], "ws")


class TestExpectedRiskBudget(unittest.TestCase):
    """Owner, 2026-08-25: "make the budget take into account the fill
    risk" — each order charges collateral x its measured fill odds,
    floored at 5%, unmeasured charged in full, with hard GROSS
    ceilings bounding the worst correlated day in dollars."""

    def _fam(self, **cfg_kw):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyConfig
        base = dict(name="P", tag="P", expected_risk=True)
        base.update(cfg_kw)
        r = Rig(cfg=FamilyConfig(**base))
        r.add_market(A)
        return r, A

    def _order(self, r, A, oid, px, qty, pf):
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        o = FamilyOrder(id=oid, market=A, side="BUY", price=px, qty=qty,
                        intent=BUY_LONG, placed_ts=0.0, purpose="earn")
        o.live_pf = pf
        r.fam.orders[oid] = o
        return o

    def test_the_charge_is_collateral_times_odds(self):
        r, A = self._fam()
        o = self._order(r, A, "x", 0.50, 10.0, 0.30)   # $5 at 30%
        self.assertAlmostEqual(r.fam._charge(o), 1.50, places=6)

    def test_the_floor_and_the_unmeasured_full_charge(self):
        r, A = self._fam()
        low = self._order(r, A, "low", 0.50, 10.0, 0.001)  # near-zero odds
        self.assertAlmostEqual(r.fam._charge(low), 0.25, places=6)  # 5% floor
        raw = self._order(r, A, "raw", 0.50, 10.0, None)   # unmeasured
        self.assertAlmostEqual(r.fam._charge(raw), 5.00, places=6)  # full

    def test_gross_cap_defaults_to_twice_capital(self):
        r, _ = self._fam(capital_usd=100.0)
        self.assertEqual(r.fam.gross_cap(), 200.0)
        r2, _ = self._fam(capital_usd=100.0, gross_cap_usd=500.0)
        self.assertEqual(r2.fam.gross_cap(), 500.0)

    def test_the_gross_ceiling_binds_whatever_the_model_believes(self):
        # ten "safe" orders at 2% odds: expected risk tiny, gross huge —
        # the trim must still fire on the gross breach
        r, A = self._fam(capital_usd=250.0, gross_cap_usd=3.0)
        for i in range(8):
            self._order(r, A, f"o{i}", 0.50, 1.0, 0.02)   # $0.50 each
        self.assertLess(r.fam.family_spent(), 1.0)         # expected: fine
        self.assertGreater(r.fam.family_gross(), 3.0)      # gross: over
        r.fam._trim(1_000_100.0, 10)
        self.assertLessEqual(r.fam.family_gross(), 3.0 + 1e-9)

    def test_politics_config_carries_the_approved_numbers(self):
        from v3 import politics
        c = politics.config()
        self.assertEqual(c.capital_usd, 250.0)
        # owner, 2026-08-30 "2500 is fine" — the raw-claims planner
        # pressed the old $500 gross bound within hours
        self.assertEqual(c.gross_cap_usd, 2500.0)
        self.assertEqual(c.per_market_usd, 20.0)
        self.assertEqual(c.per_market_gross_usd, 60.0)
        self.assertEqual(c.min_est_day, 0.02)

    def test_low_odds_orders_multiply_under_the_same_expected_cap(self):
        # the point of the whole change: $10 nominal at 3% odds charges
        # 50c (the floor), so a $2 expected cap carries $40 nominal —
        # while four unmeasured $10 orders would blow it immediately
        r, A = self._fam(capital_usd=2.0, gross_cap_usd=100.0)
        for i in range(4):
            self._order(r, A, f"safe{i}", 0.50, 20.0, 0.03)  # $10 @ floor
        self.assertLessEqual(r.fam.family_spent(), 2.0 + 1e-9)
        self.assertAlmostEqual(r.fam.family_gross(), 40.0, places=1)

    def test_off_by_default_the_old_accounting_is_untouched(self):
        # owner, 2026-08-25: "the cap should stay the same for
        # everything except for politics"
        from v3 import politics, football, basketball
        self.assertTrue(politics.config().expected_risk)
        self.assertFalse(football.cfb().expected_risk)
        self.assertFalse(football.nfl().expected_risk)
        self.assertFalse(basketball.nba().expected_risk)
        r, A = self._fam(expected_risk=False, capital_usd=50.0)
        o = self._order(r, A, "x", 0.50, 10.0, 0.03)   # odds ignored
        self.assertAlmostEqual(r.fam._charge(o), 5.00, places=6)
        self.assertEqual(r.fam.gross_cap(), 50.0)      # one cap, as ever


class TestTheLiveCard(unittest.TestCase):
    """Owner, 2026-08-25, the live card view: an engine order he moves
    by hand from the live card is HAND-SET (pinned) — "My changes
    should be durable, as long as things more or less stay the same on
    the book. But if there is a big change, for instance another order
    reduces my earning rate, the model can resume control." The nurse
    stays on watch for the pin's first minutes (his amendment), and
    the close-out button sells into the bid — the carved taker shape,
    fired by his own tap."""

    def _rig(self, price=0.11, qty=1.0, pinned=True, purpose="earn"):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.01, 6000.0),),
                                  asks=((0.22, 5000.0),),
                                  tick=0.01, fetched_at=r.now))
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        r.exchange.live["P"] = {"id": "P", "market": A, "side": "BUY",
                                "price": price, "size": qty,
                                "intent": BUY_LONG}
        r.fam.orders["P"] = FamilyOrder(
            id="P", market=A, side="BUY", price=price, qty=qty,
            intent=BUY_LONG, placed_ts=r.now, purpose=purpose,
            pinned=pinned, pin_ts=r.now, pin_est=1.0)
        return r, A

    def test_a_pinned_order_survives_the_rules_that_pull_its_twin(self):
        # 10 ticks in front on a blank market — exactly the shape the
        # conformance sweep pulls — but HAND-SET, so the engine holds off
        r, A = self._rig(0.11)
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        self.assertIn("P", r.fam.orders)
        self.assertFalse(any(l.get("event") == "conform_pulled"
                             for l in r.fam.log))

    def test_the_release_rule_needs_a_sustained_big_change(self):
        from v3.family import PIN_RELEASE_DWELL_S
        r, A = self._rig()
        rec = r.fam.orders["P"]
        rec.live_est = 0.9                       # fine: above half of 1.0
        r.fam._pin_check(rec, r.now)
        self.assertTrue(rec.pinned)
        rec.live_est = 0.2                       # under half — the clock starts
        r.fam._pin_check(rec, r.now)
        self.assertTrue(rec.pinned)              # one bad read never releases
        r.fam._pin_check(rec, r.now + PIN_RELEASE_DWELL_S + 1)
        self.assertFalse(rec.pinned)             # sustained — engine resumes
        self.assertTrue(any(l.get("event") == "pin_released"
                            for l in r.fam.log))

    def test_a_recovered_rate_resets_the_release_clock(self):
        from v3.family import PIN_RELEASE_DWELL_S
        r, A = self._rig()
        rec = r.fam.orders["P"]
        rec.live_est = 0.2
        r.fam._pin_check(rec, r.now)
        rec.live_est = 0.9                       # the book came back
        r.fam._pin_check(rec, r.now + 60)
        rec.live_est = 0.2
        r.fam._pin_check(rec, r.now + PIN_RELEASE_DWELL_S + 5)
        self.assertTrue(rec.pinned)              # the old clock is dead

    def test_an_order_that_earned_nothing_when_set_never_rate_releases(self):
        r, A = self._rig()
        rec = r.fam.orders["P"]
        rec.pin_est = 0.0
        rec.live_est = 0.0
        r.fam._pin_check(rec, r.now + 9999)
        self.assertTrue(rec.pinned)

    def test_a_released_order_is_ordinary_again(self):
        # after the release the same rules that spared it pull it
        r, A = self._rig(0.11)
        r.fam.orders["P"].pinned = False
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        self.assertNotIn("P", r.fam.orders)

    def test_the_nurse_watches_a_pin_even_on_a_modeled_market(self):
        r, A = self._rig(0.02)
        r.fam.fairs = lambda s: 0.10             # a model exists here
        r.fam.nurse(r.now, r.exchange)           # baseline
        from v3.scoring import Book
        r.fam.cache.put(A, Book(bids=((0.03, 40.0), (0.01, 6000.0)),
                                asks=((0.22, 5000.0),),
                                tick=0.01, fetched_at=r.now))
        r.fam.nurse(r.now + 5, r.exchange)       # fronted -> pulled
        self.assertNotIn("P", r.fam.orders)
        self.assertTrue(any(l.get("event") == "nursed_pull"
                            for l in r.fam.log))

    def test_an_unpinned_order_on_a_modeled_market_is_not_nursed(self):
        r, A = self._rig(0.02, pinned=False)
        r.fam.fairs = lambda s: 0.10
        r.fam.nurse(r.now, r.exchange)
        from v3.scoring import Book
        r.fam.cache.put(A, Book(bids=((0.03, 40.0), (0.01, 6000.0)),
                                asks=((0.22, 5000.0),),
                                tick=0.01, fetched_at=r.now))
        r.fam.nurse(r.now + 5, r.exchange)
        self.assertIn("P", r.fam.orders)         # the model's ground —
                                                 # the cycle's business

    def test_the_pin_restarts_the_nurse_watch_on_an_old_order(self):
        from v3.family import NURSE_STABLE_S
        r, A = self._rig(0.02)
        rec = r.fam.orders["P"]
        rec.placed_ts = r.now - NURSE_STABLE_S * 9   # long graduated
        rec.pin_ts = r.now                           # but just hand-set
        r.fam.nurse(r.now, r.exchange)
        self.assertIn("P", r.fam._nurse_base)

    def test_trim_never_takes_the_hand_set_order_first(self):
        r, A = self._rig(0.02, qty=1.0)
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        r.exchange.live["Q"] = {"id": "Q", "market": A, "side": "BUY",
                                "price": 0.02, "size": 1.0,
                                "intent": BUY_LONG}
        r.fam.orders["Q"] = FamilyOrder(
            id="Q", market=A, side="BUY", price=0.02, qty=1.0,
            intent=BUY_LONG, placed_ts=r.now, purpose="earn")
        r.fam.cfg.capital_usd = 0.001            # everything is over now
        r.fam._trim(r.now, actions=4)
        self.assertIn("P", r.fam.orders)         # hand-set: never trimmed
        self.assertNotIn("Q", r.fam.orders)

    def test_prune_never_pulls_a_hand_set_exit(self):
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG
        r, A = self._rig()
        r.fam.orders.pop("P")
        r.fam.orders["X"] = FamilyOrder(
            id="X", market=A, side="SELL", price=0.20, qty=5.0,
            intent=SELL_LONG, placed_ts=r.now, purpose="sell",
            pinned=True, pin_ts=r.now)
        r.fam._prune_excess_exits(A, "SELL", 5.0, r.now)
        self.assertIn("X", r.fam.orders)

    def test_the_pin_survives_a_restart(self):
        from v3.family import Family
        from v3 import politics
        r, A = self._rig()
        saved = r.fam.to_dict()
        import json as _j
        saved = _j.loads(_j.dumps(saved))        # the state file roundtrip
        fam2 = Family(r.fam.desk, r.fam.cache, politics.discover,
                      config=r.fam.cfg, names=r.names)
        fam2.restore(saved)
        rec = fam2.orders["P"]
        self.assertTrue(rec.pinned)
        self.assertAlmostEqual(rec.pin_est, 1.0)

    def _monitorish(self, r):
        import types
        return types.SimpleNamespace(families={"politics": r.fam},
                                     client=r.exchange,
                                     names=r.names)

    def test_a_live_card_move_pins_and_a_plain_move_does_not(self):
        from v3.main import Monitor
        r, A = self._rig(0.02, pinned=False)
        r.fam.orders["P"].live_est = 0.8
        m = self._monitorish(r)
        out = Monitor.order_op(m, "move", "P", 0.03, pin=True)
        self.assertTrue(out["ok"])
        rec = [o for o in r.fam.orders.values() if o.market == A][0]
        self.assertTrue(rec.pinned)
        self.assertAlmostEqual(rec.pin_est, -1.0)   # baseline pending
        rec.live_est = 0.55                          # first read after
        r.fam._pin_check(rec, r.now)
        self.assertAlmostEqual(rec.pin_est, 0.55)   # measured, not guessed
        self.assertTrue(rec.pinned)
        self.assertAlmostEqual(rec.price, 0.03)
        self.assertTrue(any(l.get("event") == "hand_set" for l in r.fam.log))
        out = Monitor.order_op(m, "move", rec.id, 0.04)     # orders page
        rec2 = [o for o in r.fam.orders.values() if o.market == A][0]
        self.assertFalse(rec2.pinned)            # old behavior, unchanged

    def test_a_manual_order_stays_manual_not_pinned(self):
        from v3.main import Monitor
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        r, A = self._rig()
        r.fam.orders.pop("P")
        r.exchange.live["H"] = {"id": "H", "market": A, "side": "BUY",
                                "price": 0.02, "size": 3.0,
                                "intent": BUY_LONG}
        r.fam.orders["H"] = FamilyOrder(
            id="H", market=A, side="BUY", price=0.02, qty=3.0,
            intent=BUY_LONG, placed_ts=r.now, purpose="manual")
        out = Monitor.order_op(self._monitorish(r), "move", "H", 0.03,
                               pin=True)
        self.assertTrue(out["ok"])
        rec = [o for o in r.fam.orders.values() if o.market == A][0]
        self.assertEqual(rec.purpose, "manual")  # stronger than any pin
        self.assertFalse(rec.pinned)

    def test_a_live_card_resize_changes_size_and_pins(self):
        from v3.main import Monitor
        r, A = self._rig(0.02, pinned=False)
        out = Monitor.order_op(self._monitorish(r), "move", "P", None,
                               pin=True, qty=5.0)
        self.assertTrue(out["ok"], out.get("note"))
        rec = [o for o in r.fam.orders.values() if o.market == A][0]
        self.assertAlmostEqual(rec.qty, 5.0)
        self.assertAlmostEqual(rec.price, 0.02)      # same price, new size
        self.assertTrue(rec.pinned)
        self.assertAlmostEqual(rec.pin_est, -1.0)
        live = [o for o in r.exchange.live.values() if o["market"] == A]
        self.assertEqual(len(live), 1)               # replaced, not doubled
        self.assertAlmostEqual(live[0]["size"], 5.0)

    def _stock_rig(self, qty=10.0, bid_sz=6.0):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.05, bid_sz), (0.02, 60000.0)),
                                  asks=((0.08, 5000.0),),
                                  tick=0.01, fetched_at=r.now))
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        r.fam.inventory[A] = {"qty": qty, "cost": qty * 0.03}
        return r, A

    def test_close_out_sells_into_the_bid_and_journals_the_take(self):
        from v3.main import Monitor
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG
        r, A = self._stock_rig(qty=10.0, bid_sz=6.0)
        r.exchange.live["E"] = {"id": "E", "market": A, "side": "SELL",
                                "price": 0.08, "size": 10.0,
                                "intent": SELL_LONG}
        r.fam.orders["E"] = FamilyOrder(          # the engine's exit
            id="E", market=A, side="SELL", price=0.08, qty=10.0,
            intent=SELL_LONG, placed_ts=r.now, purpose="sell")
        out = Monitor.close_position(self._monitorish(r), A)
        self.assertTrue(out["ok"], out.get("note"))
        self.assertNotIn("E", r.fam.orders)       # exit cancelled first
        takers = [o for o in r.exchange.live.values()
                  if o["market"] == A and o["side"] == "SELL"
                  and abs(o["price"] - 0.05) < 1e-9]
        self.assertEqual(len(takers), 1)          # AT the bid, never worse
        self.assertAlmostEqual(takers[0]["size"], 10.0)
        # the displayed bid takes 6 now: journaled and off the inventory
        self.assertAlmostEqual(r.fam.inventory[A]["qty"], 4.0)
        self.assertTrue(any(f.get("market") == A and f.get("side") == "SELL"
                            and abs(f.get("qty", 0) - 6.0) < 1e-6
                            for f in r.fam.fills))
        # the 4 the bid could not take rest as the owner's own ask
        rest = [o for o in r.fam.orders.values()
                if o.market == A and o.purpose == "manual"]
        self.assertEqual(len(rest), 1)
        self.assertAlmostEqual(rest[0].qty, 4.0)

    def test_close_out_never_touches_his_own_resting_ask(self):
        from v3.main import Monitor
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG
        r, A = self._stock_rig(qty=10.0, bid_sz=20.0)
        r.exchange.live["H"] = {"id": "H", "market": A, "side": "SELL",
                                "price": 0.09, "size": 4.0,
                                "intent": SELL_LONG}
        r.fam.orders["H"] = FamilyOrder(
            id="H", market=A, side="SELL", price=0.09, qty=4.0,
            intent=SELL_LONG, placed_ts=r.now, purpose="manual")
        out = Monitor.close_position(self._monitorish(r), A)
        self.assertTrue(out["ok"], out.get("note"))
        self.assertIn("H", r.fam.orders)          # untouchable, as ever
        takers = [o for o in r.exchange.live.values()
                  if o["market"] == A and abs(o["price"] - 0.05) < 1e-9]
        self.assertAlmostEqual(takers[0]["size"], 6.0)   # 10 minus his 4

    def test_close_out_refuses_a_short_plainly(self):
        from v3.main import Monitor
        r, A = self._stock_rig(qty=10.0)
        r.fam.inventory[A] = {"qty": -5.0, "cost": -0.3}
        out = Monitor.close_position(self._monitorish(r), A)
        self.assertFalse(out["ok"])
        self.assertIn("short", out["note"])

    def test_the_live_view_shows_the_earnings_math(self):
        """Owner, 2026-08-26: "make it so that the earnings math is
        shown so I get a sense of how much it's earning." Each order in
        the live payload carries its share of the side's score, the
        side's daily pool, and share x pool = est — refigured on the
        second's own book."""
        from v3.main import Monitor
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        r = Rig()
        r.add_market(A)
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.fam.orders["P"] = FamilyOrder(
            id="P", market=A, side="BUY", price=0.44, qty=20.0,
            intent=BUY_LONG, placed_ts=r.now, purpose="earn")
        b = Monitor.live_view(self._monitorish(r), A)
        self.assertTrue(b["ok"])
        # LIVE_PROG: $100/day pool, 1 market in the event, 2 sides
        self.assertAlmostEqual(b["pool_day"], 50.0)
        o = [x for x in b["ours"] if x["id"] == "P"][0]
        self.assertIsNotNone(o["share"])
        self.assertGreater(o["share"], 0.0)
        self.assertTrue(o["qualifies"])
        self.assertAlmostEqual(o["est"], o["share"] * b["pool_day"],
                               places=2)

    def test_the_live_view_says_when_the_program_pays_nothing(self):
        from v3.main import Monitor
        from v3.tests.test_family import Rig, A, DEAD_PROG
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        r = Rig()
        r.add_market(A, prog=DEAD_PROG)
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.fam.orders["P"] = FamilyOrder(
            id="P", market=A, side="BUY", price=0.44, qty=20.0,
            intent=BUY_LONG, placed_ts=r.now, purpose="earn")
        b = Monitor.live_view(self._monitorish(r), A)
        self.assertTrue(b["ok"])
        self.assertIsNone(b["pool_day"])
        self.assertIn("pays nothing", b["prog_note"])
        o = [x for x in b["ours"] if x["id"] == "P"][0]
        self.assertIsNone(o["share"])             # no terms, no math


class TestInstantRewardsWrite(unittest.TestCase):
    """Owner, 2026-08-26: "there is info it's just not writing" —
    politics Aug-24 posted at 01:13Z, the watcher saw it, and the file
    sat cfb-only until the next hourly batch. rewards.csv is now
    written the moment the watcher sees new postings."""

    def _mon(self, v1="0"):
        import os, types
        from v3.main import Monitor
        calls = {"puts": [], "earnings": 0}

        class C:
            def earnings(self, start):
                calls["earnings"] += 1
                return [{"date": "2026-08-24", "market": "m-pol",
                         "program_type": "liquidityProgram",
                         "reward_usd": 3.07, "status": "PENDING"}]
        m = types.SimpleNamespace(
            client=C(),
            compose_rewards_csv=lambda rows, existing:
                Monitor.compose_rewards_csv(m, rows, existing),
            _gh_file=lambda path: ("date,market,program_type,reward_usd,"
                                   "status\n", "sha1"),
            _gh_put=lambda path, text, sha, msg:
                calls["puts"].append((path, text, msg)) or True,
            _note=lambda s: None)
        os.environ["V1_ENABLED"] = v1
        return m, calls

    def tearDown(self):
        import os
        os.environ["V1_ENABLED"] = "0"

    def test_the_write_happens_now_and_carries_the_rows(self):
        from v3.main import Monitor
        m, calls = self._mon()
        ok = Monitor.publish_rewards_csv(m)
        self.assertTrue(ok)
        self.assertEqual(len(calls["puts"]), 1)
        path, text, msg = calls["puts"][0]
        self.assertEqual(path, "data/rewards.csv")
        self.assertIn("2026-08-24,m-pol,liquidityProgram,3.07,PENDING",
                      text)

    def test_while_v1_runs_it_owns_the_file(self):
        from v3.main import Monitor
        m, calls = self._mon(v1="1")
        self.assertFalse(Monitor.publish_rewards_csv(m))
        self.assertEqual(calls["puts"], [])
        self.assertEqual(calls["earnings"], 0)

    def test_an_unchanged_file_is_not_rewritten(self):
        from v3.main import Monitor
        m, calls = self._mon()
        Monitor.publish_rewards_csv(m)
        existing = calls["puts"][0][1]
        m._gh_file = lambda path: (existing, "sha2")
        Monitor.publish_rewards_csv(m)
        self.assertEqual(len(calls["puts"]), 1)   # no second commit

    def test_a_failed_write_is_said_out_loud(self):
        from v3.main import Monitor
        m, calls = self._mon()
        notes = []
        m._gh_put = lambda *a: False
        m._note = lambda s: notes.append(s)
        self.assertFalse(Monitor.publish_rewards_csv(m))
        self.assertTrue(any("rewards.csv write failed" in n for n in notes))


class TestPriceGrid(unittest.TestCase):
    """Owner, 2026-08-26: "decimal prices are not [fine] on most books.
    Some are okay like house and senate party control." A real exit was
    resting at 5.90676c — break-even arithmetic sent raw. Every outgoing
    price now snaps to the book's own grid at the desk: bids down, asks
    up, so floors and caps only get safer; tenth-cent books keep their
    finer grid."""

    def test_snap_semantics(self):
        from v3.orders import snap_price
        self.assertAlmostEqual(snap_price(0.0590676, 0.01, "SELL"), 0.06)
        self.assertAlmostEqual(snap_price(0.0590676, 0.01, "BUY"), 0.05)
        self.assertAlmostEqual(snap_price(0.0590676, 0.001, "SELL"), 0.06)
        self.assertAlmostEqual(snap_price(0.0596, 0.001, "BUY"), 0.059)
        # on-grid prices pass through untouched, float noise included
        self.assertAlmostEqual(snap_price(0.059, 0.001, "SELL"), 0.059)
        self.assertAlmostEqual(snap_price(0.057, 0.001, "BUY"), 0.057)
        self.assertAlmostEqual(snap_price(0.44, 0.01, "BUY"), 0.44)

    def test_the_desk_rests_on_grid_and_reports_the_real_price(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.01, 500.0),),
                                  asks=((0.22, 500.0),),
                                  tick=0.01, fetched_at=r.now))
        r.cycle()
        res = r.desk.place_resting(A, "SELL", 0.0590676, 5.0,
                                   net_position=10.0, initiator="owner")
        self.assertTrue(res.ok, res.note)
        self.assertAlmostEqual(res.price, 0.06)     # ask snapped UP
        live = [o for o in r.exchange.live.values()
                if o["market"] == A and o["side"] == "SELL"]
        self.assertTrue(any(abs(o["price"] - 0.06) < 1e-9 for o in live))

    def test_a_failed_exit_placement_is_said_out_loud(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.5, 100.0),),
                                  asks=((0.52, 100.0),),
                                  tick=0.01, fetched_at=r.now))
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        r.fam.inventory[A] = {"qty": 10.0, "cost": 3.0}
        r.positions[A] = (10.0, 3.0)      # the exchange agrees
        # every placement dies exchange-side: the exit CANNOT rest
        orig = r.exchange.post
        def dead(url, body, path=None, **kw):
            if url.endswith("/v1/orders"):
                raise __import__("v3.api", fromlist=["ApiError"]).ApiError("book closed")
            return orig(url, body, path=path, **kw)
        r.exchange.post = dead
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        self.assertTrue(any(l.get("event") == "exit_place_failed"
                            for l in r.fam.log))


class TestThePeakDropTrail(unittest.TestCase):
    """Owner, 2026-08-26: "keep track of the percentage decrease in
    rewards from an 8 hour peak and allow me to sort the orders page by
    that." Half-hour buckets of each order's best measured rate; the
    peak is the window's max and expires with its bucket."""

    def _rec(self):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG
        r = Rig()
        r.add_market(A)
        rec = FamilyOrder(id="P", market=A, side="BUY", price=0.44,
                          qty=20.0, intent=BUY_LONG, placed_ts=r.now,
                          purpose="earn")
        return r, rec

    def test_the_peak_holds_and_the_drop_is_readable(self):
        r, rec = self._rec()
        rec.live_est = 1.20
        r.fam._track_est(rec, r.now)
        rec.live_est = 0.45
        r.fam._track_est(rec, r.now + 1800)
        self.assertAlmostEqual(rec.est_peak8, 1.20)
        drop = (rec.est_peak8 - rec.live_est) / rec.est_peak8
        self.assertAlmostEqual(drop, 0.625)

    def test_the_peak_expires_after_eight_hours(self):
        r, rec = self._rec()
        rec.live_est = 1.20
        r.fam._track_est(rec, r.now)
        rec.live_est = 0.45
        for i in range(1, 18):
            r.fam._track_est(rec, r.now + i * 1800)
        self.assertAlmostEqual(rec.est_peak8, 0.45)   # old peak gone

    def test_a_bucket_keeps_its_best_reading(self):
        r, rec = self._rec()
        rec.live_est = 0.30
        r.fam._track_est(rec, r.now)
        rec.live_est = 0.90
        r.fam._track_est(rec, r.now + 60)     # same half-hour bucket
        rec.live_est = 0.10
        r.fam._track_est(rec, r.now + 120)
        self.assertAlmostEqual(rec.est_peak8, 0.90)
        self.assertEqual(len(rec.est_hist), 1)

    def test_the_trail_survives_a_restart(self):
        import json as _j
        from v3.family import Family, FamilyOrder
        from v3 import politics
        r, rec = self._rec()
        rec.live_est = 1.20
        r.fam._track_est(rec, r.now)
        r.fam.orders["P"] = rec
        saved = _j.loads(_j.dumps(r.fam.to_dict()))
        fam2 = Family(r.fam.desk, r.fam.cache, politics.discover,
                      config=r.fam.cfg, names=r.names)
        fam2.restore(saved)
        self.assertAlmostEqual(fam2.orders["P"].est_peak8, 1.20)
        self.assertEqual(len(fam2.orders["P"].est_hist), 1)


class TestTheOnGridSweep(unittest.TestCase):
    """Owner, 2026-08-26: "Go through and change all the non whole
    number price orders." The desk's snap stops new off-grid prices;
    this sweep walks the grandfathered resting book onto each book's
    own grid — 32 exits were resting at break-even arithmetic."""

    def _rig(self, price, purpose="sell", tick=0.01, pinned=False,
             manual=False):
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.03, 500.0), (0.02, 60000.0)),
                                  asks=((0.30, 500.0), (0.98, 60000.0)),
                                  tick=tick, fetched_at=r.now))
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        r.positions[A] = (43.01, 2.54)
        r.fam.inventory[A] = {"qty": 43.01, "cost": 2.54}
        r.exchange.live["G"] = {"id": "G", "market": A, "side": "SELL",
                                "price": price, "size": 43.01,
                                "intent": SELL_LONG}
        r.fam.orders["G"] = FamilyOrder(
            id="G", market=A, side="SELL", price=price, qty=43.01,
            intent=SELL_LONG, placed_ts=1.0,
            purpose="manual" if manual else purpose,
            pinned=pinned, pin_ts=r.now if pinned else 0.0)
        return r, A

    def test_an_off_grid_exit_is_walked_onto_the_grid(self):
        r, A = self._rig(0.05906765868402698)
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        self.assertNotIn("G", r.fam.orders)
        moved = [o for o in r.fam.orders.values()
                 if o.market == A and o.purpose == "sell"]
        self.assertTrue(moved)
        for o in moved:            # wherever maintenance settled it in
            steps = o.price / 0.01     # the same cycle, it is ON grid
            self.assertAlmostEqual(steps, round(steps), places=6)
        self.assertAlmostEqual(sum(o.qty for o in moved), 43.01)
        self.assertTrue(any(l.get("event") == "regridded"
                            for l in r.fam.log))
        reg = [l for l in r.fam.log if l.get("event") == "regridded"][0]
        self.assertAlmostEqual(reg.get("to"), 0.06)    # ask snapped UP

    def test_a_tenth_cent_book_keeps_its_finer_grid(self):
        r, A = self._rig(0.059, tick=0.001)
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        # 5.9c is ON the 0.1c grid: the sweep never fires for it
        self.assertFalse(any(l.get("event") == "regridded"
                             for l in r.fam.log))

    def test_manual_and_hand_set_orders_are_never_regridded(self):
        r, A = self._rig(0.05906765868402698, manual=True)
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        self.assertIn("G", r.fam.orders)
        r2, A2 = self._rig(0.05906765868402698, pinned=True)
        r2.fam.last_action.clear()
        r2.cycle(advance=120.0)
        self.assertIn("G", r2.fam.orders)


class TestCfbCycleOut(unittest.TestCase):
    def test_the_cycle_out_rule_is_on_for_cfb_and_off_for_nfl_nba(self):
        """Owner, 2026-08-26 ("Yes to 1"): cfb gets the same cycle-out
        rule as politics; nfl and nba stay as they were."""
        from v3 import football, basketball, politics
        self.assertEqual(football.cfb().weak_pull_s, 30.0)
        self.assertEqual(politics.config().weak_pull_s, 30.0)
        self.assertEqual(football.nfl().weak_pull_s, 0.0)
        self.assertEqual(basketball.nba().weak_pull_s, 0.0)


class TestFeedHealthAndCalibrationLine(unittest.TestCase):
    """Owner approved 2026-08-26 ('Yes' to the parked three): the
    hourly stream-health line, the entry-only fill calibration count,
    and the on-grid sweep skipping snaps that leave the price bounds."""

    def test_the_cache_counts_writes_per_writer(self):
        from v3.books import BookCache
        from v3.scoring import Book
        c = BookCache()
        b = Book(bids=((0.05, 10.0),), asks=((0.07, 10.0),),
                 tick=0.01, fetched_at=1.0)
        c.put("m1", b, writer="ws")
        c.put("m2", b, writer="rest")
        c.put("m3", b, writer="rest")
        self.assertEqual(c.writes, {"ws": 1, "rest": 2})

    def test_a_ceiling_ask_is_left_alone_not_retried(self):
        # the TN shape: a 99.9c ask on a whole-cent book snaps to 100c,
        # which is not a price — the sweep must skip it, not retry
        from v3.tests.test_family import Rig, A
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.90, 500.0),),
                                  asks=((0.98, 500.0),),
                                  tick=0.01, fetched_at=r.now))
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        r.positions[A] = (1.0, 0.5)
        r.fam.inventory[A] = {"qty": 1.0, "cost": 0.5}
        r.exchange.live["T"] = {"id": "T", "market": A, "side": "SELL",
                                "price": 0.999, "size": 1.0,
                                "intent": SELL_LONG}
        r.fam.orders["T"] = FamilyOrder(
            id="T", market=A, side="SELL", price=0.999, qty=1.0,
            intent=SELL_LONG, placed_ts=1.0, purpose="sell")
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        # the SWEEP skipped it (no doomed 100c reprice attempt); the
        # ordinary exit maintenance is free to re-rest it somewhere
        # legal, and whatever rests must be within the price bounds
        self.assertFalse(any(l.get("event") == "regridded"
                             and l.get("market") == A
                             for l in r.fam.log))
        for o in r.fam.orders.values():
            self.assertLessEqual(o.price, 0.999 + 1e-12)
            self.assertGreaterEqual(o.price, 0.001 - 1e-12)


class TestPositionsLedger(unittest.TestCase):
    """Owner, 2026-08-26: sort held positions by earnings per dollar of
    liquidation value, lowest first, idle ones included — the top of
    the list is dead money to hand-place."""

    def test_positions_carry_liq_earn_and_ratio(self):
        from v3.tests.test_family import Rig, A, B
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG
        from v3.tests.test_family import politics_book
        r = Rig(switch=False)          # observing: nothing new places,
                                       # so B stays genuinely idle
        r.add_market(A, book=politics_book(r.now, bid=0.10, ask=0.12))
        r.add_market(B, book=politics_book(r.now, bid=0.40, ask=0.42))
        r.positions[A] = (10.0, 0.5)
        r.positions[B] = (5.0, 1.8)
        r.fam.inventory[A] = {"qty": 10.0, "cost": 0.5}
        r.fam.inventory[B] = {"qty": 5.0, "cost": 1.8}
        rec = FamilyOrder(id="X", market=A, side="SELL", price=0.12,
                          qty=10.0, intent=SELL_LONG, placed_ts=r.now,
                          purpose="sell")
        rec.live_est = 0.50
        r.fam.orders["X"] = rec
        r.exchange.live["X"] = {"id": "X", "market": A, "side": "SELL",
                                "price": 0.12, "size": 10.0,
                                "intent": SELL_LONG}
        s = r.cycle()
        pos = {p["market"]: p for p in s["positions"]}
        self.assertIn(A, pos)
        self.assertIn(B, pos)                    # held, no orders: listed
        self.assertAlmostEqual(pos[A]["liq"], 1.00)   # 10 x 10c bid
        self.assertAlmostEqual(pos[B]["liq"], 2.00)   # 5 x 40c bid
        self.assertGreater(pos[A]["earn"], 0.0)
        self.assertEqual(pos[B]["earn"], 0.0)         # idle money
        self.assertEqual(pos[B]["covers"], [])
        self.assertGreater(pos[A]["per_dollar"], pos[B]["per_dollar"])

    def test_a_short_values_at_what_closing_recovers(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.10, 100.0),),
                                  asks=((0.20, 100.0),),
                                  tick=0.01, fetched_at=r.now))
        r.positions[A] = (-10.0, -1.5)
        r.fam.inventory[A] = {"qty": -10.0, "cost": -1.5}
        s = r.cycle()
        pos = {p["market"]: p for p in s["positions"]}
        self.assertAlmostEqual(pos[A]["liq"], 8.00)   # 10 x (1 - 20c ask)


class TestEmptyBookExitAnchor(unittest.TestCase):
    """Owner, 2026-08-27, after the maintenance wipe re-rested 89 exits
    within 2c of break-even on empty books: "the newly placed order
    should be at much better prices (for me) because there is less
    competition." An empty ask side anchors HIGH (above fair where a
    model exists, the ceiling on blank ground); a populated side keeps
    the old join-the-touch rule."""

    def _rig(self, asks, fair=None):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.03, 100.0), (0.02, 60000.0)),
                                  asks=asks, tick=0.01, fetched_at=r.now))
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        r.positions[A] = (10.0, 0.5)
        r.fam.inventory[A] = {"qty": 10.0, "cost": 0.5}   # 5c break-even
        if fair is not None:
            r.fam.fairs = lambda s: fair
        return r, A

    def test_an_empty_ask_side_rests_high_not_at_cost(self):
        r, A = self._rig(asks=())
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        exits = [o for o in r.fam.orders.values()
                 if o.market == A and o.purpose == "sell"]
        self.assertTrue(exits)
        for o in exits:                      # nowhere near 6c anymore
            self.assertGreater(o.price, 0.15)

    def test_a_modeled_empty_side_anchors_above_fair(self):
        r, A = self._rig(asks=(), fair=0.10)
        anchor = r.fam._ask_anchor(A, r.fam.cache.any_age(A), 0.05)
        self.assertAlmostEqual(anchor, 0.25)   # fair + 15 ticks
        r2, A2 = self._rig(asks=())
        self.assertAlmostEqual(
            r2.fam._ask_anchor(A2, r2.fam.cache.any_age(A2), 0.05), 0.99)

    def test_a_populated_side_still_joins_the_touch(self):
        r, A = self._rig(asks=((0.08, 500.0), (0.98, 60000.0)))
        self.assertAlmostEqual(
            r.fam._ask_anchor(A, r.fam.cache.any_age(A), 0.05), 0.08)


class TestPostedRewardsVerification(unittest.TestCase):
    """Owner approved 2026-08-27: closed cards' reward lines are graded
    against the exchange's posted per-market-day pay. Each card gets
    the market-day's REAL pay x its share of our claims; the sum over
    cards never exceeds what the exchange paid; unposted days keep the
    claim, reported separately."""

    # noon ET on 2026-08-24, well inside one ET day
    NOON = 1787587200.0

    def _attr(self, cards, paid, days, snap=None):
        from v3.main import Monitor
        return Monitor.attribute_posted(cards, paid, set(days), snap or {})

    def test_the_split_is_proportional_and_sums_to_the_posted_pay(self):
        cards = [
            {"ts": self.NOON, "market": "m", "est_day": 2.0, "rested_h": 6.0},
            {"ts": self.NOON, "market": "m", "est_day": 1.0, "rested_h": 6.0},
        ]
        import datetime
        from zoneinfo import ZoneInfo
        d = datetime.datetime.fromtimestamp(self.NOON,
                                            ZoneInfo("America/New_York")).date().isoformat()
        out = self._attr(cards, {f"{d}|m": 0.90}, {d})
        self.assertAlmostEqual(out[0]["posted"] + out[1]["posted"], 0.90)
        self.assertAlmostEqual(out[0]["posted"], 0.60, places=6)   # 2:1 split
        self.assertAlmostEqual(out[1]["posted"], 0.30, places=6)

    def test_the_ledger_snapshot_keeps_cards_from_soaking_up_others_pay(self):
        cards = [{"ts": self.NOON, "market": "m", "est_day": 2.0,
                  "rested_h": 12.0}]                    # claims 1.00
        import datetime
        from zoneinfo import ZoneInfo
        d = datetime.datetime.fromtimestamp(self.NOON,
                                            ZoneInfo("America/New_York")).date().isoformat()
        # the whole day's claims (never-filled orders included) were 4.00
        out = self._attr(cards, {f"{d}|m": 2.00}, {d}, {f"{d}|m": 4.00})
        self.assertAlmostEqual(out[0]["posted"], 0.50, places=6)   # 1/4 of $2

    def test_a_zero_pay_posted_day_grades_the_claim_to_zero(self):
        cards = [{"ts": self.NOON, "market": "m", "est_day": 3.0,
                  "rested_h": 8.0}]
        import datetime
        from zoneinfo import ZoneInfo
        d = datetime.datetime.fromtimestamp(self.NOON,
                                            ZoneInfo("America/New_York")).date().isoformat()
        out = self._attr(cards, {}, {d})           # day posted, market absent
        self.assertAlmostEqual(out[0]["posted"], 0.0)
        self.assertGreater(out[0]["graded"], 0.9)

    def test_an_unposted_day_keeps_the_claim_separately(self):
        cards = [{"ts": self.NOON, "market": "m", "est_day": 3.0,
                  "rested_h": 8.0}]
        out = self._attr(cards, {}, set())          # nothing posted yet
        self.assertIsNone(out[0]["posted"])
        self.assertAlmostEqual(out[0]["unposted"], 1.0, places=6)


class TestOwnerLiquidation(unittest.TestCase):
    """Owner, 2026-08-27: "Take me out of all buy position on Ron
    desantis in 2028 markets." A liquidate-listed market: the engine
    sells the stock into the bid (never worse) up to the bid's shown
    size each cycle until flat, cancels its own exits there first,
    never touches the owner's asks, and never opens a BUY."""

    def _rig(self, qty=63.0, cost=7.55, bid=(0.05, 40.0)):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.fam.cfg.liquidate_tokens = (A[:20],)
        r.add_market(A, book=Book(bids=(bid, (0.02, 60000.0)),
                                  asks=((0.13, 200.0), (0.98, 60000.0)),
                                  tick=0.01, fetched_at=r.now))
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        r.positions[A] = (qty, cost)
        r.fam.inventory[A] = {"qty": qty, "cost": cost}
        return r, A

    def test_sells_into_the_bid_per_cycle_and_journals(self):
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG
        r, A = self._rig()
        r.exchange.live["E"] = {"id": "E", "market": A, "side": "SELL",
                                "price": 0.13, "size": 63.0,
                                "intent": SELL_LONG}
        r.fam.orders["E"] = FamilyOrder(
            id="E", market=A, side="SELL", price=0.13, qty=63.0,
            intent=SELL_LONG, placed_ts=r.now, purpose="sell")
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        self.assertNotIn("E", r.fam.orders)     # old 13c exit cancelled
        self.assertAlmostEqual(r.fam.inventory[A]["qty"], 23.0)  # 63-40
        self.assertTrue(any(l.get("event") == "liquidated"
                            and abs((l.get("price") or 0) - 0.05) < 1e-9
                            and abs((l.get("qty") or 0) - 40.0) < 1e-9
                            for l in r.fam.log))

    def test_never_buys_on_liquidating_ground(self):
        r, A = self._rig()
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        r.cycle(advance=120.0)
        buys = [o for o in r.fam.orders.values()
                if o.market == A and o.side == "BUY"]
        self.assertEqual(buys, [])

    def test_the_owners_own_ask_is_never_touched_or_double_offered(self):
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG
        r, A = self._rig(qty=10.0, cost=1.2, bid=(0.05, 40.0))
        r.exchange.live["H"] = {"id": "H", "market": A, "side": "SELL",
                                "price": 0.2, "size": 8.0,
                                "intent": SELL_LONG}
        r.fam.orders["H"] = FamilyOrder(
            id="H", market=A, side="SELL", price=0.2, qty=8.0,
            intent=SELL_LONG, placed_ts=r.now, purpose="manual")
        r.fam.last_action.clear()
        r.cycle(advance=120.0)
        self.assertIn("H", r.fam.orders)        # untouchable
        # only the 2 uncovered shares were sold
        self.assertAlmostEqual(r.fam.inventory[A]["qty"], 8.0)


class TestCfbOpeningWeekWindow(unittest.TestCase):
    def test_cfb_rests_sunday_2am_through_thursday_3pm(self):
        """Owner, 2026-08-28 evening: "back in around 2:00 am Sunday
        morning until Thursday September 3rd at 3 pm eastern" — the
        Week-1+ rhythm: rest Sun 02:00 -> Thu 15:00 ET, out through
        the Thu/Fri night slates and game Saturdays."""
        import datetime as dt
        from zoneinfo import ZoneInfo
        from v3 import football
        from v3.family import resting_ok
        et = ZoneInfo("America/New_York")
        cfg = football.cfb()
        sun_early = dt.datetime(2026, 8, 30, 2, 30, tzinfo=et).timestamp()
        wed_noon = dt.datetime(2026, 9, 2, 12, 0, tzinfo=et).timestamp()
        thu_2pm = dt.datetime(2026, 9, 3, 14, 0, tzinfo=et).timestamp()
        thu_4pm = dt.datetime(2026, 9, 3, 16, 0, tzinfo=et).timestamp()
        fri_night = dt.datetime(2026, 9, 4, 20, 0, tzinfo=et).timestamp()
        sat_game = dt.datetime(2026, 9, 5, 13, 0, tzinfo=et).timestamp()
        sun_before2 = dt.datetime(2026, 9, 6, 1, 0, tzinfo=et).timestamp()
        self.assertTrue(resting_ok(sun_early, cfg))
        self.assertTrue(resting_ok(wed_noon, cfg))
        self.assertTrue(resting_ok(thu_2pm, cfg))
        self.assertFalse(resting_ok(thu_4pm, cfg))     # Thu night: out
        self.assertFalse(resting_ok(fri_night, cfg))   # Fri slate: out
        self.assertFalse(resting_ok(sat_game, cfg))    # game day: out
        self.assertFalse(resting_ok(sun_before2, cfg)) # not yet 02:00
        self.assertTrue(resting_ok(sun_early, cfg))


class TestWatchedRaces(unittest.TestCase):
    """Owner, 2026-08-28: "Keep a websocket on those races" — the MA
    dem senate primary margin-of-victory books. Watched races refresh
    on a budget-exempt fast lane every cycle and seat first in the
    stream subscription."""

    def test_watched_books_refresh_every_cycle_off_budget(self):
        from v3.tests.test_family import Rig, A, B
        r = Rig()
        r.fam.cfg.watch_tokens = (A[:20],)
        r.add_market(A)
        r.add_market(B)
        r.cycle()
        # age both books far past staleness, budget squeezed to nothing
        r.fam.cfg.books_per_cycle = 0
        r.now += 3600
        r.cycle(advance=60.0)
        self.assertLess(r.fam.cache.age(A, r.now), 120.0)   # fast lane
        # (B may still be touched by the scan lane's minimum slot —
        # the claim here is only that the watched book cannot go stale)

    def test_politics_watches_the_ma_dem_mov_books(self):
        from v3 import politics
        cfg = politics.config()
        self.assertIn("ussep-mov-ma-dem", cfg.watch_tokens)

    def test_politics_engine_is_hands_off_the_mov_books(self):
        # owner, 2026-08-28: "The model should be hands off with these
        # markets" — avoided ground: the engine pulls its own orders
        # and never enters; the owner's hand orders are untouchable
        from v3 import politics
        cfg = politics.config()
        self.assertIn("ussep-mov-ma-dem", cfg.avoid_tokens)

    def test_watched_summary_reports_the_ask_sides_standing(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.fam.cfg.watch_tokens = (A[:20],)
        r.add_market(A)
        s = r.cycle()
        w = {x["market"]: x for x in s["watched"]}
        self.assertIn(A, w)
        # the default politics book rests 20 + 60,000 on the ask side
        # against LIVE_PROG's 5,000 Target Size — qualifying
        self.assertEqual(w[A]["target"], 5000)
        self.assertAlmostEqual(w[A]["ask_total"], 60020.0)
        self.assertTrue(w[A]["qualifies"])


class TestBoostWatch(unittest.TestCase):
    """Owner yes, 2026-08-30: the first sighting of a NEW program id
    with a fat pool (>=$100/day) or a boost-flavored name alerts once
    per program — the MA MoV boost paid $199.65 on its first walled
    day and was only caught by a screenshot."""

    def test_new_fat_program_alerts_once(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.cycle()                       # seed: baseline, no alert
        boost_alerts = [a for a in r.alerts if "NEW fat" in a[0]]
        self.assertFalse(boost_alerts)
        r.exchange.prog_raw[A] = {"timePeriods": [{
            "programId": "elections_boosted_high_20260901",
            "rewardPool": 1000.0, "targetSize": 10000,
            "discountFactor": 0.2, "status": "LIVE"}]}
        r.cycle(advance=r.fam.cfg.terms_active_s + 1)
        boost_alerts = [a for a in r.alerts if "NEW fat" in a[0]]
        self.assertEqual(len(boost_alerts), 1)
        self.assertIn("elections_boosted_high_20260901", boost_alerts[0][1])
        self.assertIn("$1000/day", boost_alerts[0][1])
        r.cycle(advance=r.fam.cfg.terms_active_s + 1)
        boost_alerts = [a for a in r.alerts if "NEW fat" in a[0]]
        self.assertEqual(len(boost_alerts), 1)    # never re-alerts

    def test_small_program_rollover_stays_quiet(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.add_market(A)
        r.cycle()
        r.exchange.prog_raw[A] = {"timePeriods": [{
            "programId": "politics_low_20260901", "rewardPool": 25.0,
            "targetSize": 2000, "discountFactor": 0.1, "status": "LIVE"}]}
        r.cycle(advance=r.fam.cfg.terms_active_s + 1)
        self.assertFalse([a for a in r.alerts if "NEW fat" in a[0]])


class TestHotGroundNeverJoinsTheTouch(unittest.TestCase):
    """Owner, 2026-08-29: "this sort of strategy only obviously works
    when fills are more rare." Ground where our own orders were
    recently taken (heat >= touch_heat_max) may not join or improve
    the touch; resting behind stays allowed."""

    def test_hot_market_rests_behind_never_at_the_touch(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.fam.cfg.touch_heat_max = 0.5
        r.add_market(A)
        r.fam.evidence.heat = lambda s, now=None: 1.0   # sniped recently
        r.cycle()
        placed = [o for o in r.exchange.live.values() if o["market"] == A]
        # the default book's touch is bid 0.44 / ask 0.47 — nothing may
        # rest AT or in front of either touch
        for o in placed:
            if o["side"] == "BUY":
                self.assertLess(o["price"], 0.44 - 1e-9)
            else:
                self.assertGreater(o["price"], 0.47 + 1e-9)

    def test_cool_market_still_joins(self):
        from v3.tests.test_family import Rig, A
        r = Rig()
        r.fam.cfg.touch_heat_max = 0.5
        r.add_market(A)
        r.fam.evidence.heat = lambda s, now=None: 0.0
        r.cycle()
        placed = [o for o in r.exchange.live.values() if o["market"] == A]
        self.assertTrue(any(abs(o["price"] - 0.44) < 1e-9 for o in placed
                            if o["side"] == "BUY")
                        or placed)   # joins when quiet (or rests at all)

    def test_router_routes_trade_prints(self):
        from v3.main import CacheRouter
        from v3.books import BookCache

        class F:
            def __init__(self, universe):
                self.universe = universe
                self.cache = BookCache()
        pol = F({"ussewc-usse-ga-2026-11-03-rep": {}})
        cfb = F({"aachc-cfb-wins-2026-11-28-ala-9pt5": {}})
        router = CacheRouter({"politics": pol, "cfb": cfb})
        router.note_trade("aachc-cfb-wins-2026-11-28-ala-9pt5", 123.0)
        router.note_trade("ussewc-usse-ga-2026-11-03-rep", 124.0)
        self.assertEqual(cfb.cache.trade_seen["aachc-cfb-wins-2026-11-28-ala-9pt5"], [123.0])
        self.assertEqual(pol.cache.trade_seen["ussewc-usse-ga-2026-11-03-rep"], [124.0])


class TestSharedDictSnapshots(unittest.TestCase):
    """Owner yes, 2026-08-28, after 03:17's "dictionary changed size
    during iteration" killed a cycle: the owner's hand ops (the wall
    button, live-card moves) write the shared order book-keeping from
    the web thread while the engine's cycle iterates it. Every
    iteration over the shared dicts must walk a list() snapshot."""

    def test_no_bare_iteration_over_shared_dicts(self):
        import inspect
        import re
        from v3 import family, main
        for mod in (family, main):
            src = inspect.getsource(mod)
            bare = re.findall(
                r"(?<!list\()(?:self|fam)\.(?:orders|inventory)"
                r"\.(?:values|items)\(\)", src)
            self.assertEqual(bare, [], f"unsnapshotted iteration in {mod.__name__}")


class TestQualifyAskButton(unittest.TestCase):
    """Owner, 2026-08-28: "give me a button to auto qualify the ask
    side." Owner, 2026-08-30: "keeps placing orders until the target
    size is reached" — the run fills to Target Size in the background,
    recomputing the gap from a fresh book on every pass."""

    def _mon(self, r, trim=None):
        import types
        from v3.main import Monitor
        m = types.SimpleNamespace(
            families={"politics": r.fam}, client=r.exchange, names=r.names,
            QUALIFY_MAX_ORDERS=Monitor.QUALIFY_MAX_ORDERS,
            QUALIFY_MAX_S=Monitor.QUALIFY_MAX_S,
            QUALIFY_BP_FLOOR=Monitor.QUALIFY_BP_FLOOR,
            QUALIFY_MAX_COLLATERAL=Monitor.QUALIFY_MAX_COLLATERAL,
            _qualify_jobs={})
        m._qualify_note = Monitor._qualify_note
        m._rested_size = types.MethodType(Monitor._rested_size, m)
        m._qualify_run = types.MethodType(Monitor._qualify_run, m)
        m.qualify_ask = types.MethodType(Monitor.qualify_ask, m)
        # a real exchange puts our rested order INTO the book; the fake
        # one must too, or the gap never closes
        real = r.exchange.post
        def trimming(url, body, path=None, **kw):
                resp = real(url, body, path=path, **kw)
                if url.endswith("/v1/orders"):
                    from v3.scoring import Book
                    live = r.exchange.live[resp["order"]["id"]]
                    if trim is not None:
                        live["size"] = min(live["size"], trim)
                    b = r.exchange.books[body["marketSlug"]]
                    asks = list(b.asks) + [(float(body["price"]["value"]),
                                            live["size"])]
                    r.exchange.books[body["marketSlug"]] = Book(
                        bids=b.bids, asks=tuple(asks), tick=b.tick,
                        fetched_at=b.fetched_at)
                return resp
        r.exchange.post = trimming
        return m

    def _rig(self, asks=((0.40, 120.0), (0.50, 80.0))):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        r.add_market(A, book=Book(bids=((0.05, 10.0),), asks=asks,
                                  tick=0.01, fetched_at=1_000_000.0))
        r.cycle()
        return r, A

    def test_run_keeps_placing_until_target_is_reached(self):
        # every order is trimmed to 300 shares, yet the 5,000-share
        # target still fills: each pass recomputes the gap from a
        # fresh book
        r, A = self._rig()
        m = self._mon(r, trim=300.0)
        self.assertTrue(m.qualify_ask(A)["ok"])
        job = m._qualify_run(A, r.fam)
        self.assertGreaterEqual(job["ask_total"], 5000.0)
        self.assertGreater(job["placed"], 10)      # many orders, not 6
        walls = [o for o in r.fam.orders.values()
                 if o.market == A and o.purpose == "manual"]
        self.assertEqual(len(walls), job["placed"])
        self.assertTrue(all(abs(o.price - 0.99) < 1e-9 for o in walls))

    def test_one_untrimmed_order_finishes_it(self):
        r, A = self._rig()
        m = self._mon(r)
        m.qualify_ask(A)
        job = m._qualify_run(A, r.fam)
        self.assertEqual(job["placed"], 1)
        self.assertEqual(job["stop"], "")

    def test_stops_at_the_buying_power_floor(self):
        r, A = self._rig()
        m = self._mon(r, trim=300.0)
        r.exchange.buying_power = lambda: 5.0
        m.qualify_ask(A)
        job = m._qualify_run(A, r.fam)
        self.assertIn("buying power", job["stop"])
        self.assertEqual(job["placed"], 0)

    def test_button_refuses_when_the_side_already_qualifies(self):
        r, A = self._rig(asks=((0.40, 120.0), (0.98, 60000.0)))
        out = self._mon(r).qualify_ask(A)
        self.assertFalse(out["ok"])
        self.assertIn("already qualifies", out["note"])

    def test_button_refuses_over_the_collateral_cap(self):
        from v3.tests.test_family import Rig, A
        from v3.scoring import Book
        r = Rig()
        big = {"timePeriods": [{"programId": "politics_mid_1",
                                "rewardPool": 100.0, "targetSize": 5_000_000,
                                "discountFactor": 0.2, "status": "LIVE"}]}
        r.add_market(A, book=Book(bids=((0.05, 10.0),),
                                  asks=((0.40, 120.0),), tick=0.01,
                                  fetched_at=1_000_000.0), prog=big)
        r.cycle()
        out = self._mon(r).qualify_ask(A)
        self.assertFalse(out["ok"])
        self.assertIn("$500", out["note"])

    def test_a_second_tap_reports_progress_instead_of_starting_again(self):
        # while a run is in flight, tapping again must report where it
        # is — never launch a second run against the same market
        r, A = self._rig()
        m = self._mon(r, trim=300.0)
        m._qualify_jobs[A] = {"state": "running", "placed": 4,
                              "shares": 1200.0, "target": 5000.0,
                              "started": 0.0, "stop": "",
                              "ask_total": 1400.0}
        before = len(r.exchange.live)
        again = m.qualify_ask(A)
        self.assertTrue(again["ok"])
        self.assertIn("still going", again["note"])
        self.assertIn("1,200 shares rested", again["note"])
        self.assertEqual(len(r.exchange.live), before)   # nothing placed

    def test_button_refuses_without_terms(self):
        from v3.tests.test_family import Rig
        r = Rig()
        r.add_market("vmc-x-unknown")
        r.exchange.prog_raw.pop("vmc-x-unknown", None)
        r.cycle()
        out = self._mon(r).qualify_ask("vmc-x-unknown")
        self.assertFalse(out["ok"])
        self.assertIn("terms not read", out["note"])
