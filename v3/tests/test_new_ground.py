"""Owner, 2026-09-09: "I think this is a newly added market, but I didn't
get any sort of notification. Can you give me a report on any newly
added markets?" ... "Make the changes to the market discovery. Yes
state races should be included" ... "But don't place orders in these
races before I get the chance to look at them"."""
import copy
import unittest

from v3.family import FamilyConfig
from v3.tests.test_family import Rig, politics_book

E = "ussewc-usse-ga-2026-11-03-rep"      # ordinary ground: orders may rest
H = "ewc-usag-az-2026-11-03-dem"         # held ground: looked at, not entered
H2 = "ewc-ussos-mi-2026-11-03-rep"


def cfg():
    return FamilyConfig(
        name="Politics", tag="POL", known_ground=True,
        rest_style="join_quiet", revive=True,
        capital_usd=100.0, per_market_usd=2.0, revive_max_usd=5.0,
        min_days_out=3,
        enter_tokens=("usse",), hold_tokens=("usag", "ussos"))


class TestNewMarketsAreReported(unittest.TestCase):
    def test_discovery_records_the_market_and_sends_the_alert(self):
        r = Rig(cfg=cfg())
        r.add_market(E, event="Georgia Senate")
        r.add_market(H, event="Arizona Attorney General")
        r.cycle()
        self.assertIn(E, r.fam.new_markets)
        self.assertIn(H, r.fam.new_markets)
        self.assertTrue(r.fam.new_markets[H]["name"].startswith("Q for"))
        titles = [t for t, m in r.alerts if "new market" in t]
        self.assertEqual(len(titles), 1, r.alerts)
        body = [m for t, m in r.alerts if "new market" in t][0]
        self.assertIn("Q for", body)
        self.assertIn("1 on held ground", body)
        found = [e for e in r.fam.log if e.get("event") == "discovered"]
        self.assertEqual(found[-1]["new"], 2)
        self.assertEqual(found[-1]["held"], 1)
        self.assertIn(H, found[-1]["markets"])

    def test_the_summary_carries_the_week_s_new_markets(self):
        r = Rig(cfg=cfg())
        r.add_market(E, event="Georgia Senate")
        r.add_market(H, event="Arizona Attorney General")
        s = r.cycle()
        rows = {row["market"]: row for row in s["new_markets"]}
        self.assertEqual(set(rows), {E, H})
        self.assertTrue(rows[H]["held"])
        self.assertFalse(rows[E]["held"])
        self.assertFalse(rows[H]["opened"])
        for row in rows.values():
            self.assertGreater(row["since"], 0.0)
            self.assertIn("name", row)

    def test_a_week_old_entry_falls_off(self):
        r = Rig(cfg=cfg())
        r.add_market(E, event="Georgia Senate")
        r.cycle()
        self.assertIn(E, r.fam.new_markets)
        r.fam.last_discover = 0.0                        # discovery due again
        r.cycle(advance=8 * 86400.0)
        self.assertNotIn(E, r.fam.new_markets)
        self.assertEqual(len([t for t, m in r.alerts if "new market" in t]), 1)  # no second alert

    def test_no_alert_when_nothing_is_new(self):
        r = Rig(cfg=cfg())
        r.add_market(E, event="Georgia Senate")
        r.cycle()
        r.fam.last_discover = 0.0
        r.cycle()
        self.assertEqual(len([t for t, m in r.alerts if "new market" in t]), 1)


class TestHeldGround(unittest.TestCase):
    def rig(self):
        r = Rig(cfg=cfg())
        r.add_market(E, book=politics_book(r.now), event="Georgia Senate")
        r.add_market(H, book=politics_book(r.now), event="Arizona Attorney General")
        return r

    def orders_on(self, r, slug):
        return [o for o in r.fam.orders.values() if o.market == slug]

    def test_held_ground_is_scanned_and_scored_but_never_entered(self):
        r = self.rig()
        for _ in range(4):
            r.cycle()
        self.assertTrue(r.fam.enterable(H))
        self.assertTrue(r.fam.held_ground(H))
        self.assertFalse(r.fam.may_enter(H))
        self.assertIn(H, r.fam.scoreboard)                  # looked at
        self.assertEqual(self.orders_on(r, H), [])         # nothing rests
        self.assertTrue(self.orders_on(r, E))              # the ordinary ground got its orders
        self.assertTrue(r.fam.may_enter(E))

    def test_his_tap_opens_it_and_orders_may_rest(self):
        r = self.rig()
        for _ in range(2):
            r.cycle()
        self.assertEqual(self.orders_on(r, H), [])
        res = r.fam.open_market(H, r.now)
        self.assertTrue(res["ok"], res)
        self.assertFalse(r.fam.held_ground(H))
        self.assertTrue(r.fam.may_enter(H))
        self.assertTrue(any(e.get("event") == "ground_opened" and e.get("market") == H
                            for e in r.fam.log))
        for _ in range(4):
            r.cycle()
        self.assertTrue(self.orders_on(r, H))
        # again is harmless
        self.assertTrue(r.fam.open_market(H, r.now)["ok"])

    def test_only_held_ground_can_be_opened(self):
        r = self.rig()
        r.cycle()
        self.assertFalse(r.fam.open_market(E, r.now)["ok"])
        self.assertFalse(r.fam.open_market("nope-2026-11-03-x", r.now)["ok"])

    def test_the_opening_survives_a_restart(self):
        r = self.rig()
        r.cycle()
        r.fam.open_market(H, r.now)
        d = copy.deepcopy(r.fam.to_dict())
        r2 = Rig(cfg=cfg())
        r2.fam.restore(d)
        self.assertIn(H, r2.fam.opened)
        self.assertIn(H, r2.fam.new_markets)
        self.assertFalse(r2.fam.held_ground(H))

    def test_the_politics_config_holds_the_state_races(self):
        from v3 import politics
        c = politics.config()
        for tok in ("usag", "usltgov", "ussos", "ussupct", "housepop"):
            self.assertIn(tok, c.hold_tokens)
        # the House popular vote markets: the winner pair and the margin buckets
        for slug in ("vmc-housepop-2026-11-03-dem0-2", "pvwc-housepopw-2026-11-03-dem"):
            self.assertTrue(any(t in slug for t in c.hold_tokens))
            self.assertFalse(any(t in slug for t in c.enter_tokens))
        for tok in ("usmayor", "usterr"):
            self.assertNotIn(tok, c.hold_tokens)
            self.assertNotIn(tok, c.enter_tokens)


if __name__ == "__main__":
    unittest.main()
