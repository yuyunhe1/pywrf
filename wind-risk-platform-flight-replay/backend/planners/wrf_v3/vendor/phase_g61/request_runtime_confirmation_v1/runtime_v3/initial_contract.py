import copy,math
from .work import content
from phase3b.validator import THRESHOLDS
def admit_initial(req, value, observed_perf):
    if value['request_sha256'] != content(req) or value['nonce'] != req['nonce']:
        raise ValueError('INITIAL_REQUEST_IDENTITY')
    if not (value['published_perf'] < req['deadline_perf'] and observed_perf < req['deadline_perf']):
        raise ValueError('INITIAL_WORKER_LATE')
    if value['status'] != 'INITIAL_PLAN_FOUND' or not isinstance(value.get('result'), dict):
        raise ValueError('INITIAL_NO_VALID_ROUTE')
    data = value['result']
    tr = data['trajectory']
    c = data['C']
    co = data['collision']
    original = req['case']['route']
    if c['valid'] is not True or co['valid'] is not True or c['thresholds'] != THRESHOLDS:
        raise ValueError('INITIAL_C_COLLISION_NOT_PASS')
    if tr['state_start'] != original['state_start'] or tr['state_goal'] != original['state_goal'] or tr['parameters'] != {'Va': 10.0, 'R0': 8.513422179807296}:
        raise ValueError('INITIAL_PHYSICAL_ENDPOINT_IDENTITY')
    if any((tr.get(k) != original.get(k) for k in ('frame_id', 'height_datum', 'z_layer_msl_m'))):
        raise ValueError('INITIAL_FRAME_OR_ALTITUDE_IDENTITY')
    if any((isinstance(v, bool) for v in tr['controls'] + tr['durations'])):
        raise ValueError('INITIAL_CONTROL_DURATION_BOOL_SCHEMA')
    if abs(c['diagnostic_final_state'][2] - original['state_goal'][2]) > 1e-07 or c['beta_range'][1] >= 1.0:
        raise ValueError('INITIAL_RAW_LIFT_OR_DOMAIN')
    if len(tr['controls']) != len(tr['durations']) or any((not math.isfinite(d) or d <= 0 for d in tr['durations'])):
        raise ValueError('INITIAL_DURATION_SCHEMA')
    if any((not math.isfinite(u) or abs(u) > req['case']['initial_reserve_fraction'] * (10 / 8.513422179807296) + 1e-12 for u in tr['controls'])):
        raise ValueError('INITIAL_RESERVE_CONTROL')
    return copy.deepcopy(tr)
