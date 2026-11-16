"""Immutable, single-record WRF adapter; no solver formulas or wrf-python dependency."""
import hashlib
import math
from pathlib import Path

import numpy as np

from .coordinates import WRFCoordinates, FRAME_ID, HEIGHT_DATUM

RAW_NAMES = ('U', 'V', 'W', 'PH', 'PHB', 'HGT', 'XLAT', 'XLONG', 'COSALPHA', 'SINALPHA', 'MAPFAC_M')
ATTR_NAMES = ('MAP_PROJ', 'GRID_ID', 'TRUELAT1', 'TRUELAT2', 'STAND_LON', 'MOAD_CEN_LAT', 'DX', 'DY')
EXPECTED_UNITS = {n: 'm s-1' for n in ('U', 'V', 'W')}
EXPECTED_UNITS.update(PH='m2 s-2', PHB='m2 s-2', HGT='m', XLAT='degree_north', XLONG='degree_east',
                      COSALPHA='', SINALPHA='', MAPFAC_M='')


class AdapterError(ValueError):
    def __init__(self, status, detail=''):
        self.status = status
        super().__init__(status + (': ' + detail if detail else ''))


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def text_attr(value):
    return value.decode('utf-8') if isinstance(value, bytes) else str(value)


def array_copy(value):
    # Preserve float32 source addition semantics and retain masks as missing data.
    a = np.ma.asarray(value)
    if a.dtype.kind != 'f':
        a = a.astype(float)
    return np.array(a.filled(np.nan), copy=True)


def destagger(a, axis):
    left, right = [slice(None)]*a.ndim, [slice(None)]*a.ndim
    left[axis], right[axis] = slice(0, -1), slice(1, None)
    return .5*np.asarray(a[tuple(left)]+a[tuple(right)], dtype=np.float64)


