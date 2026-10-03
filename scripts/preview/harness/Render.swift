import AppKit
import SwiftUI
@testable import BarsViews

enum Surface: String, CaseIterable { case widget, overview, details }

enum Appearance: String, CaseIterable {
    case light, dark
    var nsAppearance: NSAppearance { NSAppearance(named: self == .dark ? .darkAqua : .aqua)! }
    var colorScheme: ColorScheme { self == .dark ? .dark : .light }
}

enum PreviewError: LocalizedError {
    case usage(String), renderFailed(String)
    var errorDescription: String? {
        switch self {
        case .usage(let message): return message
        case .renderFailed(let name): return "Could not render \(name)"
        }
    }
}

enum PreviewLayout {
    static let scale: CGFloat = 2
    /// Default systemLarge canvas. The installed widget on the development machine measured 344 x 344 pt;
    /// pass `--widget-size 344x344` to match it.
    static let widgetSize = CGSize(width: 360, height: 376)
    /// Stands in for WidgetKit's default content margins, which exist only under WidgetKit hosting.
    static let widgetContentMargin: CGFloat = 16
    static let widgetCornerRadius: CGFloat = 22
    /// The app's own fixed dropdown size.
    static let dropdownSize = DropdownLayout.size
}

@MainActor struct Renderer {
    let output: URL
    var widgetSize = PreviewLayout.widgetSize
    let warn: (String) -> Void

    func render(_ variant: PreviewVariant, surfaces: [Surface]) throws -> [URL] {
        var written: [URL] = []
        // Pin the wall clock to the variant's instant so relative-date text agrees with `now`.
        // Clear any earlier offset first, so `Date()` below reads the real clock.
        bars_preview_set_clock_offset(0)
        bars_preview_set_clock_offset(variant.now.timeIntervalSince(Date()))
        defer { bars_preview_set_clock_offset(0) }
        for appearance in Appearance.allCases {
            let suffix = "\(variant.name)-\(appearance.rawValue).png"
            for surface in surfaces {
                switch surface {
                case .widget:
                    #if BARS_NO_WIDGET
                    continue
                    #else
                    let view = WidgetPreview(snapshot: variant.snapshot, now: variant.now, size: widgetSize)
                    written.append(try write(imageRendered(view, appearance: appearance), "widget-\(suffix)"))
                    #endif
                case .overview:
                    let view = OverviewMirror(snapshot: variant.snapshot, now: variant.now,
                                              messages: variant.dropdownMessages)
                    let (image, size) = try hosted(view, appearance: appearance)
                    if size.height > PreviewLayout.dropdownSize.height {
                        warn("dropdown-overview-\(suffix): content is \(Int(size.height)) pt tall; the real \(Int(PreviewLayout.dropdownSize.height)) pt dropdown scrolls")
                    }
                    written.append(try write(image, "dropdown-overview-\(suffix)"))
                case .details:
                    let model = BarsModel()
                    for provider in variant.snapshot.providers {
                        let view = DetailsPreview(provider: provider, now: variant.now, model: model,
                                                  messages: variant.dropdownMessages)
                        let (image, _) = try hosted(view, appearance: appearance)
                        written.append(try write(image, "details-\(provider.id.rawValue)-\(suffix)"))
                    }
                }
            }
        }
        return written
    }

    /// WidgetKit archives a SwiftUI-only rendering with no AppKit controls; ImageRenderer is the
    /// closest standalone equivalent.
    private func imageRendered(_ view: some View, appearance: Appearance) throws -> NSBitmapImageRep {
        var image: CGImage?
        appearance.nsAppearance.performAsCurrentDrawingAppearance {
            let renderer = ImageRenderer(content: view.environment(\.colorScheme, appearance.colorScheme))
            renderer.scale = PreviewLayout.scale
            image = renderer.cgImage
        }
        guard let image else { throw PreviewError.renderFailed("widget") }
        return NSBitmapImageRep(cgImage: image)
    }

    /// The dropdown is an NSHostingController, so render through NSHostingView. ImageRenderer
    /// draws AppKit-backed controls such as `Link` as a placeholder.
    private func hosted(_ view: some View, appearance: Appearance) throws -> (NSBitmapImageRep, CGSize) {
        let host = NSHostingView(rootView: view.frame(width: PreviewLayout.dropdownSize.width))
        let size = host.fittingSize
        // Without a 2x window, SwiftUI rasterizes text at 1x and the bitmap is upscaled.
        let window = PreviewWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: .borderless,
                                   backing: .buffered, defer: false)
        window.appearance = appearance.nsAppearance
        window.contentView = host
        host.frame = NSRect(origin: .zero, size: size)
        host.layoutSubtreeIfNeeded()
        guard let image = NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: Int((size.width * PreviewLayout.scale).rounded(.up)),
            pixelsHigh: Int((size.height * PreviewLayout.scale).rounded(.up)), bitsPerSample: 8,
            samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
            bytesPerRow: 0, bitsPerPixel: 0)
        else { throw PreviewError.renderFailed("dropdown") }
        image.size = size
        appearance.nsAppearance.performAsCurrentDrawingAppearance {
            host.cacheDisplay(in: host.bounds, to: image)
        }
        return (image.retagging(with: .sRGB) ?? image, size)
    }

    private func write(_ image: NSBitmapImageRep, _ name: String) throws -> URL {
        guard let data = image.representation(using: .png, properties: [:]) else {
            throw PreviewError.renderFailed(name)
        }
        let url = output.appendingPathComponent(name)
        try data.write(to: url, options: .atomic)
        return url
    }
}

/// An offscreen window that reports the preview scale whatever displays this Mac has attached.
private final class PreviewWindow: NSWindow {
    override var backingScaleFactor: CGFloat { PreviewLayout.scale }
}

#if !BARS_NO_WIDGET
/// The widget canvas: the real widget body inside a stand-in for `.containerBackground(for: .widget)`.
struct WidgetPreview: View {
    let snapshot: UsageSnapshot
    let now: Date
    let size: CGSize

    var body: some View {
        content
            .padding(PreviewLayout.widgetContentMargin)
            .frame(width: size.width, height: size.height)
            .background(.background, in: RoundedRectangle(cornerRadius: PreviewLayout.widgetCornerRadius,
                                                          style: .continuous))
    }

    var content: some View {
        BarsWidgetView(entry: BarsEntry(date: now, snapshot: snapshot))
    }
}
#endif
