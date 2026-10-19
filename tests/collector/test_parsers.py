import math
import unittest

from datetime import datetime, timezone

from bars_plugin import CollectionError
from plugin_fixtures import parse, plugin

parse_claude, parse_codex, parse_cursor, parse_devin = (parse(pid) for pid in ("claude", "codex", "cursor", "devin"))


class ParserTests(unittest.TestCase):
    def test_claude_budget_without_rate_windows_and_separate_exponents(self):
        value = parse_claude({"five_hour": None, "seven_day": None, "spend": {
            "enabled": True, "used": {"amount_minor": 250, "exponent": 2, "currency": "USD"},
            "limit": {"amount_minor": 100, "exponent": 1, "currency": "USD"}}},
            now=datetime(2026, 10, 1, 12, tzinfo=timezone.utc))
        self.assertEqual(value["primary_metric_id"], "monthly_spend")
        self.assertEqual(value["secondary_metric_ids"], [])
        metric = value["metrics"][0]
        self.assertEqual((metric["used"], metric["limit"], metric["remaining"]), (2.5, 10, 7.5))
        self.assertEqual(metric["scope"], "unknown")
        # No reported reset: the documented UTC calendar month supplies an estimate, marked as such.
        self.assertEqual(metric["resets_at"], "2026-11-01T00:00:00Z")
        self.assertTrue(metric["resets_at_estimated"])

    def test_claude_estimated_month_end_follows_the_fetch_and_reported_resets_win(self):
        for fetched, expected in ((datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc), "2027-01-01T00:00:00Z"),
                                  (datetime(2028, 2, 29, 12, tzinfo=timezone.utc), "2028-03-01T00:00:00Z")):
            metric = parse_claude({"spend": {"used": {"amount_minor": 1, "exponent": 2, "currency": "USD"}}},
                            now=fetched)["metrics"][0]
            self.assertEqual((metric["resets_at"], metric["resets_at_estimated"]), (expected, True))
        reported = parse_claude({"spend": {"used": {"amount_minor": 1, "exponent": 2, "currency": "USD"},
                                     "resets_at": "2026-10-15T08:00:00Z"}})["metrics"][0]
        self.assertEqual((reported["resets_at"], reported["resets_at_estimated"]), ("2026-10-15T08:00:00Z", False))
        self.assertIsNone(reported["note"])

    def test_secondary_allowances_follow_each_plugin_rule(self):
        # Claude and Codex: the other general window, only when a quota is primary.
        value = parse_claude({"five_hour": {"utilization": 1}, "seven_day": {"utilization": 2}, "seven_day_opus": {"utilization": 3}})
        self.assertEqual((value["primary_metric_id"], value["secondary_metric_ids"]), ("five_hour", ["seven_day"]))
        value = parse_claude({"seven_day": {"utilization": 2}, "seven_day_opus": {"utilization": 3}})
        self.assertEqual((value["primary_metric_id"], value["secondary_metric_ids"]), ("seven_day", []))
        value = parse_claude({"spend": {"used": {"amount_minor": 1, "exponent": 2, "currency": "USD"}},
                        "five_hour": {"utilization": 1}, "seven_day": {"utilization": 2}})
        self.assertEqual((value["primary_metric_id"], value["secondary_metric_ids"]), ("monthly_spend", []))
        value = parse_codex({"rate_limit": {"primary_window": {"used_percent": 1}, "secondary_window": {"used_percent": 2}}})
        self.assertEqual((value["primary_metric_id"], value["secondary_metric_ids"]), ("primary_window", ["secondary_window"]))
        value = parse_codex({"spend_control": {"individual_limit": {"used": "1", "unit": "credit"}},
                       "rate_limit": {"primary_window": {"used_percent": 1}}})
        self.assertEqual((value["primary_metric_id"], value["secondary_metric_ids"]), ("monthly_credits", []))
        # Cursor: the on-demand allowances that are reported. Devin: the daily quota and the credits bar.
        value = parse_cursor({"individualUsage": {"plan": {"used": 1}, "onDemand": {"used": 1}}})
        self.assertEqual(value["secondary_metric_ids"], ["individual_on_demand"])
        value = parse_devin({"overage_balance": 0, "daily_percentage": 1, "weekly_percentage": 1})
        self.assertEqual(value["secondary_metric_ids"], ["daily", "on_demand"])
        self.assertEqual(value["metrics"][-1]["note"], plugin("devin").BALANCE_NOTE)
        self.assertIsNone(value["metrics"][0]["note"])

    def test_claude_missing_used_is_not_zero(self):
        metric = parse_claude({"spend": {"limit": {
            "amount_minor": 0, "exponent": 2, "currency": "USD"}}})["metrics"][0]
        self.assertIsNone(metric["used"])
        self.assertIsNone(metric["remaining"])
        self.assertEqual(metric["limit"], 0)

    def test_claude_currency_mismatch_rejected(self):
        with self.assertRaises(CollectionError):
            parse_claude({"spend": {"used": {"amount_minor": 3, "exponent": 2, "currency": "USD"},
                                       "limit": {"amount_minor": 4, "exponent": 2, "currency": "EUR"}}})

    def test_codex_preserves_individual_scope_and_overage(self):
        metric = parse_codex({"rate_limit": None, "spend_control": {"individual_limit": {
            "used": "130", "limit": "100", "unit": "credit", "reset_at": 1790899200}}})["metrics"][0]
        self.assertEqual(metric["scope"], "individual")
        self.assertEqual(metric["unit"], "credits")
        self.assertEqual(metric["used"], 130)
        self.assertEqual(metric["remaining"], 0)
        self.assertEqual(metric["resets_at"], "2026-10-02T00:00:00Z")

    def test_zero_limit_is_distinct_from_missing_limit(self):
        for limit in (None, "0"):
            metric = parse_codex({"spend_control": {"individual_limit": {
                "used": "0", "limit": limit, "unit": "credit"}}})["metrics"][0]
            self.assertEqual(metric["limit"], None if limit is None else 0)
            self.assertEqual(metric["remaining"], None if limit is None else 0)

    def test_codex_windows_take_periods_from_reported_lengths(self):
        # Ported from PR #2: the live response carries limit_window_seconds and epoch reset_at.
        value = parse_codex({"rate_limit": {
            "primary_window": {"used_percent": 100, "limit_window_seconds": 5 * 3_600, "reset_at": 1791011563},
            "secondary_window": {"used_percent": 27, "limit_window_seconds": 7 * 86_400, "reset_at": 1791580340}}})
        by_id = {m["id"]: m for m in value["metrics"]}
        self.assertEqual(by_id["primary_window"]["period"], "five_hours")
        self.assertEqual(by_id["primary_window"]["resets_at"], "2026-10-03T07:12:43Z")
        self.assertEqual(by_id["secondary_window"]["period"], "week")
        self.assertEqual(by_id["secondary_window"]["resets_at"], "2026-10-09T21:12:20Z")
        # Without a reported length each window keeps its documented default.
        value = parse_codex({"rate_limit": {"primary_window": {"used_percent": 1}, "secondary_window": {"used_percent": 1}}})
        self.assertEqual([m["period"] for m in value["metrics"]], [None, "week"])
        # An undocumented or malformed length carries no period rather than a wrong one.
        value = parse_codex({"rate_limit": {"primary_window": {"used_percent": 10, "limit_window_seconds": 3_600},
                                             "secondary_window": {"used_percent": 10, "limit_window_seconds": "soon"}}})
        self.assertEqual([m["period"] for m in value["metrics"]], [None, None])
        value = parse_codex({"rate_limit": {"primary_window": {"used_percent": 10, "limit_window_seconds": True}}})
        self.assertEqual([m["period"] for m in value["metrics"]], [None])

    def test_codex_subscription_uses_session_quota(self):
        value = parse_codex({"rate_limit": {"primary_window": {"used_percent": 20}}})
        self.assertEqual(value["primary_metric_id"], "primary_window")
        self.assertEqual(value["metrics"][0]["used"], 20)

    def test_subscription_priority_and_missing_windows(self):
        cases = (
            (parse_claude, lambda quotas: quotas, "utilization",
             ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"]),
            (parse_codex, lambda quotas: {"rate_limit": quotas}, "used_percent",
             ["primary_window", "secondary_window"]),
        )
        for parse, wrap, field, ids in cases:
            for index, expected in enumerate(ids):
                with self.subTest(provider=parse.__module__, primary=expected):
                    # Input order and a zero used amount must not affect selection.
                    quotas = {key: {field: 0} for key in reversed(ids[index:])}
                    quotas.update({key: None for key in ids[:index]})
                    value = parse(wrap(quotas))
                    self.assertEqual(value["primary_metric_id"], expected)
                    self.assertEqual([m["id"] for m in value["metrics"]], ids[index:])
                    self.assertTrue(all(m["remaining"] == 100 for m in value["metrics"]))

    def test_monthly_budget_stays_primary_when_quotas_are_also_reported(self):
        claude = parse_claude({"spend": {"limit": {
            "amount_minor": 0, "exponent": 2, "currency": "USD"}},
            "five_hour": {"utilization": 95}})
        codex = parse_codex({"spend_control": {"individual_limit": {
            "unit": "credit", "remaining": 0}}, "rate_limit": {"primary_window": {"used_percent": 95}}})
        self.assertEqual(claude["primary_metric_id"], "monthly_spend")
        self.assertEqual(codex["primary_metric_id"], "monthly_credits")
        self.assertEqual(len(claude["metrics"]), 2)
        self.assertEqual(len(codex["metrics"]), 2)

    def test_unavailable_budget_uses_reported_quotas_without_inventing_money(self):
        for spend in (None, {}, {"enabled": False}, {"used": None, "limit": None}):
            value = parse_claude({"spend": spend, "five_hour": {"utilization": 20}})
            self.assertEqual(value["primary_metric_id"], "five_hour")
            self.assertEqual([m["unit"] for m in value["metrics"]], ["%"])
        for budget in (None, {}, {"unit": "credit", "used": None, "limit": None}):
            value = parse_codex({"spend_control": {"individual_limit": budget},
                                  "rate_limit": {"secondary_window": {"used_percent": 30}}})
            self.assertEqual(value["primary_metric_id"], "secondary_window")
            self.assertEqual([m["unit"] for m in value["metrics"]], ["%"])

    def test_malformed_budget_is_not_hidden_by_valid_quotas(self):
        with self.assertRaises(CollectionError):
            parse_claude({"spend": {"used": {"amount_minor": "bad"}},
                           "five_hour": {"utilization": 20}})
        with self.assertRaises(CollectionError):
            parse_codex({"spend_control": {"individual_limit": {"unit": "credit", "used": -1}},
                          "rate_limit": {"primary_window": {"used_percent": 20}}})

    def test_periods_come_from_reported_lengths_through_one_helper(self):
        # The SDK turns a reported window length into a period the same way for every plugin.
        from bars_plugin import period_between, period_for_length
        self.assertEqual([period_for_length(s) for s in (5 * 3_600, 86_400, 7 * 86_400, 30 * 86_400, 28 * 86_400, 31 * 86_400)],
                         ["five_hours", "day", "week", "month", "month", "month"])
        self.assertEqual(period_for_length(None, "week"), "week")
        for bad in (3_600, 27 * 86_400, 32 * 86_400, 30 * 86_400 + 0.5, True, "18000", float("nan"), -86_400):
            self.assertIsNone(period_for_length(bad, "week"), bad)
        self.assertEqual(period_between("2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z"), "month")
        self.assertEqual(period_between(1759276800, 1759881600), "week")  # epoch seconds, as some sources report
        self.assertEqual(period_between(None, "2026-11-01T00:00:00Z", "month"), "month")
        self.assertEqual(period_between("not a time", "2026-11-01T00:00:00Z", "month"), "month")
        self.assertIsNone(period_between("2026-10-01T00:00:00Z", "2026-10-15T00:00:00Z", "month"))

    def test_cursor_billing_cycle_bounds_confirm_the_monthly_period(self):
        base = {"individualUsage": {"plan": {"used": 100, "limit": 10000}}}
        monthly = parse_cursor({**base, "billingCycleStart": "2026-10-01T16:43:25Z", "billingCycleEnd": "2026-10-31T16:43:25Z"})
        self.assertEqual(monthly["metrics"][0]["period"], "month")
        # Bounds absent: the documented default. Bounds of another length: no period.
        self.assertEqual(parse_cursor(base)["metrics"][0]["period"], "month")
        trial = parse_cursor({**base, "billingCycleStart": "2026-10-01T00:00:00Z", "billingCycleEnd": "2026-10-15T00:00:00Z"})
        self.assertIsNone(trial["metrics"][0]["period"])

    def test_cursor_equal_amounts_never_merge_scopes(self):
        allowance = {"enabled": True, "used": 250, "limit": 1000}
        value = parse_cursor({"billingCycleEnd": "2026-11-01T08:00:00+08:00",
                                "individualUsage": {"plan": allowance, "onDemand": allowance},
                                "teamUsage": {"onDemand": allowance}})
        self.assertEqual(value["primary_metric_id"], "included")
        self.assertEqual(len(value["metrics"]), 3)
        self.assertEqual([m["scope"] for m in value["metrics"]], ["individual", "individual", "team"])
        self.assertEqual([m["used"] for m in value["metrics"]], [2.5] * 3)
        self.assertEqual(value["metrics"][0]["resets_at"], "2026-11-01T00:00:00Z")

    def test_cursor_missing_used_is_not_zero(self):
        value = parse_cursor({"individualUsage": {"plan": {"enabled": True, "limit": 1000}}})
        self.assertIsNone(value["metrics"][0]["used"])
        self.assertIsNone(value["metrics"][0]["remaining"])

    def test_reported_remaining_survives_missing_used(self):
        codex = parse_codex({"spend_control": {"individual_limit": {
            "limit": "100", "remaining": "75", "unit": "credit"}}})["metrics"][0]
        cursor = parse_cursor({"individualUsage": {"plan": {
            "limit": 10000, "remaining": 7500}}})["metrics"][0]
        for value in (codex, cursor):
            self.assertIsNone(value["used"])
            self.assertEqual(value["limit"], 100)
            self.assertEqual(value["remaining"], 75)

    def test_cursor_disabled_primary_does_not_select_team(self):
        value = parse_cursor({"individualUsage": {"plan": {"enabled": False, "limit": 1000}},
                                "teamUsage": {"onDemand": {"used": 100, "limit": 1000}}})
        self.assertIsNone(value["primary_metric_id"])

    def test_devin_weekly_quota_leads_and_credits_follow(self):
        value = parse_devin({"is_quota_plan": True, "has_quota_allocation": True,
                             "overage_balance": -2.5, "daily_percentage": 0, "weekly_percentage": 120,
                             "daily_reset_at": "2026-10-02T00:00:00Z", "weekly_reset_at": None,
                             "hide_daily_quota": False})
        daily, weekly, credits = value["metrics"]
        self.assertEqual(value["primary_metric_id"], "weekly")
        self.assertEqual(value["secondary_metric_ids"], ["daily", "on_demand"])
        self.assertEqual(weekly["used"], 120)
        self.assertEqual(weekly["remaining"], 0)
        self.assertEqual(daily["remaining"], 100)
        # Without the personal figure the credits stay a balance: remaining only, no bar, no reset.
        self.assertEqual((credits["kind"], credits["scope"], credits["remaining"]), ("balance", "team", -2.5))
        self.assertIsNone(credits["used"])
        self.assertIsNone(credits["limit"])
        self.assertIsNone(credits["resets_at"])
        # Without a weekly figure the daily quota leads; without either, the credits do.
        self.assertEqual(parse_devin({"overage_balance": 1, "daily_percentage": 3})["primary_metric_id"], "daily")
        self.assertEqual(parse_devin({"overage_balance": 1})["primary_metric_id"], "on_demand")

    def test_devin_credits_bar_compares_own_draw_with_the_shared_remainder(self):
        value = parse_devin({"overage_balance": 10, "weekly_percentage": 5},
                            {"amount": 12.5, "cycle_start": "2026-10-01T00:00:00Z", "cycle_end": "2026-11-01T00:00:00Z"})
        credits = next(m for m in value["metrics"] if m["id"] == "on_demand")
        self.assertEqual((credits["kind"], credits["scope"], credits["unit"]), ("budget", "team", "USD"))
        # used is this person's draw, remaining the pool's balance, and the limit their sum: never a pool total.
        self.assertEqual((credits["used"], credits["remaining"], credits["limit"]), (12.5, 10, 22.5))
        self.assertEqual(credits["resets_at"], "2026-11-01T00:00:00Z")
        # No period: the cycle is personal and the limit derived, so the readers compute no pace.
        self.assertIsNone(credits["period"])
        self.assertEqual(credits["note"], plugin("devin").ON_DEMAND_NOTE)
        # A draw without a balance has no bar; neither figure means no credits metric at all.
        alone = next(m for m in parse_devin({"weekly_percentage": 5}, {"amount": 3})["metrics"] if m["id"] == "on_demand")
        self.assertEqual((alone["used"], alone["limit"], alone["remaining"]), (3, None, None))
        self.assertEqual([m["id"] for m in parse_devin({"weekly_percentage": 5}, {"cycle_end": "x"})["metrics"]], ["weekly"])
        # An overdrawn pool stays a balance (a budget cannot have negative remaining), so the
        # quotas still update; the overdraft shows as a negative amount left.
        overdrawn = parse_devin({"weekly_percentage": 5, "overage_balance": -2.5}, {"amount": 12.5})
        credits = next(m for m in overdrawn["metrics"] if m["id"] == "on_demand")
        self.assertEqual((credits["kind"], credits["used"], credits["limit"], credits["remaining"]), ("balance", None, None, -2.5))
        # Numeric strings, which the balance accepts elsewhere, still compare and sum as numbers.
        text = next(m for m in parse_devin({"overage_balance": "10"}, {"amount": "2.5"})["metrics"] if m["id"] == "on_demand")
        self.assertEqual((text["kind"], text["used"], text["limit"], text["remaining"]), ("budget", 2.5, 12.5, 10))
        text = next(m for m in parse_devin({"overage_balance": "-1"}, {"amount": 2})["metrics"] if m["id"] == "on_demand")
        self.assertEqual((text["kind"], text["remaining"]), ("balance", -1))
        with self.assertRaises(CollectionError):
            parse_devin({"overage_balance": 10}, {"amount": -1})
        with self.assertRaises(CollectionError):
            parse_devin({"overage_balance": "lots"}, {"amount": 1})

    def test_devin_hidden_daily_quota(self):
        value = parse_devin({"overage_balance": 0, "daily_percentage": 30, "hide_daily_quota": True})
        self.assertEqual([m["id"] for m in value["metrics"]], ["on_demand"])

    def test_empty_malformed_and_nonfinite_responses_fail(self):
        for parse in (parse_claude, parse_codex, parse_cursor, parse_devin):
            for data in ({}, [], {"irrelevant": 1}):
                with self.subTest(parse=parse.__module__, data=data), self.assertRaises(CollectionError):
                    parse(data)
        for amount in (math.nan, math.inf, "NaN", True, {}, -1):
            with self.subTest(amount=amount), self.assertRaises(CollectionError):
                parse_codex({"spend_control": {"individual_limit": {"used": amount, "unit": "credit"}}})

    def test_periods_follow_documented_allowance_lengths(self):
        claude = parse_claude({"spend": {"used": {"amount_minor": 1, "exponent": 2, "currency": "USD"}},
                                 "five_hour": {"utilization": 1}, "seven_day": {"utilization": 1}})
        self.assertEqual([m["period"] for m in claude["metrics"]], ["month", "five_hours", "week"])
        codex = parse_codex({"spend_control": {"individual_limit": {"used": "1", "unit": "credit"}},
                               "rate_limit": {"primary_window": {"used_percent": 1},
                                              "secondary_window": {"used_percent": 1}}})
        self.assertEqual([m["period"] for m in codex["metrics"]], ["month", None, "week"])
        cursor = parse_cursor({"individualUsage": {"plan": {"used": 1}, "onDemand": {"used": 1}}})
        self.assertEqual([m["period"] for m in cursor["metrics"]], ["month", "month"])
        devin = parse_devin({"overage_balance": 0, "daily_percentage": 1, "weekly_percentage": 1})
        self.assertEqual([m["period"] for m in devin["metrics"]], ["day", "week", None])

    def test_invalid_source_reset_is_not_silently_lost(self):
        with self.assertRaises(CollectionError):
            parse_devin({"weekly_percentage": 30, "weekly_reset_at": "tomorrow"})


if __name__ == "__main__":
    unittest.main()
