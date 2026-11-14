# language: python, file: levels/forest.py
# Level: forest. Defines rooms and a small on_enter hook.

LEVEL = {
    "name": "forest",
    "rooms": {
        "start": {
            "desc": "tall pines, cold air, a path east.",
            "exits": {"east": "clearing"},
        },
        "clearing": {
            "desc": "a mossy clearing. Something glints in the grass.",
            "exits": {"west": "start"},
        },
    },
}


def on_enter(app, state):
    # hook for this level; unused for now
    return None
