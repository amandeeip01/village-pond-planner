# Catchment Estimation API — Phase Report

**Name:** Amandeeip Kammari  **Roll Number:** 12341060

**GitHub repository:** `https://github.com/amandeeip01/village-pond-planner`

**Working API route:** `https://<your-app>.onrender.com/analyzeContour`

**Interactive documentation:** `https://<your-app>.onrender.com/docs`

---

## 1. Objective

Develop a backend API route that accepts a contour map in KML/KMZ format,
analyses the terrain, identifies a suitable pond location, and returns the
corresponding catchment information as JSON.

## 2. Catchment estimation approach

A contour map is a set of lines of constant elevation. It contains enough
information to reconstruct the terrain surface, and once the surface exists,
catchment estimation becomes a flow-routing problem.

**Step 1 — Contour extraction.** The KML is parsed and every `LineString` /
`LinearRing` is read as a contour line. Because contour files store the height
value in different places, four extraction strategies are attempted in order:
coordinate Z values, `ExtendedData` fields, Placemark names, and ancestor
Folder names. A strategy is accepted only if it produces values for ≥90% of
lines **and** those values vary — a constant result means the strategy has
found something that is not elevation.

**Step 2 — Surface reconstruction.** Longitude/latitude are projected to a
local metre-based frame using an equirectangular projection about the map
centroid. The contour vertices then form a scattered point cloud with known
elevations, which is interpolated onto a regular grid (a DEM) by linear
interpolation over a Delaunay triangulation.

**Step 3 — Depression filling.** An interpolated DEM contains small artificial
pits. Priority-Flood raises each pit to the level of its lowest outlet so that
every cell has a continuous downhill path to the edge of the map.

**Step 4 — Flow routing.** The D8 method assigns each cell the direction of its
steepest downhill neighbour, weighting drop by distance so diagonal neighbours
are treated fairly. Flow accumulation then counts how many cells drain through
each cell, processing cells from highest to lowest so each total is complete
before it is passed downstream. High accumulation values trace out the natural
drainage network.

**Step 5 — Pond siting.** A suitable pond site satisfies three terrain
conditions, each computed from the DEM:

| Criterion | Measure | Weight |
|---|---|---|
| Water actually arrives | flow accumulation | 0.45 |
| Dam is short and cheap | low slope | 0.35 |
| Terrain holds water | concave curvature | 0.20 |

Each is normalised to 0–1 over the candidate cells and combined. Candidates are
restricted to the drainage network (top percentile of accumulation) and to the
interior of the map, since a site near the edge would have a catchment cut off
by the map boundary.

**Step 6 — Catchment delineation.** The D8 flow graph is traversed *backwards*
from the selected site: any neighbour that flows into a cell already in the set
is added. The result is every cell whose water reaches the pond. Area is the
cell count multiplied by cell area.

**Step 7 — Boundary extraction.** Cell edges with catchment on exactly one side
are collected and stitched into a closed ring, then simplified by removing
collinear points, and converted back to WGS84 as a GeoJSON polygon.

## 3. Demonstration on the provided contour map

Command used:

```bash
curl -X POST "<API_URL>/analyzeContour" -F "file=@contours_1m.kml"
```

**Input map summary**

| Property | Value |
|---|---|
| Contour lines | _____ |
| Distinct levels | _____ |
| Contour interval | _____ m |
| Elevation range | _____ – _____ m |
| Elevation source detected | _____ |

**DEM built**

| Property | Value |
|---|---|
| Grid size | _____ × _____ |
| Cell size | _____ m |
| Extrapolated cells | _____ % |

**Results**

| Output | Value |
|---|---|
| Pond site (lon, lat) | _____, _____ |
| Site elevation | _____ m |
| Site slope | _____ ° |
| **Catchment area** | **_____ ha** (_____ m²) |
| Catchment perimeter | _____ m |
| Relief | _____ m |
| Mean slope | _____ ° |
| Gross storage @ 3 m dam | _____ m³ |
| Water spread area | _____ m² |
| Processing time | _____ s |

_[Paste the raw JSON response here, and a screenshot of the catchment polygon
rendered on geojson.io.]_

## 4. API documentation

See `README.md` in the repository for the full reference: request format, all
four query parameters with defaults and ranges, the complete response schema,
and error codes (`400` empty upload, `413` oversized, `422` unparseable map).

Interactive OpenAPI documentation is auto-generated at `/docs`.

## 5. Extensibility to future phases

Nothing in the implementation refers to any specific location. All results are
derived from the uploaded file, so the endpoint already accepts arbitrary
contour maps. Beyond that:

- Elevation parsing is a list of pluggable strategies; supporting a new KML
  dialect means adding one function.
- Siting weights and grid resolution are function arguments, so they can be
  exposed as API parameters or tuned per terrain type.
- The parser returns a format-neutral structure, so a GeoTIFF or shapefile
  reader can be added without touching the hydrology.
- D8 can be replaced by D-infinity for smoother flow on gentle terrain; the
  change is confined to `hydrology.py`.

## 6. Limitations

- Linear interpolation across contour lines produces flat triangles in wide
  contour gaps, slightly smoothing narrow ridges.
- D8 restricts flow to eight directions, which can create parallel artificial
  channels on very flat ground.
- Storage is gross volume at full supply level; siltation, freeboard, seepage
  and evaporation are not modelled.
- Rainfall and runoff coefficient cannot be derived from a contour map, so they
  are user-supplied parameters with documented defaults.
