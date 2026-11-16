"""FastAPI bridge to isolated V3 workers and asynchronous window certificates."""
from __future__ import annotations
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Event, RLock
from time import monotonic
from uuid import uuid4

from . import v3_native
from .gfs_provider import _current_utc
from .planning_control import checkpoint, remaining_seconds, planning_scope, PlanningCancelled, PlanningTimedOut
from .planning_limits import DEFAULT_TIMEOUT_SECONDS, seconds_setting

BASE = Path(__file__).resolve().parents[1]
WORKER = BASE / "planners/wrf_v3/adapter/worker.py"
WINDOW_LOCK = RLock()
WINDOW_JOBS = {}
WINDOW_KEYS = {}
WINDOW_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="v3-window")
FRAME_LOCK = RLock()
FRAMES = OrderedDict()


def artifact_root():
    return Path(os.getenv("WRF_V3_ARTIFACT_DIR", str(BASE.parent / "data/wrf_v3_runtime"))).resolve()


def numerical_python():
    configured = os.getenv("WRF_V3_PYTHON")
    if configured:
        return configured
    pinned = Path("D:/GoodTools/anaconda3/python.exe")
    return str(pinned) if pinned.is_file() else sys.executable


def trajectory(token):
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        raise ValueError("V3 轨迹标识无效")
    path = artifact_root() / "trajectories" / (token + ".json")
    if not path.is_file():
        raise ValueError("V3 连续航线材料不存在，请重新规划或恢复航线材料")
    return json.loads(path.read_text(encoding="utf-8"))


