-- DeckLink: a two-way data link between a program on the Deck and addons.
--
-- In: the host taps F9 (a 0) and F10 (a 1), one bit per tap, most significant first (deck-setup/
-- decklink/keys.py). Override bindings click a hidden button registered for downs, so nothing is typed,
-- nothing needs focus, and nothing is saved to the player's keybinds. Only downs count. Taps arrive as
-- fast as they're sent, all in order, even thousands in one frame (AddonTools lab/RESULTS.md E6).
-- Out: the strip (Strip.lua), which the host reads off the screen.
--
-- Both ways carry frames (Codec.lua). Stop-and-wait: the strip shows the last good inbound seq, which
-- is the host's ack; the strip shows one outbound frame at a time, which the host acks on channel 0.
--
-- Hosts can also send without the strip: frames arrive whether the link is open or not, with no ack
-- (channel 1 is Deck telemetry this way, for DeckTelemetry).
--
-- For other addons:
--   DeckLink.On(channel, fn(payload))   frames from the host on a channel (1-255)
--   DeckLink.Send(channel, payload)     queue a frame for the host; false if it's too long
--   DeckLink.IsOpen()                   is a host listening?
local ADDON, ns = ...

DeckKit.NewModule(ns, ADDON, "|cff9b59d6DeckLink|r")

local Codec = ns.Codec
local KEYS = { F9 = 0, F10 = 1 }
local MODS = { "", "SHIFT-", "CTRL-", "ALT-", "CTRL-SHIFT-", "ALT-SHIFT-", "ALT-CTRL-", "ALT-CTRL-SHIFT-" }
local MAX_IN = 4096 -- longest inbound payload
local IDLE_RESET = 0.3 -- seconds without a tap: a half-received frame is dropped
local OPEN_FOR = 30 -- seconds the link stays open after the last good frame
local QUEUE_MAX = 64

local api = {}
DeckLink = api

local handlers = {}
local open, lastHeard = false, 0
local rxSeq, rxCount, bad, heartbeat = 0, 0, 0, 0
local queue, txSeq = {}, 0
local lastError
local bound = false

------------------------------------------------------------------------
-- The strip
------------------------------------------------------------------------

local dirty = false
local function Render()
    dirty = false
    if not open then return ns.HideStrip() end
    local focus = GetCurrentKeyBoardFocus() ~= nil
    local flags = ns.FLAG_OPEN + (bound and ns.FLAG_BOUND or 0) + (focus and ns.FLAG_FOCUS or 0)
        + ((bound and not focus) and ns.FLAG_READY or 0)
    ns.ShowStrip(flags, heartbeat, rxSeq, rxCount, bad, queue[1] and queue[1].frame)
end
local function Changed() dirty = true end

------------------------------------------------------------------------
-- Frames in
------------------------------------------------------------------------

local function Close()
    open = false
    wipe(queue)
    Changed()
end

local function Control(payload)
    local op = payload:byte(1)
    if op == Codec.OP_HELLO then
        open = true
    elseif op == Codec.OP_ACK then
        local head = queue[1]
        if head and head.seq == payload:byte(2) then table.remove(queue, 1) end
    elseif op == Codec.OP_BYE then
        Close()
    elseif op == Codec.OP_PING then
        api.Send(0, payload)
    end
end

local function Deliver(channel, seq, payload)
    lastHeard = GetTime()
    rxCount = rxCount + 1
    if seq == rxSeq then return Changed() end -- the host sent it again: its ack (the strip) was slow
    rxSeq = seq
    if channel == 0 then
        Control(payload)
    else
        -- Delivered open or not: a fire-and-forget sender (telemetry) never says hello, so the strip
        -- stays off and nothing has to read the screen.
        local fn = handlers[channel]
        if fn then
            local ok, err = pcall(fn, payload)
            if not ok then
                lastError = ("channel %d: %s"):format(channel, tostring(err))
                geterrorhandler()(err)
            end
        end
    end
    Changed()
end

-- The frame being received, a bit at a time.
local bytes, byte, nbits = {}, 0, 0
local need -- total bytes this frame will have, once the length is in
local lastTap = 0
local discarding = false

