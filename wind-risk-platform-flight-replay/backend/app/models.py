"""API request models."""

from typing import Any
from datetime import datetime
import math

from pydantic import BaseModel, Field, field_validator, model_validator


class Thresholds(BaseModel):
    """Wind risk thresholds in m/s."""

    safe: float = 1.5
    notice: float = 3.3
    warning: float = 5.4
    danger: float = 7.9

    @field_validator("notice", "warning", "danger")
    @classmethod
    def validate_order(cls, value: float, info):
        previous = {
            "notice": info.data.get("safe"),
            "warning": info.data.get("notice"),
            "danger": info.data.get("warning"),
        }[info.field_name]
        if previous is not None and value <= previous:
            raise ValueError("风级阈值必须严格递增")
        return value


class VerticalWindShearSettings(BaseModel):
    # Kept in the schema for backward compatibility; current route planning
    # deliberately ignores vertical wind shear.
    enabled: bool = False
    caution_delta_v_10m_ms: float = Field(default=1.0, ge=0)
    hard_delta_v_10m_ms: float = Field(default=3.0, gt=0)
    hard_delta_v_30m_ms: float = Field(default=6.0, gt=0)
    caution_direction_change_deg: float = Field(default=20.0, ge=0, le=180)
    hard_direction_change_deg: float = Field(default=45.0, gt=0, le=180)
    hard_constraint_enabled: bool = False

    @model_validator(mode="after")
    def validate_threshold_order(self):
        if self.caution_delta_v_10m_ms >= self.hard_delta_v_10m_ms:
            raise ValueError("垂直风切变谨慎阈值必须小于硬约束阈值")
        if self.caution_direction_change_deg >= self.hard_direction_change_deg:
            raise ValueError("垂直风向谨慎阈值必须小于硬约束阈值")
        return self


class HorizontalWindShearSettings(BaseModel):
    enabled: bool = True
    hard_delta_wind_vector_ms: float = Field(default=5.4, gt=0)
    hard_constraint_enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_threshold_name(cls, value):
        if isinstance(value, dict) and "hard_delta_wind_vector_ms" not in value:
            for legacy_name in ("hard_horizontal_wind_shear_ms_per_km", "hard_delta_v_1km_ms"):
                if legacy_name in value:
                    value = {**value, "hard_delta_wind_vector_ms": value[legacy_name]}
                    break
        return value


class WindShearSettings(BaseModel):
    enabled: bool = True
    min_wind_speed_for_direction_ms: float = Field(default=0.5, ge=0)
    vertical: VerticalWindShearSettings = Field(default_factory=VerticalWindShearSettings)
    horizontal: HorizontalWindShearSettings = Field(default_factory=HorizontalWindShearSettings)
    note: str = "项目实验性风险阈值，可根据观测和实验结果调整，不代表无人机国家强制标准"

class RouteAnalyzeRequest(BaseModel):
    """Route analysis request. Each route point uses [longitude, latitude]."""

    points: list[tuple[float, ...]] = Field(min_length=2)
    cycle: str | None = None
    forecast_hour: float | None = Field(default=None, ge=0,allow_inf_nan=False)
    forecast_minutes: int | None = Field(default=None,ge=0)
    valid_time: str | None = None
    source: str | None = None
    level: str
    thresholds: Thresholds = Field(default_factory=Thresholds)
    wind_shear: WindShearSettings = Field(default_factory=WindShearSettings)
    sample_interval_km: float = Field(default=3.0, gt=0, le=100)

    @model_validator(mode='before')
    @classmethod
    def derive_minute_hour(cls,values):
        if not isinstance(values,dict):
            return values
        values=dict(values)
        if values.get('forecast_minutes') is not None:
            try:
                hour=float(values['forecast_minutes'])/60
            except (ValueError,TypeError) as exc:
                raise ValueError('分钟时效必须为非负整数') from exc
            if not math.isfinite(hour):
                raise ValueError('分钟时效必须为非负整数')
            if values.get('forecast_hour') is not None and not math.isclose(float(values['forecast_hour']),hour,abs_tol=1e-9):
                raise ValueError('分钟时效与小时时效不一致')
            values['forecast_hour']=hour
        return values

    @model_validator(mode="after")
    def validate_time_selection(self):
        if self.cycle is None and self.valid_time is None:
            raise ValueError("必须提供 cycle 或 valid_time")
        if self.valid_time is None and self.forecast_hour is None:
            raise ValueError("未提供 valid_time 时，必须提供 forecast_hour")
        return self


