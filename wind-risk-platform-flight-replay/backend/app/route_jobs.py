"""Bounded background planning jobs retaining the best published route."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import math
from threading import Event, RLock, Thread
from time import monotonic
from uuid import uuid4

from .planning_control import PlanningCancelled, PlanningTimedOut, PlanningFinished, checkpoint, planning_scope
from .planning_limits import DEFAULT_TIMEOUT_SECONDS

TERMINAL = {"completed", "timed_out", "cancelled", "failed"}


class PlanningQueueFull(Exception):
    pass


def candidate_score(result: dict) -> tuple:
    """Feasibility first; then the user's selected distance/wind objective."""
    analysis = result.get("analysis", {})
    feasible = bool(result.get("points")) and analysis.get("navigation_allowed") is True
    reference = bool(result.get("reference_route") or result.get("wind_shear_fallback") or result.get("endpoint_hard_block"))

    def number(key, default=math.inf):
        value = analysis.get(key, result.get(key, default))
        return float(value) if value is not None and math.isfinite(float(value)) else default

    risk = (number("danger_ratio"), number("max_wind_speed"), number("mean_wind_speed"))
    distance = number("distance_km", number("total_distance_km"))
    integrated = result.get("objective_cost")
    objective = ((float(integrated), *risk, distance) if integrated is not None and math.isfinite(float(integrated))
                 else (distance, *risk) if result.get("planning_strategy") == "distance_priority" else (*risk, distance))
    shape = result.get("shape_quality")
    if shape and result.get("planner_type") in {"astar", "a_star", "lpa_star", "wa_lpa_star"}:
        from .route_refinement import shape_key
        return (0 if feasible and not reference else 1, *objective, *shape_key(shape))
    return (0 if feasible and not reference else 1, *objective)


@dataclass
class RouteJob:
    job_id: str
    deadline: float
    created: float = field(default_factory=monotonic)
    cancel: Event = field(default_factory=Event)
    status: str = "queued"
    revision: int = 0
    result: dict | None = None
    score: tuple | None = None
    message: str = "等待规划"
    finished: float | None = None
    future: object = None
    on_finish: object = None
    search_progress: dict | None = None
    progress_revision: int = 0
    progress_updated: float = -math.inf
    client_instance_id: str | None = None
    client_request_sequence: int | None = None
    release_results: bool = False
    owner_last_seen: float = 0.
    owner_generation: int = 0
    started: float | None = None
    first_feasible_elapsed_seconds: float | None = None
    preparation_seconds: float = 0.
    finish_requested: Event = field(default_factory=Event)
    finish_reason: str | None = None
    comparison_deadline: float | None = None
    display_segment_seconds: float | None = None
    route_quality: dict | None = None
    quality_retry_deadline: float | None = None
    finish_after_quality: bool = False


