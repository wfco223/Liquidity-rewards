"""Archive the account's whole transaction record from the exchange.

Owner, 2026-10-04: "Get a record of my transaction from the api going as
far back as you can and store them somewhere where they won't be deleted
or overwritten. If there is already a record in the repo, do not
overwrite any information there."

Read-only: GET requests only. Nothing is placed, moved or cancelled.

What it reads:
  - /v1/portfolio/activities with NO type filter, so every kind of row the
    exchange keeps comes back (trades, settlements, deposits, withdrawals
    and anything else), newest first, followed page by page to the end.
    The cursor is an offset, so a trade landing mid-walk shifts the pages
    by a row: that can only repeat a row, never skip one, and repeats are
    dropped by the row's own id.
  - /v1/incentives/earnings from EARNINGS_START, every page: the reward
    payouts, raw.
  - /v1/portfolio/positions and /v1/account/balances once, so the folder
    also says what was held at the moment of the pull.

What it writes: ONE new folder, data/ledger/<UTC stamp>/, and nothing
else. It refuses to run when that folder exists, and every file in it is
opened with exclusive create, so nothing already in the repo is ever
overwritten (data/trades.csv, fills.csv, rewards.csv, fill_history.txt
and the rest are left exactly as they are). The workflow that runs it
commits only additions inside the new folder and tags the commit.

The raw rows are kept exactly as the exchange sent them, gzipped JSON
lines. The CSVs beside them are a convenience for reading on a phone.

A walk that cannot reach the end still writes what it read, and the
manifest and README say plainly that it is incomplete and why; the
process then exits non-zero so the run shows red.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
TRADE_API = "https://api.polymarket.us"
INCENTIVES_HOSTS = ("https://api.prod.polymarketexchange.com", TRADE_API)
ACTIVITIES_PATH = "/v1/portfolio/activities"
EARNINGS_PATH = "/v1/incentives/earnings"
POSITIONS_PATH = "/v1/portfolio/positions"
BALANCES_PATH = "/v1/account/balances"

PAGE_SIZE = 100          # rows a page; the size every reader here has used
MAX_PAGES = 3000         # 300,000 rows: a bound that fails loudly, never trims
EARN_PAGE_SIZE = 500
FALLBACK_EARNINGS_START = "2026-03-21"   # track_rewards.START_DATE, known good
EARN_MAX_PAGES = 400
CHUNK_ROWS = 2000        # raw rows a .jsonl.gz file (~24 MB before gzip)
PAGE_SLEEP_S = 0.25
TRIES = 6
WAIT_MAX_S = 120.0


class HTTPError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"{path} -> HTTP {status}: {body}")
        self.status = status


def scrubber(*secrets):
    """Text with every key value replaced by ***, for anything that is
    printed or written: a malformed key makes requests quote the header
    value in its error, and an error's words go into the manifest."""
    keys = sorted({k for k in secrets if k and len(k) >= 8}, key=len, reverse=True)

    def scrub(text: str) -> str:
        text = str(text)
        for k in keys:
            text = text.replace(k, "***")
        return text
    return scrub


def make_getter(key_id: str, secret: str, session=None, sleep=time.sleep):
    """A signed GET with its own retries: a 429 waits what the exchange
    names (Retry-After, 20 s when it names none, 120 s at most); a 5xx, a
    dropped or cut-off transfer, or a 200 whose body does not parse backs
    off 2, 4, 8, 16, 32 s. Any other answer (a 4xx) is returned as an
    error at once — it is a real answer. No error carries a key."""
    import requests
    import track_rewards as tr
    s = session or requests.Session()
    scrub = scrubber(key_id, secret)

    def get(host: str, path: str, params: dict) -> dict:
        last = None
        for i in range(TRIES):
            try:
                r = s.get(host + path, params=params, timeout=(10, 60),
                          headers=tr.auth_headers(key_id, secret, "GET", path))
            except requests.RequestException as e:
                last = scrub(f"{type(e).__name__}: {e}")
                if i < TRIES - 1:
                    sleep(2 ** (i + 1))
                continue
            if r.status_code == 429:
                try:
                    wait = float(r.headers.get("Retry-After") or 20)
                except ValueError:
                    wait = 20.0
                last = f"HTTP 429 (Retry-After {wait:g}s)"
                if i < TRIES - 1:
                    sleep(min(max(wait, 1.0), WAIT_MAX_S))
                continue
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}: {' '.join(r.text.split())[:200]}"
                if i < TRIES - 1:
                    sleep(2 ** (i + 1))
                continue
            if r.status_code >= 400:
                raise HTTPError(r.status_code,
                                scrub(" ".join(r.text.split())[:300]), path)
            try:
                return r.json()
            except ValueError as e:          # a body cut short or garbled
                last = f"HTTP {r.status_code} with an unreadable body: {str(e)[:120]}"
                if i < TRIES - 1:
                    sleep(2 ** (i + 1))
        raise RuntimeError(f"{path}: no answer after {TRIES} tries — {last}")
    return get


