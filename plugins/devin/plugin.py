"""Devin: the CLI credential file, `devin cloud drs whoami` for the organization, and two billing endpoints.

`billing/quota/usage` reports the team's shared on-demand balance (remaining only) and the
daily and weekly quotas as percentages. `billing/quota/my-on-demand-usage` reports what this
person drew from that shared balance in the current cycle, with the cycle's bounds.
"""

from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import shutil

from bars_plugin import CollectionError, command, http_json, login_found, metric, read_toml, result, timestamp, token

MISSING = "Devin login was not found. Run devin auth login."
UNREADABLE = "Devin login could not be read. Run devin auth login."
ORGANIZATION = re.compile(r"[A-Za-z0-9_-]{1,128}")
BALANCE_NOTE = "Devin reports only the remaining amount; purchased credits do not expire, so there is no reset."
QUOTA_URL = "https://app.devin.ai/api/%s/billing/quota/usage"
MY_USAGE_URL = "https://app.devin.ai/api/%s/billing/quota/my-on-demand-usage"
# A reported cycle of about this many days maps to the contract's named period lengths.
PERIOD_DAYS = ((1, "day"), (7, "week"), (30, "month"))


def obj(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CollectionError("Provider returned an invalid usage response.")
    return value


def cycle_period(start, end):
    """The named period length a reported cycle corresponds to, or None when it matches none."""
    if start is None or end is None:
        return None
    days = (datetime.fromisoformat(timestamp(end).replace("Z", "+00:00"))
            - datetime.fromisoformat(timestamp(start).replace("Z", "+00:00"))) / timedelta(days=1)
    for length, name in PERIOD_DAYS:
        if abs(days - length) <= (2 if length == 30 else 0.05):
            return name
    return None


def parse_my_usage(data):
    """This person's draw on the shared balance in the current cycle, or None when unreported.

    Devin reports the amount only, never a personal limit, so this is a `spend` with no bar.
    """
    data = obj(data)
    if data.get("amount") is None:
        return None
    end = data.get("cycle_end")
    return metric("my_on_demand", "My on-demand usage", "spend", "individual", "USD", used=data["amount"],
                  reset=end, period=cycle_period(data.get("cycle_start"), end))


def parse(data, my_usage=None):
    data = obj(data)
    metrics = []
    if data.get("overage_balance") is not None:
        metrics.append(metric("on_demand_balance", "On-demand balance", "balance", "team", "USD",
                              remaining=data["overage_balance"], note=BALANCE_NOTE))
    mine = parse_my_usage(my_usage) if my_usage is not None else None
    if mine is not None:
        metrics.append(mine)
    for period in ("daily", "weekly"):
        if period == "daily" and data.get("hide_daily_quota") is True:
            continue
        used = data.get(period + "_percentage")
        if used is not None:
            metrics.append(metric(period, period.capitalize() + " quota", "quota", "unknown", "%", used, 100,
                                  reset=data.get(period + "_reset_at"),
                                  period="day" if period == "daily" else "week"))
    return result("on_demand_balance", metrics, secondary=("my_on_demand", "daily", "weekly"))


def access_token():
    credentials = read_toml(Path.home() / ".local" / "share" / "devin" / "credentials.toml",
                            missing=MISSING, unreadable=UNREADABLE)
    try:
        return token(credentials["windsurf_api_key"])
    except (KeyError, TypeError):
        raise CollectionError(UNREADABLE, "login_required") from None


def cli():
    executable = shutil.which("devin")
    if not executable:
        raise CollectionError("Install the Devin CLI to discover its organization.", "setup")
    return executable


def collect():
    key = access_token()
    executable = cli()
    try:
        identity = json.loads(command([executable, "cloud", "drs", "whoami"], timeout=15))
        organization = identity["org_id"]
        if not isinstance(organization, str) or not ORGANIZATION.fullmatch(organization):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise CollectionError("Devin organization could not be read. Open the Devin CLI.", "login_required") from None
    # Do not trust endpoint overrides from the CLI credential file.
    headers = {"Authorization": "Bearer " + key, "x-cog-org-id": organization}
    quota = http_json(QUOTA_URL % organization, headers)
    # The personal figure is secondary: a transient failure there must not cost the balance
    # and quotas their refresh. A rejected login is reported, since both calls share it.
    try:
        my_usage = http_json(MY_USAGE_URL % organization, headers)
    except CollectionError as error:
        if error.status != "error":
            raise
        my_usage = None
    return parse(quota, my_usage)


def detect():
    return login_found(access_token, cli)
