-- DeckDictation: Decktation's exact state for DeckKit's dictation indicator. Decktation's status hook
-- (deck-setup/decktation-hook.sh) posts it on DeckLink channel 2 as text: "recording", "transcribing",
-- "done" or "error". DeckKit.Dictation.SetState shows it; without this addon the indicator goes by the
-- L4 + R4 chord alone.
local CHANNEL = 2
local STATES = { recording = true, transcribing = true, done = true, error = true }

DeckLink.On(CHANNEL, function(payload)
    if STATES[payload] and DeckKit.Dictation then DeckKit.Dictation.SetState(payload) end
end)
