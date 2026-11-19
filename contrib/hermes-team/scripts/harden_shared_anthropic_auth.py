"""Patch Hermes 0.20.6 to share managed team Anthropic OAuth pool state.

The patch is deliberately opt-in and edits only an explicitly supplied Hermes
source checkout. It centralizes the Anthropic credential-pool row for marked
``team-*`` profiles while leaving every other provider and unmarked profile on
Hermes' normal per-profile auth store.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile


PINNED_VERSION = "0.20.6"
AUTH_PATH = Path("hermes_cli") / "auth.py"
POOL_PATH = Path("agent") / "credential_pool.py"
VERSION_PATH = Path("hermes_cli") / "__init__.py"
VERSION_PATTERN = re.compile(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", re.M)
HELPER_MARKER = "# BEGIN hermes-team shared Anthropic pool (managed)"
HELPER_END_MARKER = "# END hermes-team shared Anthropic pool (managed)"
IMPORT_MARKER = "# hermes-team shared Anthropic pool path"
PINNED_READ_POOL_SHA256 = "e78aafe8b13d607295fee70dc65ffd0499f5b7d9f052040dbe22a2e04fd44932"


_HELPER = f'''{HELPER_MARKER}
def _credential_pool_auth_file_path(provider_id):
    """Choose the auth store for credential-pool reads, writes, and locks.

    Only a directly-contained, explicitly marked team profile shares its
    Anthropic pool. All other providers and profiles retain native behavior.
    """
    active_path = _auth_file_path()
    if provider_id != "anthropic":
        return active_path
    try:
        from hermes_constants import get_default_hermes_root

        root = get_default_hermes_root().resolve(strict=False)
        profile_home = get_hermes_home().resolve(strict=False)
        expected_parent = (root / "profiles").resolve(strict=False)
        if profile_home.parent != expected_parent or not profile_home.name.startswith("team-"):
            return active_path
        marker = profile_home / ".hermes-team-managed.json"
        if marker.is_symlink() or not marker.is_file():
            return active_path
        return root / "auth.json"
    except Exception:
        # Fail closed: an unexpected profile layout keeps native local storage.
        return active_path
{HELPER_END_MARKER}
'''


_READ_FUNCTION = '''def read_credential_pool(provider_id: Optional[str] = None) -> Dict[str, Any]:
    """Return persisted credential pools, sharing Anthropic only for managed teams.

    A managed team profile reads the canonical root Anthropic row even when a
    stale profile-local row exists. Other providers and unmarked profiles use
    Hermes' original profile-first/global-fallback behavior.
    """
    shared_anthropic = (
        provider_id == "anthropic"
        and _credential_pool_auth_file_path("anthropic") != _auth_file_path()
    )
    if shared_anthropic:
        store = _load_auth_store(_credential_pool_auth_file_path("anthropic"))
        pool = store.get("credential_pool")
        entries = pool.get("anthropic") if isinstance(pool, dict) else None
        return list(entries) if isinstance(entries, list) else []

    auth_store = _load_auth_store()
    pool = auth_store.get("credential_pool")
    if not isinstance(pool, dict):
        pool = {}

    global_pool: Dict[str, Any] = {}
    global_store = _load_global_auth_store()
    maybe_global_pool = global_store.get("credential_pool") if global_store else None
    if isinstance(maybe_global_pool, dict):
        global_pool = maybe_global_pool

    if provider_id is None:
        merged = dict(pool)
        for gp_key, gp_entries in global_pool.items():
            if not isinstance(gp_entries, list) or not gp_entries:
                continue
            existing = merged.get(gp_key)
            if isinstance(existing, list) and existing:
                continue
            merged[gp_key] = list(gp_entries)
        if _credential_pool_auth_file_path("anthropic") != _auth_file_path():
            canonical = _load_auth_store(_credential_pool_auth_file_path("anthropic"))
            canonical_pool = canonical.get("credential_pool")
            canonical_entries = canonical_pool.get("anthropic") if isinstance(canonical_pool, dict) else None
            if isinstance(canonical_entries, list) and canonical_entries:
                merged["anthropic"] = list(canonical_entries)
            else:
                merged.pop("anthropic", None)
        return merged

    provider_entries = pool.get(provider_id)
    if isinstance(provider_entries, list) and provider_entries:
        return list(provider_entries)
    global_entries = global_pool.get(provider_id)
    return list(global_entries) if isinstance(global_entries, list) else []
'''


def _version(root: Path) -> str:
    text = (root / VERSION_PATH).read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(text)
    if not match:
        raise ValueError(f"Could not determine Hermes version from {root / VERSION_PATH}")
    return match.group(1)


def _replace_function(source: str, name: str, replacement: str) -> str:
    """Replace exactly one top-level function using AST source coordinates."""
    tree = ast.parse(source)
    matches = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one top-level {name} definition")
    node = matches[0]
    lines = source.splitlines(keepends=True)
    start = sum(len(line) for line in lines[: node.lineno - 1])
    end = sum(len(line) for line in lines[: node.end_lineno])
    return source[:start] + replacement + "\n\n" + source[end:]


def patched_sources(auth_source: str, pool_source: str) -> tuple[str, str]:
    """Return patched Hermes modules or fail closed on source drift."""
    already_auth = HELPER_MARKER in auth_source or HELPER_END_MARKER in auth_source
    if already_auth:
        if _is_fully_patched(auth_source, pool_source):
            return auth_source, pool_source
        raise ValueError("Partial or duplicate shared Anthropic auth patch detected")

    if auth_source.count("def _auth_file_path() -> Path:") != 1:
        raise ValueError("Unrecognized Hermes _auth_file_path anchor")
    if auth_source.count("def read_credential_pool(provider_id: Optional[str] = None)") != 1:
        raise ValueError("Unrecognized Hermes read_credential_pool anchor")
    read_node = _top_level_function(auth_source, "read_credential_pool")
    read_source = ast.get_source_segment(auth_source, read_node)
    if not read_source or hashlib.sha256(read_source.encode("utf-8")).hexdigest() != PINNED_READ_POOL_SHA256:
        raise ValueError("Hermes read_credential_pool differs from the pinned 0.20.6 source")
    if pool_source.count("from hermes_cli.auth import (") != 1:
        raise ValueError("Unrecognized credential_pool auth import anchor")
    lock_anchor = "            with _auth_store_lock(\n                timeout_seconds=self._single_use_refresh_lock_timeout()\n            ):\n                synced = sync_entry(entry)"
    if pool_source.count(lock_anchor) != 1:
        raise ValueError("Unrecognized single-use refresh lock anchor")
    if IMPORT_MARKER in pool_source or "_credential_pool_auth_file_path" in pool_source:
        raise ValueError("Partial shared-pool path edit detected")

    auth = auth_source.replace(
        "def _auth_file_path() -> Path:\n",
        _HELPER + "\ndef _auth_file_path() -> Path:\n",
        1,
    )
    auth = _replace_function(auth, "read_credential_pool", _READ_FUNCTION)

    pool = pool_source.replace(
        "from hermes_cli.auth import (\n",
        "from hermes_cli.auth import (\n    " + IMPORT_MARKER + "\n    _credential_pool_auth_file_path,\n",
        1,
    )
    pool = pool.replace(
        lock_anchor,
        "            with _auth_store_lock(\n"
        "                target_path=_credential_pool_auth_file_path(self.provider),\n"
        "                timeout_seconds=self._single_use_refresh_lock_timeout()\n"
        "            ):\n                synced = sync_entry(entry)",
        1,
    )
    # The pool writer must hold the same per-store lock and read/write the same
    # file used by readers and token refresh. Replace this function's exact body.
    write_anchor = "def write_credential_pool(\n"
    if pool_source.count(write_anchor) != 0:
        raise ValueError("write_credential_pool belongs to auth.py, not credential_pool.py")
    auth = _patch_pool_writer(auth)
    for label, text in (("auth.py", auth), ("credential_pool.py", pool)):
        try:
            ast.parse(text)
        except SyntaxError as exc:
            raise ValueError(f"Patched {label} is not valid Python: {exc}") from exc
    return auth, pool


def _patch_pool_writer(auth: str) -> str:
    """Make provider-pool persistence use its canonical path and lock."""
    original = ast.get_source_segment(auth, _top_level_function(auth, "write_credential_pool"))
    if (original is None
            or original.count("with _auth_store_lock():") != 1
            or original.count("auth_store = _load_auth_store()") != 1
            or original.count("return _save_auth_store(auth_store)") != 1):
        raise ValueError("Unrecognized write_credential_pool lock anchor")
    original = original.replace(
        "    with _auth_store_lock():\n        auth_store = _load_auth_store()",
        "    target_path = _credential_pool_auth_file_path(provider_id)\n"
        "    with _auth_store_lock(target_path=target_path):\n        auth_store = _load_auth_store(target_path)",
        1,
    )
    original = original.replace("        return _save_auth_store(auth_store)", "        return _save_auth_store(auth_store, target_path=target_path)", 1)
    if ("target_path = _credential_pool_auth_file_path(provider_id)" not in original
            or "with _auth_store_lock(target_path=target_path):" not in original
            or "auth_store = _load_auth_store(target_path)" not in original
            or "return _save_auth_store(auth_store, target_path=target_path)" not in original):
        raise ValueError("Could not patch write_credential_pool target path")
    return _replace_function(auth, "write_credential_pool", original)


def _top_level_function(source: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(nodes) != 1:
        raise ValueError(f"Expected exactly one top-level {name} definition")
    return nodes[0]


def _is_fully_patched(auth_source: str, pool_source: str) -> bool:
    if (auth_source.count(HELPER_MARKER) != 1
            or auth_source.count(HELPER_END_MARKER) != 1
            or pool_source.count(IMPORT_MARKER) != 1):
        return False
    try:
        helper = ast.get_source_segment(auth_source, _top_level_function(auth_source, "_credential_pool_auth_file_path"))
        reader = ast.get_source_segment(auth_source, _top_level_function(auth_source, "read_credential_pool"))
        writer = ast.get_source_segment(auth_source, _top_level_function(auth_source, "write_credential_pool"))
    except (SyntaxError, ValueError):
        return False
    return bool(
        helper
        and 'provider_id != "anthropic"' in helper
        and '".hermes-team-managed.json"' in helper
        and 'return root / "auth.json"' in helper
        and reader
        and 'store = _load_auth_store(_credential_pool_auth_file_path("anthropic"))' in reader
        and 'canonical = _load_auth_store(_credential_pool_auth_file_path("anthropic"))' in reader
        and writer
        and "target_path = _credential_pool_auth_file_path(provider_id)" in writer
        and "_auth_store_lock(target_path=target_path)" in writer
        and "_load_auth_store(target_path)" in writer
        and "_save_auth_store(auth_store, target_path=target_path)" in writer
        and "target_path=_credential_pool_auth_file_path(self.provider)" in pool_source
    )


def apply_patch(source_root: str | os.PathLike[str], *, expected_version: str = PINNED_VERSION) -> tuple[Path, Path]:
    root = Path(source_root).expanduser().resolve()
    actual = _version(root)
    if actual != expected_version:
        raise ValueError(f"Expected Hermes {expected_version}, found {actual}; refusing to patch")
    auth_path, pool_path = root / AUTH_PATH, root / POOL_PATH
    updated_auth, updated_pool = patched_sources(
        auth_path.read_text(encoding="utf-8"), pool_path.read_text(encoding="utf-8")
    )
    _atomic_write_with_backup(auth_path, updated_auth)
    _atomic_write_with_backup(pool_path, updated_pool)
    return auth_path, pool_path


def _atomic_write_with_backup(target: Path, updated: str) -> None:
    original = target.read_text(encoding="utf-8")
    if original == updated:
        return
    backup = target.with_suffix(target.suffix + ".hermes-team-shared-auth.bak")
    if not backup.exists():
        shutil.copy2(target, backup)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, target.stat().st_mode)
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", help="explicit Hermes source checkout to patch")
    parser.add_argument("--expected-version", default=PINNED_VERSION)
    args = parser.parse_args(argv)
    try:
        paths = apply_patch(args.source_root, expected_version=args.expected_version)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("Patched Hermes shared Anthropic pool:")
    for path in paths:
        print(path)
        backup = path.with_suffix(path.suffix + ".hermes-team-shared-auth.bak")
        if backup.exists():
            print(f"Backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
