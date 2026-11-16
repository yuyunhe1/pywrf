"""Standalone exact-spatial/linear-UTC WRF query interface; not a static solver adapter."""
import bisect
import copy
import datetime as dt
import hashlib
import math
from numbers import Real
from pathlib import Path
from types import MappingProxyType

import numpy as np

from phase3a_implementation.adapter import StaticWRFWindAdapter, AdapterError, ATTR_NAMES
from phase3a_implementation.coordinates import ROI, WRFCoordinates
from phase_g61.common import content

UTC = dt.timezone.utc
GRID_FIELDS = ('XLAT', 'XLONG', 'HGT', 'COSALPHA', 'SINALPHA', 'MAPFAC_M')
ARRAY_FIELDS = ('u', 'v', 'w', 'height', 'terrain', 'mapfac', 'east', 'north')


def _freeze(value):
    if isinstance(value,dict):return MappingProxyType({k:_freeze(v) for k,v in value.items()})
    if isinstance(value,(list,tuple)):return tuple(_freeze(v) for v in value)
    return value


def _thaw(value):
    if isinstance(value,(dict,MappingProxyType)):return {k:_thaw(v) for k,v in value.items()}
    if isinstance(value,tuple):return [_thaw(v) for v in value]
    return copy.deepcopy(value)


class _SemanticLock:
    """Reject ordinary mutation; assert_bound also detects forced object writes."""
    def __setattr__(self,name,value):
        if self.__dict__.get('_locked',False):
            raise AdapterError('TEMPORAL_BINDING_IMMUTABLE',name)
        object.__setattr__(self,name,value)

    def __delattr__(self,name):
        if self.__dict__.get('_locked',False):
            raise AdapterError('TEMPORAL_BINDING_IMMUTABLE',name)
        object.__delattr__(self,name)


def _coordinate_signature(c):
    return (id(c.roi),c.roi.xmin,c.roi.xmax,c.roi.ymin,c.roi.ymax,
            id(c.attributes),tuple(sorted(c.attributes.items())),
            c.dx,c.dy,c.nx,c.ny,c.n,c.f,c.lon0,c.rho0,c.origin_projected,
            c.m0,c.anchor_projected,c.radius_m,c.origin_lat_lon,c.frame_id)


class _BoundCoordinates(_SemanticLock,WRFCoordinates):
    @classmethod
    def from_coordinates(cls,raw):
        obj=cls.__new__(cls)
        for k,v in vars(raw).items():object.__setattr__(obj,k,_freeze(v))
        object.__setattr__(obj,'_signature',_coordinate_signature(obj))
        object.__setattr__(obj,'_locked',True)
        return obj

    def assert_bound(self):
        if self.__dict__.get('_locked') is not True or _coordinate_signature(self)!=self._signature:
            raise AdapterError('TEMPORAL_COORDINATES_CHANGED')
        return True


def _array_signature(value):
    return (id(value),value.shape,value.dtype.str,value.strides,bool(value.flags.writeable))


class _BoundStaticAdapter(_SemanticLock,StaticWRFWindAdapter):
    """Same source/interpolation, with bytes-backed arrays and frozen metadata."""
    @classmethod
    def from_adapter(cls,raw):
        obj=cls.__new__(cls)
        for k,v in vars(raw).items():
            if k in ARRAY_FIELDS:
                # Immutable bytes prevent setflags(write=True); values/dtype and
                # original spatial interpolation arithmetic are unchanged.
                v=np.frombuffer(v.tobytes(order='C'),dtype=v.dtype).reshape(v.shape)
            elif k=='coordinates':v=_BoundCoordinates.from_coordinates(v)
            elif k=='_source':v=_freeze(dict(v))
            object.__setattr__(obj,k,v)
        object.__setattr__(obj,'_signature',obj._live_signature())
        object.__setattr__(obj,'_locked',True)
        return obj

    def _live_signature(self):
        return (id(self._source),id(self.roi),self.roi.xmin,self.roi.xmax,self.roi.ymin,self.roi.ymax,
                id(self.coordinates),self.frame_id,self.height_datum,self.kind,
                tuple(_array_signature(getattr(self,name)) for name in ARRAY_FIELDS))

    def assert_bound(self):
        if self.__dict__.get('_locked') is not True or self._live_signature()!=self._signature:
            raise AdapterError('TEMPORAL_SOURCE_BINDING_CHANGED')
        self.coordinates.assert_bound()
        return True

    def wind_query(self,*args,**kwargs):
        self.assert_bound()
        return super().wind_query(*args,**kwargs)

    __call__=wind_query


