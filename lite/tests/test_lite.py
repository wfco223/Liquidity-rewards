"""The simple app (owner, 2026-10-04): the meter, his three order taps,
the records it keeps writing, its state, and the page's guards. The one
rule over everything: nothing but his tap places, moves or cancels."""

import base64
import http.client
import threading
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from v3.api import ApiError
from v3.intents import REST_SIDE
from v3.orders import OrderDesk
from v3.scoring import Book

from lite import records
from lite.app import App, normalize_orders
from lite.web import serve

M = "ussewc-usse-ok-2026-11-03-rep"
M2 = "ussewc-usse-ks-2026-11-03-rep"


def prog(pool=200, target=1000, df=0.5):
    return {"timePeriods": [{"programId": "politics_mid_1", "rewardPool": pool,
                             "targetSize": target, "discountFactor": df,
                             "status": "LIVE"}]}


def raw_order(oid, slug, side, price, qty, intent):
    return {"id": oid, "marketSlug": slug,
            "side": "ORDER_SIDE_BUY" if side == "BUY" else "ORDER_SIDE_SELL",
            "price": {"value": str(price)}, "leavesQuantity": qty,
            "intent": intent, "state": "ORDER_STATE_NEW",
            "marketMetadata": {"title": "Oklahoma Senate", "subject": {"name": "Rep"}}}


class FakeClient:
    key_id, secret_key = "k", "s"

    def __init__(self):
        self.raw = []
        self.books = {M: Book(bids=((0.40, 900.0), (0.39, 600.0)),
                              asks=((0.42, 800.0), (0.43, 700.0)), tick=0.01, fetched_at=0.0)}
        self.posts = []
        self.pos = {}
        self.bal = [{"buyingPower": {"value": "512.5"}, "availableToWithdraw": 480}]
        self.progs = {M: prog()}
        self.fail_orders = False
        self.earn = []
        self.details = {}
        self.events = {}
        self.n = 0
        self.fund = None          # dollars free: a placement is cut to what it funds
        self.listed = True        # False: the open list lags a new order
        self.pos_fail = False
        self.trades = []
        self.no_answer = False    # the placement lands but the answer never comes

    def open_orders_raw(self, max_pages=20, tries=4, timeout=None):
        if self.fail_orders:
            raise ApiError("exchange down")
        return [dict(o) for o in self.raw]

    def open_orders(self):
        return normalize_orders(self.raw)

    def balances_raw(self):
        return self.bal

    def get(self, url, *, path=None, signed=False, params=None, timeout=None, tries=4,
            priority=False):
        if url.endswith("/v1/account/balances"):
            return {"balances": self.bal}
        raise ApiError(f"no fake for {url}")

    def positions(self, max_pages=20):
        if self.pos_fail:
            raise ApiError("positions read failed")
        return self.pos

    def book(self, slug, fetched_at=None, timeout=None, tries=4, priority=False):
        b = self.books[slug]
        return Book(bids=b.bids, asks=b.asks, tick=b.tick, fetched_at=time.time())

    def gateway_hold(self):
        return 0.0

    def programs(self, slugs, tries=4, timeout=20.0):
        return {s: self.progs[s] for s in slugs if s in self.progs}

    def earnings(self, start):
        return list(self.earn)

    def activities(self, types=None, pages=10, page_size=100, tries=4, timeout=None):
        return []

    def recent_trades(self, limit=25, tries=4, timeout=None):
        return list(self.trades)

    def event_by_slug(self, ev):
        return self.events[ev]

    def market_details(self, slug):
        return self.details[slug]

    def post(self, url, json_body, *, path=None, timeout=None, tries=1):
        self.posts.append((path or url, dict(json_body)))
        if url.endswith("/v1/orders"):
            self.n += 1
            oid = f"N{self.n}"
            side = REST_SIDE[json_body["intent"]]
            qty = json_body["quantity"]
            px = float(json_body["price"]["value"])
            if self.fund is not None:
                per = px if side == "BUY" else 1 - px
                qty = min(qty, round(self.fund / per, 2))
            if self.listed:
                self.raw.append(raw_order(oid, json_body["marketSlug"], side,
                                          json_body["price"]["value"], qty,
                                          json_body["intent"]))
            if self.no_answer:
                raise ApiError("POST /v1/orders: ReadTimeout")
            return {"id": oid, "executions": []}
        oid = url.split("/v1/order/")[1].split("/")[0]
        self.raw = [o for o in self.raw if o["id"] != oid]
        return {}


class FakeStore:
    token, repo, branch = None, "r", "lite-state"
    local_found = False

    def __init__(self, best=None, remote=None, head="h1"):
        self.best, self.remote, self.saved, self.local = best, remote, [], []
        self.head = head

    def save_local(self, state):
        self.local.append(state)
        return True

    def remote_head(self):
        return self.head

    def load_best(self):
        return self.best

    def load_remote(self):
        return self.remote

    def save_soon(self, state, local=True, force_remote=False):
        self.saved.append((state, force_remote))
        return True

    def wait_remote(self, timeout=10.0):
        return True


class FakeAlerts:
    def __init__(self):
        self.sent = []

    def notify(self, title, message, priority="default"):
        self.sent.append((title, message))
        return True


class FakeRepo:
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.writes = []

    def read(self, path):
        return self.files.get(path, ""), ("sha" if path in self.files else None)

    def write(self, path, text, sha, message):
        self.writes.append((path, message))
        self.files[path] = text
        return True


