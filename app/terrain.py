"""
terrain.py
----------
Turns contour lines (lon/lat + elevation) into a DEM: a regular grid of
elevations in metres, plus the maths to convert grid cells back to lon/lat.

Two steps:
  1. Project geographic coords to a local metre-based XY frame.
  2. Interpolate the contour vertices onto a regular grid.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.interpolate import griddata

EARTH_RADIUS_M = 6_371_008.8


class LocalProjection:
    """
    Equirectangular projection about the map centroid.

    For a catchment-sized area (a few km) the distortion is far below the
    accuracy of the contour data itself, and it avoids a heavy GIS dependency.
    x runs east, y runs north, both in metres, origin at the centroid.
    """

    def __init__(self, lon0: float, lat0: float):
        self.lon0 = lon0
        self.lat0 = lat0
        self._mx = EARTH_RADIUS_M * math.cos(math.radians(lat0)) * math.pi / 180.0
        self._my = EARTH_RADIUS_M * math.pi / 180.0

    def forward(self, lon, lat):
        lon = np.asarray(lon, dtype=float)
        lat = np.asarray(lat, dtype=float)
        return (lon - self.lon0) * self._mx, (lat - self.lat0) * self._my

    def inverse(self, x, y):
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        return x / self._mx + self.lon0, y / self._my + self.lat0


class DEM:
    """A regular elevation grid. z[row, col]; row 0 is the SOUTH edge."""

    def __init__(self, z, x0, y0, cell, projection, nodata_mask):
        self.z = z
        self.x0 = x0            # metre coord of column 0 centre
        self.y0 = y0            # metre coord of row 0 centre
        self.cell = cell        # cell size in metres
        self.proj = projection
        self.nodata_mask = nodata_mask   # True where value was extrapolated
        self.nrows, self.ncols = z.shape

    @property
    def cell_area(self) -> float:
        return self.cell * self.cell

    def cell_to_lonlat(self, row, col):
        x = self.x0 + np.asarray(col, dtype=float) * self.cell
        y = self.y0 + np.asarray(row, dtype=float) * self.cell
        return self.proj.inverse(x, y)

    def lonlat_grid(self, oversample: int = 1):
        """lon/lat of every cell centre (or of an oversampled sub-grid)."""
        k = oversample
        cols = (np.arange(self.ncols * k) + 0.5) / k - 0.5
        rows = (np.arange(self.nrows * k) + 0.5) / k - 0.5
        cc, rr = np.meshgrid(cols, rows)
        return self.cell_to_lonlat(rr, cc)

    def lonlat_to_cell(self, lon, lat):
        """Fractional (row, col) of a lon/lat."""
        x, y = self.proj.forward(lon, lat)
        return (np.asarray(y) - self.y0) / self.cell, (np.asarray(x) - self.x0) / self.cell

    @property
    def bbox(self):
        """(min_lon, min_lat, max_lon, max_lat) of the grid's outer edges."""
        lon0, lat0 = self.corner_to_lonlat(0, 0)
        lon1, lat1 = self.corner_to_lonlat(self.nrows, self.ncols)
        return float(lon0), float(lat0), float(lon1), float(lat1)

    def corner_to_lonlat(self, row_edge, col_edge):
        """Cell CORNER (used when tracing the catchment outline)."""
        x = self.x0 + (np.asarray(col_edge, dtype=float) - 0.5) * self.cell
        y = self.y0 + (np.asarray(row_edge, dtype=float) - 0.5) * self.cell
        return self.proj.inverse(x, y)