def _without_market(x):
    """The row less every embedded `market` object. That object is the
    market as it stands at the READ (its updatedAt, ep3SyncedAt and prices
    move all day), not as it stood at the event, so the same settlement
    read twice a minute apart can carry two different ones."""
    if isinstance(x, dict):
        return {k: _without_market(v) for k, v in x.items()
                if not (k == "market" and isinstance(v, dict))}
    if isinstance(x, list):
        return [_without_market(v) for v in x]
    return x


def activity_key(a: dict) -> str:
    """The row's own id: trade.id for a trade, the id of the one object a
    row of another type carries. A row with none — a settlement's
    positionResolution carries marketSlug, side, tradeId, updateTime and
    the positions before and after, but no id — is keyed by a hash of
    everything it says about the event, so two different rows can never
    collapse into one and the same row read twice is one."""
    typ = str(a.get("type") or "")
    for k, v in a.items():
        if k != "type" and isinstance(v, dict) and v.get("id"):
            return f"{typ}|{k}|{v['id']}"
    return "hash|" + hashlib.sha256(
        json.dumps(_without_market(a), sort_keys=True).encode()).hexdigest()


def walk_activities(get, page_size: int = PAGE_SIZE, max_pages: int = MAX_PAGES,
                    sleep=time.sleep, pause: float = PAGE_SLEEP_S):
    """Every activity row, newest first, to the end. Returns (rows, info);
    info["complete"] is True only when the exchange said eof or handed
    back no cursor with rows, and says why it stopped otherwise."""
    rows: list[dict] = []
    seen: set[str] = set()
    info = {"pages": 0, "raw_rows": 0, "duplicates": 0, "complete": False,
            "stopped": "", "eof_flag": None, "page_size": page_size}
    cursor = None
    seen_cursors: set[str] = set()
    try:
        for _ in range(max_pages):
            params: dict = {"limit": page_size,
                            "sortOrder": "SORT_ORDER_DESCENDING"}
            if cursor:
                params["cursor"] = cursor
            j = get(TRADE_API, ACTIVITIES_PATH, params)
            info["pages"] += 1
            batch = j.get("activities") or []
            info["raw_rows"] += len(batch)
            for a in batch:
                k = activity_key(a)
                if k in seen:
                    info["duplicates"] += 1
                    continue
                seen.add(k)
                rows.append(a)
            nxt = j.get("nextCursor")
            if j.get("eof"):
                info.update(complete=True, eof_flag=True,
                            stopped="the exchange said eof")
                break
            if not batch:
                info.update(complete=True, eof_flag=bool(j.get("eof")),
                            stopped="an empty page")
                break
            if not nxt:
                info.update(complete=True, eof_flag=bool(j.get("eof")),
                            stopped="no further cursor")
                break
            if nxt in seen_cursors:
                info["stopped"] = "the exchange handed back a cursor it had already given"
                break
            seen_cursors.add(nxt)
            cursor = nxt
            sleep(pause)
        else:
            info["stopped"] = (f"still more pages after {max_pages} — "
                               f"raise MAX_PAGES and run again")
    except Exception as e:  # noqa: BLE001 — keep what was read, say why
        info["stopped"] = f"error on page {info['pages'] + 1}: {str(e)[:300]}"
    return rows, info


