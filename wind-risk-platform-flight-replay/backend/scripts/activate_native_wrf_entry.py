"""Activate original CLI delegation only after a real batch audit succeeds."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

import paramiko

BACKEND=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BACKEND/'server_wrf_10min'))
from legacy_entry import guarded_source,MARKER
from deploy_native_wrf import remote,PYTHON,BASE,WORK


def main():
    state=json.loads((WORK/'server-runtime.json').read_text(encoding='utf-8'))
    proof=json.loads((WORK/'server-validation.json').read_text(encoding='utf-8'))
    assert proof['frames']==289 and proof['wind_time_model']=='NATIVE_WRF_TEN_MINUTE_HOLD_V1'
    original=(BACKEND/'server_wrf_10min/upstream/run_realtime_wrf_platform_cache.py').read_bytes()
    expected=hashlib.sha256(original).hexdigest();guard=guarded_source(original);guard_hash=hashlib.sha256(guard).hexdigest()
    client=paramiko.SSHClient();client.load_system_host_keys()
    client.connect('10.129.59.14',username='root',key_filename=str(Path.home()/'.ssh/gdex_proxy_ed25519'),look_for_keys=False,timeout=15)
    target=BASE+'/run_realtime_wrf_platform_cache.py'
    temporary=BASE+'/.native-producer-entry-'+uuid4().hex+'.tmp'
    try:
        with client.open_sftp() as sftp:
            with sftp.open(target,'rb') as stream:before=stream.read()
            assert hashlib.sha256(before).hexdigest() in (expected,guard_hash),'Unexpected original CLI changes'
            with sftp.open(state['directory']+'/original-entry-before-activation.py','wb') as stream:stream.write(before)
            with sftp.open(temporary,'wb') as stream:stream.write(guard)
        result=remote(client,"""
from pathlib import Path
import hashlib,json,os,py_compile,uuid
target=Path(%r);temporary=Path(%r);marker=Path(%r)
assert hashlib.sha256(target.read_bytes()).hexdigest() in %r
assert hashlib.sha256(temporary.read_bytes()).hexdigest()==%r
py_compile.compile(str(temporary),doraise=True)
payload=%r
temp=marker.with_name('active.'+uuid.uuid4().hex+'.tmp')
temp.write_text(json.dumps(payload,indent=2),encoding='utf-8')
os.replace(str(temp),str(marker))
os.chmod(str(temporary),target.stat().st_mode & 0o777)
os.replace(str(temporary),str(target))
print(json.dumps(dict(active=True,entry_sha256=payload['entry_sha256'],directory=payload['directory'])))
"""%(target,temporary,MARKER,(expected,guard_hash),guard_hash,dict(directory=state['directory'],
            entry_sha256=guard_hash,original_source_sha256=expected,wind_time_model=proof['wind_time_model'],
            activated_at=datetime.now(timezone.utc).isoformat())))
        (WORK/'entry-activation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result))
    finally:client.close()


if __name__=='__main__':main()
