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
import time
import unittest
from unittest import mock

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

    def test_close_out_ground_leaves_the_tender_and_is_not_frozen(self):
        # owner, 2026-09-12 "get out of 2028 markets": a market on the
        # family's liquidate list is the engine's to sell down — off
        # the tender's ground whatever its pool, so the engine is not
        # frozen there and its close-out can run; the tender's own
        # order there is the engine's to pull
        self.f.set_fair(NC, 45.0)
        self.tick()
        self.assertIn(NC, self.f.markets)
        self.assertTrue(self.mine(NC))
        self.r.fam.cfg.liquidate_tokens = ("usse-nc-",)
        self.tick()
        self.assertNotIn(NC, self.f.markets)
        self.assertFalse(self.f.is_boosted(NC))
        self.assertFalse(self.r.fam._frozen(NC))
        self.assertTrue(self.r.fam._liquidating(NC))
        self.assertTrue(any(e.get("event") == "left_focus" and e.get("market") == NC
                            for e in self.f.log))

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

    def test_a_two_sided_2028_book_gets_the_midpoint_as_its_fair_once(self):
        # owner, 2026-09-11: "2028 markets got boosted" ... "That sounds
        # good" — the midpoint for the party pair and the candidates at
        # 5c or more, nothing on the penny candidates
        party = "ewc-usp-party-2028-11-07-dem"
        penny = "enwc-uspres-nom-dem-2028-nobody"
        wide = "ewc-usp-2028-11-07-someone"
        self.r.add_market(party, wide_book(self.r.now, bid=0.51, ask=0.53), event="party", prog=T1)
        self.r.add_market(penny, Book(bids=((0.01, 5000.0), (0.02, 60000.0)),
                                      asks=((0.02, 300.0), (0.98, 60000.0)),
                                      tick=0.01, fetched_at=self.r.now), event="nom", prog=T1)
        self.r.add_market(wide, Book(bids=((0.10, 300.0), (0.02, 60000.0)),
                                     asks=((0.30, 300.0), (0.98, 60000.0)),
                                     tick=0.01, fetched_at=self.r.now), event="usp", prog=T1)
        for m in (party, penny, wide):
            self.r.fam.universe[m] = {"event_n": 2, "name": m}
        # the 2028 books are close-out ground since 2026-09-12; the
        # rules under test stand for the day he reopens them
        self.r.fam.cfg.liquidate_tokens = ("rondes",)
        self.f.last_terms_own = 0.0
        self.f._rotor = 0
        for _ in range(4):
            self.tick()
        self.assertIn(party, self.f.markets)
        self.assertEqual(self.f.fairs.get(party), 0.52)
        self.assertNotIn(penny, self.f.fairs)             # a penny book: his walls' ground
        self.assertNotIn(wide, self.f.fairs)              # no mid worth the name
        seeded = [e for e in self.f.log if e.get("event") == "fair_set" and e.get("market") == party]
        self.assertEqual(len(seeded), 1)
        self.assertIn("midpoint", seeded[0].get("note") or "")
        # his clear stands: no re-seed, and the record survives a restart
        self.f.set_fair(party, None)
        self.tick()
        self.tick()
        self.assertNotIn(party, self.f.fairs)
        d = json.loads(json.dumps(self.f.to_dict()))
        self.assertIn(party, d["mid_seeded"])

    def test_a_2028_book_stakes_twenty_dollars_at_most(self):
        # owner, 2026-09-11: "Because there is no model, keep maximum loss
        # per market on 2028 markets to $20"
        party = "ewc-usp-party-2028-11-07-dem"
        self.r.add_market(party, wide_book(self.r.now, bid=0.51, ask=0.53), event="party", prog=T1)
        self.r.fam.universe[party] = {"event_n": 2, "name": party}
        self.r.fam.cfg.liquidate_tokens = ("rondes",)     # close-out ground since 2026-09-12
        self.f.last_terms_own = 0.0
        self.f._rotor = 0
        for _ in range(4):
            self.tick()
        stake, src = self.f.stake(party, 2000.0)
        self.assertEqual(stake, 20.0)
        self.assertIn("$20", src)
        self.assertEqual(self.f.stake(NC, 2000.0)[0], 200.0)      # the midterms books as before
        row = self.f.rows[party]
        for side in ("BUY", "SELL"):
            plan = (row.get("tend") or {}).get(side) or {}
            if plan.get("px"):
                cost_ps = plan["px"] if side == "BUY" else 1.0 - plan["px"]
                self.assertLessEqual(plan["qty"] * cost_ps, 20.0 + 0.6)
        for o in self.mine(party):
            cost_ps = o.price if o.side == "BUY" else 1.0 - o.price
            self.assertLessEqual(o.qty * cost_ps, 20.0 + 0.6)
        # a stake he sets by hand stands as he set it
        self.f.set_stake(party, 50.0)
        self.assertEqual(self.f.stake(party, 2000.0), (50.0, "set by you"))

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
            # owner, 2026-09-12: the concession is what a fill loses when
            # the position is unwound midway between his fair (40c) and
            # the side's current price (the 44c bid): px - 42c
            self.assertAlmostEqual(buy["conc"], max(buy["px"] - 0.42, 0.0), places=4)
            self.assertAlmostEqual(buy["past"], buy["px"] - 0.40, places=4)
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

    def test_an_exit_never_sits_under_its_bound(self):
        # the bound is inclusive: at the price itself is fine
        book = self.r.cache.any_age(NC)
        for px in self.f._cands("SELL", book, 0.50, bound=True, improve=False):
            self.assertGreaterEqual(px, 0.50 - 1e-9)
        self.assertNotIn(0.46, self.f._cands("SELL", book, 0.50, bound=True, improve=False))

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


