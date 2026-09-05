"""3.0 entry point.

    python -m v3.main          # run the loop
    python -m v3.main --once   # one cycle against the live exchange, print, exit

One process, one loop, a list of families. Politics is the first and the
priority; adding a family is one config function and one line in
FAMILIES — exactly how the owner expanded 2.0.

Placing needs BOTH the master switch and the family's own switch ON.
Everything starts OFF; until the owner arms a family it only discovers,
scores, and shows what it would do. Master OFF stops every family with
one tap.

Env: POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY (required),
GITHUB_TOKEN (state survives redeploys; optional),
NTFY_TOPIC / NTFY_SERVER (alerts; optional),
V3_STATE_PATH (default ./v3_state.json), V3_PORT (default 8092).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

from . import basketball, football, gameday, politics
from .alerts import Alerts
from .api import ApiError, Client, GATEWAY
from .books import BookCache
from .family import Family
from .floor import Floor
from .names import Names
from .orders import OrderDesk, PlaceHealth
from .estimator import Estimator
from .silver import SilverFairs
from .state import StateStore
from .switch import MasterSwitch
from .ws import Stream

try:
    from zoneinfo import ZoneInfo
    ET_STATUS = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001
    import datetime as _dtz
    ET_STATUS = _dtz.timezone(_dtz.timedelta(hours=-4), "ET")

POLL_S = 60.0
NURSE_TICK_S = 5.0
ERROR_BACKOFF_CAP_S = 600.0
FLATTEN_CANCELS_PER_CYCLE = 45


def flatten_active() -> bool:
    """Owner, 2026-08-20 evening: "cancel all of my open orders except for
    the ones that are exiting a position that I'm already in... I need to
    have no risk of spending any money." And once flat: "increase the
    budget to 100 and follow the same strategy that was already existing
    in V1 and V2 for politics markets, looking at the orders that were the
    most successful."

    While the marker file ships with the build (v3/FLATTEN), the monitor
    runs the flatten: phase one cancels every opening order on the account
    and keeps every exit; once a pass finds nothing left to cancel, phase
    two lets the armed families rebuild under their (now $100 politics)
    ceilings, history-guided, while the pass keeps guarding against any
    opening order 3.0 does not own. Removing the marker (a redeploy) ends
    the mode entirely. V3_FLATTEN=0/1 overrides for tests."""
    env = os.environ.get("V3_FLATTEN")
    if env is not None:
        return env == "1"
    return os.path.exists(os.path.join(os.path.dirname(__file__), "FLATTEN"))


def is_exit_order(order: dict, positions: dict) -> bool:
    """An order whose FILL reduces a position we already hold: an ask
    while long, or a bid while short. Book side from the INTENT (the
    exchange's side field is not trustworthy for shorts — 1.0's lesson).
    Same classification the owner approved in the dead-programs sweep."""
    from .intents import REST_SIDE
    side = REST_SIDE.get(str(order.get("intent") or ""))
    if side is None:
        return False
    net = (positions.get(order.get("market")) or (0.0, 0.0))[0]
    return (side == "SELL" and net > 0.005) or (side == "BUY" and net < -0.005)

# name -> (config fn, discover fn). Adding a family = adding a line.
# Politics first — it gets the capital, the book budget, and the page.
FAMILIES = {
    "politics": (politics.config, politics.discover),
    "cfb": (football.cfb, football.cfb_discover),
    "nfl": (football.nfl, football.nfl_discover),
    "nba": (basketball.nba, basketball.nba_discover),
    # the game-day experiment (owner, 2026-09-05): a share a side on a
    # few markets of tomorrow's two biggest college games, off at kickoff
    "gameday": (gameday.config, gameday.discover),
}


def load_history() -> dict[str, float]:
    """Average $/day each market has ACTUALLY paid us, from the committed
    ground truth (data/rewards.csv on main). This is the "most successful
    orders" record the rebuild replicates. Empty on any failure — history
    guides, it never blocks."""
    tok = os.environ.get("GITHUB_TOKEN", "")
    if not tok:
        return {}, {}, {}
    import csv
    import io
    from collections import defaultdict

    import requests
    repo = os.environ.get("GITHUB_REPOSITORY", "wfco223/Liquidity-rewards")
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/contents/data/rewards.csv",
            headers={"Authorization": f"Bearer {tok}",
                     "Accept": "application/vnd.github.raw+json"},
            timeout=30)
        if r.status_code >= 400:
            return {}, {}, {}
        paid: dict = defaultdict(float)
        days: dict = defaultdict(set)
        day_totals: dict = defaultdict(float)
        r_paid: dict = defaultdict(float)
        r_days: dict = defaultdict(set)
        import datetime as _dt
        cutoff = (_dt.date.today()
                  - _dt.timedelta(days=7)).isoformat()
        for row in csv.DictReader(io.StringIO(r.text)):
            v = float(row.get("reward_usd") or 0)
            if v <= 0:
                continue
            mkt = row.get("market") or ""
            paid[mkt] += v
            days[mkt].add(row.get("date"))
            day_totals[row.get("date") or "?"] += v
            if (row.get("date") or "") >= cutoff:
                r_paid[mkt] += v
                r_days[mkt].add(row.get("date"))
        recent = {mkt: (round(r_paid[mkt] / max(len(r_days[mkt]), 1), 4),
                        len(r_days[mkt])) for mkt in r_paid}
        return ({mkt: round(paid[mkt] / max(len(days[mkt]), 1), 4)
                 for mkt in paid},
                {d: round(v, 2) for d, v in day_totals.items()},
                recent)
    except Exception:  # noqa: BLE001
        return {}, {}, {}


FILLS_CSV_HEADER_V1 = ("ts,family,market,side,qty,px,purpose,est_day,"
                       "rested_h,fair,band_lo,band_hi,conf,touch_bid,"
                       "touch_ask,conc,pos_after,why\n")
FILLS_CSV_HEADER = ("ts,family,market,side,qty,px,purpose,est_day,"
                    "rested_h,fair,band_lo,band_hi,conf,touch_bid,"
                    "touch_ask,conc,pos_after,why,oid\n")


def fills_csv_append(existing: str | None, rows: list) -> tuple[str, int]:
    """Append-only fills archive (owner, 2026-08-22: 'bound it much
    higher — write to GitHub'). `rows` are (ts, family, journal-row)
    tuples; returns (new file text, rows added). Fills from the same
    cycle share a timestamp, so dedup is by the whole line, not ts."""
    def s(x):
        if x is None:
            return ""
        return f"{x:g}" if isinstance(x, (int, float)) else str(x)
    text = existing if existing else FILLS_CSV_HEADER
    if text.startswith(FILLS_CSV_HEADER_V1):
        # oid became the final column on 2026-08-23; upgrade the header
        # in place so the file keeps one shape. Older rows simply lack
        # the trailing field.
        text = FILLS_CSV_HEADER + text[len(FILLS_CSV_HEADER_V1):]
    tail = set(text.rstrip().split("\n")[-400:])
    last = 0.0
    body = text.rstrip().rsplit("\n", 1)[-1]
    try:
        last = float(body.split(",", 1)[0])
    except Exception:
        last = 0.0
    added = 0
    for ts, fam, r in sorted(rows, key=lambda x: x[0]):
        if ts < last - 0.05:
            continue
        band = r.get("band") or [None, None]
        why = str(r.get("why") or "").replace(",", ";").replace("\n", " ")[:80]
        line = ",".join([
            f"{ts:.1f}", fam, s(r.get("market")), s(r.get("side")),
            s(r.get("qty")), s(r.get("px")), s(r.get("purpose")),
            s(r.get("est_day")), s(r.get("rested_h")), s(r.get("fair")),
            s(band[0]), s(band[1]), s(r.get("conf")),
            s(r.get("touch_bid")), s(r.get("touch_ask")),
            s(r.get("conc")), s(r.get("pos_after")), why,
            s(r.get("oid"))])
        if line in tail:
            continue
        text += line + "\n"
        tail.add(line)
        added += 1
    return text, added


TRADES_CSV_HEADER = ("ts,iso,type,market,side,intent,price,shares,"
                     "order_id,role,realized_pnl,placed_iso,rested_h,"
                     "commission,maker_bps,manual,order_state,"
                     "cancel_reason,reject_reason,amount_usd,detail\n")


def _first_num(d: dict, keys) -> float | None:
    """The first of `keys` present in `d` that reads as a number.

    Written for the settlement and account-transfer rows, where the
    feed's own name for the money is not yet known to us — try the
    plausible ones in order rather than guessing one and recording
    a blank forever."""
    for k in keys:
        if k in d:
            v = _act_num(d.get(k))
            if v is not None:
                return v
        inner = d.get(k)
        if isinstance(inner, dict):        # {"amount": {"value": "3.00"}}
            v = _act_num(inner.get("value") or inner.get("amount"))
            if v is not None:
                return v
    return None


def _shape_of(d: dict, limit: int = 10) -> str:
    """The payload's field names, for a shape we could not read a
    number out of. The owner's standing rule (2026-08-23): when a
    number cannot be checked, go find the source — so record what
    the source actually offered instead of a silent blank."""
    if not isinstance(d, dict):
        return ""
    return "keys=" + "|".join(sorted(d.keys())[:limit])


def _act_num(x):
    """Protobuf money/qty shapes: plain, {'value': '1.5'}, or string."""
    if x is None:
        return None
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _iso_to_ts(s: str) -> float:
    import datetime as _d
    s = (s or "").strip()
    if not s:
        return 0.0
    try:
        return _d.datetime.fromisoformat(
            s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_activities(rows: list, known_ids=None) -> list[dict]:
    """Activity rows -> OUR executions, one per row. `known_ids` are
    order ids we know to be ours (resting, placed, journaled, bond
    takes): when a trade names one, THAT execution is ours and the
    other is the counterparty's — the Tennessee case (2026-09-03): our
    3c take filled against two strangers whose orders carried real
    intents too, the passive side was read first, and their orders
    were journaled as the owner's own hand sales.

    The feed returns BOTH sides of every trade: ours and the
    counterparty's. Treating that as a self-cross once dropped 1,623 of
    1,623 real fills (1.0, the hard way). OUR side is the one whose
    order carries a real intent — the API redacts the counterparty's to
    ORDER_INTENT_UNDEFINED."""
    out = []
    for a in rows or []:
        atype = str(a.get("type") or a.get("activityType") or "")
        t = a.get("trade") or {}
        pr = a.get("positionResolution") or {}
        if t:
            ours = None
            role = ""
            known = known_ids or ()
            for key, name in (("passiveExecution", "passive"),
                              ("aggressorExecution", "aggressor")):
                ex = t.get(key) or {}
                o = ex.get("order") or {}
                if o.get("id") and str(o.get("id")) in known:
                    ours, role = ex, name
                    break
            if ours is None:
                for key, name in (("passiveExecution", "passive"),
                                  ("aggressorExecution", "aggressor")):
                    ex = t.get(key) or {}
                    o = ex.get("order") or {}
                    it = str(o.get("intent") or "")
                    if o.get("id") and it and not it.endswith("UNDEFINED"):
                        ours, role = ex, name
                        break
            if ours is None:
                continue                      # entirely the other side
            o = ours.get("order") or {}
            shares = _act_num(ours.get("lastShares"))
            if not shares or shares <= 0:
                continue                      # a placement, not a fill
            px = (_act_num(ours.get("lastPx"))
                  or _act_num(o.get("avgPx")) or _act_num(o.get("price")))
            intent = str(o.get("intent") or "")
            # Two of the four intents rest on the OPPOSITE side from
            # their name (BUY_SHORT is an ASK, SELL_SHORT is a BID) —
            # v3/intents.py is the single place that mapping lives.
            # Reading the name naively inverted the side on every
            # short and made the journal look like it had missed
            # fills it had actually recorded.
            from .intents import REST_SIDE
            side = REST_SIDE.get(intent, "")
            ts_s = str(ours.get("transactTime") or t.get("updateTime")
                       or t.get("createTime") or "")
            # the exchange carries far more than we were reading (the
            # 2026-08-23 shape probe): when the order was PLACED, why
            # it was cancelled, and the commissions actually charged.
            placed_s = str(o.get("createTime") or o.get("insertTime") or "")
            placed = _iso_to_ts(placed_s)
            ex_ts = _iso_to_ts(ts_s)
            out.append({
                "ts": ex_ts, "iso": ts_s,
                "type": atype or "TRADE",
                "market": str(t.get("marketSlug") or ""),
                "side": side, "intent": intent,
                "price": px, "shares": shares,
                "order_id": str(o.get("id") or ""), "role": role,
                "realized_pnl": _act_num(t.get("realizedPnl")),
                "placed_iso": placed_s,
                "placed_ts": placed or None,
                # the exact resting period, from the exchange itself —
                # no ledger needed, and it covers history too
                "rested_h": (round((ex_ts - placed) / 3600.0, 3)
                             if placed and ex_ts > placed else None),
                "commission": _act_num(
                    ours.get("commissionNotionalCollected")),
                "maker_bps": _act_num(o.get("makerCommissionsBasisPoints")),
                "manual": (1 if o.get("manualOrderIndicator") else 0),
                "order_state": str(o.get("state") or ""),
                "cancel_reason": str(ours.get("unsolicitedCancelReason")
                                     or ""),
                "reject_reason": str(ours.get("orderRejectReason") or "")})
        elif pr:
            ts_s = str(pr.get("updateTime") or pr.get("createTime") or "")
            after = pr.get("afterPosition") or {}
            before = pr.get("beforePosition") or {}
            # a settlement is a PAYMENT — the shares we held stop being
            # shares and become cash. We were recording that it happened
            # and not how much (2026-08-24, owner: "tell me what these
            # other payments I'm getting are"). Take the first amount
            # field the feed actually carries.
            amt = _first_num(pr, ("payout", "payoutNotional", "notional",
                                  "settlementNotional", "amount",
                                  "cashAmount", "proceeds"))
            held = _act_num(before.get("quantity"))
            out.append({
                "ts": _iso_to_ts(ts_s), "iso": ts_s,
                "type": atype or "POSITION_RESOLUTION",
                "market": str(pr.get("marketSlug") or ""),
                "side": "", "intent": "",
                "price": _first_num(pr, ("settlementPrice", "price",
                                         "resolutionPrice")),
                "shares": _act_num(after.get("quantity")),
                "order_id": "", "role": "",
                "realized_pnl": _act_num(pr.get("realizedPnl")),
                "amount_usd": amt,
                "detail": _shape_of(pr) if amt is None else
                          (f"held={held:g}" if held else "")})
        else:
            # Deposits, withdrawals, transfers and any shape we have
            # not seen. These carry their fields in a sub-object named
            # after the activity, not at the top level, so reading
            # updateTime/createTime off the root produced rows with no
            # date and no amount at all — four of them, all blank.
            # Find the payload, take its time and its amount, and if
            # the amount is not where we expect, write down the shape
            # so the next fetch answers the question instead of
            # repeating it.
            body = None
            for k, v in a.items():
                if isinstance(v, dict) and k not in ("trade",
                                                     "positionResolution"):
                    body = v
                    break
            src = body if isinstance(body, dict) else a
            ts_s = str(src.get("updateTime") or src.get("createTime")
                       or a.get("updateTime") or a.get("createTime") or "")
            amt = _first_num(src, ("amount", "notional", "amountUsd",
                                   "cashAmount", "value", "quantity",
                                   "usdAmount", "netAmount"))
            out.append({"ts": _iso_to_ts(ts_s), "iso": ts_s,
                        "type": atype or "UNKNOWN", "market": "",
                        "side": "", "intent": "", "price": None,
                        "shares": None, "order_id": "", "role": "",
                        "realized_pnl": None,
                        "amount_usd": amt,
                        "detail": _shape_of(src) if amt is None else ""})
    return out


def trades_csv_append(existing: str | None, rows: list) -> tuple[str, int]:
    """Append-only transaction record. Deduplicated on the whole line,
    so re-fetching overlapping pages adds nothing."""
    def s(x):
        if x is None:
            return ""
        return f"{x:g}" if isinstance(x, (int, float)) else str(x)
    text = existing if existing else TRADES_CSV_HEADER
    seen = set(text.rstrip().split("\n"))
    added = 0
    for r in sorted(rows, key=lambda x: x.get("ts") or 0.0):
        line = ",".join([
            f"{r.get('ts') or 0:.1f}", s(r.get("iso")), s(r.get("type")),
            s(r.get("market")), s(r.get("side")), s(r.get("intent")),
            s(r.get("price")), s(r.get("shares")), s(r.get("order_id")),
            s(r.get("role")), s(r.get("realized_pnl")),
            s(r.get("placed_iso")), s(r.get("rested_h")),
            s(r.get("commission")), s(r.get("maker_bps")),
            s(r.get("manual")), s(r.get("order_state")),
            s(r.get("cancel_reason")), s(r.get("reject_reason")),
            s(r.get("amount_usd")), s(r.get("detail"))])
        if line in seen:
            continue
        text += line + "\n"
        seen.add(line)
        added += 1
    return text, added


ESTIMATES_CSV_HEADER = ("day,family,est_usd,unmeasured_min,recorded_at,"
                        "paid_usd,paid_at,error_pct\n")


MARKET_EST_CSV_HEADER = ("day,market,family,est_day_usd,orders,"
                         "recorded_at,paid_usd,paid_at,error_pct,"
                         "share,pool_day,live_h,realized_share,levels\n")


def market_est_append(existing: str | None, today: str, rows: list,
                      paid_by_market_day: dict, now_iso: str,
                      keep_rows: int = 6000) -> tuple[str, int]:
    """The per-MARKET estimate ledger.

    The family ledger (estimates_csv_append) records one number a day
    per family, which is enough to see that politics is wrong and not
    enough to see WHERE. Across Aug 20-22 politics estimated $255-366
    a day and paid $76-101, while college football estimated $47.66
    and paid $54.33 — but with only family totals written down, no
    market and no race can be graded against its own prediction, so
    the cause stays a guess. This file makes it arithmetic.

    Same discipline as the family ledger: a past day's estimate is
    FROZEN the first time it is written. Only paid_usd, paid_at and
    error_pct fill in afterwards.

    `rows` are (day, market, family, est_day, orders, share, pool_day,
    live_h) tuples. The last three are the estimator's own time-weighted
    measurement of our share and the pool it competed for. Once the
    money lands, realized_share = paid / (pool_day * live_h / 24), and
    share vs realized_share is the estimator's bias measured directly,
    with no model in between (owner, 2026-08-24: the sampler runs 4,320
    times a day, so a persistent error is a bias, and averaging more
    samples cannot remove a bias).
    `paid_by_market_day` maps "day|market" -> paid, which is exactly
    the shape the monitor already keeps in self.rewards_seen.
    """
    text = existing if existing else MARKET_EST_CSV_HEADER
    kept: dict = {}
    order: list = []
    for line in text.strip().split("\n")[1:]:
        parts = line.split(",")
        if len(parts) < 9:
            continue
        parts += [""] * (14 - len(parts))      # rows written before the
        key = (parts[0], parts[1])             # share/depth columns existed
        if key not in kept:
            order.append(key)
        kept[key] = parts
    changed = 0
    for day, market, family, est, orders, share, pool_day, live_h, levels \
            in rows:
        key = (day, market)
        prior = kept.get(key)
        if prior and day != today:      # frozen: a prediction you can
            est_s = prior[3]            # revise after the fact is
            ord_s = prior[4]            # worth nothing
            rec_s = prior[5]
            share_s = prior[9] if len(prior) > 9 else ""
            pool_s = prior[10] if len(prior) > 10 else ""
            live_s = prior[11] if len(prior) > 11 else ""
            lv_s = prior[13] if len(prior) > 13 else ""
        else:
            est_s = f"{est:.4f}"
            ord_s = str(int(orders))
            rec_s = prior[5] if prior else now_iso
            share_s = f"{share:.6f}" if share else ""
            pool_s = f"{pool_day:.6f}" if pool_day else ""
            live_s = f"{live_h:.3f}" if live_h else ""
            lv_s = str(int(levels)) if levels else ""
        paid = paid_by_market_day.get(f"{day}|{market}")
        paid_s = f"{paid:.4f}" if paid is not None else ""
        paid_at = (prior[7] if prior and prior[6] else
                   (now_iso if paid is not None else ""))
        err = ""
        if paid is not None:
            try:
                e = float(est_s)
                if e > 0:
                    err = f"{(paid - e) / e * 100:+.1f}"
            except ValueError:
                pass
        realized = ""
        if paid is not None:
            try:
                offered = float(pool_s) * float(live_s) / 24.0
                if offered > 0:
                    realized = f"{paid / offered:.6f}"
            except (ValueError, ZeroDivisionError):
                pass
        row = [day, market, family, est_s, ord_s, rec_s, paid_s,
               paid_at, err, share_s, pool_s, live_s, realized, lv_s]
        if kept.get(key) != row:
            changed += 1
        if key not in kept:
            order.append(key)
        kept[key] = row
    # newest days first when trimming — the old ones are already graded
    order.sort(key=lambda k: k[0], reverse=True)
    order = order[:keep_rows]
    body = "\n".join(",".join(kept[k]) for k in order)
    return MARKET_EST_CSV_HEADER + body + "\n", changed


def estimates_csv_append(existing: str | None, today: str,
                         rows: list, paid_by_day: dict,
                         now_iso: str,
                         paid_by_fam: dict | None = None,
                         ) -> tuple[str, int]:
    """The estimate ledger (owner, 2026-08-23: "All the estimates
    should stay written down somewhere until the actual numbers come
    in").

    A past day's estimate is FROZEN the first time it is written — it
    is a prediction, and a prediction you can revise after the fact is
    worthless. Only the paid column fills in later. Today's row keeps
    updating because the day is still accruing.

    `rows` are (day, family, est_usd, unmeasured_min) tuples.
    `paid_by_day` maps day -> total paid for the whole account, and
    `paid_by_fam` maps (day, family) -> that family's own share.

    Grade a family against ITS OWN money (2026-08-24). Before this the
    whole account's total was written into every family row, so the
    politics estimate was measured against politics + football + NBA
    together, and nfl's $0.00 estimate was scored against the entire
    day. Both the paid column and error_pct were nonsense per family.
    The day total is still the fallback for a family the breakdown
    cannot classify, so a row never goes blank.
    """
    text = existing if existing else ESTIMATES_CSV_HEADER
    kept: dict = {}
    order: list = []
    for line in text.strip().split("\n")[1:]:
        parts = line.split(",")
        if len(parts) < 8:
            continue
        key = (parts[0], parts[1])
        if key not in kept:
            order.append(key)
        kept[key] = parts
    changed = 0
    for day, family, est, unmeas in rows:
        key = (day, family)
        prior = kept.get(key)
        if prior and day != today:
            est_s, unmeas_s, rec_s = prior[2], prior[3], prior[4]
        else:                       # today, or never recorded before
            est_s = f"{est:.2f}"
            unmeas_s = f"{unmeas:.1f}"
            rec_s = prior[4] if prior else now_iso
        paid = (paid_by_fam or {}).get((day, family))
        if paid is None and not paid_by_fam:
            paid = paid_by_day.get(day)
        paid_s = f"{paid:.2f}" if paid is not None else ""
        paid_at = (prior[6] if prior and prior[5] else
                   (now_iso if paid is not None else ""))
        err = ""
        if paid is not None:
            try:
                e = float(est_s)
                if e > 0:
                    err = f"{(paid - e) / e * 100:+.1f}"
            except ValueError:
                pass
        row = [day, family, est_s, unmeas_s, rec_s, paid_s, paid_at, err]
        if prior != row:
            changed += 1
        if key not in kept:
            order.append(key)
        kept[key] = row
    out = ESTIMATES_CSV_HEADER
    for key in sorted(order, key=lambda k: (k[0], k[1])):
        out += ",".join(kept[key]) + "\n"
    return out, changed


def card_is_open(card: dict) -> bool:
    """A lot is open only while the exchange still shows a position —
    a lot the pairing thinks is open on a FLAT market was closed by a
    correction or an untracked fill (the Florida card, 2026-08-22) and
    counts as closed."""
    if card.get("stray_close"):
        return False
    oq = (card.get("open_qty") if card.get("open_qty") is not None
          else card.get("qty", 0.0))
    if oq <= 0.005:
        return False
    if (card.get("pos_now") is not None
            and abs(card["pos_now"]) < 0.005):
        return False
    return True


def card_net(card: dict) -> float:
    """The card's bottom line, same math the page shows: realized plus
    rewards earned resting, plus (for open lots) the conservative mark
    and what the resting exit has earned."""
    earned = 0.0
    if card.get("est_day") and card.get("rested_h") is not None:
        earned = card["est_day"] * card["rested_h"] / 24.0
    is_open = card_is_open(card)
    net = (card.get("realized") or 0.0) + earned
    if is_open:
        oq = (card.get("open_qty") if card.get("open_qty") is not None
              else card.get("qty", 0.0))
        if card.get("side") == "BUY" and card.get("now_bid") is not None:
            net += (card["now_bid"] - card["px"]) * oq
        if card.get("side") == "SELL" and card.get("now_ask") is not None:
            net += (card["px"] - card["now_ask"]) * oq
        net += card.get("exit_earned") or 0.0
    return net


def card_visible(card: dict, now: float) -> bool:
    """Owner's retention (2026-08-22): closed cards show for 3 days
    after their last close; open cards show until they turn profitable
    (then the journal keeps tracking them silently)."""
    if card_is_open(card):
        return card_net(card) <= 0.005
    last = card.get("last_ts", card.get("ts", 0.0))
    return now - last <= 3 * 86400.0


def pair_fills(fills: list) -> list:
    """Match closes to entries, oldest lot first, per market: a buy pairs
    with the sells that unload it, a short sale with the buys that cover
    it (owner, 2026-08-21: "each should have a matching buy and sell or
    sell short and buy back"). Returns one card per entry lot carrying
    its closes and realized money, plus stray closes of stock the journal
    never saw bought."""
    out: list[dict] = []
    by_mkt: dict[str, list] = {}
    for r in sorted(fills, key=lambda x: x.get("ts", 0.0)):
        by_mkt.setdefault(r.get("market", "?"), []).append(r)
    for evs in by_mkt.values():
        longs: list[dict] = []
        shorts: list[dict] = []
        for r in evs:
            qty = float(r.get("qty") or 0.0)
            opp = shorts if r["side"] == "BUY" else longs
            while qty > 0.005 and opp:
                lot = opp[0]
                take = min(qty, lot["open_qty"])
                pl = ((lot["px"] - r["px"]) if r["side"] == "BUY"
                      else (r["px"] - lot["px"])) * take
                lot["closes"].append({
                    "ts": r["ts"], "px": r["px"], "qty": round(take, 2),
                    "pl": round(pl, 4), "kind": r.get("purpose") or ""})
                lot["realized"] = round(lot["realized"] + pl, 4)
                lot["open_qty"] = round(lot["open_qty"] - take, 2)
                lot["last_ts"] = r["ts"]
                if lot["open_qty"] <= 0.005:
                    opp.pop(0)
                qty = round(qty - take, 2)
            if qty > 0.005:
                lot = dict(r)
                lot["open_qty"] = qty
                lot["closes"] = []
                lot["realized"] = 0.0
                lot["last_ts"] = r["ts"]
                # the owner's own trade (from the exchange record) with
                # no lot to match: a closing intent, or a sliver under
                # a share, closed stock the journal never saw bought
                hand_close = (r.get("purpose") == "hand" and (
                    str(r.get("intent") or "") in (
                        "ORDER_INTENT_SELL_LONG", "ORDER_INTENT_BUY_SHORT")
                    or qty < 1.0))
                # a bond's earning order closes it: the YES ask, or the
                # cover bid of a NO bond (a short of YES)
                bond_close = (r.get("purpose") == "bond"
                              and str(r.get("intent") or "") in (
                                  "ORDER_INTENT_SELL_LONG",
                                  "ORDER_INTENT_SELL_SHORT"))
                if r.get("purpose") == "sell" or bond_close or hand_close:
                    # an exit with no purchase to match: it closed stock
                    # bought before the journal — not a new position
                    lot["stray_close"] = True
                    lot["open_qty"] = 0.0
                else:
                    (longs if r["side"] == "BUY" else shorts).append(lot)
                out.append(lot)
    return out


def build_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(Path(__file__).parent.glob("*.py")):
        h.update(p.read_bytes())
    return h.hexdigest()[:8]


class CacheRouter:
    """The stream writes here; frames route to the family that owns the
    market (politics is the fallback). One socket, every family fed."""

    def __init__(self, families: dict):
        self.families = families

    def put(self, slug: str, book, writer: str = "ws") -> None:
        # the missing `writer` parameter was the WHOLE dead-stream
        # mystery (frame-shape sampler, 2026-08-28): apply_frame calls
        # put(..., writer="ws"), this signature didn't accept it, and
        # every parsed book frame died on a TypeError inside the
        # stream's never-kill-the-socket guard — 252 good frames in
        # the sampler's first minutes, zero books written
        for key in ("cfb", "nfl", "nba"):
            fam = self.families.get(key)
            if fam is not None and slug in fam.universe:
                fam.cache.put(slug, book, writer=writer)
                return
        pol = self.families.get("politics")
        if pol is not None:
            pol.cache.put(slug, book, writer=writer)

    def note_trade(self, slug: str, ts: float) -> None:
        for key in ("cfb", "nfl", "nba"):
            fam = self.families.get(key)
            if fam is not None and slug in fam.universe:
                fam.cache.note_trade(slug, ts)
                return
        pol = self.families.get("politics")
        if pol is not None:
            pol.cache.note_trade(slug, ts)


def touch_snapshot(fam: Family, now: float, cap: int = 400) -> dict:
    """Best bid/ask + side totals + age per market the family is in —
    published so the book is readable without the dashboard."""
    out = {}
    slugs = sorted(fam.active_markets() | set(fam.inventory))[:cap]
    for s in slugs:
        b = fam.cache.any_age(s)
        if b is None or now - b.fetched_at > 600:
            continue
        out[s] = [round(b.bids[0][0] * 100, 1) if b.bids else None,
                  round(b.asks[0][0] * 100, 1) if b.asks else None,
                  round(sum(q for _, q in b.bids)),
                  round(sum(q for _, q in b.asks)),
                  int(now - b.fetched_at)]
    return out


# The daily ladder record (owner yes, 2026-09-02). After cfb's week 1 the
# state file's touch-and-totals could not show WHAT changed on the books
# that had paid $4-8/day — only that the touch moved. Once a day, the
# full ladders of every market the meter has us earning in, and of
# every market that earned in the last week, go to data/ladders/ so
# the next drop can be read from the record instead of the touch.
TAX_RATE = 0.22             # the pay page's "set aside — tax at 22%"
LADDER_HOUR_UTC = 16      # noon ET — inside every family's quiet hours
LADDER_KEEP_DAYS = 7
LADDER_LEVELS = 20        # nearest the touch; the deepest 4 ride along


def _ladder_levels(levels, keep: int = LADDER_LEVELS,
                   tail: int = 4) -> tuple[list, int]:
    """(levels kept, levels omitted): the touch end whole, the deep end
    where the qualifying walls sit, the middle counted."""
    lv = [[round(p, 3), round(q, 1)] for p, q in levels]
    if len(lv) <= keep + tail:
        return lv, 0
    return lv[:keep] + lv[-tail:], len(lv) - keep - tail


def ladder_snapshot(fam: Family, now: float, extra=(), cap: int = 400,
                    max_age: float = 600.0) -> tuple[dict, set]:
    """slug -> ladder for the markets the family is EARNING in (any
    order the meter has at a positive share or estimate) plus `extra`
    — the recent earners, so a market that STOPS earning still gets
    its after picture. Books come from the cache; nothing is fetched.
    Returns (ladders, the slugs earning right now)."""
    earning: set = set()
    ours_by: dict[str, list] = {}
    for o in list(fam.orders.values()):
        ours_by.setdefault(o.market, []).append(o)
        if (o.live_share or 0.0) > 0 or (o.live_est or 0.0) > 0:
            earning.add(o.market)
    out: dict = {}
    for s in sorted(earning | set(extra))[:cap]:
        b = fam.cache.any_age(s)
        if b is None or now - b.fetched_at > max_age:
            continue
        bids, bmore = _ladder_levels(b.bids)
        asks, amore = _ladder_levels(b.asks)
        out[s] = {
            "t": int(b.fetched_at), "tick": b.tick,
            "bids": bids, "asks": asks,
            "bids_more": bmore, "asks_more": amore,
            "bid_total": round(sum(q for _, q in b.bids)),
            "ask_total": round(sum(q for _, q in b.asks)),
            "ours": [[o.side, o.price, round(o.qty, 2), o.purpose,
                      round(o.live_share or 0.0, 4),
                      round(o.live_est or 0.0, 4)]
                     for o in ours_by.get(s, [])],
        }
    return out, earning


def ladder_due(now: float, last_day: str,
               hour: int = LADDER_HOUR_UTC) -> str | None:
    """The UTC day to write, once its hour has come and it is not yet
    written; None otherwise."""
    t = time.gmtime(now)
    day = time.strftime("%Y-%m-%d", t)
    if day == last_day or t.tm_hour < hour:
        return None
    return day


def prune_ladder_seen(seen: dict, day: str,
                      keep_days: int = LADDER_KEEP_DAYS) -> dict:
    """Recent earners are remembered for a week, then dropped."""
    import datetime as _dt
    cutoff = (_dt.date.fromisoformat(day)
              - _dt.timedelta(days=keep_days)).isoformat()
    return {s: d for s, d in seen.items() if d >= cutoff}


# Memory (owner, 2026-09-02, from the DigitalOcean graph: flat at 33% of
# the 1 GB box all day, a step to 57% when discovery and the survey
# frame refetched in the same minute on the boot+6h clock, a spike to
# 90% at the hourly publish three minutes later, then the kill — and
# every boot since replaying the same peak in its own second cycle).
# The number the app could not state is now stated every cycle.
SURVEY_FRAME_EVERY_S = 6 * 3600.0
SURVEY_BOOT_WAIT_S = 600.0          # discovery and the first publish own
                                    # a boot's first minutes
SURVEY_FIRST_OFFSET_S = 3 * 3600.0  # puts the refetch clock three hours
                                    # off discovery's, for good


def rss_mb() -> float:
    """Resident memory of this process in MB: /proc where it exists,
    the getrusage peak as the fallback, 0 when neither answers."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:  # noqa: BLE001
        return 0.0


def mem_limit_mb() -> float | None:
    """The container's memory ceiling in MB, from the cgroup, or None."""
    for p in ("/sys/fs/cgroup/memory.max",
              "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(p) as f:
                v = f.read().strip()
        except OSError:
            continue
        if v.isdigit() and int(v) < (1 << 50):
            return int(v) / 1048576.0
    return None


def survey_frame_due(now: float, boot_ts: float, last_at: float,
                     every: float = SURVEY_FRAME_EVERY_S,
                     boot_wait: float = SURVEY_BOOT_WAIT_S,
                     first_offset: float = SURVEY_FIRST_OFFSET_S):
    """When the survey frame refetches: never in a boot's first minutes,
    then every six hours on a clock three hours off discovery's, so the
    two biggest fetches never share a minute. Returns the value to keep
    as last_at when due, None otherwise. The first fetch after a boot
    stores now + first_offset, which is what shifts the clock."""
    if now - boot_ts < boot_wait:
        return None
    if last_at and now - last_at < every:
        return None
    return now + (first_offset if not last_at else 0.0)


class Monitor:
    def __init__(self):
        self.client = Client()
        self.alerts = Alerts()
        self.names = Names()
        self.store = StateStore(os.environ.get("V3_STATE_PATH", "v3_state.json"))
        self.build = build_hash()
        self.boot_ts = time.time()
        self.errors: list[str] = []
        self.audit: list[dict] = []
        self.last_state: dict = {}
        self.boots: list[float] = []
        # one answer for every desk: is the exchange taking our orders?
        self.place_health = PlaceHealth(on_change=self._place_health_changed)
        self.master = MasterSwitch(alert=self.alerts.notify,
                                   name="3.0 master switch", scope="all of 3.0")
        # The floor handshake (v3/floor.py): master ON asks 1.0 and 2.0 to
        # halt their automation; nothing here touches an order until both
        # have acknowledged. _floor_ok is refreshed every cycle and read by
        # every desk's switch closure.
        self.floor = Floor()
        self._floor_ok = False
        self.flatten = flatten_active()
        self.flatten_done = False          # phase two reached (persisted)
        self.flat_stats = {"cancelled": 0, "failed": 0}
        self.last_flat: dict | None = None
        self._history_at = 0.0
        # the boot readout: what the first cycle is doing right now, so a
        # restart shows a progress bar instead of a scary red "stale"
        self.boot_stage = {"stage": "starting", "pct": 2, "ts": time.time()}
        self.payload_json: bytes | None = None    # frozen /data.json body
        # per-market fair values SET BY THE OWNER from the orders page —
        # his number beats the model everywhere fair is used (owner,
        # 2026-08-23: "Give me an option to set fair market for the
        # 2028 markets because you're off")
        self.owner_fairs: dict[str, float] = {}
        self.backfilled = False        # one-shot journal recovery
        self.evidence_seeded = False   # one-shot evidence seed
        self._first_cycle_done = False
        self.silver = SilverFairs(client=self.client)
        self.samplers: dict[str, Estimator] = {}
        self.actuals_by_day: dict[str, float] = {}
        self.actuals_by_fam: dict[str, float] = {}   # "day|family" -> usd
        self.rewards_seen: dict[str, float] = {}
        # per market-day PAID (skipped rows excluded) and per market-day
        # CLAIM snapshot — the two sides of the closed-card rewards
        # verification (owner approved 2026-08-27: "build the
        # verification using the posted rewards number")
        self.paid_seen: dict[str, float] = {}
        self.mkt_claim_day: dict[str, float] = {}
        # the cycling market survey: a seeded sampler and the running
        # per-prefix evidence. The seed is recorded so a run can be
        # reproduced and audited (owner, 2026-08-31).
        from . import survey as _sv
        self.survey = _sv.Sampler(seed=20260831)
        self.survey_stats: dict[str, _sv.PrefixStat] = {}
        self.survey_at = 0.0
        self.survey_frame_note = ""
        self.survey_meta: dict = {}
        self.survey_event_n: dict = {}
        self.cancel_jobs: list = []
        # the daily ladder snapshot (owner, 2026-09-02): which UTC day
        # has been written, and which markets earned recently so a
        # market that STOPS earning still gets its "after" picture
        self.ladder_day: str = ""
        # journal corrections waiting for the fills.csv archive: the
        # archive is append-only, so a fixed or voided row is written as
        # a new line at correction time, never rewritten in place
        self.journal_fixes: list = []
        self._last_trade_rows: list = []
        self.ladder_seen: dict[str, str] = {}
        self.rw_last: dict | None = None      # latest payout-check result
        self._rw_at = 0.0
        self._lock = threading.Lock()
        self.families: dict[str, Family] = {}
        self.switches: dict[str, MasterSwitch] = {}
        for key, (cfg_fn, discover) in FAMILIES.items():
            cfg = cfg_fn()
            sw = MasterSwitch(alert=self.alerts.notify,
                              name=f"{cfg.name} switch", scope=cfg.name)
            cache = BookCache()
            fam = Family(None, cache, discover, config=cfg,
                         alert=self.alerts.notify, names=self.names)
            desk = OrderDesk(
                client=self.client,
                health=self.place_health,
                whitelist=fam.knows,
                switch_on=lambda s=sw: (self.master.on and s.on
                                        and self._floor_ok),
                fresh_book=lambda slug, c=cache: c.fresh(slug, 120.0, time.time()),
                # the price grid, resolved apart from book freshness:
                # the exchange's own figure where we have it, else the
                # last book of any age. No grid, no order.
                tick_for=lambda slug, c=cache: c.grid(slug),
                # we cannot buy our own orders (owner, 2026-09-02): the
                # take rails measure against what OTHERS show
                own_at=lambda slug, side, px, f=fam: sum(
                    o.qty for o in list(f.orders.values())
                    if o.market == slug and o.side == side
                    and abs(o.price - px) < 1e-9),
                log=self._audit,
            )
            fam.desk = desk
            fam.fairs = self._fair_for
            # every family's fill model learns from its own book feed
            cache.on_put = (lambda slug, book, f=fam:
                            f.fillmodel.observe_touch(
                                slug,
                                book.bids[0][0] if book.bids else None,
                                book.asks[0][0] if book.asks else None,
                                book.tick, book.fetched_at))
            self.families[key] = fam
            self.switches[key] = sw
            self.samplers[key] = Estimator()
        # Bonds: the tax reserve earning like a bond (owner, 2026-09-02).
        # Silver's odds propose, the owner approves, the engine stays
        # out; its own switch, off by default like every other
        from .bonds import Bonds
        self.switches["bonds"] = MasterSwitch(alert=self.alerts.notify,
                                              name="Bonds switch",
                                              scope="Bonds")
        self.bonds = Bonds(self.families["politics"], self.client,
                           self.silver.model_fair, alert=self.alerts.notify,
                           tax_owed=self._tax_owed, parse=parse_activities)
        # The book stream: politics markets subscribe first (its cache is
        # the one the stream writes); a dead stream degrades to REST
        # polling through the cache's own age interlock.
        pol = self.families.get("politics")
        self.stream = (Stream(CacheRouter(self.families), self._ws_slugs,
                              self.client.key_id, self.client.secret_key)
                       if pol is not None else None)
        self._restore()
        self.boots = [b for b in self.boots if time.time() - b < 86400]
        self.boots.append(time.time())
        # A deploy replaces the container and its floor files with it. If the
        # master came back ON, the request must be back on disk before 1.0's
        # first automation pass, not a poll later.
        self.floor.write_want(self.master.on or self.flatten)

    def _ws_slugs(self) -> list[str]:
        """The owner's slot order (2026-08-21): every politics market
        he is in seats first — that is the priority — then football
        markets holding orders, then idle candidates rotate through the
        leftover slots. Promising candidates (a measured rate or a
        planned estimate) hold stable seats or rotate often; cold ones
        get a thin rotation lane."""
        from .ws import SUB_CAP
        out: list[str] = []
        seen: set[str] = set()

        def take(slugs, room=None):
            for s in slugs:
                if room is not None and len(out) >= room:
                    break
                if s not in seen:
                    seen.add(s)
                    out.append(s)

        # the bonds he is in hold seats before everything (owner,
        # 2026-09-03: "reserve a websocket for each of the markets I'm
        # in") — the bonds page's live line reads their books from the
        # cache this stream feeds
        bonds = getattr(self, "bonds", None)
        if bonds is not None:
            take(sorted(bonds.held_markets()), room=SUB_CAP)
            # and every listed bond market: the bonds page reads their
            # books (owner, 2026-09-03: "A lot of the books are stale")
            take(sorted(bonds.approved), room=SUB_CAP)
        for key in ("politics", "cfb", "nfl", "nba"):
            fam = self.families.get(key)
            if fam is not None:
                # the owner's watched races seat before everything
                take(sorted(s2 for s2 in fam.universe
                            if fam._watched(s2)), room=SUB_CAP)
        for key in ("politics", "cfb", "nfl", "nba"):
            fam = self.families.get(key)
            if fam is not None:
                take(sorted(fam.active_markets() | set(fam.inventory)),
                     room=SUB_CAP)
        cands: list[tuple[float, str]] = []
        for key in ("politics", "cfb", "nfl", "nba"):
            fam = self.families.get(key)
            if fam is None:
                continue
            est = self.samplers.get(key)
            rates = est.market_rates if est is not None else {}
            for s, sb in fam.scoreboard.items():
                if s in seen:
                    continue
                promise = max(rates.get(s) or 0.0,
                              (sb.get("est") or 0.0) if sb.get("plans")
                              else 0.0)
                cands.append((promise, s))
        cands.sort(key=lambda t: (-t[0], t[1]))
        warm = [s for p, s in cands if p > 0.0]
        cold = [s for p, s in cands if p <= 0.0]
        room = max(SUB_CAP - len(out), 0)
        take(warm[:room // 2], room=SUB_CAP)     # stable seats for the best
        warm_rest = warm[room // 2:]

        def rotate(pool, n, window):
            if not pool or n <= 0:
                return []
            n = min(n, len(pool))
            off = (window * n) % len(pool)
            return (pool + pool)[off:off + n]

        window = int(time.time() // 900)         # a fresh mix every 15 min
        room = max(SUB_CAP - len(out), 0)
        take(rotate(warm_rest, (room * 3) // 4, window), room=SUB_CAP)
        room = max(SUB_CAP - len(out), 0)
        take(rotate(cold, room, window), room=SUB_CAP)
        return out[:SUB_CAP]

    def _sampler_loop(self) -> None:
        """The independent clock (REBUILD.md's lesson): earnings are
        sampled every 20s by this thread, never by anything that just
        placed an order. Nothing here can touch an order."""
        while True:
            time.sleep(20.0)
            now = time.time()
            with self._lock:
                for key, fam in self.families.items():
                    try:
                        orders = [{"market": o.market, "side": o.side,
                                   "price": o.price, "size": o.qty}
                                  for o in list(fam.orders.values())]
                        self.samplers[key].sample(
                            now, orders, fam.cache, fam.terms,
                            side_pool=lambda s, p, f=fam: f._side_pool(s, p))
                    except Exception:  # noqa: BLE001 — measuring never breaks
                        pass

    def _audit(self, row: dict) -> None:
        self.audit.append(row)
        del self.audit[:-200]

    def _place_health_changed(self, blocked: bool, note: str) -> None:
        """The placement breaker tripped or cleared (owner, 2026-09-05):
        say so once each way, on the phone and in the notes."""
        if blocked:
            self._note(f"exchange refuses placements from this server: {note}"
                       " — moves and re-prices paused; probing once a minute")
            self.alerts.notify(
                "3.0: exchange refuses this server's orders",
                "\"Your connection looks like a VPN.\" Nothing is cancelled "
                "that could not come back; one placement a minute probes. "
                "Fix: tap Deploy for a new outbound address.",
                priority="high")
        else:
            self._note(f"exchange accepts placements again: {note}")
            self.alerts.notify("3.0: placements accepted again", note)

    def _note(self, msg: str) -> None:
        self.errors.append(f"{time.strftime('%m-%d %H:%M:%S')} {msg}")
        del self.errors[:-40]
        print(f"v3: {msg}", flush=True)

    # -- persistence --------------------------------------------------------

    def _restore(self) -> None:
        saved = self.store.load_best()
        if not saved:
            self._note(f"booted build {self.build}; fresh state — "
                       "every switch is off")
            return
        if saved.get("master_switch"):
            self.master.restore(saved["master_switch"])
        if saved.get("names"):
            self.names.restore(saved["names"])
        for key, fam in self.families.items():
            if saved.get(f"fam_{key}"):
                fam.restore(saved[f"fam_{key}"])
            if saved.get(f"sw_{key}"):
                self.switches[key].restore(saved[f"sw_{key}"])
            if saved.get(f"est_{key}"):
                self.samplers[key] = Estimator.from_dict(saved[f"est_{key}"])
            if saved.get(f"evi_{key}"):
                fam.evidence.restore(saved[f"evi_{key}"])
        self.errors = list(saved.get("errors") or [])
        self.boots = list(saved.get("boots") or [])
        self.audit = list(saved.get("audit") or [])
        self.flatten_done = bool(saved.get("flatten_done"))
        self.flat_stats = dict(saved.get("flat_stats")
                               or {"cancelled": 0, "failed": 0})
        self.rewards_seen = dict(saved.get("rewards_seen") or {})
        self.paid_seen = dict(saved.get("paid_seen") or {})
        self.mkt_claim_day = dict(saved.get("mkt_claim_day") or {})
        from . import survey as _sv2
        for pref, row in (saved.get("survey_stats") or {}).items():
            st = _sv2.PrefixStat(prefix=pref)
            st.__dict__.update({k: v for k, v in row.items()
                                if k in st.__dict__})
            self.survey_stats[pref] = st
        self.survey_frame_note = str(saved.get("survey_frame") or "")
        self.cancel_jobs = list(saved.get("cancel_jobs") or [])
        self.ladder_day = str(saved.get("ladder_day") or "")
        if saved.get("bonds"):
            self.bonds.restore(saved["bonds"])
        pl = saved.get("pos_last") or {}
        if pl.get("pos") and time.time() - float(pl.get("at") or 0.0) < 3600.0:
            # the last accepted position read survives a restart, so a
            # short read right after boot is caught like any other
            self._pos_last = {str(k): tuple(v) for k, v in pl["pos"].items()}
            self._pos_last_at = float(pl.get("at") or 0.0)
        if saved.get("sw_bonds"):
            self.switches["bonds"].restore(saved["sw_bonds"])
        self.ladder_seen = {str(k): str(v) for k, v in
                            (saved.get("ladder_seen") or {}).items()}
        self.actuals_by_day = dict(saved.get("actuals_by_day") or {})
        self.actuals_by_fam = dict(saved.get("actuals_by_fam") or {})
        self.owner_fairs = {k: float(v) for k, v in
                            (saved.get("owner_fairs") or {}).items()}
        self.backfilled = bool(saved.get("backfilled_600"))
        self.evidence_seeded = bool(saved.get("evidence_seeded"))
        self.silver.changes = list(saved.get("silver_log") or [])
        self.rw_last = saved.get("rewards_last")
        age = time.time() - (saved.get("saved_at") or 0)
        armed = [k for k, sw in self.switches.items() if sw.on and self.master.on]
        self._note(f"booted build {self.build}; restored state {age:.0f}s old"
                   + (f"; ARMED: {', '.join(armed)}" if armed else ""))
        # READ-ONLY book comparison (owner approved 2026-08-25): log
        # what each endpoint sees for a few of our markets. No fetch
        # path changes; the lines land in the notes for the next check.
        try:
            slugs = []
            for fam in self.families.values():
                for o in list(fam.orders.values()):
                    if o.market not in slugs:
                        slugs.append(o.market)
                    if len(slugs) >= 4:
                        break
                if len(slugs) >= 4:
                    break
            for line in (self.client.compare_book_sources(slugs)
                         if slugs else []):
                self._note("book compare: " + line)
        except Exception as e:  # noqa: BLE001 — never blocks a boot
            self._note(f"book compare failed: {type(e).__name__}: {e}")
        if armed and saved.get("build") != self.build:
            self.alerts.notify("3.0: new build with a switch ON",
                               f"build {self.build} booted; may place orders "
                               f"({', '.join(armed)})")

    def _grades(self) -> list[dict]:
        """Per-day estimate vs what the exchange actually paid. The
        estimate is 3.0's own sampler from the day it took over; the
        actuals are the whole account's postings (during the transition
        the older versions' books pay into the same number — labelled so
        on the page)."""
        est_by_day: dict[str, dict] = {}
        for key, est in self.samplers.items():
            for h in est.history:
                row = est_by_day.setdefault(h["day"], {"est": 0.0, "stale_s": 0.0})
                row["est"] += h.get("earned") or 0.0
                row["stale_s"] += h.get("stale_s") or 0.0
            if est.day:
                row = est_by_day.setdefault(est.day, {"est": 0.0, "stale_s": 0.0})
                row["est"] += est.earned
                row["stale_s"] += est.stale_s
        days = sorted(set(est_by_day) | set(self.actuals_by_day))[-14:]
        return [{"day": d,
                 "est": round(est_by_day.get(d, {}).get("est", 0.0), 2)
                 if d in est_by_day else None,
                 "actual": self.actuals_by_day.get(d),
                 "unmeasured_min": round(
                     est_by_day.get(d, {}).get("stale_s", 0.0) / 60.0, 1)}
                for d in days]

    def _tax_owed(self) -> dict | None:
        """What he owes on everything paid so far, at the pay page's
        rate. The bonds budget follows it (owner, 2026-09-03: "set the
        budget to whatever I currently owe in taxes")."""
        pt = self._paid_total()
        if not pt:
            return None
        return {"owed": round(pt["usd"] * TAX_RATE, 2), "gross": pt["usd"],
                "rate": TAX_RATE, "days": pt["days"], "since": pt["since"]}

    def _paid_total(self) -> dict | None:
        """All-time posted rewards: EVERY day in rewards.csv, not just
        the rows the grades page lists (owner, 2026-08-22: "way more
        than 12 posted days — just look at rewards.csv")."""
        if not self.actuals_by_day:
            return None
        return {"usd": round(sum(self.actuals_by_day.values()), 2),
                "days": len(self.actuals_by_day),
                "since": min(self.actuals_by_day)}

    def _state(self, now: float, summaries: dict) -> dict:
        st = {
            "saved_at": now, "build": self.build, "boot_ts": self.boot_ts,
            "boots": self.boots[-20:], "errors": self.errors,
            "audit": self.audit[-60:],
            "master_switch": self.master.to_dict(),
            "flatten_done": self.flatten_done,
            "flat_stats": self.flat_stats,
            "rewards_seen": self.rewards_seen,
            "paid_seen": self.paid_seen,
            "mkt_claim_day": self.mkt_claim_day,
            "cancel_jobs": list(getattr(self, "cancel_jobs", [])),
            "rss_mb": round(rss_mb(), 1),
            "bonds": self.bonds.to_dict(),
            "pos_last": {"at": round(getattr(self, "_pos_last_at", 0.0), 1),
                         "pos": {k: list(v) for k, v in
                                 (getattr(self, "_pos_last", None) or {}).items()}},
            "sw_bonds": self.switches["bonds"].to_dict(),
            "ladder_day": getattr(self, "ladder_day", ""),
            "ladder_seen": dict(getattr(self, "ladder_seen", {})),
            "survey_stats": {p: st.__dict__ for p, st
                             in list(self.survey_stats.items())[:200]},
            "survey_frame": self.survey_frame_note,
            "actuals_by_day": self.actuals_by_day,
            "actuals_by_fam": self.actuals_by_fam,
            "owner_fairs": dict(self.owner_fairs),
            "backfilled_600": bool(self.backfilled),
            "evidence_seeded": bool(self.evidence_seeded),
            "names": self.names.to_dict(),
            "summaries": summaries,
            "floor": self.floor.status(now),
            "ws": dict(self.stream.status) if self.stream else {},
            "lite_study": self._lite_study(),

            "silver_log": self.silver.changes[-120:],
            "rewards_last": self.rw_last,
            "silver": {
                "priced": sum(1 for s in self.families["politics"].universe
                              if self.families["politics"].enterable(s)
                              and self.silver.model_fair(s) is not None),
                "unpriced": sum(1 for s in self.families["politics"].universe
                                if self.families["politics"].enterable(s)
                                and self.silver.model_fair(s) is None),
                "senate_races": len(self.silver.races),
                "gov_races": len(self.silver.gov_races),
                "tables_age_min": (round((now - self.silver.fetched_at) / 60)
                                   if self.silver.fetched_at else None),
                "tables_changed_h": (round(
                    (now - self.silver.changed_at) / 3600, 1)
                    if getattr(self.silver, "changed_at", 0) else None),
                "gov_changed_h": (round(
                    (now - self.silver.gov_changed_at) / 3600, 1)
                    if getattr(self.silver, "gov_changed_at", 0) else None),
                "note": getattr(self.silver, "note", ""),
                "ak_gov": dict(self.silver.gov_races.get("ak") or {}),
                "official_source": self.silver.official_source,
                "official_age_h": (round(
                    self.silver.official_run_age_s(now) / 3600, 1)
                    if self.silver.official_meta else None),
                "meta": dict(self.silver.official_meta or {}),
            },
            "grades": self._grades(),
            "paid_total": self._paid_total(),
            "flatten": ({"active": self.flatten,
                         "done": self.flatten_done, **(self.last_flat or {})}
                        if self.flatten else {"active": False}),
            "alerts_log": self.alerts.log[-30:],
        }
        for key, fam in self.families.items():
            st[f"fam_{key}"] = fam.to_dict()
            st[f"est_{key}"] = self.samplers[key].to_dict()
            st[f"evi_{key}"] = fam.evidence.to_dict()
            st[f"sw_{key}"] = self.switches[key].to_dict()
            st[f"touches_{key}"] = touch_snapshot(fam, now)
        return st

    # -- owner controls -----------------------------------------------------

    def switch_tap(self, op: str, which: str = "master") -> dict:
        """A tap on /v3/switch. Persisted IMMEDIATELY, local and remote —
        a restart between a flip and the next save must not undo it."""
        sw = self.switches.get(which, self.master)
        s = sw.op(op)
        self.floor.write_want(self.master.on or self.flatten)
        st = dict(self.last_state) if self.last_state else {}
        st["master_switch"] = self.master.to_dict()
        for key in self.families:
            st[f"sw_{key}"] = self.switches[key].to_dict()
        st["sw_bonds"] = self.switches["bonds"].to_dict()
        st["saved_at"] = time.time()
        self.last_state = st
        self.freeze_payload()      # a switch flip shows immediately
        self.store.save_local(st)
        self.store.save_remote(st)
        return s

    @staticmethod
    def _et_today(hhmm: str, now: float) -> float | None:
        """An ET clock time today ("17:00") as an epoch, or None."""
        return Monitor._et_at(hhmm, now)

    @staticmethod
    def _et_at(value, now: float) -> float | None:
        """An ET clock time as an epoch: "17:00" is today, "17:00 tomorrow"
        (or "tomorrow 17:00") is tomorrow, "2026-09-06 17:00" is that day
        (owner, 2026-09-05: "set the college football to be active until a
        time tomorrow. It looks like I can only have it active today").
        None when it cannot be read."""
        import datetime as _dt
        import re as _re
        from .family import ET
        days = 0
        date = None
        clock = []
        for p in str(value or "").strip().lower().split():
            if p == "tomorrow":
                days = 1
            elif p == "today":
                days = 0
            elif _re.fullmatch(r"\d{4}-\d{2}-\d{2}", p):
                date = p
            else:
                clock.append(p)
        if len(clock) != 1:
            return None
        try:
            h, m = clock[0].split(":")[:2]
            h, m = int(h), int(m)
        except (TypeError, ValueError):
            return None
        if not (0 <= h < 24 and 0 <= m < 60):
            return None
        if date is not None:
            try:
                day = _dt.date.fromisoformat(date)
            except ValueError:
                return None
        else:
            day = (_dt.datetime.fromtimestamp(now, ET).date()
                   + _dt.timedelta(days=days))
        return _dt.datetime(day.year, day.month, day.day, h, m, tzinfo=ET).timestamp()

    def set_active_until(self, which: str, value) -> dict:
        """The owner's say over a family's game window (2026-09-04:
        "Cfb can go active until 5:00 pm eastern today"): until the ET
        time given, today, the family rests as in resting hours. An
        empty value clears it. Persisted at once, like a switch flip."""
        fam = self.families.get(which)
        if fam is None:
            return {"ok": False, "note": f"no family called {which}"}
        now = time.time()
        if value in (None, "", "-", "clear"):
            fam.active_until = 0.0
            note = f"{fam.cfg.name}: back to its own game window"
        else:
            ts = self._et_at(str(value), now)
            if ts is None:
                return {"ok": False, "note": "pick a time like 17:00, or "
                                             "17:00 tomorrow"}
            if ts <= now:
                return {"ok": False, "note": "that time has passed"}
            if ts > now + 8 * 86400.0:
                return {"ok": False, "note": "that is more than a week out — "
                                             "the window re-decides week to week"}
            fam.active_until = float(ts)
            import datetime as _dt
            from .family import ET
            at = _dt.datetime.fromtimestamp(ts, ET)
            today = _dt.datetime.fromtimestamp(now, ET).date()
            shown = at.strftime("%I:%M %p").lstrip("0")
            delta = (at.date() - today).days
            when = ("today" if delta == 0 else "tomorrow" if delta == 1
                    else at.strftime("%a %b %d").replace(" 0", " "))
            note = f"{fam.cfg.name} stays active until {shown} ET {when}"
        self._audit({"op": "active_until", "family": which,
                     "until": fam.active_until, "initiator": "owner", "ts": now})
        self._note(note)
        st = dict(self.last_state) if self.last_state else {}
        st[f"fam_{which}"] = fam.to_dict()
        st["saved_at"] = now
        self.last_state = st
        self.freeze_payload()
        self.store.save_local(st)
        self.store.save_remote(st)
        return {"ok": True, "note": note, "active_until": fam.active_until}

    def _fair_for(self, slug: str) -> float | None:
        """One fair per market: the OWNER'S number when he has set one,
        else the model's. Every consumer of fair — the past-fair caps,
        exit guards, EV edge, watch cards — sees the same value."""
        own = self.owner_fairs.get(slug)
        if own is not None:
            return own
        return self.silver.model_fair(slug)

    def set_owner_fair(self, market: str, fair: float | None) -> dict:
        """Owner control from the orders page. fair in DOLLARS
        (0.001-0.999); None clears back to the model."""
        if not any(fam.knows(market) for fam in self.families.values()):
            return {"ok": False,
                    "note": "no family knows this market — check the slug"}
        if fair is None:
            had = self.owner_fairs.pop(market, None)
            note = ("owner fair cleared — the model prices it again"
                    if had is not None else "no owner fair was set")
        else:
            if not (0.001 <= fair <= 0.999):
                return {"ok": False, "note": "fair must be 0.1c to 99.9c"}
            self.owner_fairs[market] = round(float(fair), 4)
            note = f"owner fair set: {fair * 100:g}c — beats the model"
        # the resting book in this market is now suspect: re-check it
        # first on the next sweep instead of waiting its turn
        for fam in self.families.values():
            if fam.knows(market):
                fam.priority.add(market)
        self._audit({"op": "owner_fair", "market": market,
                     "fair": fair, "ts": time.time()})
        self._note(f"{note} ({market})")
        # persisted IMMEDIATELY, like a switch flip — a restart between
        # the tap and the next save must not undo it
        st = dict(self.last_state) if self.last_state else {}
        st["owner_fairs"] = dict(self.owner_fairs)
        st["saved_at"] = time.time()
        self.last_state = st
        self.freeze_payload()
        self.store.save_local(st)
        self.store.save_remote(st)
        return {"ok": True, "note": note}

    def owner_place(self, market: str, side: str, price: float,
                    qty: float) -> dict:
        """The owner's own hand: bypasses switches, keeps every other
        rail, and the automation never touches the result."""
        from .family import FamilyOrder
        for fam in self.families.values():
            if not fam.knows(market):
                continue
            net = 0.0
            try:
                net = (self.client.positions_net().get(market) or (0.0,))[0]
            except Exception:  # noqa: BLE001
                pass
            r = fam.desk.place_resting(market, side, price, qty,
                                       net_position=net, initiator="owner",
                                       verify=True)
            if r.ok and r.order_id:
                fam.orders[r.order_id] = FamilyOrder(
                    id=r.order_id, market=market, side=side,
                    price=(r.price or price),
                    qty=qty, intent=r.intent, placed_ts=time.time(),
                    purpose="manual", why="placed by the owner")
            return {"ok": r.ok, "note": r.note, "order_id": r.order_id}
        return {"ok": False,
                "note": "no family knows this market — check the slug"}

    QUALIFY_MAX_ORDERS = 80     # hard stop on one run, whatever happens
    QUALIFY_MAX_S = 900.0       # and a wall clock on it
    QUALIFY_BP_FLOOR = 10.0     # stop building while under $10 free
    QUALIFY_MAX_COLLATERAL = 500.0   # refuse a gap that would hold more

    def _rested_size(self, order_id: str, max_wait: float = 10.0) -> float:
        """How many shares of one order are actually resting, polled
        until the open-order list shows it (it lags placements ~4s).
        0.0 = never seen resting — the caller must NOT re-post for the
        same intent (the first may still land late)."""
        deadline = time.time() + max_wait
        while True:
            try:
                for o in self.client.open_orders():
                    if o["id"] == order_id:
                        return float(o["size"])
            except Exception:  # noqa: BLE001
                pass
            if time.time() >= deadline:
                return 0.0
            time.sleep(1.0)

    def qualify_ask(self, market: str) -> dict:
        """The watched-races button (owner, 2026-08-28 "give me a button
        to auto qualify the ask side", then 2026-08-30 "keeps placing
        orders until the target size is reached"): build the ask side up
        to Target Size, however many orders that takes.

        The exchange trims each order to free buying power (~300 shares
        at a time on the boosted races), so a 10,000-share wall is ~30
        orders and several minutes — far too long to hold a phone
        request open. The run happens in a background thread; this
        returns at once, and tapping again reports progress. Every
        order goes on the owner's hand rail (purpose "manual") — the
        automation never touches the result."""
        import math
        import threading

        from . import survey as sv
        jobs = getattr(self, "_qualify_jobs", None)
        if jobs is None:
            jobs = self._qualify_jobs = {}
        job = jobs.get(market)
        if job and job.get("state") == "running":
            return {"ok": True, "note": self._qualify_note(job)}
        for fam in self.families.values():
            if not fam.knows(market):
                continue
            prog = fam.terms.get(market)
            if prog is None or not prog.target:
                return {"ok": False, "note": "no Target Size on record "
                        "here — reward terms not read yet"}
            try:
                book = self.client.book(market, fetched_at=time.time())
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "note": f"could not read the book: {e}"}
            fam.cache.put(market, book)
            ask_total = sum(q for _, q in book.asks)
            # build PAST the line, not to it (owner, 2026-09-01: "make
            # it so my orders buy 125% of the target size"). A side
            # sitting exactly at Target Size drops under it the moment
            # somebody else pulls, and under the line the whole side
            # pays nobody.
            goal = prog.target * sv.QUALIFY_TARGET_MULT
            gap = goal - ask_total
            if gap <= 0:
                return {"ok": False, "note":
                        f"the ask side already qualifies with room — "
                        f"{ask_total:,.0f} shares resting vs a Target Size "
                        f"of {prog.target:,.0f} "
                        f"({ask_total / prog.target:.0%})"}
            tick = book.tick or 0.01
            px = round(math.floor(0.999 / tick + 1e-9) * tick, 3)
            collat = gap * (1.0 - px)
            if collat > self.QUALIFY_MAX_COLLATERAL:
                return {"ok": False, "note":
                        f"refused: closing the {gap:,.0f}-share gap would "
                        f"hold ${collat:,.0f} of collateral — over the "
                        f"button's ${self.QUALIFY_MAX_COLLATERAL:,.0f} cap"}
            job = jobs[market] = {"state": "running", "placed": 0,
                                  "shares": 0.0, "target": prog.target,
                                  "goal": goal,
                                  "started": time.time(), "stop": "",
                                  "ask_total": ask_total}
            threading.Thread(target=self._qualify_run, args=(market, fam),
                             daemon=True,
                             name=f"qualify-{market[:20]}").start()
            return {"ok": True, "note":
                    f"building the wall to {sv.QUALIFY_TARGET_MULT:.0%} of "
                    f"Target Size ({goal:,.0f} shares): {gap:,.0f} to go at "
                    f"{px * 100:.1f}c (~${collat:,.2f} collateral). "
                    f"Running in the background — tap again for progress."}
        return {"ok": False,
                "note": "no family knows this market — check the slug"}

    @staticmethod
    def _qualify_note(job: dict) -> str:
        """One phone-readable line about a wall run."""
        got, n = job.get("shares", 0.0), job.get("placed", 0)
        tgt, now_t = job.get("target", 0.0), job.get("ask_total", 0.0)
        goal = job.get("goal", tgt)
        head = (f"{'building' if job.get('state') == 'running' else 'done'}: "
                f"{n} order{'s' if n != 1 else ''}, {got:,.0f} shares "
                f"rested — ask side {now_t:,.0f} of {goal:,.0f} "
                f"({(now_t / tgt) if tgt else 0:.0%} of Target Size)")
        if job.get("state") == "running":
            return head + " — still going"
        if now_t >= goal:
            return head + " — QUALIFIES with room"
        if now_t >= tgt:
            return head + " — qualifies, but no headroom yet"
        return head + (f" — stopped: {job['stop']}" if job.get("stop")
                       else " — stopped")

    def _qualify_run(self, market: str, fam) -> dict:
        """Place asks until the side clears Target Size with the
        owner's headroom. The gap is recomputed from a FRESH book every
        pass, so the run self-corrects for other people's orders, for
        the exchange's trims, and for an order that lands late — it
        never re-posts blind for shares it already has."""
        import math

        from . import survey as sv

        from .family import FamilyOrder
        job = self._qualify_jobs[market]
        deadline = time.time() + self.QUALIFY_MAX_S
        zero_streak = 0
        # the shares this run set out to add. A second belt beside the
        # book reading: if we have verifiably rested that many, stop —
        # even if the book we fetch has not caught up with our own
        # orders yet. Without it a lagging book would keep the loop
        # placing against a gap it has already closed.
        need = max(job.get("goal", job["target"]) - job["ask_total"], 0.0)
        try:
            while True:
                if job["placed"] >= self.QUALIFY_MAX_ORDERS:
                    job["stop"] = (f"{self.QUALIFY_MAX_ORDERS}-order limit "
                                   f"for one run — tap again to continue")
                    break
                if time.time() >= deadline:
                    job["stop"] = "15-minute limit — tap again to continue"
                    break
                try:
                    book = self.client.book(market, fetched_at=time.time())
                except Exception as e:  # noqa: BLE001
                    job["stop"] = f"could not read the book: {e}"
                    break
                fam.cache.put(market, book)
                prog = fam.terms.get(market)
                target = prog.target if prog is not None else job["target"]
                goal = target * sv.QUALIFY_TARGET_MULT
                ask_total = sum(q for _, q in book.asks)
                job["ask_total"], job["target"] = ask_total, target
                job["goal"] = goal
                gap = goal - ask_total
                if gap < 1.0:
                    break                       # qualifies — done
                if job["shares"] >= need - 0.5 and job["placed"] > 0:
                    job["stop"] = ("rested the full gap already — the "
                                   "book has not caught up yet")
                    break
                bp = None
                try:
                    bp = self.client.buying_power()
                except Exception:  # noqa: BLE001
                    pass
                if bp is not None and bp < self.QUALIFY_BP_FLOOR:
                    job["stop"] = (f"buying power down to ${bp:,.2f} — "
                                   f"free some and tap again")
                    break
                tick = book.tick or 0.01
                px = round(math.floor(0.999 / tick + 1e-9) * tick, 3)
                net = 0.0
                try:
                    net = (self.client.positions_net().get(market)
                           or (0.0,))[0]
                except Exception:  # noqa: BLE001
                    pass
                r = fam.desk.place_resting(market, "SELL", px,
                                           float(math.ceil(gap)),
                                           net_position=net,
                                           initiator="owner", verify=False)
                if not (r.ok and r.order_id):
                    job["stop"] = r.note
                    break
                rested = self._rested_size(r.order_id)
                if rested >= 1.0:
                    zero_streak = 0
                    job["placed"] += 1
                    job["shares"] += rested
                    fam.orders[r.order_id] = FamilyOrder(
                        id=r.order_id, market=market, side="SELL",
                        price=(r.price or px), qty=rested, intent=r.intent,
                        placed_ts=time.time(), purpose="manual",
                        why="the owner's qualify-ask wall")
                else:
                    # never seen resting: it may still land late, so
                    # never re-post for those shares — the next pass
                    # reads the book and works from what is really there
                    zero_streak += 1
                    if zero_streak >= 3:
                        job["stop"] = ("orders are not showing up resting "
                                       "— check the book and tap again")
                        break
                    time.sleep(3.0)
        except Exception as e:  # noqa: BLE001 — a run never kills the app
            job["stop"] = f"{type(e).__name__}: {e}"
        job["state"] = "done"
        fam._log(event="qualify_wall", market=market,
                 qty=round(job["shares"], 1),
                 note=self._qualify_note(job)[:150])
        return job

    def order_op(self, op: str, order_id: str, price: float | None = None,
                 pin: bool = False, qty: float | None = None) -> dict:
        """Owner move/cancel/resize on one of OUR orders, from the
        orders page or the live card. initiator='owner' bypasses the
        switches but no other rail. pin=True (the live card's hand ops)
        marks an engine order hand-set: the engine hands off until the
        release rule or the nurse ends the pin; the hold's baseline is
        measured on the first read AFTER the change (a new price or
        size earns differently than the old one did). Manual orders
        stay manual — already stronger than any pin. An oversized SELL
        is safe by construction: the desk verifies the replacement
        rests at full size and leaves the original untouched if the
        exchange trims it."""
        for fam in self.families.values():
            rec = fam.orders.get(order_id)
            if rec is None:
                continue
            if op == "cancel":
                r = fam.desk.cancel(order_id, rec.market, initiator="owner")
                if r.ok:
                    del fam.orders[order_id]
                return {"ok": r.ok, "note": r.note}
            if op == "move" and (price is not None or qty is not None):
                new_px = float(price) if price is not None else rec.price
                new_q = round(float(qty), 2) if qty is not None else rec.qty
                r = fam.desk.reprice(
                    {"id": rec.id, "market": rec.market, "side": rec.side,
                     "price": rec.price, "size": rec.qty, "intent": rec.intent},
                    new_px, new_q if qty is not None else None,
                    initiator="owner")
                if r.ok:
                    del fam.orders[order_id]
                    from .family import FamilyOrder
                    now = time.time()
                    pinning = pin and rec.purpose != "manual"
                    fam.orders[r.order_id] = FamilyOrder(
                        id=r.order_id, market=rec.market, side=rec.side,
                        price=(r.price or new_px),
                        qty=new_q, intent=rec.intent,
                        placed_ts=now, purpose=rec.purpose,
                        why=("hand-set from the live card — the engine "
                             "holds off" if pinning
                             else "moved by the owner"),
                        pinned=pinning, pin_ts=now if pinning else 0.0,
                        pin_est=-1.0 if pinning else 0.0)
                    if pinning:
                        fam._log(event="hand_set", market=rec.market,
                                 side=rec.side, price=new_px, qty=new_q,
                                 note="the owner changed this order from "
                                      "the live card — the engine holds "
                                      "off until the book turns against it")
                return {"ok": r.ok, "note": r.note}
            return {"ok": False, "note": f"unknown op {op}"}
        return {"ok": False, "note": "not one of 3.0's orders"}

    def close_position(self, market: str) -> dict:
        """The live card's close-out button: sell the open shares at the
        current best bid, never worse — the same carved shape as the
        taker dump, fired by the owner's own tap. Engine exits are
        cancelled first so shares are never offered twice; his own
        hand-placed asks are untouchable, so their shares stay theirs.
        The part the displayed bid can take is journaled as sold; any
        rest stays resting AT the bid as the owner's own order."""
        from .family import FamilyOrder
        from .intents import SELL_LONG
        now = time.time()
        for fam in self.families.values():
            inv = fam.inventory.get(market)
            if inv is None:
                continue
            qty = round(inv.get("qty") or 0.0, 2)
            if qty <= -0.01:
                return {"ok": False,
                        "note": "this position is short — closing it means "
                                "BUYING at the ask, which nothing may cross "
                                "for; rest a bid instead"}
            if qty < 0.01:
                return {"ok": False, "note": "no open position here"}
            try:
                book = self.client.book(market, fetched_at=now)
                fam.cache.put(market, book)
            except Exception as e:  # noqa: BLE001 — fail closed, plainly
                return {"ok": False,
                        "note": f"could not read a fresh book: {e}"}
            if not book.bids:
                return {"ok": False,
                        "note": "no bid resting to sell to right now"}
            bid_px, bid_sz = book.bids[0]
            manual_cover = sum(
                o.qty for o in list(fam.orders.values())
                if o.market == market and o.side == "SELL"
                and o.purpose == "manual")
            sellable = round(qty - manual_cover, 2)
            if sellable < 0.01:
                return {"ok": False,
                        "note": "your own resting asks already cover this "
                                "position — cancel one first if you want "
                                "these shares sold here"}
            # engine exits (hand-set ones included — this tap supersedes
            # the earlier hand move) come off first
            for o in [o for o in list(fam.orders.values())
                      if o.market == market and o.purpose == "sell"
                      and o.side == "SELL"]:
                rr = fam.desk.cancel(o.id, o.market, initiator="owner")
                if rr.ok:
                    fam.orders.pop(o.id, None)
                    fam.evidence.order_gone(o.market, o.id)
            r = fam.desk.place_resting(
                market, "SELL", bid_px, sellable, net_position=qty,
                intent=SELL_LONG, initiator="owner", taker=True,
                verify=False)
            if not r.ok:
                return {"ok": False, "note": r.note}
            # journal what the displayed bid can take NOW (the dump's
            # own convention); the rest is the owner's resting ask and
            # the normal fill watch journals it when it sells
            took = round(min(sellable, bid_sz), 2)
            if took >= 0.01:
                inv["qty"] = round(inv.get("qty", 0.0) - took, 4)
                inv["cost"] = round(inv.get("cost", 0.0) - took * bid_px, 4)
                left = round(inv["qty"], 2)
                if abs(inv["qty"]) < 0.005:
                    fam.inventory.pop(market, None)
                    fam.inv_since.pop(market, None)
                fam._journal_fill(FamilyOrder(
                    id=r.order_id or f"close{int(now)}",
                    market=market, side="SELL", price=bid_px, qty=took,
                    intent=SELL_LONG, placed_ts=now, purpose="sell",
                    why="closed out by the owner from the live card — "
                        "sold into the bid"), took, now, left)
            rest = round(sellable - took, 2)
            if rest >= 0.01 and r.order_id:
                fam.orders[r.order_id] = FamilyOrder(
                    id=r.order_id, market=market, side="SELL",
                    price=bid_px, qty=rest, intent=SELL_LONG,
                    placed_ts=now, purpose="manual",
                    why="the rest of the owner's close-out — resting at "
                        "the bid until it sells")
            fam._log(event="close_out", market=market, price=bid_px,
                     qty=sellable,
                     note=(f"owner closed out — {took:g} sold into the bid"
                           + (f", {rest:g} resting at "
                              f"{bid_px * 100:g}c" if rest >= 0.01 else "")))
            note = f"sold {took:g} at {bid_px * 100:g}c"
            if rest >= 0.01:
                note += (f"; the bid could not take the other {rest:g} — "
                         f"they rest at {bid_px * 100:g}c as your own ask")
            if manual_cover > 0.01:
                note += (f" (your own resting ask for {manual_cover:g} "
                         "was left alone)")
            return {"ok": True, "note": note}
        return {"ok": False, "note": "no position on record for this market"}

    def live_view(self, slug: str) -> dict:
        """One tick of the live card: the book read STRAIGHT from the
        exchange this second — never the stored copy — with our orders
        and the position joined. The fresh read also lands in the cache,
        so the estimates sharpen while the owner watches."""
        fam = None
        for f in self.families.values():
            if slug in f.inventory or any(
                    o.market == slug for o in list(f.orders.values())):
                fam = f
                break
        if fam is None:
            for f in self.families.values():
                if f.knows(slug):
                    fam = f
                    break
        if fam is None:
            return {"ok": False, "note": "no family knows this market"}
        now = time.time()
        book = self.client.book(slug, fetched_at=now)
        fam.cache.put(slug, book)
        # the earnings math, recomputed on THIS second's book (owner,
        # 2026-08-26: "make it so that the earnings math is shown so I
        # get a sense of how much it's earning"): share of the side's
        # score x the side's daily pool, the exchange's own arithmetic
        from .scoring import estimate_join
        prog, prog_why = fam._prog_row(slug)
        side_pool = (fam._side_pool(slug, prog)
                     if prog is not None else None)
        ours = []
        for o in list(fam.orders.values()):
            if o.market != slug:
                continue
            row = {"id": o.id, "side": o.side, "price": o.price,
                   "qty": o.qty, "purpose": o.purpose,
                   "est": o.live_est, "pinned": bool(o.pinned),
                   "share": None, "qualifies": None}
            if prog is not None:
                lv = [(p, q - o.qty if abs(p - o.price) < 1e-9 else q)
                      for p, q in book.side(o.side)]
                lv = [(p, q) for p, q in lv if q > 1e-9]
                j = estimate_join(o.side, lv, book.tick, float(prog.df),
                                  float(prog.target), o.price, o.qty)
                row["share"] = round(j.share, 4)
                row["qualifies"] = bool(j.qualifies and j.in_window)
                if side_pool is not None:
                    row["est"] = round(j.share * side_pool
                                       if row["qualifies"] else 0.0, 4)
            ours.append(row)
        inv = fam.inventory.get(slug)
        # deliberately NO timestamp in the payload: the stream sends only
        # when something CHANGED, so a still book costs the phone nothing
        return {"ok": True, "market": slug,
                "name": self.names.label(slug),
                "tick": book.tick,
                "pool_day": (round(side_pool, 2)
                             if side_pool is not None else None),
                "prog_note": (prog_why if prog is None
                              else ("" if side_pool is not None
                                    else "pool share unconfirmed — no "
                                         "dollar figure until it is")),
                "bids": [[p, q] for p, q in book.bids[:10]],
                "asks": [[p, q] for p, q in book.asks[:10]],
                "ours": ours,
                "position": ({"qty": round(inv.get("qty", 0), 2),
                              "cost": round(inv.get("cost", 0), 2)}
                             if inv else None)}

    def bonds_live(self) -> dict:
        """One tick of the bonds page's live line: the rows of the
        markets he is in, on the books the stream has in the cache."""
        return self.bonds.live_rows(time.time(),
                                    getattr(self, "_bond_positions", None))

    def _kick_tracker(self) -> None:
        """Ask 1.0 (same container) to refresh rewards.csv on GitHub so
        the committed record is current when the push lands. The file
        keeps exactly one writer."""
        pw = os.environ.get("DASH_PASSWORD", "")
        if not pw:
            return
        try:
            import requests
            requests.post(
                f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/track_now",
                json={}, headers={"X-Dash-Key": pw, "X-Reprice": "1"},
                timeout=5)
        except Exception:  # noqa: BLE001 — best effort
            pass

    # -- the repo files 1.0 used to write (ported; single-writer gated) --

    def _gh_file(self, path: str):
        """(text, sha) of a repo file on main, or (None, None)."""
        tok = os.environ.get("GITHUB_TOKEN", "")
        if not tok:
            return None, None
        import requests
        repo = os.environ.get("GITHUB_REPOSITORY", "wfco223/Liquidity-rewards")
        r = requests.get(
            f"https://api.github.com/repos/{repo}/contents/{path}",
            headers={"Authorization": f"Bearer {tok}",
                     "Accept": "application/vnd.github+json"}, timeout=30)
        if r.status_code >= 400:
            return None, None
        j = r.json()
        import base64
        return base64.b64decode(j.get("content") or "").decode(), j.get("sha")

    def _gh_put(self, path: str, text: str, sha, message: str) -> bool:
        tok = os.environ.get("GITHUB_TOKEN", "")
        if not tok:
            return False
        import base64
        import requests
        repo = os.environ.get("GITHUB_REPOSITORY", "wfco223/Liquidity-rewards")
        body = {"message": message,
                "content": base64.b64encode(text.encode()).decode()}
        if sha:
            body["sha"] = sha
        r = requests.put(
            f"https://api.github.com/repos/{repo}/contents/{path}",
            headers={"Authorization": f"Bearer {tok}",
                     "Accept": "application/vnd.github+json"},
            json=body, timeout=30)
        if r.status_code >= 300:
            # a refused publish was silent (2026-09-04: nothing reached
            # the front page for eight hours and no line said why)
            self._note(f"publish {path}: HTTP {r.status_code} "
                       f"{(r.text or '')[:100]}")
        return r.status_code < 300

    def compose_rewards_csv(self, rows: list[dict], existing: str | None) -> str:
        """The exact 1.0 file shape, with history the API no longer serves
        preserved from the existing file."""
        header = "date,market,program_type,reward_usd,status"
        fetched_min = min((r["date"] for r in rows), default="9999")
        keep = []
        for line in (existing or "").splitlines():
            if not line or line.startswith("date,"):
                continue
            if line.split(",", 1)[0] < fetched_min:
                keep.append(line)
        fresh = [f"{r['date']},{r['market']},{r['program_type']},"
                 f"{r['reward_usd']:g},{r['status']}" for r in rows]
        return "\n".join([header] + keep + fresh) + "\n"

    def compose_status_md(self, now: float) -> str:
        import datetime as _dt2
        ts = _dt2.datetime.fromtimestamp(now, _dt2.timezone.utc)
        et = ts.astimezone(ET_STATUS)
        lines = [f"# Liquidity rewards — 3.0",
                 f"",
                 f"✅ Updated {et.strftime('%b %d, %I:%M %p ET')} — the app "
                 f"writes this file every hour.", ""]
        lim = mem_limit_mb()
        lines += [f"Memory: {rss_mb():.0f} MB in use"
                  + (f" of the box's {lim:,.0f} MB" if lim else "")
                  + ". The six-hour fetches are the peaks; discovery "
                  "and the survey refetch now run three hours apart.", ""]
        total_rate = 0.0
        total_today = 0.0
        for key, fam in self.families.items():
            est = self.samplers[key]
            s = (self.last_state.get("summaries") or {}).get(key) or {}
            total_today += est.earned
            rate = est.rate
            total_rate += rate
            lines.append(
                f"- **{fam.cfg.name}**: about ${rate:,.2f}/day resting "
                f"(${est.earned:,.2f} accrued today), "
                f"{len(fam.orders)} orders, "
                f"${fam.family_spent():,.2f} of "
                f"${fam.cfg.capital_usd:,.0f} at risk"
                + (f" — includes holdings worth "
                   f"${fam.holdings_value():,.2f} at liquidation"
                   if fam.cfg.holdings_in_ceiling else "") + ".")
        lines += ["",
                  f"**Whole book: ~${total_rate:,.2f}/day; "
                  f"${total_today:,.2f} accrued today.**", "",
                  "Every number is arithmetic on the exchange's own reward "
                  "terms — no fudge factors. The pages have the detail: "
                  "orders (with plain-English verdicts), the model's moves, "
                  "and grades (estimate vs. what actually paid).", ""]
        return "\n".join(lines)

    RECONCILE_GRACE_S = 1800.0     # a fresh fill's trade may lag the feed
    RECONCILE_EDGE_S = 600.0       # never judge a row at the pull's edge

    def reconcile_journal(self, rows: list, now: float) -> dict:
        """Every hour the exchange's own transaction record is matched
        against the fills journal by ORDER ID and the journal is made to
        agree with it (owner, 2026-09-02: "every so often, just pull the
        new transactions and match them up... the goal is to accurately
        portray fills").

        The Alabama case that asked for this: the owner sold 40 shares
        by hand at 90c; the engine's 98c ask vanished in the same minute
        as the position, and reconcile booked a 98c fill that never
        happened. Three moves, per family, inside the window the pull
        covers:
        - a journal row whose order the exchange shows at another price
          or size takes the exchange's figures;
        - a journal row older than the grace whose order the exchange
          never traded is voided — a cancel, not a fill;
        - executions under order ids the journal lacks are added: the
          engine's own as "backfill" (a missed fill), anyone else's as
          "hand" — the owner's trade — with the side read off the
          position move nearest in time, since the exchange's intent
          labels on a hand sale do not say which way the shares went.
        Inventory is untouched except the cost of a still-held lot whose
        price was corrected; the position feed stays the authority. The
        fills.csv archive is append-only, so every change is also queued
        as a dated correction line rather than rewritten in place."""
        from collections import defaultdict
        trades = [r for r in rows or []
                  if r.get("type") == "ACTIVITY_TYPE_TRADE"
                  and r.get("market") and r.get("order_id")
                  and r.get("price") and r.get("shares") and r.get("ts")]
        if not trades:
            return {"ok": True, "fixed": 0, "voided": 0, "added": 0,
                    "note": "no trades in the pull"}
        oldest = min(r["ts"] for r in trades)
        ex: dict = {}
        for r in trades:
            g = ex.setdefault(r["order_id"], {
                "shares": 0.0, "notional": 0.0, "ts": 0.0,
                "market": r["market"], "side": r.get("side") or "",
                "intent": r.get("intent") or "",
                "placed_ts": r.get("placed_ts")})
            g["shares"] += float(r["shares"])
            g["notional"] += float(r["shares"]) * float(r["price"])
            g["ts"] = max(g["ts"], float(r["ts"]))
        for g in ex.values():
            g["px"] = round(g["notional"] / g["shares"], 4)
        fixed = voided = added = 0
        for tag, fam in self.families.items():
            by_oid: dict = defaultdict(list)
            for row in fam.fills:
                if row.get("oid"):
                    by_oid[row["oid"]].append(row)
            # 1. rows the exchange shows differently
            for oid, jrows in by_oid.items():
                g = ex.get(oid)
                if g is None:
                    continue
                for row in jrows:
                    old = float(row.get("px") or 0.0)
                    if abs(old - g["px"]) > 0.0005:
                        row["px"] = g["px"]
                        row["why"] = (str(row.get("why") or "")
                                      + f" · price corrected to "
                                      f"{g['px'] * 100:g}c from the "
                                      f"exchange's record")
                        inv = fam.inventory.get(row["market"])
                        if inv is not None:
                            sign = 1.0 if row.get("side") == "BUY" else -1.0
                            inv["cost"] = round(
                                inv.get("cost", 0.0)
                                + sign * (g["px"] - old)
                                * float(row.get("qty") or 0.0), 4)
                        fam._log(event="journal_fixed", market=row["market"],
                                 side=row.get("side"), price=g["px"],
                                 qty=row.get("qty"), id=oid,
                                 note=f"was {old * 100:g}c in the journal")
                        self.journal_fixes.append((now, tag, dict(
                            row, purpose="fix",
                            why=f"price corrected {old * 100:g}c -> "
                                f"{g['px'] * 100:g}c per the exchange "
                                f"record (order {oid})")))
                        fixed += 1
                have = sum(float(r.get("qty") or 0.0) for r in jrows)
                over = have - g["shares"]
                if over > 0.005:
                    for row in sorted(jrows, key=lambda r: -(r.get("ts") or 0)):
                        cut = min(over, float(row.get("qty") or 0.0))
                        if cut <= 0.005:
                            continue
                        row["qty"] = round(float(row["qty"]) - cut, 2)
                        over -= cut
                        fam._log(event="journal_fixed", market=row["market"],
                                 side=row.get("side"), price=row.get("px"),
                                 qty=-round(cut, 2), id=oid,
                                 note="size trimmed to the exchange's count")
                        self.journal_fixes.append((now, tag, dict(
                            row, purpose="fix", qty=-round(cut, 2),
                            why=f"size trimmed by {cut:g} to the exchange's "
                                f"count (order {oid})")))
                        fixed += 1
                        if over <= 0.005:
                            break
                    fam.fills[:] = [r for r in fam.fills
                                    if float(r.get("qty") or 0.0) > 0.005]
            # 2. rows the exchange never traded: a cancel, not a fill
            lo_ts = oldest + self.RECONCILE_EDGE_S
            hi_ts = now - self.RECONCILE_GRACE_S
            keep = []
            for row in fam.fills:
                oid = row.get("oid")
                ts = float(row.get("ts") or 0.0)
                if oid and oid not in ex and lo_ts <= ts <= hi_ts:
                    fam._log(event="journal_void", market=row["market"],
                             side=row.get("side"), price=row.get("px"),
                             qty=row.get("qty"), id=oid,
                             note="no exchange trade for this order — "
                                  "it was cancelled, not filled")
                    self.journal_fixes.append((now, tag, dict(
                        row, purpose="void",
                        why=f"voided: the exchange has no trade for order "
                            f"{oid} — the row of "
                            f"{time.strftime('%m-%d %H:%M', time.gmtime(ts))}Z "
                            f"was a cancel, not a fill")))
                    voided += 1
                    continue
                keep.append(row)
            fam.fills[:] = keep
            # 3. executions the journal lacks
            journaled: dict = defaultdict(float)
            for row in fam.fills:
                if row.get("oid"):
                    journaled[row["oid"]] += float(row.get("qty") or 0.0)
            mine = {r.get("market") for r in fam.fills}
            for oid, g in sorted(ex.items(), key=lambda kv: kv[1]["ts"]):
                m = g["market"]
                if not (m in mine or fam.knows(m)):
                    continue
                short = g["shares"] - journaled.get(oid, 0.0)
                if short <= 0.005:
                    continue
                engine = oid in fam.placed_at
                side = g["side"]
                why = ("recovered from the exchange's transaction history "
                       "— this fill was never journaled")
                if not engine:
                    moved = sum(d for t, mk, d in fam.pos_moves
                                if mk == m and g["ts"] - 120.0 <= t
                                <= g["ts"] + 600.0)
                    if abs(moved) > 0.005:
                        side = "BUY" if moved > 0 else "SELL"
                        why = ("your own trade — from the exchange's "
                               "record; direction from the position move")
                    else:
                        why = ("your own trade — from the exchange's "
                               "record; direction from its order intent")
                placed = g.get("placed_ts") or fam.placed_at.get(oid)
                row = {"ts": round(g["ts"], 1), "market": m, "side": side,
                       "qty": round(short, 2), "px": g["px"], "oid": oid,
                       "intent": g.get("intent") or "",
                       "purpose": "backfill" if engine else "hand",
                       "why": why, "est_day": None,
                       "rested_h": (round((g["ts"] - placed) / 3600.0, 2)
                                    if placed and g["ts"] > placed else None),
                       "fair": None, "band": None, "conf": None,
                       "touch_bid": None, "touch_ask": None, "conc": None,
                       "pos_after": None}
                fam.fills.append(row)
                journaled[oid] += short
                fam._log(event="journal_added", market=m, side=side,
                         price=g["px"], qty=round(short, 2), id=oid,
                         note=row["purpose"])
                self.journal_fixes.append((now, tag, dict(row)))
                added += 1
            fam.fills.sort(key=lambda x: x.get("ts") or 0.0)
        del self.journal_fixes[:-400]
        if fixed or voided or added:
            self._note(f"journal reconciled against the exchange record: "
                       f"{fixed} corrected, {voided} voided, {added} added")
            self.freeze_payload()
        return {"ok": True, "fixed": fixed, "voided": voided, "added": added}

    def _known_order_ids(self) -> set:
        """Every order id we know to be ours: resting, ever placed by
        the engine, journaled, or a bond take."""
        ids: set = set()
        for fam in self.families.values():
            ids.update(str(k) for k in fam.orders)
            ids.update(str(k) for k in getattr(fam, "placed_at", {}) or {})
            for f in getattr(fam, "fills", []) or []:
                if isinstance(f, dict) and f.get("oid"):
                    ids.add(str(f["oid"]))
        bonds = getattr(self, "bonds", None)
        if bonds is not None:
            ids.update(str(k) for k in bonds.fill_book)
            for lot in bonds.lots.values():
                ids.update(str(x) for x in (lot.get("fills") or []))
        return ids

    def publish_trades(self, now: float, deep: bool = False) -> dict:
        """The definitive transaction record: the exchange's own
        activity history, published to data/trades.csv (owner,
        2026-08-23: "get the transaction history so we can have a
        definitive record of what is happening"). Deep at boot, a few
        pages hourly after — the file is append-only and deduplicated,
        so overlap costs nothing."""
        try:
            raw = self.client.activities(pages=25 if deep else 3)
        except Exception as e:  # noqa: BLE001 — never breaks the loop
            self._note(f"trades history: {e}")
            return {"ok": False, "note": str(e)[:120]}
        rows = parse_activities(raw, self._known_order_ids())
        self._last_trade_rows = rows      # the hourly reconciliation reads these
        # One-time shape probe: if the exchange's order object already
        # carries a creation time, resting periods come free for
        # history too — no ledger needed for the past. Written once so
        # it can be read rather than guessed at.
        if raw and not getattr(self, "_act_shape_noted", False):
            self._act_shape_noted = True
            try:
                for a in raw:
                    t = (a.get("trade") or {})
                    ex = (t.get("passiveExecution")
                          or t.get("aggressorExecution") or {})
                    o = ex.get("order") or {}
                    if o:
                        self._note("activity order fields: "
                                   + ",".join(sorted(o.keys()))
                                   + " | execution fields: "
                                   + ",".join(sorted(ex.keys())))
                        break
            except Exception:  # noqa: BLE001
                pass
        kinds = {}
        for r in rows:
            kinds[r["type"]] = kinds.get(r["type"], 0) + 1
        try:
            existing, sha = self._gh_file("data/trades.csv")
            text, added = trades_csv_append(existing, rows)
            if added:
                self._gh_put("data/trades.csv", text, sha,
                             f"trade history: +{added} rows [skip ci]")
        except Exception as e:  # noqa: BLE001
            self._note(f"trades publish: {e}")
            return {"ok": False, "note": str(e)[:120]}
        self._note(f"trade history: {len(raw)} activities, {len(rows)} ours, "
                   f"+{added} new rows; kinds={kinds}")
        return {"ok": True, "activities": len(raw), "parsed": len(rows),
                "added": added, "kinds": kinds}

    def backfill_journal(self, days: float = 3.0,
                         dry_run: bool = True) -> dict:
        """Walk the exchange's transaction record against the fills
        journal and add rows for executions the journal never recorded
        (owner, 2026-08-23).

        Matching is by ORDER ID — the exchange's own handle, exact even
        when two orders rest at one price (owner: "keep track of the
        order id in the future so we can match it up"). Journal rows
        written before order ids were recorded carry none, so their
        shares stay available as a per-market/side/price CREDIT that
        each unmatched execution consumes before claiming a shortfall;
        that keeps the transition conservative — it can under-recover,
        never double-count. Recovered rows carry the order id, so the
        next run matches them exactly. Inventory is never touched: the
        exchange's position feed stays the sole authority."""
        from collections import defaultdict
        try:
            raw = self.client.activities(pages=25)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "note": f"history fetch: {str(e)[:120]}"}
        cutoff = time.time() - days * 86400.0
        ex: dict = defaultdict(lambda: {"shares": 0.0, "ts": 0.0,
                                        "market": "", "side": "", "px": 0.0,
                                        "placed_ts": None})
        for r in parse_activities(raw, self._known_order_ids()):
            if r["type"] != "ACTIVITY_TYPE_TRADE":
                continue
            if not (r["market"] and r["side"] and r["price"]
                    and r["shares"] and r["order_id"]):
                continue
            if (r["ts"] or 0) < cutoff:
                continue
            g = ex[r["order_id"]]
            g["shares"] += r["shares"]
            g["ts"] = max(g["ts"], r["ts"])
            g["market"], g["side"] = r["market"], r["side"]
            g["px"] = round(r["price"], 4)
            if r.get("placed_ts"):
                g["placed_ts"] = r["placed_ts"]
        jr_oid: dict = defaultdict(float)
        legacy: dict = defaultdict(float)
        owner_of: dict = {}
        for tag, fam in self.families.items():
            for row in fam.fills:
                if (row.get("ts") or 0) < cutoff:
                    continue
                owner_of[row.get("market")] = tag
                qty = row.get("qty") or 0.0
                oid = row.get("oid")
                if oid:
                    jr_oid[oid] += qty
                else:
                    legacy[(row.get("market"), row.get("side"),
                            round(row.get("px") or 0, 4))] += qty
        added, skipped, rows_out = 0, 0, []
        for oid, g in sorted(ex.items(), key=lambda kv: kv[1]["ts"]):
            short = g["shares"] - jr_oid.get(oid, 0.0)
            if oid not in jr_oid:
                k = (g["market"], g["side"], g["px"])
                take = min(short, legacy.get(k, 0.0))
                if take > 0:
                    legacy[k] -= take
                    short -= take
            if round(short, 4) <= 0.005:
                continue
            tag = owner_of.get(g["market"])
            if tag is None:
                for t2, fam in self.families.items():
                    if fam.knows(g["market"]):
                        tag = t2
                        break
            if tag is None:
                skipped += 1
                continue
            rows_out.append({"family": tag, "market": g["market"],
                             "side": g["side"], "px": g["px"],
                             "qty": short, "ts": g["ts"], "oid": oid,
                             "placed_ts": g.get("placed_ts")})
            added += 1
        fed_odds = [0]
        if not dry_run:
            for r in rows_out:
                fam = self.families[r["family"]]
                fam.fills.append({
                    "ts": round(r["ts"], 1), "market": r["market"],
                    "side": r["side"], "qty": round(r["qty"], 2),
                    "px": r["px"], "oid": r["oid"], "purpose": "backfill",
                    "why": "recovered from the exchange\u2019s transaction "
                           "history \u2014 this fill was never journaled",
                    "est_day": None, "rested_h": None, "fair": None,
                    "band": None, "conf": None, "touch_bid": None,
                    "touch_ask": None, "conc": None,
                    "pos_after": None,
                    # exact resting period when we still know when the
                    # order went on the book (owner, 2026-08-23)
                    "rested_h": (round((r["ts"] - placed) / 3600.0, 2)
                                 if (placed := (r.get("placed_ts")
                                     or fam.placed_at.get(r["oid"])))
                                 and r["ts"] > placed else None)})
                # a recovered fill is real evidence about where this
                # market trades, so it corrects the band the engine
                # prices against (owner approved, 2026-08-23). Its own
                # timestamp carries it: evidence decays on a 36h half
                # life, so an older recovery lands lighter. NOT fed to
                # the fill-odds model — that needs the order's PLACED
                # time, which the exchange record does not carry, and
                # inventing one would poison the odds with fiction.
                fam.evidence.fill(r["market"], r["side"], r["px"],
                                  ts=r["ts"])
                # and the fill-odds model, but ONLY with a real resting
                # period measured from our own placement ledger. No
                # ledger entry means no observation — a guessed resting
                # time would poison the odds that price every order.
                # the exchange's own createTime first — it covers
                # history, which our ledger cannot; the ledger backs it
                placed = r.get("placed_ts") or fam.placed_at.get(r["oid"])
                if placed and r["ts"] > placed:
                    fam.fillmodel.observe_fill_age(r["market"],
                                                   r["ts"] - placed)
                    fed_odds[0] += 1
                fam.fills.sort(key=lambda x: x.get("ts") or 0.0)
            self._note(f"journal backfill: +{added} rows from the exchange "
                       f"record ({days:g} days, matched by order id)")
            self.freeze_payload()
        return {"ok": True, "dry_run": dry_run, "added": added,
                "skipped_unknown_market": skipped, "days": days,
                "shares": round(sum(r["qty"] for r in rows_out), 2),
                "odds_fed": fed_odds[0],
                "sample": [f"{r['market'][:34]} {r['side']} "
                           f"{r['qty']:g}@{r['px']*100:g}c"
                           for r in rows_out[:8]]}

    def _depth_of(self, family: str, market: str) -> int:
        """How many price levels the last book for this market carried.

        There is no cache on the Monitor — each family owns its own, and
        writing self.cache here threw on every publish, so the whole
        per-market ledger stopped being written from the moment the
        depth column was added ("'Monitor' object has no attribute
        'cache'", every hour, swallowed by the ledger's own try/except).
        The share measurement itself was never lost: the estimator banks
        it in state. Only the CSV rows went missing."""
        fam = self.families.get(family)
        cache = getattr(fam, "cache", None) if fam is not None else None
        if cache is None:
            return 0
        return int(getattr(cache, "depth_seen", {}).get(market, 0) or 0)

    def _family_of(self, market: str) -> str:
        """Which family a rewarded market belongs to. The families'
        own universes are the authority — a market can only earn where
        the engine quotes it. Falls back to the prefixes for a market
        that has since left a universe (a settled game, a closed race),
        which is most of the football rows by the time they pay."""
        for key, fam in self.families.items():
            if market in fam.universe:
                return key
        low = market.lower()
        if low.startswith(("tec-nba-", "aqc-nba-", "ftsc-nba-", "fptc-nba-")):
            return "nba"
        if "nfl" in low:
            return "nfl"
        if "cfb" in low or "ncaaf" in low:
            return "cfb"
        return "politics"

    def _feed_check(self, now: float) -> None:
        """The approved live-feed test (owner, 2026-08-25, log-only):
        for a few markets whose cached book was last written by the
        STREAM, fetch the same book fresh over REST and log both side
        by side. If the stream-written picture is consistently thinner,
        the feed is overwriting whole books with partial updates — the
        prime suspect for the inflated share estimates. Changes
        nothing; three extra fetches an hour."""
        done = 0
        for key, fam in self.families.items():
            if done >= 3:
                break
            for slug, w in list(fam.cache.last_writer.items()):
                if done >= 3:
                    break
                if w != "ws":
                    continue
                cached = fam.cache.any_age(slug)
                if cached is None or now - cached.fetched_at > 90:
                    continue
                try:
                    fresh = self.client.book(slug, fetched_at=now)
                except Exception as e:  # noqa: BLE001
                    self._note(f"feed check: {slug[:40]} REST err "
                               f"{str(e)[:50]}")
                    done += 1
                    continue
                def _shape(b):
                    bb = b.bids[0][0] * 100 if b.bids else 0
                    ba = b.asks[0][0] * 100 if b.asks else 0
                    return (f"{len(b.bids)}+{len(b.asks)} lvls "
                            f"{bb:.0f}c/{ba:.0f}c")
                self._note(
                    f"feed check: {slug[:40]}  stream-cache("
                    f"{now - cached.fetched_at:.0f}s old)={_shape(cached)}"
                    f"  fresh-REST={_shape(fresh)}")
                done += 1

    def publish_rewards_csv(self, rows: list | None = None) -> bool:
        """The exchange's posted-payout file, written NOW. Owner,
        2026-08-26: politics Aug-24 posted at 01:13Z, the watcher saw
        it and pushed the phone, but the file sat cfb-only until the
        next hourly batch — "there is info it's just not writing". In
        1.0 the watcher kicked an immediate write; that kick died with
        1.0's retirement. This is the write itself, callable from the
        hourly publish AND from the watcher the moment new postings
        land. One writer per file: skipped while 1.0 runs."""
        if os.environ.get("V1_ENABLED", "0") != "0":
            return False
        if rows is None:
            import datetime as _dt3
            start = (_dt3.datetime.now(_dt3.timezone.utc)
                     - _dt3.timedelta(days=40)).strftime("%Y-%m-%d")
            rows = self.client.earnings(start)
        existing, sha = self._gh_file("data/rewards.csv")
        text = self.compose_rewards_csv(rows, existing)
        if text == existing:
            return True
        ok = self._gh_put("data/rewards.csv", text, sha,
                          "Update rewards.csv [skip ci]")
        if not ok:
            # _gh_put fails silently by design elsewhere; the payout
            # record is too important for that — say it, out loud
            self._note("rewards.csv write failed — retrying on the next "
                       "posting or the hourly publish")
        return ok

    # The surveyor reads books and terms and writes a file. It never
    # places, moves or cancels anything. It runs a few markets per cycle
    # for ever, so the leaderboard keeps improving instead of being one
    # snapshot of whatever the API happened to return first.
    SURVEY_PER_CYCLE = 6            # markets probed per cycle
    SURVEY_TERMS_BATCH = 300        # terms fetched when the frame is refreshed

    def _survey_frame(self, now: float) -> str:
        """Load the population to sample from, refreshed a few times a
        day.

        Asks the exchange for every market on a LIVE liquidity program —
        which is the population that matters, not every market that ever
        carried one. Each row brings its own category, subcategory,
        product and event start time, so the sampler strata are the
        exchange's labels and the live-event filter costs no extra call.
        Falls back to the tags we can name, and SAYS which it got: a
        random draw from a frame we cannot prove is complete is not a
        random draw from the market (owner, 2026-08-31).
        """
        from . import survey as sv
        nxt = survey_frame_due(now, getattr(self, "boot_ts", 0.0),
                               getattr(self, "_survey_frame_at", 0.0))
        if nxt is None:
            return getattr(self, "survey_frame_note", "")
        self._survey_frame_at = nxt
        mem0 = rss_mb()
        # rows arrive already slimmed to the seven fields the sampler
        # reads — never the 4.5 KB raw row (owner, 2026-09-02)
        rows, note = self.client.all_programs(compact=sv.compact_row)
        self._note(f"memory: {mem0:.0f} -> {rss_mb():.0f} MB resident "
                   f"across the survey frame refetch ({len(rows):,} rows)")
        # Why do culture and crypto rows carry category/subcategory while
        # sports rows fall back to the slug? Show one of each rather than
        # guess — the same probe that found orderPriceMinTickSize (owner,
        # 2026-08-31).
        if rows and not getattr(self, "_inc_shape_noted", False):
            self._inc_shape_noted = True
            with_lab = next((r for r in rows if r.get("category")), None)
            without = next((r for r in rows if not r.get("category")), None)
            self._note("incentives row keys: "
                       + ",".join(sorted(rows[0].keys())))
            for tag, r in (("labelled", with_lab), ("unlabelled", without)):
                if not r:
                    self._note(f"incentives {tag}: none in {len(rows)} rows")
                    continue
                self._note(f"incentives {tag}: {r.get('marketSlug')} "
                           f"category={r.get('category')!r} "
                           f"subcategory={r.get('subcategory')!r} "
                           f"product={r.get('instrumentProduct')!r} "
                           f"state={r.get('instrumentState')!r} "
                           f"eventStart={r.get('eventStartTime')!r}")
        kept = [r for r in rows
                if r.get("marketSlug") and not sv.category_banned(r)]
        if kept:
            self.survey_meta = {str(r["marketSlug"]): r for r in kept}
            # How many markets SHARE this pool? A reward pool belongs to
            # the program period, not to one market, so a market in a
            # 30-market event competes for a thirtieth of it. The survey
            # had this hardcoded to 1, overstating every multi-market
            # side by up to 30x (owner, 2026-08-31). The full enumeration
            # makes the real divisor countable: markets carrying the same
            # programId.
            from .programs import pick_period as _pp
            share_n: dict[str, int] = {}
            pid_of: dict[str, str] = {}
            for r in kept:
                per = _pp(r.get("timePeriods") or [], str(r["marketSlug"]))
                pid = str((per or {}).get("programId") or "")
                if pid:
                    pid_of[str(r["marketSlug"])] = pid
                    share_n[pid] = share_n.get(pid, 0) + 1
            self.survey_event_n = {slug: share_n.get(pid, 1)
                                   for slug, pid in pid_of.items()}
            self.survey.load([(str(r["marketSlug"]), sv.group_of(r))
                              for r in kept])
            dropped = len(rows) - len(kept)
            full = note == "enumerated"
            self.survey_frame_note = (
                ("exchange enumeration: " if full else "NOT a full frame — "
                 + note + "; ")
                + f"{len(kept):,} markets on live liquidity programs"
                + (f", {dropped} excluded by category" if dropped else ""))
        else:
            seen: set[str] = set()
            for fam in self.families.values():
                seen.update(fam.universe or {})
            self.survey_meta = {}
            self.survey.load(sorted(seen))
            self.survey_frame_note = (
                f"NOT a full frame — {note}; sampling the "
                f"{len(seen):,} markets our own tags return")
        self._note("survey frame: " + self.survey_frame_note)
        return self.survey_frame_note

    def survey_step(self, now: float) -> dict:
        """One turn of the cycling survey: draw a few markets at random
        within their stratum, skip anything whose event is live or about
        to be, score both sides, and fold the result into the running
        leaderboard."""
        from . import survey as sv
        from .programs import pick_period, program_from_period, with_event_n

        self._survey_frame(now)
        picks = self.survey.next_batch(self.SURVEY_PER_CYCLE)
        if not picks:
            return {"ok": False, "note": "nothing to sample"}
        meta = getattr(self, "survey_meta", {})
        need = [p for p in picks if p not in meta]
        raw = {}
        if need:
            try:
                raw = self.client.programs(need)
            except Exception as e:  # noqa: BLE001 — never breaks a cycle
                return {"ok": False, "note": f"terms: {type(e).__name__}"}
        done = 0
        for slug in picks:
            row = meta.get(slug) or raw.get(slug) or {}
            group = sv.group_of(row) if row else sv.kind_of(slug)
            st = self.survey_stats.setdefault(group,
                                              sv.PrefixStat(prefix=group))
            # owner, 2026-08-31: stay out of live events until he has a
            # way of quoting them. Re-checked every pass — a market that
            # was quiet this morning goes live at kickoff. eventStartTime
            # rides on the incentives row, so this costs nothing.
            if sv.is_live_event(row, now):
                st.live_skipped += 1
                continue
            per = pick_period(row.get("timePeriods") or [], slug)
            if not per:
                continue
            prog = with_event_n(program_from_period(per),
                                getattr(self, "survey_event_n", {}).get(slug, 1))
            if not (prog.pool > 0 and prog.is_live()):
                continue
            try:
                book = self.client.book(slug, fetched_at=now)
            except Exception:  # noqa: BLE001
                continue
            st.markets += 1
            done += 1
            pool_side = (prog.daily_pool / max(prog.event_n, 1)) / 2.0
            for side in ("BUY", "SELL"):
                st.record(sv.probe_side(book, prog, side, pool_side),
                          now, slug)
            self.client._sleep(0.05)
        self.survey_at = now
        return {"ok": True, "probed": done,
                "frame": getattr(self, "survey_frame_note", "")}

    def survey_view(self) -> dict:
        from . import survey as sv
        out = sv.leaderboard(self.survey_stats)
        out["frame"] = getattr(self, "survey_frame_note", "")
        out["sampler"] = self.survey.state()
        out["at"] = round(getattr(self, "survey_at", 0.0), 1)
        return out

    def schedule_cancel(self, match: str, at_ts: float, note: str = "") -> dict:
        """Cancel every resting order whose market contains `match`, at
        `at_ts`. One shot, then it forgets.

        Owner, 2026-09-01, on the Massachusetts primary resolving today:
        "set them to cancel by noon eastern time". This is the ONLY path
        that touches his hand-placed orders — the engine itself never
        does (2026-08-22: "Don't let it cancel orders I set by hand"),
        so it runs as initiator "owner", names him in the audit line,
        and covers exactly the markets he named and no others.
        """
        if not match:
            return {"ok": False, "note": "no market pattern given"}
        self.cancel_jobs = [j for j in getattr(self, "cancel_jobs", [])
                            if j.get("match") != match]
        self.cancel_jobs.append({"match": match, "at": float(at_ts),
                                 "note": note, "set_at": time.time()})
        left = (at_ts - time.time()) / 3600.0
        return {"ok": True, "note": f"scheduled: cancel every resting order "
                f"in markets matching '{match}' in {left:.1f}h"}

    def clear_cancel(self, match: str = "") -> dict:
        jobs = getattr(self, "cancel_jobs", [])
        before = len(jobs)
        self.cancel_jobs = [j for j in jobs
                            if match and j.get("match") != match]
        return {"ok": True, "note": f"cleared {before - len(self.cancel_jobs)} "
                f"scheduled cancel(s)"}

    def _run_due_cancels(self, now: float) -> None:
        """Fire any scheduled cancel that has come due."""
        jobs = getattr(self, "cancel_jobs", None)
        if not jobs:
            return
        for job in list(jobs):
            if now < job.get("at", 0.0):
                continue
            match = str(job.get("match") or "")
            done = failed = 0
            for fam in self.families.values():
                for o in list(fam.orders.values()):
                    if match not in o.market:
                        continue
                    r = fam.desk.cancel(o.id, o.market, initiator="owner")
                    if r.ok:
                        fam.orders.pop(o.id, None)
                        fam.evidence.order_gone(o.market, o.id)
                        done += 1
                    else:
                        failed += 1
            # one shot: gone whether or not every cancel landed, so a
            # market that keeps refusing cannot loop for ever. What is
            # left is reported, and he can schedule another.
            self.cancel_jobs = [j for j in self.cancel_jobs if j is not job]
            line = (f"scheduled cancel fired for '{match}': {done} cancelled"
                    + (f", {failed} refused" if failed else ""))
            self._note(line)
            self.alerts.notify("Scheduled cancel ran", line
                               + (f" — {job['note']}" if job.get("note") else ""))

    def _tick_probe(self) -> None:
        """Ask the exchange what price grid a market actually has, and
        write down what it answers.

        Until 2026-08-31 the tick was only ever INFERRED from the prices
        already resting in a book — "any sub-cent price here means a
        tenth-cent book". That is self-reinforcing: one decimal order
        from any source flips a whole-cent market to a finer grid, and
        the engine then prices there forever. This reads the exchange's
        own market object instead, logs its field names once per family
        so the true name can be read rather than guessed at, and records
        a declared tick only when it is unambiguous.
        """
        for key, fam in self.families.items():
            if key in getattr(self, "_tick_probed", ()):  # once per family
                continue
            slug = next((s for s in fam.terms.current), None)
            if slug is None:
                continue
            self._tick_probed = set(getattr(self, "_tick_probed", ())) | {key}
            md = self.client.market_details(slug)
            if not isinstance(md, dict) or not md:
                self._note(f"tick probe {key}: no market object for {slug}")
                continue
            tick = self.client.declared_tick(md)
            if tick:
                fam.cache.declared[slug] = tick
                self._note(f"tick probe {key}: {slug} declares a "
                           f"{tick * 100:.3g}c grid")
            else:
                # the whole point of the probe: show what IS there, so
                # the field can be named from the record next time
                self._note(f"tick probe {key}: no unambiguous grid on "
                           f"{slug} — market fields: "
                           + ",".join(sorted(md.keys()))[:400])

    def bonds_op(self, op: str, market: str, value=None) -> dict:
        """The owner's taps on the bonds page: add a proposed market,
        ignore it, un-ignore it, remove one from the list, set the
        deploy budget or the price bar. Persisted at once, like a
        switch flip — the list and the money are his, and a restart
        between a tap and the next save must not undo it."""
        market = str(market or "").strip()
        now = time.time()
        if op == "bonds_budget":
            r = self.bonds.set_budget(value)
            market = market or "-"
        elif op == "bonds_budget_tax":
            r = self.bonds.follow_tax()
            market = market or "-"
        elif not market:
            return {"ok": False, "note": "no market given"}
        elif op == "bonds_adopt":
            r = self.bonds.adopt(market, value)
        elif op == "bonds_more_cap":
            r = self.bonds.set_more_cap(market, value)
        elif op == "bonds_exit_at":
            r = self.bonds.set_exit(market, value, now)
        elif op == "bonds_exit_clear":
            r = self.bonds.clear_exit(market)
        elif op == "bonds_buy":
            v = value if isinstance(value, dict) else {}
            r = self.bonds.place_buy(market, v.get("px"), v.get("qty"), now,
                                     getattr(self, "_bond_positions", None))
        elif op == "bonds_pull_buy":
            r = self.bonds.pull_buy(market, str(value or "") or None)
        elif op == "bonds_sell_into":
            v = value if isinstance(value, dict) else {}
            r = self.bonds.sell_into(market, v.get("px"), v.get("qty"), now,
                                     getattr(self, "_bond_positions", None))
        elif op == "bonds_bait":
            r = self.bonds.place_bait(market, now, getattr(self, "_bond_positions", None))
        elif op == "bonds_pull_bait":
            r = self.bonds.pull_bait(market)
        elif op == "bonds_enter":
            # his own purchase: sweep the resting orders out to his price
            r = self.bonds.enter(market, value, now)
        elif op == "bonds_approve":
            r = self.bonds.approve(market, now)
        elif op == "bonds_ignore":
            r = self.bonds.ignore(market, now)
        elif op == "bonds_unignore":
            r = self.bonds.unignore(market)
        elif op == "bonds_remove":
            r = self.bonds.remove(market, now)
        elif op == "bonds_scan":
            new = self.bonds.scan(now, force=True)
            r = {"ok": True, "note": f"scanned — {len(new)} new candidate"
                                     f"{'s' if len(new) != 1 else ''}"}
        else:
            return {"ok": False, "note": f"unknown op {op}"}
        self._audit({"op": op, "market": market, "initiator": "owner",
                     "ok": bool(r.get("ok")), "ts": now})
        if r.get("ok"):
            st = dict(self.last_state) if self.last_state else {}
            st["bonds"] = self.bonds.to_dict()
            st["saved_at"] = now
            self.last_state = st
            self.freeze_payload()
            self.store.save_local(st)
            self.store.save_remote(st)
        return r

    def publish_ladders(self, now: float) -> None:
        """Once a day (owner yes, 2026-09-02): the full ladders of every
        market we are earning in, and of every market that earned in
        the last week, to data/ladders/<day>.json on main."""
        day = ladder_due(now, self.ladder_day)
        if day is None:
            return
        fams: dict = {}
        earning: set = set()
        for key, fam in self.families.items():
            snap, earn = ladder_snapshot(fam, now, extra=self.ladder_seen)
            if snap:
                fams[key] = snap
            earning |= earn
        for s in earning:
            self.ladder_seen[s] = day
        self.ladder_seen = prune_ladder_seen(self.ladder_seen, day)
        n = sum(len(v) for v in fams.values())
        path = f"data/ladders/{day}.json"
        text = json.dumps({"day": day,
                           "taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                     time.gmtime(now)),
                           "families": fams},
                          separators=(",", ":"))
        _old, sha = self._gh_file(path)
        if self._gh_put(path, text, sha, f"ladders: {n} markets [skip ci]"):
            self.ladder_day = day
            self._note(f"ladders: {n} markets across {len(fams)} "
                       f"families -> {path}")
        else:
            self._note(f"ladders: could not write {path} ({n} markets) "
                       f"— trying again next hour")

    @staticmethod
    def fill_calibration_line(key: str, fam, now: float) -> str | None:
        """The fill model's report card, per order type, over the hours
        it actually watched (owner, 2026-09-04). The old note summed the
        odds on whatever rested at that moment and counted every fill
        of the day, bond program included — it read 5x for politics
        while the engine's own orders were 2.7x under and the owner's
        deep bids 2x over. Bond fills are directed by the owner and
        exits are the unwinding working: both shown, neither scored."""
        exp, since = fam.expected_fills_24h(now)
        start = max(now - 86400.0, since)
        hours = max((now - start) / 3600.0, 0.0)
        got: dict = {}
        for f in fam.fills:
            if (f.get("ts") or 0) > start:
                p = f.get("purpose") or ""
                got[p] = got.get(p, 0) + 1
        unscored = ("manual", "hand", "bond", "sell", "probe", "backfill")
        eng_exp = sum(v for p, v in exp.items() if p not in unscored)
        eng_got = sum(v for p, v in got.items() if p not in unscored)
        hand_exp = exp.get("manual", 0.0)
        hand_got = got.get("manual", 0) + got.get("hand", 0)

        def drift(e: float, a: float) -> str:
            # six hours of watching before anything may read as drift:
            # a fresh boot has nothing banked yet
            return ("  <-- DRIFTING" if hours >= 6.0
                    and (a > 2 * e + 2 or e > 2 * a + 2) else "")
        parts = []
        if eng_exp or eng_got:
            parts.append(f"engine orders expected {eng_exp:.1f} fills over "
                         f"the last {hours:.0f}h, got {eng_got}"
                         f"{drift(eng_exp, eng_got)}")
        if hand_exp or hand_got:
            parts.append(f"your hand orders expected {hand_exp:.1f}, got "
                         f"{hand_got}{drift(hand_exp, hand_got)}")
        if got.get("bond"):
            parts.append(f"bond program {got['bond']} (directed by you, "
                         "not scored)")
        if got.get("sell"):
            parts.append(f"exits {got['sell']} (the unwinding working, "
                         "not scored)")
        if got.get("backfill"):
            parts.append(f"{got['backfill']} found only in the record "
                         "(not scored)")
        if not parts:
            return None
        return f"fill calibration {key}: " + "; ".join(parts)

    def publish_files(self, now: float) -> None:
        """Hourly, and only while 1.0 is retired (one writer per file)."""
        if os.environ.get("V1_ENABLED", "0") != "0":
            return
        if now - getattr(self, "_pub_at", 0.0) < 3600.0:
            return
        self._pub_at = now
        lim = mem_limit_mb()
        self._note(f"memory: {rss_mb():.0f} MB resident"
                   + (f" of {lim:,.0f} MB" if lim else ""))
        try:
            # the stream-health line (owner approved 2026-08-26, after
            # the meter sawtooth traced back to the dead feed): is the
            # stream connected, when did it last speak, and how many
            # books did each writer actually put in the last hour
            ws = dict(self.stream.status) if self.stream else {}
            last = ws.get("last_msg") or 0.0
            ago = f"{now - last:.0f}s ago" if last else "never"
            wrote = {"ws": 0, "rest": 0}
            for cache in {id(f.cache): f.cache
                          for f in self.families.values()}.values():
                for w, n in getattr(cache, "writes", {}).items():
                    wrote[w] = wrote.get(w, 0) + n
                cache.writes = {"ws": 0, "rest": 0}
            self._note(
                f"stream health: {ws.get('state', 'off')} · "
                f"{ws.get('subscribed', 0)} subscribed · last message "
                f"{ago} · books written last hour: stream {wrote['ws']}, "
                f"rest {wrote['rest']}")
            # the frame-shape sampler (owner yes, 2026-08-28): what the
            # socket actually delivers, so the dead-writes mystery gets
            # solved from the record instead of guessed at
            # trade-print signal liveness (owner yes, 2026-08-29):
            # markets where the Lite feed's lastTradePx/openInterest
            # moved in the last hour — validated against our own fills
            # before the quiet gate trusts it
            prints = 0
            for cache in {id(f.cache): f.cache
                          for f in self.families.values()}.values():
                prints += sum(1 for ts_l in
                              getattr(cache, "trade_seen", {}).values()
                              if ts_l and ts_l[-1] > now - 3600)
            self._note(f"lite trade prints: {prints} markets saw trades "
                       f"in the last hour")
            shapes = dict(getattr(self.stream, "frame_shapes", {}) or {})
            if shapes:
                top = sorted(shapes.items(), key=lambda kv: -kv[1]["n"])[:4]
                self._note("ws frame shapes: " + " | ".join(
                    f"{sig} x{rec['n']}" for sig, rec in top))
                # one raw sample of the busiest non-lite shape, once an
                # hour, truncated — the evidence apply_frame needs
                for sig, rec in top:
                    if "marketDataLite" not in sig:
                        self._note(f"ws frame sample [{sig}]: {rec['sample']}")
                        break
        except Exception as e:  # noqa: BLE001 — a diagnostic, never a blocker
            self._note(f"stream health line failed: {type(e).__name__}: {e}")
        try:
            self._feed_check(now)
        except Exception as e:  # noqa: BLE001 — a diagnostic, never a blocker
            self._note(f"feed check failed: {type(e).__name__}: {e}")
        try:
            self._tick_probe()
        except Exception as e:  # noqa: BLE001 — a diagnostic, never a blocker
            self._note(f"tick probe failed: {type(e).__name__}: {e}")
        try:
            lb = self.survey_view()
            top = lb["ranked"][:5]
            if top:
                self._note("survey leaderboard: " + " | ".join(
                    f"{r['prefix']} {r['median_spd']:.2f} share%/$ "
                    f"(n={r['n']}, touch {r['median_touch']:,.0f})"
                    for r in top))
            sm = lb["sampler"]
            self._note(f"survey: {sm['population']:,} markets in frame, "
                       f"{sm['prefixes']} strata (biggest {sm['biggest']}, "
                       f"{sm['merged']} merged up, {sm['too_small']} still "
                       f"too small), {len(lb['ranked'])} ranked, "
                       f"{len(lb['sampling'])} sampling")
        except Exception as e:  # noqa: BLE001 — a diagnostic, never a blocker
            self._note(f"survey report failed: {type(e).__name__}: {e}")
        try:
            # FILL-MODEL CALIBRATION, out loud (owner, 2026-08-25: the
            # expected-risk budget leans on these odds, so they are
            # graded hourly): the model's own expected fills per day
            # across the resting book, beside actual fills in the last
            # 24h. Drift past ~2x is the tripwire to raise with the
            # owner.
            for key, fam in self.families.items():
                line = self.fill_calibration_line(key, fam, now)
                if line:
                    self._note(line)
                if line or fam.orders:
                    self._note(
                        f"risk {key}: expected ${fam.family_spent():.2f}"
                        f"/{fam.cfg.capital_usd:.0f}  gross "
                        f"${fam.family_gross():.2f}/{fam.gross_cap():.0f}")
        except Exception as e:  # noqa: BLE001
            self._note(f"fill calibration failed: {type(e).__name__}: {e}")
        try:      # the estimate ledger: every day's prediction, kept
                  # until the exchange settles it (owner, 2026-08-23)
            from .estimator import et_day
            rows = []
            for key, est in self.samplers.items():
                for h in est.history:
                    rows.append((h["day"], key, h.get("earned") or 0.0,
                                 (h.get("stale_s") or 0.0) / 60.0))
                if est.day:
                    rows.append((est.day, key, est.earned,
                                 est.stale_s / 60.0))
            if rows:
                existing, sha = self._gh_file("data/estimates.csv")
                text, n = estimates_csv_append(
                    existing, et_day(now), rows, self.actuals_by_day,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    paid_by_fam={tuple(k.split("|", 1)): v for k, v
                                 in self.actuals_by_fam.items()})
                if n:
                    self._gh_put("data/estimates.csv", text, sha,
                                 f"estimates: {n} rows [skip ci]")
        except Exception as e:  # noqa: BLE001
            self._note(f"estimates ledger: {e}")
        try:      # ...and the same thing per MARKET, so a race can be
                  # graded against its own prediction (2026-08-24)
            from .estimator import et_day
            day = et_day(now)
            per: dict = {}
            for key, fam in self.families.items():
                for o in list(fam.orders.values()):
                    if o.purpose == "manual":   # not our prediction
                        continue
                    a = per.setdefault((day, o.market, key),
                                       {"est": 0.0, "n": 0})
                    a["n"] += 1
            cal = {}
            for key in self.families:
                est = self.samplers.get(key)
                if est is None:
                    continue
                cal.update(est.calibration())
                # est_day_usd is what the market ACCRUED today — the
                # meter's rate integrated over the hours the order was
                # live — not the rate at the moment of writing. The rate
                # made a market whose order rested 12 minutes at $12/day
                # read as a $12 miss (Washington 7.5 wins, 2026-09-01:
                # written $12.58, accrued $0.10, paid $0.07)
                for m, v in est.per_market.items():
                    a = per.setdefault((day, m, key), {"est": 0.0, "n": 0})
                    a["est"] = v
            for (d_, m_, _f), a_ in per.items():
                self.mkt_claim_day[f"{d_}|{m_}"] = round(a_["est"], 4)
            if len(self.mkt_claim_day) > 8000:
                for k in sorted(self.mkt_claim_day)[
                        :len(self.mkt_claim_day) - 8000]:
                    del self.mkt_claim_day[k]
            mrows = [(d, m, f, a["est"], a["n"],
                      (cal.get(m) or {}).get("share", 0.0),
                      (cal.get(m) or {}).get("pool_day", 0.0),
                      (cal.get(m) or {}).get("live_h", 0.0),
                      self._depth_of(f, m))
                     for (d, m, f), a in per.items()]
            if mrows:
                existing, sha = self._gh_file("data/market_est.csv")
                text, n = market_est_append(
                    existing, day, mrows, self.rewards_seen,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)))
                if n:
                    self._gh_put("data/market_est.csv", text, sha,
                                 f"market estimates: {n} rows [skip ci]")
        except Exception as e:  # noqa: BLE001
            self._note(f"market estimate ledger: {e}")
        try:
            self.publish_ladders(now)
        except Exception as e:  # noqa: BLE001 — a record, never a blocker
            self._note(f"ladders: {type(e).__name__}: {e}")
        try:
            self.publish_trades(now, deep=not getattr(self, "_trades_deep",
                                                      False))
            self._trades_deep = True
        except Exception as e:  # noqa: BLE001
            self._note(f"trades: {e}")
        try:      # ...and the journal is made to agree with that record
                  # (owner, 2026-09-02: "pull the new transactions and
                  # match them up")
            self.reconcile_journal(self._last_trade_rows, now)
        except Exception as e:  # noqa: BLE001 — a correction, never a blocker
            self._note(f"journal reconcile: {type(e).__name__}: {e}")
        # one-shot recovery of fills the journal never recorded (owner,
        # 2026-08-23: "Do it"). Runs once, then the flag is persisted;
        # the fills page keeps a button for later runs. Additive and
        # idempotent, so a repeat would be harmless anyway.
        if not self.evidence_seeded:
            # the 554 rows recovered before evidence feeding existed
            n = 0
            try:
                for fam in self.families.values():
                    for row in fam.fills:
                        if row.get("purpose") != "backfill":
                            continue
                        if not (row.get("market") and row.get("side")
                                and row.get("px")):
                            continue
                        fam.evidence.fill(row["market"], row["side"],
                                          row["px"], ts=row.get("ts"))
                        n += 1
                self.evidence_seeded = True
                self._note(f"evidence seeded from {n} recovered fills")
            except Exception as e:  # noqa: BLE001
                self._note(f"evidence seed: {e}")
        if not self.backfilled:
            try:
                r = self.backfill_journal(days=3.0, dry_run=False)
                if r.get("ok"):
                    self.backfilled = True
            except Exception as e:  # noqa: BLE001
                self._note(f"backfill: {e}")
        try:
            self.publish_rewards_csv()
        except Exception as e:  # noqa: BLE001
            self._note(f"rewards.csv publish: {e}")
        try:
            frows = [(r.get("ts", 0.0), tag, r)
                     for tag, fam in self.families.items()
                     for r in fam.fills]
            # corrections ride along as dated lines: the archive is
            # append-only, so a fixed or voided row is never rewritten
            fixes = list(getattr(self, "journal_fixes", []))
            frows += fixes
            if frows:
                existing, sha = self._gh_file("data/fills.csv")
                text, added = fills_csv_append(existing, frows)
                if added:
                    if self._gh_put("data/fills.csv", text, sha,
                                    f"fills archive: +{added} rows [skip ci]"):
                        self.journal_fixes = [
                            f for f in self.journal_fixes if f not in fixes]
                elif fixes:
                    self.journal_fixes = [
                        f for f in self.journal_fixes if f not in fixes]
        except Exception as e:  # noqa: BLE001
            self._note(f"fills.csv publish: {e}")
        try:
            for path, text in (("data/silver_gov_races.csv",
                                getattr(self.silver, "gov_raw", "")),
                               ("data/silver_senate_races.csv",
                                getattr(self.silver, "senate_raw", ""))):
                if not text:
                    continue
                existing, sha = self._gh_file(path)
                if existing is not None and existing != text:
                    self._gh_put(path, text, sha,
                                 "Silver model refresh [skip ci]")
            existing, sha = self._gh_file("STATUS.md")
            text = self.compose_status_md(now)
            if text != existing:
                self._gh_put("STATUS.md", text, sha,
                             "Update STATUS.md [skip ci]")
        except Exception as e:  # noqa: BLE001
            self._note(f"STATUS.md publish: {e}")

    def _posting_progress(self, agg: dict, now: float) -> list[dict]:
        """How much of what we estimated has the exchange posted yet
        (owner, 2026-09-05: "a progress bar filling up as the percentage
        of markets I estimated to be paid appear as pending rows").
        Today and yesterday, ET: the markets whose orders ACCRUED an
        estimate that day, against the market-days the exchange lists
        for it in any status. A row is an appearance whether it reads
        pending, paid or skipped — the bar measures the posting, the
        grades page measures the money."""
        from .estimator import et_day
        out = []
        for back in (0, 1):
            day = et_day(now - back * 86400.0)
            expected = {k.split("|", 1)[1] for k, v in self.mkt_claim_day.items()
                        if k.startswith(day + "|") and (v or 0.0) > 0.005}
            rows = {a["market"]: a for a in agg.values()
                    if a.get("date") == day}
            if not expected and not rows:
                continue
            hit = expected & set(rows)

            def has(m: str, word: str) -> bool:
                return any(word in str(s) for s in rows[m].get("status") or ())
            out.append({
                "day": day, "expected": len(expected), "appeared": len(hit),
                "pct": (round(100.0 * len(hit) / len(expected))
                        if expected else None),
                "pending": sum(1 for m in hit if has(m, "PENDING")),
                "paid": sum(1 for m in hit
                            if has(m, "PAID") and not has(m, "PENDING")),
                "extra": len(set(rows) - expected),
            })
        return out

    def refresh_rewards(self) -> dict:
        """Owner's button: pull the exchange's posted payouts now, show
        what is new since the last look, and fold the day totals into the
        grades page. Reads only — rewards.csv on GitHub stays 1.0's file
        to write."""
        import datetime as _dt
        start = (_dt.datetime.now(_dt.timezone.utc)
                 - _dt.timedelta(days=6)).strftime("%Y-%m-%d")
        rows = self.client.earnings(start)
        first = not self.rewards_seen
        # The exchange splits one market-day into SEVERAL rows (a SKIPPED
        # row and a PAID row of different amounts), and each call returns
        # a different set of ancient strays outside the asked window. So:
        # AGGREGATE per market-day before diffing, remember everything
        # ever seen (never date-pruned, size-capped instead), and only
        # SHOW news from the last few days — older strays are absorbed
        # silently (owner, 2026-08-21: "still off").
        agg: dict[str, dict] = {}
        for r in rows:
            key = f"{r['date']}|{r['market']}"
            a = agg.setdefault(key, {"date": r["date"], "market": r["market"],
                                     "usd": 0.0, "paid": 0.0, "status": set()})
            a["usd"] += r["reward_usd"]
            a["status"].add(r["status"])
            if r["status"] != "SKIPPED":
                a["paid"] += r["reward_usd"]
        try:
            progress = self._posting_progress(agg, time.time())
        except Exception as e:  # noqa: BLE001 — a bar never breaks the check
            self._note(f"posting progress: {e}")
            progress = []
        seen = self.rewards_seen
        fresh = []
        totals: dict[str, float] = {}
        for key, a in agg.items():
            totals[a["date"]] = totals.get(a["date"], 0.0) + a["paid"]
            if abs(seen.get(key, -1.0) - round(a["usd"], 2)) > 0.005:
                fresh.append(a)
            seen[key] = round(a["usd"], 2)
            self.paid_seen[key] = round(a["paid"], 2)
        if len(seen) > 12000:
            for k in sorted(seen)[:len(seen) - 12000]:
                del seen[k]
        if len(self.paid_seen) > 12000:
            for k in sorted(self.paid_seen)[:len(self.paid_seen) - 12000]:
                del self.paid_seen[k]
        self.rewards_seen = seen
        # write the file from HERE, not only from the watcher (owner,
        # 2026-08-31: "make sure rewards.csv is up to date... it's
        # out"). The phone's refresh button routes to this method and
        # CONSUMED the new-postings diff without writing, so the
        # watcher then saw "0 new" and its instant write never fired —
        # tapping refresh actively kept the file stale. Any caller
        # publishes now; the write is a no-op when nothing changed,
        # and these rows spare it a second fetch.
        try:
            self.publish_rewards_csv(rows=rows)
        except Exception as e:  # noqa: BLE001 — reporting never breaks
            self._note(f"rewards.csv publish: {e}")
        for d, v in totals.items():
            self.actuals_by_day[d] = round(v, 2)
        # ...and per family, so the ledger grades each one on its own
        # money. RECOMPUTED and ASSIGNED like the day totals — the old
        # += re-added the same market-days on every 5-minute poll until
        # politics "paid" $36,525 a day (found grading Aug-26; owner
        # yes 2026-08-28)
        fam_totals: dict[str, float] = {}
        for key, a in agg.items():
            fam = self._family_of(a["market"])
            if not fam:
                continue
            fk = f"{a['date']}|{fam}"
            fam_totals[fk] = fam_totals.get(fk, 0.0) + a["paid"]
        for fk, v in fam_totals.items():
            self.actuals_by_fam[fk] = round(v, 2)
        # heal the poisoned history: a day whose family rows sum past
        # its own day total was built by the old accumulator — drop
        # those rows (days in the fetch window were just rewritten
        # correctly; older ones go blank in the ledger, and blank
        # beats wrong)
        by_day_sum: dict[str, float] = {}
        for fk, v in self.actuals_by_fam.items():
            by_day_sum[fk.split("|", 1)[0]] = (
                by_day_sum.get(fk.split("|", 1)[0], 0.0) + v)
        for d, s in by_day_sum.items():
            total = self.actuals_by_day.get(d)
            if total is not None and s > total * 1.01 + 0.01:
                for k in [k for k in self.actuals_by_fam
                          if k.startswith(d + "|")]:
                    del self.actuals_by_fam[k]
        if len(self.actuals_by_fam) > 4000:
            for k in sorted(self.actuals_by_fam)[:len(self.actuals_by_fam) - 4000]:
                del self.actuals_by_fam[k]
        # the baseline must survive a deploy between now and the next save
        # — local AND remote, immediately (a rebuild replaces the disk)
        if self.last_state:
            self.last_state["rewards_seen"] = self.rewards_seen
            self.last_state["actuals_by_day"] = self.actuals_by_day
            self.store.save_local(self.last_state)
            self.store.save_remote(self.last_state)
        days = {d: round(v, 2) for d, v in sorted(totals.items())}
        if first:
            # the FIRST check has nothing to compare against — every row
            # would read "new". Record the baseline and say so plainly.
            latest = max(totals) if totals else "?"
            self._note(f"rewards baseline: {len(rows)} rows through {latest}")
            return {"ok": True, "new_rows": [], "new_count": 0, "days": days,
                    "progress": progress,
                    "note": (f"First check: I recorded a baseline of "
                             f"{len(rows):,} rows through {latest}. From "
                             f"now on this button shows only what is new.")}
        if len(fresh) > max(400, 0.5 * len(rows)):
            # more than half the window "changed" means the baseline was
            # lost (a deploy race), not that thousands of rows posted at
            # once. Re-record it and say so, instead of spamming old rows.
            latest = max(totals) if totals else "?"
            self._note(f"rewards baseline re-recorded ({len(fresh)} rows)")
            return {"ok": True, "new_rows": [], "new_count": 0, "days": days,
                    "progress": progress,
                    "note": (f"The baseline was lost in a restart, so I "
                             f"re-recorded it ({len(rows):,} rows through "
                             f"{latest}). Press again later — only true "
                             f"news will show.")}
        if len(fresh) > max(400, 0.5 * len(agg)):
            # more than half the window "changed" means the memory was
            # lost or its format moved — re-record it, never spam old rows
            latest = max(totals) if totals else "?"
            self._note(f"rewards baseline re-recorded ({len(fresh)} rows)")
            return {"ok": True, "new_rows": [], "new_count": 0, "days": days,
                    "progress": progress,
                    "note": (f"I re-recorded the baseline "
                             f"({len(agg):,} market-days through {latest}). "
                             f"From here only true news shows.")}
        show_from = (_dt.datetime.now(_dt.timezone.utc)
                     - _dt.timedelta(days=4)).strftime("%Y-%m-%d")
        shown = [a for a in fresh if a["date"] >= show_from]
        shown.sort(key=lambda a: (a["date"], a["usd"]), reverse=True)
        out_rows = [{"day": a["date"], "market": a["market"],
                     "name": self.names.label(a["market"]),
                     "usd": round(a["usd"], 2),
                     "status": "/".join(sorted(a["status"]))}
                    for a in shown[:40]]
        self._note(f"rewards check: {len(shown)} new market-days shown, "
                   f"{len(fresh) - len(shown)} old strays absorbed")
        return {"ok": True, "new_rows": out_rows, "new_count": len(shown),
                "days": days, "progress": progress}

    def _lite_study(self) -> dict:
        """Declared-anchor scoring study (owner, 2026-08-21): what each
        of our markets would pay if scoring anchors on the exchange's
        DECLARED best bid/ask instead of the raw touch. Read-only."""
        if self.stream is None:
            return {"note": "no stream"}
        declared = dict(getattr(self.stream, "declared", {}) or {})
        if not declared:
            return {"note": "no lite frames yet"}
        rows: list[dict] = []
        n_cov = n_div = 0
        tot_cur = tot_alt = 0.0
        for tag, fam in self.families.items():
            for slug in {o.market for o in list(fam.orders.values())}:
                d = declared.get(slug)
                if not d:
                    continue
                r = fam.lite_recalc(slug, d[0], d[1])
                if r is None:
                    continue
                n_cov += 1
                div = ((r["bb"] is not None and r["raw_bid"] is not None
                        and abs(r["bb"] - r["raw_bid"]) > 0.005)
                       or (r["ba"] is not None and r["raw_ask"] is not None
                           and abs(r["ba"] - r["raw_ask"]) > 0.005))
                r["diverges"] = div
                r["family"] = tag
                n_div += 1 if div else 0
                tot_cur += r["est_cur"]
                tot_alt += r["est_alt"]
                rows.append(r)
        rows.sort(key=lambda x: -abs(x["est_alt"] - x["est_cur"]))
        return {"covered": n_cov, "divergent": n_div,
                "est_current_total": round(tot_cur, 2),
                "est_declared_total": round(tot_alt, 2),
                "rows": rows[:60]}

    @staticmethod
    def attribute_posted(cards: list, paid_by_md: dict, day_posted: set,
                         claim_by_md: dict) -> dict:
        """The closed-card rewards verification (owner approved
        2026-08-27). The exchange posts ONE number per market-day —
        never per order — so each card's posted rewards are: the
        market-day's real pay x the card's share of what we had
        claimed resting there. The estimator's absolute dollars were
        measured ~2x high, but it only DIVIDES a real number here; its
        relative judgment is all that is used. The denominator is the
        larger of the cards' combined claims and the ledger's whole-day
        claim snapshot (which counts the never-filled orders too), so
        cards can never soak up pay that belonged to other orders.
        Summing every card on a market-day never exceeds what the
        exchange actually paid. Days the exchange has not posted keep
        the claim, reported separately as unposted."""
        import datetime as _dt
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        claims: dict = {}
        out = {i: {"posted": None, "graded": 0.0, "unposted": 0.0}
               for i in range(len(cards))}
        for i, card in enumerate(cards):
            est = card.get("est_day") or 0.0
            rh = card.get("rested_h") or 0.0
            ts = card.get("ts") or 0.0
            m = card.get("market")
            if est <= 0 or rh <= 0 or not m or not ts:
                continue
            t = ts - rh * 3600.0
            while t < ts - 1e-6:
                d = _dt.datetime.fromtimestamp(t, et).date()
                day_end = _dt.datetime.combine(
                    d + _dt.timedelta(days=1), _dt.time(0), et).timestamp()
                seg = min(ts, day_end) - t
                claims.setdefault((d.isoformat(), m), []).append(
                    (i, est * seg / 86400.0))
                t = min(ts, day_end)
        for (di, m), lst in claims.items():
            key = f"{di}|{m}"
            if di in day_posted:
                paid = paid_by_md.get(key, 0.0)
                denom = max(sum(c for _, c in lst),
                            claim_by_md.get(key, 0.0), 1e-9)
                for i, c in lst:
                    o = out[i]
                    o["posted"] = (o["posted"] or 0.0) + paid * c / denom
                    o["graded"] += c
            else:
                for i, c in lst:
                    out[i]["unposted"] += c
        return out

    def fills_view(self) -> dict:
        """Every purchase as a round trip, newest activity first, joined
        with where the market stands now — one report per entry lot,
        updated as its closes land."""
        rows = []
        hidden_open = 0
        hidden_recon = 0
        for tag, fam in self.families.items():
            fam_cards = list(pair_fills(fam.fills))
            # posted-rewards verification: hidden cards stay in the
            # denominator so visible ones cannot soak up their share
            ann = self.attribute_posted(
                fam_cards, self.paid_seen, set(self.actuals_by_day),
                self.mkt_claim_day)
            for idx, card in enumerate(fam_cards):
                a = ann[idx]
                card["posted_usd"] = (round(a["posted"], 4)
                                      if a["posted"] is not None else None)
                card["claim_graded"] = round(a["graded"], 4)
                card["claim_unposted"] = round(a["unposted"], 4)
            for card in fam_cards:
                card["family"] = tag
                card["name"] = self.names.label(card["market"])
                b = fam.cache.any_age(card["market"])
                card["now_bid"] = (b.bids[0][0]
                                   if b is not None and b.bids else None)
                card["now_ask"] = (b.asks[0][0]
                                   if b is not None and b.asks else None)
                inv = fam.inventory.get(card["market"])
                card["pos_now"] = (round(inv.get("qty", 0.0), 2)
                                   if inv else 0.0)
                exits = [o for o in list(fam.orders.values())
                         if o.market == card["market"]
                         and o.purpose == "sell"]
                card["exit_resting"] = bool(exits)
                now = time.time()
                card["exit_rate"] = round(sum((o.live_est or 0.0)
                                              for o in exits), 4)
                card["exit_earned"] = round(sum(
                    (o.live_est or 0.0) * (now - o.placed_ts) / 86400.0
                    for o in exits if o.placed_ts > 0), 4)
                card["net"] = round(card_net(card), 4)
                # Cards the journal never saw closed: the position is
                # flat but the closes are missing, so there is no price
                # and no P&L to show. The owner, 2026-08-23:
                # "essentially useless to me for now" — counted, not
                # listed. The exchange's own record of them lives in
                # data/trades.csv.
                if ((card.get("open_qty") or 0) > 0.005
                        and not card.get("stray_close")
                        and card.get("pos_now") is not None
                        and abs(card["pos_now"]) < 0.005):
                    hidden_recon += 1
                    continue
                if not card_visible(card, now):
                    if card_is_open(card):
                        hidden_open += 1   # open AND profitable — off
                                           # the list, still counted
                    continue
                rows.append(card)
        # Cap each group SEPARATELY. A single rows[:150] after an
        # open-first sort let 150 open cards eat the whole budget and
        # the closed tab came up empty however many real round trips
        # existed (owner, 2026-08-23: "I'm not seeing any").
        def recent(x):
            return -x.get("last_ts", x["ts"])
        is_open = (lambda x: (x.get("open_qty", 0) > 0.005
                              and not x.get("stray_close")))
        opens = sorted([r for r in rows if is_open(r)], key=recent)[:120]
        closes = sorted([r for r in rows if not is_open(r)],
                        key=recent)[:120]
        return {"ok": True, "fills": opens + closes,
                "open_total": sum(1 for r in rows if is_open(r)),
                "closed_total": sum(1 for r in rows if not is_open(r)),
                "open_hidden": hidden_open,
                "hidden_reconciled": hidden_recon,
                "pending": self._pending_fills()}

    def _pending_fills(self) -> list[dict]:
        """Vanished orders waiting for the position feed or the trade
        history to confirm — shown gray in the closed cards (owner,
        2026-08-23: "include the card in the closed section colored
        gray and note it is waiting for position to close out")."""
        from .family import GONE_GRACE_S
        out = []
        for tag, fam in self.families.items():
            for oid, gp in list(fam.gone_pending.items()):
                rec = gp["rec"]
                out.append({"market": rec.market, "family": tag,
                            "name": self.names.label(rec.market),
                            "side": rec.side, "qty": rec.qty,
                            "px": rec.price,
                            "ts": gp["until"] - GONE_GRACE_S})
        out.sort(key=lambda r: -r["ts"])
        return out

    def book_view(self, slug: str) -> dict:
        """The raw shape of one market's book, with our own orders
        marked — the owner looks at the truth himself."""
        for fam in self.families.values():
            b = fam.cache.any_age(slug)
            if b is None:
                continue
            ours = [{"side": o.side, "price": o.price, "qty": o.qty,
                     "purpose": o.purpose, "est": o.live_est,
                     "verdict": o.verdict}
                    for o in list(fam.orders.values()) if o.market == slug]
            inv = fam.inventory.get(slug)
            return {"ok": True, "market": slug,
                    "name": self.names.label(slug),
                    "age_s": round(time.time() - b.fetched_at, 1),
                    "tick": b.tick,
                    "bids": [[p, q] for p, q in b.bids[:12]],
                    "asks": [[p, q] for p, q in b.asks[:12]],
                    "ours": ours,
                    "fair": (self.silver.model_fair(slug)
                             if hasattr(self, "silver") else None),
                    "band": fam._band(slug, b.bids, b.asks, b.tick),
                    "conf": round(fam.evidence.confidence(slug), 3),
                    "position": ({"qty": round(inv.get("qty", 0), 2),
                                  "cost": round(inv.get("cost", 0), 2)}
                                 if inv else None),
                    "ladder": fam.ladder_view(slug)}
        return {"ok": False, "note": "no book cached for this market yet"}

    def public_state(self) -> dict:
        st = dict(self.last_state) if self.last_state else {"saved_at": 0}
        st["switch_view"] = {
            "master": self.master.state(),
            **({"bonds": self.switches["bonds"].state()}
               if "bonds" in self.switches else {}),
            **{k: self._family_switch_state(k) for k in self.families}}
        st["floor"] = self.floor.status()
        st["place_health"] = self.place_health.view()
        return st

    def _family_switch_state(self, key: str) -> dict:
        """The switch page's card for a family: the switch, plus where
        its game window stands and any 'active until' the owner set."""
        from .family import resting_ok
        s = dict(self.switches[key].state())
        fam = self.families[key]
        now = time.time()
        au = float(getattr(fam, "active_until", 0.0) or 0.0)
        s["has_window"] = bool(fam.cfg.rest_from is not None and fam.cfg.rest_until is not None)
        s["resting_now"] = bool(resting_ok(now, fam.cfg))
        s["active_until"] = au if au > now else 0.0
        return s

    # what the phone pages actually read. The old payload shipped the
    # whole state minus fam_* — 1.85 MB a refresh — and worse, the web
    # thread serialized LIVE dicts while the cycle thread mutated them:
    # "dictionary changed size during iteration" dropped the socket and
    # every page read "unreachable" while the app was healthy
    # (2026-08-22 night). The payload is now frozen to bytes at the end
    # of each cycle, on the cycle's own thread, under the cycle's lock.
    PHONE_KEYS = ("owner_fairs",
                  "saved_at", "build", "boot_ts", "errors", "audit",
                  "master_switch", "flatten", "flat_stats", "summaries",
                  "silver", "silver_log", "grades", "paid_total", "ws",
                  "alerts_log", "rewards_last", "floor", "place_health")

    def build_phone_payload(self) -> dict:
        st = self.public_state()
        d = {k: st[k] for k in self.PHONE_KEYS if k in st}
        for k in st:
            if k.startswith("est_") or k.startswith("sw_") \
                    or k == "switch_view":
                d[k] = st[k]
        labels: dict[str, str] = {}
        slugs: set[str] = set()
        for key, s in (st.get("summaries") or {}).items():
            for o in s.get("orders") or []:
                slugs.add(o.get("market") or "")
            for b in s.get("best_idle") or []:
                slugs.add(b.get("market") or "")
            for t in s.get("triage_feed") or []:
                slugs.add(t.get("market") or "")
            slugs.update((s.get("inventory") or {}).keys())
            fam = self.families.get(key)
            if fam is not None:
                d[f"fam_log_{key}"] = [dict(r) for r in fam.log[-80:]]
                for row in d[f"fam_log_{key}"]:
                    mkt = row.get("market")
                    if mkt:
                        slugs.add(mkt)
        for s in slugs:
            if s:
                labels[s] = self.names.label(s)
        d["labels"] = labels
        d["now"] = time.time()
        d["boot"] = dict(self.boot_stage or {})
        # the last market survey's per-kind summary. Copied, not shared,
        # and only the summary — the full rows live in data/survey.csv.
        cj = [dict(j) for j in getattr(self, "cancel_jobs", [])]
        if cj:
            d["cancel_jobs"] = cj
        try:
            d["survey"] = self.survey_view()
        except Exception:  # noqa: BLE001 — the payload never dies for research
            pass
        try:
            d["bonds"] = self.bonds.view(time.time())
            d["est_bonds"] = {"dots": list(self.bonds.dots)}
        except Exception as e:  # noqa: BLE001
            d["bonds"] = {"rows": [], "proposed": [], "error": str(e)[:120]}
        return d

    def boot_payload(self) -> bytes:
        """What /data.json serves before the first cycle has frozen a
        real one (owner, 2026-08-31, after the app booted, served, and
        every page still read "unreachable"): a SAFE snapshot built
        only from scalars this thread owns. The old fallback rebuilt
        the payload from live dicts on the web thread while the cycle
        thread mutated them — the 2026-08-22 race, which returns
        whenever a first cycle runs long. Never touch a live dict
        here."""
        try:
            body = {
                "saved_at": float(getattr(self, "boot_ts", 0.0) or 0.0),
                "build": str(getattr(self, "build", "")),
                "now": time.time(),
                "boot": dict(self.boot_stage or {}),
                "summaries": {}, "labels": {}, "errors": [],
                "switch_view": {
                    "master": self.master.state(),
                    **({"bonds": self.switches["bonds"].state()}
               if "bonds" in self.switches else {}),
            **{k: self.switches[k].state() for k in self.families}},
                "starting": True,
            }
            return json.dumps(body).encode()
        except Exception:  # noqa: BLE001 — a bare page beats a dropped socket
            return b'{"starting": true, "summaries": {}, "labels": {}}'

    def freeze_payload(self) -> None:
        try:
            self.payload_json = json.dumps(
                self.build_phone_payload()).encode()
        except Exception as e:  # noqa: BLE001 — a stale payload beats none
            self._note(f"payload freeze: {e}")

    # -- one poll -----------------------------------------------------------

    def _flatten_pass(self, orders: list[dict], positions: dict) -> dict:
        """Cancel opening orders, a batch per cycle for the rate limiter;
        exits are never touched. Runs only once 1.0/2.0 have stood down.
        In phase two (flatten_done) it turns guard: orders the 3.0
        families own are exempt — they are the rebuild."""
        desk = self.families["politics"].desk
        owned = {oid for fam in self.families.values() for oid in fam.orders}
        done = kept = remaining = 0
        if self.flatten_done:
            # Phase two was a janitor: cancel any open order the families
            # did not own. Since 2026-08-22 an unknown order IS THE
            # OWNER'S OWN ("Don't let it cancel orders I set by hand") —
            # this guard cancelled 964 orders, his hand-placed ones
            # included, racing adoption every cycle. It now only reports.
            kept = sum(1 for o in orders if is_exit_order(o, positions))
            return {"active": True, "phase": "rebuild",
                    "kept_exits": kept, "remaining": 0,
                    "cancelled_now": 0,
                    "cancelled_total": self.flat_stats["cancelled"],
                    "failed_total": self.flat_stats["failed"]}
        for o in orders:
            if not (o.get("id") and o.get("market")):
                continue
            if is_exit_order(o, positions):
                kept += 1
                continue
            if self.flatten_done and o["id"] in owned:
                continue
            if done >= FLATTEN_CANCELS_PER_CYCLE:
                remaining += 1
                continue
            r = desk.cancel(o["id"], o["market"], initiator="flatten")
            if r.ok:
                done += 1
                self.flat_stats["cancelled"] += 1
                for fam in self.families.values():
                    fam.orders.pop(o["id"], None)
            else:
                self.flat_stats["failed"] += 1
            time.sleep(0.2)
        if not self.flatten_done and done == 0 and remaining == 0:
            self.flatten_done = True
            self.alerts.notify(
                "Flat — no spending risk left",
                f"kept {kept} exit orders, cancelled "
                f"{self.flat_stats['cancelled']} opening orders. The $100 "
                f"politics rebuild starts now, guided by what paid best.")
        return {"active": True, "phase": ("rebuild" if self.flatten_done
                                          else "cancelling"),
                "kept_exits": kept, "remaining": remaining,
                "cancelled_now": done,
                "cancelled_total": self.flat_stats["cancelled"],
                "failed_total": self.flat_stats["failed"]}

    def cycle(self, now: float | None = None) -> dict:
        now = now or time.time()
        with self._lock:
            return self._cycle_locked(now)

    def _stage(self, stage: str, pct: int) -> None:
        if not self._first_cycle_done:
            self.boot_stage = {"stage": stage, "pct": pct,
                               "ts": round(time.time(), 1)}
            # printed too, so a boot that dies names its own last step
            # in the container log (owner, 2026-08-31)
            print(f"v3: boot {pct}% — {stage}", flush=True)

    # the first cycle after a restart walks the whole board, and when
    # the exchange is erroring its retry ladders can stretch that past
    # any health check — the app then serves nothing, gets recycled,
    # and boots into the same wedge (owner, 2026-08-31). The boot cycle
    # runs on a smaller board so it FINISHES, freezes a payload, and
    # lets the pages come alive; the next cycle uses the full budgets.
    BOOT_BOOKS = 8
    BOOT_SCAN = 2

    def _cycle_locked(self, now: float) -> dict:
        boot_caps = []
        if not self._first_cycle_done:
            for fam in self.families.values():
                boot_caps.append((fam, fam.cfg.books_per_cycle,
                                  fam.cfg.scan_reserve))
                fam.cfg.books_per_cycle = min(fam.cfg.books_per_cycle,
                                              self.BOOT_BOOKS)
                fam.cfg.scan_reserve = min(fam.cfg.scan_reserve,
                                           self.BOOT_SCAN)
        try:
            return self._cycle_body(now)
        finally:
            for fam, books, scan in boot_caps:
                fam.cfg.books_per_cycle = books
                fam.cfg.scan_reserve = scan

    # A position READ is not the position feed. On 2026-09-04 04:20 ET a
    # read came back missing 114 of 171 held positions, showed them
    # again a minute later, and kept doing it for hours; in between the
    # bond ledger wrote every holding off, pulled every exit and let
    # the engine back in. A read that names fewer markets than the last
    # accepted one is read again; markets still missing keep their
    # last value until they have been missing on POS_GONE_READS reads
    # spanning POS_GONE_S — and even then the transaction record, not
    # the read, is what says where a position went.
    POS_GONE_READS = 3
    POS_GONE_S = 300.0

    def _guard_positions(self, fresh: dict, now: float) -> dict:
        last = getattr(self, "_pos_last", None) or {}
        fresh = dict(fresh or {})
        missing = {m for m, v in last.items()
                   if m not in fresh and abs(float(v[0])) > 0.005}
        if missing:
            try:
                again = dict(self.client.positions_net() or {})
            except ApiError as e:
                self._note(f"positions re-read failed: {e}")
                again = None
            if again is not None:
                still = {m for m in missing if m not in again}
                if len(still) < len(missing):
                    fresh, missing = again, still
        pend = getattr(self, "_pos_missing", None) or {}
        for m in list(pend):
            if m not in missing:
                pend.pop(m, None)
        merged = dict(fresh)
        gone = []
        for m in sorted(missing):
            cnt, since = pend.get(m, (0, now))
            cnt += 1
            pend[m] = (cnt, since)
            if cnt >= self.POS_GONE_READS and now - since >= self.POS_GONE_S:
                gone.append(m)
                pend.pop(m, None)
            else:
                merged[m] = last[m]
        self._pos_missing = pend
        if missing:
            rd = getattr(self.client, "positions_read", None)
            kept = len(missing) - len(gone)
            self._note(f"positions feed short: {len(missing)} of {len(last)} held "
                       f"markets missing (read: {rd}); "
                       + (f"{kept} kept at their last value" if kept else "")
                       + (f"; {len(gone)} missing {self.POS_GONE_READS} reads over "
                          f"{self.POS_GONE_S / 60:.0f} min, now taken as gone: "
                          f"{', '.join(gone[:4])}" if gone else ""))
        self._pos_last = merged
        self._pos_last_at = now
        return merged

    def _cycle_body(self, now: float) -> dict:
        self._stage("checking the floor and switches", 5)
        self.flatten = flatten_active()
        self.floor.write_want(self.master.on or self.flatten)
        self._floor_ok = self.floor.acked(now)
        self._stage("fetching the account's resting orders", 10)
        orders = self.client.open_orders()
        self._stage("fetching positions", 18)
        positions = self._guard_positions(self.client.positions_net(), now)
        self.last_flat = None
        if self.flatten and self._floor_ok:
            self.last_flat = self._flatten_pass(orders, positions)
        trades_by_oid: dict[str, float] = {}
        if any(fam.gone_pending for fam in self.families.values()):
            # vanished orders waiting for confirmation: ask the
            # exchange's own trade history by ORDER ID — the definitive
            # source (owner, 2026-08-23: "is there no way to see
            # transaction history and backfill?" — there is)
            try:
                for a in self.client.recent_trades(limit=50):
                    t = a.get("trade") or {}
                    for exk in ("passiveExecution", "aggressorExecution"):
                        ex = t.get(exk) or {}
                        o = ex.get("order") or {}
                        it = str(o.get("intent") or "")
                        if o.get("id") and it and not it.endswith("UNDEFINED"):
                            try:
                                sh = float(ex.get("lastShares") or 0)
                            except (TypeError, ValueError):
                                sh = 0.0
                            if sh > 0:
                                oid = str(o["id"])
                                trades_by_oid[oid] = (
                                    trades_by_oid.get(oid, 0.0) + sh)
            except Exception as e:  # noqa: BLE001 — history is a bonus
                self._note(f"trade history: {e}")
        try:
            self.silver.refresh(now)     # TTL-gated inside
        except Exception as e:  # noqa: BLE001 — the model never kills the loop
            self._note(f"silver: {e}")
        # the payout watcher (ported from 2.0, owner-approved): every five
        # minutes, diff the exchange's posted rewards and push the phone
        # the moment something new lands
        if now - self._rw_at > 300.0:
            self._rw_at = now
            try:
                res = self.refresh_rewards()
                if res.get("new_count"):
                    self.rw_last = res
                    days = sorted((res.get("days") or {}).items())[-2:]
                    line = ", ".join(f"{d[5:]} ${v:,.2f}" for d, v in days)
                    self.alerts.notify(
                        "Rewards posted",
                        f"{res['new_count']} new rows at the exchange; "
                        f"latest day totals: {line}")
                    # write the file the MOMENT postings land (owner,
                    # 2026-08-26) — while 1.0 runs, it owns the file
                    # and gets the kick instead
                    if os.environ.get("V1_ENABLED", "0") != "0":
                        self._kick_tracker()
                    else:
                        self.publish_rewards_csv()
            except Exception as e:  # noqa: BLE001 — watching never breaks
                self._note(f"rewards watch: {e}")
        # boot has to be cheap enough to FINISH (owner, 2026-08-31). The
        # hourly housekeeping — a 2,500-row trade-history pull and five
        # GitHub file round-trips — is ~30 seconds of the heaviest work
        # this process does, and running it before the first cycle had
        # ever completed left the container being recycled mid-boot, over
        # and over. It runs on the next cycle instead; nothing in it
        # touches orders.
        if self._first_cycle_done:
            self.publish_files(now)
        if now - self._history_at > 6 * 3600.0:
            self._history_at = now
            hist, day_totals, recent = load_history()
            if hist:
                for fam in self.families.values():
                    fam.history = hist
                    fam.recent_paid = recent
            if day_totals:
                self.actuals_by_day = day_totals
        summaries = {}
        fam_pct = {"politics": 25, "cfb": 78, "nfl": 88, "nba": 92,
                   "gameday": 95}
        for key, fam in self.families.items():
            self._stage(f"{fam.cfg.name}: discovering, reading terms, "
                        f"scoring books", fam_pct.get(key, 94))
            if fam.cfg.proven_usd > 0:
                # graduation takes STABILITY and HIGH EARNINGS (owner,
                # 2026-08-22): paid on 3+ of the last 7 days, averaging
                # at least the bar — no reaching back to the old era
                fam.proven = {
                    mkt for mkt, (avg, nd)
                    in getattr(fam, "recent_paid", {}).items()
                    if avg >= fam.cfg.graduate_paid_usd
                    and nd >= fam.cfg.graduate_days}
            on = self.master.on and self.switches[key].on and self._floor_ok
            foreign = {oid for k2, f2 in self.families.items() if k2 != key
                       for oid in f2.orders}
            try:
                exits_only = self.flatten and not self.flatten_done
                summaries[key] = fam.cycle(now, orders, positions,
                                           self.client, on,
                                           foreign_ids=foreign,
                                           exits_only=exits_only,
                                           trades=trades_by_oid)
                summaries[key]["name"] = fam.cfg.name
                est = self.samplers[key]
                summaries[key]["earned_today"] = round(est.earned, 2)
                summaries[key]["est_rate"] = round(est.rate, 2)
                summaries[key]["unmeasured_min"] = round(est.stale_s / 60.0, 1)
                if (self.master.on and self.switches[key].on
                        and not self._floor_ok):
                    summaries[key]["mode"] = "waiting for the floor"
            except ApiError as e:
                self._note(f"{key}: {e}")
                summaries[key] = {"name": fam.cfg.name, "error": str(e)[:120]}
        if any(getattr(fam, "last_discover", 0.0) == now
               for fam in self.families.values()):
            self._note(f"memory: {rss_mb():.0f} MB resident after discovery")
        try:      # the bonds, after the families: count sales, keep every
                  # held bond earning, reinvest — only with its switch on
            on_b = self.master.on and self.switches["bonds"].on and self._floor_ok
            self._bond_positions = dict(positions) if positions else {}
            self.bonds.cycle(now, positions, on_b)
        except Exception as e:  # noqa: BLE001 — never breaks the cycle
            self._note(f"bonds: {type(e).__name__}: {e}")
        try:
            self._run_due_cancels(now)
        except Exception as e:  # noqa: BLE001 — never breaks the cycle
            self._note(f"scheduled cancel: {type(e).__name__}: {e}")
        # the survey rides at the BACK of the cycle, after every family
        # has been managed, and never on the boot cycle — it is research,
        # and the money comes first (owner, 2026-08-31)
        if self._first_cycle_done:
            try:
                self.survey_step(now)
            except Exception as e:  # noqa: BLE001 — research never breaks it
                self._note(f"survey step: {type(e).__name__}: {e}")
        self._stage("first save", 98)
        st = self._state(now, summaries)
        self.last_state = st
        self.freeze_payload()
        self.store.save_local(st)
        self.store.maybe_save_remote(st)
        if not self._first_cycle_done:
            self._first_cycle_done = True
            self.boot_stage = {"stage": "running", "pct": 100,
                               "ts": round(time.time(), 1)}
        return st

    def run(self) -> int:
        from .web import WebServer
        web = WebServer(self)
        web.start()
        threading.Thread(target=self._sampler_loop, daemon=True,
                         name="sampler").start()
        self._note(f"serving on :{web.port}")
        backoff = 5.0
        stream_started = False
        while True:
            t0 = time.time()
            try:
                self.cycle()
                backoff = 5.0
                # the feed pours ~54 book updates a second across 200
                # markets through this one shared CPU. Holding it back
                # until the first cycle has finished stops it competing
                # with boot for the GIL, so the health check still gets
                # answered while the board is being read (owner,
                # 2026-08-31).
                if not stream_started and self.stream is not None:
                    stream_started = True
                    self.stream.start()
            except Exception as e:  # noqa: BLE001 — the loop survives anything
                self._note(f"cycle failed: {type(e).__name__}: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, ERROR_BACKOFF_CAP_S)
            # The sleep between cycles is broken into nurse ticks
            # (owner, 2026-08-25): freshly placed orders are watched
            # every few seconds for jumpers and rushing touches, on
            # THIS thread, so no cancel can race the cycle. With no
            # young orders to watch, nurse() returns immediately and
            # this is an ordinary sleep.
            rem = max(POLL_S - (time.time() - t0), 5.0)
            end_t = time.time() + rem
            while True:
                left = end_t - time.time()
                if left <= 0:
                    break
                time.sleep(min(NURSE_TICK_S, left))
                try:
                    for fam in self.families.values():
                        fam.nurse(time.time(), self.client)
                except Exception as e:  # noqa: BLE001 — never kill the loop
                    self._note(f"nurse: {type(e).__name__}: {e}")


def main() -> int:
    m = Monitor()
    if "--once" in sys.argv:
        st = m.cycle()
        import json
        print(json.dumps({k: v for k, v in st.items()
                          if k in ("summaries", "errors", "build")}, indent=1)[:4000])
        return 0
    return m.run()


if __name__ == "__main__":
    sys.exit(main())
