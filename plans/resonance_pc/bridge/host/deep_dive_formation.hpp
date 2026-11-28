#pragma once

namespace bridge {

inline constexpr auto deep_dive_formation_script = R"lua(
local formation_url = 'UI/CubeRogue/Team_Cube/Team_Battle'
local main_url = 'UI/CubeRogue/Main/CubeRogueMain'
local quit_url = 'UI/CubeRogue/Quit/CubeRogueQuit'
local main_controller = 'UICubeRogueMain/UICubeRoguewMainController'
local formation_size = 5

local function formation_board()
    local model = package.loaded['UICubeRogueMain/UICubeRogueMainDataModel']
    return model and model.rubikCube
end

local function formation_boss_encounter(level)
    local model = package.loaded['UICubeRogueMain/UICubeRogueMainDataModel']
    local selected = model and model.curSelectCube
    local event_id = selected and reward_choice_id(selected.realId)
    if not event_id or not PlayerData or type(PlayerData.GetFactoryData) ~= 'function' then return nil end
    local event = PlayerData:GetFactoryData(event_id, 'CubeEventFactory')
    return event and event.isBoss == true and reward_choice_id(event.levelId) == level or nil
end

local function roster_failure(detail, roles)
    if roles and rawget(roles, '') ~= nil then
        detail = detail .. ':empty_role_key_present'
    end
    return nil, nil, nil, detail
end

local function formation_roster()
    local cache = PlayerData and PlayerData.TempCache
    local run = cache and cache.CubeRogueData
    if not run then return roster_failure('run_cache_missing') end
    local roles = run.roles
    if type(roles) ~= 'table' then return roster_failure('roles_not_table:' .. type(roles)) end
    local rogue = package.loaded[main_controller]
    if not rogue then return roster_failure('controller_missing', roles) end
    if type(rogue.GetRoleHpPercent) ~= 'function' then
        return roster_failure('hp_reader_not_function:' .. type(rogue.GetRoleHpPercent), roles)
    end

    local available, count = {}, 0
    for key, role in pairs(roles) do
        -- The game's formation list also excludes empty roster keys.
        if key ~= '' then
            local id = reward_choice_id(key)
            if not id then return roster_failure('invalid_role_key:' .. type(key), roles) end
            if type(role) ~= 'table' then
                return roster_failure('role_not_table:' .. type(role), roles)
            end
            if available[id] ~= nil then return roster_failure('duplicate_role_id', roles) end
            local hp = rogue:GetRoleHpPercent(tostring(id))
            if type(hp) ~= 'number' then return roster_failure('hp_not_number:' .. type(hp), roles) end
            available[id] = hp > 0
            if available[id] then count = count + 1 end
        end
    end
    return run, available, count
end

local function formation_control(panel, kind, identity, subject, target)
    panel.kind, panel.identity, panel.subject_id = kind, identity, subject
    panel.rows = {{id=target, index=1, disabled=false, selected=false}}
    panel.ready = true
    return panel
end

local function formation_wait(panel, reason, roster_detail)
    panel.wait_reason = reason
    panel.roster_detail = roster_detail
    return panel
end

local function formation_entry_finished(owner)
    local animator = owner:GetComponent(typeof(CS.UnityEngine.Animator))
    if not animator or animator:IsNull() or not animator.isActiveAndEnabled
        or animator.layerCount ~= 1 or animator:IsInTransition(0) then return false end
    local state = animator:GetCurrentAnimatorStateInfo(0)
    -- The page requests Animation_SelectOpen, but its controller only has Team_Battle.
    return state:IsName('Base Layer.Team_Battle') and not state.loop and state.normalizedTime >= 1
end

local function formation_start(panel, view, buttons, level)
    local confirm = buttons and buttons.Team_Battle_Btn_Battle_Click
    if type(confirm) ~= 'function' then return formation_wait(panel, 'formation_start_callback_missing') end
    if not visible(view.Btn_Battle) then return formation_wait(panel, 'formation_start_button_hidden') end
    local widget = view.Btn_Battle
    if type(widget) == 'table' then widget = widget.self end
    local button = widget.Btn
    if not button or button:IsNull() or not button.isActiveAndEnabled or not button:IsInteractable() then
        return formation_wait(panel, 'formation_start_button_disabled')
    end
    panel.confirm, panel.content = confirm, table.concat(panel.party, ',')
    return formation_control(panel, 'formation_start', view.self, level, level)
end

local function read_formation(url)
    if url ~= formation_url then return nil end
    local prefix = 'UITeam_Battle/UITeam_Battle'
    local panel = {kind='formation_start', ready=false, rows={}}
    local model = package.loaded[prefix .. 'DataModel']
    if not model or not model.mode then return formation_wait(panel, 'formation_model_unavailable') end
    if model.mode ~= 'CubeSquad' then return nil end
    local owner = UIManager:GetPanel(url)
    if not owner then return formation_wait(panel, 'formation_owner_unavailable') end
    local view = package.loaded[prefix .. 'View']
    if not visible(view) then return formation_wait(panel, 'formation_view_hidden') end
    local level = reward_choice_id(model.levelId)
    if not level then return formation_wait(panel, 'formation_level_unavailable') end
    panel.subject_id, panel.required_count = level, formation_size
    panel.boss_encounter = formation_boss_encounter(level)
    if model.curLevelIndex ~= 1 then return formation_wait(panel, 'formation_level_tab_inactive') end
    if model.isDrag then return formation_wait(panel, 'formation_drag_active') end
    local slots = model.levelRoleList
    if type(slots) ~= 'table' or #slots ~= formation_size then
        return formation_wait(panel, 'formation_slots_invalid')
    end

