"""
landcover.py
------------
Decides which land inside the analysis area is available for excavating a pond.

Three independent evidence sources are combined on the analysis grid:

1. Satellite imagery (Esri World Imagery), classified with OpenCV into
   vegetation, open/bare land, built-up and water:
     * vegetation  - Excess-Green index (2g - r - b on chromatic coordinates),
                     threshold chosen per image with Otsu's method
     * built-up    - dense Canny edges (roofs, walls, roads) or bright,
                     unsaturated surfaces (concrete, metal roofs)
     * water       - dark pixels whose blue channel dominates red
     * open land   - everything else (fallow, bare soil, scrub, grass)
   The class masks are cleaned with morphological opening/closing.

2. OpenStreetMap (Overpass API): buildings, roads, railways, rivers, water
   bodies, forest, settlements and institutions are hard exclusions, with a
   safety buffer. Tags that usually mark common or public land (grassland,
   scrub, village green, meadow, government/public ownership) are "preferred",
   because in Indian villages ponds are normally dug on gram-panchayat or
   government land rather than private fields.

3. User-supplied land parcels (GeoJSON drawn on the map or uploaded). When
   given, they are treated as the authoritative list of government land and
   siting is restricted to them.

Terrain slope is applied last: excavation on steep ground is unsafe and
loses storage, so cells steeper than `max_slope_deg` are excluded.

OSM does not carry cadastral ownership, so without user parcels the
"preferred" class is an indication, not a legal determination. That caveat is
returned in the response.
"""

from __future__ import annotations

import threading
import time

import cv2
import httpx
import numpy as np

from . import db
from .terrain import DEM
from .tiles import SOURCES, USER_AGENT, TileError, fetch_mosaic, pick_zoom, sample_mosaic

# Land-cover class codes used on the grid
WATER, BUILT, VEGETATION, OPEN = 0, 1, 2, 3
CLASS_NAMES = {WATER: "water", BUILT: "built_up", VEGETATION: "vegetation", OPEN: "open_land"}
CLASS_COLOURS_BGR = {
    WATER: (200, 120, 30),
    BUILT: (60, 60, 220),
    VEGETATION: (60, 170, 60),
    OPEN: (80, 190, 230),
}

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Buffers (metres) around linear/point features that make land unusable.
ROAD_BUFFER = {
    "motorway": 30, "trunk": 25, "primary": 20, "secondary": 15, "tertiary": 12,
    "residential": 8, "unclassified": 8, "service": 6, "track": 4,
}
BUILDING_BUFFER_M = 20
RAILWAY_BUFFER_M = 20
RIVER_BUFFER_M = 15

EXCLUDE_LANDUSE = {
    "residential", "industrial", "commercial", "retail", "cemetery", "military",
    "railway", "construction", "quarry", "landfill", "forest", "religious",
    "education", "reservoir", "basin", "salt_pond", "aquaculture", "garages",
}
EXCLUDE_NATURAL = {"water", "wetland", "wood", "bare_rock", "cliff", "beach", "glacier"}
PREFERRED_LANDUSE = {"grass", "meadow", "village_green", "greenfield", "brownfield", "recreation_ground"}
PREFERRED_NATURAL = {"scrub", "grassland", "heath"}
FARMLAND_LANDUSE = {"farmland", "orchard", "plant_nursery", "vineyard", "farmyard"}
PUBLIC_OWNERSHIP = {"government", "public", "national", "state", "municipal", "panchayat", "community"}


