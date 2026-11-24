"""Open a named first-level page from the Resonance PC station main screen."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import math
from pathlib import Path

import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.operation_scope import OperationScope, OperationTimeout, current_operation_scope
from ....aura_base.src.services.interaction_service import (
    InteractionError, InteractionService, TransitionPolicy, TransitionProbe, TransitionState,
)
from ....aura_base.src.services.observation_service import CaptureError, ObservationService
from ._city_panel_vision import VISIT_REGION, VISIT_THRESHOLD
from ._depart_button_vision import load_depart_profile
from ._navigation_templates import PROFILE_MENU, NavigationTemplateProfile
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution


PLAN_ROOT = Path(__file__).resolve().parents[2]
CLIENT = (0, 0, 1280, 720)
ENTRY_TIMEOUT = 3.0
TRANSITION_TIMEOUT = 8.0
RETRY_INTERVAL = 1.2
POLL_INTERVAL = 0.15
MAX_CLICKS = 3

_VISIT = NavigationTemplateProfile(
    "templates/navigation/city/visit_entry.png", VISIT_REGION, VISIT_THRESHOLD,
    use_grayscale=False, match_method=cv2.TM_SQDIFF_NORMED,
)
_TRAIN_MAINTENANCE = NavigationTemplateProfile(
    "templates/navigation/global/train_maintenance.png", (920, 640, 80, 65), .8,
)


@dataclass(frozen=True)
class _Destination:
    page: str
    entry: NavigationTemplateProfile | tuple[int, int]
    # One complete group is enough. Every member of that group must be present.
    ready: tuple[tuple[NavigationTemplateProfile, ...], ...]


class StationMainNavigationError(RuntimeError):
    def __init__(self, code, message, detail):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail

    def to_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}


def _station_template(name, roi, *, threshold=.9, masked=False, use_grayscale=True):
    path = f"templates/navigation/station_main/{name}"
    return NavigationTemplateProfile(f"{path}.png", roi, threshold, use_grayscale=use_grayscale,
                                     mask=f"{path}.mask.png" if masked else None)


@lru_cache(maxsize=1)
def _destinations():
    depart = load_depart_profile()
    departure = NavigationTemplateProfile(depart.template, depart.region, depart.threshold,
                                          depart.use_grayscale, depart.match_method, depart.preprocess)
    reputation = _station_template("city_panel_reputation", (21, 633, 85, 75), threshold=.85, masked=True)
    passenger_entry = _station_template("passenger_entry", (983, 634, 85, 75), threshold=.88, masked=True)
    battle_entry = _station_template("battle_terminal_entry", (1065, 371, 107, 104), threshold=.84, masked=True)
    battle_logo = _station_template("battle_terminal_logo", (570, 10, 165, 155), masked=True, use_grayscale=False)
    daily_badge = _station_template("daily_activity_badge", (150, 111, 163, 142), threshold=.85,
                                    masked=True, use_grayscale=False)
    standard_handbook = _station_template("battle_pass_standard_handbook", (365, 132, 148, 163),
                                          masked=True, use_grayscale=False)
    return {
        "城市面板": _Destination("city_panel", _VISIT, ((reputation,),)),
        "城际地图": _Destination("intercity_map", departure, ((
            _station_template("map_help", (238, 8, 104, 67)),
            _station_template("map_legend", (329, 8, 108, 67)),
        ),)),
        "个人资料": _Destination("profile", (150, 655), ((PROFILE_MENU,),)),
        "客运管理": _Destination("passenger_management", passenger_entry, ((NavigationTemplateProfile(
            "templates/passenger/management/score_marker.png", (120, 70, 330, 130), .8,
        ),),)),
        "作战终端": _Destination("battle_terminal", battle_entry, ((battle_logo,),)),
        "每日任务": _Destination("daily_tasks", _station_template("daily_tasks_entry", (155, 0, 90, 70)),
                               ((daily_badge,),)),
        "通行证": _Destination("battle_pass", _station_template("battle_pass_entry", (820, 8, 105, 88)),
                             ((standard_handbook,),)),
    }


def _detail(target, state):
    last = state.last
    observation = None if last is None else {
        "kind": last.kind, "page": last.key, "frame_id": getattr(last.frame, "id", None), **(last.hit or {}),
    }
    return {"target": target, "click_attempts": len(state.receipts),
            "clicks": [list(receipt.point) for receipt in state.receipts if receipt.point is not None],
            "receipts": [asdict(receipt) for receipt in state.receipts],
            "last_observation": observation}


@action_info(
    name="resonance_pc.open_from_station_main", public=True, read_only=False,
    description="Open the requested page from the PC station main screen and confirm it is ready.",
)
@requires_services(app="plans/aura_base/app", interaction="plans/aura_base/interaction")
def resonance_pc_open_from_station_main(target: str, app=None, interaction=None) -> dict:
    return open_station_page(target, app=app, interaction=interaction)


def open_station_page(target, *, app, interaction=None, vision=None, check_start=False,
                      already_ready=(), cancel_check=None, entry_timeout=ENTRY_TIMEOUT):
    """Run the shared transition for a task or a legacy Python caller."""
    target = str(target or "").strip()
    destinations = _destinations()
    destination = destinations.get(target)
    if destination is None:
        raise StationMainNavigationError("station_main_target_unknown", "未知的站场主界面入口",
                                         {"target": target, "available_targets": list(destinations)})

    policy = TransitionPolicy(max_clicks=MAX_CLICKS, allow_retry=True)
    state = TransitionState(policy)
    parent = current_operation_scope()
    runtime = getattr(app, "target_runtime", None)
    if parent is not None and cancel_check is None and (runtime is None or runtime is parent.target):
        scope = parent.child()
    else:
        scope = OperationScope(parent=parent, target=runtime, cancel_check=cancel_check)
    if interaction is None:
        interaction = InteractionService(app, ObservationService(app=app, vision=vision))
    observer = interaction.observation
    main_markers = (_VISIT, destinations["城际地图"].entry, _TRAIN_MAINTENANCE)
    entry_observations = []

    def observe(budget, check_source, *, initial=False):
        if initial and not check_start and isinstance(destination.entry, tuple):
            return TransitionProbe("source", "station_main", {"center": destination.entry}, immediate=True)
        try:
            frame = observer.capture(scope=budget, roi=CLIENT)
        except CaptureError as exc:
            return TransitionProbe(hit={"reason": exc.reason})
        if frame.stale or (state.receipts and frame.timestamp <= state.receipts[-1].timestamp):
            return TransitionProbe(hit={"reason": "waiting_for_fresh_frame"}, frame=frame)

        matches = {}

        def match(profile):
            if profile.path not in matches:
                matches[profile.path] = observer.match_template(
                    frame, PLAN_ROOT / profile.path, scope=budget, roi=profile.roi,
                    mask=PLAN_ROOT / profile.mask if profile.mask else None, **profile.match_kwargs(),
                )
            return matches[profile.path]

        def evidence():
            details = {}
            for path, hit in matches.items():
                confidence = getattr(hit.value, "confidence", None)
                details[path] = {"found": hit.state == "present", "confidence": (
                    float(confidence) if confidence is not None and math.isfinite(confidence) else None
                ), "center": list(hit.client_center) if hit.client_center is not None else None}
            return {"matches": {path: hit.state for path, hit in matches.items()}, "match_details": details}

        if not initial or check_start:
            groups = (*destination.ready, *already_ready) if initial else destination.ready
            ready = any(all(match(marker).state == "present" for marker in group) for group in groups)
            if ready:
                # One sighting suppresses further clicks while the second frame is verified.
                if not initial:
                    state.source_locked = True
                return TransitionProbe("target", destination.page, evidence(), frame)
            if not check_source or any(hit.state != "absent" for hit in matches.values()):
                return TransitionProbe(hit=evidence(), frame=frame)
            if not all(match(marker).state == "present" for marker in main_markers):
                return TransitionProbe(hit=evidence(), frame=frame)

        if isinstance(destination.entry, tuple):
            center = destination.entry
        else:
            entrance = match(destination.entry)
            center = entrance.client_center if entrance.state == "present" else None
        if center is None:
            return TransitionProbe(hit=evidence(), frame=frame)
        return TransitionProbe("source", "station_main", {"center": center, **evidence()}, frame,
                               immediate=initial and not check_start)

    def click(probe, attempt):
        x, y = probe.hit["center"]
        entry_observations.append(probe.hit.get("match_details", {}))
        logger.info("[StationMainNavigation] target=%s click=%s point=(%s,%s)", target, attempt, x, y)
        return app.click(x=x, y=y)

    def probe_transition(budget, allow_source, *, initial=False):
        return interaction.probe_transition(
            lambda check: observe(budget, check, initial=initial), allow_source=allow_source, scope=budget,
            stable_scans=2, poll_seconds=POLL_INTERVAL, state=state,
        )

    def probe(allow_source):
        if not state.submitted:
            return first
        budget = transition
        if not allow_source and not state.source_locked and len(state.receipts) < MAX_CLICKS:
            budget = transition.child(RETRY_INTERVAL)
        return probe_transition(budget, allow_source)

    with scope.activate():
        scope.checkpoint()
        resonance_pc_require_client_resolution(app=app)
        first = probe_transition(scope.child(entry_timeout), True, initial=True)
        if first is None:
            scope.check_cancelled()
            raise StationMainNavigationError("station_main_entry_not_found", "未找到指定入口",
                                             {**_detail(target, state), "timeout_sec": entry_timeout})
        if first.kind == "target":
            return {**_detail(target, state), "success": True, "page_state": destination.page,
                    "entry_observations": []}
        transition = scope.child(TRANSITION_TIMEOUT)
        try:
            result = interaction.run_transition(probe, click, scope=transition, state=state)
        except OperationTimeout as exc:
            scope.check_cancelled()
            raise StationMainNavigationError("station_main_transition_timeout", "超时未确认目标页面",
                                             {**_detail(target, state), "timeout_sec": TRANSITION_TIMEOUT}) from exc
        except InteractionError as exc:
            scope.check_cancelled()
            exc.detail = _detail(target, state)
            raise
    return {**_detail(target, state), "success": result.confirmed, "page_state": destination.page,
            "entry_observations": entry_observations}
