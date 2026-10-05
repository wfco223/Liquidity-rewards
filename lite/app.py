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

The taps keep 3.0's owner-tap rails and add the ones the two reviews of
2026-10-05 found missing:
- a change is placed, confirmed resting by its id, and only then is the
  original cancelled. For an order that opens or adds to a position, a
  replacement the exchange cut to the money free is withdrawn and the
  original left as it was; for an exit (it takes no money) a shortfall
  can only be a fill, and the part that rests stands;
- one change of an order at a time, and a cancel of an order mid-change
  waits for it; a change is refused unless the open list was read at the
  tap, and when the order has filled or moved since his card showed it;
- a sale or a cover counts what he already offers on that side, so the
  same shares are never offered twice without his seeing it;
- a placement the exchange accepted but has not listed yet is pending:
  nothing more goes on that side of that market for a minute, or until
  the list shows it;
- the stop refuses new taps at once and waits for one in flight to finish
  before it saves.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

from v3.api import GATEWAY, TRADE_API, Client, DEAD_ORDER_STATES, events_of
from v3.alerts import Alerts
from v3.books import BookCache
from v3.estimator import BOOK_MAX_AGE, MAX_GAP_S, VERIFIED_MAX_S, Estimator, et_day, top_up_book
from v3.intents import BUY_LONG, BUY_SHORT, REST_SIDE, SELL_LONG, SELL_SHORT, capital_at_risk
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
BOOT_HOLD_S = 180.0        # the old copy runs on a minute: no payout check, no upload
TRADES_S = 3600.0
SAVE_S = 60.0
BOOK_STALE_S = 150.0       # a quiet book of ours is re-read past this
BOOK_READS_PER_PASS = 12
TAP_BOOK_S = 20.0          # a tap reads the book unless the cache is this fresh
OPENED_KEEP_S = 1800.0     # a market he opened keeps its book fresh this long
HOLDING_MIN_USD = 1.0      # holdings worth less are counted, not listed
PENDING_S = 60.0           # an unconfirmed placement holds its side this long
GHOST_S = 600.0            # a cancelled id the list still shows is hidden this long
EVENT_LOOKUP_S = 600.0     # one event-size lookup a market per this
STOP_TAP_WAIT_S = 20.0     # the stop waits this long for a tap in flight
STOP_SAVE_WAIT_S = 20.0    # ...and this long for its upload (launcher allows 45)
DOTS_SENT_S = 6 * 3600.0 + 600.0
STATE_BRANCH = "lite-state"
SEED_BRANCH = "v3-state"
TAGS = ("politics", "elections")
ECON_WORDS = ("usfed", "cbpac", "fomc", "cpi", "gdp", "usunemp", "banxico", "bcb")
# a market he opens that is not already his must be politics or sports
# (his scope; never econ). The exchange's codes and names for both.
OPEN_CATEGORIES = ("politics", "pol", "elections", "sports", "spo")
CLOSING = (SELL_LONG, SELL_SHORT)
WHAT = {SELL_LONG: "sells what you hold", BUY_SHORT: "opens or adds to a short",
        SELL_SHORT: "buys back your short", BUY_LONG: "buys"}


def iso_ts(s: str) -> float:
    """Seconds since the epoch for the exchange's ISO times (to the second)."""
    import datetime as dt
    try:
        return dt.datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return 0.0


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
            "event": str(md.get("eventSlug") or ""),
        })
    return out


def is_econ_market(slug: str) -> bool:
    """Standing rule: NEVER econ markets. v3's token test plus the
    central-bank and jobs slugs it misses."""
    s = str(slug or "").lower()
    return is_econ(s) or any(w in s.split("-") for w in ECON_WORDS)


