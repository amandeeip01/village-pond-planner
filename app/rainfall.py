"""
rainfall.py
-----------
Historical rainfall for the pond site from public APIs.

Precipitation sources, in order of preference ("auto"):
  1. CHIRPS v2 (Climate Hazards Center, UCSB) via NASA/USAID SERVIR
     ClimateSERV - 0.05 deg (~5 km) daily rainfall blending satellite
     estimates with rain-gauge data. Validated against IMD station normals
     (see VALIDATION.md) it is far closer than the reanalyses below, whose
     10-50 km cells overestimate rain-shadow areas such as the Deccan plateau.
  2. Open-Meteo Historical Weather API - ERA5 reanalysis.
  3. NASA POWER - MERRA-2 based, bias-corrected (PRECTOTCORR).

Reference evapotranspiration (ET0) comes from Open-Meteo (FAO-56
Penman-Monteith) or, failing that, from NASA POWER temperatures with the
Hargreaves-Samani equation. CHIRPS is slow (~1 min for 20 years), so it is
requested in parallel with the ET0 source, and every response is cached.

If the administrator knows the local long-term normal (e.g. the IMD district
normal), the daily series can be linearly scaled to it - a standard
bias-correction that keeps the day-to-day storm pattern.

The daily series is kept (not just annual totals) because runoff is computed
day by day with the SCS Curve Number method: 1000 mm falling as 100 storms of
10 mm produces far less runoff than 1000 mm in 20 storms of 50 mm.

Statistics follow Indian practice where it matters:
  * rainy day      = a day with >= 2.5 mm (IMD definition)
  * dependable     = 75 % dependable rainfall, i.e. the annual total that is
                     equalled or exceeded in 3 years out of 4 (25th percentile)
"""

from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import httpx
import numpy as np

from . import db
from .tiles import USER_AGENT

CACHE_MAX_AGE_S = 30 * 24 * 3600
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class RainfallError(RuntimeError):
    pass


def _year_range(years: int) -> tuple[int, int]:
    """Last `years` complete calendar years."""
    end = date.today().year - 1
    return end - years + 1, end


def _open_meteo(lat: float, lon: float, y0: int, y1: int) -> dict:
    resp = httpx.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
            "start_date": f"{y0}-01-01", "end_date": f"{y1}-12-31",
            "daily": "precipitation_sum,et0_fao_evapotranspiration",
            "timezone": "auto",
        },
        headers={"User-Agent": USER_AGENT}, timeout=60,
    )
    if resp.status_code != 200:
        raise RainfallError(f"Open-Meteo: HTTP {resp.status_code} {resp.text[:120]}")
    daily = resp.json()["daily"]
    return {
        "source": "Open-Meteo Historical Weather API (ERA5 reanalysis)",
        "source_url": "https://open-meteo.com/en/docs/historical-weather-api",
        "dates": daily["time"],
        "precip": [v if v is not None else 0.0 for v in daily["precipitation_sum"]],
        "et0": [v if v is not None else float("nan") for v in daily["et0_fao_evapotranspiration"]],
        "et0_method": "FAO-56 Penman-Monteith (Open-Meteo)",
    }


def _extraterrestrial_radiation(lat: float, doy: np.ndarray) -> np.ndarray:
    """Ra in MJ m-2 day-1 (FAO-56 eq. 21)."""
    phi = math.radians(lat)
    dr = 1 + 0.033 * np.cos(2 * np.pi * doy / 365)
    delta = 0.409 * np.sin(2 * np.pi * doy / 365 - 1.39)
    ws = np.arccos(np.clip(-math.tan(phi) * np.tan(delta), -1, 1))
    return (24 * 60 / np.pi) * 0.0820 * dr * (
        ws * math.sin(phi) * np.sin(delta) + math.cos(phi) * np.cos(delta) * np.sin(ws)
    )


