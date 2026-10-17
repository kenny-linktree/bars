"""Snapshot values a plugin returns: metrics, results and their normalization."""

from datetime import datetime, timezone
import math
import re

# Source-documented allowance period lengths. Readers derive the period start from
# the reset instant so pace can compare usage with elapsed time. None means unknown.
PERIODS = frozenset({"five_hours", "day", "week", "month"})
KINDS = frozenset({"budget", "quota", "balance", "spend"})
SCOPES = frozenset({"individual", "team", "unknown"})
STATUSES = frozenset({"ok", "error", "login_required", "setup"})
METRIC_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")
MAX_TEXT = 200
# Python 3.9's fromisoformat accepts only 3- or 6-digit fractions and colon offsets;
# 3.11 also accepts other fraction lengths, a comma separator and +HHMM. Normalize
# those so every supported interpreter reads provider timestamps the same way.
_FRACTION = re.compile(r"([T ]\d{2}:\d{2}:\d{2})[.,](\d+)")
_COMPACT_OFFSET = re.compile(r"([T ][\d:.]+[+-]\d{2})(\d{2})$")


# A rate limit's cooldown is bounded; a provider cannot park itself for longer than this.
MAX_RETRY_AFTER = 24 * 3600


class CollectionError(Exception):
    """A user-safe failure. `status` is `error`, `login_required` or `setup`.

    `retry_after` marks a rate limit: the provider asked for a pause of that many seconds
    (0 when it gave no usable figure). The worker does not retry such a failure within the
    run, and the collector pauses the provider's scheduled attempts, for at least that long.
    """

    def __init__(self, message="Usage is temporarily unavailable.", status="error", retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = None if retry_after is None else min(max(int(retry_after), 0), MAX_RETRY_AFTER)


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


def text(value, *, field="text"):
    """A short single-line string for labels, units and notes. Nothing else is accepted."""
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT or not value.isprintable():
        raise CollectionError("Provider returned an invalid %s." % field)
    return value


def metric(mid, label, kind, scope, unit, used=None, limit=None, remaining=None, reset=None, period=None,
           *, reset_estimated=False, note=None):
    """One allowance. Amounts are numbers or None; `remaining` is derived when both others are known.

    `reset_estimated` marks a reset the plugin derived from documentation rather than the
    source, such as the end of a documented calendar month. `note` is an optional sentence
    shown with a balance that has no total or used amount.
    """
    if not isinstance(mid, str) or not METRIC_ID.fullmatch(mid):
        raise ValueError("Invalid metric id.")
    if kind not in KINDS or scope not in SCOPES:
        raise ValueError("Unknown metric kind or scope.")
    if period is not None and period not in PERIODS:
        raise ValueError("Unknown allowance period.")
    used, limit = number(used), number(limit)
    remaining = number(remaining, negative=kind == "balance")
    if remaining is None and used is not None and limit is not None:
        remaining = max(limit - used, 0)
    return dict(id=mid, label=text(label, field="label"), kind=kind, scope=scope, unit=text(unit, field="unit"),
                used=used, limit=limit, remaining=remaining, resets_at=timestamp(reset), period=period,
                resets_at_estimated=bool(reset_estimated),
                note=None if note is None else text(note, field="note"))


def result(primary, metrics, *, alternatives=(), secondary=()):
    """The value `collect()` returns.

    Metrics without any quantity are dropped; a response with none is a failure, so it
    cannot replace last-good data. The primary is `primary` when it has a quantity, else
    the first available of `alternatives`, else None. `secondary` names the metrics the
    overview shows as mini bars beneath the primary; absent ids and the primary are dropped.
    """
    metrics = [m for m in metrics if any(m[k] is not None for k in ("used", "limit", "remaining"))]
    if not metrics:
        raise CollectionError("Provider returned no usable usage data.")
    available = {m["id"] for m in metrics}
    if len(available) != len(metrics):
        raise ValueError("Duplicate metric id.")
    selected = next((mid for mid in (primary, *alternatives) if mid in available), None)
    return {"primary_metric_id": selected,
            "secondary_metric_ids": [mid for mid in dict.fromkeys(secondary) if mid in available and mid != selected],
            "metrics": metrics}
