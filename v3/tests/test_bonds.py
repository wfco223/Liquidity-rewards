"""Bonds: the tax reserve earning like a bond (owner, 2026-09-02).

The spec and its corrections, in bonds.py's docstring. Here: the
nightly Silver check proposes at both ends and drops silently; the
owner alone adds; the engine keeps quoting bond markets but exits only
its own non-bond stock; the bond ledger is the module's own; a held
bond's order sits behind the touch keeping 60% of the best reward and
never under cost; a minnow in front is led down by a decoy and taken
at its price; a hand sale is never reinvested; one ping per $100 bought;
nothing is bought where no bond sale of ours rests.
"""

import calendar
import copy
import unittest

from v3.bonds import (DANCE_MAX_MOVES, DANCE_WAIT_S, DECOY_QTY, HIGH_ODDS,
                      KEEP_FRACTION, LOW_ODDS, MINNOW_MAX, PING_EVERY_USD,
                      Bonds, scan_due, side_for)
from v3.family import FamilyConfig, FamilyOrder
from v3.intents import BUY_LONG, BUY_SHORT, SELL_LONG, SELL_SHORT
from v3.scoring import Book
from v3.tests.test_family import LIVE_PROG, Rig

AL = "usgubewc-usgub-al-2026-11-03-rep"     # Silver 99.4 for YES: a YES bond
TN = "usgubewc-usgub-tn-2026-11-03-rep"     # Silver 99.1
ALD = "usgubewc-usgub-al-2026-11-03-dem"    # Silver 0.6 for YES: a NO bond
GA = "usgubewc-usgub-ga-2026-11-03-rep"     # Silver 71: not a bond


def yes_book(now, bid=0.98, ask=0.99, bid_q=50.0, ask_q=300.0):
    return Book(bids=((bid, bid_q), (0.50, 20000.0)),
                asks=((ask, ask_q), (0.999, 20000.0)),
                tick=0.01, fetched_at=now)


def no_book(now, bid=0.01, ask=0.02, bid_q=400.0, ask_q=60.0):
    return Book(bids=((bid, bid_q), (0.001, 20000.0)),
                asks=((ask, ask_q), (0.60, 20000.0)),
                tick=0.01, fetched_at=now)


def minnow_book(now, minnows=5.0, ask=0.90, extra=()):
    # a few rewards-seeking shares at the touch, the qualifying wall
    # nine ticks back at 99c where it barely weighs
    asks = tuple(sorted(((ask, minnows),) + tuple(extra) + ((0.99, 20000.0),)))
    return Book(bids=((0.88, 50.0), (0.50, 20000.0)), asks=asks,
                tick=0.01, fetched_at=now)


class Base(unittest.TestCase):
    def setUp(self):
        self.r = Rig(cfg=FamilyConfig(name="Politics", tag="POL",
                                      known_ground=True, capital_usd=0.0))
        self.odds = {AL: 0.994, TN: 0.991, ALD: 0.006, GA: 0.71}
        self.pings = []
        self.b = Bonds(self.r.fam, self.r.exchange,
                       lambda s: self.odds.get(s), clock=lambda: self.r.now,
                       alert=lambda t, m: self.pings.append((t, m)),
                       sleep=lambda s: None)
        for s in (AL, TN, GA):
            self.seed(s, yes_book(self.r.now))
        self.seed(ALD, no_book(self.r.now))
        self.r.fam.refresh_terms(self.r.exchange, self.r.now)
        self.now = self.r.now

    def seed(self, slug, book):
        self.r.fam.universe[slug] = {"event_n": 1, "name": slug}
        self.r.exchange.books[slug] = book
        self.r.cache.put(slug, book)
        self.r.exchange.prog_raw[slug] = copy.deepcopy(LIVE_PROG)

    def exch(self, slug, qty, yes_px):
        """The exchange's position: qty > 0 long YES, < 0 short YES."""
        self.r.positions[slug] = (qty, qty * yes_px)
        self.r.fam.inventory[slug] = {"qty": qty, "cost": qty * yes_px}

    def bond(self, slug, side, qty, yes_px):
        """A bond in the ledger AND on the exchange."""
        self.exch(slug, qty if side == "YES" else -qty, yes_px)
        cost = qty * (yes_px if side == "YES" else 1.0 - yes_px)
        self.b._book_lot(slug, side, qty, cost, ref="test")   # backed, as a real fill is

    def positions(self):
        return dict(self.r.positions)

    @staticmethod
    def trade_row(oid, intent, px, shares, ts, market=AL, commission=0.0):
        """One execution of ours in the exchange's activity shape."""
        import time as _t
        iso = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(ts))
        return {"type": "ACTIVITY_TYPE_TRADE", "trade": {
            "id": f"t{oid}", "marketSlug": market, "updateTime": iso,
            "aggressorExecution": {
                "id": f"x{oid}", "transactTime": iso,
                "order": {"id": oid, "intent": intent, "quantity": shares,
                          "cumQuantity": shares, "createTime": iso,
                          "avgPx": {"value": f"{px:.4f}"},
                          "price": {"value": f"{px:.4f}"}},
                "lastShares": f"{shares:.4f}",
                "lastPx": {"value": f"{px:.4f}"},
                "commissionNotionalCollected": {"value": f"{commission:.4f}"}}}}

    def orders(self, slug, side=None, decoy=None):
        out = []
        for o in self.r.fam.orders.values():
            if o.market != slug or (side is not None and o.side != side):
                continue
            is_decoy = str(o.why or "").startswith("bond decoy")
            if decoy is not None and is_decoy != decoy:
                continue
            out.append(o)
        return out


class TestTheBand(unittest.TestCase):
    def test_both_ends_are_bonds(self):
        self.assertEqual(side_for(0.994), "YES")
        self.assertEqual(side_for(HIGH_ODDS), "YES")
        self.assertEqual(side_for(0.006), "NO")
        self.assertEqual(side_for(LOW_ODDS), "NO")
        self.assertIsNone(side_for(0.985))
        self.assertIsNone(side_for(None))

    def test_the_check_is_nightly(self):
        t = calendar.timegm((2026, 9, 3, 6, 59, 0, 0, 0, 0))
        self.assertIsNone(scan_due(t, ""))
        self.assertEqual(scan_due(t + 120, ""), "2026-09-03")
        self.assertIsNone(scan_due(t + 120, "2026-09-03"))
        self.assertEqual(scan_due(t + 86400 + 120, "2026-09-03"), "2026-09-04")

    def test_the_owners_numbers(self):
        self.assertEqual(KEEP_FRACTION, 0.6)
        self.assertEqual(PING_EVERY_USD, 100.0)
        self.assertEqual(DECOY_QTY, 10.0)
        self.assertEqual(MINNOW_MAX, 25.0)
        self.assertEqual(DANCE_WAIT_S, 7200.0)
        self.assertEqual(DANCE_MAX_MOVES, 3)


class TestTheList(Base):
    def test_silver_proposes_both_ends_and_only_the_owner_adds(self):
        new = self.b.scan(self.now, force=True)
        self.assertEqual(set(new), {AL, TN, ALD})
        self.assertEqual(self.b.proposed[ALD]["side"], "NO")
        self.assertEqual(self.b.approved, {})
        self.assertTrue(self.b.approve(AL, self.now)["ok"])
        self.assertTrue(self.b.approve(ALD, self.now)["ok"])
        self.assertEqual(self.b.approved[ALD]["side"], "NO")
        self.assertEqual(self.pings, [])                       # silent

    def test_proposals_sit_newest_first(self):
        self.b.scan(self.now, force=True)
        self.odds[GA] = 0.995
        self.b.scan(self.now + 3600, force=True)
        self.assertEqual(self.b.view(self.now + 3600)["proposed"][0]["market"], GA)

    def test_inside_the_band_cannot_be_added_even_by_hand(self):
        r = self.b.approve(GA, self.now)
        self.assertFalse(r["ok"])
        self.assertIn("71.0%", r["note"])

    def test_a_listed_market_that_leaves_the_band_drops_by_itself(self):
        self.b.approve(AL, self.now)
        self.bond(AL, "YES", 100.0, 0.98)
        self.odds[AL] = 0.984
        self.b.scan(self.now + 1, force=True)
        self.assertNotIn(AL, self.b.approved)
        v = self.b.view(self.now + 1, self.positions())
        self.assertEqual(v["dropped"][0]["market"], AL)
        self.assertEqual(v["dropped"][0]["held"], 100.0)
        self.assertEqual(self.pings, [])

    def test_the_engine_quotes_a_listed_market_until_he_holds_a_bond_there(self):
        self.b.approve(AL, self.now)
        self.assertFalse(self.r.fam._frozen(AL))
        self.assertTrue(self.r.fam.enterable(AL))
        self.bond(AL, "YES", 100.0, 0.98)
        self.assertEqual(self.r.fam.bond_qty, {AL: 100.0})   # what _sell subtracts
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertTrue(self.r.fam._frozen(AL))                # from the first purchase on

    def test_ignore_is_remembered_and_reversible(self):
        self.b.scan(self.now, force=True)
        self.b.ignore(TN, self.now)
        self.assertNotIn(TN, self.b.scan(self.now + 1, force=True))
        self.b.unignore(TN)
        self.assertIn(TN, self.b.scan(self.now + 2, force=True))

    def test_the_scan_runs_once_a_night_on_its_own(self):
        t = calendar.timegm((2026, 9, 3, 7, 5, 0, 0, 0, 0))
        self.assertEqual(set(self.b.scan(t)), {AL, TN, ALD})
        self.odds[GA] = 0.995
        self.assertEqual(self.b.scan(t + 3600), [])
        self.assertIn(GA, self.b.scan(t + 86400))


