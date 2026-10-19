"""Cursor: the Cursor CLI Keychain login and the usage-summary endpoint."""

import base64
import json
import urllib.parse

from bars_plugin import CollectionError, http_json, keychain, keychain_entry_exists, metric, number, period_between, result, token

KEYCHAIN_SERVICE = "cursor-access-token"
MISSING = "Cursor login was not found. Install the Cursor CLI and run agent login."
USAGE_URL = "https://cursor.com/api/usage-summary"
ON_DEMAND = ("individual_on_demand", "team_on_demand")


def obj(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CollectionError("Provider returned an invalid usage response.")
    return value


def parse(data):
    data = obj(data)
    metrics = []
    individual, team = obj(data.get("individualUsage")), obj(data.get("teamUsage"))
    for mid, label, scope, source in (
        ("included", "Included usage", "individual", individual.get("plan")),
        ("individual_on_demand", "Individual on-demand", "individual", individual.get("onDemand")),
        ("team_on_demand", "Team on-demand", "team", team.get("onDemand")),
    ):
        source = obj(source)
        if not source or source.get("enabled") is False:
            continue
        # The billing cycle is monthly; its reported bounds confirm that, and a cycle of
        # another length (a trial, a changed plan) carries no period rather than a wrong one.
        period = period_between(data.get("billingCycleStart"), data.get("billingCycleEnd"), fallback="month")
        used, limit = number(source.get("used")), number(source.get("limit"))
        remaining = number(source.get("remaining")) if used is None or limit is None else None
        # The endpoint reports USD cents.
        metrics.append(metric(mid, label, "budget", scope, "USD",
                              None if used is None else used / 100, None if limit is None else limit / 100,
                              None if remaining is None else remaining / 100,
                              reset=data.get("billingCycleEnd"), period=period))
    return result("included", metrics, secondary=ON_DEMAND)


def collect():
    access_token = token(keychain(KEYCHAIN_SERVICE, missing=MISSING))
    try:
        part = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        subject = token(claims["sub"])
    except (ValueError, KeyError, IndexError, TypeError, RecursionError):
        raise CollectionError("Cursor login could not be read. Run agent login.", "login_required") from None
    # The unverified JWT claim only constructs the vendor's required session cookie.
    cookie = urllib.parse.quote(subject + "::" + access_token, safe="")
    return parse(http_json(USAGE_URL, {"Cookie": "WorkosCursorSessionToken=" + cookie}))


def detect():
    return keychain_entry_exists(KEYCHAIN_SERVICE)
