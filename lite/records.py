"""What the simple app keeps writing (owner, 2026-10-04: "Keep the ntfy
for new rewards rows and writing to rewards and trades. You can stop
writing to status."): the push when new reward rows land at the exchange,
data/rewards.csv and data/trades.csv on main.

Two defects of 3.0's writers are not carried over:

- A file past 1 MiB read back EMPTY through the contents API's JSON
  answer (GitHub drops the content and keeps the sha), and the next write
  replaced the whole history with the new rows: data/trades.csv went from
  1,048,584 bytes to 87,391 at 20:51Z on 2026-09-26. Repo.read takes the
  text RAW (good to 100 MB), checks its length against the size GitHub
  reports, and raises rather than hand back a short file.
- compose_rewards_csv cut the old lines at the earliest FETCHED date, so
  one stray row from months back (the exchange returns some outside the
  asked window) dropped every line after it. The cut is now the asked
  start date, and strays before it are ignored.
"""

from __future__ import annotations

import base64
import datetime as dt
import os

import requests

GH_API = "https://api.github.com"
REWARDS_PATH = "data/rewards.csv"
TRADES_PATH = "data/trades.csv"
REWARDS_HEADER = "date,market,program_type,reward_usd,status"
SEEN_CAP = 12000            # market-days remembered for the new-rows diff
SHOW_DAYS = 4               # a new row older than this is absorbed silently


class Repo:
    """data files on main through the contents API."""

    def __init__(self, repo: str | None = None, token: str | None = None,
                 session=None):
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY", "wfco223/Liquidity-rewards")
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self.s = session or requests.Session()

    def _url(self, path: str) -> str:
        return f"{GH_API}/repos/{self.repo}/contents/{path}"

    def _hdr(self, accept: str) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": accept}

    def read(self, path: str) -> tuple[str, str | None]:
        """(text, sha). A file that does not exist reads ("", None). Any
        other failure raises: a writer must never mistake "could not read"
        for "empty"."""
        if not self.token:
            raise RuntimeError("no GITHUB_TOKEN")
        r = self.s.get(self._url(path), headers=self._hdr("application/vnd.github+json"),
                       timeout=30)
        if r.status_code == 404:
            return "", None
        if r.status_code >= 400:
            raise RuntimeError(f"read {path}: HTTP {r.status_code}")
        meta = r.json()
        sha, size = meta.get("sha"), int(meta.get("size") or 0)
        r2 = self.s.get(self._url(path), headers=self._hdr("application/vnd.github.raw+json"),
                        timeout=60)
        if r2.status_code >= 400:
            raise RuntimeError(f"read {path} (raw): HTTP {r2.status_code}")
        raw = r2.content
        if len(raw) != size:
            raise RuntimeError(f"read {path}: got {len(raw):,} bytes of {size:,}")
        return raw.decode(), sha

    def write(self, path: str, text: str, sha: str | None, message: str) -> bool:
        if not self.token:
            return False
        body = {"message": message, "content": base64.b64encode(text.encode()).decode()}
        if sha:
            body["sha"] = sha
        r = self.s.put(self._url(path), headers=self._hdr("application/vnd.github+json"),
                       json=body, timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"write {path}: HTTP {r.status_code} {(r.text or '')[:100]}")
        return True


# -- rewards ----------------------------------------------------------------

def compose_rewards_csv(rows: list[dict], existing: str, start: str) -> str:
    """The file 1.0 and 3.0 wrote: every old line dated before `start`
    kept as it is, then the fetched rows from `start` on. Rows the
    exchange returns from before `start` (it does) are ignored — the
    file already has those days."""
    keep = [ln for ln in (existing or "").splitlines()
            if ln and not ln.startswith("date,") and ln.split(",", 1)[0] < start]
    fresh = sorted((r for r in rows if r["date"] >= start),
                   key=lambda r: (r["date"], r["market"], r["program_type"]))
    lines = [f"{r['date']},{r['market']},{r['program_type']},"
             f"{r['reward_usd']:g},{r['status']}" for r in fresh]
    return "\n".join([REWARDS_HEADER] + keep + lines) + "\n"


