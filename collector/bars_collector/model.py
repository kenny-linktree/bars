"""Snapshot structure, plugin reconciliation and cache validation."""

import copy
import json
from datetime import datetime, timedelta, timezone
import re

from bars_plugin.values import (KINDS, MAX_RETRY_AFTER, MAX_TEXT, METRIC_ID, PERIODS, SCOPES, STATUSES, CollectionError, now,
                                number, timestamp)

from .plugins import PLUGIN_ID, SETUP_FIELDS

SCHEMA_VERSION = 2
PLACEHOLDER_MESSAGE = "Waiting for first collection."
# What a plugin's `collect()` may contribute to its provider entry; everything else comes
# from the manifest, the runner or the retained entry.
RESULT_FIELDS = ("primary_metric_id", "secondary_metric_ids", "metrics")
# A rate-limited provider's attempts pause: this long after the first limit, twice as long
# after each consecutive one, never longer than the cap, and never shorter than the
# provider's own Retry-After. The runner lengthens the pause to the provider's doubled
# collection interval when that is longer (see schedule.py); every run respects it.
RATE_LIMIT_FIRST_PAUSE = 5 * 60
RATE_LIMIT_MAX_PAUSE = 60 * 60
# Keeps any one plugin from growing the snapshot past the readers' 1 MiB bound.
MAX_METRICS = 32
# The readers and the collector both refuse a snapshot larger than this once written, so
# a result that would push the file over it is refused before it is applied.
MAX_SNAPSHOT_BYTES = 1024 * 1024
METRIC_FIELDS = frozenset({"id", "label", "kind", "scope", "unit", "used", "limit", "remaining",
                           "resets_at", "period", "resets_at_estimated", "note"})
# What the collector writes and the Swift reader parses: UTC, optional fraction, Z suffix.
CANONICAL_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z")


def empty_provider(plugin):
    return dict(id=plugin.id, name=plugin.name, enabled=True, status="setup", message=PLACEHOLDER_MESSAGE,
                fetched_at=None, last_attempt_at=None, primary_metric_id=None, secondary_metric_ids=[],
                metrics=[], usage_url=plugin.usage_url, setup=plugin.setup)


def reconcile(snapshot, plugins):
    """Return the snapshot's providers rebuilt from the discovered plugins, in plugin order.

    Each plugin keeps its retained entry (values, status, timestamps, enabled flag) and
    takes its name, usage link and setup text from the manifest, so editing a manifest
    shows on the next run. Providers without a plugin are dropped; new plugins start with
    the placeholder entry. Returns whether anything changed.
    """
    before = copy.deepcopy(snapshot["providers"])
    retained = {p["id"]: p for p in snapshot["providers"]}
    providers = []
    for plugin in plugins:
        entry = retained.get(plugin.id) or empty_provider(plugin)
        entry.update(name=plugin.name, usage_url=plugin.usage_url, setup=plugin.setup)
        providers.append(entry)
    snapshot["providers"] = providers
    return providers != before


def apply_enabled(snapshot, disabled):
    """Record each provider's enabled state. Returns whether anything changed.

    Only the `enabled` field changes; a disabled provider keeps its retained values,
    status, message and timestamps exactly as they were.
    """
    changed = False
    for p in snapshot["providers"]:
        enabled = p["id"] not in disabled
        if p.get("enabled") is not enabled:
            p["enabled"] = enabled
            changed = True
    return changed


def upgrade_snapshot(snapshot):
    """Bring a version 1 cache to the current schema without losing retained values."""
    if isinstance(snapshot, dict) and snapshot.get("schema_version") == 1 and isinstance(snapshot.get("providers"), list):
        for p in snapshot["providers"]:
            if isinstance(p, dict):
                p.setdefault("secondary_metric_ids", [])
                p.setdefault("usage_url", None)
                p.setdefault("setup", None)
        snapshot["schema_version"] = SCHEMA_VERSION
    return snapshot


def _short_text(value, what):
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT or not value.isprintable():
        raise ValueError("Invalid %s." % what)


def _instant(value, what):
    """A snapshot timestamp: null or the canonical UTC text the native readers accept.

    `timestamp()` would also normalize epoch numbers and offsets, but a stored value must
    already be canonical, or the Swift publisher rejects the whole file.
    """
    if value is None:
        return
    if not isinstance(value, str) or not CANONICAL_INSTANT.fullmatch(value):
        raise ValueError("Invalid %s." % what)
    timestamp(value)


