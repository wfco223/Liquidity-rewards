"""Exploration mode (owner, 2026-09-08, for the NFL: "intentionally
small sized risks and intentionally aggressive to see what the markets
are like. Then as we get information relax the aggression to try and
find the optimal amount")."""
import unittest

from v3.family import FamilyConfig
from v3.scoring import Book
from v3.tests.test_family import Rig, A


def explore_cfg(**kw) -> FamilyConfig:
    base = dict(name="NFL futures", tag="NFL", known_ground=False,
                rest_style="behind", allow_improve=True,
                capital_usd=50.0, per_market_usd=1.0, min_days_out=3,
                explore=True, explore_usd=1.0, explore_hours=48.0,
                explore_bonus=0.30)
    base.update(kw)
    return FamilyConfig(**base)


def thin_book(now, bid=0.44, ask=0.47):
    # a thin touch, the qualifying wall far behind — the futures shape
    return Book(bids=((bid, 20.0), (0.02, 60000.0)),
                asks=((ask, 20.0), (0.98, 60000.0)),
                tick=0.01, fetched_at=now)


class TestTheBonusFadesWithOurOwnHours(unittest.TestCase):
    def test_closest_depth_carries_the_full_bonus_and_deep_none(self):
        r = Rig(cfg=explore_cfg())
        self.assertAlmostEqual(r.fam._explore_bonus(A, "BUY", 0), 0.30)
        self.assertAlmostEqual(r.fam._explore_bonus(A, "BUY", 0, in_front=True), 0.30)
        self.assertAlmostEqual(r.fam._explore_bonus(A, "BUY", 1), 0.15)
        self.assertAlmostEqual(r.fam._explore_bonus(A, "BUY", 2), 0.075)
        self.assertEqual(r.fam._explore_bonus(A, "BUY", 3), 0.0)
        self.assertEqual(r.fam._explore_bonus(A, "BUY", 7), 0.0)

    def test_forty_eight_hours_at_a_depth_fades_its_bonus_to_nothing(self):
        r = Rig(cfg=explore_cfg())
        r.fam.fillmodel.observe_rest(A, "BUY", 0, 600.0, 24 * 3600.0)
        self.assertAlmostEqual(r.fam._explore_bonus(A, "BUY", 0), 0.15)
        r.fam.fillmodel.observe_rest(A, "BUY", 0, 600.0, 24 * 3600.0)
        self.assertEqual(r.fam._explore_bonus(A, "BUY", 0), 0.0)
        # the other side and the other depths are still unlearned
        self.assertAlmostEqual(r.fam._explore_bonus(A, "SELL", 0), 0.30)
        self.assertAlmostEqual(r.fam._explore_bonus(A, "BUY", 1), 0.15)

    def test_a_family_not_exploring_has_no_bonus(self):
        r = Rig()
        self.assertEqual(r.fam._explore_bonus(A, "BUY", 0), 0.0)


class TestExploringOrders(unittest.TestCase):
    def _placed(self, r):
        return [o for o in r.exchange.live.values() if o["market"] == A]

    def test_exploring_rests_small_and_at_the_front(self):
        r = Rig(cfg=explore_cfg())
        r.add_market(A, book=thin_book(r.now))
        r.cycle()
        live = self._placed(r)
        self.assertTrue(live, r.fam.scoreboard.get(A))
        for o in live:
            cost = o["size"] * (o["price"] if o["side"] == "BUY" else 1 - o["price"])
            self.assertLessEqual(cost, 1.0 + 1e-9, o)      # small risks only
        # the bids sit at the touch or in front of it, never behind
        bids = [o for o in live if o["side"] == "BUY"]
        self.assertTrue(bids)
        self.assertTrue(all(o["price"] >= 0.44 - 1e-9 for o in bids), bids)
        plans = (r.fam.scoreboard.get(A) or {}).get("plans") or []
        self.assertTrue(any(p.get("bonus", 0) > 0 for p in plans), plans)
        self.assertTrue(any("exploring" in (p.get("why") or "") for p in plans), plans)

    def test_the_learned_depth_is_planned_on_ev_alone(self):
        r = Rig(cfg=explore_cfg())
        r.add_market(A, book=thin_book(r.now))
        # every depth on both sides has its 48 hours: nothing left to learn
        for side in ("BUY", "SELL"):
            for b in (0, 1, 2):
                r.fam.fillmodel.observe_rest(A, side, b, 600.0, 48 * 3600.0)
        r.cycle()
        plans = (r.fam.scoreboard.get(A) or {}).get("plans") or []
        self.assertTrue(all(p.get("bonus", 0) == 0 for p in plans), plans)
        self.assertFalse(any("exploring" in (p.get("why") or "") for p in plans), plans)

    def test_the_summary_reports_what_was_learned(self):
        r = Rig(cfg=explore_cfg())
        r.add_market(A, book=thin_book(r.now))
        r.fam.fillmodel.observe_rest(A, "BUY", 0, 600.0, 3 * 3600.0)
        r.fam.fillmodel.observe_own_fill(A, "BUY", 0, 600.0)
        s = r.cycle()
        ex = s.get("explore")
        self.assertIsNotNone(ex)
        self.assertEqual(ex["usd"], 1.0)
        rows = {(x["side"], x["depth"]): x for x in ex["rows"]}
        row = rows[("BUY", 0)]
        self.assertGreaterEqual(row["hours"], 3.0)
        self.assertEqual(row["fills"], 1)
        self.assertFalse(row["learned"])
        self.assertGreater(row["bonus"], 0.0)

    def test_a_family_not_exploring_reports_nothing(self):
        r = Rig()
        r.add_market(A)
        s = r.cycle()
        self.assertNotIn("explore", s)


