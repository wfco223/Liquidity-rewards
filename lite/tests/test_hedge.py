"""What a fill gives back, netted over an event's outcomes (lite/hedge.py)."""

import unittest

from lite import hedge

SEN = "scc-senate-gop-2026-11-03-"
HSE = "scc-hrep-rep-2026-11-03-"


def book(held, events):
    """held: slug -> (shares, stake a share); events: event -> slugs."""
    ev_of = {s: e for e, ss in events.items() for s in ss}
    return hedge.RiskBook(held, lambda s: ev_of.get(s, ""), events)


class Kinds(unittest.TestCase):
    def test_tokens_keep_brackets_whole(self):
        t = hedge.tokens(["vmc-usse-ga-2026-11-03-d0-10", "vmc-usse-ga-2026-11-03-r0-10",
                          "vmc-usse-ga-2026-11-03-rgte10"])
        self.assertEqual(sorted(t.values()), ["d0-10", "r0-10", "rgte10"])

    def test_which_events_net(self):
        self.assertEqual(hedge.event_kind([SEN + "46", SEN + "47", SEN + "lte45"])[0], "numeric")
        self.assertEqual(hedge.event_kind([HSE + "gte180", HSE + "gte185"])[0], "numeric")
        self.assertEqual(hedge.event_kind(["ushrewc-ushr-ca-47-2026-11-03-dem",
                                           "ushrewc-ushr-ca-47-2026-11-03-rep"])[0], "categorical")
        self.assertEqual(hedge.event_kind(["paccc-balpow-2026-11-03-dsweep",
                                           "paccc-balpow-2026-11-03-rsweep",
                                           "paccc-balpow-2026-11-03-dhou-rsen"])[0], "categorical")
        # nested: by-dates, "over N", a "wins" beside its own brackets
        self.assertIsNone(hedge.event_kind(["opdc-trump-resig-2026-12-31",
                                            "opdc-trump-resig-2027-12-31"])[0])
        self.assertIsNone(hedge.event_kind(["vtc-x-2026-11-03-gt2m", "vtc-x-2026-11-03-gt3m"])[0])
        self.assertIsNone(hedge.event_kind(["vmc-x-2026-11-03-dwin", "vmc-x-2026-11-03-d0-5",
                                            "vmc-x-2026-11-03-r0-5"])[0])
        # people who can all resolve Yes ("announced out"): not a winner product
        self.assertIsNone(hedge.event_kind(["apdc-x-2026-12-31-brorol",
                                            "apdc-x-2026-12-31-howlut"])[0])
        # not politics: never netted
        self.assertIsNone(hedge.event_kind([SEN + "46", SEN + "47"], politics=False)[0])

    def test_dated_ladders_are_never_netted(self):
        # by-date pairs whose dates share a prefix: what tells them apart is
        # a day, a month-day or a year — all of them can resolve Yes
        for pair in (["ewc-x-2026-12-15", "ewc-x-2026-12-31"],
                     ["mowc-nato-us-12-31-2026", "mowc-nato-us-12-31-2027"],
                     ["mowc-x-10-15-2026", "mowc-x-10-31-2026"],
                     ["scc-x-2026-12-15", "scc-x-2026-12-31"]):
            self.assertIsNone(hedge.event_kind(pair)[0], pair)
        # a count is netted only for the seat-count products
        self.assertIsNone(hedge.event_kind(["vtc-x-2026-11-03-15", "vtc-x-2026-11-03-31"])[0])
        # an outcome named for a product we do not know: not netted
        self.assertIsNone(hedge.event_kind(["mowc-x-2026-11-03-dem",
                                            "mowc-x-2026-11-03-rep"])[0])

    def test_a_win_beside_its_own_brackets(self):
        for toks in (["demwin", "dem0-2", "rep0-2"], ["lulawin", "lula5-10", "bols0-5"],
                     ["dwin", "dgte10", "r0-5"]):
            self.assertIsNone(hedge.event_kind(["vmc-x-2026-11-03-" + t for t in toks])[0], toks)
        self.assertEqual(hedge.event_kind(["vmc-x-2026-11-03-" + t
                                           for t in ("dwin", "r0-5", "r5-10")])[0], "categorical")


