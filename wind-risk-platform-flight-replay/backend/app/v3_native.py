"""Mirror immutable native d02 records separately from the display AGL cache."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import struct
from threading import Event, RLock, Thread
import time

from . import wrf_cache_provider

LOCK = RLock()
STOP = Event()
THREAD = None
STATE = {"running": False, "last_error": None, "last_success": None, "downloaded": 0}
CATALOG_CACHE = {}
PATTERN = re.compile(r"wrfout_d02_(\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2})$")


def root():
    return Path(os.getenv("WRF_V3_NATIVE_DIR", str(Path(__file__).resolve().parents[2] / "data/wrf_v3_native"))).resolve()


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


def native_cycle(path):
    """Read classic NetCDF global lineage without loading native arrays.

    Both supported numerical backends verify the complete native schema and
    hashes again in the worker. A filename/date folder is not a forecast run.
    """
    with Path(path).open("rb") as stream:
        if stream.read(4) not in (b"CDF\x01", b"CDF\x02"):
            raise ValueError("V3 原生文件需要可读取的 classic NetCDF 头信息")
        def integer():
            return struct.unpack(">I",stream.read(4))[0]
        def name():
            length=integer()
            if length>1048576:
                raise ValueError("原生 WRF 文件头字符串异常")
            value=stream.read(length)
            stream.read((-length)%4)
            return value.decode("ascii").rstrip("\0")
        integer()  # number of records
        tag,count=integer(),integer()
        if tag not in (0,10) or count>10000:
            raise ValueError("原生 WRF 维度头信息无效")
        for _ in range(count):
            name();integer()
        tag,count=integer(),integer()
        if tag!=12 or count>10000:
            raise ValueError("原生 WRF 全局属性缺失")
        attrs={}
        for _ in range(count):
            key=name()
            kind,length=integer(),integer()
            width={1:1,2:1,3:2,4:4,5:4,6:8}.get(kind)
            if width is None or length*width>16777216:
                raise ValueError("原生 WRF 全局属性类型无效")
            value=stream.read(length*width)
            stream.read((-length*width)%4)
            if key in ("START_DATE","SIMULATION_START_DATE") and kind==2:
                attrs[key]=value.decode("ascii").rstrip("\0")
    if not all(attrs.get(key) for key in ("START_DATE","SIMULATION_START_DATE")):
        raise ValueError("原生 WRF 缺少起报来源属性")
    # Both values belong to the identity, even for restarted WRF runs.
    cycle=datetime.strptime(attrs["SIMULATION_START_DATE"],"%Y-%m-%d_%H:%M:%S").replace(tzinfo=timezone.utc)
    return cycle.isoformat().replace("+00:00","Z"),attrs


def partition(rows):
    groups={}
    for original in rows:
        row=dict(original)
        if not row.get("native_lineage"):
            row["cycle"],row["native_lineage"]=native_cycle(row["path"])
        else:
            row["cycle"]=datetime.strptime(row["native_lineage"]["SIMULATION_START_DATE"],"%Y-%m-%d_%H:%M:%S").replace(tzinfo=timezone.utc).isoformat().replace("+00:00","Z")
        key=json.dumps(row["native_lineage"],sort_keys=True)
        group=groups.setdefault(key,{"cycle":row["cycle"],"records":[]})
        group["records"].append(row)
    batches=sorted(groups.values(),key=lambda item:item["cycle"])
    for batch in batches:
        batch["records"].sort(key=lambda row:row["utc"])
    usable=[batch for batch in batches if len(batch["records"])>=2]
    return {"records":usable[-1]["records"] if usable else [],"batches":batches}


def index():
    try:
        path=root()/"index.json"
        stat=path.stat()
        key=(str(path),stat.st_mtime_ns,stat.st_size)
        with LOCK:
            if key not in CATALOG_CACHE:
                value=json.loads(path.read_text(encoding="utf-8"))
                if "batches" not in value:
                    value.update(partition(value.get("records",[])))
                CATALOG_CACHE.clear()
                CATALOG_CACHE[key]=value
            return CATALOG_CACHE[key]
    except FileNotFoundError:
        return {"records": []}


def status():
    with LOCK:
        current = dict(STATE)
    catalog=index()
    rows = catalog.get("records", [])
    return {**current, "available_records": len(rows), "coverage_start": rows[0]["utc"] if rows else None,
            "coverage_end": rows[-1]["utc"] if rows else None, "source": "WRF_NATIVE_D02",
            "forecast_cycle":rows[0]["cycle"] if rows else None,"available_batches":len(catalog.get("batches",[])),
            "server_time":datetime.now(timezone.utc).isoformat()}


def dataset(altitude, dataset_id=None):
    if dataset_id:
        if not re.fullmatch(r"v3_[a-f0-9]{60}", dataset_id):
            raise ValueError("V3 数据版本标识无效")
        path = root() / "datasets" / (dataset_id + ".json")
        if not path.is_file():
            raise ValueError("V3 原生数据版本已过期，请重新评估")
        result = json.loads(path.read_text(encoding="utf-8"))
        if result["altitude_msl_m"] != float(altitude):
            raise ValueError("V3 海拔高度与数据版本不一致")
        return result
    rows = index().get("records", [])
    if len(rows) < 2:
        raise ValueError("V3 原生 WRF 尚未就绪，需要至少两个相邻小时文件；后台每10分钟同步")
    payload = {"kind": "native", "altitude_msl_m": float(altitude), "records": rows,
               "cycle": rows[0].get("cycle", rows[0]["utc"]), "version": "WRF_V3_NATIVE_V1"}
    key = "v3_" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:60]
    payload["dataset_id"] = key
    path = root() / "datasets" / (key + ".json")
    with LOCK:
        if not path.exists():
            atomic(path, payload)
    return payload


def get_dataset(dataset_id):
    if not re.fullmatch(r"v3_[a-f0-9]{60}", dataset_id):
        raise ValueError("V3 数据版本标识无效")
    try:
        return json.loads((root() / "datasets" / (dataset_id + ".json")).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("V3 原生数据版本已过期，请重新评估") from exc


def sync_once():
    """Stat/hash/stat binding; partially transferred files are never indexed."""
    with LOCK:
        if STATE["running"]:
            return status()
        STATE.update(running=True, last_error=None, downloaded=0)
    client = sftp = None
    try:
        client, sftp = wrf_cache_provider._connect_sftp()
        remote = os.getenv("WRF_V3_REMOTE_DIR", "/root/pyWRF-automation/wrf_output").rstrip("/")
        dates = sorted((item.filename for item in sftp.listdir_attr(remote)
                        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", item.filename)), reverse=True)
        if not dates:
            raise ValueError("服务器没有原生 WRF 输出目录")
        # A new run may still be in progress; publish each completed record,
        # preserving the previous usable cycle until at least two are ready.
        previous = [row for batch in index().get("batches",[]) for row in batch["records"]]
        old = {row["remote_path"]: row for row in previous}
        rows = []
        for entry in sorted(sftp.listdir_attr(remote + "/" + dates[0]), key=lambda value: value.filename):
            match = PATTERN.fullmatch(entry.filename)
            if not match or entry.st_size <= 0 or time.time() - entry.st_mtime < 60:
                continue
            if STOP.is_set():
                break
            remote_path = remote + "/" + dates[0] + "/" + entry.filename
            existing = old.get(remote_path)
            if existing and existing.get("remote_size") == entry.st_size and existing.get("remote_mtime") == entry.st_mtime and Path(existing["path"]).is_file():
                rows.append(existing)
            else:
                stamp = datetime.strptime(match[1], "%Y-%m-%d_%H:%M:%S").replace(tzinfo=timezone.utc)
                local = root() / dates[0] / (stamp.strftime("%Y%m%dT%H%M%SZ") + ".nc")
                local.parent.mkdir(parents=True, exist_ok=True)
                temporary = local.with_suffix(".part")
                before = sftp.stat(remote_path)
                sftp.get(remote_path, str(temporary))
                actual = digest(temporary)
                _, stdout, stderr = client.exec_command("sha256sum -- " + shlex.quote(remote_path), timeout=60)
                remote_hash = stdout.read().decode().split()[0]
                if stdout.channel.recv_exit_status() or not re.fullmatch(r"[a-f0-9]{64}", remote_hash):
                    raise ValueError("原生 WRF 远端哈希读取失败")
                after = sftp.stat(remote_path)
                if (before.st_size, before.st_mtime) != (after.st_size, after.st_mtime) or actual != remote_hash:
                    temporary.unlink(missing_ok=True)
                    continue
                # Preserve old jobs' immutable snapshots when a remote run is
                # republished under the same hour filename.
                local=local.with_stem(local.stem+"_"+actual)
                os.replace(temporary, local)
                cycle,lineage=native_cycle(local)
                rows.append({"path": str(local), "expected_sha256": actual, "expected_time": match[1],
                             "record_index": 0, "case_id": "PLATFORM_D02_" + dates[0], "source_run_id": "PLATFORM_D02_" + dates[0],
                             "utc": stamp.isoformat().replace("+00:00", "Z"), "cycle":cycle,"native_lineage":lineage,
                             "remote_path": remote_path, "remote_size": after.st_size, "remote_mtime": after.st_mtime})
                with LOCK:
                    STATE["downloaded"] += 1
            if len(rows) >= 2:
                with LOCK:
                    combined={row["remote_path"]:row for row in [*previous,*rows]}
                    atomic(root() / "index.json", {**partition(combined.values()),"updated_at":datetime.now(timezone.utc).isoformat()})
        with LOCK:
            STATE.update(last_success=datetime.now(timezone.utc).isoformat(), last_error=None)
    except Exception as exc:
        with LOCK:
            STATE["last_error"] = str(exc)
    finally:
        if sftp:
            sftp.close()
        if client:
            client.close()
        with LOCK:
            STATE["running"] = False
    return status()


def start_polling():
    global THREAD
    from .wrf_native_schema import native_required
    if native_required(): return
    if os.getenv("WRF_V3_AUTO_SYNC", "1") == "0" or not wrf_cache_provider.remote_configured():
        return
    with LOCK:
        if THREAD and THREAD.is_alive():
            return
        STOP.clear()
        def run():
            while not STOP.is_set():
                sync_once()
                STOP.wait(max(1, float(os.getenv("WRF_CACHE_INDEX_SYNC_INTERVAL_SECONDS", "600"))))
        THREAD = Thread(target=run, name="wrf-v3-native-sync", daemon=True)
        THREAD.start()


def stop_polling():
    STOP.set()