# --------------------------------------------------------------------------
# 1. Satellite classification
# --------------------------------------------------------------------------
def classify_imagery(bgr: np.ndarray, px_size_m: float) -> np.ndarray:
    """Per-pixel land-cover class from an RGB satellite image (BGR order)."""
    blur = cv2.GaussianBlur(bgr, (3, 3), 0)
    f = blur.astype(np.float32)
    b, g, r = f[..., 0], f[..., 1], f[..., 2]

    # Green-Red Vegetation Index (Tucker 1979; Motohka et al. 2010):
    # GRVI > 0 for green vegetation, < 0 for soil. Otsu picks the split for
    # this particular image (haze and sensor colour vary), kept within a
    # physically sensible band so a scene that is almost all bare, or almost
    # all green, is not forced into a 50/50 split.
    grvi = (g - r) / (g + r + 1e-6)
    grvi_u8 = np.clip((grvi + 0.5) * 255.0, 0, 255).astype(np.uint8)
    otsu, _ = cv2.threshold(grvi_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thr = float(np.clip(otsu / 255.0 - 0.5, 0.0, 0.10))
    vegetation = grvi > thr

    hsv = cv2.cvtColor(blur, cv2.COLOR_BGR2HSV)
    val = hsv[..., 2].astype(np.float32)

    # Built-up: settlements are clusters of bright, highly textured pixels
    # (roofs, walls, lanes). Isolated textured pixels are usually field
    # boundaries, so the candidate map is converted to a density over
    # roughly 40 m and only dense clusters are kept.
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    k = max(3, int(round(15.0 / max(px_size_m, 0.5))) | 1)
    mu = cv2.blur(gray, (k, k))
    local_sd = np.sqrt(np.maximum(cv2.blur(gray * gray, (k, k)) - mu * mu, 0.0))
    candidate = (~vegetation & (local_sd > max(18.0, float(np.percentile(local_sd, 90))))
                 & (val > float(np.median(val)))).astype(np.float32)
    win = max(3, int(round(40.0 / max(px_size_m, 0.5))) | 1)
    built = (cv2.blur(candidate, (win, win)) > 0.30) & ~vegetation

    # Water: dark, blue-dominant, smooth.
    water = (val < 90) & (b > g) & (b > r) & (local_sd < 8) & ~vegetation

    cls = np.full(gray.shape, OPEN, dtype=np.uint8)
    cls[vegetation] = VEGETATION
    cls[built] = BUILT
    cls[water] = WATER

    # Morphological clean-up of speckle.
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    for code in (WATER, BUILT):
        m = (cls == code).astype(np.uint8)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, kernel)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kernel)
        cls[(cls == code) & (m == 0)] = OPEN
        cls[m == 1] = code
    return cls


def satellite_landcover(dem: DEM, oversample: int = 4) -> tuple[np.ndarray, dict]:
    """
    Class fractions per DEM cell, shape (nrows, ncols, 4), from satellite
    imagery sampled at `oversample` x the DEM resolution.
    """
    bbox = dem.bbox
    target = dem.cell / oversample
    z = pick_zoom(dem.proj.lat0, target, SOURCES["esri_imagery"]["max_zoom"], bbox, max_tiles=64)
    mosaic, origin = fetch_mosaic("esri_imagery", bbox, z)
    lon, lat = dem.lonlat_grid(oversample)
    img = sample_mosaic(mosaic, origin, z, lon, lat, interpolation=cv2.INTER_AREA)
    # Where Esri has no imagery (open ocean, some remote areas) it serves flat
    # grey "Map data not yet available" tiles; classifying those would call
    # everything open land.
    placeholder = (np.ptp(img, axis=2) <= 2) & (img[..., 0] >= 195) & (img[..., 0] <= 215)
    if placeholder.mean() > 0.5:
        raise TileError("Esri World Imagery has no imagery for this area")
    cls = classify_imagery(img, dem.cell / oversample)

    fractions = np.zeros((dem.nrows, dem.ncols, 4), dtype=np.float32)
    for code in CLASS_NAMES:
        one_hot = (cls == code).astype(np.float32)
        fractions[..., code] = cv2.resize(one_hot, (dem.ncols, dem.nrows), interpolation=cv2.INTER_AREA)
    meta = {"imagery": "Esri World Imagery", "imagery_zoom": z, "classified_pixels": int(cls.size)}
    return fractions, meta


# --------------------------------------------------------------------------
# 2. OpenStreetMap
# --------------------------------------------------------------------------
OSM_CACHE_S = 7 * 24 * 3600
OSM_COOLDOWN_S = 120.0
# Overpass allows about two concurrent queries per IP; extra ones queue and
# time out. Serialising our calls (and caching the answers) keeps concurrent
# analyses from losing their OSM exclusions. After a failure, calls fail fast
# for a cool-down period instead of queueing behind one timeout after another.
_overpass_lock = threading.Lock()
_overpass_failed_at = 0.0


