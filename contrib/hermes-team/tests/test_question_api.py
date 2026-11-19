"""Owner answers carry only explicit decisions, never a client-selected identity."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from team_engine import dashboard_api as api, human_workflow as workflow

class QuestionApiTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(api.router, prefix='/api/plugins/engineering-team')
        self.client = TestClient(app)
        self.url = '/api/plugins/engineering-team/requests/req-example/answers'
        self.headers = {'Origin': 'http://testserver', 'X-Team-CSRF': api.CSRF_TOKEN}
        self.body = {'gate_id': 'current-gate', 'question_id': 'first-call',
                     'option_id': 'no-deadline', 'request_id': 'answer-12345678'}

    def test_no_write_without_csrf_and_same_origin(self):
        with patch.object(api, '_workflow_call') as call:
            for headers in ({}, {'Origin': 'http://elsewhere', 'X-Team-CSRF': api.CSRF_TOKEN}):
                self.assertEqual(self.client.post(self.url, json=self.body, headers=headers).status_code, 403)
            call.assert_not_called()

    def test_choice_and_free_text_bind_gate_question_and_authenticated_actor(self):
        with patch.object(api, '_workflow_call', return_value={'id': 'req-example'}) as call:
            for body in (self.body, {k:v for k,v in dict(self.body, text='До завтра').items() if k != 'option_id'}):
                result = self.client.post(self.url, json=body, headers=self.headers)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json(), {'id': 'req-example'})
                self.assertEqual(result.headers['cache-control'], 'no-store')
                self.assertEqual(call.call_args.args, ('answer_question', 'req-example'))
                self.assertEqual(call.call_args.kwargs, dict(body, actor='dashboard:owner', channel='dashboard'))

    def test_exactly_one_answer_kind_and_no_injected_fields(self):
        with patch.object(api, '_workflow_call') as call:
            bad = [dict(self.body, text='Другой ответ'), dict(self.body, option_id=''),
                   dict(self.body, actor='another-owner'), dict(self.body, question_id=['invalid'])]
            for body in bad:
                self.assertEqual(self.client.post(self.url, json=body, headers=self.headers).status_code, 422)
            call.assert_not_called()

    def test_stale_question_maps_to_409_not_an_approval(self):
        with patch.object(workflow, 'answer_question', create=True,
                          side_effect=workflow.WorkflowConflict('Вопрос уже изменился')):
            result = self.client.post(self.url, json=self.body, headers=self.headers)
            self.assertEqual(result.status_code, 409)
            self.assertIn('изменился', result.json()['detail'])

if __name__ == '__main__':
    unittest.main()
