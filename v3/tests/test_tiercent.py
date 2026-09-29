"""The 1-cent report (owner, 2026-09-28 "Just do the minimal amount to earn
rewards 1 cent per day" ... "Don't you need to know how many shares are
resting?" ... "Yes").

Read-only: from the live books, for every tier side that holds its Target
Size, the shares an order needs to earn a cent a day at each price from
the side's best back to where the window closes, and the money it ties
up; and every field the exchange's program record carries. Nothing here
touches an order or reads the exchange.
"""
import json
import unittest

from v3.books import BookCache
from v3.programs import Program
from v3.scoring import Book
from v3.terms import TermsStore
from v3 import risk
from v3.tiercent import (CENT, CENT_EVERY_S, MARGIN_KEEP, PAGE_ROWS_SLOW, QTY_MAX, TierCent,
                         cent_ladder, event_of, group_kind, margin_check, netted, plain,
                         window_edge, within)
from v3.tierfair import TierFair
from v3.tiervalue import side_share

T0 = 1_790_300_000.0
T1P = "midterms_t1_control_bop_tossup_senate_20260924"
T4P = "midterms_t4_house_winners_20260924"
A = "ussewc-usse-ga-2026-11-03-dem"


class NoDesk:
    def __getattr__(self, name):
        raise AssertionError(f"the 1-cent report touched the desk ({name})")


class NoClient:
    def __getattr__(self, name):
        raise AssertionError(f"the 1-cent report read the exchange ({name})")


class Fam:
    def __init__(self):
        self.universe = {}
        self.terms = TermsStore()
        self.cache = BookCache()
        self.desk = NoDesk()
        self.client = NoClient()
        self.orders = {}

    def _side_pool(self, slug, prog):
        return prog.pool / max(prog.event_n, 1) / 2.0

    def add(self, slug, pid, bids, asks, pool=1000.0, target=1000.0, df=0.5, at=T0,
            tick=0.01):
        self.universe[slug] = {"event_n": 2, "name": f"{slug} — x"}
        self.terms.current[slug] = Program(pool=pool, target=target, df=df, status="active",
                                           pid=pid, event_n=2,
                                           start="2026-09-24T02:00:00Z")
        self.cache.put(slug, Book(bids=tuple(bids), asks=tuple(asks), tick=tick,
                                  fetched_at=at))


def make(raws=None):
    fam = Fam()
    tf = TierFair(fam, clock=lambda: T0)
    tc = TierCent(tf, fam, raws=raws, clock=lambda: T0)
    return fam, tf, tc


