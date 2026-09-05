"""The reconciler adds only what the journal will keep (2026-09-05: the
journal is bounded at FILLS_KEEP rows and a week; old executions added
past that fell off at the next fill and were re-added every hour — the
same 400 trades, 800 archive rows a time)."""

import os
import tempfile
import time
import unittest

from v3.family import FILLS_KEEP
from v3.main import Monitor

AL = "usgubewc-usgub-al-2026-11-03-rep"
TN = "usgubewc-usgub-tn-2026-11-03-rep"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(self.dir.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"
        self.mon = Monitor()
        self.fam = self.mon.families["politics"]
        for s in (AL, TN):
            self.fam.universe[s] = {"event_n": 1, "name": s}
        self.now = time.time()

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    def journal(self, oid, ts, market=AL, qty=1.0, px=0.5):
        row = {"ts": round(ts, 1), "market": market, "side": "BUY", "qty": qty,
               "px": px, "oid": oid, "purpose": "earn", "why": "x",
               "est_day": None, "rested_h": None, "fair": None, "band": None,
               "conf": None, "touch_bid": None, "touch_ask": None, "conc": None,
               "pos_after": None}
        self.fam.fills.append(row)
        return row

    @staticmethod
    def trade(oid, ts, market=AL, qty=1.0, px=0.5):
        return {"type": "ACTIVITY_TYPE_TRADE", "market": market, "side": "BUY",
                "intent": "ORDER_INTENT_BUY_LONG", "price": px, "shares": qty,
                "order_id": oid, "ts": ts, "placed_ts": ts - 600.0}


class TestWhatTheJournalKeeps(Base):
    def test_an_old_trade_in_a_market_no_longer_held_is_not_added(self):
        rows = [self.trade("OLD", self.now - 8 * 86400.0, market=TN),
                self.trade("NEW", self.now - 3600.0, market=TN)]
        r = self.mon.reconcile_journal(rows, self.now)
        self.assertEqual(r["added"], 1)
        self.assertEqual([x["oid"] for x in self.fam.fills], ["NEW"])

    def test_an_old_trade_in_a_held_market_is_added_while_there_is_room(self):
        self.fam.inventory[TN] = {"qty": 5.0, "cost": 2.5}
        rows = [self.trade("OLD", self.now - 8 * 86400.0, market=TN)]
        r = self.mon.reconcile_journal(rows, self.now)
        self.assertEqual(r["added"], 1)

    def test_nothing_older_than_the_journal_holds_is_added_at_the_bound(self):
        # a full journal of the last six days, every row with its trade
        rows = []
        for i in range(FILLS_KEEP):
            ts = self.now - 6 * 86400.0 + i * 800.0
            self.journal(f"J{i}", ts)
            rows.append(self.trade(f"J{i}", ts))
        oldest = min(float(x["ts"]) for x in self.fam.fills)
        rows.append(self.trade("OLDER", oldest - 3600.0))        # before the oldest kept row
        rows.append(self.trade("FRESH", self.now - 3600.0))
        r = self.mon.reconcile_journal(rows, self.now)
        self.assertEqual(r["added"], 1)
        oids = {x["oid"] for x in self.fam.fills}
        self.assertIn("FRESH", oids)
        self.assertNotIn("OLDER", oids)
        # and a second pass adds nothing: the loop is closed
        r2 = self.mon.reconcile_journal(rows, self.now + 3600.0)
        self.assertEqual(r2["added"], 0)


if __name__ == "__main__":
    unittest.main()
