import Foundation

/// A provider plugin's id: lowercase ASCII letters, digits and underscores, at most 32 characters,
/// starting with a letter. The set of providers is whatever plugins the collector found; nothing
/// in the native readers names a particular provider.
struct ProviderID: RawRepresentable, Hashable, Codable, Identifiable, Comparable {
    let rawValue: String
    var id: String { rawValue }

    /// Nil for anything the collector would not have produced, so a crafted snapshot or link
    /// cannot smuggle arbitrary text into paths or settings.
    init?(rawValue: String) {
        guard Self.isValid(rawValue) else { return nil }
        self.rawValue = rawValue
    }

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        guard let value = ProviderID(rawValue: raw) else {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath,
                                                   debugDescription: "Invalid provider id."))
        }
        self = value
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }

    static func < (lhs: ProviderID, rhs: ProviderID) -> Bool { lhs.rawValue < rhs.rawValue }

    static func isValid(_ value: String) -> Bool {
        let bytes = Array(value.utf8)
        guard (1...32).contains(bytes.count), (97...122).contains(bytes[0]) else { return false }
        return bytes.allSatisfy { (97...122).contains($0) || (48...57).contains($0) || $0 == 95 }
    }

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

/// Setup steps from a plugin's manifest: how the provider's existing login is created, shown when
/// the collector finds no login material (setup) or cannot use it (login required).
struct ProviderSetupGuide: Codable, Equatable {
    /// The first step's link text, such as "Install the Codex CLI".
    var installTitle: String
    var installUrl: URL
    /// The Terminal command that creates or renews the login Bars reads.
    var loginCommand: String
    /// The second step's text, above the command.
    var loginStep: String
    /// The sign-in-again text, above the command.
    var signInAgain: String
    /// Which login Bars reads, so a person can tell what the steps produce.
    var credentialNote: String
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
    /// True when the plugin derived `resetsAt` from documentation (such as the end of a documented
    /// calendar month) rather than reading it from the source. Shown as "(est.)".
    var resetsAtEstimated: Bool = false
    /// An optional sentence from the plugin about a balance that has no total or used amount.
    var note: String?

    init(id: String, label: String, kind: MetricKind, scope: AllowanceScope, unit: String,
         used: Double?, limit: Double?, remaining: Double? = nil, resetsAt: Date? = nil, period: String? = nil,
         resetsAtEstimated: Bool = false, note: String? = nil) {
        self.id = id
        self.label = label
        self.kind = kind
        self.scope = scope
        self.unit = unit
        self.used = used
        self.limit = limit
        self.remaining = remaining
        self.resetsAt = resetsAt
        self.period = period
        self.resetsAtEstimated = resetsAtEstimated
        self.note = note
    }

