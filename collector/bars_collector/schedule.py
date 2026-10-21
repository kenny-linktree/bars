"""Per-provider collection schedule: how often each provider is asked, and when next.

Private to the collector. The scheduled runner fires every minute; this module decides,
per provider, whether that run is due to ask it. Every provider starts at the one-minute
baseline. A rate limit doubles that provider's interval, up to an hour; sustained
successful scheduled fetches later shorten it again in small steps, never below the
baseline. Other failures (no login, an unreachable endpoint, a parser fault) are no
evidence about the provider's rate, so they neither slow nor speed it up.

The state lives in a private JSON file beside the staging snapshot, written atomically
under the collector's writer lock. It holds only provider ids, whole-second UTC epoch
times and counters: never credentials, account identities, quantities or responses. A
missing, unreadable, oversized, malformed or unsupported file is replaced by the
baseline rather than trusted, and stored times that the clock cannot have produced (set
back, or a due time further ahead than any wait this module records) count as due, so a
damaged file can never strand a provider.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import random

from bars_plugin.values import MAX_RETRY_AFTER

from .model import MAX_SNAPSHOT_BYTES
from .plugins import PLUGIN_ID
from .storage import read_private, write_private

STATE_VERSION = 1
# A schedule row is smaller than its required public provider row, so this accommodates
# every provider set that fits the snapshot while keeping reads and writes bounded.
MAX_STATE_BYTES = MAX_SNAPSHOT_BYTES
# Every provider starts here, the scheduled runner's own cadence; no provider is asked
# more often by scheduled runs.
BASELINE_INTERVAL = 60
# A rate limit doubles the interval up to this cap. Intervals stay whole minutes.
MAX_INTERVAL = 60 * 60
# Recovery: after this many successful scheduled fetches spanning at least this long
# from the first counted success, the interval shrinks by a tenth (rounded half up to
# whole minutes, at least one minute).
RECOVERY_SUCCESSES = 12
RECOVERY_SECONDS = 6 * 60 * 60
# The runner fires on the minute after a random wait of up to 5 seconds, and a fetch's
# next due time counts from when its run started. A provider counts as due this much
# early, so a run that starts a few seconds earlier in its minute than the last one did
# is not pushed to the following minute. Smaller than a minute less the runner's
# jitter, so it never lets a run a whole minute early through.
DUE_TOLERANCE = 20
# After a rate limit, the next attempt waits a further random share of the wait, so a
# provider's clients do not all return at the same second. Never negative.
JITTER_SHARE = 0.1
MAX_JITTER = 5 * 60
# A run lasts at most about two minutes; stored waits may exceed the longest pause by that.
RUN_SLACK = 5 * 60
MAX_SUCCESSES = 1000
EARLIEST = 946684800  # 2000-01-01T00:00:00Z
LATEST = 253402300799  # 9999-12-31T23:59:59Z
FIELDS = frozenset({"interval", "started", "due", "successes", "since"})
PROBLEMS = ("unreadable", "invalid", "unsupported")


def schedule_path(output):
    """The state file beside the snapshot `output`, like its lock file."""
    output = Path(output)
    return output.parent / (output.name + ".schedule")


def instant(epoch):
    """Canonical UTC text for a whole-second epoch time, as diagnostics print it."""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def faster(interval):
    """The interval after one recovery step: a tenth shorter, in whole minutes, at least one."""
    return max(BASELINE_INTERVAL, interval - 60 * max(1, (interval + 300) // 600))


def slower(interval):
    """The interval after a rate limit: doubled, up to the cap."""
    return min(interval * 2, MAX_INTERVAL)


def _whole(value, low, high):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def valid_entry(entry):
    return (isinstance(entry, dict) and set(entry) - {"pause_until"} == FIELDS
            and _whole(entry["interval"], BASELINE_INTERVAL, MAX_INTERVAL) and entry["interval"] % 60 == 0
            and all(_whole(entry[key], EARLIEST, LATEST) for key in ("started", "due", "since"))
            and _whole(entry["successes"], 0, MAX_SUCCESSES)
            and ("pause_until" not in entry
                 or (_whole(entry["pause_until"], EARLIEST, LATEST)
                     and entry["started"] <= entry["pause_until"] <= entry["due"])))


class Schedule:
    """Each provider's interval, next due time and recovery evidence. Times are epoch seconds."""

    def __init__(self, entries=None, draw=random.random):
        self.entries = dict(entries or {})
        self.draw = draw

    def interval(self, pid):
        entry = self.entries.get(pid)
        return entry["interval"] if entry else BASELINE_INTERVAL

    def due(self, pid):
        """The stored next due time, or None for a provider with no schedule yet."""
        entry = self.entries.get(pid)
        return entry["due"] if entry else None

    def trusted(self, pid, at):
        """Whether the provider's stored times could have come from this clock.

        A start in the future means the clock was set back; a due time further after
        its start than the longest pause plus a run means the file was not written by
        this module. Either way the times are ignored; the learned interval stays.
        """
        entry = self.entries.get(pid)
        return (entry is not None and entry["started"] <= at + DUE_TOLERANCE
                and entry["due"] - entry["started"] <= MAX_RETRY_AFTER + RUN_SLACK)

    def is_due(self, pid, at):
        """Whether a scheduled run starting at `at` should ask this provider."""
        if not self.trusted(pid, at):
            return True
        return at + DUE_TOLERANCE >= self.entries[pid]["due"]

    def pause_until(self, pid, at):
        """An active persisted rate-limit pause, including when its snapshot write failed."""
        if self.trusted(pid, at):
            until = self.entries[pid].get("pause_until")
            if until is not None and until > at:
                return until
        return None

    def _entry(self, pid, at):
        entry = self.entries.get(pid)
        if entry is None:
            entry = self.entries[pid] = dict(interval=BASELINE_INTERVAL, started=at, due=at, successes=0, since=at)
        if entry["since"] > at:
            # The clock was set back: the recovery window restarts rather than stalling.
            entry["since"] = at
            entry["successes"] = 0
        return entry

    def reserve(self, pid, started):
        """Record an attempt before it starts, so a run that is killed or crashes still
        counts it and the next scheduled run does not repeat it at once."""
        entry = self._entry(pid, started)
        entry.pop("pause_until", None)
        entry["started"] = started
        entry["due"] = started + entry["interval"]

    def record(self, pid, outcome, started, *, scheduled, attempted=None, pause=0):
        """Record an attempt's outcome. Returns "slower", "faster" or None.

        `outcome` is "rate_limited", "ok" or any failure category. A rate limit doubles the
        interval and makes the next attempt wait the longer of the new interval and
        `pause` (the snapshot's escalating pause, which already honors the provider's own
        figure) after `attempted`, plus a positive jitter, within the 24-hour bound. Only a
        scheduled success counts toward recovery; an explicit one only moves the due time.
        """
        entry = self._entry(pid, started)
        entry["started"] = started
        change = None
        if outcome == "rate_limited":
            interval = slower(entry["interval"])
            change = "slower" if interval != entry["interval"] else None
            entry.update(interval=interval, successes=0, since=started)
            wait = min(max(pause, interval), MAX_RETRY_AFTER)
            jitter = int(self.draw() * min(wait * JITTER_SHARE, MAX_JITTER))
            entry["due"] = (started if attempted is None else attempted) + min(wait + max(jitter, 0), MAX_RETRY_AFTER)
            entry["pause_until"] = entry["due"]
            return change
        entry.pop("pause_until", None)
        if outcome == "ok" and scheduled:
            if entry["successes"] == 0:
                entry["since"] = started
            entry["successes"] = min(entry["successes"] + 1, MAX_SUCCESSES)
            if entry["interval"] > BASELINE_INTERVAL and entry["successes"] >= RECOVERY_SUCCESSES \
                    and started - entry["since"] >= RECOVERY_SECONDS:
                entry.update(interval=faster(entry["interval"]), successes=0, since=started)
                change = "faster"
        entry["due"] = started + entry["interval"]
        return change

    def prune(self, ids):
        """Forget providers with no plugin, so one added again starts at the baseline.
        Returns whether anything was forgotten."""
        gone = [pid for pid in self.entries if pid not in ids]
        for pid in gone:
            del self.entries[pid]
        return bool(gone)

    def save(self, path):
        """Atomically write readable state. Returns False when oversized or unwritable."""
        data = {"version": STATE_VERSION, "providers": {pid: self.entries[pid] for pid in sorted(self.entries)}}
        text = json.dumps(data, separators=(",", ":"), sort_keys=True) + "\n"
        if len(text.encode("utf-8")) > MAX_STATE_BYTES:
            return False
        try:
            write_private(path, text)
        except OSError:
            return False
        return True


