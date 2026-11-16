"""Closed native cells: a segment touching a side or vertex visits every cell."""
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
import math

from .planning_control import checkpoint
from .route_service import haversine_km

SPATIAL_RISK_MODEL = "CLOSED_CELL_SUPERCOVER_V2"
COORD_TOLERANCE = 1e-10  # Floating geometry tolerance in degrees, not a safety buffer.


@dataclass(frozen=True)
class CellVisit:
    row: int
    col: int
    enter: float
    leave: float
    start_point: tuple[float, float]
    end_point: tuple[float, float]


@dataclass(frozen=True)
class BoundaryContact:
    offset: float
    cells: tuple
    exposure_km: float


@lru_cache(maxsize=32)
def _axis(values):
    indices = tuple(sorted(range(len(values)), key=values.__getitem__))
    centers = tuple(float(values[i]) for i in indices)
    if len(centers) < 2 or any(not math.isfinite(value) for value in centers) or any(a >= b for a, b in zip(centers, centers[1:])):
        raise ValueError("风场坐标轴必须至少包含两个不同格点")
    return centers, tuple((a + b) / 2 for a, b in zip(centers, centers[1:])), indices


def axes(grid):
    return _axis(tuple(grid.lons)), _axis(tuple(grid.lats))


def _axis_cells(axis, value):
    centers, edges, indices = axis
    if value < centers[0] - COORD_TOLERANCE or value > centers[-1] + COORD_TOLERANCE:
        return ()
    index = bisect_right(edges, value)
    bins = {index}
    for edge in (index - 1, index):
        if 0 <= edge < len(edges) and abs(value - edges[edge]) <= COORD_TOLERANCE:
            bins.update((edge, edge + 1))
    return tuple(sorted(indices[i] for i in bins))


def point_cells(grid, point, prepared=None):
    if not all(math.isfinite(value) for value in point[:2]):
        return ()
    lon_axis, lat_axis = prepared or axes(grid)
    rows, cols = _axis_cells(lat_axis, point[1]), _axis_cells(lon_axis, point[0])
    return tuple((r, c) for r in rows for c in cols)


def _parts(a, b, prepared):
    cuts = [0., 1.]
    for dimension, axis in enumerate(prepared):
        delta = b[dimension] - a[dimension]
        if delta:
            lo, hi = sorted((a[dimension], b[dimension]))
            edges = axis[1]
            for edge in edges[bisect_left(edges, lo):bisect_right(edges, hi)]:
                cuts.append(min(1., max(0., (edge - a[dimension]) / delta)))
    result = [0.]
    scale = max(abs(b[0]-a[0]), abs(b[1]-a[1]), 1e-12)
    for value in sorted(cuts):
        if 0 < value < 1 and (value - result[-1]) * scale > COORD_TOLERANCE:
            result.append(value)
    if len(result)>1 and (1-result[-1])*scale <= COORD_TOLERANCE:
        result[-1] = 1.
    else:
        result.append(1.)
    return result


def _at(a, b, fraction):
    return tuple(x + fraction * (y - x) for x, y in zip(a, b))


def cell_visits(points, grid, speed):
    prepared = axes(grid)
    if any(not point_cells(grid, p, prepared) for p in points):
        raise ValueError("固定航线超出风场空间覆盖范围")
    positive, contacts = [], []
    elapsed = 0.
    for a, b in zip(points, points[1:]):
        checkpoint()
        a, b = tuple(a[:2]), tuple(b[:2])
        duration = haversine_km(a, b) * 1000 / speed
        cuts = _parts(a, b, prepared)
        for begin, end in zip(cuts, cuts[1:]):
            if duration * (end - begin) <= 0:
                continue
            for row, col in point_cells(grid, _at(a, b, (begin + end) / 2), prepared):
                positive.append(CellVisit(row, col, elapsed + duration * begin, elapsed + duration * end,
                                          _at(a, b, begin), _at(a, b, end)))
        for cut in cuts:
            point, offset = _at(a, b, cut), elapsed + duration * cut
            contacts.extend(CellVisit(row, col, offset, offset, point, point)
                            for row, col in point_cells(grid, point, prepared))
        elapsed += duration
    # Endpoint closure of a positive visit already covers that contact. Keep
    # isolated side cells at a diagonal corner as explicit zero-duration visits.
    occupied = defaultdict(list)
    for visit in positive:
        occupied[visit.row, visit.col].append((visit.enter, visit.leave))
    seen = set()
    for visit in contacts:
        key = visit.row, visit.col, visit.enter
        if key not in seen and not any(a <= visit.enter <= b for a, b in occupied[visit.row, visit.col]):
            positive.append(visit)
            seen.add(key)
    return sorted(positive, key=lambda v: (v.enter, v.leave, v.row, v.col))