def fetch_osm(bbox, timeout_s: int = 25) -> list[dict]:
    global _overpass_failed_at
    key = "osm:" + ":".join(f"{v:.5f}" for v in bbox)
    cached = db.cache_get(key, OSM_CACHE_S)
    if cached is None:
        with _overpass_lock:
            cached = db.cache_get(key, OSM_CACHE_S)   # filled while we waited?
            if cached is None:
                if time.monotonic() - _overpass_failed_at < OSM_COOLDOWN_S:
                    raise RuntimeError("Overpass API unavailable (failed recently; retrying after a cool-down)")
                try:
                    cached = {"elements": _query_overpass(bbox, timeout_s)}
                except RuntimeError:
                    _overpass_failed_at = time.monotonic()
                    raise
                db.cache_put(key, cached)
    return cached["elements"]


def _query_overpass(bbox, timeout_s: int) -> list[dict]:
    min_lon, min_lat, max_lon, max_lat = bbox
    b = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    query = f"""
[out:json][timeout:{timeout_s}][maxsize:67108864];
(
  way["building"]({b});
  way["highway"]({b});
  way["railway"]({b});
  way["waterway"]({b});
  way["natural"]({b});
  way["landuse"]({b});
  way["amenity"]({b});
  way["leisure"]({b});
  way["boundary"="protected_area"]({b});
  relation["landuse"]({b});
  relation["natural"]({b});
);
out geom qt;
"""
    last = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    for url in OVERPASS_URLS:
        try:
            # Short connect timeout: a firewalled network drops the SYN, and waiting the
            # full read timeout per server would stall every analysis.
            resp = httpx.post(url, data={"data": query}, headers=headers,
                              timeout=httpx.Timeout(timeout_s + 10, connect=5.0))
            if resp.status_code == 200:
                return resp.json().get("elements", [])
            last = f"HTTP {resp.status_code}"
        except (httpx.HTTPError, ValueError) as exc:
            last = str(exc)
    raise RuntimeError(f"Overpass API unavailable ({last})")


def _element_rings(el: dict) -> list[list[dict]]:
    """Coordinate lists for a way, or the outer member ways of a relation."""
    if el.get("type") == "way" and el.get("geometry"):
        return [el["geometry"]]
    if el.get("type") == "relation":
        parts = [m["geometry"] for m in el.get("members", [])
                 if m.get("type") == "way" and m.get("role") in ("outer", "") and m.get("geometry")]
        return _stitch(parts)
    return []


def _stitch(parts: list[list[dict]]) -> list[list[dict]]:
    """Join multipolygon member ways end-to-end into closed rings."""
    key = lambda p: (round(p["lat"], 7), round(p["lon"], 7))
    parts = [list(p) for p in parts]
    rings = []
    while parts:
        ring = parts.pop(0)
        progress = True
        while key(ring[0]) != key(ring[-1]) and progress:
            progress = False
            for i, p in enumerate(parts):
                if key(p[0]) == key(ring[-1]):
                    ring += p[1:]
                elif key(p[-1]) == key(ring[-1]):
                    ring += p[::-1][1:]
                elif key(p[-1]) == key(ring[0]):
                    ring = p[:-1] + ring
                elif key(p[0]) == key(ring[0]):
                    ring = p[::-1][:-1] + ring
                else:
                    continue
                parts.pop(i)
                progress = True
                break
        if len(ring) > 3:
            rings.append(ring if key(ring[0]) == key(ring[-1]) else ring + [ring[0]])
    return rings


