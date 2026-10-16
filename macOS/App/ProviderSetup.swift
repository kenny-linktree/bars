import AppKit
import SwiftUI

/// The details section for a provider that is not set up: the collector's explanation, numbered
/// steps with the install link and login command from the plugin manifest, and a way to hide a
/// provider the person does not use. A plugin without setup text gets a single generic step.
struct ProviderSetupSection: View {
    let provider: ProviderSnapshot
    let hide: () -> Void

    var body: some View {
        let guide = provider.setup
        VStack(alignment: .leading, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Set up \(provider.name)").font(.headline)
                if let message = provider.message {
                    Text(message)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let guide {
                step(1) {
                    Link(destination: guide.installUrl) {
                        HStack(spacing: 3) {
                            Text(guide.installTitle)
                            Image(systemName: "arrow.up.right")
                        }
                    }
                    .accessibilityLabel("\(guide.installTitle), opens in your browser")
                }
                step(2) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(guide.loginStep)
                        CommandField(command: guide.loginCommand)
                    }
                }
                step(3) { Text(provider.enabled ? "Choose Refresh now." : "Choose Show \(provider.name), which also refreshes it.") }
                Text(guide.credentialNote)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                step(1) { Text("Sign in to \(provider.name) the way its plugin expects.") }
                step(2) { Text(provider.enabled ? "Choose Refresh now." : "Choose Show \(provider.name), which also refreshes it.") }
                Text("The \(provider.name) plugin does not describe its login. Bars only reads existing logins.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if provider.enabled {
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Text("Not using \(provider.name)?").foregroundStyle(.secondary)
                    Button("Hide it", action: hide)
                        .buttonStyle(.link)
                        .accessibilityLabel("Hide \(provider.name)")
                        .accessibilityHint("Removes \(provider.name) from the overview and widget. Bars stops checking it.")
                }
                .font(.callout)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 8).fill(.quaternary.opacity(0.6)))
    }

    private func step(_ number: Int, @ViewBuilder content: () -> some View) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("\(number).")
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .frame(width: 16, alignment: .trailing)
                .accessibilityHidden(true)
            content()
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .font(.callout)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Step \(number)")
    }
}

/// A short box beneath the login-required banner with the provider's login command.
struct SignInAgainBox: View {
    let provider: ProviderSnapshot

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Sign in again").font(.subheadline.weight(.semibold))
            Text(provider.setup?.signInAgain ?? "Sign in to \(provider.name) again the way its plugin expects.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let command = provider.setup?.loginCommand {
                CommandField(command: command)
            }
            Text("Then choose Refresh now.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8).strokeBorder(.quaternary))
    }
}

/// A Terminal command in a monospaced field with a copy button.
struct CommandField: View {
    let command: String
    @State private var copied = false
    /// How long the button shows its checkmark after a copy.
    private static let copiedFeedback: Duration = .seconds(1.5)

    var body: some View {
        HStack(spacing: 8) {
            Text(command)
                .font(.system(.callout, design: .monospaced))
                .textSelection(.enabled)
                .lineLimit(1)
            Spacer(minLength: 0)
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(command, forType: .string)
                copied = true
                Task {
                    try? await Task.sleep(for: Self.copiedFeedback)
                    copied = false
                }
            } label: {
                Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                    .labelStyle(.iconOnly)
                    .frame(width: 16)
            }
            .buttonStyle(.borderless)
            .help("Copy command")
            .accessibilityLabel(copied ? "Copied \(command)" : "Copy command \(command)")
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.06)))
    }
}
