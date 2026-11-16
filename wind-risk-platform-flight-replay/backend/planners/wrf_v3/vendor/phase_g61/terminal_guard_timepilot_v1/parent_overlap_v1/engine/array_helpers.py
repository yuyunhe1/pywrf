"""Same dense states and scalar norms without repeated small-array setup.

No solution, wind, time, or position cache. All integration jobs and requested
sample times remain unchanged. Unsupported input ordering uses the old path.
"""
import collections
import numpy as np
from phase4t_g61.wrf_feasibility_validation_v1.runtime.ordered import PathReplay,R0

_counts=collections.Counter()
POLICY=dict(version='MONOTONE_DENSE_SAMPLE_AND_SINGLE_CONVERSION_SCALAR_NORMS_V1',
    sample_time_selection='ORIGINAL_SIDE_LEFT_WITH_ALL_DUPLICATES_AND_ENDPOINTS',
    dense_evaluation='ORIGINAL_DENSE_SOLUTION_AT_IDENTICAL_BINARY64_TIME',
    local_coordinate_arithmetic='ORIGINAL_R0_MULTIPLICATIONS_AND_PSI',
    comparison='ORIGINAL_NUMPY_LINALG_NORM_PER_FLOAT64_ROW_AND_PYTHON_MAX',
    wind_queries_changed=False,independent_ODE_jobs_changed=False,value_cache=False)
def reset():_counts.clear()
def stats():return dict(policy=POLICY,counts=dict(_counts))
def old_samples(path,times):return [path.local_at(t).tolist() for t in times]
def old_delta(a,b):return max(float(np.linalg.norm(np.asarray(x)[:2]-np.asarray(y)[:2])) for x,y in zip(a,b))

def samples(path,times):
    items=list(times);_counts['sample_batches']+=1;_counts['requested_samples']+=len(items)
    ts=np.asarray(items)
    eligible=(type(path) is PathReplay and ts.dtype==np.float64 and ts.ndim==1 and
        np.isfinite(ts).all() and len(path.ends)>0 and len(path.sol)==len(path.ends) and
        np.all(np.diff(path.ends)>0) and np.all(np.diff(ts)>=0) and
        (not len(ts) or (ts[0]>=0. and ts[-1]<=path.ends[-1])))
    if not eligible:
        _counts['fallback_samples']+=len(items)
        return old_samples(path,items)
    answer=[];index=0;last=len(path.ends)-1
    for time_value in items:
        # Exact counterpart of searchsorted(..., side='left'). The selected
        # interval contains time_value, so the original clip is an identity.
        while index<last and time_value>path.ends[index]:index+=1
        if not path.starts[index]<=time_value<=path.ends[index]:
            _counts['fallback_samples']+=len(items)
            return old_samples(path,items)
        state=path.sol[index](float(time_value))
        answer.append([float(R0*state[0]),float(R0*state[1]),float(state[2])])
    _counts['fast_samples']+=len(items)
    return answer

def delta(a,b):
    _counts['comparison_batches']+=1
    left=np.asarray(a);right=np.asarray(b)
    eligible=(left.dtype==np.float64 and right.dtype==np.float64 and left.ndim==right.ndim==2
        and left.shape==right.shape and left.shape[1]>=2 and len(left)>0)
    if not eligible:return old_delta(a,b)
    _counts['comparison_rows']+=len(left)
    # Retain each original 1-D norm and its arithmetic, including the original
    # sqrt/dot implementation. Only conversion/subtraction setup is batched.
    difference=left[:,:2]-right[:,:2]
    return max(float(np.linalg.norm(row)) for row in difference)
