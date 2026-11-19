"""Deterministic Git and evidence gates for the Hermes team controller.

This module deliberately accepts test commands only as argv sequences supplied by
trusted project configuration. It never evaluates task or plan text as a shell
command and never pushes or updates the configured base branch.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from .test_lock import delegated_test_runner, heavy_test_lock


class GitGateError(RuntimeError):
    """A repository, evidence, or integration gate rejected promotion."""


class EvidenceError(GitGateError):
    """A review or QA artifact is malformed, stale, or insufficient."""


@dataclass(frozen=True)
class CandidateSnapshot:
    repository: str
    base_ref: str
    feature_ref: str
    base_sha: str
    head_sha: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class IntegrationResult:
    integration_branch: str
    worktree: str
    base_sha: str
    candidate_sha: str
    integration_sha: str
    changed_paths: tuple[str, ...]
    checks: tuple[CommandResult, ...]


_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_ALLOWED_ROLES = {"reviewer", "qa"}
_ALLOWED_VERDICTS = {"pass", "changes_requested", "blocked"}
_ALLOWED_SEVERITIES = {"info", "low", "medium", "high", "critical"}
_ALLOWED_CHECK_STATUSES = {"pass", "fail", "skip"}
_CHECK_HEARTBEAT_SECONDS = 15.0
_CHECK_OUTPUT_LIMIT = 4000
_CHECK_ARG_LIMIT = 256
_CHECK_ARGV_COUNT_LIMIT = 24
_SECRET_OUTPUT_PATTERNS = (
    re.compile(r"(?i)(\bauthorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)([\"']?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|password|passwd|secret)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}]+"),
    re.compile(r"\b(?:sk-ant-|sk-)[A-Za-z0-9_-]{16,}\b"),
)


def _bounded_text(value: str, limit: int = _CHECK_OUTPUT_LIMIT) -> str:
    """Keep a safe, bounded tail for task events and failure diagnostics."""
    text = str(value).replace("\x00", "�")
    text = "".join(char if char in "\n\r\t" or ord(char) >= 32 else "�" for char in text)
    for pattern in _SECRET_OUTPUT_PATTERNS:
        if pattern.groups:
            text = pattern.sub(r"\1[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    return text[-limit:]


def _display_argv(argv: Sequence[str]) -> list[str]:
    display = [_bounded_text(arg, _CHECK_ARG_LIMIT) for arg in argv[:_CHECK_ARGV_COUNT_LIMIT]]
    if len(argv) > _CHECK_ARGV_COUNT_LIMIT:
        display.append(f"… {len(argv) - _CHECK_ARGV_COUNT_LIMIT} args omitted")
    return display


def _emit_check_progress(
    callback: Callable[[Mapping[str, Any]], None] | None,
    event: str,
    *,
    index: int,
    total: int,
    argv: Sequence[str],
    started_at: float,
    **extra: Any,
) -> None:
    if callback is None:
        return
    payload = {
        "event": event,
        "index": index,
        "total": total,
        "argv": _display_argv(argv),
        "elapsed_seconds": round(max(0.0, time.monotonic() - started_at), 3),
        **extra,
    }
    callback(payload)


def _kill_process_group(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Stop and reap the command's owned process group, preserving its output."""
    if proc.poll() is None:
        try:
            if os.name == "nt":  # pragma: no cover - local runtime is POSIX
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                proc.terminate()
            except OSError:
                pass
    try:
        return proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":  # pragma: no cover - local runtime is POSIX
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
        return proc.communicate()


