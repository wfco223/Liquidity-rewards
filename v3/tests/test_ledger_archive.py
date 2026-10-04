"""The transaction-record archive (owner, 2026-10-04: "Get a record of my
transaction from the api going as far back as you can and store them
somewhere where they won't be deleted or overwritten. If there is
already a record in the repo, do not overwrite any information there").

Read-only against the exchange; writes one new folder and nothing else."""
import base64
import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import ledger_archive as la

PROBE = Path(__file__).resolve().parents[2] / "data" / "probe_activities.json"


def trade(i, aggressor=False):
    return {"type": "ACTIVITY_TYPE_TRADE", "trade": {
        "id": f"T{i}", "marketSlug": f"m{i}", "isAggressor": aggressor,
        "aggressorExecution": {"transactTime": f"2026-01-01T00:00:{i % 60:02d}.123456789Z",
                               "lastShares": "7", "lastPx": {"value": "0.30"},
                               "order": {"id": f"A{i}", "intent": "ORDER_INTENT_BUY_LONG"}},
        "passiveExecution": {"transactTime": f"2026-01-01T00:00:{i % 60:02d}.123456789Z",
                             "lastShares": "7", "lastPx": {"value": "0.30"},
                             "order": {"id": f"P{i}", "intent": "ORDER_INTENT_SELL_LONG"}},
        "realizedPnl": {"value": "-0.5"}}}


class Feed:
    """A fake activities feed: an offset cursor over a list kept newest
    first, which can grow at the top between pages like the real one."""

    def __init__(self, rows, page_eof=True, grow=None):
        self.rows = list(rows)
        self.calls = []
        self.page_eof = page_eof
        self.grow = grow or {}

    def __call__(self, host, path, params):
        self.calls.append((host, path, dict(params)))
        n = len(self.calls)
        if n in self.grow:                   # new trades land at the top
            self.rows[:0] = self.grow[n]
        off = int(params.get("cursor") or 0)
        lim = params["limit"]
        page = self.rows[off:off + lim]
        end = off + lim >= len(self.rows)
        out = {"activities": page}
        if end:
            if self.page_eof:
                out["eof"] = True
        else:
            out["nextCursor"] = str(off + lim)
        return out


class TestTheWalk(unittest.TestCase):
    def test_follows_every_page_to_eof(self):
        rows = [trade(i) for i in range(250)]
        feed = Feed(rows)
        got, info = la.walk_activities(feed, page_size=100, sleep=lambda s: None)
        self.assertEqual(len(got), 250)
        self.assertTrue(info["complete"])
        self.assertEqual(info["pages"], 3)
        self.assertEqual(info["stopped"], "the exchange said eof")
        # no type filter: every kind of row the exchange keeps
        self.assertNotIn("types", feed.calls[0][2])
        self.assertEqual(feed.calls[0][2]["sortOrder"], "SORT_ORDER_DESCENDING")

    def test_a_trade_landing_mid_walk_repeats_a_row_and_skips_none(self):
        rows = [trade(i) for i in range(250)]
        feed = Feed(rows, grow={2: [trade(1000), trade(1001)]})
        got, info = la.walk_activities(feed, page_size=100, sleep=lambda s: None)
        ids = [a["trade"]["id"] for a in got]
        self.assertEqual(len(ids), len(set(ids)))
        for i in range(250):
            self.assertIn(f"T{i}", ids)
        self.assertEqual(info["duplicates"], 2)
        self.assertTrue(info["complete"])

    def test_no_cursor_is_the_end_too(self):
        feed = Feed([trade(i) for i in range(30)], page_eof=False)
        got, info = la.walk_activities(feed, page_size=100, sleep=lambda s: None)
        self.assertEqual(len(got), 30)
        self.assertTrue(info["complete"])
        self.assertEqual(info["stopped"], "no further cursor")

    def test_the_page_bound_says_so_and_is_not_complete(self):
        feed = Feed([trade(i) for i in range(500)])
        got, info = la.walk_activities(feed, page_size=100, max_pages=2,
                                       sleep=lambda s: None)
        self.assertEqual(len(got), 200)
        self.assertFalse(info["complete"])
        self.assertIn("still more pages", info["stopped"])

    def test_an_error_keeps_what_was_read_and_says_where(self):
        feed = Feed([trade(i) for i in range(300)])

        def flaky(host, path, params):
            if params.get("cursor") == "200":
                raise RuntimeError("no answer after 6 tries")
            return feed(host, path, params)
        got, info = la.walk_activities(flaky, page_size=100, sleep=lambda s: None)
        self.assertEqual(len(got), 200)
        self.assertFalse(info["complete"])
        self.assertIn("error on page 3", info["stopped"])

    def test_a_cursor_handed_back_twice_stops_the_walk(self):
        def stuck(host, path, params):
            return {"activities": [trade(len(params))], "nextCursor": "same"}
        got, info = la.walk_activities(stuck, page_size=100, sleep=lambda s: None)
        self.assertFalse(info["complete"])
        self.assertIn("already given", info["stopped"])


