"""Bonds: the tax reserve earning like a bond (owner, 2026-09-02).

"Designate any politics market where the Nate Silver gives >99% odds of
an outcome as a bond market. Then on a separate page in the app, give
me a list of the cheapest bond positions with information on what a
resting sell order would earn. Before adding a new market to the bond
list ask me first. Once I'm in the positions they should automatically
[have] a sell order placed so that they are earning. Then when the
sale goes through I'll want the money reinvested in the cheapest
market where it can earn rewards."

What this is and is not. A bond market is a politics market Silver's
model puts at 99% or better for the YES side. Bought at 98-99c it pays
the last cent or two at resolution — November 3 for most of them —
and, held, it is not buying power, so no engine fill can reach it.
It is bond-LIKE across many such markets; on any one of them the
price is the market's own estimate of the chance the lot goes to
zero. The page says so on every row.

The list is the owner's. Silver's numbers PROPOSE a market; nothing
joins the list until he taps Add. Approved markets are frozen ground
for the engine — it places nothing there, rests no exits, sells
nothing. Every order this module places is post-only, on the owner's
rail, and carries purpose "bond", which the engine leaves alone like
his hand orders. Nothing here places an order unless the bonds switch
on /switch is ON (owner's standing rule: no automation without a
switch, two taps on, one tap off).

The money: sale proceeds are a ledger here, not the exchange's cash.
A sale is counted only when OUR bond ask shrank or vanished while the
position fell by that much — the owner selling by hand to pay the
taxes is not reinvested behind his back. Reinvestment rests a bid at
the touch of the cheapest approved market whose bid side pays
rewards; a filled bid gets its own sell, and round it goes.
"""

from __future__ import annotations

import math
import time

from .family import FamilyOrder, slug_days_out
from .intents import BUY_LONG, SELL_LONG

BOND_ODDS = 0.99            # Silver's odds for the YES side to count
PRICE_CAP = 0.995           # never buy above this: the last half cent is not worth the wait
REINVEST_MIN_USD = 5.0      # proceeds below this wait for the next sale
SCAN_EVERY_S = 600.0        # the Silver check for new candidates
MOVE_COOLDOWN_S = 600.0     # an ask is re-rested at most this often
LOG_KEEP = 200


