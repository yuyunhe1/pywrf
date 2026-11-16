"""Lazy native columns; the upstream spatial interpolation is used unchanged."""
from collections import OrderedDict
from datetime import datetime, timezone
import bisect
import copy
import hashlib
import json
import math
import numpy as np

from phase3a_implementation.adapter import StaticWRFWindAdapter, AdapterError
from phase3a_implementation.coordinates import ROI, WRFCoordinates, FRAME_ID, HEIGHT_DATUM
from phase_g61.dynamic_wind.provider import _metadata
from .interval import I, D, down, up

VA = 10.
R0 = 8.513422179807296


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CachedAdapter(StaticWRFWindAdapter):
    """Cache corner arrays, never interpolated wind values or query positions."""
    def _horizontal(self, x, y):
        if not self.roi.contains(x, y):
            raise AdapterError("OUT_OF_ROI")
        i, j = self.coordinates.local_to_grid(x, y)
        nx, ny = self.coordinates.nx, self.coordinates.ny
        tol = 64*np.finfo(float).eps*max(nx, ny)
        if i < -tol or j < -tol or i > nx-1+tol or j > ny-1+tol:
            raise AdapterError("OUT_OF_HORIZONTAL_DOMAIN")
        original = [i, j]
        i, j = min(nx-1., max(0., i)), min(ny-1., max(0., j))
        i0, j0 = min(int(math.floor(i)), nx-2), min(int(math.floor(j)), ny-2)
        a, b = i-i0, j-j0
        weights = np.array([(1-a)*(1-b), a*(1-b), (1-a)*b, a*b])
        if not hasattr(self, "_corners"):
            self._corners = OrderedDict()
        key = (i0, j0)
        corners = self._corners.get(key)
        if corners is None:
            corners = {}
            for name in ("u", "v", "w", "height", "terrain", "mapfac", "east", "north"):
                field = getattr(self, name)
                values = np.stack((field[...,j0,i0], field[...,j0,i0+1],
                                   field[...,j0+1,i0], field[...,j0+1,i0+1]), axis=-1)
                # Source arrays are hash-bound and immutable for this worker.
                # Check the entire column once, including levels not selected
                # by this query, just as the upstream interpolator does.
                corners[id(field)] = (values, bool(np.isfinite(values).all()))
            self._corners[key] = corners
            if len(self._corners) > 128:
                self._corners.popitem(last=False)
        else:
            self._corners.move_to_end(key)
        def interp(field, level=None):
            values, finite = corners[id(field)]
            if not finite:
                raise AdapterError("MISSING_DATA")
            if level is not None:
                values = values[level:level+2]
            return values @ weights
        return interp, i, j, [i-original[0], j-original[1]]

    def core_query(self,x,y,z,t):
        """Identical native interpolation, omitting map-only diagnostics."""
        try:
            x,y,z,t=map(float,(x,y,z,t))
            if not all(math.isfinite(v) for v in (x,y,z,t)) or t<0:
                raise AdapterError("INVALID_INPUT")
            interp,i,j,_=self._horizontal(x,y)
            h=interp(self.height)
            terrain=float(interp(self.terrain))
            if np.any(np.diff(h)<=0):raise AdapterError("NON_MONOTONE_HEIGHT")
            if z<=terrain:raise AdapterError("BELOW_TERRAIN")
            if z<h[0]:raise AdapterError("BELOW_VERTICAL_DOMAIN")
            if z>h[-1]:raise AdapterError("ABOVE_VERTICAL_DOMAIN")
            k=min(max(int(np.searchsorted(h,z,side="right"))-1,0),len(h)-2)
            a=(z-h[k])/(h[k+1]-h[k])
            def vertical(values):
                column=interp(values,k)
                return float((1-a)*column[0]+a*column[1])
            wx,wy,wz=map(vertical,(self.u,self.v,self.w))
            if float(interp(self.mapfac))<=0:
                raise AdapterError("INVALID_MAP_FACTOR")
            return {"valid":True,"status":"OK","wx":wx,"wy":wy,"wz":wz,
                    "terrain_msl_m":terrain,"agl_m":z-terrain,
                    "source_indices":{"i":i,"j":j,"k":k,"vertical_weight":float(a)}}
        except AdapterError as exc:
            return {"valid":False,"status":exc.status,"detail":str(exc)}


