"""Explicit historical mission roles; a one-group role cannot satisfy two-group evidence."""
import copy,math
from .io_core import content

ROLES={
 'ORIGINAL_SINGLE_PULSE_V1':dict(role_id='ORIGINAL_SINGLE_PULSE_V1',minimum_committed_groups=1,maximum_committed_groups=2,maximum_attempts_per_group=2,minimum_initial_route_time_s=None),
 'EARLY_CAUSAL_RESTART_V1':dict(role_id='EARLY_CAUSAL_RESTART_V1',minimum_committed_groups=1,maximum_committed_groups=2,maximum_attempts_per_group=2,minimum_initial_route_time_s=None),
 'TWO_FLIGHT_DEVIATIONS_V1':dict(role_id='TWO_FLIGHT_DEVIATIONS_V1',minimum_committed_groups=2,maximum_committed_groups=2,maximum_attempts_per_group=2,minimum_initial_route_time_s=35.),
 'TWO_OBSTACLE_DEVIATIONS_V1':dict(role_id='TWO_OBSTACLE_DEVIATIONS_V1',minimum_committed_groups=2,maximum_committed_groups=2,maximum_attempts_per_group=2,minimum_initial_route_time_s=20.),
}
_DEFINITIONS_SHA256=content(ROLES)

def spec(role_id):
    if content(ROLES)!=_DEFINITIONS_SHA256:raise ValueError('ROLE_DEFINITION_MUTATED')
    if not isinstance(role_id,str) or role_id not in ROLES:raise ValueError('UNREGISTERED_MISSION_ROLE')
    return copy.deepcopy(ROLES[role_id])

def validate_input(case):
    role=case.get('mission_role')
    if not isinstance(role,dict) or content(role)!=content(spec(role.get('role_id'))):raise ValueError('MISSION_ROLE_SPEC_MISMATCH')
    if case.get('initial_time_arm')!='F0' or type(case.get('initial_reserve_fraction')) not in (int,float) or case['initial_reserve_fraction']!=.9:
        raise ValueError('COMMON_ENTRY_F0_RESERVE_ONLY')
    return role

def validate_root(root,pipeline):
    role=validate_input(root['case']);original=validate_input(pipeline['case'])
    if role!=original:raise ValueError('MISSION_ROLE_CHANGED_AFTER_INITIAL_REQUEST')
    if type(root['case'].get('required_committed_groups')) is not int or root['case']['required_committed_groups']!=role['minimum_committed_groups']:
        raise ValueError('REGISTERED_GROUP_REQUIREMENT_CHANGED')
    if not eligibility(root['case'],root['case']['T_ref_s']):raise ValueError('REGISTERED_MISSION_ROLE_NOT_ELIGIBLE')
    return role

def groups_pass(case,transactions):
    role=validate_input(case)
    if not isinstance(transactions,list) or not role['minimum_committed_groups']<=len(transactions)<=role['maximum_committed_groups']:return False
    return all(isinstance(row,dict) and type(row.get('group_index')) is int and row['group_index']==i for i,row in enumerate(transactions,1))

def eligibility(case,total):
    role=validate_input(case)
    if isinstance(total,bool) or not isinstance(total,(int,float)) or not math.isfinite(total) or total<=0:raise ValueError('INITIAL_ROLE_TIME_SCHEMA')
    minimum=role['minimum_initial_route_time_s']
    return minimum is None or total>=minimum

def private_input(mission,terminal):
    role=validate_input(mission['case'])['role_id']
    if role=='ORIGINAL_SINGLE_PULSE_V1':pairs=[(.5,.05)]
    elif role=='EARLY_CAUSAL_RESTART_V1':pairs=[(.5,.1),(1.6,.05)]
    elif role=='TWO_FLIGHT_DEVIATIONS_V1':pairs=[(.5,.1),(25.,.1)]
    elif role=='TWO_OBSTACLE_DEVIATIONS_V1':pairs=[(.5,.1),(10.,.1)]
    return dict(version='PRIVATE_FROZEN_ACTUATOR_PULSES_V1',faults=[dict(start_physical_s=t,duration_s=d) for t,d in pairs])