class Bonds:
    def __init__(self, fam, client, fair, alert=None, clock=None):
        self.fam = fam                  # the politics family
        self.client = client
        self.fair = fair                # slug -> Silver's YES odds, or None
        self.alert = alert or (lambda title, msg: None)
        self._clock = clock or time.time
        self.approved: dict[str, dict] = {}   # slug -> {added, odds}
        self.proposed: dict[str, dict] = {}   # slug -> {odds, since}
        self.ignored: dict[str, float] = {}   # slug -> ts
        self.cash: float = 0.0                # sale proceeds awaiting reinvestment
        self.pos_seen: dict[str, float] = {}
        self.moved_at: dict[str, float] = {}
        self.last_scan: float = 0.0
        self.log: list[dict] = []

    # ------------------------------------------------------------ helpers

    def _log(self, **kw) -> None:
        kw["ts"] = round(self._clock(), 1)
        self.log.append(kw)
        del self.log[:-LOG_KEEP]

    def _orders(self, slug: str, side: str | None = None) -> list[FamilyOrder]:
        return [o for o in list(self.fam.orders.values())
                if o.purpose == "bond" and o.market == slug
                and (side is None or o.side == side)]

    def _cost_px(self, slug: str) -> float:
        inv = self.fam.inventory.get(slug) or {}
        q = float(inv.get("qty") or 0.0)
        c = float(inv.get("cost") or 0.0)
        return (c / q) if q > 0.005 and c > 0 else 0.0

    @staticmethod
    def _snap(px: float, tick: float) -> float:
        return round(round(px / tick) * tick, 4)

    def _frozen(self) -> set:
        """Markets the engine must leave alone: every approved one, and
        any market still carrying bond stock or bond orders."""
        out = set(self.approved)
        for o in list(self.fam.orders.values()):
            if o.purpose == "bond":
                out.add(o.market)
        return out

    # ------------------------------------------------------------ the list

    def scan(self, now: float, force: bool = False) -> list[str]:
        """Silver's odds propose; the owner disposes. Returns the newly
        proposed slugs (already alerted)."""
        if not force and now - self.last_scan < SCAN_EVERY_S:
            return []
        self.last_scan = now
        new: list[str] = []
        pool = set(self.fam.universe) | set(self.fam.inventory)
        for slug in sorted(pool):
            p = self.fair(slug)
            if p is None:
                continue
            if slug in self.approved:
                self.approved[slug]["odds"] = round(p, 4)
                continue
            if slug in self.ignored:
                continue
            if p >= BOND_ODDS:
                if slug not in self.proposed:
                    self.proposed[slug] = {"odds": round(p, 4), "since": now}
                    new.append(slug)
                else:
                    self.proposed[slug]["odds"] = round(p, 4)
            else:
                self.proposed.pop(slug, None)
        if new:
            self._log(event="proposed", n=len(new), slugs=new[:6])
            self.alert("Bond candidates",
                       f"{len(new)} politics market{'s' if len(new) > 1 else ''} "
                       f"now at 99%+ per Silver — open the bonds page to "
                       f"add or ignore")
        return new

    def approve(self, slug: str, now: float) -> dict:
        p = self.fair(slug)
        if p is None:
            return {"ok": False, "note": "Silver has no odds for this market"}
        if p < BOND_ODDS:
            return {"ok": False,
                    "note": f"Silver puts it at {p:.1%} — under the 99% bar"}
        self.approved[slug] = {"added": round(now, 1), "odds": round(p, 4)}
        self.proposed.pop(slug, None)
        self.ignored.pop(slug, None)
        self.fam.freeze_dyn.add(slug)
        self._log(event="approved", market=slug, odds=round(p, 4))
        return {"ok": True, "note": f"added — Silver {p:.1%}; the engine "
                                    f"leaves this market alone from now on"}

    def ignore(self, slug: str, now: float) -> dict:
        self.proposed.pop(slug, None)
        self.ignored[slug] = round(now, 1)
        self._log(event="ignored", market=slug)
        return {"ok": True, "note": "ignored — it will not be proposed again"}

    def unignore(self, slug: str) -> dict:
        if self.ignored.pop(slug, None) is None:
            return {"ok": False, "note": "not on the ignore list"}
        self.last_scan = 0.0
        return {"ok": True, "note": "back in the running — the next scan "
                                    "may propose it"}

    def remove(self, slug: str, now: float) -> dict:
        if self.approved.pop(slug, None) is None:
            return {"ok": False, "note": "not on the bond list"}
        self._log(event="removed", market=slug)
        # frozen ground until the stock and the bond orders are gone
        return {"ok": True, "note": "removed — the engine stays out until "
                                    "the position and bond orders are gone"}

    # ------------------------------------------------------------ the money

    def cycle(self, now: float, positions: dict, on: bool) -> dict:
        """Once a cycle, after the family has run: count sales, keep
        every held bond earning with a resting ask, reinvest proceeds.
        Places nothing unless the bonds switch is on."""
        self.scan(now)
        self.fam.freeze_dyn = self._frozen()
        placed: list[dict] = []
        for slug in sorted(set(self.approved) | set(self.pos_seen)):
            pos = float((positions.get(slug) or (0.0, 0.0))[0])
            prev = self.pos_seen.get(slug)
            asks = self._orders(slug, "SELL")
            if prev is not None and pos < prev - 0.005:
                sold = prev - pos
                # OUR ask must have given up that much for it to count
                gave = self._ask_gave(slug, sold)
                if gave > 0.005:
                    px = self._last_ask_px.get(slug, 0.0) if hasattr(self, "_last_ask_px") else 0.0
                    px = px or (asks[0].price if asks else self._cost_px(slug))
                    self.cash = round(self.cash + gave * px, 4)
                    self._log(event="sold", market=slug, qty=round(gave, 2),
                              price=px, proceeds=round(gave * px, 2),
                              cash=round(self.cash, 2))
            self.pos_seen[slug] = pos
        if on:
            for slug in sorted(self.approved):
                pos = float((positions.get(slug) or (0.0, 0.0))[0])
                r = self._keep_earning(slug, pos, now)
                if r:
                    placed.append(r)
            r = self._reinvest(now, positions)
            if r:
                placed.append(r)
        # what our asks show as this cycle ends is what next cycle's
        # sale count is measured against — after any placement
        for slug in set(self.approved) | set(self.pos_seen):
            self._remember_asks(slug)
        return {"placed": placed, "cash": round(self.cash, 2)}

    # the size our asks showed last cycle, so a hand sale by the owner
    # (our ask untouched) is never counted as proceeds to reinvest
    def _remember_asks(self, slug: str) -> None:
        if not hasattr(self, "_ask_seen"):
            self._ask_seen = {}
            self._last_ask_px = {}
        asks = self._orders(slug, "SELL")
        self._ask_seen[slug] = sum(o.qty for o in asks)
        if asks:
            self._last_ask_px[slug] = asks[0].price

    def _ask_gave(self, slug: str, sold: float) -> float:
        seen = getattr(self, "_ask_seen", {}).get(slug, 0.0)
        now_q = sum(o.qty for o in self._orders(slug, "SELL"))
        gave = max(seen - now_q, 0.0)
        return min(gave, sold)

    def _keep_earning(self, slug: str, pos: float, now: float) -> dict | None:
        """A held bond gets one resting ask at the best-earning price at
        or above what it cost: the ask touch when that clears cost, else
        a tick over cost. Re-rested only when the touch dropped under it
        (we fell behind the queue) and the cooldown allows."""
        if pos < 1.0:
            return None
        book = self.fam.cache.fresh(slug, 120.0, now)
        if book is None:
            return None
        tick = book.tick or 0.01
        # never under what it cost: at cost the ask still earns, and a
        # fill there loses nothing (rounded UP onto the grid)
        cost = self._cost_px(slug)
        floor = (round(math.ceil(cost / tick - 1e-9) * tick, 4)
                 if cost > 0 else 0.0)
        touch = book.asks[0][0] if book.asks else None
        want = max(touch if touch is not None else 0.99, floor)
        want = min(self._snap(want, tick), 0.999)
        if book.bids and want <= book.bids[0][0] + 1e-9:
            want = self._snap(book.bids[0][0] + tick, tick)   # post-only
        asks = self._orders(slug, "SELL")
        resting = sum(o.qty for o in asks)
        if asks:
            cur = asks[0]
            behind = touch is not None and cur.price > touch + tick / 2
            if (behind and want < cur.price - tick / 2
                    and now - self.moved_at.get(slug, 0.0) >= MOVE_COOLDOWN_S):
                r = self.fam.desk.place_resting(
                    slug, "SELL", want, cur.qty, net_position=pos,
                    initiator="owner", intent=SELL_LONG)
                if r.ok and r.order_id:
                    self.fam.desk.cancel(cur.id, slug, initiator="owner")
                    self.fam.orders.pop(cur.id, None)
                    self.fam.orders[r.order_id] = FamilyOrder(
                        id=r.order_id, market=slug, side="SELL",
                        price=(r.price or want), qty=cur.qty, intent=r.intent,
                        placed_ts=now, purpose="bond",
                        why="bond: the resting ask earns while it waits — "
                            "moved to the touch")
                    self.moved_at[slug] = now
                    self._log(event="ask_moved", market=slug,
                              price=(r.price or want), qty=cur.qty)
                    return {"market": slug, "side": "SELL",
                            "price": (r.price or want), "qty": cur.qty,
                            "moved": True}
            return None
        qty = float(math.floor(pos - resting))
        if qty < 1.0:
            return None
        r = self.fam.desk.place_resting(slug, "SELL", want, qty,
                                        net_position=pos, initiator="owner",
                                        intent=SELL_LONG)
        if not (r.ok and r.order_id):
            self._log(event="ask_refused", market=slug, note=r.note[:120])
            return None
        self.fam.orders[r.order_id] = FamilyOrder(
            id=r.order_id, market=slug, side="SELL", price=(r.price or want),
            qty=qty, intent=r.intent, placed_ts=now, purpose="bond",
            why="bond: the resting ask earns while it waits")
        self.moved_at[slug] = now
        self._log(event="ask_rested", market=slug, price=(r.price or want),
                  qty=qty)
        return {"market": slug, "side": "SELL", "price": (r.price or want),
                "qty": qty}

    def _committed(self) -> float:
        return sum(o.qty * o.price for o in list(self.fam.orders.values())
                   if o.purpose == "bond" and o.side == "BUY")

    def _reinvest(self, now: float, positions: dict) -> dict | None:
        """Proceeds go into the cheapest approved market whose bid side
        pays: one post-only bid joining the touch, sized to the money."""
        spend = self.cash - self._committed()
        if spend < REINVEST_MIN_USD:
            return None
        best = None
        for slug in self.approved:
            if self._orders(slug, "BUY"):
                continue
            book = self.fam.cache.fresh(slug, 120.0, now)
            if book is None or not book.asks:
                continue
            ask = book.asks[0][0]
            if ask > PRICE_CAP:
                continue
            prog = self.fam.terms.get(slug)
            if prog is None or not prog.is_live():
                continue
            probe = self._probe(slug, book, prog, "BUY")
            if probe is None or not probe.qualifies or probe.est_day <= 0:
                continue
            if best is None or ask < best[0]:
                best = (ask, slug, book, probe)
        if best is None:
            return None
        ask, slug, book, probe = best
        tick = book.tick or 0.01
        px = book.bids[0][0] if book.bids else self._snap(ask - tick, tick)
        if px >= ask - 1e-9:
            px = self._snap(ask - tick, tick)
        qty = float(math.floor(spend / px))
        if qty < 1.0:
            return None
        pos = float((positions.get(slug) or (0.0, 0.0))[0])
        r = self.fam.desk.place_resting(slug, "BUY", px, qty, net_position=pos,
                                        initiator="owner", intent=BUY_LONG)
        if not (r.ok and r.order_id):
            self._log(event="bid_refused", market=slug, note=r.note[:120])
            return None
        self.fam.orders[r.order_id] = FamilyOrder(
            id=r.order_id, market=slug, side="BUY", price=(r.price or px),
            qty=qty, intent=r.intent, placed_ts=now, purpose="bond",
            why="bond: reinvesting sale proceeds — the bid earns while it waits")
        self.cash = round(self.cash - qty * (r.price or px), 4)
        self._log(event="bid_rested", market=slug, price=(r.price or px),
                  qty=qty, cash=round(self.cash, 2))
        return {"market": slug, "side": "BUY", "price": (r.price or px),
                "qty": qty}

    def _probe(self, slug: str, book, prog, side: str, qty: float | None = None):
        from . import survey as sv
        pool = self.fam._side_pool(slug, prog)
        if pool is None:
            return None
        try:
            return sv.probe_side(book, prog, side, pool,
                                 **({"qty": qty} if qty else {}))
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ the page

    def view(self, now: float, positions: dict | None = None) -> dict:
        rows = []
        for slug, meta in self.approved.items():
            book = self.fam.cache.any_age(slug)
            bid = book.bids[0][0] if book is not None and book.bids else None
            ask = book.asks[0][0] if book is not None and book.asks else None
            ask_size = book.asks[0][1] if book is not None and book.asks else 0.0
            inv = self.fam.inventory.get(slug) or {}
            qty = float(inv.get("qty") or 0.0)
            days = slug_days_out(slug, now)
            ytr = ((1.0 - ask) / ask) if ask else None
            ann = (ytr * 365.0 / max(days, 1)) if (ytr is not None and days) else None
            prog = self.fam.terms.get(slug)
            sell = None
            if book is not None and prog is not None and prog.is_live():
                p = self._probe(slug, book, prog, "SELL", qty=max(qty, 1.0))
                if p is not None:
                    sell = {"est_day": round(p.est_day, 4),
                            "share": round(p.share, 4),
                            "qualifies": bool(p.qualifies), "note": p.note}
            asks = self._orders(slug, "SELL")
            bids = self._orders(slug, "BUY")
            rows.append({
                "market": slug, "odds": meta.get("odds"),
                "flag": (meta.get("odds") is not None
                         and meta["odds"] < BOND_ODDS),
                "bid": bid, "ask": ask, "ask_size": round(ask_size, 1),
                "days": days, "yield": (round(ytr, 4) if ytr is not None else None),
                "annual": (round(ann, 4) if ann is not None else None),
                "qty": round(qty, 2), "cost_px": round(self._cost_px(slug), 4),
                "sell": sell,
                "ask_order": ([{"price": o.price, "qty": o.qty,
                                "est": round(o.live_est or 0.0, 4)}
                               for o in asks] or None),
                "bid_order": ([{"price": o.price, "qty": o.qty}
                               for o in bids] or None),
                "stale": (book is None or now - book.fetched_at > 600.0),
            })
        rows.sort(key=lambda r: (r["ask"] is None, r["ask"] or 1.0))
        proposed = [{"market": s, "odds": m.get("odds"),
                     "since": m.get("since")}
                    for s, m in sorted(self.proposed.items(),
                                       key=lambda kv: -(kv[1].get("odds") or 0))]
        held = sum(r["qty"] * (r["cost_px"] or 0) for r in rows)
        return {"rows": rows, "proposed": proposed,
                "ignored": sorted(self.ignored),
                "cash": round(self.cash, 2),
                "committed": round(self._committed(), 2),
                "held_cost": round(held, 2),
                "odds_bar": BOND_ODDS, "price_cap": PRICE_CAP,
                "last_scan": self.last_scan,
                "log": self.log[-12:]}

    # ------------------------------------------------------------ persistence

    def to_dict(self) -> dict:
        return {"approved": self.approved, "proposed": self.proposed,
                "ignored": self.ignored, "cash": round(self.cash, 4),
                "pos_seen": self.pos_seen, "moved_at": self.moved_at,
                "ask_seen": getattr(self, "_ask_seen", {}),
                "last_ask_px": getattr(self, "_last_ask_px", {}),
                "log": self.log[-LOG_KEEP:]}

    def restore(self, d: dict) -> None:
        self.approved = {str(k): dict(v) for k, v in (d.get("approved") or {}).items()}
        self.proposed = {str(k): dict(v) for k, v in (d.get("proposed") or {}).items()}
        self.ignored = {str(k): float(v) for k, v in (d.get("ignored") or {}).items()}
        self.cash = float(d.get("cash") or 0.0)
        self.pos_seen = {str(k): float(v) for k, v in (d.get("pos_seen") or {}).items()}
        self.moved_at = {str(k): float(v) for k, v in (d.get("moved_at") or {}).items()}
        self._ask_seen = {str(k): float(v) for k, v in (d.get("ask_seen") or {}).items()}
        self._last_ask_px = {str(k): float(v) for k, v in (d.get("last_ask_px") or {}).items()}
        self.log = list(d.get("log") or [])
        self.fam.freeze_dyn = self._frozen()
