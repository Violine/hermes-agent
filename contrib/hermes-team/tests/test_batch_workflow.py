"""Real-Git controller fixture for quick gates plus one cumulative batch regression.

No model, network, deployment, or shared repository is used. The fixture board
supplies deterministic native-run identities; controller lifecycle and Git
integration remain real.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.controller import Controller
from tests.test_full_workflow import FixtureBoard, git


class BatchWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Batch Workflow")
        git(self.repo, "config", "user.email", "batch@example.invalid")
        (self.repo / "README.md").write_text("base\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "base")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")
        self.full_runs = self.root / "full-runs.txt"
        checks = [
            [sys.executable, "-c", "from pathlib import Path; compile(Path('src/a.py').read_text(), 'src/a.py', 'exec')"],
            [sys.executable, "-c", "from pathlib import Path; compile(Path('src/b.py').read_text(), 'src/b.py', 'exec')"],
            [sys.executable, "-c",
             "from pathlib import Path; assert Path('src/a.py').is_file() and Path('src/b.py').is_file(); "
             f"Path({str(self.full_runs)!r}).open('a').write('full\\n')"],
        ]
        self.config_path = self.root / "config.json"
        roles = ("analyst", "planner", "skeptic", "test-designer", "judge",
                 "backend", "reviewer", "qa", "integrator")
        config = {
            "data_root": str(self.root / "data"),
            "bundle_root": str(Path(__file__).resolve().parents[1]),
            "board": "fixture-board",
            "hermes_root": str(self.root / "hermes"),
            "max_parallel_tasks": 2,
            "max_plan_tasks": 4,
            "human_approvals": True,
            "projects": {"demo": {
                "repo": str(self.repo), "base_ref": "main", "checks": checks,
                "check_timeout": 10,
                "execution_policy": {
                    "version": 1, "full_after_tasks": 2,
                    "path_rules": [
                        {"name": "feature-a", "prefixes": ["src/a.py"],
                         "quick_check_indexes": [0]},
                        {"name": "feature-b", "prefixes": ["src/b.py"],
                         "quick_check_indexes": [1]},
                    ],
                },
            }},
            "roles": {"team-" + role: {
                "model": "fixture-model", "provider": "fixture",
                "reasoning_effort": "low", "run_budget": 60,
            } for role in roles},
        }
        self.config_path.write_text(json.dumps(config))
        self.board = FixtureBoard()
        self.board_patch = patch("team_engine.controller.HermesBoard", return_value=self.board)
        self.board_patch.start()
        self.controller = Controller(self.config_path)

    def tearDown(self):
        self.board_patch.stop()
        self.temp.cleanup()

    def _complete_candidate_review_and_integrator(self, state, item, filename, source,
                                                 *, commit_candidate=True):
        workspace = Path(item["workspace"])
        if commit_candidate:
            (workspace / "src").mkdir(exist_ok=True)
            (workspace / "src" / filename).write_text(source)
            git(workspace, "add", f"src/{filename}")
            git(workspace, "commit", "-m", f"Implement {filename}")
        developer_run = self.board.finish_fixture_run(item["card"])
        output = self.controller.round_path(state, item)
        (output / "IMPLEMENTATION.md").write_text(
            "# Fixture implementation\n\n"
            f"Native run: {developer_run}\nBase: {item['base_sha']}\n"
            f"Head: {git(workspace, 'rev-parse', 'HEAD')}\n"
            f"Implemented {filename}; targeted syntax check passed.\n"
        )
        self.controller.start_review(state, item)
        review_runs = {}
        for role, report_name in (("reviewer", "REVIEW.json"), ("qa", "QA.json")):
            run_id = self.board.finish_fixture_run(item[role + "_card"])
            review_runs[role] = run_id
            report = {
                "schema_version": 1, "task_id": item["card"], "role": role,
                "assignee": "team-" + role, "run_id": run_id, "verdict": "pass",
                "base_sha": item["base_sha"], "head_sha": item["head_sha"],
                "findings": [], "checks": [{"name": "fixture review", "status": "pass",
                                               "evidence": "Inspected the exact candidate SHA."}],
            }
            if role == "qa":
                plan = item["verification_plan"]
                report["checks"] = [{"name": row["name"], "status": "pass",
                                     "command": row["argv"], "exit_code": 0,
                                     "evidence": "Selected fixture command passed."}
                                    for row in plan["selected_checks"]]
                if plan["deferred_checks"]:
                    report["deferred"] = {"evidence_ref": "VERIFICATION-PLAN.json",
                                           "policy_sha256": plan["policy_sha256"],
                                           "checks": plan["deferred_checks"]}
            Path(item[role + "_report_path"]).write_text(json.dumps(report))
        self.controller.start_integrator(state, item)
        integrator_run = self.board.finish_fixture_run(item["integrator_card"])
        (self.controller.round_path(state, item) / "INTEGRATION.json").write_text(json.dumps({
            "feature_id": state["id"], "assignee": "team-integrator",
            "run_id": integrator_run, "base_sha": item["base_sha"],
            "head_sha": item["head_sha"], "decision": "ready_for_owner",
            "gates": [{"name": "review and QA", "status": "pass"}],
            "blocking_items": [],
        }))
        return {"developer": developer_run, **review_runs, "integrator": integrator_run}

    def test_two_independent_candidates_rebase_refresh_evidence_and_batch_once(self):
        state = self.controller.submit(
            "demo", "Implement two independent features.\n", "Batch fixture",
            "batch-workflow", force_queue=True, require_approvals=True)
        root_card = self.board.machine_card("[batch-workflow] Batch fixture")
        state["root_card"] = root_card["id"]
        (self.controller.path(state) / "artifacts" / "SPEC.md").write_text(
            "# Specification\n\nTwo independent features must coexist in the cumulative result.\n")
        (self.controller.path(state) / "artifacts" / "DESIGN.md").write_text(
            "# Design\n\n" +
            "The two prerequisite-free tasks own separate modules and can be developed from the same base. " * 8)
        tasks = [
            {"id": "T-001", "title": "Implement feature A", "problem": "Feature A is missing.",
             "requirements": ["REQ-A"], "solution": "Add src/a.py with a valid module.",
             "acceptance_criteria": [{"id": "AC-A", "given": "feature A is loaded",
                "when": "the module is compiled", "then": "syntax is valid", "evidence": "quick check 0"}],
             "test_plan": [{"name": "feature A syntax", "type": "unit", "scope": "src/a.py",
                "reason": "validates the independent module"}], "dependencies": [], "priority": "P1",
             "priority_reason": "Independent prerequisite-free feature.", "owned_paths": ["src/a.py"],
             "assignee_role": "backend"},
            {"id": "T-002", "title": "Implement feature B", "problem": "Feature B is missing.",
             "requirements": ["REQ-B"], "solution": "Add src/b.py with a valid module.",
             "acceptance_criteria": [{"id": "AC-B", "given": "feature B is loaded",
                "when": "the module is compiled", "then": "syntax is valid", "evidence": "quick check 1"}],
             "test_plan": [{"name": "feature B syntax", "type": "unit", "scope": "src/b.py",
                "reason": "validates the independent module"}], "dependencies": [], "priority": "P1",
             "priority_reason": "Independent prerequisite-free feature.", "owned_paths": ["src/b.py"],
             "assignee_role": "backend"},
        ]
        (self.controller.path(state) / "artifacts" / "PLAN.json").write_text(json.dumps({
            "schema_version": 1, "project": "demo", "feature_id": state["id"],
            "base_sha": self.base_sha, "status": "ready", "open_questions": [], "tasks": tasks,
            "requirement_coverage": [
                {"requirement_id": "REQ-A", "task_ids": ["T-001"], "verification": "AC-A"},
                {"requirement_id": "REQ-B", "task_ids": ["T-002"], "verification": "AC-B"},
            ],
        }))
        state["planner_card"] = self.board.create_task(
            "Fixture planner", "team-planner", body="plan fixture", idempotency_key="planner-fixture")["id"]
        self.board.finish_fixture_run(state["planner_card"])
        planning_workspace = self.controller.path(state) / "worktrees" / "planning"
        planning_workspace.parent.mkdir(parents=True, exist_ok=True)
        git(self.repo, "worktree", "add", "--detach", str(planning_workspace), self.base_sha)
        self.controller.accept_plan(state)
        first, second = state["items"]
        self.assertEqual(git(first["workspace"], "rev-parse", "HEAD"), self.base_sha)
        self.assertEqual(git(second["workspace"], "rev-parse", "HEAD"), self.base_sha)
        self.assertNotEqual(first["workspace"], second["workspace"])
        self.assertEqual(first["base_sha"], second["base_sha"])
        self.assertTrue(first["workspace"] != second["workspace"])

        self._complete_candidate_review_and_integrator(state, first, "a.py", "value_a = 1\n")
        self._complete_candidate_review_and_integrator(state, second, "b.py", "value_b = 2\n")
        first_old_base = first["base_sha"]
        first_old_head = first["head_sha"]
        second_old_head = second["head_sha"]
        second_old_reviewer_run = json.loads(Path(second["reviewer_report_path"]).read_text())["run_id"]
        self.controller.integrate(state, first)
        first_integrated_sha = state["current_sha"]
        self.assertNotEqual(first_integrated_sha, self.base_sha)
        self.assertEqual(first["status"], "provisional")
        self.assertEqual(first["integration"]["verification_plan"]["mode"], "quick")
        self.assertEqual(len(state.get("regression_batches", [])), 0)
        self.assertFalse(self.full_runs.exists(), "full regression must wait for both candidates")
        self.assertEqual(second["base_sha"], first_old_base)

        # The second independent branch was also authored from the original base.
        # Its old review package is deliberately presented to the controller; it
        # must reject that stale evidence and send the task through a fresh cycle.
        old_developer_card = second["card"]
        old_review_path = second["reviewer_report_path"]
        self.controller.integrate(state, second)
        self.assertEqual(second["base_sha"], first_integrated_sha)
        self.assertEqual(second["status"], "implementation")
        self.assertNotEqual(second["card"], old_developer_card)
        self.assertIn(old_developer_card, second["past_cards"])
        self.assertEqual(second["round"], 2)
        self.assertEqual(git(second["workspace"], "rev-parse", "HEAD"), second_old_head)
        git(second["workspace"], "rebase", "--onto", first_integrated_sha, self.base_sha)
        rebased_candidate = git(second["workspace"], "rev-parse", "HEAD")
        self.assertNotEqual(rebased_candidate, second_old_head)
        self.assertEqual(git(second["workspace"], "merge-base", "HEAD", first_integrated_sha),
                         first_integrated_sha)

        fresh_runs = self._complete_candidate_review_and_integrator(
            state, second, "b.py", "", commit_candidate=False)
        self.assertEqual(second["base_sha"], first_integrated_sha)
        self.assertNotEqual(second["head_sha"], first_old_head)
        fresh_review = json.loads(Path(second["reviewer_report_path"]).read_text())
        fresh_qa = json.loads(Path(second["qa_report_path"]).read_text())
        self.assertEqual(fresh_review["base_sha"], first_integrated_sha)
        self.assertEqual(fresh_qa["base_sha"], first_integrated_sha)
        self.assertEqual(fresh_review["head_sha"], rebased_candidate)
        self.assertNotEqual(fresh_review["run_id"], fresh_qa["run_id"])
        self.assertNotEqual(fresh_review["run_id"], second_old_reviewer_run)
        self.assertTrue(Path(old_review_path).is_file(), "the stale report should remain archived in round one")

        self.controller.integrate(state, second)
        self.assertEqual(second["status"], "done")
        self.assertEqual(first["status"], "done")
        self.assertEqual(len(state["regression_batches"]), 1)
        batch = state["regression_batches"][0]
        self.assertEqual(batch["status"], "pass")
        self.assertEqual(batch["task_ids"], ["T-001", "T-002"])
        self.assertEqual(batch["head_sha"], state["accepted_regression_sha"])
        self.assertEqual(state["accepted_regression_sha"], state["current_sha"])
        self.assertEqual(len(self.full_runs.read_text().splitlines()), 1)
        self.assertEqual([entry["index"] for entry in first["integration"]["verification_plan"]["selected_checks"]], [0])
        self.assertEqual([entry["index"] for entry in second["integration"]["verification_plan"]["selected_checks"]], [1])
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.base_sha)
        self.assertEqual(state["stage"], "awaiting_delivery")
        self.assertEqual(state["approval"]["kind"], "delivery")
        self.assertEqual(self.board.completed_external_calls, 0)
        self.assertNotEqual(second_old_head, fresh_runs["developer"])


if __name__ == "__main__":
    unittest.main()
