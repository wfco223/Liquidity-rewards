"""The dust sweep (owner, 2026-09-13): every holding worth under $1 at
the midpoint is cleared, replacing every order on that side but his
qualifying walls; run again by a tap.

THE SHARES CROSS (owner, 2026-09-14, correcting my reading of the rule:
"The original intent of my request for the sweep process was to have
these shares cross the midpoint to sell because resting will do
nothing"). The midpoint decides WHICH lots go and nothing else: a long
sells at the bid, a short buys back at the ask, the whole lot, taking
whatever rests there. What the touch cannot absorb is left resting at
that same price.
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
    """Owner, 2026-09-14: "have these shares cross the midpoint to sell
    because resting will do nothing." A long sells AT THE BID, a short
    buys back AT THE ASK — the far side of the spread, every time."""

    def test_a_long_sells_at_the_bid(self):
        self.assertEqual(price_for(6, 0.05, 0.06, 0.01), ("SELL", 0.05, SELL_LONG))
        self.assertEqual(price_for(6, 0.05, 0.08, 0.01), ("SELL", 0.05, SELL_LONG))

    def test_a_short_buys_back_at_the_ask(self):
        self.assertEqual(price_for(-6, 0.05, 0.06, 0.01), ("BUY", 0.06, SELL_SHORT))
        self.assertEqual(price_for(-6, 0.05, 0.08, 0.01), ("BUY", 0.08, SELL_SHORT))

    def test_the_midpoint_is_never_the_price_however_wide_the_spread(self):
        # the old rule rested at the midpoint and sold nothing: of 96
        # orders placed at 00:08Z on 2026-09-14, 27 were still sitting
        # there two and a half hours later
        for bid, ask in ((0.05, 0.08), (0.30, 0.41), (0.10, 0.90)):
            _, long_px, _ = price_for(6, bid, ask, 0.01)
            _, short_px, _ = price_for(-6, bid, ask, 0.01)
            mid = (bid + ask) / 2.0
            self.assertNotEqual(long_px, mid)
            self.assertNotEqual(short_px, mid)
            self.assertEqual((long_px, short_px), (bid, ask))

    def test_a_tenth_cent_book_crosses_at_its_own_touch(self):
        # the price comes off the book, so it is already on the grid
        self.assertEqual(price_for(6, 0.175, 0.185, 0.001), ("SELL", 0.175, SELL_LONG))
        self.assertEqual(price_for(-6, 0.175, 0.186, 0.001), ("BUY", 0.186, SELL_SHORT))

    def test_never_worse_than_the_touch(self):
        for bid, ask in ((0.05, 0.08), (0.30, 0.41), (0.175, 0.199)):
            _, long_px, _ = price_for(6, bid, ask, 0.001)
            _, short_px, _ = price_for(-6, bid, ask, 0.001)
            self.assertGreaterEqual(long_px, bid)     # a sale never under the bid
            self.assertLessEqual(short_px, ask)       # a cover never over the ask


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

    def touch_absorbs(self, qty):
        """The touch takes this much of a crossing order; the rest rests.
        0 models a touch that was gone by the time the order landed."""
        self.r.exchange.taker_fill_qty = qty


class TestThePlan(Base):
    def test_only_holdings_under_a_dollar_at_the_midpoint(self):
        p = self.sweep.plan(self.r.now)
        self.assertEqual([r["market"] for r in p["rows"]], [A])
        row = p["rows"][0]
        # the midpoint picks the lot ($0.39); the BID is the price
        self.assertEqual((row["side"], row["price"], row["value"]), ("SELL", 0.05, 0.39))
        self.assertEqual(SWEEP_MAX_VALUE_USD, 1.0)

    def test_the_preview_says_what_crossing_gives_up(self):
        # a taker order spends money a resting one does not, so the card
        # shows the cost BEFORE he taps Place
        p = self.sweep.plan(self.r.now)
        self.assertAlmostEqual(p["rows"][0]["concession"], 6.0 * 0.015, places=4)
        self.assertAlmostEqual(p["concession"], 0.09, places=2)

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
    def test_the_whole_lot_crosses_at_the_bid_and_is_gone(self):
        res = self.sweep.run(self.r.now)
        self.assertEqual(res["n"], 1)
        row = res["placed"][0]
        self.assertEqual((row["side"], row["price"]), ("SELL", 0.05))
        self.assertEqual(row["filled"], 6.0)
        self.assertEqual(row["resting"], 0.0)
        # it traded, so nothing is left resting and the position is flat
        self.assertEqual(self.orders_on(A, "SELL"), [])
        self.assertNotIn(A, self.r.fam.inventory)
        self.assertEqual(self.sweep.preview, {})       # the plan was acted on

    def test_a_fill_is_booked_the_moment_it_happens_not_left_to_the_feed(self):
        # the position feed lags a fill by a read or more, and an unbooked
        # sale would leave the engine sizing exits for shares that are gone
        self.sweep.run(self.r.now)
        self.assertNotIn(A, self.r.fam.inventory)
        self.assertIn("sweep_filled", [r["event"] for r in self.r.fam.log[-3:]])
        self.assertTrue(any(f.get("market") == A for f in self.r.fam.fills))

    def test_what_the_touch_cannot_absorb_is_left_resting_at_the_same_price(self):
        self.touch_absorbs(4.0)                 # the bid shows 4 of our 6
        res = self.sweep.run(self.r.now)
        row = res["placed"][0]
        self.assertEqual((row["filled"], row["resting"]), (4.0, 2.0))
        mine = self.orders_on(A, "SELL")
        self.assertEqual(len(mine), 1)
        o = mine[0]
        # the remainder rests at the crossing price, not the midpoint
        self.assertEqual((o.purpose, o.price, o.qty, o.intent),
                         (PURPOSE, 0.05, 2.0, SELL_LONG))
        self.assertEqual(self.r.fam.inventory[A]["qty"], 2.0)

    def test_an_answer_with_no_executions_books_nothing_and_rests_the_lot(self):
        # the 2026-09-12 close-out lesson: book only what the exchange's
        # own answer says executed
        self.touch_absorbs(0.0)
        res = self.sweep.run(self.r.now)
        self.assertEqual((res["placed"][0]["filled"], res["placed"][0]["resting"]), (0.0, 6.0))
        self.assertEqual(self.r.fam.inventory[A]["qty"], 6.0)
        self.assertEqual(self.orders_on(A, "SELL")[0].qty, 6.0)

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
        # the sweep's own order crossed and traded, so only the wall is left
        self.assertEqual([o.id for o in self.orders_on(A, "SELL")], ["wall"])

    def test_a_short_is_bought_back_across_the_spread_at_the_ask(self):
        self.r.fam.inventory[A] = {"qty": -6.0, "cost": -0.30}
        self.r.positions[A] = (-6.0, -0.30)
        res = self.sweep.run(self.r.now)
        row = res["placed"][0]
        self.assertEqual((row["side"], row["price"], row["filled"]), ("BUY", 0.08, 6.0))
        self.assertNotIn(A, self.r.fam.inventory)          # the short is closed
        # and a remainder would rest as the cover it is, never a fresh long
        self.r.fam.inventory[A] = {"qty": -6.0, "cost": -0.30}
        self.r.positions[A] = (-6.0, -0.30)
        self.touch_absorbs(2.0)
        self.sweep.run(self.r.now)
        o = self.orders_on(A, "BUY")[0]
        self.assertEqual((o.purpose, o.price, o.qty, o.intent),
                         (PURPOSE, 0.08, 4.0, SELL_SHORT))

    def test_a_refused_placement_is_reported_and_leaves_no_record(self):
        self.r.fam.desk.place_resting = lambda *a, **k: OrderResult(ok=False, note="nope")
        res = self.sweep.run(self.r.now)
        self.assertEqual(res["n"], 0)
        self.assertEqual(res["failed"][0]["note"], "nope")
        self.assertEqual(self.orders_on(A, "SELL"), [])
        self.assertEqual(self.r.fam.log[-1]["event"], "sweep_refused")

    def test_the_engine_never_offers_the_lot_on_top_of_a_sweep_order(self):
        self.touch_absorbs(0.0)        # the lot rests, so there is one to guard
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

    def setUp(self):
        super().setUp()
        # these tests are about WHEN the book is read, not about the fill:
        # a touch that absorbs nothing leaves the order resting to inspect
        self.touch_absorbs(0.0)

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
        self.assertEqual(self.orders_on(A, "SELL")[0].price, 0.05)
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
        self.assertEqual(self.sweep.preview["rows"][0]["price"], 0.05)
        # the market moves, and the clock moves past the cached book
        self.r.exchange.books[A] = wide(self.r.now, 0.20, 0.23)   # mid 21.5c
        self.r.now += 100.0
        self.r.fam.inventory[A] = {"qty": 4.0, "cost": 0.30}      # 4 x 21.5c = $0.86
        self.r.positions[A] = (4.0, 0.30)
        res = self.sweep.run(self.r.now)
        self.assertEqual(res["n"], 1)
        o = self.orders_on(A, "SELL")[0]
        self.assertEqual(o.price, 0.20)            # the fresh book's bid
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
        self.touch_absorbs(0.0)
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


class TestTheEngineIsHandsOff(Base):
    """The sweep order is the engine's to leave alone: never cancelled,
    moved, trimmed or nursed, it charges no ceiling, and it is netted out
    of every exit the engine sizes. My own defect, found on the 19:00Z
    check of 2026-09-13: the claim was true in some paths and false in
    five. The short-cover sum was the one that cost money — it did not
    name "sweep", so a sweep cover read as nothing, the engine sized a
    second cover for the whole short and the lot was offered twice in ten
    markets after the 18:16Z run. Both filling flips a short long."""

    def setUp(self):
        super().setUp()
        # these tests are about what the ENGINE does to a sweep order that
        # is resting, so the touch absorbs nothing and the lot stays on
        # the book to be left alone
        self.touch_absorbs(0.0)

    def short_with_a_sweep_cover(self, qty=-6.0):
        """A real short with the sweep's cover resting on the exchange —
        placed by the sweep itself, so the open list shows it as the
        engine's reconcile will."""
        self.r.fam.inventory[A] = {"qty": qty, "cost": qty * 0.05}
        self.r.positions[A] = (qty, qty * 0.05)
        res = self.sweep.run(self.r.now)
        self.assertEqual(res["n"], 1, res)
        mine = self.orders_on(A, "BUY")
        self.assertEqual([o.purpose for o in mine], [PURPOSE])
        return mine[0]

    def engine_cover(self, qty=6.0, price=0.03):
        """The engine's own cover resting beside it — the live 18:16Z shape."""
        res = self.r.fam.desk.place_resting(
            A, "BUY", price, qty, net_position=-qty, intent=SELL_SHORT,
            close_short=True, verify=False)
        self.assertTrue(res.ok, res.note)
        self.r.fam.orders[res.order_id] = FamilyOrder(
            id=res.order_id, market=A, side="BUY", price=price, qty=qty,
            intent=SELL_SHORT, placed_ts=self.r.now, purpose="sell",
            why="engine cover")
        return res.order_id

    def test_a_sweep_cover_is_netted_so_the_short_is_never_covered_twice(self):
        o = self.short_with_a_sweep_cover()
        self.r.cycle()
        buys = self.orders_on(A, "BUY")
        self.assertEqual([b.purpose for b in buys], [PURPOSE])
        self.assertEqual(sum(b.qty for b in buys), 6.0)     # the short, once
        self.assertIn(o.id, self.r.fam.orders)

    def test_an_engine_cover_beside_a_sweep_cover_is_pruned_not_the_sweep(self):
        # the ten live doubles of 18:16Z: the engine's own cover comes off
        o = self.short_with_a_sweep_cover()
        eng = self.engine_cover()
        self.r.cycle()
        left = {b.id for b in self.orders_on(A, "BUY")}
        self.assertIn(o.id, left)
        self.assertNotIn(eng, left)

    def test_a_sweep_order_is_not_pulled_at_kickoff(self):
        o = self.short_with_a_sweep_cover()
        self.r.fam.cfg.kickoff_pull = True
        self.r.fam.event_start = {A: self.r.now - 60.0}
        self.r.cycle()
        self.assertIn(o.id, self.r.fam.orders)

    def test_a_sweep_order_never_advances_the_probe_ratchet(self):
        # the aged-order path does not retire anything, it advances the
        # blank-market probe ratchet; a sweep order is not a probe
        o = self.short_with_a_sweep_cover()
        self.r.fam.orders[o.id].placed_ts = self.r.now - 200000.0
        before = dict(self.r.fam.probe_ratchet)
        self.r.cycle()
        self.assertIn(o.id, self.r.fam.orders)
        self.assertEqual(self.r.fam.probe_ratchet.get(f"{A}|BUY"),
                         before.get(f"{A}|BUY"))

    def test_a_fractional_sweep_order_survives_the_whole_shares_cull(self):
        o = self.short_with_a_sweep_cover(qty=-6.25)
        self.assertNotEqual(o.qty, round(o.qty))
        self.r.fam.cfg.whole_shares = True
        self.r.cycle()
        self.assertIn(o.id, self.r.fam.orders)

    def test_a_sweep_order_charges_no_ceiling_and_is_never_trimmed(self):
        # it charges nothing (it is an owner exit), so it must never be
        # the thing cancelled to get back under a ceiling it never fed
        o = self.short_with_a_sweep_cover()
        entry = self.r.fam.desk.place_resting(
            B, "BUY", 0.44, 100.0, net_position=0.0, intent=BUY_LONG,
            verify=False)
        self.assertTrue(entry.ok, entry.note)
        self.r.fam.orders[entry.order_id] = FamilyOrder(
            id=entry.order_id, market=B, side="BUY", price=0.44, qty=100.0,
            intent=BUY_LONG, placed_ts=self.r.now, purpose="earn",
            why="an entry that blows the ceiling")
        self.r.fam.cfg.capital_usd = 0.0           # every ceiling blown
        self.assertGreater(self.r.fam.family_spent(), 0.0)
        self.r.fam._trim(self.r.now, 5)
        self.assertIn(o.id, self.r.fam.orders)              # the sweep stands
        self.assertNotIn(entry.order_id, self.r.fam.orders)  # the entry went


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


