# language: python, file: bot.py, target: Linux VPS, python-telegram-bot
# Forest & Ash — a small text-adventure bot for Telegram.
# The world is defined in levels/*.json; this file is the engine.
# Levels are sha256-verified, size-capped, and cached locally.
# Player state persists in SQLite across restarts.

import os
import re
import json
import hashlib
import sqlite3
import logging
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
MANIFEST_URL = os.environ.get(
    "MANIFEST_URL",
    "https://raw.githubusercontent.com/USER/forest-and-ash/main/manifest.json",
)
BASE_DIR  = Path.home() / ".local" / "share" / "forest-and-ash"
LEVEL_DIR = BASE_DIR / "levels"
DB_PATH   = BASE_DIR / "state.sqlite3"
MAX_BYTES = 512 * 1024

BASE_DIR.mkdir(parents=True, exist_ok=True)
LEVEL_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("forest-and-ash")


# ============ condition language ============
# Small hand-written expression parser used by level descriptions to
# branch on player state. Grammar:
#
#   expr   := or
#   or     := and ("or" and)*
#   and    := not ("and" not)*
#   not    := "not" not | cmp
#   cmp    := term (("=="|"!="|">"|"<"|">="|"<=") term)?
#   term   := atom ("[" expr "]")*
#   atom   := NUMBER | STRING | "(" expr ")" | path | call
#   path   := NAME ("." NAME)*
#   call   := NAME "(" (expr ("," expr)*)? ")"
#
# Names resolve against the room scope: 'player', and helpers 'has' and
# 'count'. Dotted paths and subscripts are the authoring surface — they
# let level writers reach nested player fields and index inventory
# without changing the engine.

_TOKEN = re.compile(
    r"\s*(?:(?P<num>\d+(?:\.\d+)?)"
    r"|(?P<str>'[^']*'|\"[^\"]*\")"
    r"|(?P<op>==|!=|>=|<=|>|<|\(|\)|,|\.|\[|\])"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*))"
)


def _tokenize(src: str):
    pos = 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            raise SyntaxError(f"token at {pos}")
        pos = m.end()
        for kind in ("num", "str", "op", "name"):
            if m.group(kind) is not None:
                yield (kind, m.group(kind) if kind != "num" else float(m.group(kind)))
                break


class _Expr:
    def __init__(self, src: str, scope: dict):
        self.toks = list(_tokenize(src)) + [("eof", None)]
        self.i = 0
        self.scope = scope

    def _peek(self): return self.toks[self.i]
    def _eat(self, kind=None, val=None):
        t = self.toks[self.i]
        if kind and t[0] != kind: raise SyntaxError(f"expected {kind}")
        if val is not None and t[1] != val: raise SyntaxError(f"expected {val}")
        self.i += 1
        return t

    def run(self):
        v = self._or()
        if self._peek()[0] != "eof": raise SyntaxError("trailing input")
        return v

    def _or(self):
        v = self._and()
        while self._peek() == ("name", "or"):
            self._eat("name", "or"); v = bool(v) or bool(self._and())
        return v

    def _and(self):
        v = self._not()
        while self._peek() == ("name", "and"):
            self._eat("name", "and"); v = bool(v) and bool(self._not())
        return v

    def _not(self):
        if self._peek() == ("name", "not"):
            self._eat("name", "not"); return not self._not()
        return self._cmp()

    def _cmp(self):
        left = self._term()
        t = self._peek()
        if t[0] == "op" and t[1] in ("==", "!=", ">", "<", ">=", "<="):
            op = self._eat("op")[1]; right = self._term()
            return {"==": left == right, "!=": left != right, ">": left > right,
                    "<": left < right, ">=": left >= right, "<=": left <= right}[op]
        return left

    def _term(self):
        v = self._atom()
        while self._peek() == ("op", "["):
            self._eat("op"); key = self._or(); self._eat("op", "]")
            v = v[key]
        return v

    def _atom(self):
        t = self._peek()
        if t[0] == "num": return self._eat("num")[1]
        if t[0] == "str": return self._eat("str")[1]
        if t == ("op", "("):
            self._eat("op"); v = self._or(); self._eat("op", ")"); return v
        if t[0] == "name":
            name = self._eat("name")[1]
            if self._peek() == ("op", "("):
                self._eat("op")
                args = []
                if self._peek() != ("op", ")"):
                    args.append(self._or())
                    while self._peek() == ("op", ","):
                        self._eat("op"); args.append(self._or())
                self._eat("op", ")")
                fn = self.scope.get(name)
                if not callable(fn): raise SyntaxError(f"{name} is not callable")
                return fn(*args)
            obj = self.scope.get(name)
            while self._peek() == ("op", "."):
                self._eat("op")
                attr = self._eat("name")[1]
                obj = getattr(obj, attr)
            return obj
        raise SyntaxError(f"unexpected {t}")


def evaluate(src: str, scope: dict):
    return _Expr(src, scope).run()


# ============ persistence ============

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS state ("
        "chat_id INTEGER PRIMARY KEY, level TEXT, room TEXT, inventory TEXT, hp INTEGER)"
    )
    return conn


def load_state(cid: int) -> dict:
    with _db() as c:
        row = c.execute(
            "SELECT level, room, inventory, hp FROM state WHERE chat_id=?", (cid,)
        ).fetchone()
    if not row:
        return {"level": "forest", "room": "start", "inventory": [], "hp": 10}
    return {"level": row[0], "room": row[1], "inventory": json.loads(row[2]), "hp": row[3]}


