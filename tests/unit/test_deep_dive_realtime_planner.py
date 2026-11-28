import asyncio
from functools import lru_cache
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from plans.resonance_pc.src.actions import _deep_dive_movement_planner as planner
from plans.resonance_pc.src.actions import _deep_dive_realtime_planner as realtime
from plans.resonance_pc.src.actions import consciousness_deep_dive_planned_run_pc_actions as run
from plans.resonance_pc.src.actions._deep_dive_planner_rules import boss_outcomes, coord_dict, geometry


def layout(size, player, boss, items=(), distance=1, **turn):
    occupants = {player: "player", boss: "singularity", **dict.fromkeys(items, "inspiration")}
    return dict(schema="resonance_pc.deep_dive_layout.v1", coordinate_frame="native_board",
                size=size, status="completed", layout_complete=True, success=True, move_distance=distance,
                native_board={"turn": turn}, cells=[
                    dict(coord_dict(i, size), occupant=occupants.get(i, "none"),
                         occupant_status="confirmed", node_status="known")
                    for i in range(6 * size**2)
                ])


def test_second_move_is_needed_when_enemy_cannot_act():
    board = layout(3, 0, 2, (1,), boss_moves=0, boss_rotations=0)
    one = planner.plan_current_state(board, planner.ENCOUNTER_THEN_INSPIRATION,
                                     1, 1, 0, realtime=True)
    two = planner.plan_current_state(board, planner.ENCOUNTER_THEN_INSPIRATION,
                                     1, 2, 0, realtime=True)

    assert one["metrics"]["deadline_encounter_probability"] == 0
    assert one["boss_branches"][0]["boss_actions"] == []
    assert two["metrics"]["deadline_encounter_probability"] == 1
    assert [action["destination"]["slot"] for action in two["player_turn_actions"]] == [1, 2]
    assert two["planned_player_pickup_count"] == 1
    assert two["player_turn_terminal"]


def test_adjacent_enemy_always_encounters_before_rotating():
    outcomes = boss_outcomes(85, 86, cube_size=4)
    assert len(outcomes) == 1
    encounter = outcomes[0]
    assert encounter.terminal and encounter.probability == 1
    assert [action["kind"] for action in encounter.sequence] == ["move"]
    assert encounter.final_player == encounter.final_boss == 85

    waiting = boss_outcomes(85, 86, cube_size=4, moves=0, rotations=0)
    assert len(waiting) == 1 and not waiting[0].terminal
    assert waiting[0].final_boss == 86


def test_nonadjacent_enemy_can_move_toward_or_away_from_player():
    outcomes = boss_outcomes(85, 87, cube_size=4, rotations=0)
    assert {outcome.final_boss: outcome.probability for outcome in outcomes} == {
        83: 1 / 3, 86: 1 / 3, 91: 1 / 3,
    }


@pytest.mark.parametrize("enemy", [(0, 0), (0, 1), (1, 0), (1, 1)])
def test_quota_bellman_matches_primitive_enumeration(enemy):
    size = 3
    geo = geometry(size, move_distance=2)
    graph = realtime._pair_graph(size, 2)
    seed = np.zeros(len(graph.move) + 2)
    seed[-2] = 1

    @lru_cache(maxsize=None)
    def reference(player, boss, moves, rotations):
        if player == boss:
            return 1.0
        if not (moves or rotations):
            return sum(branch.probability for branch in boss_outcomes(
                player, boss, cube_size=size, moves=enemy[0], rotations=enemy[1],
            ) if branch.terminal)
        candidates = []
        if moves:
            candidates.extend(reference(int(target), boss, moves - 1, rotations)
                              for target in geo.moves[player, :geo.counts[player]])
        if rotations:
            for rotation in geo.actor_rotations[player]:
                perm = geo.rotations[rotation]
                candidates.append(reference(int(perm[player]), int(perm[boss]), moves, rotations - 1))
        return max(candidates)

    portable = realtime._player_values(graph, seed, 2, 2, enemy, None, lambda: None)
    native = realtime.get_native_solver()
    if native is not None:
        actual = realtime._player_values(graph, seed, 2, 2, enemy, native, lambda: None)
        np.testing.assert_array_equal(actual, portable)
    for player, boss in ((0, 5), (3, 28), (8, 9), (4, 38), (27, 31)):
        assert portable[2, 2, graph.ids[player, boss]] == pytest.approx(reference(player, boss, 2, 2))


