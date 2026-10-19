"""Codex: the ChatGPT login in ~/.codex/auth.json and the wham usage endpoint."""

from pathlib import Path

from bars_plugin import CollectionError, http_json, login_found, metric, period_for_length, read_json, result, token

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
# The response reports each window's length as `limit_window_seconds`; the documented lengths
# map to allowance periods. Without the field a window keeps its documented default (session:
# none, weekly: `week`); an unmappable or malformed length means no period rather than a wrong one.
WINDOWS = (("primary_window", "Session quota", None), ("secondary_window", "Weekly quota", "week"))


def obj(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CollectionError("Provider returned an invalid usage response.")
    return value


def parse(data):
    data = obj(data)
    metrics = []
    budget = obj(obj(data.get("spend_control")).get("individual_limit"))
    if budget:
        unit = budget.get("unit")
        if unit == "credit":
            unit = "credits"
        if not isinstance(unit, str) or not unit.strip() or len(unit) > 24:
            raise CollectionError("Provider returned an invalid allowance unit.")
        # Retain a reported remainder when a source omits either used or limit.
        remaining = budget.get("remaining") if budget.get("used") is None or budget.get("limit") is None else None
        metrics.append(metric("monthly_credits", "Monthly credits", "budget", "individual", unit,
                              budget.get("used"), budget.get("limit"), remaining, reset=budget.get("reset_at"),
                              period="month"))
    limits = obj(data.get("rate_limit"))
    for key, label, fallback in WINDOWS:
        quota = obj(limits.get(key))
        if quota.get("used_percent") is not None:
            metrics.append(metric(key, label, "quota", "individual", "%", quota["used_percent"], 100,
                                  reset=quota.get("reset_at"),
                                  period=period_for_length(quota.get("limit_window_seconds"), fallback)))
    value = result("monthly_credits", metrics, alternatives=[key for key, _, _ in WINDOWS])
    # Quota-only accounts show the other window as a mini bar beneath the primary.
    if value["primary_metric_id"] != "monthly_credits":
        present = {m["id"] for m in value["metrics"]}
        value["secondary_metric_ids"] = [k for k, _, _ in WINDOWS if k in present and k != value["primary_metric_id"]]
    return value


def credentials():
    # Use the actual user's login, not a worker's temporary CODEX_HOME.
    data = read_json(Path.home() / ".codex" / "auth.json")
    try:
        tokens = data["tokens"]
        return token(tokens["access_token"]), token(tokens["account_id"])
    except (KeyError, TypeError):
        raise CollectionError("Codex needs a ChatGPT login for usage data. Run codex login.", "setup") from None


def collect():
    access_token, account_id = credentials()
    return parse(http_json(USAGE_URL, {"Authorization": "Bearer " + access_token, "ChatGPT-Account-Id": account_id}))


def detect():
    return login_found(credentials)
