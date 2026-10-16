# Snapshot contract, version 2

The collector writes UTF-8 JSON and atomically replaces the snapshot file. The widget never reads credentials or makes provider requests. Unknown quantities are null, never inferred from a missing field. Only finite numbers are valid.

Version 2 replaced the fixed list of four providers with one entry per installed [provider plugin](plugins.md), and moved everything the native readers used to know about a particular provider (setup steps, usage link, which allowances are mini bars, estimated resets, balance notes) into the snapshot. The native readers name no provider.

```json
{
  "schema_version": 2,
  "generated_at": "2026-10-01T12:00:00Z",
  "providers": [
    {
      "id": "claude",
      "name": "Claude",
      "enabled": true,
      "status": "ok",
      "message": null,
      "fetched_at": "2026-10-01T12:00:00Z",
      "last_attempt_at": "2026-10-01T12:00:00Z",
      "primary_metric_id": "monthly_spend",
      "secondary_metric_ids": [],
      "usage_url": "https://claude.ai/settings/usage",
      "setup": {
        "install_title": "Install Claude Code",
        "install_url": "https://code.claude.com/docs/en/overview",
        "login_command": "claude",
        "login_step": "Run Claude Code once in Terminal and sign in:",
        "sign_in_again": "Run Claude Code in Terminal and sign in again. If it does not ask, type /login.",
        "credential_note": "Bars reads the “Claude Code-credentials” Keychain entry that Claude Code saves."
      },
      "metrics": [
        {
          "id": "monthly_spend",
          "label": "Monthly budget",
          "kind": "budget",
          "scope": "unknown",
          "unit": "USD",
          "used": 20,
          "limit": 100,
          "remaining": 80,
          "resets_at": "2026-11-01T00:00:00Z",
          "resets_at_estimated": true,
          "period": "month",
          "note": null
        }
      ]
    }
  ]
}
```

## Fields and invariants