def _run_trusted_check(
    argv: Sequence[str], *, cwd: Path, timeout: float, index: int, total: int,
    progress: Callable[[Mapping[str, Any]], None] | None,
) -> CommandResult:
    started_at = time.monotonic()
    _emit_check_progress(progress, "started", index=index, total=total,
                         argv=argv, started_at=started_at)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        detail = _bounded_text(str(exc))
        _emit_check_progress(progress, "completed", index=index, total=total,
                             argv=argv, started_at=started_at, returncode=None,
                             timed_out=False, result="error", output_tail=detail)
        raise GitGateError(f"Integration check {_display_argv(argv)!r} could not start: {detail}") from exc

    deadline = started_at + max(0.0, timeout)
    while True:
        remaining = deadline - time.monotonic()
        wait_for = min(_CHECK_HEARTBEAT_SECONDS, max(0.01, remaining))
        try:
            stdout, stderr = proc.communicate(timeout=wait_for)
            break
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - started_at
            if elapsed >= max(0.0, timeout):
                stdout, stderr = _kill_process_group(proc)
                out_tail = _bounded_text(stdout or "")
                err_tail = _bounded_text(stderr or "")
                output_tail = _bounded_text("\n".join(part for part in (
                    "stdout:\n" + out_tail if out_tail else "",
                    "stderr:\n" + err_tail if err_tail else "",
                ) if part))
                _emit_check_progress(progress, "completed", index=index, total=total,
                                     argv=argv, started_at=started_at,
                                     returncode=proc.returncode, timed_out=True,
                                     result="timeout", output_tail=output_tail)
                raise GitGateError(
                    f"Integration check {_display_argv(argv)!r} timed out after {timeout:g}s"
                    + (f": {output_tail}" if output_tail else "")
                )
            try:
                _emit_check_progress(progress, "heartbeat", index=index, total=total,
                                     argv=argv, started_at=started_at)
            except Exception as exc:
                stdout, stderr = _kill_process_group(proc)
                detail = _bounded_text(str(exc))
                raise GitGateError(f"Integration progress callback failed: {detail}") from exc
        except Exception:
            if proc.poll() is None:
                _kill_process_group(proc)
            raise

    out_tail = _bounded_text(stdout or "")
    err_tail = _bounded_text(stderr or "")
    output_tail = _bounded_text("\n".join(part for part in (
        "stdout:\n" + out_tail if out_tail else "",
        "stderr:\n" + err_tail if err_tail else "",
    ) if part))
    result = "pass" if proc.returncode == 0 else "fail"
    try:
        _emit_check_progress(progress, "completed", index=index, total=total,
                             argv=argv, started_at=started_at,
                             returncode=proc.returncode, timed_out=False,
                             result=result, output_tail=output_tail)
    except Exception as exc:
        detail = _bounded_text(str(exc))
        raise GitGateError(f"Integration progress callback failed: {detail}") from exc
    return CommandResult(tuple(argv), proc.returncode, out_tail, err_tail)


