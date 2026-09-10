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
C = "pvwc-usgub-az-mar-2026-11-03-kathob"   # a county winner market: held by its prefix


def cfg():
    return FamilyConfig(
        name="Politics", tag="POL", known_ground=True,
        rest_style="join_quiet", revive=True,
        capital_usd=100.0, per_market_usd=2.0, revive_max_usd=5.0,
        min_days_out=3,
        enter_tokens=("usse", "usgub"), hold_tokens=("usag", "ussos", "pvwc-"))


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
        for tok in ("usag", "usltgov", "ussos", "ussupct", "housepop", "pvwc-"):
            self.assertIn(tok, c.hold_tokens)
        # the county winner markets (owner, 2026-09-10): they carry the
        # governor and senate tokens, and the prefix holds them
        for slug in ("pvwc-usgub-az-mar-2026-11-03-kathob", "pvwc-usse-fl-bro-2026-11-03-ashmoo",
                     "pvwc-usgub-ca-fre-2026-11-03-xavbec", "pvwc-usgub-ny-nas-2026-11-03-brubla"):
            self.assertTrue(any(t in slug for t in c.enter_tokens))
            self.assertTrue(any(t in slug for t in c.hold_tokens))
        # the House popular vote markets: the winner pair and the margin buckets
        for slug in ("vmc-housepop-2026-11-03-dem0-2", "pvwc-housepopw-2026-11-03-dem"):
            self.assertTrue(any(t in slug for t in c.hold_tokens))
            self.assertFalse(any(t in slug for t in c.enter_tokens))
        for tok in ("usmayor", "usterr"):
            self.assertNotIn(tok, c.hold_tokens)
            self.assertNotIn(tok, c.enter_tokens)


class TestCountyGround(unittest.TestCase):
    """Owner, 2026-09-10: "The model is buying in new markets that I
    didn't approve" — the county winner markets carry usgub/usse and
    read as governor and senate ground; their prefix holds them."""

    def test_the_engine_pulls_its_own_orders_but_keeps_the_exits(self):
        from v3.family import FamilyOrder
        from v3.intents import BUY_LONG, BUY_SHORT, SELL_LONG
        r = Rig(cfg=cfg())
        r.add_market(C, book=politics_book(r.now), event="Arizona Governor: Maricopa County")
        r.cycle()
        self.assertTrue(r.fam.enterable(C))        # carries usgub: governor ground
        self.assertTrue(r.fam.held_ground(C))      # but the prefix holds it
        self.assertFalse(r.fam.may_enter(C))
        # orders the engine rested before the hold: a bid, a short, a
        # probe, and the exit of 5 shares it holds
        r.positions[C] = (5.0, 2.5)
        live = r.exchange.live
        for oid, side, px, q, intent, purpose in (
                ("E1", "BUY", 0.42, 5.0, BUY_LONG, "earn"),
                ("E2", "SELL", 0.50, 1.0, BUY_SHORT, "earn"),
                ("P1", "BUY", 0.41, 1.0, BUY_LONG, "probe"),
                ("X1", "SELL", 0.48, 5.0, SELL_LONG, "sell")):
            live[oid] = {"id": oid, "market": C, "side": side, "price": px,
                         "size": q, "intent": intent}
            r.fam.orders[oid] = FamilyOrder(id=oid, market=C, side=side, price=px, qty=q,
                                            intent=intent, placed_ts=r.now, purpose=purpose)
        # the maintenance pass alone: the bid, the short and the probe
        # come off, the exit stays
        n0 = len(r.fam.log)
        r.fam._maintain(r.now + 1, 10)
        left = [o for o in r.fam.orders.values() if o.market == C]
        self.assertEqual([(o.id, o.purpose) for o in left], [("X1", "sell")])
        for oid in ("E1", "E2", "P1"):
            self.assertNotIn(oid, live)
        self.assertIn("X1", live)
        pulls = [e for e in r.fam.log[n0:] if e.get("event") == "pull" and e.get("market") == C]
        self.assertEqual(len(pulls), 3)
        for e in pulls:
            self.assertIn("held ground", e["why"])
        # and whole cycles rest nothing new there; the exit works on
        r.cycle()
        # and nothing new rests there on later cycles
        for _ in range(3):
            r.cycle()
        left = [o for o in r.fam.orders.values() if o.market == C]
        self.assertEqual([o.purpose for o in left], ["sell"])


if __name__ == "__main__":
    unittest.main()
