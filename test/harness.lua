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
    f.CreateFontString = function()
        local fs = stub({})
        fs.SetText = function(self, t) rawset(self, "text", t) end
        return fs
    end
    f.IsShown = function(self) return rawget(self, "shown") end
    f.CreateTexture = function()
        local t = stub({ owner = f })
        t.SetPoint = function(self, _, x) self.x = x end
        t.SetSize = function(self, w) self.w = w end
        t.SetColorTexture = function(self, r, g, b) self.c = { r, g, b } end
        table.insert(textures, t)
        return t
    end
    if kind == "EditBox" then
        f.SetText = function(self, t)
            rawset(self, "text", t)
            local fn = rawget(self, "OnTextChanged")
            if fn then fn(self) end
        end
        f.GetText = function(self) return rawget(self, "text") end
        f.SetFocus = function(self) FOCUSED = self end
        f.ClearFocus = function(self) if FOCUSED == self then FOCUSED = nil end end
    end
    if name then _G[name] = f end
    table.insert(frames, f)
    return f
end
GetPhysicalScreenSize = function() return W, H end
local focused = nil
GetCurrentKeyBoardFocus = function() return focused or FOCUSED end
local inCombat = false
InCombatLockdown = function() return inCombat end
local bindings = {}
SetOverrideBindingClick = function(_, _, key, button, mouse) bindings[key] = { button, mouse } end
local errors = {}
geterrorhandler = function() return function(e) table.insert(errors, tostring(e)) end end

-- The DeckKit core, only as much as DeckLink uses.
local events = {}
EVENTS = events
DeckKit = {}
function DeckKit.NewModule(ns)
    ns.On = function(event, fn) events[event] = events[event] or {}; table.insert(events[event], fn) end
    ns.Print = function(msg) print("PRINT " .. msg) end
    ns.AddCommand = function(name, fn) COMMANDS = COMMANDS or {}; COMMANDS[name] = fn end
    ns.Debug = function() end
    ns.AddSettings = function(items) SETTINGS = items end
    return ns
end
DeckKit.Dictation = { SetState = function(state, binding) print("DICTATION " .. state .. " " .. tostring(binding)) end,
    Claim = function() print("DICTATION claimed") end }
local function fire(event, ...) for _, fn in ipairs(events[event] or {}) do fn(...) end end

local ns = {}
for line in io.lines(ADDON .. "DeckLink.toc") do
    if line:match("%.lua$") then assert(loadfile(ADDON .. line))("DeckLink", ns) end
end

-- The game as DeckAgent sees it: the player in Undercity (scenarios can change PLAYER or set WayPad_Add).
PLAYER = { map = 1458, x = 0.49, y = 0.183, cont = 0, wx = 1636.25, wy = 239.62, zone = "Undercity",
           faction = "Horde", level = 20, class = "PRIEST" }
local function vector(x, y) return { GetXY = function() return x, y end } end
CreateVector2D = vector
C_Map = {
    GetBestMapForUnit = function() return PLAYER.map end,
    GetPlayerMapPosition = function() return PLAYER.x and vector(PLAYER.x, PLAYER.y) end,
    GetWorldPosFromMapPos = function() return PLAYER.cont, vector(PLAYER.wx, PLAYER.wy) end,
    GetMapInfo = function(id) return { name = id == PLAYER.map and PLAYER.zone or "Map " .. id } end,
    CanSetUserWaypointOnMap = function(id) return id ~= 0 end,
    SetUserWaypoint = function(point) print("MAPPIN", point.map, point.x, point.y) end,
}
UiMapPoint = { CreateFromCoordinates = function(map, x, y) return { map = map, x = x, y = y } end }
C_SuperTrack = { SetSuperTrackedUserWaypoint = function(on) print("TRACKED", tostring(on)) end }
UnitFactionGroup = function() return PLAYER.faction end
-- Chat and who's online, for DeckAgent's Chat.lua. Scenarios set GROUP / GUILD / BNET / FRIENDS / ROSTER.
GROUP, RAID, GUILD = false, false, false
BNET, FRIENDS, ROSTER = {}, {}, {}
SendChatMessage = function(text, kind, _, target) print("CHAT", kind, tostring(target), text) end
BNSendWhisper = function(id, text) print("BNCHAT", id, text) end
IsInGroup = function() return GROUP end
IsInRaid = function() return RAID end
IsInGuild = function() return GUILD end
BNGetNumFriends = function() return #BNET end
C_BattleNet = { GetFriendAccountInfo = function(i) return BNET[i] end }
C_FriendList = { GetNumFriends = function() return #FRIENDS end, GetFriendInfoByIndex = function(i) return FRIENDS[i] end }
GetNumGuildMembers = function() return #ROSTER end
GetGuildRosterInfo = function(i) local r = ROSTER[i]; return r[1], "", 0, r[2], r[3], r[4], "", "", r[5] end
TIMERS = {}
C_Timer = { NewTimer = function(s, fn) local t = { fn = fn, Cancel = function(self) self.cancelled = true end }
    table.insert(TIMERS, t); return t end, After = function(s, fn) end }
function fireTimers() for _, t in ipairs(TIMERS) do if not t.cancelled then t.fn() end end TIMERS = {} end
function fireEvent(event, ...) for _, fn in ipairs(EVENTS[event] or {}) do fn(...) end end
UnitClass = function() return "Priest", PLAYER.class, 5 end
UnitLevel = function() return PLAYER.level end
-- For DeckAgent's Show.lua.
DEFAULT_CHAT_FRAME = { AddMessage = function(_, msg) print("ECHO " .. msg) end }
C_Item = { GetItemIconByID = function(id) return 1000 + id end }
-- The quest log, for Quests.lua: QUESTLOG = { header or {id, complete, failed, objectives}, ... }.
QUESTLOG = { { header = "Orgrimmar" },
    { id = 5728, objectives = { { type = "monster", numFulfilled = 1, numRequired = 1, finished = true },
                                { type = "monster", numFulfilled = 0, numRequired = 1, finished = false } } },
    { id = 5725, complete = true, objectives = { { type = "item", numFulfilled = 300, numRequired = 300,
                                                   finished = true } } } }
C_QuestLog = {
    GetNumQuestLogEntries = function() return #QUESTLOG end,
    GetInfo = function(i) local q = QUESTLOG[i]; return q and { isHeader = q.header ~= nil, title = q.header,
        questID = q.id } end,
    IsComplete = function(id) for _, q in ipairs(QUESTLOG) do if q.id == id then return q.complete end end end,
    IsFailed = function(id) for _, q in ipairs(QUESTLOG) do if q.id == id then return q.failed end end end,
    GetQuestObjectives = function(id) for _, q in ipairs(QUESTLOG) do if q.id == id then return q.objectives end end end,
}

-- The addons on top of DeckLink, after it (they register their channels).
for _, name in ipairs({ "DeckTelemetry", "DeckDictation", "DeckAgent" }) do
    local dir = DIR .. "/../addons/" .. name .. "/"
    local addonNS = {}
    for line in io.lines(dir .. name .. ".toc") do
        if line:match("%.lua$") then assert(loadfile(dir .. line))(name, addonNS) end
    end
end

CODEC = ns.Codec
CODEC_FROM_B64 = ns.FromBase64
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