def save_state(cid: int, st: dict) -> None:
    with _db() as c:
        c.execute(
            "INSERT INTO state(chat_id, level, room, inventory, hp) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET level=excluded.level, room=excluded.room, "
            "inventory=excluded.inventory, hp=excluded.hp",
            (cid, st["level"], st["room"], json.dumps(st["inventory"]), st["hp"]),
        )


# ============ level fetching ============

def _fetch(url: str, max_bytes: int = MAX_BYTES, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "forest-and-ash/1.2"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("response exceeds size cap")
    return data


def _load_manifest() -> dict | None:
    try:
        return json.loads(_fetch(MANIFEST_URL, max_bytes=64 * 1024))
    except Exception as e:
        log.warning("manifest unavailable: %s", e)
        return None


def _sync_level(name: str, entry: dict) -> dict | None:
    target = LEVEL_DIR / f"{name}.json"
    try:
        blob = _fetch(entry["url"])
        if hashlib.sha256(blob).hexdigest() != entry["sha256"]:
            log.warning("level %s failed integrity check", name)
            return None
        parsed = json.loads(blob)
        target.write_bytes(blob)
        return parsed
    except Exception as e:
        log.warning("level %s fetch failed: %s", name, e)
        if target.exists():
            try:
                return json.loads(target.read_bytes())
            except Exception:
                pass
        return None


def get_level(name: str) -> dict | None:
    manifest = _load_manifest()
    if not manifest:
        return None
    entry = manifest.get("levels", {}).get(name)
    return _sync_level(name, entry) if entry else None


# ============ world resolution ============

def _scope(st: dict, level: dict) -> dict:
    """Build the scope level descriptions see: player state and helpers."""
    player = SimpleNamespace(
        room=st["room"],
        hp=st["hp"],
        inventory=list(st["inventory"]),
        stats=SimpleNamespace(hp=st["hp"], items=len(st["inventory"])),
    )
    return {
        "player": player,
        "has":   lambda item: item in st["inventory"],
        "count": lambda item: st["inventory"].count(item),
    }


def _render_desc(parts, scope: dict) -> str:
    out = []
    for part in parts:
        if isinstance(part, str):
            out.append(part)
            continue
        if isinstance(part, dict) and "if" in part:
            try:
                ok = bool(evaluate(part["if"], scope))
            except Exception as e:
                log.warning("condition error: %s", e)
                ok = False
            text = part.get("then") if ok else part.get("else")
            if isinstance(text, str):
                out.append(text)
    return "\n".join(p for p in out if p).strip()


def _room(level: dict, room_id: str) -> dict:
    return level.get("rooms", {}).get(room_id, {})


def _describe(st: dict, level: dict) -> str:
    room = _room(level, st["room"])
    parts = room.get("desc", [])
    if isinstance(parts, str):
        parts = [parts]
    scope = _scope(st, level)
    text = _render_desc(parts, scope) or "..."
    items = room.get("items", [])
    if items:
        text += "\nYou see: " + ", ".join(items)
    return text


# ============ commands ============

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    cid = update.effective_chat.id
    save_state(cid, {"level": "forest", "room": "start", "inventory": [], "hp": 10})
    level = get_level("forest")
    if not level:
        await update.message.reply_text("the world is quiet right now. try again shortly.")
        return
    await update.message.reply_text(_describe(load_state(cid), level))


async def cmd_look(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    st = load_state(update.effective_chat.id)
    level = get_level(st["level"])
    if not level:
        await update.message.reply_text("the world is quiet right now.")
        return
    await update.message.reply_text(_describe(st, level))


async def cmd_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not ctx.args:
        await update.message.reply_text("usage: /go <direction>")
        return
    cid = update.effective_chat.id
    st = load_state(cid)
    level = get_level(st["level"])
    if not level:
        await update.message.reply_text("the world is quiet right now.")
        return
    room = _room(level, st["room"])
    exits = room.get("exits", {})
    direction = ctx.args[0].lower()
    if direction not in exits:
        await update.message.reply_text("you can't go that way.")
        return
    target = exits[direction]
    # exits may point to a room in another level, e.g. "cellar:entry"
    if ":" in target:
        new_level, new_room = target.split(":", 1)
        st["level"], st["room"] = new_level, new_room
    else:
        st["room"] = target
    save_state(cid, st)
    next_level = get_level(st["level"])
    if not next_level:
        await update.message.reply_text("you step through and the world goes dark.")
        return
    await update.message.reply_text(_describe(st, next_level))


async def cmd_take(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    if not ctx.args:
        await update.message.reply_text("usage: /take <item>")
        return
    cid = update.effective_chat.id
    st = load_state(cid)
    level = get_level(st["level"])
    if not level:
        return
    room = _room(level, st["room"])
    item = ctx.args[0].lower()
    if item not in room.get("items", []):
        await update.message.reply_text("that isn't here.")
        return
    room["items"].remove(item)
    st["inventory"].append(item)
    save_state(cid, st)
    await update.message.reply_text(f"you take the {item}.")


async def cmd_inv(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    st = load_state(update.effective_chat.id)
    names = ", ".join(st["inventory"]) or "(empty)"
    await update.message.reply_text(f"inventory: {names}\nhp: {st['hp']}")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "/start — new game\n"
        "/look — describe your room\n"
        "/go <direction> — move\n"
        "/take <item> — pick up\n"
        "/inv — inventory and hp\n"
        "/help — this message"
    )


def main() -> None:
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is not set")
        return
    app = Application.builder().token(BOT_TOKEN).build()
    for name, fn in [
        ("start", cmd_start), ("look", cmd_look), ("go", cmd_go),
        ("take", cmd_take), ("inv", cmd_inv), ("help", cmd_help),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.run_polling()


if __name__ == "__main__":
    main()
