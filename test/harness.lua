-- Test harness: runs the real DeckLink files against a stub WoW API, then a scenario (stdin), then
-- writes what the strip painted, as raw RGB ("P6 <w> <h>\n" + bytes), to the file named by $PICTURE.
-- Anything the scenario prints goes to stdout.
--
-- Scenario helpers (globals): taps("0110...") taps F9/F10 for each bit, key("SHIFT-F10") taps one key,
-- later(seconds) moves the clock on, frame() runs one frame's OnUpdate scripts, focus(true) puts a text
-- box in focus, combat(true), login().
local DIR = arg[0]:match("^(.*)/[^/]*$") or "."
local ADDON = DIR .. "/../addons/DeckLink/"
local W, H = 1280, 800

-- WoW's bit library, for plain Lua 5.1.
local function bitop(a, b, keep)
    local r, p = 0, 1
    for _ = 1, 32 do
        local x, y = a % 2, b % 2
        if keep(x, y) then r = r + p end
        a, b, p = (a - x) / 2, (b - y) / 2, p * 2
    end
    return r
end
bit = {
    band = function(a, b) return bitop(a, b, function(x, y) return x == 1 and y == 1 end) end,
    bxor = function(a, b) return bitop(a, b, function(x, y) return x ~= y end) end,
}
wipe = function(t) for k in pairs(t) do t[k] = nil end return t end

local now = 100
GetTime = function() return now end
local frames, textures = {}, {}
local function stub(t) return setmetatable(t, { __index = function() return function() end end }) end

function CreateFrame(kind, name)
    local f = stub({ shown = true })
    f.GetName = function() return name end
    f.SetScript = function(self, k, fn) rawset(self, k, fn) end
    f.Show = function(self) rawset(self, "shown", true) end
    f.Hide = function(self) rawset(self, "shown", false) end
    f.CreateTexture = function()
        local t = stub({ owner = f })
        t.SetPoint = function(self, _, x) self.x = x end
        t.SetSize = function(self, w) self.w = w end
        t.SetColorTexture = function(self, r, g, b) self.c = { r, g, b } end
        table.insert(textures, t)
        return t
    end
    if name then _G[name] = f end
    table.insert(frames, f)
    return f
end
GetPhysicalScreenSize = function() return W, H end
local focused = nil
GetCurrentKeyBoardFocus = function() return focused end
local inCombat = false
InCombatLockdown = function() return inCombat end
local bindings = {}
SetOverrideBindingClick = function(_, _, key, button, mouse) bindings[key] = { button, mouse } end
local errors = {}
geterrorhandler = function() return function(e) table.insert(errors, tostring(e)) end end

-- The DeckKit core, only as much as DeckLink uses.
local events = {}
DeckKit = {}
function DeckKit.NewModule(ns)
    ns.On = function(event, fn) events[event] = events[event] or {}; table.insert(events[event], fn) end
    ns.Print = function(msg) print("PRINT " .. msg) end
    ns.AddCommand = function(name, fn) COMMANDS = COMMANDS or {}; COMMANDS[name] = fn end
    return ns
end
local function fire(event, ...) for _, fn in ipairs(events[event] or {}) do fn(...) end end

local ns = {}
for line in io.lines(ADDON .. "DeckLink.toc") do
    if line:match("%.lua$") then assert(loadfile(ADDON .. line))("DeckLink", ns) end
end

-- DeckTelemetry too, after DeckLink (it registers channel 1).
local TELEMETRY = DIR .. "/../addons/DeckTelemetry/"
local tns = {}
for line in io.lines(TELEMETRY .. "DeckTelemetry.toc") do
    if line:match("%.lua$") then assert(loadfile(TELEMETRY .. line))("DeckTelemetry", tns) end
end

CODEC = ns.Codec
function login() fire("PLAYER_LOGIN") end
function combat(on)
    inCombat = on
    if not on then fire("PLAYER_REGEN_ENABLED") end
end
function key(k)
    local b = bindings[k]
    if not b then return end
    local button = _G[b[1]]
    button.OnClick(button, b[2], true)
end
function taps(bits)
    for c in bits:gmatch("[01]") do key(c == "0" and "F9" or "F10") end
end
function later(s) now = now + s end
function frame() for _, f in ipairs(frames) do if rawget(f, "OnUpdate") then f.OnUpdate(f, 0.016) end end end
function focus(on) focused = on and {} or nil end
function errorsSeen() return #errors end

assert(loadstring(io.read("*a"), "scenario"))()

-- The strip's row, at the bottom of a black screen.
local buf = {}
for i = 1, W * H do buf[i] = "\0\0\0" end
local strip = DeckLinkStrip
if strip and rawget(strip, "shown") then
    for _, t in ipairs(textures) do
        if t.owner == strip and rawget(t, "c") then
            local px = string.char(math.floor(t.c[1] * 255 + .5), math.floor(t.c[2] * 255 + .5),
                math.floor(t.c[3] * 255 + .5))
            for dy = H - t.w, H - 1 do
                for dx = 0, t.w - 1 do buf[dy * W + t.x + dx + 1] = px end
            end
        end
    end
end
local out = assert(io.open(os.getenv("PICTURE"), "wb"))
out:write(("P6 %d %d\n"):format(W, H), table.concat(buf))
out:close()
