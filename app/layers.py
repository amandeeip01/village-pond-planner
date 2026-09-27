"""
layers.py
---------
Converts analysis grids into map layers the frontend can overlay:

  contours         GeoJSON isolines generated from the DEM (OpenCV findContours
                   on thresholded, upsampled elevation) or taken from the KML
  drainage         GeoJSON stream network traced along D8 flow directions
  suitable_land    GeoJSON polygons of land available for excavation, by tier
  elevation_png    colour-relief + hillshade image overlay
  landcover_png    satellite land-cover classification image overlay
"""

from __future__ import annotations

import base64
import math

import cv2
import numpy as np

from .hydrology import NEIGHBOURS
from .landcover import landcover_image
from .terrain import DEM


def _png_data_url(img: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise ValueError("PNG encoding failed")
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def image_bounds(dem: DEM) -> list[list[float]]:
    min_lon, min_lat, max_lon, max_lat = dem.bbox
    return [[min_lat, min_lon], [max_lat, max_lon]]


def _cells_to_lonlat(dem: DEM, rows, cols) -> list[list[float]]:
    lon, lat = dem.cell_to_lonlat(np.asarray(rows, float), np.asarray(cols, float))
    return [[round(float(a), 6), round(float(b), 6)] for a, b in zip(np.atleast_1d(lon), np.atleast_1d(lat))]


def nice_interval(relief: float, target_levels: int = 20) -> float:
    raw = max(relief / target_levels, 0.5)
    for step in (1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500):
        if step >= raw:
            return float(step)
    return 1000.0


# --------------------------------------------------------------------------
# Contours
# --------------------------------------------------------------------------
def dem_contours(dem: DEM, interval: float | None = None, upsample: int = 3) -> dict:
    z = dem.z
    relief = float(z.max() - z.min())
    interval = interval or nice_interval(relief)
    zu = cv2.resize(z.astype(np.float32), (dem.ncols * upsample, dem.nrows * upsample),
                    interpolation=cv2.INTER_CUBIC)
    zu = cv2.GaussianBlur(zu, (0, 0), upsample * 0.6)
    h, w = zu.shape
    first = math.ceil(float(z.min()) / interval) * interval
    features = []
    level = first
    while level <= float(z.max()):
        mask = (zu >= level).astype(np.uint8)
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
        lines = []
        for cnt in contours:
            pts = cnt[:, 0, :]
            # Drop points lying on the image border - they are the edge of the
            # map, not part of the isoline - and split the line there.
            on_edge = (pts[:, 0] == 0) | (pts[:, 0] == w - 1) | (pts[:, 1] == 0) | (pts[:, 1] == h - 1)
            segment = []
            for p, e in zip(pts, on_edge):
                if e:
                    if len(segment) > 3:
                        lines.append(np.array(segment))
                    segment = []
                else:
                    segment.append(p)
            if len(segment) > 3:
                closed = not on_edge.any()
                seg = np.array(segment + ([segment[0]] if closed else []))
                lines.append(seg)
        coords = []
        for seg in lines:
            simp = cv2.approxPolyDP(seg.reshape(-1, 1, 2).astype(np.int32), 1.0, False)[:, 0, :]
            if len(simp) < 2:
                continue
            cols = (simp[:, 0] + 0.5) / upsample - 0.5
            rows = (simp[:, 1] + 0.5) / upsample - 0.5
            coords.append(_cells_to_lonlat(dem, rows, cols))
        if coords:
            idx = round(level / interval)
            features.append({
                "type": "Feature",
                "properties": {"elevation_m": round(level, 2), "major": idx % 5 == 0},
                "geometry": {"type": "MultiLineString", "coordinates": coords},
            })
        level += interval
    return {"type": "FeatureCollection", "properties": {"interval_m": interval, "source": "derived_from_dem"},
            "features": features}


def kml_contours(contours: list[dict], max_points: int = 40_000) -> dict:
    total = sum(len(c["coords"]) for c in contours)
    step = max(1, math.ceil(total / max_points))
    by_level: dict[float, list] = {}
    for c in contours:
        pts = c["coords"][::step]
        if c["coords"] and pts[-1] != c["coords"][-1]:
            pts = pts + [c["coords"][-1]]
        if len(pts) < 2:
            continue
        by_level.setdefault(float(c["elevation"]), []).append(
            [[round(p[0], 6), round(p[1], 6)] for p in pts])
    levels = sorted(by_level)
    interval = float(np.median(np.diff(levels))) if len(levels) > 1 else 0.0
    feats = []
    for lv in levels:
        idx = round(lv / interval) if interval else 0
        feats.append({
            "type": "Feature",
            "properties": {"elevation_m": lv, "major": interval > 0 and idx % 5 == 0},
            "geometry": {"type": "MultiLineString", "coordinates": by_level[lv]},
        })
    return {"type": "FeatureCollection", "properties": {"interval_m": interval, "source": "uploaded_kml"},
            "features": feats}


# --------------------------------------------------------------------------
# Drainage network
# --------------------------------------------------------------------------
def drainage_network(dem: DEM, direction: np.ndarray, accum: np.ndarray,
                     min_area_m2: float = 50_000.0) -> dict:
    nrows, ncols = accum.shape
    threshold = max(min_area_m2 / dem.cell_area, float(np.percentile(accum, 97)))
    stream = accum >= threshold

    def downstream(r, c):
        k = direction[r, c]
        if k < 0:
            return None
        dr, dc, _ = NEIGHBOURS[k]
        nr, nc = r + dr, c + dc
        if 0 <= nr < nrows and 0 <= nc < ncols:
            return nr, nc
        return None

    has_upstream = np.zeros(accum.shape, dtype=bool)
    for r, c in zip(*np.nonzero(stream)):
        d = downstream(r, c)
        if d and stream[d]:
            has_upstream[d] = True

    visited = np.zeros(accum.shape, dtype=bool)
    heads = [(r, c) for r, c in zip(*np.nonzero(stream & ~has_upstream))]
    heads.sort(key=lambda rc: -accum[rc])
    feats = []
    for head in heads:
        path = [head]
        cur = head
        visited[cur] = True
        while True:
            nxt = downstream(*cur)
            if nxt is None or not stream[nxt]:
                break
            path.append(nxt)
            if visited[nxt]:
                break
            visited[nxt] = True
            cur = nxt
        if len(path) < 2:
            continue
        rows, cols = zip(*path)
        feats.append({
            "type": "Feature",
            "properties": {"upstream_area_ha": round(float(accum[path[-1]] * dem.cell_area / 10_000), 2)},
            "geometry": {"type": "LineString", "coordinates": _cells_to_lonlat(dem, rows, cols)},
        })
    return {"type": "FeatureCollection", "properties": {"min_upstream_area_m2": round(threshold * dem.cell_area, 1)},
            "features": feats}


# --------------------------------------------------------------------------
# Suitable land polygons
# --------------------------------------------------------------------------
TIER_NAMES = {3: "preferred", 2: "suitable", 1: "farmland"}


def land_polygons(dem: DEM, tier: np.ndarray, min_cells: int = 4) -> dict:
    feats = []
    for code, name in TIER_NAMES.items():
        mask = (tier == code).astype(np.uint8)
        if not mask.any():
            continue
        contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if hierarchy is None:
            continue
        hierarchy = hierarchy[0]
        for i, cnt in enumerate(contours):
            if hierarchy[i][3] != -1 or cv2.contourArea(cnt) < min_cells:
                continue   # holes are attached to their parent below
            rings = [cnt]
            child = hierarchy[i][2]
            while child != -1:
                if cv2.contourArea(contours[child]) >= min_cells:
                    rings.append(contours[child])
                child = hierarchy[child][0]
            coords = []
            for ring in rings:
                pts = cv2.approxPolyDP(ring, 0.7, True)[:, 0, :]
                if len(pts) < 3:
                    continue
                ll = _cells_to_lonlat(dem, pts[:, 1], pts[:, 0])
                coords.append(ll + [ll[0]])
            if coords:
                feats.append({
                    "type": "Feature",
                    "properties": {"tier": name, "area_m2": round(float(cv2.contourArea(cnt)) * dem.cell_area, 1)},
                    "geometry": {"type": "Polygon", "coordinates": coords},
                })
    return {"type": "FeatureCollection", "features": feats}


# --------------------------------------------------------------------------
# Raster overlays
# --------------------------------------------------------------------------
def elevation_png(dem: DEM) -> str:
    z = dem.z
    # x = east, y = north (row 0 is the south edge). Light from the north-west
    # at 45 degrees elevation; shade = surface normal . light direction.
    dzdy, dzdx = np.gradient(z, dem.cell, dem.cell)
    exaggeration = 2.0
    nx, ny, nz = -dzdx * exaggeration, -dzdy * exaggeration, np.ones_like(z)
    norm = np.sqrt(nx ** 2 + ny ** 2 + nz ** 2)
    alt = np.radians(45.0)
    lx, ly, lz = -np.cos(alt) / math.sqrt(2), np.cos(alt) / math.sqrt(2), np.sin(alt)
    shade = np.clip((nx * lx + ny * ly + nz * lz) / norm, 0, 1)
    norm = ((z - z.min()) / max(float(z.max() - z.min()), 1e-6) * 255).astype(np.uint8)
    lut = _terrain_lut()
    colour = lut[norm]
    img = (colour.astype(np.float32) * (0.45 + 0.55 * shade[..., None])).clip(0, 255).astype(np.uint8)
    return _png_data_url(cv2.flip(img, 0))


def _terrain_lut() -> np.ndarray:
    """256-entry BGR lookup: green lowlands -> yellow -> brown -> white peaks."""
    stops = [(0, (96, 160, 40)), (70, (110, 200, 150)), (130, (120, 220, 235)),
             (190, (70, 130, 190)), (255, (245, 245, 250))]
    lut = np.zeros((256, 3), dtype=np.uint8)
    for (i0, c0), (i1, c1) in zip(stops, stops[1:]):
        for i in range(i0, i1 + 1):
            t = (i - i0) / max(i1 - i0, 1)
            lut[i] = [round(c0[k] + t * (c1[k] - c0[k])) for k in range(3)]
    return lut


def landcover_png(fractions: np.ndarray) -> str:
    return _png_data_url(landcover_image(fractions))
