"""Install isolated Hermes profiles for the coding-team roles.

The installer creates only profile-local config, role instructions, and a
0600 environment file. It never clones default profile data, memories,
sessions, skills, channels, or gateways. Hermes' OAuth auth layer can use its
documented global-root credential fallback; optional API-key env vars can be
copied from the shared root ``.env`` by explicit allowlist only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import tempfile
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_PROFILE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_DEFAULT_TOOLSETS = ("file", "terminal", "kanban")


class ProfileInstallError(RuntimeError):
    """Raised when a profile cannot be installed without overwriting data."""


def _read_allowlisted_env(path: Path, names: set[str]) -> dict[str, str]:
    """Read only explicitly approved variable names; never log their values."""
    if not names or not path.exists():
        return {}
    found: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in names:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ProfileInstallError(f"Invalid newline/NUL in credential variable {key}")
        found[key] = value
    return found


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _yaml_compatible_json(value: Mapping[str, Any]) -> str:
    """JSON is a strict YAML subset and avoids an installer-time dependency."""
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _team_terminal_init(hermes_root: Path, timezone: Optional[str] = None) -> str:
    """Restore managed tool directories after Hermes' login shell resets PATH."""
    node_bin = shlex.quote(str(hermes_root / "node" / "bin"))
    team_bin = shlex.quote(str(hermes_root / "team" / "runtime" / "scripts"))
    java_home = shlex.quote(str(hermes_root / "toolchains" / "jdk-21"))
    python_bin = shlex.quote(str(hermes_root / "hermes-agent" / "venv" / "bin"))
    timezone_env = (
        f"export TZ={shlex.quote(timezone)}\nexport HERMES_TIMEZONE=\"$TZ\"\n"
        if timezone else ""
    )
    return (
        "# Hermes team managed tool paths; installed by hermes-team.\n"
        f"{timezone_env}"
        '# JVM/native tools need a UTF-8 locale for Cyrillic paths and test names.\n'
        'case "${LC_ALL:-${LC_CTYPE:-${LANG:-C}}}" in C|POSIX) '
        'LANG=C.UTF-8; LC_ALL=C.UTF-8; export LANG LC_ALL ;; esac\n'
        '_hermes_team_local_bin="${HOME}/.local/bin"\n'
        f"_hermes_team_node_bin={node_bin}\n"
        f"_hermes_team_bin={team_bin}\n"
        f"_hermes_team_java_home={java_home}\n"
        f"_hermes_team_python_bin={python_bin}\n"
        'case ":${PATH:-}:" in *":${_hermes_team_local_bin}:"*) ;; '
        '*) PATH="${_hermes_team_local_bin}:${PATH:-}" ;; esac\n'
        'case ":${PATH:-}:" in *":${_hermes_team_node_bin}:"*) ;; '
        '*) PATH="${_hermes_team_node_bin}:${PATH:-}" ;; esac\n'
        'case ":${PATH:-}:" in *":${_hermes_team_bin}:"*) ;; '
        '*) PATH="${_hermes_team_bin}:${PATH:-}" ;; esac\n'
        'case ":${PATH:-}:" in *":${_hermes_team_python_bin}:"*) ;; '
        '*) PATH="${_hermes_team_python_bin}:${PATH:-}" ;; esac\n'
        'if [ -x "${_hermes_team_java_home}/bin/java" ]; then\n'
        '  JAVA_HOME="${_hermes_team_java_home}"\n'
        '  case ":${PATH:-}:" in *":${JAVA_HOME}/bin:"*) ;; '
        '*) PATH="${JAVA_HOME}/bin:${PATH:-}" ;; esac\n'
        '  export JAVA_HOME\n'
        'fi\n'
        "export PATH\n"
        "unset _hermes_team_local_bin _hermes_team_node_bin _hermes_team_bin _hermes_team_java_home _hermes_team_python_bin\n"
    )


