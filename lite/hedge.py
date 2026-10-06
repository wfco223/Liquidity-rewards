"""What a fill does to his risk, with mutually exclusive outcomes netted.

Owner, 2026-10-06: "Can you add a filter for markets where I would be
lowering my risk by getting an order filled? That is in the mutually
exclusive markets where there is a possibility of collateral return" ...
"And include the amount that I would be getting back if my order is filled
so I can know what size to place".

The money an event holds is its worst case: every way it can resolve is
swept, and what is held there loses most in one of them. A fill that
improves that worst case gives money back. The arithmetic per market:

* A position is a stake (the money the exchange's feed says it ties up,
  cost.value, a price a share) and a payout: a long pays a share if the
  market resolves Yes, a short (held as No) a share if it resolves No.
* A scenario's loss is the stakes less what pays in it; the event's money
  is the largest of them, never under zero.
* A fill closes what he holds first (an ask sells his Yes, a bid buys back
  his No), which books what it realizes, then opens the rest.
* What it gives back = the event's money before - after + what the close
  realizes. A negative figure is money the fill ties up.

Netted only where one outcome wins: a seat count (the count swept, 2.0's
rule), a race's winner (parties, candidates, combos, margin brackets, the
closest race), politics only. Nested ladders (dates, "over N", a "wins"
beside its own brackets) can resolve Yes together and are priced market by
market, as is any event not known to be exclusive. Under "one wins",
"none of them" stays in (an independent, an outcome not listed), so the
figure never counts money a full set might return and the exchange might
not. Held positions count; resting orders do not (nothing obliges them to
fill), and each order is figured alone against what is held now.

The exchange's own margin rule is not published. On 2026-10-05 its margin
was $855.11 against $798.92 netted this way and $1,259.92 each alone (54
positions): it nets, not exactly like this. The page shows the three side
by side so the gap can be watched.
"""

from __future__ import annotations

import math
import re

from v3 import risk
from v3.tiercent import group_kind

QTY_CAP = 20000.0           # v3.orders.QTY_MAX: the most one order may be
EPS = 1e-9

# the products whose event is "who wins" (one outcome resolves Yes)
WINNER_PRODUCTS = {"paccc", "cpoc", "vmc", "cmovcuss", "cmovcusg"}
_DATE = re.compile(r"(^|-)(\d{4}-)?\d{2}-\d{2}($|-)")


def product(slug: str) -> str:
    return slug.split("-", 1)[0]


def tokens(slugs) -> dict[str, str]:
    """Each market's outcome: its slug past what every market of the event
    shares (whole '-' parts), so a bracket like "d0-10" stays whole."""
    slugs = list(dict.fromkeys(slugs))
    if len(slugs) < 2:
        return {s: risk.rung_token(s) for s in slugs}
    parts = [s.split("-") for s in slugs]
    n = 0
    for col in zip(*parts):
        if len(set(col)) != 1:
            break
        n += 1
    n = min(n, min(len(p) for p in parts) - 1)
    return {s: "-".join(p[n:]) for s, p in zip(slugs, parts)}


def event_kind(slugs, politics: bool = True) -> tuple[str | None, dict[str, str]]:
    """("numeric" | "categorical" | None, outcome of each market)."""
    tok = tokens(slugs)
    if len(tok) < 2 or not politics:
        return None, tok
    toks = list(tok.values())
    if any(_DATE.search(t) for t in toks):
        return None, tok
    kind = group_kind(toks)
    if kind == "numeric":
        return kind, tok
    prods = {product(s) for s in tok}
    if kind == "categorical" and len(prods) == 1:
        p = next(iter(prods))
        if p.endswith("wc") or p in WINNER_PRODUCTS:
            return kind, tok
    return None, tok


