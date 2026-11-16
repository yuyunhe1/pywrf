"""Run the deployed independent audit and retain real production evidence."""
import json
from pathlib import Path
import shlex

import paramiko

from deploy_native_wrf import WORK,BASE,PYTHON


def main():
    runtime=json.loads((WORK/'server-runtime.json').read_text(encoding='utf-8'))
    directory=runtime['directory']
    client=paramiko.SSHClient();client.load_system_host_keys()
    client.connect('10.129.59.14',username='root',key_filename=str(Path.home()/'.ssh/gdex_proxy_ed25519'),
                   look_for_keys=False,timeout=15)
    try:
        with client.open_sftp() as sftp:
            with sftp.open(BASE+'/data/wrf_platform_cache_10min/index.json','rb') as stream:
                raw=stream.read()
            index=json.loads(raw.decode('utf-8'))
            cycle=max(row['cycle'] for row in index['files'])
            from datetime import datetime
            label=datetime.strptime(cycle,'%Y-%m-%d %H:%M UTC').strftime('%Y%m%d%H')
        output=directory+'/server-validation.json'
        command=' '.join(shlex.quote(v) for v in (PYTHON,directory+'/validate_batch.py','--cycle',label,'--output',output))
        print('[AUDIT] Checking all 289 raw/NPZ products and frozen-reference samples: '+cycle,flush=True)
        _,stdout,stderr=client.exec_command(command,timeout=600)
        lines=[]
        for line in stdout:
            lines.append(line)
            if line.startswith('[AUDIT]'):print(line.strip(),flush=True)
        out=''.join(lines);err=stderr.read().decode('utf-8')
        if stdout.channel.recv_exit_status():raise RuntimeError(out+err)
        with client.open_sftp() as sftp:
            with sftp.open(output,'rb') as stream:proof=json.loads(stream.read().decode('utf-8'))
            assert proof['frames']==289 and proof['wrf_success_marker'] and proof['physics_and_grid_unchanged']
            (WORK/'server-validation.json').write_text(json.dumps(proof,indent=2),encoding='utf-8')
            (WORK/'server-index.json').write_bytes(raw)
            for remote_name,local_name in (
                ('WRF/test/em_real/namelist.input','production-namelist.input'),
                ('WRF/test/em_real/rsl.error.0000','wrf-success-rsl.error.0000')):
                sftp.get(BASE+'/'+remote_name,str(WORK/local_name))
        print(json.dumps(proof),flush=True)
    finally:client.close()


if __name__=='__main__':main()
