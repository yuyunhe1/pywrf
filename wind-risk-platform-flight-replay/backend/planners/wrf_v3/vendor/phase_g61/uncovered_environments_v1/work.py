import datetime,hashlib,json,os,time
from pathlib import Path
D=Path(__file__).resolve().parent;ROOT=D.parents[1]
REPAIR=ROOT/'phase_g61/online_execution_repair_v1';DEVIATION=ROOT/'phase_g61/actual_deviation_temporal_v1'
def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def content(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
def put(name,value):
    p=D/name;p.parent.mkdir(parents=True,exist_ok=True)
    if not p.resolve().is_relative_to(D):raise RuntimeError('WRITE_SCOPE')
    body=json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,indent=2).encode()
    with p.open('xb') as f:
        if f.write(body)!=len(body):raise RuntimeError('SHORT_WRITE')
        f.flush();os.fsync(f.fileno())
    if p.read_bytes()!=body:raise RuntimeError('READBACK')
def write(name,s):
    p=D/name;p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('x',encoding='utf8',newline='\n') as f:f.write(s)
def elapsed():
    r=read(D/'START.json');return time.perf_counter()-r['start_perf']+r['preparation_allowance_s']
def remaining():return read(D/'START.json')['cap_s']-elapsed()
def check(reserve=0.):
    if remaining()<reserve:raise RuntimeError('NEW_INPUT_ROUND_BUDGET')
    return remaining()
def start():
    previous=read(DEVIATION/'FINAL_PACKAGE_AND_BUDGET.json')
    put('START.json',dict(start_perf=time.perf_counter(),start_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),cap_s=7200.,preparation_allowance_s=300.,
        authority='Persistent user authorization for independent budget decisions, latest expanded five-part goal and continue unfinished work; no rerun of validated physical inputs',
        scope='Only previously unvalidated environmental tasks, then explicitly frozen failures/initial-reference gaps. No repeat of 7 validated tasks or P3 deviation pair.',
        previous_round_closed_elapsed_s=previous['cumulative_elapsed_s'],previous_round_cap_s=previous['original_round_cap_s'],previous_round_overrun_s=previous['overrun_s'],
        previous_round_final_record_sha256=sha(DEVIATION/'FINAL_PACKAGE_AND_BUDGET.json'),previous_round_archive_sha256=previous['sha256'],
        old_budget_records_untouched=True,no_budget_extension_of_a_frozen_failed_run=True))
    write('AUTHORIZATION_AND_SCOPE.md','''本独立新输入轮依据用户已授权的独立预算决策和更新后的五项目标，最多7200秒，含登记前300秒保守准备记账。上一轮封存7009.360044秒及其此前所有预算/超支记录不重置、不改写。此轮不重跑已通过的P4J-01/02/05/06/09、两种P4C障碍风及P3正负偏移物理任务。

先按原J manifest顺序审计剩余11输入。参考路线选择规则在新执行前固定：原M DIRECT若独立C/collision和raw lift均合格则优先；否则依次审计历史O0、O1，身份原样记录。不是重解释DIRECT旧失败，也不是把O0称作O1。仍不合格者保留BLOCKED原参考，本批不以新路线替换后静默覆盖。

只对合格参考执行各一次同一已冻结求解/执行数学的新namespace，均8秒含setup准备、20秒worker和120秒完整收据；原T_ref和2*T_ref+10物理期限保持。新输入未覆盖AUTUMN材料需要原native保真核查，其余已验证缓存方法不重测。后续只针对新证据揭示的缺口另冻结诊断，不用换seed或加时洗去失败。
''')
if __name__=='__main__':start()