def make(**kw):
    OrderDesk.halted = None
    c = FakeClient()
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("seed_store", FakeStore())
    app = App(client=c, store=FakeStore(), alerts=FakeAlerts(), repo=FakeRepo(), **kw)
    app.desk._sleep = lambda s: None
    app.restoring = False
    app.upload_hold = False
    app.event_n[M] = 2
    app.refresh_terms(time.time(), [M])
    return app, c


class TestTheMeter(unittest.TestCase):
    def test_the_sampler_reads_the_open_list_and_bills_its_rate(self):
        app, c = make()
        c.raw = [raw_order("O1", M, "BUY", 0.40, 100, "ORDER_INTENT_BUY_LONG")]
        app.cache.put(M, c.book(M))
        t = time.time()
        app.sample_once(t)
        self.assertAlmostEqual(app.verified_at, t, delta=5)
        self.assertEqual([o["id"] for o in app.orders], ["O1"])
        self.assertGreater(app.est.rate, 0)
        # each order's own figure is the meter's: one order, one rate
        self.assertAlmostEqual(app.order_est["O1"], app.est.rate, places=2)
        app.cache.put(M, c.book(M))
        app.sample_once(t + 20)
        self.assertAlmostEqual(app.est.earned, app.est.rate * 20 / 86400, places=4)

    def test_an_unread_open_list_keeps_the_orders_and_stops_billing(self):
        app, c = make()
        c.raw = [raw_order("O1", M, "BUY", 0.40, 100, "ORDER_INTENT_BUY_LONG")]
        t = time.time()
        app.cache.put(M, c.book(M))
        app.sample_once(t)
        c.fail_orders = True
        app.sample_once(t + 301)
        self.assertEqual([o["id"] for o in app.orders], ["O1"])
        self.assertEqual(app.est.rate, 0.0)
        self.assertEqual(app.est.dots[-1][1], 0.0)

    def test_a_market_on_its_first_day_in_a_program_counts_nothing(self):
        app, c = make()
        c.raw = [raw_order("O1", M, "BUY", 0.40, 100, "ORDER_INTENT_BUY_LONG")]
        app.terms.joined_at[M] = time.time()
        app.cache.put(M, c.book(M))
        app.sample_once()
        self.assertEqual(app.order_est["O1"], 0.0)
        self.assertEqual(app.est.rate, 0.0)

    def test_no_event_size_means_no_dollar_figure(self):
        app, c = make()
        del app.event_n[M]
        c.raw = [raw_order("O1", M, "BUY", 0.40, 100, "ORDER_INTENT_BUY_LONG")]
        app.cache.put(M, c.book(M))
        app.sample_once()
        self.assertIsNone(app.order_est["O1"])

    def test_the_book_side_comes_from_the_intent(self):
        o = raw_order("O1", M, "BUY", 0.6, 10, "ORDER_INTENT_BUY_SHORT")
        self.assertEqual(normalize_orders([o])[0]["side"], "SELL")

    def test_buying_power_is_read_with_the_meter(self):
        app, c = make()
        app.sample_once()
        self.assertEqual(app.data()["bp"], 512.5)


class TestNothingButHisTap(unittest.TestCase):
    def test_the_loops_never_place_move_or_cancel(self):
        app, c = make()
        c.raw = [raw_order("O1", M, "BUY", 0.40, 100, "ORDER_INTENT_BUY_LONG")]
        c.pos = {M: {"netPositionDecimal": "-50", "cashValue": {"value": "29"}}}
        c.earn = [{"date": records.utc_day(time.time(), 1), "market": M,
                   "program_type": "liquidityProgram", "reward_usd": 1.5, "status": "PAID"}]
        t = time.time()
        for i in range(12):
            app.sample_once(t + 20 * i)
            app.upkeep_once(t + 20 * i)
            app.records_once(t + 200 * i)
        app.run_discover()
        self.assertEqual(c.posts, [])

    def test_the_desk_refuses_anything_not_his(self):
        app, c = make()
        app.cache.put(M, c.book(M))
        r = app.desk.place_resting(M, "BUY", 0.40, 10, initiator="auto")
        self.assertFalse(r.ok)
        self.assertEqual(c.posts, [])


