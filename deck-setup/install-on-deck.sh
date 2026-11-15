#!/bin/bash
# Installs DeckLink's program side, run ON the Steam Deck as the desktop user (deck), from this folder
# (Konsole in Desktop Mode: `bash install-on-deck.sh`; install.sh does the same from a Mac or PC over SSH):
#   the decklink package into ~/.local/lib; `decklink` and `decklink-decktation-hook` (Decktation's
#   statusHookCommand) into ~/.local/bin; and the decklink-cover, decklink-telemetry and decklink-agent
#   (DeckAgent's link keeper) systemd user services, enabled, so they start with the Deck. DeckAgent
#   itself (../agent: the assistant's launcher, MCP server and database tools) goes into
#   ~/.local/lib/deckagent, with `deckagent` and `deckagent-update` in ~/.local/bin, and the
#   deckagent-update timer: once a day it rebuilds the game database when QuestieDB has changed
#   (agent/update_data.py; the first build starts right after an install that has no database yet).
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
AGENT=agent; [ -d "$AGENT" ] || AGENT=../agent  # install.sh ships it alongside; in the repo it's one up
if [ -d "$AGENT" ]; then
  rm -rf ~/.local/lib/deckagent
  mkdir -p ~/.local/lib/deckagent
  cp -r "$AGENT" ~/.local/lib/deckagent/agent
  rm -rf ~/.local/lib/deckagent/agent/__pycache__
  cat > ~/.local/bin/deckagent <<'SH'
#!/bin/sh
exec python3 /home/deck/.local/lib/deckagent/agent/deckagent "$@"
SH
  chmod +x ~/.local/bin/deckagent
  cat > ~/.local/bin/deckagent-update <<'SH'
#!/bin/sh
# Rebuilds DeckAgent's game database from QuestieDB now (add --if-changed to skip when it hasn't changed).
cd /home/deck/.local/lib/deckagent && exec python3 -m agent.update_data "$@"
SH
  chmod +x ~/.local/bin/deckagent-update
  cat > ~/.config/systemd/user/deckagent-update.service <<'UNIT'
[Unit]
Description=DeckAgent game database: rebuild from QuestieDB when it has changed
After=network-online.target
# The timer catches up on a missed day as the Deck wakes, often before Wi-Fi is back: a failed run
# tries again every 5 minutes, up to 6 times.
StartLimitIntervalSec=2h
StartLimitBurst=6
[Service]
Type=oneshot
WorkingDirectory=/home/deck/.local/lib/deckagent
ExecStart=/usr/bin/python3 -m agent.update_data --if-changed
Restart=on-failure
RestartSec=5min
Nice=10
UNIT
  cat > ~/.config/systemd/user/deckagent-update.timer <<'UNIT'
[Unit]
Description=DeckAgent game database: check QuestieDB for changes once a day
[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true
[Install]
WantedBy=timers.target
UNIT
  UPDATER=1
fi
cat > ~/.local/bin/decklink-agent-ask <<'SH'
#!/bin/sh
# Decktation's agent binding command (agentBinding.command in its button_config.json): hands the
# transcript, its last argument, to DeckAgent's link keeper to answer out loud.
PYTHONPATH=/home/deck/.local/lib exec python3 -m decklink.agentd --ask "$1"
SH
chmod +x ~/.local/bin/decklink-agent-ask
# Decktation's WoW profile gets the WoW words (wow-vocabulary.txt) as hotwords, the hint Whisper gets
# before every recording. profiles.json may hold other things of yours: only wow.hotwords is replaced.
# Decktation reads it when it starts (restart Decky, or the Deck).
if [ -f wow-vocabulary.txt ] && [ -d /home/deck/homebrew/settings/decktation ]; then
  python3 - wow-vocabulary.txt /home/deck/homebrew/settings/decktation/profiles.json <<'PY'
import json, os, sys
words = [l.strip() for l in open(sys.argv[1]) if l.strip() and not l.lstrip().startswith("#")]
path = sys.argv[2]
try:
    profiles = json.load(open(path))
except (OSError, ValueError):
    profiles = {}
profiles.setdefault("wow", {})["hotwords"] = words
with open(path + ".tmp", "w") as f:
    json.dump(profiles, f, indent=2)
os.replace(path + ".tmp", path)
print("Decktation WoW hotwords: %d words" % len(words))
PY
fi
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
cat > ~/.config/systemd/user/decklink-agent.service <<'UNIT'
[Unit]
Description=DeckLink agent link keeper: holds DeckLink open for DeckAgent while it's being asked things
[Service]
Environment=PYTHONPATH=/home/deck/.local/lib
ExecStart=/usr/bin/python3 -m decklink.agentd --voice
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
UNIT
export XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus
systemctl --user stop decklink-cover decklink-keepalive 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable decklink-cover decklink-telemetry decklink-agent >/dev/null 2>&1
systemctl --user restart decklink-cover decklink-telemetry decklink-agent
systemctl --user is-active decklink-cover decklink-telemetry decklink-agent
if [ -n "${UPDATER:-}" ]; then
  systemctl --user enable --now deckagent-update.timer >/dev/null 2>&1
  # No database yet: build one now, in the background (a minute or two; `journalctl --user -u
  # deckagent-update` shows how it went).
  [ -f ~/.deckagent/deckagent.db ] || systemctl --user start --no-block deckagent-update.service
  echo "deckagent-update timer: $(systemctl --user is-active deckagent-update.timer)"
fi
echo "decklink installed, with the decklink-cover, decklink-telemetry and decklink-agent user services"
