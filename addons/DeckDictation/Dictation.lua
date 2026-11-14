-- DeckDictation: Decktation's exact state for DeckKit's dictation indicator. Decktation's status hook
-- (deck-setup/decktation-hook.sh) and DeckAgent's voice (deck-setup/decklink/voice.py) post it on
-- DeckLink channel 2 as 2 bytes (deck-setup/decklink/dictation.py; keep the two in step):
--   byte 0  state: 1 recording, 2 transcribing, 3 done, 4 error, 5 thinking (an agent answering)
--   byte 1  binding: 0 chat (dictation), 1 agent (a question for DeckAgent)
-- DeckKit.Dictation.SetState(state, binding) shows it; without this addon the indicator goes by the
-- L4 + R4 chord alone.
local CHANNEL = 2

-- The indicator follows the real state from here on: DeckKit never shows "Listening" from the grips.
if DeckKit.Dictation and DeckKit.Dictation.Claim then DeckKit.Dictation.Claim() end
local STATES = { "recording", "transcribing", "done", "error", "thinking" }
local BINDINGS = { [0] = "chat", [1] = "agent" }

DeckLink.On(CHANNEL, function(payload)
    local state = STATES[payload:byte(1) or 0]
    local binding = BINDINGS[payload:byte(2) or 0] or "chat"
    if state and DeckKit.Dictation then DeckKit.Dictation.SetState(state, binding) end
end)
