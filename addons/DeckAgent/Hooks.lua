-- DeckAgent's hooks: the assistant asks to hear when something happens in the game ("tell me when he
-- takes quest 79192"). The game only keeps the condition, as numbers (Conditions.lua); what to do about
-- it (the assistant's note) stays on the Deck (deck-setup/decklink/hooks.py). When a hook fires it's
-- queued, and DeckLink's attention flag goes up so a host that only glances at the screen opens the link
-- and collects it. Packets as in Agent.lua's header (binary, big-endian).
--
-- HOOKS (op 12) request:
--   byte 1      action: 1 add, 2 remove, 3 remove all, 4 list
--   add:        a condition (Conditions.lua: kind uint8 and its numbers; not 0, manual), then minutes
--               until it expires, uint16 (0: 240)
--   remove:     hook id uint16
-- reply body:   byte 3 the action, then
--               add: hook id uint16, then 1 if the condition is already true (not added) else 0
--               remove / remove all: how many went, uint8
--               list: count uint8, then each: id uint16, kind uint8, minutes left uint16
--
-- EVENTS (op 13) request: just the op byte. Takes the queued events:
-- reply body:   count uint8, then each: hook id uint16, kind uint8, seconds since it fired uint16
--
-- A hook fires once, then it's gone. Hooks and unclaimed events survive a reload (DeckAgentDB.hooks).
local _, ns = ...

local B, S, Cond = ns.Bytes, ns.Status, ns.Cond
local U8, U16, Str, ReadU16 = B.U8, B.U16, B.Str, B.ReadU16
local OP_HOOKS, OP_EVENTS = 12, 13
local ADD, REMOVE, REMOVE_ALL, LIST = 1, 2, 3, 4
local DEFAULT_MINUTES = 240
local MAX_HOOKS, MAX_EVENTS = 40, 30

DeckAgentDB = DeckAgentDB or {}

local function State()
    local h = DeckAgentDB.hooks
    if not h then
        h = { nextId = 1, list = {}, events = {} }
        DeckAgentDB.hooks = h
    end
    return h
end

local function Now() return GetServerTime and GetServerTime() or time() end

local function Flag()
    if DeckLink.SetAttention then DeckLink.SetAttention("DeckAgent", #State().events > 0) end
end

-- Checks every hook: the true ones fire (queued, then removed); expired ones go quietly.
local function Update()
    local h, now, fired = State(), Now(), false
    for i = #h.list, 1, -1 do
        local hook = h.list[i]
        if now >= hook.expires then
            table.remove(h.list, i)
        elseif Cond.Check(hook.kind, hook.a, hook.b, hook.base) then
            table.remove(h.list, i)
            if #h.events < MAX_EVENTS then
                h.events[#h.events + 1] = { id = hook.id, kind = hook.kind, at = now }
            end
            fired = true
        end
    end
    if fired then ns.Debug("hooks", "fired; events waiting:", #h.events) end
    Flag()
end
Cond.OnChange(Update)

-- Expiry needs a clock even when nothing happens in the game.
C_Timer.NewTicker(30, function() if #State().list > 0 then Update() end end)
ns.On("PLAYER_LOGIN", Flag)

ns.OPS[OP_HOOKS] = function(p)
    local h, action = State(), p:byte(2)
    if action == ADD then
        local kind, a, b, i = Cond.Read(p, 3)
        if not kind then return S.BAD_ARGS, Str("hook: " .. a) end
        if kind == Cond.MANUAL then return S.BAD_ARGS, Str("a hook needs a condition the game can see") end
        if #p < i + 1 then return S.BAD_ARGS, Str("hook: minutes missing") end
        if #h.list >= MAX_HOOKS then return S.REFUSED, Str("too many hooks (" .. MAX_HOOKS .. ")") end
        local minutes = ReadU16(p, i)
        if minutes == 0 then minutes = DEFAULT_MINUTES end
        local base = Cond.XP()
        if Cond.Check(kind, a, b, base) then return S.OK, U8(ADD) .. U16(0) .. U8(1) end
        local id = h.nextId
        h.nextId = id % 65535 + 1
        h.list[#h.list + 1] = { id = id, kind = kind, a = a, b = b, base = base, expires = Now() + minutes * 60 }
        return S.OK, U8(ADD) .. U16(id) .. U8(0)
    elseif action == REMOVE or action == REMOVE_ALL then
        local id = action == REMOVE and #p >= 4 and ReadU16(p, 3)
        if action == REMOVE and not id then return S.BAD_ARGS, Str("remove needs a hook id") end
        local n = 0
        for i = #h.list, 1, -1 do
            if action == REMOVE_ALL or h.list[i].id == id then
                table.remove(h.list, i)
                n = n + 1
            end
        end
        return S.OK, U8(action) .. U8(n)
    elseif action == LIST then
        local now, parts = Now(), {}
        for i, hook in ipairs(h.list) do
            parts[i] = U16(hook.id) .. U8(hook.kind) .. U16(math.max(0, math.floor((hook.expires - now) / 60)))
        end
        return S.OK, U8(LIST) .. U8(#parts) .. table.concat(parts)
    end
    return S.BAD_ARGS, Str("HOOKS needs an action 1-4")
end

ns.OPS[OP_EVENTS] = function()
    local h, now, parts = State(), Now(), {}
    for i, e in ipairs(h.events) do
        parts[i] = U16(e.id) .. U8(e.kind) .. U16(math.min(65535, math.max(0, now - e.at)))
    end
    h.events = {}
    Flag()
    return S.OK, U8(#parts) .. table.concat(parts)
end

ns.Hooks = { Update = Update }
