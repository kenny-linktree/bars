# Bars

<img src="docs/images/icon-256.png" width="128" alt="Bars icon: green, orange and red meters on a porcelain tile">

Bars is a macOS desktop widget and menu bar item that shows how much of your Claude, Codex, Cursor and Devin allowances you have used. One overview shows each provider's used and remaining amounts, a pace marker against the allowance period and the reset time. It reuses the logins of the providers' own clients, collects in the background every 15 minutes, and stores the resulting usage snapshots on your Mac. Collection contacts the providers directly; Bars has no telemetry or server of its own.

<p>
  <img src="docs/images/widget-dark.png" width="344" alt="The Bars desktop widget in dark appearance, showing usage bars with pace markers for Claude, Codex, Cursor and Devin">
  <img src="docs/images/dropdown-light.png" width="330" alt="The Bars menu bar dropdown in light appearance, showing the same four providers with reset times, a Refresh now button and a Quit Bars button">
</p>

The screenshots are preview renders of synthetic heavy-use data, not a real account.

## What it shows

- Claude and Codex support reported monthly budgets or credits and personal subscription quotas. Without a usable monthly allowance, the overview shows a session or weekly quota with its own label. The other general quota window appears beneath it; all reported allowances remain in details. Selection follows the response from the current login, without a plan setting. Coverage is best effort and has not been verified against every subscription tier.
- Bars fill as usage increases. A tick on each bar marks how much of the allowance period had elapsed at the last fetch, and a line beneath says whether usage is on pace, ahead of pace with a projected run-out date, or already used up.
- Devin shows its remaining on-demand balance without inventing a total; its daily and weekly quotas appear as labeled mini bars beneath it. Cursor's individual and team amounts stay separate.
- Failed refreshes keep the last successful values and show their age. Reset dates use the Mac's local time zone. Claude's monthly reset is calculated from its documented schedule and marked `(est.)`.
- Bars has no Dock icon or window. Its three-bar Meter icon in the menu bar opens a dropdown with the same overview; click a row, in the dropdown or the widget, for that provider's allowances, reset times and a manual refresh. Quit from the dropdown footer or with ⌘Q while the dropdown is open.
- Providers you do not use can be hidden. A provider with no login shows setup steps instead of a bar.

## Requirements

- macOS 14 or later. Installation has been verified on macOS 26.
- Apple's Command Line Tools, version 15 or later (Swift 5.9). If they are missing, the installer opens Apple's installer and asks you to run it again when that finishes.
- Existing logins for the providers you use, made with each provider's own client.
- An account that can write to `/Applications`, and one installing account per Mac: `/Applications/Bars.app` belongs to whoever installed it first, and a second account's install or uninstall stops with a message instead of touching it. macOS shows a "Background Items Added" notice for the collector's scheduled job; turning that item off in Login Items stops collection. On a standard (non-administrator) account the installer stops before changing Bars' storage, the app or the scheduled job, and no Bars script runs `sudo` or asks for an administrator password. Apple's Command Line Tools installer, if it opens, is a separate macOS dialog that may ask for an administrator's approval.

Homebrew, full Xcode and a paid Apple Developer membership are not needed.

## Install

