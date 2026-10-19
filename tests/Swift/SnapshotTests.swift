import Foundation
import Darwin
#if canImport(XCTest)
import XCTest
@testable import BarsCore
#else
// Command Line Tools omit XCTest. Keep the same behavioral checks executable there.
class XCTestCase {}
func XCTAssertEqual<T: Equatable>(_ actual: T, _ expected: T, file: StaticString = #file, line: UInt = #line) {
    precondition(actual == expected, "Expected \(expected), got \(actual)", file: file, line: line)
}
func XCTAssertTrue(_ value: Bool, file: StaticString = #file, line: UInt = #line) {
    precondition(value, "Expected true", file: file, line: line)
}
func XCTAssertFalse(_ value: Bool, file: StaticString = #file, line: UInt = #line) {
    precondition(!value, "Expected false", file: file, line: line)
}
func XCTAssertNil<T>(_ value: T?, file: StaticString = #file, line: UInt = #line) {
    precondition(value == nil, "Expected nil", file: file, line: line)
}
func XCTUnwrap<T>(_ value: T?) throws -> T {
    guard let value else { throw SnapshotError.invalidMetrics }
    return value
}
func XCTAssertThrowsError<T>(_ value: @autoclosure () throws -> T, file: StaticString = #file, line: UInt = #line) {
    do {
        _ = try value()
        preconditionFailure("Expected an error", file: file, line: line)
    } catch {}
}

@main enum CoreChecks {
    static func main() throws {
        let tests = SnapshotTests()
        try tests.testDecodesFractionalTimestampsAndProviderAgesRemainIndependent()
        tests.testUTCDateParserValidatesCalendarAndPreservesFractions()
        try tests.testMalformedDatesNeverReplacePublishedSnapshot()
        try tests.testOverLimitTextPreservesAmountsButCapsFill()
        tests.testBalanceAndZeroOrUnknownLimitDoNotInventFill()
        tests.testPreviousPeriodDependsOnResetAndLastSuccessfulFetch()
        tests.testThresholdsUseUsageConsumed()
        try tests.testRejectsUnsupportedSchemaMissingProvidersAndDanglingPrimary()
        try tests.testFailedPublishPreservesPreviousSnapshot()
        tests.testSecondaryAllowancesShowValueResetAndAttentionTags()
        tests.testSubscriptionWindowsKeepTheirIdentityAndDoNotRepeatPrimary()
        tests.testResetCaptionsUseLocalTimeAndMarkOnlyEstimates()
        tests.testReadersNeverInventAResetAndEstimatesRetainStalePeriod()
        try tests.testManifestFieldsDecodeAndDegradeWhenAbsent()
        try tests.testPublisherEnforcesContractBoundsOnLinksMetricsAndText()
        try tests.testPublishedPathIsSeparateFromCollectorStaging()
        try tests.testOversizedOrSymlinkSourceDoesNotReplacePublishedData()
        try tests.testNonregularSourceIsRejectedWithoutWaitingForAWriter()
        tests.testPaceComparesUsageWithElapsedPeriodAtFetchTime()
        tests.testProjectionNeedsElapsedTimeAndNeverPrecedesTheFetch()
        tests.testEndedPeriodHasNoPaceEvenWhenFetchedAtOrAfterReset()
        tests.testPaceCaptionsAndAllowanceNotes()
        try tests.testUnknownPeriodDegradesToNoPaceWithoutFailingDecode()
        tests.testAmountDecimalsFollowUnit()
        tests.testAmountFormattingKeepsLocaleAndPrecisionUnderConcurrentCalls()
        try tests.testRowShowsOwnAgeOnlyWhenItDeviates()
        tests.testShortResetTextMatchesPeriod()
        tests.testContextCaptionExplainsMissingReset()
        tests.testDetailsHoistSharedResetAndGroupScopes()
        tests.testDetailsFreshnessAndFailedAttempt()
        try tests.testEnabledDefaultsToTrueAndHiddenProvidersStillValidate()
        tests.testCallToActionFollowsCollectorVerdictOnly()
        try tests.testInstallationSettingsReadModifyWritePreservesOtherKeys()
        try tests.testInstallationSettingsFileIsReplacedAtomicallyWithPrivateMode()
        try tests.testAtomicReplaceRemovesTemporaryFileOnFailureAndNeverFollowsALink()
        tests.testBarAccessibilityValueRoundsLikeVisibleText()
        tests.testDeepLinkRoundTripsAndRejectsOtherPaths()
        try tests.testMissingFileIsDistinguishedFromOtherReadFailures()
        tests.testCompactRowsCapSecondaryAllowances()
        try tests.testRateLimitPauseDecodesAndIsShownOnlyWhileItLasts()
        print("40 BarsCore checks passed")
    }
}
#endif

/// The standard plugins' ids. The app names no provider; the checks use these to build fixtures.
extension ProviderID {
    static let claude = ProviderID(rawValue: "claude")!
    static let codex = ProviderID(rawValue: "codex")!
    static let cursor = ProviderID(rawValue: "cursor")!
    static let devin = ProviderID(rawValue: "devin")!
    static let standard: [ProviderID] = [.claude, .codex, .cursor, .devin]
    var displayName: String { rawValue.prefix(1).uppercased() + rawValue.dropFirst() }
}

final class SnapshotTests: XCTestCase {
    private let now = SnapshotDate.parse("2026-10-01T12:00:00Z")!

    func testSubscriptionWindowsKeepTheirIdentityAndDoNotRepeatPrimary() {
        for (id, short, weekly) in [(ProviderID.claude, "five_hour", "seven_day"),
                                    (ProviderID.codex, "primary_window", "secondary_window")] {
            let metrics = [short, weekly, "seven_day_opus"].map { mid in
                UsageMetric(id: mid, label: mid, kind: .quota, scope: .individual, unit: "%",
                            used: 20, limit: 100, remaining: 80,
                            resetsAt: now.addingTimeInterval(86_400), period: mid == short ? nil : "week")
            }
            // The plugin names the secondary allowances; the reader resolves them in that order.
            var provider = ProviderSnapshot(id: id, name: id.displayName, status: .ok, message: nil,
                                            fetchedAt: now, lastAttemptAt: now, primaryMetricId: short,
                                            secondaryMetricIds: [weekly], metrics: metrics)
            XCTAssertEqual(provider.primaryMetric?.id, short)
            XCTAssertEqual(provider.primaryMetric?.fill, 0.2)
            XCTAssertNil(UsagePresentation.pace(for: metrics[0], provider: provider, at: now))
            XCTAssertEqual(UsagePresentation.secondaryMetrics(for: provider).map(\.id), [weekly])
            XCTAssertEqual(UsagePresentation.scopeGroups(for: provider).flatMap(\.metrics).count, 3)
            // A secondary id without a metric resolves to nothing rather than a placeholder.
            provider.metrics.remove(at: 1)
            XCTAssertTrue(UsagePresentation.secondaryMetrics(for: provider).isEmpty)
            provider.secondaryMetricIds = []
            XCTAssertTrue(UsagePresentation.secondaryMetrics(for: provider).isEmpty)
        }
    }

    private func fixture() -> [String: Any] {
        ["schema_version": 2, "generated_at": "2026-10-01T12:00:00.123Z",
         "providers": ProviderID.standard.map { id -> [String: Any] in
            ["id": id.rawValue, "name": id.displayName, "status": "ok", "message": NSNull(),
             "fetched_at": "2026-10-01T11:00:00Z", "last_attempt_at": "2026-10-01T12:00:00Z",
             "primary_metric_id": "main", "secondary_metric_ids": [], "usage_url": NSNull(), "setup": NSNull(),
             "metrics": [
                ["id": "main", "label": "Monthly budget", "kind": "budget", "scope": "individual",
                 "unit": "USD", "used": 120, "limit": 100, "remaining": 0, "resets_at": NSNull()]
             ]]
         }]
    }

    private func decode(_ fixture: [String: Any]) throws -> UsageSnapshot {
        try UsageSnapshot.decode(JSONSerialization.data(withJSONObject: fixture))
    }

    func testDecodesFractionalTimestampsAndProviderAgesRemainIndependent() throws {
        var value = fixture()
        var providers = value["providers"] as! [[String: Any]]
        providers[0]["status"] = "login_required"
        providers[0]["fetched_at"] = "2026-09-30T12:00:00Z"
        providers[1]["fetched_at"] = "2026-10-01T11:59:00Z"
        value["providers"] = providers
        let snapshot = try decode(value)
        XCTAssertEqual(snapshot.providers[0].status, .loginRequired)
        XCTAssertEqual(UsagePresentation.age(since: snapshot.providers[0].fetchedAt, now: now), "1d 0h ago")
        XCTAssertEqual(UsagePresentation.age(since: snapshot.providers[1].fetchedAt, now: now), "1m ago")
        XCTAssertTrue(snapshot.providers[0].isStale(at: now))
        XCTAssertFalse(snapshot.providers[1].isStale(at: now))
    }

