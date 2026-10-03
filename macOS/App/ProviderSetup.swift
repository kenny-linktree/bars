import AppKit
import SwiftUI

/// Setup steps: how each provider's existing login is created, shown when the collector finds no
/// login material (setup) or cannot use it (login required). Bars only reads these logins.
struct ProviderSetupGuide: Equatable {
    /// The first step's link text, such as "Install the Codex CLI".
    var installTitle: String
    var installURL: URL
    /// The Terminal command that creates or renews the login Bars reads.
    var loginCommand: String
    /// The second step's text, above the command.
    var loginStep: String
    /// The sign-in-again text, above the command.
    var signInAgain: String
    /// Which login Bars reads, so a person can tell what the steps produce.
    var credentialNote: String

    static func guide(for id: ProviderID) -> ProviderSetupGuide {
        switch id {
        case .claude:
            return .init(installTitle: "Install Claude Code",
                         installURL: URL(string: "https://code.claude.com/docs/en/overview")!,
                         loginCommand: "claude",
                         loginStep: "Run Claude Code once in Terminal and sign in:",
                         signInAgain: "Run Claude Code in Terminal and sign in again. If it does not ask, type /login.",
                         credentialNote: "Bars reads the “Claude Code-credentials” Keychain entry that Claude Code saves.")
        case .codex:
            return .init(installTitle: "Install the Codex CLI",
                         installURL: URL(string: "https://developers.openai.com/codex/cli")!,
                         loginCommand: "codex login",
                         loginStep: "Sign in with your ChatGPT account:",
                         signInAgain: "Run this in Terminal and sign in with your ChatGPT account:",
                         credentialNote: "Bars reads the ChatGPT login that Codex stores in ~/.codex/auth.json.")
        case .cursor:
            return .init(installTitle: "Install the Cursor CLI",
                         installURL: URL(string: "https://cursor.com/docs/cli/overview")!,
                         loginCommand: "agent login",
                         loginStep: "Sign in from Terminal:",
                         signInAgain: "Run this in Terminal and sign in again:",
                         credentialNote: "Bars reads the login that the Cursor CLI stores in the Keychain.")
        case .devin:
            return .init(installTitle: "Install the Devin CLI",
                         installURL: URL(string: "https://docs.devin.ai/cli")!,
                         loginCommand: "devin auth login",
                         loginStep: "Sign in from Terminal:",
                         signInAgain: "Run this in Terminal and sign in again:",
                         credentialNote: "Bars reads ~/.local/share/devin/credentials.toml and asks the Devin CLI for your organization.")
        }
    }
}

/// The details section for a provider that is not set up: the collector's explanation, numbered
/// steps with the install link and login command, and a way to hide a provider the person does not use.
struct ProviderSetupSection: View {
    let provider: ProviderSnapshot
    let hide: () -> Void

    var body: some View {
        let guide = ProviderSetupGuide.guide(for: provider.id)
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
            step(1) {
                Link(destination: guide.installURL) {
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
        let guide = ProviderSetupGuide.guide(for: provider.id)
        VStack(alignment: .leading, spacing: 6) {
            Text("Sign in again").font(.subheadline.weight(.semibold))
            Text(guide.signInAgain)
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            CommandField(command: guide.loginCommand)
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