def walk_earnings(get, start: str, hosts=INCENTIVES_HOSTS,
                  page_size: int = EARN_PAGE_SIZE, max_pages: int = EARN_MAX_PAGES,
                  sleep=time.sleep, pause: float = PAGE_SLEEP_S):
    """Every reward payout row from `start`, raw. Each host is walked from
    its first page on its own, so two hosts' rows never mix; the first
    host to answer all the way is kept, and a host that fails part way
    hands over to the next. When none completes, the longest partial walk
    is kept and marked incomplete."""
    errors: list[str] = []
    best = None
    for host in hosts:
        rows: list[dict] = []
        info = {"host": host, "start": start, "pages": 0, "complete": False,
                "stopped": ""}
        params: dict = {"startDate": start, "pageSize": page_size}
        try:
            for _ in range(max_pages):
                j = get(host, EARNINGS_PATH, params)
                info["pages"] += 1
                rows.extend(j.get("rewards") or [])
                tok = j.get("nextPageToken")
                if not tok:
                    info.update(complete=True, stopped="no further page token")
                    break
                if tok == params.get("pageToken"):
                    info["stopped"] = "the exchange handed back the same page token"
                    break
                params["pageToken"] = tok
                sleep(pause)
            else:
                info["stopped"] = f"still more pages after {max_pages}"
        except Exception as e:  # noqa: BLE001
            info["stopped"] = f"error on page {info['pages'] + 1}: {str(e)[:300]}"
        if info["complete"]:
            info["errors_before"] = errors
            return rows, info
        errors.append(f"{host}: {info['stopped']} ({len(rows)} rows)")
        if rows and (best is None or len(rows) > len(best[0])):
            best = (rows, info)
    if best:
        best[1]["errors_before"] = errors
        return best
    return [], {"host": None, "start": start, "pages": 0, "complete": False,
                "stopped": "every host refused", "errors_before": errors}


def snapshot(get) -> dict:
    """What was held and the balance rows at the moment of the pull."""
    out: dict = {"positions": {}, "positions_read": {}, "balances": None,
                 "errors": [], "complete": False}
    ended = False
    try:
        cursor, pages, eof = None, 0, False
        for _ in range(50):
            params: dict = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            j = get(TRADE_API, POSITIONS_PATH, params)
            pages += 1
            out["positions"].update(j.get("positions") or {})
            cursor = j.get("nextCursor")
            if j.get("eof") or not cursor:
                eof, ended = bool(j.get("eof")), True
                break
        out["positions_read"] = {"pages": pages, "n": len(out["positions"]),
                                 "eof": eof, "ended": ended}
        if not ended:
            out["errors"].append("positions: still more pages after 50")
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"positions: {str(e)[:300]}")
    try:
        out["balances"] = get(TRADE_API, BALANCES_PATH, {})
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"balances: {str(e)[:300]}")
    out["complete"] = ended and not out["errors"]
    return out


# -- the readable tables ---------------------------------------------------

def _val(x):
    """A number the exchange nests as {"value": ...} or sends bare."""
    if isinstance(x, dict):
        x = x.get("value")
    if x is None or x == "":
        return ""
    try:
        return float(x)
    except (TypeError, ValueError):
        return str(x)


def _time_of(a: dict) -> str:
    t = a.get("trade") or {}
    if t:
        for ex in ("aggressorExecution", "passiveExecution"):
            s = (t.get(ex) or {}).get("transactTime")
            if s:
                return str(s)
        return str(t.get("updateTime") or t.get("createTime") or "")
    for k, v in a.items():
        if k != "type" and isinstance(v, dict):
            for f in ("transactTime", "updateTime", "createTime", "time",
                      "timestamp", "createdAt"):
                if v.get(f):
                    return str(v[f])
    return ""


