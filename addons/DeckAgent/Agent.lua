-- DeckAgent: the game side of DeckAgent, the gaming assistant (agent/ in this repo). The assistant runs
-- outside the game and reaches it only through the requests below, on DeckLink channel 3. A request is
-- a small binary packet with fields at fixed places: an op from a fixed list and its numbers. Never code.
-- deck-setup/decklink/agent.py is the other end; keep the two in step (test/test_agent.py checks bytes).
--
-- All numbers are big-endian (network order), like DeckLink's own length field.
--
-- Request (host -> game, through the keys):
--   byte 0      op byte: top 2 bits the format version (1), low 6 bits the op
--   STATE (op 1):  byte 1  field mask: which fields to send back
--                    bit 0 POS     map uint16, x uint16, y uint16 (0-65535 = 0-100% of the map)
--                    bit 1 WORLD   continent uint8, world x int16, world y int16 (yards)
--                    bit 2 ZONE    length uint8, zone name (UTF-8)
--                    bit 3 WHO     faction uint8 (0 none, 1 Alliance, 2 Horde), level uint8, class ID uint8
--                    bit 4 COMBAT  uint8 (1 in combat)
--                    bit 5 BRAIN   uint8, the model picked in the DeckKit menu (Tools > DeckAgent):
--                                  0 the default, 1 Haiku, 2 Sonnet, 3 Opus
--                    bit 6 VOICE   uint8, answers: 0 spoken, 1 quiet (shown only; DeckKit menu)
--                    bit 7 TARGET  NPC ID uint32 (0: none, or not an NPC), level uint8 (255: ??), flags
--                                  uint8 (1 a target, 2 attackable, 4 dead, 8 elite, 16 rare, 32 a
--                                  player, 64 tapped by someone else), health percent uint8
--   WAY (op 2):    bytes 1-2 map uint16, 3-4 x uint16, 5-6 y uint16: a WayPad waypoint, made active
--                  (the game's own map pin when WayPad is missing)
--   WAYPOINTS (op 10): byte 1 action: 1 list, 2 remove the one with id (bytes 2-3, uint16), 3 remove
--                  the active one, 4 remove all. WayPad's waypoints (without WayPad: the game's map pin,
--                  which "active" and "all" clear)
--
-- Reply (game -> host, on the strip):
--   byte 0      the request's op byte
--   byte 1      the request's frame seq
--   byte 2      status: 0 ok, 1 unknown op, 2 bad arguments, 3 refused, 4 failed in the game,
--               5 unknown version
--   then        STATE: the asked-for fields, in mask-bit order (a field the game can't give is sent
--               zeroed, and its bit is cleared in the echoed mask at byte 3)
--               WAY: byte 3 how it was set (1 WayPad, 2 map pin), bytes 4-5 WayPad's id for the
--               waypoint, uint16 (stable, for later requests about it; 0 for the map pin), then its route
--               (DeckKit.Route, with ForeverNav): step count uint8 (0: none, a straight walk), and when
--               there are steps the whole trip's seconds uint16, then each step's seconds uint16 and text
--               (length uint8, UTF-8; "Take the zeppelin to Grom'gol Zeppelin Tower"), then a flags byte (1: its
--               walks were measured; 0: the walks are straight-line estimates, so the trip is at least that long).
--               Steps that don't fit the reply are left off (the count says how many came).
--               WAYPOINTS: byte 3 how many were removed, then the list as it is now: count uint8,
--               active id uint16 (0 none), then each: id uint16, map uint16, x uint16, y uint16
--               (no labels: names come from the assistant's own database)
--               CHAT, FRIENDS: see Chat.lua
--               any failure: length uint8, a short reason (UTF-8)
--
-- CHAT (op 3) and FRIENDS (op 4) are in Chat.lua, ASK (op 5) and DISMISS (19) in Ask.lua, SHOW (op 7) in Show.lua,
-- QUESTS (op 8) in Quests.lua, CHECKLIST (op 11) in Checklist.lua, HOOKS (op 12) and EVENTS (op 13)
-- in Hooks.lua, CHARACTER (14), BAGS (15), ITEM_INFO (16), PRICES (17) and MARKS (18) in Inventory.lua;
-- each file's header documents its ops' layout. They pack and read with Bytes.lua (ns.Bytes, loaded
-- first) and register in ns.OPS. Ops get (payload, seq).
--
-- Replies need the link open (the host says hello first): DeckLink.Send refuses otherwise.
local ADDON, ns = ...

DeckKit.NewModule(ns, ADDON, "|cffe8b04bDeckAgent|r")

-- The assistant's name (agent/SYSTEM.md): in chat, on her windows, and on her grip chord (DeckKit shows it
-- with the headphones on the cheat sheet and in the menu).
ns.NAME = "Decky"
if DeckKit.Dictation and DeckKit.Dictation.SetAssistant then DeckKit.Dictation.SetAssistant(ns.NAME) end

