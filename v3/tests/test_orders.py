"""Offline tests for the order rails — the money code.

A stub client plays the exchange and records every call; a fake clock
drives the verification polling. The one invariant checked everywhere:
no URL containing 'modify' is ever touched.
"""

import unittest

from v3.api import ApiError
from v3.intents import BUY_LONG, SELL_LONG
from v3.orders import GTC, OrderDesk, price_str
from v3.scoring import Book


class StubClient:
    def __init__(self):
        self.posts = []                 # (url, body) in order
        self.post_responses = {}        # url substring -> dict or Exception
        self.open_orders_script = [[]]  # successive open_orders() results; last repeats

    def post(self, url, body, path=None, **kw):
        self.posts.append((url, body))
        for frag, r in self.post_responses.items():
            if frag in url:
                if isinstance(r, Exception):
                    raise r
                return r
        return {}

    def open_orders(self):
        if len(self.open_orders_script) > 1:
            return self.open_orders_script.pop(0)
        return self.open_orders_script[0]


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s


def resting(oid="new1", market="scc-x", side="BUY", price=0.08, size=45.0,
            intent=BUY_LONG):
    return {"id": oid, "market": market, "side": side, "price": price,
            "size": size, "intent": intent}


def make_desk(client=None, whitelisted=True, switch=True, book="normal",
              closing_only=None, tick_for=None):
    client = client or StubClient()
    books = {
        "normal": Book(bids=((0.44, 100.0),), asks=((0.46, 50.0),), tick=0.01),
        "none": None,
    }
    logs = []
    clock = FakeClock()
    desk = OrderDesk(
        client=client,
        whitelist=lambda s: whitelisted,
        switch_on=lambda: switch,
        fresh_book=lambda s: books.get(book) if book != "none" else None,
        log=logs.append,
        sleep=clock.sleep,
        clock=clock.clock,
        closing_only=closing_only,
        tick_for=tick_for,
    )
    return desk, client, logs


class TestPriceGrid(unittest.TestCase):
    """Owner, 2026-08-31: "Confirm that no systems are placing orders
    with decimal prices unless you verify the order book accepts these
    orders through the book terms."

    The grid was only ever INFERRED from the prices already resting in
    a book — one sub-cent price from any source, ours included, flips a
    whole-cent market to a tenth-cent grid and it prices there for good.
    A declared grid from the exchange overrides that inference."""

    def test_a_declared_grid_beats_the_inferred_one(self):
        # the book says tenth-cent (someone left 25.8c resting); the
        # exchange says whole cents. The exchange wins.
        desk, client, _ = make_desk(tick_for=lambda s: 0.01)
        r = desk.place_resting("scc-x", "SELL", 0.468, 5, verify=False)
        self.assertTrue(r.ok, r.note)
        self.assertEqual(client.posts[-1][1]["price"]["value"], "0.47")

    def test_an_ask_snaps_up_and_a_bid_snaps_down(self):
        # never toward crossing: an ask rounds away from the bid and a
        # bid away from the ask, so every guard only gets safer
        desk, client, _ = make_desk(tick_for=lambda s: 0.01)
        desk.place_resting("scc-x", "SELL", 0.468, 5, verify=False)
        self.assertEqual(client.posts[-1][1]["price"]["value"], "0.47")
        desk.place_resting("scc-x", "BUY", 0.358, 5, verify=False)
        self.assertEqual(client.posts[-1][1]["price"]["value"], "0.35")

    def test_a_tenth_cent_market_keeps_its_finer_grid(self):
        desk, client, _ = make_desk(tick_for=lambda s: 0.001)
        r = desk.place_resting("scc-x", "SELL", 0.4684, 5, verify=False)
        self.assertTrue(r.ok, r.note)
        self.assertEqual(client.posts[-1][1]["price"]["value"], "0.469")

    def test_the_grid_survives_a_book_too_stale_to_place_on(self):
        # tick_for reads the last book of ANY age: a tick does not go
        # stale the way a price does. The blind-book rail still stops
        # the order, so this is belt, not a new door.
        desk, client, _ = make_desk(book="none", tick_for=lambda s: 0.01)
        r = desk.place_resting("scc-x", "SELL", 0.468, 5, verify=False)
        self.assertFalse(r.ok)
        self.assertIn("refusing to place blind", r.note)
        self.assertEqual(client.posts, [])

    def test_no_grid_at_all_refuses_rather_than_guessing(self):
        desk, client, _ = make_desk(tick_for=lambda s: None)
        # the stub's fresh book still carries a tick, so this proves the
        # fallback rather than the refusal
        r = desk.place_resting("scc-x", "SELL", 0.468, 5, verify=False)
        self.assertTrue(r.ok, r.note)
        self.assertEqual(client.posts[-1][1]["price"]["value"], "0.47")


