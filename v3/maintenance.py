"""Scheduled maintenance (owner, 2026-09-14).

"There is maintenance from 4-8:30 this morning. Can you pull the orders
15 minutes before and start putting them back in approximately their
correct spot after everything is back online? Don't have to be too
aggressive. If you can get better prices or higher earning in thinner
books do that. Stay flexible."

The exchange's maintenance of 2026-09-11 cancelled EVERY resting order,
his qualifying walls included, and nothing re-places a wall on its own:
the day after it the whole bid side of the balance-of-power book read as
earning nothing because the 25,000-share 1c wall that carried it to the
target was gone. That is what this is for. Fifteen minutes before the
window we take the open list -- the exchange's own record, not ours --
write down every order on it, and cancel them all. His hand's orders and
his walls are pulled too: this is the one time that helps him, because
the maintenance would cancel them anyway and only this snapshot can put
them back.

While the window runs nothing places: the desks read the master as off
(his stored setting is never touched) and his own taps still work.

After the window, once the exchange answers a read again, the orders go
back a few a pass -- not aggressive, his word. Each is re-placed from a
book read for it:

  a qualifying wall (1c or 99c)   exactly where it was, at its own size
  a bid                           its old price, or the best bid if that
                                  is LOWER -- never pay up
  an ask                          its old price, or the best ask if that
                                  is HIGHER -- never sell cheaper

so it lands at its old spot or at the touch, whichever is better for
him, and on a side that thinned out over the window the touch is both
the better price AND the higher earning slot. Nothing crosses. Anything
the exchange refuses waits for the next pass and is said on the page.
"""

from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# The windows he has told us about, as (start, end) in ET. A window is
# a plain pair of local times because that is how he reads them off the
# exchange's notice.
MAINT_WINDOWS: tuple[tuple[str, str], ...] = (
    ("2026-09-14T04:00", "2026-09-14T08:30"),
)

MAINT_PULL_LEAD_S = 900.0      # "15 minutes before"
MAINT_SETTLE_S = 120.0         # after the window, before the first read
MAINT_RESTORE_PER_PASS = 6     # "Don't have to be too aggressive"
MAINT_RESTORE_TRIES = 6        # per order, then it is left and reported
MAINT_BOOK_MAX_AGE_S = 30.0
# The desk will not send more than QTY_MAX in one order, and his
# qualifying walls are bigger than that (a 1c bid of 25,000 is what
# carries a side to a 25,000 target). A wall goes back as however many
# orders it takes at the same price — the book sees the same size, which
# is all the program counts.
MAINT_CHUNK_MAX = 19_990.0
# A book that has not refilled after the window has a touch that means
# nothing — the 12:32Z restore of 2026-09-14 put a 106-share bid back at
# 14c where he had it at 42c, and an ask at 62c where he had it at 43c,
# because the North Carolina House 01 book had come back 14c/62c with
# almost nothing on it. His own rule from the 2026-09-11 maintenance is
# the one that applies ("Be careful of placing orders after the
# maintenance. Don't sell everything for pennies there might not be any
# orders resting"): past this spread the touch is not trusted and the
# order goes back where HE had it, which on an empty book is the better
# estimate of fair. Nothing is at risk either way — the touch is only
# ever taken when it is better for him — but "approximately their
# correct spot" is what he asked for.
MAINT_TRUST_SPREAD = 0.10
MAINT_LIVE_RECHECK_S = 30.0    # how often the restore re-reads what is
                               # actually resting, so an order that never
                               # left is never placed a second time
MAINT_READ_TIMEOUT_S = 8.0
MAINT_READ_TRIES = 3
WALL_PRICES = (0.01, 0.99)
WALL_MIN_QTY = 5000.0
LOG_KEEP = 40


def _et(stamp: str) -> float:
    """An ET wall-clock string to a unix time."""
    return dt.datetime.fromisoformat(stamp).replace(tzinfo=ET).timestamp()


def windows() -> list[tuple[str, float, float]]:
    """(key, start, end) for every window he has given us."""
    out = []
    for a, b in MAINT_WINDOWS:
        try:
            out.append((f"{a}/{b}", _et(a), _et(b)))
        except Exception:  # noqa: BLE001 — a bad window is ignored, never fatal
            continue
    return out


