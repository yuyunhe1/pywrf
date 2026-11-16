"""Trusted inputs are explicit; no returned payload defines the scientific task."""
import copy,math,json
import numpy as np
from phase4a_design.mock_contract import content_hash
from phase4t.amendment_v1.model import PlannedRoute,StructuralStop

CONFIG=dict(version='ORDERED_LOS_WITH_CONSTANT_STRAIGHT_TRANSVERSE_CAPTURE_V1',tick_s=.02,
    lookahead_m=15.,heading_gain_per_s=1.5,projection_window_parameter_s=.1,
    capture='STATE_FEEDBACK_BANG_BANG_TO_ORIGINAL_LINE_NOT_ENDPOINT_CONNECTOR',
    capture_support='CONSTANT_PROVIDER_AND_ORIGINAL_ZERO_OMEGA_SEGMENT_ONLY',
    capture_roundoff_m=1e-10,capture_roundoff_rad=1e-12,
    crossing_roundoff_m=1e-9,progress_parameter_roundoff_s=1e-9,
    physical_horizon='2 * math.fsum(immutable_planned_durations) + 10',
    wall_cap_s=120.,cross_track_role='METRIC_ONLY',no_replanning=True)

def bind(route,context,arm,initial_state=None,*,config=None,case_id='TEST'):
    route.verify();cfg=copy.deepcopy(CONFIG if config is None else config)
    q=route.tr['state_start'] if initial_state is None else list(initial_state)
    if np.asarray(q).shape!=(3,) or not np.isfinite(q).all():raise StructuralStop('INITIAL_STATE_SCHEMA')
    descriptor=json.loads(json.dumps(context.descriptor,allow_nan=False))
    if content_hash(descriptor)!=context.id:raise StructuralStop('CONTEXT_DESCRIPTOR_HASH')
    return dict(version='EXECUTION_BINDING_V1',case_id=case_id,arm=arm,
        planned_route_sha256=route.sha256,declared_initial_state=list(q),original_goal=route.tr['state_goal'],
        context_id=context.id,context=descriptor,config=cfg,config_sha256=content_hash(cfg),
        T_ref=route.T_ref,T_exec=route.T_exec,wall_cap_s=cfg['wall_cap_s'])

def check_execution(ex,route,context,binding):
    """Fail before C if a worker substitutes goal, context, time, start, or controls."""
    route.verify()
    expected=bind(route,context,binding['arm'],binding['declared_initial_state'],
        config=binding['config'],case_id=binding['case_id'])
    if binding!=expected:raise StructuralStop('IMMUTABLE_BINDING_MISMATCH')
    if ex.get('binding_sha256')!=content_hash(expected):raise StructuralStop('REQUEST_CONTEXT_BINDING_MISMATCH')
    if ex.get('planned_route_sha256')!=route.sha256:raise StructuralStop('ROUTE_HASH_MISMATCH')
    if ex.get('T_ref')!=route.T_ref or ex.get('T_exec')!=route.T_exec:raise StructuralStop('HORIZON_MISMATCH')
    tr=ex['trajectory'];original=route.tr
    for k in original:
        if k not in ('controls','durations','states','state_start') and tr.get(k)!=original[k]:
            raise StructuralStop('IMMUTABLE_TRAJECTORY_FIELD:'+k)
    if tr.get('state_start')!=expected['declared_initial_state']:raise StructuralStop('EXECUTION_INITIAL_STATE_MISMATCH')
    ds=np.asarray(tr['durations'],float);us=np.asarray(tr['controls'],float);qs=np.asarray(tr['states'],float)
    if ds.ndim!=1 or ds.shape!=us.shape or qs.shape!=(len(us)+1,3) or not np.isfinite(np.r_[ds,us,qs.ravel()]).all() or np.any(ds<=0):
        raise StructuralStop('ACTUAL_EXECUTION_SCHEMA')
    if ex.get('actual_controls_sha256')!=content_hash(dict(controls=tr['controls'],durations=tr['durations'])):
        raise StructuralStop('ACTUAL_CONTROLS_CONTENT_HASH')
    if ex.get('executed_trajectory_sha256')!=content_hash(tr):raise StructuralStop('EXECUTED_CONTENT_HASH')
    total=math.fsum(ds)
    if abs(total-ex['physical_time_s'])>1e-10:raise StructuralStop('ACTUAL_TIME_MISMATCH')
    if not np.array_equal(qs[0],tr['state_start']):raise StructuralStop('FIRST_STATE_MISMATCH')
    return total