def category_ok(md: dict) -> bool:
    cats = [str(md.get(k) or "").lower().split("/")[0]
            for k in ("category", "subcategory")]
    return any(c in OPEN_CATEGORIES for c in cats if c)


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
        self.event_slug: dict[str, str] = {}
        self.event_tried: dict[str, float] = {}
        self.discover_n = 0            # markets the last full discovery found
        self.orders: list[dict] = []
        self.ghosts: list[dict] = []    # cancelled by us, still on the exchange's list
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
        self.pending: dict[tuple, tuple] = {}     # (market, side) -> (sent at, order id)
        self.busy: set[str] = set()               # order ids with a change in flight
        self.audit: deque = deque(maxlen=200)
        self.notes: deque = deque(maxlen=200)
        self.boot_ts = self.clock()
        self.due: dict[str, float] = {"rewards": self.boot_ts + BOOT_HOLD_S,
                                      "rewards_file": self.boot_ts + BOOT_HOLD_S}
        self.trades_deep = True
        self.restored = ""
        self.restoring = True          # nothing saves and no tap runs until restore is done
        self.seeded_v3 = False
        self.seed_caught_up = False
        self.v3_head = ""
        self.state_unread = False      # its own save may exist but could not be read
        self.upload_hold = True        # a new container uploads nothing until the old copy is gone
        self.stopping = ""
        self.tap_lock = threading.Lock()      # place and move
        self.cancel_lock = threading.Lock()   # a cancel never waits behind a placement
        self.busy_lock = threading.Lock()
        self.orders_lock = threading.Lock()
        self.mem_lock = threading.Lock()      # the payout memory, across threads
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
        with self.mem_lock:
            seen, paid = dict(self.rewards_seen), dict(self.paid_seen)
        now = self.clock()
        ids = sorted(self.placed_ids.items(), key=lambda kv: kv[1])[-5000:]
        return {
            "saved_at": round(now, 1),
            "est": self.est.to_dict(),
            "terms": self.terms.to_dict(),
            "event_n": dict(self.event_n),
            "event_slug": dict(self.event_slug),
            "discover_n": self.discover_n,
            "names": self.names.to_dict(),
            "rewards_seen": seen,
            "paid_seen": paid,
            "rw_last": self.rw_last,
            "placed_ids": dict(ids),
            "cancelled": {i: t for i, t in list(self.desk.cancelled.items()) if now - t < GHOST_S},
            "audit": list(self.audit)[-60:],
            "trades_deep": self.trades_deep,
            "seeded_v3": self.seeded_v3,
            "seed_caught_up": self.seed_caught_up,
            "v3_head": self.v3_head,
            # for a check from outside: what it said, where it came from
            "notes": list(self.notes)[-60:],
            "restored": self.restored, "boot_ts": round(self.boot_ts, 1),
            "orders_n": len(self.orders), "orders_at": round(self.orders_at, 1),
            "rate": round(self.est.rate, 2), "bp": to_num(self.balance.get("buyingPower"))
            if self.balance else None,
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
        """Its own save when there is one. The first boot (its branch
        ABSENT, not merely unread) seeds the graph, terms, event sizes,
        names, known order ids and the payout memory from 3.0's save,
        which is read only and never written. When its own branch cannot
        be read and cannot be ruled out, nothing is saved and no payout
        is checked until it can — a hiccup at boot must not overwrite the
        real state."""
        try:
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
            self.upload_hold = not getattr(self.store, "local_found", False)
            if st:
                self.from_dict(st)
                self.restored = f"own save of {st.get('saved_at')}"
                self._catch_up_from_v3(st)
            elif where == "absent":
                self._first_seed()
            else:
                self.state_unread = True
                self.restored = "nothing yet — its own save could not be read; saves held"
                self.due["restore"] = self.clock() + 60.0
        finally:
            self.restoring = False

    def _first_seed(self) -> None:
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
            # 3.0's save unread: run without it, and keep trying — the
            # first state this copy writes would otherwise rule it out
            self.state_unread = True
            self.restored = "nothing — 3.0's save could not be read; saves held"
            self.due["restore"] = self.clock() + 60.0

    def retry_restore(self, now: float) -> None:
        """A save that could not be read at boot: try again. What this
        copy learned meanwhile (payout memory, event sizes, names, order
        ids, the meter) wins over the save."""
        if not self.state_unread or not self._is_due("restore", 60.0, now):
            return
        try:
            st = self.store.load_best()
        except Exception:  # noqa: BLE001
            st = None
        if st:
            self._merge_late(st)
            self.state_unread = False
            self.restored = f"own save of {st.get('saved_at')} (read late)"
            self.note("own save read late — merged, saving again")
            return
        if self._branch_state(self.store) == "absent":
            try:
                seed = self._seed().load_remote()
            except Exception:  # noqa: BLE001
                seed = None
            if seed:
                self._merge_late(seed, from_v3=True)
                self.seeded_v3 = True
                self.state_unread = False
                self.restored = f"3.0's save of {seed.get('saved_at')} (read late)"

    def _merge_late(self, st: dict, from_v3: bool = False) -> None:
        if from_v3:
            fam = st.get("fam_politics") or {}
            seen, paid = st.get("rewards_seen") or {}, st.get("paid_seen") or {}
            n = dict(self._v3_sizes(st))
            names = (st.get("names") or {}).get("known") or {}
            ids = self._v3_ids(st)
            if not self.terms.current and fam.get("terms"):
                self.terms = TermsStore.from_dict(fam["terms"])
        else:
            seen, paid = st.get("rewards_seen") or {}, st.get("paid_seen") or {}
            n = st.get("event_n") or {}
            names = (st.get("names") or {}).get("known") or {}
            ids = st.get("placed_ids") or {}
            if not self.terms.current and st.get("terms"):
                self.terms = TermsStore.from_dict(st["terms"])
        with self.mem_lock:
            self.rewards_seen = {**seen, **self.rewards_seen}
            self.paid_seen = {**paid, **self.paid_seen}
        self.event_n = {**{s: int(v) for s, v in n.items() if v}, **self.event_n}
        self.names.known = {**names, **self.names.known}
        self.placed_ids = {**ids, **self.placed_ids}

    def _catch_up_from_v3(self, own: dict) -> None:
        """Back from a spell on 3.0 (the way back is deleting lite/ACTIVE):
        3.0's newer save carries what it pushed and earned meanwhile, and
        it wins. Read only when 3.0's branch has moved since last seen."""
        try:
            head = self._seed().remote_head() or ""
        except Exception:  # noqa: BLE001
            return
        if not head or head == self.v3_head:
            return
        try:
            v3 = self._seed().load_remote()
        except Exception:  # noqa: BLE001
            v3 = None
        if not v3:
            return
        self.v3_head = head
        if (v3.get("saved_at") or 0) <= (own.get("saved_at") or 0):
            return
        with self.mem_lock:
            self.rewards_seen.update(v3.get("rewards_seen") or {})
            self.paid_seen.update(v3.get("paid_seen") or {})
        e3 = v3.get("est_politics") or {}
        if e3.get("day") == self.est.day and (e3.get("last_ts") or 0) > (self.est.last_ts or 0):
            self.est = Estimator.from_dict(e3)
        self.note("caught up from 3.0's newer save")

    def from_dict(self, st: dict) -> None:
        if st.get("est"):
            self.est = Estimator.from_dict(st["est"])
        if st.get("terms"):
            self.terms = TermsStore.from_dict(st["terms"])
        self.event_n = {s: int(n) for s, n in (st.get("event_n") or {}).items() if n}
        self.event_slug = dict(st.get("event_slug") or {})
        self.discover_n = int(st.get("discover_n") or 0)
        self.names.restore(st.get("names") or {})
        with self.mem_lock:
            self.rewards_seen = dict(st.get("rewards_seen") or {})
            self.paid_seen = dict(st.get("paid_seen") or {})
        self.rw_last = st.get("rw_last")
        self.placed_ids = dict(st.get("placed_ids") or {})
        for i, t in (st.get("cancelled") or {}).items():
            self.desk.remember_cancel(i, at=t)
        self.audit.extend(st.get("audit") or [])
        self.trades_deep = bool(st.get("trades_deep", True))
        self.seeded_v3 = bool(st.get("seeded_v3", False))
        self.seed_caught_up = bool(st.get("seed_caught_up", False))
        self.v3_head = str(st.get("v3_head") or "")

    @staticmethod
    def _v3_sizes(v3: dict) -> dict[str, int]:
        n: dict[str, int] = {}
        for k, fam in v3.items():
            if not (k.startswith("fam_") and isinstance(fam, dict)):
                continue
            for s, v in (fam.get("event_n_seen") or {}).items():
                if v:
                    n[s] = int(v)
            for s, u in (fam.get("universe") or {}).items():
                if (u or {}).get("event_n"):
                    n[s] = int(u["event_n"])
        return n

    @staticmethod
    def _v3_ids(v3: dict) -> dict[str, float]:
        """3.0's order ids, newest first by when they were placed or
        filled, so the trades file reads which side of a trade was ours
        the way 3.0 did."""
        ids: dict[str, float] = {}
        for k, fam in v3.items():
            if not (k.startswith("fam_") and isinstance(fam, dict)):
                continue
            for oid, t in (fam.get("placed_at") or {}).items():
                ids[str(oid)] = max(ids.get(str(oid), 0.0), float(t or 0))
            for oid, o in (fam.get("orders") or {}).items():
                ids[str(oid)] = max(ids.get(str(oid), 0.0), float((o or {}).get("placed_ts") or 0))
            for f in fam.get("fills") or []:
                if f.get("oid"):
                    ids[str(f["oid"])] = max(ids.get(str(f["oid"]), 0.0), float(f.get("ts") or 0))
        return dict(sorted(ids.items(), key=lambda kv: kv[1])[-5000:])

    def seed_from_v3(self, v3: dict) -> None:
        if v3.get("est_politics"):
            self.est = Estimator.from_dict(v3["est_politics"])
        pol = v3.get("fam_politics") or {}
        if pol.get("terms"):
            self.terms = TermsStore.from_dict(pol["terms"])
        for k, fam in v3.items():
            if k.startswith("fam_") and fam is not pol and isinstance(fam, dict) and fam.get("terms"):
                other = TermsStore.from_dict(fam["terms"])
                for s, p in other.current.items():
                    if self.terms.get(s) is None:
                        self.terms.current[s] = p
                        self.terms.updated_at[s] = other.updated_at.get(s, 0.0)
        self.event_n = self._v3_sizes(v3)
        self.placed_ids = self._v3_ids(v3)
        self.names.restore(v3.get("names") or {})
        with self.mem_lock:
            self.rewards_seen = dict(v3.get("rewards_seen") or {})
            self.paid_seen = dict(v3.get("paid_seen") or {})
        try:
            self.v3_head = self._seed().remote_head() or ""
        except Exception:  # noqa: BLE001
            self.v3_head = ""
        self.seeded_v3 = True

    def save(self, force_remote: bool = False) -> None:
        if self.state_unread or self.restoring:
            return
        with self.save_lock:
            st = self.to_dict()
            if self.upload_hold:
                self.store.save_local(st)
            else:
                self.store.save_soon(st, force_remote=force_remote)

    def end_upload_hold(self) -> None:
        """The old copy has stopped: take its last word on the payout
        memory and order ids (its stop save), then upload as usual."""
        if not self.upload_hold:
            return
        try:
            remote = self.store.load_remote()
        except Exception:  # noqa: BLE001
            remote = None
        if remote:
            with self.mem_lock:
                self.rewards_seen.update(remote.get("rewards_seen") or {})
                self.paid_seen.update(remote.get("paid_seen") or {})
            self.placed_ids = {**(remote.get("placed_ids") or {}), **self.placed_ids}
        self.upload_hold = False

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
        and any market he opened that the exchange confirmed is open,
        politics or sports, and not econ (standing rule: never econ)."""
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
        rows, ghosts = [], []
        for o in normalize_orders(raw):
            t = self.desk.cancelled_at(o["id"])
            (ghosts if t and now - t < GHOST_S else rows).append(o)
        with self.orders_lock:
            if read_at < self.orders_at:
                return
            self.orders, self.ghosts, self.orders_at = rows, ghosts, read_at
            self.verified_at = read_at
            ids = {o["id"] for o in rows}
            for o in rows:
                if o["event"]:
                    self.event_slug[o["market"]] = o["event"]
            for k in [k for k, (t, oid) in self.pending.items()
                      if now - t > PENDING_S or oid in ids]:
                del self.pending[k]

    def _read_orders(self, tries: int = 2) -> bool:
        t0 = self.clock()
        try:
            self._take_orders(self.client.open_orders_raw(tries=tries, timeout=10.0), t0)
            return True
        except Exception as e:  # noqa: BLE001 — the last list stands, unverified
            self.note(f"open list: {e}")
            return False

    def _find(self, order_id: str) -> dict | None:
        return next((o for o in self.orders if o["id"] == order_id), None)

    def offered(self, slug: str, intent: str, but: str = "") -> float:
        """Shares his resting orders already offer to close this position
        (asks selling the long, bids buying back the short) — a cancel the
        exchange has not yet acted on still counts."""
        return sum(o["size"] for o in self.orders + self.ghosts
                   if o["market"] == slug and o["intent"] == intent and o["id"] != but)

    def _pending_note(self, slug: str, side: str) -> str:
        sent = self.pending.get((slug, side))
        if sent and self.clock() - sent[0] < PENDING_S:
            return (f"an order sent {self.clock() - sent[0]:.0f}s ago on this side "
                    f"({'id ' + sent[1] if sent[1] != '?' else 'no answer'}) is not listed yet — "
                    f"wait a minute or check your orders")
        return ""

    def _new_on_side(self, slug: str, side: str, before: set) -> list[dict]:
        return [o for o in self.orders if o["market"] == slug and o["side"] == side
                and o["id"] not in before]

    def _filled_since(self, order_id: str, since: float) -> float | None:
        """Shares of this order the exchange's trade record shows filled
        since `since`, or None when the record could not be read."""
        try:
            rows = self.client.recent_trades(limit=50, tries=1, timeout=8.0)
        except Exception:  # noqa: BLE001
            return None
        got = 0.0
        for a in rows:
            t = a.get("trade") or {}
            for k in ("passiveExecution", "aggressorExecution"):
                ex = t.get(k) or {}
                if (str((ex.get("order") or {}).get("id") or "") == order_id
                        and iso_ts(ex.get("transactTime") or "") >= since - 1.0):
                    got += to_num(ex.get("lastShares"))
        return got

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
        pool = self.side_pool(o["market"], prog) if prog is not None else None
        if prog is None or not prog.is_live() or book is None:
            return None, prog, pool
        s = score_resting(o["side"], o["price"], o["size"], top_up_book(book, here),
                          df=prog.df, target=prog.target, daily_side_pool=pool)
        return s, prog, pool

    def _day_roll_gap(self, now: float) -> None:
        """A sample after an outage of more than five minutes that spans
        ET midnight: the closed day banks its part of the gap, today
        banks the part since midnight, nothing is billed, and the graph
        reads zero across it."""
        e = self.est
        if not (e.last_ts and e.day and et_day(now) != e.day and now - e.last_ts > MAX_GAP_S):
            return
        midnight = et_day_start(now)
        e.stale_s += max(midnight - e.last_ts, 0.0)
        e._close_day()
        e.day = et_day(now)
        e.stale_s = max(now - midnight, 0.0)
        e.dots.append([round(e.last_ts + 20.0, 1), 0.0, 0])
        e.dots.append([round(now - 20.0, 1), 0.0, 0])
        e.last_ts = now

    def unverified(self, now: float) -> bool:
        return self.verified_at is None or now - self.verified_at > VERIFIED_MAX_S

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
        unverified = self.unverified(now)
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
            pos = self.client.positions()
            self.positions, self.positions_at = pos, now
            for s, p in pos.items():
                ev = ((p or {}).get("marketMetadata") or {}).get("eventSlug")
                if ev:
                    self.event_slug[s] = str(ev)
        except Exception as e:  # noqa: BLE001
            self.note(f"positions: {e}")

    def event_size(self, slug: str, now: float) -> None:
        """A market of his with no event size (listed since the last
        discovery, or outside politics): count its event's open markets,
        the event named on his order or position, at most once in ten
        minutes a market. One try each: this never holds up the books."""
        if self.event_n.get(slug) or now - self.event_tried.get(slug, 0.0) < EVENT_LOOKUP_S:
            return
        self.event_tried[slug] = now
        ev_slug = self.event_slug.get(slug)
        if not ev_slug:
            self.note(f"event size {slug}: no event named on its order or position")
            return
        try:
            j = self.client.get(f"{GATEWAY}/v1/events/slug/{ev_slug}", tries=1, timeout=8.0)
            ev = j.get("event") or j
            rows = [m for m in ev.get("markets") or [] if m.get("slug") and not m.get("closed")]
            if not rows:
                self.note(f"event size {slug}: event {ev_slug} lists no open markets")
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
        short = bool(self.discover_n) and len(found) < 0.6 * self.discover_n
        for s, r in found.items():
            self.event_n[s] = int(r["event_n"])
            if r.get("name"):
                self.names.known[s] = r["name"]
        if short or not found:
            # a short feed is merged in (above), never taken whole, and
            # read again soon
            self.note(f"discover: {len(found)} markets where the last full read had "
                      f"{self.discover_n} — merged")
            return False
        self.discover_n = len(found)
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
        if self.seeded_v3 and not self.seed_caught_up:
            # 3.0 ran on for a minute after the seed was read and may
            # have pushed rows in it: its final word wins
            try:
                v3 = self._seed().load_remote()
            except Exception:  # noqa: BLE001
                v3 = None
            if not v3:
                self.note("payouts: 3.0's final save could not be read — check held")
                return
            with self.mem_lock:
                self.rewards_seen.update(v3.get("rewards_seen") or {})
                self.paid_seen.update(v3.get("paid_seen") or {})
            self.seed_caught_up = True
        with self.mem_lock:
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
        so a slow exchange answer never holds up the book reads; none of
        it while the state is unread or the old copy may still run."""
        now = now if now is not None else self.clock()
        if self.state_unread or self.restoring or now < self.boot_ts + BOOT_HOLD_S:
            return
        self.end_upload_hold()
        if self._is_due("rewards_file", REWARDS_FILE_S, now):
            self.due["rewards"] = now + REWARDS_S
            self.check_rewards(now, write_file=True)
        elif self._is_due("rewards", REWARDS_S, now):
            self.check_rewards(now, write_file=False)
        if self._is_due("trades", TRADES_S, now):
            self.publish_trades()

    # -- his taps ------------------------------------------------------------

    def _gate(self) -> str:
        if self.restoring:
            return "starting — try again in a minute"
        if self.stopping:
            return f"stopping ({self.stopping}) — try again after the restart"
        return ""

    def _tap_book(self, slug: str):
        """A book under 20 s old for a tap that places: the cache's, else
        one read that waits out a gateway hold (priority) rather than
        fail."""
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
        if (g := self._gate()):
            return {"ok": False, "note": g}
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
            self._tap_book(slug)
            net = self._net_now(slug)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "note": f"could not read the market or your position: {e}"}
        with self.tap_lock:
            if (g := self._gate()):
                return {"ok": False, "note": g}
            if not self._read_orders():
                return {"ok": False, "note": "could not read your orders — try again"}
            if (pn := self._pending_note(slug, side)):
                return {"ok": False, "note": pn}
            # what he already offers to close counts against what he holds:
            # the same shares are never offered twice
            if side == "SELL":
                free = max(net, 0.0) - self.offered(slug, SELL_LONG)
                intent = SELL_LONG if qty <= free + 1e-9 else BUY_SHORT
            else:
                free = max(-net, 0.0) - self.offered(slug, SELL_SHORT)
                intent = SELL_SHORT if qty <= free + 1e-9 else BUY_LONG
            before = {o["id"] for o in self.orders}
            r = self.desk.place_resting(slug, side, price, qty, intent=intent, initiator="owner")
            if r.order_id:
                self.placed_ids[r.order_id] = round(self.clock(), 1)
            self._read_orders()
            what = WHAT.get(intent, "")
            if not r.ok and not r.order_id:
                # no clear answer: it may have landed all the same
                new = self._new_on_side(slug, side, before)
                if new:
                    for o in new:
                        self.placed_ids[o["id"]] = round(self.clock(), 1)
                    return {"ok": True, "id": new[0]["id"], "price": new[0]["price"],
                            "note": f"no clear answer, but it rests: {new[0]['size']:g} @ "
                                    f"{new[0]['price'] * 100:g}c (id {new[0]['id']}) — {what}"}
                self.pending[(slug, side)] = (self.clock(), "?")
                return {"ok": False, "note": f"{r.note} — if there was no answer it may still "
                                             f"land; check your orders before placing again"}
            if r.order_id and not r.ok:
                if r.resting_qty:
                    return {"ok": True, "id": r.order_id, "price": r.price,
                            "note": f"resting {r.resting_qty:g} of {qty:g} — the exchange "
                                    f"kept what the money allows — {what}"}
                self.pending[(slug, side)] = (self.clock(), r.order_id)
                return {"ok": True, "pending": True, "id": r.order_id, "price": r.price,
                        "note": f"sent (id {r.order_id}) but not listed yet — it may still "
                                f"land; check your orders before placing again"}
        return {"ok": r.ok, "note": f"{r.note} — {what}" if r.ok else r.note,
                "id": r.order_id, "price": r.price}

    def move(self, order_id: str, cents: float | None = None, qty: float | None = None,
             was_price: float | None = None, was_size: float | None = None) -> dict:
        """New price and/or size: the replacement is placed and confirmed
        resting by its id BEFORE the original is cancelled (never the
        exchange's modify, which destroys the order). `was_price` /
        `was_size` are what his card showed: an order that has filled or
        changed since is refused, not resized from a stale number."""
        if (g := self._gate()):
            return {"ok": False, "note": g}
        with self.busy_lock:
            if order_id in self.busy:
                return {"ok": False, "note": "a change of this order is already in flight"}
            self.busy.add(order_id)
        try:
            return self._move(order_id, cents, qty, was_price, was_size)
        finally:
            with self.busy_lock:
                self.busy.discard(order_id)

    def _move(self, order_id, cents, qty, was_price, was_size) -> dict:
        if not self._read_orders():
            return {"ok": False, "note": "could not read your orders — try again"}
        o = self._find(order_id)
        if o is None or self.desk.cancelled_at(order_id):
            return {"ok": False, "note": "that order is not on the open list"}
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
        closing = o["intent"] in CLOSING
        try:
            book = self._tap_book(o["market"])
            net = self._net_now(o["market"]) if closing else 0.0
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "note": f"could not read the market or your position: {e}"}
        held = 0.0
        if closing:
            held = max(net, 0.0) if o["intent"] == SELL_LONG else max(-net, 0.0)
            room = held - self.offered(o["market"], o["intent"], but=order_id)
            if size > room + 1e-9:
                return {"ok": False, "note": f"you hold {held:g} and other orders offer "
                                             f"{held - room:g} of it — at most {max(room, 0):g} here"}
        tick = self.cache.grid(o["market"]) or book.tick
        price = round(snap_price(price, tick, o["side"]), 4)
        if abs(price - o["price"]) < 1e-9 and abs(size - o["size"]) < 1e-9:
            return {"ok": False, "note": "nothing to change"}
        t_start = self.clock()
        with self.tap_lock:
            if (g := self._gate()):
                return {"ok": False, "note": g}
            if (pn := self._pending_note(o["market"], o["side"])):
                return {"ok": False, "note": pn}
            before = {x["id"] for x in self.orders}
            # an exit takes no money, so a shortfall can only be a fill: the
            # part that rests stands. An order that adds to a position the
            # exchange cut to the money free is withdrawn instead.
            r = self.desk.reprice(dict(o), price, size, initiator="owner", keep_trimmed=closing)
            if r.order_id and r.order_id != order_id:
                self.placed_ids[r.order_id] = round(self.clock(), 1)
            if r.withdrawn_id:
                self.placed_ids[r.withdrawn_id] = round(self.clock(), 1)
            self._read_orders()
            if not r.ok and not r.withdrawn_id:
                # no clear answer to the replacement: one may rest all the
                # same — withdraw it, the original stays as it was
                gone = []
                for x in self._new_on_side(o["market"], o["side"], before):
                    self.placed_ids[x["id"]] = round(self.clock(), 1)
                    if not self.desk.cancel(x["id"], o["market"], initiator="owner").ok:
                        return {"ok": False, "note": f"{r.note} — a new order (id {x['id']}) "
                                                     f"rests and could not be withdrawn; cancel one"}
                    gone.append(x["id"])
                if not gone:
                    self.pending[(o["market"], o["side"])] = (self.clock(), "?")
                self._read_orders()
                return {"ok": False, "note": f"{r.note} — your order is as it was"
                        + (f"; the new order that landed anyway ({', '.join(gone)}) was withdrawn"
                           if gone else "; if a new order shows up, cancel it")}
        if not r.ok:
            note = r.note
            if "resting only" in note:
                note = ("the new order rested only in part (the money cut it, or part of it "
                        "filled) — it was withdrawn and your order is as it was; check your "
                        "position and buying power")
            if r.withdrawn_id and not self.desk.cancelled_at(r.withdrawn_id):
                note += f" — the new order (id {r.withdrawn_id}) could not be withdrawn; check your orders"
            return {"ok": False, "note": note}
        note = r.note
        if r.two_orders:
            if self._find(order_id) is None:
                note = "the original filled or was already gone — only the new order rests"
            else:
                note += " — BOTH rest; cancel one"
        # the original may have filled while the replacement was checked
        filled = self._filled_since(order_id, t_start)
        if filled is None:
            note += " — the trade record could not be read; check whether the original filled meanwhile"
        elif filled > 1e-9:
            note += (f" — {filled:g} of the original filled during the change; the new order "
                     f"is still {size:g}")
        if closing:
            held_now = held - (filled or 0.0)
            out = self.offered(o["market"], o["intent"])
            if out > held_now + 1e-9:
                note += f" — check: {out:g} offered against about {max(held_now, 0):g} held"
        return {"ok": True, "note": note, "id": r.order_id, "price": r.price}

    def cancel(self, order_id: str) -> dict:
        if self.restoring:
            return {"ok": False, "note": "starting — try again in a minute"}
        for _ in range(40):                 # a change in flight finishes first
            with self.busy_lock:
                if order_id not in self.busy:
                    self.busy.add(order_id)  # and no change starts during the cancel
                    break
            self._sleep(0.5)
        else:
            return {"ok": False, "note": "a change of this order is still running — try again"}
        try:
            with self.cancel_lock:
                o = self._find_any(order_id)
                if o is None:
                    self._read_orders()
                    o = self._find_any(order_id)
                if o is None:
                    return {"ok": False, "note": "that order is not on the open list"}
                r = self.desk.cancel(order_id, o["market"], initiator="owner")
                self._read_orders()
            return {"ok": r.ok, "note": r.note}
        finally:
            with self.busy_lock:
                self.busy.discard(order_id)

    def _find_any(self, order_id: str) -> dict | None:
        """An order on the list, a cancel not yet acted on included."""
        return next((o for o in self.orders + self.ghosts if o["id"] == order_id), None)

    def open_market(self, slug: str) -> dict:
        """He opened a market's book. A market that is neither his order's
        nor a holding is checked with the exchange first: open, and
        politics or sports."""
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
            if not (slug in self.event_n or category_ok(md)):
                return {"ok": False, "note": "only politics and sports markets"}
            self.checked[slug] = self.clock()
            self.names.learn(slug, md)
            ev = md.get("eventSlug") or (md.get("event") or {}).get("slug")
            if ev:
                self.event_slug[slug] = str(ev)
        self.opened[slug] = self.clock()
        if self.terms.get(slug) is None:
            self.refresh_terms(self.clock(), [slug])
        if not self.event_n.get(slug):
            self.event_size(slug, self.clock())
        return {"ok": True}

    # -- what the page reads --------------------------------------------------

    def _view_book(self, slug: str):
        """The book to SHOW: the cache when fresh, else one quick read,
        else the last book with its age — a view never waits out a
        gateway hold."""
        now = self.clock()
        if self.cache.age(slug, now) <= TAP_BOOK_S:
            return self.cache.any_age(slug), ""
        try:
            book = self.client.book(slug, timeout=8.0, tries=1)
            self.cache.put(slug, book)
            return book, ""
        except Exception as e:  # noqa: BLE001
            return self.cache.any_age(slug), f"book read failed: {str(e)[:80]}"

    def book_view(self, slug: str) -> dict:
        """The order book with his orders marked, his position, what his
        orders already offer against it, and the program the market pays
        under."""
        r = self.open_market(slug)
        if not r["ok"]:
            return r
        book, stale = self._view_book(slug)
        now = self.clock()
        mine = [o for o in self.orders if o["market"] == slug]
        prog = self.terms.get(slug)
        pool = self.side_pool(slug, prog) if prog is not None else None
        p = self.positions.get(slug) or {}
        cv = p.get("cashValue")
        net = self.held().get(slug, 0.0)
        return {
            "ok": True, "market": slug, "name": self.label(slug), "stale": stale,
            "age": round(now - book.fetched_at, 1) if book else None,
            "tick": book.tick if book else None,
            "bids": [list(x) for x in book.bids[:10]] if book else [],
            "asks": [list(x) for x in book.asks[:10]] if book else [],
            "ours": [{"id": o["id"], "side": o["side"], "price": o["price"], "size": o["size"],
                      "est": self.order_est.get(o["id"])} for o in mine],
            "net": net,
            "free_long": max(net, 0.0) - self.offered(slug, SELL_LONG),
            "free_short": max(-net, 0.0) - self.offered(slug, SELL_SHORT),
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
        now = self.clock()
        here = [x for x in self.orders if x["market"] == o["market"]]
        book = self.cache.any_age(o["market"])
        s, prog, pool = self.score(o, book, here)
        math = {"why": "", "side_pool": pool, "target": prog.target if prog else None,
                "df": prog.df if prog else None}
        if prog is None:
            math["why"] = "no reward program read for this market"
        elif not prog.is_live():
            math["why"] = "the program is not live"
        elif book is None:
            math["why"] = "no book to score against (read failed)"
        elif pool is None:
            math["why"] = "event size unknown — no dollar figure"
        if s is not None:
            math.update(why=math["why"] or s.reason, ticks=s.ticks, side_total=s.side_total,
                        denom=s.denom, share=s.share, est=s.est_day,
                        score=(o["size"] * prog.df ** s.ticks) if s.share else 0.0,
                        window=[list(w) for w in (s.window or ())])
        if self.unverified(now):
            math["est"] = None
            math["why"] = "your orders have not been read back for 5 min — nothing counted"
        elif view.get("first_day"):
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
        orders += [{"id": o["id"], "market": o["market"], "name": self.label(o["market"]),
                    "side": o["side"], "price": o["price"], "size": o["size"],
                    "est": None, "ghost": True} for o in self.ghosts]
        orders.sort(key=lambda o: (o["name"], o["side"], -o["price"]))
        b = self.balance
        note = ""
        if self.state_unread:
            note = "its saved state could not be read — not saving or checking payouts until it can"
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
            "state_note": note,
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
        """The stop: refuse new taps at once, wait for one in flight to
        finish (a change half done must reach its cancel), then save and
        upload — 20 s and 20 s inside the launcher's 45. The desk is not
        halted: this app keeps no order records a late cancel could
        contradict, and an in-flight change must be free to finish."""
        self.stopping = why
        got_tap = self.tap_lock.acquire(timeout=STOP_TAP_WAIT_S)
        deadline = self.clock() + 5.0
        while self.busy and self.clock() < deadline:
            self._sleep(0.2)
        try:
            self.upload_hold = False
            self.save(force_remote=True)
            self.store.wait_remote(STOP_SAVE_WAIT_S)
        except Exception as e:  # noqa: BLE001
            print(f"lite: stop save failed: {e}", flush=True)
        finally:
            if got_tap:
                self.tap_lock.release()
