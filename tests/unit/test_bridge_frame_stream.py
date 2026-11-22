import asyncio
import threading
from types import SimpleNamespace
import time
from unittest.mock import patch
from uuid import uuid4

import numpy as np
import pytest
import win32event

from packages.aura_core.context.plan import current_plan_name
from packages.aura_core.scheduler.operation_scope import OperationScope, OperationTimeout
from plans.aura_base.src.platform.contracts import TargetRuntimeError
from plans.resonance_pc.src.actions.runtime_preflight_pc_actions import resonance_pc_require_client_resolution
from plans.resonance_pc.src.actions._deep_dive_four_view_reader import source_key
from plans.resonance_pc.src.actions._deep_dive_frame_source import stream_metadata
from plans.resonance_pc.src.bridge.frame_stream import (
    BACKEND, BridgeFrameStream, HEIGHT, WIDTH, _DIAGNOSTIC_TRAILER, _FRAME_BYTES, _HEADER, _PIXEL_BYTES,
)
from plans.resonance_pc.src.bridge import frame_stream as frame_stream_module
from plans.resonance_pc.src.services.bridge_target_runtime_service import ResonancePcTargetRuntimeService
from plans.resonance_pc.src.services.bridge_app_service import ResonancePcAppService
from plans.resonance_pc.src.services import bridge_target_runtime_service, input_bridge_service
from plans.resonance_pc.src.services.input_bridge_service import ResonancePcInputBridgeService


@pytest.fixture
def stream():
    frames = BridgeFrameStream(uuid4().hex, max_stale_ms=250)
    try:
        yield frames
    finally:
        frames.close()


def publish(stream, generation, *, age=0., state=1):
    tick = int((frame_stream_module.time.monotonic() - stream._clock_origin - age) * stream._frequency)
    # Store RGBA in Unity's bottom-up order.
    pixels = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
    pixels[-1, 17] = (2, 7, 11, 99)
    stream._buffer[:_HEADER.size] = _HEADER.pack(state, WIDTH, HEIGHT, _PIXEL_BYTES, generation, tick, 100)
    stream._buffer[_HEADER.size:_FRAME_BYTES] = pixels.tobytes()


@pytest.fixture
def recovery_runtime(stream, monkeypatch):
    config = {"resonance_pc.input.mode": "bridge"}
    monkeypatch.delenv("AURA_GUI_INPUT_BRIDGE", raising=False)
    bridge = ResonancePcInputBridgeService(config=config)
    runtime = ResonancePcTargetRuntimeService(config, bridge)
    bridge._viewport = {"width": WIDTH, "height": HEIGHT}
    bridge._frame_stream = stream
    bridge._session_root_cid = "-"
    clock = SimpleNamespace(now=time.monotonic(), delayed=None)

    def sleep(duration):
        clock.now += duration
        if clock.delayed is not None and clock.now >= clock.delayed[0]:
            _, generation, state = clock.delayed
            publish(stream, generation, state=state)
            clock.delayed = None

    virtual_time = SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
    monkeypatch.setattr(bridge_target_runtime_service, "time", virtual_time)
    monkeypatch.setattr(frame_stream_module, "time", virtual_time)
    monkeypatch.setattr(stream, "_query_clock", lambda: int((clock.now - stream._clock_origin) * stream._frequency))
    events = []
    monkeypatch.setattr(bridge_target_runtime_service, "log_event", lambda event, **data: events.append((event, data)))
    app = ResonancePcAppService(SimpleNamespace(target_runtime=runtime), SimpleNamespace(bridge=bridge))
    token = current_plan_name.set("resonance_pc")
    try:
        yield runtime, app, clock, events
    finally:
        current_plan_name.reset(token)


@pytest.mark.parametrize("cached", [False, True])
def test_fresh_capture_waits_through_three_second_scene_change(stream, recovery_runtime, cached):
    runtime, _, clock, events = recovery_runtime
    if cached:
        publish(stream, 4, age=.1)
    start = clock.now
    clock.delayed = (start + 3, 5, 1)
    frame = runtime.capture((16, 0, 2, 1))
    assert clock.now - start == pytest.approx(3, abs=.006)
    assert not frame.stale and frame.frame_generation == 5
    assert frame.image[0, 1].tolist() == [2, 7, 11]
    event, details = events[-1]
    assert event == "capture.bridge_recovered" and details["used_stale_fallback"] is False


