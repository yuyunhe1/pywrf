"""Immutable terminal handover policy, including non-S/C route structures.

The switch requests an exact new route. Eligibility never certifies feasibility.
"""
import math

PREPARE_S = 8.0
SUFFIX_S = 1.5  # Existing local predictor horizon, not a fitted parameter.
TICK_S = .02


def derive(route):
    us, ds = route['controls'], route['durations']
    if not us or len(us) != len(ds):
        raise ValueError('TERMINAL_ROUTE_SCHEMA')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
           not math.isfinite(v) for v in us + ds):
        raise ValueError('TERMINAL_ROUTE_NUMERIC_SCHEMA')
    if any(d <= 0 for d in ds) or max(map(abs, us)) > 10 / 8.513422179807296 + 1e-12:
        raise ValueError('TERMINAL_ROUTE_CONTROL_DURATION')
    total = math.fsum(ds)
    supported_sc = len(us) >= 2 and us[-1] != 0. and us[-2] == 0. and ds[-2] >= SUFFIX_S
    if supported_sc:
        handover = math.fsum(ds[:-1]) - SUFFIX_S
        rule = 'PRESERVE_FINAL_STRAIGHT_AND_TURN_SUFFIX'
    else:
        handover = total - SUFFIX_S
        rule = 'PRESERVE_FINAL_GEOMETRIC_SUFFIX_EXACT_CONNECTOR_REQUIRED'
    trigger = handover - PREPARE_S
    if trigger < 0:
        raise ValueError('INSUFFICIENT_PLANNED_PREFIX_FOR8S_PREPARE')
    return dict(rule=rule, trigger_physical_s=trigger,
                expected_nominal_handover_physical_s=handover,
                prepare_s=PREPARE_S, original_total_s=total,
                original_suffix_reserved_s=total-handover,
                minimum_suffix_after_one_tick_late_s=total-handover-TICK_S,
                retains_original_SC_rule_when_available=supported_sc,
                uses_immutable_planned_geometry_only=True,
                arbitrary_timestamp_or_seed_search=False,
                exact_BVP_and_full_independent_admission_still_required=True)


def authorized(case, t, index):
    expected = derive(case['route'])
    policy = case['local_recovery_policy']
    return (index == 0 and policy['terminal_switch_rule'] == expected['rule'] and
            policy['terminal_switch_physical_s'] == expected['trigger_physical_s'] and
            expected['trigger_physical_s'] <= t <= expected['trigger_physical_s'] + TICK_S + 1e-9)
