"""Comparable target binding and geometry, excluding input-driven focus state."""
from collections.abc import Mapping
from typing import Any

from packages.aura_core.observability.runtime_trace import log_event


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def freeze_target_identity(summary: Any) -> Any:
    # SendInput may focus the same window during move/click. Foreground state
    # therefore cannot identify a binding. Keep every other field, including
    # runtime-specific epochs and geometry used to interpret client positions.
    if isinstance(summary, Mapping):
        summary = {key: value for key, value in summary.items() if key != "foreground"}
    return _freeze(summary)


def read_target_identity(runtime: Any) -> Any:
    reader = getattr(runtime, "interaction_target_identity", None)
    if not callable(reader):
        reader = getattr(runtime, "target_summary", None)
    return freeze_target_identity(reader()) if callable(reader) else None


def target_identity_changed(previous: Any, current: Any, *, phase: str) -> bool:
    """Log changed fields without logging window titles or session values."""
    if previous == current:
        return False

    try:
        before, after = dict(previous or ()), dict(current or ())
    except (TypeError, ValueError):
        changed_fields = ["identity"]
    else:
        missing = object()
        changed_fields = [key for key in sorted(before.keys() | after.keys())
                          if before.get(key, missing) != after.get(key, missing)]
    log_event("input.target_changed", phase=phase, changed_fields=changed_fields)
    return True
