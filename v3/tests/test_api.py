"""Offline tests for the API client: signing, retry discipline, parsing.

No network — a stub session plays the exchange, with payload shapes
copied from real 1.0 captures.
"""

import base64
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from v3 import api as api_mod
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


GW_BOOK = "https://gateway.polymarket.us/v1/markets/m/book"
API_BAL = "https://api.polymarket.us/v1/balances"


def paced_client(*responses, clock0=1_000_000.0):
    """A client on a fake clock that only sleep moves."""
    t = {"now": clock0}
    slept = []

    def sleep(s):
        slept.append(round(s, 3))
        t["now"] += s
    c = Client(key_id="kid", secret_key=SECRET_B64, session=StubSession(responses),
               sleep=sleep, clock=lambda: t["now"])
    return c, t, slept


class TestTheGatewayThrottle(unittest.TestCase):
    """Owner, 2026-09-12 "Yes to both": a 429 from the gateway holds every
    gateway read on every thread for the wait the exchange names, gateway
    reads are paced across threads, the tender's take the next slot, and
    every 429 is recorded with its Retry-After."""

    def test_a_429_on_the_gateway_holds_every_gateway_read(self):
        c, t, slept = paced_client(
            FakeResponse(429, headers={"Retry-After": "30"}, text="Too Many Requests"),
            FakeResponse(200, {"ok": 1}),
            FakeResponse(200, {"book": {"bids": [], "offers": []}}))
        said = []
        c.on_throttle = said.append
        with self.assertRaises(ApiError) as ctx:
            c.get(GW_BOOK, tries=1)                    # a one-try read: refused with the wait
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("Retry-After 30", str(ctx.exception))
        self.assertIn("every gateway read held 30s", str(ctx.exception))
        self.assertAlmostEqual(c.gateway_hold(), 30.0)
        self.assertEqual(len(said), 1)
        self.assertEqual((said[0]["path"], said[0]["retry_after"], said[0]["wait"]),
                         ("/v1/markets/m/book", "30", 30.0))
        # another one-try gateway read is refused at once — nothing sent
        with self.assertRaises(ApiError) as ctx:
            c.get(GW_BOOK, tries=1)
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("held 30s more", str(ctx.exception))
        self.assertEqual(len(c.session.calls), 1)
        # the api host is not held
        self.assertEqual(c.get(API_BAL), {"ok": 1})
        self.assertEqual(len(c.session.calls), 2)
        # an ordinary retried read is refused at once too (2026-09-17: the
        # families' reads waiting out holds four tries deep ran every
        # cycle 37-43 minutes); a PRIORITY read waits the hold out, then sends
        with self.assertRaises(ApiError):
            c.get(GW_BOOK)
        self.assertEqual(len(c.session.calls), 2)
        self.assertEqual(c.get(GW_BOOK, priority=True)["book"]["bids"], [])
        self.assertIn(30.0, slept)
        self.assertEqual(c.gateway_hold(), 0.0)
        self.assertEqual(len(c.throttles), 1)
        self.assertEqual(c.throttles[0]["host"], "gateway.polymarket.us")

    def test_the_wait_is_the_exchanges_own_or_the_default(self):
        import email.utils
        c, t, _ = paced_client(FakeResponse(429))                      # no header
        with self.assertRaises(ApiError):
            c.get(GW_BOOK, tries=1)
        self.assertAlmostEqual(c.gateway_hold(), api_mod.GATEWAY_HOLD_DEFAULT_S)
        stamp = email.utils.formatdate(1_000_000.0 + 45, usegmt=True)   # an HTTP date
        c, t, _ = paced_client(FakeResponse(429, headers={"Retry-After": stamp}))
        with self.assertRaises(ApiError):
            c.get(GW_BOOK, tries=1)
        self.assertAlmostEqual(c.gateway_hold(), 45.0)
        c, t, _ = paced_client(FakeResponse(429, headers={"Retry-After": "600"}))
        with self.assertRaises(ApiError):
            c.get(GW_BOOK, tries=1)
        self.assertAlmostEqual(c.gateway_hold(), api_mod.GATEWAY_HOLD_MAX_S)

    def test_a_429_on_the_api_host_is_recorded_but_holds_no_gateway_read(self):
        c, t, slept = paced_client(FakeResponse(429, headers={"Retry-After": "5"}),
                                   FakeResponse(200, {"book": {}}))
        with self.assertRaises(ApiError) as ctx:
            c.get(API_BAL, tries=1)
        self.assertIn("this read held 5s", str(ctx.exception))
        self.assertEqual(c.gateway_hold(), 0.0)
        self.assertEqual(c.throttles[-1]["host"], "api.polymarket.us")
        self.assertEqual(c.get(GW_BOOK, tries=1), {"book": {}})

    def test_gateway_reads_are_paced_and_api_reads_are_not(self):
        c, t, slept = paced_client(*[FakeResponse(200, {"n": i}) for i in range(5)])
        gap = round(1.0 / api_mod.GATEWAY_PACE_PER_S, 3)
        for _ in range(3):
            c.get(GW_BOOK)
        self.assertEqual(slept, [gap, gap])
        c.get(API_BAL)
        c.get(API_BAL)
        self.assertEqual(slept, [gap, gap])

    def test_a_priority_read_takes_the_slot_ahead_of_an_ordinary_one(self):
        c, t, slept = paced_client(FakeResponse(200, {"n": 1}), FakeResponse(200, {"n": 2}))
        gap = round(1.0 / api_mod.GATEWAY_PACE_PER_S, 3)
        c._gw_prio_waiting = 1                       # the tender is waiting for a slot
        real_sleep = c._sleep

        def sleep(s):                                # ...and takes it during our wait
            real_sleep(s)
            c._gw_prio_waiting = 0
        c._sleep = sleep
        self.assertEqual(c.get(GW_BOOK), {"n": 1})   # the ordinary read yielded once
        self.assertEqual(slept, [gap])
        self.assertEqual(c.get(GW_BOOK, priority=True), {"n": 2})