class TestTheLedger(Base):
    def test_only_bond_purchases_count_as_held(self):
        self.b.approve(AL, self.now)
        self.exch(AL, 500.0, 0.92)               # the engine's own stock
        self.assertEqual(self.b.held(AL, "YES"), 0.0)
        v = self.b.view(self.now, self.positions())
        row = v["rows"][0]
        self.assertEqual(row["qty"], 0.0)
        self.assertEqual(row["uncounted"], 500.0)
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertEqual(self.orders(AL, "SELL"), [])        # no order on engine stock

    def test_the_owner_can_count_his_shares_in(self):
        self.b.approve(AL, self.now)
        self.exch(AL, 500.0, 0.92)
        r = self.b.adopt(AL, 300.0, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertEqual(self.b.held(AL, "YES"), 300.0)
        self.assertAlmostEqual(self.b.cost_basis(AL, "YES"), 0.92)
        self.assertFalse(self.b.adopt(AL, 300.0, self.positions())["ok"])  # only 200 left

    def test_the_order_never_exceeds_what_the_exchange_shows(self):
        self.b.approve(AL, self.now)
        self.b._book_lot(AL, "YES", 400.0, 400 * 0.98)   # ledger says 400
        self.exch(AL, 250.0, 0.98)                       # exchange says 250
        self.b.cycle(self.now, self.positions(), on=True)
        ask = self.orders(AL, "SELL")[0]
        self.assertEqual(ask.qty, 250.0)                 # never more than the exchange shows
        self.assertEqual(ask.qty, self.b.slot[AL]["size"])


class TestThePage(Base):
    def test_rows_are_cheapest_per_dollar_first(self):
        for s in (AL, TN, ALD):
            self.b.approve(s, self.now)
        self.r.cache.put(TN, yes_book(self.now, bid=0.97, ask=0.98))
        v = self.b.view(self.now)
        self.assertEqual([r["market"] for r in v["rows"]], [TN, AL, ALD])
        tn = v["rows"][0]
        self.assertEqual(tn["cost"], 0.98)
        self.assertAlmostEqual(tn["yield"], (1 - 0.98) / 0.98, places=4)
        self.assertIsNotNone(tn["earn"])
        self.assertEqual(v["keep"], 0.6)


class TestTheRestingOrder(Base):
    def test_switch_off_places_nothing(self):
        self.b.approve(AL, self.now)
        self.bond(AL, "YES", 1500.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=False)
        self.assertEqual(self.r.exchange.live, {})

    def test_a_big_lot_sits_behind_the_touch_keeping_most_of_the_reward(self):
        self.b.approve(AL, self.now)
        self.r.cache.put(AL, minnow_book(self.now))
        self.bond(AL, "YES", 1500.0, 0.89)
        out = self.b.cycle(self.now, self.positions(), on=True)
        ask = self.orders(AL, "SELL", decoy=False)[0]
        self.assertEqual((ask.purpose, ask.intent), ("bond", SELL_LONG))
        # at 60% kept it sits three ticks back (0.2^3 x 1500 = 12 vs 5),
        # with the whole lot (owner, 2026-09-03: "You don't have to
        # reserve any shares to maturity")
        self.assertAlmostEqual(ask.price, 0.93)
        self.assertEqual(out["placed"][0]["ticks"], 3)
        self.assertGreaterEqual(self.b.slot[AL]["keep"], 0.6)
        self.assertEqual(ask.qty, 1500.0)
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(len(self.orders(AL, "SELL", decoy=False)), 1)   # one exit

    def test_never_under_cost(self):
        self.b.approve(AL, self.now)
        self.r.cache.put(AL, minnow_book(self.now))
        self.bond(AL, "YES", 1500.0, 0.945)
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertGreaterEqual(self.orders(AL, "SELL")[0].price, 0.95 - 1e-9)

    def test_a_held_no_bond_gets_a_cover_bid_never_above_what_it_sold_at(self):
        self.b.approve(ALD, self.now)
        self.bond(ALD, "NO", 1000.0, 0.01)
        self.b.cycle(self.now, self.positions(), on=True)
        bids = self.orders(ALD, "BUY")
        self.assertEqual(len(bids), 1)
        self.assertEqual((bids[0].purpose, bids[0].intent), ("bond", SELL_SHORT))
        self.assertLessEqual(bids[0].price, 0.01 + 1e-9)
        self.assertEqual(bids[0].qty, 1000.0)
        self.assertEqual(bids[0].qty, self.b.slot[ALD]["size"])


class TestTheSniper(Base):
    """Owner: "purchasing anything that is in the way of our sell orders
    collecting rewards"; then: "the decoy joins the minnow instead of
    beating it. Each time the decoy moves wait 2 hours to see if the
    minnow will move again, then, if not kill the decoy and snap up the
    minnow. If the minnow moves more than 3 times or reaches the touch
    or below cost, snap it up immediately." Minnows are 25 shares or
    fewer."""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)
        self.r.cache.put(AL, minnow_book(self.now, minnows=0.0, ask=0.99))
        self.bond(AL, "YES", 1500.0, 0.90)
        self.b.more_cap[AL] = {"usd": 0.0, "by": "owner", "first": ""}   # no buy-more here
        self.b.cycle(self.now, self.positions(), on=True)   # rests the main ask
        self.main = self.orders(AL, "SELL", decoy=False)[0]

    def book_with_minnow(self, px, q=20.0, t=0.0, bid=0.88):
        # our main ask (and any decoy) stay on the book; a minnow sits in front
        mine = [(o.price, o.qty) for o in self.orders(AL, "SELL")]
        asks = [(px, q)] + mine + [(0.99, 20000.0)]
        merged = {}
        for p, qq in asks:
            merged[round(p, 4)] = merged.get(round(p, 4), 0.0) + qq
        d = Book(bids=((bid, 50.0), (0.50, 20000.0)),
                 asks=tuple(sorted((p, qq) for p, qq in merged.items() if qq > 0)),
                 tick=0.01, fetched_at=self.now + t)
        self.r.cache.put(AL, d)
        self.r.exchange.books[AL] = d        # a re-read after a clear sees it too
        return d

    def cyc(self, t):
        return self.b.cycle(self.now + t, self.positions(), on=True)

    def test_the_decoy_joins_the_minnow_at_its_own_price(self):
        m_px = round(self.main.price - 0.01, 2)
        self.book_with_minnow(m_px, 20.0, 60)
        out = self.cyc(60)
        decoys = self.orders(AL, "SELL", decoy=True)
        self.assertEqual(len(decoys), 1)
        self.assertAlmostEqual(decoys[0].price, m_px)          # joins, not beats
        self.assertEqual(decoys[0].qty, DECOY_QTY)
        self.assertTrue(out["placed"][0]["decoy"])
        self.assertEqual(self.b.dance[AL]["moves"], 0)
        self.assertIn(self.main.id, self.r.fam.orders)
        v = self.b.view(self.now + 60, self.positions())
        self.assertEqual(v["rows"][0]["dance"]["px"], m_px)

    def test_a_minnow_that_stays_put_for_two_hours_is_taken(self):
        m_px = round(self.main.price - 0.01, 2)
        self.book_with_minnow(m_px, 20.0, 60)
        self.cyc(60)
        self.book_with_minnow(m_px, 20.0, 60 + 3600)          # still there
        self.cyc(60 + 3600)
        self.assertEqual([o for o in self.r.fam.orders.values() if o.side == "BUY"], [])
        self.book_with_minnow(m_px, 20.0, 60 + 7200 + 5)      # two hours on
        out = self.cyc(60 + 7200 + 5)
        takes = [o for o in self.r.fam.orders.values() if o.side == "BUY"]
        self.assertEqual(len(takes), 1)
        self.assertAlmostEqual(takes[0].price, m_px)           # at ITS price
        self.assertEqual(takes[0].qty, 20.0)
        self.assertTrue(out["placed"][0]["taken"])
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])   # decoy killed
        self.assertNotIn(AL, self.b.dance)
        self.assertEqual(self.b.held(AL, "YES"), 1520.0)

    def test_each_move_restarts_the_clock_and_the_decoy_follows(self):
        m1 = round(self.main.price - 0.01, 2)
        self.book_with_minnow(m1, 20.0, 60)
        self.cyc(60)
        m2 = round(m1 - 0.01, 2)
        self.book_with_minnow(m2, 20.0, 60 + 5400)              # moved at 1.5h
        self.cyc(60 + 5400)
        d = self.orders(AL, "SELL", decoy=True)
        self.assertEqual(len(d), 1)
        self.assertAlmostEqual(d[0].price, m2)
        self.assertEqual(self.b.dance[AL]["moves"], 1)
        # 1.5h after the FIRST join is not two hours after the second
        self.book_with_minnow(m2, 20.0, 60 + 7300)
        self.cyc(60 + 7300)
        self.assertEqual([o for o in self.r.fam.orders.values() if o.side == "BUY"], [])

    def test_a_fourth_move_is_taken_at_once(self):
        px = round(self.main.price - 0.01, 2)
        t = 60
        for i in range(4):                                     # join, then 3 moves
            self.book_with_minnow(round(px - 0.01 * i, 2), 20.0, t)
            self.cyc(t)
            t += 600
        self.assertEqual(self.b.dance[AL]["moves"], 3)
        self.assertEqual([o for o in self.r.fam.orders.values() if o.side == "BUY"], [])
        self.book_with_minnow(round(px - 0.04, 2), 20.0, t)   # the fourth move
        self.cyc(t)
        takes = [o for o in self.r.fam.orders.values() if o.side == "BUY"]
        self.assertEqual(len(takes), 1)
        self.assertAlmostEqual(takes[0].price, round(px - 0.04, 2))

    def test_reaching_the_far_touch_is_taken_at_once(self):
        # a minnow one tick over the best bid has nowhere left to go
        self.book_with_minnow(0.95, 20.0, 60, bid=0.94)
        self.cyc(60)
        takes = [o for o in self.r.fam.orders.values() if o.side == "BUY"]
        self.assertEqual(len(takes), 1)
        self.assertAlmostEqual(takes[0].price, 0.95)

    def test_under_our_cost_is_taken_at_once_and_never_joined(self):
        self.book_with_minnow(0.89, 20.0, 60)                  # cost is 90c
        self.cyc(60)
        takes = [o for o in self.r.fam.orders.values() if o.side == "BUY"]
        self.assertEqual(len(takes), 1)
        self.assertAlmostEqual(takes[0].price, 0.89)
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])

    def test_a_note_only_after_a_hundred_dollars(self):
        for i in range(6):
            self.book_with_minnow(0.89, 20.0, 60 * (i + 1))
            self.cyc(60 * (i + 1))
        self.assertEqual(len(self.pings), 1)                   # 6 x $17.80 = $106.80
        self.assertIn("$106.80", self.pings[0][1])
        self.assertAlmostEqual(self.b.unpinged, 0.0)

    def test_a_decoy_holds_until_nothing_foreign_shows_for_five_minutes(self):
        # owner, 2026-09-03: "if someone is placing and immediately
        # removing their small order, it will never happen. Put the decoy
        # and then don't remove it until you've seen several minutes of
        # no foreign shares"
        m_px = round(self.main.price - 0.01, 2)
        self.book_with_minnow(m_px, 20.0, 60)
        self.cyc(60)
        self.assertEqual(len(self.orders(AL, "SELL", decoy=True)), 1)
        self.book_with_minnow(m_px, 0.0, 120)                  # the minnow left
        self.cyc(120)
        self.assertEqual(len(self.orders(AL, "SELL", decoy=True)), 1)   # the decoy stays
        self.assertEqual(self.b.dance[AL]["clear_since"], self.now + 120)
        self.book_with_minnow(m_px, 20.0, 200)                 # back again: the clock resets
        self.cyc(200)
        self.assertIsNone(self.b.dance[AL]["clear_since"])
        self.book_with_minnow(m_px, 0.0, 260)
        self.cyc(260)
        self.book_with_minnow(m_px, 0.0, 500)
        self.cyc(500)                                          # 240 s clear: still holding
        self.assertEqual(len(self.orders(AL, "SELL", decoy=True)), 1)
        self.book_with_minnow(m_px, 0.0, 600)
        self.cyc(600)                                          # 340 s clear: done
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])
        self.assertNotIn(AL, self.b.dance)
        self.assertIn("decoy_done", [e["event"] for e in self.b.log])

    def test_a_minnow_seen_then_gone_still_gets_its_decoy(self):
        # seen on one cycle, gone by the next: the decoy joins where it
        # flickered and holds
        m_px = round(self.main.price - 0.01, 2)
        self.book_with_minnow(m_px, 5.0, 60)
        self.b.dance[AL] = {"px": m_px, "moves": 0, "since": self.now + 60,
                            "last_px": m_px, "last_q": 5.0, "last_seen": self.now + 60,
                            "clear_since": None}
        self.book_with_minnow(m_px, 0.0, 90)                   # gone again
        self.cyc(90)
        decoys = self.orders(AL, "SELL", decoy=True)
        self.assertEqual(len(decoys), 1)
        self.assertAlmostEqual(decoys[0].price, m_px)

    def test_dust_in_front_is_never_snapped_the_decoy_holds(self):
        m_px = round(self.main.price - 0.01, 2)
        self.book_with_minnow(m_px, 0.5, 60)                   # half a share
        self.cyc(60)
        self.assertEqual(len(self.orders(AL, "SELL", decoy=True)), 1)
        self.book_with_minnow(m_px, 0.5, 60 + DANCE_WAIT_S + 10)
        self.cyc(60 + DANCE_WAIT_S + 10)                       # "stayed put": nothing to take
        self.assertEqual(len(self.orders(AL, "SELL", decoy=True)), 1)
        ev = [e["event"] for e in self.b.log]
        self.assertNotIn("snapped", ev)
        self.assertEqual(ev.count("dance_holds"), 1)

    def test_dust_at_the_far_touch_gets_no_decoy(self):
        # the Maryland case (2026-09-03): 0.01 share a tick under the ask.
        # It has nowhere to move, so a decoy has nothing to do ("there
        # wouldn't be much for the decoy to do anyways"); the exit's
        # contingent steps up instead
        m_px = round(self.main.price - 0.01, 2)
        bid = round(m_px - 0.01, 2)                            # the far touch is a tick away
        self.book_with_minnow(m_px, 0.01, 60, bid=bid)
        self.cyc(60)
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])
        ev = [e["event"] for e in self.b.log]
        self.assertNotIn("dance_over", ev)
        self.assertNotIn("snapped", ev)
        self.assertEqual(ev.count("dance_idle"), 1)
        self.book_with_minnow(m_px, 0.01, 120, bid=bid)
        self.cyc(120)
        self.assertEqual([e["event"] for e in self.b.log].count("dance_idle"), 1)   # noted once
        v = self.b.view(self.now + 120, self.positions())["rows"][0]
        self.assertIsNone(v["decoy"])
        self.assertTrue(v["dance"]["idle"])

    def test_dust_with_room_to_move_gets_its_decoy(self):
        m_px = round(self.main.price - 0.01, 2)
        self.book_with_minnow(m_px, 0.01, 60, bid=round(m_px - 0.05, 2))   # room in front of it
        self.cyc(60)
        decoys = self.orders(AL, "SELL", decoy=True)
        self.assertEqual(len(decoys), 1)
        self.assertAlmostEqual(decoys[0].price, m_px)
        self.assertFalse(self.b.dance[AL].get("idle"))

    def test_a_refused_decoy_says_so_on_the_page(self):
        m_px = round(self.main.price - 0.01, 2)
        self.book_with_minnow(m_px, 20.0, 60)
        real = self.r.fam.desk.place_resting
        from v3.orders import OrderResult
        self.r.fam.desk.place_resting = lambda *a, **k: OrderResult(ok=False, note="no buying power")
        try:
            self.cyc(60)
        finally:
            self.r.fam.desk.place_resting = real
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])
        self.assertEqual(self.b.dance[AL]["note"], "no buying power")
        v = self.b.view(self.now + 60, self.positions())["rows"][0]
        self.assertIsNone(v["decoy"])
        self.assertEqual(v["dance"]["note"], "no buying power")

    def test_more_than_25_shares_in_front_is_not_a_minnow(self):
        self.book_with_minnow(round(self.main.price - 0.01, 2), 26.0, 60)
        self.cyc(60)
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])
        self.book_with_minnow(round(self.main.price - 0.01, 2), 25.0, 120)
        self.cyc(120)
        self.assertEqual(len(self.orders(AL, "SELL", decoy=True)), 1)


class TestEntryAndSales(Base):
    def test_nothing_is_bought_where_no_bond_sale_of_ours_rests(self):
        # owner, 2026-09-02: "the sniper should only work where I have
        # bond sales resting" — a listed market with a cheap touch and
        # money in hand is NOT entered by itself, and a minnow-sized
        # order there draws no decoy either
        self.b.approve(AL, self.now)
        self.b.approve(TN, self.now)
        self.b.set_budget(500.0)
        self.r.cache.put(TN, yes_book(self.now, bid=0.97, ask=0.98, ask_q=20.0))
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertEqual(self.r.exchange.live, {})
        self.assertEqual(self.b.budget, 500.0)
        self.assertEqual(self.b.lots, {})

    def test_a_sale_by_our_order_leaves_the_ledger_and_the_cash_waits(self):
        self.b.approve(AL, self.now)
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        ask = self.orders(AL, "SELL")[0]
        sold = ask.qty                                    # the offered part; the rest is kept
        self.r.exchange.live.pop(ask.id, None)          # the exchange filled it
        self.r.fam.orders.pop(ask.id, None)
        self.exch(AL, 100.0 - sold, 0.98)
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertAlmostEqual(self.b.held(AL, "YES"), 100.0 - sold, places=4)
        self.assertAlmostEqual(self.b.cash, sold * ask.price, places=2)

    def test_a_hand_sale_with_our_order_untouched_is_not_counted(self):
        self.b.approve(AL, self.now)
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        self.exch(AL, 60.0, 0.98)                       # 40 sold by hand
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(self.b.cash, 0.0)
        self.assertEqual(self.b.held(AL, "YES"), 100.0)  # the ledger keeps its count

    def test_persistence_round_trip(self):
        self.b.approve(AL, self.now)
        self.b.approve(ALD, self.now)
        self.bond(AL, "YES", 50.0, 0.98)
        self.b.set_budget(250.0)
        self.b.unpinged = 33.0
        self.b.dance[AL] = {"px": 0.97, "moves": 1, "since": 5.0}
        d = self.b.to_dict()
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        b2.restore(d)
        self.assertEqual(b2.held(AL, "YES"), 50.0)
        self.assertEqual((b2.budget, b2.unpinged), (250.0, 33.0))
        self.assertEqual(b2.dance[AL]["moves"], 1)
        self.assertEqual(self.r.fam.bond_qty, {AL: 50.0})


