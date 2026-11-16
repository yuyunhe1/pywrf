"""Reject fixed-route suggestions that do not change spatial weather exposure.

This is deliberately not search-state pruning: later arrivals and revisits are
still valid in the temporal planner. A user-facing detour must actually avoid
different weather cells rather than manufacture a benefit by flying in circles.
"""
import math

from .grid_coverage import cell_visits, axes
from .planning_control import checkpoint
from .route_service import EARTH_RADIUS_KM


class DetourWeatherFilter:
    def __init__(self, dataset, original, speed):
        self.dataset, self.speed = dataset, speed
        self.grid = dataset.frames[0][1]
        self.start = original[0]
        self.cells = self._cells(original)
        self.signatures = {}

    def confined_to_one_cell(self, maximum_km):
        """Prove the distance cap cannot reach any internal cell boundary.

        Every vertex is within the total allowed path length of A. If even
        that ball cannot reach a boundary, all vertices and their connecting
        straight segments stay inside the same rectangular native cell.
        """
        if len(self.cells) != 1:
            return False
        row, col = next(iter(self.cells))
        if self._signature((row, col)) is None:
            return False
        distances = []
        lon, lat = map(math.radians, self.start[:2])
        for dimension, (centers, edges, indices) in enumerate(axes(self.grid)):
            index = indices.index(col if dimension == 0 else row)
            for edge in (index - 1, index):
                if not 0 <= edge < len(edges):
                    continue
                boundary = math.radians(edges[edge])
                # Distance to the whole meridian great circle is a lower
                # bound for reaching its finite cell side.
                delta = (math.asin(min(1., abs(math.cos(lat)*math.sin(lon-boundary))))
                         if dimension == 0 else abs(lat-boundary))
                distances.append(EARTH_RADIUS_KM * delta)
        return bool(distances) and maximum_km + 1e-8 < min(distances)

    def _cells(self, points):
        # Includes side/vertex contacts, even when their occupancy is zero.
        return frozenset((visit.row, visit.col) for visit in cell_visits(points, self.grid, self.speed))

    def _signature(self, cell):
        if cell not in self.signatures:
            row, col = cell
            values = []
            for _, grid in self.dataset.frames:
                checkpoint()
                u, v = float(grid.u[row, col]), float(grid.v[row, col])
                if not math.isfinite(u) or not math.isfinite(v):
                    self.signatures[cell] = None
                    break
                values.append((u, v))
            else:
                self.signatures[cell] = tuple(values)
        return self.signatures[cell]

    def redundant(self, points):
        try:
            cells = self._cells(points)
            original = {self._signature(cell) for cell in self.cells}
            proposed = {self._signature(cell) for cell in cells}
        except (ValueError, IndexError, OSError):
            # Missing coverage is handled by full-flight validation; it is not
            # evidence that two weather corridors are equivalent.
            return False
        if None in original or None in proposed:
            return False
        if cells == self.cells:
            return True
        # A proper subset may remove a hazardous corner/side contact, so keep
        # that geometric improvement for the normal window-benefit check.
        if cells < self.cells:
            return False
        # Exact U/V histories at every available hour imply equality in all
        # interpolated ten-minute frames too. Never round or merge close winds.
        return original == proposed
