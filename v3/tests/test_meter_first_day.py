"""A market's first day in a program pays nothing (owner, 2026-09-24
"Fix the meter's first day").

Over the two weeks to 09-21 the rewards endpoint posted no row at all
for markets that joined a reward program partway through an ET day: the
fifteen House district markets on 09-11 (their program began at 19:00Z,
$44.56 estimated) and the eleven Senate combos on 09-17 (read with no
program the evening before, $84.49). Markets already in a program posted
on their first day, 18 of 18, and both groups posted every day after.
The ledger notes the join; the meter counts nothing there until midnight
ET. Trading is untouched.
"""
import datetime as dt
import os
import tempfile
import unittest

from v3.books import BookCache
from v3.estimator import ET, Estimator, et_day
from v3.scoring import Book
from v3.terms import JOIN_EVIDENCE_S, TermsStore, et_day_start

HOUSE = "ushrewc-ushr-tx-34-2026-11-03-dem"
COMBO = "cpoc-ussec-tx-me-2026-11-03-dsweep"


def et(day, hour, minute=0):
    return dt.datetime.strptime(day, "%Y-%m-%d").replace(
        hour=hour, minute=minute, tzinfo=ET).timestamp()


def raw(pid, start, pool=600, target=15000, df=0.25):
    return {"timePeriods": [{"programId": pid, "rewardPool": pool, "targetSize": target,
                             "discountFactor": df, "status": "LIVE", "start": start}]}


def iso(ts):
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestTheLedgerNotesAJoin(unittest.TestCase):
    def test_a_program_that_began_today_is_a_join(self):
        # 09-11: the midterms program began 19:00Z (15:00 ET)
        st = TermsStore()
        seen = et("2026-09-11", 15, 20)
        st.refresh({HOUSE: raw("midterms_t2_20260911", "2026-09-11T19:00:00Z")},
                   {HOUSE: 2}, now=seen)
        self.assertEqual(st.joined_at.get(HOUSE), seen)
        self.assertEqual(list(st.joined_today(seen + 3600)), [HOUSE])
        # the next ET day it counts again
        self.assertEqual(st.joined_today(et("2026-09-12", 9)), {})

    def test_a_read_with_no_program_then_one_is_a_join(self):
        # 09-17: read programless the evening before, then in an older program
        st = TermsStore()
        st.refresh({COMBO: {}}, {COMBO: 4}, now=et("2026-09-16", 18, 40))
        self.assertIsNone(st.get(COMBO))
        seen = et("2026-09-17", 11, 43)
        st.refresh({COMBO: raw("elections_boosted_high_20260827", "2026-08-27T00:00:00Z",
                               pool=1000, target=10000, df=0.2)}, {COMBO: 4}, now=seen)
        self.assertEqual(st.joined_at.get(COMBO), seen)

    def test_an_old_no_program_read_proves_nothing(self):
        st = TermsStore()
        t0 = et("2026-09-10", 12)
        st.refresh({COMBO: {}}, {COMBO: 4}, now=t0)
        st.refresh({COMBO: raw("elections_boosted_high_20260827", "2026-08-27T00:00:00Z")},
                   {COMBO: 4}, now=t0 + JOIN_EVIDENCE_S + 3600)
        self.assertNotIn(COMBO, st.joined_at)

    def test_a_market_never_read_in_an_old_program_counts(self):
        # first read ever, in a program that began weeks ago: benefit of the doubt
        st = TermsStore()
        st.refresh({HOUSE: raw("midterms_t2_20260911", "2026-09-11T19:00:00Z")},
                   {HOUSE: 2}, now=et("2026-09-20", 13))
        self.assertNotIn(HOUSE, st.joined_at)

    def test_a_reissue_is_not_a_join(self):
        # 09-24 02:00Z: every midterms program replaced by a _20260924 one
        st = TermsStore()
        st.refresh({HOUSE: raw("midterms_t2_20260911", "2026-09-11T19:00:00Z")},
                   {HOUSE: 2}, now=et("2026-09-20", 13))
        st.refresh({HOUSE: raw("midterms_t2_20260924", "2026-09-24T02:00:00Z", pool=450)},
                   {HOUSE: 2}, now=et("2026-09-23", 22, 20))
        self.assertNotIn(HOUSE, st.joined_at)

    def test_a_program_that_began_at_midnight_is_a_whole_day(self):
        st = TermsStore()
        mid = et_day_start(et("2026-09-18", 12))
        st.refresh({COMBO: raw("elections_boosted_medium_20260918", iso(mid))},
                   {COMBO: 4}, now=mid + 6 * 3600)
        self.assertNotIn(COMBO, st.joined_at)

    def test_gone_then_back_the_same_day_is_a_join(self):
        st = TermsStore()
        st.refresh({HOUSE: raw("midterms_t2_20260911", "2026-09-11T19:00:00Z")},
                   {HOUSE: 2}, now=et("2026-09-20", 9))
        st.refresh({HOUSE: {}}, {HOUSE: 2}, now=et("2026-09-20", 10))
        back = et("2026-09-20", 14)
        st.refresh({HOUSE: raw("midterms_t2_20260911", "2026-09-11T19:00:00Z")},
                   {HOUSE: 2}, now=back)
        self.assertEqual(st.joined_at.get(HOUSE), back)

    def test_the_tenders_hand_off_notes_the_join_too(self):
        # the family read it empty; the tender's own read found the program
        # and handed it over (the 09-17 combos went this way)
        fam = TermsStore()
        fam.refresh({COMBO: {}}, {COMBO: 4}, now=et("2026-09-16", 18, 40))
        tender = TermsStore()
        seen = et("2026-09-17", 11, 43)
        tender.refresh({COMBO: raw("elections_boosted_high_20260827", "2026-08-27T00:00:00Z")},
                       {COMBO: 4}, now=seen)
        self.assertTrue(fam.adopt(COMBO, tender.get(COMBO), seen))
        self.assertEqual(fam.joined_at.get(COMBO), seen)
        self.assertEqual(fam.seeded_at.get(COMBO), seen)
        self.assertFalse(fam.adopt(COMBO, tender.get(COMBO), seen + 60))   # never over one held

    def test_it_is_saved_and_old_saves_still_load(self):
        st = TermsStore()
        import time
        now = time.time()
        st.refresh({COMBO: {}}, {COMBO: 4}, now=now - 3600)
        st.refresh({COMBO: raw("elections_boosted_high_20260827", "2026-08-27T00:00:00Z")},
                   {COMBO: 4}, now=now)
        back = TermsStore.from_dict(st.to_dict())
        self.assertEqual(back.joined_at, {COMBO: now})
        old = st.to_dict()
        old.pop("joined_at")
        old.pop("empty_at")
        self.assertEqual(TermsStore.from_dict(old).joined_at, {})


