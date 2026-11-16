"""Independent dense ODE replay and conservative geometric interval checks."""
import math
import numpy as np
from scipy.integrate import solve_ivp
from phase3a_implementation.domain import VA, R0
from phase3a_implementation.coordinates import ROI


class EdgeCollisionChecker:
    def __init__(self, roi, mask, *, provider, speed_bound, radius_m=2., ode_margin_m=R0*1e-6):
        if not isinstance(roi,ROI) or not callable(getattr(mask,'distance_lower_bound',None)):
            raise ValueError('Explicit ROI and a conservative-distance obstacle mask required')
        if not math.isfinite(radius_m) or radius_m<0 or not math.isfinite(ode_margin_m) or ode_margin_m<=0:
            raise ValueError('Finite nonnegative footprint and positive ODE margin required')
        if not math.isfinite(speed_bound['max_ground_speed_mps']) or speed_bound['max_ground_speed_mps']<=0:
            raise ValueError('Conservative physical speed bound required')
        if speed_bound['source_sha256']!=provider.source['source_sha256']:
            raise ValueError('Speed bound source mismatch')
        self.roi,self.mask,self.provider = roi,mask,provider
        self.speed_bound = speed_bound
        self.radius,self.margin = float(radius_m),float(ode_margin_m)

    def clearance(self,q):
        x,y=float(q[0]),float(q[1])
        roi_distance=min(x-self.roi.xmin,self.roi.xmax-x,y-self.roi.ymin,self.roi.ymax-y)
        obstacle_distance=float(self.mask.distance_lower_bound(x,y))
        if math.isnan(obstacle_distance) or obstacle_distance<0:
            raise ValueError('Invalid mask lower bound')
        return min(roi_distance,obstacle_distance)-self.radius

    def state_valid(self,node):
        return self.clearance(node.q())>self.margin

    def check(self,trajectory):
        result=dict(valid=False,status=None,method='INDEPENDENT_DOP853_DENSE_SPEED_TUBE',
                    assurance='NUMERICAL_CONSERVATIVE_TUBE_NOT_RIGOROUS_ODE_ERROR_CERTIFICATE',
                    radius_m=self.radius,ode_margin_m=self.margin,speed_bound=self.speed_bound,
                    mask_source=self.mask.source,query_count=0,interval_count=0,
                    sampled_min_clearance_m=None,distance_m=None,final_state=None)
        state=np.r_[trajectory['state_start'],0.]
        t0=0.;minimum=math.inf
        class Stop(Exception): pass
        def sample(q):
            nonlocal minimum
            result['query_count']+=1
            clearance=self.clearance(q);minimum=min(minimum,clearance)
            if clearance<=self.margin:
                raise Stop('COLLISION_OR_ROI_FOOTPRINT')
            return clearance
        def interval(sol,a,b,depth=0):
            result['interval_count']+=1
            mid=(a+b)/2
            clear=sample(sol(mid))
            radius=self.speed_bound['max_ground_speed_mps']*(b-a)/2+self.margin
            if clear>radius:
                return
            if depth>=16 or b-a<=1e-5:
                raise Stop('UNRESOLVED_COLLISION_INTERVAL')
            interval(sol,a,mid,depth+1);interval(sol,mid,b,depth+1)
        try:
            sample(state)
            for u,duration in zip(trajectory['controls'],trajectory['durations']):
                d=float(duration)
                if d>0:
                    def rhs(t,q):
                        s=self.provider.wind_query(float(q[0]),float(q[1]),trajectory['z_layer_msl_m'],float(t))
                        if not s.get('valid',False): raise Stop(s.get('status','WIND_QUERY_FAILED'))
                        wx,wy=float(s['wx']),float(s['wy'])
                        if not all(math.isfinite(v) for v in (wx,wy)): raise Stop('NONFINITE_WIND')
                        if math.hypot(wx,wy)/VA>=1: raise Stop('CONNECTOR_OUT_OF_DOMAIN')
                        vx,vy=VA*math.cos(q[2])+wx,VA*math.sin(q[2])+wy
                        return [vx,vy,float(u),math.hypot(vx,vy)]
                    sol=solve_ivp(rhs,(t0,t0+d),state,method='DOP853',dense_output=True,
                                  rtol=1e-11,atol=[R0*1e-12,R0*1e-12,1e-12,R0*1e-12],max_step=.1)
                    if not sol.success: raise Stop('COLLISION_REPLAY_INTEGRATION_FAILURE')
                    sample(sol.y[:,-1])
                    for a,b in zip(sol.t[:-1],sol.t[1:]): interval(sol.sol,float(a),float(b))
                    state=sol.y[:,-1]
                t0+=d
            result.update(valid=True,status='CLEAR_NUMERICAL_TUBE',distance_m=float(state[3]),final_state=state[:3].tolist())
        except Stop as exc:
            result['status']=str(exc)
        result['sampled_min_clearance_m']=minimum if math.isfinite(minimum) else None
        return result
