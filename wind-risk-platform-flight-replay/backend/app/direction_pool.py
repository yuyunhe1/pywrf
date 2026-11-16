"""Two persistent spawn processes; bounded batches and cooperative/hard stop."""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
import itertools
import math
import multiprocessing as mp
from multiprocessing.connection import wait
import os
from threading import Lock, Condition
import time

from .planning_control import PlanningCancelled, checkpoint, current_control, planning_scope
from .planning_limits import DEFAULT_MAX_TEMPORAL_LABELS, integer_setting, memory_pressure, process_peak_memory_mb


def _direction_main(connection, cancel, role):
    from .forecast_transport import open_dataset
    from .local_weather import LocalWeather, _BLOCKS
    from .models import RoutePlanRequest
    from .route_options import build_strategy_cost_config
    from .segmented_routing import SegmentedPlanner, LocalKernel, Forward
    from .temporal_routing import TemporalSearchExhausted

    class Direction(SegmentedPlanner):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.ids, self.delta, self.candidates = {}, [], []
            self.identifier = itertools.count()

        def export(self, label, side):
            identifier = next(self.identifier)
            self.ids[id(label)] = identifier
            result = dict(id=identifier, node=label.node, parent=self.ids.get(id(label.parent)), distance=label.distance)
            if side == "forward":
                result.update(point=label.point, stamp=label.stamp, cost=label.cost, pending=label.pending)
            else:
                result.update(point=self.graph.point(label.node), window=label.window, estimate=label.estimate, serial=label.serial)
            self.delta.append(result)

        def _offer_f(self, label):
            before = self.stats["forward_labels"]
            super()._offer_f(label)
            if self.stats["forward_labels"] != before:
                self.export(label, "forward")

        def _offer_b(self, label):
            before = self.stats["backward_labels"]
            super()._offer_b(label)
            if self.stats["backward_labels"] != before:
                self.export(label, "backward")

        def connect(self, *args):
            pass  # The coordinator owns cross-frontier joining and validation.

        def _candidate(self, points, via):
            key = tuple(points)
            if key not in self.tried:
                self.tried.add(key)
                self.candidates.append(dict(points=points, search_phase=via))

    cached = OrderedDict()
    signature = None
    connection.send(dict(type="ready", pid=os.getpid(), role=role))
    while True:
        message = connection.recv()
        if message["type"] == "shutdown":
            return
        if message["type"] != "start":
            continue
        token, dataset_body = message["token"], message["dataset"]
        began = time.monotonic()
        cpu = time.process_time()
        planner = None
        try:
            os.environ["FORECAST_DERIVED_CACHE_DIR"] = dataset_body["cache_dir"]
            if signature != dataset_body["signature"]:
                _BLOCKS.clear()
                signature = dataset_body["signature"]
            if signature not in cached:
                cached[signature] = open_dataset(dataset_body)
                while len(cached) > 2:
                    cached.popitem(last=False)
            dataset = cached[signature]
            io_before = dataset.io_stats.snapshot()
            request = RoutePlanRequest(**message["request"])
            # Cover eager loading with cancellation too, before a search tree exists.
            with planning_scope(message["deadline"], cancel):
                weather = LocalWeather(dataset, request, build_strategy_cost_config(request.planning_strategy),
                                       loading_mode=message.get("weather_loading_mode", "full"))
            planner = Direction(weather, request.start, request.end, message["bbox"])
            planner.algorithm, planner.accept = message["algorithm"], None
            planner.kernel = LocalKernel(planner.algorithm)
            planner.calibration_scores = {}
            planner.next_arrival = planner.eta_hint
            next_enable = time.monotonic() + .5
            with planning_scope(message["deadline"], cancel):
                if role == "forward":
                    planner._offer_f(Forward(planner.graph.start, planner.start, planner.departure, 0., 0.))
                else:
                    for _ in range(3):
                        planner.enable_arrivals()
                while True:
                    checkpoint()
                    while connection.poll():
                        command = connection.recv()
                        if command.get("token") == token and command["type"] == "bound":
                            planner.best_cost = command["value"]
                    batch_start, count = time.monotonic(), 0
                    alive = True
                    while count < 256 and time.monotonic() - batch_start < .05:
                        checkpoint()
                        if count % 64 == 0:
                            pressure = memory_pressure()
                            if pressure:
                                raise TemporalSearchExhausted("搜索达到内存资源上限；保留已验证结果", pressure)
                            limit = integer_setting("ROUTE_PLAN_MAX_TEMPORAL_LABELS", DEFAULT_MAX_TEMPORAL_LABELS)
                            if limit and planner.stats['forward_labels'] + planner.stats['backward_labels'] >= limit:
                                raise TemporalSearchExhausted("搜索达到时间标签资源上限；保留已验证结果",
                                    {"limit_type": "arrival_labels", "max_arrival_labels": limit})
                        if role == "backward" and (time.monotonic() >= next_enable or not planner.reverse_queue):
                            planner.enable_arrivals()
                            next_enable = time.monotonic() + .5
                        moved = planner.expand_forward() if role == "forward" else planner.expand_backward()
                        count += 1
                        if not moved:
                            if role == "forward" or planner.next_arrival > planner.finish:
                                alive = False
                                break
                    weather_stats = weather.diagnostics()
                    io_after = dataset.io_stats.snapshot()
                    weather_stats['source_io_delta'] = {key: value - io_before.get(key, 0)
                        for key, value in io_after.items() if type(value) in (float, int)}
                    connection.send(dict(type="batch", token=token, role=role, pid=os.getpid(),
                                         dataset_id=dataset_body['dataset_id'],
                                         records=planner.delta, candidates=planner.candidates, statistics=planner.stats.copy(),
                                         start=batch_start, end=time.monotonic(), count=count,
                                         weather=weather_stats, peak_memory_mb=process_peak_memory_mb()))
                    planner.delta, planner.candidates = [], []
                    if not alive:
                        break
        except (Exception, KeyboardInterrupt) as exc:
            try:
                connection.send(dict(type="error", token=token, role=role, dataset_id=dataset_body['dataset_id'],
                    error=type(exc).__name__, message=str(exc), diagnostics=getattr(exc, 'diagnostics', {})))
            except (BrokenPipeError, EOFError):
                return
        finally:
            final_statistics = planner.stats.copy() if planner else {}
            # Keep only bounded weather caches between jobs, never an idle
            # process's entire previous search tree.
            if planner is not None:
                planner.release_search_state()
                final_statistics['search_state_release_seconds']=planner.stats['search_state_release_seconds']
            planner = None
            weather = None
            dataset = None
            request = None
            try:
                connection.send(dict(type="done", token=token, role=role, pid=os.getpid(),
                                     dataset_id=dataset_body['dataset_id'],
                                     seconds=time.monotonic() - began, cpu_seconds=time.process_time() - cpu,
                                     statistics=final_statistics))
            except (BrokenPipeError, EOFError):
                return