def build_dem(contours: list[dict], target_cells: int = 260, max_cells: int = 420) -> tuple[DEM, dict]:
    """
    Interpolate contour vertices onto a regular grid.

    target_cells sets the resolution along the longer axis. It is capped so a
    huge map cannot blow up memory, and floored so a tiny map stays usable.
    """
    lons = np.concatenate([np.asarray([p[0] for p in c["coords"]]) for c in contours])
    lats = np.concatenate([np.asarray([p[1] for p in c["coords"]]) for c in contours])
    zs = np.concatenate([
        np.full(len(c["coords"]), c["elevation"], dtype=float) for c in contours
    ])

    proj = LocalProjection(float(lons.mean()), float(lats.mean()))
    px, py = proj.forward(lons, lats)

    xmin, xmax = float(px.min()), float(px.max())
    ymin, ymax = float(py.min()), float(py.max())
    width, height = xmax - xmin, ymax - ymin
    if width <= 0 or height <= 0:
        raise ValueError("Contour map has zero extent.")

    span = max(width, height)
    n = min(target_cells, max_cells)
    cell = span / n

    ncols = max(int(math.ceil(width / cell)) + 1, 8)
    nrows = max(int(math.ceil(height / cell)) + 1, 8)

    xs = xmin + np.arange(ncols) * cell
    ys = ymin + np.arange(nrows) * cell
    gx, gy = np.meshgrid(xs, ys)

    pts = np.column_stack([px, py])

    # Linear interpolation over the Delaunay triangulation of the contour
    # vertices. Outside the convex hull this returns NaN, so we fill those
    # cells with the nearest known value and flag them.
    z = griddata(pts, zs, (gx, gy), method="linear")
    holes = np.isnan(z)
    if holes.any():
        z[holes] = griddata(pts, zs, (gx[holes], gy[holes]), method="nearest")

    stats = {
        "grid_rows": nrows,
        "grid_cols": ncols,
        "cell_size_m": round(cell, 3),
        "extrapolated_cell_fraction": round(float(holes.mean()), 4),
        "extent_m": {"width": round(width, 1), "height": round(height, 1)},
        "bbox_lonlat": {
            "min_lon": float(lons.min()), "min_lat": float(lats.min()),
            "max_lon": float(lons.max()), "max_lat": float(lats.max()),
        },
    }
    return DEM(z.astype(float), xmin, ymin, cell, proj, holes), stats


def empty_grid(lon0: float, lat0: float, radius_m: float, target_cells: int) -> DEM:
    """
    A square grid centred on (lon0, lat0) with no elevations yet. Used when the
    terrain comes from an elevation service instead of an uploaded contour map.
    """
    proj = LocalProjection(lon0, lat0)
    n = int(max(40, min(target_cells, 420)))
    cell = 2.0 * radius_m / n
    start = -radius_m + cell / 2.0
    z = np.zeros((n, n), dtype=float)
    return DEM(z, start, start, cell, proj, np.zeros(z.shape, dtype=bool))


def dem_stats(dem: DEM, extra: dict | None = None) -> dict:
    min_lon, min_lat, max_lon, max_lat = dem.bbox
    stats = {
        "grid_rows": dem.nrows,
        "grid_cols": dem.ncols,
        "cell_size_m": round(dem.cell, 3),
        "extrapolated_cell_fraction": round(float(dem.nodata_mask.mean()), 4),
        "extent_m": {"width": round(dem.ncols * dem.cell, 1), "height": round(dem.nrows * dem.cell, 1)},
        "bbox_lonlat": {"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat},
        "elevation_min_m": round(float(dem.z.min()), 2),
        "elevation_max_m": round(float(dem.z.max()), 2),
    }
    if extra:
        stats.update(extra)
    return stats


def slope_and_curvature(dem: DEM):
    """
    slope   : radians, from the steepest local gradient
    curvature: Laplacian of elevation. Negative = concave = bowl/valley,
               which is exactly what a pond wants to sit in.
    """
    dzdy, dzdx = np.gradient(dem.z, dem.cell, dem.cell)
    slope = np.arctan(np.hypot(dzdx, dzdy))

    z = dem.z
    lap = np.zeros_like(z)
    lap[1:-1, 1:-1] = (
        z[:-2, 1:-1] + z[2:, 1:-1] + z[1:-1, :-2] + z[1:-1, 2:] - 4.0 * z[1:-1, 1:-1]
    ) / (dem.cell ** 2)
    return slope, lap
