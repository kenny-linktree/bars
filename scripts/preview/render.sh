#!/bin/bash
# Render Bars SwiftUI views to PNG previews in light and dark appearance, without installing the
# app or hosting WidgetKit. Builds with Command Line Tools, like scripts/build.sh.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HARNESS="$ROOT/scripts/preview/harness"
BUILD="$ROOT/.build/preview-harness"
OUTPUT=
CHECK=false
PASS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) CHECK=true; shift ;;
    --output) OUTPUT=${2:?Missing output directory}; shift 2 ;;
    --variant|--only|--widget-size) PASS+=("$1" "${2:?Missing value for $1}"); shift 2 ;;
    -h|--help)
      echo 'Usage: scripts/preview/render.sh [--output DIR] [--variant live|stale|mixed|heavy|previous|empty|setup|hidden]...'
      echo '         [--only widget|overview|details]... [--widget-size WxH (default 360x376)] [--check]'
      echo 'Defaults to synthetic data. --variant live explicitly renders private local usage.'
      echo '--check verifies fixture isolation and native refresh configuration without rendering.'
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
OUTPUT=${OUTPUT:-"$ROOT/.build/previews"}
mkdir -p "$OUTPUT" "$BUILD"
OUTPUT=$(cd "$OUTPUT" && pwd)

TARGET="$(uname -m)-apple-macosx14.0"
SWIFTC=(/usr/bin/xcrun swiftc -target "$TARGET" -Onone)

# The clock shim lets a fixed preview instant drive WidgetKit-style relative date text.
/usr/bin/xcrun clang -target "$TARGET" -dynamiclib -framework CoreFoundation \
  -install_name @rpath/libBarsPreviewClock.dylib \
  "$HARNESS/PreviewClock.c" -o "$BUILD/libBarsPreviewClock.dylib"

# The app's views compile into one module. The harness imports it with @testable because the
# views are internal. BarsApp.swift is excluded: it owns @main and the AppKit lifecycle.
# Globbing the rest means a new app file needs no change here.
VIEWS=("$ROOT"/macOS/Shared/*.swift)
for file in "$ROOT"/macOS/App/*.swift; do
  [[ "$(basename "$file")" == BarsApp.swift ]] || VIEWS+=("$file")
done
build_views() {
  "${SWIFTC[@]}" -parse-as-library -enable-testing -module-name BarsViews \
    -emit-library -static -emit-module -emit-module-path "$BUILD/BarsViews.swiftmodule" \
    "$@" -o "$BUILD/libBarsViews.a"
}
if [[ "$CHECK" == true ]]; then
  # Include the real delegate for lifecycle checks without invoking its app entry point.
  build_views -Xfrontend -entry-point-function-name -Xfrontend bars_app_main \
    "${VIEWS[@]}" "$ROOT/macOS/App/BarsApp.swift"
  "${SWIFTC[@]}" -D BARS_APP_LIFECYCLE -module-name BarsPreviewChecks -I "$BUILD" \
    "$HARNESS/Fixtures.swift" "$ROOT/scripts/preview/tests/main.swift" \
    -L "$BUILD" -lBarsViews -o "$BUILD/bars-preview-checks"
  "$BUILD/bars-preview-checks"
  exit 0
fi
DEFINES=()
# Renaming the widget's @main entry point lets the real BarsWidgetView link into the harness.
if ! build_views -Xfrontend -entry-point-function-name -Xfrontend bars_widget_main \
    "${VIEWS[@]}" "$ROOT/macOS/Widget/BarsWidget.swift" 2>"$BUILD/widget-build.log"; then
  # When only the widget file fails, still render the dropdown and details.
  if ! build_views "${VIEWS[@]}"; then
    echo 'error: the app views do not compile; fix the errors above, then rerun.' >&2
    exit 1
  fi
  echo "warning: widget build errors are in $BUILD/widget-build.log" >&2
  DEFINES+=(-D BARS_NO_WIDGET)
fi

"${SWIFTC[@]}" -module-name BarsPreview -I "$BUILD" -import-objc-header "$HARNESS/PreviewClock.h" \
  ${DEFINES[@]+"${DEFINES[@]}"} "$HARNESS"/*.swift \
  -L "$BUILD" -lBarsViews -lBarsPreviewClock -Xlinker -rpath -Xlinker @executable_path \
  -o "$BUILD/bars-preview"

"$BUILD/bars-preview" --output "$OUTPUT" ${PASS[@]+"${PASS[@]}"}
