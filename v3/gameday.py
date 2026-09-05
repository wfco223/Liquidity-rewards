"""The game-day experiment (owner, 2026-09-05: "create a program to
cheaply test a few day of markets for games tomorrow since we ran out of
time today").

What the Miami–Stanford probe showed (2026-09-04): one college game
carries 295 markets on eight live programs worth $42,000 together —
$10,500 across 42 spread lines, $8,250 across 33 totals, $4,500 on the
one moneyline, the rest over period and prop markets — with Target
Sizes of 100,000–200,000 contracts on the main lines and 10,000–25,000
on the small ones, and discount factors of 0.10–0.30 a tick.

The cheap test: for the two biggest games kicking off within the next
day and a half, pick ONE market of each kind — the moneyline, the
spread line and the total nearest 50c, the first-half winner, a
first-half spread, a team total, a first-touchdown prop — and rest ONE
SHARE on each side at the touch. Fourteen markets, twenty-eight shares,
about fourteen dollars of collateral. Everything leaves at kickoff;
filled shares exit through the ordinary machinery. What the exchange
then posts for those market-days, against what the estimator claimed,
is the experiment (v3/PREDICTIONS.md, P17).

Its own family, its own switch on /switch, off by default: nothing
places until the owner turns it on.
"""

from __future__ import annotations

import datetime as dt
import re
import time

from .api import events_of
from .family import FamilyConfig
from .names import name_from_market
from .programs import pick_period, to_num

TAGS = ("cfb", "college-football", "football")
EVENT_PREFIX = "cfb-"
HORIZON_S = 36 * 3600.0          # kickoffs this far ahead are "tomorrow"
MAX_GAMES = 2
# one market of each kind per game, in this order
PICKS = ("moneyline", "spread", "total", "period_winner", "period_spread",
         "team_total", "prop")
# the narrower sub-kinds we read books for, so a pick costs a handful
# of book reads and not a hundred
PREFER = {"period_winner": ("winner-1h-",), "period_spread": ("1h-",),
          "prop": ("ftd-", "fs-")}
_PERIOD = re.compile(r"^(1h|2h|1q|2q|3q|4q)-")
_START_KEYS = ("startTime", "startDate", "eventStartTime", "start",
               "gameStartTime", "startsAt")


def start_epoch(ev: dict) -> float | None:
    """Kickoff as epoch seconds, from whichever field the feed uses."""
    for k in _START_KEYS:
        v = ev.get(k)
        if v in (None, ""):
            continue
        if isinstance(v, (int, float)):
            return float(v) / (1000.0 if v > 1e11 else 1.0)
        s = str(v).strip()
        try:
            return float(s) / (1000.0 if float(s) > 1e11 else 1.0)
        except ValueError:
            pass
        try:
            t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.timestamp()
    return None


def group_of(slug: str, ev_slug: str) -> str | None:
    """Which kind of market this is, from the slug alone. Market slugs run
    <kind>-<event slug>-<rest>: asc-cfb-mia-stan-2026-09-04-neg-3pt5."""
    marker = f"-{ev_slug}" if ev_slug else "-cfb-"
    kind, sep, rest = slug.partition(marker)
    if not sep or not kind or "-" in kind:
        return None
    rest = rest.lstrip("-")
    if kind == "aec":
        return "moneyline"             # the one market with no rest
    if kind == "asc":
        return "period_spread" if _PERIOD.match(rest) else "spread"
    if kind == "tsc":
        if rest.startswith("total-"):
            return "total"
        if rest.startswith("tt-"):
            return "team_total"
        if _PERIOD.match(rest):
            return "period_total"
        return None                     # half team totals: not this test
    if kind == "atc":
        return "period_winner"
    if kind == "astatc":
        return "prop"
    return None


def _mid(client, slug: str) -> float | None:
    try:
        b = client.book(slug)
    except Exception:  # noqa: BLE001 — an unreadable book is not a pick
        return None
    if not b.bids or not b.asks:
        return None
    return (b.bids[0][0] + b.asks[0][0]) / 2.0


