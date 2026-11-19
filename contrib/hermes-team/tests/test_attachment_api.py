"""Attachment transport boundaries: authenticated upload, typed IDs and downloads."""
import base64
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from team_engine import dashboard_api as api
from team_engine import human_workflow as workflow


class AttachmentApiTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(api.router, prefix='/api/plugins/engineering-team')
        self.client = TestClient(app)
        self.base = '/api/plugins/engineering-team'
        self.headers = {'Origin': 'http://testserver', 'X-Team-CSRF': api.CSRF_TOKEN}
        self.body = {'name': 'specs/Требования.md', 'content_base64': base64.b64encode(b'# Scope').decode(),
                     'request_id': 'upload-12345678'}

    def test_upload_same_origin_csrf_required_before_storage(self):
        with patch('team_engine.attachments.upload_attachment') as upload:
            for headers in ({}, {'Origin': 'http://other', 'X-Team-CSRF': api.CSRF_TOKEN},
                            dict(self.headers, **{'X-Team-CSRF': 'stale'})):
                self.assertEqual(self.client.post(self.base + '/attachments', json=self.body,
                                                 headers=headers).status_code, 403)
            upload.assert_not_called()

    def test_upload_decodes_binary_and_pins_owner(self):
        with patch('team_engine.attachments.upload_attachment', return_value={'id': 'att-abc'}) as upload:
            response = self.client.post(self.base + '/attachments', json=self.body, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(upload.call_args.kwargs['content'], b'# Scope')
            self.assertEqual(upload.call_args.kwargs['name'], 'specs/Требования.md')
            self.assertEqual(upload.call_args.kwargs['actor'], 'dashboard:owner')
            self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_bad_encoding_oversize_or_untrusted_fields_never_reach_storage(self):
        with patch('team_engine.attachments.upload_attachment') as upload:
            for extra in ({'content_base64': 'not::base64'}, {'actor': 'invented'},
                          {'name': ['not a string']}):
                result = self.client.post(self.base + '/attachments', json=dict(self.body, **extra), headers=self.headers)
                self.assertEqual(result.status_code, 422)
            with patch.object(api, 'MAX_UPLOAD_BODY', 64):
                result = self.client.post(self.base + '/attachments', json=self.body, headers=self.headers)
                self.assertEqual(result.status_code, 413)
            upload.assert_not_called()

    def test_create_and_add_accept_only_list_of_string_ids(self):
        payload = {'project': 'demo', 'title': 'T', 'brief': 'Read docs', 'request_id': 'submit-12345678',
                   'attachments': ['att-one', 'att-two']}
        with patch.object(api, '_workflow_call', return_value={'id': 'req-example'}) as call:
            self.assertEqual(self.client.post(self.base + '/requests', json=payload, headers=self.headers).status_code, 200)
            self.assertEqual(call.call_args.kwargs['attachments'], ['att-one', 'att-two'])
            for wrong in ('att-one', [{'path': '/etc/passwd'}], ['a'] * 51):
                self.assertEqual(self.client.post(self.base + '/requests', json=dict(payload, attachments=wrong), headers=self.headers).status_code, 422)
            result = self.client.post(self.base + '/requests/req-example/attachments',
                json={'attachment_ids': ['att-new'], 'request_id': 'attach-12345678'}, headers=self.headers)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(call.call_args.args, ('attach_documents', 'req-example'))
            self.assertEqual(call.call_args.kwargs['attachment_ids'], ['att-new'])

    def test_download_forces_attachment_and_never_renders_html(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'original'
            path.write_text('<script>alert(1)</script>')
            with patch.object(api, '_workflow_call', return_value=(path, {'name': 'specs/пример.html'})) as call:
                response = self.client.get(self.base + '/requests/req-example/attachments/att-one')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, '<script>alert(1)</script>')
                self.assertTrue(response.headers['content-disposition'].startswith('attachment;'))
                self.assertEqual(response.headers['content-type'], 'application/octet-stream')
                self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
                self.assertIn('sandbox', response.headers['content-security-policy'])
                call.assert_called_once_with('request_attachment_path', 'req-example', 'att-one')

    def test_request_bound_download_maps_missing_to_404(self):
        with patch.object(workflow, 'request_attachment_path', side_effect=workflow.WorkflowNotFound('Документ не найден')):
            response = self.client.get(self.base + '/requests/req-example/attachments/att-unrelated')
            self.assertEqual(response.status_code, 404)

if __name__ == '__main__':
    unittest.main()
