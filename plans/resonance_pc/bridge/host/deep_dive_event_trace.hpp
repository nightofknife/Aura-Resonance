#pragma once

namespace bridge {

// Diagnostics only. Keep protocol arguments and callback return values intact.
inline constexpr auto deep_dive_event_trace_script = R"lua(
local mode = ...
local registry = debug.getregistry()
local key = 'AuraBridge.DeepDiveEventTrace.v1'
local trace = registry[key]

local function detach()
    if trace and trace.net.SendProto == trace.wrapper then
        trace.net.SendProto = trace.original
    end
    if trace and trace.callback_wrapper and trace.net.Callback == trace.callback_wrapper then
        trace.net.Callback = trace.original_callback
    end
    registry[key] = nil
end

if mode == 'stop' then
    detach()
    return {status='stopped'}
end
if trace and not trace.pending then
    -- A hot-loaded bridge can inherit the earlier send-only observer.
    detach()
    trace = nil
end

local net = Net
if type(net) ~= 'table' or type(net.SendProto) ~= 'function' then
    return {status='unsupported_net'}
end
if not trace then
    trace = {net=net, original=net.SendProto, records={}, sequence=0,
             pending=setmetatable({}, {__mode='k'})}
    local function record(stage, request, proto, coord, event_id, response_code)
        trace.sequence = trace.sequence + 1
        local records = trace.records
        if #records == 24 then table.remove(records, 1) end
        records[#records + 1] = {sequence=trace.sequence, stage=stage, request=request,
                                  protocol=proto, coord=coord, event_id=event_id, response_code=response_code}
        return trace.sequence
    end
    if type(net.Callback) == 'function' then
        trace.original_callback = net.Callback
        trace.callback_wrapper = function(...)
            local response, callback = ...
            local request = trace.pending[callback]
            if request and registry[key] == trace then
                local code
                if type(response) == 'string' and Json and type(Json.decode) == 'function' then
                    local decoded, body = pcall(Json.decode, response)
                    local rc = decoded and type(body) == 'table' and rawget(body, 'rc')
                    if type(rc) == 'number' or rc == 'ok' or rc == 'undisplay'
                        or type(rc) == 'string' and #rc <= 12 and rc:match('^%-?%d+$') then
                        code = tostring(rc)
                    end
                end
                record('response_received', request.sequence, request.protocol, request.coord, nil, code)
            end
            return trace.original_callback(...)
        end
        net.Callback = trace.callback_wrapper
    end
    trace.wrapper = function(owner, proto, ...)
        if os.time() > trace.expires and registry[key] == trace then detach() end
        if registry[key] ~= trace or (proto ~= 'cube.move' and proto ~= 'cube.event') then
            return trace.original(owner, proto, ...)
        end
        local callback, param = ...
        local coord = type(param) == 'string' and param:match('^%d+:%d+:%d+$')
        local request = record('send', nil, proto, coord)
        if type(callback) ~= 'function' then
            return trace.original(owner, proto, ...)
        end
        local function observe_reply(...)
            local reply = ...
            local event_id = type(reply) == 'table' and tonumber(rawget(reply, 'event_id'))
            if registry[key] == trace then record('reply', request, proto, coord, event_id) end
            local result = table.pack(callback(...))
            if registry[key] == trace then record('callback_returned', request, proto, coord, event_id) end
            return table.unpack(result, 1, result.n)
        end
        trace.pending[observe_reply] = {sequence=request, protocol=proto, coord=coord}
        return trace.original(owner, proto, observe_reply, select(2, ...))
    end
    trace.expires = os.time() + 120
    net.SendProto = trace.wrapper
    registry[key] = trace
elseif trace.net ~= net or net.SendProto ~= trace.wrapper then
    return {status='net_handler_changed'}
end
trace.expires = os.time() + 120

local model = package.loaded['UICubeRogueMain/UICubeRogueMainDataModel']
local controller = package.loaded['UICubeRogueMain/UICubeRoguewMainController']
local function cell_snapshot(cell)
    if type(cell) ~= 'table' then return nil end
    return {face=tonumber(cell.face), row=tonumber(cell.row), col=tonumber(cell.col),
            template_id=tonumber(cell.netData), event_id=tonumber(cell.realId),
            has_template=cell.netData ~= nil and cell.netData ~= '',
            has_special_item=cell.specialItemModel ~= nil}
end
local panel = controller and controller.curOptionEvent
local result = {status='active', records=trace.records,
                response_tracking=trace.callback_wrapper ~= nil and net.Callback == trace.callback_wrapper}
trace.records = {}
if model then
    result.player = cell_snapshot(model.playerCube)
    result.selected = cell_snapshot(model.curSelectCube)
    result.need_start_round = not not model.needStartRound
    result.need_trigger_round_end = not not model.needTriggerRoundEnd
    if model.roleCtrl then result.role_moving = not not model.roleCtrl.IsMoving end
    result.round = tonumber(model.stepNum)
    result.moves = tonumber(model.curMoveNum)
    result.rotations = tonumber(model.curRotNum)
end
if controller then
    result.option_event_id = type(panel) == 'table' and tonumber(panel.id) or nil
    result.option_state = tonumber(controller.optionPanelState)
    result.pending_rewards = type(controller.rewardList) == 'table' and #controller.rewardList or 0
    result.awaiting_reward_trigger = not not controller.NeedTriggerAfterReward
end
local function visible(widget)
    if type(widget) == 'table' then widget = widget.self end
    return not not (widget and not widget:IsNull() and widget.gameObject.activeInHierarchy)
end
-- Unity display state is supplementary. Keep the protocol records if this
-- client does not expose an expected widget.
pcall(function()
    local view = package.loaded['UICubeRogueMain/UICubeRogueMainView']
    if view then
        result.option_panel_visible = visible(view.Group_EventOptions)
        result.option_controls_visible = visible(view.Group_EventOptions and view.Group_EventOptions.Group_Options)
    end
    if UIManager then
        result.panel_url = UIManager:GetCurrentPanelURL()
        local mask = UIManager:GetPanel('UI/Common/NetMask')
        result.network_busy = not not (mask and mask.IsActive)
    end
end)
return result
)lua";

} // namespace bridge
