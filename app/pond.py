"""
pond.py
-------
Chooses where the pond should go, then sizes it.

Siting is a weighted score over three terrain properties, all derived from the
uploaded map — nothing about any specific location is baked in.
"""

from __future__ import annotations

import numpy as np

from .hydrology import NEIGHBOURS, delineate_catchment


def _normalise(a: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Scale values inside `valid` to 0..1. Flat input -> all zeros."""
    out = np.zeros_like(a, dtype=float)
    if not valid.any():
        return out
    v = a[valid]
    lo, hi = float(v.min()), float(v.max())
    if hi - lo < 1e-12:
        return out
    out[valid] = (a[valid] - lo) / (hi - lo)
    return out


def select_pond_site(
    z: np.ndarray,
    slope: np.ndarray,
    curvature: np.ndarray,
    accum: np.ndarray,
    *,
    allowed: np.ndarray | None = None,
    land_score: np.ndarray | None = None,
    complete: np.ndarray | None = None,
    exclude: np.ndarray | None = None,
    edge_buffer_frac: float = 0.06,
    stream_percentile: float = 92.0,
    weights: tuple[float, float, float] = (0.45, 0.35, 0.20),
    land_weight: float = 0.25,
) -> tuple[tuple[int, int], dict]:
    """
    A good village pond site is:
      1. On a drainage line, so water actually arrives      -> high accumulation
      2. On gentle ground, so excavation is stable and cheap -> low slope
      3. In a natural hollow, so it holds water             -> concave (curvature < 0)
      4. On land that is actually available                 -> `allowed` / `land_score`

    When `allowed` is given, only those cells are candidates; the drainage
    threshold is relaxed step by step if the available land does not touch
    the main drainage lines. If no available cell qualifies at all, the
    terrain-only choice is returned and flagged. Cells in `exclude` (the sea)
    are never candidates, not even in that fallback.

    Returns ((row, col), diagnostics, score) where score is the full score
    grid (-inf outside the candidate cells), used to rank alternative sites.
    """
    nrows, ncols = z.shape

    # Cells near the grid edge would have a catchment truncated by the map
    # boundary, so their area would be an underestimate. Exclude them.
    buf_r = max(2, int(nrows * edge_buffer_frac))
    buf_c = max(2, int(ncols * edge_buffer_frac))
    interior = np.zeros(z.shape, dtype=bool)
    interior[buf_r:nrows - buf_r, buf_c:ncols - buf_c] = True
    if exclude is not None:
        interior &= ~exclude
    # Prefer outlets whose whole catchment lies inside the map.
    complete_used = False
    if complete is not None and (interior & complete).sum() >= 10:
        interior &= complete
        complete_used = True

    use_land = allowed is not None and bool((allowed & interior).any())
    pool = interior & allowed if use_land else interior

    # "Stream" cells: the top few percent by flow accumulation, relaxed in
    # steps if the eligible land does not reach the drainage network.
    relaxed = False
    for pct in (stream_percentile, 85.0, 75.0, 60.0):
        threshold = np.percentile(accum[interior], pct)
        candidates = pool & (accum >= threshold)
        if candidates.sum() >= 10:
            break
        relaxed = True
    if candidates.sum() == 0:
        use_land = False
        threshold = np.percentile(accum[interior], stream_percentile)
        candidates = interior & (accum >= threshold)

    w_acc, w_slope, w_curv = weights
    s_acc = _normalise(np.log1p(accum), candidates)
    s_slope = 1.0 - _normalise(slope, candidates)
    s_curv = _normalise(-curvature, candidates)          # concave scores high

    score = np.full(z.shape, -np.inf)
    score[candidates] = (
        w_acc * s_acc[candidates]
        + w_slope * s_slope[candidates]
        + w_curv * s_curv[candidates]
    )
    applied_land_weight = 0.0
    if use_land and land_score is not None:
        applied_land_weight = land_weight
        score[candidates] = (1 - land_weight) * score[candidates] + land_weight * land_score[candidates]

    idx = int(np.argmax(score))
    site = (idx // ncols, idx % ncols)

    diagnostics = {
        "candidate_cells": int(candidates.sum()),
        "stream_threshold_cells": float(threshold),
        "relaxed_threshold": relaxed,
        "restricted_to_available_land": use_land,
        "catchment_fully_inside_map": complete_used,
        "site_score": float(score[site]),
        "score_weights": {
            "flow_accumulation": w_acc, "gentle_slope": w_slope, "concavity": w_curv,
            "land_suitability_blend": applied_land_weight,
        },
    }
    return site, diagnostics, score


def rank_sites(score: np.ndarray, direction: np.ndarray, n: int = 5, min_separation_cells: int = 12,
               first: tuple[int, int] | None = None) -> list[tuple[int, int]]:
    """
    Up to `n` alternative pond sites, best first.

    Taking the n highest scores would return n neighbouring cells on the same
    stream. Instead a candidate is accepted only if it is at least
    `min_separation_cells` from every accepted site AND its catchment is
    independent of theirs (it is neither upstream nor downstream of an
    accepted site). Each recommended pond therefore harvests a different
    part of the landscape.
    """
    from .hydrology import delineate_catchment   # local import avoids a cycle at module load

    accepted: list[tuple[int, int]] = []
    masks: list[np.ndarray] = []

    def accept(rc):
        accepted.append(rc)
        masks.append(delineate_catchment(direction, rc))

    if first is not None:
        accept(first)
    finite = np.isfinite(score)
    order = np.argsort(np.where(finite, score, -np.inf), axis=None)[::-1][: int(finite.sum())]
    ncols = score.shape[1]
    for idx in order:
        if len(accepted) >= n:
            break
        rc = (int(idx) // ncols, int(idx) % ncols)
        if any((rc[0] - a[0]) ** 2 + (rc[1] - a[1]) ** 2 < min_separation_cells ** 2 for a in accepted):
            continue
        if any(m[rc] for m in masks):                      # upstream of an accepted site
            continue
        mask = delineate_catchment(direction, rc)
        if any(mask[a] for a in accepted):                 # downstream of an accepted site
            continue
        accepted.append(rc)
        masks.append(mask)
    return accepted


def snap_to_drainage(accum: np.ndarray, rc: tuple[int, int], radius_cells: int,
                     allowed: np.ndarray | None = None) -> tuple[int, int]:
    """Move a user-chosen point to the highest-accumulation cell nearby."""
    r0, c0 = rc
    nrows, ncols = accum.shape
    r_lo, r_hi = max(0, r0 - radius_cells), min(nrows, r0 + radius_cells + 1)
    c_lo, c_hi = max(0, c0 - radius_cells), min(ncols, c0 + radius_cells + 1)
    window = accum[r_lo:r_hi, c_lo:c_hi].astype(float).copy()
    rr, cc = np.mgrid[r_lo:r_hi, c_lo:c_hi]
    window[(rr - r0) ** 2 + (cc - c0) ** 2 > radius_cells ** 2] = -1
    if allowed is not None:
        sub = allowed[r_lo:r_hi, c_lo:c_hi]
        if sub.any():
            window[~sub] = -1
    k = int(np.argmax(window))
    return r_lo + k // window.shape[1], c_lo + k % window.shape[1]


def estimate_storage(
    z_filled: np.ndarray,
    mask: np.ndarray,
    site: tuple[int, int],
    cell_area: float,
    dam_height_m: float,
) -> dict:
    """
    Flood-fill the catchment upstream of the site up to (site elevation +
    dam height) to get the water spread area and gross storage volume.

    Two things matter here:

    * Restricted to the catchment mask, so water cannot leak downstream past
      the dam line.
    * Runs on the DEPRESSION-FILLED DEM, not the raw one. The raw interpolated
      surface contains artificial pits lying below the outlet; counting those
      as storage produces negative depths and inflates the volume.
    """
    nrows, ncols = z_filled.shape
    z = z_filled
    water_level = float(z[site]) + dam_height_m

    pooled = np.zeros(z.shape, dtype=bool)
    stack = [site]
    pooled[site] = True

    while stack:
        r, c = stack.pop()
        for dr, dc, _ in NEIGHBOURS:
            nr, nc = r + dr, c + dc
            if nr < 0 or nr >= nrows or nc < 0 or nc >= ncols:
                continue
            if pooled[nr, nc] or not mask[nr, nc]:
                continue
            if z[nr, nc] <= water_level:
                pooled[nr, nc] = True
                stack.append((nr, nc))

    depths = np.clip(water_level - z[pooled], 0.0, None)
    volume = float(np.sum(depths) * cell_area)
    spread_fraction = float(pooled.sum() / max(int(mask.sum()), 1))

    return {
        "dam_height_m": dam_height_m,
        "full_supply_level_m": round(water_level, 2),
        "water_spread_area_m2": round(float(pooled.sum() * cell_area), 1),
        "gross_storage_volume_m3": round(volume, 1),
        "mean_depth_m": round(float(depths.mean()) if depths.size else 0.0, 2),
        "max_depth_m": round(float(depths.max()) if depths.size else 0.0, 2),
        "pooled_cells": int(pooled.sum()),
        "spread_fraction_of_catchment": round(spread_fraction, 3),
        "note": (
            "Backwater extends over a large share of the catchment, which "
            "indicates a very flat valley floor; reduce dam_height_m for a "
            "realistic pond." if spread_fraction > 0.15 else
            "Gross storage at full supply level; excludes siltation and freeboard."
        ),
    }


def estimate_yield(catchment_area_m2: float, rainfall_mm: float, runoff_coefficient: float) -> dict:
    """
    Rational-method annual runoff:  Yield = Area x Rainfall x Runoff coefficient

    Rainfall and the coefficient are user inputs (they depend on climate and
    land cover, which a contour map cannot tell you), so they are exposed as
    API parameters with documented defaults rather than hidden constants.
    """
    volume = catchment_area_m2 * (rainfall_mm / 1000.0) * runoff_coefficient
    return {
        "annual_rainfall_mm": rainfall_mm,
        "runoff_coefficient": runoff_coefficient,
        "estimated_annual_runoff_m3": round(volume, 1),
        "method": "Rational method: A x P x C",
    }
