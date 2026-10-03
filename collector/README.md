# Bars collector

Python 3.9 or later is required. The installer checks the selected interpreter, including the Python supplied by Apple's Command Line Tools at `/usr/bin/python3`. Collection uses only the standard library and runs once per invocation. No collector process remains between runs.

```sh
PYTHONPATH=collector python3 -m bars_collector --output "$HOME/Library/Application Support/Bars/snapshot.json"
PYTHONPATH=collector python3 -m bars_collector --provider devin
PYTHONPATH=collector python3 -m bars_collector --disable cursor --disable codex
PYTHONPATH=collector python3 -m bars_collector --detect
PYTHONPATH=collector python3 -m unittest discover -s tests/collector -v
```

The suites run under both `/usr/bin/python3` and a current CPython. Avoid standard-library features newer than 3.9, such as `tomllib`, `match`, `datetime.UTC` and `zip(strict=)`. Use `X | Y` annotations only after `from __future__ import annotations`. Python 3.9's `datetime.fromisoformat` accepts only 3- or 6-digit fractions and colon offsets. `model.timestamp` normalizes other fraction lengths, a comma separator and `+HHMM` offsets first, so every supported interpreter reads provider timestamps the same way.

The scheduler supplies the Python executable, module search path, and a PATH containing the Devin CLI. It starts the collector in the installed package directory, so files in the scheduled job's working directory cannot shadow standard-library modules. `--timeout` sets each provider's hard deadline in seconds, from 5 to 120, with a default of 40. Socket requests also have a 12-second timeout. Four isolated worker processes allow independent completion and enforce the deadline even during DNS resolution or a stalled response.

Processes consume more transient memory than threads. They allow the coordinator to terminate one blocked provider without delaying publication from the others. Python cannot safely terminate an individual blocked thread. The macOS `spawn` method also avoids forking a process after libraries may have started threads. All workers exit after collection.

Each worker owns a separate process group. Completion, deadline expiry, and cancellation terminate that entire group, including credential tools and CLI subprocesses. Cleanup sends TERM followed by KILL with at most half a second of joins per worker. SIGTERM to the coordinator triggers the same cleanup before exiting with code 143. An external supervisor should allow at least three seconds for this cleanup before forcing coordinator termination.

Each completion atomically replaces the versioned snapshot. Failed attempts retain the affected provider's previous values and successful fetch time. `--provider` refreshes one provider and retains the other three. The snapshot and persistent lock file have mode 0600, and neither is read or opened through a symlink. A newly created output directory has mode 0700. The collector does not change permissions on an existing directory; the installer creates and tightens `~/Library/Application Support/Bars` to 0700.

## Disabled providers and detection

`--disable ID` is repeatable. A disabled provider starts no worker. Its snapshot entry keeps its retained values, status, message and timestamps, and gains `enabled: false`. Every run sets each provider's flag from the `--disable` list it receives. The scheduled runner reads that list from `installation.json` on every run. `--provider ID` for a disabled provider is a successful no-op, apart from writing a changed flag.

`--detect` prints one JSON object of four booleans and exits without network requests or file writes. It runs the adapters' own local lookups. Claude and Cursor need their Keychain items, checked with `security find-generic-password` without `-w`, so no password data is requested; macOS controls any access prompts. Codex needs a ChatGPT login in `~/.codex/auth.json`, and Devin needs both its credential file and the `devin` CLI on `PATH`. A lookup that collection would report as `setup` counts as not found. Unreadable or invalid material counts as found, because collection then reports `login_required` and the person should see it. The installer uses this to choose which providers start disabled.

Exit code 0 means the snapshot represents the completed attempts, including any provider failures. Exit code 75 means another writer already holds the output lock. Exit code 1 means an infrastructure error prevented reading or publishing the snapshot. A corrupt existing snapshot is preserved and collection exits with code 1; move it aside deliberately before retrying. Standard output reports provider statuses only.

## Existing logins

- Claude reads the `Claude Code-credentials` macOS Keychain entry. A missing entry (`security` exit status 44) reports `setup`; other lookup failures, such as denied access, report `login_required`.
- Codex reads `~/.codex/auth.json`. It intentionally ignores `CODEX_HOME` so a coding agent's temporary configuration cannot select a different account.
- Cursor reads the `cursor-access-token` Keychain entry and constructs the vendor's usage session cookie. A missing entry reports `setup` (install the Cursor CLI and run `agent login`); other lookup failures report `login_required`.
- Devin reads `~/.local/share/devin/credentials.toml` and discovers its organization through `devin cloud drs whoami`. Python 3.9 has no `tomllib`, so `toml_subset.py` reads the flat subset this file uses. That subset is blank lines and comments, `[table]` headers, bare keys and basic strings with TOML escapes, decimal integers and booleans. Anything else is rejected as an unreadable login (`login_required`), the same path a `tomllib` decode error took. Its errors give line numbers, never file content.

