"""Ensure tickets have Kanban-friendly statuses for the demo board.

Maps legacy 'open' (and any unknown) values into todo / in_progress / done
without changing ticket_ids, categories, or teams.
"""
from indexops.db import execute, fetch_one


def ensure_kanban_statuses() -> dict:
    """Idempotent: only rewrites rows that are not already todo/in_progress/done."""
    before = fetch_one(
        """SELECT
             COUNT(*) FILTER (WHERE status NOT IN ('todo','in_progress','done')) AS needs_update,
             COUNT(*) AS total
           FROM tickets"""
    )
    if not before or (before["needs_update"] or 0) == 0:
        return {"updated": 0, "total": (before or {}).get("total", 0)}

    # Deterministic mix from ticket_id so the board is stable across refreshes.
    execute(
        """UPDATE tickets SET status = CASE
               WHEN ticket_id % 5 IN (0, 1) THEN 'todo'
               WHEN ticket_id % 5 IN (2, 3) THEN 'in_progress'
               ELSE 'done'
             END
           WHERE status IS NULL OR status NOT IN ('todo', 'in_progress', 'done')"""
    )
    return {"updated": before["needs_update"], "total": before["total"]}
