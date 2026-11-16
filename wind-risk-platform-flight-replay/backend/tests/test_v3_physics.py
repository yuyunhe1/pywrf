"""Run with the pinned V3 numerical interpreter (separate from the API venv)."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import numpy as np
import pytest

pytest.importorskip("scipy")
from scipy.io import netcdf_file
ROOT=Path(__file__).resolve().parents[1]/"planners/wrf_v3"
sys.path[:0]=[str(ROOT/"vendor"),str(ROOT)]
from adapter.native import NativeWind,epoch,VA,R0,heading_to_local,heading_to_true
from adapter.interval import I
from adapter.curve import Curve
from adapter.profile import build_profile
from adapter.certificate import Certificate,windows
from adapter.geometry import check_geometry
from adapter.planner import free_heading_candidate
from adapter.validation import validate_route
from phase3a_implementation.adapter import StaticWRFWindAdapter,RAW_NAMES,EXPECTED_UNITS,AdapterError
from phase3a_implementation.coordinates import WRFCoordinates,ROI
from phase3b.validator import validate

LIMITS={"safe":1.5,"notice":3.3,"warning":5.4,"danger":7.9}
REQUEST={"thresholds":LIMITS,"trajectory_id":"0"*64,"wind_shear":{"enabled":True,"horizontal":{"enabled":True,"hard_constraint_enabled":True,"hard_delta_wind_vector_ms":5.4}}}
ATTRS={"MAP_PROJ":1,"GRID_ID":2,"TRUELAT1":30,"TRUELAT2":60,"STAND_LON":118.6,"MOAD_CEN_LAT":31.05935,"DX":3000,"DY":3000}


def material(tmp_path,winds,*,hours=None,omit=()):
    rows=[]
    start=datetime(2026,11,3,tzinfo=timezone.utc)
    coords=WRFCoordinates(ATTRS,(31.05935,118.55255),(5,5),roi=ROI(-1e6,1e6,-1e6,1e6))
    lonlat=np.array([[coords.local_to_lonlat(*coords.grid_to_local(i,j)) for i in range(5)] for j in range(5)])
    for index,(u,v) in enumerate(winds):
        instant=start+timedelta(hours=index if hours is None else hours[index])
        path=tmp_path/f"frame{index}.nc"
        with netcdf_file(path,"w",version=1) as ds:
            for name,size in {"Time":1,"DateStrLen":19,"bottom_top":3,"bottom_top_stag":4,"south_north":5,"south_north_stag":6,"west_east":5,"west_east_stag":6}.items():
                ds.createDimension(name,size)
            for name,value in ATTRS.items():
                setattr(ds,name,value)
            ds.START_DATE=ds.SIMULATION_START_DATE="2026-11-03_00:00:00"
            ds.createVariable("Times","S1",("Time","DateStrLen"))[:]=np.frombuffer(instant.strftime("%Y-%m-%d_%H:%M:%S").encode(),dtype="S1").reshape(1,19)
            dims={"U":("Time","bottom_top","south_north","west_east_stag"),"V":("Time","bottom_top","south_north_stag","west_east"),
                  "W":("Time","bottom_top_stag","south_north","west_east"),"PH":("Time","bottom_top_stag","south_north","west_east"),
                  "PHB":("Time","bottom_top_stag","south_north","west_east")}
            for name in RAW_NAMES:
                if name in omit:continue
                var=ds.createVariable(name,"f4",dims.get(name,("Time","south_north","west_east")))
                var.units=EXPECTED_UNITS[name]
                if name=="U":var[:]=u
                elif name=="V":var[:]=v
                elif name=="PH":var[:]=(np.arange(4)*200*9.81).reshape(1,4,1,1)
                elif name in ("COSALPHA","MAPFAC_M"):var[:]=1
                elif name=="XLAT":var[:]=lonlat[:,:,1]
                elif name=="XLONG":var[:]=lonlat[:,:,0]
                else:var[:]=0
        rows.append({"path":str(path),"expected_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
                     "expected_time":instant.strftime("%Y-%m-%d_%H:%M:%S"),"utc":instant.isoformat(),"record_index":0,
                     "case_id":"PLATFORM_TEST","source_run_id":"PLATFORM_TEST"})
    return {"kind":"native","altitude_msl_m":300.,"records":rows,"cycle":rows[0]["utc"],"dataset_id":"v3_"+"1"*60}


def straight_curve(length=1000):
    coefficients=np.zeros((1,3,8))
    coefficients[0,:,0]=[4000,4000,0]
    coefficients[0,0,1]=length
    return Curve([0,length/10],coefficients,300)


def test_cached_native_queries_match_original_and_intervals_enclose(tmp_path):
    spec=material(tmp_path,[(2,1),(3,-1)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"]))
    original=StaticWRFWindAdapter.from_wrfout(spec["records"][0]["path"],expected_sha256=spec["records"][0]["expected_sha256"],
        expected_time=spec["records"][0]["expected_time"],case_id="PLATFORM_TEST",roi=p.roi)
    for x,y in [(4000,4000),(5000,4500),(8000,7000)]:
        a,b=p.wind_query(x,y,300,0),original.wind_query(x,y,300,0)
        assert [a[k] for k in ("wx","wy","wz")]==[b[k] for k in ("wx","wy","wz")]
    wx,wy,_=p.bounds(I(4000,5000),I(4000,4500),300,I(p.epoch+100,p.epoch+300))
    for t in np.linspace(100,300,21):
        q=p.wind_query(4500,4200,300,t)
        assert wx.v.lo<=q["wx"]<=wx.v.hi
        assert wy.v.lo<=q["wy"]<=wy.v.hi


def test_temporal_query_uses_small_relative_seconds_without_utc_quantization(tmp_path):
    spec=material(tmp_path,[(1,0),(3,0)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"]))
    for t in (0.000123,0.00123456,.123456789,123.123456789):
        q=p.wind_query(4500,4500,300,t)
        assert q["wx"]==pytest.approx(1+2*t/3600,abs=3e-15)


def test_fixed_airspeed_retiming_matches_analytic_time_and_preserves_geometry(tmp_path):
    spec=material(tmp_path,[(1,0),(3,0),(5,0)])
    c=straight_curve()
    p=NativeWind(spec,epoch(spec["records"][0]["utc"]))
    early=build_profile(c,p,REQUEST)
    later=build_profile(c,NativeWind(spec,p.epoch+3600),REQUEST)
    slope=2/3600
    expected=2*1000/(11+math.sqrt(11**2+2*slope*1000))
    assert early["flight_duration_seconds"]==pytest.approx(expected,abs=1e-5)
    assert later["flight_duration_seconds"]<early["flight_duration_seconds"]
    assert [(s["lon"],s["lat"]) for s in early["analysis"]["samples"]]==[(s["lon"],s["lat"]) for s in later["analysis"]["samples"]]
    assert early["decision"]["status"]==later["decision"]["status"]=="allowed"
    assert early["average_ground_speed_mps"]!=10


def test_safe_endpoints_do_not_certify_dangerous_middle_of_departure_interval(tmp_path):
    spec=material(tmp_path,[(0,0),(9,0),(0,0),(0,0)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"]))
    c=straight_curve(100)
    cert=Certificate(c,p,REQUEST)
    assert cert.evaluate(p.epoch)["status"]=="allowed"
    assert cert.evaluate(p.epoch+7200)["status"]=="allowed"
    assert cert.evaluate(p.epoch,p.epoch+7200)["status"]!="allowed"
    result=windows(c,p,{**REQUEST,"evaluated_at_epoch":p.epoch})
    assert result["departure_windows"]
    for window in result["departure_windows"]:
        assert not epoch(window["start"])<=p.epoch+3600<=epoch(window["end"])
        assert epoch(window["start"])%300==epoch(window["end"])%300==0


def test_cross_hour_failure_turn_rate_and_missing_material_are_not_allowed(tmp_path):
    spec=material(tmp_path,[(0,0),(9,0),(0,0)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"])+3300)
    assert Certificate(straight_curve(5000),p,REQUEST).evaluate(p.epoch)["status"]!="allowed"
    flat=material(tmp_path,[(0,0),(0,0),(0,0)])
    p=NativeWind(flat,epoch(flat["records"][0]["utc"]))
    coeff=np.zeros((1,3,8));coeff[0,:,0]=[4000,4000,0];coeff[0,0,1]=10;coeff[0,1,2]=10
    assert Certificate(Curve([0,1],coeff,300),p,REQUEST).evaluate(p.epoch)["status"]!="allowed"
    assert p.wind_query(4000,4000,-1,0)["valid"] is False
    assert p.wind_query(4000,4000,300,100000)["valid"] is False
    broken=material(tmp_path,[(0,0),(0,0)],hours=[0,2])
    gap=NativeWind(broken,epoch(broken["records"][0]["utc"]))
    assert gap.wind_query(4000,4000,300,1)["valid"] is False


def test_true_heading_round_trip_and_original_independent_validation(tmp_path):
    spec=material(tmp_path,[(2,1),(2,1)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"]))
    for heading in (0,90,180,270,359.9):
        assert heading_to_true(p.coordinates,118.6,heading_to_local(p.coordinates,118.6,heading))==pytest.approx(heading,abs=1e-12)
    route={"type":"piecewise_constant","control_interpolation":"ZOH","state_start":[4000,4000,0],"state_goal":[4120,4010,0],
           "controls":[0.],"durations":[10.],"states":[[4000,4000,0],[4120,4010,0]],"parameters":{"Va":VA,"R0":R0},
           "z_layer_msl_m":300,"frame_id":p.frame_id,"height_datum":p.height_datum}
    assert validate(route,route["controls"],10,p)["valid"]
    curve=Curve.from_route(route,p)
    np.testing.assert_allclose(curve.point(10),route["state_goal"],atol=1e-7)


def test_cached_two_level_queries_match_full_native_columns_with_spatial_variation(tmp_path):
    rng=np.random.default_rng(34)
    u=rng.uniform(-3,3,(1,3,5,6))
    v=rng.uniform(-3,3,(1,3,6,5))
    spec=material(tmp_path,[(u,v),(u,v)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"]))
    original=StaticWRFWindAdapter.from_wrfout(spec["records"][0]["path"],
        expected_sha256=spec["records"][0]["expected_sha256"],expected_time=spec["records"][0]["expected_time"],
        case_id="PLATFORM_TEST",roi=p.roi)
    for x,y,z in rng.uniform([1000,1000,110],[10000,10000,490],(80,3)):
        actual=p.wind_query(x,y,z,0)
        expected=original.wind_query(x,y,z,0)
        assert actual["valid"]==expected["valid"]
        np.testing.assert_allclose([actual[k] for k in ("wx","wy","wz")],
            [expected[k] for k in ("wx","wy","wz")],rtol=0,atol=2e-15)


def test_free_heading_core_matches_constant_wind_analytic_solution_and_c(tmp_path):
    spec=material(tmp_path,[(2,1),(2,1)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"]))
    route,receipt=free_heading_candidate([4000,4000],[5000,4000],p,300,lambda:None)
    assert receipt["valid"],receipt["failures"]
    assert route["state_start"][2]==pytest.approx(-math.asin(.1),abs=1e-9)
    assert route["durations"][0]==pytest.approx(1000/(2+math.sqrt(99)),abs=1e-7)
    curve=Curve.from_route(route,p)
    assert check_geometry(curve,p.roi)["valid"]
    assert not check_geometry(curve,ROI(4001,6000,3000,6000))["valid"]
    assert Certificate(curve,p,REQUEST).evaluate(p.epoch)["status"]=="allowed"


def test_free_heading_core_solves_cross_hour_wrf_without_freezing_wind(tmp_path):
    spec=material(tmp_path,[(2,1),(2,2),(3,-1)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"])+3550)
    route,receipt=free_heading_candidate([4000,4000],[5500,4000],p,300,lambda:None)
    assert receipt["valid"],receipt["failures"]
    assert route["durations"][0]>50
    curve=Curve.from_route(route,p)
    profile=build_profile(curve,p,REQUEST,original_time=True)
    assert profile["decision"]["status"]=="allowed"
    assert profile["analysis"]["samples"][0]["v"]!=profile["analysis"]["samples"][-1]["v"]


def test_turn_coast_core_preserves_incoming_heading_and_c_across_hours(tmp_path):
    from adapter.position_core import connect
    spec=material(tmp_path,[(2,1),(2,2),(3,-1)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"])+3550)
    incoming=[4000,4000,math.pi/2]
    route,receipt=connect(incoming,[5500,4000],p,300,lambda:None)
    assert route["state_start"]==incoming
    assert receipt["valid"] and receipt["position_error"]<.1
    assert max(map(abs,route["controls"]))<=.9*VA/R0+1e-12
    assert sum(route["durations"])>50
    curve=Curve.from_route(route,p)
    assert Certificate(curve,p,REQUEST).evaluate(p.epoch)["status"]=="allowed"
    assert build_profile(curve,p,REQUEST,original_time=True)["decision"]["status"]=="allowed"


def test_native_boundary_partition_keeps_controls_timing_and_physical_curve(tmp_path):
    from adapter.curve import partition_native_controls
    spec=material(tmp_path,[(2,1),(2,2),(3,-1)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"])+3550)
    route,_=free_heading_candidate([4000,4000],[9000,4000],p,300,lambda:None)
    curve=Curve.from_route(route,p)
    partitioned=partition_native_controls(route,curve,p)
    assert len(partitioned["controls"])>len(route["controls"])
    assert all(control==0 for control in partitioned["controls"])
    assert sum(partitioned["durations"])==pytest.approx(sum(route["durations"]),abs=1e-9)
    assert any(abs(t-50)<1e-7 for t in np.cumsum(partitioned["durations"]))
    receipt=validate_route(partitioned,partitioned["controls"],sum(partitioned["durations"]),p)
    assert receipt["valid"],receipt["constraint_residual"]
    actual=Curve.from_route(partitioned,p)
    for t in np.linspace(0,curve.end,20):
        np.testing.assert_allclose(actual.point(t),curve.point(t),atol=1e-5)


def test_one_metre_endpoint_policy_preserves_interior_heading_and_control_limits(tmp_path):
    spec=material(tmp_path,[(0,0),(0,0)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"]))
    route={"type":"piecewise_constant","control_interpolation":"ZOH","state_start":[4000,4000,0],
           "state_goal":[4100.9,4000,0],"controls":[0.,0.],"durations":[5.,5.],
           "states":[[4000,4000,0],[4050,4000,0],[4100.9,4000,0]],"parameters":{"Va":VA,"R0":R0},
           "z_layer_msl_m":300,"frame_id":p.frame_id,"height_datum":p.height_datum}
    receipt=validate_route(route,route["controls"],10,p)
    assert receipt["valid"] and not receipt["upstream_valid"]
    assert receipt["position_error"]==pytest.approx(.9)
    assert receipt["thresholds"]["endpoint_position_m"]==1.
    assert Curve.from_route(route,p).point(10)[0]==pytest.approx(4100)
    route["state_goal"][0]=4101.01
    assert not validate_route(route,route["controls"],10,p)["valid"]
    with pytest.raises(ValueError,match="CONTINUOUS_CURVE_ENDPOINT_REJECT"):
        Curve.from_route(route,p)
    route["state_goal"][0]=4100.9
    route["states"][1][0]+=.01
    assert not validate_route(route,route["controls"],10,p)["valid"]
    route["states"][1][0]-=.01
    route["state_goal"][2]=.01
    assert "HEADING_RESIDUAL" in validate_route(route,route["controls"],10,p)["failures"]
    route["state_goal"][2]=0
    route["controls"][0]=VA/R0+1
    assert "CONTROL_BOUND" in validate_route(route,route["controls"],10,p)["failures"]


def test_free_endpoint_small_turn_arc_keeps_v3_dynamics_and_turn_reserve(tmp_path):
    spec=material(tmp_path,[(2,1),(2,1)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"]))
    route,receipt=free_heading_candidate([4000,4000],[5000,4000],p,300,lambda:None,.15)
    assert receipt["valid"],receipt["failures"]
    assert 0<route["controls"][0]<=.9*VA/R0
    assert route["state_goal"][2]!=route["state_start"][2]
    assert Certificate(Curve.from_route(route,p),p,REQUEST).evaluate(p.epoch)["status"]=="allowed"


def test_unflyable_or_uncovered_retiming_keeps_geometry_without_inventing_eta(tmp_path):
    c=straight_curve()
    spec=material(tmp_path,[(12,0),(12,0)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"]))
    blocked=build_profile(c,p,REQUEST)
    assert blocked["decision"]["status"]=="blocked" and blocked["arrival_at"] is None
    assert blocked["analysis"]["navigation_allowed"] is False and blocked["analysis"]["samples"]==[]
    assert len(blocked["segments"][0]["points"])>2
    spec=material(tmp_path,[(0,0),(0,0)])
    p=NativeWind(spec,epoch(spec["records"][-1]["utc"])-10)
    unknown=build_profile(c,p,REQUEST)
    assert unknown["decision"]["status"]=="unknown" and unknown["arrival_at"] is None
    assert unknown["average_ground_speed_mps"] is None and unknown["timing_available"] is False


def test_missing_native_variable_is_reported_without_substituting_a_wind_field(tmp_path):
    spec=material(tmp_path,[(0,0),(0,0)],omit=("V",))
    with pytest.raises(AdapterError) as error:
        NativeWind(spec,epoch(spec["records"][0]["utc"]))
    assert error.value.status=="MISSING_VARIABLE"


def test_sub_ulp_control_duration_does_not_break_continuous_curve_material(tmp_path):
    spec=material(tmp_path,[(2,1),(2,1)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"]))
    route={"type":"piecewise_constant","control_interpolation":"ZOH","state_start":[4000,4000,0],
        "state_goal":[4240,4020,0],"controls":[0.,0.,0.],"durations":[10.,1e-18,10.],
        "states":[[4000,4000,0],[4120,4010,0],[4120,4010,0],[4240,4020,0]],
        "parameters":{"Va":VA,"R0":R0},"z_layer_msl_m":300,"frame_id":p.frame_id,"height_datum":p.height_datum}
    assert validate_route(route,route["controls"],20,p)["valid"]
    curve=Curve.from_route(route,p)
    assert curve.end==20
    np.testing.assert_allclose(curve.point(20),route["state_goal"],atol=1e-7)


def test_diagnostic_shear_interpolates_vectors_before_magnitude_and_compacts_map_paths(tmp_path):
    u=np.arange(6)[None,None,None,:]*.5+1
    v=np.zeros((1,3,6,5))
    spec=material(tmp_path,[(u,v),(3*u,v)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"]))
    assert p.horizontal_shear(4500,4500,300,0)==pytest.approx(.5)
    assert p.horizontal_shear(4500,4500,300,1800)==pytest.approx(1.)
    flat=material(tmp_path,[(2,0),(2,0)])
    profile=build_profile(straight_curve(),NativeWind(flat,epoch(flat["records"][0]["utc"])),REQUEST)
    assert len(profile["segments"])==1
    segment=profile["segments"][0]
    assert segment["end_seconds"]==profile["flight_duration_seconds"]
    assert len(segment["points"])==len(profile["analysis"]["samples"])


def test_partial_window_receipt_marks_unprocessed_future_and_expired_data_is_missing(tmp_path):
    spec=material(tmp_path,[(0,0),(0,0),(0,0)])
    p=NativeWind(spec,epoch(spec["records"][0]["utc"]))
    held=[]
    class Stop(Exception):pass
    def publish(result):
        held.append(result)
        if result["departure_windows"]:raise Stop()
    with pytest.raises(Stop):
        windows(straight_curve(),p,{**REQUEST,"evaluated_at_epoch":p.epoch},publish)
    assert held[-1]["complete"] is False and held[-1]["unconfirmed_ranges"]
    assert epoch(held[-1]["unconfirmed_ranges"][-1]["end"])==p.times[-1]
    expired=windows(straight_curve(),p,{**REQUEST,"evaluated_at_epoch":p.times[-1]+1})
    assert expired["departure_windows"]==[] and expired["empty_reason"]=="data_insufficient"
