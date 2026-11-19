"""Validate untrusted planner output before it can become executable work."""
from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath

ROLES = {"backend", "frontend", "mobile", "go"}
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
PRIORITIES = {"P0": 30, "P1": 20, "P2": 10, "P3": 0}


class ContractError(ValueError):
    pass


def safe_id(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ContractError(f"Unsafe identifier: {value!r}")
    return value


def validate_owned_path(path: str) -> None:
    if not isinstance(path, str) or not path.strip():
        raise ContractError("owned_paths must contain nonempty relative paths")
    parts = PurePosixPath(path).parts
    if path.startswith(("/", "~", "-")) or ".." in parts or "\\" in path:
        raise ContractError(f"Unsafe owned path: {path}")
    if not parts or parts[0] in {".git", ".env"} or path in {"*", "**", ".", "./"}:
        raise ContractError(f"Overbroad or protected owned path: {path}")


def path_owned(path: str, patterns: list[str]) -> bool:
    if not isinstance(path, str) or not PurePosixPath(path).parts:
        return False
    if PurePosixPath(path).parts[0] == ".git" or path == ".env":
        return False
    return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/")
               or fnmatch.fnmatchcase(path, p) for p in patterns)


def validate_plan(plan: dict, *, project: str, feature_id: str,
                  base_sha: str, max_tasks: int = 12) -> list[dict]:
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ContractError("PLAN.json must use schema_version 1")
    for key, expected in (("project", project), ("feature_id", feature_id), ("base_sha", base_sha)):
        if plan.get(key) != expected:
            raise ContractError(f"PLAN.{key} must equal {expected!r}")
    if plan.get("status") not in {"ready", "approved"} or plan.get("open_questions"):
        raise ContractError("Plan is not ready or has unanswered blocking questions")
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= max_tasks:
        raise ContractError(f"Plan requires 1..{max_tasks} atomic tasks")
    by_id = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise ContractError('Every task must be an object')
        safe_id(task.get("id"))
        if task["id"] in by_id:
            raise ContractError("Duplicate task ID")
        by_id[task["id"]] = task
        for key in ("title", "problem", "solution", "priority_reason"):
            if not isinstance(task.get(key), str) or len(task[key].strip()) < 5:
                raise ContractError(f"{task['id']}: missing substantive {key}")
        for key in ("requirements", "acceptance_criteria", "test_plan", "owned_paths"):
            if not isinstance(task.get(key), list) or not task[key]:
                raise ContractError(f"{task['id']}: {key} must be a nonempty list")
        if task.get("assignee_role") not in ROLES:
            raise ContractError(f"{task['id']}: choose backend/frontend/mobile/go")
        if task.get("priority") not in PRIORITIES:
            raise ContractError(f"{task['id']}: unknown priority")
        for path in task["owned_paths"]:
            validate_owned_path(path)
        for ac in task["acceptance_criteria"]:
            if not isinstance(ac, dict) or not all(isinstance(ac.get(k), str) and ac[k].strip()
                                                 for k in ("id", "given", "when", "then", "evidence")):
                raise ContractError(f"{task['id']}: AC needs id/given/when/then/evidence")
        if not isinstance(task.get("dependencies"), list) or any(not isinstance(d, str) for d in task["dependencies"]):
            raise ContractError(f"{task['id']}: invalid dependencies")
    ordered, done = [], set()
    while len(ordered) < len(tasks):
        ready = [t for t in tasks if t["id"] not in done and set(t["dependencies"]) <= done]
        if not ready:
            raise ContractError("Dependency cycle or unknown dependency")
        ready.sort(key=lambda t: (-PRIORITIES[t["priority"]], t["id"]))
        task = ready[0]
        ordered.append(task)
        done.add(task["id"])
    coverage = plan.get("requirement_coverage")
    if not isinstance(coverage, list) or not coverage:
        raise ContractError("Missing requirements-to-tasks coverage matrix")
    for index, row in enumerate(coverage):
        if not isinstance(row, dict) or not isinstance(row.get('task_ids'), list):
            raise ContractError('Coverage must contain objects with task_ids lists')
        if not row.get("requirement_id") or not row.get("verification") or not row.get("task_ids"):
            raise ContractError(f"Incomplete requirements coverage row {index}: requirement "
                                f"{row.get('requirement_id')!r} needs nonempty task_ids and verification")
        if not set(row["task_ids"]) <= set(by_id):
            raise ContractError("Coverage refers to an unknown task")
    return ordered
