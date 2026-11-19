#!/usr/bin/env python3
"""Stage a controller-owned CRM candidate and run the fixed isolated test suite.

This host-side bridge takes identifiers only. It asks the trusted Hermes
container to validate controller state and export a Git bundle, then stages
that bundle and a credential-free Gradle cache for the rootless test runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import shutil
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REQUEST_RE = re.compile(r"req-[0-9a-f]{20}\Z")
TASK_RE = re.compile(r"T-[0-9]{3}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
RUN_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\Z")
RESULT_LINE_RE = re.compile(r"^RESULT_JSON=(.+)$", re.MULTILINE)

HOME = Path("/home/alex")
CONTAINER = "hermes-vpn-hermes"
DOCKER = Path("/usr/bin/docker")
CONTAINER_PYTHON = Path("/home/alex/.hermes/hermes-agent/venv/bin/python")
CRM_REPO = Path("/home/alex/workspaces/crm-platform")
TEAM_DATA = Path("/home/alex/.hermes/team/data")
EXPORT_ROOT = TEAM_DATA / "test-exports"
SNAPSHOT_ROOT = HOME / ".local/share/hermes-test-runtime/snapshots"
GRADLE_SOURCE = Path("/home/alex/apps/hermes-vpn/.runtime-home/.gradle")
GRADLE_CACHE = HOME / ".local/share/hermes-test-runtime/cache/gradle"
RESULT_ROOT = HOME / ".local/share/hermes-test-runtime/results"
PUBLISHED_RESULT_ROOT = TEAM_DATA / "test-results"
RUNNER = Path(__file__).with_name("isolated_backend_tests.py")

MAX_RESULT_JSON_BYTES = 1024 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_RUNNER_OUTPUT_BYTES = 128 * 1024
MAX_JUNIT_FILES = 100
MAX_JUNIT_FILE_BYTES = 2 * 1024 * 1024
MAX_JUNIT_TOTAL_BYTES = 10 * 1024 * 1024
JUNIT_NAME_RE = re.compile(r"[A-Za-z0-9_$.-]{1,220}\.xml\Z")


class BridgeError(RuntimeError):
    """A staging or execution invariant could not be established."""


class _RunnerCancelled(Exception):
    def __init__(self, signum: int):
        self.signum = signum


def _proc_descendants(root_pid: int) -> list[int]:
    """Return a best-effort child-first tree for terminating an isolated runner."""
    found: list[int] = []
    visited: set[int] = set()

    def visit(pid: int) -> None:
        if pid in visited:
            return
        visited.add(pid)
        children_file = Path(f"/proc/{pid}/task/{pid}/children")
        try:
            children = [int(part) for part in children_file.read_text().split()]
        except (OSError, ValueError):
            children = []
        for child in children:
            visit(child)
            found.append(child)

    visit(root_pid)
    return found


def _terminate_process_tree(root_pid: int) -> None:
    """Terminate a runner and nested process groups it may have created."""
    pids = [*_proc_descendants(root_pid), root_pid]
    own_group = os.getpgrp()
    groups: set[int] = set()
    for pid in pids:
        try:
            group = os.getpgid(pid)
        except OSError:
            continue
        if group != own_group:
            groups.add(group)
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            pass
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not any(_pid_exists(pid) for pid in pids):
            return
        time.sleep(0.05)
    # Re-read the tree because a child can fork between the initial scan and
    # SIGTERM. This second pass prevents leaving its new session behind.
    remaining = [*_proc_descendants(root_pid), root_pid]
    groups = set()
    for pid in remaining:
        try:
            group = os.getpgid(pid)
        except OSError:
            continue
        if group != own_group:
            groups.add(group)
    for group in groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except OSError:
            pass
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class Job:
    request_id: str
    task_id: str
    sha: str
    scope: str


def parse_job(request_id: str, task_id: str, sha: str, scope: str) -> Job:
    if not isinstance(request_id, str) or not REQUEST_RE.fullmatch(request_id):
        raise BridgeError("invalid request id")
    if not isinstance(task_id, str) or not TASK_RE.fullmatch(task_id):
        raise BridgeError("invalid task id")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise BridgeError("invalid commit SHA")
    if scope not in {"salesleads", "full"}:
        raise BridgeError("scope must be 'salesleads' or 'full'")
    return Job(request_id, task_id, sha, scope)


# Trusted, fixed in-container program. It validates controller state and Git
# provenance before making a bundle. Only identifiers and a path constructed by
# this module are passed in; it has no shell or user-command interface.
SNAPSHOT_HELPER = r'''import hashlib, json, os, re, subprocess, sys
from pathlib import Path

REQ = re.compile(r"req-[0-9a-f]{20}\Z")
TASK = re.compile(r"T-[0-9]{3}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
HOME = Path("/home/alex")
DATA = HOME / ".hermes/team/data"
REPO = HOME / "workspaces/crm-platform"
EXPORTS = DATA / "test-exports"

def fail(message):
    raise SystemExit("backend-test-snapshot: " + message)

def git(repo, *args):
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
           "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null",
                             "-C", str(repo), *args], check=True, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, timeout=45)
    return result.stdout.strip()

def no_symlink(path):
    path = Path(path)
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            fail("path contains a symlink")

def main():
    if len(sys.argv) != 5:
        fail("invalid helper invocation")
    request_id, task_id, sha, export_path = sys.argv[1:]
    if not REQ.fullmatch(request_id) or not TASK.fullmatch(task_id) or not SHA.fullmatch(sha):
        fail("invalid identifiers")
    state_path = DATA / "runs" / request_id / "state.json"
    no_symlink(state_path)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail("controller state is missing or invalid")
    if state.get("id") != request_id or state.get("project") != "crm-platform":
        fail("request is not a CRM controller state")
    try:
        if Path(state.get("repo", "")).resolve(strict=True) != REPO.resolve(strict=True):
            fail("controller CRM repository provenance is invalid")
    except OSError:
        fail("registered CRM repository is missing")
    items = state.get("items")
    if not isinstance(items, list):
        fail("controller task list is invalid")
    matches = [item for item in items
               if isinstance(item, dict)
               and isinstance(item.get("definition"), dict)
               and item["definition"].get("id") == task_id]
    if len(matches) != 1:
        fail("task is not uniquely present in controller state")
    item = matches[0]
    expected_workspace = DATA / "runs" / request_id / "worktrees" / task_id
    workspace_value = item.get("workspace")
    if not isinstance(workspace_value, str) or Path(workspace_value) != expected_workspace:
        fail("task workspace provenance is invalid")
    no_symlink(expected_workspace)
    try:
        workspace = expected_workspace.resolve(strict=True)
        repo = REPO.resolve(strict=True)
    except OSError:
        fail("CRM workspace is missing")
    if workspace != expected_workspace or repo != REPO:
        fail("CRM paths are not canonical")
    expected_branch = "codex/team-" + request_id + "-" + task_id.lower()
    if item.get("branch") != expected_branch:
        fail("controller candidate branch provenance is invalid")
    item_status = item.get("status")
    if item_status not in {"implementation", "verification", "done"}:
        fail("task is not in a testable controller state")
    actual_branch = git(workspace, "branch", "--show-current")
    if actual_branch != expected_branch or git(workspace, "rev-parse", "HEAD") != sha:
        fail("candidate branch or SHA does not match")
    if item_status != "implementation" and item.get("head_sha") != sha:
        fail("persisted controller candidate SHA does not match")
    base_sha = item.get("base_sha")
    if not isinstance(base_sha, str) or not SHA.fullmatch(base_sha):
        fail("controller candidate base SHA is invalid")
    try:
        git(workspace, "merge-base", "--is-ancestor", base_sha, sha)
    except subprocess.CalledProcessError:
        fail("candidate is not based on its controller-recorded base")
    if git(workspace, "status", "--porcelain", "--untracked-files=all"):
        fail("candidate worktree is not clean")
    expected_common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
    actual_common = Path(git(workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve(strict=True)
    if actual_common != expected_common:
        fail("candidate does not share the registered CRM Git common directory")
    supplied = Path(export_path)
    if supplied.parent != EXPORTS / request_id / task_id / sha:
        fail("export path is outside the fixed export directory")
    expected_name = re.escape(request_id + "-" + task_id + "-" + sha + "-") + r"[0-9a-f]{16}\.bundle"
    if not re.fullmatch(expected_name, supplied.name):
        fail("export filename is invalid")
    no_symlink(EXPORTS)
    parent = EXPORTS / request_id / task_id / sha
    no_symlink(parent)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    no_symlink(parent)
    if supplied.exists() or supplied.is_symlink():
        fail("export already exists")
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
           "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
    try:
        subprocess.run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(workspace),
                        "bundle", "create", str(supplied), "HEAD"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       env=env, timeout=120)
        os.chmod(supplied, 0o600)
        data = supplied.read_bytes()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        fail("could not export verified candidate: " + type(exc).__name__)
    print(json.dumps({"request_id": request_id, "task_id": task_id, "sha": sha,
                      "bundle_name": supplied.name, "bundle_bytes": len(data),
                      "bundle_sha256": hashlib.sha256(data).hexdigest()}, sort_keys=True))

main()
'''


def _validate_fixed_path(path: Path, root: Path, label: str) -> Path:
    """Validate an existing path is beneath root and has no symlink component."""
    if not path.is_absolute() or not root.is_absolute():
        raise BridgeError(f"{label} must be absolute")
    cursor = Path(path.anchor)
    for component in path.parts[1:]:
        cursor /= component
        if cursor.is_symlink():
            raise BridgeError(f"{label} must not contain symlinks")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BridgeError(f"{label} is missing") from exc
    if not resolved.is_relative_to(resolved_root):
        raise BridgeError(f"{label} escaped its fixed root")
    return resolved


def _reject_symlink_components(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise BridgeError(f"{label} must be absolute")
    cursor = Path(path.anchor)
    for component in path.parts[1:]:
        cursor /= component
        if cursor.is_symlink():
            raise BridgeError(f"{label} must not contain symlinks")


def _ensure_fixed_directory(path: Path, root: Path) -> Path:
    """Create fixed path components without following symlinks."""
    if not path.is_absolute() or not root.is_absolute() or not path.is_relative_to(root):
        raise BridgeError("directory is outside its fixed root")
    cursor = Path(path.anchor)
    for component in path.parts[1:]:
        cursor /= component
        if cursor.is_symlink():
            raise BridgeError("directory path contains a symlink")
        if not cursor.exists():
            cursor.mkdir(mode=0o700)
        if not cursor.is_dir():
            raise BridgeError("directory path contains a non-directory")
    return path


def build_helper_argv(job: Job, bundle_path: Path) -> list[str]:
    if bundle_path.parent != EXPORT_ROOT / job.request_id / job.task_id / job.sha:
        raise BridgeError("bundle path is outside the fixed export layout")
    expected = re.escape(f"{job.request_id}-{job.task_id}-{job.sha}-") + r"[0-9a-f]{16}\.bundle"
    if not re.fullmatch(expected, bundle_path.name):
        raise BridgeError("invalid bundle export name")
    return [
        str(DOCKER), "exec", CONTAINER, str(CONTAINER_PYTHON), "-c",
        SNAPSHOT_HELPER, job.request_id, job.task_id, job.sha, str(bundle_path),
    ]


def _run_helper(job: Job, bundle_path: Path) -> dict:
    try:
        completed = subprocess.run(
            build_helper_argv(job, bundle_path), check=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=180, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                                         "LC_ALL": "C.UTF-8"},
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise BridgeError("trusted container snapshot helper failed") from exc
    try:
        metadata = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise BridgeError("snapshot helper returned invalid metadata") from exc
    expected_name = bundle_path.name
    if (not isinstance(metadata, dict)
            or metadata.get("request_id") != job.request_id
            or metadata.get("task_id") != job.task_id
            or metadata.get("sha") != job.sha
            or metadata.get("bundle_name") != expected_name
            or type(metadata.get("bundle_bytes")) is not int
            or metadata["bundle_bytes"] <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("bundle_sha256", "")))):
        raise BridgeError("snapshot helper metadata did not match the requested job")
    return metadata


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _run_git(argv: list[str], *, timeout: int = 120) -> str:
    env = {
        "PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        completed = subprocess.run(
            argv, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout, env=env,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise BridgeError("Git snapshot operation failed") from exc
    return completed.stdout.strip()


def _stage_bundle(job: Job, bundle_path: Path, metadata: dict) -> Path:
    bundle_path = _validate_fixed_path(bundle_path, EXPORT_ROOT, "bundle export")
    digest, size = _hash_file(bundle_path)
    if digest != metadata["bundle_sha256"] or size != metadata["bundle_bytes"]:
        raise BridgeError("exported bundle differs from helper metadata")
    snapshot_parent = SNAPSHOT_ROOT / job.request_id / job.task_id / job.sha
    _ensure_fixed_directory(SNAPSHOT_ROOT, SNAPSHOT_ROOT.parent)
    _ensure_fixed_directory(snapshot_parent, SNAPSHOT_ROOT)
    final_repo = snapshot_parent / "repo"
    if final_repo.exists() or final_repo.is_symlink():
        return _verify_staged_snapshot(job, final_repo)
    temp_repo = snapshot_parent / f".repo-{uuid.uuid4().hex}"
    try:
        # Clone reads and validates the bundle itself. Standalone bundle verify
        # can depend on the caller's current directory/repository.
        _run_git([
            "/usr/bin/git", "-c", "core.hooksPath=/dev/null", "clone",
            "--no-hardlinks", "--template=", str(bundle_path), str(temp_repo),
        ])
        _verify_staged_snapshot(job, temp_repo)
        os.replace(temp_repo, final_repo)
        return _verify_staged_snapshot(job, final_repo)
    except Exception:
        if temp_repo.exists():
            shutil.rmtree(temp_repo, ignore_errors=True)
        raise


def _verify_staged_snapshot(job: Job, repo: Path) -> Path:
    _validate_fixed_path(repo, SNAPSHOT_ROOT, "staged snapshot")
    if not repo.is_dir() or repo.is_symlink() or (repo / ".git").is_symlink():
        raise BridgeError("staged snapshot is not an independent repository")
    metadata = repo.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise BridgeError("staged snapshot ownership or mode is unsafe")
    git_path = repo / ".git"
    if not git_path.is_dir():
        raise BridgeError("staged snapshot has no private Git metadata")
    git_metadata = git_path.stat()
    if git_metadata.st_uid != os.getuid() or git_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise BridgeError("staged Git metadata ownership or mode is unsafe")
    if _run_git(["/usr/bin/git", "-C", str(repo), "rev-parse", "HEAD"]) != job.sha:
        raise BridgeError("staged snapshot HEAD does not match requested SHA")
    if _run_git(["/usr/bin/git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"]):
        raise BridgeError("staged snapshot is not clean")
    git_dir = Path(_run_git(["/usr/bin/git", "-C", str(repo), "rev-parse", "--absolute-git-dir"]))
    resolved = repo.resolve(strict=True)
    if not git_dir.resolve(strict=True).is_relative_to(resolved):
        raise BridgeError("staged snapshot Git metadata is external")
    return repo


def _assert_plain_tree(source: Path) -> Path:
    _reject_symlink_components(source, "Gradle cache source")
    try:
        root = source.resolve(strict=True)
    except OSError as exc:
        raise BridgeError("prewarmed Gradle cache is missing") from exc
    if not root.is_dir():
        raise BridgeError("prewarmed Gradle cache is not a directory")
    return root


def _copy_tree_without_locks(source: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for child in sorted(source.iterdir(), key=lambda p: p.name):
        if child.name.endswith(".lock"):
            continue
        if child.is_symlink():
            raise BridgeError("prewarmed Gradle cache contains a symlink")
        target = destination / child.name
        if child.is_dir():
            _copy_tree_without_locks(child, target)
        elif child.is_file():
            shutil.copyfile(child, target)
            executable = child.stat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            os.chmod(target, 0o700 if executable else 0o600)
        else:
            raise BridgeError("prewarmed Gradle cache contains an unsupported entry")


def stage_gradle_cache() -> Path:
    source = _assert_plain_tree(GRADLE_SOURCE)
    # Gradle normally has additional state (native/, daemon/, notifications/).
    # It is ignored completely; only the two dependency trees are inspected.
    wrapper_dists = source / "wrapper" / "dists"
    modules_2 = source / "caches" / "modules-2"
    _reject_symlink_components(wrapper_dists, "Gradle wrapper distributions")
    _reject_symlink_components(modules_2, "Gradle module cache")
    _ensure_fixed_directory(GRADLE_CACHE.parent, GRADLE_CACHE.parent.parent)
    staging_cache = GRADLE_CACHE.parent / f".gradle-{uuid.uuid4().hex}"
    backup_cache = GRADLE_CACHE.parent / f".gradle-old-{uuid.uuid4().hex}"
    staging_cache.mkdir(mode=0o700)
    try:
        (staging_cache / "wrapper").mkdir(mode=0o700)
        (staging_cache / "caches").mkdir(mode=0o700)
        if wrapper_dists.exists():
            _copy_tree_without_locks(wrapper_dists, staging_cache / "wrapper" / "dists")
        else:
            (staging_cache / "wrapper" / "dists").mkdir(mode=0o700)
        if modules_2.exists():
            _copy_tree_without_locks(modules_2, staging_cache / "caches" / "modules-2")
        else:
            (staging_cache / "caches" / "modules-2").mkdir(mode=0o700)
        if GRADLE_CACHE.is_symlink():
            raise BridgeError("private Gradle cache must not be a symlink")
        if GRADLE_CACHE.exists():
            _assert_private_cache(GRADLE_CACHE)
            os.replace(GRADLE_CACHE, backup_cache)
        os.replace(staging_cache, GRADLE_CACHE)
        if backup_cache.exists():
            shutil.rmtree(backup_cache)
        return GRADLE_CACHE
    except Exception:
        shutil.rmtree(staging_cache, ignore_errors=True)
        if backup_cache.exists() and not GRADLE_CACHE.exists():
            os.replace(backup_cache, GRADLE_CACHE)
        raise


def _assert_private_cache(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise BridgeError("private Gradle cache is not a plain directory")
    if {entry.name for entry in path.iterdir()} != {"wrapper", "caches"}:
        raise BridgeError("private Gradle cache has unexpected entries")
    for entry in path.rglob("*"):
        if entry.is_symlink() or (not entry.is_dir() and not entry.is_file()):
            raise BridgeError("private Gradle cache contains an unsafe entry")
        if entry.name.endswith(".lock"):
            raise BridgeError("private Gradle cache contains a lock file")
    for required in (path / "wrapper/dists", path / "caches/modules-2"):
        if not required.is_dir():
            raise BridgeError("private Gradle cache layout is incomplete")


def _run_runner(job: Job) -> tuple[int, str]:
    argv = [
        "/usr/bin/python3", str(RUNNER), "--request", job.request_id,
        "--task", job.task_id, "--sha", job.sha, "--scope", job.scope,
    ]
    process: subprocess.Popen[bytes] | None = None
    tail = bytearray()

    def cancel_handler(signum, _frame):
        raise _RunnerCancelled(signum)

    previous_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, cancel_handler)
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
        assert process.stdout is not None
        while True:
            # Stream available progress immediately; read(n) can buffer a
            # quiet Gradle run until all n bytes arrive or the process exits.
            chunk = process.stdout.read1(65536)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            tail.extend(chunk)
            if len(tail) > MAX_RUNNER_OUTPUT_BYTES:
                del tail[:-MAX_RUNNER_OUTPUT_BYTES]
        return process.wait(), tail.decode("utf-8", "replace")
    except _RunnerCancelled as cancelled:
        if process is not None:
            _terminate_process_tree(process.pid)
            process.wait()
        return 128 + cancelled.signum, tail.decode("utf-8", "replace")
    except OSError as exc:
        if process is not None and process.poll() is None:
            _terminate_process_tree(process.pid)
            process.wait()
        raise BridgeError("isolated runner could not be started") from exc
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _parse_result_path(job: Job, output: str) -> tuple[str, Path] | None:
    matches = RESULT_LINE_RE.findall(output)
    if not matches:
        return None
    if len(matches) != 1:
        raise BridgeError("isolated runner printed ambiguous result paths")
    reported = Path(matches[0])
    parent = RESULT_ROOT / job.request_id / job.task_id / job.sha
    if reported.parent.parent != parent:
        raise BridgeError("isolated runner result path is outside the fixed layout")
    run_id = reported.parent.name
    if not RUN_ID_RE.fullmatch(run_id) or reported.name != "result.json":
        raise BridgeError("isolated runner result path is malformed")
    _validate_fixed_path(reported, RESULT_ROOT, "isolated runner result")
    return run_id, reported.parent


def _copy_bounded(source: Path, target: Path, limit: int) -> tuple[str, int, bool]:
    if source.is_symlink() or not source.is_file():
        raise BridgeError("runner output is missing or unsafe")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256()
    copied = 0
    with source.open("rb") as src, target.open("xb") as dst:
        while copied < limit:
            chunk = src.read(min(1024 * 1024, limit - copied))
            if not chunk:
                break
            dst.write(chunk)
            digest.update(chunk)
            copied += len(chunk)
    os.chmod(target, 0o600)
    return digest.hexdigest(), copied, source.stat().st_size > copied


def _copy_junit_reports(result_dir: Path, destination: Path, manifest_files: list[dict]) -> None:
    source_dir = result_dir / "junit"
    if source_dir.is_symlink():
        raise BridgeError("runner JUnit directory must not be a symlink")
    if not source_dir.exists():
        return
    _validate_fixed_path(source_dir, RESULT_ROOT, "runner JUnit directory")
    if not source_dir.is_dir():
        raise BridgeError("runner JUnit output is not a directory")
    reports = []
    for child in sorted(source_dir.iterdir(), key=lambda path: path.name):
        if child.is_symlink():
            raise BridgeError("runner JUnit output contains a symlink")
        if child.is_dir():
            continue
        if not child.is_file():
            raise BridgeError("runner JUnit output contains a special file")
        if child.suffix.lower() != ".xml":
            continue
        if not JUNIT_NAME_RE.fullmatch(child.name):
            raise BridgeError("runner JUnit filename is unsafe")
        reports.append(child)
    if len(reports) > MAX_JUNIT_FILES:
        raise BridgeError("runner produced too many JUnit reports")
    total_size = 0
    for report in reports:
        size = report.stat().st_size
        if size > MAX_JUNIT_FILE_BYTES:
            raise BridgeError("runner JUnit report exceeds the per-file size limit")
        total_size += size
        if total_size > MAX_JUNIT_TOTAL_BYTES:
            raise BridgeError("runner JUnit reports exceed the total size limit")
    if not reports:
        return
    junit_destination = destination / "junit"
    junit_destination.mkdir(mode=0o700)
    for report in reports:
        digest, size, truncated = _copy_bounded(
            report, junit_destination / report.name, MAX_JUNIT_FILE_BYTES
        )
        if truncated or size != report.stat().st_size:
            raise BridgeError("runner JUnit report changed while being copied")
        manifest_files.append({
            "name": f"junit/{report.name}",
            "sha256": digest,
            "bytes": size,
            "truncated": False,
        })


def publish_result(job: Job, exit_code: int, result_dir: Path) -> Path:
    result_file = result_dir / "result.json"
    _validate_fixed_path(result_file, RESULT_ROOT, "runner result JSON")
    if result_file.stat().st_size > MAX_RESULT_JSON_BYTES:
        raise BridgeError("runner result JSON exceeds the size limit")
    try:
        result_data = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BridgeError("runner result JSON is invalid") from exc
    if (not isinstance(result_data, dict)
            or result_data.get("request_id") != job.request_id
            or result_data.get("task_id") != job.task_id
            or result_data.get("sha") != job.sha
            or result_data.get("scope") != job.scope
            or type(result_data.get("exit_code")) is not int
            or type(result_data.get("timed_out")) is not bool):
        raise BridgeError("runner result metadata did not match the requested job")
    expected_exit = 124 if result_data["timed_out"] else result_data["exit_code"]
    if expected_exit != exit_code:
        raise BridgeError("runner exit code did not match its result JSON")
    run_id = result_dir.name
    if not RUN_ID_RE.fullmatch(run_id):
        raise BridgeError("runner result directory has an invalid run id")
    destination = PUBLISHED_RESULT_ROOT / job.request_id / job.task_id / job.sha / run_id
    _ensure_fixed_directory(destination.parent, PUBLISHED_RESULT_ROOT)
    if destination.exists() or destination.is_symlink():
        raise BridgeError("published result directory already exists")
    destination.mkdir(mode=0o700)
    files = []
    try:
        digest, size, truncated = _copy_bounded(
            result_file, destination / "result.json", MAX_RESULT_JSON_BYTES
        )
        files.append({"name": "result.json", "sha256": digest, "bytes": size,
                      "truncated": truncated})
        allowed_logs = {"integration-tests.log", "smoke.log"}
        log_names = {
            result_data.get("log"),
            result_data.get("preflight_smoke_log"),
        } - {None}
        if not log_names or not log_names <= allowed_logs:
            raise BridgeError("runner result names an unsupported log file")
        copied_logs = 0
        for name in sorted(log_names):
            source = result_dir / name
            if not source.exists():
                continue
            digest, size, truncated = _copy_bounded(source, destination / name, MAX_LOG_BYTES)
            files.append({"name": name, "sha256": digest, "bytes": size,
                          "truncated": truncated})
            copied_logs += 1
        if not copied_logs:
            raise BridgeError("runner log output is missing")
        _copy_junit_reports(result_dir, destination, files)
        manifest = {
            "schema_version": 1,
            "request_id": job.request_id,
            "task_id": job.task_id,
            "sha": job.sha,
            "scope": job.scope,
            "run_id": run_id,
            "runner_exit_code": exit_code,
            "files": sorted(files, key=lambda row: row["name"]),
        }
        manifest_path = destination / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def run_job(job: Job) -> int:
    previous_umask = os.umask(0o077)
    try:
        return _run_job(job)
    finally:
        os.umask(previous_umask)


def _run_job(job: Job) -> int:
    export_parent = EXPORT_ROOT / job.request_id / job.task_id / job.sha
    _ensure_fixed_directory(EXPORT_ROOT, TEAM_DATA)
    _ensure_fixed_directory(export_parent, EXPORT_ROOT)
    bundle_path = export_parent / (
        f"{job.request_id}-{job.task_id}-{job.sha}-{uuid.uuid4().hex[:16]}.bundle"
    )
    metadata = _run_helper(job, bundle_path)
    _stage_bundle(job, bundle_path, metadata)
    stage_gradle_cache()
    exit_code, output = _run_runner(job)
    result_info = _parse_result_path(job, output)
    if result_info:
        _, result_dir = result_info
        published_dir = publish_result(job, exit_code, result_dir)
        print(f"PUBLISHED_RESULT_JSON={published_dir / 'result.json'}", flush=True)
    return exit_code


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, dest="request_id")
    parser.add_argument("--task", required=True, dest="task_id")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--scope", required=True, choices=("salesleads", "full"))
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return run_job(parse_job(args.request_id, args.task_id, args.sha, args.scope))
    except BridgeError as exc:
        print(f"backend-test-bridge: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
