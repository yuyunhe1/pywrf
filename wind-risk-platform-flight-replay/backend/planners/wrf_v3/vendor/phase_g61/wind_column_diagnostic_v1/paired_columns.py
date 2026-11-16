"""Immutable adjacent-layer stencils; retain original scalar interpolation.

Heights still use every original level to choose the exact original bracket.
Only U/V/W at its two adjacent levels are multiplied by the current point's
four weights. No wind value, query position, or query time is cached.
"""
from types import MappingProxyType
import numpy as np
from phase_g61.uncovered_environments_v1.shared_geometry import SharedGeometryTemporal
from phase_g61.online_execution_repair_v1.velocity_provider import immutable
from phase3a_implementation.adapter import AdapterError

POLICY = dict(version='ADJACENT_LEVEL_UVW_SINGLE_MATRIX_V1',
    height_bracket='ORIGINAL_FULL_HEIGHT_MATRIX_AND_SEARCHSORTED_RIGHT',
    winds='ORIGINAL_TWO_LEVELS_IN_ORIGINAL_ORDER_COMBINED_SIX_BY_FOUR_MATRIX',
    interpolation='ORIGINAL_BILINEAR_THEN_VERTICAL_THEN_NATIVE_TIME_LINEAR',
    no_wind_value_cache=True, no_approximate_query_or_time_hold=True,
    unsafe_cells='ORIGINAL_FALLBACK_AND_STATUS_ORDER',
    C_collision_and_ordered_methods_changed=False)

class PairedColumnsTemporal(SharedGeometryTemporal):
    @classmethod
    def from_records(cls, *args, **kwargs):
        obj = super().from_records(*args, **kwargs)
        material = []
        for pack in obj._velocity_packs:
            cells = {}
            for key, (columns, terrain, mapfac, finite) in pack.items():
                # [k, u(k), u(k+1), v(k), v(k+1), w(k), w(k+1)]
                cells[key] = immutable(np.stack([
                    np.stack([columns[n][k + q] for n in (1, 2, 3) for q in (0, 1)])
                    for k in range(len(columns[0])-1)])) if finite else None
            material.append(MappingProxyType(cells))
        object.__setattr__(obj, '_paired_columns', tuple(material))
        object.__setattr__(obj, '_paired_columns_ids', tuple(id(p) for p in material))
        obj.assert_bound()
        return obj

    def assert_bound(self):
        super().assert_bound()
        if '_paired_columns' in self.__dict__:
            if tuple(id(p) for p in self._paired_columns) != self._paired_columns_ids:
                raise AdapterError('PAIRED_COLUMN_MATERIAL_CHANGED')
        return True

    def native_geometry(self, index, z, geom):
        x, y, i0, j0, weights = geom
        cell = self._velocity_packs[index].get((i0, j0))
        if cell is None:
            raise AdapterError('VELOCITY_MATERIAL_CELL_NOT_PREPARED')
        columns, terrain_c, mapfac_c, finite = cell
        if not finite:
            return super().native_geometry(index, z, geom)
        monotone, positive_map = self._cell_convex_proofs[index][(i0, j0)]
        h = columns[0] @ weights
        terrain = float(terrain_c @ weights)
        if not monotone and np.any(np.diff(h) <= 0):
            raise AdapterError('NON_MONOTONE_HEIGHT')
        if z <= terrain:
            raise AdapterError('BELOW_TERRAIN')
        if z < h[0]:
            raise AdapterError('BELOW_VERTICAL_DOMAIN')
        if z > h[-1]:
            raise AdapterError('ABOVE_VERTICAL_DOMAIN')
        k = min(max(int(np.searchsorted(h, z, side='right')) - 1, 0), len(h)-2)
        weight = (z-h[k]) / (h[k+1]-h[k])
        nodes = self._paired_columns[index][(i0, j0)][k] @ weights
        # Scalar expressions retain the original arithmetic sequence, including
        # NumPy scalar multiplication/addition before conversion to Python float.
        winds = (float((1-weight)*nodes[0]+weight*nodes[1]),
                 float((1-weight)*nodes[2]+weight*nodes[3]),
                 float((1-weight)*nodes[4]+weight*nodes[5]))
        if not positive_map and float(mapfac_c @ weights) <= 0:
            raise AdapterError('INVALID_MAP_FACTOR')
        return winds

    def query_stats(self):
        out = super().query_stats()
        out['paired_columns_policy'] = POLICY
        return out
