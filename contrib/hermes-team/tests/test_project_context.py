from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from team_engine.project_context import (
    ProjectContextError,
    inspect_project_context,
    validate_context_settings,
    write_project_context,
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def commit_all(repo: Path, message: str = "snapshot") -> str:
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def config(repo: Path, **extra) -> dict:
    return {"repo": str(repo), "base_ref": "main", **extra}


def manifest(*, documents=None, components=None, summary="A small example service.") -> dict:
    return {
        "schema_version": 1,
        "name": "Example service",
        "summary": summary,
        "stack": ["Go", "PostgreSQL"],
        "documents": documents if documents is not None else [
            {"path": "docs/ARCHITECTURE.md", "required": True}],
        "components": components if components is not None else [{
            "id": "payments", "paths": ["src/payments"],
            "documents": ["docs/PAYMENTS.md"]}],
        "external_sources": [{"title": "Go docs", "url": "https://go.dev/doc/",
                              "note": "Reference only."}],
    }


class ProjectContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        (self.repo / "README.md").write_text("# Example\nCommitted overview.\n")
        (self.repo / "AGENTS.md").write_text("Root policy.\n")
        (self.repo / "src" / "payments").mkdir(parents=True)
        (self.repo / "src" / "payments" / "AGENTS.md").write_text("Payment-specific rule.\n")
        (self.repo / ".agents").mkdir()
        (self.repo / ".agents" / "project.json").write_text(json.dumps(manifest()))
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "ARCHITECTURE.md").write_text("System boundaries.\n")
        (self.repo / "docs" / "PAYMENTS.md").write_text("Payment flow.\n")
        self.base_sha = commit_all(self.repo)

    def test_pins_to_commit_and_ignores_dirty_worktree(self):
        (self.repo / "README.md").write_text("UNCOMMITTED API_KEY=should-not-appear\n")
        report = inspect_project_context("demo", config(self.repo))
        self.assertTrue(report["ready"])
        self.assertEqual(self.base_sha, report["revision"])
        self.assertTrue(report["sources"])
        self.assertTrue(all("content" not in source for source in report["sources"]))
        self.assertNotIn("UNCOMMITTED", json.dumps(report))

    def test_required_and_malformed_manifest_fail_closed(self):
        (self.repo / ".agents" / "project.json").unlink()
        commit_all(self.repo, "remove manifest")
        self.assertTrue(inspect_project_context("demo", config(self.repo))["ready"])
        required = inspect_project_context("demo", config(self.repo, context_required=True))
        self.assertFalse(required["ready"])
        self.assertIn("manifest is missing", required["errors"][0].lower())

        (self.repo / ".agents" / "project.json").write_text('{"schema_version":1}')
        commit_all(self.repo, "malformed manifest")
        malformed = inspect_project_context("demo", config(self.repo))
        self.assertFalse(malformed["ready"])
        with self.assertRaisesRegex(ProjectContextError, "not ready"):
            write_project_context("demo", config(self.repo), None, self.repo / "packs")

    def test_missing_required_document_fails_closed(self):
        (self.repo / "docs" / "ARCHITECTURE.md").unlink()
        commit_all(self.repo, "remove required document")
        report = inspect_project_context("demo", config(self.repo))
        self.assertFalse(report["ready"])
        self.assertTrue(any("Required context document is missing" in error
                            for error in report["errors"]))

    def test_settings_and_manifest_reject_unsafe_paths(self):
        for path in ("../outside.json", "/etc/passwd", "~/.ssh/id_ed25519",
                     ".env", ".env.production", "docs\\secret.json"):
            with self.subTest(path=path), self.assertRaises(ProjectContextError):
                validate_context_settings(config(self.repo, context_file=path))
        from team_engine.project_context import _validate_repo_path
        for path in ("../outside.md", "/etc/passwd", "~/.ssh/id_ed25519",
                     ".env", ".env.production", "docs/credentials/token.txt",
                     "docs/.aws/credentials.txt", "docs\\secret.md"):
            with self.subTest(path=path), self.assertRaises(ProjectContextError):
                _validate_repo_path(path, label="test")

    def test_symlink_and_oversize_docs_are_rejected(self):
        architecture = self.repo / "docs" / "ARCHITECTURE.md"
        architecture.unlink()
        architecture.symlink_to("PAYMENTS.md")
        commit_all(self.repo, "symlink doc")
        report = inspect_project_context("demo", config(self.repo))
        self.assertTrue(any("symlink" in error.lower() for error in report["errors"]))

        architecture.unlink()
        architecture.write_text("x" * (64 * 1024 + 1))
        commit_all(self.repo, "oversize doc")
        report = inspect_project_context("demo", config(self.repo))
        self.assertFalse(report["ready"])
        self.assertTrue(any("size limit" in error.lower() for error in report["errors"]))

    def test_nested_instruction_scope_and_pack_are_stable(self):
        report = inspect_project_context("demo", config(self.repo))
        nested = next(source for source in report["sources"]
                      if source["path"] == "src/payments/AGENTS.md")
        self.assertEqual("path:src/payments/**", nested["scope"])
        first = write_project_context("demo", config(self.repo), None, self.tmp / "packs")
        second = write_project_context("demo", config(self.repo), None, self.tmp / "packs")
        self.assertEqual(first["context_hash"], second["context_hash"])
        self.assertEqual(Path(first["pack_path"]).read_bytes(), Path(second["pack_path"]).read_bytes())
        text = Path(first["pack_path"]).read_text()
        self.assertIn("does not grant tools or credentials", text)
        self.assertIn("External reference (not fetched)", text)
        self.assertIn("Payment-specific rule.", text)

    def test_hash_changes_with_committed_source_but_not_dirty_source(self):
        original = inspect_project_context("demo", config(self.repo))
        (self.repo / "docs" / "ARCHITECTURE.md").write_text("Dirty change.\n")
        self.assertEqual(original["context_hash"],
                         inspect_project_context("demo", config(self.repo))["context_hash"])
        commit_all(self.repo, "change architecture")
        self.assertNotEqual(original["context_hash"],
                            inspect_project_context("demo", config(self.repo))["context_hash"])

    def test_external_sources_are_references_only(self):
        with patch("team_engine.project_context.subprocess.run", wraps=subprocess.run) as runner:
            report = inspect_project_context("demo", config(self.repo))
        self.assertTrue(report["ready"])
        self.assertTrue(all(call.args[0][0] == "git" for call in runner.call_args_list))

    def test_tracked_tree_entry_limit_fails_closed(self):
        with patch("team_engine.project_context.MAX_TREE_ENTRIES", 2):
            report = inspect_project_context("demo", config(self.repo))
        self.assertFalse(report["ready"])
        self.assertTrue(any("tree exceeds the entry limit" in error for error in report["errors"]))

    def test_manifest_and_documents_reject_secret_assignments(self):
        (self.repo / "docs" / "ARCHITECTURE.md").write_text("API_KEY=actual-secret-value\n")
        commit_all(self.repo, "secret in documentation")
        report = inspect_project_context("demo", config(self.repo))
        self.assertTrue(any("secret assignment" in error for error in report["errors"]))

        data = manifest(summary="Example with token=actual-secret-value")
        (self.repo / ".agents" / "project.json").write_text(json.dumps(data))
        commit_all(self.repo, "secret in manifest metadata")
        report = inspect_project_context("demo", config(self.repo))
        self.assertFalse(report["ready"])
        self.assertTrue(any("manifest appears to contain a secret assignment" in error
                            for error in report["errors"]))

    def test_component_paths_must_exist_at_pinned_revision(self):
        data = manifest(components=[{"id": "missing", "paths": ["src/not-present"],
                                     "documents": []}])
        (self.repo / ".agents" / "project.json").write_text(json.dumps(data))
        commit_all(self.repo, "missing component path")
        report = inspect_project_context("demo", config(self.repo))
        self.assertFalse(report["ready"])
        self.assertTrue(any("component path does not exist" in error.lower()
                            for error in report["errors"]))

    def test_rendered_pack_limit_is_reflected_by_inspection(self):
        documents = []
        for index in range(6):
            name = f"docs/large-{index}.md"
            (self.repo / name).write_text(f"Document {index}\n" + "x" * 48_000)
            documents.append({"path": name, "required": True})
        data = manifest(documents=documents, components=[])
        (self.repo / ".agents" / "project.json").write_text(json.dumps(data))
        commit_all(self.repo, "large context pack")
        report = inspect_project_context("demo", config(self.repo))
        self.assertFalse(report["ready"])
        self.assertTrue(any("pack exceeds the size limit" in error for error in report["errors"]))
        with self.assertRaisesRegex(ProjectContextError, "not ready"):
            write_project_context("demo", config(self.repo), None, self.tmp / "packs")


if __name__ == "__main__":
    unittest.main()
