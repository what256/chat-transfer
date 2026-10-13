#!/usr/bin/env python3
"""User-directed local chat manager. Requests arrive as JSON on stdin."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import sys
import time
import uuid

import handoff
import auto_sync

CONTROL = Path.home() / "Library/Application Support/Arbeit/chat-manager"
PROFILES = dict(auto_sync.PROFILES, personal=Path.home() / "Library/Application Support/ChatGPT Copy/codex")
NAMES = dict(auto_sync.APP_NAMES, personal="ChatGPT Personal")
ORIGINAL_PAIR = frozenset(("chatgpt", "arbeit"))


def inventory(profile):
    db = handoff.read_db(Path(profile) / "state_5.sqlite")
    try:
        rows = [dict(row) for row in db.execute(
            "SELECT id,name,title,cwd,updated_at,archived,history_mode FROM threads "
            "WHERE agent_role IS NULL AND agent_nickname IS NULL "
            "AND id NOT IN (SELECT child_thread_id FROM thread_spawn_edges) "
            "AND source NOT LIKE '%subagent%' ORDER BY updated_at DESC")]
    finally:
        db.close()
    return {row["id"]: row for row in rows}


def display_title(row):
    return row.get("name") or row.get("title", "Untitled chat").split("\n", 1)[0][:120] or "Untitled chat"


def content_fingerprint(snapshot):
    rows = json.loads(json.dumps(snapshot["rows"]))
    for row in rows["state_5.sqlite"]["threads"]:
        row["rollout_path"] = snapshot["rollouts"][row["id"]]["relative"]
        for key in ("archived", "archived_at", "updated_at", "updated_at_ms", "recency_at", "recency_at_ms"):
            row.pop(key, None)
    for database in rows.values():
        for table in database:
            database[table].sort(key=lambda row: json.dumps(row, sort_keys=True))
    return hashlib.sha256(json.dumps({"rows": rows, "rollouts": snapshot["rollouts"]},
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def route(request, profiles):
    source = request.get("source")
    destination = request.get("destination")
    if destination is None:
        destination = "arbeit" if source == "chatgpt" else "chatgpt"
    if (source not in profiles or destination not in profiles or source == destination
            or source not in NAMES or destination not in NAMES):
        raise handoff.HandoffError("Choose two different installed Codex profiles.")
    return source, destination


def list_chats(source, profiles=PROFILES, destination=None):
    source, other = route({"source": source, "destination": destination}, profiles)
    local, remote = inventory(profiles[source]), inventory(profiles[other])
    catalogs = {source: local, other: remote}
    for name, profile in profiles.items():
        if name not in catalogs:
            try:
                catalogs[name] = inventory(profile)
            except (handoff.HandoffError, sqlite3.Error, OSError):
                # An unavailable, unselected profile does not hide the source library.
                pass
    chats = []
    for identifier, row in local.items():
        running = auto_sync.latest_busy(profiles[source], {identifier})["busy"]
        chats.append({"id": identifier, "title": display_title(row),
                      "present_in_both": identifier in remote, "running": running,
                      "present_in_destination": identifier in remote, "cwd": row.get("cwd", ""),
                      "updated_at": row["updated_at"], "archived": bool(row["archived"]),
                      "profiles": [key for key, catalog in catalogs.items() if identifier in catalog],
                      "eligibility": "Saved history needs preparation" if row["history_mode"] != "paginated" else ""})
    return {"ok": True, "message": "Choose chats and an action.", "chats": chats,
            "source": source, "destination": other,
            "default_ids": [], "needsClose": [], "canRestart": []}


def validate_request(request, profiles):
    source, destination = route(request, profiles)
    operation = request.get("operation")
    if operation not in ("copy", "sync", "move"):
        raise handoff.HandoffError("Choose a source app and Copy, Sync, or Move.")
    ids = request.get("ids")
    if not isinstance(ids, list) or not ids or len(ids) > 50 or any(not isinstance(value, str) for value in ids):
        raise handoff.HandoffError("Choose between one and fifty chats.")
    try:
        if len(set(ids)) != len(ids) or any(str(uuid.UUID(value)) != value for value in ids):
            raise ValueError("Invalid selection")
    except ValueError as error:
        raise handoff.HandoffError("The chat selection contains an invalid or repeated identifier.") from error
    allowed = inventory(profiles[source])
    if any(identifier not in allowed for identifier in ids):
        raise handoff.HandoffError("A selected chat is no longer in the source app.")
    return source, destination, operation, ids, allowed


def pair_namespace(control, identifier, source="chatgpt", destination="arbeit"):
    pair = "-".join(sorted((source, destination)))
    return Path(control) / "pairs" / pair / "chats" / identifier


def state_path(control, identifier, source="chatgpt", destination="arbeit"):
    return pair_namespace(control, identifier, source, destination) / "pair-state.json"


def load_state(control, identifier, source="chatgpt", destination="arbeit"):
    path = state_path(control, identifier, source, destination)
    if not path.exists() and frozenset((source, destination)) == ORIGINAL_PAIR:
        path = Path(control) / "chats" / identifier / "pair-state.json"
    if path.is_symlink():
        raise handoff.HandoffError("The saved chat baseline is redirected.")
    state = json.loads(path.read_text()) if path.exists() else None
    if state is not None and set(state.get("expected", {})) != {source, destination}:
        raise handoff.HandoffError("The saved chat baseline belongs to different profiles.")
    return state


def check_legacy_operation(control, identifier, source, destination):
    if frozenset((source, destination)) != ORIGINAL_PAIR:
        return
    legacy = Path(control) / "chats" / identifier / "operation.json"
    if legacy.is_symlink():
        raise handoff.HandoffError("The earlier transfer journal is redirected.")
    if legacy.exists() and json.loads(legacy.read_text()).get("status") not in ("complete", "rolled_back"):
        raise handoff.HandoffError("An earlier transfer needs recovery before another handoff. Its private backup is retained.")


def seed_legacy_state(profiles, legacy_control):
    """Recover only explicitly recorded roots from a bounded verified baseline."""
    path = Path(legacy_control) / "baseline.json"
    if any(part.is_symlink() for part in (path, *path.parents)):
        return {}
    if not path.is_file() or path.stat().st_size > 256 * 1024:
        return {}
    try:
        with path.open("rb") as stream:
            data = stream.read(256 * 1024 + 1)
        if len(data) > 256 * 1024:
            return {}
        baseline = json.loads(data)
        if not isinstance(baseline, dict):
            return {}
        roots = handoff.validate_roots(baseline.get("roots"))
        recorded_profiles = baseline.get("profiles")
        if (not isinstance(recorded_profiles, list)
                or len(recorded_profiles) != len(profiles)
                or any(not isinstance(value, str) for value in recorded_profiles)
                or set(recorded_profiles) != {str(Path(value)) for value in profiles.values()}
                or not isinstance(baseline.get("fingerprint"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", baseline["fingerprint"])):
            return {}
    except (OSError, ValueError, handoff.HandoffError):
        return {}
    for profile in profiles.values():
        try:
            before = handoff.collect(Path(profile), roots)
            if before["fingerprint"] != baseline["fingerprint"]:
                continue
            states = {}
            for identifier in roots:
                snap = handoff.collect(Path(profile), (identifier,))
                states[identifier] = {"expected": {name: snap["fingerprint"] for name in profiles},
                                      "content": content_fingerprint(snap), "seeded_from_verified_legacy": True}
            if handoff.collect(Path(profile), roots)["fingerprint"] == before["fingerprint"]:
                return states
        except (OSError, ValueError, sqlite3.Error, handoff.HandoffError):
            continue
    return {}


def friendly_error(error):
    message = str(error)
    if "Stop the selected" in message:
        return "Stop the selected chat and its subagents before transferring."
    if "project or section" in message:
        return "This chat belongs to a project or section whose transfer needs preparation."
    if "attached resources" in message:
        return "This chat has attached resources whose transfer needs preparation."
    if "records in" in message:
        return "This chat has queued work, a goal, or saved memory that needs preparation."
    if "projected" in message:
        return "This chat's saved history is still being prepared. Wait, then refresh."
    return message


def prepare(request, control=CONTROL, profiles=PROFILES, legacy_control=None):
    source, other, operation, ids, local = validate_request(request, profiles)
    remote = inventory(profiles[other])
    preview, plans, closed = [], [], set()
    requires_replacement = False
    seeds = None
    fingerprints = {}
    for identifier in ids:
        title = display_title(local[identifier])
        item = {"id": identifier, "title": title, "direction": f"{NAMES[source]} → {NAMES[other]}",
                "effect": operation, "status": "ready", "message": ""}
        try:
            check_legacy_operation(control, identifier, source, other)
            snapshots = {source: handoff.collect(Path(profiles[source]), (identifier,))}
            if identifier in remote:
                snapshots[other] = handoff.collect(Path(profiles[other]), (identifier,))
                if operation != "sync" and not set(snapshots[other]["ids"]) <= set(snapshots[source]["ids"]):
                    raise handoff.HandoffError("The destination has additional saved subagent history. "
                                               "Replacing this version needs preparation; both copies will be kept.")
            fingerprints[identifier] = {name: snapshot["fingerprint"] for name, snapshot in snapshots.items()}
            state = load_state(control, identifier, source, other)
            if (state is None and len(snapshots) == 2
                    and frozenset((source, other)) == ORIGINAL_PAIR):
                if seeds is None:
                    seeds = seed_legacy_state({name: profiles[name] for name in ORIGINAL_PAIR}, legacy_control or
                                              Path.home() / "Library/Application Support/Arbeit/chat-handoff")
                state = seeds.get(identifier)
            origin, target = source, other
            if len(snapshots) == 2:
                same_content = content_fingerprint(snapshots[source]) == content_fingerprint(snapshots[other])
                expected = state.get("expected", {}) if state else {}
                changed = {key: snapshots[key]["fingerprint"] != expected.get(key) for key in snapshots}
                if operation == "sync":
                    if same_content:
                        if remote[identifier]["archived"]:
                            item["message"] = f"Show the synced copy in {NAMES[other]}."
                        else:
                            item.update(effect="no-change", message="Both copies already have the same saved history.")
                            preview.append(item)
                            continue
                    elif state and changed[source] and not changed[other]:
                        pass
                    elif state and changed[other] and not changed[source]:
                        origin, target = other, source
                    else:
                        raise handoff.HandoffError("The histories differ and have no safe common baseline. Both copies will be kept.")
                elif not same_content and (state is None or changed[other]):
                    requires_replacement = True
                    item["effect"] = "replace"
                    if request.get("replace_existing") is not True:
                        item.update(status="confirmation", message=f"Different history exists in {NAMES[other]}. "
                                    "Choose explicitly whether to replace it; the old version will be backed up.")
                        preview.append(item)
                        continue
            item["direction"] = f"{NAMES[origin]} → {NAMES[target]}"
            if target in snapshots and not set(snapshots[target]["ids"]) <= set(snapshots[origin]["ids"]):
                raise handoff.HandoffError("The destination has additional saved subagent history. "
                                           "Replacing this version needs preparation; both copies will be kept.")
            if target not in snapshots:
                for record in snapshots[origin]["rollouts"].values():
                    if not record.get("prefix") and (Path(profiles[target]) / record["relative"]).exists():
                        raise handoff.HandoffError("This chat's file already exists as an inherited snapshot in the destination. "
                                                   "Making it a separate chat needs preparation.")
            item["message"] = (f"Copy the saved history into {NAMES[target]}." if operation != "move" else
                               f"Copy to {NAMES[target]}, verify it, then hide this chat in {NAMES[origin]}. The source history stays recoverable.")
            if item["effect"] == "replace":
                item["message"] += " The different destination history will be replaced, with a private recovery backup."
            plans.append({"id": identifier, "origin": origin, "target": target, "operation": operation,
                          "source_fingerprint": snapshots[origin]["fingerprint"],
                          "target_fingerprint": snapshots[target]["fingerprint"] if target in snapshots else None,
                          "state": state})
            closed.add(target)
            if operation == "move":
                closed.add(origin)
        except (handoff.HandoffError, sqlite3.Error, OSError, ValueError) as error:
            item.update(status="blocked", effect="conflict", message=friendly_error(error))
        preview.append(item)
    needs_close = [key for key in sorted(closed) if not handoff.target_is_closed(Path(profiles[key]))]
    can_restart = [key for key in needs_close if not auto_sync.latest_busy(Path(profiles[key]))["busy"]]
    blocked = any(item["status"] == "blocked" for item in preview)
    reviewed = {"source": source, "destination": other, "operation": operation, "ids": ids,
                "replace_existing": request.get("replace_existing") is True, "fingerprints": fingerprints,
                "effects": [{key: row.get(key) for key in ("id", "direction", "effect", "status")} for row in preview]}
    token = hashlib.sha256(json.dumps(reviewed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"ok": True, "blocked": blocked, "source": source, "destination": other,
            "selection_token": token,
            "message": "Resolve the blocked chats before continuing." if blocked else
                       ("The required apps need to reload." if needs_close else "Ready to transfer the selected chats."),
            "preview": preview, "plans": plans, "needsClose": needs_close, "canRestart": can_restart,
            "chats": [], "requiresReplacement": requires_replacement and request.get("replace_existing") is not True}


def set_archived(profile, identifier, archived, recovery):
    profile, recovery = Path(profile), Path(recovery)
    if not handoff.target_is_closed(profile):
        raise handoff.HandoffError("Close the app before changing this chat's visibility.")
    handoff.private_dir(recovery)
    handoff.backup_db(profile / "state_5.sqlite", recovery / "state_5.sqlite")
    db = sqlite3.connect(profile / "state_5.sqlite", timeout=10)
    try:
        db.execute("BEGIN IMMEDIATE")
        if not handoff.target_is_closed(profile, ignore_pid=os.getpid()):
            raise handoff.HandoffError("The app reopened before the visibility change.")
        result = db.execute("UPDATE threads SET archived=?,archived_at=? WHERE id=?",
                            (1 if archived else 0, int(time.time()) if archived else None, identifier))
        if result.rowcount != 1:
            raise handoff.HandoffError("The selected source chat is no longer available.")
        db.commit()
    finally:
        db.close()


def execute(request, control=CONTROL, profiles=PROFILES, legacy_control=None):
    control = Path(control)
    handoff.private_dir(control)
    with (control / "manager.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prepared = prepare(request, control, profiles, legacy_control)
        supplied_token = request.get("selection_token")
        if supplied_token is not None and (not isinstance(supplied_token, str)
                or supplied_token != prepared["selection_token"]):
            raise handoff.HandoffError("A chat or the selected route changed after review. Nothing was transferred; review a fresh preview.")
        if prepared["blocked"] or prepared["needsClose"] or prepared["requiresReplacement"]:
            return dict(prepared, ok=False)
        source_key, destination_key = route(request, profiles)
        activity_dir = control / "activity"
        handoff.private_dir(activity_dir)
        operation_id = str(uuid.uuid4())
        activity_path = activity_dir / (operation_id + ".json")
        activity = {"id": operation_id, "created_at": time.time(), "source": source_key,
                    "destination": destination_key, "action": request["operation"],
                    "title": prepared["preview"][0]["title"] if len(prepared["preview"]) == 1 else
                             f"{len(prepared['preview'])} selected chats",
                    "status": "running", "message": "Saving and verifying the reviewed chats.",
                    "items": [{"id": row["id"], "title": row["title"],
                               "status": "unchanged" if row["effect"] == "no-change" else "pending",
                               "verified": row["effect"] == "no-change"} for row in prepared["preview"]],
                    "paths": []}
        handoff.write_json(activity_path, activity)
        results = []
        current = None
        namespace = None
        previous_journal = None
        try:
            for plan in prepared["plans"]:
                identifier, origin, target = plan["id"], plan["origin"], plan["target"]
                current = next(row for row in activity["items"] if row["id"] == identifier)
                current.update(status="running", origin=origin, target=target)
                handoff.write_json(activity_path, activity)
                namespace = pair_namespace(control, identifier, origin, target)
                handoff.private_dir(namespace)
                journal_path = namespace / "operation.json"
                previous_journal = journal_path.read_text() if journal_path.is_file() and not journal_path.is_symlink() else None
                source, destination = Path(profiles[origin]), Path(profiles[target])
                source_before = handoff.collect(source, (identifier,))
                if source_before["fingerprint"] != plan["source_fingerprint"]:
                    raise handoff.HandoffError("The source changed after the preview. Refresh and try again.")
                if plan["target_fingerprint"] is not None:
                    destination_before = handoff.collect(destination, (identifier,))
                    if destination_before["fingerprint"] != plan["target_fingerprint"]:
                        raise handoff.HandoffError("The destination changed after the preview. Both histories were kept.")
                    handoff.write_json(namespace / "baseline.json", {
                        "roots": [identifier], "profiles": [str(source), str(destination)],
                        "fingerprint": destination_before["fingerprint"], "ids": destination_before["ids"]})
                report = handoff.transfer(source, destination, namespace, roots=(identifier,), allowed_roots=(identifier,),
                                          require_closed=True)
                if report.get("backup"):
                    activity["paths"].append(report["backup"])
                current["status"] = "copied"
                handoff.write_json(activity_path, activity)
                stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
                target_snapshot = handoff.collect(destination, (identifier,))
                target_root = next(row for row in target_snapshot["rows"]["state_5.sqlite"]["threads"] if row["id"] == identifier)
                if target_root.get("archived"):
                    recovery = namespace / "visibility-backups" / (stamp + "-target")
                    set_archived(destination, identifier, False, recovery)
                    activity["paths"].append(str(recovery))
                    target_snapshot = handoff.collect(destination, (identifier,))
                if content_fingerprint(source_before) != content_fingerprint(target_snapshot):
                    raise handoff.HandoffError("The destination history did not verify. The source copy was kept.")
                journal = {"id": identifier, "operation": plan["operation"], "origin": origin,
                           "target": target, "status": "copied", "backup": report.get("backup"), "updated_at": time.time()}
                handoff.write_json(namespace / "move-journal.json", journal)
                if plan["operation"] == "move":
                    if handoff.collect(source, (identifier,))["fingerprint"] != source_before["fingerprint"]:
                        raise handoff.HandoffError("The source changed before hiding it. Both copies were kept.")
                    recovery = namespace / "visibility-backups" / (stamp + "-source")
                    set_archived(source, identifier, True, recovery)
                    activity["paths"].append(str(recovery))
                source_after = handoff.collect(source, (identifier,))
                destination_after = handoff.collect(destination, (identifier,))
                state = {"expected": {origin: source_after["fingerprint"], target: destination_after["fingerprint"]},
                         "content": content_fingerprint(destination_after), "updated_at": time.time()}
                handoff.write_json(namespace / "pair-state.json", state)
                journal["status"] = "complete"
                handoff.write_json(namespace / "move-journal.json", journal)
                current.update(status="completed", verified=True)
                handoff.write_json(activity_path, activity)
                results.append({"id": identifier, "origin": origin, "target": target, "operation": plan["operation"],
                                "verified": True, "backup": report.get("backup")})
        except Exception as error:
            incomplete_write = current is not None and current["status"] == "copied"
            if current is not None:
                current.update(status="failed", verified=False)
            for item in activity["items"]:
                if item["status"] == "pending":
                    item["status"] = "not-run"
            if namespace is not None:
                journal_path = namespace / "operation.json"
                if journal_path.is_file() and not journal_path.is_symlink():
                    journal_text = journal_path.read_text()
                    if journal_text != previous_journal:
                        recovery_journal = json.loads(journal_text)
                        incomplete_write = incomplete_write or recovery_journal.get("status") in ("committed", "complete")
                        recovery = recovery_journal.get("backup")
                        if recovery and str(recovery) not in activity["paths"]:
                            activity["paths"].append(str(recovery))
            message = friendly_error(error) if isinstance(error, (handoff.HandoffError, sqlite3.Error, OSError, ValueError)) else "The transfer stopped unexpectedly. Review its private recovery copies before retrying."
            activity.update(status="partial" if results or incomplete_write else "failed", message=message)
            handoff.write_json(activity_path, activity)
            return {"ok": False, "blocked": True, "partial": activity["status"] == "partial", "message": message,
                    "results": results, "activity_id": operation_id, "needsClose": [], "canRestart": [],
                    "preview": prepared["preview"]}
        activity.update(status="completed", message=f"Finished. {len(results)} chat(s) transferred and verified.")
        handoff.write_json(activity_path, activity)
        return {"ok": True, "message": f"Finished. {len(results)} chat(s) transferred and verified.",
                "results": results, "activity_id": operation_id,
                "needsClose": [], "canRestart": [], "preview": prepared["preview"]}


def dispatch(request, control=CONTROL, profiles=PROFILES, legacy_control=None):
    if request.get("command") == "list":
        source, destination = route(request, profiles)
        return list_chats(source, profiles, destination)
    if request.get("command") == "prepare":
        return prepare(request, control, profiles, legacy_control)
    if request.get("command") == "execute":
        return execute(request, control, profiles, legacy_control)
    raise handoff.HandoffError("Unknown chat manager action.")


def main():
    try:
        request = json.loads(sys.stdin.read(1024 * 1024))
        result = dispatch(request)
    except (handoff.HandoffError, sqlite3.Error, OSError, ValueError) as error:
        result = {"ok": False, "blocked": True, "message": friendly_error(error),
                  "needsClose": [], "canRestart": [], "preview": [], "chats": []}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
