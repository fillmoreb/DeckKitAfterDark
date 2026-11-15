-- DeckAgent's view of you and your stuff: numbers only. Item and stat names come from the assistant's
-- own database (agent/gamedb.py) and the stat codes below. Packets as in Agent.lua's header (binary,
-- big-endian); deck-setup/decklink/agent.py is the other end (STAT_CODES there: keep in step).
--
-- CHARACTER (op 14) request: just the op byte.
-- reply body:   level uint8, money uint32 (copper), free bag slots uint8, bag slots uint8,
--               specialization ID uint16 (0 none; Forever uses specs and modern talents, not trees),
--               strength, agility, stamina, intellect, spirit, armor, spell power, healing power,
--               max health, max mana: uint16 each (effective, with gear),
--               equipped count uint8, then each: slot uint8 (1-19), item ID uint32, enchant ID uint16,
--               random suffix ID int16, durability percent uint8 (255: none)
--
-- BAGS (op 15) request: byte 1: 1 bags, 2 the bank (as it was the last time you opened it)
-- reply body:   flags uint8 (bit 0 more didn't fit, bit 1 bank never seen), free slots uint8, slots
--               uint8, minutes since the bank was seen uint16 (0 for bags), count uint8, then each:
--               item ID uint32, count uint16 (stacks added up)
--
-- ITEM_INFO (op 16) request: count uint8 (1-30), then each: item ID uint32, random suffix ID int16
-- reply body:   count uint8 (the first that fit in one strip frame: ask again for the rest), then
--               each: item ID uint32, status uint8 (0 ok, 1 loading: ask again in a
--               moment, 2 no such item), and when ok: quality uint8, item level uint16, required level
--               uint8, slot uint8 (InventoryType: 1 head ... 28 relic, 0 not equippable), class uint8,
--               subclass uint8, stat count uint8, then each: stat code uint8, value int16
--               (stat code 13, DPS, is sent x10)
--
-- PRICES (op 17) request: count uint8 (1-60), then each: item ID uint32
-- reply body:   flags uint8 (bit 0 MarketDeck isn't installed), count uint8 (the first that fit), then
--               each: item ID uint32, lowest buyout now uint32, quantity up uint16, market price uint32
--               (the middle of each day's lowest, two weeks), days of history uint8, hours since that
--               auction house was scanned uint16 (65535: never), vendor price uint32, disenchant value
--               uint32 (an estimate; 0: can't be or no prices). Copper; 0 where unknown. From DeckKit's
--               MarketDeck (auction prices from its snapshots).
--
-- MARKS (op 18) request: byte 1: 1 mark, 2 clear. Mark: label (length uint8 + UTF-8), count uint8 (1-40),
-- then the item IDs, uint32. Clear: label (empty: every mark DeckAgent made). Through DeckKit's PackPad
-- (PackPad_SetMarks: a lilac star on every stack in the bags, the label in the tooltip).
-- reply body:   flags uint8 (bit 0 PackPad isn't installed), count uint8, then per item asked: how many
--               he carries, uint16 (bags only)
--
-- Items already in the game's cache (anything you have, have seen, or it has loaded this session) cost
-- nothing. One it doesn't know is a request to the server: those are paced (at most 10 at once, then
-- two a second: gentler than one auction house page, which loads dozens at once); one over the pace
-- answers "loading" without asking, and the host asks again later. The host only asks for item IDs in its database (no invalid IDs, which are the ones
-- that used to get clients disconnected for flooding) and keeps what it learns.
local _, ns = ...

local B, S = ns.Bytes, ns.Status
local U8, U16, Str, ReadU16 = B.U8, B.U16, B.Str, B.ReadU16
local OP_CHARACTER, OP_BAGS, OP_ITEM_INFO, OP_PRICES, OP_MARKS = 14, 15, 16, 17, 18
local NONE = 255

-- GetItemStats keys -> stat codes (agent.py STAT_CODES).
local STAT = {
    ITEM_MOD_STRENGTH_SHORT = 1, ITEM_MOD_AGILITY_SHORT = 2, ITEM_MOD_STAMINA_SHORT = 3,
    ITEM_MOD_INTELLECT_SHORT = 4, ITEM_MOD_SPIRIT_SHORT = 5, RESISTANCE0_NAME = 6, RESISTANCE1_NAME = 7,
    RESISTANCE2_NAME = 8, RESISTANCE3_NAME = 9, RESISTANCE4_NAME = 10, RESISTANCE5_NAME = 11,
    RESISTANCE6_NAME = 12, ITEM_MOD_DAMAGE_PER_SECOND_SHORT = 13, ITEM_MOD_SPELL_POWER_SHORT = 14,
    ITEM_MOD_SPELL_HEALING_DONE_SHORT = 15, ITEM_MOD_ATTACK_POWER_SHORT = 16, ITEM_MOD_CRIT_RATING_SHORT = 17,
    ITEM_MOD_HIT_RATING_SHORT = 18, ITEM_MOD_DEFENSE_SKILL_RATING_SHORT = 19, ITEM_MOD_POWER_REGEN0_SHORT = 20,
    ITEM_MOD_HEALTH_REGEN_SHORT = 21, ITEM_MOD_BLOCK_VALUE_SHORT = 22, ITEM_MOD_SPELL_CRIT_RATING_SHORT = 23,
    ITEM_MOD_SPELL_HIT_RATING_SHORT = 24, ITEM_MOD_RANGED_ATTACK_POWER_SHORT = 25, ITEM_MOD_DODGE_RATING_SHORT = 26,
    ITEM_MOD_PARRY_RATING_SHORT = 27, ITEM_MOD_BLOCK_RATING_SHORT = 28, ITEM_MOD_SPELL_PENETRATION_SHORT = 29,
    ITEM_MOD_CRIT_MELEE_RATING_SHORT = 17, ITEM_MOD_HIT_MELEE_RATING_SHORT = 18,
}
local INVTYPE = { INVTYPE_HEAD = 1, INVTYPE_NECK = 2, INVTYPE_SHOULDER = 3, INVTYPE_BODY = 4, INVTYPE_CHEST = 5,
    INVTYPE_WAIST = 6, INVTYPE_LEGS = 7, INVTYPE_FEET = 8, INVTYPE_WRIST = 9, INVTYPE_HAND = 10, INVTYPE_FINGER = 11,
    INVTYPE_TRINKET = 12, INVTYPE_WEAPON = 13, INVTYPE_SHIELD = 14, INVTYPE_RANGED = 15, INVTYPE_CLOAK = 16,
    INVTYPE_2HWEAPON = 17, INVTYPE_BAG = 18, INVTYPE_TABARD = 19, INVTYPE_ROBE = 20, INVTYPE_WEAPONMAINHAND = 21,
    INVTYPE_WEAPONOFFHAND = 22, INVTYPE_HOLDABLE = 23, INVTYPE_AMMO = 24, INVTYPE_THROWN = 25,
    INVTYPE_RANGEDRIGHT = 26, INVTYPE_QUIVER = 27, INVTYPE_RELIC = 28 }

local function U32(n)
    n = math.floor(n) % 4294967296
    return U16(math.floor(n / 65536)) .. U16(n % 65536)
end
local function S16(n)
    n = math.floor((n or 0) + 0.5)
    if n > 32767 then n = 32767 elseif n < -32768 then n = -32768 end
    return U16(n % 65536)
end
local function Clamp16(n) return U16(math.max(0, math.min(65535, math.floor((n or 0) + 0.5)))) end

-- Bags 0-4 (backpack and the four bag slots); the bank is -1 plus bags 5-10.
local BAGS, BANK = { 0, 1, 2, 3, 4 }, { -1, 5, 6, 7, 8, 9, 10 }

local function Scan(list)
    local counts, order, free, slots = {}, {}, 0, 0
    for _, bag in ipairs(list) do
        local n = C_Container.GetContainerNumSlots(bag) or 0
        slots = slots + n
        for slot = 1, n do
            local info = C_Container.GetContainerItemInfo(bag, slot)
            if info and info.itemID then
                if not counts[info.itemID] then order[#order + 1] = info.itemID end
                counts[info.itemID] = (counts[info.itemID] or 0) + (info.stackCount or 1)
            else
                free = free + 1
            end
        end
    end
    return counts, order, free, slots
end

DeckAgentDB = DeckAgentDB or {}
local function CharKey() return (UnitName("player") or "?") .. "-" .. (GetRealmName and GetRealmName() or "?") end
local function Now() return GetServerTime and GetServerTime() or time() end

-- The bank can only be read while it's open: remember it then.
local function RememberBank()
    local counts, order, free, slots = Scan(BANK)
    DeckAgentDB.bank = DeckAgentDB.bank or {}
    DeckAgentDB.bank[CharKey()] = { at = Now(), counts = counts, order = order, free = free, slots = slots }
end
local bankOpen = false
ns.On("BANKFRAME_OPENED", function() bankOpen = true; RememberBank() end)
ns.On("BANKFRAME_CLOSED", function() if bankOpen then RememberBank() end bankOpen = false end)
ns.On("PLAYERBANKSLOTS_CHANGED", function() if bankOpen then RememberBank() end end)

local function SpecID()
    local i = C_SpecializationInfo and C_SpecializationInfo.GetSpecialization and C_SpecializationInfo.GetSpecialization()
    local id = i and C_SpecializationInfo.GetSpecializationInfo(i)
    return U16(tonumber(id) or 0)
end

local function SpellPower()
    local best = 0
    if GetSpellBonusDamage then
        for school = 2, 7 do best = math.max(best, GetSpellBonusDamage(school) or 0) end
    end
    return best
end

ns.OPS[OP_CHARACTER] = function()
    local _, _, free, slots = Scan(BAGS)
    local stats = {}
    for i = 1, 5 do
        local _, effective = UnitStat("player", i)
        stats[i] = Clamp16(effective)
    end
    local _, armor = UnitArmor("player")
    local parts = {}
    for slot = 1, 19 do
        local link = GetInventoryItemLink("player", slot)
        local id = link and tonumber(link:match("item:(%d+)"))
        if id then
            -- item:ID:enchant:gem1:gem2:gem3:gem4:suffix:...
            local fields = { strsplit(":", link:match("item:([%-%d:]+)") or "") }
            local cur, max = GetInventoryItemDurability(slot)
            local dur = (cur and max and max > 0) and math.floor(cur / max * 100 + 0.5) or NONE
            parts[#parts + 1] = U8(slot) .. U32(id) .. U16(tonumber(fields[2]) or 0) .. S16(tonumber(fields[8]) or 0)
                .. U8(dur)
        end
    end
    return S.OK, U8(UnitLevel("player") or 0) .. U32(GetMoney() or 0) .. U8(math.min(255, free))
        .. U8(math.min(255, slots)) .. SpecID() .. table.concat(stats) .. Clamp16(armor) .. Clamp16(SpellPower())
        .. Clamp16(GetSpellBonusHealing and GetSpellBonusHealing() or 0) .. Clamp16(UnitHealthMax("player"))
        .. Clamp16(UnitPowerMax("player", 0)) .. U8(#parts) .. table.concat(parts)
end

ns.OPS[OP_BAGS] = function(p)
    local which = p:byte(2) or 1
    local counts, order, free, slots, age, flags = nil, nil, 0, 0, 0, 0
    if which == 2 then
        local bank = DeckAgentDB.bank and DeckAgentDB.bank[CharKey()]
        if bankOpen then RememberBank(); bank = DeckAgentDB.bank[CharKey()] end
        if not bank then return S.OK, U8(2) .. U8(0) .. U8(0) .. U16(0) .. U8(0) end
        counts, order, free, slots = bank.counts, bank.order, bank.free, bank.slots
        age = math.floor((Now() - (bank.at or Now())) / 60)
    else
        counts, order, free, slots = Scan(BAGS)
    end
    local budget = DeckLink.MaxPayload() - 9
    local parts = {}
    for _, id in ipairs(order) do
        if (#parts + 1) * 6 > budget then flags = flags + 1; break end
        parts[#parts + 1] = U32(id) .. U16(math.min(65535, counts[id] or 0))
    end
    return S.OK, U8(flags) .. U8(math.min(255, free)) .. U8(math.min(255, slots)) .. Clamp16(age) .. U8(#parts)
        .. table.concat(parts)
end

local OK_, LOADING, MISSING = 0, 1, 2

-- Server requests for items the game doesn't know yet: a token bucket (10, refilling two a second).
local BUCKET, REFILL = 10, 0.5
local tokens, refilledAt = BUCKET, 0
local function MayRequest()
    local now = GetTime()
    tokens = math.min(BUCKET, tokens + (now - refilledAt) / REFILL)
    refilledAt = now
    if tokens < 1 then return false end
    tokens = tokens - 1
    return true
end
local requested = {} -- item ID -> true once asked this session (one ask is enough; the reply comes)

local function ItemEntry(id, suffix)
    local _, _, _, equipLoc, _, classID, subclassID = C_Item.GetItemInfoInstant(id)
    if not equipLoc then return U32(id) .. U8(MISSING) end
    -- GetItemInfo asks the server itself when the item isn't cached: check first (a local lookup), so
    -- every server request goes through the pace above.
    if C_Item.IsItemDataCachedByID and not C_Item.IsItemDataCachedByID(id) then
        if not requested[id] and C_Item.RequestLoadItemDataByID and MayRequest() then
            requested[id] = true
            C_Item.RequestLoadItemDataByID(id)
        end
        return U32(id) .. U8(LOADING)
    end
    local link = "item:" .. id .. "::::::" .. (suffix ~= 0 and suffix or "")
    local _, _, quality, ilvl, req = C_Item.GetItemInfo(link)
    local stats = C_Item.GetItemStats(link)
    if not quality or not stats then
        if not requested[id] and C_Item.RequestLoadItemDataByID and MayRequest() then
            requested[id] = true
            C_Item.RequestLoadItemDataByID(id)
        end
        return U32(id) .. U8(LOADING)
    end
    local seen, parts = {}, {}
    for key, value in pairs(stats) do
        local code = STAT[key]
        if code and not seen[code] then
            seen[code] = true
            parts[#parts + 1] = U8(code) .. S16(code == 13 and value * 10 or value)
        end
    end
    return U32(id) .. U8(OK_) .. U8(quality or 0) .. U16(ilvl or 0) .. U8(req or 0) .. U8(INVTYPE[equipLoc] or 0)
        .. U8(classID or 0) .. U8(subclassID or 0) .. U8(#parts) .. table.concat(parts)
end

ns.OPS[OP_ITEM_INFO] = function(p)
    local n = p:byte(2)
    if not n or n < 1 or n > 30 or #p < 2 + 6 * n then return S.BAD_ARGS, Str("ITEM_INFO needs 1-30 items") end
    local parts, budget = {}, DeckLink.MaxPayload() - 5
    for k = 1, n do
        local i = 3 + (k - 1) * 6
        local id = ReadU16(p, i) * 65536 + ReadU16(p, i + 2)
        local suffix = ReadU16(p, i + 4)
        if suffix >= 32768 then suffix = suffix - 65536 end
        local entry = ItemEntry(id, suffix)
        if #entry > budget then break end
        budget = budget - #entry
        parts[#parts + 1] = entry
    end
    return S.OK, U8(#parts) .. table.concat(parts)
end

ns.OPS[OP_PRICES] = function(p)
    local n = p:byte(2)
    if not n or n < 1 or n > 60 or #p < 2 + 4 * n then return S.BAD_ARGS, Str("PRICES needs 1-60 item IDs") end
    if not MarketDeck_GetPrice then return S.OK, U8(1) .. U8(0) end
    local parts, budget, now = {}, DeckLink.MaxPayload() - 5, Now()
    for k = 1, n do
        local i = 3 + (k - 1) * 4
        local id = ReadU16(p, i) * 65536 + ReadU16(p, i + 2)
        local pr = MarketDeck_GetPrice(id) or {}
        local hours = (pr.scanned and pr.scanned > 0) and math.min(65534, math.floor((now - pr.scanned) / 3600)) or 65535
        local entry = U32(id) .. U32(pr.now or 0) .. U16(math.min(65535, pr.quantity or 0)) .. U32(pr.market or 0)
            .. U8(math.min(255, pr.days or 0)) .. U16(hours) .. U32(pr.vendor or 0) .. U32(pr.disenchant or 0)
        if #entry > budget then break end
        budget = budget - #entry
        parts[#parts + 1] = entry
    end
    return S.OK, U8(0) .. U8(#parts) .. table.concat(parts)
end

-- A mark set per label: "DeckAgent:<label>" in PackPad, so Decky can clear one set or all of hers.
local MARK_PREFIX = "DeckAgent:"

ns.OPS[OP_MARKS] = function(p)
    local action, n = p:byte(2), p:byte(3)
    if not (action and n) or 3 + n > #p + 1 then return S.BAD_ARGS, Str("MARKS needs an action and a label") end
    local label = p:sub(4, 3 + n)
    local i = 4 + n
    if action == 2 then
        if PackPad_ClearMarks and PackPad_GetMarks then
            if label == "" then
                for source in pairs(PackPad_GetMarks()) do
                    if source:sub(1, #MARK_PREFIX) == MARK_PREFIX then PackPad_ClearMarks(source) end
                end
            else
                PackPad_ClearMarks(MARK_PREFIX .. label)
            end
        end
        return S.OK, U8(PackPad_ClearMarks and 0 or 1) .. U8(0)
    end
    if action ~= 1 then return S.BAD_ARGS, Str("MARKS action is 1 (mark) or 2 (clear)") end
    local count = p:byte(i)
    if not count or count < 1 or count > 40 or #p < i + 4 * count then return S.BAD_ARGS, Str("mark 1-40 item IDs") end
    local ids, have = {}, {}
    for k = 1, count do
        local j = i + 1 + (k - 1) * 4
        ids[k] = ReadU16(p, j) * 65536 + ReadU16(p, j + 2)
        have[k] = U16(math.min(65535, (C_Item.GetItemCount and C_Item.GetItemCount(ids[k])) or 0))
    end
    if PackPad_SetMarks then PackPad_SetMarks(MARK_PREFIX .. label, ids, label) end
    return S.OK, U8(PackPad_SetMarks and 0 or 1) .. U8(count) .. table.concat(have)
end
