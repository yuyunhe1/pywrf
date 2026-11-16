"""F0 numerical planning adapted to open platform requests and ranked candidates.

Only pure seed/shooting mathematics and independent validators are called.
No plant, controller, execution dispatch or online takeover is started.
"""
import copy
import hashlib
import json
import math
import time
from uuid import uuid4
from pathlib import Path
from contextlib import contextmanager
import numpy as np
from phase2b4.solver_a.general_wind_connector import GeneralWindConnector
from phase3a_implementation.synthetic import ConstantWindProvider
from phase3b.validator import validate
from phase3b.integrator import SpatialIntegrationError
from phase_g61.uncovered_environments_v1.initial_seed import reserve_seed
from phase_g61.uncovered_environments_v1.warm_recovery import trial
from phase_g61.uncovered_environments_v1.enclosed_v3.runtime import connector as backend
from phase_g61.request_runtime_confirmation_v1.runtime_v3.initial_two_zoh import attempt as two_zoh
from .native import VA,R0,heading_to_local,iso
from .curve import Curve, partition_native_controls
from .profile import build_profile
from .validation import validate_route


class CandidateFound(Exception):
    def __init__(self,value):
        self.value=value


class CandidateStageExpired(BaseException):
    pass


@contextmanager
def candidate_budget(provider, seconds):
    previous=provider.check
    until=time.monotonic()+seconds
    def check_stage():
        previous()
        if time.monotonic()>=until:
            raise CandidateStageExpired("V3 局部连接达到阶段预算")
    provider.check=check_stage
    try:
        yield
    finally:
        provider.check=previous


def free_heading_candidate(startxy,goalxy,provider,z,check,turn_angle_hint=0.):
    """Two-variable position shooting in the V3 zero-turn core family.

    Endpoint air headings are free. Airspeed, actual UTC queries and the
    original independent validator remain unchanged.
    """
    from .free_core import shoot
    startxy,goalxy=np.asarray(startxy),np.asarray(goalxy)
    delta=goalxy-startxy
    length=float(np.linalg.norm(delta))
    tangent=delta/length
    normal=np.array([-tangent[1],tangent[0]])
    q=provider.wind_query(*startxy,z,0.)
    if not q.get("valid"):
        raise ValueError("起点原生风场无效："+str(q.get("status")))
    wind=np.array([q["wx"],q["wy"]])
    if np.linalg.norm(wind)>=VA:
        raise ValueError("起点风速超出 V3 固定空速适用范围")
    cross=float(wind@normal)
    psi=math.atan2(tangent[1],tangent[0])-math.asin(cross/VA)
    speed=float(wind@tangent)+math.sqrt(VA*VA-cross*cross)
    hint=length/speed
    horizon=provider.times[-1]-provider.epoch
    if hint<=0 or horizon<=0:
        raise ValueError("WRF 数据未覆盖航程")
    control=max(-.9*VA/R0,min(.9*VA/R0,turn_angle_hint/hint))
    start,duration,nodes,initialization_stats=shoot(startxy,goalxy,provider,z,psi,hint,horizon,check,control)
    validation_step=4.
    route={"type":"piecewise_constant","control_interpolation":"ZOH","state_start":start.tolist(),
           "state_goal":[*goalxy.tolist(),float(nodes[-1][2])],"parameters":{"Va":VA,"R0":R0},
           "z_layer_msl_m":z,"controls":[control],"durations":[duration],"states":nodes,
           "frame_id":provider.frame_id,"height_datum":provider.height_datum,
           "platform_initialization":"FREE_ENDPOINT_HEADING_CONSTANT_TURN_CORE" if control else "FREE_ENDPOINT_HEADING_ZERO_TURN_CORE",
           "seed_policy":"COARSE_INITIALIZATION_UPSTREAM_STRICT_CORRECTION",
           "initialization_stats":initialization_stats,
           "independent_validation_max_step_s":validation_step}
    receipt=validate_route(route,route["controls"],duration,provider,max_step=validation_step)
    return route,receipt


