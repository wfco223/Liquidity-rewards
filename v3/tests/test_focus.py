"""The focus tender (owner, 2026-09-10): the boosted politics markets
leave the engine and the bond list for a loop of their own; the list is
sorted by the expected value of a 10%-of-buying-power entry at the best
price; every order here is his to place, cancel, move and resize; the
tender rests one order a side only where he has set a fair, never past
it, under a $1,000 expected-loss cap. Alaska governor is on the ground;
the balance-of-power books stay his hand's.
"""
import copy
import json
import unittest

from v3 import focus as focus_mod
from v3 import politics
from v3.bonds import Bonds
from v3.family import FamilyOrder
from v3.focus import FOCUS_POOL_MIN_USD, PURPOSE, Focus
from v3.intents import BUY_LONG, SELL_LONG
from v3.scoring import Book
from v3.tests.test_family import LIVE_PROG, Rig, politics_book

NC = "ewc-usse-nc-2026-11-03-rep"          # T1: $1,500/day
OH = "ewc-usse-oh-2026-11-03-dem"          # T1 too
AK = "usgubewc-usgub-ak-2026-11-03-berwil"  # T2 $600, avoided by the engine
BP = "paccc-balpow-2026-11-03-dsweep"      # T1, his own hand's book
AG = "usag-2026-11-03-az-rep"              # held ground, boosted
PLAIN = "ewc-usgub-ks-2026-11-03-dem"      # a $100 tier market: not boosted

T1 = {"timePeriods": [{"programId": "midterms_t1_control_bop_tossup_senate_20260909",
                       "rewardPool": 1500.0, "targetSize": 25000, "discountFactor": 0.3,
                       "status": "LIVE", "start": "2026-09-10T00:00:00Z",
                       "end": "2026-11-04T00:00:00Z"}]}
T2 = {"timePeriods": [{"programId": "midterms_t2_competitive_senate_gov_seatcounts_20260909",
                       "rewardPool": 600.0, "targetSize": 15000, "discountFactor": 0.3,
                       "status": "LIVE", "start": "2026-09-10T00:00:00Z",
                       "end": "2026-11-04T00:00:00Z"}]}


def wide_book(now, bid=0.44, ask=0.47, bid_q=300.0, ask_q=300.0):
    """A boosted market's shape: a real touch, the qualifying walls
    far behind, the target reached on both sides."""
    return Book(bids=((bid, bid_q), (round(bid - 0.01, 2), 500.0), (0.02, 60000.0)),
                asks=((ask, ask_q), (round(ask + 0.01, 2), 500.0), (0.98, 60000.0)),
                tick=0.01, fetched_at=now)


class Base(unittest.TestCase):
    def setUp(self):
        self.r = Rig(cfg=politics.config())
        self.silver = {NC: 0.45, OH: 0.52, AK: 0.30, BP: 0.20, AG: 0.60, PLAIN: 0.40}
        self.pings = []
        self.b = Bonds(self.r.fam, self.r.exchange, lambda s: None,
                       clock=lambda: self.r.now, sleep=lambda s: None)
        self.b.budget, self.b.budget_mode = 1000.0, "fixed"
        self.f = Focus(self.r.fam, self.r.exchange, self.b,
                       fair=lambda s: self.silver.get(s),
                       alert=lambda t, m: self.pings.append((t, m)),
                       clock=lambda: self.r.now, switch_on=lambda: True,
                       buying_power=lambda: 2000.0)
        for slug, prog in ((NC, T1), (OH, T1), (AK, T2), (BP, T1), (AG, T2), (PLAIN, LIVE_PROG)):
            self.r.add_market(slug, wide_book(self.r.now), event=f"event {slug}", prog=prog)
        # as at boot: the ground is claimed before the family may act
        self.r.switch = False
        self.r.cycle()                       # discovery, the family's terms
        self.f.seed(self.r.now)
        self.r.switch = True

    def tick(self, on=True, advance=15.0):
        self.r.now += advance
        for s in list(self.r.exchange.books):
            b = self.r.exchange.books[s]
            self.r.cache.put(s, Book(bids=b.bids, asks=b.asks, tick=b.tick, fetched_at=self.r.now))
        return self.f.cycle(self.r.now, dict(self.r.positions), on)

    def mine(self, slug=None, side=None):
        return [o for o in self.r.fam.orders.values()
                if o.purpose == PURPOSE and (slug is None or o.market == slug)
                and (side is None or o.side == side)]


