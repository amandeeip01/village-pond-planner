# Village Pond Planning System

A web application that helps village administrators decide **where to dig a
rainwater-harvesting pond, how big to make it, and how much water it will
collect**. Search for a village; the app reconstructs the terrain, finds land
that is actually available for excavation, delineates the catchment, pulls 20
years of rainfall history, estimates runoff and recommends pond dimensions —
all shown on an interactive satellite map.

Nothing is hard-coded to a location. Every result is derived from public data
for the point you choose (or from a contour map you upload).

---

## Features (mapped to the assignment)

| # | Requirement | How it is met |
|---|---|---|
| 1 | Satellite imagery for a selected village | Village search (OpenStreetMap Nominatim) or map click; Esri World Imagery basemap with place labels, plus topographic/street maps (and MapTiler if a key is set) |
| 2 | Visualise contour maps | Contours generated from the DEM with OpenCV (auto interval, major/minor lines, elevation tooltips), or the uploaded KML contours; colour-relief hillshade overlay |
| 3 | Identify available land | OpenCV classification of satellite imagery (vegetation / open land / built-up / water) + OpenStreetMap exclusions (buildings, roads, rivers, forest, settlements, institutions) + slope limit; OSM tags suggesting public/common land are *preferred*; administrators can **draw or upload government land parcels**, which then restrict siting |
| — | Recommend suitable *locations* | A ranked shortlist of up to 5 sites with **independent catchments** (none upstream or downstream of another); each has its own catchment, runoff and pond design. Shown as numbered markers and a table |
| 4 | Catchment area | Priority-Flood depression filling, D8 flow routing, flow accumulation, reverse traversal from the pond; boundary polygon, area, perimeter, relief, slope |
| 5 | Historical rainfall from public APIs | CHIRPS 5 km daily rainfall (via NASA SERVIR ClimateSERV), fallback Open-Meteo (ERA5) then NASA POWER; 5–40 years, cached; optional scaling to a known IMD normal. Validated against IMD normals (8.9 % mean error, see `VALIDATION.md`) |
| 6 | Runoff volume | SCS Curve Number applied **daily**, AMC-adjusted, area-weighted by land cover and soil group; rational method as a cross-check |
| 7 | Pond depth & storage | Excavated frustum sized to the 75 %-dependable runoff, depth chosen 2–4.5 m, limited by available land; earthwork, evaporation and seepage losses |
| 8 | Overlay all results | Pond marker + footprint, catchment, drainage network, available land, land-cover and relief rasters, contours; sidebar with rainfall statistics, charts, runoff, dimensions and a cross-section; JSON/GeoJSON export; saved history |

---

## Quick start

```bash
python3.12 -m venv .venv   # Python 3.12 or newer (3.13 also works)
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000> for the app and <http://127.0.0.1:8000/docs> for
the interactive API documentation.

No API keys are required. Optionally copy `.env.example` to `.env` and set
`MAPTILER_KEY` to add MapTiler basemaps.

### Using the app

1. **Village tab** — search a village name (e.g. *Hiware Bazar*, *Ralegan
   Siddhi*) or click the map. The dashed square is the analysis area.
2. *(Optional)* **Draw parcel** / **Upload GeoJSON** to mark government land.
   The pond will then be placed only inside those parcels.
3. *(Optional)* tick **Choose pond location myself** and click the map; the pin
   is snapped to the strongest drainage line within 60 m and becomes rank 1,
   with automatic alternatives after it.
4. **Analyse village**. The first run for a place takes about 40 s (20 years
   of CHIRPS rainfall); repeats take 2–5 s from the cache.
5. Toggle map layers from the layer control (top right). Download the report
   as JSON or the layers as GeoJSON. Past runs are in the **History** tab.

The **Contour file** tab runs the same analysis on an uploaded KML/KMZ contour
map instead of the elevation service (`sample_contours.kml` is included).

---

## Architecture

```
                       ┌──────────── Browser (Leaflet + Leaflet.draw + Chart.js) ───────────┐
                       │ search · draw parcels · layers · charts · history · export         │
                       └───────────────────────────────┬────────────────────────────────────┘
                                                       │ JSON
