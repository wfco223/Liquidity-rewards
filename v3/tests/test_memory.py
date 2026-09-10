"""Memory (owner, 2026-09-02, "Can't you slim it down without me going
to a higher tier?").

The DigitalOcean graph: flat at 33% of the 1 GB box all day, a step to
57% when discovery and the survey frame refetched in the same minute
on the boot+6h clock, a spike to 90% at the hourly publish, then the
kill — and every boot since replaying the peak in its own second
cycle. Nothing leaked: the state file is 57 MB in Python. The survey
frame's 28,081 raw rows (~4.5 KB each, twice over while refetching)
and discovery's whole-feed lists were the peaks. So: rows slimmed as
their page arrives, the feed streamed a page at a time, the two clocks
three hours apart, and the number stated every cycle.
"""

import calendar
import sys
import unittest

from v3 import football, politics
from v3.api import Client, events_of
from v3.main import mem_limit_mb, rss_mb
from v3.programs import pick_period, program_from_period
from v3.survey import (PERIOD_KEEP, ROW_KEEP, category_banned, compact_row,
                       group_of, is_live_event)

RAW = {"marketSlug": "aachc-cfb-wins-2026-11-28-txst-5pt5wins",
       "marketId": "0x1234567890abcdef1234567890abcdef",
       "programType": "liquidityProgram",
       "instrumentState": "INSTRUMENT_STATE_ACTIVE",
       "category": "sports", "subcategory": "cfb",
       "instrumentProduct": "PRODUCT_TYPE_BINARY",
       "eventStartTime": "2026-11-28T00:00:00Z",
       "eventSlug": "cfb-wins-2026-txst",
       "eventTitle": "Texas State regular season wins 2026",
       "question": "Will Texas State win more than 5.5 games in 2026?",
       "status": "active", "createdAt": "2026-08-06T19:00:00Z",
       "updatedAt": "2026-09-01T10:00:00Z",
       "timePeriods": [{"programId": "cfb_futures_20260806",
                        "rewardPool": "75", "targetSize": "7500",
                        "discountFactor": "0.45", "status": "LIVE",
                        "start": "2026-08-06T19:00:00Z", "end": "",
                        "rewardCurrency": "USD", "periodType": "daily"}],
       "outcomes": ["Yes", "No"], "tags": ["sports", "cfb", "futures"],
       "volume24h": "1234.56", "openInterest": "5678",
       "lastTradePx": "0.42", "tickSize": "0.01"}


def deep(o, seen=None):
    seen = set() if seen is None else seen
    if id(o) in seen:
        return 0
    seen.add(id(o))
    n = sys.getsizeof(o)
    if isinstance(o, dict):
        n += sum(deep(k, seen) + deep(v, seen) for k, v in o.items())
    elif isinstance(o, (list, tuple, set)):
        n += sum(deep(x, seen) for x in o)
    return n


class TestCompactRow(unittest.TestCase):
    def test_the_survey_reads_the_same_answers_off_the_slim_row(self):
        slim = compact_row(RAW)
        self.assertEqual(group_of(slim), group_of(RAW))
        self.assertEqual(category_banned(slim), category_banned(RAW))
        now = calendar.timegm((2026, 11, 28, 12, 0, 0, 0, 0, 0))
        self.assertEqual(is_live_event(slim, now), is_live_event(RAW, now))
        self.assertTrue(is_live_event(slim, now))
        pr, ps = (program_from_period(pick_period(r["timePeriods"],
                                                  r["marketSlug"]))
                  for r in (RAW, slim))
        self.assertEqual(pr, ps)
        self.assertEqual(ps.pool, 75.0)

    def test_the_slim_row_keeps_only_the_named_fields(self):
        slim = compact_row(RAW)
        self.assertLessEqual(set(slim) - {"timePeriods"}, set(ROW_KEEP))
        self.assertLessEqual(set(slim["timePeriods"][0]), set(PERIOD_KEEP))
        self.assertNotIn("question", slim)
        self.assertNotIn("rewardCurrency", slim["timePeriods"][0])
        # a blank end date is dropped, not carried as ""
        self.assertNotIn("end", slim["timePeriods"][0])
        self.assertEqual(slim["timePeriods"][0]["rewardPool"], 75.0)

    def test_a_frame_of_slim_rows_is_at_least_three_times_smaller(self):
        # the win is ACROSS rows: labels shared, numbers as floats. One
        # row alone barely halves; three hundred with the exchange's
        # handful of categories fall by more than three
        import json
        # through JSON, as the feed arrives: every row owns its own
        # value strings (a dict() copy would share them and hide the cost)
        raw = [json.loads(json.dumps(
                   dict(RAW, marketSlug=f"{RAW['marketSlug']}-{i}",
                        category=("sports", "politics", "culture")[i % 3])))
               for i in range(300)]
        slim = [compact_row(r) for r in raw]
        self.assertLess(deep(slim) * 3, deep(raw))

    def test_a_row_with_no_periods_is_still_a_row(self):
        slim = compact_row({"marketSlug": "x", "category": None})
        self.assertEqual(slim, {"marketSlug": "x"})


