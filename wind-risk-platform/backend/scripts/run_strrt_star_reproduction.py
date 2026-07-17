"""A small, self-contained reproduction of OMPL's ST-RRT* idea.

This script is intentionally independent from OMPL's C++ build.  It follows the
structure described by OMPL's ``STRRTstar`` and ``SpaceTimeStateSpace``:

* state = (x, y, t)
* motions must move forward in time
* spatial distance / delta_t must not exceed ``v_max``
* a bidirectional tree is grown from the start and from sampled goal states
* goal-state time is sampled from a gradually expanded time bound
* the objective is minimum arrival time

It is a research/demo reproduction, not a byte-for-byte port of OMPL.  The
implementation uses a simple O(n) nearest-neighbor search so it can run without
extra dependencies.

Run:

    python wind-risk-platform/backend/scripts/run_strrt_star_reproduction.py --demo

Save the result:

    python wind-risk-platform/backend/scripts/run_strrt_star_reproduction.py --demo --output data/strrt_star_demo.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


EPSILON = 1e-9


@dataclass(frozen=True)
class SpaceTimeState:
    x: float
    y: float
    t: float

    def spatial_distance(self, other: "SpaceTimeState") -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


@dataclass
class MovingCircle:
    """Circular obstacle moving linearly from start_center to end_center."""

    start_center: tuple[float, float]
    end_center: tuple[float, float]
    t0: float
    t1: float
    radius: float

    def center_at(self, t: float) -> tuple[float, float]:
        if self.t1 <= self.t0:
            return self.end_center
        ratio = min(1.0, max(0.0, (t - self.t0) / (self.t1 - self.t0)))
        return (
            self.start_center[0] + (self.end_center[0] - self.start_center[0]) * ratio,
            self.start_center[1] + (self.end_center[1] - self.start_center[1]) * ratio,
        )

    def contains(self, state: SpaceTimeState, clearance: float = 0.0) -> bool:
        cx, cy = self.center_at(state.t)
        return math.hypot(state.x - cx, state.y - cy) <= self.radius + clearance


@dataclass
class STRRTStarConfig:
    x_bounds: tuple[float, float] = (0.0, 1.0)
    y_bounds: tuple[float, float] = (0.0, 1.0)
    v_max: float = 0.55
    max_time: float = 6.0
    start_time: float = 0.0
    max_distance: float = 0.35
    max_iterations: int = 12000
    batch_size: int = 512
    initial_time_bound_factor: float = 1.8
    time_bound_factor_increase: float = 1.6
    goal_sample_ratio: int = 4
    goal_radius: float = 0.04
    time_weight: float = 0.35
    interpolation_resolution: float = 0.015
    seed: int = 7
    optimum_approx_factor: float = 1.0


@dataclass
class Motion:
    state: SpaceTimeState
    parent: "Motion | None" = None
    root: "Motion | None" = None
    children: list["Motion"] | None = None

    def __post_init__(self) -> None:
        if self.root is None:
            self.root = self
        if self.children is None:
            self.children = []


@dataclass
class PlanningResult:
    solved: bool
    path: list[SpaceTimeState]
    best_time: float | None
    iterations: int
    start_tree_size: int
    goal_tree_size: int
    planning_time_sec: float
    message: str

    def to_json_dict(self) -> dict:
        return {
            "solved": self.solved,
            "best_time": self.best_time,
            "iterations": self.iterations,
            "start_tree_size": self.start_tree_size,
            "goal_tree_size": self.goal_tree_size,
            "planning_time_sec": self.planning_time_sec,
            "message": self.message,
            "path": [asdict(state) for state in self.path],
        }


class SpaceTimeEnvironment:
    def __init__(self, config: STRRTStarConfig, obstacles: Iterable[MovingCircle] = ()):
        self.config = config
        self.obstacles = list(obstacles)

    def in_bounds(self, state: SpaceTimeState) -> bool:
        return (
            self.config.x_bounds[0] <= state.x <= self.config.x_bounds[1]
            and self.config.y_bounds[0] <= state.y <= self.config.y_bounds[1]
            and self.config.start_time <= state.t <= self.config.max_time
        )

    def state_valid(self, state: SpaceTimeState) -> bool:
        return self.in_bounds(state) and not any(obstacle.contains(state) for obstacle in self.obstacles)

    def motion_valid(self, state_from: SpaceTimeState, state_to: SpaceTimeState) -> bool:
        """Equivalent to OMPL's SpaceTimeMotionValidator check.

        The segment must go forward in time, respect the speed bound, and stay
        valid at interpolated space-time samples.
        """

        delta_t = state_to.t - state_from.t
        if delta_t <= EPSILON:
            return False
        distance = state_from.spatial_distance(state_to)
        if distance / delta_t > self.config.v_max + 1e-8:
            return False
        if not self.state_valid(state_to):
            return False

        steps = max(2, int(math.ceil(max(distance, delta_t) / self.config.interpolation_resolution)))
        for index in range(1, steps):
            ratio = index / steps
            state = interpolate_state(state_from, state_to, ratio)
            if not self.state_valid(state):
                return False
        return True


def interpolate_state(a: SpaceTimeState, b: SpaceTimeState, ratio: float) -> SpaceTimeState:
    return SpaceTimeState(
        a.x + (b.x - a.x) * ratio,
        a.y + (b.y - a.y) * ratio,
        a.t + (b.t - a.t) * ratio,
    )


class STRRTStarReproduction:
    def __init__(
        self,
        start_xy: tuple[float, float],
        goal_xy: tuple[float, float],
        config: STRRTStarConfig | None = None,
        obstacles: Iterable[MovingCircle] = (),
    ):
        self.config = config or STRRTStarConfig()
        self.rng = random.Random(self.config.seed)
        self.start = SpaceTimeState(start_xy[0], start_xy[1], self.config.start_time)
        self.goal_xy = goal_xy
        self.env = SpaceTimeEnvironment(self.config, obstacles)
        self.start_tree: list[Motion] = []
        self.goal_tree: list[Motion] = []
        self.goal_roots: list[Motion] = []
        self.best_path: list[SpaceTimeState] = []
        self.best_time = math.inf
        self.minimum_time = self.config.start_time + self._time_to_cover_xy(self.start.x, self.start.y, *goal_xy)
        self.current_time_bound = min(
            self.config.max_time,
            max(self.minimum_time, self.minimum_time * self.config.initial_time_bound_factor),
        )

    def solve(self) -> PlanningResult:
        started = time.perf_counter()
        if not self.env.state_valid(self.start):
            return self._result(started, 0, "invalid start state")
        if self.minimum_time > self.config.max_time:
            return self._result(started, 0, "goal cannot be reached before max_time with v_max")

        self.start_tree = [Motion(self.start)]
        first_goal = self._sample_goal_root()
        if first_goal is None:
            return self._result(started, 0, "failed to sample an initial valid goal state")
        self.goal_tree.append(first_goal)
        self.goal_roots.append(first_goal)

        batch_samples = len(self.start_tree) + len(self.goal_tree)
        grow_start_tree = True
        iterations = 0

        for iterations in range(1, self.config.max_iterations + 1):
            if batch_samples >= self.config.batch_size and not math.isfinite(self.best_time):
                self._increase_time_bound()
                batch_samples = 0

            if iterations % self.config.goal_sample_ratio == 0:
                goal = self._sample_goal_root()
                if goal is not None:
                    self.goal_tree.append(goal)
                    self.goal_roots.append(goal)
                    batch_samples += 1

            random_state = self._sample_state()
            if random_state is None:
                continue

            if grow_start_tree:
                added = self._grow_tree(self.start_tree, random_state, is_start_tree=True, connect=False)
                if added is not None:
                    batch_samples += 1
                    reached = self._grow_tree(self.goal_tree, added.state, is_start_tree=False, connect=True)
                    if reached is not None and self._same_state(reached.state, added.state):
                        self._try_update_solution(added, reached)
            else:
                added = self._grow_tree(self.goal_tree, random_state, is_start_tree=False, connect=False)
                if added is not None:
                    batch_samples += 1
                    reached = self._grow_tree(self.start_tree, added.state, is_start_tree=True, connect=True)
                    if reached is not None and self._same_state(reached.state, added.state):
                        self._try_update_solution(reached, added)

            grow_start_tree = not grow_start_tree

            if math.isfinite(self.best_time):
                # OMPL narrows the upper time bound after each better solution.
                tightened = self.minimum_time + (self.best_time - self.minimum_time) * self.config.optimum_approx_factor
                self.current_time_bound = min(self.current_time_bound, max(self.minimum_time, tightened))
                if self.best_time <= self.minimum_time + 0.03:
                    break

        message = "exact/near-optimal solution found" if self.best_path else "timeout without solution"
        return self._result(started, iterations, message)

    def _space_time_distance(self, a: SpaceTimeState, b: SpaceTimeState) -> float:
        spatial = a.spatial_distance(b)
        temporal = abs(b.t - a.t)
        if spatial / self.config.v_max > temporal + 1e-8:
            return math.inf
        return (1.0 - self.config.time_weight) * spatial + self.config.time_weight * temporal

    def _time_to_cover_xy(self, x1: float, y1: float, x2: float, y2: float) -> float:
        return math.hypot(x2 - x1, y2 - y1) / self.config.v_max

    def _sample_goal_root(self) -> Motion | None:
        for _ in range(80):
            radius = self.config.goal_radius * math.sqrt(self.rng.random())
            angle = self.rng.random() * math.tau
            gx = self.goal_xy[0] + radius * math.cos(angle)
            gy = self.goal_xy[1] + radius * math.sin(angle)
            min_time = self.config.start_time + self._time_to_cover_xy(self.start.x, self.start.y, gx, gy)
            upper = min(self.config.max_time, max(self.current_time_bound, min_time))
            if min_time > upper:
                continue
            t = self.rng.uniform(min_time, upper)
            state = SpaceTimeState(gx, gy, t)
            if self.env.state_valid(state):
                return Motion(state)
        return None

    def _sample_state(self) -> SpaceTimeState | None:
        for _ in range(120):
            x = self.rng.uniform(*self.config.x_bounds)
            y = self.rng.uniform(*self.config.y_bounds)
            earliest = self.config.start_time + self._time_to_cover_xy(self.start.x, self.start.y, x, y)
            latest = min(
                self.config.max_time,
                self.current_time_bound - self._time_to_cover_xy(x, y, *self.goal_xy),
            )
            if latest <= earliest:
                t = self.rng.uniform(self.config.start_time, min(self.config.max_time, self.current_time_bound))
            else:
                # Conditional sampling in the reachable time window.
                t = self.rng.uniform(earliest, latest)
            state = SpaceTimeState(x, y, t)
            if self.env.state_valid(state):
                return state
        return None

    def _increase_time_bound(self) -> None:
        if self.current_time_bound >= self.config.max_time:
            return
        span_from_start = max(EPSILON, self.current_time_bound - self.config.start_time)
        self.current_time_bound = min(
            self.config.max_time,
            self.config.start_time + span_from_start * self.config.time_bound_factor_increase,
        )

    def _nearest_candidates(self, tree: list[Motion], target: SpaceTimeState, is_start_tree: bool) -> list[Motion]:
        candidates: list[tuple[float, Motion]] = []
        for motion in tree:
            if is_start_tree and motion.state.t >= target.t - EPSILON:
                continue
            if not is_start_tree and motion.state.t <= target.t + EPSILON:
                continue
            distance = self._space_time_distance(motion.state, target)
            if math.isfinite(distance):
                candidates.append((distance, motion))
        candidates.sort(key=lambda item: item[0])
        return [motion for _, motion in candidates]

    def _grow_tree(
        self,
        tree: list[Motion],
        target: SpaceTimeState,
        is_start_tree: bool,
        connect: bool,
    ) -> Motion | None:
        last_added: Motion | None = None
        while True:
            candidates = self._nearest_candidates(tree, target, is_start_tree)
            if not candidates:
                return last_added
            added = None
            for nearest in candidates[: max(1, int(math.ceil(math.log(len(tree) + 2))))]:
                added = self._grow_single(tree, nearest, target, is_start_tree)
                if added is not None:
                    break
            if added is None:
                return last_added
            last_added = added
            if self._same_state(added.state, target) or not connect:
                return added

    def _grow_single(
        self,
        tree: list[Motion],
        nearest: Motion,
        target: SpaceTimeState,
        is_start_tree: bool,
    ) -> Motion | None:
        distance = self._space_time_distance(nearest.state, target)
        if not math.isfinite(distance) or distance <= EPSILON:
            return None

        if distance > self.config.max_distance:
            target_state = interpolate_state(nearest.state, target, self.config.max_distance / distance)
        else:
            target_state = target

        if self._same_state(target_state, nearest.state):
            return None

        valid = (
            self.env.motion_valid(nearest.state, target_state)
            if is_start_tree
            else self.env.motion_valid(target_state, nearest.state)
        )
        if not valid:
            return None

        motion = Motion(target_state, parent=nearest, root=nearest.root)
        nearest.children.append(motion)
        tree.append(motion)

        if not is_start_tree:
            self._simplified_goal_rewire(motion)
        return motion

    def _simplified_goal_rewire(self, added: Motion) -> None:
        """Simplified form of OMPL's goal-tree rewiring.

        If a nearby goal-tree node can reach ``added`` through a faster goal
        root, move it under ``added``. This keeps the demo compact while
        preserving the important ST-RRT* idea: goal-tree branches are improved
        toward earlier arrival-time roots.
        """

        radius = self.config.max_distance * 1.5
        for other in list(self.goal_tree):
            if other is added or other.parent is None:
                continue
            if other.state.spatial_distance(added.state) > radius:
                continue
            if other.state.t >= added.state.t:
                continue
            if added.root and other.root and added.root.state.t >= other.root.state.t:
                continue
            if not self.env.motion_valid(other.state, added.state):
                continue
            if other.parent and other in other.parent.children:
                other.parent.children.remove(other)
            other.parent = added
            other.root = added.root
            added.children.append(other)
            self._update_descendant_roots(other, added.root)

    def _update_descendant_roots(self, motion: Motion, root: Motion | None) -> None:
        motion.root = root
        for child in motion.children or []:
            self._update_descendant_roots(child, root)

    def _try_update_solution(self, start_motion: Motion, goal_motion: Motion) -> None:
        path = self._construct_path(start_motion, goal_motion)
        if len(path) < 2 or not self._path_valid(path):
            return
        arrival = path[-1].t
        if self._goal_satisfied(path[-1]) and arrival < self.best_time:
            self.best_time = arrival
            self.best_path = path

    def _construct_path(self, start_motion: Motion, goal_motion: Motion) -> list[SpaceTimeState]:
        start_branch: list[SpaceTimeState] = []
        node: Motion | None = start_motion
        while node is not None:
            start_branch.append(node.state)
            node = node.parent
        start_branch.reverse()

        goal_branch: list[SpaceTimeState] = []
        node = goal_motion
        while node is not None:
            goal_branch.append(node.state)
            node = node.parent

        if start_branch and goal_branch and self._same_state(start_branch[-1], goal_branch[0]):
            goal_branch = goal_branch[1:]
        return start_branch + goal_branch

    def _path_valid(self, path: list[SpaceTimeState]) -> bool:
        return all(self.env.motion_valid(a, b) for a, b in zip(path, path[1:]))

    def _goal_satisfied(self, state: SpaceTimeState) -> bool:
        return math.hypot(state.x - self.goal_xy[0], state.y - self.goal_xy[1]) <= self.config.goal_radius

    @staticmethod
    def _same_state(a: SpaceTimeState, b: SpaceTimeState) -> bool:
        return abs(a.x - b.x) < 1e-6 and abs(a.y - b.y) < 1e-6 and abs(a.t - b.t) < 1e-6

    def _result(self, started: float, iterations: int, message: str) -> PlanningResult:
        solved = bool(self.best_path)
        return PlanningResult(
            solved=solved,
            path=self.best_path,
            best_time=self.best_time if solved else None,
            iterations=iterations,
            start_tree_size=len(self.start_tree),
            goal_tree_size=len(self.goal_tree),
            planning_time_sec=time.perf_counter() - started,
            message=message,
        )


def demo_problem(config: STRRTStarConfig) -> tuple[tuple[float, float], tuple[float, float], list[MovingCircle]]:
    """A tiny dynamic-obstacle problem.

    The moving circle crosses the straight start-goal corridor, so a valid
    space-time path typically either waits in time or bends around it.
    """

    start = (0.08, 0.50)
    goal = (0.92, 0.50)
    obstacles = [
        MovingCircle(
            start_center=(0.50, 0.20),
            end_center=(0.50, 0.80),
            t0=config.start_time,
            t1=config.max_time,
            radius=0.105,
        )
    ]
    return start, goal, obstacles


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reproduce the core idea of OMPL ST-RRT* in pure Python.")
    parser.add_argument("--demo", action="store_true", help="Run the built-in dynamic-obstacle demo.")
    parser.add_argument("--start", default="0.08,0.50", help="start x,y")
    parser.add_argument("--goal", default="0.92,0.50", help="goal x,y")
    parser.add_argument("--v-max", type=float, default=0.55)
    parser.add_argument("--max-time", type=float, default=6.0)
    parser.add_argument("--max-iterations", type=int, default=12000)
    parser.add_argument("--range", type=float, default=0.35, dest="max_distance")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", default="", help="Optional JSON output path.")
    return parser.parse_args()


def parse_xy(text: str) -> tuple[float, float]:
    values = [float(item.strip()) for item in text.split(",") if item.strip()]
    if len(values) != 2:
        raise ValueError("coordinate must be x,y")
    return values[0], values[1]


def main() -> None:
    args = parse_args()
    config = STRRTStarConfig(
        v_max=args.v_max,
        max_time=args.max_time,
        max_iterations=args.max_iterations,
        max_distance=args.max_distance,
        seed=args.seed,
    )
    if args.demo:
        start, goal, obstacles = demo_problem(config)
    else:
        start, goal = parse_xy(args.start), parse_xy(args.goal)
        obstacles = []

    planner = STRRTStarReproduction(start, goal, config=config, obstacles=obstacles)
    result = planner.solve()

    print("ST-RRT* reproduction result")
    print(f"  solved: {result.solved}")
    print(f"  message: {result.message}")
    print(f"  iterations: {result.iterations}")
    print(f"  start_tree_size: {result.start_tree_size}")
    print(f"  goal_tree_size: {result.goal_tree_size}")
    print(f"  planning_time_sec: {result.planning_time_sec:.4f}")
    if result.solved:
        print(f"  arrival_time: {result.best_time:.4f}")
        print("  path x,y,t:")
        for state in result.path:
            print(f"    {state.x:.4f}, {state.y:.4f}, {state.t:.4f}")

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result.to_json_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  saved: {output}")


if __name__ == "__main__":
    main()
