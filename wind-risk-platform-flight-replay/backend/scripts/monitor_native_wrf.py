"""Read-only remote progress for the deployed native producer."""
import json
from pathlib import Path

import paramiko

WORK=Path(__file__).resolve().parents[3]/'data/native_wrf_10min_tests'


def main():
    runtime=json.loads((WORK/'server-runtime.json').read_text(encoding='utf-8'))
    code="""
import json,subprocess,re
from pathlib import Path
base=Path('/root/pyWRF-automation')
log=Path(%r)
def tail(p,limit=12000):
    if not p.is_file():return ''
    with p.open('rb') as f:
        f.seek(max(0,p.stat().st_size-limit));return f.read().decode('utf-8','replace')
text=tail(log,40000)
lines=[l for l in text.splitlines() if any(k in l for k in ['[NATIVE','[WRF','INFO:','[DONE]','ERROR','SUCCESS','[CYCLE]','[NML]'])]
result=dict(log=str(log),log_tail=lines[-12:])
work=base/'WRF/test/em_real'
result['d02_working_files']=len(list(work.glob('wrfout_d02_*')))
result['d02_latest_working_file']=max((p.name for p in work.glob('wrfout_d02_*')),default=None)
result['rsl_tail']=tail(work/'rsl.out.0000',5000).splitlines()[-6:]
result['namelist_time']=[l.strip() for l in (work/'namelist.input').read_text().splitlines() if any(k in l for k in ['history_interval','frames_per_outfile','interval_seconds','time_step '])]
index=base/'data/wrf_platform_cache_10min/index.json'
result['staged_npz_files']=len(list(index.parent.glob('.staging-*/*/*.npz')))
if index.is_file():
    data=json.loads(index.read_text());latest=max(row['cycle'] for row in data['files'])
    rows=[r for r in data['files'] if r['cycle']==latest]
    result.update(published_cycle=latest,published_frames=len(rows),wind_time_model=data.get('wind_time_model'))
p=subprocess.run(['ps','-eo','pid,ppid,etimes,args'],stdout=subprocess.PIPE,universal_newlines=True)
result['wrf_processes']=[l.strip() for l in p.stdout.splitlines() if l.strip().endswith('./wrf.exe')]
if result['wrf_processes']:
    import netCDF4
    memory=[]
    for line in result['wrf_processes']:
        fields=line.split()
        if fields[3:]!=['./wrf.exe']:continue
        status=Path('/proc')/fields[0]/'status'
        if not status.exists():continue
        values=dict((m.group(1),int(m.group(2))) for m in re.finditer(r'^(VmRSS|VmHWM):\\s+(\\d+)',status.read_text(),re.M))
        memory.append(dict(pid=int(fields[0]),rss_mb=values.get('VmRSS',0)/1024,peak_rss_mb=values.get('VmHWM',0)/1024))
    result['rank_memory']=memory
    result['first_native_records']=[]
    for path in sorted(work.glob('wrfout_d02_*'))[:3]:
        with netCDF4.Dataset(str(path)) as dataset:
            result['first_native_records'].append(dict(file=path.name,
                times=netCDF4.chartostring(dataset.variables['Times'][:]).tolist(),
                simulation_start=getattr(dataset,'SIMULATION_START_DATE',None),
                dt=float(getattr(dataset,'DT',0))))
print(json.dumps(result))
"""%runtime['start']['log']
    c=paramiko.SSHClient();c.load_system_host_keys()
    c.connect('10.129.59.14',username='root',key_filename=str(Path.home()/'.ssh/gdex_proxy_ed25519'),look_for_keys=False,timeout=15)
    try:
        i,o,e=c.exec_command('/opt/miniconda3/envs/pywrf/bin/python3.7 -',timeout=30)
        i.write(code);i.flush();i.channel.shutdown_write()
        raw=o.read().decode('utf-8');error=e.read().decode('utf-8')
        if o.channel.recv_exit_status():raise RuntimeError(raw+error)
        value=json.loads(raw)
        (WORK/'server-progress.json').write_text(json.dumps(value,indent=2,ensure_ascii=False),encoding='utf-8')
        print(json.dumps(value,ensure_ascii=False,indent=2))
    finally:c.close()


if __name__=='__main__':main()
