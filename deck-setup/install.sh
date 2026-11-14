#!/bin/bash
# Installs DeckLink's program side on the Deck, owned by deck: the decklink package into
# /home/deck/.local/lib; `decklink` and `decklink-decktation-hook` (Decktation's statusHookCommand) into
# /home/deck/.local/bin; and the decklink-cover and decklink-telemetry systemd user services, enabled.
# Uses DECK from ../deploy.local.
set -euo pipefail
cd "$(dirname "$0")"
[ -f ../deploy.local ] && . ../deploy.local
: "${DECK:?set DECK in deploy.local}"
COPYFILE_DISABLE=1 tar --no-xattrs --exclude __pycache__ -cf - decklink decktation-hook.sh | ssh "$DECK" 'sudo -u deck bash -c "
set -e
mkdir -p ~/.local/lib ~/.local/bin
rm -rf ~/.local/lib/decklink
cd ~/.local/lib && tar xf -
cat > ~/.local/bin/decklink <<EOF
#!/bin/sh
PYTHONPATH=/home/deck/.local/lib exec python3 -m decklink \"\\\$@\"
EOF
chmod +x ~/.local/bin/decklink
mv ~/.local/lib/decktation-hook.sh ~/.local/bin/decklink-decktation-hook
chmod +x ~/.local/bin/decklink-decktation-hook
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/decklink-cover.service <<EOF
[Unit]
Description=DeckLink cover: hides the link strip from the player while the link is open
[Service]
Environment=DISPLAY=:0 PYTHONPATH=/home/deck/.local/lib
ExecStart=/usr/bin/python3 -m decklink.cover
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
EOF
cat > ~/.config/systemd/user/decklink-telemetry.service <<EOF
[Unit]
Description=DeckLink telemetry: battery, power, temperatures and load into the game (DeckTelemetry)
[Service]
Environment=PYTHONPATH=/home/deck/.local/lib
ExecStart=/usr/bin/python3 -m decklink.telemetry --tool xdotool
Restart=always
RestartSec=10
[Install]
WantedBy=default.target
EOF
export XDG_RUNTIME_DIR=/run/user/\$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/\$(id -u)/bus
systemctl --user stop decklink-cover decklink-keepalive 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable decklink-cover decklink-telemetry >/dev/null 2>&1
systemctl --user restart decklink-cover decklink-telemetry
systemctl --user is-active decklink-cover decklink-telemetry
"'
echo "installed decklink on $DECK, with the decklink-cover and decklink-telemetry user services"
