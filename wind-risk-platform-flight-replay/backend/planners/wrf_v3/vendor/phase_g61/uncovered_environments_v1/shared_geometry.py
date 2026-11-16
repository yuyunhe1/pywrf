"""Share exact point geometry between native times; prove invariant checks once.

No wind values or positions persist across queries. Original native matrix
arithmetic and interpolation order remain unchanged. Height monotonicity and
positive map factor are skipped only with a full-corner convexity proof.
"""
import bisect,math
from types import MappingProxyType
import numpy as np
from phase_g61.online_execution_repair_v1.guarded_velocity import GuardedTemporal as Base,GuardedSuffix
from phase3a_implementation.adapter import AdapterError

POLICY=dict(version='EXACT_SHARED_GEOMETRY_AND_CELL_CONVEX_PROOFS_V1',query_geometry='ONCE_PER_QUERY_NOT_ACROSS_QUERIES',
    heights='ALL_ORIGINAL_LEVEL_DOT_PRODUCTS_RETAINED',wind_columns='ALL_ORIGINAL_LEVEL_DOT_PRODUCTS_RETAINED',
    monotonicity='ALL_CORNER_LEVEL_GAPS_STRICTLY_POSITIVE_IMPLIES_EVERY_CONVEX_COLUMN_STRICTLY_INCREASING',
    map_factor='ALL_FINITE_CORNERS_STRICTLY_POSITIVE_IMPLIES_INTERPOLATED_FACTOR_POSITIVE',
    unsafe_or_nonfinite_cells='ORIGINAL_FULL_STATUS_EVALUATION_FALLBACK',wind_value_cache='NONE')

class SharedGeometryTemporal(Base):
    @classmethod
    def from_records(cls,*args,**kwargs):
        obj=super().from_records(*args,**kwargs)
        grids=[(a.coordinates.nx,a.coordinates.ny,a.coordinates.dx,a.coordinates.dy,a.coordinates.m0,a.coordinates.summary()) for a in obj._adapters]
        if any(g!=grids[0] for g in grids[1:]):raise AdapterError('SHARED_GEOMETRY_NATIVE_GRID_MISMATCH')
        proofs=[]
        for pack in obj._velocity_packs:
            p={}
            for key,(columns,terrain,mapfac,finite) in pack.items():
                p[key]=(bool(finite and np.all(np.diff(columns[0],axis=0)>0)),bool(finite and np.all(mapfac>0)))
            proofs.append(MappingProxyType(p))
        object.__setattr__(obj,'_cell_convex_proofs',tuple(proofs));object.__setattr__(obj,'_cell_convex_proof_ids',tuple(id(p) for p in proofs));obj.assert_bound();return obj
    def assert_bound(self):
        super().assert_bound()
        if '_cell_convex_proofs' in self.__dict__ and tuple(id(p) for p in self._cell_convex_proofs)!=self._cell_convex_proof_ids:raise AdapterError('SHARED_GEOMETRY_PROOFS_CHANGED')
        return True
    def geometry(self,x,y):
        c=self._adapters[0].coordinates
        if not self.roi.contains(x,y):raise AdapterError('OUT_OF_ROI')
        i,j=c.local_to_grid(x,y);tol=64*np.finfo(float).eps*max(c.nx,c.ny)
        if i < -tol or j < -tol or i > c.nx-1+tol or j > c.ny-1+tol:raise AdapterError('OUT_OF_HORIZONTAL_DOMAIN')
        i,j=min(c.nx-1.,max(0.,i)),min(c.ny-1.,max(0.,j));i0,j0=min(int(math.floor(i)),c.nx-2),min(int(math.floor(j)),c.ny-2)
        a,b=i-i0,j-j0;weights=np.array([(1-a)*(1-b),a*(1-b),(1-a)*b,a*b])
        return x,y,i0,j0,weights
    def native_geometry(self,index,z,g):
        x,y,i0,j0,weights=g;cell=self._velocity_packs[index].get((i0,j0))
        if cell is None:raise AdapterError('VELOCITY_MATERIAL_CELL_NOT_PREPARED')
        columns,terrain_c,mapfac_c,finite=cell
        if not finite:return super().native(index,x,y,z)
        monotone,positive_map=self._cell_convex_proofs[index][(i0,j0)]
        h=columns[0]@weights;terrain=float(terrain_c@weights)
        if not monotone and np.any(np.diff(h)<=0):raise AdapterError('NON_MONOTONE_HEIGHT')
        if z<=terrain:raise AdapterError('BELOW_TERRAIN')
        if z<h[0]:raise AdapterError('BELOW_VERTICAL_DOMAIN')
        if z>h[-1]:raise AdapterError('ABOVE_VERTICAL_DOMAIN')
        k=min(max(int(np.searchsorted(h,z,side='right'))-1,0),len(h)-2);weight=(z-h[k])/(h[k+1]-h[k])
        def vertical(packed):
            col=packed@weights
            return float((1-weight)*col[k]+weight*col[k+1])
        winds=tuple(vertical(columns[n]) for n in [1,2,3])
        if not positive_map and float(mapfac_c@weights)<=0:raise AdapterError('INVALID_MAP_FACTOR')
        return winds
    def wind_bound(self,x,y,z,t):
        if self._evaluation_state.depth<=0:raise AdapterError('EVALUATION_GUARD_REQUIRED')
        if self._live_signature()!=self._signature or tuple(id(p) for p in self._velocity_packs)!=self._velocity_pack_identity:raise AdapterError('TEMPORAL_BINDING_CHANGED')
        self._velocity_stats['queries']+=1;result=dict(valid=False,status=None,wx=None,wy=None,wz=None,frame_id=self.frame_id,height_datum=self.height_datum)
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
            geom=self.geometry(x,y);values=[(self.native_geometry(i,z,geom),weight) for i,weight in pairs]
            result.update(wx=math.fsum(v[0]*w for v,w in values),wy=math.fsum(v[1]*w for v,w in values),wz=math.fsum(v[2]*w for v,w in values),valid=True,status='OK')
        except AdapterError as exc:result['status']=exc.status
        except (TypeError,ValueError,OverflowError):result['status']='INVALID_INPUT'
        self._velocity_stats[result['status']]+=1
        if result['valid'] and math.hypot(result['wx'],result['wy'])>=10.:self._velocity_stats['CONNECTOR_OUT_OF_DOMAIN']+=1
        return result
    def query_stats(self):
        out=super().query_stats();out['shared_geometry_policy']=POLICY;return out
