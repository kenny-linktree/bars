# Bars

Instructions for coding agents working in this repository.

Bars is a macOS menu bar app and WidgetKit widget that show usage for AI providers. Each provider is a plugin; Claude, Codex, Cursor and Devin are the standard plugins. A short-lived Python collector runs each plugin in its own worker process to read an existing provider login and call that provider's usage endpoint, and writes a JSON snapshot. A Swift publisher validates that snapshot and publishes one file that the sandboxed widget and the menu bar dropdown both read.

## Read first

- [CONTEXT.md](CONTEXT.md) defines the project vocabulary. Use its terms in code, UI text and docs, and avoid the terms it lists under _Avoid_.
- [docs/design.md](docs/design.md) records the agreed behavior. Read the relevant section before changing behavior.
- [docs/snapshot-contract.md](docs/snapshot-contract.md) is the versioned contract between the collector and the native readers.
- [docs/plugins.md](docs/plugins.md) is the provider plugin interface: the manifest, `plugin.py`, the `bars_plugin` SDK and the update policy.
- [docs/adr/](docs/adr/) holds the architecture decisions.

## Layout

| Path | Contents |
| --- | --- |
| `plugins/<id>/` | Standard provider plugins: `plugin.json` and `plugin.py` for Claude, Codex, Cursor and Devin |
| `collector/bars_plugin/` | The plugin SDK: result values, login lookup and the bounded usage request |
| `collector/bars_collector/` | Python collector: plugin discovery, worker runner, snapshot model and storage |
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

# Python suites; run under /usr/bin/python3 (the 3.9 floor) and a current CPython.
# The collector suite loads the standard plugins from plugins/ and spawns real workers.
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

# Run the collector against the checkout's standard plugins into a scratch snapshot
PYTHONPATH=collector python3 -m bars_collector --plugins plugins --output /tmp/bars-try/snapshot.json --detect
```

Renders go to `.build/previews/`. Inspect them after any UI change. Default previews use synthetic data; `--variant live` reads private usage and its output must not be committed or shared. The tests use synthetic responses and temporary files; none needs provider credentials or network access.

## Invariants

- **Snapshot contract.** Keep the collector, the plugin SDK and the Swift readers in step with [docs/snapshot-contract.md](docs/snapshot-contract.md). Unknown quantities stay null and are never inferred. Failed attempts retain previous values and `fetched_at`. Metric identities are never merged or deduplicated. Each plugin selects its primary and secondary allowances; alternatives keep their own labels, units and scopes. Change the contract document in the same change as any schema change.
- **Providers are plugins; the readers name none.** No Swift file, `refresh.py` or `install.py` may name a provider or assume how many there are. Anything a reader needs to know about a provider travels in the manifest and the snapshot. Plugin code is imported only inside a worker process, never in the coordinator; worker results are bounded JSON validated by `validate_result` before they touch the snapshot. The public plugin interface is `bars_plugin` and `plugin.json`; changing either is a documented interface change in [docs/plugins.md](docs/plugins.md).
- **Standard plugin updates.** The installer replaces a standard plugin file only when it is unchanged since the installer wrote it (digest in `plugins/.installed.json`); an edited file is kept with a warning. Keep the four standard plugins' manifests and `scripts/preview/harness/Fixtures.swift` in step.
- **Foundation-only shared core.** `macOS/Shared/Snapshot.swift`, `macOS/Shared/UsagePresentation.swift` and `macOS/Shared/SnapshotStore.swift` import only Foundation (and Darwin). SwiftUI and AppKit belong in `ProviderRow.swift`, `macOS/App/` and `macOS/Widget/`. The Command Line Tools check above and the `BarsCore` package target depend on this.
- **Credentials.** Never log, print, snapshot or commit credentials, account identifiers or raw provider responses. Bars only reads provider logins; it never writes, refreshes or rotates them. Each credential goes only to that provider's fixed usage endpoint, and redirects are refused. Tests use synthetic data only.
- **Python 3.9.** The collector, the plugin SDK, the standard plugins and the installer must run on Python 3.9 with only the standard library. See [collector/README.md](collector/README.md) for features to avoid.
- **Preview mirror.** `scripts/preview/harness/Mirrors.swift` copies the overview layout of `BarsDropdown` in `macOS/App/BarsApp.swift`. Update both together.
- **Installer safety.** Never run the real installer against temporary paths; it stops the real LaunchAgent and registers a duplicate widget. Use the fixtures in `tests/integration`. Never point the collector or tests at the installed plugins directory; use `plugins/` in the checkout or a temporary directory.
- **Docs follow behavior.** Update `docs/design.md`, and the README or contract where affected, in the same change as any behavior change. Keep docs as a design record in neutral voice, and keep machine-specific results in `docs/verification.md` labeled as observations.

## Development notes

- [docs/development.md](docs/development.md): preview renders, Swift checks without Xcode, Python compatibility, installer testing and developing a plugin.
- [docs/plugins.md](docs/plugins.md): the provider plugin interface and update policy.
- [docs/distribution.md](docs/distribution.md): clone-and-build distribution, bootstrap and installer behavior, provider selection and uninstalling.
- [docs/troubleshooting.md](docs/troubleshooting.md): widget and dropdown drift, inspecting chronod logs and timeline archives, tracing and capture techniques.
- [docs/verification.md](docs/verification.md): dated verification results and known limits.
