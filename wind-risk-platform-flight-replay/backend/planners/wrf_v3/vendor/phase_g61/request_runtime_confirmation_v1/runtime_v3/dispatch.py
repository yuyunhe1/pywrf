"""Same original independent ordered assays, scheduled without primary-array waits.

All four original MID and conditional LOWER/UPPER assays remain, as does the
old extra reference MID equality guard. No state or event oracle is shared.
"""
import importlib
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
import numpy as np
from .work import D, ROOT, read, sha, content
from phase4t_g61.wrf_feasibility_validation_v1.runtime import ordered as original, boundaries
from phase_g61.online_execution_repair_v1.compensated_dop853 import CompensatedDOP853, solve_ivp
from phase_g61.uncovered_environments_v1.committed_events import integrate
from phase_g61.terminal_guard_timepilot_v1.parent_overlap_v1.engine import array_helpers
from phase3b.validator import THRESHOLDS
from phase4t.amendment_v1.model import StructuralStop, DomainFail

MODULE = 'phase_g61.request_runtime_confirmation_v1.runtime_v3.dispatch'
TASKS = {
 'coarse_mid': [('actual',0,'MID'),('reference',0,'MID')],
 'coarse_lower': [('actual',0,'LOWER'),('reference',0,'LOWER')],
 'coarse_upper': [('actual',0,'UPPER'),('reference',0,'UPPER')],
 'fine_reference_primary': [('reference',1,'MID')],
 'fine_reference_guard': [('reference',1,'MID')],
 'fine_actual_lower': [('actual',1,'LOWER')],
 'fine_actual_upper': [('actual',1,'UPPER')],
 'fine_reference_lower': [('reference',1,'LOWER')],
 'fine_reference_upper': [('reference',1,'UPPER')],
}
ROLES = tuple(TASKS)
POLICY = dict(version='NINE_FRESH_WORKERS_LOSSLESS_REFERENCE_DENSE_SAME13_V1',
 workers=9,persistent_workers=False,thresholds_changed=False,
 owner='FINE_ACTUAL_MID_AND_ORIGINAL_PROGRESS',
 reference='INDEPENDENT_FINE_MID_NUMERIC_MATERIAL;NO_ODE_ON_IMPORT',
 shared_event_lists=False,shared_ODE_initial_states=False,
 extra_reference_MID=1,extra_reference_array_must_be_bit_equal=True,
 unconditional_speculative_alternatives_counted=True,
 alternative_admission='ORIGINAL_INDEPENDENT_PRIMARY_EVENT_PREDICATES_ONLY')


def store(path, value):
    """Original atomic full-body fsync/readback contract, restricted to this round."""
    path = Path(path).resolve()
    if not path.is_relative_to(D) or path.exists():
        raise StructuralStop('ORDERED_WRITE_SCOPE_OR_OVERWRITE')
    path.parent.mkdir(parents=True, exist_ok=True)
    data = __import__('json').dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode()
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            if stream.write(data) != len(data):
                raise StructuralStop('ORDERED_SHORT_WRITE')
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        os.unlink(temporary)
        if path.read_bytes() != data:
            raise StructuralStop('ORDERED_FULL_READBACK')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def packet(body):
    return dict(body=body, sha256=content(body))


def unpack(value):
    if not isinstance(value, dict) or set(value) != {'body', 'sha256'} or not isinstance(value['body'], dict):
        raise StructuralStop('ORDERED_PACKET_SCHEMA')
    if content(value['body']) != value['sha256']:
        raise StructuralStop('ORDERED_PACKET_HASH')
    return value['body']


def verify_result(request, value, deadline, observed=None):
    out = unpack(value)
    for field, expected in [('request_sha256', content(request)), ('role', request['role']),
        ('context_id', request['context_id']), ('algorithm_sha256', request['algorithm_sha256'])]:
        if out.get(field) != expected:
            raise StructuralStop('ORDERED_RESULT_IDENTITY:' + field)
    if out.get('status') not in ('PASS', 'NUMERICAL_UNRESOLVED', 'DOMAIN_REJECT'):
        raise StructuralStop('ORDERED_RESULT_STATUS')
    published = out.get('published_perf')
    if isinstance(published, bool) or not isinstance(published, (int, float)) or not math.isfinite(published):
        raise StructuralStop('ORDERED_RESULT_CLOCK_SCHEMA')
    if published >= deadline or (time.perf_counter() if observed is None else observed) >= deadline:
        raise StructuralStop('ORDERED_RESULT_LATE')
    if not isinstance(out.get('primary'), dict) or not isinstance(out.get('alternatives'), list):
        raise StructuralStop('ORDERED_RESULT_ARRAY_SCHEMA')
    return out