class NativeWind:
    kind = "WRF_TIME_LINEAR_SNAPSHOTS"
    frame_id, height_datum = FRAME_ID, HEIGHT_DATUM

    def __init__(self, spec, departure, check=lambda: None):
        self.spec, self.epoch, self.check = spec, float(departure), check
        self.rows = spec["records"]
        self.times = [epoch(row["utc"]) for row in self.rows]
        self.offsets=[value-self.times[0] for value in self.times]
        self.epoch_offset=self.epoch-self.times[0]
        if len(self.times) < 2 or any(b <= a for a,b in zip(self.times, self.times[1:])):
            raise ValueError("WRF 原生时次不足或未严格递增")
        self.cache, self.bound_cache, self.spatial_cache = OrderedDict(), {}, OrderedDict()
        self.grid_signature = None
        self.query_count = 0
        self.roi = ROI(-1e8, 1e8, -1e8, 1e8)
        initial=max(0,min(len(self.times)-1,bisect.bisect_right(self.times,self.epoch)-1))
        first = self.adapter(initial)
        self.coordinates = first.coordinates
        a, b = self.coordinates.grid_to_local(0,0), self.coordinates.grid_to_local(self.coordinates.nx-1, self.coordinates.ny-1)
        self.roi = ROI(min(a[0],b[0]),max(a[0],b[0]),min(a[1],b[1]),max(a[1],b[1]))
        first.roi = self.roi
        first.coordinates.roi = self.roi
        self._source = {"source_sha256": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
                        "source_records": copy.deepcopy(self.rows), "frame_id": FRAME_ID,
                        "height_datum": HEIGHT_DATUM, "version": "PLATFORM_NATIVE_TEMPORAL_V1",
                        "vector_basis": "ORIGINAL_NATIVE_GRID_U_V", "interpolation": "UPSTREAM_SPATIAL_AND_LINEAR_UTC"}

    @property
    def source(self):
        return copy.deepcopy(self._source)

    def adapter(self, index):
        self.check()
        if index in self.cache:
            self.cache.move_to_end(index)
            return self.cache[index]
        row = self.rows[index]
        grid, lineage = _metadata(row["path"], row.get("record_index",0), row["expected_sha256"])
        signature = json.dumps([grid,lineage],sort_keys=True)
        if self.grid_signature is None:
            self.grid_signature = signature
        elif signature != self.grid_signature:
            raise ValueError("WRF 原生网格或起报来源不一致，禁止跨批次插值")
        value = StaticWRFWindAdapter.from_wrfout(row["path"], expected_sha256=row["expected_sha256"],
            expected_time=row["expected_time"], case_id=row["case_id"], roi=self.roi, record_index=row.get("record_index",0))
        value.__class__ = CachedAdapter
        self.cache[index] = value
        self.bound_cache[index] = (float(np.nanmax(np.abs(value.u))),float(np.nanmax(np.abs(value.v))))
        if len(self.cache) > 6:
            self.cache.popitem(last=False)
        return value

    def bracket(self, absolute):
        if absolute < self.times[0] or absolute > self.times[-1]:
            raise ValueError("WRF_TIME_OUTSIDE_COVERAGE")
        right = bisect.bisect_left(self.times, absolute)
        if self.times[right] == absolute:
            return right, right, 0.
        left = right-1
        if self.times[right]-self.times[left] > 3600.000001:
            raise ValueError("WRF_NATIVE_HOUR_GAP")
        return left, right, (absolute-self.times[left])/(self.times[right]-self.times[left])

    def relative_bracket(self,elapsed):
        # Match upstream TemporalWRF's small relative mission clock. Adding
        # every ODE stage to a ~1.8e9 UTC epoch quantizes time by ~2.4e-7s,
        # creating artificial wind discontinuities and thousands of RK steps.
        offset=self.epoch_offset+elapsed
        if offset<self.offsets[0] or offset>self.offsets[-1]:
            raise ValueError("WRF_TIME_OUTSIDE_COVERAGE")
        right=bisect.bisect_left(self.offsets,offset)
        if self.offsets[right]==offset:
            return right,right,0.
        left=right-1
        width=self.offsets[right]-self.offsets[left]
        if width>3600.000001:
            raise ValueError("WRF_NATIVE_HOUR_GAP")
        return left,right,(offset-self.offsets[left])/width

    def wind_query(self, x, y, z, t, diagnostic=False):
        self.check()
        self.query_count += 1
        try:
            if not math.isfinite(t) or t < 0:
                raise ValueError("INVALID_QUERY_TIME")
            left, right, weight = self.relative_bracket(t)
            adapter=self.adapter(left)
            a = adapter.wind_query(x,y,z,t) if diagnostic else adapter.core_query(x,y,z,t)
            if not a["valid"] or left == right:
                return a
            adapter=self.adapter(right)
            b = adapter.wind_query(x,y,z,t) if diagnostic else adapter.core_query(x,y,z,t)
            if not b["valid"]:
                return b
            result = dict(a)
            for key in ("wx","wy","wz"):
                result[key] = (1-weight)*a[key]+weight*b[key]
            if diagnostic:
                result["earth_diagnostic"] = {key:(1-weight)*a["earth_diagnostic"][key]+weight*b["earth_diagnostic"][key]
                                              for key in ("east","north","up")}
            result["source_bracket"] = [self.rows[left]["utc"],self.rows[right]["utc"],weight]
            return result
        except (ValueError,AdapterError) as exc:
            return {"valid":False,"status":str(exc)}

    def diagnostic_query(self,x,y,z,t):
        return self.wind_query(x,y,z,t,True)

    def horizontal_shear(self,x,y,z,t):
        """Same-UTC node-vector differences for display, without interval AD.

        Continuous safety admission still uses bounds(). These diagnostic
        queries repeat the original native node vertical/time interpolation;
        no interpolated wind value is cached or reused by the ODE solver.
        """
        left,right,weight=self.relative_bracket(t)
        def vectors(index):
            adapter=self.adapter(index)
            i,j=adapter.coordinates.local_to_grid(x,y)
            if not self.roi.contains(x,y) or not 0<=i<=adapter.coordinates.nx-1 or not 0<=j<=adapter.coordinates.ny-1:
                raise ValueError("OUT_OF_HORIZONTAL_DOMAIN")
            i=min(int(math.floor(i)),adapter.coordinates.nx-2)
            j=min(int(math.floor(j)),adapter.coordinates.ny-2)
            result=[]
            for jj,ii in ((j,i),(j,i+1),(j+1,i),(j+1,i+1)):
                h=adapter.height[:,jj,ii]
                if z<h[0] or z>h[-1] or z<=adapter.terrain[jj,ii]:
                    raise ValueError("NATIVE_VERTICAL_OR_TERRAIN_UNCONFIRMED")
                result.append([np.interp(z,h,field[:,jj,ii]) for field in (adapter.u,adapter.v)])
            return np.asarray(result)
        try:
            nodes=vectors(left)
            if left!=right:
                nodes=(1-weight)*nodes+weight*vectors(right)
            return max(float(np.linalg.norm(nodes[a]-nodes[b])) for a,b in ((0,1),(0,2),(1,3),(2,3)))
        except ValueError:
            return math.inf

    def shifted(self, offset):
        return OffsetWind(self, offset)

    def global_speed_bound(self,start=None,end=None):
        left=self.bracket(start)[0] if start is not None else 0
        right=self.bracket(end)[1] if end is not None else len(self.rows)-1
        indices=range(left,right+1)
        for index in indices:
            if index not in self.bound_cache:
                self.adapter(index)
        return VA+math.hypot(max(self.bound_cache[k][0] for k in indices),max(self.bound_cache[k][1] for k in indices))

    def _spatial_bounds(self, index, x, y, z):
        key = (index,x.lo,x.hi,y.lo,y.hi,float(z))
        if key in self.spatial_cache:
            self.spatial_cache.move_to_end(key)
            return self.spatial_cache[key]
        adapter = self.adapter(index)
        c = adapter.coordinates
        if not self.roi.contains(x.lo,y.lo) or not self.roi.contains(x.hi,y.hi):
            raise ValueError("OUT_OF_HORIZONTAL_DOMAIN")
        i0,j0 = c.local_to_grid(x.lo,y.lo)
        i1,j1 = c.local_to_grid(x.hi,y.hi)
        output, shear = None, 0.
        for j in range(max(0,int(math.floor(j0))),min(c.ny-2,int(math.floor(j1)))+1):
            for i in range(max(0,int(math.floor(i0))),min(c.nx-2,int(math.floor(i1)))+1):
                if not hasattr(adapter,"_checked_bound_cells"):
                    adapter._checked_bound_cells=set()
                if (i,j) not in adapter._checked_bound_cells:
                    for values in (adapter.height,adapter.u,adapter.v,adapter.w,adapter.terrain,adapter.mapfac):
                        if not np.isfinite(values[...,j:j+2,i:i+2]).all():
                            raise ValueError("NATIVE_MISSING_DATA")
                    if np.any(np.diff(adapter.height[:,j:j+2,i:i+2],axis=0)<=0) or np.any(adapter.mapfac[j:j+2,i:i+2]<=0):
                        raise ValueError("NATIVE_INVALID_HEIGHT_OR_MAP_FACTOR")
                    adapter._checked_bound_cells.add((i,j))
                a = I(max(0.,down(i0-i)),min(1.,up(i1-i)))
                b = I(max(0.,down(j0-j)),min(1.,up(j1-j)))
                sx,sy = c.m0/c.dx,c.m0/c.dy
                def field(values):
                    f00,f10,f01,f11 = [float(v) for v in (values[j,i],values[j,i+1],values[j+1,i],values[j+1,i+1])]
                    if not all(map(math.isfinite,(f00,f10,f01,f11))):
                        raise ValueError("NATIVE_MISSING_DATA")
                    # Use interval operations even at rectangle vertices so
                    # cancellation in bilinear/vertical arithmetic is enclosed.
                    vertices = [(1-I(ax))*(1-I(by))*f00+I(ax)*(1-I(by))*f10+
                                (1-I(ax))*I(by)*f01+I(ax)*I(by)*f11 for ax in (a.lo,a.hi) for by in (b.lo,b.hi)]
                    value = I(min(v.lo for v in vertices),max(v.hi for v in vertices))
                    return D(value,((1-b)*(I(f10)-f00)+b*(I(f11)-f01))*sx,
                             ((1-a)*(I(f01)-f00)+a*(I(f11)-f10))*sy)
                terrain = field(adapter.terrain)
                bottom,top = field(adapter.height[0]),field(adapter.height[-1])
                if z <= terrain.v.hi or z < bottom.v.hi or z > top.v.lo:
                    raise ValueError("NATIVE_VERTICAL_OR_TERRAIN_UNCONFIRMED")
                heights=adapter.height[:,j:j+2,i:i+2].reshape(len(adapter.height),4)
                possible=np.flatnonzero((heights[:-1].min(axis=1)<=z)&(heights[1:].max(axis=1)>=z))
                levels = []
                for k in possible:
                    h = field(adapter.height[k])
                    h_next = field(adapter.height[k+1])
                    if z < h.v.lo or z > h_next.v.hi:
                        continue
                    alpha = (D(z)-h)/(h_next-h)
                    alpha.v = alpha.v.intersect(I(0.,1.))
                    wind = []
                    for values in (adapter.u,adapter.v):
                        f0,f1=field(values[k]),field(values[k+1])
                        wind.append(f0+alpha*(f1-f0))
                    levels.append(wind)
                if not levels:
                    raise ValueError("NATIVE_VERTICAL_BRACKET_UNCONFIRMED")
                winds = [levels[0][0],levels[0][1]]
                for level in levels[1:]:
                    winds = [winds[k].union(level[k]) for k in (0,1)]
                output = winds if output is None else [output[k].union(winds[k]) for k in (0,1)]
                # Same-time adjacent mass-node vector differences at fixed MSL.
                node = []
                for jj,ii in ((j,i),(j,i+1),(j+1,i),(j+1,i+1)):
                    heights = adapter.height[:,jj,ii]
                    if z < heights[0] or z > heights[-1] or z <= adapter.terrain[jj,ii]:
                        shear = math.inf
                        break
                    node.append(np.array([np.interp(z,heights,v[:,jj,ii]) for v in (adapter.u,adapter.v)]))
                if len(node)==4:
                    shear = max(shear,*(up(float(np.linalg.norm(node[p]-node[q]))) for p,q in ((0,1),(0,2),(1,3),(2,3))))
        if output is None:
            raise ValueError("NATIVE_CELL_NOT_FOUND")
        result = (*output,shear)
        self.spatial_cache[key] = result
        if len(self.spatial_cache)>12000:
            self.spatial_cache.popitem(last=False)
        return result

    def bounds(self, x, y, z, absolute):
        if absolute.lo < self.times[0] or absolute.hi > self.times[-1]:
            raise ValueError("WRF_TIME_OUTSIDE_COVERAGE")
        output,shear = None,0.
        first = min(len(self.times)-2,max(0,bisect.bisect_right(self.times,absolute.lo)-1))
        last = min(len(self.times)-2,bisect.bisect_left(self.times,absolute.hi))
        for index in range(first,last+1):
            start,end = self.times[index:index+2]
            if end-start>3600.000001:
                raise ValueError("WRF_NATIVE_HOUR_GAP")
            lo,hi = max(start,absolute.lo),min(end,absolute.hi)
            if lo>hi:
                continue
            left,right = self._spatial_bounds(index,x,y,z),self._spatial_bounds(index+1,x,y,z)
            weight = D((I(lo,hi)-start)/(end-start),dt=1/(end-start))
            values = [left[k]+weight*(right[k]-left[k]) for k in (0,1)]
            output = values if output is None else [output[k].union(values[k]) for k in (0,1)]
            def shear_at(alpha):
                if alpha<=0:return left[2]
                if alpha>=1:return right[2]
                if not math.isfinite(left[2]) or not math.isfinite(right[2]):return math.inf
                return ((1-I(alpha))*left[2]+I(alpha)*right[2]).hi
            shear=max(shear,shear_at((lo-start)/(end-start)),shear_at((hi-start)/(end-start)))
        if output is None:
            raise ValueError("WRF_NATIVE_HOUR_GAP")
        return (*output,shear)


