# Troubleshooting

Known failure modes, how to inspect what the widget and collector actually did, and capture techniques for the menu bar dropdown. The investigations behind these notes are recorded in [verification.md](verification.md).

## First checks

```sh
"$HOME/Library/Application Support/Bars/bin/refresh"                    # refresh every enabled provider
"$HOME/Library/Application Support/Bars/bin/refresh" --provider devin   # refresh one provider
launchctl print "gui/$(id -u)/local.bars.collector"                      # scheduled job state
/Applications/Bars.app/Contents/MacOS/Bars --reload-widgets              # request a new widget timeline
```

`~/Library/Application Support/Bars/logs/refresh.log` records collection and publication failures and, after each run, the collector's provider statuses (for example `Provider statuses: claude: ok; codex: setup; cursor: ok; devin: disabled.`). Provider-specific errors appear in the widget and dropdown. A successful runner exit can include a provider failure whose previous values were retained, so check the provider's status as well as the exit code.

## Widget shows old UI or data while the dropdown is current

The WidgetKit extension process survives bundle replacement and keeps rendering timelines with the previous binary. Reinstall with `scripts/install.py` or the bootstrap, which end the stale extension and app processes and request a reload. To do it by hand, run `pkill -f BarsWidget.appex` followed by `/Applications/Bars.app/Contents/MacOS/Bars --reload-widgets`.

To check, compare the extension's start time with the last install: `ps -p "$(pgrep -f BarsWidget.appex)" -o lstart=`. A start time before the install means stale code.

## Desktop widget looks gray while the dropdown is colored

macOS desaturates desktop widgets whenever another app's window is frontmost. This is not a stale-binary symptom. Compare text, such as pace lines and status labels, rather than color when checking the widget. For the same reason, widget UI must never use color as the only carrier of state.

## Confirm what the widget actually rendered

The system archives each timeline the extension produces. Search it for text you expect:

```sh
strings ~/Library/Containers/local.bars.app.widget/Data/SystemData/com.apple.chrono/timelines/BarsOverview/*.chrono-timeline \
  | grep -c '<expected text>'
```

The archive's modification time is the last successful timeline reload.

## WidgetKit reload decisions

chronod logs reload requests, budget exemptions and extension launches to the unified log:

```sh
/usr/bin/log show --last 2h --info --predicate 'process == "chronod" AND eventMessage CONTAINS "local.bars"'
```

In zsh, `log` is a shell builtin, so use the full path. Observed during development: `log show` started from a sandboxed process returned nothing for any process, so run it from Terminal.

## Tracing without the unified log

Where `NSLog` output cannot be retrieved, the unsandboxed host app can append one-off traces to a file under `/tmp`. The sandboxed widget can write to `NSTemporaryDirectory()`, which resolves to `~/Library/Containers/local.bars.app.widget/Data/tmp/`. Remove such tracing before committing.

## Dropdown never appears

Two causes were found and fixed in `macOS/App/BarsApp.swift`. A transient `NSPopover` closed within a second whenever the accessory app lost active status, so dismissal is application-defined with an outside-click monitor and Escape. At a URL-triggered cold start the status item's window has a zero-height frame until the status bar lays it out, so the open waits for a non-zero frame. Regressions in either area show up as a dropdown that flashes or never opens.

## Screenshots and window capture

- Observed on the development machine: `screencapture -D <n>` omitted the status-item popover even while it was on screen. `screencapture -x out1.png out2.png` (one file per display) and `screencapture -l <windowNumber>` both captured it.
- `CGWindowListCopyWindowInfo` supplies window numbers and bounds. The desktop widget is a Notification Center window named `Bars`. A short Swift program can list windows; cast the result through `NSArray` and `NSDictionary`, because an `as! [[String: Any]]` cast crashed. System Python has no PyObjC, so it cannot call this API directly.
- The popover is not a window to System Events, so AppleScript cannot click controls such as **All providers**. Use `open bars://provider/<id>` to open a provider's details.
- For layout checks that do not need the real host, use the [preview renders](development.md#preview-renders) instead.