    local selected, count, party = {}, 0, {}
    for slot=1,formation_size do
        if slots[slot] ~= -1 then
            local id = reward_choice_id(slots[slot])
            if not id or selected[id] then return formation_wait(panel, 'formation_slots_invalid') end
            selected[id] = true
            party[#party + 1] = id
            count = count + 1
        end
    end
    panel.selected_count, panel.party = count, party
    if owner.IsPlayAnimator and not formation_entry_finished(owner) then
        return formation_wait(panel, 'formation_animating')
    end
    local buttons = package.loaded[prefix .. 'ViewFunction']
    -- The native start callback validates occupied slots and saves any deck changes.
    -- Roster data is only needed to choose how to handle open slots.
    local pinned = next(fixed_party) ~= nil
    if count == formation_size and not pinned then
        return formation_start(panel, view, buttons, level)
    end

    local run, roster, available, roster_detail = formation_roster()
    if not run then return formation_wait(panel, 'formation_roster_unavailable', roster_detail) end
    panel.available_count = available
    local fixed_alive = true
    for id in pairs(fixed_party) do
        if not roster[id] then fixed_alive = false; break end
    end
    if available < formation_size or not fixed_alive or (not allow_formation_fill and not pinned) then
        local board = formation_board()
        local back = buttons and buttons.Team_Battle_Btn_Back_Click
        if not board or not visible(view.Btn_Back) or type(back) ~= 'function' then
            return formation_wait(panel, 'formation_back_unavailable')
        end
        panel.confirm = function()
            back(nil, nil)
            -- Saving the deck can replace CubeRogueData while the current board persists.
            tracking.formation_exit = {board=board, level=level, required=formation_size,
                forced=pinned or not allow_formation_fill}
        end
        return formation_control(panel, 'formation_leave', view.self, level, level)
    end

    local wrong_slot
    if pinned then
        for slot, value in ipairs(slots) do
            local member = reward_choice_id(value)
            if member and not fixed_party[member] then wrong_slot = slot; break end
        end
        if wrong_slot then
            local remove = buttons and buttons.Team_Battle_Group_Team_StaticGrid_RoleList_Group_BattleTeamRole_Btn_Click_Click
            if type(remove) ~= 'function' then return formation_wait(panel, 'formation_remove_unavailable') end
            panel.fill = function()
                remove(nil, tostring(wrong_slot))
                return model.levelRoleList[wrong_slot] == -1 and 'applied' or 'waiting'
            end
            panel.content = table.concat(party, ',')
            return formation_control(panel, 'formation_fill', view.self, level, reward_choice_id(slots[wrong_slot]))
        elseif count == formation_size then
            return formation_start(panel, view, buttons, level)
        end
    end
    for id in pairs(selected) do
        if not roster[id] then return formation_wait(panel, 'formation_selected_role_unavailable') end
    end

    local click = buttons and buttons.Team_Battle_ScrollGrid_RoleList_Group_RoleFace_Btn_Click_Click
    if not visible(view.ScrollGrid_RoleList) or type(click) ~= 'function'
        or type(model.Roles) ~= 'table' or type(model.roleCheckMap) ~= 'table' then
        return formation_wait(panel, 'formation_recruit_controls_unavailable')
    end
    local function eligible(id)
        return id and (not pinned or fixed_party[id]) and roster[id] and not selected[id]
            and not model.roleCheckMap[tostring(id)]
    end
    local candidate, rarity = nil, -1
    local function consider(id)
        if not eligible(id) then return end
        local role = PlayerData.GetFactoryData and PlayerData:GetFactoryData(id)
        local quality = role and tonumber(role.qualityInt) or 0
        if quality > rarity then candidate, rarity = id, quality end
    end
    for _, role in ipairs(model.Roles) do consider(reward_choice_id(role)) end
    -- Filters can hide a higher-rarity reserve. Preserve the visible order for ties.
    local hidden = {}
    for id in pairs(roster) do hidden[#hidden + 1] = id end
    table.sort(hidden)
    for _, id in ipairs(hidden) do
        consider(id)
    end
    if not candidate then return formation_wait(panel, 'formation_recruit_unavailable') end
    panel.fill = function()
        click(nil, tostring(candidate))
        for _, role in ipairs(model.levelRoleList) do
            if reward_choice_id(role) == candidate then return 'applied' end
        end
        return 'waiting'
    end
    panel.content = table.concat(party, ',') .. ':' .. candidate
    return formation_control(panel, 'formation_fill', view.self, level, candidate)
end

local function read_formation_exit(url)
    local exit = tracking.formation_exit
    if not exit or (url ~= main_url and url ~= quit_url) then return nil end
    local panel = {kind=url == main_url and 'formation_quit' or 'formation_finish', ready=false, rows={}}
    local run, _, available, roster_detail = formation_roster()
    if not run then return formation_wait(panel, 'formation_roster_unavailable', roster_detail) end
    if formation_board() ~= exit.board or (not exit.forced and available >= exit.required) then
        tracking.formation_exit = nil
        return nil
    end
    local owner = UIManager:GetPanel(url)
    if not owner then return formation_wait(panel, 'formation_owner_unavailable') end
    if owner.IsPlayAnimator then return formation_wait(panel, 'formation_animating') end
    if url == main_url then
        local view = package.loaded['UICubeRogueMain/UICubeRogueMainView']
        local controller = package.loaded[main_controller]
        if not visible(view) or not controller or not controller.CanDealRotStuff() then
            return formation_wait(panel, 'formation_quit_unavailable')
        end
        -- Open the same quit panel as the main return button, including when an event owns the board.
        panel.confirm = function() UIManager:Open(quit_url) end
        return formation_control(panel, 'formation_quit', view.self, exit.level, exit.level)
    end
    local prefix = 'UICubeRogueQuit/UICubeRogueQuit'
    local view = package.loaded[prefix .. 'View']
    local buttons = package.loaded[prefix .. 'ViewFunction']
    local finish = buttons and buttons.CubeRogueQuit_Group_Btn_Btn_Finish_Click
    if not view or not view.Group_Btn or not visible(view.Group_Btn.Btn_Finish)
        or type(finish) ~= 'function' then return formation_wait(panel, 'formation_finish_unavailable') end
    panel.confirm = function()
        finish(nil, nil)
        tracking.formation_exit = nil
    end
    return formation_control(panel, 'formation_finish', view.self, exit.level, exit.level)
end
)lua";

} // namespace bridge
