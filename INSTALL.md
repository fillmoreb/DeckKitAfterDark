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

## DeckAgent: notes for the install steps (still to be written up)

A running list of everything a DeckAgent install needs, kept as we build it so nothing is lost. Fold
it into numbered steps above once DeckAgent settles. "Done by the installer" means
`install-on-deck.sh` (or `install.sh`) already does it.

**Addons:** also copy **DeckAgent** (needs DeckLink; WayPad from DeckKit is optional, for waypoints).

**Done by the installer**
- `~/.local/lib/deckagent` (launcher, MCP server, database tools), `~/.local/bin/deckagent`,
  `~/.local/bin/deckagent-update`, `~/.local/bin/decklink-agent-ask` (Decktation's agent binding
  command).
- The `decklink-agent` user service: the link keeper, which also answers voice questions (`--voice`).
- Decktation's WoW hotwords (`wow-vocabulary.txt` into its `profiles.json`, `wow.hotwords` only).
- **The game database** (`~/.deckagent/deckagent.db`): the first install starts a build in the
  background (a minute or two, needs internet). The `deckagent-update.timer` checks QuestieDB once
  a day and rebuilds only when it has a new commit. If the Deck was asleep or off, the check runs
  as it wakes; a failed run (no Wi-Fi yet) retries every 5 minutes, up to 6 times. A database
  built without QuestieDB's corrections never counts as current, so the next check tries again.
  - It downloads the QuestieDB repository (~96 MB; ~112 MB unpacked, deleted after) and runs
    QuestieDB's own `tools/export-forever.lua` with **Lua 5.1**: an installed `lua5.1`, else the
    static Lua 5.1.5 QuestieDB ships for Linux x64. **Not LuaJIT** (SteamOS has it at
    /usr/bin/luajit): it fails on QuestieDB's biggest files ("more than 65536 constants").
  - Without a working Lua it falls back to QuestieDB's raw `data/Forever` files. Those are only
    Forever's own rows, uncorrected: about two thirds of the quests (4,244 vs 5,914) and a third
    of the spawns (55k vs 141k). The build log says `corrections: no`.
  - By hand: `deckagent-update` (rebuild now) or `deckagent-update --if-changed`. Status:
    `journalctl --user -u deckagent-update`, `systemctl --user list-timers deckagent-update.timer`.
  - Never commit or redistribute the data (QuestieDB has no license file): download only.

**Manual today (to automate or document)**
- **Claude Code** on the Deck (`~/.local/bin/claude`) and signed in to a Claude plan (`claude`,
  then log in). `deckagent` warns if it ever runs on an API key instead.
- **Piper** (text to speech) for spoken answers: the `piper` binary and the
  `en_US-lessac-medium.onnx` + `.onnx.json` voice in `~/.local/share/deckagent/piper/`.
- **Decktation**: Ben's fork, with an **agent binding** (L5+R5) that hands its transcript to a
  command. In `button_config.json` (root-owned, like step 3):
  `"agentBinding": {"buttons": ["L5", "R5"], "command": "/home/deck/.local/bin/decklink-agent-ask"}`,
  `"exactBindingMs": 150`, and `"statusHookCommand"` as in step 3. Then restart Decky
  (`sudo systemctl restart plugin_loader`). This can drop the game's controller focus, so do it
  outside the game or expect to refocus.
- The speech model in Decktation's settings: **parakeet-v3** recommended (Ben, 2026-11-14: "has
  been working much better" than Whisper). It runs on the CPU and takes no hotwords, so the WoW
  hotwords above only help the Whisper models (base, small ~465 MB, medium).
- In DeckKit's menu: put **DeckKit Assistant** (`DECKKIT_ASSISTANT`) on the L5+R5 grip chord, so the
  dictation box says "Starting" and game sound fades while you ask. Tools › DeckAgent has the model
  pick, Quiet, chat preview, and "Clear Decky's checklist" (also offered to the grip chords, so
  her checklist can be taken down with the controller alone).
- First use: tell Decky who your friends are; she writes them under People in
  `~/.deckagent/CLAUDE.md`. Her facts go in `~/.deckagent/memory.db`, her hooks' notes and timers in
  `~/.deckagent/hooks.json`. All personal; they stay out of the repo.

**Requirements already on SteamOS** (checked 2026-11-14): Python 3.13, GStreamer with PyGObject,
PipeWire tools (`pw-dump`, `pw-play`), `xdotool`. The paste route needs `xclip`, which SteamOS
lacks: DeckLink uses `~/.local/bin/xclip` if present, else the copy Decktation ships.

**Gotchas met while building**
- SteamOS sets `KillUserProcesses=True`: anything started over SSH dies when the login ends unless
  it's a systemd user service (or started under `systemd-run --user --scope`).
- The screen reader asks gamescope's stream for BGRx by name: with a crop in the pipeline it would
  otherwise fall back to NV12, whose colors fail the strip's checksum.