def _et(iso: str) -> str:
    if not iso:
        return ""
    try:
        s = iso.replace("Z", "+00:00")
        # the exchange sends nanoseconds; Python takes six digits
        if "." in s:
            head, tail = s.split(".", 1)
            frac = "".join(ch for ch in tail if ch.isdigit())
            rest = tail[len(frac):]
            s = f"{head}.{frac[:6]}{rest}"
        return dt.datetime.fromisoformat(s).astimezone(ET).strftime(
            "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""


ACT_COLS = ["time_utc", "time_et", "type", "id", "market", "title", "outcome",
            "our_side", "intent", "order_side", "price", "shares", "cost",
            "cost_basis", "realized_pnl", "commission", "order_id",
            "order_placed_utc", "order_state", "manual", "amount", "detail"]


def activity_row(a: dict) -> dict:
    """One readable line for one activity. For a trade, OUR execution is
    the one the trade's isAggressor names: in every row of the 2026-08-14
    probe where only one side carried a real intent (the exchange redacts
    the counterparty's), isAggressor pointed at that side — 18 of 18."""
    typ = str(a.get("type") or "")
    when = _time_of(a)
    row = {c: "" for c in ACT_COLS}
    row.update(time_utc=when, time_et=_et(when), type=typ)
    t = a.get("trade") or {}
    if t:
        role = "aggressor" if t.get("isAggressor") else "passive"
        ex = t.get(f"{role}Execution") or {}
        o = ex.get("order") or {}
        meta = o.get("marketMetadata") or {}
        mkt = t.get("market") or {}
        row.update(
            id=t.get("id") or "", market=t.get("marketSlug") or "",
            title=mkt.get("question") or meta.get("title") or "",
            outcome=(mkt.get("title") or meta.get("outcome") or ""),
            our_side=role, intent=o.get("intent") or "",
            order_side=o.get("side") or "",
            price=_val(ex.get("lastPx")), shares=_val(ex.get("lastShares")),
            cost=_val(t.get("cost")), cost_basis=_val(t.get("costBasis")),
            realized_pnl=_val(t.get("realizedPnl")),
            commission=_val(ex.get("commissionNotionalCollected")),
            order_id=o.get("id") or "",
            order_placed_utc=o.get("createTime") or o.get("insertTime") or "",
            order_state=o.get("state") or "",
            manual=o.get("manualOrderIndicator") or "")
        return row
    body = {k: v for k, v in a.items() if k != "type"}
    sub = next((v for v in body.values() if isinstance(v, dict)), {}) or {}
    row["id"] = sub.get("id") or ""
    m = sub.get("market")
    row["market"] = sub.get("marketSlug") or (m.get("slug") or ""
                                              if isinstance(m, dict) else "")
    for f in ("amount", "value", "cashAmount", "netAmount", "payout"):
        if f in sub:
            row["amount"] = _val(sub[f])
            break
    # the whole row minus the bulky market description, which the raw
    # files keep
    lean = json.loads(json.dumps(body))
    for v in lean.values():
        if isinstance(v, dict) and isinstance(v.get("market"), dict):
            v["market"] = {k: v["market"].get(k)
                           for k in ("slug", "question", "title") if k in v["market"]}
    row["detail"] = json.dumps(lean, separators=(",", ":"), sort_keys=True)
    return row


EARN_COLS = ["date", "market", "program_type", "reward_usd", "status", "other"]


def earning_row(r: dict) -> dict:
    known = {"date", "marketSlug", "programType", "reward", "status"}
    other = {k: v for k, v in r.items() if k not in known}
    return {"date": str(r.get("date") or "")[:10], "market": r.get("marketSlug") or "",
            "program_type": r.get("programType") or "",
            "reward_usd": _val(r.get("reward")), "status": r.get("status") or "",
            "other": json.dumps(other, separators=(",", ":"), sort_keys=True)
            if other else ""}


# -- writing, once ---------------------------------------------------------

def _xwrite(path: Path, data: bytes) -> dict:
    """Exclusive create: an existing file is an error, never overwritten."""
    with open(path, "xb") as f:
        f.write(data)
    return {"file": path.name, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def _gz_lines(rows) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as g:
        for r in rows:
            g.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False).encode())
            g.write(b"\n")
    return buf.getvalue()


def _csv(cols, rows) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