class TestTheOwnersEntry(Base):
    """Owner, 2026-09-02: "the initial purchases should be made by me so
    let me see the books in the bond market and give me the choice to
    enter at various prices, snapping up all the currently resting sale
    orders at or below that price."""

    class Sweeping:
        """A book that loses each level as it is taken."""

        def __init__(self, asks, now):
            self.asks, self.now = list(asks), now
            self.books = {}
            self.trades = []

        def book(self, slug, fetched_at=None):
            return Book(bids=((0.90, 50.0), (0.50, 20000.0)),
                        asks=tuple(self.asks) + ((0.999, 20000.0),),
                        tick=0.01, fetched_at=fetched_at or self.now)

        def post(self, url, body, **k):
            # the take fills: the touch level is gone from the book, and
            # the exchange's trade record shows the fill
            self.asks.pop(0)
            oid = "T%d" % (10 - len(self.asks))
            self.fill(oid, body)
            return {"id": oid}

        def fill(self, oid, body):
            q = float(body["quantity"])
            px = float(body["price"]["value"])
            self.trades.append({"trade": {"id": "t" + oid, "aggressorExecution": {
                "id": "x" + oid,
                "order": {"id": oid, "intent": body["intent"], "quantity": q,
                          "cumQuantity": q, "avgPx": {"value": f"{px:.4f}"}},
                "lastShares": f"{q:.4f}", "lastPx": {"value": f"{px:.4f}"}}}})

        def recent_trades(self, limit=25):
            return list(self.trades)

        def __getattr__(self, name):
            return lambda *a, **k: []

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)
        self.sweep = self.Sweeping([(0.95, 20.0), (0.96, 30.0), (0.97, 40.0),
                                    (0.98, 500.0)], self.now)
        self.b.client = self.sweep
        self.r.fam.desk.client = self.sweep
        self.r.cache.put(AL, self.sweep.book(AL))

    def test_the_page_shows_the_ladder_to_enter_at(self):
        v = self.b.view(self.now, self.positions())
        lad = v["rows"][0]["ladder"]
        self.assertEqual([l["px"] for l in lad][:4], [0.95, 0.96, 0.97, 0.98])
        self.assertEqual(lad[1]["cum_qty"], 50.0)
        self.assertAlmostEqual(lad[1]["cum_usd"], 20 * 0.95 + 30 * 0.96, places=2)
        self.assertEqual(v["money"], 1000.0)

    def test_entering_at_a_price_sweeps_everything_at_or_inside_it(self):
        r = self.b.enter(AL, 0.97, self.now, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertEqual(self.b.held(AL, "YES"), 90.0)          # 20 + 30 + 40
        self.assertAlmostEqual(self.b.cost_basis(AL, "YES"),
                               (20 * 0.95 + 30 * 0.96 + 40 * 0.97) / 90, places=4)
        self.assertEqual(self.sweep.asks, [(0.98, 500.0)])      # the 98s untouched
        self.assertAlmostEqual(self.b.budget, 1000 - (19 + 28.8 + 38.8), places=2)
        self.assertIn("90 YES in 3 lots", r["note"])

    def test_the_money_is_the_limit(self):
        self.b.set_budget(40.0)
        r = self.b.enter(AL, 0.98, self.now, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertEqual(self.b.held(AL, "YES"), 20.0 + 21.0)   # 20 @ 95c, then 21 @ 96c
        self.assertLess(self.b.budget, 5.0)

    def test_nothing_inside_the_price_means_nothing_bought(self):
        r = self.b.enter(AL, 0.94, self.now, self.positions())
        self.assertFalse(r["ok"])
        self.assertEqual(self.b.held(AL, "YES"), 0.0)
        self.assertEqual(self.b.budget, 1000.0)

    def test_only_listed_markets_and_only_with_money(self):
        self.assertFalse(self.b.enter(GA, 0.97, self.now)["ok"])
        self.b.set_budget(0.0)
        self.assertIn("budget", self.b.enter(AL, 0.97, self.now)["note"])


class TestTheDesksSecondCarveOut(Base):
    def test_only_the_owner_may_take_and_never_past_the_touch(self):
        d = self.r.fam.desk
        r = d.place_resting(AL, "BUY", 0.99, 10.0, intent=BUY_LONG,
                            initiator="auto", taker="bond")
        self.assertFalse(r.ok)
        self.assertIn("owner", r.note)
        self.r.cache.put(AL, yes_book(self.now, bid=0.97, ask=0.98))
        r = d.place_resting(AL, "BUY", 0.99, 10.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertFalse(r.ok)
        self.assertIn("never worse than the touch", r.note)
        self.r.cache.put(AL, yes_book(self.now))
        r = d.place_resting(AL, "BUY", 0.99, 301.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertFalse(r.ok)
        self.assertIn("exceeds", r.note)
        r = d.place_resting(AL, "BUY", 0.99, 300.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertTrue(r.ok, r.note)

    def test_a_no_bond_takes_the_bid(self):
        d = self.r.fam.desk
        r = d.place_resting(ALD, "SELL", 0.01, 400.0, intent=BUY_SHORT,
                            initiator="owner", taker="bond")
        self.assertTrue(r.ok, r.note)
        self.assertFalse(d.place_resting(ALD, "SELL", 0.01, 401.0,
                                         intent=BUY_SHORT, initiator="owner",
                                         taker="bond").ok)

    def test_nothing_else_may_cross_under_the_bond_flag(self):
        r = self.r.fam.desk.place_resting(AL, "SELL", 0.98, 10.0,
                                          intent=SELL_LONG, net_position=10.0,
                                          initiator="owner", taker="bond")
        self.assertFalse(r.ok)
        self.assertIn("only open a bond", r.note)


class TestEngineHandsOff(unittest.TestCase):
    def test_bond_orders_are_treated_like_the_owners_hand(self):
        import inspect
        from v3 import family
        src = inspect.getsource(family)
        self.assertNotIn('rec.purpose == "manual" or self._frozen', src)
        self.assertGreaterEqual(src.count('"bond"'), 10)
        self.assertIn("self.bond_qty.get(slug, 0.0)", src)


if __name__ == "__main__":
    unittest.main()


class TestOurOwnOrdersAreNotForSale(Base):
    """Owner, 2026-09-02: "we can't buy our own orders, so exclude orders
    that the engine places. For instance on this Hawai'i governor market
    we are selling 3 shares at the touch so the actually available
    shares should be 5.\""""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)

    def ours(self, px, qty, oid="E1", purpose="sell"):
        self.r.fam.orders[oid] = FamilyOrder(
            id=oid, market=AL, side="SELL", price=px, qty=qty,
            intent=SELL_LONG, placed_ts=self.now, purpose=purpose)

    def hawaii(self):
        self.bk = Book(bids=((0.97, 50.0), (0.50, 20000.0)),
                       asks=((0.98, 8.0), (0.99, 20000.0)),
                       tick=0.01, fetched_at=self.now)
        self.r.cache.put(AL, self.bk)
        self.ours(0.98, 3.0)

    def test_eight_showing_three_ours_is_five(self):
        self.hawaii()
        row = self.b.view(self.now, self.positions())["rows"][0]
        self.assertAlmostEqual(float(row["size"]), 5.0)
        self.assertEqual(row["ladder"][0]["px"], 0.98)
        self.assertAlmostEqual(row["ladder"][0]["qty"], 5.0)
        self.assertAlmostEqual(row["ladder"][0]["cum_qty"], 5.0)
        self.assertAlmostEqual(row["ladder"][0]["cum_usd"], 5 * 0.98, places=2)
        px, cost, size = self.b._take_price("YES", self.bk, AL)
        self.assertEqual((px, size), (0.98, 5.0))

    def test_a_level_that_is_all_ours_is_not_on_the_ladder(self):
        self.r.cache.put(AL, Book(bids=((0.97, 50.0), (0.50, 20000.0)),
                                  asks=((0.98, 3.0), (0.99, 300.0)),
                                  tick=0.01, fetched_at=self.now))
        self.ours(0.98, 3.0)
        row = self.b.view(self.now, self.positions())["rows"][0]
        self.assertEqual([l["px"] for l in row["ladder"]], [0.99])
        self.assertEqual(row["cost"], 0.99)
        self.assertAlmostEqual(float(row["size"]), 300.0)

    def test_a_level_that_is_only_ours_is_cleared_not_bought(self):
        # our own 20 @ 95c sit in the take's way: they are pulled first
        # (2026-09-03), the level goes with them, and the sweep buys the
        # 96s and 97s from others
        sweep = TestTheOwnersEntry.Sweeping(
            [(0.95, 20.0), (0.96, 30.0), (0.97, 40.0), (0.98, 500.0)], self.now)

        def post(url, body, **k):
            if "/cancel" in url:
                sweep.asks = [(p, q) for p, q in sweep.asks if p != 0.95]
                return {}
            sweep.asks.pop(0)
            oid = "T%d" % len(sweep.asks)
            sweep.fill(oid, body)
            return {"id": oid}

        sweep.post = post
        self.b.client = sweep
        self.r.fam.desk.client = sweep
        self.r.cache.put(AL, sweep.book(AL))
        self.ours(0.95, 20.0)
        r = self.b.enter(AL, 0.97, self.now, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertNotIn("E1", self.r.fam.orders)
        self.assertEqual(self.b.held(AL, "YES"), 70.0)          # 30 + 40, never our 20
        self.assertEqual(sweep.asks, [(0.98, 500.0)])
        self.assertEqual(self.r.fam.hold_until[AL], self.now + 600.0)

    def test_the_desk_refuses_a_take_of_our_own_level(self):
        self.hawaii()
        d = self.r.fam.desk
        r = d.place_resting(AL, "BUY", 0.98, 6.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertFalse(r.ok)
        self.assertIn("exceeds", r.note)                       # others show 5
        r = d.place_resting(AL, "BUY", 0.98, 5.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertTrue(r.ok, r.note)
        # a level that is all ours is not the touch for a take
        self.r.cache.put(AL, Book(bids=((0.97, 50.0), (0.50, 20000.0)),
                                  asks=((0.98, 3.0), (0.99, 300.0)),
                                  tick=0.01, fetched_at=self.now))
        r = d.place_resting(AL, "BUY", 0.98, 3.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertFalse(r.ok)
        r = d.place_resting(AL, "BUY", 0.99, 300.0, intent=BUY_LONG,
                            initiator="owner", taker="bond")
        self.assertTrue(r.ok, r.note)


class TestTheMinnowCheckNetsAllOurOrders(Base):
    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)
        self.r.cache.put(AL, minnow_book(self.now, minnows=0.0, ask=0.99))
        self.bond(AL, "YES", 1500.0, 0.90)
        self.b.more_cap[AL] = {"usd": 0.0, "by": "owner", "first": ""}
        self.b.cycle(self.now, self.positions(), on=True)
        self.main = self.orders(AL, "SELL", decoy=False)[0]
        self.m_px = round(self.main.price - 0.01, 2)
        self.r.fam.orders["E1"] = FamilyOrder(
            id="E1", market=AL, side="SELL", price=self.m_px, qty=5.0,
            intent=SELL_LONG, placed_ts=self.now, purpose="sell")

    def book(self, others, t):
        merged = {}
        for p, q in [(o.price, o.qty) for o in self.orders(AL, "SELL")] + [
                (self.m_px, others), (0.99, 20000.0)]:
            merged[round(p, 4)] = merged.get(round(p, 4), 0.0) + q
        self.r.cache.put(AL, Book(
            bids=((0.88, 50.0), (0.50, 20000.0)),
            asks=tuple(sorted((p, q) for p, q in merged.items() if q > 0)),
            tick=0.01, fetched_at=self.now + t))

    def test_our_engine_order_in_front_is_not_a_minnow(self):
        self.book(0.0, 60)                       # the 5 in front are ours
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])
        self.assertNotIn(AL, self.b.dance)
        self.assertIsNone(self.b.view(self.now + 60, self.positions())["rows"][0]["minnow"])

    def test_only_the_others_shares_count_as_the_minnow(self):
        self.book(3.0, 60)                       # 8 showing, 5 ours
        self.b.cycle(self.now + 60, self.positions(), on=True)
        decoys = self.orders(AL, "SELL", decoy=True)
        self.assertEqual(len(decoys), 1)
        self.assertAlmostEqual(decoys[0].price, self.m_px)
        self.book(3.0, 61)                       # the book now shows the decoy too
        v = self.b.view(self.now + 61, self.positions())["rows"][0]
        self.assertAlmostEqual(v["minnow"]["qty"], 3.0)


class TestTheBudgetFollowsTaxes(Base):
    """Owner, 2026-09-03: "set the budget to whatever I currently owe in
    taxes." The budget follows what he owes, less what the engine has
    spent of it, unless he types a fixed figure."""

    def setUp(self):
        super().setUp()
        self.tax = {"owed": 220.0, "gross": 1000.0, "rate": 0.22}
        self.b.tax_owed = lambda: self.tax

    def test_the_budget_is_what_he_owes_less_what_was_spent(self):
        self.assertEqual(self.b.budget_mode, "tax")
        self.b.cycle(self.now, self.positions(), on=False)
        self.assertEqual(self.b.budget, 220.0)
        self.tax["owed"] = 264.0                        # more paid, more owed
        self.assertEqual(self.b.view(self.now)["budget"], 264.0)
        self.b._pay(64.0)
        self.assertEqual((self.b.budget, self.b.spent), (200.0, 64.0))
        self.b.cycle(self.now + 1, self.positions(), on=False)
        self.assertEqual(self.b.budget, 200.0)          # spent stays spent
        v = self.b.view(self.now + 1)
        self.assertEqual(v["budget_mode"], "tax")
        self.assertEqual(v["tax"]["gross"], 1000.0)
        self.assertEqual(v["money"], 200.0)

    def test_a_fixed_budget_overrides_and_follow_returns(self):
        self.b.set_budget(50.0)
        self.assertEqual(self.b.budget_mode, "fixed")
        self.b.cycle(self.now, self.positions(), on=False)
        self.assertEqual(self.b.budget, 50.0)
        r = self.b.follow_tax()
        self.assertTrue(r["ok"])
        self.assertEqual((self.b.budget_mode, self.b.budget), ("tax", 220.0))

    def test_no_pay_data_yet_means_no_budget(self):
        self.b.tax_owed = lambda: None
        self.b.approve(AL, self.now)
        self.b.cycle(self.now, self.positions(), on=False)
        self.assertEqual(self.b.budget, 0.0)
        self.assertIn("budget", self.b.enter(AL, 0.97, self.now)["note"])

    def test_old_state_without_the_mode_follows_taxes(self):
        d = self.b.to_dict()
        d.pop("budget_mode", None)
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        b2.restore(d)
        self.assertEqual(b2.budget_mode, "tax")
        self.b.set_budget(50.0)
        b3 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        b3.restore(self.b.to_dict())
        self.assertEqual((b3.budget_mode, b3.budget), ("fixed", 50.0))


class TestEarnings(Base):
    """Owner, 2026-09-03: "the bonds page should reflect the overall
    earnings from the bonds which is the profit from sales and the
    liquidity rewards payments" — then "It shouldn't be based on
    market, it should be based on orders." So rewards are what the
    bond's own orders measured while resting."""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)

    def test_profit_on_a_sale_by_our_order_is_counted(self):
        self.bond(AL, "YES", 100.0, 0.90)
        self.b.more_cap[AL]["usd"] = 0.0
        self.b.cycle(self.now, self.positions(), on=True)
        ask = self.orders(AL, "SELL")[0]
        sold = ask.qty
        self.r.exchange.live.pop(ask.id, None)          # the exchange filled it
        self.r.fam.orders.pop(ask.id, None)
        self.exch(AL, 100.0 - sold, 0.90)
        self.b.cycle(self.now + 60, self.positions(), on=True)
        e = self.b.view(self.now + 60)["earned"]
        self.assertAlmostEqual(e["sales"], sold * (ask.price - 0.90), places=2)
        self.assertAlmostEqual(e["sold_usd"], sold * ask.price, places=2)
        self.assertAlmostEqual(e["total"], e["sales"], places=2)
        self.assertEqual(e["rewards"], 0.0)

    def test_rewards_are_what_the_bond_orders_measured_not_the_markets_payout(self):
        self.bond(AL, "YES", 100.0, 0.90)
        self.b.more_cap[AL]["usd"] = 0.0
        self.b.cycle(self.now, self.positions(), on=True)
        bond = self.orders(AL, "SELL")[0]
        bond.live_est = 2.4                              # $2.40/day measured
        self.r.fam.orders["E1"] = FamilyOrder(
            id="E1", market=AL, side="BUY", price=0.90, qty=50.0,
            intent=BUY_LONG, placed_ts=self.now, purpose="earn", live_est=24.0)
        for k in range(1, 13):                           # an hour of cycles at $2.40/day
            self.b.cycle(self.now + 300 * k, self.positions(), on=True)
        e = self.b.view(self.now + 3600)["earned"]
        self.assertAlmostEqual(e["rewards"], 0.10, places=2)       # the engine's $24/day is not ours
        self.assertAlmostEqual(e["total"], 0.10, places=2)
        row = self.b.view(self.now + 3600, self.positions())["rows"][0]
        self.assertAlmostEqual(row["rewards"], 0.10, places=2)
        # a long gap after downtime is not counted as earned
        self.b.cycle(self.now + 3600 + 10 * 3600, self.positions(), on=True)
        self.assertAlmostEqual(sum(self.b.accrued.values()),
                               0.10 + 2.4 * 600 / 86400, places=3)

    def test_the_accrual_is_by_day_and_survives_a_restore(self):
        self.bond(AL, "YES", 100.0, 0.90)
        self.b.more_cap[AL]["usd"] = 0.0
        self.b.cycle(self.now, self.positions(), on=True)
        self.orders(AL, "SELL")[0].live_est = 24.0       # $1/hour
        self.b.cycle(self.now + 300, self.positions(), on=True)
        d1 = Bonds._day(self.now + 300)
        self.assertAlmostEqual(self.b.accrued[d1], 24.0 * 300 / 86400, places=4)
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s),
                   clock=lambda: self.now + 300)
        b2.restore(self.b.to_dict())
        self.assertAlmostEqual(b2.view(self.now + 300)["earned"]["rewards"],
                               24.0 * 300 / 86400, places=2)
        self.assertAlmostEqual(b2.view(self.now + 300)["earned"]["today"],
                               24.0 * 300 / 86400, places=2)
        self.assertEqual(b2._accrued_at, self.now + 300)
        # a lot from before buying more existed gets its defaults on restore
        d = self.b.to_dict()
        d.pop("more_cap", None)
        b3 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        b3.restore(d)
        self.assertEqual(b3.more_cap[AL]["usd"], 90.0)
        self.assertEqual(b3.more_cap[AL]["px"], 0.90)
        self.assertEqual(b3.more_cap[AL]["first"], "test")


class TestYourBondsFirst(Base):
    """Owner, 2026-09-03: "Take the markets I'm actually in and put them
    at the top. Show me the calculations for how much it's earning and
    any minnow / sniper info ... reserve a websocket for each of the
    markets I'm in.\""""

    def setUp(self):
        super().setUp()
        for s in (AL, TN, ALD):
            self.b.approve(s, self.now)
        self.b.set_budget(1000.0)

    def test_the_markets_he_is_in_sort_first(self):
        self.bond(TN, "YES", 100.0, 0.98)
        rows = self.b.view(self.now, self.positions())["rows"]
        self.assertEqual(rows[0]["market"], TN)
        self.assertEqual({r["market"] for r in rows[1:]}, {AL, ALD})
        self.assertEqual(self.b.held_markets(), [TN])
        self.assertEqual(self.b.held_markets(), sorted(self.b.live_rows(self.now)))

    def test_the_calculation_behind_the_estimate(self):
        self.bond(AL, "YES", 100.0, 0.90)
        self.b.cycle(self.now, self.positions(), on=True)     # rests the earning ask
        row = self.b.live_rows(self.now)[AL]
        c = row["calc"]
        self.assertEqual(c["side"], "SELL")
        self.assertAlmostEqual(c["side_pool"], c["pool_day"] / c["event_n"] / 2, places=4)
        main = [o for o in c["orders"] if not o["decoy"]]
        self.assertEqual(len(main), 1)
        o = main[0]
        self.assertEqual(o["price"], self.orders(AL, "SELL")[0].price)
        self.assertAlmostEqual(o["est"], o["share"] * c["side_pool"] if o["qualifies"] else 0.0,
                               places=2)
        self.assertGreater(o["est"], 0.0)
        self.assertIsNotNone(c["touch"])
        self.assertGreaterEqual(c["touch"]["est"], o["est"] * 0.99)
        self.assertNotIn(TN, self.b.live_rows(self.now))       # not held: not on the line
        self.assertIsNone(self.b.view(self.now)["rows"][1]["calc"])

    def test_a_big_order_in_front_shows_but_is_no_minnow(self):
        self.r.cache.put(AL, minnow_book(self.now, minnows=0.0, ask=0.99))
        self.bond(AL, "YES", 1500.0, 0.90)
        self.b.cycle(self.now, self.positions(), on=True)
        main = self.orders(AL, "SELL", decoy=False)[0]
        px = round(main.price - 0.01, 2)
        merged = {}
        for p, q in [(o.price, o.qty) for o in self.orders(AL, "SELL")] + [
                (px, 300.0), (0.99, 20000.0)]:
            merged[round(p, 4)] = merged.get(round(p, 4), 0.0) + q
        self.r.cache.put(AL, Book(
            bids=((0.88, 50.0), (0.50, 20000.0)),
            asks=tuple(sorted((p, q) for p, q in merged.items() if q > 0)),
            tick=0.01, fetched_at=self.now + 60))
        self.b.cycle(self.now + 60, self.positions(), on=True)
        row = self.b.view(self.now + 60, self.positions())["rows"][0]
        self.assertEqual(row["front"], {"price": px, "qty": 300.0})
        self.assertIsNone(row["minnow"])
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])
        self.assertEqual(self.b.view(self.now + 60)["minnow_max"], MINNOW_MAX)


