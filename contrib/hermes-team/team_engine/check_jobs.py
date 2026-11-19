"""Durable queue for long, pinned project checks, independent of model runs.

Only identifiers cross the CLI boundary. Commands, repository paths, and the
candidate binding are resolved from the controller's persisted feature state.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

from .hermes_adapter import HermesBoard

HERMES_ROOT = Path('/home/alex/.hermes')
DATA_ROOT = HERMES_ROOT / 'team/data'
STATE_ROOT = DATA_ROOT / 'runs'
QUEUE = DATA_ROOT / 'trusted-check-jobs'
CONFIG = HERMES_ROOT / 'team/config.json'
DEFAULT_TIMEOUT = 3 * 60 * 60
JOB_ID_RE = re.compile(r'[0-9a-f]{64}')
REQUEST_RE = re.compile(r'req-[0-9a-f]{20}')
TASK_RE = re.compile(r'T-[0-9]{3}')
CARD_RE = re.compile(r't_[0-9a-f]{8,}')
SHA_RE = re.compile(r'[0-9a-f]{40}')
TERMINAL = {'completed', 'interrupted', 'rejected'}
JSON_LIMIT = 1024 * 1024
# Controller-owned feature state can include accumulated task/run evidence and
# is not an untrusted queue manifest. Keep its own finite cap while leaving
# all job, status, and metadata JSON on the smaller default limit.
FEATURE_STATE_LIMIT = 16 * 1024 * 1024
TAIL_BYTES = 8192
QUEUED_MAX_AGE = 6 * 60 * 60
_STOP_REQUESTED = False


class WorkerStopping(Exception):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _validate_ids(request, task, card, run, sha, checks):
    for name, value, pattern in (
            ('request', request, REQUEST_RE), ('task', task, TASK_RE),
            ('card', card, CARD_RE), ('run', run, re.compile(r'[0-9]{1,20}')),
            ('sha', sha, SHA_RE)):
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError(f'Invalid trusted-check {name}')
    if (not isinstance(checks, (list, tuple)) or not checks
            or any(type(index) is not int or index < 0 for index in checks)
            or len(set(checks)) != len(checks)):
        raise ValueError('Check indexes must be unique non-negative integers')
    return {'request': request, 'task': task, 'card': card, 'run': run,
            'sha': sha, 'checks': sorted(checks)}


def _plain_dir(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError('Trusted-check queue path must be a plain directory')
    return path


def _reject_symlink_components(path):
    path = Path(path).absolute()
    if path.is_symlink():
        raise ValueError('Trusted-check paths cannot contain symlinks')
    # Resolve platform aliases such as macOS /var -> /private/var before
    # checking remaining components; the production paths are Linux /home.
    path = path.resolve(strict=False)
    cursor = Path(path.anchor)
    for part in path.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError('Trusted-check paths cannot contain symlinks')


def _ensure_queue(queue):
    queue = Path(queue)
    _reject_symlink_components(queue)
    queue.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _plain_dir(queue)


def _atomic_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Trusted-check queue files cannot be symlinks')
    temp = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _read_json(path, limit=JSON_LIMIT):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('Invalid trusted-check JSON file')
    return json.loads(path.read_text(encoding='utf-8'))


def _feature_state(state_root, request):
    root = Path(state_root)
    _reject_symlink_components(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Feature state root must be a plain directory')
    run_dir = root / request
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ValueError('Unknown feature request')
    state_path = run_dir / 'state.json'
    state = _read_json(state_path, limit=FEATURE_STATE_LIMIT)
    if state.get('id') != request:
        raise ValueError('Feature state identity does not match request')
    return state


def _bind(payload, *, state_root, board):
    state = _feature_state(state_root, payload['request'])
    if state.get('stage') in {'done', 'cancelled'}:
        raise ValueError('Feature is no longer active')
    item = next((row for row in state.get('items', [])
                 if isinstance(row, dict)
                 and row.get('definition', {}).get('id') == payload['task']), None)
    if item is None or item.get('status') not in {
            'implementation', 'verification', 'integration_review'}:
        raise ValueError('Task is not an active implementation or verification item')
    role_for_card = {
        'card': item.get('definition', {}).get('assignee_role'),
        'reviewer_card': 'reviewer', 'qa_card': 'qa',
        'integrator_card': 'integrator',
    }
    worker_role = next((role for field, role in role_for_card.items()
                        if item.get(field) == payload['card']), None)
    if worker_role is None:
        raise ValueError('Task/card binding does not match feature state')
    role = worker_role
    if not isinstance(role, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', role):
        raise ValueError('Task has no trusted worker role')
    contract = state.get('project_contract')
    checks = contract.get('checks') if isinstance(contract, dict) else None
    if not isinstance(checks, list) or not checks:
        raise ValueError('Pinned project checks are unavailable')
    if any(index >= len(checks) for index in payload['checks']):
        raise ValueError('Check index is outside the pinned project contract')
    argv = []
    for index in payload['checks']:
        raw = checks[index]
        command = raw.get('argv') if isinstance(raw, dict) else raw
        if (not isinstance(command, list) or not command
                or any(not isinstance(arg, str) or not arg for arg in command)):
            raise ValueError(f'Pinned check {index} is malformed')
        argv.append({'index': index,
                     'name': raw.get('name', f'check-{index + 1}') if isinstance(raw, dict) else f'check-{index + 1}',
                     'argv': list(command), 'argv_sha256': _hash(list(command))})

    repo = Path(state.get('repo', '')).resolve(strict=True)
    card_field = next(field for field in role_for_card
                      if item.get(field) == payload['card'])
    workspace_field = {
        'card': 'workspace', 'reviewer_card': 'reviewer_workspace',
        'qa_card': 'qa_workspace', 'integrator_card': 'integrator_workspace',
    }[card_field]
    candidate_value = Path(item.get(workspace_field, ''))
    _reject_symlink_components(candidate_value)
    candidate = candidate_value.resolve(strict=True)
    if candidate == repo or not candidate.is_dir():
        raise ValueError('Candidate workspace is not an isolated task worktree')
    if _git(candidate, 'rev-parse', '--path-format=absolute', '--git-common-dir') != _git(
            repo, 'rev-parse', '--path-format=absolute', '--git-common-dir'):
        raise ValueError('Candidate workspace belongs to a different repository')
    if _git(candidate, 'rev-parse', 'HEAD') != payload['sha']:
        raise ValueError('Candidate SHA does not match the candidate worktree HEAD')
    if _git(candidate, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('Candidate worktree has uncommitted changes')
    _git(repo, 'cat-file', '-e', payload['sha'] + '^{commit}')

    task_record = board.get_task(payload['card'])
    if not task_record or task_record.get('assignee') != 'team-' + role:
        raise ValueError('Native card is not assigned to the expected worker role')
    if task_record.get('status') != 'running' or str(task_record.get('current_run_id')) != payload['run']:
        raise ValueError('Native card is not running this worker run')
    runs = board.get_runs(payload['card'])
    matching = [row for row in runs
                if str(row.get('id', row.get('run_id'))) == payload['run']]
    latest = max(runs, key=lambda row: int(row.get('id', row.get('run_id', 0)))) if runs else None
    if (len(matching) != 1 or latest is None
            or str(latest.get('id', latest.get('run_id'))) != payload['run']
            or latest.get('profile') != 'team-' + role
            or latest.get('status') != 'running'):
        raise ValueError('Run does not match the latest active native worker run')
    return {'repo': str(repo), 'candidate_workspace': str(candidate),
            'role': role, 'checks': argv}


def _git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30,
                            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                                 'LC_ALL': 'C.UTF-8', 'TZ': 'Asia/Almaty'})
    if result.returncode:
        raise ValueError('Git candidate validation failed')
    return result.stdout.strip()


def _identity(payload, binding, rerun_nonce=None):
    # Run/card are part of the immutable request. A new native run is a new
    # execution; explicit reruns on the same run receive a fresh immutable id.
    return _hash({'payload': payload, 'checks': binding['checks'],
                  'rerun_nonce': rerun_nonce})


def submit(request, task, card, run, sha, checks, *, state_root, board,
           queue=QUEUE, timeout=DEFAULT_TIMEOUT, rerun=False):
    payload = _validate_ids(request, task, card, run, sha, checks)
    if type(timeout) is not int or not 1 <= timeout <= 12 * 60 * 60:
        raise ValueError('Invalid trusted-check wall timeout')
    binding = _bind(payload, state_root=state_root, board=board)
    if type(rerun) is not bool:
        raise ValueError('rerun must be a boolean')
    nonce = uuid.uuid4().hex if rerun else None
    job_id = _identity(payload, binding, nonce)
    queue = _ensure_queue(queue)
    job_dir = queue / job_id
    lock_path = queue / '.submit.lock'
    if lock_path.is_symlink():
        raise ValueError('Trusted-check submit lock cannot be a symlink')
    with lock_path.open('a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        if rerun:
            for existing in queue.iterdir():
                if (not JOB_ID_RE.fullmatch(existing.name) or existing.is_symlink()
                        or not existing.is_dir()):
                    continue
                try:
                    existing_dir, existing_manifest = _job_dir(queue, existing.name)
                    existing_payload = existing_manifest['payload']
                    existing_checks = existing_manifest['binding']['checks']
                    existing_state = _read_json(existing_dir / 'status.json')
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    continue
                if (existing_payload == payload and existing_checks == binding['checks']
                        and existing_state.get('status') not in TERMINAL):
                    raise ValueError('An identical trusted-check job is already active')
        if job_dir.is_symlink():
            raise ValueError('Trusted-check job directory cannot be a symlink')
        if not job_dir.exists():
            job_dir.mkdir(mode=0o700)
            _plain_dir(job_dir)
            _atomic_json(job_dir / 'job.json', {
                'job_id': job_id, 'payload': payload, 'binding': binding,
                'submitted_at': time.time(), 'timeout': timeout,
                'rerun_nonce': nonce})
            _atomic_json(job_dir / 'status.json', {
                'job_id': job_id, 'status': 'queued', **payload,
                'role': binding['role'],
                'submitted_at': time.time(),
                'updated_at': time.time(), 'timeout': timeout})
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return read_job(job_id, queue=queue)


def _job_dir(queue, job_id):
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise ValueError('Invalid trusted-check job id')
    queue = Path(queue)
    _reject_symlink_components(queue)
    _plain_dir(queue)
    directory = queue / job_id
    _plain_dir(directory)
    manifest = _read_json(directory / 'job.json')
    payload = _validate_ids(**manifest['payload'])
    if (manifest.get('job_id') != job_id or
            _identity(payload, manifest['binding'], manifest.get('rerun_nonce')) != job_id):
        raise ValueError('Trusted-check job identity mismatch')
    return directory, manifest


def _tail(path, limit=TAIL_BYTES):
    path = Path(path)
    if path.is_symlink() or not path.exists():
        return ''
    if not path.is_file():
        raise ValueError('Trusted-check log must be a regular file')
    with path.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - limit))
        return stream.read(limit).decode('utf-8', 'replace')


def read_job(job_id, *, queue=QUEUE, tail_bytes=TAIL_BYTES):
    if type(tail_bytes) is not int or not 0 <= tail_bytes <= TAIL_BYTES:
        raise ValueError(f'tail_bytes must be from 0 to {TAIL_BYTES}')
    directory, manifest = _job_dir(queue, job_id)
    state = _read_json(directory / 'status.json')
    if state.get('job_id') != job_id:
        raise ValueError('Trusted-check status identity mismatch')
    snapshot = {key: state[key] for key in (
        'job_id', 'status', 'request', 'task', 'card', 'run', 'role', 'sha', 'checks',
        'submitted_at', 'started_at', 'updated_at', 'finished_at', 'exit_code',
        'error', 'results', 'timeout') if key in state}
    snapshot.setdefault('attempt', 1)
    snapshot['log_tail'] = _tail(directory / 'output.log', tail_bytes)
    snapshot['result_ready'] = state.get('status') in TERMINAL
    return snapshot


def status(job_id, *, queue=QUEUE):
    return read_job(job_id, queue=queue)


def result(job_id, *, queue=QUEUE):
    return read_job(job_id, queue=queue)


def _managed_env():
    env = {'PATH': '/home/alex/.hermes/toolchains/jdk-21/bin:/home/alex/.hermes/node/bin:/home/alex/.local/bin:/usr/local/bin:/usr/bin:/bin',
           'HOME': '/home/alex', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
           'TZ': 'Asia/Almaty', 'JAVA_HOME': '/home/alex/.hermes/toolchains/jdk-21',
           'GRADLE_USER_HOME': '/home/alex/.gradle'}
    return env


def _proc_fingerprint(pid):
    """Return Linux starttime and exact cmdline digest for PID-reuse safety."""
    try:
        stat = Path(f'/proc/{int(pid)}/stat').read_text()
        closing = stat.rfind(')')
        fields = stat[closing + 2:].split()
        start_time = fields[19]
        cmdline = Path(f'/proc/{int(pid)}/cmdline').read_bytes()
    except (OSError, ValueError, IndexError):
        return None
    if not cmdline:
        return None
    return {'pid': int(pid), 'start_time': start_time,
            'cmdline_sha256': hashlib.sha256(cmdline).hexdigest()}


def _verified_pid(record):
    if not isinstance(record, dict) or type(record.get('pid')) is not int:
        return False
    actual = _proc_fingerprint(record['pid'])
    return bool(actual and actual == {key: record.get(key) for key in
                                      ('pid', 'start_time', 'cmdline_sha256')})


def _terminate_recorded_process(record):
    """Terminate only the still-identical recorded check process group."""
    if not _verified_pid(record):
        return False
    pid = record['pid']
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def _worker_pid_path(queue):
    return Path(queue) / 'worker.pid.json'


def stop_worker(*, queue=QUEUE, timeout=20):
    pid_path = _worker_pid_path(queue)
    try:
        record = _read_json(pid_path, limit=4096)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not _verified_pid(record):
        return False
    try:
        cmdline = Path(f"/proc/{record['pid']}/cmdline").read_bytes()
    except OSError:
        return False
    if b'team_engine.check_jobs' not in cmdline or b'worker' not in cmdline:
        return False
    pid = record['pid']
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _verified_pid(record):
            return True
        time.sleep(0.2)
    if _verified_pid(record):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True


def _heartbeat(status_path, state, results):
    state['updated_at'] = time.time()
    state['results'] = list(results)
    _atomic_json(status_path, state)


def execute_job(job_id, *, queue=QUEUE, timeout=None, lock_path=None):
    directory, manifest = _job_dir(queue, job_id)
    status_path = directory / 'status.json'
    state = _read_json(status_path)
    if state.get('status') != 'queued':
        return int(state.get('exit_code', 0))
    start = time.time()
    deadline = time.monotonic() + (timeout if timeout is not None else manifest['timeout'])
    state.update(status='running', started_at=start, updated_at=start)
    _atomic_json(status_path, state)
    repo = Path(manifest['binding']['repo'])
    payload = manifest['payload']
    worktree = directory / 'worktree'
    output_path = directory / 'output.log'
    results = []
    proc = None
    env = _managed_env()
    if lock_path is None:
        lock_path = Path.home() / '.hermes/team/data/.heavy-test.lock'
    lock_path = Path(lock_path)
    if lock_path.is_symlink():
        raise ValueError('Heavy-test lock cannot be a symlink')
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = None
    acquired = False
    try:
        # The detached worktree is private to the immutable job and never edits
        # the shared project checkout or the developer candidate worktree.
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach',
                        str(worktree), payload['sha']], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=60, env=env)
        lock_fd = lock_path.open('a')
        with output_path.open('ab') as log:
            while not acquired:
                if _STOP_REQUESTED:
                    raise WorkerStopping()
                if time.monotonic() >= deadline:
                    for check in manifest['binding']['checks']:
                        results.append({'index': check['index'], 'name': check['name'],
                            'argv': check['argv'], 'argv_sha256': check['argv_sha256'],
                            'status': 'not_run_timeout', 'exit_code': None,
                            'started_at': None, 'finished_at': None,
                            'log_offset': 0, 'log_bytes': 0})
                    break
                try:
                    fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    _heartbeat(status_path, state, results)
                    time.sleep(min(1, max(0.05, deadline - time.monotonic())))
            if acquired:
                for check in manifest['binding']['checks']:
                    if _STOP_REQUESTED:
                        raise WorkerStopping()
                    check_start = time.time()
                    offset = log.tell()
                    log.write((f"\n=== CHECK {check['index']} {check['name']} ===\n").encode())
                    log.flush()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        check_result = {'index': check['index'], 'name': check['name'],
                            'argv': check['argv'], 'argv_sha256': check['argv_sha256'],
                            'status': 'not_run_timeout', 'exit_code': None,
                            'started_at': None, 'finished_at': None,
                            'log_offset': log.tell(), 'log_bytes': 0}
                        results.append(check_result)
                        continue
                    timed_out = False
                    try:
                        proc = subprocess.Popen(check['argv'], cwd=worktree,
                            stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, env=env, start_new_session=True)
                        process_record = _proc_fingerprint(proc.pid)
                        state['process'] = process_record
                        _heartbeat(status_path, state, results)
                        while proc.poll() is None:
                            if _STOP_REQUESTED:
                                _stop_process(proc)
                                raise WorkerStopping()
                            if time.monotonic() >= deadline:
                                timed_out = True
                                _stop_process(proc)
                                break
                            _heartbeat(status_path, state, results)
                            time.sleep(1)
                        code = 124 if timed_out else (proc.returncode if proc.returncode is not None else 125)
                        end_offset = log.tell()
                        results.append({'index': check['index'], 'name': check['name'],
                            'argv': check['argv'], 'argv_sha256': check['argv_sha256'],
                            'status': 'passed' if code == 0 else ('timed_out' if timed_out else 'failed'),
                            'exit_code': code, 'started_at': check_start,
                            'finished_at': time.time(), 'log_offset': offset,
                            'log_bytes': max(0, end_offset - offset)})
                    except OSError:
                        end_offset = log.tell()
                        results.append({'index': check['index'], 'name': check['name'],
                            'argv': check['argv'], 'argv_sha256': check['argv_sha256'],
                            'status': 'spawn_error', 'exit_code': 127,
                            'started_at': check_start, 'finished_at': time.time(),
                            'log_offset': offset, 'log_bytes': max(0, end_offset-offset)})
                    proc = None
                    state.pop('process', None)
                    _heartbeat(status_path, state, results)
        if any(row['status'] == 'not_run_timeout' for row in results):
            overall, final_status = 124, 'completed'
        else:
            codes = [row['exit_code'] for row in results if row.get('exit_code') not in (None, 0)]
            overall = codes[0] if codes else 0
            final_status = 'completed'
    except WorkerStopping as exc:
        if proc is not None:
            _stop_process(proc)
            proc = None
        overall, final_status = 125, 'interrupted'
        state['error'] = type(exc).__name__
    except BaseException as exc:
        if proc is not None:
            _stop_process(proc)
            proc = None
        overall, final_status = 125, 'interrupted'
        state['error'] = type(exc).__name__
    finally:
        if lock_fd is not None:
            if acquired:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            lock_fd.close()
        try:
            subprocess.run(['git', '-C', str(repo), 'worktree', 'remove', '--force', str(worktree)],
                           check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30, env=env)
        except (OSError, subprocess.TimeoutExpired):
            pass
    state.update(status=final_status, results=results, exit_code=overall,
                 finished_at=time.time(), updated_at=time.time())
    state.pop('process', None)
    _atomic_json(status_path, state)
    return overall


def _stop_process(proc):
    """Reap the process and its group within a bounded grace period."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    proc.wait()
    # A command can leave grandchildren in its session. Bound their lifetime
    # even when the direct child exited cleanly or handled SIGTERM first.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def worker(*, queue=QUEUE):
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    queue = _ensure_queue(queue)
    lock = queue / '.worker.lock'
    if lock.is_symlink():
        raise ValueError('Trusted-check worker lock cannot be a symlink')
    with lock.open('a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        pid_path = _worker_pid_path(queue)
        if pid_path.is_symlink():
            raise ValueError('Trusted-check worker PID file cannot be a symlink')
        own_pid = _proc_fingerprint(os.getpid())
        if own_pid is None:
            raise RuntimeError('Cannot fingerprint trusted-check worker process')
        _atomic_json(pid_path, own_pid)

        def request_stop(_signum, _frame):
            global _STOP_REQUESTED
            _STOP_REQUESTED = True

        old_term = signal.signal(signal.SIGTERM, request_stop)
        old_int = signal.signal(signal.SIGINT, request_stop)
        try:
            for directory in queue.iterdir():
                if not JOB_ID_RE.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                    continue
                try:
                    current = _read_json(directory / 'status.json')
                    if current.get('status') == 'running':
                        _terminate_recorded_process(current.get('process'))
                        _atomic_json(directory / 'status.json', {**current,
                            'status': 'interrupted', 'exit_code': 125,
                            'error': 'Worker restarted while trusted checks were running',
                            'process': None, 'finished_at': time.time(),
                            'updated_at': time.time()})
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
            while not _STOP_REQUESTED:
                for directory in sorted(queue.iterdir()):
                    if _STOP_REQUESTED:
                        break
                    if not JOB_ID_RE.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                        continue
                    status_path = directory / 'status.json'
                    try:
                        current = _read_json(status_path)
                        if current.get('status') != 'queued':
                            continue
                        age = time.time() - float(current.get('submitted_at', 0))
                        if age > QUEUED_MAX_AGE:
                            _atomic_json(status_path, {**current, 'status': 'rejected',
                                'exit_code': 124, 'error': 'Queue wait exceeded six hours',
                                'finished_at': time.time(), 'updated_at': time.time()})
                            continue
                        execute_job(directory.name, queue=queue)
                    except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
                        try:
                            current = _read_json(status_path)
                            if current.get('status') == 'queued':
                                _atomic_json(status_path, {**current, 'status': 'rejected',
                                    'exit_code': 2, 'error': type(exc).__name__,
                                    'finished_at': time.time(), 'updated_at': time.time()})
                        except (OSError, ValueError, json.JSONDecodeError):
                            continue
                if not _STOP_REQUESTED:
                    time.sleep(1)
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
            if _verified_pid(own_pid):
                pid_path.unlink(missing_ok=True)


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


def _runtime_settings(config=CONFIG):
    value = _read_json(config)
    board_slug = value.get('board')
    hermes_root = value.get('hermes_root')
    data_root = value.get('data_root')
    if (not isinstance(board_slug, str) or not re.fullmatch(r'[a-z0-9_-]{1,64}', board_slug)
            or not isinstance(hermes_root, str) or not Path(hermes_root).is_absolute()
            or not isinstance(data_root, str) or not Path(data_root).is_absolute()):
        raise ValueError('Runtime config has invalid board/data paths')
    return Path(hermes_root), board_slug, Path(data_root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    submit_parser = sub.add_parser('submit')
    for name in ('request', 'task', 'card', 'run', 'sha'):
        submit_parser.add_argument('--' + name, required=True)
    submit_parser.add_argument('--checks', required=True,
        help='comma-separated pinned check indexes, for example 0,1')
    submit_parser.add_argument('--rerun', action='store_true',
        help='create a separate immutable job for an explicit repeat')
    for name in ('status', 'result'):
        command = sub.add_parser(name)
        command.add_argument('--job', required=True)
    sub.add_parser('worker')
    sub.add_parser('stop')
    args = parser.parse_args(argv)
    if args.action == 'worker':
        worker()
        return 0
    if args.action == 'stop':
        stop_worker()
        return 0
    if args.action in {'status', 'result'}:
        _emit(status(args.job) if args.action == 'status' else result(args.job))
        return 0
    hermes_root, board_slug, data_root = _runtime_settings()
    board = HermesBoard(board_slug, hermes_root)
    indexes = [int(part) for part in args.checks.split(',')]
    snapshot = submit(args.request, args.task, args.card, args.run, args.sha,
                      indexes, state_root=data_root / 'runs', board=board,
                      rerun=args.rerun)
    _emit(snapshot)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
