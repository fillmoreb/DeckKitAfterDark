# DeckKitAfterDark

A companion suite to [DeckKit](https://github.com/fillmoreb/DeckKit) (controller-first addons for WoW:
Forever on the Steam Deck) for addons in the **ToS gray area**: programs on the Deck exchanging data
with the game while you play. They're kept out of DeckKit on purpose. Nothing here plays for you, but
it does let outside programs feed addons and read what addons show. Install at your own judgment.

Every addon here needs the DeckKit core (`## Dependencies: DeckKit`).

**Installing:** see [INSTALL.md](INSTALL.md).

| Addon | What it does |
|---|---|
| **DeckLink** | A two-way data link between a program on the Deck and addons: key taps in, colored cells on screen out. `/deckkit link` shows how it's going. |
| **DeckDictation** | Decktation's exact state (recording, transcribing, done, error) for DeckKit's dictation indicator, from Decktation's status hook (`deck-setup/decktation-hook.sh`, installed as `decklink-decktation-hook`). |
| **DeckTelemetry** | The Deck's battery, charging, power, temperatures, fan and CPU/GPU/memory load, for other addons to show (it draws nothing itself). `/deckkit telemetry` prints the latest. |
| **DeckAgent** | The game side of DeckAgent, a gaming assistant you ask things ("give me a waypoint to the nearest priest trainer"). It answers where you are and sets waypoints (through WayPad), nothing else. `/deckkit agent`. Early: see [DeckAgent](#deckagent). |

## DeckLink

**In:** the program taps **F9** (a 0) and **F10** (a 1), one bit per tap. DeckLink's override bindings
catch them: nothing is typed, nothing needs focus, nothing is saved to your keybinds. The game queues
every tap (AddonTools lab/RESULTS.md E6), but handling key input costs it ~0.17 ms per press or
release, so a burst freezes it (1 KB ≈ 2.7 s). Taps are paced 1 ms apart instead: no visible cost,
about 450 bits (55 bytes) a second. Never two keys down at once: the
game loses the first key's release then.

**Out:** while a program has the link open, a row of 4 px colored cells runs along the bottom edge
of the screen (it's gone otherwise; the cover below hides it from you). The program reads it from gamescope's video stream: whether the
game is ready for keys (no chat box open), which frame arrived last, and one frame from the game.

**Frames** both ways: `len (2) | channel (1) | seq (1) | payload | CRC-16 (2)`. Stop-and-wait: the
program waits until the strip shows its frame's seq (resending if not), and acks each frame the game
shows. A lost or extra tap fails the CRC: that frame is dropped and counted, never delivered wrong.
Channel 0 is the link's own (hello, ack, bye, ping); addons use 1-255:

```lua
DeckLink.On(5, function(payload, seq) ... end)   -- frames from the program on channel 5 (seq: the frame's)
DeckLink.Send(5, "text")                     -- false if the link is closed or it's too long
DeckLink.MaxPayload()                        -- about 545 bytes (one strip row); inbound up to 4096
```

The program's side is `deck-setup/decklink` (Python 3, standard library + the Deck's GStreamer):

```
decklink status                  the strip as it is now
decklink ping [--size N]         open the link and time a round trip
decklink send CHANNEL [TEXT]     send TEXT (or stdin) to an addon's channel
decklink recv [--count N]        print what the game sends
```

```python
from decklink.link import Link
with Link() as link:
    link.send(5, b"hello")
    print(link.recv(timeout=5))
```

Keys go in through Decktation's ydotool daemon when the program runs as root, else xdotool on
Xwayland `:1` as the desktop user. Reading the strip needs Game Mode (gamescope's PipeWire stream).

Installing it on the Deck (on the Deck itself, or over SSH from a Mac or Linux computer), the services it
runs, and Decktation's status hook: [INSTALL.md](INSTALL.md).

## DeckTelemetry

`python3 -m decklink.telemetry` (on the Deck; `--every 5`, `--once`, `--print`) sends one small text frame
on channel 1 every few seconds: fire and forget, no strip, no screen reading, so it costs next to
nothing. A lost update (chat was open) is just replaced by the next. In the game:

```lua
local t = DeckTelemetry.Get()          -- nil before the first update; t.age = seconds since it
-- t.bat %, t.st "charging"/"discharging"/"not charging"/"full", t.ac boolean, t.bw W (+ draining),
-- t.apu W, t.cpu %, t.gpu %, t.mem %, t.tapu / t.tbat / t.tssd C, t.fan rpm
DeckTelemetry.OnUpdate(function(t) ... end)   -- after each update; returns a handle for Cancel
DeckTelemetry.Age()                     -- seconds since the last update: tells fresh from stale
```

## DeckAgent

A gaming assistant, the way Claude Code is a coding assistant: you ask, it looks things up and acts in
the game through a short, fixed list of requests. **Early (slice 1): text questions from a computer,
NPC lookups and waypoints.** Voice, info popups, addon installs and memory are planned.

```
agent/deckagent "give me a waypoint to the nearest priest trainer"
# -> I set a waypoint to X'yera, a priest trainer in Orgrimmar about 202 yards away.
```

- **The brain** is `claude -p` (Claude Code, signed in with your own Claude plan, so it draws on your
  plan's limits, not an API bill), run from the agent's own folder (`~/.deckagent`) with
  `--strict-mcp-config` and only web search and fetch as built-in tools. A `codex exec` engine is planned.
- **The only way into the game** is `agent/mcp_server.py` (MCP, standard library only), with three
  tools:
  - `get_player_state`
  - `find_npc`: nearest first, from the agent's database only (it never asks the game)
  - `set_waypoint`

  It reaches the game with `decklink agent state [FIELD…] | way MAP X Y` (`deck-setup/decklink/agent.py`,
  DeckLink channel 3), answered by the DeckAgent addon. There is no way to run Lua, send slash
  commands, or touch combat, movement, targeting or casting.
- **Packets** are binary, with fields at fixed places, because keys into the game carry only about 55
  bytes a second:
  - asking for your location is 2 bytes (op byte, field mask), 64 key taps
  - a waypoint is 7 bytes (op byte, map, x, y)
  - the reply comes back on the strip: status, then only the fields asked for, or the WayPad
    waypoint id

  The full layout is at the top of `addons/DeckAgent/Agent.lua`.
- **The database** (`~/.deckagent/deckagent.db`, SQLite) is built by `python3 -m agent.update_data`. It
  draws on QuestieDB's Forever NPCs (spawn coordinates converted for 1.60) and wago.tools' Forever map
  tables. Those are downloaded to `~/.deckagent/data`, never committed: they aren't ours to
  redistribute.
- **Setup on a Mac** (the brain on the Mac, the game on the Deck): install Claude Code and sign in, then
  set `DECKAGENT_DECK=claude@<deck>` (an SSH target that can `sudo -u deck`; `DECK` in deploy.local works too) and run
  `python3 -m agent.update_data`. On the Deck itself, leave `DECKAGENT_DECK` unset.

## Keys (Ben's setup)

Every key the setup binds, across DeckKit, DeckKitAfterDark, Decktation and the dev tools:

| Key | Sent by | Does |
|---|---|---|
| F1 / F2 / F3 / F4 | the Steam layout, while grip L4 / R4 / L5 / R5 is held | DeckKit grip chords |
| F5 / F6 / F7 / F8 | the Steam layout, once L4 / R4 / L5 / R5 has been held 0.4 s | DeckKit grip chord holds |
| F9 / F10 | a DeckLink program (ydotool or xdotool) | DeckLink bits 0 / 1 (free without DeckLink) |
| F11, F12 | | spare |
| Ctrl+Shift+F1-F5, F11, F12 | `deckctl` (AddonTools, dev only, while DeckDev is installed) | receive, commit, cancel, export, load inbox slot, strip on, reload |

Reload UI has no key: it's on a grip slot. Decktation reads the controller directly and types its text
with ydotool; its status hook goes through DeckLink (F9/F10, channel 2), so it needs no keys of its own.

DeckLink channels: 1 telemetry (DeckTelemetry), 2 Decktation's state (DeckDictation), 3 DeckAgent's requests.

## The cover

`decklink.cover` hides the strip from you: a black bar over the bottom 4 px, drawn as a gamescope
overlay (the way MangoHud's is), shown only while the link is open. You see black; the program reading
the strip doesn't, because gamescope's video stream leaves overlays out (AddonTools lab E7). It takes
no input and never takes focus. Run it as the desktop user in Game Mode:
`DISPLAY=:0 PYTHONPATH=~/.local/lib python3 -m decklink.cover` (`--color ffff00` to see where it is,
`--always` to keep it up). Hosts mark the link open in `/tmp/decklink-open`; the cover checks that file,
so it costs next to nothing.

## Development

- `./deploy.sh DeckLink` deploys through AddonTools' `deckctl` (like DeckKit's), `deploy.local` as there.
- `test/run.sh` runs the tests: the Python host side against the real addon code, through
  `test/harness.lua` (a stub WoW API; needs `lua5.1`).