def connect_candidates(start,goal,provider,z,check):
    """Same ordered constant seeds, reserve and shooting as the V3 F0 worker."""
    check()
    q=provider.wind_query(*start[:2],z,0.)
    if not q.get("valid") or math.hypot(q["wx"],q["wy"])>=VA:
        raise ValueError("V3 起点风场无效或水平风速达到固定空速："+str(q.get("status")))
    pool=GeneralWindConnector().connect(start,goal,[q["wx"],q["wy"]],{"Va":VA,"R0":R0})
    constant=ConstantWindProvider(q["wx"],q["wy"],0.,roi=provider.roi)
    horizon=provider.times[-1]-provider.epoch-getattr(provider,"offset",0.)
    if horizon<=0:
        raise ValueError("WRF 预报未覆盖飞行时间")
    class Engine(backend.SpatialWindConnector):
        def _validation_for_candidate(self,tr,total,index,stage):
            return validate_route(tr,tr["controls"],total,provider)
        def _candidate(self,tr,family,stage,c,index):
            if c["valid"] and abs(c["diagnostic_final_state"][2]-goal[2])<=1e-7 and math.fsum(tr["durations"])<=horizon and max(map(abs,tr["controls"]))<=.9*VA/R0+1e-12:
                raise CandidateFound((tr,c))
    first_hint=None
    for index,item in enumerate(pool.get("candidates",[])):
        check()
        original=copy.deepcopy(item.get("trajectory"))
        if not original or abs(original["states"][-1][2]-goal[2])>1e-7:
            continue
        original.update(z_layer_msl_m=z,frame_id=provider.frame_id,height_datum=provider.height_datum)
        total=math.fsum(original["durations"])
        c=validate(original,original["controls"],total,constant)
        if not c["valid"]:
            continue
        seed=reserve_seed(original,fraction=.9)
        if first_hint is None:
            first_hint=math.fsum(seed["durations"])
        engine=Engine(provider,z_layer_msl_m=z,optimize=False)
        engine.reference_seed_route=seed
        engine.reference_seed_offset=0.
        engine.remaining_physical_s=min(horizon,max(20.,4*math.fsum(seed["durations"])+10.))
        try:
            if all(u==0. for u in seed["controls"]) and all(d>0 for d in seed["durations"]):
                direct=engine._trajectory(np.asarray(start),np.asarray(goal),seed["controls"],seed["durations"])
                c=engine._validation_for_candidate(direct,math.fsum(direct["durations"]),index,"ENDPOINT_RECOVERY")
                engine._candidate(direct,"POSITIVE_STRAIGHT_INITIAL","ENDPOINT_RECOVERY",c,index)
            trial(engine,np.asarray(start),np.asarray(goal),backend,index)
        except CandidateFound as found:
            yield found.value
    if first_hint is not None:
        check()
        engine=Engine(provider,z_layer_msl_m=z,optimize=False)
        try:
            two_zoh(engine,np.asarray(start),np.asarray(goal),backend,len(pool.get("candidates",[]))+1,
                    first_hint,min(horizon,max(20.,4*first_hint+10.)),.9)
        except CandidateFound as found:
            yield found.value


