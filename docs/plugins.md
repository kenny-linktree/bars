# Provider plugins

Every provider Bars shows is a plugin: a directory with a manifest and one Python module. Claude, Codex, Cursor and Devin are the standard plugins shipped in [`plugins/`](../plugins/) and installed the same way as any other. To add a provider, write a plugin into your plugins directory; no change to Bars and no pull request is needed.

## Where plugins live

```
~/Library/Application Support/Bars/plugins/
├── .installed.json        what the installer last wrote, for update decisions
├── claude/                standard
├── codex/                 standard
├── cursor/                standard
├── devin/                 standard
└── my_provider/           yours
    ├── plugin.json
    └── plugin.py
```

The directory name is the plugin id: lowercase ASCII letters, digits and underscores, at most 32 characters, starting with a letter. The collector reads the directory on every run, so a new plugin appears at the next refresh (choose Refresh now in the dropdown to see it at once) and a removed one disappears. Providers are listed in ascending id order. Directories with other names, and loose files, are ignored.

Plugins can be hidden like any provider, from the dropdown or with `--disable ID`.

## `plugin.json`

```json
{
  "manifest_version": 1,
  "id": "my_provider",
  "name": "My Provider",
  "usage_url": "https://example.com/account/usage",
  "setup": {
    "install_title": "Install the Example CLI",
    "install_url": "https://example.com/docs/cli",
    "login_command": "example login",
    "login_step": "Sign in from Terminal:",
    "sign_in_again": "Run this in Terminal and sign in again:",
    "credential_note": "Bars reads the login that the Example CLI stores in ~/.example/auth.json."
  }
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `manifest_version` | yes | Always `1`. |
| `id` | yes | Must equal the directory name. |
| `name` | yes | Display name, 1 to 32 printable characters. |
| `usage_url` | no | An `https` link the details view offers as "Open \<name\> usage". |
| `setup` | no | The setup steps shown when no login is found or a login is rejected. All six strings are required when present; `install_url` is `https`. Without `setup`, the details show a generic sign-in step. |

The manifest is read without running any code. Unknown keys are ignored. A manifest that is missing or invalid does not stop collection: the provider still appears, with an error message naming the problem, so you can see and fix it.

## `plugin.py`

One module that defines `collect()` and, optionally, `detect()`. It imports only from `bars_plugin` and the standard library.

```python
"""Example: a bearer-token login file and one usage endpoint."""

from pathlib import Path

from bars_plugin import CollectionError, http_json, login_found, metric, read_json, result, token


def credentials():
    data = read_json(Path.home() / ".example" / "auth.json",
                     missing="Example login was not found. Run example login.",
                     unreadable="Example login could not be read. Run example login.")
    try:
        return token(data["access_token"])
    except (KeyError, TypeError):
        raise CollectionError("Example login is incomplete. Run example login.", "setup") from None


def parse(data):
    if not isinstance(data, dict):
        raise CollectionError("Provider returned an invalid usage response.")
    metrics = []
    monthly = data.get("monthly")
    if isinstance(monthly, dict):
        metrics.append(metric("monthly", "Monthly budget", "budget", "individual", "USD",
                              monthly.get("used"), monthly.get("limit"),
                              reset=monthly.get("resets_at"), period="month"))
    daily = data.get("daily_percent")
    if daily is not None:
        metrics.append(metric("daily", "Daily quota", "quota", "individual", "%", daily, 100,
                              reset=data.get("daily_resets_at"), period="day"))
    return result("monthly", metrics, alternatives=("daily",), secondary=("daily",))


def collect():
    return parse(http_json("https://api.example.com/v1/usage", {"Authorization": "Bearer " + credentials()}))


def detect():
    return login_found(credentials)
