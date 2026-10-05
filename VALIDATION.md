# Validation

How much can the numbers be trusted? This document covers two kinds of
evidence:

1. **Unit tests** that check every formula against an independent hand
   calculation or an exact analytic answer.
2. **Accuracy checks** that compare each data source against an external
   reference.

The scripts and raw results are in `validation/`. The tests are in `tests/`.

Summary:

| Component | Reference | Result |
|---|---|---|
| Formulas (runoff, pond volume, radiation, statistics, catchment) | Hand calculations, analytic geometry, FAO-56 / TR-55 worked examples | **35 / 35 tests pass** |
| Catchment delineation | Synthetic V-valley with an exact analytic catchment | Identical, cell for cell; polygon area = cell count |
| Rainfall — CHIRPS (default) | IMD 1991–2020 station normals, 6 stations | Mean absolute error **8.9 %**, median 7.0 %, bias −1.5 % |
| Rainfall — NASA POWER (previous default) | Same | Mean absolute error 26.1 %; **+92 % at Pune** |
| Elevation (AWS Terrain Tiles) | 44 OpenStreetMap summit elevations, 3 regions | Median error **20 m**, bias −27 m (summits smoothed; worst case) |
| Land-cover classifier (OpenCV) | 100 blind hand-labelled 20 m cells, 4 landscapes | **92 % overall accuracy, κ = 0.85** (87 unmixed cells) |

---

## 1. Unit tests (`tests/`)

Run the tests with:

```bash
.venv/bin/python -m pytest -q tests
```

The result is `35 passed`.

| Test | Independent reference |
|---|---|
| SCS runoff, P = 100 mm, CN = 80 | Hand calculation: S = 63.5, Ia = 12.7, Q = 87.3² / 150.8 = **50.54 mm** |
| SCS runoff, 4 more (P, CN) pairs | The closed-form TR-55 equation |
| No runoff below Ia | CN 70 gives Ia = 21.8 mm, so a 20 mm storm must produce 0 |
| AMC I / III conversion | Hawkins (1985): CN II 75 gives 56.8 (AMC I) and 87.0 (AMC III) |
| Runoff volume | Runoff depth × catchment area |
| Frustum, square pond | 20 × 20 m top, 2 m deep, 1:1 slopes: V = h/3 (A₁ + A₂ + √(A₁A₂)) = 650.67 m³ |
| Frustum, rectangular pond | Prismoidal formula computed by hand |
| Width solver | Round trip: the width found for V gives back V (3 volumes) |
| Pond sizing rules | Hits the target; goes deeper on small land; caps storage and says so |
| Extraterrestrial radiation | FAO-56 Example 8: 20° S, 3 September gives Ra = 32.2 MJ m⁻² d⁻¹ |
| Rainfall statistics | Known series: mean, 75 % dependable, driest year, rainy days, ET₀ |
| Depression filling | No interior sinks remain; the surface is never lowered |
| **V-valley catchment** | Analytic answer: every cell with row − \|col − centre\| ≥ outlet row. The result matches **cell for cell**, including the accumulation count |
| Boundary polygon | The shoelace area of the traced ring equals the catchment cell count |
| Bowl with channel | All cells drain to the single outlet |
| Edge-inflow flag | Flags a valley entering from the map edge; does not flag a hill-top |
| Sea exclusion (3 tests) | Coastal valley whose outlet is in the sea: no site is placed on a sea cell, even when no land is available |
| Sea mask | Flat 0 m sea, bathymetry and shallow seabed joined to it are sea; an inland polder at −1.5 m is not |
| OpenStreetMap cache (2 tests) | A repeated query is served from the cache; after a failure, the next query fails fast instead of waiting on Overpass again |

## 2. Rainfall (`validation/rainfall_check.py`)

Each dataset's 1991–2020 mean annual rainfall at the station location is
compared with the IMD 1991–2020 normal for that observatory. The source is
*Climatological Tables of Observatories in India 1991–2020*, as tabulated on
the stations' Wikipedia pages. Pune's figure is cited there to the Tokyo
Climate Center, which publishes WMO normals derived from IMD data.

| Station | Climate | IMD normal (mm) | CHIRPS (mm) | Error | NASA POWER (mm) | Error |
|---|---|---:|---:|---:|---:|---:|
| Ahmednagar | semi-arid, rain shadow | 608.1 | 658.1 | +8.2 % | 652.0 | +7.2 % |
| Pune (Shivajinagar) | leeward Western Ghats | 841.2 | 897.9 | +6.7 % | 1,616.7 | **+92.2 %** |
| Jaisalmer | arid | 256.9 | 184.8 | −28.1 % | 210.1 | −18.2 % |
| Bengaluru | semi-arid plateau | 1,077.8 | 1,054.0 | −2.2 % | 858.7 | −20.3 % |
| Chennai (Airport) | NE-monsoon coast | 1,408.4 | 1,396.8 | −0.8 % | 1,237.9 | −12.1 % |
| Mumbai (Santacruz) | windward coast | 2,502.3 | 2,684.6 | +7.3 % | 2,341.7 | −6.4 % |
| **Mean absolute error** | | | | **8.9 %** | | **26.1 %** |

Open-Meteo (ERA5) could not be scored: its daily request limit for this IP
was exhausted on the day of testing (HTTP 429).

