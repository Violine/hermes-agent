"""Public role roster and per-card model assignments, without current-config drift.

Created cards and durable assignment snapshots describe the controller's actual
selection. Current role settings are used only for explicitly labelled forecasts.
Neither is a claim about provider-side effective reasoning or token consumption.
"""
from __future__ import annotations

import json
import re

from .contracts import ContractError, validate_plan

MODEL_FIELDS = ('model', 'provider', 'reasoning_effort')
WORKFLOW_ROLES = ('analyst', 'planner', 'skeptic', 'test-designer', 'judge',
                  'reviewer', 'qa', 'integrator')
DEVELOPER_ROLES = {'backend', 'frontend', 'mobile', 'go'}
PURPOSES = {
    'analyst': 'Изучает проект, уточняет требования и критерии приёмки.',
    'planner': 'Проектирует решение и распределяет атомарные задачи между разработчиками.',
    'skeptic': 'Проверяет допущения, риски и пробелы плана.',
    'test-designer': 'Проверяет покрытие требований и план тестирования.',
    'judge': 'Разбирает замечания независимых проверяющих и принимает решение по плану.',
    'backend': 'Реализует серверную часть в пределах назначенной задачи.',
    'frontend': 'Реализует веб-интерфейс и взаимодействие с API.',
    'mobile': 'Реализует мобильную часть с учётом платформы проекта.',
    'go': 'Реализует компоненты и сервисы на Go.',
    'reviewer': 'Независимо проверяет код и соответствие согласованным требованиям.',
    'qa': 'Проверяет критерии приёмки и регрессию на подготовленном коде.',
    'integrator': 'Проверяет готовность кандидата к локальной интеграции.',
}


def _text(value):
    return value if isinstance(value, str) and value else None


def _profile(role):
    return role if role.startswith('team-') else 'team-' + role


def _task_definition(state, card_id):
    for item in state.get('items', []):
        if card_id in [item.get(key) for key in ('card', 'reviewer_card', 'qa_card', 'integrator_card')]:
            return item.get('definition', {})
    return {}


def _test_wait(state, card_id):
    """Project the bounded, durable trusted-test wait record for one card."""
    waits = state.get('test_waits', {})
    entry = waits.get(card_id) if isinstance(waits, dict) else None
    if entry is None and isinstance(waits, dict):
        matching = [row for row in waits.values() if isinstance(row, dict)
                    and row.get('card_id', row.get('card')) == card_id]
        if matching:
            def run_order(row):
                run_id = row.get('run_id')
                if isinstance(run_id, int) and not isinstance(run_id, bool):
                    return (1, run_id)
                if isinstance(run_id, str) and re.fullmatch(r'[0-9]+', run_id):
                    return (1, int(run_id))
                # Native run IDs are decimal integers. Keep malformed/legacy
                # values deterministic, but rank them below valid native IDs.
                return (0, str(run_id or ''))

            entry = max(matching, key=run_order)
    if entry is None and isinstance(waits, list):
        entry = next((row for row in waits if isinstance(row, dict)
                      and row.get('card_id', row.get('card')) == card_id), None)
    if not isinstance(entry, dict):
        entry = {}
    waiting = state.get('waiting_for_tests', [])
    in_wait_list = isinstance(waiting, (list, tuple, set)) and card_id in waiting
    def token(value, pattern):
        if not isinstance(value, str) or len(value) > 128 or not re.fullmatch(pattern, value):
            return None
        return value

    job_status = token(entry.get('job_status'), r'[a-z][a-z0-9_-]{0,63}') or token(
        entry.get('status'), r'[a-z][a-z0-9_-]{0,63}')
    wait_state = token(entry.get('status'), r'[a-z][a-z0-9_-]{0,63}')
    active = (job_status in {'accounting_pending', 'waiting'}
              or (in_wait_list and (not wait_state or wait_state in {'accounting_pending', 'waiting'})))
    if not active:
        return None, False
    projected = {
        'kind': token(entry.get('kind'), r'[a-z][a-z0-9_-]{0,63}'),
        'job_id': token(entry.get('job_id'), r'[A-Za-z0-9_.:-]{1,128}'),
        'sha': token(entry.get('sha'), r'[0-9a-f]{7,64}'),
        'job_status': job_status,
        'started_at': (entry.get('started_at') if isinstance(entry.get('started_at'), (int, float))
                       else entry.get('started_at')[:64] if isinstance(entry.get('started_at'), str) else None),
        'updated_at': (entry.get('updated_at') if isinstance(entry.get('updated_at'), (int, float))
                       else entry.get('updated_at')[:64] if isinstance(entry.get('updated_at'), str) else None),
    }
    return projected, True


