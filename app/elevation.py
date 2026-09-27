"""
elevation.py
------------
Builds a DEM for any village from a public elevation service, so the planner
works without an uploaded contour map.

Primary source: AWS Terrain Tiles (terrarium), a global mosaic of SRTM,
Copernicus/GMTED and national datasets at ~30 m in India. Tiles are decoded to
metres, then resampled onto the analysis grid with bilinear interpolation.

Fallback: the Open-Meteo elevation API (Copernicus GLO-90), sampled on a
coarser lattice and interpolated up to the grid.
"""

from __future__ import annotations

import cv2
import httpx
import numpy as np
from scipy.interpolate import RegularGridInterpolator

from .terrain import DEM, empty_grid
from .tiles import (
    SOURCES,
    TileError,
    USER_AGENT,
    decode_terrarium,
    fetch_mosaic,
    pick_zoom,
    sample_mosaic,
)


class ElevationError(RuntimeError):
    pass


def _from_terrarium(dem: DEM) -> tuple[np.ndarray, dict]:
    bbox = dem.bbox
    lat0 = dem.proj.lat0
    z = pick_zoom(lat0, dem.cell / 2.0, SOURCES["terrarium"]["max_zoom"], bbox, max_tiles=36)
    mosaic, origin = fetch_mosaic("terrarium", bbox, z, flags=cv2.IMREAD_COLOR)
    # Decode BEFORE resampling: interpolating the packed RGB bytes would mix
    # the high and low bytes of neighbouring pixels and produce garbage.
    elev = decode_terrarium(mosaic).astype(np.float32)
    lon, lat = dem.lonlat_grid()
    grid = sample_mosaic(elev, origin, z, lon, lat, interpolation=cv2.INTER_LINEAR).astype(float)
    return grid, {"elevation_source": "AWS Terrain Tiles (terrarium; SRTM/Copernicus)", "tile_zoom": z}


def _from_open_meteo(dem: DEM, lattice: int = 40) -> tuple[np.ndarray, dict]:
    rows = np.linspace(0, dem.nrows - 1, lattice)
    cols = np.linspace(0, dem.ncols - 1, lattice)
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    lon, lat = dem.cell_to_lonlat(rr.ravel(), cc.ravel())
    values = []
    with httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT}) as client:
        for i in range(0, lon.size, 100):
            resp = client.get(
                "https://api.open-meteo.com/v1/elevation",
                params={
                    "latitude": ",".join(f"{v:.6f}" for v in lat[i:i + 100]),
                    "longitude": ",".join(f"{v:.6f}" for v in lon[i:i + 100]),
                },
            )
            if resp.status_code != 200:
                raise ElevationError(f"Open-Meteo elevation: HTTP {resp.status_code}")
            values.extend(resp.json()["elevation"])
    coarse = np.asarray(values, dtype=float).reshape(lattice, lattice)
    interp = RegularGridInterpolator((rows, cols), coarse, method="cubic")
    r_full, c_full = np.meshgrid(np.arange(dem.nrows), np.arange(dem.ncols), indexing="ij")
    grid = interp(np.column_stack([r_full.ravel(), c_full.ravel()])).reshape(dem.nrows, dem.ncols)
    return grid, {"elevation_source": "Open-Meteo elevation API (Copernicus GLO-90)", "lattice": lattice}


def fetch_dem(lon: float, lat: float, radius_m: float, target_cells: int) -> tuple[DEM, dict]:
    """A DEM centred on (lon, lat) covering a square of side 2 * radius_m."""
    dem = empty_grid(lon, lat, radius_m, target_cells)
    errors = []
    for fetcher in (_from_terrarium, _from_open_meteo):
        try:
            grid, meta = fetcher(dem)
        except (TileError, ElevationError, httpx.HTTPError, KeyError, ValueError) as exc:
            errors.append(f"{fetcher.__name__}: {exc}")
            continue
        if not np.isfinite(grid).all():
            errors.append(f"{fetcher.__name__}: non-finite elevations")
            continue
        dem.z = grid
        return dem, meta
    raise ElevationError("No elevation source available. " + " | ".join(errors))