class RoutePlanRequest(RouteAnalyzeRequest):
    client_instance_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_request_sequence: int | None = Field(default=None,ge=1)
    seed_routes: list[list[tuple[float,float]]] = Field(default_factory=list,max_length=4)
    start: tuple[float, float]
    end: tuple[float, float]
    planner_type: str = "wa_lpa_star"
    aircraft_model: str = "fixed_wing"
    planning_strategy: str = "wind_avoidance"
    points: list[tuple[float, ...]] = Field(default_factory=lambda: [(0, 0), (0, 0)])
    time_aware: bool = False
    departure_at: datetime | None = None
    average_ground_speed_mps: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    dataset_id: str | None = Field(default=None, min_length=1, max_length=64)
    altitude_msl_m: float = Field(default=300.0, gt=0, le=20000, allow_inf_nan=False)
    start_heading_deg: float | None = Field(default=None, ge=0, le=360, allow_inf_nan=False)
    end_heading_deg: float | None = Field(default=None, ge=0, le=360, allow_inf_nan=False)
    constrain_headings: bool = False
    max_route_distance_km: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    stop_on_first_feasible: bool = False
    optimize_during_display: bool = False
    display_segment_seconds: float = Field(default=2.0, gt=0, le=10, allow_inf_nan=False)
    prefer_simple_route: bool = False
    route_quality_extra_seconds: float = Field(default=10., gt=0, le=10, allow_inf_nan=False)
    required_waypoints: list[tuple[float, float]] = Field(default_factory=list, max_length=20000)
    # Shared with the direction workers; only the range workflow sets these.
    minimize_distance_under_peak: bool = False
    peak_wind_limit_mps: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    peak_wind_limit_exclusive: bool = False

    @model_validator(mode="after")
    def validate_time_selection(self):
        for lon, lat in self.required_waypoints:
            if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 < lat < 90:
                raise ValueError('必经航点经纬度无效')
        if self.required_waypoints and (len(self.required_waypoints) < 2 or
                self.required_waypoints[0] != self.start or self.required_waypoints[-1] != self.end or
                any(a == b for a, b in zip(self.required_waypoints, self.required_waypoints[1:]))):
            raise ValueError('必经航点须按顺序包含起终点，相邻航点必须不同')
        for route in self.seed_routes:
            if not 2<=len(route)<=2048 or tuple(route[0])!=self.start or tuple(route[-1])!=self.end:
                raise ValueError('缓存引导路径须保留起终点且包含 2 至 2048 个航点')
            if any(not math.isfinite(lon) or not math.isfinite(lat) or not -180<=lon<=180 or not -90<lat<90 for lon,lat in route):
                raise ValueError('缓存引导路径经纬度无效')
        if self.planner_type == "wrf_v3":
            if self.source not in (None, "wrf"):
                raise ValueError("V3 规划需要原生 WRF 小时风场")
            for lon, lat in (self.start, self.end):
                if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 < lat < 90:
                    raise ValueError("V3 起终点经纬度无效")
            self.time_aware = True
            self.source = "wrf"
        if self.time_aware or self.departure_at is not None:
            if self.departure_at is not None and (self.departure_at.tzinfo is None or self.departure_at.utcoffset() is None):
                raise ValueError("起飞时间必须包含时区")
            if self.source not in (None, "wrf", "gfs"):
                raise ValueError("跨时刻航线规划需要 WRF 或 GFS 小时风场")
            if self.source == "gfs" and "average_ground_speed_mps" not in self.model_fields_set:
                self.average_ground_speed_mps = 50.0
            return self
        return super().validate_time_selection()


class RecommendationJobRequest(RoutePlanRequest):
    mode: str = Field(default='specified_time', pattern='^(specified_time|fixed_route|departure_range)$')
    time_aware: bool = True
    segment_index: int | None = Field(default=None, ge=0)
    max_detour_ratio: float = Field(default=.5, ge=0, allow_inf_nan=False)
    baseline_version: str | None = Field(default=None, max_length=128)
    departure_start_at: datetime | None = None
    departure_end_at: datetime | None = None

    @model_validator(mode='before')
    @classmethod
    def derive_fixed_endpoints(cls, values):
        values = dict(values)
        if values.get('mode') == 'fixed_route' and len(values.get('points', [])) >= 2:
            values['start'], values['end'] = values['points'][0][:2], values['points'][-1][:2]
        if values.get('mode') == 'departure_range':
            anchors = values.get('required_waypoints') or [values.get('start'), values.get('end')]
            if len(anchors) >= 2 and all(p is not None for p in anchors):
                values['required_waypoints'] = anchors
                values['start'], values['end'] = anchors[0], anchors[-1]
        return values

    @model_validator(mode='after')
    def validate_recommendation(self):
        if self.planner_type == 'wrf_v3':
            raise ValueError('推荐工作流支持 A*、LPA*、WA-LPA*')
        if self.mode == 'fixed_route':
            if self.segment_index is None or self.segment_index >= len(self.points)-1:
                raise ValueError('请选择一对相邻航点')
            if self.points[self.segment_index][:2] == self.points[self.segment_index+1][:2]:
                raise ValueError('绕行端点必须不同')
        if self.mode == 'departure_range':
            for value in (self.departure_start_at, self.departure_end_at):
                if value is None or value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError('起飞时间段须包含带时区的开始和结束时间')
                if value.second or value.microsecond:
                    raise ValueError('起飞时间段精确到分钟即可')
            if self.departure_end_at < self.departure_start_at:
                raise ValueError('起飞时间段结束时间不能早于开始时间')
        return self


