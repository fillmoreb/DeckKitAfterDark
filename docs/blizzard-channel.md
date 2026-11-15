# Proposal: a local addon channel

A request to Blizzard for a sanctioned, local, opt-in way for addons to exchange data with a program
running on the same machine, and the safeguards that would keep it from becoming a botting tool.

Written by the DeckKit project (controller-first addons for WoW: Forever on the Steam Deck). This
repository, DeckKitAfterDark, is where we keep the workarounds this channel would replace.

## Summary

Give addons one small API, a message channel to a local process:

```lua
C_LocalLink.IsEnabled()                 -- the player turned it on, and this addon is allowed
C_LocalLink.Send(payload)               -- a string, at most 4 KB; false if over the limit or rate
-- events
LOCAL_LINK_MESSAGE(payload)             -- a message from the local program
LOCAL_LINK_STATE(connected)             -- a program connected or went away
```

It should be off by default. Turning it on should take a deliberate step from the player, separately for
each addon. It should stay on the local machine, and it should grant **no capability an addon doesn't
already have**: messages land in ordinary insecure addon code, under every existing restriction on
protected actions.

## Why

### Players are already doing this, the hard way

Addons that want to talk to the outside world currently have three options, all of them bad:

| Technique | Used by | Cost |
|---|---|---|
| **Pixel strips:** an addon paints data as coloured squares; a program screen-captures and decodes them | wow-ai, WoW Context Bridge (Decktation), LibSerpix, WoWCerealize, our DeckLink | Continuous screen capture (on a Steam Deck, about 9% of a CPU core just to read one strip), capacity of a few hundred bytes per frame, breaks with UI scale, overlays or fullscreen modes, and Gamescope capture has crashed in testing |
| **Simulated input:** a program types keys or pastes text into a hidden edit box | our DeckLink (F9/F10 as bits, clipboard paste for bulk) | About 55 bytes a second by keys, input handling cost in the client, it takes over the clipboard, and it looks exactly like automation from the outside |
| **Files at load time:** SavedVariables or load-on-demand addons that a program rewrites | Data-site clients, Overwolf apps, wow-ai's pool of slot addons | Only moves on `/reload`, logout or login (or through thousands of pre-generated addon folders); not usable for anything live |

Every one of these projects exists for a legitimate reason, and every one of them is fragile, wasteful,
or hard to tell apart from cheating. A real channel replaces all of them with something smaller,
cheaper, and easier for Blizzard to police.

### What it's for

- **Controller and handheld play.** On a Steam Deck the program outside the game knows things the
  addon can't: battery level, thermals, which voice binding is held, whether dictation is listening. We
  show these in the game's own UI.
- **Voice and dictation.** Speech recognition gets names right when it knows the zone, the target and
  nearby NPCs. Decktation's community is building exactly this today, with a pixel strip.
- **Accessibility.** Screen readers and text-to-speech for quest text. Switch and eye-gaze input
  software that wants to show its state in game. Players with motor impairments using voice to ask
  "where is the flight master" and getting a waypoint.
- **Assistants.** A player asks a question by voice; a local program answers and puts the answer
  (a waypoint, a short card) in the game's UI. It never plays for them.

## Safeguards

The design rule throughout: **the channel moves data, never intent.** Anything an addon could not do
by itself before, it still cannot do with the channel.

1. **No new in-game powers.** Inbound messages arrive as an event in insecure Lua. They cannot cast,
   move, target or use items, in or out of combat, because insecure code cannot do those things. A bot
   still needs simulated hardware input to act, exactly as it does today, and gains nothing for acting.
2. **No new data out.** `Send` takes what the addon already has through the existing API. Values the
   client protects (retail's secret values) are refused by `Send`, the same way they are refused
   everywhere else, so the channel can't be used to launder them out.
3. **Opt-in, twice.**
   - A client setting, off by default, with a plain explanation, like enabling Lua errors or
     third-party addons.
   - Each addon declares `## LocalLink: 1` in its TOC, and the AddOns list shows which addons use it,
     with a separate tick box for each.
4. **Local only.** The endpoint is a Unix socket or named pipe owned by the logged-in OS user (or
   loopback with a per-session token written to the WTF folder), one per enabled addon and named after
   it, so a program talks to exactly the addon it was written for. Nothing listens on the network.
   Nothing goes to Blizzard's servers or to other players.
5. **Small and slow on purpose.** Messages of at most 4 KB, a modest rate limit (say 20 per second each
   way), and an inbound queue that drops rather than grows. That's plenty for status, questions and
   answers, and too little for anything resembling a frame-by-frame control loop.
6. **Rules Blizzard can tighten.** A server-side flag can disable the channel entirely, per ruleset, or
   in specific content (rated PvP, tournament realms, timed dungeons) if abuse ever shows up. We'd
   rather accept that than keep using workarounds that can't be switched off.
7. **Visible.** A small indicator while a program is connected, and connection events in the client
   log, so a player always knows when something outside the game is talking to their UI.

### What this does not protect against, honestly

A determined bot author could use the channel to read game state more cheaply than with a pixel strip.
But pixel strips already give them that, and the outbound side carries only what the addon API already
exposes. The actions that matter (casting, moving) remain behind hardware events, where they are
today. Meanwhile, legitimate tools stop simulating keystrokes, which removes the noise that makes real
automation harder to spot.

## A smaller first step

If a full two-way channel is too much at once, any one of these covers a large share of the need:

1. **Outbound only** (`Send` plus a local reader). This alone retires every pixel strip.
2. **A flush for SavedVariables**, such as `C_SavedVariables.Flush(addon)`, rate-limited to once every
   few seconds. It covers outbound for slower needs, though it is disk-heavy and still not two-way.
3. **A read-only inbox:** a folder per addon that its code can read at runtime (not just at load). It
   covers inbound for answers and settings without any socket.

## What we'd do on day one

DeckKit already keeps every simulated-input and screen-reading detail behind one boundary on each side:

- in the game, `DeckLink.On` / `DeckLink.Send` (`addons/DeckLink/Link.lua`)
- on the Deck, `decklink/link.py`

Adopting the channel would mean one new transport file on each side, plus a setting. The key taps,
the clipboard paste, the pixel strip and the screen capture would be deleted, and every feature built
on them would keep working unchanged.

## Contact

Questions and feedback through the DeckKit project on CurseForge or GitHub. We're glad to test an
early version, share measurements, or shape the design to whatever constraints Blizzard needs.
