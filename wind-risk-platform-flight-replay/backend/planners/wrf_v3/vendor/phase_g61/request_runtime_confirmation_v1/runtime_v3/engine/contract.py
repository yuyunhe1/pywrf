import copy, math
from fractions import Fraction
from pathlib import Path
from ..work import D as ROUND, ROOT, read, sha, content

def fail(condition, label):
    if not condition:
        raise ValueError(label)

def binding(root):
    return dict(version='MIDFLIGHT_GLOBAL_ACTUAL_JOURNAL_V1', root_request_sha256=content(root), case_id=root['case']['id'], initial_planned_route_sha256=content(root['case']['route']), original_goal=root['case']['route']['state_goal'], T_ref_s=root['case']['T_ref_s'], physical_deadline_s=root['case']['physical_deadline_s'])

def carrier(route, offset, actual):
    cut = Fraction(float(offset))
    t = Fraction(0)
    us = []
    ds = []
    nodes = [list(actual)]
    for i, (u, d) in enumerate(zip(route['controls'], route['durations'])):
        end = t + Fraction(float(d))
        if end > cut:
            us.append(float(u))
            ds.append(float(end - max(t, cut)))
            nodes.append(list(route['states'][i + 1]))
        t = end
    fail(bool(us), 'NO_CURRENT_NOMINAL_SUFFIX')
    result = copy.deepcopy(route)
    result.update(state_start=list(actual), controls=us, durations=ds, states=nodes)
    return result

def nominal_heading(route, t):
    left = Fraction(float(t))
    terms = []
    for u, d in zip(route['controls'], route['durations']):
        used = min(max(left, Fraction(0)), Fraction(float(d)))
        terms.append(float(u) * float(used))
        left -= Fraction(float(d))
    return float(route['state_start'][2]) + math.fsum(terms)

def _inherited_verify_root(root):
    from .runtime.common import D
    m = read(D.parent / 'ROOT_INPUT_MANIFEST_V2.json')
    f = read(D / 'RUNTIME_FREEZE.json')
    fail(f['manifest_sha256'] == sha(D.parent / 'ROOT_INPUT_MANIFEST_V2.json'), 'ROOT_MANIFEST_HASH')
    fail(root['version'] == 'G61_MIDFLIGHT_ROOT_V1', 'ROOT_SCHEMA')
    fail(root['runtime_freeze_sha256'] == sha(D / 'RUNTIME_FREEZE.json') and root['epoch'] == f['epoch'], 'ROOT_SOURCE_IDENTITY')
    fail(root['nonce'] == f['nonces'].get(root['run_id']), 'ROOT_NONCE')
    fail(any((r['run_id'] == root['run_id'] and r['case_id'] == root['case']['id'] and (r['arm'] == root['arm']) for r in m['order'])), 'ROOT_ORDER')
    fail(any((content(root['case']) == content(c) for c in m['cases'])), 'ROOT_CASE_IDENTITY')
    case = root['case']
    route = case['route']
    source = case['initial_plan_source']
    pipeline_path = ROOT / source['pipeline_request_file']
    fail(sha(pipeline_path) == source['pipeline_request_sha256'], 'INITIAL_PIPELINE_REQUEST_IDENTITY')
    pipeline = read(pipeline_path)
    fail(case['source_planned_route_identity']['current_live_initial_planning'] is True and f['not_new_live_initial_plan'] is False, 'LIVE_INPUT_ROLE_MISMATCH')
    fail(root['owner_start'] == pipeline['start_perf'] and root['owner_deadline'] == pipeline['root_deadline_perf'], 'CURRENT_LIVE_PIPELINE_WALL_RESET')
    fail(pipeline['case']['route']['state_start'] == route['state_start'] and pipeline['case']['route']['state_goal'] == route['state_goal'], 'FRESH_INITIAL_GOAL_CHANGED')
    fraction = case['initial_reserve_fraction']
    fail(not isinstance(fraction, bool) and fraction in (0.9, 1.0), 'INITIAL_FRACTION_ARM')
    fail(max(map(abs, route['controls'])) <= fraction * (10 / 8.513422179807296) + 1e-12, 'FRESH_INITIAL_PLANNED_RESERVE_LOST')
    fail(case['T_ref_s'] == math.fsum(route['durations']), 'ROOT_TREF_SOURCE')
    fail(case['physical_deadline_s'] == min(2 * case['T_ref_s'] + 10.0, case['legacy_physical_deadline_s']), 'ROOT_PHYSICAL_DEADLINE')
    fail(root['owner_deadline'] == root['owner_start'] + 120.0, 'ROOT_WALL_DEADLINE')
    fail(route['parameters'] == {'Va': 10.0, 'R0': 8.513422179807296}, 'ROOT_PARAMETERS')
    return True

def within(base, relative):
    path = (base / relative).resolve()
    fail(path.is_relative_to(base.resolve()), 'TX_PATH_ESCAPE')
    return path

from ..transaction_contract import prefix_controls,verify_clock,verify_transaction
def verify_root(root):
    _inherited_verify_root(root)
    from ..roles import validate_root
    pipeline=read(ROOT/root['case']['initial_plan_source']['pipeline_request_file'])
    validate_root(root,pipeline)
    fail(root['case']['parent_validation_policy']=='HOT_SINGLE_USE_WORKER_C_OWNER_COLLISION_EARLY_REFERENCE_V1','PARENT_SCHEDULE_POLICY')
    fail(root['case']['prefix_evidence_policy']=='ORIGINAL_ROW_REPLAY_ON_DURABLE_ACTUAL_HISTORY_V1','STREAM_POLICY_IDENTITY')
    fail('faults' not in root['case'] and 'future_faults' not in root['case'],'PUBLIC_FUTURE_PLANT_SCHEDULE')
    fail(root['case']['causal_prepare_policy']=={'maximum_groups':2,'maximum_attempts':2,'original_prepare_s':8.,'restart':'FIRST_OBSERVED_NORMAL_AFTER_ACTUATOR_MISMATCH'},'CAUSAL_POLICY_CHANGED')
    fail(isinstance(root['case']['private_plant_sha256'],str) and len(root['case']['private_plant_sha256'])==64,'PRIVATE_PLANT_IDENTITY')
    return True
