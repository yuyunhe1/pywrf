"""Stage audited server sources, then start only at a WRF workspace boundary."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import posixpath
import shlex

import paramiko

BACKEND=Path(__file__).resolve().parents[1]
WORK=BACKEND.parents[1]/'data/native_wrf_10min_tests'
BASE='/root/pyWRF-automation'
PYTHON='/opt/miniconda3/envs/pywrf/bin/python3.7'


def remote(client,code):
    stdin,stdout,stderr=client.exec_command(shlex.quote(PYTHON)+' -',timeout=60)
    stdin.write(code);stdin.flush();stdin.channel.shutdown_write()
    out=stdout.read().decode('utf-8');err=stderr.read().decode('utf-8')
    status=stdout.channel.recv_exit_status()
    if status:raise RuntimeError('Remote preflight failed: '+out+err)
    return json.loads(out)


def mkdir(sftp,path):
    try:sftp.stat(path)
    except OSError:
        mkdir(sftp,posixpath.dirname(path));sftp.mkdir(path)


def stage(client):
    source=BACKEND/'server_wrf_10min'
    name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory=BASE+'/operations/native_wrf_10min/'+name
    source_manifest=json.loads((source/'upstream/SOURCE.json').read_text(encoding='utf-8'))
    expected={k:v['sha256'] for k,v in source_manifest['files'].items() if k.endswith('.py')}
    preflight=remote(client,"""
from pathlib import Path
import hashlib,json,numpy,netCDF4
base=Path(%r);expected=%r
for name,digest in expected.items():
    actual=hashlib.sha256((base/name).read_bytes()).hexdigest()
    if actual!=digest:
        marker=base/'operations/native_wrf_10min/active.json'
        active=json.loads(marker.read_text()) if marker.is_file() else {}
        assert name=='run_realtime_wrf_platform_cache.py' and active.get('original_source_sha256')==digest and active.get('entry_sha256')==actual,'Production source changed: '+name
print(json.dumps(dict(numpy=numpy.__version__,netcdf=netCDF4.__version__,source_hashes=expected)))
"""%(BASE,expected))
    hashes={}
    with client.open_sftp() as sftp:
        mkdir(sftp,directory)
        for path in source.rglob('*'):
            if not path.is_file() or '__pycache__' in path.parts:continue
            relative=path.relative_to(source).as_posix();target=directory+'/'+relative
            mkdir(sftp,posixpath.dirname(target));sftp.put(str(path),target)
            with sftp.open(target,'rb') as stream:actual=hashlib.sha256(stream.read()).hexdigest()
            assert actual==hashlib.sha256(path.read_bytes()).hexdigest(),relative
            hashes[relative]=actual
        # Capture the exact live namelist separately from the earlier audited snapshot.
        with sftp.open(BASE+'/WRF/test/em_real/namelist.input','rb') as stream:current=stream.read()
        with sftp.open(directory+'/namelist.before-transition','wb') as stream:stream.write(current)
    result=remote(client,"""
from pathlib import Path
import py_compile,sys,json
root=Path(%r)
for path in root.rglob('*.py'):py_compile.compile(str(path),doraise=True)
sys.path.insert(0,str(root))
from native_runner import output_options
import run_native
assert output_options()['history_interval']=='60,10'
print(json.dumps(dict(compiled=True,history_interval=output_options()['history_interval'])))
"""%directory)
    state=dict(server='10.129.59.14',directory=directory,staged_at=datetime.now(timezone.utc).isoformat(),
        files=hashes,preflight=preflight,compile=result,forecast_hours=48,num_proc=16,poll_seconds=600)
    WORK.mkdir(parents=True,exist_ok=True)
    (WORK/'server-runtime.json').write_text(json.dumps(state,indent=2),encoding='utf-8')
    return state


def start(client,allow_idle=False):
    state=json.loads((WORK/'server-runtime.json').read_text(encoding='utf-8'))
    directory=state['directory']
    code="""
import os,sys,signal,time,subprocess,json,shlex
from pathlib import Path
base=Path(%r);deployment=Path(%r)
processes=[]
for p in Path('/proc').iterdir():
    if not p.name.isdigit():continue
    try:args=(p/'cmdline').read_bytes().replace(b'\\0',b' ').decode('utf-8','replace');cwd=os.readlink(str(p/'cwd'));exe=os.readlink(str(p/'exe'))
    except OSError:continue
    if Path(exe).name.startswith('python'):processes.append((int(p.name),args,cwd))
