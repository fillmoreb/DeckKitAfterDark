# Plan: Decky builds her own live widgets

**Status:** proposed 2026-11-17, not started.

**Goal:** stop building one-off tools. When Ben asks for "track my haul", "XP per hour" or "how fast am I gaining rep",
Decky composes it herself from general pieces, saves it so it runs without her, and remembers how she did it. We only
add a raw ingredient when she hits something she can't express.

**Why now:** Ben asked her to track his mining and skinning haul. The closest she had was a checklist, so she made up
targets ("20 Light Leather") and labels that went stale ("have 7"). The checklist can only say done or not done. Her
turns are one-shot (about 10 s), so anything live has to be counted in the game, not by her.

## What exists already

- **Layouts** (`DeckKit/Layout.lua`): windows as data, never code. `check` and `progress` already bind to live
  `DeckKit.Conditions` (quest taken, items carried, level, zone) and re-read twice a second.
- **Hosts** (`DeckKit/Displays.lua`): the same spec shows as a window, a card or the tracker (top left, while you
  play).
- **Decky's side:** `show_layout` and `track_checklist` send a layout spec (JSON, by paste, up to 4 KB). The rule of
  no Lua or slash passthrough stays.
- **Actions** (`DeckKit/Actions.lua`): one list of everything bindable (spells, items, macros, DeckKit offers), used
  by the grip slots and PadTouchGrid's cells.

## The pieces

### 1. Ledger (DeckKit core, `Ledger.lua`, `DeckKit.Ledger`)

A record of what happens, kept in the game:

| Event | Source |
|---|---|
| loot: item ID, count | `CHAT_MSG_LOOT`, the player's own lines (`LOOT_ITEM_SELF`, `_MULTIPLE`); the item link gives the ID |
| money | `CHAT_MSG_MONEY` (looted coin), plus `PLAYER_MONEY` deltas for the total |
| XP | `PLAYER_XP_UPDATE` deltas (across a level-up) |
| reputation | `CHAT_MSG_COMBAT_FACTION_CHANGE` |
| skill-ups | `CHAT_MSG_SKILL` |
| quests turned in | `QUEST_TURNED_IN` |
| whispers (both ways, Battle.net by BattleTag) | `CHAT_MSG_WHISPER`, `_INFORM`, `CHAT_MSG_BN_WHISPER`, `_INFORM` |
| chat lines | `CHAT_MSG_SAY`, `_YELL`, `_PARTY`, `_RAID`, `_GUILD`, `_CHANNEL`... |
| trades: partner, what each side gave | `TRADE_SHOW` and the trade window, recorded at `ERR_TRADE_COMPLETE` |
| party invites received | `PARTY_INVITE_REQUEST` |

- **Totals:** kept per minute rather than per event, so a long session stays small.
- **Marks:** named start times ("haul"), which measures count from. A mark survives a reload or logout (saved per
  character).
