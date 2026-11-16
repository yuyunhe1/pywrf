"""Explicit start-sampled constant-wind wrapper around unchanged Phase2B A/C."""
import copy
import math
from .coordinates import FRAME_ID, HEIGHT_DATUM
from .domain import VA, R0, wind_domain_validator


def _failed(status, detail=None, **extra):
    if extra.get('solver_a',{}).get('valid'):
        extra['diagnostic_solver_a']={'scope':'A_OUTPUT_NOT_ACCEPTED_BY_WRAPPER_C',
                                     'accepted_by_wrapper':False,'output':extra.pop('solver_a')}
    return {'status':status,'valid':False,'family':None,'time':None,'travel_time':None,
            'trajectory':None,'trajectory_2d':None,'validation_status':'NOT_VALIDATED',
            'validation_scope':'START_SAMPLED_CONSTANT_WIND_2D',
            'spatial_wrf_validated':False,'detail':detail,**extra}


class StaticSnapshotConnector:
    def __init__(self, provider, *, z_layer_msl_m):
        self.provider = provider
        self.z = float(z_layer_msl_m)
        if not math.isfinite(self.z):
            raise ValueError('An explicit finite fixed altitude is required')

    def connect(self, state_start, state_goal, parameters, *, frame_id=FRAME_ID, height_datum=HEIGHT_DATUM):
        if frame_id != FRAME_ID or getattr(self.provider,'frame_id',None) != FRAME_ID:
            return _failed('FRAME_MISMATCH')
        if height_datum != HEIGHT_DATUM or getattr(self.provider,'height_datum',None) != HEIGHT_DATUM:
            return _failed('HEIGHT_DATUM_MISMATCH')
        try:
            start,goal = [{k:float(q[k]) for k in ('x','y','z','psi_air')} for q in (state_start,state_goal)]
            va,radius = float(parameters['Va']),float(parameters['R0'])
            if not all(math.isfinite(v) for q in (start,goal) for v in q.values()):
                return _failed('INVALID_INPUT','Nonfinite state')
            if va != VA or radius != R0:
                return _failed('INVALID_INPUT','Frozen Va/R0 mismatch')
            if start['z'] != self.z or goal['z'] != self.z:
                return _failed('UNSUPPORTED_ALTITUDE_CHANGE')
        except (KeyError,TypeError,ValueError,OverflowError) as exc:
            return _failed('INVALID_INPUT',str(exc))
        sample = self.provider.wind_query(start['x'],start['y'],self.z,0.)
        if not sample['valid']:
            return _failed('WIND_QUERY_FAILED',sample['status'],start_query=sample)
        domain = wind_domain_validator(sample['wx'],sample['wy'],Va=va)
        provenance = {'source':self.provider.source,'wind_mode':'FROZEN_START_SAMPLE',
                      'provider_kind':self.provider.kind, 'sample_location':start,
                      'wind_used':[sample['wx'],sample['wy']], 'sampled_wz_mps':sample['wz'],
                      'frame_id':FRAME_ID,'height_datum':HEIGHT_DATUM,'z_layer_msl_m':self.z,
                      'vertical_wind_used':False,'vertical_model':'MODEL_2D_SLICE',
                      'global_optimality_guaranteed':False}
        if not domain['valid']:
            return _failed(domain['status'],start_query=sample,wind_domain=domain,provenance=provenance)
        goal_query = self.provider.wind_query(goal['x'],goal['y'],self.z,0.)
        if not goal_query['valid']:
            return _failed('WIND_QUERY_FAILED','Goal query: '+goal_query['status'],start_query=sample,
                           goal_query=goal_query,wind_domain=domain,provenance=provenance)
        # Imports are lazy so the data adapter does not require solver dependencies.
        from phase2b4.solver_a.general_wind_connector import GeneralWindConnector
        from phase2b4.validator.independent import validate
        q0,qf = [{k:q[k] for k in ('x','y','psi_air')} for q in (start,goal)]
        raw = GeneralWindConnector().connect(q0,qf,{'wx':sample['wx'],'wy':sample['wy']},parameters)
        common = {'solver_a':raw,'start_query':sample,'goal_query':goal_query,
                  'wind_domain':domain,'provenance':provenance}
        if not raw['valid']:
            return _failed(raw['status'],**common)
        # Every exported candidate is independently replayed through unchanged C.
        checks = []
        for candidate in raw['candidates']:
            trajectory = candidate['trajectory']
            c = validate(trajectory,trajectory['controls'],candidate['time'],[sample['wx'],sample['wy']])
            checks.append({'family':candidate['family'],'time':candidate['time'],'C':c})
        if not checks or not all(c['C']['valid'] for c in checks):
            return _failed('C_VALIDATION_FAILED',candidate_C=checks,**common)
        original = raw['trajectory']
        ticks = [0.]
        for duration in original['durations']:
            ticks.append(math.fsum((ticks[-1],duration)))
        lifted = {'type':'piecewise_constant_fixed_altitude','control_interpolation':'ZOH',
                  't':ticks,'states':[[s[0],s[1],self.z,s[2]] for s in original['states']],
                  'controls':copy.deepcopy(original['controls']),'durations':copy.deepcopy(original['durations']),
                  'state_start':[start[k] for k in ('x','y','z','psi_air')],
                  'state_goal':[goal[k] for k in ('x','y','z','psi_air')],
                  'parameters':{'Va':va,'R0':radius},'frame_id':FRAME_ID,'height_datum':HEIGHT_DATUM}
        return {'status':'SUCCESS','valid':True,'family':raw['family'],'time':raw['time'],
                'travel_time':raw['time'],'trajectory':lifted,'trajectory_2d':copy.deepcopy(original),
                'validation_status':'C_VALID_CONSTANT_WIND','validation_scope':'START_SAMPLED_CONSTANT_WIND_2D',
                'spatial_wrf_validated':False,'candidate_C':checks,
                'candidate_count':raw['candidate_count'],'root_count':raw['root_count'],
                'rejected_root_count':raw['rejected_root_count'],**common}

    __call__ = connect
