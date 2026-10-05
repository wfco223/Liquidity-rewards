"""The simple app (owner, 2026-10-04: "Okay I want to scale wayyy back.
Make a very simple version just a dashboard with a graph. My current
orders, holdings, earning rate, cash available (buying power) and get rid
of most everything else." ... "I want to be able to see the order book,
place an order, modify either the price or quantity of an order, or
cancel it. I should also see the current earnings math for every pending
order by clicking on it.").

NOTHING HERE PLACES, MOVES OR CANCELS AN ORDER ON ITS OWN. The loops only
read: the open list, positions, buying power, books, reward terms, the
payout record and the activity feed. Every order call comes from his tap
through App.place / App.move / App.cancel, with initiator "owner"; the
desk's switch is wired permanently off, so anything else is refused.

The earning rate and the graph are 3.0's meter (v3/estimator.py)
unchanged: a sample every 20 seconds, nothing billed across a gap of
more than five minutes or while the open list has not been read back for
five, nothing on a market's first day in its program. Each order's own
figure, and the math shown when he taps it, are the same score_resting
call the meter makes, so the two always agree.

The taps keep 3.0's owner-tap rails and add the ones the review of
2026-10-05 found missing:
- a change is placed, confirmed resting at its FULL size by its id, and
  only then is the original cancelled; a replacement the exchange cuts to
  the money free is withdrawn and the original left untouched (3.0's own
  owner move did the same);
- a change carries the size and price the card showed, and is refused
  when the order has filled or moved since — a price change never resends
  a stale size;
- a placement the exchange accepted but has not listed yet is reported as
  pending, not failed, and the same order cannot be sent again for a
  minute;
- the position that decides a sale's or a cover's intent is read at the
  tap, and a failed read refuses the tap rather than guess;
- a cancel never waits behind a placement, and the stop signal waits for
  a tap in flight before it halts the desk.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

from v3.api import TRADE_API, Client, DEAD_ORDER_STATES, events_of
from v3.alerts import Alerts
from v3.books import BookCache
from v3.estimator import BOOK_MAX_AGE, MAX_GAP_S, VERIFIED_MAX_S, Estimator, et_day, top_up_book
from v3.intents import REST_SIDE, SELL_LONG, SELL_SHORT, capital_at_risk
from v3.names import Names, disambiguate, name_from_market
from v3.orders import QTY_MAX, OrderDesk, snap_price
from v3.programs import is_econ, pool_days, to_num
from v3.scoring import score_resting
from v3.state import StateStore
from v3.terms import TermsStore, et_day_start

from . import records

SAMPLE_S = 20.0            # the meter's clock, and the open list and balance reads
UPKEEP_S = 15.0
RECORDS_S = 30.0
POSITIONS_S = 60.0
TERMS_S = 600.0
DISCOVER_S = 6 * 3600.0
DISCOVER_RETRY_S = 600.0   # a discovery that failed or came back short
REWARDS_S = 300.0          # the payout check (and the push)
REWARDS_FILE_S = 3600.0    # the 40-day rewards.csv rewrite
REWARDS_HOLD_S = 180.0     # no payout check until the old copy has stopped
TRADES_S = 3600.0
SAVE_S = 60.0
BOOK_STALE_S = 150.0       # a quiet book of ours is re-read past this
BOOK_READS_PER_PASS = 12
TAP_BOOK_S = 20.0          # a tap reads the book unless the cache is this fresh
OPENED_KEEP_S = 1800.0     # a market he opened keeps its book fresh this long
HOLDING_MIN_USD = 1.0      # holdings worth less are counted, not listed
PENDING_S = 60.0           # an unconfirmed placement blocks the same one this long
GHOST_S = 600.0            # a cancelled id the list still shows is hidden this long
EVENT_LOOKUP_S = 600.0     # one event-size lookup a market per this
DOTS_SENT_S = 6 * 3600.0 + 600.0
STATE_BRANCH = "lite-state"
SEED_BRANCH = "v3-state"
TAGS = ("politics", "elections")
ECON_WORDS = ("usfed", "cbpac", "fomc", "cpi", "gdp")
ECON_CATEGORIES = ("macro", "econ", "economics", "economy", "finance", "monetary")


def normalize_orders(raw: list[dict]) -> list[dict]:
    """The open list as v3's Client.open_orders gives it, with the BOOK
    side taken from the intent (the exchange's side field is not
    trustworthy for shorts, v3/main.py is_exit_order)."""
    out = []
    for o in raw or []:
        if str(o.get("state") or "") in DEAD_ORDER_STATES:
            continue
        md = o.get("marketMetadata") or {}
        intent = str(o.get("intent") or "")
        side = "BUY" if str(o.get("side", "")).upper().endswith("BUY") else "SELL"
        out.append({
            "id": str(o.get("id") or ""),
            "market": o.get("marketSlug") or md.get("slug") or "",
            "side": REST_SIDE.get(intent, side),
            "price": to_num(o.get("price")),
            "size": to_num(o.get("leavesQuantity")) or to_num(o.get("quantity")),
            "intent": intent,
            "title": str(md.get("title") or ""),
            "subject": str((md.get("subject") or {}).get("name") or ""),
        })
    return out


def is_econ_market(slug: str, md: dict | None = None) -> bool:
    """Standing rule: NEVER econ markets. v3's token test, the Fed-rate
    slugs it misses, and the exchange's own category when it is known."""
    s = str(slug or "").lower()
    if is_econ(s) or any(w in s.split("-") for w in ECON_WORDS):
        return True
    cat = str((md or {}).get("category") or "").lower()
    return any(c == cat or cat.startswith(c + "/") for c in ECON_CATEGORIES)