class Group:
    """One event's markets, its scenarios and what is held there."""

    def __init__(self, kind: str | None, tok_of: dict[str, str],
                 held: dict[str, tuple[float, float]]):
        self.kind = kind
        self.markets = list(tok_of)
        if kind == "numeric":
            marks: set[int] = set()
            for t in tok_of.values():
                n = int(t[3:]) if t.startswith(("gte", "lte")) else int(t)
                marks.update((n - 1, n, n + 1))
            ks = sorted(marks)
            self.wins = {m: [risk.rung_pays(tok_of[m], k) for k in ks] for m in self.markets}
        elif kind == "categorical":
            outs = self.markets + [None]          # "none of them" stays in
            self.wins = {m: [w == m for w in outs] for m in self.markets}
        else:                                     # one market alone
            self.wins = {m: [True, False] for m in self.markets}
        self.n = len(next(iter(self.wins.values())))
        self.held = {m: held[m] for m in self.markets if m in held}
        self.base = [0.0] * self.n
        for m, (q, c) in self.held.items():
            w = self.wins[m]
            for i in range(self.n):
                pays = w[i] if q > 0 else not w[i]
                self.base[i] += abs(q) * c - (abs(q) if pays else 0.0)
        self.money = max(0.0, max(self.base))

    # -- one fill ----------------------------------------------------------------

    def _legs(self, m: str, side: str, px: float):
        """(shares the fill closes first, per-share change in each scenario's
        loss while closing, realized a share closed, the change while opening)."""
        q, c = self.held.get(m, (0.0, 0.0))
        w = self.wins[m]
        if side == "BUY":
            opn = [px - (1.0 if x else 0.0) for x in w]
            if q < 0:          # buys back the No he holds
                return -q, [-c + (0.0 if x else 1.0) for x in w], (1.0 - px) - c, opn
        else:
            opn = [(1.0 if x else 0.0) - px for x in w]
            if q > 0:          # sells the Yes he holds
                return q, [-c + (1.0 if x else 0.0) for x in w], px - c, opn
        return 0.0, None, 0.0, opn

    def after(self, m: str, side: str, px: float, qty: float) -> "Group":
        """The same event once a fill of qty at px has happened: what a
        new order gives back after his resting ones ahead of it fill."""
        held, dc, r, do = self._legs(m, side, px)
        qc = min(qty, held)
        qo = max(qty - qc, 0.0)
        g = object.__new__(Group)
        g.kind, g.markets, g.wins, g.n = self.kind, self.markets, self.wins, self.n
        g.base = [self.base[i] + qc * (dc[i] if dc else 0.0) + qo * do[i] for i in range(self.n)]
        g.money = max(0.0, max(g.base))
        g.held = dict(self.held)
        q, c = self.held.get(m, (0.0, 0.0))
        sign = 1.0 if side == "BUY" else -1.0
        per = px if side == "BUY" else 1.0 - px
        left = q + sign * qc
        if dc is None:                # opens, or adds to the same side: stakes average
            n = abs(q) + qty
            if n > EPS:
                g.held[m] = (sign * n, (abs(q) * c + qty * per) / n)
        elif qo > EPS:                # through flat: the rest opens the other side
            g.held[m] = (sign * qo, per)
        elif abs(left) > EPS:
            g.held[m] = (left, c)
        else:
            g.held.pop(m, None)
        return g

    def frees(self, m: str, side: str, px: float, qty: float) -> float:
        """The money a fill of qty at px gives back (negative: ties up)."""
        held, dc, r, do = self._legs(m, side, px)
        qc = min(qty, held)
        qo = max(qty - qc, 0.0)
        worst = max(self.base[i] + qc * (dc[i] if dc else 0.0) + qo * do[i]
                    for i in range(self.n))
        return self.money - max(0.0, worst) + qc * r

    def best(self, m: str, side: str, px: float, cap: float = QTY_CAP) -> dict:
        """The most a fill at px gives back and the size that does it, and
        what each share gives back at the start ("ps"). Within the close,
        and within the open, the figure rises then falls (the worst case is
        the largest of straight lines), so each part's peak is found by
        halving on its slope; the better of the two stands."""
        held, dc, r, do = self._legs(m, side, px)
        segs = []
        hc = min(held, cap) if dc is not None else 0.0
        if hc > EPS:
            segs.append((0.0, hc, list(self.base), dc, r))
        if cap > hc + EPS:
            segs.append((hc, cap, [self.base[i] + hc * (dc[i] if dc else 0.0)
                                   for i in range(self.n)], do, 0.0))
        best_q, best_v, ps = 0.0, 0.0, None
        for lo, hi, a, slope, rr in segs:
            def deriv(x, lo=lo, a=a, slope=slope, rr=rr):
                vals = [a[i] + slope[i] * (x - lo) for i in range(self.n)]
                top = max(vals)
                if top < -1e-7:
                    return rr                 # under the floor: only the close's cash moves
                sm = max(slope[i] for i in range(self.n) if vals[i] >= top - 1e-7)
                if top <= 1e-7:
                    sm = max(sm, 0.0)
                return rr - sm
            d0 = deriv(lo)
            if ps is None:
                ps = d0
            if d0 <= EPS:
                continue
            if deriv(hi - 1e-6) > EPS:
                x = hi
            else:
                a_, b_ = lo, hi
                for _ in range(60):
                    mid = (a_ + b_) / 2
                    if deriv(mid) > EPS:
                        a_ = mid
                    else:
                        b_ = mid
                x = b_
            # sizes go in hundredths: the peak's neighbours, the smaller on a tie
            for q in sorted({math.floor(x * 100 + 1e-6) / 100, round(x, 2),
                             math.ceil(x * 100 - 1e-6) / 100}):
                if q < 0.01 or q < lo - EPS or q > hi + EPS:
                    continue
                v = self.frees(m, side, px, q)
                if v > best_v + 1e-9:
                    best_q, best_v = q, v
        return {"px": px, "ps": round(ps or 0.0, 4), "q": best_q, "frees": round(best_v, 2)}


