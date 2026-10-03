import SwiftUI

enum BarsPalette {
    // Native system colors adapt to light, dark and increased contrast appearances.
    static let normal = Color(nsColor: .systemGreen)
    static let warning = Color(nsColor: .systemOrange)
    static let exhausted = Color(nsColor: .systemRed)
    static let track = Color.primary.opacity(0.1)
    static let paceMarker = Color.primary.opacity(0.55)

    static func paceText(for pace: UsagePace) -> Color {
        switch pace.standing {
        case .onPace: return .secondary
        case .ahead: return .orange
        case .usedUp: return .red
        }
    }

    static func note(_ note: AllowanceNote) -> Color {
        switch note {
        case .aheadOfPace, .previousPeriod: return .orange
        case .usedUpEarly: return .red
        }
    }

    static func fill(for metric: UsageMetric) -> Color {
        switch UsagePresentation.severity(for: metric) {
        case .normal: return normal
        case .warning: return warning
        case .exhausted: return exhausted
        case nil: return .secondary
        }
    }
}

extension FetchStatus {
    var symbolName: String { self == .loginRequired ? "key.fill" : "exclamationmark.circle" }
}

struct UsageBar: View {
    let metric: UsageMetric
    var stale = false
    /// Draws a marker where the fill would sit if usage were spread evenly across the period.
    var pace: UsagePace?
    var height: CGFloat = Self.primaryHeight

    static let primaryHeight: CGFloat = 5
    /// A secondary allowance's mini bar, which also gets a thinner, shorter pace marker.
    static let miniHeight: CGFloat = 3
    private var isMini: Bool { height < Self.primaryHeight }

    var body: some View {
        if let fill = metric.fill {
            GeometryReader { proxy in
                ZStack(alignment: .leading) {
                    Capsule().fill(BarsPalette.track)
                    Capsule().fill(BarsPalette.fill(for: metric).opacity(stale ? 0.5 : 1))
                        .frame(width: proxy.size.width * fill)
                    if let pace {
                        // Clamp inside the track so the marker stays visible at both ends.
                        let x = min(max(proxy.size.width * pace.elapsedShare, 1), proxy.size.width - 1)
                        RoundedRectangle(cornerRadius: 1)
                            .fill(BarsPalette.paceMarker)
                            .frame(width: isMini ? 1.5 : 2, height: proxy.size.height + (isMini ? 3 : 4))
                            .position(x: x, y: proxy.size.height / 2)
                    }
                }
            }
            .frame(height: height)
            // Shapes are not accessibility elements on their own; make the bar one.
            .accessibilityElement()
            .accessibilityLabel(metric.label)
            .accessibilityValue(UsagePresentation.barAccessibilityValue(fill: fill, pace: pace))
        }
    }
}

/// One overview row. Every provider uses the same grammar:
/// 1. name, primary allowance label, and its own age or status only when it deviates;
/// 2. used and left amounts; 3. bar with pace marker; 4. pace; 5. reset context;
/// 6. secondary allowances as labeled mini bars.
/// The compact widget layout puts the pace and reset on one line to fit four providers.
/// A provider that is not set up shows `Not set up` and the collector's message instead of 2–6.
/// In the dropdown, a row needing setup or sign-in ends with a tertiary hint to open its details.
struct ProviderRow: View {
    let provider: ProviderSnapshot
    let now: Date
    var compact = false
    /// The consolidated freshness shown in the header. Without it every row shows its own age.
    var latestFetch: Date?

    private var captionSize: CGFloat { compact ? 9 : 11 }

