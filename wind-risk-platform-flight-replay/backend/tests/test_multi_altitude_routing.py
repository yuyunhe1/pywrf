import numpy as np

from app.models import Thresholds
from app.multi_altitude_routing import MultiAltitudeConfig, plan_multi_altitude_route
from app.wind_provider import WindGrid


def test_multi_altitude_route_can_choose_lower_wind_layer():
    lons = np.linspace(118.0, 119.0, 6)
    lats = np.linspace(31.0, 30.0, 6)
    terrain = np.zeros((6, 6), dtype=float)
    u_low = np.ones((6, 6), dtype=float) * 2.0
    v_low = np.zeros((6, 6), dtype=float)
    u_low[:, 2:4] = 7.5
    u_high = np.ones((6, 6), dtype=float) * 1.0
    v_high = np.zeros((6, 6), dtype=float)
    common = {
        "lons": lons,
        "lats": lats,
        "cycle": "2026-06-15 00:00 UTC",
        "forecast_hour": 3,
        "valid_time": "2026-06-15 03:00 UTC",
        "source": "synthetic",
        "terrain": terrain,
    }
    grids = [
        WindGrid(u=u_low, v=v_low, level="100m AGL", **common),
        WindGrid(u=u_high, v=v_high, level="200m AGL", **common),
    ]

    result = plan_multi_altitude_route(
        grids,
        (118.0, 30.6),
        (119.0, 30.6),
        Thresholds(danger=7.9),
        cost_config={"weights": {"beta_wind": 120.0}},
        config=MultiAltitudeConfig(
            min_agl_height_m=60,
            max_adjacent_msl_change_m=120,
            max_climb_gradient=0.2,
            max_iterations=20000,
        ),
    )

    assert result["altitude_summary"]["max_agl_m"] == 200.0
    assert all(point[3] == 200.0 for point in result["points"])
