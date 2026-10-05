"""Pond siting must never pick cells excluded as sea, even in the terrain-only fallback."""

import numpy as np

from app.hydrology import d8_flow_direction, fill_depressions, flow_accumulation
from app.pond import select_pond_site


def coastal_valley(n=61):
    """A valley draining to row 0, where the lowest third of the grid is sea (z <= 0)."""
    r, c = np.mgrid[0:n, 0:n].astype(float)
    return np.abs(c - n // 2) + 0.5 * (r - n // 3)


def _site(z, **kw):
    f = fill_depressions(z)
    d = d8_flow_direction(f)
    acc = flow_accumulation(f, d)
    zeros = np.zeros_like(z)
    return select_pond_site(z, zeros, zeros, acc, **kw)


def test_without_exclusion_the_outlet_lies_in_the_sea():
    z = coastal_valley()
    (r, c), _, _ = _site(z)
    assert z[r, c] <= 0


def test_sea_cells_are_never_chosen():
    z = coastal_valley()
    sea = z <= 0
    (r, c), _, score = _site(z, exclude=sea)
    assert z[r, c] > 0
    assert not np.isfinite(score[sea]).any()


def test_sea_excluded_even_when_no_land_is_available():
    z = coastal_valley()
    sea = z <= 0
    nothing = np.zeros(z.shape, dtype=bool)      # forces the terrain-only fallback
    (r, c), diag, _ = _site(z, exclude=sea, allowed=nothing)
    assert not diag["restricted_to_available_land"]
    assert z[r, c] > 0


def test_sea_mask_separates_sea_from_low_lying_land():
    from app.pipeline import sea_mask
    z = np.full((100, 100), 5.0)
    z[:, :40] = 0.0                      # flat sea surface at 0 m ...
    z[:, :10] = -20.0                    # ... with bathymetry further out
    z[:, 40:43] = -1.0                   # shallow seabed joined to the sea
    z[60:80, 70:90] = -1.5               # inland polder below sea level
    sea = sea_mask(z)
    assert sea[:, :43].all()
    assert not sea[60:80, 70:90].any()
    assert not sea[:, 43:].any()
