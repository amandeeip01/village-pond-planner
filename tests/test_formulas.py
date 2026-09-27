"""Unit tests: every formula checked against an independent hand calculation."""

import math

import numpy as np
import pytest

from app.design import frustum_volume, recommend_pond, width_for_volume
from app.rainfall import _extraterrestrial_radiation, summarise
from app.runoff import CURVE_NUMBERS, _amc_adjust, estimate_runoff, scs_daily_runoff


# ---------------------------------------------------------------- SCS-CN
def scs_by_hand(p, cn):
    s = 25400 / cn - 254
    ia = 0.2 * s
    return (p - ia) ** 2 / (p - ia + s) if p > ia else 0.0


def test_scs_textbook_value():
    # NRCS TR-55 worked example scale: P = 100 mm, CN = 80
    # S = 63.5 mm, Ia = 12.7 mm, Q = 87.3^2 / 150.8 = 50.54 mm
    q = scs_daily_runoff(np.array([0, 0, 0, 0, 0, 40.0, 100.0]), 80)
    # 5-day antecedent for the last day = 40 mm -> AMC II (35.6-53.3), so CN stays 80
    assert q[-1] == pytest.approx(50.54, abs=0.05)


@pytest.mark.parametrize("p,cn", [(10, 70), (25, 85), (60, 78), (150, 90)])
def test_scs_matches_hand_formula_amc2(p, cn):
    # 45 mm in the preceding 5 days -> AMC II
    series = np.array([9.0] * 5 + [p])
    assert scs_daily_runoff(series, cn)[-1] == pytest.approx(scs_by_hand(p, cn), rel=1e-9)


def test_scs_no_runoff_below_initial_abstraction():
    # CN 70 -> S = 108.9 mm, Ia = 21.8 mm; 20 mm must give zero
    assert scs_daily_runoff(np.array([20.0]), 70)[0] == 0.0


def test_amc_conversion_hawkins():
    # Hawkins (1985): CN I and CN III for CN II = 75 -> 56.8 and 87.0 (published tables: 57 / 88)
    cn1, cn3 = _amc_adjust(75.0, np.array([1, 3]))
    assert cn1 == pytest.approx(56.8, abs=0.3)
    assert cn3 == pytest.approx(87.0, abs=0.7)


def test_dry_antecedent_lowers_runoff():
    wet = scs_daily_runoff(np.array([20.0] * 5 + [60.0]), 80)[-1]   # 100 mm before -> AMC III
    dry = scs_daily_runoff(np.array([0.0] * 5 + [60.0]), 80)[-1]    # 0 mm before -> AMC I
    assert dry < scs_by_hand(60, 80) < wet


def test_runoff_volume_is_depth_times_area():
    dates = [f"2020-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 29)] + ["2020-12-29"]
    precip = [0.0] * len(dates)
    precip[100] = 80.0
    res = estimate_runoff({"dates": dates, "precip": precip}, 1_000_000, {"open_land": 1.0}, "C")
    cn = CURVE_NUMBERS["open_land"][2]
    # zero antecedent rain -> AMC I
    cn1 = cn / (2.281 - 0.01281 * cn)
    expected_mm = scs_by_hand(80, cn1)
    assert res["mean_annual_runoff_mm"] == pytest.approx(expected_mm, abs=0.1)
    assert res["mean_annual_runoff_m3"] == pytest.approx(expected_mm / 1000 * 1_000_000, rel=1e-3)


def test_rational_fallback():
    res = estimate_runoff(None, 2_000_000, {"open_land": 1.0}, "B", fallback_rain_mm=800, c_override=0.3)
    assert res["mean_annual_runoff_m3"] == pytest.approx(2_000_000 * 0.8 * 0.3)


# ---------------------------------------------------------------- pond geometry
def test_frustum_against_analytic_square():
    # Square pond: top 20 x 20, depth 2, slope 1:1 -> bottom 16 x 16
    # V = h/3 (A1 + A2 + sqrt(A1 A2)) = 2/3 (400 + 256 + 320) = 650.67 m3
    assert frustum_volume(20, 1.0, 2.0, 1.0) == pytest.approx(650.667, abs=0.01)


def test_frustum_rectangular_prismoidal():
    # 30 x 20 top, d = 3, z = 1.5 -> bottom 21 x 11, mid 25.5 x 15.5
    v = 3 / 6 * (30 * 20 + 4 * 25.5 * 15.5 + 21 * 11)
    assert frustum_volume(20, 1.5, 3.0, 1.5) == pytest.approx(v, rel=1e-12)


def test_width_for_volume_inverts_volume():
    for vol in (500, 10_000, 80_000):
        w = width_for_volume(vol, 1.5, 3.0, 1.5)
        assert frustum_volume(w, 1.5, 3.0, 1.5) == pytest.approx(vol, rel=1e-6)


def test_recommend_pond_hits_target_and_rules():
    r = recommend_pond(dependable_runoff_m3=60_000, mean_runoff_m3=90_000, land_area_m2=None,
                       dry_season_et0_mm=1200, soil_group="C")
    assert r["storage_capacity_m3"] == pytest.approx(30_000, rel=1e-3)      # 50 % of dependable
    assert r["water_depth_m"] == 3.0
    assert frustum_volume(r["top_width_m"], 1.5, 3.0, 1.5) == pytest.approx(30_000, rel=1e-3)
    assert r["fills_per_dependable_year"] == pytest.approx(2.0, rel=1e-3)


def test_recommend_pond_goes_deeper_on_small_land():
    r = recommend_pond(dependable_runoff_m3=60_000, mean_runoff_m3=90_000, land_area_m2=12_000,
                       dry_season_et0_mm=1200, soil_group="C")
    assert r["water_depth_m"] > 3.0
    assert r["footprint_area_m2"] <= 0.8 * 12_000 + 1


def test_recommend_pond_caps_when_land_too_small():
    r = recommend_pond(dependable_runoff_m3=400_000, mean_runoff_m3=500_000, land_area_m2=5_000,
                       dry_season_et0_mm=1200, soil_group="C")
    assert r["water_depth_m"] == 4.5
    assert r["storage_capacity_m3"] < r["target_storage_m3"]
    assert r["footprint_area_m2"] <= 4_000 + 1
    assert any("capped" in n for n in r["notes"])


# ---------------------------------------------------------------- rainfall stats
def test_extraterrestrial_radiation_fao56_example():
    # FAO-56 Example 8: lat 20 S, 3 September (doy 246) -> Ra = 32.2 MJ m-2 d-1
    assert _extraterrestrial_radiation(-20.0, np.array([246]))[0] == pytest.approx(32.2, abs=0.1)


def test_summary_statistics():
    dates, precip = [], []
    for y, total in zip(range(2001, 2005), (400, 600, 800, 1000)):
        for d in range(1, 366):
            dates.append(f"{y}-{'07' if d <= 31 else '01'}-{min(d, 28):02d}")
            precip.append(total / 10 if d <= 10 else 0.0)
    s = summarise({"dates": dates, "precip": precip, "et0": [4.0] * len(dates), "source": "t"})
    assert s["mean_annual_mm"] == 700
    assert s["dependable_75pct_mm"] == pytest.approx(np.percentile([400, 600, 800, 1000], 25))
    assert s["min_annual"] == {"year": 2001, "mm": 400.0}
    assert s["mean_rainy_days"] == 10
    assert s["mean_annual_et0_mm"] == pytest.approx(4.0 * 365, abs=0.5)
