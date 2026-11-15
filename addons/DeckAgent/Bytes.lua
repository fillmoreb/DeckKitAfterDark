-- DeckAgent's byte helpers: packing and reading the binary packets (Agent.lua's header has the layout
-- rules: big-endian, fields at fixed places). Lua 5.1 has no string.pack, so every op file uses these;
-- one copy, loaded first, so a fix to one reaches every op.
--
--   ns.Bytes.U8(n) / U16(n) / U32(n)   unsigned, wrapped to the field's size
--   ns.Bytes.S16(n)                    signed, rounded and clamped to -32768..32767
--   ns.Bytes.Clamp16(n)                unsigned, rounded and clamped to 0..65535 (a stat, a count)
--   ns.Bytes.Str(s)                    length uint8 + up to 255 bytes
--   ns.Bytes.ReadU16(p, i) / ReadU32(p, i) / ReadS16(p, i)   the number at byte i (nil past the end)
--   ns.Bytes.ReadStr(p, i)             a length-prefixed string at i: it and the next index, or nil when
--                                      it runs past the end
local _, ns = ...

local B = {}
ns.Bytes = B

function B.U8(n) return string.char(math.floor(n) % 256) end

function B.U16(n)
    n = math.floor(n) % 65536
    return string.char(math.floor(n / 256), n % 256)
end

function B.U32(n)
    n = math.floor(n) % 4294967296
    return B.U16(math.floor(n / 65536)) .. B.U16(n % 65536)
end

function B.S16(n)
    n = math.floor((n or 0) + 0.5)
    if n > 32767 then n = 32767 elseif n < -32768 then n = -32768 end
    return B.U16(n % 65536)
end

function B.Clamp16(n) return B.U16(math.max(0, math.min(65535, math.floor((n or 0) + 0.5)))) end

function B.Str(s)
    s = tostring(s or ""):sub(1, 255)
    return B.U8(#s) .. s
end

function B.ReadU16(p, i)
    local hi, lo = p:byte(i, i + 1)
    if not lo then return nil end
    return hi * 256 + lo
end

function B.ReadU32(p, i)
    local hi, lo = B.ReadU16(p, i), B.ReadU16(p, i + 2)
    if not (hi and lo) then return nil end
    return hi * 65536 + lo
end

function B.ReadS16(p, i)
    local n = B.ReadU16(p, i)
    if n and n >= 32768 then n = n - 65536 end
    return n
end

function B.ReadStr(p, i)
    local n = p:byte(i)
    if not n or i + n > #p then return nil end
    return p:sub(i + 1, i + n), i + 1 + n
end
