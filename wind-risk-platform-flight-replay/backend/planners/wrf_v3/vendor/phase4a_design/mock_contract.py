"""Executable contract specimen for mock callers only, not a planner adapter.

Backend injection is explicit. No graph, sampler, steering search, rewiring,
collision checker, cache, time-state or production connector API is implemented.
"""
import copy
from dataclasses import asdict, dataclass
import hashlib
import json
import math

from phase3a_implementation.coordinates import FRAME_ID, HEIGHT_DATUM, ROI
from phase3a_implementation.domain import VA, R0
from phase3b.validator import THRESHOLDS

VERSION = 'PHASE4A_CONTRACT_V1'
STATUSES = ('SUCCESS', 'OUT_OF_DOMAIN', 'NO_SOLUTION', 'NUMERICAL_LIMIT')
ORIGIN = (31.05935, 118.55255)


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Node:
    x: float
    y: float
    z: float
    heading: float
    time: float | None = None

    def __post_init__(self):
        for value in (self.x, self.y, self.z, self.heading):
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                raise ValueError('Node components must be finite numbers, in meters/radians')
        if self.time is not None and (isinstance(self.time, bool) or
                not isinstance(self.time, (float, int)) or not math.isfinite(self.time) or self.time < 0):
            raise ValueError('time is null or nonnegative accumulated-duration metadata')

    def q(self):
        return [float(self.x), float(self.y), float(self.heading)]


@dataclass(frozen=True)
class SnapshotBinding:
    snapshot_id: str
    source_sha256: str
    record_index: int
    source_time: str
    roi: ROI
    provider_kind: str = 'WRF_STATIC_SNAPSHOT'
    frame_id: str = FRAME_ID
    height_datum: str = HEIGHT_DATUM
    origin_lat_lon_deg: tuple = ORIGIN

    def __post_init__(self):
        if (len(self.source_sha256) != 64 or
                any(c not in '0123456789abcdef' for c in self.source_sha256)):
            raise ValueError('Lowercase source SHA256 required')
        if (isinstance(self.record_index, bool) or not isinstance(self.record_index, int)
                or self.record_index < 0 or not isinstance(self.roi, ROI)):
            raise ValueError('Explicit ROI and nonnegative record required')
        if not self.snapshot_id or not self.source_time:
            raise ValueError('Snapshot identity and fixed source time required')

    def source_identity(self):
        return {k: getattr(self, k) for k in
                ('snapshot_id', 'source_sha256', 'record_index', 'source_time')}

    def as_dict(self):
        out = asdict(self)
        out['roi'] = self.roi.as_dict()
        return out


def request_record(start, goal, snapshot, request_id):
    return dict(contract_version=VERSION, request_id=request_id,
                start=asdict(start), goal=asdict(goal), snapshot=snapshot.as_dict(),
                z_layer_msl_m=float(start.z), parameters={'Va': VA, 'R0': R0},
                cost_model='TRAVEL_TIME_SECONDS_V1')


def _quality(raw):
    selected = []
    counts = {}
    for attempt in raw.get('attempts', []):
        for stage in attempt.get('stages', []):
            label = stage.get('optimization_status')
            if label:
                counts[label] = counts.get(label, 0) + 1
            if attempt.get('seed_index') == raw.get('seed_index'):
                selected.append({k: v for k, v in stage.items() if k != 'C'})
    return dict(global_optimality_guaranteed=False, numerical_completeness='UNRESOLVED',
                spatial_optimality='UNPROVEN', generation_stage=raw.get('generation_stage'),
                selected_seed_stages=selected, local_optimizer_status_counts=counts,
                collision_status='NOT_CHECKED', continuous_domain_certificate=False)


