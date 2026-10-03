import SwiftUI

/// Hidden providers are stored as `disabled_providers` in the installer's `installation.json`,
/// under the account's real home (the same resolution as `SnapshotStore.sharedURL()`). The collector
/// skips them; the app only edits that key and leaves every other setting as it was.
enum ProviderSettings {
    /// Hidden providers from the settings file, or nil when it is missing or unreadable, in which
    /// case the published snapshot's `enabled` flags are the only source.
    static func disabledProviders() -> Set<ProviderID>? {
        guard let url = try? InstallationSettings.url(),
              let data = try? SnapshotStore.readData(from: url) else { return nil }
        return try? InstallationSettings.disabledProviders(in: data)
    }

    static func setHidden(_ hidden: Bool, provider: ProviderID) throws {
        try InstallationSettings.setHidden(hidden, provider: provider, at: InstallationSettings.url())
    }

    static func failureMessage(_ error: Error, hidden: Bool, name: String) -> String {
        if SnapshotStore.isMissingFile(error) {
            return "The background collector is not installed. Run the Bars installer, then try again."
        }
        if let error = error as? InstallationSettings.SettingsError { return error.localizedDescription }
        return "Bars could not \(hidden ? "hide" : "show") \(name). Check the installation's file permissions, then try again."
    }
}

/// The overview footer's tertiary "Hidden: Cursor, Devin" line, with a Show button for one hidden
/// provider or a Show menu for several.
struct HiddenProvidersLine: View {
    let snapshot: UsageSnapshot
    let show: (ProviderID) -> Void

    var body: some View {
        let hidden = snapshot.hiddenProviders
        if let summary = UsagePresentation.hiddenSummary(for: snapshot) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(summary).foregroundStyle(.tertiary)
                if hidden.count == 1, let provider = hidden.first {
                    Button("Show") { show(provider.id) }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                        .accessibilityLabel("Show \(provider.name)")
                        .accessibilityHint("Adds \(provider.name) back to the overview and widget and collects its usage.")
                } else {
                    Menu("Show…") {
                        ForEach(hidden) { provider in
                            Button("Show \(provider.name)") { show(provider.id) }
                        }
                    }
                    .menuStyle(.button)
                    .buttonStyle(.bordered)
                    .menuIndicator(.hidden)
                    .controlSize(.small)
                    .fixedSize()
                    .accessibilityLabel("Show a hidden provider")
                }
                Spacer(minLength: 0)
            }
            .accessibilityElement(children: .contain)
        }
    }
}

/// The details header control that hides a shown provider or shows a hidden one.
struct ProviderVisibilityButton: View {
    let provider: ProviderSnapshot
    @ObservedObject var model: BarsModel

    var body: some View {
        if provider.enabled {
            Button("Hide \(provider.name)") { model.setHidden(true, provider: provider.id) }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .accessibilityHint("Removes \(provider.name) from the overview and widget. Bars stops collecting its usage.")
        } else {
            Button("Show \(provider.name)") { model.setHidden(false, provider: provider.id) }
                .buttonStyle(.borderedProminent)
                .accessibilityHint("Adds \(provider.name) back to the overview and widget and collects its usage.")
        }
    }
}
