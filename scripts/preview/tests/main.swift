import Foundation
import Combine
#if BARS_APP_LIFECYCLE
import AppKit
#endif
@testable import BarsViews

// Fixtures share this error type with the renderer; no AppKit context or live data is needed here.
enum PreviewError: Error { case usage(String) }

MainActor.assumeIsolated {
    do {
        var reads = 0
        let first = try Fixtures.make(Fixtures.syntheticNames, wallNow: Date()) { _ in
            preconditionFailure("Synthetic previews must not warn about live data")
        } loadLive: {
            reads += 1
            preconditionFailure("Synthetic previews must not read a live snapshot")
        }
        let second = try Fixtures.make(Fixtures.syntheticNames, wallNow: Date().addingTimeInterval(86_400),
                                       warn: { _ in }) {
            reads += 1
            preconditionFailure("Synthetic previews must not read a live snapshot")
        }
        precondition(reads == 0)
        precondition(first.count == 8 && first.map(\.name) == Fixtures.syntheticNames)
        precondition(first.map(\.now) == second.map(\.now))
        precondition(first.map(\.snapshot) == second.map(\.snapshot))
        let stale = first.first { $0.name == "stale" }!
        precondition(stale.snapshot.providers.allSatisfy { $0.isStale(at: stale.now) })
        let subscription = first.first { $0.name == "subscription" }!
        for provider in subscription.snapshot.providers.prefix(2) {
            precondition(provider.primaryMetric?.kind == .quota)
            precondition(UsagePresentation.secondaryMetrics(for: provider).count == 1)
        }
        print("PASS: eight synthetic fixtures, including subscriptions, never read live data and ignore wall time")

        let supplied = Fixtures.baseline(now: Fixtures.fixedNow)
        let live = try Fixtures.make(["live"], wallNow: Fixtures.fixedNow, warn: { _ in }) {
            reads += 1
            return supplied
        }
        precondition(reads == 1 && live.count == 1 && live[0].snapshot == supplied)
        print("PASS: explicit live fixture reads only the supplied loader")

        let runner = URL(fileURLWithPath: "/synthetic path/refresh")
        let process = BarsModel.makeRefreshProcess(runner: runner, provider: .cursor, environment: [
            "HOME": "/synthetic home", "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8",
            "HTTPS_PROXY": "http://proxy.example:8080", "SSL_CERT_FILE": "/synthetic-ca.pem",
            "PYTHONPATH": "/untrusted", "PYTHONHOME": "/untrusted", "PYTHONSTARTUP": "/untrusted",
            "UNRELATED_API_KEY": "synthetic", "DYLD_INSERT_LIBRARIES": "/untrusted",
        ])
        precondition(process.executableURL == runner && process.arguments == ["--wait", "--provider", "cursor"])
        precondition(process.environment == [
            "HOME": "/synthetic home", "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8",
            "HTTPS_PROXY": "http://proxy.example:8080", "SSL_CERT_FILE": "/synthetic-ca.pem",
        ])
        precondition((process.standardInput as? FileHandle) === FileHandle.nullDevice)
        precondition((process.standardOutput as? FileHandle) === FileHandle.nullDevice)
        precondition((process.standardError as? FileHandle) === FileHandle.nullDevice)
        precondition(BarsModel.makeRefreshProcess(runner: runner, provider: nil, environment: [:]).arguments == ["--wait"])
        print("PASS: native refresh uses argument arrays, an allowlisted environment and null standard streams")
        // Real bounded reads of synthetic files exercise atomic replacements and recovery without
        // consulting the installed snapshot or settings. Equal file sizes are deliberately kept.
        let directory = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
            .appendingPathComponent(".build/performance/ui/checks-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let snapshotURL = directory.appendingPathComponent("snapshot.json")
        let settingsURL = directory.appendingPathComponent("installation.json")
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.dateEncodingStrategy = .iso8601
        let originalData = try encoder.encode(supplied)
        try originalData.write(to: snapshotURL, options: .atomic)
        try Data("{\"disabled_providers\":[]}".utf8).write(to: settingsURL, options: .atomic)
        var modelReads = 0
        let model = BarsModel(readSnapshot: {
            modelReads += 1
            return try SnapshotStore.read(from: snapshotURL)
        }, disabledProviders: {
            guard let data = try? SnapshotStore.readData(from: settingsURL) else { return nil }
            return try? InstallationSettings.disabledProviders(in: data)
        })
        var publications = 0
        let observation = model.objectWillChange.sink { publications += 1 }
        model.reload()
        precondition(model.snapshot == supplied && publications == 1)
        publications = 0
        for _ in 0..<100 { model.reload() }
        precondition(modelReads == 101 && publications == 0)

        var changed = supplied
        changed.update(.cursor) { $0.update("included") { $0.set(used: 42, limit: 100) } }
        let changedData = try encoder.encode(changed)
        precondition(changedData.count == originalData.count)
        try changedData.write(to: snapshotURL, options: .atomic)
        model.reload()
        precondition(model.snapshot == changed && publications == 1)

        try Data("malformed".utf8).write(to: snapshotURL, options: .atomic)
        model.reload()
        precondition(model.snapshot == changed && model.storageError != nil && publications == 2)
        model.reload()
        precondition(publications == 2)
        try changedData.write(to: snapshotURL, options: .atomic)
        model.reload()
        precondition(model.snapshot == changed && model.storageError == nil && publications == 3)
        try FileManager.default.removeItem(at: snapshotURL)
        model.reload()
        precondition(model.snapshot == changed && model.storageError?.hasPrefix("No usage data yet.") == true)
        let afterMissing = publications
        model.reload()
        precondition(publications == afterMissing)

        // Visibility still follows settings during a read failure, then recovers to snapshot flags
        // when settings disappear or cannot be parsed.
        try Data("{\"disabled_providers\":[\"cursor\"]}".utf8).write(to: settingsURL, options: .atomic)
        model.reload()
        precondition(model.snapshot.hiddenProviders.map(\.id) == [.cursor])
        try changedData.write(to: snapshotURL, options: .atomic)
        model.reload()
        precondition(model.storageError == nil && model.snapshot.hiddenProviders.map(\.id) == [.cursor])
        let afterRecovery = publications
        model.reload()
        precondition(publications == afterRecovery)
        try Data("malformed".utf8).write(to: settingsURL, options: .atomic)
        model.reload()
        precondition(model.snapshot == changed)
        try FileManager.default.removeItem(at: settingsURL)
        let afterInvalidSettings = publications
        model.reload()
        precondition(model.snapshot == changed && publications == afterInvalidSettings)
        withExtendedLifetime(observation) {}
        print("PASS: unchanged reads publish nothing; same-size replacements, errors, recovery and visibility remain observable")

        let beforePolling = modelReads
        model.startPolling(interval: 0.01)
        let afterStart = modelReads
        model.startPolling(interval: 0.01)
        precondition(modelReads == afterStart)
        RunLoop.main.run(until: Date().addingTimeInterval(0.06))
        precondition(modelReads > beforePolling)
        model.stopPolling()
        model.stopPolling()
        let stoppedReads = modelReads
        RunLoop.main.run(until: Date().addingTimeInterval(0.04))
        precondition(modelReads == stoppedReads)
        model.startPolling(interval: 0.01)
        RunLoop.main.run(until: Date().addingTimeInterval(0.04))
        precondition(modelReads > stoppedReads)
        model.stopPolling()
        weak var releasedModel: BarsModel?
        do {
            let temporary = BarsModel(readSnapshot: { supplied }, disabledProviders: { nil })
            releasedModel = temporary
            temporary.startPolling(interval: 0.01)
        }
        precondition(releasedModel == nil)
        print("PASS: polling starts, stops, restarts and does not retain its model")

        var clock = Fixtures.fixedNow
        var latestSnapshot = supplied
        let clockModel = BarsModel(readSnapshot: { latestSnapshot }, disabledProviders: { nil }, now: { clock })
        clockModel.selectedProvider = .cursor
        clockModel.startPolling(interval: 0.01)
        clock = clock.addingTimeInterval(59)
        RunLoop.main.run(until: Date().addingTimeInterval(0.03))
        precondition(clockModel.displayDate == Fixtures.fixedNow)
        clock = clock.addingTimeInterval(1)
        RunLoop.main.run(until: Date().addingTimeInterval(0.03))
        precondition(clockModel.displayDate == clock)
        clockModel.stopPolling()
        let closedDate = clockModel.displayDate
        clock = clock.addingTimeInterval(3_600)
        latestSnapshot = changed
        RunLoop.main.run(until: Date().addingTimeInterval(0.03))
        precondition(clockModel.displayDate == closedDate && clockModel.snapshot == supplied)
        clockModel.startPolling(interval: 0.01)
        precondition(clockModel.snapshot == changed && clockModel.displayDate == clock)
        precondition(clockModel.selectedProvider == .cursor)
        clock = clock.addingTimeInterval(-120)
        RunLoop.main.run(until: Date().addingTimeInterval(0.03))
        precondition(clockModel.displayDate == clock)
        clockModel.stopPolling()
        print("PASS: display time advances once a minute, pauses while hidden, and recovers on reopen or a backward clock change")

        #if BARS_APP_LIFECYCLE
        var closedReads = 0
        let closingModel = BarsModel(readSnapshot: { closedReads += 1; return supplied }, disabledProviders: { nil })
        let delegate = BarsAppDelegate(model: closingModel)
        closingModel.startPolling(interval: 0.01)
        delegate.popoverDidClose(Notification(name: NSPopover.didCloseNotification))
        let readsAfterClose = closedReads
        RunLoop.main.run(until: Date().addingTimeInterval(0.04))
        precondition(closedReads == readsAfterClose)
        closingModel.startPolling(interval: 0.01)
        delegate.applicationWillTerminate(Notification(name: NSApplication.willTerminateNotification))
        let readsAfterTermination = closedReads
        RunLoop.main.run(until: Date().addingTimeInterval(0.04))
        precondition(closedReads == readsAfterTermination)
        print("PASS: AppKit's did-close notification stops polling without resetting model state")
        print("7 preview and native lifecycle checks passed")
        #else
        print("6 preview and native lifecycle checks passed")
        #endif
    } catch {
        fatalError("Preview checks failed: \(error)")
    }
}
