from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.contracts import ContractError, path_owned, validate_owned_path, validate_plan


def task(task_id: str, *, priority: str = "P2", dependencies=None, owned_paths=None,
         role: str = "backend") -> dict:
    return {
        "id": task_id,
        "title": f"Implement {task_id}",
        "problem": "A concrete user need is not supported yet",
        "requirements": ["REQ-1"],
        "solution": "Add the bounded behavior in the selected module",
        "acceptance_criteria": [{
            "id": f"AC-{task_id}-1", "given": "A valid request", "when": "The feature runs",
            "then": "The expected result is returned", "evidence": "Unit test assertion",
        }],
        "test_plan": [{"name": "valid request", "type": "unit", "scope": "module",
                       "reason": "Covers the acceptance criterion"}],
        "dependencies": dependencies or [],
        "priority": priority,
        "priority_reason": "Unblocks a user-facing capability",
        "owned_paths": owned_paths or ["src/feature"],
        "assignee_role": role,
    }


def plan(tasks: list[dict], *, open_questions=None, coverage=None) -> dict:
    ids = [entry["id"] for entry in tasks]
    return {
        "schema_version": 1,
        "project": "demo",
        "feature_id": "feature-1",
        "base_sha": "a" * 40,
        "status": "ready",
        "open_questions": [] if open_questions is None else open_questions,
        "tasks": tasks,
        "requirement_coverage": ([{"requirement_id": "REQ-1", "task_ids": ids,
                                    "verification": "Unit test"}] if coverage is None else coverage),
    }


class ContractTests(unittest.TestCase):
    def validate(self, value: dict) -> list[dict]:
        return validate_plan(value, project="demo", feature_id="feature-1", base_sha="a" * 40)

    def test_returns_stable_topological_order_prioritizing_ready_work(self) -> None:
        value = plan([
            task("T-1", priority="P2"),
            task("T-2", priority="P0"),
            task("T-3", priority="P1", dependencies=["T-2"]),
        ])
        self.assertEqual([entry["id"] for entry in self.validate(value)], ["T-2", "T-3", "T-1"])

    def test_rejects_wrong_project_feature_or_base_binding(self) -> None:
        for key, wrong in (("project", "other"), ("feature_id", "feature-2"), ("base_sha", "b" * 40)):
            value = plan([task("T-1")])
            value[key] = wrong
            with self.subTest(key=key), self.assertRaises(ContractError):
                self.validate(value)

    def test_rejects_unanswered_questions_and_non_ready_plan(self) -> None:
        with self.assertRaisesRegex(ContractError, "not ready"):
            self.validate(plan([task("T-1")], open_questions=["Who owns rollout?"]))
        value = plan([task("T-1")])
        value["status"] = "draft"
        with self.assertRaisesRegex(ContractError, "not ready"):
            self.validate(value)

    def test_rejects_duplicate_ids_unknown_dependencies_and_cycles(self) -> None:
        with self.assertRaisesRegex(ContractError, "Duplicate"):
            self.validate(plan([task("T-1"), task("T-1")]))
        with self.assertRaisesRegex(ContractError, "Dependency cycle or unknown"):
            self.validate(plan([task("T-1", dependencies=["T-404"])]))
        with self.assertRaisesRegex(ContractError, "Dependency cycle or unknown"):
            self.validate(plan([task("T-1", dependencies=["T-2"]),
                                task("T-2", dependencies=["T-1"])]))

    def test_rejects_invalid_role_priority_and_incomplete_acceptance_criteria(self) -> None:
        cases = []
        value = task("T-1", role="analyst")
        cases.append(value)
        value = task("T-1", priority="P9")
        cases.append(value)
        value = task("T-1")
        value["acceptance_criteria"] = [{"id": "AC-1", "given": "", "when": "x", "then": "y", "evidence": "z"}]
        cases.append(value)
        for item in cases:
            with self.subTest(task=item), self.assertRaises(ContractError):
                self.validate(plan([item]))

    def test_rejects_unsafe_owned_paths(self) -> None:
        for bad in ("/etc", "../secrets", "src/../../secrets", "~/.ssh", "-rf", ".env", ".git/config", "**"):
            with self.subTest(path=bad), self.assertRaises(ContractError):
                validate_owned_path(bad)

    def test_path_ownership_matches_specific_directory_and_rejects_protected_paths(self) -> None:
        patterns = ["src/feature", "docs/*.md"]
        self.assertTrue(path_owned("src/feature/Handler.kt", patterns))
        self.assertTrue(path_owned("docs/overview.md", patterns))
        self.assertFalse(path_owned("src/feature-extra/Handler.kt", patterns))
        self.assertFalse(path_owned(".env", ["**"]))
        self.assertFalse(path_owned(".git/config", ["**"]))

    def test_rejects_incomplete_or_unknown_requirement_coverage(self) -> None:
        with self.assertRaisesRegex(ContractError, "Missing requirements"):
            self.validate(plan([task("T-1")], coverage=[]))
        with self.assertRaisesRegex(ContractError, "unknown task"):
            self.validate(plan([task("T-1")], coverage=[{"requirement_id": "REQ-1",
                                                           "task_ids": ["T-404"],
                                                           "verification": "Integration test"}]))


if __name__ == "__main__":
    unittest.main()
