"""Synthetic tests for read-only busy-work guards.

All fixtures live in a disposable temporary directory. No installed app is
stopped, launched, queried, or edited, and no real chat profile is opened.
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Backend"))

import importlib.util
import sqlite3
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_handoff import ProfileFixture, ROOT_A, ROOT_B, CHILD_A, UNRELATED_SOURCE


spec = importlib.util.spec_from_file_location("auto_sync_under_test", HERE.parent / "Backend/auto_sync.py")
auto_sync = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = auto_sync
spec.loader.exec_module(auto_sync)


class BusyFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chat-auto-sync-test-", dir="/private/tmp")
        self.base = Path(self.temporary.name)
        self.source = ProfileFixture(self.base, "source")
        self.destination = ProfileFixture(self.base, "destination")
        self.control = self.base / "control"
        self.control.mkdir()
        for profile in (self.source, self.destination):
            for identifier in (ROOT_A, ROOT_B, CHILD_A):
                profile.add_thread(identifier)
            profile.add_edge(ROOT_A, CHILD_A)

    def tearDown(self):
        self.temporary.cleanup()

    def mark_active(self, profile, identifier, *, started_at=1):
        profile.mutate("thread_history_1.sqlite",
                       "UPDATE thread_turns SET status='inProgress',started_at=?,completed_at=NULL WHERE thread_id=?",
                       (started_at, identifier))

    def auxiliary(self, profile, filename, table, rows):
        connection = sqlite3.connect(profile.path / filename)
        connection.execute(f'CREATE TABLE "{table}" (thread_id TEXT,status TEXT)')
        connection.executemany(f'INSERT INTO "{table}" VALUES (?,?)', rows)
        connection.commit()
        connection.close()


class BusyGuards(BusyFixture):
    def test_completed_threads_are_idle(self):
        result = auto_sync.latest_busy(self.source.path)
        self.assertFalse(result["busy"])
        self.assertEqual(result["active_turns"], 0)

    def test_old_nonterminal_latest_turn_remains_busy(self):
        self.mark_active(self.source, ROOT_A, started_at=1)
        result = auto_sync.latest_busy(self.source.path, {ROOT_A, ROOT_B, CHILD_A})
        self.assertTrue(result["busy"])
        self.assertEqual(result["active_turns"], 1)

    def test_latest_terminal_turn_can_supersede_an_old_nonterminal_turn(self):
        self.mark_active(self.source, ROOT_A, started_at=1)
        self.source.append_completed_turn(ROOT_A)
        result = auto_sync.latest_busy(self.source.path, {ROOT_A})
        self.assertFalse(result["busy"])

    def test_unrelated_work_blocks_restart_but_not_selected_scope_check(self):
        self.source.add_thread(UNRELATED_SOURCE, completed=False)
        self.assertTrue(auto_sync.latest_busy(self.source.path)["busy"])
        self.assertFalse(auto_sync.latest_busy(self.source.path, {ROOT_A, ROOT_B, CHILD_A})["busy"])

    def test_active_descendant_is_busy(self):
        self.mark_active(self.source, CHILD_A)
        result = auto_sync.latest_busy(self.source.path, {ROOT_A, ROOT_B, CHILD_A})
        self.assertTrue(result["busy"])

    def test_queued_request_blocks_restart(self):
        self.auxiliary(self.source, "queue_1.sqlite", "queued_items", [(ROOT_A, "queued")])
        result = auto_sync.latest_busy(self.source.path)
        self.assertTrue(result["busy"])
        self.assertEqual(result["queued_requests"], 1)

    def test_unrelated_queue_is_included_in_restart_guard(self):
        self.auxiliary(self.source, "queue_1.sqlite", "queued_items", [(UNRELATED_SOURCE, "queued")])
        self.assertTrue(auto_sync.latest_busy(self.source.path)["busy"])
        self.assertFalse(auto_sync.latest_busy(self.source.path, {ROOT_A, ROOT_B})["busy"])

    def test_active_and_usage_limited_goals_block_restart(self):
        for status in ("active", "usage_limited", "budget_limited"):
            with self.subTest(status=status):
                filename = self.source.path / "goals_1.sqlite"
                filename.unlink(missing_ok=True)
                self.auxiliary(self.source, "goals_1.sqlite", "thread_goals", [(ROOT_A, status)])
                result = auto_sync.latest_busy(self.source.path)
                self.assertTrue(result["busy"])
                self.assertEqual(result["active_goals"], 1)

    def test_finished_and_paused_goals_do_not_claim_running_work(self):
        self.auxiliary(self.source, "goals_1.sqlite", "thread_goals",
                       [(ROOT_A, "complete"), (ROOT_B, "paused")])
        self.assertFalse(auto_sync.latest_busy(self.source.path)["busy"])

    def test_inspection_leaves_profile_bytes_unchanged(self):
        before = self.source.snapshot()
        auto_sync.latest_busy(self.source.path)
        self.assertEqual(self.source.snapshot(), before)




class PublicSurfaceTests(unittest.TestCase):
    def test_legacy_automatic_transfer_entry_points_are_absent(self):
        for name in ("evaluate", "transfer_request", "main", "retain_automatic_backups"):
            self.assertFalse(hasattr(auto_sync, name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
