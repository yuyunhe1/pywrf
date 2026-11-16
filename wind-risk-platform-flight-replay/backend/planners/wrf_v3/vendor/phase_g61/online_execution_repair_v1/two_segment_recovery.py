"""Ordered, fixed-lift shooting with an exact ZOH heading parameterization."""
import copy,math
import numpy as np
from .rewound_heading_model import RewoundHeadingModel as HeadingModel,FamilyReject
from phase3a_implementation.domain import VA,R0
from phase3a_implementation.synthetic import ConstantWindProvider
from phase3b.integrator import SpatialIntegrationError

def connect(engine,start,goal,parameters,constant_result,backend):
    q0,qf=np.asarray(start,float),np.asarray(goal,float)
    if q0.shape!=(3,) or qf.shape!=(3,) or not np.isfinite(np.r_[q0,qf]).all():return backend.failed('INVALID_INPUT')
    if parameters is not None and parameters!={'Va':VA,'R0':R0}:return backend.failed('INVALID_INPUT')
    checks=[]
    for name,q in [('start',q0),('goal',qf)]:
        sample=engine.provider.wind_query(float(q[0]),float(q[1]),engine.z,0.)
        checks.append(dict(location=name,sample=sample))
        if not sample['valid']:return backend.failed('WIND_QUERY_FAILED',precheck=checks,detail=sample['status'])
        if math.hypot(sample['wx'],sample['wy'])>=VA:return backend.failed('CONNECTOR_OUT_OF_DOMAIN',precheck=checks)
    w=[checks[0]['sample'][k] for k in ['wx','wy']]
    if constant_result is None:
        from phase2b4.solver_a.general_wind_connector import GeneralWindConnector
        constant_result=GeneralWindConnector().connect(q0,qf,w,{'Va':VA,'R0':R0})
    pool=constant_result.get('candidates',[]);engine._ordered_seed_pool=pool;attempts=[];engine._attempts=attempts;seen=set();accepted=[]
    straight_hint=next((float(c['trajectory']['durations'][1]) for c in pool if len(c.get('trajectory',{}).get('controls',[]))==3 and c['trajectory']['controls'][1]==0.),None)
    seed_provider=ConstantWindProvider(*w,0.,roi=engine.provider.roi)
    if getattr(engine,'handover_control',None)==0. and straight_hint is not None:
        from .two_segment_model import TwoSegmentModel
        index=len(pool);attempt=dict(seed_index=index,seed_family='REGISTERED_TWO_SEGMENT_CONTROL_MODEL',stages=[],initial_spatial_replay_status='NOT_RUN_NOT_AN_OLD_SEED');attempts.append(attempt)
        try:
            model=TwoSegmentModel(q0[2],qf[2],engine.remaining_physical_s,straight_hint)
            attempt['heading_model']=dict(kind=model.kind,dimension=model.n,required_lift=float(qf[2]),horizon_s=model.H,initial=model.x0.tolist(),first_duration_hint=straight_hint)
            def residual_two(x):
                us,ds=model.decode(x);endpoint,_=backend.integrate(q0,us,ds,engine.provider,engine.z)
                return (endpoint[:2]-qf[:2])/R0
            recovered=backend.least_squares(residual_two,model.x0,bounds=(model.lower,model.upper),ftol=1e-12,xtol=1e-12,gtol=1e-12,max_nfev=100)
            us,ds=model.decode(recovered.x)
            if np.any(ds<=0.):attempt['status']='ZERO_DURATION_DEGENERACY_NOT_ADMITTED'
            else:
                trajectory=engine._trajectory(q0,qf,us,ds);total=math.fsum(ds);check=engine._validation_for_candidate(trajectory,total,index,'ENDPOINT_RECOVERY')
                attempt['stages'].append(dict(method='TWO_SEGMENT_BOUNDED_CONTROL_RECOVERY',optimizer_success=bool(recovered.success),nfev=int(recovered.nfev),scaled_XY_residual=recovered.fun.tolist(),C=check,heading_model=model.kind,root_dimension=model.n))
                if check['valid']:engine._candidate(trajectory,'TWO_SEGMENT_BOUNDED_ZOH','ENDPOINT_RECOVERY',check,index)
                attempt['status']='NO_FULLY_ADMITTED_TWO_SEGMENT_CANDIDATE'
        except FamilyReject as exc:attempt.update(status='TWO_SEGMENT_MODEL_REJECT',reason=str(exc))
        except SpatialIntegrationError as exc:attempt.update(status='TRIAL_DOMAIN_OR_INTEGRATION_FAILURE',detail=str(exc))
        except (ValueError,RuntimeError,np.linalg.LinAlgError) as exc:attempt.update(status='NUMERICAL_FAILURE',detail=str(exc))
    for index,seed in enumerate(pool):
        original=copy.deepcopy(seed.get('trajectory'));attempt=dict(seed_index=index,seed_family=seed.get('family'),stages=[],initial_spatial_replay_status='NOT_RUN_DIAGNOSTIC_ONLY');attempts.append(attempt)
        if not isinstance(original,dict) or original.get('state_start')!=q0.tolist() or original.get('state_goal')!=qf.tolist():attempt['status']='SEED_BOUNDARY_MISMATCH';continue
        original['z_layer_msl_m']=engine.z;total=float(seed.get('time',math.fsum(original['durations'])));attempt['initial_time']=total
        seed_C=backend.validate(original,original['controls'],total,seed_provider)
        if not seed_C['valid']:attempt.update(status='SEED_C_FAILED',seed_C=seed_C);continue
        try:model=HeadingModel(original['controls'],original['durations'],q0[2],qf[2],engine.remaining_physical_s,straight_hint=straight_hint)
        except FamilyReject as exc:attempt.update(status='HEADING_MODEL_REJECT',reason=str(exc),required_unwrapped_heading=float(qf[2]-q0[2]),original_controls=original['controls']);continue
        attempt['heading_model']=dict(kind=model.kind,dimension=model.n,required_lift=float(qf[2]),horizon_s=model.H,initial=model.x0.tolist(),initialization_certificate=model.initialization_certificate)
        key=model.key()
        if key in seen:attempt.update(status='EXACT_DUPLICATE_MODEL_SKIPPED');continue
        seen.add(key)
        def residual(x):
            us,ds=model.decode(x);endpoint,_=backend.integrate(q0,us,ds,engine.provider,engine.z)
            return (endpoint[:2]-qf[:2])/R0
        try:
            recovered=backend.least_squares(residual,model.x0,bounds=(model.lower,model.upper),ftol=1e-12,xtol=1e-12,gtol=1e-12,max_nfev=100)
            us,ds=model.decode(recovered.x)
            if np.any(ds<=0.):attempt.update(status='ZERO_DURATION_DEGENERACY_NOT_ADMITTED',durations=ds.tolist());continue
            trajectory=engine._trajectory(q0,qf,us,ds);total=math.fsum(ds)
            check=engine._validation_for_candidate(trajectory,total,index,'ENDPOINT_RECOVERY')
            attempt['stages'].append(dict(method='EXACT_LIFT_ELIMINATION_ENDPOINT_RECOVERY',optimizer_success=bool(recovered.success),nfev=int(recovered.nfev),scaled_XY_residual=recovered.fun.tolist(),C=check,heading_model=model.kind,root_dimension=model.n))
            if check['valid']:accepted.append(engine._candidate(trajectory,'SPATIAL_ZOH_EXACT_LIFT','ENDPOINT_RECOVERY',check,index))
            attempt['status']='RECOVERED' if check['valid'] else 'RECOVERY_C_FAILED'
        except SpatialIntegrationError as exc:attempt.update(status='TRIAL_DOMAIN_OR_INTEGRATION_FAILURE',detail=str(exc))
        except (ValueError,RuntimeError,np.linalg.LinAlgError) as exc:attempt.update(status='NUMERICAL_FAILURE',detail=str(exc))
    if not accepted:return backend.failed('NUMERICAL_LIMIT',attempts=attempts,seed_count=len(pool),candidate_count=0,solver_started=True)
    accepted.sort(key=lambda c:c['time']);return dict(accepted[0],status='SUCCESS',candidates=accepted,attempts=attempts,seed_count=len(pool),candidate_count=len(accepted))
