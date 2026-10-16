"""Devin: the CLI credential file, `devin cloud drs whoami` for the organization, and the quota endpoint."""

import json
from pathlib import Path
import re
import shutil

from bars_plugin import CollectionError, command, http_json, login_found, metric, read_toml, result, token

MISSING = "Devin login was not found. Run devin auth login."
UNREADABLE = "Devin login could not be read. Run devin auth login."
ORGANIZATION = re.compile(r"[A-Za-z0-9_-]{1,128}")
BALANCE_NOTE = "Devin reports only the remaining amount; purchased credits do not expire, so there is no reset."


def obj(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CollectionError("Provider returned an invalid usage response.")
    return value


def parse(data):
    data = obj(data)
    metrics = []
    if data.get("overage_balance") is not None:
        metrics.append(metric("on_demand_balance", "On-demand balance", "balance", "team", "USD",
                              remaining=data["overage_balance"], note=BALANCE_NOTE))
    for period in ("daily", "weekly"):
        if period == "daily" and data.get("hide_daily_quota") is True:
            continue
        used = data.get(period + "_percentage")
        if used is not None:
            metrics.append(metric(period, period.capitalize() + " quota", "quota", "unknown", "%", used, 100,
                                  reset=data.get(period + "_reset_at"),
                                  period="day" if period == "daily" else "week"))
    return result("on_demand_balance", metrics, secondary=("daily", "weekly"))


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
    return parse(http_json("https://app.devin.ai/api/%s/billing/quota/usage" % organization,
                           {"Authorization": "Bearer " + key, "x-cog-org-id": organization}))


def detect():
    return login_found(access_token, cli)
