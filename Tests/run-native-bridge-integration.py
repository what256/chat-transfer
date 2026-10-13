#!/usr/bin/env python3
"""
Probe native import with two disposable transcripts and a temporary Codex home.

Never edits an existing session, starts a model turn, or imports real data. Claude
configuration isolation uses its documented CLAUDE_CONFIG_DIR; HOME is unchanged.
Current Codex versions ignore that source override. The explicit
--stage-disposable-sources option creates a unique test-only project directory
under the detector's source root, removes only its two fixtures in finally,
and never modifies an existing Claude session or imports real data.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Backend"))

import argparse
import datetime
import hashlib
import json
import os
import tempfile
from unittest import mock
import uuid

from bridge import Bridge, NativeClient, import_target_id


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(config, project, number):
    identifier = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    assistant_id = str(uuid.uuid4())
    path = config / "projects" / str(project).replace("/", "-") / (identifier + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    common = {"sessionId": identifier, "cwd": str(project), "version": "2.1.235",
              "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(), "isSidechain": False}
    user_text = f"Synthetic native import user fixture {number}"
    assistant_text = f"Synthetic native import assistant fixture {number}"
    records = [dict(common, type="user", uuid=user_id, parentUuid=None,
                    message={"role": "user", "content": user_text}),
               dict(common, type="assistant", uuid=assistant_id, parentUuid=user_id,
                    message={"id": "msg_fixture_" + str(number), "type": "message", "role": "assistant",
                             "model": "claude-fixture", "content": [{"type": "text", "text": assistant_text}],
                             "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return {"path": str(path), "cwd": str(project), "title": f"Native fixture {number}"}, [user_text, assistant_text]


def run(stage=False):
    with tempfile.TemporaryDirectory(prefix="chat-transfer-native-", dir="/private/tmp") as temporary:
        root = Path(temporary)
        profile = root / "codex"
        profile.mkdir()
        project = root / "synthetic project $() `literal`"
        project.mkdir()
        config = Path.home() / ".claude" if stage else root / "claude-config"
        source_directory = config / "projects" / str(project).replace("/", "-")
        if stage:
            assert (config / "projects").is_dir() and not (config / "projects").is_symlink()
            assert not source_directory.exists(), "Refuse to reuse any existing source directory"
        sessions, expected_text = [], {}
        try:
            for number in (1, 2):
                session, text = fixture(config, project, number)
                sessions.append(session)
                expected_text[session["path"]] = text
                Path(session["path"]).chmod(0o600)
            source_directory.chmod(0o700)
            return exercise(root, profile, project, config, sessions, expected_text)
        finally:
            if stage:
                for session in sessions:
                    Path(session["path"]).unlink(missing_ok=True)
                if source_directory.exists():
                    source_directory.rmdir()


def exercise(root, profile, project, config, sessions, expected_text):
        fingerprints = {session["path"]: digest(Path(session["path"])) for session in sessions}
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}), NativeClient(profile) as client:
            detected = client.request("externalAgentConfig/detect", {"includeHome": True, "cwds": [str(project)],
                                      "migrationSource": "claude-code", "maxSessionAgeDays": 36500, "maxSessions": 10000})
            detected_sessions = [session for item in detected.get("items", []) if item.get("itemType") == "SESSIONS"
                                 for session in (item.get("details") or {}).get("sessions", [])]
            fixture_paths = set(expected_text)
            detected_paths = {session.get("path") for session in detected_sessions if session.get("path") in fixture_paths}
            # Only the two owned temporary paths are ever submitted. Detection
            # may also list real chats; those entries are discarded immediately.
            imported = client.request("externalAgentConfig/import", {
                "migrationSource": "claude-code", "source": "chat-transfer-native-fixture",
                "migrationItems": [{"itemType": "SESSIONS", "description": "Two disposable fixture sessions",
                                    "cwd": None, "details": {"sessions": sessions}}]})
            completion = client.wait_import(imported["importId"])
            groups = completion.get("itemTypeResults", [])
            successes = [success for group in groups for success in group.get("successes", [])]
            failures = [failure for group in groups for failure in group.get("failures", [])]
            verified = []
            for success in successes:
                if success.get("source") not in fixture_paths:
                    raise AssertionError("Native importer returned an unselected source.")
                identifier = import_target_id(success.get("target"))
                if not identifier:
                    raise AssertionError("Native importer returned an unrecognized target identifier.")
                thread = client.request("thread/read", {"threadId": identifier, "includeTurns": False}).get("thread", {})
                if thread.get("id") != identifier:
                    raise AssertionError("Imported fixture could not be read.")
                items = client.request("thread/turns/list", {"threadId": identifier, "limit": 50, "sortDirection": "asc", "itemsView": "full"})
                texts = []
                for item in [item for turn in items.get("data", []) for item in turn.get("items", [])]:
                    if item.get("type") == "userMessage":
                        texts.extend(part["text"] for part in item.get("content", []) if part.get("type") == "text")
                    elif item.get("type") == "agentMessage":
                        texts.append(item.get("text"))
                expected = expected_text[success["source"]]
                if texts[:len(expected)] != expected or any(text != "<EXTERNAL SESSION IMPORTED>" for text in texts[len(expected):]):
                    raise AssertionError("Imported fixture text or order differs from its source.")
                verified.append(identifier)
            if fingerprints != {session["path"]: digest(Path(session["path"])) for session in sessions}:
                raise AssertionError("Native import changed a fixture source.")
            if detected_paths != fixture_paths:
                assert not successes, "Undetected temporary sessions were unexpectedly imported."
                assert len(failures) == 2 and all(failure.get("subErrorType") == "session_not_detected" for failure in failures)
                return {"status": "conversion_untested_source_override_unsupported", "fixture_count": 2,
                        "detected_fixture_count": len(detected_paths), "arbitrary_path_rejection_verified": True,
                        "fixture_sources_unchanged": True, "real_sessions_imported": 0,
                        "note": "Native Claude detection ignored CLAUDE_CONFIG_DIR. An isolated OS user/source home is required to test conversion without writing the real Claude profile."}, 2
            assert not failures and len(verified) == 2
            # Verify the reverse bridge against the native converter's genuine
            # saved records. Only a disposable CLI captures argv; no Claude
            # process or model turn is started.
            captured = root / "captured.json"
            mock_cli = root / "claude mock"
            mock_cli.write_text("#!/usr/bin/python3\nimport json,os,sys\n"
                                + "json.dump({'cwd':os.getcwd(),'argv':sys.argv},open(" + repr(str(captured)) + ",'w'))\n")
            mock_cli.chmod(0o700)
            backend = Bridge(profiles={"chatgpt": profile, "arbeit": profile}, control=root / "bridge",
                             claude_cli=mock_cli)
            prepared = backend.prepare({"source": "chatgpt", "destination": "claude", "ids": [verified[0]]})
            exported = backend.execute({"source": "chatgpt", "destination": "claude", "ids": [verified[0]],
                                        "selection_token": prepared["selection_token"]})
            assert exported["ok"]
            transcript = Path(exported["transcript_file"]).read_text()
            first_source = next(success["source"] for success in successes if import_target_id(success["target"]) == verified[0])
            assert all(text in transcript for text in expected_text[first_source])
            import subprocess
            subprocess.run(["/bin/zsh", exported["launch_file"]], check=True, capture_output=True)
            arguments = json.loads(captured.read_text())
            assert arguments["cwd"] == str(project)
            assert arguments["argv"][1::2] == ["--name", "--session-id", "--append-system-prompt-file"]
            assert len(arguments["argv"]) == 7
            assert "Synthetic native import user fixture" not in Path(exported["output_directory"], "continuation.md").read_text()
            return {"status": "passed", "fixture_count": 2, "verified_imports": 2,
                    "fixture_sources_unchanged": True, "real_sessions_imported": 0,
                    "public_methods_only": True, "model_turns_started": 0,
                    "reverse_visible_transcript_verified": True, "mock_claude_launcher_verified": True,
                    "source_fixtures_removed": True}, 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage-disposable-sources", action="store_true")
    options = parser.parse_args()
    result, code = run(options.stage_disposable_sources)
    print(json.dumps(result, indent=2))
    raise SystemExit(code)
