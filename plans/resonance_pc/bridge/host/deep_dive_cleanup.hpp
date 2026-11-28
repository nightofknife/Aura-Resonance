#pragma once

namespace bridge {

inline constexpr auto deep_dive_cleanup_script = R"lua(
local final_settlement_url = 'UI/CubeRogue/Settlement/CubeRogueSettlement'
local final_reward_url = 'UI/Common/ShowItem'
local final_card_drawing_url = 'UI/CollectionCard/CardDrawing'
local final_overview_url = 'UI/CubeRogue/OverView/CubeRogueOverView'
local final_settlement_subject = 89400002

local function read_final_reward(url, reward)
    local panel = {kind='cleanup', ready=false, rows={}}
    local owner = UIManager:GetPanel(url)
    if not owner or owner.IsPlayAnimator then return panel end

    if url == final_card_drawing_url then
        local view = package.loaded['UICardDrawing/UICardDrawingView']
        if visible(view) and visible(view.Drag_) then
            panel.page = 'card_pack'
        end
        return panel
    end

    local prefix = 'UIShowItem/UIShowItem'
    local view = package.loaded[prefix .. 'View']
    local model = package.loaded[prefix .. 'DataModel']
    local buttons = package.loaded[prefix .. 'ViewFunction']
    local dismiss = buttons and buttons.ShowItem_Btn_OK_Click
    local items = model and model.Data
    if not visible(view) or not visible(view.Btn_OK) or type(dismiss) ~= 'function'
        or type(items) ~= 'table' or not next(items) then return panel end

    panel.page, panel.identity, panel.content = 'settlement_reward', view.self, items
    panel.subject_id, panel.ready = final_settlement_subject, true
    interval_row(panel, final_settlement_subject, 'dismiss', function()
        if tracking.deep_dive_final_reward ~= reward then return 'changed' end
        dismiss(nil)
        return 'submitted'
    end)
    return panel
end

local function read_final(url)
    if url == final_overview_url then
        tracking.deep_dive_final_reward = nil
    end
    if url == final_reward_url or url == final_card_drawing_url then
        local reward = tracking.deep_dive_final_reward
        if reward then return read_final_reward(url, reward) end
        return nil
    end
    if url ~= final_settlement_url then return nil end

    local panel = {kind='cleanup', ready=false, rows={}, outcome='unknown'}
    local owner = UIManager:GetPanel(url)
    local prefix = 'UICubeRogueSettlement/UICubeRogueSettlement'
    local view = package.loaded[prefix .. 'View']
    local model = package.loaded[prefix .. 'DataModel']
    local buttons = package.loaded[prefix .. 'ViewFunction']
    local data = model and model.data
    if not owner or owner.IsPlayAnimator or not visible(view) or type(data) ~= 'table' then
        return panel
    end

    local summary, details = view.Group_Process_1, view.Group_Process_2
    local show_summary, show_details = not not visible(summary), not not visible(details)
    if show_summary == show_details then return panel end
    if type(data.isWin) == 'boolean' then
        panel.outcome = data.isWin and 'victory' or 'failure'
    end

    local button, handler, action
    if show_summary then
        panel.page, panel.identity = 'settlement_summary', summary.self
        button = summary.Btn_Off
        handler = buttons and buttons.CubeRogueSettlement_Group_Process_1_Btn_Off_Click
        action = 'continue'
    else
        panel.page, panel.identity = 'settlement_details', details.self
        local group = details.Group_Btn
        local success = not not (group and visible(group.Btn_Confirm))
        local failure = not not (group and visible(group.Btn_ConfirmNot))
        if success == failure then return panel end
        local handler_name
        if success then
            button = group.Btn_Confirm
            handler_name = 'CubeRogueSettlement_Group_Process_2_Group_Btn_Btn_Confirm_Click'
        else
            button = group.Btn_ConfirmNot
            handler_name = 'CubeRogueSettlement_Group_Process_2_Group_Btn_Btn_ConfirmNot_Click'
        end
        handler = buttons and buttons[handler_name]
        action = 'confirm'
    end
    if not visible(button) or type(handler) ~= 'function' then return panel end

    panel.content, panel.subject_id, panel.ready = data, final_settlement_subject, true
    interval_row(panel, final_settlement_subject, action, function()
        if show_details then
            local reward = data.reward
            tracking.deep_dive_final_reward = type(reward) == 'table' and type(reward.item) == 'table'
                and next(reward.item) ~= nil and reward or nil
        end
        handler(nil, nil)
        return 'submitted'
    end)
    return panel
end
)lua";

} // namespace bridge