class TestTheGround(Base):
    def test_boosted_programs_make_the_list_and_the_rest_do_not(self):
        self.tick()
        self.assertEqual(set(self.f.markets), {NC, OH, AK, BP, AG})
        self.assertNotIn(PLAIN, self.f.markets)
        self.assertTrue(self.f.is_boosted(NC))
        self.assertGreaterEqual(self.f.rows[NC]["prog"]["pool_day"], FOCUS_POOL_MIN_USD)

    def test_a_bounded_pool_reads_per_day_not_per_window(self):
        self.tick()
        self.assertEqual(self.f.rows[NC]["prog"]["pool_day"], 1500.0)

    def test_the_engine_and_the_bonds_stay_off_the_focus_ground(self):
        self.tick()
        self.assertEqual(self.r.fam.freeze_dyn, set(self.f.markets))
        self.assertTrue(self.r.fam._frozen(NC))
        self.assertFalse(self.r.fam._frozen(PLAIN))
        self.assertEqual(self.b.focus_out, set(self.f.markets))
        self.assertEqual(self.f.rows[NC]["not_tended"], "no fair set — shown only")

    def test_alaska_governor_is_tended_but_balance_of_power_and_held_ground_are_not(self):
        self.f.set_fair(AK, 30.0)
        self.f.set_fair(BP, 20.0)
        self.f.set_fair(AG, 60.0)
        self.tick()
        self.assertIsNone(self.f.rows[AK]["not_tended"])
        self.assertTrue(self.r.fam._avoided(AK))          # the engine still avoids it
        self.assertIn("own hand", self.f.rows[BP]["not_tended"])
        self.assertIn("held ground", self.f.rows[AG]["not_tended"])
        self.assertTrue(self.mine(AK))
        self.assertFalse(self.mine(BP))
        self.assertFalse(self.mine(AG))

    def test_a_market_taken_off_his_hands_list_is_tended_and_put_back_is_not(self):
        # owner, 2026-09-10: "Give me a button to take a market off of
        # the hand tended list"
        self.f.set_fair(BP, 20.0)
        self.tick()
        self.assertTrue(self.f.rows[BP]["by_hand"])
        self.assertFalse(self.mine(BP))
        r = self.f.release(BP, True)
        self.assertTrue(r["ok"], r)
        self.tick()
        self.assertIsNone(self.f.rows[BP]["not_tended"])
        self.assertTrue(self.f.rows[BP]["released"])
        self.assertTrue(self.mine(BP))
        self.assertTrue(self.r.fam._avoided(BP))       # the engine still avoids it
        # a market the engine never avoided cannot be "released"
        self.assertFalse(self.f.release(NC, True)["ok"])
        # back to his hand: the tender's orders come off, his own stay
        self.f.place(BP, "BUY", 15.0, 10.0)
        r = self.f.release(BP, False)
        self.assertTrue(r["ok"], r)
        self.tick()
        self.assertFalse(self.mine(BP))
        self.assertTrue([o for o in self.r.fam.orders.values()
                         if o.market == BP and o.purpose == "manual"])
        self.assertIn("own hand", self.f.rows[BP]["not_tended"])
        # and the release survives a restart
        self.f.release(BP, True)
        g = Focus(self.r.fam, self.r.exchange, self.b, clock=lambda: self.r.now)
        g.restore(json.loads(json.dumps(self.f.to_dict())))
        self.assertIn(BP, g.released)

    def test_the_focus_ground_does_not_charge_the_family_ceiling(self):
        self.tick()
        self.r.fam.orders["x1"] = FamilyOrder(id="x1", market=NC, side="BUY", price=0.40,
                                              qty=100.0, intent=BUY_LONG, placed_ts=self.r.now,
                                              purpose=PURPOSE)
        self.r.fam.orders["x2"] = FamilyOrder(id="x2", market=PLAIN, side="BUY", price=0.40,
                                              qty=10.0, intent=BUY_LONG, placed_ts=self.r.now,
                                              purpose="earn")
        self.assertAlmostEqual(self.r.fam.family_spent(), 4.0, places=6)


