import Foundation

enum UsageSeverity: Equatable { case normal, warning, exhausted }

/// How consumption compares with the time elapsed in the allowance period, as of the last
/// successful fetch. Shares are fractions of the limit and of the period respectively.
struct UsagePace: Equatable {
    enum Standing: Equatable { case onPace, ahead, usedUp }

    var usedShare: Double
    var elapsedShare: Double
    var periodStart: Date
    var periodEnd: Date
    /// When the allowance would be exhausted if the fetch-time rate continued. Nil without usage.
    var projectedExhaustion: Date?

    var standing: Standing {
        if usedShare >= 1 { return .usedUp }
        return usedShare > elapsedShare ? .ahead : .onPace
    }
}

/// A short attention tag for a secondary allowance. Absent when nothing needs attention.
enum AllowanceNote: Equatable {
    case aheadOfPace, usedUpEarly, previousPeriod

    var text: String {
        switch self {
        case .aheadOfPace: return "ahead of pace"
        case .usedUpEarly: return "used up early"
        case .previousPeriod: return "previous period"
        }
    }
}

/// One labeled mini bar beneath a provider's primary bar on the overview.
struct SecondaryAllowance: Equatable, Identifiable {
    var metric: UsageMetric
    /// `<used> / <limit> <unit>`, or `<used>%` for percentage quotas.
    var value: String
    /// Short reset text, omitted when it repeats the primary allowance's reset.
    var reset: String?
    var note: AllowanceNote?
    var pace: UsagePace?

    var id: String { metric.id }
    var label: String { metric.label }
    /// The leading text of the sub-row: label, reset and attention tag.
    var caption: String { [label, reset, note?.text].compactMap { $0 }.joined(separator: " · ") }
}

/// What the person must do before Bars can collect a provider again.
enum ProviderCallToAction: Equatable {
    /// The collector found no login material or tool: show the setup steps.
    case setUp
    /// A login exists but was rejected or unreadable: show how to sign in again.
    case signIn

    /// The tertiary hint an overview row in the dropdown adds.
    var rowHint: String {
        switch self {
        case .setUp: return "Click for setup steps"
        case .signIn: return "Click to see how to sign in"
        }
    }
}

/// Metrics that share an allowance scope, in the order the provider reported them.
struct ScopeGroup: Equatable, Identifiable {
    var scope: AllowanceScope
    var metrics: [UsageMetric]
    var id: String { scope.rawValue }
}

enum UsagePresentation {
    private static let numberFormatters = AmountNumberFormatters()
    /// Used shares of the limit at which a bar turns from normal to warning, then to exhausted.
    static let warningShare = 0.7
    static let exhaustedShare = 0.9

    static func severity(for metric: UsageMetric) -> UsageSeverity? {
        guard let fill = metric.fill else { return nil }
        if fill >= exhaustedShare { return .exhausted }
        if fill >= warningShare { return .warning }
        return .normal
    }

    // MARK: - Amounts

    /// A source currency code such as `USD`. Its code is always printed, never a symbol.
    static func isCurrencyCode(_ unit: String) -> Bool {
        unit.count == 3 && unit.unicodeScalars.allSatisfy { $0.isASCII && CharacterSet.uppercaseLetters.contains($0) }
    }

    /// The formatted number alone: currencies with exactly two decimals, percentages with at
    /// most one, and every other unit (such as credits) as a whole number.
    static func number(_ value: Double, unit: String, locale: Locale = .current) -> String {
        let digits = unit == "%" ? 1 : (isCurrencyCode(unit) ? 2 : 0)
        return numberFormatters.string(value, digits: digits, locale: locale)
    }

    static func amount(_ value: Double, unit: String, locale: Locale = .current) -> String {
        let number = number(value, unit: unit, locale: locale)
        if unit == "%" { return "\(number)%" }
        // Include the currency code so a dollar sign never obscures the source unit.
        return "\(number) \(unit)"
    }

    /// The used share of a proportional allowance, such as "9%". Nil without a fill.
    static func percentUsed(_ metric: UsageMetric) -> String? {
        metric.fill.map(percent)
    }

