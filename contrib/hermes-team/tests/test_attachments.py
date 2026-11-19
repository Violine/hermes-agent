from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.attachments import (AttachmentError, MAX_FILE_BYTES, _extract,
    request_attachment_path, upload_attachment)
from team_engine.controller import Controller
from team_engine.project_context import ProjectContextError
from team_engine.human_workflow import (WorkflowConflict, attach_documents,
    decide_request, get_request, submit_request)
from tests.test_controller import FakeBoard, git


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Attachments")
        git(self.repo, "config", "user.email", "attachments@example.invalid")
        (self.repo / "README.md").write_text("project\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "base")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "data_root": str(self.root / "data"), "bundle_root": str(self.root / "bundle"),
            "board": "test-board", "hermes_root": str(self.root / "hermes"),
            "projects": {"demo": {"repo": str(self.repo), "base_ref": "main",
                                  "checks": [[sys.executable, "-c", "pass"]]}},
            "roles": {"team-analyst": {"model": "test", "provider": "test",
                "reasoning_effort": "high", "run_budget": 60}}, "max_active_features": 1,
        }))
        self.board = FakeBoard()
        self.patch = patch("team_engine.controller.HermesBoard", return_value=self.board)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def upload(self, key="upload-1", name="requirements.md", content=b"Need a safe feature."):
        return upload_attachment(self.config, name=name, content=content, request_id=key,
                                 actor="owner", channel="telegram")

    def test_text_and_html_extraction_skip_active_content(self):
        self.assertEqual(_extract(b"hello", ".txt"), ("extracted", None, "hello"))
        status, error, text = _extract(b"<p>Visible</p><script>secret()</script>", ".html")
        self.assertEqual(status, "extracted")
        self.assertIn("Visible", text)
        self.assertNotIn("secret", text)
        self.assertEqual(self.upload("nested", "specs/2026/TASK.md")["name"], "specs/2026/TASK.md")
        status, detail, _ = _extract(b"not really a docx", ".docx")
        self.assertEqual(status, "unsupported")
        self.assertTrue(detail)
        with patch("team_engine.attachments.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout=b'{"text":"partial","truncated":true}', stderr=b"")):
            status, detail, text = _extract(b"a pdf", ".pdf")
        self.assertEqual(status, "truncated")
        self.assertIn("truncated", detail)
        self.assertEqual(text, "partial")
        status, detail, _ = _extract(b"not really a pdf", ".pdf")
        self.assertIn(status, {"unsupported", "no_text"})
        self.assertTrue(detail)

    def test_docx_extraction_is_bounded_and_returns_document_text(self):
        from io import BytesIO
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Requirement</w:t></w:r></w:p></w:body></w:document>')
        status, error, text = _extract(buffer.getvalue(), ".docx")
        self.assertEqual(status, "extracted")
        self.assertIn("Requirement", text)

    def test_upload_is_idempotent_and_rejects_unsafe_or_oversized_files(self):
        first = self.upload()
        again = self.upload()
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(first["sha256"]), 64)
        with self.assertRaises(AttachmentError):
            self.upload("bad-name", "../x.md")
        with self.assertRaises(AttachmentError):
            self.upload("big", "x.txt", b"x" * (MAX_FILE_BYTES + 1))
        with self.assertRaises(AttachmentError):
            self.upload("upload-1", "requirements.md", b"changed")
        truncated = self.upload("large-text", "large.txt", b"x" * 1_000_001)
        self.assertEqual(truncated["text_status"], "truncated")
        self.assertIn("обрезан", truncated["text_error"])

    def test_submit_snapshots_documents_before_analyst_dispatch_and_download_is_scoped(self):
        attachment = self.upload()
        request = submit_request(self.config, project="demo", title="Feature",
            brief="Implement it", request_id="submit-1", actor="owner", channel="telegram",
            attachments=[attachment["id"]])
        self.assertEqual(request["attachments"][0]["id"], attachment["id"])
        path, metadata = request_attachment_path(self.config, request["id"], attachment["id"])
        self.assertEqual(path.read_bytes(), b"Need a safe feature.")
        self.assertEqual(metadata["sha256"], attachment["sha256"])
        state_path = self.root / "data" / "runs" / request["id"] / "state.json"
        state = json.loads(state_path.read_text())
        analyst = self.board.get_task(state["analysis_card"])
        self.assertIn("недоверенный источник", analyst["body"])
        self.assertTrue((self.root / "data" / "runs" / request["id"] / "attachments" / "manifest.json").is_file())
        with self.assertRaises(AttachmentError):
            request_attachment_path(self.config, request["id"], "att-00000000000000000000000000000000")

    def test_failed_context_build_can_retry_same_submission_after_readonly_snapshot(self):
        attachment = self.upload("retry-snapshot")
        from team_engine.project_context import write_project_context as real_writer
        calls = [0]
        def fail_once(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise ProjectContextError("simulated unavailable")
            return real_writer(*args, **kwargs)
        with patch("team_engine.controller.write_project_context", side_effect=fail_once):
            with self.assertRaises(Exception):
                submit_request(self.config, project="demo", title="Retryable",
                    brief="Implement it", request_id="retry-snapshot-submit", actor="owner",
                    channel="telegram", attachments=[attachment["id"]])
            request = submit_request(self.config, project="demo", title="Retryable",
                brief="Implement it", request_id="retry-snapshot-submit", actor="owner",
                channel="telegram", attachments=[attachment["id"]])
        self.assertEqual(request["attachments"][0]["id"], attachment["id"])

    def test_attach_documents_invalidates_requirements_gate_and_approval_digest(self):
        request = submit_request(self.config, project="demo", title="Feature",
            brief="Implement it", request_id="submit-2", actor="owner", channel="telegram")
        state_path = self.root / "data" / "runs" / request["id"] / "state.json"
        state = json.loads(state_path.read_text())
        artifacts = self.root / "data" / "runs" / request["id"] / "artifacts"
        (artifacts / "SPEC.md").write_text("Approved requirements draft\n" * 20)
        controller = Controller(self.config)
        from team_engine.human_workflow import _make_gate
        gate = _make_gate(controller, state, "requirements", "Review", ("SPEC.md",))
        state_path.write_text(json.dumps(state))
        attachment = self.upload("upload-later")
        updated = attach_documents(self.config, request["id"], attachment_ids=[attachment["id"]],
            request_id="add-docs", actor="owner", channel="telegram")
        self.assertEqual(updated["stage"], "queued")
        self.assertIsNone(updated["approval"])
        saved = json.loads(state_path.read_text())
        self.assertIn(attachment["id"], [item["id"] for item in saved["attachments"]])
        with self.assertRaises(WorkflowConflict):
            decide_request(self.config, request["id"], gate_id=gate["id"], decision="approve",
                actor="owner", channel="telegram", request_id="old-approval")

    def test_documents_cannot_be_attached_after_requirements_are_approved(self):
        request = submit_request(self.config, project="demo", title="Feature",
            brief="Implement it", request_id="submit-3", actor="owner", channel="telegram")
        state_path = self.root / "data" / "runs" / request["id"] / "state.json"
        state = json.loads(state_path.read_text())
        state["stage"] = "awaiting_plan"
        state_path.write_text(json.dumps(state))
        attachment = self.upload("upload-after")
        with self.assertRaises(WorkflowConflict):
            attach_documents(self.config, request["id"], attachment_ids=[attachment["id"]],
                request_id="too-late", actor="owner", channel="telegram")

    def test_multiple_late_additions_rewrite_readonly_manifest_atomically_and_replay(self):
        request = submit_request(self.config, project="demo", title="Feature",
            brief="Implement it", request_id="submit-multi", actor="owner", channel="telegram")
        staged = [self.upload("multi-1", "one.txt", b"one"),
                  self.upload("multi-2", "two.txt", b"two")]
        first = attach_documents(self.config, request["id"], attachment_ids=[staged[0]["id"]],
            request_id="attach-first", actor="owner", channel="telegram")
        second = attach_documents(self.config, request["id"], attachment_ids=[staged[1]["id"]],
            request_id="attach-second", actor="owner", channel="telegram")
        self.assertEqual(len(second["attachments"]), 2)
        state_path = self.root / "data" / "runs" / request["id"] / "state.json"
        state = json.loads(state_path.read_text())
        manifest = json.loads((self.root / "data" / "runs" / request["id"] / "attachments" / "manifest.json").read_text())
        self.assertEqual(len(manifest["attachments"]), 2)
        state["stage"] = "awaiting_plan"
        state["approval_audit"] = [{"kind": "requirements", "decision": "approve"}]
        state_path.write_text(json.dumps(state))
        replay = attach_documents(self.config, request["id"], attachment_ids=[staged[1]["id"]],
            request_id="attach-second", actor="owner", channel="telegram")
        self.assertEqual(len(replay["attachments"]), 2)

    def test_dashboard_and_allowlisted_telegram_actor_share_same_owner_identity(self):
        config = json.loads(self.config.read_text())
        config["telegram"] = {"enabled": True, "allowed_user_ids": ["123456"], "owner_user_id": "123456"}
        self.config.write_text(json.dumps(config))
        request = submit_request(self.config, project="demo", title="Feature",
            brief="Implement it", request_id="cross-channel", actor="dashboard:owner", channel="dashboard")
        attachment = upload_attachment(self.config, name="telegram-note.md", content=b"extra context",
            request_id="telegram-upload", actor="telegram:123456", channel="telegram")
        updated = attach_documents(self.config, request["id"], attachment_ids=[attachment["id"]],
            request_id="telegram-attach", actor="telegram:123456", channel="telegram")
        self.assertEqual(updated["attachments"][0]["id"], attachment["id"])


if __name__ == "__main__":
    unittest.main()