class OneMarket(unittest.TestCase):
    def test_sale_of_what_he_holds_gives_back_the_price(self):
        rb = book({"m": (100.0, 0.30)}, {})
        g = rb.group("m")
        self.assertAlmostEqual(g.frees("m", "SELL", 0.40, 100), 40.0)
        b = g.best("m", "SELL", 0.40)
        self.assertEqual((b["q"], b["frees"], b["ps"]), (100.0, 40.0, 0.4))

    def test_past_what_he_holds_it_opens_a_short(self):
        g = book({"m": (100.0, 0.30)}, {}).group("m")
        # 100 sold for $40, then 50 short at 40c ties up 60c a share
        self.assertAlmostEqual(g.frees("m", "SELL", 0.40, 150), 10.0)

    def test_a_new_order_ties_money_up(self):
        g = book({}, {}).group("m")
        self.assertAlmostEqual(g.frees("m", "BUY", 0.20, 10), -2.0)
        self.assertAlmostEqual(g.frees("m", "SELL", 0.20, 10), -8.0)
        b = g.best("m", "BUY", 0.20)
        self.assertEqual((b["q"], b["frees"]), (0.0, 0.0))
        self.assertAlmostEqual(b["ps"], -0.2)

    def test_buying_back_a_short_gives_back_one_less_the_price(self):
        # short 50 held as No at 90c (sold Yes at 10c), bought back at 5c
        g = book({"m": (-50.0, 0.90)}, {}).group("m")
        self.assertAlmostEqual(g.frees("m", "BUY", 0.05, 50), 47.5)


class Netted(unittest.TestCase):
    def test_two_party_race(self):
        ev = {"e": ["ewc-x-dem", "ewc-x-rep"]}
        # short the Democrat 100 (No at 60c): the worst case is he wins
        rb = book({"ewc-x-dem": (-100.0, 0.60)}, ev)
        g = rb.group("ewc-x-rep")
        self.assertEqual(g.kind, "categorical")
        self.assertAlmostEqual(g.money, 60.0)
        # selling the Republican's Yes at 55c: one of them can lose at most
        self.assertAlmostEqual(g.frees("ewc-x-rep", "SELL", 0.55, 100), 55.0)
        b = g.best("ewc-x-rep", "SELL", 0.55)
        self.assertEqual((b["q"], b["frees"]), (100.0, 55.0))
        # buying the Republican is no help: an independent can still win
        self.assertLess(g.frees("ewc-x-rep", "BUY", 0.40, 10), 0)

    def test_seat_count(self):
        ev = {"s": [SEN + t for t in ("lte45", "46", "47", "48", "gte49")]}
        # short 200 of exactly 47 (No at 90c): worst is K = 47, $180
        rb = book({SEN + "47": (-200.0, 0.90)}, ev)
        g = rb.group(SEN + "46")
        self.assertEqual(g.kind, "numeric")
        self.assertAlmostEqual(g.money, 180.0)
        # a 5c Yes sale on 46 loses only when K = 46 ($-20 there now):
        # each share gives back 5c until K = 46 binds, at 200
        b = g.best(SEN + "46", "SELL", 0.05)
        self.assertEqual((b["q"], b["frees"], b["ps"]), (200.0, 10.0, 0.05))
        # buying 47 back: 95c a share, all 200
        b = g.best(SEN + "47", "BUY", 0.05)
        self.assertEqual((b["q"], b["frees"]), (200.0, 190.0))

    def test_a_full_set_is_figured_both_ways(self):
        # long both parties: if the exchange counts "neither wins", selling
        # the Democrat frees its price; if it counts only the two, the pair
        # was nearly riskless and the sale ties money up. The smaller stands.
        ev = {"e": ["ewc-x-dem", "ewc-x-rep"]}
        g = book({"ewc-x-dem": (100.0, 0.45), "ewc-x-rep": (100.0, 0.50)}, ev).group("ewc-x-dem")
        self.assertAlmostEqual(g.money, 95.0)
        self.assertAlmostEqual(g.money_core, 0.0)
        self.assertAlmostEqual(g.frees("ewc-x-dem", "SELL", 0.46, 100), -49.0)
        # only the first 9 shares' 1c profit is sure either way
        b = g.best("ewc-x-dem", "SELL", 0.46)
        self.assertEqual((b["q"], b["frees"]), (9.09, 0.09))

    def test_no_count_under_zero_and_one_past_the_ladder_both_ways(self):
        t = "ushsscc-ushrsc-nv-2026-11-03-"
        ev = {"e": [t + k for k in ("0", "1", "2", "3")]}
        g = book({t + k: (100.0, 0.18) for k in ("0", "1", "2", "3")}, ev).group(t + "0")
        self.assertEqual(g.kind, "numeric")
        self.assertEqual(len(g.base), 5)                     # 0 to 4, nothing under zero
        self.assertAlmostEqual(g.frees(t + "0", "SELL", 0.20, 100), -52.0)

    def test_margin_plain_and_netted(self):
        ev = {"e": ["ewc-x-dem", "ewc-x-rep"]}
        rb = book({"ewc-x-dem": (-100.0, 0.60), "ewc-x-rep": (-100.0, 0.45), "lone": (10.0, 0.5)},
                  ev)
        m = rb.margin()
        self.assertEqual(m["plain"], 110.0)
        # dem wins: 60 + 45 - 100 = 5; rep wins: 5; none: -95 -> 5, plus 5 alone
        self.assertEqual(m["netted"], 10.0)

    def test_a_market_outside_the_event_list_stands_alone(self):
        rb = book({"x": (-100.0, 0.6)}, {"e": ["ewc-x-dem", "ewc-x-rep"]})
        self.assertIsNone(rb.group("x").kind)
        self.assertFalse(rb.netted("x"))


