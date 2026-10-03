import AppKit
import SwiftUI

@MainActor final class BarsModel: ObservableObject {
    @Published private(set) var snapshot = UsageSnapshot.unavailable(message: "Waiting for the first collection.")
    @Published private(set) var storageError: String?
    @Published private(set) var refreshError: String?
    @Published private(set) var settingsError: String?
    @Published private(set) var isRefreshing = false
    @Published private(set) var displayDate: Date
    @Published var selectedProvider: ProviderID?
    /// Retained while the collector runs, so the process and its termination handler outlive `refresh`.
    private var refreshProcess: Process?
    /// Provider refreshes requested by hide or show while another refresh was running. Each still
    /// has to run so the collector republishes with the new settings.
    private var queuedRefreshes: [ProviderID] = []
    private let readSnapshot: () throws -> UsageSnapshot
    private let readDisabledProviders: () -> Set<ProviderID>?
    private let now: () -> Date
    private var pollingTimer: Timer?

    init(readSnapshot: @escaping () throws -> UsageSnapshot = {
        try SnapshotStore.read(from: SnapshotStore.sharedURL())
    }, disabledProviders: @escaping () -> Set<ProviderID>? = {
        ProviderSettings.disabledProviders()
    }, now: @escaping () -> Date = Date.init) {
        self.readSnapshot = readSnapshot
        self.readDisabledProviders = disabledProviders
        self.now = now
        displayDate = now()
    }

    deinit { pollingTimer?.invalidate() }

    /// The AppKit popover owns polling explicitly. A hosted SwiftUI task can remain alive after
    /// dismissal, so view appearance is not a reliable boundary for reading the file.
    func startPolling(interval: TimeInterval = 5) {
        guard pollingTimer == nil else { return }
        reload()
        updateDisplayDate(force: true)
        let timer = Timer(timeInterval: interval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.reload()
                self?.updateDisplayDate()
            }
        }
        timer.tolerance = min(1, interval / 5)
        RunLoop.main.add(timer, forMode: .common)
        pollingTimer = timer
    }

    func stopPolling() {
        pollingTimer?.invalidate()
        pollingTimer = nil
    }

    private func updateDisplayDate(force: Bool = false) {
        let date = now()
        // Follow minute boundaries so timer tolerance does not accumulate into a slower cadence.
        let minuteChanged = floor(date.timeIntervalSinceReferenceDate / 60)
            != floor(displayDate.timeIntervalSinceReferenceDate / 60)
        if date != displayDate && (force || minuteChanged || date < displayDate) {
            displayDate = date
        }
    }

    func reload() {
        do {
            var value = try readSnapshot()
            applyVisibilitySettings(to: &value)
            if snapshot != value { snapshot = value }
            if storageError != nil { storageError = nil }
        } catch {
            var value = snapshot
            applyVisibilitySettings(to: &value)
            if snapshot != value { snapshot = value }
            let message: String
            if SnapshotStore.isMissingFile(error) {
                message = "No usage data yet. Choose Refresh now after installing the collector."
            } else {
                message = (error as? SnapshotError)?.localizedDescription
                    ?? "Bars cannot read the latest snapshot. Retained values may be old."
            }
            if storageError != message { storageError = message }
        }
    }

    /// The settings file changes the moment the person hides or shows a provider, while the
    /// published snapshot follows only after the collector republishes. Prefer the settings so the
    /// dropdown does not flip back in between. The widget reads only the published `enabled`.
    private func applyVisibilitySettings(to snapshot: inout UsageSnapshot) {
        guard let disabled = readDisabledProviders() else { return }
        for index in snapshot.providers.indices {
            snapshot.providers[index].enabled = !disabled.contains(snapshot.providers[index].id)
        }
    }

    /// Hides or shows a provider by editing `installation.json`, then refreshes that provider. For
    /// a hidden provider the collector skips collection but still republishes, so the widget drops
    /// its row promptly. Hiding from its details returns to the overview.
    func setHidden(_ hidden: Bool, provider id: ProviderID) {
        guard let index = snapshot.providers.firstIndex(where: { $0.id == id }) else { return }
        do {
            try ProviderSettings.setHidden(hidden, provider: id)
            settingsError = nil
        } catch {
            settingsError = ProviderSettings.failureMessage(error, hidden: hidden, name: snapshot.providers[index].name)
            return
        }
        snapshot.providers[index].enabled = !hidden
        if hidden, selectedProvider == id { selectedProvider = nil }
        if isRefreshing {
            if !queuedRefreshes.contains(id) { queuedRefreshes.append(id) }
        } else {
            refresh(provider: id)
        }
    }

    func refresh(provider: ProviderID? = nil) {
        guard !isRefreshing else { return }
        let runner = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Bars/bin/refresh")
        guard FileManager.default.isExecutableFile(atPath: runner.path) else {
            refreshError = "The background collector is not installed. Run the Bars installer, then refresh."
            return
        }
        let process = Self.makeRefreshProcess(runner: runner, provider: provider)
        // Foundation calls this on a background thread. Read the status there and hop to the main
        // actor for every model change.
        process.terminationHandler = { [weak self] process in
            let status = process.terminationStatus
            // Capture `self` in the task's own capture list: Swift 5.9 and 5.10 reject a captured
            // weak variable being read inside a concurrently executing closure.
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.isRefreshing = false
                self.refreshProcess = nil
                self.refreshError = status == 0 ? nil
                    : "Refresh could not complete. Check the collector status and try again."
                self.reload()
                if !self.queuedRefreshes.isEmpty { self.refresh(provider: self.queuedRefreshes.removeFirst()) }
            }
        }
        do {
            refreshError = nil
            isRefreshing = true
            refreshProcess = process
            try process.run()
        } catch {
            isRefreshing = false
            refreshProcess = nil
            refreshError = "Bars could not start the collector. Check its installation."
        }
    }

    /// Match the collector's child environment (`CHILD_ENVIRONMENT` in
    /// collector/bars_collector/adapters.py and integration/refresh.py). In particular, inherited
    /// Python startup hooks and unrelated API keys must not reach the runner before its own
    /// filtering can execute.
    static func makeRefreshProcess(runner: URL, provider: ProviderID?,
                                   environment: [String: String] = ProcessInfo.processInfo.environment) -> Process {
        let allowed: Set<String> = [
            "HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
            "__CF_USER_TEXT_ENCODING", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
            "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
        ]
        let process = Process()
        process.executableURL = runner
        process.arguments = ["--wait"] + (provider.map { ["--provider", $0.rawValue] } ?? [])
        process.environment = environment.filter { allowed.contains($0.key) }
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        return process
    }

    func open(_ url: URL) {
        guard let id = ProviderID(deepLink: url) else { return }
        selectedProvider = id
        reload()
        NSApplication.shared.activate(ignoringOtherApps: true)
    }
}
