import Foundation

enum ProviderID: String, Codable, CaseIterable, Identifiable {
    case claude, codex, cursor, devin
    var id: String { rawValue }
    var displayName: String { rawValue.prefix(1).uppercased() + rawValue.dropFirst() }

    /// The link a widget row opens: `bars://provider/<id>`, which shows that provider's details.
    var deepLink: URL { URL(string: "\(Self.deepLinkScheme)://\(Self.deepLinkHost)/\(rawValue)")! }

    /// Parses a link of exactly the `deepLink` form; any other path is not a provider link.
    init?(deepLink url: URL) {
        guard url.scheme == Self.deepLinkScheme, url.host == Self.deepLinkHost,
              url.pathComponents.count == 2, url.pathComponents[0] == "/",
              let id = ProviderID(rawValue: url.pathComponents[1]) else { return nil }
        self = id
    }

    private static let deepLinkScheme = "bars"
    private static let deepLinkHost = "provider"
}

extension Calendar {
    /// The Gregorian calendar in UTC, in which providers define monthly allowance periods.
    static let utcGregorian: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = .gmt
        return calendar
    }()
}

enum FetchStatus: String, Codable {
    case ok, error, loginRequired = "login_required", setup
    var label: String {
        switch self {
        case .ok: return "Updated"
        case .error: return "Update failed"
        case .loginRequired: return "Login required"
        case .setup: return "Setup required"
        }
    }
}

enum MetricKind: String, Codable { case budget, quota, balance, spend }
enum AllowanceScope: String, Codable {
    case individual, team, unknown
    var label: String {
        switch self {
        case .individual: return "Individual"
        case .team: return "Team"
        case .unknown: return "Scope not reported"
        }
    }
}

/// Source-documented allowance period lengths. The start is derived from the reset
/// instant, so a period alone never fabricates a reset.
enum AllowancePeriod: String, Codable {
    case fiveHours = "five_hours", day, week, month

    func start(endingAt reset: Date) -> Date? {
        switch self {
        case .fiveHours: return reset.addingTimeInterval(-5 * 3_600)
        case .day: return reset.addingTimeInterval(-86_400)
        case .week: return reset.addingTimeInterval(-7 * 86_400)
        case .month: return Calendar.utcGregorian.date(byAdding: .month, value: -1, to: reset)
        }
    }

    /// A period at most one day long projects exhaustion to a time of day, not a date.
    var isWithinOneDay: Bool { self == .fiveHours || self == .day }
}

struct UsageMetric: Codable, Identifiable, Equatable {
    var id: String
    var label: String
    var kind: MetricKind
    var scope: AllowanceScope
    var unit: String
    var used: Double?
    var limit: Double?
    var remaining: Double?
    var resetsAt: Date?
    /// Raw period token. Unknown tokens from a newer collector degrade to "no pace" instead of
    /// failing the whole snapshot; `allowancePeriod` is the typed view.
    var period: String?

    var allowancePeriod: AllowancePeriod? { period.flatMap(AllowancePeriod.init(rawValue:)) }

    var fill: Double? {
        guard let used, let limit, limit > 0 else { return nil }
        return min(max(used / limit, 0), 1)
    }

    var overage: Double? {
        guard let used, let limit, used > limit else { return nil }
        return used - limit
    }
}

struct ProviderSnapshot: Codable, Identifiable, Equatable {
    var id: ProviderID
    var name: String
    var status: FetchStatus
    var message: String?
    var fetchedAt: Date?
    var lastAttemptAt: Date?
    var primaryMetricId: String?
    var metrics: [UsageMetric]
    /// False for a hidden provider: the person disabled it, the collector skips it and the
    /// overview omits it. Its other fields keep their last values. Missing in older snapshots.
    var enabled: Bool = true

    /// Two missed 15-minute collection runs. Data older than this is drawn dimmed, and the widget
    /// header shows its age as relative text rather than as a clock time.
    static let staleInterval: TimeInterval = 30 * 60

    var primaryMetric: UsageMetric? { metrics.first { $0.id == primaryMetricId } }
    func isStale(at now: Date) -> Bool { Self.isStale(fetchedAt: fetchedAt, at: now) }

    /// Data that never succeeded counts as stale.
    static func isStale(fetchedAt: Date?, at now: Date) -> Bool {
        guard let fetchedAt else { return true }
        return now.timeIntervalSince(fetchedAt) >= staleInterval
    }

    static func unavailable(_ id: ProviderID, message: String) -> ProviderSnapshot {
        .init(id: id, name: id.displayName, status: .setup, message: message,
              fetchedAt: nil, lastAttemptAt: nil, primaryMetricId: nil, metrics: [])
    }
}

extension ProviderSnapshot {
    /// Decodes `enabled` as optional so snapshots from collectors that predate hiding still read.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(ProviderID.self, forKey: .id)
        name = try container.decode(String.self, forKey: .name)
        status = try container.decode(FetchStatus.self, forKey: .status)
        message = try container.decodeIfPresent(String.self, forKey: .message)
        fetchedAt = try container.decodeIfPresent(Date.self, forKey: .fetchedAt)
        lastAttemptAt = try container.decodeIfPresent(Date.self, forKey: .lastAttemptAt)
        primaryMetricId = try container.decodeIfPresent(String.self, forKey: .primaryMetricId)
        metrics = try container.decode([UsageMetric].self, forKey: .metrics)
        enabled = try container.decodeIfPresent(Bool.self, forKey: .enabled) ?? true
    }
}