    /// The compact value of a secondary allowance.
    static func allowanceValue(_ metric: UsageMetric, locale: Locale = .current) -> String {
        if metric.unit == "%", let used = metric.used { return amount(used, unit: "%", locale: locale) }
        if let used = metric.used, let limit = metric.limit {
            return "\(number(used, unit: metric.unit, locale: locale)) / \(amount(limit, unit: metric.unit, locale: locale))"
        }
        if let used = metric.used { return "\(amount(used, unit: metric.unit, locale: locale)) used" }
        if let remaining = metric.remaining { return "\(amount(remaining, unit: metric.unit, locale: locale)) left" }
        return "Not reported"
    }

    // MARK: - Data age

    static func age(since fetchedAt: Date?, now: Date) -> String {
        guard let fetchedAt else { return "Never updated" }
        let seconds = max(0, now.timeIntervalSince(fetchedAt))
        if seconds < 60 { return "Just now" }
        if seconds < 3_600 { return "\(Int(seconds / 60))m ago" }
        if seconds < 86_400 { return "\(Int(seconds / 3_600))h \(Int(seconds.truncatingRemainder(dividingBy: 3_600) / 60))m ago" }
        return "\(Int(seconds / 86_400))d \(Int(seconds.truncatingRemainder(dividingBy: 86_400) / 3_600))h ago"
    }

    /// The most recent successful fetch across shown providers: the consolidated freshness.
    /// A hidden provider is no longer collected, so its retained time never stands for the rest.
    static func latestFetch(in snapshot: UsageSnapshot) -> Date? {
        snapshot.visibleProviders.compactMap(\.fetchedAt).max()
    }

    /// Fetches this close to the latest one count as the same collection run. Providers in one run
    /// finish within seconds of each other, and runs are 15 minutes apart.
    static let sharedFetchTolerance: TimeInterval = 60

    /// Resets this close together are the same boundary. Providers report one boundary per
    /// allowance, with a few seconds of skew between them.
    static let sharedResetTolerance: TimeInterval = 60

    /// A row shows its own age or status only when it deviates from the consolidated freshness:
    /// it never succeeded, its latest attempt failed, or its data comes from a different run.
    static func showsOwnAge(_ provider: ProviderSnapshot, latestFetch: Date?) -> Bool {
        guard let fetchedAt = provider.fetchedAt, provider.status == .ok, let latestFetch else { return true }
        return abs(fetchedAt.timeIntervalSince(latestFetch)) > sharedFetchTolerance
    }

    /// "Updated 14m ago", "Updated just now" or "Never updated".
    static func updatedText(since fetchedAt: Date?, now: Date) -> String {
        guard let fetchedAt else { return "Never updated" }
        if now.timeIntervalSince(fetchedAt) < 60 { return "Updated just now" }
        return "Updated \(age(since: fetchedAt, now: now))"
    }

    static func isStale(latestFetch: Date?, now: Date) -> Bool {
        ProviderSnapshot.isStale(fetchedAt: latestFetch, at: now)
    }

