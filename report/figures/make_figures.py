"""Generate the data figures for the technical report from saved results."""

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).parent
VAL = ROOT / "validation"
DATA = ROOT / "report" / "data"

plt.rcParams.update({
    "font.family": "serif", "font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9,
    "axes.spines.top": False, "axes.spines.right": False, "savefig.bbox": "tight", "savefig.dpi": 220,
    "axes.grid": True, "grid.alpha": 0.25, "grid.linewidth": 0.5,
})
C_IMD, C_CHIRPS, C_NASA = "#333333", "#1f6fb2", "#e07b28"


def rainfall_validation():
    rows = json.loads((VAL / "rainfall_results.json").read_text())
    names = [r["station"].split(" (")[0] for r in rows]
    x = np.arange(len(rows)); w = 0.26
    fig, (a, b) = plt.subplots(1, 2, figsize=(7.2, 2.7), gridspec_kw={"width_ratios": [1.6, 1]})
    a.bar(x - w, [r["imd_normal_mm"] for r in rows], w, color=C_IMD, label="IMD normal 1991-2020")
    a.bar(x, [r["chirps"] for r in rows], w, color=C_CHIRPS, label="CHIRPS")
    a.bar(x + w, [r["nasa_power"] for r in rows], w, color=C_NASA, label="NASA POWER")
    a.set_xticks(x, names, rotation=25, ha="right"); a.set_ylabel("Mean annual rainfall (mm)")
    a.legend(frameon=False, fontsize=7.5); a.set_title("(a) Mean annual rainfall")
    ec = [(r["chirps"] - r["imd_normal_mm"]) / r["imd_normal_mm"] * 100 for r in rows]
    en = [(r["nasa_power"] - r["imd_normal_mm"]) / r["imd_normal_mm"] * 100 for r in rows]
    b.axhline(0, color="k", lw=0.8)
    b.bar(x - 0.18, ec, 0.36, color=C_CHIRPS, label=f"CHIRPS (MAE {np.mean(np.abs(ec)):.1f}%)")
    b.bar(x + 0.18, en, 0.36, color=C_NASA, label=f"NASA POWER (MAE {np.mean(np.abs(en)):.1f}%)")
    b.set_xticks(x, names, rotation=40, ha="right", fontsize=7.5); b.set_ylabel("Error vs IMD (%)")
    b.legend(frameon=False, fontsize=7.5); b.set_title("(b) Relative error")
    fig.savefig(OUT / "rainfall_validation.pdf"); plt.close(fig)


def confusion():
    res = json.loads((VAL / "landcover_results.json").read_text())
    cls = ["vegetation", "open_land", "built_up"]
    lab = ["Vegetation", "Open land", "Built-up"]
    m = np.array([[res["confusion_true_rows_pred_cols"][t][p] for p in cls] for t in cls])
    fig, ax = plt.subplots(figsize=(3.2, 2.7))
    ax.imshow(m, cmap="Blues"); ax.grid(False)
    for i in range(3):
        for j in range(3):
            ax.text(j, i, m[i, j], ha="center", va="center", color="white" if m[i, j] > 20 else "black", fontsize=11)
    ax.set_xticks(range(3), lab); ax.set_yticks(range(3), lab)
    ax.set_xlabel("Predicted"); ax.set_ylabel("Reference (manual)")
    ax.set_title(f"OA {res['overall_accuracy']*100:.1f}%, $\\kappa$ = {res['cohen_kappa'] + 1e-9:.2f}, n = {res['evaluated']}")
    fig.savefig(OUT / "landcover_confusion.pdf"); plt.close(fig)


def dem_errors():
    res = json.loads((VAL / "dem_results.json").read_text())
    pts = res["points"]
    err = np.array([p["dem"] - p["osm_ele"] for p in pts])
    osm = np.array([p["osm_ele"] for p in pts]); dem = np.array([p["dem"] for p in pts])
    fig, (a, b) = plt.subplots(1, 2, figsize=(6.6, 2.6))
    lim = [min(osm.min(), dem.min()) - 50, max(osm.max(), dem.max()) + 50]
    a.plot(lim, lim, color="k", lw=0.8)
    regions = sorted({p["region"] for p in pts})
    for reg, col in zip(regions, ["#1f6fb2", "#2a9d5b", "#c0392b"]):
        s = [i for i, p in enumerate(pts) if p["region"] == reg]
        a.scatter(osm[s], dem[s], s=12, color=col, label=reg.split(" (")[0])
    a.set_xlabel("Surveyed summit elevation, OSM (m)"); a.set_ylabel("DEM elevation (m)")
    a.legend(frameon=False, fontsize=7); a.set_title("(a) DEM vs summit elevation")
    b.hist(err, bins=np.arange(-250, 60, 15), color="#1f6fb2")
    b.axvline(np.median(err), color="k", ls="--", lw=0.8)
    b.set_xlabel("DEM - summit (m)"); b.set_ylabel("Count")
    b.set_title(f"(b) Error: median |e| {np.median(np.abs(err)):.0f} m, bias {err.mean():.0f} m")
    fig.savefig(OUT / "dem_validation.pdf"); plt.close(fig)