def _nasa_power(lat: float, lon: float, y0: int, y1: int) -> dict:
    resp = httpx.get(
        "https://power.larc.nasa.gov/api/temporal/daily/point",
        params={
            "parameters": "PRECTOTCORR,T2M_MAX,T2M_MIN", "community": "AG",
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
            "start": f"{y0}0101", "end": f"{y1}1231", "format": "JSON",
        },
        headers={"User-Agent": USER_AGENT}, timeout=90,
    )
    if resp.status_code != 200:
        raise RainfallError(f"NASA POWER: HTTP {resp.status_code} {resp.text[:120]}")
    body = resp.json()
    params = body["properties"]["parameter"]
    fill = body.get("header", {}).get("fill_value", -999.0)
    keys = sorted(params["PRECTOTCORR"].keys())
    precip = np.array([params["PRECTOTCORR"][k] for k in keys], dtype=float)
    tmax = np.array([params["T2M_MAX"][k] for k in keys], dtype=float)
    tmin = np.array([params["T2M_MIN"][k] for k in keys], dtype=float)
    precip[precip == fill] = 0.0
    bad = (tmax == fill) | (tmin == fill)
    dates = [f"{k[:4]}-{k[4:6]}-{k[6:]}" for k in keys]
    doy = np.array([date.fromisoformat(d).timetuple().tm_yday for d in dates])
    ra_mm = 0.408 * _extraterrestrial_radiation(lat, doy)
    et0 = 0.0023 * ra_mm * ((tmax + tmin) / 2 + 17.8) * np.sqrt(np.clip(tmax - tmin, 0, None))
    et0[bad] = np.nan
    return {
        "source": "NASA POWER daily point API (MERRA-2, bias-corrected precipitation)",
        "source_url": "https://power.larc.nasa.gov/",
        "dates": dates,
        "precip": precip.tolist(),
        "et0": et0.tolist(),
        "et0_method": "Hargreaves-Samani (from NASA POWER Tmax/Tmin)",
    }


CHIRPS_URL = "https://climateserv.servirglobal.net/api/"
SOURCES = ("chirps", "open_meteo", "nasa_power")


CHIRPS_CHUNK_YEARS = 10   # ClimateSERV rejects very long daily requests


def _chirps(lat: float, lon: float, y0: int, y1: int) -> dict:
    """CHIRPS daily series, requested as parallel chunks of <= 10 years."""
    if not -50 <= lat <= 50:
        raise RainfallError("CHIRPS covers 50S-50N only")
    chunks = [(a, min(a + CHIRPS_CHUNK_YEARS - 1, y1)) for a in range(y0, y1 + 1, CHIRPS_CHUNK_YEARS)]
    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        parts = list(pool.map(lambda c: _chirps_chunk(lat, lon, *c), chunks))
    dates = [d for part in parts for d in part[0]]
    precip = [v for part in parts for v in part[1]]
    return {
        "source": "CHIRPS v2.0 daily, 0.05 deg (via NASA SERVIR ClimateSERV)",
        "source_url": "https://www.chc.ucsb.edu/data/chirps",
        "dates": dates, "precip": precip, "et0": None, "et0_method": None,
    }


def _chirps_chunk(lat: float, lon: float, y0: int, y1: int, timeout_s: float = 180.0):
    d = 0.025   # half a CHIRPS pixel
    geometry = (
        '{"type":"Polygon","coordinates":[[[%f,%f],[%f,%f],[%f,%f],[%f,%f],[%f,%f]]]}'
        % (lon - d, lat - d, lon + d, lat - d, lon + d, lat + d, lon - d, lat + d, lon - d, lat - d)
    )
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(timeout=httpx.Timeout(30, read=150), headers=headers) as client:
        resp = client.get(CHIRPS_URL + "submitDataRequest/", params={
            "datatype": 0, "begintime": f"01/01/{y0}", "endtime": f"12/31/{y1}",
            "intervaltype": 0, "operationtype": 5, "dateType_Category": "default",
            "isZip_CurrentDataType": "false", "geometry": geometry,
        })
        if resp.status_code != 200:
            raise RainfallError(f"CHIRPS: HTTP {resp.status_code}")
        request_id = resp.json()[0]
        started = time.monotonic()
        while True:
            progress = client.get(CHIRPS_URL + "getDataRequestProgress/", params={"id": request_id}).json()[0]
            if progress < 0:
                raise RainfallError("CHIRPS: request failed on the server")
            if progress >= 100:
                break
            if time.monotonic() - started > timeout_s:
                raise RainfallError("CHIRPS: timed out")
            time.sleep(2.0)
        rows = client.get(CHIRPS_URL + "getDataFromRequest/", params={"id": request_id}).json()["data"]
    dates, precip = [], []
    for row in sorted(rows, key=lambda r: r["epochTime"]):
        m, dd, yy = row["date"].split("/")
        value = float(next(iter(row["value"].values())))
        dates.append(f"{yy}-{m}-{dd}")
        precip.append(value if value >= 0 else 0.0)
    return dates, precip