def pick_markets(client, groups: dict[str, list[dict]], ev_slug: str) -> list[tuple[dict, str]]:
    """One market per kind: the moneyline, else the two-sided book whose
    mid is nearest 50c — where a share is worth resting on both sides."""
    out = []
    for group in PICKS:
        cands = groups.get(group) or []
        if not cands:
            continue
        if group == "moneyline":
            out.append((cands[0], group))
            continue
        marker = f"-{ev_slug}-"
        pref = PREFER.get(group)
        if pref:
            narrowed = [m for m in cands
                        if m["slug"].partition(marker)[2].startswith(pref)]
            cands = narrowed or cands
            if narrowed:
                cands = narrowed
        best, best_d = None, None
        for m in cands[:60]:
            mid = _mid(client, m["slug"])
            if mid is None:
                continue
            d = abs(mid - 0.5)
            if best_d is None or d < best_d:
                best, best_d = m, d
        if best is not None:
            out.append((best, group))
    return out


def discover(client) -> dict[str, dict]:
    """Tomorrow's two biggest games, one market of each kind each."""
    now = time.time()
    games: list[tuple[float, str, str, list[dict]]] = []
    seen: set[str] = set()
    for tag in TAGS:
        try:
            for ev in events_of(client, tag, max_pages=8):
                ev_slug = str(ev.get("slug") or "")
                if not ev_slug.startswith(EVENT_PREFIX) or ev_slug in seen:
                    continue
                start = start_epoch(ev)
                if start is None or start <= now or start > now + HORIZON_S:
                    continue
                mkts = [m for m in ev.get("markets") or []
                        if m.get("slug") and not m.get("closed")]
                if not mkts:
                    continue
                seen.add(ev_slug)
                games.append((start, ev_slug,
                              str(ev.get("title") or ev.get("name") or ""), mkts))
        except Exception:  # noqa: BLE001 — an unknown tag must not sink the rest
            continue
    if not games:
        return {}
    # the biggest games first: the moneyline's pool is the game's tier
    ml = {g[1]: next((m["slug"] for m in g[3]
                      if group_of(m["slug"], g[1]) == "moneyline"), None)
          for g in games}
    pools: dict[str, float] = {}
    want = [s for s in ml.values() if s]
    if want:
        try:
            raw = client.programs(want)
        except Exception:  # noqa: BLE001 — rank by kickoff instead
            raw = {}
        for s, r in (raw or {}).items():
            per = pick_period((r or {}).get("timePeriods") or [], s)
            if per:
                pools[s] = to_num(per.get("rewardPool"))
    games.sort(key=lambda g: (-pools.get(ml.get(g[1]) or "", 0.0), g[0]))
    out: dict[str, dict] = {}
    for start, ev_slug, title, mkts in games[:MAX_GAMES]:
        groups: dict[str, list[dict]] = {}
        for m in mkts:
            g = group_of(m["slug"], ev_slug)
            if g:
                groups.setdefault(g, []).append(m)
        for m, group in pick_markets(client, groups, ev_slug):
            out[m["slug"]] = {
                # the pool divisor: every line of this kind in the game
                # shares one program (42 spread lines, one $10,500 pool)
                "event_n": len(groups[group]),
                "name": name_from_market(m, title)[:110],
                "start": start, "group": group, "event": ev_slug,
            }
    return out


def config() -> FamilyConfig:
    return FamilyConfig(
        name="Game day", tag="GAME",
        known_ground=False, rest_style="behind", revive=False,
        # two games, seven markets each, a share a side: ~$14 of
        # collateral, capped at $20 with filled shares counted
        capital_usd=20.0, per_market_usd=1.10,
        holdings_in_ceiling=True,
        whole_shares=True,
        min_days_out=0,                 # the games ARE tomorrow
        rest_from=None, rest_until=None,
        books_per_cycle=24, scan_reserve=4,
        book_stale_s=120.0, read_age_s=480.0,
        max_actions_per_cycle=8,
        probe_usd=0.0, grow_usd=0.0, replan_s=0.0, weak_pull_s=0.0,
        dump_usd_day=0.0,
        discover_s=1800.0,              # the slate firms up through the day
        # the experiment itself
        scout_all=True, scout_qty=1.0, scout_join=True, kickoff_pull=True,
    )
