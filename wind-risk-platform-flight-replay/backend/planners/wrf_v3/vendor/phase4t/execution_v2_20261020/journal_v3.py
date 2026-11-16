"""E0: journal V3 - complete-write loop, strict frame validation, explicit tail states.

Two defects were reproduced against v2 with pure I/O injection (see
`../EVIDENCE_ERRATA.md` and `journal_v3_tests.py`):

1. v2 called ``os.write`` once and never compared the returned byte count, so a
   short write still returned from the checkpoint and advanced index/head while
   the durable file held only part of the frame;
2. v2 treated a hash failure on the last *terminated* frame as a torn tail and
   silently truncated the prefix, whereas the frozen v1 journal stops on any
   corrupted record; a complete frame that fails validation must stop.

V3 therefore:

* writes with a complete-write loop, raises on a zero or negative return, and
  only advances ``index``/``head`` after the whole frame has been written AND
  fsync has returned;
* distinguishes three tail states explicitly instead of conflating them:
  ``NO_TAIL``, ``TORN_IN_FLIGHT_TAIL`` (no terminating newline, does not parse or
  does not validate - ignored, not counted) and
  ``UNTERMINATED_VALID_TAIL`` (no terminating newline but parses, hashes and
  binds correctly - reported separately and NOT counted as a committed frame);
* raises ``StructuralStop`` on any invalid *terminated* frame: parse error,
  wrong type, missing/invalid field, broken hash, broken binding, broken
  sequence.

Durability is unchanged from v2: one fsync per applied control, so an
interrupted process can lose at most the frame in flight.
"""

import json
import os

from phase4a_design.mock_contract import content_hash
from phase4t.amendment_v1.model import StructuralStop
from phase4t.repair_v1.common import put, read

FORMAT = 'APPEND_ONLY_JSONL_HASH_CHAIN_V3_WRITE_ALL'
REQUIRED_FIELDS = ('binding_sha256', 'previous_sha256', 'row', 'sha256')
ROW_FIELDS = ('index', 'u', 'd', 'state')


class JournalV3:
    def __init__(self, path, binding, *, write_fn=None, fsync_fn=None):
        self.path = path
        self.binding = binding
        self.head = content_hash(binding)
        self.index = 0
        self.frames = 0
        self.format = FORMAT
        self._write = write_fn or os.write
        self._fsync = fsync_fn or os.fsync
        path.mkdir(parents=True, exist_ok=True)
        put(path / 'prefix_identity.json', dict(binding=binding, binding_sha256=self.head))
        self.file = path / 'prefix.jsonl'
        self.fd = os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o666)

    def write_all(self, data):
        view = memoryview(data)
        written = 0
        while len(view):
            n = self._write(self.fd, view)
            if n is None or n <= 0:
                raise StructuralStop('JOURNAL_WRITE_STALLED_AFTER_%d_OF_%d' % (written, len(data)))
            written += n
            view = view[n:]
        return written

    def __call__(self, row):
        if row['index'] != self.index + 1:
            raise StructuralStop('JOURNAL_SEQUENCE')
        payload = dict(binding_sha256=content_hash(self.binding),
                       previous_sha256=self.head, row=row)
        value = dict(**payload, sha256=content_hash(payload))
        line = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':')) + '\n').encode('utf-8')
        # index/head advance only after the whole frame is durable
        self.write_all(line)
        self._fsync(self.fd)
        self.head = value['sha256']
        self.index += 1
        self.frames += 1

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _validate_frame(piece, binding, head, expected_index):
    try:
        rec = json.loads(piece.decode('utf-8'))
    except Exception as exc:
        raise StructuralStop('JOURNAL_FRAME_PARSE:' + type(exc).__name__)
    if not isinstance(rec, dict):
        raise StructuralStop('JOURNAL_FRAME_NOT_OBJECT')
    for name in REQUIRED_FIELDS:
        if name not in rec:
            raise StructuralStop('JOURNAL_FRAME_MISSING_FIELD:' + name)
    digest = rec.pop('sha256')
    row = rec.get('row')
    if not isinstance(row, dict):
        raise StructuralStop('JOURNAL_FRAME_ROW_NOT_OBJECT')
    for name in ROW_FIELDS:
        if name not in row:
            raise StructuralStop('JOURNAL_FRAME_ROW_MISSING_FIELD:' + name)
    if rec.get('binding_sha256') != content_hash(binding):
        raise StructuralStop('JOURNAL_FRAME_BINDING')
    if rec.get('previous_sha256') != head:
        raise StructuralStop('JOURNAL_FRAME_CHAIN')
    if content_hash(rec) != digest:
        raise StructuralStop('JOURNAL_FRAME_HASH')
    if not isinstance(row['index'], int) or row['index'] != expected_index:
        raise StructuralStop('JOURNAL_SEQUENCE')
    return row, digest


def recover_v3(path, binding):
    identity = read(path / 'prefix_identity.json')
    head = content_hash(binding)
    if identity != dict(binding=binding, binding_sha256=head):
        raise StructuralStop('JOURNAL_IDENTITY')
    data = (path / 'prefix.jsonl').read_bytes()
    pieces = data.split(b'\n')
    residual = pieces[-1]          # b'' when the file ends with a frame terminator
    terminated = pieces[:-1]       # complete frames only, never the residual
    rows = []
    for piece in terminated:
        row, digest = _validate_frame(piece, binding, head, len(rows) + 1)
        rows.append(row)
        head = digest
    tail = dict(kind='NO_TAIL', bytes=0, valid=False, row=None)
    if residual:
        valid = True
        row = None
        try:
            row, _ = _validate_frame(residual, binding, head, len(rows) + 1)
        except StructuralStop:
            valid = False
        tail = dict(kind='UNTERMINATED_VALID_TAIL' if valid else 'TORN_IN_FLIGHT_TAIL',
                    bytes=len(residual), valid=valid, row=row)
    return dict(rows=rows, prefix_head_sha256=head, complete_controls=len(rows),
                partial_only=True, late_revalidation_cannot_create_timely_success=True,
                format=FORMAT, in_flight_tail=tail,
                torn_tail_ignored=tail['kind'] == 'TORN_IN_FLIGHT_TAIL')


def verify_prefix_against_actual(prefix, executed_trajectory, *, require_complete=True):
    """Owner admission: the recovered prefix must BE the actual applied controls.

    A corrupted or truncated journal can therefore never keep a success claim by
    falling back to a shorter prefix: on success the count, every u/d/state and
    the chain head must match the sealed executed record exactly.
    """
    rows = prefix['rows']
    controls = executed_trajectory['controls']
    durations = executed_trajectory['durations']
    states = executed_trajectory['states']
    if require_complete and len(rows) != len(controls):
        raise StructuralStop('PREFIX_ACTUAL_COUNT_MISMATCH:%d!=%d' % (len(rows), len(controls)))
    if [r['u'] for r in rows] != list(controls):
        raise StructuralStop('PREFIX_ACTUAL_CONTROLS_MISMATCH')
    if [r['d'] for r in rows] != list(durations):
        raise StructuralStop('PREFIX_ACTUAL_DURATIONS_MISMATCH')
    if [r['state'] for r in rows] != list(states[1:]):
        raise StructuralStop('PREFIX_ACTUAL_STATES_MISMATCH')
    if prefix['complete_controls'] and prefix['in_flight_tail']['kind'] != 'NO_TAIL':
        raise StructuralStop('PREFIX_TAIL_PRESENT_ON_COMPLETE_PREFIX')
    return dict(prefix_matches_actual_controls=True, records=len(rows))