def test_stale_opt_in_keeps_capture_identity_and_four_second_budget(stream, recovery_runtime):
    _, app, clock, events = recovery_runtime
    publish(stream, 4, age=.1)
    start = clock.now
    returned = []
    for rect in ((16, 0, 2, 1), (17, 0, 1, 1), (16, 0, 2, 1)):
        returned.append(app.capture_latest(rect=rect))
    assert all(frame.stale for frame in returned)
    assert {frame.frame_id for frame in returned} == {f"{stream.session_id}:4"}
    assert {frame.frame_generation for frame in returned} == {4}
    assert len({frame.captured_at_monotonic for frame in returned}) == 1
    assert returned[1].image[0, 0].tolist() == [2, 7, 11]
    with pytest.raises(TargetRuntimeError) as error:
        app.capture_latest()
    assert error.value.code == "bridge_capture_timeout"
    assert clock.now - start == pytest.approx(4)
    assert [event for event, _ in events] == ["capture.bridge_stale"] * 3 + ["capture.bridge_timeout"]


def test_fresh_frame_replaces_stale_and_new_request_gets_its_own_budget(stream, recovery_runtime):
    _, app, clock, events = recovery_runtime
    publish(stream, 4, age=.1)
    start = clock.now
    first = app.capture_latest()
    clock.delayed = (start + 2.5, 5, 1)
    assert app.capture_latest().frame_id == first.frame_id
    latest = app.capture_latest()
    assert latest.frame_generation == 5 and not latest.stale
    assert events[-1][0] == "capture.bridge_recovered"
    assert events[-1][1]["used_stale_fallback"] is True
    assert app.capture_latest().frame_id == latest.frame_id
    assert clock.now - start == pytest.approx(3.5, abs=.02)


def test_viewport_change_or_outer_deadline_do_not_return_cached_frame(stream, recovery_runtime):
    _, app, clock, events = recovery_runtime
    publish(stream, 4, age=.1)
    start = clock.now
    scope = OperationScope(target=app.target_runtime, timeout=.5, monotonic=lambda: clock.now)
    with scope.activate(), pytest.raises(OperationTimeout):
        app.capture_latest()
    assert clock.now - start == pytest.approx(.5, abs=.006)
    clock.delayed = (clock.now + .2, 5, 2)
    with pytest.raises(TargetRuntimeError) as error:
        app.capture_latest()
    assert error.value.code == "viewport_changed"
    assert not events


def test_bound_viewport_change_invalidates_even_the_unmodified_native_buffer(stream, recovery_runtime):
    runtime, app, _, events = recovery_runtime
    publish(stream, 4, age=.1)
    stream.target_key = (None, (WIDTH, HEIGHT, 1), None)
    runtime.bridge._viewport["generation"] = 2
    with pytest.raises(TargetRuntimeError, match="viewport changed") as error:
        app.capture_latest()
    assert error.value.code == "viewport_changed"
    assert stream.capture_stream_frame(allow_stale=True) is None
    assert not events


def test_cooperative_cancellation_never_returns_a_cached_frame(stream, recovery_runtime, monkeypatch):
    _, app, clock, events = recovery_runtime
    publish(stream, 4, age=.1)
    stop_at = clock.now + .3
    monkeypatch.setattr(bridge_target_runtime_service, "is_current_task_cancel_requested",
                        lambda: clock.now >= stop_at)
    with pytest.raises(asyncio.CancelledError):
        app.capture_latest()
    assert clock.now == pytest.approx(stop_at, abs=.006, rel=0)
    assert stream._capture_wait is None and not events


def test_failure_attachment_includes_marked_cached_frame(stream, recovery_runtime):
    runtime, _, _, _ = recovery_runtime
    runtime.bridge._mode = "bridge"
    publish(stream, 4, age=2.)
    result = runtime._capture_failure_png({"plan": "resonance_pc", "root_cid": "-"})
    assert result["png"].startswith(b"\x89PNG")
    assert result["stale"] is True and result["frame_generation"] == 4
    assert result["frame_age_ms"] == pytest.approx(2000, abs=.01)