class TestTheExchangeIsTheTruth(Base):
    """Owner, 2026-09-03: "It is saying that I hold 10 shares that I do
    not hold. That should not be possible. The api is the source of
    truth and nothing should be making up holdings or transactions."
    The Hawaii case: two Enter taps each booked 5 NO shares the
    exchange never filled."""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)

    def test_a_take_the_exchange_never_filled_books_nothing(self):
        self.r.exchange.recent_trades = lambda limit=25: []    # no fill on record
        r = self.b.enter(AL, 0.99, self.now, self.positions())
        self.assertFalse(r["ok"])
        self.assertEqual(self.b.lots, {})
        self.assertEqual(self.b.held(AL, "YES"), 0.0)
        self.assertEqual(self.b.budget, 1000.0)             # nothing spent
        ev = [e for e in self.b.log if e["event"] == "take_unfilled"]
        self.assertEqual(len(ev), 1)
        self.assertIn("no fill", ev[0]["note"])
        self.assertEqual(self.r.exchange.live, {})          # it was pulled, not left resting

    def test_a_take_books_exactly_what_the_record_shows(self):
        # the record shows 7 of the 10 asked, at a better price
        real = self.r.exchange.post

        def post(url, body, **k):
            out = real(url, body, **k)
            t = self.r.exchange.trades[-1]["trade"]["aggressorExecution"]
            t["order"]["cumQuantity"] = 7
            t["order"]["avgPx"] = {"value": "0.9850"}
            t["lastShares"] = "7.0000"
            t["lastPx"] = {"value": "0.9850"}
            return out
        self.r.exchange.post = post
        self.r.cache.put(AL, yes_book(self.now, ask=0.99, ask_q=10.0))
        r = self.b.enter(AL, 0.99, self.now, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertEqual(self.b.held(AL, "YES"), 7.0)
        self.assertAlmostEqual(self.b.cost_basis(AL, "YES"), 0.985, places=4)
        self.assertAlmostEqual(self.b.budget, 1000 - 7 * 0.985, places=2)

    def test_the_ledger_is_trimmed_to_the_position_feed(self):
        # a smaller reading must hold over several reads and minutes
        # (2026-09-04: one short read wrote off 32 markets), and the
        # record must agree — here it shows no purchase, so it does
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)     # the exchange showed 100
        self.exch(AL, 60.0, 0.98)                              # 40 sold by hand
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(self.b.held(AL, "YES"), 100.0)        # one read is not believed
        self.b.cycle(self.now + 200, self.positions(), on=True)
        self.assertEqual(self.b.held(AL, "YES"), 100.0)        # nor two
        self.b.cycle(self.now + 400, self.positions(), on=True)
        self.assertEqual(self.b.held(AL, "YES"), 60.0)         # three reads over 5 min: the truth
        self.assertEqual(self.b.cash, 0.0)                     # a hand sale: not reinvested
        self.assertEqual(self.b.budget, 1000.0)                # and nothing refunded
        ev = [e for e in self.b.log if e["event"] == "trimmed_to_exchange"]
        self.assertEqual((ev[0]["qty"], ev[0]["refund"]), (40.0, 0.0))

    def test_one_short_read_writes_nothing_off(self):
        # the 2026-09-04 wipe: the feed showed every holding as zero on
        # one read, then showed them again. Nothing moves.
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertTrue(self.r.fam._frozen(AL))
        good = self.positions()
        self.b.cycle(self.now + 60, {}, on=True)                    # an empty read
        self.b.cycle(self.now + 120, {GA: (5.0, 3.0)}, on=True)     # a short read
        self.assertEqual(self.b.held(AL, "YES"), 100.0)
        self.assertTrue(self.r.fam._frozen(AL))                     # the engine stays out
        self.assertTrue(self.orders(AL, "SELL"))                    # the exit stays
        self.b.cycle(self.now + 180, good, on=True)                 # the feed is back
        self.assertEqual(self.b.held(AL, "YES"), 100.0)
        self.assertNotIn(AL, self.b.unconfirmed)
        self.assertEqual([e for e in self.b.log if e["event"] == "trimmed_to_exchange"], [])

    def test_a_smaller_reading_the_record_cannot_explain_is_kept(self):
        # the record shows the purchase and no sale: the feed's smaller
        # figure is flagged, not believed (owner, 2026-09-04: "if the
        # positions disappear, you can verify their disposition using
        # the transaction lists")
        from v3.main import parse_activities
        self.b.parse = parse_activities
        self.r.exchange.trades.append(self.trade_row("B1", BUY_LONG, 0.98, 100.0, self.now - 100))
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        self.exch(AL, 0.0, 0.0)
        for dt in (60, 200, 400, 800):
            self.b.cycle(self.now + dt, self.positions(), on=True)
        self.assertEqual(self.b.held(AL, "YES"), 100.0)
        self.assertIn(AL, self.b.unconfirmed)
        self.assertEqual(self.b.unconfirmed[AL]["record"], 100.0)
        row = self.b.view(self.now + 801, self.positions())["rows"][0]
        self.assertEqual(row["unconfirmed"]["exch"], 0.0)
        self.assertIn("holding_unconfirmed", [e["event"] for e in self.b.log])
        # the record then shows the sale: now it is gone
        self.r.exchange.trades.append(self.trade_row("B2", SELL_LONG, 0.99, 100.0, self.now + 500))
        self.b.cycle(self.now + 1200, self.positions(), on=True)
        self.assertEqual(self.b.held(AL, "YES"), 0.0)
        self.assertNotIn(AL, self.b.unconfirmed)

    def test_shares_the_exchange_never_showed_go_back_to_the_money(self):
        # the Hawaii state: a lot booked, paid for, never on the exchange
        self.b._book_lot(AL, "YES", 10.0, 9.3)
        self.b._pay(9.3)
        self.assertEqual(self.b.budget, 1000 - 9.3)
        for dt in (600, 700, 1000):
            self.b.cycle(self.now + dt, {GA: (5.0, 3.0)}, on=True)   # a live feed, AL absent
        self.assertEqual(self.b.lots, {})
        self.assertAlmostEqual(self.b.budget, 1000.0, places=2)
        self.assertEqual(self.b.spent, 0.0)
        ev = [e for e in self.b.log if e["event"] == "trimmed_to_exchange"]
        self.assertAlmostEqual(ev[0]["refund"], 9.3, places=2)

    def test_an_empty_feed_is_not_believed(self):
        self.b._book_lot(AL, "YES", 10.0, 9.3)
        self.b.cycle(self.now + 600, {}, on=True)
        self.assertEqual(self.b.held(AL, "YES"), 10.0)


