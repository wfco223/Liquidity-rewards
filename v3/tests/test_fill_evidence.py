"""The fill model learns from our own fills (owner, 2026-09-04: "fit the
touch hazard from our own fills per hour rested"), fresh orders fill
faster than old ones, and the calibration note grades what the model
expected while the orders rested — per order type, bond fills left out."""

import unittest

from v3.fillmodel import (AGE_PRIOR_FILLS, DAY_S, OWN_PRIOR_S, FillModel,
                          family_of)

GOV = "usgubewc-usgub-tx-2026-11-03-rep"
FAM = family_of(GOV)


def _crossing_only(m: FillModel, per_day: float, hours: float = 240.0) -> None:
    """Feed the book-sample proxy so its hazard reads per_day at the touch."""
    sec = hours * 3600.0
    m.obs[m._key(FAM, "BUY", 0)] = [sec, per_day * sec / DAY_S]


class TestOwnFillsCarryTheHazard(unittest.TestCase):
    def test_no_own_exposure_means_the_crossing_proxy(self):
        m = FillModel()
        _crossing_only(m, 0.4)
        self.assertAlmostEqual(m.hazard_per_day(FAM, "BUY", 0),
                               m.crossing_hazard_per_day(FAM, "BUY", 0), 6)

    def test_own_fills_lift_a_touch_the_proxy_calls_quiet(self):
        m = FillModel()
        _crossing_only(m, 0.4)                     # the proxy says 0.4/day
        # our own orders rested ten days at the touch and took 27 fills
        for _ in range(240):
            m.observe_rest(GOV, "BUY", 0, 1800.0, 3600.0)
        for _ in range(27):
            m.observe_own_fill(GOV, "BUY", 0, 1800.0)
        h = m.hazard_per_day(FAM, "BUY", 0)
        # 27 fills over 10 days, with the ~0.4/day proxy standing in for
        # a week more: (27 + proxy * 7) / 17 days
        cross = m.crossing_hazard_per_day(FAM, "BUY", 0)
        self.assertAlmostEqual(cross, 0.4, 1)
        self.assertAlmostEqual(h, (27 + cross * 7) / 17.0, 6)
        self.assertGreater(h, 2 * m.crossing_hazard_per_day(FAM, "BUY", 0))

    def test_own_survival_lowers_a_depth_the_proxy_overrates(self):
        m = FillModel()
        m.obs[m._key(FAM, "BUY", 3)] = [240 * 3600.0, 0.15 * 10.0]   # 0.15/day
        for _ in range(240):
            m.observe_rest(GOV, "BUY", 7, 1800.0, 3600.0)   # 7 back -> "3+"
        m.observe_own_fill(GOV, "BUY", 7, 1800.0)             # one fill
        self.assertLess(m.hazard_per_day(FAM, "BUY", 7),
                        m.crossing_hazard_per_day(FAM, "BUY", 7))

    def test_the_prior_weighs_a_week_of_order_time(self):
        self.assertEqual(OWN_PRIOR_S, 7 * DAY_S)

    def test_other_cells_and_families_are_untouched(self):
        m = FillModel()
        before = m.hazard_per_day(FAM, "SELL", 0)
        for _ in range(48):
            m.observe_rest(GOV, "BUY", 0, 0.0, 3600.0)
        for _ in range(20):
            m.observe_own_fill(GOV, "BUY", 0, 0.0)
        self.assertAlmostEqual(m.hazard_per_day(FAM, "SELL", 0), before, 9)
        self.assertAlmostEqual(
            m.hazard_per_day("senate", "BUY", 0),
            FillModel().hazard_per_day("senate", "BUY", 0), 9)


