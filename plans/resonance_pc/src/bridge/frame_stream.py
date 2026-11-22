"""Read the native frame-end buffer without entering the bridge input queue."""
from __future__ import annotations

import ctypes
import mmap
import os
import struct
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

import numpy as np
import win32event

from ....aura_base.src.platform.contracts import CaptureResult, TargetRuntimeError

BACKEND = "unity_bridge"
WIDTH, HEIGHT = 1280, 720
_HEADER = struct.Struct("<IIIIQQQ")
_PIXEL_BYTES = WIDTH * HEIGHT * 4
_FRAME_BYTES = _HEADER.size + _PIXEL_BYTES
_DIAGNOSTIC_TRAILER = struct.Struct("<8Q")
_DIAGNOSTIC_SEQUENCE = struct.Struct("<Q")


@dataclass(frozen=True)
class _FrameSnapshot:
    generation: int
    timestamp: float
    unity_frame: int
    pixels: bytes


@dataclass
class _CaptureWait:
    started_at: float
    initial_diagnostics: dict
    stale_returned: bool = False


class BridgeFrameStream:
    def __init__(self, session: str, *, max_stale_ms: int, target_key=None):
        self.session_id = uuid4().int
        self.target_key = target_key
        self._max_age = max_stale_ms / 1000
        self._lock = threading.Lock()
        self._closed = False
        self._accepting_reads = True
        self._latest: _FrameSnapshot | None = None
        self._capture_wait: _CaptureWait | None = None
        self._invalidated_at = float("-inf")
        kernel = ctypes.windll.kernel32
        frequency, tick = ctypes.c_int64(), ctypes.c_int64()
        kernel.QueryPerformanceFrequency(ctypes.byref(frequency))
        monotonic = time.monotonic()
        kernel.QueryPerformanceCounter(ctypes.byref(tick))
        self._frequency = frequency.value
        # Python on Windows can use GetTickCount64 for monotonic(), which has
        # a coarser clock than QPC. Bias toward an earlier capture timestamp.
        self._clock_origin = monotonic - tick.value / self._frequency - time.get_clock_info("monotonic").resolution
        name = f"Local\\AuraResonanceFrame-{os.getpid()}-{session}"
        self._mutex = win32event.CreateMutex(None, False, name + "-lock")
        try:
            self._buffer = mmap.mmap(-1, _FRAME_BYTES + _DIAGNOSTIC_TRAILER.size, tagname=name)
        except BaseException:
            self._mutex.Close()
            raise

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._latest = self._capture_wait = None
                self._buffer.close()
                self._mutex.Close()

    def stop_reading(self):
        with self._lock:
            self._accepting_reads = False
            self._latest = self._capture_wait = None

    def invalidate_cache(self):
        with self._lock:
            self._latest = self._capture_wait = None
            self._invalidated_at = time.monotonic()

    def begin_capture_wait(self, requested_at: float, diagnostics: dict) -> _CaptureWait:
        with self._lock:
            if self._closed or not self._accepting_reads:
                raise TargetRuntimeError("capture_stream_closed", "Bridge frame session is closed")
            if self._capture_wait is None:
                self._capture_wait = _CaptureWait(requested_at, diagnostics)
            return self._capture_wait

    def end_capture_wait(self, wait):
        with self._lock:
            if self._capture_wait is wait:
                self._capture_wait = None

    def capture_stream_frame(self, after_generation=-1, *, expected_session_id=None, allow_stale=False):
        if expected_session_id is not None and expected_session_id != self.session_id:
            raise TargetRuntimeError("capture_stream_replaced", "Bridge frame session changed")
        if not self._lock.acquire(blocking=False):
            return None
        try:
            if self._closed or not self._accepting_reads:
                raise TargetRuntimeError("capture_stream_closed", "Bridge frame session is closed")
            acquired = win32event.WaitForSingleObject(self._mutex, 0)
            if acquired == win32event.WAIT_TIMEOUT:
                snapshot = self._latest if allow_stale else None
            elif acquired == win32event.WAIT_ABANDONED:
                win32event.ReleaseMutex(self._mutex)
                self._latest = None
                raise TargetRuntimeError("bridge_capture_failed", "Game abandoned the frame buffer")
            elif acquired != win32event.WAIT_OBJECT_0:
                self._latest = None
                raise TargetRuntimeError("bridge_capture_failed", "Cannot lock the frame buffer")
            else:
                try:
                    snapshot = self._read_snapshot(after_generation, allow_stale)
                finally:
                    win32event.ReleaseMutex(self._mutex)
        finally:
            self._lock.release()
        if snapshot is None or snapshot.generation <= after_generation:
            return None
        rgb = np.frombuffer(snapshot.pixels, dtype=np.uint8).reshape(HEIGHT, WIDTH, 4)[::-1, :, :3].copy()
        client_rect = (0, 0, WIDTH, HEIGHT)
        result = CaptureResult(
            True, rgb, client_rect, client_rect, BACKEND,
            frame_id=f"{self.session_id}:{snapshot.generation}", frame_generation=snapshot.generation,
            captured_at_monotonic=snapshot.timestamp, stale=allow_stale,
        )
        return {"capture": result, "generation": snapshot.generation,
                "arrived_at_monotonic": snapshot.timestamp, "session_id": self.session_id,
                "unity_frame": snapshot.unity_frame}

    def _read_snapshot(self, after_generation, allow_stale):
        state, width, height, size, generation, tick, unity_frame = _HEADER.unpack_from(self._buffer)
        if state != 1 or (width, height, size) != (WIDTH, HEIGHT, _PIXEL_BYTES):
            self._latest = None
            if state == 0:
                return None
            if state == 2:
                raise TargetRuntimeError("viewport_changed", "Game window or Unity render size changed")
            if state == 3:
                raise TargetRuntimeError("bridge_capture_failed", "Unity could not read a rendered frame")
            raise TargetRuntimeError("bridge_capture_invalid", "Invalid bridge frame header")
        timestamp = self._clock_origin + tick / self._frequency
        if (generation <= after_generation or timestamp <= self._invalidated_at
                or (not allow_stale and time.monotonic() - timestamp > self._max_age)):
            return None
        if self._latest is None or (self._latest.generation, self._latest.timestamp) != (generation, timestamp):
            self._latest = _FrameSnapshot(generation, timestamp, unity_frame, self._buffer[_HEADER.size:_FRAME_BYTES])
        return self._latest

    def diagnostic_snapshot(self) -> dict:
        """Sample counters and timing without copying pixels or waiting on the writer."""
        if not self._lock.acquire(blocking=False):
            return {}
        try:
            if self._closed:
                return {}
            for _ in range(3):
                values = _DIAGNOSTIC_TRAILER.unpack_from(self._buffer, _FRAME_BYTES)
                sequence, attempts, started, finished, duration, generation, frame_tick, unity_frame = values
                if sequence == 0:
                    return self._legacy_diagnostic_snapshot()
                confirmed, = _DIAGNOSTIC_SEQUENCE.unpack_from(self._buffer, _FRAME_BYTES)
                if sequence == confirmed and sequence % 2 == 0:
                    now = self._query_clock()
                    return {
                        "generation": generation,
                        "last_unity_frame": unity_frame if generation else None,
                        "last_frame_age_ms": self._elapsed_ms(now - frame_tick) if frame_tick else None,
                        "native_capture_attempts": attempts,
                        "last_native_capture_ms": self._elapsed_ms(duration) if finished else None,
                        "native_capture_in_progress_ms": self._elapsed_ms(now - started) if started > finished else None,
                    }
            return {}
        finally:
            self._lock.release()

    def _legacy_diagnostic_snapshot(self) -> dict:
        acquired = win32event.WaitForSingleObject(self._mutex, 0)
        if acquired != win32event.WAIT_OBJECT_0:
            if acquired == win32event.WAIT_ABANDONED:
                win32event.ReleaseMutex(self._mutex)
            return {}
        try:
            _, _, _, _, generation, frame_tick, unity_frame = _HEADER.unpack_from(self._buffer)
        finally:
            win32event.ReleaseMutex(self._mutex)
        return {"generation": generation, "last_unity_frame": unity_frame if generation else None,
                "last_frame_age_ms": self._elapsed_ms(self._query_clock() - frame_tick) if frame_tick else None}

    @staticmethod
    def _query_clock() -> int:
        tick = ctypes.c_int64()
        ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(tick))
        return tick.value

    def _elapsed_ms(self, ticks: int) -> float:
        return round(max(0, ticks) * 1000 / self._frequency, 3)
