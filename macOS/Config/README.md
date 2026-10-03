# Local signing and widget storage

The local build uses ad-hoc signing. The host has no additional
entitlements. The widget keeps App Sandbox enabled and grants read-only access
to exactly one file:

```text
~/Library/Application Support/Bars/widget-snapshot.json
```

The host validates and atomically publishes this file with mode `0600`. The
collector's staging file, credentials, and other files are outside the exception.

Apple documents
[`com.apple.security.temporary-exception.files.home-relative-path.read-only`](https://developer.apple.com/library/archive/documentation/Miscellaneous/Reference/EntitlementKeyReference/Chapters/AppSandboxTemporaryExceptionEntitlements.html)
for file-specific access relative to the user's real home directory. The value
starts with `/` and has no trailing slash because it names a file.

App Groups were rejected by `containermanagerd` when WidgetKit launched the
ad-hoc signed extension. A standalone sandboxed executable was insufficient to
test that restriction. Verify this configuration with the actual system-hosted
widget after installation. The Xcode target and Command Line Tools build both use these same
entitlement files.
