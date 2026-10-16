# Security and privacy

Bars reads existing provider logins to collect usage. Installing it gives locally built code access to the same user account as the provider clients. Review the source before installing, and use only a repository and branch you trust.

## Credentials and requests

The standard plugins read the `Claude Code-credentials` and `cursor-access-token` Keychain items through `/usr/bin/security`, the ChatGPT login in `~/.codex/auth.json` and the Devin CLI login in `~/.local/share/devin/credentials.toml`. Scheduled collection reads only enabled providers. Installation detection asks each standard plugin locally and discards the login material without making usage requests.

The collector itself never writes, refreshes or rotates these logins. For Devin it also runs `devin cloud drs whoami` to discover the organization. That external client makes its own requests and may manage its own login; its behavior is outside Bars' control. Keychain access may prompt. Choosing Always Allow permits other processes running as the same user to read that item through `/usr/bin/security`.

Each enabled standard plugin sends its usage requests only to its fixed HTTPS host: `api.anthropic.com`, `chatgpt.com`, `cursor.com` or `app.devin.ai`. A refresh runs about once a minute and makes one request per attempt; a transient failure is retried a few times with backoff within that minute, a rejected login is not retried. Each credential appears in one authentication header, and every redirect is refused. HTTPS requires TLS 1.2 or later with certificate and hostname verification; the shared request helper refuses plain `http` destinations other than loopback. Proxy settings from the environment or macOS apply, as do certificate-authority settings from the environment. A trusted TLS-intercepting proxy can read requests.

Requests have a 12-second socket timeout and a 1 MiB response limit. Each provider worker also has a total deadline (55 seconds by default) and runs in its own process group so timeouts and worker exits can clean up the descendants in that group; a child that detaches into its own session is outside this containment, which is why the plugin interface forbids starting one. Within the deadline a worker retries a transient error with exponential backoff; a later scheduled or manual refresh tries again after that. Credential tools run without a shell, with closed standard input and an allowlisted environment.

These controls are implemented in the plugin SDK, [bars_plugin/access.py](collector/bars_plugin/access.py), and in [runner.py](collector/bars_collector/runner.py). [Collector security tests](tests/collector/test_security.py) exercise fixed endpoints, credential placement, redirect refusal, TLS verification, response limits and child-process isolation.

## Provider plugins

Providers are [plugins](docs/plugins.md): directories under `~/Library/Application Support/Bars/plugins` containing a manifest and a Python module. The coordinator reads only the manifest; the module runs in a separate worker process with the same deadline, process-group cleanup, allowlisted environment and discarded output as the standard plugins, and its result is bounded and validated before it can change the snapshot. That contains faulty plugins: one that hangs, crashes, prints or returns nonsense costs its own provider one attempt and cannot alter another provider's entry, its own name or setup text, or the enabled flags. It does not contain hostile plugins. A plugin runs with your account's access, including your provider logins, like any script you choose to run; read a plugin before installing it, and treat the plugins directory with the same care as the source checkout. The installer refreshes the standard plugins on update but never overwrites a file you edited, never touches plugins you added, and records what it wrote in `plugins/.installed.json`.

## Storage and other traffic

Bars saves usage amounts, reset times and statuses in `~/Library/Application Support/Bars`. This directory is mode 0700; snapshots, settings and logs are 0600. Snapshots and collection logs omit credentials, raw response bodies, and account and organization IDs. Failed refreshes keep the last successful usage values and use fixed error messages. Private usage amounts remain sensitive even when they contain no account identifiers. File permissions and symlink checks do not isolate Bars from other software running as the same user.

The sandboxed widget has read-only access to the published `widget-snapshot.json` file. It has no provider credentials or network entitlement. The unsandboxed menu bar app controls refreshes and settings. macOS also maintains widget containers, cached timelines and system logs outside the Bars directory. File publication and validation are covered by [storage.py](collector/bars_collector/storage.py), [SnapshotStore.swift](macOS/Shared/SnapshotStore.swift) and [Swift storage checks](tests/Swift/SnapshotTests.swift).

Bars has no telemetry, analytics or automatic update check. Installing or updating downloads a bootstrap script and fetches the source repository. Apple's tools installer may download prerequisites. The Devin CLI contacts its own service, and provider links open in a browser when clicked.

The [uninstall command](README.md#uninstalling) removes the app, its preferences, the LaunchAgent and Bars' data directory, unless `--keep-data` preserves the latter. It leaves provider logins and the macOS-managed widget sandbox alone. [Installer tests](tests/integration/test_refresh.py) use isolated destinations and stub system calls; [bootstrap tests](tests/integration/test_bootstrap.py) use a temporary Git repository.

## Trust and updates

Anyone who can change the selected repository or branch controls code executed at the next install or update. Downloading the complete bootstrap before running it prevents execution after a failed partial download; it does not authenticate a compromised repository. Updates require running the bootstrap again. Scheduled collection runs the installed copy without fetching new code.

Repository protection and maintainer account security are external settings, not guarantees made by this source tree. They are owner actions: protecting the default branch against force pushes, requiring review for changes, enabling two-factor authentication on the maintainer account and enabling private vulnerability reporting. Check the current repository settings if those controls matter to your decision to install.

## Reporting a vulnerability

When enabled, use [GitHub private vulnerability reporting](https://github.com/kenny-linktree/bars/security/advisories/new). If the private report form is unavailable, open an issue asking for a private reporting channel without including vulnerability details. Do not include real tokens, account IDs, usage figures, raw provider responses or live preview images. A minimal reproduction using synthetic data or a failing test helps establish the problem.