**What changed because of this check.** NASA POWER's ~50 km cells mix wet
Western Ghats slopes with the dry plateau behind them, which is where most
pond planning happens; Pune reads almost double its true rainfall. CHIRPS
(~5 km, calibrated against rain gauges) is now the default source. The other
two datasets remain as automatic fallbacks, and the source used is always
reported.

CHIRPS also resolves the *storm pattern* better. At Hiware Bazar it gives 53
rainy days a year, against NASA POWER's 82 spread-out drizzle days. That
matters for daily SCS-CN runoff, because a few heavy storms produce more
runoff per millimetre than many light ones.

**Residual error.** CHIRPS underestimates arid western Rajasthan (−28 % at
Jaisalmer), a known weakness of satellite rainfall in deserts. When an
administrator knows the IMD district normal, they can enter it as *Local
rainfall normal*. The daily series is then linearly scaled to that normal,
which keeps the storm pattern.

**Why it matters (sensitivity).** For Ralegan Siddhi, scaling CHIRPS from
697 mm to the IMD normal of 608 mm (−13 % rain) cuts runoff by **35 %**
(161,913 → 105,883 m³), and the recommended pond shrinks to match. Curve-number
runoff is strongly nonlinear, so the rainfall dataset is the single most
important input to the pond size.

## 3. Elevation (`validation/dem_check.py`)

The DEM (AWS Terrain Tiles at zoom 13, bilinear) is compared at every
OpenStreetMap `natural=peak` node that has an `ele` tag in the test regions.

| Region | n | Bias (m) | Median \|error\| (m) | RMSE (m) |
|---|---:|---:|---:|---:|
| Sahyadri (Pune–Ahilyanagar) | 18 | −25.9 | 14.1 | 64.0 |
| Deccan plateau (Karnataka) | 6 | −60.3 | 53.1 | 79.6 |
| Nilgiris / Western Ghats | 20 | −17.1 | 17.8 | 24.9 |
| **All** | **44** | **−26.6** | **19.9** | **53.1** |

(The Aravalli test region had no tagged peaks.)

Summits are the worst case for a ~30 m DEM. A sharp peak is averaged with
the slopes around it, hence the consistently negative bias, and OSM summit
positions are themselves approximate. Pond siting uses valley floors and
*relative* heights (slope, flow direction, catchment divides) across gentle
terrain, where the vertical error is much smaller (SRTM mission
specification: 16 m absolute and 10 m relative vertical accuracy at 90 %;
measured errors on gentle terrain are usually lower). The main practical
limitation is resolution: bunds, farm drains and road embankments narrower
than ~30 m are invisible to the DEM. Field verification of the chosen site
remains necessary.

## 4. Land-cover classifier (`validation/landcover_*.py`)

**Protocol.**

1. Four contrasting landscapes were chosen: semi-arid Maharashtra, irrigated
   Punjab, wet Kerala (Kuttanad) and arid Rajasthan.
2. In each, 25 of the classifier's 20 m cells were drawn at random (seed 42),
   100 cells in total.
3. For each cell, a 100 m chip of zoom-18 imagery (about 4× finer than the
   classifier input) was rendered with the cell outlined
   (`validation/chips/sheet_*.jpg`). The classifier's output was saved
   separately and not shown on the chips.
4. Each cell was labelled by eye with its dominant cover: vegetation, open
   land, built-up or water. Cells split roughly evenly between two classes
   were labelled *mixed* and excluded (13 of 100).
5. The labels were then scored against the predictions
   (`validation/landcover_score.py`).

The labels were made by the developer, not by an independent field survey.

**Results (87 cells).** Overall accuracy is **92.0 %** and Cohen's
κ = **0.845**.

| True \ Predicted | Vegetation | Open land | Built-up | Water |
|---|---:|---:|---:|---:|
| Vegetation | **33** | 3 | 1 | 0 |
| Open land | 3 | **45** | 0 | 0 |
| Built-up | 0 | 0 | **2** | 0 |

| Site | Correct |
|---|---|
| Hiware Bazar, MH (semi-arid) | 20 / 21 |
| Ludhiana rural, PB (irrigated) | 20 / 20 |
| Kuttanad, KL (wet) | 21 / 23 |
| Jaipur rural, RJ (arid) | 19 / 23 |

**Errors.**

- Three of the seven errors are pale-green sparse grass in arid Rajasthan,
  which the classifier calls vegetation. That is a borderline call either
  way.
- Two are cells with a single tree, where the tree covers roughly half the
  cell.
- Random sampling drew only 2 built-up cells and no water cells, so the
  accuracy of those classes is not established by this sample.

In the app, buildings, roads and water bodies are excluded primarily from
OpenStreetMap, with a safety buffer. The satellite built-up and water classes
are a secondary safeguard.

## 5. What has not been validated

- **Runoff volumes against measured stream flow.** There are no gauged
  village catchments in open data. The method (daily SCS-CN, AMC-adjusted) is
  the standard in Indian watershed manuals, and its formula is tested, but
  curve numbers carry roughly ±15–20 % uncertainty.
- **Catchment boundaries against surveyed watershed maps.** They were
  checked against an analytic surface, not against Bhuvan or other official
  micro-watershed boundaries.
- **Land ownership.** Open data does not contain it; see the parcel feature.
- **Pond siting against sites engineers actually chose.** This would be the
  natural next step with MGNREGA / Amrit Sarovar site records.
