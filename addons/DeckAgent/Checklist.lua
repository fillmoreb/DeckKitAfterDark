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
-- The panel sits at the top left in the quest tracker's look (DeckAgent's card moves to its right
-- while it's up). It takes no buttons. A newly done item ticks with the quest-complete sound; once
-- all are done it fades out after a minute. It survives a reload (DeckAgentDB.checklist). Cleared by
-- the assistant, the DeckKit menu (Tools > DeckAgent) or a grip chord ("Clear Decky's checklist").
local _, ns = ...

local B, S = ns.Bytes, ns.Status
local U8, Str = B.U8, B.Str
local Cond = ns.Cond
local OP_CHECKLIST = 11
local SET, CLEAR = 1, 2
local MAX_ITEMS = 12
local DONE_FADE = 60 -- seconds the finished list stays up
local TICK_SOUND = 878 -- SOUNDKIT.IG_QUEST_LIST_COMPLETE
local TODO, CHECK = "|A:checkbox-minimal:14:14|a", "|A:Tracker-Check:14:14|a"

DeckAgentDB = DeckAgentDB or {}
local Checklist = {}
ns.Checklist = Checklist

local function Clean(s) return (s:gsub("%c", " "):gsub("|", "||")) end

local function Check(item, list)
    if item.kind == Cond.MANUAL then return item.done == true end
    return Cond.Check(item.kind, item.a, item.b, list.base)
end

------------------------------------------------------------------------
-- The panel
------------------------------------------------------------------------

local panel, rows, fadeTimer = nil, {}, nil

local function Build()
    local UI = DeckKit.UI
    local font = UI and UI.Font or {}
    panel = CreateFrame("Frame", "DeckAgentChecklist", UIParent)
    panel:SetSize(300, 60)
    panel:SetPoint("TOPLEFT", 24, -70)
    panel:SetFrameStrata("MEDIUM")
    panel:Hide()
    local bg = panel:CreateTexture(nil, "BACKGROUND", nil, -8)
    bg:SetAllPoints()
    bg:SetColorTexture(0, 0, 0, 0.45)
    panel.title = panel:CreateFontString(nil, "OVERLAY")
    panel.title:SetFontObject(font.heading or GameFontNormal)
    panel.title:SetPoint("TOPLEFT", 10, -8)
    panel.title:SetPoint("RIGHT", panel, "RIGHT", -10, 0)
    panel.title:SetJustifyH("LEFT")
    for i = 1, MAX_ITEMS do
        local fs = panel:CreateFontString(nil, "OVERLAY")
        fs:SetFontObject(font.body or GameFontHighlight)
        fs:SetJustifyH("LEFT")
        fs:SetWordWrap(true)
        rows[i] = fs
    end
end

local function Draw(list, flash)
    if not panel then Build() end
    local done, y = 0, -30
    panel.title:SetText(Clean(list.title or ""))
    for i, fs in ipairs(rows) do
        local item = list.items[i]
        fs:ClearAllPoints()
        if item then
            fs:SetPoint("TOPLEFT", 12, y)
            fs:SetPoint("RIGHT", panel, "RIGHT", -10, 0)
            local label = Clean(item.label or "")
            fs:SetText(item.state and (CHECK .. " |cff9d9d9d" .. label .. "|r") or (TODO .. " " .. label))
            fs:SetAlpha(flash and flash[i] and 0.35 or 1)
            if flash and flash[i] and UIFrameFadeIn then UIFrameFadeIn(fs, 0.6, 0.35, 1) end
            fs:Show()
            y = y - (fs:GetStringHeight() or 16) - 4
            if item.state then done = done + 1 end
        else
            fs:Hide()
        end
    end
    panel:SetHeight(-y + 8)
    panel:Show()
    if ns.Card and ns.Card.Reanchor then ns.Card.Reanchor() end
    return done == #list.items
end

function Checklist.Anchor() return panel and panel:IsShown() and panel or nil end

function Checklist.Clear()
    DeckAgentDB.checklist = nil
    if fadeTimer then fadeTimer:Cancel(); fadeTimer = nil end
    if panel then panel:Hide() end
    if ns.Card and ns.Card.Reanchor then ns.Card.Reanchor() end
end

-- Re-checks every item; ticks the newly done ones (sound, a short fade in).
function Checklist.Update(quiet)
    local list = DeckAgentDB.checklist
    if not list then return end
    local flash, any = {}, false
    for i, item in ipairs(list.items) do
        local now = Check(item, list) and true or false
        if now and not item.state then flash[i], any = true, true end
        item.state = now
    end
    local all = Draw(list, (not quiet) and flash or nil)
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

local function ReadStr(p, i)
    local n = p:byte(i)
    if not n or i + n > #p then return nil end
    return p:sub(i + 1, i + n), i + 1 + n
end

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
