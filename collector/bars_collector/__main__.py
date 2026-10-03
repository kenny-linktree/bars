import argparse
import json
from pathlib import Path
import sys

from .adapters import detect
from .model import PROVIDERS
from .runner import CollectionCancelled, run
from .storage import AlreadyRunning


def main(argv=None):
    parser = argparse.ArgumentParser(description="Collect Bars usage once, without changing provider logins.")
    parser.add_argument("--output", type=Path, default=Path.home() / "Library/Application Support/Bars/snapshot.json")
    parser.add_argument("--provider", choices=PROVIDERS)
    parser.add_argument("--disable", action="append", choices=PROVIDERS, default=[], metavar="ID",
                        help="Skip this provider and mark it disabled in the snapshot. Repeatable.")
    parser.add_argument("--detect", action="store_true",
                        help="Print which providers have local login material as JSON, without network requests.")
    parser.add_argument("--timeout", type=int, default=40, help="Per-provider hard deadline in seconds (5-120; default 40).")
    arguments = parser.parse_args(argv)
    if not 5 <= arguments.timeout <= 120:
        parser.error("--timeout must be between 5 and 120 seconds")
    if arguments.detect:
        if arguments.provider or arguments.disable:
            parser.error("--detect cannot be combined with --provider or --disable")
        # Booleans only: never credential values, paths or account identities.
        print(json.dumps(detect()))
        return 0
    selected = [arguments.provider] if arguments.provider else list(PROVIDERS)
    try:
        snapshot = run(arguments.output.expanduser(), selected, arguments.timeout, disabled=arguments.disable)
    except CollectionCancelled:
        print("Bars collection was stopped.", file=sys.stderr)
        return 143
    except AlreadyRunning:
        print("Bars collection is already running.", file=sys.stderr)
        return 75
    except Exception:
        print("Bars could not read or publish its snapshot. Check the output directory and existing snapshot.", file=sys.stderr)
        return 1
    # Status only: no quantities, credentials, account identities or raw responses.
    print("; ".join(p["id"] + ": " + (p["status"] if p.get("enabled", True) else "disabled")
                    for p in snapshot["providers"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
