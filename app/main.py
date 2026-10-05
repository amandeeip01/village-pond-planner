"""
main.py
-------
FastAPI app for the Village Pond Planning System.

    GET  /                        interactive map frontend
    GET  /api/geocode?q=          village search (OpenStreetMap Nominatim)
    GET  /api/reverse?lat=&lon=   place name for a map click
    POST /api/analyze             full analysis for a village location (JSON body)
    POST /analyzeContour          full analysis from an uploaded KML/KMZ contour map
    GET  /api/rainfall            historical rainfall statistics for a point
    GET  /api/analyses            saved analyses (SQLite)
    GET  /api/analyses/{id}       one saved analysis
    DELETE /api/analyses/{id}     delete a saved analysis
    GET  /health                  liveness probe
    GET  /docs                    interactive API documentation
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Real env vars already set in the shell take precedence over values in .env,
# which is the right behaviour for production deployments.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

from . import db, geocode, pipeline
from .elevation import ElevationError, fetch_dem
from .kml_parser import KMLParseError, parse_contours
from .layers import kml_contours
from .rainfall import RainfallError, fetch_daily, summarise
from .schemas import AnalysisResponse, AnalyzeRequest
from .terrain import build_dem, dem_stats

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
VERSION = "2.0.0"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="Village Pond Planning System",
    version=VERSION,
    description=(
        "Recommends pond locations for a village: reconstructs terrain from an elevation "
        "service or an uploaded contour map, identifies land available for excavation from "
        "satellite imagery and OpenStreetMap, delineates the catchment, retrieves historical "
        "rainfall, estimates runoff and recommends pond depth and storage capacity."
    ),
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"status": "error", "detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    msgs = [f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in exc.errors()]
    return JSONResponse(status_code=422, content={"status": "error", "detail": "; ".join(msgs)})


@app.get("/health")
def health():
    return {"status": "ok", "service": "village-pond-planner", "version": VERSION}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def root():
    """Serve the map frontend, injecting the optional MapTiler key from the environment."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    key = os.environ.get("MAPTILER_KEY", "")
    html = html.replace('window.__MAPTILER_KEY__ || ""', json.dumps(key))
    return HTMLResponse(content=html)


# --------------------------------------------------------------------------
# Location lookup
# --------------------------------------------------------------------------
@app.get("/api/geocode", summary="Search for a village by name")
def api_geocode(q: str = Query(..., min_length=2, max_length=200),
                country: str | None = Query("in", description="ISO country code filter; empty for worldwide")):
    try:
        return {"results": geocode.search(q, country or None)}
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Geocoding service unavailable: {exc}")


@app.get("/api/reverse", summary="Place name for a coordinate")
def api_reverse(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180)):
    try:
        return geocode.reverse(lat, lon) or {}
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Geocoding service unavailable: {exc}")


@app.get("/api/rainfall", summary="Historical rainfall statistics for a point")
def api_rainfall(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
                 years: int = Query(20, ge=5, le=40),
                 source: str = Query("auto", pattern="^(auto|chirps|open_meteo|nasa_power)$"),
                 annual_normal_mm: float | None = Query(None, gt=0, le=15000)):
    try:
        return summarise(fetch_daily(lat, lon, years, source=source, annual_normal_mm=annual_normal_mm))
    except RainfallError as exc:
        raise HTTPException(502, str(exc))


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
def _options(req: AnalyzeRequest | None = None, **kw) -> pipeline.Options:
    if req is not None:
        kw = {
            "village": req.village, "soil_group": req.soil_group, "fill_fraction": req.fill_fraction,
            "side_slope": req.side_slope, "max_depth_m": req.max_depth_m,
            "rainfall_years": req.rainfall_years, "max_slope_deg": req.max_slope_deg,
            "rainfall_source": req.rainfall_source, "annual_normal_mm": req.annual_normal_mm,
            "n_candidates": req.n_candidates,
            "runoff_coefficient": req.runoff_coefficient, "fallback_rainfall_mm": req.fallback_rainfall_mm,
            "parcels": req.land_parcels,
            "pond_lonlat": (req.pond_lon, req.pond_lat) if req.pond_lat is not None and req.pond_lon is not None else None,
        } | kw
    return pipeline.Options(**kw)


@app.post("/api/analyze", response_model=AnalysisResponse,
          summary="Analyse a village location and recommend a pond")
