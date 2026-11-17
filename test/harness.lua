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
UnitAffectingCombat = function() return inCombat end
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
    SetAssistant = function(name) print("ASSISTANT " .. name) end,
    Claim = function() print("DICTATION claimed") end }
local function fire(event, ...) for _, fn in ipairs(events[event] or {}) do fn(...) end end

-- DeckKit's own Conditions and JSON, the real files (DeckAgent's conditions and layouts are DeckKit's):
-- from the DeckKit repo beside this one, or DECKKIT_DIR.
local DECKKIT = os.getenv("DECKKIT_DIR") or (DIR .. "/../../DeckKit/addons/DeckKit/")
-- DeckKit's UI as DeckAgent sees it (below) and its actions: what a saved widget offers lands in OFFERS.
OFFERS = {}
local coreNS = { On = function(event, fn) events[event] = events[event] or {}; table.insert(events[event], fn) end,
    Readable = function(v) return v ~= nil and not (issecretvalue and issecretvalue(v)) end,
    UI = setmetatable({}, { __index = function(_, k) return DeckKit.UI[k] end }),
    Actions = { OfferNow = function(id, opts) OFFERS[id] = opts end, Withdraw = function(id) OFFERS[id] = nil end } }
-- Ledger, Measures, People, Widgets: what the PEOPLE, INVITE, WIDGET and LEDGER ops answer from (DeckAgent only
-- packs them).
for _, file in ipairs({ "Conditions.lua", "JSON.lua", "Ledger.lua", "Measures.lua", "People.lua", "Widgets.lua" }) do
    assert(loadfile(DECKKIT .. file), "DeckKit's " .. file .. " (set DECKKIT_DIR to the DeckKit addon folder)")("DeckKit", coreNS)
end
DeckKit.Conditions, DeckKit.JSON, DeckKit.Ledger, DeckKit.People = coreNS.Conditions, coreNS.JSON, coreNS.Ledger,
    coreNS.People
DeckKit.Measures, DeckKit.Widgets = coreNS.Measures, coreNS.Widgets
-- DeckKit's layout window, as far as DeckAgent sees it: LAYOUT is the one up (scenarios call its
-- opts.onReply(n) for a reply button, DeckKit.UI.HideLayout() for B).
TRACKERS = {}
DeckKit.UI = {
    ShowLayout = function(spec, opts)
        local old = LAYOUT
        LAYOUT = { spec = spec, opts = opts, shown = true }
        if old and old.shown and old.opts.onClose then old.shown = false; old.opts.onClose("replaced") end
        print("LAYOUT " .. tostring(spec.type) .. " " .. tostring(opts.title))
        return true
    end,
    -- The default tracker is TRACKER; named ones (saved widgets) are TRACKERS[name].
    ShowTracker = function(spec, opts)
        if opts and opts.name then TRACKERS[opts.name] = { spec = spec, shown = true }
        else TRACKER = { spec = spec, shown = true } end
    end,
    ShowCard = function(spec, opts) CARD = { spec = spec, opts = opts, shown = true } end,
    HideCard = function() if CARD then CARD.shown = false end end,
    HideTracker = function(name)
        local t = name and TRACKERS[name] or (not name and TRACKER)
        if t then t.shown = false end
    end,
    TrackerShown = function(name)
        local t = name and TRACKERS[name] or (not name and TRACKER)
        return t ~= nil and t ~= false and t.shown
    end,
    HideLayout = function()
        if LAYOUT and LAYOUT.shown then
            LAYOUT.shown = false
            if LAYOUT.opts.onClose then LAYOUT.opts.onClose("closed") end
        end
    end,
}

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
    table.insert(TIMERS, t); return t end, After = function(s, fn) end,
    NewTicker = function(s, fn) TICKERS = TICKERS or {}; table.insert(TICKERS, fn); return { Cancel = function() end } end }
