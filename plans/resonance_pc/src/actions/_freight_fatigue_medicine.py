"""Select and verify batches of fatigue medicine from the profile."""
from __future__ import annotations

from dataclasses import dataclass
from numbers import Real
from pathlib import Path
import re
import time

import cv2

from packages.aura_core.scheduler.operation_scope import OperationTimeout
from packages.aura_core.utils.exceptions import StopTaskException
from packages.resonance_shared.freight_parameters import FATIGUE_MEDICINES

from .city_travel_pc_actions import _check_intercity_cancelled, _sleep_with_cancel
from .player_data_pc_actions import _close_profile_panel_to_main, _open_profile_panel, _read_profile_fatigue
from .player_recovery_pc_actions import RecoveryReader, load_recovery_layout


_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates/recovery/fatigue/medicine"
_CARDS = {"lollipop": (574, 155, 170, 169), "gum": (814, 155, 170, 169),
          "cactus": (574, 412, 170, 169)}
_RECOVERY = {"lollipop": 60, "gum": 100, "cactus": 900}
_NAME_ROI = (499, 220, 190, 47)
_INVENTORY_ROI = (735, 228, 169, 32)
_CURRENT_ROI = (450, 372, 92, 34)
_PREVIEW_ROI = (730, 372, 112, 34)
_SELECTION_ROI = (608, 448, 66, 30)
_TIMEOUT = 5.0


@dataclass(frozen=True)
class MedicineDialog:
    stock: int
    current: int
    preview: int
    selected: int
    maximum: int


def _safe_quantity(current: int, stop: int, recovery: int) -> int:
    if current <= stop:
        return 0
    # The game's preview is clamped to zero, which is also a valid lower limit.
    return (current - stop if stop else current + recovery - 1) // recovery


def _batch_quantity(dialog: MedicineDialog, *, key: str, stop: int, remaining: int) -> int:
    return min(dialog.stock, dialog.maximum, remaining, _safe_quantity(dialog.current, stop, _RECOVERY[key]))


def _make_reader(app, ocr, vision):
    layout = load_recovery_layout(vision)
    targets = {f"card_{key}": (roi, .90, False) for key, roi in _CARDS.items()}
    targets.update({f"name_{key}": (_NAME_ROI, .93, False) for key in FATIGUE_MEDICINES})
    targets.update({
        "quantity_title": ((535, 134, 215, 44), .90, True),
        "quantity_plus_one": ((795, 447, 65, 66), .90, False),
        "quantity_minus_one": ((421, 447, 66, 66), .90, False),
        "confirm_button": ((620, 520, 660, 120), .93, True),
        "cancel_replenish": ((240, 546, 191, 77), .92, False),
    })
    for key, (roi, threshold, grayscale) in targets.items():
        layout["templates"][key] = {"roi": roi, "threshold": threshold, "use_grayscale": grayscale,
                                    "resolved_path": str(_TEMPLATE_DIR / f"{key}.png")}
    return RecoveryReader(app, ocr, vision, layout)


def _click(reader, target, frame) -> bool:
    hit = reader.match(target, frame=frame)
    if not hit.found:
        return False
    x, y, width, height = reader.layout["templates"][target]["roi"]
    cx, cy = hit.center_point
    if not (0 <= cx < width and 0 <= cy < height):
        raise ValueError(f"Click point outside medicine control: {target}")
    _check_intercity_cancelled()
    reader.app.click(x=x + int(cx), y=y + int(cy))
    return True