def rasterize_osm(elements: list[dict], dem: DEM) -> dict[str, np.ndarray]:
    """Burn OSM features into exclusion / preference masks on the DEM grid."""
    shape = (dem.nrows, dem.ncols)
    exclude = np.zeros(shape, dtype=np.uint8)
    preferred = np.zeros(shape, dtype=np.uint8)
    farmland = np.zeros(shape, dtype=np.uint8)
    forest = np.zeros(shape, dtype=np.uint8)
    osm_water = np.zeros(shape, dtype=np.uint8)
    counts = {"buildings": 0, "roads": 0, "water_features": 0, "excluded_areas": 0, "preferred_areas": 0}

    def to_px(geom):
        lon = np.array([p["lon"] for p in geom])
        lat = np.array([p["lat"] for p in geom])
        r, c = dem.lonlat_to_cell(lon, lat)
        return np.column_stack([c, r]).round().astype(np.int32)

    def px(m):
        return max(1, int(round(m / dem.cell)))

    for el in elements:
        tags = el.get("tags", {})
        rings = _element_rings(el)
        if not rings:
            continue
        closed = all(len(g) > 3 and g[0] == g[-1] for g in rings) or el.get("type") == "relation"
        polys = [to_px(g) for g in rings]

        def fill(mask):
            if closed:
                cv2.fillPoly(mask, polys, 1)
            else:
                cv2.polylines(mask, polys, False, 1, thickness=1)

        def line(mask, width_m):
            cv2.polylines(mask, polys, closed, 1, thickness=px(2 * width_m))

        landuse, natural = tags.get("landuse"), tags.get("natural")
        ownership = (tags.get("ownership") or tags.get("operator:type") or "").lower()

        if "building" in tags:
            fill(exclude)
            line(exclude, BUILDING_BUFFER_M)
            counts["buildings"] += 1
        elif "highway" in tags:
            line(exclude, ROAD_BUFFER.get(tags["highway"], 5))
            counts["roads"] += 1
        elif "railway" in tags:
            line(exclude, RAILWAY_BUFFER_M)
        elif "waterway" in tags:
            # Rivers and canals are excluded; small streams and drains are the
            # natural place for a pond, so they are left available.
            if tags["waterway"] in ("river", "canal", "riverbank", "dam", "weir"):
                line(exclude, RIVER_BUFFER_M)
                line(osm_water, RIVER_BUFFER_M)
                counts["water_features"] += 1
        elif natural in EXCLUDE_NATURAL or landuse in EXCLUDE_LANDUSE or \
                tags.get("boundary") == "protected_area" or "amenity" in tags or \
                tags.get("leisure") in ("park", "nature_reserve", "pitch", "stadium", "sports_centre"):
            fill(exclude)
            counts["excluded_areas"] += 1
            if natural in ("water", "wetland") or landuse in ("reservoir", "basin"):
                fill(osm_water)
                counts["water_features"] += 1
            if natural == "wood" or landuse == "forest":
                fill(forest)
        elif landuse in PREFERRED_LANDUSE or natural in PREFERRED_NATURAL or \
                any(k in ownership for k in PUBLIC_OWNERSHIP):
            fill(preferred)
            counts["preferred_areas"] += 1
        elif landuse in FARMLAND_LANDUSE:
            fill(farmland)

    return {
        "exclude": exclude.astype(bool),
        "preferred": preferred.astype(bool),
        "farmland": farmland.astype(bool),
        "forest": forest.astype(bool),
        "water": osm_water.astype(bool),
        "counts": counts,
    }


# --------------------------------------------------------------------------
# 3. User parcels
# --------------------------------------------------------------------------
def rasterize_parcels(geojson: dict, dem: DEM) -> np.ndarray:
    """Mask of cells inside any Polygon/MultiPolygon of a GeoJSON object."""
    mask = np.zeros((dem.nrows, dem.ncols), dtype=np.uint8)

    def polygons(obj):
        t = obj.get("type")
        if t == "FeatureCollection":
            for f in obj.get("features", []):
                yield from polygons(f)
        elif t == "Feature":
            yield from polygons(obj.get("geometry") or {})
        elif t == "Polygon":
            yield obj["coordinates"]
        elif t == "MultiPolygon":
            yield from obj["coordinates"]
        elif t == "GeometryCollection":
            for g in obj.get("geometries", []):
                yield from polygons(g)

    for poly in polygons(geojson):
        for i, ring in enumerate(poly):
            arr = np.asarray(ring, dtype=float)
            r, c = dem.lonlat_to_cell(arr[:, 0], arr[:, 1])
            pts = np.column_stack([c, r]).round().astype(np.int32)
            # Outer ring adds land, inner rings (holes) remove it.
            cv2.fillPoly(mask, [pts], 1 if i == 0 else 0)
    return mask.astype(bool)


