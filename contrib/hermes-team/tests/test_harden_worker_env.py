from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.harden_worker_env import (
    PINNED_VERSION,
    apply_patch,
    filter_worker_environment,
    patched_source,
)


class WorkerEnvironmentFilterTests(unittest.TestCase):
    def test_team_profile_drops_inherited_secrets_but_keeps_profile_keys_and_runtime_env(self):
        env = {
            "PATH": "/usr/bin",
            "HOME": "/home/alex",
            "HTTPS_PROXY": "http://proxy.internal:3128",
            "ANTHROPIC_BASE_URL": "https://gateway.example/v1",
            "OPENAI_API_KEY": "ambient-openai-secret",
            "ANTHROPIC_API_KEY": "ambient-anthropic-secret",
            "GITHUB_TOKEN": "ambient-github-secret",
            "AWS_SECRET_ACCESS_KEY": "ambient-cloud-secret",
            "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/service-account.json",
            "HERMES_KANBAN_TASK": "t_12345678",
            "HERMES_KANBAN_TOKEN": "kanban-context-value",
        }

        result = filter_worker_environment(env, "team-backend", {"OPENAI_API_KEY"})

        self.assertEqual(result["OPENAI_API_KEY"], "ambient-openai-secret")
        self.assertEqual(result["PATH"], env["PATH"])
        self.assertEqual(result["HOME"], env["HOME"])
        self.assertEqual(result["HTTPS_PROXY"], env["HTTPS_PROXY"])
        self.assertEqual(result["ANTHROPIC_BASE_URL"], env["ANTHROPIC_BASE_URL"])
        self.assertEqual(result["HERMES_KANBAN_TASK"], env["HERMES_KANBAN_TASK"])
        self.assertEqual(result["HERMES_KANBAN_TOKEN"], env["HERMES_KANBAN_TOKEN"])
        for key in (
            "ANTHROPIC_API_KEY",
            "GITHUB_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ):
            self.assertNotIn(key, result)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "ambient-anthropic-secret")

    def test_non_team_profile_environment_is_unchanged(self):
        env = {"OPENAI_API_KEY": "ambient-secret", "PATH": "/usr/bin"}
        self.assertEqual(filter_worker_environment(env, "default", set()), env)


class HermesSourcePatchTests(unittest.TestCase):
    def source(self) -> str:
        return '''import os

def _default_spawn(task, workspace, *, board=None):
    from hermes_cli.profiles import normalize_profile_name
    profile_arg = normalize_profile_name(task.assignee)
    prompt = "work"
    env = dict(os.environ)
    env["HERMES_HOME"] = resolve_profile_env(profile_arg)
    log_f = open("worker.log", "ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            cmd,
            env=env,
        )
    except FileNotFoundError:
        raise RuntimeError("missing hermes")
'''

    def test_patch_is_valid_and_idempotent(self):
        updated = patched_source(self.source())
        self.assertIn("def _hermes_team_filter_worker_env(env, profile_env):", updated)
        self.assertIn("if profile_arg.startswith(\"team-\"):", updated)
        self.assertLess(updated.index("filter ambient credentials"), updated.index("proc = subprocess.Popen"))
        self.assertEqual(patched_source(updated), updated)

    def test_injected_helper_reads_only_profile_dotenv_key_names(self):
        updated = patched_source(self.source())
        namespace: dict[str, object] = {}
        exec(compile(updated, "kanban_db.py", "exec"), namespace)
        with tempfile.TemporaryDirectory() as tmp:
            profile_home = Path(tmp)
            (profile_home / ".env").write_text(
                "# comment\nexport OPENAI_API_KEY=profile-value\n", encoding="utf-8"
            )
            result = namespace["_hermes_team_filter_worker_env"](
                {
                    "PATH": "/usr/bin",
                    "OPENAI_API_KEY": "inherited-value",
                    "ANTHROPIC_API_KEY": "unapproved-value",
                    "HERMES_KANBAN_TASK": "t_12345678",
                },
                str(profile_home),
            )
        self.assertEqual(result["OPENAI_API_KEY"], "inherited-value")
        self.assertNotIn("ANTHROPIC_API_KEY", result)
        self.assertEqual(result["PATH"], "/usr/bin")
        self.assertEqual(result["HERMES_KANBAN_TASK"], "t_12345678")

    def test_unknown_anchor_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Popen anchor"):
            patched_source(self.source().replace("subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above", "subprocess.Popen("))

    def test_partial_patch_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Partial or duplicate"):
            patched_source(self.source() + "\n# BEGIN hermes-team worker env hardening (managed)\n")

    def test_apply_patch_makes_backup_and_rechecks_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hermes_cli").mkdir()
            (root / "hermes_cli" / "__init__.py").write_text(
                f'__version__ = "{PINNED_VERSION}"\n', encoding="utf-8"
            )
            target = root / "hermes_cli" / "kanban_db.py"
            original = self.source()
            target.write_text(original, encoding="utf-8")

            self.assertEqual(apply_patch(root), target.resolve())
            backup = target.with_suffix(".py.hermes-team.bak")
            self.assertEqual(backup.read_text(encoding="utf-8"), original)
            patched = target.read_text(encoding="utf-8")
            self.assertEqual(patched_source(patched), patched)

            (root / "hermes_cli" / "__init__.py").write_text('__version__ = "0.20.7"\n', encoding="utf-8")
            target.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Expected Hermes"):
                apply_patch(root)
            self.assertEqual(target.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
