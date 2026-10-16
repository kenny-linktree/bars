"""The interface a Bars provider plugin uses. See docs/plugins.md.

A plugin is a directory with `plugin.json` (identity and setup text, read without
running code) and `plugin.py`, a single module that defines:

    def collect():
        '''One bounded fetch. Return `result(...)`, or raise `CollectionError`.'''

    def detect():  # optional
        '''True when local login material exists. No network. Nothing is printed.'''

`plugin.py` runs only inside a worker process with a hard deadline, an allowlisted
environment and discarded standard streams. Return values cross to the collector as
JSON. Anything other than a `CollectionError` becomes a fixed error message, so no
exception text, credential or raw response can reach the snapshot.

Everything a plugin needs from Bars is exported here. Other modules of the collector
are private and may change between versions.
"""

from .access import (CHILD_ENVIRONMENT, MAX_BYTES, child_environment, command, http_json, keychain,
                     keychain_entry_exists, login_found, read_bytes, read_json, read_toml, token)
from .values import (KINDS, PERIODS, SCOPES, CollectionError, metric, now, number, result, timestamp)

SDK_VERSION = 1

__all__ = [
    "SDK_VERSION", "CollectionError", "KINDS", "PERIODS", "SCOPES",
    "metric", "result", "number", "timestamp", "now",
    "http_json", "keychain", "keychain_entry_exists", "command", "login_found",
    "read_bytes", "read_json", "read_toml", "token",
    "CHILD_ENVIRONMENT", "MAX_BYTES", "child_environment",
]