def is_wall(price: float, qty: float) -> bool:
    """His qualifying wall: the far edge of the book at size. It goes
    back exactly as it was — it is what carries a side to the program's
    target, and the model reads its size as on the book."""
    return round(float(price), 4) in WALL_PRICES and float(qty) >= WALL_MIN_QTY


def restore_price(side: str, was: float, bid: float | None,
                  ask: float | None, tick: float) -> float | None:
    """Where an order goes back. His old price, or the touch when the
    touch is better for him — never across it, never worse than where he
    was. On a book that has not refilled (a spread wider than
    MAINT_TRUST_SPREAD) the touch is not trusted and his own price
    stands. Returns None when the book cannot support the order."""
    tick = float(tick or 0.01)
    was = float(was)
    wide = (bid is not None and ask is not None
            and (ask - bid) > MAINT_TRUST_SPREAD + 1e-12)
    if side == "BUY":
        px = was if wide else (min(was, bid) if bid is not None else was)
        if ask is not None and px >= ask - 1e-12:
            px = round(ask - tick, 4)          # never cross
        return px if px > 0 else None
    px = was if wide else (max(was, ask) if ask is not None else was)
    if bid is not None and px <= bid + 1e-12:
        px = round(bid + tick, 4)
    return px if px < 1.0 else None


