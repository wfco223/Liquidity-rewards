"""The football families, migrated from 2.0 so the whole book can live
under one roof when the owner flips the floor.

College keeps its launch behavior on the owner's word ("The collateral is
low, so I wouldn't change anything for now"): it may still price a
minimum-size order in front of a junk wall (`allow_improve`). The NFL —
and every family after it — is behind-the-touch only, per the owner's
2026-08-20 correction, which is the engine default.

Both discover from the exchange's sports event tags, same shape as
politics: names and event divisors come with the feed, so estimates are
never guessed.
"""

from __future__ import annotations

from .api import events_of
from .family import FamilyConfig
from .names import name_from_market

CFB_PREFIXES = ("aachc-cfb-wins-",)
NFL_PREFIXES = ("tec-nfl-", "aqc-nfl-", "ftsc-nfl-", "fptc-nfl-")


def _feed_discover(tags: tuple[str, ...], prefixes: tuple[str, ...]):
    def discover(client) -> dict[str, dict]:
        out: dict[str, dict] = {}
        order: list[str] = []
        for tag in tags:
            n_tag = 0
            try:
                # a page at a time, never a tag's whole feed (2026-09-02)
                for ev in events_of(client, tag, max_pages=8):
                    n_tag += 1
                    title = str(ev.get("title") or ev.get("name") or "").strip()
                    rows = [m for m in ev.get("markets") or []
                            if m.get("slug") and not m.get("closed")
                            and m["slug"].startswith(prefixes)]
                    for m in rows:
                        slug = m["slug"]
                        if slug not in out:
                            order.append(slug)
                        out[slug] = {"event_n": len(rows),
                                     "name": name_from_market(m, title)[:110]}
            except Exception:  # noqa: BLE001
                if n_tag:
                    raise     # died mid-feed: no partial universe
                continue      # an unknown tag must not sink the rest
        groups: dict[str, list[str]] = {}
        for s in order:
            groups.setdefault(s.rsplit("-", 1)[0], []).append(s)
        for s in order:
            g = groups[s.rsplit("-", 1)[0]]
            if len(g) > out[s]["event_n"]:
                out[s]["event_n"] = len(g)
        return out
    return discover


cfb_discover = _feed_discover(("football", "cfb", "college-football"),
                              CFB_PREFIXES)
nfl_discover = _feed_discover(("nfl", "football", "nfl-futures"),
                              NFL_PREFIXES)


def cfb() -> FamilyConfig:
    """Win totals. Week 0 kicks off Saturday 2026-08-29; the weekly
    Thursday-evening-to-Sunday-morning pull starts with that Thursday."""
    return FamilyConfig(
        name="College football", tag="CFB",
        known_ground=False, rest_style="behind", revive=False,
        allow_improve=True,
        # owner, 2026-08-21 evening: "Bring cfb down to 50." — and the
        # holdings count against it at liquidation value: "no more than
        # $50 of risk in cfb, orders + holdings"
        # owner, 2026-08-28: "You can bump it to 100" — the $50 cap was
        # pinned with holdings eating $32 of it while the planner's idle
        # queue claimed $100+/day across books it could not fund; the
        # $1-per-market breadth cap stays
        capital_usd=100.0, per_market_usd=1.00,
        holdings_in_ceiling=True,
        dump_usd_day=10.0,
        # the dead-money drain (owner, 2026-08-29, choosing this over
        # a weekly liquidation): 63 of 90 held positions were earning
        # nothing — exits outside the paying window or on sides below
        # Target Size. Stock whose engine exits measure ~$0 for six
        # hours drains through the taker rail at <=2-tick spreads,
        # never more than 5 ticks under cost. cfb only for now.
        dead_drain_s=21600.0,
        # owner, 2026-08-27, opening week: "I don't think there are any
        # games until Saturday. You can turn it on in the meantime."
        # owner, 2026-08-28 evening: "we can go back in around 2:00 am
        # Sunday morning until Thursday September 3rd at 3 pm eastern"
        # — rest SUNDAY 02:00 -> THURSDAY 15:00 ET. THIS WEEK'S call,
        # not a fixed rhythm ("Doesn't have to be weekly. Things might
        # change, or I might get a strategy for competing"): the window
        # repeats by default but he re-decides it week to week.
        # (Deployed after the Sat 09:00 Week-0 pull he approved, so
        # that pull ran on the old clock.)
        rest_from=(6, 2), rest_until=(3, 15),
        season_start=(2026, 8, 27),
        # 400+ live orders need real book coverage: the meter went blind
        # for 8 hours on the smaller budget (2026-08-21 morning)
        # owner, 2026-08-30 ("make actions up to our limit"): the
        # exchange's documented budget is 20 req/s — cfb's slow eyes
        # (456 books) get 48 look-ups/cycle with a 16-slot scan lane,
        # and the action allowance doubles. The 60s cycle and blast
        # radius are the caps now, not the exchange.
        books_per_cycle=48, scan_reserve=16,
        book_stale_s=300.0, read_age_s=900.0,
        max_actions_per_cycle=12,
        # owner, 2026-08-21: football must test hypotheses too — scouts
        # and starter positions are how it learns what earns
        probe_usd=3.0, grow_usd=10.0,
        replan_s=900.0,
        # the cycle-out rule, ON (owner, 2026-08-26 "Yes to 1", after
        # 24 hours of zero cfb churn with the budget pinned full): an
        # order measuring under the bar with no better plan at its
        # market is pulled so the money can go to the next best one.
        # Same setting as politics; nfl/nba stay off.
        weak_pull_s=30.0,
    )


def nfl() -> FamilyConfig:
    """NFL futures: awards, title races, playoffs, season stat futures.
    Resting window Tuesday 06:00 -> Thursday 17:00 ET — the NFL plays
    Thursday/Sunday/Monday, so the family is out from Thursday evening
    through Tuesday morning."""
    return FamilyConfig(
        name="NFL futures", tag="NFL",
        known_ground=False, rest_style="behind", revive=False,
        allow_improve=True,
        # owner, 2026-08-22: "similar to cfb, which has done pretty
        # well" — $50 all-in (orders AND holdings), same dump cap, same
        # coverage and book-freshness posture
        capital_usd=50.0, per_market_usd=1.00,
        holdings_in_ceiling=True,
        dump_usd_day=10.0,
        # owner, 2026-09-08: "intentionally small sized risks and
        # intentionally aggressive to see what the markets are like.
        # Then as we get information relax the aggression" — $1 of
        # collateral per order, the closer depths favoured until each
        # has 48 hours of our own resting time behind it
        explore=True, explore_usd=1.0, explore_hours=48.0,
        explore_bonus=0.30,
        rest_from=(1, 6), rest_until=(3, 17),
        season_start=(2026, 8, 20),
        # 2026-09-09 01:27Z, switch on: nothing placed for hours. An
        # entry needs a book under two minutes old, idle markets were
        # re-read every four hours, and the one pass after boot had run
        # with the switch off. The explorer re-reads its 1,160 idle
        # markets every 15 minutes, 40 a cycle, so the placer always
        # has fresh ground to enter.
        books_per_cycle=40, scan_reserve=16, rescan_s=900.0,
        book_stale_s=300.0, read_age_s=900.0,
        max_actions_per_cycle=6,
        probe_usd=3.0, grow_usd=10.0,
        replan_s=900.0,
    )