class RouteJobManager:
    def __init__(self, workers=2, capacity=8, timeout_seconds=DEFAULT_TIMEOUT_SECONDS, retention_seconds=1800,
                 owner_timeout_seconds=60, monitor_interval=1):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="route-plan")
        self.capacity = capacity
        self.timeout_seconds = timeout_seconds
        self.retention_seconds = retention_seconds
        self.jobs = {}
        self.lock = RLock()
        self.owner_timeout_seconds, self.monitor_interval = owner_timeout_seconds, monitor_interval
        self.monitor_stop, self.monitor = Event(), None

    def _finish(self, job, status, message):
        job.status = status
        job.message = message
        job.finished = monotonic()
        if job.result and job.search_progress and job.search_progress.get("statistics"):
            job.result.setdefault("planning_diagnostics", {})["final_observed_search"] = deepcopy(job.search_progress["statistics"])
            job.revision += 1
        job.search_progress = None
        job.progress_revision += 1
        if status in {"completed", "timed_out", "failed"} and job.result and job.on_finish:
            callback, job.on_finish = job.on_finish, None
            try:
                callback(job.result)
            except Exception as exc:
                job.result["window_error"] = str(exc)
            job.revision += 1

    def _expire(self, job):
        if job.status not in TERMINAL and job.client_instance_id and monotonic() - job.owner_last_seen >= self.owner_timeout_seconds:
            job.cancel.set()
            if job.client_request_sequence is not None: job.release_results=True
            if job.future is not None:
                job.future.cancel()
            self._finish(job, "cancelled", "页面连接已断开，规划已自动取消")
        if job.status not in TERMINAL and monotonic() >= job.deadline:
            job.cancel.set()
            self._finish(job, "timed_out", "规划达到时限，保留已找到的最佳航线" if job.result else "规划达到时限，尚未找到可行航线")
        if job.status not in TERMINAL and job.comparison_deadline is not None and monotonic() >= job.comparison_deadline:
            job.finish_reason = job.finish_reason or 'display_budget_elapsed'
            job.finish_requested.set()
        if job.status not in TERMINAL and job.finish_after_quality and (
            job.quality_retry_deadline is None or monotonic() >= job.quality_retry_deadline):
            job.finish_requested.set()

    def submit(self, compute, request, on_finish=None, preparation_seconds=0.):
        with self.lock:
            now = monotonic()
            for key, job in list(self.jobs.items()):
                self._expire(job)
                if job.finished and now - job.finished > self.retention_seconds and job.future.done():
                    del self.jobs[key]
            if sum(not job.future.done() for job in self.jobs.values()) >= self.capacity:
                raise PlanningQueueFull()
            job = RouteJob(str(uuid4()), now + self.timeout_seconds)
            job.client_instance_id = getattr(request, "client_instance_id", None)
            job.client_request_sequence = getattr(request,'client_request_sequence',None)
            job.owner_last_seen = now
            job.preparation_seconds = preparation_seconds
            job.on_finish = on_finish
            if getattr(request, 'optimize_during_display', False) and not getattr(request, 'stop_on_first_feasible', False):
                job.display_segment_seconds = request.display_segment_seconds
            self.jobs[job.job_id] = job
            job.future = self.executor.submit(self._run, job, compute, request)
            if (job.client_instance_id or job.display_segment_seconds is not None) and self.monitor is None:
                self.monitor = Thread(target=self._monitor, name="route-owner-monitor", daemon=True)
                self.monitor.start()
            return self.snapshot(job.job_id)

    def _monitor(self):
        while not self.monitor_stop.wait(self.monitor_interval):
            with self.lock:
                now=monotonic()
                for key,job in list(self.jobs.items()):
                    self._expire(job)
                    if job.release_results and job.future.done(): self._discard_results(job)
                    if job.finished and now-job.finished>self.retention_seconds and job.future.done(): del self.jobs[key]

    def _discard_results(self,job):
        job.result=None;job.score=None;job.search_progress=None;job.on_finish=None
        job.revision+=1;job.progress_revision+=1
        job.revision_check=None
        job.release_results=False
        job.route_quality=None;job.quality_retry_deadline=None;job.finish_after_quality=False
        if getattr(job,'workflow',None):
            job.workflow={};job.workflow_revision+=1
        if hasattr(job,'workflow_signature'): job.workflow_signature=None

    def cancel_client(self,client_id,through,forget=False):
        with self.lock:
            jobs=[job for job in self.jobs.values() if job.client_instance_id==client_id
                and (job.client_request_sequence or 0)<=through]
            for job in jobs:
                self.cancel_job(job.job_id)
                if forget:
                    job.release_results=True
                    if job.future.done(): self._discard_results(job)
            return all(job.future.done() for job in jobs)

    def attach_owner(self, job_id, client_instance_id):
        with self.lock:
            job = self.jobs[job_id]
            self._expire(job)
            if not job.client_instance_id or job.client_instance_id != client_instance_id or job.status in TERMINAL:
                return None
            job.owner_generation += 1
            job.owner_last_seen = monotonic()
            return job.owner_generation

    def touch_owner(self, job_id, client_instance_id, generation):
        with self.lock:
            job = self.jobs[job_id]
            self._expire(job)
            if job.status in TERMINAL or job.client_instance_id != client_instance_id or job.owner_generation != generation:
                return False
            job.owner_last_seen = monotonic()
            return True

    def owner_alive(self, job_id, generation):
        with self.lock:
            job = self.jobs[job_id]
            return job.status not in TERMINAL and job.owner_generation == generation

    def _publish(self, job, result, *, final=False):
        checkpoint()
        score = candidate_score(result)
        # References are returned only as final diagnostics, never as previews.
        if not final and score[0] != 0:
            return
        with self.lock:
            self._expire(job)
            if job.finish_requested.is_set():
                raise PlanningFinished()
            if job.status in TERMINAL:
                return
            if job.score is None or score < job.score:
                if score[0] == 0 and job.first_feasible_elapsed_seconds is None:
                    job.first_feasible_elapsed_seconds = monotonic() - job.created
                if score[0] == 0 and job.display_segment_seconds is not None and (not job.result or job.result['points'] != result['points']):
                    departure = datetime.fromisoformat(result['departure_at'].replace('Z', '+00:00')).timestamp()
                    duration = result['flight_profile']['flight_duration_seconds']
                    segments = max(1, math.ceil((departure + duration) / 600 - 1e-8) - math.floor(departure / 600))
                    # Changed geometry may need to redraw earlier legs. The
                    # parent budget remains the absolute cap on extensions.
                    job.comparison_deadline = min(job.deadline, monotonic() + segments * job.display_segment_seconds + 15)
                job.result = deepcopy(result)
                job.score = score
                job.revision += 1
            elif final and score == job.score and job.result:
                updated = False
                for key in ("planning_diagnostics", "planning_message"):
                    if key in result:
                        job.result[key] = deepcopy(result[key])
                        updated = True
                if updated:
                    job.revision += 1
            job.message = (("首条可行航线已验收，正在结束搜索" if result.get('stop_on_first_feasible') else "已找到可行航线，继续优化")
                           if job.score and job.score[0] == 0 else "正在规划")

    def _run(self, job, compute, request):
        try:
            with self.lock:
                if job.status in TERMINAL:
                    return
                job.status = "running"
                job.started = monotonic()
                job.message = "正在寻找首条可行航线"
            with planning_scope(job.deadline, job.cancel, progress=lambda value: self._progress(job, value), finish=job.finish_requested):
                result = compute(request, publish=lambda candidate: self._publish(job, candidate))
                self._publish(job, result, final=True)
            with self.lock:
                if job.status not in TERMINAL:
                    self._finish(job, "completed", "规划完成" if job.score and job.score[0] == 0 else (result.get("planning_message") or "规划完成，请查看通航决策"))
        except PlanningFinished:
            with self.lock:
                if job.status not in TERMINAL and job.score and job.score[0] == 0:
                    job.result['planning_termination'] = job.finish_reason or 'display_finished'
                    job.result['planning_message'] = '分段展示结束，已固定当前已验收的最佳航线'
                    self._finish(job, 'completed', job.result['planning_message'])
        except PlanningCancelled:
            with self.lock:
                if job.status not in TERMINAL:
                    self._finish(job, "cancelled", "规划已取消")
        except PlanningTimedOut:
            with self.lock:
                self._expire(job)
        except Exception as exc:
            with self.lock:
                if job.status not in TERMINAL:
                    reason = str(getattr(exc, "detail", None) or exc)
                    self._finish(job, "failed", f"继续优化失败，保留已找到的航线：{reason}" if job.result else reason)
        finally:
            if job.release_results:
                with self.lock: self._discard_results(job)

    def _progress(self, job, value):
        checkpoint()
        with self.lock:
            self._expire(job)
            if job.status in TERMINAL:
                return
            quality=value.get('route_quality')
            if quality:
                job.route_quality=deepcopy({k:v for k,v in quality.items() if k!='retry_until_monotonic'})
                job.quality_retry_deadline=quality.get('retry_until_monotonic') if quality['status']=='searching_alternative' else None
                job.message=quality['message']
                if job.quality_retry_deadline is not None and job.comparison_deadline is not None:
                    job.comparison_deadline=min(job.deadline,max(job.comparison_deadline,job.quality_retry_deadline))
                if job.finish_after_quality and job.quality_retry_deadline is None: job.finish_requested.set()
            if not quality and monotonic() - job.progress_updated < 1.: return
            job.search_progress = deepcopy(value)
            job.progress_revision += 1
            job.progress_updated = monotonic()

    def snapshot(self, job_id, since_revision=None, since_progress_revision=None):
        with self.lock:
            job = self.jobs[job_id]
            self._expire(job)
            return {
                "job_id": job.job_id,
                "status": job.status,
                "revision": job.revision,
                "result": deepcopy(job.result) if since_revision != job.revision else None,
                "has_result": job.result is not None,
                "feasible": bool(job.score is not None and job.score[0] == 0),
                "message": job.message,
                "elapsed_seconds": round((job.finished or monotonic()) - job.created, 2),
                "timeout_seconds": self.timeout_seconds,
                "progress_revision": job.progress_revision,
                "owner_timeout_seconds": self.owner_timeout_seconds if job.client_instance_id else None,
                "compute_finished": bool(job.future is not None and job.future.done()),
                "finish_requested": job.finish_requested.is_set(),
                "route_quality": ({**deepcopy(job.route_quality), 'remaining_seconds':max(0.,(job.quality_retry_deadline or monotonic())-monotonic())}
                                  if job.route_quality else None),
                "queue_wait_seconds": round((job.started or job.finished or monotonic()) - job.created, 6),
                "preparation_seconds": round(job.preparation_seconds, 6),
                "first_feasible_elapsed_seconds": job.first_feasible_elapsed_seconds,
                "search_progress": deepcopy(job.search_progress) if since_progress_revision != job.progress_revision else None,
            }

    def cancel_job(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            self._expire(job)
            if job.status not in TERMINAL:
                job.cancel.set()
                job.future.cancel()
                self._finish(job, "cancelled", "规划已取消")
            return self.snapshot(job_id)

    def finish_job(self, job_id):
        """Freeze a verified incumbent and cooperatively release the search."""
        with self.lock:
            job = self.jobs[job_id]
            self._expire(job)
            if job.status not in TERMINAL:
                if not job.score or job.score[0] != 0:
                    raise ValueError('尚无完整验收路线，不能结束为规划成功')
                job.finish_reason = 'display_finished'
                if job.quality_retry_deadline is not None and monotonic()<job.quality_retry_deadline:
                    job.finish_after_quality=True
                    job.message='正在额外搜索更合适的非盘旋路线（最多10秒）'
                else:
                    job.finish_requested.set()
                    job.message = '分段展示结束，正在释放搜索资源'
            return self.snapshot(job_id)

    def shutdown(self):
        self.monitor_stop.set()
        with self.lock:
            for job in self.jobs.values():
                job.cancel.set()
                if job.status not in TERMINAL:
                    job.future.cancel()
                    self._finish(job, "cancelled", "规划已取消")
        self.executor.shutdown(wait=False, cancel_futures=True)