def write_archive(root: Path, stamp: str, acts: list, act_info: dict,
                  earn: list, earn_info: dict, snap: dict, meta: dict,
                  scrub=str) -> Path:
    out = Path(root) / "data" / "ledger" / stamp
    if out.exists():
        raise FileExistsError(f"{out} already exists — refusing to write over it")
    out.mkdir(parents=True, exist_ok=False)
    files = []
    chunks = [acts[i:i + CHUNK_ROWS] for i in range(0, len(acts), CHUNK_ROWS)] or [[]]
    for n, chunk in enumerate(chunks, 1):
        files.append(_xwrite(out / f"activities-{n:04d}.jsonl.gz", _gz_lines(chunk))
                     | {"rows": len(chunk)})
    readable = sorted((activity_row(a) for a in acts), key=lambda r: r["time_utc"])
    files.append(_xwrite(out / "activities.csv", _csv(ACT_COLS, readable))
                 | {"rows": len(readable)})
    files.append(_xwrite(out / "earnings.jsonl.gz", _gz_lines(earn)) | {"rows": len(earn)})
    erows = sorted((earning_row(r) for r in earn), key=lambda r: (r["date"], r["market"]))
    files.append(_xwrite(out / "earnings.csv", _csv(EARN_COLS, erows)) | {"rows": len(erows)})
    files.append(_xwrite(out / "positions.json.gz", _gz_lines([snap.get("positions") or {}])))
    files.append(_xwrite(out / "balances.json",
                         json.dumps(snap.get("balances"), indent=1).encode()))

    by_type: dict = {}
    for a in acts:
        k = str(a.get("type") or "?")
        by_type[k] = by_type.get(k, 0) + 1
    times = sorted(r["time_utc"] for r in readable if r["time_utc"])
    dates = sorted(r["date"] for r in erows if r["date"])
    paid = round(sum(r["reward_usd"] for r in erows
                     if isinstance(r["reward_usd"], float)
                     and str(r["status"]).upper() == "PAID"), 2)
    manifest = {
        **meta,
        "activities": {**act_info, "rows": len(acts), "by_type": by_type,
                       "first_utc": times[0] if times else None,
                       "last_utc": times[-1] if times else None},
        "earnings": {**earn_info, "rows": len(earn),
                     "first_date": dates[0] if dates else None,
                     "last_date": dates[-1] if dates else None,
                     "paid_usd": paid},
        "snapshot": {"positions_read": snap.get("positions_read"),
                     "complete": bool(snap.get("complete")),
                     "errors": snap.get("errors")},
        "files": files,
    }
    files_ = list(files)
    manifest_bytes = scrub(json.dumps(manifest, indent=1, sort_keys=False)).encode()
    _xwrite(out / "manifest.json", manifest_bytes)
    _xwrite(out / "README.md", scrub(readme(stamp, manifest, files_)).encode())
    return out


