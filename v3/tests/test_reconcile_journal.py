"""The journal agrees with the exchange (owner, 2026-09-02: "every so
often, just pull the new transactions and match them up... the goal is
to accurately portray fills").

The Alabama case: the owner sold 40 shares by hand at 90c at 16:45 ET.
The engine's 98c ask vanished in the same minute as the position, and
reconcile booked "sold 40 @ 98c" — a fill that never happened. The
exchange's own record has three executions at 90c under order ids the
engine never placed, and no trade at all for the 98c ask.
"""

import os
import tempfile
import time
import unittest

from v3.main import Monitor, pair_fills

AL = "usgubewc-usgub-al-2026-11-03-rep"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(self.dir.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        self.mon = Monitor()
        self.fam = self.mon.families["politics"]
        self.fam.universe[AL] = {"event_n": 1, "name": "Alabama governor"}
        self.now = time.time()
        self.t_sale = self.now - 2 * 3600          # the hand sale, 2h ago

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def journal(self, oid, side, qty, px, ago_s, purpose="earn", why="x"):
        row = {"ts": round(self.now - ago_s, 1), "market": AL, "side": side,
               "qty": qty, "px": px, "oid": oid, "purpose": purpose,
               "why": why, "est_day": None, "rested_h": None, "fair": None,
               "band": None, "conf": None, "touch_bid": None,
               "touch_ask": None, "conc": None, "pos_after": None}
        self.fam.fills.append(row)
        return row

    @staticmethod
    def trade(oid, side, intent, px, shares, ts, market=AL):
        return {"type": "ACTIVITY_TYPE_TRADE", "market": market,
                "side": side, "intent": f"ORDER_INTENT_{intent}",
                "price": px, "shares": shares, "order_id": oid, "ts": ts,
                "placed_ts": ts - 600.0}

    def alabama(self):
        """The journal as the engine wrote it, and the exchange's record."""
        t_a, t_b = self.now - 50 * 3600, self.now - 7 * 3600
        self.fam.placed_at.update({"A": t_a - 3600, "B": t_b - 3600,
                                   "C": self.t_sale - 9000})
        self.journal("A", "BUY", 20.0, 0.93, 50 * 3600)
        self.journal("B", "BUY", 20.0, 0.92, 7 * 3600)
        self.journal("C", "SELL", 40.0, 0.98, 2 * 3600 - 20, purpose="sell",
                     why="selling filled stock — it earns while it waits")
        # the position feed showed 40 -> 0 in the sale's minute
        self.fam.pos_moves.append([round(self.t_sale + 19, 1), AL, -40.0])
        self.fam.positions_seen[AL] = 0.0
        rows = [self.trade("A", "BUY", "BUY_LONG", 0.93, 20, t_a),
                self.trade("B", "BUY", "BUY_LONG", 0.92, 20, t_b),
                # the exchange's intent labels on a hand sale point
                # every way; the position move says SELL
                self.trade("H1", "BUY", "BUY_LONG", 0.90, 13.42, self.t_sale),
                self.trade("H2", "BUY", "SELL_SHORT", 0.90, 21.41, self.t_sale),
                self.trade("H3", "SELL", "SELL_LONG", 0.90, 5.47, self.t_sale)]
        return rows


class TestAlabama(Base):
    def test_the_phantom_sale_is_voided_and_the_hand_sale_added(self):
        rows = self.alabama()
        r = self.mon.reconcile_journal(rows, self.now)
        self.assertEqual((r["fixed"], r["voided"], r["added"]), (0, 1, 3))
        oids = [x["oid"] for x in self.fam.fills]
        self.assertNotIn("C", oids)                       # the 98c fiction
        hand = [x for x in self.fam.fills if x["purpose"] == "hand"]
        self.assertEqual(len(hand), 3)
        self.assertTrue(all(x["side"] == "SELL" and x["px"] == 0.9
                            for x in hand))
        self.assertAlmostEqual(sum(x["qty"] for x in hand), 40.3, places=2)
        self.assertIn("position move", hand[0]["why"])
        # the cards now say what happened: 40 bought at 92.5c average,
        # sold at 90c by hand — a dollar lost on the lots, not $2.20
        # made. The exchange's extra 0.3 shares close stock the journal
        # never saw bought, as a stray sliver.
        cards = pair_fills(self.fam.fills)
        lots = [c for c in cards if c["side"] == "BUY"]
        realized = sum(c["realized"] for c in lots)
        self.assertAlmostEqual(realized, -1.0, places=2)
        sliver = [c for c in cards if c.get("stray_close")]
        self.assertEqual(len(sliver), 1)
        self.assertEqual(sliver[0]["purpose"], "hand")
        self.assertEqual(sliver[0]["open_qty"], 0.0)
        self.assertTrue(all(cl["kind"] == "hand"
                            for c in lots for cl in c["closes"]))
        # the archive gets dated correction lines, never a rewrite
        kinds = [row["purpose"] for _, _, row in self.mon.journal_fixes]
        self.assertEqual(kinds.count("void"), 1)
        self.assertEqual(kinds.count("hand"), 3)
        self.assertTrue(all(ts == self.now for ts, _, _ in self.mon.journal_fixes))

    def test_running_it_again_changes_nothing(self):
        rows = self.alabama()
        self.mon.reconcile_journal(rows, self.now)
        before = [dict(x) for x in self.fam.fills]
        r = self.mon.reconcile_journal(rows, self.now + 3600)
        self.assertEqual((r["fixed"], r["voided"], r["added"]), (0, 0, 0))
        self.assertEqual(self.fam.fills, before)


class TestCorrections(Base):
    def test_a_price_the_exchange_shows_differently_is_taken(self):
        self.fam.placed_at["A"] = self.now - 9000
        row = self.journal("A", "BUY", 20.0, 0.93, 7200)
        self.fam.inventory[AL] = {"qty": 20.0, "cost": 18.6}
        r = self.mon.reconcile_journal(
            [self.trade("A", "BUY", "BUY_LONG", 0.92, 20, self.now - 7300)],
            self.now)
        self.assertEqual(r["fixed"], 1)
        self.assertEqual(row["px"], 0.92)
        self.assertIn("corrected to 92c", row["why"])
        self.assertAlmostEqual(self.fam.inventory[AL]["cost"], 18.4)  # held lot
        fix = [row for _, _, row in self.mon.journal_fixes][0]
        self.assertEqual(fix["purpose"], "fix")

    def test_a_size_the_exchange_shows_smaller_is_trimmed(self):
        self.fam.placed_at["A"] = self.now - 9000
        row = self.journal("A", "BUY", 20.0, 0.93, 7200)
        r = self.mon.reconcile_journal(
            [self.trade("A", "BUY", "BUY_LONG", 0.93, 12, self.now - 7300)],
            self.now)
        self.assertEqual(r["fixed"], 1)
        self.assertEqual(row["qty"], 12.0)

    def test_a_missed_engine_fill_comes_back_as_backfill(self):
        self.fam.placed_at["A"] = self.now - 9000
        r = self.mon.reconcile_journal(
            [self.trade("A", "BUY", "BUY_LONG", 0.93, 20, self.now - 7300)],
            self.now)
        self.assertEqual(r["added"], 1)
        self.assertEqual(self.fam.fills[0]["purpose"], "backfill")
        self.assertEqual(self.fam.fills[0]["side"], "BUY")


class TestRestraint(Base):
    """What it must NOT do: judge a fresh fill whose trade has not
    reached the feed yet, or a row from before the pull's coverage."""

    def test_a_fresh_fill_is_left_alone(self):
        self.journal("F", "BUY", 5.0, 0.5, 300)               # 5 min old
        r = self.mon.reconcile_journal(
            [self.trade("Z", "BUY", "BUY_LONG", 0.4, 1, self.now - 7000,
                        market="other-mkt")], self.now)
        self.assertEqual(r["voided"], 0)
        self.assertEqual(len(self.fam.fills), 1)

    def test_a_row_before_the_pulls_coverage_is_left_alone(self):
        self.journal("OLD", "BUY", 5.0, 0.5, 30 * 3600)       # 30h old
        r = self.mon.reconcile_journal(
            [self.trade("Z", "BUY", "BUY_LONG", 0.4, 1, self.now - 7000,
                        market="other-mkt")], self.now)         # pull covers ~2h
        self.assertEqual(r["voided"], 0)

    def test_a_row_at_the_pulls_edge_is_left_alone(self):
        oldest = self.now - 7000
        self.journal("EDGE", "BUY", 5.0, 0.5, 7000 - 30)      # 30s after oldest
        r = self.mon.reconcile_journal(
            [self.trade("Z", "BUY", "BUY_LONG", 0.4, 1, oldest,
                        market="other-mkt")], self.now)
        self.assertEqual(r["voided"], 0)

    def test_an_unknown_market_is_not_ours(self):
        r = self.mon.reconcile_journal(
            [self.trade("Q", "BUY", "BUY_LONG", 0.4, 1, self.now - 7000,
                        market="not-a-market-we-know")], self.now)
        self.assertEqual(r["added"], 0)

    def test_no_position_move_falls_back_to_the_order_intent(self):
        self.fam.positions_seen[AL] = 0.0
        r = self.mon.reconcile_journal(
            [self.trade("H", "SELL", "SELL_LONG", 0.9, 3, self.now - 7000)],
            self.now)
        self.assertEqual(r["added"], 1)
        self.assertEqual(self.fam.fills[0]["side"], "SELL")
        self.assertIn("order intent", self.fam.fills[0]["why"])


class TestHandCloseCards(unittest.TestCase):
    def test_a_hand_sale_with_no_lot_is_a_stray_close_not_a_short(self):
        fills = [{"ts": 1.0, "market": AL, "side": "SELL", "qty": 5.0,
                  "px": 0.9, "purpose": "hand",
                  "intent": "ORDER_INTENT_SELL_LONG"}]
        cards = pair_fills(fills)
        self.assertTrue(cards[0].get("stray_close"))

    def test_a_hand_buy_opens_a_lot(self):
        fills = [{"ts": 1.0, "market": AL, "side": "BUY", "qty": 5.0,
                  "px": 0.9, "purpose": "hand",
                  "intent": "ORDER_INTENT_BUY_LONG"}]
        cards = pair_fills(fills)
        self.assertFalse(cards[0].get("stray_close"))
        self.assertEqual(cards[0]["open_qty"], 5.0)


if __name__ == "__main__":
    unittest.main()