def test_diagnostic_snapshot_reads_native_timing_and_legacy_frame_age(stream, monkeypatch):
    publish(stream, 1)
    legacy = stream.diagnostic_snapshot()
    assert legacy["generation"] == 1 and legacy["last_unity_frame"] == 100
    assert 0 <= legacy["last_frame_age_ms"] < 250
    assert "last_native_capture_ms" not in legacy

    now = 10 * stream._frequency
    monkeypatch.setattr(stream, "_query_clock", lambda: now)
    ticks = stream._frequency // 1000
    _DIAGNOSTIC_TRAILER.pack_into(
        stream._buffer, _FRAME_BYTES, 4, 3, now - 410*ticks, now - 500*ticks,
        290*ticks, 2, now - 790*ticks, 101,
    )
    assert stream.diagnostic_snapshot() == {
        "generation": 2, "last_unity_frame": 101, "last_frame_age_ms": 790.0,
        "native_capture_attempts": 3, "last_native_capture_ms": 290.0,
        "native_capture_in_progress_ms": 410.0,
    }
    assert stream.capture_stream_frame()["generation"] == 1
    # An odd sequence means the writer has not finished its metadata update.
    _DIAGNOSTIC_TRAILER.pack_into(stream._buffer, _FRAME_BYTES, 5, 0, 0, 0, 0, 0, 0, 0)
    assert stream.diagnostic_snapshot() == {}


def test_capture_timeout_logs_changed_generation_even_if_new_frame_was_stale(stream, monkeypatch):
    config = {"resonance_pc.input.mode": "bridge"}
    monkeypatch.delenv("AURA_GUI_INPUT_BRIDGE", raising=False)
    bridge = ResonancePcInputBridgeService(config=config)
    runtime = ResonancePcTargetRuntimeService(config, bridge)
    bridge._viewport = {"width": WIDTH, "height": HEIGHT}
    bridge._frame_stream = stream
    bridge._session_root_cid = "-"
    events = []
    monkeypatch.setattr(bridge_target_runtime_service, "log_event", lambda event, **data: events.append((event, data)))
    raw_poll = stream.capture_stream_frame
    publish(stream, 4, age=2)

    def offer_stale_frame(*args, **kwargs):
        publish(stream, 5, age=2)
        return raw_poll(*args, **kwargs)

    monkeypatch.setattr(stream, "capture_stream_frame", offer_stale_frame)
    token = current_plan_name.set("resonance_pc")
    try:
        with pytest.raises(TargetRuntimeError) as error:
            runtime.capture((30, 470, 540, 180))
    finally:
        current_plan_name.reset(token)
    assert error.value.code == "bridge_capture_timeout"
    assert events == [("capture.bridge_timeout", error.value.detail)]
    detail = error.value.detail
    assert detail["frame_generation_start"] == 4 and detail["frame_generation_end"] == 5
    assert detail["frame_generation_changed"] is True
    assert detail["last_frame_age_ms"] > 250
    assert detail["last_native_capture_ms"] is None


def test_poll_is_atomic_and_rejects_old_or_replaced_frames(stream):
    publish(stream, 1, age=2)
    assert stream.capture_stream_frame() is None
    publish(stream, 2)
    frame = stream.capture_stream_frame()
    assert frame['capture'].backend == BACKEND
    assert tuple(frame['capture'].image[0, 17]) == (2, 7, 11)
    assert stream.capture_stream_frame(after_generation=2) is None
    with pytest.raises(TargetRuntimeError) as replaced:
        stream.capture_stream_frame(expected_session_id=stream.session_id + 1)
    assert replaced.value.code == 'capture_stream_replaced'
    publish(stream, 3, state=2)
    with pytest.raises(TargetRuntimeError) as changed:
        stream.capture_stream_frame()
    assert changed.value.code == 'viewport_changed'
    stream.stop_reading()
    with pytest.raises(TargetRuntimeError) as closed:
        stream.capture_stream_frame()
    assert closed.value.code == 'capture_stream_closed'


def test_frame_poll_is_independent_of_input_and_drops_busy_buffers(stream):
    publish(stream, 1)
    assert win32event.WaitForSingleObject(stream._mutex, 0) == win32event.WAIT_OBJECT_0
    results = []
    reader = threading.Thread(target=lambda: results.append(stream.capture_stream_frame()))
    try:
        reader.start()
        reader.join(1)
        assert results == [None]
    finally:
        win32event.ReleaseMutex(stream._mutex)
        reader.join(1)
    bridge = ResonancePcInputBridgeService(config={})
    bridge._active = True
    bridge._frame_stream = stream
    bridge._session_root_cid = '-'
    assert bridge.open_frame_stream().capture_stream_frame()['generation'] == 1
    publish(stream, 2)
    frame = bridge.open_frame_stream().capture_stream_frame(after_generation=1)
    assert source_key(dict(stream_metadata(frame), frame_id=0, map_revision=0))