    var body: some View {
        VStack(alignment: .leading, spacing: compact ? 2 : 4) {
            titleLine

            if let metric = provider.primaryMetric {
                amountLine(metric)
                let pace = UsagePresentation.pace(for: metric, provider: provider, at: now)
                if metric.fill != nil {
                    UsageBar(metric: metric, stale: provider.isStale(at: now), pace: pace)
                        .padding(.vertical, compact ? 1 : 2)
                }
                paceAndContext(metric, pace: pace)
                ForEach(UsagePresentation.secondaryAllowances(for: provider, now: now)) { allowance in
                    SecondaryAllowanceRow(allowance: allowance, stale: provider.isStale(at: now), compact: compact)
                        .padding(.top, compact ? 0 : 2)
                }
            } else if UsagePresentation.isNotSetUp(provider) {
                notSetUp
            } else {
                Text(provider.message ?? (provider.status == .ok ? "Preferred allowance unavailable" : provider.status.label))
                    .font(.system(size: compact ? 11 : 13))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .minimumScaleFactor(0.8)
            }

            // The widget has no room for the hint, and its rows open the same details anyway.
            if !compact, let action = UsagePresentation.callToAction(for: provider) {
                Text(action.rowHint)
                    .font(.system(size: captionSize))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    /// `Not set up`, then the collector's explanation. The compact widget shares one line.
    @ViewBuilder private var notSetUp: some View {
        let label = Text("Not set up").fontWeight(.medium).foregroundStyle(.secondary)
        if compact {
            Text("\(label)\(Text(provider.message.map { " · \($0)" } ?? "").foregroundStyle(.tertiary))")
                .font(.system(size: 11))
                .lineLimit(2)
                .minimumScaleFactor(0.8)
        } else {
            label.font(.system(size: 13))
            if let message = provider.message {
                Text(message)
                    .font(.system(size: captionSize))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var titleLine: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(provider.name)
                .font(.system(size: compact ? 13 : 15, weight: .semibold))
                .lineLimit(1)
                .layoutPriority(3)
            // A compact row has no room for both the allowance label and a status with its age.
            if let label = provider.primaryMetric?.label, !(compact && provider.status != .ok) {
                Text(label)
                    .font(.system(size: compact ? 10 : 12))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .layoutPriority(1)
            }
            Spacer(minLength: 6)
            // A row that is not set up already says so beneath the name.
            if UsagePresentation.showsOwnAge(provider, latestFetch: latestFetch), !UsagePresentation.isNotSetUp(provider) {
                deviation
                    .font(.system(size: compact ? 10 : 11))
                    // Secondary rather than tertiary: a deviating row is the thing to notice.
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                    .layoutPriority(2)
            }
        }
    }

    /// Status symbol and label when the latest attempt failed, then the data age.
    @ViewBuilder private var deviation: some View {
        let status = provider.status
        if let fetchedAt = provider.fetchedAt {
            let age = compact
                // WidgetKit can delay timeline reloads. Relative text keeps aging itself.
                ? Text("\(Text(fetchedAt, style: .relative)) ago")
                : Text(UsagePresentation.age(since: fetchedAt, now: now))
            if status == .ok {
                age
            } else {
                Text("\(Image(systemName: status.symbolName)) \(status.label) · \(age)")
            }
        } else if status == .ok {
            Text(UsagePresentation.age(since: nil, now: now))
        } else if status == .setup && provider.lastAttemptAt == nil {
            // The collector's first-run placeholder: nothing has been tried yet, so "Setup
            // required" would be a guess, and the row's message already says it is waiting.
            EmptyView()
        } else {
            Text("\(Image(systemName: status.symbolName)) \(status.label)")
        }
    }

    private func amountLine(_ metric: UsageMetric) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            if let used = metric.used {
                Text("\(UsagePresentation.amount(used, unit: metric.unit)) used")
                    .layoutPriority(1)
            }
            if let remaining = metric.remaining {
                if metric.used != nil { Spacer(minLength: 0) }
                Text("\(UsagePresentation.amount(remaining, unit: metric.unit)) left")
                    .fontWeight(metric.kind == .balance ? .medium : .regular)
                    .layoutPriority(1)
            }
            if metric.used == nil && metric.remaining == nil { Text("Amount unavailable") }
        }
        .font(.system(size: compact ? 11 : 13))
        .monospacedDigit()
        .lineLimit(1)
        .minimumScaleFactor(0.8)
    }

    @ViewBuilder private func paceAndContext(_ metric: UsageMetric, pace: UsagePace?) -> some View {
        let paceLine = pace.flatMap { pace in
            metric.allowancePeriod.map { period in
                Text(UsagePresentation.paceCaption(for: pace, period: period))
                    .foregroundStyle(BarsPalette.paceText(for: pace))
            }
        }
        if compact, let paceLine {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                paceLine.layoutPriority(2)
                Spacer(minLength: 0)
                context(metric).layoutPriority(1)
            }
            .font(.system(size: captionSize))
            .lineLimit(1)
            .minimumScaleFactor(0.8)
        } else {
            if let paceLine {
                paceLine
                    .font(.system(size: captionSize))
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            context(metric)
                .font(.system(size: captionSize))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
        }
    }

    /// Reset text, or why there is none, followed by previous-period or overage warnings.
    private func context(_ metric: UsageMetric) -> some View {
        HStack(spacing: 4) {
            Text(UsagePresentation.contextCaption(for: metric, provider: provider, now: now))
                .foregroundStyle(.tertiary)
            if UsagePresentation.isPreviousPeriod(for: metric, provider: provider, at: now) {
                Text("Previous period").foregroundStyle(.orange)
            } else if let overage = metric.overage {
                Text("\(UsagePresentation.amount(overage, unit: metric.unit)) over").foregroundStyle(.red)
            }
        }
    }
}

/// A secondary allowance: label, optional reset and attention tag, value, and a 3 pt mini bar.
struct SecondaryAllowanceRow: View {
    let allowance: SecondaryAllowance
    var stale = false
    var compact = false

    var body: some View {
        VStack(alignment: .leading, spacing: compact ? 2 : 3) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                caption
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                    .layoutPriority(1)
                Spacer(minLength: 0)
                Text(allowance.value)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .layoutPriority(2)
            }
            .font(.system(size: compact ? 9 : 11))
            UsageBar(metric: allowance.metric, stale: stale, pace: allowance.pace, height: UsageBar.miniHeight)
        }
        .accessibilityElement(children: .combine)
    }

    private var caption: Text {
        let label = Text(allowance.label).foregroundStyle(.tertiary)
        let reset = allowance.reset.map { Text(" · \($0)").foregroundStyle(.tertiary) } ?? Text("")
        guard let note = allowance.note else { return Text("\(label)\(reset)") }
        let tag = Text(" · \(note.text)").fontWeight(.medium).foregroundStyle(BarsPalette.note(note))
        return Text("\(label)\(reset)\(tag)")
    }
}
