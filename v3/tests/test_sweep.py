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


class TestTheBookIsReadRightBeforePlacing(Base):
    """Owner, 2026-09-13: "It skipped almost everything because the books
    were old. Can you read the book immediately before placing." The
    16:51Z run placed 7 and was refused 95, every refusal "no book
    fresher than 120s — refusing to place blind": the plan had priced all
    102 holdings up front and the placing loop that followed took
    minutes, so the books it was pricing from aged out under it."""

    def place_takes(self, seconds):
        """Every placement advances the clock, as a real one does."""
        real = self.r.fam.desk.place_resting

        def slow(*a, **kw):
            self.r.now += seconds
            return real(*a, **kw)
        self.r.fam.desk.place_resting = slow

    def test_a_long_placing_loop_still_places_every_order(self):
        for i in range(6):
            slug = f"{A}-{i}"
            self.r.add_market(slug, book=wide(self.r.now))
            self.r.fam.inventory[slug] = {"qty": 6.0, "cost": 0.30}
            self.r.positions[slug] = (6.0, 0.30)
        self.place_takes(45.0)          # 7 orders x 45 s = five minutes of loop
        res = self.sweep.run(self.r.now)
        self.assertEqual(res["n"], 7, res["failed"])
        self.assertEqual(res["failed"], [])
        # every one rests, and each was priced on a book read for it
        for i in range(6):
            self.assertEqual(len(self.orders_on(f"{A}-{i}", "SELL")), 1)

    def test_the_book_is_read_for_the_order_when_the_cache_has_gone_old(self):
        self.r.exchange.book_reads = []
        self.r.now += 100.0             # older than SWEEP_PLACE_BOOK_MAX_AGE_S
        self.sweep.run(self.r.now)
        # the plan took the cached book (still inside its own 120s) and the
        # placement read again for itself — the read that the price and the
        # desk's freshness gate both depend on
        self.assertEqual(self.r.exchange.book_reads.count(A), 1)
        self.assertEqual(self.orders_on(A, "SELL")[0].price, 0.06)
        self.assertLessEqual(self.r.cache.age(A, self.r.now), 30.0)

    def test_the_plan_stamps_each_book_at_its_own_read(self):
        # the tender's 2026-09-12 lesson, which the sweep needed too: a
        # pass over a hundred holdings takes minutes through a throttled
        # gateway, and a book stamped with the PASS's start reads minutes
        # old the moment it lands, so the desk throws it out
        slugs = [A]
        for i in range(3):
            slug = f"{A}-{i}"
            slugs.append(slug)
            self.r.add_market(slug, book=wide(self.r.now))
            self.r.fam.inventory[slug] = {"qty": 6.0, "cost": 0.30}
            self.r.positions[slug] = (6.0, 0.30)
        self.r.now += 300.0             # every cached book past the plan's window
        real = self.r.exchange.book

        def slow_read(slug, **kw):
            self.r.now += 20.0          # each read costs time, as a real one does
            return real(slug, **kw)
        self.r.exchange.book = slow_read
        start = self.r.now
        self.sweep.plan(self.r.now)
        self.assertGreaterEqual(self.r.now - start, 80.0)     # the pass took 80 s
        # the LAST book read is seconds old, not 80 s old
        ages = sorted(self.r.cache.age(s, self.r.now) for s in slugs)
        self.assertLessEqual(ages[0], 20.0)
        self.assertLess(ages[-1], self.r.now - start + 1.0)

    def test_a_book_the_stream_just_delivered_spends_no_gateway_read(self):
        self.sweep.plan(self.r.now)     # warms the cache at this second
        self.r.exchange.book_reads = []
        self.sweep.run(self.r.now)
        self.assertEqual(self.r.exchange.book_reads, [])
        self.assertEqual(len(self.orders_on(A, "SELL")), 1)

    def test_the_price_comes_from_the_read_not_the_plan(self):
        real = self.r.fam.desk.place_resting

        def move_the_book(*a, **kw):
            return real(*a, **kw)
        self.r.fam.desk.place_resting = move_the_book
        self.sweep.plan(self.r.now)
        self.assertEqual(self.sweep.preview["rows"][0]["price"], 0.06)
        # the market moves, and the clock moves past the cached book
        self.r.exchange.books[A] = wide(self.r.now, 0.20, 0.23)   # mid 21.5c
        self.r.now += 100.0
        self.r.fam.inventory[A] = {"qty": 4.0, "cost": 0.30}      # 4 x 21.5c = $0.86
        self.r.positions[A] = (4.0, 0.30)
        res = self.sweep.run(self.r.now)
        self.assertEqual(res["n"], 1)
        o = self.orders_on(A, "SELL")[0]
        self.assertEqual(o.price, 0.21)            # the fresh book's rule price
        self.assertEqual(res["placed"][0]["mid"], 0.215)

    def test_a_lot_worth_over_the_dollar_on_the_fresh_read_is_left_alone(self):
        self.sweep.plan(self.r.now)
        self.assertEqual(self.sweep.preview["n"], 1)
        self.r.exchange.books[A] = wide(self.r.now, 0.40, 0.43)   # 6 sh x 41.5c = $2.49
        self.r.now += 100.0
        res = self.sweep.run(self.r.now)
        self.assertEqual(res["n"], 0)
        self.assertEqual(self.orders_on(A, "SELL"), [])
        self.assertIn("over the dollar", res["skipped"][-1]["why"])

    def test_a_failed_read_leaves_the_lot_and_says_so(self):
        self.sweep.plan(self.r.now)
        self.r.now += 100.0

        def boom(slug, **kw):
            raise RuntimeError("HTTP 429: held 10s more after a 429")
        self.r.exchange.book = boom
        res = self.sweep.run(self.r.now)
        self.assertEqual(res["n"], 0)
        self.assertEqual(self.orders_on(A, "SELL"), [])
        self.assertIn("429", res["failed"][0]["note"])
        self.assertEqual(self.r.fam.log[-1]["event"], "sweep_refused")

    def test_an_order_laid_on_the_side_since_the_plan_is_replaced_too(self):
        # the lot is never offered twice: what rests NOW comes off, not
        # what rested when the plan ran
        self.sweep.plan(self.r.now)
        self.assertEqual(self.sweep.preview["rows"][0]["replace"], [])
        self.r.fam.orders["late"] = FamilyOrder(
            id="late", market=A, side="SELL", price=0.07, qty=6.0,
            intent=SELL_LONG, placed_ts=1.0, purpose="sell", why="engine exit")
        res = self.sweep.run(self.r.now)
        self.assertEqual(res["placed"][0]["replaced"], ["late"])
        self.assertEqual(len(self.orders_on(A, "SELL")), 1)
        self.assertEqual(self.orders_on(A, "SELL")[0].purpose, PURPOSE)

    def test_running_the_button_again_replaces_the_sweeps_own_order(self):
        self.sweep.run(self.r.now)
        first = self.orders_on(A, "SELL")[0].id
        self.r.now += 100.0
        res = self.sweep.run(self.r.now)
        self.assertEqual(res["placed"][0]["replaced"], [first])
        self.assertEqual(len(self.orders_on(A, "SELL")), 1)   # never twice