class TestRetry(unittest.TestCase):
    def test_429_honours_retry_after_then_succeeds(self):
        c = client(FakeResponse(429, headers={"Retry-After": "0.01"}),
                   FakeResponse(200, {"ok": True}))
        self.assertEqual(c.get("https://x.test/v1/thing"), {"ok": True})
        self.assertEqual(len(c.session.calls), 2)
        self.assertEqual(len(c.throttles), 1)

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
                                       "keys": ["eof", "nextCursor"], "capped": False})
        # one page with no cursor: one call, as before
        c = client(FakeResponse(200, {"orders": []}))
        self.assertEqual(c.open_orders(), [])
        self.assertEqual(len(c.session.calls), 1)
        self.assertEqual(c.open_read["pages"], 1)
        # the state lookup reads every page too
        c = client(FakeResponse(200, page1), FakeResponse(200, page2))
        self.assertEqual(c.order_state("a2"), "ORDER_STATE_NEW")

    def test_a_long_one_page_list_with_no_paging_field_reads_as_capped(self):
        from unittest import mock
        rows = [{"id": f"o{i}", "state": "ORDER_STATE_NEW", "marketSlug": "m",
                 "side": "SIDE_BUY", "price": {"value": "0.10"}, "quantity": "5"}
                for i in range(249)]
        # the hint is off in practice (a 260-row list read complete on
        # 2026-09-11); with a real cut known, it would read like this
        with mock.patch("v3.api.OPEN_LIST_CAP_HINT", 240):
            c = client(FakeResponse(200, {"orders": rows}))
            c.open_orders()
            self.assertTrue(c.open_read["capped"])
            # the same length with an eof flag is a complete read
            c = client(FakeResponse(200, {"orders": rows, "eof": True}))
            c.open_orders()
            self.assertFalse(c.open_read["capped"])
            # a short list is complete
            c = client(FakeResponse(200, {"orders": rows[:10]}))
            c.open_orders()
            self.assertFalse(c.open_read["capped"])
        # and with the hint off, the same 249 rows read complete
        c = client(FakeResponse(200, {"orders": rows}))
        c.open_orders()
        self.assertFalse(c.open_read["capped"])


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


