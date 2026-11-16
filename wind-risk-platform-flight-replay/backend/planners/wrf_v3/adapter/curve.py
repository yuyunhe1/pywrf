"""Persist continuous DOP853 polynomials, independently of display thinning."""
import math
import copy
from itertools import accumulate
from pathlib import Path
import numpy as np
from numpy.polynomial import Polynomial
from scipy.integrate import solve_ivp
from .interval import I, polynomial
from .native import VA, R0
from .validation import ENDPOINT_TOLERANCE_M


def partition_native_controls(route, curve, provider):
    """Split unchanged ZOH controls at native-cell and UTC-hour boundaries.

    Native interpolation has derivative changes at these boundaries. Keeping
    them inside a long absolute-position RK step can exceed the original
    micrometre switch-node threshold. Splitting introduces no new controls or
    tolerance changes; stored nodes come from the independent dense curve.
    """
    c = provider.coordinates
    origin = np.asarray(c.grid_to_local(0, 0))
    steps = (float(c.grid_to_local(1, 0)[0] - origin[0]), float(c.grid_to_local(0, 1)[1] - origin[1]))
    cuts = [0., curve.end, *(stamp - provider.epoch for stamp in provider.times if 0 < stamp - provider.epoch < curve.end)]
    for index, block in enumerate(curve.coefficients):
        left, right = curve.breaks[index:index + 2]
        for axis, step in enumerate(steps):
            values = (block[axis] - np.r_[origin[axis], np.zeros(7)]) / step
            lo = float(values[0] + np.minimum(values[1:], 0).sum())
            hi = float(values[0] + np.maximum(values[1:], 0).sum())
            for boundary in range(math.ceil(lo), math.floor(hi) + 1):
                polynomial = values.copy()
                polynomial[0] -= boundary
                for root in np.polynomial.polynomial.polyroots(polynomial):
                    if abs(root.imag) <= 1e-8 and 1e-10 < root.real < 1 - 1e-10:
                        cuts.append(float(left + root.real * (right - left)))
    # The vertically interpolated native column also changes derivative when
    # the selected MSL level crosses a model mass level inside a grid cell.
    # Inspect both hourly snapshots; their vertical indices can differ.
    def signature(t):
        point = curve.point(t)
        a, b, _ = provider.relative_bracket(t)
        result = []
        for index in set((a, b)):
            q = provider.adapter(index).core_query(float(point[0]), float(point[1]), curve.z, t)
            if not q.get("valid"):
                raise ValueError("原生分层节点缺少风场：" + str(q.get("status")))
            node = q["source_indices"]
            result.append((index, math.floor(node["i"]), math.floor(node["j"]), node["k"]))
        return tuple(result)
    for left, right in zip(curve.breaks, curve.breaks[1:]):
        provider.check()
        points = np.linspace(float(left) + min(1e-7, (right-left)/100), float(right) - min(1e-7, (right-left)/100), 3)
        signatures = [signature(t) for t in points]
        for a, b, sa, sb in zip(points, points[1:], signatures, signatures[1:]):
            if sa == sb:
                continue
            while b - a > 1e-7:
                middle = (a + b) / 2
                if signature(middle) == sa:
                    a = middle
                else:
                    b = middle
            cuts.append(float((a + b) / 2))
    native = np.unique(cuts)
    controls, durations, stamps = [], [], [0.]
    start = 0.
    for control, duration in zip(route["controls"], route["durations"]):
        end = start + duration
        inside = native[(native > start + 1e-8) & (native < end - 1e-8)]
        previous = start
        for stop in [*inside.tolist(), end]:
            if stop > previous:
                controls.append(control)
                durations.append(stop - previous)
                stamps.append(stop)
            previous = stop
        start = end
    result = copy.deepcopy(route)
    result.update(controls=controls, durations=durations, states=[curve.point(t).tolist() for t in stamps],
                  platform_partition="NATIVE_CELL_AND_UTC_BOUNDARIES_ZOH_UNCHANGED",
                  original_control_segments=len(route["controls"]))
    return result


