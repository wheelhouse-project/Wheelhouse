"""Convert NavigationCommands to IPC hotkey action dicts."""

from .models import NavigationCommand

_LANDMARK_KEYS: dict[str, list[str]] = {
    "home": ["home"],
    "end": ["end"],
    "top": ["ctrl", "home"],
    "bottom": ["ctrl", "end"],
    "start_of_word": ["ctrl", "left"],
    "end_of_word": ["ctrl", "right"],
    "start_of_paragraph": ["ctrl", "up"],
    "end_of_paragraph": ["ctrl", "down"],
}

# Maps (direction, unit) -> base keys (without shift)
_RELATIVE_KEYS: dict[tuple[str, str], list[str]] = {
    ("right", "character"): ["right"],
    ("left", "character"): ["left"],
    ("right", "word"): ["ctrl", "right"],
    ("left", "word"): ["ctrl", "left"],
    ("right", "paragraph"): ["ctrl", "down"],
    ("left", "paragraph"): ["ctrl", "up"],
}


class NavigationExecutor:
    """Stateless converter: NavigationCommand list -> IPC action dicts."""

    @staticmethod
    def to_actions(commands: list) -> list:
        """Convert parsed commands to a list of IPC hotkey action dicts.

        Each dict has format:
            {"action": "hotkey_action", "params": {"keys": [...], "repeat": N}}
        """
        return [NavigationExecutor._command_to_action(cmd) for cmd in commands]

    @staticmethod
    def _command_to_action(cmd: NavigationCommand) -> dict:
        if cmd.kind == "landmark":
            base_keys = _LANDMARK_KEYS[cmd.landmark]
            repeat = 1
        else:
            base_keys = _RELATIVE_KEYS[(cmd.direction, cmd.unit)]
            repeat = cmd.count

        keys = ["shift"] + base_keys if cmd.verb == "grab" else list(base_keys)
        return {"action": "hotkey_action", "params": {"keys": keys, "repeat": repeat}}
