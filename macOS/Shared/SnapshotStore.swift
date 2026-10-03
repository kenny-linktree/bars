import Foundation
import Darwin

enum SnapshotStore {
    static let widgetKind = "BarsOverview"
    static let fileName = "widget-snapshot.json"
    static let maximumSnapshotBytes = 1_048_576
    /// Upper bound for the `getpwuid_r` buffer, which doubles from 16 KiB while the call reports ERANGE.
    private static let maximumPasswordBufferBytes = 1_048_576

    static func sharedURL() throws -> URL {
        // Sandbox NSHomeDirectory points at the extension container. Resolve the account's
        // actual home for the single published file allowed by its read-only entitlement.
        var capacity = 16_384
        while capacity <= maximumPasswordBufferBytes {
            var buffer = [CChar](repeating: 0, count: capacity)
            var code: Int32 = 0
            let path: String? = buffer.withUnsafeMutableBufferPointer { bytes in
                var entry = passwd()
                var result: UnsafeMutablePointer<passwd>?
                code = getpwuid_r(getuid(), &entry, bytes.baseAddress, bytes.count, &result)
                guard code == 0, result != nil, let home = entry.pw_dir else { return nil }
                return String(cString: home)
            }
            if code == ERANGE { capacity *= 2; continue }
            guard code == 0, let path, path.hasPrefix("/"), path != "/" else {
                throw SnapshotError.sharedContainerUnavailable
            }
            return URL(fileURLWithPath: path, isDirectory: true)
                .appendingPathComponent("Library/Application Support/Bars", isDirectory: true)
                .appendingPathComponent(fileName)
        }
        throw SnapshotError.sharedContainerUnavailable
    }

    static func read(from url: URL) throws -> UsageSnapshot {
        try UsageSnapshot.decode(readData(from: url))
    }

    /// Reads a regular file without following a symlink, bounded to `maximumSnapshotBytes`.
    static func readData(from url: URL) throws -> Data {
        let descriptor = open(url.path, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else { throw posixError() }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        defer { try? handle.close() }
        var attributes = stat()
        guard fstat(descriptor, &attributes) == 0 else { throw posixError() }
        guard (attributes.st_mode & S_IFMT) == S_IFREG,
              attributes.st_size >= 0, attributes.st_size <= maximumSnapshotBytes else {
            throw SnapshotError.invalidMetrics
        }
        var data = Data()
        // Keep allocation bounded even if the source grows after fstat.
        while let chunk = try handle.read(upToCount: maximumSnapshotBytes + 1 - data.count), !chunk.isEmpty {
            data.append(chunk)
            guard data.count <= maximumSnapshotBytes else { throw SnapshotError.invalidMetrics }
        }
        return data
    }

    /// Validates before replacing the old snapshot. A failed publish leaves it intact.
    static func publish(from source: URL, to destination: URL) throws {
        let data = try readData(from: source)
        _ = try UsageSnapshot.decode(data)
        let directory = destination.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                               attributes: [.posixPermissions: 0o700])
        try replaceAtomically(destination, with: data, temporaryPrefix: ".widget-snapshot")
    }

    /// Writes a private mode-0600 temporary file in the destination's directory, flushes it and
    /// renames it over the destination. A failure leaves the previous file intact and removes the
    /// temporary file. `rename(2)` replaces a symlink at the destination rather than writing
    /// through it, so a planted link cannot redirect the write.
    ///
    /// `synchronize()` is `fsync`, not `F_FULLFSYNC`, and the directory is not synced: both files
    /// are rewritten on the next collection or edit, so surviving a power loss is not worth the cost.
    static func replaceAtomically(_ destination: URL, with data: Data, temporaryPrefix: String) throws {
        let directory = destination.deletingLastPathComponent()
        var template = Array(directory.appendingPathComponent("\(temporaryPrefix).XXXXXX").path.utf8CString)
        let descriptor = mkstemp(&template) // Creates a private 0600 file before writing any bytes.
        guard descriptor >= 0 else { throw posixError() }
        let temporaryPath = String(cString: template)
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        var renamed = false
        defer {
            try? handle.close()
            if !renamed { unlink(temporaryPath) }
        }
        try handle.write(contentsOf: data)
        try handle.synchronize()
        guard rename(temporaryPath, destination.path) == 0 else { throw posixError() }
        renamed = true
    }

