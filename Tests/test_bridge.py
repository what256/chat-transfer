"""Synthetic files and protocol fixtures only; never starts a real coding agent."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Backend"))

import contextlib
import copy
import hashlib
import json
import os
import queue
import shlex
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
import uuid

import bridge

CODEX_ID = "00000001-0001-4001-8001-000000000001"
IMPORTED_ID = "00000002-0002-4002-8002-000000000002"
CLAUDE_ID = "00000003-0003-4003-8003-000000000003"


def item(identifier, kind, **values):
    return {"turnId": "turn-one", "item": {"id": identifier, "type": kind, **values}}


class FixtureClient:
    def __init__(self, fixture, profile):
        self.fixture = fixture
        self.profile = Path(profile)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def request(self, method, params=None, timeout=45):
        self.fixture.calls.append((str(self.profile), method, copy.deepcopy(params or {})))
        f = self.fixture
        if method == "externalAgentConfig/detect":
            return copy.deepcopy(f.detected)
        if method == "externalAgentConfig/import/readHistories":
            return copy.deepcopy(f.histories)
        if method == "externalAgentConfig/import":
            f.imported_request = copy.deepcopy(params)
            if f.accept_error:
                raise bridge.BridgeError("Synthetic import rejected")
            return {"importId": f.import_id}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "updatedAt": 10}}
        if method == "thread/turns/list":
            if params.get("itemsView") == "full":
                index = 1 if params.get("cursor") == "page-two" else 0
                page = copy.deepcopy(f.pages[index])
                page["data"] = [{"id": "turn-one", "status": f.turn_status,
                                  "items": [entry["item"] for entry in page["data"]]}]
                return page
            return {"data": [{"id": "turn-one", "status": f.turn_status}]}
        if method == "thread/items/list":
            if params.get("cursor") == "page-two":
                return copy.deepcopy(f.pages[1])
            return copy.deepcopy(f.pages[0])
        raise AssertionError("Unexpected method " + method)

    def wait_import(self, identifier, timeout=180):
        self.fixture.waited = identifier
        return copy.deepcopy(self.fixture.completion)


class Fixture:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.project = self.directory / "project with space $() `quoted`"
        self.project.mkdir()
        self.claude_root = self.directory / ".claude/projects"
        self.session = self.claude_root / "project-one" / (CLAUDE_ID + ".jsonl")
        self.session.parent.mkdir(parents=True)
        self.session.write_text('{"type":"user","message":{"content":"fixture"}}\n')
        self.profiles = {name: self.directory / name for name in ("chatgpt", "arbeit", "personal")}
        for profile in self.profiles.values():
            profile.mkdir()
        self.control = self.directory / "control/bridge"
        self.cli = self.directory / "claude mock"
        self.cli.write_text("#!/bin/sh\nexit 0\n")
        self.cli.chmod(0o700)
        self.rollout = self.profiles["chatgpt"] / "saved.jsonl"
        self.rollout.write_text("unchanged saved source\n")
        self.inventory = {"chatgpt": {CODEX_ID: {"id": CODEX_ID, "title": "Chat 'one' $(touch NEVER) `NEVER`", "name": None,
                                               "cwd": str(self.project), "updated_at": 10, "updated_at_ms": 10000,
                                               "rollout_path": str(self.rollout)}}, "arbeit": {}, "personal": {}}
        self.detected = {"items": [
            {"itemType": "CONFIG", "description": "Settings must not be imported"},
            {"itemType": "MCP_SERVER_CONFIG", "description": "Credentials must not be imported"},
            {"itemType": "SESSIONS", "description": "Import selected sessions", "cwd": None,
             "details": {"sessions": [{"path": str(self.session), "cwd": str(self.project), "title": "Claude fixture"}],
                         "hooks": [{"name": "Do not execute"}], "mcpServers": [{"name": "Do not import"}]}}]}
        self.histories = {"data": []}
        self.import_id = "00000004-0004-4004-8004-000000000004"
        self.completion = {"importId": self.import_id, "itemTypeResults": [{"itemType": "SESSIONS", "successes": [
            {"itemType": "SESSIONS", "source": str(self.session), "target": str(self.profiles['arbeit'] / ('rollout-' + IMPORTED_ID + '.jsonl'))}], "failures": []}]}
        self.pages = [
            {"data": [item("user-1", "userMessage", content=[{"type": "text", "text": "Use Unicode: Ä 👋. Keep $() and `x` literal."},
                                                               {"type": "localImage", "path": "/DO_NOT_READ_IMAGE"}]),
                      item("private-1", "reasoning", content=["PRIVATE_REASONING_NEVER_EXPORT"]),
                      item("tool-1", "commandExecution", command="PRIVATE_TOOL_NEVER_EXPORT"),
                      item("agent-1", "agentMessage", phase="commentary", text="Visible progress")], "nextCursor": "page-two"},
            {"data": [item("agent-2", "agentMessage", phase="final_answer", text="Visible final answer"),
                      item("bad-phase", "agentMessage", phase="analysis", text="PRIVATE_PHASE_NEVER_EXPORT")], "nextCursor": None}]
        self.turn_status = "completed"
        self.calls = []
        self.imported_request = None
        self.accept_error = False
        self.waited = None
        self.backend = bridge.Bridge(profiles=self.profiles, control=self.control, claude_cli=self.cli,
                                     claude_roots=(self.claude_root,),
                                     client_factory=lambda profile: FixtureClient(self, profile),
                                     inventory_loader=self.load_inventory)

    def load_inventory(self, profile):
        key = next(name for name, path in self.profiles.items() if Path(profile) == path)
        return copy.deepcopy(self.inventory[key])

    def request(self, command, source="claude", ids=None, destination=None):
        request = {"command": command, "source": source, "destination": destination or ("arbeit" if source == "claude" else "claude")}
        if ids is not None:
            request["ids"] = ids
        return request

    def claude_selection(self):
        return [self.backend.list(self.request("bridge_list"))["chats"][0]["id"]]

    def prepared(self, source="claude"):
        request = self.request("bridge_prepare", source, self.claude_selection() if source == "claude" else [CODEX_ID])
        response = self.backend.handle(request)
        execute = dict(request, command="bridge_execute", selection_token=response["selection_token"])
        return response, execute


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bridge-test-", dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.f = Fixture(self.temporary.name)

    def test_listing_claude_uses_native_detection_and_does_not_write_control(self):
        result = self.f.backend.handle(self.f.request("bridge_list"))
        self.assertEqual(len(result["chats"]), 1)
        self.assertTrue(result["chats"][0]["id"].startswith("claude:"))
        self.assertFalse(self.f.control.exists())
        self.assertEqual({call[1] for call in self.f.calls}, {"externalAgentConfig/detect"})

    def test_same_basename_in_different_projects_has_distinct_namespaced_id(self):
        second = self.f.claude_root / "project-two" / self.f.session.name
        second.parent.mkdir(); second.write_text(self.f.session.read_text())
        self.f.detected["items"][-1]["details"]["sessions"].append({"path": str(second), "cwd": str(self.f.project), "title": "Another"})
        rows = self.f.backend.list(self.f.request("bridge_list"))["chats"]
        self.assertEqual(len({row["id"] for row in rows}), 2)

    def test_untrusted_external_or_redirected_detection_paths_are_excluded(self):
        outside = self.f.directory / "outside.jsonl"; outside.write_text("outside")
        redirect = self.f.session.parent / "redirect.jsonl"; redirect.symlink_to(self.f.session)
        for path in (outside, redirect):
            self.f.detected["items"][-1]["details"]["sessions"].append({"path": str(path), "cwd": str(self.f.project)})
        self.assertEqual(len(self.f.backend.list(self.f.request("bridge_list"))["chats"]), 1)

    def test_selection_routes_and_ids_reject_before_transfer(self):
        for request in [self.f.request("bridge_prepare", ids=[]),
                        self.f.request("bridge_prepare", ids=["unknown"]),
                        self.f.request("bridge_prepare", source="chatgpt", ids=["../not-a-chat"]),
                        self.f.request("bridge_prepare", source="chatgpt", destination="arbeit", ids=[CODEX_ID]),
                        self.f.request("bridge_prepare", source="chatgpt", ids=[CODEX_ID, str(uuid.uuid4())])]:
            with self.subTest(request=request), self.assertRaises(bridge.BridgeError):
                self.f.backend.handle(request)
        self.assertIsNone(self.f.imported_request)

    def test_prepare_is_read_only_and_response_contains_no_transcript(self):
        source_hash = hashlib.sha256(self.f.rollout.read_bytes()).hexdigest()
        result, _ = self.f.prepared("chatgpt")
        self.assertEqual(result["counts"]["messages"], 3)
        self.assertEqual(result["counts"]["attachments_omitted"], 1)
        self.assertEqual(result["counts"]["other_items_omitted"], 3)
        self.assertNotIn("Visible final answer", json.dumps(result))
        self.assertFalse(self.f.control.exists())
        self.assertEqual(hashlib.sha256(self.f.rollout.read_bytes()).hexdigest(), source_hash)

    def test_export_keeps_full_visible_text_and_excludes_private_state(self):
        _, request = self.f.prepared("chatgpt")
        result = self.f.backend.execute(request)
        text = Path(result["transcript_file"]).read_text()
        self.assertIn("Ä 👋", text)
        self.assertIn("Visible progress", text)
        self.assertIn("Visible final answer", text)
        self.assertIn("Referenced attachment", text)
        self.assertNotIn("PRIVATE_", text)
        self.assertNotIn("DO_NOT_READ_IMAGE", text)
        self.assertNotIn("Visible final answer", json.dumps(result))
        self.assertTrue(self.f.rollout.exists())
        self.assertFalse(any(method in {"turn/start", "thread/start", "thread/resume", "externalAgentConfig/import"} for _, method, _ in self.f.calls))

    def test_continuation_prompt_references_transcript_as_data_not_embedded_history(self):
        _, request = self.f.prepared("chatgpt")
        result = self.f.backend.execute(request)
        prompt = (Path(result["output_directory"]) / "continuation.md").read_text()
        self.assertIn(result["transcript_file"], prompt)
        self.assertIn("quoted historical data", prompt)
        self.assertIn("Wait for the user's next message", prompt)
        self.assertNotIn("Visible final answer", prompt)

    def test_launch_script_is_shell_safe_and_has_no_initial_prompt(self):
        _, request = self.f.prepared("chatgpt")
        result = self.f.backend.execute(request)
        lines = Path(result["launch_file"]).read_text().splitlines()
        cd_args = shlex.split(lines[2]); self.assertEqual(cd_args, ["cd", "--", str(self.f.project)])
        args = shlex.split(lines[3]); self.assertEqual(args[0], "exec")
        self.assertEqual(args[1], str(self.f.cli))
        self.assertEqual(args[2], "--name")
        self.assertEqual(args[3], self.f.inventory["chatgpt"][CODEX_ID]["title"])
        self.assertEqual(len(args), 8)
        self.assertTrue(bridge.canonical_uuid(args[5]))
        self.assertEqual(args[6], "--append-system-prompt-file")
        self.assertNotIn("-p", args)

    def test_export_paths_and_permissions_are_private_unique_and_manifest_verified(self):
        _, request = self.f.prepared("chatgpt")
        first = self.f.backend.execute(request); second = self.f.backend.execute(request)
        self.assertNotEqual(first["output_directory"], second["output_directory"])
        output = Path(first["output_directory"])
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        for path in output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.suffix == ".command" else 0o600)
        manifest = json.loads((output / "manifest.json").read_text())
        self.assertEqual(manifest["transcript_sha256"], hashlib.sha256(Path(first["transcript_file"]).read_bytes()).hexdigest())
        self.assertTrue(manifest["no_model_prompt_sent"])

    def test_changed_claude_source_rejects_preview_without_import(self):
        _, request = self.f.prepared()
        self.f.session.write_text(self.f.session.read_text() + '{"type":"assistant"}\n')
        with self.assertRaisesRegex(bridge.BridgeError, "changed after the preview"):
            self.f.backend.execute(request)
        self.assertIsNone(self.f.imported_request)

    def test_changed_codex_visible_history_rejects_preview_without_export(self):
        _, request = self.f.prepared("chatgpt")
        self.f.pages[1]["data"][0]["item"]["text"] = "Changed final answer"
        with self.assertRaisesRegex(bridge.BridgeError, "changed after the preview"):
            self.f.backend.execute(request)
        self.assertFalse((self.f.control / "exports").exists())

    def test_execute_requires_exact_valid_preview_token(self):
        _, request = self.f.prepared()
        for value in (None, "", "abc", "0" * 64):
            with self.subTest(value=value), self.assertRaises(bridge.BridgeError):
                self.f.backend.execute(dict(request, selection_token=value))
        self.assertIsNone(self.f.imported_request)

    def test_native_import_filters_only_selected_sessions_and_verifies_completion(self):
        _, request = self.f.prepared()
        result = self.f.backend.execute(request)
        self.assertTrue(result["ok"])
        self.assertEqual(self.f.waited, self.f.import_id)
        self.assertEqual(result["imported_ids"], [IMPORTED_ID])
        migration = self.f.imported_request
        self.assertEqual(migration["migrationSource"], "claude-code")
        self.assertEqual(len(migration["migrationItems"]), 1)
        chosen = migration["migrationItems"][0]
        self.assertEqual(chosen["itemType"], "SESSIONS")
        self.assertEqual(set(chosen["details"]), {"sessions"})
        self.assertEqual(chosen["details"]["sessions"][0]["path"], str(self.f.session))
        self.assertTrue(self.f.session.exists())
        self.assertNotIn("launch_file", result)

    def test_unselected_session_is_not_imported(self):
        second = self.f.claude_root / "another" / (str(uuid.uuid4()) + ".jsonl")
        second.parent.mkdir(); second.write_text("unselected")
        self.f.detected["items"][-1]["details"]["sessions"].append({"path": str(second), "cwd": str(self.f.project), "title": "Unselected"})
        ids = [row["id"] for row in self.f.backend.list(self.f.request("bridge_list"))["chats"] if row["session_path"] == str(self.f.session)]
        request = self.f.request("bridge_prepare", ids=ids); prepared = self.f.backend.prepare(request)
        self.f.backend.execute(dict(request, command="bridge_execute", selection_token=prepared["selection_token"]))
        self.assertEqual(len(self.f.imported_request["migrationItems"][0]["details"]["sessions"]), 1)

    def test_partial_native_completion_is_not_success(self):
        _, request = self.f.prepared()
        self.f.completion["itemTypeResults"][0]["successes"] = []
        self.f.completion["itemTypeResults"][0]["failures"] = [{"failureStage": "import", "message": "fixture failure"}]
        result = self.f.backend.execute(request)
        self.assertFalse(result["ok"])
        self.assertEqual(result["imported_ids"], [])
        self.assertTrue(self.f.session.exists())

    def test_wrong_source_or_unknown_target_cannot_verify_import(self):
        for field, value in (("source", "/unselected.jsonl"), ("target", "unrecognized-target")):
            with self.subTest(field=field):
                original = copy.deepcopy(self.f.completion)
                _, request = self.f.prepared()
                self.f.completion["itemTypeResults"][0]["successes"][0][field] = value
                result = self.f.backend.execute(request)
                self.assertFalse(result["ok"])
                self.f.completion = original

    def test_previous_native_import_blocks_overwrite_before_execute(self):
        self.f.histories = {"data": [{"successes": [{"itemType": "SESSIONS", "source": str(self.f.session), "target": IMPORTED_ID}]}]}
        with self.assertRaisesRegex(bridge.BridgeError, "already imported"):
            self.f.prepared()
        self.assertIsNone(self.f.imported_request)

    def test_destination_id_collision_blocks_import(self):
        self.f.inventory["arbeit"][CLAUDE_ID] = {"id": CLAUDE_ID}
        with self.assertRaisesRegex(bridge.BridgeError, "already contains this session"):
            self.f.prepared()

    def test_import_history_change_invalidates_preview(self):
        _, request = self.f.prepared()
        self.f.histories["data"].append({"importId": "unrelated new import", "successes": []})
        with self.assertRaisesRegex(bridge.BridgeError, "changed after the preview"):
            self.f.backend.execute(request)
        self.assertIsNone(self.f.imported_request)

    def test_running_chat_rejected(self):
        self.f.turn_status = "inProgress"
        with self.assertRaisesRegex(bridge.BridgeError, "Stop the selected"):
            self.f.prepared("chatgpt")

    def test_repeated_page_cursor_or_items_rejected_without_partial_export(self):
        self.f.pages[1]["nextCursor"] = "page-two"
        with self.assertRaisesRegex(bridge.BridgeError, "pagination did not finish"):
            self.f.prepared("chatgpt")
        self.f.pages[1]["nextCursor"] = None
        self.f.pages[1]["data"].append(copy.deepcopy(self.f.pages[0]["data"][0]))
        with self.assertRaisesRegex(bridge.BridgeError, "repeated an item"):
            self.f.prepared("chatgpt")

    def test_large_transcript_warns_without_truncation_and_hard_limit_blocks(self):
        with mock.patch.object(bridge, "LARGE_CONTEXT", 20):
            response, request = self.f.prepared("chatgpt")
            self.assertTrue(any("large transcript" in warning for warning in response["warnings"]))
            result = self.f.backend.execute(request)
            self.assertIn("Visible final answer", Path(result["transcript_file"]).read_text())
        with mock.patch.object(bridge, "MAX_TRANSCRIPT", 20), self.assertRaisesRegex(bridge.BridgeError, "nothing was truncated"):
            self.f.prepared("chatgpt")

    def test_missing_cwd_or_cli_prevents_continuation(self):
        self.f.inventory["chatgpt"][CODEX_ID]["cwd"] = str(self.f.directory / "gone")
        with self.assertRaisesRegex(bridge.BridgeError, "project folder"):
            self.f.prepared("chatgpt")
        self.f.cli.unlink()
        with self.assertRaisesRegex(bridge.BridgeError, "CLI is not installed"):
            self.f.prepared("chatgpt")

    def test_redirected_control_folder_is_rejected(self):
        _, request = self.f.prepared("chatgpt")
        self.f.control.parent.mkdir(parents=True)
        self.f.control.symlink_to(self.f.directory)
        with self.assertRaisesRegex(bridge.BridgeError, "redirected"):
            self.f.backend.execute(request)

    def test_uuid_target_parser_accepts_only_known_identifier_shapes(self):
        self.assertEqual(bridge.import_target_id(IMPORTED_ID), IMPORTED_ID)
        self.assertEqual(bridge.import_target_id('/saved/rollout-2026-10-13T10-00-' + IMPORTED_ID + '.jsonl'), IMPORTED_ID)
        self.assertIsNone(bridge.import_target_id("not-a-target"))
        self.assertIsNone(bridge.import_target_id(None))

    def activity(self, response):
        path = self.f.control.parent / "activity" / (response["activity_id"] + ".json")
        return path, json.loads(path.read_text())

    def test_personal_native_import_uses_its_own_fixed_profile(self):
        request = self.f.request("bridge_prepare", ids=self.f.claude_selection(), destination="personal")
        preview = self.f.backend.prepare(request)
        result = self.f.backend.execute(dict(request, command="bridge_execute", selection_token=preview["selection_token"]))
        self.assertTrue(result["ok"])
        imports = [(profile, method) for profile, method, _ in self.f.calls if method == "externalAgentConfig/import"]
        self.assertEqual(imports, [(str(self.f.profiles["personal"]), "externalAgentConfig/import")])
        _, activity = self.activity(result)
        self.assertEqual(activity["destination"], "personal")

    def test_personal_continuation_preserves_source_and_has_reviewed_counts(self):
        self.f.inventory["personal"][CODEX_ID] = copy.deepcopy(self.f.inventory["chatgpt"][CODEX_ID])
        result, request = self.f.prepared("personal")
        self.assertEqual(result["counts"]["messages"], 3)
        completed = self.f.backend.execute(request)
        _, activity = self.activity(completed)
        self.assertEqual(activity["source"], "personal")
        self.assertEqual(activity["items"][0]["status"], "prepared")
        self.assertTrue(self.f.rollout.exists())

    def test_metadata_activity_is_durable_private_and_contains_no_saved_text(self):
        _, request = self.f.prepared("chatgpt")
        result = self.f.backend.execute(request)
        path, activity = self.activity(result)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(activity["status"], "completed")
        self.assertEqual(activity["action"], "continue")
        self.assertTrue(activity["items"][0]["verified"])
        self.assertEqual(activity["paths"], [result["output_directory"]])
        self.assertNotIn("Visible final answer", json.dumps(activity))
        self.assertNotIn("PRIVATE_", json.dumps(activity))

    def test_partial_native_import_activity_records_unverified_items(self):
        _, request = self.f.prepared()
        self.f.completion["itemTypeResults"][0]["successes"] = []
        result = self.f.backend.execute(request)
        _, activity = self.activity(result)
        self.assertEqual(activity["status"], "partial")
        self.assertFalse(activity["items"][0]["verified"])
        self.assertEqual(activity["items"][0]["status"], "unverified")

    def test_failed_import_has_reviewable_activity_without_removing_source(self):
        _, request = self.f.prepared()
        self.f.accept_error = True
        with self.assertRaises(bridge.BridgeError):
            self.f.backend.execute(request)
        activities = list((self.f.control.parent / "activity").glob("*.json"))
        self.assertEqual(len(activities), 1)
        activity = json.loads(activities[0].read_text())
        self.assertEqual(activity["status"], "partial")
        self.assertFalse(activity["items"][0]["verified"])
        self.assertTrue(self.f.session.exists())

    def test_accepted_import_timeout_keeps_import_id_and_journal_for_review(self):
        _, request = self.f.prepared()
        with mock.patch.object(FixtureClient, "wait_import", side_effect=bridge.BridgeError("Synthetic verification timeout")):
            with self.assertRaisesRegex(bridge.BridgeError, "Destination history may already exist"):
                self.f.backend.execute(request)
        activities = list((self.f.control.parent / "activity").glob("*.json"))
        record = json.loads(activities[0].read_text())
        self.assertEqual(record["status"], "partial")
        self.assertFalse(record["items"][0]["verified"])
        self.assertEqual(len(record["paths"]), 1)
        journal = json.loads(Path(record["paths"][0]).read_text())
        self.assertEqual(journal["state"], "accepted")
        self.assertEqual(journal["import_id"], self.f.import_id)
        self.assertTrue(journal["destination_may_have_changed"])
        self.assertTrue(self.f.session.exists())

    def test_invalid_review_does_not_record_a_transfer(self):
        _, request = self.f.prepared()
        with self.assertRaises(bridge.BridgeError):
            self.f.backend.execute(dict(request, selection_token="0" * 64))
        self.assertFalse((self.f.control.parent / "activity").exists())

    def test_bridge_list_preserves_archive_and_truthful_running_metadata(self):
        self.f.inventory["chatgpt"][CODEX_ID].update(archived=1, status="inProgress")
        row = self.f.backend.list(self.f.request("bridge_list", source="chatgpt"))["chats"][0]
        self.assertTrue(row["archived"])
        self.assertTrue(row["running"])
        claude = self.f.backend.list(self.f.request("bridge_list"))["chats"][0]
        self.assertFalse(claude["archived"])
        self.assertIsNone(claude["running"])


class NativeProtocolTests(unittest.TestCase):
    def client(self, values):
        client = bridge.NativeClient.__new__(bridge.NativeClient)
        client.incoming = queue.Queue()
        for value in values:
            client.incoming.put(value)
        client.notifications = []
        client.request_id = 0
        client.sent = []
        client._send = client.sent.append
        return client

    def test_completion_before_rpc_response_is_retained(self):
        imported = "00000004-0004-4004-8004-000000000004"
        completion = {"method": "externalAgentConfig/import/completed", "params": {"importId": imported, "itemTypeResults": []}}
        client = self.client([completion, {"id": 1, "result": {"importId": imported}}])
        response = client.request("externalAgentConfig/import", {})
        self.assertEqual(client.wait_import(response["importId"]), completion["params"])

    def test_unrelated_completion_cannot_finish_current_import(self):
        client = self.client([
            {"method": "externalAgentConfig/import/completed", "params": {"importId": "other"}},
            {"method": "externalAgentConfig/import/completed", "params": {"importId": "selected", "itemTypeResults": []}}])
        self.assertEqual(client.wait_import("selected")["importId"], "selected")

    def test_error_exposes_only_method_and_code_not_secrets(self):
        client = self.client([{"id": 1, "error": {"code": -1, "message": "PRIVATE_TOKEN"}}])
        with self.assertRaises(bridge.BridgeError) as error:
            client.request("thread/read", {})
        self.assertNotIn("PRIVATE_TOKEN", str(error.exception))

    def test_interactive_tool_request_is_refused_without_execution(self):
        client = self.client([{"id": "tool-request", "method": "item/tool/call", "params": {"secret": "do not execute"}}])
        with self.assertRaisesRegex(bridge.BridgeError, "interactive action"):
            client.request("thread/read", {})
        self.assertEqual(client.sent[-1]["error"]["code"], -32601)

    def test_timeout_does_not_report_completion(self):
        client = self.client([])
        with self.assertRaisesRegex(bridge.BridgeError, "did not finish"):
            client.wait_import("selected", timeout=0.001)


class InventoryTests(unittest.TestCase):
    def test_read_only_catalog_filters_subagents_and_preserves_settings(self):
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            profile = Path(temporary)
            database = profile / "state_5.sqlite"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE threads (id TEXT,title TEXT,name TEXT,cwd TEXT,updated_at INTEGER,archived INTEGER,agent_role TEXT,agent_nickname TEXT,source TEXT)")
            connection.execute("CREATE TABLE thread_spawn_edges (child_thread_id TEXT)")
            for identifier, role, source in [(CODEX_ID, None, "cli"), (str(uuid.uuid4()), "worker", "subagent")]:
                connection.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)", (identifier, "Fixture", None, temporary, 10, 0, role, None, source))
            connection.commit(); connection.close()
            settings = profile / "config.toml"; settings.write_text('model = "keep"\n')
            before = database.read_bytes()
            result = bridge.codex_inventory(profile)
            self.assertEqual(set(result), {CODEX_ID})
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(settings.read_text(), 'model = "keep"\n')


if __name__ == "__main__":
    unittest.main()