class OffsetWind:
    def __init__(self, original, offset):
        self.original,self.offset = original,float(offset)
    def __getattr__(self, key):
        return getattr(self.original,key)
    def wind_query(self,x,y,z,t):
        return self.original.wind_query(x,y,z,t+self.offset)
    def horizontal_shear(self,x,y,z,t):
        return self.original.horizontal_shear(x,y,z,t+self.offset)


class FrozenWind:
    """Single-UTC visualization only; normal planning never uses this view."""
    def __init__(self, original):
        self.original=original
    def __getattr__(self,key):
        return getattr(self.original,key)
    def wind_query(self,x,y,z,t):
        return self.original.wind_query(x,y,z,0.)
    def horizontal_shear(self,x,y,z,t):
        return self.original.horizontal_shear(x,y,z,0.)
    def bounds(self,x,y,z,absolute):
        wx,wy,shear=self.original.bounds(x,y,z,I(self.epoch))
        return D(wx.v,wx.dx,wx.dy,0.),D(wy.v,wy.dx,wy.dy,0.),shear


def heading_to_local(coordinates, lon, heading_deg):
    convergence = coordinates.n*(math.radians(lon)-coordinates.lon0)
    return math.pi/2-math.radians(heading_deg)+convergence


def heading_to_true(coordinates, lon, psi):
    convergence = coordinates.n*(math.radians(lon)-coordinates.lon0)
    result=math.degrees(math.pi/2-psi+convergence)%360
    return 0. if abs(result-360.)<1e-12 or abs(result)<1e-12 else result
