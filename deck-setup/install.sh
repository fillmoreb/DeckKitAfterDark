#!/bin/bash
# Installs DeckLink's program side on the Deck from a Mac or Linux PC over SSH: copies this folder over and
# runs install-on-deck.sh there as deck (see it for what's installed). Windows: install.bat. On the Deck
# itself: install-on-deck.sh.
# The Deck's address comes from DECK in ../deploy.local (e.g. DECK=deck@steamdeck.local), or the first
# argument: ./install.sh deck@steamdeck.local
set -euo pipefail
cd "$(dirname "$0")"
[ -f ../deploy.local ] && . ../deploy.local
DECK="${1:-${DECK:-}}"
: "${DECK:?the Deck address, e.g. ./install.sh deck@steamdeck.local (or set DECK in deploy.local)}"
# Connected as deck: run it straight. As another account (with sudo): run it as deck.
REMOTE='d=$(mktemp -d) && chmod 755 "$d" && cd "$d" && tar xf - && \
  if [ "$(id -un)" = deck ]; then bash install-on-deck.sh; else sudo -u deck bash install-on-deck.sh; fi; \
  cd / && rm -rf "$d"'
COPYFILE_DISABLE=1 tar --no-xattrs --exclude __pycache__ -cf - decklink decktation-hook.sh install-on-deck.sh \
  | ssh "$DECK" "$REMOTE"