def checked_samples(values, count):
    array = np.asarray(values)
    if array.dtype != np.float64 or array.shape != (count, 3) or not np.isfinite(array).all():
        raise StructuralStop('ORDERED_SAMPLE_SCHEMA')
    return values


def primary_meta(path, role, level):
    return dict(level=level, role=role, events=path.engine.events, contacts=path.engine.contacts,
        stats=path.engine.stats, continuous_pieces=path.engine.pieces)


def needed_alternatives(primary_has_events, result):
    """Unneeded speculative numerical results cannot alter the original predicate."""
    if not primary_has_events:
        return []
    if result['status'] == 'DOMAIN_REJECT':
        raise DomainFail(result['detail'])
    if result['status'] == 'NUMERICAL_UNRESOLVED':
        raise boundaries.BoundaryUnresolved(result['detail'], result.get('evidence'))
    alternatives = result['alternatives']
    if [row.get('choice') for row in alternatives] != ['LOWER', 'UPPER']:
        raise StructuralStop('ORDERED_REQUIRED_ALTERNATIVES_MISSING')
    return alternatives


def dense_binding(body):
    return dict(request_sha256=content(body), context_id=body['context_id'],
        nonce=body['root_request']['nonce'], trajectory_sha256=content(body['reference']))


def worker(path):
    from . import dense_material
    path = Path(path)
    body = unpack(read(path))
    if body.get('role') not in TASKS or body.get('algorithm_sha256') != sha(__file__):
        raise StructuralStop('ORDERED_WORKER_ROLE_OR_CODE')
    common = importlib.import_module(body['runtime'] + '.common')
    environment = importlib.import_module(body['runtime'] + '.environment')
    replay = importlib.import_module(body['runtime'] + '.replay')
    common.verify(body['root_request'])
    common.source_check()
    if not body['diagnostic'] and body['evaluation_deadline_perf'] != body['root_request']['owner_deadline']:
        raise StructuralStop('ORDERED_WORKER_DEADLINE_CONTRACT')
    clock = common.Clock(body['evaluation_deadline_perf'])
    setup = time.perf_counter()
    ctx = environment.Context(body['root_request']['case'], environment.load(body['root_request']['case']), clock)
    if ctx.id != body['context_id']:
        raise StructuralStop('ORDERED_WORKER_CONTEXT')
    out = dict(status='PASS', request_sha256=content(body), role=body['role'], context_id=ctx.id,
        algorithm_sha256=body['algorithm_sha256'], primary={}, alternatives=[], role_outputs={},
        setup_s=time.perf_counter() - setup, stages=[], replay_started_count=0)
    with ctx.evaluation(), patch.object(original, 'PartitionReplay', replay.PartitionReplay), patch.object(boundaries, 'DOP853', CompensatedDOP853), patch.object(boundaries, 'solve_ivp', solve_ivp), patch.object(boundaries.PartitionReplay, 'integrate', integrate):
        for role, level, choice in TASKS[body['role']]:
            result = dict(status='PASS', primary={}, primary_metadata=[], alternatives=[])
            out['role_outputs'][role] = result
            probes = body['times'][role]
            start, index = time.perf_counter(), out['replay_started_count']
            out['replay_started_count'] += 1
            store(path.with_name(body['role'] + '_stage_' + str(index) + '_begin.json'),
                dict(role=role, level=level, choice=choice, start_perf=start, request_sha256=content(body)))
            try:
                replayed = original.PathReplay(body[role], ctx.provider, .02 if level == 0 else .01, event_choice=choice)
                values = checked_samples(array_helpers.samples(replayed, probes), len(probes))
                meta = primary_meta(replayed, role, level)
                if choice == 'MID':
                    result['primary'][role] = values
                    result['primary_metadata'].append(meta)
                    if body['role'] == 'fine_reference_primary':
                        began = time.perf_counter()
                        result['dense_material'] = dense_material.export(replayed,
                            path.with_name('fine_reference_primary_dense.npz'), dense_binding(body), clock)
                        result['dense_material_export_s'] = time.perf_counter() - began
                else:
                    result['alternatives'].append(dict(role=role, level=level, choice=choice, values=values,
                        events=replayed.engine.events, stats=replayed.engine.stats,
                        continuous_pieces=replayed.engine.pieces))
                stage = dict(role=role, level=level, choice=choice, inclusive_s=time.perf_counter() - start,
                    events=replayed.engine.events, stats=replayed.engine.stats,
                    continuous_pieces=replayed.engine.pieces, samples_sha256=content(values))
                store(path.with_name(body['role'] + '_stage_' + str(index) + '_complete.json'),
                    dict(stage, request_sha256=content(body)))
                out['stages'].append(stage)
            except boundaries.BoundaryUnresolved as exc:
                result.update(status='NUMERICAL_UNRESOLVED', detail=exc.reason, evidence=exc.evidence)
            except DomainFail as exc:
                result.update(status='DOMAIN_REJECT', detail=str(exc))
    clock.check()
    out['published_perf'] = time.perf_counter()
    store(path.with_name(body['role'] + '_result.json'), packet(out))