def utc_native(value):
    return dt.datetime.strptime(value, '%Y-%m-%d_%H:%M:%S').replace(tzinfo=UTC)


def checked_epoch(value):
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AdapterError('UTC_EPOCH_REQUIRED')
    return value.astimezone(UTC)


def _metadata(path, index, expected_sha256):
    from phase3a_implementation.adapter import file_sha256
    path=Path(path)
    before=path.stat()
    if file_sha256(path)!=expected_sha256:
        raise AdapterError('SOURCE_CHANGED')
    try:
        from netCDF4 import Dataset
        ds = Dataset(str(path), 'r')
    except ImportError:
        from scipy.io import netcdf_file
        ds = netcdf_file(str(path), 'r', mmap=False)
    try:
        if 'Times' not in ds.variables or index>=len(ds.variables['Times'][:]):
            raise AdapterError('SNAPSHOT_MISMATCH','record_index out of bounds')
        grid = {}
        for name in GRID_FIELDS:
            a = np.ma.asarray(ds.variables[name][index])
            if np.ma.getmaskarray(a).any() or not np.isfinite(a).all():
                raise AdapterError('GRID_MISSING_DATA', name)
            a = np.ascontiguousarray(a)
            grid[name] = dict(dtype=str(a.dtype), shape=list(a.shape),
                              sha256=hashlib.sha256(a.tobytes()).hexdigest())
        grid['projection'] = {k: float(getattr(ds, k)) for k in ATTR_NAMES}
        from phase3a_implementation.adapter import text_attr
        lineage = {k: text_attr(getattr(ds, k, '')) for k in ('START_DATE', 'SIMULATION_START_DATE')}
        if not all(lineage.values()):
            raise AdapterError('SOURCE_LINEAGE_REQUIRED')
    finally:
        ds.close()
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or file_sha256(path)!=expected_sha256:
        raise AdapterError('SOURCE_CHANGED','METADATA_READ')
    return grid, lineage


