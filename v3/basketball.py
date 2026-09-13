"""The NBA futures family (owner, 2026-08-22: "Also add in NBA.").

Discovery is the exchange's events feed under the basketball tags,
filtered to slug prefixes. tec-nba- is measured (the 2026-08 event
survey: conference winners, $1000/day pool). The other three are the
NBA spellings of the NFL family's futures classes — a prefix that
matches nothing costs nothing, and the nba_tags.yml survey will
confirm the full list once the owner restores GitHub Actions minutes.
Deliberately OUT: pntcbk-nba- (single-game player props — game-time
fill risk, not futures), WNBA, and college basketball; the owner
asked for the NBA only.

The config mirrors the NFL's ("similar to cfb" was the owner's word
for that one): $50 all-in with holdings counted at liquidation value,
behind-the-touch resting, the same dump cap.

No resting window yet: the 2026-27 season opens in late October, and
until then there are no game days at all. The NBA then plays nearly
every day, so football's weekly quiet stretch has no equivalent —
the in-season posture is an owner decision to make before opening
night, not a guess to bake in now.
"""

from __future__ import annotations

from .family import FamilyConfig
from .football import _feed_discover
from .names import name_from_market

NBA_PREFIXES = ("tec-nba-", "aqc-nba-", "ftsc-nba-", "fptc-nba-")

_nba_by_tag = _feed_discover(("nba", "basketball", "nba-futures"),
                             NBA_PREFIXES)

# what the search sweep asks for when the tags come back empty — each
# query is one market class the family covers
_NBA_SEARCHES = ("NBA Champion", "NBA Finals", "NBA Eastern Conference",
                 "NBA Western Conference", "NBA MVP", "NBA award",
                 "NBA wins", "NBA Defensive Player", "NBA Rookie",
                 "NBA scoring")


def nba_discover(client) -> dict[str, dict]:
    """Tags first; the exchange's basketball tags sat EMPTY on
    2026-08-23 (the NBA card read '0 markets known' while the same
    mechanism fed the NFL 1064), so the search endpoint — which found
    the East-winner event for the August survey — is the fallback."""
    out = _nba_by_tag(client)
    if out:
        return out
    order: list[str] = []
    for q in _NBA_SEARCHES:
        try:
            j = client.search(q, limit=25)
        except Exception:  # noqa: BLE001 — one query must not sink the rest
            continue
        for ev in (j.get("events") or []) if isinstance(j, dict) else []:
            title = str(ev.get("title") or ev.get("name") or "").strip()
            rows = [m for m in ev.get("markets") or []
                    if m.get("slug") and not m.get("closed")
                    and m["slug"].startswith(NBA_PREFIXES)]
            for m in rows:
                slug = m["slug"]
                if slug not in out:
                    order.append(slug)
                out[slug] = {"event_n": len(rows),
                             "name": name_from_market(m, title)[:110]}
    groups: dict[str, list[str]] = {}
    for s in order:
        groups.setdefault(s.rsplit("-", 1)[0], []).append(s)
    for s in order:
        g = groups[s.rsplit("-", 1)[0]]
        if len(g) > out[s]["event_n"]:
            out[s]["event_n"] = len(g)
    return out


def nba() -> FamilyConfig:
    return FamilyConfig(
        name="NBA futures", tag="NBA",
        known_ground=False, rest_style="behind", revive=False,
        # owner, 2026-08-23: "Try to join the walls, even if you don't
        # make all that much. Looking for stability here and not a lot
        # of bad buys." Joining a 400-900k wall means the whole wall
        # trades before our shares do — tiny share, near-zero fill
        # odds. So: never price in front of a wall, and no earnings
        # bar — any positive-EV join may rest, however small.
        allow_improve=False,
        min_est_day=0.0,
        # owner, 2026-08-23: "You can increase the amounts in NBA per
        # market. They are so small that they aren't earning anything."
        wall_size_up=True,
        capital_usd=50.0, per_market_usd=2.00,
        holdings_in_ceiling=True,
        dump_usd_day=10.0,
        rest_from=None, rest_until=None,     # offseason: no game days yet
        books_per_cycle=10, scan_reserve=5,
        book_stale_s=300.0, read_age_s=900.0,
        max_actions_per_cycle=6,
        probe_usd=3.0, grow_usd=10.0,
        replan_s=900.0,
    )
