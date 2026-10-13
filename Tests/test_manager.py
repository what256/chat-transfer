"""Synthetic chat manager tests; never open real profiles or stop apps."""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Backend"))

import importlib.util
import json
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "Backend"))
from test_handoff import ProfileFixture, ROOT_A, ROOT_B, CHILD_A, UNRELATED_SOURCE, UNRELATED_DESTINATION

spec = importlib.util.spec_from_file_location("manager_under_test", HERE.parent / "Backend/manager.py")
manager = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = manager
spec.loader.exec_module(manager)

ARBITRARY = "88888888-8888-4888-8888-888888888888"
ANCESTOR = "99999999-9999-4999-8999-999999999999"


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chat-manager-test-", dir="/private/tmp")
        self.base = Path(self.temporary.name)
        self.source = ProfileFixture(self.base, "source")
        self.destination = ProfileFixture(self.base, "destination")
        self.personal = ProfileFixture(self.base, "personal")
        self.control = self.base / "manager-control"
        self.legacy = self.base / "legacy-control"
        self.legacy.mkdir()
        self.profiles = {"chatgpt": self.source.path, "arbeit": self.destination.path, "personal": self.personal.path}
        for identifier in (ROOT_A, ROOT_B, CHILD_A, ARBITRARY):
            self.source.add_thread(identifier)
        self.source.add_edge(ROOT_A, CHILD_A)
        self.source.add_thread(UNRELATED_SOURCE, references=True)
        self.destination.add_thread(UNRELATED_DESTINATION, references=True)
        self.source_sentinels = self.source.sentinel_files()
        self.destination_sentinels = self.destination.sentinel_files()
        self.personal_sentinels = self.personal.sentinel_files()
        self.closed = mock.patch.object(manager.handoff, "target_is_closed", return_value=True)
        self.closed.start()

    def tearDown(self):
        self.closed.stop()
        self.temporary.cleanup()

    def request(self, operation="copy", ids=None, source="chatgpt", command="execute", **extras):
        return {"command": command, "operation": operation, "source": source,
                "ids": [ROOT_A] if ids is None else ids, **extras}

    def dispatch(self, request):
        return manager.dispatch(request, control=self.control, profiles=self.profiles,
                                legacy_control=self.legacy)

    def state(self, identifier):
        return json.loads(manager.state_path(self.control, identifier).read_text())

    def assert_private_settings(self):
        self.assertEqual(self.source.sentinel_files(), self.source_sentinels)
        self.assertEqual(self.destination.sentinel_files(), self.destination_sentinels)
        self.assertEqual(self.personal.sentinel_files(), self.personal_sentinels)

    def profile_content(self, profile, identifier):
        return manager.content_fingerprint(manager.handoff.collect(profile.path, (identifier,)))

    def mark_active(self, profile, identifier):
        profile.mutate("thread_history_1.sqlite",
                       "UPDATE thread_turns SET status='inProgress',completed_at=NULL WHERE thread_id=?",
                       (identifier,))

    def test_copy_arbitrary_selected_chat_into_absent_destination(self):
        source_before = self.source.snapshot()
        result = self.dispatch(self.request(ids=[ARBITRARY]))
        self.assertTrue(result["ok"])
        self.assertTrue(result["results"][0]["verified"])
        self.assertEqual(self.source.snapshot(), source_before)
        self.assertEqual(self.profile_content(self.source, ARBITRARY),
                         self.profile_content(self.destination, ARBITRARY))
        self.assertEqual({row[0] for row in self.destination.rows("threads")},
                         {ARBITRARY, UNRELATED_DESTINATION})
        self.assert_private_settings()

    def test_copy_scope_includes_saved_child_and_preserves_unrelated_threads(self):
        unrelated_source = self.source.thread_row(UNRELATED_SOURCE)
        unrelated_target = self.destination.thread_row(UNRELATED_DESTINATION)
        self.dispatch(self.request())
        self.assertEqual({row[0] for row in self.destination.rows("threads")},
                         {ROOT_A, CHILD_A, UNRELATED_DESTINATION})
        self.assertEqual(self.source.thread_row(UNRELATED_SOURCE), unrelated_source)
        self.assertEqual(self.destination.thread_row(UNRELATED_DESTINATION), unrelated_target)
        self.assertIsNone(self.destination.thread_row(ROOT_B))
        self.assert_private_settings()

    def test_inherited_rollout_is_copied_without_adding_ancestor_chat_row(self):
        path, data = self.source.add_thread(ANCESTOR)
        self.source.attach_history_base(ROOT_A, {"thread_id": ANCESTOR,
                                                "end_byte_offset": len(data),
                                                "end_ordinal_exclusive": 4})
        result = self.dispatch(self.request())
        self.assertTrue(result["ok"])
        self.assertIsNone(self.destination.thread_row(ANCESTOR))
        copied = self.destination.path / path.relative_to(self.source.path)
        self.assertEqual(copied.read_bytes(), data)
        self.assertEqual({row[0] for row in self.destination.rows("threads")},
                         {ROOT_A, CHILD_A, UNRELATED_DESTINATION})

    def test_move_copies_then_archives_only_source_root_and_keeps_rollouts(self):
        paths = {identifier: Path(self.source.thread_row(identifier)["rollout_path"])
                 for identifier in (ROOT_A, CHILD_A)}
        before = {identifier: path.read_bytes() for identifier, path in paths.items()}
        result = self.dispatch(self.request("move"))
        self.assertTrue(result["ok"])
        self.assertEqual(self.source.thread_row(ROOT_A)["archived"], 1)
        self.assertEqual(self.source.thread_row(CHILD_A)["archived"], 0)
        self.assertEqual(self.destination.thread_row(ROOT_A)["archived"], 0)
        for identifier, path in paths.items():
            self.assertEqual(self.source.thread_row(identifier)["rollout_path"], str(path))
            self.assertEqual(path.read_bytes(), before[identifier])
        self.assertEqual(self.profile_content(self.source, ROOT_A), self.profile_content(self.destination, ROOT_A))
        self.assert_private_settings()

    def test_reverse_move_restores_original_visibility_and_preserves_work(self):
        self.dispatch(self.request("move"))
        self.destination.append_completed_turn(ROOT_A)
        result = self.dispatch(self.request("move", source="arbeit"))
        self.assertTrue(result["ok"])
        self.assertEqual(self.source.thread_row(ROOT_A)["archived"], 0)
        self.assertEqual(self.destination.thread_row(ROOT_A)["archived"], 1)
        self.assertEqual(self.profile_content(self.source, ROOT_A), self.profile_content(self.destination, ROOT_A))
        self.assertEqual(result["results"][0]["origin"], "arbeit")
        self.assert_private_settings()

    def test_sync_after_move_can_show_archived_destination(self):
        self.dispatch(self.request("move"))
        result = self.dispatch(self.request("sync", source="arbeit"))
        self.assertTrue(result["ok"])
        self.assertEqual(self.source.thread_row(ROOT_A)["archived"], 0)
        self.assertEqual(self.destination.thread_row(ROOT_A)["archived"], 0)
        self.assert_private_settings()

    def test_source_busy_prevents_any_copy(self):
        self.mark_active(self.source, ROOT_A)
        before_source, before_target = self.source.snapshot(), self.destination.snapshot()
        result = self.dispatch(self.request())
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertEqual(self.source.snapshot(), before_source)
        self.assertEqual(self.destination.snapshot(), before_target)

    def test_destination_selected_busy_prevents_overwrite(self):
        self.dispatch(self.request())
        self.mark_active(self.destination, ROOT_A)
        before_source, before_target = self.source.snapshot(), self.destination.snapshot()
        result = self.dispatch(self.request("sync"))
        self.assertFalse(result["ok"])
        self.assertEqual(self.source.snapshot(), before_source)
        self.assertEqual(self.destination.snapshot(), before_target)

    def test_open_destination_requires_close_and_no_database_changes(self):
        before = self.destination.snapshot()
        with mock.patch.object(manager.handoff, "target_is_closed", return_value=False):
            result = self.dispatch(self.request())
        self.assertFalse(result["ok"])
        self.assertEqual(result["needsClose"], ["arbeit"])
        self.assertEqual(result["canRestart"], ["arbeit"])
        self.assertEqual(self.destination.snapshot(), before)

    def test_open_busy_unrelated_destination_cannot_restart(self):
        self.mark_active(self.destination, UNRELATED_DESTINATION)
        before = self.destination.snapshot()
        with mock.patch.object(manager.handoff, "target_is_closed", return_value=False):
            result = self.dispatch(self.request())
        self.assertFalse(result["ok"])
        self.assertEqual(result["needsClose"], ["arbeit"])
        self.assertEqual(result["canRestart"], [])
        self.assertEqual(self.destination.snapshot(), before)

    def test_independent_root_baselines_choose_opposite_sync_directions(self):
        self.dispatch(self.request(ids=[ROOT_A, ROOT_B]))
        self.source.append_completed_turn(ROOT_A, turn_id="source-new-turn")
        self.destination.append_completed_turn(ROOT_B, turn_id="destination-new-turn")
        preview = self.dispatch(self.request("sync", ids=[ROOT_A, ROOT_B], command="prepare"))
        directions = {plan["id"]: (plan["origin"], plan["target"]) for plan in preview["plans"]}
        self.assertEqual(directions, {ROOT_A: ("chatgpt", "arbeit"), ROOT_B: ("arbeit", "chatgpt")})
        result = self.dispatch(self.request("sync", ids=[ROOT_A, ROOT_B]))
        self.assertTrue(result["ok"])
        for identifier in (ROOT_A, ROOT_B):
            self.assertEqual(self.profile_content(self.source, identifier),
                             self.profile_content(self.destination, identifier))
            self.assertEqual(set(self.state(identifier)["expected"]), {"chatgpt", "arbeit"})
        self.assert_private_settings()

    def test_conflicting_existing_copy_requires_explicit_replacement(self):
        self.destination.add_thread(ARBITRARY, title="Different destination work")
        before_source, before_target = self.source.snapshot(), self.destination.snapshot()
        preview = self.dispatch(self.request(ids=[ARBITRARY], command="prepare"))
        self.assertTrue(preview["requiresReplacement"])
        self.assertEqual(preview["preview"][0]["status"], "confirmation")
        for operation in ("copy", "move"):
            with self.subTest(operation=operation):
                result = self.dispatch(self.request(operation, ids=[ARBITRARY]))
                self.assertFalse(result["ok"])
                self.assertEqual(self.source.snapshot(), before_source)
                self.assertEqual(self.destination.snapshot(), before_target)

    def test_explicit_replacement_copies_and_keeps_recovery_backup(self):
        self.destination.add_thread(ARBITRARY, title="Different destination work")
        result = self.dispatch(self.request(ids=[ARBITRARY], replace_existing=True))
        self.assertTrue(result["ok"])
        self.assertEqual(self.profile_content(self.source, ARBITRARY),
                         self.profile_content(self.destination, ARBITRARY))
        backups = list((manager.pair_namespace(self.control, ARBITRARY) / "backups").iterdir())
        self.assertTrue(backups)
        preserved = [backup / "destination" / "state_5.sqlite" for backup in backups]
        self.assertTrue(any(path.is_file() for path in preserved))
        saved = manager.handoff.read_db(next(path for path in preserved if path.is_file()))
        try:
            self.assertEqual(saved.execute("SELECT title FROM threads WHERE id=?", (ARBITRARY,)).fetchone()[0],
                             "Different destination work")
        finally:
            saved.close()
        self.assert_private_settings()

    def test_sync_conflict_does_not_obey_replacement_flag(self):
        self.destination.add_thread(ARBITRARY, title="Different destination work")
        before = self.destination.snapshot()
        result = self.dispatch(self.request("sync", ids=[ARBITRARY], replace_existing=True))
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertEqual(self.destination.snapshot(), before)

    def test_both_roots_conflicted_does_not_partially_copy_ready_root(self):
        self.destination.add_thread(ROOT_B, title="Different destination work")
        before = self.destination.snapshot()
        result = self.dispatch(self.request("sync", ids=[ROOT_A, ROOT_B]))
        self.assertFalse(result["ok"])
        self.assertIsNone(self.destination.thread_row(ROOT_A))
        self.assertEqual(self.destination.snapshot(), before)

    def test_list_is_read_only_and_uses_display_name(self):
        self.source.mutate("state_5.sqlite", "UPDATE threads SET name='Friendly fixture name' WHERE id=?", (ARBITRARY,))
        before_source, before_target = self.source.snapshot(), self.destination.snapshot()
        result = self.dispatch({"command": "list", "source": "chatgpt"})
        chats = {item["id"]: item for item in result["chats"]}
        self.assertEqual(result["default_ids"], [])
        self.assertEqual(chats[ARBITRARY]["title"], "Friendly fixture name")
        self.assertFalse(chats[ARBITRARY]["present_in_both"])
        self.assertEqual(self.source.snapshot(), before_source)
        self.assertEqual(self.destination.snapshot(), before_target)

    def test_invalid_ids_are_rejected_before_profile_mutations(self):
        values = ([], [ROOT_A, ROOT_A], ["../../escape"], ["unknown"], [[ROOT_A]], [{"id": ROOT_A}])
        for ids in values:
            with self.subTest(ids=ids):
                before_source, before_target = self.source.snapshot(), self.destination.snapshot()
                with self.assertRaises(manager.handoff.HandoffError):
                    self.dispatch(self.request(ids=ids))
                self.assertEqual(self.source.snapshot(), before_source)
                self.assertEqual(self.destination.snapshot(), before_target)

    def test_personal_explicit_routes_keep_pair_baselines_independent(self):
        original = self.dispatch(self.request(ids=[ARBITRARY], destination="personal"))
        self.assertTrue(original["ok"])
        self.assertIsNone(self.destination.thread_row(ARBITRARY))
        second = self.dispatch(self.request(ids=[ARBITRARY], source="personal", destination="arbeit"))
        self.assertTrue(second["ok"])
        left = manager.state_path(self.control, ARBITRARY, "chatgpt", "personal")
        right = manager.state_path(self.control, ARBITRARY, "personal", "arbeit")
        self.assertNotEqual(left, right)
        self.assertEqual(set(json.loads(left.read_text())["expected"]), {"chatgpt", "personal"})
        self.assertEqual(set(json.loads(right.read_text())["expected"]), {"personal", "arbeit"})
        self.assertEqual(right, manager.state_path(self.control, ARBITRARY, "arbeit", "personal"))
        self.assert_private_settings()

    def test_list_uses_selected_destination_and_lists_all_presence_and_project_folder(self):
        self.personal.add_thread(ARBITRARY)
        request = {"command": "list", "source": "chatgpt", "destination": "personal"}
        chats = {row["id"]: row for row in self.dispatch(request)["chats"]}
        self.assertTrue(chats[ARBITRARY]["present_in_destination"])
        self.assertEqual(set(chats[ARBITRARY]["profiles"]), {"chatgpt", "personal"})
        self.assertEqual(chats[ARBITRARY]["cwd"], str(self.source.workspace))
        request["destination"] = "arbeit"
        self.assertFalse(next(row for row in self.dispatch(request)["chats"] if row["id"] == ARBITRARY)["present_in_destination"])

    def test_routes_cannot_point_to_same_unknown_or_unselected_profile(self):
        before = self.source.snapshot()
        for destination in ("chatgpt", "unknown", "../personal"):
            with self.subTest(destination=destination), self.assertRaises(manager.handoff.HandoffError):
                self.dispatch(self.request(destination=destination))
        self.assertEqual(self.source.snapshot(), before)
        self.assertFalse((self.control / "activity").exists())

    def test_review_token_binds_route_selection_and_replacement_choice(self):
        request = self.request(ids=[ARBITRARY], destination="personal", command="prepare")
        token = self.dispatch(request)["selection_token"]
        before = self.personal.snapshot()
        changes = ({"destination": "arbeit"}, {"ids": [ROOT_A]}, {"replace_existing": True},
                   {"source": "personal", "destination": "chatgpt"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(manager.handoff.HandoffError):
                self.dispatch(dict(request, command="execute", selection_token=token, **change))
        self.assertEqual(self.personal.snapshot(), before)
        self.assertFalse((self.control / "activity").exists())

    def test_review_token_rejects_saved_history_changed_since_preview(self):
        request = self.request(ids=[ARBITRARY], destination="personal", command="prepare")
        token = self.dispatch(request)["selection_token"]
        self.source.mutate("state_5.sqlite", "UPDATE threads SET name='Changed after review' WHERE id=?", (ARBITRARY,))
        before = self.personal.snapshot()
        with self.assertRaisesRegex(manager.handoff.HandoffError, "changed after review"):
            self.dispatch(dict(request, command="execute", selection_token=token))
        self.assertEqual(self.personal.snapshot(), before)

    def test_review_token_survives_required_destination_closing(self):
        request = self.request(ids=[ARBITRARY], command="prepare")
        with mock.patch.object(manager.handoff, "target_is_closed", return_value=False):
            token = self.dispatch(request)["selection_token"]
        result = self.dispatch(dict(request, command="execute", selection_token=token))
        self.assertTrue(result["ok"])

    def test_old_original_pair_state_is_used_only_for_original_pair(self):
        self.dispatch(self.request(ids=[ARBITRARY]))
        current = manager.state_path(self.control, ARBITRARY)
        legacy = self.control / "chats" / ARBITRARY / "pair-state.json"
        legacy.parent.mkdir(parents=True)
        current.replace(legacy)
        self.source.mutate("state_5.sqlite", "UPDATE threads SET name='Next original work' WHERE id=?", (ARBITRARY,))
        self.assertTrue(self.dispatch(self.request("sync", ids=[ARBITRARY]))["ok"])
        self.assertTrue(current.is_file())
        self.assertTrue(legacy.is_file())
        self.personal.add_thread(ARBITRARY, title="Different Personal work")
        prepared = self.dispatch(self.request("sync", ids=[ARBITRARY], destination="personal", command="prepare"))
        self.assertTrue(prepared["blocked"])
        self.assertIsNone(manager.load_state(self.control, ARBITRARY, "chatgpt", "personal"))

    def test_unfinished_legacy_operation_is_not_bypassed_by_new_pair_namespace(self):
        legacy = self.control / "chats" / ARBITRARY
        legacy.mkdir(parents=True)
        (legacy / "operation.json").write_text(json.dumps({"status": "prepared"}))
        before = self.destination.snapshot()
        result = self.dispatch(self.request(ids=[ARBITRARY]))
        self.assertFalse(result["ok"])
        self.assertIn("needs recovery", result["preview"][0]["message"])
        self.assertEqual(self.destination.snapshot(), before)

    def test_activity_keeps_private_metadata_and_completed_backup_refs(self):
        response = self.dispatch(self.request(ids=[ARBITRARY], destination="personal"))
        path = self.control / "activity" / (response["activity_id"] + ".json")
        record = json.loads(path.read_text())
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["destination"], "personal")
        self.assertTrue(record["items"][0]["verified"])
        self.assertTrue(record["paths"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn("büro", path.read_text())
        self.assertTrue(all(Path(value).is_dir() and self.control in Path(value).parents for value in record["paths"]))
        second = self.dispatch(self.request("sync", ids=[ARBITRARY], destination="personal"))
        self.assertNotEqual(response["activity_id"], second["activity_id"])
        self.assertTrue(path.is_file())

    def test_batch_partial_failure_reports_completed_and_unrun_items_truthfully(self):
        original_transfer = manager.handoff.transfer
        def fail_second(source, destination, control, **options):
            if options["roots"] == (ROOT_B,):
                raise manager.handoff.HandoffError("Fixture failure before second import")
            return original_transfer(source, destination, control, **options)
        with mock.patch.object(manager.handoff, "transfer", side_effect=fail_second):
            result = self.dispatch(self.request(ids=[ROOT_A, ROOT_B, ARBITRARY]))
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial"])
        record = json.loads((self.control / "activity" / (result["activity_id"] + ".json")).read_text())
        self.assertEqual(record["status"], "partial")
        self.assertEqual([row["status"] for row in record["items"]], ["completed", "failed", "not-run"])
        self.assertEqual([row["verified"] for row in record["items"]], [True, False, False])
        self.assertIsNotNone(self.destination.thread_row(ROOT_A))
        self.assertIsNone(self.destination.thread_row(ROOT_B))
        self.assertIsNone(self.destination.thread_row(ARBITRARY))
        self.assert_private_settings()

    def test_failed_initial_import_is_recorded_without_claiming_partial_success(self):
        before = self.destination.snapshot()
        with mock.patch.object(manager.handoff, "transfer", side_effect=manager.handoff.HandoffError("Fixture initial failure")):
            result = self.dispatch(self.request(ids=[ARBITRARY]))
        self.assertFalse(result["ok"])
        self.assertFalse(result["partial"])
        record = json.loads((self.control / "activity" / (result["activity_id"] + ".json")).read_text())
        self.assertEqual(record["status"], "failed")
        self.assertFalse(record["items"][0]["verified"])
        self.assertEqual(record["paths"], [])
        self.assertEqual(self.destination.snapshot(), before)


    def legacy_baseline(self, roots=(ARBITRARY,), **overrides):
        snapshot = manager.handoff.collect(self.source.path, roots)
        record = {"roots": list(roots), "profiles": [str(self.source.path), str(self.destination.path)],
                  "fingerprint": snapshot["fingerprint"]}
        record.update(overrides)
        path = self.legacy / "baseline.json"
        path.write_text(json.dumps(record))
        return path

    def test_legacy_seed_uses_recorded_arbitrary_roots_without_hardcoded_ids(self):
        self.legacy_baseline()
        before = self.source.snapshot()
        states = manager.seed_legacy_state({"chatgpt": self.source.path, "arbeit": self.destination.path}, self.legacy)
        self.assertEqual(set(states), {ARBITRARY})
        self.assertTrue(states[ARBITRARY]["seeded_from_verified_legacy"])
        self.assertEqual(self.source.snapshot(), before)
        self.assert_private_settings()

    def test_legacy_seed_rejects_unrelated_profile_membership(self):
        self.legacy_baseline(profiles=["/fixture/unrelated", "/fixture/other"])
        with mock.patch.object(manager.handoff, "collect", side_effect=AssertionError("No profile reads")):
            self.assertEqual(manager.seed_legacy_state(self.profiles, self.legacy), {})

    def test_legacy_seed_rejects_empty_invalid_oversized_or_redirected_metadata(self):
        profile_pair = {"chatgpt": self.source.path, "arbeit": self.destination.path}
        path = self.legacy / "baseline.json"
        for record in ({"roots": []}, {"roots": ["invalid"]}, [], "x" * (256 * 1024 + 1)):
            with self.subTest(kind=type(record).__name__):
                path.write_text(json.dumps(record))
                with mock.patch.object(manager.handoff, "collect", side_effect=AssertionError("No profile reads")):
                    self.assertEqual(manager.seed_legacy_state(profile_pair, self.legacy), {})
        outside = self.base / "redirected-baseline.json"
        outside.write_text("{}")
        path.unlink()
        path.symlink_to(outside)
        self.assertEqual(manager.seed_legacy_state(profile_pair, self.legacy), {})

    def test_legacy_seed_keeps_changed_history_unseeded(self):
        self.legacy_baseline()
        self.source.append_completed_turn(ARBITRARY)
        self.assertEqual(manager.seed_legacy_state({"chatgpt": self.source.path, "arbeit": self.destination.path}, self.legacy), {})


if __name__ == "__main__":
    unittest.main()
