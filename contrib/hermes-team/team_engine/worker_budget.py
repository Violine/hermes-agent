"""Fail-closed budget accounting for team-managed native Hermes workers.

This module is deliberately independent of controller internals. It reads the
fixed team config and run state, and cross-checks every binding against the
native Kanban SQLite database before applying an override or writing usage.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
from typing import Any, Mapping

SCHEMA_VERSION = 1
_TASK_ID = re.compile(r"t_[A-Za-z0-9_-]{4,80}\Z")
_RUN_ID = re.compile(r"[1-9][0-9]{0,15}\Z")
_PROFILE = re.compile(r"team-[a-z0-9][a-z0-9-]{0,48}\Z")
_RESUMABLE_WAIT_STATES = {"resuming", "resumed"}
_TEAM_CONTROLLER = "hermes-team-controller"
_TERMINAL_RUN_STATUSES = {"blocked", "cancelled", "completed", "done", "failed", "gave_up", "timed_out"}
_EXHAUSTED_RUN_STATUSES = {"failed", "blocked", "timed_out", "gave_up"}
_ATTEMPT_LIMIT_FIELDS = {
    "schema_version", "card_id", "assignee", "previous_run_id", "max_seconds",
    "max_turns", "purpose", "created_at",
}
_VERIFIER_ATTEMPT_BINDING_FIELDS = {
    "item_id", "round", "expected_sha", "approved_spec_sha256",
    "approved_plan_sha256", "workspace",
}
_ATTEMPT_LIMIT_MAX_SECONDS = 600
_ATTEMPT_LIMIT_MAX_TURNS = 20


class WorkerBudgetError(RuntimeError):
    """A team worker's run binding is invalid or its remaining budget is spent."""


class NotTeamManaged(WorkerBudgetError):
    """The card belongs to a native non-team workflow and must be untouched."""