local CHANNEL = 3
local VERSION = 1
local OP_STATE, OP_WAY, OP_WAYPOINTS = 1, 2, 10
local POS, WORLD, ZONE, WHO, COMBAT, BRAIN, VOICE, TARGET = 1, 2, 4, 8, 16, 32, 64, 128
local MODELS = { haiku = 1, sonnet = 2, opus = 3 }
ns.MODELS = MODELS
local OK, UNKNOWN_OP, BAD_ARGS, REFUSED, FAILED, UNKNOWN_VERSION = 0, 1, 2, 3, 4, 5
local VIA_WAYPAD, VIA_MAPPIN = 1, 2
local FACTION = { Alliance = 1, Horde = 2 }

local B = ns.Bytes -- Bytes.lua, loaded first
local U8, U16, U32, S16, Str, ReadU16 = B.U8, B.U16, B.U32, B.S16, B.Str, B.ReadU16

local function Fraction(n) return math.floor(math.max(0, math.min(1, n)) * 65535 + 0.5) end

local function Readable(v)
    return v ~= nil and not (issecretvalue and issecretvalue(v))
end

------------------------------------------------------------------------
-- Ops: each returns status, body
------------------------------------------------------------------------

local function Position()
    local map = C_Map.GetBestMapForUnit("player")
    if not Readable(map) then return end
    local pos = C_Map.GetPlayerMapPosition(map, "player")
    local x, y
    if pos then x, y = pos:GetXY() end
    if Readable(x) and Readable(y) then return map, x, y end
    return map
end

-- The target: its NPC ID (from its GUID: Creature-0-<server>-<instance>-<zone>-<npc ID>-<spawn>), level
-- and a few flags; names come from the assistant's database.
local function TargetInfo()
    if not UnitExists("target") then return U32(0) .. U8(0) .. U8(0) .. U8(0) end
    local guid = UnitGUID("target")
    local npc = Readable(guid) and guid:match("^%a+%-%d+%-%d+%-%d+%-%d+%-(%d+)%-") or nil
    local isNpc = Readable(guid) and (guid:find("^Creature") or guid:find("^Vehicle")) and npc
    local level = UnitLevel("target")
    local class = UnitClassification("target")
    local flags = 1 + (UnitCanAttack("player", "target") and 2 or 0) + (UnitIsDead("target") and 4 or 0)
        + ((class == "elite" or class == "rareelite" or class == "worldboss") and 8 or 0)
        + ((class == "rare" or class == "rareelite") and 16 or 0) + (UnitIsPlayer("target") and 32 or 0)
        + (UnitIsTapDenied and UnitIsTapDenied("target") and 64 or 0)
    local hp, max = UnitHealth("target"), UnitHealthMax("target")
    local pct = (Readable(hp) and Readable(max) and max and max > 0) and math.floor(hp / max * 100 + 0.5) or 0
    return U32(isNpc and tonumber(npc) or 0) .. U8((Readable(level) and level and level > 0) and level or 255)
        .. U8(flags) .. U8(pct)
end

