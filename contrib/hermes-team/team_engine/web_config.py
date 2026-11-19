"""Safe, revisioned configuration editing for the local engineering dashboard.

The web editor exposes a deliberately small projection of the runtime config.
Everything used internally by Hermes (role instructions, toolsets, credential
allowlists, installation paths, board identity, polling settings, and quota
probe metadata) remains owned by the runtime and is copied through unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping

from .controller import file_lock, read_json
from .install_profiles import install_profiles
from .project_context import validate_context_settings
from .execution_policy import ExecutionPolicyError, verification_plan


class ConfigEditorError(ValueError):
    """The requested editor payload is invalid or unsafe."""


class ConfigConflict(ConfigEditorError):
    """The config changed after the editor loaded it."""


PROVIDERS = [
    {"id": "kimi-coding", "label": "Kimi Coding"},
    {"id": "openai-codex", "label": "OpenAI Codex"},
    {"id": "anthropic", "label": "Anthropic Claude"},
]
EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
ROLE_CATALOG = {
    "analyst": ("Аналитик", "Уточняет проблему, пользователей, ограничения и критерии результата."),
    "planner": ("Планировщик / архитектор", "Проектирует решение и декомпозирует его на зависимые задачи."),
    "skeptic": ("Скептик", "Ищет пробелы, риски, неоднозначности и слабые предположения в плане."),
    "test-designer": ("Проектировщик тестов", "Проверяет покрытие требований и проектирует проверки."),
    "judge": ("Судья плана", "Сопоставляет независимые заключения и принимает решение по плану."),
    "backend": ("Backend-разработчик", "Реализует серверную логику, API и интеграции."),
    "frontend": ("Frontend-разработчик", "Реализует веб-интерфейсы и клиентскую логику."),
    "mobile": ("Mobile-разработчик", "Реализует мобильные приложения и их клиентскую архитектуру."),
    "go": ("Go-разработчик", "Реализует и проверяет сервисы и библиотеки на Go."),
    "reviewer": ("Ревьюер", "Проверяет изменения на дефекты, регрессии и несоответствие требованиям."),
    "qa": ("QA и регрессия", "Проводит тест-дизайн и проверяет регрессионные сценарии."),
    "integrator": ("Интегратор", "Собирает проверенные изменения в локальную ветку-кандидат."),
    "coordinator": ("Координатор", "Следит за ходом работ, артефактами, зависимостями и статусами."),
}
ROLE_IDS = tuple("team-" + role_id for role_id in ROLE_CATALOG)
ROLE_EDITABLE = {
    "provider", "model", "reasoning_effort", "run_budget", "max_turns", "pool", "candidates"
}
ROLE_REQUIRED = {"provider", "model", "reasoning_effort", "run_budget", "max_turns"}
POOL_FIELDS = {
    "provider", "enabled", "auth_ready", "max_concurrent", "reserve_percent",
    "max_snapshot_age", "unknown_policy",
}
POOL_REQUIRED = POOL_FIELDS
WORKFLOW_FIELDS = {
    "max_parallel_tasks": "max_parallel_tasks",
    "max_plan_tasks": "max_plan_tasks",
    "max_plan_corrections": "max_plan_corrections",
    "max_rework_rounds": "max_rework_rounds",
    "max_plan_report_retries": "max_plan_report_retries",
    "max_report_retries": "max_report_retries",
    "auto_start_after_judge_accept": "auto_start_after_judge_accept",
}
PROJECT_FIELDS = {"repo", "base_ref", "checks", "check_timeout", "context_file", "context_required", "execution_policy"}
PROJECT_REQUIRED_FIELDS = {"repo", "base_ref", "checks", "check_timeout"}
PROVIDER_IDS = {item["id"] for item in PROVIDERS}
_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _raw_and_revision(path: Path) -> tuple[bytes, dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        config = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigEditorError(f"Unable to read team configuration: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigEditorError("Team configuration must be a JSON object")
    return raw, config, hashlib.sha256(raw).hexdigest()


def _editor_projection(config: Mapping[str, Any]) -> dict[str, Any]:
    raw_roles = config.get("roles")
    if not isinstance(raw_roles, Mapping):
        raise ConfigEditorError("Runtime config has no roles object")
    roles: dict[str, dict[str, Any]] = {}
    for role_id in ROLE_IDS:
        value = raw_roles.get(role_id)
        if not isinstance(value, Mapping):
            raise ConfigEditorError(f"Runtime config is missing the managed role {role_id}")
        projected = {name: copy.deepcopy(value[name]) for name in ROLE_REQUIRED if name in value}
        for name in ("pool", "candidates"):
            if name in value:
                if name == "pool":
                    projected[name] = copy.deepcopy(value[name])
                elif isinstance(value[name], list):
                    # Existing configs can predate the strict editor schema;
                    # only project fields this UI is designed to edit.
                    candidate_fields = {"pool", "provider", "model", "reasoning_effort", "tier"}
                    projected[name] = [
                        {key: copy.deepcopy(candidate[key]) for key in candidate_fields if key in candidate}
                        for candidate in value[name] if isinstance(candidate, Mapping)
                    ]
                else:
                    projected[name] = copy.deepcopy(value[name])
        roles[role_id] = projected

    quota = config.get("quota")
    pools = quota.get("pools") if isinstance(quota, Mapping) else None
    if not isinstance(pools, Mapping) or not pools:
        raise ConfigEditorError("Runtime config has no quota pools")
    projected_pools: dict[str, dict[str, Any]] = {}
    for pool_id, value in pools.items():
        if not isinstance(pool_id, str) or not isinstance(value, Mapping):
            raise ConfigEditorError("Runtime quota pool has invalid shape")
        projected_pools[pool_id] = {name: copy.deepcopy(value.get(name)) for name in POOL_FIELDS}

    workflow = {name: config.get(source, defaults) for name, source, defaults in (
        ("max_parallel_tasks", "max_parallel_tasks", 1),
        ("max_plan_tasks", "max_plan_tasks", 12),
        ("max_plan_corrections", "max_plan_corrections", 2),
        ("max_rework_rounds", "max_rework_rounds", 2),
        ("max_plan_report_retries", "max_plan_report_retries", 1),
        ("max_report_retries", "max_report_retries", 1),
        ("auto_start_after_judge_accept", "auto_start_after_judge_accept", False),
    )}
    projects = config.get("projects", {})
    if not isinstance(projects, Mapping):
        raise ConfigEditorError("Runtime config projects must be an object")
    safe_projects = {}
    for project_id, project in projects.items():
        if not isinstance(project_id, str) or not isinstance(project, Mapping):
            raise ConfigEditorError("Runtime project entry has invalid shape")
        projected = {key: copy.deepcopy(project[key]) for key in PROJECT_FIELDS if key in project}
        # Context settings were added after project registration. Normalize
        # legacy entries in the editor while retaining internal fields in the
        # runtime config during save.
        projected.setdefault("context_file", ".agents/project.json")
        projected.setdefault("context_required", False)
        safe_projects[project_id] = projected
    return {"roles": roles, "quota": {"pools": projected_pools},
            "workflow": workflow, "projects": safe_projects}


def _catalog() -> dict[str, Any]:
    return {
        "providers": copy.deepcopy(PROVIDERS),
        "efforts": list(EFFORTS),
        "roles": {"team-" + key: {"label": value[0], "description": value[1]}
                  for key, value in ROLE_CATALOG.items()},
    }


def _envelope(config_path: Path, config: Mapping[str, Any], revision: str) -> dict[str, Any]:
    return {"revision": revision, "config": _editor_projection(config), "catalog": _catalog()}


def load_editor(config_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Return only user-editable settings and safe catalog metadata."""
    path = Path(config_path).expanduser().resolve()
    _, config, revision = _raw_and_revision(path)
    return _envelope(path, config, revision)


