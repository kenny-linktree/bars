"""Pure adapters for the vendors' usage response shapes."""

from .model import CollectionError, metric, number, result


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


def claude(data):
    data = obj(data)
    metrics = []
    spend = obj(data.get("spend"))
    if spend and spend.get("enabled") is not False:
        used, used_unit = money(spend.get("used"))
        limit, limit_unit = money(spend.get("limit"))
        if used_unit and limit_unit and used_unit != limit_unit:
            raise CollectionError("Provider returned inconsistent currencies.")
        if used_unit or limit_unit:
            metrics.append(metric("monthly_spend", "Monthly budget", "budget", "unknown", used_unit or limit_unit,
                                  used, limit, reset=spend.get("resets_at"), period="month"))
    for key, label, period in (("five_hour", "5-hour quota", "five_hours"), ("seven_day", "Weekly quota", "week"),
                               ("seven_day_opus", "Weekly Opus quota", "week"),
                               ("seven_day_sonnet", "Weekly Sonnet quota", "week")):
        quota = obj(data.get(key))
        if quota.get("utilization") is not None:
            metrics.append(metric(key, label, "quota", "individual", "%", quota["utilization"], 100,
                                  reset=quota.get("resets_at"), period=period))
    return result("monthly_spend", metrics,
                  alternatives=("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"))


def codex(data):
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
    # The response reports each window's length in seconds. Documented lengths map
    # to allowance periods. An absent field keeps each window's documented default;
    # an unmappable or malformed length means no pace rather than a wrong one.
    periods = {5 * 3_600: "five_hours", 7 * 86_400: "week"}
    for key, label, fallback in (("primary_window", "Session quota", None),
                                 ("secondary_window", "Weekly quota", "week")):
        quota = obj(limits.get(key))
        if quota.get("used_percent") is not None:
            seconds = quota.get("limit_window_seconds")
            period = fallback
            if seconds is not None:
                period = periods.get(seconds) if isinstance(seconds, int) and not isinstance(seconds, bool) else None
            metrics.append(metric(key, label, "quota", "individual", "%", quota["used_percent"], 100,
                                  reset=quota.get("reset_at"), period=period))
    return result("monthly_credits", metrics, alternatives=("primary_window", "secondary_window"))


def cursor(data):
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
        used, limit = number(source.get("used")), number(source.get("limit"))
        remaining = number(source.get("remaining")) if used is None or limit is None else None
        metrics.append(metric(mid, label, "budget", scope, "USD",
                              None if used is None else used / 100, None if limit is None else limit / 100,
                              None if remaining is None else remaining / 100,
                              reset=data.get("billingCycleEnd"), period="month"))
    return result("included", metrics)


def devin(data):
    data = obj(data)
    metrics = []
    if data.get("overage_balance") is not None:
        metrics.append(metric("on_demand_balance", "On-demand balance", "balance", "team", "USD",
                              remaining=data["overage_balance"]))
    for period in ("daily", "weekly"):
        if period == "daily" and data.get("hide_daily_quota") is True:
            continue
        used = data.get(period + "_percentage")
        if used is not None:
            metrics.append(metric(period, period.capitalize() + " quota", "quota", "unknown", "%", used, 100,
                                  reset=data.get(period + "_reset_at"),
                                  period="day" if period == "daily" else "week"))
    return result("on_demand_balance", metrics)
