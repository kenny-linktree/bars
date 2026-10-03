# Distribution

How Bars reaches a Mac: the clone-and-build model, what the bootstrap script and installer do, how provider selection persists, and how to uninstall. The decision and its trade-offs are recorded in [ADR 0005](adr/0005-distribute-by-source-bootstrap.md).

## Clone and build, not a binary

No prebuilt app is published. The [README install command](../README.md#install) downloads `scripts/bootstrap.sh` completely before executing it. The script clones the repository, builds the app and widget with the Command Line Tools and runs `scripts/install.py`. The locally compiled app is ad-hoc signed and normally has no download quarantine attribute. It has no Developer ID signature or notarization; local security policy can still prevent execution.

The repository and branch are the trust boundary: each Mac builds whatever it fetched.

## Repository

`scripts/bootstrap.sh` and the README install from `kenny-linktree/bars`. A fork changes that default in both places. `BARS_REPO_URL` and `BARS_REF` (a branch) override the repository and branch without editing the script.

## What the bootstrap does

1. Refuses to run as root, validates its options and environment before fetching anything, and checks the macOS version (14 or later) and the Command Line Tools. If the tools are missing it opens Apple's installer and asks to be run again afterwards.
2. Clones the repository into `~/Library/Application Support/Bars/src`, or fetches and hard-resets an existing clone to the latest commit of the branch. It refuses a data directory that is a symbolic link or owned by another user, and a source directory that is not a Git checkout.
3. Chooses the Command Line Tools' `/usr/bin/python3` (Python 3.9 or later), falling back to `/opt/homebrew/bin/python3` or `/usr/local/bin/python3` only if that Python is unusable.
4. Builds the app and widget with `scripts/build.sh --toolchain clt`, using an ad-hoc signature. `--build-only` stops here.
5. Runs `scripts/install.py`, passing every other option through.

The bootstrap checks that the account can write to `/Applications` before fetching or building, and the installer repeats that check and also refuses to replace a `Bars.app` owned by another account, so a standard account or a second user changes nothing. The previous bundle is unregistered from Launch Services and the widget gallery before its staged copy is deleted, so no registration names a removed path. The installer waits up to 160 seconds for a running refresh, and each install empties `logs/launchd.log`. It then copies Bars to `/Applications`, installs the collector under `~/Library/Application Support/Bars`, registers the widget, starts the per-user LaunchAgent and launches the menu bar app. After the bundle swap it ends any widget extension or Bars process still running a previous build and requests a fresh widget timeline, while holding the refresh lock, so later widget timelines use the new binary. WidgetKit still controls when the desktop redraws. See [Troubleshooting](troubleshooting.md#widget-shows-old-ui-or-data-while-the-dropdown-is-current) for why.

`--no-start` installs without loading the scheduled job or launching the app in the current login session; the LaunchAgent remains installed for future logins. If Bars was already running, the installer still relaunches it on the new build.

## Provider selection

A first install asks the collector (`python3 -m bars_collector --detect`, which makes no network request) which providers have local login material, disables the rest and prints one line per provider. If detection itself fails, every provider stays enabled rather than one being hidden by mistake.

`--disable ID` (repeatable) and `--enable-all` set the selection explicitly. Without either, a reinstall keeps the existing `disabled_providers` list in `~/Library/Application Support/Bars/installation.json`, including changes made from the dropdown. Every refresh reads that file after acquiring its lock. Explicit native refreshes wait up to 150 seconds for active work, so hide/show changes made during a scheduled run are applied by the subsequent request.

## Uninstall

Run uninstall from the account that installed Bars. Both entry points refuse an existing app owned by another account before changing jobs, processes, registrations, preferences or data, including with `--keep-data`. An app-path symlink is left in place, and an absent app permits cleanup of the remaining per-user files.

`bootstrap.sh --uninstall [--keep-data]` or `scripts/install.py --uninstall [--keep-data]`. Uninstalling stops and removes the LaunchAgent, ends running Bars and widget processes, unregisters the app and widget from Launch Services and the widget gallery, and removes `/Applications/Bars.app`, the app's preferences (`defaults delete local.bars.app`, then `~/Library/Preferences/local.bars.app.plist` if it remains) and, unless `--keep-data` is given, `~/Library/Application Support/Bars` including the source clone. If no checkout or usable Python is present, the bootstrap script performs the same removal itself. Provider logins are never touched. macOS keeps the widget's own sandbox container at `~/Library/Containers/local.bars.app.widget`.

## Python floor

The collector and installer run on Python 3.9 or later so the verified Command Line Tools interpreter, Python 3.9.6, works without Homebrew. See [Development](development.md#python-compatibility) for the compatibility rules and test matrix.
