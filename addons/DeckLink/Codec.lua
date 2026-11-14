-- DeckLink frames, the same in both directions (deck-setup/decklink/codec.py is the host's copy):
--   len (2 bytes, payload length) | channel (1) | seq (1) | payload | CRC-16 (2)
-- big-endian, CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) over everything before it.
local _, ns = ...

local band, bxor = bit.band, bit.bxor

local Codec = {}
ns.Codec = Codec

Codec.HEADER, Codec.OVERHEAD = 4, 6
Codec.OP_HELLO, Codec.OP_ACK, Codec.OP_BYE, Codec.OP_PING = 1, 2, 3, 4

-- CRC-16 of bytes (a string, or a table of byte values) i..j.
function Codec.CRC(data, i, j)
    local crc = 0xFFFF
    local isString = type(data) == "string"
    for k = i or 1, j or #data do
        crc = bxor(crc, (isString and data:byte(k) or data[k]) * 256)
        for _ = 1, 8 do
            if band(crc, 0x8000) ~= 0 then
                crc = band(bxor(crc * 2, 0x1021), 0xFFFF)
            else
                crc = band(crc * 2, 0xFFFF)
            end
        end
    end
    return crc
end

local function U16(n) return string.char(math.floor(n / 256) % 256, n % 256) end
Codec.U16 = U16

function Codec.Build(channel, seq, payload)
    local body = U16(#payload) .. string.char(channel, seq) .. payload
    return body .. U16(Codec.CRC(body))
end
