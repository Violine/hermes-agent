"""Hermes dashboard extension for the engineering team's operator surface.

Authentication is provided by Hermes. Mutations additionally require a same-
origin request and the process-local editor CSRF token. No provider calls,
credential editing, arbitrary file access or service-control endpoints exist.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .controller import read_json
from .efficiency import verification_progress
from .assignments import _test_wait
from .hermes_adapter import HermesBoard
from .quota import QuotaManager
from .project_context import ProjectContextError, inspect_project_context
from .web_config import ConfigConflict, load_editor, save_editor


router = APIRouter()
CSRF_TOKEN = secrets.token_urlsafe(32)
MAX_BODY = 128 * 1024
MAX_UPLOAD_BODY = 7 * 1024 * 1024
PROJECT_ID_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')
REQUEST_ID_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{7,127}$')


def config_path():
    return Path(os.environ.get('HERMES_TEAM_CONFIG', '/home/alex/.hermes/team/config.json'))


def guard_write(request):
    """Do not accept browser cross-origin/form writes, even on trusted LAN."""
    origin = request.headers.get('origin', '')
    expected = f'{request.url.scheme}://{request.url.netloc}'
    if (origin != expected or urlsplit(origin).path
            or request.headers.get('sec-fetch-site', '') == 'cross-site'):
        raise HTTPException(403, 'Изменение настроек разрешено только из этого дашборда.')
    if not secrets.compare_digest(request.headers.get('x-team-csrf', ''), CSRF_TOKEN):
        raise HTTPException(403, 'Обновите страницу редактора: защитный токен устарел.')
    if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
        raise HTTPException(415, 'Ожидается JSON.')


def ensure_profiles_idle(old, new):
    changed = {name for name in old['roles'] if old['roles'][name] != new['roles'][name]}
    if not changed:
        return
    board = HermesBoard(old['board'], old['hermes_root'])
    # A ready worker could be spawned immediately by the native dispatcher.
    busy = [name for name in sorted(changed)
            if any(board.list_tasks(assignee=name, status=status, limit=1)
                   for status in ('running', 'ready'))]
    if busy:
        raise ConfigConflict('Дождитесь завершения назначенной работы этих ролей: ' + ', '.join(sorted(set(busy))))


@router.get('/config')
def get_config():
    try:
        result = load_editor(config_path())
    except (OSError, ValueError):
        raise HTTPException(503, 'Не удалось прочитать конфигурацию команды. Проверьте установку teamctl.')
    result['csrf_token'] = CSRF_TOKEN
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.put('/config')
async def put_config(request: Request):
    guard_write(request)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_BODY:
            raise HTTPException(413, 'Конфигурация слишком велика.')
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Некорректный JSON.')
    try:
        result = await run_in_threadpool(save_editor, config_path(), payload,
                                        before_apply=ensure_profiles_idle)
    except ConfigConflict as exc:
        raise HTTPException(409, str(exc))
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc))
    except (OSError, RuntimeError):
        raise HTTPException(503, 'Не удалось сохранить настройки. Проверьте журнал сервера и резервную копию конфигурации.')
    result['csrf_token'] = CSRF_TOKEN
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


def _public_project_context(value):
    """Drop filesystem/internal artifact paths before exposing context metadata."""
    if not isinstance(value, dict):
        return None
    manifest = value.get('manifest') if isinstance(value.get('manifest'), dict) else {}
    return {
        'project_id': value.get('project_id'),
        'revision': value.get('revision'),
        'context_file': value.get('context_file'),
        'context_hash': value.get('context_hash'),
        'ready': value.get('ready'),
        'manifest': {key: manifest.get(key) for key in
                     ('schema_version', 'name', 'summary', 'stack', 'documents', 'components', 'external_sources')
                     if key in manifest},
        'sources': [{key: source.get(key) for key in ('path', 'sha256', 'scope') if key in source}
                    for source in value.get('sources', []) if isinstance(source, dict)],
        'warnings': list(value.get('warnings', [])) if isinstance(value.get('warnings'), list) else [],
        'errors': list(value.get('errors', [])) if isinstance(value.get('errors'), list) else [],
    }


@router.get('/projects/{project_id}/context')
def get_project_context(project_id: str):
    """Inspect the registered project's manifest at its configured base revision."""
    if not PROJECT_ID_RE.fullmatch(project_id):
        raise HTTPException(404, 'Проект не найден.')
    try:
        config = read_json(config_path())
        projects = config.get('projects') if isinstance(config, dict) else None
        project = projects.get(project_id) if isinstance(projects, dict) else None
        if not isinstance(project, dict):
            raise HTTPException(404, 'Проект не найден.')
        result = inspect_project_context(project_id, project)
    except HTTPException:
        raise
    except ProjectContextError as exc:
        # Inspection diagnostics are structured in the normal result. This is
        # only for failures that prevented creation of that report.
        raise HTTPException(422, 'Не удалось проверить контекст проекта.') from exc
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(503, 'Не удалось прочитать конфигурацию или проверить контекст проекта.')
    return JSONResponse(_public_project_context(result), headers={'Cache-Control': 'no-store'})


