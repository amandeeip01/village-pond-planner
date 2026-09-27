# Study guide — preparing for the demonstration

The course policy says you may be asked to *explain design decisions,
algorithms, implementation details, and justify the use of external
libraries*. This guide is for exactly that.

Work through it with the code open. For each section:

1. Read the explanation.
2. Open the file it names.
3. Find the function.
4. Say the explanation out loud **without looking**.

If you can do that for every section, you can handle the viva.

---

## Part 1 — The 60-second pitch (memorise this)

> "A village administrator types the village name. The app downloads free
> elevation tiles and builds a 20-metre terrain grid. It works out where water
> flows using a standard algorithm called D8, after filling fake pits in the
> terrain. It finds land that's free to dig by classifying satellite imagery
> with OpenCV and removing buildings, roads and rivers from OpenStreetMap. It
> scores every cell on a drainage line for water, flatness, hollowness and land,
> and picks five sites whose catchments don't overlap. For each site it traces
> the catchment, pulls 20 years of daily CHIRPS rainfall, simulates runoff day by
> day with the SCS Curve Number method, and sizes a 3-metre-deep pond to hold
> half of a dry year's runoff. Everything is shown on a Leaflet map. I validated
> it: rainfall within 9 % of IMD, land classification 92 % accurate, and
> 23 unit tests."

---

## Part 2 — The pipeline, step by step

Open `app/pipeline.py`, function `run()`. It *is* the system. Every other file
is one step of it.

### Step 1 — Terrain (`app/elevation.py`, `app/tiles.py`, `app/terrain.py`)

**What:** Make a 200 × 200 grid of elevations, with 20 m cells, covering 4 × 4 km.

**How:**
- `terrain.empty_grid()` lays out the grid in metres around the village centre.
  It uses an *equirectangular projection*: x = east metres, y = north metres.
  The formula is in `LocalProjection`.
- `tiles.py` downloads map tiles (256 × 256 PNG images) from *AWS Terrain
  Tiles*. Each pixel's colour **is** the elevation:
  `height = R×256 + G + B/256 − 32768`.
- OpenCV decodes the PNGs (`cv2.imdecode`). The tiles are stitched into one
  big image. Then `cv2.remap` samples it at the centre of each grid cell with
  bilinear interpolation.

**Likely questions**
- *Why decode before resampling?* The colour channels are packed bytes of one
  number. Averaging two neighbouring pixels' R, G and B separately gives
  nonsense heights. You must convert to metres first, then interpolate.
- *Why not use an elevation API with one call per point?* A 200 × 200 grid is
  40,000 points, so 40,000 calls. Tiles give about 65,000 heights per request.
- *Why not UTM?* Over 4 km the equirectangular distortion is centimetres, far
  below the DEM error. It also avoids installing PROJ/GDAL.
- *What is the fallback?* The Open-Meteo elevation API
  (`_from_open_meteo`), sampled on a coarse lattice and interpolated.

**KML mode:** `kml_parser.py` reads contour lines. Their height can be in
four places (z coordinate, ExtendedData, name, folder name), so it tries each
in turn. `terrain.build_dem()` then interpolates the contour points onto the
grid with Delaunay triangulation (`scipy.griddata`).

### Step 2 — Where does water flow? (`app/hydrology.py`)

**2a. `fill_depressions` (Priority-Flood, Barnes 2014)**

The DEM has small fake pits, which are errors. Water routed into a pit gets
stuck. Priority-Flood:

1. Put all border cells in a priority queue (lowest first).
2. Pop the lowest cell.
3. Each unvisited neighbour gets `max(its height, popped height + 0.0001)`
   and goes into the queue.

The effect is like flooding the map from outside: every pit fills up to the
level of its lowest exit. Afterwards every cell has a downhill path to the
edge. Complexity is O(n log n) because of the heap.

