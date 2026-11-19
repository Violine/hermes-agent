from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from team_engine.controller import Controller
from team_engine.contracts import ContractError
from team_engine.test_waits import read_wait


CARD = "t_12345678"
JOB = "a" * 64
REQUEST = "req-" + "1" * 20


class Board:
    def __init__(self):
        self.tasks = {CARD: {"id": CARD, "status": "blocked", "assignee": "team-backend",
                            "block_kind": "transient", "current_run_id": None},
                      "root": {"status": "running"}}
        self.runs = {CARD: [{"id": 103, "profile": "team-backend", "status": "blocked",
                            "started_at": 1000, "ended_at": 2100}]}
        self.activations, self.comments = [], []

    def get_task(self, card):
        return self.tasks[card]

    def get_runs(self, card):
        return self.runs[card]

    def add_comment(self, *args):
        self.comments.append(args)

    def set_waiting_task_runtime(self, card, seconds):
        self.tasks[card]["max_runtime_seconds"] = seconds

    def activate_task(self, card):
        self.activations.append(card)
        self.tasks[card]["status"] = "ready"


class Quota:
    def __init__(self):
        self.released = []

    def release(self, key):
        self.released.append(key)


class TestWaitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "app.txt").write_text("code")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.sha = self.git("rev-parse", "HEAD")
        self.controller = Controller.__new__(Controller)
        self.controller.root = self.root / "data"
        self.controller.config = {"roles": {"team-backend": {"run_budget": 3600, "max_turns": 80}}}
        self.controller.board = Board()
        self.controller.quota = None
        self.controller.waiting_cards = set()
        self.saved = []
        self.controller.save = lambda state: self.saved.append(copy.deepcopy(state))
        self.directory = self.controller.root / "runs" / REQUEST / "prerequisites" / "card-context"
        self.directory.mkdir(parents=True)
        self.state = {"id": REQUEST, "root_card": "root", "stage": "implementation", "index": 0,
                      "items": [{"status": "implementation", "card": CARD, "workspace": str(self.repo),
                                 "definition": {"id": "T-005", "assignee_role": "backend"}}],
                      "prerequisite_paths": {CARD: str(self.directory)}}
        self.handoff = {"schema_version": 1, "card_id": CARD, "run_id": "103",
                        "assignee": "team-backend", "kind": "backend", "job_id": JOB, "sha": self.sha}
        (self.directory / "TEST-WAIT.json").write_text(json.dumps(self.handoff))
        self.job = {"job_id": JOB, "request": REQUEST, "task": "T-005", "sha": self.sha,
                    "status": "running", "submitted_at": time.time()}
        self.controller.test_job_snapshot = lambda *_args: copy.deepcopy(self.job)
        self.usage(103, 1100.4, 20)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def usage(self, run, seconds, iterations):
        directory = self.controller.root / "worker-budget" / CARD
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{run}.json").write_text(json.dumps({
            "schema_version": 1, "card_id": CARD, "run_id": str(run), "assignee": "team-backend",
            "finalized": True, "active_seconds": seconds, "iterations": iterations,
            "api_call_count": iterations,
        }))

    def test_wait_drops_quota_and_does_not_block_feature(self):
        self.controller.quota = Quota()
        self.state["dispatch"] = {CARD: {"key": "old-lease", "armed": True, "waiting": False}}
        self.controller.reconcile_test_waits(self.state)
        self.assertEqual(self.controller.status(CARD), "waiting_tests")
        self.assertEqual(self.state["stage"], "implementation")
        self.assertEqual(self.state["waiting_for_tests"], [CARD])
        self.assertEqual(self.controller.quota.released, ["old-lease"])
        self.assertFalse(self.state["dispatch"][CARD]["armed"])
        self.assertEqual(self.controller.board.activations, [])
        book = self.state["test_wait_budgets"][CARD]
        self.assertEqual((book["remaining_seconds"], book["remaining_turns"]), (2499, 60))

    def test_original_native_allowance_is_not_changed_by_later_role_edit(self):
        self.controller.config["roles"]["team-backend"].update(run_budget=7200, max_turns=120)
        path = self.controller.root / "worker-budget" / CARD / "103.json"
        usage = json.loads(path.read_text())
        usage.update(budget_seconds=3600, max_turns=80)
        path.write_text(json.dumps(usage))
        self.controller.reconcile_test_waits(self.state)
        book = self.state["test_wait_budgets"][CARD]
        self.assertEqual((book["remaining_seconds"], book["remaining_turns"]), (2499, 60))

    def test_terminal_failure_resumes_role_for_interpretation_without_passing_gate(self):
        self.controller.reconcile_test_waits(self.state)
        self.job.update(status="completed", exit_code=1, finished_at=time.time())
        self.controller.reconcile_test_waits(self.state)
        self.assertEqual(self.controller.board.activations, [CARD])
        self.assertEqual(self.state["items"][0]["status"], "implementation")
        self.assertEqual(self.controller.board.tasks[CARD]["max_runtime_seconds"], 2589)
        self.assertEqual(self.state["waiting_for_tests"], [])
        self.assertIn("exit_code=1", self.controller.board.comments[-1][2])
        self.assertEqual(self.state["test_wait_budgets"][CARD]["used_turns"], 20)

    def test_restart_is_idempotent_and_test_wall_time_does_not_reduce_active_allowance(self):
        self.controller.reconcile_test_waits(self.state)
        for _ in range(3):
            self.controller.reconcile_test_waits(self.state)
        self.assertEqual(len(self.state["test_wait_budgets"][CARD]["charged_runs"]), 1)
        self.assertEqual(self.state["test_wait_budgets"][CARD]["remaining_seconds"], 2499)
        self.assertEqual(len(self.controller.board.comments), 1)
        self.job.update(status="completed", exit_code=0)
        self.controller.reconcile_test_waits(self.state)
        self.controller.reconcile_test_waits(self.state)
        self.assertEqual(self.controller.board.activations, [CARD])

    def test_native_activation_crash_gap_replays_saved_intent_once(self):
        self.controller.reconcile_test_waits(self.state)
        entry = self.state["test_waits"][CARD + ":103"]
        entry["status"] = "resuming"
        self.job.update(status="completed", exit_code=0)
        self.controller.reconcile_test_waits(self.state)
        self.controller.reconcile_test_waits(self.state)
        self.assertEqual(self.controller.board.activations, [CARD])
        self.assertEqual(entry["status"], "resumed")

    def test_saved_resume_intent_cannot_activate_without_terminal_job(self):
        self.controller.reconcile_test_waits(self.state)
        self.state["test_waits"][CARD + ":103"]["status"] = "resuming"
        with self.assertRaisesRegex(ContractError, "no terminal test result"):
            self.controller.reconcile_test_waits(self.state)
        self.assertEqual(self.controller.board.activations, [])

    def test_successor_block_is_not_reactivated_by_old_resume_intent(self):
        self.controller.reconcile_test_waits(self.state)
        self.state["test_waits"][CARD + ":103"]["status"] = "resuming"
        self.controller.board.runs[CARD].append({"id": 104, "status": "blocked", "profile": "team-backend",
                                                "started_at": 3000, "ended_at": 3010})
        self.controller.board.tasks[CARD]["block_kind"] = "needs_input"
        self.controller.reconcile_test_waits(self.state)
        self.assertEqual(self.controller.board.activations, [])
        self.assertEqual(self.state["test_waits"][CARD + ":103"]["status"], "resumed")

    def test_changed_head_rejects_stale_test_result(self):
        self.controller.reconcile_test_waits(self.state)
        (self.repo / "app.txt").write_text("changed")
        self.git("commit", "-am", "changed")
        self.job.update(status="completed", exit_code=0)
        with self.assertRaisesRegex(ContractError, "HEAD changed"):
            self.controller.reconcile_test_waits(self.state)
        self.assertEqual(self.controller.board.activations, [])

    def test_foreign_job_is_rejected(self):
        self.job["request"] = "req-" + "2" * 20
        with self.assertRaisesRegex(ContractError, "identity"):
            self.controller.reconcile_test_waits(self.state)

    def test_missing_accounting_is_waited_for_then_explicitly_blocked(self):
        (self.controller.root / "worker-budget" / CARD / "103.json").unlink()
        self.controller.reconcile_test_waits(self.state)
        self.assertEqual(self.controller.status(CARD), "waiting_tests")
        self.state["test_waits"][CARD + ":103"]["registered_at"] -= 121
        with self.assertRaisesRegex(ContractError, "accounting did not finalize"):
            self.controller.reconcile_test_waits(self.state)

    def test_remaining_turns_not_reset_by_repeated_parks(self):
        self.controller.reconcile_test_waits(self.state)
        self.job.update(status="completed", exit_code=0)
        self.controller.reconcile_test_waits(self.state)
        self.handoff["run_id"] = "104"
        (self.directory / "TEST-WAIT.json").write_text(json.dumps(self.handoff))
        self.controller.board.tasks[CARD].update(status="blocked")
        self.controller.board.runs[CARD].append({"id": 104, "profile": "team-backend", "status": "blocked",
                                                "started_at": 4000, "ended_at": 4300})
        self.usage(104, 300, 60)
        with self.assertRaisesRegex(ContractError, "allowance is exhausted"):
            self.controller.reconcile_test_waits(self.state)
        self.assertEqual(self.state["test_wait_budgets"][CARD]["used_turns"], 80)
        self.assertEqual(self.controller.board.activations, [CARD])

    def test_real_owner_block_is_not_converted_to_test_wait(self):
        self.controller.board.tasks[CARD]["block_kind"] = "needs_input"
        self.controller.reconcile_test_waits(self.state)
        self.assertNotIn("test_waits", self.state)
        self.assertEqual(self.state["waiting_for_tests"], [])

    def test_owner_gate_or_unrelated_blocker_not_resumed(self):
        self.controller.reconcile_test_waits(self.state)
        self.state.update(stage="blocked", resume_stage="implementation", blocker="Owner decision required")
        self.job.update(status="completed", exit_code=0)
        self.controller.reconcile_test_waits(self.state)
        self.assertEqual(self.state["stage"], "blocked")
        self.assertEqual(self.controller.board.activations, [])

    def test_symlink_handoff_is_rejected(self):
        path = self.directory / "TEST-WAIT.json"
        other = self.directory / "other.json"
        path.rename(other)
        path.symlink_to(other)
        with self.assertRaises(OSError):
            read_wait(path)


if __name__ == "__main__":
    unittest.main()
