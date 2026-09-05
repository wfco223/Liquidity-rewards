"""The game-day experiment (owner, 2026-09-05: "create a program to
cheaply test a few day of markets for games tomorrow"): tomorrow's two
biggest college games, one market of each kind, one share a side at the
touch, off at kickoff, its own switch."""

import time
import unittest

from v3 import gameday
from v3.books import BookCache
from v3.family import Family
from v3.names import Names
from v3.orders import OrderDesk
from v3.scoring import Book
from v3.tests.test_family import FakeClient

NOW = 1_788_544_800.0                  # 2026-09-04 18:00Z, a Friday afternoon
KICK = NOW + 20 * 3600.0               # tomorrow's kickoff, 14:00Z Saturday
EV = "cfb-mia-stan-2026-09-05"
EV2 = "cfb-tex-ohst-2026-09-05"
EV_LATE = "cfb-usc-ucla-2026-09-08"    # three days out: not tomorrow
EV_GONE = "cfb-neb-cin-2026-09-04"     # kicked off an hour ago


def _mk(ev, rest, question=""):
    kind = rest.split(":")[0]
    tail = rest.split(":", 1)[1]
    slug = f"{kind}-{ev}" + (f"-{tail}" if tail else "")
    return {"slug": slug, "question": question or slug, "closed": False}


def _book(bid, ask, q=100.0):
    return Book(bids=((bid, q), (0.02, 60000.0)),
                asks=((ask, q), (0.98, 60000.0)), tick=0.01, fetched_at=NOW)


PROG = {"timePeriods": [{"programId": "cfb_t1_x", "rewardPool": 1000.0,
                         "targetSize": 5000, "discountFactor": 0.15,
                         "status": "LIVE"}]}


class GameClient(FakeClient):
    """The exchange with a college slate: events under the cfb tags."""

    def __init__(self):
        super().__init__()
        self.game_events: list[dict] = []
        self.book_reads: list[str] = []

    def events_by_tag(self, tag, max_pages=30):
        return list(self.game_events) if tag in gameday.TAGS else []

    def book(self, slug, fetched_at=None):
        self.book_reads.append(slug)
        return super().book(slug, fetched_at=fetched_at)


