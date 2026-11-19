#!/usr/bin/env python3
"""Prepare a version-pinned Hermes source patch for team worker budgets.

Default mode validates the installed tree without changing it. Use --apply only
after reviewing the report. The patch is intentionally pinned to one Hermes
revision and exact source hashes; unknown updates fail closed for re-audit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time

EXPECTED_REVISION = "5cc1369fa298021f8c740de154ff8c37c30bdcc8"
EXPECTED_HASHES = {
    "agent/turn_context.py": "305560e09e34a9219ae8a089872071cb6c4b778f496ef01409e4a8949dfab9cc",
    "agent/turn_finalizer.py": "584ade4613a11565c2ccd48546327adb9682f58ec70ace9d02921c3c240c14d5",
}
START_MARKER = "# BEGIN team worker budget hook (managed)"
END_MARKER = "# END team worker budget hook (managed)"
FINAL_MARKER = "# TEAM_WORKER_BUDGET_FINALIZER_HOOK"

START_ANCHOR = "    agent.iteration_budget = IterationBudget(agent.max_iterations)\n"
START_INSERT = START_ANCHOR + '''
    # BEGIN team worker budget hook (managed)
    import os as _team_os
    if _team_os.environ.get("HERMES_KANBAN_TASK"):
        from pathlib import Path as _TeamPath
        import sys as _team_sys
        _team_runtime = _TeamPath.home() / ".hermes" / "team" / "runtime"
        if _team_runtime.is_dir() and str(_team_runtime) not in _team_sys.path:
            _team_sys.path.insert(0, str(_team_runtime))
        from team_engine.worker_budget import apply_worker_budget_override
        apply_worker_budget_override(agent)
    # END team worker budget hook (managed)
'''

FINAL_ANCHOR = "    return result\n"
FINAL_INSERT = '''    # TEAM_WORKER_BUDGET_FINALIZER_HOOK
    import os as _team_os
    if _team_os.environ.get("HERMES_KANBAN_TASK"):
        try:
            from pathlib import Path as _TeamPath
            import sys as _team_sys
            _team_runtime = _TeamPath.home() / ".hermes" / "team" / "runtime"
            if _team_runtime.is_dir() and str(_team_runtime) not in _team_sys.path:
                _team_sys.path.insert(0, str(_team_runtime))
            from team_engine.worker_budget import record_worker_budget_usage
            record_worker_budget_usage(
                agent, api_call_count=api_call_count,
                exit_reason=str(_turn_exit_reason),
            )
        except Exception as _team_budget_exc:
            # Do not log model text, environment, or exception details.
            logger.warning("team worker budget usage record failed (%s)", type(_team_budget_exc).__name__)

'''+FINAL_ANCHOR

# Exact pre-fix insertion shipped to the server. Accept it only when reversing
# the hook reproduces the audited upstream file byte-for-byte, then migrate it
# atomically to the self-contained current insertion during --apply.
LEGACY_START_INSERT = START_INSERT.replace(
    "    import os as _team_os\n    if _team_os.environ.get(\"HERMES_KANBAN_TASK\"):",
    "    if os.environ.get(\"HERMES_KANBAN_TASK\"):",
)
LEGACY_FINAL_INSERT = FINAL_INSERT.replace("    import os as _team_os\n", "").replace(
    "    if _team_os.environ.get(\"HERMES_KANBAN_TASK\"):",
    "    if os.environ.get(\"HERMES_KANBAN_TASK\"):",
)


class PatchError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_revision(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        value = result.stdout.strip()
        return value if len(value) == 40 and all(c in "0123456789abcdef" for c in value) else None
    except (OSError, subprocess.SubprocessError):
        return None


def _state(text: str, relative: str) -> str:
    markers = [START_MARKER in text, END_MARKER in text, FINAL_MARKER in text]
    if any(markers):
        if relative.endswith("turn_context.py") and markers[0] and markers[1] and not markers[2]:
            if text.count(START_INSERT) == 1:
                return "patched-current"
            if text.count(LEGACY_START_INSERT) == 1:
                return "patched-legacy"
        if relative.endswith("turn_finalizer.py") and markers[2] and not markers[0] and not markers[1]:
            if text.count(FINAL_INSERT) == 1:
                return "patched-current"
            if text.count(LEGACY_FINAL_INSERT) == 1:
                return "patched-legacy"
        raise PatchError(f"partial or conflicting hook markers in {relative}")
    return "unpatched"


def patched_text(text: str, relative: str) -> str:
    state = _state(text, relative)
    if state == "patched-current":
        return text
    if state == "patched-legacy":
        old = LEGACY_START_INSERT if relative.endswith("turn_context.py") else LEGACY_FINAL_INSERT
        new = START_INSERT if relative.endswith("turn_context.py") else FINAL_INSERT
        return text.replace(old, new, 1)
    if relative.endswith("turn_context.py"):
        anchor, insert = START_ANCHOR, START_INSERT
    elif relative.endswith("turn_finalizer.py"):
        anchor, insert = FINAL_ANCHOR, FINAL_INSERT
    else:
        raise PatchError(f"unsupported target {relative}")
    count = text.count(anchor)
    if count != 1:
        raise PatchError(f"expected one exact anchor in {relative}, found {count}")
    return text.replace(anchor, insert, 1)


def original_text(text: str, relative: str) -> str:
    """Reverse only the exact managed insertion for source provenance checks."""
    if relative.endswith("turn_context.py"):
        inserts, anchor = (START_INSERT, LEGACY_START_INSERT), START_ANCHOR
    elif relative.endswith("turn_finalizer.py"):
        inserts, anchor = (FINAL_INSERT, LEGACY_FINAL_INSERT), FINAL_ANCHOR
    else:
        raise PatchError(f"unsupported target {relative}")
    matches = [insert for insert in inserts if text.count(insert) == 1]
    if len(matches) != 1:
        raise PatchError(f"managed hook differs from the audited insertion in {relative}")
    return text.replace(matches[0], anchor, 1)


def validate_tree(root: Path) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink():
        raise PatchError("Hermes source root must be a plain directory")
    revision = git_revision(root)
    hashes = {}
    for relative, expected in EXPECTED_HASHES.items():
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise PatchError(f"missing plain source file: {relative}")
        actual = sha256(path)
        hashes[relative] = actual
        text = path.read_text(encoding="utf-8")
        state = _state(text, relative)
        if state == "unpatched":
            if revision != EXPECTED_REVISION:
                raise PatchError("Hermes revision differs; refusing unknown source anchors")
            if actual != expected:
                raise PatchError(f"source hash differs from audited revision: {relative}")
            patched_text(text, relative)  # Exact anchor check before backup.
        else:
            if revision not in (EXPECTED_REVISION, None):
                raise PatchError("Hermes revision differs; refusing unknown patched source")
            restored = original_text(text, relative).encode("utf-8")
            if hashlib.sha256(restored).hexdigest() != expected:
                raise PatchError(f"patched source baseline differs from audited revision: {relative}")
    return hashes


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def apply(root: Path, runtime_root: Path, source_helper: Path) -> str:
    hashes = validate_tree(root)
    targets = {rel: root / rel for rel in EXPECTED_HASHES}
    texts = {rel: path.read_text(encoding="utf-8") for rel, path in targets.items()}
    if source_helper.is_symlink() or not source_helper.is_file():
        raise PatchError("worker budget helper source is unavailable")
    if runtime_root.is_symlink():
        raise PatchError("runtime root cannot be a symlink")
    package_dir = runtime_root / "team_engine"
    if not package_dir.is_dir() or package_dir.is_symlink():
        raise PatchError("runtime team_engine package is missing or unsafe")
    helper_target = package_dir / "worker_budget.py"
    if helper_target.is_symlink():
        raise PatchError("runtime worker_budget.py cannot be a symlink")
    if helper_target.exists() and not helper_target.is_file():
        raise PatchError("runtime worker_budget.py is not a plain file")
    helper_bytes = source_helper.read_bytes()
    helper_previous = helper_target.read_bytes() if helper_target.exists() else None
    all_current = all(_state(text, rel) == "patched-current" for rel, text in texts.items())
    if all_current and helper_previous == helper_bytes:
        return "already patched"

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = root / f".team-worker-budget-backup-{stamp}"
    if backup.exists() or backup.is_symlink():
        raise PatchError("backup path already exists; refusing overwrite")
    backup.mkdir(mode=0o700)
    manifest = {
        "revision": git_revision(root),
        "source_sha256": hashes,
        "created_at": stamp,
        "targets": sorted(targets),
        "previous_helper_sha256": hashlib.sha256(helper_previous).hexdigest() if helper_previous is not None else None,
        "new_helper_sha256": hashlib.sha256(helper_bytes).hexdigest(),
    }
    for relative, path in targets.items():
        shutil.copy2(path, backup / Path(relative).name)
    if helper_previous is not None and helper_previous != helper_bytes:
        shutil.copy2(helper_target, backup / "worker_budget.py")
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    os.chmod(backup / "manifest.json", 0o600)

    helper_mode = stat.S_IMODE(source_helper.stat().st_mode) & 0o755
    if helper_previous != helper_bytes:
        _atomic_write(helper_target, helper_bytes, helper_mode or 0o644)
    changed = []
    try:
        for relative, path in targets.items():
            new_text = patched_text(texts[relative], relative)
            mode = stat.S_IMODE(path.stat().st_mode)
            _atomic_write(path, new_text.encode("utf-8"), mode)
            changed.append(path)
    except Exception:
        # The backup is retained for explicit recovery. Avoid silently restoring
        # files if an operator changed them concurrently during patching.
        raise
    return str(backup)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.home() / ".hermes" / "hermes-agent")
    parser.add_argument("--runtime-root", type=Path, default=Path.home() / ".hermes" / "team" / "runtime")
    parser.add_argument("--check", action="store_true", help="validate exact revision and anchors only")
    parser.add_argument("--apply", action="store_true", help="back up and apply the managed patch")
    args = parser.parse_args()
    if args.check == args.apply:
        parser.error("choose exactly one of --check or --apply")
    try:
        hashes = validate_tree(args.root)
        if args.check:
            print(json.dumps({"revision": git_revision(args.root), "files": hashes,
                              "status": "already patched" if all(_state((args.root / p).read_text(), p) == "patched-current" for p in EXPECTED_HASHES) else "ready"}, indent=2))
            return 0
        helper = Path(__file__).resolve().parents[1] / "team_engine" / "worker_budget.py"
        result = apply(args.root, args.runtime_root, helper)
        print(json.dumps({"status": result, "revision": git_revision(args.root)}, indent=2))
        return 0
    except (PatchError, OSError, UnicodeError) as exc:
        print(f"patch refused: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
