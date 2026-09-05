"""The engine. One class; every market category is a config of it.

3.0's merge of the two 2.0 engines, built to the owner's 2026-08-20
instruction: "Simplicity of v2 and personality of v1. A new version that
prioritizes politics but can be expanded in the way I expanded V2."

* A family's UNIVERSE comes from a pluggable `discover` function
  (politics: the events feed, names and event divisor included) or, for
  prefix families, whatever discover function wraps their sweep. The
  family fetches its own reward terms for that universe — no survey
  dependency, no shared mutable state with any other version.
* Resting style is config: `behind` (every new family — owner's rule)
  or `join_quiet` (politics, the owner's known ground: join the touch
  only when the book has been sitting still; step back when it is busy).
  Nothing in 3.0 ever prices IN FRONT of the touch.
* Qualifying a dead side is just another candidate (`revive=True`,
  known ground only): estimate_join already prices what happens when our
  size is what carries the side over Target Size. The owner accepted
  over-target exposure explicitly ("I'm fine with the qualifier bringing
  sides of the markets over their target size, I just need a list of
  where I'm exposed") — the exposure list is the blocks page.
* One risk number binds: `capital_usd`, the family's total collateral
  ceiling. The per-market cap exists but the family line is the one the
  owner watches.
* A market whose program died is LEFT ENTIRELY — every order cancelled,
  exits included, seller stood down (owner: "I don't want to be in
  markets if there are no rewards" / "You can remove the unwinding
  positions as well").
* No estimate until the divisor is confirmed: only markets that arrived
  through discovery (which knows their event) ever show a dollar figure.
  The 2026-08-20 lesson — a guessed divisor turned $1.50/day into
  $23.30/day on a phone screen — does not repeat.
* Every skip and every plan carries a plain-English `why`. The engine is
  read-only until its own switch is armed; while observing it still
  discovers, scores and shows exactly what it would do, so the owner can
  judge it against the running 1.0 before any money moves.

All money-touching calls go through the OrderDesk rails: post-only, price
bounds, whitelist, verify-by-id, never /modify.
"""

from __future__ import annotations

import math
import datetime as dt
import time
from dataclasses import asdict, dataclass, field
from zoneinfo import ZoneInfo

from . import risk
from .books import BookCache
from .evidence import Evidence, SNATCH_WEIGHT
from .fillmodel import DAY_S, FillModel
from .intents import BUY_LONG, BUY_SHORT, SELL_LONG, SELL_SHORT, capital_at_risk
from .orders import OrderDesk, snap_price
from .scoring import estimate_join
from .terms import TermsStore

ET = ZoneInfo("America/New_York")

BOOK_MAX_AGE = 120.0
# An exit slot "pays" when the model scores it at least this much a
# day. Exits must earn while they wait (owner, 2026-09-04: 33 of 53
# politics exits sat where nothing pays — buy-backs 14 ticks under the
# bid on a 92c sale, left there by a restart): among a buy-back's
# candidate slots, one that pays beats one that does not, always at or
# better than break-even. Under a cent a day is the same as nothing.
# Stock sells keep joining the ask touch (2026-08-22) — behind it pays
# less by construction, and an empty ask side anchors high (2026-08-27).
EXIT_PAYS_MIN_USD = 0.01
# A cancel-then-re-rest hands the re-rest the price it predicted, so
# the pair lands where the mover or the step-up said it would. Two
# loops on 2026-09-04 came from the pair disagreeing: a buy-back
# re-placed at 42c every minute for three hours (the step-up compared
# against its target, the re-rest priced by the slot optimizer on a
# book still showing the cancelled order), and lone-ask sells
# re-placed hourly at the same price (an off-grid anchor, 58.39c,
# read as a move from 59c). Owner: fix both.
REPLACE_PLAN_TTL_S = 300.0
PAGE_LOSS_USD = 1.0    # only losses bigger than this reach the phone
PAGE_SETTLE_S = 20.0   # let the book settle before marking an open
GONE_GRACE_S = 300.0
# An exit that has earned NOTHING for this long is not waiting for a
# better price — it is dead capital. Measured 2026-08-24: 18 of 39
# stuck politics exits were earning zero against $49.40 of the owner's
# money, while 21 were earning $2.196/day and must not be disturbed
# (owner: "Just try and make positive ev plays"). Six hours is long
# enough that a quiet spell is not mistaken for a dead order, short
# enough to act the same day.
EXIT_DRY_S = 6 * 3600.0
# Actions held back from maintenance for the ceiling when the book is
# over it. Without this the trim never runs in a busy family.
TRIM_RESERVE = 4
# The exit gate (owner, 2026-08-25: "option B is fine"). An exit may
# rest past break-even only while the reward it measures AT THAT PRICE,
# deflated by the measured ~3x estimator optimism, beats the expected
# fill loss by a margin:   est / EST_DEFLATE >= GATE_MARGIN x p_fill x
# give-up.  The margin also bridges a known mismatch: est is $/day
# while resting, the give-up is a one-shot loss at fill. Revisit both
# numbers when the share calibration lands (~Aug-29).
EST_DEFLATE = 3.0
GATE_MARGIN = 3.0
# An information probe's size: one share. Enough that a taker would
# bother eating it (which is the information), small enough that being
# wrong costs cents. QTY_GRID[0] (0.01 shares) is too small to probe
# anything — nobody snipes a hundredth of a share.
PROBE_MAX_QTY = 1.0
# The nurse (owner, 2026-08-25): a freshly placed front order is in
# the most danger in its first minutes — someone jumps it, or the far
# touch rushes it — and the 60s cycle would not notice until too late.
# Young orders on model-less markets get watched every few seconds;
# once nothing has moved against one for NURSE_STABLE_S it graduates
# and the watch ends ("When things are stable, then the process can
# end").
NURSE_STABLE_S = 600.0
# Bump this whenever _plan_side's SEMANTICS change — new caps, new
# bounds, new deflators. It is part of the scoreboard signature, so a
# restored scoreboard scored under older rules is thrown away and the
# board rescanned under the rules actually running. Found 2026-08-25:
# every reboot re-placed pre-rule plans verbatim (the rahema 12c buys,
# each within a minute of a boot, carrying the exact pre-deflator
# estimate) because the signature only covered config knobs, and
# today's rules are code, not config.
PLAN_RULES_REV = 4
NURSE_APPROACH_TICKS = 2
NURSE_BOOK_MAX_AGE_S = 15.0
# Hand-set orders (owner, 2026-08-25, the live card view): an engine
# order the owner moves by hand from the live view is PINNED — the
# engine stops repricing, sweeping, trimming or retreating it. "My
# changes should be durable, as long as things more or less stay the
# same on the book. But if there is a big change, for instance another
# order reduces my earning rate, the model can resume control." The
# release rule: the order's live earning rate falls under half of what
# it was when he set it, sustained for PIN_RELEASE_DWELL_S so one bad
# book read cannot break his word. The nurse still watches a pin for
# its first NURSE_STABLE_S (owner: "the nurse can also stay active
# for this") — a nurse pull ends the pin with the order.
PIN_RELEASE_FRACTION = 0.5
PIN_RELEASE_DWELL_S = 120.0
# Expected-risk budgeting (owner, 2026-08-25): the family and
# per-market caps charge each order collateral x its FILL ODDS — the
# risk actually carried — instead of full collateral, so quiet resting
# money multiplies. Guards, because everything then leans on the fill
# model: no order charges below PF_CHARGE_FLOOR of its collateral, an
# order the model has not measured charges IN FULL, hard GROSS
# ceilings bound the worst day in dollars no matter what the model
# believes, and the model's expected fills are graded against actual
# fills in the log every hour ("the full risk must be extremely well
# calibrated because that could open us up to a lot of exposure").
PF_CHARGE_FLOOR = 0.05   # a vanished order waits this long for the lagging
                       # position feed before it counts as a silent cancel

# size grid the planner walks (contracts); fractional sizes are live rails
QTY_GRID = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0,
            50.0, 100.0, 200.0, 500.0)


@dataclass
class FamilyConfig:
    name: str = "Family"
    tag: str = "FAM"
    # Known ground = a category the owner reads fluently (politics). It
    # unlocks join_quiet resting and reviving dead sides. Everything the
    # owner is NOT familiar with stays behind the touch and never revives
    # (owner, 2026-08-20: "don't auto set... in families where I'm not
    # familiar" and "place them behind the touch because of the df").
    known_ground: bool = False
    rest_style: str = "behind"          # "behind" | "join_quiet"
    # Size the chosen join up to the per-market money even when the
    # fill-cost term prefers dust (owner, 2026-08-23, NBA: "increase
    # the amounts... they are so small that they aren't earning
    # anything" — stability comes from the wall's queue, not tiny size,
    # and "obviously we'll get filled occasionally" is accepted).
    wall_size_up: bool = False
    revive: bool = False                # may qualify a below-target side
    vol_quiet: float = 0.15             # book EWMA below this = quiet enough to join
    # owner, 2026-08-29 ("this sort of strategy only obviously works
    # when fills are more rare", after showing shape-churn is blind to
    # take-and-refill snipers): ground our OWN fills mark hot may not
    # JOIN the touch — resting behind stays allowed. Heat is the
    # age-weighted own-fill measure evidence already keeps. 0 = off.
    touch_heat_max: float = 0.0
    # THE risk number: total collateral this family may hold at once.
    capital_usd: float = 25.0
    per_market_usd: float = 1.00        # both sides combined
    revive_max_usd: float = 5.0         # a revival order's own collateral cap
    share_hi: float = 0.10              # courtesy ceiling when others carry the side
    # optional weekly no-resting window (game days), ET (weekday, hour);
    # None = the family rests every day (politics)
    rest_from: tuple[int, int] | None = None
    rest_until: tuple[int, int] | None = None
    season_start: tuple[int, int, int] | None = None
    min_days_out: int = 3               # nothing resolving this week
    # Restrict NEW entries to slugs containing any of these tokens (None =
    # the whole universe). Exits, adopted orders, and dead handling are
    # never scoped — only where fresh money goes.
    enter_tokens: tuple[str, ...] | None = None
    max_actions_per_cycle: int = 6
    books_per_cycle: int = 16
    scan_reserve: int = 6
    book_stale_s: float = 150.0         # refresh an active market's book this often
    read_age_s: float = 480.0           # oldest book maintenance will read
    verify_resting: bool = False        # next cycle's reconcile checks by id anyway
    rescan_s: float = 4 * 3600.0        # full REFETCH cadence per market
    # Re-SCORING a market whose book is already fresh in the cache (the
    # stream keeps ~100 live) costs no API call at all — so it happens
    # every replan_s, keeps the triage feed genuinely live, and catches a
    # spread opening minutes after it appears instead of hours
    # (owner, 2026-08-21: "I don't see anything moving").
    replan_s: float = 0.0               # 0 = off
    replans_per_cycle: int = 40
    # The prober (owner, 2026-08-21: "there are markets we can earn in
    # that need probing for information. Unless you have all the
    # information you need, go out and get some"): a market whose pool
    # could clear the bar but whose evidence confidence is still low gets
    # a one-share scout behind the touch. Its job is the information —
    # what fills it, what ignores it — and it earns a trickle meanwhile.
    probe_usd: float = 0.0            # concurrent probe collateral (0 = off)
    probe_qty: float = 1.0
    # whole-share quoting: the owner is testing whether fractional-share
    # orders are even picked up by the rewards program (2026-08-21)
    whole_shares: bool = False
    # holdings count against the family ceiling at liquidation value
    # (owner, 2026-08-21: cfb risk = orders + holdings, capped)
    holdings_in_ceiling: bool = False
    # graduated markets may carry more money than searchers
    proven_per_market_usd: float | None = None
    probe_ttl_s: float = 45 * 60.0    # rotate: 45 quiet minutes IS the datum
    probe_cooldown_s: float = 6 * 3600.0
    probe_conf: float = 0.5           # below this confidence, information pays
    probes_per_cycle: int = 1
    # Owner, 2026-08-21: "it's okay to get filled at reasonable prices."
    # When the touch sits at least join_edge_ticks INSIDE what the market
    # is worth (the Silver+evidence blend), joining it needs no quiet-book
    # proof — a fill there is a purchase at better than value, not a loss.
    # And the courtesy share cap lifts continuously with that same edge,
    # from share_hi up to share_max at four-plus ticks of edge. No edge
    # information (no model, no evidence) = the timid defaults stand.
    join_edge_ticks: float | None = None
    share_max: float = 0.10           # == share_hi means no lift
    # Growth investing (owner, 2026-08-21: "take the 75 cents per day as
    # a GOAL and if it's not doable at first, invest in the markets where
    # growth after building confidence is possible"). A market that can't
    # clear the goal at today's confidence, but WOULD at full confidence
    # (touch unlocked, full share cap, evidence-width bounds), gets a
    # starter position from its own budget. Its resting and its fills are
    # the evidence that grows the confidence that clears the goal.
    grow_usd: float = 0.0             # 0 = growth investing off
    grow_floor: float = 0.10          # a growth order must still earn this
    grow_pull_s: float = 1800.0       # under its floor this long -> out
    # College only: may price IN FRONT of a junk touch (wall-only books).
    # The owner kept college's launch behavior ("I wouldn't change anything
    # for now"); every other family leaves this off.
    allow_improve: bool = False
    # Take over resting orders already on the account in this family's
    # markets (the 1.0/2.0 handover). Owner-placed manual orders are never
    # claimed.
    adopt: bool = True
    terms_slice: int = 120              # universe terms slugs per full-refresh pass
    cooldown_s: float = 3600.0
    min_est_day: float = 0.02
    # Decisions divide every reward claim by this before acting on it
    # (owner, 2026-08-25: politics estimates measured 3.3-7.1x what the
    # exchange actually paid, so the engine acts on the deflated number
    # while the grades and ledgers stay raw). 1.0 = trust the estimate.
    # Owner's revisit trigger: "If we get to the point where we're
    # barely quoting, let's revisit."
    est_deflate: float = 1.0
    # Expected-risk budgeting is OPT-IN per family (owner, 2026-08-25:
    # "the cap should stay the same for everything except for
    # politics"). Off, the caps charge full collateral exactly as they
    # always did. On, orders charge collateral x fill odds and the
    # gross ceilings bound the worst day.
    expected_risk: bool = False
    gross_cap_usd: float = 0.0     # 0 = 2x capital (expected_risk only)
    per_market_gross_usd: float = 0.0   # 0 = 3x per-market
    # cap on the TOTAL give-up (price past break-even x size) the
    # family's exits may have in play at once — the belt on the exit
    # gate, so twenty small approved risks cannot add up quietly
    exit_giveup_cap_usd: float = 5.0
    # A SMALL exit — one whose whole give-up is under this — rests at
    # the touch OUTSIDE that budget (owner, 2026-09-02, from the cfb
    # week-1 read: a 2-share Kansas cover measured at $6.61/day and 88%
    # of its side was pulled 8 ticks back to save 16 cents the hour the
    # $5 budget filled). The reward must still beat the fill loss by
    # the gate's margin; only the family-wide budget stops applying,
    # and small exits neither draw on it nor count against it.
    exit_small_giveup_usd: float = 0.50
    # Cycle-out rule (owner, 2026-08-20: "be very picky... and if
    # something's not working cycle out of it"): an order measured under
    # min_est_day for this long, with no plan at this market that clears
    # the bar either, is pulled so the capital can go to the next best
    # market. 0 disables.
    weak_pull_s: float = 0.0
    # Graduation (owner, 2026-08-21, the v1 pattern): a market whose
    # orders have MEASURED real accrual today moves off the search
    # ceiling onto the proven pool's own cap, so the search money keeps
    # hunting new candidates. Membership is recomputed from the sampler
    # every cycle — a market that stops accruing falls back in.
    graduate_paid_usd: float = 0.25   # avg PAID $/day over recent paid days
    graduate_days: int = 3            # paid days needed in the last 7 (stability)
    dump_usd_day: float = 0.0         # taker-dump proceeds allowed per day (0 = off)
    dead_drain_s: float = 0.0         # drain stock whose exits measured ~0 this long (0 = off)
    avoid_tokens: tuple = ()
    # races the owner told us to keep a live watch on (2026-08-28,
    # the MA primaries + balance of power: "Keep a websocket on those
    # races"): their books refresh on a budget-exempt fast lane every
    # cycle and seat first in the stream subscription, so the sampler,
    # the nurse, the live cards and the qualification reads are never
    # stale there. Watching only — placement rules are untouched.
    watch_tokens: tuple = ()
    # markets the owner ordered CLOSED (2026-08-27, DeSantis 2028:
    # "Take me out of all buy position"): the engine sells the held
    # stock into the bid — never worse — up to the bid's displayed
    # size each cycle until flat, cancels its own resting orders
    # there first, and never opens a BUY. Manual orders are never
    # touched. Matching is by slug fragment, like avoid_tokens.
    liquidate_tokens: tuple = ()
    # FROZEN ground: the engine does nothing here at all — places no
    # orders, rests no exits, reprices nothing, cancels nothing. Every
    # order in a frozen market is treated exactly like one the owner
    # placed by hand (owner, 2026-08-24: "Don't sell my gop governor
    # count race orders. In fact don't touch those"). Different from
    # avoid_tokens, which PULLS the engine's orders out; freezing
    # leaves the book exactly as it stands.
    freeze_tokens: tuple = ()          # slug fragments the owner told us to stay out of
    proven_usd: float = 0.0           # 0 = graduation off
    reprice_gain_day: float = 0.06
    drift_share: float = 0.15
    terms_active_s: float = 600.0       # live terms for markets we're in
    terms_full_s: float = 3600.0        # the whole universe's terms
    discover_s: float = 6 * 3600.0
    log_keep: int = 300


@dataclass
class FamilyOrder:
    id: str
    market: str
    side: str            # book side: BUY bid / SELL ask
    price: float
    qty: float
    intent: str
    placed_ts: float
    purpose: str         # earn / revive / sell
    why: str = ""        # plain-English placement reason, shown on pages
    est_day: float = 0.0
    share: float = 0.0
    live_est: float | None = None
    dry_since: float | None = None   # when it last stopped earning
    live_pf: float | None = None     # measured fill odds; None = charge full
    live_ev: float | None = None
    live_share: float | None = None
    pinned: bool = False      # hand-set from the live card — engine hands off
    pin_est: float = 0.0      # $/day baseline for the release rule
                              # (-1 = measure on the first read after the change)
    pin_ts: float = 0.0       # when he set it (starts the nurse's watch)
    pin_weak_since: float = 0.0   # under the release line since (0 = fine)
    weak_since: float = 0.0   # measuring under the bar since (0 = fine)
    rest_noted: float = 0.0   # last time quiet resting was logged as evidence
    verdict: str = ""    # plain-English live state, refreshed each cycle
    # the 8-hour earning trail (owner, 2026-08-26: "keep track of the
    # percentage decrease in rewards from an 8 hour peak"): half-hour
    # buckets of the order's best measured $/day, oldest dropped past
    # 8h. est_peak8 is the window's max, refreshed with every read.
    est_hist: list = field(default_factory=list)   # [[bucket_ts, max_est]..]
    est_peak8: float = 0.0
    # the fill model's evidence (owner, 2026-09-04): where the order sat
    # at its last look, and when that look was, so its resting time
    # accrues to the right distance cell and a fill lands in the same one
    ticks_last: int = 0
    seen_ts: float = 0.0


def resting_ok(now: float, cfg: FamilyConfig) -> bool:
    """Inside the family's resting window? No window means every hour is a
    resting hour. Before season_start there are no game days."""
    if cfg.rest_from is None or cfg.rest_until is None:
        return True
    t = dt.datetime.fromtimestamp(now, ET)
    if cfg.season_start is not None and t.date() < dt.date(*cfg.season_start):
        return True
    m = t.weekday() * 24 + t.hour
    a = cfg.rest_from[0] * 24 + cfg.rest_from[1]
    b = cfg.rest_until[0] * 24 + cfg.rest_until[1]
    if a <= b:
        return a <= m < b
    return m >= a or m < b


def slug_days_out(slug: str, now: float) -> int | None:
    parts = (slug or "").split("-")
    for i in range(len(parts) - 2):
        if (parts[i].isdigit() and len(parts[i]) == 4
                and parts[i + 1].isdigit() and parts[i + 2].isdigit()):
            try:
                when = dt.date(int(parts[i]), int(parts[i + 1]), int(parts[i + 2][:2]))
            except ValueError:
                return None
            return (when - dt.datetime.fromtimestamp(now, ET).date()).days
    return None


def _et_day(now: float) -> str:
    return dt.datetime.fromtimestamp(now, ET).date().isoformat()


