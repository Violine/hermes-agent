from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_controller as fixtures
from team_engine.contracts import ContractError


class ScopeAmendmentTests(unittest.TestCase):
    def setUp(self):
        fixtures.ControllerTests.setUp(self)
        self.state = fixtures.ControllerTests.submit(self)
        run = self.controller.path(self.state)
        artifacts = run / "artifacts"
        (artifacts / "SPEC.md").write_text("Approved product requirements\n")
        (artifacts / "DESIGN.md").write_text("Approved design\n")
        (artifacts / "PLAN.json").write_text('{"schema_version":1}\n')
        self.state["approved_spec_sha256"] = hashlib.sha256(
            (artifacts / "SPEC.md").read_bytes()).hexdigest()
        self.state["approved_plan_sha256"] = self.controller.plan_digest(self.state)
        self.resolution_path = artifacts / "T004-REQUIREMENTS-RESOLUTION.md"
        self.resolution_path.write_text("Owner-approved security and workflow resolution.\n")
        self.resolution_sha = hashlib.sha256(self.resolution_path.read_bytes()).hexdigest()

        developer = self.board.create_task("developer", "team-backend",
                                           idempotency_key="scope-amendment-developer")
        reviewer = self.board.create_task("reviewer", "team-reviewer",
                                          idempotency_key="scope-amendment-reviewer")
        qa = self.board.create_task("qa", "team-qa", idempotency_key="scope-amendment-qa")
        developer["status"] = "done"
        reviewer["status"] = "blocked"
        qa["status"] = "blocked"
        candidate = "src/current.py"
        self.item = {
            "definition": {
                "id": "T004", "title": "Implement focused backend behavior",
                "problem": "The feature needs one focused change.",
                "requirements": ["Preserve current behavior."],
                "solution": "Implement the planned behavior.",
                "test_plan": ["Run focused unit tests."],
                "acceptance_criteria": [{"id": "AC-1", "given": "a request",
                    "when": "it is handled", "then": "the response is valid",
                    "evidence": "unit test"}],
                "owned_paths": [candidate], "priority": "P1",
                "priority_reason": "Required behavior", "dependencies": [],
                "assignee_role": "backend",
            },
            "status": "verification", "round": 3, "branch": "feature/T004",
            "card": developer["id"], "base_sha": self.base_sha,
            "head_sha": self.base_sha, "workspace": str(self.repo),
            "candidate_changed_paths": [candidate],
            "reviewer_card": reviewer["id"], "qa_card": qa["id"],
            "verification_blocked_roles": ["reviewer", "qa"],
            "reviewer_report_path": str(run / "tasks/T004/round-3/REVIEW.json"),
            "qa_report_path": str(run / "tasks/T004/round-3/QA.json"),
        }
        Path(self.item["reviewer_report_path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(self.item["reviewer_report_path"]).write_text('{"verdict":"blocked"}\n')
        Path(self.item["qa_report_path"]).write_text('{"verdict":"blocked"}\n')
        self.state.update(stage="blocked", resume_stage="verification",
                          blocker="verification runtime unavailable", index=0,
                          items=[self.item])

    def tearDown(self):
        fixtures.ControllerTests.tearDown(self)

    def evidence(self):
        return {
            "schema_version": 1,
            "operator_decision": "approve_scope_extension",
            "operator_actor": "codex:owner-delegated",
            "user_authorization": "Да, подтверждаю это ограниченное расширение scope.",
            "created_at": 1793294400,
            "feature_id": self.state["id"],
            "task_id": "T004",
            "blocked_round": 3,
            "developer_card_id": self.item["card"],
            "code_rework_round": 2,
            "base_sha": self.base_sha,
            "candidate_sha": self.base_sha,
            "approved_plan_sha256": self.state["approved_plan_sha256"],
            "approved_spec_sha256": self.state["approved_spec_sha256"],
            "original_owned_paths": ["src/current.py"],
            "added_owned_paths": ["frontend/src/components/lead-card.tsx"],
            "rationale": "The blocked candidate omitted a required user-visible outcome.",
            "resolution_path": str(self.resolution_path),
            "resolution_sha256": self.resolution_sha,
            "developer_instructions": (
                "Add the lead-card presentation described here, preserve the approved API contract, "
                "and keep all changes within the newly approved path."),
            "acceptance_criteria": [{"id": "AC-UI-1", "given": "a lead is available",
                "when": "the lead card is rendered", "then": "the contact status is visible",
                "evidence": "component test"}],
            "test_plan": [{"name": "Lead card component", "type": "component",
                           "scope": "frontend/src/components/lead-card.tsx",
                           "reason": "Verify the newly approved user-visible lead status."}],
        }

    def test_applies_single_bound_scope_extension_and_runs_normal_rework_cycle(self):
        evidence = self.evidence()
        card_id = self.controller.apply_task_scope_amendment(self.state, evidence)

        item = self.state["items"][0]
        self.assertEqual(item["round"], 4)
        self.assertEqual(item["code_rework_round"], 3)
        self.assertNotIn("scope_amendment_permit", item)
        self.assertNotIn("verification_blocked_roles", item)
        self.assertNotIn("qa_report_path", item)
        self.assertEqual(item["verification_attempt_history"][0]["blocked_roles"], ["reviewer", "qa"])
        self.assertIn("report_sha256", item["verification_attempt_history"][0]["cards"]["qa"])
        self.assertEqual(item["status"], "implementation")
        self.assertEqual(card_id, item["card"])
        self.assertEqual(item["definition"]["owned_paths"], [
            "src/current.py", "frontend/src/components/lead-card.tsx"])
        task_path = self.controller.path(self.state) / "tasks/T004/TASK.md"
        task = task_path.read_text()
        self.assertIn("lead-card presentation", task)
        self.assertIn("frontend/src/components/lead-card.tsx", task)
        self.assertIn("Сначала выполняй выбранные быстрые проверки", task)
        audit_path = Path(item["scope_amendments"][0]["audit_path"])
        self.assertTrue(audit_path.is_file())
        self.assertEqual(json.loads(audit_path.read_text()), evidence)

        # A retry of the same operator action returns the existing card and does
        # not increment round/accounting a second time.
        self.assertEqual(self.controller.apply_task_scope_amendment(self.state, evidence), card_id)
        self.assertEqual(item["round"], 4)
        self.assertEqual(item["code_rework_round"], 3)
        self.assertNotIn("scope_amendment_permit", item)
        with self.assertRaisesRegex(ContractError, "Rework limit reached"):
            self.controller.rework(self.state, item, "unrelated later retry")

    def test_blocked_root_activation_recovers_idempotently_and_tick_continues(self):
        root_id = self.state["root_card"]
        self.board.block_external(root_id, "verification blocked")
        evidence = self.evidence()
        activate = self.board.activate_task
        attempts = 0

        def fail_once(card_id):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary Hermes activation failure")
            return activate(card_id)

        with patch.object(self.board, "activate_task", side_effect=fail_once):
            with self.assertRaisesRegex(RuntimeError, "temporary Hermes activation failure"):
                self.controller.apply_task_scope_amendment(self.state, evidence)

        saved = json.loads((self.controller.path(self.state) / "state.json").read_text())
        self.assertTrue(saved["resume_requested"])
        self.assertEqual(self.board.get_task(root_id)["status"], "blocked")

        card_id = self.controller.apply_task_scope_amendment(self.state, evidence)
        self.assertEqual(card_id, self.state["items"][0]["card"])
        self.assertNotIn("resume_requested", self.state)
        self.assertEqual(self.board.get_task(root_id)["status"], "ready")
        self.controller.tick(self.state)
        self.assertNotIn("resume_requested", self.state)

    def test_rejects_stale_approval_digest(self):
        evidence = self.evidence()
        evidence["approved_plan_sha256"] = "0" * 64
        with self.assertRaisesRegex(ContractError, "approved_plan_sha256"):
            self.controller.apply_task_scope_amendment(self.state, evidence)

    def test_rejects_changed_approved_documents(self):
        (self.controller.path(self.state) / "artifacts/PLAN.json").write_text("changed\n")
        with self.assertRaisesRegex(ContractError, "Approved SPEC or plan changed"):
            self.controller.apply_task_scope_amendment(self.state, self.evidence())

    def test_rejects_tampered_or_external_resolution_artifact(self):
        evidence = self.evidence()
        self.resolution_path.write_text("changed after approval\n")
        with self.assertRaisesRegex(ContractError, "Requirements resolution path/hash"):
            self.controller.apply_task_scope_amendment(self.state, evidence)
        evidence = self.evidence()
        evidence["resolution_path"] = str(self.root / "outside-resolution.md")
        Path(evidence["resolution_path"]).write_text("not in feature artifacts\n")
        with self.assertRaisesRegex(ContractError, "Requirements resolution path/hash"):
            self.controller.apply_task_scope_amendment(self.state, evidence)

    def test_rejects_active_worker_card(self):
        self.board.get_task(self.item["qa_card"])["status"] = "running"
        with self.assertRaisesRegex(ContractError, "all feature worker cards to be terminal"):
            self.controller.apply_task_scope_amendment(self.state, self.evidence())

    def test_rejects_unapproved_or_overlapping_or_unsafe_scope(self):
        for changes, message in (
                ({"operator_decision": "draft"}, "explicit operator approval"),
                ({"added_owned_paths": ["src/current.py/new.py"]}, "outside the existing scope"),
                ({"added_owned_paths": ["../secrets"]}, "Unsafe owned path"),
                ({"added_owned_paths": ["frontend/src/components"]}, "exact files"),
                ({"added_owned_paths": ["frontend/**"]}, "concrete relative paths")):
            evidence = self.evidence()
            evidence.update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ContractError, message):
                self.controller.apply_task_scope_amendment(self.state, evidence)

    def test_rejects_incomplete_evidence_and_unfinished_developer(self):
        evidence = self.evidence()
        evidence["test_plan"] = []
        with self.assertRaisesRegex(ContractError, "test_plan entries"):
            self.controller.apply_task_scope_amendment(self.state, evidence)
        self.board.get_task(self.item["card"])["status"] = "blocked"
        with self.assertRaisesRegex(ContractError, "Previous developer card must be completed"):
            self.controller.apply_task_scope_amendment(self.state, self.evidence())


if __name__ == "__main__":
    unittest.main()