class TestFreshOrdersFillFaster(unittest.TestCase):
    def _fit(self) -> FillModel:
        m = FillModel()
        # equal resting time in every age bucket; fills front-loaded:
        # 40 in the first hour, 20 at 1-6h, 8 at 6-24h, 4 past a day
        for _ in range(100):
            m.observe_rest(GOV, "BUY", 0, 600.0, 3600.0)
            m.observe_rest(GOV, "BUY", 0, 3 * 3600.0, 3600.0)
            m.observe_rest(GOV, "BUY", 0, 12 * 3600.0, 3600.0)
            m.observe_rest(GOV, "BUY", 0, 3 * 86400.0, 3600.0)
        for n, age in ((40, 600.0), (20, 3 * 3600.0), (8, 12 * 3600.0),
                       (4, 3 * 86400.0)):
            for _ in range(n):
                m.observe_own_fill(GOV, "BUY", 0, age)
        return m

    def test_no_data_means_no_effect(self):
        m = FillModel()
        self.assertEqual(m.age_multiplier(FAM, 0.0), 1.0)
        self.assertEqual(m.age_factor(FAM, 0.0, DAY_S), 1.0)
        self.assertEqual(m.p_fill(GOV, "BUY", 0, age_s=0.0),
                         m.p_fill(GOV, "BUY", 0, age_s=5 * DAY_S))

    def test_the_multiplier_follows_the_family_s_own_rates(self):
        m = self._fit()
        # overall 72 fills over 400h; first hour 40 over 100h -> 2.22x
        # before shrinkage, past a day 4 over 100h -> 0.22x
        self.assertGreater(m.age_multiplier(FAM, 0.0), 1.8)
        self.assertLess(m.age_multiplier(FAM, 3 * 86400.0), 0.4)
        self.assertGreater(m.p_fill(GOV, "BUY", 0, age_s=0.0),
                           m.p_fill(GOV, "BUY", 0, age_s=2 * DAY_S))

    def test_shrinkage_keeps_a_handful_of_fills_from_swinging_it(self):
        m = FillModel()
        for _ in range(10):
            m.observe_rest(GOV, "BUY", 0, 600.0, 3600.0)
            m.observe_rest(GOV, "BUY", 0, 3 * 86400.0, 3600.0)
        m.observe_own_fill(GOV, "BUY", 0, 600.0)     # one fresh fill
        # one fill over ten hours in each half: rate = 1/20h; the fresh
        # bucket would read 2x raw, shrunk with AGE_PRIOR_FILLS toward 1
        raw = 2.0
        got = m.age_multiplier(FAM, 0.0)
        self.assertGreater(got, 1.0)
        self.assertLess(got, raw)
        self.assertAlmostEqual(got, (1 + AGE_PRIOR_FILLS)
                               / (0.5 + AGE_PRIOR_FILLS), 6)

    def test_a_day_ahead_averages_through_the_buckets(self):
        m = self._fit()
        fresh = m.age_factor(FAM, 0.0, DAY_S)
        # a fresh order spends 1h at the fresh rate, 5h at 1-6h, 18h at
        # 6-24h: the day average sits between the fresh and the old
        self.assertLess(fresh, m.age_multiplier(FAM, 0.0))
        self.assertGreater(fresh, m.age_multiplier(FAM, 12 * 3600.0))
        expect = (m.age_multiplier(FAM, 0.0) * 1 + m.age_multiplier(FAM, 3600.0) * 5
                  + m.age_multiplier(FAM, 6 * 3600.0) * 18) / 24.0
        self.assertAlmostEqual(fresh, expect, 9)
        # from a day old on, every hour of the next day is "1d+"
        self.assertAlmostEqual(m.age_factor(FAM, 2 * DAY_S, DAY_S),
                               m.age_multiplier(FAM, 2 * DAY_S), 9)


class TestRoundTrip(unittest.TestCase):
    def test_own_and_age_evidence_persist(self):
        m = FillModel()
        m.observe_rest(GOV, "BUY", 1, 100.0, 3600.0)
        m.observe_own_fill(GOV, "BUY", 1, 100.0)
        m2 = FillModel.from_dict(m.to_dict())
        self.assertEqual(m2.own_obs, m.own_obs)
        self.assertEqual(m2.age_fit, m.age_fit)
        self.assertAlmostEqual(m2.hazard_per_day(FAM, "BUY", 1),
                               m.hazard_per_day(FAM, "BUY", 1), 9)
        s = m.summary()
        self.assertEqual(s["hazards"][f"{FAM} BUY"][1]["own_fills"], 1)
        self.assertEqual(s["hazards"][f"{FAM} BUY"][1]["hours_rested"], 1.0)
        self.assertIn("0-1h", s["age"][FAM])


if __name__ == "__main__":
    unittest.main()
