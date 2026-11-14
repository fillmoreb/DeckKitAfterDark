"""DeckAgent tests: the game side (addons/DeckAgent, through test/harness.lua), the host side
(deck-setup/decklink/agent.py) and the agent itself (agent/: Lua data reader, game database, MCP server).

    python3 -m unittest discover -s test
"""
import json, os, sqlite3, struct, sys, tempfile, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "deck-setup"))
sys.path.insert(0, os.path.join(HERE, ".."))
from decklink import agent as host, codec  # noqa: E402
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
        self.assertEqual(host.build_state(), b"\x41\x1f")

    def test_state_all_fields(self):
        data, _ = ask(host.build_state())
        self.assertEqual(data[:4], b"\x41\x07\x00\x1f")  # op, seq, ok, every field given
        self.assertEqual(host.parse_reply(data), {
            "op": 1, "seq": 7, "ok": True, "status": "ok", "fields": host.FIELDS, "map": 1458, "x": 49.0, "y": 18.3, "cont": 0,
            "wx": 1636, "wy": 240, "zone": "Undercity", "faction": "H", "level": 20, "class": "PRIEST",
            "in_combat": False})

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
        for packet, status in ((b"\x45", "unknown op"), (b"\x81\x01", "unknown version"), (b"", "unknown version"),
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
        self.assertEqual(names, ["get_player_state", "find_npc", "set_waypoint"])
        self.assertEqual(mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"], -32601)

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