class DirectionPool:
    def __init__(self):
        self.context = mp.get_context("spawn")
        self.cancel = self.context.Event()
        self.lock = Lock()
        self.workers = {}
        self.closed = False

    def _spawn(self, role):
        if role in self.workers:
            previous = self.workers[role]
            previous["connection"].close()
            previous["process"].join(timeout=.05)
        parent, child = self.context.Pipe()
        process = self.context.Process(target=_direction_main, args=(child, self.cancel, role), name=f"route-{role}", daemon=True)
        process.start()
        child.close()
        self.workers[role] = dict(process=process, connection=parent, ready=False)

    def _wait_ready(self):
        began = time.monotonic()
        for item in self.workers.values():
            while not item['ready']:
                checkpoint()
                if not item['process'].is_alive():
                    raise RuntimeError('双向工作进程启动失败')
                if item['connection'].poll(.02):
                    value = item['connection'].recv()
                    if value['type'] == 'ready':
                        item['ready'] = True
                if time.monotonic()-began > 10:
                    raise RuntimeError('双向工作进程预热超时')

    def warm(self):
        with self.lock:
            if self.closed:
                return
            for role in ("forward", "backward"):
                if role not in self.workers or not self.workers[role]["process"].is_alive():
                    self._spawn(role)
            self._wait_ready()

    def acquire(self):
        while not self.lock.acquire(timeout=.05):
            checkpoint()
        try:
            checkpoint()
            for role in ("forward", "backward"):
                if role not in self.workers or not self.workers[role]["process"].is_alive():
                    self._spawn(role)
            self._wait_ready()
            self.cancel.clear()
            return self.workers
        except BaseException:
            self.lock.release()
            raise

    def release(self, token, already_done):
        began = time.monotonic()
        self.cancel.set()
        waiting = set(self.workers) - set(already_done)
        while waiting and time.monotonic() - began < 1.4:
            channels = [self.workers[role]["connection"] for role in waiting]
            for channel in wait(channels, timeout=.02):
                try:
                    value = channel.recv()
                except (EOFError, OSError):
                    waiting = {role for role in waiting if self.workers[role]["connection"] != channel}
                    continue
                if value.get("token") == token and value["type"] == "done":
                    waiting.discard(value["role"])
        killed = []
        for role in list(waiting):
            item = self.workers.pop(role)
            if item["process"].is_alive():
                item["process"].terminate()
            item["process"].join(timeout=.15)
            item["connection"].close()
            killed.append(role)
        self.lock.release()
        return dict(worker_release_seconds=time.monotonic() - began, terminated_workers=killed)

    def shutdown(self):
        self.closed = True
        self.cancel.set()
        # Shutdown must also be safe while an active coordinator is unwinding.
        with self.lock:
            for item in self.workers.values():
                if item["process"].is_alive():
                    item["process"].terminate()
                item["process"].join(timeout=.2)
                item["connection"].close()
            self.workers.clear()


