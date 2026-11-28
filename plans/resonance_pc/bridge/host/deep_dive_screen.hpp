#pragma once

namespace bridge {

inline constexpr auto deep_dive_screen_script = R"lua(
local runtime_main_url = 'UI/CubeRogue/Main/CubeRogueMain'
local runtime_main_prefix = 'UICubeRogueMain/UICubeRogueMain'
local runtime_event_kinds = {
    Boss='boss_battle', NormalBattle='battle', HardBattle='elite_battle',
    Shop='shop', SpecialShop='shop', Recover='healing', Reward='item', Event='vortex',
}
local runtime_feedback = {
    ['UI/CubeRogue/ItemGet/ItemGet'] = {'UIItemGet/UIItemGet', 'Btn_', 'ItemGet_Btn__Click', 'dataList'},
    ['UI/CubeRogue/ShowItem/CubeRogueShowItem'] = {
        'UICubeRogueShowItem/UICubeRogueShowItem', 'Btn_OK', 'CubeRogueShowItem_Btn_OK_Click', 'Data'},
}

local function runtime_main_parts(url)
    if url ~= runtime_main_url then return end
    local owner = UIManager:GetPanel(url)
    local view = package.loaded[runtime_main_prefix .. 'View']
    local model = package.loaded[runtime_main_prefix .. 'DataModel']
    local controller = package.loaded['UICubeRogueMain/UICubeRoguewMainController']
    if owner and not owner.IsPlayAnimator and visible(view) and model and controller then
        return view, model, controller
    end
end

local function runtime_event_type(model)
    local selected = model.curSelectCube
    if type(selected) ~= 'table' then return end
    if selected.netData == nil or selected.netData == '' then return 'empty' end
    local template = reward_choice_id(selected.netData)
    local types = model.Enum_CubeType
    if not template or type(types) ~= 'table' or type(GetCA) ~= 'function' then return end
    local record = GetCA(template, 'ActivityListFactory')
    local type_id = record and reward_choice_id(record.typeId)
    if type_id then
        for key, kind in pairs(runtime_event_kinds) do
            if tonumber(types[key]) == type_id then return kind end
        end
    end
end

local function runtime_boss_level(view, model, controller)
    local actions = controller.Enum_Continue
    local open_squad = actions and actions.OpenSquadPanel
    if open_squad ~= nil and controller.ContinueClick == open_squad and visible(view.Btn_Continue)
        and runtime_event_type(model) == 'boss_battle' then
        return reward_choice_id(controller.cacheLevelId)
    end
end

local function read_main_screen(url)
    local screen = {scene='unknown', player_turn=false}
    local view, model, controller = runtime_main_parts(url)
    if not view then return screen end
    local options, details, tips = view.Group_EventOptions, view.Group_EventDetails, view.Group_MoveTips
    if visible(options) then
        if controller.optionPanelState == 6 then screen.scene = 'event_ending' end
    elseif visible(view.Group_RestArea) then
        screen.scene = 'rest_area'
    elseif runtime_boss_level(view, model, controller) then
        screen.scene, screen.event_type = 'event_entry', 'boss_battle'
    elseif visible(details) then
        if visible(details.Btn_Fight) and controller.playerMoving and model.hasConfirmRot == true then
            local kind = runtime_event_type(model)
            if kind then screen.scene, screen.event_type = 'event_entry', kind end
        end
    elseif visible(view.Group_ConfirmRot) and model.hasConfirmRot == false then
        screen.scene = 'rotate_preview'
    elseif visible(tips and tips.Group_BossMove) or visible(view.Group_BossAction) then
        screen.scene = 'enemy_turn'
    elseif visible(view.Group_ActionInformation) and visible(tips and tips.Group_PlayerMove)
        and not visible(view.Group_Enter) and not visible(view.Btn_Continue)
        and model.hasConfirmRot == true and not model.needStartRound
        and type(controller.CanDealRotStuff) == 'function' and controller.CanDealRotStuff() then
        screen.player_turn = true
        if controller.playerMoving then screen.scene = 'choose_move'
        elseif controller.playerRotating then screen.scene = 'choose_rotate'
        else screen.scene = 'board' end
    end
    return screen
end

local function read_runtime_controls(url)
    local feedback = runtime_feedback[url]
    if feedback then
        local panel = {kind='item_feedback', ready=false, rows={}}
        local owner = UIManager:GetPanel(url)
        local view = package.loaded[feedback[1] .. 'View']
        local buttons = package.loaded[feedback[1] .. 'ViewFunction']
        local model = package.loaded[feedback[1] .. 'DataModel']
        local items = model and model[feedback[4]]
        local first = type(items) == 'table' and items[1]
        local item_id = type(first) == 'table' and reward_choice_id(first.id)
        local dismiss = buttons and buttons[feedback[3]]
        if owner and not owner.IsPlayAnimator and visible(view) and visible(view[feedback[2]])
            and item_id and type(dismiss) == 'function' then
            panel.identity, panel.content, panel.subject_id, panel.ready = view.self, items, item_id, true
            -- Each popup has its own content table, including successive identical rewards.
            interval_row(panel, item_id, 'dismiss', function() dismiss(nil, nil); return 'submitted' end)
        end
        return panel
    end

    local view, model, controller = runtime_main_parts(url)
    if not view then return end
    local level = runtime_boss_level(view, model, controller)
    local buttons = package.loaded[runtime_main_prefix .. 'ViewFunction']
    if level then
        local panel = {kind='boss_entry', ready=false, rows={}}
        local open = buttons and buttons.CubeRogueMain_Btn_Continue_Click
        if type(open) == 'function' then
            panel.identity, panel.content = view.Btn_Continue, model.curSelectCube
            panel.subject_id, panel.ready = level, true
            interval_row(panel, level, 'enter', function() open(nil, nil); return 'submitted' end)
        end
        return panel
    end

    local options, event = view.Group_EventOptions, controller.curOptionEvent
    if visible(options) and controller.optionPanelState == 6 then
        local panel = {kind='event_end', ready=false, rows={}}
        local event_id = type(event) == 'table' and reward_choice_id(event.id)
        local close = buttons and buttons.CubeRogueMain_Group_EventOptions_Btn__Click
        if event_id and visible(options.Btn_) and type(close) == 'function' then
            panel.identity, panel.content = options.self, model.AllOptions
            panel.subject_id, panel.ready = event_id, true
            interval_row(panel, event_id, 'dismiss', function() close(nil, nil); return 'submitted' end)
        end
        return panel
    end
end
)lua";

} // namespace bridge
