"""Exact fixed-height row pruning, with native times sharing one point matvec.

The certified corner bounds only exclude levels which cannot bracket the
registered height anywhere in that cell. Every possible bracket is retained.
No position, time, wind value, or ODE result survives between queries.
"""
from types import MappingProxyType
import numpy as np
from phase_g61.wind_column_diagnostic_v1.paired_columns import PairedColumnsTemporal
from phase_g61.online_execution_repair_v1.velocity_provider import immutable
from phase3a_implementation.adapter import AdapterError

POLICY=dict(version='CURRENT_POINT_TWO_NATIVE_PRUNED_HEIGHT_ROWS_V3_FROZEN_ALTITUDE',fixed_altitude_m=240.,
    excluded_levels='STRICT_ALL_CORNER_HEIGHT_BOUND_WITH_64_EPS_PADDING',
    retained_levels='ALL_POTENTIAL_BRACKETS_INCLUSIVE_OR_FULL_FALLBACK',
    terrain_check='ORIGINAL_ONE_DIMENSIONAL_DOT_PRODUCT_AND_STATUS_ORDER',
    row_arithmetic='ORIGINAL_FOUR_CORNER_DOT_AND_SCALAR_VERTICAL_BLEND',
    matrix='ALL_NATIVE_RELEVANT_HEIGHT_U_V_W_ROWS_ONE_CURRENT_POINT_PRODUCT',
    wind_or_position_cache=False,source_and_domain_guards='UNCHANGED')

class PrunedColumnsTemporal(PairedColumnsTemporal):
    @classmethod
    def from_records(cls,*args,**kwargs):
        obj=super().from_records(*args,**kwargs)
        packets={};z=240.
        object.__setattr__(obj,'_pruned_altitude_m',z)
        keys=set(obj._velocity_packs[0])
        if any(set(p)!=keys for p in obj._velocity_packs):
            raise AdapterError('PRUNED_NATIVE_CELL_SET_MISMATCH')
        for key in keys:
            blocks=[];meta=[];start=0
            for index,pack in enumerate(obj._velocity_packs):
                columns,terrain,mapfac,finite=pack[key]
                monotone,positive_map=obj._cell_convex_proofs[index][key]
                if not finite or not monotone:
                    break
                h=columns[0]
                pad=64*np.finfo(float).eps*max(1.,abs(z),float(np.max(np.abs(h))))
                below=np.flatnonzero(np.max(h,axis=1)<z-pad)
                above=np.flatnonzero(np.min(h,axis=1)>z+pad)
                lo=int(below[-1]) if len(below) else 0
                hi=int(above[0]) if len(above) else len(h)-1
                if hi<=lo:
                    lo,hi=0,len(h)-1
                size=hi-lo+1
                block=np.concatenate([columns[n][lo:hi+1] for n in (0,1,2,3)])
                meta.append((start,size,lo,hi,len(h),positive_map))
                blocks.append(block);start+=len(block)
            if len(meta)!=len(obj._velocity_packs):
                packets[key]=None
            else:
                packets[key]=(immutable(np.concatenate(blocks)),tuple(meta))
        object.__setattr__(obj,'_pruned_packets',MappingProxyType(packets))
        object.__setattr__(obj,'_pruned_packet_id',id(obj._pruned_packets))
        obj.assert_bound()
        return obj

    def assert_bound(self):
        super().assert_bound()
        if '_pruned_packets' in self.__dict__ and (id(self._pruned_packets)!=self._pruned_packet_id or self._pruned_altitude_m!=240.):
            raise AdapterError('PRUNED_COLUMN_MATERIAL_CHANGED')
        return True

    def geometry(self,x,y):
        geom=super().geometry(x,y)
        packet=self._pruned_packets.get(geom[2:4])
        return (*geom,None if packet is None else packet[0]@geom[4])

    def native_geometry(self,index,z,geom):
        original_geom=geom[:5];values=geom[5]
        if z!=self._pruned_altitude_m or values is None:
            return super().native_geometry(index,z,original_geom)
        x,y,i,j,weights=original_geom
        columns,terrain_c,mapfac_c,finite=self._velocity_packs[index][(i,j)]
        start,size,lo,hi,full_levels,positive_map=self._pruned_packets[(i,j)][1][index]
        heights=values[start:start+size]
        terrain=float(terrain_c@weights)
        if z<=terrain:
            raise AdapterError('BELOW_TERRAIN')
        if lo==0 and z<heights[0]:
            raise AdapterError('BELOW_VERTICAL_DOMAIN')
        if hi==full_levels-1 and z>heights[-1]:
            raise AdapterError('ABOVE_VERTICAL_DOMAIN')
        k=min(max(int(np.searchsorted(heights,z,side='right'))-1,0),size-2)
        weight=(z-heights[k])/(heights[k+1]-heights[k])
        u=start+size+k;v=start+2*size+k;w=start+3*size+k
        winds=(float((1-weight)*values[u]+weight*values[u+1]),
            float((1-weight)*values[v]+weight*values[v+1]),
            float((1-weight)*values[w]+weight*values[w+1]))
        if not positive_map and float(mapfac_c@weights)<=0:
            raise AdapterError('INVALID_MAP_FACTOR')
        return winds

    def query_stats(self):
        out=super().query_stats();out['pruned_columns_policy']=POLICY
        out['prepared_pruned_height_ranges']={str(key):[list(m[1:5]) for m in packet[1]]
            for key,packet in self._pruned_packets.items() if packet is not None}
        return out