_POOL = DirectionPool()
_POOLS = [_POOL]
_LEASED = set()
_POOL_GUARD = Condition()
_CURRENT_POOL = ContextVar('route_direction_pair', default=None)


def direction_pool():
    global _POOL, _POOLS
    current = _CURRENT_POOL.get()
    if current is not None:
        return current
    with _POOL_GUARD:
        if _POOL.closed:
            _POOL = DirectionPool()
            _POOLS = [_POOL]
        return _POOL


@contextmanager
def direction_lease():
    """One globally bounded pair per attempt, shared by old and new APIs."""
    if _CURRENT_POOL.get() is not None:
        yield _CURRENT_POOL.get()
        return
    direction_pool()
    limit = min(2, max(1, integer_setting('ROUTE_PLAN_DEPARTURE_CONCURRENCY', 2)))
    with _POOL_GUARD:
        while True:
            checkpoint()
            # A low-memory host runs one attempt at a time, not two decoders.
            effective = 1 if memory_pressure() else limit
            pool = next((p for p in _POOLS[:effective] if p not in _LEASED and not p.closed), None)
            if pool is None and len(_POOLS) < effective:
                pool = DirectionPool()
                _POOLS.append(pool)
            if pool is not None:
                _LEASED.add(pool)
                break
            _POOL_GUARD.wait(.05)
    token = _CURRENT_POOL.set(pool)
    try:
        yield pool
    finally:
        _CURRENT_POOL.reset(token)
        with _POOL_GUARD:
            _LEASED.discard(pool)
            _POOL_GUARD.notify_all()


def warm_direction_pool():
    if integer_setting("ROUTE_PLAN_DIRECTION_PROCESSES", 2) == 2:
        direction_pool().warm()


def shutdown_direction_pool():
    with _POOL_GUARD:
        pools = list(_POOLS)
    for pool in pools:
        pool.shutdown()
