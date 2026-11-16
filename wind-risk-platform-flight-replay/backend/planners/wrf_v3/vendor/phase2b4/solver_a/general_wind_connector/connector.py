"""Public adapter. No fallback, all four families run unless explicitly selected."""
from ..general import GeneralWindSolver


def state(value):
    return [value['x'], value['y'], value['psi_air']] if isinstance(value, dict) else list(value)


def case_input(state_start, state_goal, wind, parameters):
    w = [wind['wx'], wind['wy']] if isinstance(wind, dict) else list(wind)
    return {'q0': state(state_start), 'qf': state(state_goal), 'wind': w,
            'Va': parameters['Va'], 'R0': parameters['R0']}


class GeneralWindConnector:
    families = ('CSC', 'SC_2PI', 'CC_2PI', 'CCC')

    def connect(self, state_start, state_goal, wind, parameters, *, families=None):
        enabled = self.families if families is None else tuple(families)
        if not enabled or len(set(enabled)) != len(enabled) or any(f not in self.families for f in enabled):
            raise ValueError('Enabled families must be a nonempty unique subset of the four supported families')
        try:
            case = case_input(state_start, state_goal, wind, parameters)
            return GeneralWindSolver(case).run(enabled)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return {'status':'INVALID_INPUT','implementation_status':'FAILED','family':None,'time':None,
                    'trajectory':None,'valid':False,'validation_status':'NOT_VALIDATED',
                    'candidate_count':0,'root_count':0,'rejected_root_count':0,'candidates':[],
                    'family_metrics':{},'error':str(exc),'coverage':{'implemented_families':[],
                    'all_roots_certified':False,'global_optimality_guaranteed':False,'unresolved':[]}}

    __call__ = connect


def connect(state_start, state_goal, wind, parameters, **kwargs):
    return GeneralWindConnector().connect(state_start, state_goal, wind, parameters, **kwargs)


def CC2PI_solver(q0, qf, wind, Va=10., R0=8.513422179807296):
    return connect(q0, qf, wind, {'Va': Va, 'R0': R0}, families=('CC_2PI',))


def CCC_solver(q0, qf, wind, Va=10., R0=8.513422179807296):
    return connect(q0, qf, wind, {'Va': Va, 'R0': R0}, families=('CCC',))


def SC2PI_solver(q0, qf, wind, Va=10., R0=8.513422179807296):
    return connect(q0, qf, wind, {'Va': Va, 'R0': R0}, families=('SC_2PI',))
