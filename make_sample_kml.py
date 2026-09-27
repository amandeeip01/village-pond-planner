"""
make_sample_kml.py
------------------
Generates a synthetic contour map so the API can be tested without the real
data file. Builds an analytic valley surface, extracts contour lines from it
with matplotlib, and writes them out as KML.

    python make_sample_kml.py sample_contours.kml

Not part of the API. Testing utility only.
"""

import sys

import numpy as np

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    sys.exit("Install matplotlib to generate the sample: pip install matplotlib")

LON0, LAT0 = 77.5946, 12.9716        # arbitrary origin, any location works
SPAN_DEG = 0.02                       # roughly 2 km across


def surface(X, Y):
    """A valley running north-east, with two side gullies and a hill."""
    z = 640.0
    z -= 55.0 * np.exp(-((X * 0.9 + Y * 0.4) ** 2) / 0.45)     # main valley
    z -= 22.0 * np.exp(-(((X - 0.45) ** 2) / 0.05 + ((Y - 0.5) ** 2) / 0.6))
    z -= 18.0 * np.exp(-(((X + 0.5) ** 2) / 0.06 + ((Y - 0.3) ** 2) / 0.5))
    z += 40.0 * np.exp(-(((X - 0.75) ** 2 + (Y + 0.65) ** 2) / 0.18))  # hill
    z += 14.0 * Y + 6.0 * X                                    # regional tilt
    return z


def main(path="sample_contours.kml", interval=1.0):
    n = 400
    u = np.linspace(-1, 1, n)
    X, Y = np.meshgrid(u, u)
    Z = surface(X, Y)

    lon = LON0 + X * SPAN_DEG / 2
    lat = LAT0 + Y * SPAN_DEG / 2

    levels = np.arange(np.floor(Z.min()) + 1, np.ceil(Z.max()), interval)
    cs = plt.contour(lon, lat, Z, levels=levels)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        "<name>Synthetic contour map</name>",
    ]

    total = 0
    for level, seg_list in zip(cs.levels, cs.allsegs):
        for seg in seg_list:
            if len(seg) < 3:
                continue
            coords = " ".join(f"{x:.8f},{y:.8f},{level:.2f}" for x, y in seg)
            parts.append(
                f"<Placemark><name>{level:.1f} m</name>"
                f"<ExtendedData><SimpleData name='ELEV'>{level:.2f}</SimpleData></ExtendedData>"
                f"<LineString><altitudeMode>absolute</altitudeMode>"
                f"<coordinates>{coords}</coordinates></LineString></Placemark>"
            )
            total += 1

    parts.append("</Document></kml>")
    with open(path, "w") as fh:
        fh.write("\n".join(parts))

    print(f"Wrote {path}: {total} contour lines, {len(levels)} levels, "
          f"{Z.min():.1f}-{Z.max():.1f} m")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample_contours.kml")
