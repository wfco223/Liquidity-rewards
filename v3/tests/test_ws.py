"""The book stream writes the same cache the REST rotation does."""

import json
import unittest

from v3 import ws
from v3.books import BookCache, ws_priority
from v3.ws import Stream


class TestStream(unittest.TestCase):
    def test_frame_lands_in_the_cache_normalized(self):
        cache = BookCache()
        s = Stream(cache, lambda: ["m-1"], "k", "c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0c2Vjcg==")
        frame = json.dumps({"marketData": {
            "marketSlug": "m-1",
            "bids": [{"px": {"value": "0.44"}, "qty": 20},
                     {"px": {"value": "0.02"}, "qty": 60000}],
            "offers": [{"px": {"value": "0.47"}, "qty": 20}],
        }})
        self.assertEqual(s.apply_frame(frame), "m-1")
        b = cache.any_age("m-1")
        self.assertEqual(b.bids[0], (0.44, 20.0))
        self.assertEqual(b.asks[0], (0.47, 20.0))
        self.assertEqual(s.apply_frame("not json"), None)   # never kills the socket
        self.assertEqual(s.apply_frame(json.dumps({"x": 1})), None)

    def test_frame_shape_sampler_records_what_arrives(self):
        # owner yes, 2026-08-28: the health line shows frames arriving
        # with zero book writes — record every distinct frame shape
        # with a count and one truncated sample, so apply_frame gets
        # fixed from evidence instead of guesses.
        cache = BookCache()
        s = Stream(cache, lambda: ["m-1"], "k", "c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0c2Vjcg==")
        s.apply_frame(json.dumps({"marketDataLite": {
            "marketSlug": "m-1", "bestBid": {"value": "0.44"}}}))
        s.apply_frame(json.dumps({"marketDataLite": {
            "marketSlug": "m-1", "bestBid": {"value": "0.45"}}}))
        s.apply_frame(json.dumps({"mysteryBook": {"slug": "m-1",
                                                  "levels": []}}))
        s.apply_frame("not json")                    # never sampled, never fatal
        sigs = list(s.frame_shapes)
        self.assertEqual(len(sigs), 2)
        lite = next(k for k in sigs if "marketDataLite" in k)
        self.assertEqual(s.frame_shapes[lite]["n"], 2)
        odd = next(k for k in sigs if "mysteryBook" in k)
        self.assertIn("mysteryBook", odd)
        self.assertIn("levels", odd)                 # nested keys in the signature
        self.assertIn("mysteryBook", s.frame_shapes[odd]["sample"])

    def test_trade_prints_from_the_lite_feed(self):
        # owner, 2026-08-29: shape churn is blind to take-and-refill —
        # a lastTradePx or openInterest CHANGE is the real print. The
        # first frame is a baseline, never a print.
        cache = BookCache()
        s = Stream(cache, lambda: ["m-1"], "k", "c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0c2Vjcg==")
        def lite(ltp, oi):
            return json.dumps({"marketDataLite": {
                "marketSlug": "m-1", "bestBid": {"value": "0.44"},
                "lastTradePx": {"value": str(ltp)}, "openInterest": oi}})
        s.apply_frame(lite(0.45, 100))        # baseline
        self.assertNotIn("m-1", cache.trade_seen)
        s.apply_frame(lite(0.45, 100))        # unchanged: no print
        self.assertNotIn("m-1", cache.trade_seen)
        s.apply_frame(lite(0.46, 100))        # price moved: print
        self.assertEqual(len(cache.trade_seen["m-1"]), 1)
        s.apply_frame(lite(0.46, 103))        # OI moved: print
        self.assertEqual(len(cache.trade_seen["m-1"]), 2)
        for _ in range(12):                   # ring stays small
            s.apply_frame(lite(0.46, 103))
        self.assertEqual(len(cache.trade_seen["m-1"]), 2)

    def test_priority_puts_held_markets_first_under_the_cap(self):
        uni = [f"m-{i}" for i in range(300)]
        out = ws_priority(["m-250"], ["m-299"], uni)
        self.assertEqual(out[0], "m-250")
        self.assertEqual(out[1], "m-299")
        self.assertEqual(len(out), 200)

class TestTheStreamShards(unittest.TestCase):
    """Owner, 2026-09-14 ("Do all three for the 429s"). The exchange caps
    a SUBSCRIPTION at 200 markets, so a second connection buys a second
    200. On 2026-09-14, 335 markets wanted a live book against 200 seats;
    the 135 without one were read through the gateway every pass, 35 of
    those reads answered 429 in 10.8 minutes, and because a 429 holds
    EVERY gateway read the gateway was shut 314 s of a 645 s window —
    which is why the tender's pass line read "0 books read, 72 due"."""

    def slugs(self, n):
        return [f"m{i:04d}" for i in range(n)]

    def stream(self, shard, n):
        return ws.Stream(BookCache(), lambda: self.slugs(n), "k", "s",
                         shard=shard, shards=ws.STREAM_SHARDS)

    def test_each_shard_takes_its_own_slice_and_they_do_not_overlap(self):
        n = ws.SUB_CAP * ws.STREAM_SHARDS
        seen = [self.stream(i, n)._my_slugs() for i in range(ws.STREAM_SHARDS)]
        for got in seen:
            self.assertEqual(len(got), ws.SUB_CAP)
        flat = [s for got in seen for s in got]
        self.assertEqual(len(set(flat)), len(flat))      # no market twice
        self.assertEqual(set(flat), set(self.slugs(n)))  # and none left out

    def test_the_best_markets_still_land_on_the_first_connection(self):
        # the list is priority-ordered once, by ws_priority; the shards
        # only cut it, so a shard dying costs its own slice and no more
        first = self.stream(0, 400)._my_slugs()
        self.assertEqual(first, self.slugs(400)[:ws.SUB_CAP])

    def test_a_short_list_leaves_the_later_shards_empty_not_broken(self):
        self.assertEqual(self.stream(1, 50)._my_slugs(), [])
        self.assertEqual(len(self.stream(0, 50)._my_slugs()), 50)

    def test_the_seats_now_hold_every_market_that_wanted_a_book(self):
        # 335 on 2026-09-14, against 200 before
        self.assertGreaterEqual(ws.SUB_CAP * ws.STREAM_SHARDS, 335)