def discover(client) -> dict[str, dict]:
    """slug -> {event_n, name} for every open, non-econ politics market:
    v3/politics.discover's body, without the engine it sits beside. The
    event size divides the pool; without it no dollar figure is shown."""
    out: dict[str, dict] = {}
    order: list[str] = []
    for tag in TAGS:
        n_tag = 0
        try:
            for ev in events_of(client, tag):
                n_tag += 1
                title = str(ev.get("title") or ev.get("name") or "").strip()
                rows = [m for m in ev.get("markets") or []
                        if m.get("slug") and not m.get("closed") and not is_econ(m["slug"])]
                labels = disambiguate([(m["slug"], name_from_market(m, title)[:110])
                                       for m in rows])
                for m in rows:
                    if m["slug"] not in out:
                        order.append(m["slug"])
                    out[m["slug"]] = {"event_n": len(rows), "name": labels[m["slug"]]}
        except Exception:  # noqa: BLE001
            if n_tag:
                raise       # a feed that died mid-way must not hand back a part
            continue
    _group_sizes(out, order)
    return out


def _group_sizes(out: dict[str, dict], order: list[str]) -> None:
    """Single-market events that are really one race share one pool."""
    groups: dict[str, list[str]] = {}
    for s in order:
        groups.setdefault(s.rsplit("-", 1)[0], []).append(s)
    for s in order:
        g = groups[s.rsplit("-", 1)[0]]
        if len(g) > out[s]["event_n"]:
            out[s]["event_n"] = len(g)


