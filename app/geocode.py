"""
geocode.py
----------
Village search and reverse lookup through OpenStreetMap Nominatim.

Requests are proxied through the backend so a proper User-Agent is sent (the
Nominatim usage policy requires one) and results can be cached.
"""

from __future__ import annotations

import httpx

from . import db
from .tiles import USER_AGENT

NOMINATIM = "https://nominatim.openstreetmap.org"
CACHE_S = 7 * 24 * 3600


def search(query: str, country: str | None = "in", limit: int = 8) -> list[dict]:
    key = f"geo:{country}:{limit}:{query.strip().lower()}"
    cached = db.cache_get(key, CACHE_S)
    if cached is not None:
        return cached["results"]
    params = {"q": query, "format": "jsonv2", "limit": limit, "addressdetails": 1}
    if country:
        params["countrycodes"] = country
    resp = httpx.get(f"{NOMINATIM}/search", params=params,
                     headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}, timeout=15)
    resp.raise_for_status()
    results = []
    for r in resp.json():
        addr = r.get("address", {})
        results.append({
            "name": r.get("name") or r.get("display_name", "").split(",")[0],
            "display_name": r.get("display_name"),
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "type": r.get("addresstype") or r.get("type"),
            "district": addr.get("state_district") or addr.get("county"),
            "state": addr.get("state"),
            "bbox": [float(v) for v in r.get("boundingbox", [])] or None,  # [s, n, w, e]
        })
    db.cache_put(key, {"results": results})
    return results


def reverse(lat: float, lon: float) -> dict | None:
    key = f"rev:{lat:.4f}:{lon:.4f}"
    cached = db.cache_get(key, CACHE_S)
    if cached is not None:
        return cached
    resp = httpx.get(f"{NOMINATIM}/reverse",
                     params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 14, "addressdetails": 1},
                     headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}, timeout=15)
    resp.raise_for_status()
    body = resp.json()
    addr = body.get("address", {})
    out = {
        "name": addr.get("village") or addr.get("hamlet") or addr.get("town") or addr.get("suburb")
        or addr.get("city") or body.get("name"),
        "display_name": body.get("display_name"),
        "district": addr.get("state_district") or addr.get("county"),
        "state": addr.get("state"),
    }
    db.cache_put(key, out)
    return out
