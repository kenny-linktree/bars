import AppKit
import SwiftUI

@main enum BarsMain {
    @MainActor static func main() {
        if let status = SnapshotPublisher.run(arguments: Array(CommandLine.arguments.dropFirst())) {
            exit(status)
        }
        let application = NSApplication.shared
        let delegate = BarsAppDelegate()
        application.delegate = delegate
        application.run()
    }
}

/// Bars lives in the menu bar. Clicking its status item opens a dropdown with the overview.
/// A widget link opens the same dropdown on that provider's details.
///
/// AppKit hosts the status item because SwiftUI's `MenuBarExtra` offers no supported way to open
/// its panel programmatically, which widget deep links require. Dismissal is application-defined:
/// a transient popover closes as soon as the accessory app loses active status, which happened
/// within a second when the dropdown was opened by a URL or an accessibility press.
@MainActor final class BarsAppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    private let model: BarsModel
    private var statusItem: NSStatusItem?
    private let popover = NSPopover()
    private var outsideClickMonitor: Any?
    private var escapeKeyMonitor: Any?
    /// A URL that launches the app arrives before `applicationDidFinishLaunching`, when there is
    /// no status item to anchor the dropdown to. Remember the request and open after setup.
    private var finishedLaunching = false
    private var openWhenLaunched = false
    /// `kVK_Escape` from Carbon's HIToolbox, without importing Carbon for one constant.
    private static let escapeKeyCode: UInt16 = 53
    /// How often, and how many times, to check for the status item's on-screen frame at launch.
    private static let statusItemPollInterval: TimeInterval = 0.1
    private static let statusItemPollAttempts = 20

    init(model: BarsModel? = nil) {
        self.model = model ?? BarsModel()
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // LSUIElement in Info.plist already hides the Dock icon; this keeps the behavior if a
        // build drops that key.
        NSApp.setActivationPolicy(.accessory)
        NSApp.mainMenu = quitMenu()

        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = item.button {
            button.image = BarsIcon.menuBarImage()
            button.toolTip = "Bars · AI usage"
            button.target = self
            button.action = #selector(toggleDropdown)
        }
        statusItem = item

        popover.behavior = .applicationDefined
        popover.delegate = self
        popover.animates = true
        popover.contentSize = DropdownLayout.size
        finishedLaunching = true
        if openWhenLaunched {
            openWhenLaunched = false
            showDropdownWhenStatusItemIsOnScreen()
        }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls { model.open(url) }
        if finishedLaunching { showDropdownWhenStatusItemIsOnScreen() } else { openWhenLaunched = true }
    }

    /// The status bar positions a new item asynchronously. At launch the item's window reports
    /// itself visible while its frame is still zero-height at the origin, and showing the popover
    /// then silently produces nothing. Wait for a real frame, bounded to about two seconds.
    private func showDropdownWhenStatusItemIsOnScreen(attempt: Int = 0) {
        let frame = statusItem?.button?.window?.frame ?? .zero
        if (frame.height > 0 && frame.width > 0) || attempt >= Self.statusItemPollAttempts {
            showDropdown()
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.statusItemPollInterval) { [weak self] in
            self?.showDropdownWhenStatusItemIsOnScreen(attempt: attempt + 1)
        }
    }

    @objc private func toggleDropdown() {
        if popover.isShown { closeDropdown() } else { showDropdown() }
    }

    private func showDropdown() {
        guard let button = statusItem?.button else { return }
        model.startPolling()
        if popover.contentViewController == nil {
            popover.contentViewController = NSHostingController(rootView: BarsDropdown(model: model))
        }
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        // A failed show never calls popoverDidClose, so stop polling and install no monitors.
        guard popover.isShown else { model.stopPolling(); return }
        // Activation gives the dropdown keyboard focus for ⌘Q and Escape; it is not required
        // for the popover to stay visible.
        NSApp.activate()
        if outsideClickMonitor == nil {
            // Global monitors only see events delivered to other applications, so clicks inside
            // the dropdown and on the status item itself are unaffected. Mouse monitors need no
            // accessibility permission.
            outsideClickMonitor = NSEvent.addGlobalMonitorForEvents(
                matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]
            ) { [weak self] _ in
                Task { @MainActor in self?.closeDropdown() }
            }
        }
        if escapeKeyMonitor == nil {
            escapeKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                guard event.keyCode == Self.escapeKeyCode else { return event }
                Task { @MainActor in self?.closeDropdown() }
                return nil
            }
        }
    }

    private func closeDropdown() {
        model.stopPolling()
        popover.performClose(nil)
    }

    func popoverDidClose(_ notification: Notification) {
        model.stopPolling()
        if let outsideClickMonitor { NSEvent.removeMonitor(outsideClickMonitor) }
        if let escapeKeyMonitor { NSEvent.removeMonitor(escapeKeyMonitor) }
        outsideClickMonitor = nil
        escapeKeyMonitor = nil
    }

    func applicationWillTerminate(_ notification: Notification) {
        model.stopPolling()
    }

    /// An accessory app has no visible menu bar menus, but a main menu still supplies ⌘Q.
    private func quitMenu() -> NSMenu {
        let menu = NSMenu()
        let application = NSMenuItem()
        let submenu = NSMenu()
        submenu.addItem(withTitle: "Quit Bars", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        application.submenu = submenu
        menu.addItem(application)
        return menu
    }
}