class TestTheMeterCountsNothingThere(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        os.environ["V3_STATE_PATH"] = os.path.join(self.dir.name, "s.json")
        os.environ["V3_FLOOR_PATH"] = os.path.join(self.dir.name, "f.json")
        os.environ["GITHUB_TOKEN"] = ""
        os.environ["V3_FLATTEN"] = "0"

    def tearDown(self):
        for k in ("V3_STATE_PATH", "V3_FLOOR_PATH", "V3_FLATTEN"):
            os.environ.pop(k, None)
        self.dir.cleanup()

    @staticmethod
    def _terms():
        """HOUSE in its program since 09-11; COMBO read empty the evening
        of 09-16 and in a program at 11:43 ET on 09-17. Targets under the
        book's size, so each side qualifies."""
        st = TermsStore()
        st.refresh({HOUSE: raw("midterms_t2_20260911", "2026-09-11T19:00:00Z", target=5000)},
                   {HOUSE: 2}, now=et("2026-09-15", 9))
        st.refresh({COMBO: {}}, {COMBO: 4}, now=et("2026-09-16", 18, 40))
        st.refresh({COMBO: raw("elections_boosted_high_20260827", "2026-08-27T00:00:00Z",
                               pool=1000, target=5000, df=0.2)},
                   {COMBO: 4}, now=et("2026-09-17", 11, 43))
        return st

    def _sample_day(self, st, t0):
        """Two markets, one order each, sampled a minute apart."""
        e = Estimator()
        orders = [{"market": HOUSE, "side": "BUY", "price": 0.40, "size": 6000.0},
                  {"market": COMBO, "side": "BUY", "price": 0.40, "size": 6000.0}]

        def books(now):
            c = BookCache()
            for m in (HOUSE, COMBO):
                c.put(m, Book(bids=((0.40, 6000.0),), asks=((0.42, 50.0),),
                              tick=0.01, fetched_at=now))
            return c
        from v3.main import Monitor
        mon = Monitor()
        fam = mon.families["politics"]
        fam.terms = st
        for i in range(4):
            now = t0 + 20.0 * i
            skip = mon._first_day("politics", fam, now)
            e.sample(now, [o for o in orders if o["market"] not in skip], books(now), st,
                     side_pool=lambda s, p: p.pool / 2.0, verified_at=now)
        return e, fam

    def test_a_first_day_market_accrues_nothing_the_other_does(self):
        e, fam = self._sample_day(self._terms(), et("2026-09-17", 14))
        self.assertGreater(e.per_market.get(HOUSE, 0.0), 0.0)
        self.assertEqual(e.per_market.get(COMBO, 0.0), 0.0)
        notes = [x for x in fam.log if x.get("event") == "meter_first_day"]
        self.assertEqual([x["market"] for x in notes], [COMBO])       # said once, not per tick

    def test_the_next_day_it_counts(self):
        e, _ = self._sample_day(self._terms(), et("2026-09-18", 10))
        self.assertEqual(e.day, "2026-09-18")
        self.assertGreater(e.per_market.get(COMBO, 0.0), 0.0)

    def test_the_state_says_which_markets(self):
        from v3.main import Monitor
        mon = Monitor()
        import time
        now = time.time()
        st = mon.families["politics"].terms
        st.refresh({COMBO: {}}, {COMBO: 4}, now=now - 600)
        st.refresh({COMBO: raw("elections_boosted_high_20260827", "2026-08-27T00:00:00Z")},
                   {COMBO: 4}, now=now)
        view = mon._first_day_view(now)
        self.assertEqual(list(view.get("politics", {})), [COMBO])


class TestTheDayStart(unittest.TestCase):
    def test_midnight_eastern(self):
        t = et("2026-09-17", 11, 43)
        self.assertEqual(et_day(et_day_start(t)), "2026-09-17")
        self.assertEqual(et_day(et_day_start(t) - 1), "2026-09-16")


if __name__ == "__main__":
    unittest.main()
