from copy import deepcopy
import hashlib
import json
import io
import struct
from pathlib import Path
from threading import Event
from time import monotonic
from types import SimpleNamespace
from concurrent.futures import Future

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app import main,route_storage,v3_service,v3_native
from app.models import RoutePlanRequest,V3WindowRequest
from app.planning_control import planning_scope,PlanningCancelled
from app.route_jobs import RouteJobManager

TOKEN="a"*64
DATASET="v3_"+"b"*60


def test_vendor_snapshot_has_exactly_the_frozen_main_dependencies():
    base=Path(__file__).resolve().parents[1]/"planners/wrf_v3"
    manifest=json.loads((base/"UPSTREAM_MANIFEST.json").read_text(encoding="utf-8"))
    assert len(manifest["files"])==148
    assert sum(row["path"].endswith(".py") for row in manifest["files"])==144
    for row in manifest["files"]:
        assert hashlib.sha256((base/"vendor"/row["path"]).read_bytes()).hexdigest()==row["sha256"]
        original=Path(row["absolute_path"])
        if original.is_file():
            assert hashlib.sha256(original.read_bytes()).hexdigest()==row["sha256"]


def test_v3_job_routes_to_native_dataset_and_retains_ranked_progress(monkeypatch):
    manager=RouteJobManager(workers=1,timeout_seconds=10)
    monkeypatch.setattr(main,"route_jobs",manager)
    captured=[]
    native={"dataset_id":DATASET,"altitude_msl_m":300.,"records":[],"cycle":"2026-11-03T00:00:00Z"}
    monkeypatch.setattr(v3_native,"dataset",lambda altitude,dataset_id=None:deepcopy(native))
    monkeypatch.setattr(main,"prepare_temporal_request",lambda request:pytest.fail("V3 must not use the regular AGL dataset"))
    def compute(request,publish,dataset):
        captured.append((request,dataset))
        for distance in (4.,2.):
            result={"points":[[118,31,300],[118.01,31,300]],"distance_km":distance,"planning_strategy":"distance_priority",
                    "trajectory_id":TOKEN,"analysis":{"navigation_allowed":True,"danger_ratio":0,"max_wind_speed":1,"mean_wind_speed":1}}
            publish(result)
        return result
    monkeypatch.setattr(v3_service,"compute_plan",compute)
    monkeypatch.setattr(v3_service,"finalize_plan",lambda result:result.update(window_job_id="window-started"))
    try:
        client=TestClient(main.app)
        response=client.post("/api/route/plan/jobs",json={"start":[118,31],"end":[118.01,31],"source":"wrf","level":"100m AGL",
            "planner_type":"wrf_v3","planning_strategy":"distance_priority","altitude_msl_m":300,"start_heading_deg":90,"end_heading_deg":90})
        assert response.status_code==202,response.text
        token=response.json()["job_id"]
        manager.jobs[token].future.result(timeout=3)
        result=client.get("/api/route/plan/jobs/"+token).json()
        assert result["status"]=="completed" and result["revision"]>=3
        assert result["result"]["distance_km"]==2
        assert result["result"]["window_job_id"]=="window-started"
        request,dataset=captured[0]
        assert request.time_aware and request.departure_at.tzinfo
        assert request.dataset_id==DATASET and request.altitude_msl_m==300 and request.start_heading_deg==90
    finally:
        manager.shutdown()


def test_v3_input_validation_keeps_the_physics_and_utc_contract():
    base={"start":[118,31],"end":[118.01,31],"level":"300m MSL","planner_type":"wrf_v3"}
    for values in ({"start_heading_deg":-1},{"end_heading_deg":361},{"altitude_msl_m":float("nan")},
                   {"departure_at":"2026-11-03T12:00:00"},{"source":"gfs"},{"start":[181,31]}):
        with pytest.raises(ValidationError):
            RoutePlanRequest(**{**base,**values})
    with pytest.raises(ValidationError):
        V3WindowRequest(trajectory_id="../escape")