def assess(c, actual, reference, req, base, clock, runtime, *, diagnostic=False):
    from fractions import Fraction
    from phase4o.ownership import OwnedJob
    from . import dense_material
    planned = reference.tr
    reference.route.verify()
    for key in ['state_start', 'state_goal', 'frame_id', 'height_datum', 'z_layer_msl_m', 'parameters']:
        if actual.get(key) != planned.get(key):
            raise StructuralStop('ORDERED_ROUTE_IDENTITY:' + key)
    if not diagnostic and (clock.deadline != req['owner_deadline'] or planned['state_goal'] != req['case']['route']['state_goal']):
        raise StructuralStop('ORDERED_FORMAL_CONTRACT')
    result = dict(method='INDEPENDENT_PARALLEL_SAME_NUMERICAL_ORDERED_V1', policy=dict(original.POLICY),
        parallel_policy=POLICY, complete=False, status='FAIL',
        assessment_mode='POSTHOC_DIAGNOSTIC' if diagnostic else 'LIVE_ORIGINAL_DEADLINE',
        original_segment_count=len(planned['durations']), completed_segments=0, trust_controller_progress=False,
        actual_controls_sha256=content(dict(controls=actual['controls'], durations=actual['durations'])),
        planned_route_sha256=reference.route.sha256, tape_audit=original.tape_audit(actual, planned))
    if not c['valid']:
        return dict(result, reason='PHYSICAL_C_NOT_PASSED')
    if abs(c['diagnostic_final_state'][2] - planned['state_goal'][2]) > THRESHOLDS['heading_rad']:
        return dict(result, reason='ORIGINAL_UNWRAPPED_HEADING_MISMATCH')
    def ends(tr):
        exact, values = Fraction(0), []
        for duration in tr['durations']:
            exact += Fraction(float(duration))
            values.append(float(exact))
        return values
    ae, re = ends(actual), ends(planned)
    T = ae[-1]
    times = sorted(set([0., T, *[min(T, max(0., float(row['t']))) for row in c['wind_history']],
        *ae, *map(float, np.linspace(0, T, max(2, math.ceil(T / .02) + 1)))]))
    rt = sorted(set([0., re[-1], *re, *map(float, np.linspace(0, re[-1], max(2, math.ceil(re[-1] / .02) + 1)))]))
    template = dict(root_request=req, runtime=runtime, actual=actual, reference=planned,
        times=dict(actual=times, reference=rt), diagnostic=diagnostic,
        evaluation_deadline_perf=clock.deadline, context_id=req['parent_context_id'], algorithm_sha256=sha(__file__))
    directory = Path(base) / 'parallel_ordered'
    directory.mkdir(parents=True, exist_ok=False)
    jobs = []
    def launch(role):
        body = dict(template, role=role)
        path = directory / (role + '_request.json')
        store(path, packet(body))
        with path.with_name(role + '_stdout.txt').open('x') as out, path.with_name(role + '_stderr.txt').open('x') as err:
            process = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', '-m', MODULE, '--worker', str(path)],
                cwd=ROOT, stdout=out, stderr=err,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        job = OwnedJob(process)
        jobs.append((process, job))
        return body, process
    def collect(body, process):
        while process.poll() is None:
            clock.check()
            time.sleep(.005)
        if process.returncode != 0:
            raise StructuralStop('ORDERED_VALIDATOR_NONZERO:' + body['role'])
        value = read(directory / (body['role'] + '_result.json'))
        clock.check()
        out = verify_result(body, value, clock.deadline)
        expected = {row[0] for row in TASKS[body['role']]}
        if not isinstance(out.get('role_outputs'), dict) or set(out['role_outputs']) != expected:
            raise StructuralStop('ORDERED_PHYSICAL_ROLE_SCHEMA')
        return out
    def checked_role(out, role):
        value = out['role_outputs'][role]
        if value.get('status') == 'DOMAIN_REJECT':
            raise DomainFail(value['detail'])
        if value.get('status') == 'NUMERICAL_UNRESOLVED':
            raise boundaries.BoundaryUnresolved(value['detail'], value.get('evidence'))
        if value.get('status') != 'PASS':
            raise StructuralStop('ORDERED_NUMERICAL_ROLE_SCHEMA')
        return value
    def alternatives(outputs, role, level, needed, q):
        if not needed:
            return []
        answer = []
        for choice in ['LOWER', 'UPPER']:
            job = ('coarse_' if level == 0 else 'fine_' + role + '_') + choice.lower()
            value = checked_role(outputs[job], role)
            if len(value['alternatives']) != 1 or value['alternatives'][0].get('choice') != choice:
                raise StructuralStop('ORDERED_REQUIRED_SENSITIVITY_SCHEMA')
            alternative = value['alternatives'][0]
            if alternative.get('role') != role or alternative.get('level') != level:
                raise StructuralStop('ORDERED_SENSITIVITY_ROLE_SWAP')
            values = checked_samples(alternative['values'], len(q))
            answer.append(dict(**{k:v for k,v in alternative.items() if k != 'values'},
                delta_m=array_helpers.delta(q, values)))
        return answer
    try:
        tasks = {role: launch(role) for role in ROLES}
        began = time.perf_counter()
        af = original.PathReplay(actual, reference.provider, .01)
        aq = checked_samples(array_helpers.samples(af, times), len(times))
        owner_stage = dict(role='actual', inclusive_s=time.perf_counter()-began,
            events=af.engine.events, stats=af.engine.stats, samples_sha256=content(aq))
        store(directory / 'owner_actual_MID_complete.json', owner_stage)
        outputs = {role: collect(*tasks[role]) for role in ROLES}
        primary = checked_role(outputs['fine_reference_primary'], 'reference')
        header = primary.get('dense_material')
        if not isinstance(header, dict) or header.get('data_file') != 'fine_reference_primary_dense.npz':
            raise StructuralStop('ORDERED_DENSE_MATERIAL_SCHEMA')
        began = time.perf_counter()
        rf = dense_material.restore(planned, reference.provider, header,
            directory / 'fine_reference_primary_dense.npz', dense_binding(tasks['fine_reference_primary'][0]), clock)
        rq = checked_samples(array_helpers.samples(rf, rt), len(rt))
        import_s = time.perf_counter()-began
        if rq != checked_samples(primary['primary']['reference'], len(rt)):
            raise StructuralStop('TRANSFERRED_REFERENCE_MID_NOT_BIT_EQUAL')
        guard = checked_role(outputs['fine_reference_guard'], 'reference')
        if rq != checked_samples(guard['primary']['reference'], len(rt)):
            raise StructuralStop('EXTRA_REFERENCE_MID_NOT_BIT_EQUAL')
        coarse_extra, fine_extra, coarse_values, coarse_metadata = [], [], {}, []
        for role, probes in [('actual', times), ('reference', rt)]:
            coarse = checked_role(outputs['coarse_mid'], role)
            if len(coarse['primary_metadata']) != 1:
                raise StructuralStop('ORDERED_COARSE_PRIMARY_METADATA')
            q = checked_samples(coarse['primary'][role], len(probes))
            coarse_values[role] = q
            coarse_metadata += coarse['primary_metadata']
            coarse_extra += alternatives(outputs, role, 0, bool(coarse['primary_metadata'][0]['events']), q)
            fine_path, fine_q = (af, aq) if role == 'actual' else (rf, rq)
            fine_extra += alternatives(outputs, role, 1, bool(fine_path.engine.events), fine_q)
        ad = array_helpers.delta(aq, coarse_values['actual'])
        rd = array_helpers.delta(rq, coarse_values['reference'])
        hd = max([abs(x[2]-y[2]) for x,y in zip(aq,coarse_values['actual'])] +
            [abs(x[2]-y[2]) for x,y in zip(rq,coarse_values['reference'])])
        extra = coarse_extra+fine_extra
        sensitivities = {role:max([0.] + [e['delta_m'] for e in extra if e['role']==role]) for role in ['actual','reference']}
        roundoff = original.POLICY['global_roundoff_ulp_multiplier']*math.ulp(max(1.,float(np.max(np.abs(rf.origin[:2])))))
        uncertainty = original.POLICY['uncertainty_multiplier']*(ad+rd+sensitivities['actual']+sensitivities['reference'])+roundoff
        heading_uncertainty = float(original.POLICY['uncertainty_multiplier']*hd+32*np.finfo(float).eps*max(1.,abs(rf.origin[2])))
        gate = original.progress(af,rf,times,uncertainty,heading_uncertainty)
        return dict(result, **{k:v for k,v in gate.items() if k not in result},
            **{k:gate[k] for k in ['status','complete','completed_segments']},
            replay_representation=dict(actual_control_rows=af.original_control_row_count,
                actual_integration_runs=af.integration_run_count,planned_original_segments=rf.original_control_row_count,
                planned_integration_runs=rf.integration_run_count,all_original_switch_times_probed=True,controls_or_durations_deleted=False),
            boundary_replays=dict(primary=coarse_metadata+[primary_meta(af,'actual',1),primary_meta(rf,'reference',1)],
                event_sensitivity_replays=extra,event_sensitivity_m=sensitivities,no_shared_event_lists=True,empirical_not_rigorous_event_bound=True),
            uncertainty=dict(actual_event_sensitivity_m=sensitivities['actual'],reference_event_sensitivity_m=sensitivities['reference'],
                actual_step_delta_m=ad,reference_step_delta_m=rd,coordinate_roundoff_m=roundoff,
                position_envelope_m=uncertainty,heading_envelope_rad=heading_uncertainty,scope=original.POLICY['numerical_assurance']),
            reason='ALL_ORIGINAL_ORDERED_PREDICATES_WITHIN_ESTIMATED_ENVELOPE' if gate['complete'] else 'ORDERED_PREDICATES_NOT_RESOLVED_OR_FAILED',
            validation_workers_reaped=True,extra_reference_MID_bit_equal=True,
            total_replay_count_including_extra=1+sum(out['replay_started_count'] for out in outputs.values()),
            scheduling_work=dict(owner_stages=[owner_stage],workers={role:dict(stages=out['stages'],setup_s=out['setup_s']) for role,out in outputs.items()}),
            dense_material_work=dict(import_and_samples_s=import_s,export_s=primary['dense_material_export_s'],
                file_sha256=header['data_sha256'],bytes=header['data_bytes'],no_ODE_on_import=True,primary_samples_bit_equal=True))
    except boundaries.BoundaryUnresolved as exc:
        return dict(result,status='NUMERICAL_UNRESOLVED',reason='PARALLEL_BOUNDARY_NUMERICAL_UNRESOLVED',detail=exc.reason,evidence=exc.evidence)
    finally:
        for process,job in jobs:
            job.close()


if __name__ == '__main__':
    worker(sys.argv[-1])
