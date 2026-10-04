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
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque

from v3.api import ApiError, Client, DEAD_ORDER_STATES, events_of
from v3.alerts import Alerts
from v3.books import BookCache
from v3.estimator import BOOK_MAX_AGE, Estimator, et_day, top_up_book
from v3.intents import REST_SIDE, SELL_SHORT, capital_at_risk, intent_for
from v3.names import Names, disambiguate, name_from_market
from v3.orders import QTY_MAX, OrderDesk
from v3.programs import is_econ, pool_days, to_num
from v3.scoring import score_resting
from v3.state import StateStore
from v3.terms import TermsStore

from . import records

SAMPLE_S = 20.0            # the meter's clock, and the open list and balance reads
UPKEEP_S = 15.0
POSITIONS_S = 60.0
TERMS_S = 600.0
DISCOVER_S = 6 * 3600.0
REWARDS_S = 300.0          # the payout check (and the push)
REWARDS_FILE_S = 3600.0    # the 40-day rewards.csv rewrite
TRADES_S = 3600.0
SAVE_S = 60.0
BOOK_STALE_S = 150.0       # a quiet book of ours is re-read past this
BOOK_READS_PER_PASS = 12
TAP_BOOK_S = 20.0          # a tap reads the book unless the cache is this fresh
OPENED_KEEP_S = 1800.0     # a market he opened keeps its book fresh this long
HOLDING_MIN_USD = 1.0      # holdings worth less are counted, not listed
STATE_BRANCH = "lite-state"
SEED_BRANCH = "v3-state"
TAGS = ("politics", "elections")


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
            "outcome": str(md.get("outcome") or ""),
        })
    return out


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
    groups: dict[str, list[str]] = {}
    for s in order:
        groups.setdefault(s.rsplit("-", 1)[0], []).append(s)
    for s in order:
        g = groups[s.rsplit("-", 1)[0]]
        if len(g) > out[s]["event_n"]:
            out[s]["event_n"] = len(g)
    return out


