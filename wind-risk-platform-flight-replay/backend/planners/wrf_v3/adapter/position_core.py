"""Offline V3 turn/coast shooting with a continuous prescribed start heading.

    This is a planning candidate family, with the original wind-remainder
    integrator and independent C replay. No flight controller is invoked.
"""
import math
import numpy as np
from scipy.integrate import solve_ivp
from scipy.optimize import least_squares
from phase_g61.uncovered_environments_v1.wind_remainder import integrate, air
from phase3b.integrator import SpatialIntegrationError
from .native import VA, R0
from .validation import validate_route


def connect(start, goalxy, provider, z, check):
    start, goalxy = np.asarray(start, float), np.asarray(goalxy, float)
    displacement = goalxy - start[:2]
    length = float(np.linalg.norm(displacement))
    tangent = displacement / length
    normal = np.array([-tangent[1], tangent[0]])
    wind = provider.wind_query(*start[:2], z, 0.)
    if not wind.get("valid"):
        raise SpatialIntegrationError(wind.get("status"))
    vector = np.array([wind["wx"], wind["wy"]])
    cross = float(vector @ normal)
    if np.linalg.norm(vector) >= VA or abs(cross) >= VA:
        raise SpatialIntegrationError("CONNECTOR_OUT_OF_DOMAIN")
    desired = math.atan2(tangent[1], tangent[0]) - math.asin(cross / VA)
    theta = (desired - start[2] + math.pi) % (2 * math.pi) - math.pi
    hint = length / (float(vector @ tangent) + math.sqrt(VA * VA - cross * cross))
    horizon = provider.times[-1] - provider.epoch - getattr(provider, "offset", 0.)
    umax = .9 * VA / R0
    lower, upper = np.array([-math.pi, 1e-8]), np.array([math.pi, horizon / hint])
    values = np.array([theta, min(1., .99 * horizon / hint)])
    if upper[1] <= lower[1]:
        raise ValueError("WRF 未覆盖连接航程")

    def controls(q):
        turn = abs(float(q[0])) / umax
        return ([math.copysign(umax, float(q[0])), 0.], [turn, float(q[1]) * hint])

    def coarse(q):
        check()
        control, durations = controls(q)
        state, elapsed = start.copy(), 0.
        for u, duration in zip(control, durations):
            if duration <= 0:
                continue
            origin = state.copy()
            def rhs(t, remainder):
                xy = origin[:2] + air(origin[2], u, float(t) - elapsed) + R0 * remainder
                w = provider.wind_query(*xy, z, float(t))
                if not w.get("valid") or math.hypot(w["wx"], w["wy"]) >= VA:
                    raise SpatialIntegrationError(w.get("status", "CONNECTOR_OUT_OF_DOMAIN"))
                return [w["wx"] / R0, w["wy"] / R0]
            sol = solve_ivp(rhs, (elapsed, elapsed + duration), [0., 0.], method="DOP853", rtol=1e-8, atol=1e-9, max_step=20.)
            if not sol.success:
                raise ValueError("TURN_COAST_INITIALIZATION_FAILED")
            state[:2] = origin[:2] + air(origin[2], u, duration) + R0 * sol.y[:, -1]
            state[2] = origin[2] + u * duration
            elapsed += duration
        return (state[:2] - goalxy) / R0

    class Reached(Exception):
        def __init__(self, q):
            self.q = q.copy()
    def residual(q):
        value = coarse(q)
        if np.linalg.norm(value) * R0 < .02:
            raise Reached(q)
        return value
    try:
        seed = least_squares(residual, values, bounds=(lower, upper), diff_step=1e-4,
                             max_nfev=16, ftol=1e-9, xtol=1e-9, gtol=1e-9)
        values, jacobian = seed.x.copy(), seed.jac.copy()
    except Reached as seed:
        values = seed.q
        baseline = coarse(values)
        jacobian = np.column_stack([(coarse(values + [1e-4, 0.]) - baseline) / 1e-4,
                                    (coarse(values + [0., 1e-4]) - baseline) / 1e-4])
    previous = None
    for _ in range(8):
        check()
        control, durations = controls(values)
        end, nodes = integrate(start, control, durations, provider, z)
        value = (end[:2] - goalxy) / R0
        if np.linalg.norm(value) * R0 <= .05:
            break
        if previous is not None:
            delta = values - previous[0]
            if float(delta @ delta) > 1e-24:
                jacobian += np.outer(value - previous[1] - jacobian @ delta, delta) / float(delta @ delta)
        previous = values.copy(), value
        try:
            correction = np.linalg.solve(jacobian, value)
        except np.linalg.LinAlgError:
            break
        values = np.clip(values - correction, lower + 1e-10, upper - 1e-10)
    route = {"type": "piecewise_constant", "control_interpolation": "ZOH", "state_start": start.tolist(),
             "state_goal": [*goalxy.tolist(), float(end[2])], "parameters": {"Va": VA, "R0": R0},
             "z_layer_msl_m": z, "controls": control, "durations": durations, "states": nodes,
             "frame_id": provider.frame_id, "height_datum": provider.height_datum,
             "platform_initialization": "START_HEADING_CONTINUOUS_TURN_COAST_CORE"}
    receipt = validate_route(route, control, math.fsum(durations), provider)
    if not receipt["valid"]:
        raise ValueError("V3 转弯/直飞连接未通过独立验收：" + str(receipt["failures"]))
    return route, receipt