┌──────────────────────────────── FastAPI (app/main.py) ────────────────────────────────────┐
│ /api/geocode   /api/analyze   /analyzeContour   /api/rainfall   /api/analyses             │
└───────┬───────────────┬───────────────────────────────┬─────────────────────────┬─────────┘
        │               │                               │                         │
   geocode.py      elevation.py ── tiles.py        kml_parser.py             db.py (SQLite)
   Nominatim       AWS Terrain Tiles (OpenCV)      terrain.py (Delaunay)     analyses, API cache
                        │                               │
                        └──────────────┬────────────────┘
                                       ▼  DEM
                                  pipeline.py
     hydrology.py  → fill · D8 · accumulation · edge-inflow check · catchment · boundary
     landcover.py  → Esri imagery + OpenCV classification · Overpass OSM · parcels · slope
     pond.py       → site scoring on available land (or user pin, snapped)
     rainfall.py   → CHIRPS / Open-Meteo / NASA POWER daily series → statistics
     runoff.py     → daily SCS-CN (AMC, land cover, soil group) + rational check
     design.py     → depth, dimensions, storage, earthwork, losses, footprint polygon
     layers.py     → contours, drainage, land polygons, PNG overlays
```

### Data sources (all free, no key)

| Data | Source | Used for |
|---|---|---|
| Place search | OpenStreetMap Nominatim | Finding villages |
| Elevation | AWS Terrain Tiles (terrarium; SRTM/Copernicus, ~30 m) — fallback Open-Meteo elevation (Copernicus GLO-90) | DEM |
| Satellite imagery | Esri World Imagery | Basemap, land-cover classification |
| Map features | OpenStreetMap via Overpass API | Buildings, roads, water, land use |
| Rainfall | CHIRPS v2.0, 0.05° daily (via ClimateSERV) — fallback Open-Meteo ERA5, then NASA POWER | Rainfall statistics, runoff |
| Reference ET₀ | Open-Meteo (FAO-56) — fallback NASA POWER temperatures (Hargreaves) | Evaporation losses |

IMD gridded rainfall is distributed as bulk files rather than a query API.
CHIRPS was chosen as the default after checking three datasets against IMD
station normals (`VALIDATION.md`): NASA POWER was off by up to +92 % in
rain-shadow areas. The first analysis of a location takes about 40 s while 20
years of CHIRPS download; repeat analyses are served from the cache. If the
IMD district normal is known, enter it as *Local rainfall normal* and the
daily series is scaled to it.

---

## Methods

### Terrain
A square grid (default 4 × 4 km, 200 × 200 cells of 20 m) is laid out in a
local equirectangular projection. Terrarium tiles are decoded to metres
(`R·256 + G + B/256 − 32768`) **before** resampling and bilinearly sampled onto
the grid with `cv2.remap`. For uploaded contour maps, contour vertices are
interpolated by Delaunay triangulation (see `kml_parser.py`, `terrain.py`).

### Hydrology
Priority-Flood fills spurious pits; D8 gives each cell its steepest downhill
neighbour; accumulation counts upstream cells. Cells receiving flow from an
inward-draining border cell are flagged: in village mode the pond is never put
where the catchment would be cut off by the map edge (in contour mode a
warning is issued instead).

### Available land
- **Satellite (OpenCV):** Green-Red Vegetation Index `(G−R)/(G+R)` with an
  Otsu threshold clamped to 0–0.10 → vegetation; clusters of bright, highly
  textured pixels (local standard deviation, 40 m density) → built-up; dark
  blue-dominant smooth pixels → water; the rest → open land. Morphological
  opening/closing removes speckle. Class fractions are aggregated per cell.
- **OpenStreetMap:** buildings (+20 m), roads (+4–30 m by class), railways,
  rivers/canals, water bodies, wetlands, forest, residential/industrial/
  institutional land, protected areas → excluded. Grassland, scrub, meadow,
  village green or public ownership tags → *preferred*. Farmland → allowed
  with lower priority (farm pond on private land).
- **Parcels:** if supplied, siting is restricted to them.
- **Slope:** ground steeper than 8° (configurable) is excluded.
- **Sea:** in village mode, connected low areas holding the flat 0 m sea
  surface or bathymetry are excluded, so coastal villages never get a pond
  offshore. Low-lying land such as Kuttanad's polders is kept. A location that
  is mostly sea is rejected with `422`.

OSM has no cadastral ownership, so without parcels "preferred" is an
indication only; the response says so and recommends checking revenue maps
(e.g. Bhu-Naksha).

### Pond sites
Candidates are interior cells on the drainage network (top 8 % accumulation,
relaxed stepwise if available land does not reach it) on available land.
Score = 0.75 × (0.45 accumulation + 0.35 gentle slope + 0.20 concavity)
+ 0.25 × land suitability. Sites are then taken best-first, accepting a cell
only if it is at least 250 m from, and neither upstream nor downstream of,
every accepted site. The result is up to 5 ponds whose inflows can be added
without double counting.

### Testing and validation

`python -m pytest tests` runs 35 unit tests that check every formula against
hand calculations or exact analytic answers, including a synthetic valley
whose catchment is known cell for cell, that pond siting never lands in
the sea, and that OpenStreetMap queries are cached and fail fast. `VALIDATION.md` reports the accuracy
checks: rainfall against IMD normals, elevation against surveyed summits, and
the land-cover classifier against 100 blind hand-labelled cells (92 %,
κ = 0.85).

### Rainfall statistics
Mean, median, standard deviation, CV, **75 % dependable rainfall** (25th
percentile of annual totals), driest/wettest year, rainy days (≥ 2.5 mm, IMD
definition), heaviest day, monsoon share (Jun–Sep), monthly climatology, and
annual/dry-season (Oct–May) reference evapotranspiration.

### Runoff — SCS Curve Number
For every day: `S = 25400/CN − 254`, `Ia = 0.2 S`,
`Q = (P − Ia)² / (P − Ia + S)`. CN (TR-55) comes from each land-cover class in
the catchment for the chosen hydrologic soil group, adjusted daily to AMC I/III
from 5-day antecedent rainfall (35.6 / 53.3 mm) with Hawkins' equations, and
area-weighted. The rational method (`A·P·C`, land-cover weighted C) is
reported as a cross-check.

### Pond design
- Target storage = 50 % (configurable) of the 75 %-dependable annual runoff, so
  the pond fills about twice even in a dry year.
- Rectangular frustum, side slope 1.5 H : 1 V, 0.5 m freeboard, length 1.5 ×
  width with the long side along the contour. Volume by the prismoidal formula.
- Depth starts at 3 m (MGNREGA / Amrit Sarovar practice). It is increased (up
  to 4.5 m) when the footprint would not fit on 80 % of the connected patch of
  available land (or the 2 ha village-pond cap), and reduced for small ponds
  so the bed stays ≥ 4 m wide.
- Reported: storage, top/bed dimensions, water-spread area, excavation volume,
  dry-season open-water evaporation (1.05 × ET₀ over the half-depth area),
  seepage by soil group, fills per dependable year, lining advice.

---

## API reference

Full schema at `/docs`. Main routes:

### `POST /api/analyze` — analyse a village location

```json
{
  "lat": 19.068, "lon": 74.601, "village": "Hiware Bazar",
  "radius_m": 2000, "grid_resolution": 200,
  "soil_group": "C", "rainfall_years": 20,
  "fill_fraction": 0.5, "side_slope": 1.5, "max_depth_m": 4.5, "max_slope_deg": 8,
  "pond_lat": null, "pond_lon": null, "n_candidates": 5,
  "rainfall_source": "auto", "annual_normal_mm": null,
  "land_parcels": { "type": "FeatureCollection", "features": [] }
}
```

Only `lat` and `lon` are required.

### `POST /analyzeContour` — analyse an uploaded contour map
`multipart/form-data` with `file` (.kml/.kmz, ≤ 25 MB) and optional
`land_parcels` (GeoJSON string). Query parameters: `soil_group`,
`grid_resolution`, `rainfall_years`, `fill_fraction`, `dam_height_m`,
`annual_rainfall_mm` (fallback only), `runoff_coefficient`, `analyze_land`,
`fetch_rainfall`, `pond_lat`, `pond_lon`, `village`. `/findCatchment` is an
alias.

### Response (both routes, abridged)

```json
{
  "status": "success", "mode": "village", "analysis_id": 10,
  "pond_site":   { "location": {"lon": 74.6079, "lat": 19.0827, "elevation_m": 700.99},
                   "slope_deg": 0.07, "on_available_land": true, "land_tier": "open_land", "reason": "..." },
  "catchment":   { "area_hectares": 110.76, "perimeter_m": 7440, "relief_m": 84.3,
                   "mean_slope_deg": 3.99, "touches_analysis_edge": false, "boundary_geojson": {"...": "..."} },
  "rainfall":    { "source": "...", "period": "2006-2025", "mean_annual_mm": 703.7,
                   "dependable_75pct_mm": 603.8, "annual_series": [], "monthly_mean_mm": [] },
  "runoff":      { "composite_curve_number": 85.9, "mean_annual_runoff_m3": 102757,
                   "dependable_annual_runoff_m3": 59495, "rational_method_runoff_m3": 262215 },
  "pond_design": { "storage_capacity_m3": 29748, "water_depth_m": 3.0, "top_length_m": 127.5,
                   "top_width_m": 85.0, "excavation_volume_m3": 35250, "notes": [] },
  "candidates":  [ { "rank": 1, "score": 0.92, "pond_site": {}, "catchment": {}, "runoff": {}, "pond_design": {}, "pond_footprint": {} } ],
  "land":        { "suitable_area_m2": 12194800, "preferred_area_m2": 243600, "sources": [] },
  "layers":      { "contours": {}, "drainage": {}, "suitable_land": {}, "pond_footprint": {},
                   "elevation_png": "data:image/png;base64,...", "landcover_png": "...", "bounds": [] },
  "warnings": [], "method": {}, "processing_seconds": 4.7
}
```

Example values are from Hiware Bazar on 2 October 2026. Imagery, OpenStreetMap and
rainfall are live sources, so results for the same request can change over time.

### Other routes

| Route | Purpose |
|---|---|
| `GET /api/geocode?q=&country=in` | Village search |
| `GET /api/reverse?lat=&lon=` | Place name for a point |
| `GET /api/rainfall?lat=&lon=&years=` | Rainfall statistics only |
| `GET /api/analyses` · `GET/DELETE /api/analyses/{id}` | Saved analyses |
| `GET /health` | Liveness probe |

Errors return `{"status": "error", "detail": "..."}` — `400` empty upload,
`413` file too large, `422` invalid input or a location in the open sea,
`502` upstream data service down.

---

## Deployment

**Docker**

```bash
docker build -t pond-planner .
docker run -p 8000:8000 -v pond-data:/app/data -e MAPTILER_KEY=... pond-planner
```

**Render / Railway** — build `pip install -r requirements.txt`, start
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Set `MAPTILER_KEY` as an
environment variable if wanted. Attach a disk at `/app/data` to keep history.

Configuration (environment variables): `MAPTILER_KEY`, `DATABASE_PATH`
(default `data/pond_planner.db`), `TILE_CACHE_DIR` (default `cache/tiles`).

---

## Limitations

- ~30 m elevation data cannot resolve small bunds, drains or field
  embankments; field verification of the site is essential.
- RGB-only imagery separates vegetation well but built-up detection is
  approximate; OSM buildings are the primary exclusion and OSM coverage of
  rural India varies.
- RGB water detection misses turbid, green-tinted water and was not part of the
  land-cover validation sample; the sea is excluded using elevation instead, and
  inland water relies on OpenStreetMap.
- Land ownership is not available from open data; supply parcels for a
  definitive answer.
- Rainfall: CHIRPS is within about 9 % of IMD normals on average, but it
  underestimates deserts (−28 % at Jaisalmer). The fallback reanalyses are
  worse in rain-shadow areas. Enter the IMD normal when it is known; runoff
  is very sensitive to it (−13 % rain gives −35 % runoff).
- Soil group is a user input; groundwater depth and rock are not considered
  when choosing depth.
- Public APIs are rate-limited (Open-Meteo per IP per day, Nominatim 1 req/s,
  Overpass about 2 concurrent queries per IP) and ClimateSERV can be slow;
  results are cached (rainfall 30 days, OpenStreetMap and geocoding 7 days),
  Overpass calls are serialised with a 2-minute cool-down after a failure, and
  fallbacks are automatic.
