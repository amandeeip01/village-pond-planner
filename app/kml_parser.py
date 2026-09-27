"""
kml_parser.py
-------------
Turns a KML/KMZ contour map into a list of contour lines:

    [ {"elevation": 512.0, "coords": [(lon, lat), (lon, lat), ...]}, ... ]

Contour KML files in the wild are inconsistent about WHERE the elevation lives.
So we try several strategies in order of reliability and use the first one
that produces sensible, varying values. Nothing here is specific to any one map.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Iterable

from lxml import etree

# Any tag/field name that plausibly holds a contour's height value.
ELEV_KEY_PATTERN = re.compile(
    r"elev|elevation|contour|height|altitude|^alt$|^z$|level|value",
    re.IGNORECASE,
)
NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")


class KMLParseError(ValueError):
    """Raised when the upload is not a usable contour map."""


# --------------------------------------------------------------------------
# 1. Get raw KML bytes out of the upload (KMZ is just a ZIP containing KML)
# --------------------------------------------------------------------------
def extract_kml_bytes(raw: bytes) -> bytes:
    if raw[:2] == b"PK":  # ZIP magic number -> it's a KMZ
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
                if not names:
                    raise KMLParseError("KMZ archive contains no .kml file.")
                # doc.kml is the conventional entry point; otherwise take the first.
                name = next((n for n in names if n.lower().endswith("doc.kml")), names[0])
                return zf.read(name)
        except zipfile.BadZipFile as exc:
            raise KMLParseError("File looks like a KMZ but could not be unzipped.") from exc
    return raw


def _strip_namespaces(tree: etree._Element) -> etree._Element:
    """KML uses namespaces (and Google adds gx:). Dropping them keeps XPath simple."""
    for el in tree.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    etree.cleanup_namespaces(tree)
    return tree


def _parse_coord_block(text: str) -> list[tuple[float, float, float]]:
    """
    KML <coordinates> is whitespace-separated 'lon,lat[,alt]' tuples.
    Returns (lon, lat, z) triples; z is 0.0 when absent.
    """
    out: list[tuple[float, float, float]] = []
    for token in text.replace("\n", " ").replace("\t", " ").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
            z = float(parts[2]) if len(parts) > 2 and parts[2] != "" else 0.0
        except ValueError:
            continue
        out.append((lon, lat, z))
    return out


# --------------------------------------------------------------------------
# 2. Elevation-extraction strategies, tried in order
# --------------------------------------------------------------------------
def _elev_from_z(coords: list[tuple[float, float, float]]) -> float | None:
    """Strategy A: the Z value baked into the coordinate tuples."""
    zs = [c[2] for c in coords]
    if not zs or all(z == 0.0 for z in zs):
        return None
    return sum(zs) / len(zs)


def _elev_from_extended_data(placemark: etree._Element) -> float | None:
    """Strategy B: <ExtendedData><SimpleData name="ELEV">512</SimpleData>."""
    for node in placemark.iter("SimpleData", "Data", "value"):
        key = node.get("name") or ""
        if node.tag == "value":
            key = node.getparent().get("name") or ""
        if key and not ELEV_KEY_PATTERN.search(key):
            continue
        text = (node.text or "").strip()
        if node.tag == "Data":  # value lives in a child
            child = node.find("value")
            text = (child.text or "").strip() if child is not None else ""
        match = NUMBER_PATTERN.search(text)
        if match:
            return float(match.group())
    return None


def _elev_from_label(placemark: etree._Element) -> float | None:
    """Strategy C: the first number in <name> or <description>, e.g. '512 m'."""
    for tag in ("name", "description"):
        node = placemark.find(tag)
        if node is None or not node.text:
            continue
        match = NUMBER_PATTERN.search(node.text)
        if match:
            return float(match.group())
    return None


def _elev_from_ancestor_folder(placemark: etree._Element) -> float | None:
    """Strategy D: contours grouped into <Folder><name>512</name>."""
    node = placemark.getparent()
    while node is not None:
        if node.tag in ("Folder", "Document"):
            name = node.find("name")
            if name is not None and name.text:
                match = NUMBER_PATTERN.search(name.text)
                if match:
                    return float(match.group())
        node = node.getparent()
    return None


# --------------------------------------------------------------------------
# 3. Main entry point
# --------------------------------------------------------------------------
def parse_contours(raw: bytes) -> tuple[list[dict], dict]:
    """
    Parse an uploaded KML/KMZ into contour lines.

    Returns (contours, meta) where each contour is
        {"elevation": float, "coords": [(lon, lat), ...]}
    and meta records which strategy won, for the API response / report.
    """
    kml = extract_kml_bytes(raw)

    try:
        root = etree.fromstring(kml, parser=etree.XMLParser(recover=True, huge_tree=True))
    except etree.XMLSyntaxError as exc:
        raise KMLParseError(f"File is not valid XML/KML: {exc}") from exc
    if root is None:
        raise KMLParseError("File is empty or unreadable as KML.")
    root = _strip_namespaces(root)

    placemarks = root.iter("Placemark")

    # Collect geometry first; decide the elevation strategy afterwards, because
    # a strategy is only trustworthy if it yields DIFFERENT values per contour.
    records: list[dict] = []
    for pm in placemarks:
        # LineString and LinearRing both describe contour lines; MultiGeometry
        # wraps several of them, and .iter() already walks into it.
        geoms: Iterable[etree._Element] = list(pm.iter("LineString")) + list(pm.iter("LinearRing"))
        for geom in geoms:
            cnode = geom.find("coordinates")
            if cnode is None or not cnode.text:
                continue
            coords = _parse_coord_block(cnode.text)
            if len(coords) < 2:
                continue
            records.append({"placemark": pm, "coords": coords})

    if not records:
        raise KMLParseError(
            "No contour geometry found. The KML must contain Placemarks with "
            "<LineString> or <LinearRing> coordinates."
        )

    strategies = [
        ("coordinate_z", lambda r: _elev_from_z(r["coords"])),
        ("extended_data", lambda r: _elev_from_extended_data(r["placemark"])),
        ("placemark_name", lambda r: _elev_from_label(r["placemark"])),
        ("folder_name", lambda r: _elev_from_ancestor_folder(r["placemark"])),
    ]

    chosen_name, values = None, None
    for name, fn in strategies:
        vals = [fn(r) for r in records]
        good = [v for v in vals if v is not None]
        # Accept only if nearly every line got a value AND the values actually vary.
        if len(good) >= 0.9 * len(records) and len(set(good)) > 1:
            chosen_name, values = name, vals
            break

    if values is None:
        raise KMLParseError(
            "Could not determine elevations. Contour heights must appear in the "
            "coordinate Z values, ExtendedData, Placemark names, or Folder names."
        )

    contours = [
        {"elevation": float(v), "coords": [(c[0], c[1]) for c in r["coords"]]}
        for r, v in zip(records, values)
        if v is not None
    ]

    elevations = sorted({c["elevation"] for c in contours})
    # Contour interval = most common gap between consecutive distinct levels.
    interval = None
    if len(elevations) > 1:
        gaps = [round(b - a, 4) for a, b in zip(elevations, elevations[1:])]
        interval = max(set(gaps), key=gaps.count)

    meta = {
        "elevation_source": chosen_name,
        "contour_lines": len(contours),
        "distinct_levels": len(elevations),
        "elevation_min_m": min(elevations),
        "elevation_max_m": max(elevations),
        "contour_interval_m": interval,
        "total_vertices": sum(len(c["coords"]) for c in contours),
    }
    return contours, meta
