# Verification observations

These are dated observations from a development Mac running macOS 26. They do not establish behavior on every supported macOS release. Tests use synthetic data; historical checks of the installed app are summarized without account values or screenshots.

## October 1, 2026

The first installation used Python 3.14.7 and Command Line Tools with Swift 6.3.3, without full Xcode or a paid developer membership. The initial checks passed: 30 collector tests, 10 integration tests and 11 Swift checks. Later changes that day increased the collector suite to 31 and the Swift checks to 16; those checks passed as well.

The Release app and widget built with `scripts/build.sh --toolchain clt`, and both installed signatures passed strict verification. The system-hosted WidgetKit preview rendered all four providers, and Notification Center persisted the widget on the desktop. Manual refresh of all providers succeeded. A provider-only refresh changed only that provider's success timestamp. Background refresh succeeded with the host app closed. The published snapshot had mode 0600, and the installed LaunchAgent had calendar triggers at minutes 00, 15, 30 and 45.

### Reload delivery

A rebuilt extension contained new reset captions while the archived WidgetKit timeline retained old captions. Removing and adding the widget reused that timeline. The publisher had exited before WidgetCenter delivered its asynchronous reload request.

Servicing the publisher's run loop for two seconds after requesting a reload produced both a delivered-request log and a chronod external reload request. The regenerated timeline contained the new captions. This gives macOS a bounded opportunity to deliver the request; it is not a synchronous rendering acknowledgement. Publications with `--no-reload` still exit immediately.

### Reset times and pace

The native checks covered daylight-saving transitions, UTC and local month boundaries, year rollover, leap February and retained previous periods. After installation, the system-hosted timeline contained local reset dates and the estimate marker instead of the old UTC-only caption.

Pace checks covered period assignment, unknown period tokens, snapshots without a period field, fetch-time anchoring, projected run-out dates, used-up states and secondary allowance tags. The installed snapshot included periods for proportional allowances and omitted them for balances and the Codex session window. The host view displayed pace markers and pace text. The system-hosted widget was not captured during that change.

### Menu bar dropdown

The Release build installed and relaunched with `LSUIElement` enabled. An accessibility press on the status item and a provider URL both opened the dropdown; it remained visible across samples taken up to three seconds later. A provider URL also launched the app from a stopped state and opened the requested details view. Repeated status-item presses and provider links closed and reopened it.

The initial transient popover closed when the accessory app lost active status. Application-defined dismissal with an outside-click monitor and Escape corrected that behavior. At cold start, the provider URL arrived before status-item setup, and the new status-item window initially had a zero-height frame. Deferring the open until setup and waiting for a non-zero frame corrected the cold-start path.

Display-based screenshots omitted the popover on this Mac. Per-window and multi-display captures included it. A physical pointer click was not simulated; verification used accessibility and URL triggers. Temporary tracing used to diagnose launch timing was removed.

### Widget and dropdown drift after installation

After replacing the app bundle, the dropdown showed new pace text while the widget used an older layout. The installed extension and published snapshot contained the expected data, but chronod reused an extension process started before the rebuild. A new extension process produced a timeline with pace text.

The installer now ends processes started from the installed app and widget paths after swapping and registering the bundle. It requests a new widget timeline and relaunches the app when appropriate, while holding the refresh lock. Installer tests cover restart ordering and preserve existing processes after a failed installation.

On the installed app, both process IDs changed, the timeline archive was regenerated, and subsequent widget and dropdown captures showed matching layouts. External reload requests succeeded within three seconds in the observed logs and were marked exempt from the widget budget. The widget's own 15-minute fallback remained budgeted. These observations do not guarantee future reload timing. A later log extract on October 2 showed most of Bars' own reload requests tagged as budgeted, so the exemption is an observation about specific runs, not a guarantee.

## October 2, 2026: publication checks

