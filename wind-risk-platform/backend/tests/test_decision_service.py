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
    assert result["navigation_decision"] == "允许通航"


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


def test_endpoint_hard_wind_skips_candidate_graph_search(monkeypatch):
    from app import decision_service

    grid = _grid(3.0)
    grid.u[3, 0] = 8.2

    def fail_if_planner_runs(*args, **kwargs):
        raise AssertionError("graph search should be skipped for a hard-blocked endpoint")

    monkeypatch.setattr(decision_service, "_plan_candidate", fail_if_planner_runs)
    result = decision_service.decide_navigation(
        (0.0, 3.0),
        (6.0, 3.0),
        [grid],
        planner_type="wa_lpa_star",
        planner_hard_max_wind_speed=7.9,
    )

    assert result["navigation_decision"] == "禁止通航"
    assert result["navigation_allowed"] is False
    assert result["candidate_results"][0]["decision_hint"] == "禁止通航"
    assert "已跳过航线图搜索" in result["candidate_results"][0]["reason"]
