"""Enter a named shop from the city panel and confirm its menu."""
from __future__ import annotations

import math
from pathlib import Path

import cv2

from packages.aura_core.api import action_info, requires_services
from packages.aura_core.observability.logging.core_logger import logger
from packages.aura_core.scheduler.operation_scope import OperationScope, OperationTimeout, current_operation_scope
from ....aura_base.src.services._target_identity import read_target_identity
from ....aura_base.src.services.interaction_service import (
    InteractionError, InteractionService, TransitionPolicy, TransitionProbe, TransitionState,
)
from ....aura_base.src.services.observation_service import CaptureError
from ._city_panel_vision import CITY_REGION, CityPanelVisionError, probe_city
from ._city_shop_entry import (
    MAX_CLICKS, POLL_INTERVAL_SEC, RETRY_INTERVAL_SEC, STABLE_MATCHES,
    CityShopEntryError, probe_menu_template, valid_probe,
)
from ._navigation_templates import (
    CITY_PANEL_REPUTATION, EXCHANGE_MENU, GLOBAL_BACK, MIRAGE_ISLAND_ADVOCACY,
    PRISON_DAILY_REPORT_HEADER, NavigationTemplateProfile,
)
from ._prison_warehouse_ui import PrisonWarehouseUi
from ._recovery_assets import read_layout_json, validated_roi
from ._rubbish_recycling_vision import (
    RECYCLING_CENTER_TOLERANCE, RubbishRecyclingVisionError, classify_recycling_frame,
)
from .runtime_preflight_pc_actions import resonance_pc_require_client_resolution


PLAN_ROOT = Path(__file__).resolve().parents[2]
WATER_LAYOUT = PLAN_ROOT / "data/meta/sparkling_water.json"
_CLIENT = (0, 0, 1280, 720)
_CITY_IDENTIFY_TIMEOUT_SEC = 3.0
_SHOP_TRANSITION_TIMEOUT_SEC = 8.0
_REWARD_TITLE = NavigationTemplateProfile(
    "templates/deep_dive/consciousness_deep_dive_single_run/reward_obtained_title.png",
    (530, 50, 245, 95), .85,
)
_REWARD_EXIT = NavigationTemplateProfile(
    "templates/deep_dive/deep_dive_events/result_exit.png", (500, 645, 290, 74), .85,
)


def _shop_target(shop_key, *, vision, allow_recycling_pages=False):
    if shop_key == "exchange":
        return "shop_page", lambda frame, scope: _match(frame, EXCHANGE_MENU, vision=vision, scope=scope)
    if shop_key == "rest":
        spec = read_layout_json(WATER_LAYOUT)["templates"]["rest_menu"]
        profile = NavigationTemplateProfile(
            spec["path"], validated_roi(spec["roi"], _CLIENT[2:], "rest_menu"), spec["threshold"],
            use_grayscale=False, match_method=cv2.TM_SQDIFF_NORMED,
        )
        return "rest_menu", lambda frame, scope: _match(frame, profile, vision=vision, scope=scope, mask=spec.get("mask"))
    if shop_key == "reforming_center":
        ui = PrisonWarehouseUi()
        return "reforming_center_overview", lambda frame, scope: _probe_reforming_center(
            frame, ui=ui, vision=vision, scope=scope,
        )
    if shop_key == "mirage_island":
        return "mirage_island_home", lambda frame, scope: _match(
            frame, MIRAGE_ISLAND_ADVOCACY, vision=vision, scope=scope,
            mask="templates/commerce/investment/cape_island/controls/advocacy_button.mask.png",
        )
    if shop_key == "waste_processing_center":
        return "rubbish_processing_center", lambda frame, scope: _probe_rubbish_center(
            frame, vision=vision, scope=scope, allow_recycling_pages=allow_recycling_pages,
        )
    raise CityShopEntryError("city_shop_unsupported", "指定店铺尚未支持进店确认", {"shop_key": shop_key})