    /// True when a read or replace failed because the file or its directory does not exist,
    /// which before the first install or collection is expected rather than a fault.
    static func isMissingFile(_ error: Error) -> Bool {
        let failure = error as NSError
        return (failure.domain == NSPOSIXErrorDomain && failure.code == Int(ENOENT))
            || (failure.domain == NSCocoaErrorDomain && failure.code == NSFileReadNoSuchFileError)
    }

    private static func posixError() -> NSError {
        NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
    }
}

/// The installer's `installation.json`, which sits beside the published snapshot. The app edits only
/// `disabled_providers`, an array of provider IDs the collector skips; a missing key means none.
enum InstallationSettings {
    static let fileName = "installation.json"
    static let disabledProvidersKey = "disabled_providers"

    enum SettingsError: LocalizedError {
        case invalid
        var errorDescription: String? {
            "Bars cannot read its installation settings. Reinstall Bars, then try again."
        }
    }

    static func url() throws -> URL {
        try SnapshotStore.sharedURL().deletingLastPathComponent().appendingPathComponent(fileName)
    }

    /// Hidden providers named in the settings. Unknown IDs from a newer collector are ignored.
    static func disabledProviders(in data: Data) throws -> Set<ProviderID> {
        Set(try disabledIDs(in: object(from: data)).compactMap(ProviderID.init(rawValue:)))
    }

    /// Read-modify-write of the settings: every other key and unknown ID is kept; known IDs are
    /// listed once, in contract order. Malformed settings throw rather than being overwritten.
    static func updating(_ data: Data, provider: ProviderID, hidden: Bool) throws -> Data {
        var settings = try object(from: data)
        let current = try disabledIDs(in: settings)
        var known = Set(current.compactMap(ProviderID.init(rawValue:)))
        if hidden { known.insert(provider) } else { known.remove(provider) }
        var unknown: [String] = []
        for id in current where ProviderID(rawValue: id) == nil && !unknown.contains(id) { unknown.append(id) }
        settings[disabledProvidersKey] = ProviderID.allCases.filter { known.contains($0) }.map(\.rawValue) + unknown
        var output = try JSONSerialization.data(withJSONObject: settings,
                                                options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        output.append(0x0A)
        return output
    }

    /// Reads, updates and atomically replaces the settings file with mode 0600.
    ///
    /// Concurrency is last-writer-wins, deliberately. The only other writer is the installer, which
    /// rewrites the whole file while holding `refresh.lock`; the refresh runner only reads it. The app
    /// does not take that lock, because waiting for it would block the main thread for as long as a
    /// refresh runs. Both sides replace the file atomically, so neither reads a torn file. A hide or show
    /// made during the seconds an install runs can be lost; the dropdown then shows the installer's
    /// list on its next reload, so the display never disagrees with the file.
    static func setHidden(_ hidden: Bool, provider: ProviderID, at url: URL) throws {
        let updated = try updating(SnapshotStore.readData(from: url), provider: provider, hidden: hidden)
        try SnapshotStore.replaceAtomically(url, with: updated, temporaryPrefix: ".installation")
    }

    private static func object(from data: Data) throws -> [String: Any] {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw SettingsError.invalid
        }
        return object
    }

    private static func disabledIDs(in settings: [String: Any]) throws -> [String] {
        guard let value = settings[disabledProvidersKey], !(value is NSNull) else { return [] }
        guard let ids = value as? [String] else { throw SettingsError.invalid }
        return ids
    }
}