def controller_status(root):
    state = 'unknown'
    try:
        record = read_json(root / '.daemon.pid')
        proc = Path('/proc') / str(int(record['pid']))
        if ((proc / 'stat').read_text().split()[21] == str(record['start'])
                and b'team_engine.controller\x00daemon' in (proc / 'cmdline').read_bytes()):
            state = 'running'
        else:
            state = 'stopped'
    except (FileNotFoundError, ProcessLookupError):
        state = 'stopped'
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        pass
    return {'state': state, 'heartbeat_at': None}


def build_status(config, *, board=None, catalog=None):
    """Read native tasks plus durable state; never infer success from text."""
    root = Path(config['data_root'])
    board = board or HermesBoard(config['board'], config['hermes_root'])
    features, dispatch, waiting, waiting_tests, test_waits, errors = [], {}, set(), set(), {}, []
    heartbeat = 0
    for path in sorted((root / 'runs').glob('*/state.json')):
        try:
            state = read_json(path)
            feature = {key: state.get(key) for key in
                       ('id', 'title', 'stage', 'index', 'blocker', 'waiting_for_quota', 'waiting_for_tests')}
            progress = verification_progress(state)
            feature['activity_stage'] = progress.get('activity_stage')
            feature['verification_progress'] = progress
            # The controller's durable schema stores the registered project ID
            # under `project` (alongside a separate `project_contract`).
            feature['project_id'] = state.get('project')
            feature['integration_check'] = (
                {key: state['integration_check'].get(key)
                 for key in ('event', 'index', 'total', 'elapsed_seconds', 'result')
                 if key in state['integration_check']}
                if state.get('stage') == 'integration_review'
                and isinstance(state.get('integration_check'), dict) else None)
            feature['project_context'] = _public_project_context(state.get('project_context'))
            task_contexts = state.get('task_contexts', {})
            if isinstance(task_contexts, dict):
                feature['task_contexts'] = {card_id: _public_project_context(context)
                                            for card_id, context in task_contexts.items()
                                            if isinstance(card_id, str)}
            features.append(feature)
            features[-1]['total_tasks'] = len(state.get('items', []))
            dispatch.update(state.get('dispatch', {}))
            waiting.update(state.get('waiting_for_quota', []))
            state_waiting = state.get('waiting_for_tests', [])
            if isinstance(state_waiting, (list, tuple, set)):
                waiting_tests.update(card_id for card_id in state_waiting if isinstance(card_id, str))
            raw_test_waits = state.get('test_waits', {})
            item_card_ids = {
                item.get(field)
                for item in state.get('items', []) if isinstance(item, dict)
                for field in ('card', 'reviewer_card', 'qa_card', 'integrator_card')
                if isinstance(item.get(field), str)
            }
            if isinstance(raw_test_waits, dict):
                for key, record in raw_test_waits.items():
                    if not isinstance(key, str) or not isinstance(record, dict):
                        continue
                    card_id = record.get('card_id', record.get('card'))
                    # Older controller snapshots keyed one wait directly by card ID
                    # and omitted card_id in the value. Newer snapshots use
                    # `card_id:run_id`; only infer the key when it is independently
                    # known as a card, so run IDs are never exposed as task IDs.
                    if not isinstance(card_id, str) and (
                            key in item_card_ids or key in state.get('waiting_for_tests', [])):
                        card_id = key
                    if isinstance(card_id, str):
                        prior = test_waits.get(card_id)
                        if prior is None or str(record.get('run_id', '')) >= str(prior.get('run_id', '')):
                            test_waits[card_id] = record
            elif isinstance(raw_test_waits, list):
                test_waits.update({record.get('card_id', record.get('card')): record
                                   for record in raw_test_waits if isinstance(record, dict)
                                   and isinstance(record.get('card_id', record.get('card')), str)})
            for card_id, record in test_waits.items():
                if record.get('status') in {'accounting_pending', 'waiting'} or record.get('job_status') in {'accounting_pending', 'waiting'}:
                    waiting_tests.add(card_id)
            heartbeat = max(heartbeat, state.get('updated_at', 0))
        except (OSError, ValueError, TypeError):
            errors.append('Не удалось прочитать состояние одной из фич.')
    tasks = []
    native_tasks = {raw['id']: raw for raw in board.list_tasks(limit=200)}
    # Include real active workers even when historical rows fill the recent
    # page, and all quota waits identified by the durable controller state.
    for raw in board.list_tasks(status='running', limit=200):
        native_tasks[raw['id']] = raw
    for card_id in waiting - native_tasks.keys():
        raw = board.get_task(card_id)
        if raw:
            native_tasks[card_id] = raw
    for card_id in waiting_tests - native_tasks.keys():
        raw = board.get_task(card_id)
        if raw:
            native_tasks[card_id] = raw
    for raw in native_tasks.values():
        if raw.get('assignee') == 'team-controller':
            continue
        record = dispatch.get(raw['id'], {})
        selection = record.get('selection', {})
        status = raw['status']
        projected_wait, active_test_wait = _test_wait(
            {'test_waits': test_waits, 'waiting_for_tests': waiting_tests}, raw['id'])
        if active_test_wait:
            status = 'waiting_tests'
        elif raw['id'] in waiting and raw['status'] == 'blocked':
            status = 'waiting_quota'
        elif raw['status'] == 'blocked' and record and not record.get('armed'):
            status = 'planned'
        task = {key: raw.get(key) for key in ('id', 'title', 'assignee', 'updated_at', 'priority')}
        task.update(status=status, native_status=raw['status'],
                    model=raw.get('model_override') or selection.get('model'),
                    provider=raw.get('provider_override') or selection.get('provider'),
                    reasoning_effort=raw.get('reasoning_effort') or selection.get('reasoning_effort'),
                    quota_reason=record.get('quota_reason') if status == 'waiting_quota' else None)
        if projected_wait:
            task['test_wait'] = projected_wait
        feature_context = next((feature.get('task_contexts', {}).get(raw['id'])
                                for feature in features
                                if isinstance(feature.get('task_contexts'), dict)
                                and feature['task_contexts'].get(raw['id'])), None)
        if feature_context:
            task['project_context'] = feature_context
        else:
            feature_context = next((feature.get('project_context') for feature in features
                                    if feature.get('project_context')
                                    and feature.get('project_id') == record.get('project_id')), None)
            if feature_context:
                task['project_context'] = feature_context
        tasks.append(task)
    rank = {'running': 0, 'waiting_tests': 1, 'waiting_quota': 2, 'blocked': 3,
            'ready': 4, 'todo': 5, 'planned': 6}
    tasks.sort(key=lambda task: (rank.get(task['status'], 6), -(task.get('updated_at') or 0)))
    roles = []
    catalog = catalog or {}
    for name in config['roles']:
        current = next((task for task in tasks if task['assignee'] == name and task['status'] not in {'done', 'archived'}), None)
        roles.append({'id': name, 'label': catalog.get(name, {}).get('label', name),
                      'status': current['status'] if current else 'idle',
                      'task_title': current['title'] if current else None,
                      'task_id': current['id'] if current else None})
    controller = controller_status(root)
    controller['heartbeat_at'] = heartbeat or None
    quota = QuotaManager(config['quota'], root / 'quota.json').status() if config.get('quota') else {'pools': {}}
    # Per-run leases and raw worker output are not needed on the overview.
    return {'observed_at': time.time(), 'board': config['board'], 'board_url': '/kanban',
            'features': features, 'tasks': tasks, 'roles': roles, 'quota': {'pools': quota['pools']},
            'controller': controller, 'errors': errors}


