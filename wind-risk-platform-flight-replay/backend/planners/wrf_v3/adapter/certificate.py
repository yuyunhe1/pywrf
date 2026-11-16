"""Continuous fixed-curve, fixed-airspeed departure interval enclosures.

This is a new platform calculation, not an upstream complete-task receipt.
Unresolved boxes are UNKNOWN; endpoint sampling never proves a window.
"""
import math
from .interval import I, down, up, hypot
from .native import VA,R0,iso


class ConstraintViolation(ValueError):
    pass


def describe_reason(code):
    return {
        "WIND_LIMIT":"沿途风速达到风速硬上限或固定空速适用边界",
        "TURN_RATE":"保持固定航线所需转率超过飞机限制",
        "WIND_LIMIT_UNCONFIRMED":"无法确认整个起飞区间的沿途风速均低于硬上限",
        "TURN_RATE_UNCONFIRMED":"无法确认整个起飞区间的风补偿转率均满足限制",
        "HORIZONTAL_SHEAR_UNCONFIRMED":"无法确认沿途水平风切变均低于硬上限",
        "WRF_TIME_OUTSIDE_COVERAGE":"WRF 预报未完整覆盖沿途到达时间",
        "WRF_NATIVE_HOUR_GAP":"WRF 小时时次不连续，无法确认跨小时飞行",
        "NATIVE_VERTICAL_OR_TERRAIN_UNCONFIRMED":"无法确认整段航线均处于可用海拔范围并高于地形",
    }.get(code,"数据或区间计算不足，暂未确认完整航程满足约束")


def shear_limit(request):
    settings=request.get("wind_shear",{})
    horizontal=settings.get("horizontal",{})
    return float(horizontal.get("hard_delta_wind_vector_ms",5.4)) if settings.get("enabled",True) and horizontal.get("enabled",True) and horizontal.get("hard_constraint_enabled",True) else math.inf


class Certificate:
    def __init__(self,curve,provider,request,check=lambda:None):
        self.curve,self.provider,self.check=curve,provider,check
        self.wind_limit=float(request.get("thresholds",{}).get("danger",7.9))
        self.shear_limit=shear_limit(request)
        self.boxes=0
        self.reason=""

    def segment(self,index,a,b,arrival,depth=0):
        self.check()
        self.boxes+=1
        reason=""
        may_refine=True
        try:
            x,y,vx,vy,ax,ay=self.curve.bounds(index,a,b)
            norm=hypot(vx,vy)
            if norm.lo<=0:
                raise ValueError("CURVE_TANGENT_UNCONFIRMED")
            # Conditional first-violation enclosure: before any wind-limit
            # violation, positive along-track speed is at least Va-limit.
            # The 1e-6 proof margin only causes UNKNOWN, never relaxation.
            safe_limit=min(self.wind_limit,VA-1e-6)
            max_dt=up((b-a)*norm.hi/(VA-safe_limit))
            tube=I(arrival.lo,up(arrival.hi+max_dt))
            wx,wy,shear=self.provider.bounds(x,y,self.curve.z,tube)
            magnitude=hypot(wx.v,wy.v)
            if magnitude.lo>=min(self.wind_limit,VA):
                raise ConstraintViolation("WIND_LIMIT")
            if magnitude.hi>=self.wind_limit or magnitude.hi>=VA:
                may_refine=magnitude.lo<min(self.wind_limit,VA)
                raise ValueError("WIND_LIMIT_UNCONFIRMED")
            if math.isfinite(self.shear_limit) and shear>=self.shear_limit:
                raise ValueError("HORIZONTAL_SHEAR_UNCONFIRMED")
            tx,ty=vx/norm,vy/norm
            nx,ny=-ty,tx
            along=wx.v*tx+wy.v*ty
            cross=wx.v*nx+wy.v*ny
            root=(I(VA*VA)-cross.sq()).sqrt()
            speed=(along+root).intersect(I(down(VA-magnitude.hi),up(VA+magnitude.hi)))
            if speed.lo<=0:
                raise ValueError("POSITIVE_GROUND_SPEED_UNCONFIRMED")
            curvature=(vx*ay-vy*ax)/(norm*norm*norm)
            spatial_normal=nx*(wx.dx*tx+wx.dy*ty)+ny*(wy.dx*tx+wy.dy*ty)
            time_normal=nx*wx.dt+ny*wy.dt
            omega=curvature*speed-((spatial_normal-curvature*along)*speed+time_normal)/root
            if omega.lo>VA/R0 or omega.hi < -VA/R0:
                raise ConstraintViolation("TURN_RATE")
            if omega.absmax()>VA/R0:
                may_refine=not (omega.lo>VA/R0 or omega.hi < -VA/R0)
                raise ValueError("TURN_RATE_UNCONFIRMED")
            duration=norm/speed*(b-a)
            return arrival+duration
        except ConstraintViolation:
            raise
        except ValueError as exc:
            reason=str(exc)
        # Split reference-curve boxes before giving up on an entire departure
        # interval. Every sub-box has its own propagated arrival enclosure.
        maximum_depth=4 if arrival.hi-arrival.lo>=30 else 12
        if may_refine and depth<maximum_depth and b-a>1e-4:
            middle=(a+b)/2
            try:
                half=self.segment(index,a,middle,arrival,depth+1)
                return self.segment(index,middle,b,half,depth+1)
            except ConstraintViolation:
                raise
            except ValueError as exc:
                reason=str(exc)
        self.reason=reason
        raise ValueError(reason)

    def evaluate(self,start,end=None):
        arrival=I(start,start if end is None else end)
        before=self.boxes
        try:
            for index in range(len(self.curve.coefficients)):
                arrival=self.segment(index,float(self.curve.breaks[index]),float(self.curve.breaks[index+1]),arrival)
            return {"status":"allowed","reason":"固定空速及固定路径的整个起飞区间通过保守检查",
                    "arrival_lower":arrival.lo,"arrival_upper":arrival.hi,"boxes":self.boxes-before}
        except ConstraintViolation as exc:
            return {"status":"blocked","reason":describe_reason(str(exc)),"reason_code":str(exc),"boxes":self.boxes-before}
        except ValueError as exc:
            return {"status":"unknown","reason":describe_reason(str(exc)),"reason_code":str(exc),"boxes":self.boxes-before}


