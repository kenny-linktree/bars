"""Provider plugin discovery.

The coordinator reads only each plugin's `plugin.json`. `plugin.py` is imported solely
inside a worker process (see `Plugin.__call__`), so a plugin that fails to import,
hangs, crashes or prints cannot affect the coordinator or the other providers.
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import sys

from bars_plugin.values import CollectionError

MANIFEST_VERSION = 1
MANIFEST_NAME = "plugin.json"
MODULE_NAME = "plugin.py"
MAX_MANIFEST_BYTES = 65536
PLUGIN_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
MAX_NAME = 32
MAX_TEXT = 200
SETUP_FIELDS = ("install_title", "install_url", "login_command", "login_step", "sign_in_again", "credential_note")
URL_FIELDS = {"install_url"}


class Plugin:
    """One discovered plugin. Picklable, so workers receive it and import the module themselves."""

    def __init__(self, pid, name, directory, usage_url=None, setup=None, error=None, mode="collect"):
        self.id = pid
        self.name = name
        self.directory = str(directory)
        self.usage_url = usage_url
        self.setup = setup
        self.error = error
        self.mode = mode

    @property
    def loadable(self):
        return self.error is None

    def detector(self):
        """The same plugin as a worker task that answers `detect()` instead of `collect()`."""
        return Plugin(self.id, self.name, self.directory, self.usage_url, self.setup, self.error, mode="detect")

    def __call__(self):
        """Worker side only: import `plugin.py` and run the requested function."""
        if self.error is not None:
            raise CollectionError(self.error)
        module = load_module(self.id, Path(self.directory) / MODULE_NAME)
        if self.mode == "detect":
            # Only a literal True counts; "yes", 1 or a truthy object are not a login.
            detect = getattr(module, "detect", None)
            return {"found": detect() is True if callable(detect) else False}
        collect = getattr(module, "collect", None)
        if not callable(collect):
            raise CollectionError("Plugin has no collect function.")
        value = collect()
        if not isinstance(value, dict):
            raise CollectionError("Plugin returned an invalid result.")
        return value


def load_module(pid, path):
    """Import a plugin module under a private name, without writing bytecode beside it."""
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("bars_plugins." + pid, str(path))
    if spec is None or spec.loader is None:
        raise CollectionError("Plugin module could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _text(manifest, key, limit, *, required):
    value = manifest.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit or not value.isprintable():
        raise ValueError(key)
    return value


def _url(value, key):
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("https://") or len(value) > MAX_TEXT \
            or not value.isprintable() or any(c.isspace() for c in value):
        raise ValueError(key)
    return value


def parse_manifest(data, expected_id):
    """Validate manifest JSON. Unknown keys are ignored so newer plugins still load."""
    manifest = json.loads(data)
    if not isinstance(manifest, dict):
        raise ValueError("manifest")
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError("manifest_version")
    pid = manifest.get("id")
    if pid != expected_id:
        raise ValueError("id")
    name = _text(manifest, "name", MAX_NAME, required=True)
    usage_url = _url(manifest.get("usage_url"), "usage_url")
    setup = manifest.get("setup")
    if setup is not None:
        if not isinstance(setup, dict):
            raise ValueError("setup")
        parsed = {}
        for key in SETUP_FIELDS:
            parsed[key] = _url(setup.get(key), key) if key in URL_FIELDS else _text(setup, key, MAX_TEXT, required=True)
        if parsed["install_url"] is None:
            raise ValueError("install_url")
        setup = parsed
    return dict(id=pid, name=name, usage_url=usage_url, setup=setup)


def read_plugin(directory):
    """A `Plugin` for one directory. Manifest problems yield a plugin that reports an error
    instead of collecting, so the person sees it in the overview rather than nowhere."""
    directory = Path(directory)
    pid = directory.name
    fallback = pid.replace("_", " ").strip().capitalize() or pid
    try:
        with (directory / MANIFEST_NAME).open("rb") as file:
            data = file.read(MAX_MANIFEST_BYTES + 1)
        if len(data) > MAX_MANIFEST_BYTES:
            raise ValueError("manifest")
        manifest = parse_manifest(data, pid)
    except FileNotFoundError:
        return Plugin(pid, fallback, directory, error="Plugin is missing its plugin.json.")
    except (OSError, ValueError, RecursionError, UnicodeError):
        return Plugin(pid, fallback, directory, error="Plugin has an invalid plugin.json.")
    if not (directory / MODULE_NAME).is_file():
        return Plugin(pid, manifest["name"], directory, manifest["usage_url"], manifest["setup"],
                      error="Plugin is missing its plugin.py.")
    return Plugin(pid, manifest["name"], directory, manifest["usage_url"], manifest["setup"])


def discover(root):
    """Plugins under `root`, sorted by id. Entries whose name is not a valid id are ignored.

    A missing root means no plugins. Only directories (or links to directories) count.
    """
    root = Path(root)
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    plugins = []
    for name in names:
        if PLUGIN_ID.fullmatch(name) and (root / name).is_dir():
            plugins.append(read_plugin(root / name))
    return plugins