class TestTheStartUp(Base):
    """Owner, 2026-09-10: "The start up time for focus has to be very
    short." The seed lists the markets on the page at once; the first
    pass reads every focus book itself, not a dozen a pass."""

    def test_the_seed_lists_the_markets_before_any_pass(self):
        v = json.loads(self.f.payload_json)
        self.assertTrue(v["ok"])
        self.assertEqual({r["market"] for r in v["rows"]}, {NC, OH, AK, BP, AG})
        self.assertIn("starting", v["note"])

    def test_the_first_pass_reads_every_unread_book(self):
        self.r.cache._books.clear()
        self.f.cycle(self.r.now + 1, {}, False)
        self.assertEqual(self.f.books_read, 5)
        for s in (NC, OH, AK, BP, AG):
            self.assertIsNotNone(self.r.cache.any_age(s))
            self.assertIsNotNone(self.f.rows[s]["book"])
        # the universe walk waited for the second pass
        self.assertEqual(self.f._rotor, 0)
        self.f.cycle(self.r.now + 16, {}, False)
        self.assertGreater(self.f._rotor, 0)


class TestTheProgramWatch(Base):
    def test_a_new_boosted_program_is_an_event_and_a_ping(self):
        self.tick()
        self.pings.clear()
        new = "ewc-usse-ga-2026-11-03-dem"
        self.r.add_market(new, wide_book(self.r.now), event="GA", prog=T1)
        self.r.fam.universe[new] = {"event_n": 2, "name": "GA dem"}
        self.f.last_terms_own = 0.0
        self.f._rotor = 0
        for _ in range(3):
            self.tick()
        self.assertIn(new, self.f.markets)
        self.assertTrue(any(e["market"] == new for e in self.f.events))
        self.assertTrue(any("new boosted" in t for t, _m in self.pings))

    def test_the_seed_is_quiet(self):
        self.assertEqual(self.pings, [])
        self.assertEqual(self.f.events, [])


