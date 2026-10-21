# Changelog

Releases are tags on `main`. The install command follows `main`; `BARS_REF=<tag>` installs a
specific release, and `installation.json` records the installed commit.

## v3 (2026-10-21)

### Added
- Independent adaptive polling for each provider. Collection starts every minute; rate limits
  lengthen that provider's interval, and sustained successful scheduled fetches cautiously
  shorten it again. Learned intervals persist across runs and app updates.
- Safe scheduling diagnostics distinguish actual attempts from providers skipped because they
  are not due, are rate limited, or were already fetched for a queued request.

### Changed
- Refresh now respects rate-limit pauses instead of bypassing them. Successful refreshes retain
  the learned interval rather than immediately returning to one-minute polling.
- Scheduled collector invocations use `--scheduled`; explicit invocations can supply
  `--requested-at` to avoid repeating requests that completed while they waited.

### Fixed
- A pause saved before a failed snapshot write still protects subsequent explicit refreshes.
- Recovery requires successful fetches spanning six hours; idle time before the first success
  does not qualify.
- Queued hide/show requests preserve their original request time and republish visibility
  without repeating a fetch that already satisfied the request.
- Large supported provider lists retain their learned intervals when scheduling state is
  saved and reloaded.

### Upgrading
Run the install command again. Existing settings, plugins and usage snapshots are kept.
The private scheduling file is created automatically; no credential changes are needed.

## v2 (2026-10-19)

Providers are plugins. The four integrations are now plugin directories, and anyone can add a
provider by writing one; see [docs/plugins.md](docs/plugins.md).

### Added
- Provider plugins: `plugins/<id>/plugin.json` and `plugin.py`, discovered from the per-user
  plugins directory, each run in its own worker process with a bounded, validated result.
- The `bars_plugin` SDK: result values, credential lookup, one HTTPS request helper, and shared
  period and rate-limit helpers, so every plugin degrades the same way.
- Snapshot schema 2: a dynamic provider list carrying each plugin's setup steps, usage link,
  secondary allowances, estimated resets, notes and rate-limit pauses, so the app and widget
  name no provider.
- Rate-limit handling: `Retry-After`, the IETF `RateLimit` field and `X-RateLimit-Reset` are
  honored; a rate-limited provider pauses its scheduled attempts (5 minutes, doubling to an hour,
  never shorter than the provider's figure) and the row says when it retries. Refresh now ignores
  the pause.
- Devin shows its weekly quota first and one on-demand credits bar comparing this account's draw
  with the shared pool's remainder.
- Codex and Cursor take allowance periods from the lengths their responses report.
- `CHANGELOG.md`, `layout_version` and `installed_commit` in `installation.json`, and a `v1` tag
  for the first release.

### Changed
- Collection runs every minute (was every 15), with jitter, in-run retry of transient failures
  inside a 55-second deadline, and a throttled widget reload (changed values at most every 5
  minutes, otherwise every 15).
- Dropdown rows share one status line (pace left, reset right), the popover fits its content,
  a divider separates the footer, and metric notes appear in details.
- The widget shows at most four rows and shares a budget of six mini bars across them; the rest
  are counted and shown in the dropdown.
- The installer keeps a standard plugin file you edited (with a warning), reads settings under
  the refresh lock, rolls back only what it wrote and never through a directory that became a
  link, and accepts a branch or a tag in `BARS_REF`.

### Upgrading
Run the install command again. A v1 installation upgrades in place: settings and hidden
providers are kept, the v1 snapshot is upgraded on the first collection, and the LaunchAgent is
rewritten. Going back is manual; see [docs/distribution.md](docs/distribution.md).

## v1 (2026-10-02)

First release: a macOS menu bar app and large desktop widget showing usage for Claude, Codex,
Cursor and Devin from their existing logins, collected every 15 minutes by a Python collector
and published for the sandboxed widget by a Swift publisher.
