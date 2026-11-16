"""Fast initialization for V3 free-heading, constant-turn shooting.

The inexpensive solve only initializes the two unknowns. Final nodes are
computed by the unchanged upstream wind-remainder integrator; admission still
requires independent physical replay with the platform's 1 m endpoint policy
and its continuous constraints.
"""
import math
from time import perf_counter
import numpy as np
from scipy.integrate import solve_ivp
from scipy.optimize import least_squares
from phase_g61.uncovered_environments_v1.wind_remainder import integrate,air
from phase3b.integrator import SpatialIntegrationError
from .native import VA, R0


def shoot(startxy, goalxy, provider, z, psi, hint, horizon, check, control=0.):
    startxy, goalxy = np.asarray(startxy), np.asarray(goalxy)
    lower = np.array([psi-math.pi/2, 1e-8])
    upper = np.array([psi+math.pi/2, horizon/hint])
    initial = np.array([psi, min(1., .99*horizon/hint)])
    seed_solves=0

    class SeedReached(Exception):
        def __init__(self,values):self.values=values.copy()

    def seed_residual(values):
        nonlocal seed_solves
        seed_solves+=1
        check()
        duration = float(values[1])*hint

        def rhs(t, displacement):
            xy = startxy + air(float(values[0]),control,float(t)) + R0*displacement
            w = provider.wind_query(float(xy[0]), float(xy[1]), z, float(t))
            if not w.get('valid') or math.hypot(w['wx'], w['wy']) >= VA:
                raise SpatialIntegrationError(w.get('status', 'CONNECTOR_OUT_OF_DOMAIN'))
            return [w['wx']/R0, w['wy']/R0]

        sol = solve_ivp(rhs, (0., duration), [0., 0.], method='DOP853',
                        rtol=1e-8, atol=1e-9, max_step=30.)
        if not sol.success:
            raise ValueError('FREE_HEADING_SEED_INTEGRATION_FAILED')
        residual=(startxy + air(float(values[0]),control,duration) + R0*sol.y[:, -1] - goalxy)/R0
        if np.linalg.norm(residual)*R0<=.02:
            raise SeedReached(values)
        return residual

    started = perf_counter()
    try:
        seed = least_squares(seed_residual, initial, bounds=(lower, upper),
            diff_step=1e-4, ftol=1e-9, xtol=1e-9, gtol=1e-9, max_nfev=12)
        values=seed.x.copy()
        jacobian=seed.jac.copy()
        jacobians=seed.njev
    except SeedReached as reached:
        values=reached.values
        duration=float(values[1])*hint
        displacement=air(float(values[0]),control,duration)
        wind=provider.wind_query(float(goalxy[0]),float(goalxy[1]),z,duration)
        if not wind.get('valid'):
            raise SpatialIntegrationError(wind.get('status','WIND_QUERY_FAILED'))
        heading=float(values[0])+control*duration
        jacobian=np.column_stack(([-displacement[1]/R0,displacement[0]/R0],
            hint*np.array([VA*math.cos(heading)+wind['wx'],VA*math.sin(heading)+wind['wy']])/R0))
        jacobians=None
    best = None
    # Use a Broyden correction to avoid integrating two nearly identical
    # finite-difference trajectories at every strict shooting iteration.
    previous = None
    seed_seconds = perf_counter()-started
    for iteration in range(8):
        check()
        start = np.r_[startxy, values[0]]
        end, nodes = integrate(start, [control], [float(values[1])*hint], provider, z)
        residual = (end[:2]-goalxy)/R0
        error = float(np.linalg.norm(residual))
        if best is None or error < best[0]:
            best = error, start, float(values[1])*hint, nodes
        # Leave ample margin to the user's 1 m endpoint acceptance radius.
        if error*R0 <= .05:
            break
        if previous is not None:
            delta = values-previous[0]
            squared = float(delta@delta)
            if squared > 1e-24:
                jacobian += np.outer(residual-previous[1]-jacobian@delta, delta)/squared
        try:
            correction = np.linalg.solve(jacobian, residual)
        except np.linalg.LinAlgError:
            break
        if not np.isfinite(correction).all():
            break
        previous = values.copy(), residual
        values = np.clip(values-correction, lower+1e-12, upper-1e-12)
    return (*best[1:], {'seed_seconds': seed_seconds, 'seed_evaluations': seed_solves,
        'seed_jacobians': jacobians, 'strict_iterations': iteration+1,
        'strict_seconds': perf_counter()-started-seed_seconds,
        'strict_position_residual_over_R0': best[0]})