def test_saved_v3_route_preserves_material_and_exports_absolute_altitude(tmp_path,monkeypatch):
    monkeypatch.setenv("WRF_V3_ARTIFACT_DIR",str(tmp_path/"artifacts"))
    monkeypatch.setattr(route_storage,"DB_PATH",tmp_path/"routes.sqlite3")
    monkeypatch.setattr(main,"EXPORT_DIR",tmp_path/"exports")
    main.EXPORT_DIR.mkdir()
    directory=v3_service.artifact_root()/"trajectories";directory.mkdir(parents=True)
    points=[[118.,31.,300.],[118.01,31.,300.]]
    (directory/(TOKEN+".json")).write_text(json.dumps({"route":{"z_layer_msl_m":300},"display_points":points}),encoding="utf-8")
    payload={"name":"V3 test","start":points[0][:2],"end":points[-1][:2],"points":points,"level":"300m MSL",
             "v3":{"trajectory_id":TOKEN,"altitude_msl_m":300}}
    client=TestClient(main.app)
    response=client.post("/api/routes",json=payload)
    assert response.status_code==201,response.text
    record=response.json()
    assert client.get("/api/routes/"+record["route_id"]).json()["v3"]["time_model"]=="constant_airspeed"
    # Locate the export using the API's returned filename, independently of
    # the platform's existing default export-folder configuration.
    file_name=record["exported_json"]["file_name"]
    exported=client.get("/api/exported-routes/"+file_name).json()
    assert exported["altitude_mode"]=="MSL" and exported["airspeed_mps"]==10
    assert all(item["frame"]==0 and item["command"]==16 and item["altitude"]==300 for item in exported["mission_items"])
    assert all(point["altitude_agl_m"] is None for point in exported["waypoints"])
    payload["points"][1][0]+=.1
    assert client.put("/api/routes/"+record["route_id"],json=payload).status_code==400


def test_cancel_terminates_the_owned_numerical_worker(tmp_path,monkeypatch):
    monkeypatch.setenv("WRF_V3_ARTIFACT_DIR",str(tmp_path))
    cancellation=Event()
    class Process:
        def __init__(self,*args,**kwargs):self.returncode=None;self.terminated=False
        def poll(self):return self.returncode
        def wait(self,timeout=None):
            if self.returncode is None:
                cancellation.set()
                raise v3_service.subprocess.TimeoutExpired("test-worker",timeout)
            return self.returncode
        def terminate(self):self.returncode=-1;self.terminated=True
    process=Process()
    monkeypatch.setattr(v3_service.subprocess,"Popen",lambda *args,**kwargs:process)
    with pytest.raises(PlanningCancelled),planning_scope(monotonic()+5,cancellation):
        v3_service.run_worker("plan",{}, {})
    assert process.terminated


def test_window_and_profile_endpoints_bind_to_trajectory_identity(monkeypatch):
    seen=[]
    monkeypatch.setattr(v3_service,"start_windows",lambda request:seen.append(request) or {"job_id":"fixed-route","trajectory_id":request.trajectory_id})
    client=TestClient(main.app)
    result=client.post("/api/route/departure-windows/jobs",json={"trajectory_id":TOKEN,"dataset_id":DATASET})
    assert result.status_code==202 and seen[0].trajectory_id==TOKEN
    monkeypatch.setattr(v3_service,"profile",lambda request:{"trajectory_id":request.trajectory_id,"departure_at":request.departure_at.isoformat(),"time_model":"constant_airspeed"})
    result=client.post("/api/route/v3/flight-profile",json={"trajectory_id":TOKEN,"dataset_id":DATASET,"departure_at":"2026-11-03T10:00:00+08:00"})
    assert result.status_code==200 and result.json()["trajectory_id"]==TOKEN


def classic_header(cycle):
    def string(value):
        value=value.encode("ascii")
        return struct.pack(">I",len(value))+value+b"\0"*((-len(value))%4)
    attrs=b"".join(string(name)+struct.pack(">II",2,len(cycle))+cycle.encode("ascii")+b"\0"*((-len(cycle))%4)
        for name in ("START_DATE","SIMULATION_START_DATE"))
    return b"CDF\x02"+struct.pack(">IIIII",1,0,0,12,2)+attrs


