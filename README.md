# adventure-bot



# adventure-bot

A tiny text-adventure Telegram bot.

    export BOT_TOKEN=...
    python bot.py

## Content

Levels are Python scripts in `levels/`. The bot fetches the level script
on each `/start` and `/go` so new rooms and behavior can be added by
editing the repo — no redeploy. Level scripts define `LEVEL` (rooms and
exits) and may define `on_enter(app, state)` for custom per-level hooks.
