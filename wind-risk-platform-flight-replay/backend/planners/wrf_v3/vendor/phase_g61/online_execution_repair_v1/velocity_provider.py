"""Exact current-position native arithmetic, without per-RHS research receipts.

Only immutable interpolation stencils are prepared. No position, wind value,
ODE, endpoint, or time interpolation result is cached or held between queries.
"""
import bisect,collections,math
from types import MappingProxyType
from numbers import Real
import numpy as np
from phase_g61.dynamic_wind.provider import TemporalWRF as OriginalTemporal,MissionSuffixView as OriginalSuffix
from phase3a_implementation.adapter import AdapterError

POLICY=dict(version='SOURCE_BOUND_NATIVE_STENCIL_VELOCITY_V1',weather_model='ORIGINAL_SPATIAL_THEN_EXACT_UTC_LINEAR',
    preparation='IMMUTABLE_PER_NATIVE_SOURCE_PER_ROI_CELL_FULL_COLUMNS',wind_value_cache='NONE',
    query_frequency='EVERY_ORIGINAL_RHS_AND_VALIDATOR_EVALUATION',
    observation='BOUNDED_STATUS_COUNTERS_AND_TRANSACTION_C_RECEIPTS_NO_FULL_PER_RHS_RESEARCH_METADATA',
    original_C_collision_and_ordered_numerics='UNCHANGED',source_binding='ORIGINAL_IMMUTABLE_SIGNATURE_ON_EVERY_QUERY')

FIELDS=('height','u','v','w','east','north')
def immutable(a):return np.frombuffer(np.ascontiguousarray(a).tobytes(),dtype=a.dtype).reshape(a.shape)

def prepare(adapter,roi):
    c=adapter.coordinates
    ij=[c.local_to_grid(x,y) for x in [roi.xmin,roi.xmax] for y in [roi.ymin,roi.ymax]]
    lo_i=max(0,min(c.nx-2,math.floor(min(v[0] for v in ij))));hi_i=max(0,min(c.nx-2,math.floor(max(v[0] for v in ij))))
    lo_j=max(0,min(c.ny-2,math.floor(min(v[1] for v in ij))));hi_j=max(0,min(c.ny-2,math.floor(max(v[1] for v in ij))))
    cells={}
    for j in range(lo_j,hi_j+1):
        for i in range(lo_i,hi_i+1):
            def corners(field):return np.stack((field[...,j,i],field[...,j,i+1],field[...,j+1,i],field[...,j+1,i+1]),axis=-1)
            columns=tuple(immutable(corners(getattr(adapter,name))) for name in FIELDS)
            terrain=immutable(corners(adapter.terrain));mapfac=immutable(corners(adapter.mapfac))
            # If any diagnostic source column is nonfinite, use original status
            # evaluation order. A finite vector cannot bypass a missing field.
            finite=all(np.isfinite(v).all() for v in (*columns,terrain,mapfac))
            cells[(i,j)]=(columns,terrain,mapfac,finite)
    return MappingProxyType(cells)


