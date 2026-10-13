#!/usr/bin/env python3
"""Read-only session previews and private transfer/recovery metadata.

No command executes a model turn, replays tools, restores databases, or opens
an app. Conversation text is returned only by an explicit details request.
"""
from __future__ import annotations

import collections
import datetime
import fcntl
import itertools
import json
import math
from pathlib import Path
import re
import sqlite3
import sys

import bridge

CONTROL = bridge.CONTROL.parent
MAINTENANCE_ROOTS = (Path.home() / "Library/Application Support/Arbeit/maintenance-backups",
                     Path.home() / "Library/Application Support/ChatGPT Copy/maintenance-backups")
LEGACY_ROOT = Path.home() / "Library/Application Support/Arbeit/chat-handoff"
MAX_METADATA = 256 * 1024
MAX_RECORDS = 1024
MAX_PREVIEW_MESSAGES = 12
MAX_PREVIEW_CHARACTERS = 4000
MAX_CLAUDE_SCAN = 8 * 1024 * 1024
MAX_CLAUDE_LINE = 1024 * 1024


def short_text(value, limit=300):
    return value[:limit] if isinstance(value, str) else ""


def safe_path(value, roots, must_exist=True):
    """Accept only known local references, without following any symlink."""
    if not isinstance(value, (str, Path)):
        return None
    try:
        path = Path(value)
        if len(str(path)) > 4096 or not path.is_absolute() or ".." in path.parts:
            return None
        if any(part.is_symlink() for part in (path, *path.parents)):
            return None
        if not any(path == root or root in path.parents for root in roots):
            return None
        if must_exist and not path.exists():
            return None
        return path
    except (OSError, ValueError):
        return None


def read_metadata(path, roots):
    path = safe_path(path, roots)
    if path is None or not path.is_file() or path.stat().st_size > MAX_METADATA:
        return None
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_METADATA + 1)
        if len(data) > MAX_METADATA:
            return None
        result = json.loads(data)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def timestamp(value, fallback=0):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return float(fallback)


def bounded_paths(paths):
    return sorted(itertools.islice(paths, MAX_RECORDS))


def operation_is_running(control, action, roots):
    path = control / ("bridge/operation.lock" if action in ("import", "continue") else "manager.lock")
    approved = safe_path(path, roots)
    if approved is None or not approved.is_file():
        return False
    try:
        with approved.open("rb") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream, fcntl.LOCK_UN)
    except OSError:
        # Unknown lock state is not evidence that an operation safely completed.
        return True
    return False


def preview_messages(messages):
    """Return a small explicit UI preview; never persist conversation text."""
    clipped = len(messages) > MAX_PREVIEW_MESSAGES
    shown = []
    for message in messages[-MAX_PREVIEW_MESSAGES:]:
        text = message.get("text")
        if message.get("role") not in ("user", "assistant") or not isinstance(text, str):
            continue
        if len(text) > MAX_PREVIEW_CHARACTERS:
            text = text[:MAX_PREVIEW_CHARACTERS] + "\n[Preview shortened; the saved source remains complete.]"
            clipped = True
        shown.append({"role": message["role"], "text": text})
    return shown, clipped


