"""Politics: the first family, and the reason 3.0 exists.

Discovery is the events feed under the politics/elections tags — the
authoritative source, not slug heuristics. It carries three things the
engine needs and one it must never lose:

* every open market, with the event it belongs to;
* the EVENT SIZE — the pool divisor. A market discovered here has a
  confirmed divisor by construction, which is the only condition under
  which the engine shows a dollar estimate;
* the feed's own names for everything;
* and a hard NO at the door for economics markets (standing owner rule —
  the exchange's politics tag includes CPI/Fed markets; we never touch
  them, not even read-only in the universe).

Candidate markets of one race are sometimes modeled as separate
single-market events while the pool covers the whole race, so the event
size is raised to the race-group size (slug minus its last token) when
that group is larger — 1.0's rule, kept.

The config: known ground. Join the touch only when the book is provably
quiet, revive dead sides inside tight collateral caps, rest every day
(politics has no game days). The owner's preference verbatim: "rest near
the touch in LOW-volatility markets where fill risk is small. Fills are
usually losses here, not wins."
"""

from __future__ import annotations

from .api import events_of
from .family import FamilyConfig
from .names import disambiguate, name_from_market
from .programs import is_econ

TAGS = ("politics", "elections")


def discover(client) -> dict[str, dict]:
    """slug -> {event_n, name} for every open, non-econ politics market."""
    out: dict[str, dict] = {}
    order: list[str] = []
    for tag in TAGS:
        n_tag = 0
        try:
            # streamed a page at a time — a tag's whole feed was
            # 100-200 MB of Python held at once (owner, 2026-09-02)
            for ev in events_of(client, tag):
                n_tag += 1
                title = str(ev.get("title") or ev.get("name") or "").strip()
                rows = [m for m in ev.get("markets") or []
                        if m.get("slug") and not m.get("closed")
                        and not is_econ(m["slug"])]
                labels = disambiguate([(m["slug"],
                                        name_from_market(m, title)[:110])
                                       for m in rows])
                for m in rows:
                    slug = m["slug"]
                    if slug not in out:
                        order.append(slug)
                    out[slug] = {"event_n": len(rows),
                                 "name": labels[slug]}
        except Exception:  # noqa: BLE001
            if n_tag:
                raise     # a feed that died mid-way must not hand back a
                          # partial universe (the 09-01 list collapse)
            continue      # an unknown tag must not sink the other
    # single-market events that are really one race sharing one pool
    groups: dict[str, list[str]] = {}
    for s in order:
        groups.setdefault(s.rsplit("-", 1)[0], []).append(s)
    for s in order:
        g = groups[s.rsplit("-", 1)[0]]
        if len(g) > out[s]["event_n"]:
            out[s]["event_n"] = len(g)
    return out