@router.get('/status')
def get_status():
    try:
        path = config_path()
        result = build_status(read_json(path), catalog=load_editor(path)['catalog']['roles'])
    except (OSError, ValueError, RuntimeError):
        raise HTTPException(503, 'Статус команды временно недоступен. Проверьте контроллер и конфигурацию.')
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


def _workflow_service():
    # Keep controller-independent dashboard reads cheap; the service owns all
    # locks, snapshots, and state transitions shared with the Telegram plugin.
    from . import human_workflow
    return human_workflow


def _workflow_call(operation, *args, **kwargs):
    service = _workflow_service()
    try:
        return getattr(service, operation)(config_path(), *args, **kwargs)
    except service.WorkflowNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except service.WorkflowConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (service.WorkflowError, ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, RuntimeError):
        raise HTTPException(503, 'Не удалось обработать задачу. Повторите запрос с тем же идентификатором.')


async def _workflow_body(request, required, optional=(), *, list_fields=(), max_body=MAX_BODY):
    guard_write(request)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > max_body:
            raise HTTPException(413, 'Запрос слишком большой. Файл должен быть не больше 5 МБ.')
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, 'Ожидается корректный JSON.')
    if not isinstance(body, dict) or not set(required).issubset(body) or set(body) - set(required) - set(optional):
        raise HTTPException(422, 'Неожиданные или отсутствующие поля запроса.')
    for key, value in body.items():
        if key in list_fields:
            if not isinstance(value, list) or len(value) > 50 or any(not isinstance(item, str) for item in value):
                raise HTTPException(422, 'Ожидается список идентификаторов документов (не больше 50).')
        elif not isinstance(value, str):
            raise HTTPException(422, 'Неверный тип поля запроса.')
    if not REQUEST_ID_RE.fullmatch(body['request_id']):
        raise HTTPException(422, 'Неверный идентификатор запроса. Обновите форму и повторите.')
    return body


