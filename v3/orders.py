"""Order rails: the ONLY module that touches order endpoints.

Everything here was paid for in 1.0:

* **The modify endpoint is never called.** It has reported success,
  cancelled the original, and never placed the replacement — every order
  it touched on 2026-08-11 was destroyed. There is no function in this
  module that calls it, and none may be added. A price or size change is
  place -> verify -> cancel: place the replacement, confirm it is
  genuinely resting by ORDER ID and minimum quantity, and only then
  cancel the original. If anything fails before the cancel, the original
  is untouched. If the cancel itself fails we briefly hold two orders,
  which costs a little size and is far better than losing our place.

* **Post-only on every placement** (participateDontInitiate): the order
  rests or is rejected, it can never cross the spread and fill on
  arrival.

* **GTC only.** DAY orders silently expire at 5:00 PM ET (the
  vanished-orders incident).

* **Price serialized as a string.** The API rejects a float price value
  (settled by a controlled A/B in 1.0).

* **Never rest through the other side**: a bid stays below the best ask,
  an ask above the best bid — checked against a FRESH book, and refused
  outright when no fresh book exists (fail closed).

* **Verification is polling, not a glance**: the open-order list lags
  placements by ~4 s. And it checks remaining quantity, because the
  exchange silently trims placements (a 2,000-share ask once came back
  resting 273.04).

* **Sizes are fractional** — quantities round to 2 decimals, never to
  integers (int() once made an order's own verification unwinnable).

Whitelist, price bounds, and the master switch are enforced here, at the
choke point, so no caller can forget them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .api import TRADE_API, ApiError, Client
from .intents import REST_SIDE, SELL_LONG, SELL_SHORT, intent_for

PRICE_MIN, PRICE_MAX = 0.001, 0.999
QTY_MIN, QTY_MAX = 0.01, 20000.0
BOOK_MAX_AGE = 120.0        # never place against a book older than this
VERIFY_MAX_WAIT = 12.0      # open-order list lags placements ~4s; poll up to this
GTC = "TIME_IN_FORCE_GOOD_TILL_CANCEL"


def price_str(price: float) -> str:
    """The API's own price serialization — a string, trailing zeros
    stripped ('0.08' not '0.080', '0.5' not '0.50')."""
    return f"{price:.3f}".rstrip("0").rstrip(".")


def snap_price(price: float, tick: float, side: str) -> float:
    """Snap an outgoing price to the book's own price grid (owner,
    2026-08-26: "decimal prices are not [fine] on most books. Some are
    okay like house and senate party control"). Exit prices built from
    break-even and model-fair arithmetic land on arbitrary decimals — a
    real exit was resting at 5.90676c — and books that only take whole
    cents reject or silently round them. Bids snap DOWN and asks snap
    UP, so every floor, cap and never-cross guard only gets safer.
    Tenth-cent books keep their finer grid: the grid is the book's
    tick, not an assumption."""
    if tick <= 0:
        return round(price, 3)
    import math
    steps = price / tick
    snapped = (math.floor(steps + 1e-9) if side == "BUY"
               else math.ceil(steps - 1e-9)) * tick
    return round(snapped, 3)


# The placement breaker (owner, 2026-09-05, "Yes"). At the 01:29Z restart
# the exchange began refusing every placement from the server's address
# — HTTP 403 {"code":7,"message":"Your connection looks like a VPN"} —
# while cancels still went through. Everything that cancels an order to
# re-place it then cancels fine and cannot re-place: 44 exit re-rests
# failed in 18 minutes and the bond rail pulled bids it could not put
# back. So: the desk remembers the refusal; while it stands, the engine
# cancels nothing it means to replace, one placement a minute probes
# for recovery, and the owner is told once when it starts and once when
# it clears. A refused placement is retried on a fresh connection in
# case the host's shared outbound pool rotates.
PLACE_PROBE_S = 60.0
PLACE_RETRY_N = 2
PLACE_RETRY_S = 3.0


def is_vpn_refusal(err) -> bool:
    s = str(err)
    return (getattr(err, "status", None) == 403
            and ("looks like a VPN" in s or '"code":7' in s.replace(" ", "")))


class PlaceHealth:
    """Is the exchange accepting our placements? Shared by every desk in
    the process — the address is the same for all of them."""

    def __init__(self, clock=None, on_change=None):
        self._clock = clock if clock is not None else time.time
        self.on_change = on_change          # callable(blocked: bool, note)
        self.blocked_since: float | None = None
        self.last_refused: float = 0.0
        self.refused_n: int = 0
        self.ok_at: float = 0.0
        self.note: str = ""

    def blocked(self) -> bool:
        return self.blocked_since is not None

    def probe_due(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        return self.blocked() and now - self.last_refused >= PLACE_PROBE_S

    def refused(self, note: str, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        first = self.blocked_since is None
        if first:
            self.blocked_since = now
        self.last_refused = now
        self.refused_n += 1
        self.note = note[:160]
        if first and self.on_change is not None:
            self.on_change(True, self.note)

    def accepted(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self.ok_at = now
        if self.blocked_since is not None:
            since, n = self.blocked_since, self.refused_n
            self.blocked_since = None
            self.refused_n = 0
            if self.on_change is not None:
                self.on_change(False, f"accepted again after {n} refusals "
                                      f"over {(now - since) / 60:.0f} min")

    def view(self) -> dict:
        return {"blocked": self.blocked(), "since": self.blocked_since or 0.0,
                "last_refused": self.last_refused, "refused": self.refused_n,
                "ok_at": self.ok_at, "note": self.note}


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    note: str                     # plain-English what happened
    order_id: str = ""            # the resting order's id when ok
    intent: str = ""
    two_orders: bool = False      # reprice edge: replacement rests, original cancel failed
    price: float = 0.0            # the price actually sent (snapped to the
                                  # book's grid) — callers record THIS, so
                                  # our books show orders where they rest
    resting_qty: float = 0.0      # what the open list showed resting when the
                                  # verify fell short (the exchange trims an
                                  # order to the money there, 2026-09-04)


class OrderDesk:
    """All order-touching operations, behind the rails.

    Collaborators are injected so every path is testable offline:
      client     — v2.api.Client (its .post never blind-retries)
      whitelist  — callable(slug) -> bool; refuse any market it rejects
      switch_on  — callable() -> bool; the master switch. Gates every
                   call with initiator="auto". initiator="owner" (a tap
                   on an authenticated page) bypasses the switch but no
                   other rail.
      fresh_book — callable(slug) -> Book | None; must return a book
                   fetched within BOOK_MAX_AGE, else None (fail closed)
      log        — callable(dict); every attempt, refusal, placement,
                   verification and cancel is recorded (audit trail)
    """

    def __init__(self, client: Client, whitelist, switch_on, fresh_book, log,
                 sleep=None, clock=None, closing_only=None, tick_for=None,
                 own_at=None, health=None):
        self.client = client
        # own_at(slug, book_side, price) -> shares of OURS resting at that
        # level. We cannot buy our own orders (owner, 2026-09-02): a take
        # is measured against the best level that is not ours and what
        # OTHERS show there
        self.own_at = own_at or (lambda slug, side, px: 0.0)
        self.whitelist = whitelist
        self.switch_on = switch_on
        self.fresh_book = fresh_book
        # the price grid is a property of the MARKET, not of how recent
        # our book is — resolving it separately from fresh_book closes
        # the window where a 2-to-15-minute-old book meant no snapping
        # at all (owner, 2026-08-31: "Confirm that no systems are
        # placing orders with decimal prices unless you verify the
        # order book accepts these orders through the book terms")
        self.tick_for = tick_for
        self.log = log
        self._sleep = sleep if sleep is not None else time.sleep
        self._clock = clock if clock is not None else time.time
        # markets OFF the whitelist where REDUCING exposure is allowed —
        # the unwind list. Only SELL_LONG (sell held stock) and SELL_SHORT
        # (buy back a short) pass here; opening anything stays refused.
        self.closing_only = set(closing_only or ())
        # the placement breaker, shared across desks when the monitor
        # hands one in (one address, one answer)
        self.health = health if health is not None else PlaceHealth(clock=self._clock)

    # -- rails ---------------------------------------------------------------

    def _refuse(self, op: str, slug: str, note: str) -> OrderResult:
        self.log({"op": op, "market": slug, "refused": note, "ts": self._clock()})
        return OrderResult(ok=False, note=f"refused: {note}")

    def _check(self, op: str, slug: str, side: str, price: float, qty: float,
               initiator: str, intent: str | None = None,
               taker: bool = False) -> str | None:
        """The rail checks shared by place and reprice. Returns a refusal
        reason or None. Order matters: cheap checks first, the book last."""
        if not self.whitelist(slug):
            if not (slug in self.closing_only
                    and intent in (SELL_LONG, SELL_SHORT)):
                return f"market {slug} is not on the whitelist"
        if initiator != "owner" and not self.switch_on():
            return "master switch is off"
        if not (PRICE_MIN - 1e-12 <= price <= PRICE_MAX + 1e-12):
            return f"price {price * 100:g}c outside 0.1-99.9c"
        if not (QTY_MIN <= qty <= QTY_MAX):
            return f"quantity {qty:g} outside {QTY_MIN}-{QTY_MAX:g}"
        book = self.fresh_book(slug)
        if book is None:
            return f"no book fresher than {BOOK_MAX_AGE:g}s — refusing to place blind"
        if taker == "bond":
            # the SECOND carved exception (owner, 2026-09-02: "take a
            # resting order for proceeds rather than place and
            # potentially have that capital be used elsewhere"): the
            # bond reinvestment lifts the touch on purpose — owner's
            # rail only, never past the touch, never more than it shows
            from .intents import BUY_LONG, BUY_SHORT
            if initiator != "owner":
                return "bond taker orders are the owner's rail only"
            if (side, intent) == ("BUY", BUY_LONG):
                far, word = "SELL", "ask"
            elif (side, intent) == ("SELL", BUY_SHORT):
                far, word = "BUY", "bid"
            elif (side, intent) == ("SELL", SELL_LONG):
                # closing: held YES sold into the bids (owner, 2026-09-04:
                # "sell my mass gov rep shares to the orders resting at
                # 98 cents") — same rail: the touch not ours, its size
                far, word = "BUY", "bid"
            elif (side, intent) == ("BUY", SELL_SHORT):
                far, word = "SELL", "ask"         # a NO bond closed against the YES asks
            else:
                return "bond taker orders may only open or close a bond at the touch"
            # the levels on the far side net of our own orders: the best
            # one that is not entirely ours is the touch we may take
            others = []
            for p, q in book.side(far):
                avail = q - float(self.own_at(slug, far, p) or 0.0)
                if avail > 1e-9:
                    others.append((p, avail))
            if not others:
                return f"no {word} to take that is not our own"
            best_px, best_q = others[0]
            worse = (price > best_px + 1e-12) if far == "SELL" else (price < best_px - 1e-12)
            if worse:
                return (f"taker {'bid' if far == 'SELL' else 'ask'} "
                        f"{price * 100:g}c is past the best {word} not ours, "
                        f"{best_px * 100:g}c — never worse than the touch")
            at_level = next((q for p, q in others if abs(p - price) < 1e-9), 0.0)
            if qty > at_level + 1e-9:
                return (f"taker for {qty:g} exceeds the {at_level:g} others "
                        f"show at {price * 100:g}c")
            return None
        if taker:
            # the ONE carved exception (owner, 2026-08-22): a SELL of
            # held stock limited AT the bid crosses on purpose — but
            # never below the bid, and never anything else
            if side != "SELL" or intent != SELL_LONG:
                return "taker orders may only be SELLs of held stock"
            if book.bids and price < book.bids[0][0] - 1e-12:
                return (f"taker ask {price * 100:g}c is below the bid "
                        f"{book.bids[0][0] * 100:g}c — never worse than "
                        f"the touch")
            return None
        if side == "BUY":
            if book.asks and price >= book.asks[0][0] - 1e-12:
                return (f"bid {price * 100:g}c would cross the best ask "
                        f"{book.asks[0][0] * 100:g}c")
        else:
            if book.bids and price <= book.bids[0][0] + 1e-12:
                return (f"ask {price * 100:g}c would cross the best bid "
                        f"{book.bids[0][0] * 100:g}c")
        return None

    # -- operations ------------------------------------------------------------

    def place_resting(self, slug: str, side: str, price: float, qty: float, *,
                      net_position: float = 0.0, close_short: bool = False,
                      intent: str | None = None, initiator: str = "auto",
                      verify: bool = True, taker: bool = False) -> OrderResult:
        """Place one post-only GTC resting order and (by default) confirm it
        rests. `side` is the BOOK side: BUY = bid, SELL = ask. The intent
        is derived from the position unless the caller pins it (a reprice
        keeps the original's)."""
        qty = round(qty, 2)
        # The price grid is a property of the MARKET, not of how recent
        # our book is: the exchange's own figure where it gives one,
        # else the last book of any age. Snapped BEFORE the rails, so
        # every bound and never-cross guard sees the price that will
        # really be sent (owner, 2026-08-31).
        tick0 = self.tick_for(slug) if self.tick_for else None
        if not tick0:
            book0 = self.fresh_book(slug)
            tick0 = book0.tick if book0 is not None else None
        if tick0:
            price = snap_price(price, tick0, side)
        if intent is None:
            intent = intent_for(side, net_position, qty, close_short)
        reason = self._check("place", slug, side, price, qty, initiator,
                             intent=intent, taker=taker)
        if reason:
            return self._refuse("place", slug, reason)
        # belt: an unsnapped decimal on a whole-cent book is rejected or
        # SILENTLY ROUNDED, and a silently rounded price is one nobody
        # chose. The blind-book rail above already covers this in
        # practice; this fails closed if it ever stops.
        if not tick0:
            return self._refuse("place", slug,
                                "no price grid known for this market — "
                                "refusing rather than sending a price the "
                                "exchange may silently round")
        if REST_SIDE[intent] != side:
            return self._refuse("place", slug,
                                f"intent {intent} rests on {REST_SIDE[intent]}, not {side}")
        # the breaker: while the exchange refuses our placements, one
        # attempt a minute probes for recovery and the rest are refused
        # here without a call. The owner's own tap always tries.
        now0 = self._clock()
        if (self.health.blocked() and initiator != "owner"
                and not self.health.probe_due(now0)):
            since = time.strftime("%H:%M", time.gmtime(self.health.blocked_since or now0))
            return self._refuse("place", slug,
                                f"placements blocked — the exchange refused the "
                                f"last one as a VPN ({self.health.refused_n} since "
                                f"{since}Z); probing once a minute")
        body = {
            "marketSlug": slug,
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": price_str(price), "currency": "USD"},
            "quantity": qty,
            "tif": GTC,
            # post-only everywhere, with ONE carved exception (owner,
            # 2026-08-22): the taker dump, a SELL of held stock limited
            # at the bid — see CLAUDE.md
            "participateDontInitiate": not taker,
        }
        resp = None
        err: ApiError | None = None
        for attempt in range(PLACE_RETRY_N + 1):
            try:
                resp = self.client.post(TRADE_API + "/v1/orders", body, path="/v1/orders")
                break
            except ApiError as e:
                err = e
                if not is_vpn_refusal(e):
                    break           # a real answer about THIS order: no retry
                # a definite refusal, not a timeout — re-sending cannot
                # double the order. Try again on a fresh connection in
                # case the host's shared outbound pool rotates.
                self.health.refused(str(e), self._clock())
                if attempt < PLACE_RETRY_N:
                    fresh = getattr(self.client, "fresh_connection", None)
                    if fresh is not None:
                        fresh()
                    self._sleep(PLACE_RETRY_S)
        if resp is None:
            self.log({"op": "place", "market": slug, "error": str(err), "ts": self._clock()})
            return OrderResult(ok=False, note=f"placement failed: {err}",
                               price=price)
        self.health.accepted(self._clock())
        order_id = str((resp.get("order") or {}).get("id") or resp.get("id")
                       or resp.get("orderId") or "")
        self.log({"op": "place", "market": slug, "side": side, "price": price,
                  "qty": qty, "intent": intent, "id": order_id, "initiator": initiator,
                  "ts": self._clock()})
        if not verify:
            return OrderResult(ok=True, note="placed (unverified)",
                               order_id=order_id, intent=intent, price=price)
        ok, note, seen = self.verify_resting(slug, side, price, want_id=order_id,
                                             min_qty=qty)
        if not ok:
            # 2xx that never rests happens (and post-only rejections land
            # here too). Report it; the order id, if any, lets the caller
            # clean up. Never re-post: the first may still land late.
            return OrderResult(ok=False, note=f"placed but not resting: {note}",
                               order_id=order_id, intent=intent, price=price,
                               resting_qty=seen)
        return OrderResult(ok=True, note=note, order_id=order_id, intent=intent,
                           price=price)

    def verify_resting(self, slug: str, side: str, price: float, *,
                       want_id: str, min_qty: float) -> tuple[bool, str, float]:
        """Poll the open-order list until the order is genuinely resting:
        matched by ID (a dead record at the right price must not pass),
        with at least min_qty remaining (the exchange silently trims).
        Returns (ok, note, the size seen resting) — a trimmed order is
        still a real order of ours, and the caller decides what to do
        with it."""
        deadline = self._clock() + VERIFY_MAX_WAIT
        wait = 1.0
        last = "order not seen in the open list"
        seen = 0.0
        while True:
            try:
                for o in self.client.open_orders():
                    if want_id and o["id"] != want_id:
                        continue
                    if not want_id and not (o["market"] == slug and o["side"] == side
                                            and abs(o["price"] - price) < 1e-9):
                        continue
                    if o["size"] >= min_qty - 1e-9:
                        return True, (f"resting: {o['size']:g} @ "
                                      f"{o['price'] * 100:g}c (id {o['id']})"), o["size"]
                    last = f"resting only {o['size']:g} of {min_qty:g}"
                    seen = float(o["size"] or 0.0)
            except ApiError as e:
                last = f"open-orders read failed: {e}"
            if self._clock() >= deadline:
                return False, last, seen
            self._sleep(wait)
            wait = min(wait * 2, 4.0)

    def cancel(self, order_id: str, slug: str, *, initiator: str = "auto") -> OrderResult:
        """Cancel one order. Deliberately NOT gated on the master switch:
        reducing exposure must always be easier than adding it."""
        try:
            self.client.post(TRADE_API + f"/v1/order/{order_id}/cancel",
                             {"marketSlug": slug}, path=f"/v1/order/{order_id}/cancel")
        except ApiError as e:
            self.log({"op": "cancel", "market": slug, "id": order_id,
                      "error": str(e), "ts": self._clock()})
            return OrderResult(ok=False, note=f"cancel failed: {e}", order_id=order_id)
        self.log({"op": "cancel", "market": slug, "id": order_id,
                  "initiator": initiator, "ts": self._clock()})
        return OrderResult(ok=True, note="cancelled", order_id=order_id)

    def cancel_all(self, *, initiator: str) -> OrderResult:
        """The emergency stop. Never gated."""
        try:
            self.client.post(TRADE_API + "/v1/orders/open/cancel", {},
                             path="/v1/orders/open/cancel")
        except ApiError as e:
            return OrderResult(ok=False, note=f"cancel-all failed: {e}")
        self.log({"op": "cancel_all", "initiator": initiator, "ts": self._clock()})
        return OrderResult(ok=True, note="cancel-all sent")

    def reprice(self, existing: dict, new_price: float, new_qty: float | None = None,
                *, initiator: str = "auto") -> OrderResult:
        """Move an order to a new price/size WITHOUT ever risking its loss:
        place the replacement, verify it rests (by id, at full size), and
        only then cancel the original. `existing` needs id, market, side,
        price, size, and intent (the replacement keeps the same intent —
        deriving it fresh could flip a SELL_LONG into a BUY_SHORT)."""
        slug, side = existing["market"], existing["side"]
        qty = round(new_qty if new_qty is not None else existing["size"], 2)
        placed = self.place_resting(
            slug, side, new_price, qty,
            intent=existing.get("intent") or None,
            initiator=initiator, verify=True,
        )
        if not placed.ok:
            if placed.order_id:
                # The unverified replacement may still be live somewhere —
                # withdraw it so we never hold a ghost. Original untouched.
                self.cancel(placed.order_id, slug, initiator=initiator)
            return OrderResult(ok=False, order_id=existing["id"], intent=placed.intent,
                               note=f"original untouched — {placed.note}")
        old = self.cancel(existing["id"], slug, initiator=initiator)
        if not old.ok:
            # Two orders resting: costs a little size, far better than
            # losing our place. Surface it loudly; the caller alerts.
            return OrderResult(ok=True, order_id=placed.order_id, intent=placed.intent,
                               price=placed.price, two_orders=True,
                               note=(f"replacement resting (id {placed.order_id}) but the "
                                     f"original {existing['id']} failed to cancel — "
                                     f"two orders on the book"))
        return OrderResult(ok=True, order_id=placed.order_id, intent=placed.intent,
                           price=placed.price,
                           note=f"repriced: new {placed.order_id} resting, "
                                f"original {existing['id']} cancelled")
