"""Independent temporal-WRF partition replay, retaining frozen spatial numerics.

Each evaluator constructs its own spatial detectors, event lists and integrations.
Only immutable source arrays may be shared.  No saved crossing, reference state,
grid snap, or solver trajectory is an input to event detection.
"""
import math

import numpy as np

from phase4t_g61.wrf_feasibility_validation_v1.runtime import boundaries as original
from phase4t.amendment_v1.model import DomainFail
from phase3a_implementation.adapter import StaticWRFWindAdapter
from phase_g61.dynamic_wind.provider import TemporalWRF, MissionSuffixView

BoundaryUnresolved = original.BoundaryUnresolved
POLICY = dict(
    version='G61_TEMPORAL_NATIVE_HEIGHT_UNION_PARTITION_V1',
    inherited_spatial_algorithm=dict(original.POLICY),
    horizontal='SAME_VERIFIED_NATIVE_GRID_I_J_ONCE',
    vertical='UNION_OF_EACH_NATIVE_SOURCE_ORIGINAL_HEIGHT_SURFACES',
    time='EXPLICIT_NATIVE_KNOT_TSPAN_SPLITS_EXACT_KNOWN_CLOCK_NO_EXTRAPOLATION',
    suffix_clock='PROVIDER_MISSION_EPOCH_PLUS_IMMUTABLE_SUFFIX_OFFSET_PLUS_LOCAL_T',
    independent_events='NEW_DETECTOR_AND_EVENT_LISTS_PER_EVALUATOR',
    state_join='COMMITTED_ODE_ENDPOINT_CARRIED_CONTINUOUSLY_NO_SNAP',
    unchanged='DOP853_TOLERANCES_ROOT_BRACKET_POLICY_SPATIAL_ARITHMETIC',
    numerical_assurance='SAME_EMPIRICAL_NUMERICAL_GATE_NOT_RIGOROUS_ODE_CERTIFICATE',
)


def temporal_source(provider):
    """Return the actual immutable mission provider and total suffix offset."""
    current = provider
    offset = 0.0
    visited = set()
    while isinstance(current, MissionSuffixView):
        if id(current) in visited:
            raise BoundaryUnresolved('CYCLIC_TEMPORAL_SUFFIX_VIEW')
        visited.add(id(current))
        value = current.physical_offset_s
        if isinstance(value, (bool, np.bool_)) or not math.isfinite(value) or value < 0:
            raise BoundaryUnresolved('INVALID_TEMPORAL_SUFFIX_OFFSET')
        offset = math.fsum((offset, float(value)))
        current = current.provider
    if isinstance(current, TemporalWRF):
        if not math.isfinite(offset):
            raise BoundaryUnresolved('NONFINITE_TEMPORAL_SUFFIX_OFFSET')
        return current, offset
    if getattr(provider, 'kind', None) == TemporalWRF.kind:
        raise BoundaryUnresolved('UNSUPPORTED_TEMPORAL_PROVIDER_WRAPPER')
    return None, 0.0