def plan(provider,request,artifacts,publish,atomic,check):
    from app.route_jobs import candidate_score
    from app.planning_objective import profile_objective, OBJECTIVE_MODEL
    from app.models import Thresholds
    from .corridors import proposals, states_for_path
    c=provider.coordinates
    startxy=c.lonlat_to_local(*request["start"])
    goalxy=c.lonlat_to_local(*request["end"])
    if math.dist(startxy,goalxy)<.1:
        raise ValueError("起终点间距必须大于0.1米")
    automatic=math.atan2(goalxy[1]-startxy[1],goalxy[0]-startxy[0])
    hs=request.get("start_heading_deg") if request.get("constrain_headings",False) else None
    hg=request.get("end_heading_deg") if request.get("constrain_headings",False) else None
    ps=automatic if hs is None else heading_to_local(c,request["start"][0],hs)
    pg=automatic if hg is None else heading_to_local(c,request["end"][0],hg)
    pg+=2*math.pi*round((ps-pg)/(2*math.pi))
    start,goal=[*startxy,ps],[*goalxy,pg]
    z=request["altitude_msl_m"]
    for point in (start,goal):
        q=provider.wind_query(*point[:2],z,0.)
        if not q.get("valid"):
            raise ValueError("起终点不在原生 WRF 可用高度或区域内："+str(q.get("status")))
    best=None
    score=None
    counter=0
    errors=[]
    phase="native_corridor"
    began=time.monotonic()
    activity={"attempts":[],"first_feasible_seconds":None,"direct_preview_published":False}
    def note():
        if request.get("_progress_path"):
            atomic(request["_progress_path"],activity)
    def admit(route,receipt,curve=None):
        nonlocal best,score,counter
        check()
        if not receipt["valid"]:
            errors.append("独立节点复验未通过："+str(receipt["failures"]))
            if activity["attempts"]:
                activity["attempts"][-1].update(message=errors[-1],C_residual=receipt.get("constraint_residual"))
                note()
            return False
        curve=curve if curve is not None else Curve.from_route(route,provider)
        endpoint_error=float(np.linalg.norm(curve.point(curve.end)[:2]-np.asarray(route["state_goal"][:2])))
        from .geometry import check_geometry
        collision=check_geometry(curve,provider.roi,check)
        if not collision["valid"]:
            return False
        from .certificate import Certificate
        certificate=Certificate(curve,provider,request,check).evaluate(provider.epoch)
        if certificate["status"]!="allowed":
            errors.append(certificate["reason"])
            if activity["attempts"]:
                activity["attempts"][-1]["message"]=certificate["reason"]
                note()
            return False
        directory=Path(artifacts)/"trajectories"
        directory.mkdir(parents=True,exist_ok=True)
        staging=directory/("candidate_"+uuid4().hex+".npz")
        curve.save(staging,context=json.dumps([provider.spec["dataset_id"],provider.epoch,request],sort_keys=True))
        trajectory_id=hashlib.sha256(staging.read_bytes()).hexdigest()
        filename=directory/(trajectory_id+".npz")
        if not filename.exists():
            staging.replace(filename)
        else:
            staging.unlink()
        cfg={**request,"trajectory_id":trajectory_id}
        profile=build_profile(curve,provider,cfg,original_time=True,certificate=certificate)
        if not profile["analysis"]["navigation_allowed"]:
            errors.append(profile["decision"]["reason"])
            return False
        samples=profile["analysis"]["samples"]
        points=[[s["lon"],s["lat"],z] for s in samples]
        metadata={"version":"WRF_V3_PLATFORM_V1","trajectory_id":trajectory_id,"curve_sha256":trajectory_id,
                  "route":route,"dataset":provider.spec,"departure_at":iso(provider.epoch),
                  "source_artifact_sha256":"7642c11673ae9b163de420a8958b2cf72636eb62ba73eb92ad8bc5dedcd9cb4f",
                  "C":{k:receipt.get(k) for k in ("valid","validation_status","method","numerical_max_step_s","thresholds","terminal_residual","constraint_residual","domain_coverage","query_count","upstream_valid","upstream_failures","upstream_thresholds","position_error")},
                  "collision":collision,"start_heading_deg":hs,"end_heading_deg":hg,
                  "display_points":points,"platform_constraints":{"thresholds":request["thresholds"],"wind_shear":request.get("wind_shear",{})},
                  "execution_simulation":False,"initial_reserve_fraction":.9}
        metadata.update(endpoint_tolerance_m=1.,endpoint_error_m=endpoint_error)
        metadata["endpoint_headings_constrained"]=bool(request.get("constrain_headings",False))
        atomic(directory/(trajectory_id+".json"),metadata)
        result={"planner_type":"wrf_v3","requested_planner_type":"wrf_v3","time_aware":True,
                "time_model":"constant_airspeed","airspeed_mps":VA,"altitude_reference":"MSL","altitude_msl_m":z,
                "trajectory_id":trajectory_id,"v3":{"trajectory_id":trajectory_id,"altitude_msl_m":z,"airspeed_mps":VA,
                    "time_model":"constant_airspeed","version":"WRF_V3_PLATFORM_V1","endpoint_tolerance_m":1.},
                "endpoint_tolerance_m":1.,"endpoint_error_m":endpoint_error,
                "points":points,"level":profile["level"],"departure_at":profile["departure_at"],"arrival_at":profile["arrival_at"],
                "dataset_id":provider.spec["dataset_id"],"average_ground_speed_mps":profile["average_ground_speed_mps"],
                "planning_strategy":request["planning_strategy"],"aircraft_model":"fixed_wing","aircraft_model_label":"固定翼无人机",
                "planning_strategy_label":{"distance_priority":"路程优先","balanced":"均衡避险","wind_avoidance":"避风优先"}[request["planning_strategy"]],
                "distance_km":profile["analysis"]["total_distance_km"],"analysis":profile["analysis"],"segments":profile["segments"],
                "flight_profile":profile,"multi_altitude":False,"reference_route":False,"global_optimality_guaranteed":False,
                "metadata":{"source":"WRF · V3 原生时空规划","cycle":provider.spec["cycle"],"level":profile["level"],
                    "valid_time":profile["departure_at"],"dataset_id":provider.spec["dataset_id"],"altitude_reference":"MSL"}}
        result["route_context"]={"route_id":trajectory_id,"trajectory_id":trajectory_id,"ephemeral":True,"name":"V3 新规划航线",
                                 "points":points,"level":profile["level"],"v3":result["v3"],
                                 "dataset_id":provider.spec["dataset_id"],"departure_at":profile["departure_at"]}
        result.update(objective_cost=profile_objective(profile,request["planning_strategy"],Thresholds(**request["thresholds"])),
                      objective_model=OBJECTIVE_MODEL,search_phase=phase,route_status="feasible",planning_diagnostics=activity)
        rank=candidate_score(result)
        if score is None or rank<score:
            score,best=rank,result
            counter+=1
            if activity["first_feasible_seconds"] is None:
                activity["first_feasible_seconds"]=time.monotonic()-began
            note()
            publish(result)
        return True
    searched=False
    for proposal in proposals(provider,request,check):
        searched=True
        phase=proposal["phase"]
        activity["attempts"].append({"phase":phase,"spatial_nodes":proposal["spatial_nodes"],"guide_points":len(proposal["points"])})
        note()
        try:
            dense=[proposal["points"][0]]
            for a,b in zip(proposal["points"],proposal["points"][1:]):
                ax,ay=c.lonlat_to_local(*a)
                bx,by=c.lonlat_to_local(*b)
                count=max(1,math.ceil(math.hypot(bx-ax,by-ay)/2000.))
                dense.extend([a[0]+(b[0]-a[0])*i/count,a[1]+(b[1]-a[1])*i/count] for i in range(1,count+1))
            states=states_for_path(provider,dense,z,ps if hs is not None else None,pg if hg is not None else None)
            # A direct spatial result is an actual search outcome. Free-core
            # shooting may now be tried, never before this corridor search.
            if len(proposal["points"])==2 and not request.get("constrain_headings",False):
                for angle in (0.,.15,-.15):
                    route,receipt=free_heading_candidate(startxy,goalxy,provider,z,check,angle)
                    if admit(route,receipt):
                        start,goal=route["state_start"],route["state_goal"]
                        break
                continue
            assembled=None
            current=states[0]
            elapsed=0.
            for leg_index,target in enumerate(states[1:],start=1):
                check()
                activity["attempts"][-1].update(connected_segments=0 if assembled is None else len(assembled["durations"]),current_elapsed_seconds=elapsed)
                note()
                shifted=provider.shifted(elapsed)
                with candidate_budget(provider,30.):
                    from .position_core import connect
                    if leg_index<len(states)-1 or not request.get("constrain_headings",False):
                        chosen=connect(current,target[:2],shifted,z,check)
                    else:
                        chosen=next(connect_candidates(current,target,shifted,z,check),None)
                if chosen is None:
                    assembled=None
                    break
                route,receipt=chosen
                if assembled is None:
                    assembled=copy.deepcopy(route)
                else:
                    assembled["controls"]+=route["controls"]
                    assembled["durations"]+=route["durations"]
                    assembled["states"]+=route["states"][1:]
                current=receipt["diagnostic_final_state"]
                elapsed+=math.fsum(route["durations"])
            if assembled:
                assembled["state_goal"]=[*states[-1][:2],states[-1][2] if request.get("constrain_headings",False) else float(current[2])]
                # Preserve the controls and the original C thresholds. A
                # continuous physical replay supplies nodes at native spatial,
                # vertical and UTC interpolation boundaries before validation.
                continuous=Curve.from_route(assembled,provider)
                assembled=partition_native_controls(assembled,continuous,provider)
                receipt=validate_route(assembled,assembled["controls"],math.fsum(assembled["durations"]),provider)
                if receipt["failures"]==["INTERIOR_NODE_RESIDUAL"]:
                    # The unchanged validator's original default step is 0.1 s.
                    # Use it when the faster replay cannot resolve node error.
                    receipt=validate_route(assembled,assembled["controls"],math.fsum(assembled["durations"]),provider,max_step=.1)
                admit(assembled,receipt,continuous)
        except (ValueError,SpatialIntegrationError,CandidateStageExpired) as exc:
            errors.append(str(exc))
            activity["attempts"][-1]["message"]=str(exc)
            note()
    phase="v3_connection_refinement"
    # A failed spatial hint does not forbid native V3 alternatives. When no
    # hint exists, no unsearched direct trajectory can become a first preview.
    if searched and not request.get("constrain_headings",False):
        # Small single-turn arcs often avoid a neighbouring unavailable native
        # cell without invoking the much larger heading-constrained seed pool.
        for angle in (0.,.15,-.15,.4,-.4):
            try:
                route,receipt=free_heading_candidate(startxy,goalxy,provider,z,check,angle)
                if admit(route,receipt):
                    start,goal=route["state_start"],route["state_goal"]
                    break
            except (ValueError,SpatialIntegrationError) as exc:
                errors.append(str(exc))
    if searched:
        try:
            with candidate_budget(provider,30.):
                for route,receipt in connect_candidates(start,goal,provider,z,check):
                    try:
                        admit(route,receipt)
                    except ValueError as exc:
                        errors.append(str(exc))
        except CandidateStageExpired as exc:
            errors.append(str(exc))
    if best is None:
        raise ValueError("V3 未找到通过独立验收及平台硬约束的航线"+("："+errors[-1] if errors else ""))
    best["planning_message"]=f"当前最佳：已发布{counter}次改进；预算内搜索，不保证全局最优"
    return best