FETCHERS = {"chirps": _chirps, "open_meteo": _open_meteo, "nasa_power": _nasa_power}


def _fetch_source(name: str, lat: float, lon: float, y0: int, y1: int, errors: list) -> dict | None:
    key = f"rain:{name}:{lat:.2f}:{lon:.2f}:{y0}:{y1}"
    cached = db.cache_get(key, CACHE_MAX_AGE_S)
    if cached:
        cached["cached"] = True
        return cached
    for attempt in range(3):
        try:
            data = FETCHERS[name](lat, lon, y0, y1)
        except httpx.HTTPError as exc:
            # Transient network failures (resets, timeouts): retry.
            errors.append(f"{name} attempt {attempt + 1}: {exc}")
            time.sleep(1.0 * (attempt + 1))
            continue
        except (RainfallError, KeyError, ValueError, IndexError) as exc:
            # A real answer (rate limit, bad payload): give up on this source.
            errors.append(f"{name}: {exc}")
            return None
        if len(data["dates"]) < 365:
            errors.append(f"{name}: too few days")
            return None
        db.cache_put(key, data)
        data["cached"] = False
        return data
    return None


def fetch_daily(lat: float, lon: float, years: int = 20, source: str = "auto",
                annual_normal_mm: float | None = None) -> dict:
    """
    Daily precipitation + ET0 for the last `years` complete years.

    source: "auto" (CHIRPS, then Open-Meteo, then NASA POWER) or one of
    SOURCES to force a specific precipitation dataset.
    """
    y0, y1 = _year_range(years)
    errors: list[str] = []
    precip_order = list(SOURCES) if source == "auto" else [source]

    with ThreadPoolExecutor(max_workers=2) as pool:
        chirps_job = (pool.submit(_fetch_source, "chirps", lat, lon, y0, y1, errors)
                      if "chirps" in precip_order else None)
        # ET0 (and fallback precipitation) from the fast reanalysis sources.
        met = _fetch_source("open_meteo", lat, lon, y0, y1, errors) \
            or _fetch_source("nasa_power", lat, lon, y0, y1, errors)
        chirps = chirps_job.result() if chirps_job else None

    precip_src = None
    for name in precip_order:
        if name == "chirps":
            precip_src = chirps
        elif met is not None and met["source"].lower().startswith(name.split("_")[0]):
            precip_src = met
        elif name != "chirps" and source != "auto":
            precip_src = _fetch_source(name, lat, lon, y0, y1, errors)
        if precip_src is not None:
            break
    if precip_src is None:
        raise RainfallError("All rainfall APIs failed: " + " | ".join(errors))

    dates = precip_src["dates"]
    precip = np.asarray(precip_src["precip"], dtype=float)
    if met is not None:
        et0_map = dict(zip(met["dates"], met["et0"]))
        et0 = [et0_map.get(d, float("nan")) for d in dates]
        et0_method = met["et0_method"]
    else:
        et0, et0_method = [float("nan")] * len(dates), None

    out = {
        "source": precip_src["source"],
        "source_url": precip_src.get("source_url"),
        "dates": dates,
        "precip": precip.tolist(),
        "et0": et0,
        "et0_method": et0_method,
        "cached": bool(precip_src.get("cached")),
        "fallback_errors": errors,
    }

    if annual_normal_mm:
        n_years = len({d[:4] for d in dates})
        raw_mean = float(precip.sum() / max(n_years, 1))
        factor = annual_normal_mm / raw_mean if raw_mean > 0 else 1.0
        out["precip"] = (precip * factor).tolist()
        out["scaled_to_normal"] = {
            "normal_mm": annual_normal_mm, "dataset_mean_mm": round(raw_mean, 1),
            "scale_factor": round(factor, 3),
            "method": "Linear scaling of the daily series to the supplied long-term normal",
        }
    return out


