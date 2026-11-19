from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.install_profiles import default_role_configurations, install_profiles
from team_engine.web_config import ConfigConflict, ConfigEditorError, load_editor, save_editor


class WebConfigEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.hermes_root = self.root / ".hermes"
        self.bundle_root = self.root / "bundle"
        self.roles_dir = self.bundle_root / "roles"
        self.roles_dir.mkdir(parents=True)
        for name in ("analyst.md", "planner-architect.md", "skeptic.md", "test-designer.md",
                     "judge.md", "developer.md", "backend.md", "frontend.md", "mobile.md",
                     "go.md", "reviewer.md", "qa-regression.md", "integrator.md", "coordinator.md"):
            (self.roles_dir / name).write_text("Instructions for " + name + "\n", encoding="utf-8")
        (self.roles_dir / "COMMON.md").write_text("Common team instructions\n", encoding="utf-8")

        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "initial"], check=True)
        self.roles = default_role_configurations("kimi-coding", "kimi-k2.6")
        for value in self.roles.values():
            value["toolsets"] = ["file", "terminal", "kanban"]
            value["credential_env_vars"] = ["KIMI_API_KEY"]
        self.config_path = self.root / "team" / "config.json"
        self.config_path.parent.mkdir()
        self.config = {
            "schema_version": 1,
            "hermes_root": str(self.hermes_root),
            "bundle_root": str(self.bundle_root),
            "data_root": str(self.hermes_root / "team" / "data"),
            "board": "engineering-team",
            "poll_seconds": 5,
            "max_active_features": 1,
            "max_plan_tasks": 12,
            "max_plan_corrections": 2,
            "max_rework_rounds": 2,
            "max_plan_report_retries": 1,
            "max_report_retries": 1,
            "quota": {"pools": {"kimi-main": {
                "provider": "kimi-coding", "enabled": True, "auth_ready": True,
                "max_concurrent": 2, "reserve_percent": 20, "max_snapshot_age": 180,
                "unknown_policy": "wait", "probe": "kimi", "internal_tag": "retain-me",
            }}},
            "roles": self.roles,
            "projects": {"demo": {"repo": str(self.repo), "base_ref": "HEAD",
                                    "checks": [["python", "-m", "unittest"]], "check_timeout": 180}},
            "internal_secret_marker": "must-remain-private-and-untouched",
        }
        self._write_config(self.config)
        self.config_path.chmod(0o600)
        self.hermes_root.mkdir(parents=True, exist_ok=True)
        (self.hermes_root / ".env").write_text("KIMI_API_KEY=test-fixture-key\n", encoding="utf-8")
        install_profiles(self.hermes_root, self.roles, role_docs_dir=self.roles_dir,
                         common_instructions=self.roles_dir / "COMMON.md")

    def tearDown(self):
        self.temp.cleanup()

    def _write_config(self, value):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _payload(self):
        current = load_editor(self.config_path)
        return {"revision": current["revision"], "config": copy.deepcopy(current["config"])}

    def test_load_returns_safe_projection_and_catalog(self):
        loaded = load_editor(self.config_path)
        self.assertEqual(len(loaded["config"]["roles"]), 13)
        self.assertIn("team-go", loaded["catalog"]["roles"])
        self.assertIn("team-go", loaded["config"]["roles"])
        self.assertEqual([x["id"] for x in loaded["catalog"]["providers"]],
                         ["kimi-coding", "openai-codex", "anthropic"])
        self.assertNotIn("max_active_features", loaded["config"]["workflow"])
        self.assertNotIn("probe", loaded["config"]["quota"]["pools"]["kimi-main"])
        self.assertNotIn("toolsets", loaded["config"]["roles"]["team-backend"])
        self.assertNotIn("credential_env_vars", loaded["config"]["roles"]["team-backend"])
        self.assertNotIn("internal_secret_marker", loaded["config"])
        self.assertEqual(loaded["config"]["projects"]["demo"]["context_file"], ".agents/project.json")
        self.assertFalse(loaded["config"]["projects"]["demo"]["context_required"])
        self.assertFalse(loaded["config"]["workflow"]["auto_start_after_judge_accept"])

    def test_save_updates_editable_values_and_preserves_internal_fields(self):
        payload = self._payload()
        payload["config"]["roles"]["team-backend"]["model"] = "codex-test-model"
        payload["config"]["quota"]["pools"]["kimi-main"]["reserve_percent"] = 25
        payload["config"]["workflow"]["max_plan_tasks"] = 20
        payload["config"]["workflow"]["auto_start_after_judge_accept"] = True
        result = save_editor(self.config_path, payload)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(result["config"]["roles"]["team-backend"]["model"], "codex-test-model")
        self.assertEqual(saved["roles"]["team-backend"]["toolsets"], ["file", "terminal", "kanban"])
        self.assertEqual(saved["roles"]["team-backend"]["credential_env_vars"], ["KIMI_API_KEY"])
        self.assertEqual(saved["quota"]["pools"]["kimi-main"]["probe"], "kimi")
        self.assertEqual(saved["quota"]["pools"]["kimi-main"]["internal_tag"], "retain-me")
        self.assertEqual(saved["max_plan_tasks"], 20)
        self.assertTrue(saved["auto_start_after_judge_accept"])
        self.assertEqual(saved["internal_secret_marker"], "must-remain-private-and-untouched")
        self.assertNotEqual(result["revision"], payload["revision"])
        self.assertEqual((self.config_path.parent / "backups").stat().st_mode & 0o777, 0o700)
        self.assertEqual(next((self.config_path.parent / "backups").iterdir()).stat().st_mode & 0o777, 0o600)

    def test_context_settings_are_saved_and_project_internal_fields_are_preserved(self):
        self.config["projects"]["demo"]["internal_marker"] = "keep-project-setting"
        self._write_config(self.config)
        payload = self._payload()
        payload["config"]["projects"]["demo"].update(
            context_file="docs/ai/project.json", context_required=True)
        save_editor(self.config_path, payload)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        project = saved["projects"]["demo"]
        self.assertEqual(project["context_file"], "docs/ai/project.json")
        self.assertTrue(project["context_required"])
        self.assertEqual(project["internal_marker"], "keep-project-setting")

    def test_model_edit_preserves_timezone_in_regenerated_profile(self):
        self.config["roles"]["team-backend"]["timezone"] = "Asia/Almaty"
        self._write_config(self.config)
        payload = self._payload()
        payload["config"]["roles"]["team-backend"]["reasoning_effort"] = "high"
        save_editor(self.config_path, payload)
        saved = json.loads(self.config_path.read_text())
        profile = self.hermes_root / "profiles" / "team-backend"
        generated = json.loads((profile / "config.yaml").read_text())
        self.assertEqual(saved["roles"]["team-backend"]["timezone"], "Asia/Almaty")
        self.assertEqual(generated["timezone"], "Asia/Almaty")

    def test_rejects_unsafe_or_invalid_context_settings(self):
        for key, value in (("context_file", "../outside.json"),
                           ("context_file", "/etc/passwd"),
                           ("context_required", "yes")):
            with self.subTest(key=key, value=value):
                payload = self._payload()
                payload["config"]["projects"]["demo"][key] = value
                with self.assertRaises(ConfigEditorError):
                    save_editor(self.config_path, payload)

    def test_provider_switch_clears_kimi_credential_allowlist(self):
        payload = self._payload()
        payload["config"]["quota"]["pools"]["codex-main"] = {
            "provider": "openai-codex", "enabled": False, "auth_ready": False,
            "max_concurrent": 1, "reserve_percent": 20, "max_snapshot_age": 180,
            "unknown_policy": "wait",
        }
        payload["config"]["roles"]["team-backend"].update(
            provider="openai-codex", pool="codex-main")
        save_editor(self.config_path, payload)
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["roles"]["team-backend"]["credential_env_vars"], [])

    def test_execution_policy_roundtrip_and_parallel_bound(self):
        payload = self._payload()
        policy = {"version": 1, "full_after_tasks": 2, "path_rules": [
            {"name": "backend", "prefixes": ["backend/"], "quick_check_indexes": [0]}]}
        payload["config"]["projects"]["demo"]["execution_policy"] = policy
        payload["config"]["workflow"]["max_parallel_tasks"] = 2
        result = save_editor(self.config_path, payload)
        self.assertEqual(result["config"]["projects"]["demo"]["execution_policy"], policy)
        self.assertEqual(result["config"]["workflow"]["max_parallel_tasks"], 2)
        for field, value in (("full_after_tasks", True), ("path_rules", [])):
            bad = self._payload()
            bad["config"]["projects"]["demo"]["execution_policy"][field] = value
            with self.assertRaises(ConfigEditorError):
                save_editor(self.config_path, bad)
        bad = self._payload()
        bad["config"]["projects"]["demo"]["execution_policy"]["path_rules"][0]["quick_check_indexes"] = [1]
        with self.assertRaises(ConfigEditorError):
            save_editor(self.config_path, bad)
        bad = self._payload()
        bad["config"]["workflow"]["max_parallel_tasks"] = 3
        with self.assertRaises(ConfigEditorError):
            save_editor(self.config_path, bad)

    def test_detects_revision_conflict_before_writing(self):
        payload = self._payload()
        payload["config"]["workflow"]["max_plan_tasks"] = 18
        self.config["poll_seconds"] = 7
        self._write_config(self.config)
        after_external_edit = self.config_path.read_bytes()
        with self.assertRaises(ConfigConflict):
            save_editor(self.config_path, payload)
        self.assertEqual(self.config_path.read_bytes(), after_external_edit)
        self.assertFalse((self.config_path.parent / "backups").exists())

    def test_rejects_secret_fields_and_does_not_mutate_quota_ledger(self):
        payload = self._payload()
        payload["config"]["roles"]["team-backend"]["credential_env_vars"] = ["OPENAI_API_KEY"]
        with self.assertRaises(ConfigEditorError):
            save_editor(self.config_path, payload)
        self.assertFalse((self.hermes_root / "team" / "data" / "quota.json").exists())
        self.assertFalse((self.config_path.parent / "backups").exists())

    def test_rejects_immutable_runtime_and_invalid_types_without_mutations(self):
        for change in (
            lambda c: c["workflow"].update(max_plan_tasks=True),
            lambda c: c["workflow"].update(auto_start_after_judge_accept="true"),
            lambda c: c["workflow"].update(auto_start_after_judge_accept=1),
            lambda c: c.update(max_active_features=4),
            lambda c: c["quota"]["pools"]["kimi-main"].update(reserve_percent=100),
            lambda c: c["roles"]["team-go"].update(model="a model with spaces"),
            lambda c: c["roles"]["team-analyst"].update(provider="unknown-provider"),
        ):
            with self.subTest(change=change):
                payload = self._payload()
                change(payload["config"])
                before = self.config_path.read_bytes()
                with self.assertRaises(ConfigEditorError):
                    save_editor(self.config_path, payload)
                self.assertEqual(self.config_path.read_bytes(), before)
                self.assertFalse((self.config_path.parent / "backups").exists())

    def test_project_path_is_immutable_and_new_repo_must_be_under_workspaces(self):
        payload = self._payload()
        payload["config"]["projects"]["demo"]["repo"] = str(self.root)
        with self.assertRaises(ConfigEditorError):
            save_editor(self.config_path, payload)

    def test_missing_or_non_git_project_is_reported_as_validation_error(self):
        payload = self._payload()
        payload["config"]["projects"]["demo"]["repo"] = str(self.repo / "missing")
        with self.assertRaises(ConfigEditorError):
            save_editor(self.config_path, payload)
        non_git = self.root / "not-a-repo"
        non_git.mkdir()
        payload = self._payload()
        payload["config"]["projects"]["demo"]["repo"] = str(non_git)
        with self.assertRaises(ConfigEditorError):
            save_editor(self.config_path, payload)
        payload = self._payload()
        payload["config"]["projects"]["outside"] = {
            "repo": str(self.repo), "base_ref": "HEAD", "checks": [["go", "test", "./..."]],
            "check_timeout": 60,
        }
        with self.assertRaises(ConfigEditorError):
            save_editor(self.config_path, payload)

    def test_callback_runs_after_validation_under_save_path(self):
        payload = self._payload()
        payload["config"]["workflow"]["max_plan_tasks"] = 13
        called = []
        def callback(old, new):
            called.append((old["max_plan_tasks"], new["max_plan_tasks"]))
        save_editor(self.config_path, payload, before_apply=callback)
        self.assertEqual(called, [(12, 13)])

    def test_profile_install_failure_rolls_back_profiles_and_config(self):
        payload = self._payload()
        payload["config"]["roles"]["team-go"]["model"] = "new-go-model"
        role_file = self.hermes_root / "profiles" / "team-go" / "config.yaml"
        before_role = role_file.read_bytes()
        before_config = self.config_path.read_bytes()
        with mock.patch("team_engine.web_config.install_profiles", side_effect=RuntimeError("install failed")):
            with self.assertRaisesRegex(RuntimeError, "install failed"):
                save_editor(self.config_path, payload)
        self.assertEqual(role_file.read_bytes(), before_role)
        self.assertEqual(self.config_path.read_bytes(), before_config)
        self.assertFalse((self.config_path.parent / "backups").exists())

    def test_config_replace_followed_by_fsync_error_rolls_back_config_and_profile(self):
        payload = self._payload()
        payload["config"]["roles"]["team-go"]["model"] = "new-go-model"
        role_file = self.hermes_root / "profiles" / "team-go" / "config.yaml"
        before_role = role_file.read_bytes()
        before_config = self.config_path.read_bytes()
        from team_engine.web_config import _atomic_json_write as atomic_write

        def replace_then_fail(path, value):
            atomic_write(path, value)
            raise OSError("simulated directory fsync failure after replace")

        with mock.patch("team_engine.web_config._atomic_json_write", side_effect=replace_then_fail):
            with self.assertRaisesRegex(OSError, "simulated directory fsync failure"):
                save_editor(self.config_path, payload)
        self.assertEqual(self.config_path.read_bytes(), before_config)
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(role_file.read_bytes(), before_role)

    def test_candidate_provider_must_match_selected_pool(self):
        payload = self._payload()
        payload["config"]["roles"]["team-analyst"]["candidates"] = [
            {"pool": "kimi-main", "provider": "openai-codex", "model": "codex-test", "tier": 0}
        ]
        with self.assertRaises(ConfigEditorError):
            save_editor(self.config_path, payload)

    def test_candidate_must_select_pool_and_primary_provider_must_resolve_unambiguously(self):
        payload = self._payload()
        payload["config"]["roles"]["team-analyst"]["candidates"] = [
            {"provider": "kimi-coding", "model": "kimi-backup", "tier": 0}
        ]
        with self.assertRaises(ConfigEditorError):
            save_editor(self.config_path, payload)
        payload = self._payload()
        payload["config"]["roles"]["team-go"]["provider"] = "openai-codex"
        with self.assertRaises(ConfigEditorError):
            save_editor(self.config_path, payload)


if __name__ == "__main__":
    unittest.main()
