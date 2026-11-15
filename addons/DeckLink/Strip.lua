-- The strip: the game's side of the link, one row of colored cells along the bottom edge, read by the
-- host from gamescope's video stream (deck-setup/decklink/strip.py; keep the two in step).
--
--   cells    4 sync (red, green, blue, white), 1 bits-per-channel cell (on/off red 4, green 2, blue 1),
--            2^B gray ramp cells, then a bit stream, B bits in each of red, green and blue per cell:
--   status   8 format | 8 flags | 16 heartbeat | 8 last good inbound seq | 16 inbound frames received |
--            16 bad inbound frames | 16 outbound frame length L (0: none) | 16 CRC-16 of those 11 bytes
--   outbound L bytes: the frame at the head of the queue, until the host acks it
--   flags    1 open, 2 ready, 4 bound, 8 focus (a text box has the keyboard), 16 attention (an addon
--            has something for the host: shown even while the link is closed, so a host that only
--            glances at the screen knows to open the link)
--
-- DeckDev's strip encoding (AddonTools addons/DeckDev/Strip.lua) cut down to one row: 6 bits per
-- channel reads back exactly on the Deck. The bottom edge keeps clear of DeckDev's strip (top rows) and
-- MangoHud (top right). The frame has no parent, so hiding the UI (Alt+Z) doesn't hide it.
local _, ns = ...

local FORMAT = 1
local CELL, BITS = 4, 6
local LEVELS = 2 ^ BITS
local SYNC = { { 1, 0, 0 }, { 0, 1, 0 }, { 0, 0, 1 }, { 1, 1, 1 } }
local FIRST = #SYNC + 1 + LEVELS + 1 -- first stream cell
local STATUS_BITS = 11 * 8 + 16

ns.FLAG_OPEN, ns.FLAG_READY, ns.FLAG_BOUND, ns.FLAG_FOCUS, ns.FLAG_ATTENTION = 1, 2, 4, 8, 16

local frame, cells, perRow = nil, {}, 0

local function Layout()
    local width, height = GetPhysicalScreenSize()
    if not width or width <= 0 then width, height = 1280, 800 end
    perRow = math.floor(width / CELL)
    frame:SetScale(768 / height) -- one UI unit per screen pixel, so cells land on exact pixels
    frame:SetSize(perRow * CELL, CELL)
    for i = 1, perRow do
        local t = cells[i]
        if not t then
            t = frame:CreateTexture(nil, "OVERLAY")
            cells[i] = t
        end
        t:SetSize(CELL, CELL)
        t:ClearAllPoints()
        t:SetPoint("TOPLEFT", (i - 1) * CELL, 0)
    end
end

-- Bytes of outbound frame that fit after the status block (before the strip is first shown too).
function ns.StripCapacity()
    local cellsPerRow = perRow
    if cellsPerRow == 0 then
        local width = GetPhysicalScreenSize()
        cellsPerRow = math.floor(((width and width > 0) and width or 1280) / CELL)
    end
    return math.floor(((cellsPerRow - FIRST + 1) * 3 * BITS - STATUS_BITS) / 8)
end

local function Paint(bytes)
    for i, s in ipairs(SYNC) do cells[i]:SetColorTexture(s[1], s[2], s[3], 1) end
    cells[#SYNC + 1]:SetColorTexture(math.floor(BITS / 4) % 2, math.floor(BITS / 2) % 2, BITS % 2, 1)
    for n = 0, LEVELS - 1 do
        local v = n / (LEVELS - 1)
        cells[#SYNC + 2 + n]:SetColorTexture(v, v, v, 1)
    end
    -- The stream, 3 * BITS bits per cell, most significant first: bytes go into an accumulator that
    -- never holds more than 25 bits (exact in a double), and each full cell's worth is painted.
    local i, acc, n = FIRST - 1, 0, 0
    local cellBits, top = 3 * BITS, LEVELS - 1
    local function Emit()
        n = n - cellBits
        local v = math.floor(acc / 2 ^ n)
        acc = acc % 2 ^ n
        local b = v % LEVELS; v = (v - b) / LEVELS
        local g = v % LEVELS
        local r = (v - g) / LEVELS
        i = i + 1
        if cells[i] then cells[i]:SetColorTexture(r / top, g / top, b / top, 1) end
    end
    for k = 1, #bytes do
        acc = acc * 256 + bytes:byte(k)
        n = n + 8
        while n >= cellBits do Emit() end
    end
    if n > 0 then
        acc = acc * 2 ^ (cellBits - n)
        n = cellBits
        Emit()
    end
    for k = i + 1, perRow do cells[k]:SetColorTexture(0, 0, 0, 1) end
end

-- Shows the strip with this state; out is the outbound frame (a string) or nil.
function ns.ShowStrip(flags, heartbeat, rxSeq, rxCount, bad, out)
    if not frame then
        frame = CreateFrame("Frame", "DeckLinkStrip")
        frame:SetFrameStrata("TOOLTIP")
        frame:SetFrameLevel(9999)
        frame:SetPoint("BOTTOMLEFT", 0, 0)
        Layout()
    end
    out = out or ""
    local status = string.char(FORMAT, flags) .. ns.Codec.U16(heartbeat % 65536) .. string.char(rxSeq)
        .. ns.Codec.U16(rxCount % 65536) .. ns.Codec.U16(bad % 65536) .. ns.Codec.U16(#out)
    Paint(status .. ns.Codec.U16(ns.Codec.CRC(status)) .. out)
    frame:Show()
end

function ns.HideStrip()
    if frame then frame:Hide() end
end

-- Screen size changes move the cells.
function ns.LayoutStrip()
    if frame then Layout() end
end