The collector never writes, refreshes, or rotates credentials. The Devin CLI, which it runs only for `devin cloud drs whoami`, manages its own login as it does when you use it. Provider URLs are fixed in the adapters; the only variable part, Devin's organization, must match `[A-Za-z0-9_-]{1,128}`. Credentials travel only in request headers, never in URLs.

Each enabled provider gets at most one usage request per run, with no retries. Certificate and hostname verification use an explicit default TLS context with TLS 1.2 as the minimum version. Every redirect status (301, 302, 303, 307 and 308) fails the request instead of being followed, so a credential never reaches a redirect target. Responses are limited to 1 MiB and sockets time out after 12 seconds. The `User-Agent` is `Bars/0.1`. Proxies configured in the environment or macOS network settings apply. A TLS-intercepting proxy with a trusted certificate can read requests.

`/usr/bin/security` and the Devin CLI run from argument lists, never a shell, with standard input closed. They receive only `HOME`, `PATH`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, the locale variables, `__CF_USER_TEXT_ENCODING` and the proxy and CA settings. Other variables, such as unrelated API keys or `XDG_*` and `DEVIN_*` overrides that could make the CLI report a different login than the file Bars reads, are withheld. Standard output is limited to 1 MiB while reading; standard error is discarded. Neither stream is printed or logged. The installer detection process and scheduled collector also receive an allowlisted environment.

API failures and unexpected exceptions produce fixed user-safe messages; only those messages reach the snapshot, never exception text. Raw responses, account identifiers, credentials, and actual amounts are not printed. The scheduled runner discards the collector's standard error. It reads at most 4 KiB of standard output and logs it only when it is exactly the provider status line; any other output is logged as a fixed 'not recognized' line. Its log under `logs/refresh.log` holds only fixed status lines, provider IDs, provider statuses and durations.

## Source semantics

Claude's budget currency and exponent come from each money field. Its scope remains unknown because the response does not establish whether the budget is personal or organizational. Codex prefers the individual credit cap. When no usable monthly allowance is reported, Claude selects its 5-hour quota, then weekly quota, then weekly Opus or Sonnet quota; Codex selects its session quota, then weekly quota. Selection uses reported quantities, including zero, and preserves each allowance's label and units. It does not infer an account tier or turn a quota into money. A malformed response remains a failure rather than triggering quota selection. Cursor's usage endpoint reports monetary amounts in USD cents; individual and team allowances stay separate.

Devin's on-demand balance is a team balance in USD. The quota endpoint supplies no genuine total, used amount, or reset date for that balance. Its daily and weekly quotas remain percentages with unknown scope. Daily quota is omitted when the source sets `hide_daily_quota`. The parser does not estimate missing amounts, reset boundaries, or primary metrics from unrelated allowances.

Each metric carries its documented allowance period (`five_hours`, `day`, `week`, `month`) when the source defines one: Claude's monthly budget, Codex's monthly credits and Cursor's billing-cycle allowances are monthly; Claude's 5-hour and weekly quotas, Codex's weekly quota and Devin's daily and weekly quotas use their names. Codex's session window carries no period because its length is not reported. The native reader derives pace from the period, the reset instant and the fetch time.

The worker contract is documented in [the snapshot contract](../docs/snapshot-contract.md). Publication into WidgetKit's readable storage is a separate native operation.

## Initial resource measurement

On the development machine (macOS 26) with CPython 3.14.7, one successful four-provider run on 2026-10-01 took 0.63 seconds elapsed, 0.29 seconds user CPU and 0.13 seconds system CPU. `/usr/bin/time -lp` reported a maximum resident set size of 40.3 MiB; that value is not the concurrent process tree's total memory.

A separate run sampled the collector and its descendants with `ps`. The highest sampled sum of resident set sizes was 183.6 MiB across seven processes, including the multiprocessing helper and the Devin CLI. The sum double-counts shared pages, and sampling can miss short peaks. The 35 samples took 1.52 seconds including polling overhead. These are single-run measurements, not a battery-use benchmark. No collector processes remain between scheduled runs.
