"""Fair values for the seat ladders from the Silver forecast.

The owner wants the Silver model involved because it updates with
polling. 1.0 used the per-race tables only for individual races; the
seat-count ladder was priced off the market's own books, which is
circular. This module derives model-implied rung values instead.

**Primary source (2026-08-19, owner-provided): Silver Bulletin's OWN
simulated seat distributions** — the forecast embed publishes, per
chamber, the seat histogram of its 40,000 simulations in three model
flavors (Classic / Deluxe / Lite), as public Google-Sheets CSVs that
update with each model run. Seats in that data are DEMOCRATIC seats;
GOP = 100 - D in the Senate and R = 435 - D in the House. Each rung's
fair is read straight off those histograms, and the honest uncertainty
band is the min/max across the three flavors — Silver's own model
disagreement, not a parameter we invented. This prices the House
ladder too, which no per-district reconstruction here ever could.

**Fallback and cross-check: the one-factor probit copula** over the
per-race table (races are NOT independent — owner's correction; a
polling error moves every race the same direction):

    every race keeps EXACTLY Silver's win probability as its marginal;
    a shared national swing S ~ N(0,1) moves them together, with
    latent correlation SWING_RHO between any two races:

        P(GOP wins race i | S) = PHI((PHI^-1(p_i) - sqrt(rho) S)
                                     / sqrt(1 - rho))

    P(GOP seats = K) = holdovers + the exact Poisson-binomial of those
    conditional probabilities, averaged over the swing (numerical
    quadrature, no simulation).

The copula is computed at rho 0.2 / 0.35 / 0.6 and carried as a range.
It serves two jobs now: the Senate fair when the official distributions
are missing, and a staleness hedge — the per-race table updates with
every poll while the simulation sheet updates only when Silver reruns
the model, so once the official run is older than OFFICIAL_STALE_S the
band widens to the envelope of both models rather than trusting a
dated histogram alone.
"""

from __future__ import annotations

import csv
import io
import math
import time
from pathlib import Path
from statistics import NormalDist

_N = NormalDist()

# Latent correlation between any two races via the shared swing. Its true
# value is genuinely uncertain, and pretending otherwise would just move
# the overconfidence from one place to another — so the model carries a
# RANGE. The ladder is computed at rho 0.2 and 0.6 (the neighborhood
# polling-error studies span) and each rung's fair is an interval: where
# it barely moves across that range the model is confident; where it
# swings the model says so and the engine scouts instead of sizing.
# (Checked against the market on 2026-08-19: its ladder sits between the
# two ends nearly everywhere — the range brackets reality.)
SWING_RHO_LOW = 0.2
SWING_RHO_MID = 0.35   # central curve for display and control estimates
SWING_RHO_HIGH = 0.6
_SWING_NODES = 41      # quadrature nodes over the swing (-4..4 sigma)

SENATE_URL = "https://static.dwcdn.net/data/kNspD.csv"
SENATE_FALLBACK = Path(__file__).resolve().parent.parent / "data" / "silver_senate_races.csv"
# The site's embed references several chart ids (the owner dug them out
# of the embed source, 2026-08-21, after the governor numbers froze for
# 3 days): the fetch tries each and keeps whichever table matches the
# governor races by its own states — proof, not guesswork.
GOV_CANDIDATE_IDS = ("3DsnL", "KXB1W", "N13WX")
GOV_URL = "https://static.dwcdn.net/data/N13WX.csv"
GOV_FALLBACK = Path(__file__).resolve().parent.parent / "data" / "silver_gov_races.csv"
TTL_S = 6 * 3600.0

# Silver Bulletin's own simulation output — the forecast embed's public
# Google-Sheets CSVs (owner supplied the embed source, 2026-08-19).
# Topline carries the run datetime and headline probabilities; dist is
# the seat histogram per model flavor and chamber, in DEMOCRATIC seats.
_SHEET = ("https://docs.google.com/spreadsheets/d/e/2PACX-1vT3jZ8iv6EQOVWKKqVsA0"
          "6BEUHMlgds2PXiCLT2aPzOI--yAZSdsvQ2H1qmxEBQCuW1pvsRZtSwvIZx/pub")
