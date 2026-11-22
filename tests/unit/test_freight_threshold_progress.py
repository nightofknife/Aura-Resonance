"""Threshold recovery remains local to a visit and never finishes freight early."""
import asyncio

import pytest

from packages.resonance_gui.logic import (
    TRADE_PROGRESS_EVENT, TRADE_PROGRESS_SCHEMA, reduce_workflow_freight_progress,
)
from plans.resonance_pc.src.actions._freight_contract import (
    normalize_planning_inputs, plan_view, planning_event_data,
)
from plans.resonance_pc.src.actions.city_trade_flow_pc_actions import _TradeProgressReporter


ROUTE = [
    {"from_city_id": "1", "from_city": "A", "to_city_id": "2", "to_city": "B"},
    {"from_city_id": "2", "from_city": "B", "to_city_id": "1", "to_city": "A"},
]


def plan(*, water=True, bento=True, medicine=True, mode="thresholds"):
    request = normalize_planning_inputs({})
    water_plan = {"mode": mode, "enabled": water, "planned": water, "city_index": 1}
    return plan_view({"status": "ok", "route": ROUTE}, request, kind="run",
                     auto_bento=bento, auto_medicine=medicine, water_plan=water_plan)


def event(sequence, stage, state="completed", *, city_index=0, checkpoint="start", **data):
    return {"name": TRADE_PROGRESS_EVENT, "payload": {
        "schema": TRADE_PROGRESS_SCHEMA, "cid": "threshold-freight", "sequence": sequence,
        "stage": stage, "state": state, "city_index": city_index,
        "timestamp": f"2026-10-24T01:00:{sequence:02d}Z",
        "data": {"scope": "route", "phase": checkpoint, **data},
    }}


def phase(state, city_index, key):
    return next(item for item in state.cities[city_index].phases if item.key == key)


def planned():
    return reduce_workflow_freight_progress(None, event(
        1, "planning", **planning_event_data(plan())))


RECOVERY_STAGES = ("sparkling_water", "bento", "fatigue_medicine")


@pytest.mark.parametrize("water,bento,medicine", [
    (False, False, False), (False, True, True), (True, False, True), (True, True, False),
])
def test_threshold_plan_orders_recovery_after_trade_before_departure(water, bento, medicine):
    result = plan(water=water, bento=bento, medicine=medicine)
    visits = result["city_visits"]
    assert [visit["city_id"] for visit in visits] == ["1", "2", "1"]
    for visit in visits[:-1]:
        phases = visit["phases"]
        keys = [item["key"] for item in phases]
        assert keys[keys.index("buy") + 1:keys.index("travel")] == list(RECOVERY_STAGES)
        for key, enabled in zip(RECOVERY_STAGES, (water, bento, medicine)):
            item = next(item for item in phases if item["key"] == key)
            assert item["status"] == ("waiting" if enabled else "skipped")
            assert item["reason"] == (None if enabled else "disabled")
    terminal_keys = [item["key"] for item in visits[-1]["phases"]]
    assert terminal_keys[-1] == "final_sale"
    assert not set(RECOVERY_STAGES).intersection(terminal_keys)


def test_legacy_projection_still_supports_its_explicit_water_selection():
    result = plan(mode="legacy")
    visits = result["city_visits"]
    assert not any(item["key"] in {"bento", "sparkling_water"} for item in visits[0]["phases"])
    assert next(item for item in visits[1]["phases"] if item["key"] == "sparkling_water")["status"] == "waiting"
    assert [item["key"] for item in visits[-1]["phases"]][-2:] == ["final_sale", "bento"]


@pytest.mark.parametrize("stage", ["sparkling_water", "bento"])
def test_new_checkpoint_reopens_recovery_after_skip_without_finishing_task(stage):
    state = reduce_workflow_freight_progress(planned(), event(2, stage, "skipped"))
    assert phase(state, 0, stage).state == "skipped"
    state = reduce_workflow_freight_progress(state, event(3, stage, "started", checkpoint="pre_departure"))
    assert phase(state, 0, stage).state == "running"
    assert phase(state, 0, stage).started_at == "2026-10-24T01:00:03Z"
    assert state.stage_started_at == "2026-10-24T01:00:03Z"
    assert state.active_city_index == 0
    assert state.state == "running" and not state.task_succeeded and state.percent < 100
    state = reduce_workflow_freight_progress(state, event(4, stage, checkpoint="pre_departure"))
    assert phase(state, 0, stage).state == "completed"
    assert state.state == "running" and state.percent < 100
    assert phase(state, 1, stage).state == "waiting"
    # The old guard still rejects a late child heartbeat for this same check.
    assert reduce_workflow_freight_progress(
        state, event(5, stage, "progress", checkpoint="pre_departure")) == state


def test_terminal_sale_finishes_the_visit_before_task_success():
    state = reduce_workflow_freight_progress(planned(), event(2, "arrival", city_index=2))
    state = reduce_workflow_freight_progress(state, event(3, "final_sale", city_index=2))
    assert phase(state, 2, "final_sale").state == "completed"
    assert not set(RECOVERY_STAGES).intersection(item.key for item in state.cities[2].phases)
    assert state.active_city_index == 2 and state.state == "running"
    assert state.percent < 100 and not state.task_succeeded
    state = reduce_workflow_freight_progress(state, event(4, "task", city_index=2))
    assert state.percent == 100 and state.task_succeeded


def test_reporter_counts_each_city_recovery_once_and_only_task_can_finish():
    class Bus:
        def __init__(self):
            self.events = []

        async def publish(self, emitted):
            self.events.append({"name": emitted.name, "payload": emitted.payload})

    async def exercise():
        bus = Bus()
        reporter = _TradeProgressReporter(bus, "threshold-freight", asyncio.get_running_loop())
        await reporter.emit("planning", "completed", data=planning_event_data(plan()))
        for visit in plan()["city_visits"]:
            for item in visit["phases"]:
                if item["status"] == "waiting":
                    await reporter.emit(item["key"], "completed", city_index=visit["city_index"],
                                        data={"scope": "route", "phase": "pre_departure"})
        before = bus.events[-1]["payload"]["data"]["progress"]["completed_units"]
        for index in (0, 1):
            for stage in RECOVERY_STAGES:
                for status in ("started", "completed"):
                    await reporter.emit(stage, status, city_index=index,
                                        data={"scope": "route", "phase": "pre_departure"})
                    assert bus.events[-1]["payload"]["data"]["progress"]["completed_units"] == before
        state = None
        for emitted in bus.events:
            state = reduce_workflow_freight_progress(state, emitted)
            assert not state.task_succeeded and state.percent < 100
        await reporter.emit("task", "completed")
        state = reduce_workflow_freight_progress(state, bus.events[-1])
        assert state.task_succeeded and state.percent == 100

    asyncio.run(exercise())
