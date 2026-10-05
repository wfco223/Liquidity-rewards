"""The markets list, the scan and his tracked markets (owner, 2026-10-05:
sorts by what the bid and the ask earn and earn per dollar, a sort by
market type, the rest of every event he is in, a scan of the politics
programs whose results he can track). All of it reads; none of it may
place, move or cancel an order."""

import http.client
import json
import os
import threading
import time
import unittest
from unittest import mock

from v3.scoring import Book

from lite.app import STAKE_DEFAULT, STAKE_MAX, basis, classify, discover
from lite.scan import group_key, group_label
from lite.web import serve

from lite.tests.test_lite import M, M2, FakeStore, make, prog, raw_order

DEM = M[:-3] + "dem"          # the other side of M's race
EV = "usse-ok-2026-11-03"


def listed(app, uni):
    """Markets listed the way a full discovery lists them, just now."""
    app.universe = dict(uni)
    app.universe_at = time.time()
    app._rebuild_members()


def book(bids, asks, tick=0.01):
    return Book(bids=tuple(bids), asks=tuple(asks), tick=tick, fetched_at=time.time())


def setup_event(app, c):
    """M and its other side in one event, discovery's way."""
    listed(app, {M: EV, DEM: EV, M2: "usse-ks-2026-11-03"})
    app.event_n[DEM] = 2
    c.progs[DEM] = prog()
    c.books[DEM] = book([(0.55, 2000.0)], [(0.58, 1500.0)])
    app.cache.put(DEM, c.book(DEM))
    app.refresh_terms(time.time(), [M, DEM])


class TestClassify(unittest.TestCase):
    def test_the_chamber_and_the_state_come_from_the_slug(self):
        cases = {
            "ushrewc-ushr-ca-45-2026-11-03-rep": ("House", "California"),
            "ussewc-usse-al-2026-11-03-dem": ("Senate", "Alabama"),
            "ewc-usse-tx-2026-11-03-dem": ("Senate", "Texas"),
            "usgubewc-usgub-ak-2026-11-03-adacru": ("Governor", "Alaska"),
            "vmc-usgubp-mov-ok-rep-2026-08-25-dru0-5": ("Governor", "Oklahoma"),
            "ushsscc-ushrsc-al-2026-11-03-0": ("House", "Alabama"),
            "cpoc-ussec-tx-me-2026-11-03-dsweep": ("Senate", "Texas"),
            "scc-hrep-rep-2026-11-03-gte190": ("House", ""),
            "scc-senate-gop-2026-11-03-46": ("Senate", ""),
            "paccc-usho-midterms-2026-11-03-dem": ("House", ""),
            "paccc-usse-midterms-2026-11-03-dem": ("Senate", ""),
            "cmovcuss-usseclose-2026-11-03-ak": ("Senate", ""),   # the outcome, not a state
            "usgovcc-26mid-rep-2026-11-03-20-21": ("Governor", ""),
            "paccc-balpow-2026-11-03-dsweep": ("Other", ""),
            "ewc-usp-2028-11-07-dontru": ("Other", ""),
        }
        for slug, want in cases.items():
            self.assertEqual(classify(slug), want, slug)


class TestBasis(unittest.TestCase):
    def test_yes_at_its_price_no_at_one_less(self):
        self.assertAlmostEqual(basis("ORDER_INTENT_BUY_LONG", 0.4, 100), 40.0)
        self.assertAlmostEqual(basis("ORDER_INTENT_SELL_LONG", 0.4, 100), 40.0)
        self.assertAlmostEqual(basis("ORDER_INTENT_BUY_SHORT", 0.4, 100), 60.0)
        self.assertAlmostEqual(basis("ORDER_INTENT_SELL_SHORT", 0.4, 100), 60.0)


