"""The pay tab (owner, 2026-10-05: "Also bring over the pay page. It
should be on its own tab"): 3.0's pay page — the all-time total and the
tax set aside, the check for new payouts with the posting progress, and
each day's pay against the meter's estimate, over the markets posted so
far while the day is still posting."""

import http.client
import json
import os
import time
import unittest
from unittest import mock

from lite import records
from lite.web import serve
from lite.tests.test_lite import M, M2, FakeRepo, make

CSV = ("date,market,program_type,reward_usd,status\n"
       "2026-07-01,a,lp,1.5,PAID\n"
       "2026-07-01,a,lp,0.25,SKIPPED\n"
       "2026-07-01,b,lp,2,PAID\n"
       "2026-07-02,a,lp,0.75,PENDING\n")


def row(day, market, usd, status="PAID"):
    return {"date": day, "market": market, "program_type": "liquidityProgram",
            "reward_usd": usd, "status": status}


class TestDayTotals(unittest.TestCase):
    def test_every_row_but_the_skipped_ones(self):
        self.assertEqual(records.day_totals(CSV), {"2026-07-01": 3.5, "2026-07-02": 0.75})


class TestTheCheck(unittest.TestCase):
    def setUp(self):
        self.app, self.c = make()
        self.now = time.time()
        self.day = records.utc_day(self.now, 1)
        self.app.repo = FakeRepo({"data/rewards.csv": CSV})

    def test_it_records_the_days_it_covers_and_never_a_stray(self):
        stray = records.utc_day(self.now, 30)              # outside the asked window
        self.app.actuals_by_day = {stray: 9.0}
        self.c.earn = [row(self.day, M, 2.0), row(self.day, M2, 1.0, "SKIPPED"),
                       row(stray, M, 0.1)]
        self.app.check_rewards(self.now, write_file=False)
        self.assertEqual(self.app.actuals_by_day[self.day], 2.0)
        self.assertEqual(self.app.actuals_by_day[stray], 9.0)   # a part of a day never overwrites it

    def test_the_hourly_read_takes_every_day_the_file_holds(self):
        self.app.actuals_by_day = {"2026-06-01": 4.0}           # a day the file lacks stays
        self.c.earn = [row(self.day, M, 2.0)]
        self.app.check_rewards(self.now, write_file=True)
        self.assertEqual(self.app.actuals_by_day["2026-07-01"], 3.5)
        self.assertEqual(self.app.actuals_by_day[self.day], 2.0)
        self.assertEqual(self.app.actuals_by_day["2026-06-01"], 4.0)

    def test_progress_counts_what_appeared_of_what_the_meter_claimed(self):
        from v3.estimator import et_day
        d = et_day(self.now)
        self.app.claims = {f"{d}|{M}": 3.0, f"{d}|{M2}": 1.0, f"{d}|x": 0.5}
        self.c.earn = [row(d, M, 2.5, "PENDING"), row(d, "y", 0.2)]
        self.app.rewards_seen = {"old|m": 1.0}                   # past the first-check baseline
        self.app.check_rewards(self.now, write_file=False)
        p = self.app.pay_progress
        self.assertEqual(len(p), 1)
        self.assertEqual((p[0]["day"], p[0]["expected"], p[0]["appeared"], p[0]["pending"],
                          p[0]["extra"]), (d, 3, 1, 1, 1))
        self.assertIn(d, self.app.posting_last)

    def test_his_tap_waits_out_the_hold_and_then_checks_and_pushes(self):
        self.c.earn = [row(self.day, M, 2.0)]
        self.app.rewards_seen = {"old|m": 1.0}
        self.app.boot_ts = self.now
        r = self.app.check_payouts_now()
        self.assertFalse(r["ok"])
        self.assertIn("three minutes", r["note"])
        self.app.boot_ts = self.now - 400
        r = self.app.check_payouts_now()
        self.assertTrue(r["ok"])
        self.assertEqual(r["new_count"], 1)
        self.assertEqual(r["new_rows"][0]["market"], M)
        self.assertTrue(r["new_rows"][0]["name"])
        self.assertEqual(self.app.alerts.sent[-1][0], "Rewards posted")
        self.assertEqual(self.c.posts, [])                   # a payout check never trades

    def test_a_tap_never_runs_beside_a_check_already_running(self):
        self.app.boot_ts = self.now - 400
        with mock.patch("lite.app.TAP_CHECK_WAIT_S", 0.05):
            self.app.rw_lock.acquire()
            try:
                r = self.app.check_payouts_now()
            finally:
                self.app.rw_lock.release()
        self.assertFalse(r["ok"])


class TestThePage(unittest.TestCase):
    def test_a_day_is_graded_over_the_markets_posted_so_far(self):
        app, c = make()
        d = "2026-10-03"
        app.est.history = [{"day": d, "earned": 30.0, "stale_s": 60.0,
                            "per_market": {M: 20.0, M2: 10.0}}]
        app.paid_seen = {f"{d}|{M}": 18.0, f"{d}|other": 1.0}
        app.actuals_by_day = {d: 19.0, "2026-07-01": 100.0}
        v = app.pay_view()
        r = next(x for x in v["days"] if x["day"] == d)
        self.assertEqual((r["actual"], r["est"]), (19.0, 30.0))
        self.assertEqual((r["posted_n"], r["est_n"]), (1, 2))
        self.assertAlmostEqual(r["ratio_posted"], 0.9)        # 18 paid of the 20 claimed so far
        self.assertEqual(r["extra_paid"], 1.0)
        self.assertEqual({m["m"] for m in r["markets"]}, {M, M2, "other"})
        self.assertEqual(v["paid_total"], {"usd": 119.0, "days": 2, "since": "2026-07-01"})
        self.assertEqual(v["tax_rate"], 0.22)

    def test_the_meter_keeps_each_markets_figure_every_sample(self):
        from lite.tests.test_lite import raw_order
        app, c = make()
        c.raw = [raw_order("O1", M, "BUY", 0.40, 100, "ORDER_INTENT_BUY_LONG")]
        app.cache.put(M, c.book(M))
        t = time.time()
        app.sample_once(t)
        app.cache.put(M, c.book(M))
        app.sample_once(t + 20)
        self.assertGreater(app.claims[f"{app.est.day}|{M}"], 0)
        self.assertIn("claims", app.to_dict())