def config() -> FamilyConfig:
    return FamilyConfig(
        name="Politics", tag="POL",
        known_ground=True, rest_style="join_quiet", revive=True,
        # owner, 2026-08-21: "only quote in whole shares. No fractional
        # in politics, for now. We need to test whether those are even
        # getting picked up for earning rewards."
        whole_shares=True,
        vol_quiet=0.15,
        # Owner, 2026-08-20 (the flatten rebuild): "increase the budget
        # to 100" — one hundred dollars of collateral at risk, total —
        # then "It shouldn't be necessarily two dollars per market, feel
        # free to do up to $20 per market. Just be very picky with which
        # ones you're into and if something's not working cycle out."
        # Picky = a 10c/day entry bar (5x the old one) on a $100 book
        # ranked by what actually paid; not-working = measured under that
        # bar for two hours with nothing better at the market.
        # revive up to the full per-market cap (owner, 2026-08-21: "we
        # have to be able to qualify markets that could be winners") —
        # and with negative-risk netting, qualifying several brackets of
        # one race barely moves the ceiling
        # owner, 2026-08-21 evening: "we can up the politics budget
        # to 250"
        # expected-risk budgeting (owner, 2026-08-25): capital_usd is
        # the EXPECTED-risk cap (collateral x fill odds); the gross
        # ceilings bound the worst correlated day in nominal dollars
        expected_risk=True,
        # owner, 2026-08-30 ("2500 is fine"): the raw-claims planner
        # pressed gross to $492 of the old $500 within hours; the
        # worst-correlated-day nominal bound moves to $2,500. The
        # expected-risk cap stays at $250 — that is still the primary
        # budget; gross is the belt over it.
        capital_usd=250.0, gross_cap_usd=2500.0,
        per_market_usd=20.0, per_market_gross_usd=60.0,
        revive_max_usd=20.0,
        share_hi=0.10,
        # owner, 2026-08-21: "I would do 30 seconds under 75 cents, but
        # just for politics. There are so many options you can find
        # something better." One bar for entry and culling — nothing
        # places under 75c/day, and two consecutive under-bar readings
        # (the loop runs every 60s) pull the order for the next best spot.
        # owner, 2026-08-21 evening ("Do 1, 2, and 3"): the bar drops
        # to 50c to admit the passed-on middle of the board; the share
        # cap may reach 50% where model edge has earned the lift; proven
        # markets get double the per-market money.
        # owner, 2026-08-25 ("lift the 50 cent cap... if it is a small
        # potential benefit the risk is also small"): the bar drops to
        # 2c/day — EV-positive is the gate, and the expected-risk
        # budget makes small claims carry only small charges
        min_est_day=0.02, weak_pull_s=30.0,
        # owner, 2026-08-25 ("Yes, that's fine for now"): politics
        # decisions ran on claims divided by 3 — the measured gap
        # between estimates and pay in the July tier era (3.3x, 3.6x,
        # 7.1x on three settled days).
        # owner, 2026-08-29 ("You can remove the divided by 3
        # modifier"): the era ended — Aug-26 paid $304 on ~$265 of
        # 24h-scaled claims (~0.9x) and Aug-27 paid $219 on ~$88
        # (~0.4x); the deflator had the engine believing ~$29/day
        # while earning $219. Decisions run on raw claims again.
        est_deflate=1.0,
        # owner, 2026-08-21: "it's okay to get filled at reasonable
        # prices" — two ticks of edge earns the touch, four lifts the
        # courtesy share toward 35%; the drift alarm moves above that so
        # the two don't fight
        # owner, 2026-08-29 ("this sort of strategy only obviously
        # works when fills are more rare" — 86 entry fills/day tied
        # $94 of buying power for $2.82 of one-time edge): the touch
        # now demands 4 ticks of model edge instead of 2, and ground
        # our own orders were recently taken on (heat >= 0.5, roughly
        # a fill within the hour) may not join the touch at all —
        # resting behind stays allowed. Shape churn misses
        # take-and-refill snipers; our own fills do not.
        join_edge_ticks=4.0, touch_heat_max=0.5,
        share_max=0.50, proven_per_market_usd=40.0, drift_share=0.45,
        # graduation (owner, 2026-08-21): a market that has MEASURED at
        # least 25c of accrual today moves off the $100 search ceiling
        # onto the proven pool's own $150 cap — the search money keeps
        # hunting new candidates
        dump_usd_day=50.0,
        # owner, 2026-09-09: the settle check (30s on, re-plan, pull or
        # move), two new orders a cycle, and the exit float on a $15/day
        # concession budget
        settle_s=30.0, enter_per_cycle=2,
        exit_float=True, exit_float_usd_day=15.0,
        # owner, 2026-08-29 ("we should find a way to get the resting
        # positions down"): 76 idle positions — $91 of long stock
        # earning nothing plus $83 of collateral frozen in 22 shorts
        # whose break-even buy-backs could never fill. The dead-money
        # drain (longs, through the taker rail, $50/day cap) and the
        # dead-short step-up (buy-backs may bid to the touch, loss
        # bounded at 5 ticks over what the short collected) both key
        # off this dwell. His hand-covered stock and the avoided/
        # frozen/watched books are never touched.
        dead_drain_s=21600.0,
        # owner, 2026-08-22: stay out of the Alaska governor markets —
        # special (ranked-choice) rules pending. And the balance-of-power
        # markets are the owner's own book ("Don't place any orders in
        # the balance of power. I'm going to do that one by hand") — the
        # engine's resting orders there were killing his via the
        # exchange's self-match prevention.
        # owner, 2026-08-22: the presidential announce markets "aren't
        # doing good" — off limits ($24.92 paid all-time across 32)
        # owner, 2026-08-28 ("Let me place the orders by hand" / "The
        # model should be hands off with these markets"): the MA dem
        # senate primary MoV books are his to work — the engine pulls
        # its own orders out and never enters; his hand orders and the
        # qualify-ask button's rests are untouchable as always
        avoid_tokens=("usgub-ak", "usgubp-ak", "paccc-balpow",
                      "cranc-uspres28", "ussep-mov-ma-dem"),
        # owner, 2026-08-24: "Don't sell my gop governor count race
        # orders. In fact don't touch those." Frozen, not avoided —
        # whatever rests there stays exactly where it is.
        freeze_tokens=("usgovcc",),
        # owner, 2026-08-27: "Take me out of all buy position on Ron
        # desantis in 2028 markets" — sell the stock into the bid
        # until flat, never buy DeSantis 2028 again
        liquidate_tokens=("rondes",),
        # owner, 2026-08-28: "Keep a websocket on those races",
        # corrected to "the margin of victory in the ma dem senate
        # primary markets" — those books stay fresh every cycle and
        # lead the stream subscription
        watch_tokens=("ussep-mov-ma-dem",),
        graduate_paid_usd=1.00, proven_usd=150.0,
        # owner, 2026-08-21: 75c is a GOAL — markets that could clear it
        # at full confidence get starter positions from this budget, and
        # probes stay funded ("you have to go out and get evidence")
        probe_usd=5.0, grow_usd=30.0,
        # and fresh money goes ONLY where the fill-and-reward record is
        # deepest: governor and senate races (winners, margins, primaries,
        # seat ladders) and the 2028 presidential slate
        # owner, 2026-08-21 evening: "let's look at getting into the
        # seat counts and house control markets... You can add each of
        # these" — House control (usho) and the GOP seat brackets
        # (scc-hrep) join the scope. Turnout stays out: no model.
        # owner, 2026-08-25: the 2028 party-winner pair rejoins the
        # fundable set — his best politics markets ever ($18/day each
        # at peak), cut off from fresh money by a name mismatch: the
        # list said usp-2028, the slugs read usp-party-2028. Departures
        # (apdc), process (opdc/lawec) and science stay OUT — offered
        # the same day, declined.
        enter_tokens=("usgub", "usse", "senate", "uspres", "usp-2028",
                      "usp-party", "usho", "scc-hrep"),
        # the state races (owner, 2026-09-09: "Yes state races should be
        # included ... But don't place orders in these races before I
        # get the chance to look at them"): attorneys general,
        # lieutenant governors, secretaries of state, state supreme
        # courts — scanned, scored and shown; no order until he opens
        # the market from the status page
        hold_tokens=("usag", "usltgov", "ussos", "ussupct"),
        rest_from=None, rest_until=None,      # politics rests every day
        min_days_out=3,
        # owner, 2026-08-30 ("make actions up to our limit"), after his
        # docs find: the exchange's documented budget is 20 requests/s
        # (~1,200/min) and the whole machine was using under 10% of it.
        # The remaining caps are the 60s cycle (verification costs
        # seconds per placement) and blast radius, not the exchange.
        books_per_cycle=48, scan_reserve=12,
        book_stale_s=300.0, read_age_s=900.0,
        max_actions_per_cycle=16,
        terms_slice=300, terms_full_s=1800.0,
        replan_s=600.0,                       # a fresh look every 10 minutes

    )