class Curve:
    def __init__(self, breaks, coefficients, altitude):
        self.breaks,self.coefficients,self.z = np.asarray(breaks),np.asarray(coefficients),float(altitude)
        if not len(self.coefficients) or len(self.breaks)!=len(self.coefficients)+1 or np.any(np.diff(self.breaks)<=0):
            raise ValueError("INVALID_CONTINUOUS_CURVE")
        self.end=float(self.breaks[-1])
        self._bounds={}

    @classmethod
    def from_route(cls, route, provider):
        state=np.asarray(route["state_start"],float)
        start=0.
        breaks=[0.]
        coefficients=[]
        for control,duration in zip(route["controls"],route["durations"]):
            if duration<=0 or start+duration<=start:
                continue
            origin=state.copy()
            def rhs(t,q):
                xy=origin[:2]+R0*q[:2]
                psi=origin[2]+q[2]
                w=provider.wind_query(float(xy[0]),float(xy[1]),route["z_layer_msl_m"],max(0.,float(t)))
                if not w.get("valid") or math.hypot(w["wx"],w["wy"])>=VA:
                    raise ValueError("CURVE_WIND_QUERY_FAILED:"+str(w.get("status")))
                return [(VA*math.cos(psi)+w["wx"])/R0,(VA*math.sin(psi)+w["wy"])/R0,control]
            # Integrate displacements, rather than ~100 km absolute positions.
            # This keeps the same strict scaled tolerances meaningful for long
            # flights and for a small displacement along either coordinate.
            sol=solve_ivp(rhs,(start,start+duration),np.zeros(3),method="DOP853",rtol=1e-12,
                          atol=[1e-13,1e-13,1e-13],max_step=min(5.,.15/max(abs(control),1e-12)),dense_output=True)
            if not sol.success:
                raise ValueError("CURVE_INTEGRATION_FAILED")
            for interpolant in sol.sol.interpolants:
                if interpolant.t<=breaks[-1]:
                    continue
                # Convert SciPy's nested polynomial to powers of normalized
                # reference time. No pickles or executable serialized objects.
                block=np.zeros((3,8))
                for component in range(3):
                    p=Polynomial([0.])
                    if hasattr(interpolant,"F"):
                        for i,f in enumerate(interpolant.F[::-1,component]):
                            p=(p+float(f))*Polynomial([0.,1.] if i%2==0 else [1.,-1.])
                        p+=float(interpolant.y_old[component])
                    else:
                        p+=float(interpolant(interpolant.t)[component])
                    if component<2:
                        p*=R0
                    p+=float(origin[component])
                    block[component,:len(p.coef)]=p.coef
                coefficients.append(block)
                breaks.append(float(interpolant.t))
            state=origin+sol.y[:,-1]*[R0,R0,1.]
            start+=duration
        goal=np.asarray(route["state_goal"])
        if np.linalg.norm(state[:2]-goal[:2])>ENDPOINT_TOLERANCE_M or abs(state[2]-goal[2])>1e-7:
            raise ValueError("CONTINUOUS_CURVE_ENDPOINT_REJECT")
        return cls(breaks,coefficients,route["z_layer_msl_m"])

    def save(self,path,context=""):
        np.savez_compressed(path,breaks=self.breaks,coefficients=self.coefficients,altitude_msl_m=self.z,context=context)

    @classmethod
    def load(cls,path):
        with np.load(path,allow_pickle=False) as values:
            return cls(values["breaks"],values["coefficients"],values["altitude_msl_m"])

    def point(self,t,derivative=0):
        index=min(len(self.coefficients)-1,max(0,int(np.searchsorted(self.breaks,t,side="right")-1)))
        h=self.breaks[index+1]-self.breaks[index]
        x=(min(self.end,max(0.,float(t)))-self.breaks[index])/h
        values=self.coefficients[index]
        if derivative:
            values=np.array([np.polynomial.polynomial.polyder(v,derivative) for v in values])/h**derivative
        return np.array([np.polynomial.polynomial.polyval(x,v) for v in values])

    def bounds(self,index,a=None,b=None):
        left,right=self.breaks[index:index+2]
        a=left if a is None else a
        b=right if b is None else b
        key=(index,a,b)
        if key in self._bounds:
            return self._bounds[key]
        h=right-left
        x=(I(a,b)-float(left))/float(h)
        c=self.coefficients[index]
        values=[polynomial(v,x) for v in c[:2]]
        first=[polynomial(np.polynomial.polynomial.polyder(v),x)/float(h) for v in c[:2]]
        second=[polynomial(np.polynomial.polynomial.polyder(v,2),x)/float(h*h) for v in c[:2]]
        result=(*values,*first,*second)
        self._bounds[key]=result
        return result

    def samples(self,max_step=1.):
        # Retain every polynomial boundary as well as regular samples so short
        # turns and self intersections keep their ordered trajectory identity.
        return np.unique(np.r_[self.breaks,np.arange(0.,self.end,max_step),self.end])
