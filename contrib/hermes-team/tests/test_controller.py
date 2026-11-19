from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.controller import Controller, main as controller_main
from team_engine.contracts import ContractError
from team_engine.git_gates import GitGateError
from team_engine.quota import QuotaManager
from team_engine.execution_policy import verification_plan


class FakeBoard:
    """Small in-memory stand-in for Hermes Kanban; no agent/model calls."""

    def __init__(self, *_args, **_kwargs):
        self.tasks: dict[str, dict] = {}
        self.runs: dict[str, list[dict]] = {}
        self.comments: list[tuple[str, str, str]] = []
        self.completed_external_calls = 0
        self.configured_waiting_tasks: list[tuple[str, dict]] = []
        self.counter = 0
        self.before_activate = None

    def ensure_board(self, *_args, **_kwargs):
        return {"slug": "test-board"}

    def _create(self, title, assignee, parents=(), status="running", **kwargs):
        self.counter += 1
        task_id = f"t_{self.counter:08x}"
        self.tasks[task_id] = {"id": task_id, "title": title, "assignee": assignee,
                               "parents": list(parents), "status": status,
                               "blocked_reason": None, "created_by": "hermes-team-controller",
                               **kwargs}
        self.runs[task_id] = [{"id": f"run-{self.counter}", "profile": assignee}]
        return self.tasks[task_id]

    def machine_card(self, title, body="", idempotency_key=None, **kwargs):
        for task in self.tasks.values():
            if task.get("idempotency_key") == idempotency_key:
                return task
        return self._create(title, "team-controller", status="running", body=body,
                            idempotency_key=idempotency_key, **kwargs)

    def create_task(self, title, assignee, body=None, parents=(), priority=0,
                    workspace_path=None, model=None, provider=None, reasoning_effort=None,
                    idempotency_key=None, max_runtime_seconds=900, initial_status="running", **kwargs):
        for task in self.tasks.values():
            if task.get("idempotency_key") == idempotency_key:
                return task
        return self._create(title, assignee, parents=parents, status=initial_status,
                            body=body, priority=priority, workspace_path=workspace_path,
                            model=model, provider=provider, reasoning_effort=reasoning_effort,
                            idempotency_key=idempotency_key,
                            max_runtime_seconds=max_runtime_seconds, **kwargs)

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def get_runs(self, task_id):
        return self.runs.get(task_id, [])

    def activate_task(self, task_id):
        task = self.tasks[task_id]
        if task["status"] != "blocked":
            raise AssertionError("FakeBoard activation expects a parked task")
        if self.before_activate:
            self.before_activate(task_id)
        parents_done = all(self.tasks[parent]["status"] == "done" for parent in task["parents"])
        task["status"] = "ready" if parents_done else "todo"
        return task

    def configure_waiting_task(self, task_id, *, model, provider, reasoning_effort):
        task = self.tasks[task_id]
        if task["status"] != "blocked":
            raise AssertionError("FakeBoard model pin expects a blocked task")
        values = {"model": model, "provider": provider, "reasoning_effort": reasoning_effort}
        task.update(values)
        self.configured_waiting_tasks.append((task_id, values))

    def set_waiting_task_runtime(self, task_id, max_runtime_seconds):
        task = self.tasks[task_id]
        if task["status"] != "blocked":
            raise AssertionError("FakeBoard runtime update expects a parked task")
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


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