def install_profiles(
    hermes_root: str | os.PathLike[str],
    roles: Mapping[str, Mapping[str, Any]],
    *,
    role_docs_dir: str | os.PathLike[str],
    common_instructions: Optional[str | os.PathLike[str]] = None,
    credential_source_env: Optional[str | os.PathLike[str]] = None,
) -> list[dict[str, Any]]:
    """Create or refresh installer-owned role profiles.

    Each ``roles`` value accepts ``provider``, ``model``,
    ``reasoning_effort``, ``max_turns``, ``run_budget`` (seconds), ``toolsets``,
    ``description``, optional IANA ``timezone``, ``role_doc`` (one filename) or ``role_docs`` (ordered
    filenames under ``role_docs_dir``), and an optional explicit
    ``credential_env_vars`` allowlist. Role names are
    profile names, e.g. ``team-backend`` or ``team-analyst``.

    Existing paths are updated only when they contain this installer's
    ``.hermes-team-managed.json`` marker. An unmanaged profile is never
    overwritten. Running Hermes gateways is outside this function's scope.
    """
    root = Path(hermes_root).expanduser().resolve()
    if not root.is_absolute():
        raise ValueError("hermes_root must be absolute")
    profiles_root = root / "profiles"
    docs_root = Path(role_docs_dir).expanduser().resolve()
    common_path = Path(common_instructions).expanduser().resolve() if common_instructions else None
    credential_path = (
        Path(credential_source_env).expanduser().resolve()
        if credential_source_env else root / ".env"
    )
    common_text = common_path.read_text(encoding="utf-8") if common_path else ""
    installed: list[dict[str, Any]] = []

    for profile_name, raw_config in roles.items():
        name = str(profile_name).strip().lower()
        if not _PROFILE_RE.fullmatch(name) or name in {"default", "hermes", "root"}:
            raise ValueError(f"Invalid Hermes profile name: {profile_name!r}")
        if not isinstance(raw_config, Mapping):
            raise TypeError(f"Role config for {name} must be a mapping")
        role = dict(raw_config)
        timezone = role.get("timezone")
        if timezone is not None:
            if not isinstance(timezone, str) or not timezone.strip():
                raise ValueError(f"Role {name} timezone must be an IANA timezone name")
            timezone = timezone.strip()
            try:
                ZoneInfo(timezone)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ValueError(f"Role {name} has unsupported timezone {timezone!r}") from exc
        provider = str(role.get("provider") or "").strip()
        model = str(role.get("model") or "").strip()
        effort = str(role.get("reasoning_effort") or "medium").strip().lower()
        if not provider or not model:
            raise ValueError(f"Role {name} must configure provider and model")
        if effort not in _EFFORTS:
            raise ValueError(f"Role {name} has unsupported reasoning_effort {effort!r}")
        max_turns = int(role.get("max_turns", 40))
        run_budget = int(role.get("run_budget", 600))
        if max_turns < 1 or run_budget < 1:
            raise ValueError(f"Role {name} max_turns and run_budget must be positive")
        toolsets = role.get("toolsets", _DEFAULT_TOOLSETS)
        if not isinstance(toolsets, (list, tuple)) or not toolsets:
            raise ValueError(f"Role {name} toolsets must be a non-empty list")
        toolsets = list(dict.fromkeys(str(item).strip() for item in toolsets if str(item).strip()))
        if "terminal" not in toolsets or "file" not in toolsets:
            raise ValueError(f"Role {name} needs both file and terminal toolsets")
        if "credential_env_vars" in role:
            env_names = set(role["credential_env_vars"])
        else:
            env_names = set()
        if any(not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key) for key in env_names):
            raise ValueError(f"Role {name} has an invalid credential_env_vars allowlist")

        doc_value = role.get("role_docs")
        if doc_value is None:
            doc_names = [str(role.get("role_doc") or f"{name}.md")]
        elif isinstance(doc_value, (list, tuple)) and doc_value:
            doc_names = [str(item) for item in doc_value]
        else:
            raise ValueError(f"Role {name} role_docs must be a non-empty list")
        role_parts: list[str] = []
        for doc_name in doc_names:
            role_doc = (docs_root / doc_name).resolve()
            if docs_root not in role_doc.parents or not role_doc.is_file():
                raise ProfileInstallError(
                    f"Role instructions are missing or outside docs dir: {doc_name}"
                )
            role_parts.append(role_doc.read_text(encoding="utf-8").strip())
        role_text = "\n\n".join(part for part in role_parts if part)
        soul_text = "\n\n".join(part for part in (common_text.strip(), role_text) if part) + "\n"

        profile_dir = profiles_root / name
        marker = profile_dir / ".hermes-team-managed.json"
        if profile_dir.exists() and not marker.is_file():
            raise ProfileInstallError(
                f"Refusing to overwrite unmanaged Hermes profile: {profile_dir}"
            )
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "skills").mkdir(exist_ok=True)
        (profile_dir / "auth").mkdir(exist_ok=True)
        (profile_dir / "memories").mkdir(exist_ok=True)

        credentials = _read_allowlisted_env(credential_path, env_names)
        missing = env_names - set(credentials)
        if missing:
            raise ProfileInstallError(
                f"Role {name} requested credential variables absent from source: "
                + ", ".join(sorted(missing))
            )
        env_content = "# Explicitly allowlisted model credentials for this role.\n"
        env_content += "".join(f"{key}={value}\n" for key, value in sorted(credentials.items()))

        terminal_init = profile_dir / "hermes-team-terminal.sh"
        config = {
            "model": {"provider": provider, "default": model},
            "agent": {
                "reasoning_effort": effort,
                "max_turns": max_turns,
                "run_budget_seconds": run_budget,
            },
            "toolsets": toolsets,
            "terminal": {"backend": "local", "cwd": ".",
                         "shell_init_files": [str(terminal_init)]},
            # Messaging platform toolsets and gateway bot credentials are not
            # installed. The controller starts task workers via the Kanban
            # dispatcher, not by launching per-role gateways.
            "platform_toolsets": {},
        }
        metadata = {
            "managed_by": "hermes-team",
            "schema_version": 1,
            "profile": name,
            "role_doc": doc_names[0],
            "role_docs": doc_names,
            "provider": provider,
            "model": model,
            "reasoning_effort": effort,
            "max_turns": max_turns,
            "run_budget_seconds": run_budget,
            "toolsets": toolsets,
        }
        if timezone is not None:
            config["timezone"] = timezone
            metadata["timezone"] = timezone
        description = str(role.get("description") or f"Hermes coding team role: {name}")
        profile_meta = {"description": description, "description_auto": False}

        _atomic_write(profile_dir / "config.yaml", _yaml_compatible_json(config))
        _atomic_write(profile_dir / "SOUL.md", soul_text)
        _atomic_write(profile_dir / ".env", env_content)
        _atomic_write(profile_dir / "profile.yaml", _yaml_compatible_json(profile_meta))
        _atomic_write(marker, _yaml_compatible_json(metadata))
        _atomic_write(terminal_init, _team_terminal_init(root, timezone))
        installed.append({**metadata, "path": str(profile_dir)})

    return installed


