"""Read executable lines from chunks compiled locally by the pinned Lua 5.4 runtime.

The public input is UTF-8 source. No user bytecode is loaded or executed.
The layout follows Lua 5.4 ldump.c, including child prototypes and absolute line anchors.
"""
from io import BytesIO

from ..storage.errors import StorageError


def executable_lines(source):
    from lupa.lua54 import LuaRuntime, LuaSyntaxError
    runtime = LuaRuntime(encoding=None, register_eval=False, register_builtins=False)
    try:
        chunk = runtime.compile(source.encode("utf-8"), mode=b"t")
    except LuaSyntaxError as exc:
        raise StorageError("SCRIPT_SYNTAX_ERROR", str(exc)) from exc
    stream = BytesIO(runtime.eval(b"string.dump")(chunk))
    if stream.read(5) != b"\x1bLua\x54":
        raise StorageError("ENGINE_UNAVAILABLE", "调试器需要 Lua 5.4")
    stream.read(7)  # format and LUAC_DATA
    instruction, integer, number = stream.read(3)
    stream.read(integer + number + 1)
    lines = set()

    def size():
        value = 0
        while True:
            byte = stream.read(1)[0]
            value = (value << 7) | (byte & 127)
            if byte & 128:
                return value

    def string():
        stream.read(max(0, size() - 1))

    def prototype():
        string()
        line = size()
        size()  # last line defined
        _, vararg, _ = stream.read(3)
        stream.read(size() * instruction)
        for _ in range(size()):
            tag = stream.read(1)[0]
            if tag in (4, 20):
                string()
            elif tag in (3, 19):
                stream.read(integer if tag == 3 else number)
        stream.read(3 * size())
        for _ in range(size()):
            prototype()
        deltas = stream.read(size())
        anchors = {size(): size() for _ in range(size())}
        for pc, delta in enumerate(deltas):
            line = anchors[pc] if delta == 128 else line + (delta if delta < 128 else delta - 256)
            if line > 0 and (pc or not vararg):
                lines.add(line)
        for _ in range(size()):
            string()
            size()
            size()
        for _ in range(size()):
            string()

    prototype()
    return lines
