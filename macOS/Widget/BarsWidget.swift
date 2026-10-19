import SwiftUI
import WidgetKit

struct BarsEntry: TimelineEntry {
    let date: Date
    let snapshot: UsageSnapshot
}

struct BarsTimelineProvider: TimelineProvider {
    /// WidgetKit's own fallback reload. Collection runs every minute, but the refresh runner asks
    /// for a reload only when values changed (at most every five minutes) or every fifteen minutes,
    /// because WidgetKit budgets reloads; this matters only when such a request is missed.
    static let fallbackReloadInterval: TimeInterval = 15 * 60

    func placeholder(in context: Context) -> BarsEntry {
        BarsEntry(date: .now, snapshot: .unavailable(message: "Open Bars to collect usage."))
    }

    func getSnapshot(in context: Context, completion: @escaping (BarsEntry) -> Void) {
        completion(BarsEntry(date: .now, snapshot: load()))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<BarsEntry>) -> Void) {
        let now = Date()
        let snapshot = load()
        // Pre-schedule stale/period changes even if macOS delays the requested reload.
        // Relative timestamp text ages itself between these entries.
        var dates = [now]
        for provider in snapshot.visibleProviders {
            if let fetched = provider.fetchedAt {
                dates.append(fetched.addingTimeInterval(ProviderSnapshot.staleInterval))
            }
            // A rate-limit pause ends at a known instant; the badge must not outlive it.
            if provider.isPausing(at: now), let nextAttempt = provider.nextAttemptAt {
                dates.append(nextAttempt)
            }
            dates.append(contentsOf: provider.metrics.compactMap {
                UsagePresentation.resetDate(for: $0, provider: provider)
            })
        }
        let entries = Set(dates.filter { $0 >= now }).sorted().map {
            BarsEntry(date: $0, snapshot: snapshot)
        }
        completion(Timeline(entries: entries, policy: .after(now.addingTimeInterval(Self.fallbackReloadInterval))))
    }

    private func load() -> UsageSnapshot {
        do { return try SnapshotStore.read(from: SnapshotStore.sharedURL()) }
        catch {
            return .unavailable(message: (error as? SnapshotError)?.localizedDescription
                                ?? "Open Bars to collect usage or check its installation.")
        }
    }
}

struct BarsWidgetView: View {
    let entry: BarsEntry

    var body: some View {
        let latest = UsagePresentation.latestFetch(in: entry.snapshot)
        // Prefer rows at their natural height; text inside a height-constrained row shrinks
        // unevenly. If the widget is ever too short for that, fall back to scalable rows.
        ViewThatFits(in: .vertical) {
            content(latest: latest, pinRowHeights: true)
            content(latest: latest, pinRowHeights: false)
        }
        .containerBackground(.background, for: .widget)
    }

    /// The largest gap above each row when providers are hidden, so fewer rows stay top-aligned
    /// with modest even gaps instead of spreading apart.
    private static let maximumRowGap: CGFloat = 14
    /// Rows the large widget can hold without clipping; the overflow footer counts the rest.
    static let maximumRows = UsageSnapshot.standardProviderCount

    /// Four rows keep uncapped gaps: a capped spacer changed how the stack shared the few spare
    /// points and shrank the header text in the 344 pt widget.
    @ViewBuilder private func rowGap(capped: Bool) -> some View {
        if capped {
            Spacer(minLength: 3).frame(maxHeight: Self.maximumRowGap)
        } else {
            Spacer(minLength: 3)
        }
    }

    /// Rows keep their heights and spacers share any leftover space. Rows with an unbounded
    /// height would each be offered an equal share, squeezing the taller Cursor and Devin rows.
    /// Hidden providers have no row.
    private func content(latest: Date?, pinRowHeights: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text("Bars").font(.system(size: 13, weight: .semibold))
                Spacer()
                freshness(latest)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            // The large widget fits the standard four rows. With more plugins the first four, in
            // snapshot order, are shown and a footer counts the rest; the dropdown shows them all.
            let visible = entry.snapshot.visibleProviders
            let providers = Array(visible.prefix(Self.maximumRows))
            let limits = UsagePresentation.compactSecondaryAllocation(
                providers.map { UsagePresentation.secondaryAllowances(for: $0, now: entry.date).count })
            ForEach(Array(providers.enumerated()), id: \.element.id) { index, provider in
                rowGap(capped: providers.count < UsageSnapshot.standardProviderCount)
                Link(destination: provider.id.deepLink) {
                    ProviderRow(provider: provider, now: entry.date, compact: true, latestFetch: latest,
                                compactSecondaryLimit: limits[index])
                        .fixedSize(horizontal: false, vertical: pinRowHeights)
                }
                .buttonStyle(.plain)
                .accessibilityHint("Opens \(provider.name) details in Bars")
                .layoutPriority(1)
            }
            if visible.count > providers.count {
                Spacer(minLength: 3)
                Text("\(visible.count - providers.count) more in Bars")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            if providers.isEmpty {
                Spacer(minLength: 12)
                Text(entry.snapshot.unavailableMessage
                     ?? (entry.snapshot.providers.isEmpty
                         ? "No provider plugins are installed. Reinstall Bars or add a plugin."
                         : "Every provider is hidden. Open Bars from the menu bar to show one again."))
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
                    .multilineTextAlignment(.center)
                Spacer(minLength: 12)
            }
        }
        .frame(maxHeight: .infinity, alignment: .top)
    }

    /// Consolidated freshness. WidgetKit text keeps itself current between timeline entries; the
    /// timeline already holds an entry `staleInterval` after each fetch, when this switches to relative.
    @ViewBuilder private func freshness(_ latest: Date?) -> some View {
        if let latest {
            if UsagePresentation.isStale(latestFetch: latest, now: entry.date) {
                Text("Updated \(Text(latest, style: .relative)) ago")
            } else {
                Text("Updated \(Text(latest, style: .time))")
            }
        } else {
            Text("Never updated")
        }
    }
}

@main struct BarsWidget: Widget {
    let kind = SnapshotStore.widgetKind

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: BarsTimelineProvider()) { entry in
            BarsWidgetView(entry: entry)
        }
        .configurationDisplayName("Bars")
        .description("AI provider usage, with pace against each allowance period.")
        .supportedFamilies([.systemLarge])
    }
}