class TestCrossingIsRailed(Base):
    """Crossing is the THIRD carved exception to post-only placement
    (owner, 2026-09-14). The rails that hold it: his tap only, a close of
    something already held, and never a price worse than the touch."""

    def test_the_order_goes_out_as_a_taker_not_post_only(self):
        sent = []
        real = self.r.exchange.post

        def watch(url, body, path=None, **kw):
            if url.endswith("/v1/orders"):
                sent.append(body)
            return real(url, body, path=path, **kw)
        self.r.exchange.post = watch
        self.sweep.run(self.r.now)
        self.assertEqual(len(sent), 1)
        # post-only OFF: the order is allowed to initiate a trade
        self.assertIs(sent[0]["participateDontInitiate"], False)

    def test_a_price_worse_than_the_touch_is_refused(self):
        desk = self.r.fam.desk
        # a sale UNDER the bid, and a cover OVER the ask, both refused
        r1 = desk.place_resting(A, "SELL", 0.04, 6.0, net_position=6.0,
                                intent=SELL_LONG, taker="sweep",
                                initiator="owner", verify=False)
        self.assertFalse(r1.ok)
        self.assertIn("never worse than the touch", r1.note)
        self.r.fam.inventory[A] = {"qty": -6.0, "cost": -0.30}
        r2 = desk.place_resting(A, "BUY", 0.09, 6.0, net_position=-6.0,
                                intent=SELL_SHORT, taker="sweep",
                                close_short=True, initiator="owner", verify=False)
        self.assertFalse(r2.ok)
        self.assertIn("never worse than the touch", r2.note)

    def test_nothing_but_his_tap_may_cross(self):
        r = self.r.fam.desk.place_resting(A, "SELL", 0.05, 6.0, net_position=6.0,
                                          intent=SELL_LONG, taker="sweep",
                                          initiator="auto", verify=False)
        self.assertFalse(r.ok)
        self.assertIn("owner's tap only", r.note)

    def test_it_may_only_close_a_position_never_open_one(self):
        r = self.r.fam.desk.place_resting(A, "BUY", 0.05, 6.0, net_position=0.0,
                                          intent=BUY_LONG, taker="sweep",
                                          initiator="owner", verify=False)
        self.assertFalse(r.ok)
        self.assertIn("only close a position", r.note)