def api_analyze(req: AnalyzeRequest):
    try:
        dem, meta = fetch_dem(req.lon, req.lat, req.radius_m, req.grid_resolution)
    except ElevationError as exc:
        raise HTTPException(502, str(exc))
    info = dem_stats(dem, {"method": f"Resampled from {meta['elevation_source']} (bilinear)", **meta})
    source = {"type": "elevation_service", "center": {"lat": req.lat, "lon": req.lon},
              "radius_m": req.radius_m, **meta}
    try:
        return pipeline.run(dem, info, mode="village", source_meta=source, opts=_options(req))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.post("/analyzeContour", response_model=AnalysisResponse,
          summary="Analyse an uploaded contour map (KML/KMZ) and recommend a pond")
@app.post("/findCatchment", response_model=AnalysisResponse, include_in_schema=False)
def analyze_contour(
    file: UploadFile = File(..., description="Contour map, .kml or .kmz"),
    land_parcels: str | None = Form(None, description="Optional GeoJSON of government land parcels"),
    village: str | None = Query(None, max_length=200),
    dam_height_m: float = Query(3.0, gt=0, le=30, description="Bund height for the natural-basin storage estimate"),
    annual_rainfall_mm: float = Query(800.0, gt=0, description="Used only if the rainfall APIs are unavailable"),
    runoff_coefficient: float | None = Query(None, gt=0, le=1, description="Override the land-cover derived rational C"),
    grid_resolution: int = Query(260, ge=60, le=420, description="DEM cells along the longer axis"),
    soil_group: str = Query("C", pattern="^[ABCD]$"),
    fill_fraction: float = Query(0.5, gt=0, le=1.5),
    rainfall_years: int = Query(20, ge=5, le=40),
    rainfall_source: str = Query("auto", pattern="^(auto|chirps|open_meteo|nasa_power)$"),
    annual_normal_mm: float | None = Query(None, gt=0, le=15000),
    analyze_land: bool = Query(True, description="Fetch satellite imagery and OSM to find available land"),
    fetch_rainfall: bool = Query(True, description="Fetch historical rainfall from public APIs"),
    pond_lat: float | None = Query(None),
    pond_lon: float | None = Query(None),
):
    raw = file.file.read()
    if not raw:
        raise HTTPException(400, "Uploaded file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.")

    try:
        contours, source_meta = parse_contours(raw)
    except KMLParseError as exc:
        raise HTTPException(422, str(exc))
    if len(contours) < 3:
        raise HTTPException(422, "Need at least 3 contour lines to build a terrain surface.")

    try:
        dem, stats = build_dem(contours, target_cells=grid_resolution)
    except Exception as exc:
        raise HTTPException(422, f"Could not build a terrain grid: {exc}")
    stats["method"] = "Delaunay linear interpolation of contour vertices, nearest-neighbour fill outside hull"

    parcels = None
    if land_parcels:
        try:
            parcels = json.loads(land_parcels)
        except json.JSONDecodeError:
            raise HTTPException(422, "land_parcels is not valid GeoJSON.")

    opts = _options(
        village=village, dam_height_m=dam_height_m, fallback_rainfall_mm=annual_rainfall_mm,
        runoff_coefficient=runoff_coefficient, soil_group=soil_group, fill_fraction=fill_fraction,
        rainfall_years=rainfall_years, rainfall_source=rainfall_source,
        annual_normal_mm=annual_normal_mm, analyze_land=analyze_land, fetch_rainfall=fetch_rainfall,
        parcels=parcels, require_complete_catchment=False,
        pond_lonlat=(pond_lon, pond_lat) if pond_lat is not None and pond_lon is not None else None,
    )
    source = {"type": "uploaded_contours", "filename": file.filename, "size_bytes": len(raw), **source_meta}
    try:
        return pipeline.run(dem, stats, mode="contour_upload", source_meta=source, opts=opts,
                            contour_layer=kml_contours(contours))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


# --------------------------------------------------------------------------
# Saved analyses
# --------------------------------------------------------------------------
@app.get("/api/analyses", summary="List saved analyses")
def api_list(limit: int = Query(50, ge=1, le=500)):
    return {"analyses": db.list_analyses(limit)}


@app.get("/api/analyses/{analysis_id}", summary="Retrieve a saved analysis")
def api_get(analysis_id: int):
    result = db.get_analysis(analysis_id)
    if result is None:
        raise HTTPException(404, "Analysis not found.")
    return result


@app.delete("/api/analyses/{analysis_id}", summary="Delete a saved analysis")
def api_delete(analysis_id: int):
    if not db.delete_analysis(analysis_id):
        raise HTTPException(404, "Analysis not found.")
    return {"status": "deleted", "id": analysis_id}