local function State(p)
    if #p < 2 then return BAD_ARGS, Str("STATE needs a field mask") end
    local mask = p:byte(2)
    local sent, parts = 0, {}
    local map, x, y = Position()
    if mask % 2 >= 1 then -- POS
        if map and x then
            sent = sent + POS
            parts[#parts + 1] = U16(map) .. U16(Fraction(x)) .. U16(Fraction(y))
        else
            parts[#parts + 1] = U16(map or 0) .. U16(0) .. U16(0)
        end
    end
    if mask % 4 >= 2 then -- WORLD
        local cont, wx, wy
        if map and x and C_Map.GetWorldPosFromMapPos and CreateVector2D then
            local ok, c, world = pcall(C_Map.GetWorldPosFromMapPos, map, CreateVector2D(x, y))
            if ok and Readable(c) and world then
                local a, b = world:GetXY()
                if Readable(a) and Readable(b) then cont, wx, wy = c, a, b end
            end
        end
        if cont then sent = sent + WORLD end
        parts[#parts + 1] = U8(cont or 0) .. S16(wx or 0) .. S16(wy or 0)
    end
    if mask % 8 >= 4 then -- ZONE
        local info = map and C_Map.GetMapInfo(map)
        local name = info and Readable(info.name) and info.name or ""
        if name ~= "" then sent = sent + ZONE end
        parts[#parts + 1] = Str(name)
    end
    if mask % 16 >= 8 then -- WHO
        local _, _, classID = UnitClass("player")
        sent = sent + WHO
        parts[#parts + 1] = U8(FACTION[UnitFactionGroup("player") or ""] or 0) .. U8(UnitLevel("player") or 0)
            .. U8(classID or 0)
    end
    if mask % 32 >= 16 then -- COMBAT
        sent = sent + COMBAT
        parts[#parts + 1] = U8(InCombatLockdown() and 1 or 0)
    end
    if mask % 64 >= 32 then -- BRAIN
        sent = sent + BRAIN
        parts[#parts + 1] = U8(MODELS[DeckAgentDB and DeckAgentDB.model or ""] or 0)
    end
    if mask % 128 >= 64 then -- VOICE
        sent = sent + VOICE
        parts[#parts + 1] = U8(DeckAgentDB and DeckAgentDB.quiet and 1 or 0)
    end
    if mask >= 128 then -- TARGET
        sent = sent + TARGET
        parts[#parts + 1] = TargetInfo()
    end
    return OK, U8(sent) .. table.concat(parts)
end

-- The route DeckKit planned to waypoint `point` (WAY's reply, above): the steps that fit in `room` bytes.
local ROUTE_TEXT = 100      -- a step's text is cut at this many bytes
local function RouteBytes(point, room)
    local summary = DeckKit.Route and DeckKit.Route.Summary and DeckKit.Route.Summary(point)
    if not (summary and summary.steps and #summary.steps > 0) then return U8(0) end
    local parts, used = {}, 4
    for _, step in ipairs(summary.steps) do
        local bytes = U16(math.min(65535, step.seconds or 0)) .. Str((step.text or ""):sub(1, ROUTE_TEXT))
        if used + #bytes > room or #parts == 255 then break end
        parts[#parts + 1] = bytes
        used = used + #bytes
    end
    return U8(#parts) .. U16(math.min(65535, summary.seconds or 0)) .. table.concat(parts) .. U8(summary.measured and 1 or 0)
end

local function Way(p)
    if #p < 7 then return BAD_ARGS, Str("WAY needs map, x and y") end
    local map, x, y = ReadU16(p, 2), ReadU16(p, 4) / 65535, ReadU16(p, 6) / 65535
    if WayPad_Add then
        local point, why = WayPad_Add(map, x, y)
        if not point then return REFUSED, Str(why or "WayPad refused it") end
        return OK, U8(VIA_WAYPAD) .. U16(point.id or 0) .. RouteBytes(point, 400)
    end
    if not (C_Map.CanSetUserWaypointOnMap and C_Map.CanSetUserWaypointOnMap(map)) then
        return REFUSED, Str("can't put a map pin on map " .. map)
    end
    C_Map.SetUserWaypoint(UiMapPoint.CreateFromCoordinates(map, x, y))
    if C_SuperTrack and C_SuperTrack.SetSuperTrackedUserWaypoint then C_SuperTrack.SetSuperTrackedUserWaypoint(true) end
    return OK, U8(VIA_MAPPIN) .. U16(0)
end

local LIST, REMOVE_ID, REMOVE_ACTIVE, REMOVE_ALL = 1, 2, 3, 4

local function Waypoints(p)
    local action = p:byte(2)
    if not action or action < LIST or action > REMOVE_ALL then return BAD_ARGS, Str("WAYPOINTS needs an action 1-4") end
    if action == REMOVE_ID and #p < 4 then return BAD_ARGS, Str("remove needs a waypoint id") end
    local removed = 0
    if WayPad_GetPoints and WayPad_Remove then
        local points, active = WayPad_GetPoints()
        local ids = {}
        if action == REMOVE_ID then
            ids[1] = ReadU16(p, 3)
        elseif action == REMOVE_ACTIVE and active and points[active] then
            ids[1] = points[active].id
        elseif action == REMOVE_ALL then
            for i, pt in ipairs(points) do ids[i] = pt.id end
        end
        for _, id in ipairs(ids) do
            if WayPad_Remove(id) then removed = removed + 1 end
        end
        if action == REMOVE_ID and removed == 0 then return REFUSED, Str("no waypoint with id " .. ids[1]) end
        points, active = WayPad_GetPoints()
        local budget = math.floor((DeckLink.MaxPayload() - 8) / 8)
        local parts, activeID = {}, active and points[active] and points[active].id or 0
        for i = 1, math.min(#points, budget, 255) do
            local pt = points[i]
            parts[i] = U16(pt.id or 0) .. U16(pt.mapID or 0) .. U16(Fraction(pt.x or 0)) .. U16(Fraction(pt.y or 0))
        end
        return OK, U8(removed) .. U8(#parts) .. U16(activeID) .. table.concat(parts)
    end
    -- No WayPad: the game's own map pin is the only waypoint (id 0 when it's there).
    local pin = C_Map.HasUserWaypoint and C_Map.HasUserWaypoint() and C_Map.GetUserWaypoint and C_Map.GetUserWaypoint()
    if pin and (action == REMOVE_ACTIVE or action == REMOVE_ALL or (action == REMOVE_ID and ReadU16(p, 3) == 0)) then
        C_Map.ClearUserWaypoint()
        return OK, U8(1) .. U8(0) .. U16(0)
    end
    if action == REMOVE_ID then return REFUSED, Str("no waypoint with that id") end
    if not pin then return OK, U8(0) .. U8(0) .. U16(0) end
    local pos = pin.position
    return OK, U8(0) .. U8(1) .. U16(0) .. U16(0) .. U16(pin.uiMapID or 0) .. U16(Fraction(pos and pos.x or 0))
        .. U16(Fraction(pos and pos.y or 0))
end

local OPS = { [OP_STATE] = State, [OP_WAY] = Way, [OP_WAYPOINTS] = Waypoints }

-- For the op files loaded after this one.
ns.OPS = OPS
ns.VERSION, ns.CHANNEL = VERSION, CHANNEL
ns.Status = { OK = OK, BAD_ARGS = BAD_ARGS, REFUSED = REFUSED, FAILED = FAILED }

------------------------------------------------------------------------
-- The channel
------------------------------------------------------------------------

local handled, failed = 0, 0

local function Handle(payload, seq)
    local opByte = payload:byte(1) or 0
    local version, op = math.floor(opByte / 64), opByte % 64
    local status, body
    if version ~= VERSION then
        status, body = UNKNOWN_VERSION, Str("unknown version " .. version)
    elseif not OPS[op] then
        status, body = UNKNOWN_OP, Str("unknown op " .. op)
    else
        local ok, s, b = pcall(OPS[op], payload, seq)
        if ok then
            status, body = s, b
        else
            geterrorhandler()(s)
            status, body = FAILED, Str(tostring(s):sub(1, 120))
        end
    end
    if status == OK then handled = handled + 1 else failed = failed + 1 end
    ns.Debug("agent op", op, "status", status)
    if not DeckLink.Send(CHANNEL, U8(opByte) .. U8(seq or 0) .. U8(status) .. (body or "")) then
        ns.Debug("agent", "reply not sent (link closed or too long)")
    end
end

DeckLink.On(CHANNEL, Handle)

ns.AddCommand("agent", function()
    ns.Print(("%d requests done, %d refused or failed; link %s"):format(handled, failed,
        DeckLink.IsOpen() and "open" or "closed"))
end, "DeckAgent: what the assistant has asked of the game")