def _success_evidence(raw, start, goal, snapshot):
    """Check evidence consistency; independent C was run by the existing backend."""
    c, tr, total = raw.get('validation'), raw.get('trajectory'), raw.get('time')
    if (raw.get('valid') is not True or raw.get('status') != 'SUCCESS'
            or not isinstance(c, dict) or c.get('valid') is not True
            or c.get('method') != 'INDEPENDENT_DOP853'
            or c.get('thresholds') != THRESHOLDS or c.get('failures') != []):
        return 'C_EVIDENCE_MISSING_OR_FAILED'
    if not isinstance(tr, dict) or isinstance(total, bool) or not isinstance(total, (float, int)):
        return 'MALFORMED_SUCCESS'
    if (tr.get('state_start') != start.q() or tr.get('state_goal') != goal.q()
            or tr.get('z_layer_msl_m') != start.z or tr.get('parameters') != {'Va': VA, 'R0': R0}
            or tr.get('frame_id') != snapshot.frame_id or tr.get('height_datum') != snapshot.height_datum
            or tr.get('type') != 'piecewise_constant' or tr.get('control_interpolation') != 'ZOH'):
        return 'TRAJECTORY_REQUEST_MISMATCH'
    try:
        ds, us, nodes = tr['durations'], tr['controls'], tr['states']
        if len(ds) != len(us) or len(nodes) != len(us) + 1:
            return 'TRAJECTORY_SHAPE_MISMATCH'
        values = [total, *ds, *us, *(v for node in nodes for v in node)]
        if any(isinstance(v, bool) or not math.isfinite(v) for v in values) or any(d < 0 for d in ds):
            return 'NONFINITE_OR_NEGATIVE_OUTPUT'
        if total < 0 or abs(total - math.fsum(ds)) > 64 * math.ulp(1.) * max(1., total):
            return 'TIME_MISMATCH'
        if any(len(node) != 3 for node in nodes) or any(abs(u)/(VA/R0)-1 > THRESHOLDS['control_over_umax'] for u in us):
            return 'CONTROL_OR_NODE_CONTRACT'
        for value, limit in (
            (c['position_error']/R0, THRESHOLDS['position_over_R0']),
            (c['heading_error'], THRESHOLDS['heading_rad']),
            (c['control_violation']/(VA/R0), THRESHOLDS['control_over_umax']),
            (c['constraint_residual']['interior_node_scaled'], THRESHOLDS['interior_node_scaled']),
        ):
            if not math.isfinite(value) or value < 0 or value > limit:
                return 'C_RESIDUAL_LIMIT'
        if c.get('domain_status') != 'IN_DOMAIN_AT_EVALUATED_POINTS' or not c.get('beta_history'):
            return 'C_DOMAIN_EVIDENCE_MISSING'
        if any(not math.isfinite(v['beta']) or not 0 <= v['beta'] < 1 for v in c['beta_history']):
            return 'C_DOMAIN_EVIDENCE_FAILED'
    except (KeyError, TypeError, ValueError, OverflowError):
        return 'MALFORMED_SUCCESS'
    source = raw.get('provenance', {}).get('provider_source')
    if not isinstance(source, dict) or any(source.get(k) != v for k, v in snapshot.source_identity().items()):
        return 'SOURCE_BINDING_MISMATCH'
    return None