class TestNewOrderFigures(unittest.TestCase):
    def test_a_new_order_joins_the_best_price_with_the_stake(self):
        app, c = make()
        b = c.book(M)
        p = app.terms.get(M)
        pool = app.side_pool(M, p)
        n = app.potential(M, "BUY", b, p, pool, 40.0)
        self.assertEqual(n["px"], 0.40)
        self.assertEqual(n["qty"], 100.0)              # $40 at 40c
        self.assertAlmostEqual(n["cost"], 40.0)
        # the meter's arithmetic: 100 of 900+600*0.5+100 in the window
        # the 0.40 level alone (900 + his 100) fills the 1,000 Target Size window
        self.assertAlmostEqual(n["day"], pool * 100 / 1000, places=3)
        self.assertAlmostEqual(n["pct"], n["day"] / 40.0)
        a = app.potential(M, "SELL", b, p, pool, 40.0)
        self.assertEqual(a["px"], 0.42)
        self.assertEqual(a["qty"], 68.0)               # an ask ties up 58c a share
        self.assertAlmostEqual(a["cost"], 68 * 0.58, places=2)

    def test_a_side_short_of_its_target_pays_nothing(self):
        app, c = make()
        p = app.terms.get(M)
        b = book([(0.40, 100.0)], [(0.42, 100.0)])
        n = app.potential(M, "BUY", b, p, app.side_pool(M, p), 10.0)
        self.assertEqual(n["day"], 0.0)
        self.assertIn("Target Size", n["why"])
        # a stake big enough to carry the side over the target is paid
        n = app.potential(M, "BUY", b, p, app.side_pool(M, p), 400.0)
        self.assertGreater(n["day"], 0)

    def test_no_dollar_figure_without_the_event_size_or_on_a_first_day(self):
        app, c = make()
        p = app.terms.get(M)
        n = app.potential(M, "BUY", c.book(M), p, None, 40.0)
        self.assertIsNone(n["day"])
        n = app.potential(M, "BUY", c.book(M), p, 5.0, 40.0, first=True)
        self.assertEqual(n["day"], 0.0)
        self.assertEqual(app.potential(M, "BUY", None, p, 5.0, 40.0)["why"], "no book")

    def test_the_stake_is_bounded(self):
        app, _ = make()
        self.assertEqual(app.stake_of(None), STAKE_DEFAULT)
        self.assertEqual(app.stake_of("abc"), STAKE_DEFAULT)
        self.assertEqual(app.stake_of(-5), 1.0)
        self.assertEqual(app.stake_of(1e9), STAKE_MAX)
        self.assertEqual(app.stake_of(float("nan")), STAKE_DEFAULT)


class TestTheMarketsList(unittest.TestCase):
    def setUp(self):
        self.app, self.c = make()
        setup_event(self.app, self.c)
        self.c.raw = [raw_order("O1", M, "BUY", 0.40, 100, "ORDER_INTENT_BUY_LONG")]
        self.app.cache.put(M, self.c.book(M))
        self.app.sample_once()

    def row(self, slug, d=None):
        d = d or self.app.data(stake=40)
        return next(r for r in d["markets"] if r["m"] == slug)

    def test_his_side_shows_what_it_earns_and_per_dollar(self):
        r = self.row(M)
        self.assertTrue(r["has"])
        self.assertEqual(r["why"], "order")
        bid = r["bid"]
        self.assertAlmostEqual(bid["day"], self.app.order_est["O1"])
        self.assertAlmostEqual(bid["basis"], 40.0)
        self.assertAlmostEqual(bid["pct"], bid["day"] / 40.0)
        self.assertNotIn("new", bid)
        # the ask, where he has no order, says what a new one would earn
        self.assertEqual(r["ask"]["new"]["px"], 0.42)
        self.assertEqual((r["kind"], r["st"]), ("Senate", "Oklahoma"))

    def test_the_rest_of_his_event_is_listed(self):
        r = self.row(DEM)
        self.assertFalse(r["has"])
        self.assertEqual(r["why"], "event")
        self.assertEqual(r["bid"]["new"]["px"], 0.55)
        # a market of another event is not
        self.assertNotIn(M2, [x["m"] for x in self.app.data()["markets"]])

    def test_a_holding_with_no_order_and_a_tracked_market_are_listed(self):
        self.c.pos = {M2: {"netPositionDecimal": "30", "cashValue": {"value": "12"}}}
        self.app.refresh_positions(time.time())
        r = self.row(M2)
        self.assertEqual((r["why"], r["has"], r["net"], r["value"]), ("holding", False, 30.0, 12.0))
        self.c.pos = {}
        self.app.refresh_positions(time.time())
        self.app.watch[M2] = time.time()
        r = self.row(M2)
        self.assertEqual((r["why"], r["w"]), ("watched", True))

    def test_a_cancel_not_yet_acted_on_shows_and_counts_nothing(self):
        self.app.desk.remember_cancel("O1")
        self.app._read_orders()
        r = self.row(M)
        self.assertFalse(r["has"])
        self.assertTrue(r["bid"]["o"][0]["ghost"])
        self.assertIn("new", r["bid"])

    def test_the_books_kept_fresh_include_his_events_and_tracked_markets(self):
        self.app.watch[M2] = time.time()
        w = self.app.wanted_books()
        self.assertEqual(w[0], M)                  # his orders first
        self.assertIn(DEM, w)
        self.assertIn(M2, w)

    def test_terms_are_read_for_the_listed_markets(self):
        self.app.watch[M2] = time.time()
        self.c.progs[M2] = prog(pool=90)
        self.app.refresh_terms(time.time())
        self.assertEqual(self.app.terms.get(M2).pool, 90)