class TestTheLadder(unittest.TestCase):
    def test_the_window_takes_the_boundary_level_whole(self):
        lv = [(0.40, 300), (0.39, 500), (0.38, 400), (0.37, 900)]
        self.assertEqual(window_edge(lv, 800), 2)
        self.assertEqual(window_edge(lv, 801), 3)
        self.assertEqual(window_edge(lv, 10_000), 4)

    def test_each_size_earns_a_cent_by_the_exchanges_arithmetic(self):
        lv = [(0.40, 300), (0.39, 500), (0.38, 400), (0.37, 900)]
        pool = 50.0
        r = cent_ladder("BUY", lv, 0.01, 0.5, 1000, pool)
        self.assertEqual([x[1] for x in r["rows"]], [0.40, 0.39, 0.38])
        self.assertEqual(r["edge"], 0.38)
        for k, px, q, coll in r["rows"]:
            share, ok, _gap = side_share("BUY", lv, 0.01, 0.5, 1000, {px: q})
            self.assertTrue(ok)
            # never under a cent, and the least that earns one: a
            # hundredth of a share less falls short
            self.assertGreaterEqual(share * pool, CENT - 1e-6)
            less = side_share("BUY", lv, 0.01, 0.5, 1000, {px: q - 0.01})[0]
            self.assertLess(less * pool, CENT)
            self.assertAlmostEqual(coll, q * px, places=3)
        # further back costs more shares for the same cent
        qs = [x[2] for x in r["rows"]]
        self.assertEqual(qs, sorted(qs))

    def test_where_our_size_pushes_the_boundary_level_out_it_needs_less(self):
        # 990 of a 1,000 target rest ahead: any size of ours at 39c
        # carries the side over there and the 38c level leaves the window
        lv = [(0.40, 990), (0.38, 5000)]
        r = cent_ladder("BUY", lv, 0.01, 0.5, 1000, 0.05)
        row = [x for x in r["rows"] if x[1] == 0.39][0]
        q = row[2]
        share = side_share("BUY", lv, 0.01, 0.5, 1000, {0.39: q})[0]
        self.assertGreaterEqual(share * 0.05, CENT - 1e-6)
        less = side_share("BUY", lv, 0.01, 0.5, 1000, {0.39: q - 0.01})[0]
        self.assertLess(less * 0.05, CENT)
        # the window without us would have put 5,000 at 38c in the count
        self.assertLess(q, 0.2 * (990 + 5000 * 0.25) / (0.5 * 0.8))

    def test_nothing_is_listed_past_the_window(self):
        lv = [(0.40, 2000), (0.39, 500)]
        r = cent_ladder("BUY", lv, 0.01, 0.5, 1000, 50.0)
        # 2,000 at the best price reach the target: a tick back earns nothing
        self.assertEqual([x[1] for x in r["rows"]], [0.40])

    def test_the_ask_side_goes_back_upward_and_ties_up_one_less_the_price(self):
        lv = [(0.60, 300), (0.61, 500), (0.62, 400)]
        r = cent_ladder("SELL", lv, 0.01, 0.5, 1000, 50.0)
        self.assertEqual([x[1] for x in r["rows"]], [0.60, 0.61, 0.62])
        for _k, px, q, coll in r["rows"]:
            self.assertAlmostEqual(coll, q * (1 - px), places=3)
            share, ok, _g = side_share("SELL", lv, 0.01, 0.5, 1000, {px: q})
            self.assertGreaterEqual(share * 50.0, CENT - 1e-6)

    def test_a_side_under_its_target_size_lists_no_price(self):
        r = cent_ladder("BUY", [(0.40, 300)], 0.01, 0.5, 2000, 50.0)
        self.assertEqual(r["rows"], [])
        self.assertEqual(r["under"], 1700)
        self.assertAlmostEqual(r["coll_best"], 1700 * 0.40)
        self.assertAlmostEqual(r["coll_wall"], 17.0)
        e = cent_ladder("SELL", [], 0.01, 0.5, 2000, 50.0)
        self.assertEqual(e["under"], 2000)
        self.assertIsNone(e["coll_best"])

    def test_a_price_that_needs_more_than_the_desk_can_place_is_left_out(self):
        lv = [(0.40, 5000), (0.30, 5000)]
        # a cent of a 2c pool: half the side's score at the best price is
        # 5,000 shares; ten ticks back at df 0.25 it would be billions
        r = cent_ladder("BUY", lv, 0.01, 0.25, 6000, 0.02)
        self.assertTrue(r["rows"])
        self.assertTrue(all(q <= QTY_MAX for _k, _p, q, _c in r["rows"]))
        self.assertLess(len(r["rows"]), r["ticks_to_edge"] + 1)


