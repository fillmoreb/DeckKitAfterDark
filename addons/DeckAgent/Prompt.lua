-- DeckAgent's prompt: a small window that asks you something and gets an answer from the pad.
--
--   ns.Prompt.Show({
--       title = "Send this for you?",        -- gold, at the top
--       subtitle = "Whisper: Ednalyn",       -- optional, under it
--       text = "want to run SFK later?",     -- optional, the body
--       options = { "Send", "Cancel" },      -- 2: a dialog, A = first, B = second
--       list = false,                        -- true (or 3+ options): a list, d-pad + A, B cancels
--       seconds = 30,                        -- then it closes unanswered
--       onAnswer = function(choice) end,     -- 1..n the option, 0 cancelled (B on a list), nil none
--   })                                       -- returns "shown", or "queued" (in combat: shows after)
--
-- The answer runs inside your own button press (a real hardware event), so it can do what only a press
-- may, like /say outdoors. It reads only the pad's button events: no polling (a polled press isn't a
-- hardware event) and no keyboard (Decktation's dictation presses Enter to open and send chat).
-- While it's up it keeps A, B and (for a list) the d-pad from the game; every other button works as
-- usual. A new prompt replaces one that wasn't answered (that one's onAnswer gets nil). In combat it
-- waits for the fight to end: a window can't take buttons mid-fight.
local _, ns = ...

local Prompt = {}
ns.Prompt = Prompt

local MAX_OPTIONS = 6
local ROW_H = 24

local box, current, timer, draining, queued
local rows = {}

local function Glyph(key)
    local g = ns.Window and ns.Window.Glyph
    return g and g(key, 26) or key
end

local function Build()
    local UI = DeckKit.UI
    local font = UI and UI.Font or {}
    box = CreateFrame("Frame", "DeckAgentPrompt", UIParent)
    box:SetSize(460, 150)
    box:SetPoint("CENTER", 0, 120)
    box:SetFrameStrata("DIALOG")
    box:Hide()
    local bg = box:CreateTexture(nil, "BACKGROUND", nil, -8)
    bg:SetPoint("TOPLEFT", 2, -2)
    bg:SetPoint("BOTTOMRIGHT", -2, 2)
    bg:SetColorTexture(0.035, 0.035, 0.045, 0.96)
    if UI and UI.Border then UI.Border(box, "SimplePanelTemplate") end
    local function Text(fontObject, fallback)
        local fs = box:CreateFontString(nil, "OVERLAY")
        fs:SetFontObject(fontObject or fallback)
        return fs
    end
    box.title = Text(font.title, GameFontNormalLarge)
    box.title:SetPoint("TOP", 0, -12)
    box.subtitle = Text(font.heading, GameFontNormal)
    box.text = Text(font.body, GameFontHighlight)
    box.text:SetJustifyH("LEFT")
    box.text:SetWordWrap(true)
    box.prompts = Text(font.body, GameFontHighlight)
    box.prompts:SetPoint("BOTTOMRIGHT", -16, 12)
    for i = 1, MAX_OPTIONS do
        rows[i] = Text(font.body, GameFontHighlight)
        rows[i]:SetJustifyH("LEFT")
    end

    local function Answer(choice)
        local c = current
        current = nil
        if timer then timer:Cancel(); timer = nil end
        if c and c.onAnswer then
            local ok, err = pcall(c.onAnswer, choice)
            if not ok then geterrorhandler()(err) end
        end
    end
    local function Mark()
        for i, fs in ipairs(rows) do
            if current and current.list and i <= #current.options then
                local on = i == current.selected
                fs:SetText((on and "|cffffd100> " or "|cffffffff   ") .. current.options[i] .. "|r")
            end
        end
    end
    local function Handles(key)
        if key == "PAD1" or key == "PAD2" then return true end
        return current and current.list and (key == "PADDUP" or key == "PADDDOWN")
    end
    local function Down(key)
        if draining then box:SetPropagateKeyboardInput(not draining[key]) return end
        local mine = Handles(key)
        box:SetPropagateKeyboardInput(not mine)
        if not mine then return end
        if key == "PADDUP" or key == "PADDDOWN" then
            local n = #current.options
            current.selected = (current.selected - 1 + (key == "PADDDOWN" and 1 or -1)) % n + 1
            Mark()
            return
        end
        draining = { [key] = true }
        if current and current.list then
            Answer(key == "PAD1" and current.selected or 0)
        else
            Answer(key == "PAD1" and 1 or 2)
        end
    end
    local function Up(key)
        if draining and draining[key] then
            draining = nil
            box:SetPropagateKeyboardInput(true)
            if not current then box:Hide() end
        end
    end
    box:SetScript("OnGamePadButtonDown", function(_, key) Down(key) end)
    box:SetScript("OnGamePadButtonUp", function(_, key) Up(key) end)
    box.Mark = Mark
    box.Answer = Answer
