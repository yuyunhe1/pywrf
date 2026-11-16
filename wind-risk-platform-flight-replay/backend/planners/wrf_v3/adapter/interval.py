"""Outward-rounded interval arithmetic used for fixed-curve certificates."""
import math
import numpy as np


def down(value):
    return float(np.nextafter(value, -math.inf))


def up(value):
    return float(np.nextafter(value, math.inf))


class I:
    __slots__ = ("lo", "hi")
    def __init__(self, lo, hi=None):
        self.lo, self.hi = float(lo), float(lo if hi is None else hi)
        if math.isnan(self.lo) or math.isnan(self.hi) or self.lo > self.hi:
            raise ValueError("INVALID_INTERVAL")
    def __add__(self, other):
        b = as_i(other)
        return I(down(self.lo+b.lo), up(self.hi+b.hi))
    __radd__ = __add__
    def __neg__(self):
        return I(-self.hi, -self.lo)
    def __sub__(self, other):
        return self + -as_i(other)
    def __rsub__(self, other):
        return as_i(other) + -self
    def __mul__(self, other):
        b = as_i(other)
        values = [self.lo*b.lo, self.lo*b.hi, self.hi*b.lo, self.hi*b.hi]
        return I(down(min(values)), up(max(values)))
    __rmul__ = __mul__
    def __truediv__(self, other):
        b = as_i(other)
        if b.lo <= 0 <= b.hi:
            raise ValueError("INTERVAL_ZERO_DENOMINATOR")
        return self * I(down(1/b.hi), up(1/b.lo))
    def __rtruediv__(self, other):
        return as_i(other) / self
    def sq(self):
        return I(max(0., down(0. if self.lo <= 0 <= self.hi else min(self.lo*self.lo, self.hi*self.hi))),
                 up(max(self.lo*self.lo, self.hi*self.hi)))
    def sqrt(self):
        if self.lo < 0:
            raise ValueError("INTERVAL_NEGATIVE_SQRT")
        return I(max(0., down(math.sqrt(self.lo))), up(math.sqrt(self.hi)))
    def union(self, other):
        return I(min(self.lo, other.lo), max(self.hi, other.hi))
    def intersect(self, other):
        return I(max(self.lo, other.lo), min(self.hi, other.hi))
    def absmax(self):
        return max(abs(self.lo), abs(self.hi))


def as_i(value):
    return value if isinstance(value, I) else I(value)


def hypot(a,b):
    # Outward addition of two exact zeros includes the negative subnormal.
    # Intersect with the known nonnegative range of a sum of squares.
    return (a.sq()+b.sq()).intersect(I(0.,math.inf)).sqrt()


class D:
    """Value and partial derivatives with respect to local x, y and UTC."""
    def __init__(self, value, dx=0., dy=0., dt=0.):
        self.v, self.dx, self.dy, self.dt = map(as_i, (value, dx, dy, dt))
    def __add__(self, other):
        b = as_d(other)
        return D(self.v+b.v, self.dx+b.dx, self.dy+b.dy, self.dt+b.dt)
    __radd__ = __add__
    def __neg__(self):
        return D(-self.v, -self.dx, -self.dy, -self.dt)
    def __sub__(self, other):
        return self + -as_d(other)
    def __rsub__(self, other):
        return as_d(other) + -self
    def __mul__(self, other):
        b = as_d(other)
        return D(self.v*b.v, self.dx*b.v+self.v*b.dx,
                 self.dy*b.v+self.v*b.dy, self.dt*b.v+self.v*b.dt)
    __rmul__ = __mul__
    def __truediv__(self, other):
        b = as_d(other)
        square = b.v.sq()
        return D(self.v/b.v, (self.dx*b.v-self.v*b.dx)/square,
                 (self.dy*b.v-self.v*b.dy)/square, (self.dt*b.v-self.v*b.dt)/square)
    def union(self, other):
        return D(*(getattr(self,k).union(getattr(other,k)) for k in ("v","dx","dy","dt")))


def as_d(value):
    return value if isinstance(value, D) else D(value)


def polynomial(coefficients, interval):
    result = I(0.)
    for coefficient in coefficients[::-1]:
        result = result * interval + float(coefficient)
    return result
