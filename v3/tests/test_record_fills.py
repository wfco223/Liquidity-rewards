"""The tender reads the exchange's trade record the pass one of its
orders shrinks or leaves the book (owner, 2026-09-26 "Yes, build it").

Five of the tender's fills in twelve hours on 2026-09-26 were found only
by the hourly match against the exchange's record, because the family
books a fill only when the position feed has moved with it and the feed
lags. In between, the tender planned as if nothing had filled:

- 05:25Z, Texas Senate dem: 42 of a 90-share entry filled; ten seconds
  later the tender saw the order at 48 and resized it back up to 79.
- 23:49Z (09-25), Minnesota Senate dem: the hourly match added the fill
  and the family's reconcile booked it seconds later; the journal read
  230 of a 115-share entry and the exit rested at 230 for three minutes.
- 12:50Z, Texas governor rep: an exit of 83 booked twice the same way.
- 04:17Z, House control rep: the exit of 9 sold unbooked; at 04:36Z a
  second exit was rested on the closed position.

The three parts: the record is read by order id and the fill booked at
once; while an entry's shrink is unexplained its side grows nothing; and
one trade is booked once.
"""
import os
import tempfile
import time
import unittest

from v3 import focus as focus_mod
from v3.tests.test_focus import NC, Base


def trade_row(oid, intent, px, shares, ts, market=NC, xid=None):
    """One execution of ours in the exchange's activity shape."""
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    return {"type": "ACTIVITY_TYPE_TRADE", "trade": {
        "id": f"t{xid or oid}", "marketSlug": market, "updateTime": iso,
        "passiveExecution": {
            "id": f"x{xid or oid}", "transactTime": iso,
            "order": {"id": oid, "intent": intent, "createTime": iso,
                      "price": {"value": f"{px:.4f}"}},
            "lastShares": f"{shares:.4f}",
            "lastPx": {"value": f"{px:.4f}"}}}}


