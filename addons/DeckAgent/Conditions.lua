-- DeckAgent's game conditions: what the checklist (Checklist.lua) ticks on and what hooks (Hooks.lua)
-- fire on. In packets a condition is a kind byte and its numbers (big-endian):
--   0 manual                                   (none)
--   1 quest taken (in the log, or done)        quest ID uint32
--   2 quest ready (ready to turn in, or done)  quest ID uint32
--   3 quest turned in                          quest ID uint32
--   4 objective done                           quest ID uint32, objective uint8 (1-based)
--   5 item count                               item ID uint32, count uint16
--   6 level reached                            level uint8
--   7 in a zone (or a place inside it)         UiMap ID uint16
--   8 XP gained since it was set               XP uint32
-- Kind + 128 means NOT: the condition turns true when the plain one is false ("left Orgrimmar", "no
-- longer carrying it"). Where the game can't tell (no map during a loading screen), it's never true
-- either way, so a loading screen doesn't count as leaving.
--
--   ns.Cond.Read(p, i)            -> kind (with its NOT bit), a, b, next index; or nil, why
--   ns.Cond.Check(kind, a, b, base)  is it true now? (base: ns.Cond.XP() when it was set, for kind 8)
--   ns.Cond.XP()                  XP gained on this character since DeckAgent started counting
--   ns.Cond.OnChange(fn)          fn() once, shortly after anything a condition reads may have changed
local _, ns = ...

local ReadU16, ReadU32 = ns.Bytes.ReadU16, ns.Bytes.ReadU32
local MANUAL, TAKEN, READY, TURNED_IN, OBJECTIVE, ITEMS, LEVEL, ZONE, XP = 0, 1, 2, 3, 4, 5, 6, 7, 8
local ARGS = { [MANUAL] = "", [TAKEN] = "I", [READY] = "I", [TURNED_IN] = "I", [OBJECTIVE] = "IB", [ITEMS] = "IH",
    [LEVEL] = "B", [ZONE] = "H", [XP] = "I" }

local Cond = { MANUAL = MANUAL, XP = XP }
ns.Cond = Cond

DeckAgentDB = DeckAgentDB or {}

local NOT = 128

function Cond.Read(p, i)
    local kind = p:byte(i)
    local args = kind and ARGS[kind % NOT]
    if not args then return nil, "unknown kind " .. tostring(kind) end
    i = i + 1
    local a, b
    for f in args:gmatch(".") do
        local v
        if f == "I" then
            v = #p >= i + 3 and ReadU32(p, i)
            i = i + 4
        elseif f == "H" then
            v = #p >= i + 1 and ReadU16(p, i)
            i = i + 2
        else
            v = p:byte(i)
            i = i + 1
        end
        if not v then return nil, "cut short" end
        if a == nil then a = v else b = v end
    end
    return kind, a, b, i
end

local turnedIn = {} -- quest IDs turned in this session (IsQuestFlaggedCompleted can lag behind)

local function Done(q)
    return turnedIn[q] or (C_QuestLog.IsQuestFlaggedCompleted and C_QuestLog.IsQuestFlaggedCompleted(q)) or false
end
local function OnQuest(q) return C_QuestLog.IsOnQuest and C_QuestLog.IsOnQuest(q) or false end

local function InZone(target)
    local m = C_Map.GetBestMapForUnit("player")
    if not m or m == 0 then return nil end -- can't tell (a loading screen)
    for _ = 1, 8 do
        if not m or m == 0 then return false end
        if m == target then return true end
        local info = C_Map.GetMapInfo(m)
        m = info and info.parentMapID
    end
    return false
end

function Cond.XP() return DeckAgentDB.xpTotal or 0 end

local Plain

function Cond.Check(kind, a, b, base)
    if kind >= NOT then
        local v = Plain(kind - NOT, a, b, base)
        if v == nil then return false end
        return not v
    end
    return Plain(kind, a, b, base) or false
end

function Plain(kind, a, b, base)
    if kind == TAKEN then return OnQuest(a) or Done(a) end
    if kind == READY then return Done(a) or (OnQuest(a) and C_QuestLog.IsComplete(a)) or false end
    if kind == TURNED_IN then return Done(a) end
    if kind == OBJECTIVE then
        if Done(a) or (OnQuest(a) and C_QuestLog.IsComplete(a)) then return true end
        local goals = OnQuest(a) and C_QuestLog.GetQuestObjectives(a)
        return goals and goals[b] and goals[b].finished or false
    end
    if kind == ITEMS then return (C_Item.GetItemCount(a) or 0) >= b end
    if kind == LEVEL then return (UnitLevel("player") or 0) >= a end
    if kind == ZONE then return InZone(a) end
    if kind == XP then return Cond.XP() - (base or 0) >= a end
    return false
end

-- Watching: game events coalesce into one round of callbacks a moment later (bags and the quest log
-- fire in bursts).
local listeners, pending = {}, false

function Cond.OnChange(fn) listeners[#listeners + 1] = fn end

local function Soon()
    if pending then return end
    pending = true
    C_Timer.NewTimer(0.25, function()
        pending = false
        for _, fn in ipairs(listeners) do
            local ok, err = pcall(fn)
            if not ok then geterrorhandler()(err) end
        end
    end)
end
Cond.Soon = Soon

for _, e in ipairs({ "QUEST_ACCEPTED", "QUEST_REMOVED", "QUEST_LOG_UPDATE", "UNIT_QUEST_LOG_CHANGED",
    "BAG_UPDATE_DELAYED", "PLAYER_LEVEL_UP", "ZONE_CHANGED", "ZONE_CHANGED_NEW_AREA", "ZONE_CHANGED_INDOORS" }) do
    ns.On(e, Soon)
end
ns.On("QUEST_TURNED_IN", function(questID)
    if questID then turnedIn[questID] = true end
    Soon()
end)

-- XP: a running total across level-ups, kept in DeckAgentDB.
local lastXP, lastMax
ns.On("PLAYER_XP_UPDATE", function()
    local xp, max = UnitXP("player") or 0, UnitXPMax("player") or 0
    if lastXP then
        local gained = xp - lastXP
        if gained < 0 then gained = (lastMax or 0) - lastXP + xp end -- leveled up
        DeckAgentDB.xpTotal = Cond.XP() + math.max(0, gained)
    end
    lastXP, lastMax = xp, max
    Soon()
end)
ns.On("PLAYER_ENTERING_WORLD", function()
    lastXP, lastMax = UnitXP("player"), UnitXPMax("player")
    Soon()
end)