class After(unittest.TestCase):
    """A group after a fill is the group built from what is held then."""

    def check(self, held, ev, m, side, px, q):
        rb = book(held, ev)
        g = rb.group(m)
        a = g.after(m, side, px, q)
        b = hedge.Group(g.kind, {x: t for x, t in hedge.tokens(g.markets).items()}
                        if g.kind else {m: ""}, a.held)
        for x, y in zip(a.base, b.base):
            self.assertAlmostEqual(x, y, places=9)
        self.assertAlmostEqual(a.money, b.money, places=9)
        return a

    def test_after(self):
        ev = {"s": [SEN + t for t in ("lte45", "46", "47", "gte48")]}
        h = {SEN + "47": (-200.0, 0.9), SEN + "46": (50.0, 0.2)}
        self.check(h, ev, SEN + "46", "SELL", 0.3, 20)     # part of the long
        self.check(h, ev, SEN + "46", "SELL", 0.3, 80)     # through flat
        self.check(h, ev, SEN + "46", "BUY", 0.25, 30)     # adds to it
        self.check(h, ev, SEN + "47", "BUY", 0.08, 200)    # the short bought back
        self.check(h, ev, SEN + "lte45", "SELL", 0.05, 100)  # a new short
        a = self.check(h, ev, SEN + "47", "BUY", 0.08, 50)
        self.assertEqual(a.held[SEN + "47"], (-150.0, 0.9))


class Stake(unittest.TestCase):
    def test_fallbacks(self):
        self.assertEqual(hedge.stake_a_share(10, {"cost": {"value": "3.0"}}, None), (0.3, False))
        self.assertEqual(hedge.stake_a_share(-10, {"cost": {"value": "0"}, "avgPx": {"value": "0.8"}},
                                             None), (0.8, False))
        self.assertEqual(hedge.stake_a_share(-10, {"cost": {"value": "0"}}, 0.3), (0.7, True))
        self.assertEqual(hedge.stake_a_share(10, {}, None), (0.5, True))



