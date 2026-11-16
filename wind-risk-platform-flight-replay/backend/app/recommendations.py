"""Owned parent workflows: departure fallback and fixed-anchor detours."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from threading import Event, RLock
from time import monotonic

from .departure_windows import prepare_evaluation, describe_evaluation
from .detour_filter import DetourWeatherFilter
from .direction_pool import direction_lease
from .flight_replay import build_profile
from .gfs_provider import _current_utc
from .grid_coverage import SPATIAL_RISK_MODEL
from .local_weather import LocalWeather
from .models import FlightProfileRequest
from .planning_control import planning_scope, current_control, checkpoint, PlanningCancelled, PlanningTimedOut
from .planning_limits import seconds_setting, integer_setting, memory_pressure
from .planning_objective import OBJECTIVE_MODEL
from .route_context import geometry_route
from .route_jobs import RouteJobManager, TERMINAL
from .route_options import build_strategy_cost_config
from .route_service import haversine_km
from .temporal_plan_service import compute_temporal_plan
from .wrf_timeline import iso


def stamp(value):
    return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


def intervals(value):
    return [(stamp(row['start']),stamp(row['end'])) for row in value.get('departure_windows',[])]


def duration(ranges):
    return sum(max(0.,b-a) for a,b in ranges)


def overlap(left,right):
    return sum(max(0.,min(b,d)-max(a,c)) for a,b in left for c,d in right)


def trim_windows(value, now):
    result=deepcopy(value)
    result['departure_windows']=[]
    for row in value.get('departure_windows',[]):
        begin,end=stamp(row['start']),stamp(row['end'])
        original_begin=begin
        if begin<now:
            begin=math.ceil(now/300)*300
        if begin<=end:
            updated={**row,'start':iso(begin)}
            if row.get('first_arrival'): updated['first_arrival']=iso(stamp(row['first_arrival'])+begin-original_begin)
            result['departure_windows'].append(updated)
    return result


def gains(baseline,candidate):
    a,b=intervals(baseline),intervals(candidate)
    shared=overlap(a,b)
    confirmed=not baseline.get('coverage_incomplete') and not candidate.get('coverage_incomplete')
    advance=(a[0][0]-b[0][0])/60 if a and b else None
    return dict(benefit_confirmed=confirmed,earlier_minutes=advance,
                gained_minutes=(duration(b)-shared)/60,lost_minutes=(duration(a)-shared)/60,
                net_minutes=(duration(b)-duration(a))/60,baseline_has_windows=bool(a),
                improves=confirmed and bool(b) and (not a or advance>0 or advance==0 and duration(b)>duration(a)))


def rank(card):
    return (stamp(card['first_departure_at']),-duration(intervals(card['windows'])),
            card['plan'].get('objective_cost',math.inf),card['plan']['distance_km'])


class Workflow:
    def __init__(self,request,dataset,report,publish,now=None):
        self.request,self.dataset,self.report,self.publish=request,dataset,report,publish
        self.now=now or _current_utc
        self.control=current_control()
        self.stopped=Event()
        self.reference=request.departure_at or self.now()
        self.analysis_from=max(self.reference,self.now())
        self.cards={}
        self.simple_seeds=[]
        self.lock=RLock()
        self.attempt_seconds=seconds_setting('ROUTE_PLAN_DEPARTURE_ATTEMPT_SECONDS',60)
        self.baseline=None
        self.detour_filter=None
        self.discarded_equivalent_detours=0
        if request.mode=='fixed_route' and dataset.frames:
            i=request.segment_index
            self.detour_filter=DetourWeatherFilter(dataset,request.points[i:i+2],request.average_ground_speed_mps)
        self.version=request.baseline_version or hashlib.sha256(json.dumps(
            [request.points,request.start,request.end,dataset.dataset_id,request.level],sort_keys=True).encode()).hexdigest()
        self.report(dict(stage='preparing',dataset_id=dataset.dataset_id,baseline_version=self.version,
                         requested_departure_at=self.reference.isoformat(),mode=request.mode,recommendations=[],attempts=[]))
        self.attempts=[]

    def update_attempt(self,record,**changes):
        with self.lock:
            record.update(changes)
            self.report({'attempts':deepcopy(self.attempts)})

    def windows(self,points):
        request=self.request
        shear=request.wind_shear
        limit=(shear.horizontal.hard_delta_wind_vector_ms if shear.enabled and shear.horizontal.enabled
               and shear.horizontal.hard_constraint_enabled else None)
        with planning_scope(min(self.control.deadline,monotonic()+600),self.control.cancel):
            check=prepare_evaluation(points,list(self.dataset.frames),request.average_ground_speed_mps,
                request.thresholds.danger,limit,self.dataset.coverage_start,self.dataset.coverage_end,source=self.dataset.source,
                wind_time_model=self.dataset.time_model)
            result=describe_evaluation(check,self.analysis_from,request.average_ground_speed_mps)
        return {**result,'dataset_id':self.dataset.dataset_id,'source':self.dataset.source,'level':request.level}

    def _full_detour(self,segment,departure,full_weather):
        req=self.request;i=req.segment_index
        segment_points=[tuple(p[:2]) for p in segment['points']]
        a,b=tuple(req.points[i][:2]),tuple(req.points[i+1][:2])
        if segment_points[0]!=a or segment_points[-1]!=b:
            return None
        maximum=haversine_km(a,b)*(1+req.max_detour_ratio)
        if sum(haversine_km(x,y) for x,y in zip(segment_points,segment_points[1:]))>maximum+1e-8:
            return None
        if self.detour_filter.redundant(segment_points):
            with self.lock:
                self.discarded_equivalent_detours+=1
            return None
        points=[*req.points[:i],*segment_points,*req.points[i+2:]]
        route=geometry_route(points,req.level,source=self.dataset.source)
        profile=build_profile(route,FlightProfileRequest(departure_at=departure,
            average_ground_speed_mps=req.average_ground_speed_mps,source=req.source,
            thresholds=req.thresholds,wind_shear=req.wind_shear),self.dataset)
        if not profile['analysis']['navigation_allowed']:
            return None
        cost=full_weather.route_cost(points)
        if not math.isfinite(cost):
            return None
        return {**segment,'points':points,'segment_points':segment_points,'analysis':profile['analysis'],
                'flight_profile':profile,'segments':profile['segments'],'route_context':{**route,'dataset_id':self.dataset.dataset_id},
                'distance_km':profile['analysis']['total_distance_km'],'departure_at':profile['departure_at'],
                'arrival_at':profile['arrival_at'],'objective_cost':cost}

    def attempt(self,departure,*,initial=False):
        checkpoint()
        record=dict(departure_at=departure.isoformat(),status='queued',initial=initial)
        with self.lock:
            self.attempts.append(record)
        self.update_attempt(record)
        best=None;found=Event();started=None;last_stats=None;quality_deadline=None
        queued=monotonic()
        parent=self.control
        class Stop:
            def cancelled_by_owner(inner):
                return parent.cancel.is_set() or self.stopped.is_set()
            def is_set(inner):
                return inner.cancelled_by_owner() or (not initial and found.is_set()) or (
                    started is not None and not found.is_set() and monotonic()>=max(
                        started+self.attempt_seconds,quality_deadline or 0))
        def observe(value):
            nonlocal last_stats,quality_deadline
            if value.get('route_quality',{}).get('status')=='searching_alternative':
                quality_deadline=value['route_quality']['retry_until_monotonic']
            if value.get('statistics') is not None: last_stats=value['statistics']
            if record['status']=='preparing': self.update_attempt(record,status='searching')
            self.report({'search_progress':value,'active_attempt_at':departure.isoformat()})
        try:
            class LeaseStop:
                def is_set(inner): return parent.cancel.is_set() or self.stopped.is_set()
            with planning_scope(parent.deadline,LeaseStop()):
                with direction_lease():
                    started=monotonic()
                    self.update_attempt(record,status='preparing',started_monotonic=started,queue_wait_seconds=started-queued)
                    with planning_scope(parent.deadline,Stop(),progress=observe):
                        req=self.request.model_copy(update={'departure_at':departure,'max_route_distance_km':None})
                        validator=None;converted={}
                        if self.request.mode=='fixed_route':
                            i=req.segment_index
                            prefix=sum(haversine_km(a,b)*1000/req.average_ground_speed_mps
                                       for a,b in zip(req.points[:i+1],req.points[1:i+1]))
                            full_weather=LocalWeather(self.dataset,req,build_strategy_cost_config(req.planning_strategy))
                            a,b=req.points[i][:2],req.points[i+1][:2]
                            req=req.model_copy(update={'start':a,'end':b,'departure_at':datetime.fromtimestamp(
                                departure.timestamp()+prefix,timezone.utc),
                                'max_route_distance_km':haversine_km(a,b)*(1+req.max_detour_ratio)})
                            def validator(value):
                                full=self._full_detour(value,departure,full_weather)
                                if full: converted[tuple(map(tuple,value['points']))]=full
                                return full is not None
                        with self.lock:
                            seeds=[*req.seed_routes,*self.simple_seeds] if not initial else req.seed_routes
                            req=req.model_copy(update={'seed_routes':[points for points in seeds
                                if tuple(points[0])==tuple(req.start) and tuple(points[-1])==tuple(req.end)][:4]})
                        def accept(value):
                            nonlocal best
                            checkpoint()
                            candidate=(converted.get(tuple(map(tuple,value['points']))) if validator else value)
                            if candidate is None: return
                            best=deepcopy(candidate)
                            self.update_attempt(record,status='found',
                                first_feasible_seconds=record.get('first_feasible_seconds',monotonic()-started),
                                last_improvement_seconds=monotonic()-started)
                            found.set()
                            if initial: self.publish(candidate)
                            if initial: self.report({'stage':'specified_finishing' if req.stop_on_first_feasible else 'specified_improving',
                                'requested_status':'allowed','search_progress':None,
                                'message':'首条可行航线已验收，正在结束搜索' if req.stop_on_first_feasible else '已找到可行路线，继续比较候选'})
                        result=compute_temporal_plan(req,self.dataset,[-180,-90,180,90],0,accept,candidate_validator=validator)
                        with self.lock:
                            for points in result.get('planning_diagnostics',{}).get('route_quality',{}).get('simple_seeds',[]):
                                if points not in self.simple_seeds and len(self.simple_seeds)<4: self.simple_seeds.append(points)
                        if result.get('analysis',{}).get('navigation_allowed') and not validator:
                            best=result
                        elif not best:
                            self.update_attempt(record,status=result.get('route_status','unconfirmed'),message=result.get('planning_message'))
        except PlanningCancelled:
            if parent.cancel.is_set() or self.stopped.is_set():
                self.update_attempt(record,status='cancelled')
                raise
            if best is None: self.update_attempt(record,status='unconfirmed',message=f'{self.attempt_seconds:g}秒内尚未找到完整验收解')
        except PlanningTimedOut:
            if monotonic()>=parent.deadline:
                record['status']='timed_out'
                raise
            self.update_attempt(record,status='unconfirmed')
        except MemoryError as exc:
            self.update_attempt(record,status='resource_limit',message=str(exc))
        except (ValueError,OSError) as exc:
            self.update_attempt(record,status='data_insufficient',message=str(exc))
        finally:
            diagnostics=(best or {}).get('planning_diagnostics',{})
            preparation=diagnostics.get('full_field_load_seconds')
            if preparation is None and last_stats:
                preparation=last_stats.get('weather',{}).get('full_field_load_seconds')
            elapsed=monotonic()-started if started else 0
            self.update_attempt(record,elapsed_seconds=elapsed,statistics=last_stats,
                preparation_seconds=preparation,search_seconds=max(0,elapsed-preparation) if preparation is not None else None)
        return best

    def add_candidate(self,plan):
        checkpoint()
        identity=hashlib.sha256(json.dumps([plan['points'],self.dataset.dataset_id],sort_keys=True).encode()).hexdigest()[:24]
        seed=plan['departure_at']
        previous=self.cards.get(identity)
        if previous is not None:
            previous['verified_departures']=sorted(set([*previous.get('verified_departures',[previous['verified_departure_at']]),seed]),key=stamp)
            if stamp(seed)<stamp(previous['first_departure_at']): previous['first_departure_at']=seed
            if stamp(seed)<stamp(previous['verified_departure_at']): previous['verified_departure_at']=seed
            self.emit_cards()
            if not previous['windows'].get('coverage_incomplete'): return
        light={key:deepcopy(plan[key]) for key in ('points','source','level','dataset_id','departure_at','arrival_at',
            'average_ground_speed_mps','distance_km','objective_cost','objective_model','spatial_risk_model',
            'time_model','route_context','metadata','planner_type','planning_strategy') if key in plan}
        card=dict(id=identity,plan=light,first_departure_at=seed,verified_departure_at=seed,
                  verified_departures=previous['verified_departures'] if previous else [seed],
                  certification='single_departure',windows={'departure_windows':[],'coverage_incomplete':True},
                  baseline_version=self.version,segment_points=plan.get('segment_points'))
        if self.request.mode=='specified_time':
            self.cards[identity]=card
            self.emit_cards()
        try:
            card['windows']=self.windows(plan['points'])
            rows=card['windows']['departure_windows']
            card['certification']='windows_confirmed' if any(stamp(r['end'])>stamp(r['start']) for r in rows) else 'single_departure'
            card['first_departure_at']=min([*card['verified_departures'],*[r['start'] for r in rows]],key=stamp)
        except PlanningTimedOut:
            checkpoint()
            card['window_message']='窗口预算用尽，仅确认所列起飞时刻'
        if self.baseline is not None:
            card['metrics']=gains(self.baseline,card['windows'])
            original=sum(haversine_km(a,b) for a,b in zip(self.request.points,self.request.points[1:]))
            card['metrics'].update(extra_distance_km=plan['distance_km']-original,
                                  extra_flight_seconds=(plan['distance_km']-original)*1000/self.request.average_ground_speed_mps)
            if not card['metrics']['improves']:
                return
        self.cards[identity]=card
        self.emit_cards()

    def emit_cards(self):
        self.report({'recommendations':sorted(self.cards.values(),key=rank)[:3]})

    def run(self):
        checkpoint()
        if not self.dataset.frames:
            self.report({'stage':'data_insufficient','message':self.dataset.error or '缺少同批次预报'})
            return
        if self.request.mode=='specified_time':
            self.report({'stage':'specified_search','message':'正在搜索指定时刻的航线'})
            result=self.attempt(self.reference,initial=True)
            if result:
                self.report({'stage':'completed','message':'指定时刻规划完成','requested_status':'allowed'})
                return result
            original=self.attempts[0]
            status='blocked' if '起点风速达到硬上限' in original.get('message','') else 'unknown'
            self.report({'stage':'future_search','requested_status':status,
                         'requested_attempt_status':original['status'],
                         'message':'指定时刻尚未确认，正在并行寻找未来起飞方案','search_progress':None})
        else:
            self.report({'stage':'baseline_windows','message':'正在评估原固定路径'})
            self.baseline=self.windows(self.request.points)
            self.report({'baseline_windows':self.baseline,'stage':'detour_search','message':'正在寻找局部绕行与更早起飞窗口'})
            i=self.request.segment_index
            maximum=haversine_km(self.request.points[i],self.request.points[i+1])*(1+self.request.max_detour_ratio)
            if self.detour_filter.confined_to_one_cell(maximum):
                self.report({'stage':'completed','detour_search_skipped':True,'search_progress':None,
                    'message':'相邻航点及允许绕行范围位于同一风场网格，无需绕行'})
                return
        start=math.ceil(self.reference.timestamp()/60)*60
        finish=self.dataset.frames[-1][0]-haversine_km(self.request.start,self.request.end)*1000/self.request.average_ground_speed_mps
        # +1h/+2h first, followed by the earlier unresolved 10-minute offsets.
        seeds=[start+3600,start+7200]
        if self.request.mode=='fixed_route': seeds.insert(0,start)
        seeds.extend(range(int(start+600),int(finish)+1,600))
        seeds=list(dict.fromkeys(value for value in seeds if start<=value<=finish))
        active={};index=0
        executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='departure-attempt')
        try:
            while index<len(seeds) or active:
                checkpoint()
                concurrency=1 if memory_pressure() else 2
                while len(active)<concurrency and index<len(seeds):
                    value=seeds[index];index+=1
                    if value<self.now().timestamp(): continue
                    if self.cards and index>2 and value>min(stamp(c['first_departure_at']) for c in self.cards.values()):
                        continue
                    from contextvars import copy_context
                    ctx=copy_context()
                    future=executor.submit(ctx.run,self.attempt,datetime.fromtimestamp(value,timezone.utc))
                    active[future]=value
                ready,_=wait(list(active),timeout=.05,return_when=FIRST_COMPLETED) if active else ((),())
                for future in ready:
                    active.pop(future)
                    plan=future.result()
                    if plan: self.add_candidate(plan)
                    elif memory_pressure():
                        self.report({'stage':'resource_limit','message':'可用内存不足，已停止新搜索并保留确认结果'})
                        return
            self.report({'stage':'completed','search_progress':None,
                'discarded_equivalent_detours':self.discarded_equivalent_detours,'message':
                '已列出当前最早确认的方案；更早未确认时刻不代表不可飞' if self.cards else
                '已丢弃同网格或相同风场的绕行，暂未发现改善窗口的有效绕行方案' if self.discarded_equivalent_detours else
                '暂未找到已确认改善方案，未确认时段不代表全部不可飞'})
        finally:
            # Also stop siblings after a resource-limited early return or failure.
            self.stopped.set()
            executor.shutdown(wait=True,cancel_futures=True)


class RecommendationJobManager(RouteJobManager):
    def _finish(self,job,status,message):
        workflow=getattr(job,'workflow',None)
        if workflow is not None and status=='completed' and job.finish_requested.is_set():
            workflow.update(stage='completed',message=message)
            job.workflow_revision+=1
        if workflow is not None and status in {'cancelled','timed_out','failed'}:
            if workflow.get('stage')!='forecast_changed': workflow['stage']=status
            for attempt in workflow.get('attempts',[]):
                if attempt['status'] in {'queued','preparing','searching'}: attempt['status']=status
            if workflow.get('recommendations'):
                message=({'cancelled':'推荐已停止','timed_out':'推荐达到时限'}.get(status,message)
                         +'，保留已确认方案')
            workflow['message']=message
            job.workflow_revision+=1
        return super()._finish(job,status,message)

    def submit_workflow(self,request,dataset,preparation_seconds=0.,revision_check=None):
        def compute(queued,job):
            engine_type = Workflow
            if queued.mode == 'departure_range':
                from .departure_range import DepartureRangeWorkflow
                engine_type = DepartureRangeWorkflow
            engine=engine_type(queued,dataset,lambda value:self._report(job,value),lambda route:self._publish(job,route))
            return engine.run()
        def start(queued,publish):
            # RouteJobManager assigns the job before its worker enters this closure.
            with self.lock:
                job=next(j for j in self.jobs.values() if j.request_token==token)
                job.revision_check=revision_check
            return compute(queued,job) or {'points':[],'planning_message':job.message}
        token=object()
        if request.mode == 'departure_range':
            # A route drawing finishing must never set the parent's deadline.
            request=request.model_copy(update={'optimize_during_display':False,'stop_on_first_feasible':False})
        with self.lock:
            # Submit under the same RLock so _run cannot start before attributes exist.
            state=self.submit(start,request,preparation_seconds=preparation_seconds)
            job=self.jobs[state['job_id']]
            job.request_token=token;job.workflow={};job.workflow_revision=0
        return self.snapshot(job.job_id)

    def _report(self,job,value):
        checkpoint()
        value=dict(value)
        with self.lock:
            if job.status in TERMINAL: return
            clears_progress='search_progress' in value and value['search_progress'] is None
            progress=value.pop('search_progress',None)
            if clears_progress:
                job.search_progress=None
                job.progress_revision+=1
            job.workflow.update(deepcopy(value))
            job.workflow_revision+=1
            if 'message' in value: job.message=value['message']
        if progress: self._progress(job,progress)

    def _publish(self,job,result,*,final=False):
        if result and result.get('mode') == 'departure_range' and result.get('points'):
            checkpoint()
            with self.lock:
                self._expire(job)
                if job.status in TERMINAL: return
                previous=(job.result or {}).get('selection_revision',-1)
                if result['selection_revision'] > previous:
                    job.result=deepcopy(result)
                    job.score=(0,-result['selection_revision'])
                    job.revision+=1
                    if job.first_feasible_elapsed_seconds is None:
                        job.first_feasible_elapsed_seconds=monotonic()-job.created
                job.message=result['planning_message']
            return
        if result is not None and result.get('points'):
            return super()._publish(job,result,final=final)

    def snapshot(self,job_id,since_revision=None,since_progress_revision=None,since_workflow_revision=None):
        with self.lock:
            result=super().snapshot(job_id,since_revision,since_progress_revision)
            job=self.jobs[job_id]
            workflow=deepcopy(getattr(job,'workflow',{}))
            is_range=workflow.get('mode')=='departure_range'
            now=_current_utc().timestamp()
            if is_range and result.get('result') and stamp(result['result']['departure_at']) < now:
                result.update(result=None,has_result=False,feasible=False,result_expired=True)
                workflow['recommended_departure_expired']=True
            cards=[]
            baseline=workflow.get('baseline_windows')
            if baseline:
                baseline=trim_windows(baseline,now)
                workflow['baseline_windows']=baseline
            for card in workflow.get('recommendations',[]):
                card['windows']=trim_windows(card['windows'],now)
                rows=card['windows']['departure_windows']
                seeds=[value for value in card.get('verified_departures',[card['verified_departure_at']]) if stamp(value)>=now]
                eligible=[*seeds,*[r['start'] for r in rows]]
                if not eligible: continue
                if is_range:
                    # A low-wind optimum need not be the earliest safe departure.
                    if stamp(card['recommended_departure_at']) < now: continue
                else:
                    card['first_departure_at']=min(eligible,key=stamp)
                if baseline and card.get('metrics'):
                    card['metrics'].update(gains(baseline,card['windows']))
                    if not card['metrics']['improves']: continue
                cards.append(card)
            if is_range:
                from .departure_range import range_score
                workflow['recommendations']=sorted(cards,key=lambda card:range_score(card['plan']))[:3]
            else:
                workflow['recommendations']=sorted(cards,key=rank)[:3]
            signature=json.dumps(workflow,sort_keys=True,default=str)
            if signature!=getattr(job,'workflow_signature',None):
                job.workflow_signature=signature
                job.workflow_revision=getattr(job,'workflow_revision',0)+1
            result.update(workflow_revision=job.workflow_revision,
                          workflow=workflow if since_workflow_revision!=job.workflow_revision else None)
            return result

    def _expire(self,job):
        check=getattr(job,'revision_check',None)
        if check is not None and not check() and getattr(job,'workflow',{}).get('stage')!='forecast_changed':
            job.workflow={'stage':'forecast_changed','recommendations':[]}
            job.workflow_revision+=1
            if job.status not in TERMINAL:
                job.cancel.set()
                if job.future: job.future.cancel()
                self._finish(job,'cancelled','预报版本已更新，请重新计算推荐')
            else: job.message='预报版本已更新，请重新计算推荐'
        return super()._expire(job)
