def prefix_dynamics(c, trajectory, Stop):
    import numpy as np
    from phase3b.validator import THRESHOLDS
    PT, HT = (8.513422179807296e-06, 1e-07)
    if c['thresholds'] != THRESHOLDS or c['domain_status'] != 'IN_DOMAIN_AT_EVALUATED_POINTS':
        raise Stop('GLOBAL_PREFIX_C_DOMAIN_OR_THRESHOLD')
    if any((f not in ('POSITION_RESIDUAL', 'HEADING_RESIDUAL') for f in c['failures'])):
        raise Stop('GLOBAL_PREFIX_C_MISMATCH:' + str(c['failures']))
    a = np.asarray(trajectory['states'])
    b = np.asarray([r[1:] for r in c['diagnostic_nodes']])
    if a.shape != b.shape or not np.isfinite(a).all() or (not np.isfinite(b).all()) or (np.max(np.linalg.norm(a[:, :2] - b[:, :2], axis=1)) > PT) or (np.max(np.abs(a[:, 2] - b[:, 2])) > HT):
        raise Stop('GLOBAL_PREFIX_ACTUAL_NODE_MISMATCH')