def write_rewards(repo: Repo, rows: list[dict], start: str) -> str:
    """Rewrite data/rewards.csv from `start` on. Returns what happened."""
    if not any(r["date"] >= start for r in rows):
        return "no rows in the window — file left as it is"
    existing, sha = repo.read(REWARDS_PATH)
    text = compose_rewards_csv(rows, existing, start)
    if text == existing:
        return "unchanged"
    old_n = max(len(existing.splitlines()) - 1, 0)
    new_n = len(text.splitlines()) - 1
    if new_n < old_n * 0.9:
        return f"refused: the new file would have {new_n:,} rows where it has {old_n:,}"
    repo.write(REWARDS_PATH, text, sha, "Update rewards.csv [skip ci]")
    return f"written: {new_n:,} rows"


def utc_day(now: float, days_back: int = 0) -> str:
    return (dt.datetime.fromtimestamp(now, tz=dt.timezone.utc)
            - dt.timedelta(days=days_back)).strftime("%Y-%m-%d")


def check_rewards(rows: list[dict], seen: dict, paid_seen: dict, now: float) -> dict:
    """3.0's watcher, the engines taken out. Rows are grouped per
    market-day (the exchange splits one into a SKIPPED and a PAID row); a
    market-day is new when its total differs from the one remembered.
    The first check only records; a check where more than half the window
    reads new re-records instead of pushing old rows."""
    first = not seen
    agg: dict[str, dict] = {}
    for r in rows:
        key = f"{r['date']}|{r['market']}"
        a = agg.setdefault(key, {"date": r["date"], "market": r["market"],
                                 "usd": 0.0, "paid": 0.0})
        a["usd"] += r["reward_usd"]
        if r["status"] != "SKIPPED":
            a["paid"] += r["reward_usd"]
    fresh, totals = [], {}
    for key, a in agg.items():
        totals[a["date"]] = totals.get(a["date"], 0.0) + a["paid"]
        if abs(seen.get(key, -1.0) - round(a["usd"], 2)) > 0.005:
            fresh.append(a)
        seen[key] = round(a["usd"], 2)
        paid_seen[key] = round(a["paid"], 2)
    for d in (seen, paid_seen):
        if len(d) > SEEN_CAP:
            for k in sorted(d)[:len(d) - SEEN_CAP]:
                del d[k]
    days = {d: round(v, 2) for d, v in sorted(totals.items())}
    if first:
        return {"new_count": 0, "days": days, "note": "baseline recorded"}
    if len(fresh) > max(400, 0.5 * len(agg)):
        return {"new_count": 0, "days": days, "note": "baseline re-recorded"}
    show_from = utc_day(now, SHOW_DAYS)
    new = [a for a in fresh if a["date"] >= show_from]
    return {"new_count": len(new), "days": days, "note": ""}


def rewards_push_text(res: dict) -> str:
    days = sorted((res.get("days") or {}).items())[-2:]
    line = ", ".join(f"{d[5:]} ${v:,.2f}" for d, v in days)
    return f"{res['new_count']} new rows at the exchange; latest day totals: {line}"


# -- trades -----------------------------------------------------------------

def publish_trades(client, repo: Repo, known_ids, deep: bool) -> str:
    """Append the exchange's newest activity to data/trades.csv, the same
    rows 3.0 wrote (deduplicated line by line, so overlap costs nothing)."""
    from v3.main import parse_activities, trades_csv_append
    raw = client.activities(pages=25 if deep else 3, tries=2, timeout=20.0)
    existing, sha = repo.read(TRADES_PATH)
    # every order id the file already names is ours: with them the parser
    # reads which side of a trade was ours the way 3.0 did, not by the
    # intent alone (which a counterparty's order can carry too)
    known = set(known_ids)
    for ln in existing.splitlines()[1:]:
        parts = ln.split(",")
        if len(parts) > 8 and parts[8]:
            known.add(parts[8])
    rows = parse_activities(raw, known)
    text, added = trades_csv_append(existing, rows)
    if not added:
        return f"{len(raw)} activities, nothing new"
    repo.write(TRADES_PATH, text, sha, f"trade history: +{added} rows [skip ci]")
    return f"{len(raw)} activities, +{added} rows"
