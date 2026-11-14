# Forest & Ash

A small text-adventure bot for Telegram. You wake in a forest, find
your way through a cellar, climb a tower. Short, three levels, ~20 rooms.
Content is data — the world is defined in `levels/*.json` and pulled live
from this repo so new rooms can be added without redeploying the bot.

## Run

    pip install python-telegram-bot
    export BOT_TOKEN=...
    python bot.py

## How content works

Each level is a JSON file describing rooms. A room has:

- `desc` — description, either a string or a list of strings and
  `{"if": ..., "then": ..., "else": ...}` blocks. The condition system
  supports comparisons (`>`, `<`, `==`), `has('item')`, `count('item')`,
  `player.hp`, `player.room`, and dynamic field access like
  `player.stats.luck`. Level authors use this to show different text
  depending on what the player has done.
- `exits` — map of direction to next room id.
- `items` — items on the floor in this room.

Levels are sha256-verified against `manifest.json` before loading, and
downloads are size-capped. Player state persists in a local SQLite file.

## Levels

- `forest` — the opening area.
- `cellar` — reached from the forest clearing.
- `tower` — the finale.

## Adding content

Add rooms to an existing level, or create a new `levels/<name>.json` and
register it in `manifest.json`. Push. Running bots pick it up on the next
`/start` or `/go`.