- `providers` holds one entry per plugin directory the collector found, in ascending order of `id`, whether or not each is enabled. Ids are unique and match `[a-z][a-z0-9_]{0,31}`. The list may be empty when no plugins are installed. Readers must not assume any particular provider exists.
- `id` is the plugin directory name. `name`, `usage_url` and `setup` come from the plugin manifest and are refreshed on every run, so editing a manifest shows on the next collection. `usage_url` is an `https` URL or null. `setup` is null or an object with exactly the six strings shown; readers show generic setup text when it is null.
- `enabled` is an optional boolean; a missing value means true. False means the person hid (disabled) the provider. The collector then starts no worker for it and leaves every other field of its retained entry unchanged: metrics, primary and secondary selection, status, message, `fetched_at` and `last_attempt_at` stay as they were. A provider that was never collected keeps the `setup` placeholder with the message `Waiting for first collection.`. Each collector run sets every provider's flag from its `--disable` arguments, so re-enabling a provider sets it back to true.
- A provider whose plugin exists but cannot be loaded (missing or invalid `plugin.json`, missing `plugin.py`) keeps its retained values and reports `status: error` with a fixed message naming the problem, so the person sees it in the overview rather than the provider silently disappearing. A plugin directory that is removed drops its entry on the next run; a new one starts with the placeholder entry.
- `generated_at` describes file generation. It must never substitute for a provider's `fetched_at`.
- Provider status is `ok`, `error`, `login_required` or `setup`. `message` is a short (at most 200 printable characters) user-safe explanation, with no tokens, account IDs, raw HTTP bodies or filesystem credential contents. A plugin's own `CollectionError` text is used when it meets that bound; otherwise, and for any other exception, a fixed message is used.
- `fetched_at` is the last successful fetch completion time for that provider. It is null until a successful fetch. `last_attempt_at` is the latest completed attempt's time.
- Failed attempts preserve previous metrics, primary and secondary selection and `fetched_at`, while updating status, message and `last_attempt_at`. Successful empty or malformed responses are failures, not fresh empty results. A result that fails validation (wrong shape, unknown metric fields, non-canonical timestamps, a success without quantities, dangling ids, more than 32 metrics, over 256 KiB, unserializable) is a failure with the message `Provider plugin returned invalid usage data.` A valid result that would push the encoded snapshot past 1 MiB is refused with `Provider plugin returned more data than the snapshot can hold.`; the provider keeps its retained values.
- Publish each completed provider independently. A slow or failed provider must not hold back successful providers.
- Metric `id` is a stable identity within a provider, matching `[a-z][a-z0-9_]{0,63}`. Never deduplicate by unit, amount or limit. A provider has at most 32 metrics.
- `kind` is `budget`, `quota`, `balance` or `spend`. `scope` is `individual`, `team` or `unknown`. `unit` is the source currency code, `credits`, `%` or another explicit source unit. `label`, `unit` and `note` are non-empty printable strings of at most 200 characters.
- `used`, `limit` and `remaining` are numbers or null. A genuine allowance has nonnegative `used` and `limit`; zero is distinct from null. Derive `remaining = max(limit - used, 0)` only when both values are known. Keep a source-reported balance as reported, including a negative balance.
- A percentage quota has unit `%`, used equal to the reported percentage and limit 100. A balance-only metric has `kind: balance`, known `remaining` and null used/limit. Do not treat a balance as spend. Its optional `note` is one sentence the details show beneath it, such as why it has no reset.
- `resets_at` is a reset instant or null. Readers never invent one. A plugin may supply a documented estimate (such as the end of the UTC calendar month containing the fetch) and must then set `resets_at_estimated: true`, which readers show as `(est.)`. The flag is optional and defaults to false. Timestamps use `YYYY-MM-DDTHH:MM:SS[.digits]Z`, with Gregorian calendar dates in years 0001 through 9999. Fractional seconds are optional and native readers retain them to `Date` precision. Invalid calendar dates, leap seconds, non-UTC offsets and trailing text are rejected.
- `period` is the source-documented allowance period length: `five_hours`, `day`, `week`, `month` or null when unknown. It is optional; readers treat a missing or unrecognized value as unknown rather than rejecting the snapshot. A period never substitutes for a reset instant. Readers derive the period start by subtracting the period from the reset (a calendar month in UTC for `month`).
- Pace is derived by the reader, not stored. It exists only when `used`, a positive `limit`, `period`, a reset instant and `fetched_at` are all known and the current time is before the reset, even if a fresh fetch reports an expired reset. The elapsed share is measured at `fetched_at`, so stale data compares usage with the time that had elapsed when it was collected.
- A bar's fill is `used / limit`, visually clamped to 0...1, only when both quantities are known and limit is positive. Unknown and zero limits have no fabricated fill. Values above the limit remain visible as overage text.
- A provider's `primary_metric_id` must name one of its metrics or be null. The plugin selects it from the current successful response only, considering metrics with a reported quantity (zero counts) in its own documented priority; a selected allowance retains its own label, units, scope and reset, and successful responses replace all previous metrics rather than merging. The standard plugins' priorities: Claude `monthly_spend`, then `five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet`; Codex `monthly_credits`, then `primary_window`, `secondary_window`; Cursor only `included`; Devin only `on_demand_balance`. Missing, null or disabled Claude spend and empty monthly amounts allow quota selection; malformed fields still fail collection.
- `secondary_metric_ids` names the metrics the overview shows as labeled mini bars beneath the primary bar, in that order. Each must name one of the provider's metrics and none may be the primary; the list is optional and defaults to empty. The plugin chooses them. The standard plugins: Cursor's on-demand allowances; Devin's daily and weekly quotas; for Claude and Codex, when a quota is primary, the other general session or weekly window, excluding the primary itself, so model-specific quotas stay in details. All metrics appear in details. An ended period retains old values with a previous-period label until a fresh fetch replaces them.

## Execution interface

