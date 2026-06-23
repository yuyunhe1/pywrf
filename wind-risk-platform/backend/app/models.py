"""API request models."""

from pydantic import BaseModel, Field, field_validator, model_validator


class Thresholds(BaseModel):
    """Wind risk thresholds in m/s."""

    safe: float = 3
    notice: float = 6
    warning: float = 8
    danger: float = 10

    @field_validator("notice", "warning", "danger")
    @classmethod
    def validate_order(cls, value: float, info):
        previous = {
            "notice": info.data.get("safe"),
            "warning": info.data.get("notice"),
            "danger": info.data.get("warning"),
        }[info.field_name]
        if previous is not None and value <= previous:
            raise ValueError("thresholds must be strictly increasing")
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
            raise ValueError("cycle or valid_time is required")
        if self.valid_time is None and self.forecast_hour is None:
            raise ValueError("forecast_hour is required when valid_time is not provided")
        return self
