-- DeckAgent's view of the quest log: numbers only. Names, objectives' wording and where things are
-- come from the assistant's own quest database (agent/gamedb.py, from QuestieDB), by quest ID. Packets
-- as in Agent.lua's header (binary, big-endian); deck-setup/decklink/agent.py is the other end.
--
-- QUESTS (op 8) request: just the op byte.
-- reply body:
--   byte 3      bit 0: more didn't fit in one strip frame
--   byte 4      count
--   each quest  quest ID uint32, flags uint8 (bit 0 complete: ready to turn in, bit 1 failed),
--               objective count uint8, then each objective: kind uint8 (low 7 bits: 1 monster, 2 item,
--               3 object, 4 event, 5 reputation, 6 other; bit 7 done), have uint8, need uint8
--               (counts over 255 are sent as 255)
-- Objectives come in the game's order, which follows the quest's: kills and objects, then items.
--
-- QUEST_STATUS (op 9) request: count uint8, then that many quest IDs, uint32.
-- reply body:   count uint8, then one status uint8 per ID, in order: 0 not taken, 1 in the log,
--               2 in the log and ready to turn in, 3 done (turned in)
local _, ns = ...

local B, S = ns.Bytes, ns.Status
local U8, U32, Str = B.U8, B.U32, B.Str
local OP_QUESTS, OP_QUEST_STATUS = 8, 9
local KINDS = { monster = 1, item = 2, object = 3, event = 4, reputation = 5 }
local OTHER = 6

local function Count(n) return math.max(0, math.min(255, math.floor(tonumber(n) or 0))) end

local function Entry(questID)
    local flags = 0
    if C_QuestLog.IsComplete and C_QuestLog.IsComplete(questID) then flags = flags + 1 end
    if C_QuestLog.IsFailed and C_QuestLog.IsFailed(questID) then flags = flags + 2 end
    local goals = C_QuestLog.GetQuestObjectives and C_QuestLog.GetQuestObjectives(questID) or {}
    local parts = {}
    for i = 1, math.min(#goals, 255) do
        local g = goals[i]
        local kind = (KINDS[g.type] or OTHER) + (g.finished and 128 or 0)
        parts[i] = U8(kind) .. U8(Count(g.numFulfilled)) .. U8(Count(g.numRequired))
    end
    return U32(questID) .. U8(flags) .. U8(#parts) .. table.concat(parts)
end

ns.OPS[OP_QUESTS] = function()
    local entries, budget, more = {}, DeckLink.MaxPayload() - 5, false
    for i = 1, C_QuestLog.GetNumQuestLogEntries() do
        local info = C_QuestLog.GetInfo(i)
        if info and not info.isHeader and info.questID and info.questID > 0 then
            local entry = Entry(info.questID)
            if #entry > budget then
                more = true
                break
            end
            budget = budget - #entry
            entries[#entries + 1] = entry
        end
    end
    return S.OK, U8(more and 1 or 0) .. U8(#entries) .. table.concat(entries)
end

local NOT_TAKEN, IN_LOG, READY, DONE = 0, 1, 2, 3

ns.OPS[OP_QUEST_STATUS] = function(p)
    local n = p:byte(2)
    if not n or #p < 2 + 4 * n then return S.BAD_ARGS, Str("QUEST_STATUS needs a count and that many IDs") end
    local out = {}
    for k = 1, n do
        local i = 3 + (k - 1) * 4
        local id = B.ReadU32(p, i)
        local status = NOT_TAKEN
        if C_QuestLog.IsQuestFlaggedCompleted and C_QuestLog.IsQuestFlaggedCompleted(id) then
            status = DONE
        elseif C_QuestLog.IsOnQuest and C_QuestLog.IsOnQuest(id) then
            status = C_QuestLog.IsComplete and C_QuestLog.IsComplete(id) and READY or IN_LOG
        end
        out[k] = U8(status)
    end
    return S.OK, U8(n) .. table.concat(out)
end
