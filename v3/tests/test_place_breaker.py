"""The placement breaker (owner, 2026-09-05, "Yes"). When the exchange
refuses our placements as "a VPN" while still taking cancels, the desk
remembers it, the engine cancels nothing it means to replace, one
placement a minute probes for recovery, and the owner hears once each
way."""

import unittest

from v3.api import ApiError
from v3.family import FamilyConfig, FamilyOrder
from v3.intents import SELL_LONG, SELL_SHORT
from v3.orders import PLACE_PROBE_S, PLACE_RETRY_N, PlaceHealth, is_vpn_refusal
from v3.scoring import Book
from v3.tests.test_family import A, FakeClient, Rig, politics_book

VPN = ('https://api.polymarket.us/v1/orders -> HTTP 403: {"code":7,'
       '"message":"Your connection looks like a VPN"}')


class VpnClient(FakeClient):
    """The exchange refusing placements as a VPN while cancels go through."""

    def __init__(self):
        super().__init__()
        self.vpn = True
        self.order_posts = 0
        self.fresh = 0

    def fresh_connection(self):
        self.fresh += 1

    def post(self, url, body, path=None, **kw):
        if url.endswith("/v1/orders"):
            self.order_posts += 1
            if self.vpn:
                raise ApiError(VPN, status=403)
        return super().post(url, body, path=path, **kw)


def _vpn_rig(cfg=None, vpn=True):
    """The family rig, with the exchange refusing placements as a VPN."""
    r = Rig(cfg=cfg) if cfg is not None else Rig()
    c = VpnClient()
    c.vpn = vpn
    r.exchange = c
    r.desk.client = c
    return r


def _rig(vpn=True):
    r = _vpn_rig(vpn=vpn)
    r.add_market(A)
    r.fam.universe[A] = {"event_n": 1, "name": A}     # on the desk's whitelist
    r.cache.put(A, politics_book(r.now))
    return r


class TestTheDesk(unittest.TestCase):
    def test_a_vpn_refusal_is_recognised(self):
        self.assertTrue(is_vpn_refusal(ApiError(VPN, status=403)))
        self.assertFalse(is_vpn_refusal(ApiError("HTTP 400: bad price", status=400)))
        self.assertFalse(is_vpn_refusal(ApiError("HTTP 403: forbidden", status=403)))

    def test_refusal_retries_then_blocks_then_probes_then_clears(self):
        r = _rig()
        c = r.exchange
        changes = []
        r.desk.health.on_change = lambda b, n: changes.append((b, n))
        res = r.desk.place_resting(A, "BUY", 0.40, 1.0)
        self.assertFalse(res.ok)
        self.assertIn("placement failed", res.note)
        self.assertEqual(c.order_posts, 1 + PLACE_RETRY_N)   # retried on fresh connections
        self.assertEqual(c.fresh, PLACE_RETRY_N)
        self.assertTrue(r.desk.health.blocked())
        self.assertEqual(changes, [(True, r.desk.health.note)])
        # the next attempts inside the minute are refused without a call
        res2 = r.desk.place_resting(A, "BUY", 0.40, 1.0)
        self.assertFalse(res2.ok)
        self.assertIn("placements blocked", res2.note)
        self.assertEqual(c.order_posts, 1 + PLACE_RETRY_N)
        # a minute on, one probe goes out (and fails again)
        r.now += PLACE_PROBE_S + 1
        r.cache.put(A, politics_book(r.now))
        r.desk.place_resting(A, "BUY", 0.40, 1.0)
        self.assertEqual(c.order_posts, 2 * (1 + PLACE_RETRY_N))
        self.assertTrue(r.desk.health.blocked())
        # the exchange relents: the probe lands and the breaker clears
        c.vpn = False
        r.now += PLACE_PROBE_S + 1
        r.cache.put(A, politics_book(r.now))
        res3 = r.desk.place_resting(A, "BUY", 0.40, 1.0, verify=False)
        self.assertTrue(res3.ok, res3.note)
        self.assertFalse(r.desk.health.blocked())
        self.assertEqual(changes[-1][0], False)
        self.assertIn("accepted again", changes[-1][1])
        v = r.desk.health.view()
        self.assertFalse(v["blocked"])
        self.assertEqual(v["refused"], 0)

    def test_the_owner_s_own_tap_always_tries(self):
        r = _rig()
        c = r.exchange
        r.desk.place_resting(A, "BUY", 0.40, 1.0)
        n = c.order_posts
        res = r.desk.place_resting(A, "BUY", 0.40, 1.0, initiator="owner")
        self.assertFalse(res.ok)
        self.assertIn("placement failed", res.note)     # tried, not short-circuited
        self.assertGreater(c.order_posts, n)

    def test_other_errors_neither_retry_nor_trip(self):
        r = _rig(vpn=False)
        c = r.exchange
        orig = c.post

        def bad(url, body, path=None, **kw):
            if url.endswith("/v1/orders"):
                c.order_posts += 1
                raise ApiError("HTTP 400: price off grid", status=400)
            return orig(url, body, path=path, **kw)
        c.post = bad
        res = r.desk.place_resting(A, "BUY", 0.40, 1.0)
        self.assertFalse(res.ok)
        self.assertEqual(c.order_posts, 1)
        self.assertFalse(r.desk.health.blocked())

    def test_cancels_are_never_gated(self):
        r = _rig()
        r.desk.health.refused(VPN)
        r.exchange.live["X"] = {"id": "X", "market": A, "side": "BUY",
                                "price": 0.40, "size": 1.0, "intent": "ORDER_INTENT_BUY_LONG"}
        self.assertTrue(r.desk.cancel("X", A).ok)
        self.assertNotIn("X", r.exchange.live)


