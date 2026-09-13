"""The dust sweep (owner, 2026-09-13).

"Let's just do the holdings that are less than 1 dollar when valued at
a midpoint price between the bid and the ask. If the difference between
the bid and the ask is 1 cent, then sell at the ask. Otherwise list at
the midpoint. If the midpoint is not a whole number you can round
toward the price that it could be sold at." Then: "Replace all my hand
orders" and "Place once and leave it. Give me a button to run it again."

Every held position, in every family, whose shares x live midpoint is
under SWEEP_MAX_VALUE_USD gets ONE exit at the rule's price: a long
sells at the ask on a one-tick spread, else at the midpoint rounded
DOWN to the tick (toward the bid, the side it could be sold at); a
short buys back at the bid on a one-tick spread, else at the midpoint
rounded UP (toward the ask). Sized to the whole lot, post-only, never
inside the touch. Every order already on that side comes off first --
the engine's, the tender's and his hand's (his carve-out) -- except his
qualifying walls, which by the house convention offer none of the lot.
Each order is priced on a book read the second it goes on, never on the
plan's (owner, 2026-09-13 "Can you read the book immediately before
placing": the first run placed 7 and was refused 95, "no book fresher
than 120s", because the placing loop outlived the plan's reads).
Placed once and left; the button runs another pass. Frozen ground and
close-out ground are skipped. The order rests as purpose "sweep": the
engine treats it as hands-off like his own and nets it out of every
exit it sizes, and the tender counts it as cover on the exit side, so
the lot is never offered twice. Nothing here runs on its own -- only
his tap.
"""

from __future__ import annotations

import math
import threading
import time

from .family import FamilyOrder, is_wall
from .intents import SELL_LONG, SELL_SHORT

SWEEP_MAX_VALUE_USD = 1.0       # a holding worth less than this at the midpoint
SWEEP_BOOK_MAX_AGE_S = 120.0    # planning: a cached book older than this is re-read
# PLACING reads the book again, right before the order goes on (owner,
# 2026-09-13 "Can you read the book immediately before placing"). A book
# the stream delivered within this many seconds already is that read, so
# it stands and spends no gateway read.
SWEEP_PLACE_BOOK_MAX_AGE_S = 30.0
SWEEP_READ_TIMEOUT_S = 8.0      # one read, this long
SWEEP_READ_TRIES = 3            # ...and it waits out a 429 rather than lose the lot
PURPOSE = "sweep"
LOG_KEEP = 40


def _floor_tick(x: float, tick: float) -> float:
    return round(math.floor(x / tick + 1e-9) * tick, 4)


def _ceil_tick(x: float, tick: float) -> float:
    return round(math.ceil(x / tick - 1e-9) * tick, 4)


def price_for(qty: float, bid: float, ask: float, tick: float) -> tuple[str, float, str]:
    """The owner's rule for one holding. Returns (book side, price,
    intent). A one-tick spread has no inside, so the exit joins the far
    side of the touch; a wider one sits at the midpoint, rounded toward
    the side the position could be closed at right now."""
    tick = float(tick or 0.01)
    one_tick = (ask - bid) <= tick * 1.0001
    if qty > 0:
        px = ask if one_tick else _floor_tick((bid + ask) / 2.0, tick)
        return "SELL", round(px, 4), SELL_LONG
    px = bid if one_tick else _ceil_tick((bid + ask) / 2.0, tick)
    return "BUY", round(px, 4), SELL_SHORT