Checks ran on macOS 26.6.2 with Command Line Tools. Before the publication fixes, Python 3.9.6 and 3.14.8 each ran 72 collector tests and 53 integration tests successfully, with one expected collector skip on Python 3.9. The native build and 28 Swift core checks passed.

After the fixes, all installed supported Python interpreters were checked:

| Runtime | Collector | Integration |
| --- | --- | --- |
| Python 3.9.6 | 75 tests, 74 passed and one expected skip | 65 passed |
| Python 3.13.16 | 75 passed | 65 passed |
| Python 3.14.8 | 75 passed | 65 passed |

The skipped test compares the TOML subset reader with `tomllib`, which is absent from Python 3.9. The direct Swift compilation passed all 28 core checks. `scripts/preview/render.sh --check` passed three checks for synthetic fixture isolation, explicit live-data loading and native refresh process configuration. The Release app and widget built with Command Line Tools and passed strict signature verification. Shell syntax, property-list validation and all 60 relative documentation links passed.

The default preview command produced 84 synthetic images. All 72 images comparable with the original synthetic baseline were byte-identical. The other 12 use the corrected stale fixture, which previously could read local usage; all 84 matched a second render after that correction. Both public screenshots exactly match the synthetic heavy-use fixtures.

Regression tests cover the README's failed partial-download path, root refusal, invalid bootstrap options, Git environment isolation, symlinked storage and logs, bounded credential-tool output, process deadlines, runner termination during startup and collection, closed standard input and filtered child environments. Installer and uninstaller tests use temporary destinations, a temporary Git repository and stubbed system calls. No live installation, provider login or provider request was used for these checks.

## October 2, 2026: recovery and performance

Regression tests reproduced and then covered hide/show requests during scheduled collection, bounded lock waiting, cancellation while waiting, retry of failed publication with every provider hidden, expired-reset pace and malformed timestamps. The collector suite passed 74 tests and the integration suite passed 67 under Python 3.9.6, 3.13.16 and 3.14.8, with the same expected `tomllib` skip on 3.9.

The native core passed 32 checks, including concurrent date parsing and locale formatting. ThreadSanitizer reported no races. Date conversion matched Python's datetime results for every day of a Gregorian 400-year cycle and January 1 of each supported year, 156,096 comparisons. Formatting output remained byte-identical for 210 combinations of amounts, units and five locales.

Optimized native benchmarks used identical synthetic inputs before and after the change, one warmup, checksums and 200 samples for the ordinary decode and presentation cases. Large decode used ten samples and publication used 50. These are warmed medians on this development machine, not startup or battery measurements:

| Workload | Before | After |
| --- | ---: | ---: |
| Decode 12 metrics, 3,095 bytes | 2.435 ms | 0.117 ms |
| Decode 4,400 metrics, 832,411 bytes | 519.434 ms | 22.257 ms |
| Format and present 12 metrics | 0.418 ms | 0.063 ms |
| Validate and atomically publish the ordinary snapshot | 2.634 ms | 0.328 ms |

The Python 3.14 collector benchmark measured about 73 ms for four fresh synthetic workers and 1.3 ms for four atomic snapshot writes. Its separate memory run sampled about 159 MiB summed RSS across the process tree, including the sampler; shared pages are counted more than once and short peaks can be missed. Isolated processes remain necessary to terminate blocked workers and their credential tools independently. No provider latency or credential tool was included.

Seven-sample refresh benchmarks with stub publishers measured fast completion at 442 ms before and 365 ms after, with substantial sample variation. A synthetic 100 ms collection deadline stopped at 105 ms after instead of 258 ms before. Timed process waits increase polling; measured fast-case CPU rose from 43 ms to 51 ms. This is a responsiveness tradeoff, not evidence of lower energy use.

Retrying publication after successful no-op collection adds one publisher process. An all-hidden run with a stub simulating the native publisher's two-second reload wait took about 2.26 seconds. Publication still has a synchronous 15-second timeout, so it can delay cancellation or the next collection-deadline check. Process cleanup has its separate bounded waits. No persistent publication acknowledgement state was added.