    /// A clock time, with the date when it is not today.
    static func clockText(_ date: Date, now: Date, seconds: Bool, locale: Locale = .current,
                          timeZone: TimeZone = .current) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        var style = Date.FormatStyle(locale: locale, timeZone: timeZone).hour().minute()
        if seconds { style = style.second() }
        let time = date.formatted(style)
        guard !calendar.isDate(date, inSameDayAs: now) else { return time }
        let day = date.formatted(Date.FormatStyle(locale: locale, timeZone: timeZone).month(.abbreviated).day())
        return "\(day), \(time)"
    }

    /// Details header: "Updated 14m ago · 11:45:04 AM".
    static func detailsFreshness(fetchedAt: Date?, now: Date, locale: Locale = .current,
                                 timeZone: TimeZone = .current) -> String {
        guard let fetchedAt else { return "Never updated" }
        let clock = clockText(fetchedAt, now: now, seconds: true, locale: locale, timeZone: timeZone)
        return "\(updatedText(since: fetchedAt, now: now)) · \(clock)"
    }

    /// Mentions the latest attempt only when it is not the successful fetch already shown.
    static func failedAttemptNote(for provider: ProviderSnapshot, now: Date, locale: Locale = .current,
                                  timeZone: TimeZone = .current) -> String? {
        guard let attempted = provider.lastAttemptAt else { return nil }
        if let fetched = provider.fetchedAt,
           abs(attempted.timeIntervalSince(fetched)) <= sharedFetchTolerance { return nil }
        let clock = clockText(attempted, now: now, seconds: false, locale: locale, timeZone: timeZone)
        return "Last attempt \(clock) did not succeed"
    }

    // MARK: - Resets

    static func resetDate(for metric: UsageMetric, provider: ProviderSnapshot) -> Date? {
        if let reported = metric.resetsAt { return reported }
        guard provider.id == .claude, metric.id == "monthly_spend",
              let fetched = provider.fetchedAt else { return nil }
        // Estimate from the documented UTC calendar month containing the data.
        // Anchoring to the successful fetch keeps stale data in its original period.
        // https://support.claude.com/en/articles/14782391-claude-enterprise-consumption-guide
        return Calendar.utcGregorian.dateInterval(of: .month, for: fetched)?.end
    }

    static func isPreviousPeriod(for metric: UsageMetric, provider: ProviderSnapshot, at now: Date) -> Bool {
        guard let reset = resetDate(for: metric, provider: provider), let fetched = provider.fetchedAt else {
            return false
        }
        return reset <= now && fetched < reset
    }

    /// Full reset date for details: "Oct 31 at 5:00 PM PDT (est.)". Nil when no reset is known.
    static func fullResetText(for metric: UsageMetric, provider: ProviderSnapshot, locale: Locale = .current,
                              timeZone: TimeZone = .current) -> String? {
        guard let reset = resetDate(for: metric, provider: provider) else { return nil }
        return fullResetText(reset, estimated: metric.resetsAt == nil, locale: locale, timeZone: timeZone)
    }

    private static func fullResetText(_ reset: Date, estimated: Bool, locale: Locale, timeZone: TimeZone) -> String {
        let date = reset.formatted(Date.FormatStyle(locale: locale, timeZone: timeZone)
            .month(.abbreviated).day().hour().minute().timeZone(.specificName(.short)))
        return date + (estimated ? " (est.)" : "")
    }

    /// Overview reset text sized to the period: a time for periods up to a day ("resets 1:00 AM"),
    /// a weekday and time for a week ("resets Sun 1:00 AM"), otherwise a date ("Resets Oct 31").
    static func shortResetCaption(for metric: UsageMetric, provider: ProviderSnapshot, now: Date,
                                  capitalized: Bool = true, locale: Locale = .current,
                                  timeZone: TimeZone = .current) -> String? {
        guard let reset = resetDate(for: metric, provider: provider) else { return nil }
        var style = Date.FormatStyle(locale: locale, timeZone: timeZone)
        switch metric.allowancePeriod {
        case .fiveHours?, .day?: style = style.hour().minute()
        case .week?: style = style.weekday(.abbreviated).hour().minute()
        case .month?, nil: style = style.month(.abbreviated).day()
        }
        var verb = isPreviousPeriod(for: metric, provider: provider, at: now) ? "reset" : "resets"
        if capitalized { verb = verb.prefix(1).uppercased() + verb.dropFirst() }
        let estimate = metric.resetsAt == nil ? " (est.)" : ""
        return "\(verb) \(reset.formatted(style))\(estimate)"
    }

    /// The overview context line: the reset, or why there is none.
    static func contextCaption(for metric: UsageMetric, provider: ProviderSnapshot, now: Date,
                               locale: Locale = .current, timeZone: TimeZone = .current) -> String {
        if let reset = shortResetCaption(for: metric, provider: provider, now: now, locale: locale, timeZone: timeZone) {
            return reset
        }
        if metric.kind == .balance, metric.scope == .team, metric.allowancePeriod == nil {
            return "Shared team balance · no reset"
        }
        return "Reset not reported"
    }

    /// The reset instant shared by every allowance, when there is more than one allowance and
    /// all of them reset together (within a minute). Details then state it once.
    static func sharedReset(for provider: ProviderSnapshot) -> Date? {
        let resets = provider.metrics.map { resetDate(for: $0, provider: provider) }
        guard resets.count > 1, let first = resets[0],
              resets.allSatisfy({ $0.map { abs($0.timeIntervalSince(first)) <= sharedResetTolerance } ?? false })
        else { return nil }
        return first
    }

    /// "All allowances reset Oct 30 at 9:43 AM PDT".
    static func sharedResetCaption(for provider: ProviderSnapshot, locale: Locale = .current,
                                   timeZone: TimeZone = .current) -> String? {
        guard let reset = sharedReset(for: provider) else { return nil }
        let estimated = provider.metrics.contains { $0.resetsAt == nil }
        return "All allowances reset \(fullResetText(reset, estimated: estimated, locale: locale, timeZone: timeZone))"
    }

    // MARK: - Pace

    /// Pace needs a known limit, usage, period length, reset instant and fetch time. Data retained
    /// from an ended period has no pace, because the comparison would describe a finished period.
    static func pace(for metric: UsageMetric, provider: ProviderSnapshot, at now: Date) -> UsagePace? {
        guard let used = metric.used, let limit = metric.limit, limit > 0,
              let period = metric.allowancePeriod, let fetched = provider.fetchedAt,
              let end = resetDate(for: metric, provider: provider),
              let start = period.start(endingAt: end), start < end,
              now < end
        else { return nil }
        let length = end.timeIntervalSince(start)
        let elapsed = min(max(fetched.timeIntervalSince(start), 0), length)
        var projected: Date?
        if used > 0 {
            projected = start.addingTimeInterval(elapsed * (limit / used))
        }
        return UsagePace(usedShare: used / limit, elapsedShare: elapsed / length,
                         periodStart: start, periodEnd: end, projectedExhaustion: projected)
    }

    static func percent(_ share: Double) -> String {
        "\(Int((min(max(share, 0), 1) * 100).rounded()))%"
    }

    /// What VoiceOver reads for a bar: the used share, rounded like the visible text, and the
    /// elapsed share that the pace marker shows.
    static func barAccessibilityValue(fill: Double, pace: UsagePace?) -> String {
        var value = "\(percent(fill)) of allowance used"
        if let pace { value += ", \(percent(pace.elapsedShare)) of period elapsed" }
        return value
    }

    /// One line answering whether the remaining amount lasts until the reset at the current rate.
    static func paceCaption(for pace: UsagePace, period: AllowancePeriod, locale: Locale = .current,
                            timeZone: TimeZone = .current) -> String {
        switch pace.standing {
        case .usedUp:
            return "Used up · \(percent(1 - pace.elapsedShare)) of period left"
        case .ahead:
            guard let projected = pace.projectedExhaustion else { return "Ahead of pace" }
            var style = Date.FormatStyle(locale: locale, timeZone: timeZone)
            style = period.isWithinOneDay ? style.hour().minute() : style.month(.abbreviated).day()
            return "Ahead of pace · runs out ~\(projected.formatted(style)) at this rate"
        case .onPace:
            return "On pace · \(percent(pace.elapsedShare)) of period elapsed"
        }
    }

    /// The details pace line also states the elapsed share when usage is ahead of pace.
    static func detailsPaceCaption(for pace: UsagePace, period: AllowancePeriod, locale: Locale = .current,
                                   timeZone: TimeZone = .current) -> String {
        let caption = paceCaption(for: pace, period: period, locale: locale, timeZone: timeZone)
        guard pace.standing == .ahead else { return caption }
        return "\(caption) · \(percent(pace.elapsedShare)) of period elapsed"
    }

    static func allowanceNote(for metric: UsageMetric, provider: ProviderSnapshot, at now: Date) -> AllowanceNote? {
        if isPreviousPeriod(for: metric, provider: provider, at: now) { return .previousPeriod }
        switch pace(for: metric, provider: provider, at: now)?.standing {
        case .ahead?: return .aheadOfPace
        case .usedUp?: return .usedUpEarly
        case .onPace?, nil: return nil
        }
    }

    // MARK: - Secondary allowances

    static func secondaryMetrics(for provider: ProviderSnapshot) -> [UsageMetric] {
        switch provider.id {
        case .devin: return provider.metrics.filter { $0.kind == .quota }
        case .cursor: return provider.metrics.filter { ["individual_on_demand", "team_on_demand"].contains($0.id) }
        case .claude, .codex:
            // Quota-only accounts show the other general window without repeating the
            // primary bar. Model-specific limits stay in details to keep the widget compact.
            guard provider.primaryMetric?.kind == .quota else { return [] }
            let windows = provider.id == .claude ? ["five_hour", "seven_day"]
                : ["primary_window", "secondary_window"]
            return provider.metrics.filter { windows.contains($0.id) && $0.id != provider.primaryMetricId }
        }
    }

    static func secondaryAllowances(for provider: ProviderSnapshot, now: Date, locale: Locale = .current,
                                    timeZone: TimeZone = .current) -> [SecondaryAllowance] {
        let primaryReset = provider.primaryMetric.flatMap { resetDate(for: $0, provider: provider) }
        return secondaryMetrics(for: provider).map { metric in
            var reset = shortResetCaption(for: metric, provider: provider, now: now, capitalized: false,
                                          locale: locale, timeZone: timeZone)
            if let primaryReset, let own = resetDate(for: metric, provider: provider),
               abs(own.timeIntervalSince(primaryReset)) <= sharedResetTolerance {
                reset = nil
            }
            return SecondaryAllowance(metric: metric, value: allowanceValue(metric, locale: locale), reset: reset,
                                      note: allowanceNote(for: metric, provider: provider, at: now),
                                      pace: pace(for: metric, provider: provider, at: now))
        }
    }

    // MARK: - Calls to action and hidden providers

    /// Setup steps follow a collector verdict only. The placeholder entry before the first
    /// collection also has `setup` status, but no attempt time, and says nothing about logins.
    static func callToAction(for provider: ProviderSnapshot) -> ProviderCallToAction? {
        switch provider.status {
        case .setup: return provider.lastAttemptAt == nil ? nil : .setUp
        case .loginRequired: return .signIn
        case .ok, .error: return nil
        }
    }

    /// A provider the collector reports as not set up, with nothing retained to show instead.
    static func isNotSetUp(_ provider: ProviderSnapshot) -> Bool {
        callToAction(for: provider) == .setUp && provider.primaryMetric == nil
    }

    /// "Hidden: Cursor, Devin", or nil when every provider is shown.
    static func hiddenSummary(for snapshot: UsageSnapshot) -> String? {
        let names = snapshot.hiddenProviders.map(\.name)
        return names.isEmpty ? nil : "Hidden: \(names.joined(separator: ", "))"
    }

    // MARK: - Details structure

    static func scopeGroups(for provider: ProviderSnapshot) -> [ScopeGroup] {
        var groups: [ScopeGroup] = []
        for metric in provider.metrics {
            if let index = groups.firstIndex(where: { $0.scope == metric.scope }) {
                groups[index].metrics.append(metric)
            } else {
                groups.append(ScopeGroup(scope: metric.scope, metrics: [metric]))
            }
        }
        return groups
    }

    /// With a single scope there are no section headers, so a footnote states the scope instead.
    static func scopeFootnote(for provider: ProviderSnapshot) -> String? {
        let groups = scopeGroups(for: provider)
        guard groups.count == 1 else { return nil }
        let plural = groups[0].metrics.count > 1
        switch groups[0].scope {
        case .unknown:
            return "\(provider.name) does not report whether \(plural ? "these allowances are" : "this allowance is") personal or organizational."
        case .individual:
            return "\(provider.name) reports \(plural ? "these as individual allowances" : "this as an individual allowance")."
        case .team:
            return "\(provider.name) reports \(plural ? "these as team allowances" : "this as a team allowance")."
        }
    }

    /// A balance with neither a total nor a used amount has no bar, only its remaining amount.
    static func isBalanceOnly(_ metric: UsageMetric) -> Bool {
        metric.kind == .balance && metric.used == nil && metric.limit == nil
    }

    static func balanceNote(for metric: UsageMetric, provider: ProviderSnapshot) -> String {
        let shared = metric.scope == .team ? "Shared team balance. " : ""
        if provider.id == .devin {
            return "\(shared)Devin reports only the remaining amount; purchased credits do not expire, so there is no reset."
        }
        return "\(shared)\(provider.name) reports only the remaining amount."
    }
}

/// Reuses at most three number formats for the last requested locale. Locale changes discard
/// the old formats, and the lock protects both reconfiguration and NumberFormatter use.
private final class AmountNumberFormatters: @unchecked Sendable {
    private let lock = NSLock()
    private var locale: Locale?
    private var formatters: [Int: NumberFormatter] = [:]

    func string(_ value: Double, digits: Int, locale requestedLocale: Locale) -> String {
        lock.lock()
        defer { lock.unlock() }
        if locale != requestedLocale {
            formatters.removeAll(keepingCapacity: true)
            locale = requestedLocale
        }
        let formatter: NumberFormatter
        if let existing = formatters[digits] {
            formatter = existing
        } else {
            formatter = NumberFormatter()
            formatter.locale = requestedLocale
            formatter.numberStyle = .decimal
            formatter.minimumFractionDigits = digits == 2 ? 2 : 0
            formatter.maximumFractionDigits = digits
            formatters[digits] = formatter
        }
        return formatter.string(from: NSNumber(value: value)) ?? String(value)
    }
}