class TestKeys(unittest.TestCase):
    def test_each_kind_of_row_keys_by_its_own_id(self):
        self.assertEqual(la.activity_key(trade(3)), "ACTIVITY_TYPE_TRADE|trade|T3")
        dep = {"type": "ACTIVITY_TYPE_ACCOUNT_DEPOSIT",
               "accountBalanceChange": {"id": "D1", "amount": {"value": "50"}}}
        self.assertEqual(la.activity_key(dep),
                         "ACTIVITY_TYPE_ACCOUNT_DEPOSIT|accountBalanceChange|D1")

    def test_a_row_with_no_id_never_collapses_into_another(self):
        a = {"type": "X", "thing": {"amount": 1}}
        b = {"type": "X", "thing": {"amount": 2}}
        self.assertNotEqual(la.activity_key(a), la.activity_key(b))
        self.assertEqual(la.activity_key(a), la.activity_key(json.loads(json.dumps(a))))


class TestEarnings(unittest.TestCase):
    def test_pages_by_token_and_falls_to_the_next_host(self):
        calls = []

        def get(host, path, params):
            calls.append((host, dict(params)))
            if host == la.INCENTIVES_HOSTS[0]:
                raise la.HTTPError(404, "not here", path)
            if not params.get("pageToken"):
                return {"rewards": [{"date": "2026-07-01", "reward": 1.5,
                                     "status": "PAID", "marketSlug": "a"}],
                        "nextPageToken": "p2"}
            return {"rewards": [{"date": "2026-07-02", "reward": "2.25",
                                 "status": "PAID", "marketSlug": "b"}]}
        rows, info = la.walk_earnings(get, "2025-01-01", sleep=lambda s: None)
        self.assertEqual(len(rows), 2)
        self.assertTrue(info["complete"])
        self.assertEqual(info["host"], la.INCENTIVES_HOSTS[1])
        self.assertEqual(calls[1][1]["startDate"], "2025-01-01")
        self.assertEqual(calls[2][1]["pageToken"], "p2")
        self.assertIn("404", info["errors_before"][0])


class TestTheReadableLine(unittest.TestCase):
    def test_our_side_is_the_one_isAggressor_names(self):
        r = la.activity_row(trade(5, aggressor=True))
        self.assertEqual((r["our_side"], r["order_id"], r["intent"]),
                         ("aggressor", "A5", "ORDER_INTENT_BUY_LONG"))
        r = la.activity_row(trade(5, aggressor=False))
        self.assertEqual((r["our_side"], r["order_id"]), ("passive", "P5"))
        self.assertEqual(r["price"], 0.30)
        self.assertEqual(r["shares"], 7.0)
        self.assertEqual(r["realized_pnl"], -0.5)
        self.assertEqual(r["time_et"], "2025-12-31 19:00:05")

    @unittest.skipUnless(PROBE.exists(), "the saved probe is not in this checkout")
    def test_isAggressor_agrees_with_the_redacted_intent_on_the_saved_probe(self):
        rows = json.loads(PROBE.read_text())["activities"]
        single = 0
        for a in rows:
            t = a["trade"]
            real = {k for k in ("aggressor", "passive")
                    if not str(t[f"{k}Execution"]["order"].get("intent", "")
                               ).endswith("UNDEFINED")}
            if len(real) == 1:
                single += 1
                self.assertEqual(la.activity_row(a)["our_side"], real.pop())
        self.assertGreater(single, 0)

    def test_another_kind_of_row_keeps_its_whole_body(self):
        res = {"type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
               "positionResolution": {"id": "R1", "marketSlug": "m",
                                      "updateTime": "2026-02-01T00:00:00Z",
                                      "market": {"slug": "m", "question": "q",
                                                 "description": "long" * 500},
                                      "afterPosition": {"netPosition": 0}}}
        r = la.activity_row(res)
        self.assertEqual((r["id"], r["market"], r["time_utc"]),
                         ("R1", "m", "2026-02-01T00:00:00Z"))
        d = json.loads(r["detail"])
        self.assertEqual(d["positionResolution"]["afterPosition"], {"netPosition": 0})
        self.assertNotIn("description", d["positionResolution"]["market"])
        self.assertIn("description", res["positionResolution"]["market"])  # untouched


