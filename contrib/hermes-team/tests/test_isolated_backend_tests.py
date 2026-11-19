import importlib.util
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time
import unittest


MODULE_PATH = Path(__file__).parents[1] / "scripts/isolated_backend_tests.py"
SPEC = importlib.util.spec_from_file_location("isolated_backend_tests", MODULE_PATH)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class IsolatedBackendRunnerTests(unittest.TestCase):
    def test_sigterm_of_runner_reaps_child_process_group(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "child.log"
            child_code = (
                "import json, os, signal, subprocess, sys, time; "
                "grandchild = subprocess.Popen([sys.executable, '-c', "
                "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print(\"ready\", flush=True); time.sleep(60)'], "
                "stdout=subprocess.PIPE, text=True); "
                "assert grandchild.stdout.readline().strip() == 'ready'; "
                "print(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp(), "
                "'grandchild': grandchild.pid}), flush=True); "
                "time.sleep(60)"
            )
            shim_code = f"""
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('runner_under_test', {str(MODULE_PATH)!r})
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)
def run_fake(_job, smoke_only=False):
    return runner._stream_process([sys.executable, '-c', {child_code!r}], Path(sys.argv[1]), 60)[0]
runner.run_job = run_fake
runner.main(['--request', 'req-0557438ef182e8847164', '--task', 'T-001',
             '--sha', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', '--scope', 'full'])
"""
            shim = subprocess.Popen(
                [sys.executable, "-c", shim_code, str(log_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
            )
            pgid = None
            try:
                assert shim.stdout is not None
                selector = selectors.DefaultSelector()
                selector.register(shim.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 10
                output = bytearray()
                while b"\n" not in output and time.monotonic() < deadline:
                    if not selector.select(timeout=0.1):
                        continue
                    chunk = os.read(shim.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    output.extend(chunk)
                selector.close()
                self.assertIn(b"\n", output, "child did not report its process group")
                details = json.loads(output.splitlines()[0])
                pgid = details["pgid"]
                self.assertEqual(pgid, details["pid"])
                self.assertNotEqual(pgid, details["grandchild"])

                # Signal only the runner shim. Its signal handler unwinds through
                # _stream_process, which must terminate the command's full group.
                shim.send_signal(signal.SIGTERM)
                self.assertEqual(shim.wait(timeout=10), 143)

                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        os.killpg(pgid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    self.fail(f"child process group {pgid} survived runner cancellation")
            finally:
                if shim.poll() is None:
                    shim.kill()
                    shim.wait()
                if pgid is not None:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if shim.stdout is not None:
                    shim.stdout.close()

    def test_accepts_only_fixed_request_identifiers_and_scopes(self):
        job = runner.parse_job(
            "req-0557438ef182e8847164", "T-001", "a" * 40, "salesleads"
        )
        self.assertEqual(job.scope, "salesleads")
        for values in (
            ("req-../../etc/passwd", "T-001", "a" * 40, "full"),
            ("req-0557438ef182e8847164", "T-001;id", "a" * 40, "full"),
            ("req-0557438ef182e8847164", "T-001", "a" * 40, "../../tmp"),
            ("req-0557438ef182e8847164", "T-001", "a" * 39 + "g", "full"),
        ):
            with self.subTest(values=values), self.assertRaises(runner.RunnerError):
                runner.parse_job(*values)

    def test_gradle_argv_is_fixed_and_scope_selects_only_test_filter(self):
        common = [
            "./gradlew", "--offline", "--no-daemon",
            "-Pkotlin.compiler.execution.strategy=in-process",
            ":backend:integrationTest",
        ]
        self.assertEqual(runner.build_gradle_argv("full"), common)
        self.assertEqual(
            runner.build_gradle_argv("salesleads"),
            common + ["--tests", "com.crmplatform.integration.salesleads.*"],
        )
        with self.assertRaises(runner.RunnerError):
            runner.build_gradle_argv("backend:test;touch /tmp/pwned")

    def test_bwrap_has_an_explicit_filesystem_allowlist_and_no_host_home(self):
        argv = runner.build_bwrap_argv(
            Path("/private/run/repo"), Path("/private/run/gradle"),
            Path("/private/run/etc"),
        )
        joined = "\n".join(argv)
        self.assertIn("--ro-bind\n/usr\n/usr", joined)
        self.assertIn("--ro-bind\n/home/alex/.hermes/toolchains/jdk-21\n/opt/jdk", joined)
        self.assertIn("--cap-drop\nALL", joined)
        self.assertIn("--unshare-ipc", joined)
        self.assertIn("--unshare-uts", joined)
        self.assertIn("--new-session", joined)
        self.assertIn("--bind\n/private/run/repo\n/workspace", joined)
        self.assertIn("--bind\n/private/run/gradle\n/gradle", joined)
        self.assertIn("--bind\n/run/user/1000/hermes-test-runtime/docker.sock\n/run/docker.sock", joined)
        for forbidden in ("/home/alex/.hermes\n/home", "/home/alex/.ssh", "/var/run/docker.sock"):
            self.assertNotIn(forbidden, joined)
        self.assertEqual(argv[-1], "--")

    def test_builtin_smoke_is_fixed_python_and_checks_lan_global_ipv4_and_ipv6(self):
        command = runner._smoke_command()
        self.assertEqual(command[:2], ["/usr/bin/python3", "-c"])
        compile(command[2], "isolated-smoke", "exec")
        for address in ("1.1.1.1", "192.168.1.20", "2606:4700:4700::1111"):
            self.assertIn(address, command[2])

    def test_nsenter_joins_only_verified_user_network_and_mount_namespaces(self):
        argv = runner.build_nsenter_argv(4242, ["/fixed/bwrap", "--"])
        self.assertEqual(
            argv,
            ["/usr/bin/nsenter", "--target", "4242", "--user",
             "--preserve-credentials", "--net", "--mount", "--",
             "/fixed/bwrap", "--"],
        )
        with self.assertRaises(runner.RunnerError):
            runner.build_nsenter_argv(1, [])

    def test_only_rootlesskit_without_published_ports_is_accepted(self):
        from unittest.mock import patch

        own_netns = 100
        candidates = {
            101: [
                "rootlesskit", f"--state-dir={runner.ROOTLESS_STATE}",
                "--net=none", "--port-driver=none",
            ],
            102: [
                "rootlesskit", f"--state-dir={runner.ROOTLESS_STATE}",
                "--net=none", "--port-driver=builtin",
            ],
        }

        class ProcEntry:
            def __init__(self, name):
                self.name = name

        with patch.object(runner.Path, "iterdir", return_value=[ProcEntry("101"), ProcEntry("102")]):
            with patch.object(runner, "_read_proc_cmdline", side_effect=lambda pid: candidates.get(pid, [])):
                with patch.object(runner, "_netns_inode", side_effect=lambda pid: own_netns if pid == os.getpid() else own_netns + 1):
                    self.assertEqual(runner.find_rootless_namespace_pid(), 101)

            with patch.object(runner, "_read_proc_cmdline", side_effect=lambda pid: candidates[102]):
                with patch.object(runner, "_netns_inode", return_value=own_netns + 1):
                    with self.assertRaisesRegex(runner.RunnerError, "found 0"):
                        runner.find_rootless_namespace_pid()

    def test_route_guard_rejects_only_usable_default_routes(self):
        ipv6_reject_sentinel = (
            "0" * 32 + " 00 " + "0" * 32 + " 00 " + "0" * 32
            + " ffffffff 00000001 00000000 00200200 lo"
        )
        ipv6_localhost = (
            "0" * 31 + "1 80 " + "0" * 32 + " 00 " + "0" * 32
            + " 00000001 00000000 00000000 80200001 lo"
        )
        ipv6_usable_default = (
            "0" * 32 + " 00 " + "0" * 32 + " 00 " + "0" * 32
            + " 00000000 00000000 00000000 00000001 eth0"
        )
        self.assertFalse(runner._contains_usable_ipv6_default_route([
            ipv6_reject_sentinel, ipv6_reject_sentinel, ipv6_localhost,
        ]))
        self.assertTrue(runner._contains_usable_ipv6_default_route([ipv6_usable_default]))

        ipv4_reject_default = "lo 00000000 00000000 0201 0 0 0 00000000 0 0 0"
        ipv4_usable_default = "eth0 00000000 0101A8C0 0003 0 0 100 00000000 0 0 0"
        ipv4_non_up_default = "eth0 00000000 0101A8C0 0002 0 0 100 00000000 0 0 0"
        self.assertFalse(runner._contains_usable_ipv4_default_route([ipv4_reject_default]))
        self.assertFalse(runner._contains_usable_ipv4_default_route([ipv4_non_up_default]))
        self.assertTrue(runner._contains_usable_ipv4_default_route([ipv4_usable_default]))

    def test_daemon_filesystem_sandbox_requires_private_tmpfs_root_and_hidden_host_paths(self):
        from unittest.mock import patch

        safe_mountinfo = "\n".join((
            "1 0 0:1 / / rw - tmpfs tmpfs rw",
            "2 1 0:2 /usr /usr ro - ext4 /dev/sda rw",
        ))

        def fake_stat(_path):
            raise FileNotFoundError

        def separate_namespaces(path):
            return {
                "/proc/self/ns/mnt": "mnt:[1]",
                "/proc/4242/ns/mnt": "mnt:[2]",
                "/proc/self/ns/pid": "pid:[1]",
                "/proc/4242/ns/pid": "pid:[2]",
            }[path]

        with patch.object(runner.os, "readlink", side_effect=separate_namespaces):
            with patch.object(runner.Path, "read_text", return_value=safe_mountinfo):
                with patch.object(runner.os, "stat", side_effect=fake_stat):
                    evidence = runner.verify_daemon_filesystem_sandbox(4242)
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["root_filesystem"], "tmpfs")
        self.assertEqual(evidence["pid_namespace"], "pid:[2]")

        exposed_apps = safe_mountinfo + "\n3 1 0:3 /home/alex/apps /home/alex/apps rw - ext4 /dev/sdb rw"
        with patch.object(runner.os, "readlink", side_effect=separate_namespaces):
            with patch.object(runner.Path, "read_text", return_value=exposed_apps):
                with patch.object(runner.os, "stat", side_effect=fake_stat):
                    with self.assertRaisesRegex(runner.RunnerError, "exposes a protected host path"):
                        runner.verify_daemon_filesystem_sandbox(4242)

        for exposed_path in (runner.HOME / ".hermes", runner.RUNTIME_ROOT / "tools"):
            exposed_mount = (
                f"3 1 0:3 {exposed_path} {exposed_path} rw - ext4 /dev/sdb rw"
            )
            with self.subTest(exposed_path=exposed_path):
                with patch.object(runner.os, "readlink", side_effect=separate_namespaces):
                    with patch.object(runner.Path, "read_text", return_value=safe_mountinfo + "\n" + exposed_mount):
                        with patch.object(runner.os, "stat", side_effect=fake_stat):
                            with self.assertRaisesRegex(runner.RunnerError, "exposes a protected host path"):
                                runner.verify_daemon_filesystem_sandbox(4242)

        unsandboxed_root = "1 0 0:1 / / rw - ext4 /dev/sda rw"
        with patch.object(runner.os, "readlink", side_effect=separate_namespaces):
            with patch.object(runner.Path, "read_text", return_value=unsandboxed_root):
                with patch.object(runner.os, "stat", side_effect=fake_stat):
                    with self.assertRaisesRegex(runner.RunnerError, "root is not the private bubblewrap tmpfs"):
                        runner.verify_daemon_filesystem_sandbox(4242)

        with patch.object(runner.os, "readlink", side_effect=lambda path: "mnt:[1]" if path == "/proc/self/ns/mnt" else "mnt:[2]" if path == "/proc/4242/ns/mnt" else "pid:[1]"):
            with patch.object(runner.Path, "read_text", return_value=safe_mountinfo):
                with self.assertRaisesRegex(runner.RunnerError, "separate PID namespace"):
                    runner.verify_daemon_filesystem_sandbox(4242)

    def test_snapshot_path_must_be_clean_and_match_the_expected_commit(self):
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp) / "hermes-test-runtime"
            root = runtime_root / "snapshots"
            repo = root / "req-0557438ef182e8847164" / "T-001" / ("a" * 40) / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / "README.md").write_text("snapshot")
            for directory in (
                runtime_root, root, root / "req-0557438ef182e8847164",
                root / "req-0557438ef182e8847164" / "T-001",
                root / "req-0557438ef182e8847164" / "T-001" / ("a" * 40),
                repo, repo / ".git",
            ):
                if directory.exists():
                    directory.chmod(0o700)
            with patch.object(runner, "RUNTIME_ROOT", runtime_root), patch.object(runner, "SNAPSHOT_ROOT", root):
                with patch.object(runner, "_git", side_effect=[
                    "a" * 40, "", str(repo / ".git"), str(repo)
                ]):
                    self.assertEqual(
                        runner.validate_snapshot(
                            runner.Job("req-0557438ef182e8847164", "T-001", "a" * 40, "full")
                        ),
                        repo.resolve(),
                    )
                with patch.object(runner, "_git", side_effect=["b" * 40]):
                    with self.assertRaises(runner.RunnerError):
                        runner.validate_snapshot(
                            runner.Job("req-0557438ef182e8847164", "T-001", "a" * 40, "full")
                        )

                with patch.object(runner, "_git", side_effect=["a" * 40, " M README.md"]):
                    with self.assertRaisesRegex(runner.RunnerError, "not clean"):
                        runner.validate_snapshot(
                            runner.Job("req-0557438ef182e8847164", "T-001", "a" * 40, "full")
                        )
                (repo / ".git" / "evil").symlink_to("/etc/passwd")
                with self.assertRaisesRegex(runner.RunnerError, "contains symlinks"):
                    runner.validate_snapshot(
                        runner.Job("req-0557438ef182e8847164", "T-001", "a" * 40, "full")
                    )

    def test_docker_endpoint_verification_requires_rootless_vfs(self):
        import os
        import socket
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            socket_path = str(Path(tmp) / "docker.sock")
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(socket_path)
            os.chmod(socket_path, 0o600)
            good = {
                "SecurityOptions": ["name=rootless", "name=seccomp"],
                "Driver": "vfs",
                "DockerRootDir": str(runner.ROOTLESS_DATA),
            }
            try:
                with patch.object(runner, "_http_docker_info", return_value=good):
                    runner.validate_rootless_daemon(Path(socket_path))
                for info in (
                    {**good, "SecurityOptions": ["name=seccomp"]},
                    {**good, "Driver": "overlay2"},
                    {**good, "DockerRootDir": "/var/lib/docker"},
                ):
                    with patch.object(runner, "_http_docker_info", return_value=info):
                        with self.assertRaises(runner.RunnerError):
                            runner.validate_rootless_daemon(Path(socket_path))
            finally:
                listener.close()

    def test_docker_info_chunk_decoder_preserves_json_body(self):
        self.assertEqual(
            runner._decode_chunked(b"7\r\nhermes!\r\n0\r\n\r\n"),
            b"hermes!",
        )

    def test_zero_tests_or_cleanup_failures_never_become_a_green_gate(self):
        passing = {
            "smoke_only": False, "smoke_ok": True, "git_ok": True,
            "process_exit_code": 0, "timed_out": False,
            "counts": {"tests": 7, "reports": 1, "failures": 0, "errors": 0},
            "copied_reports": 1, "cleanup_errors": [],
        }
        self.assertTrue(runner._tests_passed(**passing))
        self.assertFalse(runner._tests_passed(**{**passing, "counts": {"tests": 0, "reports": 0}}))
        self.assertFalse(runner._tests_passed(**{**passing, "git_ok": False}))
        self.assertFalse(runner._tests_passed(**{**passing, "cleanup_errors": ["leftover container"]}))
        self.assertFalse(runner._tests_passed(**{
            **passing, "counts": {"tests": 7, "reports": 1, "failures": 1, "errors": 0}
        }))
        self.assertEqual(
            runner._effective_exit_code(
                smoke_only=False, smoke_exit_code=0, smoke_timeout=False, smoke_ok=True,
                process_exit_code=0, timed_out=False, passed=False, cleanup_errors=[],
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
