from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.install_profiles import _team_terminal_init, default_role_configurations, install_profiles


class InstallProfileTerminalEnvironmentTests(unittest.TestCase):
    def test_role_timezone_survives_profile_regeneration_and_reaches_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            docs = Path(tmp) / "roles"
            docs.mkdir()
            (docs / "analyst.md").write_text("Analyst\n")
            role = {"provider": "anthropic", "model": "test-model",
                    "role_doc": "analyst.md", "timezone": "Asia/Almaty"}
            for effort in ("high", "xhigh"):
                role["reasoning_effort"] = effort
                install_profiles(root, {"team-analyst": role}, role_docs_dir=docs)
                profile = root / "profiles" / "team-analyst"
                config = json.loads((profile / "config.yaml").read_text())
                self.assertEqual(config["timezone"], "Asia/Almaty")
                self.assertEqual(config["agent"]["reasoning_effort"], effort)
                self.assertEqual(datetime(2026, 10, 29, tzinfo=ZoneInfo(config["timezone"])).utcoffset(),
                                 timedelta(hours=5))
                command = f'. {shlex.quote(str(profile / "hermes-team-terminal.sh"))}; printf "%s\\n" "$TZ" "$HERMES_TIMEZONE"'
                result = subprocess.run(["bash", "-c", command], check=True,
                                        capture_output=True, text=True,
                                        env={**os.environ, "TZ": "Etc/UTC"})
                self.assertEqual(result.stdout.splitlines(), ["Asia/Almaty", "Asia/Almaty"])

    def test_invalid_timezone_is_rejected_before_profile_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            for timezone in ("not/a-real-zone", "", 5, "/etc/passwd"):
                with self.subTest(timezone=timezone), self.assertRaisesRegex(ValueError, "timezone"):
                    install_profiles(root, {"team-analyst": {"timezone": timezone}},
                                     role_docs_dir=Path(tmp))
                self.assertFalse((root / "profiles").exists())

    def test_managed_jdk_is_available_after_login_shell_and_init_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="hermes tools ") as tmp:
            root = Path(tmp).resolve()
            java_home = root / "toolchains" / "jdk-21"
            (java_home / "bin").mkdir(parents=True)
            java = java_home / "bin" / "java"
            java.write_text("#!/bin/sh\nexit 0\n")
            java.chmod(0o755)
            script = root / "terminal.sh"
            script.write_text(_team_terminal_init(root))
            quoted = shlex.quote(str(script))
            command = f'. {quoted}; . {quoted}; command -v java; printf "%s\\n" "$JAVA_HOME" "$PATH" "$LC_ALL"'
            result = subprocess.run(["bash", "-lc", command], check=True,
                                    capture_output=True, text=True,
                                    env={**os.environ, "PATH": "/usr/bin:/bin", "LC_ALL": "C"})
            lines = result.stdout.splitlines()
            self.assertEqual(lines[:2], [str(java), str(java_home)])
            self.assertEqual(lines[2].split(":" ).count(str(java_home / "bin")), 1)
            self.assertEqual(lines[3], "C.UTF-8")

    def test_all_team_profiles_source_managed_tool_paths_in_login_terminal(self):
        if not shutil.which("bash"):
            self.skipTest("Hermes local terminal smoke requires bash")
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "hermes").resolve()
            docs = Path(tmp) / "roles"
            docs.mkdir()
            roles = default_role_configurations("kimi-coding", "test-model")
            for settings in roles.values():
                for name in settings["role_docs"]:
                    (docs / name).write_text("role instructions\n", encoding="utf-8")
            credentials = Path(tmp) / "credentials.env"
            credentials.write_text("KIMI_API_KEY=test-key\n", encoding="utf-8")

            installed = install_profiles(root, roles, role_docs_dir=docs,
                                          credential_source_env=credentials)
            self.assertEqual(len(installed), 13)
            home = Path(tmp).resolve() / "home"
            local_bin = home / ".local" / "bin"
            node_bin = root / "node" / "bin"
            team_bin = root / "team" / "runtime" / "scripts"
            local_bin.mkdir(parents=True)
            node_bin.mkdir(parents=True)
            team_bin.mkdir(parents=True)
            for binary in (local_bin / "go", node_bin / "node", team_bin / "hermes-backend-tests"):
                binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                binary.chmod(0o755)

            for profile in roles:
                with self.subTest(profile=profile):
                    profile_dir = root / "profiles" / profile
                    config = json.loads((profile_dir / "config.yaml").read_text(encoding="utf-8"))
                    init_files = config["terminal"]["shell_init_files"]
                    self.assertEqual(init_files, [str(profile_dir / "hermes-team-terminal.sh")])
                    self.assertTrue(Path(init_files[0]).is_file())

            # This mirrors Hermes LocalEnvironment's login-shell bootstrap:
            # /etc/profile may reset PATH, then terminal.shell_init_files are
            # sourced before the command runs.
            script = root / "profiles" / "team-go" / "hermes-team-terminal.sh"
            command = f". {shlex.quote(str(script))}; command -v go; command -v node; command -v hermes-backend-tests"
            result = subprocess.run(
                ["bash", "-lc", command], check=True, capture_output=True, text=True,
                env={**os.environ, "HOME": str(home),
                     "PATH": "/usr/local/bin:/usr/bin:/bin"},
            )
            self.assertEqual(result.stdout.splitlines(), [str(local_bin / "go"), str(node_bin / "node"), str(team_bin / "hermes-backend-tests")])


if __name__ == "__main__":
    unittest.main()
