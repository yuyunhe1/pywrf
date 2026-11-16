"""Bounded local single-shooting time objective with a fully validated incumbent.

This creates a new initial route before its immutable freeze. It never retimes
an executed/reference route or changes physical/acceptance parameters.
"""
import copy,math,time
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from scipy.optimize import minimize
from phase3a_implementation.domain import VA,R0
from phase3b.validator import THRESHOLDS
from phase3b.integrator import SpatialIntegrationError
from .engine.runtime.common import Clock,Limit,Stop,put,content
from .engine.runtime.environment import Context

POLICY=dict(version='BOUNDED_INITIAL_TIME_SINGLE_SHOOTING_V4',local_admission_s=3.,maxiter=12,ftol=1e-11,eps=1e-7,
    minimum_candidate_gain_s=1e-4,minimum_phase_s=1e-6,minimum_nonzero_normalized_control=1e-8,
    numerical_control_and_duration_lower_bounds_not_physical_relaxations=True,
    optimizer='SLSQP_FINITE_DIFFERENCES_NO_AD_NO_EXPLICIT_JACOBIAN',segments='EXISTING_FIXED_COUNT_AND_SIGNS_EXACT_ZERO_PRESERVED',
    every_candidate_requires_original_C_collision_and_raw_lift=True,late_candidate_cannot_replace_incumbent=True,
    original_worker20s_root120s_and_prepare8s_unchanged=True,local_record_overhead_measured_not_hidden=True)

class ModelDomainError(ValueError):pass

class Model:
    def __init__(self,route,fraction):
        if isinstance(fraction,bool) or fraction not in (.9,1.) or route.get('parameters')!={'Va':VA,'R0':R0}:
            raise ModelDomainError('TIME_MODEL_PHYSICAL_IDENTITY')
        for key in ['state_start','state_goal']:
            if len(route[key])!=3 or any(isinstance(v,bool) for v in route[key]) or not np.isfinite(np.asarray(route[key],float)).all():
                raise ModelDomainError('TIME_MODEL_BOUNDARY_SCHEMA')
        self.route=copy.deepcopy(route);self.us=np.asarray(route['controls'],float);self.ds=np.asarray(route['durations'],float)
        if (self.us.ndim!=1 or self.us.shape!=self.ds.shape or not len(self.ds) or
            any(isinstance(x,bool) for x in route['controls']+route['durations']) or
            not np.isfinite(np.r_[self.us,self.ds]).all() or np.any(self.ds<=0)):
            raise ModelDomainError('TIME_MODEL_INPUT_SCHEMA')
        self.bound=float(fraction)*(VA/R0);self.total=math.fsum(self.ds);self.n=len(self.ds)
        if not 0<self.bound<=VA/R0 or np.max(np.abs(self.us))>self.bound+1e-12:raise ModelDomainError('TIME_MODEL_CONTROL_BOUND')
        self.nonzero=np.flatnonzero(self.us!=0.);self.sign=np.sign(self.us[self.nonzero])
        self.x0=np.r_[self.ds/self.total,np.minimum(1.,np.abs(self.us[self.nonzero])/self.bound)]
        self.lower=np.r_[np.full(self.n,POLICY['minimum_phase_s']/self.total),np.full(len(self.nonzero),POLICY['minimum_nonzero_normalized_control'])]
        self.upper=np.ones(len(self.x0));self.bounds=list(zip(self.lower,self.upper))
        if np.any(self.x0<self.lower) or np.any(self.x0>self.upper):raise ModelDomainError('TIME_MODEL_INITIAL_NUMERICAL_DOMAIN')
    def decode(self,x):
        a=np.asarray(x,float)
        if a.shape!=self.x0.shape or not np.isfinite(a).all():raise ModelDomainError('TIME_MODEL_VARIABLE_DOMAIN')
        # SLSQP may probe outside its own box before its clipped objective call.
        # Project optimizer variables only; never any state/goal/trajectory node.
        a=np.minimum(self.upper,np.maximum(self.lower,a))
        us=np.zeros(self.n);us[self.nonzero]=self.sign*self.bound*a[self.n:]
        return us,self.total*a[:self.n]
    def objective(self,x):
        _,ds=self.decode(x)
        return math.fsum(ds)/self.total