def test_native_catalog_uses_real_run_lineage_and_keeps_previous_until_two_new_hours(tmp_path):
    rows=[]
    for i,cycle in enumerate(("2026-11-03_00:00:00","2026-11-03_00:00:00","2026-11-03_06:00:00","2026-11-03_06:00:00")):
        path=tmp_path/f"{i}.nc";path.write_bytes(classic_header(cycle))
        rows.append({"path":str(path),"utc":f"2026-11-03T{(i//2)*6+i%2:02}:00:00Z","cycle":"wrong_folder_date"})
    assert v3_native.partition(rows[:3])["records"][-1]["utc"]=="2026-11-03T01:00:00Z"
    result=v3_native.partition(rows)
    assert len(result["batches"])==2 and len(result["records"])==2
    assert all(row["cycle"]=="2026-11-03T06:00:00Z" for row in result["records"])


def test_native_sync_preserves_old_hashed_material_on_remote_republication(tmp_path,monkeypatch):
    monkeypatch.setenv("WRF_V3_NATIVE_DIR",str(tmp_path))
    monkeypatch.setattr(v3_native,"STOP",Event())
    monkeypatch.setattr(v3_native,"STATE",{"running":False})
    directory=tmp_path/"2026-11-04";directory.mkdir()
    previous=[];remote={}
    for hour in (0,1):
        stamp=f"2026-11-04_{hour:02}:00:00"
        filename="wrfout_d02_"+stamp
        path=directory/f"old-{hour}.nc"
        old=classic_header("2026-11-04_00:00:00")+b"old"
        path.write_bytes(old)
        target="/root/pyWRF-automation/wrf_output/2026-11-04/"+filename
        remote[target]=classic_header("2026-11-04_00:00:00")+b"new"
        previous.append({"path":str(path),"remote_path":target,"remote_size":len(old),"remote_mtime":1,
            "utc":stamp.replace("_","T")+"Z","expected_sha256":hashlib.sha256(old).hexdigest()})
    v3_native.atomic(tmp_path/"index.json",{"records":previous})
    class Sftp:
        def listdir_attr(self,path):
            if path.endswith("wrf_output"):
                return [SimpleNamespace(filename="2026-11-04")]
            return [SimpleNamespace(filename=key.rsplit("/",1)[1],st_size=len(value),st_mtime=2) for key,value in remote.items()]
        def stat(self,path):return SimpleNamespace(st_size=len(remote[path]),st_mtime=2)
        def get(self,path,target):Path(target).write_bytes(remote[path])
        def close(self):pass
    class Client:
        def exec_command(self,cmd,timeout):
            path=cmd.split(" -- ",1)[1]
            stream=io.BytesIO((hashlib.sha256(remote[path]).hexdigest()+"  "+path).encode())
            stream.channel=SimpleNamespace(recv_exit_status=lambda:0)
            return None,stream,io.BytesIO()
        def close(self):pass
    monkeypatch.setattr(v3_native.wrf_cache_provider,"_connect_sftp",lambda:(Client(),Sftp()))
    state=v3_native.sync_once()
    assert not state["last_error"] and state["available_records"]==2
    for old,new in zip(previous,v3_native.index()["records"]):
        assert Path(old["path"]).read_bytes().endswith(b"old")
        assert new["path"]!=old["path"] and Path(new["path"]).read_bytes().endswith(b"new")


def test_window_completion_cannot_overwrite_cancellation(monkeypatch):
    partial={"complete":False,"departure_windows":[{"start":"a","end":"b"}]}
    state={"status":"running","cancel":Event(),"deadline":monotonic()+10,"result":partial,"revision":1}
    def worker(*args,**kwargs):
        state.update(status="cancelled")
        state["cancel"].set()
        return {"complete":True,"departure_windows":[]}
    monkeypatch.setattr(v3_service,"run_worker",worker)
    v3_service._run_windows(state,{}, {})
    assert state["status"]=="cancelled"
    assert state["result"]["departure_windows"]==partial["departure_windows"]
    assert state["result"]["complete"] is False


def test_cancelling_a_queued_window_frees_its_queue_slot(monkeypatch):
    state={"job_id":"queued-window","status":"queued","revision":0,"result":None,"cancel":Event(),
        "created":monotonic(),"deadline":monotonic()+600,"trajectory_id":TOKEN,"dataset_id":DATASET,
        "message":"queued","future":Future()}
    monkeypatch.setattr(v3_service,"WINDOW_JOBS",{"queued-window":state})
    assert v3_service.cancel_windows("queued-window")["status"]=="cancelled"
    assert state["future"].cancelled()