```

### `collect()`

Called for an enabled provider on each refresh, and called again with exponential backoff (about 1, 2, 4, 8, 16 seconds, jittered) when it raises a `CollectionError` with status `error` or any other exception, until it succeeds or the worker's 55-second deadline would be crossed; `setup` and `login_required` end the attempt at once. Each call must be a complete, independent fetch. It returns the value of `result(...)`, or raises `CollectionError(message, status)` with status `error`, `login_required` or `setup`. The message is shown to the person; keep it short, fixed and free of secrets, identifiers and response text. Any other exception, and any `sys.exit`, becomes a fixed error message.

`collect()` must read existing logins only. Bars never writes, refreshes or rotates a credential, and a plugin must not either.

### `detect()`

Optional. Returns the literal `True` when local login material exists, without any network request; any other value, including truthy strings, counts as not found. The installer uses it on a first install to decide which providers start hidden. `login_found(lookup, ...)` runs credential lookups and treats only a `setup` failure as "not found": unreadable material counts as found, so collection can then report `login_required`. Without `detect()` the provider counts as not found and starts hidden on a first install, which the person can undo from the dropdown.

### Building the result

- `metric(id, label, kind, scope, unit, used=None, limit=None, remaining=None, reset=None, period=None, *, reset_estimated=False, note=None)`: one allowance. `kind` is `budget`, `quota`, `balance` or `spend`; `scope` is `individual`, `team` or `unknown`; `period` is `five_hours`, `day`, `week`, `month` or `None`. Amounts may be numbers, numeric strings or `None`; `remaining` is derived when both `used` and `limit` are known. `reset` is an ISO 8601 string or epoch number. Pass `reset_estimated=True` when the reset comes from documentation rather than the response. `note` is one sentence shown beneath a balance that has no total or used amount. Invalid amounts and timestamps raise `CollectionError`; invalid ids, kinds, scopes and periods are programming errors.
- `result(primary, metrics, *, alternatives=(), secondary=())`: the value `collect()` returns. Metrics without any quantity are dropped, and a response with none is a failure, so it never replaces last-good data. The primary bar is `primary` when it has a quantity, else the first available of `alternatives`, else none. `secondary` names the metrics to show as mini bars beneath the primary; absent ids and the primary are dropped. Every metric appears in details.

Readers derive pace from `used`, `limit`, `period`, `resets_at` and the fetch time, so supply a period and a reset whenever the source documents them. Never invent a reset: estimate only from documented behavior, and mark it.

### Access helpers

| Helper | Does |
| --- | --- |
| `http_json(url, headers, timeout=12)` | One `https` GET expecting a JSON object. Certificate and hostname verification, TLS 1.2 minimum, every redirect refused, 1 MiB response bound. 401/403 become `login_required`; other failures fixed `error` messages. Plain `http` is refused except to exactly the host `127.0.0.1`; URLs carrying a username or password are refused. |
| `keychain(service, missing=...)` | The secret of a macOS generic password item via `/usr/bin/security`. A missing item is `setup` with `missing` as the message; anything else is `login_required`. |
| `keychain_entry_exists(service)` | Whether the item exists, without reading the secret. For `detect()`. |
| `read_bytes`, `read_json`, `read_toml` `(path, *, missing=..., unreadable=...)` | A login file, at most 1 MiB. Missing is `setup`, unreadable or malformed is `login_required`. `read_toml` reads the flat subset in `bars_plugin/toml_subset.py`. |
| `token(value)` | Requires visible ASCII before a value reaches a header, cookie or URL. Anything else is `login_required`. |
| `command(args, timeout=10)` | Runs a login tool from an argument list (no shell, no stdin, allowlisted environment, 1 MiB output bound, hard deadline) and returns its trimmed stdout. A non-zero exit is `login_required`; a missing tool is `setup`. |
| `login_found(*lookups)` | For `detect()`: runs lookups and returns false only on a `setup` failure. |
| `number`, `timestamp`, `now` | The normalizers `metric()` uses, for plugins that need them directly. |

Only names exported by `bars_plugin` are supported. Other collector modules may change between versions.

## What the collector guarantees around a plugin

The coordinator never imports `plugin.py`. Each enabled plugin runs `collect()` in its own spawned worker process and process group, started with the same allowlisted environment the standard plugins get (`HOME`, `PATH`, locale, proxy and CA variables; no other API keys or overrides). A plugin that raises `CollectionError(..., retry_after=seconds)` reports a rate limit: the worker does not retry it within the run (asking again is what the limit forbids), and the collector pauses the provider's scheduled attempts for at least that long (5 minutes after a first limit, doubling with consecutive limits, at most an hour; 0 means "the provider gave no usable figure, use those defaults"). `http_json()` does this for every plugin that uses it, for HTTP 429 and for HTTP 503 with `Retry-After`, reading the standard headers in order: `Retry-After` (RFC 9110, seconds or HTTP-date), the IETF `RateLimit` / `RateLimit-Reset` fields, then `X-RateLimit-Reset` / `X-Rate-Limit-Reset` (seconds or Unix time). A plugin that uses another transport gets the same reading with `bars_plugin.rate_limit_pause_seconds(headers)`. None of the four standard providers currently sends rate-limit metadata on a successful response, and Claude's edge answers 429 with `Retry-After: 0`, which is why the collector's own defaults exist. A plugin that hangs is killed at the per-provider deadline (55 seconds by default, covering every retry), together with the child processes it started in its process group. That is the limit of the containment: a child that detaches into its own session (`start_new_session=True`, a daemonizing CLI) outlives the worker, and with a run every minute such children accumulate, so a plugin must not start detached processes and should prefer `bars_plugin.command()`, which never does. A plugin that crashes, exits, prints or raises affects only its own provider's entry for that attempt. Its standard output and error are discarded, so nothing it prints can reach the collector's status line or the refresh log. The worker's result crosses to the coordinator as JSON of at most 256 KiB and is validated against the [snapshot contract](snapshot-contract.md) before it can touch the snapshot: only the contract's metric fields are accepted (an extra key fails the whole result), timestamps must already be canonical UTC text as `metric()` produces, and a success must carry quantities. A result that fails validation, or that would push the whole snapshot past its 1 MiB bound, is recorded as an error with a fixed message, and the provider's last good values stay. Plugin code also runs with the same allowlisted environment as the credential tools, whoever started the collector. A result can change only its own provider's metrics, primary and secondary selection, status, message and timestamps; its name, setup text, usage link, id and enabled flag come from the manifest and the settings.

`detect()` runs the same way, with a shorter deadline, and anything but a boolean true counts as not found.

This contains faults, not malice. A plugin runs as you, with your logins readable to it, like any script you choose to run. Install plugins you trust, and read a plugin before installing it the way you would read a shell script.

## Updates

Running the installer again (the README's install command, or `scripts/bootstrap.sh`) refreshes the standard plugins. For each file a standard plugin ships, the installer compares the installed file with what it last wrote, recorded by digest in `plugins/.installed.json`:

- missing, or unchanged since the installer wrote it: replaced with the shipped version;
- changed by you: left alone, with a warning naming the file. Delete the file and reinstall to get the shipped version back. The record keeps the shipped digest, so your edit stays recognised as an edit on later updates;
- a plugin directory that is a symbolic link (for example to a development checkout): left alone with a warning.

Files you add inside a standard plugin's directory and plugins you wrote are never touched by an update, and an update never deletes a plugin. Uninstalling without `--keep-data` removes the whole support directory, including your plugins.

## Trying a plugin

```sh
# Run the collector against a plugins directory of your choice, into a scratch snapshot.
PYTHONPATH=collector python3 -m bars_collector --plugins ~/Library/Application\ Support/Bars/plugins \
  --output /tmp/bars-try/snapshot.json --provider my_provider
cat /tmp/bars-try/snapshot.json

# Ask every plugin whether it finds a login, without network access.
PYTHONPATH=collector python3 -m bars_collector --plugins ~/Library/Application\ Support/Bars/plugins --detect
```

The standalone run prints one status line, such as `my_provider: ok`. Status `error` with `Provider plugin returned invalid usage data.` means the result failed validation; `Provider collection failed. Collection will retry later.` means `collect()` raised something other than a `CollectionError`. The tests in `tests/collector/test_plugins.py` show how to exercise a plugin's `parse` function with synthetic responses, which is the recommended way to test one; keep real responses out of the repository.