class TestTheCostWalksBackToZero(Base):
    """A position's cost carries the sign of its quantity (owner,
    2026-09-12), so a basis is always a price a share. A crossing fill
    has to move the cost with the shares or the next round trip learns a
    nonsense number."""

    def test_a_sale_takes_its_proceeds_out_of_the_basis(self):
        self.touch_absorbs(3.0)                  # 3 of 6 sell at 5c
        self.sweep.run(self.r.now)
        inv = self.r.fam.inventory[A]
        self.assertEqual(inv["qty"], 3.0)
        self.assertAlmostEqual(inv["cost"], 0.30 - 3.0 * 0.05, places=4)
        self.assertGreater(inv["cost"] / inv["qty"], 0.0)   # still a price a share

    def test_covering_a_short_walks_its_basis_back_toward_zero(self):
        self.r.fam.inventory[A] = {"qty": -6.0, "cost": -0.30}
        self.r.positions[A] = (-6.0, -0.30)
        self.touch_absorbs(3.0)                  # 3 of 6 bought back at 8c
        self.sweep.run(self.r.now)
        inv = self.r.fam.inventory[A]
        self.assertEqual(inv["qty"], -3.0)
        self.assertAlmostEqual(inv["cost"], -0.30 + 3.0 * 0.08, places=4)
        self.assertGreater(inv["cost"] / inv["qty"], 0.0)   # sign still matches