SERVERTIME = 1000000
GetServerTime = function() return SERVERTIME end
function fireTimers() for _, t in ipairs(TIMERS) do if not t.cancelled then t.fn() end end TIMERS = {} end
function fireEvent(event, ...) for _, fn in ipairs(EVENTS[event] or {}) do fn(...) end end
UnitClass = function() return "Priest", PLAYER.class, 5 end
UnitLevel = function(u) if u == "target" then return TARGET and TARGET.level end return PLAYER.level end
C_Item = C_Item or {}
-- For DeckAgent's target field and Inventory.lua. Scenarios set TARGET, BAGSLOTS, EQUIPPED, ITEMS.
TARGET = nil -- { guid, level, class, attackable, dead, player, hp, max }
UnitExists = function(u) return u == "target" and TARGET ~= nil end
UnitGUID = function(u) return u == "target" and TARGET and TARGET.guid end
UnitClassification = function() return TARGET and TARGET.class or "normal" end
UnitCanAttack = function() return TARGET and TARGET.attackable end
UnitIsDead = function() return TARGET and TARGET.dead end
UnitIsPlayer = function() return TARGET and TARGET.player end
UnitHealth = function(u) return u == "target" and TARGET and TARGET.hp or 100 end
UnitHealthMax = function(u) return u == "target" and TARGET and TARGET.max or 300 end
UnitPowerMax = function() return 400 end
UnitStat = function(_, i) return 10 + i, 20 + i end
UnitArmor = function() return 90, 95 end
GetMoney = function() return 123456 end
GetSpellBonusDamage = function(school) return school == 6 and 9 or 4 end
GetSpellBonusHealing = function() return 12 end
UnitName = function() return "Lucien" end
GetRealmName = function() return "Realm" end
BAGSLOTS = { [0] = { { itemID = 6948, stackCount = 1 }, false, { itemID = 2589, stackCount = 15 } },
             [1] = { { itemID = 2589, stackCount = 5 }, false } }
C_Container = {
    GetContainerNumSlots = function(bag) return BAGSLOTS[bag] and #BAGSLOTS[bag] or 0 end,
    GetContainerItemInfo = function(bag, slot) local s = BAGSLOTS[bag] and BAGSLOTS[bag][slot]; return s or nil end,
}
-- Links are item:ID:enchant:gem1:gem2:gem3:gem4:suffix:uniqueID: the Mug's random suffix (-3) changes its
-- stats (ITEMS below); 14 is the copy's own ID, which must never be read as the suffix.
EQUIPPED = { [5] = "|cff1eff00|Hitem:16607::::::::14|h[Robe]|h|r", [16] = "|Hitem:277247:15:::::-3:14|h[Mug]|h" }
GetInventoryItemLink = function(_, slot) return EQUIPPED[slot] end
GetInventoryItemDurability = function(slot) if slot == 5 then return 30, 40 end end
C_SpecializationInfo = { GetSpecialization = function() return 1 end, GetSpecializationInfo = function() return 1487 end }
ITEMS = { [19019] = { cached = true, loc = "INVTYPE_WEAPON", class = 2, sub = 7, q = 5, ilvl = 80, req = 60,
                      stats = { ITEM_MOD_AGILITY_SHORT = 5, ITEM_MOD_DAMAGE_PER_SECOND_SHORT = 41.84, RESISTANCE2_NAME = 8,
                                FIRE_RESISTANCE = 8 } },
          [5000] = { cached = false, loc = "INVTYPE_HEAD", class = 4, sub = 1, q = 2, ilvl = 20, req = 15, stats = {} },
          [277247] = { cached = true, loc = "INVTYPE_WEAPON", class = 2, sub = 4, q = 2, ilvl = 18, req = 13,
                       stats = { ITEM_MOD_DAMAGE_PER_SECOND_SHORT = 9.5 },
                       suffixes = { [-3] = { ITEM_MOD_DAMAGE_PER_SECOND_SHORT = 9.5, ITEM_MOD_STAMINA_SHORT = 4 } } } }
LOADS = {}
C_Item.GetItemInfoInstant = function(id) local it = ITEMS[id]; if it then return id, "", "", it.loc, 0, it.class, it.sub end end
C_Item.IsItemDataCachedByID = function(id) return ITEMS[id] and ITEMS[id].cached end
C_Item.RequestLoadItemDataByID = function(id) table.insert(LOADS, id) end
C_Item.GetItemInfo = function(link) local it = ITEMS[tonumber(link:match("item:(%d+)"))]
    if it and it.cached then return "x", link, it.q, it.ilvl, it.req end end
C_Item.GetItemStats = function(link)
    local fields = { strsplit(":", link:match("item:([%-%d:]+)") or "") }
    local it = ITEMS[tonumber(fields[1])]
    if not (it and it.cached) then return end
    return it.suffixes and it.suffixes[tonumber(fields[7])] or it.stats
end
strsplit = strsplit or function(sep, s) local out = {} for part in (s .. sep):gmatch("(.-)" .. sep:gsub("%p", "%%%0")) do out[#out + 1] = part end return unpack(out) end

-- For DeckAgent's Show.lua.
DEFAULT_CHAT_FRAME = { AddMessage = function(_, msg) print("ECHO " .. msg) end }
C_Item.GetItemIconByID = function(id) return 1000 + id end
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
function later(s) now = now + s; SERVERTIME = SERVERTIME + s end
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