def load(path, draw=random.random):
    """The stored schedule and a problem category ("unreadable", "invalid", "unsupported")
    or None. A missing file is no problem; any problem yields the baseline for the providers
    it affects, never an exception."""
    try:
        content = read_private(path, MAX_STATE_BYTES)
    except FileNotFoundError:
        return Schedule(draw=draw), None
    except (OSError, ValueError):
        return Schedule(draw=draw), "unreadable"
    try:
        data = json.loads(content)
    except (ValueError, RecursionError, UnicodeError):
        return Schedule(draw=draw), "invalid"
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return Schedule(draw=draw), "unsupported"
    providers = data.get("providers")
    if not isinstance(providers, dict):
        return Schedule(draw=draw), "invalid"
    entries = {pid: dict(entry) for pid, entry in providers.items()
               if isinstance(pid, str) and PLUGIN_ID.fullmatch(pid) and valid_entry(entry)}
    return Schedule(entries, draw=draw), (None if len(entries) == len(providers) else "invalid")


# Diagnostics: one line per provider per run on the collector's standard error, in a fixed
# grammar the refresh runner checks before logging. Only ids, fixed words, whole numbers
# and canonical times appear: never messages, responses or headers.

def attempt_line(pid, outcome, interval, due=None, retry_after=None, change=None):
    parts = ["schedule:", pid, "attempted", "outcome=" + outcome, "interval=%ds" % interval]
    if retry_after is not None:
        parts.append("retry_after=%ds" % retry_after)
    if due is not None:
        parts.append("next=" + instant(due))
    if change:
        parts.append("change=" + change)
    return " ".join(parts)


def skip_line(pid, reason, interval, next_at=None):
    parts = ["schedule:", pid, "skipped", "reason=" + reason, "interval=%ds" % interval]
    if next_at is not None:
        parts.append("next=" + instant(next_at))
    return " ".join(parts)


def state_line(problem):
    if problem == "not_saved":
        return "schedule: state not_saved"
    return "schedule: state discarded reason=" + problem
