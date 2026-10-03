import Foundation

// Compile with swiftc -O plus Snapshot.swift, UsagePresentation.swift and SnapshotStore.swift.
// All inputs are synthetic. File operations stay under .build/performance/native.
@main enum CoreBenchmark {
    static func main() throws {
        let now = SnapshotDate.parse("2026-10-09T17:00:00Z")!
        let epoch = "2026-10-09T17:00:00.123456Z"
        let locale = Locale(identifier: "en_US")
        let timeZone = TimeZone(identifier: "America/Los_Angeles")!
        func fixture(_ count: Int) throws -> Data {
            let providers = ProviderID.allCases.map { id -> [String: Any] in
                ["id": id.rawValue, "name": id.displayName, "enabled": true, "status": "ok",
                 "fetched_at": epoch, "last_attempt_at": epoch, "primary_metric_id": "metric0",
                 "metrics": (0..<count).map { index -> [String: Any] in
                     ["id": "metric\(index)", "label": "Monthly usage", "kind": "budget",
                      "scope": "individual", "unit": "USD", "used": 20.0, "limit": 100.0,
                      "remaining": 80.0, "resets_at": "2026-11-01T00:00:00.123456Z", "period": "month"]
                 }]
            }
            return try JSONSerialization.data(withJSONObject: ["schema_version": 1, "generated_at": epoch,
                                                               "providers": providers], options: [.sortedKeys])
        }
        func measure(_ name: String, iterations: Int, _ body: () throws -> Int) rethrows {
            _ = try body() // Warm framework initialization and filesystem state equally.
            var samples: [Double] = []
            var checksum = 0
            for _ in 0..<iterations {
                let start = DispatchTime.now().uptimeNanoseconds
                checksum &+= try body()
                samples.append(Double(DispatchTime.now().uptimeNanoseconds - start) / 1e6)
            }
            samples.sort()
            print("\(name) iterations=\(iterations) median_ms=\(samples[samples.count / 2]) p95_ms=\(samples[min(samples.count - 1, Int(Double(samples.count) * 0.95))]) checksum=\(checksum)")
        }
        let normal = try fixture(3)
        let large = try fixture(1100)
        let snapshot = try UsageSnapshot.decode(normal)
        print("synthetic normal_bytes=\(normal.count) normal_metrics=12 large_bytes=\(large.count) large_metrics=4400")
        try measure("normal_decode", iterations: 200) { try UsageSnapshot.decode(normal).providers.count }
        try measure("large_decode", iterations: 10) { try UsageSnapshot.decode(large).providers[0].metrics.count }
        measure("presentation_12_metrics", iterations: 200) {
            var checksum = 0
            for provider in snapshot.providers {
                checksum += UsagePresentation.detailsFreshness(fetchedAt: provider.fetchedAt, now: now,
                                                                locale: locale, timeZone: timeZone).count
                for metric in provider.metrics {
                    for value in [metric.used!, metric.remaining!, metric.limit!] {
                        checksum += UsagePresentation.amount(value, unit: metric.unit, locale: locale).count
                    }
                    checksum += UsagePresentation.fullResetText(for: metric, provider: provider,
                                                                 locale: locale, timeZone: timeZone)?.count ?? 0
                    if let pace = UsagePresentation.pace(for: metric, provider: provider, at: now) {
                        checksum += UsagePresentation.paceCaption(for: pace, period: .month,
                                                                   locale: locale, timeZone: timeZone).count
                    }
                }
            }
            return checksum
        }
        measure("numbers_120_values", iterations: 200) {
            var checksum = 0
            for index in 0..<120 {
                checksum += UsagePresentation.amount(Double(index) + 0.46,
                                                      unit: ["USD", "%", "credits"][index % 3], locale: locale).count
            }
            return checksum
        }
        let directory = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
            .appendingPathComponent(".build/performance/native").appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let source = directory.appendingPathComponent("synthetic-source.json")
        let destination = directory.appendingPathComponent("synthetic-published.json")
        try normal.write(to: source)
        try measure("normal_read_data", iterations: 200) { try SnapshotStore.readData(from: source).count }
        try measure("normal_read_decode", iterations: 200) { try SnapshotStore.read(from: source).providers.count }
        try measure("normal_publish", iterations: 50) {
            try SnapshotStore.publish(from: source, to: destination)
            return normal.count
        }
        try large.write(to: source)
        try measure("large_read_data", iterations: 100) { try SnapshotStore.readData(from: source).count }
        try measure("large_read_decode", iterations: 10) { try SnapshotStore.read(from: source).providers[0].metrics.count }
    }
}