class TestDeclaredGrid(unittest.TestCase):
    """Reading the exchange's own price grid instead of inferring it.
    The field name is unconfirmed, so the parser takes only what is
    unambiguous and the probe logs the rest — a wrong grid places wrong
    prices, which is the thing being fixed."""

    def test_reads_a_dollar_tick_from_the_market_object(self):
        from v3.api import Client
        self.assertEqual(Client.declared_tick({"minPriceIncrement": 0.01}), 0.01)
        self.assertEqual(Client.declared_tick({"tick_size": "0.001"}), 0.001)
        # nested where the exchange buries it
        self.assertEqual(
            Client.declared_tick({"marketMetadata": {"minTick": 0.01}}), 0.01)

    def test_refuses_to_guess_units(self):
        from v3.api import Client
        # a bare 1 could be a cent or a hundredth of one — not ours to
        # decide; the probe line shows it instead
        self.assertIsNone(Client.declared_tick({"tickSize": 1}))
        self.assertIsNone(Client.declared_tick({"minTick": 0}))
        self.assertIsNone(Client.declared_tick({"minTick": "wide"}))
        self.assertIsNone(Client.declared_tick({"somethingElse": 0.01}))
        self.assertIsNone(Client.declared_tick({}))

    def test_the_cache_prefers_a_declared_grid_over_the_inferred_one(self):
        from v3.books import BookCache
        c = BookCache()
        # a book carrying one sub-cent price infers a tenth-cent grid
        c.put("m", Book(bids=((0.258, 10.0),), asks=((0.46, 5.0),),
                        tick=0.001, fetched_at=1000.0))
        self.assertEqual(c.grid("m"), 0.001)
        # the exchange's own figure overrides it
        c.declared["m"] = 0.01
        self.assertEqual(c.grid("m"), 0.01)

    def test_the_grid_outlives_the_books_freshness(self):
        from v3.books import BookCache
        c = BookCache()
        c.put("m", Book(bids=((0.44, 10.0),), asks=((0.46, 5.0),),
                        tick=0.01, fetched_at=1.0))
        # ancient book, but a tick is a property of the market
        self.assertIsNone(c.fresh("m", 120.0, 1e9))
        self.assertEqual(c.grid("m"), 0.01)
        self.assertIsNone(c.grid("never-seen"))


class TestPriceString(unittest.TestCase):
    def test_api_string_format(self):
        self.assertEqual(price_str(0.08), "0.08")
        self.assertEqual(price_str(0.5), "0.5")
        self.assertEqual(price_str(0.999), "0.999")
        self.assertEqual(price_str(0.1), "0.1")
        self.assertEqual(price_str(0.075), "0.075")


