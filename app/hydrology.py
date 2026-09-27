"""
hydrology.py
------------
The actual terrain analysis. Standard textbook DEM hydrology, implemented
directly so the whole thing runs on numpy/scipy with no GIS binaries:

  fill_depressions   Priority-Flood, removes spurious pits
  d8_flow_direction  each cell drains to its steepest lower neighbour
  flow_accumulation  how many cells drain through each cell
  delineate_catchment  everything upstream of a chosen outlet
  trace_boundary     the catchment mask -> a lon/lat polygon ring
"""

from __future__ import annotations

import heapq

import numpy as np

# The 8 neighbours, in a fixed order. dr, dc and the distance to that neighbour
# in cell-widths (diagonals are sqrt(2) further away).
NEIGHBOURS = [
    (-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
    (-1, -1, 1.4142135623730951), (-1, 1, 1.4142135623730951),
    (1, -1, 1.4142135623730951), (1, 1, 1.4142135623730951),
]


def fill_depressions(z: np.ndarray, epsilon: float = 1e-4) -> np.ndarray:
    """
    Priority-Flood depression filling (Barnes, Lehman & Mulla 2014).

    Interpolated DEMs contain small artificial pits. Water routed on the raw
    grid gets trapped in them and flow accumulation breaks. This raises every
    pit to the level of its lowest outlet, guaranteeing a downhill path from
    every cell to the grid edge.

    epsilon adds a tiny slope across filled flats so flow directions stay
    defined instead of becoming ambiguous.
    """
    nrows, ncols = z.shape
    filled = np.full_like(z, np.inf)
    closed = np.zeros(z.shape, dtype=bool)
    heap: list[tuple[float, int, int]] = []

    # Seed with the grid edge: those cells drain out of the map by definition.
    for r in range(nrows):
        for c in (0, ncols - 1):
            heapq.heappush(heap, (float(z[r, c]), r, c))
            closed[r, c] = True
            filled[r, c] = z[r, c]
    for c in range(1, ncols - 1):
        for r in (0, nrows - 1):
            heapq.heappush(heap, (float(z[r, c]), r, c))
            closed[r, c] = True
            filled[r, c] = z[r, c]

    # Always grow from the lowest frontier cell outward.
    while heap:
        zc, r, c = heapq.heappop(heap)
        for dr, dc, _ in NEIGHBOURS:
            nr, nc = r + dr, c + dc
            if nr < 0 or nr >= nrows or nc < 0 or nc >= ncols or closed[nr, nc]:
                continue
            # A neighbour can never be lower than the path used to reach it.
            nz = max(float(z[nr, nc]), zc + epsilon)
            filled[nr, nc] = nz
            closed[nr, nc] = True
            heapq.heappush(heap, (nz, nr, nc))

    return filled


def d8_flow_direction(z: np.ndarray) -> np.ndarray:
    """
    For each cell, the index into NEIGHBOURS of its steepest downhill
    neighbour, or -1 if it drains off the grid edge.

    Steepness is drop / distance, so diagonals are not unfairly favoured.
    """
    nrows, ncols = z.shape
    direction = np.full(z.shape, -1, dtype=np.int8)

    for k, (dr, dc, dist) in enumerate(NEIGHBOURS):
        # Shift the whole grid instead of looping cell by cell.
        shifted = np.full(z.shape, np.nan)
        r_src = slice(max(0, -dr), nrows - max(0, dr))
        c_src = slice(max(0, -dc), ncols - max(0, dc))
        r_dst = slice(max(0, dr), nrows - max(0, -dr))
        c_dst = slice(max(0, dc), ncols - max(0, -dc))
        shifted[r_src, c_src] = z[r_dst, c_dst]

        drop = (z - shifted) / dist
        if k == 0:
            best = np.where(np.isnan(drop), -np.inf, drop)
            direction = np.where(best > 0, 0, -1).astype(np.int8)
        else:
            cand = np.where(np.isnan(drop), -np.inf, drop)
            better = (cand > best) & (cand > 0)
            best = np.where(better, cand, best)
            direction = np.where(better, k, direction).astype(np.int8)

    return direction


def flow_accumulation(z: np.ndarray, direction: np.ndarray) -> np.ndarray:
    """
    Number of cells draining through each cell, including itself.

    Because every cell drains strictly downhill on the filled DEM, processing
    cells from highest to lowest guarantees a cell's own total is final before
    it passes water on. That makes this O(n log n) with no recursion.
    """
    nrows, ncols = z.shape
    accum = np.ones(z.shape, dtype=np.float64)

    order = np.argsort(z.ravel())[::-1]          # highest first
    rows, cols = np.unravel_index(order, z.shape)

    for r, c in zip(rows, cols):
        k = direction[r, c]
        if k < 0:
            continue
        dr, dc, _ = NEIGHBOURS[k]
        accum[r + dr, c + dc] += accum[r, c]

    return accum


def edge_draining_cells(z: np.ndarray, direction: np.ndarray) -> np.ndarray:
    """
    Cells that receive flow from the grid border.

    A border cell whose flow points INTO the grid sits on ground sloping
    inward from the map edge, so the land beyond the edge very likely drains
    in as well. Any outlet downstream of such a cell has a catchment cut off
    by the map boundary, and its area would be underestimated. Border cells
    that drain off the map (divides and outflows) do not taint anything.
    Propagated highest-first like flow accumulation.
    """
    nrows, ncols = z.shape
    border = np.zeros(z.shape, dtype=bool)
    border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = True
    tainted = border & (direction >= 0)
    order = np.argsort(z.ravel())[::-1]
    rows, cols = np.unravel_index(order, z.shape)
    for r, c in zip(rows, cols):
        if not tainted[r, c]:
            continue
        k = direction[r, c]
        if k < 0:
            continue
        dr, dc, _ = NEIGHBOURS[k]
        tainted[r + dr, c + dc] = True
    return tainted


def delineate_catchment(direction: np.ndarray, outlet: tuple[int, int]) -> np.ndarray:
    """
    Boolean mask of every cell whose water eventually passes through `outlet`.

    Walks the flow graph backwards: start at the outlet, repeatedly add any
    neighbour that flows INTO a cell already in the set.
    """
    nrows, ncols = direction.shape
    mask = np.zeros(direction.shape, dtype=bool)
    stack = [outlet]
    mask[outlet] = True

    while stack:
        r, c = stack.pop()
        for k, (dr, dc, _) in enumerate(NEIGHBOURS):
            nr, nc = r - dr, c - dc          # the cell that would flow to (r,c)
            if nr < 0 or nr >= nrows or nc < 0 or nc >= ncols or mask[nr, nc]:
                continue
            if direction[nr, nc] == k:
                mask[nr, nc] = True
                stack.append((nr, nc))

    return mask


def trace_boundary(mask: np.ndarray) -> list[tuple[int, int]]:
    """
    Outline of the mask as an ordered ring of cell-corner coordinates.

    Every cell edge that has mask on one side and not-mask on the other is a
    boundary segment. Collect them all, then stitch them end-to-end into a
    closed loop. Result is a staircase following exact cell edges (no
    smoothing, so the polygon area matches the reported cell count).
    """
    nrows, ncols = mask.shape
    edges: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def add(a, b):
        edges.setdefault(a, []).append(b)

    for r in range(nrows):
        for c in range(ncols):
            if not mask[r, c]:
                continue
            # Corners of this cell, in (row_edge, col_edge) index space.
            bl, br_, tl, tr = (r, c), (r, c + 1), (r + 1, c), (r + 1, c + 1)
            if r == 0 or not mask[r - 1, c]:
                add(bl, br_)                     # south edge
            if c == ncols - 1 or not mask[r, c + 1]:
                add(br_, tr)                     # east edge
            if r == nrows - 1 or not mask[r + 1, c]:
                add(tr, tl)                      # north edge
            if c == 0 or not mask[r, c - 1]:
                add(tl, bl)                      # west edge

    if not edges:
        return []

    start = min(edges)
    ring = [start]
    current = start
    while True:
        outs = edges.get(current)
        if not outs:
            break
        nxt = outs.pop()
        if not outs:
            del edges[current]
        ring.append(nxt)
        current = nxt
        if current == start:
            break

    # Drop collinear midpoints so long straight runs become single segments.
    simplified = [ring[0]]
    for i in range(1, len(ring) - 1):
        pr, cur, nx = simplified[-1], ring[i], ring[i + 1]
        cross = (cur[0] - pr[0]) * (nx[1] - pr[1]) - (cur[1] - pr[1]) * (nx[0] - pr[0])
        if cross != 0:
            simplified.append(cur)
    simplified.append(ring[-1])
    return simplified