class TestAfterAFill(Base):
    """2026-09-10, the House dem control market: an ask at the touch
    filled eleven times in fifteen minutes while the tender re-rested
    it after every fill, and the exits sat eight ticks behind the touch
    bound by his fair. Now: a fill of an entry pulls the rest and holds
    that side for two hours; an entry never adds past the stake; the
    exit joins the touch, never under the position's cost."""

    def fill(self, order, qty):
        """The exchange fills part of a tender order: the family's
        reconcile shrinks the record and books the position."""
        live = self.r.exchange.live[order.id]
        live["size"] -= qty
        if live["size"] < 0.5:
            self.r.exchange.live.pop(order.id, None)
        net, cost = self.r.positions.get(order.market, (0.0, 0.0))
        if order.side == "SELL":
            self.r.positions[order.market] = (net - qty, cost + qty * (1.0 - order.price))
        else:
            self.r.positions[order.market] = (net + qty, cost + qty * order.price)
        self.r.switch = False
        self.r.cycle(advance=1.0)            # the reconcile, nothing placed
        self.r.switch = True

    def test_a_fill_pulls_the_rest_of_the_entry_and_holds_the_side(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        ask = self.mine(NC, "SELL")[0]
        self.fill(ask, 20.0)
        self.tick()
        self.assertFalse(self.mine(NC, "SELL"))      # the rest came off
        self.assertTrue(any(e.get("event") == "filled" for e in self.f.log))
        self.assertIn("filled", self.f.rows[NC]["tend"]["SELL"].get("note", "")
                      if "note" in self.f.rows[NC]["tend"]["SELL"] else "filled")
        for _ in range(3):
            self.tick()
        self.assertFalse(self.mine(NC, "SELL"))      # and stays off
        # fifteen minutes on, the side re-enters at a quarter of the
        # stake (owner, 2026-09-10: "The stand off after a fill for a
        # side should be 15 minutes and when entering size should be
        # scaled down until the two hour window has passed")
        self.r.now += focus_mod.FOCUS_REFILL_WAIT_S
        self.f.moved_at.clear()
        self.tick()
        plan = self.f.rows[NC]["tend"]["SELL"]
        self.assertLess(plan["scale"], focus_mod.FOCUS_REFILL_FLOOR + 0.05)
        self.assertIn("of the stake", plan["scale_note"])
        full_qty = self.f.rows[NC]["sides"]["SELL"]["qty"]
        self.assertLessEqual(plan["qty"], 0.3 * full_qty + 1)
        self.assertTrue(self.mine(NC, "SELL"))
        # an hour in, about half way up the ramp; two hours in, full size
        self.r.now += 45 * 60.0
        self.f.moved_at.clear()
        self.tick()
        mid = self.f.rows[NC]["tend"]["SELL"]["scale"]
        self.assertTrue(0.45 < mid < 0.8, mid)
        self.r.now += focus_mod.FOCUS_REFILL_SCALE_S
        self.f.moved_at.clear()
        self.tick()
        self.assertNotIn("scale", self.f.rows[NC]["tend"]["SELL"])

    def test_an_order_the_open_list_left_out_for_a_read_is_not_a_fill(self):
        # 13:35Z: the list left the TX governor bid out for one read; the
        # family restored its record a minute later; the tender had
        # taken it for a fill, held the side and pulled the restored order
        self.f.set_fair(NC, 45.0)
        self.tick()
        bid = self.mine(NC, "BUY")[0]
        live = self.r.exchange.live.pop(bid.id)
        self.r.switch = False
        self.r.cycle(advance=1.0)                    # the read that left it out
        self.r.exchange.live[bid.id] = live
        self.r.cycle(advance=1.0)                    # and the one that shows it again
        self.r.switch = True
        self.tick()
        self.assertFalse(self.f.filled_at)
        self.assertNotIn("filled", (self.f.rows[NC]["tend"]["BUY"].get("note") or ""))
        self.assertIn(bid.id, self.r.exchange.live)  # still resting, never pulled
        self.assertFalse(any(e.get("event") == "filled" for e in self.f.log))

    def test_the_family_relabelling_a_tender_exit_does_not_read_as_a_fill(self):
        self.r.positions[NC] = (200.0, 200.0 * 0.40)
        self.f.set_fair(NC, 45.0)
        self.tick()
        ask = self.mine(NC, "SELL")[0]
        ask.purpose = "sell"
        ask.why = "an exit — its fill reduces the position it sits on"
        self.tick()
        self.assertFalse(self.f.filled_at)
        self.assertTrue(self.f._is_mine(ask))
        self.assertEqual(len(self.f._mine(NC, "SELL")), 1)

    def test_a_buying_power_dip_does_not_pull_an_order_earning_on_its_own(self):
        # 13:43-13:46Z: the balance-of-power ask was pulled and re-rested
        # three times in three minutes as the stake followed the reads
        self.f.set_fair(NC, 45.0)
        self.tick()
        ask = self.mine(NC, "SELL")[0]
        self.f._bp_fn = lambda: 30.0                 # a dip: the stake would be $3
        self.f._bp = None
        self.tick()
        self.assertEqual(self.f.rows[NC]["stake"], 200.0)   # the half-hour high stands
        self.assertIn(ask.id, self.r.exchange.live)
        # and when the high has aged out, the plan shrinks but the
        # resting order stays while its own expected value is positive
        self.f._bp_reads = []
        self.f._bp = None
        self.tick()
        self.assertEqual(self.f.rows[NC]["stake"], 3.0)
        self.assertIn(ask.id, self.r.exchange.live)
        self.assertFalse(any(e.get("event") == "pull" and e.get("market") == NC
                             for e in self.f.log))

    def test_a_negative_reading_pulls_only_after_the_dwell(self):
        # the tenth-cent flicker: one pass under zero is not a pull
        self.f.set_fair(NC, 45.0)
        self.tick()
        ask = self.mine(NC, "SELL")[0]
        # a crossed frame from the stream (an ask under the bid, five
        # shares): every slot reads ticks behind it and the claim
        # collapses for the pass the frame stands
        dead = Book(bids=((0.44, 300.0), (0.02, 60000.0)),
                    asks=((0.40, 5000.0), (0.46, 300.0), (0.98, 60000.0)),
                    tick=0.01, fetched_at=self.r.now)
        self.r.exchange.books[NC] = dead
        self.tick()
        self.assertIn(ask.id, self.r.exchange.live)
        self.assertIn(f"{NC}|SELL", self.f.weak_since)
        self.tick()
        self.assertIn(ask.id, self.r.exchange.live)
        # the reading holds past the dwell: now it comes off
        self.r.now += focus_mod.FOCUS_WEAK_DWELL_S
        self.tick()
        self.assertNotIn(ask.id, self.r.exchange.live)
        self.assertTrue(any(e.get("event") == "pull" and "min" in e.get("why", "")
                            for e in self.f.log))
        # and a reading that recovers clears the clock
        self.r.exchange.books[NC] = wide_book(self.r.now)
        self.f.moved_at.clear()
        self.tick()
        self.assertNotIn(f"{NC}|SELL", self.f.weak_since)

    def test_an_exit_fill_holds_nothing_and_an_exit_follows_the_lot_within_a_minute(self):
        # owner, 2026-09-10: "Exit orders should never be held and don't
        # need to ramp up. They can always be placed"
        self.r.positions[NC] = (200.0, 200.0 * 0.40)
        self.r.fam.positions_seen[NC] = 200.0        # the family knows the lot
        self.r.fam.inventory[NC] = {"qty": 200.0, "cost": 80.0}
        self.f.set_fair(NC, 45.0)
        self.tick()
        ex = self.f._mine(NC, "SELL")[0]
        self.assertIn("focus exit", ex.why)
        # part of the exit sells: no hold on the side, logged as the position leaving
        self.fill(ex, 50.0)
        self.tick()
        self.assertNotIn(f"{NC}|SELL", self.f.filled_at)
        self.assertTrue(any(e.get("event") == "exit_filled" for e in self.f.log))
        self.assertFalse(any(e.get("event") == "filled" and e.get("market") == NC
                             for e in self.f.log))
        # the lot grows again: the exit is re-sized within a minute, not five
        self.r.positions[NC] = (400.0, 400.0 * 0.40)
        self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S
        self.tick()
        ex2 = self.f._mine(NC, "SELL")
        self.assertEqual(len(ex2), 1)
        self.assertEqual(ex2[0].qty, 400.0)

    def vanish(self, order, qty, journal=True):
        """The order leaves the book (a fill the feed has not shown yet):
        the family's record goes, the journal books it, the feed stands."""
        self.r.exchange.live.pop(order.id, None)
        self.r.fam.orders.pop(order.id, None)
        if journal:
            self.r.fam.fills.append({"ts": self.r.now, "market": order.market, "side": order.side,
                                     "qty": qty, "px": order.price, "oid": order.id,
                                     "purpose": order.purpose})

    def test_a_filled_exit_is_not_rested_again_on_a_stale_position_read(self):
        # 02:57-02:59Z, 2026-09-11: the exit of 68 filled, the feed still
        # showed the lot, a second exit of 68 rested within the minute
        # and filled — flat became short 68
        self.r.positions[NC] = (200.0, 200.0 * 0.40)
        self.r.fam.positions_seen[NC] = 200.0
        self.r.fam.inventory[NC] = {"qty": 200.0, "cost": 80.0}
        self.f.set_fair(NC, 45.0)
        self.tick()
        ex = self.mine(NC, "SELL")[0]
        self.vanish(ex, 200.0)                        # the whole lot sold; the feed lags
        exits = lambda: [o for o in self.mine(NC, "SELL") if self.f._exit_order(o)]
        for _ in range(4):
            self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S
            self.tick()
            self.assertFalse(exits(), "a second exit on a stale read")
            self.assertIsNone(self.f.rows[NC]["position"])
        self.assertTrue(any(e.get("event") == "exit_filled" for e in self.f.log))
        # the feed catches up to flat: still nothing to exit (a fresh
        # entry may rest on that side — the lot is gone, not the side)
        self.r.positions[NC] = (0.0, 0.0)
        self.tick()
        self.assertFalse(exits())
        self.assertIsNone(self.f.rows[NC]["position"])
        # a new lot the feed shows is exited as usual
        self.r.positions[NC] = (300.0, 120.0)
        self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S
        self.tick()
        self.assertEqual([o.qty for o in exits()], [300.0])

    def test_a_fresh_position_the_feed_already_shows_is_not_doubled(self):
        # 03:57Z, 2026-09-11: a market with no row in the feed (flat)
        # opened a short of 337 and the feed showed it at once; the
        # tender read 674 and sized the cover to it
        self.f.set_fair(NC, 45.0)
        self.tick()
        ask = self.mine(NC, "SELL")[0]                # an entry: it opens a short
        self.r.positions.pop(NC, None)                # no row while flat
        self.tick()
        self.r.exchange.live.pop(ask.id, None)        # the whole ask sells
        self.r.fam.orders.pop(ask.id, None)
        self.r.fam.fills.append({"ts": self.r.now, "market": NC, "side": "SELL", "qty": ask.qty,
                                 "px": ask.price, "oid": ask.id, "purpose": ask.purpose})
        self.r.positions[NC] = (-ask.qty, ask.qty * (1.0 - ask.price))   # the feed shows it
        self.r.fam.positions_seen[NC] = -ask.qty
        self.r.fam.inventory[NC] = {"qty": -ask.qty, "cost": ask.qty * (1.0 - ask.price)}
        self.tick()
        self.assertEqual(self.f.rows[NC]["position"]["qty"], -ask.qty)
        self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S
        self.tick()
        self.assertEqual(self.f.rows[NC]["position"]["qty"], -ask.qty)
        covers = [o for o in self.mine(NC, "BUY") if self.f._exit_order(o)]
        self.assertEqual([o.qty for o in covers], [ask.qty])

    def test_an_entry_whose_record_vanishes_unbooked_does_not_grow_the_exit(self):
        # 05:38Z, 2026-09-11, Florida governor rep: short 137 with the
        # cover resting; a 1,115-share ask's record went missing for a
        # read, was taken as filled, and the cover was sized to 1,252
        self.r.positions[NC] = (-137.0, 137.0 * 0.23)
        self.r.fam.positions_seen[NC] = -137.0
        self.r.fam.inventory[NC] = {"qty": -137.0, "cost": 137.0 * 0.23}
        self.f.set_fair(NC, 77.0)
        self.r.exchange.books[NC] = wide_book(self.r.now, bid=0.77, ask=0.84)
        self.tick()
        covers = lambda: [o for o in self.mine(NC, "BUY") if self.f._exit_order(o)]
        self.assertEqual([o.qty for o in covers()], [137.0])
        ask = self.mine(NC, "SELL")[0]                # the entry on the other side
        self.r.fam.orders.pop(ask.id)                 # the record goes; no fill anywhere
        for _ in range(3):
            self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S
            self.tick()
            self.assertEqual(self.f.rows[NC]["position"]["qty"], -137.0)
            self.assertEqual([o.qty for o in covers()], [137.0])
        # the journal books it after all: now it counts, until the feed moves
        self.r.fam.fills.append({"ts": self.r.now, "market": NC, "side": "SELL", "qty": ask.qty,
                                 "px": ask.price, "oid": ask.id, "purpose": ask.purpose})
        self.r.exchange.live.pop(ask.id, None)
        self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S
        self.tick()
        self.assertEqual(self.f.rows[NC]["position"]["qty"], -137.0 - ask.qty)

    def test_a_fill_that_lands_as_the_build_boots_is_not_counted_twice(self):
        # 21:33Z, 2026-09-11, House rep control: a 742-share bid filled
        # 192 as the build booted; the first pass had no feed "a pass
        # ago", took the post-fill feed as the before, and added the
        # journal's 192 again — 438 offered against 246 held
        self.r.positions[NC] = (54.0, 54.0 * 0.10)
        self.r.fam.positions_seen[NC] = 54.0
        self.r.fam.inventory[NC] = {"qty": 54.0, "cost": 5.4}
        self.f.set_fair(NC, 45.0)
        self.tick()
        bid = self.mine(NC, "BUY")[0]
        # the restart: no feed a pass ago; the bid's record is gone, the
        # journal books its fill and the feed already shows it
        self.f._feed_prev = {}
        self.f._feed_prev_at = 0.0
        self.r.exchange.live.pop(bid.id, None)
        self.r.fam.orders.pop(bid.id, None)
        self.r.fam.fills.append({"ts": self.r.now, "market": NC, "side": "BUY", "qty": bid.qty,
                                 "px": bid.price, "oid": bid.id, "purpose": bid.purpose,
                                 "pos_after": 54.0 + bid.qty})
        self.r.positions[NC] = (54.0 + bid.qty, 5.4 + bid.qty * bid.price)
        self.r.fam.positions_seen[NC] = 54.0 + bid.qty
        self.r.fam.inventory[NC] = {"qty": 54.0 + bid.qty, "cost": 5.4 + bid.qty * bid.price}
        self.tick()
        self.assertEqual(self.f.rows[NC]["position"]["qty"], 54.0 + bid.qty)
        self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S
        self.tick()
        exits = [o for o in self.mine(NC, "SELL") if self.f._exit_order(o)]
        self.assertEqual([o.qty for o in exits], [54.0 + bid.qty])   # not 54 + 2 x the fill

    def test_a_fill_the_feed_already_shows_is_not_counted_twice(self):
        self.r.positions[NC] = (200.0, 200.0 * 0.40)
        self.r.fam.positions_seen[NC] = 200.0
        self.r.fam.inventory[NC] = {"qty": 200.0, "cost": 80.0}
        self.f.set_fair(NC, 45.0)
        self.tick()
        ex = self.mine(NC, "SELL")[0]
        self.fill(ex, 200.0)                          # the rig's feed moves at once
        self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S
        self.tick()
        self.assertIsNone(self.f.rows[NC]["position"])
        self.assertFalse([o for o in self.mine(NC, "BUY") if self.f._exit_order(o)])

    def test_a_vanished_order_the_journal_never_books_stops_adjusting_the_position(self):
        # his hand cancel of an exit: the lot reads as gone for a short
        # while, then the feed is the truth again and the exit is re-laid
        self.r.positions[NC] = (200.0, 200.0 * 0.40)
        self.r.fam.positions_seen[NC] = 200.0
        self.r.fam.inventory[NC] = {"qty": 200.0, "cost": 80.0}
        self.f.set_fair(NC, 45.0)
        self.tick()
        ex = self.mine(NC, "SELL")[0]
        self.vanish(ex, 200.0, journal=False)         # cancelled by his hand, no fill
        exits = lambda: [o for o in self.mine(NC, "SELL") if self.f._exit_order(o)]
        self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S
        self.tick()
        self.assertFalse(exits())                      # taken as filled for now
        self.r.now += focus_mod.FOCUS_POS_PENDING_S
        self.tick()
        self.assertEqual([o.qty for o in exits()], [200.0])   # the feed stands: re-laid

    def test_the_exit_joins_the_touch_when_either_his_fair_or_the_cost_allows(self):
        # short 200 opened at 47c: cost a share 53c of collateral, the
        # break-even YES price 47c; the bid touch is 44c, under it —
        # a gain against the cost, so the touch, though past his fair
        self.r.positions[NC] = (-200.0, 200.0 * 0.53)
        self.f.set_fair(NC, 40.0)
        self.tick()
        ex = self.f.rows[NC]["exit"]
        self.assertEqual(ex["side"], "BUY")
        self.assertEqual(ex["px"], 0.44)              # the touch
        self.assertAlmostEqual(ex["basis"], 0.47, places=4)
        bids = self.mine(NC, "BUY")
        self.assertEqual(len(bids), 1)
        self.assertEqual((bids[0].price, bids[0].qty), (0.44, 200.0))
        # the touch past both his fair and the cost: the earnings beat
        # the concession charged in full, with company on the side, so
        # the exit joins the touch (owner, 2026-09-11 "Yes" to exits
        # treated as entries are)
        self.r.exchange.books[NC] = wide_book(self.r.now, bid=0.48, ask=0.51)
        self.f.moved_at.clear()
        self.tick()
        ex = self.f.rows[NC]["exit"]
        self.assertEqual(ex["px"], 0.48)
        self.assertGreater(ex["conc"], 0.0)                     # 8c over his fair, charged
        self.assertGreater(ex["est"], ex["loss"])
        # his fair raised past the cost: the fair alone brings the exit
        # to the touch (owner, 2026-09-10: "if an exit is not earning,
        # then it should be placed closer to the touch")
        self.f.set_fair(NC, 50.0)
        self.f.moved_at.clear()
        self.tick()
        self.assertEqual(self.f.rows[NC]["exit"]["px"], 0.48)

    def test_the_cost_alone_never_keeps_an_exit_off_the_touch(self):
        # 2026-09-10, Senate Republican control: 195 held at a 59c cost,
        # his fair 49c, the ask touch 53c — the exit had sat at 59c,
        # earning nothing
        self.r.positions[NC] = (195.0, 195.0 * 0.59)
        self.f.set_fair(NC, 49.0)
        self.r.exchange.books[NC] = wide_book(self.r.now, bid=0.50, ask=0.53)
        self.tick()
        ex = self.f.rows[NC]["exit"]
        self.assertEqual((ex["side"], ex["px"], ex["qty"]), ("SELL", 0.53, 195.0))
        self.assertGreater(ex["est"], 0.0)
        self.assertAlmostEqual(ex["basis"], 0.59, places=4)
        asks = self.mine(NC, "SELL")
        self.assertEqual((asks[0].price, asks[0].qty), (0.53, 195.0))
        # the touch under both: with company on the side the earnings
        # beat the 2c concession and the exit joins the touch
        self.r.exchange.books[NC] = wide_book(self.r.now, bid=0.44, ask=0.47)
        self.f.moved_at.clear()
        self.tick()
        self.assertEqual(self.f.rows[NC]["exit"]["px"], 0.47)

    def test_his_qualifying_wall_offers_none_of_the_lot_and_widens_the_stake(self):
        # owner, 2026-09-10: "My qualifying orders (1c or 99c) should not
        # impair the tender from placing orders"
        self.r.fam.orders["w1"] = FamilyOrder(id="w1", market=NC, side="SELL", price=0.99,
                                              qty=6000.0, intent=SELL_LONG, placed_ts=self.r.now,
                                              purpose="manual", why="his wall")
        self.r.exchange.live["w1"] = {"id": "w1", "market": NC, "side": "SELL",
                                      "price": 0.99, "size": 6000.0, "intent": SELL_LONG}
        self.r.positions[NC] = (200.0, 200.0 * 0.40)
        self.f.set_fair(NC, 45.0)
        self.tick()
        row = self.f.rows[NC]
        # the exit offers the whole lot beside the wall
        self.assertEqual((row["exit"]["px"], row["exit"]["qty"]), (0.47, 200.0))
        self.assertEqual(self.mine(NC, "SELL")[0].qty, 200.0)
        # the $60 the wall holds counts back into the buying power: the
        # stake is 10% of $2,060, not of $2,000
        self.assertAlmostEqual(self.f.walls_held(), 60.0, places=2)
        self.assertAlmostEqual(row["stake"], 206.0, places=2)
        # a small resting ask of his is not a wall: in a market with a
        # fair it is the tender's (owner, 2026-09-10), so the exit stays
        # one order for the whole lot and his ask comes off beside it
        self.r.fam.orders["h1"] = FamilyOrder(id="h1", market=NC, side="SELL", price=0.48,
                                              qty=50.0, intent=SELL_LONG, placed_ts=self.r.now,
                                              purpose="manual", why="his ask")
        self.r.exchange.live["h1"] = {"id": "h1", "market": NC, "side": "SELL",
                                      "price": 0.48, "size": 50.0, "intent": SELL_LONG}
        self.f.moved_at.clear()
        self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S
        self.tick()
        self.assertEqual(self.f.rows[NC]["exit"]["qty"], 200.0)
        asks = [o for o in self.r.fam.orders.values()
                if o.market == NC and o.side == "SELL" and not focus_mod.is_wall(o)]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0].qty, 200.0)

    def test_the_exit_is_one_order_even_after_the_family_relabels_it(self):
        self.r.positions[NC] = (200.0, 200.0 * 0.40)
        self.f.set_fair(NC, 45.0)
        self.tick()
        ask = self.mine(NC, "SELL")[0]
        ask.purpose = "sell"                          # the family's re-label
        self.r.positions[NC] = (300.0, 300.0 * 0.40)  # the lot grew
        self.f.moved_at.clear()
        self.tick()
        exits = [o for o in self.r.fam.orders.values()
                 if o.market == NC and o.side == "SELL" and o.why.startswith("focus exit")]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0].qty, 300.0)

    def test_an_entry_never_adds_past_the_stake(self):
        # long 400 at 45c: $180 of the $200 stake is held already
        self.r.positions[NC] = (400.0, 180.0)
        self.f.set_fair(NC, 45.0)
        self.tick()
        t = self.f.rows[NC]["tend"]["BUY"]
        if t.get("px"):
            self.assertLessEqual(t["qty"] * t["px"], 20.0 + 1e-6)
        else:
            self.assertIn("no entry that adds", t["note"])
        self.r.positions[NC] = (500.0, 225.0)
        self.tick()
        self.assertIn("no entry that adds", self.f.rows[NC]["tend"]["BUY"]["note"])
        self.assertFalse(self.mine(NC, "BUY"))


