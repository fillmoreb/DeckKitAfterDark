#!/bin/bash
# Installs DeckLink's program side, run ON the Steam Deck as the desktop user (deck), from this folder
# (Konsole in Desktop Mode: `bash install-on-deck.sh`; install.sh does the same from a Mac or PC over SSH):
#   the decklink package into ~/.local/lib; `decklink` and `decklink-decktation-hook` (Decktation's
#   statusHookCommand) into ~/.local/bin; and the decklink-cover and decklink-telemetry systemd user
#   services, enabled, so they start with the Deck.
set -euo pipefail
cd "$(dirname "$0")"
[ "$(id -un)" = deck ] || { echo "run this as the deck user (not root)" >&2; exit 1; }
mkdir -p ~/.local/lib ~/.local/bin ~/.config/systemd/user
rm -rf ~/.local/lib/decklink
cp -r decklink ~/.local/lib/decklink
rm -rf ~/.local/lib/decklink/__pycache__
cat > ~/.local/bin/decklink <<'SH'
#!/bin/sh
PYTHONPATH=/home/deck/.local/lib exec python3 -m decklink "$@"
SH
chmod +x ~/.local/bin/decklink
cp decktation-hook.sh ~/.local/bin/decklink-decktation-hook
chmod +x ~/.local/bin/decklink-decktation-hook
cat > ~/.config/systemd/user/decklink-cover.service <<'UNIT'
[Unit]
Description=DeckLink cover: hides the link strip from the player while the link is open
[Service]
Environment=DISPLAY=:0 PYTHONPATH=/home/deck/.local/lib
ExecStart=/usr/bin/python3 -m decklink.cover
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
UNIT
cat > ~/.config/systemd/user/decklink-telemetry.service <<'UNIT'
[Unit]
Description=DeckLink telemetry: battery, power, temperatures and load into the game (DeckTelemetry)
[Service]
Environment=PYTHONPATH=/home/deck/.local/lib
ExecStart=/usr/bin/python3 -m decklink.telemetry --tool xdotool
Restart=always
RestartSec=10
[Install]
WantedBy=default.target
UNIT
export XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
systemctl --user stop decklink-cover decklink-keepalive 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable decklink-cover decklink-telemetry >/dev/null 2>&1
systemctl --user restart decklink-cover decklink-telemetry
systemctl --user is-active decklink-cover decklink-telemetry
echo "decklink installed, with the decklink-cover and decklink-telemetry user services"
