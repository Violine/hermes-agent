"""Read-only, evidence-based workflow efficiency metrics.

The report deliberately distinguishes measured values from unavailable ones.
Hermes worker wall time is not model-active time or subscription usage. Code
size is measured from Git object ranges, not summed commits, so retries do not
double count the same lines.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Mapping


_TIME_KEYS_START = ("started_at", "start_time", "started", "claimed_at")
_TIME_KEYS_END = ("ended_at", "finished_at", "completed_at", "ended", "finished")
_CATEGORIES = ("production", "tests", "sql", "config", "docs", "other", "binary")


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        number = float(value)
        return number if number >= 0 else None
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def _first_time(record: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key in record:
            result = _timestamp(record.get(key))
            if result is not None:
                return result
    return None


def _union_seconds(intervals: list[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return round(sum(end - start for start, end in merged), 3)


def _walk_cards(value: Any, key: str = "") -> set[str]:
    cards: set[str] = set()
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            lowered = str(child_key).lower()
            if (lowered == "card" or lowered.endswith("_card")) and isinstance(child, str):
                if child.startswith("t_"):
                    cards.add(child)
            else:
                cards.update(_walk_cards(child, lowered))
    elif isinstance(value, list):
        for child in value:
            cards.update(_walk_cards(child, key))
    return cards


def _native_runs(state: Mapping[str, Any], board: Any, now: float) -> dict[str, Any]:
    run_rows: list[dict[str, Any]] = []
    missing_timestamps = 0
    role_seconds: dict[str, float] = defaultdict(float)
    intervals: list[tuple[float, float]] = []
    total = completed = active = 0
    cards = _walk_cards(state)
    # State keeps only current card references after retries. Union them with
    # the bounded archived-inclusive board scan to recover replaced attempts.
    tasks: dict[str, dict[str, Any]] = {}
    if callable(getattr(board, "get_task", None)):
        for card_id in sorted(cards):
            try:
                task = board.get_task(card_id)
            except Exception:
                task = None
            if isinstance(task, Mapping):
                tasks[card_id] = dict(task)
    board_scan_limit = 200
    archived_included: bool | None = None
    board_scan_truncated = False
    if callable(getattr(board, "list_tasks", None)):
        try:
            rows = board.list_tasks(limit=board_scan_limit, include_archived=True)
            archived_included = True
        except TypeError:
            try:
                rows = board.list_tasks(limit=board_scan_limit)
                archived_included = False
            except Exception:
                rows = []
        except Exception:
            rows = []
            archived_included = False
        board_scan_truncated = len(rows) >= board_scan_limit
        request_id = str(state.get("id", ""))
        for task in rows:
            if not isinstance(task, Mapping):
                continue
            assignee = str(task.get("assignee", task.get("profile", "")))
            title = str(task.get("title", ""))
            task_id = str(task.get("id", ""))
            if (request_id and title.startswith(f"[{request_id}]") and assignee.startswith("team-")
                    and assignee != "team-controller" and task_id):
                tasks.setdefault(task_id, dict(task))

    seen_runs: set[tuple[str, str]] = set()

    for card_id, task in tasks.items():
        profile = str(task.get("assignee", task.get("profile", "")))
        if not profile.startswith("team-") or profile == "team-controller":
            continue
        try:
            runs = board.get_runs(card_id)
        except Exception:
            runs = []
        if not isinstance(runs, list):
            continue
        for raw in runs:
            if not isinstance(raw, Mapping):
                continue
            run_id = raw.get("id", raw.get("run_id"))
            dedupe_id = (str(run_id) if run_id is not None else
                         json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str))
            run_key = (card_id, dedupe_id)
            if run_key in seen_runs:
                continue
            seen_runs.add(run_key)
            run_profile = str(raw.get("profile", raw.get("assignee", profile)))
            if run_profile == "team-controller" or not run_profile.startswith("team-"):
                continue
            total += 1
            end = _first_time(raw, _TIME_KEYS_END)
            start = _first_time(raw, _TIME_KEYS_START)
            status = str(raw.get("status", "")).lower()
            outcome = str(raw.get("outcome", "")).lower()
            is_active = end is None and status in {"running", "active", "claimed", "in_progress"}
            if is_active:
                active += 1
                end = now
            elif end is not None:
                completed += 1
            if start is None or end is None:
                missing_timestamps += 1
                continue
            seconds = max(0.0, end - start)
            role_seconds[run_profile] += seconds
            intervals.append((start, end))
            run_rows.append({
                "card_id": card_id,
                "run_id": raw.get("id", raw.get("run_id")),
                "role": run_profile,
                "status": status or None,
                "outcome": outcome or None,
                "started_at": start,
                "ended_at": end if not is_active else None,
                "wall_seconds": round(seconds, 3),
                "active": is_active,
            })
    return {
        "attempts": {"total": total, "completed": completed, "active": active,
                     "missing_timestamps": missing_timestamps},
        "wall_seconds_sum": round(sum(x["wall_seconds"] for x in run_rows), 3),
        "wall_seconds_union": _union_seconds(intervals),
        "by_role_wall_seconds_sum": {key: round(value, 3) for key, value in sorted(role_seconds.items())},
        "intervals": run_rows,
        "coverage": {
            "complete": not board_scan_truncated and archived_included is True,
            "truncated": board_scan_truncated,
            "state_reference_count": len(cards),
            "observed_card_count": len(tasks),
            "board_scan_limit": board_scan_limit if archived_included is not None else None,
            "archived_included": archived_included,
        },
        "model_active_seconds": None,
        "subscription_usage": None,
    }


def _explicit_waits(state: Mapping[str, Any], now: float,
                    events: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Aggregate only structured interval records; never parse free-text events."""
    intervals: dict[str, list[tuple[float, float]]] = defaultdict(list)
    names = {"owner_gate", "quota_wait", "dependency_wait", "test_environment_wait",
             "test_wait", "technical_stop"}
    unknown = 0
    rows = list(state.get("efficiency_intervals", [])) if isinstance(state.get("efficiency_intervals", []), list) else []
    # Historical controller events are free-form prose and cannot support
    # reliable durations. Only accept an explicit machine-readable interval.
    for event in events or []:
        if not isinstance(event, Mapping):
            continue
        interval = event.get("efficiency_interval")
        if isinstance(interval, Mapping):
            rows.append(interval)
    if not isinstance(rows, list):
        rows = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        kind = row.get("kind")
        start = _first_time(row, _TIME_KEYS_START)
        end = _first_time(row, _TIME_KEYS_END)
        if end is None and row.get("status") in {"open", "active", "waiting"}:
            end = now
        if kind not in names or start is None or end is None:
            unknown += 1
            continue
        intervals[str(kind)].append((start, max(start, end)))
    durations = {kind: _union_seconds(values) for kind, values in sorted(intervals.items())}
    return {"seconds_by_kind": durations,
            "classified_seconds": round(sum(durations.values()), 3),
            "unclassified_records": unknown,
            "unclassified_seconds": None}


