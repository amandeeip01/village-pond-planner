"""Hydrology tests on synthetic terrain whose true answer is known exactly."""

import numpy as np
import pytest

from app.hydrology import (
    d8_flow_direction,
    delineate_catchment,
    edge_draining_cells,
    fill_depressions,
    flow_accumulation,
    trace_boundary,
)


def v_valley(n=61, cross=1.0, down=0.5):
    """Two planes meeting in a valley along the centre column, sloping to row 0."""
    r, c = np.mgrid[0:n, 0:n].astype(float)
    return cross * np.abs(c - n // 2) + down * r


def route(z):
    f = fill_depressions(z)
    d = d8_flow_direction(f)
    return f, d, flow_accumulation(f, d)


def shoelace(ring):
    pts = np.array(ring, dtype=float)
    x, y = pts[:, 1], pts[:, 0]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def test_fill_removes_all_pits():
    rng = np.random.default_rng(0)
    z = v_valley() + rng.normal(0, 0.8, (61, 61))
    f, d, _ = route(z)
    interior = np.zeros_like(d, dtype=bool)
    interior[1:-1, 1:-1] = True
    assert (d[interior] >= 0).all(), "every interior cell must drain somewhere"
    assert (f >= z - 1e-9).all(), "filling never lowers the surface"


def test_v_valley_catchment_matches_analytic():
    n = 61
    z = v_valley(n)
    _, d, acc = route(z)
    cx = n // 2
    outlet = (20, cx)
    mask = delineate_catchment(d, outlet)
    # Off-valley cells step diagonally (drop 1.5/sqrt2 = 1.06 > 1), so a cell at
    # row r, distance k from the valley joins the valley at row r - k.
    # Everything that joins at or above the outlet row drains through it.
    r, c = np.mgrid[0:n, 0:n]
    k = np.abs(c - cx)
    expected = (r - k >= outlet[0])
    assert mask.sum() == expected.sum()
    assert (mask == expected).all()
    assert acc[outlet] == expected.sum()


def test_catchment_polygon_area_equals_cell_count():
    z = v_valley(41)
    _, d, _ = route(z)
    mask = delineate_catchment(d, (10, 20))
    assert shoelace(trace_boundary(mask)) == pytest.approx(mask.sum())


def test_bowl_drains_to_centre():
    # Cone around the centre with a single low outlet on the south edge.
    n = 51
    r, c = np.mgrid[0:n, 0:n].astype(float)
    z = np.hypot(r - 25, c - 25)
    z[0:26, 25] = np.linspace(-1, -0.01, 26)   # a channel from the centre to the edge
    _, d, acc = route(z)
    assert acc[0, 25] == pytest.approx(n * n, rel=0.02)


def test_edge_inflow_flags_truncated_catchments():
    # Valley sloping to row 0; its head is at the north edge, so outlets on
    # the valley receive water from beyond the map.
    z = v_valley(41)
    f, d, _ = route(z)
    tainted = edge_draining_cells(f, d)
    assert tainted[10, 20]
    # A ridge-top cell next to the west edge that drains inward is flagged, but a
    # cell on a hill whose border drains outward is not.
    r, c = np.mgrid[0:41, 0:41].astype(float)
    hill = -np.hypot(r - 20, c - 20)
    f2, d2, _ = route(hill)
    assert not edge_draining_cells(f2, d2)[20, 20]