class TestTheTapAnswersAtOnce(Base):
    """2026-09-13, the owner: "Nothing is happening when I click preview
    the sweep" — the first tap read ~120 books through the throttled
    gateway on the web thread and the page, frozen at cycle end, showed
    nothing for minutes. Now the work runs on the sweep's own thread,
    the tap answers at once, and the card polls /sweep.json."""

    def test_a_preview_runs_in_the_background_and_answers_at_once(self):
        r = self.sweep.start("preview", self.r.now)
        self.assertTrue(r["ok"], r)
        self.assertIn("reading the books of 2 holdings", r["note"])
        self.sweep._thread.join(5.0)
        self.assertIsNone(self.sweep.busy)
        self.assertEqual(self.sweep.preview["n"], 1)
        self.assertEqual(self.sweep.error, "")

    def test_a_second_tap_while_busy_answers_with_progress_not_a_second_run(self):
        import threading
        gate = threading.Event()
        real = self.sweep.plan

        def slow_plan(now, busy=None):
            if busy is not None:
                busy.update(phase="reading", done=3, total=9)
            gate.wait(5.0)
            return real(now, busy=busy)
        self.sweep.plan = slow_plan
        self.assertTrue(self.sweep.start("preview", self.r.now)["ok"])
        again = self.sweep.start("preview", self.r.now)
        self.assertFalse(again["ok"])
        self.assertIn("already previewing", again["note"])
        self.assertIn("3 of 9", again["note"])
        gate.set()
        self.sweep._thread.join(5.0)
        self.assertIsNone(self.sweep.busy)
        self.assertEqual(self.sweep.preview["n"], 1)

    def test_a_run_calls_the_done_hook_with_its_result(self):
        got = []
        r = self.sweep.start("run", self.r.now, on_done=got.append)
        self.assertTrue(r["ok"])
        self.assertIn("then places", r["note"])
        self.sweep._thread.join(5.0)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["n"], 1)
        self.assertEqual(len(self.orders_on(A, "SELL")), 1)

    def test_an_error_in_the_work_is_said_on_the_card_not_swallowed(self):
        def boom(now, busy=None):
            raise RuntimeError("gateway melted")
        self.sweep.plan = boom
        self.assertTrue(self.sweep.start("preview", self.r.now)["ok"])
        self.sweep._thread.join(5.0)
        self.assertIsNone(self.sweep.busy)
        self.assertIn("gateway melted", self.sweep.error)
        v = self.sweep.view()
        self.assertIn("busy", v); self.assertIn("error", v)
        self.assertIsNone(v["busy"])

    def test_an_unknown_op_is_refused(self):
        self.assertFalse(self.sweep.start("nuke", self.r.now)["ok"])


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
