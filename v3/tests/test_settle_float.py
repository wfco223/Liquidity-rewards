"""Owner, 2026-09-09. The settle check: 30 seconds after a new order
rests, read the book fresh and run the plan again with the order in
place — pull it, move it, or keep it, and let the settled number
replace the claim. The exit float: an exit that has earned nothing
steps toward the touch a tick at a time on a daily concession budget,
and what closing really cost feeds the fill model."""
import unittest

from v3.family import FamilyConfig, FamilyOrder
from v3.scoring import Book
from v3.tests.test_family import Rig, A, B, politics_book


def cfg(**kw) -> FamilyConfig:
    base = dict(name="Politics", tag="POL", known_ground=True,
                rest_style="join_quiet", revive=True,
                capital_usd=100.0, per_market_usd=2.0, revive_max_usd=5.0,
                min_days_out=3)
    base.update(kw)
    return FamilyConfig(**base)


class TestTheSettleCheck(unittest.TestCase):
    def _armed(self, **kw):
        r = Rig(cfg=cfg(settle_s=30.0, enter_per_cycle=2, **kw))
        r.add_market(A)
        r.cycle()
        earn = [o for o in r.fam.orders.values() if o.purpose == "earn"]
        self.assertTrue(earn)
        self.assertTrue(all(not o.settled for o in earn))
        return r, earn

    def test_a_wall_landing_on_our_price_pulls_the_order(self):
        r, earn = self._armed()
        bid = [o for o in earn if o.side == "BUY"][0]
        # someone drops 50,000 shares on our price and the ask side too:
        # our share of both sides is now nothing
        r.exchange.books[A] = Book(bids=((bid.price, 50000.0), (0.02, 60000.0)),
                                   asks=((0.47, 50000.0), (0.98, 60000.0)),
                                   tick=0.01, fetched_at=r.now)
        r.cycle(advance=31.0)
        pulled = [e for e in r.fam.log if e.get("event") == "settle_pulled"]
        self.assertTrue(pulled, [e.get("event") for e in r.fam.log[-8:]])
        self.assertNotIn(bid.id, r.fam.orders)

    def test_an_undisturbed_order_is_kept_and_its_claim_settled(self):
        r, earn = self._armed()
        before = {o.id: o.est_day for o in earn}
        r.cycle(advance=31.0)
        kept = [e for e in r.fam.log if e.get("event") == "settle_kept"]
        self.assertTrue(kept, [e.get("event") for e in r.fam.log[-8:]])
        for oid in before:
            if oid in r.fam.orders:
                self.assertTrue(r.fam.orders[oid].settled)
        self.assertFalse(any(e.get("event") == "settle_pulled" for e in r.fam.log))

    def test_nothing_is_checked_before_thirty_seconds(self):
        r, earn = self._armed()
        r.cycle(advance=10.0)
        self.assertFalse(any(e.get("event", "").startswith("settle_") for e in r.fam.log))
        self.assertTrue(all(not r.fam.orders[o.id].settled for o in earn if o.id in r.fam.orders))

    def test_two_new_orders_a_cycle(self):
        r = Rig(cfg=cfg(settle_s=30.0, enter_per_cycle=2))
        for slug in (A, B, "vmc-ussemov-ga-2026-11-03-d0-3", "vmc-ussemov-ga-2026-11-03-r4-7"):
            r.add_market(slug)
        r.cycle()
        placed = [e for e in r.fam.log if e.get("event") == "place"]
        self.assertEqual(len(placed), 2, placed)

    def test_off_by_default_orders_rest_settled(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        self.assertTrue(all(o.settled for o in r.fam.orders.values()))
        self.assertFalse(any(e.get("event", "").startswith("settle_") for e in r.fam.log))


class TestTheExitFloat(unittest.TestCase):
    """A long bought at 60c on a book now trading 40c/42c: the exit
    rests at break-even, earns nothing, and may step down."""

    def _rig(self, **kw):
        opts = dict(exit_float=True, exit_float_idle_s=6 * 3600.0,
                    exit_float_step_s=1800.0, exit_float_usd_day=5.0,
                    # the gate's give-up budget is spent (as it is in
                    # production when exits strand): no door past
                    # break-even but the float
                    exit_giveup_cap_usd=0.0, exit_small_giveup_usd=0.0)
        opts.update(kw)
        r = Rig(cfg=cfg(**opts))
        # a stranded exit is one whose floor IS break-even: no model and
        # no evidence band that would authorise a loss-cut (the 1,132
        # idle politics shares of 2026-09-09 sat exactly there)
        r.fam._exit_floor = lambda slug, side, basis, tick, book=None, qty=None: (
            (basis + tick, basis) if side == "SELL" else (basis - tick, basis))
        book = Book(bids=((0.40, 50.0), (0.02, 60000.0)),
                    asks=((0.42, 50.0), (0.98, 60000.0)),
                    tick=0.01, fetched_at=r.now)
        r.add_market(A, book=book)
        r.fam.inventory[A] = {"qty": 10.0, "cost": 6.0}
        r.positions[A] = (10.0, 6.0)
        r.cycle()
        return r

    def _exits(self, r):
        return [o for o in r.fam.orders.values()
                if o.market == A and o.purpose == "sell" and o.side == "SELL"]

    def _go_dry(self, r, hours=7.0):
        for o in self._exits(r):
            o.live_est = 0.0
            o.dry_since = r.now - hours * 3600.0

    def test_the_exit_rests_at_break_even_and_holds_while_fresh(self):
        r = self._rig()
        ex = self._exits(r)
        self.assertTrue(ex)
        self.assertAlmostEqual(min(o.price for o in ex), 0.61, places=3)
        self._go_dry(r, hours=1.0)                 # dry, but not for six hours
        r.cycle()
        self.assertFalse(any(e.get("event") == "exit_floated" for e in r.fam.log))

    def test_six_idle_hours_step_it_down_a_tick_on_the_budget(self):
        r = self._rig()
        self._go_dry(r)
        r.cycle()                                  # the step, and the cancel
        fl = [e for e in r.fam.log if e.get("event") == "exit_floated"]
        self.assertTrue(fl, [e.get("event") for e in r.fam.log[-8:]])
        self.assertAlmostEqual(fl[-1]["to"], 0.60, places=3)
        self.assertAlmostEqual(r.fam.float_day["usd"], 0.10, places=3)   # 1c x 10 shares
        self._go_dry(r)
        r.cycle()                                  # re-rested at the float level
        ex = self._exits(r)
        self.assertTrue(ex)
        self.assertAlmostEqual(min(o.price for o in ex), 0.60, places=3)
        # the next step waits half an hour
        self._go_dry(r); r.cycle(advance=60.0)
        self.assertEqual(len([e for e in r.fam.log if e.get("event") == "exit_floated"]), 1)
        self._go_dry(r); r.cycle(advance=1800.0)
        fl = [e for e in r.fam.log if e.get("event") == "exit_floated"]
        self.assertEqual(len(fl), 2)
        self.assertAlmostEqual(fl[-1]["to"], 0.59, places=3)

    def test_an_earning_exit_holds_where_a_tick_closer_is_worth_no_more(self):
        r = self._rig()
        self._go_dry(r); r.cycle()
        self.assertEqual(len([e for e in r.fam.log if e.get("event") == "exit_floated"]), 1)
        r.fam._float_wants_more = lambda *a, **k: False
        for o in self._exits(r):
            o.live_est = 0.40                      # it earns now
        r.cycle(advance=1800.0)
        self.assertEqual(len([e for e in r.fam.log if e.get("event") == "exit_floated"]), 1)

    def test_an_earning_exit_keeps_stepping_while_a_tick_closer_is_worth_more(self):
        # owner, 2026-09-09: "not minimally earning. Just optimally
        # earning, where there is no marginal benefit to moving up any
        # further all things considered"
        r = self._rig()
        self._go_dry(r); r.cycle()                 # step one, cancel
        self._go_dry(r); r.cycle()                 # re-rested at 60c
        r.fam._float_wants_more = lambda *a, **k: True
        for o in self._exits(r):
            o.live_est = 0.40                      # earning, and a tick closer is worth more
        r.cycle(advance=1800.0)
        fl = [e for e in r.fam.log if e.get("event") == "exit_floated"]
        self.assertEqual(len(fl), 2, fl)
        self.assertAlmostEqual(fl[-1]["to"], 0.59, places=3)

    def test_wants_more_weighs_earnings_against_the_loss_on_a_fill(self):
        r = self._rig()
        book = r.exchange.books[A]
        # one tick closer to the 42c ask touch, from 44c to 43c, on a
        # 60c basis: a fat pool makes the extra share worth the extra
        # give-up; a thin one does not
        r.fam._side_pool = lambda slug, prog: 100.0
        self.assertTrue(r.fam._float_wants_more(A, "SELL", book, 0.44, 0.43, 10.0, 0.60))
        r.fam._side_pool = lambda slug, prog: 0.05
        self.assertFalse(r.fam._float_wants_more(A, "SELL", book, 0.44, 0.43, 10.0, 0.60))

    def test_no_cancel_churn_once_the_exit_sits_at_the_float_level(self):
        # 2026-09-09 04:00Z: three exits were cancelled and re-rested
        # twenty times each — the cancel fired on the level, the re-rest
        # landed elsewhere. A cancel now needs a re-rest that lands
        # somewhere better.
        r = self._rig()
        self._go_dry(r); r.cycle()                 # step + cancel
        self._go_dry(r); r.cycle()                 # re-rested at the level
        for _ in range(5):
            self._go_dry(r); r.cycle(advance=60.0)
        moves = [e for e in r.fam.log if e.get("event") == "exit_float_move"]
        self.assertEqual(len(moves), 1, moves)
        ex = self._exits(r)
        self.assertEqual(len(ex), 1, ex)
        self.assertAlmostEqual(ex[0].price, 0.60, places=3)

    def test_a_qualifying_wall_is_not_an_exit_to_float(self):
        from v3.family import FamilyOrder, QUALIFY_WALL_WHY
        r = self._rig()
        # the wall rests on the exchange (adopted as an exit, as the
        # 2026-09-08 Nebraska bids were)
        w = r.desk.place_resting(A, "SELL", 0.99, 500.0, net_position=10.0,
                                 initiator="owner")
        self.assertTrue(w.ok, w.note)
        wall = FamilyOrder(id=w.order_id, market=A, side="SELL", price=0.99, qty=500.0,
                           intent=w.intent or "ORDER_INTENT_SELL_LONG", placed_ts=r.now,
                           purpose="sell", why=QUALIFY_WALL_WHY, live_est=0.0,
                           dry_since=r.now - 8 * 3600.0)
        r.fam.orders[wall.id] = wall
        self._go_dry(r); r.cycle()
        self._go_dry(r); r.cycle()
        self.assertIn(wall.id, r.fam.orders)      # never cancelled
        self.assertAlmostEqual(r.fam.orders[wall.id].price, 0.99)
        # and its 500 shares never counted in the concession
        self.assertLess(r.fam.float_day["usd"], 0.5)

    def test_the_day_budget_stops_the_float(self):
        r = self._rig(exit_float_usd_day=0.15)     # one step of 10c fits, two do not
        self._go_dry(r); r.cycle()
        self._go_dry(r); r.cycle(advance=1800.0)
        self._go_dry(r); r.cycle(advance=1800.0)
        fl = [e for e in r.fam.log if e.get("event") == "exit_floated"]
        self.assertEqual(len(fl), 1, fl)

    def test_the_float_is_remembered_across_a_restart(self):
        from v3.family import Family
        from v3 import politics
        r = self._rig()
        self._go_dry(r); r.cycle()
        d = r.fam.to_dict()
        f2 = Family(None, r.cache, politics.discover, config=r.fam.cfg,
                    names=r.names, clock=lambda: r.now)
        f2.restore(d)
        self.assertEqual(f2.exit_float, r.fam.exit_float)
        self.assertEqual(f2.float_day, r.fam.float_day)

    def test_off_by_default(self):
        r = Rig()
        book = Book(bids=((0.40, 50.0), (0.02, 60000.0)),
                    asks=((0.42, 50.0), (0.98, 60000.0)), tick=0.01, fetched_at=r.now)
        r.add_market(A, book=book)
        r.fam.inventory[A] = {"qty": 10.0, "cost": 6.0}
        r.positions[A] = (10.0, 6.0)
        r.cycle()
        for o in r.fam.orders.values():
            o.live_est = 0.0; o.dry_since = r.now - 8 * 3600.0
        r.cycle()
        self.assertFalse(any(e.get("event") == "exit_floated" for e in r.fam.log))


class TestClosingCostFeedsTheFillModel(unittest.TestCase):
    def test_a_sale_under_basis_raises_the_trip_cost_and_the_fill_cost(self):
        r = Rig()
        fm = r.fam.fillmodel
        before = fm.fill_cost(A, "BUY", 0.44, None)
        r.fam.inventory[A] = {"qty": 10.0, "cost": 6.0}       # basis 60c
        rec = FamilyOrder(id="x1", market=A, side="SELL", price=0.50, qty=10.0,
                          intent="ORDER_INTENT_SELL_LONG", placed_ts=r.now, purpose="sell")
        r.fam.orders[rec.id] = rec
        r.fam._on_fill(rec, 10.0, r.now)
        from v3.fillmodel import family_of
        self.assertGreater(fm.trip_cost[family_of(A)], 0.0)      # 10c lost a share, weighted in
        self.assertEqual(fm.trip_n[family_of(A)], 1)
        self.assertGreater(fm.fill_cost(A, "BUY", 0.44, None), before)
        self.assertNotIn(A, r.fam.inventory)                      # flat: float memory cleared
        self.assertEqual(r.fam.exit_float, {})

    def test_a_sale_at_or_above_basis_costs_nothing(self):
        r = Rig()
        fm = r.fam.fillmodel
        r.fam.inventory[A] = {"qty": 10.0, "cost": 6.0}
        rec = FamilyOrder(id="x2", market=A, side="SELL", price=0.62, qty=10.0,
                          intent="ORDER_INTENT_SELL_LONG", placed_ts=r.now, purpose="sell")
        r.fam._on_fill(rec, 10.0, r.now)
        from v3.fillmodel import family_of
        self.assertEqual(fm.trip_cost.get(family_of(A), 0.0), 0.0)

    def test_the_trip_cost_survives_a_save(self):
        from v3.fillmodel import FillModel
        fm = FillModel()
        fm.observe_round_trip(A, 0.05, 20.0)
        fm2 = FillModel.from_dict(fm.to_dict())
        self.assertEqual(fm2.trip_cost, fm.trip_cost)
        self.assertEqual(fm2.trip_n, fm.trip_n)
        self.assertIsNotNone(fm2.trip_summary()["cents"])


class TestABrokenBasisTeachesNothing(unittest.TestCase):
    """Owner, 2026-09-12 "Yes fix that". The exchange reports a
    position's cost as the money tied up, POSITIVE for a short as well
    as a long (Massachusetts governor rep read as -209 shares at
    +$196.72). Stored as given, a short's basis came out negative and a
    cover recorded its price PLUS that basis — about a dollar a share on
    a contract that settles between 0 and 1. The fill model had learned
    54c a share on the governor books and 238c on the house seats, and
    it rejected 59 of 71 sides on those numbers."""

    def test_the_feed_cost_takes_the_sign_of_the_position(self):
        from v3.family import Family
        self.assertEqual(Family._feed_cost(-209.0, 196.72), -196.72)
        self.assertEqual(Family._feed_cost(209.0, 196.72), 196.72)
        self.assertEqual(Family._feed_cost(-209.0, -196.72), -196.72)

    def test_a_seeded_short_gets_a_basis_that_is_a_price(self):
        r = Rig()
        r.add_market(A)
        r.fam.universe.setdefault(A, {})
        r.fam._seed_inventory({A: (-209.0, 196.72)})
        inv = r.fam.inventory[A]
        basis = inv["cost"] / inv["qty"]
        self.assertAlmostEqual(basis, 0.9413, places=3)
        self.assertTrue(0.0 < basis < 1.0)

    def test_a_cover_against_a_broken_basis_is_not_learned(self):
        from v3.fillmodel import family_of
        r = Rig()
        fm = r.fam.fillmodel
        # the state as it stood: a short with a positive cost
        r.fam.inventory[A] = {"qty": -209.0, "cost": 196.72}
        rec = FamilyOrder(id="b1", market=A, side="BUY", price=0.07, qty=209.0,
                          intent="ORDER_INTENT_SELL_SHORT", placed_ts=r.now, purpose="sell")
        r.fam._on_fill(rec, 209.0, r.now)
        self.assertEqual(fm.trip_cost.get(family_of(A), 0.0), 0.0)
        self.assertEqual(fm.trip_n.get(family_of(A), 0), 0)

    def test_a_cost_per_share_off_the_contract_is_refused(self):
        from v3.fillmodel import FillModel, family_of
        fm = FillModel()
        fm.observe_round_trip(A, 2.38, 100.0)       # $2.38 a share: impossible
        fm.observe_round_trip(A, -0.5, 100.0)
        self.assertEqual(fm.trip_cost.get(family_of(A), 0.0), 0.0)
        self.assertEqual(fm.trip_dropped, 2)
        fm.observe_round_trip(A, 0.03, 100.0)       # 3c a share: a measurement
        self.assertGreater(fm.trip_cost[family_of(A)], 0.0)

    def test_no_single_close_replaces_the_pools_number(self):
        from v3.fillmodel import FillModel, TRIP_W_MAX, family_of
        fm = FillModel()
        for _ in range(4):
            fm.observe_round_trip(A, 0.01, 1000.0)   # small, real losses
        settled = fm.trip_cost[family_of(A)]
        fm.observe_round_trip(A, 0.90, 5000.0)       # one huge close
        after = fm.trip_cost[family_of(A)]
        self.assertLess(after, settled + 0.90 * TRIP_W_MAX + 1e-6)
        self.assertLess(after, 0.30)                 # it cannot own the number

    def test_the_stored_numbers_are_dropped_once_and_relearned(self):
        from v3.fillmodel import FillModel, TRIP_REPAIR
        old = {"trip_cost": {"governor": 0.5413, "house-seats": 2.3805},
               "trip_n": {"governor": 49, "house-seats": 6}}
        fm = FillModel.from_dict(old)
        self.assertEqual(fm.trip_cost, {})           # learned through the bug: dropped
        self.assertEqual(fm.trip_repair, TRIP_REPAIR)
        fm.observe_round_trip(A, 0.02, 100.0)
        again = FillModel.from_dict(fm.to_dict())
        self.assertEqual(again.trip_cost, fm.trip_cost)   # and kept from now on

    def test_a_restore_repairs_the_rows_already_on_the_books(self):
        r = Rig()
        r.fam.restore({"inventory": {A: {"qty": -209.0, "cost": 196.72},
                                     "b": {"qty": 100.0, "cost": 44.0}}})
        self.assertEqual(r.fam.inventory[A]["cost"], -196.72)
        self.assertEqual(r.fam.inventory["b"]["cost"], 44.0)
        self.assertTrue(any(e.get("event") == "basis_signs_fixed" for e in r.fam.log))


class TestTheLiveConfigs(unittest.TestCase):
    def test_politics_and_football_settle_float_and_pace(self):
        from v3 import politics, football
        for c, per in ((politics.config(), 2), (football.cfb(), 2), (football.nfl(), 6)):
            self.assertEqual(c.settle_s, 30.0)
            self.assertEqual(c.enter_per_cycle, per)
            self.assertTrue(c.exit_float)
        self.assertEqual(politics.config().exit_float_usd_day, 15.0)
        self.assertEqual(football.cfb().exit_float_usd_day, 5.0)
        self.assertEqual(football.nfl().exit_float_usd_day, 2.0)

    def test_the_status_card_shows_the_float_and_the_settling(self):
        from v3 import web
        self.assertIn("floatLine(s2.exit_float,s2.settling)", web.STATUS_JS)
        self.assertIn("closing a filled position has cost", web.STATUS_JS)


if __name__ == "__main__":
    unittest.main()
