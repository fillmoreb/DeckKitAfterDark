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
--   WAY (op 2):    bytes 1-2 map uint16, 3-4 x uint16, 5-6 y uint16: a WayPad waypoint, made active
--                  (the game's own map pin when WayPad is missing)
--
-- Reply (game -> host, on the strip):
--   byte 0      the request's op byte
--   byte 1      the request's frame seq
--   byte 2      status: 0 ok, 1 unknown op, 2 bad arguments, 3 refused, 4 failed in the game,
--               5 unknown version
--   then        STATE: the asked-for fields, in mask-bit order (a field the game can't give is sent
--               zeroed, and its bit is cleared in the echoed mask at byte 3)
--               WAY: byte 3 how it was set (1 WayPad, 2 map pin), bytes 4-5 WayPad's id for the
--               waypoint, uint16 (stable, for later requests about it; 0 for the map pin)
--               CHAT, FRIENDS: see Chat.lua
--               any failure: length uint8, a short reason (UTF-8)
--
-- CHAT (op 3) and FRIENDS (op 4) are in Chat.lua, ASK (op 5) in Ask.lua; they use the helpers shared
-- below (ns.Bytes, ns.OPS). Ops get (payload, seq).
--
-- Replies need the link open (the host says hello first): DeckLink.Send refuses otherwise.
local ADDON, ns = ...

DeckKit.NewModule(ns, ADDON, "|cffe8b04bDeckAgent|r")

local CHANNEL = 3
local VERSION = 1
local OP_STATE, OP_WAY = 1, 2
local POS, WORLD, ZONE, WHO, COMBAT, BRAIN = 1, 2, 4, 8, 16, 32
local MODELS = { haiku = 1, sonnet = 2, opus = 3 }
ns.MODELS = MODELS
local OK, UNKNOWN_OP, BAD_ARGS, REFUSED, FAILED, UNKNOWN_VERSION = 0, 1, 2, 3, 4, 5
local VIA_WAYPAD, VIA_MAPPIN = 1, 2
local FACTION = { Alliance = 1, Horde = 2 }

------------------------------------------------------------------------
-- Bytes (Lua 5.1 has no string.pack)
------------------------------------------------------------------------

local function U8(n) return string.char(math.floor(n) % 256) end

local function U16(n)
    n = math.floor(n) % 65536
    return string.char(math.floor(n / 256), n % 256)
end

local function S16(n)
    n = math.floor(n + 0.5)
    if n > 32767 then n = 32767 elseif n < -32768 then n = -32768 end
    return U16(n % 65536)
end

local function Str(s)
    s = tostring(s or ""):sub(1, 255)
    return U8(#s) .. s
end

local function ReadU16(p, i)
    local hi, lo = p:byte(i, i + 1)
    return hi * 256 + lo
end

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
    return OK, U8(sent) .. table.concat(parts)
end

local function Way(p)
    if #p < 7 then return BAD_ARGS, Str("WAY needs map, x and y") end
    local map, x, y = ReadU16(p, 2), ReadU16(p, 4) / 65535, ReadU16(p, 6) / 65535
    if WayPad_Add then
        local point, why = WayPad_Add(map, x, y)
        if not point then return REFUSED, Str(why or "WayPad refused it") end
        return OK, U8(VIA_WAYPAD) .. U16(point.id or 0)
    end
    if not (C_Map.CanSetUserWaypointOnMap and C_Map.CanSetUserWaypointOnMap(map)) then
        return REFUSED, Str("can't put a map pin on map " .. map)
    end
    C_Map.SetUserWaypoint(UiMapPoint.CreateFromCoordinates(map, x, y))
    if C_SuperTrack and C_SuperTrack.SetSuperTrackedUserWaypoint then C_SuperTrack.SetSuperTrackedUserWaypoint(true) end
    return OK, U8(VIA_MAPPIN) .. U16(0)
end

local OPS = { [OP_STATE] = State, [OP_WAY] = Way }

-- For Chat.lua (loaded after this file).
ns.OPS = OPS
ns.Bytes = { U8 = U8, U16 = U16, Str = Str, ReadU16 = ReadU16 }
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