class App:
    def __init__(self, client=None, store=None, seed_store=None, alerts=None,
                 repo=None, clock=None):
        self.client = client or Client()
        self.clock = clock or time.time
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
        self.orders: list[dict] = []
        self.orders_at = 0.0
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
        self.audit: deque = deque(maxlen=200)
        self.notes: deque = deque(maxlen=200)
        self.due: dict[str, float] = {}
        self.trades_deep = True
        self.restored = ""
        self.boot_ts = self.clock()
        self.tap_lock = threading.Lock()
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
            "placed_ids": dict(list(self.placed_ids.items())[-2000:]),
            "audit": list(self.audit)[-60:],
            "trades_deep": self.trades_deep,
        }

    def restore(self) -> None:
        """Its own saved state when there is one; the first boot seeds the
        graph, terms, event sizes, names and the payout memory from 3.0's
        save (read only — 3.0's branch is never written)."""
        st = None
        try:
            st = self.store.load_best()
        except Exception as e:  # noqa: BLE001
            self.note(f"state read failed: {e}")
        if st:
            self.from_dict(st)
            self.restored = f"own save of {st.get('saved_at')}"
            return
        try:
            seed = (self.seed_store or StateStore("lite_v3_seed.json", branch=SEED_BRANCH)
                    ).load_remote()
        except Exception as e:  # noqa: BLE001
            self.note(f"3.0 seed read failed: {e}")
            seed = None
        if seed:
            self.seed_from_v3(seed)
            self.restored = f"3.0's save of {seed.get('saved_at')}"
        else:
            self.restored = "nothing — a fresh start"

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

    def seed_from_v3(self, v3: dict) -> None:
        fam = v3.get("fam_politics") or {}
        if v3.get("est_politics"):
            self.est = Estimator.from_dict(v3["est_politics"])
        if fam.get("terms"):
            self.terms = TermsStore.from_dict(fam["terms"])
        n = {s: int(v) for s, v in (fam.get("event_n_seen") or {}).items() if v}
        for s, u in (fam.get("universe") or {}).items():
            if (u or {}).get("event_n"):
                n[s] = int(u["event_n"])
        self.event_n = n
        self.names.restore(v3.get("names") or {})
        self.rewards_seen = dict(v3.get("rewards_seen") or {})
        self.paid_seen = dict(v3.get("paid_seen") or {})

    def save(self, force_remote: bool = False) -> None:
        with self.save_lock:
            self.store.save_soon(self.to_dict(), force_remote=force_remote)

    # -- whitelist and labels -----------------------------------------------

    def order_markets(self) -> list[str]:
        return list(dict.fromkeys(o["market"] for o in self.orders if o["market"]))

    def held(self) -> dict[str, float]:
        out = {}
        for s, p in self.positions.items():
            n = to_num(p.get("netPositionDecimal")) if p.get("netPositionDecimal") is not None \
                else to_num(p.get("netPosition"))
            if n:
                out[s] = n
        return out

    def known(self, slug: str) -> bool:
        """The markets a tap may act in: his orders' and holdings' markets,
        and any market he opened that the exchange confirmed is open and
        not an econ market (standing rule: never econ)."""
        if not slug or is_econ(slug):
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

    # -- the meter -----------------------------------------------------------

    def side_pool(self, slug: str, prog) -> float | None:
        """$/day one side of this market competes for, or None while the
        event's size is unconfirmed — then no dollar figure, not a guess."""
        n = self.event_n.get(slug)
        if not n or not prog or not prog.pool:
            return None
        return prog.pool / pool_days(prog, slug) / n / 2.0

    def score(self, o: dict, book, here: list[dict], now: float):
        """The meter's own arithmetic for one order: (Score, prog, pool)."""
        prog = self.terms.get(o["market"])
        if prog is None or not prog.is_live() or book is None:
            return None, prog, None
        pool = self.side_pool(o["market"], prog)
        s = score_resting(o["side"], o["price"], o["size"], top_up_book(book, here),
                          df=prog.df, target=prog.target, daily_side_pool=pool)
        return s, prog, pool

    def sample_once(self, now: float | None = None) -> None:
        now = now if now is not None else self.clock()
        try:
            raw = self.client.open_orders_raw(tries=1, timeout=10.0)
            self.orders = normalize_orders(raw)
            self.orders_at = now
            self.verified_at = now
        except Exception as e:  # noqa: BLE001 — the last list stands, unverified
            self.note(f"open list: {e}")
        first = set(self.terms.joined_today(now))
        meter = [o for o in self.orders if o["market"] not in first]
        try:
            self.est.sample(now, meter, self.cache, self.terms,
                            side_pool=self.side_pool, verified_at=self.verified_at)
        except Exception as e:  # noqa: BLE001
            self.note(f"meter: {e}")
        est: dict[str, float | None] = {}
        by_m: dict[str, list[dict]] = {}
        for o in self.orders:
            by_m.setdefault(o["market"], []).append(o)
        for o in self.orders:
            if o["market"] in first:
                est[o["id"]] = 0.0
                continue
            book = self.cache.fresh(o["market"], BOOK_MAX_AGE, now)
            s, _, _ = self.score(o, book, by_m[o["market"]], now)
            est[o["id"]] = s.est_day if s is not None else None
        self.order_est = est
        try:
            rows = self.client.balances_raw()
            best = max(rows, key=lambda r: to_num(r.get("buyingPower")), default=None)
            if best is not None:
                self.balance, self.balance_at = dict(best), now
        except Exception as e:  # noqa: BLE001
            self.note(f"balance: {e}")

    # -- upkeep: reads only ---------------------------------------------------

    def wanted_books(self, now: float) -> list[str]:
        """Markets whose books we keep fresh, in priority order: his
        orders, what he opened lately, then holdings worth a dollar.
        Called from the stream threads too, so it only reads."""
        held = sorted(((to_num((p.get("cashValue") or {}).get("value")
                                if isinstance(p.get("cashValue"), dict) else p.get("cashValue")), s)
                       for s, p in self.positions.items()), reverse=True)
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

    def run_discover(self) -> None:
        try:
            found = discover(self.client)
        except Exception as e:  # noqa: BLE001
            self.note(f"discover: {e}")
            return
        if len(found) < 0.6 * len(self.event_n):
            # a short feed is merged in, never taken whole
            self.note(f"discover: {len(found)} markets against {len(self.event_n)} known — merged")
        for s, r in found.items():
            self.event_n[s] = int(r["event_n"])
            if r.get("name"):
                self.names.known[s] = r["name"]

    def check_rewards(self, now: float, write_file: bool) -> None:
        days = 40 if write_file else 6
        start = records.utc_day(now, days)
        try:
            rows = self.client.earnings(start)
        except Exception as e:  # noqa: BLE001
            self.note(f"payouts: {e}")
            return
        res = records.check_rewards(rows, self.rewards_seen, self.paid_seen, now)
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
        self.save(force_remote=bool(res["new_count"]))

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
        if self._is_due("positions", POSITIONS_S, now):
            self.refresh_positions(now)
        self.refresh_books(now)
        if self._is_due("terms", TERMS_S, now):
            self.refresh_terms(now)
        if self._is_due("rewards_file", REWARDS_FILE_S, now):
            self.due["rewards"] = now + REWARDS_S
            self.check_rewards(now, write_file=True)
        elif self._is_due("rewards", REWARDS_S, now):
            self.check_rewards(now, write_file=False)
        if self._is_due("trades", TRADES_S, now):
            self.publish_trades()
        if self._is_due("save", SAVE_S, now):
            self.save()

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

    def _read_orders(self) -> None:
        try:
            self.orders = normalize_orders(self.client.open_orders_raw(tries=2, timeout=10.0))
            self.orders_at = self.verified_at = self.clock()
        except Exception as e:  # noqa: BLE001
            self.note(f"open list after a tap: {e}")

    def _net(self, slug: str) -> float:
        """His position in this market, read now (it decides whether an
        ask sells what he holds and whether a bid buys a short back)."""
        self.refresh_positions(self.clock())
        return self.held().get(slug, 0.0)

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
        with self.tap_lock:
            if not self.known(slug):
                return {"ok": False, "note": "not a market of yours — open it first"}
            try:
                self._tap_book(slug)
                net = self._net(slug)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "note": f"could not read the market: {e}"}
            close_short = side == "BUY" and net < 0 and qty <= -net + 1e-9
            r = self.desk.place_resting(slug, side, price, qty, net_position=net,
                                        close_short=close_short, initiator="owner")
            if r.order_id:
                self.placed_ids[r.order_id] = round(self.clock(), 1)
            self._read_orders()
        return {"ok": r.ok, "note": r.note, "id": r.order_id, "price": r.price,
                "resting": r.resting_qty}

    def _find(self, order_id: str) -> dict | None:
        return next((o for o in self.orders if o["id"] == order_id), None)

    def move(self, order_id: str, cents: float | None = None, qty: float | None = None) -> dict:
        """New price and/or size: the replacement is placed and confirmed
        resting by its id and size BEFORE the original is cancelled (never
        the exchange's modify, which destroys the order)."""
        with self.tap_lock:
            self._read_orders()
            o = self._find(order_id)
            if o is None:
                return {"ok": False, "note": "that order is not on the open list"}
            if o["intent"] not in REST_SIDE:
                return {"ok": False, "note": "the exchange gave no intent for it — cancel and place instead"}
            try:
                price = round(float(cents) / 100.0, 4) if cents not in (None, "") else o["price"]
                size = round(float(qty), 2) if qty not in (None, "") else o["size"]
            except (TypeError, ValueError):
                return {"ok": False, "note": "price and size must be numbers"}
            if size < 0.01 or size > QTY_MAX:
                return {"ok": False, "note": f"size must be 0.01 to {QTY_MAX:,.0f}"}
            if abs(price - o["price"]) < 1e-9 and abs(size - o["size"]) < 1e-9:
                return {"ok": False, "note": "nothing to change"}
            try:
                self._tap_book(o["market"])
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "note": f"could not read the market: {e}"}
            r = self.desk.reprice(dict(o), price, size, initiator="owner", keep_trimmed=True)
            if r.order_id:
                self.placed_ids[r.order_id] = round(self.clock(), 1)
            self._read_orders()
        note = r.note
        if r.two_orders:
            note += " — the original could not be cancelled, so BOTH rest; cancel one"
        return {"ok": r.ok, "note": note, "id": r.order_id, "price": r.price}

    def cancel(self, order_id: str) -> dict:
        with self.tap_lock:
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
        if is_econ(slug):
            return {"ok": False, "note": "econ markets are off limits"}
        if not self.known(slug):
            try:
                md = self.client.market_details(slug)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "note": f"no such market: {e}"}
            if md.get("closed") or md.get("active") is False:
                return {"ok": False, "note": "that market is closed"}
            self.checked[slug] = self.clock()
            self.names.learn(slug, md)
        self.opened[slug] = self.clock()
        if self.terms.get(slug) is None:
            self.refresh_terms(self.clock(), [slug])
        return {"ok": True}

    # -- what the page reads --------------------------------------------------

    def book_view(self, slug: str) -> dict:
        """The order book with his orders marked, his position, and the
        program the market pays under."""
        r = self.open_market(slug)
        if not r["ok"]:
            return r
        try:
            book = self._tap_book(slug)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "note": f"book: {e}"}
        now = self.clock()
        mine = [o for o in self.orders if o["market"] == slug]
        prog = self.terms.get(slug)
        pool = self.side_pool(slug, prog) if prog is not None else None
        p = self.positions.get(slug) or {}
        net = self.held().get(slug, 0.0)
        return {
            "ok": True, "market": slug, "name": self.label(slug),
            "age": round(now - book.fetched_at, 1), "tick": book.tick,
            "bids": [list(x) for x in book.bids[:10]], "asks": [list(x) for x in book.asks[:10]],
            "ours": [{"id": o["id"], "side": o["side"], "price": o["price"], "size": o["size"],
                      "est": self.order_est.get(o["id"])} for o in mine],
            "net": net, "value": to_num((p.get("cashValue") or {}).get("value")
                                        if isinstance(p.get("cashValue"), dict) else p.get("cashValue")),
            "pool": pool, "target": prog.target if prog else None,
            "df": prog.df if prog else None, "live": bool(prog and prog.is_live()),
            "first_day": slug in self.terms.joined_today(now),
        }

    def order_math(self, order_id: str) -> dict:
        """The earnings math for one order, the meter's own arithmetic."""
        o = self._find(order_id)
        if o is None:
            return {"ok": False, "note": "that order is not on the open list"}
        view = self.book_view(o["market"])
        if not view.get("ok"):
            return view
        now = self.clock()
        here = [x for x in self.orders if x["market"] == o["market"]]
        book = self.cache.any_age(o["market"])
        s, prog, pool = self.score(o, book, here, now)
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
            "dots": list(self.est.dots),
            "bp": to_num(b.get("buyingPower")) if b else None,
            "cash": to_num(b.get("availableToWithdraw")) if b else None,
            "bp_age": round(now - self.balance_at, 0) if self.balance_at else None,
            "orders": orders, "orders_age": round(now - self.orders_at, 0) if self.orders_at else None,
            "holdings": holdings, "small_n": small_n, "small_v": round(small_v, 2),
            "holdings_value": round(sum(h["value"] for h in holdings) + small_v, 2),
            "rw_last": self.rw_last,
        }

    # -- running -------------------------------------------------------------

    def _loop(self, name: str, every: float, fn) -> None:
        def run():
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
            st = Stream(self.cache, lambda: self.wanted_books(self.clock()), kid, sec,
                        shard=i, shards=2, name=f"lite-books-{i}")
            st.start()
            self.streams.append(st)

    def shutdown_save(self, why: str) -> None:
        OrderDesk.halted = why
        try:
            self.save(force_remote=True)
            self.store.wait_remote(25.0)
        except Exception as e:  # noqa: BLE001
            print(f"lite: stop save failed: {e}", flush=True)
