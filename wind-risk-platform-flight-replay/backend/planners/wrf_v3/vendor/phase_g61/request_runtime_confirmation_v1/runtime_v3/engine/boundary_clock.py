"""Copied into the new namespace before freeze; pure, no ODE or tolerances."""
import math
from bisect import bisect_right

def absolute_ends(origin,local_ends):
    if not math.isfinite(origin) or origin<0:raise ValueError('BOUNDARY_ORIGIN')
    result=[];previous=origin
    for end in local_ends:
        value=math.fsum((origin,float(end)))
        if not math.isfinite(value) or value<=previous:
            raise ValueError('BOUNDARY_NOT_REPRESENTABLE_NO_SEGMENT_DELETION')
        result.append(value);previous=value
    if not result:raise ValueError('EMPTY_BOUNDARY_SCHEDULE')
    return tuple(result)

def active_index(ends,now):
    if not math.isfinite(now):raise ValueError('BOUNDARY_QUERY_NONFINITE')
    i=bisect_right(ends,now)
    if i>=len(ends):raise ValueError('NO_ACTIVE_SEGMENT_AFTER_COMPLETION')
    return i
