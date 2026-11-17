-- DeckAgent's people: who matters right now, so the assistant can turn "the guy who just gave me a linen bag" or
-- "Jesse" (as speech-to-text heard it) into a real name. The game side is DeckKit's (DeckKit.Ledger records whispers,
-- chat, trades and invites, in memory only; DeckKit.People reads them, and your group and target, and sends
-- invites); this file only packs it for the assistant. Friends and guild online come from FRIENDS (Chat.lua).
-- Packets as in Agent.lua's header (binary, big-endian); deck-setup/decklink/agent.py is the other end.
--
-- PEOPLE (op 20) request:
--   byte 1      kinds: bit 0 whispers, 1 chat, 2 trades, 3 party invites, 4 group, 5 target (0: all)
--   byte 2      only the last N minutes (0: each kind's own window: whispers 30, chat 2, trades 30, invites 10)
-- reply body: whispers, trades, invites, group and target first, then chat (so busy chat can't push the
-- rest out of a full reply); newest first within each:
--   byte 3      bit 0: more didn't fit in one strip frame
--   byte 4      count
--   each        kind uint8, name str, age uint16 (seconds; 65535: longer), then by kind:
--     1 whisper       direction uint8 (1 from them, 2 you to them), text str (the latest, cut to 100 bytes)
--     2 bnet whisper  direction uint8, character str (empty when not in WoW), text str; name is the BattleTag
--     3 chat          channel str ("Say", "Party", "General"...), text str (the latest, cut to 100 bytes)
--     4 trade         (a finished trade) their money uint32 (copper), their items count uint8, each name str
--                     and count uint16; then yours the same
--     5 invite        nothing more (they invited you to a group)
--     6 group         flags uint8 (1 the leader)
--     7 target        nothing more (a player you have targeted)
--   (str = length uint8 + UTF-8). One entry per person and kind: a newer whisper replaces their older one.
--   Never a Battle.net real name (RealID): Battle.net whispers are named by BattleTag.
--
-- INVITE (op 21) request: name str (Name or Name-Realm). Reply body: byte 3 1 = the invite was sent.
local _, ns = ...

local B, S = ns.Bytes, ns.Status
local U8, U16, U32, Str, ReadStr = B.U8, B.U16, B.U32, B.Str, B.ReadStr
local OP_PEOPLE, OP_INVITE = 20, 21
local CODE = { whisper = 1, bnet = 2, chat = 3, trade = 4, invite = 5, group = 6, target = 7 }
local DIRECTION = { ["in"] = 1, out = 2 }
-- Request bits -> DeckKit.People's kinds.
local BITS = { { "whisper", "bnet" }, { "chat" }, { "trade" }, { "invite" }, { "group" }, { "target" } }

local function Side(side)
    local items = side.items or {}
    local parts = { U32(side.money or 0), U8(math.min(#items, 255)) }
    for i = 1, math.min(#items, 255) do
        parts[#parts + 1] = Str(items[i].name) .. U16(math.min(items[i].count or 1, 65535))
    end
    return table.concat(parts)
end

local function Body(e)
    if e.kind == "whisper" then return U8(DIRECTION[e.direction] or 1) .. Str(e.text) end
    if e.kind == "bnet" then return U8(DIRECTION[e.direction] or 1) .. Str(e.character) .. Str(e.text) end
    if e.kind == "chat" then return Str(e.channel) .. Str(e.text) end
    if e.kind == "trade" then return Side(e.theirs or {}) .. Side(e.yours or {}) end
    if e.kind == "group" then return U8(e.leader and 1 or 0) end
    return ""
end

ns.OPS[OP_PEOPLE] = function(p)
    local mask = p:byte(2) or 0
    if mask == 0 then mask = 63 end
    local minutes = p:byte(3) or 0
    local kinds = {}
    for bit, names in ipairs(BITS) do
        if math.floor(mask / 2 ^ (bit - 1)) % 2 == 1 then
            for _, k in ipairs(names) do kinds[k] = true end
        end
    end
    local now = GetServerTime()
    local entries, budget, more = {}, DeckLink.MaxPayload() - 5, false
    for _, e in ipairs(DeckKit.People.Recent(kinds, minutes > 0 and minutes * 60 or nil)) do
        local age = e.t and math.min(65535, math.max(0, now - e.t)) or 0
        local bytes = U8(CODE[e.kind]) .. Str(e.name) .. U16(age) .. Body(e)
        if #bytes > budget then more = true break end
        budget = budget - #bytes
        entries[#entries + 1] = bytes
    end
    return S.OK, U8(more and 1 or 0) .. U8(#entries) .. table.concat(entries)
end

ns.OPS[OP_INVITE] = function(p)
    local name = ReadStr(p, 2)
    if not name or name:gsub("%s", "") == "" then return S.BAD_ARGS, Str("INVITE needs a name") end
    local ok, why = DeckKit.People.Invite(name)
    if not ok then return why == "no name" and S.BAD_ARGS or S.REFUSED, Str(why) end
    ns.Debug("agent invite", name)
    return S.OK, U8(1)
end