Paste this into Terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/kenny-linktree/bars/main/scripts/bootstrap.sh -o ~/Downloads/bars-bootstrap.sh && sh ~/Downloads/bars-bootstrap.sh
```

When it finishes, add the widget: right-click the desktop, choose **Edit Widgets**, search for Bars and add the large widget.

The script clones the repository into `~/Library/Application Support/Bars/src`, builds the app and widget on your Mac with the Command Line Tools, copies Bars to `/Applications`, starts a per-user LaunchAgent for background collection and launches the menu bar app. The locally built app is ad-hoc signed, without Developer ID signing or notarization. It normally has no download quarantine attribute; local security policy may still prevent execution. [docs/distribution.md](docs/distribution.md) describes each step and [ADR 0005](docs/adr/0005-distribute-by-source-bootstrap.md) explains the choice.

The command saves the complete script to your Downloads folder before running it, so a failed or partial download runs nothing. To read the script first, run the `curl` part on its own, then the `sh` part. You can delete the downloaded copy afterwards. To pass options on the first install, add them after the second `bars-bootstrap.sh`; `--help` lists them. Later commands can use the copy in the installed source clone, as shown below. `BARS_REPO_URL` and `BARS_REF` (a branch) select a different repository or branch.

## What Bars reads on your Mac

| Provider | Login Bars reads | Usage endpoint it calls |
| --- | --- | --- |
| Claude | `Claude Code-credentials` item in the macOS Keychain | `api.anthropic.com` |
| Codex | ChatGPT login in `~/.codex/auth.json` | `chatgpt.com` |
| Cursor | `cursor-access-token` item in the macOS Keychain | `cursor.com` |
| Devin | Devin CLI login at `~/.local/share/devin/credentials.toml`; organization from `devin cloud drs whoami` | `app.devin.ai` |

Bars' collector reads these logins without writing, refreshing or rotating them. It sends each credential in a request header to that provider's fixed usage endpoint over certificate-verified HTTPS and refuses redirects. Proxy settings from the environment or macOS apply, as do certificate-authority settings from the environment. A trusted TLS-intercepting proxy can read requests. The external Devin CLI performs its own organization lookup and may manage its own login. Bars does not control that client's behavior.

Bars stores usage amounts, reset times and statuses, without credentials, account IDs or raw response bodies, under `~/Library/Application Support/Bars`. That directory is mode 0700; snapshots, settings and logs are 0600. Installation also creates `/Applications/Bars.app` and `~/Library/LaunchAgents/local.bars.collector.plist`, and registers the app and widget with macOS. macOS maintains its own widget sandbox, caches and logs outside the Bars directory.

Installing or updating downloads the bootstrap script and fetches the Git repository. Apple's tools installer may download prerequisites. During collection, the collector calls only the four usage endpoints; the Devin CLI also contacts its service. Provider links open in your browser when clicked. There is no telemetry or automatic update check.

Reading a Keychain item can make macOS ask whether to allow `security` access. Choosing Always Allow permits other processes running as your user to read that item through `/usr/bin/security`; choose Allow if you want a prompt each time. If a login expires, sign in through the provider's client, then refresh Bars. See [collector/README.md](collector/README.md) for adapter behavior and account-scope limits, and [SECURITY.md](SECURITY.md) for the trust model and vulnerability reporting.

Bars is not affiliated with or endorsed by Anthropic, OpenAI, Cursor or Cognition. The usage endpoints it calls are the ones the providers' own clients use; they are not published APIs and may change without notice.

## Choosing providers

By default the installer checks which providers have a local login, using the lookups above without any network request, and disables the rest. It then prints one line per provider, such as `Cursor: no login found, disabled`. A disabled provider is not collected and is hidden from the widget and overview. Its row remains in the snapshot with any values it last had.

```sh
sh "$HOME/Library/Application Support/Bars/src/scripts/bootstrap.sh" --disable cursor --disable codex
sh "$HOME/Library/Application Support/Bars/src/scripts/bootstrap.sh" --enable-all
```

`--disable` and `--enable-all` set the selection explicitly. Without either option, a first install disables providers with no local login, and an update keeps the existing selection, including changes made in the dropdown. Providers can also be hidden and shown again from the Bars dropdown, which saves the choice in `installation.json`. Every refresh reads that file, whether scheduled or manual.

## Updating

Run the install command again. It fetches the latest `main`, rebuilds and reinstalls, keeping your provider selection. The copy in the installed clone accepts the same options and updates the same way:

```sh
sh "$HOME/Library/Application Support/Bars/src/scripts/bootstrap.sh"
```

`--build-only` builds without installing. `--no-start` installs without loading the scheduled job in the current login session. It launches the new app only if Bars was already running; the LaunchAgent starts collection at the next login. Updating replaces tracked changes in the managed source clone, so use a separate checkout for development.

## Uninstalling

```sh
sh "$HOME/Library/Application Support/Bars/src/scripts/bootstrap.sh" --uninstall
# Keep ~/Library/Application Support/Bars, including the source clone:
sh "$HOME/Library/Application Support/Bars/src/scripts/bootstrap.sh" --uninstall --keep-data
```

If the installed copy of the script is missing, run the install command above with `--uninstall` after the second `bars-bootstrap.sh`; uninstalling fetches and builds nothing. From a checkout, `python3 scripts/install.py --uninstall` does the same.

Uninstalling stops and removes the LaunchAgent and ends running Bars and widget processes. It then unregisters the app and widget from Launch Services and the widget gallery and removes `/Applications/Bars.app`, the app's preferences (`~/Library/Preferences/local.bars.app.plist`) and, unless you pass `--keep-data`, `~/Library/Application Support/Bars`, including the source clone. It reports each removal and is safe to run again. It never touches provider logins. macOS keeps the widget's own sandbox container in `~/Library/Containers/local.bars.app.widget`.

## How it works

- **Collection.** The LaunchAgent runs at login and at minutes 00, 15, 30 and 45, and macOS resumes missed runs after wake. Collection continues when the menu bar app is closed. Each run starts short-lived Python workers, one per enabled provider, publishes each completed provider independently and exits. A lock prevents overlapping runs.
- **Publication.** The collector writes a private `snapshot.json`. The native publisher validates it and atomically replaces `widget-snapshot.json`, both under `~/Library/Application Support/Bars`, with mode 0600. The sandboxed widget has read-only access to that exact file, with no credential access or provider network requests. Because App Group access failed under actual WidgetKit hosting without team signing, the ad-hoc signed build uses this scoped file exception instead. See the [storage decision](docs/adr/0004-publish-one-file-for-the-personal-widget.md) and the [snapshot contract](docs/snapshot-contract.md).
- **Display.** WidgetKit controls when the widget redraws, so collection every 15 minutes does not guarantee a redraw every 15 minutes. The headers show when the latest fetch succeeded, and a row shows its own age when it differs. Stale values remain visible, and retained values from an expired allowance period are labeled.

The dropdown's **Refresh now** refreshes all providers or one provider. If another refresh is running, explicit refresh and hide/show requests wait up to 150 seconds before reporting a failure. Settings are read after the wait so the requested provider selection is applied. Collection and publication failures are logged to `~/Library/Application Support/Bars/logs/refresh.log`; [docs/troubleshooting.md](docs/troubleshooting.md) covers diagnosis, including a widget that lags behind the dropdown.

The agreed behavior is recorded in [docs/design.md](docs/design.md), the vocabulary in [CONTEXT.md](CONTEXT.md) and the decisions in [docs/adr](docs/adr/).

## Development

### Build

From a checkout:

```sh
scripts/build.sh --toolchain clt
/usr/bin/python3 scripts/install.py \
  --app .build/native/Release/Bars.app \
  --python /usr/bin/python3