class App:
    def __init__(self, client=None, store=None, seed_store=None, alerts=None,
                 repo=None, clock=None, sleep=None):
        self.client = client or Client()
        self.clock = clock or time.time
        self._sleep = sleep or time.sleep
        self.store = store or StateStore(os.environ.get("LITE_STATE_PATH", "lite_state.json"),
                                         branch=STATE_BRANCH)
        self.seed_store = seed_store
        self.alerts = alerts or Alerts()
        self.repo = repo or records.Repo()
        self.cache = BookCache()
        self.terms = TermsStore()
        self.est = Estimator()
        self.names = Names()
        self.event_n: dict[str, int] = {}
        self.event_tried: dict[str, float] = {}
        self.orders: list[dict] = []
        self.orders_at = 0.0           # when the list now shown was READ (its request's start)
        self.order_est: dict[str, float | None] = {}
        self.verified_at: float | None = None
        self.positions: dict[str, dict] = {}
        self.positions_at = 0.0
        self.balance: dict = {}
        self.balance_at = 0.0
        self.opened: dict[str, float] = {}
        self.checked: dict[str, float] = {}       # slugs he opened that the exchange confirmed
        self.rewards_seen: dict[str, float] = {}
        self.paid_seen: dict[str, float] = {}
        self.rw_last: dict | None = None
        self.placed_ids: dict[str, float] = {}
        self.pending: dict[tuple, float] = {}     # placements accepted, not yet seen resting
        self.audit: deque = deque(maxlen=200)
        self.notes: deque = deque(maxlen=200)
        self.boot_ts = self.clock()
        # the old copy runs on for about a minute after this one answers:
        # no payout check (and no push) until it has stopped
        self.due: dict[str, float] = {"rewards": self.boot_ts + REWARDS_HOLD_S,
                                      "rewards_file": self.boot_ts + REWARDS_HOLD_S}
        self.discover_next = 0.0
        self.trades_deep = True
        self.restored = ""
        self.seeded_v3 = False
        self.state_unread = False      # its own save exists but could not be read
        self.tap_lock = threading.Lock()      # place and move
        self.cancel_lock = threading.Lock()   # a cancel never waits behind a placement
        self.orders_lock = threading.Lock()
        self.save_lock = threading.Lock()
        self.desk = OrderDesk(
            self.client, whitelist=self.known,
            switch_on=lambda: False,            # nothing but his tap ever places
            fresh_book=lambda s: self.cache.fresh(s, 120.0, self.clock()),
            log=self._audit, tick_for=self.cache.grid)
        self.streams: list = []

    # -- bookkeeping ---------------------------------------------------------

    def note(self, text: str) -> None:
        self.notes.append({"ts": round(self.clock(), 1), "note": str(text)[:300]})
        print(f"lite: {text}", flush=True)

    def _audit(self, row: dict) -> None:
        self.audit.append(row)

    def _is_due(self, key: str, every: float, now: float) -> bool:
        if now >= self.due.get(key, 0.0):
            self.due[key] = now + every
            return True
        return False

    # -- state ---------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "saved_at": round(self.clock(), 1),
            "est": self.est.to_dict(),
            "terms": self.terms.to_dict(),
            "event_n": self.event_n,
            "names": self.names.to_dict(),
            "rewards_seen": self.rewards_seen,
            "paid_seen": self.paid_seen,
            "rw_last": self.rw_last,
            "placed_ids": dict(list(self.placed_ids.items())[-5000:]),
            "audit": list(self.audit)[-60:],
            "trades_deep": self.trades_deep,
            "seeded_v3": self.seeded_v3,
        }

    def _branch_state(self, store) -> str:
        """"present", "absent" or "unknown" for a store's branch on GitHub:
        "could not read" must never pass for "never saved"."""
        if not getattr(store, "token", None):
            return "absent"
        try:
            r = store._gh("GET", f"/repos/{store.repo}/git/ref/heads/{store.branch}")
        except Exception:  # noqa: BLE001
            return "unknown"
        if r.status_code == 404:
            return "absent"
        return "present" if r.status_code < 300 else "unknown"

    def _seed(self):
        return self.seed_store or StateStore("lite_v3_seed.json", branch=SEED_BRANCH)

    def restore(self) -> None:
        """Its own save when there is one. The first boot (the branch
        absent, not merely unread) seeds the graph, terms, event sizes,
        names, known order ids and the payout memory from 3.0's save,
        which is read only and never written. When its own branch exists
        but cannot be read, nothing is saved until it can be — a hiccup
        at boot must not overwrite the real state."""
        st, where = None, "unknown"
        for i in range(3):
            try:
                st = self.store.load_best()
            except Exception as e:  # noqa: BLE001
                self.note(f"state read failed: {e}")
            if st:
                break
            where = self._branch_state(self.store)
            if where == "absent":
                break
            self._sleep(2.0 * (i + 1))
        if st:
            self.from_dict(st)
            self.state_unread = False
            self.restored = f"own save of {st.get('saved_at')}"
            self._catch_up_from_v3(st)
            return
        if where != "absent":
            self.state_unread = True
            self.restored = "nothing yet — its own save exists but could not be read; saves held"
            self.due["restore"] = self.clock() + 60.0
            return
        seed = None
        for i in range(3):
            try:
                seed = self._seed().load_remote()
            except Exception as e:  # noqa: BLE001
                self.note(f"3.0 seed read failed: {e}")
            if seed:
                break
            self._sleep(2.0 * (i + 1))
        if seed:
            self.seed_from_v3(seed)
            self.restored = f"3.0's save of {seed.get('saved_at')}"
        else:
            self.restored = "nothing — a fresh start"

    def retry_restore(self, now: float) -> None:
        """Its own save was unreadable at boot: try again; until it reads,
        the running copy saves nothing."""
        if not self.state_unread or not self._is_due("restore", 60.0, now):
            return
        try:
            st = self.store.load_best()
        except Exception:  # noqa: BLE001
            st = None
        if st:
            keep_orders = self.orders
            self.from_dict(st)
            self.orders = keep_orders
            self.state_unread = False
            self.restored = f"own save of {st.get('saved_at')} (read late)"
            self.note("own save read — saving again")

    def _catch_up_from_v3(self, own: dict) -> None:
        """Back from a spell on 3.0 (the way back is deleting lite/ACTIVE):
        3.0's newer save carries what it pushed meanwhile."""
        try:
            v3 = self._seed().load_remote()
        except Exception:  # noqa: BLE001
            return
        if not v3 or (v3.get("saved_at") or 0) <= (own.get("saved_at") or 0):
            return
        self._union_seen(v3)
        e3 = v3.get("est_politics") or {}
        if e3.get("day") == self.est.day and (e3.get("last_ts") or 0) > (self.est.last_ts or 0):
            self.est = Estimator.from_dict(e3)
        self.note("caught up from 3.0's newer save")

    def _union_seen(self, v3: dict) -> None:
        for k, v in (v3.get("rewards_seen") or {}).items():
            self.rewards_seen.setdefault(k, v)
        for k, v in (v3.get("paid_seen") or {}).items():
            self.paid_seen.setdefault(k, v)

    def from_dict(self, st: dict) -> None:
        if st.get("est"):
            self.est = Estimator.from_dict(st["est"])
        if st.get("terms"):
            self.terms = TermsStore.from_dict(st["terms"])
        self.event_n = {s: int(n) for s, n in (st.get("event_n") or {}).items() if n}
        self.names.restore(st.get("names") or {})
        self.rewards_seen = dict(st.get("rewards_seen") or {})
        self.paid_seen = dict(st.get("paid_seen") or {})
        self.rw_last = st.get("rw_last")
        self.placed_ids = dict(st.get("placed_ids") or {})
        self.audit.extend(st.get("audit") or [])
        self.trades_deep = bool(st.get("trades_deep", True))
        self.seeded_v3 = bool(st.get("seeded_v3", False))

    def seed_from_v3(self, v3: dict) -> None:
        fams = [v for k, v in v3.items() if k.startswith("fam_") and isinstance(v, dict)]
        if v3.get("est_politics"):
            self.est = Estimator.from_dict(v3["est_politics"])
        pol = v3.get("fam_politics") or {}
        if pol.get("terms"):
            self.terms = TermsStore.from_dict(pol["terms"])
        n: dict[str, int] = {}
        ids: dict[str, float] = {}
        for fam in fams:
            for s, v in (fam.get("event_n_seen") or {}).items():
                if v:
                    n[s] = int(v)
            for s, u in (fam.get("universe") or {}).items():
                if (u or {}).get("event_n"):
                    n[s] = int(u["event_n"])
            if fam is not pol and fam.get("terms"):
                other = TermsStore.from_dict(fam["terms"])
                for s, p in other.current.items():
                    if self.terms.get(s) is None:
                        self.terms.current[s] = p
                        self.terms.updated_at[s] = other.updated_at.get(s, 0.0)
            # 3.0's order ids, so the trades file reads which side of a
            # trade was ours the way 3.0 did
            for oid in list((fam.get("orders") or {}).keys()) + list((fam.get("placed_at") or {}).keys()):
                ids[str(oid)] = 0.0
        self.event_n = n
        self.placed_ids = dict(list(ids.items())[-5000:])
        self.names.restore(v3.get("names") or {})
        self.rewards_seen = dict(v3.get("rewards_seen") or {})
        self.paid_seen = dict(v3.get("paid_seen") or {})
        self.seeded_v3 = True

    def save(self, force_remote: bool = False) -> None:
        if self.state_unread:
            return
        with self.save_lock:
            self.store.save_soon(self.to_dict(), force_remote=force_remote)

    # -- whitelist and labels -----------------------------------------------

    def order_markets(self) -> list[str]:
        return list(dict.fromkeys(o["market"] for o in self.orders if o["market"]))

    @staticmethod
    def _held_of(positions: dict) -> dict[str, float]:
        out = {}
        for s, p in positions.items():
            n = (to_num(p.get("netPositionDecimal")) if p.get("netPositionDecimal") is not None
                 else to_num(p.get("netPosition")))
            if n:
                out[s] = n
        return out

    def held(self) -> dict[str, float]:
        return self._held_of(self.positions)

    def known(self, slug: str) -> bool:
        """The markets a tap may act in: his orders' and holdings' markets,
        and any market he opened that the exchange confirmed is open and
        not an econ market (standing rule: never econ)."""
        if not slug or is_econ_market(slug):
            return False
        return (slug in self.order_markets() or slug in self.positions
                or slug in self.checked)

    def label(self, slug: str) -> str:
        """The exchange's own words on his order or position first (the
        event title and the candidate or bracket), then a name learned
        from discovery, then the slug decoded."""
        for o in self.orders:
            if o["market"] == slug and o["title"] and o["subject"]:
                return name_from_market({"subject": {"name": o["subject"]}}, o["title"])
        md = (self.positions.get(slug) or {}).get("marketMetadata") or {}
        if md.get("title") and (md.get("subject") or {}).get("name"):
            return name_from_market({"subject": md["subject"]}, md["title"])
        return self.names.label(slug)

    # -- the open list -------------------------------------------------------

    def _take_orders(self, raw: list[dict], read_at: float) -> None:
        """Adopt an open-list read unless a newer one already landed; ids
        we cancelled in the last ten minutes are left out (the list lags a
        cancel by minutes, and a ghost would be metered twice)."""
        now = self.clock()
        rows = [o for o in normalize_orders(raw)
                if not ((t := self.desk.cancelled_at(o["id"])) and now - t < GHOST_S)]
        with self.orders_lock:
            if read_at < self.orders_at:
                return
            self.orders, self.orders_at = rows, read_at
            self.verified_at = read_at
            live = {(o["market"], o["side"], round(o["price"], 4)) for o in rows}
            for k in [k for k, t in self.pending.items()
                      if now - t > PENDING_S or (k[0], k[1], k[2]) in live]:
                del self.pending[k]

    def _read_orders(self, tries: int = 2) -> None:
        t0 = self.clock()
        try:
            self._take_orders(self.client.open_orders_raw(tries=tries, timeout=10.0), t0)
        except Exception as e:  # noqa: BLE001 — the last list stands, unverified
            self.note(f"open list: {e}")

    def _find(self, order_id: str) -> dict | None:
        return next((o for o in self.orders if o["id"] == order_id), None)

    # -- the meter -----------------------------------------------------------

    def side_pool(self, slug: str, prog) -> float | None:
        """$/day one side of this market competes for, or None while the
        event's size is unconfirmed — then no dollar figure, not a guess."""
        n = self.event_n.get(slug)
        if not n or not prog or not prog.pool:
            return None
        return prog.pool / pool_days(prog, slug) / n / 2.0

    def score(self, o: dict, book, here: list[dict]):
        """The meter's own arithmetic for one order: (Score, prog, pool)."""
        prog = self.terms.get(o["market"])
        if prog is None or not prog.is_live() or book is None:
            return None, prog, None
        pool = self.side_pool(o["market"], prog)
        s = score_resting(o["side"], o["price"], o["size"], top_up_book(book, here),
                          df=prog.df, target=prog.target, daily_side_pool=pool)
        return s, prog, pool

    def _day_roll_gap(self, now: float) -> None:
        """A sample after a restart that spans ET midnight: the unmeasured
        gap is today's only from midnight on, and the graph reads zero
        across it."""
        e = self.est
        if e.last_ts and e.day and et_day(now) != e.day and now - e.last_ts > MAX_GAP_S:
            e.dots.append([round(e.last_ts + 20.0, 1), 0.0, 0])
            e.last_ts = max(e.last_ts, et_day_start(now))

    def sample_once(self, now: float | None = None) -> None:
        now = now if now is not None else self.clock()
        self._read_orders(tries=1)
        first = set(self.terms.joined_today(now))
        orders = list(self.orders)
        meter = [o for o in orders if o["market"] not in first]
        try:
            self._day_roll_gap(now)
            self.est.sample(now, meter, self.cache, self.terms,
                            side_pool=self.side_pool, verified_at=self.verified_at)
        except Exception as e:  # noqa: BLE001
            self.note(f"meter: {e}")
        unverified = self.verified_at is None or now - self.verified_at > VERIFIED_MAX_S
        est: dict[str, float | None] = {}
        by_m: dict[str, list[dict]] = {}
        for o in orders:
            by_m.setdefault(o["market"], []).append(o)
        for o in orders:
            if unverified:
                est[o["id"]] = None
                continue
            if o["market"] in first:
                est[o["id"]] = 0.0
                continue
            book = self.cache.fresh(o["market"], BOOK_MAX_AGE, now)
            s, _, _ = self.score(o, book, by_m[o["market"]])
            est[o["id"]] = s.est_day if s is not None else None
        self.order_est = est
        try:
            j = self.client.get(TRADE_API + "/v1/account/balances", signed=True,
                                tries=1, timeout=10.0)
            rows = list(j.get("balances") or [])
            best = max(rows, key=lambda r: to_num(r.get("buyingPower")), default=None)
            if best is not None:
                self.balance, self.balance_at = dict(best), now
        except Exception as e:  # noqa: BLE001
            self.note(f"balance: {e}")

    # -- upkeep: reads only ---------------------------------------------------

    def wanted_books(self) -> list[str]:
        """Markets whose books we keep fresh, in priority order: his
        orders, what he opened lately, then holdings worth a dollar.
        Called from the stream threads too, so it only reads."""
        held = sorted(((to_num((p.get("cashValue") or {}).get("value")
                                if isinstance(p.get("cashValue"), dict) else p.get("cashValue")), s)
                       for s, p in list(self.positions.items())), reverse=True)
        big = [s for v, s in held if v >= HOLDING_MIN_USD]
        return list(dict.fromkeys(self.order_markets() + list(self.opened) + big))

    def refresh_books(self, now: float) -> None:
        """Re-read the quiet books the stream has not touched lately:
        orders and opened markets only, one try each, never under a
        gateway hold."""
        want = list(dict.fromkeys(self.order_markets() + list(self.opened)))
        n = 0
        for s in want:
            if n >= BOOK_READS_PER_PASS or self.client.gateway_hold() > 0:
                break
            if self.cache.age(s, now) <= BOOK_STALE_S:
                continue
            n += 1
            try:
                self.cache.put(s, self.client.book(s, timeout=8.0, tries=1))
            except Exception as e:  # noqa: BLE001
                self.note(f"book {s}: {e}")
                if getattr(e, "status", None) == 429:
                    break

    def refresh_terms(self, now: float, slugs: list[str] | None = None) -> None:
        batch = slugs or list(dict.fromkeys(
            self.order_markets() + list(self.opened)
            + [s for s, n in self.held().items() if abs(n) >= 1]))
        if not batch:
            return
        try:
            raw = self.client.programs(batch)
        except Exception as e:  # noqa: BLE001 — aged terms beat no terms
            self.note(f"terms: {e}")
            return
        live_before = sum(1 for s in batch if self.terms.get(s) is not None)
        if not any(raw.get(s) for s in batch) and live_before >= 3:
            self.note("terms read came back empty for markets that had programs — kept the old")
            return
        for s in batch:
            raw.setdefault(s, {})
        self.terms.refresh(raw, {s: self.event_n.get(s) or 1 for s in batch}, now=now)

    def refresh_positions(self, now: float) -> None:
        try:
            self.positions = self.client.positions()
            self.positions_at = now
        except Exception as e:  # noqa: BLE001
            self.note(f"positions: {e}")

    def event_size(self, slug: str, now: float) -> None:
        """A market of his with no event size (listed since the last
        discovery, or outside politics): count its event's open markets
        directly, at most once in ten minutes a market."""
        if self.event_n.get(slug) or now - self.event_tried.get(slug, 0.0) < EVENT_LOOKUP_S:
            return
        self.event_tried[slug] = now
        try:
            md = self.client.market_details(slug)
            ev_slug = md.get("eventSlug") or (md.get("event") or {}).get("slug")
            if not ev_slug:
                return
            ev = self.client.event_by_slug(ev_slug)
            rows = [m for m in ev.get("markets") or [] if m.get("slug") and not m.get("closed")]
            if not rows:
                return
            out = {m["slug"]: {"event_n": len(rows)} for m in rows}
            _group_sizes(out, [m["slug"] for m in rows])
            for s, r in out.items():
                self.event_n.setdefault(s, int(r["event_n"]))
        except Exception as e:  # noqa: BLE001
            self.note(f"event size {slug}: {e}")

    def run_discover(self) -> bool:
        try:
            found = discover(self.client)
        except Exception as e:  # noqa: BLE001
            self.note(f"discover: {e}")
            return False
        short = len(found) < 0.6 * len(self.event_n)
        for s, r in found.items():
            self.event_n[s] = int(r["event_n"])
            if r.get("name"):
                self.names.known[s] = r["name"]
        if short:
            # a short feed is merged in (above), never taken whole, and
            # read again soon
            self.note(f"discover: {len(found)} markets against {len(self.event_n)} known — merged")
            return False
        return True

    def discover_loop(self) -> None:
        while True:
            ok = self.run_discover()
            self._sleep(DISCOVER_S if ok else DISCOVER_RETRY_S)

    def check_rewards(self, now: float, write_file: bool) -> None:
        days = 40 if write_file else 6
        start = records.utc_day(now, days)
        try:
            rows = self.client.earnings(start)
        except Exception as e:  # noqa: BLE001
            self.note(f"payouts: {e}")
            return
        if self.seeded_v3 and not getattr(self, "_seed_caught_up", False):
            # 3.0 ran on for a minute after the seed was read and may
            # have pushed rows in it: take its final word first
            try:
                v3 = self._seed().load_remote()
                if v3:
                    self._union_seen(v3)
                self._seed_caught_up = True
            except Exception:  # noqa: BLE001
                pass
        res = records.check_rewards(rows, self.rewards_seen, self.paid_seen, now)
        # the memory first, then the push: a copy that starts after this
        # one must not push the same rows again
        self.save(force_remote=bool(res["new_count"]))
        if res["note"]:
            self.note(f"payouts: {res['note']}")
        if res["new_count"]:
            self.rw_last = {**res, "at": round(now, 1)}
            self.alerts.notify("Rewards posted", records.rewards_push_text(res))
        if res["new_count"] or write_file:
            try:
                self.note("rewards.csv: " + records.write_rewards(self.repo, rows, start))
            except Exception as e:  # noqa: BLE001
                self.note(f"rewards.csv: {e}")

    def publish_trades(self) -> None:
        known = set(self.placed_ids) | {o["id"] for o in self.orders}
        try:
            self.note("trades.csv: " + records.publish_trades(
                self.client, self.repo, known, deep=self.trades_deep))
            self.trades_deep = False
        except Exception as e:  # noqa: BLE001
            self.note(f"trades.csv: {e}")

    def upkeep_once(self, now: float | None = None) -> None:
        now = now if now is not None else self.clock()
        self.opened = {s: t for s, t in self.opened.items() if now - t <= OPENED_KEEP_S}
        self.retry_restore(now)
        if self._is_due("positions", POSITIONS_S, now):
            self.refresh_positions(now)
        self.refresh_books(now)
        for s in self.order_markets() + list(self.opened):
            if not self.event_n.get(s):
                self.event_size(s, now)
        if self._is_due("terms", TERMS_S, now):
            self.refresh_terms(now)
        if self._is_due("save", SAVE_S, now):
            self.save()

    def records_once(self, now: float | None = None) -> None:
        """The payout check, the files and the push: on their own thread,
        so a slow exchange answer never holds up the book reads."""
        now = now if now is not None else self.clock()
        if self._is_due("rewards_file", REWARDS_FILE_S, now):
            self.due["rewards"] = now + REWARDS_S
            self.check_rewards(now, write_file=True)
        elif self._is_due("rewards", REWARDS_S, now):
            self.check_rewards(now, write_file=False)
        if self._is_due("trades", TRADES_S, now):
            self.publish_trades()

    # -- his taps ------------------------------------------------------------

    def _tap_book(self, slug: str):
        """A book under 20 s old for a tap: the cache's, else one read
        that waits out a gateway hold (priority) rather than fail."""
        now = self.clock()
        if self.cache.age(slug, now) <= TAP_BOOK_S:
            return self.cache.fresh(slug, TAP_BOOK_S, now)
        book = self.client.book(slug, timeout=8.0, tries=3, priority=True)
        self.cache.put(slug, book)
        return book

    def _net_now(self, slug: str) -> float:
        """His position in this market, read now: it decides whether an
        ask sells what he holds and whether a bid buys a short back. A
        failed read raises — the tap is refused, never guessed."""
        pos = self.client.positions()
        self.positions, self.positions_at = pos, self.clock()
        return self._held_of(pos).get(slug, 0.0)

    def place(self, slug: str, side: str, cents: float, qty: float) -> dict:
        side = str(side or "").upper()
        if side not in ("BUY", "SELL"):
            return {"ok": False, "note": "side must be bid or ask"}
        try:
            price = round(float(cents) / 100.0, 4)
            qty = round(float(qty), 2)
        except (TypeError, ValueError):
            return {"ok": False, "note": "price and size must be numbers"}
        if qty < 0.01 or qty > QTY_MAX:
            return {"ok": False, "note": f"size must be 0.01 to {QTY_MAX:,.0f}"}
        if not self.known(slug):
            return {"ok": False, "note": "not a market of yours — open it first"}
        try:
            book = self._tap_book(slug)
            net = self._net_now(slug)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "note": f"could not read the market or your position: {e}"}
        tick = self.cache.grid(slug) or book.tick
        key = (slug, side, round(snap_price(price, tick, side), 4), qty)
        with self.tap_lock:
            if self.clock() - self.pending.get(key, -1e9) < PENDING_S:
                return {"ok": False, "note": "the same order was sent a moment ago and may "
                                             "still land — check your orders first"}
            close_short = side == "BUY" and net < 0 and qty <= -net + 1e-9
            r = self.desk.place_resting(slug, side, price, qty, net_position=net,
                                        close_short=close_short, initiator="owner")
            if r.order_id:
                self.placed_ids[r.order_id] = round(self.clock(), 1)
            self._read_orders()
            if r.order_id and not r.ok:
                self.pending[key] = self.clock()
                if r.resting_qty:
                    return {"ok": True, "pending": True, "id": r.order_id, "price": r.price,
                            "note": f"resting {r.resting_qty:g} of {qty:g} — the exchange "
                                    f"kept what the money allows"}
                return {"ok": True, "pending": True, "id": r.order_id, "price": r.price,
                        "note": f"sent (id {r.order_id}) but not listed yet — it may still "
                                f"land; check your orders before placing again"}
        return {"ok": r.ok, "note": r.note, "id": r.order_id, "price": r.price}

    def move(self, order_id: str, cents: float | None = None, qty: float | None = None,
             was_price: float | None = None, was_size: float | None = None) -> dict:
        """New price and/or size: the replacement is placed and confirmed
        resting at its full size by its id BEFORE the original is
        cancelled (never the exchange's modify, which destroys the order).
        `was_price`/`was_size` are what his card showed: an order that has
        filled or changed since is refused, not resized from a stale
        number."""
        self._read_orders()
        o = self._find(order_id)
        if o is None:
            return {"ok": False, "note": "that order is not on the open list"}
        if self.desk.cancelled_at(order_id):
            return {"ok": False, "note": "that order was cancelled"}
        if o["intent"] not in REST_SIDE:
            return {"ok": False, "note": "the exchange gave no intent for it — cancel and place instead"}
        try:
            if was_size not in (None, "") and abs(float(was_size) - o["size"]) > 1e-6:
                return {"ok": False, "note": f"it changed since you opened it — now "
                                             f"{o['size']:g} rest; check and try again"}
            if was_price not in (None, "") and abs(float(was_price) / 100.0 - o["price"]) > 1e-6:
                return {"ok": False, "note": "its price changed since you opened it — check and try again"}
            price = round(float(cents) / 100.0, 4) if cents not in (None, "") else o["price"]
            size = round(float(qty), 2) if qty not in (None, "") else o["size"]
        except (TypeError, ValueError):
            return {"ok": False, "note": "price and size must be numbers"}
        if size < 0.01 or size > QTY_MAX:
            return {"ok": False, "note": f"size must be 0.01 to {QTY_MAX:,.0f}"}
        try:
            book = self._tap_book(o["market"])
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "note": f"could not read the market: {e}"}
        tick = self.cache.grid(o["market"]) or book.tick
        price = round(snap_price(price, tick, o["side"]), 4)
        if abs(price - o["price"]) < 1e-9 and abs(size - o["size"]) < 1e-9:
            return {"ok": False, "note": "nothing to change"}
        with self.tap_lock:
            r = self.desk.reprice(dict(o), price, size, initiator="owner")
            if r.order_id and r.order_id != order_id:
                self.placed_ids[r.order_id] = round(self.clock(), 1)
            if r.withdrawn_id:
                self.placed_ids[r.withdrawn_id] = round(self.clock(), 1)
            self._read_orders()
        if not r.ok:
            note = r.note
            if "resting only" in note:
                note = ("not enough buying power free for the new order while the old one "
                        "rests — your order is untouched; lower the size, or cancel and place")
            if r.withdrawn_id and not self.desk.cancelled_at(r.withdrawn_id):
                note += f" — the new order (id {r.withdrawn_id}) could not be withdrawn; check your orders"
            return {"ok": False, "note": note}
        note = r.note
        if r.two_orders:
            if self._find(order_id) is None:
                note = "the original filled or was already gone — only the new order rests"
                if o["intent"] in (SELL_LONG, SELL_SHORT):
                    note += "; check your position: it may now offer more than you hold"
            else:
                note += " — BOTH rest; cancel one"
        return {"ok": True, "note": note, "id": r.order_id, "price": r.price}

    def cancel(self, order_id: str) -> dict:
        with self.cancel_lock:
            o = self._find(order_id)
            if o is None:
                self._read_orders()
                o = self._find(order_id)
            if o is None:
                return {"ok": False, "note": "that order is not on the open list"}
            r = self.desk.cancel(order_id, o["market"], initiator="owner")
            self._read_orders()
        return {"ok": r.ok, "note": r.note}

    def open_market(self, slug: str) -> dict:
        """He opened a market's book. A market that is neither his order's
        nor a holding is checked with the exchange first."""
        slug = str(slug or "").strip()
        if not slug:
            return {"ok": False, "note": "no market"}
        if is_econ_market(slug):
            return {"ok": False, "note": "econ markets are off limits"}
        if not self.known(slug):
            try:
                md = self.client.market_details(slug)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "note": f"no such market: {e}"}
            if md.get("closed") or md.get("active") is False:
                return {"ok": False, "note": "that market is closed"}
            if is_econ_market(slug, md):
                return {"ok": False, "note": "econ markets are off limits"}
            self.checked[slug] = self.clock()
            self.names.learn(slug, md)
        self.opened[slug] = self.clock()
        if self.terms.get(slug) is None:
            self.refresh_terms(self.clock(), [slug])
        if not self.event_n.get(slug):
            self.event_size(slug, self.clock())
        return {"ok": True}

    # -- what the page reads --------------------------------------------------

    def book_view(self, slug: str) -> dict:
        """The order book with his orders marked, his position, and the
        program the market pays under. A failed read shows the last book
        it had, with its age."""
        r = self.open_market(slug)
        if not r["ok"]:
            return r
        stale = ""
        try:
            book = self._tap_book(slug)
        except Exception as e:  # noqa: BLE001
            book = self.cache.any_age(slug)
            stale = f"book read failed: {str(e)[:80]}"
        now = self.clock()
        mine = [o for o in self.orders if o["market"] == slug]
        prog = self.terms.get(slug)
        pool = self.side_pool(slug, prog) if prog is not None else None
        p = self.positions.get(slug) or {}
        cv = p.get("cashValue")
        return {
            "ok": True, "market": slug, "name": self.label(slug), "stale": stale,
            "age": round(now - book.fetched_at, 1) if book else None,
            "tick": book.tick if book else None,
            "bids": [list(x) for x in book.bids[:10]] if book else [],
            "asks": [list(x) for x in book.asks[:10]] if book else [],
            "ours": [{"id": o["id"], "side": o["side"], "price": o["price"], "size": o["size"],
                      "est": self.order_est.get(o["id"])} for o in mine],
            "net": self.held().get(slug, 0.0),
            "value": to_num(cv.get("value") if isinstance(cv, dict) else cv),
            "pool": pool, "target": prog.target if prog else None,
            "df": prog.df if prog else None, "live": bool(prog and prog.is_live()),
            "prog": prog is not None, "sized": bool(self.event_n.get(slug)),
            "first_day": slug in self.terms.joined_today(now),
        }

    def order_math(self, order_id: str) -> dict:
        """The earnings math for one order, the meter's own arithmetic.
        The order and its Cancel come back even when the book cannot be
        read."""
        o = self._find(order_id)
        if o is None:
            self._read_orders()
            o = self._find(order_id)
        if o is None:
            return {"ok": False, "note": "that order is not on the open list"}
        view = self.book_view(o["market"])
        if not view.get("ok"):
            return {**view, "order": o}
        here = [x for x in self.orders if x["market"] == o["market"]]
        book = self.cache.any_age(o["market"])
        s, prog, pool = self.score(o, book, here)
        math = {"why": "", "side_pool": pool, "target": prog.target if prog else None,
                "df": prog.df if prog else None}
        if prog is None:
            math["why"] = "no reward program read for this market"
        elif not prog.is_live():
            math["why"] = "the program is not live"
        elif pool is None:
            math["why"] = "event size unknown — no dollar figure"
        if s is not None:
            math.update(why=math["why"] or s.reason, ticks=s.ticks, side_total=s.side_total,
                        denom=s.denom, share=s.share, est=s.est_day,
                        score=(o["size"] * prog.df ** s.ticks) if s.share else 0.0,
                        window=[list(w) for w in (s.window or ())])
        if view.get("first_day"):
            math["why"] = "first day in its program — counts nothing until midnight ET"
            math["est"] = 0.0
        return {**view, "order": o, "math": math,
                "risk": capital_at_risk(o["intent"], o["price"], o["size"])}

    def data(self) -> dict:
        now = self.clock()
        holdings = []
        small_n, small_v = 0, 0.0
        for s, n in self.held().items():
            p = self.positions.get(s) or {}
            cv = p.get("cashValue")
            v = to_num(cv.get("value") if isinstance(cv, dict) else cv)
            if v < HOLDING_MIN_USD:
                small_n += 1
                small_v += v
                continue
            holdings.append({"market": s, "name": self.label(s), "net": n, "value": v})
        holdings.sort(key=lambda h: -h["value"])
        orders = [{"id": o["id"], "market": o["market"], "name": self.label(o["market"]),
                   "side": o["side"], "price": o["price"], "size": o["size"],
                   "est": self.order_est.get(o["id"])} for o in self.orders]
        orders.sort(key=lambda o: (o["name"], o["side"], -o["price"]))
        b = self.balance
        return {
            "now": round(now, 1),
            "starting": not self.orders_at,
            "rate": round(self.est.rate, 2), "earned": round(self.est.earned, 2),
            "day": self.est.day or et_day(now),
            "unmeasured_min": round(self.est.stale_s / 60.0, 1),
            "dots": [d for d in list(self.est.dots) if d[0] >= now - DOTS_SENT_S],
            "bp": to_num(b.get("buyingPower")) if b else None,
            "bp_age": round(now - self.balance_at) if self.balance_at else None,
            "orders": orders,
            "orders_age": round(now - self.orders_at) if self.orders_at else None,
            "holdings": holdings, "small_n": small_n, "small_v": round(small_v, 2),
            "holdings_value": round(sum(h["value"] for h in holdings) + small_v, 2),
            "positions_age": round(now - self.positions_at) if self.positions_at else None,
            "state_note": ("its saved state could not be read — not saving until it can"
                           if self.state_unread else ""),
        }

    # -- running -------------------------------------------------------------

    def _loop(self, name: str, every: float, fn, first_delay: float = 0.0) -> None:
        def run():
            if first_delay:
                time.sleep(first_delay)
            while True:
                t0 = self.clock()
                try:
                    fn()
                except Exception as e:  # noqa: BLE001 — a loop never dies
                    self.note(f"{name}: {e}")
                time.sleep(max(every - (self.clock() - t0), 1.0))
        threading.Thread(target=run, daemon=True, name=name).start()

    def start_streams(self) -> None:
        from v3.ws import Stream
        kid, sec = self.client.key_id, self.client.secret_key
        for i in range(2):
            st = Stream(self.cache, self.wanted_books, kid, sec,
                        shard=i, shards=2, name=f"lite-books-{i}")
            st.start()
            self.streams.append(st)

    def shutdown_save(self, why: str) -> None:
        """The stop: wait for a tap in flight (a change half done must
        finish its cancel), halt the desk, save, upload. Within the
        launcher's 45 seconds: up to 15 to wait, 25 to upload."""
        got_tap = self.tap_lock.acquire(timeout=12.0)
        got_cancel = self.cancel_lock.acquire(timeout=3.0)
        OrderDesk.halted = why
        try:
            self.save(force_remote=True)
            self.store.wait_remote(25.0)
        except Exception as e:  # noqa: BLE001
            print(f"lite: stop save failed: {e}", flush=True)
        finally:
            if got_cancel:
                self.cancel_lock.release()
            if got_tap:
                self.tap_lock.release()
