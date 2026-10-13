#!/bin/bash
# Synthetic fixtures and headless protocol checks only. No native import here.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
BUILD="$ROOT/build.noindex"
SWIFT_SOURCES=()
if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
    echo "The complete test suite requires macOS on Apple silicon." >&2
    exit 1
fi
if [[ -L "$BUILD" ]]; then
    echo "Refusing a redirected build directory." >&2
    exit 1
fi
if [[ ! -x /usr/bin/python3 ]]; then
    echo "Install Xcode Command Line Tools to provide /usr/bin/python3." >&2
    exit 1
fi
/usr/bin/python3 -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9 or newer is required"'

export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$ROOT/Backend:$ROOT/Tests"
/usr/bin/python3 -m unittest discover -s "$ROOT/Tests" -p 'test_*.py' -v

mkdir -p "$BUILD/module-cache" "$BUILD/tests"
for source in "$ROOT"/App/*.swift; do
    if [[ "${source##*/}" == make-icon.swift ]]; then continue; fi
    if [[ ! -f "$source" || -L "$source" ]]; then
        echo "Refusing a missing or redirected Swift source." >&2
        exit 1
    fi
    SWIFT_SOURCES+=("$source")
done
xcrun swiftc -parse-as-library -swift-version 6 \
    -D CHAT_MANAGER_PROTOCOL_TESTS -target arm64-apple-macos13.0 \
    -module-cache-path "$BUILD/module-cache" \
    "${SWIFT_SOURCES[@]}" \
    -o "$BUILD/tests/protocol-tests"
"$BUILD/tests/protocol-tests"

xcrun swiftc -parse-as-library -swift-version 6 \
    -D CHAT_DISCOVERY_TESTS \
    -target arm64-apple-macos13.0 \
    -module-cache-path "$BUILD/module-cache" \
    "$ROOT/App/AutoDiscovery.swift" "$ROOT/Tests/AutoDiscoveryTests.swift" \
    -o "$BUILD/tests/discovery-tests"
"$BUILD/tests/discovery-tests"