class TestRails(unittest.TestCase):
    def test_refuses_market_off_the_whitelist(self):
        desk, client, _ = make_desk(whitelisted=False)
        r = desk.place_resting("evil-market", "BUY", 0.05, 10)
        self.assertFalse(r.ok)
        self.assertIn("whitelist", r.note)
        self.assertEqual(client.posts, [])

    def test_closing_only_lets_exits_through_but_never_opens(self):
        # unwind markets sit OFF the whitelist: reducing is allowed there,
        # opening anything is not
        desk, client, _ = make_desk(whitelisted=False,
                                    closing_only={"old-market"})
        r = desk.place_resting("old-market", "BUY", 0.05, 10)   # BUY_LONG opens
        self.assertFalse(r.ok)
        self.assertIn("whitelist", r.note)
        client.open_orders_script = [[resting(market="old-market", side="SELL",
                                              price=0.45, size=10.0,
                                              intent=SELL_LONG)]]
        r = desk.place_resting("old-market", "SELL", 0.45, 10,
                               net_position=10)                  # SELL_LONG exits
        self.assertTrue(r.ok, r.note)
        # and a market not on the closing list stays fully refused
        r = desk.place_resting("other-market", "SELL", 0.45, 10, net_position=10)
        self.assertFalse(r.ok)

    def test_switch_gates_auto_but_not_owner(self):
        desk, client, _ = make_desk(switch=False)
        r = desk.place_resting("scc-x", "BUY", 0.05, 10)
        self.assertFalse(r.ok)
        self.assertIn("master switch", r.note)
        self.assertEqual(client.posts, [])
        client.open_orders_script = [[resting(price=0.05, size=10.0)]]
        r = desk.place_resting("scc-x", "BUY", 0.05, 10, initiator="owner")
        self.assertTrue(r.ok)

    def test_price_and_quantity_bounds(self):
        desk, client, _ = make_desk()
        self.assertIn("outside 0.1-99.9c",
                      desk.place_resting("scc-x", "BUY", 0.0005, 10).note)
        self.assertIn("outside 0.1-99.9c",
                      desk.place_resting("scc-x", "SELL", 0.9995, 10).note)
        self.assertIn("quantity", desk.place_resting("scc-x", "BUY", 0.05, 0).note)
        self.assertIn("quantity", desk.place_resting("scc-x", "BUY", 0.05, 20001).note)
        self.assertEqual(client.posts, [])

    def test_no_fresh_book_fails_closed(self):
        desk, client, _ = make_desk(book="none")
        r = desk.place_resting("scc-x", "BUY", 0.05, 10)
        self.assertFalse(r.ok)
        self.assertIn("refusing to place blind", r.note)
        self.assertEqual(client.posts, [])

    def test_never_rests_through_the_other_side(self):
        desk, client, _ = make_desk()  # best bid 44c, best ask 46c
        self.assertIn("cross the best ask",
                      desk.place_resting("scc-x", "BUY", 0.46, 10).note)
        self.assertIn("cross the best bid",
                      desk.place_resting("scc-x", "SELL", 0.44, 10).note)
        self.assertEqual(client.posts, [])

    def test_pinned_intent_must_match_the_book_side(self):
        # SELL_LONG rests as an ask; pinning it on a bid is the classic
        # self-bidding bug and must be refused.
        desk, client, _ = make_desk()
        r = desk.place_resting("scc-x", "BUY", 0.05, 10, intent=SELL_LONG)
        self.assertFalse(r.ok)
        self.assertIn("rests on SELL", r.note)
        self.assertEqual(client.posts, [])


