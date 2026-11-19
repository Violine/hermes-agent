import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch

from team_engine import telegram_workflow as workflow
from team_engine import attachments


class FakeMessage:
    def __init__(self, text="", *, chat=None, sender=None, message_id=7, **kwargs):
        self.text = text
        self.chat = chat or SimpleNamespace(id=55, type="private")
        self.from_user = sender or SimpleNamespace(id=55, is_bot=False)
        self.message_id = message_id
        for key, value in kwargs.items():
            setattr(self, key, value)
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


class FakeAdapter:
    def __init__(self, authorized=True):
        self.authorized = authorized
        self.seen = []

    def _is_user_authorized_from_message(self, message):
        self.seen.append(message)
        return self.authorized


class TelegramWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_env = os.environ.get("HERMES_TEAM_CONFIG")
        self.addCleanup(self._restore_env)
        self.config_path = Path(self.temp.name) / "team.json"
        self._write_config()

    def _restore_env(self):
        if self.old_env is None:
            os.environ.pop("HERMES_TEAM_CONFIG", None)
        else:
            os.environ["HERMES_TEAM_CONFIG"] = self.old_env

    def _write_config(self, *, users=("55",), notify=""):
        config = {"data_root": str(Path(self.temp.name) / "data"),
                  "telegram": {"allowed_user_ids": list(users),
                               "notification_chat_id": notify}}
        self.config_path.write_text(json.dumps(config))
        os.environ["HERMES_TEAM_CONFIG"] = str(self.config_path)
        return config

    @staticmethod
    def _update(text, *, user_id=55, chat_type="private", forwarded=False):
        user = SimpleNamespace(id=user_id, is_bot=False)
        chat = SimpleNamespace(id=user_id, type=chat_type)
        extra = {"forward_origin": object()} if forwarded else {}
        message = FakeMessage(text, chat=chat, sender=user, **extra)
        return SimpleNamespace(effective_user=user, effective_chat=chat,
                               effective_message=message)

    def test_private_owner_command_is_routed_and_request_id_is_durable(self):
        adapter = FakeAdapter()
        called = {}

        def submit(path, **kwargs):
            called.update(kwargs)
            return {"id": "req-1234567890", "title": kwargs["title"],
                    "project": kwargs["project"], "stage": "awaiting_requirements",
                    "stage_label": "Согласование требований", "tasks": [], "approval": None}

        with patch.object(workflow.human_workflow, "submit_request", submit):
            update = self._update("/team new crm-platform Поиск клиентов\nНайти клиента по номеру")
            asyncio.run(workflow._handle_command(update, None, adapter))

        self.assertEqual(called["project"], "crm-platform")
        self.assertEqual(called["title"], "Поиск клиентов")
        self.assertEqual(called["brief"], "Найти клиента по номеру")
        self.assertEqual(called["channel"], "telegram")
        self.assertTrue(called["request_id"].startswith("telegram:55:7:"))
        self.assertEqual(called["origin"]["telegram_user_id"], "55")
        self.assertEqual(adapter.seen, [update.effective_message])
        self.assertIn("Задача принята", update.effective_message.replies[0][0])

    def test_text_submission_binds_staged_documents_and_consumes_only_after_success(self):
        config = workflow._config()
        workflow._stage_attachment(config, "55", "55", {"id": "att-a", "name": "brief.pdf", "size": 20})
        called = {}

        def submit(path, **kwargs):
            called.update(kwargs)
            return {"id": "req-files", "title": kwargs["title"], "project": kwargs["project"],
                    "stage": "awaiting_requirements", "tasks": [], "approval": None}

        with patch.object(workflow.human_workflow, "submit_request", submit):
            update = self._update("/team new crm-platform Feature\nDescription")
            asyncio.run(workflow._handle_command(update, None, FakeAdapter()))
        self.assertEqual(called["attachments"], ["att-a"])
        self.assertEqual(workflow._staged(config, "55", "55"), [])

    def test_redelivered_new_reuses_frozen_attachment_snapshot_and_preserves_later_staged_files(self):
        config = workflow._config()
        workflow._stage_attachment(config, "55", "55", {"id": "att-first", "name": "first.pdf", "size": 20})
        called = []
        def submit(_path, **kwargs):
            called.append(kwargs["attachments"])
            return {"id": "req-redeliver", "title": "Demo", "project": "crm"}
        with patch.object(workflow.human_workflow, "submit_request", submit):
            workflow._submit_with_staged(self.config_path, chat_id="55", user_id="55",
                project="crm", title="Demo", brief="brief", request_id="same-update", actor="telegram:55", channel="telegram")
            workflow._stage_attachment(config, "55", "55", {"id": "att-second", "name": "second.pdf", "size": 30})
            workflow._submit_with_staged(self.config_path, chat_id="55", user_id="55",
                project="crm", title="Demo", brief="brief", request_id="same-update", actor="telegram:55", channel="telegram")
        self.assertEqual(called, [["att-first"], ["att-first"]])
        self.assertEqual([item["id"] for item in workflow._staged(config, "55", "55")], ["att-second"])

    def test_submit_retry_after_ambiguous_accepted_response_reuses_durable_intent(self):
        config = workflow._config()
        workflow._stage_attachment(config, "55", "55", {"id": "att-ambiguous", "name": "brief.pdf", "size": 20})
        attempts = []
        def submit(_path, **kwargs):
            attempts.append(kwargs["attachments"])
            if len(attempts) == 1:
                # Simulate a lost response after the workflow may have accepted the request.
                raise workflow.human_workflow.WorkflowError("connection dropped after submit")
            return {"id": "req-ambiguous", "title": "Demo", "project": "crm"}
        with patch.object(workflow.human_workflow, "submit_request", submit):
            with self.assertRaises(workflow.human_workflow.WorkflowError):
                workflow._submit_with_staged(self.config_path, chat_id="55", user_id="55",
                    project="crm", title="Demo", brief="brief", request_id="same-submit", actor="telegram:55", channel="telegram")
            workflow._submit_with_staged(self.config_path, chat_id="55", user_id="55",
                project="crm", title="Demo", brief="brief", request_id="same-submit", actor="telegram:55", channel="telegram")
        self.assertEqual(attempts, [["att-ambiguous"], ["att-ambiguous"]])
        self.assertEqual(workflow._staged(config, "55", "55"), [])

    def test_attach_retry_after_ambiguous_response_reuses_frozen_attachment_snapshot(self):
        config = workflow._config()
        workflow._stage_attachment(config, "55", "55", {"id": "att-existing", "name": "source.docx", "size": 20})
        attempts = []
        def attach(_path, feature_id, **kwargs):
            attempts.append((feature_id, kwargs["attachment_ids"]))
            if len(attempts) == 1:
                raise workflow.human_workflow.WorkflowError("connection dropped after attach")
            return {"id": feature_id}
        with patch.object(workflow.human_workflow, "attach_documents", attach):
            with self.assertRaises(workflow.human_workflow.WorkflowError):
                workflow._submit_with_staged(self.config_path, chat_id="55", user_id="55", attach_to="req-target",
                    request_id="same-attach", actor="telegram:55", channel="telegram")
            workflow._stage_attachment(config, "55", "55", {"id": "att-later", "name": "later.pdf", "size": 30})
            workflow._submit_with_staged(self.config_path, chat_id="55", user_id="55", attach_to="req-target",
                request_id="same-attach", actor="telegram:55", channel="telegram")
        self.assertEqual(attempts, [("req-target", ["att-existing"]), ("req-target", ["att-existing"])])
        self.assertEqual([item["id"] for item in workflow._staged(config, "55", "55")], ["att-later"])

    def test_failed_submission_keeps_staged_documents(self):
        config = workflow._config()
        item = {"id": "att-retain", "name": "brief.pdf", "size": 20}
        workflow._stage_attachment(config, "55", "55", item)
        with patch.object(workflow.human_workflow, "submit_request",
                          side_effect=workflow.human_workflow.WorkflowError("submit failed")):
            update = self._update("/team new crm-platform Feature\nDescription")
            asyncio.run(workflow._handle_command(update, None, FakeAdapter()))
        self.assertEqual(workflow._staged(config, "55", "55")[0]["id"], item["id"])

    def test_document_caption_upload_authorizes_before_download_and_deduplicates_retries(self):
        user = SimpleNamespace(id=55, is_bot=False)
        chat = SimpleNamespace(id=55, type="private")
        doc = SimpleNamespace(file_id="tg-file", file_unique_id="tg-unique", file_name="brief.pdf",
                              file_size=4, mime_type="application/pdf")
        message = FakeMessage("", chat=chat, sender=user, message_id=991,
                              caption="/team upload", document=doc)
        update = SimpleNamespace(effective_user=user, effective_chat=chat, effective_message=message)
        calls = []
        class TelegramFile:
            async def download_as_bytearray(self): return bytearray(b"%PDF")
        class Bot:
            async def get_file(self, file_id):
                calls.append(("download", file_id))
                return TelegramFile()
        class Context: bot = Bot()
        upload = lambda *args, **kwargs: (
            calls.append(("upload", kwargs["request_id"], kwargs["content"])) or
            {"id": "att-once", "name": kwargs["name"], "size": len(kwargs["content"])})
        with patch.object(attachments, "upload_attachment", upload):
            asyncio.run(workflow._handle_document(update, Context(), FakeAdapter()))
            asyncio.run(workflow._handle_document(update, Context(), FakeAdapter()))
        self.assertEqual(len([call for call in calls if call[0] == "upload"]), 2, message.replies)
        self.assertEqual(calls[1][1], calls[3][1])
        self.assertEqual(workflow._staged(workflow._config(), "55", "55")[0]["id"], "att-once")
        self.assertIn("Документ добавлен", message.replies[0][0])

        denied = FakeAdapter(authorized=False)
        class NoDownload:
            class bot:
                async def get_file(self, _file_id): raise AssertionError("must not download unauthorized file")
        message.replies.clear()
        asyncio.run(workflow._handle_document(update, NoDownload(), denied))
        self.assertIn("только владелец", message.replies[0][0])

    def test_document_size_is_checked_before_download_and_existing_request_attach_consumes_on_success(self):
        user = SimpleNamespace(id=55, is_bot=False)
        chat = SimpleNamespace(id=55, type="private")
        doc = SimpleNamespace(file_id="large", file_unique_id="large-u", file_name="large.pdf",
                              file_size=workflow.MAX_UPLOAD_BYTES + 1)
        message = FakeMessage("", chat=chat, sender=user, message_id=992,
                              caption="/team upload", document=doc)
        update = SimpleNamespace(effective_user=user, effective_chat=chat, effective_message=message)
        class NoDownload:
            class bot:
                async def get_file(self, _file_id): raise AssertionError("oversize metadata must short-circuit")
        asyncio.run(workflow._handle_document(update, NoDownload(), FakeAdapter()))
        self.assertIn("больше 5 МиБ", message.replies[0][0])

        config = workflow._config()
        item = {"id": "att-add", "name": "source.docx", "size": 200}
        workflow._stage_attachment(config, "55", "55", item)
        with patch.object(workflow.human_workflow, "attach_documents", return_value={
                "id": "req-target", "title": "Existing", "project": "crm", "stage": "analysis",
                "tasks": [], "approval": None}) as attach:
            command = self._update("/team attach req-target")
            asyncio.run(workflow._handle_command(command, None, FakeAdapter()))
        self.assertEqual(attach.call_args.kwargs["attachment_ids"], ["att-add"])
        self.assertEqual(workflow._staged(config, "55", "55"), [])

    def test_document_caption_new_submits_current_file_without_separate_upload_command(self):
        user = SimpleNamespace(id=55, is_bot=False)
        chat = SimpleNamespace(id=55, type="private")
        doc = SimpleNamespace(file_id="inline", file_unique_id="inline-unique", file_name="brief.docx",
                              file_size=5, mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        message = FakeMessage("", chat=chat, sender=user, message_id=993,
            caption="/team new crm-platform Requirements\nAnalyze this attached requirements file.", document=doc)
        update = SimpleNamespace(effective_user=user, effective_chat=chat, effective_message=message)
        class TelegramFile:
            async def download_as_bytearray(self): return bytearray(b"docx!")
        class Bot:
            async def get_file(self, _file_id): return TelegramFile()
        class Context: bot = Bot()
        submitted = {}
        upload_calls = []
        def module_upload(*args, **kwargs):
            upload_calls.append(kwargs["request_id"])
            return {"id": "att-inline", "name": kwargs["name"], "size": len(kwargs["content"])}
        def submit(_path, **kwargs):
            submitted.update(kwargs)
            return {"id": "req-inline", "title": kwargs["title"], "project": kwargs["project"],
                    "stage": "awaiting_requirements", "tasks": [], "approval": None}
        with patch.object(attachments, "upload_attachment", module_upload), \
             patch.object(workflow.human_workflow, "submit_request", submit):
            asyncio.run(workflow._handle_document(update, Context(), FakeAdapter()))
            asyncio.run(workflow._handle_document(update, Context(), FakeAdapter()))
        self.assertEqual(submitted["attachments"], ["att-inline"])
        self.assertEqual(len(upload_calls), 1)
        self.assertEqual(submitted["title"], "Requirements")
        self.assertIn("Задача принята", message.replies[0][0])
        self.assertEqual(workflow._staged(workflow._config(), "55", "55"), [])

    def test_unsupported_caption_does_not_download_file_and_text_upload_explains_how_to_attach(self):
        user = SimpleNamespace(id=55, is_bot=False)
        chat = SimpleNamespace(id=55, type="private")
        doc = SimpleNamespace(file_id="file", file_unique_id="unique", file_name="brief.pdf", file_size=5)
        message = FakeMessage("", chat=chat, sender=user, message_id=994,
                              caption="/team approve req-target gate-id", document=doc)
        update = SimpleNamespace(effective_user=user, effective_chat=chat, effective_message=message)
        class NoDownload:
            class bot:
                async def get_file(self, _file_id): raise AssertionError("unsupported caption must be rejected first")
        asyncio.run(workflow._handle_document(update, NoDownload(), FakeAdapter()))
        self.assertIn("Другие команды не принимают файл", message.replies[0][0])
        plain = self._update("/team upload")
        asyncio.run(workflow._handle_command(plain, None, FakeAdapter()))
        self.assertIn("Отправьте документ", plain.effective_message.replies[0][0])

    def test_files_and_clearfiles_are_private_owner_scoped(self):
        config = workflow._config()
        workflow._stage_attachment(config, "55", "55", {"id": "att-a", "name": "scope.docx", "size": 512})
        other = self._update("/team files", user_id=77)
        asyncio.run(workflow._handle_command(other, None, FakeAdapter()))
        self.assertIn("владельцу", other.effective_message.replies[0][0])
        own = self._update("/team files")
        asyncio.run(workflow._handle_command(own, None, FakeAdapter()))
        self.assertIn("scope.docx", own.effective_message.replies[0][0])
        clear = self._update("/team clearfiles")
        asyncio.run(workflow._handle_command(clear, None, FakeAdapter()))
        self.assertEqual(workflow._staged(config, "55", "55"), [])

    def test_group_forwarded_and_non_allowlisted_requests_are_consumed_without_service_calls(self):
        called = []
        with patch.object(workflow.human_workflow, "submit_request", lambda *a, **k: called.append(k)):
            cases = [
                (self._update("/team help", chat_type="group"), FakeAdapter()),
                (self._update("/team help", forwarded=True), FakeAdapter()),
                (self._update("/team help", user_id=99), FakeAdapter()),
                (self._update("/team help"), FakeAdapter(authorized=False)),
            ]
            for update, adapter in cases:
                asyncio.run(workflow._handle_command(update, None, adapter))
                if update.effective_chat.type == "private":
                    self.assertTrue(update.effective_message.replies)
        self.assertEqual(called, [])

    def test_unknown_team_command_returns_local_help(self):
        update = self._update("/team nonsense")
        asyncio.run(workflow._handle_command(update, None, FakeAdapter()))
        self.assertIn("/team approve", update.effective_message.replies[0][0])

    def test_disabled_telegram_workflow_is_not_authorized_and_status_link_is_deep_linked(self):
        self._write_config()
        value = json.loads(self.config_path.read_text())
        value["telegram"]["enabled"] = False
        self.config_path.write_text(json.dumps(value))
        update = self._update("/team help")
        adapter = FakeAdapter()
        self.assertFalse(workflow._authorized(update, adapter))
        self.assertEqual(adapter.seen, [])
        self.assertIn("?request=req-demo", workflow._dashboard_link(value, "req-demo"))

    def test_callback_gate_button_rejects_stale_gate_and_never_approves(self):
        config = workflow._config(self.config_path)
        token = workflow._save_action_token(config, feature_id="req-demo", action="approve",
            gate_id="old-gate", digest="old-digest", user_id="55", chat_id="55")
        approval = {"id": "new-gate", "kind": "plan", "revision": 2,
                    "documents": [{"name": "PLAN.json", "content": "new plan"}]}
        with patch.object(workflow.human_workflow, "get_request", return_value={
            "id": "req-demo", "title": "Demo", "project": "demo", "stage": "awaiting_plan",
            "stage_label": "Согласование плана", "approval": approval, "tasks": []
        }), patch.object(workflow.human_workflow, "decide_request") as decide:
            query = self._query(f"team:a:{token}")
            update = SimpleNamespace(callback_query=query,
                effective_user=SimpleNamespace(id=55, is_bot=False),
                effective_chat=SimpleNamespace(id=55, type="private"))
            asyncio.run(workflow._handle_callback(update, None, FakeAdapter()))
        self.assertIn("устарел", query.edited.lower())
        decide.assert_not_called()

    def test_current_button_uses_adapter_auth_and_explicit_gate_decision(self):
        approval = {"id": "gate-1", "kind": "requirements", "revision": 1,
                    "documents": [{"name": "BRIEF.md", "content": "brief"}]}
        request = {"id": "req-demo", "title": "Demo", "project": "demo",
                   "stage": "awaiting_requirements", "stage_label": "Согласование требований",
                   "approval": approval, "tasks": []}
        token = workflow._save_action_token(workflow._config(), feature_id="req-demo",
            action="approve", gate_id="gate-1", digest=workflow._gate_digest(approval),
            user_id="55", chat_id="55")
        with patch.object(workflow.human_workflow, "get_request", return_value=request), \
             patch.object(workflow.human_workflow, "decide_request", return_value=request) as decide:
            query = self._query(f"team:a:{token}")
            update = SimpleNamespace(callback_query=query,
                effective_user=SimpleNamespace(id=55, is_bot=False),
                effective_chat=SimpleNamespace(id=55, type="private"))
            adapter = FakeAdapter(authorized=True)
            asyncio.run(workflow._handle_callback(update, None, adapter))
        self.assertEqual(decide.call_args.kwargs["gate_id"], "gate-1")
        self.assertEqual(decide.call_args.kwargs["decision"], "approve")
        self.assertEqual(adapter.seen[0].from_user.id, 55)

    def test_callback_actor_spoof_is_denied_before_gate_lookup(self):
        token = workflow._save_action_token(workflow._config(), feature_id="req-demo",
            action="approve", gate_id="gate-1", digest="digest", user_id="55", chat_id="55")
        with patch.object(workflow.human_workflow, "get_request") as get_request, \
             patch.object(workflow.human_workflow, "decide_request") as decide:
            query = self._query(f"team:a:{token}")
            update = SimpleNamespace(callback_query=query,
                effective_user=SimpleNamespace(id=77, is_bot=False),
                effective_chat=SimpleNamespace(id=77, type="private"))
            adapter = FakeAdapter(authorized=True)
            asyncio.run(workflow._handle_callback(update, None, adapter))
        self.assertIn("владельцу", query.edited)
        get_request.assert_not_called()
        decide.assert_not_called()
        self.assertEqual(adapter.seen, [])

    @staticmethod
    def _query(data):
        class Query:
            id = "query-1"
            message = FakeMessage("status", sender=SimpleNamespace(id=0, is_bot=True))
            edited = ""
            async def answer(self):
                pass
            async def edit_message_text(self, text, reply_markup=None):
                self.edited = text
                self.reply_markup = reply_markup
        query = Query()
        query.data = data
        return query

    def test_notification_failure_retries_and_success_persists_cursor(self):
        self._write_config(notify="55")
        request = {"id": "req-demo", "title": "Demo", "project": "demo", "stage": "awaiting_plan",
            "stage_label": "Согласование плана", "approval": {"id": "gate", "kind": "plan",
                "revision": 1, "summary": "Approve", "documents": [{"name": "PLAN.json", "content": "{}"}]},
            "tasks": [], "blocker": None}

        class Bot:
            def __init__(self):
                self.calls = 0
            async def send_message(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary Telegram outage")

        app = SimpleNamespace(bot=Bot())
        with patch.object(workflow.human_workflow, "list_requests", return_value=[request]), \
             patch.object(workflow.log, "exception"):
            asyncio.run(workflow._poll_notifications(app))
            cursor_path = Path(workflow._config()["data_root"]) / "telegram-workflow" / "notification-cursor.json"
            state = json.loads(cursor_path.read_text())["req-demo"]
            self.assertEqual(state["failures"], 1)
            self.assertGreater(state["retry_after"], 0)
            # Simulate expiry so the bounded backoff retry is immediately testable.
            state["retry_after"] = 0
            cursor_path.write_text(json.dumps({"req-demo": state}))
            asyncio.run(workflow._poll_notifications(app))
            asyncio.run(workflow._poll_notifications(app))
        self.assertEqual(app.bot.calls, 2)
        self.assertEqual(json.loads(cursor_path.read_text())["req-demo"]["failures"], 0)

    def test_successful_notification_does_not_repeat_on_next_poll(self):
        self._write_config(notify="55")
        request = {"id": "req-demo", "title": "Demo", "project": "demo", "stage": "blocked",
                   "stage_label": "Требуется внимание", "approval": None, "tasks": [],
                   "blocker": "Waiting on external dependency"}
        class Bot:
            calls = 0
            async def send_message(self, **kwargs): self.calls += 1
        app = SimpleNamespace(bot=Bot())
        cursor_path = Path(workflow._config()["data_root"]) / "telegram-workflow" / "notification-cursor.json"
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        cursor_path.write_text(json.dumps({"req-demo": {"signature": "previous-stage",
                                                         "failures": 0, "retry_after": 0}}))
        with patch.object(workflow.human_workflow, "list_requests", return_value=[request]):
            asyncio.run(workflow._poll_notifications(app))
            asyncio.run(workflow._poll_notifications(app))
        self.assertEqual(app.bot.calls, 1)

    @staticmethod
    def _prerequisite_request(*, evidence="На медиасервере отсутствует newuidmap.", digest=None):
        item = {"id": "ubuntu_uidmap", "assignee": "team-backend", "run_id": "run-1",
                "evidence": evidence,
                "reason": "Для изолированного запуска тестовой базы нужен uidmap.",
                # Even a forged projected command must be ignored by Telegram.
                "catalog": {"command": "sudo rm -rf /"}}
        request = {"id": "req-prereq", "title": "Avito: кабинет менеджера",
                   "project": "crm-platform", "stage": "blocked", "approval": None,
                   "blocker": "generic fallback blocker", "tasks": [], "role_plan": [],
                   "prerequisites": [item], "prerequisite_digest": digest or "a" * 64}
        return request

    def test_first_seen_prerequisite_notifies_once_and_uses_trusted_command(self):
        self._write_config(notify="55")
        request = self._prerequisite_request()
        class Bot:
            calls = []
            async def send_message(self, **kwargs): self.calls.append(kwargs)
        app = SimpleNamespace(bot=Bot())
        with patch.object(workflow.human_workflow, "list_requests", return_value=[request]), \
             patch.object(workflow, "_button_markup", side_effect=lambda rows: rows):
            asyncio.run(workflow._poll_notifications(app))
            asyncio.run(workflow._poll_notifications(app))
        self.assertEqual(len(app.bot.calls), 1)
        text = app.bot.calls[0]["text"]
        self.assertIn("ssh -t alex@192.168.1.20 'sudo apt-get install -y uidmap'", text)
        self.assertNotIn("sudo rm -rf /", text)
        self.assertNotIn("generic fallback blocker", text)
        self.assertNotIn("назначения", text)
        buttons = app.bot.calls[0]["reply_markup"]
        self.assertTrue(any(label == "Установил — проверить снова"
                            for row in buttons for label, _ in row))

    def test_changed_prerequisite_evidence_notifies_again(self):
        self._write_config(notify="55")
        request = self._prerequisite_request()
        class Bot:
            calls = []
            async def send_message(self, **kwargs): self.calls.append(kwargs)
        app = SimpleNamespace(bot=Bot())
        with patch.object(workflow.human_workflow, "list_requests", return_value=[request]), \
             patch.object(workflow, "_button_markup", side_effect=lambda rows: rows):
            asyncio.run(workflow._poll_notifications(app))
            request["prerequisites"][0]["evidence"] = "newuidmap теперь найден, проверка повторилась."
            request["prerequisite_digest"] = "b" * 64
            asyncio.run(workflow._poll_notifications(app))
        self.assertEqual(len(app.bot.calls), 2)

    def test_unstructured_generic_blocker_never_gets_install_command_or_retry_button(self):
        request = {"id": "req-generic", "stage": "blocked", "blocker": "quota or auth failed",
                   "prerequisites": [], "prerequisite_digest": None}
        config = workflow._config()
        self.assertEqual(workflow.format_prerequisite_notice(request, config), "")
        with patch.object(workflow, "_button_markup", side_effect=lambda rows: rows):
            keyboard = workflow._status_keyboard(config, request, user_id="55", chat_id="55")
        self.assertFalse(any(label == "Установил — проверить снова"
                             for row in keyboard for label, _ in row))

    def test_prerequisite_retry_callback_rejects_stale_digest(self):
        config = workflow._config(self.config_path)
        token = workflow._save_action_token(config, feature_id="req-prereq",
            action="retry_prerequisites", digest="a" * 64, user_id="55", chat_id="55")
        request = self._prerequisite_request(digest="b" * 64)
        with patch.object(workflow.human_workflow, "get_request", return_value=request), \
             patch.object(workflow.human_workflow, "retry_prerequisites", create=True) as retry:
            query = self._query(f"team:r:{token}")
            update = SimpleNamespace(callback_query=query,
                effective_user=SimpleNamespace(id=55, is_bot=False),
                effective_chat=SimpleNamespace(id=55, type="private"))
            asyncio.run(workflow._handle_callback(update, None, FakeAdapter()))
        self.assertIn("устарело", query.edited)
        retry.assert_not_called()

    def test_prerequisite_retry_callback_is_owner_bound_and_calls_workflow_only(self):
        config = workflow._config(self.config_path)
        token = workflow._save_action_token(config, feature_id="req-prereq",
            action="retry_prerequisites", digest="a" * 64, user_id="55", chat_id="55")
        request = self._prerequisite_request()
        resumed = {**request, "stage": "verification", "prerequisites": [],
                   "prerequisite_digest": None}
        with patch.object(workflow.human_workflow, "get_request", return_value=request), \
             patch.object(workflow.human_workflow, "retry_prerequisites", return_value=resumed,
                          create=True) as retry, \
             patch("subprocess.run", side_effect=AssertionError("must not run shell")):
            query = self._query(f"team:r:{token}")
            update = SimpleNamespace(callback_query=query,
                effective_user=SimpleNamespace(id=55, is_bot=False),
                effective_chat=SimpleNamespace(id=55, type="private"))
            asyncio.run(workflow._handle_callback(update, None, FakeAdapter()))
        self.assertIn("Запрос на повторную проверку сохранён", query.edited)
        self.assertEqual(retry.call_args.args[1], "req-prereq")
        self.assertEqual(retry.call_args.kwargs, {"digest": "a" * 64,
            "actor": "telegram:55", "channel": "telegram"})

        # A valid-looking button copied to another user's private chat is denied
        # before loading the request or invoking the retry method.
        with patch.object(workflow.human_workflow, "get_request") as get_request, \
             patch.object(workflow.human_workflow, "retry_prerequisites", create=True) as retry:
            query = self._query(f"team:r:{token}")
            update = SimpleNamespace(callback_query=query,
                effective_user=SimpleNamespace(id=77, is_bot=False),
                effective_chat=SimpleNamespace(id=77, type="private"))
            asyncio.run(workflow._handle_callback(update, None, FakeAdapter()))
        self.assertIn("владельцу", query.edited)
        get_request.assert_not_called()
        retry.assert_not_called()

    def test_owner_question_shows_comment_answer_and_has_no_approve_button(self):
        config = workflow._config()
        request = {"id": "req-question", "title": "Question", "project": "demo",
                   "stage": "awaiting_owner_input", "stage_label": "Нужен ответ владельца",
                   "approval": {"id": "q-gate", "kind": "requirements", "revision": 1,
                                "question": "Which identifier should be used?", "documents": []},
                   "tasks": []}
        status = workflow._status_text(request, config)
        self.assertIn("Вопрос 1: Which identifier should be used?", status)
        self.assertNotIn("/team comment", status)
        self.assertNotIn("q-gate", status)
        self.assertNotIn("/team approve", status)
        workflow._status_keyboard(config, request, user_id="55", chat_id="55")
        callback_path = Path(config["data_root"]) / "telegram-workflow" / "callbacks.json"
        actions = [item["action"] for item in json.loads(callback_path.read_text()).values()]
        self.assertNotIn("approve", actions)
        self.assertIn("answer_custom", actions)

    def test_status_explains_separate_test_wait_without_action(self):
        request = {"id": "req-test-wait", "title": "Backend checks", "stage": "implementation",
                   "tasks": [{"role": "team-backend", "status": "waiting_tests",
                              "test_wait": {"kind": "backend", "job_status": "waiting",
                                            "job_id": "private-safe-id", "sha": "a" * 40}}]}
        text = workflow._status_text(request, workflow._config())
        self.assertIn("Проверку ожидает: бэкенд-разработчик", text)
        self.assertIn("Проверка выполняется отдельным заданием", text)
        self.assertIn("Бюджет агента и слот свободны", text)
        self.assertIn("Действие не требуется", text)
        self.assertIn("продолжит по результату проверки", text)
        self.assertNotIn("private-safe-id", text)

    def test_question_card_is_readable_in_russian_and_has_bound_choice_buttons(self):
        config = workflow._config()
        approval = {"id": "gate-q", "kind": "requirements", "revision": 2,
            "documents": [], "questions": [{"id": "first-contact", "question": "Когда менеджеру нужно связаться с новым лидом?",
                "example": "Если заявка пришла в 14:00, считать ли срок до 15:00?",
                "options": [{"id": "immediate", "label": "Сразу", "description": "В течение 15 минут"},
                            {"id": "hour", "label": "В течение часа", "description": "До 15:00"}],
                "recommended_option_id": "hour", "recommendation_reason": "Такой срок реалистичен для первого теста.", "answer": None}]}
        request = {"id": "req-question", "title": "Первый контакт по лидам", "project": "crm",
                   "stage": "awaiting_owner_input", "approval": approval, "tasks": [
                    {"role": "team-analyst", "status": "done"},
                    {"role": "backend-developer", "status": "running", "model": "claude-opus-5-5",
                     "assignment_state": "selected", "provider": "anthropic", "reasoning_effort": "high"}]}
        text = workflow._status_text(request, config)
        self.assertIn("Если заявка пришла в 14:00", text)
        self.assertIn("Рекомендую «В течение часа»", text)
        self.assertNotIn("team-analyst", text)
        self.assertNotIn("ID: req-question", text)
        self.assertIn("?request=req-question", text)
        self.assertNotIn("gate-q", text)
        self.assertNotIn("/team approve", text)
        workflow._status_keyboard(config, request, user_id="55", chat_id="55")
        actions = list(json.loads((Path(config["data_root"]) / "telegram-workflow" / "callbacks.json").read_text()).values())
        option = next(item for item in actions if item["action"] == "answer_option")
        self.assertEqual(option["question_id"], "first-contact")
        self.assertIn(option["option_id"], {"immediate", "hour"})
        self.assertEqual(option["gate_id"], "gate-q")
        self.assertTrue(any(item["action"] == "answer_custom" for item in actions))

    def test_choice_callback_records_choice_only_when_gate_and_question_are_current(self):
        config = workflow._config()
        approval = {"id": "gate-q", "kind": "requirements", "revision": 2, "documents": [],
                    "questions": [{"id": "first-contact", "question": "Когда?", "options": [
                        {"id": "hour", "label": "В течение часа"}], "answer": None}]}
        request = {"id": "req-q", "title": "Leads", "project": "crm", "stage": "awaiting_owner_input",
                   "approval": approval, "tasks": []}
        token = workflow._save_action_token(config, feature_id="req-q", action="answer_option",
            gate_id="gate-q", digest=workflow._gate_digest(approval), user_id="55", chat_id="55",
            question_id="first-contact", option_id="hour")
        with patch.object(workflow.human_workflow, "get_request", return_value=request), \
             patch.object(workflow.human_workflow, "answer_question", return_value=request) as answer:
            query = self._query(f"team:q:{token}")
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=55, is_bot=False),
                effective_chat=SimpleNamespace(id=55, type="private"))
            asyncio.run(workflow._handle_callback(update, None, FakeAdapter()))
        self.assertEqual(answer.call_args.args[1], "req-q")
        self.assertEqual(answer.call_args.kwargs["gate_id"], "gate-q")
        self.assertEqual(answer.call_args.kwargs["question_id"], "first-contact")
        self.assertEqual(answer.call_args.kwargs["option_id"], "hour")
        self.assertEqual(answer.call_args.kwargs["actor"], "telegram:55")

    def test_stale_question_button_is_rejected_without_recording_answer(self):
        config = workflow._config()
        old = {"id": "gate-q", "documents": [], "questions": [{"id": "old-q", "question": "Old", "options": [], "answer": None}]}
        current = {"id": "gate-q", "documents": [], "questions": [{"id": "new-q", "question": "New", "options": [], "answer": None}]}
        token = workflow._save_action_token(config, feature_id="req-q", action="answer_custom",
            gate_id="gate-q", digest=workflow._gate_digest(old), user_id="55", chat_id="55", question_id="old-q")
        request = {"id": "req-q", "title": "Leads", "project": "crm", "stage": "awaiting_owner_input",
                   "approval": current, "tasks": []}
        with patch.object(workflow.human_workflow, "get_request", return_value=request), \
             patch.object(workflow.human_workflow, "answer_question") as answer:
            query = self._query(f"team:q:{token}")
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=55, is_bot=False),
                effective_chat=SimpleNamespace(id=55, type="private"))
            asyncio.run(workflow._handle_callback(update, None, FakeAdapter()))
        self.assertIn("обновился", query.edited)
        answer.assert_not_called()

    def test_repeated_choice_acknowledges_saved_answer_and_shows_next_question(self):
        config = workflow._config()
        token_approval = {"id": "gate-q", "documents": [], "questions": [
            {"id": "first", "question": "Когда?", "options": [{"id": "hour", "label": "За час"}], "answer": None},
            {"id": "second", "question": "Куда назначить?", "options": [{"id": "sales", "label": "Менеджеру продаж"}], "answer": None}]}
        current_approval = {"id": "gate-q", "documents": [], "questions": [
            {"id": "first", "question": "Когда?", "options": [{"id": "hour", "label": "За час"}],
             "answer": {"option_id": "hour", "text": "За час"}},
            token_approval["questions"][1]]}
        token = workflow._save_action_token(config, feature_id="req-q", action="answer_option",
            gate_id="gate-q", digest=workflow._gate_digest(token_approval), user_id="55", chat_id="55",
            question_id="first", option_id="hour")
        request = {"id": "req-q", "title": "Leads", "project": "crm", "stage": "awaiting_owner_input",
                   "approval": current_approval, "tasks": []}
        with patch.object(workflow.human_workflow, "get_request", return_value=request), \
             patch.object(workflow.human_workflow, "answer_question") as answer:
            query = self._query(f"team:q:{token}")
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=55, is_bot=False),
                effective_chat=SimpleNamespace(id=55, type="private"))
            asyncio.run(workflow._handle_callback(update, None, FakeAdapter()))
        self.assertIn("уже сохранён", query.edited)
        answer.assert_not_called()
        saved_actions = list(json.loads((Path(config["data_root"]) / "telegram-workflow" / "callbacks.json").read_text()).values())
        self.assertTrue(any(item["action"] == "answer_option" and item["question_id"] == "second"
                            for item in saved_actions))

    def test_successful_option_answer_adds_buttons_for_the_next_question(self):
        config = workflow._config()
        approval = {"id": "gate-q", "documents": [], "questions": [
            {"id": "first", "question": "Когда?", "options": [{"id": "hour", "label": "За час"}], "answer": None},
            {"id": "second", "question": "Куда назначить?", "options": [{"id": "sales", "label": "Менеджеру продаж"}], "answer": None}]}
        request = {"id": "req-q", "title": "Leads", "project": "crm", "stage": "awaiting_owner_input",
                   "approval": approval, "tasks": []}
        updated = {**request, "approval": {**approval, "questions": [
            {**approval["questions"][0], "answer": {"option_id": "hour", "text": "За час"}},
            approval["questions"][1]]}}
        token = workflow._save_action_token(config, feature_id="req-q", action="answer_option",
            gate_id="gate-q", digest=workflow._gate_digest(approval), user_id="55", chat_id="55",
            question_id="first", option_id="hour")
        with patch.object(workflow.human_workflow, "get_request", return_value=request), \
             patch.object(workflow.human_workflow, "answer_question", return_value=updated), \
             patch.object(workflow, "_button_markup", side_effect=lambda rows: rows):
            query = self._query(f"team:q:{token}")
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=55, is_bot=False),
                effective_chat=SimpleNamespace(id=55, type="private"))
            asyncio.run(workflow._handle_callback(update, None, FakeAdapter()))
        self.assertIn("Выбор записан", query.edited)
        self.assertTrue(query.reply_markup)
        action_records = json.loads((Path(config["data_root"]) / "telegram-workflow" / "callbacks.json").read_text())
        self.assertTrue(any(item["action"] == "answer_option" and item["question_id"] == "second"
                            for item in action_records.values()))

    def test_custom_reply_is_bound_to_exact_prompt_and_unrelated_reply_passes_through(self):
        config = workflow._config()
        approval = {"id": "gate-q", "documents": [], "questions": [{"id": "first-contact",
            "question": "Когда связываться?", "options": [], "answer": None}]}
        request = {"id": "req-q", "title": "Leads", "project": "crm", "stage": "awaiting_owner_input",
                   "approval": approval, "tasks": []}
        token = workflow._save_action_token(config, feature_id="req-q", action="answer_custom",
            gate_id="gate-q", digest=workflow._gate_digest(approval), user_id="55", chat_id="55",
            question_id="first-contact")
        class PromptMessage:
            message_id = 900
            async def reply_text(self, text, reply_markup=None):
                self.sent_text = text
                return SimpleNamespace(message_id=self.message_id)
        with patch.object(workflow.human_workflow, "get_request", return_value=request), \
             patch.dict(sys.modules, {"telegram": SimpleNamespace(ForceReply=lambda **kwargs: kwargs)}):
            query = self._query(f"team:q:{token}")
            query.message = PromptMessage()
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=55, is_bot=False),
                effective_chat=SimpleNamespace(id=55, type="private"))
            asyncio.run(workflow._handle_callback(update, None, FakeAdapter()))
        saved = workflow._get_reply_prompt(config, "900", "55")
        self.assertEqual(saved["question_id"], "first-contact")
        reply_update = self._update("В течение часа")
        reply_update.effective_message.reply_to_message = SimpleNamespace(message_id=900)
        with patch.object(workflow.human_workflow, "answer_question", return_value=request) as answer:
            self.assertTrue(asyncio.run(workflow._handle_question_reply(reply_update, None, FakeAdapter())))
        self.assertEqual(answer.call_args.kwargs["text"], "В течение часа")
        other_update = self._update("Обычный ответ")
        other_update.effective_message.reply_to_message = SimpleNamespace(message_id=901)
        with patch.object(workflow.human_workflow, "answer_question") as answer:
            self.assertFalse(asyncio.run(workflow._handle_question_reply(other_update, None, FakeAdapter())))
            answer.assert_not_called()

    def test_notification_chunks_long_text_and_places_keyboard_on_last_chunk(self):
        self._write_config(notify="55")
        request = {"id": "req-demo", "title": "Demo", "project": "demo", "stage": "awaiting_plan",
            "stage_label": "Согласование плана", "approval": {"id": "gate", "kind": "plan",
                "revision": 1, "documents": []}, "tasks": []}

        class Bot:
            calls = []
            async def send_message(self, **kwargs):
                self.calls.append(kwargs)

        app = SimpleNamespace(bot=Bot())
        with patch.object(workflow.human_workflow, "list_requests", return_value=[request]), \
             patch.object(workflow, "_status_text", return_value="x" * 7601), \
             patch.object(workflow, "_status_keyboard", return_value="approval-keyboard"):
            asyncio.run(workflow._poll_notifications(app))
        self.assertGreater(len(app.bot.calls), 1)
        self.assertTrue(all(len(call["text"]) <= workflow.MAX_MESSAGE for call in app.bot.calls))
        self.assertTrue(all(call["reply_markup"] is None for call in app.bot.calls[:-1]))
        self.assertEqual(app.bot.calls[-1]["reply_markup"], "approval-keyboard")

    def test_no_jobqueue_fallback_is_singleton_and_cancellable(self):
        class Handler:
            def __init__(self, *args, **kwargs): pass
        ext = SimpleNamespace(ApplicationHandlerStop=type("ApplicationHandlerStop", (Exception,), {}),
                              CallbackQueryHandler=Handler, MessageHandler=Handler,
                              filters=SimpleNamespace(Regex=lambda value: value,
                                  Document=SimpleNamespace(ALL=1), CaptionRegex=lambda value: 2))
        handlers = []
        app = SimpleNamespace(bot_data={}, job_queue=None, running=False,
                              add_handler=lambda *args, **kwargs: handlers.append(args))
        async def run():
            workflow._install_handlers(app, FakeAdapter())
            task = app.bot_data["engineering_team_notification_task"]
            workflow._install_handlers(app, FakeAdapter())
            self.assertIs(task, app.bot_data["engineering_team_notification_task"])
            self.assertEqual(len(handlers), 3)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(task.cancelled())
        with patch.dict(sys.modules, {"telegram.ext": ext}):
            asyncio.run(run())

    def test_fallback_polls_only_between_application_start_and_stop(self):
        app = SimpleNamespace(running=False)
        calls = []
        async def sleep(_delay):
            if not calls:
                app.running = True
                calls.append("start")
        async def poll(application):
            self.assertTrue(application.running)
            calls.append("poll")
            application.running = False
        with patch.object(workflow.asyncio, "sleep", sleep), \
             patch.object(workflow, "_poll_notifications", poll):
            asyncio.run(workflow._notification_loop(app))
        self.assertEqual(calls, ["start", "poll"])
        with patch.object(workflow, "_poll_notifications") as poll, self.assertLogs(workflow.log, level="WARNING"):
            asyncio.run(workflow._notification_loop(app, startup_timeout=0))
            poll.assert_not_called()

    def test_status_keeps_direct_dashboard_link_without_a_pending_gate(self):
        request = {"id": "req-link", "title": "Лиды", "stage": "analysis", "tasks": []}
        config = workflow._config()
        text = workflow._status_text(request, config)
        self.assertIn("http://192.168.1.20:9119/engineering-team?request=req-link", text)

    def test_status_reports_selected_pending_unknown_and_forecast_models(self):
        config = workflow._config()
        request = {
            "id": "req-models", "title": "Model transparency", "project": "demo",
            "stage": "implementing", "stage_label": "Разработка",
            "tasks": [
                {"role": "backend", "title": "Old blocked item", "status": "blocked",
                 "assignment_state": "unknown"},
                {"role": "frontend", "title": "Active work", "status": "running",
                 "assignment_state": "selected", "provider": "anthropic",
                 "model": "claude-opus-5-5", "reasoning_effort": "high",
                 "assignment_source": "quota", "quota_pool": "anthropic-main"},
                {"role": "qa", "title": "Waiting test", "status": "ready",
                 "assignment_state": "pending", "provider": "anthropic",
                 "model": "claude-opus-5-5", "reasoning_effort": "xhigh"},
                {"role": "mobile", "status": "blocked", "assignment_state": "unknown"},
            ],
            "role_plan": [{"role": "skeptic", "title": "Challenge plan",
                "purpose": "Independent review", "selection_reason": "Plan requires review",
                "selection_source": "workflow", "assignment_state": "planned",
                "provider": "anthropic", "model": "claude-opus-5-5",
                "reasoning_effort": "xhigh", "quota_pool": "anthropic-main"}],
            "api_key": "must-not-be-rendered", "approval": None,
        }
        text = workflow._status_text(request, config)
        self.assertIn("Сейчас работает: frontend. Состояние: работает.", text)
        self.assertIn("Модель: назначено · anthropic · claude-opus-5-5 · effort high", text)
        self.assertNotIn("backend", text)
        self.assertNotIn("скептик", text)
        self.assertNotIn("прогноз", text)
        self.assertNotIn("must-not-be-rendered", text)

    def test_status_bounds_full_composition_and_points_to_dashboard(self):
        config = workflow._config()
        request = {"id": "req-many", "title": "Many", "project": "demo",
            "stage": "planning", "tasks": [{"role": f"role-{i}", "status": "ready",
                "assignment_state": "unknown"} for i in range(12)],
            "role_plan": [{"role": f"planned-{i}", "model": "some-model",
                "provider": "anthropic", "assignment_state": "planned"} for i in range(8)]}
        text = workflow._status_text(request, config)
        self.assertIn("Карточка задачи: http://", text)
        self.assertNotIn("ID: req-many", text)
        self.assertLess(len(text), workflow.MAX_MESSAGE)

    def test_notification_signature_changes_when_quota_fallback_model_changes(self):
        self._write_config(notify="55")
        request = {"id": "req-fallback", "title": "Fallback", "project": "demo",
            "stage": "implementing", "stage_label": "Разработка", "approval": None,
            "tasks": [{"id": "task-1", "role": "backend", "title": "API", "status": "running",
                "assignment_state": "selected", "assignment_source": "quota",
                "provider": "anthropic", "model": "claude-opus-5-5", "reasoning_effort": "high"}],
            "role_plan": []}
        with patch.object(workflow.human_workflow, "list_requests", return_value=[request]):
            _config, first_pending = workflow._collect_notifications(self.config_path)
            self.assertEqual(first_pending, [])
            cursor_path = Path(workflow._config()["data_root"]) / "telegram-workflow" / "notification-cursor.json"
            first_signature = json.loads(cursor_path.read_text())["req-fallback"]["signature"]
            request["tasks"][0]["provider"] = "anthropic-backup"
            request["tasks"][0]["model"] = "claude-sonnet-5-5"
            _config, second_pending = workflow._collect_notifications(self.config_path)
        self.assertEqual(len(second_pending), 1)
        self.assertNotEqual(first_signature, second_pending[0][1])

    def test_notification_signature_changes_when_owner_answers_a_question(self):
        self._write_config(notify="55")
        approval = {"id": "gate-q", "documents": [], "questions": [
            {"id": "first", "question": "Когда связываться?", "answer": None}]}
        request = {"id": "req-q-progress", "title": "Leads", "project": "crm",
                   "stage": "awaiting_owner_input", "approval": approval, "tasks": [], "role_plan": []}
        with patch.object(workflow.human_workflow, "list_requests", return_value=[request]):
            _config, first = workflow._collect_notifications(self.config_path)
            workflow._mark_notification(_config, "req-q-progress", first[0][1], True)
            request["approval"] = {"id": "gate-q", "documents": [], "questions": [
                {"id": "first", "question": "Когда связываться?", "answer": {"text": "В течение часа"}}]}
            _config, second = workflow._collect_notifications(self.config_path)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0][1], second[0][1])

    def test_native_ptb_handlers_are_scoped_group_zero_and_registered_once(self):
        class Handler:
            def __init__(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs

        class Filter:
            def __and__(self, other): return (self, other)
        reply_filter = Filter()
        text_filter = Filter()
        ext = SimpleNamespace(ApplicationHandlerStop=type("ApplicationHandlerStop", (Exception,), {}),
                              CallbackQueryHandler=Handler, MessageHandler=Handler,
                              filters=SimpleNamespace(Regex=lambda pattern: ("regex", pattern),
                                  Document=SimpleNamespace(ALL=Filter()), CaptionRegex=lambda pattern: Filter(),
                                  REPLY=reply_filter, TEXT=text_filter))
        telegram = SimpleNamespace(ext=ext)
        handlers = []

        class Queue:
            jobs = []
            def run_repeating(self, *args, **kwargs): self.jobs.append((args, kwargs))

        class Application:
            def __init__(self):
                self.job_queue = Queue()
                self.bot_data = {}
            def add_handler(self, handler, group=0): handlers.append((handler, group))

        app = Application()
        with patch.dict(sys.modules, {"telegram": telegram, "telegram.ext": ext}):
            workflow._install_handlers(app, FakeAdapter())
            workflow._install_handlers(app, FakeAdapter())
        self.assertEqual(len(handlers), 4)
        self.assertEqual([group for _, group in handlers], [0, 0, 0, 0])
        self.assertEqual(len(app.job_queue.jobs), 1)
        message_handler = handlers[0][0]
        self.assertTrue(workflow._TEAM_COMMAND.match("/team status"))
        self.assertFalse(workflow._TEAM_COMMAND.match("/teams status"))


if __name__ == "__main__":
    unittest.main()
