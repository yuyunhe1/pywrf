"""Lossless fixed-implementation dense-output transport; no pickle and no ODE.

Only registered continuous wrappers, SciPy1.15.3 OdeSolution and original
Dop853DenseOutput numeric arrays are accepted. Actual/reference never share
integration starts or event detection. Imported arrays are evaluation material.
"""
import copy
import inspect
import math
import os
import tempfile
import types
import zipfile
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import scipy
from scipy.integrate import OdeSolution
from scipy.integrate._ivp import rk, common as scipy_common
from scipy.integrate._ivp.rk import Dop853DenseOutput
from phase4t_g61.wrf_feasibility_validation_v1.runtime import ordered
from phase_g61.dynamic_wind.partition import PartitionReplay as TemporalPartition
from phase_g61.uncovered_environments_v1.committed_events import integrate as committed
from phase4t.amendment_v1.model import StructuralStop
from .work import D, read, sha, content, CODE

MAX_BYTES = 64 * 1024 * 1024
PATH = ordered.PathReplay
_DENSE_CODE = Dop853DenseOutput._call_impl.__code__
_ODE_CODE = OdeSolution._call_single.__code__
_REGISTERED = {}
for _kind, _function, _field in [('temporal', TemporalPartition.integrate, 'segments'), ('pieces', committed, 'pieces')]:
    _codes = [c for c in _function.__code__.co_consts if isinstance(c, types.CodeType) and c.co_name == 'dense']
    assert len(_codes) == 1
    _REGISTERED[_codes[0]] = (_kind, _field)


def guard():
    identity = read(CODE / 'resources/PINNED_SOURCE_IDENTITY.json')
    if scipy.__version__ != '1.15.3' or Dop853DenseOutput._call_impl.__code__ is not _DENSE_CODE or OdeSolution._call_single.__code__ is not _ODE_CODE:
        raise StructuralStop('DENSE_IMPLEMENTATION_SUBSTITUTION')
    if sha(inspect.getsourcefile(rk)) != identity['local_rk_sha256'] or sha(inspect.getsourcefile(scipy_common)) != identity['ode_solution_file_sha256']:
        raise StructuralStop('DENSE_PINNED_SOURCE_CHANGED')
    return dict(rk=identity['local_rk_sha256'], ode_solution=identity['ode_solution_file_sha256'])


def bounds(tr):
    exact, ends, groups, mapping = Fraction(0), [], [], []
    for u, duration in zip(tr['controls'], tr['durations']):
        start = exact
        exact += Fraction(float(duration))
        ends.append(float(exact))
        if groups and float(u).hex() == float(groups[-1][2]).hex():
            groups[-1] = (groups[-1][0], float(exact), u)
        else:
            groups.append((float(start), float(exact), u))
        mapping.append(len(groups) - 1)
    return np.asarray(ends), groups, mapping