class TestThePlan(Base):
    def test_an_entry_is_up_to_ten_percent_of_buying_power_and_may_sit_past_fair(self):
        # owner, 2026-09-10: "A bid over fair value is fine as long as
        # it is appropriately sized for the risk and rewards it can
        # earn" — the concession past fair is a fill cost, charged in
        # full, and the size is chosen with the price
        self.f.set_fair(NC, 40.0)                # the touch (44/47) sits past it
        self.tick()
        row = self.f.rows[NC]
        self.assertEqual(row["stake"], 200.0)
        buy = row["sides"]["BUY"]
        self.assertLessEqual(buy["qty"] * buy["px"], 200.0 + 1e-6)
        if buy["px"] > 0.40:
            self.assertAlmostEqual(buy["conc"], buy["px"] - 0.40, places=4)
            self.assertGreaterEqual(buy["fc"], buy["conc"] + self.f.fill_floor - 1e-9)
        sell = row["sides"]["SELL"]
        for p in (buy, sell):
            self.assertAlmostEqual(p["ev"], p["est"] - p["loss"] - p["coc"], places=3)
            self.assertGreater(p["pf"], 0.0)
            self.assertGreaterEqual(p["fc"], self.f.fill_floor)
        # the slot a tick inside the fair is always among the candidates
        book = self.r.cache.any_age(NC)
        self.assertIn(0.39, self.f._cands("BUY", book, 0.40))
        self.assertIn(0.44, self.f._cands("BUY", book, 0.40))

    def test_a_small_pool_far_past_fair_sizes_the_entry_down_or_back(self):
        # a $100 tier: little to earn against a 24c concession at the
        # touch — the best order is smaller than the full stake, or
        # sits back at the fair
        self.f.set_fair(PLAIN, 20.0)
        self.r.fam.universe[PLAIN] = {"event_n": 1, "name": PLAIN}
        self.f.terms.current[PLAIN] = self.r.fam.terms.get(PLAIN)
        self.f.markets = sorted(set(self.f.markets) | {PLAIN})
        row = self.f._row(PLAIN, self.r.now, {}, 2000.0)
        buy = row["sides"]["BUY"]
        full = float(int(200.0 / buy["px"]))
        self.assertTrue(buy["qty"] < full or buy["px"] <= 0.19 + 1e-9,
                        (buy["qty"], full, buy["px"]))
        self.assertGreater(buy["ev"], 0.0)

    def test_an_exit_never_sits_under_fair(self):
        book = self.r.cache.any_age(NC)
        for px in self.f._cands("SELL", book, 0.50, bound=True):
            self.assertGreaterEqual(px, 0.51 - 1e-9)

    def test_without_a_fair_silver_stands_in_for_the_plan_only(self):
        self.tick()
        row = self.f.rows[OH]
        self.assertEqual(row["sides"]["BUY"]["fair_used"], "silver")
        self.assertLessEqual(row["sides"]["BUY"]["px"], 0.51 + 1e-9)
        self.assertEqual(row["not_tended"], "no fair set — shown only")

    def test_the_list_is_sorted_by_the_entry_ev(self):
        self.tick()
        v = self.f.view(self.r.now, 2000.0, True)
        evs = [r["ev"] for r in v["rows"] if r["ev"] is not None]
        self.assertEqual(evs, sorted(evs, reverse=True))
        json.dumps(v)                                          # the page's bytes

    def test_his_stake_beats_the_ten_percent(self):
        self.f.set_stake(NC, 50.0)
        self.tick()
        self.assertEqual(self.f.rows[NC]["stake"], 50.0)
        self.assertEqual(self.f.rows[NC]["stake_src"], "set by you")
        self.f.set_stake(NC, "")
        self.tick()
        self.assertEqual(self.f.rows[NC]["stake"], 200.0)

    def test_the_fill_cost_floor_binds(self):
        self.f.set_fair(NC, 45.0)
        self.f.set_number("floor", 10.0)
        self.tick()
        self.assertGreaterEqual(self.f.rows[NC]["sides"]["BUY"]["fc"], 0.10 - 1e-9)


