# Bars

Bars shows the current usage of AI providers such as Claude, Codex, Cursor and Devin. Each provider has its own bar showing usage and the amount remaining.

## Language

**Provider**:
An AI service whose usage Bars displays. Every provider is supplied by exactly one provider plugin; the standard plugins supply Claude, Codex, Cursor and Devin.

**Provider plugin**:
The directory that adds one provider: a manifest with its identity, usage link and setup steps, and a module that reads an existing login and returns the provider's allowances.
_Avoid_: Adapter, vendor plugin, integration

**Standard plugin**:
A provider plugin shipped with Bars and installed by its installer. An update replaces a standard plugin file only while it is unchanged since the installer wrote it; an edited file is kept.

**Plugin SDK**:
The fixed set of helpers a provider plugin may use: result building, login lookup and the bounded usage request. Everything else in the collector is private.

**Manifest**:
A provider plugin's `plugin.json`: what the collector and the native readers know about a provider without running its code.

**Used amount**:
The amount consumed within a particular provider allowance and period, expressed in that allowance's units.

**Remaining amount**:
The unused portion of a particular provider allowance. It does not by itself describe whether the provider will stop access or charge for further use.
_Avoid_: Remaining access

**Hidden provider**:
A provider the person has disabled. Collection is skipped and its last values are kept, but the widget and overview omit it except for a control to show it again. The installer, collector and snapshot call the same state disabled (`--disable`, `disabled_providers`, `enabled: false`).
_Avoid_: Removed provider, deleted provider

**Setup steps**:
The per-provider instructions shown when login material is missing: install the provider's client, run its login command, then refresh. A shorter sign-in box reuses the login command when a login is rejected. They come from the plugin manifest.

**Provider bar**:
The single visual summary of a provider's usage and remaining amount.

**Devin quota summary**:
Devin's daily and weekly included quotas, shown as two secondary allowances beneath its on-demand balance, each with its reset time and pace tag.

**Secondary allowance**:
An allowance shown as a labeled mini bar beneath a provider's primary bar, such as Cursor's on-demand allowances and Devin's quotas. The provider plugin chooses them.

**On-demand balance**:
The remaining extra-usage balance reported for Devin. It is distinct from accumulated spend and from the daily and weekly included quotas.
_Avoid_: Billed usage, used amount

**Allowance period**:
The documented length of time an allowance covers before it resets, such as five hours, a day, a week or a month. The period start is derived from the reset instant.

**Estimated reset**:
A reset instant a provider plugin derives from documented behavior, such as the end of a calendar month, because the source reports none. It is marked as an estimate wherever it is shown; the readers never derive one themselves.

**Pace**:
The comparison of an allowance's used share with the share of its period that had elapsed at the last successful fetch. Ahead of pace means the remaining amount would run out before the reset at that rate.
_Avoid_: Burn rate, velocity

**Rate limit**:
A provider's answer that it was asked too often (HTTP 429, or 503 with a stated pause). Unlike a transient failure it is not retried within the run; the provider's own figure, read from the standard headers, sets the floor of the pause.

**Pause**:
The interval after a rate limit during which collection leaves that provider alone, including Refresh now. It lasts at least as long as the provider requests and may grow after repeated limits. It is distinct from the provider's normal polling interval. The row shows `Rate limited · retries <time>` while it lasts.
_Avoid_: Cooldown, backoff (that is the in-run retry wait)

**Pace marker**:
The tick on a provider bar at the elapsed share of the period. Fill beyond the marker means usage is ahead of pace.

**Dropdown**:
The host app's menu bar panel, opened by clicking the status item or a widget row. It shows the overview or one provider's details. There is no standalone window.
_Avoid_: Host window

**Overview**:
The list of provider bars for every shown provider, in plugin id order, in the widget and at the top level of the dropdown.

**Details**:
The dropdown view for one provider, opened by clicking its row: all reported allowances, scopes, reset times, status, manual refresh and, when needed, setup steps.

**Data age**:
The time since a provider's last successful data fetch. Each provider has its own data age.

**Consolidated freshness**:
The shared updated time in the widget and dropdown headers, taken from the latest successful fetch. A row shows its own data age only when it deviates: it never succeeded, its latest attempt failed, or its fetch is more than a minute from the latest.

**Included allowance**:
The provider-reported usage included in a plan for a particular period. Cursor's main bar represents its individual included allowance.

**Allowance scope**:
The individual or team whose usage and limit an allowance describes. Amounts from different scopes are not interchangeable.

**Meter mark**:
The static Bars identity: three left-aligned horizontal bars, shortest at the top and longest at the bottom. The app icon renders it in color on a tile; the menu bar uses a monochrome template of the same bars. It never represents live usage.
