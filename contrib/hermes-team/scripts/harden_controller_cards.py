"""Prepare an auditable, card-scoped Hermes auto-decomposition guard.

This utility patches a pinned Hermes source checkout so the native Kanban
decomposer skips tasks created by the engineering-team controller. Native
decomposition remains enabled for every other task and board card.
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


PINNED_VERSION = "0.20.6"
_SOURCE_RELATIVE_PATH = Path("hermes_cli") / "kanban_decompose.py"
_DB_SOURCE_RELATIVE_PATH = Path("hermes_cli") / "kanban_db.py"
_VERSION_RELATIVE_PATH = Path("hermes_cli") / "__init__.py"
_VERSION_PATTERN = re.compile(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", re.M)
_TASK_STATUS_ANCHOR = (
    '    if task is None:\n'
    '        return DecomposeOutcome(task_id, False, "unknown task id")\n'
)
_GUARD_MARKER = "# hermes-team: skip controller-managed cards in native decomposition"
_TRIAGE_IDS_MARKER = "# hermes-team: exclude controller-managed cards from the auto-decomposer batch"
_TRIAGE_IDS_ANCHOR = "    return [row.id for row in rows]\n"
_GUARD = (
    f"    {_GUARD_MARKER}\n"
    '    if getattr(task, "created_by", None) == "hermes-team-controller":\n'
    '        return DecomposeOutcome(\n'
    '            task_id, False, "controller-managed task is excluded from auto-decomposition"\n'
    '        )\n'
)
_TRIAGE_IDS_FILTER = (
    f"    {_TRIAGE_IDS_MARKER}\n"
    '    return [row.id for row in rows\n'
    '            if getattr(row, "created_by", None) != "hermes-team-controller"]\n'
)
_DB_GUARD_MARKER = "CASE WHEN created_by = 'hermes-team-controller' THEN 'blocked' ELSE 'triage' END"
_DB_TRIAGE_STATUS = "                   SET status        = 'triage',\n"
_DB_MANAGED_STATUS = f"                   SET status        = {_DB_GUARD_MARKER},\n"
_DB_STICKY_BLOCK_MARKER = "# hermes-team: keep controller-managed blocked cards sticky"
_DB_STICKY_BLOCK_ANCHOR = (
    '    row = conn.execute(\n'
    '        "SELECT kind FROM task_events "\n'
    '        "WHERE task_id = ? AND kind IN (\'blocked\', \'unblocked\') "\n'
)
_DB_MANAGED_STICKY_BLOCK = (
    f"    {_DB_STICKY_BLOCK_MARKER}\n"
    '    managed_task = get_task(conn, task_id)\n'
    '    if (managed_task is not None\n'
    '            and getattr(managed_task, "created_by", None) == "hermes-team-controller"\n'
    '            and getattr(managed_task, "status", None) == "blocked"):\n'
    '        return True\n'
)


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
    """Insert the narrow created_by guard or fail closed on unknown source."""
    marker_count = source.count(_GUARD_MARKER)
    triage_marker_count = source.count(_TRIAGE_IDS_MARKER)
    if marker_count or triage_marker_count:
        if (marker_count != 1 or triage_marker_count != 1
                or source.count('getattr(task, "created_by", None)') != 1
                or source.count('getattr(row, "created_by", None)') != 1):
            raise ValueError("Partial or duplicate Hermes team decomposition guard detected")
        try:
            ast.parse(source)
        except SyntaxError as exc:
            raise ValueError(f"Existing Hermes team decomposition guard is invalid: {exc}") from exc
        return source

    if source.count(_TASK_STATUS_ANCHOR) != 1:
        raise ValueError("Unrecognized Hermes decompose_task anchor; refusing to modify source")
    if source.count(_TRIAGE_IDS_ANCHOR) != 1:
        raise ValueError("Unrecognized Hermes list_triage_ids anchor; refusing to modify source")
    updated = source.replace(_TASK_STATUS_ANCHOR, _TASK_STATUS_ANCHOR + _GUARD, 1)
    updated = updated.replace(_TRIAGE_IDS_ANCHOR, _TRIAGE_IDS_FILTER, 1)
    try:
        ast.parse(updated)
    except SyntaxError as exc:
        raise ValueError(f"Patched Hermes source is not valid Python: {exc}") from exc
    return updated


def patched_db_source(source: str) -> str:
    """Keep controller-owned blocked cards out of native recovery/decomposition.

    Hermes only treats a card as sticky-blocked when its latest block event is
    ``blocked``. Controller-owned cards also enter ``blocked`` before any
    ``blocked`` event exists (parked work and quota waits), and block-loop
    routing records ``block_loop_detected`` instead. The managed-card guard in
    ``_has_sticky_block`` covers both cases while preserving native behavior.
    """
    status_marker_count = source.count(_DB_GUARD_MARKER)
    sticky_marker_count = source.count(_DB_STICKY_BLOCK_MARKER)
    if status_marker_count or sticky_marker_count:
        if (status_marker_count != 1 or sticky_marker_count not in {0, 1}
                or _DB_TRIAGE_STATUS in source
                or (sticky_marker_count == 1 and source.count(
                    'getattr(managed_task, "created_by", None) == "hermes-team-controller"'
                ) != 1)
                or (sticky_marker_count == 1 and source.count(
                    'getattr(managed_task, "status", None) == "blocked"'
                ) != 1)):
            raise ValueError("Partial or duplicate Hermes team block-loop guard detected")
        try:
            ast.parse(source)
        except SyntaxError as exc:
            raise ValueError(f"Existing Hermes team block-loop guard is invalid: {exc}") from exc
        if sticky_marker_count == 1:
            return source
        if source.count(_DB_STICKY_BLOCK_ANCHOR) != 1:
            raise ValueError("Unrecognized Hermes sticky-block anchor; refusing to modify source")
        updated = source.replace(_DB_STICKY_BLOCK_ANCHOR,
                                 _DB_MANAGED_STICKY_BLOCK + _DB_STICKY_BLOCK_ANCHOR, 1)
    else:
        if source.count(_DB_TRIAGE_STATUS) != 1:
            raise ValueError("Unrecognized Hermes block-loop status anchor; refusing to modify source")
        if source.count(_DB_STICKY_BLOCK_ANCHOR) != 1:
            raise ValueError("Unrecognized Hermes sticky-block anchor; refusing to modify source")
        updated = source.replace(_DB_TRIAGE_STATUS, _DB_MANAGED_STATUS, 1)
        updated = updated.replace(_DB_STICKY_BLOCK_ANCHOR,
                                  _DB_MANAGED_STICKY_BLOCK + _DB_STICKY_BLOCK_ANCHOR, 1)
    try:
        ast.parse(updated)
    except SyntaxError as exc:
        raise ValueError(f"Patched Hermes block-loop source is not valid Python: {exc}") from exc
    return updated


def apply_patch(
    source_root: str | os.PathLike[str], *, expected_version: str = PINNED_VERSION
) -> tuple[Path, Path]:
    """Patch an explicit pinned checkout, writing one-time backups first."""
    root = Path(source_root).expanduser().resolve()
    actual_version = _hermes_version(root)
    if actual_version != expected_version:
        raise ValueError(f"Expected Hermes {expected_version}, found {actual_version}; refusing to patch")
    targets = (root / _DB_SOURCE_RELATIVE_PATH, root / _SOURCE_RELATIVE_PATH)
    originals = [target.read_text(encoding="utf-8") for target in targets]
    updated = [patched_db_source(originals[0]), patched_source(originals[1])]
    changed = [(target, original, replacement)
               for target, original, replacement in zip(targets, originals, updated)
               if original != replacement]
    # Validate both files before writing either one, so an unknown source
    # layout cannot leave a half-applied runtime patch.
    for target, _original, _replacement in changed:
        backup = target.with_suffix(target.suffix + ".hermes-team.bak")
        if not backup.exists():
            shutil.copy2(target, backup)
    for target, _original, replacement in changed:
        fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(replacement)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp_name, target.stat().st_mode)
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    return targets


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
    for path in paths:
        print(f"Patched Hermes controller-card safety guard: {path}")
        backup = path.with_suffix(path.suffix + ".hermes-team.bak")
        if backup.exists():
            print(f"Backup: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
