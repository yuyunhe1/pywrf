"""Necessary source-time coverage test; never physical infeasibility or success."""
import datetime as dt
import math
from .native_clock import clock_cells
from .work import content
from phase3a_implementation.domain import VA,R0
from phase3b.validator import THRESHOLDS
from phase4t.amendment_v1.model import StructuralStop

SCHEMA='NECESSARY_TIME_COVERAGE_BOUND_WITH_ORIGINAL_BETA_DOMAIN_V1'


def evaluate(case,source):
    tr=case['route'];start,goal=tr['state_start'],tr['state_goal']
    if tr['parameters']!={'Va':10.,'R0':8.513422179807296} or VA!=10.:raise StructuralStop('TIME_GUARD_ORIGINAL_MODEL_REQUIRED')
    if any(not isinstance(v,(list,tuple)) or len(v)!=3 for v in [start,goal]):raise StructuralStop('TIME_GUARD_STATE_SCHEMA')
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for q in [start,goal] for v in q):raise StructuralStop('TIME_GUARD_STATE_SCHEMA')
    native=source['native_utc'];offset=source.get('physical_offset_s',0.)
    # Use the same validated UTC coverage contract as the production provider.
    # Checking the last source alone would accidentally admit an earlier start.
    _,coverage=clock_cells(source,case['provider']['max_gap_s'])
    available=coverage[1]
    dx,dy=abs(goal[0]-start[0]),abs(goal[1]-start[1])
    if not math.isfinite(dx+dy):raise StructuralStop('TIME_GUARD_DISTANCE_OVERFLOW')
    tolerance=R0*THRESHOLDS['position_over_R0']
    roundoff=64.*max(math.ulp(v) for q in [start,goal] for v in q[:2])
    lower_distance=max(0.,math.nextafter(max(dx,dy)-tolerance-roundoff,-math.inf))
    lower_time=max(0.,math.nextafter(lower_distance/(2.*VA),0.))
    return dict(schema=SCHEMA,decision='REJECT_SOURCE_TIME_COVERAGE' if lower_time>available else 'PERMIT_CANDIDATE_SEARCH_NOT_FEASIBILITY',
        necessary_position_time_lower_s=lower_time,available_source_time_s=available,
        position_distance_lower_m=lower_distance,max_ground_speed_upper_mps=2.*VA,
        original_terminal_position_tolerance_m=tolerance,coordinate_roundoff_allowance_m=roundoff,
        conditional_model='Any path satisfying unchanged Va10 and beta<1 has ground speed<20; no claim outside that model',
        native_utc=list(native),mission_epoch_utc=source['mission_epoch_utc'],physical_offset_s=float(offset),
        case_sha256=content(case),source_spec_sha256=content(case['provider']),
        physical_infeasibility_proven=False,flight_or_route_success=False)


def source_from_spec(case):
    spec=case['provider']
    if spec['kind']!='WRF_TIME_LINEAR_SNAPSHOTS':raise StructuralStop('TIME_GUARD_TEMPORAL_SOURCE_REQUIRED')
    source=dict(native_utc=[dt.datetime.strptime(r['expected_time'],'%Y-%m-%d_%H:%M:%S').replace(tzinfo=dt.timezone.utc).isoformat() for r in spec['records']],
        mission_epoch_utc=spec['epoch_utc'],physical_offset_s=0.)
    clock_cells(source,spec['max_gap_s'])
    return source


def verify_receipt(req,receipt):
    if not isinstance(receipt,dict) or set(receipt)!={'request_sha256','nonce','context_id','context_descriptor','proof'}:raise StructuralStop('TIME_GUARD_RECEIPT_SCHEMA')
    if receipt['request_sha256']!=content(req) or receipt['nonce']!=req['nonce']:raise StructuralStop('TIME_GUARD_REQUEST_IDENTITY')
    descriptor=receipt['context_descriptor'];case=req['case']
    if not isinstance(descriptor,dict) or content(descriptor)!=receipt['context_id']:raise StructuralStop('TIME_GUARD_CONTEXT_IDENTITY')
    if descriptor.get('case_sha256')!=content(case) or descriptor.get('provider_spec')!=case['provider'] or descriptor.get('parameters')!=case['route']['parameters'] or descriptor.get('z_layer_msl_m')!=case['route']['z_layer_msl_m'] or descriptor.get('mission_offset_s')!=0.:raise StructuralStop('TIME_GUARD_CONTEXT_BINDING')
    if descriptor.get('C',{}).get('thresholds')!=dict(THRESHOLDS):raise StructuralStop('TIME_GUARD_C_CONTRACT')
    expected=evaluate(req['case'],source_from_spec(req['case']))
    if evaluate(case,descriptor['source'])!=expected:raise StructuralStop('TIME_GUARD_ACTUAL_SOURCE_CONTEXT_MISMATCH')
    if receipt['proof']!=expected:raise StructuralStop('TIME_GUARD_BOUND_OR_SOURCE_MISMATCH')
    if expected['decision']!='REJECT_SOURCE_TIME_COVERAGE':raise StructuralStop('TIME_GUARD_FALSE_DOMAIN_REJECTION')
    return expected