class TestEvents(unittest.TestCase):
    def test_discovery_names_each_markets_event(self):
        app, c = make()
        c.events_by_tag = lambda tag, max_pages=30: (
            [{"slug": EV, "title": "Oklahoma Senate", "markets": [
                {"slug": M, "subject": {"name": "Rep"}}, {"slug": DEM, "subject": {"name": "Dem"}},
                {"slug": "x-closed", "closed": True}]}] if tag == "politics" else [])
        found = discover(c)
        self.assertEqual({s: r["event"] for s, r in found.items()}, {M: EV, DEM: EV})
        with mock.patch("lite.app.discover", return_value=found):
            self.assertTrue(app.run_discover())
        self.assertEqual(sorted(app.members[EV]), sorted([M, DEM]))
        self.assertEqual(app.event_of(DEM), EV)

    def test_an_event_discovery_did_not_list_is_read_on_its_own(self):
        app, c = make()
        o = raw_order("Q1", M2, "BUY", 0.3, 10, "ORDER_INTENT_BUY_LONG")
        o["marketMetadata"]["eventSlug"] = "ev-ks"
        c.raw = [o]
        c.events["ev-ks"] = {"title": "Kansas Senate", "markets": [
            {"slug": M2, "subject": {"name": "Rep"}}, {"slug": "kansas-dem", "subject": {"name": "Dem"}}]}
        c.get = lambda url, **kw: ({"balances": c.bal} if "balances" in url
                                   else c.events[url.rsplit("/", 1)[1]])
        app.sample_once()
        app.upkeep_once()
        self.assertIn("kansas-dem", app.siblings())
        app.upkeep_once()                          # read once, not every pass
        self.assertEqual(len([k for k in app.event_tried if k.startswith("ev:")]), 1)


class TestTracking(unittest.TestCase):
    def test_a_discovered_market_is_tracked_and_saved(self):
        app, c = make()
        listed(app, {M2: "usse-ks-2026-11-03"})
        c.progs[M2] = prog()
        r = app.set_watch(M2, True)
        self.assertTrue(r["ok"])
        self.assertIn(M2, app.watch)
        self.assertTrue(app.known(M2))
        st = app.to_dict()
        self.assertIn(M2, st["watch"])
        app2, _ = make()
        app2.from_dict(st)
        self.assertIn(M2, app2.watch)
        self.assertEqual(app2.universe, app.universe)
        self.assertTrue(app.set_watch(M2, False)["ok"])
        self.assertNotIn(M2, app.watch)

    def test_an_econ_market_or_an_unchecked_one_is_refused(self):
        app, c = make()
        self.assertFalse(app.set_watch("usfed-rate-2026-12", True)["ok"])
        c.details["nfl-thing"] = {"active": True, "closed": False, "category": "crypto"}
        self.assertFalse(app.set_watch("nfl-thing", True)["ok"])
        self.assertEqual(app.watch, {})


def mid(pool):
    return {"timePeriods": [{"programId": "politics_mid_20260924", "rewardPool": pool,
                             "targetSize": 1000, "discountFactor": 0.5, "status": "LIVE"}]}


