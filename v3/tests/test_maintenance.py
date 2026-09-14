"""Scheduled maintenance (owner, 2026-09-14): "There is maintenance from
4-8:30 this morning. Can you pull the orders 15 minutes before and start
putting them back in approximately their correct spot after everything
is back online? Don't have to be too aggressive. If you can get better
prices or higher earning in thinner books do that. Stay flexible."
"""
import unittest

from v3 import maintenance as M
from v3.family import FamilyOrder
from v3.intents import BUY_LONG, SELL_LONG
from v3.maintenance import Maintenance, is_wall, restore_price
from v3.scoring import Book
from v3.tests.test_family import A, Rig

B = "vmc-ussemov-ga-2026-11-03-d8-9"


def book(now, bid=0.40, ask=0.44, tick=0.01, bq=500.0, aq=500.0):
    return Book(bids=((bid, bq),), asks=((ask, aq),), tick=tick, fetched_at=now)


class TestWhereAnOrderGoesBack(unittest.TestCase):
    """"approximately their correct spot ... If you can get better prices
    or higher earning in thinner books do that." His old price, or the
    touch when the touch is better for him, and never across it."""

    def test_a_bid_goes_back_where_it_was(self):
        self.assertEqual(restore_price("BUY", 0.40, 0.42, 0.44, 0.01), 0.40)

    def test_a_bid_takes_the_touch_when_the_touch_is_CHEAPER(self):
        # the side thinned out over the window: the best bid is now under
        # his old price, so resting there is both the better price and
        # the top of the queue. Never pay up.
        self.assertEqual(restore_price("BUY", 0.40, 0.36, 0.44, 0.01), 0.36)

    def test_a_bid_never_pays_more_than_he_did(self):
        for bid in (0.20, 0.36, 0.39, 0.40, 0.55, 0.80):
            px = restore_price("BUY", 0.40, bid, 0.90, 0.01)
            self.assertLessEqual(px, 0.40 + 1e-9)

    def test_an_ask_takes_the_touch_when_the_touch_is_DEARER(self):
        self.assertEqual(restore_price("SELL", 0.44, 0.20, 0.52, 0.01), 0.52)

    def test_an_ask_never_sells_cheaper_than_he_did(self):
        for ask in (0.20, 0.40, 0.44, 0.52, 0.90):
            px = restore_price("SELL", 0.44, 0.10, ask, 0.01)
            self.assertGreaterEqual(px, 0.44 - 1e-9)

    def test_nothing_ever_crosses(self):
        # the market ran away while the exchange was down
        px = restore_price("BUY", 0.60, 0.55, 0.50, 0.01)
        self.assertLess(px, 0.50)
        px = restore_price("SELL", 0.20, 0.30, 0.34, 0.01)
        self.assertGreater(px, 0.30)

    def test_a_one_sided_book_still_places_at_his_old_price(self):
        self.assertEqual(restore_price("BUY", 0.40, None, None, 0.01), 0.40)

    def test_a_qualifying_wall_is_recognised_by_price_and_size(self):
        self.assertTrue(is_wall(0.01, 25000.0))
        self.assertTrue(is_wall(0.99, 9000.0))
        self.assertFalse(is_wall(0.01, 40.0))       # a small 1c bid is an order
        self.assertFalse(is_wall(0.40, 25000.0))


