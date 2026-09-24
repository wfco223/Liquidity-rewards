"""Reward terms as first-class, tracked data.

1.0's most expensive information failure: it recorded only "does this
market have a program, yes/no", never the terms. When the exchange cut
the standard pool from ~$500 to ~$200 per event, nothing noticed and the
day's income halved with no explanation available from the code. Its
late fix then split into two readers that never talked to each other:
the scoring cache kept serving stale pools while the change-watcher
(which also silently skipped whole families, senate seats included)
couldn't write back.

2.0 has ONE store. The estimator, the engine, and the change alerts all
read the same object; there is nothing else to go stale against. Every
change to a market's pool, Target Size, discount factor, event divisor
or program is a change event — alerted, and appended to a history sink
so "what were the terms on the 14th?" is answerable forever. The first
sighting of a market seeds silently (there is nothing to compare
against) but is stamped, so "tracking started here" is explicit.
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass

from .programs import Program, pick_period, program_from_period, with_event_n


@dataclass(frozen=True)
class TermsChange:
    slug: str
    field: str        # pool / target / df / pid / status / event_n / program_gone / program_new
    old: object
    new: object

    def __str__(self) -> str:
        return f"{self.slug}: {self.field} {self.old} -> {self.new}"


# Fields whose change is worth an alert, in the order they are reported.
_WATCHED = ("pool", "target", "df", "pid", "status")

# A MARKET'S FIRST DAY IN A PROGRAM PAYS NOTHING (owner, 2026-09-24 "Fix
# the meter's first day"). Over the two weeks to 09-21 the rewards
# endpoint posted no row at all for the markets that joined a program
# partway through an ET day: the fifteen House district markets on 09-11
# (their program began 19:00Z; $44.56 estimated), the eleven Senate
# combos on 09-17 (read with no program the evening before; $84.49) —
# 29 of 30 such markets, while the ones already in a program posted on
# their first day, 18 of 18, and both groups posted every day after. So
# the ledger notes a JOIN: a program seen after a read that found none
# within JOIN_EVIDENCE_S, or a first sighting of a program that itself
# began after that day's midnight ET. A program replaced by another
# (a re-issue) is not a join. The meter counts nothing for a market on
# the ET day it joined.
JOIN_EVIDENCE_S = 36 * 3600.0      # how old a no-program read may be to show a join
JOIN_KEEP_S = 3 * 86400.0          # joins kept for the page and the checks

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # no tz database: the estimator's fixed EDT fallback
    _ET = _dt.timezone(_dt.timedelta(hours=-4), "ET")


def et_day_start(now: float) -> float:
    """Midnight ET, as a timestamp, of the ET day `now` falls in."""
    loc = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc).astimezone(_ET)
    return loc.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _iso_ts(s: str) -> float | None:
    try:
        return _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


class TermsStore:
    """Current terms per market plus change detection.

    `history_sink` is called with one dict per recorded row (seed rows
    included) — the caller decides where rows go (a JSONL file committed
    to the repo). `refresh` takes RAW incentives programs (from
    api.Client.programs) plus the event-size map from discovery; picking
    the paying period — spill filtering included — happens here, in one
    place, every time."""

    def __init__(self, history_sink=None):
        self.current: dict[str, Program] = {}
        self.updated_at: dict[str, float] = {}
        self.seeded_at: dict[str, float] = {}
        self.empty_at: dict[str, float] = {}    # last read that found no program
        self.joined_at: dict[str, float] = {}   # when a market joined a program mid-day
        self._sink = history_sink or (lambda row: None)

    def get(self, slug: str) -> Program | None:
        return self.current.get(slug)

    def age(self, slug: str, now: float | None = None) -> float:
        """Seconds since this market's terms were last confirmed. Markets
        never seen return infinity — unknown terms are infinitely stale."""
        ts = self.updated_at.get(slug)
        return (now or time.time()) - ts if ts else float("inf")

    def refresh(self, raw_programs: dict[str, dict], event_sizes: dict[str, int],
                now: float | None = None) -> list[TermsChange]:
        """Fold a fresh incentives read into the store. Returns the change
        events (empty on a pure seed). A market present in raw_programs
        whose periods yield no paying program records program_gone —
        that is the single largest thing that can happen to its income."""
        now = now or time.time()
        changes: list[TermsChange] = []
        for slug, raw in raw_programs.items():
            tp = pick_period(raw.get("timePeriods") or [], slug)
            new = (with_event_n(program_from_period(tp),
                                max(event_sizes.get(slug, 1), 1))
                   if tp is not None else None)
            old = self.current.get(slug)
            if new is None:
                self.empty_at[slug] = now
                if old is not None:
                    changes.append(TermsChange(slug, "program_gone", old.pid, None))
                    del self.current[slug]
                    self._record(slug, None, now, "gone")
                continue
            self.updated_at[slug] = now
            if old is None:
                self._note_join(slug, new, now)
                first_ever = slug not in self.seeded_at
                self.current[slug] = new
                if first_ever:
                    self.seeded_at[slug] = now
                    self._record(slug, new, now, "seed")
                else:  # was gone, came back
                    changes.append(TermsChange(slug, "program_new", None, new.pid))
                    self._record(slug, new, now, "new")
                continue
            diffs = [f for f in _WATCHED if getattr(old, f) != getattr(new, f)]
            if old.event_n != new.event_n:
                diffs.append("event_n")
            if diffs:
                for f in diffs:
                    changes.append(TermsChange(slug, f, getattr(old, f), getattr(new, f)))
                self._record(slug, new, now, "change")
            self.current[slug] = new
        self._prune(now)
        return changes

    def _note_join(self, slug: str, prog: Program, now: float) -> None:
        """A program on a market that had none: a JOIN when a read found
        no program on it recently, or when the program itself began after
        this ET day's midnight. A market never read before, in a program
        older than today, gets the benefit of the doubt."""
        empty = self.empty_at.get(slug)
        was_empty = empty is not None and 0.0 <= now - empty <= JOIN_EVIDENCE_S
        start = _iso_ts(prog.start) if prog.start else None
        began_today = start is not None and start > et_day_start(now) + 60.0
        if was_empty or began_today:
            self.joined_at[slug] = now

    def adopt(self, slug: str, prog: Program, now: float) -> bool:
        """Take a program another reader found (the tender's hand-off to
        the family's ledger) as a read of our own would: the same join
        rule, the same stamps. Never over a program already held."""
        if slug in self.current:
            return False
        self._note_join(slug, prog, now)
        self.current[slug] = prog
        self.updated_at[slug] = now
        self.seeded_at.setdefault(slug, now)
        return True

    def joined_today(self, now: float) -> dict[str, float]:
        """The markets that joined a program on the ET day `now` is in."""
        lo = et_day_start(now)
        return {s: t for s, t in list(self.joined_at.items()) if lo <= t <= now + 60.0}

    def _prune(self, now: float) -> None:
        for d, keep in ((self.empty_at, JOIN_EVIDENCE_S), (self.joined_at, JOIN_KEEP_S)):
            for s in [s for s, t in d.items() if now - t > keep]:
                del d[s]

    def _record(self, slug: str, prog: Program | None, now: float, why: str) -> None:
        row = {"ts": round(now, 1), "slug": slug, "why": why}
        if prog is not None:
            row.update(pool=prog.pool, target=prog.target, df=prog.df,
                       pid=prog.pid, status=prog.status, event_n=prog.event_n)
        self._sink(row)

    # -- persistence -----------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "current": {s: [p.pool, p.target, p.df, p.status, p.pid, p.tier,
                            p.start, p.end, p.event_n, p.pool_n]
                        for s, p in self.current.items()},
            "updated_at": self.updated_at,
            "seeded_at": self.seeded_at,
            "empty_at": self._pruned(self.empty_at, JOIN_EVIDENCE_S),
            "joined_at": self._pruned(self.joined_at, JOIN_KEEP_S),
        }

    @staticmethod
    def _pruned(d: dict, keep: float) -> dict:
        now = time.time()
        return {s: t for s, t in list(d.items()) if now - t <= keep}

    @classmethod
    def from_dict(cls, d: dict, history_sink=None) -> "TermsStore":
        st = cls(history_sink=history_sink)
        for s, v in (d.get("current") or {}).items():
            st.current[s] = Program(pool=v[0], target=v[1], df=v[2], status=v[3],
                                    pid=v[4], tier=v[5], start=v[6], end=v[7],
                                    event_n=v[8], pool_n=v[9])
        st.updated_at = dict(d.get("updated_at") or {})
        st.seeded_at = dict(d.get("seeded_at") or {})
        st.empty_at = {s: float(t) for s, t in (d.get("empty_at") or {}).items()}
        st.joined_at = {s: float(t) for s, t in (d.get("joined_at") or {}).items()}
        return st