The standalone collector command is `python3 -m bars_collector --output PATH --plugins DIR [--provider ID] [--disable ID]... [--timeout SECONDS]` and needs Python 3.9 or later. `--timeout` (default 55, range 5 to 120) is each worker's hard deadline including retries: a worker retries an `error` result with exponential backoff (1, 2, 4, ... seconds, jittered) while the next wait still ends before the deadline; `setup` and `login_required` are reported at once. `--detect` never retries. `PYTHONPATH` points to the installed `collector` directory, which holds both `bars_collector` and the plugin SDK package `bars_plugin`. `--plugins` defaults to `~/Library/Application Support/Bars/plugins`. The collector writes only to its output directory, keeps a single-writer lock and exits after bounded collection. A provider failure is represented in the snapshot; infrastructure failures return a nonzero exit code. `--provider` refreshes only that provider while retaining the other entries; naming an id with no plugin is a usage error (exit 2).

Every run rebuilds the provider list from the plugins directory before collecting, and writes the snapshot if the list, a manifest field or an `enabled` flag changed, or if no snapshot existed yet. A version 1 snapshot is read and upgraded in place, keeping its retained values.

`--disable` is repeatable. Disabled providers are skipped and marked `enabled: false`; all others are marked true. Ids with no plugin are ignored. Naming a disabled provider with `--provider` starts no worker and exits 0. It still writes the snapshot if an `enabled` flag changed, so a newly disabled provider is published promptly.

`python3 -m bars_collector --detect --plugins DIR` writes no snapshot and makes no network request through the standard plugins. It prints one JSON object, such as `{"claude": true, "codex": false, "cursor": true, "devin": true}`, with one boolean per plugin in id order. True means the plugin's `detect()` returned true in its own worker process: a Keychain item for Claude or Cursor (checked without reading the secret), a ChatGPT login in `~/.codex/auth.json` for Codex, or both Devin's credential file and its CLI on `PATH`. A plugin without `detect()`, one that fails or hangs, or one that cannot be loaded counts as not found. Material that exists but cannot be read counts as found, so collection can report `login_required`. Output never contains credentials, paths or account identifiers.

`~/Library/Application Support/Bars/installation.json` lists disabled providers as `"disabled_providers": ["cursor"]`, each once, in the order they were added. A missing key means none. It also records `plugins_path`, `layout_version` (2 for this layout; the first release wrote none) and `installed_commit` (the checkout's commit, or null outside a Git checkout). The installer writes it and the app edits `disabled_providers` in place, preserving the other keys and mode 0600. The refresh runner reads the file after acquiring its lock on every run and passes each well-formed listed id as `--disable`; malformed entries are logged and skipped, and ids with no plugin are passed through and ignored by the collector.

Explicit native refreshes pass the runner's `--wait` flag. It waits up to 150 seconds for another refresh, then reads the current settings; timeout returns a nonzero status without starting collection. Scheduled invocations pass `--scheduled` instead: they wait a random time up to `--jitter` seconds (default 5) and exit successfully without collecting if the lock is busy. Successful collections retry final publication of any saved staging snapshot even when no values changed. This retry does not advance `fetched_at` or `last_attempt_at`. The final publication of a `--scheduled` run requests a widget timeline reload only when the snapshot's display content (everything except `generated_at`, `fetched_at` and `last_attempt_at`) differs from the last reloaded content and at least 5 minutes have passed, or when 15 minutes have passed; `widget-reload.json` beside the snapshot records the last reload. Explicit refreshes always request a reload.

The private staging path is `~/Library/Application Support/Bars/snapshot.json`. The native publisher validates this file and atomically writes `~/Library/Application Support/Bars/widget-snapshot.json` with mode 0600, then requests a timeline reload. The widget remains sandboxed with a home-relative, read-only file exception for this exact published file. It cannot read the collector staging file, the plugins directory or provider credentials. Native code resolves the account's actual home through `getpwuid_r`, since the widget's default home path is its sandbox container.

This explicit storage arrangement supports the local ad-hoc signed build. A real WidgetKit-hosted extension could not access the proposed App Group without a team signing identity, despite a direct executable probe passing. There is no automatic storage fallback. The publisher rejects inaccessible, oversized, malformed or unsupported snapshots without replacing the last published file.