def scan_app():
    app, c = make()
    listed(app, {M: EV, DEM: EV, M2: "usse-ks-2026-11-03",
                 "nflx-team-1": "nflx", "macro-cpi-1": "m"})
    for s in (DEM, M2):
        app.event_n[s] = 2
    c.progs.update({M: mid(200), DEM: mid(400), M2: mid(100),
                    "nflx-team-1": {"timePeriods": [{"programId": "nfl_futures_core_20260901",
                                                     "rewardPool": 50, "targetSize": 100,
                                                     "discountFactor": 0.5, "status": "LIVE"}]}})
    c.books[DEM] = book([(0.55, 2000.0)], [(0.58, 1500.0)])
    c.books[M2] = book([(0.20, 3000.0)], [(0.25, 3000.0)])
    app.refresh_terms(time.time(), list(app.universe))
    return app, c


def fake_source(c, slugs_seen):
    def src(cache, slugs, kid, sec, progress=None, **kw):
        slugs_seen.extend(slugs)
        for i, s in enumerate(slugs):
            if s in c.books:
                cache.put(s, c.book(s), writer="ws")
                if progress:
                    progress(i + 1)
        return ""
    return src


class TestTheScan(unittest.TestCase):
    def test_the_pop_up_lists_politics_programs_only(self):
        app, _ = scan_app()
        keys = [g["key"] for g in app.scanner.groups()]
        self.assertEqual(keys, ["politics_mid"])
        g = app.scanner.groups()[0]
        self.assertEqual(g["n"], 3)
        self.assertEqual(g["pool"], 400)

    def test_a_scan_reads_prices_and_ranks_and_places_nothing(self):
        app, c = scan_app()
        seen = []
        app.scanner.book_source = fake_source(c, seen)
        with mock.patch("threading.Thread") as T:
            T.return_value.start = lambda: None
            r = app.scanner.start(["politics_mid"], 40)
        self.assertTrue(r["ok"])
        self.assertEqual(app.scanner.state, "running")
        self.assertFalse(app.scanner.start(["politics_mid"], 40)["ok"])   # one at a time
        app.scanner._run(["politics_mid"], 40.0)
        self.assertEqual(app.scanner.state, "done")
        self.assertEqual(seen[0], DEM)                 # the biggest pool read first
        v = app.scanner.view(sort="pct")
        self.assertEqual(v["counts"]["markets"], 3)
        pcts = [max(r["bid"].get("pct") or 0, r["ask"].get("pct") or 0) for r in v["rows"]]
        self.assertEqual(pcts, sorted(pcts, reverse=True))
        self.assertTrue(all(r["kind"] == "Senate" for r in v["rows"]))
        v = app.scanner.view(sort="bid_day")
        days = [r["bid"].get("day") or 0 for r in v["rows"]]
        self.assertEqual(days, sorted(days, reverse=True))
        self.assertEqual(c.posts, [])

    def test_an_unknown_or_empty_pick_is_refused(self):
        app, _ = scan_app()
        self.assertFalse(app.scanner.start([], 40)["ok"])
        self.assertFalse(app.scanner.start(["nfl_futures_core"], 40)["ok"])
        self.assertEqual(app.scanner.state, "idle")

    def test_a_tracked_result_says_so(self):
        app, c = scan_app()
        app.scanner.book_source = fake_source(c, [])
        app.scanner.state = "running"
        app.scanner._run(["politics_mid"], 40.0)
        app.set_watch(M2, True)
        row = next(r for r in app.scanner.view()["rows"] if r["m"] == M2)
        self.assertTrue(row["w"])

    def test_markets_past_the_cap_are_counted_not_dropped_silently(self):
        app, c = scan_app()
        app.scanner.book_source = fake_source(c, [])
        with mock.patch("lite.scan.SCAN_MAX", 2):
            app.scanner._run(["politics_mid"], 40.0)
        self.assertEqual(app.scanner.counts["dropped"], 1)
        self.assertIn("were not read", app.scanner.note)

    def test_program_names(self):
        self.assertEqual(group_key("midterms_t1_control_bop_tossup_senate_20261001"),
                         "midterms_t1_control_bop_tossup_senate")
        self.assertEqual(group_label("midterms_t1_control_bop_tossup_senate"),
                         "Midterms Tier 1 · control balance of power toss-up senate")
        self.assertEqual(group_label("politics_low"), "Politics · low")


class TestThePageRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.c = scan_app()
        with mock.patch.dict(os.environ, {"DASH_PASSWORD": "pw"}):
            cls.srv = serve(cls.app, port=0, bind="127.0.0.1")
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def req(self, method, path, headers=None, body=None):
        h = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h.request(method, path, body=body, headers=headers or {})
        r = h.getresponse()
        return r.status, r.read()

    def test_the_scan_and_the_watch_need_the_key_and_the_csrf_header(self):
        self.assertEqual(self.req("GET", "/scan.json")[0], 401)
        st, body = self.req("GET", "/scan.json?sort=pct", {"X-Dash-Key": "pw"})
        self.assertEqual(st, 200)
        self.assertEqual([g["key"] for g in json.loads(body)["groups"]], ["politics_mid"])
        w = json.dumps({"op": "watch", "market": M2, "on": True})
        self.assertEqual(self.req("POST", "/op", {"X-Dash-Key": "pw"}, w)[0], 403)
        st, out = self.req("POST", "/op", {"X-Dash-Key": "pw", "X-Reprice": "1"}, w)
        self.assertTrue(json.loads(out)["ok"])
        self.assertIn(M2, self.app.watch)
        st, body = self.req("GET", "/data.json?stake=25", {"X-Dash-Key": "pw"})
        d = json.loads(body)
        self.assertEqual(d["stake"], 25.0)
        self.assertIn(M2, [r["m"] for r in d["markets"]])
        self.assertEqual(self.c.posts, [])

    def test_the_page_carries_its_own_look(self):
        st, body = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"prefers-color-scheme:dark", body)
        self.assertIn(b"openScan", body)


if __name__ == "__main__":
    unittest.main()


try:
    import websockets  # noqa: F401
    HAVE_WS = True
except ImportError:
    HAVE_WS = False


@unittest.skipUnless(HAVE_WS, "websockets not installed")
class TestTheScanStream(unittest.TestCase):
    def test_books_arrive_over_several_connections_and_they_close(self):
        import asyncio
        import websockets
        from v3.books import BookCache
        from lite.scan import ws_books
        subs, conns, closed = [], [], []

        async def handler(ws, *a):
            conns.append(1)
            try:
                async for raw in ws:
                    sub = json.loads(raw)["subscribe"]
                    subs.append(len(sub["marketSlugs"]))
                    for s in sub["marketSlugs"]:
                        if s.endswith("-quiet"):
                            continue          # a market the exchange sends nothing for
                        await ws.send(json.dumps({"marketData": {
                            "marketSlug": s, "bids": [{"px": "0.40", "qty": "100"}],
                            "offers": [{"px": "0.42", "qty": "90"}]}}))
            finally:
                closed.append(1)

        ready = threading.Event()
        box = {}

        def serve_ws():
            async def main():
                async with websockets.serve(handler, "127.0.0.1", 0) as srv:
                    box["port"] = srv.sockets[0].getsockname()[1]
                    box["stop"] = asyncio.get_running_loop().create_future()
                    box["loop"] = asyncio.get_running_loop()
                    ready.set()
                    await box["stop"]
            asyncio.run(main())
        t = threading.Thread(target=serve_ws, daemon=True)
        t.start()
        ready.wait(5)
        slugs = [f"m{i}" for i in range(2500)] + ["x-quiet"]
        cache = BookCache()
        seen = []
        with mock.patch("v3.ws.WS_URL", f"ws://127.0.0.1:{box['port']}"), \
                mock.patch("v3.api.auth_headers", lambda *a, **k: {}):
            t0 = time.time()
            note = ws_books(cache, slugs, "k", "s", progress=seen.append, wait_s=10, quiet_s=1)
        self.assertLess(time.time() - t0, 8)
        self.assertEqual(len(conns), 2)                  # 2,000 a connection
        self.assertTrue(all(n <= 200 for n in subs))     # 200 a subscription
        self.assertEqual(sum(subs), len(slugs))
        self.assertIsNotNone(cache.any_age("m2499"))
        self.assertIsNone(cache.any_age("x-quiet"))
        self.assertEqual(seen[-1], 2500)
        time.sleep(0.3)
        self.assertEqual(len(closed), 2)                 # both connections closed
        self.assertEqual(note, "")
        box["loop"].call_soon_threadsafe(box["stop"].set_result, None)


