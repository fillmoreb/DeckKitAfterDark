-- DeckAgent's widgets and ledger: the assistant builds live displays from DeckKit's pieces (DeckKit.Widgets: layouts
-- whose stat pieces follow DeckKit.Measures) and reads what you've gained (DeckKit.Ledger). The game side is all
-- DeckKit's; this file packs it. Specs and measures are JSON (DeckKit.JSON), never code.
-- Packets as in Agent.lua's header (binary, big-endian); deck-setup/decklink/agent.py is the other end.
--
-- WIDGET (op 22) request:
--   byte 1      action: 1 show, 2 hide, 3 list, 4 delete
--   show:       byte 2 flags: 1 save it (an action for grip slots and touch grids), 2 for the whole account (else
--               this character); then name str, mark str (empty: none; a DeckKit.Ledger mark set afresh each
--               time it opens), then the layout: length uint16 + JSON
--   hide:       name str
--   delete:     byte 2 scope: 0 whichever has it, 1 the character's, 2 the account's; then name str
-- reply body:   show / hide / delete: byte 3 1 = done
--               list: byte 3 count, each name str and flags uint8 (1 the account's, 2 shown)
--
-- LEDGER (op 23) request:
--   byte 1      action: 1 values, 2 totals, 3 hidden counts
--   values:     measures: length uint16 + a JSON array of DeckKit.Measures tables (at most 20)
--   totals:     byte 2 kind (1 loot, 2 created, 3 coin, 4 money, 5 xp, 6 rep, 7 skill, 8 quest), bytes 3-4 minutes
--               uint16 (0: this session), byte 5 scope (1 account)
-- reply body:   values: byte 3 count, each value str (a number as text, "" when it can't be told)
--               totals: byte 3 bit 0: more didn't fit; byte 4 count, each key str (item ID, faction or skill
--               name, or "1"), amount str (a number as text)
--               hidden counts: byte 3 count, each kind str, context str ("combat/instance"...), seen uint16,
--               hidden uint16: what the game withheld from DeckKit's ledger this session
-- DISPLAYS (op 24): what of the assistant's is on screen, so she can take it down (and knows about one she
-- didn't remember making). Her widgets come from DeckKit.Widgets (saved ones, shown or not, and any shown unsaved);
-- her checklist, card, reading view and layout window from this addon.
--   byte 1      action: 1 list, 2 hide one, 3 hide all
--   hide one:   byte 2 kind, then name str (a widget's name; ignored for the others)
-- reply body:   list: byte 3 count, each kind uint8 (1 widget, 2 checklist, 3 card, 4 reading view, 5 window),
--               name str (a widget's name, else the title shown), flags uint8 (1 saved, 2 the account's, 4 on
--               screen)
--               hide one: byte 3 1 = it was up and went, 0 = nothing of that kind was up
--               hide all: byte 3 how many went
--   Hiding never forgets a saved widget (WIDGET delete does) and never touches anyone else's displays.
-- (str = length uint8 + UTF-8)
local _, ns = ...

local B, S = ns.Bytes, ns.Status
local U8, U16, Str, ReadStr, ReadU16 = B.U8, B.U16, B.Str, B.ReadStr, B.ReadU16
local OP_WIDGET, OP_LEDGER, OP_DISPLAYS = 22, 23, 24
local WIDGET, CHECKLIST, CARD, READER, WINDOW = 1, 2, 3, 4, 5
local SAVED, ITS_ACCOUNT, ON_SCREEN = 1, 2, 4
local SAVE, ACCOUNT = 1, 2
local KINDS = { "loot", "created", "coin", "money", "xp", "rep", "skill", "quest" }
local MAX_MEASURES = 20

local function Json(p, i)
    local n = ReadU16(p, i)
    if not n or #p < i + 1 + n then return nil end
    local ok, v = pcall(DeckKit.JSON.Decode, p:sub(i + 2, i + 1 + n))
    if ok then return v end
end

local function Number(v)
    if type(v) ~= "number" then return "" end
    if v == math.floor(v) then return tostring(v) end
    return ("%.2f"):format(v)
end

ns.OPS[OP_WIDGET] = function(p)
    local W = DeckKit.Widgets
    if not W then return S.FAILED, Str("this DeckKit has no widgets") end
    local action = p:byte(2)
    if action == 1 then
        local flags = p:byte(3) or 0
        local name, i = ReadStr(p, 4)
        local mark
        if name then mark, i = ReadStr(p, i) end
        local spec = i and Json(p, i)
        if not (name and name ~= "" and type(spec) == "table") then return S.BAD_ARGS, Str("WIDGET show needs a name and a layout") end
        mark = mark ~= "" and mark or nil
        local scope = flags % 4 >= ACCOUNT and "account" or nil
        local ok, why
        if flags % 2 >= SAVE then
            ok, why = W.Save(name, spec, { scope = scope, mark = mark })
            if ok then ok, why = W.Show(name) end
        else
            ok, why = W.ShowSpec(name, spec, { scope = scope, mark = mark })
        end
        if not ok then return S.REFUSED, Str(why or "not shown") end
        return S.OK, U8(1)
    elseif action == 2 then
        local name = ReadStr(p, 3)
        if not name then return S.BAD_ARGS, Str("WIDGET hide needs a name") end
        W.Hide(name)
        return S.OK, U8(1)
    elseif action == 3 then
        local parts, list = {}, W.List()
        for n = 1, math.min(#list, 255) do
            local w = list[n]
            parts[#parts + 1] = Str(w.name) .. U8((w.scope == "account" and 1 or 0) + (w.shown and 2 or 0))
        end
        return S.OK, U8(#parts) .. table.concat(parts)
    elseif action == 4 then
        local scope = ({ [1] = "character", [2] = "account" })[p:byte(3) or 0]
        local name = ReadStr(p, 4)
        if not name then return S.BAD_ARGS, Str("WIDGET delete needs a name") end
        local ok, why = W.Delete(name, scope)
        if not ok then return S.REFUSED, Str(why) end
        return S.OK, U8(1)
    end
    return S.BAD_ARGS, Str("unknown WIDGET action")
end

ns.OPS[OP_LEDGER] = function(p)
    local L, M = DeckKit.Ledger, DeckKit.Measures
    if not (L and M) then return S.FAILED, Str("this DeckKit has no ledger") end
    local action = p:byte(2)
    if action == 1 then
        local list = Json(p, 3)
        if type(list) ~= "table" or #list == 0 or #list > MAX_MEASURES then
            return S.BAD_ARGS, Str("LEDGER values needs 1-" .. MAX_MEASURES .. " measures")
        end
        local parts = {}
        for n, m in ipairs(list) do parts[n] = Str(Number(M.Value(m))) end
        return S.OK, U8(#parts) .. table.concat(parts)
    elseif action == 2 then
        local kind = KINDS[p:byte(3) or 0]
        local minutes = ReadU16(p, 4) or 0
        if not kind then return S.BAD_ARGS, Str("unknown ledger kind") end
        local since = minutes > 0 and (GetServerTime() - minutes * 60) or nil
        local totals = L.Totals(kind, since, p:byte(6) == 1 and "account" or nil)
        local keys = {}
        for k in pairs(totals) do keys[#keys + 1] = k end
        table.sort(keys, function(a, b) return math.abs(totals[a]) > math.abs(totals[b]) end) -- biggest first
        local entries, budget, more = {}, DeckLink.MaxPayload() - 5, false
        for _, k in ipairs(keys) do
            local e = Str(tostring(k)) .. Str(Number(totals[k]))
            if #e > budget then more = true break end
            budget = budget - #e
            entries[#entries + 1] = e
        end
        return S.OK, U8(more and 1 or 0) .. U8(#entries) .. table.concat(entries)
    elseif action == 3 then
        local parts = {}
        for kind, contexts in pairs(L.Diagnostics()) do
            for context, c in pairs(contexts) do
                parts[#parts + 1] = Str(kind) .. Str(context) .. U16(math.min(c.seen, 65535)) .. U16(math.min(c.hidden, 65535))
            end
        end
        return S.OK, U8(math.min(#parts, 255)) .. table.concat(parts, "", 1, math.min(#parts, 255))
    end
    return S.BAD_ARGS, Str("unknown LEDGER action")
end

-- Everything of hers: { kind, name, flags }.
local function Mine()
    local out = {}
    for _, w in ipairs(DeckKit.Widgets and DeckKit.Widgets.List() or {}) do
        out[#out + 1] = { kind = WIDGET, name = w.name, flags = (w.saved and SAVED or 0)
            + (w.scope == "account" and ITS_ACCOUNT or 0) + (w.shown and ON_SCREEN or 0) }
    end
    local list = DeckAgentDB and DeckAgentDB.checklist
    if list then out[#out + 1] = { kind = CHECKLIST, name = list.title or "", flags = ON_SCREEN } end
    for kind, get in pairs({ [CARD] = ns.Mine.card, [READER] = ns.Mine.reader, [WINDOW] = ns.Mine.window }) do
        local title = get()
        if title then out[#out + 1] = { kind = kind, name = title, flags = ON_SCREEN } end
    end
    table.sort(out, function(a, b) if a.kind ~= b.kind then return a.kind < b.kind end return a.name < b.name end)
    return out
end

-- Takes one down: true if it was up.
local function HideOne(kind, name)
    if kind == WIDGET then
        if not (name and DeckKit.Widgets.Shown(name)) then return false end
        DeckKit.Widgets.Hide(name)
        return true
    elseif kind == CHECKLIST then
        if not (DeckAgentDB and DeckAgentDB.checklist) then return false end
        ns.Checklist.Clear()
        return true
    end
    local which = ({ [CARD] = "card", [READER] = "reader", [WINDOW] = "window" })[kind]
    if not which or not ns.Mine[which]() then return false end
    ns.Mine.Hide(which)
    return true
end

ns.OPS[OP_DISPLAYS] = function(p)
    local action = p:byte(2)
    if action == 1 then
        local parts = {}
        for _, d in ipairs(Mine()) do
            if #parts < 255 then parts[#parts + 1] = U8(d.kind) .. Str(d.name:sub(1, 60)) .. U8(d.flags) end
        end
        return S.OK, U8(#parts) .. table.concat(parts)
    elseif action == 2 then
        local kind = p:byte(3)
        local name = ReadStr(p, 4)
        if not kind or kind < 1 or kind > 5 then return S.BAD_ARGS, Str("unknown display kind") end
        return S.OK, U8(HideOne(kind, name) and 1 or 0)
    elseif action == 3 then
        local n = 0
        for _, d in ipairs(Mine()) do
            if d.flags % 8 >= ON_SCREEN and HideOne(d.kind, d.name) then n = n + 1 end
        end
        return S.OK, U8(n)
    end
    return S.BAD_ARGS, Str("unknown DISPLAYS action")
end
