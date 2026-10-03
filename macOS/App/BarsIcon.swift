import AppKit

/// The Meter mark keeps the app icon's ascending fill lengths at menu-bar size.
enum BarsIcon {
    static func menuBarImage() -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            NSColor.black.setFill()
            // Whole-point bounds keep the three-point strokes sharp at 1x and 2x.
            for (y, width): (CGFloat, CGFloat) in [(13, 10), (8, 13), (3, 16)] {
                NSBezierPath(
                    roundedRect: NSRect(x: 1, y: y, width: width, height: 3),
                    xRadius: 1.5, yRadius: 1.5
                ).fill()
            }
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "Bars AI usage"
        return image
    }
}
