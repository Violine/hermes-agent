import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import team_engine.check_jobs as jobs
from team_engine.check_jobs import (execute_job, read_job, submit, worker)


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class FakeBoard:
    def __init__(self, card='t_12345678', role='backend', run='77', status='running'):
        self.task = {'id': card, 'assignee': 'team-' + role, 'status': status,
                     'current_run_id': run}
        self.runs = [{'id': run, 'profile': 'team-' + role, 'status': status}]

    def get_task(self, _card):
        return dict(self.task)

    def get_runs(self, _card):
        return [dict(row) for row in self.runs]


class CheckJobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        git(self.repo, 'config', 'user.name', 'Test')
        (self.repo / 'source.txt').write_text('base\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-qm', 'base')
        self.sha = git(self.repo, 'rev-parse', 'HEAD')
        self.candidate = self.root / 'candidate'
        git(self.repo, 'worktree', 'add', '--detach', str(self.candidate), self.sha)
        self.card = 't_12345678'
        self.qa_card = 't_87654321'
        self.run = '77'
        self.request = 'req-0557438ef182e8847164'
        self.state_root = self.root / 'runs'
        feature_dir = self.state_root / self.request
        feature_dir.mkdir(parents=True)
        self.queue = self.root / 'queue'
        self.lock = self.root / 'heavy.lock'
        self.events = self.root / 'events.txt'
        checks = [
            {'name': 'first', 'argv': [sys.executable, '-c',
                f"from pathlib import Path; Path({str(self.events)!r}).open('a').write('first\\n'); raise SystemExit(9)"]},
            {'name': 'second', 'argv': [sys.executable, '-c',
                f"from pathlib import Path; Path({str(self.events)!r}).open('a').write('second\\n')"]},
        ]
        state = {'id': self.request, 'stage': 'implementation', 'repo': str(self.repo),
                 'project_contract': {'checks': checks},
                 'items': [{'definition': {'id': 'T-001', 'assignee_role': 'backend'},
                            'status': 'implementation', 'card': self.card,
                            'head_sha': self.sha, 'workspace': str(self.candidate),
                            'qa_card': self.qa_card, 'qa_workspace': str(self.candidate)}]}
        (feature_dir / 'state.json').write_text(json.dumps(state))
        self.board = FakeBoard()

    def tearDown(self):
        self.tmp.cleanup()

    def submit(self, **overrides):
        values = {'request': self.request, 'task': 'T-001', 'card': self.card,
                  'run': self.run, 'sha': self.sha, 'checks': [0, 1],
                  'state_root': self.state_root, 'board': self.board,
                  'queue': self.queue}
        values.update(overrides)
        return submit(**values)

    def test_executes_each_pinned_check_on_private_sha_and_preserves_exit_codes(self):
        snapshot = self.submit()
        self.assertEqual(snapshot['status'], 'queued')
        job_id = snapshot['job_id']
        self.assertEqual(execute_job(job_id, queue=self.queue, lock_path=self.lock), 9)
        final = read_job(job_id, queue=self.queue)
        self.assertEqual(final['status'], 'completed')
        self.assertEqual([row['exit_code'] for row in final['results']], [9, 0])
        self.assertEqual([row['index'] for row in final['results']], [0, 1])
        self.assertTrue(all(row['argv_sha256'] for row in final['results']))
        self.assertEqual(self.events.read_text(), 'first\nsecond\n')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.sha)
        self.assertFalse((self.queue / job_id / 'worktree').exists())

    def test_duplicate_submit_reuses_same_immutable_job(self):
        first = self.submit()
        again = self.submit()
        self.assertEqual(first['job_id'], again['job_id'])
        with self.assertRaisesRegex(ValueError, 'already active'):
            self.submit(rerun=True)
        self.assertEqual(execute_job(first['job_id'], queue=self.queue, lock_path=self.lock), 9)
        repeated = self.submit(rerun=True)
        self.assertNotEqual(first['job_id'], repeated['job_id'])
        self.assertEqual(len(list(self.queue.glob('*/job.json'))), 2)

    def test_read_job_bounds_log_tail_and_rejects_symlink_queue(self):
        snapshot = self.submit()
        with self.assertRaisesRegex(ValueError, 'tail_bytes'):
            read_job(snapshot['job_id'], queue=self.queue, tail_bytes=jobs.TAIL_BYTES + 1)
        alias = self.root / 'queue-read-alias'
        alias.symlink_to(self.queue, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            read_job(snapshot['job_id'], queue=alias)

    def test_rejects_wrong_card_run_role_or_candidate_sha(self):
        with self.assertRaisesRegex(ValueError, 'card binding'):
            self.submit(card='t_00000000')
        self.board.task['current_run_id'] = '78'
        with self.assertRaisesRegex(ValueError, 'worker run'):
            self.submit()
        self.board.task['current_run_id'] = self.run
        self.board.runs[0]['profile'] = 'team-frontend'
        with self.assertRaisesRegex(ValueError, 'latest active native worker run'):
            self.submit()
        self.board.runs[0]['profile'] = 'team-backend'
        with self.assertRaisesRegex(ValueError, 'Candidate SHA'):
            self.submit(sha='a' * 40)

    def test_qa_worker_can_submit_only_from_its_own_active_card_and_worktree(self):
        state_path = self.state_root / self.request / 'state.json'
        state = json.loads(state_path.read_text())
        state['items'][0]['status'] = 'verification'
        state_path.write_text(json.dumps(state))
        board = FakeBoard(card=self.qa_card, role='qa', run=self.run)
        snapshot = self.submit(card=self.qa_card, board=board, checks=[1])
        self.assertEqual(snapshot['role'], 'qa')
        self.assertEqual(execute_job(snapshot['job_id'], queue=self.queue, lock_path=self.lock), 0)
        result = read_job(snapshot['job_id'], queue=self.queue)
        self.assertEqual(result['checks'], [1])
        self.assertEqual([row['index'] for row in result['results']], [1])
        self.assertEqual(self.events.read_text(), 'second\n')

    def test_reviewer_and_integrator_bind_to_their_own_workspaces(self):
        cases = [('reviewer_card', 'reviewer_workspace', 't_11111111',
                  'reviewer', 'verification'),
                 ('integrator_card', 'integrator_workspace', 't_22222222',
                  'integrator', 'integration_review')]
        for card_field, workspace_field, card, role, item_status in cases:
            with self.subTest(role=role):
                state_path = self.state_root / self.request / 'state.json'
                state = json.loads(state_path.read_text())
                item = state['items'][0]
                item['status'] = item_status
                item[card_field] = card
                item[workspace_field] = str(self.candidate)
                state_path.write_text(json.dumps(state))
                board = FakeBoard(card=card, role=role, run=self.run)
                snapshot = self.submit(card=card, board=board, checks=[1])
                self.assertEqual(snapshot['role'], role)
                job = json.loads((self.queue / snapshot['job_id'] / 'job.json').read_text())
                self.assertEqual(job['binding']['role'], role)

    def test_rejects_symlinked_state_and_queue_directories(self):
        alias = self.root / 'state-alias'
        alias.symlink_to(self.state_root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.submit(state_root=alias)
        queue_alias = self.root / 'queue-alias'
        queue_alias.symlink_to(self.queue, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            self.submit(queue=queue_alias)

    def test_feature_state_has_larger_bounded_limit_than_queue_json(self):
        state_path = self.state_root / self.request / 'state.json'
        state = json.loads(state_path.read_text())
        state['accumulated_evidence'] = 'x' * (jobs.JSON_LIMIT + 1)
        state_path.write_text(json.dumps(state))
        self.assertGreater(state_path.stat().st_size, jobs.JSON_LIMIT)
        self.assertLessEqual(state_path.stat().st_size, jobs.FEATURE_STATE_LIMIT)
        self.assertEqual(jobs._feature_state(self.state_root, self.request)['id'],
                         self.request)

        state['accumulated_evidence'] = 'x' * jobs.FEATURE_STATE_LIMIT
        state_path.write_text(json.dumps(state))
        self.assertGreater(state_path.stat().st_size, jobs.FEATURE_STATE_LIMIT)
        with self.assertRaisesRegex(ValueError, 'JSON file'):
            jobs._feature_state(self.state_root, self.request)

    def test_untrusted_job_manifest_keeps_one_mib_limit(self):
        snapshot = self.submit()
        manifest_path = self.queue / snapshot['job_id'] / 'job.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['padding'] = 'x' * jobs.JSON_LIMIT
        manifest_path.write_text(json.dumps(manifest))
        self.assertGreater(manifest_path.stat().st_size, jobs.JSON_LIMIT)
        with self.assertRaisesRegex(ValueError, 'JSON file'):
            read_job(snapshot['job_id'], queue=self.queue)

    def test_rejects_symlinked_candidate_worktree(self):
        state_path = self.state_root / self.request / 'state.json'
        state = json.loads(state_path.read_text())
        real = state['items'][0]['workspace']
        state['items'][0]['workspace'] = str(self.root / 'candidate-link')
        (self.root / 'candidate-link').symlink_to(real, target_is_directory=True)
        state_path.write_text(json.dumps(state))
        with self.assertRaises((ValueError, OSError)):
            self.submit()

    def test_worker_restart_marks_inflight_job_interrupted_without_reexecution(self):
        snapshot = self.submit()
        job_dir = self.queue / snapshot['job_id']
        state = json.loads((job_dir / 'status.json').read_text())
        state.update(status='running', started_at=1)
        (job_dir / 'status.json').write_text(json.dumps(state))
        with patch('team_engine.check_jobs._proc_fingerprint', return_value={
                'pid': os.getpid(), 'start_time': 'test', 'cmdline_sha256': 'test'}), \
             patch('team_engine.check_jobs.time.sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                worker(queue=self.queue)
        result = read_job(snapshot['job_id'], queue=self.queue)
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['exit_code'], 125)
        self.assertFalse(self.events.exists())

    def test_timeout_while_waiting_for_heavy_lock_is_bounded_and_records_heartbeat(self):
        import fcntl
        snapshot = self.submit()
        self.lock.touch()
        with self.lock.open('a') as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            start = time.monotonic()
            code = execute_job(snapshot['job_id'], queue=self.queue, timeout=0.25,
                               lock_path=self.lock)
            elapsed = time.monotonic() - start
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        result = read_job(snapshot['job_id'], queue=self.queue)
        self.assertEqual(code, 124)
        self.assertLess(elapsed, 3)
        self.assertEqual([row['status'] for row in result['results']],
                         ['not_run_timeout', 'not_run_timeout'])
        self.assertGreater(result['updated_at'], result['started_at'])

    def test_check_timeout_terminates_and_reaps_its_process_group(self):
        state_path = self.state_root / self.request / 'state.json'
        state = json.loads(state_path.read_text())
        marker = self.root / 'child.pid'
        state['project_contract']['checks'] = [[sys.executable, '-c',
            f"import os,time; open({str(marker)!r},'w').write(str(os.getpid())); time.sleep(20)"]]
        state_path.write_text(json.dumps(state))
        snapshot = self.submit(checks=[0])
        code = execute_job(snapshot['job_id'], queue=self.queue, timeout=1.5,
                           lock_path=self.lock)
        result = read_job(snapshot['job_id'], queue=self.queue)
        self.assertEqual(code, 124)
        self.assertEqual(result['results'][0]['status'], 'timed_out')
        pid = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_worker_obeys_stop_request_and_removes_fingerprint_file(self):
        snapshot = self.submit()
        status_path = self.queue / snapshot['job_id'] / 'status.json'
        state = json.loads(status_path.read_text())
        state.update(status='running', started_at=time.time())
        status_path.write_text(json.dumps(state))
        pid = {'pid': os.getpid(), 'start_time': 'test', 'cmdline_sha256': 'test'}
        def stop_after_scan(_seconds):
            jobs._STOP_REQUESTED = True
        with patch('team_engine.check_jobs._proc_fingerprint', return_value=pid), \
             patch('team_engine.check_jobs.time.sleep', side_effect=stop_after_scan):
            worker(queue=self.queue)
        self.assertFalse((self.queue / 'worker.pid.json').exists())
        self.assertEqual(read_job(snapshot['job_id'], queue=self.queue)['status'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