class TestHisTaps(unittest.TestCase):
    def setUp(self):
        self.app, self.c = make()
        self.c.raw = [raw_order("O1", M, "BUY", 0.40, 100, "ORDER_INTENT_BUY_LONG")]
        self.app.sample_once()

    def placed(self):
        return [b for p, b in self.c.posts if p == "/v1/orders"]

    def test_a_bid_rests_post_only_as_a_buy(self):
        r = self.app.place(M, "BUY", 39, 50)
        self.assertTrue(r["ok"], r)
        b = self.placed()[0]
        self.assertEqual((b["intent"], b["price"]["value"], b["quantity"]),
                         ("ORDER_INTENT_BUY_LONG", "0.39", 50))
        self.assertTrue(b["participateDontInitiate"])

    def test_a_bid_while_short_buys_the_short_back(self):
        self.c.pos = {M: {"netPositionDecimal": "-150"}}
        self.app.place(M, "BUY", 39, 100)
        self.assertEqual(self.placed()[0]["intent"], "ORDER_INTENT_SELL_SHORT")

    def test_an_ask_sells_what_he_holds_else_opens_a_short(self):
        self.c.pos = {M: {"netPositionDecimal": "50"}}
        self.app.place(M, "SELL", 43, 50)
        self.app.place(M, "SELL", 44, 80)
        self.assertEqual([b["intent"] for b in self.placed()],
                         ["ORDER_INTENT_SELL_LONG", "ORDER_INTENT_BUY_SHORT"])

    def test_bad_taps_are_refused_before_the_exchange(self):
        for args in ((M2, "BUY", 40, 10), (M, "HOLD", 40, 10), (M, "BUY", 40, 0),
                     (M, "BUY", 40, 30000), (M, "BUY", "x", 10)):
            r = self.app.place(*args)
            self.assertFalse(r["ok"], args)
        self.assertEqual(self.c.posts, [])

    def test_a_bid_at_the_ask_never_crosses(self):
        r = self.app.place(M, "BUY", 42, 10)
        self.assertFalse(r["ok"])
        self.assertEqual(self.placed(), [])

    def test_a_change_places_the_new_order_then_cancels_the_old(self):
        r = self.app.move("O1", 41, None)
        self.assertTrue(r["ok"], r)
        paths = [p for p, _ in self.c.posts]
        self.assertEqual(paths, ["/v1/orders", "/v1/order/O1/cancel"])
        self.assertEqual(self.placed()[0]["intent"], "ORDER_INTENT_BUY_LONG")
        self.assertEqual([o["id"] for o in self.app.orders], [r["id"]])

    def test_a_size_change_keeps_the_price(self):
        self.app.move("O1", None, 60)
        b = self.placed()[0]
        self.assertEqual((b["price"]["value"], b["quantity"]), ("0.4", 60))

    def test_a_change_with_nothing_changed_or_no_intent_is_refused(self):
        self.assertFalse(self.app.move("O1", 40, 100)["ok"])
        self.c.raw.append(raw_order("O2", M, "BUY", 0.38, 10, ""))
        self.assertFalse(self.app.move("O2", 37, None)["ok"])
        self.assertEqual(self.c.posts, [])

    def test_cancel(self):
        r = self.app.cancel("O1")
        self.assertTrue(r["ok"])
        self.assertEqual([p for p, _ in self.c.posts], ["/v1/order/O1/cancel"])
        self.assertEqual(self.app.orders, [])

    def test_opening_a_new_market_checks_it_first(self):
        self.c.details[M2] = {"slug": M2, "active": True, "closed": False, "category": "politics"}
        self.c.books[M2] = self.c.books[M]
        self.assertFalse(self.app.known(M2))
        self.assertTrue(self.app.book_view(M2)["ok"])
        self.assertTrue(self.app.known(M2))
        self.c.details["closed-mkt"] = {"closed": True}
        self.assertFalse(self.app.open_market("closed-mkt")["ok"])
        self.assertFalse(self.app.open_market("us-cpi-2026-10")["ok"])

    def test_the_tap_math_is_the_meters(self):
        self.app.cache.put(M, self.c.book(M))
        self.app.sample_once()
        m = self.app.order_math("O1")
        self.assertTrue(m["ok"])
        self.assertAlmostEqual(m["math"]["est"], self.app.order_est["O1"], places=6)
        self.assertGreater(m["math"]["share"], 0)
        self.assertTrue(any(w[4] for w in m["math"]["window"]))

    def test_a_stop_refuses_new_taps_and_saves(self):
        self.app.shutdown_save("signal 15")
        r = self.app.place(M, "BUY", 39, 10)
        self.assertFalse(r["ok"])
        self.assertIn("stopping", r["note"])
        self.assertEqual(self.c.posts, [])
        self.assertTrue(self.app.store.saved[-1][1])
        self.assertIsNone(OrderDesk.halted)


