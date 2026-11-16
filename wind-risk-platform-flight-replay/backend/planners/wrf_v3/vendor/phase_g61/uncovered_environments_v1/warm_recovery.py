"""One deterministic reference-suffix model before the existing bounded pool."""
import math
import numpy as np
from .reference_model import ReferenceModel
from .work import content
from phase_g61.online_execution_repair_v1.heading_model import FamilyReject
from phase_g61.online_execution_repair_v1.two_segment_recovery import connect as inherited
from phase3a_implementation.domain import R0
from phase3b.integrator import SpatialIntegrationError

def trial(engine,start,goal,backend,index):
    q0,qf=np.asarray(start,float),np.asarray(goal,float);attempt=dict(kind='REFERENCE_SUFFIX_SINGLE_SHOOTING',index=index,
        source_route_sha256=content(engine.reference_seed_route),offset_s=engine.reference_seed_offset,start_actual=q0.tolist(),goal=qf.tolist(),status='STARTED',stages=[])
    engine._reference_warm_attempts=[attempt]
    try:
        model=ReferenceModel(engine.reference_seed_route,engine.reference_seed_offset,q0[2],qf[2],engine.remaining_physical_s)
        attempt.update(model=model.kind,initial=model.x0.tolist(),dimension=model.n,original_unexecuted_segments=len(model.u),horizon_s=model.H)
        def residual(x):
            us,ds=model.decode(x);endpoint,_=backend.integrate(q0,us,ds,engine.provider,engine.z);return (endpoint[:2]-qf[:2])/R0
        result=backend.least_squares(residual,model.x0,bounds=(model.lower,model.upper),ftol=1e-12,xtol=1e-12,gtol=1e-12,max_nfev=100)
        us,ds=model.decode(result.x);attempt.update(nfev=int(result.nfev),optimizer_success=bool(result.success),scaled_XY=result.fun.tolist())
        if np.any(ds<=0):attempt['status']='ZERO_DURATION_NOT_ADMITTED';return
        tr=engine._trajectory(q0,qf,us,ds);total=math.fsum(ds);c=engine._validation_for_candidate(tr,total,index,'ENDPOINT_RECOVERY')
        attempt['stages'].append(dict(C=c,controls=us.tolist(),durations=ds.tolist()));attempt['status']='C_PASS_PENDING_COLLISION_AND_SELECTION' if c['valid'] else 'C_REJECT'
        if c['valid']:engine._candidate(tr,'REFERENCE_SEEDED_NEW_ROUTE','ENDPOINT_RECOVERY',c,index)
    except FamilyReject as exc:attempt.update(status='REFERENCE_MODEL_REJECT',reason=str(exc))
    except SpatialIntegrationError as exc:attempt.update(status='DOMAIN_OR_INTEGRATION_REJECT',reason=str(exc))
    except (ValueError,RuntimeError,np.linalg.LinAlgError) as exc:attempt.update(status='NUMERICAL_REJECT',reason=str(exc))

def connect(engine,start,goal,parameters,constant_result,backend):
    if getattr(engine,'reference_seed_route',None) is not None:
        if constant_result is None:
            from phase2b4.solver_a.general_wind_connector import GeneralWindConnector
            w=engine.provider.wind_query(float(start[0]),float(start[1]),engine.z,0.)
            if not w.get('valid'):return backend.failed('WIND_QUERY_FAILED',detail=w.get('status'))
            constant_result=GeneralWindConnector().connect(start,goal,[w['wx'],w['wy']],parameters)
        engine._ordered_seed_pool=constant_result.get('candidates',[])
        trial(engine,start,goal,backend,len(engine._ordered_seed_pool)+1)
    return inherited(engine,start,goal,parameters,constant_result,backend)
