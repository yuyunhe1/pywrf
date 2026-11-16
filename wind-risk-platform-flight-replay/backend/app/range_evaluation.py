"""Exact fixed-geometry peak envelopes and bounded low-wind windows.

Each closed cell contributes its held weather intervals shifted by the
aircraft's enter/leave offsets. A sweep retains point contacts and open frame
ends; it never certifies an interval by checking only its two endpoints.
"""
from collections import defaultdict
from datetime import datetime, timezone
import heapq
import math

from .departure_windows import (Interval, _cell_visits, _intersection, _complement,
    _merge, prepare_evaluation, describe_evaluation)
from .planning_control import checkpoint
from .wind_time import held_vectors
from .wrf_timeline import iso

PEAK_MODEL = 'CLOSED_CELL_CONTACT_PEAK_V1'
SELECTION_MODEL = 'PEAK_WIND_BAND_DISTANCE_V1'
PEAK_TOLERANCE = 1.


def first_minute(interval, now):
    value = math.ceil(max(interval.start, now)/60)*60
    if not interval.contains(value):
        value += 60
    return value if interval.contains(value) else None


def peak_envelope(observations, domain):
    events = defaultdict(lambda: [[], [], [], []])
    events[domain.start]; events[domain.end]
    for identifier, (interval, value) in enumerate(observations):
        part = _intersection(interval, domain)
        if part is None:
            continue
        events[part.start][1 if part.left_closed else 3].append((identifier, value))
        events[part.end][2 if part.right_closed else 0].append((identifier, value))
    times = sorted(events)
    active, heap, result = {}, [], []
    def add(items):
        for key, value in items:
            active[key] = value
            heapq.heappush(heap, (-value, key))
    def remove(items):
        for key, _ in items:
            active.pop(key, None)
    def highest():
        while heap and heap[0][1] not in active:
            heapq.heappop(heap)
        return -heap[0][0] if heap else None
    for i, stamp in enumerate(times):
        if i % 256 == 0:
            checkpoint()
        before, closed_start, closed_end, after = events[stamp]
        remove(before); add(closed_start)
        value = highest()
        if value is not None:
            result.append((Interval(stamp, stamp), value))
        remove(closed_end); add(after)
        value = highest()
        if i+1 < len(times) and value is not None:
            result.append((Interval(stamp, times[i+1], False, False), value))
    return result


class RouteRangeEvaluation:
    def __init__(self, points, dataset, request, start, end):
        self.dataset, self.request = dataset, request
        self.start, self.end = start, end
        settings = request.wind_shear
        shear = (settings.horizontal.hard_delta_wind_vector_ms if settings.enabled and settings.horizontal.enabled
                 and settings.horizontal.hard_constraint_enabled else None)
        self.check = prepare_evaluation(points, list(dataset.frames), request.average_ground_speed_mps,
            request.thresholds.danger, shear, dataset.coverage_start, dataset.coverage_end,
            source=dataset.source, wind_time_model=dataset.time_model)
        self.domain = Interval(max(start, self.check.coverage_start or start),
            min(end, (self.check.coverage_end or -math.inf)-self.check.duration_s))
        self.atoms = []
        if self.check.data_error or not self.domain.valid():
            return
        safe = _complement(self.domain, [issue.departure for issue in self.check.issues])
        if not safe:
            return
        frames = list(dataset.frames)
        times = [t for t, _ in frames]
        observations, vectors = [], {}
        for visit in _cell_visits(points, frames[0][1], request.average_ground_speed_mps):
            checkpoint()
            key = visit.row, visit.col
            if key not in vectors:
                values = [(float(g.u[key]), float(g.v[key])) for _, g in frames]
                rows = []
                for i, (a, b) in enumerate(zip(times, times[1:])):
                    for begin, finish, uv in held_vectors(a, b, values[i], values[i+1], dataset.time_model):
                        if uv is not None:
                            rows.append((Interval(begin, finish, True, False), math.hypot(*uv)))
                if all(map(math.isfinite, values[-1])):
                    rows.append((Interval(times[-1], times[-1]), math.hypot(*values[-1])))
                vectors[key] = rows
            for interval, peak in vectors[key]:
                shifted = Interval(interval.start-visit.leave, interval.end-visit.enter,
                                   interval.left_closed, interval.right_closed)
                if _intersection(shifted, self.domain) is not None:
                    observations.append((shifted, peak))
        # Both lists are ordered; avoid a quadratic intersection over all events.
        index = 0
        for interval, peak in peak_envelope(observations, self.domain):
            while index < len(safe) and safe[index].end < interval.start:
                index += 1
            j = index
            while j < len(safe) and safe[j].start <= interval.end:
                part = _intersection(interval, safe[j])
                if part is not None:
                    self.atoms.append((part, peak))
                j += 1

    def best_minute(self, now, limit=math.inf):
        choices = [(peak, minute) for interval, peak in self.atoms if peak <= limit
                   and (minute := first_minute(interval, now)) is not None]
        return min(choices, default=None)

    def windows(self, limit, now):
        start = max(self.start, now)
        result = describe_evaluation(self.check, datetime.fromtimestamp(start, timezone.utc),
            self.request.average_ground_speed_mps, datetime.fromtimestamp(self.end, timezone.utc))
        # "Depart now" is independent of a future user-specified lower bound.
        if start != now:
            current = describe_evaluation(self.check, datetime.fromtimestamp(now, timezone.utc),
                                           self.request.average_ground_speed_mps)
            result.update(current_departure=current['current_departure'], evaluated_at=iso(now), refresh_at=current['refresh_at'])
        ranges = []
        domain = Interval(start, self.end)
        for interval, peak in self.atoms:
            part = _intersection(interval, domain)
            if peak <= limit and part is not None:
                ranges.append(part)
        windows = []
        for interval in _merge(ranges):
            a, b = math.ceil(interval.start/300)*300, math.floor(interval.end/300)*300
            if not interval.contains(a): a += 300
            if not interval.contains(b): b -= 300
            if a <= b:
                windows.append(dict(start=iso(a), end=iso(b), first_arrival=iso(a+self.check.duration_s),
                                    last_arrival=iso(b+self.check.duration_s)))
        result.update(departure_windows=windows, peak_wind_limit_mps=limit, peak_wind_model=PEAK_MODEL,
            departure_start_at=iso(self.start), departure_end_at=iso(self.end),
            dataset_id=self.dataset.dataset_id, source=self.dataset.source, level=self.request.level,
            low_wind_certified=True)
        unknown = []
        if self.domain.end < self.end and max(start, self.domain.end) < self.end:
            unknown.append(dict(start=iso(max(start, self.domain.end)), end=iso(self.end),
                                reason='预报未覆盖该起飞时刻对应的完整航程'))
        if start < self.domain.start:
            unknown.append(dict(start=iso(start), end=iso(self.domain.start), reason='起飞早于预报覆盖'))
        result['unconfirmed_ranges'] = unknown
        result['coverage_incomplete'] = result['coverage_incomplete'] or bool(unknown)
        if not windows and not result.get('coverage_incomplete'):
            result['empty_reason'] = 'no_low_wind_window'
        return result