already=[pid for pid,args,cwd in processes if str(deployment/'run_native.py') in args and '--watch' in args]
if already:
    print(json.dumps(dict(already_running=True,pid=already[0])));sys.exit(0)
old=[pid for pid,args,cwd in processes if ('run_realtime_wrf_platform_cache.py' in args or '/operations/native_wrf_10min/' in args and 'run_native.py' in args) and '--watch' in args and cwd==str(base)]
assert len(old)<=1 and (old or %r),'Expected one production watcher, or an explicitly idle restart'
pid=old[0] if old else None
environment=dict(os.environ)
if pid:
    for field in Path('/proc/%%d/environ'%%pid).read_bytes().split(b'\\0'):
        if b'=' in field:
            key,value=field.split(b'=',1);environment[key.decode()]=value.decode('utf-8','surrogateescape')
else:
    activate='source /opt/miniconda3/etc/profile.d/conda.sh && conda activate pywrf && source /root/pyWRF-automation/setup_wrf_env.sh >/dev/null && exec '+shlex.quote(%r)+' -c '+shlex.quote('import os,json; print(json.dumps(dict(os.environ)))')
    environment=json.loads(subprocess.check_output(['/bin/bash','-c',activate],universal_newlines=True))
environment.pop('TMUX',None);environment.pop('TMUX_PANE',None)
if pid:os.kill(pid,signal.SIGSTOP)
try:
    active=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            ident=int(p.name);args=(p/'cmdline').read_bytes().replace(b'\\0',b' ').decode('utf-8','replace');cwd=os.readlink(str(p/'cwd'))
        except OSError:continue
        if cwd==str(base/'WRF/test/em_real') and any(name in args for name in ('wrf.exe','real.exe','mpirun','mpiexec')):active.append(ident)
        if cwd==str(base) and ident!=pid and 'main.py' in args:active.append(ident)
    assert not active,'WRF batch still active; retry after completion: '+str(active)
except BaseException:
    if pid:os.kill(pid,signal.SIGCONT)
    raise
if pid:
    os.kill(pid,signal.SIGTERM);os.kill(pid,signal.SIGCONT)
    for _ in range(50):
        if not Path('/proc/%%d'%%pid).exists():break
        time.sleep(.05)
    assert not Path('/proc/%%d'%%pid).exists(),'Original watcher did not exit'
stamp=time.strftime('%%Y%%m%%d-%%H%%M%%S')
log=base/'logs'/('native_wrf_10min-'+stamp+'.log');log.parent.mkdir(exist_ok=True)
command=[%r,'-u',str(deployment/'run_native.py'),'--base-dir',str(base),'--gfs-dir',str(base/'data/gdex_gfs_0p25_global'),
    '--forecast-hours','48','--num-proc','16','--watch','--interval-hours',str(1/6)]
shell_command='exec '+' '.join(shlex.quote(x) for x in command)+' > '+shlex.quote(str(log))+' 2>&1'
try:
    subprocess.run(['tmux','-L','native_wrf_10min','new-session','-d','-s','production',shell_command],env=environment,cwd=str(base),check=True)
except BaseException:
    # Restore the original producer if the detached launcher itself fails.
    with (base/'logs/native_wrf_10min-launch-fallback.log').open('ab') as output:
        subprocess.Popen([%r,'-u','run_realtime_wrf_platform_cache.py','--base-dir',str(base),'--gfs-dir',str(base/'data/gdex_gfs_0p25_global'),
            '--cache-dir',str(base/'data/wrf_platform_cache'),'--forecast-hours','48','--num-proc','16','--watch','--interval-hours','1'],
            cwd=str(base),env=environment,stdout=output,stderr=output,start_new_session=True)
    raise
print(json.dumps(dict(started=True,old_pid=pid,log=str(log),tmux_socket='native_wrf_10min',tmux_session='production')))
"""%(BASE,directory,allow_idle,PYTHON,PYTHON,PYTHON)
    result=remote(client,code)
    state.update(start=result,started_at=datetime.now(timezone.utc).isoformat())
    (WORK/'server-runtime.json').write_text(json.dumps(state,indent=2),encoding='utf-8')
    return state


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--start',action='store_true');parser.add_argument('--idle',action='store_true')
    args=parser.parse_args()
    client=paramiko.SSHClient();client.load_system_host_keys()
    client.connect('10.129.59.14',username='root',key_filename=str(Path.home()/'.ssh/gdex_proxy_ed25519'),look_for_keys=False,timeout=15)
    try:
        result=start(client,args.idle) if args.start else stage(client)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:client.close()


if __name__=='__main__':main()
