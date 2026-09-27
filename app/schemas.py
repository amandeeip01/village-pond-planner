"""
schemas.py
----------
Pydantic models. These define the JSON contract, and FastAPI uses them to
generate the OpenAPI docs automatically.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Point(BaseModel):
    lon: float
    lat: float
    elevation_m: float


class PondSite(BaseModel):
    location: Point
    slope_deg: float = Field(..., description="Ground slope at the site")
    upstream_cells: int
    on_available_land: bool | None = Field(None, description="Site lies on land classified as available")
    land_tier: str | None = Field(None, description="preferred / open_land / farmland / excluded")
    reason: str


class Catchment(BaseModel):
    area_m2: float
    area_hectares: float
    area_km2: float
    perimeter_m: float
    elevation_min_m: float
    elevation_max_m: float
    relief_m: float
    mean_slope_deg: float
    centroid: dict[str, float]
    touches_analysis_edge: bool = Field(False, description="Catchment receives inflow from beyond the map edge")
    boundary_geojson: dict[str, Any] = Field(
        ..., description="GeoJSON Polygon of the catchment divide (WGS84)"
    )


class AnalysisResponse(BaseModel):
    status: str
    mode: str
    village: str | None = None
    analysis_id: int | None = None
    source_map: dict[str, Any]
    dem: dict[str, Any]
    pond_site: PondSite
    catchment: Catchment
    land: dict[str, Any] | None = Field(None, description="Land availability assessment")
    rainfall: dict[str, Any] | None = Field(None, description="Historical rainfall statistics")
    runoff: dict[str, Any] = Field(..., description="Runoff volume estimate")
    pond_design: dict[str, Any] = Field(..., description="Recommended pond dimensions and storage")
    storage: dict[str, Any] = Field(..., description="Natural basin storage behind a bund of dam_height_m")
    water_yield: dict[str, Any]
    candidates: list[dict[str, Any]] = Field(
        default_factory=list, description="Ranked alternative pond sites (rank 1 = the headline site)")
    layers: dict[str, Any] = Field(..., description="GeoJSON and image overlays for the map")
    warnings: list[str]
    method: dict[str, Any]
    processing_seconds: float


class AnalyzeRequest(BaseModel):
    lat: float = Field(..., ge=-60, le=60, description="Village centre latitude")
    lon: float = Field(..., ge=-180, le=180, description="Village centre longitude")
    village: str | None = Field(None, max_length=200)
    radius_m: float = Field(2000, ge=500, le=6000, description="Half-width of the square analysis area")
    grid_resolution: int = Field(200, ge=60, le=320, description="DEM cells along each side")
    pond_lat: float | None = Field(None, description="Optional user-chosen pond latitude")
    pond_lon: float | None = Field(None, description="Optional user-chosen pond longitude")
    soil_group: Literal["A", "B", "C", "D"] = Field("C", description="NRCS hydrologic soil group")
    rainfall_years: int = Field(20, ge=5, le=40)
    rainfall_source: Literal["auto", "chirps", "open_meteo", "nasa_power"] = Field(
        "auto", description="Precipitation dataset; auto = CHIRPS, then Open-Meteo, then NASA POWER")
    annual_normal_mm: float | None = Field(
        None, gt=0, le=15000, description="Optional local long-term normal (e.g. IMD) to scale the series to")
    fill_fraction: float = Field(0.5, gt=0, le=1.5,
                                 description="Pond storage as a fraction of 75%-dependable annual runoff")
    side_slope: float = Field(1.5, ge=1.0, le=3.0, description="Excavation side slope, horizontal per vertical")
    max_depth_m: float = Field(4.5, ge=2.0, le=6.0)
    n_candidates: int = Field(5, ge=1, le=10, description="How many ranked candidate sites to return")
    max_slope_deg: float = Field(8.0, gt=0, le=30, description="Steepest ground allowed for excavation")
    runoff_coefficient: float | None = Field(None, gt=0, le=1,
                                             description="Override the land-cover derived rational C")
    fallback_rainfall_mm: float = Field(800.0, gt=0, description="Used only if rainfall APIs fail")
    land_parcels: dict[str, Any] | None = Field(
        None, description="GeoJSON of government land parcels; siting is restricted to them")


class ErrorResponse(BaseModel):
    status: str = "error"
    detail: str
