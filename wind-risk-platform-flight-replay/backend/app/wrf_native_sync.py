"""Verify an entire native batch before publishing its local selection index."""
import hashlib
import json
import os
from pathlib import Path
from time import monotonic
from uuid import uuid4

import numpy as np

from .wrf_native_schema import npz_metadata, validate_index


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def sync(provider):
    began=monotonic()
    with provider.SYNC_LOCK:
        client,sftp=provider._connect_sftp()
        downloaded=skipped=transferred=0
        try:
            with sftp.open(provider._remote_path('index.json'),'rb') as stream:
                remote=json.loads(stream.read().decode('utf-8'))
            batches=validate_index(remote)
            latest=max(batches)
            rows=[row for row in remote['files'] if row['cycle']==latest]
            for row in rows:
                target=provider.cache_dir()/row['path']
                target.parent.mkdir(parents=True,exist_ok=True)
                if target.is_file() and target.stat().st_size==row['size_bytes'] and digest(target)==row['sha256']:
                    skipped+=1
                    continue
                temporary=target.with_name(target.name+'.'+uuid4().hex+'.part')
                try:
                    path=provider._remote_path(row['path'])
                    before=sftp.stat(path)
                    sftp.get(path,str(temporary))
                    after=sftp.stat(path)
                    if ((before.st_size,before.st_mtime)!=(after.st_size,after.st_mtime)
                        or temporary.stat().st_size!=row['size_bytes'] or digest(temporary)!=row['sha256']):
                        raise ValueError('WRF 10 分钟批次传输不完整或哈希不一致')
                    with np.load(temporary,allow_pickle=False) as data:
                        metadata=npz_metadata(data)
                        if (str(data['cycle_utc'].item())!=row['cycle'] or str(data['valid_time_utc'].item())!=row['valid_time']
                            or metadata['forecast_minutes']!=row['forecast_minutes'] or not {'u','v'}.issubset(data.files)):
                            raise ValueError('WRF 文件与批次索引不一致')
                    os.replace(temporary,target)
                    downloaded+=1;transferred+=target.stat().st_size
                finally:
                    temporary.unlink(missing_ok=True)
            # Preserve earlier, completely downloaded native batches, while a
            # partial local directory can never become selectable by globbing.
            ready={latest}
            previous={};prior={}
            try:
                prior=json.loads((provider.cache_dir()/'index.json').read_text(encoding='utf-8'))
                validate_index(prior)
                previous={(row['cycle'],row['valid_time']):row for row in prior['files']}
            except (OSError,ValueError,KeyError):
                pass
            def previously_verified(row):
                old=previous.get((row['cycle'],row['valid_time']),{})
                if any(old.get(key)!=row.get(key) for key in ('path','size_bytes','sha256','forecast_minutes')):
                    return False
                try:return (provider.cache_dir()/row['path']).stat().st_size==row['size_bytes']
                except OSError:return False
            for cycle in batches:
                if all(previously_verified(row) for row in remote['files'] if row['cycle']==cycle):
                    ready.add(cycle)
            payload={**remote,'files':[row for row in remote['files'] if row['cycle'] in ready]}
            payload['cycles']=sorted(ready)
            by_valid={}
            for row in sorted(payload['files'],key=lambda r:(r['cycle'],r['valid_time'])):
                by_valid[row['valid_time']]={key:row[key] for key in ('cycle','cycle_bj','forecast_hour','forecast_minutes','valid_time','valid_time_bj') if key in row}
            payload['valid_times']=[dict(row,label=row.get('valid_time_bj',row['valid_time'])) for _,row in sorted(by_valid.items())]
            payload['forecast_hours']=sorted({row['forecast_hour'] for row in payload['files']})
            payload['forecast_hours_by_cycle']={cycle:sorted({row['forecast_hour'] for row in payload['files'] if row['cycle']==cycle}) for cycle in ready}
            validate_index(payload)
            directory=provider.cache_dir();directory.mkdir(parents=True,exist_ok=True)
            temporary=directory/('index.'+uuid4().hex+'.tmp')
            try:
                with temporary.open('w',encoding='utf-8') as stream:
                    json.dump(payload,stream,ensure_ascii=False,indent=2)
                    stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,directory/'index.json')
            finally:temporary.unlink(missing_ok=True)
            provider.load_index.cache_clear()
            # A ten-minute check of the same immutable products must not throw
            # away all hot display frames. New/replaced products still reset it.
            identity = lambda index: (index.get('wind_time_model'), index.get('levels'), index.get('files'))
            if downloaded or identity(payload) != identity(prior):
                provider._get_grid_cached.cache_clear()
            return dict(enabled=True,latest_cycle=latest,files=len(rows),downloaded=downloaded,skipped=skipped,
                        transferred_bytes=transferred,sync_seconds=monotonic()-began,
                        output_interval_seconds=600,wind_time_model=remote['wind_time_model'])
        finally:
            sftp.close();client.close()
