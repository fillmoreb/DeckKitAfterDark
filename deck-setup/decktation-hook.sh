#!/bin/sh
# Decktation status hook -> DeckDictation (DeckKitAfterDark) -> DeckKit's dictation indicator.
# Decktation runs this with DECKTATION_EVENT set (0.3.24+: as the desktop user; it gives a hook 3 s),
# and DECKTATION_BINDING (DeckKit's fork: "chat" for dictation, "agent" for DeckAgent's voice
# questions). It posts the state on DeckLink channel 2 as 2 bytes (`decklink dictation`, dictation.py),
# fire and forget, holding the keys from recording until done so telemetry can't delay the next state:
#   record_start -> recording   record_stop -> transcribing   transcription_done / record_abort -> done
#   error -> error
# With the agent binding, a finished transcript leaves the box up: DeckAgent closes it once it has
# answered (deck-setup/decklink/voice.py).
# install.sh puts it at ~/.local/bin/decklink-decktation-hook; set Decktation's "statusHookCommand"
# (/home/deck/homebrew/settings/decktation/button_config.json) to that path.
binding=chat
[ "$DECKTATION_BINDING" = agent ] && binding=agent
case "$DECKTATION_EVENT" in
    record_start) state=recording ;;
    record_stop) state=transcribing ;;
    transcription_done) [ "$binding" = agent ] && exit 0; state=done ;;
    record_abort) state=done ;;
    error) state=error ;;
    *) exit 0 ;;
esac
# Quickest: the running decklink-agent service posts it (socat, no Python to start: ~0.15 s sooner).
# Without the service, decklink itself does.
sock=/run/user/1000/deckagent.sock
if [ "$(id -u)" != 0 ] && [ -S "$sock" ] && command -v socat >/dev/null &&
    printf '{"cmd": "dictation", "state": "%s", "binding": "%s"}\n' "$state" "$binding" |
    socat -t 3 - "UNIX-CONNECT:$sock" 2>/dev/null | grep -q '"ok": true'; then
    exit 0
fi
if [ "$(id -u)" = 0 ]; then
    exec runuser -u deck -- /home/deck/.local/bin/decklink --tool xdotool dictation "$state" --binding "$binding"
fi
exec /home/deck/.local/bin/decklink --tool xdotool dictation "$state" --binding "$binding"
