"""Native WRF cache identity and strict minute-resolution provenance checks."""
from datetime import datetime, timezone
import math
import os
from pathlib import PurePosixPath

NATIVE_WRF_TIME_MODEL = 'NATIVE_WRF_TEN_MINUTE_HOLD_V1'
NATIVE_WRF_STEP_SECONDS = 600


def native_required():
    return os.getenv('WRF_NATIVE_10MIN_REQUIRED', '1') != '0'


def utc(value):
    return datetime.strptime(value, '%Y-%m-%d %H:%M UTC').replace(tzinfo=timezone.utc)


def minute_of(record):
    value=(utc(record['valid_time'])-utc(record['cycle'])).total_seconds()/60
    if not math.isfinite(value) or value < 0 or not value.is_integer():
        raise ValueError('WRF 预报时次必须为非负整数分钟')
    minute=int(value)
    if 'forecast_minutes' in record and record['forecast_minutes'] != minute:
        raise ValueError('WRF 分钟时效与有效时间不一致')
    return minute


def validate_record(record):
    if record.get('wind_time_model') != NATIVE_WRF_TIME_MODEL or record.get('output_interval_seconds') != 600:
        raise ValueError('该批次缺少原生 10 分钟 WRF 产物；不使用小时数据插值补齐')
    minute=minute_of(record)
    path=PurePosixPath(record['path'])
    if path.is_absolute() or '..' in path.parts or '\\' in record['path']:
        raise ValueError('WRF 缓存路径无效')
    if minute % 10 or record.get('wind_time_model') != NATIVE_WRF_TIME_MODEL or record.get('output_interval_seconds') != 600:
        raise ValueError('该批次缺少原生 10 分钟 WRF 产物；不使用小时数据插值补齐')
    return minute


def validate_index(index, complete=True):
    if index.get('wind_time_model') != NATIVE_WRF_TIME_MODEL or index.get('output_interval_seconds') != 600:
        raise ValueError('WRF 索引不是原生 10 分钟产物')
    batches={}
    seen=set()
    for row in index.get('files', []):
        minute=validate_record(row)
        key=row['cycle'],row['valid_time']
        if key in seen: raise ValueError('WRF 索引包含重复时次')
        seen.add(key)
        if len(row.get('sha256','')) != 64 or row.get('size_bytes',0) <= 0:
            raise ValueError('WRF 索引缺少文件大小或 SHA256')
        batches.setdefault(row['cycle'],set()).add(minute)
    if complete:
        expected=set(range(0,int(index.get('forecast_duration_minutes',2880))+1,10))
        if not batches or any(minutes != expected for minutes in batches.values()):
            raise ValueError('WRF 原生 10 分钟批次不完整')
    return batches


def npz_metadata(data):
    model=str(data['wind_time_model'].item()) if 'wind_time_model' in data.files else None
    minute=int(data['forecast_minutes'].item()) if 'forecast_minutes' in data.files else None
    if native_required() or model == NATIVE_WRF_TIME_MODEL:
        row=dict(cycle=str(data['cycle_utc'].item()),valid_time=str(data['valid_time_utc'].item()),
                 path='snapshot.npz',forecast_minutes=minute,wind_time_model=model,
                 output_interval_seconds=int(data['output_interval_seconds'].item()) if 'output_interval_seconds' in data.files else None)
        validate_record(row)
        if minute is None: raise ValueError('WRF 缺少整数分钟时效')
        hour=float(data['forecast_hour'].item())
        if not math.isclose(hour,minute/60,abs_tol=1e-9):
            raise ValueError('WRF 小时时效与分钟时效不一致')
    else:
        from .wind_time import WIND_TIME_MODEL
        model=WIND_TIME_MODEL
        hour=float(data['forecast_hour'].item())
    return dict(forecast_hour=hour,forecast_minutes=minute,wind_time_model=model)