struct BarsDropdown: View {
    @ObservedObject var model: BarsModel

    var body: some View {
        let now = model.displayDate
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let error = model.storageError { statusMessage(error) }
                if let error = model.refreshError { statusMessage(error) }
                if let error = model.settingsError { statusMessage(error) }

                if let id = model.selectedProvider,
                   let provider = model.snapshot.providers.first(where: { $0.id == id }) {
                    Button { model.selectedProvider = nil } label: {
                        Label("All providers", systemImage: "chevron.left")
                    }
                    .buttonStyle(.plain).foregroundStyle(.secondary)
                    .accessibilityHint("Returns to the overview")
                    ProviderDetails(provider: provider, now: now, model: model)
                } else {
                    let latest = UsagePresentation.latestFetch(in: model.snapshot)
                    HStack(alignment: .firstTextBaseline) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Usage").font(.title2.weight(.semibold))
                            Text(UsagePresentation.updatedText(since: latest, now: now))
                                .font(.callout)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button(model.isRefreshing ? "Refreshing…" : "Refresh now") { model.refresh() }
                            .disabled(model.isRefreshing)
                            .accessibilityHint("Collects usage for every shown provider")
                    }
                    // Hidden providers have no row; the footer offers to show them again.
                    let visible = model.snapshot.visibleProviders
                    ForEach(visible) { provider in
                        Button { model.selectedProvider = provider.id } label: {
                            ProviderRow(provider: provider, now: now, latestFetch: latest)
                        }
                        .buttonStyle(.plain)
                        .accessibilityHint("Shows \(provider.name) details")
                        if provider.id != visible.last?.id { Divider() }
                    }
                    if visible.isEmpty {
                        Text("Every provider is hidden. Show one below to see its usage.")
                            .foregroundStyle(.secondary)
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        HiddenProvidersLine(snapshot: model.snapshot) { model.setHidden(false, provider: $0) }
                        HStack(alignment: .firstTextBaseline, spacing: 12) {
                            Text("Updates about every 15 minutes while this Mac is awake. A provider shows its own time when it differs.")
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 0)
                            Button("Quit Bars") { NSApp.terminate(nil) }
                                .buttonStyle(.plain)
                                .foregroundStyle(.secondary)
                                .keyboardShortcut("q")
                        }
                    }
                    .font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(20)
        }
        .frame(width: DropdownLayout.size.width, height: DropdownLayout.size.height)
    }

    private func statusMessage(_ text: String) -> some View {
        Label(text, systemImage: "exclamationmark.circle")
            .font(.callout)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)
    }
}