class TestTheRoutes(unittest.TestCase):
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

    def test_the_pay_data_needs_the_key_and_the_old_page_opens_the_tab(self):
        self.assertEqual(self.req("GET", "/pay.json")[0], 401)
        st, body, _ = self.req("GET", "/pay.json", {"X-Dash-Key": "pw"})
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["ok"])
        st, _, loc = self.req("GET", "/pay")
        self.assertEqual((st, loc), (302, "/#pay"))
        st, _, loc = self.req("GET", "/grades")
        self.assertEqual((st, loc), (302, "/#pay"))
        b = json.dumps({"op": "check_payouts"})
        self.assertEqual(self.req("POST", "/op", {"X-Dash-Key": "pw"}, b)[0], 403)
        st, out, _ = self.req("POST", "/op", {"X-Dash-Key": "pw", "X-Reprice": "1"}, b)
        self.assertEqual(st, 200)
        self.assertIn("ok", json.loads(out))

    def test_the_page_has_the_tab(self):
        st, body, _ = self.req("GET", "/")
        self.assertIn(b'id="t-pay"', body)
        self.assertIn(b"drawPay", body)


if __name__ == "__main__":
    unittest.main()


class TestTheStop(unittest.TestCase):
    def test_a_check_still_reading_when_the_stop_comes_records_and_pushes_nothing(self):
        app, c = make()
        app.rewards_seen = {"old|m": 1.0}
        now = time.time()
        c.earn = [row(records.utc_day(now, 1), M, 2.0)]
        real = c.earnings

        def earnings(start):
            app.stopping = "signal 15"           # the stop arrives during the read
            return real(start)
        c.earnings = earnings
        self.assertIsNone(app.check_rewards(now, write_file=False))
        self.assertEqual(app.alerts.sent, [])
        self.assertNotIn(f"{records.utc_day(now, 1)}|{M}", app.rewards_seen)

    def test_the_stop_waits_for_a_check_past_its_read(self):
        import threading
        app, c = make()
        app.upload_hold = app.hold_pending = False
        app.rw_lock.acquire()
        order = []

        def finish():
            time.sleep(0.3)
            order.append("check done")
            app.rw_lock.release()
        threading.Thread(target=finish).start()
        app.shutdown_save("signal 15")
        order.append("stop saved")
        self.assertEqual(order, ["check done", "stop saved"])


class TestThePayReview(unittest.TestCase):
    """What the review of the pay tab found (2026-10-05)."""

    def test_new_rows_list_the_biggest_first(self):
        now = time.time()
        d = records.utc_day(now, 1)
        rows = [row(d, f"m{i}", v) for i, v in enumerate([5, 0.01, 2.5, 0.2, 12])]
        seen, paid = {"old|m": 1.0}, {}
        res = records.check_rewards(rows, seen, paid, now)
        self.assertEqual([r["usd"] for r in res["new_rows"]], [12.0, 5.0, 2.5, 0.2, 0.01])

    def test_the_days_before_the_switch_read_as_3_0_graded_them(self):
        from lite.tests.test_lite import FakeStore
        app, c = make()
        d = "2026-09-30"
        app.est.history = [{"day": d, "earned": 20.0, "stale_s": 0.0,
                            "per_market": {M: 15.0}}]            # the top 50 of politics only
        app.paid_seen = {f"{d}|{M}": 14.0, f"{d}|{M2}": 3.0}
        app.seed_store = FakeStore(remote={
            "saved_at": 1791165791.0,                              # 2026-10-04, ET
            "actuals_by_day": {d: 17.0, "2026-07-01": 50.0},
            "mkt_claim_day": {f"{d}|{M}": 15.0, f"{d}|{M2}": 4.0},
            "est_politics": {"history": [{"day": d, "earned": 20.0}]},
            "est_cfb": {"history": [{"day": d, "earned": 1.5}]}})
        self.assertTrue(app.seed_pay(time.time()))
        self.assertEqual(app.v3_cut, "2026-10-04")
        v = app.pay_view()
        r = next(x for x in v["days"] if x["day"] == d)
        self.assertEqual(r["est"], 21.5)                           # every meter, as 3.0 summed them
        self.assertEqual((r["posted_n"], r["est_n"]), (2, 2))      # every market 3.0 claimed
        self.assertAlmostEqual(r["ratio_posted"], 17 / 19, places=3)
        self.assertEqual(v["paid_total"]["days"], 2)
        self.assertFalse(app.seed_pay(time.time()) is False)       # read once, then done

    def test_no_day_totals_yet_reads_as_not_read_never_as_nothing_paid(self):
        app, c = make()
        app.actuals_by_day = {}
        self.assertIsNone(app.pay_view()["paid_total"])
        app.actuals_by_day = {"2026-10-01": 1.0}
        app.restoring = True
        self.assertIsNone(app.pay_view()["paid_total"])

    def test_the_taps_answer_carries_its_time(self):
        app, c = make()
        app.boot_ts = time.time() - 400
        r = app.check_payouts_now()
        self.assertIn("at", r)