class PartitionReplay(original.PartitionReplay):
    """Frozen partition locator with two-source height union and time cuts."""

    def __init__(self, provider, origin, z, step, *, choice='MID'):
        mission, offset = temporal_source(provider)
        super().__init__(provider, origin, z, step, choice=choice)
        self.temporal = mission
        self.suffix_offset_s = offset
        self.temporal_contacts = []
        if mission is None:
            self.native_knots = ()
            self._detectors = ()
            return

        if hasattr(mission, 'assert_bound'):
            mission.assert_bound()
        adapters = tuple(mission._adapters)
        native_offsets = tuple(map(float, mission._offsets))
        if (len(adapters) < 2 or len(native_offsets) != len(adapters)
                or not all(isinstance(a, StaticWRFWindAdapter) for a in adapters)
                or not all(math.isfinite(t) for t in native_offsets)
                or any(b <= a for a, b in zip(native_offsets, native_offsets[1:]))):
            raise BoundaryUnresolved('INVALID_BOUND_TEMPORAL_NATIVE_SOURCES')
        first_grid = adapters[0].coordinates.summary()
        if any(a.coordinates.summary() != first_grid for a in adapters[1:]):
            raise BoundaryUnresolved('TEMPORAL_PARTITION_GRID_MISMATCH')
        if any(a.height.flags.writeable for a in adapters):
            raise BoundaryUnresolved('MUTABLE_TEMPORAL_HEIGHT_SURFACE')
        epoch = float(mission._epoch_offset)
        if not math.isfinite(epoch):
            raise BoundaryUnresolved('NONFINITE_TEMPORAL_MISSION_EPOCH')

        # Keep raw non-None: original.stream_trial must never silently disable
        # spatial boundary checks for this explicitly temporal provider kind.
        self.raw = adapters[0]
        self._detectors = tuple(
            original.PartitionReplay(a, origin, z, step, choice=choice)
            for a in adapters
        )
        for detector in self._detectors:
            # Shared counters only inside THIS evaluator, never event lists,
            # replay results, root brackets or state arrays across evaluators.
            detector.stats = self.stats
        self.native_knots = tuple(t - epoch - offset for t in native_offsets)
        self.stats.update(
            native_height_source_detectors=len(self._detectors),
            temporal_segment_calls=0,
            temporal_knot_splits=0,
            temporal_knot_boundary_contacts=0,
        )

    def value(self, dense, t, kind, index):
        if self.temporal is None:
            return super().value(dense, t, kind, index)
        if kind in ('i', 'j'):
            return self._detectors[0].value(dense, t, kind, index)
        if not isinstance(kind, str) or not kind.startswith('height_source_'):
            raise BoundaryUnresolved('UNKNOWN_TEMPORAL_PARTITION_AXIS')
        try:
            source_index = int(kind.removeprefix('height_source_'))
        except ValueError as exc:
            raise BoundaryUnresolved('INVALID_TEMPORAL_HEIGHT_SOURCE') from exc
        if not 0 <= source_index < len(self._detectors):
            raise BoundaryUnresolved('INVALID_TEMPORAL_HEIGHT_SOURCE')
        return self._detectors[source_index].value(dense, t, 'height', index)

    def candidates(self, dense):
        if self.temporal is None:
            return super().candidates(dense)
        union = []
        for source_index, detector in enumerate(self._detectors):
            for root, kind, index in detector.candidates(dense):
                if kind in ('i', 'j'):
                    if source_index == 0:
                        union.append((root, kind, index))
                elif kind == 'height':
                    union.append((root, 'height_source_' + str(source_index), index))
                else:
                    raise BoundaryUnresolved('UNKNOWN_INHERITED_PARTITION_AXIS')
        return sorted(set(union))

    def excluded_by_box(self, dense):
        if self.temporal is None:
            return super().excluded_by_box(dense)
        # All native height surfaces must be excluded.  A first-source-only
        # exclusion would miss a second-source vertical-stencil transition.
        return all(detector.excluded_by_box(dense) for detector in self._detectors)

    def integrate(self, a, b, state, u):
        if self.temporal is None:
            return super().integrate(a, b, state, u)
        a, b = float(a), float(b)
        if not all(math.isfinite(v) for v in (a, b)) or b <= a:
            raise BoundaryUnresolved('INVALID_TEMPORAL_REPLAY_INTERVAL')
        if a < self.native_knots[0] or b > self.native_knots[-1]:
            raise DomainFail('OUT_OF_TIME_DOMAIN')
        if hasattr(self.temporal, 'assert_bound'):
            self.temporal.assert_bound()
        internal = [t for t in self.native_knots if a < t < b]
        cuts = [a, *internal, b]
        current = np.asarray(state, float).copy()
        segments = []
        for left, right in zip(cuts[:-1], cuts[1:]):
            before = current.copy()
            current, dense = super().integrate(left, right, current, u)
            self.stats['temporal_segment_calls'] += 1
            segments.append(dict(a=left, b=right, dense=dense))
            if right in internal:
                if len(self.events) >= original.POLICY['max_events_per_run']:
                    raise BoundaryUnresolved('BOUNDARY_EVENT_LIMIT')
                source_index = self.native_knots.index(right)
                event = dict(
                    axis='native_time', index=source_index, t=right,
                    bracket_s=[right, right], width_s=0.0,
                    root_choice='KNOWN_NATIVE_UTC_KNOT',
                    source_utc=self.temporal._native_utc[source_index].isoformat(),
                    state_before_restart=current.tolist(),
                    state_after_restart=current.tolist(), u=float(u),
                    context='OWN_CONTINUOUS_REPLAY_KNOWN_NATIVE_CLOCK',
                    spatial_event_oracle=False,
                )
                self.events.append(event)
                self.stats['temporal_knot_splits'] += 1
            if left in self.native_knots:
                self.temporal_contacts.append(dict(
                    kind='NATIVE_TIME_AT_CONTROL_OR_ROUTE_BOUNDARY',
                    t=left, state=before.tolist(),
                ))
                self.stats['temporal_knot_boundary_contacts'] += 1

        def dense(t):
            i = min(int(np.searchsorted([s['b'] for s in segments], t, side='left')),
                    len(segments) - 1)
            return segments[i]['dense'](float(t))

        return current, dense
