"""Explicit terminal trigger with immutable actual-clock threshold, never human-selected."""
import math

def trigger_kind(case,physical_t,reference_index,position_error,heading_error):
    if not all(math.isfinite(v) and v>=0 for v in (physical_t,position_error,heading_error)):
        raise ValueError('LOCAL_TRIGGER_SCHEMA')
    if position_error>case['trigger']['position_error_m'] or heading_error>case['trigger']['heading_error_rad']:
        return 'ORIGINAL_DEVIATION_THRESHOLD'
    if reference_index==0 and physical_t>=case['local_recovery_policy']['terminal_switch_physical_s']:
        return 'IMMUTABLE_TERMINAL_CONTRACT_SWITCH'
    return None

def authorized_anchor(root,anchor,reference,index,nominal_heading):
    kind=anchor.get('trigger_kind');case=root['case']
    if kind=='ORIGINAL_DEVIATION_THRESHOLD':
        return abs(anchor['actual_state'][2]-nominal_heading)>case['trigger']['heading_error_rad'] or anchor['position_error_m']>case['trigger']['position_error_m']
    if kind!='IMMUTABLE_TERMINAL_CONTRACT_SWITCH' or index!=1 or reference['reference_index']!=0:
        return False
    from ..terminal_policy import authorized
    return (authorized(case,anchor['physical_t'],reference['reference_index']) and
        reference['origin_physical_s']==0. and anchor['physical_t']<case['physical_deadline_s']-8.)