def project_assignments(controller, state):
    """Return allowlisted created cards and forecasts of not-yet-created work."""
    ids = []
    for key in ('analysis_card', 'planner_card', 'skeptic_card', 'test-designer_card',
                'judge_card', 'reviewer_card', 'qa_card', 'integrator_card', 'card'):
        if state.get(key): ids.append(state[key])
    for item in state.get('items', []):
        ids.extend(item[key] for key in ('card', 'reviewer_card', 'qa_card', 'integrator_card')
                   if item.get(key))
    # Include prior rounds, so changing team settings cannot rewrite their history.
    ids.extend(state.get('task_contexts', {}))
    ids.extend(state.get('assignment_snapshots', {}))
    cards = []
    for card_id in dict.fromkeys(ids):
        snapshot = state.get('assignment_snapshots', {}).get(card_id, {})
        dispatch = state.get('dispatch', {}).get(card_id, {})
        selection = dispatch.get('selection') or {}
        try:
            card = controller.board.get_task(card_id) or {}
        except (RuntimeError, ValueError, OSError):
            card = {}
        if not card and not snapshot:
            continue
        test_wait, waiting_tests = _test_wait(state, card_id)
        role = _text(card.get('assignee')) or _text(snapshot.get('role'))
        if not role or role == 'team-controller':
            continue
        short = role.removeprefix('team-')
        definition = _task_definition(state, card_id)
        # A card is the explicit native assignment, including quota overrides.
        # Missing historical fields never fall back to the current role config.
        model_data = {key: _text(card.get(key)) or _text(selection.get(key))
                      or _text(snapshot.get(key)) for key in MODEL_FIELDS}
        pending = bool(dispatch.get('waiting'))
        known = bool(model_data['model'] and model_data['provider'])
        source = ('quota' if selection and known else 'card' if card.get('model')
                  else 'snapshot' if snapshot.get('model') else 'unknown')
        assignment_state = 'pending' if pending else 'selected' if known else 'unknown'
        task_id = _text(definition.get('id'))
        reason = ('Назначено в плане для задачи ' + task_id + ': ' + str(definition.get('title', ''))
                  if task_id else 'Роль предусмотрена процессом анализа и независимых проверок.')
        if dispatch.get('quota_reason'):
            reason += ' Выбор ресурса: ' + str(dispatch['quota_reason'])[:700]
        cards.append({
            'id': card_id, 'role': role,
            'title': _text(card.get('title')) or _text(snapshot.get('title')) or role,
            'status': 'waiting_tests' if waiting_tests else _text(card.get('status')) or 'unknown',
            **({'test_wait': test_wait} if test_wait else {}),
            **model_data,
            'quota_pool': _text(selection.get('pool')) or _text(snapshot.get('quota_pool'))
                          or _text(dispatch.get('settings', {}).get('pool')),
            'assignment_state': assignment_state, 'assignment_source': source,
            'purpose': PURPOSES.get(short, 'Выполняет назначенную задачу.'),
            'selection_reason': reason, 'plan_task_id': task_id,
        })
    order = {'running': 0, 'waiting_tests': 1, 'ready': 2, 'todo': 3,
             'blocked': 4, 'triage': 4, 'done': 5, 'archived': 6}
    cards.sort(key=lambda row: order.get(row['status'], 6))
    present_roles = {row['role'] for row in cards}
    planned = []

    def forecast(role, title, reason, source='workflow', task_id=None, proposed=False):
        settings = controller.config.get('roles', {}).get(_profile(role), {})
        planned.append({
            'role': _profile(role), 'title': title, 'purpose': PURPOSES.get(role, ''),
            'selection_reason': reason, 'selection_source': source,
            **{key: _text(settings.get(key)) for key in MODEL_FIELDS},
            'quota_pool': _text(settings.get('pool')),
            'assignment_state': 'proposed' if proposed else 'planned',
            'plan_task_id': task_id,
        })

    if state.get('stage') not in {'done', 'cancelled'}:
        for role in WORKFLOW_ROLES:
            if _profile(role) not in present_roles:
                forecast(role, PURPOSES[role], 'Обязательная роль процесса; карточка ещё не создана.')
        path = controller.path(state) / 'artifacts/PLAN.json'
        if path.is_file():
            try:
                plan = json.loads(path.read_text(encoding='utf-8'))
                definitions = validate_plan(plan, project=state['project'], feature_id=state['id'],
                    base_sha=state['initial_base_sha'], max_tasks=controller.config.get('max_plan_tasks', 12))
            except (OSError, ValueError, TypeError, KeyError, ContractError):
                definitions = []
            created = {item.get('definition', {}).get('id') for item in state.get('items', [])
                       if item.get('card')}
            approved = bool(state.get('approved_plan_sha256'))
            for definition in definitions:
                role, task_id = definition['assignee_role'], definition['id']
                if role not in DEVELOPER_ROLES or task_id in created:
                    continue
                forecast(role, definition['title'],
                    'Планировщик назначил роль в PLAN.json для задачи ' + task_id + '.',
                    source='plan', task_id=task_id, proposed=not approved)
    return cards, planned