def _feature_id(value):
    if not PROJECT_ID_RE.fullmatch(value):
        raise HTTPException(404, 'Задача не найдена.')
    return value


@router.get('/requests')
def requests_list():
    return JSONResponse({'requests': _workflow_call('list_requests')}, headers={'Cache-Control': 'no-store'})


@router.get('/requests/{feature_id}')
def request_detail(feature_id: str):
    return JSONResponse(_workflow_call('get_request', _feature_id(feature_id)), headers={'Cache-Control': 'no-store'})


@router.get('/requests/{feature_id}/efficiency')
def request_efficiency(feature_id: str):
    """Compute Git/run metrics on demand; never put diff work on list polling."""
    return JSONResponse(_workflow_call('get_efficiency_report', _feature_id(feature_id)),
                        headers={'Cache-Control': 'no-store'})


@router.post('/requests')
async def request_submit(request: Request):
    body = await _workflow_body(request, {'project', 'title', 'brief', 'request_id'},
                                {'attachments'}, list_fields={'attachments'})
    result = await run_in_threadpool(_workflow_call, 'submit_request', **body,
                                    actor='dashboard:owner', channel='dashboard')
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/requests/{feature_id}/decision')
async def request_decide(feature_id: str, request: Request):
    _feature_id(feature_id)
    body = await _workflow_body(request, {'gate_id', 'decision', 'request_id'}, {'comment'})
    if body['decision'] not in {'approve', 'revise'}:
        raise HTTPException(422, 'Выберите согласование или возврат с замечаниями.')
    if body['decision'] == 'revise' and not body.get('comment', '').strip():
        raise HTTPException(422, 'Опишите, что требуется изменить.')
    result = await run_in_threadpool(_workflow_call, 'decide_request', feature_id, **body,
                                    actor='dashboard:owner', channel='dashboard')
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/requests/{feature_id}/answers')
async def request_answer(feature_id: str, request: Request):
    _feature_id(feature_id)
    body = await _workflow_body(request, {'gate_id', 'question_id', 'request_id'},
                                {'option_id', 'text'})
    if bool(body.get('option_id', '').strip()) == bool(body.get('text', '').strip()):
        raise HTTPException(422, 'Выберите вариант или напишите свой ответ.')
    result = await run_in_threadpool(_workflow_call, 'answer_question', feature_id, **body,
                                    actor='dashboard:owner', channel='dashboard')
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/requests/{feature_id}/comments')
async def request_comment(feature_id: str, request: Request):
    _feature_id(feature_id)
    body = await _workflow_body(request, {'comment', 'request_id'})
    if not body['comment'].strip():
        raise HTTPException(422, 'Напишите комментарий.')
    result = await run_in_threadpool(_workflow_call, 'add_comment', feature_id, **body,
                                    actor='dashboard:owner', channel='dashboard')
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/attachments')
async def attachment_upload(request: Request):
    body = await _workflow_body(request, {'name', 'content_base64', 'request_id'},
                                max_body=MAX_UPLOAD_BODY)
    from .attachments import upload_attachment, MAX_FILE_BYTES
    encoded = body['content_base64']
    if len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4:
        raise HTTPException(413, 'Файл должен быть не больше 5 МБ.')
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(422, 'Некорректное содержимое файла.')
    try:
        result = await run_in_threadpool(upload_attachment, config_path(),
            name=body['name'], content=content, request_id=body['request_id'],
            actor='dashboard:owner', channel='dashboard')
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, RuntimeError):
        raise HTTPException(503, 'Не удалось сохранить документ. Повторите загрузку.')
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/requests/{feature_id}/attachments')
async def request_attach(feature_id: str, request: Request):
    _feature_id(feature_id)
    body = await _workflow_body(request, {'attachment_ids', 'request_id'},
                                list_fields={'attachment_ids'})
    result = await run_in_threadpool(_workflow_call, 'attach_documents', feature_id, **body,
                                    actor='dashboard:owner', channel='dashboard')
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.get('/requests/{feature_id}/attachments/{attachment_id}')
def request_attachment_download(feature_id: str, attachment_id: str):
    _feature_id(feature_id)
    # Resolve only immutable files already attached to this request. Never
    # accept a caller-supplied filesystem path or render uploaded HTML inline.
    path, metadata = _workflow_call('request_attachment_path', feature_id, attachment_id)
    return FileResponse(path, filename=Path(metadata['name']).name,
        media_type='application/octet-stream',
        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                 'Content-Security-Policy': "sandbox; default-src 'none'"})
