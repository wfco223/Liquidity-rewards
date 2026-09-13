"""The dust sweep (owner, 2026-09-13): every holding worth under $1 at
the midpoint gets one exit at his rule, replacing every order on that
side but his qualifying walls; placed once, left, run again by a tap.
"""
import unittest

from v3.family import FamilyOrder
from v3.intents import BUY_LONG, SELL_LONG, SELL_SHORT
from v3.orders import OrderResult
from v3.scoring import Book
from v3.sweep import PURPOSE, SWEEP_MAX_VALUE_USD, Sweep, price_for
from v3.tests.test_family import A, Rig

B = "vmc-ussemov-ga-2026-11-03-d8-9"       # a sibling, not dust


def wide(now, bid=0.05, ask=0.08, tick=0.01):
    return Book(bids=((bid, 100.0),), asks=((ask, 100.0),), tick=tick, fetched_at=now)


class TestTheRule(unittest.TestCase):
    def test_a_long_on_a_one_tick_spread_sells_at_the_ask(self):
        self.assertEqual(price_for(6, 0.05, 0.06, 0.01), ("SELL", 0.06, SELL_LONG))

    def test_a_long_on_a_wide_spread_lists_at_the_midpoint_rounded_toward_the_bid(self):
        self.assertEqual(price_for(6, 0.05, 0.08, 0.01), ("SELL", 0.06, SELL_LONG))   # mid 6.5c
        self.assertEqual(price_for(6, 0.05, 0.07, 0.01), ("SELL", 0.06, SELL_LONG))   # mid 6c exactly

    def test_a_short_on_a_one_tick_spread_buys_back_at_the_bid(self):
        self.assertEqual(price_for(-6, 0.05, 0.06, 0.01), ("BUY", 0.05, SELL_SHORT))

    def test_a_short_on_a_wide_spread_rounds_toward_the_ask(self):
        self.assertEqual(price_for(-6, 0.05, 0.08, 0.01), ("BUY", 0.07, SELL_SHORT))   # mid 6.5c

    def test_a_tenth_cent_book_rounds_to_its_tick(self):
        # a 1c spread on a 0.1c book has an inside: the midpoint, to the tick
        self.assertEqual(price_for(6, 0.175, 0.185, 0.001), ("SELL", 0.18, SELL_LONG))
        self.assertEqual(price_for(-6, 0.175, 0.186, 0.001), ("BUY", 0.181, SELL_SHORT))
        # one tick on that book joins the touch
        self.assertEqual(price_for(6, 0.175, 0.176, 0.001), ("SELL", 0.176, SELL_LONG))

    def test_never_inside_the_touch(self):
        for q in (6, -6):
            for bid, ask in ((0.05, 0.08), (0.30, 0.41), (0.175, 0.199)):
                _, px, _ = price_for(q, bid, ask, 0.01 if bid > 0.2 else 0.001)
                self.assertGreaterEqual(px, bid)
                self.assertLessEqual(px, ask)


