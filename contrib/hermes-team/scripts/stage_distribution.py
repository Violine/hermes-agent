#!/usr/bin/env python3
"""Prepare a source-only installation layout in a NEW directory; never deploy it."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil

PACKAGE = Path(__file__).resolve().parents[1]
RUNTIME_DIRS = (
    "team_engine", "scripts", "dashboard", "roles", "templates", "tests",
    "plugins", "skills", "examples", "docs", "references",
)
HOST_TOOLS = (
    "backend_test_bridge.py", "demo_walkthrough_bridge.py",
    "isolated_backend_tests.py", "isolated_demo_walkthrough.py",
    "hermes-rootless-test-docker.sh",
)
UNITS = (
    "hermes-team-controller.service", "hermes-check-jobs.service",
    "hermes-backend-test-jobs.service", "hermes-rootless-test-docker.service",
    "hermes-vpn-dashboard-proxy.service",
)


def source_files(directory: Path):
    for source in sorted(directory.rglob("*")):
        relative = source.relative_to(directory)
        if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            continue
        if source.is_symlink():
            raise ValueError(f"Source symlink is not distributable: {relative}")
        if source.is_file() and source.suffix not in {".pyc", ".pyo"}:
            yield source


def distribution_plan():
    """Return an explicit layout, without reading any installed state."""
    plan = []
    for name in RUNTIME_DIRS:
        directory = PACKAGE / name
        if directory.is_dir():
            plan.extend((p, Path("runtime") / p.relative_to(PACKAGE)) for p in source_files(directory))
    for name in ("README.md", "SOURCE-MANIFEST.json", "requirements-attachments.txt"):
        plan.append((PACKAGE / name, Path("runtime") / name))
    for source in source_files(PACKAGE / "plugins/engineering-team"):
        plan.append((source, Path("plugin/engineering-team") / source.relative_to(PACKAGE / "plugins/engineering-team")))
    for name in ("index.js", "style.css", "manifest.json", "plugin_api.py"):
        plan.append((PACKAGE / "dashboard" / name, Path("plugin/engineering-team/dashboard") / name))
    for name in HOST_TOOLS:
        plan.append((PACKAGE / "scripts" / name, Path("host-tools") / name))
    plan.append((PACKAGE / "team_engine/backend_test_jobs.py", Path("host-tools/backend_test_jobs.py")))
    for name in UNITS:
        plan.append((PACKAGE / "scripts" / name, Path("systemd") / name))
    plan.append((PACKAGE / "dashboard/themes/team-readable.yaml", Path("themes/team-readable.yaml")))
    for source, _ in plan:
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Missing plain source: {source.relative_to(PACKAGE)}")
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="New review/staging directory, never an installed Hermes root")
    parser.add_argument("--write", action="store_true", help="Create the layout; otherwise only print the plan")
    args = parser.parse_args()
    target = args.target.absolute()
    if target.exists() or target.is_symlink():
        parser.error("target already exists; choose a new empty path")
    plan = distribution_plan()
    if not args.write:
        for source, destination in plan:
            print(f"{source.relative_to(PACKAGE)} -> {destination}")
        print(f"Dry run: {len(plan)} files. Add --write to create {target}.")
        return
    target.mkdir(parents=True, exist_ok=False)
    for source, destination in plan:
        output = target / destination
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
    print(f"Prepared {len(plan)} files in {target}. No installation or service changes were performed.")


if __name__ == "__main__":
    main()