def test_runtime_falls_back_to_a_legal_plan_when_reward_search_runs_out(monkeypatch):
    def out_of_time(*_args, **_kwargs):
        raise planner.PlanningBudgetExceeded("simulated slow reward search")

    monkeypatch.setattr(planner, "get_native_solver", lambda: object())
    monkeypatch.setattr(planner, "plan_inspiration", out_of_time)
    result = planner.plan_current_state(layout(3, 4, 31), planner.ENCOUNTER_THEN_INSPIRATION,
                                        realtime=True)

    assert result["status"] == "solved"
    assert result["next_action"] is not None
    assert result["solver"]["reward_lookahead_turns"] == 1


@pytest.mark.parametrize("encounter", [False, True])
def test_zero_probability_last_round_uses_observed_enemy_outcome(tmp_path, monkeypatch, encounter):
    board = layout(4, 81, 60)
    board.update(player_cell=coord_dict(81, 4), singularity_cell=coord_dict(60, 4))
    board["native_board"]["map_id"] = 1
    hud = dict(plane_index=2, rounds_remaining=1, moves_total=1, moves_used=1,
               rotations_total=1, rotations_used=0, collected_count=0, inspiration_total=0)
    state = dict(status="running", phase="planning", layout=board, hud=hud,
                 plane_index=2, rounds_remaining=1, collected_count=0, planning_time_budget_sec=.35,
                 scan_epoch=1, map_revision=0, pose_epoch=0, output_dir=str(tmp_path),
                 actions=[], event_stack=[], terminal_reached=False)
    confirm = AsyncMock(return_value=True)
    monkeypatch.setattr(run, "_save", AsyncMock())
    monkeypatch.setattr(run, "_fresh_click", confirm)

    async def advance(observation, raw_hud=None):
        await run._advance_state(state, None, None, None, None, observation, raw_hud or {})

    async def play_last_round():
        await run._plan(state, None, None)
        assert state["latest_plan"]["metrics"]["deadline_encounter_probability"] == 0
        assert state["phase"] == "operation" and state["pending_action"]["kind"] == "rotate"
        state["pending_action"]["submitted"] = True
        run._phase(state, "operation_outcome")
        await advance(dict(valid=True, scene="enemy_turn"))
        assert state["phase"] == "enemy_wait"

        depleted = dict(hud, rounds_remaining=0, rotations_used=1)
        for _ in range(2):
            await advance(dict(valid=True, scene="board", player_turn=True), depleted)
        assert state["rounds_remaining"] == 0 and state["phase"] == "enemy_wait"
        assert state["status"] == "running" and not state["terminal_reached"]
        confirm.assert_not_awaited()

        if encounter:
            boss = dict(valid=True, scene="event_entry", event_type="boss_battle", click=[1, 1])
            await advance(boss)
            await advance(boss)
            assert state["status"] == "running" and state["phase"] == "event_dispatch"
            assert state["event_stack"][0]["owner"] == "enemy_move_encounter"
            confirm.assert_awaited_once()
        else:
            settlement = dict(valid=True, scene="settlement", outcome="failure")
            await advance(settlement)
            assert state["status"] == "running"
            await advance(settlement)
            assert state["status"] == "completed" and state["outcome"] == "failure"
            assert state["reason"] == "settlement_observed" and state["terminal_reached"]
            confirm.assert_not_awaited()

    asyncio.run(play_last_round())


def test_extra_move_is_observed_before_waiting_for_the_enemy(monkeypatch):
    first = SimpleNamespace(commits=[], waits=[])
    before = dict(plane_index=1, rounds_remaining=4, moves_total=3, moves_used=1,
                  rotations_used=1, rotations_total=1)
    state = dict(pending_action=dict(kind="move", before_hud=before))
    monkeypatch.setattr(run, "_commit_action", lambda _state, evidence: first.commits.append(evidence))
    monkeypatch.setattr(run, "_enemy_wait", lambda _state, **evidence: first.waits.append(evidence))
    monkeypatch.setattr(run, "_progress_ledger", lambda *_: None)
    monkeypatch.setattr(run, "_invalidate", lambda *_: None)
    monkeypatch.setattr(run, "_phase", lambda state, phase: state.update(phase=phase))
    hud = dict(before, moves_used=2)
    run._action_outcome(state, None, {"scene": "board", "player_turn": True}, hud)

    assert state["phase"] == "scan_turn"
    assert len(first.commits) == 1
    assert first.waits == []
