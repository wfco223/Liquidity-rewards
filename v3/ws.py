"""The WebSocket book stream — writer 1 of the book cache.

A close port of 1.0's, which REBUILD.md marked worth keeping: every
frame goes through the same normalizer as the REST fetch so the two
writers produce identical books; the exchange caps a subscription at
200 markets, so held and defended markets subscribe first; a dead or
missing stream degrades to REST polling with no state change anywhere
(the cache's 15-second interlock does that by itself).

Runs as a daemon thread. If the websockets library is missing the
status says so and the system simply polls — the stream is an
optimization, never a dependency.
"""

from __future__ import annotations

import json
import threading
import time

from .api import auth_headers
from .books import BookCache
from .programs import to_num
from .scoring import normalize_book

WS_URL = "wss://api.polymarket.us/v1/ws/markets"
WS_PATH = "/v1/ws/markets"
RECONNECT_WAIT_S = 15.0
RESUBSCRIBE_CHECK_S = 60.0
SUB_CAP = 200
# The cap is per SUBSCRIPTION, so a second connection buys a second 200
# (owner, 2026-09-14, "Do all three for the 429s"). On 2026-09-14, 335
# markets wanted a live book against 200 seats: the 135 with no seat
# were read through the throttled gateway every pass instead, 35 of
# those reads answered 429 in 10.8 minutes and each 429 holds EVERY
# gateway read — 314 s of blackout in a 645 s window, 49% of the time,
# which is why the tender's own pass line read "0 books read, 72 due".
# Each shard is its own connection, its own thread and its own slice of
# the priority-ordered list; a shard that cannot connect degrades to
# REST exactly as one did before, and the others carry on.
STREAM_SHARDS = 2


