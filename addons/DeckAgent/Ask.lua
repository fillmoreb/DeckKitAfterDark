-- DeckAgent's questions: the assistant asks you something with a prompt (Prompt.lua) and gets the
-- answer back. Packets as in Agent.lua's header (binary, big-endian); deck-setup/decklink/agent.py is
-- the other end.
--
-- ASK (op 5) request:
--   byte 1      bit 0: a list (else 2 options make a dialog: A the first, B the second)
--   byte 2      seconds to wait for an answer (5-120)
--   then        the question: length uint8 + UTF-8
--   then        count uint8 (2-6), then each option: length uint8 + UTF-8
-- reply body:   byte 3: 1 shown, 2 queued (in combat: it shows when the fight ends)
--
-- ANSWER (op 6): sent on its own once you answer, the timeout passes, or another prompt replaces it:
--   byte 0      op byte (version 1, op 6)
--   byte 1      the ASK frame's seq
--   byte 2      status 0
--   byte 3      choice: 1-6 the option, 0 cancelled (B on a list), 255 no answer
-- (DeckLink.Send queues it until the host reads the strip; the host keeps the link open while it waits.)
--
-- DISMISS (op 19) request: byte 1 the ASK frame's seq. Takes that prompt down unanswered (no ANSWER is
-- sent): the host gave up on it (the question it was for was dropped). Only DeckAgent's own latest
-- prompt, never a chat preview.
-- reply body:   byte 3: 1 taken down, 0 it wasn't up any more
local _, ns = ...

local B, S = ns.Bytes, ns.Status
local U8, Str, ReadStr = B.U8, B.Str, B.ReadStr
local OP_ASK, OP_ANSWER, OP_DISMISS = 5, 6, 19
local SHOWN, QUEUED = 1, 2
local NO_ANSWER = 255
local lastSeq, lastSpec -- the latest ASK: its frame's seq and its prompt, for DISMISS

ns.OPS[OP_ASK] = function(p, seq)
    local flags, seconds = p:byte(2), p:byte(3)
    if not (flags and seconds) then return S.BAD_ARGS, Str("ASK needs flags and seconds") end
    local question, i = ReadStr(p, 4)
    local count = question and p:byte(i)
    if not count or count < 2 or count > 6 then return S.BAD_ARGS, Str("ASK needs a question and 2-6 options") end
    i = i + 1
    local options = {}
    for n = 1, count do
        local opt
        opt, i = ReadStr(p, i)
        if not opt or opt == "" then return S.BAD_ARGS, Str("option " .. n .. " is missing") end
        options[n] = (opt:gsub("[%c|]", " "))
    end
    local function Reply(choice)
        local answer = U8(ns.VERSION * 64 + OP_ANSWER) .. U8(seq or 0) .. U8(S.OK) .. U8(choice)
        if not DeckLink.Send(ns.CHANNEL, answer) then ns.Debug("agent", "answer not sent (link closed)") end
    end
    local spec = {
        title = "Decky asks", -- the assistant's name (agent/SYSTEM.md)
        text = (question:gsub("[%c|]", " ")),
        options = options,
        list = flags % 2 == 1,
        seconds = math.max(5, math.min(120, seconds)),
        onAnswer = function(choice) Reply(choice or NO_ANSWER) end,
    }
    lastSeq, lastSpec = seq, spec
    local result = ns.Prompt.Show(spec)
    return S.OK, U8(result == "queued" and QUEUED or SHOWN)
end

ns.OPS[OP_DISMISS] = function(p)
    local seq = p:byte(2)
    if not seq then return S.BAD_ARGS, Str("DISMISS needs the ASK frame's seq") end
    local done = seq == lastSeq and lastSpec and ns.Prompt.Dismiss(lastSpec)
    if done then lastSeq, lastSpec = nil, nil end
    return S.OK, U8(done and 1 or 0)
end
