import importlib.util
import json
import hashlib
import io
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "scripts/backend_test_bridge.py"
SPEC = importlib.util.spec_from_file_location("backend_test_bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


REQUEST = "req-0557438ef182e8847164"
TASK = "T-001"
SHA = "a" * 40
RUN_ID = "20261028T100000Z-1234abcd"


class BackendTestBridgeTests(unittest.TestCase):
    def setUp(self):
        self.job = bridge.parse_job(REQUEST, TASK, SHA, "salesleads")

    def test_cli_job_accepts_only_fixed_ids_and_scopes(self):
        for args in (
            (REQUEST + "/../x", TASK, SHA, "full"),
            (REQUEST, "T-001;id", SHA, "full"),
            (REQUEST, TASK, SHA[:-1] + "g", "full"),
            (REQUEST, TASK, SHA, "--tests=anything"),
        ):
            with self.subTest(args=args), self.assertRaises(bridge.BridgeError):
                bridge.parse_job(*args)
        self.assertEqual(bridge.parse_job(REQUEST, TASK, SHA, "full").scope, "full")

    def test_helper_argv_is_fixed_and_contains_no_user_command(self):
        export = (
            bridge.EXPORT_ROOT / REQUEST / TASK / SHA
            / f"{REQUEST}-{TASK}-{SHA}-0123456789abcdef.bundle"
        )
        argv = bridge.build_helper_argv(self.job, export)
        self.assertEqual(
            argv[:5],
            [str(bridge.DOCKER), "exec", bridge.CONTAINER,
             str(bridge.CONTAINER_PYTHON), "-c"],
        )
        self.assertEqual(argv[-4:], [REQUEST, TASK, SHA, str(export)])
        self.assertIn("core.hooksPath=/dev/null", bridge.SNAPSHOT_HELPER)
        self.assertIn('state.get("project") != "crm-platform"', bridge.SNAPSHOT_HELPER)
        compile(bridge.SNAPSHOT_HELPER, "<snapshot-helper>", "exec")
        with self.assertRaises(bridge.BridgeError):
            bridge.build_helper_argv(self.job, Path("/tmp/payload.bundle"))

    def test_fixed_path_check_rejects_symlink_ancestors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "root"
            root.mkdir()
            real = root / "real"
            real.mkdir()
            (real / "file").write_text("x")
            link = root / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(bridge.BridgeError):
                bridge._validate_fixed_path(link / "file", root, "test path")

    def test_gradle_copy_includes_only_allowed_trees_and_excludes_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            (source / "wrapper/dists/gradle-8.10").mkdir(parents=True)
            (source / "wrapper/dists/gradle-8.10/gradle.zip").write_bytes(b"wrapper")
            (source / "caches/modules-2/files/module.jar").parent.mkdir(parents=True)
            (source / "caches/modules-2/files/module.jar").write_bytes(b"module")
            (source / "caches/modules-2/modules-2.lock").write_text("lock")
            (source / "gradle.properties").write_text("secret=never-copy")
            destination = root / "private"
            bridge._copy_tree_without_locks(source / "wrapper/dists", destination / "wrapper-dists")
            bridge._copy_tree_without_locks(source / "caches/modules-2", destination / "modules-2")
            self.assertEqual((destination / "wrapper-dists/gradle-8.10/gradle.zip").read_bytes(), b"wrapper")
            self.assertEqual((destination / "modules-2/files/module.jar").read_bytes(), b"module")
            self.assertFalse((destination / "modules-2/modules-2.lock").exists())
            self.assertFalse((destination / "gradle.properties").exists())

    def test_gradle_stage_ignores_normal_state_and_replaces_private_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            (source / "wrapper/dists/gradle-8.10/bin").mkdir(parents=True)
            gradle_bin = source / "wrapper/dists/gradle-8.10/bin/gradle"
            gradle_bin.write_text("#!/bin/sh\n")
            gradle_bin.chmod(0o755)
            (source / "caches/modules-2/files").mkdir(parents=True)
            (source / "caches/modules-2/files/module.jar").write_bytes(b"module")
            (source / "caches/modules-2/modules-2.lock").write_text("lock")
            for extra in ("native", "daemon", "notifications"):
                (source / extra).mkdir()
                (source / extra / "state").write_text("ignored")
            (source / "gradle.properties").write_text("secret=ignored")
            cache = root / "runtime/cache/gradle"
            with patch.object(bridge, "GRADLE_SOURCE", source), \
                 patch.object(bridge, "GRADLE_CACHE", cache):
                bridge.stage_gradle_cache()
                bridge.stage_gradle_cache()
            self.assertEqual((cache / "wrapper/dists/gradle-8.10/bin/gradle").read_text(), "#!/bin/sh\n")
            self.assertTrue((cache / "wrapper/dists/gradle-8.10/bin/gradle").stat().st_mode & stat.S_IXUSR)
            self.assertEqual((cache / "caches/modules-2/files/module.jar").read_bytes(), b"module")
            self.assertFalse((cache / "caches/modules-2/modules-2.lock").exists())
            self.assertFalse((cache / "gradle.properties").exists())
            self.assertFalse((cache / "native").exists())

    def test_bundle_stages_and_reuses_only_the_same_clean_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            source.mkdir()
            subprocess.run(["/usr/bin/git", "-C", str(source), "init", "-q", "-b", "candidate"], check=True)
            subprocess.run([
                "/usr/bin/git", "-C", str(source), "-c", "user.name=Test",
                "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "candidate",
            ], check=True)
            sha = subprocess.check_output(
                ["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"], text=True
            ).strip()
            bundle_root = root / "exports"
            bundle_root.mkdir()
            bundle = bundle_root / "candidate.bundle"
            subprocess.run([
                "/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(source),
                "bundle", "create", str(bundle), "HEAD",
            ], check=True)
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            job = bridge.parse_job(REQUEST, TASK, sha, "salesleads")
            metadata = {"bundle_sha256": digest, "bundle_bytes": bundle.stat().st_size}
            snapshot_root = root / "snapshots"
            with patch.object(bridge, "EXPORT_ROOT", bundle_root), \
                 patch.object(bridge, "SNAPSHOT_ROOT", snapshot_root):
                first = bridge._stage_bundle(job, bundle, metadata)
                second = bridge._stage_bundle(job, bundle, metadata)
            self.assertEqual(first, second)
            self.assertEqual(
                subprocess.check_output(["/usr/bin/git", "-C", str(second), "rev-parse", "HEAD"], text=True).strip(),
                sha,
            )
            self.assertFalse((second / ".git").is_symlink())

    def test_result_path_accepts_only_canonical_runner_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            canonical = root / REQUEST / TASK / SHA / RUN_ID / "result.json"
            canonical.parent.mkdir(parents=True)
            canonical.write_text("{}")
            output = f"last line\nRESULT_JSON={canonical}\n"
            with patch.object(bridge, "RESULT_ROOT", root):
                self.assertEqual(
                    bridge._parse_result_path(self.job, output),
                    (RUN_ID, canonical.parent),
                )
        with self.assertRaises(bridge.BridgeError):
            bridge._parse_result_path(self.job, "RESULT_JSON=/tmp/result.json\n")

    def test_runner_exit_status_is_preserved(self):
        original_mask = os.umask(0)
        os.umask(original_mask)

        def runner_result(_job):
            current_mask = os.umask(0)
            os.umask(current_mask)
            self.assertEqual(current_mask, 0o077)
            return 7, "test failure"

        with patch.object(bridge, "_ensure_fixed_directory"), \
             patch.object(bridge, "_run_helper", return_value={"bundle_name": "x"}), \
             patch.object(bridge, "_stage_bundle"), \
             patch.object(bridge, "stage_gradle_cache"), \
             patch.object(bridge, "_run_runner", side_effect=runner_result):
            self.assertEqual(bridge.run_job(self.job), 7)
        restored_mask = os.umask(0)
        os.umask(restored_mask)
        self.assertEqual(restored_mask, original_mask)

    def test_runner_signal_cancels_and_cleans_its_process_tree(self):
        class FakeStream:
            def read1(self, _size):
                os.kill(os.getpid(), bridge.signal.SIGTERM)
                return b""

        class FakeProcess:
            pid = 987654
            stdout = FakeStream()

            @staticmethod
            def wait():
                return -bridge.signal.SIGTERM

        with patch.object(bridge.subprocess, "Popen", return_value=FakeProcess()), \
             patch.object(bridge, "_terminate_process_tree") as terminate:
            code, output = bridge._run_runner(self.job)
        self.assertEqual(code, 128 + bridge.signal.SIGTERM)
        self.assertEqual(output, "")
        terminate.assert_called_once_with(FakeProcess.pid)

    def test_publish_writes_bounded_artifacts_and_stable_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source_root = root / "runner-results"
            run_dir = source_root / REQUEST / TASK / SHA / RUN_ID
            run_dir.mkdir(parents=True)
            result = {
                "request_id": REQUEST, "task_id": TASK, "sha": SHA,
                "scope": "salesleads", "exit_code": 3, "timed_out": False,
                "log": "integration-tests.log",
            }
            (run_dir / "result.json").write_text(json.dumps(result))
            (run_dir / "integration-tests.log").write_bytes(b"L" * 32)
            (run_dir / "junit").mkdir()
            (run_dir / "junit/TEST-com.example.Sample.xml").write_text(
                '<testsuite tests="1"><testcase name="ok"/></testsuite>'
            )
            published = root / "published"
            with patch.object(bridge, "RESULT_ROOT", source_root), \
                 patch.object(bridge, "PUBLISHED_RESULT_ROOT", published), \
                 patch.object(bridge, "MAX_LOG_BYTES", 10):
                destination = bridge.publish_result(self.job, 3, run_dir)
            self.assertEqual((destination / "integration-tests.log").read_bytes(), b"L" * 10)
            manifest = json.loads((destination / "manifest.json").read_text())
            self.assertEqual(manifest["runner_exit_code"], 3)
            self.assertEqual(manifest["files"], sorted(manifest["files"], key=lambda row: row["name"]))
            log_record = next(row for row in manifest["files"] if row["name"].endswith(".log"))
            self.assertEqual(log_record["bytes"], 10)
            self.assertTrue(log_record["truncated"])
            self.assertTrue((destination / "junit/TEST-com.example.Sample.xml").is_file())
            junit_record = next(row for row in manifest["files"] if row["name"].startswith("junit/"))
            self.assertEqual(junit_record["bytes"], len(
                '<testsuite tests="1"><testcase name="ok"/></testsuite>'.encode()
            ))
            with self.assertRaises(bridge.BridgeError):
                bridge.publish_result(self.job, 3, run_dir)

    def test_failed_preflight_smoke_publishes_the_named_smoke_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source_root = root / "runner-results"
            run_dir = source_root / REQUEST / TASK / SHA / RUN_ID
            run_dir.mkdir(parents=True)
            result = {
                "request_id": REQUEST, "task_id": TASK, "sha": SHA,
                "scope": "salesleads", "exit_code": 2, "timed_out": False,
                "log": "smoke.log", "preflight_smoke_log": "smoke.log",
            }
            (run_dir / "result.json").write_text(json.dumps(result))
            (run_dir / "smoke.log").write_text("network isolation smoke failed\n")
            published = root / "published"
            with patch.object(bridge, "RESULT_ROOT", source_root), \
                 patch.object(bridge, "PUBLISHED_RESULT_ROOT", published):
                destination = bridge.publish_result(self.job, 2, run_dir)
            self.assertEqual(
                (destination / "smoke.log").read_text(),
                "network isolation smoke failed\n",
            )
            self.assertFalse((destination / "integration-tests.log").exists())
            manifest = json.loads((destination / "manifest.json").read_text())
            self.assertEqual(manifest["runner_exit_code"], 2)
            self.assertEqual(
                {row["name"] for row in manifest["files"]},
                {"result.json", "smoke.log"},
            )

    def test_junit_reports_enforce_count_size_and_symlink_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source_root = root / "runner-results"
            run_dir = source_root / REQUEST / TASK / SHA / RUN_ID
            (run_dir / "junit").mkdir(parents=True)
            result = {
                "request_id": REQUEST, "task_id": TASK, "sha": SHA,
                "scope": "salesleads", "exit_code": 1, "timed_out": False,
                "log": "smoke.log",
            }
            (run_dir / "result.json").write_text(json.dumps(result))
            (run_dir / "smoke.log").write_text("failed\n")
            (run_dir / "junit/TEST-one.xml").write_text("<test/>")
            with patch.object(bridge, "RESULT_ROOT", source_root), \
                 patch.object(bridge, "PUBLISHED_RESULT_ROOT", root / "published"), \
                 patch.object(bridge, "MAX_JUNIT_FILES", 0):
                with self.assertRaisesRegex(bridge.BridgeError, "too many JUnit"):
                    bridge.publish_result(self.job, 1, run_dir)
            self.assertFalse((root / "published" / REQUEST / TASK / SHA / RUN_ID).exists())

            with patch.object(bridge, "RESULT_ROOT", source_root), \
                 patch.object(bridge, "PUBLISHED_RESULT_ROOT", root / "published"), \
                 patch.object(bridge, "MAX_JUNIT_FILE_BYTES", 1):
                with self.assertRaisesRegex(bridge.BridgeError, "per-file size"):
                    bridge.publish_result(self.job, 1, run_dir)

    def test_run_job_prints_published_shared_result_path(self):
        output = io.StringIO()
        # Keep the public result path independent of the deployment host. The
        # bridge's normal destination is configured at runtime; this unit test
        # only needs to verify the emitted receipt path.
        published = Path(tempfile.gettempdir()) / "hermes-team-test-results" / "job" / "run"
        with patch.object(bridge, "_ensure_fixed_directory"), \
             patch.object(bridge, "_run_helper", return_value={"bundle_name": "x"}), \
             patch.object(bridge, "_stage_bundle"), \
             patch.object(bridge, "stage_gradle_cache"), \
             patch.object(bridge, "_run_runner", return_value=(4, "runner output")), \
             patch.object(bridge, "_parse_result_path", return_value=(RUN_ID, Path("/tmp/runner"))), \
             patch.object(bridge, "publish_result", return_value=published), \
             redirect_stdout(output):
            self.assertEqual(bridge.run_job(self.job), 4)
        self.assertEqual(
            output.getvalue().strip(),
            f"PUBLISHED_RESULT_JSON={published / 'result.json'}",
        )


if __name__ == "__main__":
    unittest.main()
