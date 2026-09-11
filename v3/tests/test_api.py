"""Offline tests for the API client: signing, retry discipline, parsing.

No network — a stub session plays the exchange, with payload shapes
copied from real 1.0 captures.
"""

import base64
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from v3.api import ApiError, Client, auth_headers

KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
SECRET_B64 = base64.b64encode(bytes(range(32))).decode()


class FakeResponse:
    def __init__(self, status=200, json_data=None, headers=None, text=""):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._json


class StubSession:
    """Plays canned responses in order and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


def client(*responses):
    return Client(key_id="kid", secret_key=SECRET_B64,
                  session=StubSession(responses), sleep=lambda s: None)


class TestAuth(unittest.TestCase):
    def test_signature_verifies_and_headers_are_complete(self):
        h = auth_headers("kid", SECRET_B64, "GET", "/v1/orders/open", now_ms=1234567890123)
        self.assertEqual(h["X-PM-Access-Key"], "kid")
        self.assertEqual(h["X-PM-Timestamp"], "1234567890123")
        KEY.public_key().verify(  # raises if the signature is wrong
            base64.b64decode(h["X-PM-Signature"]),
            b"1234567890123GET/v1/orders/open",
        )

    def test_64_byte_secret_uses_first_32_as_seed(self):
        h = auth_headers("kid", base64.b64encode(bytes(range(32)) + b"\0" * 32).decode(),
                         "GET", "/x", now_ms=1)
        KEY.public_key().verify(base64.b64decode(h["X-PM-Signature"]), b"1GET/x")


class TestRetry(unittest.TestCase):
    def test_429_honours_retry_after_then_succeeds(self):
        c = client(FakeResponse(429, headers={"Retry-After": "0.01"}),
                   FakeResponse(200, {"ok": True}))
        self.assertEqual(c.get("https://x.test/v1/thing"), {"ok": True})
        self.assertEqual(len(c.session.calls), 2)

    def test_plain_4xx_raises_immediately(self):
        c = client(FakeResponse(403, text="forbidden"))
        with self.assertRaises(ApiError) as ctx:
            c.get("https://x.test/v1/thing")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(len(c.session.calls), 1)

    def test_signed_get_signs_the_path_not_the_query(self):
        c = client(FakeResponse(200, {}))
        c.get("https://api.polymarket.us/v1/orders/open", signed=True,
              params={"pageSize": 5})
        _, _, kw = c.session.calls[0]
        h = kw["headers"]
        ts = h["X-PM-Timestamp"]
        KEY.public_key().verify(base64.b64decode(h["X-PM-Signature"]),
                                f"{ts}GET/v1/orders/open".encode())


class TestOpenListPages(unittest.TestCase):
    def test_the_open_list_is_read_to_its_end(self):
        # 2026-09-11: orders placed, never seen in the list within twelve
        # seconds, found by their withdrawal — a read that stopped at
        # page one would explain exactly that
        page1 = {"orders": [{"id": "a1", "state": "ORDER_STATE_NEW", "marketSlug": "m1",
                             "side": "SIDE_BUY", "price": {"value": "0.10"}, "quantity": "5"}],
                 "nextCursor": "c2", "eof": False}
        page2 = {"orders": [{"id": "a2", "state": "ORDER_STATE_NEW", "marketSlug": "m2",
                             "side": "SIDE_SELL", "price": {"value": "0.90"}, "quantity": "7"}],
                 "eof": True}
        c = client(FakeResponse(200, page1), FakeResponse(200, page2))
        rows = c.open_orders()
        self.assertEqual([r["id"] for r in rows], ["a1", "a2"])
        self.assertEqual(len(c.session.calls), 2)
        _, _, kw = c.session.calls[1]
        self.assertEqual((kw.get("params") or {}).get("cursor"), "c2")
        self.assertEqual(c.open_read, {"pages": 2, "n": 2, "eof": True,
                                       "keys": ["eof", "nextCursor"]})
        # one page with no cursor: one call, as before
        c = client(FakeResponse(200, {"orders": []}))
        self.assertEqual(c.open_orders(), [])
        self.assertEqual(len(c.session.calls), 1)
        self.assertEqual(c.open_read["pages"], 1)
        # the state lookup reads every page too
        c = client(FakeResponse(200, page1), FakeResponse(200, page2))
        self.assertEqual(c.order_state("a2"), "ORDER_STATE_NEW")


class TestParsing(unittest.TestCase):
    def test_open_orders_filters_dead_states_and_parses_shapes(self):
        payload = {"orders": [
            {"id": "ghost", "state": "ORDER_STATE_REPLACED", "marketSlug": "m1",
             "side": "SIDE_BUY", "price": {"value": "0.08"}, "quantity": "45"},
            {"id": "live1", "state": "ORDER_STATE_OPEN", "marketSlug": "m1",
             "side": "SIDE_BUY", "price": {"value": "0.08"},
             "quantity": "45", "leavesQuantity": "44.5",
             "createTime": "2026-08-18T12:00:00Z",
             "manualOrderIndicator": "ORDER_ENTRY_MANUAL",
             "marketMetadata": {"title": "Senate seats",
                                "subject": {"name": "GOP 52", "image": "http://img"}}},
        ]}
        c = client(FakeResponse(200, payload))
        orders = c.open_orders()
        self.assertEqual(len(orders), 1)
        o = orders[0]
        self.assertEqual(o["id"], "live1")
        self.assertEqual(o["side"], "BUY")
        self.assertAlmostEqual(o["price"], 0.08)
        self.assertAlmostEqual(o["size"], 44.5)  # leavesQuantity wins
        self.assertTrue(o["manual"])
        self.assertEqual(o["subject"], "GOP 52")

    def test_buying_power_is_not_the_first_row(self):
        # The 1.0 bug: a zero row before the funded one read as $0 and
        # silently blocked the qualifier. Parse every row, nested shapes too.
        payload = {"balances": [
            {"asset": "X", "buyingPower": 0},
            {"asset": "USDC", "buyingPower": {"value": "209.53"}},
        ]}
        c = client(FakeResponse(200, payload))
        self.assertAlmostEqual(c.buying_power(), 209.53)

    def test_buying_power_none_when_absent(self):
        c = client(FakeResponse(200, {"balances": [{"asset": "X"}]}))
        self.assertIsNone(c.buying_power())

    def test_book_normalizes_and_infers_tick(self):
        payload = {"book": {
            "bids": [{"px": "0.44", "qty": "100"}, {"px": "0.45", "qty": "5"}],
            "offers": [{"px": "0.46", "qty": "7"}],
        }}
        c = client(FakeResponse(200, payload))
        b = c.book("scc-x", fetched_at=123.0)
        self.assertEqual(b.bids[0], (0.45, 5.0))  # sorted best first
        self.assertEqual(b.asks[0], (0.46, 7.0))
        self.assertEqual(b.tick, 0.01)
        self.assertEqual(b.fetched_at, 123.0)

    def test_earnings_paginates_and_sorts(self):
        c = client(
            FakeResponse(200, {"rewards": [
                {"date": "2026-08-16", "marketSlug": "b", "programType": "lp",
                 "reward": 1.5, "status": "pending"}], "nextPageToken": "t2"}),
            FakeResponse(200, {"rewards": [
                {"date": "2026-08-15", "marketSlug": "a", "programType": "lp",
                 "reward": 2.0, "status": "paid"}]}),
        )
        rows = c._earnings_from("https://x.test", "2026-03-21")
        self.assertEqual([r["date"] for r in rows], ["2026-08-15", "2026-08-16"])
        self.assertEqual(rows[0]["status"], "PAID")

    def test_programs_falls_back_to_second_host(self):
        # The prod host 500s through all four retries; the trade host answers.
        c = client(
            FakeResponse(500, text="boom"), FakeResponse(500, text="boom"),
            FakeResponse(500, text="boom"), FakeResponse(500, text="boom"),
            FakeResponse(200, {"programs": [{"marketSlug": "m1", "timePeriods": []}]}),
        )
        got = c.programs(["m1"])
        self.assertIn("m1", got)
        # the fallback call was signed (it went to the trade host)
        _, url, kw = c.session.calls[-1]
        self.assertIn("api.polymarket.us", url)
        self.assertIn("X-PM-Signature", kw["headers"])

    def test_programs_merges_rows_for_a_market_in_two_programs(self):
        # 2026-08-28: a market in both July's tier and the elections
        # boost arrives as two rows with the same marketSlug — both
        # sets of periods must survive for pick_period to choose from.
        c = client(FakeResponse(200, {"programs": [
            {"marketSlug": "m1", "timePeriods": [
                {"programId": "politics_low_20260727", "status": "LIVE"}]},
            {"marketSlug": "m1", "timePeriods": [
                {"programId": "elections_boosted_high_20260827", "status": "LIVE"}]},
        ]}))
        got = c.programs(["m1"])
        pids = [tp["programId"] for tp in got["m1"]["timePeriods"]]
        self.assertEqual(sorted(pids), ["elections_boosted_high_20260827",
                                        "politics_low_20260727"])


if __name__ == "__main__":
    unittest.main()
