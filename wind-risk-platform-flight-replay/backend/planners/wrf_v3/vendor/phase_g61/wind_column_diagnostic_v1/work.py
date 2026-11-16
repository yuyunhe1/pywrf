"""A separate bounded diagnostic round; sealed prior rounds remain immutable."""
import datetime, hashlib, json, os, time
from pathlib import Path

D = Path(__file__).resolve().parent
ROOT = D.parents[1]
OLD = ROOT / 'phase_g61/uncovered_environments_v1'

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def content(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def put(name, value):
    path = D / name
    if not path.resolve().is_relative_to(D):
        raise RuntimeError('WRITE_SCOPE')
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2).encode()
    with path.open('xb') as stream:
        if stream.write(data) != len(data):
            raise RuntimeError('SHORT_WRITE')
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != data:
        raise RuntimeError('READBACK')

def elapsed():
    return time.perf_counter() - read(D / 'START.json')['start_perf']

def remaining():
    return read(D / 'START.json')['cap_s'] - elapsed()

def check(reserve=0.):
    if remaining() < reserve:
        raise RuntimeError('DIAGNOSTIC_ROUND_BUDGET')
    return remaining()

def history_before():
    started = time.perf_counter()
    old = read(OLD / 'HISTORY_BEFORE.json')['files']
    changes = []
    files = {}
    for name, expected in old.items():
        actual = sha(name) if Path(name).is_file() else None
        if actual != expected:
            changes.append(dict(path=name, expected=expected, actual=actual))
        files[name] = actual
    for path in OLD.rglob('*'):
        if path.is_file():
            files[str(path)] = sha(path)
    put('HISTORY_BEFORE.json', dict(status='PASS' if not changes else 'FAIL',
        files=files, count=len(files), changes=changes, wall_s=time.perf_counter()-started))
    if changes:
        raise RuntimeError('HISTORY_CHANGED_BEFORE')

def main():
    # Includes initial investigation from the first recorded tool result, plus
    # 30 seconds covering the first message/tool invocation before that result.
    origin = datetime.datetime.fromisoformat('2026-10-24T03:11:49.9225676+08:00').timestamp() - 30.
    previous = read(OLD / 'FINAL_PACKAGE_AND_BUDGET.json')
    put('START.json', dict(start_perf=time.perf_counter()-(time.time()-origin),
        start_utc=datetime.datetime.fromtimestamp(origin, datetime.timezone.utc).isoformat(),
        cap_s=3600., authority='Standing user authorization for independent bounded budgets and implementation/execution; latest request asks root-cause validation.',
        scope='Saved failed03 fidelity/performance diagnostics; at most one fresh failed03 confirmation after concrete tested code change. No passed physical mission reruns.',
        previous_round_closed=True, previous_record_sha256=sha(OLD/'FINAL_PACKAGE_AND_BUDGET.json'),
        previous_round_elapsed_s=previous['final_elapsed_s'], previous_round_cap_s=previous['cap_s'],
        previous_round_overrun_s=previous['overrun_s'], old_budgets_not_reset=True,
        task_prepare_s=8., worker_s=20., offline_full_receipt_s=120., physical_horizon='IMMUTABLE_2_T_REF_PLUS_10'))
    history_before()
    print('NEW_BOUNDED_ROUND', check(), 'HISTORY_PASS', flush=True)

if __name__ == '__main__':
    main()