class TestTheEngineHoldsStill(unittest.TestCase):
    def _blocked(self, r):
        r.desk.health.refused(VPN, r.now)

    def test_a_misplaced_exit_is_not_moved_while_blocked(self):
        r = _vpn_rig()
        book = Book(bids=((0.92, 30.0), (0.02, 60000.0)),
                    asks=((0.97, 217.0), (0.99, 60000.0)),
                    tick=0.01, fetched_at=r.now)
        r.add_market(A, book=book)
        r.fam.inventory[A] = {"qty": 10.0, "cost": 9.2}
        r.positions[A] = (10.0, 9.2)
        rec = FamilyOrder(id="OLD", market=A, side="SELL", price=0.99,
                          qty=10.0, intent=SELL_LONG, placed_ts=0.0,
                          purpose="sell", live_est=0.01)
        r.fam.orders["OLD"] = rec
        r.exchange.live["OLD"] = {"id": "OLD", "market": A, "side": "SELL",
                                  "price": 0.99, "size": 10.0}
        self._blocked(r)
        for _ in range(3):
            r.cycle(advance=700.0)          # probes go out and fail; the breaker holds
        self.assertTrue(r.desk.health.blocked())
        self.assertIn("OLD", r.fam.orders)              # parked, but still there
        self.assertFalse(any(w.get("kind") == "exit move" for w in r.fam.wind_down))
        # the exchange relents: a probe lands, the breaker clears, and
        # the move happens as it always did
        r.exchange.vpn = False
        for _ in range(4):
            r.fam.last_action.clear()       # the refused tries had marked the cooldown
            r.cycle(advance=700.0)
        self.assertFalse(r.desk.health.blocked())
        self.assertNotIn("OLD", r.fam.orders)           # it acted again
        sells = [o for o in r.fam.orders.values() if o.side == "SELL"]
        self.assertTrue(sells)
        self.assertTrue(all(o.price <= 0.97 + 1e-9 for o in sells),
                        [o.price for o in sells])

    def test_a_stranded_cover_is_not_re_priced_while_blocked(self):
        r = _vpn_rig()
        r.add_market(A, book=Book(bids=((0.20, 100.0), (0.02, 60000.0)),
                                  asks=((0.24, 100.0), (0.98, 60000.0)),
                                  tick=0.01, fetched_at=r.now))
        r.cycle()                       # its first placements are refused: blocked
        self.assertTrue(r.desk.health.blocked())
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        r.fam.inventory[A] = {"qty": -60.0, "cost": -55.08}
        r.positions[A] = (-60.0, -55.08)
        r.exchange.live["OLD"] = {"id": "OLD", "market": A, "side": "BUY",
                                  "price": 0.06, "size": 60.0, "intent": SELL_SHORT}
        r.fam.orders["OLD"] = FamilyOrder(
            id="OLD", market=A, side="BUY", price=0.06, qty=60.0,
            intent=SELL_SHORT, placed_ts=r.now - 7200.0, purpose="sell",
            live_est=0.0)
        for _ in range(3):
            r.fam.last_action.clear()
            r.cycle()
        self.assertTrue(r.desk.health.blocked())
        self.assertIn("OLD", r.fam.orders)
        self.assertEqual([o.price for o in r.fam.orders.values()
                          if o.market == A and o.side == "BUY"], [0.06])
        # the exchange relents: the stranded cover is re-priced as before
        r.exchange.vpn = False
        for _ in range(4):
            r.fam.last_action.clear()
            r.cycle(advance=120.0)
        covers = [o for o in r.fam.orders.values()
                  if o.market == A and o.side == "BUY" and o.purpose == "sell"]
        self.assertTrue(covers)
        self.assertTrue(all(o.price >= 0.18 for o in covers), [o.price for o in covers])

    def test_risk_reducing_pulls_still_run_while_blocked(self):
        # a family outside its game window pulls its orders whether or
        # not the exchange would let it re-place them
        cfg = FamilyConfig(name="P", tag="P", known_ground=True,
                           rest_style="join_quiet", capital_usd=100.0,
                           per_market_usd=2.0, min_days_out=3,
                           rest_from=(0, 0), rest_until=(0, 1))     # Monday 00-01 ET only
        r = Rig(cfg=cfg)
        r.add_market(A)
        r.exchange.live["E"] = {"id": "E", "market": A, "side": "BUY",
                                "price": 0.40, "size": 5.0,
                                "intent": "ORDER_INTENT_BUY_LONG"}
        r.fam.orders["E"] = FamilyOrder(id="E", market=A, side="BUY", price=0.40,
                                        qty=5.0, intent="ORDER_INTENT_BUY_LONG",
                                        placed_ts=r.now, purpose="earn")
        self._blocked(r)
        s = r.cycle()
        self.assertEqual(s.get("mode"), "game window")
        self.assertNotIn("E", r.fam.orders)
        self.assertTrue(any(e.get("event") == "window_pull" for e in r.fam.log))

    def test_the_bond_rail_reads_the_same_breaker(self):
        from v3.bonds import Bonds
        r = Rig()
        b = Bonds(r.fam, r.exchange, lambda s: None, clock=lambda: r.now,
                  alert=lambda t, m: None, sleep=lambda s: None)
        self.assertFalse(b._placing_blocked())
        self._blocked(r)
        self.assertTrue(b._placing_blocked())

    def test_the_health_view_and_a_shared_object(self):
        h = PlaceHealth(clock=lambda: 1000.0)
        self.assertFalse(h.blocked())
        h.refused("x")
        self.assertTrue(h.blocked())
        self.assertFalse(h.probe_due(1030.0))
        self.assertTrue(h.probe_due(1000.0 + PLACE_PROBE_S))
        v = h.view()
        self.assertEqual((v["blocked"], v["refused"], v["since"]), (True, 1, 1000.0))


if __name__ == "__main__":
    unittest.main()
