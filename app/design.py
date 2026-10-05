"""
design.py
---------
Recommends pond dimensions for an excavated (dug-out) village pond.

Geometry: a rectangular frustum (inverted truncated pyramid) with side slopes
of `side_slope` horizontal : 1 vertical, long side laid along the contour so
the pond sits across the drainage line. Volume uses the prismoidal formula,
which is exact for this shape:

    V = d / 6 * (A_top + 4 * A_mid + A_bottom)

Sizing logic:
  1. Target storage = dependable (75 %) annual runoff x fill_fraction.
     With fill_fraction = 0.5 the pond fills about twice over even in a dry
     year, leaving margin for evaporation, seepage and poor early-monsoon
     yield, instead of being a large pond that is rarely full.
  2. Depth starts at 3.0 m, the depth used in MGNREGA / Mission Amrit Sarovar
     village-pond designs. Deeper ponds lose proportionally less to
     evaporation, so if the footprint does not fit on the available land the
     depth is increased (up to max_depth_m) before the volume is reduced.
     Very small targets use a shallower depth so the pond bed stays wide
     enough to excavate with machinery.
  3. The water surface (at full supply) must fit within 80 % of the connected
     patch of suitable land around the site; the rest is left for the spoil
     bund and access.
"""

from __future__ import annotations

import math

import numpy as np

SEEPAGE_MM_PER_DAY = {"A": 10.0, "B": 5.0, "C": 2.5, "D": 1.0}
OPEN_WATER_KC = 1.05          # FAO-56 open-water evaporation factor on ET0
MIN_BED_WIDTH_M = 4.0
MAX_TOP_AREA_M2 = 20_000.0    # keep it a village pond, not a reservoir


def _dims(width: float, ratio: float, depth: float, z: float):
    length = width * ratio
    lb, wb = length - 2 * z * depth, width - 2 * z * depth
    lm, wm = length - z * depth, width - z * depth
    return length, lb, wb, lm, wm


def frustum_volume(width: float, ratio: float, depth: float, z: float) -> float:
    length, lb, wb, lm, wm = _dims(width, ratio, depth, z)
    if lb <= 0 or wb <= 0:
        return 0.0
    return depth / 6.0 * (length * width + 4 * lm * wm + lb * wb)


def width_for_volume(volume: float, ratio: float, depth: float, z: float) -> float:
    """Top width (m) that stores `volume` at `depth`, by bisection."""
    lo = 2 * z * depth + 1e-6
    hi = lo + 10.0
    while frustum_volume(hi, ratio, depth, z) < volume:
        hi *= 2
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if frustum_volume(mid, ratio, depth, z) < volume:
            lo = mid
        else:
            hi = mid
    return hi


