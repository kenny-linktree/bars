import Foundation
@testable import BarsViews

/// One data scenario rendered on every surface.
struct PreviewVariant {
    let name: String
    let now: Date
    let snapshot: UsageSnapshot
    /// Messages the dropdown shows above its content, mirroring `BarsModel.storageError`.
    var dropdownMessages: [String] = []
}

enum Fixtures {
    static let names = ["live", "stale", "mixed", "heavy", "previous", "empty", "setup", "hidden", "subscription"]
    static let syntheticNames = names.filter { $0 != "live" }
    /// Synthetic variants share one fixed instant so their renders are deterministic.
    static let fixedNow = utc("2026-10-09T17:00:00Z")

    /// Only an explicit `live` request reads the published snapshot. Every other variant uses
    /// fixed synthetic values, so previews are safe to share and do not change with local usage.
    static func make(_ requested: [String], wallNow: Date, warn: (String) -> Void,
                     loadLive: () throws -> UsageSnapshot = {
                         try SnapshotStore.read(from: SnapshotStore.sharedURL())
                     }) throws -> [PreviewVariant] {
        var live: UsageSnapshot?
        if requested.contains("live") {
            do { live = try loadLive() }
            catch { warn("live snapshot unavailable; skipping live") }
        }
        return try requested.compactMap { name -> PreviewVariant? in
            let variant: PreviewVariant
            switch name {
            case "live":
                guard let live else { return nil }
                variant = .init(name: name, now: wallNow, snapshot: live)
            case "stale": variant = .init(name: name, now: fixedNow, snapshot: stale(baseline(now: fixedNow), now: fixedNow))
            case "mixed": variant = .init(name: name, now: fixedNow, snapshot: mixed(now: fixedNow))
            case "heavy": variant = .init(name: name, now: fixedNow, snapshot: heavy(now: fixedNow))
            case "previous": variant = .init(name: name, now: fixedNow, snapshot: previous(now: fixedNow))
            case "setup": variant = .init(name: name, now: fixedNow, snapshot: setup(now: fixedNow))
            case "hidden": variant = .init(name: name, now: fixedNow, snapshot: hidden(now: fixedNow))
            case "subscription": variant = .init(name: name, now: fixedNow, snapshot: subscription(now: fixedNow))
            case "empty":
                // First launch: BarsModel's initial snapshot plus its missing-file storage message.
                variant = .init(name: name, now: fixedNow,
                                snapshot: .unavailable(message: "Waiting for the first collection."),
                                dropdownMessages: ["No usage data yet. Choose Refresh now after installing the collector."])
            default: throw PreviewError.usage("Unknown variant \(name). Choose from: \(names.joined(separator: ", "))")
            }
            // Prove each fixture is something the app could actually read.
            return .init(name: variant.name, now: variant.now, snapshot: try roundTrip(variant.snapshot),
                         dropdownMessages: variant.dropdownMessages)
        }
    }

    // MARK: Variants

    static func subscription(now: Date) -> UsageSnapshot {
        var snapshot = baseline(now: now)
        for (id, short, weekly) in [(ProviderID.claude, "five_hour", "seven_day"),
                                    (ProviderID.codex, "primary_window", "secondary_window")] {
            snapshot.update(id) {
                $0.primaryMetricId = short
                $0.metrics = [
                    .init(id: short, label: id == .claude ? "5-hour quota" : "Session quota",
                          kind: .quota, scope: .individual, unit: "%", used: 35, limit: 100, remaining: 65,
                          resetsAt: now.addingTimeInterval(2 * 3_600), period: id == .claude ? "five_hours" : nil),
                    .init(id: weekly, label: "Weekly quota", kind: .quota, scope: .individual, unit: "%",
                          used: 55, limit: 100, remaining: 45,
                          resetsAt: now.addingTimeInterval(3 * 86_400), period: "week"),
                ]
            }
        }
        return snapshot
    }

    static func stale(_ base: UsageSnapshot, now: Date) -> UsageSnapshot {
        var snapshot = base
        let fetched = now.addingTimeInterval(-2 * 3_600)
        for index in snapshot.providers.indices {
            snapshot.providers[index].fetchedAt = fetched
            snapshot.providers[index].lastAttemptAt = fetched
        }
        snapshot.generatedAt = fetched
        return snapshot
    }

