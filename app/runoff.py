"""
runoff.py
---------
Runoff volume reaching the pond, from the catchment and the rainfall record.

Primary method: SCS Curve Number (USDA NRCS TR-55 / NEH-4), applied to every
day of the rainfall record, which is also the method prescribed in Indian
watershed-planning manuals.

    S  = 25400 / CN - 254              potential retention (mm)
    Ia = 0.2 * S                       initial abstraction (mm)
    Q  = (P - Ia)^2 / (P - Ia + S)     daily runoff (mm), for P > Ia

The curve number is not a single constant:
  * it comes from the land cover inside the catchment (the satellite
    classification + OSM forest), per class, for the chosen hydrologic soil
    group, and runoff is area-weighted across classes;
  * it is adjusted daily for antecedent moisture (AMC I / II / III) from the
    previous 5 days' rainfall, using the growing-season limits of 35.6 mm and
    53.3 mm, and the Hawkins (1985) conversion equations.

Secondary method: the rational method (A x P x C) with a land-cover weighted
runoff coefficient, reported alongside as a cross-check.
"""

from __future__ import annotations

import numpy as np

# CN (AMC II) for hydrologic soil groups A, B, C, D - NRCS TR-55 tables.
CURVE_NUMBERS = {
    "water":      (98, 98, 98, 98),   # rain on the water surface
    "built_up":   (89, 92, 94, 95),   # dense settlement, ~85 % impervious
    "vegetation": (67, 78, 85, 89),   # row crops, straight row, good condition
    "open_land":  (68, 79, 86, 89),   # pasture / fallow / scrub, poor condition
    "forest":     (36, 60, 73, 79),   # woods, fair condition
}
# Rational-method runoff coefficients for the same classes.
RATIONAL_C = {"water": 1.0, "built_up": 0.75, "vegetation": 0.25, "open_land": 0.35, "forest": 0.15}

SOIL_GROUPS = {"A": 0, "B": 1, "C": 2, "D": 3}
SOIL_DESCRIPTIONS = {
    "A": "Sand, loamy sand - high infiltration",
    "B": "Silt loam, loam - moderate infiltration",
    "C": "Sandy clay loam - slow infiltration",
    "D": "Clay (e.g. black cotton soil) - very slow infiltration",
}


def catchment_landcover(fractions: np.ndarray | None, forest_mask: np.ndarray | None,
                        mask: np.ndarray) -> dict[str, float]:
    """Fraction of the catchment in each runoff class."""
    if fractions is None:
        return {"open_land": 1.0}
    f = fractions[mask].astype(float)       # (n, 4): water, built, vegetation, open
    share = {
        "water": float(f[:, 0].mean()),
        "built_up": float(f[:, 1].mean()),
        "vegetation": float(f[:, 2].mean()),
        "open_land": float(f[:, 3].mean()),
    }
    if forest_mask is not None and forest_mask[mask].any():
        # OSM forest overrides whatever the imagery called that land.
        forest_share = float(forest_mask[mask].mean())
        scale = 1.0 - forest_share
        share = {k: v * scale for k, v in share.items()}
        share["forest"] = forest_share
    total = sum(share.values()) or 1.0
    return {k: v / total for k, v in share.items() if v > 0}


def _amc_adjust(cn2: float, amc: np.ndarray) -> np.ndarray:
    cn1 = cn2 / (2.281 - 0.01281 * cn2)
    cn3 = cn2 / (0.427 + 0.00573 * cn2)
    return np.where(amc == 1, cn1, np.where(amc == 3, cn3, cn2))


def scs_daily_runoff(precip_mm: np.ndarray, cn2: float, lam: float = 0.2) -> np.ndarray:
    p = np.asarray(precip_mm, dtype=float)
    # 5-day antecedent rainfall (excluding the current day)
    p5 = np.convolve(np.concatenate([np.zeros(5), p]), np.ones(5), "valid")[:-1]
    amc = np.where(p5 < 35.6, 1, np.where(p5 > 53.3, 3, 2))
    cn = np.clip(_amc_adjust(cn2, amc), 1, 99.9)
    s = 25400.0 / cn - 254.0
    ia = lam * s
    return np.where(p > ia, (p - ia) ** 2 / (p - ia + s), 0.0)


def estimate_runoff(daily: dict | None, area_m2: float, cover: dict[str, float], soil_group: str,
                    fallback_rain_mm: float | None = None, c_override: float | None = None) -> dict:
    g = SOIL_GROUPS[soil_group]
    composite_cn = sum(CURVE_NUMBERS[k][g] * v for k, v in cover.items())
    c_weighted = sum(RATIONAL_C[k] * v for k, v in cover.items())
    c = c_override if c_override is not None else c_weighted

    result: dict = {
        "catchment_area_m2": round(area_m2, 1),
        "hydrologic_soil_group": soil_group,
        "soil_description": SOIL_DESCRIPTIONS[soil_group],
        "catchment_landcover_share": {k: round(v, 4) for k, v in cover.items()},
        "composite_curve_number": round(composite_cn, 1),
    }

    if daily is None:
        rain = float(fallback_rain_mm or 0.0)
        vol = area_m2 * rain / 1000.0 * c
        result.update({
            "method": "Rational method only (no daily rainfall available)",
            "mean_annual_rainfall_mm": rain,
            "rational_runoff_coefficient": round(c, 3),
            "mean_annual_runoff_m3": round(vol, 1),
            "dependable_annual_runoff_m3": round(vol * 0.75, 1),
            "rational_method_runoff_m3": round(vol, 1),
            "annual_series": [],
        })
        return result

    dates = daily["dates"]
    p = np.asarray(daily["precip"], dtype=float)
    years = np.array([int(d[:4]) for d in dates])

    q = np.zeros_like(p)
    for cls, share in cover.items():
        q += share * scs_daily_runoff(p, CURVE_NUMBERS[cls][g])

    uniq = sorted(set(years.tolist()))
    annual_p = np.array([p[years == y].sum() for y in uniq])
    annual_q = np.array([q[years == y].sum() for y in uniq])
    annual_vol = annual_q / 1000.0 * area_m2

    mean_p = float(annual_p.mean())
    rational = area_m2 * mean_p / 1000.0 * c
    result.update({
        "method": "SCS Curve Number, daily, AMC-adjusted, area-weighted by land cover",
        "mean_annual_rainfall_mm": round(mean_p, 1),
        "mean_annual_runoff_mm": round(float(annual_q.mean()), 1),
        "effective_runoff_coefficient": round(float(annual_q.sum() / max(annual_p.sum(), 1e-9)), 3),
        "mean_annual_runoff_m3": round(float(annual_vol.mean()), 1),
        "dependable_annual_runoff_m3": round(float(np.percentile(annual_vol, 25)), 1),
        "min_annual_runoff_m3": round(float(annual_vol.min()), 1),
        "max_annual_runoff_m3": round(float(annual_vol.max()), 1),
        "rational_runoff_coefficient": round(c, 3),
        "rational_method_runoff_m3": round(rational, 1),
        "annual_series": [
            {"year": y, "rainfall_mm": round(float(a), 1), "runoff_mm": round(float(b), 1),
             "runoff_m3": round(float(v), 1)}
            for y, a, b, v in zip(uniq, annual_p, annual_q, annual_vol)
        ],
    })
    return result