class TestClearingTheWay(Base):
    """Owner, 2026-09-03: "I think the order did not go through because
    I still have resting orders from the engine ... When you clear out,
    it might make sense to tell the engine to hold off on that market
    for 10 minutes." The Hawaii case: the engine's 3-share cover bid at
    7c sat at the touch, so a 7c sell of ours would match our own bid
    first and the exchange left it unfilled."""

    def setUp(self):
        super().setUp()
        self.b.approve(ALD, self.now)                  # a NO bond: takes hit the bids
        self.b.set_budget(1000.0)

    def our_bid(self, oid, purpose, px=0.01, qty=3.0):
        self.r.fam.orders[oid] = FamilyOrder(
            id=oid, market=ALD, side="BUY", price=px, qty=qty,
            intent=SELL_SHORT, placed_ts=self.now, purpose=purpose)
        self.r.exchange.live[oid] = {"id": oid, "market": ALD, "side": "BUY",
                                     "price": px, "size": qty, "intent": SELL_SHORT}

    def test_the_engines_bid_in_the_way_is_pulled_and_the_engine_held_off(self):
        self.our_bid("E1", "sell")                     # the engine's cover bid at the touch
        self.assertTrue(self.r.fam.enterable(ALD))
        r = self.b.enter(ALD, 0.01, self.now, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertNotIn("E1", self.r.fam.orders)
        self.assertNotIn("E1", self.r.exchange.live)
        self.assertEqual(self.b.held(ALD, "NO"), 397.0)   # 400 showing less our 3
        self.assertEqual(self.r.fam.hold_until[ALD], self.now + 600.0)
        self.assertTrue(self.r.fam._frozen(ALD))
        self.assertFalse(self.r.fam.enterable(ALD))
        ev = [e for e in self.b.log if e["event"] == "cleared"][0]
        self.assertEqual(ev["orders"], ["E1"])
        row = self.b.view(self.now + 1, self.positions())["rows"][0]
        self.assertEqual(row["hold_until"], self.now + 600.0)
        self.r.fam._clock = lambda: self.now + 601.0
        self.assertFalse(self.r.fam._frozen(ALD))

    def test_a_hand_order_in_the_way_stops_the_take_untouched(self):
        self.our_bid("H1", "manual")
        r = self.b.enter(ALD, 0.01, self.now, self.positions())
        self.assertFalse(r["ok"])
        self.assertIn("your own order H1", r["note"])
        self.assertIn("H1", self.r.fam.orders)
        self.assertIn("H1", self.r.exchange.live)
        self.assertEqual(self.b.held(ALD, "NO"), 0.0)
        self.assertNotIn(ALD, self.r.fam.hold_until)

    def test_an_order_behind_the_take_price_is_not_in_the_way(self):
        self.our_bid("E2", "sell", px=0.001)           # behind the 1c bids: stays
        r = self.b.enter(ALD, 0.01, self.now, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertIn("E2", self.r.fam.orders)
        self.assertNotIn(ALD, self.r.fam.hold_until)


class TestOnlyConfirmedFillsAreHeld(Base):
    """Owner, 2026-09-03 (screenshot: "Held 3 @ 93c ... resting 3 @ 7c"):
    the trim had stopped at the exchange's 3-share short, but that short
    is the engine's own stock, not a bond purchase, and the bond's
    stale cover bid was still resting against it."""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.approve(ALD, self.now)
        self.b.set_budget(1000.0)

    def test_a_lot_nothing_confirmed_backs_is_dropped_on_restore(self):
        # the Hawaii state as the old code left it: booked, paid, unbacked
        self.b.lots[ALD] = {"qty": -10.0, "cost": 9.3}
        self.b._pay(9.3)
        # and a lot backed by a confirmed fill, which stays
        self.b._book_lot(AL, "YES", 7.0, 6.9, ref="T7")
        d = self.b.to_dict()
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        b2.restore(d)
        self.assertNotIn(ALD, b2.lots)
        self.assertEqual(b2.held(AL, "YES"), 7.0)
        self.assertEqual(b2.lots[AL]["fills"], ["T7"])
        self.assertAlmostEqual(b2.budget, 1000.0, places=2)   # the 9.30 came back
        self.assertEqual(b2.spent, 0.0)
        ev = [e for e in b2.log if e["event"] == "unbooked_unconfirmed"]
        self.assertEqual((ev[0]["market"], ev[0]["qty"]), (ALD, 10.0))

    def test_shares_he_counted_in_are_backed_by_him(self):
        self.exch(AL, 50.0, 0.98)
        self.assertTrue(self.b.adopt(AL, 20.0, self.positions())["ok"])
        self.assertEqual(self.b.lots[AL]["fills"], ["adopt"])

    def test_nothing_held_pulls_the_stale_earn_order(self):
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        ask = self.orders(AL, "SELL")[0]
        self.b.lots.clear()                              # the lot was trimmed away
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertNotIn(ask.id, self.r.fam.orders)
        self.assertNotIn(ask.id, self.r.exchange.live)
        self.assertIn("earn_pulled", [e["event"] for e in self.b.log])
        self.assertEqual(self.b.view(self.now + 61, self.positions())["rows"][0]["qty"], 0.0)

    def test_clearing_only_our_own_bond_order_does_not_hold_the_engine(self):
        self.r.fam.orders["B1"] = FamilyOrder(
            id="B1", market=ALD, side="BUY", price=0.01, qty=3.0,
            intent=SELL_SHORT, placed_ts=self.now, purpose="bond")
        self.r.exchange.live["B1"] = {"id": "B1", "market": ALD, "side": "BUY",
                                      "price": 0.01, "size": 3.0, "intent": SELL_SHORT}
        r = self.b.enter(ALD, 0.01, self.now, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertNotIn("B1", self.r.fam.orders)
        self.assertNotIn(ALD, self.r.fam.hold_until)


class TestFiveExecutionsAreFive(Base):
    """Owner, 2026-09-03: "I definitely just purchased 5 shares. Only
    three are the engine stock, can it not tell the difference?" The
    take C8RG6TS1CMVR filled in five executions; the rows carry no
    top-level id, so a duplicate check keyed on it saw one execution
    and booked one share. The order's own running total is the read."""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)
        self.r.cache.put(AL, yes_book(self.now, ask=0.99, ask_q=5.0))

    @staticmethod
    def rows(oid, n, px=0.99):
        # one row per execution, as the exchange sends them: no activity
        # id, an execution id each, the order's running total inside
        out = []
        for i in range(1, n + 1):
            out.append({"type": "ACTIVITY_TYPE_TRADE", "trade": {
                "id": f"T{i}", "aggressorExecution": {
                    "id": f"X{i}",
                    "order": {"id": oid, "intent": BUY_LONG, "quantity": n,
                              "cumQuantity": i, "leavesQuantity": n - i,
                              "avgPx": {"value": f"{px:.4f}"}},
                    "lastShares": "1.0000", "lastPx": {"value": f"{px:.4f}"}}}})
        return out

    def test_the_orders_running_total_is_read(self):
        real = self.r.exchange.post
        holder = {}

        def post(url, body, **k):
            out = real(url, body, **k)
            holder["oid"] = out["order"]["id"]
            return out
        self.r.exchange.post = post
        self.r.exchange.recent_trades = lambda limit=25: self.rows(holder.get("oid", "?"), 5)
        r = self.b.enter(AL, 0.99, self.now, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertEqual(self.b.held(AL, "YES"), 5.0)
        self.assertAlmostEqual(self.b.cost_basis(AL, "YES"), 0.99, places=4)
        self.assertEqual(self.b.fill_book[holder["oid"]]["qty"], 5.0)

    def test_rows_without_a_running_total_are_summed_per_execution(self):
        rows = self.rows("O1", 3)
        for r in rows:
            r["trade"]["aggressorExecution"]["order"].pop("cumQuantity")
        rows.append(rows[-1])                            # the same execution twice
        shares, avg, note = self.b._record_of("O1", rows)
        self.assertEqual(shares, 3.0)
        self.assertAlmostEqual(avg, 0.99, places=4)
        self.assertIn("summed", note)
        self.assertIsNone(self.b._record_of("O9", rows))

    def test_a_fill_booked_short_is_corrected_from_the_record(self):
        # the Hawaii state as left: 1 booked on an order that filled 5
        self.b._book_lot(AL, "YES", 1.0, 0.99, ref="C8R")
        self.b._pay(0.99)
        d = self.b.to_dict()
        d.pop("fill_book", None)                         # state from before fills were kept
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s),
                   clock=lambda: self.now, sleep=lambda s: None)
        b2.restore(d)
        self.assertEqual(b2.fill_book["C8R"]["qty"], 1.0)
        self.r.exchange.recent_trades = lambda limit=25: self.rows("C8R", 5)
        b2.cycle(self.now + 30, self.positions(), on=False)
        self.assertEqual(b2.held(AL, "YES"), 5.0)
        self.assertAlmostEqual(b2.lots[AL]["cost"], 5 * 0.99, places=2)
        self.assertAlmostEqual(b2.spent, 5 * 0.99, places=2)
        ev = [e for e in b2.log if e["event"] == "fill_corrected"][0]
        self.assertEqual((ev["qty"], ev["order_id"]), (5.0, "C8R"))
        # and nothing changes once it agrees
        b2.cycle(self.now + 60, self.positions(), on=False)
        self.assertEqual(len([e for e in b2.log if e["event"] == "fill_corrected"]), 1)


class TestBuyingMore(Base):
    """Owner, 2026-09-03: "Yes to 2, but only up to an amount I set
    defaulting to my original purchase price and quantity in that
    market. This default resets when I no longer hold bond shares in
    that market. And the order should place at the lowest price where
    it can capture at least 30% of the earnings for its side. Otherwise
    it should not place at all. It should move when it no longer
    captures 30% or it can no longer move up without crossing my price
    cap."""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.approve(ALD, self.now)
        self.b.set_budget(1000.0)

    def more(self, slug):
        return self.b._more_orders(slug)

    def with_ours(self, slug, t, extra_bids=()):
        """The cached book as the exchange would show it: our resting
        orders merged in (the fixture's book never has them)."""
        bk = self.r.cache.any_age(slug)
        bids = {}
        for p, q in list(bk.bids) + list(extra_bids):
            bids[round(p, 4)] = bids.get(round(p, 4), 0.0) + q
        asks = {round(p, 4): q for p, q in bk.asks}
        for o in self.r.fam.orders.values():
            if o.market != slug:
                continue
            d = bids if o.side == "BUY" else asks
            d[round(o.price, 4)] = d.get(round(o.price, 4), 0.0) + o.qty
        nb = Book(bids=tuple(sorted(bids.items(), reverse=True)),
                  asks=tuple(sorted(asks.items())), tick=0.01, fetched_at=t)
        self.r.cache.put(slug, nb)
        return nb

    def test_the_default_amount_is_his_first_purchase_and_resets(self):
        self.bond(AL, "YES", 100.0, 0.98)                 # 100 @ 98c = $98
        self.assertEqual(self.b.more_cap[AL]["usd"], 98.0)
        self.assertEqual(self.b.more_cap[AL]["by"], "default")
        self.bond(AL, "YES", 50.0, 0.97)                  # a later lot changes nothing
        self.assertEqual(self.b.more_cap[AL]["usd"], 98.0)
        r = self.b.set_more_cap(AL, 250)
        self.assertTrue(r["ok"])
        self.assertEqual((self.b.more_cap[AL]["usd"], self.b.more_cap[AL]["by"]), (250.0, "owner"))
        self.assertFalse(self.b.set_more_cap(GA, 10)["ok"])
        self.b.lots.clear()                               # no longer held
        self.b.cycle(self.now, self.positions(), on=False)
        self.assertNotIn(AL, self.b.more_cap)
        self.bond(AL, "YES", 20.0, 0.99)                  # a new first purchase: a new default
        self.assertEqual(self.b.more_cap[AL]["usd"], 19.8)

    def test_it_rests_at_the_cheapest_price_that_captures_30_percent(self):
        self.bond(AL, "YES", 100.0, 0.98)
        before = self.r.cache.fresh(AL, 120.0, self.now)
        slot = self.b._more_slot(AL, "YES", before, 98.0)
        self.assertIsNotNone(slot)
        px, qty, share, est = slot
        self.b.cycle(self.now, self.positions(), on=True)
        book = self.with_ours(AL, self.now + 1)               # the exchange now shows our bid too
        self.assertGreaterEqual(share, 0.30)
        self.assertEqual(qty, float(int(98.0 / px)))
        # nothing cheaper captures 30%
        for p in [c for c in (round(px - k * 0.01, 2) for k in range(1, 4)) if c > 0]:
            q = float(int(98.0 / p))
            self.assertLess(self.b._share_at(AL, "BUY", book, p, q)[0], 0.30)
        o = self.more(AL)
        self.assertEqual(len(o), 1)
        self.assertEqual((o[0].side, o[0].price, o[0].qty), ("BUY", px, qty))
        self.assertTrue(o[0].why.startswith("bond more"))
        self.assertLess(o[0].price, book.asks[0][0])       # inside the spread: post-only
        v = self.b.view(self.now + 1, self.positions())["rows"][0]["more"]
        self.assertEqual((v["cap_usd"], v["order"]["price"]), (98.0, px))
        self.assertGreaterEqual(v["order"]["share"], 0.30)
        # a fill on it books through the record, at any time while it rests
        oid = o[0].id
        self.assertTrue(self.b.fill_book[oid]["open"])
        self.r.exchange.recent_trades = lambda limit=25: TestFiveExecutionsAreFive.rows(oid, 3, px)
        self.exch(AL, 103.0, 0.98)                          # the exchange shows the 3 too
        self.with_ours(AL, self.now + 5000)
        self.b.cycle(self.now + 5000, self.positions(), on=True)
        self.assertEqual(self.b.held(AL, "YES"), 103.0)
        self.assertIn("bought_more", [e["event"] for e in self.b.log])

    def test_a_no_bond_buys_more_on_the_ask_side(self):
        self.bond(ALD, "NO", 100.0, 0.02)                 # 100 NO @ 98c
        self.b.cycle(self.now, self.positions(), on=True)
        o = self.more(ALD)
        self.assertEqual(len(o), 1)
        self.assertEqual(o[0].side, "SELL")
        self.assertGreater(o[0].price, 0.01)               # above the 1c bid
        self.assertLessEqual(1.0 - o[0].price, 0.995)      # inside the price cap

    def test_nothing_rests_when_no_price_captures_30_percent(self):
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.set_more_cap(AL, 0.5)                       # not even one share
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertEqual(self.more(AL), [])
        self.assertEqual(self.b.view(self.now)["rows"][0]["more"]["order"], None)

    def test_it_moves_when_it_no_longer_captures_30_percent(self):
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        o = self.more(AL)[0]
        # a big competitor joins at our price: our share collapses
        self.with_ours(AL, self.now + 60, extra_bids=[(o.price, 5000.0)])
        self.b.cycle(self.now + 60, self.positions(), on=True)   # inside the cooldown: stays
        self.assertIn(o.id, self.r.fam.orders)
        self.with_ours(AL, self.now + 1900, extra_bids=[(o.price, 5000.0)])
        self.b.cycle(self.now + 1900, self.positions(), on=True)  # cooldown over: moves or comes off
        self.assertNotIn(o.id, self.r.fam.orders)
        ev = [e["event"] for e in self.b.log]
        self.assertTrue("more_moved" in ev or "more_pulled" in ev)
        now_o = self.more(AL)
        if now_o:
            self.assertGreaterEqual(
                self.b._share_at(AL, "BUY", self.r.cache.fresh(AL, 120.0, self.now + 1900),
                                 now_o[0].price, now_o[0].qty)[0], 0.30)

    def test_the_graph_gets_the_bond_rate(self):
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        for o in self.r.fam.orders.values():
            if o.purpose == "bond":
                o.live_est = 0.5
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(self.b.dots[-1][0], self.now + 60)
        self.assertAlmostEqual(self.b.dots[-1][1], 0.5 * len(self.b._orders(AL)), places=2)
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        b2.restore(self.b.to_dict())
        self.assertEqual(b2.dots, self.b.dots)
        self.assertEqual(b2.more_cap[AL]["usd"], 98.0)


class TestCommissionsInTheCost(Base):
    """Owner, 2026-09-03: commissions must count — then "Never mind on
    the new price floor ... Holding at cost is fine." So a take's
    commission is in the cost shown and in the profit math, and the
    exit's floor stays the price paid."""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.approve(ALD, self.now)
        self.b.set_budget(1000.0)

    def test_the_takes_commission_is_in_the_cost_and_the_money(self):
        real = self.r.exchange.post
        holder = {}

        def post(url, body, **k):
            out = real(url, body, **k)
            holder["oid"] = out["order"]["id"]
            return out
        self.r.exchange.post = post

        def rows(limit=25):
            rs = TestFiveExecutionsAreFive.rows(holder.get("oid", "?"), 5)
            for r in rs:
                r["trade"]["aggressorExecution"]["commissionNotionalCollected"] = {"value": "0.0600"}
            return rs
        self.r.exchange.recent_trades = rows
        self.r.cache.put(AL, yes_book(self.now, ask=0.99, ask_q=5.0))
        r = self.b.enter(AL, 0.99, self.now, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertAlmostEqual(self.b.lots[AL]["cost"], 5 * 0.99, places=4)
        self.assertAlmostEqual(self.b.lots[AL]["fees"], 0.30, places=4)
        self.assertAlmostEqual(self.b.cost_basis(AL, "YES"), 0.99 + 0.06, places=4)   # shown, fees in
        self.assertAlmostEqual(self.b.price_basis(AL, "YES"), 0.99, places=4)         # the floor
        self.assertAlmostEqual(self.b.budget, 1000 - 5 * 0.99 - 0.30, places=2)
        # a sale by our order measures profit from the cost with fees
        self.assertAlmostEqual(self.b._unbook_lot(AL, "YES", 5.0), 5 * 0.99 + 0.30, places=4)

    def test_holding_at_cost_is_fine(self):
        self.bond(AL, "YES", 100.0, 0.98)
        self.assertEqual(self.b._bound(AL, "YES", 0.01), 0.98)
        self.assertEqual(self.b.view(self.now, self.positions())["rows"][0]["floor"], 0.98)
        self.bond(ALD, "NO", 100.0, 0.03)
        self.assertEqual(self.b._bound(ALD, "NO", 0.01), 0.03)
        self.assertEqual(self.b.view(self.now, self.positions())["rows"][1]["floor"], 0.97)


class TestBuyMoreNeverDearerThanTheFirstPrice(Base):
    """Owner, 2026-09-03: "the buy price cap is also the price I
    originally bought at. Not 99.5. I never want to buy that high.\""""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.approve(ALD, self.now)
        self.b.set_budget(1000.0)

    def test_a_yes_bond_never_bids_above_its_first_price(self):
        self.bond(AL, "YES", 100.0, 0.97)                 # first bought at 97c; book 98/99
        self.assertEqual(self.b.more_cap[AL]["px"], 0.97)
        book = self.r.cache.fresh(AL, 120.0, self.now)
        slot = self.b._more_slot(AL, "YES", book, 97.0)
        if slot:
            self.assertLessEqual(slot[0], 0.97 + 1e-9)
        self.b.cycle(self.now, self.positions(), on=True)
        for o in self.b._more_orders(AL):
            self.assertLessEqual(o.price, 0.97 + 1e-9)
        v = self.b.view(self.now, self.positions())["rows"][0]["more"]
        self.assertEqual(v["cap_px"], 0.97)
        self.b.set_more_cap(AL, 500)                      # the amount changes, the price cap does not
        self.assertEqual(self.b.more_cap[AL]["px"], 0.97)

    def test_a_no_bond_never_pays_more_than_its_first_price(self):
        self.bond(ALD, "NO", 100.0, 0.03)                 # NO at 97c: asks must be 3c or higher
        self.assertEqual(self.b.more_cap[ALD]["px"], 0.03)
        self.r.cache.put(ALD, no_book(self.now, bid=0.01, ask=0.02))
        self.b.cycle(self.now, self.positions(), on=True)
        for o in self.b._more_orders(ALD):
            self.assertGreaterEqual(o.price, 0.03 - 1e-9)
        v = self.b.view(self.now, self.positions())["rows"]
        row = [r for r in v if r["market"] == ALD][0]
        self.assertEqual(row["more"]["cap_px"], 0.97)

    def test_a_lot_from_before_the_price_was_kept_uses_the_price_paid(self):
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.more_cap[AL].pop("px")                     # state from the earlier deploy
        self.assertEqual(self.b._first_px(AL), 0.98)


class TestTheArkansasLesson(Base):
    """Owner, 2026-09-03, with the official app's book: "my orders are
    resting beyond where they need to be to earn 60%. So either the
    quantity should be reduced so that I'm reserving some to maturity
    when I would actually get the coupon or I should move the shares
    back so that if they are filled I turn a profit. Holding all of
    them out there like that does no good."""

    def setUp(self):
        super().setUp()
        self.b.approve(ALD, self.now)
        self.b.set_budget(1000.0)
        # the NO side of the official app, in YES terms
        self.book = Book(bids=((0.03, 50.0), (0.02, 2080.0), (0.01, 4000.0)),
                         asks=((0.06, 542.0), (0.09, 1.0), (0.14, 50.0),
                               (0.19, 50.0), (0.99, 4025.0)),
                         tick=0.01, fetched_at=self.now)
        self.seed(ALD, self.book)
        self.r.fam.refresh_terms(self.r.exchange, self.r.now)
        self.bond(ALD, "NO", 195.0, 0.05)                 # 195 NO at 95c
        self.b.more_cap[ALD]["usd"] = 0.0

    def test_the_whole_lot_rests_at_the_slot_that_keeps_60_percent(self):
        # first: "either the quantity should be reduced ... or I should
        # move the shares back"; then: "You don't have to reserve any
        # shares to maturity" — so the price moves, the lot stays whole
        out = self.b.cycle(self.now, self.positions(), on=True)
        bids = self.orders(ALD, "BUY", decoy=False)
        self.assertEqual(len(bids), 1)
        o = bids[0]
        self.assertLessEqual(o.price, 0.05 + 1e-9)        # never over cost
        self.assertEqual(o.qty, 195.0)                    # everything held is offered
        slot = self.b.slot[ALD]
        self.assertEqual(o.qty, slot["size"])
        self.assertGreaterEqual(slot["keep"], 0.6)
        # the whole lot at cost would be the best; the slot keeps 60% of it
        prog = self.r.fam.terms.get(ALD)
        pool = self.r.fam._side_pool(ALD, prog)
        lv = self.b._levels_net(ALD, "BUY", self.book)
        from v3.scoring import estimate_join
        full = estimate_join("BUY", lv, 0.01, float(prog.df), float(prog.target), 0.05, 195.0)
        here = estimate_join("BUY", lv, 0.01, float(prog.df), float(prog.target), o.price, 195.0)
        self.assertGreaterEqual(here.share * pool, 0.6 * full.share * pool - 1e-9)
        # one exit, cycle after cycle
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(len(self.orders(ALD, "BUY", decoy=False)), 1)


class TestTheEngineIsOutOfHeldBondMarkets(Base):
    """Owner, 2026-09-03: "clear the engine out of bond markets after I
    place my first order and until I no longer hold any position in
    that market. It's just too confusing with the engine and the
    bonds.\""""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)

    def engine_order(self, oid, side, px, qty, purpose="earn"):
        self.r.fam.orders[oid] = FamilyOrder(
            id=oid, market=AL, side=side, price=px, qty=qty,
            intent=BUY_LONG if side == "BUY" else SELL_LONG,
            placed_ts=self.now, purpose=purpose)
        self.r.exchange.live[oid] = {"id": oid, "market": AL, "side": side,
                                     "price": px, "size": qty, "intent": ""}

    def test_the_first_purchase_clears_the_engine_out_and_counts_its_stock(self):
        self.engine_order("E1", "BUY", 0.97, 20.0)
        self.engine_order("E2", "SELL", 0.99, 30.0, purpose="sell")
        self.engine_order("H1", "BUY", 0.96, 5.0, purpose="manual")   # his hand order stays
        self.exch(AL, 30.0, 0.95)                          # the engine's own 30 @ 95c
        self.r.fam.inventory[AL] = {"qty": 30.0, "cost": 28.5}
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertFalse(self.r.fam._frozen(AL))          # nothing held yet: the engine quotes
        self.assertIn("E1", self.r.fam.orders)
        # his first purchase
        self.b._book_lot(AL, "YES", 100.0, 98.0, ref="T1")
        self.exch(AL, 130.0, 0.955)
        self.r.fam.inventory[AL] = {"qty": 130.0, "cost": 126.5}
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertTrue(self.r.fam._frozen(AL))
        self.assertIn(AL, self.b.engine_out)
        self.assertNotIn("E1", self.r.fam.orders)
        self.assertNotIn("E2", self.r.fam.orders)
        self.assertNotIn("E1", self.r.exchange.live)
        self.assertIn("H1", self.r.fam.orders)             # untouched
        self.assertEqual(self.b.held(AL, "YES"), 130.0)    # the engine's 30 count as bond now
        self.assertIn("adopt", self.b.lots[AL]["fills"])
        ev = {e["event"] for e in self.b.log}
        self.assertIn("engine_cleared", ev)
        self.assertIn("adopted", ev)
        # the freeze survives a restore
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        self.r.fam.freeze_dyn.clear()
        b2.restore(self.b.to_dict())
        self.assertTrue(self.r.fam._frozen(AL))

    def test_the_engine_comes_back_when_nothing_is_held(self):
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertTrue(self.r.fam._frozen(AL))
        self.b.lots.clear()                                # sold out
        self.exch(AL, 0.0, 0.0)
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertFalse(self.r.fam._frozen(AL))
        self.assertNotIn(AL, self.b.engine_out)
        self.assertIn("engine_back", {e["event"] for e in self.b.log})

    def test_a_hand_purchase_is_counted_after_the_grace_unless_a_fill_is_pending(self):
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.cycle(self.now, self.positions(), on=True)
        self.exch(AL, 140.0, 0.98)                         # 40 more, bought by hand
        self.r.fam.inventory[AL] = {"qty": 140.0, "cost": 137.2}
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(self.b.held(AL, "YES"), 100.0)    # not yet: the grace
        self.b.cycle(self.now + 120, self.positions(), on=True)
        self.b.cycle(self.now + 400, self.positions(), on=True)
        self.assertEqual(self.b.held(AL, "YES"), 140.0)
        # an excess that keeps changing (a bond order filling in parts)
        # restarts the grace each time; the record books those fills
        self.exch(AL, 150.0, 0.98)
        self.b.cycle(self.now + 800, self.positions(), on=True)
        self.exch(AL, 155.0, 0.98)
        self.b.cycle(self.now + 900, self.positions(), on=True)
        self.b.cycle(self.now + 1150, self.positions(), on=True)
        self.assertEqual(self.b.held(AL, "YES"), 140.0)
        self.b.cycle(self.now + 1300, self.positions(), on=True)
        self.assertEqual(self.b.held(AL, "YES"), 155.0)


class TestFreshBooks(Base):
    """Owner, 2026-09-03: "A lot of the books are stale. What's up with
    that." The stream sends a book only when it changes; a listed
    market nothing else works was never read at all."""

    def setUp(self):
        super().setUp()
        for slug in (AL, TN, ALD, GA):
            self.b.approve(slug, self.now) if slug != GA else None

    def test_old_listed_books_are_read_again_a_few_per_cycle(self):
        old = self.now - 1000.0
        for slug in (AL, TN, ALD):
            b = self.r.cache.any_age(slug)
            self.r.cache.put(slug, Book(bids=b.bids, asks=b.asks, tick=b.tick, fetched_at=old))
        self.r.cache.put(GA, Book(bids=((0.5, 1.0),), asks=((0.6, 1.0),), tick=0.01, fetched_at=old))
        n = self.b._refresh_books(self.now)
        self.assertEqual(n, 3)                                    # the listed ones, not GA
        for slug in (AL, TN, ALD):
            self.assertEqual(self.r.cache.any_age(slug).fetched_at, self.now)
        self.assertEqual(self.r.cache.any_age(GA).fetched_at, old)
        # fresh enough: nothing read
        self.assertEqual(self.b._refresh_books(self.now + 60), 0)
        # never more than a few per cycle, oldest first
        for i, slug in enumerate((AL, TN, ALD)):
            b = self.r.cache.any_age(slug)
            self.r.cache.put(slug, Book(bids=b.bids, asks=b.asks, tick=b.tick,
                                        fetched_at=self.now - 400 - i))
        import v3.bonds as bm
        keep = bm.BOOK_READS_PER_CYCLE
        bm.BOOK_READS_PER_CYCLE = 1
        try:
            self.assertEqual(self.b._refresh_books(self.now + 100), 1)
            self.assertEqual(self.r.cache.any_age(ALD).fetched_at, self.now + 100)   # the oldest
            self.assertEqual(self.r.cache.any_age(AL).fetched_at, self.now - 400)
        finally:
            bm.BOOK_READS_PER_CYCLE = keep
        v = self.b.view(self.now + 100)
        self.assertFalse(any(r["stale"] for r in v["rows"]))


class TestBait(Base):
    """Owner, 2026-09-03: "let me see both sides of the book and place
    bait orders on the buy side (only one share) to entice people to
    move their buy offers some.\""""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.approve(ALD, self.now)
        self.b.set_budget(1000.0)
        self.bond(AL, "YES", 100.0, 0.98)                # book 98 / 99: no room for bait
        self.bond(ALD, "NO", 100.0, 0.05)                # NO at 95c

    def test_the_book_shows_both_sides_in_the_bonds_own_terms(self):
        self.r.cache.put(ALD, Book(bids=((0.05, 195.0), (0.03, 50.0), (0.02, 2080.0)),
                                   asks=((0.06, 542.0), (0.09, 1.0)),
                                   tick=0.01, fetched_at=self.now))
        self.r.fam.orders["X1"] = FamilyOrder(id="X1", market=ALD, side="BUY", price=0.05,
                                              qty=195.0, intent=SELL_SHORT, placed_ts=self.now,
                                              purpose="bond")
        row = [r for r in self.b.view(self.now, self.positions())["rows"] if r["market"] == ALD][0]
        bk = row["book"]
        self.assertEqual(bk["terms"], "NO")
        self.assertEqual(bk["bids"][0], [0.94, 542.0, 0.0])      # NO bids = YES asks, 100 − 6
        self.assertEqual(bk["asks"][0], [0.95, 195.0, 195.0])    # our exit, marked ours
        self.assertEqual(bk["asks"][1][0], 0.97)

    def test_a_bait_rests_one_share_a_tick_inside_their_best(self):
        self.r.cache.put(ALD, Book(bids=((0.05, 195.0), (0.03, 50.0)),
                                   asks=((0.08, 542.0), (0.09, 1.0)),
                                   tick=0.01, fetched_at=self.now))
        r = self.b.place_bait(ALD, self.now, self.positions())
        self.assertTrue(r["ok"], r["note"])
        o = self.b._bait_orders(ALD)[0]
        self.assertEqual((o.side, o.price, o.qty), ("SELL", 0.07, 1.0))   # a YES ask at 7c: buying NO at 93c
        self.assertTrue(self.b.fill_book[o.id]["open"])
        st = self.b.view(self.now, self.positions())
        bt = [x for x in st["rows"] if x["market"] == ALD][0]["bait"]
        self.assertTrue(bt["resting"])
        self.assertEqual(bt["px"], 0.07)
        self.assertFalse(self.b.place_bait(ALD, self.now)["ok"])   # one at a time
        # they follow: others show up at the bait's price — it comes off
        self.r.cache.put(ALD, Book(bids=((0.05, 195.0), (0.03, 50.0)),
                                   asks=((0.07, 301.0), (0.08, 241.0)),
                                   tick=0.01, fetched_at=self.now + 60))
        self.b.cycle(self.now + 60, self.positions(), on=False)
        self.assertEqual(self.b._bait_orders(ALD), [])
        self.assertEqual(self.b.bait[ALD]["followed"], 1)
        self.assertIn("followed", self.b.bait[ALD]["note"])
        # the next step, when he taps again
        r = self.b.place_bait(ALD, self.now + 120, self.positions())
        self.assertTrue(r["ok"], r["note"])
        self.assertEqual(self.b._bait_orders(ALD)[0].price, 0.06)
        self.assertEqual(self.b.bait[ALD]["steps"], 2)

    def test_no_room_and_the_first_price_cap(self):
        r = self.b.place_bait(AL, self.now, self.positions())      # bid 98 / ask 99: no tick inside
        self.assertFalse(r["ok"])
        self.assertIn("no room", r["note"])
        # a NO bond first bought at 5c: a bait at 4c would pay 96c, more than his 95c
        self.r.cache.put(ALD, Book(bids=((0.02, 195.0),), asks=((0.05, 542.0),),
                                   tick=0.01, fetched_at=self.now))
        r = self.b.place_bait(ALD, self.now, self.positions())
        self.assertFalse(r["ok"])
        self.assertIn("first price", r["note"])

    def test_nobody_follows_for_two_hours_and_it_comes_off(self):
        self.r.cache.put(ALD, Book(bids=((0.05, 195.0),), asks=((0.08, 542.0),),
                                   tick=0.01, fetched_at=self.now))
        self.assertTrue(self.b.place_bait(ALD, self.now, self.positions())["ok"])
        self.b.cycle(self.now + 3600, self.positions(), on=False)
        self.assertEqual(len(self.b._bait_orders(ALD)), 1)
        self.b.cycle(self.now + 7300, self.positions(), on=False)
        self.assertEqual(self.b._bait_orders(ALD), [])
        self.assertIn("nobody followed", self.b.bait[ALD]["note"])
        self.assertTrue(self.b.pull_bait(ALD)["ok"] is False)     # nothing left to pull


class TestTheHeadline(Base):
    """Owner, 2026-09-03: "Give me a top line number at the top for all
    the bonds. The amount invested and the percentage return to date.\""""

    def test_invested_and_return_to_date(self):
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)
        self.b._book_lot(AL, "YES", 100.0, 90.0, ref="T1", fee=0.5)      # $90.50 in, fees in
        self.b._pay(90.5)
        self.b.fill_book["T1"] = {"slug": AL, "side": "YES", "qty": 100.0, "px": 0.90,
                                  "fee": 0.5, "ts": self.now - 10 * 86400}
        e = self.b._earned()
        self.assertEqual(e["invested"], 90.5)
        self.assertEqual(e["deployed"], 90.5)
        self.assertEqual(e["return_pct"], 0.0)
        self.assertEqual(round(e["days"]), 10)
        # rewards accrue and a sale of 40 at 95c takes profit
        self.b.accrued["2026-09-03"] = 1.81
        self.b.realized = round(40 * (0.95 - 0.905), 4)                  # 1.80
        self.b.sold_usd = 40 * 0.95
        self.b.cash = 40 * 0.95
        self.b._unbook_lot(AL, "YES", 40.0)
        e = self.b._earned()
        self.assertAlmostEqual(e["invested"], 60 * 0.905, places=2)       # what is still in
        self.assertAlmostEqual(e["deployed"], 90.5, places=2)             # everything put in
        self.assertAlmostEqual(e["total"], 3.61, places=2)
        self.assertAlmostEqual(e["return_pct"], 3.61 / 90.5, places=4)
        self.assertAlmostEqual(e["annual_pct"], 3.61 / 90.5 * 365 / 10, places=3)
        v = self.b.view(self.now)
        self.assertEqual(v["earned"]["invested"], e["invested"])
        # buying with the $38 of proceeds is not new money: "put in" stays
        self.b._book_lot(TN, "YES", 40.0, 38.0, ref="T2")
        self.b._pay(38.0)
        e = self.b._earned()
        self.assertAlmostEqual(e["invested"], 60 * 0.905 + 38.0, places=2)
        self.assertAlmostEqual(e["deployed"], 90.5, places=2)
        # but buying from the budget is
        self.b._book_lot(GA, "YES", 10.0, 7.0, ref="T3")
        self.b._pay(7.0)
        self.assertAlmostEqual(self.b._earned()["deployed"], 97.5, places=2)
        # an older state, or one seeded the wrong way, is re-seeded from
        # what holds: held + proceeds waiting − profit taken
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        d = self.b.to_dict()
        d["money_in"] = 999.0
        d.pop("money_in_v")
        b2.restore(d)
        inv = b2._earned()["invested"]
        self.assertAlmostEqual(b2.money_in, inv + b2.cash - b2.realized, places=2)
        self.assertAlmostEqual(b2.money_in, self.b.money_in, places=2)   # the same as the live figure
        b3 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s))
        b3.restore(self.b.to_dict())                                     # current state: kept as is
        self.assertAlmostEqual(b3.money_in, self.b.money_in, places=4)