def validate_attempt_limit(
    value: Any, *, card_id: str, assignee: str,
) -> dict[str, Any]:
    """Validate and copy the controller's strict one-shot retry grant.

    This validation is intentionally usable by the controller before it
    dispatches a successor. Native run lineage is verified separately by the
    worker after Hermes has assigned the successor run id.
    """
    if not isinstance(value, Mapping):
        raise WorkerBudgetError("one-shot worker attempt limit has invalid fields")
    purpose = value.get("purpose")
    expected_fields = (_ATTEMPT_LIMIT_FIELDS | _VERIFIER_ATTEMPT_BINDING_FIELDS
                       if purpose == "finalize_existing_verification"
                       else _ATTEMPT_LIMIT_FIELDS)
    if set(value) != expected_fields:
        raise WorkerBudgetError("one-shot worker attempt limit has invalid fields")
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("card_id") != card_id
            or value.get("assignee") != assignee
            or not isinstance(value.get("previous_run_id"), str)
            or not _RUN_ID.fullmatch(value["previous_run_id"])
            or type(value.get("max_seconds")) is not int
            or not 1 <= value["max_seconds"] <= _ATTEMPT_LIMIT_MAX_SECONDS
            or type(value.get("max_turns")) is not int
            or not 1 <= value["max_turns"] <= _ATTEMPT_LIMIT_MAX_TURNS
            or value.get("purpose") not in {
                "finalize_existing_implementation",
                "finalize_existing_verification",
            }
            or type(value.get("created_at")) is not int or value["created_at"] < 1):
        raise WorkerBudgetError("one-shot worker attempt limit has invalid bindings or caps")
    # Keep the grant purpose aligned with the native profile. A persisted or
    # forged verifier grant must never become a developer retry (or vice versa).
    if ((value["purpose"] == "finalize_existing_implementation"
         and assignee not in {"team-backend", "team-frontend", "team-mobile", "team-go"})
            or (value["purpose"] == "finalize_existing_verification"
                and assignee not in {"team-reviewer", "team-qa"})):
        raise WorkerBudgetError("one-shot worker attempt purpose does not match its role")
    if value["purpose"] == "finalize_existing_verification":
        if (not isinstance(value["item_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value["item_id"])
                or type(value["round"]) is not int or value["round"] < 1
                or not isinstance(value["expected_sha"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", value["expected_sha"])
                or any(not isinstance(value[field], str)
                       or not re.fullmatch(r"[0-9a-f]{64}", value[field])
                       for field in ("approved_spec_sha256", "approved_plan_sha256"))
                or not isinstance(value["workspace"], str)
                or not Path(value["workspace"]).is_absolute()):
            raise WorkerBudgetError("one-shot verifier attempt has invalid candidate bindings")
    return dict(value)


@dataclass(frozen=True)
class WorkerBinding:
    card_id: str
    run_id: str
    assignee: str
    feature_id: str
    state_path: Path
    prerequisite_dir: Path
    data_root: Path
    ledger: Mapping[str, Any] | None
    wait_state: Mapping[str, Any] | None
    attempt_limit: Mapping[str, Any] | None = None


def _plain_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _read_json(path: Path, *, max_bytes: int = 2_000_000) -> dict[str, Any] | None:
    if not _plain_file(path):
        return None
    try:
        if path.stat().st_size > max_bytes:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _fixed_config_path(home: Path) -> Path:
    return home / ".hermes" / "team" / "config.json"


def _data_root(home: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        candidate = explicit
    else:
        config = _read_json(_fixed_config_path(home), max_bytes=256_000)
        if config is None or not isinstance(config.get("data_root"), str):
            raise WorkerBudgetError("team config is unavailable")
        candidate = Path(config["data_root"])
    if not candidate.is_absolute():
        raise WorkerBudgetError("team data root must be absolute")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir() or candidate.is_symlink():
        raise WorkerBudgetError("team data root must be a plain directory")
    return resolved


def _native_task_row(db_path: Path, card_id: str, run_id: str) -> tuple[dict, dict]:
    if not db_path.is_absolute() or db_path.is_symlink() or not _plain_file(db_path):
        raise WorkerBudgetError("native Kanban database is unavailable")
    uri = "file:" + str(db_path) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            task = conn.execute(
                "SELECT id, assignee, status, current_run_id, created_by FROM tasks WHERE id = ?",
                (card_id,),
            ).fetchone()
            run = conn.execute(
                "SELECT id, task_id, profile, started_at, ended_at, status, outcome "
                "FROM task_runs WHERE id = ? AND task_id = ?",
                (int(run_id), card_id),
            ).fetchone()
            if task is None or run is None:
                raise WorkerBudgetError("Kanban card/run binding is missing")
            return dict(task), dict(run)
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, OSError) as exc:
        if isinstance(exc, WorkerBudgetError):
            raise
        raise WorkerBudgetError("native Kanban binding could not be verified") from exc


def _card_is_declared(state: Mapping[str, Any], card_id: str) -> bool:
    known_keys = {"card", "card_id", "root_card", "reviewer_card", "qa_card",
                  "integrator_card", "developer_card", "implementation_card"}
    for key, value in state.items():
        if (key in known_keys or str(key).endswith("_card")) and value == card_id:
            return True
    items = state.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            for key, value in item.items():
                if (key in known_keys or str(key).endswith("_card")) and value == card_id:
                    return True
    return False


def _wait_state_for(
    state: Mapping[str, Any], card_id: str, latest_wait_run_id: str | None,
) -> Mapping[str, Any] | None:
    waits = state.get("test_waits")
    if isinstance(waits, dict):
        value = waits.get(card_id)
        if isinstance(value, dict) and value.get("card_id", card_id) == card_id:
            return value
        candidates = [v for v in waits.values()
                      if isinstance(v, dict) and v.get("card_id") == card_id
                      and str(v.get("run_id", "")) == str(latest_wait_run_id)]
        if len(candidates) == 1:
            return candidates[0]
    if isinstance(waits, list):
        matches = [v for v in waits if isinstance(v, dict) and v.get("card_id") == card_id
                   and str(v.get("run_id", "")) == str(latest_wait_run_id)]
        if len(matches) == 1:
            return matches[0]
    return None


def _one_shot_attempt_limit(
    state: Mapping[str, Any], *, db_path: Path, card_id: str, profile: str,
    active_run_id: str, state_path: Path, require_active: bool,
) -> Mapping[str, Any] | None:
    """Validate a controller-issued, single-successor per-card budget grant."""
    if "worker_attempt_limits" not in state:
        return None
    limits = state.get("worker_attempt_limits")
    if not isinstance(limits, dict):
        raise WorkerBudgetError("one-shot worker attempt limits are malformed")
    if card_id not in limits:
        return None
    value = validate_attempt_limit(limits[card_id], card_id=card_id, assignee=profile)
    if value["purpose"] == "finalize_existing_verification":
        _verify_verifier_attempt_snapshot(state, state_path, card_id, profile, value,
                                          require_report_absent=require_active)
    try:
        previous_run_id = int(value["previous_run_id"])
        current_run_id = int(active_run_id)
        conn = sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            previous = conn.execute(
                "SELECT id, task_id, profile, ended_at, status FROM task_runs "
                "WHERE id = ? AND task_id = ?",
                (previous_run_id, card_id),
            ).fetchone()
            successors = conn.execute(
                "SELECT id, task_id, profile FROM task_runs "
                "WHERE task_id = ? AND id > ? ORDER BY id",
                (card_id, previous_run_id),
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, ValueError, OSError) as exc:
        raise WorkerBudgetError("one-shot worker attempt history could not be verified") from exc
    if (previous is None or previous["task_id"] != card_id or previous["profile"] != profile
            or previous["ended_at"] is None
            or previous["status"] not in _EXHAUSTED_RUN_STATUSES
            or previous_run_id >= current_run_id):
        raise WorkerBudgetError("one-shot worker attempt does not follow a matching terminal run")
    if (len(successors) != 1 or int(successors[0]["id"]) != current_run_id
            or successors[0]["task_id"] != card_id or successors[0]["profile"] != profile):
        raise WorkerBudgetError("one-shot worker attempt is not the only successor of its prior run")
    return value


def _verify_verifier_attempt_snapshot(
    state: Mapping[str, Any], state_path: Path, card_id: str, profile: str,
    grant: Mapping[str, Any], *, require_report_absent: bool,
) -> None:
    """Recheck a verifier grant's pinned workspace and approved docs at worker start."""
    role = profile.removeprefix("team-")
    items = state.get("items")
    matches = [item for item in items if isinstance(item, dict)
               and item.get("definition", {}).get("id") == grant["item_id"]
               and item.get(role + "_card") == card_id] if isinstance(items, list) else []
    if len(matches) != 1:
        raise WorkerBudgetError("verifier attempt task/card binding changed")
    item = matches[0]
    workspace = Path(item.get(role + "_workspace", ""))
    feature_dir = state_path.parent.resolve(strict=True)
    worktrees_root = (feature_dir / "worktrees").resolve(strict=True)
    try:
        if (item.get("round") != grant["round"] or item.get("head_sha") != grant["expected_sha"]
                or str(workspace) != grant["workspace"] or workspace.is_symlink()
                or not workspace.is_absolute()):
            raise WorkerBudgetError("verifier attempt candidate, round, or workspace changed")
        resolved_workspace = workspace.resolve(strict=True)
        resolved_workspace.relative_to(worktrees_root)
        if resolved_workspace == worktrees_root:
            raise WorkerBudgetError("verifier worktree is not task-scoped")
        head = subprocess.run(
            ["git", "-C", str(resolved_workspace), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(resolved_workspace), "status", "--porcelain", "--untracked-files=all"],
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise WorkerBudgetError("verifier attempt worktree could not be verified") from exc
    if head != grant["expected_sha"] or dirty:
        raise WorkerBudgetError("verifier attempt worktree changed after allowance preparation")
    if (state.get("approved_spec_sha256") != grant["approved_spec_sha256"]
            or state.get("approved_plan_sha256") != grant["approved_plan_sha256"]):
        raise WorkerBudgetError("verifier attempt approved document bindings changed")
    artifacts = feature_dir / "artifacts"
    spec = artifacts / "SPEC.md"
    if not _plain_file(spec):
        raise WorkerBudgetError("approved SPEC snapshot is unavailable")
    digest = hashlib.sha256()
    try:
        for name in ("SPEC.md", "DESIGN.md", "PLAN.json"):
            path = artifacts / name
            if not _plain_file(path):
                raise WorkerBudgetError("approved plan snapshot is unavailable")
            digest.update(name.encode() + b"\x00")
            digest.update(path.read_bytes())
            digest.update(b"\x00")
    except OSError as exc:
        raise WorkerBudgetError("approved documents could not be verified") from exc
    if (hashlib.sha256(spec.read_bytes()).hexdigest() != grant["approved_spec_sha256"]
            or digest.hexdigest() != grant["approved_plan_sha256"]):
        raise WorkerBudgetError("approved SPEC/PLAN files changed after allowance preparation")
    report = Path(item.get(role + "_report_path", ""))
    round_dir = feature_dir / "tasks" / grant["item_id"] / f"round-{grant['round']}"
    try:
        report.resolve(strict=False).relative_to(round_dir.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise WorkerBudgetError("verifier report path escaped its pinned round") from exc
    if report.is_symlink() or (require_report_absent and report.exists()):
        raise WorkerBudgetError("verifier report exists before bounded finalization")


def _find_state(data_root: Path, card_id: str, profile: str) -> tuple[str, Path, Path, dict, Path]:
    runs = data_root / "runs"
    if runs.is_symlink() or not runs.is_dir():
        raise WorkerBudgetError("team run registry is unavailable")
    matches = []
    for state_path in runs.glob("*/state.json"):
        if state_path.is_symlink() or state_path.parent.is_symlink():
            continue
        state = _read_json(state_path)
        if state is None or not _card_is_declared(state, card_id):
            continue
        prerequisites = state.get("prerequisite_paths")
        snapshots = state.get("assignment_snapshots")
        if not isinstance(prerequisites, dict) or not isinstance(snapshots, dict):
            continue
        raw_prereq = prerequisites.get(card_id)
        snapshot = snapshots.get(card_id)
        if not isinstance(raw_prereq, str) or not isinstance(snapshot, dict):
            continue
        if snapshot.get("role") != profile:
            continue
        prerequisite_dir = Path(raw_prereq)
        try:
            resolved = prerequisite_dir.resolve(strict=True)
            feature_dir = state_path.parent.resolve(strict=True)
            resolved.relative_to(feature_dir / "prerequisites")
        except (OSError, ValueError):
            continue
        if prerequisite_dir.is_symlink() or not resolved.is_dir():
            continue
        matches.append((str(state.get("id", "")), state_path, resolved, state, data_root))
    if len(matches) != 1:
        raise WorkerBudgetError("team card must map to exactly one feature state")
    return matches[0]


def resolve_worker_binding(
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    profile: str | None = None,
    db_path: str | Path | None = None,
    home: str | Path | None = None,
    data_root: str | Path | None = None,
    require_active: bool = True,
) -> WorkerBinding:
    """Verify the worker env against native task/run and the team state file."""
    task_id = task_id if task_id is not None else os.environ.get("HERMES_KANBAN_TASK")
    run_id = run_id if run_id is not None else os.environ.get("HERMES_KANBAN_RUN_ID")
    profile = profile if profile is not None else os.environ.get("HERMES_PROFILE")
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise WorkerBudgetError("invalid Kanban task binding")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise WorkerBudgetError("invalid Kanban run binding")
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
        raise WorkerBudgetError("invalid team profile binding")
    if db_path is None:
        raw_db = os.environ.get("HERMES_KANBAN_DB")
        if not raw_db:
            raise WorkerBudgetError("native Kanban database binding is missing")
        db_path = raw_db
    db_path = Path(db_path)
    home_path = Path(home) if home is not None else Path.home()
    home_path = home_path.resolve(strict=True)
    # In production the dispatcher pins the DB below the Hermes home. Tests may
    # inject a temporary home, preserving the same containment rule.
    try:
        hermes_home = home_path / ".hermes"
        if hermes_home.is_symlink():
            raise ValueError("managed Hermes home is a symlink")
        resolved_db = db_path.resolve(strict=True)
        resolved_hermes = hermes_home.resolve(strict=True)
        # Compare canonical paths so normal platform aliases such as macOS
        # /var -> /private/var do not make a trusted DB look external.
        resolved_db.relative_to(resolved_hermes)
    except (OSError, ValueError) as exc:
        raise WorkerBudgetError("Kanban database is outside the managed Hermes home") from exc
    task, run = _native_task_row(db_path, task_id, run_id)
    if task.get("created_by") != _TEAM_CONTROLLER:
        raise NotTeamManaged("Kanban card is not owned by the team controller")
    root = _data_root(home_path, Path(data_root) if data_root is not None else None)
    if task.get("assignee") != profile or run.get("profile") != profile:
        raise WorkerBudgetError("Kanban assignee does not match worker profile")
    if require_active:
        if task.get("status") != "running" or str(task.get("current_run_id")) != run_id:
            raise WorkerBudgetError("Kanban run is no longer the active attempt")
    elif run.get("ended_at") is None:
        raise WorkerBudgetError("Kanban run is not terminal")
    feature_id, state_path, prerequisite_dir, state, root = _find_state(root, task_id, profile)
    ledgers = state.get("test_wait_budgets")
    ledger = ledgers.get(task_id) if isinstance(ledgers, dict) else None
    wait_state = _wait_state_for(
        state, task_id,
        str(ledger.get("latest_wait_run_id")) if isinstance(ledger, dict)
        and ledger.get("latest_wait_run_id") is not None else None,
    )
    if ledger is not None:
        if not isinstance(ledger, dict) or ledger.get("card_id") != task_id or ledger.get("assignee") != profile:
            raise WorkerBudgetError("test wait budget ledger binding is invalid")
    attempt_limit = _one_shot_attempt_limit(
        state, db_path=db_path, card_id=task_id, profile=profile,
        active_run_id=run_id, state_path=state_path, require_active=require_active,
    )
    return WorkerBinding(task_id, run_id, profile, feature_id, state_path,
                         prerequisite_dir, root, ledger, wait_state, attempt_limit)


def _positive_number(value: Any, *, field: str, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkerBudgetError(f"invalid {field} in test wait budget")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (not allow_zero and number <= 0):
        raise WorkerBudgetError(f"invalid {field} in test wait budget")
    return number


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkerBudgetError(f"invalid {field} in test wait budget")
    return value


def apply_worker_budget_override(agent: Any) -> bool:
    """Apply only a trusted, resumed test-wait budget to the current worker.

    Returns False for ordinary Hermes cards. A malformed team binding raises
    ``WorkerBudgetError`` so callers can fail closed without making a model call.
    """
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return False
    profile = os.environ.get("HERMES_PROFILE", "")
    # Native non-team profiles (for example `default`) share Hermes' turn
    # context hook and must remain completely untouched by team enforcement.
    if not profile.startswith("team-"):
        return False
    if not _PROFILE.fullmatch(profile):
        raise WorkerBudgetError("invalid team profile binding")
    try:
        binding = resolve_worker_binding(require_active=True)
    except NotTeamManaged:
        return False
    ledger, wait = binding.ledger, binding.wait_state
    base_seconds = getattr(agent, "run_budget_seconds", None)
    base_turns = getattr(agent, "max_iterations", None)
    if (isinstance(base_seconds, bool) or not isinstance(base_seconds, (int, float))
            or not math.isfinite(float(base_seconds)) or float(base_seconds) <= 0):
        raise WorkerBudgetError("profile has no valid wall-clock budget")
    if isinstance(base_turns, bool) or not isinstance(base_turns, int) or base_turns <= 0:
        raise WorkerBudgetError("profile has no valid iteration budget")
    effective_seconds = float(base_seconds)
    effective_turns = int(base_turns)
    if ledger is not None:
        if (wait is None or wait.get("status") not in _RESUMABLE_WAIT_STATES
                or str(ledger.get("latest_wait_run_id")) == binding.run_id
                or str(wait.get("run_id", "")) != str(ledger.get("latest_wait_run_id"))):
            raise WorkerBudgetError("resumed test wait is missing or has an invalid state")
        remaining_seconds = _positive_number(
            ledger.get("remaining_seconds"), field="remaining_seconds", allow_zero=True)
        remaining_turns = _nonnegative_int(
            ledger.get("remaining_turns"), field="remaining_turns")
        if remaining_seconds <= 0 or remaining_turns <= 0:
            raise WorkerBudgetError("test wait cumulative budget is exhausted")
        effective_seconds = min(effective_seconds, remaining_seconds)
        effective_turns = min(effective_turns, remaining_turns)
    if binding.attempt_limit is not None:
        effective_seconds = min(effective_seconds, binding.attempt_limit["max_seconds"])
        effective_turns = min(effective_turns, binding.attempt_limit["max_turns"])
    if effective_seconds <= 0 or effective_turns <= 0:
        raise WorkerBudgetError("worker cumulative budget is exhausted")
    from agent.iteration_budget import IterationBudget
    # Preserve the ordinary profile path exactly unless a test-wait budget or
    # one-shot grant actually requires an override.
    if ledger is not None or binding.attempt_limit is not None:
        agent.run_budget_seconds = effective_seconds
        agent.max_iterations = effective_turns
        agent.iteration_budget = IterationBudget(effective_turns)
    elif getattr(agent, "iteration_budget", None) is None:
        agent.iteration_budget = IterationBudget(effective_turns)
    agent._team_worker_budget_binding = binding
    agent._team_worker_budget_started_monotonic = time.monotonic()
    agent._team_worker_budget_started_at = time.time()
    agent._team_worker_budget_turns_used = 0
    agent._team_worker_budget_applied = True
    return True


def _safe_usage_path(binding: WorkerBinding) -> Path:
    base = binding.data_root / "worker-budget"
    if base.is_symlink():
        raise WorkerBudgetError("worker budget directory cannot be a symlink")
    base.mkdir(mode=0o700, exist_ok=True)
    card_dir = base / binding.card_id
    if card_dir.is_symlink():
        raise WorkerBudgetError("worker budget card directory cannot be a symlink")
    card_dir.mkdir(mode=0o700, exist_ok=True)
    return card_dir / f"{binding.run_id}.json"


def record_worker_budget_usage(
    agent: Any,
    *,
    api_call_count: int,
    exit_reason: str | None = None,
) -> dict[str, Any] | None:
    """Atomically write one redacted, idempotent per-run usage record."""
    if not getattr(agent, "_team_worker_budget_applied", False):
        return None
    binding = getattr(agent, "_team_worker_budget_binding", None)
    if not isinstance(binding, WorkerBinding):
        return None
    # Recheck the durable run-to-card relation at finalization. The current task
    # can already be blocked/done because its terminal Kanban tool ran first.
    verified = resolve_worker_binding(
        task_id=binding.card_id, run_id=binding.run_id, profile=binding.assignee,
        db_path=os.environ.get("HERMES_KANBAN_DB"), data_root=binding.data_root,
        home=Path.home(), require_active=False,
    )
    if verified.feature_id != binding.feature_id:
        raise WorkerBudgetError("team feature binding changed during the run")
    if isinstance(api_call_count, bool) or not isinstance(api_call_count, int) or api_call_count < 0:
        raise WorkerBudgetError("invalid API call count")
    iteration_budget = getattr(agent, "iteration_budget", None)
    iterations = getattr(iteration_budget, "used", None)
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 0:
        raise WorkerBudgetError("invalid iteration usage")
    budget_seconds = getattr(agent, "run_budget_seconds", None)
    max_turns = getattr(agent, "max_iterations", None)
    if (isinstance(budget_seconds, bool) or not isinstance(budget_seconds, (int, float))
            or not math.isfinite(float(budget_seconds)) or float(budget_seconds) <= 0):
        raise WorkerBudgetError("invalid effective worker time budget")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
        raise WorkerBudgetError("invalid effective worker turn budget")
    started_mono = getattr(agent, "_team_worker_budget_started_monotonic", None)
    started_at = getattr(agent, "_team_worker_budget_started_at", None)
    if not isinstance(started_mono, (int, float)) or not isinstance(started_at, (int, float)):
        raise WorkerBudgetError("worker budget start marker is missing")
    active_seconds = max(0.0, time.monotonic() - float(started_mono))
    ended_at = time.time()
    record = {
        "schema_version": SCHEMA_VERSION,
        "card_id": binding.card_id,
        "run_id": binding.run_id,
        "assignee": binding.assignee,
        "budget_seconds": round(float(budget_seconds), 3),
        "max_turns": max_turns,
        "active_seconds": round(active_seconds, 3),
        "iterations": iterations,
        "api_call_count": api_call_count,
        "started_at": round(float(started_at), 3),
        "ended_at": round(ended_at, 3),
        "finalized": True,
    }
    if isinstance(exit_reason, str):
        # Machine enum only; free-form finalizer text may include model output.
        safe_reason = re.sub(r"[^A-Za-z0-9_.:-]", "", exit_reason)[:80]
        if safe_reason:
            record["exit_reason"] = safe_reason
    target = _safe_usage_path(binding)
    if target.is_symlink():
        raise WorkerBudgetError("worker budget record cannot be a symlink")
    if target.exists():
        old = _read_json(target, max_bytes=16_000)
        if old and old.get("finalized") is True and old.get("card_id") == binding.card_id and old.get("run_id") == binding.run_id:
            return old
        raise WorkerBudgetError("refusing to replace an existing worker budget record")
    fd, temp_name = tempfile.mkstemp(prefix=".usage-", dir=target.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    return record


def read_usage(card_id: str, run_id: str, *, data_root: str | Path) -> dict[str, Any] | None:
    """Read a finalized, binding-checked usage record for controller charging."""
    if not _TASK_ID.fullmatch(card_id) or not _RUN_ID.fullmatch(run_id):
        return None
    root = Path(data_root)
    path = root / "worker-budget" / card_id / f"{run_id}.json"
    if path.is_symlink():
        return None
    value = _read_json(path, max_bytes=16_000)
    if not value or value.get("schema_version") != SCHEMA_VERSION:
        return None
    if value.get("card_id") != card_id or value.get("run_id") != run_id or value.get("finalized") is not True:
        return None
    if not isinstance(value.get("assignee"), str) or not _PROFILE.fullmatch(value["assignee"]):
        return None
    for key in ("active_seconds", "started_at", "ended_at", "budget_seconds"):
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or number < 0:
            return None
    if value.get("budget_seconds", 0) <= 0:
        return None
    max_turns = value.get("max_turns")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
        return None
    for key in ("iterations", "api_call_count"):
        number = value.get(key)
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            return None
    return value