def export(path, target, binding, clock):
    clock.check()
    source = guard()
    if type(path) is not PATH:
        raise StructuralStop('UNREGISTERED_DENSE_PATH')
    target = Path(target).resolve()
    if not target.is_relative_to(D) or target.exists():
        raise StructuralStop('DENSE_OUTPUT_SCOPE_OR_OVERWRITE')
    ends, groups, expected_mapping = bounds(path.tr)
    if not np.array_equal(ends, path.ends) or binding['trajectory_sha256'] != content(path.tr):
        raise StructuralStop('DENSE_EXPORT_TRAJECTORY_BINDING')
    arrays, intervals, active = {}, [], set()
    def encode(fun, depth=0):
        if depth > 12 or id(fun) in active:
            raise StructuralStop('DENSE_CALLABLE_DEPTH_OR_CYCLE')
        active.add(id(fun))
        try:
            if type(fun) is OdeSolution:
                if not fun.ascending or fun.side != 'left' or len(fun.interpolants) != len(fun.ts) - 1:
                    raise StructuralStop('UNREGISTERED_ODE_SOLUTION_CONVENTION')
                index = len(arrays)
                key = 'ts_' + str(index)
                arrays[key] = np.array(fun.ts, dtype=np.float64, copy=True)
                offset = len(intervals)
                for i, interpolation in enumerate(fun.interpolants):
                    if type(interpolation) is not Dop853DenseOutput or interpolation.F.shape != (7, 3) or interpolation.y_old.shape != (3,):
                        raise StructuralStop('UNREGISTERED_DOP853_INTERPOLANT')
                    if interpolation.t_old != fun.ts[i] or interpolation.t != fun.ts[i + 1]:
                        raise StructuralStop('DENSE_INTERPOLANT_TIME_GRID')
                    intervals.append(interpolation)
                return dict(kind='ode', ts_key=key, offset=offset, count=len(fun.interpolants))
            if not isinstance(fun, types.FunctionType) or fun.__code__ not in _REGISTERED:
                raise StructuralStop('UNREGISTERED_DENSE_CALLABLE')
            kind, field = _REGISTERED[fun.__code__]
            values = dict(zip(fun.__code__.co_freevars, [c.cell_contents for c in fun.__closure__]))
            pieces = values[field]
            return dict(kind=kind, starts=[float(p['a']) for p in pieces], ends=[float(p['b']) for p in pieces],
                children=[encode(p['dense'] if kind == 'temporal' else p['sol'], depth + 1) for p in pieces])
        finally:
            active.remove(id(fun))
    unique, trees, mapping = {}, [], []
    for fun in path.sol:
        if id(fun) not in unique:
            unique[id(fun)] = len(trees)
            trees.append(encode(fun))
        mapping.append(unique[id(fun)])
    if mapping != expected_mapping or len(trees) != len(groups):
        raise StructuralStop('DENSE_ORIGINAL_CONTROL_MAPPING')
    if not intervals:
        raise StructuralStop('DENSE_EMPTY_MATERIAL')
    arrays.update(F=np.stack([p.F for p in intervals]), y0=np.stack([p.y_old for p in intervals]),
        lo=np.asarray([p.t_old for p in intervals]), hi=np.asarray([p.t for p in intervals]))
    if any(a.dtype != np.float64 or not np.isfinite(a).all() for a in arrays.values()):
        raise StructuralStop('DENSE_NONFINITE_OR_DTYPE')
    if sum(a.nbytes for a in arrays.values()) > MAX_BYTES:
        raise StructuralStop('DENSE_MATERIAL_SIZE_LIMIT')
    schema = {k: dict(dtype=a.dtype.str, shape=list(a.shape), sha256=__import__('hashlib').sha256(a.tobytes()).hexdigest()) for k, a in arrays.items()}
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + target.name + '.', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            np.savez(stream, **arrays)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        os.unlink(temporary)
        digest = sha(target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    clock.check()
    if guard() != source:
        raise StructuralStop('DENSE_SOURCE_CHANGED_DURING_EXPORT')
    return dict(schema='LOSSLESS_PINNED_DOP853_DENSE_V1', binding=copy.deepcopy(binding), source=source,
        data_file=target.name, data_sha256=digest, data_bytes=target.stat().st_size,
        arrays=schema, trees=trees, mapping=mapping, origin=path.origin.tolist(),
        original_rows=path.original_control_row_count, integration_runs=path.integration_run_count,
        step_s=path.step, engine=dict(events=path.engine.events, contacts=path.engine.contacts,
            stats=path.engine.stats, pieces=path.engine.pieces))


def restore(tr, provider, header, target, binding, clock):
    clock.check()
    fields = {'schema','binding','source','data_file','data_sha256','data_bytes','arrays','trees','mapping','origin','original_rows','integration_runs','step_s','engine'}
    if not isinstance(header, dict) or set(header) != fields or not isinstance(header['arrays'], dict) or not isinstance(header['trees'], list):
        raise StructuralStop('DENSE_HEADER_SCHEMA')
    if header.get('schema') != 'LOSSLESS_PINNED_DOP853_DENSE_V1' or header.get('binding') != binding or binding.get('trajectory_sha256') != content(tr):
        raise StructuralStop('DENSE_IMPORT_BINDING')
    if header.get('source') != guard():
        raise StructuralStop('DENSE_IMPORT_SOURCE')
    target = Path(target).resolve()
    if not target.is_relative_to(D) or target.name != header.get('data_file') or not target.is_file():
        raise StructuralStop('DENSE_INPUT_SCOPE')
    if not 0 < target.stat().st_size <= MAX_BYTES or target.stat().st_size != header.get('data_bytes') or sha(target) != header.get('data_sha256'):
        raise StructuralStop('DENSE_FILE_HASH_OR_SIZE')
    try:
        with zipfile.ZipFile(target) as archive:
            if sum(x.file_size for x in archive.infolist()) > MAX_BYTES or len(archive.namelist()) != len(set(archive.namelist())):
                raise StructuralStop('DENSE_ARCHIVE_SIZE_OR_DUPLICATES')
        with np.load(target, allow_pickle=False, max_header_size=10000) as saved:
            if set(saved.files) != set(header['arrays']):
                raise StructuralStop('DENSE_ARRAY_SET')
            arrays = {k: np.array(saved[k], copy=True) for k in saved.files}
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        raise StructuralStop('DENSE_UNSAFE_OR_INVALID_NUMERICAL_ARCHIVE') from exc
    for key, array in arrays.items():
        info = header['arrays'][key]
        if array.dtype.str != info['dtype'] or array.dtype != np.float64 or list(array.shape) != info['shape'] or not np.isfinite(array).all():
            raise StructuralStop('DENSE_ARRAY_SCHEMA')
        if __import__('hashlib').sha256(array.tobytes()).hexdigest() != info['sha256']:
            raise StructuralStop('DENSE_ARRAY_HASH')
        array.setflags(write=False)
    if set(['F', 'y0', 'lo', 'hi']) - set(arrays):
        raise StructuralStop('DENSE_REQUIRED_ARRAYS')
    n = len(arrays['lo'])
    if arrays['F'].shape != (n, 7, 3) or arrays['y0'].shape != (n, 3) or arrays['hi'].shape != (n,) or np.any(arrays['hi'] <= arrays['lo']):
        raise StructuralStop('DENSE_COEFFICIENT_SHAPES_OR_TIME')
    ranges = {}
    def decode(node, depth=0):
        if not isinstance(node, dict) or depth > 12:
            raise StructuralStop('DENSE_DESCRIPTOR_SCHEMA')
        if node.get('kind') == 'ode':
            offset, count = node.get('offset'), node.get('count')
            if type(offset) is not int or type(count) is not int or not 0 <= offset < n or not 0 < count <= n - offset:
                raise StructuralStop('DENSE_DESCRIPTOR_RANGE')
            if node.get('ts_key') not in arrays:
                raise StructuralStop('DENSE_DESCRIPTOR_UNKNOWN_ARRAY')
            ts = arrays[node['ts_key']]
            if ts.shape != (count + 1,) or not np.all(np.diff(ts) > 0) or not np.array_equal(ts[:-1], arrays['lo'][offset:offset + count]) or not np.array_equal(ts[1:], arrays['hi'][offset:offset + count]):
                raise StructuralStop('DENSE_DESCRIPTOR_TIME_GRID')
            interpolants = [Dop853DenseOutput(arrays['lo'][i], arrays['hi'][i], arrays['y0'][i], arrays['F'][i]) for i in range(offset, offset + count)]
            result = OdeSolution(ts, interpolants, alt_segment=False)
            ranges[id(result)] = (float(ts[0]), float(ts[-1]))
            return result
        if node.get('kind') not in ('temporal', 'pieces'):
            raise StructuralStop('DENSE_DESCRIPTOR_UNKNOWN_KIND')
        if set(node) != {'kind','starts','ends','children'}:
            raise StructuralStop('DENSE_DESCRIPTOR_WRAPPER_SCHEMA')
        starts, ends, children = node['starts'], node['ends'], node['children']
        if not len(starts) == len(ends) == len(children) or not starts:
            raise StructuralStop('DENSE_DESCRIPTOR_PIECES')
        if not all(math.isfinite(a) and math.isfinite(b) and a < b for a, b in zip(starts, ends)) or any(a != b for a, b in zip(starts[1:], ends[:-1])):
            raise StructuralStop('DENSE_DESCRIPTOR_CONTINUITY')
        functions = [decode(child, depth + 1) for child in children]
        if any(ranges[id(fun)] != (a, b) for fun, a, b in zip(functions, starts, ends)):
            raise StructuralStop('DENSE_WRAPPER_CHILD_INTERVAL')
        def dense(t):
            index = min(int(np.searchsorted(ends, t, side='left')), len(ends) - 1)
            return functions[index](float(t))
        ranges[id(dense)] = (starts[0], ends[-1])
        return dense
    ends, groups, expected_mapping = bounds(tr)
    if header.get('mapping') != expected_mapping or header.get('origin') != tr['state_start'] or header.get('original_rows') != len(tr['durations']) or header.get('integration_runs') != len(groups):
        raise StructuralStop('DENSE_ORIGIN_OR_ROW_MAPPING')
    if header.get('step_s') != .01 or len(header['trees']) != len(groups):
        raise StructuralStop('DENSE_IMPORT_STEP_OR_RUNS')
    solutions = [decode(tree) for tree in header['trees']]
    if any(ranges[id(fun)] != (a, b) for fun, (a, b, u) in zip(solutions, groups)):
        raise StructuralStop('DENSE_CONTROL_RUN_TIME_INTERVAL')
    result = object.__new__(PATH)
    result.tr = tr
    result.provider = ordered.fresh(provider)
    result.origin = np.array(tr['state_start'], float)
    result.z = tr['z_layer_msl_m']
    result.ends, result.starts = ends, np.r_[0., ends[:-1]]
    result.sol = [solutions[index] for index in expected_mapping]
    result.original_control_row_count, result.integration_run_count = len(tr['durations']), len(groups)
    result.step = .01
    result.engine = SimpleNamespace(**copy.deepcopy(header['engine']))
    clock.check()
    if header['source'] != guard():
        raise StructuralStop('DENSE_SOURCE_CHANGED_DURING_IMPORT')
    return result
