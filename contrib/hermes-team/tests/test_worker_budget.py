import json
import hashlib
import os
import sqlite3
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from team_engine import worker_budget as wb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import harden_test_wait_budget as native_patch


CARD = "t_12345678"
RUN = "12"
PROFILE = "team-backend"


class FakeIterationBudget:
    def __init__(self, limit):
        self.max_total = limit
        self.used = 0

    @property
    def remaining(self):
        return max(0, self.max_total - self.used)


class WorkerBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.tmp_path = Path(self.tempdir.name)
        self.home, self.data_root, self.db, self.env = self.make_worker()
        self.patches = [
            patch.object(Path, "home", lambda: self.home),
            patch.dict(os.environ, self.env, clear=False),
            patch.dict(sys.modules, {
                "agent": SimpleNamespace(__path__=[]),
                "agent.iteration_budget": SimpleNamespace(IterationBudget=FakeIterationBudget),
            }),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def make_worker(self, *, wait_status="resumed", remaining_seconds=700,
                    remaining_turns=23, latest_wait_run_id="11"):
        home = self.tmp_path / "home"
        hermes = home / ".hermes"
        team = hermes / "team"
        data_root = team / "data"
        run_dir = data_root / "runs" / "req-0123456789abcdef0123"
        prereq = run_dir / "prerequisites" / "abc123"
        prereq.mkdir(parents=True)
        (team / "config.json").write_text(json.dumps({"data_root": str(data_root)}))
        state = {
            "id": run_dir.parent.name,
            "root_card": CARD,
            "items": [],
            "assignment_snapshots": {CARD: {"role": PROFILE}},
            "prerequisite_paths": {CARD: str(prereq)},
            "test_wait_budgets": {CARD: {
                "card_id": CARD, "assignee": PROFILE,
                "budget_seconds": 3600, "max_turns": 80,
                "used_seconds": 2900, "used_turns": 57,
                "remaining_seconds": remaining_seconds,
                "remaining_turns": remaining_turns,
                "charged_runs": {"10": {"active_seconds": 2000, "iterations": 40}},
                "latest_wait_run_id": latest_wait_run_id,
            }},
            "test_waits": {CARD: {"status": wait_status, "run_id": "11"}},
        }
        (run_dir / "state.json").write_text(json.dumps(state))
        db = hermes / "kanban" / "team.sqlite3"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE tasks(id TEXT PRIMARY KEY, assignee TEXT, status TEXT,
                current_run_id INTEGER, created_by TEXT);
            CREATE TABLE task_runs(id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,
                started_at INTEGER, ended_at INTEGER, status TEXT, outcome TEXT);
        """)
        conn.execute("INSERT INTO tasks VALUES(?,?,?,?,?)",
                     (CARD, PROFILE, "running", int(RUN), "hermes-team-controller"))
        conn.execute("INSERT INTO task_runs VALUES(?,?,?,?,?,?,?)",
                     (int(RUN), CARD, PROFILE, 1000, None, "running", None))
        conn.commit()
        conn.close()
        env = {
            "HERMES_KANBAN_TASK": CARD,
            "HERMES_KANBAN_RUN_ID": RUN,
            "HERMES_PROFILE": PROFILE,
            "HERMES_KANBAN_DB": str(db),
        }
        return home, data_root, db, env

    def grant_attempt(self, *, max_seconds=300, max_turns=5, previous_status="failed",
                      previous_ended_at=900, previous_card=CARD,
                      previous_profile=PROFILE, extra_successor=False, ledger=True,
                      omit_previous=False):
        state_path = self.data_root / "runs" / "req-0123456789abcdef0123" / "state.json"
        state = json.loads(state_path.read_text())
        if not ledger:
            state.pop("test_wait_budgets", None)
            state.pop("test_waits", None)
        state["worker_attempt_limits"] = {CARD: {
            "schema_version": 1, "card_id": CARD, "assignee": PROFILE,
            "previous_run_id": "11", "max_seconds": max_seconds,
            "max_turns": max_turns,
            "purpose": "finalize_existing_implementation", "created_at": 1000,
        }}
        state_path.write_text(json.dumps(state))
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM task_runs WHERE id IN (11, 13)")
        if not omit_previous:
            conn.execute("INSERT INTO task_runs VALUES(?,?,?,?,?,?,?)",
                         (11, previous_card, previous_profile, 100, previous_ended_at,
                          previous_status, previous_status))
        if extra_successor:
            conn.execute("INSERT INTO task_runs VALUES(?,?,?,?,?,?,?)",
                         (13, CARD, PROFILE, 1200, 1300, "failed", "failed"))
        conn.commit()
        conn.close()

    def test_apply_uses_valid_resumed_ledger_and_caps_profile(self):
        agent = SimpleNamespace(run_budget_seconds=2400, max_iterations=80,
                                iteration_budget=SimpleNamespace(used=0))
        self.assertTrue(wb.apply_worker_budget_override(agent))
        self.assertEqual(agent.run_budget_seconds, 700)
        self.assertEqual(agent.max_iterations, 23)
        self.assertEqual(agent.iteration_budget.remaining, 23)
        self.assertEqual(agent._team_worker_budget_binding.card_id, CARD)

    def test_one_shot_successor_is_capped_by_profile_and_real_wait_ledger(self):
        self.grant_attempt(max_seconds=300, max_turns=5)
        agent = SimpleNamespace(run_budget_seconds=2400, max_iterations=80,
                                iteration_budget=SimpleNamespace(used=0))
        self.assertTrue(wb.apply_worker_budget_override(agent))
        self.assertEqual(agent.run_budget_seconds, 300)
        self.assertEqual(agent.max_iterations, 5)

    def test_verifier_report_then_terminal_usage_recording_succeeds(self):
        agent = self.prepare_verifier_one_shot_worker()
        state_path = self.data_root / "runs" / "req-0123456789abcdef0123" / "state.json"
        state = json.loads(state_path.read_text())
        report = Path(state["items"][0]["reviewer_report_path"])
        report.write_text('{"verdict":"pass"}\n')
        with self.assertRaisesRegex(wb.WorkerBudgetError, "report exists before bounded finalization"):
            wb.resolve_worker_binding(require_active=True)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE tasks SET status = ?, current_run_id = NULL WHERE id = ?",
                     ("done", CARD))
        conn.execute("UPDATE task_runs SET ended_at = ?, status = ?, outcome = ? WHERE id = ?",
                     (1300, "completed", "completed", 12))
        conn.commit()
        conn.close()
        record = wb.record_worker_budget_usage(agent, api_call_count=3)
        self.assertTrue(record["finalized"])
        self.assertEqual(record["card_id"], CARD)

    def test_one_shot_cap_cannot_increase_smaller_profile_budget(self):
        self.grant_attempt(max_seconds=600, max_turns=20)
        agent = SimpleNamespace(run_budget_seconds=300, max_iterations=10,
                                iteration_budget=SimpleNamespace(used=0))
        self.assertTrue(wb.apply_worker_budget_override(agent))
        self.assertEqual(agent.run_budget_seconds, 300)
        self.assertEqual(agent.max_iterations, 10)

    def test_one_shot_cap_cannot_increase_test_wait_remaining(self):
        self.grant_attempt(max_seconds=600, max_turns=20)
        state_path = self.data_root / "runs" / "req-0123456789abcdef0123" / "state.json"
        state = json.loads(state_path.read_text())
        state["test_wait_budgets"][CARD]["remaining_seconds"] = 100
        state["test_wait_budgets"][CARD]["remaining_turns"] = 4
        state_path.write_text(json.dumps(state))
        agent = SimpleNamespace(run_budget_seconds=2400, max_iterations=80,
                                iteration_budget=SimpleNamespace(used=0))
        self.assertTrue(wb.apply_worker_budget_override(agent))
        self.assertEqual(agent.run_budget_seconds, 100)
        self.assertEqual(agent.max_iterations, 4)

    def test_one_shot_cap_does_not_require_or_create_test_wait_ledger(self):
        self.grant_attempt(max_seconds=300, max_turns=5, ledger=False)
        agent = SimpleNamespace(run_budget_seconds=2400, max_iterations=80)
        self.assertTrue(wb.apply_worker_budget_override(agent))
        self.assertEqual(agent.run_budget_seconds, 300)
        self.assertEqual(agent.max_iterations, 5)

    def test_bounded_verifier_purpose_is_accepted_only_for_reviewer_and_qa(self):
        grant = {
            "schema_version": 1, "card_id": CARD, "assignee": "team-reviewer",
            "previous_run_id": "11", "max_seconds": 300, "max_turns": 5,
            "purpose": "finalize_existing_verification", "created_at": 1000,
            "item_id": "T-008", "round": 1, "expected_sha": "a" * 40,
            "approved_spec_sha256": "b" * 64, "approved_plan_sha256": "c" * 64,
            "workspace": "/tmp/verifier-worktree",
        }
        self.assertEqual(wb.validate_attempt_limit(
            grant, card_id=CARD, assignee="team-reviewer"), grant)
        self.assertEqual(wb.validate_attempt_limit(
            dict(grant, assignee="team-qa"), card_id=CARD, assignee="team-qa")["purpose"],
            "finalize_existing_verification")
        with self.assertRaisesRegex(wb.WorkerBudgetError, "purpose does not match"):
            wb.validate_attempt_limit(dict(grant, assignee="team-backend"),
                                      card_id=CARD, assignee="team-backend")

    def prepare_verifier_one_shot_worker(self):
        self.grant_attempt(max_seconds=300, max_turns=5, ledger=False)
        state_path = self.data_root / "runs" / "req-0123456789abcdef0123" / "state.json"
        state = json.loads(state_path.read_text())
        feature_dir = state_path.parent
        workspace = feature_dir / "worktrees" / "T-008-reviewer"
        workspace.parent.mkdir(parents=True)
        workspace.mkdir()
        subprocess.run(["git", "-C", str(workspace), "init"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(workspace), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(workspace), "config", "user.email", "test@example.invalid"], check=True)
        (workspace / "candidate.txt").write_text("pinned\n")
        subprocess.run(["git", "-C", str(workspace), "add", "candidate.txt"], check=True)
        subprocess.run(["git", "-C", str(workspace), "commit", "-m", "candidate"],
                       check=True, capture_output=True)
        sha = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"],
                             check=True, capture_output=True, text=True).stdout.strip()
        artifacts = feature_dir / "artifacts"
        artifacts.mkdir()
        for name, body in (("SPEC.md", "approved\n"), ("DESIGN.md", "design\n"),
                           ("PLAN.json", '{"approved":true}\n')):
            (artifacts / name).write_text(body)
        spec_digest = hashlib.sha256((artifacts / "SPEC.md").read_bytes()).hexdigest()
        plan_digest = hashlib.sha256()
        for name in ("SPEC.md", "DESIGN.md", "PLAN.json"):
            plan_digest.update(name.encode() + b"\x00")
            plan_digest.update((artifacts / name).read_bytes())
            plan_digest.update(b"\x00")
        report = feature_dir / "tasks" / "T-008" / "round-1" / "REVIEW.json"
        report.parent.mkdir(parents=True)
        grant = state["worker_attempt_limits"][CARD]
        grant["assignee"] = "team-reviewer"
        grant["purpose"] = "finalize_existing_verification"
        grant.update(item_id="T-008", round=1, expected_sha=sha,
                     approved_spec_sha256=spec_digest,
                     approved_plan_sha256=plan_digest.hexdigest(),
                     workspace=str(workspace))
        state["assignment_snapshots"][CARD]["role"] = "team-reviewer"
        state.update(approved_spec_sha256=spec_digest,
                     approved_plan_sha256=plan_digest.hexdigest(),
                     items=[{"definition": {"id": "T-008"}, "round": 1,
                             "head_sha": sha, "reviewer_card": CARD,
                             "reviewer_workspace": str(workspace),
                             "reviewer_report_path": str(report)}])
        state_path.write_text(json.dumps(state))
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", ("team-reviewer", CARD))
        conn.execute("UPDATE task_runs SET profile = ? WHERE id = ?", ("team-reviewer", 12))
        conn.execute("UPDATE task_runs SET profile = ? WHERE id = ?", ("team-reviewer", 11))
        conn.commit()
        conn.close()
        os.environ["HERMES_PROFILE"] = "team-reviewer"
        agent = SimpleNamespace(run_budget_seconds=2400, max_iterations=80,
                                iteration_budget=SimpleNamespace(used=0))
        self.assertTrue(wb.apply_worker_budget_override(agent))
        return agent

    def test_verifier_one_shot_grant_caps_the_native_worker(self):
        agent = self.prepare_verifier_one_shot_worker()
        self.assertEqual(agent.run_budget_seconds, 300)
        self.assertEqual(agent.max_iterations, 5)

    def test_verifier_worker_rejects_changed_candidate_or_approved_docs(self):
        self.prepare_verifier_one_shot_worker()
        state_path = self.data_root / "runs" / "req-0123456789abcdef0123" / "state.json"
        state = json.loads(state_path.read_text())
        grant = state["worker_attempt_limits"][CARD]
        workspace = Path(grant["workspace"])
        (workspace / "untracked.txt").write_text("changed\n")
        state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(wb.WorkerBudgetError, "worktree changed"):
            wb.resolve_worker_binding()

    def test_verifier_worker_rejects_changed_approved_documents_after_preparation(self):
        self.prepare_verifier_one_shot_worker()
        feature_dir = self.data_root / "runs" / "req-0123456789abcdef0123"
        (feature_dir / "artifacts" / "SPEC.md").write_text("changed after grant\n")
        with self.assertRaisesRegex(wb.WorkerBudgetError, "approved SPEC/PLAN files changed"):
            wb.resolve_worker_binding()

    def test_verifier_grant_rejects_a_second_native_successor(self):
        self.prepare_verifier_one_shot_worker()
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO task_runs VALUES(?,?,?,?,?,?,?)",
                     (13, CARD, "team-reviewer", 1200, 1300, "failed", "failed"))
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(wb.WorkerBudgetError, "not the only successor"):
            wb.resolve_worker_binding()

    def test_one_shot_rejects_non_exhausted_or_unended_previous_run(self):
        for status, ended_at in (("done", 900), ("failed", None)):
            with self.subTest(status=status, ended_at=ended_at):
                self.grant_attempt(previous_status=status, previous_ended_at=ended_at)
                with self.assertRaises(wb.WorkerBudgetError):
                    wb.resolve_worker_binding()

    def test_one_shot_rejects_missing_previous_run(self):
        self.grant_attempt(omit_previous=True)
        with self.assertRaises(wb.WorkerBudgetError):
            wb.resolve_worker_binding()

    def test_one_shot_rejects_wrong_previous_run_binding_and_repeated_successor(self):
        cases = (("t_87654321", PROFILE, False), (CARD, "team-frontend", False),
                 (CARD, PROFILE, True))
        for previous_card, previous_profile, extra_successor in cases:
            with self.subTest(previous_card=previous_card, previous_profile=previous_profile,
                              extra_successor=extra_successor):
                self.grant_attempt(previous_card=previous_card,
                                   previous_profile=previous_profile,
                                   extra_successor=extra_successor)
                with self.assertRaises(wb.WorkerBudgetError):
                    wb.resolve_worker_binding()

    def test_attempt_limit_schema_is_strict(self):
        valid = {"schema_version": 1, "card_id": CARD, "assignee": PROFILE,
                 "previous_run_id": "11", "max_seconds": 300, "max_turns": 5,
                 "purpose": "finalize_existing_implementation", "created_at": 1000}
        self.assertEqual(wb.validate_attempt_limit(valid, card_id=CARD,
                                                    assignee=PROFILE), valid)
        invalid_values = []
        wrong_card = dict(valid, card_id="t_87654321"); invalid_values.append(wrong_card)
        wrong_assignee = dict(valid, assignee="team-frontend"); invalid_values.append(wrong_assignee)
        for key, value in (("max_seconds", True), ("max_seconds", 601),
                           ("max_turns", 0), ("max_turns", 21),
                           ("created_at", -1), ("previous_run_id", 11),
                           ("purpose", "retry_from_scratch"), ("schema_version", 2)):
            invalid_values.append(dict(valid, **{key: value}))
        invalid_values.append(dict(valid, unexpected="field"))
        for invalid in invalid_values:
            with self.subTest(invalid=invalid), self.assertRaises(wb.WorkerBudgetError):
                wb.validate_attempt_limit(invalid, card_id=CARD, assignee=PROFILE)

    def test_one_shot_invalid_caps_fail_before_budget_override(self):
        self.grant_attempt()
        state_path = self.data_root / "runs" / "req-0123456789abcdef0123" / "state.json"
        state = json.loads(state_path.read_text())
        state["worker_attempt_limits"][CARD]["max_seconds"] = True
        state_path.write_text(json.dumps(state))
        agent = SimpleNamespace(run_budget_seconds=2400, max_iterations=80)
        with self.assertRaises(wb.WorkerBudgetError):
            wb.apply_worker_budget_override(agent)
        self.assertEqual(agent.run_budget_seconds, 2400)
        self.assertEqual(agent.max_iterations, 80)

    def test_forged_persisted_attempt_caps_above_hard_limits_fail_closed(self):
        for field, value in (("max_seconds", 601), ("max_turns", 21)):
            with self.subTest(field=field, value=value):
                self.grant_attempt()
                state_path = self.data_root / "runs" / "req-0123456789abcdef0123" / "state.json"
                state = json.loads(state_path.read_text())
                # Simulate a forged/stale persisted grant bypassing the
                # controller's own cap validation.
                state["worker_attempt_limits"][CARD][field] = value
                state_path.write_text(json.dumps(state))
                agent = SimpleNamespace(run_budget_seconds=2400, max_iterations=80)
                with self.assertRaises(wb.WorkerBudgetError):
                    wb.apply_worker_budget_override(agent)
                self.assertEqual(agent.run_budget_seconds, 2400)
                self.assertEqual(agent.max_iterations, 80)

    def test_apply_rejects_profile_mismatch_before_overriding(self):
        with patch.dict(os.environ, {"HERMES_PROFILE": "team-frontend"}):
            agent = SimpleNamespace(run_budget_seconds=2400, max_iterations=80)
            with self.assertRaisesRegex(wb.WorkerBudgetError, "assignee"):
                wb.apply_worker_budget_override(agent)
            self.assertEqual(agent.run_budget_seconds, 2400)
            self.assertEqual(agent.max_iterations, 80)

    def test_apply_fails_closed_when_cumulative_budget_is_spent(self):
        state_path = self.data_root / "runs" / "req-0123456789abcdef0123" / "state.json"
        state = json.loads(state_path.read_text())
        state["test_wait_budgets"][CARD]["remaining_seconds"] = 0
        state_path.write_text(json.dumps(state))
        agent = SimpleNamespace(run_budget_seconds=2400, max_iterations=80)
        with self.assertRaisesRegex(wb.WorkerBudgetError, "exhausted"):
            wb.apply_worker_budget_override(agent)

    def test_usage_record_is_exact_redacted_and_idempotent(self):
        agent = SimpleNamespace(run_budget_seconds=2400, max_iterations=80,
                                iteration_budget=SimpleNamespace(used=7))
        self.assertTrue(wb.apply_worker_budget_override(agent))
        agent.iteration_budget.used = 7
        agent._team_worker_budget_started_monotonic -= 3.25
        agent._team_worker_budget_started_at -= 5

        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE tasks SET status='blocked', current_run_id=NULL WHERE id=?", (CARD,))
        conn.execute("UPDATE task_runs SET ended_at=2000, status='blocked', outcome='blocked' WHERE id=?", (int(RUN),))
        conn.commit()
        conn.close()

        record = wb.record_worker_budget_usage(agent, api_call_count=9,
                                               exit_reason="blocked: test_wait")
        self.assertEqual(record["iterations"], 7)
        self.assertEqual(record["api_call_count"], 9)
        self.assertGreaterEqual(record["active_seconds"], 3.0)
        self.assertTrue(record["finalized"])
        self.assertLessEqual(set(record), {"schema_version", "card_id", "run_id", "assignee",
                                           "budget_seconds", "max_turns", "active_seconds",
                                           "iterations", "api_call_count",
                                           "started_at", "ended_at", "finalized", "exit_reason"})
        self.assertEqual(record["exit_reason"], "blocked:test_wait")
        self.assertEqual(record["budget_seconds"], 700)
        self.assertEqual(record["max_turns"], 23)
        path = self.data_root / "worker-budget" / CARD / f"{RUN}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(wb.read_usage(CARD, RUN, data_root=self.data_root), record)
        self.assertEqual(wb.record_worker_budget_usage(agent, api_call_count=99), record)

    def test_ordinary_team_run_tracks_profile_budget_without_wait_ledger(self):
        state_path = self.data_root / "runs" / "req-0123456789abcdef0123" / "state.json"
        state = json.loads(state_path.read_text())
        state.pop("test_wait_budgets")
        state.pop("test_waits")
        state_path.write_text(json.dumps(state))

        agent = SimpleNamespace(run_budget_seconds=900, max_iterations=30,
                                iteration_budget=FakeIterationBudget(30))
        self.assertTrue(wb.apply_worker_budget_override(agent))
        self.assertEqual(agent.run_budget_seconds, 900)
        self.assertEqual(agent.max_iterations, 30)

        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE tasks SET status='done', current_run_id=NULL WHERE id=?", (CARD,))
        conn.execute("UPDATE task_runs SET ended_at=2000, status='done', outcome='done' WHERE id=?", (int(RUN),))
        conn.commit()
        conn.close()
        record = wb.record_worker_budget_usage(agent, api_call_count=2)
        self.assertEqual(record["card_id"], CARD)
        self.assertEqual(record["run_id"], RUN)
        self.assertIsNotNone(wb.read_usage(CARD, RUN, data_root=self.data_root))

    def test_usage_record_requires_native_run_end_timestamp(self):
        agent = SimpleNamespace(run_budget_seconds=2400, max_iterations=80,
                                iteration_budget=SimpleNamespace(used=1))
        self.assertTrue(wb.apply_worker_budget_override(agent))
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE tasks SET status='blocked', current_run_id=NULL WHERE id=?", (CARD,))
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(wb.WorkerBudgetError, "not terminal"):
            wb.record_worker_budget_usage(agent, api_call_count=1)

    def test_read_usage_rejects_wrong_binding_and_unfinalized_records(self):
        root = self.tmp_path / "separate-data"
        path = root / "worker-budget" / CARD / f"{RUN}.json"
        path.parent.mkdir(parents=True)
        base = {"schema_version": 1, "card_id": CARD, "run_id": RUN,
                "assignee": PROFILE, "active_seconds": 2.0, "iterations": 1,
                "budget_seconds": 5, "max_turns": 2, "api_call_count": 1,
                "started_at": 1.0, "ended_at": 3.0,
                "finalized": False}
        path.write_text(json.dumps(base))
        self.assertIsNone(wb.read_usage(CARD, RUN, data_root=root))
        base["finalized"] = True
        base["run_id"] = "13"
        path.write_text(json.dumps(base))
        self.assertIsNone(wb.read_usage(CARD, RUN, data_root=root))

    def test_non_kanban_process_does_not_get_team_overrides(self):
        with patch.dict(os.environ, {}, clear=True):
            agent = SimpleNamespace(run_budget_seconds=500, max_iterations=10)
            self.assertFalse(wb.apply_worker_budget_override(agent))
            self.assertEqual(agent.run_budget_seconds, 500)

    def test_native_non_team_profile_is_left_untouched(self):
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": CARD,
                                     "HERMES_PROFILE": "default"}, clear=True):
            agent = SimpleNamespace(run_budget_seconds=500, max_iterations=10)
            self.assertFalse(wb.apply_worker_budget_override(agent))
            self.assertEqual(agent.run_budget_seconds, 500)

    def test_native_card_created_by_other_actor_is_left_untouched(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE tasks SET created_by='operator' WHERE id=?", (CARD,))
        conn.commit()
        conn.close()
        agent = SimpleNamespace(run_budget_seconds=500, max_iterations=10)
        self.assertFalse(wb.apply_worker_budget_override(agent))
        self.assertEqual(agent.run_budget_seconds, 500)

    def test_native_patch_is_exact_anchor_idempotent_and_marker_guarded(self):
        source = native_patch.START_ANCHOR + "    next_statement()\n"
        once = native_patch.patched_text(source, "agent/turn_context.py")
        self.assertEqual(once.count(native_patch.START_MARKER), 1)
        self.assertEqual(native_patch.patched_text(once, "agent/turn_context.py"), once)
        with self.assertRaisesRegex(native_patch.PatchError, "anchor"):
            native_patch.patched_text("unrelated source\n", "agent/turn_context.py")

        finalizer = "    cleanup()\n" + native_patch.FINAL_ANCHOR
        inserted = native_patch.patched_text(finalizer, "agent/turn_finalizer.py")
        self.assertEqual(inserted.count(native_patch.FINAL_MARKER), 1)
        self.assertEqual(native_patch.patched_text(inserted, "agent/turn_finalizer.py"), inserted)

    def test_native_patch_validation_rejects_changed_baseline_with_managed_markers(self):
        root = self.tmp_path / "hermes-source"
        context_path = root / "agent" / "turn_context.py"
        finalizer_path = root / "agent" / "turn_finalizer.py"
        context_path.parent.mkdir(parents=True)
        baseline = {
            "agent/turn_context.py": native_patch.START_ANCHOR + "    untouched()\n",
            "agent/turn_finalizer.py": "    cleanup()\n" + native_patch.FINAL_ANCHOR,
        }
        baseline_hashes = {name: native_patch.hashlib.sha256(source.encode()).hexdigest()
                           for name, source in baseline.items()}
        context_path.write_text(native_patch.patched_text(
            baseline["agent/turn_context.py"], "agent/turn_context.py"))
        finalizer_path.write_text(native_patch.patched_text(
            baseline["agent/turn_finalizer.py"], "agent/turn_finalizer.py"))
        with patch.object(native_patch, "EXPECTED_HASHES", baseline_hashes), \
             patch.object(native_patch, "EXPECTED_REVISION", "a" * 40), \
             patch.object(native_patch, "git_revision", return_value="a" * 40):
            native_patch.validate_tree(root)
            context_path.write_text(context_path.read_text().replace(
                native_patch.START_INSERT, native_patch.LEGACY_START_INSERT))
            finalizer_path.write_text(finalizer_path.read_text().replace(
                native_patch.FINAL_INSERT, native_patch.LEGACY_FINAL_INSERT))
            native_patch.validate_tree(root)
            upgraded_context = native_patch.patched_text(
                context_path.read_text(), "agent/turn_context.py")
            self.assertEqual(upgraded_context, native_patch.patched_text(
                baseline["agent/turn_context.py"], "agent/turn_context.py"))
            self.assertIn(native_patch.FINAL_INSERT, native_patch.patched_text(
                finalizer_path.read_text(), "agent/turn_finalizer.py"))
            context_path.write_text(context_path.read_text().replace("untouched()", "changed()"))
            with self.assertRaisesRegex(native_patch.PatchError, "baseline differs"):
                native_patch.validate_tree(root)

    def test_native_hook_snippets_import_os_when_global_namespace_lacks_it(self):
        context_body = native_patch.START_INSERT.split(
            native_patch.START_MARKER, 1)[1].split(native_patch.END_MARKER, 1)[0]
        finalizer_body = native_patch.FINAL_INSERT.split(
            native_patch.FINAL_MARKER, 1)[1].split(native_patch.FINAL_ANCHOR, 1)[0]
        context_ns = {}
        exec("def invoke(agent):\n" + context_body, context_ns)
        finalizer_ns = {}
        exec("def invoke():\n    result = 'ok'\n" + finalizer_body + "    return result\n",
             finalizer_ns)
        with patch.dict(os.environ, {}, clear=True):
            context_ns["invoke"](SimpleNamespace())
            self.assertEqual(finalizer_ns["invoke"](), "ok")

    def test_apply_migrates_exact_legacy_hooks_and_backs_up_old_helper(self):
        root = self.tmp_path / "hermes-agent-fixture"
        agent_dir = root / "agent"
        agent_dir.mkdir(parents=True)
        baseline = {
            "agent/turn_context.py": native_patch.START_ANCHOR + "    unchanged()\n",
            "agent/turn_finalizer.py": "    cleanup()\n" + native_patch.FINAL_ANCHOR,
        }
        expected_hashes = {name: native_patch.hashlib.sha256(source.encode()).hexdigest()
                           for name, source in baseline.items()}
        for relative, source in baseline.items():
            current = native_patch.patched_text(source, relative)
            if relative.endswith("turn_context.py"):
                current = current.replace(native_patch.START_INSERT,
                                          native_patch.LEGACY_START_INSERT)
            else:
                current = current.replace(native_patch.FINAL_INSERT,
                                          native_patch.LEGACY_FINAL_INSERT)
            (root / relative).write_text(current)

        runtime = self.tmp_path / "runtime-fixture"
        package_dir = runtime / "team_engine"
        package_dir.mkdir(parents=True)
        helper_target = package_dir / "worker_budget.py"
        helper_target.write_text("# prior managed helper\n")
        with patch.object(native_patch, "EXPECTED_HASHES", expected_hashes), \
             patch.object(native_patch, "EXPECTED_REVISION", "b" * 40), \
             patch.object(native_patch, "git_revision", return_value="b" * 40):
            backup = Path(native_patch.apply(root, runtime, Path(wb.__file__)))

        self.assertEqual(native_patch._state((agent_dir / "turn_context.py").read_text(),
                                             "agent/turn_context.py"), "patched-current")
        self.assertEqual(native_patch._state((agent_dir / "turn_finalizer.py").read_text(),
                                             "agent/turn_finalizer.py"), "patched-current")
        self.assertEqual(helper_target.read_bytes(), Path(wb.__file__).read_bytes())
        self.assertEqual((backup / "worker_budget.py").read_text(), "# prior managed helper\n")


if __name__ == "__main__":
    unittest.main()
