import json
import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from team_engine.backend_test_jobs import (
    bridge_argv, execute_job, read_job, result, status, submit, submit_job,
    validate, _mark_interrupted,
)


class BackendTestJobsTests(unittest.TestCase):
    def setUp(self):
        self.payload = {'request': 'req-0557438ef182e8847164', 'task': 'T-001',
                        'sha': 'eafea661fb47f535b9400713d15cc457409b02ad',
                        'scope': 'salesleads'}

    def test_rejects_shell_text_arbitrary_paths_and_extra_fields(self):
        for key, value in [('request', '../credentials'), ('task', 'T-001; id'),
                           ('sha', 'HEAD'), ('scope', '--init-script=/tmp/evil')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate({**self.payload, key: value})
        with self.assertRaises(ValueError):
            validate({**self.payload, 'command': 'true'})

    def test_submit_is_durable_and_idempotent_across_run_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = submit_job(self.payload, queue=root)
            repeat = submit_job(self.payload, queue=root)
            legacy = submit(self.payload, queue=root, run_id='different-native-run')
            self.assertEqual(first['job_id'], repeat['job_id'])
            self.assertEqual(first['job_id'], legacy.name)
            self.assertEqual(first['status'], 'queued')
            self.assertEqual(first['submitted_at'], status(first['job_id'], queue=root)['submitted_at'])
            saved = json.loads((root / first['job_id'] / 'job.json').read_text())
            self.assertEqual(saved['payload'], self.payload)

    def test_explicit_rerun_gets_new_immutable_job_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = submit_job(self.payload, queue=root)
            job_dir = root / first['job_id']
            old_request = (job_dir / 'request.json').read_bytes()
            old_status = (job_dir / 'status.json').read_bytes()
            state = json.loads(old_status)
            (job_dir / 'status.json').write_text(json.dumps({**state, 'status': 'completed',
                                                              'exit_code': 0, 'finished_at': 10}))
            second = submit_job(self.payload, queue=root, rerun=True)
            self.assertNotEqual(first['job_id'], second['job_id'])
            self.assertEqual(second['attempt'], 2)
            self.assertEqual(old_request, (job_dir / 'request.json').read_bytes())
            self.assertEqual('completed', json.loads((job_dir / 'status.json').read_text())['status'])

    def test_rerun_is_rejected_while_identical_job_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            submitted = submit_job(self.payload, queue=root)
            with self.assertRaisesRegex(ValueError, 'active'):
                submit_job(self.payload, queue=root, rerun=True)
            self.assertEqual(submitted['job_id'], status(submitted['job_id'], queue=root)['job_id'])

    def test_symlinked_queue_job_manifest_and_output_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / 'real'
            real.mkdir()
            (root / 'alias').symlink_to(real, target_is_directory=True)
            with self.assertRaises(ValueError):
                submit_job(self.payload, queue=root / 'alias')
            created = submit_job(self.payload, queue=real)
            job_dir = real / created['job_id']
            manifest = job_dir / 'job.json'
            manifest.unlink()
            outside = root / 'outside.json'
            outside.write_text(json.dumps({'job_id': created['job_id'], 'payload': self.payload,
                                           'attempt': 1}))
            manifest.symlink_to(outside)
            with self.assertRaises(ValueError):
                status(created['job_id'], queue=real)

    def test_bounded_tail_and_log_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            created = submit_job(self.payload, queue=root)
            log = root / created['job_id'] / 'output.log'
            log.write_bytes(b'a' * 20000)
            snapshot = read_job(created['job_id'], queue=root, tail_bytes=512)
            self.assertEqual(len(snapshot['log_tail'].encode()), 512)
            log.unlink()
            outside = root / 'outside.log'
            outside.write_text('secret-ish')
            log.symlink_to(outside)
            with self.assertRaises(ValueError):
                read_job(created['job_id'], queue=root)

    def test_real_bridge_exit_and_sanitized_environment_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = root/'bridge.py'
            bridge.write_text('import json,os,sys\n'
                              'print(json.dumps({"args":sys.argv[1:],"secret_present":"ANTHROPIC_API_KEY" in os.environ}),flush=True)\n'
                              'raise SystemExit(13)\n')
            submitted = submit_job(self.payload, queue=root/'queue')
            job = root/'queue'/submitted['job_id']
            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-sentinel'}):
                self.assertEqual(execute_job(job, bridge=bridge, timeout=10,
                    lock_path=root/'heavy.lock'), 13)
            snapshot = result(submitted['job_id'], queue=root/'queue')
            output = json.loads(snapshot['log_tail'])
            self.assertEqual(snapshot['status'], 'completed')
            self.assertEqual(snapshot['exit_code'], 13)
            self.assertTrue(snapshot['result_ready'])
            self.assertFalse(output['secret_present'])
            self.assertEqual(output['args'], bridge_argv(self.payload, bridge=bridge)[2:])

    def test_queue_wait_is_bounded_and_not_confused_with_model_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            submitted = submit_job(self.payload, queue=root)
            job = root / submitted['job_id']
            state = json.loads((job/'status.json').read_text())
            self.assertGreater(state['queue_deadline'], state['submitted_at'])
            self.assertNotIn('model_runtime_seconds', state)

    def test_submit_status_result_cli_return_single_json_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            from team_engine import backend_test_jobs
            output = io.StringIO()
            argv = ['backend_test_jobs', 'submit', '--request', self.payload['request'],
                    '--task', self.payload['task'], '--sha', self.payload['sha'],
                    '--scope', self.payload['scope']]
            with patch.object(backend_test_jobs, 'QUEUE', Path(tmp)), \
                    patch('sys.argv', argv), redirect_stdout(output):
                self.assertEqual(backend_test_jobs.main(), 0)
            submitted = json.loads(output.getvalue())
            self.assertEqual(submitted['status'], 'queued')
            self.assertEqual(output.getvalue().count('\n'), 1)
            output.seek(0)
            output.truncate(0)
            with patch.object(backend_test_jobs, 'QUEUE', Path(tmp)), \
                    patch('sys.argv', ['backend_test_jobs', 'status', '--job-id', submitted['job_id']]), \
                    redirect_stdout(output):
                self.assertEqual(backend_test_jobs.main(), 0)
            self.assertEqual(json.loads(output.getvalue())['job_id'], submitted['job_id'])
            output.seek(0)
            output.truncate(0)
            with patch.object(backend_test_jobs, 'QUEUE', Path(tmp)), \
                    patch('sys.argv', ['backend_test_jobs', 'result', '--job-id', submitted['job_id']]), \
                    redirect_stdout(output):
                self.assertEqual(backend_test_jobs.main(), 0)
            self.assertFalse(json.loads(output.getvalue())['result_ready'])

    def test_worker_restart_marks_running_job_terminal_and_rerun_can_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            submitted = submit_job(self.payload, queue=root)
            job = root / submitted['job_id']
            state = json.loads((job/'status.json').read_text())
            (job/'status.json').write_text(json.dumps({**state, 'status': 'running'}))
            _mark_interrupted(job)
            terminal = status(submitted['job_id'], queue=root)
            self.assertEqual(terminal['status'], 'interrupted')
            self.assertIn('finished_at', terminal)

    def test_invalid_manifest_does_not_execute_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            submitted = submit_job(self.payload, queue=root/'queue')
            job = root/'queue'/submitted['job_id']
            (job/'request.json').write_text(json.dumps({**self.payload, 'sha': 'HEAD'}))
            with self.assertRaises(ValueError):
                execute_job(job, bridge=root/'not-an-executable.py', lock_path=root/'heavy.lock')
            self.assertFalse((job/'output.log').exists())


if __name__ == '__main__':
    unittest.main()
