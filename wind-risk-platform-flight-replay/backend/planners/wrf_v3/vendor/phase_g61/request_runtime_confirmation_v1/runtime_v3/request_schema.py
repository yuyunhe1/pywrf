"""Known mission/model identity, approved native records and canonical role validation."""
import copy,re
from .io_core import CODE,read,content,sha
from .roles import validate_input

def validate_mission(mission):
    if not isinstance(mission,dict):raise ValueError('MISSION_SCHEMA')
    key=mission.get('case_key')
    if not isinstance(key,str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}',key):raise ValueError('MISSION_CASE_KEY')
    reference=read(CODE/'resources/INPUT_CONTEXTS.json');source=mission.get('source_case_id')
    if source not in reference['missions']:raise ValueError('UNREGISTERED_ORIGINAL_MISSION')
    canonical=reference['missions'][source];original=canonical['input'];case=mission['case'];base=original['case']
    for field in ['original_J_seed','original_J_task_sha256','original_manifest_sha256','legacy_physical_cap_s']:
        if content(mission.get(field))!=content(original[field]):raise ValueError('MISSION_ORIGINAL_IDENTITY:'+field)
    if mission.get('original_base_input_sha256')!=canonical['input_file_sha256']:raise ValueError('MISSION_ORIGINAL_INPUT_SHA')
    if not isinstance(case.get('id'),str) or not case['id'] or len(case['id'])>160:raise ValueError('MISSION_CASE_ID_SCHEMA')
    allowed=set(base)|{'initial_time_arm','initial_time_refinement','parent_validation_policy','mission_role'}
    if set(case)!=allowed:raise ValueError('MISSION_CASE_FIELDS')
    for field in set(base)-{'id','provider'}:
        if content(case[field])!=content(base[field]):raise ValueError('MISSION_PHYSICAL_CONTEXT_CHANGED:'+field)
    validate_input(case)
    if case['parent_validation_policy']!='HOT_SINGLE_USE_WORKER_C_OWNER_COLLISION_EARLY_REFERENCE_V1':raise ValueError('PARENT_VALIDATION_POLICY')
    provider=case['provider'];expected=base['provider']
    for field in set(expected)-{'records','epoch_utc'}:
        if content(provider.get(field))!=content(expected[field]):raise ValueError('MISSION_WIND_CONTEXT_CHANGED:'+field)
    if set(provider)!=set(expected) or not isinstance(provider['records'],list) or len(provider['records'])<2:raise ValueError('MISSION_PROVIDER_SCHEMA')
    for record in provider['records']:
        token=record['expected_sha256']+':'+str(record['record_index'])
        if content(record)!=content(reference['approved_native_records'].get(token)):raise ValueError('UNAPPROVED_NATIVE_RECORD')
        if record['case_id']!=expected['records'][0]['case_id']:raise ValueError('MISSION_WEATHER_CLASS_CHANGED')
    # Existing native provider enforces full UTC/grid/lineage and actual coverage
    # when it loads. Header eligibility is not feasibility or a domain proof.
    from .native_clock import coverage_from_spec
    coverage_from_spec(provider)
    if case['route']['controls'] or case['route']['durations']:raise ValueError('INITIAL_ROUTE_ORACLE_FORBIDDEN')
    return case['mission_role']