class TestTheReviewOfTheList(unittest.TestCase):
    """The defects the review of 2026-10-05 confirmed."""

    def test_a_track_made_on_the_old_copy_during_a_deploy_is_kept(self):
        app, c = make()                       # the new copy, restored from an older save
        t = time.time()
        app.watch = {"old-a": t - 500, "old-b": t - 500}
        app.boot_ts = t - 200
        app.upload_hold = True
        # the old copy's stop save: tracked M2 and dropped old-b after that save
        app.store.remote = {"saved_at": t - 100, "watch": {"old-a": t - 500, M2: t - 150},
                            "unwatch": {"old-b": t - 140}}
        app.end_upload_hold()
        self.assertEqual(set(app.watch), {"old-a", M2})
        self.assertIn("old-b", app.unwatch)
        st = app.to_dict()
        self.assertIn(M2, st["watch"])
        self.assertIn("old-b", st["unwatch"])

    def test_an_untrack_before_a_late_read_stays_untracked(self):
        app, c = make()
        t = time.time()
        listed(app, {M2: "usse-ks-2026-11-03"})
        app.set_watch(M2, True)
        app.set_watch(M2, False)
        app._merge_late({"watch": {M2: t - 3600}, "event_n": {}, "placed_ids": {}})
        self.assertNotIn(M2, app.watch)

    def test_a_track_is_uploaded_at_once(self):
        app, c = make()
        listed(app, {M2: "usse-ks-2026-11-03"})
        app.set_watch(M2, True)
        self.assertTrue(app.store.saved[-1][1])          # force_remote
        app.set_watch(M2, False)
        self.assertTrue(app.store.saved[-1][1])

    def test_the_save_never_shares_a_dict_the_sweep_writes(self):
        app, _ = make()
        st = app.to_dict()
        self.assertIsNot(st["terms"]["updated_at"], app.terms.updated_at)
        self.assertIsNot(st["terms"]["seeded_at"], app.terms.seeded_at)

    def test_a_listed_market_opens_without_the_gateway_check(self):
        app, c = make()
        setup_event(app, c)
        c.market_details = mock.Mock(side_effect=AssertionError("gateway read"))
        self.assertTrue(app.book_view(DEM)["ok"])
        c.market_details = mock.Mock(side_effect=RuntimeError("held after a 429"))
        r = app.book_view("not-listed-anywhere")
        self.assertIn("could not check this market", r["note"])


class TestTheReviewOfTheScan(unittest.TestCase):
    def test_the_terms_read_stops_at_its_first_failure_and_skips_fresh_terms(self):
        app, c = scan_app()
        calls = []

        def programs(slugs, tries=4, timeout=20.0):
            calls.append((len(slugs), tries, timeout))
            raise RuntimeError("incentives host hanging")
        c.programs = programs
        app.scanner.book_source = fake_source(c, [])
        # everything was read just now: nothing to re-read
        app.scanner._run(["politics_mid"], 40.0)
        self.assertEqual(calls, [])
        # terms past the sweep's age are re-read, one quick try, and the
        # first failure ends it
        for s in app.terms.updated_at:
            app.terms.updated_at[s] -= 8 * 3600
        app.scanner._run(["politics_mid"], 40.0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1:], (1, 10.0))
        self.assertIn("terms read failed", app.scanner.note)
        self.assertEqual(app.scanner.state, "done")

    def test_the_straggler_reads_stop_after_three_failures(self):
        app, c = scan_app()
        app.scanner.book_source = lambda *a, **k: ""      # the stream sent nothing
        app.cache = type(app.cache)()                    # and nothing is cached
        n = []

        def book(slug, **kw):
            n.append(slug)
            raise RuntimeError("timed out")
        c.book = book
        app.scanner._run(["politics_mid"], 40.0)
        self.assertEqual(len(n), 3)
        self.assertEqual(app.scanner.counts["no_book"], 3)

    def test_a_running_scan_never_shows_the_last_rows_under_its_own_stake(self):
        app, c = scan_app()
        app.scanner.book_source = fake_source(c, [])
        app.scanner._run(["politics_mid"], 40.0)
        with mock.patch("threading.Thread") as T:
            T.return_value.start = lambda: None
            app.scanner.start(["politics_mid"], 500)
        v = app.scanner.view()
        self.assertEqual(v["state"], "running")
        self.assertEqual(v["stake"], 40.0)                # what the rows were priced with
        self.assertEqual(v["run_stake"], 500.0)