def _read_digits(reader, frame, roi, pattern):
    _check_intercity_cancelled()
    image = cv2.resize(reader.capture(roi, frame=frame), None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    result = reader.ocr.recognize_all(source_image=image, recognition_only=True)
    if getattr(result, "success", True) is False or (getattr(result, "debug_info", None) or {}).get("error"):
        return None
    rows = [row for row in result.results if row.text.strip()]
    if not rows or any(isinstance(row.confidence, bool) or not isinstance(row.confidence, Real)
                       or not .85 <= row.confidence <= 1 for row in rows):
        return None
    text = re.sub(r"\s+", "", "".join(row.text for row in rows)).replace("／", "/")
    match = re.fullmatch(pattern, text)
    return tuple(map(int, match.groups())) if match else None


def _stable(reader, probe):
    deadline = time.monotonic() + _TIMEOUT
    previous, previous_frame = None, None
    while time.monotonic() < deadline:
        _check_intercity_cancelled()
        frame = reader.snapshot()
        value = probe(frame)
        if time.monotonic() >= deadline:
            break
        if value is not None and value == previous and frame.id != previous_frame:
            return value, frame
        previous, previous_frame = value, frame.id
        _sleep_with_cancel(0.18)
    return None, None


def _require_dialog(reader, frame, key):
    if not reader.match("quantity_title", frame=frame).found:
        return False
    if not reader.match(f"name_{key}", frame=frame).found:
        # During a transition the title can appear before the selected card does.
        if any(reader.match(f"name_{other}", frame=frame).found for other in FATIGUE_MEDICINES if other != key):
            raise RuntimeError("The medicine on the quantity screen differs from the selected card")
        return False
    return True


def _selection(reader, frame):
    pair = _read_digits(reader, frame, _SELECTION_ROI, r"([0-9]{1,5})/([0-9]{1,5})")
    return pair if pair is not None and pair[0] <= pair[1] else None


def _read_dialog(reader, key):
    def probe(frame):
        if not _require_dialog(reader, frame, key):
            return None
        selection = _selection(reader, frame)
        stock = _read_digits(reader, frame, _INVENTORY_ROI, r"持有数量[:：]?([0-9]{1,5})")
        current = _read_digits(reader, frame, _CURRENT_ROI, r"([0-9]{1,5})")
        preview = _read_digits(reader, frame, _PREVIEW_ROI, r"([0-9]{1,5})")
        if None not in (stock, current, preview, selection):
            dialog = MedicineDialog(stock[0], current[0], preview[0], *selection)
            if dialog.selected <= dialog.stock and dialog.preview == max(0, dialog.current - dialog.selected * _RECOVERY[key]):
                return dialog
        return None

    dialog, frame = _stable(reader, probe)
    if dialog is None:
        raise RuntimeError("Could not verify medicine quantity, stock and fatigue preview")
    return dialog, frame


def _change_quantity(reader, key, current, target, frame):
    step = 1 if target > current else -1
    control = "quantity_plus_one" if step == 1 else "quantity_minus_one"
    for expected in range(current + step, target + step, step):
        if not _click(reader, control, frame):
            raise RuntimeError("The medicine quantity control is unavailable")

        def probe(frame):
            if _require_dialog(reader, frame, key):
                selection = _selection(reader, frame)
                if selection is not None and selection[0] == expected:
                    return selection
            return None

        observed, frame = _stable(reader, probe)
        if observed is None:
            raise RuntimeError(f"Medicine quantity did not reach {expected}")


def _prepare_batch(reader, key, stop, remaining):
    dialog, frame = _read_dialog(reader, key)
    target = _batch_quantity(dialog, key=key, stop=stop, remaining=remaining)
    # Refresh the complete dialog again after adjusting the quantity. The game can
    # lower its maximum or the safe count while fatigue recovers naturally.
    while target and dialog.selected != target:
        _change_quantity(reader, key, dialog.selected, target, frame)
        updated, frame = _read_dialog(reader, key)
        if updated.selected != target:
            raise RuntimeError("Selected medicine quantity changed after adjustment")
        dialog = updated
        target = _batch_quantity(dialog, key=key, stop=stop, remaining=remaining)
    if target and not stop <= dialog.preview < dialog.current:
        raise RuntimeError("Medicine preview does not respect the stop threshold")
    return dialog, frame, target


def _read_after_use(app, ocr, reader):
    def profile_visible(frame):
        return True if reader.is_page("profile", frame=frame) and not reader.match("quantity_title", frame=frame).found else None

    returned, _ = _stable(reader, profile_visible)
    if not returned:
        raise RuntimeError("Medicine submission did not return to the profile page")
    return _read_profile_fatigue(app, ocr)["current"]


def _select_card(reader, candidates, unavailable):
    for key in candidates:
        frame = reader.snapshot()
        if not reader.is_page("fatigue_recovery", frame=frame):
            raise RuntimeError("The medicine recovery page could not be confirmed before selecting a card")
        x, y, _, _ = _CARDS[key]
        stock = _read_digits(reader, frame, (x + 117, y + 130, 47, 29), r"(0|[1-9][0-9]{0,4})")
        if stock == (0,) or not _click(reader, f"card_{key}", frame):
            unavailable.add(key)
            continue
        opened, _ = _stable(reader, lambda frame: True if _require_dialog(reader, frame, key) else None)
        if opened:
            return key
        unchanged, _ = _stable(reader, lambda frame: True if reader.is_page("fatigue_recovery", frame=frame) else None)
        if not unchanged:
            raise RuntimeError("The selected medicine did not open a confirmed quantity screen")
        unavailable.add(key)
    return None


def consume_fatigue_medicine(*, current, start, stop, limits, app, ocr, vision):
    result = {"success": False, "status": "failed", "reason": None, "page_state": "unknown",
              "initial_fatigue": current, "final_fatigue": None, "items": []}
    remaining = dict(limits)
    unavailable = set()
    stage = "open_profile"
    try:
        reader = _make_reader(app, ocr, vision)
        _open_profile_panel(app, vision)
        current = _read_profile_fatigue(app, ocr)["current"]
        result["initial_fatigue"] = current
        if current <= start:
            result["reason"] = "below_start_threshold"
        while result["reason"] is None and current > stop:
            candidates = [key for key in FATIGUE_MEDICINES if remaining.get(key, 0) > 0 and key not in unavailable
                          and _safe_quantity(current, stop, _RECOVERY[key]) > 0]
            if not candidates:
                if not any(remaining.values()):
                    result["reason"] = "fatigue_medicine_limit_reached"
                elif unavailable and all(not count or key in unavailable for key, count in remaining.items()):
                    result["reason"] = "fatigue_medicine_unavailable"
                else:
                    result["reason"] = "fatigue_medicine_stop_floor"
                break

            stage = "select_card"
            reader.move("profile", "fatigue_recovery", "fatigue_plus")
            key = _select_card(reader, candidates, unavailable)
            if key is None:
                result["reason"] = "fatigue_medicine_unavailable"
                reader.restore_profile()
                break

            stage = "prepare_batch"
            dialog, frame, count = _prepare_batch(reader, key, stop, remaining[key])
            current = dialog.current
            if not count:
                if not _click(reader, "cancel_replenish", frame):
                    raise RuntimeError("Could not cancel the unsubmitted medicine batch")
                # Cancel normally goes to the recovery page; accept an observed profile too.
                page, _ = _stable(reader, lambda f: "profile" if reader.is_page("profile", frame=f) else
                                  "recovery" if reader.is_page("fatigue_recovery", frame=f) else None)
                if page == "recovery":
                    reader.restore_profile()
                elif page != "profile":
                    raise RuntimeError("Could not return to the profile after cancelling medicine")
                if not dialog.stock or not dialog.maximum:
                    unavailable.add(key)
                continue

            entry = {"medicine_id": key, "name": FATIGUE_MEDICINES[key], "count": count,
                     "before": current, "preview": dialog.preview, "after": None, "confirmed": False}
            stage = "submit_batch"
            result["items"].append(entry)
            if not _click(reader, "confirm_button", frame):
                raise RuntimeError("Could not find the verified replenish button")
            entry["submitted"] = True
            stage = "verify_batch"
            after = _read_after_use(app, ocr, reader)
            entry["after"] = after
            if after != dialog.preview:
                raise RuntimeError("The fatigue after replenishing does not match the verified preview")
            entry["confirmed"] = True
            remaining[key] -= count
            current = after

        stage = "return_to_city"
        _close_profile_panel_to_main(app, vision)
        result.update(success=True, status="completed" if result["items"] else "skipped",
                      page_state="city_main", final_fatigue=current)
        if result["reason"] is None:
            result["reason"] = "stop_threshold_reached"
    except OperationTimeout:
        raise
    except (RuntimeError, ValueError, StopTaskException) as exc:
        _check_intercity_cancelled()
        result.update(reason=f"fatigue_medicine_{stage}_failed", error=str(exc))
    return result
