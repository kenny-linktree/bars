import SwiftUI

enum DropdownLayout {
    /// The dropdown popover's fixed size. A fixed size keeps the popover from jumping as rows load
    /// or change; taller content scrolls. Defined here rather than in BarsApp.swift so the preview
    /// harness, which cannot compile that file, renders against the same value.
    static let size = CGSize(width: 440, height: 700)
}

extension ProviderID {
    var usageURL: URL {
        let address: String
        switch self {
        case .claude: address = "https://claude.ai/settings/usage"
        case .codex: address = "https://chatgpt.com/codex/cloud/settings/analytics#usage"
        case .cursor: address = "https://cursor.com/dashboard?tab=usage"
        case .devin: address = "https://app.devin.ai/settings/usage"
        }
        return URL(string: address)!
    }
}

struct ProviderDetails: View {
    let provider: ProviderSnapshot
    let now: Date
    @ObservedObject var model: BarsModel

    var body: some View {
        let groups = UsagePresentation.scopeGroups(for: provider)
        let sharedReset = UsagePresentation.sharedResetCaption(for: provider)
        let action = UsagePresentation.callToAction(for: provider)
        VStack(alignment: .leading, spacing: 14) {
            header

            // Setup steps replace the metric cards; with retained values they precede them.
            if action == .setUp {
                ProviderSetupSection(provider: provider) { model.setHidden(true, provider: provider.id) }
            } else if provider.metrics.isEmpty {
                Text(provider.enabled ? "No successful usage data yet. Refresh after signing in to this provider."
                                      : "No usage data was collected before \(provider.name) was hidden.")
                    .foregroundStyle(.secondary)
            }

            if let sharedReset {
                Label(sharedReset, systemImage: "arrow.clockwise")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            ForEach(groups) { group in
                VStack(alignment: .leading, spacing: 10) {
                    // Section headers carry the scope when allowances differ in scope.
                    if groups.count > 1 {
                        Text(group.scope.label)
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(.secondary)
                    }
                    ForEach(group.metrics) { metric in
                        MetricCard(metric: metric, provider: provider, now: now, showsReset: sharedReset == nil)
                        Divider()
                    }
                }
            }

            if let footnote = UsagePresentation.scopeFootnote(for: provider) {
                Text(footnote)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Link(destination: provider.id.usageURL) {
                HStack(spacing: 3) {
                    Text("Open \(provider.name) usage")
                    Image(systemName: "arrow.up.right")
                }
            }
            .font(.callout)
            .accessibilityLabel("Open \(provider.name) usage, opens in your browser")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(provider.name).font(.title2.weight(.semibold))
                    Text(UsagePresentation.detailsFreshness(fetchedAt: provider.fetchedAt, now: now))
                        .font(.callout)
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
                Spacer()
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    ProviderVisibilityButton(provider: provider, model: model)
                    // The collector skips a hidden provider, so refreshing it would do nothing.
                    if provider.enabled {
                        Button(model.isRefreshing ? "Refreshing…" : "Refresh now") { model.refresh(provider: provider.id) }
                            .disabled(model.isRefreshing)
                            .accessibilityHint("Collects \(provider.name) usage")
                    }
                }
            }
            if !provider.enabled {
                Label("Hidden from the overview and widget. Bars does not collect \(provider.name) until you show it again.",
                      systemImage: "eye.slash")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // The setup section carries the collector's message when there is nothing retained. A
            // hidden provider that was never attempted is not waiting for anything, so it has no banner.
            if provider.status != .ok && !UsagePresentation.isNotSetUp(provider)
                && (provider.enabled || provider.lastAttemptAt != nil) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    // The status label beside the symbol already says what it means.
                    Image(systemName: provider.status.symbolName).accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(provider.status.label).font(.headline)
                        if let message = provider.message {
                            Text(message)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Spacer(minLength: 0)
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 8).fill(.quaternary))
                .accessibilityElement(children: .combine)
            }
            if UsagePresentation.callToAction(for: provider) == .signIn {
                SignInAgainBox(provider: provider)
            }
            if let note = UsagePresentation.failedAttemptNote(for: provider, now: now) {
                Text(note).font(.callout).foregroundStyle(.secondary)
            }
        }
    }
}

/// One allowance: label and used share, bar with pace marker, pace line, and a definition grid.
private struct MetricCard: View {
    let metric: UsageMetric
    let provider: ProviderSnapshot
    let now: Date
    /// False when every allowance shares one reset, which the details header then states once.
    let showsReset: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if UsagePresentation.isBalanceOnly(metric) {
                balance
            } else {
                allowance
            }
        }
    }

    /// Devin's balance has no total or used amount, so it has no bar and no grid.
    private var balance: some View {
        Group {
            HStack(alignment: .firstTextBaseline) {
                Text(metric.label).font(.headline)
                Spacer()
                Text(metric.remaining.map { "\(UsagePresentation.amount($0, unit: metric.unit)) left" }
                     ?? "Amount not reported")
                    .font(.headline.weight(.medium))
                    .monospacedDigit()
            }
            Text(UsagePresentation.balanceNote(for: metric, provider: provider))
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    @ViewBuilder private var allowance: some View {
        let pace = UsagePresentation.pace(for: metric, provider: provider, at: now)
        let previous = UsagePresentation.isPreviousPeriod(for: metric, provider: provider, at: now)
        HStack(alignment: .firstTextBaseline) {
            Text(metric.label).font(.headline)
            Spacer()
            if let share = UsagePresentation.percentUsed(metric) {
                Text("\(share) used")
                    .font(.callout)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
        }
        if metric.fill != nil {
            UsageBar(metric: metric, stale: provider.isStale(at: now), pace: pace)
        }
        if let pace, let period = metric.allowancePeriod {
            Text(UsagePresentation.detailsPaceCaption(for: pace, period: period))
                .font(.caption)
                .foregroundStyle(BarsPalette.paceText(for: pace))
        }
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 3) {
            if let used = metric.used { row("Used", UsagePresentation.amount(used, unit: metric.unit)) }
            if let remaining = metric.remaining { row("Left", UsagePresentation.amount(remaining, unit: metric.unit)) }
            // A percentage quota's limit is always 100%, so it adds nothing.
            if let limit = metric.limit, metric.unit != "%" {
                row("Limit", UsagePresentation.amount(limit, unit: metric.unit))
            }
            if showsReset {
                row(previous ? "Reset" : "Resets",
                    UsagePresentation.fullResetText(for: metric, provider: provider) ?? "Not reported")
            }
        }
        .font(.callout)
        if let overage = metric.overage {
            Text("\(UsagePresentation.amount(overage, unit: metric.unit)) over the allowance")
                .font(.caption).foregroundStyle(.red)
        }
        if previous {
            Text("Previous period").font(.caption).foregroundStyle(.orange)
        }
    }

    private func row(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(label).foregroundStyle(.tertiary)
            Text(value)
                .monospacedDigit()
                .frame(maxWidth: .infinity, alignment: .trailing)
        }
    }
}
