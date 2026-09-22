"""When a market leaves the tender's board, the tender's own entries
there come off and its exits stay for the engine (owner, 2026-09-22
"C: Pull entries when a market leaves the board").

2026-09-21, 21:41Z: the exchange cut the Senate Combo pools from $300
to $25 a day, the twelve books left the board, and four tender entries
holding $140 of collateral rested on for six hours earning $2.70 a day
between them — the tender no longer planned that ground and the engine
left them alone."""

import unittest

from v3 import politics
from v3.bonds import Bonds
from v3.family import FamilyOrder
from v3.focus import PURPOSE, Focus
from v3.intents import BUY_LONG, SELL_LONG
from v3.scoring import Book
from v3.tests.test_family import LIVE_PROG, Rig
from v3.tests.test_focus import NC, T1, wide_book


class TestLeavingTheBoard(unittest.TestCase):
    def setUp(self):
        self.r = Rig(cfg=politics.config())
        self.b = Bonds(self.r.fam, self.r.exchange, lambda s: None,
                       clock=lambda: self.r.now, sleep=lambda s: None)
        self.f = Focus(self.r.fam, self.r.exchange, self.b,
                       fair=lambda s: None, alert=lambda t, m: None,
                       clock=lambda: self.r.now, switch_on=lambda: True,
                       buying_power=lambda: 2000.0)
        self.r.add_market(NC, wide_book(self.r.now), event="nc", prog=T1)
        self.r.switch = False
        self.r.cycle()
        self.f.seed(self.r.now)
        self.r.switch = True
        self.f.set_fair(NC, 45.0)

    def tick(self, advance=15.0):
        self.r.now += advance
        for s in list(self.r.exchange.books):
            b = self.r.exchange.books[s]
            self.r.cache.put(s, Book(bids=b.bids, asks=b.asks, tick=b.tick, fetched_at=self.r.now))
        return self.f.cycle(self.r.now, dict(self.r.positions), True)

    def _rest(self, oid, side, px, qty, intent):
        self.r.exchange.live[oid] = {"id": oid, "market": NC, "side": side,
                                     "price": px, "size": qty, "intent": intent}
        self.r.fam.orders[oid] = FamilyOrder(
            id=oid, market=NC, side=side, price=px, qty=qty, intent=intent,
            placed_ts=self.r.now, purpose=PURPOSE)
        self.f.mine_ids.append(oid)

    def _leave(self):
        # the exchange cuts the pool: a $100-a-day program, under the bar
        self.r.add_market(NC, wide_book(self.r.now), event="nc", prog=LIVE_PROG)
        self.f.terms.current[NC] = self.r.fam.terms.get(NC)

    def test_the_entry_comes_off_and_the_exit_stays(self):
        self.tick()
        self.assertIn(NC, self.f.markets)
        for oid in list(self.r.fam.orders):
            self.r.fam.orders.pop(oid)
        self.r.exchange.live.clear()
        # a 50-share long: the ask of 50 is its exit, the bid an entry
        self.r.fam.inventory[NC] = {"qty": 50.0, "cost": 22.0}
        self.r.positions[NC] = (50.0, 22.0)
        self._rest("ENTRY", "BUY", 0.44, 100.0, BUY_LONG)
        self._rest("EXIT", "SELL", 0.47, 50.0, SELL_LONG)
        self._leave()
        self.tick()
        self.assertNotIn(NC, self.f.markets)
        self.assertNotIn("ENTRY", self.r.fam.orders)
        self.assertNotIn("ENTRY", self.r.exchange.live)
        self.assertIn("EXIT", self.r.fam.orders)
        self.assertIn("EXIT", self.r.exchange.live)
        pulls = [e for e in self.f.log if e.get("event") == "pull" and e.get("market") == NC]
        self.assertEqual(len(pulls), 1, pulls)
        self.assertIn("left the board", pulls[0]["why"])
        self.assertIn("ENTRY", self.f._gone_by_me)

    def test_his_own_order_there_is_not_touched(self):
        self.tick()
        for oid in list(self.r.fam.orders):
            self.r.fam.orders.pop(oid)
        self.r.exchange.live.clear()
        self.r.exchange.live["HIS"] = {"id": "HIS", "market": NC, "side": "BUY",
                                       "price": 0.40, "size": 30.0, "intent": BUY_LONG}
        self.r.fam.orders["HIS"] = FamilyOrder(
            id="HIS", market=NC, side="BUY", price=0.40, qty=30.0, intent=BUY_LONG,
            placed_ts=self.r.now, purpose="manual")
        self._rest("ENTRY", "SELL", 0.47, 40.0, SELL_LONG)     # a short-opening ask, flat
        self._leave()
        self.tick()
        self.assertIn("HIS", self.r.fam.orders)
        self.assertIn("HIS", self.r.exchange.live)
        self.assertNotIn("ENTRY", self.r.fam.orders)


if __name__ == "__main__":
    unittest.main()
