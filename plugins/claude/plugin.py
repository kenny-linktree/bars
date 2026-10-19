"""Claude: the Claude Code OAuth login and the oauth usage endpoint."""

from datetime import datetime, timezone
import json

from bars_plugin import CollectionError, http_json, keychain, keychain_entry_exists, metric, number, result, token

KEYCHAIN_SERVICE = "Claude Code-credentials"
MISSING = "Claude Code login was not found. Install Claude Code and sign in."
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
QUOTAS = (("five_hour", "5-hour quota", "five_hours"), ("seven_day", "Weekly quota", "week"),
          ("seven_day_opus", "Weekly Opus quota", "week"), ("seven_day_sonnet", "Weekly Sonnet quota", "week"))
# The general session and weekly windows; model-specific quotas stay in details.
GENERAL_WINDOWS = ("five_hour", "seven_day")


def obj(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CollectionError("Provider returned an invalid usage response.")
    return value


def money(value):
    value = obj(value)
    amount = number(value.get("amount_minor"))
    if amount is None:
        return None, None
    exponent = value.get("exponent")
    currency = value.get("currency")
    if isinstance(exponent, bool) or not isinstance(exponent, int) or not 0 <= exponent <= 9:
        raise CollectionError("Provider returned an invalid currency exponent.")
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise CollectionError("Provider returned an invalid currency.")
    return amount / (10 ** exponent), currency.upper()


def month_end(moment):
    """The end of the documented UTC calendar month containing `moment`.

    https://support.claude.com/en/articles/14782391-claude-enterprise-consumption-guide
    """
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def parse(data, now=None):
    data = obj(data)
    metrics = []
    spend = obj(data.get("spend"))
    if spend and spend.get("enabled") is not False:
        used, used_unit = money(spend.get("used"))
        limit, limit_unit = money(spend.get("limit"))
        if used_unit and limit_unit and used_unit != limit_unit:
            raise CollectionError("Provider returned inconsistent currencies.")
        if used_unit or limit_unit:
            reset, estimated = spend.get("resets_at"), False
            if reset is None:
                # Anchor the estimate to the fetch, so stale data keeps its original period.
                reset, estimated = month_end(now or datetime.now(timezone.utc)).isoformat(), True
            metrics.append(metric("monthly_spend", "Monthly budget", "budget", "unknown", used_unit or limit_unit,
                                  used, limit, reset=reset, period="month", reset_estimated=estimated))
    for key, label, period in QUOTAS:
        quota = obj(data.get(key))
        if quota.get("utilization") is not None:
            metrics.append(metric(key, label, "quota", "individual", "%", quota["utilization"], 100,
                                  reset=quota.get("resets_at"), period=period))
    value = result("monthly_spend", metrics, alternatives=[key for key, _, _ in QUOTAS])
    # Quota-only accounts show the other general window as a mini bar; a monthly
    # budget keeps the overview row to its single bar.
    if value["primary_metric_id"] != "monthly_spend":
        present = {m["id"] for m in value["metrics"]}
        value["secondary_metric_ids"] = [k for k in GENERAL_WINDOWS if k in present and k != value["primary_metric_id"]]
    return value


def access_token():
    try:
        credentials = json.loads(keychain(KEYCHAIN_SERVICE, missing=MISSING))
        return token(credentials["claudeAiOauth"]["accessToken"])
    except (ValueError, KeyError, TypeError, RecursionError):
        raise CollectionError("Claude login could not be read. Open Claude Code.", "login_required") from None


def collect():
    return parse(http_json(USAGE_URL, {"Authorization": "Bearer " + access_token(),
                                       "anthropic-beta": "oauth-2025-04-20"}))


def detect():
    return keychain_entry_exists(KEYCHAIN_SERVICE)