class TestInTheBlack(Base):
    """Owner, 2026-09-03: "highlight markets on the bond page where the
    bid price has moved above my average cost so I know when they are
    in the black.\""""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.approve(ALD, self.now)
        self.b.set_budget(1000.0)

    def test_a_yes_bond_against_the_best_bid_others_have(self):
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.lots[AL]["fees"] = 0.5                       # cost with fees: 98.5c
        self.r.cache.put(AL, yes_book(self.now, bid=0.98, ask=0.99))
        m = self.b.view(self.now, self.positions())["rows"][0]["mark"]
        self.assertEqual((m["bid"], m["cost"], m["black"]), (0.98, 0.985, False))
        self.r.cache.put(AL, yes_book(self.now, bid=0.99, ask=0.995))
        m = self.b.view(self.now, self.positions())["rows"][0]["mark"]
        self.assertTrue(m["black"])
        self.assertAlmostEqual(m["edge"], 0.005, places=4)
        # our own bid does not count: nobody can sell to himself
        self.r.fam.orders["B1"] = FamilyOrder(id="B1", market=AL, side="BUY", price=0.99,
                                              qty=300.0, intent=BUY_LONG, placed_ts=self.now,
                                              purpose="bond")
        self.r.cache.put(AL, Book(bids=((0.99, 300.0), (0.98, 50.0)), asks=((0.995, 10.0),),
                                  tick=0.01, fetched_at=self.now))
        m = self.b.view(self.now, self.positions())["rows"][0]["mark"]
        self.assertEqual((m["bid"], m["black"]), (0.98, False))

    def test_a_no_bond_reads_the_no_bid_off_the_yes_asks(self):
        self.bond(ALD, "NO", 100.0, 0.05)                   # NO at 95c
        self.r.cache.put(ALD, no_book(self.now, bid=0.03, ask=0.06))   # NO bid = 94c
        rows = self.b.view(self.now, self.positions())["rows"]
        m = [r for r in rows if r["market"] == ALD][0]["mark"]
        self.assertEqual((m["bid"], m["black"]), (0.94, False))
        self.r.cache.put(ALD, no_book(self.now, bid=0.03, ask=0.04))   # NO bid = 96c
        m = [r for r in self.b.view(self.now, self.positions())["rows"] if r["market"] == ALD][0]["mark"]
        self.assertEqual((m["bid"], m["black"]), (0.96, True))
        self.assertIsNone([r for r in self.b.view(self.now, self.positions())["rows"] if r["market"] == AL][0]["mark"])