# --------------------------------------------------------------------------
# Combine
# --------------------------------------------------------------------------
def assess_land(dem: DEM, slope_rad: np.ndarray, *, parcels: dict | None = None,
                max_slope_deg: float = 8.0, exclude: np.ndarray | None = None) -> dict:
    """
    Returns a dict with:
      allowed      bool grid - land where a pond may be excavated
      land_score   0..1 grid - preference among allowed cells
      tier         int grid  - 0 excluded, 1 farmland, 2 open land, 3 preferred/parcel
      fractions    class fractions from imagery (or None)
      ...          metadata and warnings
    """
    shape = (dem.nrows, dem.ncols)
    warnings: list[str] = []
    meta: dict = {"sources": []}

    fractions = None
    try:
        fractions, sat_meta = satellite_landcover(dem)
        meta.update(sat_meta)
        meta["sources"].append("satellite_classification")
    except (TileError, httpx.HTTPError, cv2.error) as exc:
        warnings.append(f"Satellite classification unavailable: {exc}")

    osm = None
    try:
        osm = rasterize_osm(fetch_osm(dem.bbox), dem)
        meta["osm_feature_counts"] = osm["counts"]
        meta["sources"].append("openstreetmap")
    except Exception as exc:  # network / parse failures must not kill the analysis
        warnings.append(f"OpenStreetMap data unavailable: {exc}")

    parcel_mask = None
    if parcels:
        parcel_mask = rasterize_parcels(parcels, dem)
        meta["sources"].append("user_parcels")
        meta["parcel_area_m2"] = round(float(parcel_mask.sum() * dem.cell_area), 1)
        if not parcel_mask.any():
            warnings.append("Supplied land parcels do not overlap the analysis area.")

    slope_deg = np.degrees(slope_rad)
    allowed = slope_deg <= max_slope_deg
    if exclude is not None:
        allowed &= ~exclude
    tier = np.full(shape, 2, dtype=np.int8)
    land_score = np.full(shape, 0.7)

    if fractions is not None:
        built = fractions[..., BUILT] > 0.35
        water = fractions[..., WATER] > 0.5
        dense_veg = fractions[..., VEGETATION] > 0.75
        allowed &= ~built & ~water
        land_score = 0.4 + 0.6 * fractions[..., OPEN] + 0.2 * fractions[..., VEGETATION]
        land_score = np.where(dense_veg, 0.45, land_score)

    if osm is not None:
        allowed &= ~osm["exclude"]
        tier[osm["farmland"]] = 1
        land_score = np.where(osm["farmland"], np.minimum(land_score, 0.5), land_score)
        tier[osm["preferred"]] = 3
        land_score = np.where(osm["preferred"], 1.0, land_score)

    if parcel_mask is not None and parcel_mask.any():
        allowed &= parcel_mask
        tier[parcel_mask] = 3
        land_score = np.where(parcel_mask, 1.0, land_score)

    tier[~allowed] = 0
    land_score = np.clip(np.where(allowed, land_score, 0.0), 0.0, 1.0)

    if not allowed.any():
        warnings.append("No land passed the suitability filters; siting falls back to terrain only.")

    area = dem.cell_area
    meta.update({
        "max_slope_deg": max_slope_deg,
        "suitable_area_m2": round(float(allowed.sum() * area), 1),
        "suitable_fraction": round(float(allowed.mean()), 4),
        "preferred_area_m2": round(float((tier == 3).sum() * area), 1),
        "ownership_note": (
            "Restricted to the government land parcels you supplied."
            if parcel_mask is not None and parcel_mask.any() else
            "OpenStreetMap does not record legal ownership. 'Preferred' land carries tags that usually "
            "indicate common/public land (grassland, scrub, village green, public ownership); verify "
            "against village land records (e.g. Bhu-Naksha / revenue maps) before excavation."
        ),
    })
    if fractions is not None:
        meta["landcover_fraction_of_area"] = {
            CLASS_NAMES[k]: round(float(fractions[..., k].mean()), 4) for k in CLASS_NAMES
        }

    return {
        "allowed": allowed,
        "land_score": land_score,
        "tier": tier,
        "fractions": fractions,
        "osm": osm,
        "parcels": parcel_mask,
        "meta": meta,
        "warnings": warnings,
    }


def landcover_image(fractions: np.ndarray) -> np.ndarray:
    """RGBA-ish BGR image of the dominant class per cell, north-up for display."""
    dominant = np.argmax(fractions, axis=2)
    img = np.zeros(dominant.shape + (3,), dtype=np.uint8)
    for code, colour in CLASS_COLOURS_BGR.items():
        img[dominant == code] = colour
    return cv2.flip(img, 0)
