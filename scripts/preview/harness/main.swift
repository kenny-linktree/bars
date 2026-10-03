import AppKit
import Foundation

// Renders Bars views to PNG files. Run through scripts/preview/render.sh, which builds this.

let usage = """
Usage: scripts/preview/render.sh [--output DIR] [--variant NAME]... [--only SURFACE]... [--widget-size WxH]
  --output DIR      Write PNGs here (default .build/previews)
  --variant NAME    \(Fixtures.names.joined(separator: "|")); repeatable, default synthetic variants
  --only SURFACE    \(Surface.allCases.map(\.rawValue).joined(separator: "|")); repeatable, default all
  --widget-size WxH Widget canvas in points (default \(Int(PreviewLayout.widgetSize.width))x\(Int(PreviewLayout.widgetSize.height)); the installed widget measures 344x344)
"""

func fail(_ message: String, status: Int32) -> Never {
    FileHandle.standardError.write(Data("\(message)\n".utf8))
    exit(status)
}

func warn(_ message: String) {
    FileHandle.standardError.write(Data("warning: \(message)\n".utf8))
}

var output: URL?
var variants: [String] = []
var surfaces: [Surface] = []
var widgetSize = PreviewLayout.widgetSize
var arguments = CommandLine.arguments.dropFirst()
while let flag = arguments.popFirst() {
    guard ["--output", "--variant", "--only", "--widget-size"].contains(flag) else {
        if flag == "-h" || flag == "--help" { print(usage); exit(0) }
        fail("Unknown argument: \(flag)\n\(usage)", status: 2)
    }
    guard let value = arguments.popFirst() else { fail("Missing value for \(flag)", status: 2) }
    switch flag {
    case "--output": output = URL(fileURLWithPath: value, isDirectory: true)
    case "--variant":
        guard Fixtures.names.contains(value) else { fail("Unknown variant \(value)\n\(usage)", status: 2) }
        if !variants.contains(value) { variants.append(value) }
    case "--widget-size":
        let parts = value.split(separator: "x").compactMap { Double($0) }
        guard parts.count == 2, parts.allSatisfy({ $0 >= 100 && $0 <= 1_000 }) else {
            fail("Invalid widget size \(value); expected WxH in points, such as 344x344", status: 2)
        }
        widgetSize = CGSize(width: parts[0], height: parts[1])
    default:
        guard let surface = Surface(rawValue: value) else { fail("Unknown surface \(value)\n\(usage)", status: 2) }
        if !surfaces.contains(surface) { surfaces.append(surface) }
    }
}
guard let output else { fail("Missing --output DIR", status: 2) }
if variants.isEmpty { variants = Fixtures.syntheticNames }
if surfaces.isEmpty { surfaces = Surface.allCases }
#if BARS_NO_WIDGET
if surfaces.contains(.widget) { warn("macOS/Widget/BarsWidget.swift did not build into the harness; skipping widget renders") }
#endif

MainActor.assumeIsolated {
    // An accessory-free AppKit context lets hosted controls resolve their appearance.
    NSApplication.shared.setActivationPolicy(.prohibited)
    do {
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let renderer = Renderer(output: output, widgetSize: widgetSize, warn: warn)
        var written: [URL] = []
        for variant in try Fixtures.make(variants, wallNow: Date(), warn: warn) {
            written += try renderer.render(variant, surfaces: surfaces)
        }
        guard !written.isEmpty else { fail("No previews were rendered.", status: 1) }
        print("Wrote \(written.count) previews to \(output.path):")
        for url in written { print("  \(url.lastPathComponent)") }
    } catch {
        fail("error: \(error.localizedDescription)", status: 1)
    }
}