def _capture(interaction, scope, state, roi):
    try:
        return interaction.observation.capture(scope=scope, roi=roi)
    except CaptureError as exc:
        if read_target_identity(scope.target) != state.target_identity:
            raise InteractionError("input_target_changed", "observe") from exc
        if exc.reason != "waiting_for_fresh_frame":
            raise
        return None


def _identify_city(app, vision, interaction, scope, state):
    budget = scope.child(_CITY_IDENTIFY_TIMEOUT_SEC)

    def observe(_allow_source):
        frame = _capture(interaction, budget, state, CITY_REGION)
        if frame is None or frame.stale:
            return TransitionProbe(hit={"reason": "waiting_for_fresh_frame"})
        badge = probe_city(app=app, vision=vision, check_cancelled=budget.checkpoint,
                           source_image=frame.rgb_image())
        if valid_probe(badge) and badge["found"]:
            return TransitionProbe("target", badge["city_key"], badge, frame)
        return TransitionProbe(hit=badge, frame=frame)

    identified = interaction.probe_transition(
        observe, allow_source=False, scope=budget, stable_scans=STABLE_MATCHES,
        poll_seconds=POLL_INTERVAL_SEC, state=state,
    )
    if identified is None:
        scope.checkpoint()
        raise CityShopEntryError("city_panel_not_confirmed", "未能识别当前城市",
                                 {"last_observation": state.last.hit if state.last else None})
    return identified.key


def _match(frame, profile, *, vision, scope, mask=None):
    options = profile.match_kwargs()
    options.pop("threshold")
    if mask is not None:
        options["mask_image"] = str(PLAN_ROOT / mask)
    return probe_menu_template(
        app=None, vision=vision, template=PLAN_ROOT / profile.path, region=profile.roi,
        threshold=profile.threshold, check_cancelled=scope.checkpoint,
        source_image=frame.crop(profile.roi).rgb_image(), match_options=options,
    )


def _probe_reforming_center(frame, *, ui, vision, scope):
    report = _match(frame, PRISON_DAILY_REPORT_HEADER, vision=vision, scope=scope)
    if not report["valid"] or report["found"]:
        return {"valid": report["valid"], "found": False, "blocks_retry": True,
                "page": "daily_report" if report["found"] else "unknown", "daily_report": report}

    view = ui.probe_city_entry(cv2.cvtColor(frame.rgb_image(), cv2.COLOR_RGB2BGR))
    scope.checkpoint()
    page = view["page"]
    return {
        "valid": view["valid"], "found": page == "overview", "blocks_retry": page == "warehouse",
        "page": page, "daily_report": report, "controls": view["controls"],
    }


def _probe_rubbish_center(frame, *, vision, scope, allow_recycling_pages=False):
    view = classify_recycling_frame(image=frame.rgb_image(), vision=vision, check_cancelled=scope.checkpoint)
    page = view["state"]
    found = page == "station_menu" or (allow_recycling_pages and page in {"has_rubbish", "empty"})
    return {
        "valid": True, "found": found, "blocks_retry": not found and page != "city_panel",
        "page": page, "reason": view["reason"], "match": view["match"], "evidence": view["evidence"],
    }


def _entry_center(point):
    x, y = point["x"], point["y"]
    if (any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in (x, y)) or not (0 <= x < _CLIENT[2] and 0 <= y < _CLIENT[3])):
        raise CityShopEntryError("city_shop_point_outside_client", "店铺入口坐标超出游戏客户区", point)
    return int(x), int(y)


def _entry_detail(point, state, source_matches):
    return {
        **point,
        "click_attempts": len(state.receipts),
        "clicks": [
            {"sequence": receipt.sequence, "point": list(receipt.point), "frame_id": receipt.frame_id,
             "sent": receipt.sent, "acknowledged": receipt.acknowledged, "timestamp": receipt.timestamp,
             "source_match": source_matches.get(receipt.sequence)}
            for receipt in state.receipts
        ],
        "last_observation": state.last.hit if state.last else None,
    }


