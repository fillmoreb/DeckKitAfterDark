-- DeckAgent's chat: the assistant can send chat for you (CHAT) and see who's online (FRIENDS).
-- Packets as in Agent.lua's header (binary, big-endian); deck-setup/decklink/agent.py is the other end.
--
-- CHAT (op 3) request:
--   byte 1      channel: 1 say, 2 yell, 3 party, 4 raid, 5 guild, 6 whisper (a character),
--               7 Battle.net whisper (a friend: BattleTag, its name part, or account ID), 8 instance
--   then        target: length uint8 + UTF-8 (empty unless whisper or Battle.net whisper)
--   then        text: length uint8 + UTF-8, 1-255 bytes (chat's own limit)
-- reply body:   byte 3 what happened: 1 sent, 2 previewed (waiting for you), 3 previewed after
--               combat (it shows when the fight ends)
--   Never a public channel (Trade, General...): they aren't on the list.
--
-- "Preview agent chat" (DeckKit menu, Tools > DeckAgent): the message waits in a prompt (Prompt.lua)
-- with {A} Send / {B} Cancel instead of going straight out. The send then runs inside your own A
-- press, a real hardware event. Untouched, it cancels after 30 s; a new prompt replaces it.
--
-- FRIENDS (op 4) request:
--   byte 1      lists: bit 0 Battle.net friends, bit 1 character friends, bit 2 guild
-- reply body (online people only):
--   byte 3      bit 0: more didn't fit in one strip frame
--   byte 4      count
--   each        kind uint8, then
--                 1 Battle.net: account ID uint32, BattleTag str, in WoW uint8, and if in WoW:
--                   character str, realm str, level uint8, class str, zone str
--                 2 character friend / 3 guild: name str, level uint8, class str, zone str
--               (str = length uint8 + UTF-8). Never a Battle.net real name (RealID).
local _, ns = ...

local B, S = ns.Bytes, ns.Status
local U8, U16, U32, Str, ReadStr = B.U8, B.U16, B.U32, B.Str, B.ReadStr
local OP_CHAT, OP_FRIENDS = 3, 4
local CHANNELS = { "SAY", "YELL", "PARTY", "RAID", "GUILD", "WHISPER", "BN_WHISPER", "INSTANCE_CHAT" }
local LABELS = { SAY = "Say", YELL = "Yell", PARTY = "Party", RAID = "Raid", GUILD = "Guild", WHISPER = "Whisper",
    BN_WHISPER = "Whisper", INSTANCE_CHAT = "Instance" }
local SENT, PREVIEWED, PREVIEW_AFTER_COMBAT = 1, 2, 3
local PREVIEW_SECONDS = 30

DeckAgentDB = DeckAgentDB or {}
local function Preview() return DeckAgentDB.previewChat == true end

-- Chat can't carry control characters, and "|" starts the game's escape codes (links, colors).
local function Clean(text)
    return (text:gsub("[%c]", " "):gsub("|", "/"))
end

------------------------------------------------------------------------
-- Who's who on Battle.net
------------------------------------------------------------------------

local function BNFriends()
    local out = {}
    local n = BNGetNumFriends and BNGetNumFriends() or 0
    for i = 1, n do
        local a = C_BattleNet and C_BattleNet.GetFriendAccountInfo and C_BattleNet.GetFriendAccountInfo(i)
        if a then out[#out + 1] = a end
    end
    return out
end

-- "Edna#1234", "edna" or "12345" -> the friend's account ID (online friends only).
local function FindBNFriend(target)
    local lower, id = target:lower(), tonumber(target)
    for _, a in ipairs(BNFriends()) do
        local tag = (a.battleTag or ""):lower()
        local online = a.gameAccountInfo and a.gameAccountInfo.isOnline
        if online and (a.bnetAccountID == id or tag == lower or tag:match("^[^#]*") == lower) then
            return a.bnetAccountID, a.battleTag
        end
    end
end

------------------------------------------------------------------------
-- Sending
------------------------------------------------------------------------

-- Whether this message can go out now: nil, or why not.
local function Problem(d)
    if d.channel == "PARTY" and not IsInGroup() then return "you're not in a group" end
    if d.channel == "RAID" and not IsInRaid() then return "you're not in a raid" end
    if d.channel == "GUILD" and not IsInGuild() then return "you're not in a guild" end
    if d.channel == "INSTANCE_CHAT" and not IsInGroup(LE_PARTY_CATEGORY_INSTANCE or 2) then
        return "you're not in an instance group"
    end
    if d.channel == "WHISPER" and d.target == "" then return "whisper needs a name" end
    if d.channel == "BN_WHISPER" and not d.bnetID then return "no online Battle.net friend " .. d.target end
end

local function Send(d)
    if d.channel == "BN_WHISPER" then
        BNSendWhisper(d.bnetID, d.text)
    elseif d.channel == "WHISPER" then
        SendChatMessage(d.text, "WHISPER", nil, d.target)
    else
        SendChatMessage(d.text, d.channel)
    end
    ns.Debug("agent chat sent", d.channel, d.target)
end

------------------------------------------------------------------------
-- The preview: a prompt (Prompt.lua), {A} Send / {B} Cancel
------------------------------------------------------------------------

local function ShowPreview(d)
    return ns.Prompt.Show({
        title = "Send this for you?",
        subtitle = LABELS[d.channel] .. (d.target ~= "" and (": " .. (d.shown or d.target)) or ""),
        text = d.text,
        options = { "Send", "Cancel" },
        seconds = PREVIEW_SECONDS,
        onAnswer = function(choice)
            if choice == 1 then
                local why = Problem(d) -- still in the group, still online?
                if why then ns.Print("not sent: " .. why) else Send(d) end
            elseif choice == nil then
                ns.Print("agent message not sent (no answer in " .. PREVIEW_SECONDS .. " s)")
            end
        end,
    })
end

------------------------------------------------------------------------
-- The ops
------------------------------------------------------------------------

ns.OPS[OP_CHAT] = function(p)
    local channel = CHANNELS[p:byte(2) or 0]
    if not channel then return S.BAD_ARGS, Str("unknown channel") end
    local target, i = ReadStr(p, 3)
    local text = target and ReadStr(p, i)
    if not text or text == "" then return S.BAD_ARGS, Str("CHAT needs a target and a text") end
    local d = { channel = channel, target = target, text = Clean(text) }
    if channel == "BN_WHISPER" then d.bnetID, d.shown = FindBNFriend(target) end
    local why = Problem(d)
    if why then return S.REFUSED, Str(why) end
    if not Preview() then
        Send(d)
        return S.OK, U8(SENT)
    end
    return S.OK, U8(ShowPreview(d) == "queued" and PREVIEW_AFTER_COMBAT or PREVIEWED)
end

ns.OPS[OP_FRIENDS] = function(p)
    local lists = p:byte(2) or 7
    local entries, budget, more = {}, DeckLink.MaxPayload() - 5, false
    local function Add(entry)
        if #entry > budget then more = true return false end
        budget = budget - #entry
        entries[#entries + 1] = entry
        return true
    end
    if lists % 2 >= 1 then -- Battle.net friends that are online
        for _, a in ipairs(BNFriends()) do
            local g = a.gameAccountInfo
            if g and g.isOnline then
                local inWoW = g.clientProgram == "WoW" and g.characterName and g.characterName ~= ""
                local e = U8(1) .. U32(a.bnetAccountID or 0) .. Str(a.battleTag) .. U8(inWoW and 1 or 0)
                if inWoW then
                    e = e .. Str(g.characterName) .. Str(g.realmName) .. U8(g.characterLevel or 0)
                        .. Str(g.className) .. Str(g.areaName)
                end
                if not Add(e) then break end
            end
        end
    end
    if lists % 4 >= 2 and C_FriendList then -- character friends
        for i = 1, C_FriendList.GetNumFriends() or 0 do
            local f = C_FriendList.GetFriendInfoByIndex(i)
            if f and f.connected then
                if not Add(U8(2) .. Str(f.name) .. U8(f.level or 0) .. Str(f.className) .. Str(f.area)) then break end
            end
        end
    end
    if lists % 8 >= 4 and IsInGuild() then -- guild members online
        for i = 1, GetNumGuildMembers() or 0 do
            local name, _, _, level, class, zone, _, _, online = GetGuildRosterInfo(i)
            if online and name then
                if not Add(U8(3) .. Str(name) .. U8(level or 0) .. Str(class) .. Str(zone)) then break end
            end
        end
    end
    return S.OK, U8(more and 1 or 0) .. U8(#entries) .. table.concat(entries)
end

------------------------------------------------------------------------
-- Settings
------------------------------------------------------------------------

ns.AddSettings({
    { type = "section", tab = "Tools", label = "DeckAgent" },
    { type = "toggle", label = "Preview agent chat before sending",
      hint = "Messages the assistant sends for you wait in a small window: {A} Send, {B} Cancel. "
          .. "Off: they go straight out.",
      get = Preview,
      set = function(on) DeckAgentDB.previewChat = on end },
    { type = "toggle", label = "Quiet: show answers instead of speaking them",
      hint = "For voice chat or a quiet room: answers go on a card and in chat, nothing is said out loud.",
      get = function() return DeckAgentDB.quiet == true end,
      set = function(on) DeckAgentDB.quiet = on or nil end },
    { type = "choice", label = "Model",
      values = { { false, "Default (Sonnet)" }, { "haiku", "Haiku" }, { "sonnet", "Sonnet" }, { "opus", "Opus" } },
      hint = "Which Claude model answers. Haiku is quickest; Sonnet and Opus think harder but take a few "
          .. "seconds longer and use more of your plan. Takes effect on the next question.",
      get = function() return DeckAgentDB.model or false end,
      set = function(v) DeckAgentDB.model = v or nil end },
})
