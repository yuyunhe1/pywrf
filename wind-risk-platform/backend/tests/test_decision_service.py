import numpy as np


def _grid(speed: float, valid_time: str = "2026-06-15 03:00 UTC"):
    from app.wind_provider import WindGrid

    lons = np.arange(7, dtype=float)
    lats = np.arange(6, -1, -1, dtype=float)
    return WindGrid(
        lons=lons,
        lats=lats,
        u=np.full((7, 7), speed, dtype=float),
        v=np.zeros((7, 7), dtype=float),
        cycle="2026-06-15 00:00 UTC",
        forecast_hour=3,
        level="100m AGL",
        valid_time=valid_time,
    )


def test_decision_suitable_for_current_time():
    from app.decision_service import DECISION_SUITABLE, decide_navigation

    result = decide_navigation(
        (0.0, 3.0),
        (6.0, 3.0),
        [_grid(3.0)],
        planner_type="wa_lpa_star",
    )

    assert result["decision"] == DECISION_SUITABLE
    assert result["recommended_start_time"] == "2026-06-15 03:00 UTC"
    assert result["path_summary"]["max_wind_speed"] < 5.4


def test_decision_delays_when_current_exceeds_business_threshold_but_is_plannable():
    from app.decision_service import DECISION_DELAY, decide_navigation

    result = decide_navigation(
        (0.0, 3.0),
        (6.0, 3.0),
        [
            _grid(6.0, "2026-06-15 03:00 UTC"),
            _grid(3.0, "2026-06-15 04:00 UTC"),
        ],
        planner_type="lpa_star",
        max_wind_speed_threshold=5.4,
    )

    assert result["decision"] == DECISION_DELAY
    assert result["recommended_start_time"] == "2026-06-15 04:00 UTC"
    assert result["candidate_results"][0]["available"] is True
    assert "最大风速超过阈值" in result["candidate_results"][0]["reason"]


def test_decision_pauses_when_all_candidates_exceed_decision_threshold():
    from app.decision_service import DECISION_PAUSE, decide_navigation

    result = decide_navigation(
        (0.0, 3.0),
        (6.0, 3.0),
        [_grid(6.0), _grid(6.2, "2026-06-15 04:00 UTC")],
        planner_type="astar",
        max_wind_speed_threshold=5.4,
    )

    assert result["decision"] == DECISION_PAUSE
    assert result["recommended_start_time"] is None


def test_decision_changes_when_user_wind_threshold_changes():
    from app.decision_service import DECISION_PAUSE, DECISION_SUITABLE, decide_navigation

    low_threshold = decide_navigation(
        (0.0, 3.0),
        (6.0, 3.0),
        [_grid(6.0)],
        planner_type="lpa_star",
        max_wind_speed_threshold=5.4,
    )
    high_threshold = decide_navigation(
        (0.0, 3.0),
        (6.0, 3.0),
        [_grid(6.0)],
        planner_type="lpa_star",
        max_wind_speed_threshold=6.5,
    )

    assert low_threshold["decision"] == DECISION_PAUSE
    assert high_threshold["decision"] == DECISION_SUITABLE