class Library:
    def __init__(self, control=CONTROL, profiles=None, maintenance_roots=MAINTENANCE_ROOTS,
                 legacy_root=LEGACY_ROOT, backend=None):
        self.control = Path(control)
        self.profiles = profiles or bridge.PROFILES
        self.maintenance_roots = tuple(Path(path) for path in maintenance_roots)
        self.legacy_root = Path(legacy_root)
        self.roots = (self.control, self.legacy_root, *self.maintenance_roots)
        self.backend = backend or bridge.Bridge(profiles=self.profiles, control=self.control / "bridge")

    def activity(self):
        directory = safe_path(self.control / "activity", self.roots)
        records, skipped = [], 0
        if directory and directory.is_dir():
            for path in bounded_paths(directory.glob("*.json")):
                raw = read_metadata(path, self.roots)
                if not raw or not bridge.canonical_uuid(raw.get("id")) or path.stem != raw["id"]:
                    skipped += 1
                    continue
                if raw.get("source") not in bridge.NAMES or raw.get("destination") not in bridge.NAMES:
                    skipped += 1
                    continue
                if raw.get("status") not in ("running", "completed", "partial", "failed"):
                    skipped += 1
                    continue
                items = []
                for item in raw.get("items", [])[:50] if isinstance(raw.get("items"), list) else []:
                    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                        continue
                    items.append({"id": short_text(item["id"], 100), "title": short_text(item.get("title"), 200),
                                  "status": short_text(item.get("status"), 40), "verified": item.get("verified") is True})
                paths = []
                for value in raw.get("paths", [])[:50] if isinstance(raw.get("paths"), list) else []:
                    approved = safe_path(value, self.roots)
                    if approved is not None:
                        paths.append(str(approved))
                status = raw["status"]
                message = short_text(raw.get("message"), 2000)
                if status == "running" and not operation_is_running(self.control, raw.get("action"), self.roots):
                    status = "interrupted"
                    message = "The transfer process is no longer running. Review its saved files before retrying."
                records.append({"id": raw["id"], "created_at": timestamp(raw.get("created_at"), path.stat().st_mtime),
                                "source": raw["source"], "destination": raw["destination"],
                                "action": short_text(raw.get("action"), 40), "title": short_text(raw.get("title"), 200),
                                "status": status, "message": message,
                                "items": items, "paths": paths})
        records.sort(key=lambda record: record["created_at"], reverse=True)
        return {"ok": True, "message": f"{len(records)} recorded transfer{'s' if len(records) != 1 else ''}.",
                "activities": records, "skipped_records": skipped}

    def backups(self):
        rows, seen = [], set()

        def add(path, kind, title, metadata=None):
            approved = safe_path(path, self.roots)
            if approved is None or not approved.is_dir() or str(approved) in seen or len(rows) >= MAX_RECORDS:
                return
            seen.add(str(approved))
            data = metadata or {}
            created = timestamp(data.get("prepared_at") or data.get("created_at") or data.get("updated_at"),
                                approved.stat().st_mtime)
            rows.append({"id": kind + ":" + str(approved), "title": short_text(title, 200), "kind": kind,
                         "created_at": created, "path": str(approved),
                         "message": "Prepared reference context; source history remains available." if kind == "continuation" else
                                    "Private recovery files. Reveal them to inspect; this view does not restore databases."})

        for root in self.maintenance_roots:
            directory = safe_path(root, self.roots)
            if directory and directory.is_dir():
                for path in bounded_paths(directory.iterdir()):
                    if path.name.startswith("."):
                        continue
                    add(path, "app-backup", path.name)

        # Traverse only the fixed backup layouts, never an arbitrary recursive tree.
        patterns = ("chats/*/backups/*", "chats/*/visibility-backups/*",
                    "pairs/*/chats/*/backups/*", "pairs/*/chats/*/visibility-backups/*")
        if safe_path(self.control, self.roots):
            for pattern in patterns:
                for path in bounded_paths(self.control.glob(pattern)):
                    manifest = read_metadata(path / "validation.json", self.roots)
                    add(path, "chat-backup", "Saved chat recovery · " + path.name, manifest)
            for path in bounded_paths(self.control.glob("bridge/exports/*/manifest.json")):
                manifest = read_metadata(path, self.roots)
                if manifest and manifest.get("format") == "chat-transfer-continuation-v1":
                    add(path.parent, "continuation", manifest.get("source_title") or "Claude Code continuation", manifest)
        legacy = safe_path(self.legacy_root / "backups", self.roots)
        if legacy and legacy.is_dir():
            for path in bounded_paths(legacy.iterdir()):
                add(path, "chat-backup", "Earlier chat recovery · " + path.name,
                    read_metadata(path / "validation.json", self.roots))
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return {"ok": True, "message": f"{len(rows)} backup or continuation folder{'s' if len(rows) != 1 else ''}.",
                "backups": rows}

    def claude_details(self, identifier):
        catalog = self.backend.detect_claude()
        if identifier not in catalog:
            raise bridge.BridgeError("This Claude Code session is no longer detected. Refresh the library.")
        row = catalog[identifier]
        roots = self.backend.claude_roots
        path = safe_path(row["session_path"], roots)
        if path is None or not path.is_file():
            raise bridge.BridgeError("This Claude Code session is missing or redirected.")
        before = path.stat()
        recent = collections.deque(maxlen=MAX_PREVIEW_MESSAGES)
        attachments, count, bytes_seen, skipped = 0, 0, 0, 0
        start = max(0, before.st_size - MAX_CLAUDE_SCAN)
        with path.open("rb") as stream:
            stream.seek(start)
            if start:
                # Ignore the partial JSON line at the start of the bounded tail.
                stream.readline(MAX_CLAUDE_LINE + 1)
            while bytes_seen <= MAX_CLAUDE_SCAN:
                line = stream.readline(MAX_CLAUDE_LINE + 1)
                if not line:
                    break
                bytes_seen += len(line)
                if len(line) > MAX_CLAUDE_LINE:
                    skipped += 1
                    # An oversized source record is excluded from this preview.
                    while line and not line.endswith(b"\n") and bytes_seen <= MAX_CLAUDE_SCAN:
                        line = stream.readline(MAX_CLAUDE_LINE + 1)
                        bytes_seen += len(line)
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") not in ("user", "assistant") or entry.get("isMeta"):
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                text = content if isinstance(content, str) else None
                if isinstance(content, list):
                    pieces = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                            pieces.append(part["text"])
                        elif isinstance(part, dict) and part.get("type") in ("image", "document"):
                            attachments += 1
                    text = "\n".join(pieces)
                if not text:
                    continue
                count += 1
                recent.append({"role": entry["type"], "text": text})
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise bridge.BridgeError("This session changed while its preview was read. Refresh and try again.")
        shown, shortened = preview_messages(list(recent))
        return {"id": identifier, "source": "claude", "title": short_text(row["title"], 200),
                "cwd": row.get("cwd", ""), "updated_at": row.get("updated_at"), "archived": False,
                "running": None, "messages": shown,
                "counts": {"messages": None if start or skipped else count,
                           "preview_messages": len(shown), "text_bytes": sum(len(item["text"].encode()) for item in shown),
                           "attachments_omitted": attachments},
                "truncated": shortened or count > MAX_PREVIEW_MESSAGES or bool(start) or bool(skipped),
                "note": "Recent saved user and assistant text only. Tools, reasoning, and attachments are excluded. Large sessions use a bounded recent preview."}

    def details(self, request):
        source, identifier = request.get("source"), request.get("id")
        if source not in bridge.NAMES or not isinstance(identifier, str):
            raise bridge.BridgeError("Choose a saved coding chat to preview.")
        if source == "claude":
            if not re.fullmatch(r"claude:[0-9a-f]{64}", identifier):
                raise bridge.BridgeError("The selected Claude Code session identifier is invalid.")
            detail = self.claude_details(identifier)
        else:
            if source not in self.profiles or not bridge.canonical_uuid(identifier):
                raise bridge.BridgeError("The selected Codex chat identifier is invalid.")
            row, _, messages, counts = self.backend.read_codex(source, identifier)
            shown, clipped = preview_messages(messages)
            detail = dict(bridge.public_row(row), source=source, messages=shown,
                          counts={key: counts[key] for key in ("messages", "text_bytes", "attachments_omitted")},
                          truncated=clipped, note="Recent saved user and assistant text only. Source history remains complete; tools, reasoning, and attachments are excluded.")
        return {"ok": True, "message": "Saved conversation preview.", "detail": detail}

    def handle(self, request):
        if not isinstance(request, dict):
            raise bridge.BridgeError("The library request must be an object.")
        command = request.get("command")
        if command == "activity":
            return self.activity()
        if command == "backups":
            return self.backups()
        if command == "details":
            return self.details(request)
        raise bridge.BridgeError("Choose a supported library view.")


def main():
    try:
        data = sys.stdin.buffer.read(bridge.MAX_REQUEST + 1)
        if len(data) > bridge.MAX_REQUEST:
            raise bridge.BridgeError("The library request is too large.")
        response = Library().handle(json.loads(data))
    except (bridge.BridgeError, OSError, ValueError, sqlite3.Error) as error:
        response = {"ok": False, "message": str(error) if isinstance(error, bridge.BridgeError) else
                    "The library could not read its local files. Saved conversations were not changed."}
    print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
