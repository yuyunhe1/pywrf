"""Derivative-partition root search, never a sampled search grid.

Polynomial isolation is exact only for represented coefficients; transcendental
evaluations and physical inputs remain floating point. No global certification.
"""
import math
import numpy as np
import sympy as sp
from scipy.optimize import brentq

TWO_PI = 2 * math.pi
EPS = np.finfo(float).eps


def unique(values):
    # Do not merge distinct near roots by a broad tolerance.
    return sorted(set(float(x) for x in values))


def partition_roots(fun, points, scale=1.):
    points = unique(points)
    roots, checks, brackets = [], [], []
    roundoff = 128 * EPS * max(1., scale)
    vals = [float(fun(x)) for x in points]
    for x, f in zip(points, vals):
        if abs(f) <= roundoff:
            roots.append(x)
            checks.append({'x': x, 'residual': f, 'kind': 'BOUNDARY_OR_STATIONARY',
                           'roundoff_screen': roundoff})
    for a, b, fa, fb in zip(points[:-1], points[1:], vals[:-1], vals[1:]):
        if fa * fb < 0:
            x = brentq(fun, a, b, xtol=5e-15, rtol=4*EPS, maxiter=200)
            roots.append(float(x))
            brackets.append({'interval': [a, b], 'root': float(x),
                             'residual': float(fun(x)), 'method': 'scipy.brentq'})
    return unique(roots), {'partitions': points, 'partition_values': vals,
                           'critical_checks': checks, 'brackets': brackets,
                           'all_roots_certified': False}


def polynomial_real_roots(coefficients):
    """All real roots of the rational polynomial of IEEE coefficients, with multiplicity."""
    x = sp.Symbol('x')
    poly = sp.Poly(sum(sp.Rational(float(c))*x**i for i,c in enumerate(coefficients)),
                   x, domain=sp.QQ)
    if poly.is_zero:
        return [], {'degeneracy': 'IDENTICALLY_ZERO', 'intervals': []}
    isolated = poly.intervals(eps=sp.Rational(1, 10**24))
    roots = [float((a+b)/2) for (a,b), mult in isolated]
    return roots, {'method': 'sympy.rational_real_root_isolation',
                   'scope': 'represented_polynomial_coefficients_only',
                   'intervals': [{'a': str(a), 'b': str(b), 'multiplicity': int(mult)}
                                 for (a,b),mult in isolated],
                   'all_roots_certified': False}


def trig_roots(constant, sine, cosine, lower, upper):
    amplitude = math.hypot(sine, cosine)
    if amplitude == 0:
        return [], {'degeneracy': 'IDENTICALLY_ZERO' if constant == 0 else 'CONSTANT_NONZERO'}
    ratio = -constant / amplitude
    if abs(ratio) > 1 + 128*EPS:
        return [], {'degeneracy': 'NO_REAL_ROOT'}
    ratio = min(1., max(-1., ratio))
    phase = math.atan2(cosine, sine)
    base = math.asin(ratio)
    roots = []
    for seed in (base-phase, math.pi-base-phase):
        for k in range(math.floor((lower-seed)/TWO_PI)-1,
                       math.ceil((upper-seed)/TWO_PI)+2):
            root = seed + k*TWO_PI
            if lower <= root <= upper:
                roots.append(root)
    return unique(roots), {'method': 'analytic_sinusoid', 'ratio': ratio,
                           'all_roots_certified': False}


def csc_roots(coefficients, lower, upper):
    e1,e2,e3,e4,e5 = coefficients
    def f(h):
        return e5+e1*math.sin(h)+e2*math.cos(h)+h*(e3*math.cos(h)+e4*math.sin(h))
    if e3 == 0 and e4 == 0:
        roots,audit = trig_roots(e5,e1,e2,lower,upper)
        # An identity is a continuum, not a finite enumerated root list.
        return roots, audit
    # H=h+U/V; stationary numerator V^2+C+e5*e3*sin-e5*e4*cos.
    c = e1*e3-e2*e4
    quartic = [e3*e3+c-e5*e4, 4*e3*e4+2*e5*e3,
               -2*e3*e3+4*e4*e4+2*c, -4*e3*e4+2*e5*e3,
               e3*e3+c+e5*e4]
    zs, polynomial_audit = polynomial_real_roots(quartic)
    stationary = [(2*math.atan(z)) % TWO_PI for z in zs]
    poles,_ = trig_roots(0.,e4,e3,lower,upper)
    points = [lower,upper]+[h for h in stationary+[math.pi]+poles if lower<h<upper]
    scale = sum(abs(e) for e in coefficients)*(1+TWO_PI)
    roots, audit = partition_roots(f,points,scale)
    audit.update({'method': 'Zheng_H_derivative_quartic_partition',
                  'coefficients': list(coefficients), 'quartic_ascending': quartic,
                  'stationary_polynomial': polynomial_audit, 'V_zero_checks': poles})
    if polynomial_audit.get('degeneracy') == 'IDENTICALLY_ZERO':
        audit['numerical_unresolved'] = 'H_DERIVATIVE_IDENTITY'
    return roots, audit
