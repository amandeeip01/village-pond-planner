"""
Compare rainfall datasets with IMD 1991-2020 station normals.

IMD normals are from the Climatological Tables of Observatories in India
1991-2020 (as tabulated on the stations' Wikipedia pages); Pune's figure there
is cited to the Tokyo Climate Center (WMO normals from IMD data).
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.rainfall import FETCHERS  # noqa: E402

STATIONS = [
    # name, lat, lon, IMD normal 1991-2020 (mm), climate
    ("Ahmednagar", 19.08, 74.74, 608.1, "semi-arid, rain shadow"),
    ("Pune (Shivajinagar)", 18.53, 73.85, 841.2, "leeward Western Ghats"),
    ("Jaisalmer", 26.90, 70.92, 256.9, "arid"),
    ("Bengaluru", 12.97, 77.59, 1077.8, "semi-arid plateau"),
    ("Chennai (Airport)", 12.99, 80.17, 1408.4, "NE-monsoon coast"),
    ("Mumbai (Santacruz)", 19.09, 72.85, 2502.3, "windward coast"),
]


def mean_annual(name, lat, lon):
    try:
        d = FETCHERS[name](lat, lon, 1991, 2020)
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:80]
    years = np.array([int(x[:4]) for x in d["dates"]])
    p = np.asarray(d["precip"], float)
    return float(np.mean([p[years == y].sum() for y in np.unique(years)])), None


def main():
    jobs = {}
    # CHIRPS requests are split into 3 parallel chunks internally; run stations
    # two at a time so the ClimateSERV server is not overloaded.
    with ThreadPoolExecutor(max_workers=2) as pool:
        for st in STATIONS:
            for src in ("chirps", "nasa_power", "open_meteo"):
                jobs[(st[0], src)] = pool.submit(mean_annual, src, st[1], st[2])
    rows = []
    for name, lat, lon, normal, climate in STATIONS:
        row = {"station": name, "lat": lat, "lon": lon, "imd_normal_mm": normal, "climate": climate}
        for src in ("chirps", "nasa_power", "open_meteo"):
            v, err = jobs[(name, src)].result()
            row[src] = None if v is None else round(v, 1)
            if err:
                row[src + "_error"] = err
        rows.append(row)
    out = Path(__file__).with_name("rainfall_results.json")
    out.write_text(json.dumps(rows, indent=1))
    print(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
