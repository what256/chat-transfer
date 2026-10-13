#!/usr/bin/env python3
"""Reversible, explicitly selected local chat handoffs between isolated profiles.

This is a local migration helper, not a vendor sync feature. The destination
application must be closed; selected source conversations must be stopped.
Credentials, preferences, projects and unrelated conversations are never imported.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

ROOTS = ()  # Public releases never embed selected chat identifiers.
STATE_TABLES = {
    "threads": "id",
    "thread_dynamic_tools": "thread_id",
    "thread_attachments": "thread_id",
    "thread_spawn_edges": "parent_thread_id",
}
HISTORY_TABLES = {
    "thread_turns": "thread_id",
    "thread_items": "thread_id",
    "thread_realtime_items": "thread_id",
    "thread_history_projection_state": "thread_id",
}
TABLES = {"state_5.sqlite": STATE_TABLES, "thread_history_1.sqlite": HISTORY_TABLES}


class HandoffError(RuntimeError):
    pass


def sync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    sync_dir(path.parent)


def private_dir(path: Path) -> None:
    if path.resolve() != path.absolute():
        raise HandoffError("A handoff directory is redirected.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    sync_dir(path)
    sync_dir(path.parent)


def write_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise HandoffError("A handoff record is redirected.")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)
    sync_dir(path.parent)


def digest_file(path: Path, limit=None) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        remaining = limit
        while remaining is None or remaining > 0:
            block = handle.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
            if not block:
                if remaining:
                    raise HandoffError("A required lineage prefix is truncated.")
                break
            value.update(block)
            if remaining is not None:
                remaining -= len(block)
    return value.hexdigest()


def read_db(path: Path, *, immutable=False) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise HandoffError(f"Missing or redirected database: {path.name}")
    # Apple's system SQLite cannot always open a closed WAL database read-only
    # after its last writer removed the WAL. Without a WAL, the base file is the
    # complete checkpoint; a fresh snapshot later still fences source changes.
    immutable = immutable or not Path(str(path) + "-wal").exists()
    uri = path.as_uri() + ("?immutable=1" if immutable else "?mode=ro")
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("BEGIN")
    return connection


def columns(connection, table):
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def schema(connection, tables):
    return {table: [tuple(row) for row in connection.execute(f'PRAGMA table_info("{table}")')]
            for table in tables}


def selected_rows(connection, table, field, ids):
    marks = ",".join("?" for _ in ids)
    return [dict(row) for row in connection.execute(
        f'SELECT * FROM "{table}" WHERE "{field}" IN ({marks})', sorted(ids))]


def scope_ids(connection, roots):
    ids = set(roots)
    while True:
        edges = selected_rows(connection, "thread_spawn_edges", "parent_thread_id", ids)
        children = {row["child_thread_id"] for row in edges}
        if children <= ids:
            return ids
        ids |= children


def unrelated_digest(connection, tables, ids):
    value = hashlib.sha256()
    marks = ",".join("?" for _ in ids)
    all_tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence' ORDER BY name")]
    for table in all_tables:
        fields = columns(connection, table)
        order = ",".join(f'"{field}"' for field in fields)
        query = f'SELECT * FROM "{table}"'
        parameters = []
        if table in tables:
            query += f' WHERE "{tables[table]}" NOT IN ({marks})'
            parameters = sorted(ids)
        query += f' ORDER BY {order}'
        value.update(table.encode())
        for row in connection.execute(query, parameters):
            # sqlx migration checksums are blobs; repr retains byte identity.
            value.update(repr(tuple(row)).encode())
            value.update(b"\n")
    return value.hexdigest()


def safe_rollout(home: Path, path: str):
    rollout = Path(path)
    try:
        relative = rollout.relative_to(home)
    except ValueError as error:
        raise HandoffError("A selected rollout is outside its own profile.") from error
    if not relative.parts or relative.parts[0] not in ("sessions", "archived_sessions") or ".." in relative.parts:
        raise HandoffError("A selected rollout has an unexpected path.")
    if rollout.is_symlink() or not rollout.is_file():
        raise HandoffError("A selected rollout is missing or redirected.")
    if rollout.resolve() != rollout.absolute():
        raise HandoffError("A selected rollout directory is redirected.")
    return relative


def history_base(path):
    with path.open("rb") as handle:
        event = json.loads(handle.readline())
    if event.get("type") != "session_meta":
        raise HandoffError("A selected rollout has no session header.")
    return event.get("payload", {}).get("history_base")


def lineage_rollouts(home, selected):
    """Resolve inherited paginated history by rollout ID, without extra chats.

    history_base IDs identify immutable rollout snapshots, which may have no
    state-table row. Copy only the byte prefix required by the fork.
    """
    todo = [base for record in selected.values()
            if (base := history_base(home / record["relative"]))]
    required = {}
    links = {}
    while todo:
        base = todo.pop()
        identifier, size = base.get("thread_id"), base.get("end_byte_offset")
        try:
            uuid.UUID(identifier)
        except (ValueError, TypeError, AttributeError) as error:
            raise HandoffError("A history lineage has an invalid rollout identifier.") from error
        if not isinstance(size, int) or size <= 0:
            raise HandoffError("A history lineage has an invalid byte boundary.")
        if identifier in required:
            if size > required[identifier]["size"]:
                required[identifier]["size"] = size
                required[identifier]["end_ordinal"] = base.get("end_ordinal_exclusive")
            continue
        matches = []
        for folder in ("sessions", "archived_sessions"):
            matches.extend((home / folder).rglob("*" + identifier + ".jsonl"))
        if len(matches) != 1:
            raise HandoffError(f"Missing or ambiguous source rollout for history lineage {identifier}.")
        path = matches[0]
        relative = safe_rollout(home, str(path))
        required[identifier] = {"relative": str(relative), "size": size, "prefix": True,
                                "end_ordinal": base.get("end_ordinal_exclusive")}
        if parent := history_base(path):
            links[identifier] = parent.get("thread_id")
            todo.append(parent)
    for identifier in links:
        seen = set()
        current = identifier
        while current in links:
            if current in seen:
                raise HandoffError("History lineage contains a cycle.")
            seen.add(current)
            current = links[current]
    result = {}
    selected_paths = {row["relative"]: row for row in selected.values()}
    for identifier, record in required.items():
        path = home / record["relative"]
        if record["size"] > path.stat().st_size:
            raise HandoffError("A required lineage prefix is truncated.")
        with path.open("rb") as handle:
            handle.seek(record["size"] - 1)
            if handle.read(1) != b"\n":
                raise HandoffError("A lineage byte boundary splits a saved event.")
        parent = history_base(path)
        inherited = parent.get("end_ordinal_exclusive", 0) if parent else 0
        count = 0
        with path.open("rb") as handle:
            remaining = record["size"]
            while remaining:
                block = handle.read(min(1024 * 1024, remaining))
                remaining -= len(block)
                count += block.count(b"\n")
        if count + inherited != record["end_ordinal"]:
            raise HandoffError("A lineage ordinal does not match its saved byte boundary.")
        if record["relative"] in selected_paths:
            continue
        record["sha256"] = digest_file(path, record["size"])
        result["lineage:" + identifier] = record
    return result


def auxiliary_check(home, ids):
    # Transfer neither queued requests nor goals that could restart on their own.
    # These exact chats currently have no extracted memories or goals either.
    for db, table in (("queue_1.sqlite", "queued_items"),
                      ("goals_1.sqlite", "thread_goals"),
                      ("memories_1.sqlite", "stage1_outputs")):
        path = home / db
        if not path.exists():
            continue
        immutable = db == "memories_1.sqlite" and not Path(str(path) + "-wal").exists()
        connection = read_db(path, immutable=immutable)
        try:
            if selected_rows(connection, table, "thread_id", ids):
                raise HandoffError(f"Selected chats have records in {db}; this handoff needs review first.")
        finally:
            connection.close()



def validate_roots(roots):
    """Require a bounded, canonical, nonempty selection before any profile read."""
    if not isinstance(roots, (tuple, list, set, frozenset)) or not 1 <= len(roots) <= 50:
        raise HandoffError("Choose between one and fifty explicitly selected chats.")
    values = tuple(roots)
    try:
        if len(set(values)) != len(values) or any(
                not isinstance(value, str) or str(uuid.UUID(value)) != value for value in values):
            raise ValueError("Invalid selection")
    except (ValueError, TypeError, AttributeError) as error:
        raise HandoffError("A selected chat identifier is invalid or repeated.") from error
    return values

def collect(home: Path, roots=None, *, include_lineage=True):
    """Read only the selected rows and their recorded subagent dependencies."""
    roots = validate_roots(ROOTS if roots is None else roots)
    home = home.absolute()
    connections = {name: read_db(home / name) for name in TABLES}
    try:
        ids = scope_ids(connections["state_5.sqlite"], roots)
        rows = {
            name: {table: selected_rows(connections[name], table, field, ids)
                   for table, field in tables.items()}
            for name, tables in TABLES.items()
        }
        threads = rows["state_5.sqlite"]["threads"]
        present = {row["id"] for row in threads}
        if not set(roots) <= present:
            raise HandoffError("The source does not contain every selected chat.")
        if present != ids:
            raise HandoffError("A saved subagent dependency is missing.")
        if any(row["project_id"] is not None or row["thread_section_id"] is not None
               for row in threads):
            raise HandoffError("A selected chat acquired a project or section; review its references first.")
        if rows["state_5.sqlite"]["thread_attachments"]:
            raise HandoffError("A selected chat acquired attached resources; review those references first.")
        turns = rows["thread_history_1.sqlite"]["thread_turns"]
        if any(row["status"] not in ("completed", "interrupted", "failed") for row in turns):
            raise HandoffError("Stop the selected chats and their subagents before transferring.")
        auxiliary_check(home, ids)
        rollouts = {}
        projection = {row["thread_id"]: row for row in
                      rows["thread_history_1.sqlite"]["thread_history_projection_state"]}
        for row in threads:
            path = Path(row["rollout_path"])
            relative = safe_rollout(home, row["rollout_path"])
            before = path.stat()
            sha = digest_file(path)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise HandoffError("Selected history changed during the snapshot; stop it and retry.")
            offset = projection.get(row["id"], {}).get("next_rollout_byte_offset")
            if offset != after.st_size:
                raise HandoffError("Selected history is still being projected. Wait for both chats to finish stopping and retry.")
            rollouts[row["id"]] = {"relative": str(relative), "sha256": sha,
                                   "size": after.st_size}
        if include_lineage:
            rollouts.update(lineage_rollouts(home, rollouts))
        normalized = json.loads(json.dumps(rows))
        for row in normalized["state_5.sqlite"]["threads"]:
            row["rollout_path"] = rollouts[row["id"]]["relative"]
        for database in normalized.values():
            for table in database:
                database[table].sort(key=lambda row: json.dumps(row, sort_keys=True))
        fingerprint = hashlib.sha256(json.dumps(
            {"rows": normalized, "rollouts": rollouts}, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        return {"ids": sorted(ids), "rows": rows, "rollouts": rollouts,
                "fingerprint": fingerprint,
                "schemas": {name: schema(connection, TABLES[name])
                            for name, connection in connections.items()}}
    finally:
        for connection in connections.values():
            connection.close()


def destination_present(home, roots):
    connection = read_db(home / "state_5.sqlite")
    try:
        return len(selected_rows(connection, "threads", "id", roots))
    finally:
        connection.close()


def target_is_closed(home, *, ignore_pid=None):
    # Inspect executable paths, not command arguments or process environments.
    paths = subprocess.check_output(["/bin/ps", "-axo", "comm="], text=True).splitlines()
    executables = {
        Path.home() / ".codex": "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
        Path.home() / "Library/Application Support/Arbeit/codex":
            "/Applications/ChatGPT Arbeit.app/Contents/Helpers/ChatGPT.app/Contents/MacOS/ChatGPT",
        Path.home() / "Library/Application Support/ChatGPT Copy/codex":
            "/Applications/ChatGPT Personal.app/Contents/Helpers/ChatGPT.app/Contents/MacOS/ChatGPT",
    }
    executable = executables.get(Path(home))
    if executable is not None and executable in paths:
        return False
    # The vendor may resolve the coding binary from the original app even in
    # Arbeit. Its open database handles identify its profile without reading env.
    for name in TABLES:
        result = subprocess.run(["/usr/sbin/lsof", "-t", "--", str(home / name)],
                                capture_output=True, text=True)
        holders = [line.strip() for line in result.stdout.splitlines()
                   if line.strip() and line.strip() != str(ignore_pid)]
        if result.returncode not in (0, 1) or holders:
            return False
    return True


def backup_db(source, destination):
    connection = read_db(source)
    copy = sqlite3.connect(destination)
    try:
        connection.backup(copy)
    finally:
        copy.close()
        connection.close()
    destination.chmod(0o600)
    sync_file(destination)


def insert_rows(connection, alias, table, rows):
    if not rows:
        return
    names = list(rows[0])
    statement = (f'INSERT INTO {alias}."{table}" (' +
                 ",".join(f'"{name}"' for name in names) + ") VALUES (" +
                 ",".join("?" for _ in names) + ")")
    connection.executemany(statement, [tuple(row[name] for name in names) for row in rows])


def transfer(source: Path, destination: Path, control: Path, *, roots=None,
             require_closed=True, intent_check=None, allowed_roots=None):
    """Import exactly one scope; all other destination rows remain intact."""
    roots = validate_roots(ROOTS if roots is None else roots)
    if allowed_roots is None or not set(roots) <= set(allowed_roots):
        raise HandoffError("The selected chats were not explicitly authorized.")
    source, destination, control = source.absolute(), destination.absolute(), control.absolute()
    if source == destination or source.resolve() != source or destination.resolve() != destination:
        raise HandoffError("Profiles must be separate real directories.")
    if require_closed and not target_is_closed(destination):
        raise HandoffError("Quit the destination app first so it can reload the transferred history.")
    private_dir(control)
    lock = (control / "handoff.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock.close()
        raise HandoffError("Another handoff is already running.") from error
    try:
        return _transfer(source, destination, control, roots, require_closed, intent_check)
    finally:
        lock.close()


def _transfer(source, destination, control, roots, require_closed, intent_check=None):
    if intent_check is not None and not intent_check():
        raise HandoffError("The app switch changed before handoff.")
    journal_path = control / "operation.json"
    if journal_path.exists():
        operation_state = json.loads(journal_path.read_text())
        if operation_state.get("status") == "committed":
            return finish_committed(source, destination, control, operation_state, roots)
        if operation_state.get("status") not in ("complete", "rolled_back"):
            raise HandoffError("An earlier transfer was interrupted. Restore its private backup before another handoff.")
    incoming = collect(source, roots)
    baseline_path = control / "baseline.json"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else None
    if baseline is not None and (baseline.get("roots") != list(roots) or
                                set(baseline.get("profiles", [])) != {str(source), str(destination)}):
        raise HandoffError("The handoff baseline belongs to different profiles or chats.")
    present = destination_present(destination, roots)
    previous = None
    if present:
        previous = collect(destination, roots)
        if previous["fingerprint"] == incoming["fingerprint"]:
            write_json(baseline_path, {"roots": list(roots), "profiles": [str(source), str(destination)],
                                      "fingerprint": incoming["fingerprint"], "ids": incoming["ids"]})
            return {"changed": False, "roots": list(roots), "saved_threads": len(incoming["ids"])}
        if baseline is None or previous["fingerprint"] != baseline["fingerprint"]:
            raise HandoffError("Both apps have different history. Nothing was overwritten; choose which work to keep.")
        if not set(previous["ids"]) <= set(incoming["ids"]):
            raise HandoffError("The incoming scope lost a saved dependency; nothing was overwritten.")
    if present not in (0, len(roots)):
        raise HandoffError("The destination has an incomplete copy of the selected chats.")
    auxiliary_check(destination, incoming["ids"])
    for name, tables in TABLES.items():
        connection = read_db(destination / name)
        try:
            if schema(connection, tables) != incoming["schemas"][name]:
                raise HandoffError("The apps have different history schemas. Update Arbeit before transferring.")
            if not previous and any(selected_rows(connection, table, field, incoming["ids"])
                                    for table, field in tables.items()):
                raise HandoffError("The destination already contains conflicting dependency records.")
        finally:
            connection.close()
    index = destination / "session_index.jsonl"
    if index.is_symlink():
        raise HandoffError("The destination session index is redirected.")
    for record in incoming["rollouts"].values():
        target = destination / record["relative"]
        if target.parent.resolve() != target.parent.absolute() or target.is_symlink():
            raise HandoffError("A destination rollout path is redirected.")
        if record.get("prefix") and target.exists():
            existing_size = target.stat().st_size
            if existing_size >= record["size"]:
                matches = digest_file(target, record["size"]) == record["sha256"]
            else:
                matches = digest_file(target) == digest_file(source / record["relative"], existing_size)
            if not matches:
                raise HandoffError("A destination lineage prefix conflicts with the incoming history.")
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    operation = control / "backups" / stamp
    private_dir(operation)
    stage = operation / "incoming"
    private_dir(stage)
    saved = operation / "destination"
    private_dir(saved)
    for name in TABLES:
        backup_db(destination / name, saved / name)
    if index.exists():
        shutil.copyfile(index, saved / index.name)
        (saved / index.name).chmod(0o600)
        sync_file(saved / index.name)
    if previous:
        for record in previous["rollouts"].values():
            relative = Path(record["relative"])
            backup_path = saved / relative
            private_dir(backup_path.parent)
            shutil.copyfile(destination / relative, backup_path)
            backup_path.chmod(0o600)
            sync_file(backup_path)
    for record in incoming["rollouts"].values():
        relative = Path(record["relative"])
        existing = destination / relative
        backup_path = saved / relative
        if record.get("prefix") and existing.exists() and not backup_path.exists():
            private_dir(backup_path.parent)
            shutil.copyfile(existing, backup_path)
            backup_path.chmod(0o600)
            sync_file(backup_path)
    for record in incoming["rollouts"].values():
        relative = Path(record["relative"])
        target = stage / relative
        private_dir(target.parent)
        if record.get("prefix"):
            with (source / relative).open("rb") as reader, target.open("wb") as writer:
                remaining = record["size"]
                while remaining:
                    block = reader.read(min(1024 * 1024, remaining))
                    if not block:
                        raise HandoffError("A required lineage prefix is truncated.")
                    writer.write(block)
                    remaining -= len(block)
        else:
            shutil.copyfile(source / relative, target)
        target.chmod(0o600)
        sync_file(target)
        if digest_file(target) != record["sha256"]:
            raise HandoffError("Selected source history changed during transfer; retry after stopping it.")
    # Re-read the selected scope only; unrelated source work may continue.
    if collect(source, roots)["fingerprint"] != incoming["fingerprint"]:
        raise HandoffError("Selected source history changed during transfer; nothing was imported.")
    if require_closed and not target_is_closed(destination):
        raise HandoffError("The destination app opened during transfer; close it and retry.")
    if previous and collect(destination, roots)["fingerprint"] != previous["fingerprint"]:
        raise HandoffError("Selected destination history changed during transfer; nothing was imported.")
    if intent_check is not None and not intent_check():
        raise HandoffError("The app switch changed before importing history; nothing was imported.")
    journal = {"status": "prepared", "source": str(source), "destination": str(destination),
               "backup": str(operation), "roots": list(roots), "ids": incoming["ids"],
               "fingerprint": incoming["fingerprint"],
               "previous_fingerprint": previous["fingerprint"] if previous else None,
               "rollouts": [record["relative"] for record in incoming["rollouts"].values()]}
    write_json(journal_path, journal)
    # Use an attached rollback-journal transaction for atomic state/history rows.
    # A stopped destination has no active readers, cached thread objects or writers.
    connection = sqlite3.connect(destination / "state_5.sqlite", timeout=10)
    connection.execute("ATTACH DATABASE ? AS history", (str(destination / "thread_history_1.sqlite"),))
    aliases = {"state_5.sqlite": "main", "thread_history_1.sqlite": "history"}
    modes = {alias: connection.execute(f"PRAGMA {alias}.journal_mode").fetchone()[0]
             for alias in aliases.values()}
    installed = []
    committed = False
    try:
        for alias in aliases.values():
            connection.execute(f"PRAGMA {alias}.wal_checkpoint(TRUNCATE)")
            connection.execute(f"PRAGMA {alias}.journal_mode=DELETE")
            connection.execute(f"PRAGMA {alias}.synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        if intent_check is not None and not intent_check():
            raise HandoffError("The app switch changed before the database transaction.")
        auxiliary_check(destination, incoming["ids"])
        if previous:
            if collect(destination, roots)["fingerprint"] != previous["fingerprint"]:
                raise HandoffError("The destination changed before its transaction; nothing was imported.")
        else:
            for name, tables in TABLES.items():
                for table, field in tables.items():
                    marks = ",".join("?" for _ in incoming["ids"])
                    found = connection.execute(
                        f'SELECT 1 FROM {aliases[name]}."{table}" WHERE "{field}" IN ({marks}) LIMIT 1',
                        incoming["ids"]).fetchone()
                    if found:
                        raise HandoffError("The destination acquired selected records; nothing was imported.")
        marks = ",".join("?" for _ in incoming["ids"])
        for name, tables in TABLES.items():
            alias = aliases[name]
            for table, field in reversed(list(tables.items())):
                connection.execute(f'DELETE FROM {alias}."{table}" WHERE "{field}" IN ({marks})',
                                   incoming["ids"])
            for table in tables:
                rows = incoming["rows"][name][table]
                if table == "threads":
                    rows = [dict(row, rollout_path=str(destination / incoming["rollouts"][row["id"]]["relative"]))
                            for row in rows]
                insert_rows(connection, alias, table, rows)
        for record in incoming["rollouts"].values():
            relative = Path(record["relative"])
            target = destination / relative
            private_dir(target.parent)
            if record.get("prefix") and target.exists():
                existing_size = target.stat().st_size
                if existing_size >= record["size"]:
                    if digest_file(target, record["size"]) != record["sha256"]:
                        raise HandoffError("A destination lineage prefix changed during handoff.")
                    # Retain any later, unrelated ancestor bytes in the original.
                    continue
                if digest_file(target) != digest_file(stage / relative, existing_size):
                    raise HandoffError("A cached ancestor prefix changed during handoff.")
            if target.exists() and not record.get("prefix") and (not previous or str(relative) not in
                                    {row["relative"] for row in previous["rollouts"].values()}):
                raise HandoffError("A destination rollout path is occupied by another record.")
            os.replace(stage / relative, target)
            installed.append(relative)
            sync_dir(target.parent)
        for alias in aliases.values():
            if connection.execute(f"PRAGMA {alias}.quick_check").fetchone()[0] != "ok":
                raise HandoffError("Database integrity check failed; handoff was rolled back.")
            if connection.execute(f"PRAGMA {alias}.foreign_key_check").fetchall():
                raise HandoffError("Database references failed validation; handoff was rolled back.")
        connection.commit()
        committed = True
    finally:
        if not committed:
            connection.rollback()
            for relative in reversed(installed):
                backup_path = saved / relative
                if backup_path.exists():
                    shutil.copyfile(backup_path, destination / relative)
                    (destination / relative).chmod(0o600)
                    sync_file(destination / relative)
                else:
                    (destination / relative).unlink(missing_ok=True)
                    sync_dir((destination / relative).parent)
        for alias, mode in modes.items():
            connection.execute(f"PRAGMA {alias}.journal_mode={mode}")
        connection.close()
        if not committed:
            journal["status"] = "rolled_back"
            write_json(journal_path, journal)
    journal["status"] = "committed"
    write_json(journal_path, journal)
    return finish_committed(source, destination, control, journal, roots)


def finish_committed(source, destination, control, journal, roots):
    """Finish verification after a completed database commit, without reimporting."""
    if (journal.get("source") != str(source) or journal.get("destination") != str(destination)
            or journal.get("roots") != list(roots)):
        raise HandoffError("Finish the pending handoff in its original direction first.")
    operation = Path(journal["backup"])
    if operation.parent != control / "backups" or operation.resolve() != operation:
        raise HandoffError("The recovery backup is outside the handoff folder.")
    saved = operation / "destination"
    incoming = collect(source, roots)
    if incoming["fingerprint"] != journal["fingerprint"]:
        raise HandoffError(f"The source resumed during handoff. Destination has the staged snapshot; review backup: {operation}")
    verified = collect(destination, roots)
    if verified["fingerprint"] != incoming["fingerprint"]:
        raise HandoffError(f"Post-transfer verification failed. Private backup: {operation}")
    for name, tables in TABLES.items():
        check = read_db(destination / name)
        backup = read_db(saved / name)
        try:
            if unrelated_digest(check, tables, incoming["ids"]) != unrelated_digest(backup, tables, incoming["ids"]):
                raise HandoffError(f"Unrelated records changed unexpectedly. Private backup: {operation}")
        finally:
            check.close()
            backup.close()
    index = destination / "session_index.jsonl"
    if index.is_symlink():
        raise HandoffError("The destination session index is redirected.")
    with index.open("a", encoding="utf-8") as handle:
        for row in incoming["rows"]["state_5.sqlite"]["threads"]:
            if row["id"] in roots:
                handle.write(json.dumps({"id": row["id"], "thread_name": row["name"] or row["title"],
                                         "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["updated_at"]))}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    index.chmod(0o600)
    write_json(control / "baseline.json", {"roots": list(roots), "profiles": [str(source), str(destination)],
                              "fingerprint": incoming["fingerprint"], "ids": incoming["ids"]})
    report = {"changed": True, "source": str(source), "destination": str(destination),
              "roots": list(roots), "saved_threads": len(incoming["ids"]),
              "rollout_bytes": sum(row["size"] for row in incoming["rollouts"].values()),
              "counts": {name: {table: len(rows) for table, rows in tables.items()}
                         for name, tables in incoming["rows"].items()},
              "fingerprint": incoming["fingerprint"], "backup": str(operation),
              "verified": True, "unrelated_rows_unchanged": True, "automatic_sync": False}
    write_json(operation / "validation.json", report)
    write_json(control / "last-transfer.json", report)
    journal["status"] = "complete"
    write_json(control / "operation.json", journal)
    # Retain only destination backups and the small report, not another incoming copy.
    shutil.rmtree(operation / "incoming", ignore_errors=True)
    return report