def mock_connector_call(start, goal, snapshot, *, backend, request_id):
    """A single test request, with injected backend. No production integration."""
    req = request_record(start, goal, snapshot, request_id)
    out = dict(contract_version=VERSION, request_id=request_id, status=None,
               trajectory=None, cost=None, travel_time=None, valid=False, validation=None,
               reason=None, quality=None, no_solution_scope=None,
               provenance=dict(request=req, request_sha256=content_hash(req),
                               execution_mode='MOCK_CONTRACT', backend_called=False,
                               backend_result_sha256=None))
    def fail(status, reason):
        return {**out, 'status': status, 'reason': reason}

    if snapshot.frame_id != FRAME_ID or snapshot.origin_lat_lon_deg != ORIGIN:
        return fail('OUT_OF_DOMAIN', 'FRAME_MISMATCH')
    if snapshot.height_datum != HEIGHT_DATUM:
        return fail('OUT_OF_DOMAIN', 'HEIGHT_DATUM_MISMATCH')
    if snapshot.provider_kind not in ('WRF_STATIC_SNAPSHOT', 'SYNTHETIC_CONSTANT'):
        return fail('OUT_OF_DOMAIN', 'UNSUPPORTED_WIND_MODE')
    if start.z != goal.z:
        return fail('OUT_OF_DOMAIN', 'UNSUPPORTED_ALTITUDE_CHANGE')
    if goal.time is not None:
        return fail('OUT_OF_DOMAIN', 'UNSUPPORTED_ARRIVAL_TIME_CONSTRAINT')
    if not all(snapshot.roi.contains(node.x, node.y) for node in (start, goal)):
        return fail('OUT_OF_DOMAIN', 'OUT_OF_ROI')
    out['provenance']['backend_called'] = True
    try:
        raw = backend(start, goal, snapshot)
    except TimeoutError:
        return fail('NUMERICAL_LIMIT', 'BACKEND_TIMEOUT')
    # Programming errors deliberately propagate to the test runner.
    try:
        out['provenance']['backend_result_sha256'] = content_hash(raw)
    except (TypeError, ValueError):
        return fail('NUMERICAL_LIMIT', 'MALFORMED_BACKEND_OUTPUT')
    if not isinstance(raw, dict):
        return fail('NUMERICAL_LIMIT', 'MALFORMED_BACKEND_OUTPUT')
    out['provenance'].update(backend_status=raw.get('status'),
                             candidate_count=raw.get('candidate_count'),
                             solver_started=raw.get('solver_started'),
                             C_result_sha256=content_hash(raw['validation']) if raw.get('validation') else None)
    if raw.get('valid'):
        reason = _success_evidence(raw, start, goal, snapshot)
        if reason:
            return fail('NUMERICAL_LIMIT', reason)
        return {**out, 'status': 'SUCCESS', 'valid': True,
                'trajectory': copy.deepcopy(raw['trajectory']), 'travel_time': raw['time'],
                'cost': raw['time'], 'validation': copy.deepcopy(raw['validation']),
                'quality': _quality(raw)}
    if raw.get('trajectory') is not None or raw.get('time') is not None:
        return fail('NUMERICAL_LIMIT', 'FAILURE_HAS_FORMAL_TRAJECTORY')
    status = raw.get('status')
    if status == 'WIND_QUERY_FAILED':
        reason = raw.get('detail') or status
        domain_reasons = ('OUT_OF_ROI', 'BELOW_TERRAIN', 'OUT_OF_HORIZONTAL_DOMAIN',
                          'BELOW_VERTICAL_DOMAIN', 'ABOVE_VERTICAL_DOMAIN')
        return fail('OUT_OF_DOMAIN' if reason in domain_reasons else 'NUMERICAL_LIMIT', reason)
    if status in ('CONNECTOR_OUT_OF_DOMAIN', 'FRAME_MISMATCH',
                  'HEIGHT_DATUM_MISMATCH', 'UNSUPPORTED_PROVIDER', 'INVALID_INPUT'):
        reason = raw.get('detail') or status
        return fail('OUT_OF_DOMAIN', reason)
    if status == 'SEED_GENERATION_FAILED':
        return {**fail('NO_SOLUTION', 'SEED_GENERATION_FAILED'),
                'no_solution_scope': 'CONFIGURED_CONSTANT_SEED_GENERATOR_ONLY_NOT_PHYSICAL_INFEASIBILITY'}
    return fail('NUMERICAL_LIMIT', status or 'UNCLASSIFIED_BACKEND_FAILURE')


def rank_edges(edges):
    """Sort one mock caller's explicit successful edge list; do not grow a tree."""
    successes = []
    context = None
    for edge in edges:
        if edge.get('status') != 'SUCCESS':
            if any(edge.get(k) is not None for k in ('trajectory', 'cost', 'travel_time')):
                raise ValueError('Failed edge has a formal trajectory/cost/time')
            continue
        if (edge.get('valid') is not True or edge.get('validation', {}).get('valid') is not True
                or edge.get('trajectory') is None or edge.get('cost') != edge.get('travel_time')
                or not isinstance(edge['cost'], (float, int)) or not math.isfinite(edge['cost'])
                or edge['cost'] < 0):
            raise ValueError('Invalid successful edge cannot be ranked')
        req = edge['provenance']['request']
        ctx = content_hash({k: req[k] for k in ('snapshot', 'z_layer_msl_m', 'parameters', 'cost_model')})
        if context is None:
            context = ctx
        elif context != ctx:
            raise ValueError('Rank only costs in one model/snapshot/ROI context')
        successes.append(edge)
    return sorted(successes, key=lambda e: (e['cost'], e['request_id']))