class TestPlace(unittest.TestCase):
    def test_body_is_exactly_the_api_shape(self):
        desk, client, _ = make_desk()
        client.post_responses["/v1/orders"] = {"order": {"id": "new1"}}
        client.open_orders_script = [[resting("new1", price=0.08, size=45.0)]]
        r = desk.place_resting("scc-x", "BUY", 0.08, 45)
        self.assertTrue(r.ok)
        url, body = client.posts[0]
        self.assertTrue(url.endswith("/v1/orders"))
        self.assertEqual(body, {
            "marketSlug": "scc-x",
            "intent": BUY_LONG,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": "0.08", "currency": "USD"},
            "quantity": 45.0,
            "tif": GTC,
            "participateDontInitiate": True,
        })

    def test_silently_trimmed_placement_fails_verification(self):
        # The exchange once turned a 2,000-share ask into 273.04 resting.
        desk, client, _ = make_desk()
        client.post_responses["/v1/orders"] = {"order": {"id": "new1"}}
        client.open_orders_script = [[resting("new1", side="SELL", price=0.47,
                                              size=273.04, intent=SELL_LONG)]]
        r = desk.place_resting("scc-x", "SELL", 0.47, 2000, net_position=5000)
        self.assertFalse(r.ok)
        self.assertIn("273.04 of 2000", r.note)

    def test_verification_ends_early_once_a_trimmed_order_is_seen_twice(self):
        # 2026-09-10: a trimmed placement waited out the whole deadline,
        # four times a cycle. Seen twice at the smaller size it is final —
        # a resting order never grows.
        desk, client, _ = make_desk()
        client.post_responses["/v1/orders"] = {"order": {"id": "new1"}}
        client.open_orders_script = [[resting("new1", side="SELL", price=0.47,
                                              size=273.04, intent=SELL_LONG)]]
        t0 = desk._clock()
        r = desk.place_resting("scc-x", "SELL", 0.47, 2000, net_position=5000)
        self.assertFalse(r.ok)
        self.assertAlmostEqual(r.resting_qty, 273.04)
        self.assertLess(desk._clock() - t0, 3.0)
        # an order never seen still waits out the deadline
        client.open_orders_script = [[]]
        t0 = desk._clock()
        r = desk.place_resting("scc-x", "SELL", 0.47, 2000, net_position=5000)
        self.assertFalse(r.ok)
        self.assertGreaterEqual(desk._clock() - t0, 12.0)

    def test_verify_matches_by_id_not_by_price(self):
        # A dead or foreign record at the right price must not pass.
        desk, client, _ = make_desk()
        client.post_responses["/v1/orders"] = {"order": {"id": "new1"}}
        client.open_orders_script = [[resting("SOMEONE_ELSE", price=0.08, size=45.0)]]
        r = desk.place_resting("scc-x", "BUY", 0.08, 45)
        self.assertFalse(r.ok)