def validate_metric(m):
    if not isinstance(m, dict) or not set(m) <= METRIC_FIELDS or not isinstance(m.get("id"), str) \
            or not METRIC_ID.fullmatch(m["id"]):
        raise ValueError("Invalid metric identity.")
    if m["kind"] not in KINDS or m["scope"] not in SCOPES:
        raise ValueError("Invalid metric kind or scope.")
    for key in ("label", "unit"):
        _short_text(m[key], "metric " + key)
    for field in ("used", "limit", "remaining"):
        if m[field] is not None and (isinstance(m[field], bool) or not isinstance(m[field], (int, float))):
            raise ValueError("Invalid metric amount.")
        number(m[field], negative=field == "remaining" and m["kind"] == "balance")
    _instant(m["resets_at"], "metric reset")
    # Older caches predate the optional period, estimate and note fields.
    if m.get("period") is not None and m["period"] not in PERIODS:
        raise ValueError("Invalid allowance period.")
    if not isinstance(m.get("resets_at_estimated", False), bool):
        raise ValueError("Invalid reset estimate flag.")
    if m.get("note") is not None:
        _short_text(m["note"], "metric note")


def validate_provider(p):
    """Reject a malformed provider entry, whether read from a cache or produced by a plugin."""
    if not isinstance(p, dict) or not isinstance(p["id"], str) or not PLUGIN_ID.fullmatch(p["id"]):
        raise ValueError("Invalid provider identity.")
    if not isinstance(p["name"], str) or not p["name"].strip() or len(p["name"]) > 32 or not p["name"].isprintable():
        raise ValueError("Invalid provider name.")
    if p["status"] not in STATUSES:
        raise ValueError("Invalid provider state.")
    for field in ("fetched_at", "last_attempt_at"):
        _instant(p[field], "provider " + field)
    # Older caches predate the rate-limit pause.
    _instant(p.get("next_attempt_at"), "provider next_attempt_at")
    if p["message"] is not None:
        _short_text(p["message"], "provider message")
    if "enabled" in p and not isinstance(p["enabled"], bool):
        raise ValueError("Invalid provider enabled flag.")
    if p.get("usage_url") is not None and (not isinstance(p["usage_url"], str) or not p["usage_url"].startswith("https://")):
        raise ValueError("Invalid provider usage link.")
    setup = p.get("setup")
    if setup is not None:
        if not isinstance(setup, dict) or set(setup) != set(SETUP_FIELDS):
            raise ValueError("Invalid provider setup.")
        for key in SETUP_FIELDS:
            _short_text(setup[key], "provider setup")
    if not isinstance(p["metrics"], list) or len(p["metrics"]) > MAX_METRICS:
        raise ValueError("Invalid metrics.")
    ids = set()
    for m in p["metrics"]:
        validate_metric(m)
        if m["id"] in ids:
            raise ValueError("Invalid metric identity.")
        ids.add(m["id"])
    if p["primary_metric_id"] is not None and p["primary_metric_id"] not in ids:
        raise ValueError("Invalid primary metric.")
    secondary = p.get("secondary_metric_ids", [])
    if not isinstance(secondary, list) or len(set(secondary)) != len(secondary) \
            or not all(mid in ids and mid != p["primary_metric_id"] for mid in secondary):
        raise ValueError("Invalid secondary metrics.")
    if p["status"] == "ok" and (not p["metrics"] or p["fetched_at"] is None or p.get("next_attempt_at") is not None):
        raise ValueError("Invalid successful provider.")
    return p


