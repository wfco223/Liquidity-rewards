"""Exits must earn while they wait, and a cancel-then-re-rest lands
where it said it would (owner, 2026-09-04, "Both").

The evening of 2026-09-04: 33 of 53 politics exits earned nothing —
buy-backs 14 ticks under the bid on a 92c sale, lone asks anchored at
fair + 15 ticks — while one buy-back was cancelled and re-placed at 42c
every minute for three hours and ten sells hourly at the same price."""

import unittest

from v3.family import EXIT_PAYS_MIN_USD, FamilyOrder
from v3.intents import SELL_LONG, SELL_SHORT
from v3.scoring import Book
from v3.tests.test_family import A, Rig


def _wide(now, bid=0.20, ask=0.24, bid_q=100.0, ask_q=100.0):
    """The politics shape: a thin touch, the qualifying wall far behind."""
    return Book(bids=((bid, bid_q), (0.02, 60000.0)),
                asks=((ask, ask_q), (0.98, 60000.0)),
                tick=0.01, fetched_at=now)


def _covers(r, market):
    return [o for o in r.fam.orders.values()
            if o.market == market and o.side == "BUY" and o.purpose == "sell"]


def _sells(r, market):
    return [o for o in r.fam.orders.values()
            if o.market == market and o.side == "SELL" and o.purpose == "sell"]


class TestPayingSlotsFirst(unittest.TestCase):
    def test_a_deep_buy_back_yields_to_a_slot_that_pays(self):
        r = Rig()
        r.add_market(A, book=_wide(r.now))
        r.cycle()
        fam = r.fam
        # sold 60 at 92c; the model's deep slot (6c) promises a huge
        # paper profit and earns nothing; the touch pays
        px = fam._best_exit_px(A, "BUY", _wide(r.now), 0.06, 0.23, 60.0,
                               basis=0.91)
        self.assertGreaterEqual(px, 0.20)
        est = fam._slot_est(A, "BUY", _wide(r.now), px, 60.0)
        self.assertGreaterEqual(est, EXIT_PAYS_MIN_USD)
        self.assertLess(fam._slot_est(A, "BUY", _wide(r.now), 0.06, 60.0),
                        EXIT_PAYS_MIN_USD)

    def test_when_nothing_pays_the_score_still_decides(self):
        r = Rig()
        r.add_market(A, book=_wide(r.now))
        r.cycle()
        thin = Book(bids=((0.20, 10.0),), asks=((0.24, 10.0),),
                    tick=0.01, fetched_at=r.now)        # side below Target Size
        fam = r.fam
        self.assertEqual(fam._slot_est(A, "BUY", thin, 0.20, 60.0), 0.0)
        self.assertIsNone(fam._paying_exit_px(A, "BUY", thin, 0.06, 0.23, 60.0,
                                              basis=0.91))
        px = fam._best_exit_px(A, "BUY", thin, 0.06, 0.23, 60.0, basis=0.91)
        self.assertTrue(0.06 <= px <= 0.23)

    def test_a_stranded_cover_is_re_priced_even_when_fully_covered(self):
        r = Rig()
        r.add_market(A, book=_wide(r.now))
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        # the New Hampshire shape: short 60 sold at 91.8c, the buy-back
        # left at 6c by a morning restart, the bid since risen to 20c
        r.fam.inventory[A] = {"qty": -60.0, "cost": -55.08}
        r.positions[A] = (-60.0, -55.08)
        r.exchange.live["OLD"] = {"id": "OLD", "market": A, "side": "BUY",
                                  "price": 0.06, "size": 60.0,
                                  "intent": SELL_SHORT}
        r.fam.orders["OLD"] = FamilyOrder(
            id="OLD", market=A, side="BUY", price=0.06, qty=60.0,
            intent=SELL_SHORT, placed_ts=r.now - 7200.0, purpose="sell",
            live_est=0.0)
        r.fam.last_action.clear()
        r.cycle()
        r.fam.last_action.clear()
        r.cycle()
        covers = _covers(r, A)
        self.assertTrue(covers, "the short must stay covered")
        self.assertTrue(all(o.price >= 0.18 for o in covers),
                        [o.price for o in covers])
        self.assertNotIn("OLD", r.fam.orders)
        self.assertLessEqual(sum(o.qty for o in covers), 60.0 + 1e-9)

    def test_a_stock_sell_still_joins_the_ask_touch(self):
        # the 2026-08-22 doctrine stands: sells join the touch, never
        # undercut it, never park behind it
        r = Rig()
        book = Book(bids=((0.92, 30.0), (0.02, 60000.0)),
                    asks=((0.97, 217.0), (0.99, 60000.0)),
                    tick=0.01, fetched_at=r.now)
        r.add_market(A, book=book)
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        r.fam.inventory[A] = {"qty": 10.0, "cost": 9.2}
        r.positions[A] = (10.0, 9.2)
        r.fam.last_action.clear()
        r.cycle()
        sells = _sells(r, A)
        self.assertTrue(sells)
        self.assertTrue(all(abs(o.price - 0.97) < 1e-9 for o in sells),
                        [o.price for o in sells])