class Stream:
    """`get_slugs` returns the current subscription list (already
    priority-ordered, ws_priority's job); the stream reconnects by itself
    when that list grows."""

    def __init__(self, cache: BookCache, get_slugs, key_id: str, secret_key: str,
                 shard: int = 0, shards: int = 1, cap: int | None = None,
                 chunk: int | None = None, debounce: bool = False,
                 keep: int = 600, name: str = "ws-books"):
        self.cache = cache
        self.get_slugs = get_slugs
        self.key_id, self.secret_key = key_id, secret_key
        # which slice of the priority-ordered list this connection owns:
        # shard 0 takes the first `cap` (SUB_CAP unless told), shard 1 the
        # next, and so on
        self.shard, self.shards = int(shard), max(int(shards), 1)
        self.cap = int(cap) if cap else SUB_CAP
        # markets per subscribe request: one request for the whole slice
        # unless told otherwise. The exchange's own docs (data/
        # probe_ws_docs.txt): "You can subscribe to a maximum of 100
        # markets per subscription. Use multiple subscriptions if you
        # need more" — the Tier 4 monitor (owner, 2026-09-25) takes its
        # thousands of markets in requests of 100.
        self.chunk = int(chunk) if chunk else self.cap
        self.debounce = bool(debounce)
        self.keep = max(int(keep), 600)
        self.name = name
        self.status = {"state": "off", "last_msg": 0.0, "subscribed": 0,
                       "note": "", "shard": int(shard)}
        # subscribe requests the exchange answered with an error:
        # requestId -> (markets in it, its words)
        self.refused: dict[str, tuple[int, str]] = {}
        self._req_n: dict[str, int] = {}
        # the exchange's own DECLARED best bid/ask per market, from the
        # Lite feed (owner, 2026-08-21: does the exchange's "best" match
        # the raw touch, and is IT the scoring anchor?)
        self.declared: dict[str, tuple[float | None, float | None, float]] = {}
        # the frame-shape sampler (owner yes, 2026-08-28): the health
        # line shows frames arriving while zero books get written, so
        # record WHAT actually comes down the pipe — a signature per
        # distinct frame shape with a count and one truncated sample —
        # instead of guessing. Read by the hourly stream-health line.
        self.frame_shapes: dict[str, dict] = {}
        # last (lastTradePx, openInterest) per market, so a CHANGE in
        # either marks a trade PRINT (owner, 2026-08-29: shape churn is
        # blind to take-and-refill; prints are the real quiet signal)
        self._lite_prev: dict[str, tuple[float, float]] = {}
        # the last trade PRICE per market and when it changed (the tier
        # fair's print input, owner 2026-09-25 stage 1 — read-only)
        self.last_trade: dict[str, tuple[float, float, bool]] = {}
        self._thread: threading.Thread | None = None

    def _sample(self, msg: dict) -> None:
        keys = []
        for k, v in list(msg.items())[:8]:
            if isinstance(v, dict):
                keys.append(k + "{" + ",".join(sorted(v.keys())[:10]) + "}")
            else:
                keys.append(str(k))
        sig = ",".join(sorted(keys)) or "(empty)"
        rec = self.frame_shapes.get(sig)
        if rec is None:
            if len(self.frame_shapes) < 20:
                self.frame_shapes[sig] = {"n": 1, "ts": time.time(),
                                          "sample": json.dumps(msg)[:400]}
        else:
            rec["n"] += 1
            rec["ts"] = time.time()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name=self.name)
        self._thread.start()

    def _requests(self, slugs: list[str]) -> list[dict]:
        """The subscribe messages for a slice: the full book and the Lite
        feed, one request each per `chunk` markets. A slice that fits one
        chunk keeps the request ids it always had ("books", "lite")."""
        parts = [slugs[i:i + self.chunk] for i in range(0, len(slugs), self.chunk)] or [[]]
        out = []
        for kind, typ in (("books", "SUBSCRIPTION_TYPE_MARKET_DATA"),
                          ("lite", "SUBSCRIPTION_TYPE_MARKET_DATA_LITE")):
            for i, part in enumerate(parts):
                rid = kind if len(parts) == 1 else f"{kind}-{self.shard}-{i}"
                sub = {"requestId": rid, "subscriptionType": typ, "marketSlugs": part}
                if self.debounce:
                    sub["responsesDebounced"] = True
                self._req_n[rid] = len(part)
                out.append({"subscribe": sub})
        return out

    def _note_refusal(self, msg: dict) -> bool:
        """A subscribe the exchange refused: {"requestId", "error"}.
        Recorded with its own words, so the page says how many markets
        the exchange would not carry and why."""
        err = msg.get("error")
        if not err:
            return False
        rid = str(msg.get("requestId") or msg.get("request_id") or "?")
        self.refused[rid] = (int(self._req_n.get(rid, 0)), str(err)[:200])
        if len(self.refused) > 200:
            self.refused.pop(next(iter(self.refused)), None)
        self.status["refused"] = sum(n for n, _w in self.refused.values())
        self.status["refused_requests"] = len(self.refused)
        self.status["note"] = f"refused {rid}: {str(err)[:160]}"
        return True

    def apply_frame(self, raw: str | bytes) -> str | None:
        """Parse one frame into the cache. Returns the slug, or None for
        frames that aren't book data — one bad frame must never kill the
        socket."""
        try:
            msg = json.loads(raw)
            if isinstance(msg, dict):
                self._sample(msg)
                if self._note_refusal(msg):
                    return None
            lite = msg.get("marketDataLite") or {}
            if lite.get("marketSlug"):
                slug_l = lite["marketSlug"]
                bb = to_num((lite.get("bestBid") or {}).get("value"))
                ba = to_num((lite.get("bestAsk") or {}).get("value"))
                self.declared[slug_l] = (
                    bb if bb > 0 else None, ba if ba > 0 else None,
                    time.time())
                if len(self.declared) > self.keep:
                    oldest = min(self.declared, key=lambda k: self.declared[k][2])
                    self.declared.pop(oldest, None)
                # trade prints: lastTradePx or openInterest moved since
                # the last Lite frame for this market. The first frame
                # is a baseline, never a print.
                ltp_raw = lite.get("lastTradePx")
                ltp = to_num(ltp_raw.get("value") if isinstance(ltp_raw, dict)
                             else ltp_raw)
                oi = to_num(lite.get("openInterest"))
                prev = self._lite_prev.get(slug_l)
                if (prev is not None and prev != (ltp, oi)
                        and hasattr(self.cache, "note_trade")):
                    self.cache.note_trade(slug_l, time.time())
                # (price, when, printed): the first frame for a market
                # carries a last trade of unknown age, so it is kept but
                # marked not printed; a CHANGE is a print, timed now
                if 0.0 < ltp < 1.0 and (prev is None or prev[0] != ltp
                                        or slug_l not in self.last_trade):
                    self.last_trade[slug_l] = (ltp, time.time(), prev is not None)
                    if len(self.last_trade) > max(2000, self.keep):
                        oldest = min(self.last_trade, key=lambda k: self.last_trade[k][1])
                        self.last_trade.pop(oldest, None)
                self._lite_prev[slug_l] = (ltp, oi)
                if len(self._lite_prev) > self.keep:
                    self._lite_prev.pop(next(iter(self._lite_prev)), None)
                self.status["last_msg"] = time.time()
                return None
            md = msg.get("marketData") or {}
            slug = md.get("marketSlug")
            if not slug:
                return None
            bids = [(to_num(l.get("px")), to_num(l.get("qty")))
                    for l in md.get("bids") or []]
            asks = [(to_num(l.get("px")), to_num(l.get("qty")))
                    for l in md.get("offers") or md.get("asks") or []]
            self.cache.put(slug, normalize_book(bids, asks,
                                                fetched_at=time.time()),
                           writer="ws")
            self.status["last_msg"] = time.time()
            return slug
        except Exception:  # noqa: BLE001
            return None

    def _my_slugs(self) -> list[str]:
        """This connection's slice of the priority-ordered list. The list
        is ordered once, by ws_priority; the shards just cut it, so the
        best markets still land on the first connection and a shard that
        dies costs only its own slice."""
        all_slugs = self.get_slugs()
        lo = self.shard * self.cap
        return all_slugs[lo:lo + self.cap]

    def _run(self) -> None:
        try:
            import asyncio
            import websockets
        except ImportError:
            self.status.update(state="unavailable",
                               note="websockets not installed — REST polling only")
            return

        async def session() -> None:
            slugs = self._my_slugs()
            headers = auth_headers(self.key_id, self.secret_key, "GET", WS_PATH)
            try:  # websockets >= 14 renamed the kwarg
                conn = websockets.connect(WS_URL, additional_headers=headers)
            except TypeError:
                conn = websockets.connect(WS_URL, extra_headers=headers)
            async with conn as ws:
                self.refused.clear()
                self.status.update(refused=0, refused_requests=0)
                for req in self._requests(slugs):
                    await ws.send(json.dumps(req))
                self.status.update(state="live", subscribed=len(slugs), note="")
                last_check = started = time.time()
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=60)
                        self.apply_frame(raw)
                    except asyncio.TimeoutError:
                        pass  # quiet books are normal
                    if time.time() - last_check > RESUBSCRIBE_CHECK_S:
                        last_check = time.time()
                        want = set(self._my_slugs())
                        drift = len(want ^ set(slugs))
                        # reconnect when the wanted list really moved —
                        # the 15-minute rotation windows land here — but
                        # not for every single order placed or pulled
                        if drift >= 5 or (drift > 0 and
                                          time.time() - started > 900.0):
                            return

        while True:
            try:
                import asyncio
                asyncio.run(session())
            except Exception as e:  # noqa: BLE001 — reconnect after any failure
                self.status.update(state="reconnecting", note=str(e)[:200])
                time.sleep(RECONNECT_WAIT_S)