class Sweep:
    def __init__(self, families: dict, client, clock=None):
        self.families = families        # key -> Family (all of them)
        self.client = client
        self._clock = clock or time.time
        self.last: dict = {}            # the last run, shown on the page and saved
        self.preview: dict = {}         # the last preview, shown until a run acts on it
        self.log: list[dict] = []
        # a tap returns at once; the work runs here, and the card polls
        # /sweep.json for its progress (2026-09-13: the first tap read
        # ~120 books through the throttled gateway on the web thread and
        # the page, frozen at cycle end, showed nothing for minutes)
        self.busy: dict | None = None   # {"op", "phase", "started", "done", "total"}
        self.error: str = ""
        self._thread: threading.Thread | None = None

    # -- the plan ------------------------------------------------------------

    def plan(self, now: float, busy: dict | None = None) -> dict:
        """Read the books and apply the rule; place nothing. `busy` is
        the progress record the page polls (done / total holdings)."""
        rows: list[dict] = []
        skipped: list[dict] = []
        held = [(key, slug, inv) for key, fam in self.families.items()
                for slug, inv in list((getattr(fam, "inventory", None) or {}).items())
                if abs(float((inv or {}).get("qty") or 0.0)) >= 0.01]
        if busy is not None:
            busy.update(phase="reading", done=0, total=len(held))
        for key, slug, inv in held:
            fam = self.families[key]
            if busy is not None:
                busy["done"] = busy.get("done", 0) + 1
            if True:
                qty = round(float((inv or {}).get("qty") or 0.0), 2)
                name = fam._label(slug)
                if fam._frozen(slug):
                    skipped.append({"market": slug, "name": name, "why": "frozen ground"})
                    continue
                if fam._liquidating(slug):
                    skipped.append({"market": slug, "name": name,
                                    "why": "close-out ground — already being sold"})
                    continue
                book = fam.cache.fresh(slug, SWEEP_BOOK_MAX_AGE_S, self._clock())
                if book is None:
                    try:
                        # stamped at its OWN read, never at the pass's start:
                        # a pass over a hundred holdings takes minutes through
                        # the throttled gateway, and a book stamped with the
                        # start reads minutes old the moment it lands
                        book = self.client.book(slug, fetched_at=None,
                                                timeout=SWEEP_READ_TIMEOUT_S,
                                                tries=2, priority=True)
                        fam.cache.put(slug, book)
                    except Exception as e:  # noqa: BLE001 — said, skipped
                        skipped.append({"market": slug, "name": name,
                                        "why": f"no book: {str(e)[:70]}"})
                        continue
                if not book.bids or not book.asks:
                    skipped.append({"market": slug, "name": name, "why": "one-sided book"})
                    continue
                bid, ask = float(book.bids[0][0]), float(book.asks[0][0])
                mid = (bid + ask) / 2.0
                value = abs(qty) * mid
                if value >= SWEEP_MAX_VALUE_USD:
                    continue
                tick = float(getattr(book, "tick", 0.0) or 0.01)
                side, px, intent = price_for(qty, bid, ask, tick)
                replace = [o for o in list(fam.orders.values())
                           if o.market == slug and o.side == side
                           and not is_wall(o, abs(qty))]
                rows.append({
                    "key": key, "market": slug, "name": name, "qty": qty,
                    "bid": bid, "ask": ask, "mid": round(mid, 4),
                    "value": round(value, 2), "side": side, "price": px,
                    "intent": intent, "replace": [o.id for o in replace],
                    "hand": sum(1 for o in replace if o.purpose == "manual")})
        plan = {"at": round(now, 1), "rows": rows, "skipped": skipped,
                "n": len(rows), "value": round(sum(r["value"] for r in rows), 2),
                "replace": sum(len(r["replace"]) for r in rows),
                "hand": sum(r["hand"] for r in rows)}
        self.preview = plan
        return plan

    # -- the run -------------------------------------------------------------

    def _book_to_place_on(self, fam, slug: str):
        """The book this order is priced and placed on, read RIGHT NOW.

        Owner, 2026-09-13 ("Can you read the book immediately before
        placing"): the 16:51Z run placed 7 orders and was refused 95,
        every refusal "no book fresher than 120s — refusing to place
        blind". The plan had priced all 102 holdings up front and the
        placing loop that followed took minutes — a cancel and a
        placement a market, one at a time, through a gateway answering
        3-4 429s a minute — so by the fiftieth order the book it was
        pricing from was minutes old and the desk's own freshness gate
        threw it out. Each order now gets its own read and its price
        comes from that read, so his rule lands on the book as it stands
        the second the order goes on.

        A book the stream delivered seconds ago already IS that read, so
        a cache entry younger than SWEEP_PLACE_BOOK_MAX_AGE_S stands and
        spends no gateway read."""
        b = fam.cache.fresh(slug, SWEEP_PLACE_BOOK_MAX_AGE_S, self._clock())
        if b is not None:
            return b
        book = self.client.book(slug, fetched_at=None,
                                timeout=SWEEP_READ_TIMEOUT_S,
                                tries=SWEEP_READ_TRIES, priority=True)
        fam.cache.put(slug, book)
        return book

    def run(self, now: float) -> dict:
        """His tap: plan the candidates, then for each one read its book,
        price it on THAT read, cancel what rests on the exit side and
        place the one exit. A refused placement is reported, its lot left
        for the engine or tender to re-offer on their next pass; the
        button runs again."""
        plan = self.plan(now, busy=self.busy)
        placed: list[dict] = []
        failed: list[dict] = []
        skipped: list[dict] = list(plan["skipped"])
        if self.busy is not None:
            self.busy.update(phase="placing", done=0, total=len(plan["rows"]))
        for r in plan["rows"]:
            fam = self.families[r["key"]]
            if self.busy is not None:
                self.busy["done"] = self.busy.get("done", 0) + 1
            slug, qty = r["market"], r["qty"]
            row = {k: r[k] for k in ("key", "market", "name", "qty", "bid", "ask",
                                     "mid", "value", "side", "price", "hand")}
            row["replaced"] = []
            # the book as it stands this second, never the plan's
            try:
                book = self._book_to_place_on(fam, slug)
            except Exception as e:  # noqa: BLE001 — said on the card, lot left alone
                row["note"] = f"no book to place on: {str(e)[:90]}"
                failed.append(row)
                fam._log(event="sweep_refused", market=slug, side=r["side"],
                         price=r["price"], qty=abs(qty), note=row["note"][:120])
                continue
            if not book.bids or not book.asks:
                skipped.append({"market": slug, "name": r["name"],
                                "why": "one-sided book when the order came up"})
                continue
            bid, ask = float(book.bids[0][0]), float(book.asks[0][0])
            mid = (bid + ask) / 2.0
            value = abs(qty) * mid
            tick = float(getattr(book, "tick", 0.0) or 0.01)
            side, px, intent = price_for(qty, bid, ask, tick)
            row.update(bid=bid, ask=ask, mid=round(mid, 4),
                       value=round(value, 2), side=side, price=px)
            # his dollar, tested on the same read the price comes from
            if value >= SWEEP_MAX_VALUE_USD:
                skipped.append({
                    "market": slug, "name": r["name"],
                    "why": (f"worth ${value:.2f} at the midpoint when the order came "
                            f"up — over the dollar, left alone")})
                continue
            # and what rests on the exit side NOW, not when the plan ran:
            # the engine or the tender may have laid something there since,
            # and the lot is never offered twice. A previous sweep's own
            # order is replaced like any other — that is what running the
            # button again means.
            replace = [o for o in list(fam.orders.values())
                       if o.market == slug and o.side == side
                       and not is_wall(o, abs(qty))]
            row["hand"] = sum(1 for o in replace if o.purpose == "manual")
            gone: list[str] = []
            for o in replace:
                oid = o.id
                if oid not in fam.orders:
                    continue
                rr = fam.desk.cancel(oid, slug, initiator="owner")
                if rr.ok:
                    fam.orders.pop(oid, None)
                    try:
                        fam.evidence.order_gone(slug, oid)
                    except Exception:  # noqa: BLE001
                        pass
                    gone.append(oid)
            res = fam.desk.place_resting(
                slug, side, px, abs(qty),
                net_position=qty, intent=intent,
                close_short=(side == "BUY"), initiator="owner", verify=False)
            row["replaced"] = gone
            if not res.ok or not res.order_id:
                row["note"] = res.note
                failed.append(row)
                fam._log(event="sweep_refused", market=slug, side=side,
                         price=px, qty=abs(qty), note=str(res.note)[:120])
                continue
            got = float(res.price or px)
            fam.orders[res.order_id] = FamilyOrder(
                id=res.order_id, market=slug, side=side, price=got,
                qty=abs(qty), intent=intent, placed_ts=self._clock(), purpose=PURPOSE,
                why=(f"the dust sweep (owner, 2026-09-13): {abs(qty):g} sh worth "
                     f"${value:.2f} at the midpoint, listed at the rule's price"))
            row["id"] = res.order_id
            row["price"] = got
            placed.append(row)
            fam._log(event="sweep", market=slug, side=side, price=got,
                     qty=abs(qty),
                     note=(f"worth ${value:.2f} at mid {mid * 100:.1f}c on a book read "
                           f"now; replaced {len(gone)} ({row['hand']} yours)"))
        self.last = {"at": round(now, 1), "placed": placed, "failed": failed,
                     "skipped": skipped, "n": len(placed),
                     "value": round(sum(r["value"] for r in placed), 2),
                     "hand": sum(r["hand"] for r in placed)}
        self.log.append({"ts": round(now, 1), "placed": len(placed), "failed": len(failed),
                         "value": self.last["value"], "hand": self.last["hand"]})
        del self.log[:-LOG_KEEP]
        self.preview = {}
        return self.last

    # -- his tap: work in the background, answer at once ---------------------

    def start(self, op: str, now: float, on_done=None) -> dict:
        """Begin a preview or a run on a worker thread and return at once.
        One at a time: a tap while one is running answers with its
        progress instead of starting another. `on_done(result)` runs on
        the worker when a run finishes (the monitor saves and notifies)."""
        if op not in ("preview", "run"):
            return {"ok": False, "note": f"unknown sweep op {op}"}
        if self.busy is not None and self._thread is not None and self._thread.is_alive():
            b = self.busy
            return {"ok": False,
                    "note": (f"already {b.get('op')}ing — {b.get('phase')} "
                             f"{b.get('done', 0)} of {b.get('total', 0)}")}
        self.error = ""
        self.busy = {"op": op, "phase": "starting", "started": round(now, 1),
                     "done": 0, "total": 0}

        def work() -> None:
            try:
                if op == "preview":
                    self.plan(now, busy=self.busy)
                else:
                    result = self.run(now)
                    if on_done is not None:
                        on_done(result)
            except Exception as e:  # noqa: BLE001 — said on the card, never silent
                self.error = f"{type(e).__name__}: {str(e)[:160]}"
            finally:
                self.busy = None

        self._thread = threading.Thread(target=work, daemon=True, name=f"sweep-{op}")
        self._thread.start()
        held = sum(1 for fam in self.families.values()
                   for inv in (getattr(fam, "inventory", None) or {}).values()
                   if abs(float((inv or {}).get("qty") or 0.0)) >= 0.01)
        return {"ok": True,
                "note": (f"reading the books of {held} holdings — the card updates as "
                         f"it goes" + (", then places" if op == "run" else ""))}

    # -- the page and the state ----------------------------------------------

    def view(self) -> dict:
        return {"last": self.last, "preview": self.preview,
                "max_value": SWEEP_MAX_VALUE_USD,
                "busy": dict(self.busy) if self.busy else None,
                "error": self.error}

    def to_dict(self) -> dict:
        return {"last": self.last, "log": list(self.log)}

    def restore(self, d: dict) -> None:
        if not isinstance(d, dict):
            return
        self.last = dict(d.get("last") or {})
        self.log = list(d.get("log") or [])[-LOG_KEEP:]
