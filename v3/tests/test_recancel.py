"""An order the open list still shows after we cancelled it is OURS
still — cancelled again, never adopted as the owner's (2026-09-12,
09:44-09:46Z: the family cancelled the focus tender's fifteen leftover
2028 orders at boot, the exchange's list showed eight of them two
minutes later, and they were recorded as his hand's — untouchable —
while they rested on as the entries he had just asked out of)."""

import time
import types
import unittest

from v3.family import FamilyOrder
from v3.intents import BUY_LONG
from v3.tests.test_family import A, Rig


def _row(oid, market, side, price, size, intent):
    return {"id": oid, "market": market, "side": side, "price": price,
            "size": size, "intent": intent}


class TestOurCancelledOrdersAreNeverAdopted(unittest.TestCase):

    def test_the_desk_remembers_what_it_cancelled_for_a_day(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        oid = next(iter(r.fam.orders))
        self.assertIsNone(r.desk.cancelled_at(oid))
        r.desk.cancel(oid, A)
        self.assertIsNotNone(r.desk.cancelled_at(oid))
        r.now += 25 * 3600.0
        self.assertIsNone(r.desk.cancelled_at(oid))

    def test_a_listed_order_we_cancelled_is_cancelled_again_not_adopted(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        oid = next(iter(r.fam.orders))
        rec = r.fam.orders[oid]
        r.desk.cancel(oid, A)
        r.fam.orders.pop(oid)
        # the exchange's list shows it again — lag, or a cancel it dropped
        r.exchange.live[oid] = _row(oid, A, rec.side, rec.price, rec.qty, rec.intent)
        r.cycle()
        self.assertNotIn(oid, r.fam.orders)                 # never his
        self.assertNotIn(oid, r.exchange.live)              # cancelled again
        self.assertTrue(any(e.get("event") == "cancel_again" and e.get("id") == oid
                            for e in r.fam.log))

    def test_a_record_adopted_after_our_cancel_goes(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        oid = "ghost7"
        r.desk.remember_cancel(oid, r.now - 120.0)
        r.fam.orders[oid] = FamilyOrder(
            id=oid, market=A, side="BUY", price=0.05, qty=333.0, intent=BUY_LONG,
            placed_ts=r.now, purpose="manual",
            why="the owner's own order — the engine leaves it alone")
        r.exchange.live[oid] = _row(oid, A, "BUY", 0.05, 333.0, BUY_LONG)
        r.cycle()
        self.assertNotIn(oid, r.fam.orders)
        self.assertNotIn(oid, r.exchange.live)

    def test_his_own_order_recorded_before_any_cancel_of_it_stays(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        oid = "hand1"
        r.fam.orders[oid] = FamilyOrder(
            id=oid, market=A, side="BUY", price=0.05, qty=10.0, intent=BUY_LONG,
            placed_ts=r.now - 600.0, purpose="manual",
            why="the owner's own order — the engine leaves it alone")
        r.exchange.live[oid] = _row(oid, A, "BUY", 0.05, 10.0, BUY_LONG)
        r.cycle()
        self.assertIn(oid, r.fam.orders)
        self.assertIn(oid, r.exchange.live)

    def test_the_cancel_is_resent_at_most_once_a_minute_and_ten_times(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        oid = "stuck3"
        r.desk.remember_cancel(oid, r.now)
        sent = []
        orig = r.exchange.post

        def post(url, body, path=None, **kw):
            if "/cancel" in url and oid in url:
                sent.append(r.now)
                return {}                       # the exchange keeps it listed
            return orig(url, body, path=path, **kw)
        r.exchange.post = post
        r.exchange.live[oid] = _row(oid, A, "BUY", 0.05, 10.0, BUY_LONG)
        for _ in range(30):
            r.cycle(advance=30.0)               # every 30 s: a resend at most each minute
        self.assertLessEqual(len(sent), 10)
        self.assertGreaterEqual(len(sent), 8)
        self.assertNotIn(oid, r.fam.orders)     # and never adopted meanwhile


class TestTheMemorySurvivesARestart(unittest.TestCase):

    def test_saved_cancels_and_the_familys_own_exits_seed_every_desk(self):
        from v3.main import Monitor
        r = Rig()
        r.add_market(A)
        r.cycle()
        ns = types.SimpleNamespace(families={"politics": r.fam})
        r.fam.log.append({"ts": r.now - 60.0, "event": "exit", "id": "old9",
                          "market": A, "why": "outside the families you chose"})
        Monitor._restore_desk_cancelled(ns, {"desk_cancelled": {"saved1": r.now - 100.0}})
        self.assertIsNotNone(r.desk.cancelled_at("old9"))
        self.assertIsNotNone(r.desk.cancelled_at("saved1"))

    def test_what_is_saved_is_the_last_day_of_cancels(self):
        from v3.main import Monitor
        r = Rig()
        ns = types.SimpleNamespace(families={"politics": r.fam})
        r.desk.remember_cancel("fresh1", time.time() - 10.0)
        r.desk.remember_cancel("stale1", time.time() - 2 * 24 * 3600.0)
        saved = Monitor._desk_cancelled(ns)
        self.assertIn("fresh1", saved)
        self.assertNotIn("stale1", saved)


if __name__ == "__main__":
    unittest.main()
