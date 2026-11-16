"""Independent hash-bound request API; code is never copied into a run directory."""
import argparse,copy,os,shutil,sys,time,uuid
from pathlib import Path
from .io_core import ROOT,CODE,read,sha,content,absolute,inside,put,native,logical
from .layout import checked_artifact,checked_policy,checked_permit
from .request_schema import validate_mission

def configure(input_path,input_sha,output,policy_path,policy_sha,artifact_path,artifact_sha,permit_path,permit_sha,paid_start):
    if isinstance(paid_start,bool) or not isinstance(paid_start,(int,float)) or not 0<paid_start<=time.perf_counter():raise ValueError('REQUEST_PAID_START_CLOCK')
    artifact=checked_artifact(str(artifact_path),artifact_sha,full=True)
    source=absolute(str(input_path))
    if sha(source)!=input_sha:raise ValueError('INPUT_FILE_IDENTITY')
    mission=read(source);validate_mission(mission);key=mission['case_key']
    namespace=checked_policy(str(policy_path),policy_sha);campaign=inside(absolute(str(output)),namespace)
    if campaign==namespace or campaign.exists() or campaign.is_relative_to(CODE) or CODE.is_relative_to(campaign):raise ValueError('OUTPUT_NOT_FRESH_OR_CODE_OVERLAP')
    checked_permit(str(permit_path),permit_sha,input_sha,key,campaign,artifact_sha,policy_sha)
    material=artifact['materials'];manifest=absolute(material['manifest_file'])
    if sha(manifest)!=material['manifest_sha256']:raise ValueError('MATERIAL_DESCRIPTOR_IDENTITY')
    native(campaign).mkdir(parents=True,exist_ok=False);task=native(campaign)/'tasks'/key;task.mkdir(parents=True,exist_ok=False)
    snapshot=task/'MISSION_INPUT.json'
    with native(source).open('rb') as src,snapshot.open('xb') as target:shutil.copyfileobj(src,target);target.flush();os.fsync(target.fileno())
    if sha(snapshot)!=input_sha:raise ValueError('INPUT_SNAPSHOT_COPY')
    layout=dict(schema='INDEPENDENT_REQUEST_LAYOUT_V1',case_key=key,campaign_dir=str(campaign),input_file=str(snapshot),input_sha256=input_sha,
        original_input_file=str(source),original_input_sha256=input_sha,code_artifact_file=str(absolute(str(artifact_path))),code_artifact_sha256=artifact_sha,
        output_policy_file=str(absolute(str(policy_path))),output_policy_sha256=policy_sha,permit_file=str(absolute(str(permit_path))),permit_sha256=permit_sha,
        material_manifest_file=str(manifest),material_manifest_sha256=material['manifest_sha256'],material_root=material['root'],paid_start_perf=paid_start)
    put(campaign/'REQUEST_LAYOUT.json',layout,campaign)
    os.environ['G61_REQUEST_LAYOUT_FILE']=str(campaign/'REQUEST_LAYOUT.json');os.environ['G61_REQUEST_LAYOUT_SHA256']=sha(campaign/'REQUEST_LAYOUT.json');os.environ['G61_CAUSAL_ENV_CASE']=key
    active=dict(artifact['source_sha256'])
    for path in [source,snapshot,campaign/'REQUEST_LAYOUT.json',absolute(str(artifact_path)),absolute(str(policy_path)),absolute(str(permit_path)),manifest]:active[str(path)]=sha(path)
    required=dict(active);doc=read(manifest)
    for record in mission['case']['provider']['records']:
        required[record['path']]=record['expected_sha256']
        row=next(r for r in doc['records'] if r['source_sha256']==record['expected_sha256'] and r['record_index']==record['record_index'])
        arrays=(Path(material['root'])/row['arrays_file']).resolve()
        if not arrays.is_relative_to(Path(material['root'])) or arrays.is_relative_to(campaign):raise ValueError('REQUEST_ARRAY_SOURCE_SCOPE')
        required[str(arrays)]=row['arrays_file_sha256']
    for path,h in required.items():
        if sha(ROOT/path)!=h:raise ValueError('REQUEST_SOURCE_IDENTITY:'+path)
    frozen=copy.deepcopy(artifact['preplan_template']);frozen.update(source_sha256=required,active_source_sha256=active,
        mission_case_sha256=content(mission['case']),epoch='G61REQUEST_'+uuid.uuid4().hex,
        common_research_version='IMMUTABLE_LIBRARY_INDEPENDENT_REQUEST_LAYOUT_V1',
        request_layout_sha256=sha(campaign/'REQUEST_LAYOUT.json'),code_artifact_sha256=artifact_sha,physical_execution_authorized=True,
        historical_provenance_sha256={},historical_provenance_detection='EXPERIMENT_DRIVER_BEFORE_AND_AFTER;NOT_A_RUNTIME_HISTORICAL_SCAN')
    put(task/'PREPLAN_FREEZE.json',frozen,campaign)
    put(campaign/'EXECUTION_GATE.json',dict(status='PASS',order=[key],maximum_whole_tasks=1,no_retries=True,
        permit_sha256=permit_sha,request_layout_sha256=sha(campaign/'REQUEST_LAYOUT.json')),campaign)
    put(campaign/'CLI_REQUEST_ADMISSION.json',dict(case_key=key,paid_start_perf=paid_start,setup_inclusive_s=time.perf_counter()-paid_start,
        code_artifact_sha256=artifact_sha,required_source_files=len(required),active_source_files=len(active),
        implementation_copied=False,ODE_or_planner_calls_before_launch=0),campaign)
    return campaign,key

def main(argv=None):
    paid_start=time.perf_counter()
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    for command in ['inspect','run']:
        p=sub.add_parser(command);p.add_argument('--request',required=True);p.add_argument('--request-sha',required=True)
        p.add_argument('--code-artifact',required=True);p.add_argument('--code-sha',required=True)
        if command=='run':
            p.add_argument('--output',required=True);p.add_argument('--output-policy',required=True);p.add_argument('--policy-sha',required=True)
            p.add_argument('--permit',required=True);p.add_argument('--permit-sha',required=True);p.add_argument('--execute',action='store_true')
    args=parser.parse_args(argv)
    artifact=checked_artifact(args.code_artifact,args.code_sha,full=True)
    if sha(args.request)!=args.request_sha:parser.error('INPUT_FILE_IDENTITY')
    mission=read(args.request);role=validate_mission(mission)
    if args.command=='inspect':
        import json
        print(json.dumps(dict(case_key=mission['case_key'],source_case_id=mission['source_case_id'],mission_role=role,
            code_artifact_sha256=args.code_sha,code_root=str(CODE),original_physics_goal_ROI_source_identity='PASS',
            ODE_calls=0,planner_calls=0,no_output_created=True),ensure_ascii=False));return
    if not args.execute:parser.error('EXPLICIT_EXECUTE_REQUIRED')
    campaign,key=configure(args.request,args.request_sha,args.output,args.output_policy,args.policy_sha,args.code_artifact,args.code_sha,args.permit,args.permit_sha,paid_start)
    from .launch_v4 import main as launch
    launch(key,paid_start=paid_start)
    import json
    print(json.dumps(dict(output_dir=str(campaign),result=read(campaign/'tasks'/key/'OUTER_RECEIPT.json')),ensure_ascii=False))

if __name__=='__main__':main()
