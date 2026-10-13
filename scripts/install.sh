#!/bin/bash
# Installs this app and exactly five helper files. Never copies profile data.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
APP="$ROOT/build.noindex/Chat Transfer.app"
DESTINATION="$HOME/Applications/Chat Transfer.app"
CONTROL="$HOME/Library/Application Support/Arbeit/chat-manager"
MAINTENANCE="$HOME/Library/Application Support/Arbeit/maintenance-backups"
BUNDLE_ID="local.chatgpt.chat-manager"
HELPERS=(manager.py bridge.py library.py handoff.py auto_sync.py)

if [[ $# -gt 0 ]]; then
    if [[ $# -eq 2 && "$1" == --destination ]]; then
        DESTINATION="$2"
    else
        echo 'Usage: scripts/install.sh [--destination "/absolute/path/Chat Transfer.app"]' >&2
        exit 1
    fi
fi
if [[ "$(uname -s)" != Darwin ]]; then
    echo "Chat Transfer requires macOS." >&2
    exit 1
fi
if [[ $# -eq 0 ]]; then
    INSTALLATIONS=()
    for candidate in "/Applications/Chat Transfer.app" "/Applications/Chat Manager.app" \
        "$HOME/Applications/Chat Transfer.app" "$HOME/Applications/Chat Manager.app"; do
        if [[ -d "$candidate" && ! -L "$candidate" ]]; then
            candidate_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$candidate/Contents/Info.plist" 2>/dev/null || true)"
            if [[ "$candidate_id" == "$BUNDLE_ID" ]]; then
                INSTALLATIONS+=("$candidate")
            fi
        fi
    done
    if [[ ${#INSTALLATIONS[@]} -gt 1 ]]; then
        echo 'Multiple Chat Transfer installations found. Use --destination to choose one.' >&2
        printf '  %s\n' "${INSTALLATIONS[@]}" >&2
        exit 1
    elif [[ ${#INSTALLATIONS[@]} -eq 1 ]]; then
        DESTINATION="${INSTALLATIONS[0]}"
    fi
fi
if [[ "$DESTINATION" != /* || "$DESTINATION" != *.app || "$DESTINATION" == *'/../'* ]]; then
    echo "Choose an absolute .app destination path without parent traversal." >&2
    exit 1
fi
if [[ "$DESTINATION" == "$APP" ]]; then
    echo "Choose an installation destination outside the build output." >&2
    exit 1
fi
for directory in "$CONTROL" "$MAINTENANCE" "$(dirname "$DESTINATION")"; do
    current="$directory"
    while [[ "$current" != / ]]; do
        if [[ -L "$current" ]]; then
            echo "Refusing a redirected installation or helper directory." >&2
            exit 1
        fi
        current="$(dirname "$current")"
    done
done
if [[ -L "$DESTINATION" || -L "$APP" ]]; then
    echo "Refusing a redirected app." >&2
    exit 1
fi
if pgrep -x ChatManager >/dev/null; then
    echo "Quit Chat Transfer before installing; its existing data will be kept." >&2
    exit 1
fi
if [[ -e "$DESTINATION" ]]; then
    EXISTING_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$DESTINATION/Contents/Info.plist" 2>/dev/null || true)"
    if [[ "$EXISTING_ID" != "$BUNDLE_ID" ]]; then
        echo "Refusing to replace an app that is not Chat Transfer." >&2
        exit 1
    fi
fi
if [[ ! -d "$APP" ]]; then
    "$ROOT/scripts/build.sh"
fi
NEW_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist")"
if [[ "$NEW_ID" != "$BUNDLE_ID" ]]; then
    echo "The build is not a Chat Transfer bundle." >&2
    exit 1
fi
codesign --verify --deep --strict "$APP"
for helper in "${HELPERS[@]}"; do
    if [[ ! -f "$APP/Contents/Resources/Backend/$helper" || -L "$APP/Contents/Resources/Backend/$helper" || -L "$CONTROL/$helper" ]]; then
        echo "Missing or redirected helper: $helper" >&2
        exit 1
    fi
    if [[ -e "$CONTROL/$helper" && ! -f "$CONTROL/$helper" ]]; then
        echo "Refusing to replace a non-file helper: $helper" >&2
        exit 1
    fi
done

mkdir -p "$(dirname "$DESTINATION")" "$CONTROL" "$MAINTENANCE"
chmod 700 "$CONTROL" "$MAINTENANCE"
BACKUP_TEMP="$(mktemp -d "$MAINTENANCE/install-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
BACKUP="$BACKUP_TEMP.noindex"
mv "$BACKUP_TEMP" "$BACKUP"
chmod 700 "$BACKUP"
mkdir -m 700 "$BACKUP/helpers"
for helper in "${HELPERS[@]}"; do
    if [[ -f "$CONTROL/$helper" ]]; then
        install -m 600 "$CONTROL/$helper" "$BACKUP/helpers/$helper"
    fi
done
STAGING="$(mktemp -d "$(dirname "$DESTINATION")/.chat-transfer-install.XXXXXX")"
NEW_APP="$STAGING/Chat Transfer.app"
HELPERS_STARTED=0
OLD_APP_MOVED=0
NEW_APP_INSTALLED=0

cleanup() {
    status=$?
    trap - EXIT
    if [[ $status -ne 0 ]]; then
        if [[ $NEW_APP_INSTALLED -eq 1 ]]; then
            rm -rf "$DESTINATION"
        fi
        if [[ $OLD_APP_MOVED -eq 1 ]]; then
            mv "$BACKUP/Chat Transfer.app" "$DESTINATION"
        fi
        if [[ $HELPERS_STARTED -eq 1 ]]; then
            for helper in "${HELPERS[@]}"; do
                if [[ -f "$BACKUP/helpers/$helper" ]]; then
                    install -m 600 "$BACKUP/helpers/$helper" "$CONTROL/$helper"
                else
                    rm -f "$CONTROL/$helper"
                fi
            done
        fi
        printf 'Installation failed. Existing app/helper code was restored. Backup: %s\n' "$BACKUP" >&2
    fi
    rm -rf "$STAGING"
    exit "$status"
}
trap cleanup EXIT

ditto "$APP" "$NEW_APP"
codesign --verify --deep --strict "$NEW_APP"
if [[ -e "$DESTINATION" ]]; then
    mv "$DESTINATION" "$BACKUP/Chat Transfer.app"
    OLD_APP_MOVED=1
fi
mv "$NEW_APP" "$DESTINATION"
NEW_APP_INSTALLED=1
HELPERS_STARTED=1
for helper in "${HELPERS[@]}"; do
    install -m 600 "$APP/Contents/Resources/Backend/$helper" "$STAGING/$helper"
    mv "$STAGING/$helper" "$CONTROL/$helper"
done
codesign --verify --deep --strict "$DESTINATION"
printf 'Installed: %s\n' "$DESTINATION"
printf 'Previous app/helper code: %s\n' "$BACKUP"
printf 'Open with: open "%s"\n' "$DESTINATION"
