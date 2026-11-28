#pragma once

namespace bridge {

inline constexpr auto deep_dive_interval_script = R"lua(
local interval_entries = {
    [89800013] = {'recruit', 'UI/CubeRogue/RoleHire/RoleHire', 'UIRoleHire', 'RoleHire'},
    [89800016] = {'revive', 'UI/CubeRogue/Revive/Revive', 'UIRevive', 'Revive'},
    [89800015] = {'intensify', 'UI/CubeRogue/GamingShop/IntensifyShop', 'UIIntensifyShop', 'IntensifyShop'},
    [89800004] = {'awake', 'UI/CubeRogue/Wake/Wake', 'UIWake', 'Wake'},
}
local interval_coin = 11400607

local function interval_row(panel, id, action, apply, fields)
    local row = fields or {}
    row.id, row.action, row.index = id, action, #panel.rows + 1
    row.apply, row.disabled = apply, type(apply) ~= 'function'
    panel.rows[row.index] = row
end

local function interval_price(config, prefix, quality)
    local suffix = ({[0]='N', 'N', 'R', 'SR', 'SSR'})[quality]
    local list = suffix and config and config[prefix .. suffix .. 'List']
    local price = list and list[1]
    return price and tonumber(price.id) == interval_coin and tonumber(price.num) or nil
end

local function interval_recruits(visible_roles, available)
    local owned = PlayerData.ServerData and PlayerData.ServerData.roles
    if type(owned) ~= 'table' or type(visible_roles) ~= 'table' then return nil end
    local candidates, included, hidden = {}, {}, {}
    local function add(value)
        local id = reward_choice_id(value)
        if id and available[id] == nil and not included[id] and owned[tostring(id)] then
            candidates[#candidates + 1], included[id] = id, true
        end
    end
    for _, value in ipairs(visible_roles) do add(value) end
    for key in pairs(owned) do
        local id = reward_choice_id(key)
        if id and available[id] == nil and not included[id] then hidden[#hidden + 1] = id end
    end
    table.sort(hidden)
    for _, id in ipairs(hidden) do add(id) end
    return candidates
end

local function interval_main(panel, rest, view, run, available, model)
    local controller = package.loaded[main_controller]
    local buttons = package.loaded['UICubeRogueMain/UICubeRogueMainViewFunction']
    local skills = package.loaded['UICubeSkill/UICubeSkillDataModel']
    local open = buttons and buttons.CubeRogueMain_Group_RestArea_StaticGrid_List_Group_Item_Btn__Click
    local advance = buttons and buttons.CubeRogueMain_Group_RestArea_Group_Next_Btn__Click
    local quit = buttons and buttons.CubeRogueMain_Group_CommonTopLeft_Btn_Return_Click
    if not visible(rest) or visible(view.Group_EventOptions) or not controller
        or not controller.CanDealRotStuff() or not open or not advance or not quit then return end

    panel.party, panel.roster, panel.available_count = {}, {}, 0
    local included = {}
    if type(run.deck) ~= 'table' then return end
    for key in pairs(run.deck) do
        local member = reward_choice_id(key)
        if member and not included[member] then
            included[member] = true
            panel.party[#panel.party + 1] = member
        end
    end
    table.sort(panel.party)
    for member, alive in pairs(available) do
        if alive then
            panel.roster[#panel.roster + 1] = member
            panel.available_count = panel.available_count + 1
        end
    end
    table.sort(panel.roster)
    for _, row in ipairs(model.restPanelCfg.restEventList) do
        local entry = interval_entries[row.id]
        if entry then
            local prerequisite = tonumber(row.prepositionId)
            if prerequisite == 4294967295 then prerequisite = -1 end
            if prerequisite and prerequisite > 0 and (not skills or not skills.lockState
                or type(skills.GetSkillLockState) ~= 'function') then return end
            local unlocked = not prerequisite or prerequisite <= 0
                or skills.GetSkillLockState({id=prerequisite}) == skills.lockState.Unlocked
            if entry[1] == 'revive' and unlocked then
                unlocked = model.GetDescentAttr('BanRevive').value == 0
            end
            interval_row(panel, row.id, entry[1], unlocked and function()
                open(nil, tostring(row.id))
                return 'submitted'
            end)
        end
    end
    interval_row(panel, panel.subject_id, 'continue', panel.available_count >= formation_size and function()
        advance(nil, nil)
        return 'submitted'
    end)
    interval_row(panel, panel.subject_id, 'quit', function()
        quit(nil, nil)
        tracking.interval_exit = {rest=rest.self, id=panel.subject_id}
        return 'submitted'
    end)
    panel.identity, panel.ready = rest.self, true
end

local function interval_subpage(panel, entry_id, entry, view, model, controller, buttons, run, available)
    local operation, button_prefix = entry[1], entry[4]
    local back = buttons[button_prefix .. '_Btn_Back_Click']
    if not visible(view) or not visible(view.Btn_Back) or type(back) ~= 'function' then return end
    if operation == 'recruit' and visible(view.Group_SuccessTip) then
        local close = buttons.RoleHire_Group_SuccessTip_Btn_Close_Click
        if type(close) ~= 'function' or not visible(view.Group_SuccessTip.Btn_Close) then return end
        interval_row(panel, entry_id, 'dismiss', function() close(nil, nil); return 'applied' end)
        panel.identity, panel.ready = view.Group_SuccessTip.self, true
        return
    end
    interval_row(panel, entry_id, 'back', function() back(nil, nil); return 'submitted' end)

    local candidates
    if operation == 'recruit' then candidates = interval_recruits(model.Roles, available)
    elseif operation == 'intensify' then candidates = model.DataList
    else candidates = model.roleList end
    if type(candidates) ~= 'table' then return end
    local config = GetCA(entry_id, 'RogueEventFactory')
    for position, value in ipairs(candidates) do
        local id = reward_choice_id(type(value) == 'table' and value.id or value)
        if not id then return end
        local role = operation ~= 'intensify' and PlayerData:GetFactoryData(id) or nil
        local quality = role and role.qualityInt
        local fields, buy = {}, nil
        if operation == 'recruit' and available[id] == nil then
            fields.price = interval_price(config, 'recruitPrice', quality)
            local account = PlayerData:GetRoleById(tostring(id))
            fields.level = account and tonumber(account.lv)
            buy = function()
                local selected = model.SelectList
                if type(selected) ~= 'table' or #selected > 1
                    or (#selected == 1 and reward_choice_id(selected[1]) ~= id) then return 'waiting' end
                if #selected == 0 then controller.SelectRole(id) end
                if #model.SelectList ~= 1 or reward_choice_id(model.SelectList[1]) ~= id or not model.IsEnough
                    or model.NeedCoinNum ~= fields.price then return 'waiting' end
                controller.OnHireClick()
                return 'submitted'
            end
        elseif operation == 'revive' and available[id] == false and value.hp <= 0 then
            fields.price = interval_price(config, 'resurgencePrice', quality)
            buy = function()
                local selected = model.selectedIndex
                if type(selected) ~= 'table' then return 'waiting' end
                for key, active in pairs(selected) do
                    if active and tonumber(key) ~= position then return 'waiting' end
                end
                if not selected[tostring(position)] then controller:OnClickRoleBtn(tostring(position)) end
                if not selected[tostring(position)] or model.costNum ~= fields.price then return 'waiting' end
                controller:OnBtnRevive()
                return 'submitted'
            end
        elseif operation == 'intensify' and not value.intensify then
            fields.price = tonumber(value.upCostId) == interval_coin and tonumber(value.upCostNum) or nil
            fields.recommended = value.isRec == 1
            buy = function()
                controller.Select(position)
                if model.CurSelect ~= position then return 'waiting' end
                tracking.interval_intensify = true
                controller.ConfirmClick()
                return 'submitted'
            end
        elseif operation == 'awake' then
            local account = PlayerData:GetRoleById(tostring(id))
            local member = run.roles[tostring(id)]
            if not (account and role and member and type(role.breakthroughList) == 'table') then return end
            fields.awake = (account.awake_lv or 0) + (member.awake_lv or 0)
            fields.max_awake = #role.breakthroughList - 1
            if fields.awake < fields.max_awake then
                fields.price = interval_price(config, 'breakPrice', quality)
                buy = function()
                    controller:OnSelectRole(position)
                    if model.selectedIndex ~= position or model.costNum ~= fields.price then return 'waiting' end
                    controller:OnBtnAwake()
                    return 'submitted'
                end
            end
        end
        if buy then
            if not fields.price or fields.price < 0 or (operation == 'recruit' and not fields.level) then return end
            interval_row(panel, id, 'buy', function()
                if panel.balance < fields.price then return 'changed' end
                return buy()
            end, fields)
        end
    end
    panel.identity, panel.ready = view.self, true
end

local function read_rest_interval(url)
    local model = package.loaded['UICubeRogueMain/UICubeRogueMainDataModel']
    local page_config = model and model.restPanelCfg
    if not page_config or type(page_config.restEventList) ~= 'table' then
        tracking.interval_intensify = nil
        return nil
    end
    local operation, entry_id, entry
    if url == main_url then operation = 'main'
    elseif url == 'UI/CubeRogue/ItemGet/ItemGet' and tracking.interval_intensify then operation = 'feedback'
    else
        for id, data in pairs(interval_entries) do
            if data[2] == url then entry_id, entry, operation = id, data, data[1]; break end
        end
    end
    if not operation then return nil end
    if operation ~= 'feedback' and operation ~= 'intensify' then tracking.interval_intensify = nil end
    local main_view = package.loaded['UICubeRogueMain/UICubeRogueMainView']
    local rest = main_view and main_view.Group_RestArea
    if not rest or not rest.self or rest.self:IsNull() or not rest.self.gameObject.activeSelf then return nil end
    local panel = {kind='rest_' .. operation, ready=false, rows={}}
    local run, available, _, detail = formation_roster()
    if not run then return formation_wait(panel, 'rest_roster_unavailable', detail) end
    panel.subject_id = reward_choice_id(run.rest_id)
    local owner = UIManager:GetPanel(url)
    if not panel.subject_id or not owner or owner.IsPlayAnimator then return panel end
    local money = PlayerData:GetGoodsById(interval_coin)
    panel.balance = money and tonumber(money.num)
    if not panel.balance or panel.balance < 0 then return panel end
    if operation == 'main' then
        interval_main(panel, rest, main_view, run, available, model)
    elseif operation == 'feedback' then
        local prefix = 'UIItemGet/UIItemGet'
        local view = package.loaded[prefix .. 'View']
        local buttons = package.loaded[prefix .. 'ViewFunction']
        local dismiss = buttons and buttons.ItemGet_Btn__Click
        if visible(view) and visible(view.Btn_) and type(dismiss) == 'function' then
            interval_row(panel, panel.subject_id, 'dismiss', function() dismiss(nil, nil); return 'submitted' end)
            panel.identity, panel.ready = view.self, true
        end
    else
        local prefix = entry[3] .. '/' .. entry[3]
        local view = package.loaded[prefix .. 'View']
        local data = package.loaded[prefix .. 'DataModel']
        local controller = package.loaded[prefix .. 'Controller']
        local buttons = package.loaded[prefix .. 'ViewFunction']
        if view and data and controller and buttons then
            interval_subpage(panel, entry_id, entry, view, data, controller, buttons, run, available)
        end
    end
    if panel.ready then
        local signature = {tostring(panel.balance)}
        for _, row in ipairs(panel.rows) do
            signature[#signature + 1] = table.concat({row.id, row.action, row.price or 0,
                row.awake or 0, row.level or 0, row.max_awake or 0,
                tostring(row.recommended), tostring(row.disabled)}, ':')
        end
        if panel.party then
            signature[#signature + 1] = table.concat(panel.party, ',')
            signature[#signature + 1] = table.concat(panel.roster, ',')
        end
        panel.content = table.concat(signature, '|')
    end
    return panel
end
)lua";

} // namespace bridge