The synthetic dropdown probe measured unchanged reloads at 2.739 ms before and 0.124 ms after. Across 100 reads, observable publications fell from 200 to zero. A retained hidden dropdown performed two snapshot reads in 11 seconds before and zero after. The hosting controller is retained after its first creation; closing stops snapshot polling and the display clock without discarding provider selection or an ongoing refresh. Widget timeline entry counts remained unchanged.

The final Command Line Tools Release build passed strict signature verification. All 84 synthetic previews were pixel-identical to the previous identity baseline when rendered in the same environment. Seven preview and lifecycle checks passed, including synthetic AppKit close and application-termination callbacks through the real delegate, read-failure recovery, same-size atomic replacements and clock changes. All 63 relative documentation links resolved. Actual user-driven popover events and battery use were not measured.


## October 2, 2026: subscription quota selection

Synthetic quota-only responses reproduced the missing-primary behavior before the change. After adding explicit Claude and Codex allowance priorities, 80 collector tests and 71 integration tests passed under Python 3.9.6, 3.13.16 and 3.14.8, with one expected `tomllib` skip on 3.9. Tests cover zero usage, missing windows, monthly priority, malformed budgets, changes between monthly and quota responses, and retention after failed attempts.

All 35 native core checks, seven preview and lifecycle checks, and the signed Command Line Tools Release build passed. The eight synthetic scenarios rendered 96 images. All 84 existing images remained pixel-identical; the new subscription widget and overview were visually inspected. The subscription overview measured 695 points tall, which scrolled in the 690-point popover of that build; the dropdown was raised to 700 points in the final pass. All 64 relative documentation links resolved. These checks used synthetic data and do not establish coverage of every live subscription tier.

## Remaining verification limits

Full-Xcode builds and XCTest, actual logout/login, physical sleep/wake cycles, and provider links clicked from a desktop widget after closing the app have not been exercised. WidgetKit controls redraw timing; a 15-minute collection schedule does not guarantee a matching redraw interval. Preview renders exercise SwiftUI views outside WidgetKit and cannot verify macOS service delivery or desktop widget materials.

## October 2, 2026: consolidated publication tree

The combined security, accessibility, Meter identity, recovery, performance and subscription-quota changes, followed by a final review pass the same day, passed 87 collector tests and 87 integration tests under Python 3.9.6 and 3.14.8, with one expected `tomllib` skip on 3.9. All 35 native core checks and seven preview and lifecycle checks passed. The Command Line Tools Release build passed strict signature verification, and the portability review compiled the app and widget with Swift 5.9.2, 5.10.1 and 6.0.3 against the macOS 14.0, 14.4 and 15.2 SDKs on arm64 and x86_64. The final pass fixed a worker-cleanup race that could abort a collection run, a Swift closure capture that failed to compile with Swift 5.9 and 5.10, a Keychain-missing status that showed sign-in steps instead of setup steps, uninstall leftovers (Launch Services records and the app's preferences file), and raised the dropdown to 440 × 700 points so the subscription overview no longer scrolls.

## October 2, 2026: uninstall ownership check

Isolated tests reproduced uninstall proceeding against another account's app through direct Python invocation, bootstrap delegation to a checkout and the shell fallback. Ownership checks now stop all three routes before any system-tool call or file change, with and without `--keep-data`. Five added tests cover those refusals, an app symlink and an absent app. Ownership is simulated; these checks do not run under a second macOS account or invoke real registration or launchd operations.

All 87 collector tests and 92 integration tests passed under Python 3.9.6, 3.13.16 and 3.14.8, with one expected `tomllib` skip on 3.9. The 35 native core checks, seven preview and lifecycle checks, shell syntax check, Command Line Tools Release build and strict signature verification passed. All 60 relative Markdown link paths checked resolved. No UI code changed, so image renders were not repeated.
