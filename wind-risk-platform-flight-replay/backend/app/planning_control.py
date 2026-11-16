"""Cooperative deadlines and cancellation for background route searches."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event
from time import monotonic


class PlanningCancelled(Exception):
    pass


class PlanningTimedOut(Exception):
    pass


class PlanningFinished(Exception):
    """The page finished its display and requested the accepted incumbent."""
    pass


class PlanningStageExpired(Exception):
    pass


@dataclass(frozen=True)
class PlanningControl:
    deadline: float
    cancel: Event
    stage_deadline: float | None = None
    progress: object = None
    finish: Event | None = None


_control = ContextVar("route_planning_control", default=None)


def current_control():
    return _control.get()


def checkpoint() -> None:
    control = _control.get()
    if control is None:
        return
    if control.cancel.is_set():
        raise PlanningCancelled()
    if control.finish is not None and control.finish.is_set():
        raise PlanningFinished()
    now = monotonic()
    if now >= control.deadline:
        raise PlanningTimedOut()
    if control.stage_deadline is not None and now >= control.stage_deadline:
        raise PlanningStageExpired()


def remaining_seconds(default=600.0):
    control = _control.get()
    return max(0.0, control.deadline - monotonic()) if control else default


@contextmanager
def planning_scope(deadline: float, cancel: Event, progress=None, finish=None):
    parent = current_control()
    token = _control.set(PlanningControl(deadline, cancel, progress=progress,
        finish=finish if finish is not None else parent.finish if parent else None))
    try:
        checkpoint()
        yield
        checkpoint()
    finally:
        _control.reset(token)


@contextmanager
def stage_budget(seconds: float):
    control = _control.get()
    token = None
    if control is not None:
        token = _control.set(PlanningControl(control.deadline, control.cancel, monotonic() + seconds, control.progress, control.finish))
    try:
        checkpoint()
        yield
        checkpoint()
    finally:
        if token is not None:
            _control.reset(token)


def search_progress(value):
    control = _control.get()
    if control is not None and control.progress is not None:
        control.progress(value)
