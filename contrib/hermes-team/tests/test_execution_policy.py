from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.execution_policy import (
    ExecutionPolicyError,
    selected_argv,
    verification_plan,
)


class ExecutionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checks = [
            {"name": "backend-unit", "argv": ["pytest", "tests/backend"]},
            {"name": "frontend-lint", "argv": ["npm", "run", "lint"]},
            {"name": "frontend-full", "argv": ["npm", "run", "test:unit"]},
            {"name": "frontend-build", "argv": ["npm", "run", "build"]},
        ]
        self.policy = {
            "version": 1,
            "full_after_tasks": 2,
            "path_rules": [
                {"name": "backend", "prefixes": ["backend/", "db/"],
                 "quick_check_indexes": [0]},
                {"name": "frontend", "prefixes": ["web/"],
                 "quick_check_indexes": [1, 3]},
            ],
        }

    def test_no_policy_preserves_legacy_full_check_behavior(self):
        plan = verification_plan(self.checks, None, ["backend/model.py"])
        self.assertEqual(plan["mode"], "full")
        self.assertEqual(len(plan["selected_checks"]), 4)
        self.assertEqual(plan["deferred_checks"], [])
        self.assertIn("no_execution_policy", plan["reasons"])
        self.assertEqual(selected_argv(plan), [check["argv"] for check in self.checks])

    def test_covered_backend_path_runs_only_quick_check_and_records_deferred_suite(self):
        plan = verification_plan(self.checks, self.policy, ["backend/model.py"])
        self.assertEqual(plan["mode"], "quick")
        self.assertEqual([check["name"] for check in plan["selected_checks"]], ["backend-unit"])
        self.assertEqual([check["name"] for check in plan["deferred_checks"]],
                         ["frontend-lint", "frontend-full", "frontend-build"])
        self.assertEqual(plan["matched_rules"], ["backend"])
        self.assertEqual(selected_argv(plan), [["pytest", "tests/backend"]])
        self.assertEqual(len(plan["policy_sha256"]), 64)

    def test_multiple_changed_paths_union_quick_checks_but_any_unknown_path_forces_full(self):
        selected = verification_plan(self.checks, self.policy,
                                     ["backend/model.py", "web/LeadList.tsx"])
        self.assertEqual(selected["mode"], "quick")
        self.assertEqual([item["index"] for item in selected["selected_checks"]], [0, 1, 3])
        unknown = verification_plan(self.checks, self.policy,
                                    ["backend/model.py", "infra/deploy.yml"])
        self.assertEqual(unknown["mode"], "full")
        self.assertIn("uncovered_changed_paths", unknown["reasons"])
        self.assertEqual(len(unknown["selected_checks"]), 4)
        self.assertEqual(unknown["deferred_checks"], [])

    def test_unknown_paths_and_force_full_are_fail_closed(self):
        for paths in (None, [], ["../outside.py"], ["/absolute/path"]):
            with self.subTest(paths=paths):
                result = verification_plan(self.checks, self.policy, paths)
                self.assertEqual(result["mode"], "full")
                self.assertEqual(len(result["selected_checks"]), 4)
        forced = verification_plan(self.checks, self.policy, ["backend/model.py"],
                                   force_full=True)
        self.assertEqual(forced["mode"], "full")
        self.assertIn("full_run_required", forced["reasons"])

    def test_invalid_or_empty_policy_indexes_fail_closed_or_reject_configuration(self):
        invalid = {"version": 1, "path_rules": [
            {"name": "bad", "prefixes": ["backend/"], "quick_check_indexes": [99]}]}
        result = verification_plan(self.checks, invalid, ["backend/a.py"])
        self.assertEqual(result["mode"], "full")
        self.assertIn("policy_check_index_out_of_range", result["reasons"])
        with self.assertRaises(ExecutionPolicyError):
            verification_plan(self.checks, {"version": 2, "path_rules": []}, ["backend/a.py"])

    def test_check_commands_are_bound_into_snapshot_hash(self):
        original = verification_plan(self.checks, self.policy, ["backend/model.py"])
        changed = [dict(check) for check in self.checks]
        changed[0]["argv"] = ["pytest", "tests/backend", "--strict"]
        updated = verification_plan(changed, self.policy, ["backend/model.py"])
        self.assertNotEqual(original["policy_sha256"], updated["policy_sha256"])


if __name__ == "__main__":
    unittest.main()