    static func mixed(now: Date) -> UsageSnapshot {
        var snapshot = baseline(now: now)
        snapshot.update(.claude) { $0.fetchedAt = now.addingTimeInterval(-120); $0.lastAttemptAt = $0.fetchedAt }
        snapshot.update(.codex) {
            $0.status = .error
            $0.message = "Codex usage request failed with HTTP 503. Showing the last successful values."
            $0.fetchedAt = now.addingTimeInterval(-3 * 3_600)
            $0.lastAttemptAt = now.addingTimeInterval(-60)
        }
        snapshot.update(.cursor) {
            $0.status = .loginRequired
            $0.message = "Cursor sign-in expired. Sign in to Cursor, then refresh Bars."
            $0.fetchedAt = nil
            $0.lastAttemptAt = now.addingTimeInterval(-60)
            $0.primaryMetricId = nil
            $0.metrics = []
        }
        snapshot.update(.devin) { $0.fetchedAt = now.addingTimeInterval(-40 * 60); $0.lastAttemptAt = $0.fetchedAt }
        return snapshot
    }

    static func heavy(now: Date) -> UsageSnapshot {
        var snapshot = baseline(now: now)
        // About 28% of Claude's month has elapsed at the fixed instant, so 92% is ahead of pace.
        snapshot.update(.claude) { $0.update("monthly_spend") { $0.set(used: 1_840, limit: 2_000) } }
        // A reset six days out puts 80% of Codex's period behind it, so 75% is on pace.
        snapshot.update(.codex) {
            $0.update("monthly_credits") { $0.set(used: 112_500, limit: 150_000); $0.resetsAt = now.addingTimeInterval(6 * 86_400) }
        }
        snapshot.update(.cursor) {
            $0.update("included") { $0.set(used: 100, limit: 100) }
            $0.update("individual_on_demand") { $0.set(used: 320, limit: 400) }
            $0.update("team_on_demand") { $0.set(used: 2_970, limit: 3_000) }
        }
        snapshot.update(.devin) {
            $0.update("daily") { $0.set(used: 60, limit: 100) }
            $0.update("weekly") { $0.set(used: 100, limit: 100) }
        }
        return snapshot
    }

    static func previous(now: Date) -> UsageSnapshot {
        var snapshot = baseline(now: now)
        // Refreshes have failed since before the reset, so the retained values are last period's.
        snapshot.update(.codex) {
            $0.status = .error
            $0.message = "Codex usage request timed out."
            $0.fetchedAt = now.addingTimeInterval(-8 * 3_600)
            $0.lastAttemptAt = now.addingTimeInterval(-120)
            $0.update("monthly_credits") { $0.set(used: 147_000, limit: 150_000); $0.resetsAt = now.addingTimeInterval(-6 * 3_600) }
        }
        // No refresh has run since before the reset (for example, the Mac was asleep).
        snapshot.update(.cursor) {
            $0.fetchedAt = now.addingTimeInterval(-3 * 3_600)
            $0.lastAttemptAt = $0.fetchedAt
            for index in $0.metrics.indices { $0.metrics[index].resetsAt = now.addingTimeInterval(-2 * 3_600) }
            $0.update("included") { $0.set(used: 100, limit: 100) }
            $0.update("individual_on_demand") { $0.set(used: 80, limit: 400) }
        }
        return snapshot
    }

    /// Someone without Cursor or Devin: the collector found no login material for either.
    /// Messages are the collector's own setup messages.
    static func setup(now: Date) -> UsageSnapshot {
        var snapshot = baseline(now: now)
        let messages: [ProviderID: String] = [
            .cursor: "Existing login was not found. Sign in with the provider CLI.",
            .devin: "Devin login was not found. Run devin auth login.",
        ]
        for (id, message) in messages {
            snapshot.update(id) {
                $0.status = .setup
                $0.message = message
                $0.fetchedAt = nil
                $0.lastAttemptAt = now.addingTimeInterval(-180)
                $0.primaryMetricId = nil
                $0.metrics = []
            }
        }
        return snapshot
    }

    /// Cursor was hidden after collecting, so it keeps its last values; Devin was hidden before
    /// its first collection and is still the collector's placeholder entry.
    static func hidden(now: Date) -> UsageSnapshot {
        var snapshot = baseline(now: now)
        snapshot.update(.cursor) {
            $0.enabled = false
            $0.fetchedAt = now.addingTimeInterval(-2 * 86_400)
            $0.lastAttemptAt = $0.fetchedAt
        }
        snapshot.update(.devin) {
            $0 = ProviderSnapshot(id: .devin, name: "Devin", status: .setup, message: "Waiting for first collection.",
                                  fetchedAt: nil, lastAttemptAt: nil, primaryMetricId: nil, metrics: [], enabled: false)
        }
        return snapshot
    }

