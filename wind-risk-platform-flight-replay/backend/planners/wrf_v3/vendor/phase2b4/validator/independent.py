"""Independent physical ODE replay. No A formula or B propagation imports.

Public input is trajectory (boundary states/model), control, time, and wind.
All stored nodes, including piecewise-constant A nodes, are checked by default.
"""
import math
import numpy as np
from scipy.integrate import solve_ivp

POSITION_TOL = 1e-6
HEADING_TOL = 1e-7
CONTROL_TOL = 1e-8
INTEGRATION_RTOL = 1e-11


def failure(reason, detail=None):
    return {'position_error': None, 'heading_error': None, 'control_violation': None,
            'valid': False, 'validation_status': 'FAILED', 'terminal_residual': None,
            'constraint_residual': None, 'failures': [reason], 'detail': detail,
            'total_time_s': None, 'final_state': None, 'nfev': 0}


def validate(trajectory, control, time, wind):
    if not isinstance(trajectory, dict):
        return failure('MISSING_TRAJECTORY')
    try:
        q0 = np.asarray(trajectory['state_start'], dtype=float)
        qf = np.asarray(trajectory['state_goal'], dtype=float)
        va = float(trajectory['parameters']['Va'])
        radius = float(trajectory['parameters']['R0'])
        w = np.asarray(wind, dtype=float)
        total = float(time)
        controls = np.asarray(control, dtype=float)
        if controls.ndim != 1 or q0.shape != (3,) or qf.shape != (3,) or w.shape != (2,):
            return failure('SHAPE_ERROR')
        if va != 10. or radius <= 0.:
            return failure('MODEL_SCOPE')
        if trajectory.get('control_interpolation') != 'ZOH':
            return failure('CONTROL_INTERPOLATION')
        if trajectory['type'] == 'piecewise_constant':
            durations = np.asarray(trajectory['durations'], dtype=float)
            nodes = np.asarray(trajectory['states'], dtype=float) if 'states' in trajectory else None
        elif trajectory['type'] == 'discretized':
            ticks = np.asarray(trajectory['t'], dtype=float)
            if ticks.ndim != 1 or len(ticks) != len(controls) + 1 or ticks[0] != 0.:
                return failure('TIME_SHAPE')
            durations = np.diff(ticks)
            nodes = np.asarray(trajectory['X'], dtype=float).T
        else:
            return failure('TRAJECTORY_TYPE')
        if durations.ndim != 1 or len(durations) != len(controls):
            return failure('SEGMENT_SHAPE')
        if nodes is not None and nodes.shape != (len(controls)+1, 3):
            return failure('NODE_SHAPE')
        arrays = [q0, qf, w, controls, durations, np.array([va, radius, total])]
        if nodes is not None:
            arrays.append(nodes)
        if not all(np.isfinite(a).all() for a in arrays):
            return failure('NONFINITE_DATA')
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        return failure('MALFORMED_TRAJECTORY', str(exc))
    if total < 0. or np.any(durations < 0.):
        return failure('NEGATIVE_DURATION')
    # A reporting consistency check; no feasibility threshold is changed.
    summed = math.fsum(durations)
    if abs(total - summed) > 64*np.finfo(float).eps*max(1., total, summed):
        return failure('TIME_MISMATCH')
    umax = va/radius
    normalized_violation = float(max(0., np.max(np.abs(controls)/umax-1., initial=0.)))
    violation = float(max(0., np.max(np.abs(controls)-umax, initial=0.)))
    errors = ['CONTROL_BOUND'] if normalized_violation > CONTROL_TOL else []
    state, nfev = q0.copy(), 0
    node_error = float(np.linalg.norm((nodes[0]-state)/[radius, radius, 1.])) if nodes is not None else 0.
    for i, (u, duration) in enumerate(zip(controls, durations)):
        if duration > 0.:
            def ode(t, q):
                return [va*math.cos(q[2])+w[0], va*math.sin(q[2])+w[1], u]
            try:
                out = solve_ivp(ode, (0., float(duration)), state, method='RK45',
                                rtol=INTEGRATION_RTOL, atol=[radius*1e-12, radius*1e-12, 1e-12])
            except (ValueError, RuntimeError, OverflowError) as exc:
                return failure('INTEGRATION_FAILURE', str(exc))
            if not out.success or not np.isfinite(out.y[:, -1]).all():
                return failure('INTEGRATION_FAILURE', out.message)
            state = out.y[:, -1]
            nfev += out.nfev
        if nodes is not None:
            node_error = max(node_error, float(np.linalg.norm((nodes[i+1]-state)/[radius, radius, 1.])))
    position_m = float(np.linalg.norm(state[:2]-qf[:2]))
    position = position_m/radius
    heading = abs(math.atan2(math.sin(state[2]-qf[2]), math.cos(state[2]-qf[2])))
    if position > POSITION_TOL:
        errors.append('POSITION_RESIDUAL')
    if heading > HEADING_TOL:
        errors.append('HEADING_RESIDUAL')
    if node_error > POSITION_TOL:
        errors.append('INTERIOR_NODE_RESIDUAL')
    return {'position_error': position_m, 'heading_error': heading, 'control_violation': violation,
            'valid': not errors, 'validation_status': 'PASSED' if not errors else 'FAILED',
            'terminal_residual': {'position_over_R0': position, 'heading_rad': heading,
                                  'ground_position_m': (state[:2]-qf[:2]).tolist()},
            'constraint_residual': {'control_over_umax': normalized_violation,
                                    'negative_duration_s': 0., 'interior_node_scaled': node_error},
            'thresholds': {'position_over_R0': POSITION_TOL, 'heading_rad': HEADING_TOL,
                           'control_over_umax': CONTROL_TOL, 'interior_node_scaled': POSITION_TOL},
            'units': {'position_error': 'm', 'heading_error': 'rad', 'control_violation': 'rad/s'},
            'final_state': state.tolist(), 'total_time_s': summed, 'nfev': nfev, 'failures': errors}


def validate_case(case, trajectory, check_nodes=True):
    """Input adapter only; the public C kernel receives no solver objects."""
    if trajectory is None:
        return failure('MISSING_TRAJECTORY')
    enriched = {**trajectory, 'state_start': case['q0'], 'state_goal': case['qf'],
                'parameters': {'Va': case['Va'], 'R0': case['R0']}}
    try:
        if trajectory['type'] == 'piecewise_constant':
            control = trajectory['controls']
            total = math.fsum(trajectory['durations'])
        elif trajectory['type'] == 'discretized':
            control = np.asarray(trajectory['U'], dtype=float).reshape(-1).tolist()
            total = trajectory['t'][-1]
        else:
            return failure('TRAJECTORY_TYPE')
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        return failure('MALFORMED_TRAJECTORY', str(exc))
    # Legacy trajectories lacking the explicit label are ZOH by contract.
    enriched.setdefault('control_interpolation', 'ZOH')
    return validate(enriched, control, total, case['wind'])
