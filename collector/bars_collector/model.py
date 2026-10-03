"""Snapshot values, source normalization, and cache validation."""

from datetime import datetime, timezone
import math
import re

PROVIDERS = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor", "devin": "Devin"}
# Source-documented allowance period lengths. Readers derive the period start from
# the reset instant so pace can compare usage with elapsed time. None means unknown.
PERIODS = {"five_hours", "day", "week", "month"}
# Python 3.9's fromisoformat accepts only 3- or 6-digit fractions and colon offsets;
# 3.11 also accepts other fraction lengths, a comma separator and +HHMM. Normalize
# those so every supported interpreter reads provider timestamps the same way.
_FRACTION = re.compile(r"([T ]\d{2}:\d{2}:\d{2})[.,](\d+)")
_COMPACT_OFFSET = re.compile(r"([T ][\d:.]+[+-]\d{2})(\d{2})$")


class CollectionError(Exception):
    def __init__(self, message="Usage is temporarily unavailable.", status="error"):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def number(value, *, negative=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, float, int)):
        raise CollectionError("Provider returned an invalid amount.")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise CollectionError("Provider returned an invalid amount.") from None
    if not math.isfinite(result) or (result < 0 and not negative):
        raise CollectionError("Provider returned an invalid amount.")
    return result


def timestamp(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            result = datetime.fromisoformat(iso_text(value.replace("Z", "+00:00")))
            if result.tzinfo is None:
                raise ValueError()
            result = result.astimezone(timezone.utc)
        else:
            raise ValueError()
        return result.isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        raise CollectionError("Provider returned an invalid reset time.") from None


def iso_text(value):
    value = _FRACTION.sub(lambda m: m.group(1) + "." + (m.group(2) + "000000")[:6], value, count=1)
    return _COMPACT_OFFSET.sub(r"\1:\2", value, count=1)


def metric(mid, label, kind, scope, unit, used=None, limit=None, remaining=None, reset=None, period=None):
    used, limit = number(used), number(limit)
    remaining = number(remaining, negative=kind == "balance")
    if remaining is None and used is not None and limit is not None:
        remaining = max(limit - used, 0)
    if period is not None and period not in PERIODS:
        raise ValueError("Unknown allowance period.")
    return dict(id=mid, label=label, kind=kind, scope=scope, unit=unit,
                used=used, limit=limit, remaining=remaining, resets_at=timestamp(reset), period=period)


def result(primary, metrics, *, alternatives=()):
    # A valid response with no quantities cannot replace last-good data.
    metrics = [m for m in metrics if any(m[k] is not None for k in ("used", "limit", "remaining"))]
    if not metrics:
        raise CollectionError("Provider returned no usable usage data.")
    # Only provider-specific, explicitly labeled alternatives may replace an absent
    # preferred allowance. Filter empty quantities before choosing a display metric.
    available = {m["id"] for m in metrics}
    selected = next((mid for mid in (primary, *alternatives) if mid in available), None)
    return {"primary_metric_id": selected,
            "metrics": metrics}


def empty_provider(pid):
    return dict(id=pid, name=PROVIDERS[pid], status="setup", message="Waiting for first collection.",
                fetched_at=None, last_attempt_at=None, primary_metric_id=None, metrics=[])


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


def validate_snapshot(snapshot):
    """Reject corrupt caches before they can erase independent last-good values."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("Unsupported snapshot schema.")
    if snapshot.get("generated_at") is None:
        raise ValueError("Missing snapshot generation time.")
    timestamp(snapshot["generated_at"])
    providers = snapshot["providers"]
    if not isinstance(providers, list) or [p["id"] for p in providers] != list(PROVIDERS):
        raise ValueError("Invalid snapshot providers.")
    for p in providers:
        if p["name"] != PROVIDERS[p["id"]] or p["status"] not in {"ok", "error", "login_required", "setup"}:
            raise ValueError("Invalid provider state.")
        for field in ("fetched_at", "last_attempt_at"):
            timestamp(p[field])
        if p["message"] is not None and not isinstance(p["message"], str):
            raise ValueError("Invalid provider message.")
        # Optional: snapshots written before providers could be disabled omit it.
        if "enabled" in p and not isinstance(p["enabled"], bool):
            raise ValueError("Invalid provider enabled flag.")
        ids = set()
        for m in p["metrics"]:
            if not isinstance(m["id"], str) or m["id"] in ids:
                raise ValueError("Invalid metric identity.")
            ids.add(m["id"])
            if m["kind"] not in {"quota", "budget", "balance", "spend"} or m["scope"] not in {"individual", "team", "unknown"}:
                raise ValueError("Invalid metric kind or scope.")
            if not all(isinstance(m[k], str) and m[k] for k in ("label", "unit")):
                raise ValueError("Invalid metric label or unit.")
            for field in ("used", "limit", "remaining"):
                if m[field] is not None and (isinstance(m[field], bool) or not isinstance(m[field], (int, float))):
                    raise ValueError("Invalid metric amount.")
                number(m[field], negative=field == "remaining" and m["kind"] == "balance")
            timestamp(m["resets_at"])
            # Older caches predate the optional period field.
            if m.get("period") is not None and m["period"] not in PERIODS:
                raise ValueError("Invalid allowance period.")
        if p["primary_metric_id"] is not None and p["primary_metric_id"] not in ids:
            raise ValueError("Invalid primary metric.")
        if p["status"] == "ok" and (not p["metrics"] or p["fetched_at"] is None):
            raise ValueError("Invalid successful provider.")
    return snapshot
