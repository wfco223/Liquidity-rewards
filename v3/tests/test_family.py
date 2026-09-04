"""The 3.0 engine, end to end against a fake exchange: discovery with the
divisor built in, observing vs armed, resting styles, reviving, the one
risk number, leaving dead markets entirely, fills -> exits, the accrual."""

import unittest

from v3 import politics
from v3.books import BookCache
from v3.family import Family, FamilyConfig, resting_ok
from v3.intents import REST_SIDE
from v3.names import Names
from v3.orders import OrderDesk
from v3.scoring import Book

A = "vmc-ussemov-ga-2026-11-03-d4-7"
B = "vmc-ussemov-ga-2026-11-03-r0-3"
C = "paccc-usho-midterms-2026-11-03-rep"


class FakeClient:
    """Plays the exchange: placements rest, cancels remove, plus the read
    APIs the family calls."""

    def __init__(self):
        self.next_id = 1
        self.live: dict[str, dict] = {}
        self.books: dict[str, Book] = {}
        self.prog_raw: dict[str, dict] = {}
        self.events: list[dict] = []
        self.programs_fail = False
        self.trades: list[dict] = []     # the exchange's own trade record

    # -- desk side ----------------------------------------------------------
    def post(self, url, body, path=None, **kw):
        if url.endswith("/v1/orders"):
            oid = f"o{self.next_id}"
            self.next_id += 1
            self.live[oid] = {
                "id": oid, "market": body["marketSlug"],
                "side": REST_SIDE[body["intent"]],
                "price": float(body["price"]["value"]),
                "size": float(body["quantity"]), "intent": body["intent"],
            }
            if body.get("participateDontInitiate") is False:
                # a taker order fills: the trade record shows it
                q = float(body["quantity"])
                px = float(body["price"]["value"])
                self.trades.append({"type": "ACTIVITY_TYPE_TRADE", "trade": {
                    "id": f"t{oid}",
                    "aggressorExecution": {
                        "id": f"x{oid}",
                        "order": {"id": oid, "intent": body["intent"],
                                  "quantity": q, "cumQuantity": q,
                                  "avgPx": {"value": f"{px:.4f}"}},
                        "lastShares": f"{q:.4f}",
                        "lastPx": {"value": f"{px:.4f}"}}}})
            return {"order": {"id": oid}}
        if "/cancel" in url:
            self.live.pop(url.rstrip("/cancel").rsplit("/", 1)[-1], None)
            return {}
        return {}

    def open_orders(self):
        return [dict(o) for o in self.live.values()]

    def recent_trades(self, limit=25):
        return list(self.trades)[-limit:]

    def activities(self, types=None, pages=10, page_size=100):
        """The transaction record: every trade, newest first like the
        exchange's."""
        return list(reversed(self.trades))

    # -- read side ----------------------------------------------------------
    def book(self, slug, fetched_at=None):
        b = self.books[slug]
        return Book(bids=b.bids, asks=b.asks, tick=b.tick,
                    fetched_at=fetched_at or b.fetched_at)

    def programs(self, slugs):
        if self.programs_fail:
            raise RuntimeError("incentives down")
        return {s: dict(self.prog_raw[s]) for s in slugs if s in self.prog_raw}

    def events_by_tag(self, tag, max_pages=30):
        return list(self.events) if tag == "politics" else []


def politics_book(now, bid=0.44, ask=0.47, bid_q=20.0, ask_q=20.0):
    # the politics shape: a thin touch, the qualifying wall far behind
    return Book(bids=((bid, bid_q), (0.02, 60000.0)),
                asks=((ask, ask_q), (0.98, 60000.0)),
                tick=0.01, fetched_at=now)


LIVE_PROG = {"timePeriods": [{"programId": "politics_mid_1", "rewardPool": 100.0,
                              "targetSize": 5000, "discountFactor": 0.2,
                              "status": "LIVE"}]}
