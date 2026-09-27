"""
Build blind labelling sheets for the land-cover classifier.

For 4 contrasting landscapes, 25 random analysis cells (20 m) each are drawn.
The classifier's dominant class per cell is saved to predictions.json but NOT
drawn on the sheets; each sheet shows a 100 m high-resolution image chip
(Esri zoom 18) with the 20 m cell outlined, to be labelled independently.
"""

import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.landcover import CLASS_NAMES, satellite_landcover  # noqa: E402
from app.terrain import empty_grid  # noqa: E402
from app.tiles import fetch_mosaic, sample_mosaic  # noqa: E402

SITES = [
    ("Hiware Bazar, MH (semi-arid)", 19.068, 74.601),
    ("Ludhiana rural, PB (irrigated)", 30.80, 75.70),
    ("Kuttanad, KL (wet, paddy/water)", 9.43, 76.42),
    ("Jaipur rural, RJ (arid)", 26.75, 75.55),
]
OUT = Path(__file__).parent / "chips"
CHIP_M, CHIP_PX = 100.0, 170


def main():
    OUT.mkdir(exist_ok=True)
    rng = np.random.default_rng(42)
    preds = []
    for si, (name, lat, lon) in enumerate(SITES):
        dem = empty_grid(lon, lat, 1000.0, 100)       # 20 m cells
        fr, _ = satellite_landcover(dem)
        cells = rng.choice(dem.nrows * dem.ncols, 25, replace=False)
        sheet = np.full((5 * (CHIP_PX + 26), 5 * (CHIP_PX + 6), 3), 255, np.uint8)
        for k, idx in enumerate(cells):
            r, c = divmod(int(idx), dem.ncols)
            clon, clat = dem.cell_to_lonlat(r, c)
            clon, clat = float(clon), float(clat)
            # 100 m chip, north up, from zoom 18 imagery
            h = CHIP_M / 2
            corner_lon, corner_lat = dem.proj.inverse(np.array([-h, h]) + dem.proj.forward(clon, clat)[0],
                                                      np.array([-h, h]) + dem.proj.forward(clon, clat)[1])
            bbox = (float(corner_lon[0]), float(corner_lat[0]), float(corner_lon[1]), float(corner_lat[1]))
            mosaic, origin = fetch_mosaic("esri_imagery", bbox, 18)
            xs = np.linspace(-h, h, CHIP_PX)
            gx, gy = np.meshgrid(xs, xs[::-1])
            x0, y0 = dem.proj.forward(clon, clat)
            glon, glat = dem.proj.inverse(gx + x0, gy + y0)
            chip = sample_mosaic(mosaic, origin, 18, glon, glat)
            s = int(CHIP_PX * (dem.cell / CHIP_M) / 2)
            m = CHIP_PX // 2
            cv2.rectangle(chip, (m - s, m - s), (m + s, m + s), (0, 0, 255), 2)
            row, col = divmod(k, 5)
            y, x = row * (CHIP_PX + 26), col * (CHIP_PX + 6)
            sheet[y + 22:y + 22 + CHIP_PX, x:x + CHIP_PX] = chip
            label = f"{si * 25 + k + 1}"
            cv2.putText(sheet, label, (x + 2, y + 17), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)
            dom = int(np.argmax(fr[r, c]))
            preds.append({"id": si * 25 + k + 1, "site": name, "lat": clat, "lon": clon,
                          "predicted": CLASS_NAMES[dom],
                          "fractions": {CLASS_NAMES[i]: round(float(fr[r, c, i]), 2) for i in CLASS_NAMES}})
        cv2.imwrite(str(OUT / f"sheet_{si + 1}.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])
    (Path(__file__).parent / "landcover_predictions.json").write_text(json.dumps(preds, indent=1))
    print("wrote", len(preds), "chips")


if __name__ == "__main__":
    main()