class TestRecords(unittest.TestCase):
    def test_the_rewards_file_keeps_history_before_the_window(self):
        old = ("date,market,program_type,reward_usd,status\n"
               "2026-07-01,a,lp,1,PAID\n2026-09-30,b,lp,2,PAID\n")
        rows = [{"date": "2026-09-30", "market": "b", "program_type": "lp", "reward_usd": 2.5,
                 "status": "PAID"},
                {"date": "2026-06-01", "market": "stray", "program_type": "lp",
                 "reward_usd": 9, "status": "PAID"}]
        text = records.compose_rewards_csv(rows, old, "2026-09-28")
        self.assertEqual(text.splitlines(), ["date,market,program_type,reward_usd,status",
                                             "2026-07-01,a,lp,1,PAID",
                                             "2026-09-30,b,lp,2.5,PAID"])

    def test_a_shrinking_or_empty_rewrite_is_refused(self):
        lines = "".join(f"2026-09-{d:02d},m{d},lp,1,PAID\n" for d in range(1, 29))
        repo = FakeRepo({records.REWARDS_PATH: records.REWARDS_HEADER + "\n" + lines})
        self.assertIn("no rows", records.write_rewards(repo, [], "2026-09-01"))
        one = [{"date": "2026-09-01", "market": "m1", "program_type": "lp",
                "reward_usd": 1, "status": "PAID"}]
        self.assertIn("refused", records.write_rewards(repo, one, "2026-09-01"))
        self.assertEqual(repo.writes, [])

    def test_a_file_past_one_megabyte_is_read_whole(self):
        big = ("x" * 1_200_000).encode()
        sess = mock.Mock()
        sess.get.side_effect = [
            mock.Mock(status_code=200, json=lambda: {"sha": "abc", "size": len(big),
                                                     "content": ""}),
            mock.Mock(status_code=200, content=big)]
        text, sha = records.Repo(token="t", session=sess).read("data/trades.csv")
        self.assertEqual((len(text), sha), (len(big), "abc"))
        raw_hdr = sess.get.call_args_list[1].kwargs["headers"]["Accept"]
        self.assertEqual(raw_hdr, "application/vnd.github.raw+json")

    def test_a_short_read_raises_rather_than_hand_back_a_part(self):
        sess = mock.Mock()
        sess.get.side_effect = [
            mock.Mock(status_code=200, json=lambda: {"sha": "abc", "size": 5000}),
            mock.Mock(status_code=200, content=b"")]
        with self.assertRaises(RuntimeError):
            records.Repo(token="t", session=sess).read("data/trades.csv")

    def test_new_reward_rows_push_once(self):
        app, c = make()
        day = records.utc_day(time.time(), 1)
        c.earn = [{"date": day, "market": M, "program_type": "lp", "reward_usd": 1.0,
                   "status": "PAID"}]
        app.check_rewards(time.time(), write_file=False)      # the baseline
        self.assertEqual(app.alerts.sent, [])
        c.earn.append({"date": day, "market": M2, "program_type": "lp",
                       "reward_usd": 2.0, "status": "PAID"})
        app.check_rewards(time.time(), write_file=False)
        self.assertEqual(len(app.alerts.sent), 1)
        self.assertEqual(app.alerts.sent[0][0], "Rewards posted")
        self.assertIn("$3.00", app.alerts.sent[0][1])
        app.check_rewards(time.time(), write_file=False)
        self.assertEqual(len(app.alerts.sent), 1)
        self.assertEqual([p for p, _ in app.repo.writes], [records.REWARDS_PATH])


class TestState(unittest.TestCase):
    def test_the_first_boot_seeds_from_3_0_and_never_writes_its_branch(self):
        app, c = make()
        app.event_n = {}
        seed = {"saved_at": 5.0,
                "est_politics": {"day": "2026-10-04", "earned": 21.63, "dots": [[1.0, 70.4, 34]],
                                 "last_ts": 1.0},
                "fam_politics": {"terms": app.terms.to_dict(),
                                 "universe": {M: {"event_n": 3}}, "event_n_seen": {M2: 4}},
                "names": {"known": {M: "Oklahoma Senate — Rep"}},
                "rewards_seen": {"2026-10-03|x": 1.0}, "paid_seen": {"2026-10-03|x": 1.0}}
        app.store = FakeStore(best=None)
        app.seed_store = FakeStore(remote=seed)
        app.restore()
        self.assertIn("3.0", app.restored)
        self.assertEqual(app.est.earned, 21.63)
        self.assertEqual(app.event_n, {M: 3, M2: 4})
        self.assertEqual(app.rewards_seen, {"2026-10-03|x": 1.0})
        self.assertEqual(app.label(M), "Oklahoma Senate — Rep")
        # the exchange's own words on a position beat a decoded name
        app.names.known[M2] = "Kansas Senate — rep"
        app.positions = {M2: {"marketMetadata": {"title": "Kansas Senate Election Winner",
                                                 "subject": {"name": "Roger Marshall (R)"}}}}
        self.assertEqual(app.label(M2), "Kansas Senate Election Winner — Roger Marshall (R)")
        self.assertEqual(app.seed_store.saved, [])

    def test_its_own_save_comes_back_whole(self):
        app, c = make()
        app.rewards_seen = {"k": 1.0}
        app.placed_ids = {"N1": 1.0}
        st = json.loads(json.dumps(app.to_dict()))
        app2, _ = make()
        app2.store = FakeStore(best=st)
        app2.restore()
        self.assertEqual((app2.rewards_seen, app2.placed_ids, app2.event_n[M]),
                         ({"k": 1.0}, {"N1": 1.0}, 2))
        self.assertIsNotNone(app2.terms.get(M))


class TestThePage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.c = make()
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
        return r.status, r.read(), r.getheader("Location")

    def test_the_page_is_public_and_the_data_is_not(self):
        self.assertEqual(self.req("GET", "/")[0], 200)
        self.assertEqual(self.req("GET", "/data.json")[0], 401)
        st, body, _ = self.req("GET", "/data.json", {"X-Dash-Key": "pw"})
        self.assertEqual(st, 200)
        self.assertIn("orders", json.loads(body))

    def test_an_order_tap_needs_the_key_and_the_csrf_header(self):
        body = json.dumps({"op": "cancel", "order_id": "O1"})
        self.assertEqual(self.req("POST", "/op", {}, body)[0], 401)
        self.assertEqual(self.req("POST", "/op", {"X-Dash-Key": "pw"}, body)[0], 403)
        st, out, _ = self.req("POST", "/op", {"X-Dash-Key": "pw", "X-Reprice": "1"},
                              json.dumps({"op": "switch_on"}))
        self.assertEqual(st, 200)
        self.assertFalse(json.loads(out)["ok"])
        self.assertEqual(self.c.posts, [])

    def test_an_old_bookmark_lands_on_the_page(self):
        st, _, loc = self.req("GET", "/focus")
        self.assertEqual((st, loc), (302, "/"))


