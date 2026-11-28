#pragma once

namespace bridge {

inline constexpr auto deep_dive_opening_script = R"lua(
local opening_pages = {
    ['UI/CubeRogue/OverView/CubeRogueOverView'] = {'activity_home', 'UICubeRogueOverview'},
    ['UI/CubeRogue/DepthSelection/DepthSelection'] = {'opening_depth', 'UIDepthSelection'},
    ['UI/CubeRogue/TacticsSelection/TacticsSelection_New'] = {'opening_strategy', 'UITacticsSelection_New'},
    ['UI/CubeRogue/Team_Cube/Team_CubeNew'] = {'opening_formation', 'UITeam_CubeNew'},
}

local function opening_button_ready(widget)
    if not visible(widget) then return false end
    if type(widget) == 'table' then widget = widget.self end
    local button = widget.Btn
    return not button or (not button:IsNull() and button.isActiveAndEnabled and button:IsInteractable())
end

local function opening_click(handler, parameter)
    if type(handler) ~= 'function' then return nil end
    return function()
        handler(nil, parameter)
        return 'submitted'
    end
end

local function opening_control(panel, action, widget, handler)
    if opening_button_ready(widget) then
        interval_row(panel, panel.subject_id, action, opening_click(handler))
    end
end

local function read_opening(url)
    local spec = opening_pages[url]
    if not spec then return nil end
    local page, module = spec[1], spec[2]
    local panel = {kind='opening', page=page, ready=false, rows={}}
    local prefix = module .. '/' .. module
    local owner = UIManager:GetPanel(url)
    local view = package.loaded[prefix .. 'View']
    local model = package.loaded[prefix .. 'DataModel']
    local buttons = package.loaded[prefix .. 'ViewFunction']
    if not (owner and not owner.IsPlayAnimator and visible(view) and model and buttons) then return panel end
    local cache = PlayerData and PlayerData.TempCache
    local run = cache and cache.CubeRogueData
    local config = cache and cache.RogueCache
    local cube_id = reward_choice_id(model.cubeId) or reward_choice_id(model.configCubeId)
        or reward_choice_id(config and config.cubeId)
    if not cube_id then return panel end
    panel.subject_id, panel.identity = cube_id, view.self

    if page == 'activity_home' then
        local enter = view.Group_Enter
        local start = enter and enter.Group_Start
        opening_control(panel, 'start', start and start.Btn_, buttons.CubeRogueOverview_Group_Enter_Group_Start_Btn__Click)
    elseif page == 'opening_depth' then
        local controller = package.loaded[prefix .. 'Controller']
        local depths = model.cubeCA and model.cubeCA.rogueDifficultList
        local choose = buttons.DepthSelection_ScrollView_Main_Viewport_Content_StaticGrid_Level_Group_Depth_Btn_Defult_Click
        local start = buttons.DepthSelection_Btn_Start_Click
        if type(depths) ~= 'table' or not controller or type(controller.IsDifficultyUnlock) ~= 'function'
            or type(choose) ~= 'function' or type(start) ~= 'function' then return panel end
        for depth in ipairs(depths) do
            local selected = model.curDiffIndex == depth
            local action = selected and 'confirm_depth' or 'select_depth'
            local apply = nil
            if controller:IsDifficultyUnlock(depth) then
                if not selected or opening_button_ready(view.Btn_Start) then
                    apply = selected and opening_click(start) or opening_click(choose, tostring(depth))
                elseif selected then
                    return panel
                end
            end
            interval_row(panel, depth, action, apply, {selected=selected})
        end
    elseif page == 'opening_strategy' then
        local teams = model.cubeCA and model.cubeCA.rogueTeamList
        local choose = buttons.TacticsSelection_New_Group_Selection_StaticGrid_Tactics_Group_Tactics_Btn_Click_Click
        local confirm = buttons.TacticsSelection_New_Group_Info_Btn_Confirm_Click
        if type(teams) ~= 'table' or type(choose) ~= 'function' or type(confirm) ~= 'function'
            or not TimeUtil or type(TimeUtil.IsActive) ~= 'function' then return panel end
        opening_control(panel, 'back', view.Btn_Back, buttons.TacticsSelection_New_Btn_Back_Click)
        for index, team in ipairs(teams) do
            local id = reward_choice_id(team.id)
            local strategy = id and PlayerData:GetFactoryData(id)
            if not strategy or type(strategy.startTime) ~= 'string' then return panel end
            local selected = model.curIndex == index
            local action = selected and 'confirm_strategy' or 'select_strategy'
            local apply = nil
            if TimeUtil:IsActive(strategy.startTime, '') then
                local info = view.Group_Info
                if not selected or opening_button_ready(info and info.Btn_Confirm) then
                    apply = selected and opening_click(confirm) or opening_click(choose, tostring(index))
                end
            end
            interval_row(panel, id, action, apply, {selected=selected})
        end
    else
        if model.mode ~= 'InitCube' then return panel end
        opening_control(panel, 'back', view.Btn_Back, buttons.Team_CubeNew_Btn_Back_Click)
        local slots = model.levelRoleList
        local confirm = buttons.Team_CubeNew_Btn_Battle_Click
        if type(slots) == 'table' and opening_button_ready(view.Btn_Battle) then
            local seen, occupants, complete = {}, {}, #slots >= 5
            for index, slot in ipairs(slots) do
                local id = reward_choice_id(slot)
                occupants[index] = id or -1
                if id then
                    if seen[id] then complete = false end
                    seen[id] = true
                elseif index <= 5 or slot ~= -1 then
                    complete = false
                end
            end
            interval_row(panel, cube_id, 'confirm_formation', complete and opening_click(confirm) or nil)
            panel.content = tostring(run and run.difficulty) .. ':' .. tostring(run and run.team)
                .. ':' .. table.concat(occupants, ',')
        end
    end

    local signature = {page, panel.content or ''}
    for _, row in ipairs(panel.rows) do
        signature[#signature + 1] = table.concat({row.id, row.action, tostring(row.disabled)}, ':')
    end
    panel.content = table.concat(signature, '|')
    panel.ready = #panel.rows > 0
    return panel
end
)lua";

} // namespace bridge
