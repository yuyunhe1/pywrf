"""Isolated historical workflow; stop after its first certified alternative."""
from copy import deepcopy
from datetime import datetime
import json
from threading import Event
from time import monotonic

from verify_route_quality_case import BACKEND, WORK
from app.models import RecommendationJobRequest
from app.recommendations import Workflow
from app.planning_control import planning_scope, PlanningTimedOut
from app.wrf_timeline import load_dataset
from app.route_quality import assess_route_quality
from app.direction_pool import shutdown_direction_pool


class ConfirmedAlternativeFound(Exception): pass


def main():
    case=json.loads((BACKEND/'tests/fixtures/route_quality_user_20261115.json').read_text(encoding='utf-8'))
    departure=datetime.fromisoformat(case['departure_at'].replace('Z','+00:00'))
    data=load_dataset(case['level'],departure,case['cycle'])
    assert data.dataset_id==case['dataset_id']
    req=RecommendationJobRequest(source=case['source'],level=case['level'],cycle=case['cycle'],
        start=case['points'][0],end=case['points'][-1],departure_at=departure,
        average_ground_speed_mps=case['average_ground_speed_mps'],planning_strategy=case['planning_strategy'],
        planner_type='wa_lpa_star',prefer_simple_route=True,route_quality_extra_seconds=10)
    published=[];state={};confirmed=False;events=[];began=monotonic();status='completed';work=None
    def report(value):
        nonlocal confirmed
        state.update(deepcopy(value))
        if value.get('search_progress',{}):
            quality=value['search_progress'].get('route_quality')
            if quality:events.append(dict(seconds=monotonic()-began,**quality))
        cards=state.get('recommendations',[])
        if not confirmed and any(card.get('certification')=='windows_confirmed' and card.get('windows',{}).get('departure_windows') for card in cards):
            confirmed=True
            raise ConfirmedAlternativeFound()
    try:
        try:
            with planning_scope(began+160,Event()):
                work=Workflow(req,data,report,published.append,now=lambda:departure)
                work.run()
        except ConfirmedAlternativeFound:status='stopped_after_confirmed_alternative'
        except PlanningTimedOut:status='isolated_test_budget_reached'
        cards=sorted(work.cards.values(),key=lambda card:card['first_departure_at']) if work else []
        for card in cards:assert assess_route_quality(card['plan']['points'],req.average_ground_speed_mps)['acceptable']
        for value in published:assert value['display_quality']['acceptable']
        report_data=dict(status=status,elapsed_seconds=monotonic()-began,source_job_id=case['job_id'],
            dataset_id=data.dataset_id,isolated_clock=departure.isoformat(),attempts=work.attempts,
            current_route_published=len(published),recommendations=cards,quality_events=events,
            confirmed_alternative=confirmed,writes_history=False,production_clock_modified=False)
        (WORK/'real-quality-workflow.json').write_text(json.dumps(report_data,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({**{k:v for k,v in report_data.items() if k not in ('recommendations','quality_events','attempts')},
            'attempts':[{k:v for k,v in row.items() if k not in ('statistics','started_monotonic')} for row in work.attempts],
            'recommendations':[dict(first_departure_at=c['first_departure_at'],certification=c['certification'],
                distance_km=c['plan']['distance_km']) for c in cards]},ensure_ascii=True,indent=2))
        assert confirmed or published,'no certified simple-route alternative was found'
    finally:shutdown_direction_pool()


if __name__=='__main__':main()
