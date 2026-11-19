"""Explicit, fail-closed selection of quick and deferred verification commands.

A project may opt in with a policy mapping repository path prefixes to command
indexes. The legacy behavior is preserved when no policy is configured: all
trusted commands are selected. Unknown, malformed, or uncovered changed paths
also select the complete command set.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import posixpath
from typing import Any, Mapping, Sequence


POLICY_VERSION = 1


class ExecutionPolicyError(ValueError):
    """The trusted project verification policy is malformed."""


@dataclass(frozen=True)
class Check:
    index: int
    name: str
    argv: tuple[str, ...]
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "name": self.name,
                "argv": list(self.argv), "sha256": self.sha256}


def _check_records(checks: Sequence[Any]) -> list[Check]:
    if not isinstance(checks, (list, tuple)) or not checks:
        raise ExecutionPolicyError("At least one trusted check is required")
    records: list[Check] = []
    names: set[str] = set()
    for index, raw in enumerate(checks):
        if isinstance(raw, Mapping):
            name, argv = raw.get("name"), raw.get("argv")
            if not isinstance(name, str) or not name.strip():
                raise ExecutionPolicyError(f"Check {index} needs a non-empty name")
        else:
            name, argv = f"check-{index + 1}", raw
        if not isinstance(argv, (list, tuple)) or not argv or any(
                not isinstance(arg, str) or not arg for arg in argv):
            raise ExecutionPolicyError(f"Check {name!r} needs a non-empty argv list")
        if name in names:
            raise ExecutionPolicyError(f"Duplicate check name: {name}")
        names.add(name)
        canonical = json.dumps(list(argv), ensure_ascii=False, separators=(",", ":"))
        records.append(Check(index=index, name=name, argv=tuple(argv),
                             sha256=hashlib.sha256(canonical.encode()).hexdigest()))
    return records


def _valid_repo_path(path: Any) -> str | None:
    if not isinstance(path, str) or not path or "\\" in path or path.startswith("/"):
        return None
    normalized = posixpath.normpath(path)
    if normalized in {".", ".."} or normalized.startswith("../"):
        return None
    return normalized


def _parse_policy(policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(policy, Mapping) or set(policy) - {"version", "path_rules", "full_after_tasks"}:
        raise ExecutionPolicyError("Policy accepts only version, path_rules and full_after_tasks")
    if policy.get("version") != POLICY_VERSION:
        raise ExecutionPolicyError("Unsupported execution policy version")
    batch = policy.get("full_after_tasks", 2)
    if type(batch) is not int or batch < 1 or batch > 20:
        raise ExecutionPolicyError("full_after_tasks must be an integer from 1 to 20")
    rules = policy.get("path_rules")
    if not isinstance(rules, list) or not rules:
        raise ExecutionPolicyError("Policy requires at least one path rule")
    result = []
    seen_names: set[str] = set()
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping) or set(rule) != {"name", "prefixes", "quick_check_indexes"}:
            raise ExecutionPolicyError(f"Path rule {index} has invalid fields")
        name = rule["name"]
        prefixes = rule["prefixes"]
        indexes = rule["quick_check_indexes"]
        if not isinstance(name, str) or not name.strip() or name in seen_names:
            raise ExecutionPolicyError(f"Path rule {index} needs a unique name")
        seen_names.add(name)
        if not isinstance(prefixes, list) or not prefixes:
            raise ExecutionPolicyError(f"Path rule {name!r} needs path prefixes")
        clean_prefixes = []
        for prefix in prefixes:
            clean = _valid_repo_path(prefix)
            if clean is None:
                raise ExecutionPolicyError(f"Invalid path prefix in rule {name!r}")
            clean_prefixes.append(clean.rstrip("/"))
        if (not isinstance(indexes, list) or not indexes
                or any(type(item) is not int or item < 0 for item in indexes)
                or len(set(indexes)) != len(indexes)):
            raise ExecutionPolicyError(f"Rule {name!r} needs unique non-negative check indexes")
        result.append({"name": name, "prefixes": clean_prefixes,
                       "quick_check_indexes": list(indexes)})
    return result


def verification_plan(
    checks: Sequence[Any],
    policy: Mapping[str, Any] | None,
    changed_paths: Sequence[str] | None,
    *,
    force_full: bool = False,
) -> dict[str, Any]:
    """Return selected fast checks and deferred full checks with a bound snapshot.

    Policy indexes address the trusted check list exactly as configured. A path
    must match at least one declared rule; otherwise the full set is required.
    ``changed_paths=None`` and an empty path list are treated as unknown.
    """
    records = _check_records(checks)
    all_indexes = set(range(len(records)))
    reasons: list[str] = []
    matched_rules: set[str] = set()
    normalized_paths: list[str] | None = None
    selected_indexes = all_indexes
    mode = "full"

    if policy is None:
        reasons.append("no_execution_policy")
    else:
        rules = _parse_policy(policy)
        if changed_paths is None or isinstance(changed_paths, (str, bytes)) or not changed_paths:
            reasons.append("changed_paths_unknown")
        else:
            normalized_paths = []
            uncovered: list[str] = []
            selected: set[int] = set()
            for raw_path in changed_paths:
                path = _valid_repo_path(raw_path)
                if path is None:
                    uncovered.append(str(raw_path))
                    continue
                normalized_paths.append(path)
                path_matches = []
                for rule in rules:
                    for prefix in rule["prefixes"]:
                        if path == prefix or path.startswith(prefix + "/"):
                            path_matches.append(rule)
                            break
                if not path_matches:
                    uncovered.append(path)
                for rule in path_matches:
                    matched_rules.add(rule["name"])
                    selected.update(rule["quick_check_indexes"])
            if uncovered:
                reasons.append("uncovered_changed_paths")
            elif not selected:
                reasons.append("no_quick_checks_selected")
            elif any(index not in all_indexes for index in selected):
                reasons.append("policy_check_index_out_of_range")
            else:
                selected_indexes = selected
                mode = "quick"
                reasons.append("all_changed_paths_covered")
    if force_full:
        mode = "full"
        selected_indexes = all_indexes
        reasons.append("full_run_required")

    selected_records = [records[index] for index in sorted(selected_indexes)]
    deferred_records = [record for record in records if record.index not in selected_indexes]
    normalized_policy = None if policy is None else dict(policy)
    snapshot_core = {
        "version": POLICY_VERSION,
        "mode": mode,
        "policy": normalized_policy,
        "changed_paths": normalized_paths,
        "matched_rules": sorted(matched_rules),
        "selected_checks": [record.as_dict() for record in selected_records],
        "deferred_checks": [record.as_dict() for record in deferred_records],
        "reasons": reasons,
    }
    policy_hash = hashlib.sha256(json.dumps(snapshot_core, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {**snapshot_core, "policy_sha256": policy_hash}


def selected_argv(plan: Mapping[str, Any]) -> list[list[str]]:
    """Convert a verified plan to the argv shape expected by existing gates."""
    if not isinstance(plan, Mapping) or plan.get("mode") not in {"quick", "full"}:
        raise ExecutionPolicyError("Invalid verification plan")
    selected = plan.get("selected_checks")
    if not isinstance(selected, list) or not selected:
        raise ExecutionPolicyError("A verification plan must select at least one check")
    return [list(check["argv"]) for check in selected]