DEAD_PROG = {"timePeriods": [{"programId": "politics_mid_1", "rewardPool": 0,
                              "targetSize": 5000, "discountFactor": 0.2,
                              "status": "LIVE"}]}


class Rig:
    def __init__(self, cfg=None, switch=True):
        self.now = 1_000_000.0
        self.exchange = FakeClient()
        self.cache = BookCache()
        self.switch = switch
        self.alerts = []
        self.names = Names()
        cfg = cfg or FamilyConfig(
            name="Politics", tag="POL", known_ground=True,
            rest_style="join_quiet", revive=True,
            capital_usd=100.0, per_market_usd=2.0, revive_max_usd=5.0,
            min_days_out=3)
        self.fam = Family(None, self.cache, politics.discover, config=cfg,
                          alert=lambda t, m: self.alerts.append((t, m)),
                          names=self.names, clock=lambda: self.now)
        self.desk = OrderDesk(
            client=self.exchange,
            whitelist=self.fam.knows,
            switch_on=lambda: self.switch,
            fresh_book=lambda s: self.cache.fresh(s, 120, self.now),
            own_at=lambda slug, side, px: sum(
                o.qty for o in self.fam.orders.values()
                if o.market == slug and o.side == side
                and abs(o.price - px) < 1e-9),
            log=lambda e: None,
            sleep=lambda s: None, clock=lambda: self.now,
        )
        self.fam.desk = self.desk
        self.positions: dict[str, tuple] = {}

    def add_market(self, slug, book=None, event="Georgia Senate margin",
                   siblings=None, prog=LIVE_PROG):
        rows = [{"slug": s, "question": f"Q for {s}"}
                for s in ([slug] + list(siblings or []))]
        self.exchange.events.append({"title": event, "markets": rows})
        self.exchange.books[slug] = book or politics_book(self.now)
        import copy
        self.exchange.prog_raw[slug] = copy.deepcopy(prog)

    def cycle(self, advance=60.0):
        self.now += advance
        return self.fam.cycle(self.now, self.exchange.open_orders(),
                              self.positions, self.exchange, self.switch)


class TestDiscovery(unittest.TestCase):
    def test_universe_carries_divisor_and_names(self):
        r = Rig()
        r.add_market(A, siblings=[B])
        r.cycle()
        self.assertEqual(r.fam.universe[A]["event_n"], 2)
        self.assertTrue(r.names.label(A).startswith("Q for"))

    def test_econ_is_refused_at_the_door(self):
        r = Rig()
        r.exchange.events.append({"title": "CPI", "markets": [
            {"slug": "usacpi-2026-09-0", "question": "CPI above 3%?"}]})
        r.add_market(A)
        r.cycle()
        self.assertNotIn("usacpi-2026-09-0", r.fam.universe)

    def test_race_grouping_raises_the_divisor(self):
        # two single-market events whose slugs share one race prefix
        n1 = "enwc-uspres-nom-dem-2028-petbut"
        n2 = "enwc-uspres-nom-dem-2028-gavnew"
        r = Rig()
        r.add_market(n1, event="Dem nominee — Pete")
        r.add_market(n2, event="Dem nominee — Gavin")
        r.cycle()
        self.assertEqual(r.fam.universe[n1]["event_n"], 2)