def refine(incumbent,ctx,req,base,*,minimize_fn=None):
    begin=time.perf_counter();deadline=min(begin+POLICY['local_admission_s'],req['deadline_perf']);local=Clock(deadline)
    held=copy.deepcopy(incumbent);route=held['trajectory'];model=Model(route,req['case']['initial_reserve_fraction'])
    initial_time=model.total;start=np.asarray(route['state_start'],float);goal=np.asarray(route['state_goal'],float)
    events=[];iters=[];selected=[];status='STARTED';opt=None;last_eq=None;projection_trials=[];phase_costs=dict(independent_C_s=0.,collision_s=0.)
    counts=dict(objective_callbacks=0,constraint_callbacks=0,model_integrations=0,model_ODE_started=0,model_ODE_completed=0,
                model_RHS_completed_solves=0,model_ODE_aborted=0,independent_C_calls=0,collision_calls=0)
    stats0=ctx.raw.query_stats() if hasattr(ctx.raw,'query_stats') else None
    from phase_g61.uncovered_environments_v1 import wind_remainder
    original_ivp=wind_remainder.solve_ivp
    def timed_ivp(fun,*args,**kwargs):
        local.check();counts['model_ODE_started']+=1
        def rhs(t,y):local.check();return fun(t,y)
        try:
            sol=original_ivp(rhs,*args,**kwargs);counts['model_ODE_completed']+=1;counts['model_RHS_completed_solves']+=int(sol.nfev)
            return sol
        except BaseException:
            counts['model_ODE_aborted']+=1;raise
    def evaluate(x):
        local.check()
        proposed=np.asarray(x,float)
        if proposed.shape==model.x0.shape and np.isfinite(proposed).all() and (np.any(proposed<model.lower) or np.any(proposed>model.upper)):
            projection_trials.append(dict(x=proposed.tolist(),elapsed_s=time.perf_counter()-begin,optimizer_variables_only=True))
        us,ds=model.decode(x);counts['model_integrations']+=1
        end,nodes=wind_remainder.integrate(start,us,ds,rctx.provider,rctx.z)
        local.check();return us,ds,end,nodes
    def objective(x):local.check();counts['objective_callbacks']+=1;return model.objective(x)
    def constraint(x):
        nonlocal last_eq
        counts['constraint_callbacks']+=1;us,ds,end,nodes=evaluate(x)
        error=(end-goal)/np.array([R0,R0,1.]);last_eq=error.tolist()
        events.append(dict(kind='ENDPOINT_TRIAL',x=np.asarray(x).tolist(),total_s=math.fsum(ds),
            scaled_residual=last_eq,elapsed_s=time.perf_counter()-begin,not_candidate_admission=True))
        return error
    def candidate(x):
        nonlocal held
        us,ds,end,nodes=evaluate(x);total=math.fsum(ds)
        row=dict(iteration=len(iters),total_s=total,position_m=float(np.linalg.norm(end[:2]-goal[:2])),
                 raw_heading_rad=float(end[2]-goal[2]),elapsed_s=time.perf_counter()-begin,status='ENDPOINT_NOT_ADMISSIBLE')
        iters.append(row)
        if total>=math.fsum(held['trajectory']['durations'])-POLICY['minimum_candidate_gain_s']:row['status']='NO_STRICT_TIME_GAIN';return
        if row['position_m']>THRESHOLDS['position_over_R0']*R0*.5 or abs(row['raw_heading_rad'])>THRESHOLDS['heading_rad']*.5:return
        tr=copy.deepcopy(route);tr.update(controls=us.tolist(),durations=ds.tolist(),states=nodes)
        counts['independent_C_calls']+=1;ct=time.perf_counter()
        try:c=rctx.replay(tr,total)
        finally:phase_costs['independent_C_s']+=time.perf_counter()-ct
        if any(f in ('INTERIOR_NODE_RESIDUAL','CONTROL_BOUND') for f in c['failures']):raise Stop('TIME_REFINEMENT_C_STATE_OR_CONTROL_MISMATCH')
        if not c['valid'] or abs(c['diagnostic_final_state'][2]-goal[2])>THRESHOLDS['heading_rad']:
            row.update(status='ORIGINAL_C_OR_RAW_LIFT_REJECT',C_failures=c['failures']);return
        counts['collision_calls']+=1;ct=time.perf_counter()
        try:co=rctx.checker.check(tr)
        finally:phase_costs['collision_s']+=time.perf_counter()-ct
        if not co['valid']:row.update(status='COLLISION_REJECT',collision_status=co['status']);return
        local.check()
        data=copy.deepcopy(held);data.update(trajectory=tr,C=c,collision=co,planned_peak_turn_rate=float(np.max(np.abs(us))),
            request_sha256=content(req),context_input_sha256=content(req['case']),nonce=req.get('nonce'),
            context_id=ctx.id,trajectory_sha256=content(tr),
            time_optimization=dict(version=POLICY['version'],initial_incumbent_time_s=initial_time,new_time_s=total,
                                   context_id=rctx.id,elapsed_s=time.perf_counter()-begin))
        path=base/'time_refinement'/f'candidate_{len(selected):02d}.json';put(path,data)
        # A candidate durable only after cutoff cannot replace the first feasible.
        local.check();held=data;selected.append(dict(file=str(path),time_s=total,elapsed_s=time.perf_counter()-begin))
        row['status']='COMPLETE_TIMELY_C_COLLISION_RAW_LIFT_ADMITTED'
    try:
        local.check();rctx=type(ctx)(ctx.case,ctx.raw,local,ctx.offset)
        if rctx.id!=ctx.id:raise Stop('TIME_REFINEMENT_CONTEXT_IDENTITY')
        if len(model.x0)<3:status='INSUFFICIENT_MODEL_VARIABLES_NO_REFINEMENT'
        else:
            with rctx.evaluation(),patch.object(wind_remainder,'solve_ivp',timed_ivp):
                opt=(minimize_fn or minimize)(objective,model.x0,method='SLSQP',bounds=model.bounds,
                    constraints=[dict(type='eq',fun=constraint),dict(type='ineq',fun=lambda x:1.-model.objective(x))],
                    callback=candidate,options=dict(ftol=POLICY['ftol'],maxiter=POLICY['maxiter'],eps=POLICY['eps'],disp=False))
                candidate(opt.x);local.check()
            status='OPTIMIZER_RETURNED'
    except Limit:status='LOCAL_ADMISSION_DEADLINE_EXHAUSTED_INCUMBENT_RETAINED'
    except (SpatialIntegrationError,ModelDomainError) as exc:
        status='MODEL_TRIAL_REJECT_INCUMBENT_RETAINED';events.append(dict(kind='REJECT',detail=type(exc).__name__+':'+str(exc)))
    except np.linalg.LinAlgError as exc:status='LOCAL_NUMERICAL_LIMIT_INCUMBENT_RETAINED';events.append(dict(kind='NUMERICAL',detail=str(exc)))
    except BaseException:
        status='STRUCTURAL_REFINEMENT_EXCEPTION';raise
    finally:
        finish=time.perf_counter();stats1=ctx.raw.query_stats() if hasattr(ctx.raw,'query_stats') else None
        payload=dict(policy=POLICY,status=status,context_id=ctx.id,request_sha256=content(req),initial_incumbent_time_s=initial_time,
            selected_time_s=math.fsum(held['trajectory']['durations']),selected_candidate_count=len(selected),selected=selected,
            model_variables=len(model.x0),segments=model.n,initial_x=model.x0.tolist(),bounds=model.bounds,
            counts=counts,phase_costs=phase_costs,trial_evaluations=events,iterations=iters,projected_model_trials=projection_trials,local_begin_perf=begin,local_deadline_perf=deadline,
            computation_finish_perf=finish,computation_inclusive_s=finish-begin,local_compute_overrun_s=max(0.,finish-deadline),
            original_worker_deadline_perf=req['deadline_perf'],last_scaled_equality=last_eq,
            optimizer=None if opt is None else dict(success=bool(opt.success),status=int(opt.status),message=str(opt.message),nit=int(opt.nit),nfev=int(opt.nfev)),
            nfev_is_objective_callbacks_not_ODE_count=True,aborted_ODE_RHS_count='UNKNOWN' if counts['model_ODE_aborted'] else 0,
            counters_scope='Model ODE only; independent C/collision use original receipts; source query delta includes all refinement roles',
            raw_query_stats_before=stats0,raw_query_stats_after=stats1,minimize_injected_for_unit_test=minimize_fn is not None,
            no_online_replan_math_or_deadline_change=True,no_historical_route_oracle=True,first_valid_preserved_on_any_ordinary_limit=True)
        put(base/'time_refinement'/'summary.json',payload)
    return held
