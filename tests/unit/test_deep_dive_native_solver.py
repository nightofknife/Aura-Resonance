from threading import Event
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import _deep_dive_bounded_inspiration as bounded
from plans.resonance_pc.src.actions import _deep_dive_inspiration_planner as planner
from plans.resonance_pc.src.actions._deep_dive_native_solver import get_native_solver


@pytest.fixture
def native():
    solver = get_native_solver()
    if solver is None:
        pytest.skip("Build the Windows AVX2 Deep Dive solver to run native parity tests")
    return solver


def result_values(result):
    return {key: result[key] for key in
            ("status", "next_action", "player_turn_actions", "boss_branches", "metrics")}


def test_native_sparse_plan_matches_numpy_and_keeps_caches_separate(native, monkeypatch):
    inputs = dict(cube_size=4, encounter_first=True, time_budget_sec=30)
    with monkeypatch.context() as patch:
        patch.setattr(planner, "get_native_solver", lambda: None)
        reference = planner.plan_inspiration(5, 20, (32, 49), 5, **inputs)
    observed = planner.plan_inspiration(5, 20, (32, 49), 5, **inputs)
    assert reference["solver"]["backend"] == "numpy"
    assert observed["solver"]["backend"] == "native"
    assert result_values(observed) == result_values(reference)
    repeated = planner.plan_inspiration(5, 20, (32, 49), 5, **inputs)
    assert repeated["solver"]["reused_future_rounds"] >= 2
    assert result_values(repeated) == result_values(reference)


def test_nonadjacent_enemy_walk_changes_two_round_encounter_odds(native, monkeypatch):
    args = (58, 87, (33, 39), 2)
    inputs = dict(cube_size=4, moves_left=0, rotations_left=1, encounter_first=True)
    observed = planner.plan_inspiration(*args, **inputs)
    monkeypatch.setattr(planner, "get_native_solver", lambda: None)
    reference = planner.plan_inspiration(*args, **inputs)

    assert observed["solver"]["backend"] == "native"
    assert reference["solver"]["backend"] == "numpy"
    assert observed["metrics"]["deadline_encounter_probability"] == pytest.approx(5 / 12)
    assert result_values(observed) == result_values(reference)


@pytest.mark.parametrize("distance,destination", [(2, 6), (3, 7)])
def test_native_five_order_uses_the_extended_same_face_move_range(native, monkeypatch, distance, destination):
    inputs = dict(cube_size=5, move_distance=distance, rotations_left=0, encounter_first=True)
    args = (0, destination, (30,), 3)
    observed = planner.plan_inspiration(*args, **inputs)
    monkeypatch.setattr(planner, "get_native_solver", lambda: None)
    reference = planner.plan_inspiration(*args, **inputs)

    assert observed["solver"]["backend"] == "native"
    assert observed["next_action"]["kind"] == "move"
    target = observed["next_action"]["destination"]
    assert target["face"] == "U" and target["row"] + target["col"] == distance
    assert result_values(observed) == result_values(reference)


@pytest.mark.parametrize("turns,expected_backend", [(11, "native"), (12, "numpy")])
def test_native_bounded_deadline_matches_exact_python_integer_arithmetic(native, turns, expected_backend, monkeypatch):
    args = (6, 30, (50, 76, 100, 126), turns)
    inputs = dict(cube_size=5, encounter_first=True, time_budget_sec=30)
    observed = planner.plan_inspiration(*args, **inputs)
    monkeypatch.setattr(bounded, "get_native_solver", lambda: None)
    reference = planner.plan_inspiration(*args, **inputs)
    assert observed["solver"]["backend"] == expected_backend
    assert result_values(observed) == result_values(reference)


def test_cancelled_native_step_does_not_publish_a_completed_layer(native, monkeypatch):
    cancelled = Event()
    original = native._step

    def step_and_cancel(*args):
        status = original(*args)
        cancelled.set()
        return status

    monkeypatch.setattr(planner, "_CONTINUATION_CACHE", planner.OrderedDict())
    with monkeypatch.context() as patch:
        patch.setattr(native, "_step", step_and_cancel)
        with pytest.raises(planner.PlanningCancelled):
            planner.plan_inspiration(4, 13, (18, 28), 3, cancel_check=cancelled.is_set, time_budget_sec=30)
        assert cancelled.is_set()
        assert not planner._CONTINUATION_CACHE
    result = planner.plan_inspiration(4, 13, (18, 28), 3, time_budget_sec=30)
    assert result["status"] == "solved"
    assert result["solver"]["backend"] == "native"


def test_native_step_checks_deadline_before_publishing_a_layer(native, monkeypatch):
    now = 0.0
    original = native._step

    def step_and_expire(*args):
        nonlocal now
        status = original(*args)
        now = 2.0
        return status

    monkeypatch.setattr(planner, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(planner, "_CONTINUATION_CACHE", planner.OrderedDict())
    monkeypatch.setattr(native, "_step", step_and_expire)
    with pytest.raises(planner.PlanningBudgetExceeded):
        planner.plan_inspiration(4, 13, (18, 28), 3, time_budget_sec=1)
    assert now == 2.0
    assert not planner._CONTINUATION_CACHE