def occupancy_spans(visits):
    """Partition positive occupancy; boundary-following cells overlap in time."""
    starts, ends = defaultdict(list), defaultdict(list)
    for visit in visits:
        if visit.leave > visit.enter:
            starts[visit.enter].append((visit.row, visit.col))
            ends[visit.leave].append((visit.row, visit.col))
    cuts, active, result = sorted(starts.keys() | ends.keys()), Counter(), []
    for index, stamp in enumerate(cuts[:-1]):
        active.subtract(ends[stamp])
        active.update(starts[stamp])
        result.append((stamp, cuts[index+1], tuple(sorted(cell for cell, count in active.items() if count > 0))))
    return tuple(result)


def shear_contacts(visits):
    """All cells simultaneously touched, including a full shared boundary."""
    active, result, seen = [], [], set()
    for visit in sorted(visits, key=lambda v: v.enter):
        active = [other for other in active if other.leave >= visit.enter]
        for other in active:
            cells = tuple(sorted(((other.row, other.col), (visit.row, visit.col))))
            if cells[0] == cells[1]:
                continue
            begin, end = max(other.enter, visit.enter), min(other.leave, visit.leave)
            key = cells, begin, end
            if key in seen:
                continue
            seen.add(key)
            span = visit.leave - visit.enter
            point = lambda stamp: _at(visit.start_point, visit.end_point, (stamp-visit.enter)/span) if span else visit.start_point
            result.append((cells[0], cells[1], CellVisit(visit.row, visit.col, begin, end, point(begin), point(end))))
        active.append(visit)
    return tuple(result)


def boundary_contacts(points, grid, speed):
    """Nonzero risk toll at each grid crossing, invariant to collinear splitting.

    Use half the smallest local cell spacing as equivalent exposure. Endpoint
    crossings pay half, so two split edges pay exactly one crossing. A line
    following a boundary uses both cells in the positive-length integral; an
    arbitrary waypoint along that boundary does not create another toll.
    """
    prepared, result, elapsed = axes(grid), [], 0.
    for a, b in zip(points, points[1:]):
        a, b = tuple(a[:2]), tuple(b[:2])
        duration = haversine_km(a, b) * 1000 / speed
        if not duration:
            continue
        for cut in _parts(a, b, prepared):
            point = _at(a, b, cut)
            crossing = any(b[dimension] != a[dimension] and len(_axis_cells(axis, point[dimension])) > 1
                           for dimension, axis in enumerate(prepared))
            if not crossing:
                continue
            cells = point_cells(grid, point, prepared)
            spacings = []
            for dimension, axis in enumerate(prepared):
                centers, edges, _ = axis
                near = bisect_right(edges, point[dimension])
                for index in (near-1, near):
                    if 0 <= index < len(centers)-1:
                        left, right = list(point), list(point)
                        left[dimension], right[dimension] = centers[index:index+2]
                        spacings.append(haversine_km(left, right))
            scale = .5 * min(spacings) * (.5 if cut == 0. or cut == 1. else 1.)
            result.append(BoundaryContact(elapsed + duration * cut, cells, scale))
        elapsed += duration
    return tuple(result)
