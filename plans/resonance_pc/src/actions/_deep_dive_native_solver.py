"""Windows implementation of the hot Deep Dive solving kernels."""

from __future__ import annotations

import ctypes
from functools import lru_cache
import os
from pathlib import Path
import sys

import numpy as np


_ASSET = Path(__file__).resolve().parents[2] / "assets" / "deep_dive_solver" / "aura_deep_dive_solver.dll"
_ABI = 3
_PF_AVX2_INSTRUCTIONS_AVAILABLE = 40
_POINTER = ctypes.c_void_p
_INT = ctypes.c_int


def _addresses(*arrays):
    return [array.ctypes.data for array in arrays]


def _int32(*arrays):
    return [np.ascontiguousarray(array, dtype=np.int32) for array in arrays]


def _check_status(status, operation):
    if status < 0:
        raise MemoryError(f"Deep Dive {operation} ran out of native memory")
    if status:
        raise RuntimeError(f"Deep Dive {operation} rejected the state ({status})")


class NativeSolver:
    def __init__(self, path):
        library = ctypes.CDLL(os.fspath(path))
        library.aura_deep_dive_solver_abi.argtypes = []
        library.aura_deep_dive_solver_abi.restype = _INT
        if library.aura_deep_dive_solver_abi() != _ABI:
            raise OSError("Unsupported Deep Dive solver DLL ABI")
        self._index = self._bind(library.canonical_index, 5, 6, 6)
        self._transitions = self._bind(library.build_packed_transitions, 16, 7, 3)
        self._step = self._bind(library.step_packed, 6, 6, 2)
        self._live = self._bind(library.live_packed, 6, 2, 1, None)
        self._tail = self._bind(library.step_two_requested, 6, 6, 7)
        self._pair = self._bind(library.pair_deadline, 3, 4, 4, ctypes.c_int64)
        self.player_quota_values = self._bind(library.player_quota_values, 2, 4, 1, None)
        self._workers = min(16, os.cpu_count() or 1)

    @staticmethod
    def _bind(function, pointers, integers, outputs, result=_INT):
        function.argtypes = [_POINTER] * pointers + [_INT] * integers + [_POINTER] * outputs
        function.restype = result
        return function

    def canonical_ids(self, representatives, first, second, group, oriented, checkpoint):
        slots = group.shape[1] - 1
        placements = len(first)
        capacity = len(representatives) * slots * placements
        identity = np.flatnonzero(np.all(group == np.arange(slots + 1), axis=1)).item()
        inputs = _int32(representatives, first, second, group, oriented[:, first, second])
        pieces = [np.empty(capacity, dtype=np.int32) for _ in range(4)]
        free = np.empty(capacity, dtype=np.int8)
        ids = np.empty((len(representatives), slots, placements + 1), dtype=np.int32)
        checkpoint()
        count = self._index(*_addresses(*inputs), len(representatives), slots, placements,
                            len(group), identity, min(self._workers, 12) if capacity >= 100_000 else 1,
                            *_addresses(*pieces, free, ids))
        checkpoint()
        if not 0 < count <= capacity:
            _check_status(count or 1, "state indexing")
        return ids, *(array[:count] for array in pieces), free[:count]

    def transitions(self, cube_size, geo, index, players, bosses, first, second, checkpoint):
        slots, columns = geo.moves.shape
        group = geo.symmetries
        representatives = np.unique(group[:, :slots].min(axis=0))
        identity = np.flatnonzero(np.all(group == np.arange(slots + 1), axis=1)).item()
        canonical = np.ascontiguousarray(group[:, geo.rotations], dtype=np.uint8)
        inputs = _int32(players, bosses, first, second, representatives, geo.moves, geo.boss_moves,
                        geo.actor_rotations, geo.rotations, np.argsort(geo.rotations, axis=1), group,
                        index.player_frame, index.player_orbit, index.oriented_placements[identity],
                        index.state_ids)
        own = np.empty((len(players), 8 * columns), dtype=np.int32)
        boss = np.empty((len(players), 16), dtype=np.int32)
        weights = np.empty(len(players), dtype=np.float64)
        checkpoint()
        status = self._transitions(*_addresses(*inputs, canonical), len(players), len(representatives),
                                   cube_size, columns, index.state_ids.shape[2], len(geo.rotations),
                                   min(self._workers, 12) if len(players) >= 8192 else 1,
                                   *_addresses(own, boss, weights))
        checkpoint()
        _check_status(status, "transition generation")
        return own, boss, weights

    def step(self, previous, tables, limits, offsets, baseline, encounter_first, zero, checkpoint):
        lane_limits = np.asarray(limits, dtype=np.int32)
        padded_offsets = np.empty(len(offsets) + 1, dtype=np.int32)
        padded_offsets[:-1] = offsets
        padded_offsets[-1] = -1
        boss_values = np.empty_like(previous)
        current = np.empty_like(previous)
        tables_buffers = tables.own, tables.boss, tables.boss_weight
        checkpoint()
        status = self._step(*_addresses(previous, *tables_buffers, lane_limits, padded_offsets),
                            tables.own.shape[1], len(limits), baseline, int(encounter_first),
                            self._workers if len(previous) >= 8192 else 1, int(zero),
                            *_addresses(boss_values, current))
        checkpoint()
        _check_status(status, "Bellman update")
        return current

    def boss_values(self, previous, tables, rows, offsets, count, baseline, checkpoint):
        positions = np.ascontiguousarray(rows, dtype=np.int32)
        lanes = np.ascontiguousarray(np.broadcast_to(count, positions.shape), dtype=np.int32)
        out = np.empty((len(positions), 5), dtype=np.float64)
        checkpoint()
        self._live(*_addresses(previous, tables.boss, tables.boss_weight, positions, offsets, lanes),
                   len(positions), baseline, out.ctypes.data)
        checkpoint()
        return out

    def requested_values(self, previous, tables, offsets, needed, baseline, encounter_first,
                         layers, marks, values, checkpoint):
        checkpoint()
        status = self._tail(*_addresses(previous, tables.own, tables.boss, tables.boss_weight, offsets, needed),
                            tables.own.shape[1], len(offsets), baseline, int(encounter_first),
                            len(needed), min(self._workers, 12), *_addresses(*layers, *marks, values))
        checkpoint()
        _check_status(status, "on-demand Bellman update")

    def pair_deadline(self, tables, horizon, slot_count, checkpoint):
        rows, columns = tables.own_next.shape
        probability = np.empty(rows, dtype=np.int64)
        moment = np.empty_like(probability)
        tile_probability = np.empty((rows, slot_count + 1), dtype=np.int64)
        tile_moment = np.empty_like(tile_probability)
        outputs = probability, moment, tile_probability, tile_moment
        checkpoint()
        unit = self._pair(*_addresses(tables.own_next, tables.pickup_slots, tables.boss_next),
                          rows, columns, slot_count, horizon, *_addresses(*outputs))
        checkpoint()
        if unit <= 0:
            _check_status(unit or 1, "integer Bellman update")
        # The outer reward ranking also handles large collected counts.
        return *(array.astype(object) for array in outputs), unit


@lru_cache(maxsize=1)
def get_native_solver():
    if sys.platform != "win32" or ctypes.sizeof(_POINTER) != 8 or not _ASSET.is_file():
        return None
    try:
        if not ctypes.WinDLL("kernel32").IsProcessorFeaturePresent(_PF_AVX2_INSTRUCTIONS_AVAILABLE):
            return None
        return NativeSolver(_ASSET)
    except (OSError, AttributeError):
        # Other platforms and standalone source installs can use NumPy.
        return None