OFFICIAL_TOPLINE_URL = _SHEET + "?gid=0&single=true&output=csv"
OFFICIAL_DIST_URL = _SHEET + "?gid=27833269&single=true&output=csv"
OFFICIAL_TOPLINE_FALLBACK = Path(__file__).resolve().parent.parent / "data" / "silver_official_topline.csv"
OFFICIAL_DIST_FALLBACK = Path(__file__).resolve().parent.parent / "data" / "silver_official_dist.csv"
OFFICIAL_STALE_S = 5 * 86400.0   # older run than this -> hedge with the copula
FLAVORS = ("classic", "deluxe", "lite")
SENATE_TOTAL = 100
HOUSE_TOTAL = 435
SENATE_PREFIX = "scc-senate-gop-"
HOUSE_PREFIX = "scc-hrep-rep-"

# GOP seats NOT up in 2026 = 53 currently held minus the 22 GOP-held
# seats on the ballot (20 of Class II: AL AK AR IA ID KS KY LA ME MS MT
# NC NE OK SC SD TN TX WV WY, plus the OH and FL specials). The Silver
# table carries all 35 races (those 22 plus 13 Dem-held Class II). If
# this constant is wrong the whole ladder shifts sideways by the same
# amount — which is why the engine cross-checks the implied
# P(GOP >= 50) against the market's own ladder sum and flags a gross
# disagreement instead of trading on it.
SENATE_GOP_NOT_UP = 31
SENATE_RACES_EXPECTED = 35


def slug_code(name: str) -> str:
    """The exchange's candidate code: first three letters of the first
    name plus first three of the last ("Xavier Becerra" -> xavbec,
    "J.D. Vance" -> jdvan)."""
    words = [w for w in name.replace(".", "").split() if w]
    if not words:
        return ""
    first = "".join(c for c in words[0] if c.isalpha()).lower()[:3]
    last = "".join(c for c in words[-1] if c.isalpha()).lower()[:3]
    return first + last


def parse_races(text: str) -> dict[str, dict]:
    """Datawrapper race table -> {abbr: {dem, rep, name, cands}} as
    fractions. cands maps the exchange's candidate code to Silver's OWN
    per-candidate odds (the name_D1/winner_D1... columns) — the owner's
    2026-08-21 correction: the model DOES price candidate markets, and
    a port that drops those columns silently unpriced hundreds of them."""
    out: dict[str, dict] = {}
    for row in csv.DictReader(io.StringIO(text)):
        abbr = (row.get("abbr") or "").strip().lower()
        if not abbr:
            continue
        try:
            dem = float(row.get("winner_Dparty") or "") / 100.0
            rep = float(row.get("winner_Rparty") or "") / 100.0
        except ValueError:
            continue
        cands: dict[str, float] = {}
        for party in ("D", "R"):
            for i in (1, 2, 3, 4):
                nm = (row.get(f"name_{party}{i}") or "").strip()
                pv = (row.get(f"winner_{party}{i}") or "").strip()
                if not nm or not pv:
                    continue
                try:
                    code = slug_code(nm)
                    if code:
                        cands[code] = float(pv) / 100.0
                except ValueError:
                    continue
        out[abbr] = {"dem": dem, "rep": rep,
                     "name": (row.get("state") or "").strip(),
                     "cands": cands}
    return out


