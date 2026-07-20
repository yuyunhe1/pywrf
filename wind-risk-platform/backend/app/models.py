"""API request models."""

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
    enabled: bool = True
    caution_delta_v_10m_ms: float = Field(default=1.0, ge=0)
    hard_delta_v_10m_ms: float = Field(default=3.0, gt=0)
    hard_delta_v_30m_ms: float = Field(default=6.0, gt=0)
    caution_direction_change_deg: float = Field(default=20.0, ge=0, le=180)
    hard_direction_change_deg: float = Field(default=45.0, gt=0, le=180)
    hard_constraint_enabled: bool = True

    @model_validator(mode="after")
    def validate_threshold_order(self):
        if self.caution_delta_v_10m_ms >= self.hard_delta_v_10m_ms:
            raise ValueError("垂直风切变谨慎阈值必须小于硬约束阈值")
        if self.caution_direction_change_deg >= self.hard_direction_change_deg:
            raise ValueError("垂直风向谨慎阈值必须小于硬约束阈值")
        return self


class HorizontalWindShearSettings(BaseModel):
    enabled: bool = True
    hard_delta_v_1km_ms: float = Field(default=2.6, gt=0)
    hard_direction_change_deg: float = Field(default=45.0, gt=0, le=180)
    hard_constraint_enabled: bool = True


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
    forecast_hour: int | None = Field(default=None, ge=0)
    valid_time: str | None = None
    source: str | None = None
    level: str
    thresholds: Thresholds = Field(default_factory=Thresholds)
    wind_shear: WindShearSettings = Field(default_factory=WindShearSettings)
    sample_interval_km: float = Field(default=3.0, gt=0, le=100)

    @model_validator(mode="after")
    def validate_time_selection(self):
        if self.cycle is None and self.valid_time is None:
            raise ValueError("必须提供 cycle 或 valid_time")
        if self.valid_time is None and self.forecast_hour is None:
            raise ValueError("未提供 valid_time 时，必须提供 forecast_hour")
        return self


class RoutePlanRequest(RouteAnalyzeRequest):
    start: tuple[float, float]
    end: tuple[float, float]
    planner_type: str = "wa_lpa_star"
    aircraft_model: str = "fixed_wing"
    planning_strategy: str = "wind_avoidance"
    points: list[tuple[float, ...]] = Field(default_factory=lambda: [(0, 0), (0, 0)])


class RouteDecisionRequest(BaseModel):
    """Navigation decision request. Each point uses [longitude, latitude]."""

    start: tuple[float, float]
    end: tuple[float, float]
    candidate_valid_times: list[str] = Field(default_factory=list)
    candidate_offsets_hours: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 6])
    cycle: str | None = None
    forecast_hour: int | None = Field(default=None, ge=0)
    valid_time: str | None = None
    source: str | None = None
    level: str
    planner_type: str = "wa_lpa_star"
    max_wind_speed_threshold: float = 7.9
    max_rain_threshold: float = 10.0
    min_agl_height: float = 0.0
    max_cumulative_cost: float | None = None
    thresholds: Thresholds = Field(default_factory=Thresholds)


class RouteRecord(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    start: tuple[float, float]
    end: tuple[float, float]
    points: list[tuple[float, ...]] = Field(min_length=2)
    level: str
    cycle: str | None = None
    forecast_hour: int | None = None


class ExportedRouteRenameRequest(BaseModel):
    route_name: str | None = Field(default=None, min_length=1, max_length=120)
    file_name: str | None = Field(default=None, min_length=1, max_length=180)