def default_role_configurations(
    provider: str,
    model: str,
    *,
    analysis_effort: str = "high",
    implementation_effort: str = "low",
    review_effort: str = "high",
    qa_effort: str = "medium",
) -> dict[str, dict[str, Any]]:
    """Return editable role presets; callers supply provider/model per role as needed."""
    roles: dict[str, dict[str, Any]] = {}
    role_docs = {
        "team-analyst": (["analyst.md"], analysis_effort, 40, 600),
        "team-planner": (["planner-architect.md"], analysis_effort, 40, 600),
        "team-skeptic": (["skeptic.md"], review_effort, 40, 600),
        "team-test-designer": (["test-designer.md"], review_effort, 40, 600),
        "team-judge": (["judge.md"], review_effort, 40, 600),
        "team-backend": (["developer.md", "backend.md"], implementation_effort, 40, 600),
        "team-frontend": (["developer.md", "frontend.md"], implementation_effort, 40, 600),
        "team-mobile": (["developer.md", "mobile.md"], implementation_effort, 40, 600),
        "team-go": (["developer.md", "backend.md", "go.md"], implementation_effort, 40, 600),
        "team-reviewer": (["reviewer.md"], review_effort, 40, 600),
        "team-qa": (["qa-regression.md"], qa_effort, 40, 600),
        "team-integrator": (["integrator.md"], review_effort, 40, 600),
        "team-coordinator": (["coordinator.md"], analysis_effort, 40, 600),
    }
    normalized_provider = provider.strip().lower()
    kimi_credential_allowlist = (
        ["KIMI_API_KEY"]
        if normalized_provider in {"kimi", "kimi-coding", "moonshot"}
        else []
    )
    for profile, (docs, effort, turns, budget) in role_docs.items():
        roles[profile] = {
            "provider": provider,
            "model": model,
            "reasoning_effort": effort,
            "max_turns": turns,
            "run_budget": budget,
            "toolsets": ["file", "terminal", "kanban"],
            "role_docs": docs,
            "description": f"Hermes coding team: {profile.removeprefix('team-')} role",
        }
        if kimi_credential_allowlist:
            roles[profile]["credential_env_vars"] = list(kimi_credential_allowlist)
    return roles