def run_worker(operation, request, dataset, publish=None, budget_seconds=None):
    checkpoint()
    job_dir = artifact_root() / "jobs" / str(uuid4())
    job_dir.mkdir(parents=True)
    seconds = max(.1, min(budget_seconds or 600., remaining_seconds()))
    body = {"operation": operation, "request": request, "dataset": dataset,
            "budget_seconds": max(.05, seconds-.5), "artifact_root": str(artifact_root())}
    (job_dir / "request.json").write_text(json.dumps(body, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
                   "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    seen = 0
    started = monotonic()
    process = None
    def progress():
        nonlocal seen
        # Read each durable improvement in order, even if several were written
        # between polls. The caller owns its deadline and generation guard.
        while (job_dir / f"result_{seen+1:05d}.json").is_file():
            seen += 1
            value = json.loads((job_dir / f"result_{seen:05d}.json").read_text(encoding="utf-8"))
            if publish:
                publish(value)
    try:
        with (job_dir / "stdout.log").open("wb") as stdout, (job_dir / "stderr.log").open("wb") as stderr:
            process = subprocess.Popen([numerical_python(), "-B", str(WORKER), str(job_dir / "request.json"), str(job_dir)],
                cwd=BASE, env=environment, stdout=stdout, stderr=stderr,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            while process.poll() is None:
                checkpoint()
                progress()
                if monotonic()-started >= seconds:
                    raise PlanningTimedOut()
                try:
                    process.wait(timeout=.1)
                except subprocess.TimeoutExpired:
                    pass
            progress()
        final_path = job_dir / "final.json"
        if not final_path.is_file():
            raise ValueError("V3 数值工作进程异常退出，请检查数值环境和任务日志")
        final = json.loads(final_path.read_text(encoding="utf-8"))
        if final["status"] == "timed_out":
            raise PlanningTimedOut()
        if final["status"] != "completed":
            raise ValueError(final.get("message", "V3 计算失败"))
        return final["result"]
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def prepare_plan(request):
    from .wrf_native_schema import native_required
    if native_required():
        raise ValueError('V3 暂停新规划：原生 10 分钟时间模型尚未适配，请使用 WA-LPA*、LPA* 或 A*')
    from .models import DepartureWindowsRequest
    from .route_options import normalize_aircraft_model, normalize_planning_strategy
    DepartureWindowsRequest(thresholds=request.thresholds,wind_shear=request.wind_shear)
    normalize_aircraft_model(request.aircraft_model)
    dataset = v3_native.dataset(request.altitude_msl_m, request.dataset_id)
    departure = request.departure_at or _current_utc()
    if departure.tzinfo is None or departure.utcoffset() is None:
        raise ValueError("起飞时间必须包含时区")
    request = request.model_copy(update={"departure_at": departure, "dataset_id": dataset["dataset_id"], "time_aware": True, "source": "wrf",
                                         "planning_strategy":normalize_planning_strategy(request.planning_strategy)})
    return request, dataset


def compute_plan(request, publish=None, dataset=None):
    if dataset is None:
        request, dataset = prepare_plan(request)
    return run_worker("plan", request.model_dump(mode="json"), dataset, publish,
                      budget_seconds=seconds_setting("ROUTE_PLAN_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))


def validate_record(record):
    if not record.v3:
        return
    saved = trajectory(record.v3.trajectory_id)
    z = saved["route"]["z_layer_msl_m"]
    if z != record.v3.altitude_msl_m or record.level != f"{z:g}m MSL":
        raise ValueError("保存的 V3 海拔高度与连续曲线不一致")
    # Geometry material, rather than a mutable display polyline, is authoritative.
    from math import dist
    points = record.points
    expected=saved.get("display_points",[])
    if len(points)!=len(expected) or any(len(p)<3 or dist(p[:3],q[:3])>1e-6 for p,q in zip(points,expected)):
        raise ValueError("V3 展示点与连续曲线材料不一致，请保留原规划几何")
    if dist(record.start,points[0][:2])>1e-6 or dist(record.end,points[-1][:2])>1e-6:
        raise ValueError("V3 起终点与连续曲线不一致")


def _window_input(request):
    material = trajectory(request.trajectory_id)
    dataset = v3_native.dataset(material["route"]["z_layer_msl_m"], request.dataset_id)
    now = _current_utc()
    body = request.model_dump(mode="json")
    body.update(departure_at=now.isoformat(), evaluated_at_epoch=now.timestamp(), route_id=request.route_id or request.trajectory_id)
    return body, dataset


def start_windows(request):
    body, dataset = _window_input(request)
    key = hashlib.sha256(json.dumps([request.trajectory_id, dataset["dataset_id"], body["thresholds"],body["wind_shear"],
                                    int(body["evaluated_at_epoch"]//600)],sort_keys=True).encode()).hexdigest()
    with WINDOW_LOCK:
        previous = WINDOW_KEYS.get(key)
        if previous in WINDOW_JOBS and WINDOW_JOBS[previous]["status"] != "cancelled":
            return window_snapshot(previous)
        active = sum(not row["future"].done() for row in WINDOW_JOBS.values() if row.get("future"))
        if active >= 8:
            raise ValueError("V3 通航窗口任务较多，请稍后重试")
        # Retain material independently; only short-lived job state is pruned.
        for token,row in list(WINDOW_JOBS.items()):
            if row.get("finished") and monotonic()-row["finished"]>1800:
                WINDOW_JOBS.pop(token,None)
        token = str(uuid4())
        WINDOW_KEYS[key] = token
        state = {"job_id":token,"status":"queued","revision":0,"result":None,"cancel":Event(),
                 "created":monotonic(),"deadline":monotonic()+max(1,float(os.getenv("WRF_V3_WINDOW_TIMEOUT_SECONDS","600"))),
                 "trajectory_id":request.trajectory_id,"dataset_id":dataset["dataset_id"],"message":"等待固定航线窗口评估"}
        WINDOW_JOBS[token] = state
        state["future"] = WINDOW_POOL.submit(_run_windows,state,body,dataset)
        return window_snapshot(token)


def _run_windows(state, body, dataset):
    def publish(result):
        checkpoint()
        with WINDOW_LOCK:
            if state["status"] in {"cancelled","timed_out"}:
                return
            state["result"] = result
            state["revision"] += 1
    try:
        with WINDOW_LOCK:
            if state["cancel"].is_set():
                return
            state.update(status="running",message="正在保守检查连续起飞区间")
        with planning_scope(state["deadline"],state["cancel"]):
            result = run_worker("windows",body,dataset,publish)
        with WINDOW_LOCK:
            if state["status"] == "running" and not state["cancel"].is_set():
                state.update(result=result,status="completed",message="固定航线通航窗口评估完成")
                state["revision"] += 1
    except PlanningTimedOut:
        with WINDOW_LOCK:
            if state["status"] != "cancelled":
                state.update(status="timed_out",message="达到窗口计算时限，保留已确认区间；其余时段未确认")
    except PlanningCancelled:
        with WINDOW_LOCK:
            if state["status"] != "timed_out":
                state.update(status="cancelled",message="通航窗口评估已取消")
    except Exception as exc:
        with WINDOW_LOCK:
            if state["status"] not in {"cancelled","timed_out"}:
                state.update(status="failed",message=str(exc))
    finally:
        with WINDOW_LOCK:
            state["finished"] = monotonic()
            if state["result"]:
                result=dict(state["result"])
                result["refresh_at"] = datetime.fromtimestamp(_current_utc().timestamp()+600,timezone.utc).isoformat()
                if state["status"] != "completed":
                    result.update(complete=False,coverage_incomplete=True)
                state["result"] = result
                state["revision"] += 1


def window_snapshot(token, since_revision=None):
    with WINDOW_LOCK:
        state=WINDOW_JOBS[token]
        if state["status"] not in {"completed","failed","timed_out","cancelled"} and monotonic()>=state["deadline"]:
            state.update(status="timed_out",message="达到窗口计算时限，保留已确认区间",finished=monotonic())
            state["cancel"].set()
            if state.get("future"):
                state["future"].cancel()
        return {key:state[key] for key in ("job_id","status","revision","trajectory_id","dataset_id","message")} | {
            "result":state["result"] if since_revision != state["revision"] else None,
            "elapsed_seconds":round(monotonic()-state["created"],2),"timeout_seconds":state["deadline"]-state["created"]}


def cancel_windows(token):
    with WINDOW_LOCK:
        state=WINDOW_JOBS[token]
        if state["status"] not in {"completed","failed","timed_out","cancelled"}:
            state["cancel"].set()
            if state.get("future"):
                state["future"].cancel()
            state.update(status="cancelled",message="通航窗口评估已取消",finished=monotonic())
        return window_snapshot(token)


def finalize_plan(result):
    from .models import V3WindowRequest
    info = trajectory(result["trajectory_id"])
    context = info.get("platform_constraints", {})
    request = V3WindowRequest(trajectory_id=result["trajectory_id"],dataset_id=result["dataset_id"],**context)
    result["window_job_id"] = start_windows(request)["job_id"]


def profile(request):
    material = trajectory(request.trajectory_id)
    dataset = v3_native.dataset(material["route"]["z_layer_msl_m"],request.dataset_id)
    return run_worker("profile",request.model_dump(mode="json"),dataset,budget_seconds=60)


def snapshot(request):
    material = trajectory(request.trajectory_id)
    dataset = v3_native.dataset(material["route"]["z_layer_msl_m"],request.dataset_id)
    return run_worker("snapshot",request.model_dump(mode="json"),dataset,budget_seconds=60)


def frame(dataset_id, at):
    dataset = v3_native.get_dataset(dataset_id)
    key=(dataset_id,at.isoformat())
    with FRAME_LOCK:
        if key in FRAMES:
            FRAMES.move_to_end(key)
            return FRAMES[key]
    value=run_worker("frame",{"departure_at":at.isoformat()},dataset,budget_seconds=45)
    with FRAME_LOCK:
        FRAMES[key]=value
        if len(FRAMES)>48:
            FRAMES.popitem(last=False)
    return value


def shutdown():
    with WINDOW_LOCK:
        for state in WINDOW_JOBS.values():
            state["cancel"].set()
    WINDOW_POOL.shutdown(wait=False,cancel_futures=True)