class TestTheSecondReview(unittest.TestCase):
    """What the review of the fixes found (2026-10-05)."""

    def test_a_restart_inside_the_hold_keeps_the_hold(self):
        app, c = make()
        st = app.to_dict()
        st["hold"] = True                         # saved to disk while the hold stood
        app.store = FakeStore(best=st)
        app.store.local_found = True              # the restart finds its own disk copy
        app.restore()
        self.assertTrue(app.upload_hold)
        self.assertTrue(app.hold_pending)

    def test_a_failed_read_of_the_old_copys_save_keeps_the_hold(self):
        app, c = make()
        app.upload_hold = app.hold_pending = True

        class Present(FakeStore):
            token = "t"

            def _gh(self, method, path):
                return mock.Mock(status_code=200)
        app.store = Present(remote=None)
        c.earn = [{"date": "2026-10-04", "market": M, "program_type": "lp",
                   "reward_usd": 1.0, "status": "PAID"}]
        t = app.boot_ts + 200
        app.records_once(t)
        self.assertTrue(app.upload_hold)          # no merge, so no upload and no payout check
        self.assertEqual(app.rewards_seen, {})
        for i in range(12):
            app.records_once(t + 30 * (i + 1))
        self.assertFalse(app.upload_hold)          # gave up after HOLD_READ_TRIES, and said so
        self.assertTrue(any("could not be read in" in n["note"] for n in app.notes))

    def test_no_track_or_untrack_while_the_save_is_read_back(self):
        app, c = make()
        app.restoring = True
        self.assertFalse(app.set_watch(M2, False)["ok"])
        self.assertFalse(app.set_watch(M2, True)["ok"])

    def test_an_old_listing_does_not_stand_in_for_the_check(self):
        app, c = make()
        listed(app, {M2: "usse-ks-2026-11-03"})
        app.universe_at = time.time() - 8 * 3600
        c.details[M2] = {"active": True, "closed": True}
        r = app.book_view(M2)
        self.assertFalse(r["ok"])
        self.assertIn("closed", r["note"])

    def test_three_short_discoveries_that_agree_are_the_new_list(self):
        app, c = make()
        full = {f"p{i}": {"event_n": 2, "name": "", "event": "e"} for i in range(500)}
        short = dict(list(full.items())[:200])
        with mock.patch("lite.app.discover", return_value=full):
            self.assertTrue(app.run_discover())
        with mock.patch("lite.app.discover", return_value=short):
            self.assertFalse(app.run_discover())
            self.assertFalse(app.run_discover())
            self.assertTrue(app.run_discover())      # the third agreeing read is taken
        self.assertEqual(len(app.universe), 200)
        self.assertEqual(app.discover_n, 200)

    def test_the_scan_rereads_terms_older_than_half_an_hour(self):
        app, c = scan_app()
        seen = []
        orig = c.programs

        def programs(slugs, tries=4, timeout=20.0):
            seen.extend(slugs)
            return orig(slugs)
        c.programs = programs
        app.scanner.book_source = fake_source(c, [])
        for s in app.terms.updated_at:
            app.terms.updated_at[s] -= 40 * 60
        c.progs[DEM] = mid(25)                        # the pool was cut
        app.scanner._run(["politics_mid"], 40.0)
        self.assertIn(DEM, seen)
        row = next(r for r in app.scanner.rows if r["m"] == DEM)
        self.assertAlmostEqual(row["pool"], 25 / 2 / 2)