class TestTheReviewFixes(unittest.TestCase):
    """The review of 2026-10-05, one test a finding."""

    def setUp(self):
        self.app, self.c = make()
        self.c.raw = [raw_order("O1", M, "BUY", 0.40, 1000, "ORDER_INTENT_BUY_LONG")]
        self.app.sample_once()

    def test_a_change_the_money_cannot_fund_leaves_the_original_untouched(self):
        self.c.fund = 20.0
        r = self.app.move("O1", 41, None, was_price=40, was_size=1000)
        self.assertFalse(r["ok"])
        self.assertIn("as it was", r["note"])
        ids = {o["id"]: o["size"] for o in self.app.orders}
        self.assertEqual(ids, {"O1": 1000.0})        # the cut replacement withdrawn

    def test_a_change_after_a_fill_is_refused_not_regrown(self):
        self.c.raw[0]["leavesQuantity"] = 100          # 900 filled while he typed
        r = self.app.move("O1", 41, None, was_price=40, was_size=1000)
        self.assertFalse(r["ok"])
        self.assertIn("now 100", r["note"])
        self.assertEqual(self.c.posts, [])
        r = self.app.move("O1", 41, None, was_price=40, was_size=100)
        self.assertTrue(r["ok"], r)
        self.assertEqual([b["quantity"] for p, b in self.c.posts if p == "/v1/orders"], [100.0])

    def test_a_price_that_snaps_back_onto_its_own_is_nothing_to_change(self):
        r = self.app.move("O1", 40.4, None)
        self.assertEqual(r["note"], "nothing to change")
        self.assertEqual(self.c.posts, [])

    def test_a_placement_not_listed_yet_holds_its_side(self):
        self.c.listed = False
        r = self.app.place(M, "BUY", 38, 500)
        self.assertTrue(r.get("pending"), r)
        # another order at that price showing does not release it; only its own id
        self.c.raw.append(raw_order("O5", M, "BUY", 0.38, 200, "ORDER_INTENT_BUY_LONG"))
        self.app.sample_once()
        for args in ((M, "BUY", 38, 500), (M, "BUY", 38, 499)):
            self.assertFalse(self.app.place(*args)["ok"])
        self.assertEqual(len([p for p, _ in self.c.posts if p == "/v1/orders"]), 1)

    def test_an_unreadable_position_refuses_the_tap(self):
        self.c.pos_fail = True
        r = self.app.place(M, "SELL", 45, 50)
        self.assertFalse(r["ok"])
        self.assertIn("position", r["note"])
        self.assertEqual(self.c.posts, [])

    def test_a_cancel_never_waits_behind_a_placement(self):
        with self.app.tap_lock:
            r = self.app.cancel("O1")
        self.assertTrue(r["ok"])

    def test_the_stop_waits_for_a_tap_in_flight(self):
        self.app.tap_lock.acquire()
        t = threading.Thread(target=lambda: self.app.shutdown_save("signal 15"))
        t.start()
        time.sleep(0.3)
        self.assertEqual(self.app.store.saved, [])      # still waiting for the tap
        self.app.tap_lock.release()
        t.join(5)
        self.assertTrue(self.app.store.saved)

    def test_a_cancelled_order_the_list_still_shows_is_not_metered(self):
        self.app.desk.remember_cancel("O1")
        self.app.sample_once()
        self.assertEqual(self.app.orders, [])

    def test_an_older_read_never_overwrites_a_newer_one(self):
        self.app._take_orders([raw_order("N9", M, "BUY", 0.39, 10, "ORDER_INTENT_BUY_LONG")],
                              time.time() + 5)
        self.app._take_orders([], time.time() - 5)
        self.assertEqual([o["id"] for o in self.app.orders], ["N9"])

    def test_the_card_keeps_its_cancel_when_the_book_cannot_be_read(self):
        def boom(*a, **k):
            raise ApiError("every gateway read held 90s more after a 429", status=429)
        self.app.cache = type(self.app.cache)()
        self.c.book = boom
        m = self.app.order_math("O1")
        self.assertEqual(m["order"]["id"], "O1")

    def test_fed_rate_markets_are_econ(self):
        self.c.details["cbpac-usfed-2026-cut"] = {"active": True, "closed": False}
        self.assertFalse(self.app.open_market("cbpac-usfed-2026-cut")["ok"])
        self.c.details["xyz-rate-2026"] = {"active": True, "closed": False, "category": "macro"}
        self.assertFalse(self.app.open_market("xyz-rate-2026")["ok"])

    def test_two_changes_of_one_order_never_both_go(self):
        self.app.busy.add("O1")
        r = self.app.move("O1", 41, None, was_price=40, was_size=1000)
        self.assertFalse(r["ok"])
        self.assertIn("in flight", r["note"])
        self.assertEqual(self.c.posts, [])

    def test_a_change_needs_the_open_list_read_at_the_tap(self):
        self.c.fail_orders = True
        r = self.app.move("O1", 41, None, was_price=40, was_size=1000)
        self.assertFalse(r["ok"])
        self.assertIn("could not read your orders", r["note"])
        self.assertEqual(self.c.posts, [])

    def test_an_exit_part_filled_during_its_change_keeps_what_rests(self):
        self.c.pos = {M: {"netPositionDecimal": "100"}}
        self.c.raw = [raw_order("X1", M, "SELL", 0.43, 100, "ORDER_INTENT_SELL_LONG")]
        self.app.sample_once()
        orig = self.c.post

        def part_fill(url, body, **kw):
            out = orig(url, body, **kw)
            if url.endswith("/v1/orders"):
                self.c.raw[-1]["leavesQuantity"] = 70        # 30 filled at once
                self.c.pos = {M: {"netPositionDecimal": "70"}}
            return out
        self.c.post = part_fill
        r = self.app.move("X1", 44, None, was_price=43, was_size=100)
        self.assertTrue(r["ok"], r)
        self.assertEqual([(o["id"], o["size"]) for o in self.app.orders], [(r["id"], 70.0)])

    def test_the_same_shares_are_never_offered_twice(self):
        self.c.pos = {M: {"netPositionDecimal": "100"}}
        self.c.raw.append(raw_order("X1", M, "SELL", 0.45, 100, "ORDER_INTENT_SELL_LONG"))
        self.app.sample_once()
        self.app.place(M, "SELL", 44, 100)
        intents = [b["intent"] for p, b in self.c.posts if p == "/v1/orders"]
        self.assertEqual(intents, ["ORDER_INTENT_BUY_SHORT"])   # not a second sale
        r = self.app.move("X1", None, 250, was_price=45, was_size=100)
        self.assertFalse(r["ok"])
        self.assertIn("you hold 100", r["note"])

    def test_a_new_market_must_be_politics_or_sports(self):
        for slug, md in (("rdc-banxico-2026-09-24-cut25", {}),
                         ("abc-thing-2026", {"category": "MAC", "subcategory": "MAC/RATES"}),
                         ("abc-other-2026", {})):
            self.c.details[slug] = {"active": True, "closed": False, **md}
            self.assertFalse(self.app.open_market(slug)["ok"], slug)
        self.c.details["abc-pol-2026"] = {"active": True, "closed": False, "category": "politics"}
        self.c.books["abc-pol-2026"] = self.c.books[M]
        self.assertTrue(self.app.open_market("abc-pol-2026")["ok"])

    def test_a_stop_during_the_restore_saves_nothing(self):
        app, c = make()
        app.restoring = True
        app.shutdown_save("signal 15")
        self.assertEqual((app.store.saved, app.store.local), ([], []))

    def test_a_new_container_uploads_nothing_until_the_old_copy_is_gone(self):
        app, c = make()
        app.upload_hold = True
        app.store.remote = {"rewards_seen": {"d|old": 2.0}, "placed_ids": {"OLDX": 5.0}}
        app.save()
        self.assertEqual((len(app.store.local), app.store.saved), (1, []))
        app.records_once(app.boot_ts + 60)
        self.assertTrue(app.upload_hold)
        app.records_once(app.boot_ts + 200)
        self.assertFalse(app.upload_hold)
        self.assertEqual(app.rewards_seen.get("d|old"), 2.0)
        self.assertIn("OLDX", app.placed_ids)

    def test_an_absent_branch_found_late_seeds_from_3_0(self):
        app, c = make()
        app.state_unread = True
        class Late(FakeStore):
            token = "t"
            def _gh(self, method, path):
                return mock.Mock(status_code=404)
        app.store = Late(best=None)
        app.seed_store = FakeStore(remote={"rewards_seen": {"a": 1.0}})
        app.rewards_seen = {"b": 2.0}
        app.retry_restore(time.time() + 120)
        self.assertFalse(app.state_unread)
        self.assertEqual(app.rewards_seen, {"a": 1.0, "b": 2.0})

    def test_a_late_read_keeps_what_this_copy_learned(self):
        app, c = make()
        app.state_unread = True
        app.rewards_seen = {"k": 9.0}
        app.store = FakeStore(best={"saved_at": 1, "rewards_seen": {"k": 1.0, "j": 2.0},
                                    "event_n": {"s": 3}})
        app.retry_restore(time.time() + 120)
        self.assertEqual(app.rewards_seen, {"k": 9.0, "j": 2.0})
        self.assertEqual(app.event_n["s"], 3)

    def test_3_0s_final_memory_unread_holds_the_first_check(self):
        app, c = make()
        app.seeded_v3 = True
        app.seed_store = FakeStore(remote=None)
        c.earn = [{"date": records.utc_day(time.time(), 1), "market": M, "program_type": "lp",
                   "reward_usd": 1.0, "status": "PAID"}]
        app.check_rewards(time.time(), write_file=False)
        self.assertFalse(app.seed_caught_up)
        self.assertEqual(app.rewards_seen, {})

    def test_discovery_is_short_against_its_own_last_read(self):
        app, c = make()
        app.event_n = {f"m{i}": 2 for i in range(10000)}
        found = {f"p{i}": {"event_n": 2, "name": ""} for i in range(500)}
        with mock.patch("lite.app.discover", return_value=found):
            self.assertTrue(app.run_discover())          # first full read: no comparison
            self.assertEqual(app.discover_n, 500)
        with mock.patch("lite.app.discover", return_value=dict(list(found.items())[:100])):
            self.assertFalse(app.run_discover())          # a fifth of the last: short

    def test_an_order_names_its_event_for_the_size_lookup(self):
        o = raw_order("Q1", M2, "BUY", 0.3, 10, "ORDER_INTENT_BUY_LONG")
        o["marketMetadata"]["eventSlug"] = "ev-ks"
        self.c.raw.append(o)
        self.c.events["ev-ks"] = {"markets": [{"slug": M2}, {"slug": "kansas-dem"}]}
        self.c.get = lambda url, **kw: ({"balances": self.c.bal} if "balances" in url
                                        else self.c.events[url.rsplit("/", 1)[1]])
        self.app.sample_once()
        self.app.upkeep_once()
        self.assertEqual(self.app.event_n[M2], 2)

    def test_a_restart_just_after_midnight_bills_nothing_for_the_outage(self):
        from v3.terms import et_day_start
        app, c = make()
        now = time.time()
        midnight = et_day_start(now)
        app.est.day = "1999-01-01"
        app.est.last_ts = midnight - 23 * 60
        app.est.market_rates = {M: 67.0}
        app.cache.put(M, c.book(M))
        app.sample_once(midnight + 180)
        self.assertEqual(app.est.earned, 0.0)
        self.assertAlmostEqual(app.est.history[-1]["stale_s"], 23 * 60, delta=1)

    def test_a_market_with_no_event_size_is_looked_up(self):
        self.c.details[M2] = {"active": True, "closed": False, "eventSlug": "ev-ks",
                              "category": "politics"}
        self.c.events["ev-ks"] = {"markets": [{"slug": M2}, {"slug": M2[:-3] + "dem"},
                                              {"slug": "x", "closed": True}]}
        self.c.get = lambda url, **kw: self.c.events[url.rsplit("/", 1)[1]]
        self.c.books[M2] = self.c.books[M]
        self.app.open_market(M2)
        self.assertEqual(self.app.event_n[M2], 2)

    def test_its_own_branch_unreadable_holds_every_save(self):
        app, c = make()
        class Unread(FakeStore):
            token = "t"
            def _gh(self, method, path):
                return mock.Mock(status_code=502)
        app.store = Unread(best=None)
        app.seed_store = FakeStore(remote={"saved_at": 1, "rewards_seen": {"a": 1.0}})
        app.restore()
        self.assertTrue(app.state_unread)
        self.assertEqual(app.rewards_seen, {})        # not reseeded from 3.0
        app.save(force_remote=True)
        self.assertEqual(app.store.saved, [])

    def test_its_own_branch_absent_seeds_from_3_0(self):
        app, c = make()
        class Absent(FakeStore):
            token = "t"
            def _gh(self, method, path):
                return mock.Mock(status_code=404)
        app.store = Absent(best=None)
        app.seed_store = FakeStore(remote={"saved_at": 1, "rewards_seen": {"a": 1.0},
                                           "fam_cfb": {"event_n_seen": {"cfb-x": 12},
                                                       "orders": {"OLD1": {}}}})
        app.restore()
        self.assertFalse(app.state_unread)
        self.assertEqual((app.rewards_seen, app.event_n.get("cfb-x")), ({"a": 1.0}, 12))
        self.assertIn("OLD1", app.placed_ids)

    def test_a_seed_boot_takes_3_0s_final_payout_memory_before_its_first_check(self):
        app, c = make()
        app.seeded_v3 = True
        day = records.utc_day(time.time(), 1)
        app.rewards_seen = {f"{day}|{M}": 1.0}
        app.seed_store = FakeStore(remote={"rewards_seen": {f"{day}|{M}": 1.0,
                                                            f"{day}|{M2}": 2.0}})
        c.earn = [{"date": day, "market": m, "program_type": "lp", "reward_usd": v,
                   "status": "PAID"} for m, v in ((M, 1.0), (M2, 2.0))]
        app.check_rewards(time.time(), write_file=False)
        self.assertEqual(app.alerts.sent, [])           # 3.0 pushed M2 already

    def test_no_payout_check_while_the_old_copy_still_runs(self):
        app, c = make()
        c.earn = [{"date": records.utc_day(time.time(), 1), "market": M, "program_type": "lp",
                   "reward_usd": 1.0, "status": "PAID"}]
        app.records_once(app.boot_ts + 60)
        self.assertEqual(app.rewards_seen, {})
        app.records_once(app.boot_ts + 200)
        self.assertTrue(app.rewards_seen)

    def test_a_restart_across_midnight_counts_only_todays_gap(self):
        from v3.terms import et_day_start
        app, c = make()
        now = time.time()
        midnight = et_day_start(now)
        app.est.day = "1999-01-01"
        app.est.last_ts = midnight - 4 * 3600
        app.sample_once(now)
        self.assertLessEqual(app.est.stale_s, now - midnight + 1)

    def test_the_page_gets_six_hours_of_dots(self):
        app, c = make()
        now = time.time()
        app.est.dots = [[now - 20 * 3600, 5.0, 1], [now - 60, 7.0, 1]]
        self.assertEqual([d[1] for d in app.data()["dots"]], [7.0])

    def test_the_trades_file_reads_its_own_order_ids_as_ours(self):
        known = []
        repo = FakeRepo({records.TRADES_PATH: "ts,iso,type,market,side,intent,price,shares,"
                         "order_id,role\n1.0,x,T,m,BUY,I,0.5,1,OURS1,passive\n"})
        with mock.patch("v3.main.parse_activities",
                        side_effect=lambda raw, k: known.append(set(k)) or []):
            records.publish_trades(self.c, repo, {"P1"}, deep=False)
        self.assertEqual(known[0], {"P1", "OURS1"})