class TestTheReport(unittest.TestCase):
    def test_it_reports_every_tier_from_the_books_and_reads_nothing(self):
        fam, tf, tc = make()
        fam.add(A, T1P, [(0.40, 600), (0.39, 600)], [(0.42, 300)], pool=100.0,
                target=1000.0)
        tc.tick(T0)
        r = tc.report
        self.assertTrue(r["ok"])
        t1 = r["tiers"]["t1"]
        self.assertEqual((t1["markets"], t1["fresh"]), (1, 1))
        self.assertEqual((t1["sides_paying"], t1["sides_under"]), (1, 1))
        self.assertAlmostEqual(t1["pool_paying"], 25.0)
        row = r["markets"][A]
        self.assertEqual(row["spread"], 0.02)
        self.assertEqual(r["tiers"]["t1"]["spreads"], {"<=2c": 1})
        self.assertEqual(row["SELL"]["under"], 700)
        self.assertGreater(row["coll_nearest"], 0)
        json.dumps(tc.to_dict())

    def test_a_stale_book_is_not_reported_on(self):
        fam, tf, tc = make()
        fam.add(A, T1P, [(0.40, 2000)], [(0.42, 2000)], at=T0 - 3600)
        tc.tick(T0)
        t1 = tc.report["tiers"]["t1"]
        self.assertEqual((t1["markets"], t1["fresh"]), (1, 0))
        self.assertNotIn(A, tc.report["markets"])

    def test_it_runs_every_ten_minutes(self):
        fam, tf, tc = make()
        fam.add(A, T1P, [(0.40, 2000)], [(0.42, 2000)])
        tc.tick(T0)
        at = tc.report["at"]
        tc.tick(T0 + 60)
        self.assertEqual(tc.report["at"], at)
        tc.tick(T0 + CENT_EVERY_S + 1)
        self.assertEqual(tc.report["at"], round(T0 + CENT_EVERY_S + 1, 1))

    def test_tier_4_keeps_the_cheapest_markets_and_counts_them_all(self):
        fam, tf, tc = make()
        n = PAGE_ROWS_SLOW + 15
        for i in range(n):
            fam.add(f"ushrewc-ushr-x-{i:03d}-2026-11-03-dem", T4P,
                    [(0.40, 2000 + 100 * i)], [(0.45, 2000 + 100 * i)], pool=5.0,
                    target=2000.0, df=0.4)
        tc.tick(T0)
        r = tc.report
        self.assertEqual(r["tiers"]["t4"]["fresh"], n)
        self.assertEqual(r["tiers"]["t4"]["sides_paying"], 2 * n)
        kept = [s for s, x in r["markets"].items() if x["tier"] == "t4"]
        self.assertEqual(len(kept), PAGE_ROWS_SLOW)
        # the thinnest books need the fewest shares: those are kept
        self.assertIn("ushrewc-ushr-x-000-2026-11-03-dem", kept)
        self.assertNotIn(f"ushrewc-ushr-x-{n - 1:03d}-2026-11-03-dem", kept)

    def test_the_page_gets_totals_and_field_names_not_every_ladder(self):
        seen = {T1P: {"row": {"marketSlug": A, "maxSpread": 0.05}, "period": {"programId": T1P},
                      "row_keys": ["marketSlug", "maxSpread"],
                      "period_keys": ["programId"], "reads": 3, "at": T0}}
        fam, tf, tc = make(raws=lambda: [seen, {}])
        fam.add(A, T1P, [(0.40, 2000)], [(0.42, 2000)])
        tc.tick(T0)
        # the saved report keeps the whole record
        self.assertEqual(tc.to_dict()["programs"][T1P]["row"]["maxSpread"], 0.05)
        v = tc.view()
        self.assertNotIn("markets", v)
        self.assertEqual(v["programs"][T1P]["row_keys"], ["marketSlug", "maxSpread"])

    def test_the_tier_fairs_page_carries_it(self):
        fam, tf, tc = make()
        tf.cent = tc
        fam.add(A, T1P, [(0.40, 2000)], [(0.42, 2000)])
        tc.tick(T0)
        self.assertEqual(tf._cent_view()["tiers"]["t1"]["fresh"], 1)


def t4_raws(ms=0.06):
    return lambda: [{T4P: {"row": {"marketSlug": "x"},
                           "period": {"programId": T4P, "maxSpread": ms},
                           "row_keys": ["marketSlug"], "period_keys": ["maxSpread", "programId"],
                           "reads": 1, "at": T0, "extra": {"maxSpread": [ms]}}}]