def _recycling_evidence_is_stable(previous, current):
    def center(sample):
        match = sample.hit.get("panel") if sample.kind == "source" else sample.hit["menu"]["match"]
        return (match or {}).get("center")

    old, new = center(previous), center(current)
    return old is not None and new is not None and all(
        abs(a - b) <= RECYCLING_CENTER_TOLERANCE for a, b in zip(old, new)
    )


def _enter_shop(point, *, app, vision, interaction, scope, state, locate_entry=None,
                city_panel_confirmed=False, source_frame=None, allow_recycling_pages=False, target_probe=None):
    city_key, shop_key = point["city_key"], point["shop_key"]
    page_state, default_probe = _shop_target(shop_key, vision=vision, allow_recycling_pages=allow_recycling_pages)
    probe_target = target_probe or default_probe
    initial_center = _entry_center(point)
    equivalent = _recycling_evidence_is_stable if shop_key == "waste_processing_center" else None
    source_matches = {}

    def observe_page(budget, allow_source):
        def observe(check_source):
            frame = _capture(interaction, budget, state, _CLIENT)
            if (frame is None or frame.stale
                    or state.receipts and frame.timestamp <= state.receipts[-1].timestamp):
                return TransitionProbe(hit={"reason": "waiting_for_fresh_frame"})

            target = probe_target(frame, budget)
            evidence = {"menu": target}
            if not target["valid"] or target.get("blocks_retry"):
                return TransitionProbe(hit=evidence, frame=frame)

            # A reward overlay can leave the underlying city or menu anchors visible.
            matches = target.get("evidence", {})
            evidence["reward_title"] = matches.get("reward_title") or _match(
                frame, _REWARD_TITLE, vision=vision, scope=budget,
            )
            evidence["reward_exit"] = matches.get("reward_exit") or _match(
                frame, _REWARD_EXIT, vision=vision, scope=budget,
            )
            if any(evidence[key]["found"] for key in ("reward_title", "reward_exit")):
                return TransitionProbe(hit=evidence, frame=frame)
            if target["found"]:
                return TransitionProbe("target", target.get("page", page_state), evidence, frame)
            if check_source:
                initial_menu_entry = not state.submitted and shop_key in {"exchange", "rest"}
                evidence["panel"] = matches.get("city_panel") or _match(
                    frame, CITY_PANEL_REPUTATION, vision=vision, scope=budget,
                )
                evidence["back"] = matches.get("back") or _match(frame, GLOBAL_BACK, vision=vision, scope=budget)
                if initial_menu_entry or (evidence["panel"]["found"] and evidence["back"]["found"]):
                    badge = probe_city(app=app, vision=vision, check_cancelled=budget.checkpoint,
                                       source_image=frame.crop(CITY_REGION).rgb_image())
                    evidence["city"] = badge
                    if valid_probe(badge) and badge["found"] and badge["city_key"] == city_key:
                        evidence["center"] = _entry_center(locate_entry()) if locate_entry else initial_center
                        return TransitionProbe("source", (city_key, evidence["center"]), evidence, frame)
            return TransitionProbe(hit=evidence, frame=frame)

        return interaction.probe_transition(
            observe, allow_source=allow_source, scope=budget, stable_scans=STABLE_MATCHES,
            poll_seconds=POLL_INTERVAL_SEC, equivalent=equivalent, state=state,
        )

    def probe(allow_source):
        if not state.submitted:
            return initial
        budget = transition_scope
        if not allow_source and len(state.receipts) < state.policy.max_clicks:
            budget = transition_scope.child(RETRY_INTERVAL_SEC)
        return observe_page(budget, allow_source)

    def click(observed, attempt):
        source_matches[attempt] = observed.hit.get("panel")
        x, y = observed.hit["center"]
        return app.click(x=x, y=y)

    try:
        if city_panel_confirmed or source_frame is not None:
            initial = TransitionProbe("source", city_key, {"center": initial_center}, frame=source_frame,
                                      immediate=True, target_identity=state.target_identity, target_bound=True)
        else:
            initial = observe_page(scope.child(_SHOP_TRANSITION_TIMEOUT_SEC), True)
            if initial is None:
                raise OperationTimeout("City panel or shop was not confirmed")
        if initial.kind != "target":
            transition_scope = scope.child(_SHOP_TRANSITION_TIMEOUT_SEC)
            interaction.run_transition(probe, click, scope=transition_scope, state=state)
    except OperationTimeout as exc:
        detail = {**_entry_detail(point, state, source_matches), "timeout_sec": _SHOP_TRANSITION_TIMEOUT_SEC}
        logger.warning("[CityShopNavigation] target not confirmed detail=%s", detail)
        raise CityShopEntryError("city_shop_entry_timeout", "超时未确认进入指定店铺", detail) from exc
    except CityPanelVisionError as exc:
        raise CityShopEntryError(exc.code, exc.message, {**_entry_detail(point, state, source_matches), **exc.detail}) from exc
    except (InteractionError, CaptureError, RubbishRecyclingVisionError) as exc:
        exc.detail = {**_entry_detail(point, state, source_matches), **getattr(exc, "detail", {})}
        raise
    result = {**_entry_detail(point, state, source_matches), "success": True, "page_state": page_state}
    logger.info("[CityShopNavigation] target confirmed result=%s", result)
    return result