class Base(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        self.r.add_market(A, book=wide(self.r.now))          # 6 sh x 6.5c = $0.39: dust
        self.r.add_market(B, book=wide(self.r.now, 0.45, 0.48))  # 100 sh x 46.5c = $46: not dust
        self.r.fam.inventory[A] = {"qty": 6.0, "cost": 0.30}
        self.r.fam.inventory[B] = {"qty": 100.0, "cost": 45.0}
        self.r.positions[A] = (6.0, 0.30)
        self.r.positions[B] = (100.0, 45.0)
        self.sweep = Sweep({"politics": self.r.fam}, self.r.exchange, clock=lambda: self.r.now)

    def orders_on(self, slug, side):
        return [o for o in self.r.fam.orders.values() if o.market == slug and o.side == side]


class TestThePlan(Base):
    def test_only_holdings_under_a_dollar_at_the_midpoint(self):
        p = self.sweep.plan(self.r.now)
        self.assertEqual([r["market"] for r in p["rows"]], [A])
        row = p["rows"][0]
        self.assertEqual((row["side"], row["price"], row["value"]), ("SELL", 0.06, 0.39))
        self.assertEqual(SWEEP_MAX_VALUE_USD, 1.0)

    def test_frozen_and_close_out_ground_are_skipped(self):
        self.r.fam.freeze_dyn.add(A)
        p = self.sweep.plan(self.r.now)
        self.assertEqual(p["rows"], [])
        self.assertEqual(p["skipped"][0]["why"], "frozen ground")
        self.r.fam.freeze_dyn.discard(A)
        self.r.fam.cfg.liquidate_tokens = ("ussemov-ga-2026-11-03-d4",)
        p = self.sweep.plan(self.r.now)
        self.assertEqual(p["rows"], [])
        self.assertIn("close-out", p["skipped"][0]["why"])

    def test_a_one_sided_or_missing_book_is_skipped_and_said(self):
        self.r.cache.put(A, Book(bids=((0.05, 100.0),), asks=(), tick=0.01, fetched_at=self.r.now))
        self.r.exchange.books[A] = self.r.cache.any_age(A)
        p = self.sweep.plan(self.r.now)
        self.assertEqual(p["rows"], [])
        self.assertEqual(p["skipped"][0]["why"], "one-sided book")


class TestTheRun(Base):
    def test_places_one_exit_for_the_whole_lot_as_a_sweep_order(self):
        res = self.sweep.run(self.r.now)
        self.assertEqual(res["n"], 1)
        mine = self.orders_on(A, "SELL")
        self.assertEqual(len(mine), 1)
        o = mine[0]
        self.assertEqual((o.purpose, o.price, o.qty, o.intent), (PURPOSE, 0.06, 6.0, SELL_LONG))
        self.assertIn(o.id, self.r.exchange.live)      # it rests on the exchange
        self.assertEqual(self.sweep.preview, {})       # the plan was acted on

    def test_replaces_the_engines_the_tenders_and_his_hand_orders_but_not_a_wall(self):
        for oid, purpose, price, qty, why in (
                ("eng", "sell", 0.07, 6.0, "engine exit"),
                ("ten", "focus", 0.08, 6.0, "focus exit: ~$1/day"),
                ("hand", "manual", 0.10, 6.0, ""),
                ("wall", "manual", 0.99, 25000.0, "")):      # his qualifying wall: far edge, huge
            self.r.fam.orders[oid] = FamilyOrder(id=oid, market=A, side="SELL", price=price,
                                                 qty=qty, intent=SELL_LONG, placed_ts=1.0,
                                                 purpose=purpose, why=why)
        self.r.fam.orders["bid"] = FamilyOrder(id="bid", market=A, side="BUY", price=0.04,
                                               qty=6.0, intent=BUY_LONG, placed_ts=1.0,
                                               purpose="manual")           # the other side
        res = self.sweep.run(self.r.now)
        self.assertEqual(res["n"], 1)
        placed = res["placed"][0]
        self.assertEqual(sorted(placed["replaced"]), ["eng", "hand", "ten"])
        self.assertEqual(placed["hand"], 1)
        left = {o.id for o in self.r.fam.orders.values() if o.market == A}
        self.assertNotIn("eng", left); self.assertNotIn("ten", left); self.assertNotIn("hand", left)
        self.assertIn("wall", left)                    # the wall offers none of the lot
        self.assertIn("bid", left)                     # the other side is not an exit
        self.assertEqual(len(self.orders_on(A, "SELL")), 2)   # the sweep's + the wall

    def test_a_short_is_bought_back_at_the_rule(self):
        self.r.fam.inventory[A] = {"qty": -6.0, "cost": -0.30}
        self.r.positions[A] = (-6.0, -0.30)
        res = self.sweep.run(self.r.now)
        o = self.orders_on(A, "BUY")[0]
        self.assertEqual((o.purpose, o.price, o.qty, o.intent), (PURPOSE, 0.07, 6.0, SELL_SHORT))

    def test_a_refused_placement_is_reported_and_leaves_no_record(self):
        self.r.fam.desk.place_resting = lambda *a, **k: OrderResult(ok=False, note="nope")
        res = self.sweep.run(self.r.now)
        self.assertEqual(res["n"], 0)
        self.assertEqual(res["failed"][0]["note"], "nope")
        self.assertEqual(self.orders_on(A, "SELL"), [])
        self.assertEqual(self.r.fam.log[-1]["event"], "sweep_refused")

    def test_the_engine_never_offers_the_lot_on_top_of_a_sweep_order(self):
        # control: with nothing resting, the engine's own cycle rests an exit
        self.r.cycle()
        self.assertGreaterEqual(len(self.orders_on(A, "SELL")), 1)
        for o in list(self.orders_on(A, "SELL")):
            self.r.fam.orders.pop(o.id); self.r.exchange.live.pop(o.id, None)
        # with the sweep's exit resting, the engine sees the lot offered and adds nothing
        self.sweep.run(self.r.now)
        self.assertEqual(len(self.orders_on(A, "SELL")), 1)
        self.r.cycle()
        sells = self.orders_on(A, "SELL")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0].purpose, PURPOSE)   # and never touched it

    def test_the_last_run_is_kept_and_restored(self):
        self.sweep.run(self.r.now)
        d = self.sweep.to_dict()
        s2 = Sweep({"politics": self.r.fam}, self.r.exchange, clock=lambda: self.r.now)
        s2.restore(d)
        self.assertEqual(s2.last["n"], 1)
        self.assertEqual(s2.view()["last"]["placed"][0]["market"], A)


class TestTheTenderStandsDown(unittest.TestCase):
    def test_a_sweep_order_counts_as_cover_and_is_never_adopted(self):
        from v3.tests.test_focus import Base as FocusBase, NC
        fb = FocusBase("setUp"); fb.setUp()
        fb.f.set_fair(NC, 45.0)
        fb.r.positions[NC] = (6.0, 2.7)
        fb.r.fam.inventory[NC] = {"qty": 6.0, "cost": 2.7}
        fb.r.fam.orders["swp"] = FamilyOrder(id="swp", market=NC, side="SELL", price=0.46,
                                             qty=6.0, intent=SELL_LONG, placed_ts=1.0,
                                             purpose=PURPOSE, why="the dust sweep")
        fb.tick()
        self.assertEqual(fb.r.fam.orders["swp"].purpose, PURPOSE)      # not adopted, not relabelled
        ex = fb.f.rows[NC].get("exit") or {}
        self.assertTrue(ex.get("hold"), ex)                            # the lot reads as offered
        tender_exits = [o for o in fb.r.fam.orders.values()
                        if o.market == NC and o.side == "SELL" and o.id != "swp"
                        and o.purpose != "manual"]
        self.assertEqual(tender_exits, [])                             # no second exit


if __name__ == "__main__":
    unittest.main()
