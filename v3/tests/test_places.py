"""Places (owner, 2026-09-07: "make a way of tracking which places
Polymarket thinks are vpn and which are okay"): the outbound addresses
this server has run from, each with the exchange's last word on it."""

import unittest

from v3.orders import PlaceHealth
from v3.places import CHECK_EVERY_S, RECHECK_AFTER_S, Places, parse_address


class Lookup:
    """A what-is-my-address service that answers what we tell it."""

    def __init__(self, ip="203.0.113.5"):
        self.ip = ip
        self.calls = 0
        self.fail = False

    def __call__(self, url):
        self.calls += 1
        if self.fail:
            raise OSError("no route")
        return f"{self.ip}\n"


class TestTheLedger(unittest.TestCase):
    def setUp(self):
        self.look = Lookup()
        self.changes = []
        self.now = 1_000_000.0
        self.p = Places(fetch=self.look, clock=lambda: self.now,
                        on_change=lambda ip, prior, m: self.changes.append((ip, prior)))

    def test_the_answer_is_parsed_and_junk_is_not(self):
        self.assertEqual(parse_address(" 203.0.113.5\n"), "203.0.113.5")
        self.assertEqual(parse_address("2001:db8::1"), "2001:db8::1")
        self.assertIsNone(parse_address("<html>nope</html>"))
        self.assertIsNone(parse_address(""))

    def test_the_first_look_records_the_address_as_new(self):
        self.assertTrue(self.p.due(self.now))
        self.assertEqual(self.p.check(self.now, why="boot"), "203.0.113.5")
        self.assertEqual(self.p.current, "203.0.113.5")
        self.assertEqual(self.p.verdict(), "new")
        self.assertEqual(self.changes, [("203.0.113.5", "new")])
        self.assertEqual(self.p.seen["203.0.113.5"]["boots"], 1)
        self.assertFalse(self.p.due(self.now + 10))
        self.assertTrue(self.p.due(self.now + CHECK_EVERY_S))

    def test_the_exchange_s_words_pin_to_the_address(self):
        self.p.check(self.now)
        self.p.accepted(self.now + 5)
        self.assertEqual(self.p.verdict(), "okay")
        self.p.refused(self.now + 60)
        self.assertEqual(self.p.verdict(), "vpn")
        m = self.p.seen["203.0.113.5"]
        self.assertEqual((m["accepted"], m["refused"]), (1, 1))
        self.assertEqual([e["event"] for e in self.p.events], ["address", "okay", "vpn"])
        # accepted again later: the last word wins
        self.p.accepted(self.now + 120)
        self.assertEqual(self.p.verdict(), "okay")

    def test_a_refusal_looks_the_address_up_again_but_not_every_minute(self):
        self.p.check(self.now)
        self.assertEqual(self.look.calls, 1)
        self.look.ip = "198.51.100.9"                     # the pool rotated under us
        self.p.refused(self.now + 30)
        self.assertEqual(self.look.calls, 2)
        self.assertEqual(self.p.current, "198.51.100.9")   # the refusal is pinned to the new one
        self.assertEqual(self.p.verdict("198.51.100.9"), "vpn")
        self.assertEqual(self.p.verdict("203.0.113.5"), "new")
        self.p.refused(self.now + 90)                      # the probe a minute later
        self.assertEqual(self.look.calls, 2)               # no second lookup yet
        self.p.refused(self.now + 30 + RECHECK_AFTER_S)
        self.assertEqual(self.look.calls, 3)

    def test_a_deploy_onto_a_known_vpn_address_is_said_at_once(self):
        self.p.check(self.now, why="boot")
        self.p.refused(self.now + 10)
        self.look.ip = "198.51.100.9"
        self.p.check(self.now + 3600, why="boot")
        self.p.accepted(self.now + 3610)
        self.look.ip = "203.0.113.5"                       # back on the bad one
        self.p.check(self.now + 7200, why="boot")
        self.assertEqual(self.changes[-1], ("203.0.113.5", "vpn"))
        v = self.p.view()
        self.assertEqual((v["current"], v["verdict"]), ("203.0.113.5", "vpn"))
        self.assertEqual((v["okay_n"], v["vpn_n"]), (1, 1))
        self.assertEqual(v["rows"][0]["ip"], "203.0.113.5")   # the current one first
        self.assertTrue(v["rows"][0]["current"])
        self.assertEqual(v["rows"][0]["boots"], 2)

    def test_recovery_looks_again_and_marks_the_address_okay(self):
        self.p.check(self.now)
        self.p.refused(self.now + 10)
        self.look.ip = "198.51.100.9"
        self.p.recovered(self.now + 600)
        self.assertEqual(self.p.current, "198.51.100.9")
        self.assertEqual(self.p.verdict(), "okay")
        self.assertEqual(self.p.verdict("203.0.113.5"), "vpn")

    def test_a_dead_lookup_keeps_the_last_address_and_says_so(self):
        self.p.check(self.now)
        self.look.fail = True
        self.assertIsNone(self.p.check(self.now + 3600))
        self.assertEqual(self.p.current, "203.0.113.5")
        self.assertIn("no route", self.p.check_note)
        self.assertEqual(self.p.events[-1]["event"], "lookup_failed")
        self.p.accepted(self.now + 3601)                   # still pinned to the last known
        self.assertEqual(self.p.verdict(), "okay")
        self.look.fail = False
        self.p.check(self.now + 7200)
        self.assertEqual(self.p.check_note, "")

    def test_the_ledger_survives_a_restart(self):
        self.p.check(self.now)
        self.p.refused(self.now + 10)
        d = self.p.to_dict()
        q = Places(fetch=Lookup("198.51.100.9"), clock=lambda: self.now + 9000)
        q.restore(d)
        self.assertEqual(q.verdict("203.0.113.5"), "vpn")
        self.assertTrue(q.due())                           # a restart looks at once
        q.check(why="boot")
        self.assertEqual(q.current, "198.51.100.9")
        self.assertEqual(q.verdict(), "new")
        self.assertEqual(len(q.seen), 2)

    def test_the_breaker_tells_the_ledger_every_word(self):
        self.p.check(self.now)
        kinds = []

        def verdict(kind, now):
            kinds.append(kind)
            {"refused": self.p.refused, "recovered": self.p.recovered,
             "accepted": self.p.accepted}[kind](now)
        h = PlaceHealth(clock=lambda: self.now + 100, on_verdict=verdict)
        h.accepted()
        h.refused("VPN")
        h.refused("VPN")
        h.accepted()
        self.assertEqual(kinds, ["accepted", "refused", "refused", "recovered"])
        m = self.p.seen["203.0.113.5"]
        self.assertEqual((m["accepted"], m["refused"]), (2, 2))
        self.assertEqual(self.p.verdict(), "okay")

    def test_a_broken_ledger_never_breaks_a_placement(self):
        def boom(kind, now):
            raise RuntimeError("x")
        h = PlaceHealth(clock=lambda: self.now, on_verdict=boom)
        h.refused("VPN")
        h.accepted()
        self.assertFalse(h.blocked())


if __name__ == "__main__":
    unittest.main()
