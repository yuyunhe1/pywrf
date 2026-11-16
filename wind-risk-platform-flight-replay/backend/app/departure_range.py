"""Owned, bounded joint departure-time / ordered-waypoint recommendations."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextvars import copy_context
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from queue import Queue, Empty
from threading import Event, RLock
from time import monotonic

from .direction_pool import direction_lease
from .flight_replay import build_profile
from .gfs_provider import _current_utc
from .local_weather import LocalWeather, preserves_waypoints
from .models import FlightProfileRequest
from .planning_control import (checkpoint, current_control, planning_scope,
    PlanningCancelled, PlanningTimedOut)
from .planning_limits import memory_pressure, seconds_setting
from .range_evaluation import RouteRangeEvaluation, PEAK_MODEL, SELECTION_MODEL, PEAK_TOLERANCE
from .route_context import geometry_route
from .route_options import build_strategy_cost_config
from .route_service import haversine_km
from .temporal_plan_service import compute_temporal_plan
from .wrf_timeline import iso


def epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def geometry_key(points):
    return hashlib.sha256(json.dumps(points, separators=(',', ':')).encode()).hexdigest()


def range_score(plan):
    mean = plan['analysis'].get('mean_wind_speed')
    return (plan['distance_km'], plan['max_contact_wind_speed_mps'],
            mean if mean is not None and math.isfinite(mean) else math.inf,
            epoch(plan['departure_at']), geometry_key(plan['points']))


def spread_order(values):
    """Cover both ends, then bisect the largest unchecked time gaps."""
    values = sorted(set(values))
    if not values:
        return []
    result = [values[0]]
    if len(values) > 1:
        result.append(values[-1])
    queue = deque([(1, len(values)-2)])
    while queue:
        a, b = queue.popleft()
        if a > b:
            continue
        mid = (a+b)//2
        result.append(values[mid])
        queue.extend([(a, mid-1), (mid+1, b)])
    return result


class DepartureRangeWorkflow:
    def __init__(self, request, dataset, report, publish, now=None):
        self.request, self.dataset, self.report, self.publish = request, dataset, report, publish
        self.now = now or _current_utc
        self.control = current_control()
        self.lock, self.stopped = RLock(), Event()
        self.start, self.end = request.departure_start_at.timestamp(), request.departure_end_at.timestamp()
        self.version = request.baseline_version or geometry_key([request.required_waypoints, self.start, self.end, dataset.dataset_id])
        self.candidates, self.materials, self.attempts = {}, {}, []
        self.pending_geometry, self.queued_geometry = Queue(), set()
        self.improvements, self.seen_tasks = deque(), set()
        self.peak_reference, self.revision, self.signature = None, 0, None
        self.best = None
        self.attempt_seconds = seconds_setting('ROUTE_PLAN_DEPARTURE_ATTEMPT_SECONDS', 60)
        self.report(dict(mode='departure_range', stage='range_search', recommendations=[], attempts=[],
            baseline_version=self.version, dataset_id=dataset.dataset_id, selection_model=SELECTION_MODEL,
            peak_tolerance_mps=PEAK_TOLERANCE, departure_start_at=iso(self.start), departure_end_at=iso(self.end),
            message='正在限定时间段内共同比较低风路线与起飞时刻'))

    def update_attempt(self, record, **changes):
        with self.lock:
            record.update(changes)
            attempted = {epoch(row['departure_at']) for row in self.attempts}
            self.report({'attempts': deepcopy(self.attempts), 'compared_departure_minutes': len(attempted),
                'unsearched_departure_minutes': max(0, getattr(self, 'total_minutes', len(attempted))-len(attempted))})

    def register(self, candidate):
        """Only independent full-flight certificates enter the comparison pool."""
        checkpoint()
        peak = candidate.get('max_contact_wind_speed_mps')
        departure = epoch(candidate['departure_at'])
        if (candidate.get('analysis', {}).get('navigation_allowed') is not True or peak is None or
                not math.isfinite(peak) or peak >= self.request.thresholds.danger or
                not max(self.start, self.now().timestamp()) <= departure <= self.end or
                not preserves_waypoints(candidate['points'], self.request.required_waypoints)):
            return
        candidate = deepcopy(candidate)
        candidate['analysis'].update(max_wind_speed=peak, max_contact_wind_speed_mps=peak, peak_wind_model=PEAK_MODEL)
        candidate['flight_profile']['analysis'].update(candidate['analysis'])
        key = geometry_key(candidate['points'])
        with self.lock:
            self.candidates[key, departure] = candidate
            if key not in self.queued_geometry:
                self.queued_geometry.add(key)
                self.pending_geometry.put((key, candidate))
            self.refresh()

    def refresh(self, final=False):
        with self.lock:
            now = self.now().timestamp()
            values = [v for v in self.candidates.values() if epoch(v['departure_at']) >= now]
            if not values:
                self.best = None
                self.report({'recommendations': [], 'recommended_departure_expired': bool(self.candidates)})
                return
            reference = min(v['max_contact_wind_speed_mps'] for v in values)
            changed_reference = self.peak_reference != reference
            self.peak_reference = reference
            cap = reference + PEAK_TOLERANCE
            eligible = sorted((v for v in values if v['max_contact_wind_speed_mps'] <= cap), key=range_score)
            best = eligible[0]
            signature = (geometry_key(best['points']), best['departure_at'], reference, final,
                         tuple(sorted(self.materials)))
            if signature == self.signature:
                return
            self.signature = signature
            self.revision += 1
            cards, shown = [], set()
            for value in eligible:
                key = geometry_key(value['points'])
                if key in shown:
                    continue
                shown.add(key)
                material = self.materials.get(key)
                windows = material.windows(cap, now) if material else {
                    'departure_windows': [], 'coverage_incomplete': True, 'low_wind_certified': False}
                cards.append(dict(id=key, plan={k: deepcopy(v) for k, v in value.items()
                    if k not in {'flight_profile', 'planning_diagnostics'}},
                    first_departure_at=value['departure_at'], recommended_departure_at=value['departure_at'],
                    verified_departure_at=value['departure_at'], verified_departures=[value['departure_at']],
                    certification='windows_confirmed' if any(epoch(row['end'])>epoch(row['start'])
                        for row in windows['departure_windows']) else 'single_departure',
                    windows=windows, max_contact_wind_speed_mps=value['max_contact_wind_speed_mps'],
                    selection_revision=self.revision))
                if len(cards) == 3:
                    break
            self.best = {**deepcopy(best), 'mode': 'departure_range', 'selection_model': SELECTION_MODEL,
                'objective_model': SELECTION_MODEL, 'objective_cost': best['distance_km'],
                'peak_tolerance_mps': PEAK_TOLERANCE, 'min_verified_peak_mps': reference,
                'peak_wind_limit_mps': cap, 'selection_revision': self.revision,
                'recommended_departure_at': best['departure_at'], 'range_comparison_complete': final,
                'departure_start_at': iso(self.start), 'departure_end_at': iso(self.end),
                'departure_windows': cards[0]['windows'] if cards[0]['windows']['low_wind_certified'] else None,
                'planning_message': '限定时间段比较结束，保留当前已确认的低风短航线' if final else '当前推荐已验收，继续比较限定时间段内的方案'}
            self.publish(self.best)
            self.report(dict(recommendations=cards, selection_revision=self.revision,
                recommended_departure_expired=False,
                recommended_departure_at=best['departure_at'], min_verified_peak_mps=reference,
                peak_wind_limit_mps=cap, range_comparison_complete=final))
            if changed_reference:
                self.improvements.extend([('tighten', epoch(best['departure_at'])), ('shorten', epoch(best['departure_at']))])

    def analyse_geometry(self, key, candidate):
        try:
            with planning_scope(min(self.control.deadline, monotonic()+600), self.control.cancel):
                material = RouteRangeEvaluation(candidate['points'], self.dataset, self.request, self.start, self.end)
                with self.lock:
                    self.materials[key] = material
                choice = material.best_minute(self.now().timestamp())
                if choice:
                    _, stamp = choice
                    departure = datetime.fromtimestamp(stamp, timezone.utc)
                    req = self.request.model_copy(update={'departure_at': departure, 'peak_wind_limit_mps': None,
                        'minimize_distance_under_peak': False, 'peak_wind_limit_exclusive': False})
                    weather = LocalWeather(self.dataset, req, build_strategy_cost_config('wind_avoidance'), loading_mode='local')
                    if math.isfinite(weather.route_cost(candidate['points'])):
                        route = geometry_route(candidate['points'], req.level, source=self.dataset.source)
                        profile = build_profile(route, FlightProfileRequest(departure_at=departure,
                            average_ground_speed_mps=req.average_ground_speed_mps, thresholds=req.thresholds,
                            wind_shear=req.wind_shear, dataset_id=self.dataset.dataset_id), self.dataset)
                        self.register({**candidate, 'departure_at': profile['departure_at'], 'arrival_at': profile['arrival_at'],
                            'flight_profile': profile, 'analysis': profile['analysis'], 'segments': profile['segments'],
                            'route_context': {**route, 'dataset_id': self.dataset.dataset_id, 'departure_at': profile['departure_at']},
                            'metadata': {**candidate.get('metadata', {}), 'valid_time': iso(stamp)},
                            'max_contact_wind_speed_mps': weather.contact_peak(candidate['points'])})
                self.refresh()
        except PlanningTimedOut:
            if monotonic() >= self.control.deadline:
                raise
            self.report({'message': '窗口计算达到子预算，保留已验收起飞时刻；其余窗口未确认'})

    def attempt(self, phase, stamp, cap):
        parent, started, best, quality_deadline = self.control, None, None, None
        record = dict(departure_at=iso(stamp), phase=phase, peak_wind_limit_mps=cap, status='queued')
        with self.lock:
            self.attempts.append(record)
        self.update_attempt(record)
        queued = monotonic()
        class Stop:
            def cancelled_by_owner(inner):
                return parent.cancel.is_set() or self.stopped.is_set()
            def is_set(inner):
                return inner.cancelled_by_owner() or (started is not None and
                    monotonic() >= max(started+self.attempt_seconds, quality_deadline or 0))
        def observe(value):
            nonlocal quality_deadline
            if value.get('route_quality', {}).get('status') == 'searching_alternative':
                quality_deadline = value['route_quality']['retry_until_monotonic']
            self.update_attempt(record, status='searching', statistics=value.get('statistics', {}))
        def accept(value):
            nonlocal best
            best = value
            self.register(value)
            self.update_attempt(record, status='found', first_feasible_seconds=record.get('first_feasible_seconds', monotonic()-started))
        try:
            with planning_scope(parent.deadline, Stop(), progress=observe):
                with direction_lease():
                    started = monotonic()
                    self.update_attempt(record, status='preparing', queue_wait_seconds=started-queued)
                    with self.lock:
                        seeds = list({geometry_key(points): points for points in [
                            *self.request.seed_routes, *(v['points'] for v in self.candidates.values())]}.values())[:4]
                    req = self.request.model_copy(update={'departure_at': datetime.fromtimestamp(stamp, timezone.utc),
                        'planning_strategy': 'wind_avoidance', 'minimize_distance_under_peak': phase != 'seed',
                        'peak_wind_limit_mps': cap, 'peak_wind_limit_exclusive': phase == 'tighten',
                        'stop_on_first_feasible': phase != 'shorten', 'optimize_during_display': False,
                        'prefer_simple_route': True,
                        'max_route_distance_km': None, 'seed_routes': seeds})
                    result = compute_temporal_plan(req, self.dataset, [-180, -90, 180, 90], 0, accept)
                    if result.get('analysis', {}).get('navigation_allowed'):
                        best = result
                    self.update_attempt(record, status='found' if best else 'unconfirmed',
                        outcome=result.get('route_status'), message=result.get('planning_message'),
                        elapsed_seconds=monotonic()-started, diagnostics=result.get('planning_diagnostics'))
        except (PlanningCancelled, PlanningTimedOut):
            if parent.cancel.is_set() or self.stopped.is_set() or monotonic() >= parent.deadline:
                raise
            self.update_attempt(record, status='found' if best else 'unconfirmed',
                outcome='attempt_budget', elapsed_seconds=monotonic()-started if started else 0)
        return best

    def run(self):
        if not self.dataset.frames:
            self.report({'stage': 'data_insufficient', 'message': self.dataset.error or '缺少同批次风场'})
            return
        now = self.now().timestamp()
        if self.end < now:
            self.report({'stage': 'range_expired', 'message': '限定起飞时间段已经过期，请选择未来时间段'})
            return
        self.total_minutes = max(0, int((self.end-math.ceil(max(self.start, now)/60)*60)//60)+1)
        begin = math.ceil(max(self.start, now, self.dataset.coverage_start)/60)*60
        chord = sum(haversine_km(a, b) for a, b in zip(self.request.required_waypoints, self.request.required_waypoints[1:]))
        finish = math.floor(min(self.end, self.dataset.coverage_end-chord*1000/self.request.average_ground_speed_mps)/60)*60
        if begin > finish:
            self.report({'stage': 'data_insufficient', 'message': '限定起飞时间段内没有未过期且覆盖最短完整航程的预报'})
            return
        self.report(dict(searchable_departure_start_at=iso(begin), searchable_departure_end_at=iso(finish),
            unsearched_departure_minutes=self.total_minutes, compared_departure_minutes=0))
        coarse_values = [begin, finish, *range(math.ceil(begin/600)*600, int(finish)+1, 600)]
        coarse = deque(spread_order(coarse_values))
        fine = deque(spread_order(t for t in range(int(begin), int(finish)+1, 60) if t not in set(coarse_values)))
        active, turns = {}, 0
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='departure-range')
        def next_task():
            nonlocal turns
            while coarse or self.improvements or fine:
                turns += 1
                if coarse and (turns % 3 or not self.improvements):
                    phase, at = 'seed', coarse.popleft()
                elif self.improvements:
                    phase, at = self.improvements.popleft()
                else:
                    at = fine.popleft()
                    phase = 'tighten' if self.peak_reference is not None else 'seed'
                    self.improvements.append(('shorten', at))
                cap = (self.peak_reference if phase == 'tighten' else
                       self.peak_reference+PEAK_TOLERANCE if phase == 'shorten' and self.peak_reference is not None else None)
                if phase != 'seed' and cap is None:
                    phase = 'seed'
                key = at, phase, cap
                if key not in self.seen_tasks and at >= self.now().timestamp():
                    self.seen_tasks.add(key)
                    return phase, at, cap
            return None
        try:
            while True:
                checkpoint()
                try:
                    key, candidate = self.pending_geometry.get_nowait()
                except Empty:
                    pass
                else:
                    self.analyse_geometry(key, candidate)
                pressure = memory_pressure()
                if pressure and not active:
                    self.report({'stage': 'resource_limit', 'message': '内存余量不足，保留已确认方案；未搜索部分仍未确认'})
                    break
                maximum = 1 if pressure else 2
                while len(active) < maximum:
                    task = next_task()
                    if task is None:
                        break
                    context = copy_context()
                    future = executor.submit(context.run, self.attempt, *task)
                    active[future] = task
                if not active and not coarse and not fine and not self.improvements and self.pending_geometry.empty():
                    break
                ready, _ = wait(list(active), timeout=.05, return_when=FIRST_COMPLETED) if active else ((), ())
                for future in ready:
                    phase, at, _ = active.pop(future)
                    future.result()
                    if phase == 'seed' and self.peak_reference is not None:
                        self.improvements.extend([('tighten', at), ('shorten', at)])
                if pressure and not active:
                    self.report({'stage': 'resource_limit', 'message': '内存余量不足，保留已确认方案；未搜索部分仍未确认'})
                    break
            self.refresh(final=True)
            attempted = {epoch(row['departure_at']) for row in self.attempts}
            all_minutes = set(range(int(begin), int(finish)+1, 60))
            self.report(dict(stage='resource_limit' if pressure else 'completed', search_progress=None,
                compared_departure_minutes=len(attempted), unsearched_departure_minutes=len(all_minutes-attempted),
                unconfirmed_departures=[r['departure_at'] for r in self.attempts if r['status']=='unconfirmed'],
                message='限定时间段比较结束，已保留当前最佳方案；未确认时刻不代表不可飞' if self.best else
                        '限定时间段内暂未找到已确认方案，搜索中断不代表整个时间段不可飞'))
            return self.best
        finally:
            self.stopped.set()
            executor.shutdown(wait=True, cancel_futures=True)