def _keys(value: Mapping[str, Any], allowed: set[str], required: set[str], where: str) -> None:
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise ConfigEditorError(f"Unsupported fields in {where}: {', '.join(sorted(map(str, unknown)))}")
    if missing:
        raise ConfigEditorError(f"Missing fields in {where}: {', '.join(sorted(missing))}")


def _safe_string(value: Any, where: str, *, nonempty: bool = True, max_length: int = 256) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()) or len(value) > max_length or _CONTROL_RE.search(value):
        raise ConfigEditorError(f"{where} must be a safe string")
    return value


def _provider(value: Any, where: str) -> str:
    provider = _safe_string(value, where, max_length=64).strip()
    if provider not in PROVIDER_IDS:
        raise ConfigEditorError(f"{where} is not a supported provider")
    return provider


def _model(value: Any, where: str) -> str:
    model = _safe_string(value, where, max_length=200).strip()
    if any(char.isspace() for char in model):
        raise ConfigEditorError(f"{where} must not contain whitespace")
    return model


def _integer(value: Any, where: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigEditorError(f"{where} must be an integer from {low} to {high}")
    return value


def _validate_payload_shape(payload: Any) -> tuple[str, Mapping[str, Any]]:
    if not isinstance(payload, Mapping) or set(payload) != {"revision", "config"}:
        raise ConfigEditorError("Request must contain exactly revision and config")
    revision = payload.get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{64}", revision):
        raise ConfigEditorError("revision must be a SHA-256 config revision")
    value = payload.get("config")
    if not isinstance(value, Mapping) or set(value) != {"roles", "quota", "workflow", "projects"}:
        raise ConfigEditorError("config must contain exactly roles, quota, workflow, and projects")
    return revision, value


def _validate_role_settings(value: Any, role_id: str, old_role: Mapping[str, Any],
                            pools: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigEditorError(f"Role {role_id} must be an object")
    _keys(value, ROLE_EDITABLE, ROLE_REQUIRED, f"role {role_id}")
    result = copy.deepcopy(dict(value))
    result["provider"] = _provider(result["provider"], f"role {role_id} provider")
    result["model"] = _model(result["model"], f"role {role_id} model")
    if not isinstance(result["reasoning_effort"], str) or result["reasoning_effort"] not in EFFORTS:
        raise ConfigEditorError(f"Role {role_id} has unsupported reasoning_effort")
    result["run_budget"] = _integer(result["run_budget"], f"role {role_id} run_budget", 1, 86400)
    result["max_turns"] = _integer(result["max_turns"], f"role {role_id} max_turns", 1, 500)
    if "pool" in result:
        pool_id = _safe_string(result["pool"], f"role {role_id} pool", max_length=64)
        if pool_id not in pools:
            raise ConfigEditorError(f"Role {role_id} refers to unknown pool {pool_id}")
        if pools[pool_id]["provider"] != result["provider"]:
            raise ConfigEditorError(f"Role {role_id} provider does not match pool {pool_id}")
    else:
        matching_pools = [pool_id for pool_id, pool_config in pools.items()
                          if pool_config["provider"] == result["provider"]]
        if len(matching_pools) != 1:
            raise ConfigEditorError(
                f"Role {role_id} needs an explicit pool unless exactly one pool matches its provider"
            )
    if "candidates" in result:
        candidates = result["candidates"]
        if not isinstance(candidates, list) or not candidates:
            raise ConfigEditorError(f"Role {role_id} candidates must be a non-empty list")
        clean_candidates = []
        for index, candidate in enumerate(candidates):
            where = f"role {role_id} candidate {index}"
            if not isinstance(candidate, Mapping):
                raise ConfigEditorError(f"{where} must be an object")
            allowed = {"pool", "provider", "model", "reasoning_effort", "tier"}
            required = {"provider", "model", "pool"}
            _keys(candidate, allowed, required, where)
            clean = copy.deepcopy(dict(candidate))
            clean["provider"] = _provider(clean["provider"], where + " provider")
            clean["model"] = _model(clean["model"], where + " model")
            effort = clean.get("reasoning_effort", result["reasoning_effort"])
            if not isinstance(effort, str) or effort not in EFFORTS:
                raise ConfigEditorError(f"{where} has unsupported reasoning_effort")
            if "reasoning_effort" in clean:
                clean["reasoning_effort"] = effort
            tier = clean.get("tier", 0)
            clean["tier"] = _integer(tier, where + " tier", 0, 100)
            if "pool" in clean:
                pool_id = _safe_string(clean["pool"], where + " pool", max_length=64)
                if pool_id not in pools:
                    raise ConfigEditorError(f"{where} refers to unknown pool {pool_id}")
                if pools[pool_id]["provider"] != clean["provider"]:
                    raise ConfigEditorError(f"{where} provider does not match pool {pool_id}")
            clean_candidates.append(clean)
        result["candidates"] = clean_candidates
    # Preserve any existing non-editable role fields verbatim.
    merged = copy.deepcopy(dict(old_role))
    for key in ROLE_EDITABLE:
        merged.pop(key, None)
    merged.update(result)
    kimi_provider = "kimi-coding"
    has_kimi_candidate = any(
        isinstance(candidate, Mapping) and candidate.get("provider") == kimi_provider
        for candidate in merged.get("candidates", [])
    )
    if merged.get("provider") != kimi_provider and not has_kimi_candidate:
        # Never carry a Kimi API key into a role configured exclusively for a
        # different provider. A deliberately mixed candidate list may retain
        # an existing allowlist, but this editor never expands it.
        merged["credential_env_vars"] = []
    return merged


def _validate_pools(value: Any, old_pools: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or not value:
        raise ConfigEditorError("quota.pools must be a non-empty object")
    result: dict[str, dict[str, Any]] = {}
    for pool_id, raw in value.items():
        if not isinstance(pool_id, str) or not _ID_RE.fullmatch(pool_id):
            raise ConfigEditorError("Quota pool names must be simple identifiers")
        if not isinstance(raw, Mapping):
            raise ConfigEditorError(f"Quota pool {pool_id} must be an object")
        _keys(raw, POOL_FIELDS, POOL_REQUIRED, f"quota pool {pool_id}")
        clean = copy.deepcopy(dict(raw))
        clean["provider"] = _provider(clean["provider"], f"quota pool {pool_id} provider")
        for field in ("enabled", "auth_ready"):
            if type(clean[field]) is not bool:
                raise ConfigEditorError(f"Quota pool {pool_id} {field} must be boolean")
        clean["max_concurrent"] = _integer(clean["max_concurrent"], f"quota pool {pool_id} max_concurrent", 1, 32)
        reserve = clean["reserve_percent"]
        if (isinstance(reserve, bool) or not isinstance(reserve, (int, float))
                or not math.isfinite(reserve) or not 0 <= reserve < 100):
            raise ConfigEditorError(f"Quota pool {pool_id} reserve_percent must be in [0, 100)")
        clean["reserve_percent"] = float(reserve)
        age = clean["max_snapshot_age"]
        if (isinstance(age, bool) or not isinstance(age, (int, float))
                or not math.isfinite(age) or not 0 <= age <= 86400):
            raise ConfigEditorError(f"Quota pool {pool_id} max_snapshot_age must be in 0..86400")
        clean["max_snapshot_age"] = float(age)
        if not isinstance(clean["unknown_policy"], str) or clean["unknown_policy"] not in {"wait", "allow_limited"}:
            raise ConfigEditorError(f"Quota pool {pool_id} unknown_policy is unsupported")
        # Probe selection and future private metadata belong to the runtime.
        prior = old_pools.get(pool_id, {})
        if isinstance(prior, Mapping):
            for key, item in prior.items():
                if key not in POOL_FIELDS:
                    clean[key] = copy.deepcopy(item)
        result[pool_id] = clean
    return result


def _validate_workflow(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - set(WORKFLOW_FIELDS) or (set(WORKFLOW_FIELDS) - {"max_parallel_tasks"}) - set(value):
        raise ConfigEditorError("workflow has an unexpected schema")
    value = dict(value)
    value.setdefault("max_parallel_tasks", 1)
    bounds = {
        "max_parallel_tasks": (1, 2),
        "max_plan_tasks": (1, 50),
        "max_plan_corrections": (0, 5),
        "max_rework_rounds": (0, 5),
        "max_plan_report_retries": (0, 5),
        "max_report_retries": (0, 5),
    }
    result: dict[str, Any] = {name: _integer(value[name], f"workflow {name}", *bounds[name]) for name in bounds}
    auto_start = value["auto_start_after_judge_accept"]
    if type(auto_start) is not bool:
        raise ConfigEditorError("workflow auto_start_after_judge_accept must be boolean")
    result["auto_start_after_judge_accept"] = auto_start
    return result


def _run_git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigEditorError("Project Git validation could not be completed") from exc
    if result.returncode:
        raise ConfigEditorError("Project Git validation failed")
    return result.stdout.strip()


def _validate_project(name: Any, raw: Any, old_projects: Mapping[str, Any],
                      hermes_root: Path) -> dict[str, Any]:
    if not isinstance(name, str) or not _ID_RE.fullmatch(name):
        raise ConfigEditorError("Project names must be simple identifiers")
    if not isinstance(raw, Mapping) or not PROJECT_REQUIRED_FIELDS.issubset(raw) or set(raw) - PROJECT_FIELDS:
        raise ConfigEditorError(f"Project {name} has an unexpected schema")
    repo_raw = _safe_string(raw["repo"], f"project {name} repo", max_length=1024)
    repo_input = Path(repo_raw)
    if not repo_input.is_absolute():
        raise ConfigEditorError(f"Project {name} repo must be an absolute path")
    try:
        repo = repo_input.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ConfigEditorError(f"Project {name} repository path does not exist or cannot be resolved") from exc
    if not repo.is_dir():
        raise ConfigEditorError(f"Project {name} repo must be a directory")
    existing = old_projects.get(name)
    if existing is not None:
        if not isinstance(existing, Mapping) or str(Path(existing.get("repo", "")).resolve()) != str(repo):
            raise ConfigEditorError(f"Project {name} repository path is immutable")
    else:
        # New registrations may only point into the media server's managed
        # workspace tree; existing configured repositories remain editable.
        workspace_root = (hermes_root.parent / "workspaces").resolve()
        try:
            repo.relative_to(workspace_root)
        except ValueError as exc:
            raise ConfigEditorError("New project repositories must be under the managed workspaces directory") from exc
    top = Path(_run_git(repo, "rev-parse", "--show-toplevel")).resolve()
    if top != repo:
        raise ConfigEditorError(f"Project {name} path must be the Git repository root")
    base_ref = _safe_string(raw["base_ref"], f"project {name} base_ref", max_length=200).strip()
    if any(char.isspace() for char in base_ref):
        raise ConfigEditorError(f"Project {name} base_ref must not contain whitespace")
    _run_git(repo, "rev-parse", "--verify", "--end-of-options", base_ref + "^{commit}")
    checks = raw["checks"]
    if not isinstance(checks, list) or not checks:
        raise ConfigEditorError(f"Project {name} checks must be a non-empty list of argv arrays")
    clean_checks = []
    for index, command in enumerate(checks):
        if not isinstance(command, list) or not command:
            raise ConfigEditorError(f"Project {name} check {index} must be a non-empty argv array")
        clean_checks.append([_safe_string(arg, f"project {name} check {index} argument", max_length=4096)
                             for arg in command])
    timeout = _integer(raw["check_timeout"], f"project {name} check_timeout", 1, 3600)
    try:
        context_settings = validate_context_settings(raw)
    except ValueError as exc:
        raise ConfigEditorError(f"Project {name} context settings are invalid: {exc}") from exc
    result = {"repo": str(repo), "base_ref": base_ref, "checks": clean_checks,
              "check_timeout": timeout, **context_settings}
    if "execution_policy" in raw:
        policy = raw["execution_policy"]
        if not isinstance(policy, Mapping):
            raise ConfigEditorError(f"Project {name} execution_policy must be an object")
        try:
            verification_plan(clean_checks, policy, None)
            # Unknown paths force full checks, so validate rule indexes explicitly.
            for rule in policy["path_rules"]:
                if any(index >= len(clean_checks) for index in rule["quick_check_indexes"]):
                    raise ExecutionPolicyError("Quick check index is outside the project checks")
        except (ExecutionPolicyError, KeyError, TypeError) as exc:
            raise ConfigEditorError(f"Project {name} execution policy is invalid: {exc}") from exc
        result["execution_policy"] = copy.deepcopy(dict(policy))
    return result


def _validate_config(editable: Mapping[str, Any], old: Mapping[str, Any]) -> dict[str, Any]:
    old_roles, old_quota, old_projects = old.get("roles"), old.get("quota"), old.get("projects", {})
    if not isinstance(old_roles, Mapping) or not isinstance(old_quota, Mapping) or not isinstance(old_projects, Mapping):
        raise ConfigEditorError("Runtime config has invalid internal shape")
    role_input = editable["roles"]
    if not isinstance(role_input, Mapping) or set(role_input) != set(ROLE_IDS):
        raise ConfigEditorError("All 13 managed roles must be present exactly once")
    quota_input = editable["quota"]
    if not isinstance(quota_input, Mapping) or set(quota_input) != {"pools"}:
        raise ConfigEditorError("quota must contain exactly pools")
    old_pools = old_quota.get("pools", {})
    if not isinstance(old_pools, Mapping):
        raise ConfigEditorError("Runtime quota pools have invalid shape")
    pools = _validate_pools(quota_input["pools"], old_pools)
    roles = {}
    for role_id in ROLE_IDS:
        profile = role_id
        roles[profile] = _validate_role_settings(role_input[role_id], role_id, old_roles[profile], pools)
    project_input = editable["projects"]
    if not isinstance(project_input, Mapping) or not set(old_projects).issubset(project_input):
        raise ConfigEditorError("Existing projects must remain present; add new projects without removing current ones")
    hermes_root = Path(old.get("hermes_root", "")).expanduser().resolve()
    projects = {}
    for name, value in project_input.items():
        clean_project = _validate_project(name, value, old_projects, hermes_root)
        previous = old_projects.get(name)
        if isinstance(previous, Mapping):
            for key, item in previous.items():
                if key not in PROJECT_FIELDS:
                    clean_project[key] = copy.deepcopy(item)
        projects[name] = clean_project
    workflow = _validate_workflow(editable["workflow"])
    result = copy.deepcopy(dict(old))
    result["roles"] = roles
    result.setdefault("quota", {})["pools"] = pools
    result.update(workflow)
    result["projects"] = projects
    # This controller deliberately supports a single active feature.
    if result.get("max_active_features", 1) != 1:
        raise ConfigEditorError("max_active_features is fixed at 1 in this runtime")
    return result


_PROFILE_FILES = ("config.yaml", "SOUL.md", ".env", "profile.yaml",
                  ".hermes-team-managed.json", "hermes-team-terminal.sh")


def _snapshot_profiles(config: Mapping[str, Any], role_names: set[str]) -> dict[Path, tuple[bytes, int] | None]:
    root = Path(config["hermes_root"]).resolve()
    profiles = (root / "profiles").resolve()
    snapshot: dict[Path, tuple[bytes, int] | None] = {}
    for role in role_names:
        directory = (profiles / role).resolve()
        if profiles not in directory.parents:
            raise ConfigEditorError("Managed profile path escaped profiles directory")
        for filename in _PROFILE_FILES:
            path = directory / filename
            if path.is_symlink():
                raise ConfigEditorError("Refusing to edit a symlink in a managed profile")
            if path.exists():
                if not path.is_file():
                    raise ConfigEditorError("Managed profile file has invalid type")
                snapshot[path] = (path.read_bytes(), path.stat().st_mode & 0o777)
            else:
                snapshot[path] = None
    return snapshot


def _restore_profiles(snapshot: Mapping[Path, tuple[bytes, int] | None]) -> None:
    errors = []
    for path, prior in snapshot.items():
        try:
            if prior is None:
                path.unlink(missing_ok=True)
            else:
                data, mode = prior
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".rollback.", dir=path.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temp_name, mode)
                    os.replace(temp_name, path)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
        except OSError as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError("Unable to fully restore managed profile files")


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _restore_config(path: Path, raw: bytes) -> None:
    """Restore the pre-transaction config, including mode 0600, atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".rollback.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_config_backup(path: Path, raw: bytes, revision: str) -> Path:
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = backup_dir / f"config-{stamp}-{revision[:12]}.json"
    if target.exists():
        target = backup_dir / f"config-{stamp}-{revision[:12]}-{os.getpid()}.json"
    fd, temp_name = tempfile.mkstemp(prefix=".config-backup.", dir=backup_dir)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return target


def _profile_config_changed(old: Mapping[str, Any], new: Mapping[str, Any]) -> set[str]:
    changed = set()
    for profile in old["roles"]:
        before, after = old["roles"][profile], new["roles"][profile]
        fields = ("provider", "model", "reasoning_effort", "max_turns", "run_budget",
                  "credential_env_vars")
        if any(before.get(field) != after.get(field) for field in fields):
            changed.add(profile)
    return changed


def save_editor(config_path: str | os.PathLike[str], payload: Any, *,
                before_apply: Callable[[Mapping[str, Any], Mapping[str, Any]], None] | None = None
                ) -> dict[str, Any]:
    """Validate and atomically save editor settings; refresh only changed profiles.

    ``before_apply`` runs under the config lock after complete validation and
    before any persistent changes. The dashboard uses it to reject profile
    updates while a worker for that profile is active.
    """
    path = Path(config_path).expanduser().resolve()
    expected_revision, editable = _validate_payload_shape(payload)
    with file_lock(str(path) + ".lock"):
        raw, old_config, current_revision = _raw_and_revision(path)
        if expected_revision != current_revision:
            raise ConfigConflict("Configuration changed since it was loaded; reload and apply your edits again")
        new_config = _validate_config(editable, old_config)
        if before_apply is not None:
            before_apply(copy.deepcopy(old_config), copy.deepcopy(new_config))
        if new_config == old_config:
            return {**_envelope(path, old_config, current_revision),
                    "message": "Изменений нет; конфигурация уже актуальна."}

        changed_profiles = _profile_config_changed(old_config, new_config)
        profile_snapshot = _snapshot_profiles(old_config, changed_profiles) if changed_profiles else {}
        try:
            if changed_profiles:
                bundle_root = Path(old_config["bundle_root"]).resolve()
                install_profiles(
                    new_config["hermes_root"],
                    {name: new_config["roles"][name] for name in sorted(changed_profiles)},
                    role_docs_dir=bundle_root / "roles",
                    common_instructions=bundle_root / "roles" / "COMMON.md",
                )
            _write_config_backup(path, raw, current_revision)
            _atomic_json_write(path, new_config)
        except BaseException:
            errors = []
            try:
                # _atomic_json_write may raise after os.replace (for example,
                # while fsyncing the directory), so restore both sides of the
                # transaction regardless of which step raised.
                _restore_config(path, raw)
            except BaseException as exc:
                errors.append(exc)
            try:
                _restore_profiles(profile_snapshot)
            except BaseException as exc:
                errors.append(exc)
            if errors:
                raise RuntimeError("Config transaction failed and rollback was incomplete") from errors[0]
            raise
        final_raw, final_config, final_revision = _raw_and_revision(path)
        return {**_envelope(path, final_config, final_revision),
                "message": "Настройки сохранены. Новые назначения задач будут использовать обновлённую конфигурацию."}
