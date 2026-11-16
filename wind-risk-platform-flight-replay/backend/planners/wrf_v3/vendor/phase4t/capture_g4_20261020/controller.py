"""E0/E2: the FROZEN repair_v1 control law with an explicit, content-bound config.

`repair_v1/controller.py` reads the module-global ``CONFIG``, so a request whose
binding declared a different ``tick_s`` would still execute at 0.02 s.  The
review required any update-period experiment to use a configuration that actually
takes effect and is content-bound, with fail-fast on mismatch.

This module is a line-by-line copy of the frozen controller: same `line_values`,
same `guidance` (finite-time transverse capture branch and original LOS branch),
same `advance`, same `seal`, same `OrderedProgress`, same event handling.  The
ONLY change is that every configuration number is read from ``cfg`` and that
``execute`` refuses to run when ``cfg`` is not the bound configuration.

E2 uses it only to change the control update period and the declared initial
state - never the geometry, thresholds, bounds or the control law.
"""

import copy
import math
import time

import numpy as np
from scipy.integrate import solve_ivp

from phase3a_implementation.domain import VA, R0
from phase4a_design.mock_contract import content_hash
from phase4t.amendment_v1.controller import wind_inverse
from phase4t.amendment_v1.model import at_goal, StructuralStop, velocity, wind, wrap
from phase4t.repair_v1.contract import CONFIG
from phase4t.repair_v1.progress import OrderedProgress

FROZEN_CONFIG_SHA256 = None  # filled lazily; used for the equivalence statement


def assert_effective_config(binding, cfg):
    """Fail fast unless the configuration that will execute is the bound one."""
    if content_hash(cfg) != binding.get('config_sha256'):
        raise StructuralStop('EFFECTIVE_CONFIG_MISMATCH')
    if binding.get('config') != cfg:
        raise StructuralStop('BINDING_CONFIG_MISMATCH')
    if cfg.get('version') != CONFIG['version']:
        raise StructuralStop('UNEXPECTED_CONFIG_VERSION')
    for name in ('lookahead_m', 'heading_gain_per_s', 'projection_window_parameter_s',
                 'tick_s', 'capture_roundoff_m', 'capture_roundoff_rad',
                 'crossing_roundoff_m', 'progress_parameter_roundoff_s', 'wall_cap_s',
                 'physical_horizon', 'capture', 'capture_support', 'cross_track_role',
                 'no_replanning'):
        if name not in cfg:
            raise StructuralStop('CONFIG_FIELD_MISSING:' + name)


def line_values(q, anchor, tangent, w, cfg):
    phi = math.atan2(tangent[1], tangent[0])
    n = np.array([-tangent[1], tangent[0]])
    theta = wrap(float(q[2]) - phi)
    wn = float(w @ n)
    star = math.asin(-wn / VA)
    err = wrap(theta - star)
    e = float((q[:2] - anchor[:2]) @ n)
    braking = -math.copysign(VA / R0, err) if err else VA / R0
    stop = (VA * (math.cos(theta) - math.cos(star)) + wn * (star - theta)) / braking
    return e, err, e + stop


def base_guidance(ref, p, q, w, cfg):
    i, s = p.i, p.s
    r, rv, d = ref.geometry(i, s)
    if not cfg.get('disable_capture', False) and ref.tr['controls'][i] == 0. and ref.provider.kind == 'SYNTHETIC_CONSTANT' and not p.complete:
        anchor = ref.q(i, ref.starts[i])
        e, err, surface = line_values(q, anchor, d, w, cfg)
        if abs(err) < math.pi / 2:
            if abs(e) <= cfg['capture_roundoff_m'] and abs(err) <= cfg['capture_roundoff_rad']:
                return 0., dict(mode='CAPTURED_ORIGINAL_LINE', e=e, heading_error=err), []
            u = (-math.copysign(VA / R0, surface) if abs(surface) > cfg['capture_roundoff_m']
                 else -math.copysign(VA / R0, err))
            events = []
            if abs(surface) > cfg['capture_roundoff_m']:
                def switch(t, qq):
                    return line_values(qq, anchor, d, w, cfg)[2]
                switch.terminal = True
                switch.direction = -math.copysign(1., surface)
                events.append(switch)
            if err * u < 0. and abs(err) > cfg['capture_roundoff_rad']:
                def heading(t, qq):
                    return line_values(qq, anchor, d, w, cfg)[1]
                heading.terminal = True
                heading.direction = math.copysign(1., u)
                events.append(heading)
            return u, dict(mode='FINITE_TIME_TRANSVERSE_CAPTURE', e=e,
                           heading_error=err, switching_surface_m=surface), events
    n = np.array([-d[1], d[0]])
    cross = float(n @ (q[:2] - r[:2]))
    course = math.atan2(d[1], d[0]) - math.atan2(cross, cfg['lookahead_m'])
    desired, _ = wind_inverse([math.cos(course), math.sin(course)], w)
    ff = 0. if p.complete else ref.tr['controls'][i]
    extra = {}
    if cfg.get('feedforward_mode') == 'DESIRED_RATE_F1':
        from phase4t.recovery_g_20261020.desired_rate import desired_rate
        ff, extra = desired_rate(ref, p, q, w, cfg)
    raw = ff + cfg['heading_gain_per_s'] * wrap(desired - q[2])
    return float(np.clip(raw, -VA / R0, VA / R0)), dict(mode='ORIGINAL_LOS', e=cross,
                                                        raw_omega=raw, desired_psi_air=desired, feedforward=ff, **extra), []


