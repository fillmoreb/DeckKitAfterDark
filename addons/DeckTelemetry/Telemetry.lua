-- DeckTelemetry: the Steam Deck's own state (battery, power, temperatures, fan, load) inside the game,
-- for other addons to show. It arrives on DeckLink channel 1 from deck-setup/decklink/telemetry.py,
-- every 5 s by default. Nothing here draws anything.
--
--   DeckTelemetry.Get()        the latest readings as a new table (nil before the first update),
--                              plus age = seconds since that update
--   DeckTelemetry.Get(key)     one reading (nil if the Deck didn't send it)
--   DeckTelemetry.Age()        seconds since the last update (nil before the first)
--   DeckTelemetry.OnUpdate(fn) fn(readings) after each update; returns a handle for Cancel
--   DeckTelemetry.Cancel(handle)
--
-- Readings (telemetry.py documents them too; numbers unless noted):
--   bat   battery charge, %          st    "charging", "discharging", "not charging" or "full"
--   ac    true plugged in            bw    battery power, W: positive draining, negative charging
--   apu   APU power, W               cpu   CPU busy, %        gpu   GPU busy, %       mem   memory used, %
--   tapu  APU temperature, C         tbat  battery temp, C    tssd  SSD temp, C       fan   fan, rpm
--
-- Updates are fire and forget: if one is lost (chat was open), the next replaces it. Check Age() to
-- tell a fresh reading from a stale one (no sender running, or the Deck asleep).
local ADDON, ns = ...

DeckKit.NewModule(ns, ADDON, "|cff5fd38dDeckTelemetry|r")

local CHANNEL = 1
local STATES = { c = "charging", d = "discharging", n = "not charging", f = "full" }

local api = {}
DeckTelemetry = api

local latest, at
local listeners, nextHandle = {}, 0

-- "v=1;bat=87;st=d;..." into a table: numbers where they're numbers, ac as a boolean, st spelled out.
local function Parse(payload)
    local t = {}
    for key, value in payload:gmatch("([%w_]+)=([^;]*)") do
        t[key] = tonumber(value) or value
    end
    if t.v ~= 1 then return nil end
    t.v = nil
    if t.ac ~= nil then t.ac = t.ac == 1 end
    if t.st then t.st = STATES[t.st] or t.st end
    return t
end

local function Copy(t)
    local c = {}
    for k, v in pairs(t) do c[k] = v end
    return c
end

DeckLink.On(CHANNEL, function(payload)
    local t = Parse(payload)
    if not t then return end
    latest, at = t, GetTime()
    for _, fn in pairs(listeners) do
        local ok, err = pcall(fn, Copy(t))
        if not ok then geterrorhandler()(err) end
    end
end)

function api.Get(key)
    if not latest then return nil end
    if key then return latest[key] end
    local c = Copy(latest)
    c.age = GetTime() - at
    return c
end

function api.Age() return at and (GetTime() - at) or nil end

function api.OnUpdate(fn)
    nextHandle = nextHandle + 1
    listeners[nextHandle] = fn
    return nextHandle
end

function api.Cancel(handle) listeners[handle] = nil end

ns.AddCommand("telemetry", function()
    if not latest then return ns.Print("nothing yet (is decklink.telemetry running on the Deck?)") end
    local keys = {}
    for k in pairs(latest) do keys[#keys + 1] = k end
    table.sort(keys)
    local parts = {}
    for _, k in ipairs(keys) do parts[#parts + 1] = k .. "=" .. tostring(latest[k]) end
    ns.Print(("%s (%.0f s ago)"):format(table.concat(parts, " "), api.Age()))
end, "DeckTelemetry: the Deck's latest readings")