class TestTheFirstCycleReadsTheGatewayOneTry(unittest.TestCase):
    """Owner, 2026-09-17 ("The meter is busted"): two boots ran 45 minutes
    each behind a throttled gateway, every family read waiting out four
    holds in turn. With client.boot_one_try set for the first cycle a
    held read is refused at once, like the tender's; cleared, the ladder
    is back."""

    def test_a_held_read_is_refused_at_once_during_the_first_cycle(self):
        c, t, slept = paced_client(
            FakeResponse(429, headers={"Retry-After": "30"}, text="Too Many Requests"),
            FakeResponse(200, {"ok": 1}),
            FakeResponse(200, {"ok": 2}))
        with self.assertRaises(ApiError):
            c.get(GW_BOOK, tries=1)                    # the hold is set: 30 s
        self.assertAlmostEqual(c.gateway_hold(), 30.0)
        before = list(slept)
        c.boot_one_try = True
        with self.assertRaises(ApiError) as ctx:
            c.get(GW_BOOK)                             # default tries=4, but the boot keeps time
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("every gateway read held 30s", str(ctx.exception))
        self.assertEqual(list(slept), before)          # it did not wait the hold out
        c.boot_one_try = False
        t["now"] += 31.0                               # the hold expires on its own
        j = c.get(GW_BOOK)                             # cleared and open: an ordinary read
        self.assertEqual(j, {"ok": 1})

    def test_the_flag_is_off_by_default(self):
        c, _t, _slept = paced_client(FakeResponse(200, {"ok": 1}))
        self.assertFalse(c.boot_one_try)


class TestAnOrdinaryReadUnderAHoldIsOneTry(unittest.TestCase):
    """Owner, 2026-09-17 "Yes, ship it": every cycle from 18:51Z ran 37-43
    minutes because each family book read waited out the 429 hold and
    retried into the next one, four tries deep. While a hold stands an
    ordinary gateway read is refused at once; a priority read keeps its
    ladder; and once the hold clears the ladder is back for everyone."""

    def test_refused_at_once_while_the_hold_stands(self):
        c, t, slept = paced_client(
            FakeResponse(429, headers={"Retry-After": "30"}, text="Too Many Requests"),
            FakeResponse(200, {"ok": 1}))
        with self.assertRaises(ApiError):
            c.get(GW_BOOK, tries=1)                    # the hold is set: 30 s
        before = list(slept)
        with self.assertRaises(ApiError) as ctx:
            c.get(GW_BOOK)                             # default tries=4, no priority
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("every gateway read held", str(ctx.exception))
        self.assertEqual(list(slept), before)          # no wait at all
        self.assertFalse(c.boot_one_try)               # not the boot rule: the hold rule

    def test_a_priority_read_still_waits_the_hold_out(self):
        c, t, slept = paced_client(
            FakeResponse(429, headers={"Retry-After": "30"}, text="Too Many Requests"),
            FakeResponse(200, {"ok": 1}))
        with self.assertRaises(ApiError):
            c.get(GW_BOOK, tries=1)
        j = c.get(GW_BOOK, priority=True)              # the sweep's kind of read
        self.assertEqual(j, {"ok": 1})
        self.assertTrue(any(abs(x - 30.0) < 1e-6 for x in slept))

    def test_the_ladder_is_back_once_the_hold_clears(self):
        c, t, slept = paced_client(
            FakeResponse(429, headers={"Retry-After": "30"}, text="Too Many Requests"),
            FakeResponse(429, headers={"Retry-After": "5"}, text="Too Many Requests"),
            FakeResponse(200, {"ok": 2}))
        with self.assertRaises(ApiError):
            c.get(GW_BOOK, tries=1)                    # hold: 30 s
        t["now"] += 31.0                               # the hold expires on its own
        self.assertEqual(c.gateway_hold(), 0.0)
        n0 = len(slept)
        j = c.get(GW_BOOK)                             # open gateway: a fresh 429 is retried
        self.assertEqual(j, {"ok": 2})
        self.assertGreater(len(slept), n0)             # it waited and tried again
