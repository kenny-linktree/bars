# Bars

Instructions for coding agents working in this repository.

Bars is a macOS menu bar app and WidgetKit widget that show usage for Claude, Codex, Cursor and Devin. A short-lived Python collector reads existing provider logins, calls each provider's usage endpoint and writes a JSON snapshot. A Swift publisher validates that snapshot and publishes one file that the sandboxed widget and the menu bar dropdown both read.

## Read first

- [CONTEXT.md](CONTEXT.md) defines the project vocabulary. Use its terms in code, UI text and docs, and avoid the terms it lists under _Avoid_.
- [docs/design.md](docs/design.md) records the agreed behavior. Read the relevant section before changing behavior.
- [docs/snapshot-contract.md](docs/snapshot-contract.md) is the versioned contract between the collector and the native readers.
- [docs/adr/](docs/adr/) holds the architecture decisions.

## Layout

| Path | Contents |
| --- | --- |
| `collector/bars_collector/` | Python collector: provider adapters, parsers, snapshot model and storage |
| `integration/refresh.py` | Scheduled refresh runner that runs the collector and the native publisher |
| `macOS/Shared/` | Swift snapshot model, presentation logic and storage, shared by app and widget |
| `macOS/App/`, `macOS/Widget/` | Menu bar app and WidgetKit extension |
| `macOS/Config/` | Info property lists, entitlements and the signing and widget storage note |
| `scripts/` | `bootstrap.sh`, `build.sh`, `install.py` and the preview harness |
| `tests/` | Python collector, integration and Swift checks |

## Build, test and preview

Requires macOS 14 or later and Apple's Command Line Tools. Full Xcode is optional.

```sh
# Build the app and widget
scripts/build.sh --toolchain clt

# Python suites; run under /usr/bin/python3 (the 3.9 floor) and a current CPython
PYTHONPATH=collector python3 -m unittest discover -s tests/collector -v
python3 -m unittest discover -s tests/integration -v
sh -n scripts/bootstrap.sh
scripts/preview/render.sh --check

# Swift core checks without XCTest
swiftc macOS/Shared/Snapshot.swift \
  macOS/Shared/UsagePresentation.swift \
  macOS/Shared/SnapshotStore.swift \
  tests/Swift/SnapshotTests.swift -o /tmp/bars-core-checks
/tmp/bars-core-checks

# Render the widget, dropdown overview and details to PNG without installing
scripts/preview/render.sh --widget-size 344x344
```

Renders go to `.build/previews/`. Inspect them after any UI change. Default previews use synthetic data; `--variant live` reads private usage and its output must not be committed or shared. The tests use synthetic responses and temporary files; none needs provider credentials or network access.

## Invariants

- **Snapshot contract.** Keep the collector and the Swift readers in step with [docs/snapshot-contract.md](docs/snapshot-contract.md). Unknown quantities stay null and are never inferred. Failed attempts retain previous values and `fetched_at`. Metric identities are never merged or deduplicated. Primary selection follows the provider-specific priorities in the contract; alternatives keep their own labels, units and scopes. Change the contract document in the same change as any schema change.
- **Foundation-only shared core.** `macOS/Shared/Snapshot.swift`, `macOS/Shared/UsagePresentation.swift` and `macOS/Shared/SnapshotStore.swift` import only Foundation (and Darwin). SwiftUI and AppKit belong in `ProviderRow.swift`, `macOS/App/` and `macOS/Widget/`. The Command Line Tools check above and the `BarsCore` package target depend on this.
- **Credentials.** Never log, print, snapshot or commit credentials, account identifiers or raw provider responses. Bars only reads provider logins; it never writes, refreshes or rotates them. Each credential goes only to that provider's fixed usage endpoint, and redirects are refused. Tests use synthetic data only.
- **Python 3.9.** The collector and installer must run on Python 3.9 with only the standard library. See [collector/README.md](collector/README.md) for features to avoid.
- **Preview mirror.** `scripts/preview/harness/Mirrors.swift` copies the overview layout of `BarsDropdown` in `macOS/App/BarsApp.swift`. Update both together.
- **Installer safety.** Never run the real installer against temporary paths; it stops the real LaunchAgent and registers a duplicate widget. Use the fixtures in `tests/integration`.
- **Docs follow behavior.** Update `docs/design.md`, and the README or contract where affected, in the same change as any behavior change. Keep docs as a design record in neutral voice, and keep machine-specific results in `docs/verification.md` labeled as observations.

## Development notes

- [docs/development.md](docs/development.md): preview renders, Swift checks without Xcode, Python compatibility and installer testing.
- [docs/distribution.md](docs/distribution.md): clone-and-build distribution, bootstrap and installer behavior, provider selection and uninstalling.
- [docs/troubleshooting.md](docs/troubleshooting.md): widget and dropdown drift, inspecting chronod logs and timeline archives, tracing and capture techniques.
- [docs/verification.md](docs/verification.md): dated verification results and known limits.
