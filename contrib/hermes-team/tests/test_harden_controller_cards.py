from __future__ import annotations

import sys
import sqlite3
import tempfile
import unittest
import ast
import importlib
import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.harden_controller_cards import (
    PINNED_VERSION,
    _DB_MANAGED_STICKY_BLOCK,
    _hermes_version,
    apply_patch,
    patched_db_source,
    patched_source,
)


class ControllerCardDecompositionPatchTests(unittest.TestCase):
    def source(self) -> str:
        return '''def decompose_task(task_id):
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return DecomposeOutcome(task_id, False, "unknown task id")
    if task.status != "triage":
        return DecomposeOutcome(task_id, False, f"task is not in triage (status={task.status!r})")
    return DecomposeOutcome(task_id, True, "continued native decomposition")

def list_triage_ids(rows):
    return [row.id for row in rows]
'''

    def db_source(self) -> str:
        return '''def _has_sticky_block(conn, task_id):
    row = conn.execute(
        "SELECT kind FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return bool(row) and row["kind"] == "blocked"

def update_block_loop_status(conn, task_id):
    conn.execute(
        """
        UPDATE tasks
                   SET status        = 'triage',
                       block_kind    = 'capability'
                 WHERE id = ?
        """,
        (task_id,),
    )
'''

    def test_patch_is_valid_and_idempotent(self):
        updated = patched_source(self.source())
        self.assertIn('getattr(task, "created_by", None) == "hermes-team-controller"', updated)
        self.assertIn('getattr(row, "created_by", None) != "hermes-team-controller"', updated)
        self.assertLess(updated.index("skip controller-managed cards"), updated.index('task.status != "triage"'))
        self.assertEqual(patched_source(updated), updated)

    def test_runtime_guard_skips_only_controller_created_cards(self):
        namespace: dict[str, object] = {
            "DecomposeOutcome": lambda task_id, ok, message: SimpleNamespace(
                task_id=task_id, ok=ok, message=message
            )
        }
        tasks = {
            "root": SimpleNamespace(status="triage", created_by="hermes-team-controller"),
            "worker": SimpleNamespace(status="triage", created_by="hermes-team-controller"),
            "native": SimpleNamespace(status="triage", created_by="alex"),
            "not-triage": SimpleNamespace(status="blocked", created_by="alex"),
        }

        class BoardDB:
            def connect_closing(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get_task(self, _conn, task_id):
                return tasks.get(task_id)

        namespace["kb"] = BoardDB()
        exec(compile(patched_source(self.source()), "kanban_decompose.py", "exec"), namespace)
        decompose_task = namespace["decompose_task"]

        for card_id in ("root", "worker"):
            result = decompose_task(card_id)
            self.assertFalse(result.ok)
            self.assertIn("controller-managed", result.message)

        native = decompose_task("native")
        self.assertTrue(native.ok)
        self.assertEqual(native.message, "continued native decomposition")
        blocked = decompose_task("not-triage")
        self.assertFalse(blocked.ok)
        self.assertIn("not in triage", blocked.message)
        triage = namespace["list_triage_ids"]([
            SimpleNamespace(id="native", created_by="alex"),
            SimpleNamespace(id="managed", created_by="hermes-team-controller"),
        ])
        self.assertEqual(triage, ["native"])

    def test_block_loop_keeps_managed_card_blocked_and_leaves_native_triage_unchanged(self):
        namespace: dict[str, object] = {
            "get_task": lambda conn, task_id: (
                lambda row: SimpleNamespace(status=row[0], created_by=row[1]) if row else None
            )(conn.execute("SELECT status, created_by FROM tasks WHERE id=?", (task_id,)).fetchone()),
        }
        exec(compile(patched_db_source(self.db_source()), "kanban_db.py", "exec"), namespace)
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE tasks(id TEXT, created_by TEXT, assignee TEXT, status TEXT, block_kind TEXT)")
        db.execute("CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT)")
        db.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", [
            ("managed", "hermes-team-controller", "team-controller", "running", None),
            ("native", "alex", "default", "running", None),
        ])

        update = namespace["update_block_loop_status"]
        update(db, "managed")
        update(db, "native")
        rows = dict(db.execute("SELECT id, status FROM tasks"))

        self.assertEqual(rows, {"managed": "blocked", "native": "triage"})
        self.assertEqual(db.execute("SELECT assignee FROM tasks WHERE id='managed'").fetchone()[0],
                         "team-controller")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
        self.assertTrue(namespace["_has_sticky_block"](db, "managed"))
        self.assertFalse(namespace["_has_sticky_block"](db, "native"))

        # Emulate the native recompute_ready decision: a managed blocked card
        # remains sticky even though the block-loop event is not ``blocked``.
        db.execute("INSERT INTO task_events(task_id, kind) VALUES ('managed', 'block_loop_detected')")
        self.assertTrue(namespace["_has_sticky_block"](db, "managed"))

        # The decomposition guard still prevents managed triage cards from
        # being decomposed, while ordinary native cards retain that behavior.
        adapter = SimpleNamespace()
        adapter.connect_closing = lambda: nullcontext(adapter)
        adapter.get_task = lambda _conn, task_id: namespace["get_task"](db, task_id)
        decompose_ns: dict[str, object] = {
            "kb": adapter,
            "DecomposeOutcome": lambda task_id, ok, message: SimpleNamespace(
                task_id=task_id, ok=ok, message=message
            ),
        }
        exec(compile(patched_source(self.source()), "kanban_decompose.py", "exec"), decompose_ns)
        # Set the managed card to its patched block-loop status for the guard.
        db.execute("UPDATE tasks SET status='blocked' WHERE id='managed'")
        outcome = decompose_ns["decompose_task"]("managed")
        self.assertFalse(outcome.ok)
        self.assertIn("controller-managed", outcome.message)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
        db.close()

    def test_existing_status_guard_is_upgraded_with_sticky_recovery_guard(self):
        status_only = patched_db_source(self.db_source()).replace(
            _DB_MANAGED_STICKY_BLOCK, ""
        )
        self.assertIn("CASE WHEN created_by = 'hermes-team-controller'", status_only)
        self.assertNotIn("keep controller-managed blocked cards sticky", status_only)

        upgraded = patched_db_source(status_only)
        self.assertIn("keep controller-managed blocked cards sticky", upgraded)
        self.assertIn('getattr(managed_task, "status", None) == "blocked"', upgraded)
        self.assertEqual(patched_db_source(upgraded), upgraded)

        partial = upgraded.replace(
            'getattr(managed_task, "created_by", None) == "hermes-team-controller"',
            'managed_task.created_by == "hermes-team-controller"',
        )
        with self.assertRaisesRegex(ValueError, "Partial or duplicate"):
            patched_db_source(partial)
        incomplete = upgraded.replace(
            'getattr(managed_task, "status", None) == "blocked"',
            'getattr(managed_task, "status", None) == "ready"',
        )
        with self.assertRaisesRegex(ValueError, "Partial or duplicate"):
            patched_db_source(incomplete)

    def test_managed_block_stays_sticky_in_native_recompute_ready(self):
        source_root = Path(os.environ.get(
            "HERMES_SOURCE_ROOT", str(Path.home() / ".hermes" / "hermes-agent")
        )).expanduser()
        native_source = source_root / "hermes_cli" / "kanban_db.py"
        if not native_source.is_file():
            self.skipTest("Hermes source is unavailable; set HERMES_SOURCE_ROOT to its checkout")
        if _hermes_version(source_root) != PINNED_VERSION:
            self.skipTest(f"Native SQLite smoke targets Hermes {PINNED_VERSION}")

        source_root_string = str(source_root)
        sys.path.insert(0, source_root_string)
        try:
            kanban_db = importlib.import_module("hermes_cli.kanban_db")
        except ImportError as exc:
            sys.path.remove(source_root_string)
            self.skipTest(f"Hermes Kanban module is unavailable: {exc}")
        self.assertEqual(Path(kanban_db.__file__).resolve(), native_source.resolve())

        # Inject just the helper produced by our source patch into the real
        # Hermes module, then run its real SQLite create/block/unblock/
        # recompute_ready functions against an isolated temporary database.
        patched_tree = ast.parse(patched_db_source(native_source.read_text(encoding="utf-8")))
        patched_functions = [node for node in patched_tree.body
                             if isinstance(node, ast.FunctionDef)
                             and node.name in {"_has_sticky_block", "block_task"}]
        self.assertEqual({node.name for node in patched_functions},
                         {"_has_sticky_block", "block_task"})
        helper_module = ast.Module(body=patched_functions, type_ignores=[])
        previous_helper = kanban_db._has_sticky_block
        previous_block_task = kanban_db.block_task
        exec(compile(ast.fix_missing_locations(helper_module),
                     "patched-hermes-kanban-db.py", "exec"), kanban_db.__dict__)

        with tempfile.TemporaryDirectory(prefix="hermes-sticky-block-test-") as temp:
            conn = kanban_db.connect(db_path=Path(temp) / "kanban.db")
            try:
                managed_id = kanban_db.create_task(
                    conn, title="parked controller card", assignee="team-controller",
                    created_by="hermes-team-controller", initial_status="blocked",
                )
                native_id = kanban_db.create_task(
                    conn, title="ordinary blocked card", assignee="alex",
                    created_by="alex", initial_status="blocked",
                )
                self.assertEqual(kanban_db.get_task(conn, managed_id).status, "blocked")
                self.assertEqual(kanban_db.get_task(conn, native_id).status, "blocked")

                # Native create_task emits only a created event. The managed
                # parked/quota-wait card must not be made ready, while an ordinary
                # blocked card preserves Hermes' existing recovery behavior.
                self.assertEqual(kanban_db.recompute_ready(conn), 1)
                managed = kanban_db.get_task(conn, managed_id)
                native = kanban_db.get_task(conn, native_id)
                self.assertEqual(managed.status, "blocked")
                self.assertEqual(native.status, "ready")
                original_assignee = managed.assignee

                # Explicit unblock is the supported way out of the sticky state.
                self.assertTrue(kanban_db.unblock_task(conn, managed_id))
                self.assertEqual(kanban_db.get_task(conn, managed_id).status, "ready")

                # Repeating the same capability block reaches Hermes' native loop
                # breaker. The SQL guard keeps it blocked, and the sticky guard
                # prevents recompute_ready from re-queuing it despite the native
                # block_loop_detected event kind.
                for attempt in range(2):
                    self.assertTrue(kanban_db.block_task(
                        conn, managed_id, reason="owner decision required", kind="capability"
                    ))
                    task = kanban_db.get_task(conn, managed_id)
                    if task.status == "blocked" and attempt == 0:
                        self.assertTrue(kanban_db.unblock_task(conn, managed_id))
                managed = kanban_db.get_task(conn, managed_id)
                self.assertEqual(managed.status, "blocked")
                self.assertEqual(managed.assignee, original_assignee)
                events = kanban_db.list_events(conn, managed_id)
                self.assertTrue(any(event.kind == "block_loop_detected" for event in events))
                self.assertEqual(kanban_db.recompute_ready(conn), 0)
                self.assertEqual(kanban_db.get_task(conn, managed_id).status, "blocked")
                self.assertEqual(kanban_db.get_task(conn, managed_id).assignee, original_assignee)
            finally:
                conn.close()
                kanban_db._has_sticky_block = previous_helper
                kanban_db.block_task = previous_block_task
                sys.path.remove(source_root_string)

    def test_unknown_or_partial_source_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "decompose_task anchor"):
            patched_source(self.source().replace('if task is None:', 'if task.id is None:'))
        partial = patched_source(self.source()).replace(
            'getattr(task, "created_by", None)', 'task.created_by'
        )
        with self.assertRaisesRegex(ValueError, "Partial or duplicate"):
            patched_source(partial)
        with self.assertRaisesRegex(ValueError, "block-loop status anchor"):
            patched_db_source(self.db_source().replace("status        = 'triage'", "status = 'todo'"))

    def test_apply_patch_makes_backup_and_rechecks_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hermes_cli").mkdir()
            (root / "hermes_cli" / "__init__.py").write_text(
                f'__version__ = "{PINNED_VERSION}"\n', encoding="utf-8"
            )
            db_target = root / "hermes_cli" / "kanban_db.py"
            target = root / "hermes_cli" / "kanban_decompose.py"
            original = self.source()
            db_original = self.db_source()
            target.write_text(original, encoding="utf-8")
            db_target.write_text(db_original, encoding="utf-8")

            self.assertEqual(apply_patch(root), (db_target.resolve(), target.resolve()))
            backup = target.with_suffix(".py.hermes-team.bak")
            self.assertEqual(backup.read_text(encoding="utf-8"), original)
            patched = target.read_text(encoding="utf-8")
            self.assertEqual(patched_source(patched), patched)
            db_backup = db_target.with_suffix(".py.hermes-team.bak")
            self.assertEqual(db_backup.read_text(encoding="utf-8"), db_original)
            db_patched = db_target.read_text(encoding="utf-8")
            self.assertEqual(patched_db_source(db_patched), db_patched)

            # An already-patched checkout from the earlier SQL-only guard is
            # upgraded in place without replacing its existing backup.
            status_only = db_patched.replace(_DB_MANAGED_STICKY_BLOCK, "")
            db_target.write_text(status_only, encoding="utf-8")
            db_backup.write_text("preserve existing backup\n", encoding="utf-8")
            apply_patch(root)
            upgraded_db = db_target.read_text(encoding="utf-8")
            self.assertIn("keep controller-managed blocked cards sticky", upgraded_db)
            self.assertEqual(db_backup.read_text(encoding="utf-8"), "preserve existing backup\n")

            (root / "hermes_cli" / "__init__.py").write_text(
                '__version__ = "0.20.7"\n', encoding="utf-8"
            )
            target.write_text(original, encoding="utf-8")
            db_target.write_text(db_original, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Expected Hermes"):
                apply_patch(root)
            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertEqual(db_target.read_text(encoding="utf-8"), db_original)


if __name__ == "__main__":
    unittest.main()
