"""Exchange API: signing, one disciplined HTTP client, read endpoints.

READ-ONLY on purpose. Everything that places, moves or cancels orders
lives in orders.py behind the safety rails; this module only knows how
to sign, retry, and parse. The quirks encoded here were all paid for in
1.0 — each one is commented where it is handled.

Auth: Ed25519 signature over ``timestamp + METHOD + path`` (the query
string is NOT signed), sent as X-PM-Access-Key / X-PM-Timestamp (ms) /
X-PM-Signature. The secret is base64 of a 32-byte seed (or a 64-byte
key, of which the first 32 bytes are the seed). Matches the official
polymarket-us SDK.
"""

from __future__ import annotations

import base64
import os
import time

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .programs import to_num
from .scoring import Book, normalize_book

TRADE_API = "https://api.polymarket.us"          # authenticated
GATEWAY = "https://gateway.polymarket.us"        # public
# The documented incentives host first, the trade host as fallback.
INCENTIVES_HOSTS = ("https://api.prod.polymarketexchange.com", TRADE_API)
EARNINGS_PATH = "/v1/incentives/earnings"

# /v1/orders/open keeps returning finished orders (a modify used to mint a
# permanent REPLACED ghost per call). They are not resting and must not be
# counted, scored, or matched by verification. Denylist on purpose:
# dropping a state that is actually live would make us re-place a
# duplicate; keeping an unknown state merely over-counts — the cheaper
# mistake.
DEAD_ORDER_STATES = frozenset({
    "ORDER_STATE_REPLACED", "ORDER_STATE_CANCELED", "ORDER_STATE_CANCELLED",
    "ORDER_STATE_FILLED", "ORDER_STATE_EXPIRED", "ORDER_STATE_REJECTED",
    "ORDER_STATE_DONE_FOR_DAY",
})

RETRYABLE_STATUSES = (429, 500, 502, 503, 504)


def auth_headers(key_id: str, secret_key: str, method: str, path: str,
                 now_ms: int | None = None) -> dict:
    """Sign ``timestamp+method+path`` with the account's Ed25519 key."""
    timestamp = str(now_ms if now_ms is not None else int(time.time() * 1000))
    seed = base64.b64decode(secret_key)
    if len(seed) == 64:
        seed = seed[:32]
    key = Ed25519PrivateKey.from_private_bytes(seed)
    signature = key.sign(f"{timestamp}{method}{path}".encode())
    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": base64.b64encode(signature).decode(),
    }


def err_text(resp) -> str:
    """Readable one-line HTTP error — never a raw Cloudflare HTML page."""
    body = " ".join((resp.text or "").split())[:300]
    return f"HTTP {resp.status_code}: {body}"


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def events_of(client, tag: str, max_pages: int = 30):
    """Discovery's event feed: a page at a time where the client can
    stream (the real one), the whole list where a fake cannot."""
    it = getattr(client, "iter_events", None)
    if it is not None:
        return it(tag, max_pages)
    return client.events_by_tag(tag, max_pages=max_pages)