- **History:** loot, money and XP totals are kept per minute, saved for a few days ("how much did I make
  yesterday"), both per character and summed across the account.
- **People's events stay off the disk:** whispers, chat lines, names and trade partners are kept in memory for the
  session only, never in SavedVariables. Only loot, money and XP totals and marks are saved.
- **Safety:** every value is checked for secrets before it's stored.
- **Not read:** buffs. Reading auras taints the gamepad bar (it blocked InteractUnit on 2026-11-17).

### 1b. People (`DeckKit.People`): moved from AfterDark

The people tracker built on 2026-11-17 (`DeckAgent/People.lua`) collects whispers, chat, trades and invites
itself. Ben: that belongs in DeckKit proper. It becomes a view over the ledger:

- **`DeckKit.People.Recent(kinds, seconds)`:** everyone involved in recent events, one entry per person with
  their reasons and ages. Group and target are read live, as now.
- **AfterDark keeps only the encoding:** the PEOPLE and INVITE ops (same numbers, same bytes, so
  `decklink/agent.py` and the MCP tools don't change) call `DeckKit.People` instead of listening to events
  themselves.
- **The invite action moves too:** `DeckKit.People.Invite(name)`, with the leader and full-group checks, so
  DeckKit can offer it (a "whisper back" or "invite" from the touch grids or a chat peek) without Decky.
- **Tests:** the event scenarios move to DeckKit's offline harness. AfterDark keeps its byte tests.

### 2. Measures (`DeckKit.Measures`, beside Conditions)

Numbers a display can follow live, written as plain tables:

```lua
{ measure = "looted", item = 2318, since = "haul" }            -- 14
{ measure = "looted", item = 2318, since = "haul", per = "hour" } -- 36/h
{ measure = "value", of = { measure = "looted", item = 2318, since = "haul" } } -- 3s 78c at auction price
{ measure = "money", since = "haul" }   { measure = "xp", per = "hour" }   { measure = "rep", faction = 2601, per = "hour" }
{ measure = "elapsed", since = "haul" } { measure = "carried", item = 2318 }  { measure = "sum", of = { ... } }
```

- **Prices:** from a provider MarketDeck registers (`DeckKit.Prices`), vendor price otherwise. The core never
  depends on MarketDeck.
- **Unknown measures:** read as nil and show as "?", like an unknown layout node.

### 3. Live widget pieces (Layout.lua)

- **New node `stat`:** `{ type = "stat", label = "Light Leather", bind = <measure>, format = "count" | "rate" |
  "money" | "time" }`. In a `row`, several stats line up as columns: label, +14, 36/h, 3s 78c.
- **`progress`:** accepts a measure as well as a condition, for "100 Light Leather" or "Friendly with the Nightclaw Druids".
- **Hosts:** works in all three, so a tracker is the usual home for something live.

### 4. Saved widgets as actions

- **Saving:** a spec saved by name and host: "Haul", tracker, start mark "haul". Either for this character or
  for the whole account (Decky asks only if it's unclear; "for all my characters" means account).
- **Binding:** each saved widget is a DeckKit action (`WIDGET:<name>`). It shows in the picker (grip slots, touch
  grids) and toggles the widget, resetting its mark when it opens if it's saved that way.
- **Without Decky:** it all runs in the game.

### 5. Decky's side (AfterDark)

- **Requests:** a WIDGET op to show, save, list, delete and set a mark, and a LEDGER op for summaries ("what did I
  loot in the last hour"). Binary like the others; specs go in by paste.
- **MCP tools:**
  - `show_widget(spec, host, save_as?, mark?)`
  - `list_widgets()`
  - `delete_widget(name)`
  - `get_ledger(since, kinds)`

  `track_checklist` stays: a checklist is a widget made of checks.
- **SYSTEM.md:** "track how much / how fast" means a widget with measures, never a checklist with made-up targets.
  Ask what to track only when it isn't clear.

### 6. Her skills

- **Where:** Claude Code skills in her own folder (`~/.deckagent/.claude/skills/`), written with the file tools she
  already has there.
- **When:** only after Ben approves a result ("that's good, remember how"). She can list them and forget one.
- **Limits:** skills are how-to notes for the same fixed tools. They add no powers.
- **To check:** that `claude -p` with her allowlist loads skills from the working folder. If it doesn't, she reads
  them like her notes file.

## Build order

1. **Ledger and measures,** with offline harness scenarios: loot lines, a level-up across XP, marks surviving a
   reload, secrets skipped. Then `DeckKit.People` on the ledger, and AfterDark's People.lua cut down to its ops
   (its byte tests must still pass unchanged).
2. **The `stat` node and live `progress`,** with a scenario that shows the numbers updating.
3. **Saved widgets as actions:** binding one to a grip slot and toggling it, offline and then in the game.
4. **AfterDark ops and MCP tools,** with byte tests both ways like the other ops.
5. **SYSTEM.md and skills;** then ask Decky "track my haul" by voice. The first real test is that it needs no new code.

Steps 1–3 are DeckKit and useful without Decky. Steps 4–5 are AfterDark.

## Decided (Ben, 2026-11-17)

- **History:** a few days of it, not just the session.
- **Saved widgets:** both, account-wide and per character.
- **Several trackers:** they stack (Decky's checklist and saved widgets one under another at the top left), so
  `UI.ShowTracker` gets a list of trackers by name instead of one.
- **No kill counts:** a bragging number. What matters is what kills earn (loot, XP, reputation, money), and those
  are all tracked. Kill events exist only inside the closed combat log anyway.

## Still to check in the game

- Loot, money, XP, reputation and skill-up chat lines readable (not secret) in instances and in combat.
