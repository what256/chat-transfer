#!/bin/bash
# Build only. Does not install helpers, read chats, or modify vendor apps.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
BUILD="$ROOT/build"
APP="$BUILD/Chat Transfer.app"
HELPERS=(manager.py bridge.py library.py handoff.py auto_sync.py)
SWIFT_SOURCES=()

if [[ "$(uname -s)" != Darwin ]]; then
    echo "Chat Transfer requires macOS." >&2
    exit 1
fi
if [[ "$(uname -m)" != arm64 ]]; then
    echo "This build targets Apple silicon (arm64)." >&2
    exit 1
fi
if [[ -L "$BUILD" || -L "$APP" ]]; then
    echo "Refusing a redirected build directory or app." >&2
    exit 1
fi
if ! xcrun --find swiftc >/dev/null 2>&1; then
    echo "Install Xcode or its Command Line Tools with a Swift 6 compiler." >&2
    exit 1
fi
SWIFT_VERSION="$(xcrun swiftc -version)"
if ! [[ "$SWIFT_VERSION" =~ Swift\ version\ ([6-9]|[1-9][0-9]+)\. ]]; then
    echo "Swift 6 or newer is required." >&2
    exit 1
fi
for file in ChatManager.swift AutoDiscovery.swift make-icon.swift; do
    if [[ ! -f "$ROOT/App/$file" || -L "$ROOT/App/$file" ]]; then
        echo "Missing regular source file: App/$file" >&2
        exit 1
    fi
done
for source in "$ROOT"/App/*.swift; do
    if [[ "${source##*/}" == make-icon.swift ]]; then continue; fi
    if [[ ! -f "$source" || -L "$source" ]]; then
        echo "Refusing a missing or redirected Swift source." >&2
        exit 1
    fi
    SWIFT_SOURCES+=("$source")
done
for helper in "${HELPERS[@]}"; do
    if [[ ! -f "$ROOT/Backend/$helper" || -L "$ROOT/Backend/$helper" ]]; then
        echo "Missing regular source file: Backend/$helper" >&2
        exit 1
    fi
done

mkdir -p "$BUILD/module-cache"
STAGING="$(mktemp -d "$BUILD/.build.XXXXXX")"
trap 'rm -rf "$STAGING"' EXIT
BUNDLE="$STAGING/Chat Transfer.app"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources/Backend"

xcrun swiftc -parse-as-library -swift-version 6 \
    -target arm64-apple-macos13.0 \
    -module-cache-path "$BUILD/module-cache" \
    "${SWIFT_SOURCES[@]}" \
    -o "$BUNDLE/Contents/MacOS/ChatManager"
xcrun swiftc -swift-version 6 -target arm64-apple-macos13.0 \
    -module-cache-path "$BUILD/module-cache" \
    "$ROOT/App/make-icon.swift" -o "$STAGING/make-icon"
"$STAGING/make-icon" "$STAGING/ChatManager.iconset"
iconutil -c icns "$STAGING/ChatManager.iconset" \
    -o "$BUNDLE/Contents/Resources/ChatManager.icns"
cp "$ROOT/Packaging/Info.plist" "$BUNDLE/Contents/Info.plist"
plutil -lint "$BUNDLE/Contents/Info.plist" >/dev/null
for helper in "${HELPERS[@]}"; do
    install -m 600 "$ROOT/Backend/$helper" "$BUNDLE/Contents/Resources/Backend/$helper"
done
chmod 755 "$BUNDLE/Contents/MacOS/ChatManager"
codesign --force --sign - "$BUNDLE"
codesign --verify --deep --strict "$BUNDLE"
if [[ -e "$APP" ]]; then
    rm -rf "$APP"
fi
mv "$BUNDLE" "$APP"
printf 'Built: %s\n' "$APP"
printf 'To install: %s/scripts/install.sh\n' "$ROOT"