def enter_resolved_city_shop(point, *, app, vision, interaction=None, check_cancelled=None,
                             source_frame=None, city_panel_confirmed=False, allow_recycling_pages=False,
                             max_clicks=MAX_CLICKS, target_probe=None):
    """Enter a resolved shop from an existing workflow, retaining its source evidence."""
    scope = OperationScope(parent=current_operation_scope(), target=getattr(app, "target_runtime", None),
                           cancel_check=check_cancelled)
    with scope.activate():
        clicks = min(MAX_CLICKS, max(1, int(max_clicks)))
        state = TransitionState(TransitionPolicy(allow_retry=True, max_clicks=clicks),
                                target_identity=read_target_identity(scope.target), target_bound=True)
        return _enter_shop(point, app=app, vision=vision, interaction=interaction or InteractionService(app),
                           scope=scope, state=state, source_frame=source_frame,
                           city_panel_confirmed=city_panel_confirmed, allow_recycling_pages=allow_recycling_pages,
                           target_probe=target_probe)


@action_info(
    name="resonance_pc.enter_city_shop", public=True, read_only=False,
    description="Enter the requested shop from the current city panel and confirm its menu.",
)
@requires_services(app="plans/aura_base/app", vision="plans/aura_base/vision", interaction="plans/aura_base/interaction",
                   resonance_pc_city_shop_data="resonance_pc_city_shop_data")
def resonance_pc_enter_city_shop(shop_name: str, city_name: str | None = None, app=None, vision=None,
                                 interaction=None, resonance_pc_city_shop_data=None) -> dict:
    scope = OperationScope(parent=current_operation_scope(), target=getattr(app, "target_runtime", None))
    with scope.activate():
        resonance_pc_require_client_resolution(app=app)
        state = TransitionState(TransitionPolicy(allow_retry=True, max_clicks=MAX_CLICKS),
                                target_identity=read_target_identity(scope.target), target_bound=True)
        city = str(city_name or "").strip() or _identify_city(app, vision, interaction, scope, state)
        point = resonance_pc_city_shop_data.resolve_shop_point(city, shop_name)
        scope.checkpoint()
        return _enter_shop(point, app=app, vision=vision, interaction=interaction,
                           scope=scope, state=state, city_panel_confirmed=True,
                           locate_entry=lambda: resonance_pc_city_shop_data.resolve_shop_point(
                               point["city_key"], point["shop_key"],
                           ))
