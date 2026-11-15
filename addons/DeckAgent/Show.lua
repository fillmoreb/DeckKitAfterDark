-- DeckAgent's displays: the assistant shows you things. Packets as in Agent.lua's header (binary,
-- big-endian); deck-setup/decklink/agent.py is the other end.
--
-- SHOW (op 7) request:
--   byte 1      kind: 1 chat echo, 2 card, 3 reading view
--   byte 2      card: seconds on screen, 0 until you close it (other kinds ignore it)
--   bytes 3-6   card: an item ID, shown first (its icon and name, its tooltip on Y), uint32 (0 none)
--   then        title: length uint8 + UTF-8 (chat echo: the gray line above the answer, as it should
--               read, e.g. "You asked: ..." or "Hook: ..."; may be empty)
--   then        text: length uint16 + UTF-8 (chat echo: the answer)
-- reply body:   byte 3: 1 shown, 2 queued (a reading view in combat shows when the fight ends)
--               byte 4: voice: 0 speak answers, 1 quiet (show them only; DeckKit menu)
--
--   chat echo   the title line in gray, then "Decky: ..." in the chat frame (local only, nobody
--               else sees it), so answers can be read again in the chat peek and chat history.
--   card        DeckKit's card (UI.ShowCard: top left, right of a tracker), the text's markup as layout
--               pieces: title, a few lines (lists as below). It goes after its seconds, or on {B}. In
--               combat it still shows, but without {B} (B is the game's mid-fight): it waits out its time.
--   reading     a bigger window for long text: the d-pad scrolls, LB/RB turn pages, {B} closes. It
--               takes those buttons only, never the sticks. In combat it waits for the fight to end.
--   Both: "# " starts a heading, "- " a bullet, "[ ] " a checklist item to do, "[x] " one done (a
--   check, the line grayed). The checks are the quest tracker's own.
--   layout      (kind 4) text is a DeckKit layout as JSON (DeckKit/Layout.lua's header has the node types),
--               shown in DeckKit's layout window, titled Decky. The host swaps reply values for numbers
--               1-n; a reply sends ANSWER (Ask.lua's op 6) with this frame's seq and that number and
--               closes the window, B closing it sends 0, a newer display replacing it 255. byte 2
--               (seconds) > 0: the host is waiting for that answer. The title isn't used (the layout has
--               its own). Your own ticks are kept per layout `id` (DeckAgentDB.layoutTicks). In combat it
--               waits for the fight to end, and one closed by a fight comes back after it.
-- A new card replaces the one up, a new reading view the one up.
local _, ns = ...

local B, S = ns.Bytes, ns.Status
local U8, Str, ReadU16, ReadU32 = B.U8, B.Str, B.ReadU16, B.ReadU32
local OP_SHOW = 7
local ECHO, CARD, READING, LAYOUT = 1, 2, 3, 4
local SHOWN, QUEUED = 1, 2
local DECKY = "|cffe8b04bDecky:|r " -- the assistant's name (agent/SYSTEM.md), in DeckAgent's color

DeckAgentDB = DeckAgentDB or {}
local function Quiet() return DeckAgentDB.quiet == true end
ns.Quiet = Quiet

-- Text from outside: no control characters but newlines, and "|" shown as itself (it starts the
-- game's escape codes).
local function Clean(s, keepLines)
    s = s:gsub("\r", "")
    s = keepLines and s:gsub("[%z\1-\9\11-\31]", " ") or s:gsub("%c", " ")
    return (s:gsub("|", "||"))
end

local function Glyph(key)
    local g = ns.Window and ns.Window.Glyph
    return g and g(key, 26) or key
end

local function Panel(name, width, strata)
    local UI = DeckKit.UI
    local f = CreateFrame("Frame", name, UIParent)
    f:SetSize(width, 100)
    f:SetFrameStrata(strata)
    f:Hide()
    local bg = f:CreateTexture(nil, "BACKGROUND", nil, -8)
    bg:SetPoint("TOPLEFT", 2, -2)
    bg:SetPoint("BOTTOMRIGHT", -2, 2)
    bg:SetColorTexture(0.035, 0.035, 0.045, 0.94)
    if UI and UI.Border then UI.Border(f, "SimplePanelTemplate") end
    return f
end

local function Font(f, kind, fallback)
    local font = DeckKit.UI and DeckKit.UI.Font or {}
    local fs = f:CreateFontString(nil, "OVERLAY")
    fs:SetFontObject(font[kind] or fallback)
    return fs
end

-- List markup (after Clean): headings, bullets, checklists.
local TODO, DONE = "|A:checkbox-minimal:14:14|a ", "|A:Tracker-Check:14:14|a "
local function Markup(text)
    local out = {}
    for line in (text .. "\n"):gmatch("(.-)\n") do
        local h = line:match("^#+%s+(.*)")
        local mark, rest = line:match("^%s*%[([ xX]?)%]%s*(.*)")
        if h then
            out[#out + 1] = "|cffffd100" .. h .. "|r"
        elseif mark then
            out[#out + 1] = (mark == "" or mark == " ") and TODO .. rest or DONE .. "|cff9d9d9d" .. rest .. "|r"
        else
            out[#out + 1] = (line:gsub("^%s*[-*]%s+", "  \226\128\162 "))
        end
    end
    return table.concat(out, "\n")
end

------------------------------------------------------------------------
-- Buttons: a frame that takes only the buttons it names, out of combat (like Prompt.lua)
------------------------------------------------------------------------

-- catcher(handles) -> frame; handles[key] = fn(key) on press, fn(key, true) repeated while held.
local function Catcher(name, handles, repeats)
    local c = CreateFrame("Frame", name, UIParent)
    c:SetSize(1, 1)
    c:SetPoint("TOPLEFT")
    c:Hide()
    local held, nextAt
    c:SetScript("OnGamePadButtonDown", function(self, key)
        local fn = handles[key]
        self:SetPropagateKeyboardInput(not fn)
        if not fn then return end
        fn(key)
        if repeats and repeats[key] then held, nextAt = key, GetTime() + 0.35 end
    end)
    c:SetScript("OnGamePadButtonUp", function(self, key)
        if key == held then held = nil end
        self:SetPropagateKeyboardInput(true)
    end)
    c:SetScript("OnUpdate", function()
        if held and GetTime() >= nextAt then
            nextAt = GetTime() + 0.06
            handles[held](held, true)
        end
    end)
    c:SetScript("OnHide", function(self) held = nil end)
    function c:Take(on)
        if on and not InCombatLockdown() then
            self:SetPropagateKeyboardInput(true)
            if self.EnableGamePadButton then self:EnableGamePadButton(true) end
            self:Show()
        else
            self:Hide()
        end
    end
    return c
end

------------------------------------------------------------------------
-- Chat echo
------------------------------------------------------------------------

local function Echo(question, answer)
    local cf = DEFAULT_CHAT_FRAME or ChatFrame1
    if not cf then return end
    if question ~= "" then cf:AddMessage(Clean(question), 0.6, 0.6, 0.6) end
    if answer ~= "" then cf:AddMessage(DECKY .. Clean(answer), 1, 1, 1) end
end

------------------------------------------------------------------------
-- Card: DeckKit's (DeckKit/Displays.lua, UI.ShowCard), with the text's markup as layout pieces
------------------------------------------------------------------------

-- "# Heading", "- bullet", "[ ] to do" / "[x] done" and plain lines -> a layout (after Clean). Plain
-- lines in a row stay one paragraph.
local function CardSpec(title, text, itemID)
    local items, para = {}, {}
    local function Flush()
        if #para > 0 then items[#items + 1] = { type = "text", text = table.concat(para, "\n") } end
        para = {}
    end
    if itemID and itemID > 0 then items[1] = { type = "item", id = itemID } end
    for line in (text .. "\n"):gmatch("(.-)\n") do
        local h = line:match("^#+%s+(.*)")
        local mark, rest = line:match("^%s*%[([ xX]?)%]%s*(.*)")
        if h then
            Flush()
            items[#items + 1] = { type = "heading", text = h }
        elseif mark then
            Flush()
            items[#items + 1] = { type = "row", items = { { type = "check", done = mark == "x" or mark == "X" },
                { type = "text", text = rest } } }
        elseif line:match("^%s*[-*]%s+") then
            para[#para + 1] = (line:gsub("^%s*[-*]%s+", "  \226\128\162 "))
        else
            para[#para + 1] = line
        end
    end
    Flush()
    return { type = "list", title = title, width = 400, items = items }
end

local function ShowCard(title, text, seconds, itemID)
    DeckKit.UI.ShowCard(CardSpec(Clean(title), Clean(text, true), itemID), { seconds = seconds })
end
ns.CardSpec = CardSpec -- (for the tests)

------------------------------------------------------------------------
-- Reading view
------------------------------------------------------------------------

local reader, readCatch, readQueued
local LINE = 3 * 16 -- a d-pad press scrolls about three lines

local function Scroll(by)
    local sf = reader.scroll
    local page = math.max(1, sf:GetHeight() or 1)
    local max = math.max(0, (reader.body:GetStringHeight() or 0) - page)
    local y = math.max(0, math.min(max, (sf:GetVerticalScroll() or 0) + by))
    sf:SetVerticalScroll(y)
    local pages = math.max(1, math.ceil((max + page) / page - 0.05))
    local at = y >= max and pages or math.min(pages, math.floor(y / page + 0.05) + 1)
    reader.page:SetText(pages > 1 and ("Page %d of %d"):format(at, pages) or "")
end

local function HideReader()
    readQueued = nil
    if reader then reader:Hide() end
    if readCatch then readCatch:Take(false) end
end

local function BuildReader()
    reader = Panel("DeckAgentReader", 620, "DIALOG")
    reader:SetSize(620, 470)
    reader:SetPoint("CENTER", 0, 40)
    reader.title = Font(reader, "title", GameFontNormalLarge)
    reader.title:SetPoint("TOPLEFT", 18, -14)
    reader.title:SetPoint("TOPRIGHT", -18, -14)
    reader.title:SetJustifyH("LEFT")
    local sf = CreateFrame("ScrollFrame", nil, reader)
    sf:SetPoint("TOPLEFT", 18, -44)
    sf:SetPoint("BOTTOMRIGHT", -18, 46)
    local child = CreateFrame("Frame", nil, sf)
    child:SetSize(584, 10)
    sf:SetScrollChild(child)
    reader.scroll, reader.child = sf, child
    reader.body = Font(child, "body", GameFontHighlight)
    reader.body:SetPoint("TOPLEFT")
    reader.body:SetWidth(584)
    reader.body:SetJustifyH("LEFT")
    reader.body:SetWordWrap(true)
    reader.body:SetSpacing(3)
    reader.page = Font(reader, "dim", GameFontDisable)
    reader.page:SetPoint("BOTTOMLEFT", 18, 14)
    reader.hint = Font(reader, "body", GameFontHighlight)
    reader.hint:SetPoint("BOTTOMRIGHT", -16, 12)
    reader.hint:SetText(Glyph("DPAD_UD") .. " Scroll    " .. Glyph("LB") .. Glyph("RB") .. " Page    "
        .. Glyph("B") .. " Close")
    local function Page(key) Scroll((key == "PADLSHOULDER" and -1 or 1) * ((sf:GetHeight() or 0) - 16)) end
    local function Line(key) Scroll(key == "PADDUP" and -LINE or LINE) end
    readCatch = Catcher("DeckAgentReaderButtons",
        { PAD2 = HideReader, PADDUP = Line, PADDDOWN = Line, PADLSHOULDER = Page, PADRSHOULDER = Page },
        { PADDUP = true, PADDDOWN = true })
end

local function OpenReader(spec)
    if not reader then BuildReader() end
    reader.spec = spec
    reader.title:SetText(Clean(spec.title))
    reader.body:SetText(Markup(Clean(spec.text, true)))
    reader.child:SetHeight(math.max(10, reader.body:GetStringHeight() or 0))
    reader.scroll:SetVerticalScroll(0)
    reader:Show()
    Scroll(0)
    readCatch:Take(true)
end

local function ShowReader(title, text)
    local spec = { title = title, text = text }
    if InCombatLockdown() then
        readQueued = spec
        return QUEUED
    end
    OpenReader(spec)
    return SHOWN
end

ns.On("PLAYER_REGEN_DISABLED", function()
    if reader and reader:IsShown() then -- it comes back after the fight, at the top
        readQueued = reader.spec
        reader:Hide()
        readCatch:Take(false)
    end
end)

ns.On("PLAYER_REGEN_ENABLED", function()
    if readQueued then
        local spec = readQueued
        readQueued = nil
        OpenReader(spec)
    end
end)

------------------------------------------------------------------------
-- Layout
------------------------------------------------------------------------

local layoutQueued -- { spec, seq, waits }: shows when the fight ends
local TICKS_KEPT = 20

local function TicksFor(spec)
    DeckAgentDB.layoutTicks = DeckAgentDB.layoutTicks or {}
    local all, key = DeckAgentDB.layoutTicks, tostring(spec.id or spec.title or "layout")
    if not all[key] then
        local n = 0
        for _ in pairs(all) do n = n + 1 end
        if n >= TICKS_KEPT then all[next(all)] = nil end -- an old one makes room
        all[key] = {}
    end
    return all[key]
end

local function OpenLayout(q)
    local answered = false
    local function Answer(choice)
        if answered then return end
        answered = true
        if q.waits then ns.SendAnswer(q.seq, choice) end
    end
    return DeckKit.UI.ShowLayout(q.spec, {
        title = "Decky", ticks = TicksFor(q.spec),
        onReply = function(v)
            Answer(tonumber(v) or 0)
            DeckKit.UI.HideLayout()
        end,
        onClose = function(why)
            if why == "closed" and (InCombatLockdown() or UnitAffectingCombat("player")) then
                layoutQueued = q -- a fight took it down: back after the fight, still waiting
                return
            end
            Answer(why == "replaced" and 255 or 0)
        end,
    })
end

local function ShowLayout(spec, seq, waits)
    local q = { spec = spec, seq = seq, waits = waits }
    local old = layoutQueued
    if old and old.waits then ns.SendAnswer(old.seq, 255) end -- replaced before it could show
    layoutQueued = nil
    if InCombatLockdown() then
        layoutQueued = q
        return QUEUED
    end
    OpenLayout(q)
    return SHOWN
end

ns.On("PLAYER_REGEN_ENABLED", function()
    if layoutQueued then
        local q = layoutQueued
        layoutQueued = nil
        OpenLayout(q)
    end
end)

------------------------------------------------------------------------
-- The op
------------------------------------------------------------------------

ns.OPS[OP_SHOW] = function(p, seq)
    local kind, seconds = p:byte(2), p:byte(3)
    if not (kind and seconds) or #p < 8 then return S.BAD_ARGS, Str("SHOW needs kind, seconds and icon") end
    local itemID = ReadU32(p, 4)
    local n = p:byte(8)
    local title = p:sub(9, 8 + n)
    local i = 9 + n
    if #title ~= n or i + 1 > #p then return S.BAD_ARGS, Str("SHOW's title or text is cut short") end
    local len = ReadU16(p, i)
    local text = p:sub(i + 2, i + 1 + len)
    if #text ~= len then return S.BAD_ARGS, Str("SHOW's text is cut short") end
    local result = SHOWN
    if kind == ECHO then
        Echo(title, text)
    elseif kind == CARD then
        if text == "" and title == "" then return S.BAD_ARGS, Str("an empty card") end
        ShowCard(title, text, seconds, itemID)
    elseif kind == READING then
        if text == "" then return S.BAD_ARGS, Str("nothing to read") end
        result = ShowReader(title, text)
    elseif kind == LAYOUT then
        local spec, why = DeckKit.JSON.Decode(text)
        if type(spec) ~= "table" or type(spec.type) ~= "string" then
            return S.BAD_ARGS, Str("layout: " .. (why or "not a layout (no type)"))
        end
        result = ShowLayout(spec, seq, seconds > 0)
    else
        return S.BAD_ARGS, Str("unknown kind " .. kind)
    end
    return S.OK, U8(result) .. U8(Quiet() and 1 or 0)
end
