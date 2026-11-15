"""DeckAgent tests: the game side (addons/DeckAgent, through test/harness.lua), the host side
(deck-setup/decklink/agent.py) and the agent itself (agent/: Lua data reader, game database, MCP server).

    python3 -m unittest discover -s test
"""
import json, os, sqlite3, struct, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "deck-setup"))
sys.path.insert(0, os.path.join(HERE, ".."))
from decklink import agent as host, codec, strip  # noqa: E402
from agent import common, gamedb, luadata, mcp_server  # noqa: E402
from test_decklink import Game, bitstr, hello  # noqa: E402


def ask(packet, setup="", seq=7):
    """Sends one request packet to DeckAgent in the harness; returns (reply bytes or None, printed lines)."""
    frame = codec.build(host.CHANNEL, seq, packet)
    lines, s = Game.run("login() " + setup + " " + hello() + ' taps("%s") frame()' % bitstr(frame))
    if s is None or not s.out:
        return None, lines
    channel, _, data = codec.parse(s.out)
    return (data if channel == host.CHANNEL else None), lines


class GameSide(unittest.TestCase):
    def test_state_request_is_two_bytes(self):
        self.assertEqual(host.build_state(["pos", "world"]), b"\x41\x03")
        self.assertEqual(host.build_state(), b"\x41\xff")

    def test_state_all_fields(self):
        data, _ = ask(host.build_state())
        self.assertEqual(data[:4], b"\x41\x07\x00\xff")  # op, seq, ok, every field given
        self.assertEqual(host.parse_reply(data), {
            "op": 1, "seq": 7, "ok": True, "status": "ok", "fields": host.FIELDS, "map": 1458, "x": 49.0, "y": 18.3, "cont": 0,
            "wx": 1636, "wy": 240, "zone": "Undercity", "faction": "H", "level": 20, "class": "PRIEST",
            "in_combat": False, "model": None, "quiet": False, "target": None})

    def test_state_brain_is_the_menu_model(self):
        for setup, model in (("", None), ('DeckAgentDB = { model = "sonnet" }', "sonnet"),
                             ('DeckAgentDB = { model = "opus" }', "opus")):
            data, _ = ask(host.build_state(["brain"]), setup)
            self.assertEqual(host.parse_reply(data, ["brain"])["model"], model, setup)

    def test_state_only_what_was_asked(self):
        data, _ = ask(host.build_state(["pos", "world"]))
        self.assertEqual(len(data), 4 + 6 + 5)  # header + mask, pos, world: 15 bytes
        self.assertEqual(data[4:], struct.pack(">HHHBhh", 1458, round(0.49 * 65535), round(0.183 * 65535), 0, 1636, 240))
        reply = host.parse_reply(data, ["pos", "world"])
        self.assertNotIn("zone", reply)
        self.assertEqual((reply["map"], reply["wx"]), (1458, 1636))

    def test_missing_position_is_zeroed_and_unflagged(self):
        data, _ = ask(host.build_state(["pos", "world", "zone"]), "PLAYER.x = nil")
        self.assertEqual(data[3], 0b100)  # only zone could be given
        reply = host.parse_reply(data, ["pos", "world", "zone"])
        self.assertEqual(reply["fields"], ["zone"])
        self.assertEqual(reply["zone"], "Undercity")
        self.assertNotIn("map", reply)
        self.assertNotIn("wx", reply)

    def test_way_is_seven_bytes_and_returns_an_id(self):
        packet = host.build_way(1454, 36, 87.7)
        self.assertEqual(packet, struct.pack(">BHHH", 0x42, 1454, round(0.36 * 65535), round(0.877 * 65535)))
        setup = 'WayPad_Add = function(m, x, y, l) print("WAYPAD", m, ("%.4f"):format(x), ("%.4f"):format(y), tostring(l)) return { id = 513 } end'
        data, lines = ask(packet, setup)
        self.assertEqual(host.parse_reply(data), {"op": 2, "seq": 7, "ok": True, "status": "ok",
                                                  "via": "waypad", "waypoint": 513})
        self.assertIn("WAYPAD\t1454\t0.3600\t0.8770\tnil", lines)

    def test_waypad_refusal_comes_back(self):
        data, _ = ask(host.build_way(9999, 50, 50), "WayPad_Add = function() return nil, 'unknown map 9999' end")
        self.assertEqual(host.parse_reply(data), {"op": 2, "seq": 7, "ok": False, "status": "refused",
                                                  "reason": "unknown map 9999"})

    def test_way_without_waypad_uses_the_map_pin(self):
        data, lines = ask(host.build_way(1454, 50, 25))
        self.assertEqual(host.parse_reply(data)["via"], "mappin")
        self.assertTrue(any(l.startswith("MAPPIN\t1454\t0.5") for l in lines))
        self.assertIn("TRACKED\ttrue", lines)

    def test_bad_requests_are_refused(self):
        for packet, status in ((b"\x7f", "unknown op"), (b"\x81\x01", "unknown version"), (b"", "unknown version"),
                               (b"\x42\x05\x8e", "bad arguments"), (b"\x41", "bad arguments")):
            data, _ = ask(packet)
            reply = host.parse_reply(data)
            self.assertEqual((reply["seq"], reply["ok"], reply["status"]), (7, False, status), packet)
            self.assertTrue(reply["reason"])


class HostSide(unittest.TestCase):
    def test_way_coordinates(self):
        self.assertEqual(host.fraction(0), 0)
        self.assertEqual(host.fraction(100), 65535)
        with self.assertRaises(ValueError):
            host.build_way(1, 101, 5)

    def test_truncated_replies_are_malformed(self):
        for p in (b"\x41\x01", b"\x41\x01\x00\x01\x05", b"\x42\x01\x00\x01", b"\x41\x01\x03\x09ab"):
            with self.assertRaises(host.Malformed, msg=p):
                host.parse_reply(p, ["pos"])

    def test_request_waits_for_its_own_reply(self):
        class FakeLink:
            def __init__(self):
                self.sent, self.queue = [], []

            def send(self, ch, payload):
                self.sent.append((ch, payload))
                self.queue = [(1, b"v=1;bat=50"), (3, b"\x41\x09\x00"), (3, b"\x42\x21\x00"), (3, b"\x41\x21\x00\x00")]
                return 0x21

            def recv(self, timeout):
                return self.queue.pop(0) if self.queue else None
        lk = FakeLink()
        self.assertEqual(host.request(lk, host.build_state([])), b"\x41\x21\x00\x00")
        self.assertEqual(lk.sent, [(3, b"\x41\x00")])


NPC_LUA = """QuestieDB.npcData = [[return {
[1] = {'Aelthalyste',100,100,30,30,0,{[1497]={{49.3,17.1}}},nil,1497,nil,nil,68,"H","Priest Trainer",19},
[2] = {'Far Trainer',100,100,30,30,0,{[1497]={{90,90}}},nil,1497,nil,nil,68,"H","Priest Trainer",19},
[3] = {'Alliance Priest',100,100,30,30,0,{[1497]={{49,18}}},nil,1497,nil,nil,12,"A","Priest Trainer",19},
[4] = {'Dungeon Priest',100,100,30,30,0,{[1497]={{-1,-1}},[717]={{50,50}}},nil,717,nil,nil,68,"AH","Priest Trainer",19},
[5] = {'Kalimdor Priest',100,100,30,30,0,{[1637]={{36,87.7}}},nil,1637,nil,nil,68,"H","Priest Trainer",19},
}]]"""
ZONES_LUA = """ZoneDB.private.areaIdToUiMapIdOverride = [[return { [717] = 225 }]]
ZoneDB.private.areaIdToUiMapId = [[return { [1497] = 1458, [1637] = 1454, [717] = 1 }]]"""
UIMAP_CSV = "Name_lang,ID,ParentUiMapID,Flags\nUndercity,1458,1415,0\nOrgrimmar,1454,1414,0\nStockades,225,0,0\n"
UMA_CSV = ("UiMin_0,UiMin_1,UiMax_0,UiMax_1,Region_0,Region_1,Region_2,Region_3,Region_4,Region_5,ID,UiMapID,"
           "OrderIndex,MapID,AreaID\n"
           "0,0,1,1,1300,0,-1e6,1900,600,1e6,1,1458,0,0,1497\n"
           "0,0,1,1,1000,-5000,-1e6,2000,-4000,1e6,2,1454,0,1,1637\n"
           "0.1,0.1,0.5,0.5,0,0,0,1,1,1,3,1454,0,1,1637\n")


class LuaData(unittest.TestCase):
    def test_literals(self):
        self.assertEqual(luadata.loads('return {1, "two", nil, true}'), [1, "two", None, True])
        self.assertEqual(luadata.loads("{[12]={{49.81,-3.5}}, name = 'a\\'b'}"), {12: [[49.81, -3.5]], "name": "a'b"})
        self.assertEqual(luadata.loads("{ 'x', [3] = 'z' }"), {1: "x", 3: "z"})

    def test_code_is_refused(self):
        for text in ("return os.exit()", "{ f() }", "{ x = y }"):
            with self.assertRaises(luadata.LuaDataError, msg=text):
                luadata.loads(text)

    def test_blocks(self):
        self.assertEqual(set(luadata.blocks(ZONES_LUA)),
                         {"ZoneDB.private.areaIdToUiMapIdOverride", "ZoneDB.private.areaIdToUiMapId"})


class GameDB(unittest.TestCase):
    def setUp(self):
        self.db = gamedb.connect(":memory:")
        gamedb.load_maps(self.db, UIMAP_CSV, UMA_CSV)
        gamedb.load_npcs(self.db, NPC_LUA, ZONES_LUA)
        self.addCleanup(self.db.close)

    def test_world_position(self):
        rect = gamedb.map_rects(self.db)[1458]
        self.assertEqual(gamedb.to_world(rect, 0.5, 0.25), (0, 1750, 300))
        self.assertNotIn(225, gamedb.map_rects(self.db))  # no world rectangle known

    def test_instance_spawns_skipped_and_override_wins(self):
        maps = [r[0] for r in self.db.execute("SELECT map FROM spawn WHERE npc = 4")]
        self.assertEqual(maps, [225])

    def test_nearest_first_friendly_only(self):
        here = {"map": 1458, "x": 0.49, "y": 0.18}
        found = gamedb.find_npc(self.db, "priest trainer", here, "H")
        self.assertEqual([f["name"] for f in found], ["Aelthalyste", "Far Trainer", "Dungeon Priest", "Kalimdor Priest"])
        self.assertLess(found[0]["distance_yards"], found[1]["distance_yards"])
        self.assertIsNone(found[2]["distance_yards"])  # no world position
        self.assertIsNone(found[3]["distance_yards"])  # other continent
        self.assertEqual((found[0]["map_id"], found[0]["x"], found[0]["zone"]), (1458, 49.3, "Undercity"))

    def test_game_world_position_wins_and_any_faction(self):
        org = {"map": 1458, "x": 0.5, "y": 0.5, "cont": 1, "wx": 1100, "wy": -4350}  # really in Orgrimmar
        found = gamedb.find_npc(self.db, "priest", org, None, limit=1)
        self.assertEqual(found[0]["name"], "Kalimdor Priest")
        self.assertIn("Alliance Priest", [f["name"] for f in gamedb.find_npc(self.db, "priest", None, None, 10)])

    def test_no_words_no_results(self):
        self.assertEqual(gamedb.find_npc(self.db, "  ", None), [])


class MCPServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = os.path.join(self.tmp.name, "db.sqlite")
        db = gamedb.connect(path)
        gamedb.load_maps(db, UIMAP_CSV, UMA_CSV)
        gamedb.load_npcs(db, NPC_LUA, ZONES_LUA)
        db.commit()
        db.close()
        p = mock.patch.object(common, "DB_PATH", path)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []

    def fake_game(self, *args, **kw):
        """Answers like the real game would, through the real reply parser (no position for combat)."""
        self.calls.append(args)
        if args[0] == "state":
            asked = list(args[1:]) or host.FIELDS
            given = [f for f in asked if f != "combat"]
            body = bytes([sum(1 << host.FIELDS.index(f) for f in given)])
            for f in asked:
                body += {"pos": struct.pack(">HHH", 1458, 32112, 11796), "world": struct.pack(">Bhh", 0, 1636, 240),
                         "zone": b"\x09Undercity", "who": b"\x02\x14\x05", "combat": b"\x00"}[f]
            return host.parse_reply(b"\x41\x01\x00" + body, asked)
        if args[0] == "prompt":
            options = list(args[args.index("--seconds") + 3:])
            return {"ok": True, "shown": "shown", "choice": 2, "answer": options[1]}
        if args[0] == "chat":
            return host.parse_reply(b"\x43\x01\x00\x01")
        if args[0] == "friends":
            return host.parse_reply(b"\x44\x01\x00\x00\x01\x02\x05Grunt\x14\x07Warrior\x07Durotar")
        return host.parse_reply(b"\x42\x01\x00\x01\x00\x03")

    def call(self, name, args):
        with mock.patch.object(common, "game", self.fake_game):
            r = mcp_server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                   "params": {"name": name, "arguments": args}})
        content = r["result"]["content"][0]["text"]
        return (content if r["result"]["isError"] else json.loads(content)), r["result"]["isError"]

    def test_handshake_and_tools(self):
        r = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["get_player_state", "find_npc", "set_waypoint", "get_waypoints",
                                 "remove_waypoints", "send_chat", "get_online",
                                 "ask_player", "get_quests", "find_quests", "get_character", "get_bags", "find_items", "flag_items", "clear_flags", "get_prices", "get_item_info", "set_hook", "list_hooks", "remove_hook", "track_checklist", "show_layout", "clear_checklist", "respond", "remember", "remember_fact",
                                 "recall", "forget_fact"])
        self.assertEqual(mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"], -32601)

    def test_tools_advertised_handled_and_allowed_are_the_same(self):
        # Every tool the server lists has a handler and is allowed by the launcher, and nothing else:
        # a tool the launcher doesn't allow is one the model sees and can't call.
        import importlib.machinery, importlib.util
        path = os.path.join(HERE, "..", "agent", "deckagent")
        loader = importlib.machinery.SourceFileLoader("deckagent_tools", path)
        la = importlib.util.module_from_spec(importlib.util.spec_from_loader("deckagent_tools", loader))
        loader.exec_module(la)
        listed = {t["name"] for t in mcp_server.TOOLS}
        self.assertEqual(listed, set(mcp_server.HANDLERS))
        self.assertEqual(listed, {t.split("__")[-1] for t in la.TOOLS})

    def test_player_state_asks_only_for_its_fields(self):
        out, err = self.call("get_player_state", {"fields": ["position", "who", "combat"]})
        self.assertFalse(err)
        self.assertEqual(self.calls, [("state", "pos", "world", "who", "combat")])
        self.assertEqual((out["map_id"], out["x"], out["faction"]), (1458, 49.0, "H"))
        self.assertNotIn("in_combat", out)
        self.assertEqual(out["unavailable"], ["combat"])  # the fake game didn't give it

    def test_find_npc_never_asks_the_game(self):
        out, err = self.call("find_npc", {"query": "priest trainer", "limit": 2, "faction": "H",
                                          "near": {"map_id": 1458, "x": 49, "y": 18}})
        self.assertFalse(err)
        self.assertEqual([r["name"] for r in out["results"]], ["Aelthalyste", "Far Trainer"])
        self.assertEqual(self.calls, [])  # database only

    def test_find_npc_without_a_position(self):
        out, err = self.call("find_npc", {"query": "priest", "limit": 10})
        self.assertIn("Alliance Priest", [r["name"] for r in out["results"]])  # no faction: everyone
        self.assertIn("aren't sorted", out["note"])
        self.assertEqual(self.calls, [])

    def test_set_waypoint(self):
        out, err = self.call("set_waypoint", {"map_id": 1454, "x": 36, "y": 87.7})
        self.assertEqual(out, {"done": True, "waypoint_id": 3, "via": "waypad"})
        self.assertEqual(self.calls[-1], ("way", 1454, "36.00", "87.70"))

    def test_send_chat(self):
        out, err = self.call("send_chat", {"channel": "whisper", "to": "Ednalyn", "text": "SFK?"})
        self.assertEqual((err, out["result"]), (False, "sent"))
        self.assertEqual(self.calls[-1], ("chat", "whisper", "Ednalyn", "SFK?"))
        self.call("send_chat", {"channel": "say", "to": "ignored", "text": "hello"})
        self.assertEqual(self.calls[-1], ("chat", "say", "", "hello"))
        out, err = self.call("send_chat", {"channel": "whisper", "text": "who?"})
        self.assertTrue(err)
        out, err = self.call("send_chat", {"channel": "say", "text": "x" * 256})
        self.assertTrue(err)

    def test_ask_player(self):
        out, err = self.call("ask_player", {"question": "Which?", "options": ["Ednalyn", "Ednabelle"], "list": True})
        self.assertEqual((err, out), (False, {"answer": "Ednabelle"}))
        self.assertEqual(self.calls[-1], ("prompt", "--list", "--seconds", "45", "Which?", "Ednalyn", "Ednabelle"))

    def test_remember_adds_lines_under_their_section(self):
        notes = os.path.join(self.tmp.name, "CLAUDE.md")
        with open(notes, "w") as f:
            f.write("# Notes\n\n## Ben's rules\n\n## People\n- Bob: Bob#1\n\n")
        with mock.patch.object(common, "HOME", self.tmp.name):
            self.call("remember", {"section": "people", "note": "Edna: AMI#1362; Heatherjr  Locklear"})
            self.call("remember", {"section": "rules", "note": "never yell"})
        with open(notes) as f:
            text = f.read()
        self.assertEqual(text, "# Notes\n\n## Ben's rules\n- never yell\n\n## People\n- Bob: Bob#1\n"
                               "- Edna: AMI#1362; Heatherjr Locklear\n\n")

    def test_get_online(self):
        out, err = self.call("get_online", {"lists": ["friends"]})
        self.assertEqual(out["people"], [{"list": "friends", "name": "Grunt", "level": 20, "class": "Warrior",
                                          "zone": "Durotar"}])
        self.assertEqual(self.calls[-1], ("friends", "friends"))

    def test_game_errors_are_tool_errors(self):
        def broken(*a, **k):
            raise common.GameError("the game didn't answer")
        with mock.patch.object(common, "game", broken):
            r = mcp_server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                   "params": {"name": "set_waypoint", "arguments": {"map_id": 1, "x": 1, "y": 1}}})
        self.assertTrue(r["result"]["isError"])
        unknown = mcp_server.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "run_lua"}})
        self.assertIn("error", unknown)


if __name__ == "__main__":
    unittest.main()