class TestTheSpreadRule(unittest.TestCase):
    """Tier 4's program records carry maxSpread (0.06 on 2026-09-28). What
    it means is not settled, so both readings are reported side by side:
    A, every order within it of the midpoint; B, the book's bid-ask gap
    within it."""

    def test_reading_a_counts_only_the_band_toward_the_target_size(self):
        bids = [(0.40, 1500), (0.37, 300), (0.33, 1000)]
        # midpoint 41c: 40c and 37c are inside 6c, 33c is 8c out
        base = cent_ladder("BUY", bids, 0.01, 0.4, 2000, 1.25)
        self.assertTrue(base["rows"])
        a = within("BUY", bids, 0.01, 0.4, 2000, 1.25, 0.41, 0.06)
        self.assertEqual(a["rows"], [])
        self.assertEqual(a["under"], 200)

    def test_reading_a_takes_a_price_exactly_at_the_edge(self):
        asks = [(0.42, 1000), (0.47, 1500)]
        # midpoint 41c: 47c is exactly 6c out
        a = within("SELL", asks, 0.01, 0.4, 2000, 1.25, 0.41, 0.06)
        self.assertEqual([r[1] for r in a["rows"]][-1], 0.47)

    def test_both_readings_are_counted_per_tier_and_the_others_are_untouched(self):
        fam, tf, tc = make(raws=t4_raws())
        # a 2c book: both readings agree with the book as it rests
        fam.add("ushrewc-a", T4P, [(0.40, 2500)], [(0.42, 2500)], pool=5.0, target=2000.0,
                df=0.4)
        # a 10c book: B shuts it; A keeps what sits within 6c of 45c
        fam.add("ushrewc-b", T4P, [(0.40, 2500)], [(0.50, 2500)], pool=5.0, target=2000.0,
                df=0.4)
        # one side only: no midpoint, no gap
        fam.add("ushrewc-c", T4P, [(0.40, 2500)], [], pool=5.0, target=2000.0, df=0.4)
        fam.add(A, T1P, [(0.40, 2000)], [(0.42, 2000)])
        tc.tick(T0)
        t4 = tc.report["tiers"]["t4"]
        self.assertEqual(t4["max_spread"], [0.06])
        self.assertEqual(t4["sides_paying"], 5)
        self.assertEqual((t4["A"]["sides_paying"], t4["A"]["sides_off"]), (4, 2))
        self.assertEqual((t4["B"]["sides_paying"], t4["B"]["sides_off"]), (2, 4))
        self.assertEqual(t4["no_mid"], 1)
        rb = tc.report["markets"]["ushrewc-b"]
        self.assertFalse(rb["B"])
        self.assertEqual(rb["A"]["mid"], 0.45)
        self.assertEqual(rb["ms"], 0.06)
        self.assertTrue(tc.report["markets"]["ushrewc-a"]["B"])
        # Tier 1's program carries no spread rule: no readings there
        self.assertNotIn("A", tc.report["tiers"]["t1"])
        self.assertNotIn("ms", tc.report["markets"][A])
        json.dumps(tc.to_dict())

    def test_a_program_without_the_field_gets_no_readings(self):
        fam, tf, tc = make(raws=lambda: [{}])
        fam.add("ushrewc-a", T4P, [(0.40, 2500)], [(0.50, 2500)], pool=5.0, target=2000.0,
                df=0.4)
        tc.tick(T0)
        self.assertNotIn("A", tc.report["tiers"]["t4"])
        self.assertEqual(tc.report["tiers"]["t4"]["sides_paying"], 2)


def short(m, px, q):
    return risk.Leg(m, False, q, (1 - px) * q, firm=False)


def long_(m, px, q):
    return risk.Leg(m, True, q, px * q, firm=False)