    func testUTCDateParserValidatesCalendarAndPreservesFractions() {
        // Independent Unix epoch values, including the Gregorian century leap-year rule.
        for (text, seconds) in [("0001-01-01T00:00:00Z", -62_135_596_800.0),
                                ("1969-12-31T23:59:59Z", -1.0),
                                ("1970-01-01T00:00:00Z", 0.0),
                                ("2000-02-29T00:00:00Z", 951_782_400.0),
                                ("9999-12-31T23:59:59Z", 253_402_300_799.0)] {
            XCTAssertEqual(SnapshotDate.parse(text), Date(timeIntervalSince1970: seconds))
        }
        for fraction in ["1", "123", "123456", "123456789", "12345678901234567890"] {
            XCTAssertEqual(SnapshotDate.parse("1970-01-01T00:00:00.\(fraction)Z"),
                           Date(timeIntervalSince1970: Double("0.\(fraction)")!))
        }
        let precise = SnapshotDate.parse("2026-10-01T12:00:00.123456Z")!
        XCTAssertTrue(abs(precise.timeIntervalSince(now) - 0.123456) < 0.000001)
        XCTAssertTrue(SnapshotDate.parse("1600-02-29T00:00:00Z") != nil)
        XCTAssertTrue(SnapshotDate.parse("2028-02-29T23:59:59Z") != nil)
        for invalid in ["", "2026-02-30T12:00:00Z", "1900-02-29T12:00:00Z",
                        "1500-02-29T12:00:00Z", "2026-04-31T12:00:00Z", "0000-01-01T00:00:00Z",
                        "10000-01-01T00:00:00Z", "999999999-01-01T00:00:00Z", "2026-00-01T00:00:00Z",
                        "2026-13-01T00:00:00Z", "2026-01-00T00:00:00Z", "2026-01-01T24:00:00Z",
                        "2026-01-01T00:60:00Z", "2026-01-01T00:00:60Z", "2026-01-01T00:00:00.Z",
                        "2026-01-01T00:00:00.1junkZ", "2026-01-01T00:00:00ZjunkZ",
                        "2026-01-01T00:00:00+00:00", "2026-01-01 00:00:00Z", "2026-01-01T00:00:00z",
                        "2026-1x-01T00:00:00Z", "2026-01-01T00:00:00Z\n"] {
            XCTAssertNil(SnapshotDate.parse(invalid))
        }
    }