    /// Same providers, metric identities, labels, scopes, units and periods as the collector emits.
    /// Reset instants are derived from `now` the way each provider schedules them.
    static func baseline(now: Date) -> UsageSnapshot {
        let fetched = now.addingTimeInterval(-180)
        let monthStart = next(DateComponents(day: 1, hour: 0, minute: 0, second: 0), after: now)
        let cursorReset = next(DateComponents(day: 30, hour: 16, minute: 43, second: 25), after: now)
        let devinDaily = next(DateComponents(hour: 8, minute: 0, second: 0), after: now)
        let devinWeekly = next(DateComponents(hour: 8, minute: 0, second: 0, weekday: 1), after: now)
        func provider(_ id: ProviderID, primary: String, _ metrics: [UsageMetric]) -> ProviderSnapshot {
            .init(id: id, name: id.displayName, status: .ok, message: nil, fetchedAt: fetched,
                  lastAttemptAt: fetched, primaryMetricId: primary, metrics: metrics)
        }
        func metric(_ id: String, _ label: String, _ kind: MetricKind, _ scope: AllowanceScope, _ unit: String,
                    used: Double?, limit: Double?, remaining: Double? = nil, resetsAt: Date?, period: AllowancePeriod?) -> UsageMetric {
            .init(id: id, label: label, kind: kind, scope: scope, unit: unit, used: used, limit: limit,
                  remaining: remaining ?? used.flatMap { used in limit.map { max($0 - used, 0) } },
                  resetsAt: resetsAt, period: period?.rawValue)
        }
        return .init(schemaVersion: UsageSnapshot.schemaVersion, generatedAt: fetched, providers: [
            provider(.claude, primary: "monthly_spend", [
                metric("monthly_spend", "Monthly budget", .budget, .unknown, "USD",
                       used: 400, limit: 2_000, resetsAt: nil, period: .month),
            ]),
            provider(.codex, primary: "monthly_credits", [
                metric("monthly_credits", "Monthly credits", .budget, .individual, "credits",
                       used: 36_000, limit: 150_000, resetsAt: monthStart, period: .month),
            ]),
            provider(.cursor, primary: "included", [
                metric("included", "Included usage", .budget, .individual, "USD",
                       used: 32, limit: 100, resetsAt: cursorReset, period: .month),
                metric("individual_on_demand", "Individual on-demand", .budget, .individual, "USD",
                       used: 0, limit: 400, resetsAt: cursorReset, period: .month),
                metric("team_on_demand", "Team on-demand", .budget, .team, "USD",
                       used: 75, limit: 3_000, resetsAt: cursorReset, period: .month),
            ]),
            provider(.devin, primary: "on_demand_balance", [
                metric("on_demand_balance", "On-demand balance", .balance, .team, "USD",
                       used: nil, limit: nil, remaining: 2_500, resetsAt: nil, period: nil),
                metric("daily", "Daily quota", .quota, .unknown, "%",
                       used: 20, limit: 100, resetsAt: devinDaily, period: .day),
                metric("weekly", "Weekly quota", .quota, .unknown, "%",
                       used: 45, limit: 100, resetsAt: devinWeekly, period: .week),
            ]),
        ])
    }

    // MARK: Helpers

    static func next(_ components: DateComponents, after date: Date) -> Date {
        Calendar.utcGregorian.nextDate(after: date, matching: components, matchingPolicy: .nextTime)!
    }

    static func utc(_ value: String) -> Date { SnapshotDate.parse(value)! }

    /// Encodes with the snapshot contract's snake_case keys and UTC timestamps, then decodes with
    /// the app's own validating decoder.
    static func roundTrip(_ snapshot: UsageSnapshot) throws -> UsageSnapshot {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.dateEncodingStrategy = .custom { date, encoder in
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            var container = encoder.singleValueContainer()
            try container.encode(formatter.string(from: date))
        }
        return try UsageSnapshot.decode(encoder.encode(snapshot))
    }
}

extension UsageSnapshot {
    mutating func update(_ id: ProviderID, _ change: (inout ProviderSnapshot) -> Void) {
        guard let index = providers.firstIndex(where: { $0.id == id }) else { return }
        change(&providers[index])
    }
}

extension ProviderSnapshot {
    mutating func update(_ metricID: String, _ change: (inout UsageMetric) -> Void) {
        guard let index = metrics.firstIndex(where: { $0.id == metricID }) else { return }
        change(&metrics[index])
    }
}

extension UsageMetric {
    mutating func set(used: Double, limit: Double) {
        self.used = used
        self.limit = limit
        remaining = max(limit - used, 0)
    }
}
