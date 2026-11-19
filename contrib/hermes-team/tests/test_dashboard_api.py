from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from team_engine import dashboard_api as api
from team_engine.web_config import ConfigConflict


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(api.router, prefix='/api/plugins/engineering-team')
        self.client = TestClient(self.app)
        self.url = '/api/plugins/engineering-team/config'
        self.headers = {'Origin': 'http://testserver', 'X-Team-CSRF': api.CSRF_TOKEN}

    def test_mutations_require_same_origin_and_current_csrf(self):
        with patch.object(api, 'save_editor') as save:
            for headers in ({}, {'Origin': 'http://evil.example', 'X-Team-CSRF': api.CSRF_TOKEN},
                            {'Origin': 'http://testserver', 'X-Team-CSRF': 'old'},
                            dict(self.headers, **{'Sec-Fetch-Site': 'cross-site'})):
                with self.subTest(headers=headers):
                    self.assertEqual(self.client.put(self.url, json={}, headers=headers).status_code, 403)
            save.assert_not_called()

    def test_conflict_preserves_client_draft_and_returns_actionable_error(self):
        with patch.object(api, 'save_editor', side_effect=ConfigConflict('Changed elsewhere')):
            response = self.client.put(self.url, json={'revision': 'old', 'config': {}}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'], 'Changed elsewhere')

    def test_save_forwards_validation_and_has_no_service_or_model_side_effect(self):
        with patch.object(api, 'save_editor', return_value={'revision': 'new', 'config': {}}) as save:
            payload = {'revision': 'previous', 'config': {}}
            response = self.client.put(self.url, json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(save.call_args.args[1], payload)
            self.assertIs(save.call_args.kwargs['before_apply'], api.ensure_profiles_idle)
            self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_oversized_body_is_rejected_before_save(self):
        with patch.object(api, 'save_editor') as save:
            response = self.client.put(self.url, content=' ' * (api.MAX_BODY + 1),
                                       headers=dict(self.headers, **{'Content-Type': 'application/json'}))
            self.assertEqual(response.status_code, 413)
            save.assert_not_called()

    def test_running_or_ready_profile_cannot_be_reconfigured(self):
        old = {'roles': {'team-go': {'model': 'a'}}, 'board': 'engineering-team', 'hermes_root': '/none'}
        new = dict(old, roles={'team-go': {'model': 'b'}})
        with patch.object(api, 'HermesBoard') as board:
            for status in ('ready', 'running'):
                board.return_value.list_tasks.return_value = [{'assignee': 'team-go', 'status': status}]
                with self.assertRaises(ConfigConflict):
                    api.ensure_profiles_idle(old, new)
            board.return_value.list_tasks.return_value = []
            api.ensure_profiles_idle(old, new)

    def test_project_context_inspection_uses_only_registered_project_and_hides_internal_paths(self):
        with patch.object(api, 'config_path', return_value=Path('/configured/team.json')), \
             patch.object(api, 'read_json', return_value={'projects': {'demo': {'repo': '/safe/repo', 'base_ref': 'main'}}}), \
             patch.object(api, 'inspect_project_context', return_value={
                 'project_id': 'demo', 'revision': 'abc123', 'context_file': '.agents/project.json',
                 'context_hash': 'hash', 'ready': True, 'manifest': {'schema_version': 1, 'name': 'Demo',
                     'summary': 'A project', 'stack': ['Go'], 'documents': [], 'components': [],
                     'external_sources': [], 'secret': 'strip'},
                 'sources': [{'path': 'README.md', 'sha256': 'digest', 'scope': 'project',
                              'absolute_path': '/private/repo/README.md'}],
                 'warnings': [], 'errors': [], 'pack_path': '/private/pack.json',
             }) as inspect:
            response = self.client.get('/api/plugins/engineering-team/projects/demo/context')
        self.assertEqual(response.status_code, 200)
        inspect.assert_called_once_with('demo', {'repo': '/safe/repo', 'base_ref': 'main'})
        body = response.json()
        self.assertTrue(body['ready'])
        self.assertEqual(body['manifest']['name'], 'Demo')
        self.assertNotIn('pack_path', body)
        self.assertNotIn('secret', body['manifest'])
        self.assertNotIn('absolute_path', body['sources'][0])
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_project_context_rejects_unknown_and_unsafe_ids_without_inspecting_paths(self):
        with patch.object(api, 'config_path', return_value=Path('/configured/team.json')), \
             patch.object(api, 'read_json', return_value={'projects': {}}), \
             patch.object(api, 'inspect_project_context') as inspect:
            self.assertEqual(self.client.get('/api/plugins/engineering-team/projects/unknown/context').status_code, 404)
            self.assertEqual(self.client.get('/api/plugins/engineering-team/projects/%2e%2e/context').status_code, 404)
        inspect.assert_not_called()

    def test_status_distinguishes_quota_wait_from_unarmed_work_and_filters_worker_body(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / 'runs' / 'feature'
            run.mkdir(parents=True)
            (run / 'state.json').write_text(json.dumps({
                'id': 'feature', 'title': 'Feature', 'stage': 'implementation', 'index': 0,
                'items': [{}, {}], 'waiting_for_quota': ['t_wait'], 'updated_at': 123,
                'project': 'demo',
                'project_context': {'project_id': 'demo', 'revision': 'rev', 'context_hash': 'hash',
                    'ready': True, 'manifest': {'schema_version': 1, 'name': 'Demo', 'stack': ['Go'],
                        'documents': [], 'components': [], 'external_sources': []},
                    'sources': [], 'warnings': [], 'errors': [], 'manifest_path': '/private/path'},
                'task_contexts': {'t_wait': {'project_id': 'demo', 'revision': 'rev', 'context_hash': 'taskhash',
                    'ready': True, 'manifest': {'schema_version': 1, 'name': 'Demo task', 'stack': [],
                        'documents': [], 'components': [], 'external_sources': []},
                    'sources': [], 'warnings': [], 'errors': [], 'pack_path': '/private/pack'}},
                'dispatch': {'t_wait': {'armed': True, 'quota_reason': 'reserve'},
                             't_later': {'armed': False}},
            }))
            class Board:
                def list_tasks(self, **kwargs):
                    return [
                        {'id': 't_wait', 'assignee': 'team-go', 'status': 'blocked', 'title': 'Wait', 'body': 'PRIVATE'},
                        {'id': 't_later', 'assignee': 'team-go', 'status': 'blocked', 'title': 'Later'},
                        {'id': 't_root', 'assignee': 'team-controller', 'status': 'running', 'title': 'Root'},
                    ]
            result = api.build_status({'data_root': directory, 'roles': {'team-go': {}}, 'board': 'engineering-team'},
                                      board=Board(), catalog={'team-go': {'label': 'Go-разработчик'}})
            self.assertEqual([t['status'] for t in result['tasks']], ['waiting_quota', 'planned'])
            self.assertEqual(result['roles'][0]['status'], 'waiting_quota')
            self.assertEqual(result['roles'][0]['label'], 'Go-разработчик')
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertEqual(result['tasks'][0]['project_context']['manifest']['name'], 'Demo task')
            self.assertNotIn('/private', json.dumps(result))
            self.assertEqual(result['features'][0]['total_tasks'], 2)
            self.assertEqual(result['features'][0]['project_id'], 'demo')

    def test_status_exposes_separate_test_wait_with_safe_fields_and_role_status(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / 'runs' / 'feature'
            run.mkdir(parents=True)
            (run / 'state.json').write_text(json.dumps({
                'id': 'feature', 'title': 'Feature', 'stage': 'implementation',
                'items': [{'card': 't_test'}, {'card': 't_accounting'}], 'waiting_for_tests': ['t_test'],
                'test_waits': {'t_test': {'kind': 'backend', 'job_id': 'safe-job-id',
                    'sha': 'a' * 40, 'status': 'waiting', 'started_at': 100,
                    'updated_at': 150, 'private_command': 'do-not-return'},
                    't_accounting': {'kind': 'backend', 'job_id': 'safe-accounting-id',
                    'sha': 'b' * 40, 'status': 'accounting_pending', 'started_at': 151,
                    'updated_at': 152}},
                'dispatch': {'t_test': {'armed': True}}, 'updated_at': 150,
            }))
            class Board:
                def list_tasks(self, **kwargs):
                    return [{'id': 't_test', 'assignee': 'team-go', 'status': 'blocked',
                             'title': 'Wait for trusted test'},
                            {'id': 't_accounting', 'assignee': 'team-frontend', 'status': 'blocked',
                             'title': 'Wait for result accounting'}]
            result = api.build_status({'data_root': directory, 'roles': {'team-go': {}},
                                       'board': 'engineering-team'}, board=Board())
            by_id = {task['id']: task for task in result['tasks']}
            self.assertEqual(by_id['t_test']['status'], 'waiting_tests')
            self.assertEqual(by_id['t_test']['test_wait']['job_status'], 'waiting')
            self.assertEqual(by_id['t_accounting']['status'], 'waiting_tests')
            self.assertEqual(by_id['t_accounting']['test_wait']['job_status'], 'accounting_pending')
            self.assertEqual({role['status'] for role in result['roles']}, {'waiting_tests'})
            self.assertNotIn('private_command', json.dumps(result))

    def test_status_exposes_current_regression_progress_without_commands_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / 'runs' / 'feature'
            run.mkdir(parents=True)
            path = run / 'state.json'
            state = {'id': 'feature', 'stage': 'integration_review', 'items': [],
                     'integration_check': {'event': 'heartbeat', 'index': 2, 'total': 5,
                         'elapsed_seconds': 28.5, 'argv': ['PRIVATE_COMMAND'],
                         'output_tail': 'PRIVATE_OUTPUT'}}
            path.write_text(json.dumps(state))
            class Board:
                def list_tasks(self, **kwargs):
                    return []
            config = {'data_root': directory, 'roles': {}, 'board': 'engineering-team'}
            result = api.build_status(config, board=Board())
            self.assertEqual(result['features'][0]['integration_check'], {
                'event': 'heartbeat', 'index': 2, 'total': 5, 'elapsed_seconds': 28.5})
            self.assertNotIn('PRIVATE', json.dumps(result))
            state['stage'] = 'done'
            path.write_text(json.dumps(state))
            self.assertIsNone(api.build_status(config, board=Board())['features'][0]['integration_check'])

    def test_status_projects_provisional_and_batch_regression_without_git_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / 'runs' / 'feature'
            run.mkdir(parents=True)
            (run / 'state.json').write_text(json.dumps({
                'id': 'feature', 'title': 'Feature', 'stage': 'integration_review',
                'items': [{'status': 'provisional'}, {'status': 'implementation', 'batch_repair': True}],
                'pending_regression_items': ['T-001'], 'staging_sha': 'abc123',
                'accepted_regression_sha': 'base123',
                'regression_batch': {'id': 4, 'head_sha': 'abc123', 'status': 'running'},
            }))
            class Board:
                def list_tasks(self, **kwargs):
                    return []
            config = {'data_root': directory, 'roles': {}, 'board': 'engineering-team'}
            with patch('team_engine.efficiency._git_diff', side_effect=AssertionError('status must not run Git')):
                result = api.build_status(config, board=Board())
            feature = result['features'][0]
            self.assertEqual(feature['verification_progress']['provisional_task_count'], 1)
            self.assertEqual(feature['verification_progress']['regression_status'], 'running')
            self.assertEqual(feature['verification_progress']['staging_sha'], 'abc123')
            self.assertEqual(feature['verification_progress']['accepted_regression_sha'], 'base123')
            self.assertEqual(feature['activity_stage'], 'batch_repair')
            self.assertFalse(feature['verification_progress']['final_acceptance_ready'])

    def test_efficiency_route_is_lazy_and_returns_no_store_json(self):
        payload = {'schema_version': 1, 'workers': {'wall_seconds_sum': 12}}
        with patch.object(api, '_workflow_call', return_value=payload) as workflow:
            response = self.client.get('/api/plugins/engineering-team/requests/request-123/efficiency')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        workflow.assert_called_once_with('get_efficiency_report', 'request-123')


if __name__ == '__main__':
    unittest.main()
