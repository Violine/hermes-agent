"""The owner-facing transport must not accept invented identities or bypass CSRF."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from team_engine import dashboard_api as api


class Invalid(ValueError):
    pass


class Conflict(ValueError):
    pass


class Missing(ValueError):
    pass


class WorkflowApiTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(api.router, prefix='/api/plugins/engineering-team')
        self.client = TestClient(app)
        self.base = '/api/plugins/engineering-team/requests'
        self.headers = {'Origin': 'http://testserver', 'X-Team-CSRF': api.CSRF_TOKEN}
        self.feature = {'id': 'req-example', 'stage': 'queued', 'approval': None}
        self.service = SimpleNamespace(
            WorkflowError=Invalid, WorkflowConflict=Conflict, WorkflowNotFound=Missing,
            submit_request=Mock(return_value=self.feature),
            list_requests=Mock(return_value=[self.feature]),
            get_request=Mock(return_value=self.feature),
            decide_request=Mock(return_value=self.feature),
            add_comment=Mock(return_value=self.feature))
        self.patch = patch.object(api, '_workflow_service', return_value=self.service)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_submit_pins_owner_identity_and_rejects_untrusted_fields(self):
        payload = {'project': 'demo', 'title': 'Task', 'brief': 'What to build',
                   'request_id': 'request-12345'}
        response = self.client.post(self.base, json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        kwargs = self.service.submit_request.call_args.kwargs
        self.assertEqual(kwargs['actor'], 'dashboard:owner')
        self.assertEqual(kwargs['channel'], 'dashboard')
        for key in ('actor', 'channel', 'origin', 'require_approvals', 'repo'):
            with self.subTest(key=key):
                response = self.client.post(self.base, json=dict(payload, **{key: 'injected'}), headers=self.headers)
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.service.submit_request.call_count, 1)

    def test_all_writes_require_csrf_and_same_origin(self):
        cases = [(self.base, {'project': 'demo', 'title': 'T', 'brief': 'B', 'request_id': 'request-12345'}),
                 (self.base + '/req-example/decision', {'gate_id': 'gate', 'decision': 'approve', 'request_id': 'request-12345'}),
                 (self.base + '/req-example/comments', {'comment': 'Hi', 'request_id': 'request-12345'})]
        for url, body in cases:
            for headers in ({}, {'Origin': 'http://evil.test', 'X-Team-CSRF': api.CSRF_TOKEN}):
                self.assertEqual(self.client.post(url, json=body, headers=headers).status_code, 403)
        for name in ('submit_request', 'decide_request', 'add_comment'):
            getattr(self.service, name).assert_not_called()

    def test_decision_rejects_empty_feedback_and_forwards_gate_id(self):
        url = self.base + '/req-example/decision'
        body = {'gate_id': 'exact-reviewed-version', 'decision': 'revise', 'request_id': 'request-12345'}
        self.assertEqual(self.client.post(url, json=body, headers=self.headers).status_code, 422)
        body.update(comment='Нужна проверка прав доступа')
        self.assertEqual(self.client.post(url, json=body, headers=self.headers).status_code, 200)
        self.assertEqual(self.service.decide_request.call_args.kwargs['gate_id'], 'exact-reviewed-version')
        self.assertEqual(self.service.decide_request.call_args.kwargs['decision'], 'revise')

    def test_stale_unknown_and_service_failure_have_distinct_responses(self):
        for error, expected in [(Conflict('Версия устарела'), 409), (Missing('Не найдено'), 404),
                                (Invalid('Некорректно'), 422), (OSError('private path'), 503)]:
            self.service.get_request.side_effect = error
            response = self.client.get(self.base + '/req-example')
            self.assertEqual(response.status_code, expected)
            self.assertNotIn('private path', response.text)

    def test_read_responses_do_not_cache_approvals(self):
        for url in (self.base, self.base + '/req-example'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_malformed_or_oversized_input_never_calls_service(self):
        for body in ('not json', '[]', '{"request_id":4}'):
            response = self.client.post(self.base, content=body,
                                        headers=dict(self.headers, **{'Content-Type': 'application/json'}))
            self.assertIn(response.status_code, (400, 422))
        response = self.client.post(self.base, content='x' * (api.MAX_BODY + 1),
                                    headers=dict(self.headers, **{'Content-Type': 'application/json'}))
        self.assertEqual(response.status_code, 413)
        self.service.submit_request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