def recommend_pond(*, dependable_runoff_m3: float, mean_runoff_m3: float, land_area_m2: float | None,
                   dry_season_et0_mm: float | None, soil_group: str, fill_fraction: float = 0.5,
                   side_slope: float = 1.5, aspect_ratio: float = 1.5, freeboard_m: float = 0.5,
                   preferred_depth_m: float = 3.0, max_depth_m: float = 4.5,
                   min_depth_m: float = 2.0) -> dict:
    notes: list[str] = []
    target = max(dependable_runoff_m3 * fill_fraction, 0.0)
    land_limit = MAX_TOP_AREA_M2
    limit_reason = f"the {MAX_TOP_AREA_M2 / 10_000:.0f} ha maximum footprint for a village pond"
    if land_area_m2 is not None and 0.8 * land_area_m2 < land_limit:
        land_limit = 0.8 * land_area_m2
        limit_reason = f"80 % of the {land_area_m2:,.0f} m2 patch of available land at the site"

    if target < 200:
        notes.append("Catchment yields very little runoff; a pond here would rarely fill. Consider another site.")
        target = max(target, 200.0)

    def top_area(vol, d):
        # Footprint is measured at the top of the freeboard (the excavation edge).
        w = width_for_volume(vol, aspect_ratio, d, side_slope)
        grow = 2 * side_slope * freeboard_m
        return w, (w * aspect_ratio + grow) * (w + grow)

    # Choose depth
    depth = preferred_depth_m
    width, footprint = top_area(target, depth)
    # small pond: go shallower so the bed is wide enough to excavate
    while depth - 0.5 >= min_depth_m and (width - 2 * side_slope * depth) < MIN_BED_WIDTH_M:
        depth -= 0.5
        width, footprint = top_area(target, depth)
    # large pond on limited land: go deeper first
    while footprint > land_limit and depth + 0.5 <= max_depth_m:
        depth += 0.5
        width, footprint = top_area(target, depth)
    volume = target
    if footprint > land_limit:
        # Reduce the volume to what fits at max depth
        lo, hi = 0.0, target
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if top_area(mid, depth)[1] <= land_limit:
                lo = mid
            else:
                hi = mid
        volume = lo
        width, footprint = top_area(volume, depth)
        if volume < 1.0:
            # Even the smallest pond at maximum depth does not fit on the land patch.
            volume = 0.0
            notes.append(
                f"No pond fits here: even the smallest excavation would exceed {limit_reason}. "
                "Choose another site or mark more available land."
            )
        else:
            notes.append(
                f"Storage capped at {volume:,.0f} m3: the full target of {target:,.0f} m3 would exceed "
                f"{limit_reason}. The catchment can support a larger structure or a second pond downstream."
            )
    if (width - 2 * side_slope * depth) < MIN_BED_WIDTH_M:
        notes.append("Pond is small; the bed is narrower than a typical excavator working width.")

    length, lb, wb, lm, wm = _dims(width, aspect_ratio, depth, side_slope)
    water_area = length * width
    mid_area = lm * wm
    exc_depth = depth + freeboard_m
    w_exc = width + 2 * side_slope * freeboard_m
    excavation = frustum_volume(w_exc, (length + 2 * side_slope * freeboard_m) / w_exc, exc_depth, side_slope)

    evap = None
    if dry_season_et0_mm and math.isfinite(dry_season_et0_mm):
        # Oct-May open-water evaporation over the mean water surface (the
        # area at half depth, as the level falls through the dry season).
        evap = mid_area * OPEN_WATER_KC * dry_season_et0_mm / 1000.0
    seep_rate = SEEPAGE_MM_PER_DAY[soil_group]
    seepage = lb * wb * seep_rate / 1000.0 * 120       # ~4 months of standing water on the bed
    if soil_group in ("A", "B"):
        notes.append("Permeable soil: line the bed (clay blanket or HDPE/LDPE film) to control seepage.")

    fills_per_year = dependable_runoff_m3 / volume if volume else 0.0
    return {
        "shape": "Rectangular excavated pond (frustum), long side along the contour",
        "design_basis": f"{fill_fraction:.0%} of 75 %-dependable annual runoff",
        "target_storage_m3": round(target, 1),
        "storage_capacity_m3": round(volume, 1),
        "water_depth_m": round(depth, 2),
        "freeboard_m": freeboard_m,
        "excavation_depth_m": round(exc_depth, 2),
        "side_slope_h_per_v": side_slope,
        "top_length_m": round(length, 1),
        "top_width_m": round(width, 1),
        "bed_length_m": round(lb, 1),
        "bed_width_m": round(wb, 1),
        "water_spread_area_m2": round(water_area, 1),
        "footprint_area_m2": round(footprint, 1),
        "excavation_volume_m3": round(excavation, 1),
        "available_land_m2": None if land_area_m2 is None else round(land_area_m2, 1),
        "dry_season_evaporation_loss_m3": None if evap is None else round(evap, 1),
        "evaporation_loss_pct_of_storage": round(100 * evap / volume, 1) if evap is not None and volume else None,
        "seasonal_seepage_loss_m3": round(seepage, 1),
        "fills_per_dependable_year": round(fills_per_year, 2),
        "mean_year_inflow_to_storage_ratio": round(mean_runoff_m3 / volume, 2) if volume else None,
        "notes": notes,
    }


def footprint_polygon(dem, site_rc: tuple[int, int], length: float, width: float) -> list[list[float]]:
    """Rectangle (lon/lat ring) centred on the site, long side along the contour."""
    dzdy, dzdx = np.gradient(dem.z, dem.cell, dem.cell)
    gx, gy = float(dzdx[site_rc]), float(dzdy[site_rc])
    if math.hypot(gx, gy) < 1e-9:
        ux, uy = 1.0, 0.0
    else:
        # Contour direction is perpendicular to the gradient.
        ux, uy = -gy / math.hypot(gx, gy), gx / math.hypot(gx, gy)
    vx, vy = -uy, ux
    cx = dem.x0 + site_rc[1] * dem.cell
    cy = dem.y0 + site_rc[0] * dem.cell
    hl, hw = length / 2.0, width / 2.0
    corners = [(-hl, -hw), (hl, -hw), (hl, hw), (-hl, hw), (-hl, -hw)]
    xs = np.array([cx + a * ux + b * vx for a, b in corners])
    ys = np.array([cy + a * uy + b * vy for a, b in corners])
    lon, lat = dem.proj.inverse(xs, ys)
    return [[round(float(a), 7), round(float(b), 7)] for a, b in zip(lon, lat)]