class RiskBook:
    """Every event he holds in, built from the positions feed."""

    def __init__(self, held: dict[str, tuple[float, float]], event_of, members,
                 politics=lambda s: True):
        self.held = held
        self._event_of = event_of
        self._members = members
        self._politics = politics
        self._groups: dict[str, Group] = {}

    def group(self, slug: str) -> Group:
        """The event's netted group, or the market alone."""
        ev = self._event_of(slug) or ""
        g = self._groups.get(ev) if ev else None
        if g is None and ev and ev not in self._groups:
            mem = list(dict.fromkeys(self._members.get(ev) or ()))
            if slug not in mem:
                mem.append(slug)
            kind, tok = (event_kind(mem, all(self._politics(s) for s in mem))
                         if len(mem) > 1 else (None, {}))
            g = Group(kind, tok, self.held) if kind else None
            self._groups[ev] = g
        if g is not None and slug in g.wins:
            return g
        lone = self._groups.get("\0" + slug)
        if lone is None:
            lone = self._groups["\0" + slug] = Group(None, {slug: ""}, self.held)
        return lone

    def netted(self, slug: str) -> bool:
        return self.group(slug).kind is not None

    def margin(self) -> dict:
        """What everything held ties up: each market alone, and netted."""
        plain = sum(abs(q) * c for q, c in self.held.values())
        seen, net = set(), 0.0
        for s in self.held:
            g = self.group(s)
            if id(g) in seen:
                continue
            seen.add(id(g))
            net += g.money
        return {"plain": round(plain, 2), "netted": round(net, 2)}


def stake_a_share(q: float, p: dict, mid: float | None) -> tuple[float, bool]:
    """What one share of a position ties up, a price between 0 and 1: the
    feed's cost.value over the shares, else its avgPx, else the midpoint
    (a long's price, a short's one less it) — marked estimated."""
    def num(x):
        if isinstance(x, dict):
            x = x.get("value")
        try:
            return float(x)
        except (TypeError, ValueError):
            return float("nan")
    if abs(q) > EPS:
        c = num(p.get("cost")) / abs(q)
        if 0.0 < c <= 1.0:
            return c, False
    a = num(p.get("avgPx"))
    if 0.0 < a <= 1.0:
        return a, False
    if mid is not None and 0.0 < mid < 1.0:
        return (mid if q > 0 else 1.0 - mid), True
    return 1.0 if q < 0 else 0.5, True