class Family:
    """One market category: its universe, terms, books, orders, and money.

    `discover(client) -> dict[slug, {"event_n": int, "name": str, ...}]`
    is injected; everything the family knows starts there. The desk's
    whitelist should be wired to `knows()` so no order can leave the
    family's own ground.
    """

    def __init__(self, desk: OrderDesk, cache: BookCache, discover,
                 config: FamilyConfig | None = None, alert=None,
                 names=None, clock=None):
        self.desk = desk
        self.cache = cache
        self.discover = discover
        self.cfg = config or FamilyConfig()
        self.alert = alert or (lambda title, msg: None)
        self.names = names
        self.fairs = None      # callable(slug) -> model fair prob | None
        self.evidence = Evidence(clock=clock)
        self.fillmodel = FillModel()
        self.pending_marks: list[dict] = []   # fills awaiting their 1h grade
        # what the model EXPECTED, accrued as the orders actually rested
        # (owner, 2026-09-04): hour bucket -> purpose -> expected fills.
        # The calibration note grades this against the fills that came,
        # instead of a snapshot of whatever happens to rest right now.
        self.exp_fills: dict[str, dict[str, float]] = {}
        # slug|side -> (price, ts, qty): the price a cancel-then-re-rest
        # promised for that size, consumed by the re-rest in the same pass
        self._replace_at: dict[str, tuple[float, float, float]] = {}
        self.fills: list[dict] = []           # the purchase journal, one row per fill
        self.proven: set[str] = set()         # graduated markets (main feeds it)
        self.recent_paid: dict[str, tuple] = {}   # mkt -> (avg $/day, paid days), last 7d
        self.inv_since: dict[str, float] = {}  # market -> first-fill ts
        self._exit_rate_ps = 0.0               # $/share/day our exits earn
        self.triage_feed: list[dict] = []     # the sweep's recent verdicts
        self._clock = clock or time.time
        self.terms = TermsStore()
        self.universe: dict[str, dict] = {}       # slug -> {event_n, ...}
        self.orders: dict[str, FamilyOrder] = {}
        self.history: dict[str, float] = {}       # slug -> avg $/day actually PAID
        self.inventory: dict[str, dict] = {}      # slug -> {qty, cost}
        self.positions_seen: dict[str, float] = {}
        self.scoreboard: dict[str, dict] = {}     # slug -> {ts, plans, why...}
        self.last_action: dict[str, float] = {}   # "slug|side" -> ts
        self.known_dead: set[str] = set()          # program read as gone
        self.seen_pids: set[str] = set()           # program ids ever seen
                                                   # (the boost watch)
        self.last_discover = 0.0
        self.last_terms_active = 0.0
        self.last_terms_full = 0.0
        self._terms_rotor = 0
        self.earned_today = 0.0
        self.earned_day = ""
        self.dump_today = 0.0                     # taker-dump proceeds today
        # the wind-down ledger (owner, 2026-08-31: "you can fix so
        # there is a more clear answer"): every position the engine
        # actually retires, with what it fetched and how. Position
        # COUNTS alone could not tell a sale from a fill; this is the
        # record of the selling itself, kept across restarts.
        self.wind_down: list[dict] = []
        self.earned_history: list[list] = []      # [day, $] rolling
        self._last_accrual = 0.0
        self.silent_cancels = 0
        self.gone_pending: dict[str, dict] = {}   # vanished, feed pending
        self.probe_ratchet: dict[str, list] = {}  # "slug|side" ->
                                                  # [ticks allowed, last advance ts]
        self._nurse_base: dict[str, dict] = {}    # young orders' first-
                                                  # seen book, for the nurse
        self._fill_evi_buf: list[dict] = []       # this cycle's fills, fed
                                                  # to evidence as EVENTS
                                                  # (sweeps collapse to one)
        # order id -> when WE placed it. The audit log keeps 60 rows, so
        # a fill recovered from the exchange later had no placement time
        # and its resting period was unknowable (owner, 2026-08-23:
        # "can't you match up the placement time with the execution
        # time to get an exact resting period?" — yes, with this).
        self.placed_at: dict[str, float] = {}
        # every position move the feed showed, [ts, market, delta]: the
        # hourly journal reconciliation reads the DIRECTION of a hand
        # trade off the move nearest its time (owner, 2026-09-02)
        self.pos_moves: list[list] = []
        # frozen ground set at runtime: the owner's bond markets (owner,
        # 2026-09-02). Same rule as freeze_tokens — the engine places
        # nothing, rests no exits, sells nothing there.
        self.freeze_dyn: set[str] = set()
        # a timed hold: the engine does nothing in the market until the
        # time given (owner, 2026-09-03: "when you clear out, it might
        # make sense to tell the engine to hold off on that market for
        # 10 minutes") — set by the bonds module before a take
        self.hold_until: dict[str, float] = {}
        # the owner's say over the game window (2026-09-04: "Cfb can go
        # active until 5:00 pm eastern today"): until this time the
        # family rests as in resting hours, whatever the window says
        self.active_until: float = 0.0
        # the owner's bond shares per market, signed YES (owner,
        # 2026-09-02: "the engine does not need to ignore these
        # markets, only the orders I place"; "I only want to know for a
        # market what the bond purchases are"): the engine quotes those
        # markets as normal and exits only the stock that is NOT bond
        self.bond_qty: dict[str, float] = {}
        self.priority: set = set()   # markets to re-check first
        self.pending_pages: list = []   # open fills awaiting a mark
        self.log: list[dict] = []

    # ------------------------------------------------------------- helpers

    def _label(self, slug: str) -> str:
        return self.names.label(slug) if self.names is not None else slug

    def _log(self, **row) -> None:
        row.setdefault("ts", round(self._clock(), 1))
        self.log.append(row)
        del self.log[:-self.cfg.log_keep]

    def _avoided(self, slug: str) -> bool:
        """Markets the owner told us to stay out of (2026-08-22: Alaska
        governor, special rules pending). Exits still manage held stock;
        nothing new rests, probes, revives, or dumps here."""
        return any(t in slug for t in self.cfg.avoid_tokens)

    def _watched(self, slug: str) -> bool:
        return any(t in slug for t in self.cfg.watch_tokens)

    def _liquidating(self, slug: str) -> bool:
        """Owner-ordered close-out ground: sell the stock into the
        bid until flat, rest nothing new, never buy."""
        return any(t in slug for t in self.cfg.liquidate_tokens)

    def _frozen(self, slug: str) -> bool:
        """Hands off entirely (owner, 2026-08-24: "don't touch those").
        Unlike an avoided market, nothing is pulled: whatever rests
        here stays exactly as it is, and the engine adds nothing."""
        return (slug in self.freeze_dyn
                or self.hold_until.get(slug, 0.0) > self._clock()
                or any(t in slug for t in self.cfg.freeze_tokens))

    def enterable(self, slug: str) -> bool:
        if self._avoided(slug) or self._frozen(slug) \
                or self._liquidating(slug):
            return False
        toks = self.cfg.enter_tokens
        return toks is None or any(t in slug for t in toks)

    def knows(self, slug: str) -> bool:
        """This family's ground: discovered markets, plus anything we
        already hold orders or stock in (so exits always stay legal)."""
        return (slug in self.universe or slug in self.inventory
                or any(o.market == slug for o in list(self.orders.values())))

    def _cooldown_ok(self, slug: str, side: str, now: float) -> bool:
        return now - self.last_action.get(f"{slug}|{side}", 0.0) >= self.cfg.cooldown_s

    def _mark(self, slug: str, side: str, now: float) -> None:
        self.last_action[f"{slug}|{side}"] = now

    # Only these put money IN. A short step-up moves a buy-back order
    # up the book: no shares change hands, nothing closes, and when it
    # does fill the cash goes the other way. Counting those as sales
    # made 42 of 51 lines read as sold and overstated a day's proceeds
    # by two thirds — $16.01 against a real $9.82 (owner, 2026-08-31:
    # "Fix all of this").
    SALE_KINDS = ("close-out", "drain", "dump")

    @classmethod
    def _wd_sale(cls, w: dict) -> bool:
        """Was this ledger line money coming in? Rows written before
        the flag existed carry none, so they are classed by kind and
        the ledger still reads right across the restart that ships
        this."""
        if "sale" in w:
            return bool(w["sale"])
        return w.get("kind") in cls.SALE_KINDS

    def _note_wind_down(self, slug: str, kind: str, qty: float,
                        px: float, now: float, left: float = 0.0,
                        from_px: float | None = None,
                        gain: float | None = None) -> None:
        """One line in the wind-down ledger: stock actually sold, a
        short's buy-back stepped toward filling, or an owner close-out.
        `left` is what remains of the position after it, so the report
        can say how many went fully flat. Only a SALE can go flat — a
        repricing leaves the position exactly where it was, so it never
        counts as one however `left` was passed."""
        sale = kind in self.SALE_KINDS
        row = {
            "ts": round(now, 1), "market": slug, "kind": kind,
            "qty": round(float(qty), 2), "px": round(float(px), 4),
            "usd": round(float(qty) * float(px), 2),
            "sale": sale,
            "flat": sale and abs(float(left)) < 0.01}
        # a repricing carries where it came from and what the model
        # expects the move to add per day, so the report can say what
        # was gained instead of listing the moves (owner, 2026-08-31)
        if from_px is not None:
            row["from_px"] = round(float(from_px), 4)
        if gain is not None:
            row["gain"] = round(float(gain), 4)
        self.wind_down.append(row)
        del self.wind_down[:-400]

    def _charge(self, o) -> float:
        """What one order costs the EXPECTED-risk budget: collateral x
        its measured fill odds (owner, 2026-08-25). Floored at
        PF_CHARGE_FLOOR so near-zero odds cannot stack unbounded size;
        an UNMEASURED order charges in full — optimism is earned."""
        car = capital_at_risk(o.intent, o.price, o.qty)
        if not self.cfg.expected_risk:
            return car             # the old accounting, untouched
        pf = getattr(o, "live_pf", None)
        if pf is None:
            return car
        return car * min(max(pf, PF_CHARGE_FLOOR), 1.0)

    def gross_cap(self) -> float:
        if not self.cfg.expected_risk:
            return self.cfg.capital_usd    # one cap, as it always was
        return self.cfg.gross_cap_usd or 2.0 * self.cfg.capital_usd

    def _per_market_gross(self, slug: str) -> float:
        if not self.cfg.expected_risk:
            return self._market_budget(slug)
        if self.cfg.per_market_gross_usd:
            return self.cfg.per_market_gross_usd
        return 3.0 * self._market_budget(slug)

    def market_spent(self, slug: str) -> float:
        """Expected risk resting in one market."""
        return sum(self._charge(o)
                   for o in list(self.orders.values())
                   if o.market == slug and o.purpose != "sell")

    def market_gross(self, slug: str) -> float:
        return sum(capital_at_risk(o.intent, o.price, o.qty)
                   for o in list(self.orders.values())
                   if o.market == slug and o.purpose != "sell")

    def _market_budget(self, slug: str) -> float:
        """Proven ground earns a bigger allowance (owner, 2026-08-21)."""
        if slug in self.proven and self.cfg.proven_per_market_usd:
            return self.cfg.proven_per_market_usd
        return self.cfg.per_market_usd

    def family_spent(self) -> float:
        """The search ceiling's number, in EXPECTED risk: each order
        charges collateral x fill odds (owner, 2026-08-25 — "make the
        budget take into account the fill risk"). Holdings are real
        positions and charge in full. family_gross() bounds the worst
        day in nominal dollars behind this."""
        spent = sum(self._charge(o)
                    for o in list(self.orders.values())
                    if o.market not in self.proven
                    and not self._owner_exit(o))
        if self.cfg.holdings_in_ceiling:
            spent += self.holdings_value()
        return spent

    def family_gross(self) -> float:
        """Worst-case nominal collateral of the whole engine book —
        negative risk netted — bounding a correlated day no matter what
        the fill model believes."""
        g = risk.book_risk(risk.order_legs(
            o for o in list(self.orders.values())
            if not self._owner_exit(o)))
        if self.cfg.holdings_in_ceiling:
            g += self.holdings_value()
        return g

    def _owner_exit(self, o) -> bool:
        """The owner's own order. It never counts against a ceiling.

        The family budget limits what the ENGINE puts at risk on its
        own initiative. The owner sizes his own book, and standing
        instruction is that the engine neither touches it nor is
        credited for it — so spending it against his cap is charging
        the engine for money it did not commit.

        Was reduce-side manual orders only, on the reasoning that
        those add no new risk. On 2026-08-24 the manual orders became
        VISIBLE for the first time (before that the exchange's MANUAL
        flag made them invisible to the whole engine), and the
        risk-opening ones alone measured $484.66 against a $250
        politics cap — 194% of the budget, spent entirely by the
        owner. The engine was locked out: 0 entry orders, $4.27 of
        stale exits, and the lowest earning rate on record beside the
        highest budget utilisation on record. The owner spotted the
        contradiction before I did.

        Owner, 2026-08-24, on excluding all of them: "That's good."

        The bond program's orders are his money too (2026-09-04): he
        approves each market and sets each cap, and the program keeps
        its own ledger. On 2026-09-04 a $300 Illinois bond bid in a
        market not yet graduated landed $114 of expected risk on the
        engine's $250 search ceiling; the trimmer cut the engine's own
        politics book from 50 orders to 11 in an evening. Owner: "Yes
        to #1" — bond orders charge no engine ceiling."""
        return o.purpose in ("manual", "bond")

    def holdings_value(self) -> float:
        """What the stock would fetch if liquidated NOW: longs at the
        best bid, shorts at what closing them recovers (owner,
        2026-08-21 evening: this number counts against the family
        budget — 'no more than $50 of risk in cfb, orders + holdings').
        A market with no book values conservatively at cost."""
        total = 0.0
        for slug, inv in list(self.inventory.items()):
            qty = inv.get("qty") or 0.0
            if abs(qty) < 0.005:
                continue
            book = self.cache.any_age(slug)
            if qty > 0:
                if book is not None and book.bids:
                    total += qty * book.bids[0][0]
                else:
                    total += max(inv.get("cost", 0.0), 0.0)
            else:
                if book is not None and book.asks:
                    total += -qty * (1.0 - book.asks[0][0])
                else:
                    total += max(-inv.get("cost", 0.0), 0.0)
        return total

    def proven_spent(self) -> float:
        return sum(self._charge(o)
                   for o in list(self.orders.values())
                   if o.market in self.proven and not self._owner_exit(o))

    def active_markets(self) -> set[str]:
        return {o.market for o in list(self.orders.values()) if o.purpose != "sell"}

    def _dead_here(self, slug: str) -> bool:
        """Program known dead: read as paying nothing, or read as GONE
        (absent from the incentives response — the store drops the record,
        so gone markets are remembered here). A market never successfully
        read is NOT dead — no data is no verdict."""
        prog = self.terms.get(slug)
        if prog is None:
            return slug in self.known_dead
        return not prog.is_live() or not prog.pool

    def _prog_row(self, slug: str):
        """The program to score against, or (None, why-not)."""
        prog = self.terms.get(slug)
        if prog is None:
            return None, "no reward terms read yet"
        if not prog.is_live() or not prog.pool:
            return None, "program pays nothing"
        if not prog.df or not prog.target:
            return None, "terms incomplete (no df or Target Size)"
        return prog, ""

    def _side_pool(self, slug: str, prog) -> float | None:
        """$/day one side competes for — or None when the event divisor is
        unconfirmed (then NOTHING shows a dollar figure; owner: "don't
        estimate until you have a grasp of everything you need to know")."""
        u = self.universe.get(slug) or {}
        n = u.get("event_n")
        if not n:
            return None
        # daily_pool, not pool: a bounded program's rewardPool covers
        # its whole period, so a tournament-length one read as daily
        # overstates by the length of the event (owner, 2026-08-31).
        # Every live program in our four families is open-ended, so this
        # is the same number for them.
        return (prog.daily_pool or 0.0) / max(int(n), 1) / 2.0

    # ------------------------------------------------------------ discovery

    def refresh_universe(self, client, now: float) -> None:
        if now - self.last_discover < self.cfg.discover_s and self.universe:
            return
        self.last_discover = now
        try:
            found = self.discover(client) or {}
        except Exception as e:  # noqa: BLE001 — keep the old universe
            self._log(event="discover_error", error=str(e)[:80])
            return
        fresh = set(found) - set(self.universe)
        self.universe = found
        if self.names is not None:
            for slug, row in found.items():
                if row.get("name"):
                    self.names.learn(slug, {"title": row["name"]})
        if fresh:
            self._log(event="discovered", n=len(found), new=len(fresh))

    def refresh_terms(self, client, now: float) -> None:
        """Two cadences: markets we're in (fast), the whole universe in a
        rotating slice (slow). Every requested slug is force-present in
        the raw map so 'absent from the incentives response' reads as
        program-gone — first reading acts (owner: "Don't hold the dead
        market scan"). A failed fetch changes nothing (data safety)."""
        batch: list[str] = []
        if now - self.last_terms_active >= self.cfg.terms_active_s:
            self.last_terms_active = now
            batch += sorted(self.active_markets() | set(self.inventory))
        if now - self.last_terms_full >= self.cfg.terms_full_s and self.universe:
            # markets whose terms were NEVER read come first — a restart
            # must not send the rotation back to the top of the alphabet
            # while whole families (the Aug-20 seat-count arrivals) sit
            # unread at the bottom of it
            slugs = sorted(self.universe,
                           key=lambda s: (s in self.terms.current
                                          or s in self.known_dead, s))
            take = self.cfg.terms_slice
            lo = self._terms_rotor % max(len(slugs), 1)
            batch += (slugs[lo:lo + take] + slugs[:max(0, lo + take - len(slugs))])
            self._terms_rotor = (lo + take) % max(len(slugs), 1)
            if self._terms_rotor < take:
                self.last_terms_full = now   # a full lap is done
        batch = list(dict.fromkeys(batch))
        if not batch:
            return
        try:
            raw = client.programs(batch)
        except Exception as e:  # noqa: BLE001 — aged terms beat no terms
            self._log(event="terms_error", error=str(e)[:80])
            return
        for slug in batch:
            raw.setdefault(slug, {})
        sizes = {s: int((self.universe.get(s) or {}).get("event_n") or 0) or 1
                 for s in batch}
        changes = self.terms.refresh(raw, sizes, now=now)
        # the store only keeps LIVE programs; a slug we asked about that
        # ends up without one was read-and-programless — dead ground until
        # a later read finds a program (the seat-count families read empty
        # on 2026-08-21 but were shown as "not read yet" forever)
        for slug in batch:
            if slug in self.terms.current:
                self.known_dead.discard(slug)
            else:
                self.known_dead.add(slug)
        fresh_pids: dict[str, str] = {}
        for ch in changes:
            if ch.field == "program_gone":
                self.known_dead.add(ch.slug)
            elif ch.field == "program_new":
                self.known_dead.discard(ch.slug)
            if ch.field in ("pool", "program_gone", "program_new"):
                self._log(event="terms_change", market=ch.slug, field=ch.field,
                          old=str(ch.old), new=str(ch.new))
                self.alert(f"{self.cfg.tag}: reward pool change",
                           f"{self._label(ch.slug)}: {ch.field} "
                           f"{ch.old} -> {ch.new}")
            prog_ch = self.terms.get(ch.slug)
            if (prog_ch is not None and prog_ch.pid
                    and prog_ch.pid not in self.seen_pids):
                self.seen_pids.add(prog_ch.pid)
                if prog_ch.pool >= 100.0 or "boost" in prog_ch.pid.lower():
                    fresh_pids.setdefault(prog_ch.pid, ch.slug)
        # THE BOOST WATCH (owner yes, 2026-08-30, after the MA MoV
        # boost paid $199.65 on its first walled day and was only
        # caught by his screenshot): the first sighting of a program
        # id with a fat pool (>=$100/day per event) or a boost-flavored
        # name alerts ONCE per program — the play is to wall it within
        # hours, and hours are what the alert buys.
        for pid_b, slug_b in fresh_pids.items():
            prog_b = self.terms.get(slug_b)
            if prog_b is None:
                continue
            n_in = sum(1 for p2 in self.terms.current.values()
                       if p2.pid == pid_b)
            self._log(event="boost_watch", market=slug_b, note=pid_b)
            self.alert(
                f"{self.cfg.tag}: NEW fat reward program",
                f"{pid_b}: ${prog_b.pool:g}/day per event, target "
                f"{prog_b.target:g}, df {prog_b.df:g} — {n_in} of your "
                f"markets carry it (e.g. {self._label(slug_b)}). "
                f"The MoV play: wall the sides early.")
        if not self.seen_pids:
            # first run with no saved memory: today's programs are the
            # baseline, never an alert storm
            self.seen_pids = {p.pid for p in self.terms.current.values()
                              if p.pid}

    # ------------------------------------------------------------- planning

    def _plan_side(self, slug: str, book, side: str, prog,
                   side_pool: float | None, budget: float,
                   own: FamilyOrder | None = None, bar: float | None = None,
                   full_confidence: bool = False,
                   cross_px: float | None = None,
                   ladder: list | None = None) -> dict | None:
        """The best resting order for one side, or None. Every plan and
        every refusal is phone-readable."""
        df, target = float(prog.df), float(prog.target)
        if side_pool is not None and self.cfg.est_deflate > 1.0:
            # every plan, bar test and EV in here runs on the DEFLATED
            # claim — the number the engine acts on. The estimator's
            # accrual and the calibration ledger stay raw so the bias
            # keeps being measured, not hidden.
            side_pool = side_pool / self.cfg.est_deflate
        levels = list(book.side(side))
        if own is not None:
            levels = [(p, q - own.qty if abs(p - own.price) < 1e-9 else q)
                      for p, q in levels]
            levels = [(p, q) for p, q in levels if q > 1e-9]
        side_name = "bid" if side == "BUY" else "ask"
        sign = 1.0 if side == "BUY" else -1.0
        tick = book.tick
        other = book.side("SELL" if side == "BUY" else "BUY")
        side_total = sum(q for _, q in levels)

        if side_pool is None:
            return None     # divisor unconfirmed: no estimate, no order

        # -- a side below Target Size pays nobody: skip it, or revive it --
        if side_total < target:
            if not (self.cfg.revive and self.cfg.known_ground):
                return None
            gap = target - side_total
            if not levels and not other:
                return None                       # empty book, no anchor
            anchor = (levels[0][0] if levels
                      else other[0][0] - sign * 5 * tick)
            qty = round(gap * 1.02 + 1.0, 2)
            if self.cfg.whole_shares:
                qty = float(math.ceil(qty))
            best = None
            r_lo, r_hi = self._price_bounds(
                slug, levels if side == "BUY" else other,
                other if side == "BUY" else levels, tick)
            fair_rv = self.fairs(slug) if self.fairs is not None else None
            for k in (0, 1, 2, 3):
                px = round(anchor - k * sign * tick, 3)
                if not (0.001 <= px <= 0.999):
                    continue
                if other and (px >= other[0][0] - 1e-9 if side == "BUY"
                              else px <= other[0][0] + 1e-9):
                    continue
                if fair_rv is not None and (
                        (side == "BUY" and px > fair_rv - tick + 1e-9)
                        or (side == "SELL"
                            and px < fair_rv + tick - 1e-9)):
                    continue    # the hard cap binds revives too, both
                                # sides (owner, 2026-08-23)
                if fair_rv is None:
                    cap_rv = self._evidence_cap(slug, side, r_lo, r_hi)
                    if cap_rv is not None and (px - cap_rv) * sign > 1e-9:
                        continue    # and so does the evidence cap
                                    # (owner, 2026-08-25): kamhar's 284
                                    # shares at 13c against a 5-12c
                                    # band came through THIS door
                cost = qty * (px if side == "BUY" else 1.0 - px)
                # a revival is bigger than an earn order by nature — its own
                # cap applies, not the per-market split; the family ceiling
                # still binds at placement
                if cost > self.cfg.revive_max_usd + 1e-9:
                    continue
                j = estimate_join(side, levels, tick, df, target, px, qty)
                if not (j.qualifies and j.in_window):
                    continue
                est = j.share * side_pool
                k_r = round(abs(((levels[0][0]) if levels else px) - px) / tick)
                r_ctr = None
                if r_lo is not None and r_hi is not None:
                    r_ctr = (r_lo + r_hi) / 2.0
                else:
                    r_ctr = r_hi if r_hi is not None else r_lo
                conc_r = 0.0
                if r_ctr is not None:
                    past = (px - r_ctr) if side == "BUY" else (r_ctr - px)
                    conc_r = max(past / tick, 0.0)
                pf_r = self.fillmodel.p_fill(slug, side, k_r, target=target,
                                             bait=conc_r)
                fc_r = self.fillmodel.fill_cost(slug, side, px, r_ctr,
                                                exit_rate_ps=self._exit_rate_ps)
                ev = (est * self.fillmodel.scoring_fraction(slug)
                      - pf_r * fc_r * qty
                      - cost * self._capital_charge_rate(slug))
                if best is None or ev > best["ev"]:
                    best = {"side": side, "px": px, "qty": qty,
                            "share": round(j.share, 4), "est": round(est, 4),
                            "ev": round(ev, 4), "p_fill": round(pf_r, 4),
                            "fill_cost": round(fc_r, 4),
                            "cost": round(cost, 2), "revive": True,
                            "why": (f"the {side_name} side holds "
                                    f"{side_total:,.0f} of {target:,.0f} Target"
                                    f" Size and pays NOBODY — this order "
                                    f"revives it and takes ~"
                                    f"{j.share * 100:.0f}% of the side")}
            bar_here = self.cfg.min_est_day if bar is None else bar
            if best is not None and best["ev"] >= bar_here:
                return best
            return None

        if own is not None and own.side == side:
            # net OUR OWN resting order out of the book first — planning
            # against a touch that is just ourselves anchors on a ghost
            # (the Massachusetts primary lesson: our 13c bid kept
            # re-planning against itself)
            netted = []
            for p2, q2 in levels:
                if abs(p2 - own.price) < tick / 2:
                    q2 = q2 - own.qty
                if q2 > 1e-9:
                    netted.append((p2, q2))
            levels = tuple(netted)
            if not levels:
                return None
        # -- the side qualifies: join or step back, never in front --
        # Never plan against ourselves (the Massachusetts rule,
        # generalized on the owner's word 2026-08-21: "Aren't I just
        # bidding against myself?"). Every order WE have resting on this
        # side comes out of the book before the touch is read; the share
        # math below still uses the full book, because the program counts
        # our size like anyone else's.
        mine_orders = [o for o in list(self.orders.values())
                       if o.market == slug and o.side == side
                       and (own is None or o.id != own.id)]
        mine_at: dict[float, float] = {}
        for o in mine_orders:
            pk = round(o.price, 3)
            mine_at[pk] = mine_at.get(pk, 0.0) + o.qty
        others = []
        for p2, q2 in levels:
            q3 = q2 - mine_at.get(round(p2, 3), 0.0)
            if q3 > 1e-9:
                others.append((p2, q3))
        if not others:
            return None   # the only real orders on this side are ours
        touch = others[0][0]
        # Every price level is an option (owner, 2026-08-21): the EV math
        # walks the whole in-window ladder and picks the best spot.
        # Joining an occupied level is safer than the distance alone
        # says — fills are first-come-first-served, so the shares already
        # resting there absorb takers before ours. That protection is
        # priced into the fill odds (the queue shield), not a rule.
        # One fair price per market, everything through EV (owner,
        # 2026-08-21): the band — Silver prior pulled by fills, quiet
        # rests, and sized touch anchors — gives a single fair estimate.
        # There is no hard wrong-side rule. Resting past fair pays the
        # concession inside fill_cost and raises the assumed fill speed
        # (bait); if the reward still clears the bar, it is +EV and
        # allowed — on either side, both, or neither.
        b_lo, b_hi = self._price_bounds(
            slug, levels if side == "BUY" else other,
            other if side == "BUY" else levels, tick)
        # heat (recent fills through our orders) is not a gate — the
        # owner, 2026-08-21: "Fine to try and place again with a small
        # size to see if the taker has moved on." It raises the assumed
        # fill odds everywhere in this market and shrinks the retry size
        # at the front; both fade as the fills age out.
        h = self.evidence.heat(slug)
        value_ctr = None
        if b_lo is not None and b_hi is not None:
            value_ctr = (b_lo + b_hi) / 2.0
        elif b_hi is not None:
            value_ctr = b_hi
        elif b_lo is not None:
            value_ctr = b_lo
        # Edge must come from INDEPENDENT information. The market's own
        # touches feed the band, so without a model or real fills the
        # band's center is just the spread's midpoint — and the touch must
        # not certify itself. The model counts in full; without it, edge
        # scales with fill-built confidence, continuously.
        if full_confidence:
            independence = 1.0
        elif self.fairs is not None and self.fairs(slug) is not None:
            independence = 1.0
        else:
            independence = self.evidence.confidence(slug)

        def edge_ticks(px: float) -> float:
            """How far inside independent value a fill at px is, in ticks."""
            if value_ctr is None or independence <= 0.0:
                return 0.0
            e = (value_ctr - px) if side == "BUY" else (px - value_ctr)
            return max(e / tick, 0.0) * independence

        grid = (tuple(q for q in QTY_GRID if q >= 1.0)
                if self.cfg.whole_shares else QTY_GRID)
        rungs = tuple(range(0, 16))
        cands = []
        for k in rungs:
            px = round(touch - k * sign * tick, 3)
            if not (0.001 <= px <= 0.999):
                continue
            if other and (px >= other[0][0] - 1e-9 if side == "BUY"
                          else px <= other[0][0] + 1e-9):
                continue
            if px not in cands:
                cands.append(px)
        # In front of the touch, walk ONLY to the score frontier
        # (owner, 2026-08-21: "if you can get 100% of the score at 8
        # cents, why go to 9" — the question is 27 vs 28 vs 29, not 27
        # vs 44). Each next rung must materially improve the minimum-
        # size score share, or the walk stops: deeper adds fill risk
        # and a worse price for nothing.
        if other:
            best_share = 0.0
            for kf in range(1, 51):
                px = round(touch + kf * sign * tick, 3)
                if not (0.001 <= px <= 0.999):
                    break
                if (px - (other[0][0] - sign * tick)) * sign > 1e-9:
                    break
                j = estimate_join(side, levels, tick, df, target, px,
                                  grid[0])
                s = j.share if (j.qualifies and j.in_window) else 0.0
                if best_share > 0 and s <= best_share * 1.01 + 1e-9:
                    break         # no marginal score out here — pointless
                if s > best_share:
                    best_share = s
                if px not in cands:
                    cands.append(px)
        elif self.cfg.allow_improve and not other:
            # College's launch quirk, kept on the owner's word: a book
            # with NO opposing quote at all has no value anchor, so the
            # in-front rungs stay short and size is clamped to probe
            # money (in the qty grid).
            for k in (1, 5, 10):
                px = round(touch + k * sign * tick, 3)
                if not (0.001 <= px <= 0.999):
                    continue
                if (px - (touch + sign * 10 * tick)) * sign > 1e-9:
                    continue
                if px not in cands:
                    cands.append(px)
            budget = min(budget, 0.05)
        # THE HARD CAP (owner, 2026-08-23, both halves: "not paying so
        # much past value for underdogs. That includes selling the
        # favorites short"): on a MODELED market an earn quote never
        # rests past fair — a BUY stays at or under fair minus one
        # tick, a SELL at or over fair plus one tick, at any price.
        # No concession ladder, no earned-confidence override. The NY
        # governor short (sold 1 @ 91c against a 98.4c model, filled
        # in a minute) is what the SELL half prevents. Markets with no
        # model keep the ignorance premium and independence discount.
        fair_hard = self.fairs(slug) if self.fairs is not None else None
        if fair_hard is not None:
            if side == "BUY":
                cands = [px for px in cands
                         if px <= fair_hard - tick + 1e-9]
            else:
                cands = [px for px in cands
                         if px >= fair_hard + tick - 1e-9]
        else:
            # Owner, 2026-08-25: "Only fronting past fair where we have
            # no information, and only to the extent necessary to get
            # information that would allow us to earn." Past-value
            # fronting is an information TOOL, never an earning
            # strategy — the dwajoh bid (11c, 10 ticks in front, on a
            # market that had burned us repeatedly) is what this ends.
            has_info = any(r[1].startswith("fill")
                           for r in self.evidence.events.get(slug, ()))
            if has_info:
                # the market has spoken: NO earn order — joined,
                # behind, or in front — rests past the price the
                # evidence supports (owner, 2026-08-25, kamhar: the
                # join exemption was the one door left open)
                cap = self._evidence_cap(slug, side, b_lo, b_hi)
                if cap is not None:
                    cands = [px for px in cands
                             if (px - cap) * sign <= 1e-9]
                else:
                    cands = [px for px in cands
                             if (px - touch) * sign <= 1e-9
                             or (px - (touch + sign * tick)) * sign
                             <= 1e-9]
            else:
                # a truly blank market gets ONE minimum-size probe that
                # advances like a ratchet: a tick to start, one more per
                # quiet day survived, snapped shut by the first fill
                # (which makes the market has_info)
                key = f"{slug}|{side}"
                allowed = (self.probe_ratchet.get(key) or [1, 0.0])[0]
                already = any(
                    o for o in list(self.orders.values())
                    if o.market == slug and o.side == side
                    and o.purpose not in ("manual", "sell", "bond")
                    and (own is None or o.id != own.id)   # re-planning
                    and (o.price - touch) * sign > 1e-9)  # the probe
                                                          # itself must
                                                          # see its rungs
                if already:      # one probe at a time
                    cands = [px for px in cands
                             if (px - touch) * sign <= 1e-9]
                else:
                    lim = touch + sign * allowed * tick
                    cands = [px for px in cands
                             if (px - lim) * sign <= 1e-9]
        # Every candidate is priced by the owner's EV formula
        # (2026-08-19): what it earns while resting, minus what a fill
        # would probably cost.
        #     EV/day = est x scoring_fraction - p(fill) x fill_cost x size
        # Fill odds are learned per distance bucket from every touch move;
        # fill cost is the calibrated adverse markdown plus anything
        # conceded past value; depth ahead of the price shields the odds.
        if cross_px is not None:
            # our own opposite-side order rests at cross_px: stay a full
            # tick clear so the pair can never cross (post-only would
            # bounce the second placement)
            cands = [px for px in cands
                     if (px - (cross_px - sign * tick)) * sign <= 1e-9]
        sf = self.fillmodel.scoring_fraction(slug)
        exit_rate_ps = self._exit_rate_ps
        r_day = self._exit_opportunity_rate()
        r_tie = self._capital_charge_rate(slug)
        d_off = self.fillmodel.expected_offload_days(slug)
        inv_net = (self.inventory.get(slug) or {}).get("qty", 0.0)

        def _minus(lv, price, q0):
            out = []
            for p3, q3 in lv:
                if abs(p3 - price) < tick / 2:
                    q3 = q3 - q0
                if q3 > 1e-9:
                    out.append((p3, q3))
            return out

        est0 = 0.0
        for o in mine_orders:
            j0 = estimate_join(side, _minus(levels, o.price, o.qty), tick,
                               df, target, o.price, o.qty)
            if j0.qualifies and j0.in_window:
                est0 += j0.share * side_pool
        contenders: list[dict] = []
        for px in cands:
            cost_ps = px if side == "BUY" else 1.0 - px
            in_front = (px - touch) * sign > 1e-9
            k_px = 0 if in_front else round(abs(touch - px) / tick)
            if (self.cfg.touch_heat_max > 0.0
                    and (in_front or k_px == 0)
                    and h >= self.cfg.touch_heat_max):
                # hot ground (our own orders were recently taken here)
                # may not join or improve the touch — someone is eating
                # fresh quotes, and shape-churn cannot see it (owner,
                # 2026-08-29). Behind-the-touch candidates still score.
                continue
            shield = sum(q for p2, q in levels
                         if (p2 - px) * sign > 1e-9)
            queue = sum(q for p2, q in levels if abs(p2 - px) <= 1e-9)
            if (own is not None and own.side == side
                    and abs(own.price - px) <= 1e-9):
                queue = max(queue - own.qty, 0.0)
            shield += queue
            conc = 0.0
            if value_ctr is not None:
                past = (px - value_ctr) if side == "BUY" else (value_ctr - px)
                conc = max(past / tick, 0.0)
            if in_front and independence < 1.0:
                # with no independent sense of fair value, ticks in
                # front of the touch are assumed to be a gift to takers;
                # a model or fill-built confidence waives that in
                # proportion (owner, 2026-08-21: a 35c bid on a 50c-fair
                # market is a bargain for US, however far in front)
                conc = max(conc, (abs(px - touch) / tick)
                           * (1.0 - independence))
            # the ignorance premium (owner approved 2026-08-21): when we
            # cannot price the market, a fill's cost includes the
            # EXPECTED overpay if true fair lies anywhere in the spread
            # — zero at the touch, quadratic as we advance, gone when a
            # model grounds the market or fills build confidence
            ign = 0.0
            if independence < 1.0 and other:
                spread_w = abs(other[0][0] - touch)
                if spread_w > tick / 2:
                    adv = (px - touch) if side == "BUY" else (touch - px)
                    if adv > 0:
                        ign = ((1.0 - independence) * adv * adv
                               / (2.0 * spread_w))
            pf = self.fillmodel.p_fill(slug, side, k_px, shield=shield,
                                       target=target, bait=conc + h)
            fcost = self.fillmodel.fill_cost(slug, side, px, value_ctr,
                                             exit_rate_ps=exit_rate_ps,
                                             ignorance=ign)
            for qty in grid:
                if in_front and fair_hard is None and qty > PROBE_MAX_QTY:
                    break     # no model behind it: an in-front order is
                              # an experiment, and experiments run at
                              # probe size (owner, 2026-08-25)
                if (h >= 0.5 and qty > grid[0]
                        and (in_front or k_px == 0)):
                    break     # a fill just happened here: minimum size
                # a price past fair is a TARGET (owner, 2026-08-22:
                # "bigger size is fine so long as we can use some of the
                # spread to offset losses"): size shrinks with every tick
                # conceded — three or more ticks past value rests only
                # the minimum
                if conc >= 1.0 and qty > grid[0]:
                    if conc >= 3.0:
                        break
                    if qty > grid[0] * (8.0 if conc < 2.0 else 3.0):
                        break
                if qty * cost_ps > budget + 1e-9:
                    break
                j = estimate_join(side, levels, tick, df, target, px, qty)
                if not (j.qualifies and j.in_window):
                    break
                est = j.share * side_pool
                # marginal, not gross (owner, 2026-08-21): what this
                # order ADDS is its own score minus what it takes from
                # our other orders already resting on this side
                cann = 0.0
                if mine_orders:
                    cl = list(levels) + [(px, qty)]
                    est1 = 0.0
                    for o in mine_orders:
                        j1 = estimate_join(side, _minus(cl, o.price, o.qty),
                                           tick, df, target, o.price, o.qty)
                        if j1.qualifies and j1.in_window:
                            est1 += j1.share * side_pool
                    cann = max(est0 - est1, 0.0)
                # collateral tied while resting costs the marginal-cent
                # rate, scarcity-scaled. Freed capital counts ONLY in the
                # exit scorer — an earner gets NOTHING for freeing
                # capital (owner, 2026-08-22). Selling stock we already
                # hold still ties no new collateral; that is a fact, not
                # a credit.
                if side == "BUY":
                    tie = px * qty
                    if px >= 0.75:
                        # owner, 2026-08-23 ("Yes do both"): the
                        # expensive side is WANTED — a filled favorite
                        # resolves near $1 and always has an exit, so
                        # its locked-cash charge is halved and the EV
                        # ranking stops shying away from it
                        tie *= 0.5
                else:
                    sells = max(min(qty, inv_net), 0.0)
                    tie = (1.0 - px) * (qty - sells)
                ev = ((est - cann) * sf - pf * fcost * qty
                      - tie * r_tie)
                k = k_px
                kf = round(abs(px - touch) / tick)
                row = {"side": side, "px": px, "qty": qty,
                       "share": round(j.share, 4), "est": round(est, 4),
                       "ev": round(ev, 4), "p_fill": round(pf, 4),
                       "fill_cost": round(fcost, 4),
                       "cost": round(qty * cost_ps, 2),
                       "why": (f"at the touch — a fill here is "
                               f"{edge_ticks(px):.0f} ticks inside value "
                               f"({'Silver + evidence' if independence >= 1.0 else f'evidence band only, confidence {independence:.0%}'})"
                               if k == 0 and not in_front
                               and edge_ticks(px) >= 1 else
                               "joins the touch — the book has been quiet"
                               if k == 0 and not in_front else
                               f"{kf} tick{'s' if kf != 1 else ''} in front "
                               f"of the touch — we would hold "
                               f"{j.share * 100:.0f}% of the {side_name} side"
                               if in_front else
                               f"{k} tick{'s' if k != 1 else ''} behind the "
                               f"touch — we would hold "
                               f"{j.share * 100:.0f}% of the "
                               f"{side_name} side")}
                if ladder is not None:
                    ladder.append(dict(row))
                # No share cap (owner, 2026-08-21: "why would we cap the
                # total score we can claim? I don't agree with that. No
                # cap"). Size is bounded by the per-market money, the
                # family ceiling, the EV bar, and fill odds — nothing
                # else.
                contenders.append(row)
        the_bar = self.cfg.min_est_day if bar is None else bar
        live = [r for r in contenders if r["ev"] >= the_bar]
        if not live:
            return None
        best_ev = max(r["ev"] for r in live)
        # Near-tied EVs resolve to the most CONSERVATIVE spot — lowest
        # fill odds (owner, 2026-08-21: "the model is not precise
        # enough to make a big fuss over 1 cent of ev" — never take a
        # deeper price for the last penny).
        tol = max(0.01, 0.01 * best_ev)
        close = [r for r in live if r["ev"] >= best_ev - tol]
        pick = min(close, key=lambda r: (r["p_fill"], -r["ev"]))
        if self.cfg.wall_size_up:
            # same price level, biggest size the budget allows: the
            # modeled fill cost that shrank it is an accepted cost here
            same_px = [r for r in contenders
                       if abs(r["px"] - pick["px"]) < 1e-9
                       and r["est"] > 0.0]
            if same_px:
                pick = max(same_px, key=lambda r: r["qty"])
        return pick

    def lite_recalc(self, slug: str, bb: float | None,
                    ba: float | None) -> dict | None:
        """Our estimated $/day in this market IF scoring anchors on the
        exchange's DECLARED best bid/ask (the group-chat claim,
        2026-08-21) instead of the raw touch. Study only — changes no
        behavior."""
        book = self.cache.any_age(slug)
        prog, _w = self._prog_row(slug)
        if book is None or prog is None:
            return None
        sp = self._side_pool(slug, prog)
        if sp is None:
            return None
        df, target = float(prog.df), float(prog.target)
        out = {"market": slug, "bb": bb, "ba": ba,
               "raw_bid": book.bids[0][0] if book.bids else None,
               "raw_ask": book.asks[0][0] if book.asks else None,
               "est_alt": 0.0, "est_cur": 0.0}
        # levels the exchange's declared best SKIPPED — the filter that
        # produced the declared value must reject every one of these
        # (owner, 2026-08-21: "figure out how the best bid / ask are
        # calculated ... so that I could try and move them")
        if bb is not None:
            out["skip_b"] = [[p, round(q, 2)] for p, q in book.bids
                             if p > bb + 1e-9][:6]
        if ba is not None:
            out["skip_a"] = [[p, round(q, 2)] for p, q in book.asks
                             if p < ba - 1e-9][:6]
        # the window-closing play, priced: resting Target Size at the raw
        # touch closes the scoring window there — everyone deeper earns
        # zero (the docs' own example). What that costs per side:
        if book.bids:
            out["own_bid_usd"] = round(target * book.bids[0][0], 2)
        if book.asks:
            out["own_ask_usd"] = round(target * (1.0 - book.asks[0][0]), 2)
        out["pool_side"] = round(sp, 2)
        for side, anchor in (("BUY", bb), ("SELL", ba)):
            levels = list(book.side(side))
            total = sum(q for _, q in levels)
            mine = [(o.price, o.qty) for o in list(self.orders.values())
                    if o.market == slug and o.side == side]
            out["est_cur"] += sum((o.live_est or 0.0)
                                  for o in list(self.orders.values())
                                  if o.market == slug and o.side == side)
            if anchor is None or not mine or total < target:
                continue
            denom = sum(q * df ** round(abs(p - anchor) / book.tick)
                        for p, q in levels)
            ours = sum(q * df ** round(abs(p - anchor) / book.tick)
                       for p, q in mine)
            if denom > 1e-12:
                out["est_alt"] += min(ours / denom, 1.0) * sp
        out["est_alt"] = round(out["est_alt"], 4)
        out["est_cur"] = round(out["est_cur"], 4)
        return out

    def ladder_view(self, slug: str) -> dict:
        """Every price level the planner prices, with its numbers —
        the owner reads the whole ladder himself (2026-08-21: "allow me
        to click into any market to see even more detail on the numbers
        for listing at every price level")."""
        book = self.cache.any_age(slug)
        if book is None:
            return {"ok": False, "note": "no book cached yet"}
        prog, why = self._prog_row(slug)
        if prog is None:
            return {"ok": False, "note": why}
        sp = self._side_pool(slug, prog)
        headroom = self.cfg.capital_usd - self.family_spent()
        out = {"ok": True, "bar": self.cfg.min_est_day,
               "pool_day": round(sp, 2) if sp is not None else None,
               "note": ("pool divisor unconfirmed — dollar figures held at 0"
                        if sp is None else
                        f"family at its ceiling — new orders wait for "
                        f"${-headroom + 1:.0f} of space" if headroom < 1.0
                        else ""), "sides": {}}
        for side in ("BUY", "SELL"):
            rows: list[dict] = []
            try:
                pick = self._plan_side(slug, book, side, prog, sp or 0.0,
                                       self.cfg.per_market_usd / 2.0,
                                       ladder=rows)
            except Exception as e:  # noqa: BLE001 — the view never breaks
                out["sides"][side] = {"rows": [], "note": str(e)[:80]}
                continue
            best: dict[float, dict] = {}
            for r in rows:
                b = best.get(r["px"])
                if b is None or r["ev"] > b["ev"]:
                    best[r["px"]] = r
            ordered = sorted(best.values(), key=lambda r: -r["px"]
                             if side == "BUY" else r["px"])
            for r in ordered:
                r["picked"] = bool(pick and abs(pick["px"] - r["px"]) < 1e-9)
                r["clears_bar"] = r["ev"] >= self.cfg.min_est_day
            entry = {"rows": ordered[:24]}
            if not ordered:
                st = sum(q for _, q in book.side(side))
                if st < float(prog.target):
                    entry["note"] = (
                        f"the {'bid' if side == 'BUY' else 'ask'} side holds "
                        f"{st:,.0f} of {float(prog.target):,.0f} Target Size "
                        f"shares — the whole side pays nobody, so there is "
                        f"nothing to price")
            out["sides"][side] = entry
        return out

    def _band(self, slug: str, bids, asks, tick: float) -> dict | None:
        """The evidence band for a market: Silver as prior when it prices
        it, real touches (levels holding at least 5 shares — smaller is
        bait, 1.0's rule) as anchors, WEIGHTED BY THEIR SIZE — a
        million-share wall testifies harder than a token quote."""
        fair = self.fairs(slug) if self.fairs is not None else None
        bt = next(((p, q) for p, q in (bids or ()) if q >= 5.0), (None, None))
        at = next(((p, q) for p, q in (asks or ()) if q >= 5.0), (None, None))
        return self.evidence.band(slug, prior_fair=fair,
                                  touches=(bt[0], at[0]),
                                  touch_sizes=(bt[1], at[1]))

    def _price_bounds(self, slug: str, bids, asks,
                      tick: float) -> tuple[float | None, float | None]:
        """(lo, hi) price bounds in DOLLARS for resting, or Nones.

        No thresholds (owner, 2026-08-21: "I don't want hard and fast
        rules. I want confidence values that learned over time"). The
        bound is a continuous blend: it sits ON the Silver model when the
        evidence has earned nothing, and slides toward the evidence
        band's edge exactly as far as the evidence's confidence — built
        by real fills, amplified by a tight band, decayed by time — has
        earned. One fresh fill moves it some; a run of fills moves it
        most of the way; a quiet week slides it back toward the model.
        With no model the band stands alone; with neither, no bound."""
        band = self._band(slug, bids, asks, tick)
        fair = self.fairs(slug) if self.fairs is not None else None
        lo = band["lo"] / 100.0 if band else None
        hi = band["hi"] / 100.0 if band else None
        if fair is None:
            return lo, hi
        if band is None:
            return fair, fair
        c = self.evidence.confidence(slug, band)
        return (fair + c * (lo - fair), fair + c * (hi - fair))

    def plan_market(self, book, slug: str) -> tuple[list[dict], str]:
        """Both sides' best entries, within the caps. Returns (plans, why)
        — why explains an empty answer in the owner's language."""
        prog, why = self._prog_row(slug)
        if prog is None:
            return [], why
        side_pool = self._side_pool(slug, prog)
        if side_pool is None:
            return [], ("still confirming how many markets share this "
                        "pool — no estimate until I know")
        budget = self._market_budget(slug) / 2.0

        def plan_pair(bar=None):
            a = self._plan_side(slug, book, "BUY", prog, side_pool,
                                budget, bar=bar)
            b = self._plan_side(slug, book, "SELL", prog, side_pool,
                                budget, bar=bar)
            if a and b and a["px"] >= b["px"] - 1e-9:
                if a["ev"] >= b["ev"]:
                    b = self._plan_side(slug, book, "SELL", prog, side_pool,
                                        budget, bar=bar, cross_px=a["px"])
                else:
                    a = self._plan_side(slug, book, "BUY", prog, side_pool,
                                        budget, bar=bar, cross_px=b["px"])
            return [p for p in (a, b) if p]

        out = plan_pair()
        grow: list[dict] = []
        potential = 0.0
        if not out and self.cfg.grow_usd > 0:
            for side in ("BUY", "SELL"):
                fp = self._plan_side(slug, book, side, prog, side_pool,
                                     budget, full_confidence=True)
                if fp:
                    potential = max(potential, fp["ev"])
            if potential >= self.cfg.min_est_day:
                for gp in plan_pair(bar=self.cfg.grow_floor):
                    gp["grow"] = True
                    gp["why"] = (
                        f"under the {self.cfg.min_est_day * 100:.0f}c "
                        f"goal today (${gp['ev']:.2f}/day) but worth "
                        f"${potential:.2f} at full confidence — "
                        f"investing to build the evidence")
                    grow.append(gp)
        if not out:
            why = ("nothing here clears the bar: both sides either pay "
                   f"under {self.cfg.min_est_day * 100:.0f}c/day, are louder "
                   "than the courtesy band, or don't qualify")
        return out, why, grow, round(potential, 4)

    # -------------------------------------------------------------- reconcile

    def reconcile(self, open_orders: list[dict], positions: dict, now: float,
                  trades=None) -> None:
        """Adopt reality. Fills come from position deltas, never from mere
        disappearance. Scoped to markets THIS family placed in — the
        account is shared with 1.0 and 2.0, and their fills are not ours."""
        open_by_id = {o["id"]: o for o in open_orders}
        # remember when each live order was placed, before it can vanish
        for _oid, _rec in list(self.orders.items()):
            if _rec.placed_ts:
                self.placed_at[_oid] = _rec.placed_ts
        if len(self.placed_at) > 6000:      # keep the newest, bounded
            for _k in sorted(self.placed_at, key=self.placed_at.get)[:2000]:
                self.placed_at.pop(_k, None)
        tracked = (set(self.positions_seen) | set(self.inventory)
                   | {o.market for o in list(self.orders.values())}
                   | {g["rec"].market for g in self.gone_pending.values()})
        deltas = {m: (positions.get(m) or (0.0, 0.0))[0]
                  - self.positions_seen.get(m, 0.0)
                  for m in tracked}
        for _m, _d in deltas.items():
            if abs(_d) > 0.005:
                self.pos_moves.append([round(now, 1), _m, round(_d, 2)])
        del self.pos_moves[:-500]
        # limbo first: orders that disappeared earlier waiting for the
        # lagging position feed to say fill or cancel
        for oid, gp in list(self.gone_pending.items()):
            rec = gp["rec"]
            if trades and oid in trades:
                # the exchange's own trade history names this order id:
                # the definitive confirmation — a post-only rest fills
                # at its own price, so the journal price is exact
                filled = min(float(trades[oid]), rec.qty)
                self._on_fill(rec, filled, now)
                del self.gone_pending[oid]
                continue
            d = deltas.get(rec.market, 0.0)
            expected = rec.qty if rec.intent == BUY_LONG else -rec.qty
            if abs(d) > 1e-9 and (d > 0) == (expected > 0):
                filled = min(abs(d), rec.qty)
                deltas[rec.market] = d - (filled if d > 0 else -filled)
                self._on_fill(rec, filled, now)
                del self.gone_pending[oid]
            elif now >= gp["until"]:
                self.silent_cancels += 1
                self._log(event="silent_cancel", market=rec.market,
                          side=rec.side, price=rec.price, qty=rec.qty,
                          id=oid)
                del self.gone_pending[oid]
        for oid, rec in list(self.orders.items()):
            live = open_by_id.get(oid)
            if live is not None:
                if live["size"] < rec.qty - 1e-9:
                    # a shrunken size is only a FILL if the position
                    # moved with it (the Louisiana phantom, 2026-08-21:
                    # cancelled revives were booked as 265-share shorts
                    # the exchange never saw, and the exit engine bid
                    # real money to cover them). No delta -> it is a
                    # size correction, not a fill.
                    shrink = rec.qty - live["size"]
                    d = deltas.get(rec.market, 0.0)
                    expected_sign = 1.0 if rec.intent == BUY_LONG else -1.0
                    if abs(d) > 1e-9 and (d > 0) == (expected_sign > 0):
                        filled = min(shrink, abs(d))
                        deltas[rec.market] = d - expected_sign * filled
                        self._on_fill(rec, filled, now)
                    else:
                        self._log(event="size_shrunk_no_fill",
                                  market=rec.market, side=rec.side,
                                  price=rec.price, qty=shrink, id=oid)
                    rec.qty = live["size"]
                continue
            delta = deltas.get(rec.market, 0.0)
            expected = rec.qty if rec.intent == BUY_LONG else -rec.qty
            if abs(delta) > 1e-9 and (delta > 0) == (expected > 0):
                filled = min(abs(delta), rec.qty)
                deltas[rec.market] = delta - (filled if delta > 0 else -filled)
                self._on_fill(rec, filled, now)
            elif trades and oid in trades:
                self._on_fill(rec, min(float(trades[oid]), rec.qty), now)
            else:
                # NOT ruled a silent cancel yet: the position feed LAGS
                # the order list, so a complete fill often shows the
                # order gone before the delta arrives — instant
                # classification threw those fills away and the cards
                # read "closed by reconciliation" (owner, 2026-08-23:
                # "literally every closed position... says closed by
                # reconciliation"). The record waits in limbo; a
                # matching delta books the fill, GONE_GRACE_S of
                # silence makes it a real silent cancel.
                self.gone_pending[oid] = {"rec": rec,
                                          "until": now + GONE_GRACE_S}
            self.evidence.order_gone(rec.market, oid, now=now)
            del self.orders[oid]
        for m in tracked:
            if m in positions:
                self.positions_seen[m] = positions[m][0]
                # the exchange's position feed is the truth: wherever it
                # explicitly reports this market, our inventory snaps to
                # it, purging any phantom the fill accounting invented
                feed_qty = positions[m][0]
                inv = self.inventory.get(m)
                have = (inv or {}).get("qty", 0.0)
                if abs(feed_qty - have) > 0.01:
                    if abs(feed_qty) < 0.005:
                        if inv is not None:
                            self.inventory.pop(m, None)
                            self.inv_since.pop(m, None)
                    else:
                        if abs(have) > 0.005:
                            per = (inv or {}).get("cost", 0.0) / have
                            cost = per * feed_qty
                        else:
                            cost = (positions[m][1]
                                    if len(positions[m]) > 1 else 0.0)
                        self.inventory[m] = {"qty": feed_qty,
                                             "cost": round(cost, 4)}
                    self._log(event="inventory_corrected", market=m,
                              qty=feed_qty,
                              note=f"book said {have:g}, exchange says "
                                   f"{feed_qty:g} — exchange wins")
        # The feed lists only markets actually held (a failed fetch
        # aborts the cycle upstream, so this snapshot is complete).
        # Book inventory in a market the feed does not mention is
        # phantom — the Louisiana lesson part two: the first fix only
        # snapped markets the feed NAMED, and a phantom market is
        # exactly the one it never names. Fresh fills get a grace
        # period; the next snapshot confirms them.
        for m in list(self.inventory):
            if m in positions:
                continue
            if now - self.inv_since.get(m, 0.0) < 180.0:
                continue
            gone_qty = self.inventory[m].get("qty", 0.0)
            self.inventory.pop(m, None)
            self.inv_since.pop(m, None)
            self._log(event="inventory_corrected", market=m, qty=0.0,
                      note=f"book said {gone_qty:g}, the exchange holds "
                           f"nothing — phantom purged")
        for m in list(self.positions_seen):
            if (m not in self.inventory
                    and m not in {o.market for o in list(self.orders.values())}):
                self.positions_seen.pop(m, None)

    def _on_fill(self, rec: FamilyOrder, filled: float, now: float) -> None:
        if rec.market not in self.inventory:
            self.inv_since[rec.market] = now
        inv = self.inventory.setdefault(rec.market, {"qty": 0.0, "cost": 0.0})
        q0, c0 = inv["qty"], inv["cost"]
        if rec.side == "BUY":
            inv["qty"] += filled
            inv["cost"] += filled * rec.price
        else:
            inv["qty"] -= filled
            inv["cost"] -= filled * rec.price
        qty_after = round(inv["qty"], 2)
        if abs(inv["qty"]) < 0.005:
            self.inventory.pop(rec.market, None)
            since = self.inv_since.pop(rec.market, None)
            if since is not None and now > since:
                self.fillmodel.observe_offload(rec.market,
                                               (now - since) / 86400.0)
        self._journal_fill(rec, filled, now, qty_after)
        book_now = self.cache.any_age(rec.market)
        w, verdict, adv = self._fill_speed_verdict(
            rec.side, rec.price, now - rec.placed_ts, book_now)
        self._fill_evi_buf.append({"market": rec.market, "side": rec.side,
                                   "px": rec.price, "weight": w,
                                   "adv": adv, "verdict": verdict})
        self.fillmodel.observe_fill_age(rec.market, now - rec.placed_ts)
        if rec.purpose not in ("sell", "probe", "bond"):
            # the fill lands in the cell whose resting time it ended
            self.fillmodel.observe_own_fill(rec.market, rec.side,
                                            rec.ticks_last,
                                            now - rec.placed_ts)
        self.pending_marks.append({"market": rec.market, "side": rec.side,
                                   "price": rec.price, "due": now + 3600.0})
        del self.pending_marks[:-60]
        self._log(event="fill", market=rec.market, side=rec.side,
                  price=rec.price, qty=round(filled, 2))
        gain = self._closing_gain(rec.side, rec.price, filled, q0, c0)
        if gain is not None:
            # A CLOSE. Silent unless it realised more than a dollar of
            # loss (owner, 2026-08-24). Profit, break-even and small
            # losses all stay off the phone; the card still records it.
            if gain < -PAGE_LOSS_USD:
                self.alert(f"{self.cfg.tag} closed at a loss",
                           f"{self._label(rec.market)}: {rec.side} "
                           f"{filled:g} @ {rec.price * 100:g}c — "
                           f"${-gain:.2f} lost on the round trip")
            else:
                self._log(event="fill_no_page", market=rec.market,
                          note=f"closed {filled:g} at ${gain:+.2f} — "
                               f"under the ${PAGE_LOSS_USD:g} page bar")
        else:
            # An OPEN. The decision waits for the book to settle: right
            # after a fill the touch is the one our own trade just moved
            # (owner, 2026-08-24: "liquidation 20 seconds later").
            self.pending_pages.append(
                {"market": rec.market, "side": rec.side, "qty": filled,
                 "px": rec.price, "due": now + PAGE_SETTLE_S})
            del self.pending_pages[:-200]

    @staticmethod
    def _closing_gain(side: str, price: float, filled: float,
                      q0: float, c0: float) -> float | None:
        """Realized dollars when a fill only REDUCES the position it
        found: proceeds against the average cost of the shares closed.
        None when the fill opened, grew, or flipped a position — that
        is new risk, and new risk always pages."""
        if side == "SELL" and q0 > 0.005 and filled <= q0 + 0.005:
            return (price - c0 / q0) * filled
        if side == "BUY" and q0 < -0.005 and filled <= -q0 + 0.005:
            return (c0 / q0 - price) * filled
        return None

    def _page_opens_due(self, now: float) -> None:
        """Decide the held-back OPEN fills once the book has settled.
        Page only when the position is BOTH marked at more than a
        dollar of loss AND earning nothing (owner, 2026-08-24) — a
        paper loss that is collecting rewards is the business working,
        not news."""
        for p in list(self.pending_pages):
            if now < p["due"]:
                continue
            self.pending_pages.remove(p)
            slug = p["market"]
            inv = self.inventory.get(slug) or {}
            qty = inv.get("qty") or 0.0
            if abs(qty) < 0.005:
                continue                  # already gone; nothing to warn about
            book = self.cache.any_age(slug)
            if book is None:
                continue                  # no mark, no claim
            mark = (book.bids[0][0] if qty > 0 and book.bids
                    else book.asks[0][0] if qty < 0 and book.asks else None)
            if mark is None:
                continue
            pnl = qty * mark - (inv.get("cost") or 0.0)
            earning = any((o.live_est or 0.0) > 0.0
                          for o in list(self.orders.values()) if o.market == slug)
            if pnl < -PAGE_LOSS_USD and not earning:
                self.alert(f"{self.cfg.tag} position under water, earning nothing",
                           f"{self._label(slug)}: {p['side']} {p['qty']:g} @ "
                           f"{p['px'] * 100:g}c — the {qty:g} now held marks "
                           f"${-pnl:.2f} down and nothing is earning here")
            else:
                self._log(event="fill_no_page", market=slug,
                          note=f"opened {p['qty']:g} — marks ${pnl:+.2f}"
                               f"{' and is earning' if earning else ''}; "
                               f"under the page bar")

    def _journal_fill(self, rec: FamilyOrder, filled: float, now: float,
                      qty_after: float) -> None:
        """One row per purchase, captured the moment it happens: what the
        order was doing, what value looked like right then (before this
        fill enters the evidence), and the position it left behind. The
        fills page reads these back to the owner."""
        try:
            book = self.cache.any_age(rec.market)
            fair = self.fairs(rec.market) if self.fairs is not None else None
            band = None
            if book is not None:
                try:
                    band = self._band(rec.market, book.bids, book.asks,
                                      book.tick)
                except Exception:  # noqa: BLE001
                    band = None
            ref = fair
            if ref is None and band and band.get("med") is not None:
                ref = band["med"] / 100.0
            conc = None
            if ref is not None:
                conc = round((rec.price - ref) if rec.side == "BUY"
                             else (ref - rec.price), 4)
            self.fills.append({
                "ts": round(now, 1), "market": rec.market, "side": rec.side,
                "qty": round(filled, 2), "px": rec.price,
                # the exchange's order id: the exact handle for matching
                # a journal row to the exchange's own transaction record
                # (owner, 2026-08-23: "keep track of the order id in the
                # future so we can match it up"). Price-bucket matching
                # was the only option before this and could not tell two
                # orders at one price apart.
                "oid": rec.id,
                "purpose": rec.purpose, "why": rec.why,
                "est_day": (rec.live_est if rec.live_est is not None
                            else rec.est_day),
                "rested_h": (round((now - rec.placed_ts) / 3600.0, 2)
                             if rec.placed_ts > 0 else None),
                "fair": fair,
                "band": ([band["lo"], band["hi"]] if band else None),
                "conf": round(self.evidence.confidence(rec.market), 3),
                "touch_bid": (book.bids[0][0]
                              if book is not None and book.bids else None),
                "touch_ask": (book.asks[0][0]
                              if book is not None and book.asks else None),
                "conc": conc, "pos_after": qty_after})
            # retention (owner, 2026-08-22): a row must outlive its card
            # — closed cards show for 3 days, open ones until profitable
            # — so keep a week of rows plus anything belonging to a
            # market we still hold, bounded at 600
            cutoff = now - 7 * 86400.0
            keep = [r2 for r2 in self.fills
                    if r2.get("ts", 0.0) >= cutoff
                    or abs((self.inventory.get(r2.get("market"))
                            or {}).get("qty", 0.0)) > 0.005]
            self.fills = keep[-600:]
        except Exception:  # noqa: BLE001 — the journal never breaks a fill
            pass

    # ---------------------------------------------------------------- adoption

    def adoptable(self, open_orders: list[dict], foreign_ids=()) -> list[dict]:
        """Every resting account order this family does not already
        track, in its universe.

        Orders the exchange flags MANUAL are RECORDED, not skipped
        (owner, 2026-08-24: "if I cancel an order and put a new one
        back the model won't sell more than is already there").
        Skipping them meant the owner's own exits never entered the
        book, so the cover math saw a bare position and rested a
        second exit on top of his — the flag that identifies his
        orders was the thing that hid them. Recording costs nothing:
        every adopted order becomes purpose="manual", which is never
        cancelled, moved or resized anywhere in the engine."""
        out = []
        for o in open_orders:
            if o["id"] in self.orders or o["id"] in foreign_ids:
                continue
            if o["market"] not in self.universe:
                continue
            if not o.get("size") or not o.get("price"):
                continue
            out.append(o)
        return out

    def _adopt(self, adoptable: list[dict], positions: dict, now: float) -> None:
        """An open order this engine did not place is the OWNER'S OWN
        (the 1.0/2.0 handover is finished — nothing else places orders).
        Record it so ceilings, exits, and dedupe can see it, mark it
        manual, and never cancel, move, or reprice it (owner, 2026-08-22:
        "Don't let it cancel orders I set by hand")."""
        for o in adoptable:
            self.orders[o["id"]] = FamilyOrder(
                id=o["id"], market=o["market"], side=o["side"],
                price=o["price"], qty=o["size"], intent=o["intent"],
                placed_ts=now, purpose="manual",
                why="the owner's own order — the engine leaves it alone")
            self.positions_seen.setdefault(
                o["market"], (positions.get(o["market"]) or (0.0,))[0])
            self._mark(o["market"], o["side"], now)
        if adoptable:
            self._log(event="owner_orders_seen", n=len(adoptable),
                      note="resting orders this engine did not place — "
                           "recorded hands-off, never cancelled")

    def _seed_inventory(self, positions: dict) -> None:
        """Positions on our ground the seller does not know yet — long
        stock OR shorts — join its book. Runs every armed cycle so a
        position found after the adoption still gets its exit."""
        for m, pv in positions.items():
            net, cost = ((list(pv) + [0.0, 0.0])[:2]
                         if isinstance(pv, (tuple, list)) else (float(pv), 0.0))
            if m in self.universe and abs(net) > 0.005 and m not in self.inventory:
                self.inventory[m] = {"qty": net, "cost": cost}
                self.positions_seen[m] = net

    # ------------------------------------------------------------------ cycle

    def _reclassify_exits(self, positions: dict) -> None:
        """An adopted order whose FILL reduces the position it sits on is
        an EXIT whatever its intent says — a bid covering a short, an ask
        while long. Mislabelling them "earn" once let maintenance reprice
        and pull the owner's exits (2026-08-20 23:12Z) and counted their
        collateral against the rebuild ceiling. Idempotent, every cycle."""
        for rec in list(self.orders.values()):
            if rec.purpose in ("sell", "manual", "bond"):
                continue
            net = (positions.get(rec.market) or (0.0, 0.0))[0]
            if ((rec.side == "SELL" and net > 0.005)
                    or (rec.side == "BUY" and net < -0.005)):
                rec.purpose = "sell"
                rec.why = "an exit — its fill reduces the position it sits on"

    def cycle(self, now: float, open_orders: list[dict], positions: dict,
              client, switch_on: bool, foreign_ids=(),
              exits_only: bool = False, trades=None) -> dict:
        self.reconcile(open_orders, positions, now, trades=trades)
        self._flush_fill_evidence(now)
        self._page_opens_due(now)
        self._reclassify_exits(positions)
        self.refresh_universe(client, now)
        self.refresh_terms(client, now)
        stock = sum(abs(v.get("qty") or 0.0) for v in list(self.inventory.values()))
        stock_rate = sum(o.live_est or 0.0 for o in list(self.orders.values())
                         if o.purpose == "sell")
        self._exit_rate_ps = (stock_rate / stock) if stock > 0.01 else 0.0
        refreshed = self._refresh_books(client, now)
        self._read_live(now)
        self._accrue(now)
        pending = (self.adoptable(open_orders, foreign_ids)
                   if self.cfg.adopt else [])
        summary = {"mode": "on" if switch_on else "observing",
                   "markets": len(self.universe),
                   "active": len(self.active_markets()),
                   "resting_ok": bool(resting_ok(now, self.cfg)
                                      or (self.active_until and now < self.active_until)),
                   "active_until": (self.active_until
                                    if self.active_until and now < self.active_until else 0.0),
                   "refreshed": refreshed,
                   "would_adopt": len(pending)}
        if not switch_on:
            return self._finish(summary, now)
        if pending:
            self._adopt(pending, positions, now)
            summary["would_adopt"] = 0
            summary["active"] = len(self.active_markets())
        if self.cfg.adopt:
            self._seed_inventory(positions)
        if exits_only:
            # the flatten's first phase: the monitor is cancelling every
            # opening order; this family only keeps stock exiting — asks
            # that cost nothing to place and earn while they wait
            summary["mode"] = "flatten — exits only"
            self._sell(now, self.cfg.max_actions_per_cycle)
            return self._finish(summary, now)
        actions = self.cfg.max_actions_per_cycle

        # game window: pull everything that isn't an exit — the owner's
        # hand orders included (owner, 2026-09-02, shown the Week-1 pull
        # took 28 of them: "that's a good idea to pull hand orders during
        # game windows"). The one place the hands-off rule yields.
        if not (resting_ok(now, self.cfg)
                or (self.active_until and now < self.active_until)):
            summary["mode"] = "game window"
            for rec in list(self.orders.values()):
                if actions <= 0:
                    break
                if rec.purpose == "sell":
                    continue
                r = self.desk.cancel(rec.id, rec.market)
                if r.ok:
                    self._log(event="window_pull", market=rec.market,
                              side=rec.side, price=rec.price)
                    del self.orders[rec.id]
                    actions -= 1
            return self._finish(summary, now)

        # grade fills that have had their hour: the adverse move a fill
        # actually cost is the calibration everything else leans on
        for mk in list(self.pending_marks):
            if now < mk["due"]:
                continue
            book_m = self.cache.fresh(mk["market"], self.cfg.read_age_s, now)
            if book_m is None:
                if now > mk["due"] + 4 * 3600.0:
                    self.pending_marks.remove(mk)   # too stale to grade honestly
                continue
            if book_m.bids and book_m.asks:
                mid = (book_m.bids[0][0] + book_m.asks[0][0]) / 2.0
                adverse = self.fillmodel.observe_fill_mark(
                    mk["market"], mk["side"], mk["price"], mid)
                self._log(event="fill_graded", market=mk["market"],
                          why=f"cost {adverse * 100:.1f}c/share vs the "
                              f"mid an hour on")
            self.pending_marks.remove(mk)

        # 0) zombies from a failed cancel: retry until they die
        for rec in list(self.orders.values()):
            if rec.why == "cancel failed during a move — retrying":
                r = self.desk.cancel(rec.id, rec.market)
                if r.ok:
                    self._log(event="zombie_cancelled", market=rec.market,
                              id=rec.id)
                    del self.orders[rec.id]

        # 1) leave dead or near-resolution markets ENTIRELY (exits included)
        for rec in list(self.orders.values()):
            if actions <= 0:
                break
            if rec.purpose in ("manual", "bond") or self._frozen(rec.market):
                continue      # owner, 2026-08-22: never cancel the owner's
                              # own orders — no rule outranks the hand;
                              # 2026-08-24: nor anything in frozen ground
            days = slug_days_out(rec.market, now)
            near = days is not None and days < self.cfg.min_days_out
            dead = self._dead_here(rec.market)
            out_of_scope = (rec.purpose != "sell"
                            and not self.enterable(rec.market))
            if dead or ((near or out_of_scope) and rec.purpose != "sell"):
                r = self.desk.cancel(rec.id, rec.market)
                if r.ok:
                    why = ("program pays nothing" if dead
                           else "outside the families you chose"
                           if out_of_scope else "resolves soon")
                    self._log(event="exit", market=rec.market, why=why, id=rec.id)
                    del self.orders[rec.id]
                    actions -= 1

        # 2) maintenance: reprice or pull against fresh books.
        #
        # When the book is OVER the ceiling, maintenance may not spend
        # the whole budget. It ran unbounded until 2026-08-25, and in a
        # busy family that starved the trim behind it completely: 89
        # reprices, 50 places and ZERO trims in one window, while
        # politics sat at $360.69 against a $250 cap and climbing. A
        # ceiling that queues behind price improvements is not a
        # ceiling.
        #
        # The reserve, rather than simply running the trim first,
        # because the trim ranks orders by measured value per dollar
        # and maintenance is what refreshes that measurement. Trimming
        # ahead of it means choosing between an unscored good order and
        # an unscored bad one — which a test caught before it shipped.
        over = self.family_spent() > self.cfg.capital_usd + 1e-9
        reserve = min(TRIM_RESERVE, actions) if over else 0
        actions = self._maintain(now, actions - reserve) + reserve

        # 3) the ceiling is enforced, not just checked at the door: over
        # it (reprices once grew orders past it), the worst value per
        # dollar goes first until the book fits
        actions = self._trim(now, actions)

        # 4) the seller next — getting the owner OUT always outranks new
        # risk (starving it behind entries left shorts uncovered, 23:53Z)
        actions = self._sell(now, actions)

        # 5) probes: buy information where it is missing
        actions = self._probe(now, positions, actions)

        # 6) new entries, best scoreboard candidates first
        actions = self._enter(now, positions, actions)

        # 7) growth: seed the markets whose goal needs confidence first
        self._grow(now, positions, actions)
        return self._finish(summary, now)

    def _read_live(self, now: float) -> None:
        """Refresh every order's live share/est and verdict — the reading
        happens whether or not the switch is on (the observing mode's
        whole point)."""
        for rec in list(self.orders.values()):
            book = self.cache.fresh(rec.market, self.cfg.read_age_s, now)
            prog, why = self._prog_row(rec.market)
            if book is None:
                rec.verdict = "no fresh book — can't read this one right now"
                continue
            if prog is None:
                rec.live_est, rec.live_share = 0.0, 0.0
                self._track_est(rec, now)
                rec.verdict = why
                continue
            side_pool = self._side_pool(rec.market, prog)
            lv = [(p, q - rec.qty if abs(p - rec.price) < 1e-9 else q)
                  for p, q in book.side(rec.side)]
            lv = [(p, q) for p, q in lv if q > 1e-9]
            j = estimate_join(rec.side, lv, book.tick, float(prog.df),
                              float(prog.target), rec.price, rec.qty)
            rec.live_share = round(j.share, 4)
            if side_pool is None:
                rec.live_est = None
                rec.verdict = ("scoring ~"
                               f"{j.share * 100:.1f}% of its side — holding "
                               "the estimate until the pool divisor is known")
                continue
            rec.live_est = round(j.share * side_pool
                                 if j.qualifies and j.in_window else 0.0, 4)
            # when did this order last earn anything? An exit that has
            # been dry for hours may price to FILL rather than hold out
            # for a price the book has left behind.
            if rec.live_est and rec.live_est > 0.0:
                rec.dry_since = None
            elif rec.dry_since is None:
                rec.dry_since = now
            self._track_est(rec, now)
            self.fillmodel.observe_order_age(rec.market, now - rec.placed_ts,
                                             60.0)
            if (rec.purpose not in ("manual", "sell", "bond")
                    and (self.fairs is None
                         or self.fairs(rec.market) is None)
                    and now - rec.placed_ts >= 86400.0
                    and not any(r2[1].startswith("fill") for r2 in
                                self.evidence.events.get(rec.market, ()))):
                self._advance_probe_ratchet(rec.market, rec.side, now)
            if rec.purpose not in ("sell", "probe"):
                ticks_now = (round(abs(lv[0][0] - rec.price) / book.tick)
                             if lv else 0)
                shield_now = sum(q for p2, q in lv
                                 if (p2 - rec.price)
                                 * (1.0 if rec.side == "BUY" else -1.0) > 1e-9)
                shield_now += max(sum(q for p2, q in lv
                                      if abs(p2 - rec.price) <= 1e-9)
                                  - rec.qty, 0.0)
                pf_now = self.fillmodel.p_fill(rec.market, rec.side, ticks_now,
                                               shield=shield_now,
                                               target=float(prog.target),
                                               age_s=now - rec.placed_ts)
                rec.live_pf = round(pf_now, 4)   # the expected-risk
                                                 # budget charges by this
                # the model's evidence and its own report card: the
                # time this order just rested, at this depth and age.
                # The first look only stamps it — an order adopted or
                # restored from before the stamp existed would otherwise
                # bank its whole life as fill-free. A slow cycle or a
                # restart still counts (the order rested through it and
                # its fill, if any, is found at this look), capped at
                # an hour so a long outage cannot swamp a cell.
                if rec.seen_ts:
                    dt_seen = min(now - rec.seen_ts, 3600.0)
                    if dt_seen > 0.0:
                        if rec.purpose != "bond":
                            self.fillmodel.observe_rest(
                                rec.market, rec.side, ticks_now,
                                now - rec.placed_ts, dt_seen)
                        self._accrue_expected(rec.purpose, pf_now, dt_seen,
                                              now)
                rec.ticks_last = ticks_now
                rec.seen_ts = now
                ign_now = 0.0
                ind_now = (1.0 if self.fairs is not None
                           and self.fairs(rec.market) is not None
                           else self.evidence.confidence(rec.market))
                osd = book.side("SELL" if rec.side == "BUY" else "BUY")
                if ind_now < 1.0 and lv and osd:
                    spread_w = abs(osd[0][0] - lv[0][0])
                    if spread_w > book.tick / 2:
                        adv = ((rec.price - lv[0][0]) if rec.side == "BUY"
                               else (lv[0][0] - rec.price))
                        if adv > 0:
                            ign_now = ((1.0 - ind_now) * adv * adv
                                       / (2.0 * spread_w))
                fc_now = self.fillmodel.fill_cost(rec.market, rec.side,
                                                  rec.price, None,
                                                  ignorance=ign_now)
                rec.live_ev = round(
                    rec.live_est * self.fillmodel.scoring_fraction(rec.market)
                    - pf_now * fc_now * rec.qty, 4)
                self.fillmodel.observe_scoring(rec.market,
                                               j.qualifies and j.in_window)
                self.fillmodel.observe_approach(rec.market, rec.side,
                                                ticks_now, 60.0,
                                                rec.live_est or 0.0)
            if rec.purpose != "sell" and now - rec.rest_noted > 1800.0:
                rec.rest_noted = now
                self.evidence.rest_mark(rec.market, rec.id, rec.side,
                                        rec.price, rec.placed_ts, now=now)
            if not j.qualifies:
                rec.verdict = ("its side is below Target Size — the whole "
                               "side pays nobody right now")
            elif not j.in_window:
                rec.verdict = "outside the Target Size window — earning $0"
            else:
                rec.verdict = (f"earning ~${rec.live_est:.2f}/day — "
                               f"{j.share * 100:.1f}% of its side")
        for rec in list(self.orders.values()):
            if rec.pinned:
                self._pin_check(rec, now)

    def _track_est(self, rec: FamilyOrder, now: float) -> None:
        """The 8-hour earning trail behind the orders page's
        drop-from-peak sort (owner, 2026-08-26): half-hour buckets of
        the order's best measured $/day, oldest dropped past 8h.
        est_peak8 is the window's max — an order well off its peak has
        been outbid or its pool has moved, and the sort surfaces it."""
        if rec.live_est is None:
            return
        b = int(now // 1800) * 1800
        if rec.est_hist and rec.est_hist[-1][0] == b:
            if rec.live_est > rec.est_hist[-1][1]:
                rec.est_hist[-1][1] = round(rec.live_est, 4)
        else:
            rec.est_hist.append([b, round(rec.live_est, 4)])
            if (len(rec.est_hist) > 17
                    or rec.est_hist[0][0] <= now - 28800.0):
                rec.est_hist = [x for x in rec.est_hist
                                if x[0] > now - 28800.0][-17:]
        rec.est_peak8 = round(max((x[1] for x in rec.est_hist),
                                  default=0.0), 4)

    def _ask_anchor(self, slug: str, book, break_even: float) -> float:
        """Where the ask side 'is' when it is EMPTY. It used to be
        break-even + 1 tick — and on 2026-08-27's post-maintenance
        wipe that re-rested 89 exits within 2c of cost on books with
        no competition at all (owner: "the newly placed order should
        be at much better prices (for me) because there is less
        competition"). An empty side means WE are the touch wherever
        we rest: anchor high — above fair where a model exists, at
        the ceiling on blank ground — and let the slot optimizer and
        the scoring window pull the actual price back down to the
        best PAYING slot. A populated side keeps the old rule: the
        real touch is the anchor."""
        if book.asks:
            return book.asks[0][0]
        fair = self.fairs(slug) if self.fairs is not None else None
        if fair is not None:
            return min(0.99, max(fair + 15 * book.tick,
                                 break_even + book.tick))
        return 0.99

    def _pin_check(self, rec: FamilyOrder, now: float) -> None:
        """The release rule for a hand-set order: the owner's placement
        holds until the book materially turns against it — its live
        earning rate under PIN_RELEASE_FRACTION of what it earned when
        he set it, sustained PIN_RELEASE_DWELL_S. Then the pin lifts and
        the order is an ordinary engine order again. An order that
        earned nothing when he set it has no rate to lose, so only the
        nurse and his own hand ever move it."""
        if rec.live_est is None:
            return
        if rec.pin_est < 0:
            # a fresh hand change (move or resize): the hold's baseline
            # is what the order ACTUALLY earns at its new price and
            # size, measured on the first read after the change
            rec.pin_est = max(rec.live_est, 0.0)
            return
        if rec.pin_est <= 0.005:
            return
        if rec.live_est < PIN_RELEASE_FRACTION * rec.pin_est - 1e-9:
            if not rec.pin_weak_since:
                rec.pin_weak_since = now
            elif now - rec.pin_weak_since >= PIN_RELEASE_DWELL_S:
                rec.pinned = False
                rec.pin_weak_since = 0.0
                rec.verdict = ("hand-set hold released — the engine "
                               "resumes")
                self._log(event="pin_released", market=rec.market,
                          side=rec.side, price=rec.price, qty=rec.qty,
                          note=(f"earning fell to ${rec.live_est:.2f}/day "
                                f"from ${rec.pin_est:.2f} when hand-set — "
                                "a big change; the engine resumes control"))
        else:
            rec.pin_weak_since = 0.0

    def _maintain(self, now: float, actions: int) -> int:
        # Markets the owner just repriced come FIRST. Setting a fair is
        # a statement that the resting book is wrong there, and with
        # 283 orders and 10 actions a cycle the sweep could take many
        # minutes to reach them — long enough for a non-compliant order
        # to fill (the jdvan buy at 57c against his 50c fair,
        # 2026-08-23). Same rails, same budget, different order.
        recs = sorted(list(self.orders.values()),
                      key=lambda r: 0 if r.market in self.priority else 1)
        for rec in recs:
            if actions <= 0:
                break
            if rec.pinned:
                continue      # hand-set from the live card: the owner's
                              # placement holds until the release rule
                              # (checked in _read_live) or the nurse ends it
            if (self.cfg.whole_shares
                    and rec.purpose not in ("sell", "manual", "bond")
                    and abs(rec.qty - round(rec.qty)) > 1e-9):
                r = self.desk.cancel(rec.id, rec.market)
                if r.ok:
                    self.orders.pop(rec.id, None)
                    self.evidence.order_gone(rec.market, rec.id)
                    self._log(event="whole_shares_cull", market=rec.market,
                              price=rec.price, qty=rec.qty,
                              note="fractional size retired — politics "
                                   "quotes whole shares now")
                    actions -= 1
                continue
            if rec.purpose in ("manual", "bond") or self._frozen(rec.market):
                continue          # frozen ground: never repriced,
                                  # never pulled, never resized
            if self._avoided(rec.market) or (
                    self._liquidating(rec.market)
                    and rec.purpose != "sell"):
                # liquidating ground keeps its exits (the close-out
                # block below replaces them); everything else leaves
                # out means OUT — earn orders, probes, AND the engine's
                # own exits leave (owner, 2026-08-22: the balance-of-power
                # markets are his to work by hand; an engine order resting
                # there kills his via the exchange's self-match guard).
                # Manual orders were already skipped above.
                r = self.desk.cancel(rec.id, rec.market)
                if r.ok:
                    self._log(event="pull", market=rec.market,
                              side=rec.side,
                              why=("owner: closing out this market — "
                                   "nothing new rests here"
                                   if self._liquidating(rec.market)
                                   else "owner: staying out of this "
                                        "market for now — it is his "
                                        "to work by hand"))
                    del self.orders[rec.id]
                    actions -= 1
                continue
            # ON-GRID SWEEP (owner, 2026-08-26: "Go through and change
            # all the non whole number price orders"): the desk now
            # snaps every NEW price to the book's grid; this walks the
            # grandfathered rest onto it — 32 exits were resting at
            # break-even arithmetic like 5.90676c. Bids re-grid down,
            # asks up, so floors and never-cross only get safer. Runs
            # for EVERY engine purpose, exits included; manual, frozen,
            # avoided and hand-set were already skipped above.
            if actions > 0:
                book_g = self.cache.fresh(rec.market, self.cfg.read_age_s,
                                          now)
                if book_g is not None and book_g.tick:
                    px_g = snap_price(rec.price, book_g.tick, rec.side)
                    if not (0.001 - 1e-12 <= px_g <= 0.999 + 1e-12):
                        # no legal slot on this book's grid (the 99.9c
                        # ask whose snap-up is 100c) — leave it be
                        # instead of retrying a doomed reprice forever
                        # (owner approved 2026-08-26; 13 audit refusals
                        # in 6h before this)
                        pass
                    elif abs(px_g - rec.price) > 1e-9:
                        r_g = self.desk.reprice(
                            {"id": rec.id, "market": rec.market,
                             "side": rec.side, "price": rec.price,
                             "size": rec.qty, "intent": rec.intent},
                            px_g)
                        if r_g.ok:
                            self.orders.pop(rec.id, None)
                            self.evidence.order_gone(rec.market, rec.id)
                            self.orders[r_g.order_id] = FamilyOrder(
                                id=r_g.order_id, market=rec.market,
                                side=rec.side,
                                price=(r_g.price or px_g), qty=rec.qty,
                                intent=rec.intent, placed_ts=now,
                                purpose=rec.purpose, why=rec.why,
                                est_day=rec.est_day, share=rec.share)
                            self._log(event="regridded",
                                      market=rec.market, side=rec.side,
                                      price=rec.price,
                                      to=(r_g.price or px_g), qty=rec.qty,
                                      note="was off the book's price "
                                           "grid — moved onto it")
                            actions -= 1
                        else:
                            # refused (a cross at the snapped price, a
                            # stale book): throttle the retry, keep the
                            # original resting
                            self._mark(rec.market, rec.side, now)
                        continue
            if rec.purpose in ("sell", "probe"):
                continue
            book = self.cache.fresh(rec.market, self.cfg.read_age_s, now)
            prog, _why = self._prog_row(rec.market)
            if book is None or prog is None:
                continue
            # CONFORMANCE (owner, 2026-08-25: "make sure that all
            # orders are conforming to the new rules before moving
            # on"): the fronting bounds govern the RESTING book, not
            # just new placements. An order the rules would refuse to
            # place today is pulled today — pre-rule leftovers were
            # still catching fills. Manual, frozen and avoided were
            # handled above; exits answer to the exit gate's retreats.
            if (actions > 0 and (self.fairs is None
                                 or self.fairs(rec.market) is None)):
                sign_c = 1.0 if rec.side == "BUY" else -1.0
                lv_c = [(p, q - rec.qty if abs(p - rec.price) < 1e-9 else q)
                        for p, q in book.side(rec.side)]
                lv_c = [(p, q) for p, q in lv_c if q > 1e-9]
                touch_c = lv_c[0][0] if lv_c else None
                bad = None
                if touch_c is not None:
                    front_by = (rec.price - touch_c) * sign_c / book.tick
                    has_info_c = any(
                        r2[1].startswith("fill") for r2 in
                        self.evidence.events.get(rec.market, ()))
                    if has_info_c:
                        # joins included (owner, 2026-08-25, kamhar):
                        # ANY earn order past the evidence line goes
                        eb_lo, eb_hi = self._price_bounds(
                            rec.market,
                            lv_c if rec.side == "BUY"
                            else book.side("BUY"),
                            book.side("SELL") if rec.side == "BUY"
                            else lv_c,
                            book.tick)
                        cap_c = self._evidence_cap(rec.market, rec.side,
                                                   eb_lo, eb_hi)
                        if (cap_c is not None
                                and (rec.price - cap_c) * sign_c > 1e-9):
                            bad = ("past the price the evidence supports "
                                   f"({cap_c * 100:.0f}c) on a market "
                                   "that has burned us")
                    elif front_by > 1e-6:
                        allowed_c = (self.probe_ratchet.get(
                            f"{rec.market}|{rec.side}") or [1, 0.0])[0]
                        if front_by > allowed_c + 1e-6:
                            bad = (f"{front_by:.0f} ticks in front — "
                                   "the probe's earned reach is "
                                   f"{allowed_c}")
                        elif rec.qty > PROBE_MAX_QTY + 1e-6:
                            bad = ("probe-sized only in front of an "
                                   "unknown market")
                if bad is not None:
                    r_c = self.desk.cancel(rec.id, rec.market)
                    if r_c.ok:
                        self.orders.pop(rec.id, None)
                        self.evidence.order_gone(rec.market, rec.id)
                        self._log(event="conform_pulled",
                                  market=rec.market, side=rec.side,
                                  price=rec.price, qty=rec.qty,
                                  note=bad[:100])
                        actions -= 1
                    continue
            side_pool = self._side_pool(rec.market, prog)
            if side_pool is None:
                continue
            if not self._cooldown_ok(rec.market, rec.side, now):
                continue
            best = self._plan_side(rec.market, book, rec.side, prog,
                                   side_pool,
                                   self._market_budget(rec.market) / 2.0,
                                   own=rec,
                                   bar=(self.cfg.grow_floor
                                        if rec.purpose == "grow" else None))
            gain = (best["est"] if best else 0.0) - (rec.live_est or 0.0)
            measured = rec.live_ev if rec.live_ev is not None else rec.live_est
            if measured is not None and self.cfg.est_deflate > 1.0:
                measured = measured / self.cfg.est_deflate   # cull on the
                                                             # same deflated
                                                             # basis entries
                                                             # are judged on
            floor_here = (self.cfg.grow_floor if rec.purpose == "grow"
                          else self.cfg.min_est_day)
            below = measured is not None and measured < floor_here
            if below and not rec.weak_since:
                rec.weak_since = now
            elif not below:
                rec.weak_since = 0.0
            window_here = (self.cfg.grow_pull_s if rec.purpose == "grow"
                           else self.cfg.weak_pull_s)
            weak = (window_here > 0 and rec.weak_since
                    and now - rec.weak_since > window_here
                    and (best is None
                         or best.get("ev", best["est"]) < floor_here))
            gmin = 1.0 if self.cfg.whole_shares else QTY_GRID[0]
            shrink_cap = None
            fair_m = self.fairs(rec.market) if self.fairs is not None else None
            if fair_m is not None:
                past_t = (((rec.price - fair_m) if rec.side == "BUY"
                           else (fair_m - rec.price)) / book.tick)
                if past_t >= 3.0:
                    shrink_cap = gmin
                elif past_t >= 2.0:
                    shrink_cap = gmin * 3.0
                elif past_t >= 1.0:
                    shrink_cap = gmin * 8.0
            shrink_needed = (shrink_cap is not None
                             and rec.qty > shrink_cap + 1e-9)
            if fair_m is not None and (
                    (rec.side == "BUY"
                     and rec.price > fair_m - book.tick + 1e-9)
                    or (rec.side == "SELL"
                        and rec.price < fair_m + book.tick - 1e-9)):
                # the hard cap binds the RESTING book too, both sides
                # (owner, 2026-08-23): a quote past fair moves back to
                # a compliant slot or leaves — regardless of earnings
                shrink_needed = True
            if (self.cfg.wall_size_up and best is not None
                    and best["qty"] > rec.qty * 2 + 1e-9):
                # the size-up binds the RESTING book too (owner,
                # 2026-08-23: "I don't see any increase in nba order
                # sizes" — the dust joins placed before the rule never
                # repriced, because bigger size shows worse model EV).
                # An undersized join is forced to the full-size slot
                # exactly like an oversized one is forced to shrink.
                shrink_needed = True
            if (best is not None and best.get("revive")
                    and rec.purpose != "grow"):
                # the order earns nothing only because its side is below
                # Target Size, and a revive within the caps can qualify
                # it — that is the fix, not a cancel (owner, 2026-08-21:
                # "we shouldn't cancel something on the basis that it
                # does not earn rewards if the fix is easy i.e.
                # qualifying the side")
                rec.weak_since = 0.0
                continue
            if (best is None and rec.purpose != "grow"
                    and (rec.live_est or 0.0) <= 0.0
                    and sum(q for _, q in book.side(rec.side))
                    < float(prog.target)
                    and self._plan_side(rec.market, book, rec.side, prog,
                                        side_pool,
                                        self._market_budget(rec.market) / 2.0,
                                        own=rec, bar=0.0) is not None):
                # same caveat, bar aside: the side CAN be qualified within
                # the caps, it just does not pay enough to act on yet —
                # the order stays; the revive places if its EV ever clears
                rec.weak_since = 0.0
                continue
            if ((best is None and ((rec.live_est or 0.0) <= 0.0
                                   or shrink_needed)) or weak):
                r = self.desk.cancel(rec.id, rec.market)
                if r.ok:
                    why = (f"under {self.cfg.min_est_day * 100:.0f}c/day for "
                           f"{(now - rec.weak_since) / 3600:.1f}h — cycling "
                           f"out to the next best market" if weak else
                           "resting size past fair — a target; pulled "
                           "(owner, 2026-08-22)" if shrink_needed else
                           "earning nothing and no better spot")
                    self._log(event="pull", market=rec.market, side=rec.side,
                              why=why)
                    del self.orders[rec.id]
                    self._mark(rec.market, rec.side, now)
                    actions -= 1
            elif (best is not None
                    and (gain >= self.cfg.reprice_gain_day
                         or shrink_needed)
                    and (abs(best["px"] - rec.price) > 1e-9
                         or abs(best["qty"] - rec.qty) > 1e-9)
                    # a reprice that GROWS the order answers to the same
                    # ceilings as a new entry (the $121.99-of-$100
                    # lesson) — expected risk AND the worst-day gross
                    and (self.family_spent() - self._charge(rec)
                         + capital_at_risk(rec.intent, best["px"],
                                           best["qty"])
                         * min(max(rec.live_pf or 1.0, PF_CHARGE_FLOOR),
                               1.0)
                         <= self.cfg.capital_usd + 1e-9)
                    and (self.family_gross()
                         - capital_at_risk(rec.intent, rec.price, rec.qty)
                         + capital_at_risk(rec.intent, best["px"],
                                           best["qty"])
                         <= self.gross_cap() + 1e-9)):
                r = self.desk.reprice(
                    {"id": rec.id, "market": rec.market, "side": rec.side,
                     "price": rec.price, "size": rec.qty, "intent": rec.intent},
                    best["px"], new_qty=best["qty"])
                if r.ok:
                    self._log(event="reprice", market=rec.market, side=rec.side,
                              frm=rec.price, to=best["px"], qty=best["qty"])
                    if r.two_orders:
                        # the original REFUSED to cancel and still rests.
                        # It stays tracked — its collateral is real, the
                        # ceiling must see it — and the cancel is retried
                        # every cycle until it dies (owner, 2026-08-21: no
                        # ghosts left behind after a move).
                        rec.why = "cancel failed during a move — retrying"
                        self.alert(f"{self.cfg.tag}: two orders resting",
                                   f"{self._label(rec.market)}: the original "
                                   f"would not cancel during a move; holding "
                                   f"both and retrying the cancel")
                    else:
                        del self.orders[rec.id]
                    new_purpose = ("revive" if best.get("revive")
                                   else "solo" if best.get("solo")
                                   else "grow" if (rec.purpose == "grow"
                                   and best.get("ev", best["est"])
                                   < self.cfg.min_est_day)
                                   else "earn")
                    self.orders[r.order_id] = FamilyOrder(
                        id=r.order_id, market=rec.market, side=rec.side,
                        price=best["px"], qty=best["qty"], intent=rec.intent,
                        placed_ts=now, purpose=new_purpose,
                        why=best["why"], est_day=best["est"], share=best["share"])
                    self._mark(rec.market, rec.side, now)
                    actions -= 1
        return actions

    def _enter(self, now: float, positions: dict, actions: int) -> int:
        have = {(o.market, o.side) for o in list(self.orders.values())
                if o.purpose != "sell"}
        # proven ground first (owner, 2026-08-20: "looking at the orders
        # that were the most successful and trying to replicate those") —
        # a market's record of actually PAYING us counts alongside what
        # the book says it should pay now
        # Rank purely by summed plan EV (owner, 2026-08-30: "that is
        # what the expected value is for"). The old paid-history bonus
        # (up to +$5) was a crutch from the era when estimates overshot
        # 3-7x; with estimates now grading ~1.0x it dominated ranking
        # and fought the churn lesson. History still feeds confidence
        # and graduation — it just no longer double-counts into the
        # queue order. EV, not raw claim: the same number the placement
        # filter below judges by.
        ranked = sorted(((s, sb) for s, sb in self.scoreboard.items()
                         if sb.get("plans")),
                        key=lambda kv: -sum(p.get("ev", p["est"])
                                            for p in kv[1]["plans"]))
        for slug, sb in ranked:
            if actions <= 0:
                break
            if slug not in self.universe or self._dead_here(slug):
                continue
            if not self.enterable(slug):
                continue
            days = slug_days_out(slug, now)
            if days is not None and days < self.cfg.min_days_out:
                continue
            for plan in sb["plans"]:
                if actions <= 0:
                    break
                if plan.get("ev", plan["est"]) < self.cfg.min_est_day:
                    continue    # under the bar (old plans lack ev: use est)
                if (slug, plan["side"]) in have:
                    continue
                if not self._cooldown_ok(slug, plan["side"], now):
                    continue
                plan_charge = (plan["cost"] * min(
                    max(plan.get("p_fill") or 1.0, PF_CHARGE_FLOOR), 1.0)
                    if self.cfg.expected_risk else plan["cost"])
                if (self.market_spent(slug) + plan_charge
                        > self._market_budget(slug) + 1e-9
                        and not plan.get("revive")):
                    continue    # per-market EXPECTED-risk allowance
                if (self.market_gross(slug) + plan["cost"]
                        > self._per_market_gross(slug) + 1e-9
                        and not plan.get("revive")):
                    continue    # per-market worst-case ceiling
                guess = BUY_LONG if plan["side"] == "BUY" else BUY_SHORT
                if slug in self.proven and self.cfg.proven_usd > 0:
                    pool_orders = [o for o in list(self.orders.values())
                                   if o.market in self.proven]
                    if (self.proven_spent() + plan_charge
                            > self.cfg.proven_usd + 1e-9):
                        continue      # the proven pool has its own cap
                    if (self.family_gross() + risk.marginal(
                            pool_orders, slug, guess,
                            plan["px"], plan["qty"])
                            > self.gross_cap() + 1e-9):
                        continue      # the worst-day ceiling binds all
                else:
                    # The SAME book family_spent() measures — engine
                    # orders only. Leaving the owner's manual orders in
                    # here let negative-risk netting offset each new
                    # order against his book, so every placement looked
                    # cheaper than it was while the spend it was checked
                    # against excluded him. Politics reached $324.58
                    # against a $250 cap that way on 2026-08-25, the
                    # morning after manual orders stopped counting
                    # toward the ceiling. The two sides of the
                    # comparison have to be the same book.
                    search_orders = [o for o in list(self.orders.values())
                                     if o.market not in self.proven
                                     and o.purpose not in ("manual", "bond")]
                    if (self.family_spent() + plan_charge
                            > self.cfg.capital_usd + 1e-9):
                        continue      # the EXPECTED-risk ceiling
                    if (self.family_gross() + risk.marginal(
                            search_orders, slug, guess,
                            plan["px"], plan["qty"])
                            > self.gross_cap() + 1e-9):
                        continue      # the worst-day ceiling — dollars
                                      # bound the tail, not the model
                book = self.cache.fresh(slug, BOOK_MAX_AGE, now)
                if book is None:
                    continue
                net = (positions.get(slug) or (0.0,))[0]
                r = self.desk.place_resting(slug, plan["side"], plan["px"],
                                            plan["qty"], net_position=net,
                                            verify=self.cfg.verify_resting)
                if r.ok and r.order_id:
                    self.positions_seen.setdefault(slug, net)
                    self.orders[r.order_id] = FamilyOrder(
                        id=r.order_id, market=slug, side=plan["side"],
                        price=(r.price or plan["px"]),
                        qty=plan["qty"], intent=r.intent,
                        placed_ts=now,
                        purpose=("revive" if plan.get("revive")
                                 else "solo" if plan.get("solo") else "earn"),
                        why=plan["why"], est_day=plan["est"],
                        share=plan["share"],
                        live_pf=(round(plan["p_fill"], 4)
                                 if plan.get("p_fill") is not None
                                 else None))   # the guard admitted it at
                                               # these odds; the spend
                                               # charges the same
                    self._log(event="place", market=slug, side=plan["side"],
                              price=plan["px"], qty=plan["qty"],
                              est=plan["est"], why=plan["why"][:90])
                    self._mark(slug, plan["side"], now)
                    actions -= 1
                else:
                    self._log(event="refused", market=slug, side=plan["side"],
                              note=r.note[:90])
                    self._mark(slug, plan["side"], now)
        return actions

    def _trim(self, now: float, actions: int) -> int:
        while actions > 0:
            spent = self.family_spent()
            gross = self.family_gross()
            over_exp = spent > self.cfg.capital_usd + 1e-9
            over_gross = gross > self.gross_cap() + 1e-9
            if not over_exp and not over_gross:
                break
            cands = [o for o in list(self.orders.values())
                     if o.purpose not in ("sell", "manual", "bond")
                     and not o.pinned
                     and not self._frozen(o.market)]
            if not cands:
                break
            def value_per_dollar(o):
                est = (o.live_est if o.live_est is not None else o.est_day) or 0.0
                return est / max(self._charge(o), 0.01)
            worst = min(cands, key=value_per_dollar)
            r = self.desk.cancel(worst.id, worst.market)
            if not r.ok:
                break
            freed = self._charge(worst)
            self._log(event="trim", market=worst.market, side=worst.side,
                      why=((f"${spent:.2f} expected risk over the "
                            f"${self.cfg.capital_usd:.0f} ceiling"
                            if over_exp else
                            f"${gross:.2f} gross over the "
                            f"${self.gross_cap():.0f} worst-day ceiling")
                           + f" — freeing ${freed:.2f} from the lowest "
                             "earner per expected dollar"))
            del self.orders[worst.id]
            actions -= 1
        return actions

    def _grow(self, now: float, positions: dict, actions: int) -> int:
        if self.cfg.grow_usd <= 0 or actions <= 0:
            return actions
        spent = sum(capital_at_risk(o.intent, o.price, o.qty)
                    for o in list(self.orders.values()) if o.purpose == "grow")
        have = {(o.market, o.side) for o in list(self.orders.values())
                if o.purpose != "sell"}
        ranked = sorted(((s, sb) for s, sb in self.scoreboard.items()
                         if sb.get("grow")),
                        key=lambda kv: -(kv[1].get("potential") or 0.0))
        for slug, sb in ranked:
            if actions <= 0 or spent >= self.cfg.grow_usd - 1e-9:
                break
            if slug not in self.universe or self._dead_here(slug) \
                    or not self.enterable(slug):
                continue
            days = slug_days_out(slug, now)
            if days is not None and days < self.cfg.min_days_out:
                continue
            for plan in sb["grow"]:
                if actions <= 0 or spent + plan["cost"] > self.cfg.grow_usd + 1e-9:
                    break
                if (slug, plan["side"]) in have:
                    continue
                if not self._cooldown_ok(slug, plan["side"], now):
                    continue
                book = self.cache.fresh(slug, BOOK_MAX_AGE, now)
                if book is None:
                    continue
                net = (positions.get(slug) or (0.0,))[0]
                r = self.desk.place_resting(slug, plan["side"], plan["px"],
                                            plan["qty"], net_position=net,
                                            verify=self.cfg.verify_resting)
                if r.ok and r.order_id:
                    self.positions_seen.setdefault(slug, net)
                    self.orders[r.order_id] = FamilyOrder(
                        id=r.order_id, market=slug, side=plan["side"],
                        price=(r.price or plan["px"]),
                        qty=plan["qty"], intent=r.intent,
                        placed_ts=now, purpose="grow", why=plan["why"],
                        est_day=plan["est"], share=plan["share"],
                        live_pf=(round(plan["p_fill"], 4)
                                 if plan.get("p_fill") is not None
                                 else None))
                    self._log(event="grow", market=slug, side=plan["side"],
                              price=plan["px"], qty=plan["qty"],
                              why=plan["why"][:80])
                    self._mark(slug, plan["side"], now)
                    spent += plan["cost"]
                    actions -= 1
        return actions

    def _prune_excess_exits(self, slug: str, side: str, excess: float,
                            now: float) -> None:
        """Exits must never total more than the position they exit —
        an over-covered short flips long when everything fills (the
        Alabama six-covers-for-five-shares case, 2026-08-21). Pull the
        worst-earning excess, never manual orders."""
        cands = sorted((o for o in list(self.orders.values())
                        if o.market == slug and o.purpose == "sell"
                        and o.side == side and not o.pinned),
                       key=lambda o: (o.live_est or 0.0))
        for rec in cands:
            if excess < 0.01:
                break
            if rec.qty > excess + 0.01:
                continue          # too big to pull whole — fallback below
            r = self.desk.cancel(rec.id, rec.market)
            if r.ok:
                excess -= rec.qty
                self.orders.pop(rec.id, None)
                self.evidence.order_gone(rec.market, rec.id)
                self._log(event="excess_exit_pruned", market=slug,
                          price=rec.price, qty=rec.qty,
                          note="exits exceeded the position")
        if excess >= 0.01:
            # every remaining cover is BIGGER than the excess (the tulgab
            # 500-vs-1 shape) — cancel the worst earner whole; the next
            # pass rests one sized to the real position. Cancel-first, so
            # nothing is ever over-offered (owner approved 2026-08-21).
            for rec in cands:
                if rec.id not in self.orders:
                    continue
                r = self.desk.cancel(rec.id, rec.market)
                if r.ok:
                    self.orders.pop(rec.id, None)
                    self.evidence.order_gone(rec.market, rec.id)
                    self._log(event="excess_exit_pruned", market=slug,
                              price=rec.price, qty=rec.qty,
                              note="bigger than the position it covers — "
                                   "cancelled whole; the next pass rests "
                                   "one sized to the real position")
                break

    def _maybe_move_exit(self, slug: str, side: str, mine: list, book,
                         inv: dict, now: float) -> None:
        """A single resting exit in a clearly worse slot moves to the
        better one. Cancel-first on purpose: placing the replacement
        before cancelling would briefly offer MORE than the position
        holds, and that extra could fill. Ladders of several exits are
        left alone — moving them wholesale would churn the adopted
        book."""
        if len(mine) != 1:
            return
        rec = mine[0]
        qty = inv.get("qty") or 0.0
        if not self._cooldown_ok(slug, side, now):
            return
        if side == "SELL":
            break_even = min(max(inv.get("cost", 0.0) / qty, 0.001), 0.989)
            floor_px, sb = self._exit_floor(slug, "SELL", break_even,
                                            book.tick, book=book, qty=qty)
            lo = max(floor_px,
                     (book.bids[0][0] + book.tick) if book.bids else 0.002)
            # 2026-08-22: the target IS the front of the profitable
            # range — join the ask touch unless it gives away against
            # the model
            fair_m2 = self.fairs(slug) if self.fairs is not None else None
            jp = self._ask_anchor(slug, book, break_even)
            if fair_m2 is not None and jp < fair_m2 - 3 * book.tick:
                jp = fair_m2 - book.tick
            hi = max(min(jp, 0.999), lo)
        else:
            received = min(max(-inv.get("cost", 0.0) / -qty, 0.002), 0.999)
            cap_px, sb = self._exit_floor(slug, "BUY", received, book.tick,
                                          book=book, qty=-qty)
            hi = min(cap_px,
                     (book.asks[0][0] - book.tick) if book.asks
                     else cap_px)
            # empty bid side: the mirror of _ask_anchor — a buy-back
            # with no competition bids LOW, not at the expensive end
            lo = min((book.bids[0][0] if book.bids else 0.011), hi)
        gone = (rec.price, rec.qty)      # the order leaves the book if it moves
        if side == "SELL":
            # stock sells join the ask touch (owner, 2026-08-22: "sell
            # more aggressively"; the MN example — never undercut it,
            # never park behind it). Behind the touch pays less than the
            # touch by construction, so there is no better-paying slot
            # to look for; an empty ask side anchors high by the
            # 2026-08-27 rule.
            best = hi
        else:
            best = self._best_exit_px(slug, side, book, lo, hi, rec.qty,
                                      basis=sb, exclude=gone)
        if best is None:
            return
        # on the grid the placer uses: an off-grid anchor (fair + 15
        # ticks = 58.39c) against a resting 59c is not a move
        best = snap_price(best, book.tick, side)
        if abs(best - rec.price) < book.tick / 2:
            return
        # the placer has the LAST word on price (owner, 2026-08-30:
        # exits were cancelled and re-placed at the same price every
        # cycle because this mover and the re-rest path priced
        # independently — the exit gate pinned the replacement right
        # back where it was). Consult the same gate the placer will;
        # if the final price would not move, the order does not move.
        # And the re-rest is HANDED this price (2026-09-04), so the
        # pair cannot disagree on a book still showing the old order.
        basis_g = (break_even if side == "SELL" else received)
        gate_g = self._exit_gate(slug, side, basis_g, rec.qty, book, now)
        predicted = snap_price(gate_g if gate_g is not None else best,
                               book.tick, side)
        if abs(predicted - rec.price) < book.tick / 2:
            return
        prog, _w = self._prog_row(slug)
        side_pool = self._side_pool(slug, prog) if prog is not None else None
        if prog is None or side_pool is None:
            return
        levels = self._levels_less(book.side(side), gone)
        j = estimate_join(side, levels, book.tick, float(prog.df),
                          float(prog.target), predicted, rec.qty)
        best_est = (j.share * side_pool
                    if j.qualifies and j.in_window else 0.0)
        cur_est = rec.live_est or 0.0
        if side != "SELL" and best_est < cur_est * 1.5 + 0.05:
            return                      # covers: move only when clearly better
        # stock sells always come to the front (owner, 2026-08-22:
        # "sell more aggressively") — the cooldown throttles the churn
        r = self.desk.cancel(rec.id, rec.market)
        if r.ok:
            self.orders.pop(rec.id, None)
            self.evidence.order_gone(rec.market, rec.id)
            self._replace_at[f"{slug}|{side}"] = (predicted, now, rec.qty)
            # the gain is already in hand: best_est is what the model
            # scores the new slot at, cur_est what it was earning. The
            # Sold tab reports the sum of these instead of one line per
            # move (owner, 2026-08-31). best_est is measured at `best`,
            # the target slot; the order rests at the gate's price.
            self._note_wind_down(slug, "exit move", rec.qty, predicted,
                                 now, left=(qty or rec.qty),
                                 from_px=rec.price,
                                 gain=best_est - cur_est)
            self._log(event="exit_moved", market=slug, price=rec.price,
                      qty=rec.qty,
                      note=f"a slot at {best:.2f} earns more — moving")

    # Fill-time quartiles by context — spread width x how far past the
    # touch the order rested — measured from our own 368 fills that
    # carry both the book at placement and the exchange's resting time
    # (2026-08-25). A fill faster than its cell's 25th percentile is a
    # SNATCH; the same minutes mean opposite things at the touch of a
    # tight book (25th pct: 2 hours) and deep in a wide one (10 min).
    # Thin cells fall back to the pooled row. Minutes.
    FILL_TIME_SEED = {
        ("tight", "touch"): (122.9, 1750.3),
        ("tight", "deep"): (0.4, 51.5),
        ("mid", "touch"): (38.5, 648.6),
        ("mid", "deep"): (0.4, 56.9),
        ("wide", "touch"): (13.3, 1309.1),
        ("wide", "deep"): (10.0, 681.4),
        ("pooled", "pooled"): (16.5, 1062.2),
    }

    def _accrue_expected(self, purpose: str, pf: float, dt_s: float,
                         now: float) -> None:
        """Bank the fills the model expected from one order over the
        dt_s it just rested, in this hour's bucket, by purpose."""
        if dt_s <= 0 or pf is None or pf <= 0:
            return
        h = -math.log(max(1.0 - min(pf, 0.999999), 1e-9))   # fills/day
        key = str(int(now // 3600) * 3600)
        cell = self.exp_fills.setdefault(key, {})
        cell[purpose] = round(cell.get(purpose, 0.0) + h * dt_s / DAY_S, 6)
        if len(self.exp_fills) > 26:
            for k in sorted(self.exp_fills, key=int)[:-26]:
                self.exp_fills.pop(k, None)

    def expected_fills_24h(self, now: float) -> tuple[dict[str, float], float]:
        """(expected fills by purpose over the last 24h, when that record
        starts). The start matters right after a boot: the note grades
        only the hours it actually watched."""
        out: dict[str, float] = {}
        since = None
        floor = int((now - DAY_S) // 3600) * 3600
        for k, cell in self.exp_fills.items():
            ts = int(k)
            if ts < floor:
                continue
            since = ts if since is None else min(since, ts)
            for p, v in cell.items():
                out[p] = out.get(p, 0.0) + v
        return out, (float(since) if since is not None else now)

    def _fill_speed_verdict(self, side: str, px: float, rested_s: float,
                            book) -> tuple[float, str, float]:
        """(evidence weight, verdict text, advancement fraction) for a
        fill, judged against its own context's clock (owner, 2026-08-25:
        fast = past fair, a while = in range — and the clock depends on
        the spread and where we rested)."""
        adv_frac = 0.0
        cell = ("pooled", "pooled")
        if book is not None and book.bids and book.asks:
            tb, ta = book.bids[0][0], book.asks[0][0]
            spread = (ta - tb) * 100.0
            adv = ((px - tb) if side == "BUY" else (ta - px)) * 100.0
            adv_frac = max(adv, 0.0) / max(spread, 1.0)
            sb = ("tight" if spread <= 2.0 + 1e-6       # 0.46-0.44 is
                  else "mid" if spread <= 5.0 + 1e-6      # 2.0000000018c
                  else "wide")                            # in floats
            ab = "touch" if adv_frac < 0.01 else (
                 "front" if adv_frac < 0.33 else "deep")
            cell = (sb, "deep" if ab == "front" else ab)
        q25, _q75 = (self.FILL_TIME_SEED.get(cell)
                     or self.FILL_TIME_SEED[("pooled", "pooled")])
        mins = max(rested_s, 0.0) / 60.0
        if mins < q25:
            return (SNATCH_WEIGHT,
                    f"snatched in {mins:.0f} min (this context's 25th "
                    f"percentile is {q25:.0f} min) — fair is past this "
                    f"price", adv_frac)
        return 1.0, f"filled after {mins:.0f} min — within the range", adv_frac

    def nurse(self, now: float, client) -> None:
        """The between-cycles guard (owner, 2026-08-25: "A process
        should stick with it just to monitor and guard against quick
        movements by others that would not get caught until a full
        cycle pass").

        Watches every engine order younger than NURSE_STABLE_S on a
        model-less market. Two dangers, both pulled on sight:

        * FRONTED — the order was alone in front of its side and
          someone has now quoted past it. The jumper knows something;
          the order's score collapsed with the jump; sitting there is
          pure fill risk for nothing.
        * RUSHED — the opposite touch has closed on the order by
          NURSE_APPROACH_TICKS since the watch began, or sits a tick
          away. That speed is a taker forming, not drift.

        Runs on the SAME thread as every other order operation (the
        run loop's sleep is broken into nurse ticks), so no cancel can
        race the cycle. Pulled markets get the normal cooldown mark so
        the next cycle does not instantly re-place into the same
        trap. Manual orders are never nursed, exits only when
        hand-set; a hand-set (pinned) order of any other purpose is
        watched on any market, model or not. Frozen and avoided
        ground is skipped wholesale."""
        watch = []
        for rec in list(self.orders.values()):
            if rec.purpose in ("manual", "bond"):
                continue
            if rec.purpose == "sell" and not rec.pinned:
                continue
            # a hand-set order is watched from the moment the owner set
            # it, on ANY market (owner, 2026-08-25: "the nurse can also
            # stay active for this") — a nurse pull ends the pin with
            # the order, and the next cycle re-judges under the rules
            age_ref = rec.pin_ts if rec.pinned else rec.placed_ts
            if now - age_ref > NURSE_STABLE_S:
                self._nurse_base.pop(rec.id, None)
                continue
            if self._frozen(rec.market) or self._avoided(rec.market):
                continue
            if (not rec.pinned and self.fairs is not None
                    and self.fairs(rec.market) is not None):
                continue
            watch.append(rec)
        if not watch:
            if self._nurse_base:
                self._nurse_base.clear()
            return
        for rec in watch:
            book = self.cache.fresh(rec.market, NURSE_BOOK_MAX_AGE_S, now)
            if book is None and client is not None:
                try:
                    book = client.book(rec.market, fetched_at=now)
                    self.cache.put(rec.market, book)
                except Exception:  # noqa: BLE001 — a fetch miss skips a tick
                    continue
            if book is None:
                continue
            sign = 1.0 if rec.side == "BUY" else -1.0
            mine_side = [(p, q - rec.qty if abs(p - rec.price) < 1e-9 else q)
                         for p, q in book.side(rec.side)]
            mine_side = [(p, q) for p, q in mine_side if q > 1e-9]
            opp = book.side("SELL" if rec.side == "BUY" else "BUY")
            opp_t = opp[0][0] if opp else None
            base = self._nurse_base.get(rec.id)
            if base is None:
                in_front = (not mine_side
                            or (rec.price - mine_side[0][0]) * sign > 1e-9)
                self._nurse_base[rec.id] = {"opp": opp_t,
                                            "front": in_front}
                continue
            why = None
            if (base.get("front") and mine_side
                    and (mine_side[0][0] - rec.price) * sign > 1e-9):
                why = (f"fronted — {mine_side[0][0]*100:.0f}c quoted past "
                       f"our {rec.price*100:.0f}c; the jumper knows "
                       "something")
            elif opp_t is not None:
                gap_t = abs(opp_t - rec.price) / book.tick
                moved = (abs(base["opp"] - opp_t) / book.tick
                         if base.get("opp") is not None else 0.0)
                closing = (base.get("opp") is not None
                           and (base["opp"] - opp_t) * sign > 1e-9)
                # a pull needs actual MOVEMENT toward us — on a tight
                # book a one-tick gap is the normal resting state, and
                # the first hour live showed pulls reading "rushed from
                # 2c to 2c" on healthy orders
                if closing and (gap_t <= 1.0 + 1e-6
                                or moved >= NURSE_APPROACH_TICKS):
                    why = (f"the {'ask' if rec.side == 'BUY' else 'bid'} "
                           f"rushed from {base['opp']*100:.0f}c to "
                           f"{opp_t*100:.0f}c — a taker forming, not drift")
            if why is None:
                continue
            r = self.desk.cancel(rec.id, rec.market)
            if r.ok:
                self.orders.pop(rec.id, None)
                self._nurse_base.pop(rec.id, None)
                self.evidence.order_gone(rec.market, rec.id)
                # NO cooldown and NO price memory (owner, 2026-08-25:
                # "You shouldn't wait, if a replacement is called for
                # by the rules, it should happen quickly. If it is
                # not, then it should not."). The next cycle re-judges
                # this market under exactly the rules every placement
                # answers to — ratchet, band edge, probe size, the
                # deflated bar — and does whatever they say.
                self._log(event="nursed_pull", market=rec.market,
                          side=rec.side, price=rec.price, qty=rec.qty,
                          note=why[:110])

    def _evidence_cap(self, slug: str, side: str, b_lo, b_hi):
        """The price line the evidence supports on a MODEL-LESS market
        that has real fills (owner, 2026-08-25, the kamhar card: bought
        14c at the touch against a 5-12c band, on a market we had
        already round-tripped 13c -> 5c for -$25 the week before).

        A BUY caps at the band's low edge, sliding toward the band's
        center as fill-built confidence grows; a SELL mirrors from the
        high edge. Joins are NOT exempt: 'joins the touch' was the one
        door the fronting rules left open, and joining a bait quote 6c
        past our own valuation is the same trade as fronting it.
        Returns None where it does not apply: modeled markets (the
        fair hard cap governs) and markets with no fills (the band
        there is only the spread certifying itself)."""
        if b_lo is None or b_hi is None:
            return None
        if not any(r[1].startswith("fill")
                   for r in self.events_of(slug)):
            return None
        ctr = (b_lo + b_hi) / 2.0
        conf = self.evidence.confidence(slug)
        return (b_lo + (ctr - b_lo) * conf if side == "BUY"
                else b_hi - (b_hi - ctr) * conf)

    def events_of(self, slug: str):
        return self.evidence.events.get(slug, ())

    def _advance_probe_ratchet(self, slug: str, side: str,
                               now: float) -> None:
        """A probe that survived a quiet day earns one more tick of
        reach, at most one per day, capped — each step licensed by the
        market itself proving the last one safe (owner, 2026-08-25:
        "only to the extent necessary to get information")."""
        key = f"{slug}|{side}"
        ticks, last = self.probe_ratchet.get(key) or [1, 0.0]
        if now - last >= 86400.0 and ticks < 8:
            self.probe_ratchet[key] = [ticks + 1, now]

    def _flush_fill_evidence(self, now: float) -> None:
        """Feed this cycle's fills to the evidence band — one EVENT per
        market-and-side. Seven rungs of a ladder swept in one moment
        are one loud observation at the deepest rung, not seven
        ordinary votes (owner, 2026-08-25, the Johnson ladder: 76
        shares across 7 rungs, all gone in the same minute)."""
        if not self._fill_evi_buf:
            return
        groups: dict[tuple, list[dict]] = {}
        for f in self._fill_evi_buf:
            groups.setdefault((f["market"], f["side"]), []).append(f)
        self._fill_evi_buf = []
        for (mkt, side), fs in groups.items():
            lead = max(fs, key=lambda f: f["adv"])
            w = max(f["weight"] for f in fs)
            self.evidence.fill(mkt, side, lead["px"], ts=now, weight=w)
            note = lead["verdict"]
            if len(fs) > 1:
                note = (f"{len(fs)} rungs swept together — judged at "
                        f"the deepest; " + note)
            self._log(event="fill_evidence", market=mkt, side=side,
                      price=lead["px"], weight=w, note=note[:110])

    def _exit_giveup_in_play(self) -> float:
        """Total give-up the family's exits currently risk below (SELL)
        or above (BUY) their positions' break-even. The exit gate's
        family-wide budget draws down against this."""
        total = 0.0
        for o in list(self.orders.values()):
            if o.purpose != "sell":
                continue
            inv = self.inventory.get(o.market) or {}
            q = inv.get("qty") or 0.0
            if not q:
                continue
            basis = abs((inv.get("cost") or 0.0) / q)
            give = ((basis - o.price) if o.side == "SELL"
                    else (o.price - basis)) * o.qty
            # small exits live outside the budget (owner, 2026-09-02)
            if give > self.cfg.exit_small_giveup_usd:
                total += give
        return total

    def _exit_gate(self, slug: str, side: str, basis: float, qty: float,
                   book, now: float) -> float | None:
        """The one path past break-even for an exit (owner, 2026-08-25,
        option B): the price is allowed only while the reward measured
        AT THAT PRICE, deflated for the estimator's known ~3x optimism,
        beats the expected fill loss by GATE_MARGIN — using the fill
        model's own odds, not a guess — inside the family's total
        give-up budget.

        Checked on the IL senate book where the trade-off is real:
        fronting an empty side at 2c earns 67% of the pool and passes;
        the same discount on a 183-share lot fails on size alone.
        Candidates are the achievable fronts (one tick inside each
        touch); the one with the LEAST give-up that passes wins, so the
        engine never gives more price than the reward justifies.

        SMALL exits (owner, 2026-09-02): when the whole give-up is under
        exit_small_giveup_usd the family budget does not apply, and
        joining our own touch is a candidate too — a wide book has no
        achievable front worth the extra give-up, and the cfb earners
        that paid $4-8/day were 1-2 share covers sitting AT the touch.
        The reward-beats-fill-loss test is unchanged for them."""
        if book is None or qty <= 0:
            return None
        prog, _w = self._prog_row(slug)
        if prog is None or not prog.is_live():
            return None
        side_pool = self._side_pool(slug, prog)
        if side_pool is None or side_pool <= 0:
            return None
        tick = book.tick
        small_cap = self.cfg.exit_small_giveup_usd
        cands = set()
        if book.bids:
            cands.add(round(book.bids[0][0] + tick, 3))
        if book.asks:
            cands.add(round(book.asks[0][0] - tick, 3))
        # joining our own side's touch never crosses; small exits only
        joins = set()
        own = book.bids if side == "BUY" else book.asks
        if own:
            joins.add(round(own[0][0], 3))
        if book.bids:      # post-only: a SELL never crosses the bid
            lo_ok = book.bids[0][0] + tick - 1e-9
        else:
            lo_ok = 0.002
        if book.asks:      # ...and a BUY never crosses the ask
            hi_ok = book.asks[0][0] - tick + 1e-9
        else:
            hi_ok = 0.998
        budget = self.cfg.exit_giveup_cap_usd - self._exit_giveup_in_play()
        # our current exits on this side leave the book before scoring,
        # so we never count our own size as competition
        mine_now = [(o.price, o.qty) for o in list(self.orders.values())
                    if o.market == slug and o.purpose == "sell"
                    and o.side == side]
        raw = list(book.side(side))
        for mp, mq in mine_now:
            raw = [(p2, (q2 - mq) if abs(p2 - mp) < 1e-9 else q2)
                   for p2, q2 in raw]
        lv = [(p2, q2) for p2, q2 in raw if q2 > 1e-9]
        best = None
        for px in cands | joins:
            if not (0.002 <= px <= 0.998):
                continue
            joining = px in joins
            if joining:
                # our own touch crosses only on a locked book
                if ((side == "BUY" and px > hi_ok)
                        or (side == "SELL" and px < lo_ok)):
                    continue
            elif px < lo_ok or px > hi_ok:
                continue
            give = ((basis - px) if side == "SELL"
                    else (px - basis)) * qty
            if give <= 0.01:
                continue
            small = give <= small_cap
            if joining and not small:
                continue            # joining is the small exit's privilege
            if not small and give > budget:
                continue
            j = estimate_join(side, lv, tick, float(prog.df),
                              float(prog.target), px, qty)
            est = (j.share * side_pool
                   if j.qualifies and j.in_window else 0.0)
            if est <= 0:
                continue
            closer = sum(q2 for p2, q2 in lv
                         if (p2 - px) * (-1.0 if side == "SELL" else 1.0)
                         > 1e-9)
            at_level = sum(q2 for p2, q2 in lv if abs(p2 - px) <= 1e-9)
            pf = self.fillmodel.p_fill(slug, side, j.ticks,
                                       shield=closer + at_level,
                                       target=float(prog.target))
            if est / EST_DEFLATE >= GATE_MARGIN * pf * give:
                if best is None or give < best[0]:
                    best = (give, px)
        return best[1] if best else None

    def _exit_floor(self, slug: str, side: str, basis: float,
                    tick: float, book=None,
                    qty: float | None = None) -> tuple[float, float]:
        """(price limit, scoring basis) for an exit. Break-even bounds it
        by default. When the model prices the market and says holding to
        resolution loses MORE than closing near fair, the limit extends
        to fair (owner, 2026-08-21, the Massachusetts short). With no
        model, the EVIDENCE BAND's conservative edge does the same job —
        sell no lower than the band's top, cover no higher than its
        bottom (owner, 2026-08-22: stranded exits must be able to fill).

        The dust and dry-exit paths that once priced past break-even
        here are gone (2026-08-25): after the maintenance wipe they
        priced exits to fill against ghost books — a SELL at 2c on a
        56c basis. Every price past break-even now goes through ONE
        door, _exit_gate, which demands the reward at that price pay
        for the fill risk it takes. The model-fair path stays: a model
        saying the position is worth less than basis is an authorized
        loss-cut, not a discount for speed."""
        fair = self.fairs(slug) if self.fairs is not None else None
        if fair is None and book is not None:
            try:
                band = self._band(slug, book.bids, book.asks, book.tick)
            except Exception:  # noqa: BLE001
                band = None
            if band:
                edge = band.get("hi") if side == "SELL" else band.get("lo")
                if edge is not None:
                    fair = edge / 100.0
        if side == "SELL":
            if fair is not None and fair < basis:
                fl = max(fair, 0.002)
                return fl, fl
            return basis + tick, basis
        if fair is not None and fair > basis:
            cp = min(fair, 0.998)
            return cp, cp
        return basis - tick, basis

    def _capital_charge_rate(self, slug: str) -> float:
        """The rate a candidate pays for tying capital up. Opportunity
        cost only exists under scarcity (owner, 2026-08-22: "opportunity
        cost is not a factor here" — the ceiling has slack): the
        marginal-cent rate scaled by how full the relevant pool is."""
        r = self._exit_opportunity_rate()
        if slug in self.proven and self.cfg.proven_usd > 0:
            util = self.proven_spent() / self.cfg.proven_usd
        else:
            util = self.family_spent() / max(self.cfg.capital_usd, 1e-9)
        return r * min(max(util, 0.0), 1.0)

    def _exit_opportunity_rate(self) -> float:
        """$/day one freed cent could earn — the owner's definition
        (2026-08-21): "assume that we could use each cent gained from a
        sale about as effectively on average as our last marginal cent."
        Measured as the lower-quartile value-per-dollar among the
        deployed earn orders: the rate of the last money we chose to
        put to work, not the average of the best of it."""
        rates = []
        for o in list(self.orders.values()):
            if o.purpose in ("sell", "manual", "probe", "bond"):
                continue
            risk = capital_at_risk(o.intent, o.price, o.qty)
            if risk > 0.005 and (o.live_est or 0.0) > 0:
                # cap what one order's claim may testify (owner,
                # 2026-08-22: "realistically we can do no better than
                # 2 dollars a day on one dollar worth of capital")
                rates.append(min((o.live_est or 0.0) / risk, 2.0))
        if not rates:
            return 0.0
        rates.sort()
        return rates[max(len(rates) // 4 - 1, 0)]

    def _exit_score(self, est: float, pf: float, qty: float, px: float,
                    basis: float, side: str, r_eff: float,
                    d_off: float) -> float:
        """$/day value of resting an exit at px: what it earns resting,
        plus fill odds times (the realized profit over basis AND the
        freed capital redeployed at the book's rate for the measured
        hold). The owner's exit math, 2026-08-21."""
        if side == "SELL":
            profit = max(px - basis, 0.0) * qty
            freed = px * qty
        else:
            profit = max(basis - px, 0.0) * qty
            freed = (1.0 - px) * qty
        return est + pf * (profit + freed * r_eff * d_off)

    @staticmethod
    def _levels_less(levels, exclude: tuple | None) -> list:
        """The book's side without one of our own orders in it — the
        order about to be moved is not competition for its own
        replacement, and a cancelled one still shows in the cached
        book for a while."""
        out = []
        for p, q in levels:
            if exclude is not None and abs(p - exclude[0]) < 1e-9:
                q = q - exclude[1]
            if q > 1e-9:
                out.append((p, q))
        return out

    def _slot_est(self, slug: str, side: str, book, px: float, qty: float,
                  exclude: tuple | None = None) -> float | None:
        """$/day the model scores an exit of qty at px. None when the
        market cannot be scored (no program row, no pool divisor)."""
        prog, _w = self._prog_row(slug)
        if prog is None:
            return None
        side_pool = self._side_pool(slug, prog)
        if side_pool is None:
            return None
        levels = self._levels_less(book.side(side), exclude)
        j = estimate_join(side, levels, book.tick, float(prog.df),
                          float(prog.target), px, qty)
        return j.share * side_pool if j.qualifies and j.in_window else 0.0

    def _exit_slots(self, slug: str, side: str, book, lo: float,
                    hi: float, qty: float, basis: float | None,
                    exclude: tuple | None) -> list[tuple]:
        """(price, est $/day, score) for each candidate exit slot in
        [lo, hi]. Empty when the market cannot be scored."""
        lo, hi = round(lo, 3), round(hi, 3)
        if hi < lo:
            return []
        prog, _w = self._prog_row(slug)
        if prog is None:
            return []
        side_pool = self._side_pool(slug, prog)
        levels = self._levels_less(book.side(side), exclude)
        touch = levels[0][0] if levels else (hi if side == "SELL" else lo)
        r_eff = self._exit_opportunity_rate()
        d_off = self.fillmodel.expected_offload_days(slug)
        base = basis if basis is not None else (lo if side == "SELL" else hi)
        n = int(round((hi - lo) / book.tick)) + 1
        step = max(1, n // 24)            # sample big ranges, walk small
        cands = [round(lo + i * book.tick, 3) for i in range(0, n, step)]
        if cands[-1] != hi:
            cands.append(hi)
        rows = []
        for px in cands:
            j = estimate_join(side, levels, book.tick, float(prog.df),
                              float(prog.target), px, qty)
            est = (j.share * side_pool
                   if side_pool is not None and j.qualifies and j.in_window
                   else 0.0)
            ticks = (max(round((px - touch) / book.tick), 0)
                     if side == "SELL"
                     else max(round((touch - px) / book.tick), 0))
            pf = self.fillmodel.p_fill(slug, side, ticks,
                                       target=float(prog.target))
            score = self._exit_score(est, pf, qty, px, base, side,
                                     r_eff, d_off)
            rows.append((px, est, score))
        return rows

    @staticmethod
    def _top_slot(rows: list[tuple], side: str) -> float | None:
        best_px, best_key = None, None
        for px, _est, score in rows:
            near = -px if side == "SELL" else px
            key = (round(score, 4), near)
            if best_key is None or key > best_key:
                best_px, best_key = px, key
        return best_px

    def _paying_exit_px(self, slug: str, side: str, book, lo: float,
                        hi: float, qty: float,
                        basis: float | None = None,
                        exclude: tuple | None = None) -> float | None:
        """The best-scoring slot in [lo, hi] that PAYS, or None when
        none does (owner, 2026-09-04: exits must earn while they wait)."""
        rows = [r for r in self._exit_slots(slug, side, book, lo, hi, qty,
                                            basis, exclude)
                if r[1] >= EXIT_PAYS_MIN_USD]
        return self._top_slot(rows, side)

    def _best_exit_px(self, slug: str, side: str, book, lo: float,
                      hi: float, qty: float,
                      basis: float | None = None,
                      exclude: tuple | None = None) -> float:
        """The exit slot with the best $/day VALUE: resting earnings
        plus the expected gain of actually exiting (profit + freed
        money redeployed). With slack in the ceiling this reduces to
        the best-earning slot; with the ceiling binding it concedes
        toward faster exits (owner, 2026-08-21: opportunity cost).

        Paying slots first (owner, 2026-09-04): while any slot in the
        range pays, the choice is among those — a deep buy-back's
        paper profit no longer outbids a slot that actually earns.
        Only when nothing pays does the raw score decide."""
        lo, hi = round(lo, 3), round(hi, 3)
        if hi < lo:
            return hi
        rows = self._exit_slots(slug, side, book, lo, hi, qty, basis, exclude)
        if not rows:
            return hi if side == "SELL" else lo
        paying = [r for r in rows if r[1] >= EXIT_PAYS_MIN_USD]
        px = self._top_slot(paying or rows, side)
        return px if px is not None else (hi if side == "SELL" else lo)

    def _sell(self, now: float, actions: int) -> int:
        for slug, inv in list(self.inventory.items()):
            if actions <= 0:
                break
            qty = inv.get("qty") or 0.0
            if abs(qty) < 0.01:
                continue
            if self._dead_here(slug):
                continue      # out means out — no resting anything there
            if self._avoided(slug) or self._frozen(slug):
                continue      # the owner works these by hand: the engine
                              # rests NO exits here (owner, 2026-08-22),
                              # and a FROZEN market is not even tidied
            # the bond shares are the bonds module's to exit (owner,
            # 2026-09-02); the engine works only what is left over
            qty = round(qty - self.bond_qty.get(slug, 0.0), 4)
            if abs(qty) < 0.01:
                continue
            book = self.cache.fresh(slug, BOOK_MAX_AGE, now)
            if book is None:
                continue
            if self._liquidating(slug) and qty >= 0.01:
                # OWNER-ORDERED CLOSE-OUT (2026-08-27, DeSantis 2028):
                # sell into the bid, never worse, up to its displayed
                # size each cycle until flat — regardless of basis;
                # the loss is the owner's explicit choice. His own
                # asks are never touched and never double-offered.
                if not book.bids:
                    continue
                bid_l, bidsz_l = book.bids[0]
                manual_l = sum(o.qty for o in list(self.orders.values())
                               if o.market == slug and o.side == "SELL"
                               and o.purpose in ("manual", "bond"))
                dq_l = round(min(qty - manual_l, bidsz_l), 2)
                if dq_l < 0.01:
                    continue
                for o2 in [o for o in list(self.orders.values())
                           if o.market == slug and o.purpose == "sell"
                           and o.side == "SELL" and not o.pinned]:
                    rr = self.desk.cancel(o2.id, o2.market)
                    if rr.ok:
                        self.orders.pop(o2.id, None)
                        self.evidence.order_gone(o2.market, o2.id)
                r_l = self.desk.place_resting(
                    slug, "SELL", bid_l, dq_l, net_position=qty,
                    intent=SELL_LONG, taker=True, verify=False)
                if r_l.ok:
                    inv["qty"] = round(inv.get("qty", 0.0) - dq_l, 4)
                    inv["cost"] = round(inv.get("cost", 0.0)
                                        - dq_l * bid_l, 4)
                    left_l = round(inv["qty"], 2)
                    if abs(inv["qty"]) < 0.005:
                        self.inventory.pop(slug, None)
                        self.inv_since.pop(slug, None)
                    self._journal_fill(FamilyOrder(
                        id=r_l.order_id or f"liq{int(now)}",
                        market=slug, side="SELL", price=bid_l, qty=dq_l,
                        intent=SELL_LONG, placed_ts=now, purpose="sell",
                        why="owner-ordered close-out — sold into the "
                            "bid"), dq_l, now, left_l)
                    self._note_wind_down(slug, "close-out", dq_l, bid_l,
                                         now, left=left_l)
                    self._log(event="liquidated", market=slug,
                              price=bid_l, qty=dq_l,
                              note=f"owner's close-out — {left_l:g} "
                                   "left" if left_l >= 0.01
                                   else "owner's close-out — flat")
                    actions -= 1
                continue
            if qty >= 0.01:
                # long stock: an ask at break-even or better
                mine = [o for o in list(self.orders.values())
                        if o.market == slug and o.purpose == "sell"
                        and o.side == "SELL" and not o.pinned]
                self._maybe_move_exit(slug, "SELL", mine, book, inv, now)
                # the owner's own resting SELLs of this stock count as
                # cover too — the engine sizes around them and never
                # offers the same shares twice (owner, 2026-08-22)
                manual_cover = sum(
                    o.qty for o in list(self.orders.values())
                    if o.market == slug and o.purpose in ("manual", "bond")
                    and o.side == "SELL")
                covered = manual_cover + sum(
                    o.qty for o in list(self.orders.values())
                    if o.market == slug and o.purpose == "sell"
                    and o.side == "SELL")
                rest = qty - covered
                if covered > qty + 0.01:
                    self._prune_excess_exits(slug, "SELL", covered - qty, now)
                # THE CARVED EXCEPTION (owner, 2026-08-22 "Carve it"):
                # the taker dump — a limit SELL of held stock priced AT
                # the bid, never worse. Tight spread only, never past the
                # bid's displayed size, never a giveaway against the
                # model, exits cancelled first, capped per day.
                if (self.cfg.dump_usd_day > 0 and actions > 0
                        and not self._avoided(slug)
                        and book.bids and book.asks
                        and self._cooldown_ok(slug, "SELL", now)
                        and self.dump_today
                        < self.cfg.dump_usd_day - 1e-9):
                    be_d = min(max(inv.get("cost", 0.0) / qty, 0.001),
                               0.989)
                    fair_d = (self.fairs(slug)
                              if self.fairs is not None else None)
                    bid_t, bid_sz = book.bids[0]
                    profit_gate = bid_t >= be_d + 2 * book.tick
                    # the dead-money drain (owner, 2026-08-29 "Sounds
                    # good. Yes, thanks", after declining a weekly
                    # liquidation): stock whose ENGINE exits have
                    # measured ~nothing for dead_drain_s may leave AT
                    # the bid even slightly under cost — never more
                    # than 5 ticks under break-even; every other rail
                    # of the carved exception is unchanged. Stock
                    # covered only by the owner's hand exits has no
                    # engine exit here and is never drained. A fresh
                    # or unmeasured exit (live_est None) is not dead.
                    dead_gate = False
                    if self.cfg.dead_drain_s > 0 and not profit_gate:
                        exits_here = [o for o in list(self.orders.values())
                                      if o.market == slug
                                      and o.purpose == "sell"
                                      and o.side == "SELL"]
                        dead_gate = (
                            bool(exits_here)
                            and all(o.live_est is not None
                                    and o.live_est < 0.005
                                    for o in exits_here)
                            and min(o.placed_ts for o in exits_here)
                            <= now - self.cfg.dead_drain_s
                            and bid_t >= be_d - 5 * book.tick)
                    if ((profit_gate or dead_gate)
                            and book.asks[0][0] - bid_t
                            <= 2 * book.tick + 1e-9
                            and (fair_d is None
                                 or bid_t >= fair_d - 3 * book.tick)):
                        # hand-set exits stay resting like manual ones —
                        # the dump sizes around them, never cancels them
                        pinned_cover = sum(
                            o.qty for o in list(self.orders.values())
                            if o.market == slug and o.purpose == "sell"
                            and o.side == "SELL" and o.pinned)
                        dq = min(qty - manual_cover - pinned_cover, bid_sz,
                                 (self.cfg.dump_usd_day
                                  - self.dump_today)
                                 / max(bid_t, 0.01))
                        if self.cfg.whole_shares:
                            dq = float(int(dq))
                        dq = round(dq, 2)
                        if dq >= (1.0 if self.cfg.whole_shares else 0.01):
                            for o2 in [o2 for o2 in list(self.orders.values())
                                       if o2.market == slug
                                       and o2.purpose == "sell"
                                       and o2.side == "SELL"
                                       and not o2.pinned]:
                                rr = self.desk.cancel(o2.id, o2.market)
                                if rr.ok:
                                    self.orders.pop(o2.id, None)
                                    self.evidence.order_gone(o2.market,
                                                             o2.id)
                            r2 = self.desk.place_resting(
                                slug, "SELL", bid_t, dq,
                                net_position=qty, intent=SELL_LONG,
                                taker=True, verify=False)
                            if r2.ok:
                                self.dump_today = round(
                                    self.dump_today + dq * bid_t, 2)
                                # the sale is journaled HERE, at the
                                # known price — dumps used to leave no
                                # record, so every one surfaced later
                                # as "closed by reconciliation, no
                                # price recorded" (owner, 2026-08-23).
                                # A rare partial fill rests at the bid
                                # and the exchange snapshot reconciles.
                                inv["qty"] -= dq
                                inv["cost"] -= dq * bid_t
                                left = round(inv["qty"], 2)
                                if abs(inv["qty"]) < 0.005:
                                    self.inventory.pop(slug, None)
                                    self.inv_since.pop(slug, None)
                                self._journal_fill(FamilyOrder(
                                    id=r2.order_id or f"dump{int(now)}",
                                    market=slug, side="SELL",
                                    price=bid_t, qty=dq,
                                    intent=SELL_LONG, placed_ts=now,
                                    purpose="sell",
                                    why="taker dump — sold into the "
                                        "bid (the carved exception)"),
                                    dq, now, left)
                                self._note_wind_down(
                                    slug, "drain" if (dead_gate and
                                                      not profit_gate)
                                    else "dump", dq, bid_t, now,
                                    left=left)
                                self._log(event="dump", market=slug,
                                          price=bid_t, qty=dq,
                                          note=("dead-stock drain — "
                                                "exits measured ~$0, "
                                                "sold into the bid"
                                                if dead_gate and
                                                not profit_gate else
                                                "sold into the bid — "
                                                "tight spread, above "
                                                "basis"))
                                self._mark(slug, "SELL", now)
                                actions -= 1
                                continue
                # the stray checks below run even when the stock is
                # fully covered (2026-09-04) — see the short branch
                break_even = min(max(inv.get("cost", 0.0) / qty, 0.001), 0.989)
                floor_px, score_basis = self._exit_floor(
                    slug, "SELL", break_even, book.tick, book=book, qty=qty)
                gate_px = self._exit_gate(slug, "SELL", break_even, rest,
                                          book, now)
                ask_touch = self._ask_anchor(slug, book, break_even)
                lo = max(floor_px,
                         (book.bids[0][0] + book.tick) if book.bids
                         else 0.002)
                if gate_px is not None:
                    lo = min(lo, gate_px)
                # an exit resting BELOW today's floor — yesterday's dry
                # pricing, a ghost-book quote — retreats: cancelled here,
                # re-rested at a defensible price next pass (cancel-first
                # never over-offers)
                low_stray = [o for o in mine if o.price < lo - 1e-9
                             and o.id in self.orders]
                if low_stray and rest >= 0.01:    # as before: judged when
                                                   # cover is being added
                    worst = min(low_stray, key=lambda o: o.price)
                    rr = self.desk.cancel(worst.id, worst.market)
                    if rr.ok:
                        self.orders.pop(worst.id, None)
                        self.evidence.order_gone(worst.market, worst.id)
                        self._log(event="exit_retreated", market=slug,
                                  price=worst.price, qty=worst.qty,
                                  note="below break-even without the "
                                       "gate's blessing — pulled back")
                        actions -= 1
                    continue
                bound = max(ask_touch, floor_px) + 2 * book.tick
                stray = [o for o in mine if o.price > bound + 1e-9
                         and o.id in self.orders]
                if stray:
                    worst = max(stray, key=lambda o: o.price)
                    rr = self.desk.cancel(worst.id, worst.market)
                    if rr.ok:
                        self.orders.pop(worst.id, None)
                        self.evidence.order_gone(worst.market, worst.id)
                        self._log(event="stranded_exit_repriced",
                                  market=slug, price=worst.price,
                                  qty=worst.qty,
                                  note="past the touch and the allowed "
                                       "bound — re-resting where it can "
                                       "fill (owner, 2026-08-22)")
                        actions -= 1
                    continue
                if rest < 0.01:
                    continue
                if covered > 0.01 and not self._cooldown_ok(slug, "SELL",
                                                            now):
                    continue    # adjustments throttle; a bare position
                                # gets its exit NOW (owner, 2026-08-22:
                                # "no reason to wait")
                # sell at the FRONT of the profitable range (owner,
                # 2026-08-22): join the ask touch — unless the touch is
                # a giveaway against the model, then rest just under fair
                fair_g = self.fairs(slug) if self.fairs is not None else None
                join_px = ask_touch
                if fair_g is not None and join_px < fair_g - 3 * book.tick:
                    join_px = fair_g - book.tick
                px = max(lo, min(join_px, 0.999))
                if gate_px is not None:
                    px = gate_px          # the gate's price IS the plan:
                                          # it was chosen as the least
                                          # give-up that pays for itself
                planned = self._replace_at.pop(f"{slug}|SELL", None)
                if (planned is not None
                        and now - planned[1] <= REPLACE_PLAN_TTL_S
                        and abs(planned[2] - rest) < 0.01):
                    px = planned[0]       # what the mover promised, for
                                          # this same size
                px = min(max(px, 0.002), 0.999)
                side, intent, rest_qty = "SELL", SELL_LONG, rest
                why = "selling filled stock — it earns while it waits"
            else:
                # a SHORT: buy it back at the bid touch, never above
                # break-even — the bid earns rewards while it exits and
                # adds no collateral (owner, 2026-08-20: "try and exit
                # positions in a way that earns liquidity reward")
                mine = [o for o in list(self.orders.values())
                        if o.market == slug and o.purpose == "sell"
                        and o.side == "BUY"]
                self._maybe_move_exit(slug, "BUY", mine, book, inv, now)
                covered = sum(
                    o.qty for o in list(self.orders.values())
                    if o.market == slug and o.side == "BUY"
                    and o.purpose in ("sell", "manual", "bond"))
                rest = -qty - covered
                if covered > -qty + 0.01:
                    self._prune_excess_exits(slug, "BUY", covered + qty, now)
                # the dead-short step-up (owner, 2026-08-29 "we should
                # find a way to get the resting positions down", then
                # "the step ups can start immediately on things that
                # aren't currently earning"): a buy-back pinned at
                # break-even on a book trading well above it never
                # fills, and the short's collateral sits frozen. As
                # soon as its exits MEASURE ~$0 — no waiting period;
                # repricing resets order age, so a dwell keyed to it
                # could starve forever — the buy-back may bid UP TO
                # THE TOUCH: never more than 5 ticks above what the
                # short sold for, never over fair + 3 ticks, still
                # post-only. An unmeasured exit (live_est None) is
                # not dead — a zero READING is required, not absence
                # of one.
                dead_s = (self.cfg.dead_drain_s > 0 and bool(mine)
                          and all(o.live_est is not None
                                  and o.live_est < 0.005 for o in mine))
                # the step-up's TARGET, computed BEFORE any cancel
                # (owner, 2026-08-30, after the audit caught buy-backs
                # cancelled and re-placed at the same price every 60s
                # for 18 hours): a buy-back already resting at the best
                # price the rules allow gains nothing from a cancel —
                # it only loses its place in the queue. No target
                # improvement, no touch.
                step_tgt = None
                if dead_s:
                    received_t = min(max(-inv.get("cost", 0.0) / -qty,
                                         0.002), 0.999)
                    step_tgt = min(received_t + 5 * book.tick,
                                   book.bids[0][0] if book.bids
                                   else received_t - book.tick)
                    fair_t = (self.fairs(slug)
                              if self.fairs is not None else None)
                    if fair_t is not None:
                        step_tgt = min(step_tgt, fair_t + 3 * book.tick)
                    if book.asks:
                        step_tgt = min(step_tgt,
                                       book.asks[0][0] - book.tick)
                worst = (min(mine, key=lambda o: o.price)
                         if mine else None)
                step_pred = None
                if dead_s and step_tgt is not None and worst is not None:
                    # what will the re-rest ACTUALLY price at? The exit
                    # gate has the last word (owner, 2026-08-30, the
                    # nh-dem loop); with no gate the slot optimizer
                    # does — scored on the book WITHOUT this order,
                    # which is how the re-rest must see it too
                    # (2026-09-04: comparing against the raw step
                    # target re-placed one buy-back at 42c every minute
                    # for three hours). The prediction is handed to the
                    # re-rest so the pair lands where it says.
                    received_w = min(max(-inv.get("cost", 0.0) / -qty,
                                         0.002), 0.999)
                    cap_w, basis_w = self._exit_floor(
                        slug, "BUY", received_w, book.tick, book=book,
                        qty=-qty)
                    gate_p = self._exit_gate(slug, "BUY", received_w,
                                             worst.qty, book, now)
                    if gate_p is not None:
                        step_pred = gate_p
                    else:
                        gone_w = (worst.price, worst.qty)
                        lv_w = self._levels_less(book.side("BUY"), gone_w)
                        bid_w = (lv_w[0][0] if lv_w
                                 else received_w - book.tick)
                        hi_w = min(cap_w,
                                   (book.asks[0][0] - book.tick)
                                   if book.asks else cap_w)
                        hi_w = max(hi_w, step_tgt)
                        step_pred = self._best_exit_px(
                            slug, "BUY", book, min(bid_w, hi_w), hi_w,
                            worst.qty, basis=basis_w, exclude=gone_w)
                    step_pred = snap_price(step_pred, book.tick, "BUY")
                if (dead_s and rest < 0.01 and actions > 0
                        and step_pred is not None
                        and step_pred > worst.price + book.tick / 2):
                    rr = self.desk.cancel(worst.id, worst.market)
                    if rr.ok:
                        self.orders.pop(worst.id, None)
                        self.evidence.order_gone(worst.market, worst.id)
                        self._replace_at[f"{slug}|BUY"] = (step_pred, now,
                                                           worst.qty)
                        # what the step buys, scored the same way the
                        # exit mover scores its own moves: the model's
                        # estimate at the new price less what it was
                        # earning where it sat (owner, 2026-08-31)
                        gain_s = None
                        prog_s, _ws = self._prog_row(slug)
                        pool_s = (self._side_pool(slug, prog_s)
                                  if prog_s is not None else None)
                        if prog_s is not None and pool_s is not None:
                            lv_s = [(p, q) for p, q in book.side("BUY")
                                    if q > 1e-9]
                            j_s = estimate_join(
                                "BUY", lv_s, book.tick, float(prog_s.df),
                                float(prog_s.target), step_pred, worst.qty)
                            gain_s = ((j_s.share * pool_s
                                       if j_s.qualifies and j_s.in_window
                                       else 0.0)
                                      - (worst.live_est or 0.0))
                        # the short is untouched by a repricing — pass
                        # what is really still open rather than letting
                        # left default to zero and read as "flat"
                        self._note_wind_down(slug, "short step-up",
                                             worst.qty, step_pred, now,
                                             left=-qty,
                                             from_px=worst.price,
                                             gain=gain_s)
                        self._log(event="dead_short_stepup", market=slug,
                                  price=worst.price, qty=worst.qty,
                                  note="buy-back never fills at "
                                       "break-even — stepping up "
                                       "toward the touch to free the "
                                       "collateral")
                        covered -= worst.qty
                        rest = -qty - covered
                        actions -= 1
                # the stray checks below run even when the short is
                # fully covered — a buy-back stranded far under the
                # touch is wasted whether or not more cover is due
                # (2026-09-04: covers placed at a 10:29Z restart sat 14
                # ticks under the bid for twelve hours, untouched,
                # because "nothing left to cover" skipped them)
                received = min(max(-inv.get("cost", 0.0) / -qty, 0.002), 0.999)
                cap_px, score_basis = self._exit_floor(
                    slug, "BUY", received, book.tick, book=book, qty=-qty)
                gate_px = self._exit_gate(slug, "BUY", received, rest,
                                          book, now)
                bid_touch = (book.bids[0][0] if book.bids
                             else received - book.tick)
                hi = min(cap_px,
                         (book.asks[0][0] - book.tick) if book.asks
                         else cap_px)
                if gate_px is not None:
                    hi = max(hi, gate_px)
                if dead_s and step_tgt is not None:
                    hi = max(hi, step_tgt)
                # a cover bidding ABOVE today's cap (the 98c-on-a-2c-
                # basis ghosts) retreats the same way
                high_stray = [o for o in mine if o.price > hi + 1e-9
                              and o.id in self.orders]
                if high_stray and rest >= 0.01:   # as before: judged when
                                                   # cover is being added
                                                   # (the gate's blessing
                                                   # is sized to `rest`)
                    worst = max(high_stray, key=lambda o: o.price)
                    rr = self.desk.cancel(worst.id, worst.market)
                    if rr.ok:
                        self.orders.pop(worst.id, None)
                        self.evidence.order_gone(worst.market, worst.id)
                        self._log(event="exit_retreated", market=slug,
                                  price=worst.price, qty=worst.qty,
                                  note="above break-even without the "
                                       "gate's blessing — pulled back")
                        actions -= 1
                    continue
                bound = min(bid_touch, cap_px) - 2 * book.tick
                stray = [o for o in mine if o.price < bound - 1e-9
                         and o.id in self.orders]
                if stray:
                    worst = min(stray, key=lambda o: o.price)
                    rr = self.desk.cancel(worst.id, worst.market)
                    if rr.ok:
                        self.orders.pop(worst.id, None)
                        self.evidence.order_gone(worst.market, worst.id)
                        self._log(event="stranded_exit_repriced",
                                  market=slug, price=worst.price,
                                  qty=worst.qty,
                                  note="past the touch and the allowed "
                                       "bound — re-resting where it can "
                                       "fill (owner, 2026-08-22)")
                        actions -= 1
                    continue
                if rest < 0.01:
                    continue
                if covered > 0.01 and not self._cooldown_ok(slug, "BUY",
                                                            now):
                    continue    # same rule for covers: bare shorts get
                                # their buy-back immediately
                px = self._best_exit_px(slug, "BUY", book,
                                        min(bid_touch, hi), hi, rest,
                                        basis=score_basis)
                if gate_px is not None:
                    px = gate_px
                planned = self._replace_at.pop(f"{slug}|BUY", None)
                if (planned is not None
                        and now - planned[1] <= REPLACE_PLAN_TTL_S
                        and abs(planned[2] - rest) < 0.01):
                    px = planned[0]       # what the mover or the step-up
                                          # promised this pass, for this
                                          # same size (a different size
                                          # is a different gate answer)
                px = min(max(px, 0.001), 0.999)
                side, intent, rest_qty = "BUY", SELL_SHORT, rest
                why = ("buying back the short at or under what it sold "
                       "for — the bid earns while it waits")
            r = self.desk.place_resting(slug, side, px, rest_qty,
                                        net_position=qty, intent=intent)
            if r.ok and r.order_id:
                self.orders[r.order_id] = FamilyOrder(
                    id=r.order_id, market=slug, side=side,
                    price=(r.price or px),
                    qty=rest_qty, intent=intent, placed_ts=now,
                    purpose="sell", why=why)
                self._log(event="sell_rested", market=slug,
                          price=(r.price or px),
                          qty=rest_qty, side=side)
                self._mark(slug, side, now)
                actions -= 1
            else:
                # a position whose exit will not rest is the loudest
                # kind of problem — it used to fail in silence (owner,
                # 2026-08-26)
                self._log(event="exit_place_failed", market=slug,
                          side=side, price=px, qty=rest_qty,
                          note=r.note[:110])
        # an "exit" with no position behind it is not an exit — a fill
        # would OPEN a position, not close one (the petbut shape; owner
        # approved 2026-08-21). Reduce-checking also catches covers left
        # on the wrong side after a phantom position was corrected.
        for rec in list(self.orders.values()):
            if actions <= 0:
                break
            if rec.purpose != "sell":
                continue
            if now - rec.placed_ts < 300.0:
                continue      # a fresh exit gets its feed cycle first
            pos = (self.inventory.get(rec.market) or {}).get("qty", 0.0)
            reduces = ((rec.side == "BUY" and pos < -0.005)
                       or (rec.side == "SELL" and pos > 0.005))
            if reduces:
                continue
            r = self.desk.cancel(rec.id, rec.market)
            if r.ok:
                self.orders.pop(rec.id, None)
                self.evidence.order_gone(rec.market, rec.id)
                self._log(event="orphan_exit_cancelled", market=rec.market,
                          price=rec.price, qty=rec.qty,
                          note="no position behind it — a fill would open "
                               "a new position, not close one")
                actions -= 1
        return actions

    # --------------------------------------------------------------- books

    def _probe(self, now: float, positions: dict, actions: int) -> int:
        if self.cfg.probe_usd <= 0 or actions <= 0:
            return actions
        spent = sum(capital_at_risk(o.intent, o.price, o.qty)
                    for o in list(self.orders.values()) if o.purpose == "probe")
        placed = 0
        for slug, sb in sorted(self.scoreboard.items(),
                               key=lambda kv: -(kv[1].get("pool_day") or 0.0)):
            if actions <= 0 or placed >= self.cfg.probes_per_cycle:
                break
            if spent >= self.cfg.probe_usd - 1e-9:
                break
            if not self.enterable(slug) or self._dead_here(slug):
                continue
            prog, _w = self._prog_row(slug)
            if prog is None:
                continue
            side_pool = self._side_pool(slug, prog)
            if side_pool is None or side_pool < self.cfg.min_est_day:
                continue      # even owning the side couldn't pay the bar
            if sb.get("plans"):
                continue      # the planner can already act — no probe needed
            band = self.evidence.band(
                slug, prior_fair=self.fairs(slug) if self.fairs else None)
            if self.evidence.confidence(slug, band) >= self.cfg.probe_conf:
                continue      # we already know enough here
            if now - self.last_action.get(f"{slug}|probe", 0.0) \
                    < self.cfg.probe_cooldown_s:
                continue
            if any(o.market == slug and o.purpose == "probe"
                   for o in list(self.orders.values())):
                continue
            book = self.cache.fresh(slug, BOOK_MAX_AGE, now)
            if book is None or not book.bids:
                continue
            # aim the scout at the least-observed fill-odds bucket
            # (owner, 2026-08-19: "we won't get a full picture of the odds
            # if we just stick on the safe side")
            from .fillmodel import DIST_BUCKETS, family_of
            fam_k = family_of(slug)
            def bucket_hours(b):
                cell = self.fillmodel.obs.get(
                    self.fillmodel._key(fam_k, "BUY", b))
                return (cell or [0.0])[0]
            k_probe = min(DIST_BUCKETS, key=bucket_hours)
            px = round(book.bids[0][0] - k_probe * book.tick, 3)
            _lo, hi = self._price_bounds(slug, book.bids, book.asks, book.tick)
            fair_pr = self.fairs(slug) if self.fairs is not None else None
            if fair_pr is not None:
                # scouts obey the hard cap too (owner, 2026-08-23)
                cap_pr = fair_pr - book.tick
                hi = cap_pr if hi is None else min(hi, cap_pr)
            if hi is not None:
                px = min(px, round(hi, 3))
            if not (0.001 <= px <= 0.6):
                continue      # 1.0's rule: probes buy cheap or not at all
            r = self.desk.place_resting(slug, "BUY", px, self.cfg.probe_qty,
                                        net_position=(positions.get(slug)
                                                      or (0.0,))[0],
                                        verify=self.cfg.verify_resting)
            if r.ok and r.order_id:
                self.orders[r.order_id] = FamilyOrder(
                    id=r.order_id, market=slug, side="BUY",
                    price=(r.price or px),
                    qty=self.cfg.probe_qty, intent=r.intent, placed_ts=now,
                    purpose="probe",
                    why=("a scout — this market's pool could pay, but I "
                         "don't know enough yet; what happens to this "
                         "share IS the information"))
                self._log(event="probe", market=slug, price=px)
                self.last_action[f"{slug}|probe"] = now
                spent += capital_at_risk(r.intent, px, self.cfg.probe_qty)
                placed += 1
                actions -= 1
        # rotation: a scout that sat its full watch has reported in
        for rec in list(self.orders.values()):
            if rec.purpose != "probe":
                continue
            if now - rec.placed_ts >= self.cfg.probe_ttl_s:
                r = self.desk.cancel(rec.id, rec.market)
                if r.ok:
                    self.evidence.rest_mark(rec.market, rec.id, rec.side,
                                            rec.price, rec.placed_ts, now=now)
                    self.evidence.order_gone(rec.market, rec.id, now=now)
                    self._log(event="probe_done", market=rec.market,
                              why="sat its watch untouched — noted")
                    del self.orders[rec.id]
        return actions

    def _refresh_books(self, client, now: float) -> int:
        """Active markets by staleness first; the candidate scan keeps its
        reserved slice so discovery can never starve (the 2026-08-20 CFB
        lesson)."""
        budget = self.cfg.books_per_cycle
        scan_reserve = min(self.cfg.scan_reserve, budget)
        done = 0
        if self.cfg.watch_tokens:
            # the owner's watched races: fresh every cycle, off-budget
            for slug in sorted(s2 for s2 in self.universe
                               if self._watched(s2)):
                if self.cache.age(slug, now) > 60.0:
                    try:
                        self.cache.put(slug,
                                       client.book(slug, fetched_at=now))
                    except Exception as e:  # noqa: BLE001
                        self._log(event="book_error", market=slug,
                                  error=str(e)[:60])
        active = sorted(self.active_markets() | set(self.inventory),
                        key=lambda s: self.cache.age(s, now), reverse=True)
        for slug in active:
            if done >= budget - scan_reserve:
                break
            if self.cache.age(slug, now) > self.cfg.book_stale_s:
                try:
                    self.cache.put(slug, client.book(slug, fetched_at=now))
                except Exception as e:  # noqa: BLE001
                    self._log(event="book_error", market=slug, error=str(e)[:60])
                done += 1
        idle = [s for s in self.universe if s not in self.active_markets()
                and self.enterable(s)
                and now - (self.scoreboard.get(s) or {}).get("ts", 0.0)
                > self.cfg.rescan_s]

        def triage(s: str) -> float:
            """Rapid triage (owner, 2026-08-21): mispriced markets and big
            spreads first, using whatever is already known — the model's
            distance from the last seen touch, the spread's width, the
            payout record. A market never seen at all scores a flat
            curiosity bonus so first looks keep happening."""
            score = min(self.history.get(s, 0.0), 5.0)
            b2 = self.cache.any_age(s)
            if b2 is None:
                return score + 1.0
            bb2 = b2.bids[0][0] if b2.bids else None
            ba2 = b2.asks[0][0] if b2.asks else None
            if bb2 is not None and ba2 is not None:
                score += min((ba2 - bb2) * 100.0 / 2.0, 10.0)   # spread, cents
                fair2 = self.fairs(s) if self.fairs is not None else None
                if fair2 is not None:
                    mid2 = (bb2 + ba2) / 2.0
                    score += min(abs(mid2 - fair2) * 100.0, 20.0)  # mispricing
            return score

        idle.sort(key=lambda s: (-triage(s),
                                 (self.scoreboard.get(s) or {}).get("ts", 0.0)))
        for slug in idle:
            if done >= budget:
                break
            days = slug_days_out(slug, now)
            if days is not None and days < self.cfg.min_days_out:
                self.scoreboard[slug] = {"ts": now, "plans": [],
                                         "why": "resolves soon — not worth entering"}
                continue
            if self._dead_here(slug):
                self.scoreboard[slug] = {"ts": now, "plans": [],
                                         "why": "program pays nothing"}
                continue
            prog, no_prog_why = self._prog_row(slug)
            if prog is None:
                # no terms yet: no book fetch spent; retry in ~15 minutes
                # rather than a full rescan interval, because the terms
                # rotor may confirm a pool for it within the hour
                self.scoreboard[slug] = {
                    "ts": now - self.cfg.rescan_s + 900.0, "plans": [],
                    "why": no_prog_why}
                continue
            try:
                book = client.book(slug, fetched_at=now)
                self.cache.put(slug, book)
            except Exception as e:  # noqa: BLE001
                self.scoreboard[slug] = {"ts": now, "plans": [],
                                         "why": f"book fetch failed: {str(e)[:50]}"}
                done += 1
                continue
            plans, why, grow, potential = self.plan_market(book, slug)
            prog2, _ = self._prog_row(slug)
            sp2 = self._side_pool(slug, prog2) if prog2 else None
            conf2 = self.evidence.confidence(slug)
            self.scoreboard[slug] = {
                "ts": now, "plans": plans, "why": why,
                "grow": grow, "potential": potential,
                "est": round(sum(p["est"] for p in plans), 4),
                "pool_day": round(sp2, 4) if sp2 is not None else None,
                "conf": conf2}
            # the sweep's verdict, for the live triage feed on the page
            spread_c = (round((book.asks[0][0] - book.bids[0][0]) * 100, 1)
                        if book.bids and book.asks else None)
            best_ev = (max(p.get("ev", p["est"]) for p in plans)
                       if plans else (potential if grow else 0.0))
            top = (max(plans, key=lambda p: p.get("ev", p["est"]))
                   if plans else (grow[0] if grow else None))
            self.triage_feed.append({
                "ts": round(now, 1), "market": slug,
                "in": bool(plans or grow),
                "ev": round(best_ev, 2), "spread": spread_c,
                "pool": round(sp2, 2) if sp2 is not None else None,
                "conf": round(conf2, 2),
                "plan": (f"{'bid' if top['side'] == 'BUY' else 'ask'} "
                         f"{top['qty']:g} @ {top['px'] * 100:.0f}c"
                         if top else None),
                "book": {"b": [[p2, round(q2, 1)] for p2, q2 in book.bids[:6]],
                         "a": [[p2, round(q2, 1)] for p2, q2 in book.asks[:6]]},
                "picks": [{"s": p["side"], "px": p["px"], "q": p["qty"],
                           "ev": round(p.get("ev", p["est"]), 2)}
                          for p in plans[:2]],
                "why": (plans[0]["why"][:60] if plans
                        else grow[0]["why"][:60] if grow
                        else (why or "")[:60])})
            del self.triage_feed[:-40]
            done += 1
        # the free pass: re-plan from cached books, no fetches spent
        if self.cfg.replan_s > 0:
            fresh_idle = [s for s in self.universe
                          if s not in self.active_markets()
                          and self.enterable(s)
                          and not self._dead_here(s)
                          and now - (self.scoreboard.get(s) or {}).get("ts", 0.0)
                          > self.cfg.replan_s
                          and self.cache.fresh(s, BOOK_MAX_AGE, now) is not None]
            fresh_idle.sort(key=lambda s: (self.scoreboard.get(s) or {}).get("ts", 0.0))
            for slug in fresh_idle[:self.cfg.replans_per_cycle]:
                prog3, _w3 = self._prog_row(slug)
                if prog3 is None:
                    continue
                book3 = self.cache.fresh(slug, BOOK_MAX_AGE, now)
                plans, why, grow, potential = self.plan_market(book3, slug)
                sp3 = self._side_pool(slug, prog3)
                conf3 = self.evidence.confidence(slug)
                self.scoreboard[slug] = {
                    "ts": now, "plans": plans, "why": why,
                    "grow": grow, "potential": potential,
                    "est": round(sum(p["est"] for p in plans), 4),
                    "pool_day": round(sp3, 4) if sp3 is not None else None,
                    "conf": conf3}
                spread3 = (round((book3.asks[0][0] - book3.bids[0][0]) * 100, 1)
                           if book3.bids and book3.asks else None)
                best3 = (max(p.get("ev", p["est"]) for p in plans)
                         if plans else (potential if grow else 0.0))
                self.triage_feed.append({
                    "ts": round(now, 1), "market": slug,
                    "in": bool(plans or grow),
                    "ev": round(best3, 2), "spread": spread3,
                    "pool": round(sp3, 2) if sp3 is not None else None,
                    "conf": round(conf3, 2),
                    "book": {"b": [[p2, round(q2, 1)]
                                   for p2, q2 in book3.bids[:6]],
                             "a": [[p2, round(q2, 1)]
                                   for p2, q2 in book3.asks[:6]]},
                    "picks": [{"s": p["side"], "px": p["px"], "q": p["qty"],
                               "ev": round(p.get("ev", p["est"]), 2)}
                              for p in plans[:2]],
                    "why": (plans[0]["why"][:60] if plans
                            else grow[0]["why"][:60] if grow
                            else (why or "")[:60])})
                del self.triage_feed[:-40]
        for gone in set(self.scoreboard) - set(self.universe):
            del self.scoreboard[gone]
        return done

    # ------------------------------------------------------------- estimate

    def _accrue(self, now: float) -> None:
        """ONE earned-today number: the live rate integrated over time,
        accruing only while enough of our order books are fresh (no
        quorum, no accrual — a blind stretch adds nothing rather than a
        guess; owner: "If you miss a few seconds that is fine")."""
        day = _et_day(now)
        if self.earned_day and day != self.earned_day:
            self.dump_today = 0.0
            self.earned_history.append([self.earned_day,
                                        round(self.earned_today, 2)])
            del self.earned_history[:-14]
            self.earned_today = 0.0
        self.earned_day = day
        dt_s = now - self._last_accrual if self._last_accrual else 0.0
        self._last_accrual = now
        if not (0.0 < dt_s <= 600.0):
            return
        mkts = {o.market for o in list(self.orders.values())
                if o.live_est is not None}
        if not mkts:
            return
        if self.cache.coverage(mkts, self.cfg.read_age_s, now) < 0.6:
            return
        rate = sum(o.live_est or 0.0 for o in list(self.orders.values()))
        self.earned_today += rate * dt_s / 86400.0

    # --------------------------------------------------------------- finish

    def _finish(self, summary: dict, now: float) -> dict:
        summary["orders"] = [vars(o) for o in list(self.orders.values())]
        ests = [o.live_est if o.live_est is not None else o.est_day
                for o in list(self.orders.values()) if o.purpose != "sell"]
        summary["est_day"] = round(sum(ests), 2)
        summary["stock_day"] = round(sum(o.live_est or 0.0
                                         for o in list(self.orders.values())
                                         if o.purpose == "sell"), 2)
        summary["spent"] = round(self.family_spent(), 2)
        if self.cfg.proven_usd > 0:
            summary["proven_spent"] = round(self.proven_spent(), 2)
            summary["proven_usd"] = self.cfg.proven_usd
            summary["proven_n"] = len(self.proven)
        summary["holdings_usd"] = round(self.holdings_value(), 2)
        summary["holdings_counted"] = bool(self.cfg.holdings_in_ceiling)
        summary["capital_usd"] = self.cfg.capital_usd
        summary["earned_today"] = round(self.earned_today, 2)
        summary["inventory"] = {k: dict(v) for k, v in list(self.inventory.items())}
        # every held position by what it earns per dollar of
        # liquidation value (owner, 2026-08-26: "even the ones with no
        # earnings and no orders. The lowest should be at the top so I
        # can hand place those") — liquidation valued like the budget
        # does (longs at the bid, shorts at what closing recovers, no
        # book = conservatively at cost), earnings = the reduce-side
        # orders resting there, engine and hand alike
        positions = []
        for slug, inv in list(self.inventory.items()):
            qty = inv.get("qty") or 0.0
            if abs(qty) < 0.005:
                continue
            book = self.cache.any_age(slug)
            if qty > 0:
                liq = (qty * book.bids[0][0]
                       if book is not None and book.bids
                       else max(inv.get("cost", 0.0), 0.0))
                cover_side = "SELL"
            else:
                liq = (-qty * (1.0 - book.asks[0][0])
                       if book is not None and book.asks
                       else max(-inv.get("cost", 0.0), 0.0))
                cover_side = "BUY"
            covers = [o for o in list(self.orders.values())
                      if o.market == slug and o.side == cover_side]
            earn = sum(o.live_est or 0.0 for o in covers)
            positions.append({
                "market": slug, "qty": round(qty, 2),
                "cost": round(inv.get("cost", 0.0), 2),
                "liq": round(liq, 2), "earn": round(earn, 4),
                "per_dollar": (round(earn / liq, 4)
                               if liq > 0.01 else 0.0),
                "covers": [{"side": o.side, "price": o.price,
                            "qty": o.qty, "purpose": o.purpose}
                           for o in covers]})
        summary["positions"] = positions
        # the wind-down report: what the engine has actually retired,
        # so "where are we on selling off positions" has a number
        # instead of an inference from position counts
        day = now - 86400.0
        week = now - 7 * 86400.0
        recent = [w for w in self.wind_down if w["ts"] > week]
        today_w = [w for w in recent if w["ts"] > day]
        by_kind: dict = {}
        for w in today_w:
            k = by_kind.setdefault(w["kind"], {"n": 0, "usd": 0.0})
            k["n"] += 1
            k["usd"] += w["usd"]
        # sold is SOLD: proceeds count stock that went out the door.
        # The repricings are reported beside them, never inside them —
        # their dollars are what closing a short would COST.
        sold_wk = [w for w in recent if self._wd_sale(w)]
        sold_day = [w for w in today_w if self._wd_sale(w)]
        moved_day = [w for w in today_w if not self._wd_sale(w)]
        moved_4h = [w for w in moved_day if w["ts"] > now - 4 * 3600.0]
        summary["wind_down"] = {
            "day_n": len(sold_day),
            "day_usd": round(sum(w["usd"] for w in sold_day), 2),
            "week_n": len(sold_wk),
            "week_usd": round(sum(w["usd"] for w in sold_wk), 2),
            "by_kind": {k: {"n": v["n"], "usd": round(v["usd"], 2)}
                        for k, v in by_kind.items()},
            "flat_day": sum(1 for w in sold_day if w["flat"]),
            "moves_n": len(moved_day),
            "moves_usd": round(sum(w["usd"] for w in moved_day), 2),
            # the repricings collapse to one line: how many markets
            # moved a price in the last four hours, and what the model
            # says that added per day (owner, 2026-08-31)
            "moves_4h_markets": len({w["market"] for w in moved_4h}),
            "moves_4h_n": len(moved_4h),
            "moves_4h_gain": round(sum(w.get("gain") or 0.0
                                       for w in moved_4h), 4),
            "recent": [{"market": w["market"], "kind": w["kind"],
                        "qty": w["qty"], "px": w["px"],
                        "usd": round(w["usd"], 2), "ts": w["ts"],
                        "sale": self._wd_sale(w),
                        "from_px": w.get("from_px"), "gain": w.get("gain"),
                        "flat": w["flat"] and self._wd_sale(w)}
                       for w in today_w[-12:]],
        }
        # the owner's watched races: the ask side's standing against
        # Target Size (owner, 2026-08-28: "Just give me a button to
        # auto qualify the ask side" — the button needs the gap in
        # front of it). Book totals include every resting order, his
        # and anyone's; the fast lane keeps these books fresh.
        if self.cfg.watch_tokens:
            watched = []
            for slug in sorted(s for s in self.universe
                               if self._watched(s)):
                prog = self.terms.get(slug)
                book = self.cache.any_age(slug)
                ask_total = (round(sum(q for _, q in book.asks), 1)
                             if book is not None else None)
                target = prog.target if prog is not None else None
                # the button builds PAST the line, so the page shows the
                # same goal it is working to (owner, 2026-09-01)
                from .survey import QUALIFY_TARGET_MULT
                goal = target * QUALIFY_TARGET_MULT if target else None
                watched.append({
                    "market": slug, "ask_total": ask_total,
                    "target": target, "goal": goal,
                    "qualifies": (bool(ask_total >= target)
                                  if ask_total is not None and target
                                  else None),
                    "has_room": (bool(ask_total >= goal)
                                 if ask_total is not None and goal
                                 else None)})
            summary["watched"] = watched
        summary["scanned"] = sum(1 for sb in self.scoreboard.values()
                                 if "plans" in sb)
        # the triage sweep's progress: how much of the eligible board —
        # in scope, carrying a live program — has a current score
        elig = [s for s in self.universe
                if self.enterable(s) and s in self.terms.current
                and not self._dead_here(s)]
        done = sum(1 for s in elig
                   if now - (self.scoreboard.get(s) or {}).get("ts", 0.0)
                   <= self.cfg.rescan_s)
        summary["triage"] = {"total": len(elig), "done": done,
                             "per_cycle": max(self.cfg.scan_reserve, 1)}
        # how much of the scored board is actually worth funding (owner,
        # 2026-08-31: "give an overview for each cycle what percentage
        # are coming in as being worth the budget"). Scored = markets
        # carrying a verdict; worth = markets whose best plan clears the
        # entry bar. The rotating cards said the same thing one market
        # at a time; this says it in one number.
        scored = [sb for sb in self.scoreboard.values() if "plans" in sb]
        worth = [sb for sb in scored
                 if any(p.get("ev", p.get("est", 0.0)) >= self.cfg.min_est_day
                        for p in (sb.get("plans") or []))]
        cyc = [t for t in self.triage_feed[-self.cfg.scan_reserve:]]
        cyc_worth = sum(1 for t in cyc if t.get("picks"))
        summary["worth"] = {
            "scored": len(scored), "n": len(worth),
            "pct": round(100.0 * len(worth) / len(scored), 1) if scored else 0.0,
            "cycle_n": len(cyc), "cycle_worth": cyc_worth,
            "cycle_pct": (round(100.0 * cyc_worth / len(cyc), 1)
                          if cyc else 0.0)}
        summary["triage_feed"] = self.triage_feed[-16:]
        top = sorted(((s, sb) for s, sb in self.scoreboard.items()
                      if sb.get("plans")),
                     key=lambda kv: -(kv[1].get("est") or 0.0))[:12]
        summary["best_idle"] = [
            {"market": s, "name": self._label(s), "est": sb.get("est"),
             "hist": self.history.get(s), "conf": sb.get("conf"),
             "plans": sb["plans"]} for s, sb in top]
        return summary

    # ------------------------------------------------------------ persistence

    def _cfg_sig(self) -> str:
        c = self.cfg
        return "|".join(str(x) for x in (
            c.per_market_usd, c.min_est_day, c.share_hi, c.rest_style,
            c.allow_improve, c.revive, c.revive_max_usd, c.vol_quiet,
            c.est_deflate, PLAN_RULES_REV))

    def to_dict(self) -> dict:
        return {
            "cfg_sig": self._cfg_sig(),
            "orders": {oid: vars(o) for oid, o in list(self.orders.items())},
            "probe_ratchet": self.probe_ratchet,
            "inventory": self.inventory,
            "positions_seen": self.positions_seen,
            "silent_cancels": self.silent_cancels,
            "placed_at": self.placed_at,
            "active_until": self.active_until,
            "pos_moves": self.pos_moves[-500:],
            "pending_pages": self.pending_pages,
            "gone_pending": {oid: {"rec": asdict(g["rec"]),
                                   "until": g["until"]}
                             for oid, g in self.gone_pending.items()},
            "last_action": self.last_action,
            "known_dead": sorted(self.known_dead),
            "wind_down": self.wind_down[-400:],
            "seen_pids": sorted(self.seen_pids),
            "inv_since": self.inv_since,
            "fillmodel": self.fillmodel.to_dict(),
            "exp_fills": self.exp_fills,
            "pending_marks": self.pending_marks[-60:],
            # 600 to match the in-memory retention trim: saving
            # only 200 silently discarded most of the journal on
            # every save, and threw away 300 of the 493 rows the
            # 2026-08-23 recovery had just rebuilt. data/fills.csv
            # and data/trades.csv remain the unbounded archives.
            "fills": self.fills[-600:],
            "scoreboard": self.scoreboard,
            "universe": self.universe,
            "terms": self.terms.to_dict(),
            "earned_today": round(self.earned_today, 4),
            "earned_day": self.earned_day,
            "dump_today": round(self.dump_today, 2),
            "earned_history": self.earned_history,
            "log": self.log[-self.cfg.log_keep:],
        }

    def restore(self, d: dict) -> None:
        for oid, v in (d.get("orders") or {}).items():
            rec = FamilyOrder(**{k: x for k, x in v.items()
                                 if k in FamilyOrder.__dataclass_fields__})
            if rec.why == "adopted from the earlier versions":
                # one-time migration (owner, 2026-08-22 "Don't let it
                # cancel orders I set by hand", then "Still getting
                # orders cancelled"): everything claimed by the old
                # adoption after the 1.0/2.0 retirement was the owner's
                # hand — relabel it untouchable.
                rec.purpose = "manual"
                rec.why = "the owner's own order — the engine leaves it alone"
            self.orders[oid] = rec
        self.inventory = dict(d.get("inventory") or {})
        self.probe_ratchet = {k: list(v) for k, v in
                              (d.get("probe_ratchet") or {}).items()}
        self.positions_seen = dict(d.get("positions_seen") or {})
        self.silent_cancels = d.get("silent_cancels") or 0
        self.placed_at = {k: float(v) for k, v in
                          (d.get("placed_at") or {}).items()}
        self.pending_pages = list(d.get("pending_pages") or [])
        self.gone_pending = {}
        for oid, g in (d.get("gone_pending") or {}).items():
            try:
                self.gone_pending[oid] = {
                    "rec": FamilyOrder(**{k: x for k, x in g["rec"].items()
                                          if k in
                                          FamilyOrder.__dataclass_fields__}),
                    "until": float(g["until"])}
            except (KeyError, TypeError, ValueError):
                continue
        self.last_action = dict(d.get("last_action") or {})
        self.known_dead = set(d.get("known_dead") or ())
        self.wind_down = list(d.get("wind_down") or ())
        self.seen_pids = set(d.get("seen_pids") or ())
        self.inv_since = dict(d.get("inv_since") or {})
        if d.get("fillmodel"):
            self.fillmodel = FillModel.from_dict(d["fillmodel"])
        self.pending_marks = list(d.get("pending_marks") or [])
        self.exp_fills = {str(k): {p: float(v) for p, v in cell.items()}
                          for k, cell in (d.get("exp_fills") or {}).items()
                          if isinstance(cell, dict)}
        self.fills = list(d.get("fills") or [])
        self.dump_today = float(d.get("dump_today") or 0.0)
        self.active_until = float(d.get("active_until") or 0.0)
        if d.get("cfg_sig") == self._cfg_sig():
            self.scoreboard = dict(d.get("scoreboard") or {})
        else:
            # the plans were scored under different knobs — the 2026-08-20
            # 23:53Z lesson: stale $1-era crumbs placed under a $20 config.
            # Rescan everything under the config actually running.
            self.scoreboard = {}
        self.universe = dict(d.get("universe") or {})
        self.pos_moves = [list(x) for x in (d.get("pos_moves") or [])
                          if isinstance(x, (list, tuple)) and len(x) == 3]
        if d.get("terms"):
            self.terms = TermsStore.from_dict(d["terms"])
        self.earned_today = float(d.get("earned_today") or 0.0)
        self.earned_day = str(d.get("earned_day") or "")
        self.earned_history = list(d.get("earned_history") or [])
        self.log = list(d.get("log") or [])
