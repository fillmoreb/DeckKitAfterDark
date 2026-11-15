# DeckAgent: tentative improvements

Ideas taken from other AI bridge addons for WoW (surveyed 2026-11-15), filtered for a controller-first,
voice-first assistant on the Steam Deck. Nothing here is decided. Strongest fit first.

Sources: [wow-ai](https://github.com/chelinho139/wow-ai),
[AzerothLM](https://github.com/chefm4tt/AzerothLM), [wow-agent](https://github.com/Oighty/wow-agent),
[Azeroth Companion](https://github.com/nardo86/azeroth-companion),
[Gnomish Relay](https://www.curseforge.com/wow/addons/gnomish-relay),
[WoW Context Bridge](https://github.com/nukeador/wow-context-bridge) (via
[Decktation #36](https://github.com/silverfoxy/decktation/issues/36)).

## Strong fits

### 1. Game names as hints for Whisper (from WoW Context Bridge)

Before each dictation, feed faster-whisper's `initial_prompt` and hotwords the zone, subzone, target,
nearby NPC names, party and friends' names. That fixes the "Edna" class of garbled names, for chat
dictation as well as agent questions.

- **Already in place:**
  - DeckAgent's state (`get_player_state`) has most of the names.
  - AfterDark already owns Decktation's status hook.
  - Context Bridge builds a whole second pixel strip for this; we wouldn't need one.
- **Open:**
  - whether Decktation exposes a way to pass a prompt or hotwords per dictation
  - how fresh the names need to be (fetch at L4+R4 press, or keep a cache updated every few seconds)

### 2. "What's this?": the thing you're looking at as context (from wow-ai's tooltip links)

wow-ai attaches the tooltip of a shift-clicked item, spell or quest to the question. With a controller,
the natural version is whatever's under the cursor or reticle: DeckHUD's LookAt card already copies the
game tooltip. So the question goes with it, and "is this an upgrade?", "what drops this?" or "what
does this do?" need no names.

- Also: the gamepad cursor's item in bags, or the focused loot or quest reward.
- Needs: a "looking at" field in the state reply (item link or unit, plus tooltip lines).

### 3. Macros written by the agent (from wow-ai)

"Make me a macro that casts Renew on my focus" gives a preview in the game:
- **A** saves it and puts it on the cursor or crossbar.
- If one already exists, it asks before replacing it.
- Macros that run code (`/run`, `/script`, `/click`) are marked as such.

Nothing is saved in combat, the agent never runs a macro itself, and there's an undo.

- Limits are checked before the preview: names up to 16 characters, bodies up to 255 bytes.
- HealPad's macro handling (character macros only, names checked against what the macro casts) is a
  starting point for "is this one ours".

### 4. Routes: several stops the compass works through (from wow-ai's map layers and navigator)

wow-ai's agent draws numbered routes and pins, and an arrow advances within 12 yards, with an optional
loop for farming. Our waypoints already remove themselves on arrival, so most of a route is there:
"route me through my Duskwood objectives" becomes an ordered set the compass works through.

- **Needs:**
  - a `set_route` tool (ordered stops, with labels)
  - routes shown in the WayPad list
  - optionally pins on MapDeck
- Farming loops are tied to the shelved gathering route; leave them out unless it comes back.

### 5. The agent can see the screen (from wow-agent's screenshot attach)

We already capture the screen for the strip. Handing one full frame to Claude makes "what's that on
my screen?", "read me this quest text" or "what does this error say?" possible.

- Cost: one image per question that asks for it. Only on request, via a `look` tool, never by default.
- Check: Gamescope capture of the whole frame, not just the strip rows (the Grabber crops to the strip).

## Good, smaller

### 6. A log of past answers (from AzerothLM's journal)

Spoken and shown answers vanish. Keep the last N questions and answers, grouped by topic, in a
controller list (DeckKit UI screen) to reread directions or a boss guide. Saved per character.

### 7. Briefings when you arrive (from wow-agent, Azeroth Companion)

Entering a new zone gives a short summary. Entering a dungeon gives the tips that matter for your role
(tank, healer or damage). Offer these as ready-made hooks ("brief me when I enter a dungeon"), since
hooks already track zones. Off by default.

### 8. More of the character as context (from AzerothLM)

Check what's still missing from `get_character` and the state reply: talents, reputations,
professions with skill levels. Send only what the question needs, which the tools already do. That
makes "what should I upgrade" and "what talent next" answerable.

### 9. Progress while it works (from wow-ai, Gnomish Relay)

Show the agent's steps in the dictation box instead of a plain "thinking":
"looking up quests…", "setting waypoint…". The MCP server knows each tool call as it happens, and a
short label per tool is enough.

### 10. Which quests to drop (from Azeroth Companion)

"Which of my quests should I drop?": gray, far away, a dead-end chain, or a reward you can't use. Mostly
a prompt plus `get_quests` and `find_quests`. Needs no new mechanism.

## How our display options compare

What the agent can put on screen, here and elsewhere:

| | DeckAgent | wow-ai | Others (AzerothLM, Azeroth Companion, Gnomish Relay) |
|---|---|---|---|
| Spoken answer | `say`, 160 characters | no (whisper sound plus a summary echoed to chat) | no |
| Shaped display | none, text, list, checklist, reading (required enum) | full reply in a window; summary in chat | a text window or journal |
| Live state | `track_checklist`: ticks itself as you play | — | — |
| Questions back | `ask_player`: pad choices | — | — |
| Map | waypoint plus compass | routes, pins on the zone and continent maps, navigator arrow | — |
| Actions | flags on bag items (PackPad stars), waypoints, chat | macro button, permission button | — |
| Items | plain names | tooltips attached on the way in | names coloured by rarity (AzerothLM) |
| Memory | notes file | full transcript per chat | journal by topic (AzerothLM) |

Gaps worth closing, in order:
1. map pins and routes (4)
2. action buttons, starting with macros (3)
3. item names coloured by rarity, with the item's tooltip on focus, in list and reading displays
4. an answers log (6)
