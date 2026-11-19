"""End-to-end controller workflow using explicit deterministic agent fixtures.

This test exercises real temporary Git repositories, branches, worktrees, and
the actual integration/check gates. Every would-be model response is a fixture;
this is not a live-LLM or Hermes-dispatch test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.controller import Controller
from team_engine.human_workflow import submit_request, get_request, decide_request
from team_engine.hermes_adapter import HermesBoard
from tests.test_hermes_adapter import HERMES_SOURCE


class FixtureBoard:
    """In-memory Hermes board fixture with native-run-shaped identity rows."""

    def __init__(self, *_args, **_kwargs):
        self.tasks: dict[str, dict] = {}
        self.runs: dict[str, list[dict]] = {}
        self.comments: list[tuple[str, str, str]] = []
        self.counter = 0
        self.run_counter = 1000
        self.completed_external_calls = 0

    def ensure_board(self, *_args, **_kwargs):
        return {"slug": "fixture-board"}

    def _create(self, title, assignee, *, parents=(), status="running", **kwargs):
        self.counter += 1
        task_id = f"t_{self.counter:08x}"
        self.run_counter += 1
        run = {"id": self.run_counter, "profile": assignee,
               "status": "running", "outcome": None}
        self.tasks[task_id] = {"id": task_id, "title": title,
                               "assignee": assignee, "parents": list(parents),
                               "status": status, "blocked_reason": None, **kwargs}
        self.runs[task_id] = [run]
        return self.tasks[task_id]

    def machine_card(self, title, body="", idempotency_key=None, **kwargs):
        for task in self.tasks.values():
            if task.get("idempotency_key") == idempotency_key:
                return task
        return self._create(title, "team-controller", status="running", body=body,
                            idempotency_key=idempotency_key, **kwargs)

    def create_task(self, title, assignee, body=None, parents=(), priority=0,
                    workspace_path=None, model=None, provider=None,
                    reasoning_effort=None, idempotency_key=None,
                    max_runtime_seconds=900, initial_status="running", **kwargs):
        for task in self.tasks.values():
            if task.get("idempotency_key") == idempotency_key:
                return task
        return self._create(
            title, assignee, parents=parents, status=initial_status, body=body,
            priority=priority, workspace_path=workspace_path, model=model,
            provider=provider, reasoning_effort=reasoning_effort,
            idempotency_key=idempotency_key,
            max_runtime_seconds=max_runtime_seconds, **kwargs,
        )

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def get_runs(self, task_id):
        return self.runs.get(task_id, [])

    def activate_task(self, task_id):
        task = self.tasks[task_id]
        if task["status"] != "blocked":
            raise AssertionError("Fixture activation requires a blocked card")
        task["status"] = (
            "ready" if all(self.tasks[parent]["status"] == "done"
                            for parent in task["parents"]) else "todo"
        )
        return task

    def set_waiting_task_runtime(self, task_id, max_runtime_seconds):
        task = self.tasks[task_id]
        if task["status"] != "blocked":
            raise AssertionError("Fixture runtime update requires a parked card")
        task["max_runtime_seconds"] = max_runtime_seconds

    def add_comment(self, task_id, author, message):
        self.comments.append((task_id, author, message))

    def heartbeat(self, task_id):
        return self.tasks[task_id]

    def block_external(self, task_id, reason):
        self.tasks[task_id]["status"] = "blocked"
        self.tasks[task_id]["blocked_reason"] = reason

    def complete_external(self, task_id, message, payload=None):
        self.completed_external_calls += 1
        self.tasks[task_id]["status"] = "done"
        self.tasks[task_id]["completion"] = {"message": message, "payload": payload}

    def finish_fixture_run(self, task_id):
        task = self.tasks[task_id]
        task["status"] = "done"
        run = self.runs[task_id][-1]
        run.update({"status": "completed", "outcome": "completed"})
        return str(run["id"])


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


class FullWorkflowFixtureIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Fixture Test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "README.md").write_text("base\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "base")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")

        self.roles = (
            "analyst", "planner", "skeptic", "test-designer", "judge",
            "backend", "reviewer", "qa", "integrator",
        )
        check = [sys.executable, "-c",
                 "from pathlib import Path; assert Path('src/feature.py').read_text() == 'value = 42\\n'"]
        config = {
            "data_root": str(self.root / "data"),
            "bundle_root": str(self.root / "bundle"),
            "board": "fixture-board",
            "hermes_root": str(self.root / "hermes"),
            "projects": {"demo": {"repo": str(self.repo), "base_ref": "main",
                                    "checks": [check]}},
            "roles": {
                "team-" + role: {"model": "fixture-model", "provider": "fixture",
                                 "reasoning_effort": "low" if role == "backend" else "high",
                                 "run_budget": 60}
                for role in self.roles
            },
            "max_plan_tasks": 4,
        }
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(config))
        self.board = FixtureBoard()
        self.board_patch = patch("team_engine.controller.HermesBoard", return_value=self.board)
        self.board_patch.start()
        self.controller = Controller(self.config_path)

    def tearDown(self):
        self.board_patch.stop()
        self.temp.cleanup()

    def test_controller_fixture_responses_reach_delivery_through_real_git_gates(self):
        self._run_workflow(gated=False)

    def test_owner_gates_stop_all_three_stages_through_real_git_checks(self):
        self._run_workflow(gated=True)

    def test_auto_start_after_judge_keeps_requirements_and_delivery_gates(self):
        config = json.loads(self.config_path.read_text())
        config["auto_start_after_judge_accept"] = True
        self.config_path.write_text(json.dumps(config))
        self.controller.config["auto_start_after_judge_accept"] = True
        self._run_workflow(gated=True, auto_plan=True)

    def _approve_gate(self, state, kind, next_stage):
        """Prove repeated controller ticks cannot substitute for an owner action."""
        before_count = len(self.board.tasks)
        pending = get_request(self.config_path, state["id"])
        self.assertEqual(pending["approval"]["kind"], kind)
        if kind == "plan":
            developer = next(row for row in pending["role_plan"] if row["role"] == "team-backend")
            self.assertEqual(developer["model"], "fixture-model")
            self.assertEqual(developer["assignment_state"], "proposed")
            self.assertEqual(developer["plan_task_id"], "T-1")
        self.assertEqual(pending["stage"], "awaiting_" + kind)
        self.controller.tick(state)
        self.controller.tick_all()
        self.assertEqual(len(self.board.tasks), before_count)
        self.assertEqual(get_request(self.config_path, state["id"])["stage"],
                         "awaiting_" + kind)
        result = decide_request(
            self.config_path, state["id"], gate_id=pending["approval"]["id"],
            decision="approve", actor="fixture-owner", channel="test",
            request_id="approve-" + kind)
        self.assertEqual(result["stage"], "done" if kind == "delivery" else "queued")
        self.controller.tick_all()
        updated = json.loads((self.controller.path(state) / "state.json").read_text())
        state.clear()
        state.update(updated)
        self.assertEqual(state["stage"], next_stage, state.get("blocker"))

    def _run_workflow(self, *, gated, auto_plan=False):
        # Explicit fixture outputs stand in for each model response. The
        # controller, contract/evidence gates, Git worktrees, merge and trusted
        # regression command are the real implementation.
        if gated:
            request = submit_request(self.config_path, project="demo",
                brief="Add a tiny deterministic feature.\n", title="Fixture feature",
                request_id="gated-fixture", actor="fixture-owner", channel="test")
            run_dir = self.root / "data/runs" / request["id"]
            state = json.loads((run_dir / "state.json").read_text())
        else:
            state = self.controller.submit("demo", "Add a tiny deterministic feature.\n",
                                           "Fixture feature", "fixture-feature")
            run_dir = self.root / "data/runs/fixture-feature"
        artifacts = run_dir / "artifacts"

        # Analyst fixture -> planner fixture.
        (artifacts / "SPEC.md").write_text(
            "# Specification\n\n" + "The feature returns the fixed value 42. " * 15
        )
        analyst_run = self.board.finish_fixture_run(state["analysis_card"])
        self.controller.tick(state)
        if gated:
            self.assertNotIn("planner_card", state)
            self._approve_gate(state, "requirements", "planning")
        self.assertEqual(state["stage"], "planning")
        planner_card = state["planner_card"]

        # Planner fixture: one atomic task, explicit AC, owned path and test.
        design = "# Design\n\n" + "A pure module exposes one stable value. " * 12
        (artifacts / "DESIGN.md").write_text(design)
        task = {
            "id": "T-1", "title": "Add the feature value",
            "problem": "The requested value is not currently available.",
            "requirements": ["REQ-1"],
            "solution": "Add a small Python module exporting the requested value.",
            "acceptance_criteria": [{"id": "AC-1", "given": "the module is imported",
                                     "when": "the feature value is read",
                                     "then": "the value equals 42",
                                     "evidence": "assert src.feature.value == 42"}],
            "test_plan": [{"name": "fixed value", "type": "unit",
                           "scope": "src/feature.py", "reason": "proves AC-1"}],
            "dependencies": [], "priority": "P1",
            "priority_reason": "This is the sole user-visible acceptance criterion.",
            "owned_paths": ["src/feature.py"], "assignee_role": "backend",
        }
        (artifacts / "PLAN.json").write_text(json.dumps({
            "schema_version": 1, "project": "demo", "feature_id": state["id"],
            "base_sha": self.base_sha, "status": "ready", "open_questions": [],
            "tasks": [task],
            "requirement_coverage": [{"requirement_id": "REQ-1", "task_ids": ["T-1"],
                                      "verification": "AC-1 and trusted regression check"}],
        }))
        planner_run = self.board.finish_fixture_run(planner_card)
        self.controller.tick(state)
        self.assertEqual(state["stage"], "plan_challenge")
        self.assertEqual(self.board.get_task(state["skeptic_card"])["parents"], [planner_card])

        # Independent skeptic and test-designer fixture reports bind to the
        # exact snapshotted plan hash and to distinct successful fixture runs.
        review_dir = Path(state["plan_review_path"])
        plan_hash = state["plan_sha256"]
        plan_runs = {}
        for role in ("skeptic", "test-designer"):
            card_id = state[role + "_card"]
            plan_runs[role] = self.board.finish_fixture_run(card_id)
            report = {"schema_version": 1, "feature_id": state["id"],
                      "plan_sha256": plan_hash, "role": role,
                      "assignee": "team-" + role, "run_id": plan_runs[role],
                      "verdict": "pass", "findings": []}
            (review_dir / (role + ".json")).write_text(json.dumps(report))
        self.controller.tick(state)
        self.assertEqual(state["stage"], "plan_judgment")

        # Judge fixture accepts the precise reviewed package.
        judge_run = self.board.finish_fixture_run(state["judge_card"])
        (review_dir / "DECISION.json").write_text(json.dumps({
            "schema_version": 1, "feature_id": state["id"],
            "plan_sha256": plan_hash, "assignee": "team-judge",
            "run_id": judge_run, "decision": "accept", "resolutions": [],
            "summary": "The reviewers found no material issue and the plan covers the requirement.",
        }))
        self.controller.tick(state)
        if gated:
            if auto_plan:
                self.assertEqual(state["stage"], "queued")
                self.assertFalse(state.get("items"))
                self.assertEqual(state["queue_reason"], "plan_approved")
                # The queued scheduler path enforces the reviewed plan and
                # approved SPEC hashes before admitting implementation.
                self.controller.tick_all()
                state.clear()
                state.update(json.loads((run_dir / "state.json").read_text()))
                self.assertEqual(state["stage"], "implementation", state.get("blocker"))
            else:
                self.assertFalse(state.get("items"))
                self._approve_gate(state, "plan", "implementation")
        self.assertEqual(state["stage"], "implementation")
        item = state["items"][0]
        self.assertEqual(item["definition"]["assignee_role"], "backend")
        self.assertEqual(self.board.get_task(item["card"])["status"], "ready")
        self.assertEqual(git(Path(item["workspace"]), "rev-parse", "HEAD"), self.base_sha)

        # Developer fixture creates a real commit in the controller-created
        # feature worktree. The fixture report records the actual run and SHA.
        candidate_worktree = Path(item["workspace"])
        (candidate_worktree / "src").mkdir()
        (candidate_worktree / "src/feature.py").write_text("value = 42\n")
        git(candidate_worktree, "add", "src/feature.py")
        git(candidate_worktree, "commit", "-m", "Add deterministic feature value")
        candidate_sha = git(candidate_worktree, "rev-parse", "HEAD")
        developer_run = self.board.finish_fixture_run(item["card"])
        output_dir = self.controller.round_path(state, item)
        (output_dir / "IMPLEMENTATION.md").write_text(
            "# Implementation fixture\n\n"
            f"assignee=team-backend\nrun_id={developer_run}\n"
            f"base_sha={item['base_sha']}\nhead_sha={candidate_sha}\n"
            "Committed src/feature.py. The isolated project regression checks this exact value.\n"
        )
        self.controller.tick(state)
        self.assertEqual(state["stage"], "verification")
        self.assertEqual(item["head_sha"], candidate_sha)

        # Reviewer and QA fixture artifacts are bound to the developer's exact
        # base/head, while their actual fixture-board runs prove independent
        # successful role identity to the controller.
        evidence_runs = {}
        for role, filename in (("reviewer", "REVIEW.json"), ("qa", "QA.json")):
            card_id = item[role + "_card"]
            evidence_runs[role] = self.board.finish_fixture_run(card_id)
            evidence = {
                "schema_version": 1, "task_id": item["card"], "role": role,
                "assignee": "team-" + role, "run_id": evidence_runs[role],
                "verdict": "pass", "base_sha": item["base_sha"],
                "head_sha": item["head_sha"], "findings": [],
                "checks": [{"name": "fixture acceptance", "status": "pass",
                            "command": [sys.executable, "-c", "assert True"],
                            "exit_code": 0}],
            }
            if role == "qa":
                evidence["checks"] = [{
                    "name": row["name"], "status": "pass", "command": row["argv"],
                    "exit_code": 0, "evidence": "Selected fixture command passed."}
                    for row in item["verification_plan"]["selected_checks"]]
            (output_dir / filename).write_text(json.dumps(evidence))
        self.controller.tick(state)
        self.assertEqual(state["stage"], "integration_review")
        self.assertNotEqual(evidence_runs["reviewer"], evidence_runs["qa"])

        # Integrator fixture approves that exact candidate. Controller then
        # performs the real isolated merge and runs the trusted check above.
        integrator_run = self.board.finish_fixture_run(item["integrator_card"])
        (output_dir / "INTEGRATION.json").write_text(json.dumps({
            "feature_id": state["id"], "assignee": "team-integrator",
            "run_id": integrator_run, "base_sha": item["base_sha"],
            "head_sha": item["head_sha"], "decision": "ready_for_owner",
            "gates": [{"name": "review and QA evidence", "status": "pass"},
                      {"name": "scope and candidate snapshot", "status": "pass"}],
            "blocking_items": [],
        }))
        self.controller.tick(state)

        self.assertEqual(state["integration_check"]["event"], "completed")
        self.assertEqual(state["integration_check"]["result"], "pass")
        progress_events = [json.loads(line)["message"] for line in
                           (run_dir / "events.jsonl").read_text().splitlines()
                           if "обязательная проверка" in json.loads(line)["message"]]
        self.assertEqual(len(progress_events), 2)

        if gated:
            self.assertEqual(self.board.completed_external_calls, 0)
            self._approve_gate(state, "delivery", "done")
            if auto_plan:
                self.assertEqual([row["kind"] for row in state["approval_audit"]],
                                 ["requirements", "delivery"])
            else:
                self.assertEqual([row["kind"] for row in state["approval_audit"]],
                                 ["requirements", "plan", "delivery"])

        # Delivery is complete, the configured base did not move, and the
        # integration branch contains the tested developer candidate.
        self.assertEqual(state["stage"], "done")
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.base_sha)
        delivery = json.loads((artifacts / "DELIVERY.json").read_text())
        self.assertEqual(delivery["status"], "ready_for_owner")
        self.assertFalse(delivery["protected_branch_modified"])
        self.assertEqual(delivery["base_sha"], self.base_sha)
        self.assertEqual(delivery["head_sha"], state["current_sha"])
        integration_ref = delivery["candidate_ref"]
        integration_sha = git(self.repo, "rev-parse", integration_ref)
        self.assertEqual(integration_sha, delivery["head_sha"])
        ancestor = subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor",
                                   candidate_sha, integration_sha], capture_output=True)
        self.assertEqual(ancestor.returncode, 0)

        # The real integration gate captured and passed the trusted check.
        gate_result = json.loads((output_dir / "GATE-RESULT.json").read_text())
        self.assertEqual(gate_result["candidate_sha"], candidate_sha)
        self.assertEqual(gate_result["base_sha"], self.base_sha)
        self.assertEqual(len(gate_result["checks"]), 1)
        self.assertEqual(gate_result["checks"][0]["exit_code"], 0)

        # All requested roles had successful fixture runs under their matching
        # profile identities, and all evidence reports cite those run ids.
        role_cards = {
            "analyst": state["analysis_card"], "planner": state["planner_card"],
            "skeptic": state["skeptic_card"], "test-designer": state["test-designer_card"],
            "judge": state["judge_card"], "backend": item["card"],
            "reviewer": item["reviewer_card"], "qa": item["qa_card"],
            "integrator": item["integrator_card"],
        }
        fixture_runs = {"analyst": analyst_run, "planner": planner_run,
                        **plan_runs, "judge": judge_run, "backend": developer_run,
                        **evidence_runs, "integrator": integrator_run}
        for role, card_id in role_cards.items():
            with self.subTest(role=role):
                card = self.board.get_task(card_id)
                self.assertEqual(card["assignee"], "team-" + role)
                matching = [run for run in self.board.get_runs(card_id)
                            if str(run["id"]) == fixture_runs[role]]
                self.assertEqual(len(matching), 1)
                self.assertEqual(matching[0]["profile"], card["assignee"])
                self.assertEqual(matching[0]["outcome"], "completed")
                context = state['task_contexts'][card_id]
                self.assertTrue(context['ready'])
                self.assertEqual(context['project_id'], 'demo')
                self.assertTrue(Path(context['pack_path']).is_file())
                expected_revision = candidate_sha if role in {'reviewer', 'qa', 'integrator'} else self.base_sha
                self.assertEqual(context['revision'], expected_revision)
                self.assertFalse(context['planned'])
                if role == 'backend':
                    self.assertIn(context['pack_path'], (output_dir / 'TASK.md').read_text())
                else:
                    self.assertIn(context['pack_path'], card['body'])

        review_fixture = json.loads((output_dir / "REVIEW.json").read_text())
        qa_fixture = json.loads((output_dir / "QA.json").read_text())
        self.assertEqual((review_fixture["assignee"], review_fixture["run_id"],
                          review_fixture["base_sha"], review_fixture["head_sha"]),
                         ("team-reviewer", evidence_runs["reviewer"], item["base_sha"], candidate_sha))
        self.assertEqual((qa_fixture["assignee"], qa_fixture["run_id"],
                          qa_fixture["base_sha"], qa_fixture["head_sha"]),
                         ("team-qa", evidence_runs["qa"], item["base_sha"], candidate_sha))

        # Retry/idempotent tick after durable completion cannot add cards,
        # rebuild the integration branch or complete the root twice.
        task_count = len(self.board.tasks)
        main_sha = git(self.repo, "rev-parse", "main")
        integration_sha_before = integration_sha
        self.controller.tick(state)
        self.assertEqual(state["stage"], "done")
        self.assertEqual(len(self.board.tasks), task_count)
        self.assertEqual(self.board.completed_external_calls, 1)
        self.assertEqual(git(self.repo, "rev-parse", "main"), main_sha)
        self.assertEqual(git(self.repo, "rev-parse", integration_ref), integration_sha_before)


class NativeFixtureBoard(HermesBoard):
    """Real SQLite claims/completion; only the model work itself is a fixture."""

    completed_external_calls = 0

    @property
    def tasks(self):
        return {row["id"]: row for row in self.list_tasks(limit=200)}

    def get_task(self, task_id):
        task = super().get_task(task_id)
        if task is not None:
            with self._connection() as conn:
                task["parents"] = self._kb.parent_ids(conn, task_id)
        return task

    def complete_external(self, *args, **kwargs):
        result = super().complete_external(*args, **kwargs)
        self.completed_external_calls += 1
        return result

    def finish_fixture_run(self, task_id):
        with self._connection() as conn:
            task = self._kb.claim_task(conn, task_id, claimer="fixture:worker")
            if task is None:
                raise AssertionError(f"Native worker card not claimable: {task_id}")
            if not self._kb.complete_task(conn, task_id, result="fixture complete",
                                          summary="fixture complete",
                                          expected_run_id=task.current_run_id):
                raise AssertionError("Native fixture completion failed")
            return str(task.current_run_id)


@unittest.skipUnless((HERMES_SOURCE / "hermes_cli/kanban_db.py").is_file(),
                     "Hermes source unavailable for native gate lifecycle test")
class NativeFullWorkflowTests(FullWorkflowFixtureIntegrationTests):
    """Same real Git scenario, plus actual Hermes root/worker run ownership."""

    def setUp(self):
        super().setUp()
        self.env_patch = patch.dict(os.environ, {
            "HERMES_SOURCE_ROOT": str(HERMES_SOURCE),
            "HERMES_KANBAN_HOME": str(self.root / "hermes"),
        })
        self.env_patch.start()
        self.board_patch.stop()
        self.board = NativeFixtureBoard("fixture-board", self.root / "hermes")
        self.board_patch = patch("team_engine.controller.HermesBoard", return_value=self.board)
        self.board_patch.start()
        self.controller = Controller(self.config_path)

    def tearDown(self):
        self.env_patch.stop()
        super().tearDown()


if __name__ == "__main__":
    unittest.main()
