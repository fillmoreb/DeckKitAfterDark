# Installing DeckKitAfterDark

Two halves: **addons** in the game, and a small **program side** on the Steam Deck (Python, two
background services) that feeds them. Read the README's first paragraph before you install: these
addons exchange data with programs outside the game, a gray area under Blizzard's terms.

## Before you start

- **DeckKit** installed and working (its [INSTALL.md](https://github.com/fillmoreb/DeckKit/blob/main/INSTALL.md)).
- Play in **Game Mode**: DeckLink reads the screen through gamescope's video stream, which only Game Mode has.
- For DeckDictation: **Decktation** 0.3.24 or later (DeckKit's INSTALL.md, "Speech to text").
- **A password for the Deck's `deck` account.** It has none out of the box, and both `sudo` (step 3) and
  SSH need one. In Desktop Mode, open Konsole and run `passwd` once.

## 1. The addons

Copy the folders in `addons/` (**DeckLink**, **DeckTelemetry**, **DeckDictation**) into WoW: Forever's
`_forever_/Interface/AddOns` folder, next to DeckKit's. DeckTelemetry and DeckDictation need DeckLink;
install only the ones you want.

## 2. The program side

It installs into your home folder only (`~/.local`), as the `deck` user, with no system changes:

- the `decklink` package in `~/.local/lib/decklink`
- `~/.local/bin/decklink` (the command) and `~/.local/bin/decklink-decktation-hook` (for Decktation)
- two systemd **user** services, started now and with every boot:
  - `decklink-telemetry`: sends the Deck's battery, power, temperatures, fan and load to DeckTelemetry
    every 5 s
  - `decklink-cover`: hides DeckLink's strip of colored cells from you while a program has the link open

Pick one way:

**On the Deck** (Desktop Mode, Konsole):
```
git clone https://github.com/fillmoreb/DeckKitAfterDark.git
bash DeckKitAfterDark/deck-setup/install-on-deck.sh
```
(No git? Download the repo as a zip from GitHub, unzip it, and run the same script from it.)

**From a Mac or Linux computer, over SSH.** On the Deck first (Desktop Mode, Konsole), turn SSH on with
`sudo systemctl enable --now sshd`. Then, from a copy of this repo on the computer, connect as `deck`:

- Mac or Linux: `./deck-setup/install.sh deck@steamdeck.local`

`steamdeck.local` is the Deck's usual network name; its IP address works too (`deck@192.168.x.x`, in
Settings › Internet on the Deck). ssh asks for the Deck's password.

**Updating:** get the new version first, then run the same install again. In a git copy,
`git -C DeckKitAfterDark pull --ff-only`; from a zip, download and unzip a fresh one. Then copy the
addon folders again (step 1) and rerun the install (step 2).

## 3. Decktation's status hook (for DeckDictation)

Decktation runs a command each time its state changes; DeckLink carries that into the game. Decktation
keeps that setting in a file owned by root, so on the Deck, in Konsole, run these three commands (the
first keeps a backup of the file; the second sets the one setting and leaves the rest as they are; the
third restarts Decky to load it):
```
sudo cp /home/deck/homebrew/settings/decktation/button_config.json{,.bak}
sudo python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["statusHookCommand"]="/home/deck/.local/bin/decklink-decktation-hook"; json.dump(d, open(p, "w"), indent=2)' /home/deck/homebrew/settings/decktation/button_config.json
sudo systemctl restart plugin_loader
```
If Decktation misbehaves afterwards, put the backup back:
`sudo cp /home/deck/homebrew/settings/decktation/button_config.json{.bak,}` and restart Decky again.

## 4. Check it

- In the game: `/deckkit telemetry` prints the latest readings (DeckHUD's status bar can show them:
  Screen › Status bar). `/deckkit link` shows how the link is going.
- On the Deck: `systemctl --user status decklink-telemetry decklink-cover`, and in Game Mode with the game
  running, `decklink status` shows the strip as it reads it.

## Uninstalling

```
systemctl --user disable --now decklink-cover decklink-telemetry
rm -rf ~/.local/lib/decklink ~/.local/bin/decklink ~/.local/bin/decklink-decktation-hook \
  ~/.config/systemd/user/decklink-cover.service ~/.config/systemd/user/decklink-telemetry.service
```
Put Decktation's settings back from the backup (step 3), or run step 3's python command with `d.pop("statusHookCommand", None)` in place of the assignment, then delete the addon folders.