def _game(client, ev, start, ml_pool=4500.0):
    """One game's markets with books: the shapes the Miami probe showed."""
    rows = {
        "aec:": (0.55, 0.57),
        "asc:neg-3pt5": (0.48, 0.52), "asc:neg-10pt5": (0.20, 0.24),
        "asc:pos-3pt5": (0.60, 0.64),
        "asc:1h-neg-3pt5": (0.45, 0.50), "asc:1h-neg-7pt5": (0.30, 0.34),
        "tsc:total-50pt5": (0.49, 0.53), "tsc:total-60pt5": (0.20, 0.24),
        "tsc:tt-mia-30pt5": (0.47, 0.51), "tsc:tt-stan-10pt5": (0.30, 0.36),
        "tsc:1q-11pt5": (0.40, 0.44),
        "atc:winner-1h-mia": (0.55, 0.60), "atc:winner-1h-stan": (0.35, 0.40),
        "atc:winner-1h-draw": (0.05, 0.07), "atc:winner-1q-mia": (0.50, 0.51),
        "astatc:ftd-mia": (0.50, 0.54), "astatc:ftd-stan": (0.40, 0.44),
        "astatc:ftd-none": (0.02, 0.04), "astatc:rec-a-14pt5": (0.49, 0.51),
    }
    mkts = []
    for rest, (bid, ask) in rows.items():
        m = _mk(ev, rest)
        mkts.append(m)
        client.books[m["slug"]] = _book(bid, ask)
        client.prog_raw[m["slug"]] = {"timePeriods": [
            {**PROG["timePeriods"][0], "programId": f"{ev}-{rest.split(':')[0]}"}]}
    ml = f"aec-{ev}"
    client.prog_raw[ml] = {"timePeriods": [{**PROG["timePeriods"][0],
                                            "rewardPool": ml_pool}]}
    client.game_events.append({"slug": ev, "title": ev.replace("-", " "),
                               "startTime": time.strftime(
                                   "%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
                               "markets": mkts})
    return mkts


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        gameday.time.time = lambda: NOW          # freeze the clock
        self.addCleanup(setattr, gameday.time, "time", time.time)

    def test_one_market_of_each_kind_nearest_fifty_cents(self):
        c = GameClient()
        _game(c, EV, KICK)
        out = gameday.discover(c)
        self.assertEqual(sorted(out), sorted([
            f"aec-{EV}", f"asc-{EV}-neg-3pt5", f"tsc-{EV}-total-50pt5",
            f"atc-{EV}-winner-1h-mia", f"asc-{EV}-1h-neg-3pt5",
            f"tsc-{EV}-tt-mia-30pt5", f"astatc-{EV}-ftd-mia"]))
        row = out[f"asc-{EV}-neg-3pt5"]
        self.assertEqual(row["event_n"], 3)          # three full-game lines share the pool
        self.assertEqual(row["start"], KICK)
        self.assertEqual(row["group"], "spread")
        self.assertEqual(out[f"aec-{EV}"]["event_n"], 1)
        self.assertEqual(out[f"astatc-{EV}-ftd-mia"]["event_n"], 4)
        # the first-quarter winner was never read: the pick narrows to
        # the first half before it opens a book
        self.assertNotIn(f"atc-{EV}-winner-1q-mia", c.book_reads)
        self.assertNotIn(f"astatc-{EV}-rec-a-14pt5", c.book_reads)

    def test_only_tomorrow_s_games(self):
        c = GameClient()
        _game(c, EV, KICK)
        _game(c, EV_LATE, NOW + 3 * 86400.0)
        _game(c, EV_GONE, NOW - 3600.0)
        c.game_events.append({"slug": "cfb-no-start-2026-09-05", "title": "?",
                              "markets": [_mk("cfb-no-start-2026-09-05", "aec:")]})
        out = gameday.discover(c)
        self.assertTrue(out)
        self.assertTrue(all(EV in s for s in out), sorted(out))

    def test_the_two_biggest_games_by_moneyline_pool(self):
        c = GameClient()
        _game(c, EV, KICK, ml_pool=4500.0)
        _game(c, EV2, KICK + 3600.0, ml_pool=9000.0)
        _game(c, "cfb-small-town-2026-09-05", KICK - 3600.0, ml_pool=500.0)
        out = gameday.discover(c)
        events = {r["event"] for r in out.values()}
        self.assertEqual(events, {EV, EV2})
        self.assertEqual(len(out), 14)

    def test_groups(self):
        self.assertEqual(gameday.group_of(f"asc-{EV}-neg-3pt5", EV), "spread")
        self.assertEqual(gameday.group_of(f"asc-{EV}-2q-pos-3pt5", EV), "period_spread")
        self.assertEqual(gameday.group_of(f"tsc-{EV}-tt1h-mia-14pt5", EV), None)
        self.assertEqual(gameday.group_of(f"tsc-{EV}-3q-8pt5", EV), "period_total")
        self.assertEqual(gameday.group_of(f"atc-{EV}-winner-4q-draw", EV), "period_winner")
        self.assertEqual(gameday.group_of(f"astatc-{EV}-ryd-h-100pt5", EV), "prop")
        self.assertEqual(gameday.group_of("aachc-cfb-wins-2026-11-28-mia-9pt5wins", EV), None)
        import datetime as dt
        want = dt.datetime(2026, 9, 5, 1, 0, tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(gameday.start_epoch({"startTime": "2026-09-05T01:00:00Z"}), want)
        self.assertEqual(gameday.start_epoch({"startTime": int(want * 1000)}), want)
        self.assertIsNone(gameday.start_epoch({"title": "no clock"}))


class Rig:
    def __init__(self, switch=True):
        self.now = NOW
        self.exchange = GameClient()
        self.cache = BookCache()
        self.switch = switch
        self.names = Names()
        self.fam = Family(None, self.cache, gameday.discover,
                          config=gameday.config(), alert=lambda t, m: None,
                          names=self.names, clock=lambda: self.now)
        self.desk = OrderDesk(
            client=self.exchange, whitelist=self.fam.knows,
            switch_on=lambda: self.switch,
            fresh_book=lambda s: self.cache.fresh(s, 120, self.now),
            own_at=lambda slug, side, px: sum(
                o.qty for o in self.fam.orders.values()
                if o.market == slug and o.side == side
                and abs(o.price - px) < 1e-9),
            log=lambda e: None, sleep=lambda s: None, clock=lambda: self.now)
        self.fam.desk = self.desk
        self.positions: dict[str, tuple] = {}

    def cycle(self, advance=60.0):
        self.now += advance
        gameday.time.time = lambda: self.now
        return self.fam.cycle(self.now, self.exchange.open_orders(),
                              self.positions, self.exchange, self.switch)


class TestScouts(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, gameday.time, "time", time.time)

    def _scouts(self, r):
        return [o for o in r.fam.orders.values() if o.purpose != "sell"]

    def test_a_share_a_side_at_the_touch_on_every_pick(self):
        r = Rig()
        _game(r.exchange, EV, KICK)
        for _ in range(4):
            r.cycle()
        picks = set(r.fam.universe)
        self.assertEqual(len(picks), 7)
        scouts = self._scouts(r)
        self.assertEqual(len(scouts), 14, [(o.market[-14:], o.side) for o in scouts])
        for o in scouts:
            self.assertEqual(o.qty, 1.0)
            self.assertEqual(o.purpose, "earn")
            self.assertTrue(o.why.startswith("game-day scout"))
            book = r.exchange.books[o.market]
            touch = book.bids[0][0] if o.side == "BUY" else book.asks[0][0]
            self.assertAlmostEqual(o.price, touch, 6)
        self.assertEqual({(o.market, o.side) for o in scouts},
                         {(s, side) for s in picks for side in ("BUY", "SELL")})
        self.assertLessEqual(r.fam.family_spent(), r.fam.cfg.capital_usd + 1e-9)
        self.assertLess(r.fam.family_spent(), 8.0)      # ~7 markets x ~$1
        # and nothing but scouts: no planner entries, probes or growth
        self.assertFalse(any(e.get("event") in ("place", "probe", "grow")
                             for e in r.fam.log))

    def test_switch_off_places_nothing(self):
        r = Rig(switch=False)
        _game(r.exchange, EV, KICK)
        for _ in range(3):
            r.cycle()
        self.assertEqual(self._scouts(r), [])
        self.assertEqual(r.exchange.live, {})

    def test_kickoff_pulls_the_scouts_and_keeps_the_exits(self):
        r = Rig()
        _game(r.exchange, EV, KICK)
        for _ in range(4):
            r.cycle()
        scouts = self._scouts(r)
        self.assertEqual(len(scouts), 14)
        # one bid fills before the game: the share is held, an exit rests
        bid = next(o for o in scouts if o.side == "BUY")
        del r.exchange.live[bid.id]
        r.positions[bid.market] = (1.0, bid.price)
        r.cycle()
        r.cycle()
        exits = [o for o in r.fam.orders.values() if o.purpose == "sell"]
        self.assertTrue(exits, r.fam.log[-5:])
        self.assertTrue(all(o.market == bid.market for o in exits))
        # kickoff
        r.cycle(advance=KICK - r.now + 1.0)
        r.cycle()
        pulls = [e for e in r.fam.log if e.get("event") == "kickoff_pull"]
        self.assertTrue(pulls)
        self.assertEqual(self._scouts(r), [])
        still = [o for o in r.fam.orders.values() if o.purpose == "sell"]
        self.assertTrue(still)                     # the exit stays
        # and no scout comes back while the game is on
        r.cycle()
        self.assertEqual(self._scouts(r), [])

    def test_kickoff_survives_a_restart(self):
        r = Rig()
        _game(r.exchange, EV, KICK)
        r.cycle()
        saved = r.fam.to_dict()
        self.assertTrue(saved["event_start"])
        r2 = Rig()
        r2.fam.restore(saved)
        self.assertEqual(r2.fam.event_start, r.fam.event_start)
        self.assertFalse(r2.fam.kicked_off(f"aec-{EV}", KICK - 1.0))
        self.assertTrue(r2.fam.kicked_off(f"aec-{EV}", KICK + 1.0))

    def test_the_family_is_registered_and_off_by_default(self):
        from v3.main import FAMILIES
        self.assertIn("gameday", FAMILIES)
        cfg = FAMILIES["gameday"][0]()
        self.assertTrue(cfg.scout_all)
        self.assertEqual(cfg.min_days_out, 0)
        self.assertIsNone(cfg.rest_from)
        self.assertLessEqual(cfg.capital_usd, 20.0)


if __name__ == "__main__":
    unittest.main()
