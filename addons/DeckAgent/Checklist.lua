-- DeckAgent's checklist tracker: a list the assistant puts up that stays on screen and ticks itself
-- off as the game says so, with no link and no assistant needed. Packets as in Agent.lua's header
-- (binary, big-endian); deck-setup/decklink/agent.py is the other end.
--
-- CHECKLIST (op 11) request:
--   byte 1      action: 1 set (replaces the one up), 2 clear
--   set:        title (length uint8 + UTF-8), count uint8 (1-12), then each item:
--               a condition (Conditions.lua: kind uint8 and its numbers), done uint8 (the start state;
--               only kind 0, manual, keeps it), label (length uint8 + UTF-8)
-- reply body:   byte 3 count, then one byte per item: 1 done now, 0 not
--
-- It shows in DeckKit's tracker (UI.ShowTracker: top left, the quest tracker's marks; DeckAgent's card
-- moves to its right while it's up). It takes no buttons. A newly done item ticks with the quest-complete sound; once
-- all are done it fades out after a minute. It survives a reload (DeckAgentDB.checklist). Cleared by
-- the assistant, the DeckKit menu (Tools > DeckAgent) or a grip chord ("Clear Decky's checklist").
local _, ns = ...

local B, S = ns.Bytes, ns.Status
local U8, Str, ReadStr = B.U8, B.Str, B.ReadStr
local Cond = ns.Cond
local OP_CHECKLIST = 11
local SET, CLEAR = 1, 2
local MAX_ITEMS = 12
local DONE_FADE = 60 -- seconds the finished list stays up
local TICK_SOUND = 878 -- SOUNDKIT.IG_QUEST_LIST_COMPLETE

DeckAgentDB = DeckAgentDB or {}
local Checklist = {}
ns.Checklist = Checklist

local function Clean(s) return (s:gsub("%c", " "):gsub("|", "||")) end

local function Check(item, list)
    if item.kind == Cond.MANUAL then return item.done == true end
    return Cond.Check(item.kind, item.a, item.b, list.base)
end

------------------------------------------------------------------------
-- The panel: DeckKit's tracker (DeckKit/Displays.lua, UI.ShowTracker), one row per item: a check in
-- the quest tracker's marks and the label, done ones grayed. DeckAgent keeps the items' state and the
-- fade itself (XP and manual items are its own), so the tracker gets fixed checks and no fade.
------------------------------------------------------------------------

local fadeTimer

local function Spec(list)
    local items = {}
    for i, item in ipairs(list.items) do
        items[i] = { type = "row", items = {
            { type = "check", done = item.state and true or false },
            { type = "text", text = Clean(item.label or "") } } }
    end
    return { type = "list", title = Clean(list.title or ""), items = items }
end

local function Draw(list)
    DeckKit.UI.ShowTracker(Spec(list), { fade = false })
    local done = 0
    for _, item in ipairs(list.items) do if item.state then done = done + 1 end end
    return done == #list.items
end

function Checklist.Anchor() return DeckKit.UI.TrackerShown() and DeckKitTracker or nil end

function Checklist.Clear()
    DeckAgentDB.checklist = nil
    if fadeTimer then fadeTimer:Cancel(); fadeTimer = nil end
    DeckKit.UI.HideTracker()
end

-- Re-checks every item; a newly done one ticks with the quest-complete sound.
function Checklist.Update(quiet)
    local list = DeckAgentDB.checklist
    if not list then return end
    local any = false
    for _, item in ipairs(list.items) do
        local now = Check(item, list) and true or false
        if now and not item.state then any = true end
        item.state = now
    end
    local all = Draw(list)
    if any and not quiet then PlaySound(TICK_SOUND, "SFX") end
    if all and not fadeTimer then
        fadeTimer = C_Timer.NewTimer(DONE_FADE, function()
            fadeTimer = nil
            local l = DeckAgentDB.checklist
            if l == list then Checklist.Clear() end
        end)
    elseif not all and fadeTimer then
        fadeTimer:Cancel()
        fadeTimer = nil
    end
end

Cond.OnChange(function() Checklist.Update() end)

------------------------------------------------------------------------
-- The op
------------------------------------------------------------------------

local function Parse(p)
    local title, i = ReadStr(p, 3)
    local count = title and p:byte(i)
    if not count or count < 1 or count > MAX_ITEMS then return nil, "a checklist needs a title and 1-12 items" end
    i = i + 1
    local items = {}
    for n = 1, count do
        local kind, a, b
        kind, a, b, i = Cond.Read(p, i)
        if not kind then return nil, "item " .. n .. ": " .. a end
        local done = p:byte(i)
        local label
        label, i = ReadStr(p, i + 1)
        if not (done and label and label ~= "") then return nil, "item " .. n .. " needs a label" end
        items[n] = { kind = kind, a = a, b = b, done = done == 1, label = label }
    end
    return { title = title, items = items, base = Cond.XP() }
end

ns.OPS[OP_CHECKLIST] = function(p)
    local action = p:byte(2)
    if action == CLEAR then
        Checklist.Clear()
        return S.OK, U8(0)
    end
    if action ~= SET then return S.BAD_ARGS, Str("CHECKLIST needs action 1 (set) or 2 (clear)") end
    local list, why = Parse(p)
    if not list then return S.BAD_ARGS, Str(why) end
    if fadeTimer then fadeTimer:Cancel(); fadeTimer = nil end
    DeckAgentDB.checklist = list
    Checklist.Update(true)
    local states = {}
    for i, item in ipairs(list.items) do states[i] = U8(item.state and 1 or 0) end
    return S.OK, U8(#states) .. table.concat(states)
end

------------------------------------------------------------------------
-- Clearing it from the pad: the menu and a grip chord
------------------------------------------------------------------------

if ns.Chords and ns.Chords.OfferPress then
    ns.Chords.OfferPress("DECKAGENT_CHECKLIST", function() Checklist.Clear() end, "Clear Decky's checklist")
end

ns.AddSettings({
    { type = "section", tab = "Tools", label = "DeckAgent" },
    { type = "action", label = "Clear Decky's checklist", verb = "Clear",
      hint = "Takes down the checklist she put up (it also goes by itself a minute after it's all done).",
      run = function() Checklist.Clear() end },
})