class TestAContingentMovesUp(Base):
    """Owner, 2026-09-03: "shouldn't a contingent of my orders resting a
    step back move up since I'm not at 60%." The Maryland exit sat a
    tick behind a dust order, earning 14% against 63% at the touch."""

    def setUp(self):
        super().setUp()
        self.b.approve(ALD, self.now)
        self.b.set_budget(1000.0)
        self.b.more_cap[ALD] = {"usd": 0.0, "by": "owner", "first": "", "px": 0.05}
        self.bond(ALD, "NO", 265.0, 0.05)                    # NO at 95c: the cover bid may sit up to 5c
        self.book = Book(bids=((0.04, 5.0), (0.03, 500.0), (0.02, 8000.0), (0.01, 4000.0)),
                         asks=((0.05, 430.0), (0.08, 7.0)),
                         tick=0.01, fetched_at=self.now)
        self.seed(ALD, self.book)
        self.r.fam.refresh_terms(self.r.exchange, self.r.now)

    def test_the_exit_steps_up_when_it_no_longer_keeps_60_percent(self):
        # the exit was left a tick behind the touch with the whole lot
        self.r.fam.orders["X1"] = FamilyOrder(id="X1", market=ALD, side="BUY", price=0.03,
                                              qty=256.0, intent=SELL_SHORT, placed_ts=self.now,
                                              purpose="bond", why="bond: resting")
        self.r.exchange.live["X1"] = {"id": "X1", "market": ALD, "side": "BUY", "price": 0.03,
                                      "size": 256.0, "intent": SELL_SHORT}
        self.b.moved_at[ALD] = self.now - 3600                  # the cooldown is over
        slot = self.b._best_slot(ALD, "NO", self.book, 265.0, self.b._bound(ALD, "NO", 0.01))
        self.assertGreater(slot[0], 0.03)                       # the slot is closer than where it sits
        est_before = self.b._est_at(ALD, "BUY", self.book, 0.03, 256.0)
        best = slot[1] / slot[3]
        self.assertLess(est_before, 0.6 * best)                 # under the target where it sits
        self.b.cycle(self.now, self.positions(), on=True)
        ex = self.orders(ALD, "BUY", decoy=False)
        self.assertEqual(len(ex), 1)
        self.assertAlmostEqual(ex[0].price, slot[0])
        self.assertEqual(ex[0].qty, 265.0)                      # everything, nothing kept back
        self.assertLessEqual(ex[0].price, 0.05 + 1e-9)          # never past cost
        self.assertIn("earn_moved_up", [e["event"] for e in self.b.log])

    def test_an_exit_keeping_its_share_does_not_chase(self):
        # sitting at the slot already: nothing to do
        slot = self.b._best_slot(ALD, "NO", self.book, 265.0, self.b._bound(ALD, "NO", 0.01))
        self.r.fam.orders["X2"] = FamilyOrder(id="X2", market=ALD, side="BUY", price=slot[0],
                                              qty=slot[4], intent=SELL_SHORT, placed_ts=self.now,
                                              purpose="bond", why="bond: resting")
        self.r.exchange.live["X2"] = {"id": "X2", "market": ALD, "side": "BUY", "price": slot[0],
                                      "size": slot[4], "intent": SELL_SHORT}
        self.b.moved_at[ALD] = self.now - 3600
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertIn("X2", self.r.fam.orders)
        self.assertNotIn("earn_moved_up", [e["event"] for e in self.b.log])


class TestTheBuyMoreOrderStepsBack(Base):
    """Owner, 2026-09-03, on Massachusetts (72 alone at 96c, 100% of
    its side, nothing else until 93c): "Couldn't the bid do better by
    standing a little further back." """

    def setUp(self):
        super().setUp()
        self.b.approve(ALD, self.now)
        self.b.set_budget(1000.0)
        self.bond(ALD, "NO", 175.0, 0.04)                    # NO at 96c: buys at 96c or cheaper
        # NO bids (YES asks) in YES terms: nothing until 7c, then a wall
        self.book = Book(bids=((0.03, 2582.0), (0.02, 0.2), (0.01, 2025.0)),
                         asks=((0.07, 25.0), (0.08, 3055.0), (0.09, 50.0), (0.99, 2025.0)),
                         tick=0.01, fetched_at=self.now)
        self.seed(ALD, self.book)
        self.r.fam.refresh_terms(self.r.exchange, self.r.now)

    def rest_more_at(self, px, qty):
        self.r.fam.orders["M1"] = FamilyOrder(id="M1", market=ALD, side="SELL", price=px,
                                              qty=qty, intent=BUY_SHORT, placed_ts=self.now,
                                              purpose="bond", why="bond more: buying")
        self.r.exchange.live["M1"] = {"id": "M1", "market": ALD, "side": "SELL", "price": px,
                                      "size": qty, "intent": BUY_SHORT}

    def test_it_steps_back_to_the_cheapest_price_that_still_captures_the_share(self):
        self.rest_more_at(0.04, 72.0)                        # at his first price, 100% of the side
        self.b.more_cap[ALD] = {"usd": 69.12, "by": "owner", "first": "", "px": 0.04}
        slot = self.b._more_slot(ALD, "NO", self.book, 69.12)
        self.assertIsNotNone(slot)
        self.assertGreater(slot[0], 0.04)                    # a cheaper NO (higher YES ask) still captures 30%
        self.b.moved_more_at[ALD] = self.now - 60            # inside the cooldown: stays
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertIn("M1", self.r.fam.orders)
        self.b.moved_more_at[ALD] = self.now - 3600          # cooldown over: steps back
        self.b.cycle(self.now + 1, self.positions(), on=True)
        cur = self.b._more_orders(ALD)
        self.assertEqual(len(cur), 1)
        self.assertAlmostEqual(cur[0].price, slot[0])
        self.assertGreaterEqual(self.b._share_at(ALD, "SELL", self.book, cur[0].price, cur[0].qty)[0], 0.30)
        ev = [e for e in self.b.log if e["event"] == "more_pulled"]
        self.assertIn("stepping back", ev[-1]["note"])

    def test_already_at_the_cheapest_slot_it_stays(self):
        slot = self.b._more_slot(ALD, "NO", self.book, 69.12)
        self.b.more_cap[ALD] = {"usd": 69.12, "by": "owner", "first": "", "px": 0.04}
        self.rest_more_at(slot[0], slot[1])
        self.b.moved_more_at[ALD] = self.now - 3600
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertIn("M1", self.r.fam.orders)
        self.assertNotIn("more_pulled", [e["event"] for e in self.b.log])


class TestADecoyNeverRestsUnderCost(Base):
    """Idaho, 2026-09-03: a decoy joined 0.01 share at 84c against a 94c
    cost, was filled, and sold ten shares at a ten-cent loss. A decoy
    is an exit order too."""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)
        self.b.more_cap[AL] = {"usd": 0.0, "by": "owner", "first": "", "px": 0.94}
        # cost 94c; the exit will rest at or above it
        self.r.cache.put(AL, Book(bids=((0.80, 50.0), (0.50, 20000.0)),
                                  asks=((0.96, 100.0), (0.99, 20000.0)),
                                  tick=0.01, fetched_at=self.now))
        self.bond(AL, "YES", 100.0, 0.94)
        self.b.cycle(self.now, self.positions(), on=True)
        self.main = self.orders(AL, "SELL", decoy=False)[0]
        self.assertGreaterEqual(self.main.price, 0.94)

    def book(self, front_px, front_q, t):
        asks = {round(o.price, 4): o.qty for o in self.orders(AL, "SELL")}
        asks[round(front_px, 4)] = asks.get(round(front_px, 4), 0.0) + front_q
        asks[0.99] = asks.get(0.99, 0.0) + 20000.0
        self.r.cache.put(AL, Book(bids=((0.80, 50.0), (0.50, 20000.0)),
                                  asks=tuple(sorted(asks.items())), tick=0.01,
                                  fetched_at=self.now + t))

    def test_dust_under_cost_gets_no_decoy(self):
        self.book(0.84, 0.01, 60)                            # dust ten ticks under cost
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])
        ev = [e for e in self.b.log if e["event"] == "dance_idle"]
        self.assertEqual(len(ev), 1)
        self.assertIn("under our cost", ev[0]["note"])
        self.assertNotIn("snapped", [e["event"] for e in self.b.log])

    def test_a_real_minnow_under_cost_is_taken_not_joined(self):
        self.book(0.90, 5.0, 60)                             # 5 shares under cost: bought at once
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(self.orders(AL, "SELL", decoy=True), [])
        self.assertIn("snapped", [e["event"] for e in self.b.log])


class TestLeavingTheBand(Base):
    """Owner, 2026-09-04: "if a market is removed from the bond program
    because the silver bulletin no longer shows it as > 99%. Leave the
    position on in the bond page, just make it clear that the odds have
    changed and don't show it again once I have exited that market
    fully until the odds are back in range"."""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.set_more_cap(AL, 50.0)
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertTrue(self.orders(AL, "SELL"))
        self.assertTrue(self.orders(AL, "BUY"))              # the buy-more rests
        self.odds[AL] = 0.984
        self.b.scan(self.now + 1, force=True)
        self.assertNotIn(AL, self.b.approved)

    def test_the_held_position_stays_on_the_page_flagged(self):
        self.b.cycle(self.now + 60, self.positions(), on=True)
        rows = self.b.view(self.now + 61, self.positions())["rows"]
        self.assertEqual(rows[0]["market"], AL)
        self.assertTrue(rows[0]["odds_changed"])
        self.assertEqual(rows[0]["qty"], 100.0)
        self.assertAlmostEqual(rows[0]["odds"], 0.984)
        self.assertIn(AL, self.b.live_rows(self.now + 61, self.positions()))
        self.assertTrue(self.r.fam._frozen(AL))               # the engine stays out

    def test_the_exit_keeps_working_and_nothing_new_is_bought(self):
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertTrue(self.orders(AL, "SELL"))
        self.assertEqual(self.orders(AL, "BUY"), [])          # the buy-more came off
        ev = [e for e in self.b.log if e["event"] == "more_pulled"]
        self.assertIn("left the band", ev[-1]["note"])
        self.assertIn("no new buying", self.b.enter(AL, 0.98, self.now + 60)["note"])
        self.assertIn("no new buying", self.b.place_bait(AL, self.now + 60,
                                                         self.positions())["note"])
        more = self.b.view(self.now + 61, self.positions())["rows"][0]["more"]
        self.assertIn("left the band", more["paused"])

    def test_it_leaves_the_page_once_he_is_out(self):
        self.b.lots.clear()
        self.exch(AL, 0.0, 0.0)
        self.b.cycle(self.now + 60, self.positions(), on=True)
        v = self.b.view(self.now + 61, self.positions())
        self.assertEqual([r["market"] for r in v["rows"]], [])
        self.assertEqual(v["dropped"][0]["market"], AL)
        # back in range: proposed again, for him to add
        self.odds[AL] = 0.995
        self.assertIn(AL, self.b.scan(self.now + 120, force=True))

    def test_back_in_range_while_held_it_is_a_bond_again(self):
        self.odds[AL] = 0.995
        self.b.scan(self.now + 120, force=True)
        self.assertIn(AL, self.b.approved)
        self.assertNotIn(AL, self.b.dropped)
        self.assertIn("back_in_band", [e["event"] for e in self.b.log])
        self.b.cycle(self.now + 180, self.positions(), on=True)
        self.assertTrue(self.orders(AL, "BUY"))              # buying more again


