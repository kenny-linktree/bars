#!/bin/bash
# Package the approved artwork into all standard macOS icon sizes.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
ICONSET="$ROOT/.build/artwork/Bars.iconset"
mkdir -p "$ICONSET" "$ROOT/macOS/App/Resources"
for size in 16 32 128 256 512; do
  /usr/bin/sips -z "$size" "$size" "$ROOT/macOS/Artwork/Bars.png" \
    --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  retina=$((size * 2))
  /usr/bin/sips -z "$retina" "$retina" "$ROOT/macOS/Artwork/Bars.png" \
    --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
/usr/bin/iconutil -c icns "$ICONSET" -o "$ROOT/macOS/App/Resources/Bars.icns"
