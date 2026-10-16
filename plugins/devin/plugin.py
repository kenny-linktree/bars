"""Devin: the CLI credential file, `devin cloud drs whoami` for the organization, and two billing endpoints.

`billing/quota/usage` reports the team's shared on-demand balance (remaining only) and the
daily and weekly quotas as percentages. `billing/quota/my-on-demand-usage` reports what this
person drew from that shared balance in the current cycle, with the cycle's bounds.
"""

import json
from pathlib import Path
import re
import shutil

from bars_plugin import CollectionError, command, http_json, login_found, metric, read_toml, result, token

MISSING = "Devin login was not found. Run devin auth login."
UNREADABLE = "Devin login could not be read. Run devin auth login."
ORGANIZATION = re.compile(r"[A-Za-z0-9_-]{1,128}")
BALANCE_NOTE = "Devin reports only the remaining amount; purchased credits do not expire, so there is no reset."
ON_DEMAND_NOTE = ("Used is your draw this cycle; left is what remains in the shared team pool. The bar compares the two, "
                  "since Devin does not report the pool total.")
QUOTA_URL = "https://app.devin.ai/api/%s/billing/quota/usage"
MY_USAGE_URL = "https://app.devin.ai/api/%s/billing/quota/my-on-demand-usage"


def obj(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CollectionError("Provider returned an invalid usage response.")
    return value


def on_demand(data, my_usage):
    """One bar for the shared on-demand credits: this person's draw in the current cycle against
    that draw plus what remains in the shared pool. Devin reports those two amounts and no pool
    total, so the limit is their sum and the note says so. The bar carries no period: the cycle
    is this person's, not the pool's, so no pace is computed against the derived limit. A pool
    in overdraft, or a missing personal figure, falls back to a remaining-only balance. None when
    neither amount is reported."""
    balance = obj(data).get("overage_balance")
    mine = obj(my_usage)
    used = mine.get("amount")
    if balance is None and used is None:
        return None
    if used is None or (balance is not None and balance < 0):
        return metric("on_demand", "On-demand credits", "balance", "team", "USD", remaining=balance, note=BALANCE_NOTE)
    limit = None if balance is None else float(used) + float(balance)
    return metric("on_demand", "On-demand credits", "budget", "team", "USD", used, limit, balance,
                  reset=mine.get("cycle_end"), note=ON_DEMAND_NOTE)


def parse(data, my_usage=None):
    data = obj(data)
    metrics = []
    # Metrics in period order, shortest first; the weekly quota still leads the overview.
    for period in ("daily", "weekly"):
        if period == "daily" and data.get("hide_daily_quota") is True:
            continue
        used = data.get(period + "_percentage")
        if used is not None:
            metrics.append(metric(period, period.capitalize() + " quota", "quota", "unknown", "%", used, 100,
                                  reset=data.get(period + "_reset_at"),
                                  period="day" if period == "daily" else "week"))
    credits = on_demand(data, my_usage)
    if credits is not None:
        metrics.append(credits)
    return result("weekly", metrics, alternatives=("daily", "on_demand"), secondary=("daily", "on_demand"))


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