local function Reset()
    wipe(bytes)
    byte, nbits, need = 0, 0, nil
end

local function Bad(why)
    bad = bad + 1
    lastError = why
    Reset()
    Changed()
end

local function Bit(b)
    local now = GetTime()
    if now - lastTap > IDLE_RESET then
        if #bytes > 0 or nbits > 0 then Bad("frame cut short") end
        Reset()
        discarding = false
    end
    lastTap = now
    if discarding then return end
    byte = byte * 2 + b
    nbits = nbits + 1
    if nbits < 8 then return end
    bytes[#bytes + 1] = byte
    byte, nbits = 0, 0
    if #bytes == 2 then
        local len = bytes[1] * 256 + bytes[2]
        if len > MAX_IN then
            Bad("frame too long")
            discarding = true -- until the taps stop
            return
        end
        need = Codec.OVERHEAD + len
    end
    if need and #bytes == need then
        local crc = bytes[need - 1] * 256 + bytes[need]
        if Codec.CRC(bytes, 1, need - 2) ~= crc then return Bad("checksum") end
        local chars = {}
        for k = Codec.HEADER + 1, need - 2 do chars[#chars + 1] = string.char(bytes[k]) end
        local channel, seq = bytes[3], bytes[4]
        Reset()
        Deliver(channel, seq, table.concat(chars))
    end
end

local button = CreateFrame("Button", "DeckLinkKeys")
button:RegisterForClicks("AnyDown")
button:SetScript("OnClick", function(_, key, down)
    if down == false then return end
    local b = KEYS[key]
    if b then Bit(b) end
end)

local owner = CreateFrame("Frame")
local function Bind()
    if bound or InCombatLockdown() then return end
    for key in pairs(KEYS) do
        for _, mod in ipairs(MODS) do SetOverrideBindingClick(owner, true, mod .. key, button:GetName(), key) end
    end
    bound = true
    Changed()
end
ns.On("PLAYER_LOGIN", Bind)
ns.On("PLAYER_REGEN_ENABLED", Bind) -- bindings can't be set in combat

------------------------------------------------------------------------
-- The API
------------------------------------------------------------------------

function api.On(channel, fn)
    assert(type(channel) == "number" and channel >= 1 and channel <= 255, "DeckLink.On: channel 1-255")
    handlers[channel] = fn
end

function api.Send(channel, payload)
    payload = tostring(payload)
    if not open or #queue >= QUEUE_MAX then return false end
    if #payload + Codec.OVERHEAD > ns.StripCapacity() then return false end
    txSeq = txSeq % 255 + 1
    queue[#queue + 1] = { seq = txSeq, frame = Codec.Build(channel, txSeq, payload) }
    Changed()
    return true
end

function api.IsOpen() return open end

-- The longest payload Send takes (it's what fits on the strip).
function api.MaxPayload() return ns.StripCapacity() - Codec.OVERHEAD end

------------------------------------------------------------------------
-- Heartbeat: the host can tell a live strip from a frozen picture
------------------------------------------------------------------------

local ticker = CreateFrame("Frame")
local nextBeat, wasFocus = 0, false
ticker:SetScript("OnUpdate", function()
    local now = GetTime()
    if open and now - lastHeard > OPEN_FOR then Close() end
    local focus = GetCurrentKeyBoardFocus() ~= nil
    if focus ~= wasFocus then wasFocus = focus; dirty = true end
    if open and now >= nextBeat then
        nextBeat = now + 0.25
        heartbeat = heartbeat + 1
        dirty = true
    end
    if dirty then Render() end
end)

ns.On("DISPLAY_SIZE_CHANGED", function() ns.LayoutStrip(); Changed() end)

ns.AddCommand("link", function()
    ns.Print(("%s, keys %s; in: %d good, %d bad; out: %d queued%s"):format(open and "open" or "closed",
        bound and "bound (F9/F10)" or "not bound yet", rxCount, bad, #queue,
        lastError and ("; last problem: " .. lastError) or ""))
end, "DeckLink: is a host connected, and how the frames are going")