    /// Decodes the newer optional fields as absent so version 2 snapshots from an older collector read.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        label = try container.decode(String.self, forKey: .label)
        kind = try container.decode(MetricKind.self, forKey: .kind)
        scope = try container.decode(AllowanceScope.self, forKey: .scope)
        unit = try container.decode(String.self, forKey: .unit)
        used = try container.decodeIfPresent(Double.self, forKey: .used)
        limit = try container.decodeIfPresent(Double.self, forKey: .limit)
        remaining = try container.decodeIfPresent(Double.self, forKey: .remaining)
        resetsAt = try container.decodeIfPresent(Date.self, forKey: .resetsAt)
        period = try container.decodeIfPresent(String.self, forKey: .period)
        resetsAtEstimated = try container.decodeIfPresent(Bool.self, forKey: .resetsAtEstimated) ?? false
        note = try container.decodeIfPresent(String.self, forKey: .note)
    }

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
    /// While set and in the future, the provider's scheduled attempts are paused after a rate
    /// limit; an explicit refresh still tries. Missing in older snapshots.
    var nextAttemptAt: Date?
    var primaryMetricId: String?
    /// Metrics the overview shows as labeled mini bars beneath the primary bar, chosen by the
    /// plugin. Never includes the primary.
    var secondaryMetricIds: [String] = []
    var metrics: [UsageMetric]
    /// False for a hidden provider: the person disabled it, the collector skips it and the
    /// overview omits it. Its other fields keep their last values. Missing in older snapshots.
    var enabled: Bool = true
    /// The provider's own usage page, from the plugin manifest. Details offer it as a link.
    var usageUrl: URL?
    /// Setup steps from the plugin manifest; nil when the plugin supplies none.
    var setup: ProviderSetupGuide?

    /// Collection runs every minute and retries transient failures within each run, so half an
    /// hour without a success means something is wrong (sleep, network, a rejected login). Data
    /// older than this is drawn dimmed, and the widget header shows its age as relative text
    /// rather than as a clock time.
    static let staleInterval: TimeInterval = 30 * 60

    var primaryMetric: UsageMetric? { metrics.first { $0.id == primaryMetricId } }
    var secondaryMetrics: [UsageMetric] { secondaryMetricIds.compactMap { id in metrics.first { $0.id == id } } }
    func isStale(at now: Date) -> Bool { Self.isStale(fetchedAt: fetchedAt, at: now) }
    /// The longest pause the collector ever records; a longer one in a file is not a pause.
    static let maximumPause: TimeInterval = 24 * 3600
    /// True while scheduled attempts are paused after a rate limit, within the collector's bound.
    func isPausing(at now: Date) -> Bool {
        guard let until = nextAttemptAt, until > now, until.timeIntervalSince(now) <= Self.maximumPause,
              let attempted = lastAttemptAt, attempted <= until, until.timeIntervalSince(attempted) <= Self.maximumPause
        else { return false }
        return true
    }

    /// Data that never succeeded counts as stale.
    static func isStale(fetchedAt: Date?, at now: Date) -> Bool {
        guard let fetchedAt else { return true }
        return now.timeIntervalSince(fetchedAt) >= staleInterval
    }

    /// A placeholder entry with no values, like the collector's before a first collection.
    static func unavailable(_ id: ProviderID, name: String? = nil, message: String) -> ProviderSnapshot {
        .init(id: id, name: name ?? id.rawValue.prefix(1).uppercased() + id.rawValue.dropFirst(),
              status: .setup, message: message, fetchedAt: nil, lastAttemptAt: nil, primaryMetricId: nil, metrics: [])
    }

    init(id: ProviderID, name: String, status: FetchStatus, message: String?, fetchedAt: Date?,
         lastAttemptAt: Date?, nextAttemptAt: Date? = nil, primaryMetricId: String?, secondaryMetricIds: [String] = [],
         metrics: [UsageMetric], enabled: Bool = true, usageUrl: URL? = nil, setup: ProviderSetupGuide? = nil) {
        self.id = id
        self.name = name
        self.status = status
        self.message = message
        self.fetchedAt = fetchedAt
        self.lastAttemptAt = lastAttemptAt
        self.nextAttemptAt = nextAttemptAt
        self.primaryMetricId = primaryMetricId
        self.secondaryMetricIds = secondaryMetricIds
        self.metrics = metrics
        self.enabled = enabled
        self.usageUrl = usageUrl
        self.setup = setup
    }
}

extension ProviderSnapshot {
    /// Decodes the optional fields as absent so snapshots from older collectors still read.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(ProviderID.self, forKey: .id)
        name = try container.decode(String.self, forKey: .name)
        status = try container.decode(FetchStatus.self, forKey: .status)
        message = try container.decodeIfPresent(String.self, forKey: .message)
        fetchedAt = try container.decodeIfPresent(Date.self, forKey: .fetchedAt)
        lastAttemptAt = try container.decodeIfPresent(Date.self, forKey: .lastAttemptAt)
        nextAttemptAt = try container.decodeIfPresent(Date.self, forKey: .nextAttemptAt)
        primaryMetricId = try container.decodeIfPresent(String.self, forKey: .primaryMetricId)
        secondaryMetricIds = try container.decodeIfPresent([String].self, forKey: .secondaryMetricIds) ?? []
        metrics = try container.decode([UsageMetric].self, forKey: .metrics)
        enabled = try container.decodeIfPresent(Bool.self, forKey: .enabled) ?? true
        usageUrl = try container.decodeIfPresent(URL.self, forKey: .usageUrl)
        setup = try container.decodeIfPresent(ProviderSetupGuide.self, forKey: .setup)
    }
}

struct UsageSnapshot: Codable, Equatable {
    static let schemaVersion = 2
    /// The number of standard plugins. Layouts tuned for four rows use it as their reference.
    static let standardProviderCount = 4
    var schemaVersion: Int
    var generatedAt: Date
    var providers: [ProviderSnapshot]

    /// A snapshot with no providers, for when no file could be read. `message` is the reason and
    /// is kept so a reader can show it instead of claiming that no plugins are installed.
    static func unavailable(message: String) -> UsageSnapshot {
        var snapshot = UsageSnapshot(schemaVersion: schemaVersion, generatedAt: .distantPast, providers: [])
        snapshot.unavailableMessage = message
        return snapshot
    }

    /// Why the snapshot could not be read, when it could not. Never decoded from a file.
    var unavailableMessage: String?

    private enum CodingKeys: String, CodingKey { case schemaVersion, generatedAt, providers }

