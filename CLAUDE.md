# CLAUDE.md

DeckKitAfterDark: Ben's companion suite to DeckKit for ToS-gray-area addons (programs on the Deck
exchanging data with the game). Read `~/Projects/DeckKit/CLAUDE.md` for the game's quirks and rules
(Lua 5.1, Interface 16001, the DeckKit core, never rename SavedVariables or bindings), and
`~/Projects/AddonTools/CLAUDE.md` for driving the game (`deckctl`).

- Gray-area features go here, never into DeckKit. Addons here depend on the DeckKit core; DeckKit
  must never depend on anything here.
- Keys: README "Keys" is the whole map for Ben's setup. Change it there when a binding moves.
- DeckLink: the key side is `addons/DeckLink/Link.lua` + `deck-setup/decklink/keys.py`; the strip is
  `Strip.lua` + `strip.py`; frames are `Codec.lua` + `codec.py`. Each pair must stay in step; the
  tests run the real Lua against the Python. Run `test/run.sh` after changing either side.
- Key facts measured on the Deck (AddonTools lab/RESULTS.md E6): every tap arrives, in order, however
  fast; two keys held at once lose a release; a focused text box swallows the keys (deckctl run leaves
  its export box focused until Escape).
- DeckAgent (the gaming assistant): `agent/` (launcher `deckagent`, MCP server, SQLite game database,
  `SYSTEM.md`), `deck-setup/decklink/agent.py` (host side), `deck-setup/decklink/agentd.py` (the link
  keeper service: one link held open across requests, closed when idle) and `addons/DeckAgent` (game
  side, channel 3).
  The game is reachable only through the MCP server's named tools, and the addon only accepts named ops
  with data fields, never code. Packets are binary with fixed field positions: the framing and the first
  ops in `Agent.lua`'s header, each other op in the header of the file that answers it (README's
  DeckAgent section lists which); numbers packed and read with `Bytes.lua`. The addon files and
  `agent.py` must stay in step, and `test/test_agent.py` checks exact bytes. `agent.py` alone builds and
  checks requests (`prepare`); the link keeper only runs them.
  New ops take new numbers, new state fields the next mask bit; nothing existing moves. Keep it that way: no Lua or slash passthrough. Downloaded reference data
  (QuestieDB, wago.tools) lives in `~/.deckagent`, never in the repo. Brainstorm and plans: Ben's DeckKit
  memory `wow-agent-brainstorm`.
- Every DeckLink channel's payload is binary with fixed field positions (README table): keys into the
  game carry ~55 bytes a second. Keep it that way for new channels: no text formats. Telemetry waits
  while the link is open or a dictation holds the keys (`link.hold`), so it never delays anything.
- DeckLink sends frames of 40+ bytes by paste (`link.py` send_by_paste, `Link.lua` ExpectPaste/ReadPaste,
  `clipboard.py`): keep the line format `DLP1 seq channel length crc base64` in step on both sides.
  Anything that shows a window answering to the pad must read only `OnGamePadButtonDown` (no polling,
  no keyboard): Decktation's dictation presses Enter, and only a real press may /say (DeckAgent's
  Prompt.lua).
