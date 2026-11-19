"""Prepare an auditable Hermes worker-environment hardening patch.

This utility edits a pinned Hermes source checkout only when explicitly run.
It is intentionally not imported by the runtime team controller. The patch
keeps normal host/network settings and profile dotenv keys while removing
ambient credential-looking variables from ``team-*`` worker subprocesses.

The patch is a hygiene measure for accidental credential inheritance, not a
security boundary: workers running under the same Unix account can still read
that account's files and processes.
"""

from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Mapping


PINNED_VERSION = "0.20.6"
_SOURCE_RELATIVE_PATH = Path("hermes_cli") / "kanban_db.py"
_VERSION_RELATIVE_PATH = Path("hermes_cli") / "__init__.py"
_VERSION_PATTERN = re.compile(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", re.M)
_SPAWN_DEF_ANCHOR = "def _default_spawn("
_SPAWN_PROFILE_ANCHOR = "    profile_arg = normalize_profile_name(task.assignee)\n"
_POPEN_ANCHOR = "        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above\n"
_HELPER_MARKER = "# BEGIN hermes-team worker env hardening (managed)"
_HELPER_END_MARKER = "# END hermes-team worker env hardening (managed)"
_CALL_MARKER = "        # hermes-team: filter ambient credentials for team workers\n"

# Match credential *names*, not values. Broad enough for common model/cloud
# credentials while preserving endpoint settings such as ANTHROPIC_BASE_URL.
_SECRET_NAME = re.compile(
    r"(?:^|_)(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|ID_TOKEN|TOKEN|SECRET|"
    r"PASSWORD|PASSWD|CREDENTIALS?|AUTHORIZATION|ACCESS_KEY|PRIVATE_KEY)(?:_|$)",
    re.IGNORECASE,
)
_KNOWN_SHORT_SECRET_NAMES = frozenset({"GH_TOKEN", "GITHUB_TOKEN", "HF_TOKEN", "HUGGINGFACE_HUB_TOKEN"})
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_credential_env_name(name: str) -> bool:
    """Return whether an environment variable name conventionally holds a secret."""
    return name.upper() in _KNOWN_SHORT_SECRET_NAMES or bool(_SECRET_NAME.search(name))


def dotenv_key_names(dotenv_path: str | os.PathLike[str]) -> set[str]:
    """Read only dotenv key names; never parse or return secret values."""
    path = Path(dotenv_path)
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return set()
    keys: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if _ENV_NAME.fullmatch(name):
            keys.add(name)
    return keys


def filter_worker_environment(
    env: Mapping[str, str], profile_name: str, allowed_profile_keys: set[str]
) -> dict[str, str]:
    """Filter inherited credentials for team profiles, retaining dotenv keys.

    The function copies values without logging them. Other Hermes profiles are
    returned unchanged. All ``HERMES_KANBAN_*`` values are retained because
    the worker needs its board/task/workspace context.
    """
    result = dict(env)
    if not profile_name.startswith("team-"):
        return result
    return {
        key: value
        for key, value in result.items()
        if key.startswith("HERMES_KANBAN_")
        or key in allowed_profile_keys
        or not is_credential_env_name(key)
    }


_INJECTED_HELPER = f'''{_HELPER_MARKER}
def _hermes_team_filter_worker_env(env, profile_env):
    """Drop ambient credential variables before launching team workers."""
    import re as _re
    from pathlib import Path as _Path

    try:
        text = (_Path(profile_env) / ".env").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        text = ""
    allowed = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" in line:
            key = line.split("=", 1)[0].strip()
            if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                allowed.add(key)

    secret_name = _re.compile(
        r"(?:^|_)(?:API_KEY|ACCESS_TOKEN|REFRESH_TOKEN|ID_TOKEN|TOKEN|SECRET|"
        r"PASSWORD|PASSWD|CREDENTIALS?|AUTHORIZATION|ACCESS_KEY|PRIVATE_KEY)(?:_|$)",
        _re.IGNORECASE,
    )
    short_names = {{"GH_TOKEN", "GITHUB_TOKEN", "HF_TOKEN", "HUGGINGFACE_HUB_TOKEN"}}
    return {{
        key: value for key, value in env.items()
        if key.startswith("HERMES_KANBAN_")
        or key in allowed
        or not (key.upper() in short_names or secret_name.search(key))
    }}
{_HELPER_END_MARKER}
'''


def _hermes_version(source_root: Path) -> str:
    version_file = source_root / _VERSION_RELATIVE_PATH
    try:
        text = version_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Hermes version file is missing: {version_file}") from exc
    match = _VERSION_PATTERN.search(text)
    if not match:
        raise ValueError(f"Could not determine Hermes version from {version_file}")
    return match.group(1)


def patched_source(source: str) -> str:
    """Return patched source or fail closed when source anchors are unfamiliar."""
    has_helper = _HELPER_MARKER in source and _HELPER_END_MARKER in source
    has_partial_helper = (_HELPER_MARKER in source) != (_HELPER_END_MARKER in source)
    has_call = _CALL_MARKER in source
    if has_helper or has_partial_helper or has_call:
        if has_helper and has_call and source.count(_HELPER_MARKER) == 1 and source.count(_CALL_MARKER) == 1:
            return source
        raise ValueError("Partial or duplicate Hermes team env patch detected; refusing to modify source")

    if source.count(_SPAWN_DEF_ANCHOR) != 1:
        raise ValueError("Unrecognized Hermes _default_spawn anchor; refusing to modify source")
    if source.count(_SPAWN_PROFILE_ANCHOR) != 1:
        raise ValueError("Unrecognized Hermes worker profile anchor; refusing to modify source")
    if source.count(_POPEN_ANCHOR) != 1:
        raise ValueError("Unrecognized Hermes Popen anchor; refusing to modify source")

    patched = source.replace(
        _SPAWN_DEF_ANCHOR,
        _INJECTED_HELPER + "\n\n" + _SPAWN_DEF_ANCHOR,
        1,
    )
    # Keep the credential filter directly next to the launch as defense in
    # depth against future code adding credentials between setup and Popen.
    call_site = (
        "        # hermes-team: filter ambient credentials for team workers\n"
        "        if profile_arg.startswith(\"team-\"):\n"
        "            env = _hermes_team_filter_worker_env(env, env.get(\"HERMES_HOME\", \"\"))\n"
    )
    patched = patched.replace(
        _POPEN_ANCHOR,
        call_site + _POPEN_ANCHOR,
        1,
    )
    try:
        ast.parse(patched)
    except SyntaxError as exc:
        raise ValueError(f"Patched source is not valid Python: {exc}") from exc
    return patched


def apply_patch(source_root: str | os.PathLike[str], *, expected_version: str = PINNED_VERSION) -> Path:
    """Patch an explicit pinned checkout, writing a one-time backup first."""
    root = Path(source_root).expanduser().resolve()
    actual_version = _hermes_version(root)
    if actual_version != expected_version:
        raise ValueError(f"Expected Hermes {expected_version}, found {actual_version}; refusing to patch")
    target = root / _SOURCE_RELATIVE_PATH
    original = target.read_text(encoding="utf-8")
    updated = patched_source(original)
    if updated == original:
        return target

    backup = target.with_suffix(target.suffix + ".hermes-team.bak")
    if not backup.exists():
        shutil.copy2(target, backup)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_name, target.stat().st_mode)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", help="explicit Hermes source checkout to patch")
    parser.add_argument("--expected-version", default=PINNED_VERSION)
    args = parser.parse_args(argv)
    try:
        path = apply_patch(args.source_root, expected_version=args.expected_version)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Patched Hermes worker environment filter: {path}")
    backup = path.with_suffix(path.suffix + ".hermes-team.bak")
    if backup.exists():
        print(f"Backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