class TemporalWRF(_SemanticLock):
    """No extrapolation and no query cache; every source query uses current position."""
    kind = 'WRF_TIME_LINEAR_SNAPSHOTS'
    time_mode = 'EXPLICIT_UTC_LINEAR_BRACKET_NO_EXTRAPOLATION'

    @classmethod
    def from_records(cls, records, *, roi, epoch_utc, max_gap_s, source_time_convention):
        if source_time_convention != 'UTC':
            raise AdapterError('UTC_SOURCE_CONVENTION_REQUIRED')
        checked_epoch(epoch_utc)
        if isinstance(max_gap_s, (bool,np.bool_)) or not isinstance(max_gap_s,Real) or not math.isfinite(max_gap_s) or max_gap_s <= 0:
            raise AdapterError('INVALID_MAX_TIME_GAP')
        if not isinstance(roi,ROI):raise AdapterError('INVALID_ROI')
        if not isinstance(records,(list,tuple)) or any(not isinstance(r,dict) for r in records):
            raise AdapterError('INVALID_SOURCE_SCHEMA')
        if len(records) < 2:
            raise AdapterError('AT_LEAST_TWO_NATIVE_TIMES_REQUIRED')
        for r in records:
            if any(k not in r for k in ('path','expected_sha256','expected_time','case_id','source_run_id')):
                raise AdapterError('INVALID_SOURCE_SCHEMA')
            if not all(isinstance(r[k],str) and r[k] for k in ('path','expected_sha256','expected_time','case_id','source_run_id')):
                raise AdapterError('INVALID_SOURCE_SCHEMA')
            if len(r['expected_sha256'])!=64 or any(c not in '0123456789abcdef' for c in r['expected_sha256']):
                raise AdapterError('INVALID_SOURCE_SCHEMA')
            idx=r.get('record_index',0)
            if isinstance(idx,(bool,np.bool_)) or not isinstance(idx,int) or idx<0:
                raise AdapterError('INVALID_RECORD_INDEX')
            try:utc_native(r['expected_time'])
            except (ValueError,TypeError):raise AdapterError('INVALID_NATIVE_TIME')
        if not records[0].get('source_run_id') or any(
                r.get('source_run_id') != records[0]['source_run_id'] or
                r.get('case_id') != records[0].get('case_id') for r in records):
            raise AdapterError('SOURCE_RUN_BINDING_MISMATCH')
        adapters, identity = [], []
        first_grid = first_lineage = None
        previous = None
        for row in records:
            instant = utc_native(row['expected_time'])
            if previous is not None and instant <= previous:
                raise AdapterError('UNSORTED_OR_DUPLICATE_SOURCE_TIMES')
            previous = instant
            grid, lineage = _metadata(row['path'], row.get('record_index', 0),row['expected_sha256'])
            if first_grid is None:
                first_grid, first_lineage = grid, lineage
            elif grid != first_grid:
                raise AdapterError('INCOMPATIBLE_GRID')
            elif lineage != first_lineage:
                raise AdapterError('INCOMPATIBLE_SOURCE_LINEAGE')
            adapter = StaticWRFWindAdapter.from_wrfout(
                row['path'], expected_sha256=row['expected_sha256'], expected_time=row['expected_time'],
                case_id=row['case_id'], roi=roi, record_index=row.get('record_index', 0))
            # Verify the independent metadata read was bound to the same immutable file.
            from phase3a_implementation.adapter import file_sha256
            if file_sha256(row['path']) != adapter.source['source_sha256']:
                raise AdapterError('SOURCE_CHANGED')
            adapter=_BoundStaticAdapter.from_adapter(adapter)
            adapters.append(adapter)
            identity.append(dict(utc=instant.isoformat(), source=adapter.source, grid=grid,
                                 lineage=lineage,source_run_id=row['source_run_id']))
        obj = cls.__new__(cls)
        obj._adapters = tuple(adapters)
        obj._native_utc = tuple(utc_native(r['expected_time']) for r in records)
        obj._offsets = tuple((v-obj._native_utc[0]).total_seconds() for v in obj._native_utc)
        obj.epoch_utc = checked_epoch(epoch_utc)
        obj._epoch_offset = (obj.epoch_utc-obj._native_utc[0]).total_seconds()
        obj.max_gap_s = float(max_gap_s)
        obj.roi = roi
        obj.frame_id, obj.height_datum = adapters[0].frame_id, adapters[0].height_datum
        obj.coordinates = adapters[0].coordinates
        context = dict(version='TEMPORAL_WRF_IMMUTABLE_BINDING_V4',kind=cls.kind, temporal_rule=cls.time_mode, identity=identity,
                           epoch_utc=obj.epoch_utc.isoformat(), max_gap_s=obj.max_gap_s,
                           roi=roi.as_dict(), frame_id=obj.frame_id, height_datum=obj.height_datum,
                           vector_basis='ORIGINAL_NATIVE_GRID_U_V', query_cache='NONE',
                           clock=dict(mission_epoch_utc=obj.epoch_utc.isoformat(),
                               native_utc=[v.isoformat() for v in obj._native_utc],
                               offsets_from_first_source_s=list(obj._offsets),
                               epoch_offset_from_first_source_s=obj._epoch_offset,
                               rule='UTC_EQUALS_MISSION_EPOCH_PLUS_NONNEGATIVE_ELAPSED_SECONDS'),
                           immutable_arrays='EXACT_VALUE_DTYPE_SHAPE_BYTES_BACKED',
                           identity_checks='SEMANTIC_SIGNATURE_AND_EACH_BOUND_SOURCE_ON_EVERY_QUERY')
        obj.context_sha256 = content(context)
        obj._context=_freeze(context)
        obj._source=_freeze(dict(snapshot_id='TEMPORAL_COMPOSITE/'+obj.context_sha256,
            source_sha256=obj.context_sha256,source_identity_kind='COMPOSITE_CONTEXT_SHA256_NOT_RAW_FILE_SHA256',
            source_time='UTC_LINEAR_INTERVAL',record_index=None,
            source_time_convention='UTC',mission_epoch_utc=obj.epoch_utc.isoformat(),
            native_utc=[v.isoformat() for v in obj._native_utc],
            source_records=[dict(utc=v['utc'],raw_source_sha256=v['source']['source_sha256'],
                source_snapshot_id=v['source']['snapshot_id'],record_index=v['source']['record_index'],
                source_run_id=v['source_run_id']) for v in identity],
            binding='HASH_VERIFIED_NATIVE_RECORDS_WITH_IMMUTABLE_COMPOSITE_CLOCK_CONTEXT'))
        obj._trace = []
        obj._signature=obj._live_signature()
        obj._locked=True
        obj.assert_bound()
        return obj

    def _live_signature(self):
        return (self.kind,self.time_mode,self.epoch_utc,self.max_gap_s,self._epoch_offset,
                self._native_utc,self._offsets,id(self._adapters),tuple(id(a) for a in self._adapters),
                id(self.roi),self.roi.xmin,self.roi.xmax,self.roi.ymin,self.roi.ymax,
                self.frame_id,self.height_datum,id(self.coordinates),id(self._context),
                id(self._source),self.context_sha256)

    def assert_bound(self):
        try:
            if self.__dict__.get('_locked') is not True or self._live_signature()!=self._signature:
                raise AdapterError('TEMPORAL_BINDING_CHANGED')
            for adapter in self._adapters:adapter.assert_bound()
        except (AttributeError,TypeError,ValueError) as exc:
            if isinstance(exc,AdapterError):raise
            raise AdapterError('TEMPORAL_BINDING_CHANGED',type(exc).__name__) from exc
        return True

    @property
    def context(self):
        self.assert_bound()
        return _thaw(self._context)

    @property
    def source(self):
        self.assert_bound()
        return _thaw(self._source)

    @property
    def clock(self):
        self.assert_bound()
        return _thaw(self._context['clock'])

    @property
    def trace(self):return copy.deepcopy(self._trace)

    def wind_query(self, x, y, z, t):
        self.assert_bound()
        result = dict(valid=False, status='DYNAMIC_WIND_QUERY_ERROR', wx=None, wy=None, wz=None,
                      frame_id=self.frame_id, height_datum=self.height_datum, kind=self.kind,
                      time_mode=self.time_mode, context_sha256=self.context_sha256,
                      sources=[], query=None, raw_query_repr=repr((x,y,z,t)))
        try:
            raw = (x, y, z, t)
            if any(isinstance(v, (bool, np.bool_)) for v in raw):
                raise AdapterError('INVALID_INPUT')
            x, y, z, t = map(float, raw)
            if not all(math.isfinite(v) for v in (x, y, z, t)) or t < 0:
                raise AdapterError('INVALID_INPUT')
            q = self._epoch_offset+t
            result['query'] = dict(x=x, y=y, z_msl_m=z, mission_elapsed_s=t,
                                    utc=(self.epoch_utc+dt.timedelta(seconds=t)).isoformat(),
                                    offset_from_first_source_s=q)
            if q < self._offsets[0] or q > self._offsets[-1]:
                raise AdapterError('OUT_OF_TIME_DOMAIN')
            j = bisect.bisect_left(self._offsets, q)
            if q == self._offsets[j]:
                pairs = [(j, 1.)]
            else:
                i = j-1
                gap = self._offsets[j]-self._offsets[i]
                if gap > self.max_gap_s:
                    raise AdapterError('TIME_GAP_EXCEEDS_CONTRACT')
                w = (q-self._offsets[i])/gap
                pairs = [(i, 1.-w), (j, w)]
            winds = []
            result['bracket']=[dict(source_utc=self._native_utc[i].isoformat(),weight=w,
                source_sha256=self._adapters[i].source['source_sha256']) for i,w in pairs]
            for i, weight in pairs:
                original = self._adapters[i].wind_query(x, y, z, 0.)
                result['sources'].append(dict(source_utc=self._native_utc[i].isoformat(), weight=weight,
                    source_sha256=self._adapters[i].source['source_sha256'], source_status=original['status'],
                    source_indices=original.get('source_indices'),
                    wx=original['wx'], wy=original['wy'], wz=original['wz']))
                if not original['valid']:
                    raise AdapterError(original['status'], 'INVALID_REQUIRED_TEMPORAL_SOURCE')
                winds.append((original, weight))
            for key in ('wx', 'wy', 'wz'):
                result[key] = math.fsum(original[key]*w for original, w in winds)
            result.update(valid=True, status='OK')
        except AdapterError as exc:
            result.update(valid=False, status=exc.status, detail=str(exc))
        except (TypeError, ValueError, OverflowError) as exc:
            result.update(valid=False, status='INVALID_INPUT', detail=str(exc))
        # Tests persist every record; production adoption requires a bounded durable sink.
        self._trace.append(copy.deepcopy(result))
        return result

    __call__ = wind_query