def test_runtime_roi_and_pixel_reads_use_the_same_rgb_coordinates(stream, monkeypatch):
    config = {'resonance_pc.input.mode': 'bridge'}
    monkeypatch.delenv('AURA_GUI_INPUT_BRIDGE', raising=False)
    bridge = ResonancePcInputBridgeService(config=config)
    runtime = ResonancePcTargetRuntimeService(config, bridge)
    bridge._viewport = {'width': WIDTH, 'height': HEIGHT}
    bridge._frame_stream = stream
    bridge._session_root_cid = '-'
    monkeypatch.setattr(bridge, 'viewport', lambda: (0, 0, WIDTH, HEIGHT))
    poll = stream.capture_stream_frame
    def refresh(*args, **kwargs):
        time.sleep(.032)
        publish(stream, 1 + max(0, kwargs.get('after_generation', -1)))
        return poll(*args, **kwargs)
    monkeypatch.setattr(stream, 'capture_stream_frame', refresh)
    monkeypatch.setattr(runtime, '_build_session', lambda *_: pytest.fail('desktop adapter started'))
    token = current_plan_name.set('resonance_pc')
    try:
        assert runtime.get_client_rect() == (0, 0, WIDTH, HEIGHT)
        area = runtime.capture((16, 0, 2, 1))
        assert area.relative_rect == (16, 0, 2, 1) and area.image_size == (2, 1)
        assert tuple(area.image[0, 1]) == (2, 7, 11)
        assert runtime.get_pixel_color_at(17, 0) == (2, 7, 11)
        with pytest.raises(TargetRuntimeError) as invalid:
            runtime.capture((WIDTH, 0, 1, 1))
        assert invalid.value.code == 'capture_rect_invalid'
    finally:
        current_plan_name.reset(token)


def test_resolution_preflight_preserves_the_restart_hint_for_an_old_bridge(monkeypatch):
    config = {'resonance_pc.input.mode': 'bridge'}
    bridge = ResonancePcInputBridgeService(config=config)
    runtime = ResonancePcTargetRuntimeService(config, bridge)
    app = ResonancePcAppService(SimpleNamespace(target_runtime=runtime), SimpleNamespace(bridge=bridge))
    monkeypatch.setenv('AURA_GUI_INPUT_BRIDGE', '1')
    monkeypatch.setattr(bridge, '_bridge_target', lambda: (222, 333))
    monkeypatch.setattr(input_bridge_service.win32gui, 'IsIconic', lambda _: True)

    def request(op, **_):
        assert op == 'status'
        return {'result': {'state': 'ready', 'capabilities': {'absolute_pointer': True}}}

    monkeypatch.setattr(input_bridge_service, 'BridgeClient', lambda _: SimpleNamespace(uncertain=False, request=request))
    token = current_plan_name.set('resonance_pc')
    try:
        with pytest.raises(TargetRuntimeError, match='重启雷索纳斯') as rejected:
            resonance_pc_require_client_resolution(app=app)
        assert rejected.value.code == 'bridge_restart_required'
        assert bridge._client is None and runtime._session is None
    finally:
        current_plan_name.reset(token)


@pytest.mark.parametrize('terminal_status', ['success', 'cancelled', 'failed'])
def test_root_release_orders_stream_close_and_ignores_earlier_task(stream, terminal_status):
    from packages.aura_game.runner import EmbeddedGameRunner

    bridge = ResonancePcInputBridgeService(config={})
    bridge._frame_stream = stream
    bridge._session_root_cid = 'active'
    pending = True
    requests = []
    def request(op, **_):
        with pytest.raises(TargetRuntimeError) as closed:
            stream.capture_stream_frame()
        assert closed.value.code == 'capture_stream_closed'
        assert not stream._closed
        requests.append(op)
    bridge._client = SimpleNamespace(request=request)
    runner = EmbeddedGameRunner()
    runner.get_run = lambda _: {'plan_name': 'resonance_pc', 'status': terminal_status,
                                 'execution_pending': pending}
    with patch('packages.aura_game.runner.service_registry.get_service_instance', return_value=bridge):
        assert runner.close_task_input('active') is False
        pending = False
        assert runner.close_task_input('old') is True
        assert requests == []
        assert runner.close_task_input('active') is True
    assert requests == ['stream_stop', 'close']
    assert stream._closed and bridge._frame_stream is None and bridge._client is None
