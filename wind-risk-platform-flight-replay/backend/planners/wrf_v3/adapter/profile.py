"""Retiming and visualization of a frozen continuous path at fixed airspeed."""
import math
import numpy as np
from scipy.integrate import solve_ivp
from .native import VA,iso,heading_to_true
from .certificate import Certificate,shear_limit
from .interval import I

COLORS={"一级风":"#28c76f","二级风":"#f4c95d","三级风":"#ff9f43","四级风":"#ea5455","大于四级":"#9b51e0","数据不足":"#94a3b8"}


def risk(speed,thresholds):
    return "一级风" if speed<=thresholds["safe"] else "二级风" if speed<=thresholds["notice"] else "三级风" if speed<=thresholds["warning"] else "四级风" if speed<=thresholds["danger"] else "大于四级"


def motion(curve,provider,parameter,elapsed):
    point=curve.point(parameter)
    derivative=curve.point(parameter,1)
    norm=float(np.linalg.norm(derivative[:2]))
    if norm<=0:
        raise ValueError("固定航线切向量无效")
    tangent=derivative[:2]/norm
    normal=np.array([-tangent[1],tangent[0]])
    w=provider.wind_query(float(point[0]),float(point[1]),curve.z,max(0.,float(elapsed)))
    if not w.get("valid"):
        raise ValueError("固定航线风场数据不足："+str(w.get("status")))
    vector=np.array([w["wx"],w["wy"]])
    cross=float(vector@normal)
    if np.linalg.norm(vector)>=VA or abs(cross)>=VA:
        raise ValueError("固定空速风补偿超出 V3 风场适用范围")
    along=float(vector@tangent)
    speed=along+math.sqrt(VA*VA-cross*cross)
    if speed<=0:
        raise ValueError("固定航线无法保持正向地速")
    psi=math.atan2(tangent[1],tangent[0])-math.asin(cross/VA)
    return point,norm,tangent,w,speed,psi,along,cross


def unavailable_profile(curve,provider,request,error):
    """Retain fixed geometry, without inventing a duration or an ETA."""
    verdict=Certificate(curve,provider,request,provider.check).evaluate(provider.epoch)
    status="blocked" if verdict["status"]=="blocked" else "unknown"
    reason=verdict["reason"] if status=="blocked" else str(error)
    local=np.array([curve.point(t)[:2] for t in curve.samples(max(5.,curve.end/1000))])
    distance=float(np.linalg.norm(np.diff(local,axis=0),axis=1).sum())/1000
    points=[list(provider.coordinates.local_to_lonlat(*p)) for p in local]
    analysis={"samples":[],"total_distance_km":distance,"max_wind_speed":None,"mean_wind_speed":None,
        "danger_ratio":None,"tailwind_ratio":None,"max_continuous_danger_km":None,
        "unknown_ratio":1. if status=="unknown" else 0.,"coverage_incomplete":status=="unknown",
        "navigation_allowed":False,"navigation_status":status,"navigation_decision_reason":reason,
        "risk_level":"不可通航" if status=="blocked" else "数据不足",
        "wind_shear":{"enabled":math.isfinite(shear_limit(request)),"max_horizontal_wind_shear":None,
                      "horizontal_wind_shear_profile":[]}}
    return {"trajectory_id":request["trajectory_id"],"route_id":request.get("route_id",request["trajectory_id"]),
        "route_name":request.get("name","V3 固定航线"),"dataset_id":provider.spec["dataset_id"],
        "forecast_cycle":provider.spec["cycle"],"level":f"{curve.z:g}m MSL","departure_at":iso(provider.epoch),
        "arrival_at":None,"time_model":"constant_airspeed","airspeed_mps":VA,"altitude_reference":"MSL",
        "altitude_msl_m":curve.z,"average_ground_speed_mps":None,"flight_duration_seconds":0.,
        "decision":{"status":status,"reason":reason},"analysis":analysis,
        "segments":[{"points":points,"status":status,"risk":analysis["risk_level"],"reason":reason,
                     "color":"#ea5455" if status=="blocked" else "#94a3b8"}],"events":[],
        "certificate":verdict,"execution_simulation":False,"timing_available":False,
        "frame_times":[iso(t) for t in provider.times],
        "bbox":[min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)]}