end

local function Open(spec)
    if not box then Build() end
    if current then box.Answer(nil) end -- replaced, unanswered
    current = spec
    draining = nil
    local y = -40
    box.title:SetText(spec.title or "")
    box.subtitle:ClearAllPoints()
    box.subtitle:SetPoint("TOPLEFT", 18, y)
    box.subtitle:SetText(spec.subtitle or "")
    if spec.subtitle and spec.subtitle ~= "" then y = y - 22 end
    box.text:ClearAllPoints()
    box.text:SetPoint("TOPLEFT", 18, y)
    box.text:SetPoint("TOPRIGHT", -18, y)
    box.text:SetText(spec.text or "")
    if spec.text and spec.text ~= "" then y = y - (box.text:GetStringHeight() or 16) - 10 end
    for i, fs in ipairs(rows) do
        fs:ClearAllPoints()
        if spec.list and i <= #spec.options then
            fs:SetPoint("TOPLEFT", 24, y - (i - 1) * ROW_H)
            fs:Show()
        else
            fs:Hide()
        end
    end
    if spec.list then
        y = y - #spec.options * ROW_H
        box.prompts:SetText(Glyph("A") .. " Choose     " .. Glyph("B") .. " Cancel")
    else
        box.prompts:SetText(Glyph("A") .. " " .. spec.options[1] .. "     " .. Glyph("B") .. " " .. spec.options[2])
    end
    box.Mark()
    box:SetHeight(math.max(110, -y + 52))
    box:SetPropagateKeyboardInput(true)
    if box.EnableGamePadButton then box:EnableGamePadButton(true) end
    box:Show()
    if timer then timer:Cancel() end
    timer = C_Timer.NewTimer(spec.seconds or 30, function()
        timer = nil
        if not draining then box:Hide() end
        box.Answer(nil)
    end)
end

function Prompt.Show(spec)
    local options = spec.options or {}
    if #options < 2 or #options > MAX_OPTIONS then error("a prompt needs 2-" .. MAX_OPTIONS .. " options") end
    spec.list = spec.list or #options > 2
    spec.selected = 1
    if InCombatLockdown() then
        if queued and queued.onAnswer then pcall(queued.onAnswer, nil) end
        queued = spec
        return "queued"
    end
    Open(spec)
    return "shown"
end

function Prompt.IsShown() return box ~= nil and box:IsShown() end

ns.On("PLAYER_REGEN_ENABLED", function()
    if queued then
        local spec = queued
        queued = nil
        Open(spec)
    end
end)

-- A prompt open as a fight starts goes away (its buttons can't be taken back mid-fight) and comes back
-- after it, with a fresh timeout.
ns.On("PLAYER_REGEN_DISABLED", function()
    if box and box:IsShown() and current then
        queued = current
        current = nil
        if timer then timer:Cancel(); timer = nil end
        draining = nil
        box:Hide()
    end
end)
