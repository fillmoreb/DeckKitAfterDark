"""Reads Lua table literals (the data files QuestieDB ships) without running them.

    loads('return {1, "two", nil}')      ->  [1, "two", None]
    loads('return {[12] = {{49.8, 39.5}}}')  ->  {12: [[49.8, 39.5]]}

A table with only positional entries becomes a list (nil holes become None). A table with any
explicit key ([k] = v or name = v) becomes a dict; its positional entries keep their 1-based index
as the key. Only literals: strings, numbers, true/false/nil, tables. Anything else is an error.
"""
import re

_TOKEN = re.compile(r"""
    \s+ | --\[\[.*?\]\] | --[^\n]*                       # whitespace, comments (skipped)
  | (?P<str>'(?:[^'\\]|\\.)*' | "(?:[^"\\]|\\.)*")
  | (?P<num>-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|-?0[xX][0-9a-fA-F]+)
  | (?P<name>[A-Za-z_]\w*)
  | (?P<punct>[{}\[\]=,;])
""", re.S | re.X)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "\n": "\n", "a": "\a", "b": "\b",
            "f": "\f", "v": "\v"}


class LuaDataError(ValueError):
    pass


def _unquote(s):
    body = s[1:-1]

    def esc(m):
        c = m.group(1)
        if c[0].isdigit():
            return chr(int(c))
        return _ESCAPES.get(c, c)
    return re.sub(r"\\(\d{1,3}|.)", esc, body, flags=re.S)


def _tokens(text):
    pos, n = 0, len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m:
            raise LuaDataError("unexpected %r at %d" % (text[pos:pos + 20], pos))
        pos = m.end()
        kind = m.lastgroup
        if kind:
            yield kind, m.group(kind)


def loads(text):
    """The value of a Lua chunk that is `return <literal>` or just `<literal>`."""
    toks = list(_tokens(text))
    i = 0
    if toks and toks[0] == ("name", "return"):
        i = 1
    value, i = _value(toks, i)
    if i != len(toks):
        raise LuaDataError("trailing tokens after the value: %r" % (toks[i:i + 3],))
    return value


def _value(toks, i):
    kind, tok = toks[i]
    if kind == "str":
        return _unquote(tok), i + 1
    if kind == "num":
        v = float.fromhex(tok) if "x" in tok.lower() else float(tok)
        if "x" in tok.lower() or re.fullmatch(r"-?\d+", tok):
            v = int(int(tok, 0) if "x" in tok.lower() else tok)
        return v, i + 1
    if kind == "name":
        if tok in ("nil", "true", "false"):
            return {"nil": None, "true": True, "false": False}[tok], i + 1
        raise LuaDataError("not a literal: %s" % tok)
    if tok == "{":
        return _table(toks, i + 1)
    raise LuaDataError("unexpected %r" % tok)


def _table(toks, i):
    positional, keyed = [], {}
    while True:
        kind, tok = toks[i]
        if tok == "}":
            i += 1
            break
        if tok == "[":
            key, i = _value(toks, i + 1)
            if toks[i][1] != "]" or toks[i + 1][1] != "=":
                raise LuaDataError("bad [key] = entry")
            keyed[key], i = _value(toks, i + 2)
        elif kind == "name" and toks[i + 1][1] == "=" and tok not in ("nil", "true", "false"):
            keyed[tok], i = _value(toks, i + 2)
        else:
            v, i = _value(toks, i)
            positional.append(v)
        if toks[i][1] in (",", ";"):
            i += 1
        elif toks[i][1] != "}":
            raise LuaDataError("expected , or } but got %r" % toks[i][1])
    if not keyed:
        return positional, i
    for n, v in enumerate(positional, 1):
        keyed.setdefault(n, v)
    return keyed, i


def blocks(text):
    """QuestieDB files hold their data as `Name.field = [[return {...}]]`: {field: chunk text}."""
    return {m.group(1): m.group(2) for m in re.finditer(r"([\w.]+)\s*=\s*\[\[(.*?)\]\]", text, re.S)}


_ROW = re.compile(r"^[A-Za-z_]\w*\[(\d+)\]\s*=\s*(\{.*\})\s*$", re.M)


def rows(text):
    """QuestieDB's export (tools/export-forever.lua) writes one entity per line, `quest[2]={...}`:
    {id: row}. Comments and the `local quest = {}` / `return quest` lines around them are skipped."""
    return {int(m.group(1)): loads(m.group(2)) for m in _ROW.finditer(text)}


def entities(text, block):
    """The entities in a QuestieDB file, either form: the raw data files' `block = [[return {...}]]`
    or the export's one-line rows."""
    found = blocks(text)
    if block in found:
        return loads(found[block])
    return rows(text)