class TestNegativeRisk(unittest.TestCase):
    """Owner, 2026-09-29: "We also need to account for negative risk and
    mutually exclusive outcomes." Only one outcome of an event resolves
    Yes, so orders across its outcomes cannot all lose."""

    NAMES = {"d": "TX-15 House Election Winner — dem", "r": "TX-15 House Election Winner — rep",
             "b1": "TX-15 House Election Margin of Victory — d0-3",
             "b2": "TX-15 House Election Margin of Victory — d3-6",
             "bw": "TX-15 House Election Margin of Victory — rwin",
             "dw": "TX-15 House Election Margin of Victory — dwin",
             "t1": "Texas Senate Election: Turnout — gt10pt5m",
             "t2": "Texas Senate Election: Turnout — gt11m",
             "s50": "2026 Midterms: Republican Senate Seats? — 50",
             "s51": "2026 Midterms: Republican Senate Seats? — 51",
             "s57": "2026 Midterms: Republican Senate Seats? — gte57"}

    def test_the_event_is_the_name_and_a_bracket_is_one_outcome(self):
        self.assertEqual(event_of("vmc-ushrmov-tx-15-2026-11-03-d0-3", self.NAMES["b1"]),
                         ("TX-15 House Election Margin of Victory", "d0-3"))
        # no name: the slug, as the engine groups it
        self.assertEqual(event_of("ushrewc-ushr-tx-15-2026-11-03-dem", ""),
                         ("ushrewc-ushr-tx-15-2026-11-03", "dem"))

    def test_what_is_known_to_be_exclusive(self):
        self.assertEqual(group_kind(["dem", "rep"]), "categorical")
        self.assertEqual(group_kind(["d0-3", "d3-6", "rwin", "dgte12"]), "categorical")
        self.assertEqual(group_kind(["50", "51", "gte57", "lte45"]), "numeric")
        # nested thresholds can all resolve Yes together
        self.assertIsNone(group_kind(["gt10pt5m", "gt11m"]))
        self.assertIsNone(group_kind(["gte2", "gt3k"]))
        # a side's "wins" overlaps its own brackets
        self.assertIsNone(group_kind(["dwin", "d0-3"]))

    def test_shorts_across_one_event_share_one_collateral(self):
        legs = [short("d", 0.40, 100), short("r", 0.58, 100)]
        self.assertAlmostEqual(plain(legs), 60 + 42)
        # dem wins: the dem short loses 60, the rep short loses nothing
        self.assertAlmostEqual(netted(legs, self.NAMES), 60)

    def test_bids_get_no_credit_for_what_would_win(self):
        legs = [long_("d", 0.40, 100), long_("r", 0.58, 100)]
        # an order that would gain need not fill: "none of them" loses both
        self.assertAlmostEqual(netted(legs, self.NAMES), plain(legs))

    def test_nested_thresholds_stay_at_plain_collateral(self):
        legs = [short("t1", 0.30, 100), short("t2", 0.20, 100)]
        self.assertAlmostEqual(netted(legs, self.NAMES), plain(legs))

    def test_a_wins_beside_its_own_brackets_stays_plain(self):
        legs = [short("dw", 0.50, 100), short("b1", 0.20, 100)]
        self.assertAlmostEqual(netted(legs, self.NAMES), plain(legs))

    def test_seat_counts_are_swept_over_the_count(self):
        legs = [short("s50", 0.20, 100), short("s51", 0.25, 100), short("s57", 0.05, 100)]
        # one count wins: the worst is the dearest single short
        self.assertAlmostEqual(netted(legs, self.NAMES), 95)
        self.assertAlmostEqual(plain(legs), 80 + 75 + 95)

    def test_held_positions_count_their_gains(self):
        # a complete set of Yes held pays a dollar a share whoever wins
        firm = [risk.Leg("d", True, 100, 40, firm=True), risk.Leg("r", True, 100, 58, firm=True)]
        # the "none" outcome stays in, as in v3/risk.py: both lose there
        self.assertAlmostEqual(netted(firm, self.NAMES), 98)
        firm = [risk.Leg("d", True, 100, 40, firm=True), risk.Leg("r", False, 100, 42, firm=True)]
        # long dem and short rep: rep winning loses both
        self.assertAlmostEqual(netted(firm, self.NAMES), 82)

    def test_the_margin_check_prices_what_we_hold_beside_the_exchange(self):
        inv = [("d", -100, -40.0, False), ("r", -100, -58.0, True), ("x", 10, 30.0, False)]
        c = margin_check(inv, {"marginRequirement": 61.5, "openOrders": 1.0, "buyingPower": 5,
                               "currentBalance": 70}, 3, self.NAMES, T0)
        # the 30-dollar lot of 10 shares is not a price: skipped, and said
        self.assertEqual((c["n"], c["skipped"], c["est"], c["feed_n"]), (2, 1, 1, 3))
        self.assertAlmostEqual(c["plain"], 102)
        # HELD shorts on both outcomes took in 98 against one dollar owed a
        # share whoever wins: 2 at worst (a resting order would get no such
        # credit)
        self.assertAlmostEqual(c["netted"], 2)
        self.assertEqual(c["mr"], 61.5)

    def test_the_report_carries_both_and_keeps_its_margin_checks(self):
        fam, tf, tc = make()
        for slug, name in (("ushrewc-tx15-dem", "TX-15 House Election Winner — dem"),
                           ("ushrewc-tx15-rep", "TX-15 House Election Winner — rep")):
            fam.add(slug, T1P, [(0.40, 2000)], [(0.42, 2000)])
            fam.universe[slug]["name"] = name
        tc.margin = lambda: {"inv": [("ushrewc-tx15-dem", -10, -4.0, False)],
                             "row": {"marginRequirement": 6.0}, "feed_n": 1,
                             "names": {"ushrewc-tx15-dem": "TX-15 House Election Winner — dem"}}
        tc.tick(T0)
        t1 = tc.report["tiers"]["t1"]
        self.assertEqual(t1["events"], 1)
        r = t1["risk"]["nearest"]
        self.assertEqual(r["n"], 4)
        self.assertLess(r["netted"], r["plain"])
        self.assertEqual(tc.report["risk_all"]["nearest"], r)
        self.assertEqual(tc.report["margin"]["mr"], 6.0)
        self.assertEqual(len(tc.report["margin_hist"]), 1)
        self.assertNotIn("margin_hist", tc.view())
        # the checks carry across a restart, three days of them at most
        tc2 = TierCent(tf, fam, clock=lambda: T0)
        tc2.restore({"margin_hist": [{"mr": 1.0}] * (MARGIN_KEEP + 5)})
        self.assertEqual(len(tc2.margin_hist), MARGIN_KEEP)
        json.dumps(tc.to_dict())

    def test_a_failing_margin_read_is_said_and_the_report_stands(self):
        fam, tf, tc = make()
        fam.add(A, T1P, [(0.40, 2000)], [(0.42, 2000)])

        def boom():
            raise RuntimeError("no row")
        tc.margin = boom
        tc.tick(T0)
        self.assertTrue(tc.report["ok"])
        self.assertIn("no row", tc.report["margin"]["error"])