SECRET = "c2VjcmV0c2VjcmV0c2VjcmV0c2VjcmV0c2Vjcg=="


class TestTheTierFourMonitorsStream(unittest.TestCase):
    """Owner, 2026-09-25 ("focus on getting the tier 4 markets identified
    and monitored both $5 and $2"): ~5,800 markets on connections of
    their own. The exchange's docs cap a subscription at 100 markets and
    say to use more subscriptions for more, so a slice goes in requests
    of `chunk`; a request the exchange refuses is recorded in its words."""

    def slugs(self, n):
        return [f"m-{i:04d}" for i in range(n)]

    def test_the_existing_connections_send_exactly_what_they_did(self):
        s = Stream(BookCache(), lambda: self.slugs(150), "k", SECRET)
        reqs = s._requests(s._my_slugs())
        self.assertEqual([r["subscribe"]["requestId"] for r in reqs], ["books", "lite"])
        self.assertEqual(len(reqs[0]["subscribe"]["marketSlugs"]), 150)
        self.assertNotIn("responsesDebounced", reqs[0]["subscribe"])

    def test_a_big_slice_goes_in_requests_of_a_hundred(self):
        all_ = self.slugs(5838)
        s0 = Stream(BookCache(), lambda: all_, "k", SECRET, shard=0, shards=2,
                    cap=3000, chunk=100, debounce=True)
        s1 = Stream(BookCache(), lambda: all_, "k", SECRET, shard=1, shards=2,
                    cap=3000, chunk=100, debounce=True)
        a, b = s0._my_slugs(), s1._my_slugs()
        self.assertEqual((len(a), len(b)), (3000, 2838))
        self.assertFalse(set(a) & set(b))
        reqs = s1._requests(b)
        books = [r["subscribe"] for r in reqs
                 if r["subscribe"]["subscriptionType"] == "SUBSCRIPTION_TYPE_MARKET_DATA"]
        lite = [r["subscribe"] for r in reqs
                if r["subscribe"]["subscriptionType"] == "SUBSCRIPTION_TYPE_MARKET_DATA_LITE"]
        self.assertEqual((len(books), len(lite)), (29, 29))
        self.assertTrue(all(len(x["marketSlugs"]) <= 100 for x in books + lite))
        self.assertEqual(sum(len(x["marketSlugs"]) for x in books), 2838)
        self.assertTrue(all(x["responsesDebounced"] for x in books + lite))
        self.assertEqual(len({x["requestId"] for x in books + lite}), 58)

    def test_a_refused_request_is_recorded_in_the_exchanges_words(self):
        s = Stream(BookCache(), lambda: self.slugs(250), "k", SECRET, cap=3000, chunk=100)
        s._requests(s._my_slugs())
        self.assertIsNone(s.apply_frame(json.dumps(
            {"requestId": "books-0-2", "error": "too many subscriptions"})))
        self.assertEqual(s.status["refused"], 50)
        self.assertEqual(s.status["refused_requests"], 1)
        self.assertIn("too many subscriptions", s.status["note"])

    def test_it_writes_its_own_store(self):
        mine, other = BookCache(), BookCache()
        s = Stream(mine, lambda: ["m-1"], "k", SECRET, cap=3000, chunk=100)
        s.apply_frame(json.dumps({"marketData": {
            "marketSlug": "m-1", "bids": [{"px": {"value": "0.20"}, "qty": 3000}],
            "offers": [{"px": {"value": "0.24"}, "qty": 3000}]}}))
        self.assertIsNotNone(mine.any_age("m-1"))
        self.assertIsNone(other.any_age("m-1"))

    def test_it_keeps_the_last_trade_of_every_market_it_carries(self):
        s = Stream(BookCache(), lambda: [], "k", SECRET, cap=3000, chunk=100, keep=6000)

        def lite(slug, ltp):
            return json.dumps({"marketDataLite": {"marketSlug": slug,
                                                  "lastTradePx": {"value": str(ltp)},
                                                  "openInterest": "10"}})
        for i in range(3000):
            s.apply_frame(lite(f"m-{i}", 0.30))
        for i in range(3000):
            s.apply_frame(lite(f"m-{i}", 0.31))
        self.assertTrue(s.last_trade["m-0"][2])      # the change was seen as a print
        self.assertEqual(len(s.last_trade), 3000)