struct UsageSnapshot: Codable, Equatable {
    static let schemaVersion = 1
    var schemaVersion: Int
    var generatedAt: Date
    var providers: [ProviderSnapshot]

    static func unavailable(message: String) -> UsageSnapshot {
        .init(schemaVersion: schemaVersion, generatedAt: .distantPast,
              providers: ProviderID.allCases.map { .unavailable($0, message: message) })
    }

    /// Providers the overview and widget show, in contract order.
    var visibleProviders: [ProviderSnapshot] { providers.filter(\.enabled) }
    /// Providers the person has hidden. Only a show control mentions them.
    var hiddenProviders: [ProviderSnapshot] { providers.filter { !$0.enabled } }

    func validate() throws {
        guard schemaVersion == Self.schemaVersion else { throw SnapshotError.unsupportedVersion }
        guard providers.map(\.id) == ProviderID.allCases else { throw SnapshotError.invalidProviders }
        for provider in providers {
            guard !provider.name.isEmpty,
                  Set(provider.metrics.map(\.id)).count == provider.metrics.count,
                  provider.primaryMetricId == nil || provider.primaryMetric != nil
            else { throw SnapshotError.invalidMetrics }
            if provider.status == .ok && (provider.fetchedAt == nil || provider.metrics.isEmpty) {
                throw SnapshotError.invalidMetrics
            }
            if !provider.metrics.isEmpty && provider.fetchedAt == nil { throw SnapshotError.invalidMetrics }
            for metric in provider.metrics {
                guard !metric.id.isEmpty, !metric.label.isEmpty, !metric.unit.isEmpty else {
                    throw SnapshotError.invalidMetrics
                }
                for number in [metric.used, metric.limit, metric.remaining].compactMap({ $0 }) {
                    guard number.isFinite else { throw SnapshotError.invalidMetrics }
                }
                guard (metric.used ?? 0) >= 0, (metric.limit ?? 0) >= 0 else {
                    throw SnapshotError.invalidMetrics
                }
            }
        }
    }

    static func decode(_ data: Data) throws -> UsageSnapshot {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .custom { decoder in
            let string = try decoder.singleValueContainer().decode(String.self)
            guard let date = SnapshotDate.parse(string) else {
                throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath,
                                                       debugDescription: "Expected a UTC ISO-8601 timestamp."))
            }
            return date
        }
        // Check the version before the shape, so a snapshot from a newer collector reports that
        // it needs a newer Bars instead of failing as malformed data.
        struct Version: Decodable { var schemaVersion: Int }
        if let version = try? decoder.decode(Version.self, from: data), version.schemaVersion != Self.schemaVersion {
            throw SnapshotError.unsupportedVersion
        }
        let snapshot = try decoder.decode(Self.self, from: data)
        try snapshot.validate()
        return snapshot
    }
}

enum SnapshotDate {
    static func parse(_ value: String) -> Date? {
        // The collector writes Gregorian UTC dates with four-digit years. Parsing these bytes
        // directly avoids a formatter per field and rejects calendar normalization/trailing text.
        let bytes = Array(value.utf8)
        guard bytes.count >= 20, bytes[4] == 45, bytes[7] == 45, bytes[10] == 84,
              bytes[13] == 58, bytes[16] == 58, bytes.last == 90 else { return nil }
        func digits(_ start: Int, _ count: Int) -> Int? {
            var result = 0
            for byte in bytes[start..<(start + count)] {
                guard byte >= 48, byte <= 57 else { return nil }
                result = result * 10 + Int(byte - 48)
            }
            return result
        }
        guard let year = digits(0, 4), year > 0,
              let month = digits(5, 2), (1...12).contains(month),
              let day = digits(8, 2),
              let hour = digits(11, 2), hour < 24,
              let minute = digits(14, 2), minute < 60,
              let second = digits(17, 2), second < 60 else { return nil }
        let leapYear = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
        let monthLengths = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        guard day >= 1, day <= monthLengths[month - 1] else { return nil }

        var fraction = 0.0
        if bytes.count > 20 {
            guard bytes.count >= 22, bytes[19] == 46 else { return nil }
            var place = 0.1
            for byte in bytes[20..<(bytes.count - 1)] {
                guard byte >= 48, byte <= 57 else { return nil }
                fraction += Double(byte - 48) * place
                place *= 0.1
            }
        }
        // Days before this date in the proleptic Gregorian calendar, relative to 1970-01-01.
        let priorYear = year - 1
        let monthOffsets = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
        let days = 365 * priorYear + priorYear / 4 - priorYear / 100 + priorYear / 400
            + monthOffsets[month - 1] + (leapYear && month > 2 ? 1 : 0) + day - 1 - 719_162
        let seconds = days * 86_400 + hour * 3_600 + minute * 60 + second
        return Date(timeIntervalSince1970: Double(seconds) + fraction)
    }
}

enum SnapshotError: LocalizedError {
    case unsupportedVersion, invalidProviders, invalidMetrics, sharedContainerUnavailable
    var errorDescription: String? {
        switch self {
        case .unsupportedVersion: return "This snapshot needs a newer version of Bars."
        case .invalidProviders: return "The snapshot does not contain all four providers in order."
        case .invalidMetrics: return "The snapshot contains invalid usage data."
        case .sharedContainerUnavailable: return "Bars cannot locate its published widget data. Check this Mac's user account and installation."
        }
    }
}