class TestModes(unittest.TestCase):
    def test_observing_scores_but_never_places(self):
        r = Rig(switch=False)
        r.add_market(A)
        s = r.cycle()
        self.assertEqual(s["mode"], "observing")
        self.assertTrue(s["best_idle"])          # it found the opportunity
        self.assertEqual(r.exchange.live, {})    # and touched nothing

    def test_armed_places_on_both_sides_without_crossing(self):
        r = Rig()
        r.add_market(A)
        s = r.cycle()
        self.assertEqual(len(r.exchange.live), 2)  # both sides
        bids = [o.price for o in r.fam.orders.values() if o.side == "BUY"]
        asks = [o.price for o in r.fam.orders.values() if o.side == "SELL"]
        self.assertTrue(bids and asks)
        # in front is allowed now (owner, 2026-08-21) — but our own two
        # quotes must never cross
        self.assertLess(max(bids), min(asks) - 0.009)
        for o in r.fam.orders.values():
            self.assertTrue(o.why)
        self.assertLessEqual(s["spent"], r.fam.cfg.capital_usd)

    def test_bids_stay_inside_the_opposing_touch(self):
        # no quiet-proof needed and no share cap — the one hard bound
        # left is post-only mechanics: a tick inside the other side
        r = Rig()
        r.add_market(A)
        r.cycle()
        prices = {o.price for o in r.fam.orders.values() if o.side == "BUY"}
        self.assertTrue(prices)
        self.assertTrue(all(p <= 0.46 + 1e-9 for p in prices))


    def test_busy_book_may_join_when_ev_clears(self):
        # Owner, 2026-08-21: every level is an option, no hard rules —
        # a moving touch no longer forbids joining it; fill odds and
        # the queue ahead carry the caution instead.
        r = Rig()
        r.add_market(A)
        for i in range(6):                       # touch moves every sighting
            r.cache.put(A, politics_book(r.now, bid=0.40 + i * 0.01))
        r.exchange.books[A] = politics_book(r.now, bid=0.45)
        r.cycle()
        bids = [o for o in r.fam.orders.values() if o.side == "BUY"]
        self.assertTrue(bids)
        for o in bids:
            self.assertLessEqual(o.price, 0.46)  # a tick inside the ask


class TestRevive(unittest.TestCase):
    def bare_book(self, now):
        # bid side holds 10 of a 60 Target Size: pays NOBODY
        return Book(bids=((0.03, 10.0),), asks=((0.97, 50.0),),
                    tick=0.01, fetched_at=now)

    def prog(self, target=60):
        return {"timePeriods": [{"programId": "politics_mid_1",
                                 "rewardPool": 10.0, "targetSize": target,
                                 "discountFactor": 0.2, "status": "LIVE"}]}

    def test_known_ground_revives_a_dead_side(self):
        r = Rig()
        r.add_market(A, book=self.bare_book(r.now), prog=self.prog())
        r.cycle()
        # BOTH thin sides get revived — each side's target is its own
        revs = [o for o in r.fam.orders.values() if o.purpose == "revive"]
        self.assertEqual(len(revs), 2)
        bid = next(o for o in revs if o.side == "BUY")
        self.assertGreaterEqual(bid.qty, 50.0)       # fills the bid gap
        self.assertIn("revives", bid.why)

    def test_new_ground_never_revives(self):
        cfg = FamilyConfig(name="X", known_ground=False, revive=False,
                           capital_usd=100.0)
        r = Rig(cfg=cfg)
        r.add_market(A, book=self.bare_book(r.now), prog=self.prog())
        r.cycle()
        self.assertEqual([o for o in r.fam.orders.values()
                          if o.purpose == "revive"], [])

    def test_revive_respects_its_own_cap(self):
        r = Rig()
        # gap of ~5000 at 3c = $150 collateral >> revive_max_usd
        r.add_market(A, book=Book(bids=((0.03, 10.0),), asks=((0.97, 50.0),),
                                  tick=0.01, fetched_at=r.now),
                     prog=self.prog(target=5000))
        r.cycle()
        self.assertEqual([o for o in r.fam.orders.values()
                          if o.purpose == "revive"], [])


