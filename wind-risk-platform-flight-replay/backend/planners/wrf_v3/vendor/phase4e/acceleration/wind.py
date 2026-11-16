"""Exact-coordinate static memoization, with the frozen interpolation arithmetic."""
from collections import OrderedDict,Counter
import math
import struct
import numpy as np
from phase3a_implementation.adapter import AdapterError,StaticWRFWindAdapter
from phase4a_design.mock_contract import content_hash

POLICY=dict(query_capacity=8192,column_capacity=2048,stencil_capacity=32,
    key='IEEE754_FLOAT64_EXACT_X_Y_Z_NO_ROUNDING_TIME_VALIDATED_THEN_STATIC',
    interpolation='ORIGINAL_CORNERS_AT_WEIGHTS_ORIGINAL_VERTICAL_LINEAR_ORDER',
    cache_scope='ONE_HASH_VERIFIED_IMMUTABLE_SNAPSHOT_INSTANCE')

def bits(*values):return struct.pack('!'+'d'*len(values),*values)

def clone_result(value):
    """Copy every mutable container in the frozen adapter's output schema."""
    out=value.copy()
    for key in ('source_indices','earth_diagnostic'):
        if isinstance(value.get(key),dict):out[key]=value[key].copy()
    for key in ('roundoff_grid_adjustment','vertical_domain_msl_m'):
        if isinstance(value.get(key),list):out[key]=value[key].copy()
    return out

class LRU:
    def __init__(self,capacity):
        if isinstance(capacity,bool) or not isinstance(capacity,int) or capacity<1:raise ValueError('Positive integer cache capacity')
        self.capacity=capacity;self.data=OrderedDict();self.hits=0;self.misses=0;self.evictions=0
    def get(self,key):
        try:value=self.data.pop(key)
        except KeyError:self.misses+=1;return None
        self.hits+=1;self.data[key]=value;return value
    def put(self,key,value):
        self.data.pop(key,None);self.data[key]=value
        if len(self.data)>self.capacity:self.data.popitem(last=False);self.evictions+=1
    def summary(self):return dict(hits=self.hits,misses=self.misses,evictions=self.evictions,
        size=len(self.data),capacity=self.capacity)