class TestTheTenderReadsTheRecord(Base):

    def entry(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        ask = self.mine(NC, "SELL")[0]
        self.assertFalse(self.f._exit_order(ask))
        return ask

    def shrink_unseen(self, order, qty, record=True):
        """The exchange fills part of an order; the feed has not moved,
        so the family's reconcile only shrinks the record."""
        live = self.r.exchange.live[order.id]
        live["size"] -= qty
        if live["size"] < 0.5:
            self.r.exchange.live.pop(order.id, None)
        if record:
            self.r.exchange.trades.append(
                trade_row(order.id, order.intent, order.price, qty, self.r.now))
        self.r.switch = False
        self.r.cycle(advance=1.0)            # the reconcile: no delta, no fill
        self.r.switch = True

    def booked(self, oid):
        return sum(float(r.get("qty") or 0.0) for r in self.r.fam.fills if r.get("oid") == oid)

    def test_a_partial_fill_the_feed_has_not_shown_is_booked_and_the_entry_is_not_regrown(self):
        # 05:25Z, Texas Senate dem: 42 of 90 filled, and ten seconds later
        # the tender resized the entry back up to 79
        ask = self.entry()
        full = ask.qty
        part = round(full * 0.45)
        self.shrink_unseen(ask, part)
        self.assertEqual(self.booked(ask.id), 0.0)      # the family could not see it
        self.tick()
        self.assertAlmostEqual(self.booked(ask.id), part, places=2)   # booked from the record
        self.assertTrue(any(e.get("event") == "record_fill" for e in self.f.log))
        self.assertIn(f"{NC}|SELL", self.f.filled_at)             # the side holds
        self.assertAlmostEqual(self.r.fam.inventory[NC]["qty"], -part, places=2)
        self.assertFalse(self.mine(NC, "SELL"))          # the rest came off, nothing re-grown
        self.assertFalse([e for e in self.f.log if e.get("event") == "moved"
                          and e.get("side") == "SELL"])
        # the feed catches up: nothing is booked twice
        self.r.positions[NC] = (-part, part * (1.0 - ask.price))
        self.r.switch = False
        self.r.cycle(advance=60.0)
        self.r.switch = True
        self.assertAlmostEqual(self.booked(ask.id), part, places=2)
        self.assertAlmostEqual(self.r.fam.inventory[NC]["qty"], -part, places=2)

    def test_a_whole_fill_the_feed_has_not_shown_is_booked_once(self):
        # the order left the book whole; the family put it in limbo
        ask = self.entry()
        full = ask.qty
        self.shrink_unseen(ask, full)
        self.assertIn(ask.id, self.r.fam.gone_pending)
        self.tick()
        self.assertAlmostEqual(self.booked(ask.id), full, places=2)
        self.assertNotIn(ask.id, self.r.fam.gone_pending)      # out of limbo
        # the feed's later move is not booked a second time
        self.r.positions[NC] = (-full, full * (1.0 - ask.price))
        self.r.switch = False
        self.r.cycle(advance=60.0)
        self.r.switch = True
        self.assertAlmostEqual(self.booked(ask.id), full, places=2)
        self.assertAlmostEqual(self.r.fam.inventory[NC]["qty"], -full, places=2)

    def test_an_exit_that_sold_unseen_is_not_rested_again_on_the_closed_position(self):
        # 04:17Z, House control rep: the exit of 9 sold unbooked and a
        # second exit rested on the closed position at 04:36Z
        self.r.positions[NC] = (200.0, 200.0 * 0.40)
        self.r.fam.positions_seen[NC] = 200.0
        self.r.fam.inventory[NC] = {"qty": 200.0, "cost": 80.0}
        self.f.set_fair(NC, 45.0)
        self.tick()
        ex = self.mine(NC, "SELL")[0]
        self.assertTrue(self.f._exit_order(ex))
        self.shrink_unseen(ex, ex.qty)
        exits = lambda: [o for o in self.mine(NC, "SELL") if self.f._exit_order(o)]
        for _ in range(12):                    # three minutes, past the pending window
            self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S / 4
            self.tick(advance=0.0)
            self.assertFalse(exits(), "an exit rested on a position already sold")
        self.assertAlmostEqual(self.booked(ex.id), 200.0, places=2)
        self.assertNotIn(NC, self.r.fam.inventory)

    def test_a_shrink_the_record_does_not_name_holds_the_side_and_places_nothing(self):
        # part 2: until the record or the journal explains it, the side
        # grows nothing — no resize, no move, no new entry
        ask = self.entry()
        full = ask.qty
        part = round(full * 0.45)
        self.shrink_unseen(ask, part, record=False)
        placed = len(self.r.exchange.live)
        seen = len(self.f.log)
        for _ in range(6):                    # six minutes, inside the hold
            self.f.moved_at.clear()           # the move cooldown is not what holds it
            self.tick(advance=60.0)
        cur = self.mine(NC, "SELL")
        self.assertEqual([o.id for o in cur], [ask.id])          # what rests stays
        self.assertAlmostEqual(cur[0].qty, full - part, places=2)  # not resized back up
        self.assertFalse([e for e in self.f.log[seen:] if e.get("event") in ("moved", "rested")
                          and e.get("side") == "SELL" and e.get("market") == NC])
        self.assertLessEqual(len(self.r.exchange.live), placed)
        # the record reads stop after FOCUS_RECORD_TRIES: the page cost is bounded
        self.assertLessEqual(self.r.exchange.trade_reads, focus_mod.FOCUS_RECORD_TRIES)
        # ten minutes on, the hold lifts: the side works again as before
        self.r.now += focus_mod.FOCUS_VANISH_WAIT_S
        self.f.moved_at.clear()
        self.tick()
        self.assertNotIn(f"{NC}|SELL", self.f.filled_at)

    def test_a_failed_record_read_books_nothing_and_says_why(self):
        ask = self.entry()
        self.shrink_unseen(ask, round(ask.qty * 0.45))
        self.r.exchange.trades_fail = True
        self.tick()
        self.assertEqual(self.booked(ask.id), 0.0)
        self.assertIn("ReadTimeout", self.f.record_note)
        self.r.exchange.trades_fail = False
        self.r.now += focus_mod.FOCUS_RECORD_EVERY_S
        self.tick()
        self.assertGreater(self.booked(ask.id), 0.0)
        self.assertEqual(self.f.record_note, "")

    def test_a_repeated_record_row_never_books_past_what_left_the_book(self):
        ask = self.entry()
        part = round(ask.qty * 0.45)
        self.shrink_unseen(ask, part)
        # the feed repeats the row under another execution id
        self.r.exchange.trades.append(trade_row(ask.id, ask.intent, ask.price, part,
                                                self.r.now, xid="dup"))
        self.tick()
        self.assertAlmostEqual(self.booked(ask.id), part, places=2)

    def test_the_record_is_read_only_when_an_order_shrank_or_left(self):
        self.entry()
        for _ in range(6):
            self.tick()
        self.assertEqual(getattr(self.r.exchange, "trade_reads", 0), 0)


class TestOneTradeIsBookedOnce(unittest.TestCase):
    """12:50Z, Texas governor rep: the hourly match added an execution of
    an order still on our books, and the family's reconcile booked the
    same trade seconds later. A fresh execution of an order the live
    paths still track is left to them; the tender's orders are ours."""

    def setUp(self):
        from v3.main import Monitor
        self.dir = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(self.dir.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        self.mon = Monitor()
        self.fam = self.mon.families["politics"]
        self.fam.universe[NC] = {"event_n": 2, "name": "North Carolina Senate"}
        self.now = time.time()

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def trade(self, oid, ago):
        return {"type": "ACTIVITY_TYPE_TRADE", "market": NC, "side": "SELL",
                "intent": "ORDER_INTENT_SELL_LONG", "price": 0.77, "shares": 83.0,
                "order_id": oid, "ts": self.now - ago, "placed_ts": self.now - ago - 600}

    def rec(self, oid):
        from v3.family import FamilyOrder
        from v3.intents import SELL_LONG
        return FamilyOrder(id=oid, market=NC, side="SELL", price=0.77, qty=83.0,
                           intent=SELL_LONG, placed_ts=self.now - 1200, purpose="sell",
                           why="an exit")

    def test_a_fresh_trade_of_an_order_still_on_our_books_is_left_to_the_live_paths(self):
        self.fam.orders["X1"] = self.rec("X1")
        r = self.mon.reconcile_journal([self.trade("X1", 480)], self.now)
        self.assertEqual(r.get("added", 0), 0)
        self.assertFalse([x for x in self.fam.fills if x.get("oid") == "X1"])

    def test_one_in_limbo_is_left_to_them_too_and_added_once_the_grace_is_out(self):
        self.fam.gone_pending["X2"] = {"rec": self.rec("X2"), "until": self.now + 300}
        self.mon.reconcile_journal([self.trade("X2", 480)], self.now)
        self.assertFalse([x for x in self.fam.fills if x.get("oid") == "X2"])
        from v3.family import RECORD_ADD_GRACE_S
        self.mon.reconcile_journal([self.trade("X2", 480)], self.now + RECORD_ADD_GRACE_S)
        rows = [x for x in self.fam.fills if x.get("oid") == "X2"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["purpose"], "backfill")        # ours, not his hand's

    def test_an_order_off_our_books_is_added_at_once(self):
        self.mon.reconcile_journal([self.trade("X3", 480)], self.now)
        rows = [x for x in self.fam.fills if x.get("oid") == "X3"]
        self.assertEqual(len(rows), 1)

    def test_the_tenders_order_is_ours_not_his_hand(self):
        # 23:49Z, Minnesota Senate dem: 115 @ 90c on the tender's own list
        # read as "your own trade"
        self.mon.focus.mine_ids.append("X4")
        self.mon.reconcile_journal([self.trade("X4", 2400)], self.now)
        rows = [x for x in self.fam.fills if x.get("oid") == "X4"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["purpose"], "backfill")


if __name__ == "__main__":
    unittest.main()