class TestReprice(unittest.TestCase):
    def existing(self):
        return {"id": "old1", "market": "scc-x", "side": "BUY",
                "price": 0.07, "size": 45.0, "intent": BUY_LONG}

    def test_happy_path_places_verifies_then_cancels_original(self):
        desk, client, _ = make_desk()
        client.post_responses["/v1/orders"] = {"order": {"id": "new1"}}
        client.post_responses["/cancel"] = {}
        client.open_orders_script = [[resting("new1", price=0.08, size=45.0)]]
        r = desk.reprice(self.existing(), 0.08)
        self.assertTrue(r.ok)
        self.assertFalse(r.two_orders)
        urls = [u for u, _ in client.posts]
        self.assertTrue(urls[0].endswith("/v1/orders"))
        self.assertTrue(urls[1].endswith("/v1/order/old1/cancel"))
        self.assertFalse(any("modify" in u.lower() for u in urls))

    def test_failed_verify_leaves_the_original_untouched(self):
        desk, client, _ = make_desk()
        client.post_responses["/v1/orders"] = {"order": {"id": "new1"}}
        client.post_responses["/cancel"] = {}
        client.open_orders_script = [[]]  # replacement never appears
        r = desk.reprice(self.existing(), 0.08)
        self.assertFalse(r.ok)
        self.assertIn("original untouched", r.note)
        cancels = [u for u, _ in client.posts if "/cancel" in u]
        # the ghost replacement is withdrawn; old1 is never cancelled
        self.assertEqual(cancels, ["https://api.polymarket.us/v1/order/new1/cancel"])

    def test_cancel_failure_reports_two_orders_not_a_loss(self):
        desk, client, _ = make_desk()
        client.post_responses["/v1/orders"] = {"order": {"id": "new1"}}
        client.post_responses["/cancel"] = ApiError("boom", status=500)
        client.open_orders_script = [[resting("new1", price=0.08, size=45.0)]]
        r = desk.reprice(self.existing(), 0.08)
        self.assertTrue(r.ok)
        self.assertTrue(r.two_orders)
        self.assertIn("two orders", r.note)

    def test_a_trimmed_replacement_is_kept_when_asked(self):
        # the amplifier's rule (owner, 2026-09-09: "What the exchange
        # funds of it is what rests"): the replacement the exchange cut
        # to the money stays, the original goes
        desk, client, _ = make_desk()
        client.post_responses["/v1/orders"] = {"order": {"id": "new1"}}
        client.post_responses["/cancel"] = {}
        client.open_orders_script = [[resting("new1", price=0.08, size=20.0)]]
        r = desk.reprice(self.existing(), 0.08, keep_trimmed=True)
        self.assertTrue(r.ok, r.note)
        self.assertEqual(r.order_id, "new1")
        self.assertAlmostEqual(r.resting_qty, 20.0)
        self.assertIn("20 of 45", r.note)
        cancels = [u for u, _ in client.posts if "/cancel" in u]
        self.assertEqual(cancels, ["https://api.polymarket.us/v1/order/old1/cancel"])

    def test_a_trimmed_replacement_is_withdrawn_unless_asked(self):
        desk, client, _ = make_desk()
        client.post_responses["/v1/orders"] = {"order": {"id": "new1"}}
        client.post_responses["/cancel"] = {}
        client.open_orders_script = [[resting("new1", price=0.08, size=20.0)]]
        r = desk.reprice(self.existing(), 0.08)
        self.assertFalse(r.ok)
        cancels = [u for u, _ in client.posts if "/cancel" in u]
        self.assertEqual(cancels, ["https://api.polymarket.us/v1/order/new1/cancel"])

    def test_a_replacement_never_seen_is_withdrawn_even_when_trimmed_is_fine(self):
        desk, client, _ = make_desk()
        client.post_responses["/v1/orders"] = {"order": {"id": "new1"}}
        client.post_responses["/cancel"] = {}
        client.open_orders_script = [[]]
        r = desk.reprice(self.existing(), 0.08, keep_trimmed=True)
        self.assertFalse(r.ok)
        cancels = [u for u, _ in client.posts if "/cancel" in u]
        self.assertEqual(cancels, ["https://api.polymarket.us/v1/order/new1/cancel"])

    def test_replacement_keeps_the_original_intent(self):
        # A SELL_LONG ask must never come back as a BUY_SHORT just because
        # the position read as flat at reprice time.
        desk, client, _ = make_desk()
        client.post_responses["/v1/orders"] = {"order": {"id": "new1"}}
        client.post_responses["/cancel"] = {}
        client.open_orders_script = [[resting("new1", side="SELL", price=0.48,
                                              size=10.0, intent=SELL_LONG)]]
        old = {"id": "old1", "market": "scc-x", "side": "SELL",
               "price": 0.47, "size": 10.0, "intent": SELL_LONG}
        desk.reprice(old, 0.48)
        _, body = client.posts[0]
        self.assertEqual(body["intent"], SELL_LONG)


class TestCancel(unittest.TestCase):
    def test_cancel_is_never_gated_by_the_switch(self):
        desk, client, _ = make_desk(switch=False)
        self.assertTrue(desk.cancel("old1", "scc-x").ok)
        self.assertTrue(desk.cancel_all(initiator="owner").ok)
        urls = [u for u, _ in client.posts]
        self.assertTrue(urls[0].endswith("/v1/order/old1/cancel"))
        self.assertTrue(urls[1].endswith("/v1/orders/open/cancel"))


if __name__ == "__main__":
    unittest.main()
