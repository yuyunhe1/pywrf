"""Per-request identity replaces compiled campaign case lists."""
from pathlib import Path
from .work import D,BOUND,read,sha,content
from .layout import assert_same
from .request_schema import validate_mission

def require_input(key,mission):
    assert_same(BOUND)
    if key!=BOUND['case_key'] or sha(BOUND['input_file'])!=BOUND['input_sha256'] or read(BOUND['input_file'])!=mission:raise RuntimeError('REQUEST_MISSION_IDENTITY')
    role=validate_mission(mission)
    return dict(mission_file='tasks/'+key+'/MISSION_INPUT.json',mission_sha256=BOUND['input_sha256'],source_case_id=mission['source_case_id'],role_id=role['role_id'])
def checked_registry():
    key=BOUND['case_key'];entry=require_input(key,read(BOUND['input_file']))
    return dict(schema='REQUEST_BOUND_INPUT_V1',entries={key:entry},order=[key])
