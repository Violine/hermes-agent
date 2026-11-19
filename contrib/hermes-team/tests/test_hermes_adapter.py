from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.hermes_adapter import HermesBoard, HermesBoardError


HERMES_SOURCE = Path(
    os.environ.get(
        "HERMES_SOURCE_ROOT",
        str(Path.home() / ".hermes" / "hermes-agent"),
    )
).expanduser()


@unittest.skipUnless(
    (HERMES_SOURCE / "hermes_cli" / "kanban_db.py").is_file(),
    "Hermes source is not available; set HERMES_SOURCE_ROOT to its checkout",
)
class HermesAdapterIntegrationTests(unittest.TestCase):
    """Exercise adapter contracts against Hermes' real Kanban SQLite API."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="hermes-adapter-test-")
        self.root = Path(self.temp.name) / ".hermes"
        self.root.mkdir()
        self._old_kanban_home = os.environ.get("HERMES_KANBAN_HOME")
        self._old_source_root = os.environ.get("HERMES_SOURCE_ROOT")
        os.environ["HERMES_KANBAN_HOME"] = str(self.root)
        os.environ["HERMES_SOURCE_ROOT"] = str(HERMES_SOURCE)
        self.board = HermesBoard("adapter-test", self.root)
        self.board.ensure_board("Adapter test", "Hermes adapter integration tests")

    def tearDown(self) -> None:
        if self._old_kanban_home is None:
            os.environ.pop("HERMES_KANBAN_HOME", None)
        else:
            os.environ["HERMES_KANBAN_HOME"] = self._old_kanban_home
        if self._old_source_root is None:
            os.environ.pop("HERMES_SOURCE_ROOT", None)
        else:
            os.environ["HERMES_SOURCE_ROOT"] = self._old_source_root
        self.temp.cleanup()

    def test_controller_card_survives_controller_restart_with_same_run(self) -> None:
        card = self.board.machine_card(
            "controller run", idempotency_key="feature:root", max_runtime_seconds=None
        )
        run_id = card["current_run_id"]
        self.assertIsInstance(run_id, int)
        runs = self.board.get_runs(card["id"])
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["id"], run_id)
        self.assertEqual(runs[0]["profile"], HermesBoard.CONTROLLER_ASSIGNEE)
        self.assertEqual(runs[0]["status"], "running")

        restarted = HermesBoard("adapter-test", self.root)
        recovered = restarted.machine_card(
            "controller run", idempotency_key="feature:root", max_runtime_seconds=None
        )
        self.assertEqual(recovered["id"], card["id"])
        self.assertEqual(recovered["current_run_id"], run_id)
        self.assertTrue(restarted.heartbeat(card["id"], "after restart"))
        self.assertTrue(restarted.complete_external(card["id"], "controller complete"))
        self.assertEqual(restarted.get_task(card["id"])["status"], "done")
        final_run = restarted.get_runs(card["id"])[0]
        self.assertEqual(final_run["id"], run_id)
        self.assertEqual(final_run["outcome"], "completed")

    def test_controller_card_reclaims_expired_run_after_restart(self) -> None:
        card = self.board.machine_card(
            "controller run", idempotency_key="feature:root", max_runtime_seconds=None
        )
        old_run_id = card["current_run_id"]
        with self.board._connection() as conn:
            conn.execute(
                "UPDATE tasks SET claim_expires = 1 WHERE id = ?", (card["id"],)
            )
            conn.execute(
                "UPDATE task_runs SET claim_expires = 1 WHERE id = ?", (old_run_id,)
            )
            self.board._kb.release_stale_claims(conn, signal_fn=lambda *_args: None)

        self.assertEqual(self.board.get_task(card["id"])["status"], "ready")
        restarted = HermesBoard("adapter-test", self.root)
        recovered = restarted.machine_card(
            "controller run", idempotency_key="feature:root", max_runtime_seconds=None
        )
        self.assertEqual(recovered["id"], card["id"])
        self.assertNotEqual(recovered["current_run_id"], old_run_id)
        runs = restarted.get_runs(card["id"])
        self.assertEqual([run["id"] for run in runs], [old_run_id, recovered["current_run_id"]])
        self.assertEqual(runs[0]["outcome"], "reclaimed")
        self.assertEqual(runs[1]["profile"], HermesBoard.CONTROLLER_ASSIGNEE)
        self.assertEqual(runs[1]["status"], "running")

    def test_controller_root_block_activate_reclaim_heartbeat_and_complete(self) -> None:
        """Exercise the gate lifecycle using Hermes' native task/run rows."""
        key = "feature:approval-gate-lifecycle:new-run"
        card = self.board.machine_card(
            "controller approval gate",
            body="Deterministic integration test of a blocked root-card claim.",
            idempotency_key=key,
            max_runtime_seconds=None,
        )
        task_id = card["id"]
        first_run_id = card["current_run_id"]
        self.assertIsInstance(first_run_id, int)
        self.assertTrue(self.board.block_external(task_id, "Awaiting owner approval"))

        blocked = self.board.get_task(task_id)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIsNone(blocked["current_run_id"])
        blocked_runs = self.board.get_runs(task_id)
        self.assertEqual(len(blocked_runs), 1)
        self.assertEqual(blocked_runs[0]["id"], first_run_id)
        self.assertEqual(blocked_runs[0]["outcome"], "blocked")

        activated = self.board.activate_task(task_id)
        self.assertEqual(activated["status"], "ready")
        self.assertIsNone(activated["current_run_id"])

        # The same idempotent machine card must preserve its task identity but
        # acquire a fresh native run after the external gate is released.
        reclaimed = self.board.machine_card(
            "controller approval gate",
            body="Deterministic integration test of a blocked root-card claim.",
            idempotency_key=key,
            max_runtime_seconds=None,
        )
        second_run_id = reclaimed["current_run_id"]
        self.assertEqual(reclaimed["id"], task_id)
        self.assertIsInstance(second_run_id, int)
        self.assertNotEqual(second_run_id, first_run_id)
        self.assertEqual(reclaimed["status"], "running")

        self.assertTrue(self.board.heartbeat(task_id, "owner approved; controller resumed"))
        heartbeat_events = [event for event in self.board.list_events(task_id)
                            if event.get("kind") == "heartbeat"]
        self.assertTrue(heartbeat_events)
        self.assertEqual(heartbeat_events[-1].get("run_id"), second_run_id)

        self.assertTrue(self.board.complete_external(task_id, "Plan approved and gate closed"))
        completed = self.board.get_task(task_id)
        self.assertEqual(completed["status"], "done")
        self.assertIsNone(completed["current_run_id"])
        runs = self.board.get_runs(task_id)
        self.assertEqual([run["id"] for run in runs], [first_run_id, second_run_id])
        self.assertEqual([run["outcome"] for run in runs], ["blocked", "completed"])
        self.assertTrue(self.board.list_events(task_id))

    def test_controller_refuses_run_record_with_mismatched_profile(self) -> None:
        card = self.board.machine_card(
            "controller run", idempotency_key="feature:root", max_runtime_seconds=None
        )
        with self.board._connection() as conn:
            conn.execute(
                "UPDATE task_runs SET profile = 'team-qa' WHERE id = ?",
                (card["current_run_id"],),
            )
        with self.assertRaisesRegex(HermesBoardError, "run record does not match"):
            self.board.heartbeat(card["id"], "must be rejected")

    def test_parked_task_runtime_cap_can_be_refreshed_with_audited_event(self) -> None:
        task = self.board.create_task(
            "parked worker", "team-backend", idempotency_key="parked-runtime",
            max_runtime_seconds=690, initial_status="blocked",
        )

        self.board.set_waiting_task_runtime(task["id"], 1290)

        refreshed = self.board.get_task(task["id"])
        self.assertEqual(refreshed["max_runtime_seconds"], 1290)
        events = self.board.list_events(task["id"])
        event = next(row for row in events if row["kind"] == "runtime_limit_updated")
        self.assertEqual(event["payload"], {"max_runtime_seconds": 1290})
        self.assertEqual(self.board.activate_task(task["id"])["status"], "ready")

    def test_worker_run_id_profile_and_native_completion(self) -> None:
        card = self.board.create_task("QA task", "team-qa")
        with self.board._connection() as conn:
            claimed = self.board._kb.claim_task(
                conn, card["id"], claimer="dispatcher:test-worker"
            )
            self.assertIsNotNone(claimed)
            run_id = claimed.current_run_id
            self.assertIsInstance(run_id, int)
            run = self.board._kb.get_run(conn, run_id)
            self.assertEqual(run.profile, "team-qa")
            self.assertTrue(
                self.board._kb.complete_task(
                    conn,
                    card["id"],
                    result="QA report saved",
                    summary="QA report saved",
                    expected_run_id=run_id,
                )
            )

        self.assertEqual(self.board.get_task(card["id"])["status"], "done")
        completed = self.board.get_runs(card["id"])[0]
        self.assertEqual(completed["id"], run_id)
        self.assertEqual(completed["profile"], "team-qa")
        self.assertEqual(completed["outcome"], "completed")


if __name__ == "__main__":
    unittest.main()
