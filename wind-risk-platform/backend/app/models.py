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

    points: list[tuple[float, float]] = Field(min_length=2)
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
    points: list[tuple[float, float]] = Field(default_factory=lambda: [(0, 0), (0, 0)])


class RouteRecord(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    start: tuple[float, float]
    end: tuple[float, float]
    points: list[tuple[float, float]] = Field(min_length=2)
    level: str
    cycle: str | None = None
    forecast_hour: int | None = None