def case_rainfall():
    d = json.loads((DATA / "hiware_bazar_analysis.json").read_text())
    rain, ro = d["rainfall"], d["runoff"]
    yrs = [s["year"] for s in rain["annual_series"]]
    p = [s["rainfall_mm"] for s in rain["annual_series"]]
    q = {s["year"]: s["runoff_mm"] for s in ro["annual_series"]}
    fig, (a, b) = plt.subplots(1, 2, figsize=(7.2, 2.5), gridspec_kw={"width_ratios": [1.7, 1]})
    a.bar(yrs, p, color="#8fb8de", label="Rainfall")
    a.bar(yrs, [q[y] for y in yrs], color="#1f4e79", label="Runoff (SCS-CN)")
    a.axhline(rain["mean_annual_mm"], color="#e07b28", ls="--", lw=1, label=f"Mean {rain['mean_annual_mm']:.0f} mm")
    a.axhline(rain["dependable_75pct_mm"], color="#c0392b", ls=":", lw=1, label=f"75% dependable {rain['dependable_75pct_mm']:.0f} mm")
    a.set_ylabel("mm"); a.legend(frameon=False, fontsize=7, ncol=2); a.set_title("(a) Annual rainfall and runoff, Hiware Bazar")
    mm = rain["monthly_mean_mm"]
    b.bar(range(12), [m["rainfall_mm"] for m in mm], color="#8fb8de")
    b.set_xticks(range(12), [m["month"][0] for m in mm])   # positional: J/J/M/M/A/A must not merge
    b.set_title("(b) Mean monthly rainfall (mm)")
    fig.savefig(OUT / "case_rainfall.pdf"); plt.close(fig)


def sensitivity():
    """Runoff response to rainfall scaling and to curve number, on the real series."""
    from app.rainfall import fetch_daily
    from app.runoff import estimate_runoff
    d = json.loads((DATA / "hiware_bazar_analysis.json").read_text())
    site = d["pond_site"]["location"]
    daily = fetch_daily(site["lat"], site["lon"], 20)          # served from cache
    area = d["catchment"]["area_m2"]; cover = d["runoff"]["catchment_landcover_share"]
    base = estimate_runoff(daily, area, cover, "C")["mean_annual_runoff_m3"]
    fac = np.linspace(0.6, 1.4, 17)
    rel = []
    for f in fac:
        dd = dict(daily, precip=list(np.asarray(daily["precip"]) * f))
        rel.append(estimate_runoff(dd, area, cover, "C")["mean_annual_runoff_m3"] / base)
    soils = {g: estimate_runoff(daily, area, cover, g)["mean_annual_runoff_m3"] for g in "ABCD"}
    fig, (a, b) = plt.subplots(1, 2, figsize=(6.6, 2.5))
    a.plot((fac - 1) * 100, (np.array(rel) - 1) * 100, color="#1f6fb2", marker="o", ms=3, label="SCS-CN (daily)")
    a.plot((fac - 1) * 100, (fac - 1) * 100, color="grey", ls="--", lw=0.8, label="Proportional (rational)")
    a.set_xlabel("Change in rainfall (%)"); a.set_ylabel("Change in runoff (%)")
    a.legend(frameon=False, fontsize=7.5); a.set_title("(a) Sensitivity to rainfall")
    b.bar(list(soils), [v / 1000 for v in soils.values()], color=["#9ecae1", "#6baed6", "#3182bd", "#08519c"])
    b.set_xlabel("Hydrologic soil group"); b.set_ylabel("Mean runoff (1000 m$^3$/yr)")
    b.set_title("(b) Sensitivity to soil group")
    fig.savefig(OUT / "sensitivity.pdf"); plt.close(fig)
    return {"soils": soils, "rel": dict(zip(np.round(fac, 2).tolist(), np.round(rel, 3).tolist()))}


def candidates_plot():
    d = json.loads((DATA / "hiware_bazar_analysis.json").read_text())
    fig, ax = plt.subplots(figsize=(3.4, 3.4))
    cols = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4"]
    for k, col in zip(d["candidates"], cols):
        ring = np.array(k["catchment"]["boundary_geojson"]["geometry"]["coordinates"][0])
        ax.fill(ring[:, 0], ring[:, 1], color=col, alpha=0.18); ax.plot(ring[:, 0], ring[:, 1], color=col, lw=1)
        s = k["pond_site"]["location"]
        ax.plot(s["lon"], s["lat"], "o", color=col, mec="k", ms=6)
        ax.annotate(str(k["rank"]), (s["lon"], s["lat"]), xytext=(4, 4), textcoords="offset points", fontsize=8, weight="bold")
    for f in d["layers"]["drainage"]["features"]:
        c = np.array(f["geometry"]["coordinates"])
        ax.plot(c[:, 0], c[:, 1], color="#5dade2", lw=0.5, zorder=0)
    b = d["dem"]["bbox_lonlat"]
    ax.set_xlim(b["min_lon"], b["max_lon"]); ax.set_ylim(b["min_lat"], b["max_lat"])
    ax.set_aspect(1 / np.cos(np.radians((b["min_lat"] + b["max_lat"]) / 2)))
    ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(4))
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(5))
    ax.set_xlabel("Longitude (°E)"); ax.set_ylabel("Latitude (°N)"); ax.tick_params(labelsize=7)
    ax.set_title("Ranked candidates and independent catchments")
    fig.savefig(OUT / "candidates.pdf"); plt.close(fig)


if __name__ == "__main__":
    rainfall_validation(); confusion(); dem_errors(); case_rainfall(); candidates_plot()
    print(json.dumps(sensitivity(), indent=1))