class ControllerTests(unittest.TestCase):
    def test_root_key_preserves_legacy_persisted_idempotency_key(self):
        self.assertEqual(Controller.root_key({"id": "old-feature"}), "old-feature:root")
        self.assertEqual(Controller.root_key({"id": "new-feature", "root_round": 1}),
                         "new-feature:root:r1")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Test User")
        git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("base\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "base")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({
            "data_root": str(self.root / "data"),
            "bundle_root": str(self.root / "bundle"),
            "board": "test-board",
            "hermes_root": str(self.root / "hermes"),
            "projects": {"demo": {"repo": str(self.repo), "base_ref": "main",
                                    "checks": [[sys.executable, "-c", "pass"]]}},
            "roles": {
                "team-analyst": {"model": "test-model", "provider": "test", "reasoning_effort": "high", "run_budget": 100},
                "team-planner": {"model": "test-model", "provider": "test", "reasoning_effort": "high", "run_budget": 100},
                "team-skeptic": {"model": "test-model", "provider": "test", "reasoning_effort": "high", "run_budget": 100},
                "team-test-designer": {"model": "test-model", "provider": "test", "reasoning_effort": "high", "run_budget": 100},
                "team-judge": {"model": "test-model", "provider": "test", "reasoning_effort": "high", "run_budget": 100},
                "team-backend": {"model": "test-model", "provider": "test", "reasoning_effort": "low", "run_budget": 100, "max_turns": 40},
                "team-reviewer": {"model": "test-model", "provider": "test", "reasoning_effort": "high", "run_budget": 100},
                "team-qa": {"model": "test-model", "provider": "test", "reasoning_effort": "medium", "run_budget": 100},
                "team-integrator": {"model": "test-model", "provider": "test", "reasoning_effort": "high", "run_budget": 100},
            },
            "max_plan_tasks": 8,
        }))
        self.bundle = self.root / "bundle"
        (self.bundle / "templates").mkdir(parents=True)
        self.bundle_templates = self.bundle / "templates"
        self.bundle_templates.mkdir(exist_ok=True)
        self.board = FakeBoard()
        self.board_patch = patch("team_engine.controller.HermesBoard", return_value=self.board)
        self.board_patch.start()
        self.controller = Controller(self.config_path)

    def tearDown(self):
        self.board_patch.stop()
        self.temp.cleanup()

    def submit(self):
        return self.controller.submit("demo", "Implement one safe feature.\n", "Demo feature", "feature-1")

    def enable_test_quota(self, *, max_snapshot_age=300):
        self.controller.quota = QuotaManager({"pools": {
            "test": {"provider": "test", "enabled": True, "auth_ready": True,
                     "max_concurrent": 20, "reserve_percent": 20,
                     "unknown_policy": "wait", "max_snapshot_age": max_snapshot_age},
        }}, self.root / "data/quota.json")

    def update_test_quota(self, used_percent=10):
        self.controller.quota.update_snapshot("test", {
            "source": "controller-test", "observed_at": time.time(),
            "windows": [{"name": "weekly", "used_percent": used_percent,
                         "reset_at": time.time() + 3600}],
        })

    def _parked_backend_card(self):
        self.enable_test_quota()
        state = self.submit()
        card_id = self.controller.spawn(
            state, 'backend', 'Legacy backend task', 'Implement backend work.',
            self.repo, 'dev:T-008', parked=True)
        state['items'] = [{'card': card_id, 'status': 'planned'}]
        self.controller.save(state)
        return state, card_id

    def _make_legacy_worker_binding(self, state, card_id):
        old_path = Path(state['prerequisite_paths'].pop(card_id))
        if old_path.exists():
            old_path.rmdir()
        snapshot = state['assignment_snapshots'][card_id]
        snapshot.pop('run_budget', None)
        snapshot.pop('max_turns', None)
        self.controller.save(state)
        return old_path

    def test_legacy_parked_worker_binding_is_migrated_before_unblock(self):
        state, card_id = self._parked_backend_card()
        self._make_legacy_worker_binding(state, card_id)
        task = self.board.get_task(card_id)
        self.assertEqual(task['status'], 'blocked')

        def assert_state_was_saved_before_activation(activated_card):
            self.assertEqual(activated_card, card_id)
            persisted = json.loads((self.controller.path(state) / 'state.json').read_text())
            snapshot = persisted['assignment_snapshots'][card_id]
            prereq = Path(persisted['prerequisite_paths'][card_id])
            self.assertEqual(snapshot['role'], 'team-backend')
            self.assertEqual(snapshot['run_budget'], 100)
            self.assertEqual(snapshot['max_turns'], 40)
            self.assertTrue(prereq.is_dir())
            self.assertEqual(persisted['legacy_worker_binding_migrations'][card_id]['version'], 1)

        self.board.before_activate = assert_state_was_saved_before_activation
        self.update_test_quota()
        self.controller.activate_with_quota(state, card_id)

        repaired_path = Path(state['prerequisite_paths'][card_id])
        self.assertEqual(self.board.get_task(card_id)['status'], 'ready')
        self.assertTrue(repaired_path.is_dir())
        comments = [body for target, _author, body in self.board.comments if target == card_id]
        handoff = next(body for body in comments if 'hermes-team-worker-binding-v1' in body)
        self.assertIn(str(repaired_path / 'PREREQUISITES.json'), handoff)
        self.assertIn(str(repaired_path / 'TEST-CHECKPOINT.md'), handoff)

    def test_existing_valid_worker_prerequisite_path_is_preserved(self):
        state, card_id = self._parked_backend_card()
        path = state['prerequisite_paths'][card_id]
        self.assertFalse(self.controller.ensure_legacy_worker_binding(
            state, card_id, self.board.get_task(card_id)))
        self.assertEqual(state['prerequisite_paths'][card_id], path)

    def test_legacy_binding_fails_closed_for_invalid_path_or_native_owner(self):
        state, card_id = self._parked_backend_card()
        state['prerequisite_paths'][card_id] = str(self.root / 'outside-prerequisites')
        with self.assertRaisesRegex(ContractError, 'outside its feature directory'):
            self.controller.ensure_legacy_worker_binding(state, card_id, self.board.get_task(card_id))

        state, card_id = self._parked_backend_card()
        self._make_legacy_worker_binding(state, card_id)
        self.board.get_task(card_id)['assignee'] = 'team-frontend'
        with self.assertRaisesRegex(ContractError, 'does not match its pinned assignment'):
            self.controller.ensure_legacy_worker_binding(state, card_id, self.board.get_task(card_id))

    def test_submit_pins_request_to_clean_base_and_starts_analysis(self):
        state = self.submit()
        self.assertEqual(state["stage"], "analysis")
        self.assertEqual(state["initial_base_sha"], self.base_sha)
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.base_sha)
        self.assertTrue((self.root / "data/runs/feature-1/BRIEF.md").is_file())
        analysis = self.board.get_task(state["analysis_card"])
        self.assertEqual(analysis["assignee"], "team-analyst")
        self.assertEqual(analysis["status"], "running")
        snapshot = state['project_context']
        self.assertEqual(snapshot['revision'], self.base_sha)
        self.assertEqual(state['task_contexts'][analysis['id']]['context_hash'], snapshot['context_hash'])
        self.assertIn(snapshot['pack_path'], analysis['body'])
        self.assertTrue(Path(snapshot['manifest_path']).is_file())

    def test_required_context_failure_creates_no_native_cards(self):
        self.controller.config['projects']['demo']['context_required'] = True
        with self.assertRaisesRegex(ContractError, 'context'):
            self.submit()
        self.assertEqual(self.board.tasks, {})

    def test_context_uses_base_commit_not_working_branch_head(self):
        git(self.repo, 'checkout', '-b', 'unrelated-docs')
        (self.repo / 'README.md').write_text('Other branch rules\n')
        git(self.repo, 'commit', '-am', 'unrelated docs')
        state = self.submit()
        package = Path(state['project_context']['pack_path']).read_text()
        self.assertNotIn('Other branch rules', package)
        self.assertEqual(state['project_context']['revision'], self.base_sha)

    def test_new_role_gets_candidate_context_without_replacing_feature_baseline(self):
        state = self.submit()
        original = state['project_context']
        workspace = self.controller.worktree(state, 'candidate-docs', self.base_sha, 'candidate-docs')
        (workspace / 'README.md').write_text('Candidate project documentation\n')
        git(workspace, 'commit', '-am', 'candidate docs')
        candidate = git(workspace, 'rev-parse', 'HEAD')
        card = self.controller.spawn(state, 'reviewer', 'review', 'Review candidate', workspace, 'candidate-review')
        snapshot = state['task_contexts'][card]
        self.assertEqual(snapshot['revision'], candidate)
        self.assertNotEqual(snapshot['context_hash'], original['context_hash'])
        self.assertEqual(state['project_context'], original)
        self.assertIn('Candidate project documentation', Path(snapshot['pack_path']).read_text())
        self.assertIn(snapshot['pack_path'], self.board.get_task(card)['body'])

    def test_context_handoff_rejects_foreign_repository(self):
        state = self.submit()
        other = self.root / 'other'
        other.mkdir()
        git(other, 'init', '-b', 'main')
        git(other, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '--allow-empty', '-m', 'other')
        before = len(self.board.tasks)
        with self.assertRaisesRegex(ContractError, 'different repository'):
            self.controller.spawn(state, 'reviewer', 'wrong repo', 'body', other, 'foreign')
        self.assertEqual(len(self.board.tasks), before)

    def test_existing_worktree_with_wrong_head_does_not_launch_or_reset(self):
        state = self.submit()
        workspace = self.controller.worktree(state, 'stale', self.base_sha, 'stale')
        (workspace / 'README.md').write_text('New revision in existing workspace\n')
        git(workspace, 'commit', '-am', 'drifted workspace')
        head = git(workspace, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(ContractError, 'expected repository or revision'):
            self.controller.worktree(state, 'stale', self.base_sha, 'stale')
        self.assertEqual(git(workspace, 'rev-parse', 'HEAD'), head)

    def test_submit_pins_project_check_policy_against_later_config_mutation(self):
        state = self.submit()
        original_checks = state["project_contract"]["checks"]
        self.controller.config["projects"]["demo"]["checks"] = [[sys.executable, "-c", "raise SystemExit(1)"]]
        self.assertNotEqual(self.controller.config["projects"]["demo"]["checks"], original_checks)
        self.assertEqual(self.controller.project_settings(state)["checks"], original_checks)

    def test_quota_wait_wakes_on_snapshot_pins_selection_and_releases_completed_lease(self):
        self.enable_test_quota()
        state = self.submit()
        card_id = state["analysis_card"]
        task = self.board.get_task(card_id)
        lease_key = state["dispatch"][card_id]["key"]
        self.assertEqual(task["status"], "blocked")
        self.assertTrue(state["dispatch"][card_id]["waiting"])
        self.assertEqual(state['assignment_snapshots'][card_id]['model'], task['model'])
        self.assertEqual(self.controller.quota.status()["leases"], {})
        self.assertEqual(self.board.get_task(state["root_card"])["status"], "running")

        self.controller.tick(state)
        self.assertEqual(state["stage"], "analysis")
        self.assertIn(card_id, state["waiting_for_quota"])
        self.update_test_quota()
        self.controller.tick(state)
        task = self.board.get_task(card_id)
        self.assertEqual(task["status"], "ready")
        self.assertEqual((task["provider"], task["model"], task["reasoning_effort"]),
                         ("test", "test-model", "high"))
        self.assertEqual(self.board.configured_waiting_tasks[-1][0], card_id)
        self.assertFalse(state["dispatch"][card_id]["waiting"])
        self.assertEqual(self.controller.quota.status()["leases"][lease_key]["active"], True)

        task["status"] = "done"
        run = self.board.runs[card_id][-1]
        run.update({"id": 301, "status": "completed", "outcome": "completed"})
        self.controller.tick(state)
        self.assertFalse(self.controller.quota.status()["leases"][lease_key]["active"])

    def test_parked_developer_task_does_not_reserve_quota_before_activation(self):
        self.enable_test_quota()
        self.update_test_quota()
        state = self.submit()
        card_id = self.controller.spawn(state, "backend", "Parked developer task", "body",
                                        self.root / "workspace", "parked-dev", parked=True)
        self.assertEqual(self.board.get_task(card_id)["status"], "blocked")
        self.assertFalse(state["dispatch"][card_id]["armed"])
        self.assertTrue(state["dispatch"][card_id]["waiting"])
        self.assertEqual(self.controller.quota.status()["pools"]["test"]["active_leases"], 1)
        self.assertNotIn(state["dispatch"][card_id]["key"],
                         self.controller.quota.status()["leases"])

    def test_parked_task_refreshes_current_role_runtime_cap_without_changing_frozen_model(self):
        state = self.submit()
        card_id = self.controller.spawn(state, "backend", "Parked developer task", "body",
                                        self.root / "workspace", "parked-runtime", parked=True)
        task = self.board.get_task(card_id)
        self.assertEqual(task["max_runtime_seconds"], 190)
        frozen = (task["model"], task["provider"], task["reasoning_effort"])

        current_role = self.controller.config["roles"]["team-backend"]
        current_role.update(run_budget=1200, model="new-model", provider="new-provider",
                            reasoning_effort="xhigh")
        self.controller.activate_with_quota(state, card_id)

        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["max_runtime_seconds"], 1290)
        self.assertEqual((task["model"], task["provider"], task["reasoning_effort"]), frozen)

    def test_quota_activation_refreshes_runtime_but_keeps_frozen_quota_assignment(self):
        self.enable_test_quota()
        self.update_test_quota()
        state = self.submit()
        card_id = self.controller.spawn(state, "backend", "Parked developer task", "body",
                                        self.root / "workspace", "parked-quota-runtime", parked=True)
        task = self.board.get_task(card_id)
        frozen = (task["model"], task["provider"], task["reasoning_effort"])
        self.controller.config["roles"]["team-backend"].update(
            run_budget=1200, model="new-model", provider="new-provider", reasoning_effort="xhigh")
        self.update_test_quota(used_percent=20)

        self.controller.activate_with_quota(state, card_id)

        self.assertEqual(task["status"], "ready", state["dispatch"][card_id])
        self.assertEqual(task["max_runtime_seconds"], 1290)
        self.assertEqual((task["model"], task["provider"], task["reasoning_effort"]), frozen)

    def test_repeat_activation_of_already_ready_quota_task_only_clears_wait_state(self):
        self.enable_test_quota()
        self.update_test_quota()
        state = self.submit()
        card_id = self.controller.spawn(state, "backend", "Parked developer task", "body",
                                        self.root / "workspace", "repeat-activation", parked=True)
        task = self.board.get_task(card_id)
        frozen = (task["max_runtime_seconds"], task["model"], task["provider"],
                  task["reasoning_effort"])
        task["status"] = "ready"  # Native activation committed before the controller state did.
        state["dispatch"][card_id]["waiting"] = True

        with patch.object(self.board, "set_waiting_task_runtime", wraps=self.board.set_waiting_task_runtime) as set_runtime, \
             patch.object(self.board, "configure_waiting_task", wraps=self.board.configure_waiting_task) as configure:
            self.controller.activate_with_quota(state, card_id)

        self.assertFalse(state["dispatch"][card_id]["waiting"])
        self.assertEqual((task["max_runtime_seconds"], task["model"], task["provider"],
                          task["reasoning_effort"]), frozen)
        set_runtime.assert_not_called()
        configure.assert_not_called()

    def test_repeat_activation_of_already_ready_nonquota_task_is_idempotent(self):
        state = self.submit()
        card_id = self.controller.spawn(state, "backend", "Parked developer task", "body",
                                        self.root / "workspace", "repeat-noquota", parked=True)
        task = self.board.get_task(card_id)
        original_cap = task["max_runtime_seconds"]
        task["status"] = "ready"

        with patch.object(self.board, "set_waiting_task_runtime", wraps=self.board.set_waiting_task_runtime) as set_runtime, \
             patch.object(self.board, "activate_task", wraps=self.board.activate_task) as activate:
            self.controller.activate_with_quota(state, card_id)

        self.assertEqual(task["status"], "ready")
        self.assertEqual(task["max_runtime_seconds"], original_cap)
        set_runtime.assert_not_called()
        activate.assert_not_called()

    def test_retry_worker_cannot_reuse_released_quota_lease_without_fresh_admission(self):
        self.enable_test_quota()
        self.update_test_quota()
        state = self.submit()
        card_id = state["analysis_card"]
        key = state["dispatch"][card_id]["key"]
        self.assertTrue(self.controller.quota.status()["leases"][key]["active"])

        self.board.block_external(card_id, "simulated worker failure")
        self.controller.reconcile_dispatch(state, activate=False)
        self.assertFalse(self.controller.quota.status()["leases"][key]["active"])

        # This models resume --retry-workers: the existing task key is tried
        # again, but quota has since been exhausted.
        self.controller.quota.update_snapshot("test", {
            "source": "controller-test", "observed_at": time.time(),
            "windows": [{"name": "weekly", "used_percent": 100,
                         "reset_at": time.time() + 3600}],
        })
        state["dispatch"][card_id]["waiting"] = True
        self.controller.activate_with_quota(state, card_id)
        self.assertEqual(self.board.get_task(card_id)["status"], "blocked")
        self.assertFalse(self.controller.quota.status()["leases"][key]["active"])

        self.update_test_quota(used_percent=25)
        self.controller.activate_with_quota(state, card_id)
        self.assertEqual(self.board.get_task(card_id)["status"], "ready")
        self.assertTrue(self.controller.quota.status()["leases"][key]["active"])
        self.assertEqual(self.controller.quota.status()["leases"][key]["generation"], 2)

    def test_submit_rejects_unregistered_or_unchecked_project(self):
        with self.assertRaisesRegex(Exception, "not registered"):
            self.controller.submit("missing", "brief", "title", "feature-2")
        self.controller.config["projects"]["unchecked"] = {"repo": str(self.repo), "base_ref": "main", "checks": []}
        with self.assertRaisesRegex(Exception, "trusted regression"):
            self.controller.submit("unchecked", "brief", "title", "feature-3")

    def test_submit_is_idempotent_for_same_request_and_rejects_id_collision(self):
        first = self.submit()
        card_count = len(self.board.tasks)
        repeated = self.submit()
        self.assertEqual(repeated["root_card"], first["root_card"])
        self.assertEqual(len(self.board.tasks), card_count)
        with self.assertRaisesRegex(Exception, "different request"):
            self.controller.submit("demo", "different brief", "Demo feature", "feature-1")

    def test_auto_start_rejects_spec_changed_after_owner_approval(self):
        state = self.submit()
        state["workflow_managed"] = True
        self.controller.config["auto_start_after_judge_accept"] = True
        run_dir = self.controller.path(state)
        artifacts = run_dir / "artifacts"
        approved_spec = "# Approved\n" + "Owner-approved requirement. " * 15
        (artifacts / "SPEC.md").write_text("# Mutated\n" + "Changed requirement. " * 15)
        (artifacts / "DESIGN.md").write_text("# Design\n" + "A sufficient design. " * 12)
        (artifacts / "PLAN.json").write_text('{"schema_version":1}\n')
        state["approved_spec"] = approved_spec
        state["approved_spec_sha256"] = hashlib.sha256(
            approved_spec.encode("utf-8")).hexdigest()
        review_dir = run_dir / "analysis-round-1"
        review_dir.mkdir()
        (review_dir / "DECISION.json").write_text("{}\n")
        state.update({"stage": "plan_judgment", "plan_round": 1,
                      "plan_review_path": str(review_dir),
                      "plan_sha256": "accepted-plan-digest",
                      "judge_card": self.board.create_task("judge", "team-judge")["id"],
                      "judge_workspace": str(self.controller.worktree(state, "approved-spec-judge",
                                                                         self.base_sha))})
        self.board.tasks[state["judge_card"]]["status"] = "done"
        self.board.runs[state["judge_card"]] = [{
            "id": 701, "profile": "team-judge", "status": "completed", "outcome": "completed"}]

        with patch.object(self.controller, "plan_reviews", return_value=[]), \
             patch.object(self.controller, "verify_actor"), \
             patch("team_engine.controller.validate_plan_decision", return_value="accept"):
            self.controller.tick(state)

        self.assertEqual(state["stage"], "blocked")
        self.assertEqual(state["resume_stage"], "plan_judgment")
        self.assertIn("Approved requirements changed", state["blocker"])
        self.assertNotIn("approved_plan_sha256", state)
        self.assertEqual(state["items"], [])

    def test_base_ref_movement_blocks_stale_feature(self):
        state = self.submit()
        (self.repo / "README.md").write_text("base moved\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "move base")

        self.controller.tick(state)

        self.assertEqual(state["stage"], "blocked")
        self.assertIn("base branch changed", state["blocker"])
        self.assertEqual(self.board.tasks[state["root_card"]]["status"], "blocked")

    def test_ready_task_scheduler_is_bounded_and_waits_for_dependencies(self):
        self.controller.config["max_parallel_tasks"] = 2
        state = {"stage": "implementation", "current_sha": self.base_sha, "id": "feature-1",
                 "items": [
                     {"definition": {"id": "T-1", "dependencies": [],
                                      "owned_paths": ["backend/leads"]}, "status": "planned"},
                     {"definition": {"id": "T-2", "dependencies": [],
                                      "owned_paths": ["frontend/leads"]}, "status": "planned"},
                     {"definition": {"id": "T-3", "dependencies": ["T-1"],
                                      "owned_paths": ["backend/reports"]}, "status": "planned"},
                     {"definition": {"id": "T-4", "dependencies": [],
                                      "owned_paths": ["backend/leads/detail"]}, "status": "planned"},
                 ]}
        prepared = []

        def prepare(_state, item):
            item["status"] = "implementation"
            item["card"] = "worker-" + item["definition"]["id"]
            prepared.append(item["definition"]["id"])

        with patch.object(self.controller, "prepare_specific_item", side_effect=prepare):
            self.controller.prepare_ready_items(state)

        self.assertEqual(prepared, ["T-1", "T-2"])
        self.assertEqual(state["items"][2]["status"], "planned")
        self.assertEqual(state["items"][3]["status"], "planned", "overlapping ownership must serialize")

    def test_ready_task_scheduler_serializes_unknown_ownership(self):
        self.controller.config["max_parallel_tasks"] = 2
        state = {"stage": "implementation", "current_sha": self.base_sha, "id": "feature-unknown",
                 "items": [
                     {"definition": {"id": "T-1", "dependencies": [], "owned_paths": None},
                      "status": "planned"},
                     {"definition": {"id": "T-2", "dependencies": [], "owned_paths": ["frontend/src"]},
                      "status": "planned"},
                 ]}
        prepared = []

        def prepare(_state, item):
            item["status"] = "implementation"
            item["card"] = "worker-" + item["definition"]["id"]
            prepared.append(item["definition"]["id"])

        with patch.object(self.controller, "prepare_specific_item", side_effect=prepare):
            self.controller.prepare_ready_items(state)

        self.assertEqual(prepared, ["T-1"])
        self.assertEqual(state["items"][1]["status"], "planned",
                         "unknown ownership must act as a repository-wide lock")

    def test_blocked_qa_retry_uses_selected_fast_checks_from_saved_plan(self):
        quick = [sys.executable, "-c", "print('quick')"]
        full = [sys.executable, "-c", "print('full')"]
        self.controller.config["projects"]["demo"].update({
            "checks": [{"name": "backend-unit", "argv": quick},
                       {"name": "full-regression", "argv": full}],
            "execution_policy": {"version": 1, "full_after_tasks": 2,
                                 "path_rules": [{"name": "backend", "prefixes": ["backend"],
                                                 "quick_check_indexes": [0]}]},
        })
        state = {"id": "feature-retry", "project": "demo", "stage": "blocked",
                 "initial_base_sha": self.base_sha,
                 "items": []}
        item = {"definition": {"id": "T-001", "title": "Backend change"}, "round": 1,
                "workspace": str(self.repo), "head_sha": self.base_sha,
                "base_sha": self.base_sha, "card": "t_parent", "qa_card": "t_old_qa",
                "candidate_changed_paths": ["backend/leads.py"],
                "verification_blocked_roles": ["qa"]}
        state["items"].append(item)
        state["index"] = 0
        round_dir = self.controller.round_path(state, item)
        (round_dir / "QA.json").write_text("{}\n")
        spawned = {}

        def capture_spawn(_state, role, _title, body, workspace, _key, parents=None):
            spawned.update(role=role, body=body, workspace=workspace, parents=parents)
            return "t_new_qa"

        with patch.object(self.controller, "worktree", return_value=self.repo), \
             patch.object(self.controller, "spawn", side_effect=capture_spawn):
            self.controller.retry_blocked_verification(state, item, "environment fixed")

        self.assertEqual(spawned["role"], "qa")
        self.assertIn(json.dumps([quick]), spawned["body"])
        self.assertNotIn(json.dumps([full]), spawned["body"])
        self.assertEqual(item["status"], "verification")

    def test_candidate_with_advanced_staging_base_gets_a_fresh_review_cycle(self):
        state = {"current_sha": "b" * 40}
        item = {"base_sha": "a" * 40, "round": 1, "definition": {"id": "T-2"}}
        with patch.object(self.controller, "rework") as rework:
            refreshed = self.controller._refresh_candidate_on_current_base(
                state, item, "review reports are stale")

        self.assertTrue(refreshed)
        self.assertEqual(item["base_sha"], "b" * 40)
        rework.assert_called_once()
        self.assertIn("prior review/QA evidence is stale", rework.call_args.args[2])

    def test_batch_regression_accepts_only_exact_staging_head_and_marks_provisional_items(self):
        root = self.board.machine_card("feature root", idempotency_key="feature-1:root")
        first = {"definition": {"id": "T-1", "assignee_role": "backend"},
                 "status": "provisional", "integration": {"integration_sha": self.base_sha}}
        second = {"definition": {"id": "T-2", "assignee_role": "backend"},
                  "status": "provisional", "integration": {"integration_sha": self.base_sha}}
        future = {"definition": {"id": "T-3", "assignee_role": "backend", "dependencies": []},
                  "status": "planned"}
        state = {"id": "feature-1", "title": "Feature", "repo": str(self.repo),
                 "root_card": root["id"], "project": "demo", "project_contract": {
                     "checks": [[sys.executable, "-c", "pass"]]},
                 "initial_base_sha": self.base_sha, "current_sha": self.base_sha,
                 "candidate_ref": "local-candidate", "index": 2, "items": [first, second, future],
                 "pending_regression_items": ["T-1", "T-2"], "stage": "implementation"}
        self.controller.path = lambda _state: self.root / "data/runs/feature-1"
        (self.root / "data/runs/feature-1").mkdir(parents=True)

        with patch("team_engine.controller.run_batch_regression", return_value={
                "head_sha": self.base_sha, "status": "pass", "results": [], "failed_count": 0}):
            self.controller.run_pending_regression(state, force=True)

        self.assertEqual(state["accepted_regression_sha"], self.base_sha)
        self.assertEqual(state["pending_regression_items"], [])
        self.assertEqual([item["status"] for item in state["items"]], ["done", "done", "planned"])
        self.assertEqual([item["batch_id"] for item in state["items"][:2]], [1, 1])
        self.assertEqual(state["regression_batches"][0]["status"], "pass")

    def test_batch_flushes_when_next_task_overlaps_provisional_scope(self):
        provisional = {
            "definition": {"id": "T-001", "owned_paths": ["backend/leads"]},
            "status": "provisional",
        }
        next_item = {
            "definition": {"id": "T-002", "owned_paths": ["backend/leads/assignment"],
                            "dependencies": []},
            "status": "planned",
        }
        state = {"index": 1, "items": [provisional, next_item],
                 "pending_regression_items": ["T-001"], "project_contract": {
                     "execution_policy": {"full_after_tasks": 3}}}

        self.assertFalse(self.controller._scopes_are_disjoint_from_unaccepted(state, next_item))
        self.assertTrue(self.controller._batch_must_flush(state))

        next_item["definition"]["owned_paths"] = ["frontend/leads"]
        self.assertTrue(self.controller._scopes_are_disjoint_from_unaccepted(state, next_item))
        self.assertFalse(self.controller._batch_must_flush(state))

    def test_batch_regression_failure_preserves_provisional_work_for_repair(self):
        root = self.board.machine_card("feature root", idempotency_key="feature-1:root")
        task = {"definition": {"id": "T-1", "assignee_role": "backend"},
                "status": "provisional", "integration": {"integration_sha": self.base_sha}}
        state = {"id": "feature-1", "title": "Feature", "repo": str(self.repo),
                 "root_card": root["id"], "project": "demo", "project_contract": {
                     "checks": [[sys.executable, "-c", "pass"]]},
                 "initial_base_sha": self.base_sha, "current_sha": self.base_sha,
                 "candidate_ref": "local-candidate", "index": 1, "items": [task],
                 "pending_regression_items": ["T-1"], "stage": "implementation"}
        self.controller.path = lambda _state: self.root / "data/runs/feature-1"
        (self.root / "data/runs/feature-1").mkdir(parents=True)

        with patch("team_engine.controller.run_batch_regression", return_value={
                "head_sha": self.base_sha, "status": "fail", "failed_count": 1,
                "results": [{"name": "backend", "argv": ["test"], "status": "fail"}]}), \
             patch.object(self.controller, "create_batch_repair") as repair:
            self.controller.run_pending_regression(state, force=True)

        repair.assert_called_once()
        self.assertNotIn("accepted_regression_sha", state)
        self.assertEqual(task["status"], "provisional")
        self.assertEqual(state["pending_regression_items"], ["T-1"])
        self.assertEqual(state["regression_batches"][0]["status"], "fail")

    def test_interrupted_batch_retries_same_id_sha_and_scope(self):
        root = self.board.machine_card("feature root", idempotency_key="feature-1:root")
        task = {"definition": {"id": "T-001", "assignee_role": "backend"},
                "status": "provisional", "integration": {"integration_sha": self.base_sha}}
        future = {"definition": {"id": "T-002", "assignee_role": "backend", "dependencies": []},
                  "status": "planned"}
        batch = {"id": 7, "task_ids": ["T-001"], "head_sha": self.base_sha,
                 "status": "running", "attempts": 1}
        state = {"id": "feature-1", "title": "Feature", "repo": str(self.repo),
                 "root_card": root["id"], "project": "demo", "project_contract": {
                     "checks": [[sys.executable, "-c", "pass"]]},
                 "initial_base_sha": self.base_sha, "current_sha": self.base_sha,
                 "candidate_ref": "local-candidate", "index": 1, "items": [task, future],
                 "pending_regression_items": ["T-001"], "stage": "implementation",
                 "regression_batch": dict(batch), "regression_batches": [dict(batch)]}
        self.controller.path = lambda _state: self.root / "data/runs/feature-1"
        (self.root / "data/runs/feature-1").mkdir(parents=True)

        with patch("team_engine.controller.run_batch_regression", return_value={
                "head_sha": self.base_sha, "status": "pass", "results": [], "failed_count": 0}) as run:
            self.controller.run_pending_regression(state, force=True)

        self.assertEqual(run.call_args.args[1], self.base_sha)
        self.assertEqual(state["regression_batch"]["id"], 7)
        self.assertEqual(state["regression_batch"]["attempts"], 2)
        self.assertEqual(state["regression_batches"][0]["status"], "pass")

    def test_batch_repair_uses_trusted_test_task_id_contract_and_combines_scope(self):
        root = self.board.machine_card("feature root", idempotency_key="feature-1:root")
        item = {"definition": {"id": "T-001", "assignee_role": "backend",
                                "owned_paths": ["backend/src"]},
                "status": "provisional"}
        second = {"definition": {"id": "T-002", "assignee_role": "frontend",
                                  "owned_paths": ["frontend/src"]},
                  "status": "provisional"}
        state = {"id": "feature-1", "title": "Feature", "repo": str(self.repo),
                 "root_card": root["id"], "items": [item, second], "index": 2,
                 "project": "demo", "initial_base_sha": self.base_sha,
                 "batch_repair_round": 0}
        feature_path = self.root / "data/runs/feature-1"
        feature_path.mkdir(parents=True)
        self.controller.path = lambda _state: feature_path
        captured = []

        def prepare(_state, repair):
            repair["status"] = "implementation"
            repair["workspace"] = str(self.repo)
            repair["card"] = "repair-card"
            captured.append(repair)
            task_path = feature_path / "tasks" / repair["definition"]["id"] / "TASK.md"
            task_path.parent.mkdir(parents=True)
            task_path.write_text("# repair task\n")
            out = feature_path / "tasks" / repair["definition"]["id"] / "round-1"
            out.mkdir(parents=True)
            (out / "TASK.md").write_text("# repair task\n")

        with patch.object(self.controller, "prepare_specific_item", side_effect=prepare), \
             patch.object(self.controller, "spawn", return_value="repair-card"):
            self.controller.create_batch_repair(state, {
                "id": 1, "head_sha": self.base_sha, "task_ids": ["T-001", "T-002"],
                "results": [{"name": "backend regression", "status": "fail", "error": "assertion"}],
            })

        repair = captured[0]
        self.assertEqual(repair["definition"]["id"], "T-003")
        self.assertEqual(repair["definition"]["owned_paths"], ["backend/src", "frontend/src"])
        self.assertTrue(repair["batch_repair"])
        self.assertIn("assertion", (feature_path / "tasks/T-003/TASK.md").read_text())

    def test_verify_actor_requires_exact_latest_successful_worker_run_and_profile(self):
        card = self.board.create_task("review", "team-reviewer")
        # Hermes 0.20.6 complete_task uses status=done, outcome=completed.
        run = {"id": 101, "profile": "team-reviewer", "status": "done", "outcome": "completed"}
        self.board.runs[card["id"]] = [run]
        self.controller.verify_actor(card["id"], {"assignee": "team-reviewer", "run_id": "101"}, "reviewer")
        self.board.runs[card["id"]] = [{**run, "status": "completed"}]
        self.controller.verify_actor(card["id"], {"assignee": "team-reviewer", "run_id": "101"}, "reviewer")

        cases = [
            ([{**run, "profile": "team-other"}], {"assignee": "team-reviewer", "run_id": "101"}, "identity"),
            ([{**run, "status": "running"}], {"assignee": "team-reviewer", "run_id": "101"}, "completed successfully"),
            ([{**run, "outcome": "failed"}], {"assignee": "team-reviewer", "run_id": "101"}, "completed successfully"),
            ([run, {**run, "id": 102}], {"assignee": "team-reviewer", "run_id": "101"}, "obsolete"),
            ([run], {"assignee": "team-reviewer", "run_id": "actual"}, "numeric"),
            ([run], {"assignee": "team-not-reviewer", "run_id": "101"}, "assignee"),
        ]
        for runs, evidence, message in cases:
            with self.subTest(runs=runs, evidence=evidence):
                self.board.runs[card["id"]] = runs
                with self.assertRaisesRegex(ContractError, message):
                    self.controller.verify_actor(card["id"], evidence, "reviewer")

    def test_malformed_review_report_gets_one_same_snapshot_role_retry_and_preserves_original(self):
        state = self.submit()
        self.controller.config["max_report_retries"] = 2
        definition = {"id": "task-a", "title": "Implement safe behavior",
                      "assignee_role": "backend", "owned_paths": ["README.md"],
                      "test_plan": [], "acceptance_criteria": [], "priority": "P1",
                      "priority_reason": "Needed", "dependencies": []}
        item_workspace = self.controller.worktree(state, "task-a", self.base_sha)
        out = self.root / "data/runs/feature-1/tasks/task-a/round-1"
        out.mkdir(parents=True)
        reviewer = self.board.create_task("review", "team-reviewer", workspace_path=str(item_workspace),
                                          idempotency_key="reviewer-original")
        qa = self.board.create_task("qa", "team-qa", workspace_path=str(item_workspace),
                                    idempotency_key="qa-original")
        for card, run_id, profile in ((reviewer, 501, "team-reviewer"), (qa, 502, "team-qa")):
            self.board.tasks[card["id"]]["status"] = "done"
            self.board.runs[card["id"]] = [{"id": run_id, "profile": profile,
                "status": "completed", "outcome": "completed"}]
        original = out / "REVIEW.json"
        original.write_text('{not valid json\n')
        (out / "QA.json").write_text("{}")
        state.update({"stage": "verification", "index": 0, "items": [{
            "definition": definition, "round": 1, "card": "implementation-card",
            "workspace": str(item_workspace), "base_sha": self.base_sha,
            "head_sha": self.base_sha, "status": "verification",
            "reviewer_card": reviewer["id"], "qa_card": qa["id"],
            "reviewer_workspace": str(item_workspace), "qa_workspace": str(item_workspace),
        }]})

        self.controller.tick(state)

        item = state["items"][0]
        retry_id = item["reviewer_card"]
        self.assertNotEqual(retry_id, reviewer["id"], state.get("blocker"))
        self.assertEqual(self.board.tasks[retry_id]["assignee"], "team-reviewer")
        self.assertEqual(self.board.tasks[retry_id]["parents"], [reviewer["id"]])
        self.assertEqual(item["qa_card"], qa["id"], "only the malformed report's role is retried")
        archived = out / "report-attempts/reviewer/source/attempt-1.json"
        self.assertEqual(archived.read_text(), original.read_text())
        self.assertEqual(Path(item["reviewer_report_path"]).name, "attempt-2.json")
        self.assertEqual(item["head_sha"], self.base_sha)
        self.assertEqual(item["report_retries"], {"reviewer": 1})
        self.assertEqual(state["stage"], "verification")

        current_path = Path(item["reviewer_report_path"])
        current_path.write_text('{"still_invalid": true}\n')
        current_card = item["reviewer_card"]
        self.board.runs[current_card] = [{"id": 503, "profile": "team-reviewer",
            "status": "completed", "outcome": "completed"}]
        self.controller.retry_invalid_review_report(state, item, "reviewer", "second bad report")
        archived_retry = out / "report-attempts/reviewer/source/attempt-2.json"
        self.assertEqual(archived_retry.read_text(), '{"still_invalid": true}\n')
        self.assertEqual(Path(item["reviewer_report_path"]).name, "attempt-3.json")
        self.assertEqual(item["report_retries"]["reviewer"], 2)

    def test_invalid_report_retry_refuses_wrong_actor_review_workspace(self):
        state = self.submit()
        workspace = self.controller.worktree(state, "reviewer-check", self.base_sha)
        card = self.board.create_task("review", "team-reviewer", workspace_path=str(workspace))
        self.board.runs[card["id"]] = [{"id": 601, "profile": "team-other",
            "status": "completed", "outcome": "completed"}]
        item = {"definition": {"id": "task-a", "title": "Review target"}, "round": 1,
                "card": "implementation-card", "base_sha": self.base_sha,
                "head_sha": self.base_sha, "reviewer_card": card["id"],
                "reviewer_workspace": str(workspace)}
        with self.assertRaisesRegex(ContractError, "successful reviewer run"):
            self.controller.retry_invalid_review_report(state, item, "reviewer", "bad JSON")

        self.assertEqual(item["reviewer_card"], card["id"])
        self.assertFalse((self.root / "data/runs/feature-1/tasks/task-a/round-1/report-attempts")
                         .exists())

    def test_review_report_shape_rejects_pass_with_nonzero_exit_and_bad_supplemental(self):
        item = {"card": "implementation-card", "base_sha": self.base_sha,
                "head_sha": self.base_sha}
        report = {"schema_version": 1, "task_id": "implementation-card", "role": "qa",
                  "assignee": "team-qa", "run_id": "12", "verdict": "pass",
                  "base_sha": self.base_sha, "head_sha": self.base_sha,
                  "findings": [], "checks": [{"name": "unit tests", "status": "pass",
                                                "exit_code": 1, "evidence": "contradictory"}]}
        with self.assertRaisesRegex(ContractError, "says pass but has non-zero exit_code"):
            self.controller._validate_review_report_shape(report, "qa", item)

        report["checks"][0]["exit_code"] = 0
        report["supplemental_checks"] = [{"kind": "mutation_killed", "exit_code": 124}]
        with self.assertRaisesRegex(ContractError, "missing fields"):
            self.controller._validate_review_report_shape(report, "qa", item)

    def test_qa_report_requires_exact_pinned_plan_binding_for_deferred_checks(self):
        checks = [
            {"name": "quick-unit", "argv": ["pytest", "tests/unit"]},
            {"name": "full-regression", "argv": ["pytest", "tests"]},
        ]
        policy = {"version": 1, "path_rules": [
            {"name": "backend", "prefixes": ["backend"], "quick_check_indexes": [0]},
        ]}
        plan = verification_plan(checks, policy, ["backend/app.py"])
        self.assertEqual(plan["mode"], "quick")
        item = {"card": "implementation-card", "base_sha": self.base_sha,
                "head_sha": self.base_sha, "verification_plan": plan}
        report = {"schema_version": 1, "task_id": item["card"], "role": "qa",
                  "assignee": "team-qa", "run_id": "120", "verdict": "pass",
                  "base_sha": self.base_sha, "head_sha": self.base_sha,
                  "findings": [], "checks": [{"name": "quick-unit", "status": "pass",
                      "command": ["pytest", "tests/unit"], "exit_code": 0,
                      "evidence": "unit tests passed"}],
                  "deferred": {"evidence_ref": "VERIFICATION-PLAN.json",
                      "policy_sha256": plan["policy_sha256"],
                      "checks": plan["deferred_checks"]}}

        self.controller._validate_review_report_shape(report, "qa", item)

        invalid = [
            ("aggregate skip", {**report, "checks": [*report["checks"],
                {"name": "full regression (indexes 0,1)", "status": "skip",
                 "evidence": "delegated to controller"}]}, "cannot contain skip rows"),
            ("missing deferred", {key: value for key, value in report.items() if key != "deferred"},
             "need exact VERIFICATION-PLAN.json bindings"),
            ("wrong digest", {**report, "deferred": {**report["deferred"],
                 "policy_sha256": "0" * 64}}, "digest does not match"),
            ("partial deferred", {**report, "deferred": {**report["deferred"],
                 "checks": plan["deferred_checks"][:0]}}, "exactly match every pinned deferred"),
            ("forged argv", {**report, "deferred": {**report["deferred"],
                 "checks": [{**plan["deferred_checks"][0], "argv": ["false"]}]}},
             "exactly match every pinned deferred"),
            ("wrong selected argv", {**report, "checks": [{**report["checks"][0],
                 "command": ["pytest", "tests"]}]}, "exactly one executed check"),
        ]
        for label, candidate, message in invalid:
            with self.subTest(label=label), self.assertRaisesRegex(ContractError, message):
                self.controller._validate_review_report_shape(candidate, "qa", item)

        failed = {**report, "checks": [{**report["checks"][0], "status": "fail",
                                          "exit_code": 1, "evidence": "assertion failed"}]}
        # An actual failed command is a valid QA result and must reach the
        # normal developer rework path, rather than being repaired as JSON.
        self.controller._validate_review_report_shape(failed, "qa", item)

        blocked = {**report, "verdict": "blocked", "checks": [{
            "name": "quick-unit", "status": "skip", "evidence": "required database unavailable"}]}
        # A real environment blocker may prevent even the planned command from
        # starting. Preserve this report as a blocker instead of creating a
        # format-repair loop that asks QA to invent unavailable evidence.
        self.controller._validate_review_report_shape(blocked, "qa", item)

    def test_integrator_deferred_gate_requires_exact_pinned_deferred_checks(self):
        checks = [
            {"name": "backend-unit", "argv": ["pytest", "tests/backend"]},
            {"name": "frontend-unit", "argv": ["npm", "test"]},
            {"name": "compose-check", "argv": ["docker", "compose", "config"]},
            {"name": "full-integration", "argv": ["pytest", "tests/integration"]},
        ]
        policy = {"version": 1, "full_after_tasks": 2, "path_rules": [
            {"name": "backend", "prefixes": ["backend"], "quick_check_indexes": [0]},
            {"name": "compose", "prefixes": ["compose.yaml"], "quick_check_indexes": [2]},
        ]}
        plan = verification_plan(checks, policy, ["backend/app.py", "compose.yaml"])
        self.assertEqual(plan["mode"], "quick")
        self.assertEqual([row["index"] for row in plan["selected_checks"]], [0, 2])
        self.assertEqual([row["index"] for row in plan["deferred_checks"]], [1, 3])
        deferred_gate = {"name": "deferred_optional_suites", "status": "deferred",
                         "evidence_ref": "VERIFICATION-PLAN.json",
                         "policy_sha256": plan["policy_sha256"],
                         "deferred_checks": plan["deferred_checks"]}
        report = {"gates": [{"name": "scope_and_review", "status": "pass"}, deferred_gate],
                  "blocking_items": [], "findings": []}

        self.controller._validate_integrator_gates(report, plan)

        invalid = [
            ("unknown index", {**deferred_gate,
             "deferred_checks": [{**plan["deferred_checks"][0], "index": 99}]}, "non-deferred"),
            ("wrong argv", {**deferred_gate,
             "deferred_checks": [{**plan["deferred_checks"][0], "argv": ["forged"]}]}, "does not match"),
            ("missing binding", {key: value for key, value in deferred_gate.items()
                                  if key != "deferred_checks"}, "exact deferred_checks"),
            ("wrong digest", {**deferred_gate, "policy_sha256": "0" * 64}, "digest"),
            ("wrong evidence ref", {**deferred_gate, "evidence_ref": "some prose about deferred checks"},
             "reference VERIFICATION-PLAN.json exactly"),
            ("duplicate check", {**deferred_gate,
             "deferred_checks": [plan["deferred_checks"][0], plan["deferred_checks"][0]]}, "duplicate check index"),
            ("required quick check", {**deferred_gate,
             "deferred_checks": [plan["selected_checks"][0]]}, "non-deferred"),
            ("pass with deferred metadata", {**deferred_gate, "status": "pass"},
             "cannot carry deferred-check bindings"),
        ]
        for label, gate, message in invalid:
            with self.subTest(label=label), self.assertRaisesRegex(ContractError, message):
                self.controller._validate_integrator_gates(
                    {**report, "gates": [gate]}, plan)

        full_plan = verification_plan(checks, policy, ["backend/app.py", "compose.yaml"],
                                      force_full=True)
        with self.assertRaisesRegex(ContractError, "outside quick mode"):
            self.controller._validate_integrator_gates(
                {**report, "gates": [deferred_gate]}, full_plan)
        for status in ("fail", "blocked", "skip", "other"):
            with self.subTest(status=status), self.assertRaisesRegex(ContractError, "unsupported status"):
                self.controller._validate_integrator_gates(
                    {**report, "gates": [{"name": "bad", "status": status}]}, plan)
        with self.assertRaisesRegex(ContractError, "non-empty array"):
            self.controller._validate_integrator_gates({**report, "gates": []}, plan)
        with self.assertRaisesRegex(ContractError, "blocking_items"):
            self.controller._validate_integrator_gates({**report, "blocking_items": ["open risk"]}, plan)
        with self.assertRaisesRegex(ContractError, "negative findings"):
            self.controller._validate_integrator_gates({**report, "findings": ["negative"]}, plan)
        with self.assertRaisesRegex(ContractError, "cover every deferred check"):
            self.controller._validate_integrator_gates(
                {**report, "gates": [{**deferred_gate,
                 "deferred_checks": [plan["deferred_checks"][0]]}]}, plan)
        duplicate_across_gates = [
            {**deferred_gate, "name": "deferred_a",
             "deferred_checks": [plan["deferred_checks"][0]]},
            {**deferred_gate, "name": "deferred_b",
             "deferred_checks": [plan["deferred_checks"][0], plan["deferred_checks"][1]]},
        ]
        with self.assertRaisesRegex(ContractError, "repeated across gates"):
            self.controller._validate_integrator_gates({**report, "gates": duplicate_across_gates}, plan)

    def test_integrator_retry_keeps_old_report_and_uses_fresh_attempt_paths(self):
        state = self.submit()
        item = {"definition": {"id": "task-a", "title": "Safe behavior"}, "round": 1,
                "card": "developer-card", "base_sha": self.base_sha, "head_sha": self.base_sha,
                "reviewer_card": "reviewer-card", "qa_card": "qa-card",
                "integrator_card": "old-integrator-card", "status": "integration_review"}
        out = self.controller.round_path(state, item)
        old_report = out / "INTEGRATION.json"
        old_report.write_text('{"old": true}\n')
        self.board.runs["old-integrator-card"] = [{"id": "100", "profile": "team-integrator",
                                                    "status": "done", "outcome": "completed"}]
        with patch.object(self.controller, "read_evidence", return_value=({"verdict": "pass"}, {"verdict": "pass"})), \
             patch.object(self.controller, "validate_pair"):
            self.controller.retry_integrator(state, item, "Старая попытка не приложила план проверок")

        fresh_report = Path(item["integrator_report_path"])
        self.assertEqual(old_report.read_text(), '{"old": true}\n')
        self.assertEqual(fresh_report.name, "attempt-1.json")
        self.assertNotEqual(fresh_report, old_report)
        self.assertEqual(item["integrator_attempts"][0]["card"], "old-integrator-card")
        self.assertEqual(item["integrator_card"], self.board.tasks[item["integrator_card"]]["id"])
        self.assertTrue(self.board.tasks[item["integrator_card"]]["idempotency_key"].endswith(":retry-1"))
        retry_archive = Path(item["integrator_attempts"][0]["archive_path"])
        retry_prompt = self.board.tasks[item["integrator_card"]]["body"]
        self.assertEqual(retry_archive.read_text(), '{"old": true}\n')
        self.assertIn(hashlib.sha256(retry_archive.read_bytes()).hexdigest(), retry_prompt)
        self.assertIn(self.base_sha, retry_prompt)
        self.assertIn("run_id=100", retry_prompt)
        self.assertIn("Старая попытка не приложила план проверок", retry_prompt)
        self.assertIn("не перезапускай QA и долгие regression suites", retry_prompt)
        self.assertIn("сохрани все реальные блокирующие замечания", retry_prompt)
        self.assertIn("Неблокирующие info/low notes перенеси в accepted_risks", retry_prompt)
        with patch.object(self.controller, "read_evidence", return_value=({"verdict": "pass"}, {"verdict": "pass"})), \
             patch.object(self.controller, "validate_pair"):
            self.controller.retry_integrator(state, item, "Первая новая попытка всё ещё неверна")
        second_report = Path(item["integrator_report_path"])
        self.assertEqual(second_report.name, "attempt-2.json")
        self.assertNotEqual(second_report, fresh_report)
        with self.assertRaisesRegex(ContractError, "retry limit reached"):
            self.controller.retry_integrator(state, item, "Третья попытка запрещена")

    def test_new_candidate_round_resets_integrator_retry_path_without_touching_old_report(self):
        state = self.submit()
        item = {"definition": {"id": "task-a", "title": "Safe behavior",
                                "owned_paths": ["README.md"]}, "round": 1,
                "card": "developer-card", "base_sha": self.base_sha, "head_sha": self.base_sha,
                "workspace": str(self.repo), "branch": "codex/test-round-2",
                "reviewer_card": "reviewer-card", "qa_card": "qa-card",
                "integrator_card": "old-integrator-card", "status": "integration_review"}
        round1 = self.controller.round_path(state, item)
        old_integrator_report = round1 / "INTEGRATION.json"
        old_integrator_report.write_text('{"round": 1, "attempt": 1}\n')
        with patch.object(self.controller, "read_evidence", return_value=({"verdict": "pass"}, {"verdict": "pass"})), \
             patch.object(self.controller, "validate_pair"):
            self.controller.retry_integrator(state, item, "Повтор первой интеграционной проверки")
        round1_retry_report = Path(item["integrator_report_path"])
        round1_retry_report.write_text('{"round": 1, "attempt": 2}\n')
        round1_retry_bytes = round1_retry_report.read_bytes()

        item["round"] = 2
        item["status"] = "implementation"
        item["card"] = "developer-round-2"
        round2 = self.controller.round_path(state, item)
        (round2 / "IMPLEMENTATION.md").write_text("Round two candidate implementation details.\n" * 5)
        item["workspace"] = str(self.repo)
        fake_candidate = type("Candidate", (), {"head_sha": self.base_sha,
                                                  "changed_paths": ["README.md"]})()
        with patch("team_engine.controller.validate_candidate", return_value=fake_candidate):
            self.controller.start_review(state, item)

        self.assertEqual([attempt["round"] for attempt in item["integrator_attempts"]], [1, 1])
        self.assertNotIn("integrator_report_path", item)
        self.assertEqual(round1_retry_report.read_bytes(), round1_retry_bytes)
        item.update({"status": "integration_review", "reviewer_card": "reviewer-card-2",
                     "qa_card": "qa-card-2"})
        with patch.object(self.controller, "read_evidence", return_value=({"verdict": "pass"}, {"verdict": "pass"})), \
             patch.object(self.controller, "validate_pair"):
            self.controller.start_integrator(state, item)
        self.assertEqual(Path(item["integrator_report_path"]), round2 / "INTEGRATION.json")
        self.assertEqual(round1_retry_report.read_bytes(), round1_retry_bytes)

    def test_failed_qa_check_still_takes_developer_rework_path(self):
        state = self.submit()
        item = {"definition": {"id": "task-a", "title": "Safe behavior",
                                "assignee_role": "backend"},
                "round": 1, "card": "implementation-card", "base_sha": self.base_sha,
                "head_sha": self.base_sha}
        reviewer = {"schema_version": 1, "task_id": item["card"], "role": "reviewer",
                    "assignee": "team-reviewer", "run_id": "91", "verdict": "pass",
                    "base_sha": self.base_sha, "head_sha": self.base_sha, "findings": [],
                    "checks": [{"name": "review", "status": "pass", "exit_code": 0,
                                "evidence": "review complete"}]}
        for verdict, expected_reason in (("pass", "did not pass"),
                                         ("changes_requested", "changes_requested")):
            with self.subTest(verdict=verdict):
                qa = {"schema_version": 1, "task_id": item["card"], "role": "qa",
                      "assignee": "team-qa", "run_id": "92", "verdict": verdict,
                      "base_sha": self.base_sha, "head_sha": self.base_sha, "findings": [],
                      "checks": [{"name": "mandatory test", "status": "fail", "exit_code": 1,
                                  "evidence": "assertion failed"}]}
                # A real failed check is a valid report shape and reaches the
                # promotion gate, which returns it to the developer for rework.
                self.controller._validate_review_report_shape(qa, "qa", item)
                with patch.object(self.controller, "read_evidence", return_value=(reviewer, qa)), \
                     patch.object(self.controller, "rework") as rework:
                    self.controller.start_integrator(state, item)

                rework.assert_called_once()
                self.assertIn(expected_reason, rework.call_args.args[2])
                self.assertEqual(item["round"], 1)

    def test_qa_report_retry_prompt_preserves_mutant_provenance_and_avoids_suite_rerun(self):
        state = self.submit()
        workspace = self.controller.worktree(state, "qa-format-retry", self.base_sha)
        card = self.board.create_task("qa", "team-qa", workspace_path=str(workspace))
        self.board.runs[card["id"]] = [{"id": 903, "profile": "team-qa",
                                         "status": "completed", "outcome": "completed"}]
        out = self.root / "data/runs/feature-1/tasks/task-a/round-1"
        out.mkdir(parents=True)
        report_path = out / "QA.json"
        report_path.write_text(json.dumps({"checks": [{
            "name": "AC-T001-2 sensitivity: mutants on check order",
            "status": "pass", "command": ["./gradlew", ":backend:test"], "exit_code": 1,
            "evidence": "run61 scratch /tmp/qa-t001-r3-run61; M1 log qa-logs/qa-mutant-m1.log; "
                        "M2 log qa-logs/qa-mutant-m2.log; scratch worktree clean after revert"
        }]}))
        item = {"definition": {"id": "task-a", "title": "Safe behavior"}, "round": 1,
                "card": "implementation-card", "base_sha": self.base_sha, "head_sha": self.base_sha,
                "qa_card": card["id"], "qa_workspace": str(workspace),
                "qa_report_path": str(report_path)}

        self.controller.retry_invalid_review_report(
            state, item, "qa", "QA check says pass but has non-zero exit_code"
        )

        prompt = self.board.tasks[item["qa_card"]]["body"]
        for expected in ("supplemental_checks", "kind:mutation_killed", "head_sha",
                         "run61", "/tmp/qa-t001-r3-run61", "qa-logs/qa-mutant-m1.log",
                         "qa-logs/qa-mutant-m2.log", "M1", "M2",
                         "Отдельная supplemental_checks-запись для каждого обнаруженного mutant_id",
                         "сохраняй их фактические status/exit_code", "Полный suite повторно не запускай"):
            self.assertIn(expected, prompt)
        self.assertIn("текущим HERMES_KANBAN_RUN_ID", prompt)

    def test_plan_review_wrong_package_hash_retries_only_that_role_before_judge(self):
        state = self.submit()
        run_dir = self.root / "data/runs/feature-1"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(exist_ok=True)
        for name, content in (("SPEC.md", "a complete requirements specification"),
                              ("DESIGN.md", "a complete architecture design"),
                              ("PLAN.json", '{"tasks":[]}')):
            (artifacts / name).write_text(content)
        review_dir = run_dir / "analysis-round-1"
        review_dir.mkdir()
        for name in ("SPEC.md", "DESIGN.md", "PLAN.json"):
            (review_dir / name).write_bytes((artifacts / name).read_bytes())
        state["plan_sha256"] = self.controller.plan_digest(state)
        state["plan_review_path"] = str(review_dir)
        state["plan_round"] = 1
        workspace = self.controller.worktree(state, "plan-review-test", self.base_sha)
        state["stage"] = "plan_challenge"
        for role, run_id in (("skeptic", 701), ("test-designer", 702)):
            card = self.board.create_task("plan review", "team-" + role,
                                          idempotency_key="plan-" + role)
            state[role + "_card"] = card["id"]
            state[role + "_workspace"] = str(workspace)
            self.board.tasks[card["id"]]["status"] = "done"
            self.board.runs[card["id"]] = [{"id": run_id, "profile": "team-" + role,
                "status": "completed", "outcome": "completed"}]
            reported_hash = state["plan_sha256"]
            if role == "test-designer":
                # Reproduce the common bug: hashing PLAN.json alone instead of
                # the exact SPEC+DESIGN+PLAN package reviewed by the agent.
                import hashlib
                reported_hash = hashlib.sha256((artifacts / "PLAN.json").read_bytes()).hexdigest()
            report = {"schema_version": 1, "feature_id": state["id"],
                      "plan_sha256": reported_hash, "role": role,
                      "assignee": "team-" + role, "run_id": str(run_id),
                      "verdict": "pass", "findings": []}
            report_path = review_dir / (role + ".json")
            report_path.write_text(json.dumps(report))
            state[role + "_report_path"] = str(report_path)

        original_test_designer = state["test-designer_report_path"]
        original_content = Path(original_test_designer).read_text()
        old_test_designer_card = state["test-designer_card"]
        old_skeptic_card = state["skeptic_card"]
        self.controller.tick(state)

        self.assertEqual(state["stage"], "plan_challenge")
        self.assertEqual(state["skeptic_card"], old_skeptic_card)
        self.assertNotEqual(state["test-designer_card"], old_test_designer_card)
        self.assertEqual(self.board.tasks[state["test-designer_card"]]["assignee"], "team-test-designer")
        self.assertEqual(self.board.tasks[state["test-designer_card"]]["parents"], [old_test_designer_card])
        self.assertEqual(Path(review_dir / "report-attempts/test-designer/source/attempt-1.json").read_text(),
                         original_content)
        self.assertEqual(Path(state["test-designer_report_path"]).name, "attempt-2.json")
        self.assertEqual(state["plan_report_retries"], {"test-designer": 1})
        self.assertNotIn("judge_card", state)

        retry_card = state["test-designer_card"]
        retry_run = 703
        self.board.tasks[retry_card]["status"] = "done"
        self.board.runs[retry_card] = [{"id": retry_run, "profile": "team-test-designer",
            "status": "completed", "outcome": "completed"}]
        report = {"schema_version": 1, "feature_id": state["id"],
                  "plan_sha256": state["plan_sha256"], "role": "test-designer",
                  "assignee": "team-test-designer", "run_id": str(retry_run),
                  "verdict": "pass", "findings": []}
        Path(state["test-designer_report_path"]).write_text(json.dumps(report))

        self.controller.tick(state)

        self.assertEqual(state["stage"], "plan_judgment")
        judge_prompt = self.board.tasks[state["judge_card"]]["body"]
        self.assertIn(state["skeptic_report_path"], judge_prompt)
        self.assertIn(state["test-designer_report_path"], judge_prompt)
        self.assertIn("ровно эти указанные пути", judge_prompt)

    def test_plan_report_retry_does_not_mask_changed_snapshot(self):
        state = self.submit()
        run_dir = self.root / "data/runs/feature-1"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(exist_ok=True)
        for name, content in (("SPEC.md", "complete spec"), ("DESIGN.md", "complete design"),
                              ("PLAN.json", "{}")):
            (artifacts / name).write_text(content)
        review_dir = run_dir / "analysis-round-1"
        review_dir.mkdir()
        for name in ("SPEC.md", "DESIGN.md", "PLAN.json"):
            (review_dir / name).write_bytes((artifacts / name).read_bytes())
        state["plan_sha256"] = self.controller.plan_digest(state)
        state["plan_review_path"] = str(review_dir)
        workspace = self.controller.worktree(state, "plan-review-dirty", self.base_sha)
        (review_dir / "SPEC.md").write_text("changed after reviewers started")
        card = self.board.create_task("plan review", "team-test-designer",
                                      idempotency_key="plan-designer-dirty")
        self.board.runs[card["id"]] = [{"id": 801, "profile": "team-test-designer",
            "status": "completed", "outcome": "completed"}]
        state.update({"test-designer_card": card["id"], "test-designer_workspace": str(workspace)})
        with self.assertRaisesRegex(ContractError, "snapshot changed"):
            self.controller.retry_invalid_plan_report(state, "test-designer", "wrong package hash")
        self.assertEqual(state["test-designer_card"], card["id"])

    def test_new_plan_round_resets_report_paths_and_retry_counts(self):
        state = self.submit()
        run_dir = self.root / "data/runs/feature-1"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(exist_ok=True)
        for name, content in (("SPEC.md", "spec"), ("DESIGN.md", "design"), ("PLAN.json", "{}")):
            (artifacts / name).write_text(content)
        self.controller.worktree(state, "planning", self.base_sha)
        state["planner_card"] = self.board.create_task(
            "planner", "team-planner", idempotency_key="planner-round-2")["id"]
        state["plan_round"] = 2
        state["plan_report_retries"] = {"skeptic": 1, "test-designer": 1}
        state["skeptic_report_path"] = "/old/round/skeptic.json"
        state["test-designer_report_path"] = "/old/round/test-designer.json"

        self.controller.challenge_plan(state)

        review_dir = run_dir / "analysis-round-2"
        self.assertEqual(state["plan_report_retries"], {})
        self.assertEqual(Path(state["skeptic_report_path"]), review_dir / "skeptic.json")
        self.assertEqual(Path(state["test-designer_report_path"]), review_dir / "test-designer.json")

    def test_new_code_review_round_resets_report_paths_and_retry_counts(self):
        state = self.submit()
        workspace = self.controller.worktree(state, "task-rework", self.base_sha, branch="feature-rework")
        (workspace / "README.md").write_text("candidate\n")
        git(workspace, "add", "README.md")
        git(workspace, "commit", "-m", "candidate change")
        item = {"definition": {"id": "task-a", "title": "Safe behavior",
                               "owned_paths": ["README.md"], "test_plan": [],
                               "acceptance_criteria": [], "priority": "P1",
                               "priority_reason": "Needed", "dependencies": [],
                               "assignee_role": "backend"},
                "round": 2, "branch": "feature-rework", "base_sha": self.base_sha,
                "workspace": str(workspace), "card": "developer-round-2",
                "report_retries": {"reviewer": 1, "qa": 1},
                "reviewer_report_path": "/old/round/REVIEW.json",
                "qa_report_path": "/old/round/QA.json"}
        output = self.controller.round_path(state, item)
        (output / "IMPLEMENTATION.md").write_text("implementation evidence\n" * 10)

        self.controller.start_review(state, item)

        self.assertEqual(item["report_retries"], {})
        self.assertEqual(Path(item["reviewer_report_path"]), output / "REVIEW.json")
        self.assertEqual(Path(item["qa_report_path"]), output / "QA.json")

    def test_second_plan_report_retry_archives_current_output_without_self_copy(self):
        state = self.submit()
        self.controller.config["max_plan_report_retries"] = 2
        run_dir = self.root / "data/runs/feature-1"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(exist_ok=True)
        for name, content in (("SPEC.md", "complete spec"), ("DESIGN.md", "complete design"),
                              ("PLAN.json", "{}")):
            (artifacts / name).write_text(content)
        review_dir = run_dir / "analysis-round-1"
        review_dir.mkdir()
        for name in ("SPEC.md", "DESIGN.md", "PLAN.json"):
            (review_dir / name).write_bytes((artifacts / name).read_bytes())
        state.update({"plan_sha256": self.controller.plan_digest(state),
                      "plan_review_path": str(review_dir), "plan_round": 1})
        workspace = self.controller.worktree(state, "plan-report-repeat", self.base_sha)
        card = self.board.create_task("plan review", "team-test-designer", idempotency_key="repeat-1")
        state.update({"test-designer_card": card["id"], "test-designer_workspace": str(workspace),
                      "test-designer_report_path": str(review_dir / "test-designer.json")})
        self.board.runs[card["id"]] = [{"id": 901, "profile": "team-test-designer",
            "status": "completed", "outcome": "completed"}]
        self.controller.retry_invalid_plan_report(state, "test-designer", "first invalid report")

        current_path = Path(state["test-designer_report_path"])
        current_path.write_text('{"invalid": true}\n')
        current_card = state["test-designer_card"]
        self.board.runs[current_card] = [{"id": 902, "profile": "team-test-designer",
            "status": "completed", "outcome": "completed"}]
        self.controller.retry_invalid_plan_report(state, "test-designer", "second invalid report")

        archived = review_dir / "report-attempts/test-designer/source/attempt-2.json"
        self.assertEqual(archived.read_text(), '{"invalid": true}\n')
        self.assertEqual(Path(state["test-designer_report_path"]).name, "attempt-3.json")
        self.assertEqual(state["plan_report_retries"]["test-designer"], 2)

    def test_integrator_prompt_uses_current_review_and_qa_attempt_paths(self):
        state = self.submit()
        workspace = self.controller.worktree(state, "integrator-current-paths", self.base_sha)
        item = {"definition": {"id": "task-a", "title": "Safe behavior"}, "round": 1,
                "card": "developer-card", "base_sha": self.base_sha, "head_sha": self.base_sha,
                "workspace": str(workspace), "reviewer_card": "reviewer-card", "qa_card": "qa-card",
                "reviewer_report_path": str(self.root / "current-review-attempt.json"),
                "qa_report_path": str(self.root / "current-qa-attempt.json")}
        with patch.object(self.controller, "read_evidence", return_value=({}, {})), \
             patch.object(self.controller, "validate_pair"):
            self.controller.start_integrator(state, item)

        prompt = self.board.tasks[item["integrator_card"]]["body"]
        self.assertIn(item["reviewer_report_path"], prompt)
        self.assertIn(item["qa_report_path"], prompt)
        self.assertIn("ровно эти пути", prompt)
        self.assertIn("при decision=ready_for_owner и полностью pass gates оба массива должны быть пустыми", prompt)
        self.assertIn("в существующем поле accepted_risks", prompt)
        self.assertIn("не переклассифицируй реальный блокер как advisory", prompt)

    def test_blocked_verification_pauses_without_developer_rework(self):
        state = self.submit()
        item = {"definition": {"id": "task-a", "title": "Safe behavior"},
                "round": 1, "card": "developer-card", "base_sha": self.base_sha,
                "head_sha": self.base_sha, "workspace": str(self.repo),
                "reviewer_card": "reviewer-card", "qa_card": "qa-card"}
        review = {"verdict": "blocked"}
        qa = {"verdict": "pass"}
        with patch.object(self.controller, "read_evidence", return_value=(review, qa)), \
             patch.object(self.controller, "rework") as rework:
            self.controller.start_integrator(state, item)

        self.assertEqual(state["stage"], "blocked")
        self.assertEqual(state["resume_stage"], "verification")
        self.assertEqual(item["round"], 1)
        self.assertEqual(item["verification_blocked_roles"], ["reviewer"])
        self.assertIn("не смогла подтвердить", state["blocker"])
        self.assertIn("--retry-workers", state["blocker"])
        rework.assert_not_called()

    def test_blocked_qa_with_unavailable_selected_checks_is_not_format_repaired(self):
        checks = [
            {"name": "npm ci", "argv": ["npm", "ci"]},
            {"name": "lint", "argv": ["npm", "run", "lint"]},
            {"name": "build", "argv": ["npm", "run", "build"]},
        ]
        policy = {"version": 1, "path_rules": [
            {"name": "frontend", "prefixes": ["frontend"],
             "quick_check_indexes": [0, 1, 2]},
        ]}
        plan = verification_plan(checks, policy, ["frontend/App.tsx"])
        self.assertEqual(plan["mode"], "quick")
        item = {"definition": {"id": "task-blocked", "title": "Frontend QA",
                               "assignee_role": "backend"},
                "round": 1, "card": "developer-card", "base_sha": self.base_sha,
                "head_sha": self.base_sha, "workspace": str(self.repo),
                "verification_plan": plan}
        qa = {"schema_version": 1, "task_id": item["card"], "role": "qa",
              "assignee": "team-qa", "run_id": "77", "verdict": "blocked",
              "base_sha": self.base_sha, "head_sha": self.base_sha, "findings": [],
              "checks": [
                  {"name": "npm ci", "status": "fail", "command": ["npm", "ci"],
                   "exit_code": 1, "evidence": "registry unavailable"},
                  {"name": "lint", "status": "skip", "evidence": "dependency install failed"},
                  {"name": "build", "status": "skip", "evidence": "dependency install failed"},
              ]}

        # This is a real environment blocker, not a malformed pass report.
        self.controller._validate_review_report_shape(qa, "qa", item)
        state = self.submit()
        item.update({"definition": {"id": "task-blocked", "title": "Frontend QA",
                                    "assignee_role": "backend"},
                     "reviewer_card": "reviewer-card", "qa_card": "qa-card"})
        review = {"verdict": "pass", "findings": [], "checks": []}
        with patch.object(self.controller, "read_evidence", return_value=(review, qa)), \
             patch.object(self.controller, "rework") as rework:
            self.controller.start_integrator(state, item)

        self.assertEqual(state["stage"], "blocked")
        self.assertEqual(item["verification_blocked_roles"], ["qa"])
        rework.assert_not_called()

    def test_blocker_message_ignores_nonblocking_notes_and_explains_skipped_check(self):
        state = {"id": "feature-1", "root_card": self.board.machine_card("root")["id"]}
        self.controller.path(state).mkdir(parents=True)
        evidence = {"verdict": "blocked", "findings": [
            {"blocking": False, "description": "Info note one"},
            {"blocking": False, "description": "Info note two"},
        ], "checks": [{"name": "frontend-vitest", "status": "skip",
                       "evidence": "run budget expired before execution"}]}

        self.controller.block_verification(
            state, {"definition": {"id": "T-004"}}, ["reviewer"], {"reviewer": evidence})

        self.assertIn("frontend-vitest (skip): run budget expired before execution", state["blocker"])
        self.assertNotIn("Info note", state["blocker"])

    def test_blocker_message_prioritizes_blocking_finding_before_failed_check(self):
        state = {"id": "feature-1", "root_card": self.board.machine_card("root")["id"]}
        self.controller.path(state).mkdir(parents=True)
        evidence = {"verdict": "blocked", "findings": [
            {"blocking": False, "description": "Informational note"},
            {"blocking": True, "description": "Required scenario is unverified"},
        ], "checks": [{"name": "unit", "status": "fail", "evidence": "assertion failed"}]}

        self.controller.block_verification(
            state, {"definition": {"id": "T-004"}}, ["reviewer"], {"reviewer": evidence})

        self.assertLess(state["blocker"].index("Required scenario is unverified"),
                        state["blocker"].index("unit (fail): assertion failed"))
        self.assertNotIn("Informational note", state["blocker"])

    def test_blocker_message_uses_failed_environment_check_without_findings(self):
        state = {"id": "feature-1", "root_card": self.board.machine_card("root")["id"]}
        self.controller.path(state).mkdir(parents=True)
        evidence = {"verdict": "blocked", "findings": [],
                    "checks": [{"name": "postgres-smoke", "status": "fail",
                                "evidence": "could not connect to test database"}]}

        self.controller.block_verification(
            state, {"definition": {"id": "T-004"}}, ["qa"], {"qa": evidence})

        self.assertIn("postgres-smoke (fail): could not connect to test database", state["blocker"])

    def test_blocked_reviewer_retry_reuses_only_hash_bound_matching_qa_evidence(self):
        quick = [sys.executable, "-c", "print('quick')"]
        full = [sys.executable, "-c", "print('full')"]
        self.controller.config["projects"]["demo"].update({
            "checks": [{"name": "frontend-vitest", "argv": quick},
                       {"name": "full-regression", "argv": full}],
            "execution_policy": {"version": 1, "full_after_tasks": 2,
                                 "path_rules": [{"name": "frontend", "prefixes": ["frontend"],
                                                 "quick_check_indexes": [0]}]},
        })
        state = {"id": "feature-retry", "project": "demo", "stage": "blocked",
                 "initial_base_sha": self.base_sha, "items": []}
        item = {"definition": {"id": "T-001", "title": "Frontend change"}, "round": 1,
                "workspace": str(self.repo), "head_sha": self.base_sha,
                "base_sha": self.base_sha, "card": "parent-card", "reviewer_card": "old-reviewer",
                "qa_card": "old-qa", "candidate_changed_paths": ["frontend/app.tsx"],
                "verification_blocked_roles": ["reviewer"]}
        state["items"].append(item)
        state["index"] = 0
        round_dir = self.controller.round_path(state, item)
        qa_path = round_dir / "QA.json"
        self.board.runs["old-qa"] = [{"id": "42", "profile": "team-qa",
                                     "status": "done", "outcome": "completed"}]
        qa_report = {"schema_version": 1, "task_id": "parent-card", "role": "qa",
                     "assignee": "team-qa", "run_id": "42", "verdict": "pass",
                     "base_sha": self.base_sha, "head_sha": self.base_sha,
                     "findings": [], "checks": [{"name": "frontend-vitest", "status": "pass",
                                                   "command": quick, "exit_code": 0,
                                                   "evidence": "passed on candidate SHA"}]}
        qa_bytes = (json.dumps(qa_report) + "\n").encode()
        qa_path.write_bytes(qa_bytes)
        item["qa_report_path"] = str(qa_path)
        item["verification_plan"] = verification_plan(
            self.controller.config["projects"]["demo"]["checks"],
            self.controller.config["projects"]["demo"]["execution_policy"],
            item["candidate_changed_paths"])
        qa_report["deferred"] = {"evidence_ref": "VERIFICATION-PLAN.json",
                                  "policy_sha256": item["verification_plan"]["policy_sha256"],
                                  "checks": item["verification_plan"]["deferred_checks"]}
        qa_bytes = (json.dumps(qa_report) + "\n").encode()
        qa_path.write_bytes(qa_bytes)
        spawned = {}

        def capture_spawn(_state, role, _title, body, workspace, _key, parents=None):
            spawned.update(role=role, body=body, workspace=workspace, parents=parents)
            return "new-reviewer"

        with patch.object(self.controller, "worktree", return_value=self.repo), \
             patch.object(self.controller, "spawn", side_effect=capture_spawn):
            self.controller.retry_blocked_verification(state, item, "retry reviewer only")

        self.assertIn(str(qa_path), spawned["body"])
        self.assertIn(hashlib.sha256(qa_bytes).hexdigest(), spawned["body"])
        self.assertIn(json.dumps([{"name": "frontend-vitest", "argv": quick}], ensure_ascii=False),
                      spawned["body"])
        self.assertIn("не твой собственный запуск", spawned["body"])
        self.assertIn("Не утверждай, что неперечисленные", spawned["body"])
        self.assertIn("архив предыдущего отчёта reviewer", spawned["body"])
        self.assertIn("Прежде чем повторять дорогую проверку", spawned["body"])
        self.assertIn("Самостоятельно вынеси новый verdict", spawned["body"])
        self.assertNotIn(json.dumps(full), spawned["body"])

    def test_blocked_reviewer_retry_does_not_reference_stale_qa_sha(self):
        state = {"id": "feature-retry", "project": "demo", "stage": "blocked",
                 "initial_base_sha": self.base_sha, "items": []}
        item = {"definition": {"id": "T-001", "title": "Frontend change"}, "round": 1,
                "workspace": str(self.repo), "head_sha": self.base_sha,
                "base_sha": self.base_sha, "card": "parent-card", "reviewer_card": "old-reviewer",
                "qa_card": "old-qa", "verification_blocked_roles": ["reviewer"]}
        state["items"].append(item)
        state["index"] = 0
        round_dir = self.controller.round_path(state, item)
        qa_path = round_dir / "QA.json"
        self.board.runs["old-qa"] = [{"id": "42", "profile": "team-qa",
                                     "status": "done", "outcome": "completed"}]
        qa_path.write_text(json.dumps({"schema_version": 1, "task_id": "parent-card", "role": "qa",
                                      "assignee": "team-qa", "run_id": "42", "verdict": "pass",
                                      "base_sha": self.base_sha, "head_sha": "f" * 40,
                                      "findings": [], "checks": [{"name": "unit", "status": "pass"}]}))
        item["qa_report_path"] = str(qa_path)
        spawned = {}

        def capture_spawn(_state, role, _title, body, workspace, _key, parents=None):
            spawned.update(role=role, body=body)
            return "new-reviewer"

        with patch.object(self.controller, "worktree", return_value=self.repo), \
             patch.object(self.controller, "spawn", side_effect=capture_spawn):
            self.controller.retry_blocked_verification(state, item, "retry reviewer only")

        self.assertNotIn("Успешный отчёт QA-соседа", spawned["body"])

    def test_changes_requested_verification_still_uses_developer_rework(self):
        state = self.submit()
        item = {"definition": {"id": "task-a", "title": "Safe behavior"},
                "round": 1, "card": "developer-card", "base_sha": self.base_sha,
                "head_sha": self.base_sha, "workspace": str(self.repo)}
        with patch.object(self.controller, "read_evidence",
                          return_value=({"verdict": "changes_requested"}, {"verdict": "pass"})), \
             patch.object(self.controller, "validate_pair",
                          side_effect=GitGateError("review found a code issue")), \
             patch.object(self.controller, "rework") as rework:
            self.controller.start_integrator(state, item)

        rework.assert_called_once()
        self.assertIn("review found a code issue", rework.call_args.args[2])

    def test_blocked_verification_retry_uses_same_candidate_without_rework(self):
        state = self.submit()
        candidate = self.controller.worktree(state, "blocked-verification-candidate", self.base_sha)
        (candidate / "README.md").write_text("candidate\n")
        git(candidate, "add", "README.md")
        git(candidate, "commit", "-m", "candidate")
        head_sha = git(candidate, "rev-parse", "HEAD")
        developer = self.board.create_task("implementation", "team-backend",
                                           idempotency_key="test-implementation")
        developer["status"] = "done"
        reviewer = self.board.create_task("reviewer", "team-reviewer",
                                           idempotency_key="test-reviewer")
        reviewer["status"] = "blocked"
        qa = self.board.create_task("qa", "team-qa", idempotency_key="test-qa")
        qa["status"] = "blocked"
        out = self.root / "data/runs/feature-1/tasks/task-a/round-1"
        out.mkdir(parents=True)
        blocked_report = out / "QA.json"
        blocked_report.write_text('{"verdict":"blocked"}\n')
        item = {"definition": {"id": "task-a", "title": "Safe behavior",
                                "assignee_role": "backend", "owned_paths": ["README.md"]},
                "round": 1, "card": developer["id"], "base_sha": self.base_sha,
                "head_sha": head_sha, "workspace": str(candidate),
                "reviewer_card": reviewer["id"], "qa_card": qa["id"],
                "reviewer_workspace": str(candidate), "qa_workspace": str(candidate),
                "reviewer_report_path": str(out / "REVIEW.json"),
                "qa_report_path": str(blocked_report), "verification_blocked_roles": ["reviewer", "qa"]}
        (out / "REVIEW.json").write_text('{"verdict":"pass"}\n')

        state.update(stage="blocked", resume_stage="verification",
                     blocker="verification unavailable")
        state["items"] = [item]
        self.controller.save(state)
        self.board.block_external(state["root_card"], state["blocker"])
        with patch("sys.argv", ["teamctl", "--config", str(self.config_path), "resume",
                                 "feature-1", "--reason", "Test runtime is now available",
                                 "--retry-workers"]), \
             patch("team_engine.controller.Controller", return_value=self.controller):
            controller_main()

        saved = json.loads((self.root / "data/runs/feature-1/state.json").read_text())
        item = saved["items"][0]
        self.assertEqual(saved["stage"], "verification")
        self.assertEqual(item["round"], 1)
        self.assertNotEqual(item["reviewer_card"], reviewer["id"])
        self.assertNotEqual(item["qa_card"], qa["id"])
        self.assertEqual(self.board.get_task(developer["id"])["status"], "done")
        for role, prior_card_id in (("reviewer", reviewer["id"]), ("qa", qa["id"])):
            retry_id = item[role + "_card"]
            retry_card = self.board.get_task(retry_id)
            self.assertEqual(retry_card["parents"], [developer["id"]])
            self.assertIn(prior_card_id, retry_card["body"])
            retry_card["status"] = "blocked"
            self.assertEqual(self.board.activate_task(retry_id)["status"], "ready",
                             f"a blocked previous {role} card must not prevent retry admission")
        self.assertEqual(git(item["qa_workspace"], "rev-parse", "HEAD"), head_sha)
        self.assertIn("verification-retry-1.json", item["qa_report_path"])
        self.assertTrue((out / "report-attempts/qa/blocked/attempt-1.json").is_file())
        self.assertNotIn("verification_blocked_roles", item)

    def test_finalizing_recovers_when_root_card_already_done_without_duplicate_completion(self):
        state = self.submit()
        run = self.root / "data/runs/feature-1"
        delivery = {"feature_id": state["id"], "status": "ready_for_owner", "head_sha": self.base_sha}
        (run / "artifacts/DELIVERY.json").write_text(json.dumps(delivery))
        state["stage"] = "finalizing"
        self.board.tasks[state["root_card"]]["status"] = "done"

        self.controller.tick(state)

        self.assertEqual(state["stage"], "done")
        self.assertEqual(self.board.completed_external_calls, 0)

    def test_finalizing_completes_running_root_once_and_is_idempotent(self):
        state = self.submit()
        run = self.root / "data/runs/feature-1"
        (run / "artifacts/DELIVERY.json").write_text(json.dumps({
            "feature_id": state["id"], "status": "ready_for_owner", "head_sha": self.base_sha,
        }))
        state["stage"] = "finalizing"

        self.controller.tick(state)
        self.assertEqual(state["stage"], "done")
        self.assertEqual(self.board.tasks[state["root_card"]]["status"], "done")
        self.assertEqual(self.board.completed_external_calls, 1)

        self.controller.tick(state)
        self.assertEqual(self.board.completed_external_calls, 1)

    def test_plan_acceptance_parks_work_until_parent_and_dependencies_are_done(self):
        state = self.submit()
        run = self.root / "data/runs/feature-1"
        (run / "artifacts/SPEC.md").write_text("# Spec\n" + "A complete specification. " * 12)
        self.board.tasks[state["analysis_card"]]["status"] = "done"
        self.controller.tick(state)
        self.assertEqual(state["stage"], "planning")
        self.assertEqual(self.board.tasks[state["planner_card"]]["parents"], [state["analysis_card"]])

        (run / "artifacts/DESIGN.md").write_text("# Design\n" + "A complete design description. " * 8)
        self.write_plan(run)
        self.board.tasks[state["planner_card"]]["status"] = "done"
        self.controller.tick(state)

        self.assertEqual(state["stage"], "plan_challenge")
        review_path = Path(state["plan_review_path"])
        for run_id, role in enumerate(("skeptic", "test-designer"), start=201):
            card = state[role + "_card"]
            report = {"schema_version": 1, "feature_id": state["id"],
                      "plan_sha256": state["plan_sha256"], "role": role,
                      "assignee": "team-" + role,
                      "run_id": str(run_id),
                      "verdict": "pass", "findings": []}
            (review_path / (role + ".json")).write_text(json.dumps(report))
            self.board.tasks[card]["status"] = "done"
            self.board.runs[card] = [{"id": int(report["run_id"]), "profile": "team-" + role,
                                      "status": "completed", "outcome": "completed"}]

        self.controller.tick(state)
        self.assertEqual(state["stage"], "plan_judgment", state.get("blocker"))
        judge_card = state["judge_card"]
        decision = {"schema_version": 1, "feature_id": state["id"],
                    "plan_sha256": state["plan_sha256"], "assignee": "team-judge",
                    "run_id": "203",
                    "decision": "accept", "resolutions": [],
                    "summary": "Both independent reviews found the plan complete and implementable."}
        (review_path / "DECISION.json").write_text(json.dumps(decision))
        self.board.tasks[judge_card]["status"] = "done"
        self.board.runs[judge_card] = [{"id": int(decision["run_id"]), "profile": "team-judge",
                                        "status": "completed", "outcome": "completed"}]
        self.controller.tick(state)

        first, second = state["items"]
        self.assertEqual(state["stage"], "implementation")
        self.assertEqual(self.board.tasks[first["card"]]["status"], "ready")
        self.assertEqual(self.board.tasks[second["card"]]["status"], "blocked")
        self.assertEqual(self.board.tasks[second["card"]]["parents"], [state["planner_card"], first["card"]])
        self.assertTrue(Path(first["workspace"]).is_dir())
        self.assertEqual(git(first["workspace"], "rev-parse", "HEAD"), self.base_sha)

    def test_plan_reviews_rejects_modified_review_snapshot(self):
        state = self.submit()
        run = self.root / "data/runs/feature-1"
        artifacts = run / "artifacts"
        for name, content in (("SPEC.md", "spec"), ("DESIGN.md", "design"),
                              ("PLAN.json", '{"plan": 1}')):
            (artifacts / name).write_text(content)
        review_path = run / "analysis-round-1"
        review_path.mkdir()
        state["plan_sha256"] = self.controller.plan_digest(state)
        state["plan_review_path"] = str(review_path)
        for name in ("SPEC.md", "DESIGN.md", "PLAN.json"):
            (review_path / name).write_bytes((artifacts / name).read_bytes())
        (review_path / "PLAN.json").write_text('{"plan": 2}')

        with self.assertRaisesRegex(ContractError, "review snapshot changed"):
            self.controller.plan_reviews(state)

    def test_decide_plan_rejects_clean_judge_worktree_at_wrong_head(self):
        state = self.submit()
        review_path = self.root / "data/runs/feature-1/analysis-round-1"
        review_path.mkdir()
        (review_path / "DECISION.json").write_text(json.dumps({"decision": "accept"}))
        state.update({"plan_review_path": str(review_path), "plan_sha256": "digest",
                      "judge_card": "judge-card", "initial_base_sha": self.base_sha})
        workspace = self.controller.worktree(state, "plan-judge-r1", self.base_sha)
        state["judge_workspace"] = str(workspace)
        (workspace / "README.md").write_text("changed judge checkout\n")
        git(workspace, "add", "README.md")
        git(workspace, "commit", "-m", "change inspected revision")

        with patch.object(self.controller, "plan_reviews", return_value=[]), \
             patch.object(self.controller, "verify_actor"):
            with self.assertRaisesRegex(ContractError, "judge changed the inspected revision"):
                self.controller.decide_plan(state)

    def write_plan(self, run: Path, *, malformed: bool = False):
        if malformed:
            value = {"schema_version": 1, "project": "demo", "feature_id": "feature-1",
                     "base_sha": self.base_sha, "status": "ready", "open_questions": [],
                     "tasks": [None], "requirement_coverage": []}
        else:
            def entry(task_id, dependencies):
                return {
                    "id": task_id, "title": f"Implement {task_id}",
                    "problem": "A needed user capability is missing",
                    "requirements": ["REQ-1"], "solution": "Implement a bounded module change",
                    "acceptance_criteria": [{"id": f"AC-{task_id}", "given": "valid input",
                                             "when": "called", "then": "returns expected data",
                                             "evidence": "unit assertion"}],
                    "test_plan": [{"name": "valid input", "type": "unit", "scope": "handler",
                                   "reason": "checks acceptance"}],
                    "dependencies": dependencies, "priority": "P1" if not dependencies else "P2",
                    "priority_reason": "High user impact and unblocks work",
                    "owned_paths": ["src/feature"], "assignee_role": "backend",
                }
            value = {"schema_version": 1, "project": "demo", "feature_id": "feature-1",
                     "base_sha": self.base_sha, "status": "ready", "open_questions": [],
                     "tasks": [entry("T-1", []), entry("T-2", ["T-1"])],
                     "requirement_coverage": [{"requirement_id": "REQ-1", "task_ids": ["T-1", "T-2"],
                                                "verification": "Unit test"}]}
        (run / "artifacts/PLAN.json").write_text(json.dumps(value))

    def test_malformed_plan_is_returned_for_bounded_contract_repair_then_blocks(self):
        state = self.submit()
        run = self.root / "data/runs/feature-1"
        (run / "artifacts/SPEC.md").write_text("# Spec\n" + "A complete specification. " * 12)
        self.board.tasks[state["analysis_card"]]["status"] = "done"
        self.controller.tick(state)
        self.board.tasks[state["planner_card"]]["status"] = "done"
        (run / "artifacts/DESIGN.md").write_text("# Design\n" + "A complete design description. " * 8)
        (run / "artifacts/SPEC.md").write_text("# Spec\n" + "A complete specification. " * 12)
        self.write_plan(run, malformed=True)

        original_planner = state["planner_card"]
        self.controller.tick(state)

        self.assertEqual(state["stage"], "planning")
        self.assertNotEqual(state["planner_card"], original_planner)
        self.assertEqual(state["plan_round"], 2)
        error_file = run / "analysis-round-1/CONTRACT-ERROR.txt"
        self.assertTrue(error_file.is_file())
        self.assertIn("task", error_file.read_text().lower())

        for expected_round in (3,):
            self.board.tasks[state["planner_card"]]["status"] = "done"
            self.controller.tick(state)
            self.assertEqual(state["stage"], "planning")
            self.assertEqual(state["plan_round"], expected_round)
        self.board.tasks[state["planner_card"]]["status"] = "done"
        self.controller.tick(state)
        self.assertEqual(state["stage"], "blocked")
        self.assertIn("repair limit", state["blocker"].lower())
        self.assertEqual(self.board.tasks[state["root_card"]]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
