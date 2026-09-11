"""Tests for the one earned-today number: integration, quorum, day roll."""

import datetime as dt
import unittest

from v3.books import BookCache
def _sp(m, prog):
    return (prog.pool or 0.0) / max(prog.event_n or 1, 1) / 2.0


from v3.estimator import ET, Estimator, et_day, top_up_book
from v3.scoring import Book
from v3.terms import TermsStore

SLUG = "scc-senate-gop-2026-11-03-50"


def terms_with(pool=100, target=5000, df=0.2, event_n=13):
    st = TermsStore()
    st.refresh({SLUG: {"timePeriods": [{
        "programId": "politics_mid_1", "rewardPool": pool, "targetSize": target,
        "discountFactor": df, "status": "LIVE"}]}}, {SLUG: event_n}, now=1.0)
    return st


def one_order(price=0.17, size=6000.0):
    return [{"market": SLUG, "side": "BUY", "price": price, "size": size}]


def books_at(now, bid_qty=6000.0):
    c = BookCache()
    c.put(SLUG, Book(bids=((0.17, bid_qty),), asks=((0.19, 50.0),),
                     tick=0.01, fetched_at=now))
    return c


def noon_et(day="2026-08-18"):
    d = dt.datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=ET)
    return d.timestamp()


class TestIntegration(unittest.TestCase):
    def test_rate_integrates_over_elapsed_time(self):
        # Alone in the window: 100% share of $100/13/2 = $3.846/day.
        e, st = Estimator(), terms_with()
        t0 = noon_et()
        e.sample(t0, one_order(), books_at(t0), st, side_pool=_sp)
        self.assertAlmostEqual(e.rate, 100 / 13 / 2, places=3)
        self.assertEqual(e.earned, 0.0)          # nothing elapsed yet
        e.sample(t0 + 100, one_order(), books_at(t0 + 100), st, side_pool=_sp)
        self.assertAlmostEqual(e.earned, e.rate * 100 / 86400, places=6)
        self.assertAlmostEqual(e.per_market[SLUG], e.earned, places=6)

    def test_a_blackout_bills_nothing_and_reads_zero_on_the_graph(self):
        # owner, 2026-09-11, the graph had plateaued across the exchange's
        # maintenance: "Make the earning estimate during the period of
        # maintenance 0"
        e, st = Estimator(), terms_with()
        t0 = noon_et()
        e.sample(t0, one_order(), books_at(t0), st, side_pool=_sp)
        e.sample(t0 + 5000, one_order(), books_at(t0 + 5000), st, side_pool=_sp)  # 83 min gap
        self.assertEqual(e.earned, 0.0)
        self.assertAlmostEqual(e.stale_s, 5000.0)
        zeros = [d for d in e.dots if d[1] == 0.0]
        self.assertEqual([round(d[0] - t0) for d in zeros], [20, 4980])
        self.assertEqual([round(d[0] - t0) for d in e.dots], [0, 20, 4980, 5000])
        # a short gap still bills at the rate that was in force
        e.sample(t0 + 5100, one_order(), books_at(t0 + 5100), st, side_pool=_sp)
        self.assertAlmostEqual(e.earned, e.rate * 100 / 86400, places=6)

    def test_stale_books_bank_time_instead_of_accruing(self):
        e, st = Estimator(), terms_with()
        t0 = noon_et()
        stale = books_at(t0 - 1000)              # far older than BOOK_MAX_AGE
        e.sample(t0, one_order(), stale, st, side_pool=_sp)
        e.sample(t0 + 100, one_order(), stale, st, side_pool=_sp)
        self.assertEqual(e.earned, 0.0)
        self.assertAlmostEqual(e.stale_s, 100.0)
        self.assertEqual(e.covered_s, 0.0)

    def test_closed_program_earns_nothing(self):
        st = TermsStore()
        st.refresh({SLUG: {"timePeriods": [{
            "programId": "politics_mid_1", "rewardPool": 100, "targetSize": 5000,
            "discountFactor": 0.2, "status": "STATUS_CLOSED"}]}}, {SLUG: 13}, now=1.0)
        e = Estimator()
        t0 = noon_et()
        e.sample(t0, one_order(), books_at(t0), st, side_pool=_sp)
        self.assertEqual(e.rate, 0.0)


class TestDayRoll(unittest.TestCase):
    def test_day_closes_at_midnight_eastern(self):
        e, st = Estimator(), terms_with()
        # accrue on the evening of the 18th ET
        d = dt.datetime(2026, 8, 18, 23, 50, tzinfo=ET).timestamp()
        e.sample(d, one_order(), books_at(d), st, side_pool=_sp)
        e.sample(d + 240, one_order(), books_at(d + 240), st, side_pool=_sp)
        earned_before = e.earned
        self.assertGreater(earned_before, 0.0)
        # first sample past midnight: the 18th closes into history; stale
        # books on this sample so the new day starts with zero accrual
        roll = dt.datetime(2026, 8, 19, 0, 1, tzinfo=ET).timestamp()
        e.sample(roll, one_order(), books_at(roll - 1000), st, side_pool=_sp)
        self.assertEqual(e.day, "2026-08-19")
        self.assertEqual(len(e.history), 1)
        self.assertEqual(e.history[0]["day"], "2026-08-18")
        self.assertAlmostEqual(e.history[0]["earned"], round(earned_before, 4))
        self.assertEqual(e.earned, 0.0)

    def test_et_day_is_the_reward_day(self):
        # 03:00 UTC on the 19th is still 23:00 ET on the 18th (EDT)
        t = dt.datetime(2026, 8, 19, 3, 0, tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(et_day(t), "2026-08-18")


class TestTopUp(unittest.TestCase):
    def test_book_missing_our_order_gets_topped_up(self):
        # The book predates our 100-share bid at 18c: scoring it raw would
        # call us 1 tick off best. Topped up, we ARE the best.
        b = Book(bids=((0.17, 50.0),), asks=((0.19, 10.0),), tick=0.01, fetched_at=5.0)
        t = top_up_book(b, [{"side": "BUY", "price": 0.18, "size": 100.0}])
        self.assertEqual(t.bids[0], (0.18, 100.0))
        self.assertEqual(t.bids[1], (0.17, 50.0))
        self.assertEqual(t.fetched_at, 5.0)

    def test_book_already_containing_us_is_untouched(self):
        b = Book(bids=((0.17, 500.0),), asks=(), tick=0.01, fetched_at=5.0)
        t = top_up_book(b, [{"side": "BUY", "price": 0.17, "size": 100.0}])
        self.assertIs(t, b)


class TestPersistence(unittest.TestCase):
    def test_roundtrip_keeps_the_running_day(self):
        e, st = Estimator(), terms_with()
        t0 = noon_et()
        e.sample(t0, one_order(), books_at(t0), st, side_pool=_sp)
        e.sample(t0 + 100, one_order(), books_at(t0 + 100), st, side_pool=_sp)
        e2 = Estimator.from_dict(e.to_dict())
        self.assertEqual(e2.day, e.day)
        self.assertAlmostEqual(e2.earned, round(e.earned, 4), places=4)
        e2.sample(t0 + 200, one_order(), books_at(t0 + 200), st, side_pool=_sp)
        self.assertGreater(e2.earned, e.earned - 1e-9)


if __name__ == "__main__":
    unittest.main()
