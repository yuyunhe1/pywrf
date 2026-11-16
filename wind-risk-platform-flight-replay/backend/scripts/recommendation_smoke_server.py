"""Isolated real workflow/UI service over reproducible hazardous weather."""
import os
from pathlib import Path
from dataclasses import replace
os.environ.setdefault('CUSTOM_ROUTE_TEST_DIR',str(Path(__file__).resolve().parents[3]/'data/recommendation_tests/browser-storage'))
os.environ.update(ROUTE_PLAN_TIMEOUT_SECONDS='120',ROUTE_PLAN_DEPARTURE_ATTEMPT_SECONDS='.8',
                  ROUTE_PLAN_SEGMENTED_IMPROVEMENT_SECONDS='1',ROUTE_PLAN_OWNER_TIMEOUT_SECONDS='15')
import workspace_smoke_server as workspace
fixture=workspace.fixture
for source in ('wrf','gfs'):
    data=fixture.DATASETS[source,'100m AGL']
    frames=[]
    for hour,(stamp,grid) in enumerate(data.frames):
        u,v=grid.u.copy(),grid.v.copy()
        if hour<=2: u[7:10,11]=12.
        frames.append((stamp,replace(grid,u=u,v=v)))
    data=replace(data,dataset_id='recommendation-'+source+'-100',frames=tuple(frames))
    fixture.DATASETS[source,'100m AGL']=data
    fixture.wrf_timeline._DATASETS[data.dataset_id]=data

for module in (fixture.departure_windows,fixture.flight_replay):
    module.load_dataset=lambda level,now,cycle=None:fixture.DATASETS['wrf',level]
    module.load_gfs_dataset=lambda level,now,cycle=None:fixture.DATASETS['gfs',level]
fixture.main.wrf_cache_provider.records_for_cycle=lambda cycle:[]
old_grid=fixture.fixture_grid
fixture.fixture_grid=lambda hour,level,source: (fixture.DATASETS[source,level].frames[hour][1]
    if fixture.DATASETS[source,level].frames else old_grid(hour,level,source))

if __name__=='__main__':
    import uvicorn
    uvicorn.run(fixture.main.app,host='127.0.0.1',port=8002)
