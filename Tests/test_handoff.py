"""Temporary-profile integration tests for selective local chat handoff.

Fixtures contain only synthetic rows; no real profile data is imported or edited.
The schema below was captured from SQLite DDL only, with no source profile rows.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Backend"))

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from unittest import mock

SCHEMA = 'CREATE TABLE _sqlx_migrations (\n    version BIGINT PRIMARY KEY,\n    description TEXT NOT NULL,\n    installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n    success BOOLEAN NOT NULL,\n    checksum BLOB NOT NULL,\n    execution_time BIGINT NOT NULL\n);\nCREATE TABLE backfill_state (\n    id INTEGER PRIMARY KEY CHECK (id = 1),\n    status TEXT NOT NULL,\n    last_watermark TEXT,\n    last_success_at INTEGER,\n    updated_at INTEGER NOT NULL\n);\nCREATE TABLE external_agent_config_imports (\n    import_id TEXT PRIMARY KEY,\n    completed_at_ms INTEGER NOT NULL,\n    successes TEXT NOT NULL,\n    failures TEXT NOT NULL\n, provider_id TEXT);\nCREATE TABLE project_idempotency_keys (\n    key TEXT PRIMARY KEY,\n    project_id TEXT NOT NULL,\n    created_at_ms INTEGER NOT NULL\n);\nCREATE TABLE project_roots (\n    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,\n    position INTEGER NOT NULL,\n    path TEXT NOT NULL,\n    PRIMARY KEY (project_id, position)\n);\nCREATE TABLE projects (\n    id TEXT PRIMARY KEY,\n    name TEXT NOT NULL,\n    metadata TEXT NOT NULL DEFAULT \'{}\',\n    position INTEGER NOT NULL,\n    created_at_ms INTEGER NOT NULL,\n    updated_at_ms INTEGER NOT NULL\n);\nCREATE TABLE remote_control_enrollments (\n    websocket_url TEXT NOT NULL,\n    account_id TEXT NOT NULL,\n    app_server_client_name TEXT NOT NULL,\n    server_id TEXT NOT NULL,\n    environment_id TEXT NOT NULL,\n    server_name TEXT NOT NULL,\n    updated_at INTEGER NOT NULL, remote_control_enabled INTEGER,\n    PRIMARY KEY (websocket_url, account_id, app_server_client_name)\n);\nCREATE TABLE rollout_migration_skipped_rollouts (\n    migration_id TEXT NOT NULL,\n    rollout_path TEXT NOT NULL,\n    rollout_size_bytes INTEGER NOT NULL,\n    rollout_modified_at_ns INTEGER NOT NULL,\n    skip_reason TEXT NOT NULL,\n    skipped_at INTEGER NOT NULL,\n    PRIMARY KEY (migration_id, rollout_path)\n);\nCREATE TABLE rollout_migration_state (\n    migration_id TEXT PRIMARY KEY,\n    last_checked_thread_created_at INTEGER,\n    last_checked_thread_id TEXT,\n    updated_at INTEGER NOT NULL\n);\nCREATE TABLE "thread_attachments" (\n    id TEXT PRIMARY KEY,\n    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,\n    attachment_type TEXT NOT NULL,\n    identity_key TEXT NOT NULL,\n    payload TEXT NOT NULL,\n    created_at INTEGER NOT NULL,\n    UNIQUE (thread_id, attachment_type, identity_key)\n);\nCREATE TABLE thread_dynamic_tools (\n    thread_id TEXT NOT NULL,\n    position INTEGER NOT NULL,\n    name TEXT NOT NULL,\n    description TEXT NOT NULL,\n    input_schema TEXT NOT NULL, defer_loading INTEGER NOT NULL DEFAULT 0, namespace TEXT,\n    PRIMARY KEY(thread_id, position),\n    FOREIGN KEY(thread_id) REFERENCES threads(id) ON DELETE CASCADE\n);\nCREATE TABLE thread_sections (\n    id TEXT PRIMARY KEY,\n    name TEXT NOT NULL\n, appearance TEXT);\nCREATE TABLE thread_spawn_edges (\n    parent_thread_id TEXT NOT NULL,\n    child_thread_id TEXT NOT NULL PRIMARY KEY,\n    status TEXT NOT NULL\n);\nCREATE TABLE threads (\n    id TEXT PRIMARY KEY,\n    rollout_path TEXT NOT NULL,\n    created_at INTEGER NOT NULL,\n    updated_at INTEGER NOT NULL,\n    source TEXT NOT NULL,\n    model_provider TEXT NOT NULL,\n    cwd TEXT NOT NULL,\n    title TEXT NOT NULL,\n    sandbox_policy TEXT NOT NULL,\n    approval_mode TEXT NOT NULL,\n    tokens_used INTEGER NOT NULL DEFAULT 0,\n    has_user_event INTEGER NOT NULL DEFAULT 0,\n    archived INTEGER NOT NULL DEFAULT 0,\n    archived_at INTEGER,\n    git_sha TEXT,\n    git_branch TEXT,\n    git_origin_url TEXT\n, cli_version TEXT NOT NULL DEFAULT \'\', first_user_message TEXT NOT NULL DEFAULT \'\', agent_nickname TEXT, agent_role TEXT, memory_mode TEXT NOT NULL DEFAULT \'enabled\', model TEXT, reasoning_effort TEXT, agent_path TEXT, created_at_ms INTEGER, updated_at_ms INTEGER, thread_source TEXT, preview TEXT NOT NULL DEFAULT \'\', recency_at INTEGER NOT NULL DEFAULT 0, recency_at_ms INTEGER NOT NULL DEFAULT 0, history_mode TEXT NOT NULL DEFAULT \'legacy\', name TEXT, is_pinned INTEGER NOT NULL DEFAULT 0, thread_section_id TEXT\n    REFERENCES thread_sections(id) ON DELETE SET NULL, section_position INTEGER, section_entered_at_ms INTEGER, project_id TEXT\n    REFERENCES projects(id) ON DELETE SET NULL, originator TEXT, daybreak_enabled BOOLEAN, creator_user_id TEXT, creator_account_id TEXT);\n'

HISTORY_SCHEMA = "CREATE TABLE _sqlx_migrations (\n    version BIGINT PRIMARY KEY,\n    description TEXT NOT NULL,\n    installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,\n    success BOOLEAN NOT NULL,\n    checksum BLOB NOT NULL,\n    execution_time BIGINT NOT NULL\n);\nCREATE TABLE thread_history_projection_state (\n    thread_id TEXT PRIMARY KEY,\n    next_rollout_byte_offset INTEGER NOT NULL,\n    next_rollout_ordinal INTEGER NOT NULL\n);\nCREATE TABLE thread_items (\n    thread_id TEXT NOT NULL,\n    turn_id TEXT NOT NULL,\n    item_id TEXT NOT NULL,\n    rollout_ordinal INTEGER NOT NULL,\n    created_at_ms INTEGER NOT NULL,\n    item_json TEXT NOT NULL, item_type TEXT NOT NULL DEFAULT '', updated_at_ordinal INTEGER NOT NULL DEFAULT 0, started_at_ms INTEGER, completed_at_ms INTEGER,\n    PRIMARY KEY (thread_id, turn_id, item_id)\n);\nCREATE TABLE thread_realtime_items (\n    thread_id TEXT NOT NULL,\n    item_id TEXT NOT NULL,\n    rollout_ordinal INTEGER NOT NULL,\n    created_at_ms INTEGER NOT NULL,\n    item_type TEXT NOT NULL,\n    item_json TEXT NOT NULL,\n    PRIMARY KEY (thread_id, item_id)\n);\nCREATE TABLE thread_turns (\n    thread_id TEXT NOT NULL,\n    turn_id TEXT NOT NULL,\n    rollout_ordinal INTEGER NOT NULL,\n    status TEXT NOT NULL,\n    error_json TEXT,\n    started_at INTEGER,\n    completed_at INTEGER,\n    duration_ms INTEGER,\n    first_user_item_id TEXT,\n    final_agent_item_id TEXT, rollout_byte_offset INTEGER, rollout_end_ordinal INTEGER, rollout_end_byte_offset INTEGER, root_turn_id TEXT,\n    PRIMARY KEY (thread_id, turn_id)\n);\n"

spec = importlib.util.spec_from_file_location("handoff_under_test", Path(__file__).resolve().parents[1] / "Backend/handoff.py")
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)
ROOT_A = "11111111-1111-4111-8111-111111111111"
ROOT_B = "22222222-2222-4222-8222-222222222222"
FIXTURE_ROOTS = (ROOT_A, ROOT_B)
handoff.ROOTS = FIXTURE_ROOTS  # Only this synthetic module gets fixture defaults.
CHILD_A = "33333333-3333-4333-8333-333333333333"
UNRELATED_SOURCE = "44444444-4444-4444-8444-444444444444"
UNRELATED_DESTINATION = "55555555-5555-4555-8555-555555555555"
ANCESTOR_A = "66666666-6666-4666-8666-666666666666"
ANCESTOR_B = "77777777-7777-4777-8777-777777777777"


def line(kind, payload, timestamp="2026-10-11T10:00:00.000Z"):
    return (json.dumps({"timestamp": timestamp, "type": kind, "payload": payload},
                       ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def rollout_bytes(thread_id, cwd, completed=True, suffix=b""):
    return (line("session_meta", {"id": thread_id, "cwd": str(cwd), "originator": "codex_app",
                                  "cli_version": "fixture", "source": "vscode"})
            + line("event_msg", {"type": "task_started", "turn_id": "fixture-turn"})
            + line("event_msg", {"type": "user_message", "message": "Fixture — büro ☕"})
            + (line("event_msg", {"type": "task_complete", "turn_id": "fixture-turn"}) if completed else b"")
            + suffix)


def with_history_base(data, reference):
    first,rest=data.split(b"\n",1)
    entry=json.loads(first)
    entry["payload"]["history_base"]=reference
    return (json.dumps(entry,ensure_ascii=False,separators=(",",":"))+"\n").encode()+rest


class ProfileFixture:
    def __init__(self, base, name):
        self.path = Path(base) / name
        self.path.mkdir()
        self.workspace = Path(base) / "workspace"
        self.workspace.mkdir(exist_ok=True)
        self.db = self.path / "state_5.sqlite"
        self.history_db = self.path / "thread_history_1.sqlite"
        c = sqlite3.connect(self.db)
        c.executescript(SCHEMA)
        c.execute("INSERT INTO _sqlx_migrations(version,description,success,checksum,execution_time) VALUES (1,'fixture migration',1,?,0)", (b"same-fixture-schema",))
        c.execute("INSERT INTO projects VALUES ('fixture-project','Fixture project','{}',0,1,1)")
        c.execute("INSERT INTO project_roots VALUES ('fixture-project',0,?)", (str(self.workspace),))
        c.execute("INSERT INTO thread_sections(id,name,appearance) VALUES ('fixture-section','Fixture section','{}')")
        c.commit(); c.close()
        c = sqlite3.connect(self.history_db)
        c.executescript(HISTORY_SCHEMA)
        c.commit(); c.close()
        self.auth_bytes = (name + " fixture auth; must never change\n").encode()
        (self.path / "auth.json").write_bytes(self.auth_bytes)
        (self.path / "config.toml").write_bytes((name + " fixture config\n").encode())
        (self.path / "history.jsonl").write_bytes((name + " unrelated history sentinel\n").encode())
        (self.path / "unrelated-settings.json").write_bytes((name + " unrelated settings\n").encode())
        (self.path / "session_index.jsonl").write_bytes(
            (json.dumps({"id":name+"-unrelated-index","thread_name":"Unrelated fixture","updated_at":"2026-10-11T00:00:00Z"})+"\n").encode())

    def add_thread(self, thread_id, *, title=None, completed=True, suffix=b"", references=False):
        rollout = self.path / "sessions/2026/10/11" / ("rollout-" + thread_id + ".jsonl")
        rollout.parent.mkdir(parents=True, exist_ok=True)
        data = rollout_bytes(thread_id, self.workspace, completed, suffix)
        rollout.write_bytes(data)
        c = sqlite3.connect(self.db)
        c.execute("""INSERT INTO threads
                     (id,rollout_path,created_at,updated_at,source,model_provider,cwd,title,
                      sandbox_policy,approval_mode,project_id,thread_section_id,history_mode)
                     VALUES (?,?,1,1,'vscode','openai',?,?,'{}','never',?,?,'legacy')""",
                  (thread_id, str(rollout), str(self.workspace), title or thread_id,
                   "fixture-project" if references else None, "fixture-section" if references else None))
        c.execute("INSERT INTO thread_dynamic_tools(thread_id,position,name,description,input_schema) VALUES (?,0,'fixture-tool','Fixture tool','{}')", (thread_id,))
        if references:
            c.execute("INSERT INTO thread_attachments VALUES (?,?, 'pull_request',?, ?,1)",
                      (thread_id+"-attachment", thread_id, thread_id+"-key", json.dumps({"url":"https://fixture.invalid/"+thread_id})))
        c.commit(); c.close()
        c = sqlite3.connect(self.history_db)
        c.execute("INSERT INTO thread_turns(thread_id,turn_id,rollout_ordinal,status,started_at,completed_at) VALUES (?,'fixture-turn',1,?,1,?)",
                  (thread_id, "completed" if completed else "inProgress", 2 if completed else None))
        c.execute("INSERT INTO thread_items(thread_id,turn_id,item_id,rollout_ordinal,created_at_ms,item_json,item_type) VALUES (?,'fixture-turn','fixture-item',2,1,?,'userMessage')",
                  (thread_id, json.dumps({"text":"Fixture — büro ☕"},ensure_ascii=False)))
        c.execute("INSERT INTO thread_realtime_items VALUES (?,'fixture-realtime',3,1,'fixture',?)",
                  (thread_id, json.dumps({"text":"Synthetic voice transcript"})))
        c.execute("INSERT INTO thread_history_projection_state VALUES (?,?,4)", (thread_id,len(data)))
        c.commit(); c.close()
        return rollout, data

    def add_edge(self, parent, child):
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO thread_spawn_edges VALUES (?,?,'completed')", (parent, child))
        c.commit(); c.close()

    def rows(self, table):
        c = sqlite3.connect(self.db)
        result = c.execute("SELECT * FROM " + table).fetchall()
        c.close()
        return result

    def thread_row(self, thread_id):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        c.close()
        return dict(row) if row else None

    def sentinel_files(self):
        return {name: (self.path / name).read_bytes() for name in
                ["auth.json", "config.toml", "history.jsonl", "unrelated-settings.json"]}

    def snapshot(self):
        return {str(p.relative_to(self.path)): p.read_bytes() for p in self.path.rglob("*") if p.is_file()}

    def mutate(self, database, statement, parameters=()):
        c = sqlite3.connect(self.path / database)
        c.execute(statement, parameters)
        c.commit(); c.close()

    def history_rows(self, table, thread_id):
        c = sqlite3.connect(self.history_db)
        result = c.execute("SELECT * FROM " + table + " WHERE thread_id=?", (thread_id,)).fetchall()
        c.close()
        return result

    def append_completed_turn(self, thread_id, turn_id="later-turn"):
        row = self.thread_row(thread_id)
        rollout = Path(row["rollout_path"])
        with rollout.open("ab") as handle:
            handle.write(line("event_msg", {"type": "task_started", "turn_id": turn_id}))
            handle.write(line("event_msg", {"type": "user_message", "message": "Later selected work"}))
            handle.write(line("event_msg", {"type": "task_complete", "turn_id": turn_id}))
        self.mutate("state_5.sqlite", "UPDATE threads SET updated_at=updated_at+1, title=? WHERE id=?",
                    ("Updated selected thread",thread_id))
        c = sqlite3.connect(self.history_db)
        c.execute("INSERT INTO thread_turns(thread_id,turn_id,rollout_ordinal,status,started_at,completed_at) VALUES (?,?,5,'completed',3,4)",
                  (thread_id,turn_id))
        c.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=?, next_rollout_ordinal=7 WHERE thread_id=?",
                  (rollout.stat().st_size,thread_id))
        c.commit(); c.close()

    def attach_history_base(self, thread_id, reference):
        rollout=Path(self.thread_row(thread_id)["rollout_path"])
        rollout.write_bytes(with_history_base(rollout.read_bytes(),reference))
        self.mutate("thread_history_1.sqlite","UPDATE thread_history_projection_state SET next_rollout_byte_offset=? WHERE thread_id=?",
                    (rollout.stat().st_size,thread_id))


class SelectiveHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chat-handoff-test-",dir="/private/tmp")
        self.base = Path(self.temporary.name)
        self.source = ProfileFixture(self.base,"source")
        self.destination = ProfileFixture(self.base,"destination")
        self.control = self.base / "control"
        self.allowed = {ROOT_A, ROOT_B, CHILD_A}
        for thread_id in (ROOT_A, ROOT_B, CHILD_A):
            self.source.add_thread(thread_id)
        self.source.add_edge(ROOT_A,CHILD_A)
        self.source.add_thread(UNRELATED_SOURCE,references=True)
        self.destination.add_thread(UNRELATED_DESTINATION,references=True)
        self.original_source_sentinels = self.source.sentinel_files()
        self.original_destination_sentinels = self.destination.sentinel_files()

    def tearDown(self):
        self.temporary.cleanup()

    def transfer(self, source=None, destination=None, **kwargs):
        kwargs.setdefault("roots", FIXTURE_ROOTS)
        kwargs.setdefault("allowed_roots", FIXTURE_ROOTS)
        return handoff.transfer((source or self.source).path,
                                (destination or self.destination).path,
                                self.control,require_closed=False,**kwargs)

    def unrelated_state(self, profile, thread_id):
        state = {"thread":profile.thread_row(thread_id),"sentinels":profile.sentinel_files()}
        row=state["thread"]
        if row:
            state["rollout"]=Path(row["rollout_path"]).read_bytes()
        for table in ("thread_dynamic_tools","thread_attachments","thread_spawn_edges"):
            column="parent_thread_id" if table=="thread_spawn_edges" else "thread_id"
            c=sqlite3.connect(profile.db)
            state[table]=c.execute("SELECT * FROM "+table+" WHERE "+column+"=?",(thread_id,)).fetchall()
            c.close()
        for table in ("projects","project_roots","thread_sections","_sqlx_migrations",
                      "external_agent_config_imports","remote_control_enrollments"):
            state[table]=profile.rows(table)
        for table in ("thread_turns","thread_items","thread_realtime_items","thread_history_projection_state"):
            state[table]=profile.history_rows(table,thread_id)
        return state

    def assert_profiles_private(self):
        self.assertEqual(self.source.sentinel_files(),self.original_source_sentinels)
        self.assertEqual(self.destination.sentinel_files(),self.original_destination_sentinels)

    def assert_snapshot_unchanged(self, profile, before):
        after=profile.snapshot()
        self.assertEqual(set(after),set(before),"Destination file set changed")
        self.assertEqual(sorted(name for name in before if before[name]!=after[name]),[],
                         "Destination file bytes changed")

    def assert_rejected_without_destination_changes(self, message=None, **kwargs):
        before=self.destination.snapshot()
        if message:
            with self.assertRaisesRegex(handoff.HandoffError,message): self.transfer(**kwargs)
        else:
            with self.assertRaises(handoff.HandoffError): self.transfer(**kwargs)
        self.assert_snapshot_unchanged(self.destination,before)
        self.assert_profiles_private()

    def test_import_only_two_roots_plus_descendants_preserves_unrelated_and_auth(self):
        before=self.unrelated_state(self.destination,UNRELATED_DESTINATION)
        original_source=self.source.snapshot()
        original_index=(self.destination.path/"session_index.jsonl").read_bytes()
        report=self.transfer()
        self.assertTrue(report["changed"])
        self.assertEqual({r[0] for r in self.destination.rows("threads")},self.allowed|{UNRELATED_DESTINATION})
        self.assertIsNone(self.destination.thread_row(UNRELATED_SOURCE))
        self.assertEqual(self.unrelated_state(self.destination,UNRELATED_DESTINATION),before)
        self.assertEqual(self.source.snapshot(),original_source)
        self.assert_profiles_private()
        updated_index=(self.destination.path/"session_index.jsonl").read_bytes()
        self.assertTrue(updated_index.startswith(original_index))
        appended=[json.loads(row)["id"] for row in updated_index[len(original_index):].splitlines()]
        self.assertEqual(set(appended),{ROOT_A,ROOT_B})
        for thread_id in self.allowed:
            src=self.source.thread_row(thread_id)
            dst=self.destination.thread_row(thread_id)
            self.assertEqual(Path(src["rollout_path"]).read_bytes(),Path(dst["rollout_path"]).read_bytes())
            self.assertEqual(Path(dst["rollout_path"]).parent,self.destination.path/"sessions/2026/10/11")
            for table in ("thread_turns","thread_items","thread_realtime_items","thread_history_projection_state"):
                self.assertEqual(self.source.history_rows(table,thread_id),self.destination.history_rows(table,thread_id))

    def test_bidirectional_updates_and_idempotent_replay(self):
        self.transfer()
        before=self.destination.snapshot()
        repeat=self.transfer()
        self.assertFalse(repeat["changed"])
        self.assert_snapshot_unchanged(self.destination,before)
        self.destination.append_completed_turn(ROOT_A,"work-side-turn")
        preserved=self.unrelated_state(self.source,UNRELATED_SOURCE)
        reverse=self.transfer(self.destination,self.source)
        self.assertTrue(reverse["changed"])
        self.assertEqual(self.unrelated_state(self.source,UNRELATED_SOURCE),preserved)
        self.source.append_completed_turn(ROOT_B,"original-side-turn")
        forward=self.transfer()
        self.assertTrue(forward["changed"])
        self.assert_profiles_private()
        for thread_id in self.allowed:
            self.assertEqual(Path(self.source.thread_row(thread_id)["rollout_path"]).read_bytes(),
                             Path(self.destination.thread_row(thread_id)["rollout_path"]).read_bytes())

    def test_divergent_edits_rejected(self):
        self.transfer()
        self.source.append_completed_turn(ROOT_A,"source-divergence")
        self.destination.append_completed_turn(ROOT_B,"destination-divergence")
        self.assert_rejected_without_destination_changes("different history")

    def test_active_selected_turn_rejected(self):
        self.source.mutate("thread_history_1.sqlite","UPDATE thread_turns SET status='inProgress',completed_at=NULL WHERE thread_id=?",(ROOT_A,))
        self.assert_rejected_without_destination_changes("Stop the selected")

    def test_active_descendant_turn_rejected(self):
        self.source.mutate("thread_history_1.sqlite","UPDATE thread_turns SET status='inProgress',completed_at=NULL WHERE thread_id=?",(CHILD_A,))
        self.assert_rejected_without_destination_changes("Stop the selected")

    def test_unprojected_active_rollout_tail_rejected(self):
        # A busy app may have appended a start event before its projection catches
        # up. The previously projected completed turn alone cannot prove idle.
        rollout=Path(self.source.thread_row(ROOT_A)["rollout_path"])
        with rollout.open("ab") as handle:
            handle.write(line("event_msg",{"type":"task_started","turn_id":"not-yet-projected"}))
        self.assert_rejected_without_destination_changes()

    def test_schema_mismatch_rejected(self):
        self.destination.mutate("thread_history_1.sqlite","ALTER TABLE thread_items ADD COLUMN incompatible_fixture_column TEXT")
        self.assert_rejected_without_destination_changes("different history schemas")

    def test_missing_descendant_rejected(self):
        self.source.mutate("state_5.sqlite","DELETE FROM threads WHERE id=?",(CHILD_A,))
        self.assert_rejected_without_destination_changes("dependency is missing")

    def test_missing_rollout_rejected(self):
        Path(self.source.thread_row(ROOT_B)["rollout_path"]).unlink()
        self.assert_rejected_without_destination_changes("missing or redirected")

    def test_missing_agreed_root_rejected(self):
        self.source.mutate("state_5.sqlite","DELETE FROM threads WHERE id=?",(ROOT_B,))
        self.assert_rejected_without_destination_changes("every selected chat")

    def test_disallowed_third_root_rejected(self):
        self.assert_rejected_without_destination_changes("not explicitly authorized",roots=(ROOT_A,ROOT_B,UNRELATED_SOURCE))

    def test_selected_external_references_rejected(self):
        self.source.mutate("state_5.sqlite","UPDATE threads SET project_id='fixture-project' WHERE id=?",(ROOT_A,))
        self.assert_rejected_without_destination_changes("project or section")

    def test_rollout_cannot_escape_profile_or_be_symlink(self):
        outside=self.base/"outside.jsonl"
        outside.write_bytes(b"fixture outside-profile sentinel")
        self.source.mutate("state_5.sqlite","UPDATE threads SET rollout_path=? WHERE id=?",(str(outside),ROOT_A))
        self.assert_rejected_without_destination_changes("outside its own profile")
        self.assertEqual(outside.read_bytes(),b"fixture outside-profile sentinel")

    def test_selected_auxiliary_records_rejected(self):
        c=sqlite3.connect(self.source.path/"queue_1.sqlite")
        c.execute("CREATE TABLE queued_items(thread_id TEXT NOT NULL,payload TEXT)")
        c.execute("INSERT INTO queued_items VALUES (?,?)",(ROOT_A,"fixture queue request"))
        c.commit();c.close()
        self.assert_rejected_without_destination_changes("records in queue_1")

    def test_unrelated_auxiliary_records_preserved_and_not_imported(self):
        for profile,thread_id in ((self.source,UNRELATED_SOURCE),(self.destination,UNRELATED_DESTINATION)):
            c=sqlite3.connect(profile.path/"queue_1.sqlite")
            c.execute("CREATE TABLE queued_items(thread_id TEXT NOT NULL,payload TEXT)")
            c.execute("INSERT INTO queued_items VALUES (?,?)",(thread_id,"unrelated fixture queue"))
            c.commit();c.close()
        original_source=(self.source.path/"queue_1.sqlite").read_bytes()
        original_destination=(self.destination.path/"queue_1.sqlite").read_bytes()
        self.transfer()
        self.assertEqual((self.source.path/"queue_1.sqlite").read_bytes(),original_source)
        self.assertEqual((self.destination.path/"queue_1.sqlite").read_bytes(),original_destination)

    def test_preexisting_selected_destination_queue_rejected_before_import(self):
        # A stale queue record can exist without either selected root in state.
        # Importing the thread first could make that request runnable again.
        c=sqlite3.connect(self.destination.path/"queue_1.sqlite")
        c.execute("CREATE TABLE queued_items(thread_id TEXT NOT NULL,payload TEXT)")
        c.execute("INSERT INTO queued_items VALUES (?,?)",(ROOT_A,"stale destination fixture queue"))
        c.commit();c.close()
        self.assert_rejected_without_destination_changes("records in queue_1")

    def test_destination_open_rejected(self):
        before=self.destination.snapshot()
        with mock.patch.object(handoff,"target_is_closed",return_value=False):
            with self.assertRaisesRegex(handoff.HandoffError,"Quit the destination"):
                handoff.transfer(self.source.path,self.destination.path,self.control,roots=FIXTURE_ROOTS,allowed_roots=FIXTURE_ROOTS,require_closed=True)
        self.assert_snapshot_unchanged(self.destination,before)
        self.assert_profiles_private()

    def test_unfinished_operation_blocks_another_transfer(self):
        self.control.mkdir()
        (self.control/"operation.json").write_text(json.dumps({"status":"prepared"}))
        self.assert_rejected_without_destination_changes()

    def test_control_cannot_be_reused_for_different_profiles(self):
        self.transfer()
        self.control.joinpath("baseline.json").write_text(json.dumps({"roots":list(handoff.ROOTS),"profiles":["/fixture/other","/fixture/another"],"fingerprint":"fake"}))
        self.assert_rejected_without_destination_changes("different profiles or chats")

    def test_initial_destination_rollout_parent_symlink_rejected(self):
        original=Path(self.source.thread_row(ROOT_A)["rollout_path"])
        relocated=self.source.path/"sessions/2026/10/12"/original.name
        relocated.parent.mkdir()
        original.rename(relocated)
        self.source.mutate("state_5.sqlite","UPDATE threads SET rollout_path=? WHERE id=?",(str(relocated),ROOT_A))
        outside=self.base/"outside-destination"
        outside.mkdir()
        sentinel=outside/"outside-sentinel.txt"
        sentinel.write_bytes(b"Outside synthetic data must stay untouched")
        redirected=self.destination.path/"sessions/2026/10/12"
        redirected.symlink_to(outside,target_is_directory=True)
        self.assert_rejected_without_destination_changes("destination rollout path is redirected")
        self.assertEqual(list(outside.iterdir()),[sentinel])
        self.assertEqual(sentinel.read_bytes(),b"Outside synthetic data must stay untouched")

    def commit_failure(self):
        real_connect=sqlite3.connect
        observed_installed=[]
        fixture=self

        class FailingCommitConnection:
            def __init__(self, connection): self.connection=connection
            def __getattr__(self,name): return getattr(self.connection,name)
            def commit(self):
                # This failure occurs after the helper replaced every selected
                # rollout, proving restoration is exercised rather than an
                # earlier validation exception.
                for thread_id in fixture.allowed:
                    src=Path(fixture.source.thread_row(thread_id)["rollout_path"])
                    relative=src.relative_to(fixture.source.path)
                    current=fixture.destination.path/relative
                    fixture.assertTrue(current.exists())
                    fixture.assertEqual(current.read_bytes(),src.read_bytes())
                    observed_installed.append(str(relative))
                raise sqlite3.OperationalError("fixture commit failure after rollout install")

        def connect(*args,**kwargs):
            connection=real_connect(*args,**kwargs)
            if args and isinstance(args[0],Path) and args[0]==self.destination.db:
                return FailingCommitConnection(connection)
            return connection

        before=self.destination.snapshot()
        with mock.patch.object(handoff.sqlite3,"connect",side_effect=connect):
            with self.assertRaisesRegex(sqlite3.OperationalError,"fixture commit failure"):
                self.transfer()
        self.assertEqual(len(observed_installed),len(self.allowed))
        self.assert_snapshot_unchanged(self.destination,before)
        self.assert_profiles_private()
        operation=json.loads((self.control/"operation.json").read_text())
        self.assertEqual(operation["status"],"rolled_back")

    def test_failed_initial_commit_removes_installed_rollouts(self):
        self.commit_failure()
        for thread_id in self.allowed:
            self.assertIsNone(self.destination.thread_row(thread_id))

    def test_failed_update_commit_restores_previous_rollout_bytes(self):
        self.transfer()
        self.source.append_completed_turn(ROOT_A,"after-baseline-before-failed-commit")
        self.commit_failure()

    def create_committed_pending_operation(self):
        with mock.patch.object(handoff,"finish_committed",side_effect=handoff.HandoffError("fixture verification interrupted")):
            with self.assertRaisesRegex(handoff.HandoffError,"fixture verification interrupted"):
                self.transfer()
        operation=json.loads((self.control/"operation.json").read_text())
        self.assertEqual(operation["status"],"committed")
        self.assertEqual({r[0] for r in self.destination.rows("threads")},self.allowed|{UNRELATED_DESTINATION})

    def test_committed_postverification_retry_finishes_without_rewriting_databases(self):
        self.create_committed_pending_operation()
        before=self.destination.snapshot()
        unrelated=self.unrelated_state(self.destination,UNRELATED_DESTINATION)
        with mock.patch.object(handoff,"insert_rows",side_effect=AssertionError("Retry must not rewrite SQL rows")), \
             mock.patch.object(handoff,"backup_db",side_effect=AssertionError("Retry must not make a new SQL backup")):
            report=self.transfer()
        self.assertTrue(report["verified"])
        after=self.destination.snapshot()
        for name in before:
            if name!="session_index.jsonl": self.assertEqual(after[name],before[name],name)
        self.assertEqual(self.unrelated_state(self.destination,UNRELATED_DESTINATION),unrelated)
        operation=json.loads((self.control/"operation.json").read_text())
        self.assertEqual(operation["status"],"complete")
        self.assertTrue((self.control/"baseline.json").is_file())
        self.assert_profiles_private()

    def test_committed_retry_rejects_wrong_direction(self):
        self.create_committed_pending_operation()
        source_before=self.source.snapshot()
        destination_before=self.destination.snapshot()
        with self.assertRaisesRegex(handoff.HandoffError,"original direction"):
            self.transfer(self.destination,self.source)
        self.assert_snapshot_unchanged(self.source,source_before)
        self.assert_snapshot_unchanged(self.destination,destination_before)

    def test_committed_retry_rejects_source_changes(self):
        self.create_committed_pending_operation()
        self.source.append_completed_turn(ROOT_A,"source-resumed-after-commit")
        self.assert_rejected_without_destination_changes("source resumed")
        self.assertEqual(json.loads((self.control/"operation.json").read_text())["status"],"committed")

    def test_closed_wal_database_reads_without_sidecar_or_base_mutation(self):
        c=sqlite3.connect(self.source.db)
        self.assertEqual(c.execute("PRAGMA journal_mode=WAL").fetchone()[0],"wal")
        c.execute("UPDATE threads SET title='closed-WAL fixture' WHERE id=?",(ROOT_A,))
        c.commit()
        checkpoint=c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self.assertEqual(checkpoint,(0,0,0))
        c.close()
        # System SQLite may retain empty sidecars on close. Construct the exact
        # fully checkpointed, sidecar-free state encountered by recovery.
        for suffix in ("-wal","-shm"):
            Path(str(self.source.db)+suffix).unlink(missing_ok=True)
        self.assertFalse(Path(str(self.source.db)+"-wal").exists())
        self.assertFalse(Path(str(self.source.db)+"-shm").exists())
        before=self.source.db.read_bytes()
        read=handoff.read_db(self.source.db)
        try:
            self.assertEqual(read.execute("SELECT title FROM threads WHERE id=?",(ROOT_A,)).fetchone()[0],"closed-WAL fixture")
        finally: read.close()
        self.assertEqual(self.source.db.read_bytes(),before)
        self.assertFalse(Path(str(self.source.db)+"-wal").exists())
        self.assertFalse(Path(str(self.source.db)+"-shm").exists())

    def test_live_wal_reader_does_not_ignore_committed_wal_rows(self):
        c=sqlite3.connect(self.source.db)
        try:
            self.assertEqual(c.execute("PRAGMA journal_mode=WAL").fetchone()[0],"wal")
            c.execute("UPDATE threads SET title='live-WAL fixture' WHERE id=?",(ROOT_A,))
            c.commit()
            self.assertTrue(Path(str(self.source.db)+"-wal").exists())
            self.assertGreater(Path(str(self.source.db)+"-wal").stat().st_size,0)
            read=handoff.read_db(self.source.db)
            try:
                self.assertEqual(read.execute("SELECT title FROM threads WHERE id=?",(ROOT_A,)).fetchone()[0],"live-WAL fixture")
            finally: read.close()
        finally: c.close()

    def setup_two_level_fork_lineage(self):
        # File aliases deliberately differ from metadata IDs, matching actual
        # fork history_base resolution by rollout filename suffix.
        ancestor_dir=self.source.path/"sessions/2026/10/10"
        ancestor_dir.mkdir(parents=True)
        path_b=ancestor_dir/("rollout-fixture-"+ANCESTOR_B+".jsonl")
        prefix_b=rollout_bytes("99999999-9999-4999-8999-999999999999",self.source.workspace)
        reference_b={"thread_id":ANCESTOR_B,"end_byte_offset":len(prefix_b),"end_ordinal_exclusive":4}
        tail_b=line("event_msg",{"type":"user_message","message":"OUTSIDE B fork boundary; never import this later work"})
        path_b.write_bytes(prefix_b+tail_b)
        path_a=ancestor_dir/("rollout-fixture-"+ANCESTOR_A+".jsonl")
        prefix_a=with_history_base(rollout_bytes("88888888-8888-4888-8888-888888888888",self.source.workspace),reference_b)
        reference_a={"thread_id":ANCESTOR_A,"end_byte_offset":len(prefix_a),"end_ordinal_exclusive":8}
        tail_a=line("event_msg",{"type":"user_message","message":"OUTSIDE A fork boundary; never import this later work"})
        path_a.write_bytes(prefix_a+tail_a)
        self.source.attach_history_base(ROOT_A,reference_a)
        return {ANCESTOR_A:(path_a,prefix_a,prefix_a+tail_a),
                ANCESTOR_B:(path_b,prefix_b,prefix_b+tail_b)}

    def test_fork_lineage_copies_two_bounded_prefixes_without_visible_ancestor_threads(self):
        ancestors=self.setup_two_level_fork_lineage()
        source_before=self.source.snapshot()
        self.transfer()
        self.assertEqual(self.source.snapshot(),source_before)
        self.assertEqual({row[0] for row in self.destination.rows("threads")},self.allowed|{UNRELATED_DESTINATION})
        for ancestor,(source_path,prefix,full) in ancestors.items():
            destination_path=self.destination.path/source_path.relative_to(self.source.path)
            self.assertTrue(destination_path.is_file())
            self.assertEqual(destination_path.read_bytes(),prefix)
            self.assertLess(destination_path.stat().st_size,len(full))
            self.assertNotIn(b"OUTSIDE",destination_path.read_bytes())
            self.assertIsNone(self.destination.thread_row(ancestor))
            for table in ("thread_turns","thread_items","thread_realtime_items","thread_history_projection_state"):
                self.assertEqual(self.destination.history_rows(table,ancestor),[])
        self.assert_profiles_private()

    def test_fork_lineage_reverse_handoff_preserves_original_extra_ancestor_bytes(self):
        ancestors=self.setup_two_level_fork_lineage()
        self.transfer()
        self.destination.append_completed_turn(ROOT_A,"continued-fork-in-work")
        source_unrelated=self.unrelated_state(self.source,UNRELATED_SOURCE)
        report=self.transfer(self.destination,self.source)
        self.assertTrue(report["verified"])
        self.assertEqual(self.unrelated_state(self.source,UNRELATED_SOURCE),source_unrelated)
        for ancestor,(source_path,prefix,full) in ancestors.items():
            self.assertEqual(source_path.read_bytes(),full)
            self.assertEqual((self.destination.path/source_path.relative_to(self.source.path)).read_bytes(),prefix)
            self.assertIsNone(self.source.thread_row(ancestor))
        self.assertEqual(Path(self.source.thread_row(ROOT_A)["rollout_path"]).read_bytes(),
                         Path(self.destination.thread_row(ROOT_A)["rollout_path"]).read_bytes())
        self.assert_profiles_private()

    def test_missing_direct_fork_base_rejected_before_destination_mutation(self):
        ancestors=self.setup_two_level_fork_lineage()
        ancestors[ANCESTOR_A][0].unlink()
        self.assert_rejected_without_destination_changes()

    def test_missing_transitive_fork_base_rejected_before_destination_mutation(self):
        ancestors=self.setup_two_level_fork_lineage()
        ancestors[ANCESTOR_B][0].unlink()
        self.assert_rejected_without_destination_changes()

    def test_fork_lineage_cycle_rejected_before_destination_mutation(self):
        ancestors=self.setup_two_level_fork_lineage()
        path_a,prefix_a,_=ancestors[ANCESTOR_A]
        path_b,_,_=ancestors[ANCESTOR_B]
        back_reference={"thread_id":ANCESTOR_A,"end_byte_offset":len(prefix_a),"end_ordinal_exclusive":8}
        path_b.write_bytes(with_history_base(path_b.read_bytes(),back_reference))
        self.assert_rejected_without_destination_changes("cycle")

    def test_fork_lineage_inconsistent_ordinal_rejected_before_destination_mutation(self):
        ancestors=self.setup_two_level_fork_lineage()
        _,prefix_a,_=ancestors[ANCESTOR_A]
        self.source.attach_history_base(ROOT_A,{"thread_id":ANCESTOR_A,"end_byte_offset":len(prefix_a),
                                                "end_ordinal_exclusive":9})
        self.assert_rejected_without_destination_changes("ordinal")

    def test_fork_lineage_boundary_splitting_event_rejected_before_destination_mutation(self):
        ancestors=self.setup_two_level_fork_lineage()
        _,prefix_a,_=ancestors[ANCESTOR_A]
        self.source.attach_history_base(ROOT_A,{"thread_id":ANCESTOR_A,"end_byte_offset":len(prefix_a)-1,
                                                "end_ordinal_exclusive":8})
        self.assert_rejected_without_destination_changes("splits a saved event")

    def test_archived_fork_base_copied_as_invisible_bounded_dependency(self):
        ancestors=self.setup_two_level_fork_lineage()
        path_b,prefix_b,_=ancestors[ANCESTOR_B]
        archived=self.source.path/"archived_sessions"/path_b.name
        archived.parent.mkdir()
        path_b.rename(archived)
        self.transfer()
        copied=self.destination.path/archived.relative_to(self.source.path)
        self.assertEqual(copied.read_bytes(),prefix_b)
        self.assertIsNone(self.destination.thread_row(ANCESTOR_B))
        self.assertEqual({row[0] for row in self.destination.rows("threads")},self.allowed|{UNRELATED_DESTINATION})


class PublicSelectionGuards(unittest.TestCase):
    def test_public_defaults_have_no_roots_and_refuse_before_reading_profiles(self):
        import handoff as public_handoff
        self.assertEqual(public_handoff.ROOTS, ())
        with mock.patch.object(public_handoff, "read_db", side_effect=AssertionError("No profile read allowed")):
            with self.assertRaises(public_handoff.HandoffError):
                public_handoff.collect(Path("/fixture/unused"))
            with self.assertRaises(public_handoff.HandoffError):
                public_handoff.collect(Path("/fixture/unused"), ())

    def test_explicit_roots_need_authorization_before_any_profile_access(self):
        import handoff as public_handoff
        with mock.patch.object(public_handoff, "target_is_closed", side_effect=AssertionError("No process query allowed")):
            for allowed in (None, (), (ROOT_B,)):
                with self.subTest(allowed=allowed), self.assertRaises(public_handoff.HandoffError):
                    public_handoff.transfer(Path("/fixture/source"), Path("/fixture/destination"),
                                            Path("/fixture/control"), roots=(ROOT_A,), allowed_roots=allowed)

    def test_selection_is_bounded_canonical_and_unique(self):
        import handoff as public_handoff
        for values in ((), [ROOT_A] * 2, ["invalid"], [ROOT_A] * 51, "invalid", None):
            with self.subTest(values=values), self.assertRaises(public_handoff.HandoffError):
                public_handoff.validate_roots(values)
        self.assertEqual(public_handoff.validate_roots((ROOT_A, ROOT_B)), (ROOT_A, ROOT_B))

    def test_legacy_command_and_repair_entry_points_are_absent(self):
        import handoff as public_handoff
        for name in ("main", "repair_lineage"):
            self.assertFalse(hasattr(public_handoff, name))


class ProfileClosureTests(unittest.TestCase):
    """Lifecycle checks use mocked process listings, never real applications."""
    def test_personal_matches_its_exact_nested_executable(self):
        personal = Path.home() / "Library/Application Support/ChatGPT Copy/codex"
        executable = "/Applications/ChatGPT Personal.app/Contents/Helpers/ChatGPT.app/Contents/MacOS/ChatGPT"
        with mock.patch.object(handoff.subprocess, "check_output", return_value=executable + "\n"), \
             mock.patch.object(handoff.subprocess, "run") as files:
            self.assertFalse(handoff.target_is_closed(personal))
            files.assert_not_called()

    def test_arbeit_executable_does_not_stand_in_for_personal(self):
        personal = Path.home() / "Library/Application Support/ChatGPT Copy/codex"
        arbeit = "/Applications/ChatGPT Arbeit.app/Contents/Helpers/ChatGPT.app/Contents/MacOS/ChatGPT"
        released = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(handoff.subprocess, "check_output", return_value=arbeit + "\n"), \
             mock.patch.object(handoff.subprocess, "run", return_value=released):
            self.assertTrue(handoff.target_is_closed(personal))

    def test_profile_database_holder_blocks_even_with_different_app_executable(self):
        personal = Path.home() / "Library/Application Support/ChatGPT Copy/codex"
        holder = mock.Mock(returncode=0, stdout="4321\n")
        with mock.patch.object(handoff.subprocess, "check_output", return_value="/Applications/ChatGPT.app/Contents/MacOS/ChatGPT\n"), \
             mock.patch.object(handoff.subprocess, "run", return_value=holder):
            self.assertFalse(handoff.target_is_closed(personal))
            self.assertTrue(handoff.target_is_closed(personal, ignore_pid=4321))


if __name__ == "__main__":
    unittest.main(verbosity=2)
