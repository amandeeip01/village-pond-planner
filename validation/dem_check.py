"""
Elevation accuracy: terrain-tile DEM vs elevations recorded on OpenStreetMap
peaks (ele=* tag) in several Indian regions.

Peaks are the worst case for a ~30 m DEM (summits are narrow, so the DEM cell
averages them down), so the result is an upper bound on typical error; the
bias is reported separately from the scatter.
"""

import json
import re
import sys
import time
from pathlib import Path

import cv2
import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.tiles import USER_AGENT, decode_terrarium, fetch_mosaic, sample_mosaic  # noqa: E402

REGIONS = {
    "Sahyadri (Pune-Ahilyanagar)": (18.6, 73.4, 19.4, 74.6),
    "Deccan plateau (Karnataka)": (13.0, 76.0, 14.0, 77.5),
    "Aravalli (Rajasthan)": (24.4, 73.4, 25.2, 74.2),
    "Nilgiris / Western Ghats (TN-Kerala)": (10.9, 76.4, 11.6, 77.2),
}
ZOOM = 13


def peaks(bbox):
    s, w, n, e = bbox
    q = f'[out:json][timeout:60];node["natural"="peak"]["ele"]({s},{w},{n},{e});out;'
    r = None
    for attempt in range(4):
        for url in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"):
            try:
                r = httpx.post(url, data={"data": q}, headers={"User-Agent": USER_AGENT}, timeout=90)
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                r = None
        if r is not None and r.status_code == 200:
            break
        time.sleep(10)
    r.raise_for_status()
    out = []
    for el in r.json()["elements"]:
        m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*(m)?\s*$", el["tags"]["ele"])
        if m:
            out.append((el["lon"], el["lat"], float(m.group(1)), el["tags"].get("name", "")))
    return out


def point_elevation(lon, lat):
    b = (lon - 0.002, lat - 0.002, lon + 0.002, lat + 0.002)
    mosaic, origin = fetch_mosaic("terrarium", b, ZOOM)
    elev = decode_terrarium(mosaic).astype(np.float32)
    return float(sample_mosaic(elev, origin, ZOOM, np.array([[lon]]), np.array([[lat]]), cv2.INTER_LINEAR)[0, 0])


def main():
    rows, summary = [], {}
    for region, bbox in REGIONS.items():
        pts = peaks(bbox)
        if not pts:
            continue
        lon = np.array([p[0] for p in pts]); lat = np.array([p[1] for p in pts])
        dem = np.array([point_elevation(x, y) for x, y in zip(lon, lat)])
        err = dem - np.array([p[2] for p in pts])
        keep = np.abs(err) < 300          # drop gross OSM tagging errors (e.g. feet, typos)
        e = err[keep]
        summary[region] = {"n": int(keep.sum()), "dropped_outliers": int((~keep).sum()),
                           "bias_m": round(float(e.mean()), 1), "mae_m": round(float(np.abs(e).mean()), 1),
                           "rmse_m": round(float(np.sqrt((e ** 2).mean())), 1),
                           "median_abs_m": round(float(np.median(np.abs(e))), 1)}
        rows += [{"region": region, "name": p[3], "osm_ele": p[2], "dem": round(float(d), 1)}
                 for p, d, k in zip(pts, dem, keep) if k]
    allerr = np.array([r["dem"] - r["osm_ele"] for r in rows])
    summary["ALL"] = {"n": len(rows), "bias_m": round(float(allerr.mean()), 1),
                      "mae_m": round(float(np.abs(allerr).mean()), 1),
                      "rmse_m": round(float(np.sqrt((allerr ** 2).mean())), 1),
                      "median_abs_m": round(float(np.median(np.abs(allerr))), 1)}
    Path(__file__).with_name("dem_results.json").write_text(json.dumps({"summary": summary, "points": rows}, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
