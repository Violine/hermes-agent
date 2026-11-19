"""Durable, identifier-only transport to the trusted backend test runner.

The public queue API is intentionally small: ``submit`` is non-blocking and
returns a stable job id; ``status`` and ``result`` return bounded JSON
snapshots. The legacy ``run`` command still submits and waits for completion.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

QUEUE = Path('/home/alex/.hermes/team/data/backend-test-jobs')
HEAVY_TEST_LOCK = Path('/home/alex/.hermes/team/data/.heavy-test.lock')
BRIDGE = Path('/home/alex/.local/share/hermes-test-runtime/tools/backend_test_bridge.py')
FIELDS = {'request', 'task', 'sha', 'scope'}
JOB_ID_RE = re.compile(r'[0-9a-f]{64}')
TERMINAL = {'completed', 'interrupted', 'rejected'}
LOG_TAIL_BYTES = 8192
JSON_LIMIT = 4096
MAX_ATTEMPT = 9999
QUEUE_WAIT_TIMEOUT = 6 * 60 * 60


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ValueError('A test job accepts only request, task, sha and scope')
    patterns = {'request': r'req-[0-9a-f]{20}', 'task': r'T-[0-9]{3}',
                'sha': r'[0-9a-f]{40}', 'scope': r'(salesleads|full)'}
    for key, pattern in patterns.items():
        if not isinstance(payload[key], str) or not re.fullmatch(pattern, payload[key]):
            raise ValueError(f'Invalid test job {key}')
    return dict(payload)


def identity_for(payload, attempt=1):
    """Stable identity across continuations; explicit reruns get new ids."""
    if not isinstance(attempt, int) or isinstance(attempt, bool) or not 1 <= attempt <= MAX_ATTEMPT:
        raise ValueError('Invalid test job attempt')
    canonical = json.dumps(validate(payload), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(f'{canonical}:attempt={attempt}'.encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Test queue files cannot be symlinks')
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def plain_directory(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError('Test queue directory must be a plain directory')
    return path


def ensure_queue(queue):
    queue = Path(queue)
    # Do not silently traverse a symlink at the queue root or any existing
    # component below the user's home. The configured production path is fixed.
    if queue.is_symlink():
        raise ValueError('Test queue directory cannot be a symlink')
    queue.mkdir(parents=True, exist_ok=True, mode=0o700)
    plain_directory(queue)
    return queue


@contextmanager
def submission_lock(queue):
    path = Path(queue) / '.submit.lock'
    if path.is_symlink():
        raise ValueError('Test submission lock cannot be a symlink')
    with path.open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _read_json(path, *, limit=JSON_LIMIT):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('Invalid test queue JSON file')
    return json.loads(path.read_text())


def _job_dir(queue, job_id):
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise ValueError('Invalid test job identity')
    job_dir = Path(queue) / job_id
    plain_directory(job_dir)
    manifest = _read_json(job_dir / 'job.json')
    payload = validate(manifest.get('payload'))
    attempt = manifest.get('attempt')
    expected_id = identity_for(payload, attempt)
    if manifest.get('job_id') != job_id or expected_id != job_id:
        raise ValueError('Test job identity does not match its request')
    return job_dir, payload, attempt


def _create_attempt(queue, payload, job_id, attempt):
    job_dir = Path(queue) / job_id
    job_dir.mkdir(mode=0o700)
    plain_directory(job_dir)
    submitted_at = time.time()
    atomic_json(job_dir / 'job.json', {'job_id': job_id, 'attempt': attempt,
                                       'payload': payload})
    atomic_json(job_dir / 'request.json', {
        'job_id': job_id, 'attempt': attempt, 'payload': payload})
    atomic_json(job_dir / 'status.json', {
        'job_id': job_id, 'attempt': attempt, 'status': 'queued', **payload,
        'submitted_at': submitted_at, 'queue_deadline': submitted_at + QUEUE_WAIT_TIMEOUT,
        'updated_at': submitted_at})
    return job_dir


def submit_job(payload, *, queue=QUEUE, rerun=False):
    payload = validate(payload)
    queue = ensure_queue(queue)
    with submission_lock(queue):
        matching = []
        for candidate in queue.iterdir():
            if not JOB_ID_RE.fullmatch(candidate.name) or candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                candidate_dir, candidate_payload, candidate_attempt = _job_dir(queue, candidate.name)
            except (ValueError, OSError, json.JSONDecodeError):
                continue
            if candidate_payload == payload:
                matching.append((candidate_attempt, candidate.name))
        for candidate_attempt, candidate_id in matching:
            candidate_state = _read_json(queue / candidate_id / 'status.json')
            if candidate_state.get('status') not in TERMINAL:
                if rerun:
                    raise ValueError('Cannot rerun a test job while an identical attempt is active')
                return _snapshot(queue, candidate_id)
        if matching and not rerun:
            attempt, job_id = min(matching)
            return _snapshot(queue, job_id)
        attempt = max((item[0] for item in matching), default=0) + (1 if rerun else 1)
        if attempt > MAX_ATTEMPT:
            raise ValueError('Test job rerun limit exceeded')
        job_id = identity_for(payload, attempt)
        job_dir = queue / job_id
        if job_dir.exists() or job_dir.is_symlink():
            # Idempotent explicit retry if caller retried after a lost response.
            existing_dir, existing_payload, existing_attempt = _job_dir(queue, job_id)
            if existing_payload != payload or existing_attempt != attempt:
                raise ValueError('Test job identity was reused')
        else:
            job_dir = _create_attempt(queue, payload, job_id, attempt)
    return _snapshot(queue, job_id)


def submit(payload, *, queue=QUEUE, run_id=None, rerun=False):
    """Compatibility helper returning the attempt directory.

    ``run_id`` is accepted for old callers but intentionally ignored: a model
    continuation must not create a second copy of the same test request.
    """
    _ = run_id
    result = submit_job(payload, queue=queue, rerun=rerun)
    return Path(queue) / result['job_id']


def _tail(path, limit=LOG_TAIL_BYTES):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Test output cannot be a symlink')
    if not path.exists():
        return ''
    if not path.is_file():
        raise ValueError('Test output must be a regular file')
    with path.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - limit))
        data = stream.read(limit)
    return data.decode('utf-8', 'replace')


def _snapshot(queue, job_id, *, include_tail=True, tail_bytes=LOG_TAIL_BYTES):
    job_dir, payload, attempt = _job_dir(queue, job_id)
    state = _read_json(job_dir / 'status.json')
    if state.get('job_id') != job_id or state.get('attempt') != attempt:
        raise ValueError('Test status identity does not match its request')
    snapshot = {key: state.get(key) for key in (
        'job_id', 'attempt', 'status', 'request', 'task', 'sha', 'scope',
        'submitted_at', 'queue_deadline', 'started_at', 'updated_at',
        'finished_at', 'exit_code', 'error')
        if key in state}
    snapshot.setdefault('job_id', job_id)
    snapshot.setdefault('request', payload['request'])
    snapshot.setdefault('task', payload['task'])
    snapshot.setdefault('sha', payload['sha'])
    snapshot.setdefault('scope', payload['scope'])
    if include_tail:
        snapshot['log_tail'] = _tail(job_dir / 'output.log', tail_bytes)
    return snapshot


def status(job_id, *, queue=QUEUE):
    return _snapshot(queue, job_id)


def read_job(job_id, *, queue=QUEUE, tail_bytes=LOG_TAIL_BYTES):
    """Controller-facing durable snapshot with a bounded log tail."""
    if not isinstance(tail_bytes, int) or isinstance(tail_bytes, bool) or not 0 <= tail_bytes <= LOG_TAIL_BYTES:
        raise ValueError('Invalid test log tail size')
    return _snapshot(queue, job_id, tail_bytes=tail_bytes)


def result(job_id, *, queue=QUEUE):
    snapshot = _snapshot(queue, job_id)
    if snapshot['status'] not in TERMINAL:
        snapshot['result_ready'] = False
    else:
        snapshot['result_ready'] = True
    return snapshot


def bridge_argv(payload, *, bridge=BRIDGE):
    payload = validate(payload)
    return [sys.executable, str(bridge),
            '--request', payload['request'], '--task', payload['task'],
            '--sha', payload['sha'], '--scope', payload['scope']]


@contextmanager
def heavy_test_lock(path=HEAVY_TEST_LOCK):
    """Serialize host-side backend jobs with controller-run test commands."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError('Heavy test lock cannot be a symlink')
    with path.open('a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def execute_job(directory, *, bridge=BRIDGE, timeout=3500, lock_path=HEAVY_TEST_LOCK):
    plain_directory(directory)
    request = directory / 'request.json'
    envelope = _read_json(request)
    payload = validate(envelope.get('payload'))
    job_id = identity_for(payload)
    attempt = envelope.get('attempt')
    if envelope.get('job_id') != job_id or not isinstance(attempt, int) or attempt < 1:
        raise ValueError('Invalid test job identity or attempt')
    manifest = _read_json(directory / 'job.json')
    if manifest.get('job_id') != job_id or manifest.get('attempt') != attempt or validate(manifest.get('payload')) != payload:
        raise ValueError('Test job manifest does not match request')
    status_path = directory / 'status.json'
    log_path = directory / 'output.log'
    if status_path.is_symlink() or log_path.is_symlink():
        raise ValueError('Test queue outputs cannot be symlinks')
    previous = _read_json(status_path)
    if previous.get('status') != 'queued':
        return int(previous.get('exit_code', 0))
    started = time.time()
    if previous.get('job_id') != job_id or previous.get('attempt') != attempt:
        raise ValueError('Test status identity does not match request')
    state = {**previous, 'status': 'running', 'started_at': started,
             'updated_at': started}
    atomic_json(status_path, state)
    process = None
    try:
        with heavy_test_lock(lock_path):
            with log_path.open('wb') as output:
                process = subprocess.Popen(bridge_argv(payload, bridge=bridge),
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env={'PATH': '/usr/bin:/bin', 'HOME': '/home/alex',
                         'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'})
                while process.poll() is None:
                    if time.time() - started > timeout:
                        raise TimeoutError('Backend test job exceeded its time limit')
                    state['updated_at'] = time.time()
                    atomic_json(status_path, state)
                    time.sleep(2)
                code = process.returncode
    except BaseException:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        atomic_json(status_path, {**state, 'status': 'interrupted', 'exit_code': 124,
                                  'finished_at': time.time(), 'updated_at': time.time()})
        raise
    atomic_json(status_path, {**state, 'status': 'completed', 'exit_code': code,
                              'finished_at': time.time(), 'updated_at': time.time()})
    return code


def _mark_interrupted(directory):
    status_path = directory / 'status.json'
    if status_path.is_symlink() or not status_path.is_file():
        return
    try:
        previous = _read_json(status_path)
    except (ValueError, OSError, json.JSONDecodeError):
        return
    if previous.get('status') == 'running':
        atomic_json(status_path, {**previous, 'status': 'interrupted',
                                 'exit_code': 125, 'finished_at': time.time(),
                                 'updated_at': time.time(),
                                 'error': 'Worker restarted while test was running'})


def worker(*, queue=QUEUE):
    queue = ensure_queue(queue)
    lock_path = queue / '.worker.lock'
    if lock_path.is_symlink():
        raise ValueError('Test worker lock cannot be a symlink')
    with lock_path.open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        for job_dir in queue.iterdir():
            if not JOB_ID_RE.fullmatch(job_dir.name) or job_dir.is_symlink() or not job_dir.is_dir():
                continue
            try:
                _mark_interrupted(job_dir)
            except (ValueError, OSError):
                continue
        while True:
            for job_dir in sorted(queue.iterdir()):
                if not JOB_ID_RE.fullmatch(job_dir.name) or job_dir.is_symlink() or not job_dir.is_dir():
                    continue
                try:
                    status_path = job_dir / 'status.json'
                    if status_path.is_symlink() or not status_path.is_file():
                        continue
                    state = _read_json(status_path)
                    if state.get('status') != 'queued':
                        continue
                    try:
                        if time.time() > float(state.get('queue_deadline', 0)):
                            atomic_json(status_path, {**state, 'status': 'rejected',
                                'exit_code': 124, 'error': 'Backend test job queue wait limit exceeded',
                                'finished_at': time.time(), 'updated_at': time.time()})
                        else:
                            execute_job(job_dir)
                    except (ValueError, OSError, TimeoutError) as exc:
                        atomic_json(status_path, {**state, 'status': 'rejected',
                            'exit_code': 2, 'error': str(exc),
                            'finished_at': time.time(), 'updated_at': time.time()})
                except (ValueError, OSError):
                    continue
            time.sleep(1)


def wait_job(directory, *, timeout=3500):
    directory = Path(directory)
    status_path = directory / 'status.json'
    started = time.monotonic()
    last_message = 0
    while time.monotonic() - started < timeout:
        state = _read_json(status_path)
        log_path = directory / 'output.log'
        if log_path.is_file() and not log_path.is_symlink():
            # Preserve old streaming behavior while keeping each read bounded.
            offset = getattr(wait_job, '_offset', {}).get(str(directory), 0)
            with log_path.open('rb') as stream:
                stream.seek(offset)
                data = stream.read(65536)
                offset += len(data)
            if not hasattr(wait_job, '_offset'):
                wait_job._offset = {}
            wait_job._offset[str(directory)] = offset
            if data:
                sys.stdout.write(data.decode('utf-8', 'replace'))
                sys.stdout.flush()
        if state.get('status') in TERMINAL:
            print('TEST_JOB_STATUS=' + str(status_path), flush=True)
            if state.get('error'):
                print(state['error'], file=sys.stderr)
            return int(state.get('exit_code', 2))
        if time.monotonic() - last_message >= 15:
            print(f"Backend test job {state.get('job_id','')[:12]}: queued/running; elapsed {int(time.monotonic()-started)}s", flush=True)
            last_message = time.monotonic()
        time.sleep(1)
    print('Waiting timed out; host job may still be running. Inspect ' + str(directory), file=sys.stderr)
    return 124


def _json_command(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('worker')
    run = sub.add_parser('run')
    for command in (run,):
        command.add_argument('--request', required=True)
        command.add_argument('--task', required=True)
        command.add_argument('--sha', required=True)
        command.add_argument('--scope', choices=['salesleads', 'full'], default='salesleads')
    for action in ('submit', 'status', 'result'):
        command = sub.add_parser(action)
        if action == 'submit':
            command.add_argument('--request', required=True)
            command.add_argument('--task', required=True)
            command.add_argument('--sha', required=True)
            command.add_argument('--scope', choices=['salesleads', 'full'], default='salesleads')
            command.add_argument('--rerun', action='store_true',
                                 help='create a fresh attempt after a terminal attempt')
        else:
            command.add_argument('--job-id', required=True)
    args = parser.parse_args()
    if args.action == 'worker':
        worker()
        return 0
    if args.action in {'run', 'submit'}:
        payload = {key: getattr(args, key) for key in FIELDS}
        snapshot = submit_job(payload, queue=QUEUE, rerun=getattr(args, 'rerun', False))
        if args.action == 'submit':
            _json_command(snapshot)
            return 0
        return wait_job(QUEUE / snapshot['job_id'])
    snapshot = status(args.job_id, queue=QUEUE) if args.action == 'status' else result(args.job_id, queue=QUEUE)
    _json_command(snapshot)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
