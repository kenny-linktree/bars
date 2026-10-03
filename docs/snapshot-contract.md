# Snapshot contract, version 1

The collector writes UTF-8 JSON and atomically replaces the snapshot file. The widget never reads credentials or makes provider requests. Unknown quantities are null, never inferred from a missing field. Only finite numbers are valid.

```json
{
  "schema_version": 1,
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
          "resets_at": null,
          "period": "month"
        }
      ]
    }
  ]
}
```

## Fields and invariants

- A snapshot always contains exactly the provider IDs `claude`, `codex`, `cursor` and `devin`, in that order, whether or not they are enabled. The example shows only one to illustrate the structure.
- `enabled` is an optional boolean; a missing value means true, so snapshots written before it existed remain valid. False means the person hid (disabled) the provider. The collector then starts no worker for it and leaves every other field of its retained entry unchanged: metrics, primary selection, status, message, `fetched_at` and `last_attempt_at` stay as they were. A provider that was never collected keeps the `setup` placeholder with the message `Waiting for first collection.`. Each collector run sets every provider's flag from its `--disable` arguments, so re-enabling a provider sets it back to true.
- `generated_at` describes file generation. It must never substitute for a provider's `fetched_at`.
- Provider status is `ok`, `error`, `login_required` or `setup`. `message` is a short user-safe explanation, with no tokens, account IDs, raw HTTP bodies or filesystem credential contents.
- `fetched_at` is the last successful fetch completion time for that provider. It is null until a successful fetch. `last_attempt_at` is the latest completed attempt's time.
- Failed attempts preserve previous metrics, primary selection and `fetched_at`, while updating status, message and `last_attempt_at`. Successful empty or malformed responses are failures, not fresh empty results.
- Publish each completed provider independently. A slow or failed provider must not hold back successful providers.
- Metric `id` is a stable identity within a provider. Never deduplicate by unit, amount or limit.
- `kind` is `budget`, `quota`, `balance` or `spend`. `scope` is `individual`, `team` or `unknown`. `unit` is the source currency code, `credits`, `%` or another explicit source unit.
- `used`, `limit` and `remaining` are numbers or null. A genuine allowance has nonnegative `used` and `limit`; zero is distinct from null. Derive `remaining = max(limit - used, 0)` only when both values are known. Keep a source-reported balance as reported, including a negative balance.
- A percentage quota has unit `%`, used equal to the reported percentage and limit 100. A balance-only metric has `kind: balance`, known `remaining` and null used/limit. Do not treat a balance as spend.
- `resets_at` is a source-reported reset instant or null. Never invent a monthly boundary. Timestamps use `YYYY-MM-DDTHH:MM:SS[.digits]Z`, with Gregorian calendar dates in years 0001 through 9999. Fractional seconds are optional and native readers retain them to `Date` precision. Invalid calendar dates, leap seconds, non-UTC offsets and trailing text are rejected.
- `period` is the source-documented allowance period length: `five_hours`, `day`, `week`, `month` or null when unknown. It is optional; readers treat a missing or unrecognized value as unknown rather than rejecting the snapshot. A period never substitutes for a reset instant. Readers derive the period start by subtracting the period from the reset (a calendar month in UTC for `month`).
- Pace is derived by the reader, not stored. It exists only when `used`, a positive `limit`, `period`, a reset instant and `fetched_at` are all known and the current time is before the reset, even if a fresh fetch reports an expired reset. The elapsed share is measured at `fetched_at`, so stale data compares usage with the time that had elapsed when it was collected.
- A bar's fill is `used / limit`, visually clamped to 0...1, only when both quantities are known and limit is positive. Unknown and zero limits have no fabricated fill. Values above the limit remain visible as overage text.
- A provider's `primary_metric_id` must name one of its metrics or be null. Selection considers only metrics with a reported quantity in the current successful response, in this order: Claude `monthly_spend`, `five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet`; Codex `monthly_credits`, `primary_window`, `secondary_window`. Zero is a reported quantity. Missing, null or disabled Claude spend and empty monthly amounts allow quota selection; malformed fields still fail collection. A selected quota retains its own label, units, scope and reset. No plan name is inferred, and successful responses replace all previous metrics rather than merging account or allowance types. Cursor still selects only `included` and Devin only `on_demand_balance`; neither substitutes an unrelated scope or allowance.
- Devin daily/weekly quotas and Cursor on-demand metrics appear as secondary allowances on the overview. When Claude or Codex has a quota primary, its other reported general session or weekly window appears beneath it, excluding the primary itself. Model-specific quotas remain in details. Monthly-budget overview rows remain unchanged. All metrics appear in details. An ended period retains old values with a previous-period label until a fresh fetch replaces them.

## Execution interface

The standalone collector command is `python3 -m bars_collector --output PATH [--provider ID] [--disable ID]...` and needs Python 3.9 or later. `PYTHONPATH` points to the installed `collector` directory. It writes only to its output directory, keeps a single-writer lock and exits after bounded collection. A provider failure is represented in the snapshot; infrastructure failures return a nonzero exit code. `--provider` refreshes only that provider while retaining the other entries.

`--disable` is repeatable. Disabled providers are skipped and marked `enabled: false`; all others are marked true. Naming a disabled provider with `--provider` starts no worker and exits 0. It still writes the snapshot if an `enabled` flag changed, so a newly disabled provider is published promptly.

`python3 -m bars_collector --detect` makes no network request and writes no file. It prints one JSON object, such as `{"claude": true, "codex": false, "cursor": true, "devin": true}`, with the four IDs in order. True means local login material was found: a Keychain item for Claude or Cursor, a ChatGPT login in `~/.codex/auth.json` for Codex, or both Devin's credential file and its CLI on `PATH`. Detection uses the adapters' own lookups, checks Keychain items without reading their secrets and treats a lookup that would report `setup` as not found. Material that exists but cannot be read counts as found, so collection can report `login_required`. Output never contains credentials, paths or account identifiers.

`~/Library/Application Support/Bars/installation.json` lists disabled providers as `"disabled_providers": ["cursor"]`. A missing key means none. The installer writes it and the app edits it in place, preserving the other keys and mode 0600. The refresh runner reads the file after acquiring its lock on every run and passes each listed ID as `--disable`. It ignores unknown IDs and logs them. An invalid list is logged and treated as empty.

Explicit native refreshes pass the runner's `--wait` flag. It waits up to 150 seconds for another refresh, then reads the current settings; timeout returns a nonzero status without starting collection. Scheduled invocations omit the flag and exit successfully without collecting if the lock is busy. Successful collections retry final publication of any saved staging snapshot even when no values changed. This retry does not advance `fetched_at` or `last_attempt_at`.

The private staging path is `~/Library/Application Support/Bars/snapshot.json`. The native publisher validates this file and atomically writes `~/Library/Application Support/Bars/widget-snapshot.json` with mode 0600, then requests a timeline reload. The widget remains sandboxed with a home-relative, read-only file exception for this exact published file. It cannot read the collector staging file or provider credentials. Native code resolves the account's actual home through `getpwuid_r`, since the widget's default home path is its sandbox container.

This explicit storage arrangement supports the local ad-hoc signed build. A real WidgetKit-hosted extension could not access the proposed App Group without a team signing identity, despite a direct executable probe passing. There is no automatic storage fallback. The publisher rejects inaccessible, oversized, malformed or unsupported snapshots without replacing the last published file.
