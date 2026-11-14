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