class TheApp(unittest.TestCase):
    """The page's figures (lite/app.py): rows, the card, the order card,
    the typed quote and the margin line. All of them read; none may place,
    move or cancel an order."""

    def setUp(self):
        import time
        from lite.tests.test_lite import M, M2, make, raw_order
        from lite.tests.test_markets import DEM, EV, book, listed
        self.M, self.M2, self.DEM = M, M2, DEM
        app, c = make()
        listed(app, {M: EV, DEM: EV, M2: "usse-ks-2026-11-03"})
        c.books[DEM] = book([(0.55, 2000.0)], [(0.58, 1500.0)])
        for s in (M, DEM):
            app.cache.put(s, c.book(s))
        # short the Democrat 100, held as No at 60c; long 10 of Kansas alone
        app.positions = {DEM: {"netPositionDecimal": "-100", "cost": {"value": "60"}},
                         M2: {"netPositionDecimal": "10", "cost": {"value": "3"},
                              "cashValue": {"value": "4"}}}
        c.raw = [raw_order("O1", M, "SELL", 0.42, 50, "ORDER_INTENT_BUY_SHORT")]
        app._read_orders()
        self.app, self.c, self.t = app, c, time.time()

    def rows(self):
        return {r["m"]: r for r in self.app.market_rows(self.t, 50.0)}

    def test_rows_say_what_a_fill_gives_back(self):
        r = self.rows()
        m = r[self.M]
        self.assertTrue(m["lr"])
        # selling the Republican's Yes at 42c, one of the two can lose:
        # 42c a share back up to the 100 the short holds; his own 50 at
        # 42c fill first, so a new order there adds the other 50's
        b = m["risk"]["SELL"]
        self.assertEqual((b["q"], b["frees"], b["ahead"]), (100.0, 42.0, 50))
        self.assertEqual((b["more"]["q"], b["more"]["frees"]), (50.0, 21.0))
        self.assertNotIn("BUY", m["risk"])        # an independent can still win
        self.assertEqual(m["ask"]["o"][0]["fr"], 21.0)
        self.assertGreater(m["risk"]["SELL"]["gain"], 40)    # alone it would tie money up
        d = r[self.DEM]
        # buying the short back at 55c frees 45c a share — what it would
        # free in a market by itself, so it is not what the filter is for
        self.assertEqual(d["risk"]["BUY"]["frees"], 45.0)
        self.assertEqual(d["risk"]["BUY"]["gain"], 0.0)
        self.assertFalse(d["lr"])

    def test_a_resting_order_counts_the_better_ones_first(self):
        from lite.tests.test_lite import raw_order
        self.c.raw = [raw_order("O1", self.M, "SELL", 0.42, 100, "ORDER_INTENT_BUY_SHORT"),
                      raw_order("O2", self.M, "SELL", 0.45, 100, "ORDER_INTENT_BUY_SHORT")]
        self.app._read_orders()
        r = self.rows()[self.M]
        fr = {o["id"]: o["fr"] for o in r["ask"]["o"]}
        # the 42c ask fills first and takes the whole 100 of the hedge; the
        # 45c one after it opens a short beside it
        self.assertEqual(fr, {"O1": 42.0, "O2": -55.0})
        self.assertEqual(self.app.order_math("O2")["fill"], -55.0)
        self.assertEqual(self.app.risk_quote(self.M, "SELL", "45", "100")["frees"], -55.0)

    def test_a_plain_sale_is_not_a_reason(self):
        # long the Republican alone in his race: selling it frees its price,
        # exactly what it would free in a market by itself
        self.app.positions = {self.M: {"netPositionDecimal": "10", "cost": {"value": "3"},
                                       "cashValue": {"value": "4"}}}
        self.c.raw = []
        self.app._read_orders()
        r = self.rows()[self.M]
        self.assertEqual(r["risk"]["SELL"]["gain"], 0.0)
        self.assertFalse(r["lr"])

    def test_a_market_alone_is_not_in_the_filter(self):
        r = self.rows()
        self.assertIn(self.M2, r)
        self.assertFalse(r[self.M2]["lr"])

    def test_the_card_the_order_and_the_quote(self):
        a = self.app
        v = a.book_view(self.M)
        self.assertTrue(v["netted"])
        self.assertEqual(v["outcomes"], 2)
        self.assertEqual(v["rk"]["SELL"]["frees"], 42.0)
        self.assertEqual(v["rk"]["SELL"]["more"]["frees"], 21.0)
        self.assertEqual(v["ours"][0]["fr"], 21.0)
        self.assertEqual(a.order_math("O1")["fill"], 21.0)
        q = a.risk_quote(self.M, "SELL", "42", "50")
        self.assertTrue(q["ok"])
        self.assertEqual((q["frees"], q["ahead"]), (21.0, 50))
        self.assertEqual((q["best"]["q"], q["best"]["frees"]), (50.0, 21.0))
        # an ask at 41c sells before his at 42c: nothing of his is ahead
        q = a.risk_quote(self.M, "SELL", "41", "100")
        self.assertEqual((q["ahead"], q["frees"]), (0.0, 41.0))
        self.assertNotIn("frees", a.risk_quote(self.M, "SELL", "42", ""))
        for bad in ((self.M, "SELL", "0", "5"), (self.M, "X", "42", "5"),
                    (self.M, "SELL", "42", "999999"), ("", "SELL", "42", "5")):
            self.assertFalse(a.risk_quote(*bad)["ok"], bad)

    def test_the_margin_line(self):
        # the balance row the 20-second read keeps
        self.app.balance = {"buyingPower": 512.5, "marginRequirement": 61.0}
        m = self.app.data()["margin"]
        self.assertEqual((m["exchange"], m["netted"], m["plain"], m["n"], m["est"]),
                         (61.0, 63.0, 63.0, 2, 0.0))
        self.app.balance = {}
        self.assertIsNone(self.app.data()["margin"]["exchange"])

    def test_a_failure_in_the_figures_leaves_the_page_standing(self):
        def boom():
            raise ValueError("bad feed row")
        self.app.risk_book = boom
        d = self.app.data()
        self.assertIsNone(d["margin"])
        self.assertTrue(d["markets"])
        self.assertFalse(any(r["lr"] for r in d["markets"]))
        v = self.app.book_view(self.M)
        self.assertTrue(v["ok"])
        self.assertEqual(v["rk"], {})
        self.assertIsNone(self.app.order_math("O1")["fill"])
        self.assertFalse(self.app.risk_quote(self.M, "SELL", "42", "5")["ok"])
        self.assertEqual(sum("risk figures" in n["note"] for n in self.app.notes), 1)

    def test_nothing_is_placed_moved_or_cancelled(self):
        self.rows()
        self.app.book_view(self.M)
        self.app.risk_quote(self.M, "SELL", "42", "50")
        self.app.data()
        self.app.order_math("O1")
        self.assertEqual(self.c.posts, [])


if __name__ == "__main__":
    unittest.main()