    /// Providers the overview and widget show, in snapshot order.
    var visibleProviders: [ProviderSnapshot] { providers.filter(\.enabled) }
    /// Providers the person has hidden. Only a show control mentions them.
    var hiddenProviders: [ProviderSnapshot] { providers.filter { !$0.enabled } }

    /// The contract's bounds, mirrored from the collector so the publisher refuses what the
    /// collector would never write: at most this many metrics per provider, and short printable
    /// text for every string a view shows.
    static let maximumMetrics = 32
    static let maximumTextLength = 200

    /// Short, printable, non-empty: no control characters can reach a label or a log line.
    static func isDisplayText(_ text: String, maximumLength: Int = maximumTextLength) -> Bool {
        !text.isEmpty && text.count <= maximumLength
            && !text.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) || CharacterSet.newlines.contains($0) }
    }

    /// A link the app may hand to the browser: absolute `https` with a host.
    static func isBrowserLink(_ url: URL) -> Bool {
        url.scheme?.lowercased() == "https" && !(url.host ?? "").isEmpty && url.user == nil && url.password == nil
    }

    /// A metric id as the collector writes it: `[a-z][a-z0-9_]{0,63}`.
    static func isMetricID(_ id: String) -> Bool {
        let bytes = Array(id.utf8)
        guard (1...64).contains(bytes.count), (97...122).contains(bytes[0]) else { return false }
        return bytes.allSatisfy { (97...122).contains($0) || (48...57).contains($0) || $0 == 95 }
    }

    func validate() throws {
        guard schemaVersion == Self.schemaVersion else { throw SnapshotError.unsupportedVersion }
        // Ids are unique and ascending, the order the collector writes plugins in.
        let ids = providers.map(\.id)
        guard Set(ids).count == ids.count, ids == ids.sorted() else { throw SnapshotError.invalidProviders }
        for provider in providers {
            let metricIds = Set(provider.metrics.map(\.id))
            guard Self.isDisplayText(provider.name, maximumLength: 32),
                  provider.message.map({ Self.isDisplayText($0) }) ?? true,
                  provider.usageUrl.map(Self.isBrowserLink) ?? true,
                  provider.metrics.count <= Self.maximumMetrics,
                  metricIds.count == provider.metrics.count,
                  provider.primaryMetricId == nil || provider.primaryMetric != nil,
                  Set(provider.secondaryMetricIds).count == provider.secondaryMetricIds.count,
                  provider.secondaryMetricIds.allSatisfy({ metricIds.contains($0) && $0 != provider.primaryMetricId })
            else { throw SnapshotError.invalidMetrics }
            if let setup = provider.setup {
                guard Self.isBrowserLink(setup.installUrl),
                      [setup.installTitle, setup.loginCommand, setup.loginStep, setup.signInAgain, setup.credentialNote]
                          .allSatisfy({ Self.isDisplayText($0) })
                else { throw SnapshotError.invalidMetrics }
            }
            if provider.status == .ok && (provider.fetchedAt == nil || provider.metrics.isEmpty) {
                throw SnapshotError.invalidMetrics
            }
            if !provider.metrics.isEmpty && provider.fetchedAt == nil { throw SnapshotError.invalidMetrics }
            // A success ends any rate-limit pause; the two together would contradict each other.
            if provider.status == .ok && provider.nextAttemptAt != nil { throw SnapshotError.invalidProviders }
            for metric in provider.metrics {
                guard Self.isMetricID(metric.id), Self.isDisplayText(metric.label), Self.isDisplayText(metric.unit),
                      metric.note.map({ Self.isDisplayText($0) }) ?? true else {
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
        // it needs a newer Bars, and one from an older collector (the minutes after an update,
        // before the new collector first publishes) says so, instead of failing as malformed data.
        struct Version: Decodable { var schemaVersion: Int }
        if let version = try? decoder.decode(Version.self, from: data), version.schemaVersion != Self.schemaVersion {
            throw version.schemaVersion < Self.schemaVersion ? SnapshotError.outdatedVersion : SnapshotError.unsupportedVersion
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
    case unsupportedVersion, outdatedVersion, invalidProviders, invalidMetrics, sharedContainerUnavailable
    var errorDescription: String? {
        switch self {
        case .unsupportedVersion: return "This snapshot needs a newer version of Bars."
        case .outdatedVersion: return "Usage data from the previous version of Bars is being replaced. Refresh now if this stays."
        case .invalidProviders: return "The snapshot's provider list is invalid."
        case .invalidMetrics: return "The snapshot contains invalid usage data."
        case .sharedContainerUnavailable: return "Bars cannot locate its published widget data. Check this Mac's user account and installation."
        }
    }
}
