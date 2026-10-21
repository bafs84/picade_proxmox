"""Read the cabinet's existing EmulationStation SDL key/button mapping."""
from pathlib import Path
import time
import xml.etree.ElementTree as ET


DEFAULT_KEYS = {
    1073741904: "left", 1073741903: "right",
    1073741906: "up", 1073741905: "down",
    1073742048: "details", 1073742050: "back",
    1073742049: "exit", 32: "toggle_view", 13: "details",
    27: "back", 97: "details", 98: "back", 120: "overview", 110: "network", 109: "overview", 103: "gps", 9: "toggle_view",
}
ACTIONS = {"up": "up", "down": "down", "left": "left", "right": "right",
           "a": "details", "b": "back", "x": "toggle_view", "y": "storage",
           "start": "overview", "select": "exit"}


class Controls:
    def __init__(self, path, exit_key=None):
        self.keys = dict(DEFAULT_KEYS)
        self.controllers = {}
        self.warning = None
        try:
            tree = ET.parse(str(Path(path).expanduser()))
            for device in tree.getroot().findall("inputConfig"):
                mapping = {}
                for item in device.findall("input"):
                    action = ACTIONS.get(item.get("name"))
                    if action:
                        mapping[(item.get("type"), int(item.get("id")), int(item.get("value", "1")))] = action
                if device.get("type") == "keyboard":
                    self.keys.update({code: action for (kind, code, value), action in mapping.items() if kind == "key"})
                else:
                    self.controllers[device.get("deviceGUID", "").lower()] = mapping
        except (OSError, ValueError, ET.ParseError):
            self.warning = "Default PICADE keys"
        if exit_key is not None:
            # A cabinet-specific exit replaces old keyboard shortcuts, rather
            # than leaving the original Select button able to close the app.
            self.keys = {key: action for key, action in self.keys.items() if action != "exit"}
            self.keys[exit_key] = "exit"
        self.last_axis = {}

    def joystick(self, guid, kind, index, value, now=None):
        mapping = self.controllers.get(guid.lower(), {})
        if kind == "axis":
            if abs(value) < 16000:
                self.last_axis.pop((guid, index), None)
                return None
            sign = 1 if value > 0 else -1
            now = time.monotonic() if now is None else now
            prior = self.last_axis.get((guid, index))
            if prior and prior[0] == sign and now - prior[1] < .18:
                return None
            self.last_axis[(guid, index)] = (sign, now)
            return mapping.get((kind, index, sign))
        if kind == "hat" and value == 0:
            return None
        return mapping.get((kind, index, value))


class Selection:
    def __init__(self, gps_enabled=False):
        self.host = 0
        self.guest_key = None
        self.detail = False
        self.storage = 0
        self.view = "network"
        self.views = ('network', 'metrics', 'gps') if gps_enabled else ('network', 'metrics')

    def resolve(self, guests):
        keys = [g["key"] for g in guests]
        if self.guest_key not in keys:
            self.guest_key = keys[0] if keys else None
        return keys.index(self.guest_key) if keys else 0

    def act(self, action, hosts, guests):
        selected = self.resolve(guests)
        if action == "exit":
            return False
        if action in ("left", "right"):
            self.host = (self.host + (-1 if action == "left" else 1)) % max(1, len(hosts))
            self.guest_key = None
            self.storage = 0
        elif action in ("up", "down") and guests:
            self.guest_key = guests[(selected + (-1 if action == "up" else 1)) % len(guests)]["key"]
        elif action == "details":
            self.view = "metrics"
            self.detail = bool(guests)
        elif action in ("back", "overview"):
            self.detail = False
            self.view = "network" if action == "back" else "metrics"
        elif action == "network":
            self.view = "network"
        elif action == "gps" and 'gps' in self.views:
            self.view = "gps"
        elif action == "toggle_view":
            self.view = self.views[(self.views.index(self.view) + 1) % len(self.views)]
        elif action == "storage":
            self.storage += 1
        return True
