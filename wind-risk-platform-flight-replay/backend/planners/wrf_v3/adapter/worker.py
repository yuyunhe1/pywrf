"""Isolated numerical worker. This is not the upstream execution CLI."""
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import traceback

BASE=Path(__file__).resolve().parents[1]
BACKEND=BASE.parents[1]
sys.path[:0]=[str(BASE/"vendor"),str(BACKEND),str(BASE)]
os.environ["PYTHONDONTWRITEBYTECODE"]="1"


class BudgetExpired(BaseException):
    pass


def atomic(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(",",":")),encoding="utf-8")
    os.replace(tmp,path)


def verify_vendor():
    manifest=json.loads((BASE/"UPSTREAM_MANIFEST.json").read_text(encoding="utf-8"))
    for row in manifest["files"]:
        if hashlib.sha256((BASE/"vendor"/row["path"]).read_bytes()).hexdigest()!=row["sha256"]:
            raise ValueError("V3 源码副本哈希不一致："+row["path"])


def planning_activity():
    forbidden=[key for key in sys.modules if key.endswith((".plant",".engine.controller",".dispatch",".pipeline_v4",".local_native")) and key.startswith("phase_g61.")]
    if forbidden:
        raise ValueError("执行模块不得进入平台规划进程："+str(forbidden))
    return {"planning_only":True,"execution_modules_loaded":forbidden,"injected_deviations":False}


def main(input_path,output):
    job=json.loads(Path(input_path).read_text(encoding="utf-8"))
    output=Path(output)
    deadline=time.monotonic()+float(job.get("budget_seconds",600.))
    def check():
        if time.monotonic()>=deadline:
            raise BudgetExpired()
    revision=0
    held=None
    def publish(result):
        nonlocal revision,held
        check()
        result["planning_activity"]=planning_activity()
        revision+=1
        held=result
        atomic(output/f"result_{revision:05d}.json",result)
        atomic(output/"progress.json",{"revision":revision,"file":f"result_{revision:05d}.json"})
    try:
        import numpy,scipy
        if sys.version_info[:3]!=(3,13,5) or numpy.__version__!="2.1.3" or scipy.__version__!="1.15.3":
            raise ValueError("V3 数值环境须为 Python 3.13.5 / NumPy 2.1.3 / SciPy 1.15.3")
        verify_vendor()
        from adapter.native import NativeWind,epoch
        request=job["request"]
        provider=NativeWind(job["dataset"],epoch(request["departure_at"]),check)
        operation=job["operation"]
        if operation=="plan":
            from adapter.planner import plan
            request["_progress_path"]=str(output/"search_progress.json")
            result=plan(provider,request,job["artifact_root"],publish,atomic,check)
        elif operation in ("profile","windows","snapshot"):
            from adapter.curve import Curve
            directory=Path(job["artifact_root"])/"trajectories"
            token=request["trajectory_id"]
            curve_file=directory/(token+".npz")
            if hashlib.sha256(curve_file.read_bytes()).hexdigest()!=token:
                raise ValueError("V3 连续曲线材料哈希不一致")
            curve=Curve.load(curve_file)
            if curve.z!=provider.spec["altitude_msl_m"]:
                raise ValueError("V3 连续曲线高度与预报数据不一致")
            if operation in ("profile","snapshot"):
                from adapter.profile import build_profile
                if operation=="snapshot":
                    from adapter.native import FrozenWind,iso
                    result=build_profile(curve,FrozenWind(provider),request,original_time=True)
                    result["time_model"]="snapshot"
                    for sample in result["analysis"]["samples"]:
                        sample["eta"]=iso(provider.epoch)
                else:
                    result=build_profile(curve,provider,request)
            else:
                from adapter.certificate import windows
                result=windows(curve,provider,request,publish,check)
        elif operation=="frame":
            from adapter.frame import frame
            result=frame(provider,request)
        else:
            raise ValueError("未知 V3 工作任务")
        atomic(output/"final.json",{"status":"completed","result":result,
            "activity":planning_activity()})
    except BudgetExpired:
        atomic(output/"final.json",{"status":"timed_out","result":held,"message":"达到计算时限，保留已确认结果"})
    except BaseException as exc:
        atomic(output/"final.json",{"status":"failed","result":held,"message":str(exc),"traceback":traceback.format_exc()})


if __name__=="__main__":
    main(*sys.argv[1:])