class Base(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        self.r.add_market(A, book=book(self.r.now))
        self.r.add_market(B, book=book(self.r.now, 0.10, 0.14))
        # the desk only touches markets the family knows: in production
        # that is discovery, so run it here rather than faking it
        self.r.fam.last_discover = 0.0
        self.r.fam.refresh_universe(self.r.exchange, self.r.now)
        self.m = Maintenance({"politics": self.r.fam}, self.r.exchange,
                             clock=lambda: self.r.now)
        # one window, starting an hour out
        self.start = self.r.now + 3600.0
        self.end = self.start + 4.5 * 3600.0
        self.m.current = lambda now=None: ("w1", self.start, self.end)

    def rest(self, slug, side, price, qty, purpose="manual"):
        r = self.r.fam.desk.place_resting(
            slug, side, price, qty,
            intent=(BUY_LONG if side == "BUY" else SELL_LONG),
            initiator="owner", verify=False)
        self.r.fam.orders[r.order_id] = FamilyOrder(
            id=r.order_id, market=slug, side=side, price=price, qty=qty,
            intent=(BUY_LONG if side == "BUY" else SELL_LONG),
            placed_ts=self.r.now, purpose=purpose)
        return r.order_id


class TestTheHold(Base):
    def test_nothing_places_from_fifteen_minutes_before_to_the_end(self):
        self.assertFalse(self.m.holding(self.start - M.MAINT_PULL_LEAD_S - 1))
        self.assertTrue(self.m.holding(self.start - M.MAINT_PULL_LEAD_S + 1))
        self.assertTrue(self.m.holding(self.start + 600.0))
        self.assertTrue(self.m.holding(self.end - 1))
        self.assertFalse(self.m.holding(self.end + 1))

    def test_the_lead_is_the_fifteen_minutes_he_asked_for(self):
        self.assertEqual(M.MAINT_PULL_LEAD_S, 900.0)


class TestThePull(Base):
    def test_every_order_comes_off_his_hands_and_walls_included(self):
        self.rest(A, "BUY", 0.40, 50.0, "focus")
        self.rest(A, "SELL", 0.44, 50.0, "sell")
        self.rest(B, "BUY", 0.10, 30.0, "manual")
        self.rest(B, "BUY", 0.01, 19000.0, "manual")       # his wall
        self.r.now = self.start - M.MAINT_PULL_LEAD_S + 1
        self.m.tick(self.r.now)
        self.assertEqual(self.m.phase, "pulled")
        self.assertEqual(len(self.m.orders), 4)
        self.assertEqual(self.r.exchange.live, {})          # all off the book
        self.assertEqual(self.r.fam.orders, {})
        self.assertEqual(sum(1 for r in self.m.orders if r["wall"]), 1)

    def test_the_snapshot_is_the_EXCHANGES_list_not_ours(self):
        # an order of his we never adopted is on the exchange's list and
        # is exactly the kind that never comes back on its own
        oid = self.rest(A, "BUY", 0.38, 20.0)
        self.r.fam.orders.pop(oid)                          # we do not know it
        self.r.now = self.start - M.MAINT_PULL_LEAD_S + 1
        self.m.tick(self.r.now)
        self.assertIn(0.38, [r["price"] for r in self.m.orders])

    def test_it_does_not_fire_early(self):
        self.rest(A, "BUY", 0.40, 50.0)
        self.m.tick(self.start - M.MAINT_PULL_LEAD_S - 60.0)
        self.assertEqual(self.m.phase, "idle")
        self.assertEqual(len(self.r.exchange.live), 1)

    def test_it_fires_once_not_every_tick(self):
        self.rest(A, "BUY", 0.40, 50.0)
        self.r.now = self.start - 60.0
        self.m.tick(self.r.now)
        n = len(self.m.orders)
        self.m.tick(self.r.now + 20.0)
        self.assertEqual(len(self.m.orders), n)
        self.assertEqual(self.m.phase, "pulled")


class TestPuttingThemBack(Base):
    def pull_now(self):
        self.r.now = self.start - 60.0
        self.m.tick(self.r.now)

    def test_nothing_goes_back_until_the_exchange_answers(self):
        self.rest(A, "BUY", 0.40, 50.0)
        self.pull_now()

        def dead(*a, **k):
            raise RuntimeError("connection refused")
        self.r.exchange.open_orders_raw = dead
        self.r.now = self.end + M.MAINT_SETTLE_S + 1
        self.m.tick(self.r.now)
        self.assertEqual(self.m.phase, "pulled")            # still waiting
        self.assertEqual(self.r.exchange.live, {})

    def test_they_go_back_once_it_does(self):
        self.rest(A, "BUY", 0.40, 50.0)
        self.rest(A, "SELL", 0.44, 50.0)
        self.pull_now()
        self.r.now = self.end + M.MAINT_SETTLE_S + 1
        self.m.tick(self.r.now)                             # exchange answers
        self.assertEqual(self.m.phase, "restoring")
        self.m.tick(self.r.now + 20.0)
        self.assertEqual(len(self.m.done), 2)
        prices = sorted(o["price"] for o in self.r.exchange.live.values())
        self.assertEqual(prices, [0.40, 0.44])

    def test_a_wall_goes_back_exactly_where_it_was(self):
        self.rest(B, "BUY", 0.01, 19000.0)
        self.pull_now()
        self.r.now = self.end + M.MAINT_SETTLE_S + 1
        self.m.tick(self.r.now); self.m.tick(self.r.now + 20.0)
        live = list(self.r.exchange.live.values())
        self.assertEqual(len(live), 1)
        self.assertEqual((live[0]["price"], live[0]["size"]), (0.01, 19000.0))

    def test_a_thinner_side_gets_the_better_price(self):
        self.rest(A, "BUY", 0.40, 50.0)
        self.pull_now()
        # the window ends with the bid side thinned right out
        self.r.exchange.books[A] = book(self.r.now, 0.34, 0.44)
        self.r.cache.put(A, self.r.exchange.books[A])
        self.r.now = self.end + M.MAINT_SETTLE_S + 1
        self.m.tick(self.r.now); self.m.tick(self.r.now + 20.0)
        live = list(self.r.exchange.live.values())
        self.assertEqual(live[0]["price"], 0.34)            # cheaper AND the touch
        self.assertEqual(self.m.done[0]["moved"], -0.06)

    def test_not_too_aggressive_a_few_a_pass(self):
        for i in range(14):
            self.rest(A, "BUY", round(0.30 + i * 0.001, 4), 10.0)
        self.pull_now()
        self.assertEqual(len(self.m.orders), 14)
        self.r.now = self.end + M.MAINT_SETTLE_S + 1
        self.m.tick(self.r.now)                             # -> restoring
        self.m.tick(self.r.now + 20.0)
        self.assertEqual(len(self.m.done), M.MAINT_RESTORE_PER_PASS)
        self.m.tick(self.r.now + 40.0)
        self.assertEqual(len(self.m.done), 2 * M.MAINT_RESTORE_PER_PASS)

    def test_a_refused_placement_is_retried_then_reported_never_lost(self):
        self.rest(A, "BUY", 0.40, 50.0)
        self.pull_now()
        from v3.orders import OrderResult
        self.r.fam.desk.place_resting = lambda *a, **k: OrderResult(
            ok=False, note="not enough buying power")
        self.r.now = self.end + M.MAINT_SETTLE_S + 1
        for i in range(M.MAINT_RESTORE_TRIES + 2):
            self.m.tick(self.r.now + 20.0 * i)
        self.assertEqual(len(self.m.done), 0)
        self.assertEqual(len(self.m.failed), 1)
        self.assertIn("buying power", self.m.failed[0]["why"])
        self.assertEqual(self.m.phase, "done")

    def test_the_run_survives_a_restart(self):
        self.rest(A, "BUY", 0.40, 50.0)
        self.pull_now()
        d = self.m.to_dict()
        m2 = Maintenance({"politics": self.r.fam}, self.r.exchange,
                         clock=lambda: self.r.now)
        m2.current = lambda now=None: ("w1", self.start, self.end)
        m2.restore_state(d)
        self.assertEqual(m2.phase, "pulled")
        self.assertEqual(len(m2.orders), 1)
        self.r.now = self.end + M.MAINT_SETTLE_S + 1
        m2.tick(self.r.now); m2.tick(self.r.now + 20.0)
        self.assertEqual(len(m2.done), 1)


class TestTheWindowHeGave(unittest.TestCase):
    def test_the_window_is_four_to_eight_thirty_that_morning_in_his_time(self):
        import datetime as dt
        ws = M.windows()
        self.assertTrue(ws)
        key, a, b = ws[0]
        sa = dt.datetime.fromtimestamp(a, M.ET)
        sb = dt.datetime.fromtimestamp(b, M.ET)
        self.assertEqual((sa.hour, sa.minute), (4, 0))
        self.assertEqual((sb.hour, sb.minute), (8, 30))
        self.assertEqual(sa.date().isoformat(), "2026-09-14")


class TestAWallBiggerThanOneOrder(Base):
    """A qualifying wall can be bigger than the desk will send in one
    order (QTY_MAX is 20,000 and a 1c bid of 25,000 is what carries a
    side to a 25,000 target). It goes back as however many orders it
    takes at the same price — the book sees the same size, which is all
    the program counts."""

    def test_it_goes_back_in_chunks_at_the_same_price(self):
        self.m.key = "w1"
        self.m.phase = "restoring"
        self.m.orders = [{"market": B, "side": "BUY", "price": 0.01,
                          "qty": 25000.0, "id": "old", "key": "politics",
                          "purpose": "manual", "wall": True, "tries": 0}]
        self.r.now = self.end + M.MAINT_SETTLE_S + 1
        self.m.tick(self.r.now)
        live = list(self.r.exchange.live.values())
        self.assertEqual(len(live), 2)
        self.assertEqual({o["price"] for o in live}, {0.01})
        self.assertAlmostEqual(sum(o["size"] for o in live), 25000.0, places=2)
        self.assertEqual(self.m.done[0]["parts"], 2)
        self.assertEqual(len(self.m.failed), 0)

    def test_a_part_way_failure_is_not_counted_as_back(self):
        from v3.orders import OrderResult
        calls = {"n": 0}
        real = self.r.fam.desk.place_resting

        def one_then_refuse(*a, **k):
            calls["n"] += 1
            if calls["n"] > 1:
                return OrderResult(ok=False, note="not enough buying power")
            return real(*a, **k)
        self.r.fam.desk.place_resting = one_then_refuse
        self.m.key = "w1"
        self.m.phase = "restoring"
        self.m.orders = [{"market": B, "side": "BUY", "price": 0.01,
                          "qty": 25000.0, "id": "old", "key": "politics",
                          "purpose": "manual", "wall": True, "tries": 0}]
        self.r.now = self.end + M.MAINT_SETTLE_S + 1
        self.m.tick(self.r.now)
        self.assertEqual(self.m.done, [])
        self.assertEqual(self.m.orders[0]["placed_qty"], 19990.0)
        self.assertIn("buying power", self.m.orders[0]["why"])


class TestAnOrderThatNeverLeftIsNotPlacedTwice(Base):
    """Found on the live run of 2026-09-14: the pull cancelled 272 of 313
    and 29 were still showing when the window opened — a cancel can be
    refused, and the open list lags one either way. Re-placing those
    would offer the same shares twice, which is the shape that flips a
    position when both fill. The restore checks the EXCHANGE'S list, not
    our books."""

    def pull_now(self):
        self.r.now = self.start - 60.0
        self.m.tick(self.r.now)

    def test_one_still_resting_is_left_alone_not_re_placed(self):
        a = self.rest(A, "BUY", 0.40, 50.0)
        self.rest(A, "SELL", 0.44, 50.0)
        self.pull_now()
        # the exchange never took the first one off
        self.r.exchange.live[a] = {"id": a, "market": A, "side": "BUY",
                                   "price": 0.40, "size": 50.0,
                                   "intent": BUY_LONG}
        self.r.now = self.end + M.MAINT_SETTLE_S + 1
        self.m.tick(self.r.now)
        self.m.tick(self.r.now + 20.0)
        buys = [o for o in self.r.exchange.live.values() if o["side"] == "BUY"]
        self.assertEqual(len(buys), 1)                 # never doubled
        self.assertEqual(len(self.m.done), 2)          # both accounted for
        kept = [r for r in self.m.done if r.get("kept")]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["id"], a)
        self.assertEqual(kept[0]["moved"], 0.0)

    def test_the_check_reads_the_exchange_not_our_own_books(self):
        # our books can be wrong in both directions; only the exchange
        # decides whether an order is resting
        a = self.rest(A, "BUY", 0.40, 50.0)
        self.pull_now()
        self.r.fam.orders["ghost"] = FamilyOrder(
            id=a, market=A, side="BUY", price=0.40, qty=50.0,
            intent=BUY_LONG, placed_ts=self.r.now, purpose="manual")
        self.r.now = self.end + M.MAINT_SETTLE_S + 1
        self.m.tick(self.r.now); self.m.tick(self.r.now + 20.0)
        # the exchange does NOT show it, so it goes back
        self.assertEqual(len(self.m.done), 1)
        self.assertFalse(self.m.done[0].get("kept"))
        self.assertEqual(len(self.r.exchange.live), 1)

    def test_a_failed_live_read_never_causes_a_placement(self):
        a = self.rest(A, "BUY", 0.40, 50.0)
        self.pull_now()
        self.r.exchange.live[a] = {"id": a, "market": A, "side": "BUY",
                                   "price": 0.40, "size": 50.0,
                                   "intent": BUY_LONG}
        self.r.now = self.end + M.MAINT_SETTLE_S + 1
        self.m.tick(self.r.now)                       # -> restoring
        self.m.tick(self.r.now + 1.0)                 # the pass reads the list
        self.assertIn(a, self.m._live)

        def dead(*args, **kw):
            raise RuntimeError("down")
        self.r.exchange.open_orders = dead
        self.r.now += M.MAINT_LIVE_RECHECK_S + 1
        self.m.tick(self.r.now)
        self.assertIn(a, self.m._live)                # the last read stands
        self.assertEqual(len([o for o in self.r.exchange.live.values()
                              if o["side"] == "BUY"]), 1)