class PagedIncentives(Client):
    """Two pages of the incentives feed, no network."""

    def __init__(self):
        super().__init__(key_id="k", secret_key="s", sleep=lambda s: None)
        self.pages = {None: {"programs": [RAW], "nextPageToken": "p2"},
                      "p2": {"programs": [dict(RAW, marketSlug="other")]}}

    def get(self, url, **kw):
        return self.pages[(kw.get("params") or {}).get("page_token")]


class TestCompactAsPagesArrive(unittest.TestCase):
    def test_rows_are_slimmed_page_by_page(self):
        rows, note = PagedIncentives().all_programs(page_size=500,
                                                    compact=compact_row)
        self.assertEqual(note, "enumerated")
        self.assertEqual([r["marketSlug"] for r in rows],
                         [RAW["marketSlug"], "other"])
        self.assertTrue(all("question" not in r for r in rows))

    def test_without_a_compactor_rows_come_back_whole(self):
        rows, _ = PagedIncentives().all_programs(page_size=500)
        self.assertIn("question", rows[0])


class Streaming:
    """A client that can only stream — events_by_tag would explode."""

    def __init__(self, pages, fail_after=None):
        self.pages, self.fail_after = pages, fail_after

    def iter_events(self, tag, max_pages=30):
        n = 0
        for page in self.pages.get(tag, []):
            for ev in page:
                if self.fail_after is not None and n >= self.fail_after:
                    raise RuntimeError("feed died")
                n += 1
                yield ev

    def events_by_tag(self, tag, max_pages=30):
        raise AssertionError("discovery must stream, not load the list")


def ev(title, *slugs):
    return {"title": title, "markets": [{"slug": s, "question": s}
                                        for s in slugs]}


class TestStreamedDiscovery(unittest.TestCase):
    def test_politics_discovery_walks_the_feed_a_page_at_a_time(self):
        c = Streaming({"politics": [[ev("GA", "vmc-ussemov-ga-2026-11-03-d4-7",
                                            "vmc-ussemov-ga-2026-11-03-r0-3")],
                                    [ev("X", "usacpi-2026-09-0")]],
                       "elections": []})
        out = politics.discover(c)
        self.assertEqual(set(out), {"vmc-ussemov-ga-2026-11-03-d4-7",
                                    "vmc-ussemov-ga-2026-11-03-r0-3"})
        self.assertEqual(out["vmc-ussemov-ga-2026-11-03-d4-7"]["event_n"], 2)

    def test_football_discovery_streams_too(self):
        c = Streaming({"football": [[ev("wins", "aachc-cfb-wins-2026-11-28-txst-5pt5wins")]]})
        self.assertIn("aachc-cfb-wins-2026-11-28-txst-5pt5wins",
                      football.cfb_discover(c))

    def test_a_fake_with_only_the_list_still_works(self):
        class Listy:
            def events_by_tag(self, tag, max_pages=30):
                return [ev("GA", "vmc-ussemov-ga-2026-11-03-d4-7")]
        self.assertEqual(list(events_of(Listy(), "politics")),
                         [ev("GA", "vmc-ussemov-ga-2026-11-03-d4-7")])
        self.assertIn("vmc-ussemov-ga-2026-11-03-d4-7", politics.discover(Listy()))

    def test_an_unknown_tag_is_skipped_but_a_feed_dying_midway_raises(self):
        # first page fails: the tag is skipped, the other still counts
        class FirstPageDies(Streaming):
            def iter_events(self, tag, max_pages=30):
                if tag == "politics":
                    raise RuntimeError("404")
                return super().iter_events(tag, max_pages)
        c = FirstPageDies({"elections": [[ev("GA", "vmc-ussemov-ga-2026-11-03-d4-7")]]})
        self.assertIn("vmc-ussemov-ga-2026-11-03-d4-7", politics.discover(c))
        # died after handing out events: no partial universe — it
        # raises, and refresh_universe keeps the old one (the 09-01
        # list collapse must not repeat)
        c2 = Streaming({"politics": [[ev("GA", "vmc-ussemov-ga-2026-11-03-d4-7"),
                                      ev("MA", "vmc-ussemov-ma-2026-11-03-d4-7")]]},
                       fail_after=1)
        with self.assertRaises(RuntimeError):
            politics.discover(c2)


class TestMemoryLine(unittest.TestCase):
    def test_the_number_can_be_stated(self):
        self.assertGreater(rss_mb(), 1.0)
        lim = mem_limit_mb()
        self.assertTrue(lim is None or lim > 0)


if __name__ == "__main__":
    unittest.main()
