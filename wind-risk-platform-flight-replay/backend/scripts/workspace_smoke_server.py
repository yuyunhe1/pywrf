"""UI acceptance service; real algorithms and storage over deterministic weather.

The weather fixtures and isolated DB are shared with custom_routes_smoke_server.
No product endpoint or database schema is changed.
"""
import os
from datetime import timedelta
from pathlib import Path

os.environ.setdefault('CUSTOM_ROUTE_TEST_DIR', str(Path(__file__).resolve().parents[3] / 'data/ui_redesign_tests'))
import custom_routes_smoke_server as fixture
from app.models import FlightProfileRequest


def prepare(request):
    source = request.source or 'wrf'
    dataset = fixture.DATASETS[source, request.level]
    request = request.model_copy(update={'time_aware': True, 'dataset_id': dataset.dataset_id,
        'source': source, 'departure_at': request.departure_at or fixture.BASE + timedelta(minutes=30)})
    FlightProfileRequest(**request.model_dump(include={'departure_at', 'average_ground_speed_mps', 'thresholds', 'wind_shear'}))
    return request, dataset


fixture.main.prepare_temporal_request = prepare

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(fixture.main.app, host='127.0.0.1', port=8002)