class MissionSuffixView(_SemanticLock):
    """Explicit mission clock binding; never silently reset suffix time to zero."""
    def __init__(self, provider, physical_offset_s):
        if isinstance(physical_offset_s,(bool,np.bool_)) or not isinstance(physical_offset_s,Real) or not math.isfinite(physical_offset_s) or physical_offset_s < 0:
            raise AdapterError('INVALID_SUFFIX_OFFSET')
        if not isinstance(provider,TemporalWRF):raise AdapterError('TEMPORAL_PROVIDER_REQUIRED')
        provider.assert_bound()
        self.provider, self.physical_offset_s = provider, float(physical_offset_s)
        self.kind = provider.kind
        self.time_mode=provider.time_mode
        self.frame_id,self.height_datum=provider.frame_id,provider.height_datum
        self.roi,self.coordinates=provider.roi,provider.coordinates
        try:local_zero_utc=(provider.epoch_utc+dt.timedelta(seconds=self.physical_offset_s)).isoformat()
        except (OverflowError,ValueError) as exc:raise AdapterError('INVALID_SUFFIX_OFFSET','UTC_RANGE') from exc
        context=dict(version='IMMUTABLE_MISSION_SUFFIX_VIEW_V4',kind=self.kind,
            parent_context_sha256=provider.context_sha256,parent_source_sha256=provider.source['source_sha256'],
            mission_epoch_utc=provider.epoch_utc.isoformat(),physical_offset_s=self.physical_offset_s,
            local_zero_utc=local_zero_utc,
            time_rule='MISSION_ELAPSED_EQUALS_PHYSICAL_OFFSET_PLUS_SUFFIX_ELAPSED',
            roi=self.roi.as_dict(),frame_id=self.frame_id,height_datum=self.height_datum,
            temporal_rule=self.time_mode)
        self.context_sha256=content(context)
        self._context=_freeze(context)
        source=provider.source
        source.update(snapshot_id='TEMPORAL_SUFFIX_COMPOSITE/'+self.context_sha256,
            source_sha256=self.context_sha256,parent_source_sha256=provider.context_sha256,
            physical_offset_s=self.physical_offset_s,local_zero_utc=context['local_zero_utc'],
            binding='IMMUTABLE_OFFSET_VIEW_OF_HASH_VERIFIED_NATIVE_TEMPORAL_CONTEXT')
        self._source=_freeze(source)
        self._trace=[]
        self._signature=self._live_signature()
        self._locked=True
        self.assert_bound()

    def _live_signature(self):
        return (id(self.provider),self.provider.context_sha256,self.physical_offset_s,
                self.kind,self.time_mode,self.frame_id,self.height_datum,id(self.roi),
                self.roi.xmin,self.roi.xmax,self.roi.ymin,self.roi.ymax,id(self.coordinates),
                self.context_sha256,id(self._context),id(self._source))

    def assert_bound(self):
        try:
            if self.__dict__.get('_locked') is not True or self._live_signature()!=self._signature:
                raise AdapterError('SUFFIX_BINDING_CHANGED')
            self.provider.assert_bound()
        except (AttributeError,TypeError,ValueError) as exc:
            if isinstance(exc,AdapterError):raise
            raise AdapterError('SUFFIX_BINDING_CHANGED',type(exc).__name__) from exc
        return True

    @property
    def context(self):
        self.assert_bound()
        return _thaw(self._context)

    @property
    def source(self):
        self.assert_bound()
        return _thaw(self._source)

    @property
    def clock(self):
        self.assert_bound()
        return _thaw(self._context)

    @property
    def trace(self):return copy.deepcopy(self._trace)

    @property
    def epoch_utc(self):return self.provider.epoch_utc

    @property
    def max_gap_s(self):return self.provider.max_gap_s

    def wind_query(self, x, y, z, suffix_elapsed_s):
        self.assert_bound()
        if isinstance(suffix_elapsed_s,(bool,np.bool_)) or not isinstance(suffix_elapsed_s,Real) or not math.isfinite(suffix_elapsed_s) or suffix_elapsed_s < 0:
            raise AdapterError('INVALID_SUFFIX_TIME')
        result=self.provider.wind_query(x, y, z, self.physical_offset_s+suffix_elapsed_s)
        result['mission_context_sha256']=result['context_sha256']
        result['context_sha256']=self.context_sha256
        result['suffix_view']=dict(context_sha256=self.context_sha256,
            physical_offset_s=self.physical_offset_s,suffix_elapsed_s=float(suffix_elapsed_s),
            mission_elapsed_s=self.physical_offset_s+float(suffix_elapsed_s))
        self._trace.append(copy.deepcopy(result))
        return result

    __call__ = wind_query