def validate_result(value):
    """Check what a worker reported before it can touch the snapshot.

    A plugin's success must carry exactly the result fields with a valid shape. Failures
    carry a status and message only. Anything else is a plugin fault, not a snapshot change.
    """
    if not isinstance(value, dict) or value.get("status") not in STATUSES:
        raise ValueError("Invalid worker status.")
    if value["status"] == "ok":
        expected = {"status", "message", "fetched_at", "last_attempt_at", *RESULT_FIELDS}
        if set(value) != expected:
            raise ValueError("Invalid worker result.")
    elif set(value) - {"retry_after"} != {"status", "message", "last_attempt_at"}:
        raise ValueError("Invalid worker failure.")
    if "retry_after" in value and (isinstance(value["retry_after"], bool) or not isinstance(value["retry_after"], int)
                                   or not 0 <= value["retry_after"] <= MAX_RETRY_AFTER):
        raise ValueError("Invalid worker retry hint.")
    probe = dict(id="probe", name="Probe", metrics=[], primary_metric_id=None, secondary_metric_ids=[])
    probe.update({k: v for k, v in value.items() if k != "retry_after"})
    for key in ("fetched_at", "last_attempt_at", "message"):
        probe.setdefault(key, None)
    validate_provider(probe)
    # A success must bring quantities; `result()` drops empty metrics, but a plugin that
    # builds its own dictionaries could otherwise replace last-good values with nothing.
    if value["status"] == "ok" and not all(any(m[k] is not None for k in ("used", "limit", "remaining"))
                                           for m in value["metrics"]):
        raise ValueError("Successful result without quantities.")
    return value


def parse_instant(value):
    """A canonical snapshot timestamp as an aware datetime, or None.

    Second precision is enough here, and dropping the fraction keeps Python 3.9's
    `fromisoformat`, which accepts only three- or six-digit fractions, from refusing a
    timestamp the validator accepts.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(re.sub(r"[.,]\d+", "", value).replace("Z", "+00:00"))
    except ValueError:
        return None


def rate_limit_pause(previous, retry_after, at):
    """How long a rate-limited provider's scheduled attempts pause, in seconds.

    The first pause is `RATE_LIMIT_FIRST_PAUSE`; while the previous pause is still fresh (its
    end lies after the last attempt) each consecutive limit doubles it, up to the cap. The
    provider's own `Retry-After`, when it gives one, is a floor, not a ceiling.
    """
    pause = RATE_LIMIT_FIRST_PAUSE
    previous_end, previous_attempt = parse_instant(previous.get("next_attempt_at")), parse_instant(previous.get("last_attempt_at"))
    if previous_end is not None and previous_attempt is not None and previous_end > previous_attempt:
        pause = min(max(int((previous_end - previous_attempt).total_seconds()) * 2, RATE_LIMIT_FIRST_PAUSE),
                    RATE_LIMIT_MAX_PAUSE)
    return min(max(pause, retry_after or 0), MAX_RETRY_AFTER)


def is_pausing(provider, at):
    """True while the provider's scheduled attempts are paused for a rate limit.

    A pause the collector did not write (an edited or foreign cache, or a clock that was set
    back) could otherwise park a provider for years: a pause counts only while it ends within
    `MAX_RETRY_AFTER` of now and began no earlier than its own last attempt.
    """
    until = parse_instant(provider.get("next_attempt_at"))
    if until is None or until <= at or (until - at).total_seconds() > MAX_RETRY_AFTER:
        return False
    attempted = parse_instant(provider.get("last_attempt_at"))
    return attempted is not None and attempted <= until and (until - attempted).total_seconds() <= MAX_RETRY_AFTER


def validate_snapshot(snapshot):
    """Reject corrupt caches before they can erase independent last-good values."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported snapshot schema.")
    if snapshot.get("generated_at") is None:
        raise ValueError("Missing snapshot generation time.")
    _instant(snapshot["generated_at"], "snapshot generation time")
    providers = snapshot["providers"]
    if not isinstance(providers, list):
        raise ValueError("Invalid snapshot providers.")
    ids = [p["id"] for p in providers if isinstance(p, dict)]
    if len(ids) != len(providers) or ids != sorted(set(ids)):
        raise ValueError("Invalid snapshot providers.")
    for p in providers:
        validate_provider(p)
    return snapshot


def encoded_size(snapshot):
    """The size the snapshot will have on disk, with the storage module's compact encoding."""
    return len(json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(",", ":"))) + 1


__all__ = ["CollectionError", "MAX_SNAPSHOT_BYTES", "SCHEMA_VERSION", "apply_enabled", "empty_provider", "is_pausing", "rate_limit_pause",
           "encoded_size", "now", "reconcile", "upgrade_snapshot", "validate_provider", "validate_result",
           "validate_snapshot"]