class StaticWRFWindAdapter:
    frame_id = FRAME_ID
    height_datum = HEIGHT_DATUM
    kind = 'WRF_STATIC_SNAPSHOT'

    def __init__(self, raw, attributes, source, *, roi):
        missing = set(RAW_NAMES)-set(raw)
        if missing:
            raise AdapterError('MISSING_VARIABLE', ','.join(sorted(missing)))
        a = {name: array_copy(raw[name]) for name in RAW_NAMES}
        if a['HGT'].ndim != 2:
            raise AdapterError('SHAPE_ERROR', 'HGT must be a mass-grid matrix')
        ny, nx = a['HGT'].shape
        if a['PH'].ndim != 3:
            raise AdapterError('SHAPE_ERROR', 'PH must be a vertical interface array')
        nz = a['PH'].shape[0]-1
        shapes = {'U': (nz,ny,nx+1), 'V': (nz,ny+1,nx), 'W': (nz+1,ny,nx),
                  'PH': (nz+1,ny,nx), 'PHB': (nz+1,ny,nx)}
        shapes.update({n: (ny,nx) for n in ('HGT','XLAT','XLONG','COSALPHA','SINALPHA','MAPFAC_M')})
        if nz < 2 or any(a[n].shape != shapes[n] for n in RAW_NAMES):
            raise AdapterError('SHAPE_ERROR', 'Staggered dimensions do not match mass grid')
        if not all(source.get(k) is not None for k in ('snapshot_id','source_time','source_sha256','record_index')):
            raise AdapterError('SNAPSHOT_MISMATCH', 'Explicit source identity is required')
        self._source = dict(source)
        self.coordinates = WRFCoordinates(attributes, (float(a['XLAT'][0,0]), float(a['XLONG'][0,0])),
                                          (ny,nx), roi=roi)
        self.roi = roi
        self.u, self.v, self.w = destagger(a['U'],2), destagger(a['V'],1), destagger(a['W'],0)
        self.height = destagger(a['PH']+a['PHB'],0)/9.81
        self.terrain = np.asarray(a['HGT'], dtype=float)
        self.mapfac = np.asarray(a['MAPFAC_M'], dtype=float)
        ca, sa = a['COSALPHA'].astype(float), a['SINALPHA'].astype(float)
        self.east, self.north = self.u*ca-self.v*sa, self.u*sa+self.v*ca
        for array in (self.u,self.v,self.w,self.height,self.terrain,self.mapfac,self.east,self.north):
            array.setflags(write=False)

    @property
    def source(self):
        return dict(self._source)

    @classmethod
    def from_wrfout(cls, path, *, expected_sha256, expected_time, case_id, roi, record_index=0):
        path = Path(path)
        if isinstance(record_index, bool) or not isinstance(record_index, int) or record_index < 0:
            raise AdapterError('INVALID_INPUT', 'record_index must be a nonnegative integer')
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise AdapterError('INVALID_INPUT', 'Full source SHA256 is required')
        before = path.stat()
        actual_hash = file_sha256(path)
        if actual_hash != expected_sha256.lower():
            raise AdapterError('SOURCE_CHANGED', str(path))
        try:
            from netCDF4 import Dataset
            dataset, backend = Dataset(str(path), 'r'), 'netCDF4'
        except ImportError:
            from scipy.io import netcdf_file
            try:
                dataset, backend = netcdf_file(str(path), 'r', mmap=False), 'scipy.io.netcdf_file'
            except (TypeError, ValueError) as exc:
                raise AdapterError('UNSUPPORTED_NETCDF_BACKEND', 'Install netCDF4 for non-classic NetCDF input') from exc
        try:
            if 'Times' not in dataset.variables:
                raise AdapterError('MISSING_VARIABLE', 'Times')
            time_array = dataset.variables['Times'][:]
            if record_index >= len(time_array):
                raise AdapterError('SNAPSHOT_MISMATCH', 'record_index out of bounds')
            source_time = b''.join(np.asarray(time_array[record_index]).tolist()).decode('ascii').strip('\x00 ')
            if source_time != expected_time:
                raise AdapterError('SNAPSHOT_MISMATCH', 'Times does not match explicit expected time')
            raw = {}
            expected_dims = {
                'U': ('Time','bottom_top','south_north','west_east_stag'),
                'V': ('Time','bottom_top','south_north_stag','west_east'),
                'W': ('Time','bottom_top_stag','south_north','west_east'),
                'PH': ('Time','bottom_top_stag','south_north','west_east'),
                'PHB': ('Time','bottom_top_stag','south_north','west_east')}
            for name in RAW_NAMES:
                if name not in dataset.variables:
                    raise AdapterError('MISSING_VARIABLE', name)
                variable = dataset.variables[name]
                if tuple(variable.dimensions) != expected_dims.get(name, ('Time','south_north','west_east')):
                    raise AdapterError('DIMENSION_MISMATCH', name)
                if text_attr(getattr(variable, 'units', '')) != EXPECTED_UNITS[name]:
                    raise AdapterError('UNIT_MISMATCH', name)
                values = np.ma.asarray(variable[record_index])
                # scipy's classic reader does not mask _FillValue / missing_value.
                for attr in ('_FillValue', 'missing_value'):
                    if hasattr(variable, attr):
                        values = np.ma.masked_equal(values, getattr(variable, attr))
                raw[name] = array_copy(values)
            attributes = {name: float(getattr(dataset, name)) for name in ATTR_NAMES}
        finally:
            dataset.close()
        after = path.stat()
        if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns) or file_sha256(path) != actual_hash:
            raise AdapterError('SOURCE_CHANGED', 'File changed during binding')
        source = {'snapshot_id': case_id+'/'+source_time, 'source_time': source_time,
                  'source_sha256': actual_hash, 'record_index': record_index, 'path': str(path),
                  'size_bytes': before.st_size, 'netcdf_backend': backend,
                  'source_time_convention': 'UTC', 'binding': 'HASH_VERIFIED_IMMUTABLE_IN_MEMORY_ARRAYS'}
        return cls(raw, attributes, source, roi=roi)

    def _base(self):
        return {'wx': None, 'wy': None, 'wz': None, 'valid': False,
                'frame_id': self.frame_id, 'height_datum': self.height_datum,
                'time_mode': 'BOUND_STATIC_SNAPSHOT', 'source_indices': None,
                **{k:self._source[k] for k in ('snapshot_id','source_time','source_sha256','record_index')}}

    def _horizontal(self, x, y):
        if not self.roi.contains(x,y):
            raise AdapterError('OUT_OF_ROI')
        i,j = self.coordinates.local_to_grid(x,y)
        nx,ny = self.coordinates.nx,self.coordinates.ny
        tol = 64*np.finfo(float).eps*max(nx,ny)
        if i < -tol or j < -tol or i > nx-1+tol or j > ny-1+tol:
            raise AdapterError('OUT_OF_HORIZONTAL_DOMAIN')
        original = [i,j]
        i,j = min(nx-1.,max(0.,i)),min(ny-1.,max(0.,j))
        i0,j0 = min(int(math.floor(i)),nx-2),min(int(math.floor(j)),ny-2)
        a,b = i-i0,j-j0
        weights = np.array([(1-a)*(1-b),a*(1-b),(1-a)*b,a*b])
        def interp(field):
            corners = np.stack((field[...,j0,i0],field[...,j0,i0+1],field[...,j0+1,i0],field[...,j0+1,i0+1]),axis=-1)
            if not np.isfinite(corners).all():
                raise AdapterError('MISSING_DATA')
            return corners@weights
        return interp, i,j,[i-original[0],j-original[1]]

    def wind_query(self, x, y, z, t):
        result = self._base()
        try:
            x,y,z,t = map(float,(x,y,z,t))
            if not all(math.isfinite(v) for v in (x,y,z,t)) or t < 0:
                raise AdapterError('INVALID_INPUT')
            interp,i,j,adjust = self._horizontal(x,y)
            h = interp(self.height)
            terrain = float(interp(self.terrain))
            if np.any(np.diff(h) <= 0):
                raise AdapterError('NON_MONOTONE_HEIGHT')
            if z <= terrain:
                raise AdapterError('BELOW_TERRAIN')
            if z < h[0]:
                raise AdapterError('BELOW_VERTICAL_DOMAIN')
            if z > h[-1]:
                raise AdapterError('ABOVE_VERTICAL_DOMAIN')
            k = min(max(int(np.searchsorted(h,z,side='right'))-1,0),len(h)-2)
            a = (z-h[k])/(h[k+1]-h[k])
            def vertical(field):
                column = interp(field)
                return float((1-a)*column[k]+a*column[k+1])
            wx,wy,wz = [vertical(f) for f in (self.u,self.v,self.w)]
            mapfac = float(interp(self.mapfac))
            if mapfac <= 0:
                raise AdapterError('INVALID_MAP_FACTOR')
            result.update(wx=wx,wy=wy,wz=wz,valid=True,status='OK',
                          source_indices={'i':i,'j':j,'k':k,'vertical_weight':float(a)},
                          roundoff_grid_adjustment=adjust, terrain_msl_m=terrain, agl_m=z-terrain,
                          vertical_domain_msl_m=[float(h[0]),float(h[-1])],map_factor=mapfac,
                          earth_diagnostic={'east':vertical(self.east),'north':vertical(self.north),'up':wz})
        except AdapterError as exc:
            result.update(status=exc.status,detail=str(exc))
        except (TypeError,ValueError,OverflowError) as exc:
            result.update(status='INVALID_INPUT',detail=str(exc))
        return result

    __call__ = wind_query
