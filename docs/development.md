# Development

Notes for changing Bars locally: previewing UI without reinstalling, the Swift checks that run without Xcode, Python compatibility and testing the installer safely. Build, install and test commands are in the [README](../README.md#development).

## Preview renders

App-icon artwork and its rebuild command are documented in [Meter artwork](../macOS/Artwork/README.md). The menu-bar symbol is drawn separately in `macOS/App/BarsIcon.swift`.

`scripts/preview/render.sh` renders the real widget, provider row, bar and details views to PNG files at 2x, in light and dark appearance, without installing the app. It builds with the Command Line Tools and takes a few seconds.

```sh
scripts/preview/render.sh                                   # synthetic variants into .build/previews/
scripts/preview/render.sh --variant mixed --only widget --output /tmp/previews
scripts/preview/render.sh --widget-size 344x344             # match the installed widget's size
scripts/preview/render.sh --check                          # synthetic fixtures, processes and dropdown lifecycle
scripts/preview/render.sh --help
```

- Default variants are `stale`, `mixed` (failures and a login problem), `heavy` (high use), `previous` (retained values from an ended period), `empty` (first launch), `setup` (missing logins), `hidden` (disabled providers) and `subscription` (Claude and Codex quota-only accounts). These use fixed synthetic data. `--variant live` explicitly reads the published snapshot and is skipped when none exists. Live renders contain private usage figures and must not be committed or shared.
- Surfaces are `widget`, `overview` and `details`. Files are named by surface, variant and appearance, such as `widget-heavy-dark.png` and `dropdown-overview-heavy-light.png`.
- The widget canvas defaults to WidgetKit's large size of 360 × 376 pt. On the development machine (macOS 26) the installed large widget measured 344 × 344 pt, so pass `--widget-size 344x344` to check layout at that size.
- `scripts/preview/harness/Mirrors.swift` copies the overview layout of `BarsDropdown`, because `macOS/App/BarsApp.swift`, which defines it, owns `@main` and the AppKit lifecycle. Keep the mirror in step with `BarsDropdown` by hand. The rows, bars and details inside it are the real shared views.
- The dropdown is a fixed 440 × 700 pt popover, and overview content taller than 700 pt scrolls. The harness fits each render to its content and prints a warning when the overview exceeds 700 pt.
- Renders are not hosted by WidgetKit. Desktop widget tinting, popover materials and WidgetKit's layout of relative-date text differ from the installed app.

## Swift checks without Xcode

The Command Line Tools omit XCTest, so the core checks compile the three Foundation-only shared files together with `tests/Swift/SnapshotTests.swift` using `swiftc` (see [Tests](../README.md#tests)). Keep SwiftUI and AppKit out of `macOS/Shared/Snapshot.swift`, `macOS/Shared/UsagePresentation.swift` and `macOS/Shared/SnapshotStore.swift`; importing either breaks that path and the `BarsCore` package target in `Package.swift`, which excludes the SwiftUI file `ProviderRow.swift`. With full Xcode selected, `swift test` runs the same checks through XCTest.

## Synthetic performance measurements

These benchmarks use generated data only. They do not read provider logins, contact providers or launch the installed app. Run them from the repository root:

```sh
mkdir -p .build/benchmarks
swiftc -O macOS/Shared/Snapshot.swift macOS/Shared/UsagePresentation.swift \
  macOS/Shared/SnapshotStore.swift scripts/benchmark-core.swift \
  -o .build/benchmarks/core
.build/benchmarks/core
PYTHONDONTWRITEBYTECODE=1 python3 scripts/benchmark-collector.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/benchmark-refresh.py --samples 5
```

The native benchmark reports warmed decode, formatting, read and publication timings for normal and deliberately large snapshots. The collector benchmark measures fresh worker processes, atomic snapshot writes and a separate sampled process-tree RSS total. The refresh benchmark uses stub publishers to measure fast completion, partial publication, all-hidden collection and deadline expiry. Its `--reload-delay 2` option models the native publisher's run-loop wait without contacting WidgetKit. RSS includes shared pages more than once and sampling can miss short peaks; these are not battery-use measurements. Compare the same input, compiler optimization, interpreter and machine when evaluating a change. Dated observations are in [verification.md](verification.md).

## Python compatibility

The collector and installer support Python 3.9 or later so that the Command Line Tools' `/usr/bin/python3` (3.9.6 in the verified installation) works without Homebrew. Run both Python suites under `/usr/bin/python3` and a current CPython before merging. Python 3.9 differs in `datetime.fromisoformat` strictness and in `HTTPError` cleanup; the collector handles both. [collector/README.md](../collector/README.md) lists the newer language and standard-library features to avoid.

## Testing the installer

Never point the real installer at temporary paths to test it: it stops the real LaunchAgent and registers a duplicate widget. Use the isolated fixtures in `tests/integration`, which mock system registration and `launchctl`, and a bare Git repository in a temporary directory for the bootstrap script (`BARS_REPO_URL=file:///…`), as `tests/integration/test_bootstrap.py` does.
