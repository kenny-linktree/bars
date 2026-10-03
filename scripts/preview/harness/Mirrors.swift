import AppKit
import SwiftUI
@testable import BarsViews

// Hand-maintained copies of layouts the harness cannot compile. The source of truth is
// `BarsDropdown.body` and `BarsDropdown.statusMessage(_:)` in macOS/App/BarsApp.swift, which owns
// `@main` and the AppKit lifecycle. When you change the dropdown's header, footer, spacing or
// copy there, make the same change here, or the overview previews silently drift from the app.
// The provider rows, bars, hidden-provider line and details inside them are the real shared
// views, so changes to those need no mirroring.

/// The overview branch of `BarsDropdown`. The real dropdown wraps this in a `ScrollView` of the
/// fixed `DropdownLayout.size`; the harness fits the height to the content instead, and its
/// buttons do nothing. Accessibility hints and keyboard shortcuts draw nothing, so they are omitted.
/// `now` mirrors the model's display date, advanced only while the real dropdown is open.
struct OverviewMirror: View {
    let snapshot: UsageSnapshot
    let now: Date
    var messages: [String] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            ForEach(messages, id: \.self, content: DropdownStatusMessage.init)
            let latest = UsagePresentation.latestFetch(in: snapshot)
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Usage").font(.title2.weight(.semibold))
                    Text(UsagePresentation.updatedText(since: latest, now: now))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Refresh now") {}
            }
            let visible = snapshot.visibleProviders
            ForEach(visible) { provider in
                Button {} label: { ProviderRow(provider: provider, now: now, latestFetch: latest) }
                    .buttonStyle(.plain)
                if provider.id != visible.last?.id { Divider() }
            }
            if visible.isEmpty {
                Text("Every provider is hidden. Show one below to see its usage.")
                    .foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 8) {
                HiddenProvidersLine(snapshot: snapshot) { _ in }
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text("Updates about every 15 minutes while this Mac is awake. A provider shows its own time when it differs.")
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    Button("Quit Bars") {}
                        .buttonStyle(.plain)
                        .foregroundStyle(.secondary)
                }
            }
            .font(.caption).foregroundStyle(.secondary)
        }
        .padding(20)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}

/// The details branch of `BarsDropdown`: the back button above the real `ProviderDetails`.
struct DetailsPreview: View {
    let provider: ProviderSnapshot
    let now: Date
    @ObservedObject var model: BarsModel
    var messages: [String] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            ForEach(messages, id: \.self, content: DropdownStatusMessage.init)
            Button {} label: { Label("All providers", systemImage: "chevron.left") }
                .buttonStyle(.plain).foregroundStyle(.secondary)
            ProviderDetails(provider: provider, now: now, model: model)
        }
        .padding(20)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}

/// `BarsDropdown.statusMessage(_:)`.
struct DropdownStatusMessage: View {
    let text: String

    var body: some View {
        Label(text, systemImage: "exclamationmark.circle")
            .font(.callout)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }
}
