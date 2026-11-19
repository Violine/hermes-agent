"""One-shot recovery must preserve code, evidence gates and native run history."""
import json
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_controller as fixtures
from team_engine.contracts import ContractError


class FinalizationAttemptTests(unittest.TestCase):
    setUp = fixtures.ControllerTests.setUp
    tearDown = fixtures.ControllerTests.tearDown
    submit = fixtures.ControllerTests.submit
    enable_test_quota = fixtures.ControllerTests.enable_test_quota
    update_test_quota = fixtures.ControllerTests.update_test_quota

    def candidate(self):
        state = self.submit()
        workspace = self.controller.path(state) / 'worktrees' / 'T-008'
        workspace.parent.mkdir(parents=True, exist_ok=True)
        fixtures.git(self.repo, 'worktree', 'add', '-b', 'demo-finalize', str(workspace), self.base_sha)
        (workspace / 'demo.sql').write_text('SELECT 1;\n')
        fixtures.git(workspace, 'add', '.')
        fixtures.git(workspace, 'commit', '-m', 'Existing demo artifacts')
        sha = fixtures.git(workspace, 'rev-parse', 'HEAD')
        card = self.board.create_task('Demo artifacts', 'team-backend', initial_status='blocked',
                                     workspace_path=str(workspace))
        card.update(current_run_id=None, claim_lock=None, worker_pid=None,
                    consecutive_failures=2, last_failure_error='Iteration budget exhausted (80/80)')
        self.board.runs[card['id']] = [dict(id=131, profile='team-backend', status='gave_up',
            ended_at=100, started_at=10, worker_pid=None, claim_lock=None,
            error='Iteration budget exhausted (80/80)')]
        item = dict(definition=dict(id='T-008', assignee_role='backend', title='Demo artifacts'), status='implementation',
                    round=1, card=card['id'], workspace=str(workspace), base_sha=self.base_sha)
        state.update(stage='verification', items=[item], index=0)
        state['dispatch'] = {card['id']: dict(key=state['id'] + ':dev:T-008', role='backend',
                                            armed=True, waiting=False)}
        prereq = self.controller.path(state) / 'prerequisites' / 'finalization-fixture'
        prereq.mkdir(parents=True)
        state.setdefault('prerequisite_paths', {})[card['id']] = str(prereq)
        state.setdefault('assignment_snapshots', {})[card['id']] = dict(role='team-backend')
        self.controller.save(state)
        return state, item, card, sha

    def prepare(self, state, card, sha, **kwargs):
        return self.controller.prepare_finalization_attempt(state, card['id'], previous_run_id='131',
            expected_sha=sha, instructions='Read the existing committed demo artifacts and the exact task. '
            'Record actual checks and missing evidence truthfully in IMPLEMENTATION.md; then use normal completion.',
            max_seconds=60, max_turns=12, **kwargs)

    def verifier_candidate(self, role='reviewer'):
        state, item, developer, sha = self.candidate()
        developer['status'] = 'done'
        artifacts = self.controller.path(state) / 'artifacts'
        artifacts.mkdir(exist_ok=True)
        (artifacts / 'SPEC.md').write_text('# Approved SPEC\n')
        (artifacts / 'DESIGN.md').write_text('# Approved DESIGN\n')
        (artifacts / 'PLAN.json').write_text('{"schema_version":1}\n')
        state['approved_spec_sha256'] = hashlib.sha256(
            (artifacts / 'SPEC.md').read_bytes()).hexdigest()
        state['approved_plan_sha256'] = self.controller.plan_digest(state)
        key = role + '_card'
        workspace_key = role + '_workspace'
        report_key = role + '_report_path'
        profile = 'team-' + role
        workspace = self.controller.path(state) / 'worktrees' / ('T-008-' + role)
        workspace.parent.mkdir(parents=True, exist_ok=True)
        fixtures.git(self.repo, 'worktree', 'add', '-b', 'demo-' + role, str(workspace), sha)
        previous_id = '157' if role == 'reviewer' else '158'
        verifier = self.board.create_task('Verify demo artifacts', profile,
            initial_status='blocked', workspace_path=str(workspace),
            idempotency_key='verifier-' + role)
        verifier.update(current_run_id=None, claim_lock=None, worker_pid=None,
                        last_failure_error='Iteration budget exhausted (40/40)')
        self.board.runs[verifier['id']] = [dict(id=int(previous_id), profile=profile,
            status='gave_up', ended_at=200, started_at=100, worker_pid=None,
            claim_lock=None, error='Iteration budget exhausted (40/40)')]
        item.update(status='verification', head_sha=sha, reviewer_card=None, qa_card=None,
                    verification_blocked_roles=[role])
        item[key] = verifier['id']
        item[workspace_key] = str(workspace)
        item[report_key] = str(self.controller.round_path(state, item) /
                                ('REVIEW.json' if role == 'reviewer' else 'QA.json'))
        state['dispatch'][verifier['id']] = dict(role=role, key=state['id'] + ':' + role,
                                                armed=True, waiting=False)
        state['assignment_snapshots'][verifier['id']] = dict(role=profile)
        state.update(stage='blocked', resume_stage='verification', index=0)
        self.controller.save(state)
        return state, item, verifier, sha, previous_id, workspace

    def prepare_verifier(self, state, verifier, sha, previous_id, role='reviewer'):
        return self.controller.prepare_verification_finalization_attempt(
            state, verifier['id'], previous_run_id=previous_id, expected_sha=sha,
            instructions=(f'Finish only the existing {role} report for this exact candidate. '
                          'Do not edit code, requirements, approved plan, or prior evidence. '
                          'Record actual checks, findings, and their provenance honestly. '
                          'Do not claim any missing or unrun test passed.'),
            max_seconds=60, max_turns=12)

    def test_preparation_is_durable_before_normal_activation_and_keeps_evidence_absent(self):
        state, item, card, sha = self.candidate()
        limit = self.prepare(state, card, sha)
        saved = json.loads((self.controller.path(state) / 'state.json').read_text())
        self.assertEqual(saved['worker_attempt_limits'][card['id']], limit)
        self.assertEqual(limit['previous_run_id'], '131')
        self.assertEqual(card['status'], 'blocked')
        self.assertEqual(card['consecutive_failures'], 2)
        self.assertEqual(item['status'], 'implementation')
        self.assertFalse((self.controller.round_path(state, item) / 'IMPLEMENTATION.md').exists())
        self.board.before_activate = lambda _: self.assertEqual(
            json.loads((self.controller.path(state) / 'state.json').read_text())['worker_attempt_limits'][card['id']], limit)
        self.controller.activate_with_quota(state, card['id'])
        self.assertEqual(card['status'], 'ready')
        self.assertEqual(card['max_runtime_seconds'], 150)
        self.assertEqual(fixtures.git(item['workspace'], 'rev-parse', 'HEAD'), sha)
        self.assertEqual(len(self.board.runs[card['id']]), 1)

    def test_second_grant_and_second_successor_are_rejected(self):
        state, item, card, sha = self.candidate()
        self.prepare(state, card, sha)
        with self.assertRaisesRegex(ContractError, 'already exists'):
            self.prepare(state, card, sha)
        self.board.runs[card['id']].append(dict(id=145, profile='team-backend', status='gave_up'))
        with self.assertRaisesRegex(ContractError, 'already used'):
            self.controller.activate_with_quota(state, card['id'])
        self.assertEqual(card['status'], 'blocked')
        card['block_kind'] = 'transient'
        self.assertFalse(self.controller._continue_budget_exhausted_implementation(state))

    def test_dirty_changed_and_reported_candidates_are_not_restarted(self):
        state, item, card, sha = self.candidate()
        path = self.controller.round_path(state, item) / 'IMPLEMENTATION.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Already exists; inspect its validity, do not start another model.')
        with self.assertRaisesRegex(ContractError, 'report exists'):
            self.prepare(state, card, sha)
        path.unlink()
        (self.controller.path(state) / 'worktrees/T-008/demo.sql').write_text('SELECT 2;\n')
        with self.assertRaisesRegex(ContractError, 'not clean'):
            self.prepare(state, card, sha)
        fixtures.git(item['workspace'], 'checkout', '--', 'demo.sql')
        with self.assertRaisesRegex(ContractError, 'HEAD changed'):
            self.prepare(state, card, '0' * 40)
        self.assertNotIn('worker_attempt_limits', state)

    def test_unrelated_failures_and_active_cards_are_not_restarted(self):
        state, item, card, sha = self.candidate()
        self.board.runs[card['id']][0]['error'] = '403 authentication failed'
        with self.assertRaisesRegex(ContractError, 'budget-exhausted'):
            self.prepare(state, card, sha)
        card['status'] = 'running'
        with self.assertRaisesRegex(ContractError, 'blocked and unclaimed'):
            self.prepare(state, card, sha)

    def test_malformed_or_excessive_allowances_fail_closed(self):
        state, item, card, sha = self.candidate()
        role = self.controller.config['roles']['team-backend']
        role.update(run_budget=30, max_turns=8)
        with self.assertRaisesRegex(ContractError, 'reduce'):
            self.prepare(state, card, sha)
        state['worker_attempt_limits'] = {card['id']: dict(schema_version=True)}
        with self.assertRaises(ContractError):
            self.controller.current_runtime_cap(state, card['id'], card)

    def test_genuine_test_wait_budget_is_not_replaced(self):
        state, item, card, sha = self.candidate()
        state['test_wait_budgets'] = {card['id']: dict(remaining_seconds=1, remaining_turns=1)}
        with self.assertRaisesRegex(ContractError, 'genuine remaining'):
            self.prepare(state, card, sha)
        self.assertNotIn('worker_attempt_limits', state)

    def test_prepared_attempt_waits_for_a_real_quota_snapshot(self):
        state, item, card, sha = self.candidate()
        self.enable_test_quota()
        record = state['dispatch'][card['id']]
        record.update(settings=dict(self.controller.config['roles']['team-backend']), priority=10)
        self.prepare(state, card, sha)
        self.controller.activate_with_quota(state, card['id'])
        self.assertEqual(card['status'], 'blocked')
        self.assertTrue(record['waiting'])
        self.update_test_quota()
        self.controller.activate_with_quota(state, card['id'])
        self.assertEqual(card['status'], 'ready')
        self.assertEqual(card['max_runtime_seconds'], 150)
        self.assertEqual(self.controller.config['roles']['team-backend']['max_turns'], 40)
        self.assertEqual(self.controller.quota.status()['pools']['test']['active_leases'], 1)

    def test_interrupted_handoff_does_not_arm_a_worker(self):
        state, item, card, sha = self.candidate()
        with patch.object(self.board, 'add_comment', side_effect=RuntimeError('handoff unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'handoff unavailable'):
                self.prepare(state, card, sha)
        saved = json.loads((self.controller.path(state) / 'state.json').read_text())
        self.assertIn(card['id'], saved['worker_attempt_limits'])
        self.assertFalse(saved['dispatch'][card['id']]['armed'])
        self.assertFalse(saved['dispatch'][card['id']]['waiting'])
        self.assertEqual(card['status'], 'blocked')

    def test_pending_owner_decisions_cannot_be_resumed_by_finalization(self):
        state, item, card, sha = self.candidate()
        state['approval'] = dict(status='pending', actor=None)
        for stage in ['awaiting_requirements', 'awaiting_plan', 'awaiting_acceptance', 'awaiting_owner_input']:
            with self.subTest(stage=stage):
                state['stage'] = stage
                with self.assertRaisesRegex(ContractError, 'active implementation'):
                    self.prepare(state, card, sha)
                self.assertEqual(state['approval'], dict(status='pending', actor=None))
                self.assertNotIn('worker_attempt_limits', state)
                self.assertEqual(card['status'], 'blocked')

    def test_verifier_grant_is_durable_parked_and_does_not_fabricate_report(self):
        state, item, card, sha, run_id, _ = self.verifier_candidate()
        limit = self.prepare_verifier(state, card, sha, run_id)
        saved = json.loads((self.controller.path(state) / 'state.json').read_text())
        self.assertEqual(saved['worker_attempt_limits'][card['id']], limit)
        self.assertEqual(limit['purpose'], 'finalize_existing_verification')
        self.assertEqual(limit['previous_run_id'], run_id)
        self.assertEqual(card['status'], 'blocked')
        self.assertFalse(state['dispatch'][card['id']]['armed'])
        self.assertFalse(state['dispatch'][card['id']]['waiting'])
        self.assertFalse(Path(item['reviewer_report_path']).exists())
        self.assertEqual(state['stage'], 'blocked')
        self.assertEqual(state['resume_stage'], 'verification')

    def test_verifier_derives_missing_blocked_role_from_native_exhaustion(self):
        state, item, card, sha, run_id, _ = self.verifier_candidate()
        item.pop('verification_blocked_roles')
        self.prepare_verifier(state, card, sha, run_id)
        self.assertEqual(item['verification_blocked_roles'], ['reviewer'])

    def test_verifier_appends_derived_role_without_dropping_other_blockers(self):
        state, item, card, sha, run_id, _ = self.verifier_candidate()
        qa = self.board.create_task('QA exhausted sibling', 'team-qa', initial_status='blocked',
                                    idempotency_key='blocked-qa-sibling')
        item['qa_card'] = qa['id']
        item['verification_blocked_roles'] = ['qa']
        state['dispatch'][qa['id']] = dict(role='qa', key=state['id'] + ':qa',
                                           armed=False, waiting=False)
        self.prepare_verifier(state, card, sha, run_id)
        self.assertEqual(item['verification_blocked_roles'], ['qa', 'reviewer'])

    def test_verifier_pins_are_rechecked_before_later_quota_admission(self):
        state, item, card, sha, run_id, workspace = self.verifier_candidate()
        self.prepare_verifier(state, card, sha, run_id)
        (self.controller.path(state) / 'artifacts' / 'SPEC.md').write_text('# Changed after preparation\n')
        with self.assertRaisesRegex(ContractError, 'Approved SPEC/PLAN files changed'):
            self.controller.current_runtime_cap(state, card['id'], card)

    def test_verifier_workspace_pin_is_rechecked_before_later_quota_admission(self):
        state, item, card, sha, run_id, workspace = self.verifier_candidate()
        self.prepare_verifier(state, card, sha, run_id)
        (workspace / 'new.txt').write_text('dirty after preparation\n')
        with self.assertRaisesRegex(ContractError, 'Working copy is not clean'):
            self.controller.current_runtime_cap(state, card['id'], card)

    def test_verifier_rechecks_sibling_quiescence_before_quota_admission(self):
        state, item, card, sha, run_id, _ = self.verifier_candidate()
        self.prepare_verifier(state, card, sha, run_id)
        sibling = self.board.create_task('Late sibling', 'team-qa', initial_status='ready',
                                         idempotency_key='late-qa-sibling')
        item['qa_card'] = sibling['id']
        with self.assertRaisesRegex(ContractError, 'sibling native cards terminal'):
            self.controller.current_runtime_cap(state, card['id'], card)

    def test_verifier_refuses_active_sibling_and_active_test_wait(self):
        state, item, card, sha, run_id, _ = self.verifier_candidate()
        qa = self.board.create_task('QA sibling', 'team-qa', initial_status='ready',
                                    idempotency_key='sibling-qa')
        item['qa_card'] = qa['id']
        with self.assertRaisesRegex(ContractError, 'sibling native cards terminal'):
            self.prepare_verifier(state, card, sha, run_id)
        qa['status'] = 'done'
        state['test_waits'] = {qa['id']: dict(card_id=qa['id'], status='waiting',
                                               kind='checks', job_id='job')}
        self.controller.test_job_snapshot = lambda *_: {'status': 'running'}
        with self.assertRaisesRegex(ContractError, 'test jobs terminal'):
            self.prepare_verifier(state, card, sha, run_id)
        self.assertNotIn(card['id'], state.get('worker_attempt_limits', {}))

    def test_verifier_refuses_existing_report_changed_sha_and_wrong_exhaustion(self):
        state, item, card, sha, run_id, workspace = self.verifier_candidate()
        report = Path(item['reviewer_report_path'])
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text('{"verdict":"blocked"}\n')
        with self.assertRaisesRegex(ContractError, 'report exists'):
            self.prepare_verifier(state, card, sha, run_id)
        report.unlink()
        with self.assertRaisesRegex(ContractError, 'pinned candidate SHA'):
            self.prepare_verifier(state, card, '0' * 40, run_id)
        fixtures.git(workspace, 'checkout', '--detach', sha + '^')
        with self.assertRaisesRegex(ContractError, 'workspace HEAD changed'):
            self.prepare_verifier(state, card, sha, run_id)
        fixtures.git(workspace, 'checkout', '--detach', sha)
        self.board.runs[card['id']][0]['error'] = '403 authentication failed'
        with self.assertRaisesRegex(ContractError, 'budget-exhausted'):
            self.prepare_verifier(state, card, sha, run_id)
        self.assertNotIn(card['id'], state.get('worker_attempt_limits', {}))

    def test_verifier_grant_cannot_be_granted_twice(self):
        state, item, card, sha, run_id, _ = self.verifier_candidate('qa')
        self.prepare_verifier(state, card, sha, run_id, 'qa')
        with self.assertRaisesRegex(ContractError, 'already exists'):
            self.prepare_verifier(state, card, sha, run_id, 'qa')

    def test_explicit_resume_reuses_granted_verifier_card(self):
        state, item, card, sha, run_id, _ = self.verifier_candidate()
        self.prepare_verifier(state, card, sha, run_id)
        with patch.object(self.controller, 'retry_blocked_verification') as fresh_retry, \
                patch.object(self.controller, 'activate_with_quota') as admit:
            self.controller.resume_blocked(state, 'operator explicitly resumes bounded verification',
                                           retry_workers=True)
        fresh_retry.assert_not_called()
        admit.assert_called_once_with(state, card['id'])
        self.assertEqual(item['reviewer_card'], card['id'])
        self.assertEqual(card['status'], 'blocked')
        self.assertEqual(state['stage'], 'verification')

    def finish_verifier_with_blocked_report(self, state, item, card, sha, role='reviewer',
                                            successor_id='159'):
        profile = 'team-' + role
        self.board.runs[card['id']].append(dict(id=int(successor_id), profile=profile,
            status='done', outcome='completed', started_at=300, ended_at=400,
            worker_pid=None, claim_lock=None))
        card.update(status='done', current_run_id=None, claim_lock=None, worker_pid=None)
        report_path = Path(item[role + '_report_path'])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({
            'schema_version': 1, 'task_id': item['card'], 'role': role,
            'assignee': profile, 'run_id': successor_id, 'verdict': 'blocked',
            'base_sha': item['base_sha'], 'head_sha': sha,
            'findings': [], 'checks': [{'name': 'evidence', 'status': 'skip'}],
        }))

    def test_used_verifier_allowance_with_real_blocked_result_routes_fresh_card(self):
        state, item, card, sha, run_id, _ = self.verifier_candidate()
        limit = self.prepare_verifier(state, card, sha, run_id)
        self.finish_verifier_with_blocked_report(state, item, card, sha)
        old_report = Path(item['reviewer_report_path'])

        with patch.object(self.controller, 'activate_with_quota') as admit:
            self.controller.resume_blocked(state, 'owner supplied missing evidence', retry_workers=True)

        admit.assert_not_called()
        self.assertNotEqual(item['reviewer_card'], card['id'])
        self.assertEqual(state['worker_attempt_limits'][card['id']], limit)
        self.assertEqual(item['verification_retries']['reviewer'], 1)
        self.assertTrue(old_report.is_file())
        archived = self.controller.round_path(state, item) / 'report-attempts' / 'reviewer' / 'blocked' / 'attempt-1.json'
        self.assertTrue(archived.is_file())
        self.assertEqual(state['stage'], 'verification')
        self.assertNotIn('verification_blocked_roles', item)

    def test_used_verifier_allowance_without_report_fails_closed(self):
        state, item, card, sha, run_id, _ = self.verifier_candidate()
        self.prepare_verifier(state, card, sha, run_id)
        self.board.runs[card['id']].append(dict(id=159, profile='team-reviewer',
            status='done', outcome='completed', started_at=300, ended_at=400))
        card.update(status='done', current_run_id=None)
        with self.assertRaisesRegex(ContractError, 'missing valid blocked-result evidence'):
            self.controller.resume_blocked(state, 'owner supplied evidence', retry_workers=True)
        self.assertEqual(item['reviewer_card'], card['id'])
        self.assertEqual(state['stage'], 'blocked')

    def test_verifier_allowance_with_active_successor_fails_closed(self):
        state, item, card, sha, run_id, _ = self.verifier_candidate()
        self.prepare_verifier(state, card, sha, run_id)
        self.board.runs[card['id']].append(dict(id=159, profile='team-reviewer',
            status='running', started_at=300, ended_at=None))
        card.update(status='running', current_run_id=159)
        with self.assertRaisesRegex(ContractError, 'active native successor'):
            self.controller.resume_blocked(state, 'owner supplied evidence', retry_workers=True)
        self.assertEqual(item['reviewer_card'], card['id'])
        self.assertEqual(state['stage'], 'blocked')

    def test_verifier_allowance_with_multiple_successors_fails_closed(self):
        state, item, card, sha, run_id, _ = self.verifier_candidate()
        self.prepare_verifier(state, card, sha, run_id)
        self.finish_verifier_with_blocked_report(state, item, card, sha)
        self.board.runs[card['id']].append(dict(id=160, profile='team-reviewer',
            status='done', outcome='completed', started_at=410, ended_at=420))
        with self.assertRaisesRegex(ContractError, 'unexpected native successors'):
            self.controller.resume_blocked(state, 'owner supplied evidence', retry_workers=True)
        self.assertEqual(item['reviewer_card'], card['id'])
        self.assertEqual(state['stage'], 'blocked')


if __name__ == '__main__':
    unittest.main()
