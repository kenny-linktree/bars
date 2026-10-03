// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Bars",
    platforms: [.macOS(.v14)],
    products: [.library(name: "BarsCore", targets: ["BarsCore"])],
    targets: [
        // The Foundation-only snapshot model, presentation and storage. ProviderRow.swift is SwiftUI
        // and builds only with the app and widget. `swift test` needs full Xcode for XCTest; with
        // Command Line Tools, compile the same checks with swiftc as the README describes.
        .target(name: "BarsCore", path: "macOS/Shared", exclude: ["ProviderRow.swift"]),
        .testTarget(name: "BarsCoreTests", dependencies: ["BarsCore"], path: "tests/Swift")
    ]
)