class Maintenance:
    """The window's whole state machine. Ticked from the sampler's clock
    (every 20 s) so a slow cycle can never make it late."""

    def __init__(self, families: dict, client, clock=None, alert=None):
        self.families = families
        self.client = client
        self._clock = clock or time.time
        self.alert = alert
        self.key = ""            # the window we are working
        self.phase = "idle"      # idle | pulled | restoring | done
        self.pulled_at = 0.0
        self.orders: list[dict] = []     # the snapshot, richest record we have
        self.done: list[dict] = []       # restored, with where they landed
        self.failed: list[dict] = []
        self.note = ""
        self.log: list[dict] = []
        self._live: set[str] = set()     # ids the EXCHANGE says are resting
        self._live_at = 0.0

    # -- where we are --------------------------------------------------------

    def current(self, now: float | None = None):
        """The window in play: from its pull time until we have finished
        putting the orders back."""
        now = now if now is not None else self._clock()
        for key, a, b in windows():
            if a - MAINT_PULL_LEAD_S <= now <= b + 6 * 3600.0:
                return key, a, b
        return None

    def holding(self, now: float | None = None) -> bool:
        """True while nothing may place: from the pull to the end of the
        window. The restore itself runs with the owner's own hand, so it
        is not blocked by this."""
        now = now if now is not None else self._clock()
        w = self.current(now)
        if w is None:
            return False
        _, a, b = w
        return (a - MAINT_PULL_LEAD_S) <= now <= b

    # -- the machine ---------------------------------------------------------

    def tick(self, now: float | None = None) -> str:
        now = now if now is not None else self._clock()
        w = self.current(now)
        if w is None:
            return self.phase
        key, a, b = w
        if key != self.key:
            self.key, self.phase = key, "idle"
            self.orders, self.done, self.failed = [], [], []
        if self.phase == "idle" and now >= a - MAINT_PULL_LEAD_S:
            self.pull(now)
        elif self.phase == "pulled" and now >= b + MAINT_SETTLE_S:
            if self._exchange_answers():
                self.phase = "restoring"
                self._say(f"the exchange is answering again — putting "
                          f"{len(self.orders)} orders back, a few at a time")
        elif self.phase == "restoring":
            self.restore_step(now)
        return self.phase

    def _exchange_answers(self) -> bool:
        """One cheap signed read. Until it answers, we wait."""
        try:
            self.client.open_orders_raw(tries=1, timeout=10.0)
            return True
        except Exception:  # noqa: BLE001 — still down; try again next tick
            return False

    # -- the pull ------------------------------------------------------------

    def pull(self, now: float) -> dict:
        """Write down every order the EXCHANGE says is resting, then
        cancel them all. The exchange's list is the record, not ours: an
        order of his we never adopted is on it and is exactly the kind
        that never comes back on its own."""
        try:
            rows = self.client.open_orders()
        except Exception as e:  # noqa: BLE001 — try again on the next tick
            self.note = f"could not read the open list: {str(e)[:90]}"
            return {"ok": False, "note": self.note}
        snap: list[dict] = []
        for o in rows:
            slug = o.get("market")
            key = self._family_of(slug)
            rec = {"market": slug, "side": o.get("side"),
                   "price": float(o.get("price") or 0.0),
                   "qty": float(o.get("size") or 0.0),
                   "id": str(o.get("id") or ""), "key": key,
                   "intent": o.get("intent"), "tries": 0}
            fam = self.families.get(key) if key else None
            mine = (fam.orders.get(rec["id"]) if fam else None)
            rec["purpose"] = getattr(mine, "purpose", "manual")
            rec["wall"] = is_wall(rec["price"], rec["qty"])
            snap.append(rec)
        gone = 0
        for rec in snap:
            fam = self.families.get(rec["key"]) if rec["key"] else None
            if fam is None:
                continue
            try:
                r = fam.desk.cancel(rec["id"], rec["market"], initiator="owner")
            except Exception:  # noqa: BLE001 — counted, said, left
                continue
            if r.ok:
                gone += 1
                fam.orders.pop(rec["id"], None)
                try:
                    fam.evidence.order_gone(rec["market"], rec["id"])
                except Exception:  # noqa: BLE001
                    pass
        self.orders = snap
        self.pulled_at = now
        self.phase = "pulled"
        walls = sum(1 for r in snap if r["wall"])
        hands = sum(1 for r in snap if r["purpose"] == "manual")
        self.note = (f"pulled {gone} of {len(snap)} before the maintenance "
                     f"({hands} yours, {walls} qualifying walls)")
        self._say(self.note)
        return {"ok": True, "n": len(snap), "cancelled": gone}

    # -- putting them back ---------------------------------------------------

    def _refresh_live(self, now: float) -> None:
        """What the EXCHANGE says is resting, not what our books say. The
        pull cancels, but a cancel can be refused and the open list lags
        one either way (2026-09-14: 29 of the 313 pulled were still
        showing when the window opened, and the desk was re-cancelling
        them). An order that never left must NOT be placed a second time
        — that is the same shape that offers a lot twice."""
        if now - self._live_at < MAINT_LIVE_RECHECK_S:
            return
        try:
            rows = self.client.open_orders()
        except Exception:  # noqa: BLE001 — keep the last read; it only ever
            return          # stops us placing, never causes one
        self._live = {str(o.get("id") or "") for o in rows}
        self._live_at = now

    def restore_step(self, now: float) -> int:
        """A few a pass. Each order is priced on a book read for it."""
        self._refresh_live(now)
        left = [r for r in self.orders
                if not r.get("back") and r.get("tries", 0) < MAINT_RESTORE_TRIES]
        if not left:
            self.phase = "done"
            self.note = (f"back on: {len(self.done)} restored, "
                         f"{len(self.failed)} could not be")
            self._say(self.note)
            return 0
        n = 0
        for rec in left[:MAINT_RESTORE_PER_PASS]:
            if rec.get("id") and rec["id"] in self._live:
                # it never came off: leave it exactly where it is
                rec["back"] = True
                rec["at"] = float(rec["price"])
                rec["moved"] = 0.0
                rec["kept"] = True
                self.done.append(rec)
                n += 1
                continue
            rec["tries"] = rec.get("tries", 0) + 1
            if self._restore_one(rec, now):
                n += 1
        return n

    def _restore_one(self, rec: dict, now: float) -> bool:
        key = rec.get("key")
        fam = self.families.get(key) if key else None
        if fam is None:
            rec["why"] = "no desk for this market"
            self.failed.append(rec)
            rec["back"] = True
            return False
        slug, side = rec["market"], rec["side"]
        book = fam.cache.fresh(slug, MAINT_BOOK_MAX_AGE_S, self._clock())
        if book is None:
            try:
                book = self.client.book(slug, fetched_at=None,
                                        timeout=MAINT_READ_TIMEOUT_S,
                                        tries=MAINT_READ_TRIES, priority=True)
                fam.cache.put(slug, book)
            except Exception as e:  # noqa: BLE001 — next pass
                rec["why"] = f"no book: {str(e)[:70]}"
                return False
        bid = float(book.bids[0][0]) if book.bids else None
        ask = float(book.asks[0][0]) if book.asks else None
        tick = float(getattr(book, "tick", 0.0) or 0.01)
        if rec.get("wall"):
            px = float(rec["price"])        # exactly where it was
        else:
            px = restore_price(side, rec["price"], bid, ask, tick)
        if px is None:
            rec["why"] = "the book cannot hold it"
            return False
        want = float(rec["qty"])
        pos = float((fam.inventory.get(slug) or {}).get("qty") or 0.0)
        ids: list[str] = []
        left = want
        while left > 0.005:
            lot = round(min(left, MAINT_CHUNK_MAX), 2)
            try:
                r = fam.desk.place_resting(
                    slug, side, px, lot, net_position=pos,
                    initiator="owner", verify=False)
            except Exception as e:  # noqa: BLE001 — next pass
                rec["why"] = str(e)[:90]
                break
            if not r.ok or not r.order_id:
                rec["why"] = str(r.note)[:110]
                break
            ids.append(r.order_id)
            left = round(left - lot, 2)
        if left > 0.005:
            rec["placed_qty"] = round(want - left, 2)
            if rec["tries"] >= MAINT_RESTORE_TRIES:
                rec["back"] = True
                self.failed.append(rec)
            return bool(ids)
        rec["back"] = True
        rec["at"] = px
        rec["moved"] = round(px - float(rec["price"]), 4)
        rec["new_id"] = ids[0] if ids else ""
        rec["parts"] = len(ids)
        self.done.append(rec)
        fam._log(event="maint_restored", market=slug, side=side, price=px,
                 qty=float(rec["qty"]),
                 note=(f"back after the maintenance — was {rec['price'] * 100:.1f}c"
                       + ("" if abs(rec["moved"]) < 1e-9 else
                          f", the touch was better at {px * 100:.1f}c")))
        return True

    # -- plumbing ------------------------------------------------------------

    def _family_of(self, slug: str) -> str:
        for key, fam in self.families.items():
            try:
                if slug in fam.inventory or fam.knows(slug):
                    return key
            except Exception:  # noqa: BLE001
                continue
        return "politics" if "politics" in self.families else ""

    def _say(self, msg: str) -> None:
        self.log.append({"ts": round(self._clock(), 1), "note": msg,
                         "phase": self.phase})
        del self.log[:-LOG_KEEP]
        if self.alert is not None:
            try:
                self.alert("maintenance", msg)
            except Exception:  # noqa: BLE001
                pass

    def view(self) -> dict:
        return {"key": self.key, "phase": self.phase, "note": self.note,
                "n": len(self.orders), "restored": len(self.done),
                "kept": sum(1 for r in self.done if r.get("kept")),
                "failed": len(self.failed),
                "left": sum(1 for r in self.orders if not r.get("back")),
                "windows": [{"start": a, "end": b} for a, b in MAINT_WINDOWS],
                "log": list(self.log)[-8:]}

    def to_dict(self) -> dict:
        return {"key": self.key, "phase": self.phase, "pulled_at": self.pulled_at,
                "orders": self.orders, "done": self.done, "failed": self.failed,
                "note": self.note, "log": list(self.log)}

    def restore_state(self, d: dict) -> None:
        if not isinstance(d, dict):
            return
        self.key = str(d.get("key") or "")
        self.phase = str(d.get("phase") or "idle")
        self.pulled_at = float(d.get("pulled_at") or 0.0)
        self.orders = list(d.get("orders") or [])
        self.done = list(d.get("done") or [])
        self.failed = list(d.get("failed") or [])
        self.note = str(d.get("note") or "")
        self.log = list(d.get("log") or [])[-LOG_KEEP:]
        self._live, self._live_at = set(), 0.0