class Client:
    """One HTTP client with the retry discipline 1.0 learned the hard way:
    429/5xx retried in place honouring Retry-After (a throttled response
    must not abort a whole cycle), network faults retried with backoff,
    but any other 4xx raised immediately — a real answer, retrying it
    wastes time. All timestamps the exchange needs come from this box's
    clock; all money numbers are parsed with to_num (protobuf shapes)."""

    def __init__(self, key_id: str | None = None, secret_key: str | None = None,
                 session=None, timeout: float = 30.0, sleep=None):
        self.key_id = key_id if key_id is not None else os.environ.get("POLYMARKET_KEY_ID", "")
        self.secret_key = (secret_key if secret_key is not None
                           else os.environ.get("POLYMARKET_SECRET_KEY", ""))
        self.session = session or requests.Session()
        self.timeout = timeout
        self._sleep = sleep if sleep is not None else time.sleep

    def fresh_connection(self) -> None:
        """Drop the pooled connection and open a new one (2026-09-05: the
        exchange refused a placement as 'a VPN'; a new connection may
        leave the host on a different outbound address)."""
        try:
            self.session.close()
        except Exception:  # noqa: BLE001
            pass
        self.session = requests.Session()

    # -- plumbing ----------------------------------------------------------

    def _headers(self, method: str, path: str) -> dict:
        if not (self.key_id and self.secret_key):
            raise ApiError("no API credentials configured")
        return auth_headers(self.key_id, self.secret_key, method, path)

    def _request(self, method: str, url: str, *, path: str | None = None,
                 signed: bool = False, params: dict | None = None,
                 json_body: dict | None = None, timeout: float | None = None,
                 tries: int = 4):
        """One HTTP call with the retry discipline; returns parsed JSON.
        `path` is the signed path (defaults to the URL's path — the query
        string is never part of the signature)."""
        if path is None:
            path = "/" + url.split("://", 1)[-1].split("/", 1)[-1].split("?")[0]
        delay = 2.0
        last_exc: Exception | None = None
        for attempt in range(tries):
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body,
                    headers=self._headers(method, path) if signed else {},
                    timeout=timeout or self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as e:
                last_exc = e
                if attempt < tries - 1:
                    self._sleep(delay)
                    delay = min(delay * 2, 15.0)
                continue
            if resp.status_code < 400:
                return resp.json()
            if resp.status_code not in RETRYABLE_STATUSES or attempt == tries - 1:
                raise ApiError(f"{url} -> {err_text(resp)}", status=resp.status_code)
            ra = resp.headers.get("Retry-After")
            try:
                wait = min(float(ra), 30.0) if ra else delay
            except ValueError:
                wait = delay
            if resp.status_code == 429:
                # Cloudflare throttle windows are TIME-based and outlast a
                # 2/4/8s ladder — the 2026-08-20 cycle failures were 429s
                # that survived all four quick tries. Wait the window out:
                # a cycle that stretches a minute beats a cycle that dies
                # (the web thread keeps serving health checks meanwhile).
                wait = max(wait, 15.0 * (attempt + 1))
            self._sleep(wait)
            delay = min(delay * 2, 15.0)
        raise ApiError(f"{url}: {type(last_exc).__name__} on every one of {tries} tries — {last_exc}")

    def get(self, url: str, *, path: str | None = None, signed: bool = False,
            params: dict | None = None, timeout: float | None = None, tries: int = 4):
        return self._request("GET", url, path=path, signed=signed, params=params,
                             timeout=timeout, tries=tries)

    def post(self, url: str, json_body: dict, *, path: str | None = None,
             timeout: float | None = None, tries: int = 1):
        """Signed POST. Default tries=1 on purpose: order-touching calls must
        never be blindly re-sent — a timed-out placement may have landed,
        and re-posting it doubles the order. The caller (orders.py) decides
        how to recover, by looking at the book, never by resending."""
        return self._request("POST", url, signed=True, path=path,
                             json_body=json_body, timeout=timeout, tries=tries)

    # -- account -----------------------------------------------------------

    def balances_raw(self) -> list[dict]:
        j = self.get(TRADE_API + "/v1/account/balances", signed=True)
        return list(j.get("balances") or [])

    def buying_power(self) -> float | None:
        """1.0's bug was returning the FIRST balance row carrying a
        buyingPower key — with several rows (a zero row before the funded
        one) that reads $0 while the account holds money, and it silently
        blocked the qualifier for weeks. Parse every row through to_num
        (the API nests numbers) and take the largest. The first read-only
        run logs balances_raw so the true payload shape gets confirmed."""
        vals = [to_num(b.get("buyingPower")) for b in self.balances_raw()
                if b.get("buyingPower") is not None]
        return max(vals) if vals else None

    def positions(self, max_pages: int = 20) -> dict[str, dict]:
        """All portfolio positions keyed by market slug. This endpoint
        paginates with cursor/eof (not pageToken) and returns a DICT —
        both confirmed by 1.0's working reader."""
        out: dict[str, dict] = {}
        cursor = None
        pages = 0
        eof = False
        for _ in range(max_pages):
            params: dict = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            j = self.get(TRADE_API + "/v1/portfolio/positions", signed=True, params=params)
            pages += 1
            out.update(j.get("positions") or {})
            cursor = j.get("nextCursor")
            if j.get("eof") or not cursor:
                eof = bool(j.get("eof"))
                break
        # the shape of the last read, for the log: on 2026-09-04 a read
        # came back missing 114 of 171 positions and nothing said so
        self.positions_read = {"pages": pages, "n": len(out), "eof": eof}
        return out

    def positions_net(self) -> dict[str, tuple[float, float]]:
        """slug -> (net contracts, cost $) for every non-flat position.
        Positive net = long. Field shapes per 1.0: netPosition, cost.value."""
        out: dict[str, tuple[float, float]] = {}
        for slug, p in self.positions().items():
            net = to_num(p.get("netPosition"))
            cost = to_num((p.get("cost") or {}).get("value") if isinstance(p.get("cost"), dict)
                          else p.get("cost"))
            if net or cost:
                out[slug] = (net, round(cost, 2))
        return out

    def activities(self, types=None, pages: int = 10,
                   page_size: int = 100) -> list[dict]:
        """The account's activity history, paginated to the end or
        `pages` pages — the DEFINITIVE record of what happened (owner,
        2026-08-23: "get the transaction history so we can have a
        definitive record"). types=None asks for everything the
        exchange records, not just trades, so cancels and resolutions
        show up if the feed carries them. Raw rows; the caller parses."""
        out: list[dict] = []
        cursor = None
        for _ in range(max(pages, 1)):
            params: dict = {"limit": page_size,
                            "sortOrder": "SORT_ORDER_DESCENDING"}
            if types:
                params["types"] = list(types)
            if cursor:
                params["cursor"] = cursor
            j = self.get(TRADE_API + "/v1/portfolio/activities",
                         signed=True, params=params)
            rows = j.get("activities") or []
            out.extend(rows)
            cursor = j.get("nextCursor")
            if j.get("eof") or not cursor or not rows:
                break
        return out

    def recent_trades(self, limit: int = 25) -> list[dict]:
        """Latest trade activities. The feed returns BOTH sides of every
        trade — treating that as a self-cross once dropped 1,623 of 1,623
        real fills. Deduplication is the caller's job; this returns raw."""
        j = self.get(TRADE_API + "/v1/portfolio/activities", signed=True,
                     params={"types": ["ACTIVITY_TYPE_TRADE"], "pageSize": limit})
        return list(j.get("activities") or [])

    # -- orders (read only here) --------------------------------------------

    def open_orders(self) -> list[dict]:
        """Live resting orders, normalized, dead states filtered."""
        j = self.get(TRADE_API + "/v1/orders/open", signed=True)
        orders = []
        for o in j.get("orders") or []:
            if str(o.get("state") or "") in DEAD_ORDER_STATES:
                continue
            md = o.get("marketMetadata") or {}
            size = to_num(o.get("leavesQuantity")) or to_num(o.get("quantity"))
            orders.append({
                "id": str(o.get("id") or ""),
                "market": o.get("marketSlug") or md.get("slug") or "",
                "side": "BUY" if str(o.get("side", "")).upper().endswith("BUY") else "SELL",
                "price": to_num(o.get("price")),
                "size": size,
                "intent": str(o.get("intent") or ""),
                "state": str(o.get("state") or ""),
                "created": str(o.get("createTime") or ""),
                "title": str(md.get("title") or ""),
                "subject": str(((md.get("subject") or {}).get("name")) or ""),
                "image": str(((md.get("subject") or {}).get("image")) or ""),
                "manual": str(o.get("manualOrderIndicator") or "").endswith("MANUAL"),
            })
        return orders

    # -- market data (public) ------------------------------------------------

    BOOK_DEPTH = 50          # the endpoint's documented maximum

    def compare_book_sources(self, slugs: list[str]) -> list[str]:
        """READ-ONLY diagnostic (owner approved 2026-08-25): fetch the
        same markets through the current book path and the documented
        /v1/orderbook/{slug}?depth=50 path, and return log lines with
        the level counts and touches side by side. CHANGES NOTHING —
        every live fetch still uses the current path. The owner's
        screenshots showed the exchange rendering 14 levels and a 1c
        spread on a market our feed shows as 4 levels and 50c wide;
        this measures whether the other endpoint sees the real book."""
        def shape(j) -> tuple:
            md = (j.get("book") or j.get("marketData")
                  or j.get("orderbook") or j)
            bids = [(to_num(l.get("px") or l.get("price")),
                     to_num(l.get("qty") or l.get("size")))
                    for l in md.get("bids") or []]
            asks = [(to_num(l.get("px") or l.get("price")),
                     to_num(l.get("qty") or l.get("size")))
                    for l in md.get("offers") or md.get("asks") or []]
            bb = max((p for p, _ in bids), default=None)
            ba = min((p for p, _ in asks), default=None)
            return (len(bids), len(asks),
                    f"{bb*100:.0f}c" if bb else "-",
                    f"{ba*100:.0f}c" if ba else "-")
        lines = []
        for slug in slugs:
            row = [slug[:44]]
            try:
                cur = shape(self.get(f"{GATEWAY}/v1/markets/{slug}/book",
                                     params={"depth": self.BOOK_DEPTH}))
                row.append(f"current={cur[0]}+{cur[1]} lvls "
                           f"{cur[2]}/{cur[3]}")
            except Exception as e:  # noqa: BLE001
                row.append(f"current=ERR {str(e)[:40]}")
            try:
                ob = shape(self.get(f"{GATEWAY}/v1/orderbook/{slug}",
                                    params={"depth": self.BOOK_DEPTH}))
                row.append(f"orderbook={ob[0]}+{ob[1]} lvls "
                           f"{ob[2]}/{ob[3]}")
            except Exception as e:  # noqa: BLE001
                row.append(f"orderbook=ERR {str(e)[:40]}")
            lines.append("  ".join(row))
        return lines

    def book(self, slug: str, fetched_at: float | None = None) -> Book:
        """The resting book, as DEEP as the endpoint will give us.

        We asked for no depth and took the default, which measures at
        4-5 price levels a side across 370 stored snapshots. Asking for
        the documented maximum is strictly more information for the same
        request, and if the parameter is ignored the response is what we
        get today, so this cannot make the book worse.

        It is NOT a fix for the share overestimate, though it was
        proposed as one on 2026-08-24 and measured down: with a discount
        factor of 0.3, a ladder seen 3 deep scores 37.5% and the same
        ladder 20 deep scores 36.8%. df**ticks decays faster than depth
        accumulates, so levels past the fourth are nearly weightless.
        Depth is worth having for the fill model and the touch, not for
        the share. cache.depth_seen records what actually came back."""
        j = self.get(f"{GATEWAY}/v1/markets/{slug}/book",
                     params={"depth": self.BOOK_DEPTH})
        md = j.get("book") or j.get("marketData") or j
        bids = [(to_num(l.get("px")), to_num(l.get("qty"))) for l in md.get("bids") or []]
        asks = [(to_num(l.get("px")), to_num(l.get("qty")))
                for l in md.get("offers") or md.get("asks") or []]
        return normalize_book(bids, asks,
                              fetched_at if fetched_at is not None else time.time())

    def programs(self, slugs: list[str]) -> dict[str, dict]:
        """Raw incentive programs for the given markets, keyed by slug.
        Batched (hundreds of symbols overflow the URL); each batch tries
        each host — api.polymarket.us needs the signed headers, the prod
        host does not. Picking the paying period from timePeriods is
        programs.pick_period's job, at the caller. Raises if EVERY batch
        fails; a partial result never silently stands in for a full one."""
        out: dict[str, dict] = {}
        errors: list[str] = []
        for i in range(0, len(slugs), 40):
            batch = slugs[i:i + 40]
            got = None
            for host in INCENTIVES_HOSTS:
                try:
                    j = self.get(host + "/v1/incentives", signed=(host == TRADE_API),
                                 path="/v1/incentives",
                                 params={"symbols": batch, "page_size": 100}, timeout=20)
                    got = {}
                    for p in j.get("programs") or []:
                        s2 = p.get("marketSlug")
                        if not s2:
                            continue
                        if s2 in got:
                            # a market in TWO programs (e.g. the old tier
                            # plus 2026-08-27's elections boost) comes back
                            # as two rows — merge the periods, don't let
                            # the last row silently win
                            got[s2].setdefault("timePeriods", []).extend(
                                p.get("timePeriods") or [])
                        else:
                            got[s2] = dict(p)
                    break
                except ApiError as e:
                    errors.append(str(e))
            if got is None:
                raise ApiError("programs fetch failed on every host: " + " | ".join(errors[-2:]))
            out.update(got)
            self._sleep(0.05)
        return out

    def all_programs(self, max_pages: int = 400, page_size: int = 500,
                     program_type: str = "liquidityProgram",
                     statuses: tuple = ("active",),
                     compact=None) -> tuple[list[dict], str]:
        """Every market currently paying a liquidity program.

        `compact`, when given, is applied to each row AS ITS PAGE
        ARRIVES, so the raw 4.5 KB rows never pile up (owner,
        2026-09-02: 28,081 of them were the biggest thing in memory).

        The docs settle two things I had guessed wrong (owner supplied
        them, 2026-08-31). Query parameters are snake_case and a
        camelCase one is SILENTLY IGNORED — our pageSize was dropped
        without an error, so a first page of defaults read like the
        whole population. And pagination is page_token in, nextPageToken
        out; the cursor names I tried do not exist, so it stopped after
        one page with 500 of ~67,569 markets and reported success.

        Returns (rows, note). Rows carry the exchange's own category,
        subcategory, eventStartTime and instrumentProduct, so the survey
        needs no per-market detail call to know what a market is or when
        its event starts.
        """
        out: list[dict] = []
        seen: set[str] = set()
        token = None
        for page in range(max_pages):
            params: dict = {"page_size": int(page_size)}
            if token:
                params["page_token"] = token
            if program_type:
                params["program_type"] = program_type
            if statuses:
                params["statuses"] = list(statuses)
            j = None
            for host in INCENTIVES_HOSTS:
                try:
                    j = self.get(host + "/v1/incentives",
                                 signed=(host == TRADE_API),
                                 path="/v1/incentives", params=params,
                                 timeout=25)
                    break
                except ApiError:
                    j = None
            if j is None:
                return out, (f"no host answered on page {page + 1}"
                             if not out else
                             f"stopped after {len(out):,} — page "
                             f"{page + 1} failed")
            rows = j.get("programs") or []
            for r in rows:
                s2 = str(r.get("marketSlug") or "")
                if s2 and s2 not in seen:
                    seen.add(s2)
                    out.append(compact(r) if compact is not None else r)
            token = j.get("nextPageToken")
            if not token:
                # a page exactly the size we asked for, with no token to
                # follow, is the signature of the bug this replaces —
                # never report that as a complete enumeration
                if len(rows) >= page_size:
                    return out, (f"stopped at {len(out):,} — a full page "
                                 "with no nextPageToken, so pagination is "
                                 "not understood")
                return out, "enumerated"
            self._sleep(0.2)          # these endpoints allow 5/second
        return out, (f"stopped at the {max_pages}-page cap with "
                     f"{len(out):,} markets and more to come")

    def earnings(self, start_date: str) -> list[dict]:
        """The published-payout ground truth, complete from start_date.
        Explicit big pages (the server default ~31/page once silently
        capped history while the heartbeat stayed green); bounded
        pagination that RAISES rather than truncating silently."""
        errors: list[str] = []
        for host in INCENTIVES_HOSTS:
            try:
                return self._earnings_from(host, start_date)
            except ApiError as e:
                errors.append(str(e))
        raise ApiError("earnings failed on every host: " + " | ".join(errors))

    def _earnings_from(self, host: str, start_date: str) -> list[dict]:
        rows: list[dict] = []
        params: dict = {"startDate": start_date, "pageSize": 500}
        token = None
        for _ in range(200):
            j = self.get(host + EARNINGS_PATH, signed=True, path=EARNINGS_PATH,
                         params=params, timeout=60)
            for r in j.get("rewards") or []:
                rows.append({
                    "date": str(r.get("date", ""))[:10],
                    "market": r.get("marketSlug", ""),
                    "program_type": r.get("programType", ""),
                    "reward_usd": float(r.get("reward", 0) or 0),
                    "status": str(r.get("status", "")).upper(),
                })
            token = j.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
        else:
            if token:
                raise ApiError(f"{host}{EARNINGS_PATH}: still more pages after the bound "
                               f"({len(rows)} rows) — raise the limit")
        rows.sort(key=lambda r: (r["date"], r["market"], r["program_type"]))
        return rows

    # -- discovery (public) ---------------------------------------------------

    def iter_events(self, tag: str, max_pages: int = 30):
        """Events under a tag, ONE PAGE AT A TIME (limit/offset, not
        pageToken). Discovery walks this so a tag's whole feed — every
        market object of every event, 100 to 200 MB of Python for the
        politics tag — is never alive at once (owner, 2026-09-02: the
        six-hour discovery and survey refreshes landing together on a
        1 GB box killed the container)."""
        offset = 0
        for _ in range(max_pages):
            j = self.get(GATEWAY + "/v1/events",
                         params={"tagSlug": tag, "active": "true",
                                 "limit": 100, "offset": offset})
            events = j.get("events") or []
            yield from events
            if len(events) < 100:
                break
            offset += 100

    def events_by_tag(self, tag: str, max_pages: int = 30) -> list[dict]:
        """The whole list — kept for callers that want it; discovery
        streams iter_events instead."""
        return list(self.iter_events(tag, max_pages))

    # Field names the exchange might use for its own price grid. The
    # name is UNCONFIRMED — declared_tick logs whatever a market object
    # actually carries, so this list gets corrected from the record
    # rather than guessed at twice (owner, 2026-08-23: "It's always out
    # there for you to find").
    TICK_FIELDS = ("minpriceincrement", "priceincrement", "ticksize",
                   "mintick", "minimumpriceincrement", "minimumticksize",
                   "pricetick", "tickvalue", "minimumtick")

    @staticmethod
    def declared_tick(md: dict) -> float | None:
        """The exchange's own price grid for a market, if it says so.

        Only an unambiguous DOLLAR figure is accepted — a tick is never
        bigger than a dime, and a bare "1" could mean a cent or a
        hundredth of one. Anything outside that is left for the probe
        line to show rather than guessed at: a wrong grid places wrong
        prices, which is the whole thing being fixed here.
        """
        pools = [md]
        for k in ("marketMetadata", "metadata", "market", "terms"):
            v = md.get(k)
            if isinstance(v, dict):
                pools.append(v)
        for pool in pools:
            for key, val in pool.items():
                if str(key).lower().replace("_", "") not in Client.TICK_FIELDS:
                    continue
                try:
                    f = float(val)
                except (TypeError, ValueError):
                    continue
                if 0.0 < f <= 0.1:
                    return f
        return None

    def market_details(self, slug: str) -> dict:
        j = self.get(f"{GATEWAY}/v1/market/slug/{slug}")
        return j.get("market") or j.get("marketData") or j

    def event_by_slug(self, event_slug: str) -> dict:
        j = self.get(f"{GATEWAY}/v1/events/slug/{event_slug}")
        return j.get("event") or j

    def search(self, query: str, limit: int = 20) -> dict:
        return self.get(GATEWAY + "/v1/search", params={"query": query, "limit": limit})