class TestTheFinalReview(unittest.TestCase):
    """The last review of the order paths, 2026-10-05."""

    def setUp(self):
        self.app, self.c = make()

    def placed(self):
        return [b for p, b in self.c.posts if p == "/v1/orders"]

    def test_selling_exactly_what_is_free_sells_it(self):
        self.c.pos = {M: {"netPositionDecimal": "100.3"}}
        self.c.raw = [raw_order("X1", M, "SELL", 0.45, 50.1, "ORDER_INTENT_SELL_LONG")]
        self.app.sample_once()
        r = self.app.place(M, "SELL", 44, 50.2)
        self.assertEqual(self.placed()[0]["intent"], "ORDER_INTENT_SELL_LONG")
        self.assertIn("sells what you hold", r["note"])

    def test_fills_of_the_original_during_a_change_are_said(self):
        self.c.raw = [raw_order("O1", M, "BUY", 0.40, 1000, "ORDER_INTENT_BUY_LONG")]
        self.app.sample_once()
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 1)) + ".5Z"
        self.c.trades = [{"trade": {"passiveExecution": {"order": {"id": "O1"},
                                                         "lastShares": "600",
                                                         "transactTime": now}}}]
        r = self.app.move("O1", 39, None, was_price=40, was_size=1000)
        self.assertTrue(r["ok"], r)
        self.assertIn("600 of the original filled during the change", r["note"])

    def test_a_placement_with_no_answer_that_landed_is_found(self):
        self.c.raw = []
        self.app.checked[M] = time.time()          # he opened the market
        self.app.sample_once()
        self.c.no_answer = True
        r = self.app.place(M, "BUY", 39, 10)
        self.assertTrue(r["ok"], r)
        self.assertIn("no clear answer, but it rests", r["note"])

    def test_a_placement_with_no_answer_holds_its_side(self):
        self.c.raw = []
        self.app.checked[M] = time.time()
        self.app.sample_once()
        self.c.no_answer, self.c.listed = True, False
        self.assertFalse(self.app.place(M, "BUY", 39, 10)["ok"])
        r = self.app.place(M, "BUY", 39, 10)
        self.assertIn("no answer", r["note"])
        self.assertEqual(len(self.placed()), 1)

    def test_a_change_with_no_answer_withdraws_what_landed(self):
        self.c.raw = [raw_order("O1", M, "BUY", 0.40, 100, "ORDER_INTENT_BUY_LONG")]
        self.app.sample_once()
        self.c.no_answer = True
        r = self.app.move("O1", 39, None, was_price=40, was_size=100)
        self.assertFalse(r["ok"])
        self.assertIn("was withdrawn", r["note"])
        self.assertEqual([o["id"] for o in self.app.orders], ["O1"])

    def test_a_change_waits_for_a_pending_placement_on_its_side(self):
        self.c.raw = [raw_order("O1", M, "BUY", 0.40, 100, "ORDER_INTENT_BUY_LONG")]
        self.app.sample_once()
        self.app.pending[(M, "BUY")] = (time.time(), "N7")
        r = self.app.move("O1", 39, None, was_price=40, was_size=100)
        self.assertFalse(r["ok"])
        self.assertIn("not listed yet", r["note"])
        self.assertEqual(self.c.posts, [])

    def test_a_cancel_claims_the_order_while_it_runs(self):
        self.c.raw = [raw_order("O1", M, "BUY", 0.40, 100, "ORDER_INTENT_BUY_LONG")]
        self.app.sample_once()
        seen = []
        real = self.app.desk.cancel
        def spy(oid, slug, **kw):
            seen.append(oid in self.app.busy)
            return real(oid, slug, **kw)
        self.app.desk.cancel = spy
        self.app.cancel("O1")
        self.assertEqual(seen, [True])
        self.assertNotIn("O1", self.app.busy)

    def test_a_cancel_the_exchange_has_not_acted_on_still_counts_and_can_be_sent_again(self):
        self.c.pos = {M: {"netPositionDecimal": "100"}}
        self.c.raw = [raw_order("X1", M, "SELL", 0.45, 100, "ORDER_INTENT_SELL_LONG")]
        self.app.sample_once()
        self.app.desk.remember_cancel("X1")           # cancelled, still listed
        self.app.sample_once()
        self.assertEqual(self.app.offered(M, "ORDER_INTENT_SELL_LONG"), 100.0)
        self.assertTrue(self.app.data()["orders"][0].get("ghost"))
        self.assertTrue(self.app.cancel("X1")["ok"])


class TestTheLauncher(unittest.TestCase):
    def test_the_marker_file_picks_the_simple_app_and_the_env_overrides(self):
        import launcher
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(launcher, "HERE", d), \
                    mock.patch.dict(os.environ, {"APP": ""}):
                self.assertEqual(launcher.app_choice(), "v3")
                os.makedirs(os.path.join(d, "lite"))
                open(os.path.join(d, "lite", "ACTIVE"), "w").close()
                self.assertEqual(launcher.app_choice(), "lite")
                with mock.patch.dict(os.environ, {"APP": "v3"}):
                    self.assertEqual(launcher.app_choice(), "v3")
                with mock.patch.dict(os.environ, {"POLYMARKET_KEY_ID": "k",
                                                  "POLYMARKET_SECRET_KEY": "s"}):
                    procs = launcher.children()
        self.assertEqual(list(procs), ["lite"])
        self.assertEqual(procs["lite"][-1], "lite.main")


if __name__ == "__main__":
    unittest.main()
