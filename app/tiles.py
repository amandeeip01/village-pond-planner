"""
tiles.py
--------
Fetches slippy-map (XYZ) raster tiles, stitches them into one mosaic and
resamples that mosaic onto the analysis grid.

Two tile sources are used:

  * AWS Terrain Tiles ("terrarium" encoding) - global elevation, no API key.
    Elevation (m) = R * 256 + G + B / 256 - 32768
  * Esri World Imagery - satellite RGB used for land-cover classification.

Tiles are decoded and resampled with OpenCV. Downloaded tiles are cached on
disk so repeat analyses of the same village do not hit the servers again.
"""

from __future__ import annotations

import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import httpx
import numpy as np

CACHE_DIR = Path(os.environ.get("TILE_CACHE_DIR", Path(__file__).resolve().parent.parent / "cache" / "tiles"))
USER_AGENT = "VillagePondPlanner/2.0 (academic project)"
TILE_SIZE = 256
MAX_TILES = 100

SOURCES = {
    "terrarium": {
        "url": "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
        "max_zoom": 15,
        "ext": "png",
    },
    "esri_imagery": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "max_zoom": 18,
        "ext": "jpg",
    },
}


class TileError(RuntimeError):
    """Raised when tiles cannot be downloaded or decoded."""


# --------------------------------------------------------------------------
# Web-Mercator maths
# --------------------------------------------------------------------------
def lonlat_to_pixel(lon, lat, z: int):
    """Global pixel coordinates (floating) at zoom z."""
    lon = np.asarray(lon, dtype=float)
    lat = np.clip(np.asarray(lat, dtype=float), -85.05112878, 85.05112878)
    n = TILE_SIZE * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    s = np.sin(np.radians(lat))
    y = (0.5 - np.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def ground_resolution_m(lat: float, z: int) -> float:
    """Metres per pixel at a latitude and zoom."""
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** z)


def pick_zoom(lat: float, target_m_per_px: float, max_zoom: int, bbox, max_tiles: int = MAX_TILES) -> int:
    """Coarsest zoom that still resolves `target_m_per_px`, limited by tile count."""
    z = max_zoom
    for cand in range(1, max_zoom + 1):
        if ground_resolution_m(lat, cand) <= target_m_per_px:
            z = cand
            break
    while z > 1 and _tile_count(bbox, z) > max_tiles:
        z -= 1
    return z


def _tile_range(bbox, z):
    min_lon, min_lat, max_lon, max_lat = bbox
    x0, y1 = lonlat_to_pixel(min_lon, min_lat, z)
    x1, y0 = lonlat_to_pixel(max_lon, max_lat, z)
    tx0, tx1 = int(x0 // TILE_SIZE), int(x1 // TILE_SIZE)
    ty0, ty1 = int(y0 // TILE_SIZE), int(y1 // TILE_SIZE)
    return tx0, tx1, ty0, ty1


def _tile_count(bbox, z):
    tx0, tx1, ty0, ty1 = _tile_range(bbox, z)
    return (tx1 - tx0 + 1) * (ty1 - ty0 + 1)


# --------------------------------------------------------------------------
# Download + mosaic
# --------------------------------------------------------------------------
def _fetch_tile(client: httpx.Client, source: str, z: int, x: int, y: int) -> bytes:
    spec = SOURCES[source]
    path = CACHE_DIR / source / str(z) / str(x) / f"{y}.{spec['ext']}"
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    url = spec["url"].format(z=z, x=x, y=y)
    last_exc = None
    # Back off between attempts: on flaky networks DNS lookups and connections
    # fail transiently, and one missing tile would abort the whole analysis.
    for attempt in range(5):
        if attempt:
            time.sleep(0.5 * attempt)
        try:
            resp = client.get(url)
            if resp.status_code == 200 and resp.content:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(resp.content)
                return resp.content
            last_exc = TileError(f"{source} tile {z}/{x}/{y}: HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            last_exc = exc
    raise TileError(f"Could not download {source} tile {z}/{x}/{y}: {last_exc}")


def fetch_mosaic(source: str, bbox, z: int, flags=cv2.IMREAD_COLOR):
    """
    Download every tile covering bbox at zoom z and stitch them.

    Returns (mosaic_bgr, origin_px) where origin_px is the global pixel
    coordinate of the mosaic's top-left corner.
    """
    tx0, tx1, ty0, ty1 = _tile_range(bbox, z)
    n_tiles = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
    if n_tiles > MAX_TILES * 2:
        raise TileError(f"Area too large: {n_tiles} tiles at zoom {z}.")

    coords = [(x, y) for y in range(ty0, ty1 + 1) for x in range(tx0, tx1 + 1)]
    with httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=8) as pool:
            blobs = list(pool.map(lambda xy: _fetch_tile(client, source, z, xy[0], xy[1]), coords))

    h = (ty1 - ty0 + 1) * TILE_SIZE
    w = (tx1 - tx0 + 1) * TILE_SIZE
    mosaic = np.zeros((h, w, 3), dtype=np.uint8)
    for (x, y), blob in zip(coords, blobs):
        img = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), flags)
        if img is None:
            raise TileError(f"Could not decode {source} tile {z}/{x}/{y}.")
        if img.shape[:2] != (TILE_SIZE, TILE_SIZE):
            img = cv2.resize(img, (TILE_SIZE, TILE_SIZE), interpolation=cv2.INTER_AREA)
        oy, ox = (y - ty0) * TILE_SIZE, (x - tx0) * TILE_SIZE
        mosaic[oy:oy + TILE_SIZE, ox:ox + TILE_SIZE] = img[:, :, :3]
    return mosaic, (tx0 * TILE_SIZE, ty0 * TILE_SIZE)


def sample_mosaic(mosaic: np.ndarray, origin_px, z: int, lon: np.ndarray, lat: np.ndarray,
                  interpolation=cv2.INTER_LINEAR) -> np.ndarray:
    """Resample the mosaic at arbitrary lon/lat arrays (same shape) using cv2.remap."""
    px, py = lonlat_to_pixel(lon, lat, z)
    map_x = (px - origin_px[0]).astype(np.float32)
    map_y = (py - origin_px[1]).astype(np.float32)
    return cv2.remap(mosaic, map_x, map_y, interpolation, borderMode=cv2.BORDER_REPLICATE)


def decode_terrarium(bgr: np.ndarray) -> np.ndarray:
    """Terrarium RGB -> elevation in metres."""
    b = bgr[..., 0].astype(np.float64)
    g = bgr[..., 1].astype(np.float64)
    r = bgr[..., 2].astype(np.float64)
    return r * 256.0 + g + b / 256.0 - 32768.0
