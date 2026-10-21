import argparse
import json
from pathlib import Path
import sys

from .plugins import PLUGIN_ID, discover
from .runner import DEFAULT_TIMEOUT, CollectionCancelled, collect_results, run
from .storage import AlreadyRunning, SnapshotTooLarge

# Exit status the scheduler translates into a fixed explanation; stderr itself is not forwarded.
SNAPSHOT_TOO_LARGE_STATUS = 78

SUPPORT = Path.home() / "Library/Application Support/Bars"
DETECT_TIMEOUT = 20


def provider_id(value):
    if not PLUGIN_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("provider ids are lowercase letters, digits and underscores")
    return value


def detect(plugins, timeout=DETECT_TIMEOUT):
    """Which plugins find local login material, each asked in its own worker process.

    No network request is made by the standard plugins; a plugin that fails, hangs or
    returns anything but a boolean counts as not found. Output never contains
    credentials, paths or account identifiers.
    """
    loadable = [plugin for plugin in plugins if plugin.loadable]
    found = {plugin.id: False for plugin in plugins}
    detectors = {plugin.id: plugin.detector() for plugin in loadable}
    for pid, value in collect_results(list(detectors), timeout, detectors, retry=False):
        found[pid] = value.get("status") == "ok" and value.get("found") is True
    return found


def diagnostic(line):
    """Scheduling diagnostics go to standard error, one fixed-grammar line each (see schedule.py)."""
    try:
        print(line, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect Bars usage once, without changing provider logins.")
    parser.add_argument("--output", type=Path, default=SUPPORT / "snapshot.json")
    parser.add_argument("--plugins", type=Path, default=SUPPORT / "plugins", metavar="DIR",
                        help="Directory of provider plugins, one subdirectory per provider.")
    parser.add_argument("--provider", type=provider_id, metavar="ID")
    parser.add_argument("--disable", action="append", type=provider_id, default=[], metavar="ID",
                        help="Skip this provider and mark it disabled in the snapshot. Repeatable.")
    parser.add_argument("--scheduled", action="store_true",
                        help="A scheduled run: collect only providers due on their own interval, and count "
                             "successes toward shortening an interval lengthened by rate limits.")
    parser.add_argument("--requested-at", type=int, metavar="EPOCH",
                        help="When an explicit refresh was requested, in Unix seconds: skip providers whose "
                             "latest attempt finished since then.")
    parser.add_argument("--detect", action="store_true",
                        help="Print which providers have local login material as JSON, without network requests.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="Per-provider hard deadline in seconds, including retries (5-120; default %d)." % DEFAULT_TIMEOUT)
    arguments = parser.parse_args(argv)
    if not 5 <= arguments.timeout <= 120:
        parser.error("--timeout must be between 5 and 120 seconds")
    plugins = discover(arguments.plugins.expanduser())
    if arguments.detect:
        if arguments.provider or arguments.disable:
            parser.error("--detect cannot be combined with --provider or --disable")
        try:
            found = detect(plugins)
        except CollectionCancelled:
            return 143
        # Booleans only: never credential values, paths or account identities.
        print(json.dumps(found))
        return 0
    ids = [plugin.id for plugin in plugins]
    if arguments.provider and arguments.provider not in ids:
        parser.error("--provider names a provider with no plugin: " + arguments.provider)
    if arguments.scheduled and arguments.requested_at is not None:
        parser.error("--scheduled cannot be combined with --requested-at")
    selected = [arguments.provider] if arguments.provider else ids
    try:
        snapshot = run(arguments.output.expanduser(), selected, arguments.timeout,
                       disabled=arguments.disable, plugins=plugins, scheduled=arguments.scheduled,
                       requested_at=arguments.requested_at, report=diagnostic)
    except CollectionCancelled:
        print("Bars collection was stopped.", file=sys.stderr)
        return 143
    except AlreadyRunning:
        print("Bars collection is already running.", file=sys.stderr)
        return 75
    except SnapshotTooLarge:
        print("Installed plugins exceed the snapshot size bound; remove some. The previous snapshot was kept.",
              file=sys.stderr)
        return SNAPSHOT_TOO_LARGE_STATUS
    except Exception:
        print("Bars could not read or publish its snapshot. Check the output directory and existing snapshot.", file=sys.stderr)
        return 1
    # Status only: no quantities, credentials, account identities or raw responses.
    print("; ".join(p["id"] + ": " + (p["status"] if p.get("enabled", True) else "disabled")
                    for p in snapshot["providers"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