class TestTending(Base):
    def test_a_fair_and_the_switch_rest_one_order_a_side(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        buys, sells = self.mine(NC, "BUY"), self.mine(NC, "SELL")
        self.assertEqual(len(buys), 1)
        self.assertEqual(len(sells), 1)
        plan = self.f.rows[NC]["sides"]["BUY"]
        self.assertAlmostEqual(buys[0].price, plan["px"])
        self.assertAlmostEqual(buys[0].qty, plan["qty"])
        self.assertIn("focus entry", buys[0].why)
        self.assertIn(buys[0].id, self.r.exchange.live)
        # the next pass keeps them where they are
        n = len(self.r.exchange.live)
        self.tick()
        self.assertEqual(len(self.r.exchange.live), n)

    def test_the_switch_off_shows_and_rests_nothing(self):
        self.f.set_fair(NC, 45.0)
        self.tick(on=False)
        self.assertFalse(self.mine())
        self.assertIn("switch is off", self.f.note)

    def test_clearing_the_fair_pulls_the_tenders_orders_not_his(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        self.assertTrue(self.mine(NC))
        r = self.f.place(NC, "BUY", 40.0, 10.0)
        self.assertTrue(r["ok"], r)
        self.f.set_fair(NC, "")
        self.tick()
        self.assertFalse(self.mine(NC))
        his = [o for o in self.r.fam.orders.values() if o.market == NC and o.purpose == "manual"]
        self.assertEqual(len(his), 1)
        self.assertIn(his[0].id, self.r.exchange.live)

    def test_pause_pulls_and_resume_rests_again(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        self.f.pause(NC, True)
        self.assertFalse(self.mine(NC))
        self.tick()
        self.assertFalse(self.mine(NC))
        self.f.pause(NC, False)
        self.tick()
        self.assertTrue(self.mine(NC))

    def test_the_loss_cap_admits_the_best_ev_first(self):
        self.f.set_fair(NC, 45.0)
        self.f.set_fair(OH, 52.0)
        self.f.set_number("cap", 60.0)
        self.tick()
        used = self.f.risk_used()
        self.assertLessEqual(used, 60.0 + 1e-6)
        self.assertTrue(self.mine())
        plans = []
        for s in (NC, OH):
            for sd in ("BUY", "SELL"):
                p = self.f.rows[s]["sides"].get(sd) or {}
                if p.get("px"):
                    plans.append((p["ev"], s, sd))
        plans.sort(reverse=True)
        # the best EV that fits under the cap on its own is rested (two
        # identical plans tie: either one)
        best_ev = next(t[0] for t in plans
                       if self.f.rows[t[1]]["sides"][t[2]]["risk"] <= 60.0)
        rested = [(t[0], t[1], t[2]) for t in plans if self.mine(t[1], t[2])]
        self.assertTrue(rested)
        self.assertAlmostEqual(rested[0][0], best_ev, places=3)

    def test_the_engine_orders_here_become_the_tenders(self):
        self.r.fam.orders["e1"] = FamilyOrder(id="e1", market=NC, side="BUY", price=0.30,
                                              qty=5.0, intent=BUY_LONG, placed_ts=self.r.now,
                                              purpose="earn", why="engine")
        self.r.exchange.live["e1"] = {"id": "e1", "market": NC, "side": "BUY",
                                      "price": 0.30, "size": 5.0, "intent": BUY_LONG}
        self.r.fam.orders["b1"] = FamilyOrder(id="b1", market=OH, side="SELL", price=0.90,
                                              qty=5.0, intent=SELL_LONG, placed_ts=self.r.now,
                                              purpose="bond", why="bond exit")
        self.tick(on=False)
        self.assertEqual(self.r.fam.orders["e1"].purpose, PURPOSE)
        self.assertIn("inherited from the engine", self.r.fam.orders["e1"].why)
        self.assertEqual(self.r.fam.orders["b1"].purpose, "sell")
        # with a fair set, the inherited order is re-laid at the plan
        self.f.set_fair(NC, 45.0)
        self.f.moved_at.clear()
        self.tick()
        buys = self.mine(NC, "BUY")
        self.assertEqual(len(buys), 1)
        self.assertNotEqual(buys[0].id, "e1")
        self.assertNotIn("e1", self.r.exchange.live)

    def test_held_stock_gets_an_exit_at_or_past_his_fair(self):
        self.r.positions[NC] = (200.0, 88.0)
        self.f.set_fair(NC, 45.0)
        self.tick()
        row = self.f.rows[NC]
        self.assertEqual(row["position"]["qty"], 200.0)
        self.assertEqual(row["exit"]["side"], "SELL")
        self.assertGreaterEqual(row["exit"]["px"], 0.46 - 1e-9)
        sells = self.mine(NC, "SELL")
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0].qty, 200.0)
        self.assertIn("focus exit", sells[0].why)
        # no second ask opens a short beside the exit
        self.assertEqual(len([o for o in self.r.fam.orders.values()
                              if o.market == NC and o.side == "SELL"]), 1)


class TestHisTaps(Base):
    def test_place_cancel_and_move_any_order_here(self):
        self.tick(on=False)
        r = self.f.place(NC, "SELL", 60.0, 25.0)
        self.assertTrue(r["ok"], r)
        oid = r["order_id"]
        rec = self.r.fam.orders[oid]
        self.assertEqual((rec.purpose, rec.side, rec.price, rec.qty), ("manual", "SELL", 0.60, 25.0))
        r = self.f.move(NC, oid, cents=62.0, qty=30.0)
        self.assertTrue(r["ok"], r)
        self.assertNotIn(oid, self.r.fam.orders)
        rec2 = self.r.fam.orders[r["order_id"]]
        self.assertEqual((rec2.price, rec2.qty, rec2.purpose), (0.62, 30.0, "manual"))
        r = self.f.cancel(NC, rec2.id)
        self.assertTrue(r["ok"], r)
        self.assertNotIn(rec2.id, self.r.exchange.live)
        self.assertFalse(self.r.fam.orders)

    def test_a_tender_order_he_moves_becomes_his(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        o = self.mine(NC, "BUY")[0]
        r = self.f.move(NC, o.id, cents=40.0)
        self.assertTrue(r["ok"], r)
        rec = self.r.fam.orders[r["order_id"]]
        self.assertEqual(rec.purpose, "manual")
        self.f.moved_at.clear()
        self.tick()
        # the tender rests its own bid beside his, never touches his
        self.assertIn(rec.id, self.r.exchange.live)
        self.assertEqual(rec.price, 0.40)

    def test_the_numbers_and_the_fair_go_in_plain_units(self):
        self.assertTrue(self.f.set_number("coc", 1.0)["ok"])
        self.assertEqual(self.f.coc_day, 0.01)
        self.assertFalse(self.f.set_fair(NC, "abc")["ok"])
        self.assertFalse(self.f.set_fair(NC, 150)["ok"])
        self.assertTrue(self.f.set_fair(NC, 46.5)["ok"])
        self.assertEqual(self.f.fairs[NC], 0.465)
        self.assertFalse(self.f.set_stake(NC, -1)["ok"])

    def test_settings_survive_a_restart(self):
        self.f.set_fair(NC, 45.0)
        self.f.set_stake(OH, 75.0)
        self.f.pause(BP, True)
        self.f.set_number("cap", 500.0)
        d = json.loads(json.dumps(self.f.to_dict()))
        g = Focus(self.r.fam, self.r.exchange, self.b, clock=lambda: self.r.now)
        g.restore(d)
        self.assertEqual(g.fairs[NC], 0.45)
        self.assertEqual(g.stakes[OH], 75.0)
        self.assertIn(BP, g.paused)
        self.assertEqual(g.loss_cap, 500.0)


class TestTheBondsHandOver(Base):
    def test_a_listed_boosted_market_leaves_the_bond_list_and_a_lot_stays_in_the_ledger(self):
        self.b.approved[NC] = {"added": self.r.now, "odds": 0.99, "side": "YES"}
        self.b.approved[OH] = {"added": self.r.now, "odds": 0.99, "side": "YES"}
        self.b._book_lot(OH, "YES", 50.0, 45.0, ref="test")
        self.tick()
        self.b.cycle(self.r.now, dict(self.r.positions), False)
        self.assertNotIn(NC, self.b.approved)
        self.assertIn(OH, self.b.approved)            # the lot keeps its place
        self.assertIn(OH, self.b._working())          # the ledger follows the record
        self.assertNotIn(OH, self.b._tending())       # but nothing is tended here
        self.assertTrue(any(r.get("event") == "focus_out" for r in self.b.log))
        rows = {r["market"]: r for r in self.b.view(self.r.now)["rows"]}
        self.assertTrue(rows[OH]["focus"])

    def test_the_scan_never_proposes_a_focus_market(self):
        self.tick()
        self.b.fair = lambda s: 0.995 if s in (NC, PLAIN) else None
        self.b.scan(self.r.now, force=True)
        self.assertNotIn(NC, self.b.proposed)
        self.assertIn(PLAIN, self.b.proposed)


class TestTheWebPage(unittest.TestCase):
    def test_the_page_and_its_ops_are_wired(self):
        from v3 import web
        self.assertIn("/focus", web.PAGES)
        self.assertIn(("focus", "focus"), web.NAV)
        calls = []

        class M:
            def focus_op(self, op, market, value=None):
                calls.append((op, market, value))
                return {"ok": True}
        srv = web.WebServer(M(), port=0)
        srv.handle_op({"op": "focus_fair", "market": NC, "value": 45})
        srv.handle_op({"op": "focus_move", "market": NC, "value": {"order_id": "o1", "px": 40}})
        self.assertEqual(calls[0], ("focus_fair", NC, 45))
        self.assertEqual(calls[1][2]["order_id"], "o1")

    def test_the_websocket_seats_the_focus_first_and_caps_the_engine(self):
        self.assertEqual(focus_mod.FOCUS_WS_CAP + focus_mod.ENGINE_WS_CAP, 200)


if __name__ == "__main__":
    unittest.main()
