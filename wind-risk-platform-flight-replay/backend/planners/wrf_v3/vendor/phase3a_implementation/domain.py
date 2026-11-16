"""Unrelaxed inherited horizontal wind domain; no physical reachability claim."""
import math

VA = 10.0
R0 = 8.513422179807296


def wind_domain_validator(wx, wy, *, Va=VA):
    try:
        values = [float(wx), float(wy), float(Va)]
        if not all(math.isfinite(v) for v in values) or values[2] != VA:
            raise ValueError('Frozen Va=10.0 required')
    except (TypeError, ValueError, OverflowError):
        return {'valid': False, 'status': 'INVALID_INPUT', 'beta': None, 'speed_mps': None, 'Va': VA}
    speed = math.hypot(values[0], values[1])
    beta = speed / VA if math.isfinite(speed) else math.hypot(values[0]/VA,values[1]/VA)
    valid = beta < 1.0
    return {'valid': valid, 'status': 'OK' if valid else 'CONNECTOR_OUT_OF_DOMAIN',
            'beta': beta, 'speed_mps': speed if math.isfinite(speed) else None,
            'speed_overflow': not math.isfinite(speed), 'Va': VA, 'rule': 'hypot(wx,wy)/Va < 1; no tolerance',
            'scope': 'INHERITED_CONSTANT_WIND_CONNECTOR_NOT_A_REACHABILITY_PROOF'}