**2b. `d8_flow_direction` (D8, O'Callaghan & Mark 1984)**

Each cell sends all its water to **one** of its 8 neighbours: the steepest
downhill one. Steepness = drop ÷ distance, and distance is √2 for diagonals
so they aren't unfairly favoured. The code shifts the whole array 8 times
instead of looping over cells, which is why it's fast.

**2c. `flow_accumulation`**

This counts how many cells drain through each cell. Sort the cells from
highest to lowest. Each cell adds its count to its downstream neighbour.
Because water only flows downhill, when you reach a cell everything above it
has already been added. High numbers = streams.

**2d. `delineate_catchment`**

Start at the pond cell. Look at the 8 neighbours: which of them flow *into*
this cell? Add them. Repeat from each added cell. This walks the flow graph
backwards. The result is every cell whose water reaches the pond.

**2e. `trace_boundary`**

This turns the catchment's cells into a polygon. It collects every cell edge
that has catchment on one side and not on the other, joins them into a ring,
and removes points in the middle of straight lines. The polygon's area equals
the number of cells × 400 m², and a test checks this.

**2f. `edge_draining_cells`**

A border cell whose flow points *into* the map sits on land that slopes in
from outside, so water from beyond the map probably enters there. Every cell
downstream of it would have an incomplete catchment. In village mode those
cells can't be chosen.

**Likely questions**
- *Why fill depressions? Aren't real pits real?* At 30 m resolution almost all
  pits are data errors. Real ponds and tanks are rare and are handled as
  water/excluded land.
- *Limitation of D8?* Water can only go one of 8 directions, so on flat ground
  it makes parallel straight channels. D-infinity would split flow between two
  cells, and swapping it in would only change `hydrology.py`.
- *Why is accumulation O(n log n)?* The sort. The pass itself is O(n).

### Step 3 — Which land is free to dig? (`app/landcover.py`)

There are three evidence sources, combined in `assess_land()`.

**3a. Satellite imagery + OpenCV (`classify_imagery`)**
- **Vegetation.** GRVI = (G − R)/(G + R). Plants reflect more green than red,
  and soil reflects more red. The threshold is chosen per image by **Otsu's
  method**, which finds the value that best splits a histogram into two
  groups. It is clamped to 0–0.10 so an all-green or all-bare image isn't
  split in half.
- **Built-up.** Villages are bright and "busy": lots of edges and texture. The
  code computes local standard deviation (texture), marks bright, textured
  pixels, and keeps only **dense clusters** of them (40 m window, over 30 %).
  Single textured pixels are usually field boundaries.
- **Water.** Dark, blue-dominant, smooth pixels.
- **Open land.** Everything else.
- `morphologyEx` (opening then closing) removes salt-and-pepper noise.
- The 5 m classification is averaged into the 20 m cells (`INTER_AREA`), which
  gives the *fraction* of each class per cell.

**3b. OpenStreetMap (`fetch_osm`, `rasterize_osm`)**

One Overpass query gets buildings, roads, rivers, land use and so on.
`cv2.fillPoly` / `cv2.polylines` "paint" them onto the grid with safety
buffers: 20 m round buildings, 4–30 m round roads.
- **Excluded:** settlements, schools, forest, water, rivers.
- **Preferred:** grassland, scrub, meadow, village green, or "public"
  ownership, because in villages these are usually common/government land.

**3c. Parcels** that the user draws (Leaflet.draw) or uploads. If given, the
pond can only go inside them.

Finally, **slope ≤ 8°** (too steep is unsafe to dig).

**Likely questions**
- *Is this AI / machine learning?* It is classical computer vision: spectral
  indices, Otsu thresholding, texture and morphology. That was a deliberate
  choice. There is no labelled Indian village dataset to train a model, the
  rules are explainable, they need no GPU, and they scored 92 % accuracy.
  Future work would be a trained model on Sentinel-2 bands.
- *How do you know it's government land?* **We don't, and the app says so.**
  OSM doesn't record ownership. "Preferred" means "tags that usually indicate
  common land". The parcel feature is how an administrator supplies the real
  answer from revenue records (Bhu-Naksha).
- *Why is OSM the main exclusion and not the satellite?* The validation drew
  only 2 built-up cells, so satellite built-up accuracy isn't proven. OSM
  buildings are exact footprints.

### Step 4 — Choosing sites (`app/pond.py`)

**`select_pond_site`** scores every candidate cell:

```
score = 0.75 × (0.45·flow + 0.35·flatness + 0.20·hollowness) + 0.25·land
```

- *flow* = log of accumulation, scaled 0–1: water must arrive.
- *flatness* = 1 − scaled slope: excavation is stable and cheap.
- *hollowness* = negative Laplacian (curvature): natural bowls hold water.
- *land* = the land score from step 3.

Candidates must be on the stream network (top 8 % accumulation), on allowed
land, away from the map edge, and not affected by edge inflow. If allowed land
doesn't touch streams, the threshold relaxes step by step.

**`rank_sites`** (Algorithm 1 in the report) goes down the list from the best
score and accepts a cell only if:
1. it is ≥ 250 m from accepted sites;
2. it is **not upstream** of an accepted site;
3. it is **not downstream** of one.

So the five ponds have **separate catchments**. That means their water can be
added up without counting it twice.

**Likely questions**
- *Why those weights?* They encode priorities: water arriving matters most,
  then stability, then shape. They're function arguments, so they could be
  tuned. Future work: learn them from records of successful ponds.
- *Why not just take the top 5 scores?* They'd be 5 neighbouring cells on the
  same stream.

### Step 5 — Rainfall (`app/rainfall.py`)

- **CHIRPS** (0.05° ≈ 5 km, satellite + rain gauges), via NASA SERVIR
  **ClimateSERV**. 20 years is requested as 2 chunks of 10 years **in
  parallel**, because the server rejects long requests.
- **Fallbacks:** Open-Meteo (ERA5), then NASA POWER.
- **ET₀** (evaporation demand) comes from Open-Meteo, or from NASA temperatures
  with the Hargreaves formula.
- **Cache:** results go into SQLite (`api_cache`), so the second run is
  instant.
- **Statistics:** mean, **75 % dependable** (the 25th percentile of annual
  totals, i.e. rain you get in 3 years out of 4), CV, driest and wettest year,
  rainy days (≥ 2.5 mm, the IMD definition), monsoon share, monthly means.
- **Optional scaling:** if the user knows the IMD normal, every day is
  multiplied by `normal / dataset mean`.

**Likely questions**
- *Why CHIRPS and not IMD?* IMD gridded data is bulk file downloads, not an
  API. I compared CHIRPS with IMD station normals at 6 stations and got an
  8.9 % mean error.
- *Why not NASA POWER, which is faster?* Its ~50 km cells mixed the wet Ghats
  into Pune: +92 % error. That finding is why CHIRPS became the default.

### Step 6 — Runoff (`app/runoff.py`)

This is the **SCS Curve Number** method, applied **every day**:
```
S  = 25400/CN − 254        (how much the soil can absorb, in mm)
Ia = 0.2·S                 (rain absorbed before any runoff starts)
Q  = (P − Ia)² / (P − Ia + S)   if P > Ia, else 0
```

- **CN** (0–100) comes from a table by land cover and soil group. Higher CN
  means more runoff: concrete is 98, forest on sand is 36.
- Runoff is computed for each land-cover class and then weighted by area. This
  is more accurate than averaging CN, because the formula isn't linear.
- **Antecedent moisture (AMC):** if the previous 5 days had less than 35.6 mm,
  the soil is dry, so CN is lowered. If they had more than 53.3 mm, the soil is
  wet, so CN is raised (Hawkins' equations).
- **Rational method** (area × rain × C) is shown as a cross-check.

**Worked example (from the tests).** P = 100 mm, CN = 80:
S = 25400/80 − 254 = 63.5; Ia = 12.7; Q = (87.3)²/(87.3 + 63.5) = **50.5 mm**.

**Likely questions**
- *Why daily and not annual?* The same 700 mm falling as 70 small showers gives
  almost no runoff; as 10 big storms it gives a lot. Only a daily model sees
  that.
- *Why is SCS-CN runoff 2.4× smaller than the rational method?* The rational
  method uses one fixed fraction of all rain. SCS-CN knows that light rain
  soaks in completely.
- *What matters most?* Sensitivity analysis: rainfall has an **elasticity of
  about 3** (10 % more rain gives about 30 % more runoff), and soil group A vs
  D changes runoff 4.7×.

### Step 7 — Pond design (`app/design.py`)

- **Shape:** a rectangle, 1.5 × as long as wide, with sloping sides of 1.5
  horizontal : 1 vertical (it's a *frustum*, an upside-down cut-off pyramid).
  The long side runs along the contour.
- **Volume:** the prismoidal formula `V = d/6 × (A_top + 4·A_mid + A_bottom)`,
  which is exact for this shape. The width for a target volume is found by
  bisection (`width_for_volume`).
- **Target storage:** 50 % of the 75 %-dependable runoff, so the pond fills
  about **twice** even in a dry year.
- **Depth:**
  1. Start at 3 m (MGNREGA / Amrit Sarovar practice).
  2. If the pond doesn't fit on the available land (80 % of the patch) or the
     2 ha cap, go deeper, up to 4.5 m.
  3. If it still doesn't fit, cap the volume and say so.
  4. Small ponds go shallower so the bed is at least 4 m wide for machines.
- **Also reported:** freeboard 0.5 m, earthwork volume, dry-season evaporation
  (1.05 × ET₀ from October to May × the water area at half depth), seepage by
  soil type, and lining advice for sandy soils.

**Likely questions**
- *Why deeper rather than bigger?* Evaporation depends on surface area. At
  Hiware Bazar about 44 % of storage evaporates over the dry season, so a
  deeper pond loses a smaller share.
- *Why 50 % of dependable runoff?* A pond that fills every year is better than
  a huge one that is rarely full. The 50 % is a parameter (`fill_fraction`).

### Step 8 — Map layers (`app/layers.py`)

- **Contours:** threshold the DEM at each level and `cv2.findContours`.
- **Drainage lines:** follow D8 from stream heads.
- **Land polygons:** `findContours` on the land mask.
- **Relief and land-cover images:** PNG overlays.

### Step 9 — API and database (`app/main.py`, `app/schemas.py`, `app/db.py`)

- **FastAPI** routes. **Pydantic** models validate input (for example, lat must
  be between −60 and 60) and automatically create the `/docs` page.
- **SQLite** has two tables: `analyses` (history) and `api_cache` (rainfall
  and geocoding).
- *Why SQLite, not PostgreSQL?* Small data, one writer, zero setup. All
  database code is in `db.py`, so switching would change only that file.

### Step 10 — Front end (`app/static/`)

This is Leaflet (map), Leaflet.draw (drawing parcels) and Chart.js (charts),
with plain JavaScript and no framework.

`render()` in `app.js` draws everything. Clicking a candidate row calls
`render(base, i)` to show that candidate.

Accessibility: keyboard navigation, ARIA tabs, focus outlines, WCAG AA
contrast, and a reduced-motion setting.

---

## Part 3 — Numbers to remember

| Thing | Value |
|---|---|
| Grid | 200 × 200 cells, 20 m, 4 × 4 km |
| Hiware Bazar best pond | 141 × 94 m, 3 m deep, **36,856 m³**, catchment 138 ha |
| Five candidates combined | 121,155 m³ |
| Rainfall at Hiware Bazar | 713 mm (CHIRPS), dependable 599 mm |
| Runoff at the best site | 134,368 m³/yr, effective C 0.14, CN 85.8 |
| Evaporation loss | 44 % of storage over October–May |
| Rainfall validation | CHIRPS 8.9 % error vs NASA 26.1 % (Pune +92 %) |
| Land-cover accuracy | 92 %, κ = 0.85, 87 cells |
| DEM vs summits | median error 20 m (summits = worst case) |
| Tests | 23 passing |
| Timing | ~40 s first run (rainfall download), ~2.5 s cached |

---

## Part 4 — Tricky questions

**"What happens if an API is down?"**
Every source has a fallback: tiles → Open-Meteo elevation; CHIRPS → Open-Meteo
→ NASA; if imagery or OSM is missing, siting uses what's left. Network errors
are retried three times. Whatever failed appears in `warnings` and on screen.

**"How do you know your catchment is right?"**
A unit test builds a V-shaped valley whose exact catchment can be written as a
formula. The code matches it cell for cell. Real catchments weren't compared
with official watershed maps; that is listed under limitations.

**"Your runoff — has it been validated?"**
No. There's no measured stream-flow data for village catchments. The formula
is tested, the method is the Indian standard, and the sensitivity analysis
shows which inputs matter.

**"Why is κ (kappa) useful?"**
92 % accuracy could partly happen by chance if one class dominates. Kappa
measures agreement *beyond* chance: 0.85 counts as "almost perfect".

**"What's the most important limitation?"**
Rainfall and soil type dominate the uncertainty, and resolution (30 m) hides
small bunds and drains. So it's a *pre-feasibility* tool, and an engineer
still visits the site.

**"If you had another month?"**
- Sentinel-2 bands for better land cover (NDVI/NDWI).
- SoilGrids for automatic soil group.
- Cost per m³.
- Learning the score weights from existing successful ponds.
- IMD gridded rainfall.

---

## Part 5 — Demo script (5 minutes)

1. Open the app. Point out the three steps on the welcome card.
2. Search **Hiware Bazar** → click the result. The dashed square is the
   4 × 4 km area.
3. *(Optional)* Draw a small parcel to show government-land restriction.
4. Click **Analyse village**. While the overlay runs, explain the stages it
   lists.
5. Walk through the results:
   - the hero card (storage, size);
   - the KPIs;
   - the **candidate table**. Click #3 to show that the map and every panel
     switch.
6. **Pond** tab: the cross-section. **Rainfall** tab: the charts and the
   75 % dependable figure. **Runoff** tab: CN and the land-cover bar.
   **Site & land** tab.
7. Layer control: turn on **Land-cover classification**. Point at the village
   detected as red (built-up).
8. **Zoom to pond**: the footprint rectangle across the drainage line.
9. Open `/docs` to show the API documentation.
10. If asked for proof, run `pytest` (23 pass) and show `VALIDATION.md`.

---

## Part 6 — Self-test

Answer each without looking. If you can't, reread that section.

1. What does Priority-Flood do, and why is it needed?
2. How does D8 choose a direction? Why divide by √2?
3. Why does sorting by height make flow accumulation work?
4. How is a catchment found from a pond location?
5. What is GRVI and why use Otsu?
6. How does the app avoid calling field boundaries "villages"?
7. Why can the five candidate ponds' water be added together?
8. Write the SCS-CN equations. What does AMC change?
9. What is "75 % dependable" rainfall?
10. Why CHIRPS instead of NASA POWER? Give the Pune number.
11. How is pond depth chosen?
12. What is κ and what was yours?
13. What happens when Overpass is down?
14. Why SQLite? Why FastAPI?