```

`--python` is the interpreter the scheduled collector uses, so give a stable path. `scripts/build.sh` also accepts `--toolchain auto|xcode`, `--configuration Debug|Release` and `--output DIR`. Full Xcode is optional. `scripts/install.py` accepts the same `--disable`, `--enable-all`, `--no-start` and `--uninstall [--keep-data]` options as the bootstrap.

### Tests

These checks use synthetic responses and isolated temporary files, without provider credentials. Run the Python suites under both `/usr/bin/python3` (the 3.9 floor) and a current CPython:

```sh
PYTHONPATH=collector python3 -m unittest discover -s tests/collector -v
python3 -m unittest discover -s tests/integration -v
sh -n scripts/bootstrap.sh
scripts/preview/render.sh --check
swiftc macOS/Shared/Snapshot.swift \
  macOS/Shared/UsagePresentation.swift \
  macOS/Shared/SnapshotStore.swift \
  tests/Swift/SnapshotTests.swift -o /tmp/bars-core-checks
/tmp/bars-core-checks
```

The direct Swift command works with the Command Line Tools, which omit XCTest. The package also provides an XCTest path through `swift test` when full Xcode is selected; that path and the Xcode build have not been exercised yet. [docs/verification.md](docs/verification.md) records tested results and remaining limits.

### Preview renders

`scripts/preview/render.sh` renders the widget, the dropdown overview and each provider's details to PNG files at 2x, in light and dark appearance, without installing the app:

```sh
scripts/preview/render.sh                        # synthetic variants into .build/previews/
scripts/preview/render.sh --variant mixed --only widget --output /tmp/previews
scripts/preview/render.sh --widget-size 344x344  # the installed widget's measured size
```

The default variants are `stale`, `mixed`, `heavy`, `previous`, `empty`, `setup` (missing logins) and `hidden` (disabled providers). They use fixed synthetic data. Pass `--variant live` explicitly to render the locally published snapshot; those images contain private usage figures and must not be committed or shared. Renders are not hosted by WidgetKit, so desktop tinting, popover materials and relative-date layout differ from the installed app. [docs/development.md](docs/development.md) has more detail, including the overview layout mirror that must be kept in step with the app.

## Contributing

Issues and pull requests are welcome. Please open an issue to discuss a behavior change before starting on it. A pull request that changes behavior should update [docs/design.md](docs/design.md) in the same change, along with [CONTEXT.md](CONTEXT.md) for any new term, and should pass the tests above. Coding agents should start with [AGENTS.md](AGENTS.md).

Report security problems privately as described in [SECURITY.md](SECURITY.md), not in a public issue. Issues and pull requests must not contain real tokens, account IDs, usage figures or live preview renders.

## License

[MIT](LICENSE)
