"""Continuous local shooting connector; no global spatial optimality claim."""
import copy
import math
import numpy as np
from scipy.optimize import least_squares, minimize
from phase3a_implementation.coordinates import FRAME_ID, HEIGHT_DATUM
from phase3a_implementation.domain import VA, R0
from phase3a_implementation.synthetic import ConstantWindProvider
from phase3b.integrator import SpatialIntegrationError
import phase3b.connector as inherited_backend


def failed(status, **extra):
    return {'status':status,'valid':False,'family':None,'time':None,'trajectory':None,
            'validation':None,'validation_status':'NOT_VALIDATED',**extra}


class SpatialWindConnector:
    """Start/goal=(x,y,psi_air), explicit fixed MSL layer, one frozen provider.

    Optional constant_result reuses audited frozen A output for reproducibility;
    all boundaries and start wind are checked and every candidate is C replayed.
    It is explicit seed input, not an alternative dynamics backend.
    """
    def __init__(self, snapshot, *, z_layer_msl_m, optimize=True):
        self.provider = snapshot
        self.z = float(z_layer_msl_m)
        if not math.isfinite(self.z): raise ValueError('Explicit finite layer required')
        self.optimize = bool(optimize)

    def _legacy_connect(self, start, goal, parameters=None, *, constant_result=None):
        parameters = {'Va':VA,'R0':R0} if parameters is None else parameters
        if parameters != {'Va':VA,'R0':R0}: return failed('INVALID_INPUT',detail='Frozen parameters mismatch')
        if getattr(self.provider,'frame_id',None)!=FRAME_ID: return failed('FRAME_MISMATCH')
        if getattr(self.provider,'height_datum',None)!=HEIGHT_DATUM: return failed('HEIGHT_DATUM_MISMATCH')
        if getattr(self.provider,'kind',None) not in ('SYNTHETIC_CONSTANT','SYNTHETIC_LINEAR_GRADIENT','WRF_STATIC_SNAPSHOT','WRF_TIME_LINEAR_SNAPSHOTS'):
            return failed('UNSUPPORTED_PROVIDER',detail='Only explicit static providers supported')
        try:
            def state(q):
                if isinstance(q,dict):
                    if 'z' in q and float(q['z'])!=self.z: raise ValueError('UNSUPPORTED_ALTITUDE_CHANGE')
                    return np.array([q[k] for k in ('x','y','psi_air')],float)
                return np.asarray(q,float)
            q0,qf=state(start),state(goal)
            if q0.shape!=(3,) or qf.shape!=(3,) or not np.isfinite(np.r_[q0,qf]).all():
                raise ValueError('Finite 2D air-heading states required')
        except (KeyError,ValueError,TypeError,OverflowError) as exc:
            return failed('INVALID_INPUT',detail=str(exc))
        precheck=[]
        for label,q in (('start',q0),('goal',qf)):
            sample=self.provider.wind_query(float(q[0]),float(q[1]),self.z,0.)
            if not sample.get('valid',False): return failed('WIND_QUERY_FAILED',detail=sample.get('status'),precheck=precheck)
            try:
                w=[float(sample[k]) for k in ('wx','wy','wz')]
                if not all(math.isfinite(v) for v in w): raise ValueError('NONFINITE_WIND')
            except (KeyError,ValueError,TypeError) as exc:
                return failed('WIND_QUERY_FAILED',detail=str(exc),precheck=precheck)
            beta=math.hypot(*w[:2])/VA
            precheck.append({'location':label,'wind':w,'beta':beta,'sample':sample})
            if beta>=1.: return failed('CONNECTOR_OUT_OF_DOMAIN',precheck=precheck,solver_started=False)
        wind=precheck[0]['wind'][:2]
        provenance={'provider_source':copy.deepcopy(self.provider.source),'z_layer_msl_m':self.z,
                    'frame_id':FRAME_ID,'height_datum':HEIGHT_DATUM,'wind_mode':('DYNAMIC_UTC_LINEAR' if self.provider.kind=='WRF_TIME_LINEAR_SNAPSHOTS' else 'STATIC_SPATIAL'),
                    'Va':VA,'R0':R0,'global_optimality_guaranteed':False,
                    'numerical_completeness':'UNRESOLVED','seed_source':'EXPLICIT_FROZEN_A' if constant_result is not None else 'READ_ONLY_PHASE2B_A'}
        if constant_result is None:
            from phase2b4.solver_a.general_wind_connector import GeneralWindConnector
            constant_result=GeneralWindConnector().connect(q0,qf,wind,parameters)
        if not isinstance(constant_result,dict) or not isinstance(constant_result.get('candidates',[]),list):
            return failed('INVALID_SEED_INPUT',precheck=precheck,provenance=provenance)
        pool=constant_result.get('candidates',[])
        self._ordered_seed_pool=pool
        if not pool: return failed('SEED_GENERATION_FAILED',precheck=precheck,provenance=provenance)
        # Check supplied seeds against boundaries and start-sampled constant wind.
        seed_provider=ConstantWindProvider(*wind,0.,roi=self.provider.roi)
        attempts=[]
        self._attempts=attempts
        accepted=[]
        for index,seed in enumerate(pool):
            if not isinstance(seed,dict):
                attempts.append({'seed_index':index,'status':'MALFORMED_SEED'});continue
            original=copy.deepcopy(seed.get('trajectory'))
            if not isinstance(original,dict):
                attempts.append({'seed_index':index,'status':'MALFORMED_SEED'});continue
            if original.get('state_start')!=q0.tolist() or original.get('state_goal')!=qf.tolist():
                attempts.append({'seed_index':index,'status':'SEED_BOUNDARY_MISMATCH'});continue
            original['z_layer_msl_m']=self.z
            try:
                # Preserve Phase2B's reported float, checked against durations by C.
                initial_time=float(seed['time']) if 'time' in seed else math.fsum(original['durations'])
                seed_c=validate(original,original['controls'],initial_time,seed_provider)
            except (KeyError,TypeError,ValueError,OverflowError):
                attempts.append({'seed_index':index,'status':'MALFORMED_SEED'});continue
            if not seed_c['valid']:
                attempts.append({'seed_index':index,'status':'SEED_C_FAILED','seed_C':seed_c});continue
            replay=None
            if type(self.provider) is ConstantWindProvider or initial_time==0.:
                replay=validate(original,original['controls'],initial_time,self.provider)
            else:
                record_skip(index,original,initial_time)
            attempt={'seed_index':index,'seed_family':seed.get('family'),'initial_time':initial_time,
                     'initial_spatial_replay':replay,'stages':[]}
            if replay is None:
                attempt['initial_spatial_replay_status']='NOT_RUN_DIAGNOSTIC_ONLY'
            attempts.append(attempt)
            if type(self.provider) is ConstantWindProvider or initial_time==0.:
                if replay['valid']:
                    stage='CONSTANT_REDUCTION' if type(self.provider) is ConstantWindProvider else 'ZERO_TIME_DEGENERACY'
                    candidate=self._candidate(original,seed.get('family'),stage,replay,index)
                    candidate['time']=initial_time
                    accepted.append(candidate)
                    attempt['status']='CONSTANT_REGRESSION_VALID'
                else: attempt['status']='CONSTANT_REGRESSION_FAILED'
                continue
            target=qf.copy()
            target[2]=original['states'][-1][2]  # Preserve seed winding, not modulo root discontinuities.
            controls=np.asarray(original['controls'],float)
            durations=np.asarray(original['durations'],float)
            horizon=max(20.,4*initial_time+10.)
            scale=R0/VA
            variable_control=len(durations)<3

            def decode_root(v):
                ds=np.asarray(v[:len(durations)])*scale
                us=np.asarray(v[len(durations):])*(VA/R0) if variable_control else controls
                return us,ds

            def residual(us,ds):
                endpoint,_=integrate(q0,us,ds,self.provider,self.z)
                return (endpoint-target)/[R0,R0,1.]

            v0=durations/scale
            lower=np.zeros(len(durations));upper=np.full(len(durations),horizon/scale)
            if variable_control:
                v0=np.r_[v0,controls/(VA/R0)]
                lower=np.r_[lower,-np.ones(len(controls))];upper=np.r_[upper,np.ones(len(controls))]
            try:
                recovered=least_squares(lambda v:residual(*decode_root(v)),v0,bounds=(lower,upper),
                                        ftol=1e-12,xtol=1e-12,gtol=1e-12,max_nfev=100)
                us,ds=decode_root(recovered.x)
                trajectory=self._trajectory(q0,qf,us,ds)
                check=self._validation_for_candidate(trajectory,math.fsum(ds),index,'ENDPOINT_RECOVERY')
                attempt['stages'].append({'method':'CONTINUOUS_ENDPOINT_RECOVERY','optimizer_success':bool(recovered.success),
                                          'nfev':recovered.nfev,'scaled_residual':residual(us,ds).tolist(),'C':check})
                if check['valid']:
                    accepted.append(self._candidate(trajectory,'SPATIAL_ZOH','ENDPOINT_RECOVERY',check,index))
                if not check['valid'] or not self.optimize:
                    attempt['status']='RECOVERED' if check['valid'] else 'RECOVERY_C_FAILED';continue
                # Bounded, nonuniform ZOH mesh. Stage durations and every cell's
                # control are continuous variables; no spatial family/grid claim.
                pieces=np.array([min(4,max(1,math.ceil(d/3.))) for d in ds],int)
                mesh_us=np.repeat(us,pieces)
                group=np.repeat(np.arange(len(ds)),pieces)
                n=len(ds)

                def decode(v):
                    return v[n:]*(VA/R0),(v[:n]*scale)[group]/pieces[group]

                opt0=np.r_[ds/scale,mesh_us/(VA/R0)]
                bounds=[(0.,horizon/scale)]*n+[(-1.,1.)]*len(mesh_us)
                optimized=minimize(lambda v:float(np.sum(v[:n])),opt0,method='SLSQP',bounds=bounds,
                                   constraints={'type':'eq','fun':lambda v:residual(*decode(v))},
                                   options={'ftol':1e-11,'maxiter':60,'eps':1e-7})
                us2,ds2=decode(optimized.x)
                trajectory2=self._trajectory(q0,qf,us2,ds2)
                check2=validate(trajectory2,trajectory2['controls'],math.fsum(ds2),self.provider)
                attempt['stages'].append({'method':'LOCAL_TIME_OPTIMIZATION','optimizer_success':bool(optimized.success),
                                          'optimizer_status':int(optimized.status),'message':str(optimized.message),
                                          'nit':int(optimized.nit),'nfev':int(optimized.nfev),'mesh_cells':len(us2),
                                          'C':check2,'optimization_status':'CONVERGED' if optimized.success else 'NUMERICAL_LIMIT'})
                if check2['valid']:
                    accepted.append(self._candidate(trajectory2,'SPATIAL_ZOH','LOCAL_TIME_OPTIMIZATION',check2,index))
                attempt['status']='REFINED_VALID' if check2['valid'] else 'OPTIMIZATION_C_FAILED_RECOVERY_RETAINED'
            except SpatialIntegrationError as exc:
                attempt['status']='TRIAL_DOMAIN_OR_INTEGRATION_FAILURE'
                attempt['detail']=str(exc)
            except (ValueError,RuntimeError,np.linalg.LinAlgError) as exc:
                attempt['status']='NUMERICAL_FAILURE';attempt['detail']=str(exc)
        if not accepted:
            return failed('NUMERICAL_LIMIT',precheck=precheck,attempts=attempts,provenance=provenance,
                          seed_count=len(pool),candidate_count=0,solver_started=True)
        accepted.sort(key=lambda c:c['time'])
        best=accepted[0]
        return {**copy.deepcopy(best),'status':'SUCCESS','seed_count':len(pool),'candidate_count':len(accepted),
                'candidates':accepted,'attempts':attempts,'precheck':precheck,'provenance':provenance,
                'selection':'MINIMUM_TIME_C_VALID_ENUMERATED_LOCAL_CANDIDATE','solver_started':True}

    def _trajectory(self,q0,qf,controls,durations):
        _,nodes=integrate(q0,controls,durations,self.provider,self.z)
        return {'type':'piecewise_constant','control_interpolation':'ZOH','state_start':q0.tolist(),
                'state_goal':qf.tolist(),'parameters':{'Va':VA,'R0':R0},'z_layer_msl_m':self.z,
                'controls':np.asarray(controls).tolist(),'durations':np.asarray(durations).tolist(),
                'states':nodes,'frame_id':FRAME_ID,'height_datum':HEIGHT_DATUM}

    def _validation_for_candidate(self,trajectory,total,index,stage):
        return validate(trajectory,trajectory['controls'],total,self.provider)

    @staticmethod
    def _candidate(trajectory,family,stage,validation,index):
        return inherited_backend.SpatialWindConnector._candidate(trajectory,family,stage,validation,index)

    def connect(self,start,goal,parameters=None,*,constant_result=None):
        if self.provider.kind=='SYNTHETIC_CONSTANT':return self._legacy_connect(start,goal,parameters,constant_result=constant_result)
        from phase_g61.uncovered_environments_v1.warm_recovery import connect as recovery
        import sys
        return recovery(self,start,goal,parameters,constant_result,sys.modules[__name__])

    __call__ = connect

