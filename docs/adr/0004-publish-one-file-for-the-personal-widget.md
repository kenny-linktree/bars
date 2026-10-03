# Publish one file for the personal widget

The ad-hoc signed WidgetKit extension could register and render, but macOS rejected its App Group access when hosted by the widget system because the signature had no authorized developer team. The local build instead grants the sandboxed widget read-only access to exactly `~/Library/Application Support/Bars/widget-snapshot.json` through Apple's documented home-relative file exception entitlement. The native publisher validates the collector's separate staging snapshot and atomically replaces this private published file; the widget receives no credential-directory or write access.

This is a choice for locally built, ad-hoc signed installations. A future signed distribution should reassess storage and use a properly authorized App Group where appropriate. Full Xcode remains supported by the project; the Command Line Tools build links the widget through `_NSExtensionMain` so WidgetKit can host it.

See [Apple's sandbox exception reference](https://developer.apple.com/library/archive/documentation/Miscellaneous/Reference/EntitlementKeyReference/Chapters/AppSandboxTemporaryExceptionEntitlements.html).