class TestTheProgramRecord(unittest.TestCase):
    RAW = {A: {"marketSlug": A, "instrumentState": "INSTRUMENT_STATE_OPEN", "newField": 7,
               "timePeriods": [{"programId": T1P, "programType": "liquidityProgram",
                                "start": "2026-09-24T02:00:00Z", "rewardPool": 1000,
                                "status": "active", "discountFactor": 0.3,
                                "targetSize": 25000, "period": "daily_event",
                                "maxSpread": 0.04}]}}

    def test_every_field_is_kept_per_program_and_the_reading_is_unchanged(self):
        st = TermsStore()
        st.refresh(self.RAW, {A: 2}, now=T0)
        r = st.raw_seen[T1P]
        self.assertEqual(r["period"]["maxSpread"], 0.04)
        self.assertEqual(r["row"]["newField"], 7)
        self.assertIn("maxSpread", r["period_keys"])
        self.assertIn("instrumentState", r["row_keys"])
        self.assertEqual(r["reads"], 1)
        # a field the reader does not use keeps every value it has shown
        self.assertEqual(r["extra"], {"maxSpread": [0.04]})
        st.refresh({A: dict(self.RAW[A], timePeriods=[dict(self.RAW[A]["timePeriods"][0],
                                                             maxSpread=0.03)])},
                   {A: 2}, now=T0 + 60)
        self.assertEqual(st.raw_seen[T1P]["extra"]["maxSpread"], [0.04, 0.03])
        prog = st.get(A)
        self.assertEqual((prog.pool, prog.target, prog.df, prog.pid, prog.event_n),
                         (1000, 25000, 0.3, T1P, 2))
        # a store that never kept the record reads the same
        plain = TermsStore()
        plain.refresh(self.RAW, {A: 2}, now=T0)
        self.assertEqual(plain.get(A), prog)

    def test_the_record_is_not_saved_with_the_ledger(self):
        st = TermsStore()
        st.refresh(self.RAW, {A: 2}, now=T0)
        self.assertNotIn("raw_seen", json.dumps(st.to_dict()))


if __name__ == "__main__":
    unittest.main()
