"""
pipeline.py
-----------
The full pond-planning analysis on a DEM, independent of where the DEM came
from (an uploaded contour map or an elevation service):

    terrain derivatives -> flow routing -> land availability -> pond site
    -> catchment -> rainfall -> runoff -> pond design -> map layers
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import db, layers
from .design import footprint_polygon, recommend_pond
from .hydrology import (
    d8_flow_direction,
    delineate_catchment,
    edge_draining_cells,
    fill_depressions,
    flow_accumulation,
    trace_boundary,
)
from .landcover import assess_land
from .pond import estimate_storage, rank_sites, select_pond_site, snap_to_drainage
from .rainfall import RainfallError, fetch_daily, summarise
from .runoff import catchment_landcover, estimate_runoff
from .terrain import DEM, slope_and_curvature


@dataclass
class Options:
    village: str | None = None
    soil_group: str = "C"
    fill_fraction: float = 0.5
    side_slope: float = 1.5
    max_depth_m: float = 4.5
    rainfall_years: int = 20
    rainfall_source: str = "auto"
    annual_normal_mm: float | None = None
    max_slope_deg: float = 8.0
    dam_height_m: float = 3.0
    runoff_coefficient: float | None = None
    fallback_rainfall_mm: float = 800.0
    parcels: dict | None = None
    pond_lonlat: tuple[float, float] | None = None
    require_complete_catchment: bool = True
    n_candidates: int = 5
    candidate_separation_m: float = 250.0
    analyze_land: bool = True
    fetch_rainfall: bool = True
    save: bool = True
    timings: dict = field(default_factory=dict)


def _tick(opts: Options, name: str, t0: float) -> float:
    now = time.perf_counter()
    opts.timings[name] = round(now - t0, 3)
    return now


def _catchment_geometry(dem: DEM, mask: np.ndarray):
    ring = trace_boundary(mask)
    if not ring:
        return [], 0.0
    rows = np.array([p[0] for p in ring], dtype=float)
    cols = np.array([p[1] for p in ring], dtype=float)
    blon, blat = dem.corner_to_lonlat(rows, cols)
    coords = [[round(float(a), 8), round(float(b), 8)] for a, b in zip(blon, blat)]
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    px = dem.x0 + (cols - 0.5) * dem.cell
    py = dem.y0 + (rows - 0.5) * dem.cell
    return coords, float(np.sum(np.hypot(np.diff(px), np.diff(py))))


def run(dem: DEM, dem_info: dict, *, mode: str, source_meta: dict, opts: Options,
        contour_layer: dict | None = None) -> dict:
    started = time.perf_counter()
    t = started
    warnings: list[str] = []

    # ---- 1. Terrain + flow routing ---------------------------------------
    slope, curvature = slope_and_curvature(dem)
    filled = fill_depressions(dem.z)
    direction = d8_flow_direction(filled)
    accum = flow_accumulation(filled, direction)
    edge_inflow = edge_draining_cells(filled, direction)
    t = _tick(opts, "terrain_and_flow", t)

    # ---- 2. Land availability ---------------------------------------------
    land = None
    if opts.analyze_land:
        land = assess_land(dem, slope, parcels=opts.parcels, max_slope_deg=opts.max_slope_deg)
        warnings += land["warnings"]
    t = _tick(opts, "land_assessment", t)
    allowed = land["allowed"] if land else None

    # ---- 3. Candidate pond sites -----------------------------------------
    score = None
    if opts.pond_lonlat:
        r, c = dem.lonlat_to_cell(*opts.pond_lonlat)
        r, c = int(round(float(r))), int(round(float(c)))
        if not (0 <= r < dem.nrows and 0 <= c < dem.ncols):
            raise ValueError("Chosen pond location is outside the analysis area.")
        radius = max(1, int(round(60.0 / dem.cell)))
        site = snap_to_drainage(accum, (r, c), radius, allowed)
        site_diag = {"mode": "user_selected", "snapped_within_m": 60,
                     "snapped_distance_m": round(float(np.hypot(site[0] - r, site[1] - c) * dem.cell), 1)}
        # Still score the terrain so automatic alternatives can be offered.
        _, auto_diag, score = select_pond_site(
            dem.z, slope, curvature, accum,
            allowed=allowed, land_score=land["land_score"] if land else None,
            complete=~edge_inflow if opts.require_complete_catchment else None,
        )
    else:
        site, site_diag, score = select_pond_site(
            dem.z, slope, curvature, accum,
            allowed=allowed, land_score=land["land_score"] if land else None,
            complete=~edge_inflow if opts.require_complete_catchment else None,
        )
        site_diag["mode"] = "automatic"
        if land and not site_diag.get("restricted_to_available_land"):
            warnings.append("No available land lies on a drainage line; the site was chosen on terrain alone.")
        elif land and site_diag.get("relaxed_threshold"):
            warnings.append("Available land only reaches minor drainage lines here, so the catchment is small. "
                            "Mark additional government land or pick a location manually.")

    min_sep = max(3, int(round(opts.candidate_separation_m / dem.cell)))
    sites = rank_sites(score, direction, n=opts.n_candidates, min_separation_cells=min_sep, first=site)
    t = _tick(opts, "site_ranking", t)

    # ---- 4. Rainfall (once, at the top site) ----------------------------------
    site_lon, site_lat = (float(v) for v in dem.cell_to_lonlat(site[0], site[1]))
    daily, rain_stats = None, None
    if opts.fetch_rainfall:
        try:
            daily = fetch_daily(site_lat, site_lon, opts.rainfall_years,
                                source=opts.rainfall_source, annual_normal_mm=opts.annual_normal_mm)
            rain_stats = summarise(daily)
            if daily.get("fallback_errors"):
                rain_stats["fallback_errors"] = daily["fallback_errors"]
        except RainfallError as exc:
            warnings.append(f"{exc}. Using the fallback annual rainfall of {opts.fallback_rainfall_mm} mm.")
    t = _tick(opts, "rainfall", t)

    # ---- 5. Catchment, runoff and pond design for every candidate ------------
    land_labels = None
    if allowed is not None:
        _, land_labels = cv2.connectedComponents(allowed.astype(np.uint8), connectivity=8)
    ctx = dict(dem=dem, slope=slope, accum=accum, direction=direction, filled=filled,
               edge_inflow=edge_inflow, land=land, allowed=allowed, land_labels=land_labels,
               daily=daily, rain_stats=rain_stats, opts=opts)
    candidates = []
    for rank, rc in enumerate(sites, start=1):
        cand = _evaluate_site(rc, **ctx)
        cand["rank"] = rank
        cand["score"] = None if not np.isfinite(score[rc]) else round(float(score[rc]), 3)
        if rank == 1 and site_diag["mode"] == "user_selected":
            cand["pond_site"]["reason"] = ("Location chosen by the user, snapped to the strongest drainage "
                                           "line within 60 m so the pond intercepts runoff.")
        else:
            cand["pond_site"]["reason"] = (
                f"Rank {rank}: combined score {cand['score']:.2f} for flow accumulation, gentle slope, "
                "terrain concavity" + (" and land availability" if land else "")
                + "; catchment independent of the higher-ranked sites."
                if cand["score"] is not None else "Terrain-only fallback site.")
        candidates.append(cand)
    top = candidates[0]
    if top["catchment"]["touches_analysis_edge"]:
        warnings.append("Part of the catchment drains in from beyond the edge of the map, so its area is "
                        "underestimated." + (" Increase the analysis radius." if mode == "village" else
                                             " Use a contour map that covers the whole upstream area."))
    if allowed is not None and not top["pond_site"]["on_available_land"]:
        warnings.append("The pond site is not on land classified as available.")
    t = _tick(opts, "catchments_and_design", t)

    # ---- 6. Map layers -----------------------------------------------------------
    map_layers = {
        "bounds": layers.image_bounds(dem),
        "contours": contour_layer or layers.dem_contours(dem),
        "drainage": layers.drainage_network(dem, direction, accum),
        "elevation_png": layers.elevation_png(dem),
        "pond_footprint": top["pond_footprint"],
    }
    if land:
        map_layers["suitable_land"] = layers.land_polygons(dem, land["tier"])
        if land["fractions"] is not None:
            map_layers["landcover_png"] = layers.landcover_png(land["fractions"])
    t = _tick(opts, "layers", t)

    runoff = top["runoff"]
    water_yield = {
        "annual_rainfall_mm": runoff["mean_annual_rainfall_mm"],
        "runoff_coefficient": runoff.get("effective_runoff_coefficient", runoff["rational_runoff_coefficient"]),
        "estimated_annual_runoff_m3": runoff["mean_annual_runoff_m3"],
        "method": runoff["method"],
    }
    result = {
        "status": "success",
        "mode": mode,
        "village": opts.village,
        "source_map": source_meta,
        "dem": dem_info,
        "pond_site": top["pond_site"],
        "catchment": top["catchment"],
        "land": land["meta"] if land else None,
        "rainfall": rain_stats,
        "runoff": runoff,
        "pond_design": top["pond_design"],
        "storage": top["storage"],
        "water_yield": water_yield,
        "candidates": [
            {k: v for k, v in c.items() if k in ("rank", "score", "pond_site", "catchment", "runoff",
                                                 "pond_design", "pond_footprint")}
            for c in candidates
        ],
        "layers": map_layers,
        "warnings": warnings,
        "method": {
            "dem_interpolation": dem_info.get("method", "see source_map"),
            "depression_filling": "Priority-Flood (Barnes et al. 2014)",
            "flow_routing": "D8 steepest descent",
            "catchment_delineation": "Reverse traversal of the D8 flow graph from the outlet",
            "land_classification": "OpenCV: GRVI + Otsu vegetation, texture-density built-up, OSM exclusions",
            "runoff": runoff["method"],
            "pond_sizing": top["pond_design"]["design_basis"],
            "pond_siting": site_diag,
            "candidate_ranking": {"requested": opts.n_candidates, "found": len(candidates),
                                  "min_separation_m": opts.candidate_separation_m,
                                  "rule": "best score first; independent catchments only"},
            "catchment_cells": top["catchment_cells"],
            "timings_s": opts.timings,
        },
        "processing_seconds": round(time.perf_counter() - started, 3),
    }

    if opts.save:
        summary = {
            "catchment_ha": top["catchment"]["area_hectares"],
            "mean_rainfall_mm": runoff["mean_annual_rainfall_mm"],
            "mean_runoff_m3": runoff["mean_annual_runoff_m3"],
            "storage_m3": top["pond_design"]["storage_capacity_m3"],
            "depth_m": top["pond_design"]["water_depth_m"],
            "pond_lat": top["pond_site"]["location"]["lat"],
            "pond_lon": top["pond_site"]["location"]["lon"],
            "candidates": len(candidates),
            "total_storage_m3": round(sum(c["pond_design"]["storage_capacity_m3"] for c in candidates), 1),
        }
        result["analysis_id"] = db.save_analysis(
            village=opts.village, mode=mode, lat=site_lat, lon=site_lon, summary=summary, result=result,
        )
    return result


def _evaluate_site(site, *, dem, slope, accum, direction, filled, edge_inflow, land, allowed,
                   land_labels, daily, rain_stats, opts) -> dict:
    """Catchment, runoff, pond design and footprint for one pond site."""
    mask = delineate_catchment(direction, site)
    n_cells = int(mask.sum())
    area_m2 = n_cells * dem.cell_area
    coords, perimeter = _catchment_geometry(dem, mask)
    zc = dem.z[mask]
    rr, cc = np.nonzero(mask)
    cen_lon, cen_lat = dem.cell_to_lonlat(rr.mean(), cc.mean())
    site_lon, site_lat = (float(v) for v in dem.cell_to_lonlat(site[0], site[1]))

    cover = catchment_landcover(
        land["fractions"] if land else None,
        land["osm"]["forest"] if land and land["osm"] else None,
        mask,
    )
    runoff = estimate_runoff(daily, area_m2, cover, opts.soil_group,
                             fallback_rain_mm=opts.fallback_rainfall_mm, c_override=opts.runoff_coefficient)

    land_area = None
    if land_labels is not None and allowed[site]:
        land_area = float((land_labels == land_labels[site]).sum() * dem.cell_area)
    design = recommend_pond(
        dependable_runoff_m3=runoff["dependable_annual_runoff_m3"],
        mean_runoff_m3=runoff["mean_annual_runoff_m3"],
        land_area_m2=land_area,
        dry_season_et0_mm=rain_stats["mean_dry_season_et0_mm"] if rain_stats else None,
        soil_group=opts.soil_group,
        fill_fraction=opts.fill_fraction,
        side_slope=opts.side_slope,
        max_depth_m=opts.max_depth_m,
    )
    grow = 2 * opts.side_slope * design["freeboard_m"]
    ring = footprint_polygon(dem, site, design["top_length_m"] + grow, design["top_width_m"] + grow)

    return {
        "catchment_cells": n_cells,
        "pond_site": {
            "location": {"lon": round(site_lon, 8), "lat": round(site_lat, 8),
                         "elevation_m": round(float(dem.z[site]), 2)},
            "slope_deg": round(float(np.degrees(slope[site])), 2),
            "upstream_cells": int(accum[site]),
            "on_available_land": bool(allowed[site]) if allowed is not None else None,
            "land_tier": (["excluded", "farmland", "open_land", "preferred"][int(land["tier"][site])]
                          if land else None),
            "reason": "",
        },
        "catchment": {
            "area_m2": round(area_m2, 1),
            "area_hectares": round(area_m2 / 10_000.0, 3),
            "area_km2": round(area_m2 / 1_000_000.0, 5),
            "perimeter_m": round(perimeter, 1),
            "elevation_min_m": round(float(zc.min()), 2),
            "elevation_max_m": round(float(zc.max()), 2),
            "relief_m": round(float(zc.max() - zc.min()), 2),
            "mean_slope_deg": round(float(np.degrees(slope[mask].mean())), 2),
            "centroid": {"lon": round(float(cen_lon), 8), "lat": round(float(cen_lat), 8)},
            "touches_analysis_edge": bool(edge_inflow[site]),
            "boundary_geojson": {
                "type": "Feature",
                "properties": {"name": "catchment", "area_m2": round(area_m2, 1)},
                "geometry": {"type": "Polygon", "coordinates": [coords]},
            },
        },
        "runoff": runoff,
        "pond_design": design,
        "pond_footprint": {
            "type": "Feature",
            "properties": {"name": "recommended pond", "area_m2": design["footprint_area_m2"]},
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        },
        "storage": estimate_storage(filled, mask, site, dem.cell_area, opts.dam_height_m),
    }
