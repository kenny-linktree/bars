import math
import unittest

from bars_collector import parsers
from bars_collector.model import CollectionError


class ParserTests(unittest.TestCase):
    def test_claude_budget_without_rate_windows_and_separate_exponents(self):
        value = parsers.claude({"five_hour": None, "seven_day": None, "spend": {
            "enabled": True, "used": {"amount_minor": 250, "exponent": 2, "currency": "USD"},
            "limit": {"amount_minor": 100, "exponent": 1, "currency": "USD"}}})
        self.assertEqual(value["primary_metric_id"], "monthly_spend")
        metric = value["metrics"][0]
        self.assertEqual((metric["used"], metric["limit"], metric["remaining"]), (2.5, 10, 7.5))
        self.assertEqual(metric["scope"], "unknown")
        self.assertIsNone(metric["resets_at"])

    def test_claude_missing_used_is_not_zero(self):
        metric = parsers.claude({"spend": {"limit": {
            "amount_minor": 0, "exponent": 2, "currency": "USD"}}})["metrics"][0]
        self.assertIsNone(metric["used"])
        self.assertIsNone(metric["remaining"])
        self.assertEqual(metric["limit"], 0)

    def test_claude_currency_mismatch_rejected(self):
        with self.assertRaises(CollectionError):
            parsers.claude({"spend": {"used": {"amount_minor": 3, "exponent": 2, "currency": "USD"},
                                       "limit": {"amount_minor": 4, "exponent": 2, "currency": "EUR"}}})

    def test_codex_preserves_individual_scope_and_overage(self):
        metric = parsers.codex({"rate_limit": None, "spend_control": {"individual_limit": {
            "used": "130", "limit": "100", "unit": "credit", "reset_at": 1790899200}}})["metrics"][0]
        self.assertEqual(metric["scope"], "individual")
        self.assertEqual(metric["unit"], "credits")
        self.assertEqual(metric["used"], 130)
        self.assertEqual(metric["remaining"], 0)
        self.assertEqual(metric["resets_at"], "2026-10-02T00:00:00Z")

    def test_zero_limit_is_distinct_from_missing_limit(self):
        for limit in (None, "0"):
            metric = parsers.codex({"spend_control": {"individual_limit": {
                "used": "0", "limit": limit, "unit": "credit"}}})["metrics"][0]
            self.assertEqual(metric["limit"], None if limit is None else 0)
            self.assertEqual(metric["remaining"], None if limit is None else 0)

    def test_codex_subscription_uses_session_quota(self):
        value = parsers.codex({"rate_limit": {"primary_window": {"used_percent": 20}}})
        self.assertEqual(value["primary_metric_id"], "primary_window")
        self.assertEqual(value["metrics"][0]["used"], 20)

    def test_subscription_priority_and_missing_windows(self):
        cases = (
            (parsers.claude, lambda quotas: quotas, "utilization",
             ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"]),
            (parsers.codex, lambda quotas: {"rate_limit": quotas}, "used_percent",
             ["primary_window", "secondary_window"]),
        )
        for parse, wrap, field, ids in cases:
            for index, expected in enumerate(ids):
                with self.subTest(provider=parse.__name__, primary=expected):
                    # Input order and a zero used amount must not affect selection.
                    quotas = {key: {field: 0} for key in reversed(ids[index:])}
                    quotas.update({key: None for key in ids[:index]})
                    value = parse(wrap(quotas))
                    self.assertEqual(value["primary_metric_id"], expected)
                    self.assertEqual([m["id"] for m in value["metrics"]], ids[index:])
                    self.assertTrue(all(m["remaining"] == 100 for m in value["metrics"]))

    def test_monthly_budget_stays_primary_when_quotas_are_also_reported(self):
        claude = parsers.claude({"spend": {"limit": {
            "amount_minor": 0, "exponent": 2, "currency": "USD"}},
            "five_hour": {"utilization": 95}})
        codex = parsers.codex({"spend_control": {"individual_limit": {
            "unit": "credit", "remaining": 0}}, "rate_limit": {"primary_window": {"used_percent": 95}}})
        self.assertEqual(claude["primary_metric_id"], "monthly_spend")
        self.assertEqual(codex["primary_metric_id"], "monthly_credits")
        self.assertEqual(len(claude["metrics"]), 2)
        self.assertEqual(len(codex["metrics"]), 2)

    def test_unavailable_budget_uses_reported_quotas_without_inventing_money(self):
        for spend in (None, {}, {"enabled": False}, {"used": None, "limit": None}):
            value = parsers.claude({"spend": spend, "five_hour": {"utilization": 20}})
            self.assertEqual(value["primary_metric_id"], "five_hour")
            self.assertEqual([m["unit"] for m in value["metrics"]], ["%"])
        for budget in (None, {}, {"unit": "credit", "used": None, "limit": None}):
            value = parsers.codex({"spend_control": {"individual_limit": budget},
                                  "rate_limit": {"secondary_window": {"used_percent": 30}}})
            self.assertEqual(value["primary_metric_id"], "secondary_window")
            self.assertEqual([m["unit"] for m in value["metrics"]], ["%"])

    def test_malformed_budget_is_not_hidden_by_valid_quotas(self):
        with self.assertRaises(CollectionError):
            parsers.claude({"spend": {"used": {"amount_minor": "bad"}},
                           "five_hour": {"utilization": 20}})
        with self.assertRaises(CollectionError):
            parsers.codex({"spend_control": {"individual_limit": {"unit": "credit", "used": -1}},
                          "rate_limit": {"primary_window": {"used_percent": 20}}})

    def test_cursor_equal_amounts_never_merge_scopes(self):
        allowance = {"enabled": True, "used": 250, "limit": 1000}
        value = parsers.cursor({"billingCycleEnd": "2026-11-01T08:00:00+08:00",
                                "individualUsage": {"plan": allowance, "onDemand": allowance},
                                "teamUsage": {"onDemand": allowance}})
        self.assertEqual(value["primary_metric_id"], "included")
        self.assertEqual(len(value["metrics"]), 3)
        self.assertEqual([m["scope"] for m in value["metrics"]], ["individual", "individual", "team"])
        self.assertEqual([m["used"] for m in value["metrics"]], [2.5] * 3)
        self.assertEqual(value["metrics"][0]["resets_at"], "2026-11-01T00:00:00Z")

    def test_cursor_missing_used_is_not_zero(self):
        value = parsers.cursor({"individualUsage": {"plan": {"enabled": True, "limit": 1000}}})
        self.assertIsNone(value["metrics"][0]["used"])
        self.assertIsNone(value["metrics"][0]["remaining"])

    def test_reported_remaining_survives_missing_used(self):
        codex = parsers.codex({"spend_control": {"individual_limit": {
            "limit": "100", "remaining": "75", "unit": "credit"}}})["metrics"][0]
        cursor = parsers.cursor({"individualUsage": {"plan": {
            "limit": 10000, "remaining": 7500}}})["metrics"][0]
        for value in (codex, cursor):
            self.assertIsNone(value["used"])
            self.assertEqual(value["limit"], 100)
            self.assertEqual(value["remaining"], 75)

    def test_cursor_disabled_primary_does_not_select_team(self):
        value = parsers.cursor({"individualUsage": {"plan": {"enabled": False, "limit": 1000}},
                                "teamUsage": {"onDemand": {"used": 100, "limit": 1000}}})
        self.assertIsNone(value["primary_metric_id"])

    def test_devin_balance_is_not_spend_or_monthly(self):
        value = parsers.devin({"is_quota_plan": True, "has_quota_allocation": True,
                               "overage_balance": -2.5, "daily_percentage": 0, "weekly_percentage": 120,
                               "daily_reset_at": "2026-10-02T00:00:00Z", "weekly_reset_at": None,
                               "hide_daily_quota": False})
        balance, daily, weekly = value["metrics"]
        self.assertEqual(value["primary_metric_id"], "on_demand_balance")
        self.assertEqual((balance["kind"], balance["scope"], balance["remaining"]), ("balance", "team", -2.5))
        self.assertIsNone(balance["used"])
        self.assertIsNone(balance["limit"])
        self.assertIsNone(balance["resets_at"])
        self.assertEqual(daily["remaining"], 100)
        self.assertEqual(weekly["remaining"], 0)
        self.assertEqual(weekly["used"], 120)

    def test_devin_hidden_daily_quota(self):
        value = parsers.devin({"overage_balance": 0, "daily_percentage": 30, "hide_daily_quota": True})
        self.assertEqual([m["id"] for m in value["metrics"]], ["on_demand_balance"])

    def test_empty_malformed_and_nonfinite_responses_fail(self):
        for parse in (parsers.claude, parsers.codex, parsers.cursor, parsers.devin):
            for data in ({}, [], {"irrelevant": 1}):
                with self.subTest(parse=parse.__name__, data=data), self.assertRaises(CollectionError):
                    parse(data)
        for amount in (math.nan, math.inf, "NaN", True, {}, -1):
            with self.subTest(amount=amount), self.assertRaises(CollectionError):
                parsers.codex({"spend_control": {"individual_limit": {"used": amount, "unit": "credit"}}})

    def test_periods_follow_documented_allowance_lengths(self):
        claude = parsers.claude({"spend": {"used": {"amount_minor": 1, "exponent": 2, "currency": "USD"}},
                                 "five_hour": {"utilization": 1}, "seven_day": {"utilization": 1}})
        self.assertEqual([m["period"] for m in claude["metrics"]], ["month", "five_hours", "week"])
        codex = parsers.codex({"spend_control": {"individual_limit": {"used": "1", "unit": "credit"}},
                               "rate_limit": {"primary_window": {"used_percent": 1},
                                              "secondary_window": {"used_percent": 1}}})
        self.assertEqual([m["period"] for m in codex["metrics"]], ["month", None, "week"])
        cursor = parsers.cursor({"individualUsage": {"plan": {"used": 1}, "onDemand": {"used": 1}}})
        self.assertEqual([m["period"] for m in cursor["metrics"]], ["month", "month"])
        devin = parsers.devin({"overage_balance": 0, "daily_percentage": 1, "weekly_percentage": 1})
        self.assertEqual([m["period"] for m in devin["metrics"]], [None, "day", "week"])

    def test_invalid_source_reset_is_not_silently_lost(self):
        with self.assertRaises(CollectionError):
            parsers.devin({"weekly_percentage": 30, "weekly_reset_at": "tomorrow"})


if __name__ == "__main__":
    unittest.main()