class TestTheBonusCountsAtTheGate(unittest.TestCase):
    """_plan_side picks by EV plus bonus; the placer must judge by the
    same number, or a depth chosen for what it teaches is refused at
    the door (the 2026-09-09 gap)."""

    def _plan(self, ev, score):
        return {"side": "BUY", "px": 0.44, "qty": 1.0, "share": 0.05,
                "est": 0.05, "ev": ev, "bonus": round(score - ev, 4),
                "score": score, "p_fill": 0.3, "fill_cost": 0.02,
                "cost": 0.44, "why": "exploring — joins the touch"}

    def test_exploring_places_a_plan_the_bonus_lifts_over_the_bar(self):
        r = Rig(cfg=explore_cfg())
        r.add_market(A, book=thin_book(r.now))
        r.cycle()
        for o in list(r.exchange.live.values()):
            r.exchange.cancel(o["id"]) if hasattr(r.exchange, "cancel") else None
        r.fam.orders.clear(); r.fam.last_action.clear()
        r.fam.scoreboard[A] = {"ts": r.now, "plans": [self._plan(ev=-0.05, score=0.25)],
                               "why": "", "est": 0.05, "conf": 0.0}
        r.cache.put(A, thin_book(r.now))
        left = r.fam._enter(r.now, {}, 6)
        self.assertEqual(left, 5)
        self.assertTrue(any(o.market == A and o.purpose == "earn"
                            for o in r.fam.orders.values()))

    def test_not_exploring_still_judges_by_ev(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        r.fam.orders.clear(); r.fam.last_action.clear()
        r.fam.scoreboard[A] = {"ts": r.now, "plans": [self._plan(ev=-0.05, score=0.25)],
                               "why": "", "est": 0.05, "conf": 0.0}
        left = r.fam._enter(r.now, {}, 6)
        self.assertEqual(left, 6)
        self.assertFalse(any(o.market == A for o in r.fam.orders.values()))


class TestTheNflIsTheExplorer(unittest.TestCase):
    def test_nfl_rereads_idle_markets_every_fifteen_minutes(self):
        from v3 import football
        c = football.nfl()
        self.assertEqual(c.rescan_s, 900.0)
        self.assertEqual(c.books_per_cycle, 40)
        self.assertEqual(c.scan_reserve, 16)

    def test_nfl_config_explores_with_a_dollar_per_order(self):
        from v3 import football
        c = football.nfl()
        self.assertTrue(c.explore)
        self.assertEqual(c.explore_usd, 1.0)
        self.assertEqual(c.explore_hours, 48.0)
        # owner, 2026-09-10: NFL's share of the families' $250 is $40
        self.assertEqual(c.capital_usd, 40.0)
        self.assertFalse(football.cfb().explore)

    def test_the_status_card_shows_the_exploration(self):
        from v3 import web
        self.assertIn("exploreLine(s2.explore)", web.STATUS_JS)
        self.assertIn("exploring", web.STATUS_JS)


if __name__ == "__main__":
    unittest.main()
