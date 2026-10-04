"""The simple app (owner, 2026-10-04): the meter, his three order taps,
the records it keeps writing, its state, and the page's guards. The one
rule over everything: nothing but his tap places, moves or cancels."""

import base64
import http.client
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
        self.n = 0

    def open_orders_raw(self, max_pages=20, tries=4, timeout=None):
        if self.fail_orders:
            raise ApiError("exchange down")
        return [dict(o) for o in self.raw]

    def open_orders(self):
        return normalize_orders(self.raw)

    def balances_raw(self):
        return self.bal

    def positions(self, max_pages=20):
        return self.pos

    def book(self, slug, fetched_at=None, timeout=None, tries=4, priority=False):
        b = self.books[slug]
        return Book(bids=b.bids, asks=b.asks, tick=b.tick, fetched_at=time.time())

    def gateway_hold(self):
        return 0.0

    def programs(self, slugs):
        return {s: self.progs[s] for s in slugs if s in self.progs}

    def earnings(self, start):
        return list(self.earn)

    def activities(self, pages=3):
        return []

    def market_details(self, slug):
        return self.details[slug]

    def post(self, url, json_body, *, path=None, timeout=None, tries=1):
        self.posts.append((path or url, dict(json_body)))
        if url.endswith("/v1/orders"):
            self.n += 1
            oid = f"N{self.n}"
            side = REST_SIDE[json_body["intent"]]
            self.raw.append(raw_order(oid, json_body["marketSlug"], side,
                                      json_body["price"]["value"], json_body["quantity"],
                                      json_body["intent"]))
            return {"id": oid, "executions": []}
        oid = url.split("/v1/order/")[1].split("/")[0]
        self.raw = [o for o in self.raw if o["id"] != oid]
        return {}


class FakeStore:
    def __init__(self, best=None, remote=None):
        self.best, self.remote, self.saved = best, remote, []

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
    app = App(client=c, store=FakeStore(), alerts=FakeAlerts(), repo=FakeRepo(), **kw)
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
        self.assertEqual(app.verified_at, t)
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
        for i in range(5):
            app.sample_once(t + 20 * i)
            app.upkeep_once(t + 20 * i)
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
        self.c.details[M2] = {"slug": M2, "active": True, "closed": False}
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

    def test_a_stop_halts_the_taps(self):
        self.app.shutdown_save("signal 15")
        self.assertFalse(self.app.place(M, "BUY", 39, 10)["ok"])
        self.assertFalse(self.app.cancel("O1")["ok"])
        self.assertTrue(self.app.store.saved[-1][1])
        OrderDesk.halted = None


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
