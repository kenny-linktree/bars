#!/bin/bash
# Build a local, ad-hoc signed host app and its sandboxed WidgetKit extension.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TOOLCHAIN=auto
CONFIGURATION=Release
OUTPUT=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --toolchain) TOOLCHAIN=${2:?Missing toolchain}; shift 2 ;;
    --configuration) CONFIGURATION=${2:?Missing configuration}; shift 2 ;;
    --output) OUTPUT=${2:?Missing output directory}; shift 2 ;;
    -h|--help)
      echo 'Usage: scripts/build.sh [--toolchain auto|xcode|clt] [--configuration Debug|Release] [--output DIR]'
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$TOOLCHAIN" in auto|xcode|clt) ;; *) echo 'Invalid toolchain' >&2; exit 2 ;; esac
case "$CONFIGURATION" in Debug|Release) ;; *) echo 'Invalid configuration' >&2; exit 2 ;; esac
OUTPUT=${OUTPUT:-"$ROOT/.build/native/$CONFIGURATION"}
mkdir -p "$OUTPUT"
OUTPUT=$(cd "$OUTPUT" && pwd)

# Never change the machine's global developer-directory selection.
if [[ "$TOOLCHAIN" != clt ]]; then
  if [[ -z "${DEVELOPER_DIR:-}" && -d /Applications/Xcode.app/Contents/Developer ]]; then
    export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
  fi
  if /usr/bin/xcrun --find xcodebuild >/dev/null 2>&1 && /usr/bin/xcodebuild -version >/dev/null 2>&1; then
    TOOLCHAIN=xcode
  elif [[ "$TOOLCHAIN" == xcode ]]; then
    echo 'Full Xcode is unavailable. Install Xcode or select --toolchain clt.' >&2
    exit 1
  else
    TOOLCHAIN=clt
  fi
fi
if [[ "$TOOLCHAIN" == clt && -d /Library/Developer/CommandLineTools ]]; then
  # Build with the Command Line Tools even when xcode-select points at an Xcode.app, so the
  # documented toolchain is the one used and an unaccepted Xcode license cannot interfere.
  export DEVELOPER_DIR=/Library/Developer/CommandLineTools
fi

if [[ "$TOOLCHAIN" == xcode ]]; then
  /usr/bin/xcodebuild -project "$ROOT/macOS/Bars.xcodeproj" -scheme Bars \
    -configuration "$CONFIGURATION" -derivedDataPath "$ROOT/.build/xcode" \
    CONFIGURATION_BUILD_DIR="$OUTPUT" CODE_SIGN_IDENTITY=- \
    CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM= build
else
  APP="$OUTPUT/Bars.app"
  EXTENSION="$APP/Contents/PlugIns/BarsWidget.appex"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$EXTENSION/Contents/MacOS"
  cp "$ROOT/macOS/App/Resources/Bars.icns" "$APP/Contents/Resources/Bars.icns"
  TARGET="$(uname -m)-apple-macosx14.0"
  FLAGS=(-target "$TARGET" -parse-as-library)
  if [[ "$CONFIGURATION" == Release ]]; then FLAGS+=(-O); else FLAGS+=(-Onone -g); fi
  # Foundation must start the extension service. A normal Swift entry point
  # registers with PlugInKit but exits before WidgetKit can query its widgets.
  /usr/bin/xcrun swiftc "${FLAGS[@]}" -module-name BarsWidget -application-extension \
    "$ROOT"/macOS/Shared/*.swift "$ROOT"/macOS/Widget/*.swift \
    -Xlinker -e -Xlinker _NSExtensionMain \
    -o "$EXTENSION/Contents/MacOS/BarsWidget"
  /usr/bin/xcrun swiftc "${FLAGS[@]}" -module-name Bars \
    "$ROOT"/macOS/Shared/*.swift "$ROOT"/macOS/App/*.swift \
    -o "$APP/Contents/MacOS/Bars"

  # Expand the same checked-in Info.plist templates that Xcode uses.
  PYTHON=${BARS_BUILD_PYTHON:-python3}
  "$PYTHON" - "$ROOT/macOS/Config" "$APP" <<'PY'
import pathlib, plistlib, sys
config, app = map(pathlib.Path, sys.argv[1:])
targets = [
    ("App-Info.plist", app, "Bars", "local.bars.app"),
    ("Widget-Info.plist", app / "Contents/PlugIns/BarsWidget.appex", "BarsWidget", "local.bars.app.widget"),
]
for template, bundle, name, identifier in targets:
    info = plistlib.loads((config / template).read_bytes())
    info.update(CFBundleExecutable=name, CFBundleName=name, CFBundleIdentifier=identifier)
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
PY
  /usr/bin/codesign --force --sign - --entitlements "$ROOT/macOS/Config/Widget.entitlements" "$EXTENSION"
  /usr/bin/codesign --force --sign - --entitlements "$ROOT/macOS/Config/App.entitlements" "$APP"
fi
/usr/bin/codesign --verify --deep --strict "$OUTPUT/Bars.app"
echo "$OUTPUT/Bars.app"
