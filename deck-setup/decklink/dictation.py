"""Dictation states for DeckDictation (addons/DeckDictation), on DeckLink channel 2, fire and forget.

The payload is 2 bytes (keep in step with Dictation.lua):
    0   state: 1 recording, 2 transcribing, 3 done, 4 error, 5 thinking (an agent working on an answer)
    1   binding: 0 chat (Decktation's dictation), 1 agent (DeckAgent's voice questions)

While a dictation is under way (recording until done or error) the keys are held for it (link.hold),
so telemetry doesn't take them and the next state goes straight in.
"""
import time

from . import link

CHANNEL = 2
STATES = {"recording": 1, "transcribing": 2, "done": 3, "error": 4, "thinking": 5}
BINDINGS = {"chat": 0, "agent": 1}


def encode(state, binding="chat"):
    return bytes([STATES[state], BINDINGS[binding]])


def post(state, binding="chat", sender=None, seq=None):
    """Sends the state and holds or frees the keys to match."""
    link.hold(state in ("recording", "transcribing", "thinking"))
    link.post(CHANNEL, seq or int(time.time() * 10) % 255 + 1, encode(state, binding), sender)