# Forward to the original runtime hook locations. Existing numerical accuracy,
# integration memo, endpoint wind capture, deadline and seed logging still apply.
def integrate(*args,**kwargs):
    from phase_g61.uncovered_environments_v1.wind_remainder import integrate as propagation
    return propagation(*args,**kwargs)
def validate(*args,**kwargs):return inherited_backend.validate(*args,**kwargs)
def least_squares(*args,**kwargs):return inherited_backend.least_squares(*args,**kwargs)
def minimize(*args,**kwargs):return inherited_backend.minimize(*args,**kwargs)

POLICY=dict(version='SKIP_DIAGNOSTIC_INITIAL_SPATIAL_C_V1',
    branch='NONCONSTANT_POSITIVE_TIME_ONLY',
    constant_and_zero_time_replay='UNCHANGED_REQUIRED',
    initial_spatial_replay='NOT_RUN_DIAGNOSTIC_ONLY',
    seed_C='UNCHANGED',recovered_C='UNCHANGED_FULL',worker_C='UNCHANGED_FULL',
    domain='ORIGINAL_FOR_ALL_EXECUTED_QUERIES_NO_UNUSED_SEED_PATH_PROBE')
def record_skip(index,tr,total):
    import time
    from .trace import record
    from phase4a_design.mock_contract import content_hash
    record('initial_spatial_C_not_run',time.perf_counter(),seed_index=index,
        trajectory_sha256=content_hash(tr),total_s=float(total),status='NOT_RUN_DIAGNOSTIC_ONLY')