class VelocityTemporal(OriginalTemporal):
    @classmethod
    def from_records(cls,*args,**kwargs):
        obj=super().from_records(*args,**kwargs)
        packs=tuple(prepare(a,obj.roi) for a in obj._adapters)
        object.__setattr__(obj,'_velocity_packs',packs)
        object.__setattr__(obj,'_velocity_pack_identity',tuple(id(p) for p in packs))
        object.__setattr__(obj,'_velocity_stats',collections.Counter())
        object.__setattr__(obj,'_velocity_stats_source',obj.context_sha256)
        obj.assert_bound();return obj

    def assert_bound(self):
        super().assert_bound()
        if '_velocity_packs' in self.__dict__ and (tuple(id(p) for p in self._velocity_packs)!=self._velocity_pack_identity or self._velocity_stats_source!=self.context_sha256):
            raise AdapterError('VELOCITY_MATERIAL_BINDING_CHANGED')
        return True

    def native(self,index,x,y,z):
        a=self._adapters[index];c=a.coordinates
        if not self.roi.contains(x,y):raise AdapterError('OUT_OF_ROI')
        i,j=c.local_to_grid(x,y);tol=64*np.finfo(float).eps*max(c.nx,c.ny)
        if i < -tol or j < -tol or i > c.nx-1+tol or j > c.ny-1+tol:raise AdapterError('OUT_OF_HORIZONTAL_DOMAIN')
        i,j=min(c.nx-1.,max(0.,i)),min(c.ny-1.,max(0.,j))
        i0,j0=min(int(math.floor(i)),c.nx-2),min(int(math.floor(j)),c.ny-2)
        aa,bb=i-i0,j-j0;weights=np.array([(1-aa)*(1-bb),aa*(1-bb),(1-aa)*bb,aa*bb])
        cell=self._velocity_packs[index].get((i0,j0))
        if cell is None:raise AdapterError('VELOCITY_MATERIAL_CELL_NOT_PREPARED')
        columns,terrain_c,mapfac_c,finite=cell
        if not finite:
            sample=a.wind_query(x,y,z,0.)
            if not sample['valid']:raise AdapterError(sample['status'])
            return sample['wx'],sample['wy'],sample['wz']
        h=columns[0]@weights;terrain=float(terrain_c@weights)
        if np.any(np.diff(h)<=0):raise AdapterError('NON_MONOTONE_HEIGHT')
        if z<=terrain:raise AdapterError('BELOW_TERRAIN')
        if z<h[0]:raise AdapterError('BELOW_VERTICAL_DOMAIN')
        if z>h[-1]:raise AdapterError('ABOVE_VERTICAL_DOMAIN')
        k=min(max(int(np.searchsorted(h,z,side='right'))-1,0),len(h)-2);w=(z-h[k])/(h[k+1]-h[k])
        def vertical(packed):
            col=packed@weights
            return float((1-w)*col[k]+w*col[k+1])
        winds=tuple(vertical(columns[n]) for n in [1,2,3])
        if float(mapfac_c@weights)<=0:raise AdapterError('INVALID_MAP_FACTOR')
        return winds

    def wind_query(self,x,y,z,t):
        self.assert_bound();self._velocity_stats['queries']+=1
        result=dict(valid=False,status=None,wx=None,wy=None,wz=None,frame_id=self.frame_id,height_datum=self.height_datum)
        try:
            if any(isinstance(v,(bool,np.bool_)) for v in (x,y,z,t)):raise AdapterError('INVALID_INPUT')
            x,y,z,t=map(float,(x,y,z,t))
            if not all(math.isfinite(v) for v in (x,y,z,t)) or t<0:raise AdapterError('INVALID_INPUT')
            q=self._epoch_offset+t
            if q<self._offsets[0] or q>self._offsets[-1]:raise AdapterError('OUT_OF_TIME_DOMAIN')
            j=bisect.bisect_left(self._offsets,q)
            if q==self._offsets[j]:pairs=[(j,1.)]
            else:
                i=j-1;gap=self._offsets[j]-self._offsets[i]
                if gap>self.max_gap_s:raise AdapterError('TIME_GAP_EXCEEDS_CONTRACT')
                weight=(q-self._offsets[i])/gap;pairs=[(i,1.-weight),(j,weight)]
            values=[(self.native(i,x,y,z),weight) for i,weight in pairs]
            result.update(wx=math.fsum(v[0]*w for v,w in values),wy=math.fsum(v[1]*w for v,w in values),wz=math.fsum(v[2]*w for v,w in values),valid=True,status='OK')
        except AdapterError as exc:result['status']=exc.status
        except (TypeError,ValueError,OverflowError):result['status']='INVALID_INPUT'
        self._velocity_stats[result['status']]+=1
        if result['valid'] and math.hypot(result['wx'],result['wy'])>=10.:self._velocity_stats['CONNECTOR_OUT_OF_DOMAIN']+=1
        return result

    __call__=wind_query
    @property
    def trace(self):return []
    def query_stats(self):
        self.assert_bound()
        return dict(policy=POLICY,context_sha256=self.context_sha256,counts=dict(self._velocity_stats),full_per_RHS_records='NOT_COLLECTED_FLIGHT_ROLE',prepared_native_sources=len(self._velocity_packs),prepared_cell_counts=[len(v) for v in self._velocity_packs])


class VelocitySuffix(OriginalSuffix):
    def wind_query(self,x,y,z,t):
        self.assert_bound()
        if isinstance(t,(bool,np.bool_)) or not isinstance(t,Real) or not math.isfinite(t) or t<0:raise AdapterError('INVALID_SUFFIX_TIME')
        return self.provider.wind_query(x,y,z,self.physical_offset_s+float(t))
    __call__=wind_query
    @property
    def trace(self):return []