def _git(repo: str | os.PathLike[str], *args: str, timeout: float = 20) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitGateError(f"git {' '.join(args[:3])} failed: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise GitGateError(f"git {' '.join(args[:3])} exited {result.returncode}: {detail}")
    return result.stdout.strip()


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


def _resolve_ref(repo: str | os.PathLike[str], ref: str) -> str:
    if not isinstance(ref, str) or not ref or ref.startswith("-"):
        raise GitGateError(f"Invalid Git ref: {ref!r}")
    try:
        sha = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    except GitGateError as exc:
        raise GitGateError(f"Ref {ref!r} does not resolve to a commit") from exc
    if not _is_sha(sha):
        raise GitGateError(f"Git returned an invalid commit id for {ref!r}")
    return sha


def _require_clean_worktree(repo: str | os.PathLike[str]) -> None:
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise GitGateError("Repository worktree is dirty; commit or stash changes before integration")


def _worktree_snapshot(repo: str | os.PathLike[str], timeout: float) -> tuple[bytes, ...]:
    """Capture tracked index/worktree diffs and non-ignored untracked contents."""
    repo = os.fspath(repo)
    try:
        index_tree = subprocess.run(["git", "-C", repo, "write-tree"], check=True,
                                    capture_output=True, timeout=timeout).stdout
        unstaged = subprocess.run(["git", "-C", repo, "diff", "--binary"], check=True,
                                  capture_output=True, timeout=timeout).stdout
        staged = subprocess.run(["git", "-C", repo, "diff", "--cached", "--binary"], check=True,
                                capture_output=True, timeout=timeout).stdout
        untracked = subprocess.run(["git", "-C", repo, "ls-files", "--others", "--exclude-standard", "-z"],
                                   check=True, capture_output=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitGateError(f"Could not snapshot integration worktree: {exc}") from exc
    file_state: list[bytes] = []
    for raw_path in untracked.split(b"\0"):
        if not raw_path:
            continue
        path = Path(repo) / os.fsdecode(raw_path)
        try:
            if path.is_symlink():
                contents = b"symlink\0" + os.fsencode(os.readlink(path))
            elif path.is_file():
                contents = path.read_bytes()
            else:
                contents = b"other\0"
        except OSError as exc:
            raise GitGateError(f"Could not snapshot untracked path {path}: {exc}") from exc
        file_state.append(raw_path + b"\0" + hashlib.sha256(contents).digest())
    return index_tree, unstaged, staged, untracked, *file_state


def validate_candidate(
    repo: str | os.PathLike[str],
    base_ref: str,
    feature_ref: str,
    *,
    require_clean: bool = True,
    timeout: float = 20,
) -> CandidateSnapshot:
    """Validate refs/ancestry and capture immutable SHAs and changed paths.

    The base may be a protected branch such as ``main``. The feature must be a
    local branch ref, and its commit must descend from the captured base.
    """
    repo_path = Path(repo).resolve()
    _git(repo_path, "rev-parse", "--show-toplevel", timeout=timeout)
    if require_clean:
        _require_clean_worktree(repo_path)
    if feature_ref.startswith("refs/remotes/") or feature_ref.startswith("origin/"):
        raise GitGateError("Feature ref must be a local branch")
    feature_branch = feature_ref.removeprefix("refs/heads/")
    base_sha = _resolve_ref(repo_path, base_ref)
    head_sha = _resolve_ref(repo_path, f"refs/heads/{feature_branch}")
    merge_base = _git(repo_path, "merge-base", base_sha, head_sha, timeout=timeout)
    if merge_base != base_sha:
        raise GitGateError("Feature branch does not descend from the selected base commit")
    paths = _git(repo_path, "diff", "--name-only", "-z", base_sha, head_sha, timeout=timeout)
    changed_paths = tuple(path for path in paths.split("\0") if path)
    if not changed_paths:
        raise GitGateError("Feature branch has no changes relative to the selected base")
    return CandidateSnapshot(
        repository=str(repo_path), base_ref=base_ref, feature_ref=f"refs/heads/{feature_branch}",
        base_sha=base_sha, head_sha=head_sha, changed_paths=changed_paths,
    )


def _validate_evidence(
    evidence: Mapping[str, Any], *, role: str, task_id: str, base_sha: str,
    head_sha: str, developer_id: str,
) -> str:
    if not isinstance(evidence, Mapping):
        raise EvidenceError(f"{role} evidence must be a JSON object")
    required = {"schema_version", "task_id", "role", "assignee", "run_id", "verdict",
                "base_sha", "head_sha", "findings", "checks"}
    missing = required - set(evidence)
    if missing:
        raise EvidenceError(f"{role} evidence missing fields: {', '.join(sorted(missing))}")
    if type(evidence["schema_version"]) is not int or evidence["schema_version"] != 1:
        raise EvidenceError("Evidence schema_version must be 1")
    if evidence["task_id"] != task_id:
        raise EvidenceError(f"{role} evidence belongs to a different task")
    if evidence["role"] != role:
        raise EvidenceError(f"Expected role {role!r}, got {evidence['role']!r}")
    for name in ("assignee", "run_id"):
        if not isinstance(evidence[name], str) or not evidence[name].strip():
            raise EvidenceError(f"{role} evidence {name} must be a non-empty string")
    if evidence["assignee"] == developer_id:
        raise EvidenceError(f"{role} must be performed by someone other than the developer")
    if evidence["base_sha"] != base_sha or evidence["head_sha"] != head_sha:
        raise EvidenceError(f"{role} evidence is stale or bound to different base/head SHAs")
    verdict = evidence["verdict"]
    if not isinstance(verdict, str) or verdict not in _ALLOWED_VERDICTS:
        raise EvidenceError(f"Invalid {role} verdict: {verdict!r}")
    if verdict != "pass":
        raise EvidenceError(f"{role} verdict {verdict!r} cannot promote the candidate")
    findings = evidence["findings"]
    if not isinstance(findings, list):
        raise EvidenceError(f"{role} findings must be a list")
    for index, finding in enumerate(findings):
        if not isinstance(finding, Mapping):
            raise EvidenceError(f"{role} finding {index} must be an object")
        severity = finding.get("severity")
        if not isinstance(severity, str) or severity not in _ALLOWED_SEVERITIES:
            raise EvidenceError(f"{role} finding {index} has invalid severity")
        if not isinstance(finding.get("blocking"), bool):
            raise EvidenceError(f"{role} finding {index} must include boolean blocking")
        if not isinstance(finding.get("description"), str) or not finding["description"].strip():
            raise EvidenceError(f"{role} finding {index} needs a description")
        if finding["blocking"] or severity in {"high", "critical"}:
            raise EvidenceError(f"{role} evidence contains an unresolved blocking/high-severity finding")
    checks = evidence["checks"]
    if not isinstance(checks, list) or not checks:
        raise EvidenceError(f"{role} evidence must contain at least one check")
    for index, check in enumerate(checks):
        if not isinstance(check, Mapping) or not isinstance(check.get("name"), str) or not check["name"].strip():
            raise EvidenceError(f"{role} check {index} needs a name")
        status = check.get("status")
        if not isinstance(status, str) or status not in _ALLOWED_CHECK_STATUSES:
            raise EvidenceError(f"{role} check {index} has invalid status")
        if status != "pass":
            raise EvidenceError(f"{role} check {check['name']!r} did not pass")
        if "command" in check and (not isinstance(check["command"], list) or
                                    any(not isinstance(arg, str) for arg in check["command"])):
            raise EvidenceError(f"{role} check command must be an argv list")
        if "exit_code" in check and check["exit_code"] != 0:
            raise EvidenceError(f"{role} check {check['name']!r} has non-zero exit_code")
    validate_supplemental_checks(evidence, role=role, head_sha=head_sha)
    return evidence["assignee"]


def validate_qa_verification_plan(
    evidence: Mapping[str, Any], plan: Mapping[str, Any],
) -> None:
    """Bind QA's executed and deferred checks to the controller-pinned plan.

    Deferred work is metadata, never a passing check. The QA report keeps only
    executed checks in ``checks`` and may describe planned deferrals in a
    separate, exact binding object. This prevents an aggregate or free-text
    ``skip`` from silently dropping required coverage.
    """
    if not isinstance(evidence, Mapping):
        raise EvidenceError("QA evidence must be a JSON object")
    if not isinstance(plan, Mapping) or plan.get("mode") not in {"quick", "full"}:
        raise EvidenceError("QA verification plan is malformed")
    selected = plan.get("selected_checks")
    deferred = plan.get("deferred_checks")
    digest = plan.get("policy_sha256")
    if (not isinstance(selected, list) or not selected
            or not isinstance(deferred, list)
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise EvidenceError("QA verification plan has malformed selected/deferred bindings")
    supplied = evidence.get("deferred")
    if not deferred and supplied is not None:
        raise EvidenceError("QA cannot report deferred checks when the plan has none")
    if deferred and plan.get("mode") != "quick":
        raise EvidenceError("QA checks may be deferred only in quick mode")

    checks = evidence.get("checks")
    if not isinstance(checks, list):
        raise EvidenceError("qa checks must be an array")
    if any(isinstance(row, Mapping) and row.get("status") == "skip" for row in checks):
        raise EvidenceError(
            "QA checks cannot contain skip rows; planned deferrals belong in the exact deferred binding"
        )
    # _validate_evidence rejects all non-pass checks, but this plan-level check
    # also requires the exact planned command and prevents duplicate coverage.
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for entry in selected:
        if not isinstance(entry, Mapping):
            raise EvidenceError("QA selected check binding is malformed")
        name, argv = entry.get("name"), entry.get("argv")
        if (not isinstance(name, str) or not name.strip()
                or not isinstance(argv, list) or not argv
                or any(not isinstance(arg, str) for arg in argv)):
            raise EvidenceError("QA selected check binding needs exact name and argv")
        key = (name, tuple(argv))
        if key in seen:
            raise EvidenceError(f"QA selected check {name!r} is duplicated in the plan")
        seen.add(key)
        matches = [row for row in checks if isinstance(row, Mapping)
                   and row.get("name") == name and row.get("command") == argv]
        if len(matches) != 1:
            raise EvidenceError(f"QA must report exactly one executed check for planned command {name!r}")
        row = matches[0]
        if row.get("status") not in {"pass", "fail"}:
            raise EvidenceError(f"QA planned check {name!r} must report its actual pass/fail result")
        if type(row.get("exit_code")) is not int:
            raise EvidenceError(f"QA planned check {name!r} needs its actual integer exit_code")
        if row.get("status") == "pass" and row["exit_code"] != 0:
            raise EvidenceError(f"QA planned check {name!r} says pass but has non-zero exit_code")

    if not deferred:
        return
    if not isinstance(supplied, Mapping):
        raise EvidenceError("QA deferred checks need exact VERIFICATION-PLAN.json bindings")
    if set(supplied) != {"evidence_ref", "policy_sha256", "checks"}:
        raise EvidenceError("QA deferred binding has unsupported or missing fields")
    if supplied.get("evidence_ref") != "VERIFICATION-PLAN.json":
        raise EvidenceError("QA deferred binding must reference VERIFICATION-PLAN.json exactly")
    if supplied.get("policy_sha256") != digest:
        raise EvidenceError("QA deferred binding policy digest does not match the pinned plan")
    bound_checks = supplied.get("checks")
    if not isinstance(bound_checks, list) or bound_checks != deferred:
        raise EvidenceError("QA deferred checks must exactly match every pinned deferred check")


def validate_supplemental_checks(
    evidence: Mapping[str, Any], *, role: str, head_sha: str,
) -> None:
    """Validate non-gating negative evidence without weakening mandatory checks.

    Mutation-sensitivity experiments are expected to make a test command fail
    when a mutant is killed. They belong in ``supplemental_checks`` rather than
    ``checks``: only ``checks`` can satisfy the promotion gate, and every command
    there must still pass with exit code zero.
    """
    supplemental = evidence.get("supplemental_checks", [])
    if not isinstance(supplemental, list):
        raise EvidenceError(f"{role} supplemental_checks must be a list")
    if supplemental and role != "qa":
        raise EvidenceError("supplemental_checks are supported only for QA evidence")

    required = {
        "kind", "name", "command", "mutant_id", "killed_by", "exit_code",
        "head_sha", "scratch_worktree", "clean_after_revert", "evidence",
    }
    for index, check in enumerate(supplemental):
        if not isinstance(check, Mapping):
            raise EvidenceError(f"QA supplemental check {index} must be an object")
        missing = required - set(check)
        if missing:
            raise EvidenceError(
                f"QA supplemental check {index} missing fields: {', '.join(sorted(missing))}"
            )
        if check["kind"] != "mutation_killed":
            raise EvidenceError(f"QA supplemental check {index} has unsupported kind")
        for field in ("name", "mutant_id", "killed_by", "scratch_worktree", "evidence"):
            value = check[field]
            if not isinstance(value, str) or not value.strip():
                raise EvidenceError(f"QA supplemental check {index} {field} must be a non-empty string")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", check["mutant_id"]):
            raise EvidenceError(f"QA supplemental check {index} has invalid mutant_id")
        command = check["command"]
        if (not isinstance(command, list) or not command
                or any(not isinstance(arg, str) for arg in command)):
            raise EvidenceError(f"QA supplemental check {index} command must be a non-empty argv list")
        # Gradle's failing-test exit code is the expected result for a killed
        # mutant. Other failures (including timeout 124) cannot be represented
        # as mutation evidence by this schema.
        if type(check["exit_code"]) is not int or check["exit_code"] != 1:
            raise EvidenceError(f"QA supplemental check {index} must record killed-mutant exit_code 1")
        if check["head_sha"] != head_sha:
            raise EvidenceError(f"QA supplemental check {index} is bound to a different head SHA")
        if check["clean_after_revert"] is not True:
            raise EvidenceError(f"QA supplemental check {index} must confirm clean scratch worktree after revert")


def validate_promotion_evidence(
    reviewer_evidence: Mapping[str, Any], qa_evidence: Mapping[str, Any], *,
    task_id: str, base_sha: str, head_sha: str, developer_id: str,
    qa_verification_plan: Mapping[str, Any] | None = None,
) -> None:
    """Require independent, passing review and QA evidence for exact SHAs."""
    reviewer = _validate_evidence(reviewer_evidence, role="reviewer", task_id=task_id,
                                  base_sha=base_sha, head_sha=head_sha, developer_id=developer_id)
    if qa_verification_plan is not None:
        validate_qa_verification_plan(qa_evidence, qa_verification_plan)
    elif isinstance(qa_evidence, Mapping) and "deferred" in qa_evidence:
        raise EvidenceError("QA deferred metadata requires the controller-pinned verification plan")
    qa = _validate_evidence(qa_evidence, role="qa", task_id=task_id,
                            base_sha=base_sha, head_sha=head_sha, developer_id=developer_id)
    if reviewer == qa:
        raise EvidenceError("Reviewer and QA must be different assignees")
    if reviewer_evidence["run_id"] == qa_evidence["run_id"]:
        raise EvidenceError("Reviewer and QA must have distinct run_id values")



def run_batch_regression(
    repo: str | os.PathLike[str],
    head_sha: str,
    trusted_test_commands: Sequence[Sequence[str] | Mapping[str, Any]],
    *,
    worktree_parent: str | os.PathLike[str] | None = None,
    timeout: float = 1800,
    check_progress: Callable[[Mapping[str, Any]], None] | None = None,
    check_lock_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Run every trusted regression check at one immutable cumulative SHA.

    Each command gets its own timeout. A failure or timeout is recorded and
    later checks still run, so one batch produces a complete repair report.
    The candidate is tested in a temporary detached worktree; generated
    changes invalidate the batch and are never committed or merged.
    """
    repo_path = Path(repo).resolve()
    _require_clean_worktree(repo_path)
    if not _is_sha(head_sha):
        raise GitGateError("Batch regression requires a full immutable commit SHA")
    resolved_sha = _resolve_ref(repo_path, head_sha)
    if resolved_sha != head_sha:
        raise GitGateError("Batch regression head_sha must resolve to itself")
    commands: list[tuple[str, tuple[str, ...]]] = []
    for index, raw in enumerate(trusted_test_commands):
        if isinstance(raw, Mapping):
            name, argv = raw.get("name"), raw.get("argv")
            if not isinstance(name, str) or not name.strip():
                raise GitGateError(f"Batch check {index} needs a name")
        else:
            name, argv = f"check-{index + 1}", raw
        if not isinstance(argv, (list, tuple)) or not argv or any(
                not isinstance(arg, str) or not arg for arg in argv):
            raise GitGateError(f"Batch check {name!r} needs a non-empty argv list")
        commands.append((name, tuple(argv)))
    if not commands:
        raise GitGateError("Batch regression requires at least one trusted check")
    parent = Path(worktree_parent).resolve() if worktree_parent else Path(tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    worktree_path = Path(tempfile.mkdtemp(prefix="hermes-batch-regression-", dir=parent))
    worktree_path.rmdir()
    attached = False
    results: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        _git(repo_path, "worktree", "add", "--detach", str(worktree_path), head_sha)
        attached = True
        initial_snapshot = _worktree_snapshot(worktree_path, 20)
        for index, (name, argv) in enumerate(commands, start=1):
            command_started = time.monotonic()
            try:
                if delegated_test_runner(argv):
                    # The host-side backend queue acquires this same shared lock.
                    # Holding it here would deadlock while waiting for that worker.
                    result = _run_trusted_check(argv, cwd=worktree_path, timeout=timeout,
                                                index=index, total=len(commands),
                                                progress=check_progress)
                else:
                    with heavy_test_lock(check_lock_path):
                        result = _run_trusted_check(argv, cwd=worktree_path, timeout=timeout,
                                                    index=index, total=len(commands),
                                                    progress=check_progress)
                results.append({"name": name, "argv": list(argv),
                    "status": "pass" if result.exit_code == 0 else "fail",
                    "exit_code": result.exit_code, "stdout": result.stdout,
                    "stderr": result.stderr,
                    "duration_seconds": round(time.monotonic() - command_started, 3)})
            except GitGateError as exc:
                detail = _bounded_text(str(exc))
                timed_out = "timed out" in detail.lower()
                results.append({"name": name, "argv": list(argv),
                    "status": "timeout" if timed_out else "error",
                    "exit_code": 124 if timed_out else None,
                    "error": detail,
                    "duration_seconds": round(time.monotonic() - command_started, 3)})
        if _worktree_snapshot(worktree_path, 20) != initial_snapshot:
            results.append({"name": "worktree-cleanliness", "argv": [],
                            "status": "fail", "exit_code": 1,
                            "error": "Regression checks modified the detached worktree"})
    finally:
        if attached:
            subprocess.run(["git", "-C", os.fspath(repo_path), "worktree", "remove",
                            "--force", os.fspath(worktree_path)],
                           capture_output=True, timeout=30)
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)
    failed = [result for result in results if result["status"] != "pass"]
    return {"head_sha": head_sha, "status": "fail" if failed else "pass",
            "results": results, "failed_count": len(failed),
            "duration_seconds": round(time.monotonic() - started, 3)}


def _worktree_records(repo: Path) -> list[dict[str, str]]:
    output = _git(repo, "worktree", "list", "--porcelain")
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in output.splitlines() + [""]:
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return records


def _remove_interrupted_integration_worktrees(
    repo: Path, *, branch_ref: str, task_id: str,
    worktree_parent: Path, git_timeout: float,
) -> None:
    """Remove only stale worktrees in this task's configured integration dir."""
    prefix = f"hermes-team-{task_id}-"
    for record in _worktree_records(repo):
        if record.get("branch") != branch_ref:
            continue
        raw_path = record.get("worktree")
        if not raw_path:
            raise GitGateError("Cannot safely identify an interrupted integration worktree")
        path = Path(raw_path).resolve()
        if (not path.is_relative_to(worktree_parent)
                or not path.name.startswith(prefix)):
            raise GitGateError("Integration branch is checked out outside its owned temporary directory")
        result = subprocess.run(["git", "-C", os.fspath(repo), "worktree", "remove",
                                 "--force", os.fspath(path)], capture_output=True,
                                text=True, timeout=git_timeout)
        if result.returncode:
            raise GitGateError("Could not remove the owned interrupted integration worktree: "
                               + _bounded_text(result.stderr.strip()))


def _recover_integration_branch(
    repo: Path, *, branch: str, branch_ref: str, task_id: str,
    base_sha: str, candidate_sha: str, worktree_parent: Path,
    git_timeout: float,
) -> str | None:
    """Classify an existing deterministic integration branch for safe retry.

    Return ``None`` after removing a pre-commit branch at the exact base, or
    return a previously committed merge SHA whose two parents exactly match
    this invocation. All other branch states fail closed.
    """
    exists = subprocess.run(["git", "-C", os.fspath(repo), "show-ref", "--verify", "--quiet", branch_ref],
                            capture_output=True, timeout=git_timeout).returncode == 0
    if not exists:
        return None
    tip = _resolve_ref(repo, branch_ref)
    if tip == base_sha:
        _remove_interrupted_integration_worktrees(
            repo, branch_ref=branch_ref, task_id=task_id,
            worktree_parent=worktree_parent, git_timeout=git_timeout)
        _git(repo, "branch", "-D", branch, timeout=git_timeout)
        return None
    parents_line = _git(repo, "rev-list", "--parents", "-n", "1", tip, timeout=git_timeout)
    parents = parents_line.split()[1:]
    if parents != [base_sha, candidate_sha]:
        raise GitGateError(
            f"Integration branch already exists with an unrecognized state: {branch}; "
            "expected the original base or a merge commit for this exact candidate"
        )
    _remove_interrupted_integration_worktrees(
        repo, branch_ref=branch_ref, task_id=task_id,
        worktree_parent=worktree_parent, git_timeout=git_timeout)
    return tip

def integrate_candidate(
    repo: str | os.PathLike[str], *, task_id: str, base_ref: str, head_sha: str,
    developer_id: str, reviewer_evidence: Mapping[str, Any], qa_evidence: Mapping[str, Any],
    trusted_test_commands: Iterable[Sequence[str]],
    qa_verification_plan: Mapping[str, Any] | None = None, timeout: float = 300,
    git_timeout: float = 30, worktree_parent: str | os.PathLike[str] | None = None,
    check_progress: Callable[[Mapping[str, Any]], None] | None = None,
    check_lock_path: str | os.PathLike[str] | None = None,
) -> IntegrationResult:
    """Test and merge a reviewed SHA in a dedicated local integration worktree.

    The base branch is never checked out or moved. On any failure, the temporary
    worktree and its branch are removed. Test commands must come from trusted
    project configuration and are run directly as argv (never through a shell).
    ``check_progress`` optionally receives ``started``, ``heartbeat``, and
    ``completed`` mappings for each check. A callback failure aborts the gate.
    """
    if not _SAFE_ID_RE.fullmatch(task_id):
        raise GitGateError("task_id must contain only letters, digits, dot, underscore, or hyphen")
    repo_path = Path(repo).resolve()
    _require_clean_worktree(repo_path)
    base_sha = _resolve_ref(repo_path, base_ref)
    candidate_sha = _resolve_ref(repo_path, head_sha)
    if candidate_sha != head_sha:
        raise GitGateError("head_sha must be a full immutable commit SHA")
    _git(repo_path, "merge-base", "--is-ancestor", base_sha, candidate_sha, timeout=git_timeout)
    validate_promotion_evidence(reviewer_evidence, qa_evidence, task_id=task_id,
                                base_sha=base_sha, head_sha=candidate_sha,
                                developer_id=developer_id,
                                qa_verification_plan=qa_verification_plan)
    commands = [tuple(command) for command in trusted_test_commands]
    if not commands or any(not command or any(not isinstance(arg, str) for arg in command) for command in commands):
        raise GitGateError("At least one trusted test command argv is required")

    branch = f"codex/team-integration/{task_id}"
    branch_ref = f"refs/heads/{branch}"
    parent = Path(worktree_parent).resolve() if worktree_parent else Path(tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    recovered_sha = _recover_integration_branch(
        repo_path, branch=branch, branch_ref=branch_ref, task_id=task_id,
        base_sha=base_sha, candidate_sha=candidate_sha, worktree_parent=parent,
        git_timeout=git_timeout)
    worktree_path = Path(tempfile.mkdtemp(prefix=f"hermes-team-{task_id}-", dir=parent))
    worktree_path.rmdir()
    attached = False
    created_branch = recovered_sha is None
    try:
        if recovered_sha is None:
            _git(repo_path, "worktree", "add", "-b", branch, str(worktree_path), base_sha,
                 timeout=git_timeout)
        else:
            _git(repo_path, "worktree", "add", "--detach", str(worktree_path), base_sha,
                 timeout=git_timeout)
        attached = True
        _git(worktree_path, "merge", "--no-ff", "--no-commit", candidate_sha, timeout=git_timeout)
        if recovered_sha is not None:
            expected_tree = _git(worktree_path, "write-tree", timeout=git_timeout)
            existing_tree = _git(repo_path, "rev-parse", f"{recovered_sha}^{{tree}}", timeout=git_timeout)
            if expected_tree != existing_tree:
                raise GitGateError("Interrupted integration commit tree differs from the exact base/candidate merge")
            _git(worktree_path, "reset", "--hard", recovered_sha, timeout=git_timeout)
        merge_snapshot = _worktree_snapshot(worktree_path, git_timeout)
        check_results: list[CommandResult] = []
        for index, argv in enumerate(commands, start=1):
            if delegated_test_runner(argv):
                # The isolated host worker owns the shared lock for this command.
                result = _run_trusted_check(
                    argv, cwd=worktree_path, timeout=timeout, index=index,
                    total=len(commands), progress=check_progress,
                )
            else:
                with heavy_test_lock(check_lock_path):
                    result = _run_trusted_check(
                        argv, cwd=worktree_path, timeout=timeout, index=index,
                        total=len(commands), progress=check_progress,
                    )
            check_results.append(result)
            if result.exit_code != 0:
                detail = _bounded_text(result.stderr or result.stdout)
                raise GitGateError(
                    f"Integration check {_display_argv(argv)!r} exited {result.exit_code}"
                    + (f": {detail}" if detail else "")
                )
        if _worktree_snapshot(worktree_path, git_timeout) != merge_snapshot:
            raise GitGateError("Integration checks modified the worktree; refusing to commit generated changes")
        if recovered_sha is None:
            _git(worktree_path, "commit", "--no-edit", timeout=git_timeout)
            integration_sha = _resolve_ref(worktree_path, "HEAD")
        else:
            integration_sha = recovered_sha
        paths = _git(repo_path, "diff", "--name-only", "-z", base_sha, candidate_sha, timeout=git_timeout)
        return IntegrationResult(branch, str(worktree_path), base_sha, candidate_sha, integration_sha,
                                 tuple(path for path in paths.split("\0") if path), tuple(check_results))
    except Exception:
        if attached:
            subprocess.run(["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree_path)],
                           capture_output=True, timeout=git_timeout)
        if created_branch:
            subprocess.run(["git", "-C", str(repo_path), "branch", "-D", branch],
                           capture_output=True, timeout=git_timeout)
        raise


def evidence_template(*, task_id: str, role: str, assignee: str, run_id: str,
                      base_sha: str, head_sha: str) -> dict[str, Any]:
    """Return an empty schema-v1 evidence object for an agent to complete."""
    if role not in _ALLOWED_ROLES:
        raise ValueError(f"role must be one of {sorted(_ALLOWED_ROLES)}")
    return {
        "schema_version": 1,
        "task_id": task_id,
        "role": role,
        "assignee": assignee,
        "run_id": run_id,
        "verdict": "blocked",
        "base_sha": base_sha,
        "head_sha": head_sha,
        "findings": [],
        "checks": [],
    }
