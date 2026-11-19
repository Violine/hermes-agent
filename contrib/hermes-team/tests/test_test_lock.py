from __future__ import annotations

import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.test_lock import delegated_test_runner, heavy_test_lock


def _lock_writer(path: str, entered) -> None:
    with heavy_test_lock(path):
        entered.set()


class HeavyTestLockTests(unittest.TestCase):
    def test_lock_serializes_separate_processes(self):
        context = multiprocessing.get_context("spawn")
        entered = context.Event()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".heavy-test.lock"
            process = context.Process(target=_lock_writer, args=(str(path), entered))
            with heavy_test_lock(path):
                process.start()
                self.assertFalse(entered.wait(0.2), "second process must wait while lock is held")
            self.assertTrue(entered.wait(3), "waiting process should acquire lock after release")
            process.join(3)
            self.assertEqual(process.exitcode, 0)

    def test_delegated_backend_runner_is_left_to_acquire_its_own_lock(self):
        self.assertTrue(delegated_test_runner(["/runtime/scripts/hermes-backend-tests", "--scope", "full"]))
        self.assertTrue(delegated_test_runner([sys.executable, "-m", "team_engine.backend_test_jobs", "run"]))
        self.assertFalse(delegated_test_runner(["npm", "run", "test:unit"]))

    def test_batch_caller_can_wait_for_delegated_backend_wrapper_without_nested_lock(self):
        # This fixture exercises the same protocol: outer test orchestration
        # recognizes the wrapper and does not acquire the lock; the child does.
        from team_engine.git_gates import run_batch_regression
        import subprocess

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True,
                           capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Lock Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "lock@example.invalid"], check=True)
            (repo / "README").write_text("baseline\n")
            subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "baseline"], check=True,
                           capture_output=True)
            sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                 capture_output=True, text=True).stdout.strip()
            lock = root / "shared.lock"
            wrapper = root / "hermes-backend-tests"
            wrapper.write_text(
                "import fcntl, pathlib\n"
                f"with pathlib.Path({str(lock)!r}).open('a') as f:\n"
                " fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
                " print('backend wrapper acquired lock')\n"
            )
            result = run_batch_regression(
                repo, sha, [{"name": "backend", "argv": [sys.executable, str(wrapper)]}],
                worktree_parent=root, timeout=3, check_lock_path=lock)
            self.assertEqual(result["status"], "pass", result)
            self.assertIn("backend wrapper acquired lock", result["results"][0]["stdout"])


if __name__ == "__main__":
    unittest.main()