def build_profile(curve,provider,request,*,original_time=False,certify=True,certificate=None):
    departure=provider.epoch
    if original_time:
        elapsed_at=lambda parameter:float(parameter)
    else:
        def rhs(parameter,value):
            _,norm,_,_,speed,_,_,_=motion(curve,provider,parameter,value[0])
            return [norm/speed]
        try:
            timing=solve_ivp(rhs,(0.,curve.end),[0.],method="DOP853",rtol=1e-10,atol=1e-8,max_step=5.,dense_output=True)
        except ValueError as exc:
            return unavailable_profile(curve,provider,request,exc)
        if not timing.success:
            raise ValueError("固定空速航线时间积分失败")
        elapsed_at=lambda parameter:float(timing.sol(parameter)[0])
    duration=elapsed_at(curve.end)
    limits=request.get("thresholds",{"safe":1.5,"notice":3.3,"warning":5.4,"danger":7.9})
    samples=[]
    distance=0.
    previous_point=None
    shear_values=[]
    for parameter in curve.samples(max(1.,curve.end/5000)):
        elapsed=elapsed_at(parameter)
        try:
            point,norm,tangent,w,speed,psi,along,cross=motion(curve,provider,float(parameter),elapsed)
        except ValueError as exc:
            return unavailable_profile(curve,provider,request,exc)
        if previous_point is not None:
            distance+=float(np.linalg.norm(point[:2]-previous_point[:2]))
        previous_point=point
        lon,lat=provider.coordinates.local_to_lonlat(float(point[0]),float(point[1]))
        magnitude=math.hypot(w["wx"],w["wy"])
        samples.append({"lon":lon,"lat":lat,"distance_km":distance/1000,"elapsed_seconds":elapsed,
                        "reference_parameter":float(parameter),"eta":iso(departure+elapsed),"u":w["wx"],"v":w["wy"],
                        "wind_speed":magnitude,"headwind_component":along,"crosswind_component":abs(cross),
                        "ground_speed_mps":speed,"airspeed_mps":VA,
                        "flight_heading":heading_to_true(provider.coordinates,lon,psi),
                        "track_heading":heading_to_true(provider.coordinates,lon,math.atan2(tangent[1],tangent[0])),
                        "risk":risk(magnitude,limits),"status":"blocked" if magnitude>=limits["danger"] else "allowed"})
    certificate=certificate or (Certificate(curve,provider,request,provider.check).evaluate(departure) if certify else {"status":"unknown","reason":"等待连续区间检查"})
    bad=next((s for s in samples if s["status"]=="blocked"),None)
    status="blocked" if bad else certificate["status"]
    reason=f"沿途风速达到 {limits['danger']:g} m/s 硬上限" if bad else certificate["reason"]
    segments=[]
    weighted=tail=high=longest=continuous=0.
    for a,b in zip(samples,samples[1:]):
        dt=b["elapsed_seconds"]-a["elapsed_seconds"]
        magnitude=(a["wind_speed"]+b["wind_speed"])/2
        weighted+=dt*magnitude
        tail+=dt if a["headwind_component"]+b["headwind_component"]>=0 else 0.
        if magnitude>limits["warning"]:
            high+=dt
            continuous+=b["distance_km"]-a["distance_km"]
            longest=max(longest,continuous)
        else:
            continuous=0.
        segment_status="unknown" if status=="unknown" else "blocked" if a["status"]=="blocked" or b["status"]=="blocked" else "allowed"
        label="数据不足" if segment_status=="unknown" else risk(max(a["wind_speed"],b["wind_speed"]),limits)
        segment={"start_seconds":a["elapsed_seconds"],"end_seconds":b["elapsed_seconds"],
                         "points":[[a["lon"],a["lat"]],[b["lon"],b["lat"]]],"status":segment_status,
                         "risk":label,"reason":reason if segment_status!="allowed" else "按实际经过时间评估",
                         "color":COLORS[label]}
        # Contiguous identical colours need one Leaflet path. Keep every
        # sample and every turn in its geometry; only merge display objects.
        if segments and all(segments[-1][key]==segment[key] for key in ("status","risk","reason","color")):
            segments[-1]["points"].append(segment["points"][-1])
            segments[-1]["end_seconds"]=segment["end_seconds"]
        else:
            segments.append(segment)
    # Shear is measured at a single UTC, never between sequential flight samples.
    for sample in samples[::max(1,len(samples)//1000)]:
        x,y=provider.coordinates.lonlat_to_local(sample["lon"],sample["lat"])
        try:
            delta=provider.horizontal_shear(x,y,curve.z,sample["elapsed_seconds"])
        except ValueError:
            continue
        if math.isfinite(delta):
            shear_values.append({"start_distance_km":sample["distance_km"],"end_distance_km":sample["distance_km"],
                "center_distance_km":sample["distance_km"],"horizontal_wind_shear":delta,"delta_wind_vector_ms":delta,
                "eta":sample["eta"],"hard_constraint_triggered":delta>=shear_limit(request)})
    maximum=max(s["wind_speed"] for s in samples)
    analysis={"samples":samples,"total_distance_km":distance/1000,"max_wind_speed":maximum,
              "mean_wind_speed":weighted/duration if duration else maximum,"danger_ratio":high/duration if duration else 0.,
              "tailwind_ratio":tail/duration if duration else 0.,"max_continuous_danger_km":longest,
              "unknown_ratio":1. if status=="unknown" else 0.,"coverage_incomplete":status=="unknown",
              "navigation_allowed":status=="allowed","navigation_status":status,"navigation_decision_reason":reason,
              "risk_level":"数据不足" if status=="unknown" else risk(maximum,limits),
              "wind_shear":{"enabled":math.isfinite(shear_limit(request)),
                  "max_horizontal_wind_shear":max((v["horizontal_wind_shear"] for v in shear_values),default=None),
                  "horizontal_shear_warning_count":sum(v["hard_constraint_triggered"] for v in shear_values),
                  "horizontal_shear_evaluation_count":len(shear_values),"horizontal_wind_shear_profile":shear_values}}
    return {"trajectory_id":request["trajectory_id"],"route_id":request.get("route_id",request["trajectory_id"]),
            "route_name":request.get("name","V3 固定航线"),"dataset_id":provider.spec["dataset_id"],
            "forecast_cycle":provider.spec["cycle"],"level":f"{curve.z:g}m MSL","departure_at":iso(departure),
            "arrival_at":iso(departure+duration),"time_model":"constant_airspeed","airspeed_mps":VA,
            "altitude_reference":"MSL","altitude_msl_m":curve.z,"average_ground_speed_mps":distance/duration if duration else 0.,
            "flight_duration_seconds":duration,"decision":{"status":status,"reason":reason},"analysis":analysis,
            "segments":segments,"events":[],"certificate":certificate,"execution_simulation":False,
            "bbox":[min(s["lon"] for s in samples),min(s["lat"] for s in samples),max(s["lon"] for s in samples),max(s["lat"] for s in samples)],
            "frame_times":[iso(t) for t in provider.times]}
