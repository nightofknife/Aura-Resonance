#pragma once

namespace bridge {

inline constexpr auto deep_dive_shop_script = R"lua(
local gaming_shop_url = 'UI/CubeRogue/GamingShop/GamingShop'
local gaming_shop_prefix = 'UIGamingShop/UIGamingShop'
local gaming_shop_feedback = {
    ['UI/CubeRogue/ItemGet/ItemGet'] = {'UIItemGet/UIItemGet', 'Btn_', 'ItemGet_Btn__Click', 'dataList'},
    ['UI/CubeRogue/ShowItem/CubeRogueShowItem'] = {
        'UICubeRogueShowItem/UICubeRogueShowItem', 'Btn_OK', 'CubeRogueShowItem_Btn_OK_Click', 'Data'},
}

local function shop_amount(value)
    return type(value) == 'number' and value >= 0 and value < math.huge
end

local function shop_balance(coin)
    local goods = PlayerData:GetGoodsById(coin)
    return goods and (goods.num or 0) or 0
end

local function read_shop_feedback(url)
    local purchase = tracking.shop_purchase
    local feedback = gaming_shop_feedback[url]
    if not purchase or not feedback or UIManager:GetPanel(gaming_shop_url) ~= purchase.owner
        or (purchase.item.py_cnt or 0) <= purchase.count then return nil end
    local panel = {kind='shop_feedback', ready=false, rows={}}
    local owner = UIManager:GetPanel(url)
    local view = package.loaded[feedback[1] .. 'View']
    local buttons = package.loaded[feedback[1] .. 'ViewFunction']
    local model = package.loaded[feedback[1] .. 'DataModel']
    local items = model and model[feedback[4]]
    local dismiss = buttons and buttons[feedback[3]]
    if owner and not owner.IsPlayAnimator and visible(view) and visible(view[feedback[2]])
        and type(dismiss) == 'function' and type(items) == 'table' and #items > 0 then
        panel.identity, panel.subject_id, panel.ready = view.self, purchase.subject, true
        -- The next reward can reuse the view and URL immediately after dismissal.
        panel.content = items
        interval_row(panel, purchase.subject, 'dismiss', function() dismiss(nil, nil); return 'submitted' end)
    end
    return panel
end

local function read_gaming_shop(url)
    if url ~= gaming_shop_url then return read_shop_feedback(url) end
    local panel = {kind='shop', ready=false, rows={}}
    local owner = UIManager:GetPanel(url)
    local view = package.loaded[gaming_shop_prefix .. 'View']
    local model = package.loaded[gaming_shop_prefix .. 'DataModel']
    local controller = package.loaded[gaming_shop_prefix .. 'Controller']
    local buttons = package.loaded[gaming_shop_prefix .. 'ViewFunction']
    local store = package.loaded['UIStore/UIStoreDataModel']
    local attributes = package.loaded['UICubeRogueMain/CubeRogueEquipAttrManager']
    local rogue = package.loaded['UICubeRogueMain/UICubeRogueMainDataModel']
    local run = PlayerData and PlayerData.TempCache and PlayerData.TempCache.CubeRogueData
    local select_item = controller and controller.OnSelectWeapon
    local buy = buttons and buttons.GamingShop_Group_Right_Btn_Buy_Click
    local back = buttons and buttons.GamingShop_Group_CommonTopLeft_Btn_Return_Click
    if not (owner and not owner.IsPlayAnimator and visible(view) and model and model.isTriggerClose
            and run and run.team and store and type(store.GetPurchaseNum) == 'function'
            and attributes and rogue and Enum_AttrType and DataManager
            and type(select_item) == 'function' and type(buy) == 'function' and type(back) == 'function') then
        return panel
    end
    local tab = model.curIndex
    local shop = model.shops and model.shops[tab]
    local subject = shop and reward_choice_id(shop.id)
    local items = shop and shop.data and shop.data.items
    local coin = model.cubeCA and reward_choice_id(model.cubeCA.coinId)
    if not subject or not coin or type(items) ~= 'table' then return panel end
    local purchase = tracking.shop_purchase
    if purchase and (purchase.owner ~= owner or purchase.subject ~= subject) then tracking.shop_purchase = nil end
    local balance = shop_balance(coin)
    local price_adjustment = attributes:GetAttr(Enum_AttrType.Price)
    local descent = rogue.GetDescentAttr('Price')
    local descent_price = descent and descent.value
    if not shop_amount(balance) or type(price_adjustment) ~= 'number' or type(descent_price) ~= 'number' then
        return panel
    end
    local sale = PlayerData:GetFactoryData(shop.id)
    if not sale or type(sale.shopList) ~= 'table' then return panel end
    local offered = {}
    for _, offer in ipairs(sale.shopList) do
        local id = reward_choice_id(offer.id)
        if id then offered[id] = true end
    end

    local signature = {tostring(tab), string.format('%.17g', balance)}
    for position, item in ipairs(items) do
        local id = reward_choice_id(item.id)
        local commodity = id and PlayerData:GetFactoryData(item.id)
        local reward = commodity and commodity.commodityItemList and commodity.commodityItemList[1]
        local money = commodity and commodity.moneyList and commodity.moneyList[1]
        local source = reward and PlayerData:GetFactoryData(reward.id)
        if not source or not money or type(money.moneyNum) ~= 'number' then return panel end
        local count = item.py_cnt or 0
        local price = money.moneyNum * (1 + price_adjustment + descent_price)
        local maximum = math.huge
        if commodity.purchase then
            maximum = store:GetPurchaseNum(commodity)
            if not shop_amount(maximum) then return panel end
        end
        if not shop_amount(count) or count % 1 ~= 0 or count > 2147483647 or not shop_amount(price) then
            return panel
        end
        local factory = DataManager:GetFactoryNameById(reward.id)
        local recommended = (factory == 'RogueBuffFactory' or factory == 'ItemFactory')
            and source.correspondingTeam ~= nil and tostring(source.correspondingTeam) == tostring(run.team)
        local purchasable = offered[id] and count < maximum and tonumber(money.moneyID) == coin
        local apply = purchasable and function()
            if shop_balance(coin) < price then return 'changed' end
            select_item(controller, position)
            if model.curIndex ~= tab or model.shops[tab] ~= shop or shop.data.items ~= items
                or model.commodityIndex ~= position or items[position] ~= item
                or (item.py_cnt or 0) ~= count then return 'changed' end
            local right = view.Group_Right
            local button = right and right.Btn_Buy
            if not visible(button) or visible(button.Img_Unable) then return 'waiting' end
            tracking.shop_purchase = {owner=owner, subject=subject, item=item, count=count}
            buy(nil, nil)
            return 'submitted'
        end
        interval_row(panel, id, 'buy', apply, {price=price, purchased=count, recommended=recommended})
        signature[#signature + 1] = table.concat({id, count, string.format('%.17g', price),
            tostring(recommended), tostring(purchasable == true)}, ':')
    end
    local left = view.Group_CommonTopLeft
    interval_row(panel, subject, 'back', visible(left and left.Btn_Return) and function()
        tracking.shop_purchase = nil
        back(nil, nil)
        return 'submitted'
    end)
    signature[#signature + 1] = tostring(panel.rows[#panel.rows].disabled)
    panel.identity, panel.subject_id, panel.balance, panel.ready = items, subject, balance, true
    panel.content = table.concat(signature, '|')
    return panel
end
)lua";

} // namespace bridge