def _category(path: str) -> str:
    normalized = path.replace("\\", "/").lower()
    parts = normalized.split("/")
    name = parts[-1] if parts else normalized
    if name.endswith((".sql",)) or "/migration/" in normalized or "/migrations/" in normalized:
        return "sql"
    if any(part in {"test", "tests", "integrationtest", "integrationtests", "androidtest"}
           for part in parts) or name.endswith(("test.kt", "tests.kt", "test.java", "tests.java", ".spec.ts", ".test.ts", ".spec.js", ".test.js")):
        return "tests"
    if any(part in {"docs", "doc", "documentation"} for part in parts) or name.endswith((".md", ".rst", ".adoc")):
        return "docs"
    if name in {"dockerfile", "makefile", "gradlew", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"} or name.endswith((".yml", ".yaml", ".toml", ".ini", ".properties", ".xml", ".conf", ".config")):
        return "config"
    if name.endswith((".kt", ".java", ".py", ".go", ".rs", ".ts", ".tsx", ".js", ".jsx", ".swift", ".rb", ".php", ".cs", ".cpp", ".h", ".sql")):
        return "production"
    return "other"


def _git_diff(repo: str | os.PathLike[str] | None, base: Any, head: Any) -> dict[str, Any]:
    unavailable = {"status": "unavailable", "reason": None, "base_sha": base,
                   "head_sha": head, "added": None, "deleted": None, "files": None,
                   "by_category": {category: {"added": None, "deleted": None, "files": None}
                                   for category in _CATEGORIES}}
    if not repo or not base or not head:
        unavailable["reason"] = "repository or revision missing"
        return unavailable
    try:
        proc = subprocess.run(["git", "-C", str(Path(repo).expanduser()), "diff", "--numstat",
                               "--no-renames", f"{base}..{head}"], capture_output=True,
                              text=True, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        unavailable["reason"] = type(exc).__name__
        return unavailable
    if proc.returncode:
        unavailable["reason"] = "git diff failed"
        return unavailable
    counts = {category: {"added": 0, "deleted": 0, "files": 0} for category in _CATEGORIES}
    for line in proc.stdout.splitlines():
        columns = line.split("\t", 2)
        if len(columns) != 3:
            continue
        added_raw, deleted_raw, path = columns
        if added_raw == "-" or deleted_raw == "-":
            category = "binary"
            added = deleted = 0
        else:
            try:
                added, deleted = int(added_raw), int(deleted_raw)
            except ValueError:
                continue
            category = _category(path)
        counts[category]["added"] += added
        counts[category]["deleted"] += deleted
        counts[category]["files"] += 1
    return {"status": "measured", "reason": None, "base_sha": str(base), "head_sha": str(head),
            "added": sum(x["added"] for x in counts.values()),
            "deleted": sum(x["deleted"] for x in counts.values()),
            "files": sum(x["files"] for x in counts.values()), "by_category": counts,
            "method": "git diff --numstat --no-renames base..head; commits are not summed"}


def _code_metrics(state: Mapping[str, Any], repo: str | os.PathLike[str] | None) -> dict[str, Any]:
    base = state.get("initial_base_sha")
    accepted_sha = state.get("accepted_regression_sha") or state.get("current_sha")
    accepted = _git_diff(repo, base, accepted_sha)
    accepted["meaning"] = "accepted only when SHA is confirmed by full regression"
    if (accepted.get("status") == "measured" and not state.get("accepted_regression_sha")
            and state.get("stage") not in {"done", "ready_for_owner", "accepted"}):
        accepted["status"] = "provisional"
        accepted["meaning"] = "integrated SHA so far; feature regression/owner acceptance may still be pending"
    provisional_sha = state.get("staging_sha") or state.get("provisional_sha")
    provisional = _git_diff(repo, base, provisional_sha) if provisional_sha else None
    if provisional is None:
        provisional = {"status": "unavailable", "reason": "no single staging SHA recorded",
                       "added": None, "deleted": None, "files": None, "by_category": None}
    item_rows = []
    for item in state.get("items", []) if isinstance(state.get("items"), list) else []:
        if not isinstance(item, Mapping):
            continue
        candidate = item.get("provisional_integration") or item.get("integration") or {}
        head = (candidate.get("head_sha") or candidate.get("integration_sha")
                or item.get("head_sha")) if isinstance(candidate, Mapping) else item.get("head_sha")
        item_rows.append({"task_id": (item.get("definition") or {}).get("id")
                                         if isinstance(item.get("definition"), Mapping) else None,
                          "status": item.get("status"),
                          "candidate": _git_diff(repo, item.get("base_sha") or base, head)})
    return {"accepted_or_integrated": accepted, "staging": provisional,
            "task_candidates": item_rows,
            "token_counts": None,
            "model_active_seconds": None}


def verification_progress(state: Mapping[str, Any]) -> dict[str, Any]:
    """Compact verification status for normal list polling; performs no Git work."""
    items = state.get("items", [])
    if not isinstance(items, list):
        items = []
    items = [item for item in items if isinstance(item, Mapping)]
    total_tasks = len(items)
    completed_tasks = sum(1 for item in items if item.get("status") == "done")
    all_tasks_done = total_tasks > 0 and completed_tasks == total_tasks
    implementation_active = any(item.get("status") in {
        "implementation", "verification", "integration_review"
    } for item in items)
    provisional = sum(1 for item in items
                      if isinstance(item, Mapping) and item.get("status") == "provisional")
    repair_active = any(isinstance(item, Mapping) and item.get("batch_repair")
                        and item.get("status") not in {"done", "cancelled"} for item in items)
    pending_ids = state.get("pending_regression_items", [])
    pending = bool(pending_ids) if isinstance(pending_ids, list) else False
    batch = state.get("regression_batch")
    batch_status = batch.get("status") if isinstance(batch, Mapping) else None
    staging_sha = state.get("staging_sha")
    accepted_sha = state.get("accepted_regression_sha")
    batch_matches_staging = (isinstance(batch, Mapping)
                             and (not staging_sha or batch.get("head_sha") == staging_sha))
    if batch_status == "running" and batch_matches_staging:
        regression_status = "running"
    elif batch_status == "fail" and batch_matches_staging:
        regression_status = "failed"
    elif (batch_status == "pass" and batch_matches_staging and not pending
          and all_tasks_done and state.get("stage") in {"awaiting_delivery", "done"}):
        regression_status = "passed"
    elif batch_status == "pass" and batch_matches_staging:
        # A previous cumulative batch may be green while later task changes are
        # still being implemented or reviewed. Keep that history distinct from
        # feature-level completion.
        regression_status = "in_progress"
    elif implementation_active:
        regression_status = "in_progress"
    elif pending or provisional:
        regression_status = "pending"
    elif batch_status in {"running", "fail"} and batch_matches_staging:
        regression_status = {"running": "running", "fail": "failed"}[batch_status]
    elif accepted_sha and accepted_sha == staging_sha:
        regression_status = ("passed" if all_tasks_done
                             and state.get("stage") in {"awaiting_delivery", "done"}
                             else "in_progress")
    else:
        regression_status = "unknown"
    activity_stage = ("batch_repair" if repair_active else
                      "batch_regression" if regression_status == "running" else None)
    return {
        "provisional_task_count": provisional,
        "pending_regression_task_count": len(pending_ids) if isinstance(pending_ids, list) else 0,
        "total_task_count": total_tasks,
        "completed_task_count": completed_tasks,
        "staging_sha": staging_sha,
        "accepted_regression_sha": accepted_sha,
        "regression_status": regression_status,
        "activity_stage": activity_stage,
        "batch_id": batch.get("id") if isinstance(batch, Mapping) else None,
        "batch_repair_active": repair_active,
        "check": batch.get("progress") if isinstance(batch, Mapping) and regression_status == "running" else None,
        "final_acceptance_ready": bool(
            state.get("stage") in {"awaiting_delivery", "done"}
            and regression_status == "passed"
            and accepted_sha
            and accepted_sha == staging_sha
        ),
    }


_OWNER_GATE_STAGES = {
    "awaiting_requirements", "awaiting_plan", "awaiting_delivery", "awaiting_owner_input",
}
_WAIT_KINDS = {"owner_gate", "quota_wait", "dependency_wait", "test_environment_wait",
               "test_wait", "technical_stop"}


def update_efficiency_intervals(state: dict[str, Any], *, now: int | float | str | None = None) -> None:
    """Open/close only directly observed wait intervals in durable feature state.

    Call this while holding the feature state lock and before `save`. The
    function intentionally does not inspect event prose or infer dependencies.
    A test-environment wait needs an explicit `environment_wait_active=True`
    marker (set only after trusted prerequisite evidence); otherwise a blocked
    feature is classified as a generic technical stop.
    """
    if not isinstance(state, dict):
        raise TypeError("state must be a mutable dict")
    timestamp = (_timestamp(now) if now is not None
                 else datetime.now(timezone.utc).timestamp())
    if timestamp is None:
        raise ValueError("now must be a valid timestamp")
    rows = state.setdefault("efficiency_intervals", [])
    if not isinstance(rows, list):
        # Preserve malformed historical data without allowing it to corrupt
        # newly observed intervals.
        rows = []
        state["efficiency_intervals"] = rows

    stage = state.get("stage")
    active: set[str] = set()
    if stage in _OWNER_GATE_STAGES:
        active.add("owner_gate")
    waiting = state.get("waiting_for_quota")
    if isinstance(waiting, (list, tuple, set)) and waiting:
        active.add("quota_wait")
    environment_wait = state.get("environment_wait_active") is True
    if environment_wait:
        active.add("test_environment_wait")
    waiting_for_tests = state.get("waiting_for_tests")
    if isinstance(waiting_for_tests, (list, tuple, set)) and waiting_for_tests:
        active.add("test_wait")
    if (stage == "blocked" and not environment_wait and "quota_wait" not in active
            and "test_wait" not in active):
        active.add("technical_stop")

    open_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        if (isinstance(row, dict) and row.get("kind") in _WAIT_KINDS
                and row.get("status") == "open" and row.get("ended_at") is None):
            open_rows[str(row["kind"])] = row
    for kind, row in open_rows.items():
        if kind not in active:
            row["ended_at"] = timestamp
            row["status"] = "closed"
    for kind in sorted(active):
        if kind not in open_rows:
            rows.append({"kind": kind, "started_at": timestamp,
                         "ended_at": None, "status": "open"})


def compute_efficiency(state: Mapping[str, Any], board: Any, *,
                       repo: str | os.PathLike[str] | None = None,
                       now: int | float | str | None = None,
                       events: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Build a JSON-safe snapshot without writing state or contacting providers.

    `board` is the read-only subset of HermesBoard (`get_task`, `get_runs`, and
    optionally `list_tasks`). Missing timestamps or evidence remain unknown.
    """
    if not isinstance(state, Mapping):
        raise TypeError("state must be a mapping")
    snapshot = _timestamp(now) if now is not None else datetime.now(timezone.utc).timestamp()
    created = _timestamp(state.get("created_at"))
    end = _timestamp(state.get("completed_at") or state.get("finished_at"))
    if end is None:
        end = snapshot
    elapsed = round(max(0.0, end - created), 3) if created is not None else None
    return {
        "schema_version": 1,
        "request_id": state.get("id"),
        "stage": state.get("stage"),
        "status": "measured" if created is not None else "partial",
        "elapsed_seconds": elapsed,
        "elapsed_reason": None if elapsed is not None else "created_at timestamp unavailable",
        "workers": _native_runs(state, board, snapshot),
        "waits": _explicit_waits(state, snapshot, events),
        "code": _code_metrics(state, repo),
        "limitations": [
            "Worker wall time includes commands, waiting, tool overhead, and model activity.",
            "Provider tokens, cost, and model-active time are unavailable unless explicitly recorded.",
            "Wait categories are reported only from structured timestamped efficiency_intervals.",
        ],
    }