class ExactCachedWRFWindProvider:
    """Returns the entire original query dictionary, never rounded coordinates.

    Corners are cached only from read-only source arrays. Each field still uses
    the same shape/order corners@weights as the reference, including full-column
    missing checks and the original early-failure order. Cache values are private.
    """
    def __init__(self,reference,*,query_capacity=8192,column_capacity=2048,stencil_capacity=32):
        if not isinstance(reference,StaticWRFWindAdapter):raise TypeError('A frozen WRF adapter is required')
        self.reference=reference
        self.query_cache=LRU(query_capacity);self.column_cache=LRU(column_capacity);self.stencil_cache=LRU(stencil_capacity)
        self.calls=0;self._source=content_hash(reference.source)
        self.fields=dict(height=reference.height,terrain=reference.terrain,u=reference.u,v=reference.v,
            w=reference.w,mapfac=reference.mapfac,east=reference.east,north=reference.north)
        if any(a.flags.writeable for a in self.fields.values()):raise ValueError('Immutable source arrays required')
    def __getattr__(self,key):return getattr(self.reference,key)
    def assert_same_snapshot(self):
        if content_hash(self.reference.source)!=self._source or any(a.flags.writeable for a in self.fields.values()):
            raise ValueError('Frozen cache source changed')
        for key,a in self.fields.items():
            if a is not getattr(self.reference,key):raise ValueError('Cached source array replaced')
    def _column_entry(self,x,y):
        # ROI must be checked even when the coordinate stencil was memoized.
        if not self.roi.contains(x,y):raise AdapterError('OUT_OF_ROI')
        key=bits(x,y);cached=self.column_cache.get(key)
        if cached is not None:return cached
        i,j=self.coordinates.local_to_grid(x,y);nx,ny=self.coordinates.nx,self.coordinates.ny
        tol=64*np.finfo(float).eps*max(nx,ny)
        if i < -tol or j < -tol or i > nx-1+tol or j > ny-1+tol:raise AdapterError('OUT_OF_HORIZONTAL_DOMAIN')
        original=[i,j];i,j=min(nx-1.,max(0.,i)),min(ny-1.,max(0.,j))
        i0,j0=min(int(math.floor(i)),nx-2),min(int(math.floor(j)),ny-2)
        a,b=i-i0,j-j0
        weights=np.array([(1-a)*(1-b),a*(1-b),(1-a)*b,a*b])
        cell_key=(i0,j0);cell=self.stencil_cache.get(cell_key)
        if cell is None:cell={};self.stencil_cache.put(cell_key,cell)
        entry=dict(i=i,j=j,adjust=[i-original[0],j-original[1]],weights=weights,
            cell=cell,i0=i0,j0=j0,columns={})
        self.column_cache.put(key,entry);return entry
    def _interp(self,entry,name):
        if name in entry['columns']:return entry['columns'][name]
        cell=entry['cell']
        if name not in cell:
            field=self.fields[name];i0,j0=entry['i0'],entry['j0']
            corners=np.stack((field[...,j0,i0],field[...,j0,i0+1],field[...,j0+1,i0],field[...,j0+1,i0+1]),axis=-1)
            corners.setflags(write=False);cell[name]=(corners,bool(np.isfinite(corners).all()))
        corners,finite=cell[name]
        if not finite:raise AdapterError('MISSING_DATA')
        column=corners@entry['weights'];entry['columns'][name]=column;return column
    def wind_query(self,x,y,z,t):
        self.calls+=1
        try:
            x,y,z,t=map(float,(x,y,z,t))
            if not all(math.isfinite(v) for v in (x,y,z,t)) or t<0:raise AdapterError('INVALID_INPUT')
            key=bits(x,y,z);hit=self.query_cache.get(key)
            if hit is not None:return clone_result(hit)
            result=self.reference._base()
            entry=self._column_entry(x,y);h=self._interp(entry,'height');terrain=float(self._interp(entry,'terrain'))
            if np.any(np.diff(h)<=0):raise AdapterError('NON_MONOTONE_HEIGHT')
            if z<=terrain:raise AdapterError('BELOW_TERRAIN')
            if z<h[0]:raise AdapterError('BELOW_VERTICAL_DOMAIN')
            if z>h[-1]:raise AdapterError('ABOVE_VERTICAL_DOMAIN')
            k=min(max(int(np.searchsorted(h,z,side='right'))-1,0),len(h)-2);a=(z-h[k])/(h[k+1]-h[k])
            def vertical(name):
                column=self._interp(entry,name);return float((1-a)*column[k]+a*column[k+1])
            wx,wy,wz=[vertical(name) for name in ('u','v','w')]
            mapfac=float(self._interp(entry,'mapfac'))
            if mapfac<=0:raise AdapterError('INVALID_MAP_FACTOR')
            result.update(wx=wx,wy=wy,wz=wz,valid=True,status='OK',
                source_indices={'i':entry['i'],'j':entry['j'],'k':k,'vertical_weight':float(a)},
                roundoff_grid_adjustment=list(entry['adjust']),terrain_msl_m=terrain,agl_m=z-terrain,
                vertical_domain_msl_m=[float(h[0]),float(h[-1])],map_factor=mapfac,
                earth_diagnostic={'east':vertical('east'),'north':vertical('north'),'up':wz})
            self.query_cache.put(key,clone_result(result))
        except AdapterError as exc:
            result=self.reference._base();result.update(status=exc.status,detail=str(exc))
        except (TypeError,ValueError,OverflowError) as exc:
            result=self.reference._base();result.update(status='INVALID_INPUT',detail=str(exc))
        return result
    def wind_query_batch(self,points):
        """Ordered exact scalar evaluation; no changed vector arithmetic or ODE."""
        return [self.wind_query(*point) for point in points]
    def stats(self):
        self.assert_same_snapshot()
        return dict(calls=self.calls,query=self.query_cache.summary(),columns=self.column_cache.summary(),
            stencils=self.stencil_cache.summary(),source_identity_sha256=self._source,policy=POLICY)
    __call__=wind_query
