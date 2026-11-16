"""Every actor binds one immutable request layout; implementation location stays fixed."""
import os,re,time
from types import MappingProxyType
from .io_core import ROOT,CODE,read,sha,content,absolute,inside

def checked_artifact(path,expected,full=False):
    p=absolute(str(path))
    if sha(p)!=expected:raise ValueError('CODE_ARTIFACT_IDENTITY')
    value=read(p)
    if value.get('schema')!='IMMUTABLE_REQUEST_RUNTIME_CODE_V1' or absolute(value['code_root'])!=CODE:raise ValueError('CODE_ARTIFACT_LOCATION_OR_SCHEMA')
    if full:
        for name,digest in value['source_sha256'].items():
            if sha(ROOT/name)!=digest:raise ValueError('IMMUTABLE_CODE_OR_DEPENDENCY_CHANGED:'+name)
    return value

def checked_policy(path,expected):
    p=absolute(str(path))
    if sha(p)!=expected:raise ValueError('OUTPUT_POLICY_IDENTITY')
    value=read(p)
    if value.get('schema')!='FROZEN_OUTPUT_NAMESPACE_V1':raise ValueError('OUTPUT_POLICY_SCHEMA')
    root=absolute(value['allowed_root'])
    if not root.is_relative_to(ROOT) or root==ROOT or root.is_relative_to(CODE) or CODE.is_relative_to(root):raise ValueError('OUTPUT_POLICY_CODE_OVERLAP')
    return root

def checked_permit(path,expected,mission_sha,key,output,artifact_sha,policy_sha):
    p=absolute(str(path))
    if sha(p)!=expected:raise ValueError('REQUEST_EXECUTION_PERMIT_CHANGED')
    value=read(p)
    if value.get('schema')!='ONE_REQUEST_EXECUTION_PERMIT_V1' or type(value.get('maximum_requests')) is not int or value['maximum_requests']!=1:raise ValueError('REQUEST_PERMIT_SCHEMA')
    wanted=dict(mission_sha256=mission_sha,case_key=key,output_dir=str(output),code_artifact_sha256=artifact_sha,output_policy_sha256=policy_sha,
        limits=dict(prepare_s=8.,worker_s=20.,initial_s=60.,root_s=120.))
    for field,required in wanted.items():
        if content(value.get(field))!=content(required):raise ValueError('REQUEST_PERMIT_BINDING:'+field)
    if not isinstance(value.get('permit_id'),str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}',value['permit_id']):raise ValueError('REQUEST_PERMIT_ID')
    return value

def require_binding():
    path=absolute(os.environ.get('G61_REQUEST_LAYOUT_FILE',''))
    expected=os.environ.get('G61_REQUEST_LAYOUT_SHA256','')
    if not re.fullmatch('[0-9a-f]{64}',expected) or sha(path)!=expected:raise ValueError('REQUEST_LAYOUT_IDENTITY')
    value=read(path)
    fields={'schema','case_key','campaign_dir','input_file','input_sha256','original_input_file','original_input_sha256',
        'code_artifact_file','code_artifact_sha256','output_policy_file','output_policy_sha256','permit_file','permit_sha256',
        'material_manifest_file','material_manifest_sha256','material_root','paid_start_perf'}
    if set(value)!=fields or value['schema']!='INDEPENDENT_REQUEST_LAYOUT_V1':raise ValueError('REQUEST_LAYOUT_SCHEMA')
    key=value['case_key']
    if not isinstance(key,str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}',key) or os.environ.get('G61_CAUSAL_ENV_CASE')!=key:raise ValueError('REQUEST_CASE_IDENTITY')
    root=checked_policy(value['output_policy_file'],value['output_policy_sha256']);campaign=inside(absolute(value['campaign_dir']),root)
    if campaign==root or campaign.is_relative_to(CODE) or CODE.is_relative_to(campaign):raise ValueError('REQUEST_OUTPUT_CODE_OVERLAP')
    if path!=campaign/'REQUEST_LAYOUT.json':raise ValueError('REQUEST_LAYOUT_PATH')
    snapshot=absolute(value['input_file'])
    if snapshot!=campaign/'tasks'/key/'MISSION_INPUT.json' or sha(snapshot)!=value['input_sha256']:raise ValueError('REQUEST_INPUT_SNAPSHOT')
    if sha(absolute(value['original_input_file']))!=value['original_input_sha256'] or value['input_sha256']!=value['original_input_sha256']:raise ValueError('REQUEST_ORIGINAL_INPUT_CHANGED')
    checked_artifact(value['code_artifact_file'],value['code_artifact_sha256'])
    checked_permit(value['permit_file'],value['permit_sha256'],value['input_sha256'],key,campaign,value['code_artifact_sha256'],value['output_policy_sha256'])
    if sha(absolute(value['material_manifest_file']))!=value['material_manifest_sha256']:raise ValueError('REQUEST_MATERIAL_MANIFEST_CHANGED')
    material=absolute(value['material_root'])
    if not material.is_relative_to(ROOT) or material.is_relative_to(campaign):raise ValueError('REQUEST_MATERIAL_ROOT')
    t=value['paid_start_perf']
    if isinstance(t,bool) or not isinstance(t,(int,float)) or not 0<t<=time.perf_counter():raise ValueError('REQUEST_PAID_START_CLOCK')
    return MappingProxyType(dict(value,layout_file=str(path),layout_sha256=expected))

def assert_same(expected):
    current=require_binding()
    if dict(current)!=dict(expected):raise ValueError('REQUEST_LAYOUT_SWITCHED_DURING_ACTOR')
    return current

