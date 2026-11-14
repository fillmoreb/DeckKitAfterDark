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
--
-- On the wire: 17 bytes, fields at fixed places, big-endian (telemetry.py has the layout; keep the two
-- in step). A field mask says which readings are real; the rest are left out of the table.
local ADDON, ns = ...

DeckKit.NewModule(ns, ADDON, "|cff5fd38dDeckTelemetry|r")

local CHANNEL = 1
local FORMAT = 2
local STATES = { [0] = "discharging", [1] = "charging", [2] = "not charging", [3] = "full" }
local FIELDS = { "bat", "st", "ac", "bw", "apu", "cpu", "gpu", "mem", "tapu", "tbat", "tssd", "fan" } -- mask bits 0..11

local api = {}
DeckTelemetry = api

local latest, at
local listeners, nextHandle = {}, 0

local function U16(p, i)
    local hi, lo = p:byte(i, i + 1)
    return hi * 256 + lo
end

local function Bit(n, b) return math.floor(n / 2 ^ b) % 2 == 1 end

-- The 17-byte payload into a table of the readings the mask says are real.
local function Parse(p)
    if #p < 17 or p:byte(1) ~= FORMAT then return nil end
    local mask, power = U16(p, 2), p:byte(5)
    local bw = U16(p, 6)
    if bw >= 32768 then bw = bw - 65536 end
    local all = {
        bat = p:byte(4), st = STATES[power % 4], ac = Bit(power, 2), bw = bw / 10, apu = U16(p, 8) / 10,
        cpu = p:byte(10), gpu = p:byte(11), mem = p:byte(12), tapu = p:byte(13), tbat = p:byte(14),
        tssd = p:byte(15), fan = U16(p, 16),
    }
    local t = {}
    for b, key in ipairs(FIELDS) do
        if Bit(mask, b - 1) then t[key] = all[key] end
    end
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