    func testMalformedDatesNeverReplacePublishedSnapshot() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let source = directory.appendingPathComponent("source.json")
        let destination = directory.appendingPathComponent("published.json")
        let good = try JSONSerialization.data(withJSONObject: fixture())
        try good.write(to: source)
        try SnapshotStore.publish(from: source, to: destination)
        for field in ["generated_at", "fetched_at", "last_attempt_at", "resets_at"] {
            for invalid in ["2026-02-30T12:00:00Z", "2026-10-01T12:00:00ZjunkZ"] {
                var value = fixture()
                if field == "generated_at" {
                    value[field] = invalid
                } else {
                    var providers = value["providers"] as! [[String: Any]]
                    if field == "resets_at" {
                        var metrics = providers[0]["metrics"] as! [[String: Any]]
                        metrics[0][field] = invalid
                        providers[0]["metrics"] = metrics
                    } else {
                        providers[0][field] = invalid
                    }
                    value["providers"] = providers
                }
                let data = try JSONSerialization.data(withJSONObject: value)
                XCTAssertThrowsError(try UsageSnapshot.decode(data))
                try data.write(to: source)
                XCTAssertThrowsError(try SnapshotStore.publish(from: source, to: destination))
                XCTAssertEqual(try Data(contentsOf: destination), good)
            }
        }
    }

    func testOverLimitTextPreservesAmountsButCapsFill() throws {
        let metric = try XCTUnwrap(decode(fixture()).providers[0].primaryMetric)
        XCTAssertEqual(metric.fill, 1)
        XCTAssertEqual(metric.overage, 20)
        let locale = Locale(identifier: "en_US_POSIX")
        XCTAssertEqual(UsagePresentation.amount(metric.used!, unit: metric.unit, locale: locale), "120.00 USD")
        XCTAssertEqual(UsagePresentation.amount(metric.remaining!, unit: metric.unit, locale: locale), "0.00 USD")
        XCTAssertEqual(UsagePresentation.allowanceValue(metric, locale: locale), "120.00 / 100.00 USD")
        XCTAssertEqual(UsagePresentation.percentUsed(metric), "100%")
    }

    func testBalanceAndZeroOrUnknownLimitDoNotInventFill() {
        var metric = UsageMetric(id: "balance", label: "On-demand", kind: .balance, scope: .team,
                                 unit: "USD", used: nil, limit: nil, remaining: -5, resetsAt: nil)
        XCTAssertNil(metric.fill)
        XCTAssertNil(UsagePresentation.percentUsed(metric))
        XCTAssertTrue(UsagePresentation.isBalanceOnly(metric))
        XCTAssertEqual(UsagePresentation.allowanceValue(metric, locale: Locale(identifier: "en_US_POSIX")), "-5.00 USD left")
        metric.used = 10
        metric.limit = 0
        XCTAssertNil(metric.fill)
        XCTAssertEqual(metric.overage, 10)
        metric.limit = nil
        XCTAssertNil(metric.fill)
        XCTAssertNil(metric.overage)
    }

    func testPreviousPeriodDependsOnResetAndLastSuccessfulFetch() {
        let reset = now.addingTimeInterval(-60)
        let metric = UsageMetric(id: "daily", label: "Daily", kind: .quota, scope: .unknown,
                                 unit: "%", used: 80, limit: 100, remaining: 20, resetsAt: reset)
        func provider(fetchedAt: Date) -> ProviderSnapshot {
            ProviderSnapshot(id: .devin, name: "Devin", status: .ok, message: nil, fetchedAt: fetchedAt,
                             lastAttemptAt: fetchedAt, primaryMetricId: nil, metrics: [metric])
        }
        let beforeReset = provider(fetchedAt: now.addingTimeInterval(-120))
        XCTAssertTrue(UsagePresentation.isPreviousPeriod(for: metric, provider: beforeReset, at: now))
        XCTAssertFalse(UsagePresentation.isPreviousPeriod(for: metric, provider: provider(fetchedAt: now), at: now))
        XCTAssertFalse(UsagePresentation.isPreviousPeriod(for: metric, provider: beforeReset, at: reset.addingTimeInterval(-1)))
    }

    func testThresholdsUseUsageConsumed() {
        var metric = UsageMetric(id: "main", label: "Usage", kind: .quota, scope: .individual,
                                 unit: "%", used: 69.9, limit: 100, remaining: 30.1, resetsAt: nil)
        XCTAssertEqual(UsagePresentation.severity(for: metric), .normal)
        metric.used = 70
        XCTAssertEqual(UsagePresentation.severity(for: metric), .warning)
        metric.used = 90
        XCTAssertEqual(UsagePresentation.severity(for: metric), .exhausted)
    }

    func testRejectsUnsupportedSchemaMissingProvidersAndDanglingPrimary() throws {
        var value = fixture()
        value["schema_version"] = 1
        // The file left by the previous version, in the moments after an update.
        do {
            _ = try decode(value)
            XCTAssertTrue(false)
        } catch {
            XCTAssertEqual(error as? SnapshotError, .outdatedVersion)
        }
        // A newer schema says so even when its shape no longer decodes as version 2.
        do {
            _ = try UsageSnapshot.decode(Data(#"{"schema_version": 3, "providers": {}}"#.utf8))
            XCTAssertTrue(false)
        } catch {
            XCTAssertEqual(error as? SnapshotError, .unsupportedVersion)
        }
        // Any set of plugins is valid, including none, but ids must be unique, ascending and well formed.
        value = fixture()
        value["providers"] = []
        XCTAssertEqual(try decode(value).providers.count, 0)
        value = fixture()
        var providers = value["providers"] as! [[String: Any]]
        providers.swapAt(0, 1)
        value["providers"] = providers
        XCTAssertThrowsError(try decode(value))
        providers = fixture()["providers"] as! [[String: Any]]
        providers[1]["id"] = "claude"
        value["providers"] = providers
        XCTAssertThrowsError(try decode(value))
        for bad in ["Claude", "my-plugin", "../x", "", String(repeating: "a", count: 33)] {
            providers = fixture()["providers"] as! [[String: Any]]
            providers[3]["id"] = bad
            value["providers"] = providers
            XCTAssertThrowsError(try decode(value))
        }
        providers = fixture()["providers"] as! [[String: Any]]
        providers[3]["id"] = "zeta_2"
        value["providers"] = providers
        XCTAssertEqual(try decode(value).providers.last?.id.rawValue, "zeta_2")
        // Dangling primary or secondary ids, or a primary repeated as secondary, are rejected.
        providers = fixture()["providers"] as! [[String: Any]]
        providers[0]["primary_metric_id"] = "missing"
        value["providers"] = providers
        XCTAssertThrowsError(try decode(value))
        providers = fixture()["providers"] as! [[String: Any]]
        providers[0]["secondary_metric_ids"] = ["missing"]
        value["providers"] = providers
        XCTAssertThrowsError(try decode(value))
        providers = fixture()["providers"] as! [[String: Any]]
        providers[0]["secondary_metric_ids"] = ["main"]
        value["providers"] = providers
        XCTAssertThrowsError(try decode(value))
    }

    func testPublisherEnforcesContractBoundsOnLinksMetricsAndText() throws {
        func mutated(_ change: (inout [String: Any]) -> Void) -> [String: Any] {
            var value = fixture()
            var providers = value["providers"] as! [[String: Any]]
            change(&providers[0])
            value["providers"] = providers
            return value
        }
        // Links must be absolute https with a host, or the file is refused.
        for link in ["file:///tmp/example", "bars://provider/example", "relative/path", "https://", "http://example.com",
                     "https://user:pw@example.com/"] {
            XCTAssertThrowsError(try decode(mutated { $0["usage_url"] = link }))
            XCTAssertThrowsError(try decode(mutated {
                $0["setup"] = ["install_title": "Install", "install_url": link, "login_command": "x login",
                               "login_step": "Sign in:", "sign_in_again": "Again:", "credential_note": "Note."]
            }))
        }
        XCTAssertEqual(try decode(mutated { $0["usage_url"] = "https://example.com/usage" }).providers[0].usageUrl?.host, "example.com")
        // Metric count, id grammar and printable bounded text.
        let metric = (fixture()["providers"] as! [[String: Any]])[0]["metrics"] as! [[String: Any]]
        XCTAssertThrowsError(try decode(mutated { provider in
            provider["metrics"] = (0..<(UsageSnapshot.maximumMetrics + 1)).map { index -> [String: Any] in
                var copy = metric[0]; copy["id"] = "m\(index)"; return copy
            }
            provider["primary_metric_id"] = "m0"
        }))
        XCTAssertEqual(try decode(mutated { provider in
            provider["metrics"] = (0..<UsageSnapshot.maximumMetrics).map { index -> [String: Any] in
                var copy = metric[0]; copy["id"] = "m\(index)"; return copy
            }
            provider["primary_metric_id"] = "m0"
        }).providers[0].metrics.count, UsageSnapshot.maximumMetrics)
        for badID in ["../bad", "Main", "", String(repeating: "a", count: 65)] {
            XCTAssertThrowsError(try decode(mutated { provider in
                var copy = metric[0]; copy["id"] = badID
                provider["metrics"] = [copy]; provider["primary_metric_id"] = badID
            }))
        }
        for badText in ["", "line\nbreak", "tab\tbed", String(repeating: "x", count: 201)] {
            XCTAssertThrowsError(try decode(mutated { provider in
                var copy = metric[0]; copy["note"] = badText; provider["metrics"] = [copy]
            }))
            XCTAssertThrowsError(try decode(mutated { provider in
                var copy = metric[0]; copy["label"] = badText; provider["metrics"] = [copy]
            }))
            XCTAssertThrowsError(try decode(mutated { $0["message"] = badText }))
        }
        XCTAssertThrowsError(try decode(mutated { $0["name"] = String(repeating: "n", count: 33) }))
        // An unreadable file keeps its reason apart from an empty plugin list.
        let unavailable = UsageSnapshot.unavailable(message: "Open Bars to collect usage.")
        XCTAssertEqual(unavailable.unavailableMessage, "Open Bars to collect usage.")
        XCTAssertNil(try decode(mutated { _ in }).unavailableMessage)
    }

    func testManifestFieldsDecodeAndDegradeWhenAbsent() throws {
        var value = fixture()
        var providers = value["providers"] as! [[String: Any]]
        providers[0]["usage_url"] = "https://example.com/usage"
        providers[0]["setup"] = ["install_title": "Install Example", "install_url": "https://example.com/install",
                                 "login_command": "example login", "login_step": "Sign in:",
                                 "sign_in_again": "Sign in again:", "credential_note": "Bars reads the Example login."]
        var metrics = providers[0]["metrics"] as! [[String: Any]]
        metrics[0]["resets_at"] = "2026-11-01T00:00:00Z"
        metrics[0]["resets_at_estimated"] = true
        metrics[0]["note"] = "Example reports only the remaining amount."
        providers[0]["metrics"] = metrics
        // A provider written by an older version 2 collector omits the newer keys entirely.
        for key in ["secondary_metric_ids", "usage_url", "setup"] { providers[1].removeValue(forKey: key) }
        value["providers"] = providers
        let snapshot = try decode(value)
        XCTAssertEqual(snapshot.providers[0].usageUrl, URL(string: "https://example.com/usage"))
        XCTAssertEqual(snapshot.providers[0].setup?.loginCommand, "example login")
        XCTAssertEqual(snapshot.providers[0].setup?.installUrl, URL(string: "https://example.com/install"))
        XCTAssertTrue(snapshot.providers[0].metrics[0].resetsAtEstimated)
        XCTAssertEqual(snapshot.providers[0].metrics[0].note, "Example reports only the remaining amount.")
        XCTAssertNil(snapshot.providers[1].usageUrl)
        XCTAssertNil(snapshot.providers[1].setup)
        XCTAssertEqual(snapshot.providers[1].secondaryMetricIds, [])
        XCTAssertFalse(snapshot.providers[1].metrics[0].resetsAtEstimated)
        // The "(est.)" marker follows the flag, not the absence of a reset.
        let locale = Locale(identifier: "en_US@calendar=gregorian")
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        let estimated = snapshot.providers[0]
        XCTAssertEqual(UsagePresentation.fullResetText(for: estimated.metrics[0], provider: estimated,
                                                       locale: locale, timeZone: zone)?
                           .replacingOccurrences(of: "\u{202f}", with: " "),
                       "Oct 31 at 5:00 PM PDT (est.)")
        XCTAssertEqual(UsagePresentation.balanceNote(for: estimated.metrics[0], provider: estimated),
                       "Example reports only the remaining amount.")
        XCTAssertEqual(UsagePresentation.balanceNote(for: snapshot.providers[1].metrics[0], provider: snapshot.providers[1]),
                       "Codex reports only the remaining amount.")
    }

    func testFailedPublishPreservesPreviousSnapshot() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let source = directory.appendingPathComponent("source.json")
        let destination = directory.appendingPathComponent("shared/snapshot.json")
        let good = try JSONSerialization.data(withJSONObject: fixture())
        try good.write(to: source)
        try SnapshotStore.publish(from: source, to: destination)
        XCTAssertEqual(try Data(contentsOf: destination), good)
        try Data("{}".utf8).write(to: source)
        XCTAssertThrowsError(try SnapshotStore.publish(from: source, to: destination))
        XCTAssertEqual(try Data(contentsOf: destination), good)
        let permissions = try FileManager.default.attributesOfItem(atPath: destination.path)[.posixPermissions] as? Int
        XCTAssertEqual(permissions, 0o600)
    }

    func testSecondaryAllowancesShowValueResetAndAttentionTags() {
        let locale = Locale(identifier: "en_US")
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        let fetched = SnapshotDate.parse("2026-10-02T19:00:06Z")!
        let cursorReset = SnapshotDate.parse("2026-10-30T16:43:25Z")
        func cursorMetric(_ id: String, _ label: String, _ scope: AllowanceScope, used: Double, limit: Double) -> UsageMetric {
            UsageMetric(id: id, label: label, kind: .budget, scope: scope, unit: "USD", used: used, limit: limit,
                        remaining: max(limit - used, 0), resetsAt: cursorReset, period: "month")
        }
        let cursor = ProviderSnapshot(id: .cursor, name: "Cursor", status: .ok, message: nil, fetchedAt: fetched,
                                      lastAttemptAt: fetched, primaryMetricId: "included",
                                      secondaryMetricIds: ["individual_on_demand", "team_on_demand"], metrics: [
            cursorMetric("included", "Included usage", .individual, used: 100, limit: 100),
            cursorMetric("individual_on_demand", "Individual on-demand", .individual, used: 12.5, limit: 400),
            cursorMetric("team_on_demand", "Team on-demand", .team, used: 75, limit: 3_000),
        ])
        let rows = UsagePresentation.secondaryAllowances(for: cursor, now: fetched, locale: locale, timeZone: zone)
        XCTAssertEqual(rows.map(\.value), ["12.50 / 400.00 USD", "75.00 / 3,000.00 USD"])
        // The shared reset is already on the primary context line, so sub-rows do not repeat it.
        XCTAssertEqual(rows.map(\.caption), ["Individual on-demand", "Team on-demand"])
        XCTAssertNil(rows[0].note)
        XCTAssertEqual(rows[0].pace?.standing, .onPace)

        let devin = devinProvider(fetched: fetched, dailyUsed: 80)
        let quotas = UsagePresentation.secondaryAllowances(for: devin, now: fetched, locale: locale, timeZone: zone)
            .map { ($0.caption.replacingOccurrences(of: "\u{202f}", with: " "), $0.value, $0.note) }
        XCTAssertEqual(quotas.map(\.0), ["Daily quota · resets 1:00 AM · ahead of pace",
                                         "Weekly quota · resets Sun 1:00 AM · used up early"])
        XCTAssertEqual(quotas.map(\.1), ["80%", "100%"])
        XCTAssertEqual(quotas.map(\.2), [.aheadOfPace, .usedUpEarly])

        // Retained values from an ended period say so instead of a pace tag.
        let later = SnapshotDate.parse("2026-10-03T09:00:00Z")!
        let stale = UsagePresentation.secondaryAllowances(for: devin, now: later, locale: locale, timeZone: zone)
        XCTAssertEqual(stale[0].note, .previousPeriod)
        XCTAssertEqual(stale[0].caption.replacingOccurrences(of: "\u{202f}", with: " "),
                       "Daily quota · reset 1:00 AM · previous period")
        XCTAssertEqual(stale[1].note, .usedUpEarly)
    }

    private func devinProvider(fetched: Date, dailyUsed: Double = 0) -> ProviderSnapshot {
        ProviderSnapshot(id: .devin, name: "Devin", status: .ok, message: nil, fetchedAt: fetched,
                         lastAttemptAt: fetched, primaryMetricId: "on_demand_balance",
                         secondaryMetricIds: ["daily", "weekly"], metrics: [
            UsageMetric(id: "on_demand_balance", label: "On-demand balance", kind: .balance, scope: .team, unit: "USD",
                        used: nil, limit: nil, remaining: 2_500, resetsAt: nil, period: nil,
                        note: "Devin reports only the remaining amount; purchased credits do not expire, so there is no reset."),
            UsageMetric(id: "daily", label: "Daily quota", kind: .quota, scope: .unknown, unit: "%", used: dailyUsed,
                        limit: 100, remaining: 100 - dailyUsed, resetsAt: SnapshotDate.parse("2026-10-03T08:00:00Z"),
                        period: "day"),
            UsageMetric(id: "weekly", label: "Weekly quota", kind: .quota, scope: .unknown, unit: "%", used: 100, limit: 100,
                        remaining: 0, resetsAt: SnapshotDate.parse("2026-10-04T08:00:00Z"), period: "week"),
        ])
    }

    /// A monthly budget. The Claude plugin estimates a missing reset as the end of the UTC month
    /// and marks it; the reader shows whatever reset the snapshot carries.
    private func monthlyProvider(_ id: ProviderID, fetched: String, reset: String? = nil,
                                 estimated: Bool = false) -> ProviderSnapshot {
        let date = SnapshotDate.parse(fetched)!
        let metric = UsageMetric(id: "monthly_spend", label: "Monthly budget", kind: .budget,
                                 scope: .unknown, unit: "USD", used: 10, limit: 100, remaining: 90,
                                 resetsAt: reset.flatMap(SnapshotDate.parse), period: "month", resetsAtEstimated: estimated)
        return ProviderSnapshot(id: id, name: id.displayName, status: .ok, message: nil,
                                fetchedAt: date, lastAttemptAt: date, primaryMetricId: metric.id, metrics: [metric])
    }

    func testResetCaptionsUseLocalTimeAndMarkOnlyEstimates() {
        // Pin the calendar too: a developer Mac set to another calendar would otherwise format
        // these dates in it, which is correct behavior for the app but not what this test checks.
        let locale = Locale(identifier: "en_US@calendar=gregorian")
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        for id in ProviderID.standard {
            let provider = monthlyProvider(id, fetched: "2026-10-01T12:00:00Z",
                                           reset: "2026-11-01T00:00:00Z", estimated: id == .claude)
            let caption = UsagePresentation.fullResetText(for: provider.metrics[0], provider: provider,
                                                          locale: locale, timeZone: zone)!
                .replacingOccurrences(of: "\u{202f}", with: " ")
            XCTAssertEqual(caption, "Oct 31 at 5:00 PM PDT" + (id == .claude ? " (est.)" : ""))
        }
        // The UTC month has rolled over while the local date is still October 31.
        let november = monthlyProvider(.claude, fetched: "2026-11-01T00:30:00Z", reset: "2026-12-01T00:00:00Z", estimated: true)
        let caption = UsagePresentation.fullResetText(for: november.metrics[0], provider: november,
                                                      locale: locale, timeZone: zone)!
            .replacingOccurrences(of: "\u{202f}", with: " ")
        XCTAssertEqual(caption, "Nov 30 at 4:00 PM PST (est.)")
    }

    func testReadersNeverInventAResetAndEstimatesRetainStalePeriod() {
        // An estimated reset behaves like a reported one for period membership and pace, and
        // without any reset the reader invents none.
        for reset in ["2027-01-01T00:00:00Z", "2028-03-01T00:00:00Z"] {
            let provider = monthlyProvider(.claude, fetched: "2026-12-31T23:59:00Z", reset: reset, estimated: true)
            let date = SnapshotDate.parse(reset)!
            XCTAssertEqual(UsagePresentation.resetDate(for: provider.metrics[0], provider: provider), date)
            XCTAssertFalse(UsagePresentation.isPreviousPeriod(for: provider.metrics[0], provider: provider,
                                                              at: date.addingTimeInterval(-1)))
            XCTAssertTrue(UsagePresentation.isPreviousPeriod(for: provider.metrics[0], provider: provider, at: date))
        }
        let none = monthlyProvider(.claude, fetched: "2026-10-01T12:00:00Z")
        XCTAssertNil(UsagePresentation.resetDate(for: none.metrics[0], provider: none))
        XCTAssertNil(UsagePresentation.fullResetText(for: none.metrics[0], provider: none))
        XCTAssertFalse(UsagePresentation.isPreviousPeriod(for: none.metrics[0], provider: none, at: now.addingTimeInterval(86_400 * 60)))
        let reported = monthlyProvider(.claude, fetched: "2026-10-01T12:00:00Z", reset: "2026-10-15T08:00:00Z")
        XCTAssertEqual(UsagePresentation.resetDate(for: reported.metrics[0], provider: reported),
                       SnapshotDate.parse("2026-10-15T08:00:00Z"))
    }

    private func pacedProvider(_ id: ProviderID, metricId: String = "monthly_credits", used: Double, limit: Double = 100,
                               period: String?, fetched: String, reset: String?) -> ProviderSnapshot {
        let date = SnapshotDate.parse(fetched)!
        let metric = UsageMetric(id: metricId, label: "Allowance", kind: .budget, scope: .individual, unit: "USD",
                                 used: used, limit: limit, remaining: max(limit - used, 0),
                                 resetsAt: reset.flatMap(SnapshotDate.parse), period: period)
        return ProviderSnapshot(id: id, name: id.displayName, status: .ok, message: nil,
                                fetchedAt: date, lastAttemptAt: date, primaryMetricId: metric.id, metrics: [metric])
    }

    func testPaceComparesUsageWithElapsedPeriodAtFetchTime() {
        // Oct 16 00:00Z is exactly half of the UTC month that resets Nov 1.
        let codex = pacedProvider(.codex, used: 25, period: "month",
                                  fetched: "2026-10-16T12:00:00Z", reset: "2026-11-01T00:00:00Z")
        let pace = UsagePresentation.pace(for: codex.metrics[0], provider: codex, at: now.addingTimeInterval(86_400 * 20))!
        XCTAssertEqual(pace.periodStart, SnapshotDate.parse("2026-10-01T00:00:00Z")!)
        XCTAssertEqual(pace.usedShare, 0.25)
        XCTAssertEqual(pace.elapsedShare, 0.5)
        XCTAssertEqual(pace.standing, .onPace)
        // Projection: 25% in 15.5 days exhausts after 62 days, far past the reset.
        XCTAssertEqual(pace.projectedExhaustion, SnapshotDate.parse("2026-12-02T00:00:00Z")!)

        // Elapsed time is measured at the successful fetch, not at display time.
        let later = UsagePresentation.pace(for: codex.metrics[0], provider: codex, at: now.addingTimeInterval(86_400 * 30))!
        XCTAssertEqual(later.elapsedShare, 0.5)

        // Claude's estimated month boundary, written by its plugin, supplies both the reset and the period start.
        let claude = pacedProvider(.claude, metricId: "monthly_spend", used: 90, limit: 2_000, period: "month",
                                   fetched: "2026-10-01T20:30:00Z", reset: "2026-11-01T00:00:00Z")
        let early = UsagePresentation.pace(for: claude.metrics[0], provider: claude, at: now)!
        XCTAssertEqual(early.standing, .ahead)
        XCTAssertEqual(early.periodStart, SnapshotDate.parse("2026-10-01T00:00:00Z")!)
        XCTAssertEqual(early.periodEnd, SnapshotDate.parse("2026-11-01T00:00:00Z")!)
        XCTAssertTrue(early.projectedExhaustion! < early.periodEnd)

        // Devin's weekly quota: 100% used three days before the reset.
        let devin = pacedProvider(.devin, metricId: "weekly", used: 100, period: "week",
                                  fetched: "2026-10-01T20:30:00Z", reset: "2026-10-04T08:00:00Z")
        let usedUp = UsagePresentation.pace(for: devin.metrics[0], provider: devin, at: now)!
        XCTAssertEqual(usedUp.standing, .usedUp)
        XCTAssertEqual(usedUp.periodStart, SnapshotDate.parse("2026-09-27T08:00:00Z")!)

        // No pace without a period, a reset, usage, or a positive limit; none for an ended period.
        let noPeriod = pacedProvider(.codex, used: 25, period: nil, fetched: "2026-10-16T00:00:00Z", reset: "2026-11-01T00:00:00Z")
        XCTAssertNil(UsagePresentation.pace(for: noPeriod.metrics[0], provider: noPeriod, at: now))
        let noReset = pacedProvider(.codex, used: 25, period: "month", fetched: "2026-10-16T00:00:00Z", reset: nil)
        XCTAssertNil(UsagePresentation.pace(for: noReset.metrics[0], provider: noReset, at: now))
        let zeroLimit = pacedProvider(.codex, used: 0, limit: 0, period: "month", fetched: "2026-10-16T00:00:00Z", reset: "2026-11-01T00:00:00Z")
        XCTAssertNil(UsagePresentation.pace(for: zeroLimit.metrics[0], provider: zeroLimit, at: now))
        XCTAssertNil(UsagePresentation.pace(for: codex.metrics[0], provider: codex, at: SnapshotDate.parse("2026-11-01T00:00:00Z")!))
        let noUsage = pacedProvider(.codex, used: 0, period: "month", fetched: "2026-10-16T00:00:00Z", reset: "2026-11-01T00:00:00Z")
        let idle = UsagePresentation.pace(for: noUsage.metrics[0], provider: noUsage, at: now)!
        XCTAssertNil(idle.projectedExhaustion)
        XCTAssertEqual(idle.standing, .onPace)
    }

    func testProjectionNeedsElapsedTimeAndNeverPrecedesTheFetch() {
        let locale = Locale(identifier: "en_US")
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        // Fetched before the derived period start (a billing cycle end minus a calendar month can
        // land after the fetch): usage with no elapsed time is ahead of pace but has no rate.
        let early = pacedProvider(.cursor, metricId: "included", used: 5, limit: 100, period: "month",
                                  fetched: "2026-09-30T10:00:00Z", reset: "2026-10-30T16:43:25Z")
        let pace = UsagePresentation.pace(for: early.metrics[0], provider: early, at: SnapshotDate.parse("2026-09-30T11:00:00Z")!)!
        XCTAssertEqual(pace.elapsedShare, 0)
        XCTAssertEqual(pace.standing, .ahead)
        XCTAssertNil(pace.projectedExhaustion)
        XCTAssertEqual(UsagePresentation.paceCaption(for: pace, period: .month, locale: locale, timeZone: zone), "Ahead of pace")
        // A projection is never earlier than the fetch that produced it.
        let fetchedAt = SnapshotDate.parse("2026-10-16T12:00:00Z")!
        let heavy = pacedProvider(.codex, used: 99.999, limit: 100, period: "month",
                                  fetched: "2026-10-16T12:00:00Z", reset: "2026-11-01T00:00:00Z")
        let late = UsagePresentation.pace(for: heavy.metrics[0], provider: heavy, at: fetchedAt)!
        XCTAssertTrue(late.projectedExhaustion! >= fetchedAt)
        // Exhausted or over-limit usage has no projection; it is already used up.
        for used in [100.0, 150.0] {
            let done = pacedProvider(.codex, used: used, limit: 100, period: "month",
                                     fetched: "2026-10-16T12:00:00Z", reset: "2026-11-01T00:00:00Z")
            let usedUp = UsagePresentation.pace(for: done.metrics[0], provider: done, at: fetchedAt)!
            XCTAssertEqual(usedUp.standing, .usedUp)
            XCTAssertNil(usedUp.projectedExhaustion)
        }
    }

    func testEndedPeriodHasNoPaceEvenWhenFetchedAtOrAfterReset() {
        let reset = SnapshotDate.parse("2026-10-03T08:00:00Z")!
        for fetched in [reset.addingTimeInterval(-1), reset, reset.addingTimeInterval(1)] {
            var provider = pacedProvider(.devin, used: 20, period: "day",
                                         fetched: "2026-10-03T07:00:00Z", reset: "2026-10-03T08:00:00Z")
            provider.fetchedAt = fetched
            XCTAssertTrue(UsagePresentation.pace(for: provider.metrics[0], provider: provider,
                                                  at: reset.addingTimeInterval(-1)) != nil)
            XCTAssertNil(UsagePresentation.pace(for: provider.metrics[0], provider: provider, at: reset))
            XCTAssertNil(UsagePresentation.pace(for: provider.metrics[0], provider: provider,
                                                 at: reset.addingTimeInterval(1)))
        }
    }

    func testPaceCaptionsAndAllowanceNotes() {
        let locale = Locale(identifier: "en_US")
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        // 50% used after 25% of a month, fetched Oct 8 18:00Z, projected exhaustion Oct 16 12:00Z.
        let ahead = pacedProvider(.codex, used: 5, limit: 10, period: "month", fetched: "2026-10-08T18:00:00Z", reset: "2026-11-01T00:00:00Z")
        var pace = UsagePresentation.pace(for: ahead.metrics[0], provider: ahead, at: now)!
        XCTAssertEqual(pace.projectedExhaustion, SnapshotDate.parse("2026-10-16T12:00:00Z")!)
        XCTAssertEqual(UsagePresentation.paceCaption(for: pace, period: .month, locale: locale, timeZone: zone),
                       "Ahead of pace · runs out ~Oct 16 at this rate")
        // Details add the elapsed share, which the on-pace caption already states.
        XCTAssertEqual(UsagePresentation.detailsPaceCaption(for: pace, period: .month, locale: locale, timeZone: zone),
                       "Ahead of pace · runs out ~Oct 16 at this rate · 25% of period elapsed")
        XCTAssertEqual(UsagePresentation.allowanceNote(for: ahead.metrics[0], provider: ahead, at: now), .aheadOfPace)

        let onPace = pacedProvider(.codex, used: 10, period: "month", fetched: "2026-10-08T18:00:00Z", reset: "2026-11-01T00:00:00Z")
        pace = UsagePresentation.pace(for: onPace.metrics[0], provider: onPace, at: now)!
        XCTAssertEqual(UsagePresentation.paceCaption(for: pace, period: .month, locale: locale, timeZone: zone),
                       "On pace · 25% of period elapsed")
        XCTAssertEqual(UsagePresentation.detailsPaceCaption(for: pace, period: .month, locale: locale, timeZone: zone),
                       "On pace · 25% of period elapsed")
        XCTAssertNil(UsagePresentation.allowanceNote(for: onPace.metrics[0], provider: onPace, at: now))

        // Sub-day periods project to a local time of day: 60% used 1h into a 5h window.
        let session = pacedProvider(.claude, metricId: "five_hour", used: 60, period: "five_hours",
                                    fetched: "2026-10-01T13:00:00Z", reset: "2026-10-01T17:00:00Z")
        pace = UsagePresentation.pace(for: session.metrics[0], provider: session, at: now.addingTimeInterval(3_600))!
        XCTAssertEqual(UsagePresentation.paceCaption(for: pace, period: .fiveHours, locale: locale, timeZone: zone)
                           .replacingOccurrences(of: "\u{202f}", with: " "),
                       "Ahead of pace · runs out ~6:40 AM at this rate")

        // Devin quota notes tag quotas that need attention and leave on-pace quotas plain.
        let fetched = SnapshotDate.parse("2026-10-01T20:30:00Z")!
        let devin = devinProvider(fetched: fetched)
        XCTAssertEqual(UsagePresentation.secondaryAllowances(for: devin, now: fetched, locale: locale, timeZone: zone)
                           .map(\.note), [nil, .usedUpEarly])
        pace = UsagePresentation.pace(for: devin.metrics[2], provider: devin, at: fetched)!
        // 2d 11.5h of the week remain: 35% rounded.
        XCTAssertEqual(UsagePresentation.paceCaption(for: pace, period: .week, locale: locale, timeZone: zone),
                       "Used up · 35% of period left")
    }

    func testUnknownPeriodDegradesToNoPaceWithoutFailingDecode() throws {
        var value = fixture()
        var providers = value["providers"] as! [[String: Any]]
        var metrics = providers[1]["metrics"] as! [[String: Any]]
        metrics[0]["period"] = "fortnight"
        metrics[0]["resets_at"] = "2026-11-01T00:00:00Z"
        providers[1]["metrics"] = metrics
        value["providers"] = providers
        let snapshot = try decode(value)
        XCTAssertEqual(snapshot.providers[1].metrics[0].period, "fortnight")
        XCTAssertNil(snapshot.providers[1].metrics[0].allowancePeriod)
        XCTAssertNil(UsagePresentation.pace(for: snapshot.providers[1].metrics[0], provider: snapshot.providers[1], at: now))
        // Snapshots written before the field existed still decode.
        XCTAssertNil(snapshot.providers[0].metrics[0].period)
    }

    func testAmountDecimalsFollowUnit() {
        let locale = Locale(identifier: "en_US")
        // Currency codes always carry two decimals and keep the code rather than a symbol.
        XCTAssertEqual(UsagePresentation.amount(160.6, unit: "USD", locale: locale), "160.60 USD")
        XCTAssertEqual(UsagePresentation.amount(1_839.4, unit: "USD", locale: locale), "1,839.40 USD")
        XCTAssertEqual(UsagePresentation.amount(30, unit: "EUR", locale: locale), "30.00 EUR")
        // Credits and other non-currency units are whole numbers.
        XCTAssertEqual(UsagePresentation.amount(32_816.75, unit: "credits", locale: locale), "32,817 credits")
        XCTAssertEqual(UsagePresentation.amount(3.4, unit: "requests", locale: locale), "3 requests")
        // Percentages keep at most one decimal and drop trailing zeros.
        XCTAssertEqual(UsagePresentation.amount(9.46, unit: "%", locale: locale), "9.5%")
        XCTAssertEqual(UsagePresentation.amount(100, unit: "%", locale: locale), "100%")
        XCTAssertFalse(UsagePresentation.isCurrencyCode("usd"))
        XCTAssertFalse(UsagePresentation.isCurrencyCode("credits"))
        let claude = UsageMetric(id: "monthly_spend", label: "Monthly budget", kind: .budget, scope: .unknown,
                                 unit: "USD", used: 160.6, limit: 2_000, remaining: 1_839.4, resetsAt: nil)
        XCTAssertEqual(UsagePresentation.percentUsed(claude), "8%")
        XCTAssertEqual(UsagePresentation.allowanceValue(claude, locale: locale), "160.60 / 2,000.00 USD")
    }

    func testAmountFormattingKeepsLocaleAndPrecisionUnderConcurrentCalls() {
        let cases: [(String, String, Double, String)] = [
            ("en_US", "USD", -1234.5678, "-1,234.57 USD"),
            ("de_DE", "USD", 1234.5678, "1.234,57 USD"),
            ("en_US", "%", 9.46, "9.5%"),
            ("de_DE", "%", 9.46, "9,5%"),
            ("fr_FR", "credits", 1234.5678, "1\u{202f}235 credits"),
            ("en_US_POSIX", "USD", 0.125, "0.12 USD"),
            ("en_US", "credits", 1234.5678, "1,235 credits"),
            ("en_US", "USD", -0.0, "-0.00 USD")
        ]
        // Alternate locales and precision styles to exercise formatter replacement and reuse.
        for _ in 0..<3 {
            for (locale, unit, value, expected) in cases {
                XCTAssertEqual(UsagePresentation.amount(value, unit: unit, locale: Locale(identifier: locale)), expected)
            }
        }
        DispatchQueue.concurrentPerform(iterations: 512) { iteration in
            let (locale, unit, value, expected) = cases[iteration % cases.count]
            XCTAssertEqual(UsagePresentation.amount(value, unit: unit, locale: Locale(identifier: locale)), expected)
            XCTAssertEqual(SnapshotDate.parse("2000-02-29T00:00:00.123456Z"),
                           Date(timeIntervalSince1970: 951_782_400.123456))
        }
    }

    func testRowShowsOwnAgeOnlyWhenItDeviates() throws {
        var value = fixture()
        var providers = value["providers"] as! [[String: Any]]
        providers[0]["fetched_at"] = "2026-10-01T11:00:59Z"   // 59 s after Codex: same run
        providers[1]["fetched_at"] = "2026-10-01T11:00:00Z"
        providers[2]["fetched_at"] = "2026-10-01T11:02:00Z"   // the latest fetch
        providers[3]["fetched_at"] = "2026-10-01T11:00:59Z"   // 61 s before the latest
        value["providers"] = providers
        var snapshot = try decode(value)
        let latest = try XCTUnwrap(UsagePresentation.latestFetch(in: snapshot))
        XCTAssertEqual(latest, SnapshotDate.parse("2026-10-01T11:02:00Z")!)
        // 61 s from the latest deviates; within 60 s does not.
        XCTAssertTrue(UsagePresentation.showsOwnAge(snapshot.providers[3], latestFetch: latest))
        XCTAssertFalse(UsagePresentation.showsOwnAge(snapshot.providers[2], latestFetch: latest))
        snapshot.providers[1].fetchedAt = latest.addingTimeInterval(-59)
        XCTAssertFalse(UsagePresentation.showsOwnAge(snapshot.providers[1], latestFetch: latest))
        snapshot.providers[1].fetchedAt = latest.addingTimeInterval(-61)
        XCTAssertTrue(UsagePresentation.showsOwnAge(snapshot.providers[1], latestFetch: latest))
        // A failed latest attempt shows its status and age even with current data.
        snapshot.providers[2].status = .error
        XCTAssertTrue(UsagePresentation.showsOwnAge(snapshot.providers[2], latestFetch: latest))
        // A provider that never succeeded always shows its own state.
        let never = ProviderSnapshot.unavailable(.codex, message: "Sign in")
        XCTAssertTrue(UsagePresentation.showsOwnAge(never, latestFetch: latest))
        XCTAssertNil(UsagePresentation.latestFetch(in: .unavailable(message: "None")))

        // Header freshness text and the widget's switch to relative time.
        XCTAssertEqual(UsagePresentation.updatedText(since: latest, now: latest.addingTimeInterval(14 * 60)), "Updated 14m ago")
        XCTAssertEqual(UsagePresentation.updatedText(since: latest, now: latest.addingTimeInterval(30)), "Updated just now")
        XCTAssertEqual(UsagePresentation.updatedText(since: nil, now: now), "Never updated")
        XCTAssertFalse(UsagePresentation.isStale(latestFetch: latest, now: latest.addingTimeInterval(29 * 60)))
        XCTAssertTrue(UsagePresentation.isStale(latestFetch: latest, now: latest.addingTimeInterval(30 * 60)))
        XCTAssertTrue(UsagePresentation.isStale(latestFetch: nil, now: now))
    }

    func testShortResetTextMatchesPeriod() {
        let locale = Locale(identifier: "en_US")
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        let fetched = SnapshotDate.parse("2026-10-02T19:00:00Z")!
        let devin = devinProvider(fetched: fetched)
        func short(_ metric: UsageMetric, _ provider: ProviderSnapshot, capitalized: Bool = false, at date: Date? = nil) -> String? {
            UsagePresentation.shortResetCaption(for: metric, provider: provider, now: date ?? fetched,
                                                capitalized: capitalized, locale: locale, timeZone: zone)?
                .replacingOccurrences(of: "\u{202f}", with: " ")
        }
        // A day shows only the time; a week adds the weekday; a month shows the date.
        XCTAssertEqual(short(devin.metrics[1], devin), "resets 1:00 AM")
        XCTAssertEqual(short(devin.metrics[2], devin), "resets Sun 1:00 AM")
        let codex = pacedProvider(.codex, used: 10, period: "month", fetched: "2026-10-02T19:00:00Z",
                                  reset: "2026-11-01T00:00:00Z")
        XCTAssertEqual(short(codex.metrics[0], codex, capitalized: true), "Resets Oct 31")
        // Estimates stay marked.
        let claude = monthlyProvider(.claude, fetched: "2026-10-02T19:00:00Z", reset: "2026-11-01T00:00:00Z", estimated: true)
        XCTAssertEqual(short(claude.metrics[0], claude, capitalized: true), "Resets Oct 31 (est.)")
        // An unknown period also shows the date.
        var unknownPeriod = claude
        unknownPeriod.metrics[0].period = nil
        XCTAssertEqual(short(unknownPeriod.metrics[0], unknownPeriod, capitalized: true), "Resets Oct 31 (est.)")
        let session = pacedProvider(.claude, metricId: "five_hour", used: 10, period: "five_hours",
                                    fetched: "2026-10-02T19:00:00Z", reset: "2026-10-02T22:30:00Z")
        XCTAssertEqual(short(session.metrics[0], session, capitalized: true), "Resets 3:30 PM")
        // An ended period uses the past tense.
        XCTAssertEqual(short(codex.metrics[0], codex, capitalized: true, at: SnapshotDate.parse("2026-11-02T00:00:00Z")),
                       "Reset Oct 31")
        XCTAssertNil(short(devin.metrics[0], devin))
    }

    func testContextCaptionExplainsMissingReset() {
        let locale = Locale(identifier: "en_US")
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        let fetched = SnapshotDate.parse("2026-10-02T19:00:00Z")!
        let devin = devinProvider(fetched: fetched)
        XCTAssertEqual(UsagePresentation.contextCaption(for: devin.metrics[0], provider: devin, now: fetched,
                                                        locale: locale, timeZone: zone),
                       "Shared team balance · no reset")
        let unknown = pacedProvider(.codex, used: 10, period: nil, fetched: "2026-10-02T19:00:00Z", reset: nil)
        XCTAssertEqual(UsagePresentation.contextCaption(for: unknown.metrics[0], provider: unknown, now: fetched,
                                                        locale: locale, timeZone: zone),
                       "Reset not reported")
        var individualBalance = devin
        individualBalance.metrics[0].scope = .individual
        XCTAssertEqual(UsagePresentation.contextCaption(for: individualBalance.metrics[0], provider: individualBalance,
                                                        now: fetched, locale: locale, timeZone: zone),
                       "Reset not reported")
    }

    func testDetailsHoistSharedResetAndGroupScopes() {
        let locale = Locale(identifier: "en_US")
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        let fetched = SnapshotDate.parse("2026-10-02T19:00:00Z")!
        let reset = SnapshotDate.parse("2026-10-30T16:43:25Z")!
        func metric(_ id: String, _ scope: AllowanceScope, reset: Date?) -> UsageMetric {
            UsageMetric(id: id, label: id, kind: .budget, scope: scope, unit: "USD", used: 1, limit: 10,
                        remaining: 9, resetsAt: reset, period: "month")
        }
        var cursor = ProviderSnapshot(id: .cursor, name: "Cursor", status: .ok, message: nil, fetchedAt: fetched,
                                      lastAttemptAt: fetched, primaryMetricId: "included", metrics: [
            metric("included", .individual, reset: reset),
            metric("individual_on_demand", .individual, reset: reset.addingTimeInterval(30)),
            metric("team_on_demand", .team, reset: reset),
        ])
        XCTAssertEqual(UsagePresentation.sharedResetCaption(for: cursor, locale: locale, timeZone: zone)?
                           .replacingOccurrences(of: "\u{202f}", with: " "),
                       "All allowances reset Oct 30 at 9:43 AM PDT")
        XCTAssertEqual(UsagePresentation.scopeGroups(for: cursor).map(\.scope), [.individual, .team])
        XCTAssertEqual(UsagePresentation.scopeGroups(for: cursor).map { $0.metrics.map(\.id) },
                       [["included", "individual_on_demand"], ["team_on_demand"]])
        XCTAssertNil(UsagePresentation.scopeFootnote(for: cursor))
        // More than a minute apart, or a metric without a reset, keeps resets on each card.
        cursor.metrics[1].resetsAt = reset.addingTimeInterval(61)
        XCTAssertNil(UsagePresentation.sharedReset(for: cursor))
        cursor.metrics[1].resetsAt = nil
        XCTAssertNil(UsagePresentation.sharedReset(for: cursor))

        // Devin's quotas reset at different times, and its balance never does.
        let devin = devinProvider(fetched: fetched)
        XCTAssertNil(UsagePresentation.sharedReset(for: devin))
        XCTAssertEqual(UsagePresentation.scopeGroups(for: devin).map(\.scope), [.team, .unknown])
        XCTAssertEqual(UsagePresentation.balanceNote(for: devin.metrics[0], provider: devin),
                       "Shared team balance. Devin reports only the remaining amount; purchased credits do not expire, so there is no reset.")

        // A single allowance never hoists its reset; an unknown single scope gets a footnote.
        let claude = monthlyProvider(.claude, fetched: "2026-10-02T19:00:00Z")
        XCTAssertNil(UsagePresentation.sharedReset(for: claude))
        XCTAssertEqual(UsagePresentation.scopeFootnote(for: claude),
                       "Claude does not report whether this allowance is personal or organizational.")
        let codex = pacedProvider(.codex, used: 10, period: "month", fetched: "2026-10-02T19:00:00Z",
                                  reset: "2026-11-01T00:00:00Z")
        XCTAssertEqual(UsagePresentation.scopeFootnote(for: codex), "Codex reports this as an individual allowance.")
    }

    func testDetailsFreshnessAndFailedAttempt() {
        let locale = Locale(identifier: "en_US")
        let zone = TimeZone(identifier: "America/Los_Angeles")!
        let fetched = SnapshotDate.parse("2026-10-02T18:45:04Z")!
        let current = fetched.addingTimeInterval(14 * 60)
        func clean(_ text: String?) -> String? { text?.replacingOccurrences(of: "\u{202f}", with: " ") }
        XCTAssertEqual(clean(UsagePresentation.detailsFreshness(fetchedAt: fetched, now: current, locale: locale, timeZone: zone)),
                       "Updated 14m ago · 11:45:04 AM")
        // An older fetch names its date so the clock time is unambiguous.
        XCTAssertEqual(clean(UsagePresentation.detailsFreshness(fetchedAt: fetched, now: current.addingTimeInterval(86_400),
                                                                locale: locale, timeZone: zone)),
                       "Updated 1d 0h ago · Oct 2, 11:45:04 AM")
        XCTAssertEqual(UsagePresentation.detailsFreshness(fetchedAt: nil, now: current), "Never updated")

        var provider = ProviderSnapshot(id: .claude, name: "Claude", status: .ok, message: nil, fetchedAt: fetched,
                                        lastAttemptAt: fetched.addingTimeInterval(5), primaryMetricId: nil, metrics: [])
        XCTAssertNil(UsagePresentation.failedAttemptNote(for: provider, now: current, locale: locale, timeZone: zone))
        provider.status = .error
        provider.lastAttemptAt = SnapshotDate.parse("2026-10-02T18:59:00Z")
        XCTAssertEqual(clean(UsagePresentation.failedAttemptNote(for: provider, now: current, locale: locale, timeZone: zone)),
                       "Last attempt 11:59 AM did not succeed")
        provider.fetchedAt = nil
        XCTAssertEqual(clean(UsagePresentation.failedAttemptNote(for: provider, now: current, locale: locale, timeZone: zone)),
                       "Last attempt 11:59 AM did not succeed")
    }

    func testEnabledDefaultsToTrueAndHiddenProvidersStillValidate() throws {
        // Snapshots from collectors that predate hiding have no `enabled` key.
        let absent = try decode(fixture())
        XCTAssertEqual(absent.providers.map(\.enabled), [true, true, true, true])
        XCTAssertEqual(absent.hiddenProviders.count, 0)

        var value = fixture()
        var providers = value["providers"] as! [[String: Any]]
        // A disabled provider keeps its retained metrics and newest fetch; it still validates.
        providers[2]["enabled"] = false
        providers[2]["fetched_at"] = "2026-10-01T11:59:00Z"
        // A disabled provider that was never collected is the collector's placeholder entry.
        providers[3] = ["id": "devin", "name": "Devin", "status": "setup", "message": "Waiting for first collection.",
                        "fetched_at": NSNull(), "last_attempt_at": NSNull(), "primary_metric_id": NSNull(),
                        "metrics": [], "enabled": false]
        providers[1]["enabled"] = true
        value["providers"] = providers
        let snapshot = try decode(value)
        XCTAssertEqual(snapshot.providers.map(\.enabled), [true, true, false, false])
        XCTAssertEqual(snapshot.providers[2].metrics.count, 1)
        XCTAssertEqual(snapshot.visibleProviders.map(\.id), [.claude, .codex])
        XCTAssertEqual(snapshot.hiddenProviders.map(\.id), [.cursor, .devin])
        XCTAssertEqual(UsagePresentation.hiddenSummary(for: snapshot), "Hidden: Cursor, Devin")
        XCTAssertNil(UsagePresentation.hiddenSummary(for: absent))
        // The hidden provider's newer retained fetch does not become the consolidated freshness.
        XCTAssertEqual(UsagePresentation.latestFetch(in: snapshot), SnapshotDate.parse("2026-10-01T11:00:00Z"))

        // Other validation is unchanged for hidden providers.
        providers[2]["primary_metric_id"] = "missing"
        value["providers"] = providers
        XCTAssertThrowsError(try decode(value))
    }

    func testCallToActionFollowsCollectorVerdictOnly() {
        var provider = ProviderSnapshot(id: .devin, name: "Devin", status: .setup,
                                        message: "Devin login was not found. Run devin auth login.",
                                        fetchedAt: nil, lastAttemptAt: now, primaryMetricId: nil, metrics: [])
        XCTAssertEqual(UsagePresentation.callToAction(for: provider), .setUp)
        XCTAssertTrue(UsagePresentation.isNotSetUp(provider))
        XCTAssertEqual(UsagePresentation.callToAction(for: provider)?.rowHint, "Click for setup steps")
        // The placeholder before any collection has setup status but no attempt.
        provider.lastAttemptAt = nil
        XCTAssertNil(UsagePresentation.callToAction(for: provider))
        XCTAssertFalse(UsagePresentation.isNotSetUp(provider))
        XCTAssertNil(UsagePresentation.callToAction(for: .unavailable(.claude, message: "Waiting for the first collection.")))

        provider.status = .loginRequired
        XCTAssertEqual(UsagePresentation.callToAction(for: provider), .signIn)
        XCTAssertEqual(UsagePresentation.callToAction(for: provider)?.rowHint, "Click to see how to sign in")
        XCTAssertFalse(UsagePresentation.isNotSetUp(provider))
        provider.status = .error
        XCTAssertNil(UsagePresentation.callToAction(for: provider))
        provider.status = .ok
        XCTAssertNil(UsagePresentation.callToAction(for: provider))

        // Setup with retained values keeps the normal row; only the details add the steps.
        let retained = devinProvider(fetched: now)
        var lapsed = retained
        lapsed.status = .setup
        lapsed.lastAttemptAt = now
        XCTAssertEqual(UsagePresentation.callToAction(for: lapsed), .setUp)
        XCTAssertFalse(UsagePresentation.isNotSetUp(lapsed))
    }

    private func settingsObject(_ data: Data) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    func testInstallationSettingsReadModifyWritePreservesOtherKeys() throws {
        let original = Data("""
        {"python": "/opt/homebrew/bin/python3", "collector_path": "/Users/me/Library/Application Support/Bars/runtime",
         "app_executable": "/Applications/Bars.app/Contents/MacOS/Bars", "path": "/usr/bin:/bin", "port": 3}
        """.utf8)
        XCTAssertEqual(try InstallationSettings.disabledProviders(in: original), [])

        let hidden = try InstallationSettings.updating(original, provider: .devin, hidden: true)
        var object = try settingsObject(hidden)
        XCTAssertEqual(object["python"] as? String, "/opt/homebrew/bin/python3")
        XCTAssertEqual(object["collector_path"] as? String, "/Users/me/Library/Application Support/Bars/runtime")
        XCTAssertEqual(object["app_executable"] as? String, "/Applications/Bars.app/Contents/MacOS/Bars")
        XCTAssertEqual(object["path"] as? String, "/usr/bin:/bin")
        XCTAssertEqual(object["port"] as? Int, 3)
        XCTAssertEqual(object["disabled_providers"] as? [String], ["devin"])
        XCTAssertEqual(object.count, 6)
        XCTAssertTrue(String(decoding: hidden, as: UTF8.self).contains("\"/usr/bin:/bin\""))

        // IDs stay unique; hiding twice changes nothing but moves the id to the end.
        let both = try InstallationSettings.updating(
            InstallationSettings.updating(hidden, provider: .cursor, hidden: true), provider: .devin, hidden: true)
        XCTAssertEqual(try settingsObject(both)["disabled_providers"] as? [String], ["cursor", "devin"])
        XCTAssertEqual(try InstallationSettings.disabledProviders(in: both), [.cursor, .devin])

        // Showing removes only that ID and keeps every other entry, including ones for plugins
        // this build has never seen and entries that are not valid ids.
        let future = Data(#"{"python": "/p", "disabled_providers": ["devin", "gemini", "cursor", "devin", "Not-An-Id"]}"#.utf8)
        let shown = try InstallationSettings.updating(future, provider: .devin, hidden: false)
        object = try settingsObject(shown)
        XCTAssertEqual(object["disabled_providers"] as? [String], ["gemini", "cursor", "Not-An-Id"])
        XCTAssertEqual(object["python"] as? String, "/p")
        XCTAssertEqual(try InstallationSettings.disabledProviders(in: shown), [ProviderID(rawValue: "gemini")!, .cursor])
        let none = try InstallationSettings.updating(shown, provider: .cursor, hidden: false)
        XCTAssertEqual(try settingsObject(none)["disabled_providers"] as? [String], ["gemini", "Not-An-Id"])

        // Malformed settings are never rewritten.
        XCTAssertThrowsError(try InstallationSettings.updating(Data("[]".utf8), provider: .claude, hidden: true))
        XCTAssertThrowsError(try InstallationSettings.updating(Data("not json".utf8), provider: .claude, hidden: true))
        XCTAssertThrowsError(try InstallationSettings.updating(Data(#"{"disabled_providers": "devin"}"#.utf8),
                                                               provider: .claude, hidden: true))
        XCTAssertThrowsError(try InstallationSettings.disabledProviders(in: Data(#"{"disabled_providers": [1]}"#.utf8)))
        XCTAssertEqual(try InstallationSettings.disabledProviders(in: Data(#"{"disabled_providers": null}"#.utf8)), [])
    }

    func testInstallationSettingsFileIsReplacedAtomicallyWithPrivateMode() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent(InstallationSettings.fileName)
        // Missing settings mean Bars is not installed; nothing is created.
        XCTAssertThrowsError(try InstallationSettings.setHidden(true, provider: .cursor, at: url))
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))

        try Data(#"{"python": "/opt/homebrew/bin/python3", "path": "/usr/bin"}"#.utf8).write(to: url)
        XCTAssertEqual(chmod(url.path, 0o644), 0)
        try InstallationSettings.setHidden(true, provider: .cursor, at: url)
        let object = try settingsObject(Data(contentsOf: url))
        XCTAssertEqual(object["disabled_providers"] as? [String], ["cursor"])
        XCTAssertEqual(object["python"] as? String, "/opt/homebrew/bin/python3")
        XCTAssertEqual(object["path"] as? String, "/usr/bin")
        let permissions = try FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as? Int
        XCTAssertEqual(permissions, 0o600)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: directory.path), [InstallationSettings.fileName])

        // A malformed file is left exactly as it was.
        let corrupt = Data("{\"python\": ".utf8)
        try corrupt.write(to: url)
        XCTAssertThrowsError(try InstallationSettings.setHidden(false, provider: .cursor, at: url))
        XCTAssertEqual(try Data(contentsOf: url), corrupt)
    }

    func testAtomicReplaceRemovesTemporaryFileOnFailureAndNeverFollowsALink() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        // A file cannot be renamed over a directory, so the final step fails after the write.
        let blocked = directory.appendingPathComponent("blocked")
        try FileManager.default.createDirectory(at: blocked, withIntermediateDirectories: true)
        XCTAssertThrowsError(try SnapshotStore.replaceAtomically(blocked, with: Data("new".utf8), temporaryPrefix: ".check"))
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: directory.path), ["blocked"])

        // A symlink at the destination is itself replaced; the file it pointed to is untouched.
        let target = directory.appendingPathComponent("target.json")
        try Data("old".utf8).write(to: target)
        let link = directory.appendingPathComponent("link.json")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: target)
        try SnapshotStore.replaceAtomically(link, with: Data("new".utf8), temporaryPrefix: ".check")
        XCTAssertEqual(try Data(contentsOf: target), Data("old".utf8))
        // `readData` refuses symlinks, so reading the new content proves the link is gone.
        XCTAssertEqual(try SnapshotStore.readData(from: link), Data("new".utf8))
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: directory.path).sorted(),
                       ["blocked", "link.json", "target.json"])
    }

    func testBarAccessibilityValueRoundsLikeVisibleText() {
        // The visible text rounds 99.6% to 100%; VoiceOver must not say 99.
        XCTAssertEqual(UsagePresentation.barAccessibilityValue(fill: 0.996, pace: nil), "100% of allowance used")
        let pace = UsagePace(usedShare: 0.5, elapsedShare: 0.254, periodStart: now,
                             periodEnd: now.addingTimeInterval(3_600), projectedExhaustion: nil)
        XCTAssertEqual(UsagePresentation.barAccessibilityValue(fill: 0.5, pace: pace),
                       "50% of allowance used, 25% of period elapsed")
    }

    func testDeepLinkRoundTripsAndRejectsOtherPaths() {
        for id in ProviderID.standard + [ProviderID(rawValue: "my_plugin2")!] {
            XCTAssertEqual(id.deepLink.absoluteString, "bars://provider/\(id.rawValue)")
            XCTAssertEqual(ProviderID(deepLink: id.deepLink), id)
        }
        XCTAssertEqual(ProviderID(deepLink: URL(string: "bars://provider/cursor/")!), .cursor)
        for link in ["bars://provider/extra/cursor", "bars://provider/cursor/extra", "bars://provider/",
                     "bars://provider", "bars://settings/cursor", "https://provider/cursor",
                     "bars://provider/Cursor", "bars:provider/cursor", "bars://provider/my-plugin",
                     "bars://provider/2fast", "bars://provider/%2e%2e"] {
            XCTAssertNil(ProviderID(deepLink: URL(string: link)!))
        }
        // Only the collector's id grammar is accepted anywhere an id is parsed.
        XCTAssertNil(ProviderID(rawValue: "Claude"))
        XCTAssertNil(ProviderID(rawValue: ""))
        XCTAssertNil(ProviderID(rawValue: String(repeating: "a", count: 33)))
        XCTAssertEqual(ProviderID(rawValue: String(repeating: "a", count: 32))?.rawValue.count, 32)
    }

    func testRateLimitPauseDecodesAndIsShownOnlyWhileItLasts() throws {
        let locale = Locale(identifier: "en_US_POSIX")
        let zone = TimeZone(identifier: "UTC")!
        let json = """
        {"schema_version": 2, "generated_at": "2026-10-01T12:05:00Z", "providers": [
         {"id": "p", "name": "P", "status": "error", "message": "Provider rate limit reached. Collection will retry later.",
          "fetched_at": "2026-10-01T12:00:00Z", "last_attempt_at": "2026-10-01T12:05:00Z",
          "next_attempt_at": "2026-10-01T12:10:00Z", "primary_metric_id": null, "metrics": []}]}
        """
        let provider = try UsageSnapshot.decode(Data(json.utf8)).providers[0]
        XCTAssertEqual(provider.nextAttemptAt, SnapshotDate.parse("2026-10-01T12:10:00Z"))
        let during = SnapshotDate.parse("2026-10-01T12:07:00Z")!
        let after = SnapshotDate.parse("2026-10-01T12:11:00Z")!
        XCTAssertTrue(provider.isPausing(at: during))
        XCTAssertFalse(provider.isPausing(at: after))
        XCTAssertEqual(UsagePresentation.pauseText(for: provider, now: during, locale: locale, timeZone: zone)?
                           .replacingOccurrences(of: "\u{202f}", with: " "), "retries 12:10 PM")
        XCTAssertNil(UsagePresentation.pauseText(for: provider, now: after, locale: locale, timeZone: zone))
        XCTAssertEqual(UsagePresentation.failedAttemptNote(for: provider, now: during, locale: locale, timeZone: zone)?
                           .replacingOccurrences(of: "\u{202f}", with: " "),
                       "Last attempt 12:05 PM hit the provider's rate limit · retries 12:10 PM")
        // Across the autumn change the retry names its zone, since the clock time repeats.
        let losAngeles = TimeZone(identifier: "America/Los_Angeles")!
        var autumn = provider
        autumn.lastAttemptAt = SnapshotDate.parse("2026-11-01T08:58:00Z")
        autumn.nextAttemptAt = SnapshotDate.parse("2026-11-01T09:08:00Z")  // 1:08 AM PST
        let beforeChange = SnapshotDate.parse("2026-11-01T08:58:00Z")!      // 1:58 AM PDT
        XCTAssertEqual(UsagePresentation.pauseText(for: autumn, now: beforeChange, locale: locale, timeZone: losAngeles)?
                           .replacingOccurrences(of: "\u{202f}", with: " "), "retries 1:08 AM PST")
        // A success and a pause contradict each other; the publisher refuses the pair.
        let contradictory = """
        {"schema_version": 2, "generated_at": "2026-10-01T12:05:00Z", "providers": [
         {"id": "p", "name": "P", "status": "ok", "message": null, "fetched_at": "2026-10-01T12:05:00Z",
          "last_attempt_at": "2026-10-01T12:05:00Z", "next_attempt_at": "2026-10-01T12:10:00Z", "primary_metric_id": "m",
          "metrics": [{"id": "m", "label": "M", "kind": "quota", "scope": "individual", "unit": "%", "used": 1, "limit": 100,
                       "remaining": 99, "resets_at": null}]}]}
        """
        XCTAssertThrowsError(try UsageSnapshot.decode(Data(contradictory.utf8)))
        // Older snapshots without the field decode with no pause.
        var plain = provider
        plain.nextAttemptAt = nil
        XCTAssertFalse(plain.isPausing(at: during))
        XCTAssertEqual(UsagePresentation.failedAttemptNote(for: plain, now: during, locale: locale, timeZone: zone)?
                           .replacingOccurrences(of: "\u{202f}", with: " "), "Last attempt 12:05 PM did not succeed")
    }

    func testCompactRowsCapSecondaryAllowances() {
        // A plugin may select up to 31 mini bars; a compact row shows a few and counts the rest.
        let many = (0..<31).map { index in
            SecondaryAllowance(metric: UsageMetric(id: "m\(index)", label: "Allowance \(index)", kind: .quota, scope: .unknown,
                                                   unit: "%", used: 1, limit: 100, remaining: 99, resetsAt: nil),
                               value: "1%", reset: nil, note: nil, pace: nil)
        }
        let capped = UsagePresentation.compactSecondaryAllowances(many)
        XCTAssertEqual(capped.shown.map(\.id), ["m0", "m1", "m2"])
        XCTAssertEqual(capped.omitted, 28)
        let few = UsagePresentation.compactSecondaryAllowances(Array(many.prefix(2)))
        XCTAssertEqual(few.shown.count, 2)
        XCTAssertEqual(few.omitted, 0)
        // The widget shares a budget of six across rows, one at a time, so the standard four
        // providers keep today's layout and a dense plugin cannot push the others off.
        XCTAssertEqual(UsagePresentation.compactSecondaryAllocation([1, 1, 2, 2]), [1, 1, 2, 2])
        XCTAssertEqual(UsagePresentation.compactSecondaryAllocation([31, 1, 2, 2]), [2, 1, 2, 1])
        XCTAssertEqual(UsagePresentation.compactSecondaryAllocation([31, 31, 31, 31]), [2, 2, 1, 1])
        XCTAssertEqual(UsagePresentation.compactSecondaryAllocation([31]), [3])
        XCTAssertEqual(UsagePresentation.compactSecondaryAllocation([0, 0]), [0, 0])
        XCTAssertEqual(UsagePresentation.compactSecondaryAllocation([]), [])
    }

    func testMissingFileIsDistinguishedFromOtherReadFailures() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        func failure(_ read: () throws -> Void) -> Error? {
            do { try read(); return nil } catch { return error }
        }
        let missing = try XCTUnwrap(failure { _ = try SnapshotStore.readData(from: directory.appendingPathComponent("absent.json")) })
        XCTAssertTrue(SnapshotStore.isMissingFile(missing))
        let noDirectory = directory.appendingPathComponent("absent/installation.json")
        let replace = try XCTUnwrap(failure { try SnapshotStore.replaceAtomically(noDirectory, with: Data(), temporaryPrefix: ".check") })
        XCTAssertTrue(SnapshotStore.isMissingFile(replace))
        let link = directory.appendingPathComponent("link.json")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: directory.appendingPathComponent("target.json"))
        let refused = try XCTUnwrap(failure { _ = try SnapshotStore.readData(from: link) })
        XCTAssertFalse(SnapshotStore.isMissingFile(refused))
        XCTAssertFalse(SnapshotStore.isMissingFile(SnapshotError.invalidMetrics))
    }

    func testPublishedPathIsSeparateFromCollectorStaging() throws {
        let url = try SnapshotStore.sharedURL()
        XCTAssertEqual(url.lastPathComponent, "widget-snapshot.json")
        XCTAssertTrue(url.path.hasSuffix("/Library/Application Support/Bars/widget-snapshot.json"))
        XCTAssertFalse(url.path.contains("/Library/Containers/"))
    }

    func testOversizedOrSymlinkSourceDoesNotReplacePublishedData() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let source = directory.appendingPathComponent("source.json")
        let published = directory.appendingPathComponent("widget-snapshot.json")
        let good = try JSONSerialization.data(withJSONObject: fixture())
        try good.write(to: source)
        try SnapshotStore.publish(from: source, to: published)
        let link = directory.appendingPathComponent("link.json")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: source)
        XCTAssertThrowsError(try SnapshotStore.publish(from: link, to: published))
        let handle = try FileHandle(forWritingTo: source)
        try handle.truncate(atOffset: UInt64(SnapshotStore.maximumSnapshotBytes + 1))
        try handle.close()
        XCTAssertThrowsError(try SnapshotStore.publish(from: source, to: published))
        XCTAssertEqual(try Data(contentsOf: published), good)
    }

    func testNonregularSourceIsRejectedWithoutWaitingForAWriter() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let fifo = directory.appendingPathComponent("unexpected-fifo.json")
        XCTAssertEqual(mkfifo(fifo.path, 0o600), 0)
        XCTAssertThrowsError(try SnapshotStore.read(from: fifo))
        XCTAssertThrowsError(try SnapshotStore.publish(from: fifo,
                                                      to: directory.appendingPathComponent("published.json")))
    }
}
