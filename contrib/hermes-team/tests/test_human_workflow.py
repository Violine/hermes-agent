from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.controller import Controller, file_lock
from team_engine.human_workflow import (
    WorkflowConflict, add_comment, answer_question, decide_request, get_request, list_requests,
    get_efficiency_report, submit_request,
    retry_prerequisites,
)
from team_engine.human_workflow import _make_gate, _make_owner_question_gate
from tests.test_controller import FakeBoard, git


class HumanWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Workflow Test")
        git(self.repo, "config", "user.email", "workflow@example.invalid")
        (self.repo / "README.md").write_text("project\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "base")
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "data_root": str(self.root / "data"), "bundle_root": str(self.root / "bundle"),
            "board": "test-board", "hermes_root": str(self.root / "hermes"),
            "projects": {"demo": {"repo": str(self.repo), "base_ref": "main",
                                  "checks": [[sys.executable, "-c", "pass"]]}},
            "roles": {
                "team-analyst": {"model": "test", "provider": "test",
                                 "reasoning_effort": "high", "run_budget": 60},
                "team-planner": {"model": "test", "provider": "test",
                                 "reasoning_effort": "high", "run_budget": 60},
            },
            "max_active_features": 1,
        }))
        self.board = FakeBoard()
        self.patch = patch("team_engine.controller.HermesBoard", return_value=self.board)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def submit(self, key, brief="Implement one feature"):
        return submit_request(self.config, project="demo", title=key, brief=brief,
                              request_id=key, actor="owner", channel="test")

    def test_initial_request_starts_analysis_and_is_idempotent(self):
        first = self.submit("request-1")
        duplicate = self.submit("request-1")
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(first["stage"], "analysis")
        self.assertEqual(first['verification_progress']['regression_status'], 'unknown')
        self.assertIsNone(first['activity_stage'])
        self.assertIsNone(first["approval"])
        self.assertTrue(any(card["assignee"] == "team-analyst" for card in self.board.tasks.values()))

    def test_efficiency_is_calculated_only_on_demand_and_uses_git_sha_range(self):
        request = self.submit('efficiency-report')
        run_dir = self.root / 'data' / 'runs' / request['id']
        with (run_dir / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps({'time': 100, 'message': 'Waited 999 seconds for quota.'}) + '\n')
            stream.write(json.dumps({'time': 110, 'efficiency_interval': {
                'kind': 'owner_gate', 'started_at': 100, 'ended_at': 110}}) + '\n')

        report = get_efficiency_report(self.config, request['id'])

        self.assertEqual(report['request_id'], request['id'])
        self.assertEqual(report['waits']['seconds_by_kind'], {'owner_gate': 10})
        self.assertEqual(report['code']['accepted_or_integrated']['status'], 'provisional')
        self.assertEqual(report['code']['accepted_or_integrated']['added'], 0)
        self.assertIsNone(report['workers']['model_active_seconds'])

    def test_read_projections_do_not_wait_behind_long_controller_locks(self):
        request = self.submit('read-with-lock-held')
        state_lock = self.root / 'data' / 'runs' / request['id'] / '.lock'
        global_lock = self.root / 'data' / '.submit.lock'
        with ThreadPoolExecutor(max_workers=3) as pool:
            with file_lock(global_lock), file_lock(state_lock):
                detail = pool.submit(get_request, self.config, request['id'])
                listing = pool.submit(list_requests, self.config)
                efficiency = pool.submit(get_efficiency_report, self.config, request['id'])
                self.assertEqual(detail.result(timeout=0.5)['id'], request['id'])
                self.assertIn(request['id'], [item['id'] for item in listing.result(timeout=0.5)])
                self.assertEqual(efficiency.result(timeout=0.5)['request_id'], request['id'])

    def _environment_blocked_request(self, key='missing-component'):
        request = self.submit(key)
        state_path = self.root / 'data/runs' / request['id'] / 'state.json'
        state = json.loads(state_path.read_text())
        card_id = state['analysis_card']
        self.board.runs[card_id] = [{'id': 10, 'profile': 'team-analyst', 'status': 'blocked'}]
        directory = Path(state['prerequisite_paths'][card_id])
        artifact = directory / 'PREREQUISITES.json'
        artifact.write_text(json.dumps({'schema_version': 1, 'assignee': 'team-analyst',
            'run_id': '10', 'prerequisites': [{'id': 'ubuntu_uidmap',
            'reason': 'Не могу запустить изолированную тестовую базу.',
            'evidence': 'На медиасервере отсутствуют newuidmap и newgidmap.'}]}))
        self.board.block_external(card_id, 'Missing external component')
        Controller(self.config).tick(state)
        return request, state_path, card_id, artifact

    def test_environment_block_is_not_a_product_question_and_has_trusted_command(self):
        request, state_path, card_id, artifact = self._environment_blocked_request()
        public = get_request(self.config, request['id'])
        self.assertEqual(public['stage'], 'blocked')
        self.assertIsNone(public['approval'])
        self.assertEqual(public['prerequisites'][0]['run_id'], '10')
        self.assertIn('sudo apt-get install -y uidmap', public['prerequisites'][0]['catalog']['command'])
        self.assertEqual(len(public['prerequisite_digest']), 64)
        self.assertIn(str(artifact), self.board.tasks[card_id]['body'])

    def test_owner_retry_does_not_install_or_change_requirements_and_invalidates_old_button(self):
        request, state_path, card_id, artifact = self._environment_blocked_request()
        before = json.loads(state_path.read_text())
        digest = get_request(self.config, request['id'])['prerequisite_digest']
        with patch('subprocess.run', side_effect=AssertionError('Recovery must not execute commands')):
            public = retry_prerequisites(self.config, request['id'], digest=digest,
                                         actor='telegram:55', channel='telegram')
        after = json.loads(state_path.read_text())
        self.assertEqual(public['stage'], 'analysis')
        self.assertEqual(self.board.tasks[card_id]['status'], 'ready')
        self.assertEqual(before['brief_revision'], after['brief_revision'])
        self.assertIsNone(public['prerequisite_digest'])
        with self.assertRaises(WorkflowConflict):
            retry_prerequisites(self.config, request['id'], digest=digest,
                                actor='telegram:55', channel='telegram')

    def test_stale_artifact_run_or_changed_evidence_cannot_use_old_retry(self):
        request, state_path, card_id, artifact = self._environment_blocked_request()
        digest = get_request(self.config, request['id'])['prerequisite_digest']
        value = json.loads(artifact.read_text())
        value['prerequisites'][0]['evidence'] = 'Проверка повторена, компонент по-прежнему отсутствует.'
        artifact.write_text(json.dumps(value))
        with self.assertRaises(WorkflowConflict):
            retry_prerequisites(self.config, request['id'], digest=digest,
                                actor='owner', channel='telegram')
        self.board.runs[card_id].append({'id': 11, 'profile': 'team-analyst', 'status': 'blocked'})
        self.assertEqual(get_request(self.config, request['id'])['prerequisites'], [])

    def test_unsafe_artifact_and_quota_parking_do_not_offer_installation(self):
        request, state_path, card_id, artifact = self._environment_blocked_request()
        value = json.loads(artifact.read_text())
        value['prerequisites'][0]['command'] = 'some arbitrary command'
        artifact.write_text(json.dumps(value))
        self.assertEqual(get_request(self.config, request['id'])['prerequisites'], [])
        del value['prerequisites'][0]['command']
        artifact.write_text(json.dumps(value))
        state = json.loads(state_path.read_text())
        state['dispatch'] = {card_id: {'waiting': True}}
        state_path.write_text(json.dumps(state))
        public = get_request(self.config, request['id'])
        self.assertEqual(public['prerequisites'], [])
        self.assertIsNone(public['prerequisite_digest'])

    def test_analyst_waiting_for_subscription_is_not_an_owner_question(self):
        request = self.submit('analyst-quota-wait')
        state_path = self.root / 'data/runs' / request['id'] / 'state.json'
        state = json.loads(state_path.read_text())
        self.board.block_external(state['analysis_card'], 'Waiting for subscription slot')
        controller = Controller(self.config)
        controller.waiting_cards = {state['analysis_card']}
        controller.tick(state)
        self.assertEqual(state['stage'], 'analysis')
        self.assertIsNone(get_request(self.config, request['id'])['approval'])

    def test_comment_during_analysis_preserves_brief_and_forces_fresh_analysis(self):
        request = self.submit("request-comments")
        original_brief = (self.root / "data/runs" / request["id"] / "BRIEF.md").read_text()
        updated = add_comment(self.config, request["id"], comment="Support partial matches",
                              actor="owner", channel="telegram", request_id="comment-1")
        self.assertEqual(updated["stage"], "analysis")
        self.assertIsNone(updated["approval"])
        self.assertEqual(original_brief,
                         (self.root / "data/runs" / request["id"] / "BRIEF.md").read_text())
        self.assertIn("Support partial matches",
                      (self.root / "data/runs" / request["id"] / "OWNER-DISCUSSION.md").read_text())
        state = json.loads((self.root / "data/runs" / request["id"] / "state.json").read_text())
        self.assertGreater(state["brief_revision"], state["analysis_input_revision"])

    def test_blocked_analyst_question_can_be_answered_and_resumed(self):
        request = self.submit("request-question")
        state_path = self.root / "data/runs" / request["id"] / "state.json"
        state = json.loads(state_path.read_text())
        card_id = state["analysis_card"]
        self.board.block_external(card_id, "Which customer identifier should be canonical?")
        Controller(self.config).tick(state)
        pending = get_request(self.config, request["id"])
        self.assertEqual(pending["stage"], "awaiting_owner_input")
        self.assertIn("Which customer identifier", pending["approval"]["question"])
        self.assertEqual(self.board.get_task(state["root_card"])["status"], "blocked")
        answered = add_comment(self.config, request["id"], comment="Use the CRM customer UUID.",
                               actor="owner", channel="telegram", request_id="answer-1")
        self.assertEqual(answered["stage"], "queued")
        Controller(self.config).tick_all()
        latest = get_request(self.config, request["id"])
        self.assertEqual(latest["stage"], "analysis")
        latest_state = json.loads(state_path.read_text())
        self.assertNotEqual(state["analysis_card"], latest_state["analysis_card"])

    def test_owner_question_cannot_be_approved_without_an_answer(self):
        request = self.submit("request-question-approval")
        state_path = self.root / "data/runs" / request["id"] / "state.json"
        state = json.loads(state_path.read_text())
        state["owner_question"] = "Which customer identifier is canonical?"
        (self.root / "data/runs" / request["id"] / "artifacts" / "SPEC.md").write_text(
            "Need one decision before requirements can proceed.\n")
        gate = _make_gate(Controller(self.config), state, "requirements", "Need an answer", ("SPEC.md",))
        gate["question"] = state["owner_question"]
        state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "Ответьте на вопрос"):
            decide_request(self.config, request["id"], gate_id=gate["id"], decision="approve",
                           actor="owner", channel="dashboard", request_id="question-approve")

    def _structured_question_request(self, key="request-structured-question"):
        request = self.submit(key)
        run = self.root / "data/runs" / request["id"]
        (run / "artifacts/QUESTIONS.json").write_text(json.dumps({
            "schema_version": 1,
            "questions": [{"id": "contact_timing", "question": "Когда связаться с клиентом?",
                "example": "Лид пришёл в 14:00, а менеджер занят до 15:00.",
                "options": [{"id": "now", "label": "Сразу", "description": "Поставить задачу сейчас."},
                            {"id": "free", "label": "Когда освободится", "description": "Подождать менеджера."}],
                "recommended_option_id": "now", "recommendation_reason": "Новый лид быстрее получит внимание."},
                {"id": "owner", "question": "Кому назначить лид без менеджера?",
                "example": "В интеграции не удалось определить ответственного.",
                "options": [{"id": "queue", "label": "В общую очередь", "description": "Руководитель назначит позже."},
                            {"id": "stop", "label": "Остановить обработку", "description": "Лид ждёт ручного решения."}],
                "recommended_option_id": "queue", "recommendation_reason": "Лид не потеряется."}]
        }, ensure_ascii=False))
        state_path = run / "state.json"
        state = json.loads(state_path.read_text())
        _make_owner_question_gate(Controller(self.config), state, "Нужно уточнение")
        state_path.write_text(json.dumps(state, ensure_ascii=False))
        return request["id"], state["approval"]["id"]

    def test_structured_owner_answers_are_partial_then_resume_once(self):
        feature_id, gate_id = self._structured_question_request()
        first = answer_question(self.config, feature_id, gate_id=gate_id,
            question_id="contact_timing", option_id="now", actor="owner", channel="telegram",
            request_id="answer-structured-1")
        self.assertEqual(first["stage"], "awaiting_owner_input")
        self.assertEqual(first["approval"]["questions"][0]["answer"]["option_id"], "now")
        self.assertIsNone(first["approval"]["questions"][1]["answer"])
        repeated = answer_question(self.config, feature_id, gate_id=gate_id,
            question_id="contact_timing", option_id="now", actor="owner", channel="telegram",
            request_id="answer-structured-1")
        self.assertEqual(repeated["stage"], "awaiting_owner_input")
        with self.assertRaises(WorkflowConflict):
            answer_question(self.config, feature_id, gate_id=gate_id,
                question_id="contact_timing", option_id="free", actor="owner", channel="telegram",
                request_id="answer-structured-other")
        done = answer_question(self.config, feature_id, gate_id=gate_id,
            question_id="owner", text="Назначить дежурному менеджеру.", actor="owner",
            channel="dashboard", request_id="answer-structured-2")
        self.assertEqual(done["stage"], "queued")
        self.assertIsNone(done["approval"])
        saved = json.loads((self.root / "data/runs" / feature_id / "state.json").read_text())
        self.assertEqual(saved["queue_reason"], "requirements_questions_answered")
        self.assertEqual(len(saved["approval_audit"]), 1)
        self.assertIn("Назначить дежурному менеджеру", saved["owner_answer"])
        retry = answer_question(self.config, feature_id, gate_id=gate_id,
            question_id="owner", text="Назначить дежурному менеджеру.", actor="owner",
            channel="dashboard", request_id="answer-structured-2")
        self.assertEqual(retry["stage"], "queued")
        self.assertEqual(len(json.loads((self.root / "data/runs" / feature_id / "state.json").read_text())["approval_audit"]), 1)

    def test_structured_question_answer_rejects_non_owner_and_stale_gate(self):
        feature_id, gate_id = self._structured_question_request("request-structured-security")
        with self.assertRaises(WorkflowConflict):
            answer_question(self.config, feature_id, gate_id=gate_id,
                question_id="contact_timing", option_id="now", actor="intruder", channel="telegram",
                request_id="answer-not-owner")
        with self.assertRaises(WorkflowConflict):
            answer_question(self.config, feature_id, gate_id="stale-gate",
                question_id="contact_timing", option_id="now", actor="owner", channel="telegram",
                request_id="answer-stale")

    def test_legacy_question_accepts_gate_bound_custom_text_without_inventing_options(self):
        request = self.submit("request-legacy-custom-question")
        state_path = self.root / "data/runs" / request["id"] / "state.json"
        state = json.loads(state_path.read_text())
        _make_owner_question_gate(Controller(self.config), state,
                                  "Как трактовать срок первого контакта?", "BRIEF.md")
        state_path.write_text(json.dumps(state, ensure_ascii=False))
        pending = get_request(self.config, request["id"])
        self.assertEqual(pending["approval"]["question"], "Как трактовать срок первого контакта?")
        self.assertIsNone(pending["approval"]["questions"])
        with self.assertRaises(ValueError):
            answer_question(self.config, request["id"], gate_id=state["approval"]["id"],
                question_id="legacy", option_id="recommended", actor="owner", channel="telegram",
                request_id="legacy-no-invented-option")
        answered = answer_question(self.config, request["id"], gate_id=state["approval"]["id"],
            question_id="legacy", text="Срок считаем от момента получения лида.", actor="owner",
            channel="telegram", request_id="legacy-custom-answer")
        self.assertEqual(answered["stage"], "queued")
        self.assertIsNone(answered["approval"])
        saved = json.loads(state_path.read_text())
        self.assertIn("Срок считаем от момента получения лида", saved["owner_answer"])
        self.assertNotIn("questions", saved["approval_audit"][0])
        with self.assertRaises(WorkflowConflict):
            answer_question(self.config, request["id"], gate_id=state["approval"]["id"],
                question_id="legacy", text="Повтор с другим токеном", actor="owner",
                channel="telegram", request_id="legacy-stale-answer")

    def test_malformed_questions_artifact_blocks_without_ordinary_approval(self):
        request = self.submit("request-malformed-question-contract")
        state_path = self.root / "data/runs" / request["id"] / "state.json"
        state = json.loads(state_path.read_text())
        (self.root / "data/runs" / request["id"] / "artifacts/SPEC.md").write_text(
            "**Итог:** ready\n\nТребования понятны.\n")
        (self.root / "data/runs" / request["id"] / "artifacts/QUESTIONS.json").write_text(
            '{"schema_version":1,"questions":[{"question":"bad"}]}')
        self.board.tasks[state["analysis_card"]]["status"] = "done"
        Controller(self.config).tick(state)
        saved = json.loads(state_path.read_text())
        self.assertEqual(saved["stage"], "blocked")
        self.assertIsNone(saved.get("approval"))
        self.assertIn("формат не прошёл проверку", saved["blocker"])
        self.assertIn("Решение владельца пока не требуется", saved["blocker"])

    def test_structured_judge_answers_resume_judge_without_reopening_analysis(self):
        feature_id = self.submit("request-structured-judge-question")["id"]
        run = self.root / "data/runs" / feature_id
        state_path = run / "state.json"
        state = json.loads(state_path.read_text())
        review_dir = run / "analysis-round-1"
        review_dir.mkdir()
        state["plan_review_path"] = str(review_dir)
        state["approved_spec"] = "Owner approved requirements snapshot."
        state["approved_spec_sha256"] = "kept"
        for name, content in (("SPEC.md", "requirements\n"), ("DESIGN.md", "design\n"),
                              ("PLAN.json", "{}\n")):
            (run / "artifacts" / name).write_text(content)
            (review_dir / name).write_text(content)
        (review_dir / "QUESTIONS.json").write_text(json.dumps({"schema_version": 1,
            "questions": [{"id": "manager_mapping", "question": "Кому попадёт лид, если ответственный не найден?",
                "example": "Система приняла лид, но не распознала отдел менеджера.",
                "options": [{"id": "queue", "label": "В очередь отдела", "description": "Руководитель назначит менеджера позже."},
                            {"id": "retry", "label": "Не принимать лид", "description": "Интеграция сообщит об ошибке."}],
                "recommended_option_id": "queue", "recommendation_reason": "Лид останется виден команде."}]}, ensure_ascii=False))
        gate = _make_gate(Controller(self.config), state, "plan", "Нужно продуктовое решение",
                          ("SPEC.md", "DESIGN.md", "PLAN.json"), include_questions=True)
        state["stage"] = "awaiting_owner_input"
        state_path.write_text(json.dumps(state, ensure_ascii=False))
        self.assertEqual(gate["questions"][0]["recommended_option_id"], "queue")
        with self.assertRaisesRegex(ValueError, "Сначала ответьте на вопросы"):
            decide_request(self.config, feature_id, gate_id=gate["id"], decision="approve",
                           actor="owner", channel="dashboard", request_id="judge-question-approve")
        answered = answer_question(self.config, feature_id, gate_id=gate["id"],
            question_id="manager_mapping", option_id="queue", actor="owner", channel="dashboard",
            request_id="judge-question-answer")
        self.assertEqual(answered["stage"], "queued")
        saved = json.loads(state_path.read_text())
        self.assertEqual(saved["queue_reason"], "judge_answered")
        self.assertEqual(saved.get("analysis_round"), state.get("analysis_round"))
        self.assertEqual(saved["approved_spec"], "Owner approved requirements snapshot.")
        self.assertFalse((review_dir / "QUESTIONS.json").exists())
        self.assertTrue((review_dir / "QUESTIONS-answered-r1.json").exists())

    def test_partial_structured_answer_is_in_analysis_context_if_gate_is_superseded(self):
        feature_id, gate_id = self._structured_question_request("request-partial-answer-context")
        answer_question(self.config, feature_id, gate_id=gate_id,
            question_id="contact_timing", option_id="free", actor="owner", channel="telegram",
            request_id="partial-answer-context")
        context_path = self.root / "data/runs" / feature_id / "OWNER-DISCUSSION.md"
        self.assertIn("Когда освободится — Подождать менеджера.", context_path.read_text())
        superseded = add_comment(self.config, feature_id, comment="Также покажите напоминание менеджеру.",
            actor="owner", channel="telegram", request_id="comment-after-partial-answer")
        self.assertEqual(superseded["stage"], "queued")
        self.assertIn("Когда освободится — Подождать менеджера.", context_path.read_text())

    def test_judge_question_comment_queues_judge_with_owner_answer(self):
        request = self.submit("request-judge-question")
        run_dir = self.root / "data/runs" / request["id"]
        state_path = run_dir / "state.json"
        state = json.loads(state_path.read_text())
        state["owner_question"] = "Should the endpoint remain backward compatible?"
        artifacts = run_dir / "artifacts"
        for name, content in (("SPEC.md", "requirements\n"),
                              ("DESIGN.md", "design\n"), ("PLAN.json", "{}\n")):
            (artifacts / name).write_text(content)
        gate = _make_gate(Controller(self.config), state, "plan", "Judge needs an answer",
                          ("SPEC.md", "DESIGN.md", "PLAN.json"))
        gate["question"] = state["owner_question"]
        state_path.write_text(json.dumps(state))
        answered = add_comment(self.config, request["id"],
                               comment="Keep the endpoint backward compatible.",
                               actor="owner", channel="dashboard", request_id="judge-answer")
        self.assertEqual(answered["stage"], "queued")
        saved = json.loads(state_path.read_text())
        self.assertEqual(saved["queue_reason"], "judge_answered")
        self.assertEqual(saved["owner_answer"], "Keep the endpoint backward compatible.")
        self.assertNotEqual(saved.get("root_card"), state.get("root_card"))

    def test_delivery_revision_creates_linked_queued_work_from_candidate(self):
        request = self.submit("request-delivery-revision")
        git(self.repo, "branch", "candidate/reviewed")
        state_path = self.root / "data/runs" / request["id"] / "state.json"
        state = json.loads(state_path.read_text())
        state["candidate_ref"] = "candidate/reviewed"
        state["current_sha"] = git(self.repo, "rev-parse", "candidate/reviewed")
        state["stage"] = "awaiting_delivery"
        (self.root / "data/runs" / request["id"] / "artifacts" / "DELIVERY.json").write_text(
            json.dumps({"candidate_ref": state["candidate_ref"], "status": "ready_for_owner"}))
        gate = _make_gate(Controller(self.config), state, "delivery",
                          "Review result", ("DELIVERY.json",))
        state_path.write_text(json.dumps(state))
        parent = decide_request(self.config, request["id"], gate_id=gate["id"], decision="revise",
                                comment="Add a validation message", actor="owner",
                                channel="dashboard", request_id="delivery-revise-1")
        self.assertEqual(parent["stage"], "blocked")
        self.assertEqual(len(parent["followups"]), 1)
        child = get_request(self.config, parent["followups"][0])
        self.assertEqual(child["stage"], "queued")
        child_state = json.loads((self.root / "data/runs" / child["id"] / "state.json").read_text())
        self.assertEqual(child_state["base_ref"], "candidate/reviewed")
        self.assertEqual(child_state["parent_feature"], request["id"])

    def test_completed_delivery_followup_reopens_parent_acceptance_gate(self):
        request = self.submit("request-delivery-reconcile")
        git(self.repo, "branch", "candidate/reviewed")
        state_path = self.root / "data/runs" / request["id"] / "state.json"
        parent_state = json.loads(state_path.read_text())
        parent_state.update(candidate_ref="candidate/reviewed",
                            current_sha=git(self.repo, "rev-parse", "candidate/reviewed"),
                            stage="blocked", followups=["child-rework"], active_followup="child-rework")
        state_path.write_text(json.dumps(parent_state))
        child_path = self.root / "data/runs/child-rework"
        (child_path / "artifacts").mkdir(parents=True)
        child_state = {"id": "child-rework", "stage": "done", "parent_feature": request["id"],
                       "origin": {"source": "delivery-revision"}, "current_sha": parent_state["current_sha"],
                       "candidate_ref": "candidate/rework"}
        (child_path / "state.json").write_text(json.dumps(child_state))
        (child_path / "artifacts/DELIVERY.json").write_text(json.dumps({"feature_id": "child-rework",
            "status": "ready_for_owner", "candidate_ref": "candidate/rework",
            "head_sha": child_state["current_sha"]}))
        Controller(self.config).tick_all()
        parent = get_request(self.config, request["id"])
        self.assertEqual(parent["stage"], "awaiting_delivery")
        self.assertEqual(parent["approval"]["kind"], "delivery")
        self.assertEqual(parent["artifacts"]["DELIVERY.json"].count("candidate/rework"), 1)

    def test_idempotency_keys_are_bound_to_payload_and_actor(self):
        request = self.submit("request-idempotency")
        add_comment(self.config, request["id"], comment="first", actor="owner",
                    channel="telegram", request_id="comment-key")
        with self.assertRaises(WorkflowConflict):
            add_comment(self.config, request["id"], comment="different", actor="owner",
                        channel="telegram", request_id="comment-key")
        # The original brief remains immutable even when discussion changes.
        self.assertEqual((self.root / "data/runs" / request["id"] / "BRIEF.md").read_text(),
                         "Implement one feature")

    def test_stale_gate_after_artifact_change_is_rejected(self):
        request = self.submit("request-stale")
        state_path = self.root / "data/runs" / request["id"] / "state.json"
        state = json.loads(state_path.read_text())
        artifacts = self.root / "data/runs" / request["id"] / "artifacts"
        (artifacts / "SPEC.md").write_text("A detailed, valid requirements document.\n" * 20)
        controller = Controller(self.config)
        gate = _make_gate(controller, state, "requirements", "Review spec", ("SPEC.md",))
        state["stage"] = "awaiting_requirements"
        state_path.write_text(json.dumps(state))
        (artifacts / "SPEC.md").write_text("Changed after snapshot.\n" * 20)
        with self.assertRaises(WorkflowConflict):
            decide_request(self.config, request["id"], gate_id=gate["id"], decision="approve",
                           actor="owner", channel="dashboard", request_id="decision-old")

    def test_approvals_are_idempotent_and_queue_runs_one_feature_at_a_time(self):
        first = self.submit("request-a")
        second = self.submit("request-b")
        # Simulate the analyst finishing and prepare a requirements gate for
        # each accepted analysis package.
        for request in (first, second):
            run = self.root / "data/runs" / request["id"] / "artifacts"
            (run / "SPEC.md").write_text("A complete requirement specification.\n" * 20)
            state_path = self.root / "data/runs" / request["id"] / "state.json"
            state = json.loads(state_path.read_text())
            if not state.get("analysis_card"):
                controller = Controller(self.config)
                controller.worktree(state, "analysis", state["initial_base_sha"])
                state["analysis_card"] = self.board.create_task(
                    title="Analysis complete", assignee="team-analyst",
                    workspace_path=str(self.repo), initial_status="done")["id"]
            gate = _make_gate(Controller(self.config), state, "requirements",
                              "Approve requirements", ("SPEC.md",))
            state_path.write_text(json.dumps(state))
            request["approval"] = gate
            request["stage"] = "awaiting_requirements"
        for request, action_id in ((first, "approve-a"), (second, "approve-b")):
            decide_request(self.config, request["id"], gate_id=request["approval"]["id"],
                           decision="approve", actor="owner", channel="telegram",
                           request_id=action_id)
        # Replaying the exact Telegram update is safe and returns current state.
        replay = decide_request(self.config, first["id"], gate_id=first["approval"]["id"],
                                decision="approve", actor="owner", channel="telegram",
                                request_id="approve-a")
        self.assertEqual(replay["stage"], "queued")
        Controller(self.config).tick_all()
        states = list_requests(self.config)
        stages = [state["stage"] for state in states]
        self.assertEqual(stages.count("planning"), 1)
        self.assertEqual(stages.count("queued"), 1)

    def test_legacy_submission_still_starts_analysis_without_gates(self):
        controller = Controller(self.config)
        state = controller.submit("demo", "legacy brief", "Legacy task", "legacy")
        self.assertEqual(state["stage"], "analysis")
        self.assertFalse(state["workflow_managed"])
        self.assertIn(state["analysis_card"], self.board.tasks)

    def test_approval_audit_captures_actor_channel_and_exact_gate(self):
        request = self.submit("request-audit")
        artifacts = self.root / "data/runs" / request["id"] / "artifacts"
        (artifacts / "SPEC.md").write_text("A complete requirement specification.\n" * 20)
        state_path = self.root / "data/runs" / request["id"] / "state.json"
        state = json.loads(state_path.read_text())
        gate = _make_gate(Controller(self.config), state, "requirements",
                          "Approve requirements", ("SPEC.md",))
        state_path.write_text(json.dumps(state))
        result = decide_request(self.config, request["id"], gate_id=gate["id"],
                                decision="approve", comment="Looks right", actor="alex",
                                channel="dashboard", request_id="audit-1")
        state = json.loads(state_path.read_text())
        entry = state["approval_audit"][0]
        self.assertEqual(entry["gate_id"], gate["id"])
        self.assertEqual(entry["decision"], "approve")
        self.assertEqual((entry["actor"], entry["channel"]), ("alex", "dashboard"))
        self.assertEqual(result["stage"], "queued")
        self.assertEqual(get_request(self.config, request["id"])["id"], request["id"])


if __name__ == "__main__":
    unittest.main()