class TestNoChurn(unittest.TestCase):
    def test_an_off_grid_anchor_is_not_a_move(self):
        r = Rig()
        # an EMPTY ask side and a model fair: the anchor is fair + 15
        # ticks = 58.39c, off the grid; the resting ask sits at 59c
        book = Book(bids=((0.40, 100.0), (0.02, 60000.0)), asks=(),
                    tick=0.01, fetched_at=r.now)
        r.add_market(A, book=book)
        r.fam.fairs = lambda slug: 0.4339 if slug == A else None
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        r.fam.inventory[A] = {"qty": 40.0, "cost": 9.52}
        r.positions[A] = (40.0, 9.52)
        r.exchange.live["S"] = {"id": "S", "market": A, "side": "SELL",
                                "price": 0.59, "size": 40.0,
                                "intent": SELL_LONG}
        r.fam.orders["S"] = FamilyOrder(
            id="S", market=A, side="SELL", price=0.59, qty=40.0,
            intent=SELL_LONG, placed_ts=r.now - 7200.0, purpose="sell",
            live_est=0.0)
        moves_before = [w for w in r.fam.wind_down if w.get("kind") == "exit move"]
        for _ in range(3):
            r.fam.last_action.clear()          # every pass is past the cooldown
            r.cycle()
        moves = [w for w in r.fam.wind_down if w.get("kind") == "exit move"]
        # whatever it did, it did not cancel and re-place at the SAME price
        for w in moves[len(moves_before):]:
            self.assertGreaterEqual(abs(w["px"] - w["from_px"]), 0.005, w)
        live = [o for o in r.exchange.live.values()
                if o["market"] == A and o["side"] == "SELL"]
        self.assertTrue(live)

    def test_the_step_up_lands_where_it_said_and_does_not_loop(self):
        r = Rig()
        r.fam.cfg.dead_drain_s = 21600.0
        # the house-seats shape of 2026-09-04: our 10-share buy-back IS
        # the best bid at 42c, the next bid 40c, the ask 45c; the short
        # sold at 41c
        book = Book(bids=((0.42, 10.0), (0.40, 50.0), (0.02, 60000.0)),
                    asks=((0.45, 20.0), (0.98, 60000.0)),
                    tick=0.01, fetched_at=r.now)
        r.add_market(A, book=book)
        r.cycle()
        for oid in list(r.fam.orders):
            r.fam.orders.pop(oid)
        r.exchange.live.clear()
        r.fam.inventory[A] = {"qty": -10.0, "cost": -4.1}
        r.positions[A] = (-10.0, -4.1)
        r.exchange.live["BB"] = {"id": "BB", "market": A, "side": "BUY",
                                 "price": 0.42, "size": 10.0,
                                 "intent": SELL_SHORT}
        r.fam.orders["BB"] = FamilyOrder(
            id="BB", market=A, side="BUY", price=0.42, qty=10.0,
            intent=SELL_SHORT, placed_ts=r.now - 7200.0, purpose="sell",
            live_est=0.0)
        for _ in range(6):
            r.fam.last_action.clear()
            r.cycle()
        ups = [e for e in r.fam.log if e.get("event") == "dead_short_stepup"]
        rests = [e for e in r.fam.log if e.get("event") == "sell_rested"
                 and e.get("market") == A]
        # a step that lands on the price it left is no step: at most one
        # genuine move, never the minute-by-minute loop
        self.assertLessEqual(len(ups), 1, ups)
        prices = [round(e["price"], 3) for e in rests]
        self.assertLessEqual(len([p for p in prices if abs(p - 0.42) < 1e-9]), 1,
                             prices)
        covers = _covers(r, A)
        self.assertEqual(round(sum(o.qty for o in covers), 2), 10.0)


if __name__ == "__main__":
    unittest.main()