class TestTheLedgerIsRebuiltFromTheExchange(Base):
    """2026-09-04: one short position read wrote off 32 markets. The
    holdings come from the feed and their cost from the transaction
    record (owner: "The bonds list should be written from my positions
    from the api. The transactions give the cost basis")."""

    def setUp(self):
        super().setUp()
        from v3.main import parse_activities
        self.b.parse = parse_activities
        self.b.approve(AL, self.now)
        self.b.approve(ALD, self.now)
        self.b.set_budget(1000.0)

    def test_an_empty_ledger_claims_what_the_exchange_holds_at_the_records_cost(self):
        self.r.exchange.trades.append(self.trade_row("B1", BUY_LONG, 0.97, 100.0,
                                                     self.now - 200, commission=0.5))
        self.r.exchange.trades.append(self.trade_row("B2", BUY_SHORT, 0.02, 50.0,
                                                     self.now - 150, market=ALD))
        self.bond(AL, "YES", 100.0, 0.97)
        self.bond(ALD, "NO", 50.0, 0.02)
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertTrue(self.orders(AL, "SELL") and self.orders(ALD, "BUY"))
        d = self.b.to_dict()
        d["lots"], d["engine_out"], d["more_cap"] = {}, [], {}   # the wipe
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s),
                   clock=lambda: self.r.now, sleep=lambda s: None,
                   parse=self.b.parse)
        self.r.fam.freeze_dyn.clear()
        b2.restore(d)
        self.assertEqual(b2.lots, {})
        b2.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(b2.held(AL, "YES"), 100.0)
        self.assertEqual(b2.held(ALD, "NO"), 50.0)
        self.assertEqual(b2.cost_src[AL], "record")
        self.assertAlmostEqual(b2.price_basis(AL, "YES"), 0.97, places=4)
        self.assertAlmostEqual(b2.lots[AL]["fees"], 0.5, places=4)     # the commission, from the record
        self.assertAlmostEqual(b2.cost_basis(ALD, "NO"), 0.98, places=4)
        self.assertTrue(self.r.fam._frozen(AL) and self.r.fam._frozen(ALD))
        inv = b2._earned()["invested"]
        self.assertAlmostEqual(b2.money_in, inv + b2.cash - b2.realized, places=2)
        ev = {e["event"] for e in b2.log}
        self.assertIn("ledger_rebuilt", ev)
        self.assertIn("adopted", ev)
        self.assertTrue(self.orders(AL, "SELL") and self.orders(ALD, "BUY"))  # the exits
        # the buy-more default is the claimed lot (fees aside); nothing counted twice
        self.assertAlmostEqual(b2.more_cap[AL]["usd"], 97.0, places=2)

    def test_engine_stock_before_his_first_purchase_is_still_uncounted(self):
        self.exch(AL, 500.0, 0.92)                # the engine's own stock, no bond lot
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertEqual(self.b.held(AL, "YES"), 0.0)
        self.assertFalse(self.r.fam._frozen(AL))
        row = self.b.view(self.now, self.positions())["rows"][0]
        self.assertEqual(row["uncounted"], 500.0)

    def test_the_record_walk_prices_a_no_bond_in_its_own_terms(self):
        # bought NO twice (sold YES at 3c and 2c), covered some at 1c
        for row in (self.trade_row("S1", BUY_SHORT, 0.03, 100.0, self.now - 300, market=ALD),
                    self.trade_row("S2", BUY_SHORT, 0.02, 100.0, self.now - 200, market=ALD),
                    self.trade_row("C1", SELL_SHORT, 0.01, 50.0, self.now - 100, market=ALD)):
            self.r.exchange.trades.append(row)
        self.b._refresh_record(self.now, force=True)
        rec = self.b._record_position(ALD, "NO")
        self.assertEqual(rec["qty"], 150.0)
        self.assertAlmostEqual(rec["cost"] / rec["qty"], 0.975, places=4)   # the average NO cost
        self.assertAlmostEqual(rec["realized"], 50 * (0.99 - 0.975), places=4)
        self.assertAlmostEqual(rec["sold_usd"], 50 * 0.99, places=4)
        self.assertEqual(self.b._record_position(ALD, "YES")["qty"], 0.0)


class TestBuyingMoreWithTheMoneyThere(Base):
    """2026-09-04: an order the account could not fund was sent every
    minute; the exchange trimmed it to the money there or killed it,
    and the trimmed remainders sat untracked as strays."""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.set_budget(1000.0)
        self.bond(AL, "YES", 100.0, 0.98)
        self.b.set_more_cap(AL, 98.0)
        self.calls = []
        self._orig = self.r.desk.place_resting

    def buys(self):
        return [o for o in self.orders(AL, "BUY")]

    def test_the_order_is_sized_to_the_buying_power(self):
        self.r.exchange.buying_power = lambda: 20.0
        self.b.cycle(self.now, self.positions(), on=True)
        o = self.buys()[0]
        self.assertEqual(o.qty, float(int(20.0 / o.price)))

    def test_no_buying_power_means_no_order_and_a_wait(self):
        self.r.exchange.buying_power = lambda: 0.4
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertEqual(self.buys(), [])
        self.assertIn("no buying power", self.b._more_note[AL])
        more = self.b.view(self.now + 1, self.positions())["rows"][0]["more"]
        self.assertIsNotNone(more["retry_at"])
        self.r.exchange.buying_power = lambda: 500.0
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(self.buys(), [])                       # still waiting
        self.r.cache.put(AL, yes_book(self.now + 1900))         # a fresh book, later
        self.b.cycle(self.now + 1900, self.positions(), on=True)
        self.assertTrue(self.buys())                            # the wait is over

    def test_a_trimmed_order_is_kept_as_ours(self):
        from v3.orders import OrderResult

        def trimmed(slug, side, price, qty, **kw):
            r = self._orig(slug, side, price, qty, **kw)
            if side == "BUY" and r.ok:
                self.r.exchange.live[r.order_id]["size"] = 40.0
                return OrderResult(ok=False, note="placed but not resting: resting only 40 of "
                                   f"{qty:g}", order_id=r.order_id, intent=r.intent,
                                   price=r.price, resting_qty=40.0)
            return r
        self.r.desk.place_resting = trimmed
        self.b.cycle(self.now, self.positions(), on=True)
        o = self.buys()[0]
        self.assertEqual(o.qty, 40.0)
        self.assertEqual(o.purpose, "bond")
        ev = [e for e in self.b.log if e["event"] == "more_trimmed"]
        self.assertIn("kept 40", ev[0]["note"])
        # the next pass does not stack another on top
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertEqual(len(self.buys()), 1)

    def test_a_refused_order_waits_before_trying_again(self):
        from v3.orders import OrderResult

        def refused(slug, side, price, qty, **kw):
            if side == "BUY":
                self.calls.append(qty)
                return OrderResult(ok=False, note="placed but not resting: order not seen "
                                   "in the open list", order_id="dead1", price=price)
            return self._orig(slug, side, price, qty, **kw)
        self.r.desk.place_resting = refused
        for dt in (0, 60, 120, 180):
            self.b.cycle(self.now + dt, self.positions(), on=True)
        self.assertEqual(len(self.calls), 1)                    # once, then the wait
        self.assertEqual(self.buys(), [])
        self.r.cache.put(AL, yes_book(self.now + 1900))         # a fresh book, later
        self.b.cycle(self.now + 1900, self.positions(), on=True)
        self.assertEqual(len(self.calls), 2)


class TestExitAtHisPrice(Base):
    """Owner, 2026-09-04: "Give me the option to exit a position at a
    given price if it is higher than my cost"."""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.approve(ALD, self.now)
        self.b.set_budget(1000.0)

    def sells(self):
        return self.orders(AL, "SELL")

    def test_the_whole_lot_rests_at_his_price_and_stays(self):
        self.bond(AL, "YES", 100.0, 0.90)
        self.r.cache.put(AL, yes_book(self.now, bid=0.91, ask=0.99))
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertNotEqual(self.sells()[0].price, 0.97)          # the slot logic put it elsewhere
        r = self.b.set_exit(AL, 97, self.now)                     # cents
        self.assertTrue(r["ok"], r["note"])
        self.b.cycle(self.now + 60, self.positions(), on=True)
        asks = self.sells()
        self.assertEqual(len(asks), 1)
        self.assertEqual((asks[0].price, asks[0].qty), (0.97, 100.0))
        self.assertIn("pinned by you", asks[0].why)
        self.r.cache.put(AL, yes_book(self.now + 1000, bid=0.91, ask=0.99))
        self.b.cycle(self.now + 1000, self.positions(), on=True)   # the slot logic stays out
        self.assertEqual(self.sells()[0].price, 0.97)
        row = self.b.view(self.now + 1001, self.positions())["rows"][0]
        self.assertEqual(row["pin"]["bond_px"], 0.97)
        self.assertTrue(row["slot"]["pinned"])

    def test_at_or_under_cost_is_refused(self):
        self.bond(AL, "YES", 100.0, 0.95)
        self.b.lots[AL]["fees"] = 1.0                                 # 96c a share with fees
        r = self.b.set_exit(AL, 96, self.now)
        self.assertFalse(r["ok"])
        self.assertIn("not above your cost", r["note"])
        self.assertTrue(self.b.set_exit(AL, 0.97, self.now)["ok"])   # a fraction works too
        self.assertEqual(self.b.exit_px[AL]["px"], 0.97)

    def test_a_price_under_the_bid_rests_a_tick_over_it(self):
        self.bond(AL, "YES", 100.0, 0.90)
        self.r.cache.put(AL, yes_book(self.now, bid=0.97, ask=0.99))
        self.assertTrue(self.b.set_exit(AL, 95, self.now)["ok"])
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertEqual(self.sells()[0].price, 0.98)                 # better than his 95c, never crossing

    def test_a_no_bond_is_priced_in_its_own_terms(self):
        self.bond(ALD, "NO", 50.0, 0.02)                              # cost 98c NO
        self.assertFalse(self.b.set_exit(ALD, 98, self.now)["ok"])
        self.assertTrue(self.b.set_exit(ALD, 99, self.now)["ok"])     # sell NO at 99c = buy YES at 1c
        self.b.cycle(self.now, self.positions(), on=True)
        bids = self.orders(ALD, "BUY")
        self.assertEqual(len(bids), 1)
        self.assertAlmostEqual(bids[0].price, 0.01, places=4)
        self.assertEqual(bids[0].qty, 50.0)

    def test_clearing_hands_the_exit_back_to_the_slot_logic(self):
        self.bond(AL, "YES", 100.0, 0.90)
        self.r.cache.put(AL, yes_book(self.now, bid=0.91, ask=0.99))
        self.b.set_exit(AL, 96, self.now)
        self.b.cycle(self.now, self.positions(), on=True)
        self.assertEqual(self.sells()[0].price, 0.96)
        self.assertTrue(self.b.clear_exit(AL)["ok"])
        self.assertFalse(self.b.clear_exit(AL)["ok"])
        row = self.b.view(self.now + 1, self.positions())["rows"][0]
        self.assertIsNone(row["pin"])
        self.assertNotIn("exit_px", {k for k in self.b.to_dict()["exit_px"]})

    def test_the_pin_survives_a_restore_and_dies_with_the_position(self):
        self.bond(AL, "YES", 100.0, 0.95)
        self.b.set_exit(AL, 99, self.now)
        b2 = Bonds(self.r.fam, self.r.exchange, lambda s: self.odds.get(s),
                   clock=lambda: self.r.now, sleep=lambda s: None)
        b2.restore(self.b.to_dict())
        self.assertEqual(b2.exit_px[AL]["px"], 0.99)
        self.b.lots.clear()
        self.exch(AL, 0.0, 0.0)
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertNotIn(AL, self.b.exit_px)


class TestHisOwnBuy(Base):
    """Owner, 2026-09-04: "let me buy a portion of the shares at a given
    price point"."""

    def setUp(self):
        super().setUp()
        self.b.approve(AL, self.now)
        self.b.approve(ALD, self.now)
        self.b.set_budget(1000.0)

    def buys(self, slug=AL, side="BUY"):
        return [o for o in self.orders(slug, side) if str(o.why).startswith("bond buy")]

    def test_it_rests_at_his_price_for_his_size_and_the_record_books_the_fill(self):
        self.bond(AL, "YES", 100.0, 0.95)
        r = self.b.place_buy(AL, 96, 25, self.now)
        self.assertTrue(r["ok"], r["note"])
        o = self.buys()[0]
        self.assertEqual((o.price, o.qty), (0.96, 25.0))
        row = self.b.view(self.now, self.positions())["rows"][0]
        self.assertEqual((row["buys"][0]["qty"], row["buys"][0]["price"]), (25.0, 0.96))
        self.b.cycle(self.now + 60, self.positions(), on=True)
        self.assertIn(o.id, self.r.fam.orders)                      # the buy-more logic leaves it alone
        # it fills: the record books it, and the ledger pays for it
        del self.r.exchange.live[o.id]
        self.r.exchange.trades.append(self.trade_row(o.id, BUY_LONG, 0.96, 25.0, self.now + 90))
        self.exch(AL, 125.0, 0.952)
        self.b.cycle(self.now + 120, self.positions(), on=True)
        self.assertEqual(self.b.held(AL, "YES"), 125.0)
        self.assertAlmostEqual(self.b.budget, 1000.0 - 24.0, places=2)

    def test_a_price_that_reaches_the_offers_is_refused(self):
        self.bond(AL, "YES", 100.0, 0.95)
        r = self.b.place_buy(AL, 99, 10, self.now)
        self.assertFalse(r["ok"])
        self.assertIn("Enter", r["note"])
        self.assertEqual(self.buys(), [])

    def test_sized_to_the_buying_power_and_pulled_on_a_tap(self):
        self.bond(AL, "YES", 100.0, 0.95)
        self.r.exchange.buying_power = lambda: 10.0
        r = self.b.place_buy(AL, 96, 50, self.now)
        self.assertTrue(r["ok"], r["note"])
        self.assertEqual(self.buys()[0].qty, 10.0)                  # floor(10 / 0.96)
        self.assertIn("sized to the buying power", r["note"])
        self.assertTrue(self.b.pull_buy(AL)["ok"])
        self.assertEqual(self.buys(), [])
        self.assertFalse(self.b.pull_buy(AL)["ok"])

    def test_a_no_bond_buy_is_an_ask_in_yes_terms(self):
        self.bond(ALD, "NO", 50.0, 0.02)
        r = self.b.place_buy(ALD, 97, 20, self.now)                  # buy NO at 97c = sell YES at 3c
        self.assertTrue(r["ok"], r["note"])
        o = self.buys(ALD, "SELL")[0]
        self.assertAlmostEqual(o.price, 0.03, places=4)
        self.assertEqual(o.qty, 20.0)
        row = [x for x in self.b.view(self.now, self.positions())["rows"] if x["market"] == ALD][0]
        self.assertAlmostEqual(row["buys"][0]["price"], 0.97, places=4)

    def test_no_new_buying_where_the_odds_left_the_band(self):
        self.bond(AL, "YES", 100.0, 0.95)
        self.odds[AL] = 0.984
        self.b.scan(self.now + 1, force=True)
        r = self.b.place_buy(AL, 96, 10, self.now + 2)
        self.assertFalse(r["ok"])
        self.assertIn("left the band", r["note"])
