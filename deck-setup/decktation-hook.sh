#!/bin/sh
# Decktation status hook -> DeckDictation (DeckKitAfterDark) -> DeckKit's dictation indicator.
# Decktation runs this with DECKTATION_EVENT set (0.3.24+: as the desktop user; it gives a hook 3 s).
# It posts the state on DeckLink channel 2, fire and forget (about 0.3 s of paced key taps):
#   record_start -> recording   record_stop -> transcribing   transcription_done / record_abort -> done
#   error -> error
# install.sh puts it at ~/.local/bin/decklink-decktation-hook; set Decktation's "statusHookCommand"
# (/home/deck/homebrew/settings/decktation/button_config.json) to that path.
case "$DECKTATION_EVENT" in
    record_start) state=recording ;;
    record_stop) state=transcribing ;;
    transcription_done|record_abort) state=done ;;
    error) state=error ;;
    *) exit 0 ;;
esac
if [ "$(id -u)" = 0 ]; then
    exec runuser -u deck -- /home/deck/.local/bin/decklink --tool xdotool post 2 "$state"
fi
exec /home/deck/.local/bin/decklink --tool xdotool post 2 "$state"
