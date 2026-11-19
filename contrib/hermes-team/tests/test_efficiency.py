from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from team_engine.efficiency import (_native_runs, compute_efficiency,
                                    update_efficiency_intervals, verification_progress)


class FakeBoard:
    def __init__(self, tasks, runs):
        self.tasks = tasks
        self.runs = runs

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def get_runs(self, task_id):
        return self.runs.get(task_id, [])


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class EfficiencyTests(unittest.TestCase):
    def test_worker_metrics_include_replaced_archived_cards_once(self):
        class Board:
            tasks = {
                "t_current": {"id": "t_current", "assignee": "team-backend",
                              "title": "[req-archive] Implement retry"},
                "t_old": {"id": "t_old", "assignee": "team-backend",
                          "title": "[req-archive] Implement retry (replaced)"},
                "t_other": {"id": "t_other", "assignee": "team-backend",
                            "title": "[req-other] Implement unrelated"},
            }
            runs = {
                "t_current": [{"id": 2, "profile": "team-backend", "status": "done",
                               "started_at": 200, "ended_at": 260}],
                "t_old": [{"id": 1, "profile": "team-backend", "status": "done",
                           "started_at": 100, "ended_at": 160}],
                "t_other": [{"id": 3, "profile": "team-backend", "status": "done",
                             "started_at": 100, "ended_at": 300}],
            }
            def get_task(self, task_id):
                return self.tasks.get(task_id)
            def list_tasks(self, *, limit, include_archived=False):
                assert limit == 200
                assert include_archived is True
                return list(self.tasks.values())
            def get_runs(self, task_id):
                return self.runs.get(task_id, [])

        metrics = _native_runs({"id": "req-archive", "items": [{"card": "t_current"}]}, Board(), now=300)

        self.assertEqual(metrics["attempts"]["total"], 2)
        self.assertEqual(metrics["wall_seconds_sum"], 120)
        self.assertEqual({row["card_id"] for row in metrics["intervals"]}, {"t_current", "t_old"})
        self.assertTrue(metrics["coverage"]["complete"])

    def test_worker_metrics_mark_board_scan_truncated_at_cap(self):
        rows = [{"id": f"t_{index}", "assignee": "team-backend",
                 "title": f"[req-many] task {index}"} for index in range(200)]
        class Board:
            def get_task(self, task_id):
                return None
            def list_tasks(self, *, limit, include_archived=False):
                self.assert_limit = limit
                self.assert_archived = include_archived
                return rows
            def get_runs(self, _task_id):
                return []

        metrics = _native_runs({"id": "req-many", "items": []}, Board(), now=300)

        self.assertEqual(metrics["coverage"]["board_scan_limit"], 200)
        self.assertTrue(metrics["coverage"]["truncated"])
        self.assertFalse(metrics["coverage"]["complete"])

    def test_verification_progress_does_not_call_partial_batch_feature_complete(self):
        state = {
            "stage": "verification",
            "items": [{"status": "done"}, {"status": "verification"}, {"status": "implementation"}],
            "pending_regression_items": [],
            "staging_sha": "abc123",
            "accepted_regression_sha": "abc123",
            "regression_batch": {"id": 1, "head_sha": "abc123", "status": "pass",
                                 "task_ids": ["T-001"]},
        }

        progress = verification_progress(state)

        self.assertEqual(progress["total_task_count"], 3)
        self.assertEqual(progress["completed_task_count"], 1)
        self.assertEqual(progress["regression_status"], "in_progress")
        self.assertFalse(progress["final_acceptance_ready"])

    def test_verification_progress_reports_pass_only_at_owner_final_stage(self):
        state = {
            "stage": "finalizing",
            "items": [{"status": "done"}, {"status": "done"}],
            "pending_regression_items": [],
            "staging_sha": "abc123",
            "accepted_regression_sha": "abc123",
            "regression_batch": {"id": 1, "head_sha": "abc123", "status": "pass",
                                 "task_ids": ["T-001", "T-002"]},
        }

        self.assertEqual(verification_progress(state)["regression_status"], "in_progress")
        state["stage"] = "awaiting_delivery"
        progress = verification_progress(state)
        self.assertEqual(progress["regression_status"], "passed")
        self.assertEqual(progress["completed_task_count"], 2)
        self.assertTrue(progress["final_acceptance_ready"])

    def test_wall_sum_and_union_exclude_controller_and_keep_unknowns_unknown(self):
        state = {"id": "req-1", "created_at": 100, "stage": "verification",
                 "root_card": "t_root", "items": [{"card": "t_dev", "reviewer_card": "t_rev",
                                                        "qa_card": "t_qa"}]}
        board = FakeBoard({"t_root": {"assignee": "team-controller"},
                           "t_dev": {"assignee": "team-backend"},
                           "t_rev": {"assignee": "team-reviewer"},
                           "t_qa": {"assignee": "team-qa"}},
                          {"t_root": [{"id": 1, "profile": "team-controller", "started_at": 100,
                                       "ended_at": 150}],
                           "t_dev": [{"id": 2, "profile": "team-backend", "started_at": 110,
                                      "ended_at": 150, "status": "done", "outcome": "completed"}],
                           "t_rev": [{"id": 3, "profile": "team-reviewer", "started_at": 130,
                                      "ended_at": 170, "status": "done"}],
                           "t_qa": [{"id": 4, "profile": "team-qa", "status": "done"}]})

        report = compute_efficiency(state, board, now=200)

        self.assertEqual(report["elapsed_seconds"], 100)
        self.assertEqual(report["workers"]["attempts"],
                         {"total": 3, "completed": 2, "active": 0, "missing_timestamps": 1})
        self.assertEqual(report["workers"]["wall_seconds_sum"], 80)
        self.assertEqual(report["workers"]["wall_seconds_union"], 60)
        self.assertIsNone(report["workers"]["model_active_seconds"])
        self.assertIsNone(report["workers"]["subscription_usage"])

    def test_wait_durations_accept_only_explicit_structured_intervals(self):
        state = {"id": "req-2", "created_at": "2026-10-28T00:00:00Z",
                 "efficiency_intervals": [{"kind": "owner_gate", "started_at": 100,
                                            "ended_at": 130, "reason": "approval"},
                                           {"kind": "quota_wait", "started_at": 125,
                                            "status": "open"}]}
        board = FakeBoard({}, {})
        events = [{"time": 110, "message": "Ожидание лимита подписки 999 секунд."},
                  {"efficiency_interval": {"kind": "dependency_wait", "started_at": 140,
                                            "ended_at": 160}}]

        report = compute_efficiency(state, board, now=150, events=events)

        self.assertEqual(report["waits"]["seconds_by_kind"],
                         {"dependency_wait": 20, "owner_gate": 30, "quota_wait": 25})
        self.assertEqual(report["waits"]["classified_seconds"], 75)
        self.assertEqual(report["waits"]["unclassified_records"], 0)

    def test_code_size_uses_git_range_and_separates_categories(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git(repo, "init", "-q")
            git(repo, "config", "user.email", "test@example.invalid")
            git(repo, "config", "user.name", "Test")
            (repo / "src").mkdir()
            (repo / "tests").mkdir()
            (repo / "docs").mkdir()
            (repo / "src/Feature.kt").write_text("old\n")
            (repo / "tests/FeatureTest.kt").write_text("old test\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "base")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "src/Feature.kt").write_text("one\ntwo\n")
            (repo / "tests/FeatureTest.kt").write_text("a\nb\nc\n")
            (repo / "docs/feature.md").write_text("readme\n")
            git(repo, "add", ".")
            git(repo, "commit", "-qm", "feature")
            head = git(repo, "rev-parse", "HEAD")
            state = {"id": "req-3", "created_at": 1, "initial_base_sha": base,
                     "current_sha": head, "stage": "verification", "items": []}

            report = compute_efficiency(state, FakeBoard({}, {}), repo=repo, now=2)
            code = report["code"]["accepted_or_integrated"]

            self.assertEqual(code["status"], "provisional")
            self.assertEqual((code["added"], code["deleted"], code["files"]), (6, 2, 3))
            self.assertEqual(code["by_category"]["production"]["added"], 2)
            self.assertEqual(code["by_category"]["tests"]["added"], 3)
            self.assertEqual(code["by_category"]["docs"]["added"], 1)
            self.assertIsNone(report["code"]["staging"]["added"])

    def test_missing_repository_and_revisions_do_not_invent_zero_code(self):
        report = compute_efficiency({"id": "req-4", "stage": "blocked"}, FakeBoard({}, {}), now=100)
        self.assertIsNone(report["elapsed_seconds"])
        self.assertEqual(report["code"]["accepted_or_integrated"]["status"], "unavailable")
        self.assertIsNone(report["code"]["accepted_or_integrated"]["added"])
        self.assertEqual(report["workers"]["attempts"]["total"], 0)
        self.assertEqual(report["waits"]["classified_seconds"], 0)

    def test_interval_tracker_opens_once_and_closes_on_observed_transition(self):
        state = {"stage": "awaiting_plan", "waiting_for_quota": []}
        update_efficiency_intervals(state, now=100)
        update_efficiency_intervals(state, now=110)
        self.assertEqual(len(state["efficiency_intervals"]), 1)
        self.assertEqual(state["efficiency_intervals"][0]["started_at"], 100)
        self.assertEqual(state["efficiency_intervals"][0]["status"], "open")

        state["stage"] = "implementation"
        state["waiting_for_quota"] = ["t_123"]
        update_efficiency_intervals(state, now=120)
        update_efficiency_intervals(state, now=125)
        self.assertEqual(len(state["efficiency_intervals"]), 2)
        self.assertEqual(state["efficiency_intervals"][0]["ended_at"], 120)
        self.assertEqual(state["efficiency_intervals"][1]["kind"], "quota_wait")
        self.assertEqual(state["efficiency_intervals"][1]["started_at"], 120)

        state["waiting_for_quota"] = []
        update_efficiency_intervals(state, now=140)
        self.assertEqual(state["efficiency_intervals"][1]["ended_at"], 140)

    def test_blocked_state_uses_explicit_environment_marker_or_generic_stop(self):
        state = {"stage": "blocked"}
        update_efficiency_intervals(state, now=50)
        self.assertEqual([row["kind"] for row in state["efficiency_intervals"]], ["technical_stop"])

        state["environment_wait_active"] = True
        update_efficiency_intervals(state, now=60)
        self.assertEqual([row["kind"] for row in state["efficiency_intervals"]],
                         ["technical_stop", "test_environment_wait"])
        self.assertEqual(state["efficiency_intervals"][0]["ended_at"], 60)
        self.assertIsNone(state["efficiency_intervals"][1]["ended_at"])

        report = compute_efficiency(state, FakeBoard({}, {}), now=80)
        self.assertEqual(report["waits"]["seconds_by_kind"],
                         {"technical_stop": 10, "test_environment_wait": 20})

    def test_separate_test_wait_is_accounted_as_its_own_interval(self):
        state = {"stage": "implementation", "waiting_for_tests": ["t_backend"],
                 "test_waits": {"t_backend": {"status": "waiting", "started_at": 100}}}
        update_efficiency_intervals(state, now=110)
        update_efficiency_intervals(state, now=120)
        self.assertEqual(len(state["efficiency_intervals"]), 1)
        self.assertEqual(state["efficiency_intervals"][0]["kind"], "test_wait")
        self.assertEqual(state["efficiency_intervals"][0]["started_at"], 110)
        state["waiting_for_tests"] = []
        update_efficiency_intervals(state, now=140)
        self.assertEqual(state["efficiency_intervals"][0]["ended_at"], 140)
        report = compute_efficiency(state, FakeBoard({}, {}), now=150)
        self.assertEqual(report["waits"]["seconds_by_kind"], {"test_wait": 30})
        self.assertIn("Worker wall time includes commands, waiting, tool overhead, and model activity.",
                      report["limitations"])


if __name__ == "__main__":
    unittest.main()