class LinkKeeper(unittest.TestCase):
    """deck-setup/decklink/agentd.py: one link kept open across requests, closed when idle."""

    def setUp(self):
        from decklink import agentd
        self.agentd = agentd
        self.now = [100.0]
        self.opened = self.closed = 0
        test = self

        class FakeLink:
            def open(self):
                test.opened += 1
                return self

            def close(self):
                test.closed += 1
        self.FakeLink = FakeLink
        self.keeper = agentd.Keeper(25, make_link=FakeLink, clock=lambda: self.now[0])
        self.sent = []

        def request(lk, packet, timeout=None):
            """The game's reply to a STATE request: each asked field, zeroed."""
            self.sent.append(packet)
            sizes = {"pos": 6, "world": 5, "zone": 1, "who": 3, "combat": 1, "brain": 1, "voice": 1, "target": 7}
            mask = packet[1]
            return bytes([packet[0], 1, 0, mask]) + b"".join(
                bytes(sizes[f]) for b, f in enumerate(host.FIELDS) if mask >> b & 1)
        p = mock.patch.object(host, "request", request)
        p.start()
        self.addCleanup(p.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(agentd.link, "OPEN_FILE", os.path.join(tmp.name, "open"))
        p.start()
        self.addCleanup(p.stop)

    def test_one_link_for_many_requests(self):
        for _ in range(3):
            self.assertTrue(self.keeper.handle({"cmd": "state", "fields": ["pos"]})["ok"])
        self.assertEqual((self.opened, self.closed), (1, 0))

    def test_idle_link_closes_and_reopens(self):
        self.keeper.handle({"cmd": "warm"})
        self.now[0] += 24
        self.keeper.tick()
        self.assertEqual(self.closed, 0)
        self.assertGreater(self.agentd.link.open_since(), 0)  # held open: marked, so telemetry waits
        self.now[0] += 2
        self.keeper.tick()
        self.assertEqual(self.closed, 1)
        self.assertFalse(self.keeper.handle({"cmd": "status"})["open"])
        self.keeper.handle({"cmd": "state"})
        self.assertEqual(self.opened, 2)

    def test_a_failed_request_starts_clean(self):
        def broken(lk, packet, timeout=None):
            raise self.agentd.link.LinkError("frame not received")
        with mock.patch.object(host, "request", broken):
            reply = self.keeper.handle({"cmd": "state"})
        self.assertEqual(reply, {"ok": False, "error": "frame not received"})
        self.assertEqual(self.closed, 1)
        self.keeper.handle({"cmd": "state"})
        self.assertEqual(self.opened, 2)

    def test_bad_requests(self):
        self.assertFalse(self.keeper.handle({"cmd": "runlua"})["ok"])
        self.assertFalse(self.keeper.handle({"cmd": "state", "fields": ["secrets"]})["ok"])
        self.assertFalse(self.keeper.handle({"cmd": "way", "map": 1})["ok"])  # missing x, y
        self.assertFalse(self.keeper.handle({"cmd": "chat", "channel": "trade", "text": "wts"})["ok"])
        self.assertEqual((self.opened, self.sent), (0, []))  # refused before any link opened

    def test_every_command_is_prepared_once_in_agent_py(self):
        # agentd has no command list or packet builders of its own: a waypoint (once missing from its
        # checks) is built by agent.prepare and sent as is.
        self.keeper.handle({"cmd": "way", "map": 1454, "x": 36, "y": 87.7})
        self.assertEqual(self.sent, [host.build_way(1454, 36, 87.7)])
        self.assertIn("way", host.COMMANDS)
        self.assertFalse(self.keeper.handle({"cmd": "way", "map": 1454, "x": 136, "y": 87.7})["ok"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.opened, 1)

    def test_a_dropped_question_frees_the_link_and_its_queued_requests_never_run(self):
        # Its prompt holds the lock waiting for Ben; its next request queues behind it. The voice drops
        # the question (cancel_requests): the prompt ends within its next check, the queued request is
        # refused unrun, and the next question's request runs.
        import threading, time as t
        started = threading.Event()

        def waits_for_ben(lk, cancelled=None):
            started.set()
            while not cancelled():
                t.sleep(0.01)
            return {"ok": False, "cancelled": True}
        real = host.prepare
        prompt = mock.Mock(run=waits_for_ben)
        with mock.patch.object(host, "prepare", lambda req: prompt if req["cmd"] == "prompt" else real(req)):
            out = {}
            first = threading.Thread(target=lambda: out.update(prompt=self.keeper.handle({"cmd": "prompt"})))
            first.start()
            started.wait(2)
            queued = threading.Thread(target=lambda: out.update(chat=self.keeper.handle(
                {"cmd": "chat", "channel": "say", "target": "", "text": "old question's message"})))
            queued.start()
            t.sleep(0.05)
            self.keeper.cancel_requests()
            first.join(2)
            queued.join(2)
        self.assertTrue(out["prompt"]["cancelled"])
        self.assertTrue(out["chat"]["cancelled"])
        self.assertEqual(self.sent, [])  # the chat never went out
        self.assertTrue(self.keeper.handle({"cmd": "state", "fields": ["zone"]})["ok"])  # the next question's

    def test_a_client_that_hung_up_gets_nothing_run(self):
        reply = self.keeper.handle({"cmd": "chat", "channel": "say", "target": "", "text": "hi"}, client_gone=lambda: True)
        self.assertTrue(reply["cancelled"])
        self.assertEqual(self.sent, [])

    def test_the_service_notices_a_client_hang_up(self):
        import socket, threading, time as t
        seen = threading.Event()

        def waits_for_ben(lk, cancelled=None):
            for _ in range(300):
                if cancelled():
                    seen.set()
                    break
                t.sleep(0.01)
            return {"ok": False, "cancelled": True}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(host, "prepare", return_value=mock.Mock(run=waits_for_ben)):
            path = os.path.join(tmp, "s.sock")
            threading.Thread(target=self.agentd.serve, args=(self.keeper, path), daemon=True).start()
            for _ in range(100):
                if os.path.exists(path):
                    break
                t.sleep(0.01)
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(path)
            c.sendall(b'{"cmd": "prompt"}\n')
            t.sleep(0.1)
            c.close()  # the asker's process was ended
            self.assertTrue(seen.wait(2))

    def test_a_request_the_service_took_is_never_sent_again(self):
        # A timeout after the keeper took the request: reported as unknown, not handed to a second link.
        from decklink import agentd
        with mock.patch.object(agentd, "ask", side_effect=agentd.NoAnswer("timed out")):
            code, out = host.via_service(["chat", "say", "", "hello"])
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(out)["unknown"])
        with mock.patch.object(agentd, "ask", side_effect=agentd.NotRunning(2, "no socket")):
            self.assertIsNone(host.via_service(["chat", "say", "", "hello"]))  # nothing sent: do it yourself

    def test_a_slow_service_is_no_answer_not_not_running(self):
        import socket, threading
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "slow.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(path)
            srv.listen(1)
            got = []
            threading.Thread(target=lambda: got.append(srv.accept()), daemon=True).start()
            with self.assertRaises(self.agentd.NoAnswer):
                self.agentd.ask({"cmd": "status"}, path, timeout=0.2)
            with self.assertRaises(self.agentd.NotRunning):
                self.agentd.ask({"cmd": "status"}, os.path.join(tmp, "nobody.sock"))
            for conn, _ in got:
                conn.close()
            srv.close()

    def test_socket_round_trip(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.sock")
            threading.Thread(target=self.agentd.serve, args=(self.keeper, path), daemon=True).start()
            for _ in range(100):
                if os.path.exists(path):
                    break
                __import__("time").sleep(0.01)
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            reply = self.agentd.ask({"cmd": "state", "fields": ["zone"]}, path)
            self.assertEqual((reply["ok"], reply["fields"]), (True, ["zone"]))
            with self.assertRaises(OSError):
                self.agentd.ask({"cmd": "status"}, os.path.join(tmp, "nobody.sock"))


class Roles(unittest.TestCase):
    def setUp(self):
        self.db = gamedb.connect(":memory:")
        self.addCleanup(self.db.close)
        gamedb.load_maps(self.db, UIMAP_CSV, UMA_CSV)
        extra = ("[6] = {'Doras',100,100,30,30,0,{[1637]={{45.1,63.9}}},nil,1637,nil,nil,29,\"H\",\"Wind Rider Master\",11},\n"
                 "[7] = {'Far Flyer',100,100,30,30,0,{[1637]={{90,10}}},nil,1637,nil,nil,29,\"H\",\"Flight Master\",0},\n"
                 "[8] = {'Auctioneer Thathung',100,100,30,30,0,{[1637]={{55,62}}},nil,1637,nil,nil,29,\"H\",nil,4096},\n"
                 "[9] = {'Horse Breeder',100,100,30,30,0,{[1637]={{50,80}}},nil,1637,nil,nil,29,\"H\",\"Horse Breeder\",132},\n"
                 "[10] = {'Innkeeper Gryshka',100,100,30,30,0,{[1637]={{54,68}}},nil,1637,nil,nil,29,\"H\",\"Innkeeper\",135},\n")
        gamedb.load_npcs(self.db, NPC_LUA.replace("}]]", extra + "}]]"), ZONES_LUA)
        self.here = {"map": 1454, "x": 0.5, "y": 0.8}

    def names(self, **kw):
        return [r["name"] for r in gamedb.find_npc(self.db, state=self.here, faction="H", limit=10, **kw)]

    def test_flight_master_by_flag_and_by_title(self):
        self.assertEqual(self.names(role="flight_master"), ["Doras", "Far Flyer"])  # flag 0x8; flags 0 but titled

    def test_untitled_auctioneer_found_by_flag(self):
        self.assertEqual(self.names(role="auctioneer"), ["Auctioneer Thathung"])

    def test_innkeeper_flag_alone_isnt_enough(self):
        self.assertEqual(self.names(role="innkeeper"), ["Innkeeper Gryshka"])  # not the horse breeder

    def test_role_and_words_and_filler(self):
        self.assertEqual(self.names(role="flight_master", query="the nearest doras"), ["Doras"])
        self.assertEqual(self.names(query="nearest"), [])
        with self.assertRaises(ValueError):
            self.names(role="mailbox")


class VoiceAnswers(unittest.TestCase):
    """voice.py: a voice question (from Decktation's agent binding) answered and spoken."""

    def setUp(self):
        from decklink import voice
        self.voice = voice
        self.said, self.shown, self.ran = [], [], []
        indicator = mock.Mock(show=lambda state: self.shown.append(state))

        def run(cmd, **kw):
            self.ran.append(cmd)
            return mock.Mock(stdout="Waypoint set: Doras, Orgrimmar.\n", stderr="", returncode=0)
        self.v = voice.Voice(indicator=indicator, run=run, say=self.said.append)

    def wait(self):
        for _ in range(200):
            if not self.v.busy:
                return
            __import__("time").sleep(0.01)

    def test_answers_closes_the_box_and_speaks(self):
        self.assertTrue(self.v.ask("  nearest flight master "))
        self.wait()
        self.assertEqual(self.ran, [[self.voice.DECKAGENT, "nearest flight master"]])
        self.assertEqual(self.shown, ["thinking", "done"])
        self.assertEqual(self.said, ["Waypoint set: Doras, Orgrimmar."])

    def test_phantoms_close_the_box_and_go_nowhere(self):
        for text in ("   ", "you", "Thank you.", " Thanks for watching! ", "...", "Um"):
            self.assertFalse(self.v.ask(text), text)
        self.assertEqual(self.ran, [])
        self.assertEqual(self.shown, ["done"] * 6)
        self.assertFalse(self.voice.is_phantom("thank you, where is Thrall"))

    def test_one_question_at_a_time_and_none_empty(self):
        self.assertFalse(self.v.ask("   "))
        self.v.busy = True
        self.assertFalse(self.v.ask("another"))
        self.v.busy = False
        self.assertEqual(self.ran, [])

    def test_a_failure_says_so(self):
        def broken(cmd, **kw):
            raise OSError("no deckagent")
        self.v.run = broken
        self.v.ask("anything")
        self.wait()
        self.assertEqual(self.shown, ["thinking", "error"])
        self.assertEqual(self.said, ["Sorry, that didn't work."])

    def test_a_new_recording_cuts_off_the_answer(self):
        import threading
        release = threading.Event()

        def slow(cmd, **kw):
            self.ran.append(cmd)
            release.wait(2)
            return mock.Mock(stdout="Old answer.\n", stderr="", returncode=0)
        self.v.run = slow
        stops = []
        self.v.say = mock.Mock(side_effect=self.said.append, stop=lambda: stops.append(1) or True)
        self.assertTrue(self.v.ask("first question"))
        self.v.interrupt()  # L5+R5 again, even a tap: stop talking, drop the question
        self.assertFalse(self.v.busy)
        self.assertTrue(self.v.ask("second question"))  # not turned away
        release.set()
        self.wait()
        __import__("time").sleep(0.05)
        self.assertEqual(stops, [1])
        self.assertEqual(self.said, ["Old answer."])  # one answer: the second question's, not both
        self.assertEqual(self.shown.count("done"), 1)

    def test_dropping_a_question_ends_its_whole_process_group(self):
        # deckagent starts claude, which starts the MCP server: all of them go, even one that ignores
        # SIGTERM (killed after the grace), and the old run never clears the next question's process.
        import threading
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(self.voice, "KILL_GRACE_S", 0.3):
            pidfile = os.path.join(tmp, "child")
            v = self.voice.Voice(indicator=mock.Mock(), say=mock.Mock())
            v.busy = True
            done = []
            cmd = ["sh", "-c", 'trap "" TERM; sleep 30 & echo $! > %s; wait' % pidfile]
            t = threading.Thread(target=lambda: done.append(v._run(cmd, gen=0)), daemon=True)
            t.start()
            for _ in range(200):
                if os.path.exists(pidfile) and open(pidfile).read().strip():
                    break
                __import__("time").sleep(0.01)
            child = int(open(pidfile).read())
            v.interrupt()
            t.join(5)
            self.assertTrue(done)
            for _ in range(300):
                try:
                    os.kill(child, 0)
                except ProcessLookupError:
                    break
                __import__("time").sleep(0.01)
            else:
                self.fail("the grandchild outlived its dropped question")
            newer = object()
            v.proc, v.gen = (1, newer), 1
            v._run(["true"], gen=0)  # an older question's runner, finishing late
            self.assertEqual(v.proc, (1, newer))

    def test_time_waiting_on_ben_isnt_the_assistants(self):
        from decklink import agentd
        clock = [1000.0]
        keeper = agentd.Keeper(25, make_link=lambda: None)
        keeper._ensure = lambda: object()

        def answered_after_10_s(lk, cancelled=None):
            clock[0] += 10
            return {"ok": True, "answer": "A"}
        with mock.patch.object(agentd.agent, "prepare", return_value=mock.Mock(run=answered_after_10_s)), \
                mock.patch.object(agentd.time, "monotonic", lambda: clock[0]):
            for _ in range(8):  # every wait counts, the sixth and later too
                keeper.handle({"cmd": "prompt"})
            self.assertEqual(keeper.prompt_waited(), 80)
            keeper._waiting_since = clock[0] - 5  # one up now, for 5 s
            self.assertEqual(keeper.prompt_waited(), 85)
        keeper._waited, keeper._waiting_since = 30.0, None
        v = self.voice.Voice(indicator=mock.Mock(), say=mock.Mock(), game=keeper)
        t = __import__("time").monotonic()
        # The question started 60 s ago, when the keeper's total was 10: 20 s of it were Ben's.
        self.assertAlmostEqual(v.model_seconds(t - 60, 10.0), 40, delta=0.5)
        v = self.voice.Voice(indicator=mock.Mock(), say=mock.Mock())  # no keeper: all of it counts
        start = __import__("time").monotonic()
        out = v._run(["sleep", "30"], timeout=0.2)
        self.assertNotEqual(out.returncode, 0)
        self.assertLess(__import__("time").monotonic() - start, 5)

    def test_a_reminder_asks_for_quiet_first(self):
        asked = []
        game = mock.Mock(quiet=False, shown_at=0)

        def handle(req):
            asked.append(req)
            if req["cmd"] == "state":
                game.quiet = True  # Ben turned Quiet on since the last answer
            return {"ok": True}
        game.handle = handle
        said = []
        v = self.voice.Voice(indicator=mock.Mock(), say=said.append, game=game)
        with mock.patch("threading.Thread", lambda target, daemon=None, args=(): mock.Mock(start=lambda: target(*args))):
            v.remind("Train your spells.", "Hook: level 20")
        self.assertEqual(asked[0], {"cmd": "state", "fields": ["voice"]})
        self.assertEqual(said, [])

    def test_dictation_recording_interrupts_the_voice(self):
        from decklink import agentd
        keeper = agentd.Keeper(25, make_link=lambda: None)
        calls = []
        keeper.voice = mock.Mock(interrupt=lambda drop_question: calls.append(drop_question))
        with mock.patch.object(agentd.dictation, "post"):
            keeper.handle({"cmd": "dictation", "state": "recording", "binding": "agent"})
            keeper.handle({"cmd": "dictation", "state": "recording", "binding": "chat"})
            keeper.handle({"cmd": "dictation", "state": "done", "binding": "agent"})
        self.assertEqual(calls, [True, False])  # chat dictation only stops the speech

    def test_the_service_hands_questions_over(self):
        from decklink import agentd
        keeper = agentd.Keeper(25, make_link=lambda: None)
        self.assertFalse(keeper.handle({"cmd": "ask", "text": "hi"})["ok"])  # voice off
        keeper.voice = mock.Mock(ask=lambda text: text == "hi")
        self.assertTrue(keeper.handle({"cmd": "ask", "text": "hi"})["ok"])


class DictationThroughTheService(unittest.TestCase):
    def test_the_hook_can_post_states_through_the_service(self):
        from decklink import agentd, dictation
        keeper = agentd.Keeper(25, make_link=lambda: None)
        sent = []
        keeper.dictation_keys = type("S", (), {"send": lambda self, bits: sent.append(bits)})()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(agentd.link, "HOLD_FILE", os.path.join(tmp, "h")):
            self.assertTrue(keeper.handle({"cmd": "dictation", "state": "recording", "binding": "agent"})["ok"])
            self.assertTrue(agentd.link.held())
        self.assertEqual(len(sent), 1)
        bits = "".join(map(str, sent[0]))
        frame = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
        self.assertEqual(codec.parse(frame)[2], dictation.encode("recording", "agent"))
        self.assertFalse(keeper.handle({"cmd": "dictation", "state": "shouting"})["ok"])


class ChatOps(unittest.TestCase):
    """CHAT and FRIENDS (addons/DeckAgent/Chat.lua <-> decklink/agent.py), and the preview window."""

    def send(self, packet, setup="", after=""):
        frame = codec.build(host.CHANNEL, 7, packet)
        lines, s = Game.run("login() " + setup + " " + hello() + ' taps("%s") frame() ' % bitstr(frame) + after)
        reply = host.parse_reply(codec.parse(s.out)[2]) if s and s.out else None
        return reply, lines

    def test_chat_packet(self):
        self.assertEqual(host.build_chat("whisper", "Lucien", "hi"), b"\x43\x06\x06Lucien\x02hi")
        with self.assertRaises(ValueError):
            host.build_chat("trade", "", "wts")
        with self.assertRaises(ValueError):
            host.build_chat("say", "", "x" * 256)
        with self.assertRaises(ValueError):
            host.build_chat("say", "", "   ")

    def test_whisper_goes_out_at_once_when_preview_is_off(self):
        reply, lines = self.send(host.build_chat("whisper", "Lucien", "hello there"))
        self.assertEqual((reply["ok"], reply["result"]), (True, "sent"))
        self.assertIn("CHAT\tWHISPER\tLucien\thello there", lines)

    def test_say_and_escapes_cleaned(self):
        _, lines = self.send(host.build_chat("say", "", "a|cff b\nc"))
        self.assertIn("CHAT\tSAY\tnil\ta/cff b c", lines)

    def test_refusals(self):
        for packet, why in ((host.build_chat("party", "", "inc"), "you're not in a group"),
                            (host.build_chat("guild", "", "hi"), "you're not in a guild"),
                            (host.build_chat("bnet", "Nobody", "hi"), "no online Battle.net friend Nobody"),
                            (b"\x43\x09\x00\x02hi", None)):
            reply, lines = self.send(packet)
            self.assertFalse(reply["ok"], packet)
            if why:
                self.assertEqual(reply["reason"], why)
            self.assertFalse(any(l.startswith(("CHAT", "BNCHAT")) for l in lines))

    def test_battle_net_whisper_by_name_or_tag(self):
        bnet = ('BNET = {{bnetAccountID = 77, battleTag = "Edna#1234", gameAccountInfo = {isOnline = true}},'
                ' {bnetAccountID = 78, battleTag = "Gone#1", gameAccountInfo = {isOnline = false}}}')
        for target in ("edna", "Edna#1234", "77"):
            _, lines = self.send(host.build_chat("bnet", target, "SFK?"), bnet)
            self.assertIn("BNCHAT\t77\tSFK?", lines, target)
        reply, _ = self.send(host.build_chat("bnet", "Gone", "hi"), bnet)
        self.assertFalse(reply["ok"])  # offline

    def test_preview_waits_for_a(self):
        pv = 'DeckAgentDB.previewChat = true'
        press = lambda key: ('local f = DeckAgentPrompt; f.OnGamePadButtonDown(f, "%s") f.OnGamePadButtonUp(f, "%s")'
                             ' print("SHOWN", tostring(f.shown))' % (key, key))
        reply, lines = self.send(host.build_chat("whisper", "Lucien", "hi"), pv)
        self.assertEqual(reply["result"], "previewed")
        self.assertFalse(any(l.startswith("CHAT") for l in lines))
        _, lines = self.send(host.build_chat("whisper", "Lucien", "hi"), pv, press("PAD1"))
        self.assertIn("CHAT\tWHISPER\tLucien\thi", lines)
        self.assertIn("SHOWN\tfalse", lines)
        _, lines = self.send(host.build_chat("whisper", "Lucien", "hi"), pv, press("PAD2"))
        self.assertFalse(any(l.startswith("CHAT") for l in lines))
        _, lines = self.send(host.build_chat("whisper", "Lucien", "hi"), pv, 'fireTimers()' + press("PAD1"))
        self.assertFalse(any(l.startswith("CHAT") for l in lines))  # timed out: A does nothing then
        enter = ('local f = DeckAgentPrompt; if f.OnKeyDown then f.OnKeyDown(f, "ENTER") end'
                 ' print("SHOWN", tostring(f.shown))')
        _, lines = self.send(host.build_chat("whisper", "Lucien", "hi"), pv, enter)
        self.assertFalse(any(l.startswith("CHAT") for l in lines))  # the keyboard (Decktation's Enter) never sends
        self.assertIn("SHOWN\ttrue", lines)

    def test_preview_in_combat_shows_afterwards(self):
        setup = 'DeckAgentDB.previewChat = true combat(true)'
        reply, _ = self.send(host.build_chat("say", "", "gg"), setup,
                             'print("BEFORE", tostring(DeckAgentPrompt and DeckAgentPrompt.shown))'
                             ' combat(false) print("AFTER", tostring(DeckAgentPrompt.shown))')
        self.assertEqual(reply["result"], "previewed after combat")

    def test_friends(self):
        setup = ('GUILD = true'
                 ' BNET = {{bnetAccountID = 77, battleTag = "Edna#1234", gameAccountInfo = {isOnline = true,'
                 '   clientProgram = "WoW", characterName = "Ednalyn", realmName = "Forever", characterLevel = 15,'
                 '   className = "Mage", areaName = "Undercity"}},'
                 '  {bnetAccountID = 78, battleTag = "Bob#2", gameAccountInfo = {isOnline = true, clientProgram = "App"}},'
                 '  {bnetAccountID = 79, battleTag = "Off#3", gameAccountInfo = {isOnline = false}}}'
                 ' FRIENDS = {{connected = true, name = "Grunt", level = 20, className = "Warrior", area = "Durotar"},'
                 '  {connected = false, name = "Away"}}'
                 ' ROSTER = {{"Guildie", 30, "Priest", "Orgrimmar", true}, {"Asleep", 10, "Rogue", "?", false}}')
        reply, _ = self.send(host.build_friends(), setup)
        self.assertEqual(reply["people"], [
            {"list": "bnet", "account_id": 77, "battletag": "Edna#1234", "in_wow": True, "character": "Ednalyn",
             "realm": "Forever", "level": 15, "class": "Mage", "zone": "Undercity"},
            {"list": "bnet", "account_id": 78, "battletag": "Bob#2", "in_wow": False},
            {"list": "friends", "name": "Grunt", "level": 20, "class": "Warrior", "zone": "Durotar"},
            {"list": "guild", "name": "Guildie", "level": 30, "class": "Priest", "zone": "Orgrimmar"}])
        self.assertFalse(reply["more"])
        reply, _ = self.send(host.build_friends(["guild"]), setup)
        self.assertEqual([p["name"] for p in reply["people"]], ["Guildie"])

    def test_too_many_friends_says_more(self):
        setup = ('GUILD = true ROSTER = {} for i = 1, 60 do ROSTER[i] = {"Member" .. i .. string.rep("x", 10),'
                 ' 60, "Warrior", "Orgrimmar", true} end')
        reply, _ = self.send(host.build_friends(["guild"]), setup)
        self.assertTrue(reply["more"])
        self.assertLess(len(reply["people"]), 60)


class Conversation(unittest.TestCase):
    """agent/deckagent: questions close together continue one Claude session."""

    def setUp(self):
        import importlib.machinery, importlib.util
        path = os.path.join(HERE, "..", "agent", "deckagent")
        loader = importlib.machinery.SourceFileLoader("deckagent_launcher", path)
        spec = importlib.util.spec_from_loader("deckagent_launcher", loader)
        self.la = importlib.util.module_from_spec(spec)
        loader.exec_module(self.la)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "session.json")

    def test_prefetch_asks_for_quiet_so_the_keeper_knows_it(self):
        calls = []
        with mock.patch.object(self.la.common, "game", lambda *a, **kw: calls.append(a) or {"ok": True}):
            self.la.prefetch(False)
        self.assertIn("voice", calls[0])

    def test_continues_within_the_window_then_starts_fresh(self):
        la = self.la
        self.assertIsNone(la.last_session(1000, self.path))  # nothing yet
        la.save_session("abc", 1000, self.path)
        self.assertEqual(la.last_session(1000 + la.CONTINUE_S - 1, self.path), "abc")
        self.assertIsNone(la.last_session(1000 + la.CONTINUE_S + 1, self.path))
        la.save_session(None, 2000, self.path)  # no session id: keeps the old record
        self.assertEqual(la.last_session(1001, self.path), "abc")

    def test_start_over_phrases(self):
        for text in ("start over", "Start over, where's Thrall", "new topic: flight master", "forget that",
                     "never mind"):
            self.assertTrue(self.la.FRESH.match(text), text)
        for text in ("where's the nearest innkeeper", "AMI#1362", "whisper Edna"):
            self.assertFalse(self.la.FRESH.match(text), text)


    def test_name_hints_for_misheard_people(self):
        la = self.la
        notes = ("# notes\n## People\nWho Ben means, e.g.\n- Edna: BattleTag Edna#1234; characters Ednalyn\n"
                 "- Edna: BattleTag Sylunne#1513 (bnet account_id 5).\n- Moneygrip (Ace#11886): warrior\n"
                 "## Ben's rules\n- Thrall: never whisper\n")
        self.assertEqual(la.people(notes), {"Edna": "Sylunne#1513", "Moneygrip": "Ace#11886"})
        names = ["Edna"]
        self.assertIn('"Edmunds" is probably Edna',
                      la.name_hints("Whisper Edmunds' character. See if she wants to play.", names))
        self.assertIn('"Adna" is probably Edna', la.name_hints("Her name is Adna, my friend.", names))
        for text in ("Whisper Edna's character", "Need a waypoint to the bank", "and the flight master",
                     "Where is Undercity", "whisper edmunds"):
            self.assertIsNone(la.name_hints(text, names), text)
        self.assertIsNone(la.name_hints("Whisper Edmunds", []))


class Memory(unittest.TestCase):
    """agent/memory.py: characters seen through get_online, and the launcher's hints from them."""

    def setUp(self):
        from agent import memory
        self.memory = memory
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = memory.connect(os.path.join(self.tmp.name, "memory.db"))
        self.addCleanup(self.db.close)

    def test_records_characters_and_finds_them_by_battletag(self):
        m, day = self.memory, 86400
        people = [
            {"list": "bnet", "account_id": 5, "battletag": "Sylunne#1513", "in_wow": True, "character": "Ednalyn",
             "realm": "R4", "level": 20, "class": "Mage", "zone": "Barrens"},
            {"list": "bnet", "account_id": 7, "battletag": "Ace#11886", "in_wow": False},
            {"list": "guild", "name": "Guildie", "level": 30, "class": "Rogue", "zone": "Orgrimmar"},
        ]
        self.assertEqual(m.record_online(self.db, people, now=1000), 2)
        people[0].update(character="Ednabelle", level=12, **{"class": "Priest"})
        m.record_online(self.db, people, now=1000 + 2 * day)
        people[0].update(character="Ednalyn", level=21, **{"class": "Mage"})
        m.record_online(self.db, people, now=1000 + 3 * day)  # updates, doesn't duplicate
        chars = m.characters_of(self.db, "sylunne#1513")
        self.assertEqual([(c["name"], c["level"]) for c in chars], [("Ednalyn", 21), ("Ednabelle", 12)])
        self.assertEqual(chars[0]["first_seen"], 1000)

        import importlib.machinery, importlib.util
        path = os.path.join(HERE, "..", "agent", "deckagent")
        loader = importlib.machinery.SourceFileLoader("deckagent_launcher2", path)
        spec = importlib.util.spec_from_loader("deckagent_launcher2", loader)
        la = importlib.util.module_from_spec(spec)
        loader.exec_module(la)
        tags = {"Edna": "Sylunne#1513", "Bob": None}
        hint = la.character_hints("Whisper Edmunds' character", tags, self.db, now=1000 + 4 * day)
        self.assertEqual(hint, "[Characters you've seen them on: Edna (Sylunne#1513): Ednalyn, level 21 Mage, "
                               "last seen online yesterday; Ednabelle, level 12 Priest, last seen online 2 days ago]")
        self.assertIsNone(la.character_hints("Whisper Bob", tags, self.db))
        self.assertIsNone(la.character_hints("where is Thrall", tags, self.db))


class Facts(unittest.TestCase):
    """agent/memory.py facts: remember_fact / recall / forget_fact, and the launcher's [From your memory]."""

    def setUp(self):
        from agent import memory, mcp_server
        self.memory, self.mcp = memory, mcp_server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = os.path.join(self.tmp.name, "memory.db")
        patcher = mock.patch.object(memory, "PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, name, args):
        return self.mcp.HANDLERS[name](args)

    def test_words_drop_filler_and_possessives(self):
        self.assertEqual(self.memory.words("Does Edna's guild want to run Stratholme? Tell me, Decky."),
                         ["edna", "guild", "run", "stratholme"])

    def test_remember_recall_replace_forget(self):
        a = self.call("remember_fact", {"about": "Edna", "fact": "Hates  Stratholme; prefers healing."})["id"]
        self.call("remember_fact", {"about": "Ben", "fact": "Wants the Robes of Arcana from Scarlet Monastery."})
        found = self.call("recall", {"query": "would Edna come to Stratholme"})["facts"]
        self.assertEqual([(f["id"], f["fact"]) for f in found], [(a, "Hates Stratholme; prefers healing.")])
        self.assertEqual(self.call("recall", {"query": "robes"})["facts"][0]["about"], "Ben")  # prefix match
        b = self.call("remember_fact", {"about": "Edna", "fact": "Tanks now.", "replaces": a})["id"]
        self.assertEqual([f["id"] for f in self.call("recall", {"about": "edna"})["facts"]], [b])
        self.call("forget_fact", {"id": b})
        self.assertEqual(self.call("recall", {"about": "Edna"})["facts"], [])
        with self.assertRaises(ValueError):
            self.call("forget_fact", {"id": 999})
        with self.assertRaises(ValueError):
            self.call("remember_fact", {"about": "x", "fact": "y z", "replaces": 999})
        with self.assertRaises(ValueError):
            self.call("recall", {"query": "  "})

    def test_the_launcher_brings_related_facts(self):
        import importlib.machinery, importlib.util
        path = os.path.join(HERE, "..", "agent", "deckagent")
        loader = importlib.machinery.SourceFileLoader("deckagent_launcher3", path)
        spec = importlib.util.spec_from_loader("deckagent_launcher3", loader)
        la = importlib.util.module_from_spec(spec)
        loader.exec_module(la)
        m = self.memory
        with m.connect() as db:
            m.add_fact(db, "Edna", "Hates Stratholme.", now=1000)
            m.add_fact(db, "Ben", "Saving gold for his mount.", now=1000)
            tags = {"Edna": "Sylunne#1513"}
            hint = la.fact_hints("Whisper Edmunds, see if she wants to do Shadowfang Keep", tags, db)
            self.assertTrue(hint.startswith("[From your memory (recall for more): #1 Edna: Hates Stratholme. ("), hint)
            self.assertNotIn("mount", hint)
            self.assertIsNone(la.fact_hints("where is the nearest flight master", tags, db))
            self.assertIn("mount", la.fact_hints("how much is my mount going to cost", tags, db))


class Asking(unittest.TestCase):
    """ASK / ANSWER (addons/DeckAgent/Ask.lua + Prompt.lua <-> decklink/agent.py) and the ask_player tool."""

    def run_ask(self, packet, after, setup=""):
        """Sends ASK, acks its "shown" reply, runs `after` (button presses...), returns (shown, answer)."""
        frame = codec.build(host.CHANNEL, 7, packet)
        base = "login() " + setup + " " + hello() + ' taps("%s") frame()' % bitstr(frame)
        _, s = Game.run(base)
        shown = host.parse_reply(codec.parse(s.out)[2])
        ack = bitstr(codec.build(0, 8, bytes([codec.OP_ACK, codec.parse(s.out)[1]])))
        _, s = Game.run(base + ' taps("%s") frame() ' % ack + after + " frame()")
        answer = host.parse_reply(codec.parse(s.out)[2]) if s and s.out else None
        return shown, answer

    PRESS = 'local f = DeckAgentPrompt; f.OnGamePadButtonDown(f, "%s") f.OnGamePadButtonUp(f, "%s") '

    def press(self, *keys):
        return "".join(self.PRESS % (k, k) for k in keys)

    def test_packet(self):
        self.assertEqual(host.build_ask("Go?", ["Yes", "No"], seconds=20), b"\x45\x00\x14\x03Go?\x02\x03Yes\x02No")
        with self.assertRaises(ValueError):
            host.build_ask("Go?", ["Only one"])
        with self.assertRaises(ValueError):
            host.build_ask("x" * 161, ["a", "b"])

    def test_dialog_a_and_b(self):
        shown, answer = self.run_ask(host.build_ask("Invite Edna?", ["Yes", "No"]), self.press("PAD1"))
        self.assertEqual((shown["result"], answer["op"], answer["seq"], answer["choice"]), ("shown", 6, 7, 1))
        _, answer = self.run_ask(host.build_ask("Invite Edna?", ["Yes", "No"]), self.press("PAD2"))
        self.assertEqual(answer["choice"], 2)

    def test_list_moves_and_picks_or_cancels(self):
        packet = host.build_ask("Which one?", ["Ednalyn", "Ednabelle", "Edgar"])
        # 1 -> down 2 -> down 3 -> down wraps to 1 -> up wraps to 3
        _, answer = self.run_ask(packet, self.press("PADDDOWN", "PADDDOWN", "PADDDOWN", "PADDUP", "PAD1"))
        self.assertEqual(answer["choice"], 3)
        _, answer = self.run_ask(packet, self.press("PAD2"))
        self.assertEqual(answer["choice"], 0)

    def test_timeout_and_replaced_say_no_answer(self):
        _, answer = self.run_ask(host.build_ask("Q?", ["Yes", "No"]), "fireTimers()")
        self.assertIsNone(answer["choice"])

    def test_other_buttons_pass_through(self):
        _, answer = self.run_ask(host.build_ask("Q?", ["Yes", "No"]), self.press("PADDDOWN", "PAD3"))
        self.assertIsNone(answer)  # a dialog ignores the d-pad and X: no answer yet

    def test_ask_waits_for_its_answer_and_keeps_the_link_alive(self):
        class FakeLink:
            def __init__(self):
                self.sent, self.queue, self.t = [], [], 0.0

            def clock(self):
                return self.t

            def send(self, ch, payload):
                self.sent.append((ch, bytes(payload)))
                if ch == 3:
                    self.queue = [(3, b"\x45\x21\x00\x01")] + [None] * 30 + [(3, b"\x46\x21\x00\x02")]
                return 0x21

            def recv(self, timeout):
                self.t += 1.0
                return self.queue.pop(0) if self.queue else None

            def keepalive(self):
                self.kept += 1
        lk = FakeLink()
        lk.kept = 0
        out = host.ask(lk, "Which?", ["Ednalyn", "Ednabelle"], clock=lk.clock)
        self.assertEqual(out, {"ok": True, "shown": "shown", "choice": 2, "answer": "Ednabelle"})
        self.assertGreaterEqual(lk.kept, 2)
        self.assertEqual([ch for ch, _ in lk.sent], [3])  # nothing else went out on its own


class Dismissing(unittest.TestCase):
    """DISMISS (Ask.lua): a prompt the host gave up on goes, unanswered and with no ANSWER sent."""

    def test_dismiss_takes_the_prompt_down_without_an_answer(self):
        self.assertEqual(host.build_dismiss(7), b"\x53\x07")
        ask_frame = bitstr(codec.build(host.CHANNEL, 7, host.build_ask("Go?", ["Yes", "No"])))
        base = "login() " + hello() + ' taps("%s") frame() ' % ask_frame
        _, s = Game.run(base)
        ack = lambda st, seq: 'taps("%s") frame() ' % bitstr(codec.build(0, seq, bytes([codec.OP_ACK, codec.parse(st.out)[1]])))
        dismiss = 'taps("%s") frame() ' % bitstr(codec.build(host.CHANNEL, 9, host.build_dismiss(7)))
        shown = 'print("PROMPT", tostring(DeckAgentPrompt.shown)) '
        lines, s2 = Game.run(base + ack(s, 8) + dismiss + shown)
        reply = host.parse_reply(codec.parse(s2.out)[2])
        self.assertEqual((reply["op"], reply["dismissed"]), (host.OP_DISMISS, True))
        self.assertIn("PROMPT\tfalse", lines)
        # Its time running out later sends nothing: the strip is empty once the reply is acked.
        _, s3 = Game.run(base + ack(s, 8) + dismiss + ack(s2, 10) + "fireTimers() frame()")
        self.assertIsNone(s3.out if s3 else None)
        # Another seq (a prompt that isn't the latest) is left alone.
        _, s4 = Game.run(base + ack(s, 8) + 'taps("%s") frame() ' % bitstr(codec.build(host.CHANNEL, 9, host.build_dismiss(3))))
        self.assertFalse(host.parse_reply(codec.parse(s4.out)[2])["dismissed"])

    def test_a_cancelled_wait_dismisses_and_returns_at_once(self):
        sent = []

        class FakeLink:
            t = 0.0

            def send(self, ch, payload):
                sent.append(bytes(payload))
                return 0x21 if payload[0] & 0x3F == host.OP_ASK else 0x22

            def recv(self, timeout):
                self.t += 1.0
                if sent[-1][0] & 0x3F == host.OP_DISMISS:
                    return (3, b"\x53\x22\x00\x01")
                return None
        lk = FakeLink()
        calls = [0]

        def cancelled():
            calls[0] += 1
            return calls[0] > 2
        out = host.prepare({"cmd": "prompt", "question": "Which?", "options": ["A", "B"]}).run(lk, cancelled)
        self.assertTrue(out["cancelled"])
        self.assertEqual(sent[-1], host.build_dismiss(0x21))
        self.assertLess(lk.t, 10)  # seconds, not the prompt's minutes


class Transcripts(Conversation):
    def test_one_line_per_question(self):
        la = self.la
        la.TRANSCRIPTS = os.path.join(self.tmp.name, "transcripts")
        la.write_transcript({"question": "nearest innkeeper", "steps": [{"tool": "find_npc"}], "answer": "Gryshka"})
        la.write_transcript({"question": "thanks", "answer": "ok"})
        (name,) = os.listdir(la.TRANSCRIPTS)
        with open(os.path.join(la.TRANSCRIPTS, name)) as f:
            records = [json.loads(line) for line in f]
        self.assertEqual([r["question"] for r in records], ["nearest innkeeper", "thanks"])


class Layouts(unittest.TestCase):
    """SHOW kind layout (Show.lua <-> decklink/agent.py layout_packet) and the show_layout tool."""

    SPEC = {"type": "list", "id": "sfk", "items": [{"type": "item", "id": 6948}],
            "footer": {"type": "row", "items": [
                {"type": "button", "text": "In", "buttons": {"a": {"reply": "in", "label": "Join"}}},
                {"type": "button", "text": "Out", "buttons": {"a": {"reply": {"why": "busy"}}}}]}}

    def test_reply_values_go_as_numbers(self):
        packet, replies = host.layout_packet(self.SPEC, wait=30)
        self.assertEqual(replies, ["in", {"why": "busy"}])
        self.assertEqual(packet[1:3], bytes([4, 30]))  # kind layout, waiting 30 s
        text = packet[11:].decode()
        self.assertIn('"reply":1', text)
        self.assertIn('"reply":2', text)
        self.assertNotIn(" ", text.replace('"In"', "").replace('"Out"', ""))  # compact
        plain, none = host.layout_packet({"type": "text", "text": "hi"}, wait=30)
        self.assertEqual((none, plain[2]), ([], 0))  # nothing to reply with: nobody waits
        with self.assertRaises(ValueError):
            host.layout_packet({"items": []})
        prepared = host.prepare({"cmd": "layout", "spec": self.SPEC, "wait": 30})
        self.assertTrue(prepared.waits)
        self.assertFalse(host.prepare({"cmd": "layout", "spec": self.SPEC}).waits)

    def test_the_cli_waits_as_long_as_the_layout_does(self):
        self.assertEqual(host.service_wait({"cmd": "layout", "wait": 120}), 270)
        self.assertEqual(host.service_wait({"cmd": "layout"}), 0)
        self.assertEqual(host.service_wait({"cmd": "prompt", "seconds": 45}), 195)

    def run_layout(self, after):
        packet, _ = host.layout_packet(self.SPEC, wait=30)
        return Asking.run_ask(self, packet, after)

    def test_a_reply_is_an_answer_and_b_cancels(self):
        shown, answer = self.run_layout("LAYOUT.opts.onReply(2)")
        self.assertEqual((shown["ok"], shown["result"], answer["op"], answer["seq"], answer["choice"]), (True, "shown", 6, 7, 2))
        _, answer = self.run_layout("DeckKit.UI.HideLayout()")
        self.assertEqual(answer["choice"], 0)

    def test_the_game_shows_it_titled_decky_with_its_ticks(self):
        packet, _ = host.layout_packet(self.SPEC)
        data, lines = ask(packet, 'DeckAgentDB.layoutTicks = { sfk = { ["1.1"] = true } }')
        self.assertTrue(host.parse_reply(data)["ok"])
        self.assertIn("LAYOUT list Decky", lines)
        lines, _ = Game.run("login() " + hello() + ' taps("%s") frame() print("TICKS " .. tostring(LAYOUT.opts.ticks == '
                            'DeckAgentDB.layoutTicks.sfk))' % bitstr(codec.build(host.CHANNEL, 7, packet)))
        self.assertIn("TICKS true", lines)

    def test_in_combat_it_waits_for_the_fight_to_end(self):
        packet, _ = host.layout_packet(self.SPEC)
        frame = bitstr(codec.build(host.CHANNEL, 7, packet))
        lines, s = Game.run('login() combat(true) ' + hello() + ' taps("%s") frame() print("UP " .. tostring(LAYOUT ~= nil)) '
                            'combat(false) print("UP " .. tostring(LAYOUT ~= nil and LAYOUT.shown))' % frame)
        self.assertEqual(host.parse_reply(codec.parse(s.out)[2])["result"], "queued")
        self.assertEqual([l for l in lines if l.startswith("UP ")], ["UP false", "UP true"])

    def test_not_a_layout_is_refused(self):
        data, _ = ask(host.build_show("layout", "", "{not json"))
        reply = host.parse_reply(data)
        self.assertFalse(reply["ok"])
        self.assertIn("layout", reply.get("reason", ""))


class Showing(unittest.TestCase):
    """SHOW (addons/DeckAgent/Show.lua <-> decklink/agent.py) and speak or show (decklink/voice.py)."""

    def test_packet(self):
        self.assertEqual(host.build_show("card", "Hi", "two\nlines", seconds=20, item=6948),
                         b"\x47\x02\x14" + struct.pack(">I", 6948) + b"\x02Hi" + struct.pack(">H", 9) + b"two\nlines")
        echo = host.build_show("echo", "q", "x" * 5000)  # an echo is cut, not refused
        self.assertEqual(struct.unpack(">H", echo[9:11])[0], 1000)
        self.assertTrue(echo.endswith("\u2026".encode()))
        with self.assertRaises(ValueError):
            host.build_show("card", "t", "x" * 601)
        with self.assertRaises(ValueError):
            host.build_show("poster", "t", "x")
        self.assertLessEqual(len(codec.build(host.CHANNEL, 1, host.build_show("reading", "t" * 120, "x" * host.SHOW_TEXT["reading"]))),
                             codec.MAX_IN + 16)
        self.assertEqual(mcp_server.SHOW_TEXT, {k: v for k, v in host.SHOW_TEXT.items() if k not in ("echo", "layout")})

    def test_a_card_without_seconds_stays_long_enough_to_read(self):
        # One rule (card_seconds) for every card: respond's (no --seconds) and the voice's.
        text = "x" * 300
        self.assertEqual(host.build_show("card", "t", text)[2], host.card_seconds(text))
        self.assertEqual(host.card_seconds(text), 28)
        self.assertEqual(host.card_seconds("y" * 5000), 60)
        self.assertEqual(host.build_show("card", "t", text, seconds=0)[2], 0)  # until he closes it
        self.assertEqual(host.build_show("reading", "t", text)[2], 0)

    def test_echo_goes_to_chat_and_reply_says_quiet(self):
        data, lines = ask(host.build_show("echo", "You asked: where's the |cff bank", "Waypoint set: the bank."))
        self.assertIn("ECHO You asked: where's the ||cff bank", lines)
        self.assertTrue(any(l.startswith("ECHO ") and l.endswith("Decky:|r Waypoint set: the bank.") for l in lines))
        reply = host.parse_reply(data)
        self.assertEqual((reply["ok"], reply["result"], reply["quiet"]), (True, "shown", False))
        data, _ = ask(host.build_show("echo", "", "hi"), "DeckAgentDB.quiet = true")
        self.assertTrue(host.parse_reply(data)["quiet"])
        data, _ = ask(host.build_state(["voice"]), "DeckAgentDB.quiet = true")
        self.assertEqual(host.parse_reply(data, ["voice"])["quiet"], True)

    def test_card_shows_and_b_or_time_closes_it(self):
        packet = host.build_show("card", "SFK", "Missing 2 quests", seconds=10, item=6948)
        show = 'print("CARD", tostring(DeckAgentCard.shown), DeckAgentCard.title.text, DeckAgentCard.text.text) '
        press = 'local c = DeckAgentCardButtons; c.OnGamePadButtonDown(c, "PAD2") c.OnGamePadButtonUp(c, "PAD2") '
        lines, _ = Game.run("login() " + hello() + ' taps("%s") frame() ' % bitstr(codec.build(host.CHANNEL, 7, packet))
                            + show + press + show)
        self.assertIn("CARD\ttrue\tSFK\tMissing 2 quests", lines)
        self.assertIn("CARD\tfalse\tSFK\tMissing 2 quests", lines)
        lines, _ = Game.run("login() " + hello() + ' taps("%s") frame() ' % bitstr(codec.build(host.CHANNEL, 7, packet))
                            + "fireTimers() " + show)
        self.assertIn("CARD\tfalse\tSFK\tMissing 2 quests", lines)

    def test_reading_waits_for_combat_and_takes_its_buttons(self):
        packet = host.build_show("reading", "Walkthrough", "# Step one\n- go there\nthen here")
        data, lines = ask(packet, "combat(true)", 7)
        self.assertEqual(host.parse_reply(data)["result"], "queued")
        lines, _ = Game.run("login() combat(true) " + hello() + ' taps("%s") frame() ' % bitstr(
            codec.build(host.CHANNEL, 7, packet)) + 'print("BEFORE", tostring(DeckAgentReader)) combat(false) '
            'print("AFTER", tostring(DeckAgentReader.shown), DeckAgentReader.body.text) '
            'local c = DeckAgentReaderButtons; c.OnGamePadButtonDown(c, "PAD2") c.OnGamePadButtonUp(c, "PAD2") '
            'print("CLOSED", tostring(DeckAgentReader.shown))')
        self.assertIn("BEFORE\tnil", lines)
        i = lines.index("AFTER\ttrue\t|cffffd100Step one|r")
        self.assertEqual(lines[i + 1:i + 3], ["  \u2022 go there", "then here"])
        self.assertIn("CLOSED\tfalse", lines)

    def test_bad_packets_are_refused(self):
        data, _ = ask(bytes([host.op_byte(host.OP_SHOW), 2, 0, 0, 0, 0, 0, 5]) + b"ab")
        self.assertEqual(host.parse_reply(data)["status"], "bad arguments")
        data, _ = ask(host.build_show("echo", "q", "a")[:1] + b"\x09" + host.build_show("echo", "q", "a")[2:])
        self.assertEqual(host.parse_reply(data)["reason"], "unknown kind 9")

    def test_cli(self):
        self.assertEqual(host._request_for(["show", "card", "--seconds", "5", "--item", "12", "T", "body"]),
                         {"cmd": "show", "kind": "card", "seconds": 5, "item": 12, "title": "T", "text": "body"})
        self.assertIsNone(host._request_for(["show", "card", "only title"]))


class SpeakOrShow(unittest.TestCase):
    def deliver(self, answer, quiet=False, shown_at=0, heard=""):
        from decklink import voice
        said, shown = [], []

        class Game:
            def handle(self, req):
                shown.append(req)
                return {"ok": True}
        g = Game()
        g.quiet, g.shown_at = quiet, shown_at
        v = voice.Voice(indicator=mock.Mock(), run=mock.Mock(), say=said.append, game=g)
        with mock.patch("threading.Thread", lambda target, daemon=None, args=(): mock.Mock(start=lambda: target(*args))):
            v.deliver("the question", answer, asked_at=10, heard=heard)
        return said, shown

    def test_short_answer_is_said_and_echoed(self):
        said, shown = self.deliver("Waypoint set: the bank.")
        self.assertEqual(said, ["Waypoint set: the bank."])
        self.assertEqual([r["kind"] for r in shown], ["echo"])
        self.assertEqual((shown[0]["title"], shown[0]["text"]), ("You asked: the question", "Waypoint set: the bank."))

    def test_long_answer_says_what_fits_and_shows_it_all(self):
        long = ("You need three things for this run. First the key from the Silverpine camp, which drops off the "
                "ogres there. Then the robe from the vendor in Undercity, near the bank. Then a group of five for the "
                "dungeon itself, ideally with a healer.")
        said, shown = self.deliver(long)
        self.assertTrue(said[0].endswith(". The rest is on screen.") and len(said[0]) <= 200, said)
        self.assertTrue(long.startswith(said[0][:-len(" The rest is on screen.")]), said)  # whole sentences
        self.assertEqual([r["kind"] for r in shown], ["echo", "card"])
        self.assertEqual(shown[1]["text"], long)

    def test_respond_sized_answers_are_said_whole(self):
        # Ben, 2026-11-14: this came out as "Not quite. It's on screen." (cut by sentence count).
        say = ("Not quite. Rahauro still has two for you, and both show as not taken. Only the satchel follow-up "
               "is inside, so grab those two first.")
        said, shown = self.deliver(say, shown_at=11)
        self.assertEqual(said, [say])
        self.assertEqual([r["kind"] for r in shown], ["echo"])

    def test_quiet_says_nothing_and_shows_a_card(self):
        said, shown = self.deliver("Waypoint set.", quiet=True)
        self.assertEqual(said, [])
        self.assertEqual([r["kind"] for r in shown], ["echo", "card"])

    def test_no_second_card_when_the_assistant_showed_one(self):
        answer = "Route's on screen. " + "Then the next stop, and the one after that, all along the road. " * 4
        said, shown = self.deliver(answer.strip(), shown_at=11)
        self.assertEqual([r["kind"] for r in shown], ["echo"])  # she showed the full version
        self.assertTrue(said[0].startswith("Route's on screen.") and len(said[0]) <= 200, said)

    def test_heard_as_goes_to_chat_only(self):
        from decklink import voice
        spoken, heard = voice.split_heard("Waypoint set: Ragefire Chasm.\nHeard as: \"rage fire\" is Ragefire Chasm")
        self.assertEqual((spoken, heard), ("Waypoint set: Ragefire Chasm.", 'Heard as: "rage fire" is Ragefire Chasm'))
        self.assertEqual(voice.split_heard("Done.\nNote: from warcrafttavern.com"), ("Done.", "Note: from warcrafttavern.com"))
        said, shown = self.deliver("Waypoint set.", heard=heard)
        self.assertEqual(said, ["Waypoint set."])
        self.assertEqual(shown[0]["text"], 'Waypoint set. (Heard as: "rage fire" is Ragefire Chasm)')
        self.assertEqual([r["kind"] for r in shown], ["echo"])

    def test_answers_are_cleaned_of_sources_and_links(self):
        from decklink import voice
        raw = ("There are four gather quests for Ragefire Chasm: the Satchel and the Heart. "
               "The list is on your screen with sources.\n\nSources:\n"
               "- [Warcrafttavern guide](https://www.warcrafttavern.com/forever/guides/rfc/)\n"
               "- [Gameskinny](https://www.gameskinny.com/tips/rfc/)\n")
        self.assertEqual(voice.clean_answer(raw), "There are four gather quests for Ragefire Chasm: the Satchel "
                         "and the Heart. The list is on your screen with sources.")
        self.assertEqual(voice.clean_answer("See **[Wowhead](https://wowhead.com/x)** for more"), "See Wowhead for more.")
        self.assertEqual(voice.clean_answer("Two options:\n- **Satchel**\n- Heart"), "Two options: Satchel. Heart.")
        self.assertEqual(voice.clean_answer("Waypoint set: the bank."), "Waypoint set: the bank.")


QUEST_LUA = """QuestieDB.questData = [[return {
[5728] = {"Hidden Enemies",{{1}},{{1}},13,16,nil,nil,{"Kill Bazzalan and Jergosh."},nil,{{{2,nil},{3,"Jergosh the Invoker slain"}}},nil,nil,{5727},nil,nil,nil,2437},
[5727] = {"Hidden Enemies (part 1)",{{1}},{{1}},10,12,178,nil,{"Talk to Thrall."},nil,nil,nil,nil,nil,nil,nil,nil,2437},
[5900] = {"Alliance Only",{{3}},{{3}},10,12,77,nil,{"Nope."},nil,nil,nil,nil,nil,nil,nil,nil,2437},
[5725] = {"The Power to Destroy...",{{1}},{{5}},14,16,nil,nil,{"Bring the books."},nil,{nil,nil,{{900}}}},
}]]"""
ITEM_LUA = """QuestieDB.itemData = [[return {
[900] = {'Spells of Shadow',{2,3},nil,nil,nil,nil,nil,nil,1,0,0,12,0,{5}},
}]]"""
OBJECT_LUA = """QuestieDB.objectData = [[return {
[31] = {"Old Lion Statue",nil,nil,{[1497]={{50,50}}},1497},
}]]"""


class Quests(unittest.TestCase):
    """QUESTS (addons/DeckAgent/Quests.lua <-> decklink/agent.py), the quest database and get_quests."""

    def test_game_sends_ids_and_counts_only(self):
        data, _ = ask(host.build_quests())
        self.assertEqual(data[3:], b"\x00\x02" + struct.pack(">IBB", 5728, 0, 2) + b"\x81\x01\x01\x01\x00\x01"
                         + struct.pack(">IBB", 5725, 1, 1) + b"\x82\xff\xff")
        self.assertNotIn(b"Orgrimmar", data)
        reply = host.parse_reply(data)
        self.assertEqual(reply["quests"][0], {"id": 5728, "complete": False, "failed": False, "objectives": [
            {"kind": "monster", "done": True, "have": 1, "need": 1}, {"kind": "monster", "done": False, "have": 0, "need": 1}]})
        self.assertEqual((reply["quests"][1]["complete"], reply["more"]), (True, False))

    def db(self):
        db = gamedb.connect(":memory:")
        gamedb.load_maps(db, UIMAP_CSV, UMA_CSV)
        gamedb.load_npcs(db, NPC_LUA, ZONES_LUA)
        gamedb.load_quests(db, QUEST_LUA)
        gamedb.load_items(db, ITEM_LUA)
        gamedb.load_objects(db, OBJECT_LUA, ZONES_LUA)
        return db

    def test_database(self):
        db = self.db()
        self.assertEqual(db.execute("SELECT name, level, req_level, text FROM quest WHERE id = 5728").fetchone()[:],
                         ("Hidden Enemies", 16, 13, "Kill Bazzalan and Jergosh."))
        self.assertEqual([tuple(r) for r in db.execute("SELECT kind, id, role FROM quest_giver WHERE quest = 5725")],
                         [("npc", 1, "start"), ("npc", 5, "end")])
        self.assertEqual([tuple(r) for r in db.execute("SELECT kind, id FROM item_source WHERE item = 900")],
                         [("npc", 2), ("npc", 3), ("vendor", 5)])
        self.assertEqual(db.execute("SELECT map, x FROM object_spawn WHERE object = 31").fetchone()[:], (1458, 50))

    def test_quest_log_names_the_ids(self):
        game = host.parse_reply(ask(host.build_quests())[0])["quests"]
        log = gamedb.quest_log(self.db(), game, {"map": 1458, "x": 0.49, "y": 0.18})
        hidden, books = log
        self.assertEqual(hidden["name"], "Hidden Enemies")
        self.assertEqual([(o["what"], o["have"], o["done"]) for o in hidden["objectives"]],
                         [("Far Trainer", 1, True), ("Jergosh the Invoker slain", 0, False)])
        self.assertEqual([g["name"] for g in hidden["turned_in_at"]], ["Aelthalyste"])
        self.assertEqual(books["objectives"][0]["from"], ["Far Trainer", "Alliance Priest", "Kalimdor Priest (vendor)"])
        self.assertEqual((books["complete"], books["turned_in_at"][0]["zone"]), (True, "Orgrimmar"))

    def test_quest_log_gives_every_turn_in_like_find_quests(self):
        # A quest taken in at an NPC and at an object: both, with the object's coordinates, the same
        # list find_quests gives (quest_givers).
        db = self.db()
        db.execute("INSERT INTO quest_giver VALUES (5728, 'object', 31, 'end')")
        game = host.parse_reply(ask(host.build_quests())[0])["quests"]
        state = {"map": 1458, "x": 0.49, "y": 0.18}
        hidden = gamedb.quest_log(db, game, state)[0]
        self.assertEqual([g["name"] for g in hidden["turned_in_at"]], ["Aelthalyste", "Old Lion Statue"])
        statue = hidden["turned_in_at"][1]
        self.assertEqual((statue["map_id"], statue["x"], statue["y"]), (1458, 50, 50))
        self.assertEqual(hidden["turned_in_at"], gamedb.quest_givers(db, 5728, "end", state))

    def test_cli(self):
        self.assertEqual(host._request_for(["quests"]), {"cmd": "quests"})


class CardMarkup(unittest.TestCase):
    def test_checklists_and_bullets(self):
        packet = host.build_show("card", "RFC", "[ ] Heart (Taragaman)\n[x] Satchel\n- note")
        lines, _ = Game.run("login() " + hello() + ' taps("%s") frame() ' % bitstr(codec.build(host.CHANNEL, 7, packet))
                            + 'print("TEXT", DeckAgentCard.text.text)')
        i = lines.index("TEXT\t|A:checkbox-minimal:14:14|a Heart (Taragaman)")
        self.assertEqual(lines[i + 1:i + 3], ["|A:Tracker-Check:14:14|a |cff9d9d9dSatchel|r", "  \u2022 note"])


DUNGEONS_LUA = """local isHorde = UnitFactionGroup("Player") == "Horde"
local dungeons = {
    [2437] = {"Ragefire Chasm",nil,1637,{{1637, 52.6, 49}}},
    [209] = {"Shadowfang Keep",{10014,10015},130,{{130, 44.8, 67.8}}},
}"""


class FindQuests(unittest.TestCase):
    def db(self):
        db = Quests.db(self)
        gamedb.load_areas(db, DUNGEONS_LUA, ZONES_LUA)
        return db

    def test_areas(self):
        db = self.db()
        self.assertEqual(sorted(tuple(r) for r in db.execute("SELECT id, name FROM area WHERE kind = 'dungeon'")),
                         [(209, "Shadowfang Keep"), (2437, "Ragefire Chasm"), (10014, "Shadowfang Keep"),
                          (10015, "Shadowfang Keep")])
        self.assertIn((1497, "Undercity"), [tuple(r) for r in db.execute("SELECT id, name FROM area WHERE kind = 'zone'")])

    def test_by_place_with_faction_and_prerequisites(self):
        found = gamedb.find_quests(self.db(), place="ragefire", faction="H")
        self.assertEqual([q["name"] for q in found], ["Hidden Enemies (part 1)", "Hidden Enemies"])  # level order
        self.assertEqual(found[1]["prerequisites"], [{"id": 5727, "name": "Hidden Enemies (part 1)"}])
        self.assertEqual(found[1]["filed_under"], "Ragefire Chasm")
        self.assertEqual(found[1]["givers"][0]["name"], "Aelthalyste")
        self.assertIn("Alliance Only", [q["name"] for q in gamedb.find_quests(self.db(), place="ragefire", faction="A")])
        self.assertEqual([q["id"] for q in gamedb.find_quests(self.db(), name="part 1")], [5727])
        self.assertEqual(gamedb.find_quests(self.db()), [])

    def test_game_status_is_a_number_per_id(self):
        packet = host.build_quest_status([5728, 5725, 1, 99])
        self.assertEqual(packet[:2], b"\x49\x04")
        setup = ('C_QuestLog.IsQuestFlaggedCompleted = function(id) return id == 1 end '
                 'C_QuestLog.IsOnQuest = function(id) return id == 5728 or id == 5725 end')
        data, _ = ask(packet, setup)
        self.assertEqual(data[3:], b"\x04\x01\x02\x03\x00")
        self.assertEqual(host.parse_reply(data)["status"], ["in log", "ready to turn in", "done", "not taken"])
        self.assertEqual(host._request_for(["quest-status", "5", "6"]), {"cmd": "quest-status", "ids": [5, 6]})


class CardItems(unittest.TestCase):
    def test_items_become_checklist_and_bullets(self):
        self.assertEqual(mcp_server.card_text([{"text": "Heart", "done": False}, {"text": "Satchel  ", "done": True},
                                               {"text": "Rahauro, Thunder Bluff"}], "From the quest log."),
                         "[ ] Heart\n[x] Satchel\n- Rahauro, Thunder Bluff\n\nFrom the quest log.")

    def test_text_cards_stay_paragraphs(self):
        self.assertEqual(mcp_server.card_text([], "Wowhead.", "Take the lift down.\n- not a bullet"),
                         "Take the lift down.\nnot a bullet\n\nWowhead.")


class Respond(unittest.TestCase):
    """The respond tool (her answer: what's said + one display) and the launcher reading it."""

    def call(self, **args):
        with mock.patch.object(mcp_server.common, "game", return_value={"ok": True, "result": "shown"}) as game:
            out = mcp_server.tool_respond(dict({"say": "Two left; they're on screen."}, **args))
        return out, game

    def test_displays(self):
        out, game = self.call(display="none")
        self.assertEqual((out["spoken"], game.called), ("Two left; they're on screen.", False))
        out, game = self.call(display="checklist", title="RFC", items=[{"text": "Satchel", "done": False}],
                              note="From the database.")
        self.assertEqual(game.call_args[0][1], "card")
        self.assertEqual(game.call_args[0][-1], "[ ] Satchel\n\nFrom the database.")
        _, game = self.call(display="list", title="Givers", items=[{"text": "Rahauro", "done": True}])
        self.assertEqual(game.call_args[0][-1], "- Rahauro")  # a list never shows checks
        _, game = self.call(display="text", title="How", text="Take the lift.")
        self.assertEqual(game.call_args[0][-1], "Take the lift.")
        self.assertNotIn("--seconds", game.call_args[0])  # how long it stays: the host side's card_seconds
        _, game = self.call(display="reading", title="Guide", text="# Boss\n- dodge")
        self.assertEqual(game.call_args[0][1], "reading")

    def test_refusals_she_can_fix(self):
        for args in ({"display": "checklist", "items": [{"text": "a"}]}, {"display": "list"},
                     {"display": "text"}, {"display": "none", "say": "x" * 161},
                     {"display": "list", "items": [{"text": "x" * 80}] * 12}):
            with self.assertRaises(common.GameError, msg=args):
                self.call(**args)

    def test_launcher_speaks_the_accepted_respond(self):
        import importlib.machinery, importlib.util, io
        path = os.path.join(HERE, "..", "agent", "deckagent")
        loader = importlib.machinery.SourceFileLoader("deckagent_launcher3", path)
        spec = importlib.util.spec_from_loader("deckagent_launcher3", loader)
        la = importlib.util.module_from_spec(spec)
        loader.exec_module(la)

        def use(i, say, heard=None):
            inp = {"say": say, "display": "none"}
            if heard:
                inp["heard"] = heard
            return {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": i, "name": "mcp__deckagent__respond", "input": inp}]}}

        def result(i, err):
            return {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": i, "content": "x", "is_error": err}]}}
        events = [use("a", "a long one"), result("a", True), use("b", "Short.", "rage fire is Ragefire"),
                  result("b", False), {"type": "result", "result": "Some trailing text."}]
        proc = mock.Mock(stdout=io.StringIO("".join(json.dumps(e) + "\n" for e in events)), returncode=0)
        with mock.patch.object(la.subprocess, "Popen", return_value=proc), \
                mock.patch.object(la, "save_session"), mock.patch.object(la, "write_transcript"):
            answer, rc = la.run_claude("q", "mcp.json", "sonnet", False)
        self.assertEqual((answer, rc), ("Short.\nHeard as: rage fire is Ragefire", 0))


WAYPAD = ('PTS = { { id = 3, mapID = 1454, x = 0.5, y = 0.25 }, { id = 7, mapID = 1456, x = 0.7, y = 0.3 } } ACT = 2 '
          'WayPad_GetPoints = function() return PTS, ACT end '
          'WayPad_Remove = function(id) for i, p in ipairs(PTS) do if p.id == id then table.remove(PTS, i) '
          'if ACT and ACT >= i then ACT = (#PTS > 0) and math.max(1, ACT - 1) or nil end return true end end '
          'return false end ')


class Waypoints(unittest.TestCase):
    def test_list(self):
        data, _ = ask(host.build_waypoints(), WAYPAD)
        reply = host.parse_reply(data)
        self.assertEqual(reply["removed"], 0)
        self.assertEqual(reply["waypoints"], [
            {"id": 3, "map": 1454, "x": 50.0, "y": 25.0, "active": False},
            {"id": 7, "map": 1456, "x": 70.0, "y": 30.0, "active": True}])

    def test_remove_by_id_active_and_all(self):
        reply = host.parse_reply(ask(host.build_waypoints("remove", 3), WAYPAD)[0])
        self.assertEqual((reply["removed"], [w["id"] for w in reply["waypoints"]]), (1, [7]))
        reply = host.parse_reply(ask(host.build_waypoints("remove-active"), WAYPAD)[0])
        self.assertEqual((reply["removed"], [w["id"] for w in reply["waypoints"]]), (1, [3]))
        reply = host.parse_reply(ask(host.build_waypoints("remove-all"), WAYPAD)[0])
        self.assertEqual((reply["removed"], reply["waypoints"]), (2, []))
        reply = host.parse_reply(ask(host.build_waypoints("remove", 99), WAYPAD)[0])
        self.assertEqual((reply["status"], reply["reason"]), ("refused", "no waypoint with id 99"))

    def test_cli(self):
        self.assertEqual(host._request_for(["waypoints", "remove", "7"]), {"cmd": "waypoints", "action": "remove", "id": 7})
        self.assertEqual(host._request_for(["waypoints", "remove-all"]), {"cmd": "waypoints", "action": "remove-all"})
        self.assertIsNone(host._request_for(["waypoints", "bogus"]))


CHECK_SETUP = ('BAGS = { [6948] = 1 } C_Item.GetItemCount = function(id) return BAGS[id] or 0 end '
               'PlaySound = function(kit) print("SOUND " .. kit) end UnitXP = function() return XPNOW or 100 end '
               'UnitXPMax = function() return 1000 end '
               'C_QuestLog.IsOnQuest = function(id) return id == 5728 or id == 5725 end '
               'C_QuestLog.IsQuestFlaggedCompleted = function(id) return id == 1 end ')
SHOWN = ('print("PANEL", tostring(DeckAgentChecklist and DeckAgentChecklist.shown)) '
         'for i, it in ipairs(DeckAgentDB.checklist and DeckAgentDB.checklist.items or {}) do '
         'print("ITEM", i, tostring(it.state)) end ')


class Checklists(unittest.TestCase):
    """CHECKLIST (addons/DeckAgent/Checklist.lua <-> decklink/agent.py) and track_checklist."""

    ITEMS = [{"label": "Pick up Hidden Enemies", "track": "quest_taken", "quest_id": 5728},
             {"label": "Bazzalan", "track": "objective", "quest_id": 5728, "objective": 2},
             {"label": "Hearthstone", "track": "item_count", "item_id": 6948, "count": 2},
             {"label": "Level 20", "track": "level", "level": 20},
             {"label": "Old quest done", "track": "quest_turned_in", "quest_id": 1},
             {"label": "Ask Edna", "track": "manual", "done": True},
             {"label": "Earn 500 XP", "track": "xp", "xp": 500}]

    def run_list(self, after=""):
        packet = host.build_checklist("RFC prep", self.ITEMS)
        frame = codec.build(host.CHANNEL, 7, packet)
        lines, s = Game.run("login() " + CHECK_SETUP + hello() + ' taps("%s") frame() ' % bitstr(frame)
                            + 'fireEvent("PLAYER_ENTERING_WORLD") ' + after + SHOWN)
        return host.parse_reply(codec.parse(s.out)[2]) if s and s.out else None, lines

    def test_set_says_what_is_done_already(self):
        reply, lines = self.run_list()
        # taken yes, objective 2 not finished, 1 of 2 hearthstones, level 20 vs 20, turned in yes, manual yes, xp 0
        self.assertEqual(reply["done"], [True, False, False, True, True, True, False])
        self.assertIn("PANEL\ttrue", lines)
        self.assertNotIn("SOUND 878", lines)  # the starting state ticks quietly

    def test_ticks_itself_with_a_sound(self):
        _, lines = self.run_list('BAGS[6948] = 2 fireEvent("BAG_UPDATE_DELAYED") fireTimers() '
                                 'XPNOW = 400 fireEvent("PLAYER_XP_UPDATE") XPNOW = 650 fireEvent("PLAYER_XP_UPDATE") '
                                 'fireTimers() ')
        self.assertIn("SOUND 878", lines)
        self.assertIn("ITEM\t3\ttrue", lines)  # 2 hearthstones now
        self.assertIn("ITEM\t7\ttrue", lines)  # 300 + 250 XP since it went up

    def test_clear_and_bad_items(self):
        lines, s = Game.run("login() " + CHECK_SETUP + hello() + ' taps("%s") frame() ' % bitstr(codec.build(
            host.CHANNEL, 7, host.build_checklist(clear=True))) + SHOWN)
        self.assertEqual(host.parse_reply(codec.parse(s.out)[2])["done"], [])
        with self.assertRaises(ValueError):
            host.build_checklist("x", [{"label": "a", "track": "objective", "quest_id": 1}])  # no objective
        with self.assertRaises(ValueError):
            host.build_checklist("x", [{"label": "a", "track": "dance"}])
        self.assertEqual(host._request_for(["checklist", "clear"]), {"cmd": "checklist", "clear": True})


HOOK_SETUP = CHECK_SETUP + 'login() fireEvent("PLAYER_ENTERING_WORLD") '


class GameHooks(unittest.TestCase):
    """HOOKS / EVENTS (addons/DeckAgent/Hooks.lua <-> decklink/agent.py) and DeckLink's attention flag."""

    def frames(self, *packets):
        return "".join(' taps("%s") frame() ' % bitstr(codec.build(host.CHANNEL, 7 + n, p))
                       for n, p in enumerate(packets))

    def test_add_and_already_true(self):
        data, _ = ask(host.build_hooks("add", {"track": "quest_turned_in", "quest_id": 79192}, 30), HOOK_SETUP)
        self.assertEqual((host.parse_reply(data)["id"], host.parse_reply(data)["already"]), (1, False))
        data, _ = ask(host.build_hooks("add", {"track": "quest_taken", "quest_id": 5728}), HOOK_SETUP)
        self.assertTrue(host.parse_reply(data)["already"])  # already on it: not set, act now
        lines, _ = Game.run(HOOK_SETUP + hello() + self.frames(host.build_hooks("add", {"track": "level", "level": 30}, 30))
                            + 'for _, h in ipairs(DeckAgentDB.hooks.list) do print("HOOK", h.id, h.kind, h.a) end')
        self.assertIn("HOOK\t1\t6\t30", lines)
        with self.assertRaises(ValueError):
            host.build_hooks("add", {"track": "manual"})

    def test_list_and_remove_replies(self):
        setup = HOOK_SETUP + ('DeckAgentDB.hooks = { nextId = 3, events = {}, list = { '
                              '{ id = 1, kind = 6, a = 30, base = 0, expires = SERVERTIME + 600 }, '
                              '{ id = 2, kind = 3, a = 79192, base = 0, expires = SERVERTIME + 120 } } } ')
        reply = host.parse_reply(ask(host.build_hooks("list"), setup)[0])
        self.assertEqual(reply["hooks"], [{"id": 1, "track": "level", "not": False, "minutes_left": 10},
                                          {"id": 2, "track": "quest_turned_in", "not": False, "minutes_left": 2}])
        self.assertEqual(host.parse_reply(ask(host.build_hooks("remove", hid=2), setup)[0])["removed"], 1)
        self.assertEqual(host.parse_reply(ask(host.build_hooks("remove-all"), setup)[0])["removed"], 2)
        empty = host.parse_reply(ask(host.build_hooks("list"), HOOK_SETUP)[0])
        self.assertEqual(empty["hooks"], [])  # an empty list isn't mistaken for a remove reply

    def test_fires_raises_attention_and_events_collect_it(self):
        add = host.build_hooks("add", {"track": "item_count", "item_id": 6948, "count": 2})
        lines, s = Game.run(HOOK_SETUP + hello() + self.frames(add) + 'BAGS[6948] = 2 fireEvent("BAG_UPDATE_DELAYED") '
                            'fireTimers() frame() print("EVENTS", #DeckAgentDB.hooks.events, #DeckAgentDB.hooks.list) ')
        self.assertIn("EVENTS\t1\t0", lines)  # fired once, then gone
        self.assertTrue(s.flags & strip.FLAG_ATTENTION)
        queued = (HOOK_SETUP + 'DeckAgentDB.hooks = { nextId = 2, list = {}, '
                  'events = { { id = 1, kind = 5, at = SERVERTIME - 12 } } } ')
        data, _ = ask(host.build_events(), queued)
        self.assertEqual(host.parse_reply(data)["events"], [{"id": 1, "track": "item_count", "not": False, "seconds_ago": 12}])

    def test_attention_shows_the_strip_with_the_link_closed(self):
        lines, s = Game.run('login() DeckLink.SetAttention("DeckAgent", true) frame()')
        self.assertIsNotNone(s)
        self.assertEqual(s.flags & (strip.FLAG_ATTENTION | strip.FLAG_OPEN), strip.FLAG_ATTENTION)
        lines, s = Game.run('login() DeckLink.SetAttention("DeckAgent", true) frame() '
                            'DeckLink.SetAttention("DeckAgent", false) frame()')
        self.assertIsNone(s)  # nothing waiting, link closed: no strip

    def test_expired_hooks_go_quietly(self):
        lines, _ = Game.run(HOOK_SETUP + hello() + self.frames(host.build_hooks("add", {"track": "level", "level": 30}, 1))
                            + 'SERVERTIME = SERVERTIME + 61 for _, fn in ipairs(TICKERS) do fn() end '
                            'print("LEFT", #DeckAgentDB.hooks.list, #DeckAgentDB.hooks.events)')
        self.assertIn("LEFT\t0\t0", lines)


class DeckHooks(unittest.TestCase):
    """decklink/hooks.py and the link keeper's glance and dispatch (agentd.py)."""

    def setUp(self):
        from decklink import hooks
        self.hooks_mod = hooks
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [1000.0]
        self.store = hooks.Hooks(os.path.join(self.tmp.name, "hooks.json"), clock=lambda: self.now[0])

    def test_registry_survives_and_timers_fire(self):
        self.store.add_game(3, "Set the waypoint to Pocket Litter.", "wake", "Stepping Stones turned in", 30)
        tid = self.store.add_timer(20, "Check the auction house.", "say")
        again = self.hooks_mod.Hooks(self.store.path, clock=lambda: self.now[0])
        self.assertTrue(again.any_game())
        self.assertEqual(again.due_timers(), [])
        self.now[0] += 20 * 60
        self.assertEqual([t for t, _ in again.due_timers()], [tid])
        self.now[0] += 31 * 60
        self.assertEqual(again.expire(), ["3"])
        self.assertIn("Stepping Stones turned in", self.hooks_mod.wake_prompt("Stepping Stones turned in", "next"))

    def test_saves_from_many_threads_keep_every_hook(self):
        # Requests and the glance save from their own threads: each save its own temporary file, the
        # state under the store's lock, so none fails and none is lost.
        import threading
        errors = []

        def add(n):
            try:
                for k in range(10):
                    self.store.add_timer(5, "note %d-%d" % (n, k), "say")
            except Exception as e:  # noqa: BLE001 - any failure is the bug
                errors.append(e)
        threads = [threading.Thread(target=add, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        again = self.hooks_mod.Hooks(self.store.path, clock=lambda: self.now[0])
        self.assertEqual(len(again.data["timers"]), 80)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["hooks.json"])  # no temporary file left

    def test_a_hook_is_set_and_its_note_saved_under_the_keeper_lock(self):
        from decklink import agentd
        k = agentd.Keeper(25, make_link=lambda: mock.Mock(), hook_store=self.store)
        held = []
        real = self.store.add_game
        self.store.add_game = lambda *a, **kw: held.append(k.lock.locked()) or real(*a, **kw)
        with mock.patch.object(agentd.agent, "hooks", return_value={"ok": True, "id": 5, "already": False}):
            reply = k.handle({"cmd": "hook", "op": "add", "when": {"track": "level", "level": 20}, "note": "n",
                              "action": "say"})
        self.assertTrue(reply["ok"])
        self.assertEqual(held, [True])  # the glance can't collect hook 5 before its note is saved

    def test_a_hook_note_goes_only_once_the_game_removed_the_hook(self):
        from decklink import agentd
        self.store.add_game(3, "Set the waypoint.", "wake", "d", 30)
        self.store.add_game(4, "Train.", "say", "level 20 reached", 30)
        k = agentd.Keeper(25, make_link=lambda: mock.Mock(), hook_store=self.store)
        lost = agentd.link.LinkError("frame not received")
        with mock.patch.object(agentd.agent, "hooks", side_effect=lost):
            self.assertFalse(k.handle({"cmd": "hook", "op": "remove", "id": "3"})["ok"])
            self.assertFalse(k.handle({"cmd": "hook", "op": "clear"})["ok"])
        self.assertEqual(sorted(self.store.data["game"]), ["3", "4"])  # still set in the game: notes kept
        with mock.patch.object(agentd.agent, "hooks", return_value={"ok": True, "removed": 1}):
            self.assertTrue(k.handle({"cmd": "hook", "op": "remove", "id": "3"})["ok"])
        self.assertEqual(sorted(self.store.data["game"]), ["4"])
        # A removal whose answer was lost leaves a stale note: the next list, from the game's own, drops it.
        with mock.patch.object(agentd.agent, "hooks", return_value={"ok": True, "hooks": []}), \
                mock.patch.object(agentd.agent, "events", return_value={"ok": True, "events": []}):
            self.assertEqual(k.handle({"cmd": "hook", "op": "list"})["hooks"], [])
        self.assertFalse(self.store.any_game())

    def test_a_list_collects_fired_hooks_before_pruning(self):
        # Hook 4 fired: it's off the game's list but its event waits. The list takes the event (and the
        # note) first, so pruning can't lose it, and the next glance hands it to the voice.
        from decklink import agentd
        self.store.add_game(4, "Train.", "say", "level 20 reached", 30)
        k = agentd.Keeper(25, make_link=lambda: mock.Mock(), hook_store=self.store)
        with mock.patch.object(agentd.agent, "hooks", return_value={"ok": True, "hooks": []}), \
                mock.patch.object(agentd.agent, "events",
                                  return_value={"ok": True, "events": [{"id": 4, "seconds_ago": 2}]}):
            k.handle({"cmd": "hook", "op": "list"})
        fired = k.glance()
        self.assertEqual([e["note"] for e, _ in fired], ["Train."])

    def test_a_hook_change_dropped_while_waiting_for_the_lock_does_nothing(self):
        from decklink import agentd
        k = agentd.Keeper(25, make_link=lambda: mock.Mock(), hook_store=self.store)
        answers = iter([False, True])  # not yet dropped when it arrives; dropped once it has the lock
        with mock.patch.object(agentd.agent, "hooks") as hooks_call:
            reply = k.hook({"op": "clear"}, cancelled=lambda: next(answers))
        self.assertTrue(reply.get("cancelled"))
        hooks_call.assert_not_called()

    def test_glance_collects_and_dispatches(self):
        from decklink import agentd

        class Lk:
            status = strip.Status(strip.FLAG_OPEN | strip.FLAG_ATTENTION, 1, 0, 0, 0, None)

            def read(self):
                return self.status
        self.store.add_game(3, "Set the waypoint to Pocket Litter.", "wake", "Stepping Stones turned in", 30)
        self.store.add_game(4, "Train your new spells.", "say", "level 20 reached", 30)
        k = agentd.Keeper(25, hook_store=self.store)
        k.lk = Lk()
        with mock.patch.object(agentd.agent, "events", return_value={"ok": True, "events": [
                {"id": 3, "track": "quest_turned_in", "seconds_ago": 2}, {"id": 4, "track": "level", "seconds_ago": 1},
                {"id": 9, "track": "zone", "seconds_ago": 1}]}):
            fired = k.glance()
        self.assertEqual([e["action"] for e, _ in fired], ["wake", "say"])  # 9 has no note: skipped
        self.assertFalse(self.store.any_game())
        k.voice = mock.Mock()
        k.dispatch(fired)
        k.voice.wake.assert_called_once()
        self.assertIn("Pocket Litter", k.voice.wake.call_args[0][0])
        k.voice.remind.assert_called_once_with("Train your new spells.", "Hook: level 20 reached")

    def test_glance_does_nothing_without_attention_or_hooks(self):
        from decklink import agentd
        k = agentd.Keeper(25, hook_store=self.store)
        self.assertEqual(k.glance(), [])  # no hooks: doesn't even look
        self.store.add_game(3, "n", "wake", "d", 30)
        k.lk = mock.Mock(read=mock.Mock(return_value=strip.Status(strip.FLAG_OPEN, 1, 0, 0, 0, None)))
        with mock.patch.object(agentd.agent, "events") as events:
            self.assertEqual(k.glance(), [])
        events.assert_not_called()

    def test_wake_waits_for_a_busy_question(self):
        from decklink import voice
        started = []
        v = voice.Voice(indicator=mock.Mock(), run=mock.Mock(), say=mock.Mock())
        v.busy = True
        with mock.patch("threading.Thread", lambda target, args=(), daemon=None: mock.Mock(
                start=lambda: started.append(args))):
            v.wake("[A hook you set fired: x]", "Hook: x")
            self.assertEqual((started, v.waiting), ([], [("[A hook you set fired: x]", "Hook: x")]))
            v.busy = False
            v.wake("second", "Hook: y")
            self.assertEqual(started, [("second", 0, "Hook: y")])

    def test_cli_and_describe(self):
        self.assertEqual(host._request_for(["hook", "add", '{"when": {"track": "level", "level": 20}, "note": "n", '
                                                            '"action": "say"}']),
                         {"cmd": "hook", "op": "add", "when": {"track": "level", "level": 20}, "note": "n", "action": "say"})
        self.assertEqual(host._request_for(["hook", "remove", "t2"]), {"cmd": "hook", "op": "remove", "id": "t2"})
        with mock.patch.object(mcp_server.common, "DB_PATH", "/nonexistent.db"):
            self.assertEqual(mcp_server.describe({"track": "level", "level": 20}), "level 20 reached")
            self.assertEqual(mcp_server.describe({"track": "timer", "minutes": 20}), "20 minutes passed")


class NotConditions(unittest.TestCase):
    def test_left_a_zone_fires_but_a_loading_screen_does_not(self):
        add = host.build_hooks("add", {"track": "zone", "map_id": 1458, "not": True})
        self.assertEqual(add[2], 7 | 0x80)
        base = HOOK_SETUP + hello() + ' taps("%s") frame() ' % bitstr(codec.build(host.CHANNEL, 7, add))
        lines, _ = Game.run(base + 'PLAYER.map = nil fireEvent("ZONE_CHANGED") fireTimers() '
                            'print("LOADING", #DeckAgentDB.hooks.events) PLAYER.map = 1454 fireEvent("ZONE_CHANGED") '
                            'fireTimers() print("LEFT", #DeckAgentDB.hooks.events)')
        self.assertIn("LOADING\t0", lines)  # no map: can't tell, not "left"
        self.assertIn("LEFT\t1", lines)
        reply = host.parse_reply(ask(add, HOOK_SETUP + "PLAYER.map = 1454")[0])
        self.assertTrue(reply["already"])  # already out of Undercity
        with self.assertRaises(ValueError):
            host.build_hooks("add", {"track": "manual", "not": True})
        with mock.patch.object(mcp_server.common, "DB_PATH", "/nonexistent.db"):
            self.assertEqual(mcp_server.describe({"track": "zone", "map_id": 1454, "not": True}), "left #1454")


class Inventory(unittest.TestCase):
    """CHARACTER / BAGS / ITEM_INFO (addons/DeckAgent/Inventory.lua) and the target field."""

    def test_target(self):
        setup = ('TARGET = { guid = "Creature-0-4372-1-123-11519-00004A1B2C", level = 16, class = "elite", '
                 'attackable = true, hp = 150, max = 300 }')
        reply = host.parse_reply(ask(host.build_state(["target"]), setup)[0], ["target"])
        self.assertEqual(reply["target"], {"npc_id": 11519, "level": 16, "attackable": True, "dead": False,
                                           "elite": True, "rare": False, "player": False, "tapped_by_others": False,
                                           "health_pct": 50})
        reply = host.parse_reply(ask(host.build_state(["target"]), 'TARGET = { guid = "Player-4372-0ABC", '
                                     'player = true, level = 14 }')[0], ["target"])
        self.assertEqual((reply["target"]["npc_id"], reply["target"]["player"]), (None, True))
        self.assertIsNone(host.parse_reply(ask(host.build_state(["target"]))[0], ["target"])["target"])

    def test_character(self):
        reply = host.parse_reply(ask(host.build_character())[0])
        self.assertEqual((reply["level"], reply["money_copper"], reply["spec_id"]), (20, 123456, 1487))
        self.assertEqual((reply["bag_free"], reply["bag_slots"]), (2, 5))
        self.assertEqual(reply["stats"]["intellect"], 24)
        self.assertEqual((reply["stats"]["armor"], reply["stats"]["spell_power"], reply["stats"]["healing"]), (95, 9, 12))
        self.assertEqual(reply["equipped"], [
            {"slot": "chest", "item_id": 16607, "enchant_id": None, "suffix_id": None, "durability_pct": 75},
            {"slot": "main_hand", "item_id": 277247, "enchant_id": 15, "suffix_id": -3, "durability_pct": None}])

    def test_equipped_suffix_is_link_field_7_and_changes_the_stats(self):
        # The Mug's link (harness EQUIPPED) is item:277247:15:::::-3:14: suffix -3, then the copy's own ID
        # 14. The suffix, asked back through ITEM_INFO (signed), brings its stamina; the base item has none.
        mug = host.parse_reply(ask(host.build_character())[0])["equipped"][1]
        self.assertEqual((mug["item_id"], mug["suffix_id"]), (277247, -3))
        with_suffix, base = host.parse_reply(ask(host.build_item_info([(277247, mug["suffix_id"]), 277247]))[0])["items"]
        self.assertEqual(with_suffix["stats"], {"dps": 9.5, "stamina": 4})
        self.assertEqual(base["stats"], {"dps": 9.5})

    def test_bags_and_bank(self):
        reply = host.parse_reply(ask(host.build_bags())[0])
        self.assertEqual(reply["items"], [{"item_id": 6948, "count": 1}, {"item_id": 2589, "count": 20}])
        self.assertEqual((reply["free"], reply["slots"], reply["more"]), (2, 5, False))
        reply = host.parse_reply(ask(host.build_bags(bank=True))[0])
        self.assertTrue(reply["bank_never_seen"])
        reply = host.parse_reply(ask(host.build_bags(bank=True), 'BAGSLOTS[-1] = { { itemID = 4306, stackCount = 40 } } '
                                     'fireEvent("BANKFRAME_OPENED") fireEvent("BANKFRAME_CLOSED") SERVERTIME = SERVERTIME + 600')[0])
        self.assertEqual((reply["items"], reply["minutes_old"]), ([{"item_id": 4306, "count": 40}], 10))

    def test_item_info_is_exact_and_paced(self):
        reply = host.parse_reply(ask(host.build_item_info([19019, 5000, 1]))[0])
        tf, helm, bad = reply["items"]
        self.assertEqual((tf["quality"], tf["item_level"], tf["slot"]), ("legendary", 80, "one-hand"))
        self.assertEqual(tf["stats"], {"agility": 5, "dps": 41.8, "fire_resistance": 8})
        self.assertEqual((helm["status"], bad["status"]), ("loading", "no such item"))
        # Not cached: one paced server request, and only once however often it's asked.
        many = [5000 + n for n in range(14)]
        lines, _ = Game.run("login() " + "".join("ITEMS[%d] = { cached = false, loc = 'INVTYPE_HEAD' } " % n for n in many)
                            + hello() + ' taps("%s") frame() ' % bitstr(codec.build(host.CHANNEL, 7, host.build_item_info(many)))
                            + ' taps("%s") frame() ' % bitstr(codec.build(host.CHANNEL, 8, host.build_item_info(many)))
                            + 'print("LOADS", #LOADS)')
        self.assertIn("LOADS\t10", lines)  # 14 unknown items, twice: 10 requests (the bucket), none repeated

    def test_cli(self):
        self.assertEqual(host._request_for(["item-info", "19019", "1234:-7"]), {"cmd": "item-info", "items": [[19019, 0], [1234, -7]]})
        self.assertEqual(host._request_for(["bags", "bank"]), {"cmd": "bags", "bank": True})


class ItemLookups(unittest.TestCase):
    """agent/items.py and mcp_server.lookup_items: remembered, valid IDs only, paced retries."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(mcp_server.items, "PATH", os.path.join(self.tmp.name, "items.db"))
        p.start()
        self.addCleanup(p.stop)

    def test_remembers_and_skips_unknown_ids(self):
        replies = [{"ok": True, "items": [{"item_id": 19019, "status": "ok", "stats": {"agility": 5}},
                                          {"item_id": 5000, "status": "loading"}]},
                   {"ok": True, "items": [{"item_id": 5000, "status": "ok", "stats": {}}]}]
        with mock.patch.object(mcp_server.common, "game", side_effect=replies) as game:
            out = mcp_server.lookup_items([(19019, 0), (5000, 0), (424242, 0)], known={19019: "TF", 5000: "Helm"},
                                          sleep=lambda s: None)
        self.assertEqual(game.call_count, 2)
        self.assertEqual(game.call_args_list[0][0], ("item-info", "19019:0", "5000:0"))  # never 424242
        self.assertEqual(game.call_args_list[1][0], ("item-info", "5000:0"))  # only what was still loading
        self.assertIn("not in the database", out[(424242, 0)]["status"])
        with mock.patch.object(mcp_server.common, "game") as game:  # second time: from memory, no game at all
            again = mcp_server.lookup_items([(19019, 0)], known={19019: "TF"})
        game.assert_not_called()
        self.assertEqual(again[(19019, 0)]["stats"], {"agility": 5})

    def test_gives_up_politely(self):
        with mock.patch.object(mcp_server.common, "game", return_value={"ok": True, "items": [
                {"item_id": 5000, "status": "loading"}]}) as game:
            out = mcp_server.lookup_items([(5000, 0)], sleep=lambda s: None, tries=3)
        self.assertEqual(game.call_count, 3)
        self.assertIn("still loading", out[(5000, 0)]["status"])

    def test_target_line_names_it(self):
        db_path = os.path.join(self.tmp.name, "g.db")
        db = gamedb.connect(db_path)
        gamedb.load_maps(db, UIMAP_CSV, UMA_CSV)
        gamedb.load_npcs(db, NPC_LUA, ZONES_LUA)
        gamedb.load_items(db, ITEM_LUA)
        db.commit()
        db.close()
        t = mcp_server.target_line({"npc_id": 2, "level": 30, "elite": True}, db_path)
        self.assertEqual((t["name"], t["title"], t["quest_items_it_drops"]), ("Far Trainer", "Priest Trainer", ["Spells of Shadow"]))
        self.assertIsNone(mcp_server.target_line(None))


class Prices(unittest.TestCase):
    """PRICES (Inventory.lua, from MarketDeck_GetPrice) and get_prices / get_bags' price views."""

    STUB = ('MarketDeck_GetPrice = function(id) if id == 2589 then return { now = 1200, quantity = 40, market = 1000, '
            'days = 9, scanned = SERVERTIME - 7200, vendor = 13 } elseif id == 12299 then return { vendor = 16, '
            'disenchant = 900 } end end ')

    def test_game_reply(self):
        reply = host.parse_reply(ask(host.build_prices([2589, 12299, 6948]), self.STUB)[0])
        linen, gloves, hearth = reply["prices"]
        self.assertTrue(reply["market_installed"])
        self.assertEqual((linen["auction_now"], linen["quantity_up"], linen["market"], linen["days_of_history"],
                          linen["hours_since_scan"], linen["vendor"]), (1200, 40, 1000, 9, 2, 13))
        self.assertEqual((gloves["vendor"], gloves["disenchant"], gloves["hours_since_scan"]), (16, 900, None))
        self.assertIsNone(hearth["vendor"])
        none = host.parse_reply(ask(host.build_prices([2589]))[0])
        self.assertFalse(none["market_installed"])

    def test_best_way_to_sell(self):
        v = mcp_server.price_view({"auction_now": 1200, "quantity_up": 40, "market": 1000, "days_of_history": 9,
                                   "vendor": 13, "disenchant": None})
        self.assertEqual((v["auction_now"], v["auction_market"], v["vendor"], v["best"]),
                         ("0g 12s 0c (40 up)", "0g 10s 0c (9 days)", "0g 0s 13c", "auction house"))
        v = mcp_server.price_view({"vendor": 16, "disenchant": 900, "days_of_history": 0, "quantity_up": 0})
        self.assertEqual(v["best"], "disenchant")
        v = mcp_server.price_view({"market": 20, "days_of_history": 3, "vendor": 20, "quantity_up": 0})
        self.assertEqual(v["best"], "vendor")  # 20 at auction is 19 after the 5% cut

    def test_not_installed(self):
        with mock.patch.object(mcp_server.common, "game", return_value={"ok": True, "market_installed": False, "prices": []}):
            self.assertIn("MarketDeck", mcp_server.tool_get_prices({"item_ids": [2589]})["note"])

    def test_get_prices_asks_the_game_once(self):
        reply = host.parse_reply(ask(host.build_prices([2589, 12299]), self.STUB)[0])
        with mock.patch.object(mcp_server.common, "game", return_value=reply) as game, \
                mock.patch.object(mcp_server.common, "DB_PATH", "/nonexistent.db"):
            out = mcp_server.tool_get_prices({"item_ids": [2589, 12299]})
        self.assertEqual(game.call_count, 1)
        self.assertEqual([i["best"] for i in out["items"]], ["auction house", "disenchant"])
        self.assertNotIn("note", out)  # the newest scan (2 hours) is fresh


class Marks(unittest.TestCase):
    """MARKS (Inventory.lua -> PackPad_SetMarks) and find_items / flag_items / clear_flags."""

    STUB = ('MARKS = {} PackPad_SetMarks = function(src, ids, label) MARKS[src] = { label = label, ids = ids } return true end '
            'PackPad_ClearMarks = function(src) if src then MARKS[src] = nil else MARKS = {} end end '
            'PackPad_GetMarks = function() return MARKS end C_Item.GetItemCount = function(id) return id == 2589 and 20 or 0 end ')

    def test_mark_and_clear(self):
        lines, s = Game.run("login() " + self.STUB + hello() + ' taps("%s") frame() ' % bitstr(codec.build(
            host.CHANNEL, 7, host.build_marks("Red Linen Bag", [2589, 2320]))) + 'for k, m in pairs(MARKS) do print("MARK", k, m.label, #m.ids) end')
        reply = host.parse_reply(codec.parse(s.out)[2])
        self.assertEqual((reply["carried"], reply["packpad_installed"]), ([20, 0], True))
        self.assertIn("MARK\tDeckAgent:Red Linen Bag\tRed Linen Bag\t2", lines)
        lines, _ = Game.run("login() " + self.STUB + 'MARKS["DeckAgent:a"] = {} MARKS["DeckAgent:b"] = {} MARKS["Other"] = {} '
                            + hello() + ' taps("%s") frame() ' % bitstr(codec.build(host.CHANNEL, 7, host.build_marks("", clear=True)))
                            + 'for k in pairs(MARKS) do print("LEFT", k) end')
        self.assertEqual([l for l in lines if l.startswith("LEFT")], ["LEFT\tOther"])  # only DeckAgent's
        with self.assertRaises(ValueError):
            host.build_marks("", [1])

    def test_without_packpad(self):
        reply = host.parse_reply(ask(host.build_marks("x", [2589]))[0])
        self.assertFalse(reply["packpad_installed"])

    def test_find_items(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "g.db")
        db = gamedb.connect(path)
        gamedb.load_items(db, ITEM_LUA.replace("}]]", "[2996] = {'Bolt of Linen Cloth'},[2589] = {'Linen Cloth'},}]]"))
        db.commit()
        db.close()
        with mock.patch.object(mcp_server.common, "DB_PATH", path):
            out = mcp_server.tool_find_items({"name": "linen cloth"})
        self.assertEqual([i["name"] for i in out["items"]], ["Linen Cloth", "Bolt of Linen Cloth"])  # exact first
