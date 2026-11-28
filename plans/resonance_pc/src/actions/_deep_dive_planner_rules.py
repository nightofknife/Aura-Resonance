"""Pure Deep Dive rules in the scanner's logical coordinates.

Slots describe space, not persistent tiles. A layer rotation moves all attached
contents with the same permutation. Ordinary moves never cross a face. Random
boss choices are uniform, as explicitly assumed by the planning model.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from copy import deepcopy
from functools import lru_cache
import itertools
from numbers import Integral

import numpy as np

FACES = ("U", "R", "F", "D", "L", "B")
BASES = {
    "U": ((0, -1, 0), (1, 0, 0), (0, 0, -1)),
    "R": ((1, 0, 0), (0, 0, 1), (0, 1, 0)),
    "F": ((0, 0, -1), (1, 0, 0), (0, 1, 0)),
    "D": ((0, 1, 0), (1, 0, 0), (0, 0, 1)),
    "L": ((-1, 0, 0), (0, 0, -1), (0, 1, 0)),
    "B": ((0, 0, 1), (-1, 0, 0), (0, 1, 0)),
}
EMPTY_INSPIRATION = 54
RULES_VERSION = "cube3-uniform-boss-v2"
SUPPORTED_CUBE_SIZES = (3, 4, 5)


def validate_cube_size(cube_size: int) -> int:
    if (not isinstance(cube_size, Integral) or isinstance(cube_size, bool)
            or cube_size not in SUPPORTED_CUBE_SIZES):
        raise ValueError("Cube order must be 3, 4, or 5")
    return int(cube_size)


def validate_move_distance(move_distance: int) -> int:
    if (not isinstance(move_distance, Integral) or isinstance(move_distance, bool)
            or move_distance < 1):
        raise ValueError("Maximum move distance must be a positive integer")
    return int(move_distance)


def rules_version(cube_size: int = 3, *, move_distance: int = 1) -> str:
    size = validate_cube_size(cube_size)
    distance = validate_move_distance(move_distance)
    return f"cube{size}-uniform-boss-v2" + (f"-move{distance}" if distance > 1 else "")


@dataclass(frozen=True)
class Cell:
    face: str
    row: int
    col: int


def cell_to_slot(cell: Cell | dict | tuple, cube_size: int = 3) -> int:
    size = validate_cube_size(cube_size)
    if isinstance(cell, dict):
        cell = Cell(cell.get("face", cell.get("face_id")), cell["row"], cell["col"])
    elif isinstance(cell, tuple):
        cell = Cell(*cell)
    if not isinstance(cell, Cell) or cell.face not in FACES:
        raise ValueError("Cell requires a face in U/R/F/D/L/B")
    if (not isinstance(cell.row, Integral) or isinstance(cell.row, bool)
            or not isinstance(cell.col, Integral) or isinstance(cell.col, bool)
            or not 0 <= cell.row < size or not 0 <= cell.col < size):
        raise ValueError(f"Row and column must be integers in [0, {size - 1}]")
    return FACES.index(cell.face) * size**2 + int(cell.row) * size + int(cell.col)


def validate_slot(slot: int, cube_size: int = 3) -> int:
    slot_count = len(FACES) * validate_cube_size(cube_size)**2
    if not isinstance(slot, Integral) or isinstance(slot, bool) or not 0 <= slot < slot_count:
        raise ValueError(f"Slot must be an integer in [0, {slot_count - 1}]")
    return int(slot)


def slot_to_cell(slot: int, cube_size: int = 3) -> Cell:
    slot = validate_slot(slot, cube_size)
    size = int(cube_size)
    face, remainder = divmod(slot, size**2)
    row, col = divmod(remainder, size)
    return Cell(FACES[face], row, col)


def coord_dict(slot: int, cube_size: int = 3) -> dict:
    cell = slot_to_cell(slot, cube_size)
    return dict(slot=int(slot), face=cell.face, row=cell.row, col=cell.col)


def boss_move_mask(players: np.ndarray, destinations: np.ndarray) -> np.ndarray:
    """Keep only the collision when the player is a legal one-step destination."""
    collision = destinations == players[:, None]
    return (destinations >= 0) & (~collision.any(axis=1, keepdims=True) | collision)


@dataclass(frozen=True)
class Geometry:
    points: np.ndarray
    normals: np.ndarray
    rotations: np.ndarray
    actor_rotations: np.ndarray
    symmetries: np.ndarray
    moves: np.ndarray
    counts: np.ndarray
    boss_moves: np.ndarray
    boss_counts: np.ndarray
    rotation_specs: tuple[tuple[int, int, int], ...]


def geometry(cube_size: int = 3, *, move_distance: int = 1) -> Geometry:
    """Cached integer layer permutations; the slot-count sentinel is fixed."""
    size = validate_cube_size(cube_size)
    distance = min(validate_move_distance(move_distance), 2 * (size - 1))
    return _long_move_geometry(size, distance) if distance > 1 else _geometry(size)


def _move_slots(size: int, distance: int) -> tuple[np.ndarray, np.ndarray]:
    face_area = size**2
    slot_count = len(FACES) * face_area
    offsets = ((-1, 0), (1, 0), (0, -1), (0, 1))
    for steps in range(2, distance + 1):
        offsets += tuple((dr, dc) for dr in range(-steps, steps + 1)
                         for dc in range(-steps, steps + 1) if abs(dr) + abs(dc) == steps)
    moves = np.full((slot_count, len(offsets)), -1, dtype=np.int64)
    counts = np.zeros(slot_count, dtype=np.int64)
    for slot in range(slot_count):
        face, remainder = divmod(slot, face_area)
        row, col = divmod(remainder, size)
        for dr, dc in offsets:
            if 0 <= row + dr < size and 0 <= col + dc < size:
                moves[slot, counts[slot]] = face * face_area + (row + dr) * size + col + dc
                counts[slot] += 1
    moves.flags.writeable = counts.flags.writeable = False
    return moves, counts


@lru_cache(maxsize=sum(2 * (size - 1) - 1 for size in SUPPORTED_CUBE_SIZES))
def _long_move_geometry(size: int, distance: int) -> Geometry:
    base = _geometry(size)
    moves, counts = _move_slots(size, distance)
    return replace(base, moves=moves, counts=counts)


@lru_cache(maxsize=len(SUPPORTED_CUBE_SIZES))
def _geometry(size: int) -> Geometry:
    face_area = size**2
    slot_count = len(FACES) * face_area
    # Even orders use twice the centered coordinate so every layer is integral.
    layers = tuple(range(-(size // 2), size // 2 + 1)) if size % 2 else tuple(range(1 - size, size, 2))
    bases = np.asarray([BASES[face] for face in FACES], dtype=np.int64)
    points = np.asarray([normal * layers[-1] + u * col + v * row
                         for normal, u, v in bases for row in layers for col in layers])
    normals = np.repeat(bases[:, 0], face_area, axis=0)
    radius = layers[-1]
    width = 2 * radius + 1

    def sticker_keys(positions, directions):
        x, y, z = (positions + radius).T
        a, b, c = (directions + 1).T
        return ((x * width + y) * width + z) * 27 + (a * 3 + b) * 3 + c

    inverse = np.full(width**3 * 27, -1, dtype=np.int64)
    inverse[sticker_keys(points, normals)] = np.arange(slot_count)
    specs = tuple((axis, layer, sign) for axis in range(3)
                  for layer in layers for sign in (-1, 1))
    ids = {spec: i for i, spec in enumerate(specs)}
    rotations = np.tile(np.arange(slot_count + 1, dtype=np.int64), (len(specs), 1))
    axes_basis = np.eye(3, dtype=np.int64)
    for rotation_id, (axis, layer, sign) in enumerate(specs):
        selected = np.flatnonzero(points[:, axis] == layer)
        layer_points, layer_normals = points[selected], normals[selected]
        unit = axes_basis[axis]
        moved = sign * np.cross(unit, layer_points) + layer_points[:, axis, None] * unit
        turned = sign * np.cross(unit, layer_normals) + layer_normals[:, axis, None] * unit
        rotations[rotation_id, selected] = inverse[sticker_keys(moved, turned)]
    actor = np.empty((slot_count, 4), dtype=np.int64)
    for face, face_basis in enumerate(bases):
        face_slots = slice(face * face_area, (face + 1) * face_area)
        for tangent_index, tangent in enumerate(face_basis[1:]):
            axis = int(np.flatnonzero(tangent)[0])
            for sign_index, sign in enumerate((-1, 1)):
                actor[face_slots, 2 * tangent_index + sign_index] = [
                    ids[axis, int(coordinate), sign] for coordinate in points[face_slots, axis]
                ]
    symmetries = []
    for axes in itertools.permutations(range(3)):
        for signs in itertools.product((-1, 1), repeat=3):
            matrix = np.zeros((3, 3), dtype=np.int64)
            for row, col in enumerate(axes):
                matrix[row, col] = signs[row]
            if round(np.linalg.det(matrix)) == 1:
                mapping = inverse[sticker_keys(points @ matrix.T, normals @ matrix.T)]
                symmetries.append(np.r_[mapping, slot_count])
    symmetries = np.asarray(symmetries, dtype=np.int64)
    moves, counts = _move_slots(size, 1)
    for array in (points, normals, rotations, actor, symmetries):
        array.flags.writeable = False
    return Geometry(points, normals, rotations, actor, symmetries, moves, counts, moves, counts, specs)


def move_dict(origin: int, destination: int, actor: str = "player", *, cube_size: int = 3) -> dict:
    return dict(kind="move", actor=actor, origin=coord_dict(origin, cube_size),
                destination=coord_dict(destination, cube_size))


def rotation_dict(rotation_id: int, actor_slot: int, actor: str = "player", *, cube_size: int = 3) -> dict:
    axis, layer, sign = geometry(cube_size).rotation_specs[rotation_id]
    action = dict(kind="rotate_layer", actor=actor, actor_cell=coord_dict(actor_slot, cube_size),
                  rotation_id=int(rotation_id), world_axis=("X", "Y", "Z")[axis],
                  world_axis_index=axis, layer=layer, sign=sign, angle_degrees=sign * 90,
                  sign_convention="right_hand_about_positive_world_axis")
    if cube_size % 2 == 0:
        action["layer_scale"] = 2
    return action


@dataclass(frozen=True)
class PlayerAction:
    final_player: int
    final_boss: int
    rotation_id: int | None
    pickup_original_slot: int
    sequence: tuple[dict, ...]
    terminal: bool


@lru_cache(maxsize=54 * 54)
def player_actions(player: int, boss: int, *, cube_size: int = 3,
                   move_distance: int = 1) -> tuple[PlayerAction, ...]:
    """Both orders for one move plus one rotation; collision stops immediately.

    pickup_original_slot is the tile's slot before either primitive. Thus callers
    remove inspiration there, then apply rotation_id to the remaining items.
    """
    player, boss = validate_slot(player, cube_size), validate_slot(boss, cube_size)
    geo = geometry(cube_size, move_distance=move_distance)
    if player == boss:
        return ()
    result = []
    for destination in geo.moves[player, :geo.counts[player]]:
        destination = int(destination)
        move = move_dict(player, destination, cube_size=cube_size)
        if destination == boss:
            result.append(PlayerAction(destination, boss, None, destination, (move,), True))
            continue
        for rotation_id in geo.actor_rotations[destination]:
            rotation_id = int(rotation_id)
            perm = geo.rotations[rotation_id]
            rotation = rotation_dict(rotation_id, destination, cube_size=cube_size)
            result.append(PlayerAction(int(perm[destination]), int(perm[boss]), rotation_id,
                                       destination, (move, rotation), False))
    for rotation_id in geo.actor_rotations[player]:
        rotation_id = int(rotation_id)
        perm = geo.rotations[rotation_id]
        rotated_player, rotated_boss = int(perm[player]), int(perm[boss])
        inverse = np.argsort(perm)
        rotation = rotation_dict(rotation_id, player, cube_size=cube_size)
        for destination in geo.moves[rotated_player, :geo.counts[rotated_player]]:
            destination = int(destination)
            result.append(PlayerAction(destination, rotated_boss, rotation_id, int(inverse[destination]),
                                       (rotation, move_dict(rotated_player, destination, cube_size=cube_size)),
                                       destination == rotated_boss))
    return tuple(result)


def remaining_player_actions(player: int, boss: int, moves_left: int = 1,
                             rotations_left: int = 1, *, cube_size: int = 3,
                             move_distance: int = 1) -> tuple[PlayerAction, ...]:
    """Actions still available in the current turn, without replenishing quotas.

    A rotation-only action never picks up an inspiration: its tile remains
    distinct from the player's tile under the same bijective permutation.
    """
    player, boss = validate_slot(player, cube_size), validate_slot(boss, cube_size)
    distance = validate_move_distance(move_distance)
    for name, quota in (("moves_left", moves_left), ("rotations_left", rotations_left)):
        if not isinstance(quota, Integral) or isinstance(quota, bool) or quota not in (0, 1):
            raise ValueError(f"{name} must be 0 or 1")
    if moves_left and rotations_left:
        return player_actions(player, boss, cube_size=cube_size, move_distance=distance)
    if player == boss or not (moves_left or rotations_left):
        return ()
    geo = geometry(cube_size, move_distance=distance)
    if moves_left:
        return tuple(PlayerAction(int(destination), boss, None, int(destination),
                                  (move_dict(player, int(destination), cube_size=cube_size),),
                                  int(destination) == boss)
                     for destination in geo.moves[player, :geo.counts[player]])
    return tuple(PlayerAction(int(geo.rotations[int(rotation_id), player]),
                              int(geo.rotations[int(rotation_id), boss]), int(rotation_id),
                              len(geo.moves), (rotation_dict(int(rotation_id), player, cube_size=cube_size),),
                              False)
                 for rotation_id in geo.actor_rotations[player])


@dataclass(frozen=True)
class BossOutcome:
    final_player: int
    final_boss: int
    rotation_id: int | None
    eat_original_slot: int
    probability: float
    terminal: bool
    sequence: tuple[dict, ...]


@lru_cache(maxsize=54 * 54)
def boss_outcomes(player: int, boss: int, *, cube_size: int = 3,
                  moves: int = 1, rotations: int = 1) -> tuple[BossOutcome, ...]:
    player, boss = validate_slot(player, cube_size), validate_slot(boss, cube_size)
    if player == boss:
        return (BossOutcome(player, boss, None, len(FACES) * cube_size**2, 1.0, True, ()),)
    geo = geometry(cube_size)
    destinations = tuple(map(int, geo.boss_moves[boss, :geo.boss_counts[boss]]))
    if not moves:
        destinations = (boss,)
    elif player in destinations:
        destinations = (player,)
    result = []
    for destination in destinations:
        sequence = (move_dict(boss, destination, "boss", cube_size=cube_size),) if moves else ()
        probability = 1.0 / len(destinations)
        if destination == player:
            result.append(BossOutcome(player, destination, None, len(geo.moves),
                                      probability, True, sequence))
            continue
        consumed = destination if moves else len(geo.moves)
        if not rotations:
            result.append(BossOutcome(player, destination, None, consumed, probability, False, sequence))
            continue
        for rotation_id in geo.actor_rotations[destination]:
            rotation_id = int(rotation_id)
            perm = geo.rotations[rotation_id]
            rotation = rotation_dict(rotation_id, destination, "boss", cube_size=cube_size)
            result.append(BossOutcome(int(perm[player]), int(perm[destination]), rotation_id,
                                      consumed, probability / 4, False, (*sequence, rotation)))
    return tuple(result)


def action_dict(action: PlayerAction, *, cube_size: int = 3) -> dict:
    return dict(sequence=deepcopy(list(action.sequence)), terminal=action.terminal,
                after_player=dict(player=coord_dict(action.final_player, cube_size),
                                  boss=coord_dict(action.final_boss, cube_size)),
                rotation_id=action.rotation_id, pickup_original_slot=action.pickup_original_slot)


def boss_branch_dict(outcome: BossOutcome, *, cube_size: int = 3) -> dict:
    return dict(probability=outcome.probability, terminal=outcome.terminal,
                player=coord_dict(outcome.final_player, cube_size), boss=coord_dict(outcome.final_boss, cube_size),
                rotation_id=outcome.rotation_id, eat_original_slot=outcome.eat_original_slot,
                sequence=deepcopy(list(outcome.sequence)))
