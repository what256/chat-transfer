#!/usr/bin/env python3
"""Read-only installed-profile names and busy-work guards.

The historical module name is retained for imports. This public module has no
foreground watcher, app lifecycle control, or automatic transfer entry point.
"""
from __future__ import annotations
from pathlib import Path

import handoff

PROFILES = {
    "chatgpt": Path.home() / ".codex",
    "arbeit": Path.home() / "Library/Application Support/Arbeit/codex",
}
APP_NAMES = {"chatgpt": "ChatGPT", "arbeit": "ChatGPT Arbeit"}


def latest_busy(profile, ids=None):
    """Unknown or old unfinished latest turns still block an automatic restart."""
    profile = Path(profile)
    counts = {"busy": False, "active_turns": 0, "queued_requests": 0, "active_goals": 0}
    marks = ",".join("?" for _ in ids) if ids is not None else ""
    if ids is not None and not ids:
        return counts
    condition = f" AND t.thread_id IN ({marks})" if ids is not None else ""
    db = handoff.read_db(profile / "thread_history_1.sqlite")
    try:
        counts["active_turns"] = db.execute(
            "SELECT count(*) FROM thread_turns t WHERE t.status NOT IN ('completed','interrupted','failed') "
            "AND t.rollout_ordinal=(SELECT max(rollout_ordinal) FROM thread_turns WHERE thread_id=t.thread_id)" + condition,
            sorted(ids) if ids is not None else []).fetchone()[0]
    finally:
        db.close()
    for name, table, key, extra in (
        ("queue_1.sqlite", "queued_items", "queued_requests", ""),
        ("goals_1.sqlite", "thread_goals", "active_goals", "status IN ('active','usage_limited','budget_limited')"),
    ):
        path = profile / name
        if not path.exists():
            continue
        db = handoff.read_db(path)
        try:
            conditions = [extra] if extra else []
            if ids is not None:
                conditions.append(f"thread_id IN ({marks})")
            query = f'SELECT count(*) FROM "{table}"'
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            counts[key] = db.execute(query, sorted(ids) if ids is not None else []).fetchone()[0]
        finally:
            db.close()
    counts["busy"] = any(counts[key] for key in ("active_turns", "queued_requests", "active_goals"))
    return counts