def parse_official_dist(text: str) -> dict[str, dict[str, dict[int, float]]]:
    """The simulation histogram CSV (model,chamber,seats,prob — seats are
    DEMOCRATIC, prob in percent) -> {"senate"/"house": {flavor: pmf}} with
    pmf keyed by GOP/R seats and normalized to sum 1. A flavor whose rows
    don't sum to ~100% is dropped rather than served half-parsed."""
    out: dict[str, dict[str, dict[int, float]]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        try:
            flavor = (row.get("model") or "").strip().lower()
            chamber = (row.get("chamber") or "").strip().lower()
            d_seats = int(row["seats"])
            prob = float(row["prob"])
        except (KeyError, TypeError, ValueError):
            continue
        if flavor not in FLAVORS or chamber not in ("senate", "house"):
            continue
        seats = (SENATE_TOTAL if chamber == "senate" else HOUSE_TOTAL) - d_seats
        pmf = out.setdefault(chamber, {}).setdefault(flavor, {})
        pmf[seats] = pmf.get(seats, 0.0) + prob
    for chamber in list(out):
        for flavor in list(out[chamber]):
            pmf = out[chamber][flavor]
            total = sum(pmf.values())
            if not 95.0 <= total <= 105.0:
                del out[chamber][flavor]
                continue
            out[chamber][flavor] = {k: v / total for k, v in pmf.items()}
        if not out[chamber]:
            del out[chamber]
    return out


def parse_official_topline(text: str) -> dict:
    """Run metadata and headline control odds from the topline CSV:
    {"run": iso datetime, "date": "YYYY-MM-DD", "sims": int,
     "d_control": {chamber: {flavor: fraction}}} — {} on garbage."""
    run = date = ""
    sims = 0
    d_control: dict[str, dict[str, float]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        flavor = (row.get("model") or "").strip().lower()
        chamber = (row.get("chamber") or "").strip().lower()
        if flavor not in FLAVORS or chamber not in ("senate", "house"):
            continue
        try:
            d_control.setdefault(chamber, {})[flavor] = float(row["prob"]) / 100.0
        except (KeyError, TypeError, ValueError):
            continue
        run = (row.get("runDatetime") or "").strip() or run
        date = (row.get("date") or "").strip() or date
        try:
            sims = int(float(row.get("simCount") or 0)) or sims
        except (TypeError, ValueError):
            pass
    if not d_control:
        return {}
    return {"run": run, "date": date, "sims": sims, "d_control": d_control}


def _poisson_binomial(probs: list[float]) -> list[float]:
    """Exact distribution of the number of wins among independent races —
    the standard DP, used per swing node (races ARE independent once the
    shared swing is conditioned on)."""
    dist = [1.0]
    for p in probs:
        p = min(max(p, 0.0), 1.0)
        nxt = [0.0] * (len(dist) + 1)
        for k, m in enumerate(dist):
            nxt[k] += m * (1.0 - p)
            nxt[k + 1] += m * p
        dist = nxt
    return dist


def seat_pmf(rep_probs: list[float], not_up: int = SENATE_GOP_NOT_UP,
             rho: float = SWING_RHO_MID) -> dict[int, float]:
    """P(total GOP seats = K) under the one-factor copula: mix the exact
    conditional Poisson-binomial over the national swing. rho=0 is the
    old independent model; marginals match Silver's odds at every rho."""
    if rho <= 0.0:
        dist = _poisson_binomial(rep_probs)
        return {not_up + k: m for k, m in enumerate(dist)}
    rho = min(rho, 0.999)
    x = [_N.inv_cdf(min(max(p, 1e-9), 1 - 1e-9)) for p in rep_probs]
    sq_r, sq_1r = math.sqrt(rho), math.sqrt(1.0 - rho)
    nodes = [-4.0 + 8.0 * i / (_SWING_NODES - 1) for i in range(_SWING_NODES)]
    weights = [math.exp(-s * s / 2.0) for s in nodes]
    wsum = sum(weights)
    mixed = [0.0] * (len(rep_probs) + 1)
    for s, w in zip(nodes, weights):
        cond = [_N.cdf((xi - sq_r * s) / sq_1r) for xi in x]
        dist = _poisson_binomial(cond)
        for k, m in enumerate(dist):
            mixed[k] += m * w / wsum
    return {not_up + k: m for k, m in enumerate(mixed)}


def rung_fair(pmf: dict[int, float], rung: str) -> float | None:
    """A ladder rung's model value: '52' -> P(=52), 'gte57' -> P(>=57),
    'lte45' -> P(<=45). None for a rung this pmf cannot price."""
    try:
        if rung.startswith("gte"):
            n = int(rung[3:])
            return sum(v for k, v in pmf.items() if k >= n)
        if rung.startswith("lte"):
            n = int(rung[3:])
            return sum(v for k, v in pmf.items() if k <= n)
        n = int(rung)
        return pmf.get(n, 0.0)
    except ValueError:
        return None


def slug_rung(slug: str) -> str:
    return slug.rsplit("-", 1)[-1]


class SilverFairs:
    """Cached senate-ladder fairs. `refresh` fetches on a slow TTL (call
    it from the engine cycle, never from a web request); `fair` reads
    the cache only and never blocks."""

    def __init__(self, client=None, clock=None):
        self.client = client            # v2.api.Client, for its session/retries
        self._clock = clock or time.time
        # every observed model move, for the /silver page: the feed does
        # not carry the POLLS that cause a move, only the odds — so the
        # log records what changed and when we saw it, and the page links
        # to the source table
        self.changes: list[dict] = []
        self.races: dict[str, dict] = {}
        self.gov_races: dict[str, dict] = {}   # governor table, same shape
        self.pmf: dict[int, float] = {}       # central curve (SWING_RHO_MID)
        self.pmf_lo: dict[int, float] = {}    # rho = SWING_RHO_LOW
        self.pmf_hi: dict[int, float] = {}    # rho = SWING_RHO_HIGH
        self.fetched_at = 0.0
        self.changed_at = 0.0            # when the numbers last MOVED
        self._content_sig = ""
        self.gov_changed_at = 0.0
        self._gov_sig = ""
        self._gov_cid = ""
        self.gov_raw = ""
        self.senate_raw = ""
        self.source = "none"
        self.note = ""
        # Silver's own simulated distributions — the primary model
        self.official: dict[str, dict[str, dict[int, float]]] = {}
        self.official_meta: dict = {}
        self.official_fetched = 0.0
        self.official_source = "none"
        self.official_note = ""

    def refresh(self, now: float | None = None) -> bool:
        now = now if now is not None else self._clock()
        changed = self._refresh_races(now)
        changed = self._refresh_gov(now) or changed
        return self._refresh_official(now) or changed

    def _resolve_csv(self, cid: str, default_url: str) -> str:
        """A republished Datawrapper chart can leave its old data
        address serving frozen numbers forever — the Alaska governor
        lesson (2026-08-21: the table froze at the Aug 18 primary while
        the site moved on). Follow the chart page's redirects to the
        live version and use the data URL it actually references."""
        try:
            import re
            import requests
            url = f"https://datawrapper.dwcdn.net/{cid}/"
            for _ in range(4):
                r = requests.get(url, timeout=20,
                                 headers={"User-Agent": "liquidity-rewards v3"})
                if r.status_code >= 400:
                    return default_url
                if len(r.text) < 2000:
                    m = re.search(
                        r"url=(https://datawrapper\.dwcdn\.net/[^\"'>\s]+)",
                        r.text)
                    if m:
                        url = m.group(1).rstrip("+' ")
                        continue
                m = re.search(
                    r"https://static\.dwcdn\.net/data/[A-Za-z0-9]+\.csv"
                    r"[^\\\s\"']*",
                    r.text)
                found = m.group(0).rstrip("\\") if m else default_url
                if found.split("?")[0] != default_url:
                    self.note = (f"{cid} data moved to "
                                 f"{found.rsplit('/', 1)[-1].split('?')[0]}")
                return found
            return default_url
        except Exception:  # noqa: BLE001 — the fixed address still works
            return default_url

    def _refresh_gov(self, now: float) -> bool:
        if self.gov_races and now - getattr(self, "_gov_at", 0.0) < TTL_S:
            return False
        want = set(self.gov_races)
        if not want:
            try:
                want = set(parse_races(GOV_FALLBACK.read_text()))
            except OSError:
                pass
        got: dict = {}
        raw_text = ""
        try:
            import requests
            order = ((self._gov_cid,) if self._gov_cid else ()) + \
                tuple(c for c in GOV_CANDIDATE_IDS if c != self._gov_cid)
            for cid in order:
                u = f"https://static.dwcdn.net/data/{cid}.csv"
                u += ("&" if "?" in u else "?") + f"v={int(now * 1000)}"
                r = requests.get(u, timeout=20,
                                 headers={"User-Agent": "liquidity-rewards v3"})
                if r.status_code >= 400:
                    continue
                cand = parse_races(r.text)
                if not cand:
                    continue
                if set(cand) == set(self.races) and cand == self.races:
                    continue          # that one is the senate table
                if want and set(cand) != want:
                    continue          # different race set — not governor
                got = cand
                raw_text = r.text
                if cid != self._gov_cid:
                    self._gov_cid = cid
                    self.note = f"governor data found under chart {cid}"
                break
        except Exception:  # noqa: BLE001 — fall through to the disk copy
            pass
        if not got:
            try:
                got = parse_races(GOV_FALLBACK.read_text())
            except OSError:
                return False
        if got:
            if raw_text:
                self.gov_raw = raw_text
            self._diff_races(self.gov_races, got, "governor", now)
            sig = repr(sorted((k, v.get("dem")) for k, v in got.items()))
            if sig != self._gov_sig:
                self._gov_sig = sig
                self.gov_changed_at = now
            self.gov_races = got
            self._gov_at = now
        return bool(got)

    def _diff_races(self, old: dict, new: dict, chamber: str,
                    now: float) -> None:
        if not old:
            return
        for ab, row in new.items():
            o = old.get(ab)
            if not o:
                continue
            d = (row.get("rep") or 0.0) - (o.get("rep") or 0.0)
            if abs(d) >= 0.005:
                self.changes.append({
                    "ts": round(now, 1), "chamber": chamber, "abbr": ab,
                    "name": row.get("name") or ab.upper(),
                    "old": round((o.get("rep") or 0.0) * 100, 1),
                    "new": round((row.get("rep") or 0.0) * 100, 1)})
        del self.changes[:-200]

    def race_fair(self, slug: str) -> float | None:
        """The Silver table's win probability for a party's race-winner
        market: senate (usse...) and governor (usgub...) markets whose
        last token names the party. Candidate-coded and margin markets
        return None — the table doesn't price them."""
        parts = (slug or "").split("-")
        tail = parts[-1] if parts else ""
        # PRIMARIES ARE NOT THIS TABLE (owner, 2026-08-23: "It's a
        # primary and he's going to win the primary but lose the
        # general election"). Silver prices the GENERAL; a primary asks
        # a different question entirely. The docstring always claimed
        # primaries stayed unpriced, but the matching below is by
        # substring — "usgubp" CONTAINS "usgub" and "ussep" STARTS WITH
        # "usse" — so every primary was quietly priced at its
        # candidate's chance of winning the general. Massachusetts
        # governor rep: model 0.055c against a market at 92/94, which
        # built a 381-share short.
        if any(p in ("usgubp", "ussep", "ushrp", "uspresp") for p in parts):
            return None
        # party tails AND candidate-coded tails both resolve; anything
        # else (margin brackets, primaries) stays unpriced
        if any(p.startswith("usse") for p in parts):
            table = self.races
        elif any("usgub" in p for p in parts):
            table = self.gov_races
        else:
            return None
        st = next((p for p in parts if p in table), None)
        if st is None:
            return None
        v = table[st].get(tail)
        if v is not None:
            return float(v)
        cv = (table[st].get("cands") or {}).get(tail)
        return float(cv) if cv is not None else None

    def model_fair(self, slug: str) -> float | None:
        """One entry point for the engine: a race-winner probability
        (party or candidate), a chamber-control probability, or a
        seat-ladder rung value, or None."""
        v = self.race_fair(slug)
        if v is not None:
            return v
        tail = slug.rsplit("-", 1)[-1]
        if tail in ("dem", "rep") and ("usho" in slug or "usse-midterms" in slug):
            # chamber control: the House winner markets, and the Senate
            # control market (2026-09-10: it had no number, so the fair
            # the owner asked for could not be seeded there)
            ctl = self.control("house" if "usho" in slug else "senate")
            if ctl:
                gop = ctl.get("deluxe") or next(iter(ctl.values()))
                return gop if tail == "rep" else 1.0 - gop
        return self.fair(slug)

    def _refresh_races(self, now: float) -> bool:
        if self.races and now - self.fetched_at < TTL_S:
            return False
        text = ""
        try:
            if self.client is not None:
                import requests
                u = self._resolve_csv("kNspD", SENATE_URL)
                u += ("&" if "?" in u else "?") + f"v={int(now * 1000)}"
                r = requests.get(u, timeout=20,
                                 headers={"User-Agent": "liquidity-rewards v2"})
                if r.status_code < 400:
                    text, self.source = r.text, "cdn"
        except Exception as e:  # noqa: BLE001 — fall through to the disk copy
            self.note = f"cdn: {type(e).__name__}"
        if not text:
            try:
                text, self.source = SENATE_FALLBACK.read_text(), "disk"
            except OSError:
                self.note = "no silver table anywhere"
                return False
        return self.load(text, now)

    def _refresh_official(self, now: float) -> bool:
        # a disk copy is a stopgap, not a success — keep trying the sheet
        ttl = TTL_S if self.official_source == "sheets" else 1800.0
        if self.official and now - self.official_fetched < ttl:
            return False
        top = dist = ""
        source = "none"
        try:
            if self.client is not None:
                import requests
                hdrs = {"User-Agent": "liquidity-rewards v2"}
                rt = requests.get(OFFICIAL_TOPLINE_URL, timeout=15, headers=hdrs)
                rd = requests.get(OFFICIAL_DIST_URL, timeout=20, headers=hdrs)
                if rt.status_code < 400 and rd.status_code < 400:
                    top, dist, source = rt.text, rd.text, "sheets"
        except Exception as e:  # noqa: BLE001 — fall through to the disk copy
            self.official_note = f"sheets: {type(e).__name__}"
        if not dist:
            if self.official:
                self.official_fetched = now   # keep what we have, retry later
                return False
            try:
                top = OFFICIAL_TOPLINE_FALLBACK.read_text()
                dist = OFFICIAL_DIST_FALLBACK.read_text()
                source = "disk"
            except OSError:
                self.official_note = "no official distributions anywhere"
                return False
        ok = self.load_official(top, dist, now)
        if ok:
            self.official_source = source
        return ok

    def load_official(self, topline_text: str, dist_text: str,
                      now: float) -> bool:
        dist = parse_official_dist(dist_text)
        if not dist:
            self.official_note = "official dist parsed empty"
            return False
        meta = parse_official_topline(topline_text)
        notes = []
        for chamber in ("senate", "house"):
            missing = [f for f in FLAVORS if f not in (dist.get(chamber) or {})]
            if missing:
                notes.append(f"{chamber} missing {'/'.join(missing)}")
        # the topline states each flavor's control odds from the full run —
        # if the histogram disagrees, the parse or the conversion broke
        for chamber, need in (("senate", 50), ("house", 218)):
            for flavor, pmf in (dist.get(chamber) or {}).items():
                stated = (meta.get("d_control") or {}).get(chamber, {}).get(flavor)
                if stated is None:
                    continue
                implied = sum(v for k, v in pmf.items() if k >= need)
                if abs((1.0 - stated) - implied) > 0.03:
                    notes.append(f"{chamber}/{flavor} control {implied:.2f} "
                                 f"vs topline {1.0 - stated:.2f}")
        self.official = dist
        self.official_meta = meta
        self.official_fetched = now
        self.official_note = "; ".join(notes)
        return True

    def official_run_age_s(self, now: float | None = None) -> float:
        """Age of the MODEL RUN itself, not of our fetch — a fresh download
        of a two-week-old run is still a two-week-old model."""
        now = now if now is not None else self._clock()
        iso = str((self.official_meta or {}).get("run") or "")
        try:
            import datetime as _dt
            return max(0.0, now - _dt.datetime.fromisoformat(iso).timestamp())
        except ValueError:
            return float("inf")

    def load(self, text: str, now: float) -> bool:
        got0 = parse_races(text)
        if got0:
            self._diff_races(self.races, got0, "senate", now)
        return self._load_inner(text, now)

    def _load_inner(self, text: str, now: float) -> bool:
        races = parse_races(text)
        if not races:
            self.note = "silver table parsed empty"
            return False
        self.senate_raw = text
        if len(races) != SENATE_RACES_EXPECTED:
            # a missing race silently shifts the whole ladder — say so
            self.note = f"{len(races)} races, expected {SENATE_RACES_EXPECTED}"
        self.races = races
        # "checked 10 minutes ago" can hide a frozen feed — track when
        # the CONTENT last changed, separately from when we last looked
        # (owner, 2026-08-21: the site had moved and the numbers had not)
        sig = repr(sorted((k, v.get("dem")) for k, v in races.items()))
        if sig != self._content_sig:
            self._content_sig = sig
            self.changed_at = now
        probs = [r["rep"] for r in races.values()]
        self.pmf = seat_pmf(probs, rho=SWING_RHO_MID)
        self.pmf_lo = seat_pmf(probs, rho=SWING_RHO_LOW)
        self.pmf_hi = seat_pmf(probs, rho=SWING_RHO_HIGH)
        self.fetched_at = now
        return True

    def age(self, now: float | None = None) -> float:
        return (now if now is not None else self._clock()) - self.fetched_at \
            if self.fetched_at else float("inf")

    @staticmethod
    def _chamber(slug: str) -> str | None:
        if slug.startswith(SENATE_PREFIX):
            return "senate"
        if slug.startswith(HOUSE_PREFIX):
            return "house"
        return None

    def fair_range(self, slug: str) -> tuple[float, float] | None:
        """The model's honest interval for a rung, not a point estimate.
        Primary: min/max across Silver's Classic/Deluxe/Lite histograms —
        Silver's own model disagreement. The copula's rho-range joins the
        envelope only when it's all there is, or when the official run has
        gone stale (the per-race table moves with every poll; the
        histogram only when Silver reruns). None for anything neither
        model can price."""
        chamber = self._chamber(slug)
        if chamber is None:
            return None
        r = slug_rung(slug)
        vals = [v for pmf in (self.official.get(chamber) or {}).values()
                if (v := rung_fair(pmf, r)) is not None]
        if chamber == "senate" and self.pmf and (
                not vals or self.official_run_age_s() > OFFICIAL_STALE_S):
            vals += [v for pmf in (self.pmf_lo, self.pmf, self.pmf_hi)
                     if (v := rung_fair(pmf, r)) is not None]
        if not vals:
            return None
        return min(vals), max(vals)

    def fair(self, slug: str) -> float | None:
        """The central value — display only; the engine uses the range.
        Deluxe is Silver's headline flavor, so it is the center."""
        chamber = self._chamber(slug)
        if chamber is None:
            return None
        r = slug_rung(slug)
        deluxe = (self.official.get(chamber) or {}).get("deluxe")
        if deluxe:
            v = rung_fair(deluxe, r)
            if v is not None:
                return v
        if chamber == "senate" and self.pmf:
            return rung_fair(self.pmf, r)
        return None

    def flavors_fair(self, slug: str) -> dict[str, float] | None:
        """Each flavor's value for a rung — what the page charts so the
        owner can see WHERE the band comes from."""
        chamber = self._chamber(slug)
        if chamber is None:
            return None
        r = slug_rung(slug)
        out = {f: round(v, 4)
               for f, pmf in (self.official.get(chamber) or {}).items()
               if (v := rung_fair(pmf, r)) is not None}
        return out or None

    def gop_control(self) -> float | None:
        """Implied P(GOP >= 50 Senate seats) — the cross-check against the
        market ladder sum; a gross disagreement means a parse or the
        holdover constant is wrong."""
        deluxe = (self.official.get("senate") or {}).get("deluxe")
        if deluxe:
            return sum(v for k, v in deluxe.items() if k >= 50)
        if not self.pmf:
            return None
        return sum(v for k, v in self.pmf.items() if k >= 50)

    def control(self, chamber: str) -> dict[str, float] | None:
        """P(GOP controls the chamber) per flavor: R >= 50 in the Senate
        (the VP breaks 50-50), R >= 218 in the House. For the headline
        card, so the page never has to interpolate rungs."""
        need = 50 if chamber == "senate" else 218
        flavors = self.official.get(chamber) or {}
        out = {f: round(sum(v for k, v in pmf.items() if k >= need), 4)
               for f, pmf in flavors.items()}
        if not out and chamber == "senate" and self.pmf:
            out = {"swing": round(sum(v for k, v in self.pmf.items()
                                      if k >= need), 4)}
        return out or None