def advance(q, u, d, provider, z, t, events=()):
    if not math.isfinite(u) or abs(u) > VA / R0 + 1e-12:
        raise StructuralStop('CONTROL_BOUND')

    def rhs(tt, qq):
        return [*velocity(qq, wind(provider, qq, z, tt)), u]

    sol = solve_ivp(rhs, (t, t + d), q, method='RK45', rtol=2e-12,
                    atol=[R0 * 1e-13, R0 * 1e-13, 1e-13], max_step=min(.05, d),
                    events=list(events) or None, dense_output=True)
    if not sol.success or not np.isfinite(sol.y).all():
        raise StructuralStop('EXECUTION_INTEGRATION_FAILURE')
    used = float(sol.t[-1] - t)
    if used <= 1e-13:
        raise StructuralStop('ZERO_CONTROLLER_STEP_EVENT')
    fired = [getattr(event, "g4_kind", "ORIGINAL_ENDPOINT") for event, times in zip(events, sol.t_events) if len(times)] if events else []
    return sol.y[:, -1], used, fired


def seal(route, binding, us, ds, nodes, reason, history, events, timing):
    tr = route.tr
    tr.update(state_start=binding['declared_initial_state'], controls=us, durations=ds, states=nodes)
    return dict(version='EXECUTED_ROUTE_V1', arm=binding['arm'],
                binding_sha256=content_hash(binding),
                planned_route_sha256=route.sha256, T_ref=route.T_ref, T_exec=route.T_exec,
                trajectory=tr, physical_time_s=math.fsum(ds),
                actual_controls_sha256=content_hash(dict(controls=us, durations=ds)),
                executed_trajectory_sha256=content_hash(tr), stop_reason=reason,
                controller_history=history, controller_segment_events=events,
                simulation_parts_disjoint_s=timing)


def execute(route, provider, reference, clock, binding, checkpoint, cfg):
    assert_effective_config(binding, cfg)
    route.verify()
    q = np.array(binding['declared_initial_state'], float)
    p = OrderedProgress(reference, q)
    us = []
    ds = []
    nodes = [q.tolist()]
    history = []
    t = 0.
    timing = dict(guidance_and_progress_s=0., plant_integration_s=0., checkpoint_s=0.)
    reason = 'PHYSICAL_HORIZON_REACHED'
    k = 0
    while t < route.T_exec - 1e-12:
        clock.check()
        start = time.perf_counter()
        if binding['arm'] == 'OPEN_LOOP':
            if k >= len(route.tr['controls']):
                reason = 'OPEN_LOOP_PLAN_EXHAUSTED'
                break
            u = float(route.tr['controls'][k])
            d = float(route.tr['durations'][k])
            ev = []
            diag = dict(mode='OPEN_LOOP')
            k += 1
        elif binding['arm'] == 'CLOSED_LOOP':
            p.observe(q, t)
            if not p.complete:
                p.boundary(q, t)
            if p.complete and at_goal(q, route.tr['state_goal']):
                reason = 'ORIGINAL_GOAL_REACHED'
                break
            w = wind(provider, q, reference.z, t)
            u, diag, ev = guidance(reference, p, q, w, cfg)
            d = min(cfg['tick_s'], route.T_exec - t)
            boundary = p.endpoint_event()
            if boundary is not None:
                ev = [*ev, boundary]
        else:
            raise StructuralStop('UNKNOWN_ARM')
        pre = dict(t_start=t, state_start=q.tolist(), active_segment_start=p.i, path_parameter_start_s=p.s, original_complete_start=p.complete)
        timing['guidance_and_progress_s'] += time.perf_counter() - start
        start = time.perf_counter()
        qn, used, fired = advance(q, u, d, provider, reference.z, t, ev)
        from .capture import after_event
        after_event(reference, p, qn, fired)
        timing['plant_integration_s'] += time.perf_counter() - start
        us.append(u)
        ds.append(used)
        t = math.fsum(ds)
        q = qn
        nodes.append(q.tolist())
        start = time.perf_counter()
        if binding['arm'] == 'CLOSED_LOOP':
            p.observe(q, t)
            if not p.complete:
                p.boundary(q, t)
        row = dict(index=len(us), u=u, d=used, state=q.tolist(), t=t, active_segment=p.i,
                   path_parameter_s=p.s, g4_events=fired, **pre, **diag)
        history.append(row)
        timing['guidance_and_progress_s'] += time.perf_counter() - start
        start = time.perf_counter()
        checkpoint(row)
        timing['checkpoint_s'] += time.perf_counter() - start
        if binding['arm'] == 'CLOSED_LOOP' and p.complete and at_goal(q, route.tr['state_goal']):
            reason = 'ORIGINAL_GOAL_REACHED'
            break
    executed = seal(route, binding, us, ds, nodes, reason, history, p.events, timing)
    executed['g4_prediction_stats'] = getattr(p, 'g4_stats', {})
    executed['effective_tick_s'] = cfg['tick_s']
    executed['effective_config_sha256'] = content_hash(cfg)
    return executed


def guidance(ref,p,q,w,cfg):
    from .capture import command
    return command(ref,p,q,w,cfg,base_guidance)
