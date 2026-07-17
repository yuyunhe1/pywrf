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

class RouteAnalyzeRequest(BaseModel):
    """Route analysis request. Each route point uses [longitude, latitude]."""

    points: list[tuple[float, ...]] = Field(min_length=2)
    cycle: str | None = None
    forecast_hour: int | None = Field(default=None, ge=0)
    valid_time: str | None = None
    source: str | None = None
    level: str
    thresholds: Thresholds = Field(default_factory=Thresholds)
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