class TestTheRecordOfTheOrders(Base):
    """21:00Z, 2026-09-10: the balance-of-power cover was rested and
    pulled "over the cap" eight times in four minutes; a refused resize
    on New York governor was retried every twenty seconds; and two
    orders the desk reported "placed but not resting" filled as the
    owner's own trades. Exits count nothing toward the cap and are
    never trimmed by it; a cover closes the short; a refused placement
    or move waits out the cooldown; an unverified placement is
    withdrawn, claimed, and any fill of it is the tender's."""

    def hide_next_placement(self):
        """The exchange accepts the next placement but its open list
        never shows it (a fill at once, a rejection, a lagging list)."""
        ex = self.r.exchange
        hidden: set = set()
        flag = [True]
        orig_post = ex.post

        def post(url, body, path=None, **kw):
            r = orig_post(url, body, path=path, **kw)
            if url.endswith("/v1/orders") and flag[0]:
                hidden.add(r["order"]["id"])
                flag[0] = False
            return r
        ex.post = post
        ex.open_orders = lambda: [dict(o) for o in ex.live.values() if o["id"] not in hidden]
        # the desk's verify waits on the clock: the rig's sleep must move it
        self.r.desk._sleep = lambda sec: setattr(self.r, "now", self.r.now + sec)
        return hidden, flag

    def test_an_exit_counts_nothing_toward_the_cap_and_is_never_trimmed_by_it(self):
        self.r.positions[NC] = (200.0, 200.0 * 0.40)
        self.f.set_fair(NC, 45.0)
        self.tick()
        self.assertEqual(len(self.mine(NC, "SELL")), 1)      # the exit
        self.assertEqual(len(self.mine(NC, "BUY")), 1)       # an entry
        entry_risk = self.f.risk_used()
        self.assertGreater(entry_risk, 0.0)
        # the cap shrinks under the entry: the entry comes off, the exit stays
        self.f.loss_cap = 0.5
        self.tick()
        self.assertEqual(len(self.mine(NC, "SELL")), 1)
        self.assertFalse(self.mine(NC, "BUY"))
        pulls = [e for e in self.f.log if e.get("event") == "pull" and "cap" in e.get("why", "")]
        self.assertTrue(pulls and all(e["side"] == "BUY" for e in pulls), pulls)
        self.assertEqual(self.f.risk_used(), 0.0)

    def test_a_cover_closes_the_short_and_a_long_resting_as_one_is_relaid(self):
        self.r.positions[NC] = (-200.0, 200.0 * 0.53)
        self.f.set_fair(NC, 40.0)
        self.tick()
        cover = self.mine(NC, "BUY")[0]
        self.assertEqual(cover.intent, focus_mod.SELL_SHORT)
        self.assertEqual(self.r.exchange.live[cover.id]["intent"], focus_mod.SELL_SHORT)
        # a cover the desk laid as a fresh long is re-laid as the close it is
        cover.intent = focus_mod.BUY_LONG
        self.r.exchange.live[cover.id]["intent"] = focus_mod.BUY_LONG
        self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S
        self.tick()
        covers = self.mine(NC, "BUY")
        self.assertEqual(len(covers), 1)
        self.assertNotEqual(covers[0].id, cover.id)
        self.assertEqual(covers[0].intent, focus_mod.SELL_SHORT)
        self.assertNotIn(cover.id, self.r.exchange.live)

    def test_a_placement_the_list_never_shows_is_withdrawn_and_its_fill_is_the_tenders(self):
        hidden, _ = self.hide_next_placement()
        self.f.set_fair(NC, 45.0)
        self.tick()
        self.assertEqual(len(hidden), 1)
        hid = next(iter(hidden))
        refused = [e for e in self.f.log if e.get("event") == "refused"]
        self.assertEqual(len(refused), 1)
        self.assertTrue(refused[0]["note"].startswith("withdrawn"), refused[0])
        side = refused[0]["side"]
        self.assertIn(hid, self.f.mine_ids)                  # claimed
        self.assertNotIn(hid, self.r.exchange.live)          # withdrawn
        self.assertGreater(self.f.moved_at.get(f"{NC}|{side}", 0.0), 0.0)
        # the note says the withdrawal found it (it was live, the list lagged)
        self.assertIn("withdrawn (cancelled)", refused[0]["note"])
        # it had filled before the withdrawal: the journal books it to
        # the tender, and the side stands off
        self.r.fam.fills.append({"ts": self.r.now, "market": NC, "side": side,
                                 "qty": refused[0]["qty"], "oid": hid, "purpose": "hand"})
        self.tick()
        self.assertIn(f"{NC}|{side}", self.f.filled_at)
        self.assertTrue(any(e.get("event") == "filled" and e.get("market") == NC
                            for e in self.f.log))
        self.assertFalse(self.mine(NC, side))

    def test_an_accepted_order_the_capped_list_cannot_show_is_kept(self):
        # 2026-09-11: the open list came back cut at ~250 rows with no
        # paging field; four markets' orders were accepted, never listed,
        # and withdrawn 80 times a day
        hidden, _ = self.hide_next_placement()
        self.r.exchange.open_read = {"pages": 1, "n": 249, "eof": None, "keys": [], "capped": True}
        self.f.set_fair(NC, 45.0)
        self.tick()
        self.assertEqual(len(hidden), 1)
        hid = next(iter(hidden))
        self.assertIn(hid, self.r.exchange.live)             # never withdrawn
        self.assertIn(hid, self.r.fam.orders)                # on the books as the tender's
        self.assertEqual(self.r.fam.orders[hid].purpose, focus_mod.PURPOSE)
        self.assertIn("unverified", self.r.fam.orders[hid].why)
        rested = [e for e in self.f.log if e.get("event") == "rested" and "capped" in (e.get("note") or "")]
        self.assertEqual(len(rested), 1, [e for e in self.f.log if e.get("market") == NC][-3:])
        self.assertFalse([e for e in self.f.log if e.get("event") == "refused"])
        self.assertIn(hid, self.f.mine_ids)

    def test_a_refused_placement_waits_out_the_cooldown(self):
        # 12:29-12:34Z, 2026-09-11: four orders refused every pass, 13 in
        # four minutes — the cooldown was set but never asked on this path
        hidden, _ = self.hide_next_placement()
        self.f.set_fair(NC, 45.0)
        self.tick()
        refused = [e for e in self.f.log if e.get("event") == "refused"]
        self.assertEqual(len(refused), 1)
        side = refused[0]["side"]
        rested_before = len([e for e in self.f.log if e.get("event") == "rested" and e.get("side") == side])
        for _ in range(6):                        # ninety seconds of passes
            self.tick()
        self.assertEqual(len([e for e in self.f.log if e.get("event") == "refused"]), 1)
        self.assertEqual(len([e for e in self.f.log if e.get("event") == "rested" and e.get("side") == side]),
                         rested_before)
        self.assertFalse(self.mine(NC, side))
        # the cooldown out: it tries again, and this time the list shows it
        self.r.now += focus_mod.FOCUS_MOVE_COOLDOWN_S
        self.tick()
        self.assertTrue(self.mine(NC, side))

    def test_a_refused_move_waits_out_the_cooldown(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        bid = self.mine(NC, "BUY")[0]
        # a bigger stake wants a bigger order; the replacement never shows
        self.f.set_stake(NC, 400.0)
        self.f.moved_at.clear()
        hidden, flag = self.hide_next_placement()
        posts = [0]
        ex = self.r.exchange
        inner = ex.post

        def counting(url, body, path=None, **kw):
            if url.endswith("/v1/orders"):
                posts[0] += 1
            return inner(url, body, path=path, **kw)
        ex.post = counting
        self.tick()
        refused = [e for e in self.f.log if e.get("event") == "move_refused"]
        self.assertEqual(len(refused), 1, refused)
        self.assertIn(bid.id, self.r.exchange.live)          # the original stays
        self.assertNotIn(next(iter(hidden)), self.r.exchange.live)
        n = posts[0]
        # the next passes do not try again inside the cooldown
        flag[0] = True
        self.tick()
        self.tick()
        self.assertEqual(posts[0], n)
        self.assertEqual(len([e for e in self.f.log if e.get("event") == "move_refused"]), 1)
        # the cooldown out, the list showing orders again: the move lands
        flag[0] = False
        self.r.now += focus_mod.FOCUS_MOVE_COOLDOWN_S
        self.tick()
        self.assertTrue(any(e.get("event") == "moved" and e.get("market") == NC
                            for e in self.f.log))


class TestHisWallCarriesTheSide(Base):
    def test_a_side_his_wall_carries_to_the_target_earns(self):
        # 22:35Z, 2026-09-10, balance of power dhou-rsen: bids of 301,
        # 70, 100, 215, 429 and his 25,000 at 1c; the T1 target 25,000.
        # The model had stripped his wall with every order of ours and
        # read the bid side as earning nothing — the exit at the touch
        # showed $0.00 a day
        # (2026-09-11: an exit is placed as an entry is, and the 10c
        # concession of a cover at 41c against his 31c fair needs
        # company within six cents — 300 shares at 40c and 200 at 38c
        # stand in here; on the bare book of that night the cover would
        # rest at his fair)
        thin = Book(bids=((0.41, 2.0), (0.40, 300.0), (0.38, 200.0), (0.32, 70.0),
                          (0.25, 100.0), (0.23, 215.0), (0.02, 429.0), (0.01, 37500.0)),
                    asks=((0.42, 87.0), (0.43, 36.0), (0.48, 70.0), (0.99, 27100.0)),
                    tick=0.01, fetched_at=self.r.now)
        self.r.exchange.books[NC] = thin
        self.r.fam.orders["wall"] = FamilyOrder(id="wall", market=NC, side="BUY", price=0.01,
                                                qty=25000.0, intent=BUY_LONG,
                                                placed_ts=self.r.now - 3600,
                                                purpose="manual", why="his wall")
        self.r.exchange.live["wall"] = {"id": "wall", "market": NC, "side": "BUY",
                                        "price": 0.01, "size": 25000.0, "intent": BUY_LONG}
        self.r.positions[NC] = (-299.0, 299.0 * 0.583)
        self.f.set_fair(NC, 31.0)
        self.tick()
        row = self.f.rows[NC]
        # the exit at the touch earns nearly the whole bid-side pool
        self.assertEqual(row["exit"]["px"], 0.41)
        self.assertGreater(row["exit"]["est"], 100.0, row["exit"])
        # his wall itself earns nothing (forty ticks back) and is shown so
        wall = [d for d in row["orders"] if d["id"] == "wall"][0]
        self.assertLess(wall["est"], 1.0)
        # the bare book of that night: two shares at the touch, the
        # next bid 9c back — the cover rests at his fair, no concession
        bare = Book(bids=((0.41, 2.0), (0.32, 70.0), (0.25, 100.0), (0.23, 215.0),
                          (0.02, 429.0), (0.01, 37500.0)),
                    asks=thin.asks, tick=0.01, fetched_at=self.r.now)
        prog = self.f.terms.get(NC)
        pool = self.f.fam._side_pool(NC, prog)
        ex = self.f._exit_plan(NC, "BUY", bare, prog, pool, 0.31, 299.0, 0.417)
        self.assertEqual(ex["px"], 0.31)
        # without the wall the side is short of the target: nothing earns
        self.r.fam.orders.pop("wall")
        self.r.exchange.live.pop("wall")
        self.r.exchange.books[NC] = Book(bids=thin.bids[:-1] + ((0.01, 12500.0),),
                                         asks=thin.asks, tick=0.01, fetched_at=self.r.now)
        self.tick()
        self.assertEqual(self.f.rows[NC]["exit"]["est"], 0.0)


class TestAGhostOfItsOwnMoveIsNotAdopted(Base):
    def test_the_lagging_original_of_a_move_stays_out_of_the_tenders_hands(self):
        # 00:05Z, 2026-09-11: the open list showed the original of a
        # move for a read after its cancel; the family adopted it as his
        # and the tender adopted it back — four times in an hour
        self.f.set_fair(NC, 45.0)
        self.tick()
        bid = self.mine(NC, "BUY")[0]
        self.f.set_stake(NC, 400.0)
        self.f.moved_at.clear()
        self.tick()
        self.assertNotIn(bid.id, self.r.fam.orders)          # moved: the original is gone
        new = self.mine(NC, "BUY")[0]
        # the list lags: the family brings the original back as his
        self.r.fam.orders[bid.id] = FamilyOrder(id=bid.id, market=NC, side="BUY", price=bid.price,
                                                qty=bid.qty, intent=BUY_LONG, placed_ts=self.r.now,
                                                purpose="manual", why="the owner's own order")
        n_log = len(self.f.log)
        self.tick()
        self.assertEqual(self.r.fam.orders[bid.id].purpose, "manual")
        self.assertFalse([e for e in self.f.log[n_log:] if e.get("event") in ("adopted", "pull")])
        self.assertEqual(self.mine(NC, "BUY"), [new] if new.id in self.r.fam.orders else self.mine(NC, "BUY"))
        # ten minutes on, a manual order with that id would be a new one of his
        self.r.now += focus_mod.FOCUS_VANISH_WAIT_S
        self.tick()
        self.assertNotIn(bid.id, self.f._gone_by_me)


class TestTheCapHasSlack(Base):
    def test_a_little_over_the_cap_pulls_nothing_and_a_fresh_order_waits(self):
        # 23:32-23:35Z: orders rested under the cap were pulled "over the
        # cap" three minutes later as the readings moved
        self.f.set_fair(NC, 45.0)
        self.f.set_fair(OH, 52.0)
        self.tick()
        entries = [o for o in self.mine() if not self.f._exit_order(o)]
        self.assertGreaterEqual(len(entries), 2)
        used = self.f.risk_used()
        self.assertGreater(used, 0.0)
        # the cap a hair under what rests: within the slack, nothing moves
        self.f.loss_cap = used / 1.05
        self.tick()
        self.assertFalse([e for e in self.f.log if e.get("event") == "pull" and "cap" in e.get("why", "")])
        self.assertEqual(len([o for o in self.mine() if not self.f._exit_order(o)]), len(entries))
        # past the slack but not far over: the orders just rested are spared
        self.f.loss_cap = used / 1.15
        self.tick()
        self.assertFalse([e for e in self.f.log if e.get("event") == "pull" and "cap" in e.get("why", "")])
        # five minutes on, the weakest comes off
        self.r.now += focus_mod.FOCUS_CAP_GRACE_S
        self.tick()
        pulls = [e for e in self.f.log if e.get("event") == "pull" and "cap" in e.get("why", "")]
        self.assertTrue(pulls)
        # far over: even a fresh order comes off
        self.f.loss_cap = 0.5
        self.f.moved_at.clear()
        self.tick()
        self.assertFalse([o for o in self.mine() if not self.f._exit_order(o)])


class TestTheTenderJudgesByItsOwnNumbers(Base):
    def test_the_familys_rescoring_of_a_tender_order_changes_nothing(self):
        # 01:38Z, 2026-09-11: a bid rested at +$168 a day read −$697 a
        # minute later — the family's own model rescoring the same record
        self.f.set_fair(NC, 45.0)
        self.tick()
        o = self.mine(NC, "BUY")[0]
        self.assertGreater(self.f._own_ev(o), 0.0)
        for _ in range(3):
            o.live_ev = -697.0                      # the family's reading
            o.live_pf = 0.99
            self.r.now += focus_mod.FOCUS_WEAK_DWELL_S
            self.tick()
        self.assertIn(o.id, self.r.exchange.live)   # never pulled as weak
        self.assertGreater(self.f._own_ev(o), 0.0)  # the tender's own score stands
        self.assertNotIn(f"{NC}|BUY", self.f.weak_since)
        # and the cap's accounting uses the tender's fill odds, not 0.99
        self.assertLess(self.f._own_pf(o), 0.99)


class TestTheCapGoesByValue(Base):
    """Owner, 2026-09-11: "Yes to value ranked cap allocation"."""

    def entries(self, slug=None):
        return [o for o in self.mine(slug) if not self.f._exit_order(o)]

    def test_a_better_plan_displaces_the_weakest_resting_entries(self):
        # Alaska governor ($600 a day over seven markets) rests first and
        # fills the cap; the NC Senate plan ($1,500 a day over two) is
        # worth far more per dollar of expected loss and takes the room
        self.f.set_fair(AK, 30.0)
        self.tick()
        ak = self.entries(AK)
        self.assertTrue(ak)
        self.f.loss_cap = self.f.risk_used()          # full
        self.r.now += focus_mod.FOCUS_DISPLACE_GRACE_S  # past the grace
        self.f.set_fair(NC, 45.0)
        self.tick()
        self.assertTrue(self.entries(NC))
        pulls = [e for e in self.f.log if e.get("event") == "pull" and "displaced" in e.get("why", "")]
        self.assertTrue(pulls)
        self.assertTrue(all(e["market"] == AK for e in pulls))
        self.assertLess(len(self.entries(AK)), len(ak))
        self.assertLessEqual(self.f.risk_used(), self.f.loss_cap * focus_mod.FOCUS_CAP_SLACK + 1e-6)

    def test_the_margin_and_the_grace_hold(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        # against the weakest resting entry (a market rests two sides)
        o = min(self.entries(NC), key=lambda x: self.f._value(float(x.live_ev), self.f._entry_risk(x)))
        risk = self.f._entry_risk(o)
        val = self.f._value(float(o.live_ev), risk)
        used = self.f.risk_used()
        self.f.loss_cap = used
        plan = {"ev": val * 1.9 * risk, "risk": risk}
        # inside the grace: nothing is displaced however good the plan
        self.assertIsNone(self.f._make_room(self.r.now, OH, "BUY",
                                            {"ev": val * 5 * risk, "risk": risk}, used, 8))
        self.r.now += focus_mod.FOCUS_DISPLACE_GRACE_S
        # not double: no displacement
        self.assertIsNone(self.f._make_room(self.r.now, OH, "BUY", plan, used, 8))
        self.assertIn(o.id, self.r.exchange.live)
        # double and more: the weaker order comes off, and its side may
        # displace nothing for an hour
        room = self.f._make_room(self.r.now, OH, "BUY", {"ev": val * 2.1 * risk, "risk": risk}, used, 8)
        self.assertIsNotNone(room)
        self.assertAlmostEqual(room[0], risk, places=6)
        self.assertNotIn(o.id, self.r.exchange.live)
        self.assertGreater(self.f.moved_at.get(f"{NC}|{o.side}", 0.0), 0.0)
        self.assertIsNone(self.f._make_room(self.r.now, NC, o.side,
                                            {"ev": val * 9 * risk, "risk": risk}, used, 8))
        d = json.loads(json.dumps(self.f.to_dict()))
        g = Focus(self.r.fam, self.r.exchange, self.b, clock=lambda: self.r.now)
        g.restore(d)
        self.assertIn(f"{NC}|{o.side}", g.displaced_at)


class TestSilverSeedsTheFairsHeNamed(Base):
    def test_a_named_market_gets_silvers_number_once(self):
        # owner, 2026-09-10: "those little hanging fruit markets that you
        # identified except for Alaska set the fair to Nate Silvers"
        saved = focus_mod.FOCUS_SILVER_FAIRS
        focus_mod.FOCUS_SILVER_FAIRS = (OH, AK)
        try:
            self.assertNotIn(OH, self.f.fairs)
            self.tick()
            self.assertEqual(self.f.fairs[OH], 0.52)             # Silver's number
            self.assertIn(OH, self.f.silver_seeded)
            self.assertTrue(any(e.get("event") == "fair_set" and e.get("market") == OH
                                and "Silver" in e.get("note", "") for e in self.f.log))
            self.assertTrue(self.mine(OH))                       # tended from it now
            # AK is on the list here only to show a clear is final: cleared
            # by him, never seeded again
            self.assertEqual(self.f.fairs[AK], 0.30)
            self.f.set_fair(AK, "-")
            self.tick()
            self.assertNotIn(AK, self.f.fairs)
            # and the seed survives a restart
            d = json.loads(json.dumps(self.f.to_dict()))
            g = Focus(self.r.fam, self.r.exchange, self.b, clock=lambda: self.r.now)
            g.restore(d)
            self.assertEqual(g.silver_seeded, {OH, AK})
        finally:
            focus_mod.FOCUS_SILVER_FAIRS = saved


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

    def test_a_tender_order_he_moves_is_tended_again_where_a_fair_is_set(self):
        # owner, 2026-09-10: "my orders should be like any other the
        # tender places, susceptible to being moved if there is another
        # place they could be resting that is more positive ev"
        self.f.set_fair(NC, 45.0)
        self.tick()
        o = self.mine(NC, "BUY")[0]
        r = self.f.move(NC, o.id, cents=40.0)
        self.assertTrue(r["ok"], r)
        rec = self.r.fam.orders[r["order_id"]]
        self.assertEqual(rec.purpose, "manual")
        self.f.moved_at.clear()
        self.tick()
        # his bid five ticks back earns less than the plan: it is the
        # tender's again and re-laid at the plan, one order on the side
        bids = self.mine(NC, "BUY")
        self.assertEqual(len(bids), 1)
        self.assertNotEqual(bids[0].price, 0.40)
        self.assertNotIn(rec.id, self.r.exchange.live)
        self.assertEqual(len([x for x in self.r.fam.orders.values()
                              if x.market == NC and x.side == "BUY"]), 1)

    def test_his_orders_are_the_tenders_only_where_a_fair_is_set_and_never_his_walls(self):
        def his(oid, slug, side, px, q):
            self.r.fam.orders[oid] = FamilyOrder(id=oid, market=slug, side=side, price=px,
                                                 qty=q, intent=BUY_LONG if side == "BUY" else SELL_LONG,
                                                 placed_ts=self.r.now - 600, purpose="manual",
                                                 why="the owner's own order")
            self.r.exchange.live[oid] = {"id": oid, "market": slug, "side": side, "price": px,
                                         "size": q, "intent": BUY_LONG if side == "BUY" else SELL_LONG}
        his("h1", NC, "BUY", 0.40, 100.0)            # a fair is set here
        his("h2", OH, "BUY", 0.40, 100.0)            # no fair: stays his
        his("h3", NC, "BUY", 0.01, 25000.0)          # his qualifying wall: stays his
        self.f.set_fair(NC, 45.0)
        self.tick()
        self.assertEqual(self.r.fam.orders["h2"].purpose, "manual")
        self.assertEqual(self.r.fam.orders["h3"].purpose, "manual")
        self.assertIn("h3", self.r.exchange.live)
        self.assertIn("h2", self.r.exchange.live)
        # his NC bid became the tender's and was re-laid at the plan
        self.assertNotIn("h1", self.r.exchange.live)
        bids = [x for x in self.r.fam.orders.values()
                if x.market == NC and x.side == "BUY" and not focus_mod.is_wall(x)]
        self.assertEqual(len(bids), 1)
        self.assertEqual(bids[0].purpose, PURPOSE)
        self.assertTrue(any(e.get("event") == "moved" and e.get("market") == NC
                            for e in self.f.log))

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


class TestTheGlance(Base):
    def test_a_row_carries_the_rate_the_shares_and_the_eight_hour_peak(self):
        # owner, 2026-09-11: "the name, the earning rate summed for all
        # orders on that market, how many shares I own, and the drop in
        # earning rate from its 8 hr peak"
        self.r.positions[NC] = (120.0, 48.0)
        self.f.set_fair(NC, 45.0)
        self.tick()
        self.tick()
        row = self.f.rows[NC]
        self.assertEqual(row["shares"], 120.0)
        self.assertGreater(row["rate"], 0.0)
        self.assertAlmostEqual(row["rate"], sum(d["est"] for d in row["orders"]), places=2)
        self.assertAlmostEqual(row["peak8"], row["rate"], places=2)
        self.assertEqual(row["drop"], 0.0)
        peak = row["peak8"]
        # the tender's orders come off: the rate falls, the peak remembers
        self.f.pause(NC, True)
        self.tick()
        self.tick()
        row = self.f.rows[NC]
        self.assertLess(row["rate"], peak)
        self.assertAlmostEqual(row["peak8"], peak, places=2)
        self.assertAlmostEqual(row["drop"], peak - row["rate"], places=2)
        # eight hours on, the old peak is forgotten
        self.r.now += focus_mod.FOCUS_PEAK_WINDOW_S + focus_mod.FOCUS_PEAK_BUCKET_S
        self.tick()
        row = self.f.rows[NC]
        self.assertAlmostEqual(row["peak8"], row["rate"], places=2)
        # and the history survives a restart
        d = json.loads(json.dumps(self.f.to_dict()))
        g = Focus(self.r.fam, self.r.exchange, self.b, clock=lambda: self.r.now)
        g.restore(d)
        self.assertIn(NC, g.rate_hist)

    def test_a_side_under_the_target_reads_unqualified(self):
        # owner, 2026-09-11: "Maintenance may also result in many markets
        # being unqualified for a while" — the exchange's maintenance
        # cancelled every resting order, the walls that carried the
        # sides to the target included
        self.f.set_fair(NC, 45.0)
        self.tick()
        row = self.f.rows[NC]
        self.assertEqual(row["unqualified"], 0)
        self.assertTrue(row["qual"]["bid"][2] and row["qual"]["ask"][2])
        self.assertEqual(row["qual"]["bid"][1], 25000.0)
        thin = Book(bids=((0.44, 300.0), (0.43, 500.0)),          # the 1c wall gone
                    asks=((0.47, 300.0), (0.48, 500.0), (0.98, 60000.0)),
                    tick=0.01, fetched_at=self.r.now)
        self.r.exchange.books[NC] = thin
        self.tick()
        row = self.f.rows[NC]
        self.assertEqual(row["unqualified"], 1)
        self.assertFalse(row["qual"]["bid"][2])
        self.assertEqual(row["qual"]["bid"][0], 800)
        self.assertTrue(row["qual"]["ask"][2])
        # the qualify button's figures: the bonds page's wall, to 125%
        # of the target at the far edge (owner, 2026-09-11 "a button
        # similar to the one on the bonds page that lets me
        # automatically qualify the ask side")
        w = row["wall"]
        self.assertEqual(w["bid"]["goal"], 31250)
        self.assertEqual(w["bid"]["gap"], 31250 - 800)
        self.assertEqual(w["bid"]["px"], 0.01)
        self.assertAlmostEqual(w["bid"]["usd"], (31250 - 800) * 0.01, places=2)
        self.assertFalse(w["bid"]["room"])
        self.assertTrue(w["ask"]["room"])
        self.assertEqual(w["ask"]["gap"], 0)
        # the run's note rides on the view when the monitor has one
        self.f.wall_note = lambda slug: "3 orders, 900 shares" if slug == NC else None
        v = self.f.view(self.r.now, 2000.0, True)
        self.assertEqual([r.get("qualify") for r in v["rows"] if r["market"] == NC],
                         ["3 orders, 900 shares"])
        # nothing on the unqualified side reads as earning
        self.assertEqual(sum(d["est"] for d in row["orders"] if d["side"] == "BUY"), 0.0)


class TestTheCompanyWindowIsSixCents(Base):
    # the House rep control book of 2026-09-11 19:12Z: ticks of 0.1c,
    # 50 shares at the 17.6c touch and 577 at 17.4c, 13,400 shares
    # 1.5c back; six TICKS of company was 0.6c and read the side as
    # bare against a $139 stake, so the bid sat at his 16c fair earning
    # nothing while a bid at the touch was worth about $170 a day
    HOUSE = Book(bids=((0.176, 50.0), (0.174, 577.0), (0.162, 86.0), (0.161, 5100.0),
                       (0.160, 8300.0), (0.157, 4.0), (0.15, 37.0), (0.14, 10400.0),
                       (0.01, 400000.0)),
                 asks=((0.177, 9900.0), (0.179, 500.0), (0.18, 2600.0), (0.193, 5.0),
                       (0.194, 707.0), (0.20, 500.0), (0.42, 275.0), (0.98, 900.0),
                       (0.99, 400000.0)),
                 tick=0.001, fetched_at=0.0)

    def test_company_is_counted_six_cents_back_whatever_the_tick(self):
        levels = self.f._levels_net(NC, "BUY", self.HOUSE)
        self.assertFalse(self.f._bare(levels, 0.001, 787.0))      # 14,113 shares within 6c
        # only the first six ticks: 627 shares, bare — the old reading
        near = sum(q for p, q in levels if abs(p - 0.176) <= 6 * 0.001 + 1e-9)
        self.assertLess(near, 787.0)
        # and on a 1c-tick book six cents is the same six ticks as before
        deep = Book(bids=((0.44, 300.0),), asks=((0.46, 4000.0), (0.47, 6000.0), (0.53, 9000.0)),
                    tick=0.01, fetched_at=0.0)
        self.assertFalse(self.f._bare(self.f._levels_net(NC, "SELL", deep), 0.01, 370.0))
        bare = Book(bids=((0.44, 300.0),), asks=((0.46, 20.0), (0.47, 30.0), (0.53, 9000.0)),
                    tick=0.01, fetched_at=0.0)
        self.assertTrue(self.f._bare(self.f._levels_net(NC, "SELL", bare), 0.01, 370.0))

    def test_the_search_reaches_the_resting_levels_six_cents_back(self):
        cands = self.f._cands("BUY", self.HOUSE, 0.16)
        for px in (0.176, 0.175, 0.170):                        # every tick near the touch
            self.assertIn(px, cands)
        for px in (0.162, 0.161, 0.160, 0.157, 0.15):           # the levels within six cents
            self.assertIn(px, cands)
        self.assertIn(0.14, cands)                               # 3.6c back, within six cents
        self.assertNotIn(0.01, cands)                            # the wall, far past the window
        self.assertNotIn(0.165, cands)                           # an empty slot past six ticks

    def test_a_bid_past_fair_rests_where_the_company_is(self):
        self.f.set_fair(NC, 16.0)
        prog = self.f.terms.get(NC)
        pool = self.f.fam._side_pool(NC, prog)
        plan = self.f._entry_plan(NC, "BUY", self.HOUSE, prog, pool, 0.16, 138.59)
        self.assertIsNotNone(plan)
        self.assertGreater(plan["px"], 0.16 + 1e-9)              # past his fair, with company
        self.assertGreater(plan["conc"], 0.0)                    # the concession charged
        self.assertGreater(plan["est"], 20.0)                    # and it earns


class TestTheConcessionIsUnwoundMidway(Base):
    """Owner, 2026-09-12: "The fair amount shouldn't affect the fill
    odds. The fill odds should be based on the shape of the book. The
    concession should affect the ev but make the concession as if I
    sell it back midway between my fair price and the current price."
    The New York governor rep book of 08:19 EDT: short 392, his fair
    8c, the bid 10c x4.7k, the ask 11c; the cover had sat at 4c earning
    nothing because a 2c concession had pushed the fill odds toward
    certain and the 10c touch read worse than a 4c lottery ticket."""
    NY = "usgubewc-usgub-ny-2026-11-03-rep"
    NYP = {"timePeriods": [{"programId": "midterms_t3_coverage_gov_senate_house_districts_20260911",
                            "rewardPool": 250.0, "targetSize": 10000, "discountFactor": 0.2,
                            "status": "LIVE", "start": "2026-09-11T19:00:00Z",
                            "end": "2026-11-04T00:00:00Z"}]}
    BOOK = Book(bids=((0.10, 4700.0), (0.06, 19.0), (0.05, 51.0), (0.04, 25.0),
                      (0.02, 335.0), (0.01, 13600.0)),
                asks=((0.11, 1600.0), (0.17, 89.0), (0.18, 45.0), (0.96, 615.0),
                      (0.97, 695.0), (0.98, 830.0), (0.99, 24800.0)),
                tick=0.01, fetched_at=0.0)

    def _ready(self):
        self.r.add_market(self.NY, self.BOOK, event="ny", prog=self.NYP)
        self.r.fam.universe[self.NY] = {"event_n": 2, "name": self.NY}
        self.f.set_fair(self.NY, 8.0)
        self.f.last_terms_own = 0.0
        self.f._rotor = 0
        for _ in range(4):
            self.tick()
        prog = self.f.terms.get(self.NY)
        pool = self.f.fam._side_pool(self.NY, prog)
        book = self.r.cache.any_age(self.NY)
        return prog, pool, book

    def test_the_fill_odds_read_the_book_not_his_fair(self):
        prog, pool, book = self._ready()
        levels = self.f._levels_net(self.NY, "BUY", book)
        at8 = self.f._score(self.NY, "BUY", book, prog, pool, 0.08, 0.10, 392.0, levels, is_exit=True)
        at10 = self.f._score(self.NY, "BUY", book, prog, pool, 0.10, 0.10, 392.0, levels, is_exit=True)
        self.assertAlmostEqual(at8["pf"], at10["pf"], places=6)
        self.assertGreater(at8["est"], 5.0)

    def test_the_concession_is_the_unwind_midway_to_the_current_price(self):
        prog, pool, book = self._ready()
        levels = self.f._levels_net(self.NY, "BUY", book)
        at10 = self.f._score(self.NY, "BUY", book, prog, pool, 0.08, 0.10, 392.0, levels, is_exit=True)
        self.assertAlmostEqual(at10["past"], 0.02, places=4)     # 2c past his fair
        self.assertAlmostEqual(at10["conc"], 0.01, places=4)     # unwound at 9c: 1c a share
        self.assertAlmostEqual(at10["loss"], at10["pf"] * 392.0 * 0.01, places=3)
        at9 = self.f._score(self.NY, "BUY", book, prog, pool, 0.08, 0.09, 392.0, levels, is_exit=True)
        self.assertAlmostEqual(at9["conc"], 0.0, places=4)       # at the unwind price: nothing
        entry = self.f._score(self.NY, "BUY", book, prog, pool, 0.08, 0.10, 100.0, levels)
        self.assertAlmostEqual(entry["conc"], 0.01, places=4)    # an entry the same way

    def test_the_cover_rests_at_the_touch_where_it_earns(self):
        prog, pool, book = self._ready()
        ex = self.f._exit_plan(self.NY, "BUY", book, prog, pool, 0.08, 392.0, 0.81)
        self.assertIsNotNone(ex)
        self.assertAlmostEqual(ex["px"], 0.10, places=4)
        self.assertGreater(ex["est"], 5.0)
        at4 = self.f._score(self.NY, "BUY", book, prog, pool, 0.08, 0.04, 392.0,
                            self.f._levels_net(self.NY, "BUY", book), is_exit=True)
        self.assertGreater(ex["ev"], 2 * at4["ev"])


class TestGhostNetting(Base):
    """Owner, 2026-09-12 "Yes, do the ghost netting": an order the tender
    moved or pulled is netted out of the book for a minute — the
    exchange's book shows it for a read or two after the cancel, and
    it had read as company and as the touch (the NY governor rep cover
    flipped 4c<->10c chasing its own shadow)."""

    def _rest_and_pull(self):
        self.f.set_fair(NC, 45.0)
        self.tick()
        o = self.mine(NC, "BUY")[0]
        # the tender's own pull: the record goes, the id is forgotten
        self.r.exchange.live.pop(o.id, None)
        self.r.fam.orders.pop(o.id, None)
        self.f._forget(o.id)
        return o

    def test_the_ghost_is_netted_out_while_the_book_still_shows_it(self):
        o = self._rest_and_pull()
        ghost = Book(bids=((o.price, o.qty + 5.0), (0.02, 60000.0)),
                     asks=((0.47, 300.0), (0.98, 60000.0)), tick=0.01, fetched_at=self.r.now)
        lv = dict(self.f._levels_net(NC, "BUY", ghost))
        self.assertAlmostEqual(lv.get(o.price, 0.0), 5.0, places=6)     # the others alone
        only = Book(bids=((o.price, o.qty), (0.02, 60000.0)),
                    asks=((0.47, 300.0), (0.98, 60000.0)), tick=0.01, fetched_at=self.r.now)
        lv2 = self.f._levels_net(NC, "BUY", only)
        self.assertNotIn(o.price, dict(lv2))                             # no level, no touch
        self.assertAlmostEqual(lv2[0][0], 0.02, places=6)

    def test_the_ghost_is_forgotten_after_three_minutes(self):
        o = self._rest_and_pull()
        self.r.now += focus_mod.FOCUS_GHOST_S + 1.0
        late = Book(bids=((o.price, o.qty), (0.02, 60000.0)),
                    asks=((0.47, 300.0), (0.98, 60000.0)), tick=0.01, fetched_at=self.r.now)
        lv = dict(self.f._levels_net(NC, "BUY", late))
        self.assertAlmostEqual(lv.get(o.price, 0.0), o.qty, places=6)   # a stranger's now
        self.assertFalse(self.f.departed.get(f"{NC}|BUY"))

    def test_the_ghost_outlasts_the_exit_cooldown(self):
        # 16:41-16:44Z, 2026-09-12: at a minute the memory expired at the
        # very pass the exit cooldown let the cover move again, on a book
        # read up to 45 s earlier that still showed the old order — the
        # New York governor rep cover flipped 8c<->10c every minute with
        # the netting in place
        o = self._rest_and_pull()
        self.assertGreater(focus_mod.FOCUS_GHOST_S,
                           focus_mod.FOCUS_EXIT_COOLDOWN_S + focus_mod.FOCUS_ACT_AGE_S)
        self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S + 1.0
        stale = Book(bids=((o.price, o.qty + 5.0), (0.02, 60000.0)),
                     asks=((0.47, 300.0), (0.98, 60000.0)), tick=0.01,
                     fetched_at=self.r.now - 40.0)
        lv = dict(self.f._levels_net(NC, "BUY", stale))
        self.assertAlmostEqual(lv.get(o.price, 0.0), 5.0, places=6)     # still netted

    def test_a_book_read_well_after_the_cancel_is_not_netted(self):
        o = self._rest_and_pull()
        fresh = Book(bids=((o.price, o.qty), (0.02, 60000.0)),
                     asks=((0.47, 300.0), (0.98, 60000.0)), tick=0.01,
                     fetched_at=self.r.now + focus_mod.FOCUS_GHOST_S + 1.0)
        lv = dict(self.f._levels_net(NC, "BUY", fresh))
        self.assertAlmostEqual(lv.get(o.price, 0.0), o.qty, places=6)

    def test_a_level_showing_less_than_the_ghost_has_already_lost_it(self):
        # the exchange has taken the order off: what is left at the level
        # is the others', and stripping the ghost from it would read the
        # side barer than it is
        o = self._rest_and_pull()
        gone = Book(bids=((o.price, o.qty - 10.0), (0.02, 60000.0)),
                    asks=((0.47, 300.0), (0.98, 60000.0)), tick=0.01, fetched_at=self.r.now)
        lv = dict(self.f._levels_net(NC, "BUY", gone))
        self.assertAlmostEqual(lv.get(o.price, 0.0), o.qty - 10.0, places=6)

    def test_a_fresh_rest_pulled_before_it_was_ever_scored_still_leaves_its_ghost(self):
        # the price is remembered at the rest itself, not only at the
        # next pass's scoring
        self.f.set_fair(NC, 45.0)
        self.tick()
        o = self.mine(NC, "BUY")[0]
        self.assertIn(o.id, self.f._px_seen)
        self.r.exchange.live.pop(o.id, None)
        self.r.fam.orders.pop(o.id, None)
        self.f._forget(o.id)
        ghosts = self.f.departed.get(f"{NC}|BUY") or []
        self.assertTrue(any(abs(g[0] - o.price) < 1e-9 and abs(g[1] - o.qty) < 1e-9 for g in ghosts))
        self.assertNotIn(o.id, self.f._px_seen)


class TestTheTenderKeepsItsOwnIds(Base):
    def test_ids_of_orders_still_resting_survive_the_trim(self):
        # 20:37Z, 2026-09-11: an eleven-hour-old cover's id had been
        # trimmed by age, the cover read as his, and a second one rested
        with mock.patch.object(focus_mod, "MINE_IDS_KEEP", 3):
            for i in range(3):
                oid = f"old{i}"
                self.r.fam.orders[oid] = FamilyOrder(id=oid, market=NC, side="BUY", price=0.46,
                                                     qty=84.0, intent=focus_mod.SELL_SHORT,
                                                     placed_ts=self.r.now, purpose="sell",
                                                     why="an exit — its fill reduces the position")
                self.f._claim_id(oid)
            self.f._claim_id("gone0")                         # a moved order, no longer resting
            for i in range(6):
                self.f._claim_id(f"new{i}")
            self.assertTrue(all(f"old{i}" in self.f.mine_ids for i in range(3)))
            self.assertNotIn("gone0", self.f.mine_ids)
            self.assertIn("new5", self.f.mine_ids)

    def test_an_exit_side_his_orders_cover_pulls_the_tenders_own_exit(self):
        # his cover of the whole short rests; the tender's own duplicate
        # comes off rather than offering the lot twice
        self.r.positions[NC] = (-84.0, 84.0 * 0.54)
        self.f.set_fair(NC, 62.0)
        self.r.fam.orders["his"] = FamilyOrder(id="his", market=NC, side="BUY", price=0.44,
                                               qty=84.0, intent=focus_mod.SELL_SHORT,
                                               placed_ts=self.r.now - 3600, purpose="sell",
                                               why="an exit — its fill reduces the position")
        self.r.exchange.live["his"] = {"id": "his", "market": NC, "side": "BUY",
                                       "price": 0.44, "size": 84.0, "intent": focus_mod.SELL_SHORT}
        self.r.fam.orders["dup"] = FamilyOrder(id="dup", market=NC, side="BUY", price=0.44,
                                               qty=84.0, intent=focus_mod.SELL_SHORT,
                                               placed_ts=self.r.now - 60, purpose=PURPOSE,
                                               why="focus exit: ~$1.00/day")
        self.r.exchange.live["dup"] = {"id": "dup", "market": NC, "side": "BUY",
                                       "price": 0.44, "size": 84.0, "intent": focus_mod.SELL_SHORT}
        self.f._claim_id("dup")
        self.tick()
        self.tick()
        self.assertNotIn("dup", self.r.exchange.live)          # the duplicate is gone
        self.assertIn("his", self.r.exchange.live)             # his stands
        self.assertEqual(self.f.rows[NC]["exit"]["note"], "your own orders already offer the lot")
        self.assertTrue(any(e["event"] == "pull" and "already offer" in (e.get("why") or "")
                            for e in self.f.log))


class TestAnExitIsPlacedAsAnEntryIs(Base):
    # the Iowa Senate rep book of 2026-09-11 19:35Z: 201 held at 68.6c,
    # his fair 63c, the ask touch 61c. The exit had sat at his fair, two
    # ticks back, earning $19 a day where the touch was worth about $140
    # (owner: "This is another one where it seems it's not going below
    # fair" ... "Yes")
    IOWA = Book(bids=((0.60, 4200.0), (0.58, 26.0), (0.54, 76.0), (0.52, 18.0), (0.51, 100.0),
                      (0.02, 167.0), (0.01, 38500.0)),
                asks=((0.61, 132.0), (0.62, 303.0), (0.63, 1000.0), (0.64, 804.0), (0.68, 83.0),
                      (0.69, 10500.0), (0.77, 230.0), (0.99, 40000.0)),
                tick=0.01, fetched_at=0.0)
    BARE = Book(bids=((0.60, 4200.0), (0.01, 38500.0)),
                asks=((0.61, 5.0), (0.62, 3.0), (0.99, 40000.0)),
                tick=0.01, fetched_at=0.0)

    def test_the_exit_goes_under_his_fair_where_the_earnings_beat_the_concession(self):
        self.f.set_fair(NC, 63.0)
        prog = self.f.terms.get(NC)
        pool = self.f.fam._side_pool(NC, prog)
        ex = self.f._exit_plan(NC, "SELL", self.IOWA, prog, pool, 0.63, 201.0, 0.686)
        self.assertIsNotNone(ex)
        self.assertLess(ex["px"], 0.63 - 1e-9)                 # under his fair
        self.assertIn(ex["px"], (0.61, 0.62))
        self.assertGreater(ex["conc"], 0.0)                     # the concession charged
        self.assertGreater(ex["est"], 5 * ex["loss"])           # and the earnings beat it
        at_fair = self.f._score(NC, "SELL", self.IOWA, prog, pool, 0.63, 0.63, 201.0,
                                self.f._levels_net(NC, "SELL", self.IOWA), is_exit=True)
        self.assertGreater(ex["ev"], at_fair["ev"])
        self.assertAlmostEqual(ex["basis"], 0.686, places=4)   # the cost carried, never a floor

    def test_on_a_bare_side_the_exit_rests_at_his_fair(self):
        self.f.set_fair(NC, 63.0)
        prog = self.f.terms.get(NC)
        pool = self.f.fam._side_pool(NC, prog)
        ex = self.f._exit_plan(NC, "SELL", self.BARE, prog, pool, 0.63, 201.0, 0.686)
        self.assertIsNotNone(ex)
        self.assertGreaterEqual(ex["px"], 0.63 - 1e-9)
        self.assertEqual(ex["conc"], 0.0)

    def test_an_exit_under_fair_that_lost_its_company_is_moved_back(self):
        self.r.positions[NC] = (201.0, 201.0 * 0.686)
        self.f.set_fair(NC, 63.0)
        self.r.exchange.books[NC] = Book(bids=self.IOWA.bids, asks=self.IOWA.asks, tick=0.01,
                                         fetched_at=self.r.now)
        self.tick()
        self.tick()
        asks = [o for o in self.mine(NC, "SELL") if self.f._exit_order(o)]
        self.assertEqual(len(asks), 1)
        self.assertLess(asks[0].price, 0.63 - 1e-9)
        self.r.exchange.books[NC] = Book(bids=self.BARE.bids, asks=self.BARE.asks, tick=0.01,
                                         fetched_at=self.r.now)
        self.r.now += focus_mod.FOCUS_EXIT_COOLDOWN_S + 1.0
        self.tick()
        self.tick()
        asks = [o for o in self.mine(NC, "SELL") if self.f._exit_order(o)]
        self.assertEqual(len(asks), 1)
        self.assertGreaterEqual(asks[0].price, 0.63 - 1e-9)


class TestABareSideGetsNoConcession(Base):
    def test_past_fair_needs_company(self):
        # owner, 2026-09-11, after the maintenance wiped the books: "Be
        # careful of placing orders after the maintenance. Don't sell
        # everything for pennies there might not be any orders resting"
        # — the Ohio Senate dem short sold 16c under his fair on a bare
        # ask side that made 84 shares read $360 a day
        self.f.set_fair(NC, 62.0)
        bare = Book(bids=((0.44, 300.0), (0.43, 500.0), (0.02, 60000.0)),
                    asks=((0.46, 20.0), (0.47, 30.0), (0.98, 60000.0)),
                    tick=0.01, fetched_at=self.r.now)
        self.r.exchange.books[NC] = bare
        self.tick()
        self.tick()
        # nothing rests under his fair — and with the at-fair slot far
        # behind the touch earning nothing, nothing at all
        for o in self.mine(NC, "SELL"):
            self.assertGreaterEqual(o.price, 0.62 - 1e-9)
        # the plan itself: on the bare side nothing past fair is a candidate
        book = self.r.cache.any_age(NC)
        prog = self.f.terms.get(NC)
        pool = self.f.fam._side_pool(NC, prog)
        plan = self.f._entry_plan(NC, "SELL", book, prog, pool, 0.62, 200.0)
        self.assertIsNotNone(plan)
        self.assertGreaterEqual(plan["px"], 0.62 - 1e-9)
        self.assertEqual(plan["conc"], 0.0)
        # with company at the touch the same concession is on the table
        deep = Book(bids=((0.44, 300.0), (0.43, 500.0), (0.02, 60000.0)),
                    asks=((0.46, 4000.0), (0.47, 6000.0), (0.98, 60000.0)),
                    tick=0.01, fetched_at=self.r.now)
        self.assertFalse(self.f._bare(self.f._levels_net(NC, "SELL", deep), 0.01, 370.0))
        self.assertTrue(self.f._bare(self.f._levels_net(NC, "SELL", bare), 0.01, 370.0))
        plan = self.f._entry_plan(NC, "SELL", deep, prog, pool, 0.62, 200.0)
        self.assertIsNotNone(plan)
        s_bare = self.f._score(NC, "SELL", bare, prog, pool, 0.62, 0.46, 84.0,
                               self.f._levels_net(NC, "SELL", bare))
        # 16c past his fair; charged as an unwind midway between 62c and
        # the 46c ask: 8c a share (owner, 2026-09-12)
        self.assertGreater(s_bare["past"], 0.15)
        self.assertAlmostEqual(s_bare["conc"], 0.08, places=4)

    def test_one_resting_past_fair_on_a_bare_side_is_moved(self):
        self.f.set_fair(NC, 62.0)
        deep = Book(bids=((0.44, 300.0), (0.43, 500.0), (0.02, 60000.0)),
                    asks=((0.46, 4000.0), (0.47, 6000.0), (0.98, 60000.0)),
                    tick=0.01, fetched_at=self.r.now)
        self.r.exchange.books[NC] = deep
        self.tick()
        self.tick()
        # the tender's own ask is re-laid past fair by hand of the test:
        # the shape of an order rested with company that lost it
        asks = self.mine(NC, "SELL")
        self.assertTrue(asks)
        o = asks[0]
        o.price = 0.46
        self.r.exchange.live[o.id]["price"] = 0.46
        bare = Book(bids=((0.44, 300.0), (0.43, 500.0), (0.02, 60000.0)),
                    asks=((0.46, 20.0), (0.47, 30.0), (0.98, 60000.0)),
                    tick=0.01, fetched_at=self.r.now)
        self.r.exchange.books[NC] = bare
        self.r.now += focus_mod.FOCUS_MOVE_COOLDOWN_S
        self.tick()
        self.tick()
        for x in self.mine(NC, "SELL"):
            self.assertGreaterEqual(x.price, 0.62 - 1e-9)
        evs = [e["event"] for e in self.f.log if e.get("market") == NC and e.get("side") == "SELL"]
        self.assertTrue("moved" in evs or "pull" in evs, evs)


class TestTheBuyingPowerRead(Base):
    def test_the_view_carries_the_reads_age_and_flags_a_failing_read(self):
        # owner, 2026-09-11: "The buying power number is out of date"
        self.tick()
        v = self.f.view(self.r.now, self.f.buying_power(self.r.now), True)
        self.assertEqual(v["bp"], 2000.0)
        self.assertLessEqual(v["bp_age_s"], focus_mod.FOCUS_BP_EVERY_S)
        self.assertEqual(v["bp_note"], "")
        self.assertEqual(v["stake_bp"], 2000.0 + self.f.walls_held())
        self.assertAlmostEqual(v["stake"], 0.1 * v["stake_bp"], places=2)
        # the exchange stops answering: the last read stands, aged and flagged
        def down():
            raise RuntimeError("HTTP 503")
        self.f._bp_fn = down
        self.r.now += focus_mod.FOCUS_BP_EVERY_S + 1
        self.tick()
        self.r.now += 300.0
        self.tick()
        v = self.f.view(self.r.now, self.f.buying_power(self.r.now), True)
        self.assertEqual(v["bp"], 2000.0)
        self.assertGreater(v["bp_age_s"], 300)
        self.assertIn("reads failing since", v["bp_note"])
        self.assertIn("HTTP 503", v["bp_note"])
        # and it clears the pass a read comes back
        self.f._bp_fn = lambda: 1500.0
        self.r.now += focus_mod.FOCUS_BP_EVERY_S + 1
        self.tick()
        v = self.f.view(self.r.now, self.f.buying_power(self.r.now), True)
        self.assertEqual(v["bp"], 1500.0)
        self.assertEqual(v["bp_note"], "")
        self.assertLessEqual(v["bp_age_s"], focus_mod.FOCUS_BP_EVERY_S)
        # the stake still follows the half-hour high
        self.assertEqual(v["bp_high"], 2000.0)


class TestTheBareTestSizesItselfAsThePlanDoes(Base):
    def test_a_resting_entry_is_judged_by_the_ramped_room(self):
        # 15:08-15:20Z, 2026-09-11: a Texas governor dem bid rested at 21c
        # against a 15c fair and was pulled "no company" fifteen times in
        # twelve minutes — the plan sized its bare test by the ramped
        # room, the pull by the whole stake
        self.f.set_fair(NC, 40.0)
        self.f.set_stake(NC, 200.0)
        self.r.positions[NC] = (200.0, 80.0)          # long: a bid adds to it
        thin = Book(bids=((0.44, 100.0), (0.43, 50.0), (0.02, 60000.0)),
                    asks=((0.47, 300.0), (0.48, 500.0), (0.98, 60000.0)),
                    tick=0.01, fetched_at=self.r.now)
        self.r.exchange.books[NC] = thin
        self.r.cache.put(NC, Book(bids=thin.bids, asks=thin.asks, tick=0.01, fetched_at=self.r.now))
        self.f.filled_at[f"{NC}|BUY"] = self.r.now - 20 * 60.0   # past the standoff, ramping
        r = self.f.place(NC, "BUY", 44.0, 79.0)       # 4c past fair, sized to the ramped room
        self.assertTrue(r["ok"], r)
        o = self.r.fam.orders[r["order_id"]]
        room = self.f._entry_room(f"{NC}|BUY", "BUY", 200.0, 200.0, 80.0, self.r.now)
        self.assertLess(room, 200.0 - 80.0)
        self.assertGreater(room, 1.0)
        # the plans and the scores, without the tend: 150 shares of company
        # against a ramped room of ~80 shares is not bare
        self.f._plan_all(self.r.now, dict(self.r.positions), 2000.0)
        self.assertFalse(self.f._own_bare(o))
        # the plan on that side sizes by the same room
        plan = (self.f.rows[NC].get("tend") or {}).get("BUY") or {}
        self.assertTrue(plan.get("px") is None or plan["qty"] * 0.44 <= room + 1.0, plan)
        # with the ramp long over, the whole room applies: 150 against ~270 is bare
        self.f.filled_at.pop(f"{NC}|BUY", None)
        self.f._plan_all(self.r.now, dict(self.r.positions), 2000.0)
        self.assertTrue(self.f._own_bare(o))


class TestNoMoneyNoPlacement(Base):
    def test_an_entry_that_does_not_fit_the_buying_power_is_not_sent(self):
        # owner, 2026-09-11: the exchange app at $177 available while the
        # tender kept placing — 126 placements rejected in an hour
        self.f.set_fair(NC, 45.0)
        self.f.set_stake(NC, 300.0)               # ~$150 an order
        self.f._bp_fn = lambda: 50.0
        self.r.now += focus_mod.FOCUS_BP_EVERY_S + 1
        before = len(self.r.exchange.live)
        self.tick()
        self.tick()
        self.assertEqual(len(self.r.exchange.live), before)
        self.assertEqual(self.mine(NC), [])
        evs = [e for e in self.f.log if e.get("event") == "no_money"]
        self.assertTrue(evs)
        self.assertIn("$50 of buying power free", evs[0]["why"])
        v = self.f.view(self.r.now, 50.0, True)
        self.assertGreaterEqual(v["waiting_money"], 1)
        # noted once a cooldown, not every pass
        self.tick()
        self.tick()
        self.assertEqual(len([e for e in self.f.log if e.get("event") == "no_money"]), len(evs))
        # money comes back: the entries rest
        self.f._bp_fn = lambda: 2000.0
        self.r.now += focus_mod.FOCUS_BP_EVERY_S + 1
        self.tick()
        self.tick()
        self.assertTrue(self.mine(NC))
        v = self.f.view(self.r.now, 2000.0, True)
        self.assertEqual(v["waiting_money"], 0)

    def test_an_exit_is_placed_with_no_money_free(self):
        self.f.set_fair(NC, 45.0)
        self.r.positions[NC] = (120.0, 48.0)
        self.f._bp_fn = lambda: 0.0
        self.r.now += focus_mod.FOCUS_BP_EVERY_S + 1
        self.tick()
        self.tick()
        asks = self.mine(NC, "SELL")
        self.assertTrue(asks)
        self.assertEqual(self.mine(NC, "BUY"), [])


class TestHisTapNeverWaitsOnAPass(Base):
    def test_a_placement_by_hand_reaches_the_exchange_while_the_pass_holds_the_lock(self):
        # owner, 2026-09-11: "No answer from the server in time" on a tap
        # — the pass was placing and verifying orders of its own for a
        # minute, and his placement waited behind it
        import threading
        self.tick()
        before = set(self.r.exchange.live)
        out = {}
        self.f.lock.acquire()                     # the pass, mid-placement
        try:
            t = threading.Thread(target=lambda: out.update(self.f.place(NC, "BUY", 40.0, 25.0)))
            t.start()
            deadline = time.time() + 5.0
            while time.time() < deadline and set(self.r.exchange.live) == before:
                time.sleep(0.05)
            new = set(self.r.exchange.live) - before
            self.assertTrue(new, "the tap's order never reached the exchange while the lock was held")
            self.assertTrue(t.is_alive() or out)  # the record waits for the lock, the order does not
        finally:
            self.f.lock.release()
        t.join(5.0)
        self.assertTrue(out.get("ok"), out)
        self.assertIn(out["order_id"], self.r.fam.orders)
        self.assertEqual(self.r.fam.orders[out["order_id"]].purpose, "manual")

    def test_refreeze_does_not_wait_out_a_pass(self):
        import threading
        self.tick()
        held = threading.Event()
        done = threading.Event()

        def pass_holding_the_lock():
            with self.f.lock:
                held.set()
                done.wait(8.0)
        t = threading.Thread(target=pass_holding_the_lock)
        t.start()
        held.wait(2.0)
        try:
            t0 = time.time()
            self.f.refreeze()                     # gives up rather than waiting out the pass
            self.assertLess(time.time() - t0, 4.5)
        finally:
            done.set()
            t.join(5.0)


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
        srv.handle_op({"op": "focus_qualify", "market": NC, "value": "ask"})
        self.assertEqual(calls[0], ("focus_fair", NC, 45))
        self.assertEqual(calls[1][2]["order_id"], "o1")
        self.assertEqual(calls[2], ("focus_qualify", NC, "ask"))

    def test_the_websocket_seats_the_focus_first_and_caps_the_engine(self):
        self.assertEqual(focus_mod.FOCUS_WS_CAP + focus_mod.ENGINE_WS_CAP, 200)


if __name__ == "__main__":
    unittest.main()
