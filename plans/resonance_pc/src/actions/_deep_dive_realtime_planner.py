"""Deadline pursuit with actual player quotas and a bounded reward search."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import time

import numpy as np

from ._deep_dive_native_solver import get_native_solver
from ._deep_dive_planner_rules import boss_move_mask, boss_outcomes, coord_dict, geometry, move_dict, rotation_dict


REALTIME_BUDGET_SEC = 0.350
_MAX_SEARCH_QUOTA = 12
_REWARD_BEAM_WIDTH = 16
_PROBABILITY_TOLERANCE = 1e-12


@dataclass(frozen=True)
class _PairGraph:
    ids: np.ndarray
    move: np.ndarray
    rotate: np.ndarray
    boss_move: np.ndarray
    boss_rotate: np.ndarray
    boss_degree: np.ndarray


@lru_cache(maxsize=12)
def _pair_graph(size: int, distance: int) -> _PairGraph:
    geo = geometry(size, move_distance=distance)
    slots = len(geo.moves)
    group = geo.symmetries[:, :slots]
    canonical = (group[:, :, None] * slots + group[:, None, :]).min(axis=0)
    mask = ~np.eye(slots, dtype=bool)
    pairs, inverse = np.unique(canonical[mask], return_inverse=True)
    player, boss = np.divmod(pairs, slots)
    rows = len(pairs)
    ids = np.full((slots, slots), rows, dtype=np.int32)
    ids[mask] = inverse
    move = geo.moves[player, :int(geo.counts.max())]
    index = np.arange(rows)[:, None]
    rotation_column = np.arange(4)

    def rotation_edges(actor):
        permutations = geo.rotations[geo.actor_rotations[actor]]
        return ids[permutations[index, rotation_column, player[:, None]],
                   permutations[index, rotation_column, boss[:, None]]]

    move_edges = ids[move, boss[:, None]]
    move_edges[move < 0] = rows + 1
    relative_moves = geo.boss_moves[boss]

    legal = boss_move_mask(player, relative_moves)
    boss_move_edges = ids[player[:, None], relative_moves]
    boss_move_edges[~legal] = rows + 1
    return _PairGraph(ids, move_edges, rotation_edges(player), boss_move_edges, rotation_edges(boss),
                      legal.sum(axis=1))


def _player_values(graph, continuation, moves, rotations, enemy, native, checkpoint):
    rows, columns = graph.move.shape
    values = np.empty((moves + 1, rotations + 1, rows + 2))
    values[0, 0] = continuation
    for enabled, edges, divisor in ((enemy[1], graph.boss_rotate, 4),
                                    (enemy[0], graph.boss_move, graph.boss_degree)):
        if enabled:
            values[0, 0, :rows] = values[0, 0, edges].sum(axis=1) / divisor
    checkpoint()
    if native:
        native.player_quota_values(graph.move.ctypes.data, graph.rotate.ctypes.data, rows, columns,
                                   moves, rotations, values.ctypes.data)
    else:
        values[..., rows:] = (1., 0.)
        for m in range(moves + 1):
            for r in range(rotations + 1):
                if not (m or r):
                    continue
                checkpoint()
                output = values[m, r, :rows]
                output[:] = values[m - 1, r, graph.move].max(axis=1) if m else 0.
                if r:
                    np.maximum(output, values[m, r - 1, graph.rotate].max(axis=1), out=output)
    checkpoint()
    return values


@dataclass(frozen=True)
class _Route:
    player: int
    boss: int
    free: tuple[int, ...]
    moves: int
    rotations: int
    sequence: tuple[tuple[str, int, int], ...] = ()


def _rewards(start, graph, values, geo, future_geo, finish_by, checkpoint):
    """Keep viable probability-optimal prefixes while ranking current-turn pickups."""
    @lru_cache(maxsize=None)
    def reachable_slots(player, current):
        model = geo if current else future_geo
        return frozenset(model.moves[player, :model.counts[player]])

    frontier = [start]
    for _ in range(start.moves + start.rotations):
        ranked = {}
        for route in frontier:
            checkpoint()
            if time.perf_counter() >= finish_by and route.sequence:
                return frontier[0]
            p, b, free, m, r, sequence = (route.player, route.boss, route.free,
                                          route.moves, route.rotations, route.sequence)
            desired = values[m, r, graph.ids[p, b]]
            candidates = []
            if p == b or not (m or r):
                candidates.append(route)
            else:
                if m:
                    for target in geo.moves[p, :geo.counts[p]]:
                        target = int(target)
                        if values[m - 1, r, graph.ids[target, b]] >= desired - _PROBABILITY_TOLERANCE:
                            remaining = tuple(slot for slot in free if slot != target)
                            candidates.append(_Route(target, b, remaining, m - 1, r,
                                                     (*sequence, ("move", p, target))))
                if r:
                    for rotation in geo.actor_rotations[p]:
                        rotation = int(rotation)
                        perm = geo.rotations[rotation]
                        new_p, new_b = int(perm[p]), int(perm[b])
                        if values[m, r - 1, graph.ids[new_p, new_b]] >= desired - _PROBABILITY_TOLERANCE:
                            remaining = tuple(sorted(int(perm[slot]) for slot in free))
                            candidates.append(_Route(new_p, new_b, remaining, m, r - 1,
                                                     (*sequence, ("rotate", p, rotation))))
            for candidate in candidates:
                cp, cb, items = candidate.player, candidate.boss, candidate.free
                key = (cp, cb, items, candidate.moves, candidate.rotations)
                if key not in ranked:
                    reachable = cp != cb and not reachable_slots(cp, bool(candidate.moves)).isdisjoint(items)
                    ranked[key] = ((-len(items), cp == cb, reachable, -len(candidate.sequence)), candidate)
        if not ranked:
            break
        frontier = [candidate for _, candidate in sorted(ranked.values(), key=lambda item: item[0], reverse=True)
                    [:_REWARD_BEAM_WIDTH]]
    return frontier[0]


def solve_realtime(player, boss, inspirations, rounds, moves, rotations, collected, *,
                   size, move_distance, future_distance, future_quotas, enemy_quotas, finish_by, checkpoint):
    graph = _pair_graph(size, move_distance)
    future = _pair_graph(size, future_distance)
    native = get_native_solver()
    search_moves, search_rotations = min(moves, _MAX_SEARCH_QUOTA), min(rotations, _MAX_SEARCH_QUOTA)
    search_future = tuple(min(count, _MAX_SEARCH_QUOTA) for count in future_quotas)
    rows = len(graph.move)
    continuation = np.zeros(rows + 2)
    continuation[rows] = 1.
    completed = 0
    # Reserve time for solving and reconstructing the current decision. An
    # interrupted horizon reports the probability only for the completed depth.
    search_limit = finish_by - (0.040 if max(search_moves, search_rotations) > 2 else 0.020)
    for _ in range(rounds - 1):
        if time.perf_counter() >= search_limit:
            break
        continuation = _player_values(future, continuation, *search_future, (1, 1),
                                       native, checkpoint)[search_future]
        completed += 1
        if continuation[:rows].min() >= 1.:
            completed = rounds - 1
            break
    values = _player_values(graph, continuation, search_moves, search_rotations, enemy_quotas, native, checkpoint)
    route = _rewards(_Route(player, boss, inspirations, search_moves, search_rotations), graph, values,
                     geometry(size, move_distance=move_distance), geometry(size, move_distance=future_distance),
                     finish_by, checkpoint)
    sequence = [move_dict(origin, target, cube_size=size) if kind == "move"
                else rotation_dict(target, origin, cube_size=size) for kind, origin, target in route.sequence]
    terminal = route.player == route.boss
    gain = len(inspirations) - len(route.free)
    branches = []
    if not terminal and not (route.moves or route.rotations) and (moves, rotations) == (search_moves, search_rotations):
        geo = geometry(size)
        for outcome in boss_outcomes(route.player, route.boss, cube_size=size,
                                     moves=enemy_quotas[0], rotations=enemy_quotas[1]):
            survivors = [slot for slot in route.free if slot != outcome.eat_original_slot]
            if outcome.rotation_id is not None:
                survivors = sorted(int(geo.rotations[outcome.rotation_id, slot]) for slot in survivors)
            branches.append(dict(probability=outcome.probability, encounter=outcome.terminal,
                                 boss_actions=list(outcome.sequence),
                                 player_cell=coord_dict(outcome.final_player, size),
                                 boss_cell=coord_dict(outcome.final_boss, size),
                                 inspiration_cells=[coord_dict(slot, size) for slot in survivors],
                                 newly_consumed_inspirations=len(route.free) - len(survivors),
                                 collected_count=collected + gain, remaining_rounds=rounds - 1,
                                 continuation_deadline_probability=float(
                                     continuation[future.ids[outcome.final_player, outcome.final_boss]])))
    probability = float(values[search_moves, search_rotations, graph.ids[player, boss]])
    return dict(status="solved", success=True, next_action=sequence[0], player_turn_actions=sequence,
                player_turn_terminal=terminal, planned_player_pickup_count=gain, boss_branches=branches,
                inspiration_after_player_turn=[coord_dict(slot, size) for slot in route.free],
                objective="max_deadline_encounter_probability_then_current_turn_inspiration",
                metrics=dict(deadline_encounter_probability=probability),
                solver=dict(method="player_quota_dynamic_programming_with_current_turn_reward_search",
                            backend="native" if native else "numpy", state_count=rows,
                            completed_horizon=completed + 1, requested_horizon=rounds,
                            turn_actions_complete=terminal or (not (route.moves or route.rotations) and
                                                               (moves, rotations) == (search_moves, search_rotations)),
                            future_player_quotas=list(future_quotas), searched_player_quotas=[search_moves, search_rotations],
                            quota_search_truncated=search_future != future_quotas or
                            (moves, rotations) != (search_moves, search_rotations),
                            enemy_quotas=list(enemy_quotas), reward_lookahead_turns=1))