class TestWritingOnce(unittest.TestCase):
    def _write(self, root, acts, stamp="2026-10-04T1500Z"):
        return la.write_archive(
            Path(root), stamp, acts, {"complete": True, "stopped": "eof", "pages": 1,
                                      "duplicates": 0},
            [{"date": "2026-07-01", "reward": "1.25", "status": "PAID",
              "marketSlug": "a", "programType": "x", "extra": 1}],
            {"complete": True, "stopped": "done", "start": "2025-01-01",
             "host": "h", "pages": 1},
            {"positions": {"m": {"netPosition": 3}}, "balances": {"rows": []},
             "positions_read": {"n": 1}, "errors": []},
            {"pulled_utc": "2026-10-04T15:00:00Z"})

    def test_everything_lands_in_one_new_folder_raw_and_exact(self):
        acts = [trade(i) for i in range(5)]
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "data").mkdir()
            (Path(root) / "data" / "trades.csv").write_text("old record\n")
            with mock.patch.object(la, "CHUNK_ROWS", 2):
                out = self._write(root, acts)
            self.assertEqual(out, Path(root) / "data" / "ledger" / "2026-10-04T1500Z")
            self.assertEqual((Path(root) / "data" / "trades.csv").read_text(), "old record\n")
            every = sorted(p.relative_to(root).as_posix()
                           for p in Path(root).rglob("*") if p.is_file())
            self.assertTrue(all(p.startswith("data/ledger/2026-10-04T1500Z/")
                                or p == "data/trades.csv" for p in every))
            chunks = sorted(out.glob("activities-*.jsonl.gz"))
            self.assertEqual(len(chunks), 3)
            back = [json.loads(l) for c in chunks
                    for l in gzip.decompress(c.read_bytes()).decode().splitlines()]
            self.assertEqual(back, acts)
            man = json.loads((out / "manifest.json").read_text())
            for f in man["files"]:
                self.assertEqual(hashlib.sha256((out / f["file"]).read_bytes()).hexdigest(),
                                 f["sha256"])
            self.assertEqual(man["activities"]["rows"], 5)
            self.assertEqual(man["earnings"]["paid_usd"], 1.25)
            self.assertIn("✅ Complete", (out / "README.md").read_text())
            lines = (out / "activities.csv").read_text().splitlines()
            self.assertEqual(len(lines), 6)

    def test_an_existing_folder_is_never_written_over(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "data" / "ledger" / "2026-10-04T1500Z"
            out.mkdir(parents=True)
            (out / "README.md").write_text("first pull")
            with self.assertRaises(FileExistsError):
                self._write(root, [trade(1)])
            self.assertEqual((out / "README.md").read_text(), "first pull")
            self.assertEqual([p.name for p in out.iterdir()], ["README.md"])

    def test_an_incomplete_walk_says_so_up_top(self):
        with tempfile.TemporaryDirectory() as root:
            out = la.write_archive(
                Path(root), "s", [], {"complete": False, "pages": 3, "duplicates": 0,
                                      "stopped": "error on page 4: HTTP 429"},
                [], {"complete": True, "stopped": "", "start": "x", "host": None,
                     "pages": 0},
                {"positions": {}, "balances": None, "errors": []}, {"pulled_utc": "t"})
            text = (out / "README.md").read_text()
            self.assertIn("INCOMPLETE", text)
            self.assertIn("error on page 4", text)


class TestTheSignedRead(unittest.TestCase):
    class Resp:
        def __init__(self, status, body=None, headers=None):
            self.status_code, self._b = status, body or {}
            self.headers = headers or {}
            self.text = json.dumps(self._b)

        def json(self):
            return self._b

    def _getter(self, answers):
        sess = mock.Mock()
        sess.get.side_effect = answers
        waits = []
        seed = base64.b64encode(bytes(range(32))).decode()
        return la.make_getter("kid", seed, session=sess, sleep=waits.append), sess, waits

    def test_a_429_waits_what_the_exchange_names(self):
        get, sess, waits = self._getter([self.Resp(429, headers={"Retry-After": "7"}),
                                         self.Resp(200, {"ok": 1})])
        self.assertEqual(get(la.TRADE_API, la.ACTIVITIES_PATH, {}), {"ok": 1})
        self.assertEqual(waits, [7.0])
        hdr = sess.get.call_args.kwargs["headers"]
        self.assertEqual(hdr["X-PM-Access-Key"], "kid")

    def test_a_4xx_is_an_answer_not_retried(self):
        get, sess, waits = self._getter([self.Resp(403, {"error": "no"})])
        with self.assertRaises(la.HTTPError):
            get(la.TRADE_API, la.ACTIVITIES_PATH, {})
        self.assertEqual(sess.get.call_count, 1)

    def test_a_5xx_backs_off_then_gives_up_saying_why(self):
        get, sess, waits = self._getter([self.Resp(502)] * la.TRIES)
        with self.assertRaises(RuntimeError) as e:
            get(la.TRADE_API, la.ACTIVITIES_PATH, {})
        self.assertIn("HTTP 502", str(e.exception))
        self.assertEqual(waits, [2, 4, 8, 16, 32])   # none after the last


class TestTheWholeRun(unittest.TestCase):
    """main() end to end on a fake exchange: the folder, the step output
    the workflow commits by, and the exit code that turns the run red."""

    def _run(self, earnings_get, env_extra=None):
        feed = Feed([trade(i) for i in range(120)])

        def get(host, path, params):
            if path == la.ACTIVITIES_PATH:
                return feed(host, path, params)
            if path == la.EARNINGS_PATH:
                return earnings_get(host, path, params)
            if path == la.POSITIONS_PATH:
                return {"positions": {"m": {"netPosition": 1}}, "eof": True}
            return {"balances": [{"buyingPower": 1}]}
        with tempfile.TemporaryDirectory() as root:
            out_file = Path(root) / "gh_out"
            env = {"POLYMARKET_KEY_ID": "k", "POLYMARKET_SECRET_KEY": "s",
                   "ARCHIVE_ROOT": root, "GITHUB_OUTPUT": str(out_file),
                   **(env_extra or {})}
            with mock.patch.dict("os.environ", env), \
                    mock.patch.object(la, "make_getter", return_value=get), \
                    mock.patch.object(la.time, "sleep"):
                code = la.main()
            stamp = out_file.read_text().strip().split("=", 1)[1]
            man = json.loads((Path(root) / "data" / "ledger" / stamp
                              / "manifest.json").read_text())
            return code, man

    def test_a_complete_pull_exits_clean_and_names_its_folder(self):
        code, man = self._run(lambda h, p, q: {"rewards": [{"date": "2026-07-01",
                                                            "reward": 2, "status": "PAID"}]})
        self.assertEqual(code, 0)
        self.assertEqual(man["activities"]["rows"], 120)
        self.assertEqual(man["earnings"]["rows"], 1)
        self.assertEqual(man["earnings"]["starts_tried"][0]["start"], "2025-01-01")

    def test_an_early_start_refused_falls_back_to_the_known_date(self):
        def earn(host, path, params):
            if params["startDate"] < "2026-01-01":
                raise la.HTTPError(400, "startDate out of range", path)
            return {"rewards": [{"date": "2026-07-01", "reward": 2, "status": "PAID"}]}
        code, man = self._run(earn)
        self.assertEqual(code, 0)
        self.assertEqual([t["start"] for t in man["earnings"]["starts_tried"]],
                         ["2025-01-01", la.FALLBACK_EARNINGS_START])
        self.assertEqual(man["earnings"]["start"], la.FALLBACK_EARNINGS_START)

    def test_payouts_never_read_turn_the_run_red_but_keep_the_trades(self):
        def earn(host, path, params):
            raise la.HTTPError(403, "no", path)
        code, man = self._run(earn)
        self.assertEqual(code, 2)
        self.assertEqual(man["activities"]["rows"], 120)
        self.assertFalse(man["earnings"]["complete"])


if __name__ == "__main__":
    unittest.main()
