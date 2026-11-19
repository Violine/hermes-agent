from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.git_gates import (
    EvidenceError,
    GitGateError,
    integrate_candidate,
    run_batch_regression,
    validate_candidate,
    validate_promotion_evidence,
)
from team_engine.execution_policy import verification_plan


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


class GitGatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Test User")
        git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "README.md").write_text("base\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "base")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-c", "feature/demo")
        (self.repo / "README.md").write_text("feature\n")
        (self.repo / "new.txt").write_text("new\n")
        git(self.repo, "add", "README.md", "new.txt")
        git(self.repo, "commit", "-m", "implement demo")
        self.head_sha = git(self.repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def evidence(self, role: str, assignee: str, run_id: str, *, verdict: str = "pass",
                 head_sha: str | None = None, base_sha: str | None = None,
                 check_status: str = "pass") -> dict:
        return {
            "schema_version": 1,
            "task_id": "demo-1",
            "role": role,
            "assignee": assignee,
            "run_id": run_id,
            "verdict": verdict,
            "base_sha": base_sha or self.base_sha,
            "head_sha": head_sha or self.head_sha,
            "findings": [],
            "checks": [{"name": "unit tests", "status": check_status, "command": ["pytest"], "exit_code": 0 if check_status == "pass" else 1}],
        }


    def test_batch_regression_runs_all_checks_and_aggregates_failures_at_one_sha(self) -> None:
        marker = Path(self.temp.name) / "second-ran"
        commands = [
            {"name": "first-fails", "argv": [sys.executable, "-c",
                "import sys; print('first failure'); sys.exit(7)"]},
            {"name": "second-passes", "argv": [sys.executable, "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"]},
        ]
        result = run_batch_regression(self.repo, self.head_sha, commands,
                                      worktree_parent=self.temp.name, timeout=5)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["head_sha"], self.head_sha)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual([item["name"] for item in result["results"]],
                         ["first-fails", "second-passes"])
        self.assertEqual(result["results"][0]["exit_code"], 7)
        self.assertEqual(result["results"][1]["status"], "pass")
        self.assertTrue(marker.is_file(), "later checks must run after an earlier failure")
        self.assertFalse(any(path.name.startswith("hermes-batch-regression-")
                             for path in Path(self.temp.name).iterdir()))

    def test_batch_regression_records_timeout_and_continues(self) -> None:
        marker = Path(self.temp.name) / "after-timeout"
        commands = [
            {"name": "times-out", "argv": [sys.executable, "-c",
                "import time; time.sleep(2)"]},
            {"name": "after-timeout", "argv": [sys.executable, "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"]},
        ]
        result = run_batch_regression(self.repo, self.head_sha, commands,
                                      worktree_parent=self.temp.name, timeout=0.15)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["results"][0]["status"], "timeout")
        self.assertEqual(result["results"][1]["status"], "pass")
        self.assertTrue(marker.is_file())

    def test_candidate_snapshot_captures_sha_and_changed_paths(self) -> None:
        snapshot = validate_candidate(self.repo, "main", "feature/demo")
        self.assertEqual(snapshot.base_sha, self.base_sha)
        self.assertEqual(snapshot.head_sha, self.head_sha)
        self.assertEqual(snapshot.changed_paths, ("README.md", "new.txt"))

    def test_dirty_source_worktree_is_rejected(self) -> None:
        (self.repo / "uncommitted.txt").write_text("dirty")
        with self.assertRaisesRegex(GitGateError, "dirty"):
            validate_candidate(self.repo, "main", "feature/demo")

    def test_stale_evidence_is_rejected(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa", head_sha=self.base_sha)
        with self.assertRaisesRegex(EvidenceError, "stale"):
            validate_promotion_evidence(reviewer, qa, task_id="demo-1", base_sha=self.base_sha,
                                        head_sha=self.head_sha, developer_id="developer-1")

    def test_high_or_critical_finding_blocks_even_when_flagged_nonblocking(self) -> None:
        for severity in ("high", "critical"):
            with self.subTest(severity=severity):
                reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
                reviewer["findings"] = [{"severity": severity, "blocking": False,
                                         "description": "Unresolved material defect"}]
                qa = self.evidence("qa", "qa-1", "run-qa")
                with self.assertRaisesRegex(EvidenceError, "blocking/high-severity"):
                    validate_promotion_evidence(reviewer, qa, task_id="demo-1",
                                                base_sha=self.base_sha, head_sha=self.head_sha,
                                                developer_id="developer-1")

    def test_malformed_json_field_types_fail_closed_with_evidence_error(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa")
        malformed_cases = []
        bad = dict(reviewer, verdict=["pass"])
        malformed_cases.append((bad, qa))
        bad = self.evidence("reviewer", "reviewer-1", "run-review")
        bad["findings"] = [{"severity": ["critical"], "blocking": False,
                            "description": "Malformed severity"}]
        malformed_cases.append((bad, qa))
        bad = self.evidence("qa", "qa-1", "run-qa")
        bad["checks"] = [{"name": "test", "status": ["pass"]}]
        malformed_cases.append((reviewer, bad))
        bad = dict(reviewer, schema_version=True)
        malformed_cases.append((bad, qa))
        for review, quality in malformed_cases:
            with self.subTest(review=review, qa=quality), self.assertRaises(EvidenceError):
                validate_promotion_evidence(review, quality, task_id="demo-1",
                                            base_sha=self.base_sha, head_sha=self.head_sha,
                                            developer_id="developer-1")

    def test_failed_qa_check_cannot_promote(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa", check_status="fail")
        with self.assertRaisesRegex(EvidenceError, "did not pass"):
            integrate_candidate(self.repo, task_id="demo-1", base_ref="main", head_sha=self.head_sha,
                                developer_id="developer-1", reviewer_evidence=reviewer,
                                qa_evidence=qa, trusted_test_commands=[["true"]])
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.base_sha)
        self.assertFalse((self.repo / ".git" / "refs" / "heads" / "codex" / "team-integration" / "demo-1").exists())

    def test_promotion_binds_qa_deferrals_to_pinned_quick_plan(self) -> None:
        plan = verification_plan(
            [{"name": "unit", "argv": ["pytest", "tests/unit"]},
             {"name": "full-regression", "argv": ["pytest", "tests"]}],
            {"version": 1, "path_rules": [
                {"name": "backend", "prefixes": ["backend"], "quick_check_indexes": [0]},
            ]},
            ["backend/app.py"],
        )
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa")
        qa["checks"] = [{"name": "unit", "status": "pass",
                          "command": ["pytest", "tests/unit"], "exit_code": 0}]
        qa["deferred"] = {"evidence_ref": "VERIFICATION-PLAN.json",
                          "policy_sha256": plan["policy_sha256"],
                          "checks": plan["deferred_checks"]}

        validate_promotion_evidence(reviewer, qa, task_id="demo-1", base_sha=self.base_sha,
                                    head_sha=self.head_sha, developer_id="developer-1",
                                    qa_verification_plan=plan)

        with self.assertRaisesRegex(EvidenceError, "requires the controller-pinned"):
            validate_promotion_evidence(reviewer, qa, task_id="demo-1", base_sha=self.base_sha,
                                        head_sha=self.head_sha, developer_id="developer-1")

        invalid_cases = [
            ({**qa, "checks": [*qa["checks"], {"name": "full tests", "status": "skip"}]},
             "cannot contain skip rows"),
            ({**qa, "deferred": {**qa["deferred"], "policy_sha256": "0" * 64}},
             "digest does not match"),
            ({**qa, "deferred": {**qa["deferred"],
                "checks": [{**plan["deferred_checks"][0], "argv": ["false"]}]}},
             "exactly match every pinned deferred"),
            ({**qa, "deferred": {**qa["deferred"], "checks": []}},
             "exactly match every pinned deferred"),
        ]
        for candidate, message in invalid_cases:
            with self.subTest(message=message), self.assertRaisesRegex(EvidenceError, message):
                validate_promotion_evidence(
                    reviewer, candidate, task_id="demo-1", base_sha=self.base_sha,
                    head_sha=self.head_sha, developer_id="developer-1",
                    qa_verification_plan=plan,
                )

        full_plan = verification_plan(
            [{"name": "unit", "argv": ["pytest", "tests/unit"]},
             {"name": "full-regression", "argv": ["pytest", "tests"]}],
            {"version": 1, "path_rules": [
                {"name": "backend", "prefixes": ["backend"], "quick_check_indexes": [0]},
            ]},
            ["backend/app.py"], force_full=True,
        )
        with self.assertRaisesRegex(EvidenceError, "plan has none"):
            validate_promotion_evidence(
                reviewer, qa, task_id="demo-1", base_sha=self.base_sha,
                head_sha=self.head_sha, developer_id="developer-1",
                qa_verification_plan=full_plan,
            )

    def test_killed_mutant_is_supplemental_and_does_not_replace_passing_checks(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa")
        qa["supplemental_checks"] = [{
            "kind": "mutation_killed",
            "name": "AC-1 sensitivity: check order",
            "command": ["./gradlew", ":backend:test", "--tests", "ExampleTest"],
            "mutant_id": "M1",
            "killed_by": "stale version is rejected before checking stage",
            "exit_code": 1,
            "head_sha": self.head_sha,
            "scratch_worktree": "/tmp/qa-mutation-run",
            "clean_after_revert": True,
            "evidence": "Expected failing assertion killed M1; candidate worktree remains clean.",
        }]
        validate_promotion_evidence(reviewer, qa, task_id="demo-1", base_sha=self.base_sha,
                                    head_sha=self.head_sha, developer_id="developer-1")

    def test_nonzero_mandatory_command_still_fails_even_with_mutation_evidence(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa")
        qa["checks"][0]["exit_code"] = 1
        qa["supplemental_checks"] = [{
            "kind": "mutation_killed", "name": "M1", "command": ["pytest"],
            "mutant_id": "M1", "killed_by": "expected assertion",
            "exit_code": 1, "head_sha": self.head_sha,
            "scratch_worktree": "/tmp/mutant", "clean_after_revert": True,
            "evidence": "mutant was killed",
        }]
        with self.assertRaisesRegex(EvidenceError, "non-zero exit_code"):
            validate_promotion_evidence(reviewer, qa, task_id="demo-1", base_sha=self.base_sha,
                                        head_sha=self.head_sha, developer_id="developer-1")

    def test_malformed_supplemental_mutation_evidence_fails_closed(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        invalid_checks = [
            {"kind": "command_failed"},
            {"kind": "mutation_killed", "name": "M1", "command": ["pytest"],
             "mutant_id": "M1", "killed_by": "expected assertion", "exit_code": 124,
             "head_sha": self.head_sha, "scratch_worktree": "/tmp/mutant",
             "clean_after_revert": True, "evidence": "timed out"},
            {"kind": "mutation_killed", "name": "M1", "command": ["pytest"],
             "mutant_id": "M1", "killed_by": "expected assertion", "exit_code": 1,
             "head_sha": self.base_sha, "scratch_worktree": "/tmp/mutant",
             "clean_after_revert": True, "evidence": "stale candidate"},
            {"kind": "mutation_killed", "name": "M1", "command": ["pytest"],
             "mutant_id": "M1", "killed_by": "expected assertion", "exit_code": 1,
             "head_sha": self.head_sha, "scratch_worktree": "/tmp/mutant",
             "clean_after_revert": False, "evidence": "worktree remained dirty"},
        ]
        for supplemental in invalid_checks:
            with self.subTest(supplemental=supplemental):
                qa = self.evidence("qa", "qa-1", "run-qa")
                qa["supplemental_checks"] = [supplemental]
                with self.assertRaises(EvidenceError):
                    validate_promotion_evidence(reviewer, qa, task_id="demo-1",
                                                base_sha=self.base_sha, head_sha=self.head_sha,
                                                developer_id="developer-1")

    def test_same_person_cannot_review_and_qa(self) -> None:
        reviewer = self.evidence("reviewer", "same-agent", "run-review")
        qa = self.evidence("qa", "same-agent", "run-qa")
        with self.assertRaisesRegex(EvidenceError, "different assignees"):
            validate_promotion_evidence(reviewer, qa, task_id="demo-1", base_sha=self.base_sha,
                                        head_sha=self.head_sha, developer_id="developer-1")

    def test_success_integrates_in_isolated_branch_without_moving_main(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa")
        progress = []
        result = integrate_candidate(
            self.repo, task_id="demo-1", base_ref="main", head_sha=self.head_sha,
            developer_id="developer-1", reviewer_evidence=reviewer, qa_evidence=qa,
            trusted_test_commands=[[sys.executable, "-c", "from pathlib import Path; assert Path('new.txt').read_text() == 'new\\n'; print('API_KEY=do-not-show {\"access_token\":\"also-secret\"}')"]],
            check_progress=progress.append,
        )
        self.assertEqual(result.base_sha, self.base_sha)
        self.assertEqual(result.candidate_sha, self.head_sha)
        self.assertNotEqual(result.integration_sha, self.base_sha)
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.base_sha)
        self.assertEqual(git(self.repo, "rev-parse", result.integration_branch), result.integration_sha)
        self.assertEqual(result.changed_paths, ("README.md", "new.txt"))
        self.assertTrue(Path(result.worktree, "new.txt").exists())
        self.assertEqual([event["event"] for event in progress], ["started", "completed"])
        self.assertEqual(progress[0]["index"], 1)
        self.assertEqual(progress[0]["total"], 1)
        self.assertEqual(progress[1]["result"], "pass")
        self.assertEqual(progress[1]["returncode"], 0)
        self.assertLessEqual(len(progress[1]["output_tail"]), 4000)
        self.assertNotIn("do-not-show", progress[1]["output_tail"])
        self.assertNotIn("also-secret", progress[1]["output_tail"])
        self.assertIn("[redacted]", progress[1]["output_tail"])


    def test_integration_holds_shared_lock_for_direct_checks(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa")
        lock = self.repo.parent / "integration-heavy.lock"
        checker = self.repo.parent / "check_lock.py"
        checker.write_text(
            "import fcntl,sys\n"
            "with open(sys.argv[1], 'a') as lock:\n"
            " try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            " except BlockingIOError: print('outer-lock-held')\n"
            " else: raise SystemExit('outer lock missing')\n"
        )
        result = integrate_candidate(
            self.repo, task_id="demo-1", base_ref="main", head_sha=self.head_sha,
            developer_id="developer-1", reviewer_evidence=reviewer, qa_evidence=qa,
            trusted_test_commands=[[sys.executable, str(checker), str(lock)]],
            check_lock_path=lock,
        )
        self.assertEqual(result.checks[0].exit_code, 0)
        self.assertIn("outer-lock-held", result.checks[0].stdout)


    def _leave_interrupted_integration(self, worktree_parent: Path, *, committed: bool) -> tuple[Path, str]:
        branch = "codex/team-integration/demo-1"
        worktree_parent.mkdir(parents=True, exist_ok=True)
        stale = worktree_parent / "hermes-team-demo-1-crashed"
        git(self.repo, "worktree", "add", "-b", branch, str(stale), self.base_sha)
        git(stale, "merge", "--no-ff", "--no-commit", self.head_sha)
        if committed:
            git(stale, "commit", "--no-edit")
        return stale, git(self.repo, "rev-parse", branch)

    def test_integration_recovers_branch_left_before_commit_and_rechecks_candidate(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa")
        parent = self.repo.parent / "integrations"
        stale, previous_tip = self._leave_interrupted_integration(parent, committed=False)
        self.assertEqual(previous_tip, self.base_sha)
        self.assertTrue((stale / "README.md").is_file())
        result = integrate_candidate(
            self.repo, task_id="demo-1", base_ref="main", head_sha=self.head_sha,
            developer_id="developer-1", reviewer_evidence=reviewer, qa_evidence=qa,
            trusted_test_commands=[[sys.executable, "-c", "assert True"]],
            worktree_parent=parent, check_lock_path=self.repo.parent / "lock")
        self.assertEqual(result.candidate_sha, self.head_sha)
        self.assertNotEqual(result.integration_sha, self.base_sha)
        self.assertEqual(result.checks[0].exit_code, 0)
        self.assertFalse(stale.exists())
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.base_sha)
        self.assertEqual(git(self.repo, "rev-parse", result.integration_branch), result.integration_sha)

    def test_integration_rechecks_exact_existing_merge_commit_after_interruption(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa")
        parent = self.repo.parent / "integrations"
        stale, previous_tip = self._leave_interrupted_integration(parent, committed=True)
        check_marker = self.repo.parent / "recovered-check-ran"
        result = integrate_candidate(
            self.repo, task_id="demo-1", base_ref="main", head_sha=self.head_sha,
            developer_id="developer-1", reviewer_evidence=reviewer, qa_evidence=qa,
            trusted_test_commands=[[sys.executable, "-c",
                f"from pathlib import Path; Path({str(check_marker)!r}).write_text('ran')"]],
            worktree_parent=parent, check_lock_path=self.repo.parent / "lock")
        self.assertEqual(result.integration_sha, previous_tip)
        self.assertEqual(result.checks[0].exit_code, 0)
        self.assertEqual(check_marker.read_text(), "ran")
        self.assertFalse(stale.exists())
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.base_sha)
        self.assertEqual(git(self.repo, "rev-parse", result.integration_branch), previous_tip)

    def test_integration_rejects_unrecognized_existing_task_branch(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa")
        branch = "codex/team-integration/demo-1"
        git(self.repo, "branch", branch, self.head_sha)
        with self.assertRaisesRegex(GitGateError, "unrecognized state"):
            integrate_candidate(
                self.repo, task_id="demo-1", base_ref="main", head_sha=self.head_sha,
                developer_id="developer-1", reviewer_evidence=reviewer, qa_evidence=qa,
                trusted_test_commands=[[sys.executable, "-c", "assert True"]],
                worktree_parent=self.repo.parent / "integrations",
                check_lock_path=self.repo.parent / "lock")
        self.assertEqual(git(self.repo, "rev-parse", branch), self.head_sha)
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.base_sha)

    def test_long_check_emits_periodic_heartbeat(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa")
        progress = []
        with mock.patch("team_engine.git_gates._CHECK_HEARTBEAT_SECONDS", 0.03):
            integrate_candidate(
                self.repo, task_id="demo-1", base_ref="main", head_sha=self.head_sha,
                developer_id="developer-1", reviewer_evidence=reviewer, qa_evidence=qa,
                trusted_test_commands=[[sys.executable, "-c", "import time; time.sleep(.11)"]],
                check_progress=progress.append,
            )
        events = [event["event"] for event in progress]
        self.assertEqual(events[0], "started")
        self.assertIn("heartbeat", events)
        self.assertEqual(events[-1], "completed")

    def test_timeout_kills_command_and_descendant_process_group(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa")
        marker = self.repo.parent / "descendant-survived-timeout"
        child_code = (
            "import time; from pathlib import Path; time.sleep(.9); "
            f"Path({str(marker)!r}).write_text('survived')"
        )
        parent_code = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
            "print('descendant started',flush=True); time.sleep(10)"
        )
        progress = []
        with self.assertRaisesRegex(GitGateError, "timed out"):
            integrate_candidate(
                self.repo, task_id="demo-1", base_ref="main", head_sha=self.head_sha,
                developer_id="developer-1", reviewer_evidence=reviewer, qa_evidence=qa,
                trusted_test_commands=[[sys.executable, "-c", parent_code]],
                timeout=0.2, check_progress=progress.append,
            )
        time.sleep(1.0)
        self.assertFalse(marker.exists())
        completion = [event for event in progress if event["event"] == "completed"][-1]
        self.assertTrue(completion["timed_out"])
        self.assertEqual(completion["result"], "timeout")
        self.assertLessEqual(len(completion["output_tail"]), 4000)
        self.assertEqual(git(self.repo, "branch", "--list", "codex/team-integration/demo-1"), "")

    def test_failed_integration_test_cleans_up_branch_and_worktree(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa")
        progress = []
        with self.assertRaisesRegex(GitGateError, "exited 7"):
            integrate_candidate(
                self.repo, task_id="demo-1", base_ref="main", head_sha=self.head_sha,
                developer_id="developer-1", reviewer_evidence=reviewer, qa_evidence=qa,
                trusted_test_commands=[[sys.executable, "-c", "raise SystemExit(7)"]],
                check_progress=progress.append,
            )
        failed_check = [event for event in progress if event["event"] == "completed"][-1]
        self.assertEqual(failed_check["returncode"], 7)
        self.assertEqual(failed_check["result"], "fail")
        self.assertFalse(failed_check["timed_out"])
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.base_sha)
        self.assertEqual(git(self.repo, "branch", "--list", "codex/team-integration/demo-1"), "")
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_check_cannot_change_staged_file_even_if_status_shape_is_similar(self) -> None:
        reviewer = self.evidence("reviewer", "reviewer-1", "run-review")
        qa = self.evidence("qa", "qa-1", "run-qa")
        mutate_and_stage = (
            sys.executable, "-c",
            "from pathlib import Path; import subprocess; Path('new.txt').write_text('changed\\n'); "
            "subprocess.run(['git','add','new.txt'],check=True)",
        )
        with self.assertRaisesRegex(GitGateError, "modified the worktree"):
            integrate_candidate(
                self.repo, task_id="demo-1", base_ref="main", head_sha=self.head_sha,
                developer_id="developer-1", reviewer_evidence=reviewer, qa_evidence=qa,
                trusted_test_commands=[mutate_and_stage],
            )
        self.assertEqual(git(self.repo, "rev-parse", "main"), self.base_sha)
        self.assertEqual(git(self.repo, "branch", "--list", "codex/team-integration/demo-1"), "")


if __name__ == "__main__":
    unittest.main()
