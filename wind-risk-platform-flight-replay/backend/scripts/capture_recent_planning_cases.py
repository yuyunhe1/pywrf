"""Read recent existing jobs and saved geometry without modifying history."""
import json
from pathlib import Path
import re
import sqlite3
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT/'data/route_quality_tests'


def main():
    WORK.mkdir(parents=True,exist_ok=True)
    runtime=json.loads((ROOT/'data/platform_runtime/v3-running.json').read_text(encoding='utf-8-sig'))
    text=Path(runtime['backend_stdout']).read_text(encoding='utf-8',errors='replace')
    matches=re.findall(r'/api/route/(plan|recommendation)/jobs/([a-f0-9-]{36})',text)
    unique=list(dict.fromkeys(reversed(matches)))[:14]
    known=set()
    for name in ('first_feasible_tests','segment_comparison_tests','planning_demo_tests'):
        file=ROOT/'data'/name/'production-readiness.json'
        if file.exists():
            known.update(case.get('job_id') for case in json.loads(file.read_text(encoding='utf-8')).get('cases',[]))
    rows=[]
    for kind,identity in unique:
        try:
            with urlopen(f'http://127.0.0.1:8001/api/route/{kind}/jobs/{identity}',timeout=15) as response:
                state=json.load(response)
            (WORK/f'captured-{identity}.json').write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
            value=state.get('result') or {}
            rows.append(dict(job_id=identity,kind=kind,known_validation=identity in known,status=state['status'],
                has_result=bool(value),points=len(value.get('points',[])),strategy=value.get('planning_strategy'),
                source=value.get('source'),level=value.get('level'),departure_at=value.get('departure_at'),
                start=value.get('points',[None])[0],end=value.get('points',[None])[-1],
                distance_km=value.get('distance_km'),shape=value.get('shape_quality'),
                revision=state['revision'],elapsed_seconds=state['elapsed_seconds']))
        except Exception as error:
            rows.append(dict(job_id=identity,kind=kind,error=str(error)))
    db=ROOT/'wind-risk-platform-flight-replay/backend/routes.sqlite3'
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as connection:
        connection.row_factory=sqlite3.Row
        saved=[dict(row) for row in connection.execute('SELECT route_id,name,start_json,end_json,level,cycle,created_at FROM routes ORDER BY created_at DESC LIMIT 5')]
    report=dict(recent_jobs=rows,recent_saved=saved,writes_history=False)
    (WORK/'recent-cases.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=='__main__': main()
