# Collect independently of the host app and widget

Bars collects usage in a background process that starts at login and continues after the host app quits. The widget reads saved results on its own update schedule because collection must not depend on the host app being open or the widget being active. This requires managing a separate background process and per-provider freshness; a per-user LaunchAgent schedules each short-lived run. [ADR 0004](0004-publish-one-file-for-the-personal-widget.md) records the shared-storage mechanism.