class RouteDecisionRequest(BaseModel):
    """Navigation decision request. Each point uses [longitude, latitude]."""

    start: tuple[float, float]
    end: tuple[float, float]
    candidate_valid_times: list[str] = Field(default_factory=list)
    candidate_offsets_hours: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 6])
    cycle: str | None = None
    forecast_hour: float | None = Field(default=None, ge=0,allow_inf_nan=False)
    forecast_minutes: int | None = Field(default=None,ge=0)
    valid_time: str | None = None
    source: str | None = None
    level: str
    planner_type: str = "wa_lpa_star"
    max_wind_speed_threshold: float = 7.9
    max_rain_threshold: float = 10.0
    min_agl_height: float = 0.0
    max_cumulative_cost: float | None = None
    thresholds: Thresholds = Field(default_factory=Thresholds)


class DepartureWindowsRequest(BaseModel):
    cycle: str | None = None
    valid_time: str | None = None
    """Evaluate departures on a saved, fixed-height route using hourly WRF."""

    average_ground_speed_mps: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    source: str | None = Field(default=None, pattern="^(wrf|gfs)$")
    thresholds: Thresholds = Field(default_factory=Thresholds)
    wind_shear: WindShearSettings = Field(default_factory=WindShearSettings)

    @model_validator(mode="after")
    def validate_finite_limits(self):
        if self.source == "gfs" and "average_ground_speed_mps" not in self.model_fields_set:
            self.average_ground_speed_mps = 50.0
        values = [*self.thresholds.model_dump().values(), self.wind_shear.horizontal.hard_delta_wind_vector_ms]
        if not all(math.isfinite(value) and value >= 0 for value in values) or self.thresholds.danger <= 0:
            raise ValueError("风速和水平风切变阈值必须为有效的有限数值")
        return self


class FlightProfileRequest(DepartureWindowsRequest):
    departure_at: datetime
    dataset_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("departure_at")
    @classmethod
    def require_timezone(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("起飞时间必须包含时区")
        return value


class GeometryDepartureWindowsRequest(DepartureWindowsRequest):
    """Evaluate an unsaved route without writing routes or export files."""

    points: list[tuple[float, ...]] = Field(min_length=2, max_length=20000)
    level: str
    name: str = "新规划航线"
    dataset_id: str | None = Field(default=None, min_length=1, max_length=64)


class GeometryFlightProfileRequest(FlightProfileRequest):
    points: list[tuple[float, ...]] = Field(min_length=2, max_length=20000)
    level: str
    name: str = "新规划航线"


class V3RouteMetadata(BaseModel):
    trajectory_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    altitude_msl_m: float = Field(gt=0, le=20000, allow_inf_nan=False)
    airspeed_mps: float = Field(default=10.0, ge=10.0, le=10.0)
    time_model: str = Field(default="constant_airspeed", pattern="^constant_airspeed$")
    version: str = "WRF_V3_PLATFORM_V1"
    endpoint_tolerance_m: float = Field(default=1.0,ge=1.0,le=1.0)


class V3WindowRequest(DepartureWindowsRequest):
    trajectory_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    dataset_id: str | None = Field(default=None, pattern=r"^v3_[a-f0-9]{60}$")
    route_id: str | None = None
    name: str = Field(default="V3 固定航线", max_length=120)


class V3ProfileRequest(V3WindowRequest):
    departure_at: datetime

    @field_validator("departure_at")
    @classmethod
    def require_timezone(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("起飞时间必须包含时区")
        return value


class RouteRecord(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    start: tuple[float, float]
    end: tuple[float, float]
    points: list[tuple[float, ...]] = Field(min_length=2)
    level: str
    source: str = Field(default="wrf", pattern="^(wrf|gfs)$")
    cycle: str | None = None
    forecast_hour: float | None = Field(default=None,ge=0,allow_inf_nan=False)
    forecast_minutes: int | None = Field(default=None,ge=0)
    valid_time: str | None = None
    mission_items: list[dict[str, Any]] | None = None
    v3: V3RouteMetadata | None = None


class ExportedRouteRenameRequest(BaseModel):
    route_name: str | None = Field(default=None, min_length=1, max_length=120)
    file_name: str | None = Field(default=None, min_length=1, max_length=180)
