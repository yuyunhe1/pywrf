"""Measure cold-worker first solutions using archived real native d02 WRF.

Run with the API interpreter; numerical work uses WRF_V3_PYTHON. This stops
each owned worker after its first fully admitted result, without running any
execution simulation or modifying saved routes.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from threading import Event
from time import monotonic

BACKEND=Path(__file__).resolve().parents[1]
ROOT=BACKEND.parents[1]
sys.path.insert(0,str(BACKEND))
sys.stdout.reconfigure(encoding='utf-8')
from app import v3_native,v3_service
from app.models import RoutePlanRequest
from app.planning_control import planning_scope

WORK=ROOT/'data/v3_integration_tests'


class FirstSolution(Exception):
    def __init__(self,result,elapsed):
        self.result,self.elapsed=result,elapsed


def main():
    original=json.loads((WORK/'fast-first.json').read_text(encoding='utf-8'))['request']
    long_case=json.loads((WORK/'long-fixture-candidates.json').read_text(encoding='utf-8'))[0]
    dataset=v3_native.dataset(300)
    original.update(departure_at='2026-11-04T02:00:00Z',dataset_id=dataset['dataset_id'])
    cases=[('short_0_95km',[117.6,31.],[117.61,31.]),
           ('medium_9_5km',[117.6,31.],[117.7,31.]),
           ('long_87km',long_case['start'],long_case['end'])]
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'cases':[],
            'execution_simulation':False,'timing_scope':'cold subprocess through admitted first publication'}
    for name,start,end in cases:
        request=RoutePlanRequest(**{**original,'start':start,'end':end,'planning_strategy':'distance_priority'})
        dataset=v3_native.get_dataset(request.dataset_id)
        began=monotonic()
        def publish(result):
            assert result['analysis']['navigation_allowed']
            raise FirstSolution(result,monotonic()-began)
        try:
            with planning_scope(monotonic()+600,Event()):
                v3_service.compute_plan(request,publish,dataset)
            raise AssertionError('No first solution published')
        except FirstSolution as found:
            result=found.result
            material=v3_service.trajectory(result['trajectory_id'])
            curve_file=v3_service.artifact_root()/'trajectories'/(result['trajectory_id']+'.npz')
            assert hashlib.sha256(curve_file.read_bytes()).hexdigest()==result['trajectory_id']
            assert material['C']['valid'] and material['collision']['valid']
            assert result['flight_profile']['certificate']['status']=='allowed'
            assert material['endpoint_headings_constrained'] is False
            assert result['planning_activity']['planning_only'] is True
            assert result['planning_activity']['execution_modules_loaded']==[]
            row={'name':name,'first_feasible_seconds':found.elapsed,'distance_km':result['distance_km'],
                 'flight_duration_seconds':result['flight_profile']['flight_duration_seconds'],
                 'trajectory_id':result['trajectory_id'],'dataset_id':result['dataset_id'],
                 'arrival_at':result['arrival_at'],'departure_at':result['departure_at'],
                 'C':material['C'],'initialization_stats':material['route'].get('initialization_stats'),
                 'independent_validation_max_step_s':material['route'].get('independent_validation_max_step_s'),
                 'endpoint_error_m':result['endpoint_error_m'],'endpoint_tolerance_m':result['endpoint_tolerance_m'],
                 'map_segments':len(result['segments']),'profile_samples':len(result['analysis']['samples']),
                 'certificate':result['flight_profile']['certificate']}
            (WORK/(name+'-plan.json')).write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
        except Exception as exc:
            row={'name':name,'error':type(exc).__name__+': '+str(exc),'elapsed_seconds':monotonic()-began}
        report['cases'].append(row)
        (WORK/'planning-performance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(row,ensure_ascii=False),flush=True)
    assert all('error' not in row for row in report['cases']),report


if __name__=='__main__':
    main()