def _finite(v) -> float | None:
    return round(float(v), 1) if np.isfinite(v) else None


def summarise(daily: dict) -> dict:
    """Annual and monthly statistics from a daily series."""
    dates = np.array(daily["dates"])
    p = np.asarray(daily["precip"], dtype=float)
    et0 = np.asarray(daily["et0"], dtype=float)
    years = np.array([int(d[:4]) for d in dates])
    months = np.array([int(d[5:7]) for d in dates])

    uniq = sorted(set(years.tolist()))
    annual = np.array([p[years == y].sum() for y in uniq])
    rainy_days = np.array([(p[years == y] >= 2.5).sum() for y in uniq])
    max_1day = np.array([p[years == y].max() for y in uniq])
    et0 = np.where(np.isfinite(et0), et0, np.nan)
    with np.errstate(all="ignore"):
        annual_et0 = np.array([np.nanmean(et0[years == y]) * (years == y).sum() for y in uniq])
    # Oct-May: the period the pond has to carry water through after the monsoon.
    dry = (months >= 10) | (months <= 5)
    dry_et0 = np.array([np.nanmean(et0[(years == y) & dry]) * ((years == y) & dry).sum() for y in uniq])

    monthly = np.array([[p[(years == y) & (months == m)].sum() for m in range(1, 13)] for y in uniq])
    monthly_mean = monthly.mean(axis=0)
    monsoon_share = float(monthly_mean[5:9].sum() / max(monthly_mean.sum(), 1e-9))

    mean = float(annual.mean())
    return {
        "source": daily["source"],
        "source_url": daily.get("source_url"),
        "period": f"{uniq[0]}-{uniq[-1]}",
        "years_of_record": len(uniq),
        "mean_annual_mm": round(mean, 1),
        "median_annual_mm": round(float(np.median(annual)), 1),
        "std_annual_mm": round(float(annual.std(ddof=1)) if len(uniq) > 1 else 0.0, 1),
        "coefficient_of_variation": round(float(annual.std(ddof=1) / mean) if len(uniq) > 1 and mean else 0.0, 3),
        "dependable_75pct_mm": round(float(np.percentile(annual, 25)), 1),
        "min_annual": {"year": uniq[int(np.argmin(annual))], "mm": round(float(annual.min()), 1)},
        "max_annual": {"year": uniq[int(np.argmax(annual))], "mm": round(float(annual.max()), 1)},
        "mean_rainy_days": round(float(rainy_days.mean()), 1),
        "max_daily_mm": round(float(max_1day.max()), 1),
        "monsoon_jun_sep_share": round(monsoon_share, 3),
        "mean_annual_et0_mm": _finite(np.nanmean(annual_et0) if np.isfinite(annual_et0).any() else np.nan),
        "mean_dry_season_et0_mm": _finite(np.nanmean(dry_et0) if np.isfinite(dry_et0).any() else np.nan),
        "et0_method": daily.get("et0_method"),
        "annual_series": [
            {"year": y, "rainfall_mm": round(float(a), 1), "rainy_days": int(rd), "max_daily_mm": round(float(mx), 1)}
            for y, a, rd, mx in zip(uniq, annual, rainy_days, max_1day)
        ],
        "monthly_mean_mm": [{"month": MONTHS[i], "rainfall_mm": round(float(v), 1)} for i, v in enumerate(monthly_mean)],
        "cached": daily.get("cached", False),
        "scaled_to_normal": daily.get("scaled_to_normal"),
    }