def readme(stamp: str, m: dict, files: list) -> str:
    a, e, sn = m["activities"], m["earnings"], m["snapshot"]
    ok = a["complete"] and e["complete"] and sn.get("complete")
    lines = [
        f"# Transaction record, pulled {m['pulled_utc']}",
        "",
        ("✅ Complete: both walks reached the end of the exchange's record."
         if ok else
         "❌ INCOMPLETE — read the \"stopped\" lines and the snapshot below "
         "before relying on this."),
        "",
        "Pulled from the exchange's own API by a read-only run. Nothing in this "
        "folder is ever edited after it is written; a later pull goes in a new "
        f"folder, and the commit is tagged `ledger-{stamp}`.",
        "",
        "## Activities (trades, settlements, cash moves — every type)",
        f"- {a['rows']:,} rows, {a['first_utc'] or '—'} to {a['last_utc'] or '—'} (UTC)",
        f"- by type: " + (", ".join(f"{k} {v:,}" for k, v in sorted(a["by_type"].items()))
                          or "none"),
        f"- {a['pages']} pages read; {a['duplicates']} repeated rows dropped by id",
        f"- stopped: {a['stopped']}",
        "",
        "## Reward payouts",
        f"- {e['rows']:,} rows, {e['first_date'] or '—'} to {e['last_date'] or '—'}; "
        f"PAID rows add up to ${e['paid_usd']:,.2f}",
        f"- asked from {e['start']} on {e['host'] or 'no host'}; {e['pages']} pages",
        f"- stopped: {e['stopped']}",
        "",
        "## What was held at the pull",
        f"- positions: {(sn.get('positions_read') or {}).get('n', '—')} markets "
        f"read; balances: {'read' if not any(str(x).startswith('balances') for x in sn.get('errors') or []) else 'NOT read'}",
        *[f"- ❌ {x}" for x in sn.get("errors") or []],
        "",
        "## Files",
        "- `activities-NNNN.jsonl.gz`: every activity row exactly as the exchange "
        "sent it, newest first, 2,000 to a file",
        "- `activities.csv`: one readable line per activity, oldest first. For a "
        "trade, `our_side` is the side the exchange's isAggressor flag names; "
        "`price` and `shares` are that side's execution",
        "- `earnings.jsonl.gz` / `earnings.csv`: reward payout rows",
        "- `positions.json.gz`, `balances.json`: what was held at the pull",
        "- `manifest.json`: counts, ranges, why each walk stopped, and a sha256 "
        "of every file",
        "",
        "| file | rows | bytes |",
        "|---|---:|---:|",
    ]
    for f in files:
        lines.append(f"| {f['file']} | {f.get('rows', '')} | {f['bytes']:,} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    kid = os.environ.get("POLYMARKET_KEY_ID", "").strip()
    sec = os.environ.get("POLYMARKET_SECRET_KEY", "").strip()
    if not kid or not sec:
        print("no exchange key in the environment — nothing read")
        return 1
    start = os.environ.get("EARNINGS_START") or "2025-01-01"
    max_pages = int(os.environ.get("MAX_PAGES") or MAX_PAGES)
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H%MZ")
    root = Path(os.environ.get("ARCHIVE_ROOT") or ".")
    if (root / "data" / "ledger" / stamp).exists():
        print(f"data/ledger/{stamp} already exists — refusing")
        return 1
    get = make_getter(kid, sec)
    scrub = scrubber(kid, sec)

    def say(text: str) -> None:
        print(scrub(text))
    t0 = time.time()
    acts, ainfo = walk_activities(get, max_pages=max_pages)
    say(f"activities: {len(acts):,} rows, {ainfo['pages']} pages, "
          f"{ainfo['duplicates']} repeats dropped — stopped: {ainfo['stopped']}")
    # an early start the exchange refuses must not cost the payouts: fall
    # back to the date the pay reader has always asked from
    tried = []
    for st in dict.fromkeys([start, FALLBACK_EARNINGS_START]):
        earn, einfo = walk_earnings(get, st)
        tried.append({"start": st, "rows": len(earn), "stopped": einfo["stopped"]})
        if earn or einfo["complete"]:
            break
    einfo["starts_tried"] = tried
    say(f"earnings: {len(earn):,} rows from {einfo['host']} — stopped: {einfo['stopped']}")
    for err in einfo.get("errors_before") or []:
        say(f"  earlier host: {err}")
    snap = snapshot(get)
    say(f"positions: {snap['positions_read']}; errors: {snap['errors']}")
    meta = {"pulled_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "stamp": stamp,
            "seconds": round(time.time() - t0, 1),
            "run": os.environ.get("RUN_URL", ""),
            "reads": {"activities": f"GET {TRADE_API}{ACTIVITIES_PATH} limit={PAGE_SIZE} "
                                    "sortOrder=SORT_ORDER_DESCENDING, no type filter",
                      "earnings": f"GET {EARNINGS_PATH} startDate={start} "
                                  f"pageSize={EARN_PAGE_SIZE}"}}
    if not acts and not earn:
        # every read refused (a key gone bad, the runner's address turned
        # away): a folder of nothing, tagged for good, would only say so
        # forever. Say it here and write nothing.
        say("nothing was read — no folder written")
        return 1
    out = write_archive(root, stamp, acts, ainfo, earn, einfo, snap, meta, scrub)
    say(f"wrote {out}")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"stamp={stamp}\n")
    return 0 if (ainfo["complete"] and einfo["complete"] and snap["complete"]) else 2


if __name__ == "__main__":
    sys.exit(main())
