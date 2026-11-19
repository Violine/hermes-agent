import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from team_engine.assignments import _test_wait, project_assignments


class AssignmentProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'artifacts').mkdir()
        self.cards = {'t_12345678': {'id': 't_12345678', 'assignee': 'team-analyst',
            'title': 'Analyze', 'status': 'running', 'provider': 'original-provider',
            'model': 'original-model', 'reasoning_effort': 'high'}}
        self.controller = SimpleNamespace(path=lambda _s: self.root,
            board=SimpleNamespace(get_task=lambda card_id: self.cards.get(card_id)),
            config={'roles': {'team-analyst': {'provider': 'changed', 'model': 'changed'},
                             'team-go': {'provider': 'next-provider', 'model': 'next-model',
                                         'reasoning_effort': 'xhigh', 'pool': 'subscription'}},
                    'max_plan_tasks': 12})
        self.state = {'id': 'request', 'stage': 'analysis', 'project': 'demo',
                      'initial_base_sha': 'abc', 'analysis_card': 't_12345678'}

    def test_created_card_does_not_follow_later_role_configuration(self):
        tasks, planned = project_assignments(self.controller, self.state)
        self.assertEqual((tasks[0]['model'], tasks[0]['provider'], tasks[0]['reasoning_effort']),
                         ('original-model', 'original-provider', 'high'))
        self.assertEqual(tasks[0]['assignment_state'], 'selected')
        self.assertNotIn('team-analyst', [item['role'] for item in planned])
        self.assertNotIn('team-mobile', [item['role'] for item in planned])
        self.assertNotIn('team-coordinator', [item['role'] for item in planned])

    def test_quota_selected_pool_and_model_are_visible_not_preferred_settings(self):
        card_id = 't_12345678'
        self.cards[card_id].update(provider='fallback-provider', model='fallback-model', reasoning_effort='low')
        self.state['dispatch'] = {card_id: {'waiting': False, 'settings': {'pool': 'preferred'},
            'selection': {'provider': 'fallback-provider', 'model': 'fallback-model',
                          'reasoning_effort': 'low', 'pool': 'selected-pool'},
            'quota_reason': 'preferred pool is unavailable'}}
        tasks, _ = project_assignments(self.controller, self.state)
        self.assertEqual(tasks[0]['model'], 'fallback-model')
        self.assertEqual(tasks[0]['quota_pool'], 'selected-pool')
        self.assertEqual(tasks[0]['assignment_source'], 'quota')
        self.assertIn('preferred pool is unavailable', tasks[0]['selection_reason'])
        self.state['dispatch'][card_id]['waiting'] = True
        tasks, _ = project_assignments(self.controller, self.state)
        self.assertEqual(tasks[0]['assignment_state'], 'pending')

    def test_missing_history_is_unknown_and_snapshot_survives_missing_card(self):
        self.cards['t_12345678'].update(provider=None, model=None, reasoning_effort=None)
        tasks, _ = project_assignments(self.controller, self.state)
        self.assertIsNone(tasks[0]['model'])
        self.assertEqual(tasks[0]['assignment_state'], 'unknown')
        self.state['assignment_snapshots'] = {'t_12345678': {
            'role': 'team-analyst', 'model': 'saved', 'provider': 'saved-provider',
            'reasoning_effort': 'high', 'quota_pool': 'saved-pool'}}
        self.cards.clear()
        tasks, _ = project_assignments(self.controller, self.state)
        self.assertEqual(tasks[0]['model'], 'saved')
        self.assertEqual(tasks[0]['assignment_source'], 'snapshot')
        self.assertEqual(tasks[0]['status'], 'unknown')

    def test_parked_test_job_is_publicly_waiting_without_running_card(self):
        self.state['waiting_for_tests'] = ['t_12345678']
        self.state['test_waits'] = {'t_12345678': {
            'kind': 'backend', 'job_id': 'job-safe-id', 'sha': 'a' * 40,
            'status': 'waiting', 'started_at': 100, 'updated_at': 125,
            'private_command': 'must not leak'}}
        tasks, _ = project_assignments(self.controller, self.state)
        self.assertEqual(tasks[0]['status'], 'waiting_tests')
        self.assertEqual(tasks[0]['test_wait'], {
            'kind': 'backend', 'job_id': 'job-safe-id', 'sha': 'a' * 40,
            'job_status': 'waiting', 'started_at': 100, 'updated_at': 125})
        self.assertNotIn('private_command', json.dumps(tasks))

    def test_test_wait_selects_numeric_latest_native_run_id(self):
        card_id = 't_12345678'
        state = {
            'waiting_for_tests': [card_id],
            'test_waits': {
                f'{card_id}:99': {'card_id': card_id, 'run_id': '99', 'kind': 'backend',
                    'job_id': 'job-99', 'sha': 'a' * 40, 'status': 'waiting'},
                f'{card_id}:100': {'card_id': card_id, 'run_id': '100', 'kind': 'backend',
                    'job_id': 'job-100', 'sha': 'b' * 40, 'status': 'waiting'},
            },
        }

        projected, waiting = _test_wait(state, card_id)

        self.assertTrue(waiting)
        self.assertEqual(projected['job_id'], 'job-100')
        self.assertEqual(projected['sha'], 'b' * 40)

    def test_plan_specializations_are_proposals_until_approved_and_not_duplicated(self):
        (self.root / 'artifacts/PLAN.json').write_text('{}')
        definition = {'id': 'T-1', 'title': 'Go service', 'assignee_role': 'go'}
        with patch('team_engine.assignments.validate_plan', return_value=[definition]):
            tasks, planned = project_assignments(self.controller, self.state)
            go = next(row for row in planned if row['role'] == 'team-go')
            self.assertEqual(go['assignment_state'], 'proposed')
            self.assertEqual(go['model'], 'next-model')
            self.assertEqual(go['selection_source'], 'plan')
            self.state['approved_plan_sha256'] = 'approved'
            _, planned = project_assignments(self.controller, self.state)
            self.assertEqual(next(row for row in planned if row['role']=='team-go')['assignment_state'], 'planned')
            self.cards['t_87654321'] = {'id':'t_87654321', 'assignee':'team-go', 'status':'ready',
                'model':'pinned', 'provider':'actual', 'reasoning_effort':'high'}
            self.state['items'] = [{'card':'t_87654321', 'definition':definition}]
            tasks, planned = project_assignments(self.controller, self.state)
            self.assertNotIn('team-go', [row['role'] for row in planned])
            self.assertEqual(next(row for row in tasks if row['role']=='team-go')['plan_task_id'], 'T-1')

    def test_projection_allowlist_omits_credentials_and_malformed_plan(self):
        self.cards['t_12345678']['api_key']='private-value'
        self.state['dispatch'] = {'t_12345678': {'settings': {'credential_env_vars':['SECRET']},
                                              'selection': {'token':'private-value'}}}
        (self.root/'artifacts/PLAN.json').write_text('not json')
        tasks, planned = project_assignments(self.controller, self.state)
        rendered=json.dumps([tasks, planned])
        self.assertNotIn('private-value', rendered)
        self.assertNotIn('SECRET', rendered)
        self.assertNotIn('team-go', [row['role'] for row in planned])


if __name__=='__main__':
    unittest.main()