def windows(curve,provider,request,publish=lambda result:None,check=lambda:None):
    now=float(request["evaluated_at_epoch"])
    step=300.
    cert=Certificate(curve,provider,request,check)
    safe=[]
    unknown=[]
    blocked=[]
    coverage_start,coverage_end=provider.times[0],provider.times[-1]
    current=cert.evaluate(now)
    current_result={"status":current["status"],"reason":current["reason"],"departure_at":iso(now),
                    "arrival_at":iso(current["arrival_upper"]) if current["status"]=="allowed" else None}
    def describe(complete=False):
        unconfirmed=list(unknown)
        if now<coverage_start:
            unconfirmed.append((now,coverage_start,"WRF 预报尚未覆盖这一时段"))
        if not complete:
            covered=sorted([(lo,hi) for lo,hi,*_ in safe]+blocked+[(lo,hi) for lo,hi,*_ in unknown])
            cursor=max(now,coverage_start)
            for lo,hi in covered:
                if lo>cursor:
                    unconfirmed.append((cursor,lo,"尚未完成这一起飞区间的检查"))
                cursor=max(cursor,hi)
            if cursor<coverage_end:
                unconfirmed.append((cursor,coverage_end,"尚未完成这一起飞区间的检查"))
        missing_coverage=now>=coverage_end or now<coverage_start
        combined=[]
        for lo,hi,arrival_lo,arrival_hi in sorted(safe):
            if combined and lo<=combined[-1][1]:
                combined[-1][1]=hi
                combined[-1][2]=min(combined[-1][2],arrival_lo)
                combined[-1][3]=max(combined[-1][3],arrival_hi)
            else:
                combined.append([lo,hi,arrival_lo,arrival_hi])
        shown=[]
        for lo,hi,alo,ahi in combined:
            lo=math.ceil(lo/step)*step
            hi=math.floor(hi/step)*step
            if hi>lo:
                shown.append({"start":iso(lo),"end":iso(hi),"first_arrival":iso(alo),"last_arrival":iso(ahi),
                              "arrival_bounds":True,"certification":"CONSERVATIVE_CONTINUOUS"})
        return {"evaluated_at":iso(now),"refresh_at":iso(now+600),"source":"wrf","time_model":"constant_airspeed",
                "airspeed_mps":VA,"altitude_reference":"MSL","altitude_msl_m":curve.z,"level":f"{curve.z:g}m MSL",
                "forecast_cycle":provider.spec["cycle"],"dataset_id":provider.spec["dataset_id"],
                "trajectory_id":request["trajectory_id"],"route_id":request.get("route_id",request["trajectory_id"]),
                "route_name":request.get("name","V3 固定航线"),"coverage_start":iso(coverage_start),"coverage_end":iso(coverage_end),
                "current_departure":current_result,"departure_windows":shown,"display_step_minutes":5,
                "coverage_incomplete":bool(unconfirmed) or missing_coverage or not complete,"complete":complete,"unconfirmed_intervals":len(unconfirmed),
                "blocked_intervals":len(blocked),
                "unconfirmed_ranges":[{"start":iso(lo),"end":iso(hi),"reason":reason} for lo,hi,reason in sorted(unconfirmed)],
                "empty_reason":None if shown else "data_insufficient" if unconfirmed or missing_coverage or not complete else "conditions_unsuitable","certificate_boxes":cert.boxes,
                "certification":"OUTWARD_INTERVAL_FIXED_CURVE_V1","execution_simulation":False}
    publish(describe())
    # Hour-sized boxes are refined in departure time; all hours are retained,
    # including intervals whose endpoints alone would appear safe.
    def evaluate(lo,hi):
        check()
        result=cert.evaluate(lo,hi)
        if result["status"]=="allowed":
            safe.append((lo,hi,result["arrival_lower"],result["arrival_upper"]))
            publish(describe())
        elif result["status"]=="blocked":
            blocked.append((lo,hi))
        elif hi-lo>30.:
            middle=(lo+hi)/2
            evaluate(lo,middle)
            evaluate(middle,hi)
        else:
            unknown.append((lo,hi,result["reason"]))
    start=max(now,coverage_start)
    while start<coverage_end:
        end=min(coverage_end,(math.floor(start/3600)+1)*3600)
        evaluate(start,end)
        start=end
    result=describe(True)
    publish(result)
    return result
