"""Real city-trade sequencing returns to the city after settlement."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from plans.resonance_pc.src.actions import city_trade_flow_pc_actions as trade


@pytest.fixture
def rig(monkeypatch):
    state = SimpleNamespace(
        calls=[], main={"success": True, "page_state": "city_main"},
        sell={"success": True, "sold_confirmed": True, "page_state": "shop_page",
              "sell_result": "complete", "settlement": {"closed": True},
              "raise": {"actual_fatigue_used": 8}},
        buy={"success": True, "page_state": "shop_page", "buy_result": "complete",
             "selected_products": ["goods"], "settlement": {"closed": True},
             "bargain": {"actual_fatigue_used": 16}},
    )

    def enter(**kwargs):
        state.calls.append("enter")
        return {"success": True, "page_state": "shop_page", "menu_ready": {"ready": True}}

    def ready(*args, **kwargs):
        state.calls.append("ready")
        return {"ready": True}

    def node(*, kind, **kwargs):
        state.calls.append("sell_node" if kind == "sell" else "buy_node")
        return {"clicked": True}

    def sell(**kwargs):
        state.calls.append("sell_negotiation")
        assert kwargs["raise_to_cap"] is True
        state.calls.append("sell_settlement_closed")
        return deepcopy(state.sell)

    def buy(**kwargs):
        state.calls.append("buy_negotiation")
        assert kwargs["bargain_to_cap"] is True
        state.calls.append("buy_settlement_closed")
        return deepcopy(state.buy)

    def main(**kwargs):
        state.calls.append("main")
        return deepcopy(state.main)

    monkeypatch.setattr(trade, "_enter_exchange", enter)
    monkeypatch.setattr(trade, "_wait_for_shop_menu_ready", ready)
    monkeypatch.setattr(trade, "_open_trade_page", node)
    monkeypatch.setattr(trade, "resonance_pc_sell_goods_on_sell_page", sell)
    monkeypatch.setattr(trade, "resonance_pc_buy_goods_on_buy_page", buy)
    monkeypatch.setattr(trade, "resonance_pc_go_city_main_direct", main)

    def run(*, scoped=False, purchase=True, reuse_entry=False):
        action = (trade._execute_city_trade_inside_current_city_scoped if scoped
                  else trade._execute_city_trade_inside_current_city)
        return action(
            current_city="city-a", buy_products=["goods"] if purchase else [], books_used=0,
            sell_raise_to_cap=True, buy_bargain_to_cap=purchase,
            app=object(), ocr=object(), vision=object(), city_shop_data=object(),
            shop_entry=({"success": True, "page_state": "shop_page"} if reuse_entry else None),
        )

    state.run = run
    return state


@pytest.mark.parametrize("scoped", [False, True])
def test_sell_then_buy_returns_to_main_after_settlement(rig, scoped):
    result = rig.run(scoped=scoped)
    assert rig.calls == ["enter", "sell_node", "sell_negotiation", "sell_settlement_closed",
                         "buy_node", "buy_negotiation", "buy_settlement_closed", "main"]
    assert result["success"] is True and result["page_state"] == "city_main"
    assert result["sell"]["sold_confirmed"] is True and result["buy"]["settlement"]["closed"]


@pytest.mark.parametrize("scoped", [False, True])
def test_sale_only_returns_to_main_after_settlement(rig, scoped):
    result = rig.run(scoped=scoped, purchase=False)
    assert rig.calls == ["enter", "sell_node", "sell_negotiation", "sell_settlement_closed", "main"]
    assert result["buy"] is None


@pytest.mark.parametrize("main", [{"success": False, "page_state": "unknown"},
                                   {"success": True, "page_state": "shop_page"}])
def test_failed_main_navigation_is_not_reported_as_completed_trade(rig, main):
    rig.main = main
    with pytest.raises(trade.CityTradeFlowError) as caught:
        rig.run()
    assert caught.value.code == "trade_main_not_restored"
    assert rig.calls[-2:] == ["buy_settlement_closed", "main"]
    confirmed = caught.value.detail["city_trade"]
    assert confirmed["success"] is True and confirmed["page_state"] == "shop_page"
    assert confirmed["sell"]["sold_confirmed"] is True
    assert confirmed["buy"]["settlement"]["closed"] is True


@pytest.mark.parametrize("stage", ["sell", "buy"])
def test_unconfirmed_transaction_does_not_exit_the_exchange(rig, stage):
    getattr(rig, stage)["success"] = False
    with pytest.raises(trade.CityTradeFlowError) as caught:
        rig.run()
    assert caught.value.code == f"{stage}_transaction_not_confirmed"
    assert "main" not in rig.calls


def test_investment_shop_entry_reuse_returns_to_main_after_trade(rig):
    result = rig.run(reuse_entry=True)
    assert "enter" not in rig.calls and rig.calls[0] == "ready"
    assert rig.calls[-2:] == ["buy_settlement_closed", "main"]