class TestDeadMarkets(unittest.TestCase):
    def test_program_gone_leaves_entirely_exits_included(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        self.assertTrue(r.exchange.live)
        # hand the family some stock so a sell exit rests too
        r.fam.inventory[A] = {"qty": 10.0, "cost": 4.0}
        r.positions[A] = (10.0, 4.0)
        r.fam.positions_seen[A] = 10.0
        r.cycle(advance=r.fam.cfg.cooldown_s + 1)
        self.assertTrue(any(o.purpose == "sell" for o in r.fam.orders.values()))
        # the pool dies
        r.exchange.prog_raw[A] = dict(DEAD_PROG)
        r.cycle(advance=r.fam.cfg.terms_active_s + 1)
        self.assertEqual(r.exchange.live, {})        # every order pulled
        self.assertEqual(r.fam.orders, {})
        # and the seller does not come back while it stays dead
        r.cycle(advance=r.fam.cfg.cooldown_s + 1)
        self.assertEqual(r.exchange.live, {})

    def test_absent_from_incentives_reads_as_gone(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        self.assertTrue(r.exchange.live)
        del r.exchange.prog_raw[A]                   # not in the response at all
        r.cycle(advance=r.fam.cfg.terms_active_s + 1)
        self.assertEqual(r.exchange.live, {})

    def test_failed_terms_fetch_changes_nothing(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        n = len(r.exchange.live)
        r.exchange.programs_fail = True
        r.cycle(advance=r.fam.cfg.terms_active_s + 1)
        self.assertEqual(len(r.exchange.live), n)    # no data, no verdict


class TestMoney(unittest.TestCase):
    def test_the_one_risk_number_binds(self):
        cfg = FamilyConfig(name="P", known_ground=True, rest_style="join_quiet",
                           revive=True, capital_usd=0.5, per_market_usd=2.0)
        r = Rig(cfg=cfg)
        r.add_market(A)
        r.add_market(C, event="House control")
        r.cycle()
        self.assertLessEqual(r.fam.family_spent(), 0.5 + 1e-9)

    def test_no_estimate_without_the_divisor(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        # forget the divisor: the market fell out of discovery but the
        # order still rests
        r.fam.universe[A] = {}
        r.fam._read_live(r.now)
        rec = next(iter(r.fam.orders.values()))
        self.assertIsNone(rec.live_est)
        self.assertIn("holding the estimate", rec.verdict)

    def test_fill_becomes_stock_and_an_exit_that_earns(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        bid = next(o for o in r.fam.orders.values() if o.side == "BUY")
        # the bid fills entirely
        del r.exchange.live[bid.id]
        r.positions[A] = (bid.qty, bid.qty * bid.price)
        s = r.cycle(advance=r.fam.cfg.cooldown_s + 1)
        self.assertIn(A, r.fam.inventory)
        # owner, 2026-08-24: an ordinary open no longer pages — the
        # verdict waits 20s for the book to settle and then only fires
        # on a >$1 mark-to-market loss with nothing earning.
        self.assertEqual([t for t, _ in r.alerts if "filled" in t], [])
        self.assertTrue(r.fam.pending_pages)
        sells = [o for o in r.fam.orders.values() if o.purpose == "sell"]
        # the seller's exit, plus the market's old earn-ask which the
        # reclassifier now (correctly) counts as an exit while stock is held
        seller_asks = [o for o in sells if "selling filled stock" in o.why]
        self.assertEqual(len(seller_asks), 1)
        self.assertGreaterEqual(seller_asks[0].price,
                                r.fam.inventory[A]["cost"] / bid.qty)

    def test_foreign_fills_are_not_adopted(self):
        # 1.0 fills in a market 3.0 has no orders in must not become stock
        r = Rig()
        r.add_market(A)
        r.cycle()
        r.positions["some-v1-market"] = (500.0, 100.0)
        r.cycle()
        self.assertNotIn("some-v1-market", r.fam.inventory)

    def test_earned_today_accrues_and_rolls(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        r.cycle()
        rate = sum(o.live_est or 0 for o in r.fam.orders.values())
        self.assertGreater(rate, 0)
        before = r.fam.earned_today
        r.cycle()
        self.assertAlmostEqual(r.fam.earned_today - before,
                               rate * 60 / 86400.0, places=4)
        r.cycle(advance=86400.0)                     # next ET day
        self.assertTrue(r.fam.earned_history)

    def test_restore_round_trip(self):
        r = Rig()
        r.add_market(A)
        r.cycle()
        d = r.fam.to_dict()
        r2 = Rig()
        r2.fam.restore(d)
        self.assertEqual(set(r2.fam.orders), set(r.fam.orders))
        self.assertEqual(r2.fam.universe.keys(), r.fam.universe.keys())
        self.assertEqual(r2.fam.terms.get(A).pool, 100.0)


class TestWindow(unittest.TestCase):
    def test_no_window_means_always_resting(self):
        self.assertTrue(resting_ok(0.0, FamilyConfig(rest_from=None)))
        self.assertTrue(resting_ok(1e9, politics.config()))

    def test_the_owner_can_keep_a_family_active_past_its_window(self):
        # owner, 2026-09-04: "Cfb can go active until 5:00 pm eastern
        # today" — a resting window of one hour on Monday mornings, so
        # the rig's clock (a Monday, 08:46 ET) sits in the game window
        cfg = FamilyConfig(name="Politics", tag="POL", known_ground=True,
                           rest_style="join_quiet", revive=True,
                           capital_usd=100.0, per_market_usd=2.0, revive_max_usd=5.0,
                           min_days_out=3, rest_from=(0, 0), rest_until=(0, 1))
        r = Rig(cfg=cfg)
        r.add_market(A)
        self.assertFalse(resting_ok(r.now, cfg))
        s = r.cycle()
        self.assertEqual(s["mode"], "game window")
        self.assertEqual(r.exchange.live, {})
        r.fam.active_until = r.now + 3600.0          # the owner's say
        s = r.cycle()
        self.assertEqual(s["mode"], "on")
        self.assertTrue(s["resting_ok"])
        self.assertEqual(s["active_until"], r.fam.active_until)
        self.assertTrue(r.exchange.live)             # it quotes as in resting hours
        d = r.fam.to_dict()
        self.assertEqual(d["active_until"], r.fam.active_until)
        r2 = Rig(cfg=cfg)
        r2.fam.restore(d)
        self.assertEqual(r2.fam.active_until, r.fam.active_until)   # survives a restart
        s = r.cycle(advance=3601.0)                  # the time passes: the window rules again
        self.assertEqual(s["mode"], "game window")
        self.assertEqual(s["active_until"], 0.0)
        self.assertEqual(r.exchange.live, {})


if __name__ == "__main__":
    unittest.main()


class TestAdoption(unittest.TestCase):
    def foreign(self, oid, market, intent="ORDER_INTENT_BUY_LONG",
                price=0.30, size=5.0, manual=False):
        from v3.intents import REST_SIDE
        return {"id": oid, "market": market, "side": REST_SIDE[intent],
                "price": price, "size": size, "intent": intent,
                "manual": manual}

    def rig_with_foreign(self, switch=True):
        r = Rig(switch=switch)
        r.add_market(A)
        r.exchange.live["v1a"] = self.foreign("v1a", A)
        r.exchange.live["v1x"] = self.foreign(
            "v1x", A, intent="ORDER_INTENT_SELL_LONG", price=0.60)
        r.exchange.live["own"] = self.foreign("own", A, manual=True)
        r.exchange.live["far"] = self.foreign("far", "not-our-market")
        return r

    def test_observing_previews_but_claims_nothing(self):
        r = self.rig_with_foreign(switch=False)
        s = r.cycle()
        # v1a + v1x + own: an exchange-flagged MANUAL order is RECORDED
        # too since 2026-08-24, so the cover math can see it. Only the
        # far market (not our ground) is left out.
        self.assertEqual(s["would_adopt"], 3)
        self.assertEqual(set(r.fam.orders), set())

    def test_armed_records_unknown_orders_hands_off(self):
        """Since 2026-08-22 ("Don't let it cancel orders I set by hand"):
        the 1.0/2.0 handover is over, so any order this engine did not
        place is the OWNER'S — recorded as manual, never managed."""
        r = self.rig_with_foreign()
        r.positions[A] = (10.0, 4.0)                 # held stock too
        s = r.cycle()
        self.assertIn("v1a", r.fam.orders)
        self.assertEqual(r.fam.orders["v1a"].purpose, "manual")
        self.assertIn("v1x", r.fam.orders)
        self.assertEqual(r.fam.orders["v1x"].purpose, "manual")
        self.assertIn("own", r.fam.orders)           # recorded...
        self.assertEqual(r.fam.orders["own"].purpose, "manual")  # ...hands off
        self.assertNotIn("far", r.fam.orders)        # not our ground
        self.assertEqual(s["would_adopt"], 0)
        self.assertEqual(r.fam.inventory[A]["qty"], 10.0)
        # no takeover page — the owner knows what he placed
        self.assertFalse(any("took over" in t for t, _ in r.alerts))
        # recorded orders are not phantom-filled on the next cycle
        n = len(r.fam.orders)
        r.cycle()
        self.assertEqual(len(r.fam.orders), n)
        self.assertIn("v1a", r.fam.orders)           # still untouched

    def test_sibling_family_claims_are_respected(self):
        r = self.rig_with_foreign()
        s = r.fam.cycle(r.now + 60, r.exchange.open_orders(), r.positions,
                        r.exchange, True, foreign_ids={"v1a"})
        self.assertNotIn("v1a", r.fam.orders)
        self.assertIn("v1x", r.fam.orders)


class TestImprove(unittest.TestCase):
    def wall_book(self, now):
        # college shape: a junk wall as the only bid, a real ask far away
        return Book(bids=((0.01, 6000.0),), asks=((0.47, 20.0),),
                    tick=0.01, fetched_at=now)

    def cfg(self, improve):
        return FamilyConfig(name="CFB", tag="CFB", known_ground=False,
                            rest_style="behind", revive=False,
                            allow_improve=improve,
                            capital_usd=150.0, per_market_usd=1.0)

    def test_college_still_quotes_a_junk_wall_within_the_probe(self):
        # owner, 2026-08-25: fronting a blank market is the probe
        # ratchet's job, at minimum size — which also means JOINING the
        # wall with real size can now legitimately beat a tiny front.
        # Either way the book gets quoted, nothing rests deeper than
        # the earned reach, and anything in front is probe-sized.
        r = Rig(cfg=self.cfg(True))
        r.add_market(A, book=self.wall_book(r.now))
        r.fam.probe_ratchet[f"{A}|BUY"] = [3, 0.0]
        r.cycle()
        bids = [o for o in r.fam.orders.values() if o.side == "BUY"]
        self.assertTrue(bids)
        self.assertLessEqual(bids[0].price, 0.04 + 1e-9)   # ratchet-bound
        if bids[0].price > 0.011:                    # in front: probe size
            self.assertLessEqual(bids[0].qty, 1.0 + 1e-9)
        self.assertLessEqual(bids[0].price * bids[0].qty, 0.51)  # inside caps

    def test_fronting_stays_inside_the_other_touch(self):
        # every family may quote in front now (owner, 2026-08-21) — the
        # hard bound is post-only mechanics: a full tick inside the
        # opposing touch, and small money
        r = Rig(cfg=self.cfg(False))
        r.add_market(A, book=self.wall_book(r.now))
        r.cycle()
        bids = [o for o in r.fam.orders.values() if o.side == "BUY"]
        self.assertTrue(bids)
        for o in bids:
            self.assertLessEqual(o.price, 0.47 - 0.01 + 1e-9)
            self.assertLessEqual(o.price * o.qty, 0.51)
