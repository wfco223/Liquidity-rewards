"""The book on each move (owner, 2026-09-23 "Log the book on each move").

Covers flipped price every minute or two (Texas governor dem 15c<->29c,
Pennsylvania-01 rep 57c<->60c) and entries filled while the tender was
moving them. The saved state kept no books, so every placement, move,
pull and fill the tender logs now carries the book it was judged on:
its age and writer, the side raw and netted, every order of the
tender's there with its pass stamp and its real send moment against the
book's read, and the ghosts. Read-only — nothing about trading changes.
"""
import json
import unittest
from unittest import mock

from v3 import focus as focus_mod
from v3.scoring import Book
from v3.tests.test_focus import NC, OH, Base


class TestTheBookOnEachMove(Base):
    def rested(self):
        return [e for e in self.f.log if e.get("event") == "rested"]

    def test_a_placement_carries_the_book_it_was_judged_on(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        ev = [e for e in self.rested() if e["side"] == "BUY"]
        self.assertEqual(len(ev), 1)
        d = ev[0]["diag"]
        book = self.r.cache.any_age(NC)
        self.assertEqual(d["raw"][0], [book.bids[0][0], book.bids[0][1]])
        self.assertLessEqual(len(d["raw"]), focus_mod.DIAG_LEVELS)
        self.assertIn("net", d)
        self.assertEqual(d["src"], self.r.cache.last_writer.get(NC))
        self.assertIsNotNone(d["age"])
        self.assertIn("desk", ev[0])
        # the order just placed is on the side, with its stamps
        self.assertEqual(len(d["mine"]), 1)
        m = d["mine"][0]
        self.assertIn("placed_vs_read", m)
        self.assertIn("sent_vs_read", m)
        self.assertIn("netted", m)

    def test_the_netted_flag_is_the_nettings_own_test(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        o = self.mine(NC, "BUY")[0]
        # a book read after the order's stamp: the netting takes it as in the book
        b = self.r.cache.any_age(NC)
        self.r.cache.put(NC, Book(bids=b.bids, asks=b.asks, tick=b.tick,
                                  fetched_at=float(o.placed_ts) + 5.0))
        self.r.now = float(o.placed_ts) + 6.0
        m = self.f._diag(NC, "BUY")["mine"][0]
        self.assertTrue(m["netted"])
        self.assertAlmostEqual(m["placed_vs_read"], -5.0)
        # a book read before it: not in the book, not netted
        self.r.cache.put(NC, Book(bids=b.bids, asks=b.asks, tick=b.tick,
                                  fetched_at=float(o.placed_ts) - 5.0))
        m = self.f._diag(NC, "BUY")["mine"][0]
        self.assertFalse(m["netted"])
        self.assertAlmostEqual(m["placed_vs_read"], 5.0)

    def test_the_send_moment_is_the_clock_before_the_desk(self):
        self.f.set_fair(NC, 45.0)
        real = self.f.fam.desk.place_resting
        moments = []

        def place(*a, **k):
            moments.append(self.r.now)
            self.r.now += 3.0                 # the desk's verify takes a while
            return real(*a, **k)
        with mock.patch.object(self.f.fam.desk, "place_resting", side_effect=place):
            self.tick()
        o = self.mine(NC, "BUY")[0]
        self.assertIn(o.id, self.f._sent_at)
        self.assertIn(self.f._sent_at[o.id], moments)

    def test_a_move_says_why_and_what_the_resting_order_read(self):
        # the Texas governor dem shape of 2026-09-23: short 62, his fair
        # 15c, others' 70 at 29c; the rig's book never shows our own
        # order, which is the case the diag exists to catch
        shape = dict(bids=((0.29, 70.0), (0.14, 30.0), (0.01, 30000.0)),
                     asks=((0.31, 200.0), (0.99, 30000.0)), tick=0.01)
        self.r.exchange.books[OH] = Book(fetched_at=self.r.now, **shape)
        self.r.positions[OH] = (-62.0, -62.0 * 0.24)
        self.f.set_fair(OH, 15.0)
        for _ in range(12):
            self.tick()
            if any(e.get("event") == "moved" for e in self.f.log):
                break
        mv = [e for e in self.f.log if e.get("event") == "moved"]
        self.assertTrue(mv, [e.get("event") for e in self.f.log])
        e = mv[-1]
        self.assertTrue(e["exit"])
        self.assertTrue(e["why"])
        self.assertIsNotNone(e["prev"])        # the resting order's own reading
        self.assertIn("desk", e)
        self.assertIn("two", e)
        d = e["diag"]
        self.assertEqual(d["raw"][0], [0.29, 70.0])
        self.assertTrue(any(abs(g[0] - e["was"]) < 1e-9 for g in d["ghosts"]))
        self.assertEqual(len(d["mine"]), 1)
        self.assertAlmostEqual(d["mine"][0]["px"], e["price"])
        # the order it replaced, against the book it was judged on
        self.assertAlmostEqual(e["was_d"]["px"], e["was"])
        self.assertIn("netted", e["was_d"])

    def test_the_page_gets_the_lines_not_the_books(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        page = json.loads(self.f.payload_json)
        self.assertTrue(page["log"])
        self.assertFalse(any("diag" in e for e in page["log"]))
        # the saved state keeps them for the checks
        self.assertTrue(any("diag" in e for e in self.f.to_dict()["log"]))

    def test_a_diag_that_fails_never_breaks_the_pass(self):
        self.f.set_fair(NC, 45.0)
        with mock.patch.object(self.f, "_diag", side_effect=RuntimeError("boom")):
            self.tick()
        ev = [e for e in self.rested() if e["side"] == "BUY"]
        self.assertEqual(len(ev), 1)
        self.assertIn("boom", ev[0]["diag"]["error"])
        self.assertEqual(len(self.mine(NC, "BUY")), 1)

    def test_nothing_about_trading_changes(self):
        # the same passes with the diag stubbed out rest the same orders
        self.f.set_fair(NC, 45.0)
        for _ in range(4):
            self.tick()
        with_diag = sorted((o.side, o.price, o.qty) for o in self.mine(NC))
        self.setUp()
        self.f.set_fair(NC, 45.0)
        with mock.patch.object(self.f, "_diag", return_value={}):
            for _ in range(4):
                self.tick()
        without = sorted((o.side, o.price, o.qty) for o in self.mine(NC))
        self.assertEqual(with_diag, without)


if __name__ == "__main__":
    unittest.main()
