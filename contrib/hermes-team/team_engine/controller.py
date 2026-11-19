"""Durable controller; Hermes owns worker dispatch and the public task history."""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
import re
import signal
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from .contracts import (ContractError, PRIORITIES, safe_id, validate_plan, path_owned,
                        validate_owned_path)
from .git_gates import (EvidenceError, GitGateError, validate_candidate,
                        validate_promotion_evidence, validate_supplemental_checks,
                        validate_qa_verification_plan, integrate_candidate,
                        run_batch_regression)
from .hermes_adapter import HermesBoard, HermesBoardError
from .plan_gates import (PlanGateError, validate_plan_review, validate_plan_reviews,
                         validate_plan_decision)
from .quota import QuotaManager
from .project_context import ProjectContextError, write_project_context
from .prerequisites import load_prerequisites, PrerequisiteError
from .execution_policy import verification_plan, selected_argv
from .test_waits import TestWaitsMixin

TERMINAL = {"done", "blocked", "cancelled"}


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def file_lock(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise GitGateError(result.stderr.strip()[-2000:] or "Git operation failed")
    return result.stdout.strip()


def require_clean(path):
    if git(path, "status", "--porcelain", "--untracked-files=all"):
        raise ContractError(f"Working copy is not clean: {path}")


def require_artifact(path, minimum=100):
    p = Path(path)
    if not p.is_file() or len(p.read_text().strip()) < minimum:
        raise ContractError(f"Missing or incomplete artifact: {p}")
    return p


class Controller(TestWaitsMixin):
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.config = read_json(self.config_path)
        max_active_features = self.config.get("max_active_features", 1)
        if type(max_active_features) is not int or max_active_features != 1:
            raise ContractError(
                "This runtime supports max_active_features=1; "
                "multi-feature scheduling is not implemented"
            )
        self.root = Path(self.config["data_root"])
        self.bundle = Path(self.config["bundle_root"])
        self.root.mkdir(parents=True, exist_ok=True)
        self.board = HermesBoard(self.config["board"], self.config["hermes_root"])
        self.board.ensure_board("Инженерная команда",
                                "ТЗ → анализ → план → разработка → review + QA → интеграция")
        self.quota = (QuotaManager(self.config['quota'], self.root / 'quota.json')
                      if self.config.get('quota') else None)
        self.waiting_cards = set()
        self.test_waiting_cards = set()

    def profile(self, role):
        name = "team-" + role
        return name, self.config["roles"][name]

    def path(self, state):
        return self.root / "runs" / state["id"]

    @staticmethod
    def root_key(state):
        if "root_round" not in state:
            return f"{state['id']}:root"  # Existing persisted states retain their key.
        return f"{state['id']}:root:r{state['root_round']}"

    def project_settings(self, state):
        # Pin executable regression policy for an in-flight feature.
        if 'project_contract' not in state:
            state['project_contract'] = copy.deepcopy(self.config['projects'][state['project']])
        return state['project_contract']

    def save(self, state):
        state["updated_at"] = int(time.time())
        write_json(self.path(state) / "state.json", state)

    def event(self, state, message, **metadata):
        self.board.add_comment(state["root_card"], "team-controller", message)
        with (self.path(state) / "events.jsonl").open("a") as stream:
            stream.write(json.dumps({"time": int(time.time()), "message": message,
                                     **metadata}, ensure_ascii=False) + "\n")

    def spawn(self, state, role, label, body, workspace, key, *, parents=(),
              priority=20, parked=False):
        profile, settings = self.profile(role)
        # Planned developer cards have no worktree yet. Their final context is
        # attached to TASK.md when dependencies are integrated and they activate.
        snapshot = self.ensure_project_context(state)
        if not parked:
            snapshot = self.workspace_context(state, workspace)
        body += self.context_handoff(snapshot, workspace, planned=parked)
        prerequisite_dir = self.path(state) / 'prerequisites' / hashlib.sha256(
            (state['id'] + ':' + key).encode()).hexdigest()[:24]
        prerequisite_dir.mkdir(parents=True, exist_ok=True)
        body += (
            '\n## Если не хватает компонента среды\n'
            f'Единственный путь запроса помощи этой карточки: {prerequisite_dir / "PREREQUISITES.json"}. '
            'Используй контракт PREREQUISITES.json из общих инструкций и точный '
            'HERMES_KANBAN_RUN_ID. Если проверка подтвердила отсутствие компонента, '
            'сохрани доказательства и заблокируй карточку через kanban_block. '
            'Лимит подписки, ошибка входа и дефект кода не являются отсутствующим компонентом.\n'
            'Для долгих проверок используй hermes-checks или hermes-backend-tests submit, '
            f'сохрани {prerequisite_dir / "TEST-CHECKPOINT.md"} вне репозитория, затем hermes-test-wait '
            f'--request {state["id"]} --kind checks|backend --job <job_id> и '
            'kanban_block(kind="transient", reason="Ожидает проверку <kind>:<job_id>"). '
            'Контроллер продолжит эту же карточку после результата, с оставшимся активным бюджетом.\n'
        )
        card = self.board.create_task(
            title=f"[{state['id']}] {label}",
            assignee=profile, body=body, parents=parents, priority=priority,
            workspace_path=str(workspace), model=settings["model"],
            provider=settings["provider"], reasoning_effort=settings["reasoning_effort"],
            idempotency_key=state["id"] + ":" + key,
            max_runtime_seconds=settings.get("run_budget", 600) + 90,
            initial_status="blocked" if parked or self.quota else "running")
        state.setdefault('assignment_snapshots', {}).setdefault(card['id'], {
            'role': profile, 'title': card.get('title'),
            **{name: card.get(name) or settings.get(name)
               for name in ('model', 'provider', 'reasoning_effort')},
            'quota_pool': settings.get('pool'), 'created_at': int(time.time()),
            'run_budget': settings.get('run_budget', 600),
            'max_turns': settings.get('max_turns', 40),
        })
        state.setdefault('task_contexts', {}).setdefault(card['id'], {
            **snapshot, 'workspace': str(workspace), 'planned': parked,
        })
        state.setdefault('prerequisite_paths', {})[card['id']] = str(prerequisite_dir)
        if self.quota:
            dispatch = state.setdefault('dispatch', {})
            dispatch.setdefault(card['id'], {'role':role, 'settings':copy.deepcopy(settings),
                'key':state['id'] + ':' + key, 'priority':priority, 'armed':not parked, 'waiting':True})
            if not parked:
                self.activate_with_quota(state, card['id'])
        return card["id"]

    @staticmethod
    def current_worker_cards(state):
        stage = state.get('resume_stage') if state.get('stage') == 'blocked' else state.get('stage')
        keys = {'analysis': ('analysis_card',), 'planning': ('planner_card',),
                'plan_challenge': ('skeptic_card', 'test-designer_card'),
                'plan_judgment': ('judge_card',)}.get(stage)
        if keys is not None:
            return [state[key] for key in keys if state.get(key)]
        items = state.get('items') or []
        if stage in {'implementation', 'verification', 'integration_review'}:
            cards = []
            stage_keys = {
                'implementation': {'implementation': ('card',)},
                'verification': {'verification': ('reviewer_card', 'qa_card'),
                                 'implementation': ('card',)},
                'integration_review': {'integration_review': ('integrator_card',),
                                       'implementation': ('card',)},
            }[stage]
            for item in items:
                for key in stage_keys.get(item.get('status'), ()):
                    if item.get(key):
                        cards.append(item[key])
            return cards
        index = state.get('index', 0)
        return [] if not isinstance(index, int) or not 0 <= index < len(items) else []

    def prerequisites(self, state):
        """Read current terminal workers' evidence, never quota parking or free text."""
        result = []
        root = self.path(state) / 'prerequisites'
        for card_id in self.current_worker_cards(state):
            directory = state.get('prerequisite_paths', {}).get(card_id)
            if not directory:
                continue  # Older cards did not receive this contract.
            directory = Path(directory)
            # Paths are controller-owned, direct children of this feature's root.
            if directory.parent != root or directory.is_symlink() or root.is_symlink():
                continue
            card = self.board.get_task(card_id)
            dispatch = state.get('dispatch', {}).get(card_id, {})
            if (not card or card.get('status') not in {'blocked', 'done'}
                    or dispatch.get('waiting')):
                continue
            try:
                runs = self.board.get_runs(card_id)
                latest = max(runs, key=lambda r: int(r.get('id', r.get('run_id'))))
                run_id = str(latest.get('id', latest.get('run_id')))
                if (latest.get('profile') != card.get('assignee')
                        or latest.get('status') not in {'blocked', 'done', 'completed'}):
                    continue
                entries = load_prerequisites(directory, expected_assignee=card['assignee'],
                                             expected_run_id=run_id)
            except (PrerequisiteError, ValueError, TypeError, KeyError):
                continue  # Invalid/stale worker text is never an installation recipe.
            result.extend(dict(entry, card_id=card_id) for entry in entries)
        return result

    def resume_blocked(self, state, reason, *, retry_workers=False):
        """Trusted recovery shared by CLI and owner action, under admission/feature locks."""
        if state['stage'] != 'blocked':
            raise ContractError('Only blocked features can be resumed')
        active = [summary for summary in self.summaries()
                  if summary['id'] != state['id'] and summary['stage'] not in TERMINAL]
        if len(active) >= self.config.get('max_active_features', 1):
            raise ContractError('Active feature limit reached; wait for completion or resolve its blocker')
        if retry_workers:
            cards = self.current_worker_cards(state)
            if (state.get('resume_stage') == 'verification' and state.get('items')
                    and state['items'][state['index']].get('verification_blocked_roles')):
                item = state['items'][state['index']]
                roles = list(item.get('verification_blocked_roles', []))
                if (not roles or len(set(roles)) != len(roles)
                        or any(role not in {'reviewer', 'qa'} for role in roles)):
                    raise ContractError('Blocked verification roles are missing or malformed')
                limits = state.get('worker_attempt_limits', {})
                if not isinstance(limits, dict):
                    raise ContractError('Invalid worker attempt limit registry')
                bounded = []
                fresh_roles = []
                for role in roles:
                    card_id = item.get(role + '_card')
                    if card_id not in limits:
                        fresh_roles.append(role)
                        continue
                    limit = limits[card_id]
                    disposition = self._classify_verifier_retry_grant(state, item, role, card_id, limit)
                    if disposition == 'unused':
                        bounded.append(card_id)
                    else:
                        fresh_roles.append(role)
                if bounded and fresh_roles:
                    raise ContractError('Bounded and fresh verification retries must be resumed separately')
                # A consumed bounded allowance is history. Only a structurally
                # valid blocked report from its one terminal successor can move
                # that role to the ordinary fresh-card retry path.
                if fresh_roles:
                    item['verification_blocked_roles'] = fresh_roles
                    self.retry_blocked_verification(state, item, reason)
                cards = bounded
            for card_id in cards:
                if self.board.get_task(card_id)['status'] == 'blocked':
                    if self.quota:
                        state['dispatch'][card_id]['waiting'] = True
                    self.activate_with_quota(state, card_id)
        elif (state.get('resume_stage') == 'verification' and state.get('items')
              and state['items'][state['index']].get('verification_blocked_roles')):
            raise ContractError('Blocked verification needs a fresh reviewer/QA run; '
                                'use --retry-workers after resolving the blocker')
        state['stage'] = state.pop('resume_stage', state.get('stage'))
        state.pop('blocker', None)
        state['resume_requested'] = True
        self.save(state)
        if self.board.get_task(state['root_card'])['status'] == 'blocked':
            self.board.activate_task(state['root_card'])
        state.pop('resume_requested', None)
        self.event(state, 'Владелец возобновил: ' + reason)
        self.save(state)

    def _classify_verifier_retry_grant(self, state, item, role, card_id, raw_limit):
        """Classify a recorded verifier allowance as unused or spent on a blocked result.

        Registry membership alone is not proof that a grant is reusable. A used
        grant can only fall back to a fresh verifier card after its sole native
        successor completed successfully and produced a valid, bound blocked report.
        """
        from .worker_budget import validate_attempt_limit, WorkerBudgetError

        card = self.board.get_task(card_id)
        try:
            limit = validate_attempt_limit(raw_limit, card_id=card_id,
                                           assignee='team-' + role)
        except WorkerBudgetError as exc:
            raise ContractError(str(exc)) from exc
        if limit.get('purpose') != 'finalize_existing_verification':
            raise ContractError('Verifier retry registry contains an unexpected allowance purpose')
        if (limit.get('item_id') != item.get('definition', {}).get('id')
                or limit.get('round') != item.get('round')):
            raise ContractError('Verifier allowance no longer matches the blocked task round')
        if not card or card.get('assignee') != 'team-' + role:
            raise ContractError('Verifier allowance card identity changed')
        if (card.get('current_run_id') is not None or card.get('claim_lock')
                or card.get('worker_pid') or card.get('status') in
                {'running', 'ready', 'todo', 'triage', 'claimed', 'in_progress'}):
            raise ContractError('Verifier allowance has an active native successor')
        # Recheck candidate and owner-approved document pins while permitting a
        # report only for the spent-result branch below.
        self._validate_verifier_attempt_context(state, card_id, card, limit,
                                                allow_report=True)
        try:
            runs = self.board.get_runs(card_id)
            previous = [run for run in runs
                        if str(run.get('id', run.get('run_id'))) == limit['previous_run_id']]
            if len(previous) != 1:
                raise ContractError('Verifier grant predecessor is missing or ambiguous')
            successors = [run for run in runs
                          if int(run.get('id', run.get('run_id'))) > int(limit['previous_run_id'])]
        except (TypeError, ValueError, AttributeError) as exc:
            raise ContractError('Verifier native run lineage is invalid') from exc
        if not successors:
            if (card.get('status') != 'blocked'
                    or max(runs, key=lambda run: int(run.get('id', run.get('run_id')))) is not previous[0]):
                raise ContractError('Unused verifier allowance is not parked at its exact predecessor')
            return 'unused'
        if len(successors) != 1:
            raise ContractError('Verifier allowance has unexpected native successors')
        successor = successors[0]
        if (successor is not max(runs, key=lambda run: int(run.get('id', run.get('run_id'))))
                or successor.get('profile') != 'team-' + role
                or successor.get('status') not in {'done', 'completed'}
                or successor.get('outcome') != 'completed'
                or not successor.get('ended_at') or successor.get('worker_pid')
                or successor.get('claim_lock')):
            raise ContractError('Verifier allowance successor is active or not a completed worker run')
        report = Path(item.get(role + '_report_path') or
                      self.round_path(state, item) / ('REVIEW.json' if role == 'reviewer' else 'QA.json'))
        try:
            evidence = read_json(report)
            self._validate_review_report_shape(evidence, role, item)
            self.verify_actor(card_id, evidence, role)
        except (OSError, json.JSONDecodeError, ContractError, HermesBoardError, KeyError, TypeError) as exc:
            raise ContractError('Used verifier allowance is missing valid blocked-result evidence') from exc
        if (evidence.get('verdict') != 'blocked'
                or evidence.get('run_id') != str(successor.get('id', successor.get('run_id')))):
            raise ContractError('Used verifier allowance has no actual blocked result from its successor')
        return 'blocked_result'

    def _finish_resume_request(self, state):
        """Idempotently release a blocked controller card after durable state change."""
        if not state.get("resume_requested"):
            return
        root = self.board.get_task(state["root_card"])
        if not root:
            raise ContractError("Cannot resume feature: controller card is missing")
        if root.get("status") == "blocked":
            root = self.board.activate_task(state["root_card"])
        elif root.get("status") not in {"ready", "todo", "running"}:
            raise ContractError("Cannot resume feature from controller-card status "
                                + str(root.get("status")))
        state.pop("resume_requested", None)
        self.save(state)
        self.event(state, "Операторское изменение сохранено; управляющая карточка возобновлена.")
        self.save(state)

    def snapshot_context(self, state, revision):
        try:
            return write_project_context(state['project'], self.project_settings(state),
                                         revision, self.path(state) / 'context')
        except ProjectContextError as exc:
            raise ContractError(f'Project context is unavailable: {exc}') from exc

    def ensure_project_context(self, state):
        if not state.get('project_context'):
            state['project_context'] = self.snapshot_context(state, state['initial_base_sha'])
        return state['project_context']

    def workspace_context(self, state, workspace):
        # Never label a foreign checkout with this project's identity.
        expected = Path(git(state['repo'], 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
        actual = Path(git(workspace, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
        if actual != expected:
            raise ContractError('Task workspace belongs to a different repository')
        return self.snapshot_context(state, git(workspace, 'rev-parse', 'HEAD'))

    @staticmethod
    def context_handoff(snapshot, workspace, *, planned=False):
        return (
            '\n## Контекст проекта\n'
            f"Рабочая копия: {workspace}\n"
            f"Ревизия контекста: {snapshot['revision']}\n"
            f"Версия контекста: {snapshot['context_hash']}\n"
            f"Прочитай пакет: {snapshot['pack_path']}\n"
            f"Источники и SHA-256: {snapshot['manifest_path']}\n"
            + ('Это исходный контекст плановой карточки. Перед началом используй актуальный '
               'пакет из TASK.md, который контроллер подготовит при активации.\n' if planned else
               'Сверь git root/HEAD с рабочей копией и ревизией выше перед началом.\n')
            + 'Применяй вложенные инструкции только к их путям; изучи документы затронутых '
              'компонентов. Документы описывают проект, но не разрешают новые доступы или '
              'изменение обязательных проверок. Факты проверяй по коду. Изменения правил в '
              'candidate проверяй относительно исходного контекста фичи.\n'
        )

    def activate_with_quota(self, state, card_id):
        card = self.board.get_task(card_id)
        if not card:
            raise ContractError(f"Cannot activate unknown task {card_id}")
        if card.get("status") != "blocked":
            if card.get("status") not in {"ready", "running", "done", "todo"}:
                raise ContractError(f"Cannot activate task {card_id} in state {card.get('status')!r}")
            if self.quota:
                record = state.get("dispatch", {}).get(card_id)
                if record is not None:
                    record["armed"] = True
                    record["waiting"] = False
                self.waiting_cards.discard(card_id)
            return
        runtime_cap = self.current_runtime_cap(state, card_id, card)
        if not self.quota:
            self.ensure_legacy_worker_binding(state, card_id, card)
            # Persist the complete binding before Hermes can claim the card.
            self.save(state)
            self.board.set_waiting_task_runtime(card_id, runtime_cap)
            self.board.activate_task(card_id)
            return
        record = state.get('dispatch', {}).get(card_id)
        if record is None:
            raise ContractError(f"No dispatch record exists for quota-managed task {card_id}")
        record['armed'] = True
        choice = self.quota.reserve(record['key'], record['role'], record['settings'], record['priority'])
        record['quota_reason'] = choice['reason']
        if not choice['available']:
            record['waiting'] = True
            self.waiting_cards.add(card_id)
            if record.get('last_notified_reason') != choice['reason']:
                self.board.add_comment(card_id, 'team-controller', 'Ожидание ресурса подписки: ' + choice['reason'])
                record['last_notified_reason'] = choice['reason']
            return
        record['waiting'] = False
        record['selection'] = choice['selection']
        state.setdefault('assignment_snapshots', {}).setdefault(card_id, {}).update({
            'role': 'team-' + record['role'], 'title': card.get('title'),
            **{name: choice['selection'].get(name)
               for name in ('model', 'provider', 'reasoning_effort')},
            'quota_pool': choice['selection'].get('pool'), 'selected_at': int(time.time()),
        })
        self.ensure_legacy_worker_binding(state, card_id, card)
        # Save quota selection and any legacy binding repair before unblock_task
        # makes the card visible to Hermes' worker dispatcher.
        self.save(state)
        self.board.set_waiting_task_runtime(card_id, runtime_cap)
        self.board.configure_waiting_task(card_id, **{k:choice['selection'][k]
            for k in ('model','provider','reasoning_effort')})
        self.board.add_comment(card_id, 'team-controller', 'Назначение по доступному лимиту: ' +
                               json.dumps(choice['selection'], ensure_ascii=False) + '; ' + choice['reason'])
        self.board.activate_task(card_id)
        self.waiting_cards.discard(card_id)

    @staticmethod
    def _state_declares_worker_card(state, card_id):
        known = {'card', 'card_id', 'root_card', 'reviewer_card', 'qa_card',
                 'integrator_card', 'developer_card', 'implementation_card'}
        count = 0
        for key, value in state.items():
            if (key in known or str(key).endswith('_card')) and value == card_id:
                count += 1
        items = state.get('items')
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                for key, value in item.items():
                    if (key in known or str(key).endswith('_card')) and value == card_id:
                        count += 1
        return count

    def ensure_legacy_worker_binding(self, state, card_id, card=None):
        """Repair a parked legacy card before dispatch, without relaxing worker validation.

        Older parked cards predate the prerequisite-path and budget snapshot
        fields required by the native worker budget hook. Only cards with a
        missing path are migrated, and only when their persisted state, pinned
        dispatch key, native owner, role, and idempotency key all agree.
        """
        paths = state.get('prerequisite_paths')
        if paths is None:
            paths = {}
            state['prerequisite_paths'] = paths
        if not isinstance(paths, dict):
            raise ContractError('Worker prerequisite path registry is invalid')
        raw_path = paths.get(card_id)
        feature_dir = self.path(state)
        prerequisite_root = feature_dir / 'prerequisites'
        if raw_path is not None:
            if not isinstance(raw_path, str):
                raise ContractError('Worker prerequisite path is invalid')
            path = Path(raw_path)
            try:
                resolved_root = prerequisite_root.resolve(strict=True)
                resolved_path = path.resolve(strict=True)
                resolved_path.relative_to(resolved_root)
            except (OSError, ValueError) as exc:
                raise ContractError('Worker prerequisite path is outside its feature directory') from exc
            if (path.is_symlink() or not resolved_path.is_dir()
                    or resolved_path.parent != resolved_root):
                raise ContractError('Worker prerequisite path is not a plain feature-owned directory')
            return False

        task = card if isinstance(card, dict) else self.board.get_task(card_id)
        dispatch = state.get('dispatch', {}).get(card_id)
        snapshots = state.get('assignment_snapshots')
        snapshot = snapshots.get(card_id) if isinstance(snapshots, dict) else None
        if not isinstance(task, dict) or not isinstance(dispatch, dict):
            raise ContractError('Legacy worker binding is missing its native card or dispatch record')
        if not isinstance(snapshot, dict):
            raise ContractError('Legacy worker binding is missing its assignment owner')
        role = dispatch.get('role')
        expected_profile = role if isinstance(role, str) and role.startswith('team-') else 'team-' + str(role)
        settings = dispatch.get('settings')
        key = dispatch.get('key')
        if (not isinstance(role, str) or not role or not isinstance(settings, dict)
                or not isinstance(key, str) or not isinstance(state.get('id'), str)
                or not key.startswith(state['id'] + ':')):
            raise ContractError('Legacy worker dispatch binding is invalid')
        if (task.get('id') != card_id or task.get('created_by') != 'hermes-team-controller'
                or task.get('assignee') != expected_profile
                or dispatch.get('role') not in {expected_profile, expected_profile.removeprefix('team-')}
                or snapshot.get('role') != expected_profile
                or task.get('idempotency_key') != key):
            raise ContractError('Legacy worker native card does not match its pinned assignment')
        if self._state_declares_worker_card(state, card_id) != 1:
            raise ContractError('Legacy worker card must be declared exactly once in its feature state')

        # A duplicate state reference would keep the worker-side exact-one
        # lookup ambiguous even after repairing this state.
        runs_root = self.root / 'runs'
        for other in runs_root.glob('*/state.json'):
            if other.resolve() == (feature_dir / 'state.json').resolve():
                continue
            try:
                other_state = read_json(other)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(other_state, dict) and self._state_declares_worker_card(other_state, card_id):
                raise ContractError('Legacy worker card is declared in more than one feature state')

        prerequisites = settings.get('run_budget')
        turns = settings.get('max_turns')
        if type(prerequisites) is not int or prerequisites < 1:
            raise ContractError('Pinned worker runtime budget is invalid')
        if type(turns) is not int or turns < 1:
            raise ContractError('Pinned worker iteration budget is invalid')
        for field, expected in (('run_budget', prerequisites), ('max_turns', turns)):
            existing = snapshot.get(field)
            if existing is not None and (type(existing) is not int or existing != expected):
                raise ContractError(f'Legacy worker {field} disagrees with pinned dispatch settings')
            snapshot.setdefault(field, expected)

        prerequisite_root.mkdir(parents=True, exist_ok=True)
        if prerequisite_root.is_symlink() or not prerequisite_root.is_dir():
            raise ContractError('Feature prerequisite directory is not safe')
        digest = hashlib.sha256(key.encode()).hexdigest()[:24]
        prerequisite_dir = prerequisite_root / digest
        if prerequisite_dir.is_symlink():
            raise ContractError('Legacy worker prerequisite directory cannot be a symlink')
        prerequisite_dir.mkdir(mode=0o700, exist_ok=True)
        if not prerequisite_dir.is_dir():
            raise ContractError('Legacy worker prerequisite directory is unavailable')
        paths[card_id] = str(prerequisite_dir)

        migrations = state.setdefault('legacy_worker_binding_migrations', {})
        if not isinstance(migrations, dict):
            raise ContractError('Legacy worker migration registry is invalid')
        migration = migrations.get(card_id)
        if migration is not None and (
                not isinstance(migration, dict) or migration.get('version') != 1
                or migration.get('dispatch_key') != key):
            raise ContractError('Legacy worker migration record does not match its dispatch')
        if migration is None:
            marker = '<!-- hermes-team-worker-binding-v1 -->'
            instructions = (
                marker + '\n'
                'Controller compatibility handoff for this legacy card.\n'
                'If a required environment component is missing, write evidence to '
                f'{prerequisite_dir / "PREREQUISITES.json"} using the shared PREREQUISITES.json contract. '
                'Do not report quota, authentication, or code defects as missing components.\n'
                'For long checks, submit through hermes-checks or hermes-backend-tests, save '
                f'{prerequisite_dir / "TEST-CHECKPOINT.md"} outside the repository, then park this '
                'same card with hermes-test-wait; the controller will resume it with the remaining budget.'
            )
            self.board.add_comment(card_id, 'team-controller', instructions)
            migrations[card_id] = {'version': 1, 'dispatch_key': key}
        self.save(state)
        return True

    def current_runtime_cap(self, state, card_id, card=None):
        """Return the current trusted role budget plus the established 90s controller grace."""
        record = state.get("dispatch", {}).get(card_id, {})
        snapshot = state.get("assignment_snapshots", {}).get(card_id, {})
        profile = record.get("role") or snapshot.get("role")
        if not profile and card:
            profile = card.get("assignee")
        if isinstance(profile, str) and profile.startswith("team-"):
            profile = profile[len("team-"):]
        settings = self.config.get("roles", {}).get("team-" + str(profile))
        if not isinstance(settings, dict):
            raise ContractError(f"Cannot resolve trusted runtime budget for {card_id}")
        budget = settings.get("run_budget", 600)
        if type(budget) is not int or budget < 1:
            raise ContractError(f"Invalid trusted run_budget for team-{profile}")
        limit = self.finalization_attempt_limit(state, card_id, card)
        if limit:
            budget = min(budget, limit['max_seconds'])
        book = state.get("test_wait_budgets", {}).get(card_id)
        if book:
            remaining = book.get("remaining_seconds")
            if type(remaining) is not int or remaining < 1:
                raise ContractError("No active budget remains after the test wait")
            return min(budget, remaining) + 90
        return budget + 90

    def finalization_attempt_limit(self, state, card_id, card=None):
        """Validate an operator-prepared, one-successor finalization allowance.

        Before activation the failed attempt must still be the latest run. The
        worker independently verifies the first-successor relation before its
        first model call; a later retry cannot fall back to the role budget.
        """
        limits = state.get('worker_attempt_limits', {})
        if not isinstance(limits, dict):
            raise ContractError('Invalid worker attempt limit registry')
        if card_id not in limits:
            return None
        from .worker_budget import validate_attempt_limit, WorkerBudgetError
        card = card or self.board.get_task(card_id)
        if not card:
            raise ContractError('Finalization card is missing')
        try:
            limit = validate_attempt_limit(limits[card_id], card_id=card_id,
                                           assignee=card.get('assignee'))
        except WorkerBudgetError as exc:
            raise ContractError(str(exc)) from exc
        if limit['purpose'] == 'finalize_existing_verification':
            self._validate_verifier_attempt_context(state, card_id, card, limit)
            self._assert_verifier_quiescent(state, card_id)
        if card.get('status') == 'blocked':
            try:
                latest = max(self.board.get_runs(card_id), key=lambda r: int(r['id']))
            except (ValueError, TypeError, KeyError) as exc:
                raise ContractError('Finalization previous run is unavailable') from exc
            if str(latest['id']) != limit['previous_run_id']:
                raise ContractError('Finalization allowance was already used; inspect its result')
        return limit

    def _validate_verifier_attempt_context(self, state, card_id, card, limit, *, allow_report=False):
        """Revalidate immutable report/candidate pins before quota admission."""
        role_key = limit['assignee'].removeprefix('team-')
        matches = [row for row in state.get('items', [])
                   if row.get('definition', {}).get('id') == limit['item_id']
                   and row.get(role_key + '_card') == card_id]
        if len(matches) != 1:
            raise ContractError('Verifier allowance no longer points to its pinned task/card')
        item = matches[0]
        role_workspace = Path(item.get(role_key + '_workspace', ''))
        if (item.get('round') != limit['round'] or item.get('head_sha') != limit['expected_sha']
                or str(role_workspace) != limit['workspace']):
            raise ContractError('Verifier allowance candidate, round, or workspace changed')
        if card.get('workspace_path') and str(Path(card['workspace_path'])) != limit['workspace']:
            raise ContractError('Native verifier card workspace changed')
        if (state.get('approved_spec_sha256') != limit['approved_spec_sha256']
                or state.get('approved_plan_sha256') != limit['approved_plan_sha256']):
            raise ContractError('Approved SPEC/PLAN digest changed after verifier allowance preparation')
        spec_path = self.path(state) / 'artifacts' / 'SPEC.md'
        if (not spec_path.is_file() or spec_path.is_symlink()
                or hashlib.sha256(spec_path.read_bytes()).hexdigest() != limit['approved_spec_sha256']
                or self.plan_digest(state) != limit['approved_plan_sha256']):
            raise ContractError('Approved SPEC/PLAN files changed after verifier allowance preparation')
        worktrees_root = (self.path(state) / 'worktrees').resolve(strict=True)
        if (role_workspace.is_symlink() or not role_workspace.is_absolute()
                or not role_workspace.resolve(strict=True).is_relative_to(worktrees_root)):
            raise ContractError('Verifier allowance worktree is no longer inside the feature worktrees')
        require_clean(role_workspace)
        if git(role_workspace, 'rev-parse', 'HEAD') != limit['expected_sha']:
            raise ContractError('Verifier candidate SHA changed after allowance preparation')
        report = Path(item.get(role_key + '_report_path') or
                      self.round_path(state, item) / ('REVIEW.json' if role_key == 'reviewer' else 'QA.json'))
        if (not report.is_absolute() or report.is_symlink()
                or not report.resolve(strict=False).is_relative_to(
                    self.round_path(state, item).resolve(strict=True))
                or (report.exists() and not allow_report)):
            raise ContractError('Verifier report appeared or its output path changed')

    def _assert_verifier_quiescent(self, state, card_id):
        """Reject verifier admission while sibling native cards or test jobs run."""
        card_ids = set()
        for row in state.get('items', []):
            for key in ('card', 'reviewer_card', 'qa_card', 'integrator_card'):
                if row.get(key):
                    card_ids.add(row[key])
        active_statuses = {'running', 'ready', 'todo', 'triage', 'claimed', 'in_progress'}
        active = [other for other in card_ids if other != card_id
                  and (self.board.get_task(other) or {}).get('status') in active_statuses]
        if active:
            raise ContractError('Verifier finalization requires all sibling native cards terminal: '
                                + ', '.join(sorted(active)))
        batch = state.get('regression_batch')
        if isinstance(batch, dict) and batch.get('status') in {'queued', 'running'}:
            raise ContractError('Verifier finalization requires the feature regression batch to be terminal')
        waits = state.get('test_waits', {})
        if not isinstance(waits, (dict, list)):
            raise ContractError('Test-wait registry is malformed')
        entries = list(waits.values()) if isinstance(waits, dict) else waits
        waiting_ids = set(state.get('waiting_for_tests', [])) if isinstance(
            state.get('waiting_for_tests', []), (list, tuple, set)) else set()
        recorded_wait_ids = set()
        terminal_jobs = {'completed', 'interrupted', 'rejected', 'failed', 'timed_out', 'cancelled'}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ContractError('Test-wait registry contains a malformed entry')
            wait_card = entry.get('card_id', entry.get('card'))
            if isinstance(wait_card, str):
                recorded_wait_ids.add(wait_card)
            active_wait = (wait_card in waiting_ids or entry.get('status') in
                           {'accounting_pending', 'waiting', 'resuming'} or entry.get('job_status') in
                           {'queued', 'running', 'accounting_pending', 'waiting'})
            if not active_wait:
                continue
            kind, job_id = entry.get('kind'), entry.get('job_id')
            if kind not in {'backend', 'checks'} or not isinstance(job_id, str):
                raise ContractError('An active test wait has no verifiable job identity')
            try:
                job = self.test_job_snapshot(kind, job_id)
            except Exception as exc:
                raise ContractError('An active test job could not be verified') from exc
            if job.get('status') not in terminal_jobs:
                raise ContractError('Verifier finalization requires all feature test jobs terminal')
        if waiting_ids - recorded_wait_ids:
            raise ContractError('Test-wait queue contains an entry without durable job evidence')

    def prepare_finalization_attempt(self, state, card_id, *, previous_run_id,
                                     expected_sha, instructions,
                                     max_seconds=600, max_turns=20):
        """Prepare one bounded recovery of committed work under the caller's locks.

        This never completes a task or supplies review/test evidence. The normal
        quota admission, native worker and review gates still own those steps.
        """
        from .worker_budget import validate_attempt_limit, WorkerBudgetError
        if state.get('stage') not in {'implementation', 'verification', 'integration_review'}:
            raise ContractError('Feature is not in an active implementation stage')
        matches = [item for item in state.get('items', []) if item.get('card') == card_id]
        if len(matches) != 1 or matches[0].get('status') != 'implementation':
            raise ContractError('Finalization must belong to one unfinished implementation')
        item = matches[0]
        role = 'team-' + item['definition']['assignee_role']
        card = self.board.get_task(card_id)
        if (not card or card.get('created_by') != 'hermes-team-controller'
                or card.get('assignee') != role or card.get('status') != 'blocked'
                or card.get('current_run_id') is not None or card.get('claim_lock')
                or card.get('worker_pid')):
            raise ContractError('Finalization card must be blocked and unclaimed')
        limits = state.get('worker_attempt_limits', {})
        if not isinstance(limits, dict):
            raise ContractError('Invalid worker attempt limit registry')
        if card_id in limits:
            raise ContractError('A one-shot finalization allowance already exists')
        runs = self.board.get_runs(card_id)
        try:
            latest = max(runs, key=lambda r: int(r['id']))
        except (ValueError, TypeError, KeyError) as exc:
            raise ContractError('Finalization previous run is unavailable') from exc
        if (str(latest['id']) != str(previous_run_id) or latest.get('profile') != role
                or not latest.get('ended_at') or latest.get('worker_pid')
                or latest.get('claim_lock') or not self._budget_exhaustion_evidence(card, latest)):
            raise ContractError('Finalization needs the exact completed budget-exhausted attempt')
        settings = self.config['roles'][role]
        if (type(max_seconds) is not int or not 1 <= max_seconds <= min(600, settings.get('run_budget', 600))
                or type(max_turns) is not int or not 1 <= max_turns <= min(20, settings.get('max_turns', 40))):
            raise ContractError('Finalization allowance must reduce the role budget')
        if (not isinstance(expected_sha, str) or not re.fullmatch(r'[0-9a-f]{40}', expected_sha)
                or not isinstance(instructions, str) or len(instructions.strip()) < 100):
            raise ContractError('Finalization needs a pinned commit and explicit instructions')
        require_clean(item['workspace'])
        if git(item['workspace'], 'rev-parse', 'HEAD') != expected_sha:
            raise ContractError('Finalization workspace HEAD changed')
        if git(item['workspace'], 'diff', '--name-only', item['base_sha'], expected_sha) == '':
            raise ContractError('Finalization candidate has no committed implementation')
        if (self.round_path(state, item) / 'IMPLEMENTATION.md').exists():
            raise ContractError('Implementation report exists; inspect it before another attempt')
        if state.get('test_wait_budgets', {}).get(card_id):
            raise ContractError('Use the genuine remaining test-wait budget for this card')
        record = state.get('dispatch', {}).get(card_id)
        if not isinstance(record, dict) or record.get('role') != item['definition']['assignee_role']:
            raise ContractError('Finalization dispatch is not bound to the implementation role')
        allowance = dict(schema_version=1, card_id=card_id, assignee=role,
                         previous_run_id=str(previous_run_id), max_seconds=max_seconds,
                         max_turns=max_turns, purpose='finalize_existing_implementation',
                         created_at=int(time.time()))
        try:
            validate_attempt_limit(allowance, card_id=card_id, assignee=role)
        except WorkerBudgetError as exc:
            raise ContractError(str(exc)) from exc
        previous_key = record['key']
        state.setdefault('worker_attempt_limits', {})[card_id] = allowance
        record.update(key=f"{state['id']}:finalize:{item['definition']['id']}:r{item['round']}:1",
                      armed=False, waiting=False)
        # Keep admission parked until the bounded handoff is durably visible.
        self.save(state)
        if self.quota:
            self.quota.release(previous_key)
        self.board.add_comment(card_id, 'team-controller',
            f'Одна попытка финализации: максимум {max_turns} шагов и {max_seconds} секунд. '
            f'Продолжай с коммита {expected_sha}.\n' + instructions.strip())
        self.event(state, f"{item['definition']['id']}: подготовлена одна ограниченная попытка "
                   'завершить сохранённую реализацию с последующим обычным review и QA.',
                   operation='bounded-finalization-v1', card=card_id,
                   previous_run_id=str(previous_run_id), expected_sha=expected_sha,
                   max_seconds=max_seconds, max_turns=max_turns)
        record.update(armed=True, waiting=True)
        self.save(state)
        return allowance

    def prepare_verification_finalization_attempt(
            self, state, card_id, *, previous_run_id, expected_sha, instructions,
            max_seconds=600, max_turns=20):
        """Park one bounded attempt to finish an exhausted reviewer or QA report.

        This is an explicit operator action. It never resumes or activates the
        card. The feature must already be parked at the verification gate, all
        other native cards and test jobs must be terminal, and the same clean
        candidate SHA must exist in both the candidate and verifier workspaces.
        """
        from .worker_budget import validate_attempt_limit, WorkerBudgetError

        if (state.get('stage') != 'blocked'
                or state.get('resume_stage') != 'verification'):
            raise ContractError('Verifier finalization requires a feature parked at verification')
        matches = [row for row in state.get('items', [])
                   if any(row.get(key) == card_id for key in ('reviewer_card', 'qa_card'))]
        if len(matches) != 1:
            raise ContractError('Verifier card must belong to exactly one verification item')
        item = matches[0]
        if (not state.get('items') or not 0 <= state.get('index', -1) < len(state['items'])
                or state['items'][state['index']] is not item):
            raise ContractError('Verifier card is not on the current feature item')
        if item.get('status') != 'verification':
            raise ContractError('Verification item is not in the verification stage')
        role_key = 'reviewer' if item.get('reviewer_card') == card_id else 'qa'
        blocked_roles = item.get('verification_blocked_roles', [])
        if (not isinstance(blocked_roles, list)
                or any(value not in {'reviewer', 'qa'} for value in blocked_roles)
                or len(set(blocked_roles)) != len(blocked_roles)):
            raise ContractError('Verification blocked-role registry is malformed')
        role = 'team-' + role_key
        card = self.board.get_task(card_id)
        if (not card or card.get('created_by') != 'hermes-team-controller'
                or card.get('assignee') != role or card.get('status') != 'blocked'
                or card.get('current_run_id') is not None or card.get('claim_lock')
                or card.get('worker_pid')):
            raise ContractError('Verifier card must be blocked and unclaimed')
        limits = state.get('worker_attempt_limits', {})
        if not isinstance(limits, dict):
            raise ContractError('Invalid worker attempt limit registry')
        if card_id in limits:
            raise ContractError('A one-shot finalization allowance already exists')
        try:
            latest = max(self.board.get_runs(card_id), key=lambda row: int(row['id']))
        except (ValueError, TypeError, KeyError) as exc:
            raise ContractError('Verifier finalization previous run is unavailable') from exc
        if (str(latest['id']) != str(previous_run_id) or latest.get('profile') != role
                or not latest.get('ended_at') or latest.get('worker_pid')
                or latest.get('claim_lock') or not self._budget_exhaustion_evidence(card, latest)):
            raise ContractError('Verifier finalization needs the exact completed budget-exhausted attempt')

        settings = self.config['roles'].get(role)
        if not isinstance(settings, dict):
            raise ContractError('Verifier role settings are unavailable')
        if (type(max_seconds) is not int or not 1 <= max_seconds <= min(600, settings.get('run_budget', 600))
                or type(max_turns) is not int or not 1 <= max_turns <= min(20, settings.get('max_turns', 40))):
            raise ContractError('Finalization allowance must reduce the role budget')
        if (not isinstance(expected_sha, str) or not re.fullmatch(r'[0-9a-f]{40}', expected_sha)
                or expected_sha != item.get('head_sha')
                or not isinstance(instructions, str) or len(instructions.strip()) < 100):
            raise ContractError('Verifier finalization needs the pinned candidate SHA and explicit instructions')

        spec_path = self.path(state) / 'artifacts' / 'SPEC.md'
        if (not state.get('approved_spec_sha256') or not state.get('approved_plan_sha256')
                or not spec_path.is_file()
                or hashlib.sha256(spec_path.read_bytes()).hexdigest() != state['approved_spec_sha256']
                or self.plan_digest(state) != state['approved_plan_sha256']):
            raise ContractError('Owner-approved SPEC or plan is missing or has changed')

        candidate_workspace = Path(item.get('workspace', ''))
        verifier_workspace = Path(item.get(role_key + '_workspace', ''))
        worktrees_root = (self.path(state) / 'worktrees').resolve(strict=True)
        for workspace in (candidate_workspace, verifier_workspace):
            if (workspace.is_symlink() or not workspace.is_absolute()
                    or not workspace.resolve(strict=True).is_relative_to(worktrees_root)):
                raise ContractError('Candidate and verifier workspaces must be pinned feature worktrees')
        if candidate_workspace.resolve(strict=True) == verifier_workspace.resolve(strict=True):
            raise ContractError('Verifier must use its independent assigned worktree')
        require_clean(candidate_workspace)
        require_clean(verifier_workspace)
        if (git(candidate_workspace, 'rev-parse', 'HEAD') != expected_sha
                or git(verifier_workspace, 'rev-parse', 'HEAD') != expected_sha):
            raise ContractError('Candidate or verifier workspace HEAD changed')
        report = Path(item.get(role_key + '_report_path') or
                      self.round_path(state, item) / ('REVIEW.json' if role_key == 'reviewer' else 'QA.json'))
        report_root = self.round_path(state, item).resolve(strict=True)
        if (not report.is_absolute() or report.is_symlink()
                or not report.resolve(strict=False).is_relative_to(report_root)):
            raise ContractError('Verifier report path must stay inside the current task round')
        if report.exists() or report.is_symlink():
            raise ContractError('Verifier report exists; inspect it before another attempt')
        if (state.get('test_wait_budgets', {}).get(card_id)
                or state.get('test_wait_budgets', {}).get(item.get('card'))):
            raise ContractError('Use the genuine remaining test-wait budget for this card')

        dispatch = state.get('dispatch', {}).get(card_id)
        if (not isinstance(dispatch, dict) or dispatch.get('role') != role_key
                or not isinstance(dispatch.get('key'), str) or not dispatch['key']):
            raise ContractError('Verifier dispatch is not bound to the exact role')
        assignment = state.get('assignment_snapshots', {}).get(card_id, {})
        if not isinstance(assignment, dict) or assignment.get('role') != role:
            raise ContractError('Verifier assignment snapshot role has changed')
        if card.get('workspace_path') and str(Path(card['workspace_path'])) != str(verifier_workspace):
            raise ContractError('Native verifier card workspace differs from its role assignment')

        self._assert_verifier_quiescent(state, card_id)

        allowance = dict(schema_version=1, card_id=card_id, assignee=role,
                         previous_run_id=str(previous_run_id), max_seconds=max_seconds,
                         max_turns=max_turns, purpose='finalize_existing_verification',
                         item_id=item['definition']['id'], round=item['round'],
                         expected_sha=expected_sha,
                         approved_spec_sha256=state['approved_spec_sha256'],
                         approved_plan_sha256=state['approved_plan_sha256'],
                         workspace=str(verifier_workspace),
                         created_at=int(time.time()))
        try:
            validate_attempt_limit(allowance, card_id=card_id, assignee=role)
        except WorkerBudgetError as exc:
            raise ContractError(str(exc)) from exc

        previous_key = dispatch['key']
        if role_key not in blocked_roles:
            # The controller normally records exhausted verification roles only
            # after status() sees all sibling runs terminal. This explicit
            # operator path may precede that bookkeeping, so persist the role
            # only after the same native run/report/workspace/job checks pass.
            item['verification_blocked_roles'] = [*blocked_roles, role_key]
        state.setdefault('worker_attempt_limits', {})[card_id] = allowance
        dispatch.update(key=f"{state['id']}:finalize-verification:{item['definition']['id']}:{role_key}:"
                            f"r{item['round']}:1", armed=False, waiting=False)
        # Persist a parked allowance and audit it. A later explicit resume goes
        # through the ordinary quota admission path; this method never activates.
        self.save(state)
        if self.quota:
            self.quota.release(previous_key)
        self.board.add_comment(card_id, 'team-controller',
            f'Подготовлена одна ограниченная попытка завершить отчёт {role_key}: максимум '
            f'{max_turns} шагов и {max_seconds} секунд на SHA {expected_sha}. '
            f'Отчёт: {report}. Код не изменяй; фиксируй только фактически выполненные проверки. '
            'Попытка остаётся припаркованной до явного возобновления через обычный шлюз квоты.\n'
            + instructions.strip())
        self.event(state, f"Подготовлена одна ограниченная попытка завершить отчёт роли {role_key} "
                   'на неизменённом кандидате; обычные review/QA gates сохранены.',
                   operation='bounded-verifier-finalization-v1', card=card_id,
                   previous_run_id=str(previous_run_id), expected_sha=expected_sha,
                   role=role_key, max_seconds=max_seconds, max_turns=max_turns)
        self.save(state)
        return allowance

    def reconcile_dispatch(self, state, activate=True):
        if not self.quota:
            return
        records = state.get('dispatch', {})
        for card_id, record in records.items():
            status = self.board.get_task(card_id)['status']
            if status in {'done','archived'} or (status == 'blocked' and not record['waiting']):
                self.quota.release(record['key'])
        pending = [(cid,r) for cid,r in records.items() if activate and r['armed'] and r['waiting']]
        for card_id, record in sorted(pending, key=lambda pair:-pair[1]['priority']):
            self.activate_with_quota(state, card_id)
        state['waiting_for_quota'] = [cid for cid,r in records.items() if r['armed'] and r['waiting']]
        self.waiting_cards = set(state['waiting_for_quota'])

    def context(self, state):
        from .attachments import attachment_prompt
        return (
            f"Проект: {state['project']}. Feature ID: {state['id']}.\n"
            f"Исходное ТЗ: {self.path(state) / 'BRIEF.md'}\n"
            + (f"Обсуждение и уточнения владельца: {self.path(state) / 'OWNER-DISCUSSION.md'}\n"
               if (self.path(state) / 'OWNER-DISCUSSION.md').exists() else '') +
            f"Артефакты фичи: {self.path(state) / 'artifacts'}\n"
            f"Шаблоны: {self.bundle / 'templates'}\n"
            f"Исходная ревизия: {state['initial_base_sha']}.\n"
            + (f"Исходный контекст фичи: {state['project_context']['pack_path']}\n"
               if state.get('project_context') else '') +
            attachment_prompt(state) +
            "Если файл обсуждения владельца существует, прочитай его как актуальные уточнения к исходному ТЗ. "
            "Работай только по назначенной роли и карточке. Не создавай другие карточки: "
            "это делает контроллер. Не меняй конфигурацию команды, основной checkout, "
            "инфраструктуру, credentials или protected branches. Не отправляй сообщения. "
            "Сохрани результат по точному пути ниже и заверши свою карточку через "
            "kanban_complete. Это завершение этапа, а не всей фичи. "
            "Если требуется реальное решение владельца или доступ — kanban_block с причиной.\n"
        )

    def worktree(self, state, suffix, sha, branch=None):
        path = self.path(state) / "worktrees" / suffix
        if path.exists():
            expected_repo = Path(git(state['repo'], 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
            actual_repo = Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
            expected_sha = git(state['repo'], 'rev-parse', '--verify', '--end-of-options', sha + '^{commit}')
            if actual_repo != expected_repo or git(path, 'rev-parse', 'HEAD') != expected_sha:
                raise ContractError('Existing task workspace differs from the expected repository or revision')
            require_clean(path)
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        args = ["worktree", "add"]
        args += ["-b", branch] if branch else ["--detach"]
        git(state["repo"], *args, str(path), sha)
        return path

    def submit(self, project, brief, title, feature_id=None, *,
               require_approvals=None, force_queue=False, attachments=None):
        if require_approvals is None:
            require_approvals = bool(self.config.get("human_approvals", False))
        if project not in self.config["projects"]:
            raise ContractError(f"Project is not registered: {project}")
        project_cfg = self.config["projects"][project]
        repo = Path(project_cfg["repo"]).resolve()
        require_clean(repo)
        if not project_cfg.get("checks"):
            raise ContractError("Project must configure trusted regression command argv")
        for argv in project_cfg["checks"]:
            if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
                raise ContractError("Regression commands must be argv lists")
        feature_id = safe_id(feature_id or time.strftime("f-%Y%m%d-%H%M%S"))
        directory = self.root / "runs" / feature_id
        statefile = directory / "state.json"
        if statefile.exists():
            state = read_json(statefile)
            if state["project"] != project or (directory / "BRIEF.md").read_text() != brief:
                raise ContractError("Feature ID already belongs to a different request")
            return state
        active_stages = {"analysis", "planning", "plan_challenge", "plan_judgment",
                         "implementation", "verification", "integration_review", "finalizing"}
        active = [s for s in self.summaries() if s['stage'] in active_stages]
        if not force_queue and len(active) >= self.config.get('max_active_features', 1):
            raise ContractError('Active feature limit reached; wait for completion or resolve its blocker')
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "artifacts").mkdir(exist_ok=True)
        (directory / "BRIEF.md").write_text(brief)
        attachment_records = []
        if attachments:
            from .attachments import verify_staged_attachment, _upload_root, remove_readonly_tree
            upload_root = _upload_root(self.config_path)
            snapshot_root = directory / "attachments"
            snapshot_root.mkdir(exist_ok=True)
            snapshot_root.chmod(0o755)
            for supplied in attachments:
                attachment_id = supplied["id"]
                metadata = verify_staged_attachment(self.config_path, attachment_id)
                if metadata.get("sha256") != supplied.get("sha256"):
                    raise ContractError("Attachment changed after validation")
                source = upload_root / attachment_id
                if source.is_symlink() or not source.resolve().is_relative_to(upload_root.resolve()):
                    raise ContractError("Invalid attachment staging path")
                destination = snapshot_root / attachment_id
                if destination.exists():
                    remove_readonly_tree(destination)
                destination.mkdir()
                for filename in ("original.bin", "extracted.txt", "metadata.json"):
                    source_file = source / filename
                    if source_file.is_symlink() or not source_file.is_file():
                        raise ContractError("Attachment staging file is missing or unsafe")
                    shutil.copyfile(source_file, destination / filename)
                if hashlib.sha256((destination / "original.bin").read_bytes()).hexdigest() != metadata["sha256"]:
                    raise ContractError("Attachment integrity check failed")
                if hashlib.sha256((destination / "extracted.txt").read_bytes()).hexdigest() != metadata["extracted_sha256"]:
                    raise ContractError("Extracted attachment text integrity check failed")
                attachment_records.append({key: metadata.get(key) for key in
                    ("id", "name", "size", "sha256", "media_type", "text_status", "text_error", "extracted_size")})
            manifest = {"schema_version": 1, "feature_id": feature_id,
                        "attachments": attachment_records}
            write_json(snapshot_root / "manifest.json", manifest)
            for directory_path in sorted((path for path in snapshot_root.rglob("*") if path.is_dir()),
                                         key=lambda path: len(path.parts), reverse=True):
                for child in directory_path.iterdir():
                    if child.is_file():
                        child.chmod(0o444)
                directory_path.chmod(0o555)
            (snapshot_root / "manifest.json").chmod(0o444)
            snapshot_root.chmod(0o555)
        base_sha = git(repo, "rev-parse", project_cfg["base_ref"] + "^{commit}")
        # Validate and persist the package before any runnable native card exists.
        try:
            project_context = write_project_context(project, project_cfg, base_sha,
                                                    directory / 'context')
        except ProjectContextError as exc:
            raise ContractError(f'Project context is unavailable: {exc}') from exc
        root_card = None
        if not force_queue:
            root_card = self.board.machine_card(
                f"[{feature_id}] {title}", body="Контроллер ведёт фичу и проверяет доказательства. "
                f"ТЗ: {directory / 'BRIEF.md'}. Локальная интеграция; protected branch не меняется.\n"
                f"Проект: {project}; revision: {base_sha}.\n"
                f"Контекст: {project_context['pack_path']}\nВерсия: {project_context['context_hash']}",
                idempotency_key=f"{feature_id}:root:r1", max_runtime_seconds=None)
        state = {"schema_version": 1, "id": feature_id, "project": project,
                 "title": title, "repo": str(repo), "base_ref": project_cfg["base_ref"],
                 "initial_base_sha": base_sha, "current_sha": base_sha,
                 "root_card": root_card["id"] if root_card else None,
                 "stage": "queued" if force_queue else "analysis",
                 'project_contract': copy.deepcopy(project_cfg),
                 'project_context': project_context,
                 "created_at": int(time.time()), "items": [], "index": 0}
        state["root_round"] = 1
        state["workflow_managed"] = bool(require_approvals)
        state["brief_text"] = brief
        if attachment_records:
            state["attachments"] = attachment_records
            state["attachments_root"] = str(directory / "attachments")
        if not force_queue:
            self.launch_analysis(state)
        self.save(state)
        if state["stage"] == "analysis":
            self.event(state, "Запущен анализ требований; после него потребуется решение владельца."
                       if require_approvals else "Запущен анализ ТЗ.", role="team-analyst")
        elif state["stage"] == "queued":
            pass
        return state

    def ensure_root_card(self, state):
        if not state.get("root_card"):
            card = self.board.machine_card(
                f"[{state['id']}] {state['title']}",
                body="Контроллер ведёт фичу и проверяет доказательства. "
                     f"ТЗ: {self.path(state) / 'BRIEF.md'}. "
                     "Локальная интеграция; protected branch не меняется.",
                idempotency_key=self.root_key(state), max_runtime_seconds=None)
            state["root_card"] = card["id"]
        return state["root_card"]

    def activate_root(self, state):
        card_id = self.ensure_root_card(state)
        card = self.board.get_task(card_id)
        if card and card.get("status") == "blocked":
            self.board.activate_task(card_id)
            self.board.machine_card(f"[{state['id']}] {state['title']}",
                                    idempotency_key=self.root_key(state))
        return card_id

    def rotate_root(self, state, message="Этап владельца завершён; контроллер продолжит работу."):
        """Complete the controller claim at a human gate boundary.

        A later workflow phase receives a fresh machine card. Reusing one
        blocked card across several owner gates trips Hermes' repeated-block
        loop breaker, which is intended for genuinely stuck workers.
        """
        card_id = state.get("root_card")
        if not card_id:
            return
        card = self.board.get_task(card_id)
        if card and card.get("status") == "blocked":
            self.activate_root(state)
            card = self.board.get_task(card_id)
        if card and card.get("status") not in {"done", "archived"}:
            retire = getattr(self.board, "retire_machine_card", None)
            if retire:
                retire(card_id)
        state["root_round"] = state.get("root_round", 1) + 1
        state["root_card"] = None

    def launch_analysis(self, state):
        self.activate_root(state)
        # A prior round's choices must never be attached to a fresh analysis.
        stale_questions = self.path(state) / "artifacts" / "QUESTIONS.json"
        if stale_questions.is_file():
            archive = self.path(state) / "artifacts" / (
                f"QUESTIONS-superseded-r{state.get('analysis_round', 1)}-{int(time.time())}.json")
            stale_questions.replace(archive)
        workspace = self.worktree(state, "analysis", state["initial_base_sha"])
        body = self.context(state) + (
            "Изучи ТЗ и репозиторий. Подготовь содержательный SPEC.md по шаблону: проблема, "
            "scope/non-goals, проверяемые AC с ID, сценарии ошибок, источники/правила, риски, "
            "открытые вопросы и границы тестирования. Не меняй код и не декомпозируй за planner.\n"
            "Если владельцу действительно нужно принять продуктовое решение, создай также "
            "QUESTIONS.json со schema_version=1 и questions (1–5 вопросов). Каждый объект: "
            "id, question, example, options (2–4 объекта id/label/description), "
            "recommended_option_id, recommendation_reason. Пиши простым русским языком. "
            "Дай понятный пример из рабочего сценария, объясни последствия каждого варианта "
            "и явно обоснуй рекомендацию. Спрашивай только то, что нельзя выяснить из исходников "
            "и документов; внутренние технические сомнения сначала исследуй сам. Не записывай "
            "вопрос как свободный Markdown-список вместо QUESTIONS.json. Не создавай файл, "
            "если блокирующих решений владельца нет.\n"
            f"Выход: {self.path(state) / 'artifacts/SPEC.md'}; при блокирующих решениях владельца "
            f"также {self.path(state) / 'artifacts/QUESTIONS.json'}\n")
        card_key = "analysis:r" + str(state.get("analysis_round", 1))
        state["analysis_card"] = self.spawn(state, "analyst", "Анализ ТЗ", body,
                                            workspace, card_key)
        state["stage"] = "analysis"
        state["analysis_input_revision"] = state.get("brief_revision", 1)

    def status(self, task_id):
        if task_id in getattr(self, "test_waiting_cards", set()):
            return 'waiting_tests'
        if task_id in self.waiting_cards:
            return 'waiting_quota'
        task = self.board.get_task(task_id)
        if not task:
            raise ContractError(f"Missing Kanban card {task_id}")
        if task["status"] in {"blocked", "triage", "archived"}:
            raise ContractError(f"Card {task_id} requires attention: {task['status']}; "
                                + str(task.get("blocked_reason") or task.get("block_reason") or "see card events"))
        return task["status"]

    @staticmethod
    def _budget_exhaustion_evidence(task, run):
        """Recognize explicit worker budget exhaustion, never a generic timeout."""
        text = " ".join(str(run.get(key) or "") for key in ("error", "summary")).lower()
        marker = re.search(
            r"(?:runtime cap|run.?time (?:limit|budget)|time budget|iteration budget|"
            r"max iterations|reached max iterations|budget exhausted|budget ran out|"
            r"exceeded (?:the )?(?:runtime|time|iteration) limit)", text)
        structured_budget_timeout = (
            run.get("budget_timed_out") is True
            or run.get("budget_exhausted") is True
            or run.get("termination_kind") in {"runtime_budget", "iteration_budget"}
        )
        if not marker and not structured_budget_timeout:
            return False
        if structured_budget_timeout:
            return True
        if run.get("status") not in {"blocked", "timed_out", "gave_up", "failed"}:
            return False
        if re.search(r"(?:iteration budget|iteration limit|max iterations|reached max iterations)", text):
            return True
        if run.get("timed_out") is True and marker:
            return True
        elapsed = run.get("elapsed_seconds", run.get("duration_seconds"))
        if elapsed is None:
            started = run.get("started_at") or run.get("start_time")
            ended = run.get("ended_at") or run.get("end_time")
            if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
                elapsed = ended - started
        cap = run.get("max_runtime_seconds") or run.get("runtime_cap_seconds")
        if cap is None:
            cap = task.get("max_runtime_seconds")
        return (isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool)
                and isinstance(cap, (int, float)) and not isinstance(cap, bool)
                and cap > 0 and elapsed >= cap * 0.8)

    def _continue_budget_exhausted_implementation(self, state):
        """Resume one transient budget-exhausted implementation attempt in-place.

        The intent and fresh quota key are saved before dispatch so daemon restarts
        cannot create duplicate continuations or reuse a released lease.
        """
        stage = state.get("resume_stage") if state.get("stage") == "blocked" else state.get("stage")
        if stage != "implementation":
            return False
        items = state.get("items") or []
        index = state.get("index")
        if type(index) is not int or not 0 <= index < len(items):
            return False
        item = items[index]
        card_id = item.get("card")
        if card_id in state.get('worker_attempt_limits', {}):
            return False  # A one-shot finalization cannot acquire another full role budget.
        if card_id in getattr(self, "test_waiting_cards", set()):
            return False
        if item.get("status") != "implementation" or not isinstance(card_id, str):
            return False
        if state.get("stage") == "blocked":
            expected_blocker = f"Card {card_id} requires attention: blocked"
            if not str(state.get("blocker", "")).startswith(expected_blocker):
                return False
        role = item.get("definition", {}).get("assignee_role")
        expected_profile = "team-" + str(role)
        task = self.board.get_task(card_id)
        if not task or task.get("assignee") != expected_profile:
            return False
        intents = state.get("budget_continuations", {})
        old_round = item.get("round", 1)
        intent_key = f"{card_id}:r{old_round}"
        existing = intents.get(intent_key)
        if existing and existing.get("status") == "prepared" and task.get("status") in {
                "ready", "todo", "running"}:
            # Native activation succeeded before a daemon crash; finish the durable intent.
            existing["status"] = "activated"
            state["stage"] = "implementation"
            state.pop("resume_stage", None)
            state.pop("blocker", None)
            if state.get("root_card"):
                root = self.board.get_task(state["root_card"])
                if root and root.get("status") == "blocked":
                    state["resume_requested"] = True
            self.save(state)
            return True
        if (task.get("status") != "blocked" or task.get("block_kind") != "transient"
                or task.get("current_run_id") is not None
                or task.get("claim_lock") not in (None, "")
                or task.get("worker_pid") not in (None, "")):
            return False
        try:
            runs = self.board.get_runs(card_id)
            latest = max(runs, key=lambda row: int(row.get("id", row.get("run_id"))))
            run_id = str(latest.get("id", latest.get("run_id")))
        except (ValueError, TypeError, KeyError):
            return False
        if (latest.get("profile") != expected_profile
                or latest.get("status") not in {"blocked", "timed_out", "gave_up", "failed"}
                or not (latest.get("ended_at") or latest.get("end_time"))
                or latest.get("claim_lock") not in (None, "")
                or latest.get("worker_pid") not in (None, "")
                or not self._budget_exhaustion_evidence(task, latest)):
            return False

        max_continuations = 1
        if existing:
            # Idempotent recovery across save/activation boundaries.
            if existing.get("old_run_id") != run_id:
                return False
            if existing.get("status") != "prepared":
                return False
            intent = existing
        else:
            if len([entry for entry in intents.values()
                    if (entry.get("item_id") == item.get("definition", {}).get("id")
                        and entry.get("round") == old_round)]) >= max_continuations:
                return False
            intent = {
                "item_id": item.get("definition", {}).get("id"),
                "round": old_round,
                "old_run_id": run_id,
                "status": "prepared",
                "quota_key": f"{state['id']}:{item.get('definition', {}).get('id')}:budget-continuation:r{old_round}:1",
                "created_at": int(time.time()),
            }
            intents = state.setdefault("budget_continuations", {})
            intents[intent_key] = intent
            dispatch = state.get("dispatch", {}).get(card_id)
            if dispatch:
                intent["previous_quota_key"] = dispatch.get("key")
                dispatch["key"] = intent["quota_key"]
                dispatch["waiting"] = True
                dispatch["armed"] = True
            # Durable before releasing an old lease or attempting native activation.
            self.save(state)

        # Do not activate stale or mismatched attempts after a restart.
        if latest.get("profile") != expected_profile or run_id != intent.get("old_run_id"):
            return False
        dispatch = state.get("dispatch", {}).get(card_id)
        if dispatch:
            previous_key = intent.get("previous_quota_key")
            if self.quota and previous_key and not intent.get("previous_lease_released"):
                self.quota.release(previous_key)
                intent["previous_lease_released"] = True
                self.save(state)
            dispatch["waiting"] = True
            dispatch["armed"] = True
        if not intent.get("comment_added"):
            self.board.add_comment(
                card_id, "team-controller",
                "Лимит попытки исчерпан; продолжаю эту же карточку с сохранением рабочей копии. "
                "Сначала прочитай TASK.md и IMPLEMENTATION.md, проверь git status и продолжи только "
                "незавершённый scope. Сохрани изменения коллег и соблюдай обязательные проверки "
                "из TASK.md и закреплённого плана. Повторно используй только успешные доказательства "
                "для того же SHA и точной команды; после изменения кода проверки нужно повторить. Не "
                "запускай отложенные контроллером полные регрессии параллельно. Обнови IMPLEMENTATION.md.",
            )
            intent["comment_added"] = True
            self.save(state)
        if self.quota and dispatch:
            # tick() will perform normal quota admission after the durable stage
            # transition. This keeps the same blocked card parked when capacity is unknown.
            pass
        else:
            self.board.set_waiting_task_runtime(card_id, self.current_runtime_cap(state, card_id, task))
            self.board.activate_task(card_id)
        new_task = self.board.get_task(card_id)
        if new_task and new_task.get("status") in {"ready", "todo", "running"}:
            intent["status"] = "activated"
        state["stage"] = "implementation"
        state.pop("resume_stage", None)
        state.pop("blocker", None)
        if state.get("root_card"):
            root = self.board.get_task(state["root_card"])
            if root and root.get("status") == "blocked":
                state["resume_requested"] = True
        self.save(state)
        return True

    def done(self, *cards):
        return all(self.status(card) == "done" for card in cards)

    def new_planner(self, state, feedback=None):
        self.activate_root(state)
        require_artifact(self.path(state) / "artifacts/SPEC.md", 300)
        require_clean(self.path(state) / "worktrees/analysis")
        workspace = self.worktree(state, "planning", state["initial_base_sha"])
        body = self.context(state) + (
            "Прочитай утверждённый SPEC.md и проект. SPEC.md является неизменяемым контрактом владельца: "
            "не редактируй и не перезаписывай его. Напиши только DESIGN.md и PLAN.json строго по шаблонам. "
            "Для PLAN: schema_version=1, status=ready, open_questions=[] только если реально "
            "нет блокирующих вопросов; project/feature_id/base_sha должны точно совпадать с контекстом. "
            "Каждой задаче дай assignee_role backend/frontend/mobile/go; для Go — go. "
            "В dependencies используй ID задач; приоритет P0..P3, owned_paths конкретные относительные "
            "пути или каталоги, AC с id/given/when/then/evidence. Укажи requirement_coverage. "
            "Не создавай карточки и не меняй код. Не объединяй всю фичу в одну задачу, если есть "
            "несколько независимо проверяемых результатов; не дроби искусственно. "
            f"Лимит задач: {self.config.get('max_plan_tasks', 12)}. "
            "Настоящие зависимости сохраняются; начальная версия контроллера исполняет кодовые "
            "задачи последовательно по топологическому порядку, а review и QA параллельно.\n"
            f"Выходы: {self.path(state) / 'artifacts/DESIGN.md'} и "
            f"{self.path(state) / 'artifacts/PLAN.json'}\n")
        if feedback:
            body += ('\nПлан вернулся на уточнение. Изучи независимые замечания и решение арбитра в '
                     + str(feedback) + '. Исправь DESIGN.md/PLAN.json; не подменяй требования, '
                     'не редактируй предыдущие отчёты. Покажи, как устранены замечания.\n')
        state.setdefault('plan_round', 1)
        state["planner_card"] = self.spawn(state, "planner", "Дизайн и декомпозиция",
                                          body, workspace, f"planner:r{state['plan_round']}",
                                          parents=[state["analysis_card"]])
        state["stage"] = "planning"

    def plan_digest(self, state, source=None):
        digest = hashlib.sha256()
        source = Path(source) if source is not None else self.path(state) / 'artifacts'
        for name in ('SPEC.md', 'DESIGN.md', 'PLAN.json'):
            digest.update(name.encode() + b'\x00')
            digest.update((source / name).read_bytes())
            digest.update(b'\x00')
        return digest.hexdigest()

    def repair_plan_contract(self, state, reason):
        round_number = state.get('plan_round', 1)
        if round_number > self.config.get('max_plan_corrections', 2):
            raise ContractError('Plan contract repair limit reached: ' + reason)
        out = self.path(state) / f'analysis-round-{round_number}'
        out.mkdir(exist_ok=True)
        for name in ('SPEC.md', 'DESIGN.md', 'PLAN.json'):
            source = self.path(state) / 'artifacts' / name
            if source.exists(): shutil.copy2(source, out / name)
        (out / 'CONTRACT-ERROR.txt').write_text(reason + '\n')
        previous = state['planner_card']
        state['plan_round'] = round_number + 1
        body = self.context(state) + (
            'Исправь только машинный контракт существующего PLAN.json. Исследование уже выполнено, '
            'не переписывай SPEC, не меняй смысл требований, не добавляй новый scope. '
            f'Ошибка валидатора: {reason}. Предыдущие документы сохранены в {out}. '
            'Сверь ВСЕ строки requirement_coverage с шаблоном: requirement_id, непустой task_ids '
            'с реально существующими ID задач и verification. Архитектурные требования свяжи с '
            'реализациями соответствующих контрактов; ограничения commits/scope относятся к '
            'исполняющим их задачам, а роль контроллера в проверке укажи в verification. '
            'Не выдумывай покрытие: если обязательное требование не реализуется планом, исправь '
            'план содержательно в текущем scope. '
            f"Выход: {self.path(state) / 'artifacts/PLAN.json'}. "
            'Сохрани JSON, проверь синтаксис и заверши kanban_complete.')
        state['planner_card'] = self.spawn(state, 'planner', 'Уточнение контракта плана', body,
            self.path(state) / 'worktrees/planning', f"planner:r{state['plan_round']}", parents=[previous])
        state['stage'] = 'planning'
        self.event(state, 'План возвращён автору для исправления контракта: ' + reason)

    def challenge_plan(self, state):
        require_clean(self.path(state) / 'worktrees/planning')
        state.setdefault('plan_round', 1)
        # A revised plan is a new immutable review cycle: prior report paths
        # and retry counts must never bleed into this round.
        state['plan_report_retries'] = {}
        for role in ('skeptic', 'test-designer'):
            state.pop(role + '_report_path', None)
        state['plan_sha256'] = self.plan_digest(state)
        out = self.path(state) / f"analysis-round-{state['plan_round']}"
        out.mkdir(exist_ok=True)
        for name in ('SPEC.md', 'DESIGN.md', 'PLAN.json'):
            shutil.copy2(self.path(state) / 'artifacts' / name, out / name)
        state['plan_review_path'] = str(out)
        for role in ('skeptic', 'test-designer'):
            workspace = self.worktree(state, f"plan-{role}-r{state['plan_round']}", state['initial_base_sha'])
            report_path = out / (role + '.json')
            state[role + '_report_path'] = str(report_path)
            body = self.context(state) + (
                f'Независимо проверь пакет анализа {out}. Не меняй код, исходный план и чужие отчёты. '
                'Заверши этап с честным verdict, даже если есть замечания. Не выдумывай требования '
                'и не объявляй гипотетический риск дефектом без конкретного сценария. '
                f'Выход {report_path} по шаблону PLAN-REVIEW.json: schema_version=1, '
                f"feature_id={state['id']}, plan_sha256={state['plan_sha256']}, role={role}, "
                'plan_sha256 — digest всего пакета, не одного PLAN.json: SHA256 от '
                'конкатенации filename UTF-8 + NUL + file bytes + NUL для '
                'SPEC.md, DESIGN.md, PLAN.json именно в этом порядке. '
                f'assignee=team-{role}, run_id строкой из HERMES_KANBAN_RUN_ID. '
                'verdict=pass/changes_requested/blocked. findings[]: id,severity,blocking,claim,evidence,requested_change. '
                'Каждое замечание содержит конкретные доказательства и понятное исправление. '
                'После сохранения отчёта kanban_complete; для внешнего недоступного ресурса kanban_block.')
            state[role + '_card'] = self.spawn(state, role, f'Проверка плана: {role}', body, workspace,
                f"plan-{role}:r{state['plan_round']}", parents=[state['planner_card']])
            state[role + '_workspace'] = str(workspace)
        state['stage'] = 'plan_challenge'
        self.event(state, 'План проверяют независимо скептик и тест-дизайнер.')

    def plan_reviews(self, state):
        if self.plan_digest(state) != state['plan_sha256']:
            raise ContractError('Plan changed after independent review began')
        if self.plan_digest(state, state['plan_review_path']) != state['plan_sha256']:
            raise ContractError('Plan review snapshot changed after independent review began')
        reports = []
        for role in ('skeptic', 'test-designer'):
            report = read_json(state.get(role + '_report_path', Path(state['plan_review_path']) / (role + '.json')))
            self.verify_actor(state[role + '_card'], report, role)
            require_clean(state[role + '_workspace'])
            if git(state[role + '_workspace'], 'rev-parse', 'HEAD') != state['initial_base_sha']:
                raise ContractError('Plan reviewer changed the inspected revision')
            reports.append(report)
        validate_plan_reviews(*reports, feature_id=state['id'], plan_sha256=state['plan_sha256'])
        return reports

    def preflight_plan_review_report(self, state, role):
        """Validate one review's binding and execution identity before judge dispatch."""
        if self.plan_digest(state) != state['plan_sha256']:
            raise ContractError('Plan changed after independent review began')
        if self.plan_digest(state, state['plan_review_path']) != state['plan_sha256']:
            raise ContractError('Plan review snapshot changed after independent review began')
        workspace = state[role + '_workspace']
        require_clean(workspace)
        if git(workspace, 'rev-parse', 'HEAD') != state['initial_base_sha']:
            raise ContractError('Plan reviewer changed the inspected revision')
        # Establish the actual run/profile first. A retry is permitted only
        # when Hermes proves the expected role completed on this unchanged tree.
        actual_run_id = self._latest_valid_worker_run(state[role + '_card'], role)
        fallback = Path(state['plan_review_path']) / (role + '.json')
        report = read_json(state.get(role + '_report_path', fallback))
        validate_plan_review(report, role=role, feature_id=state['id'],
                             plan_sha256=state['plan_sha256'])
        if report['run_id'] != actual_run_id:
            raise PlanGateError(f"{role} report run_id does not match its native worker run")
        self.verify_actor(state[role + '_card'], report, role)

    def retry_invalid_plan_report(self, state, role, error):
        if self.plan_digest(state) != state['plan_sha256']:
            raise ContractError('Plan changed after independent review began; report retry is unsafe')
        if self.plan_digest(state, state['plan_review_path']) != state['plan_sha256']:
            raise ContractError('Plan review snapshot changed after independent review began; report retry is unsafe')
        workspace = state[role + '_workspace']
        require_clean(workspace)
        if git(workspace, 'rev-parse', 'HEAD') != state['initial_base_sha']:
            raise ContractError('Plan reviewer changed the inspected revision; report retry is unsafe')
        self._latest_valid_worker_run(state[role + '_card'], role)
        retries = state.setdefault('plan_report_retries', {}).get(role, 0)
        limit = self.config.get('max_plan_report_retries', 1)
        if retries >= limit:
            raise ContractError(f'{role} report retry limit reached: {error}')
        out = Path(state['plan_review_path'])
        old_path = Path(state.get(role + '_report_path', out / (role + '.json')))
        archive = out / 'report-attempts' / role / 'source' / f'attempt-{retries + 1}.json'
        archive.parent.mkdir(parents=True, exist_ok=True)
        if old_path.exists():
            shutil.copy2(old_path, archive)
        else:
            archive.write_text('<report missing>\n')
        next_path = out / 'report-attempts' / role / f'attempt-{retries + 2}.json'
        previous_card = state[role + '_card']
        attempt = retries + 1
        body = self.context(state) + (
            f'Исправь только машинный контракт отчёта {role}. Повторно проверь тот же неизменный '
            f'пакет {out}; исходный отчёт сохранён в {archive}. Ошибка валидатора: {error}. '
            'Ожидаемые точные поля: schema_version=1, '
            f"feature_id={state['id']}, plan_sha256={state['plan_sha256']}, role={role}, "
            f"assignee=team-{role}, run_id — строка из HERMES_KANBAN_RUN_ID текущей карточки, "
            'verdict=pass|changes_requested|blocked, findings — массив объектов '
            '{id,severity,blocking,claim,evidence,requested_change}. '
            'plan_sha256 — SHA256 от конкатенации filename UTF-8 + NUL + file bytes + NUL '
            'для SPEC.md, DESIGN.md, PLAN.json именно в этом порядке; ожидаемое точное значение '
            f"{state['plan_sha256']}. Используй снимок из этого каталога, а не хеш только PLAN.json. "
            'Не меняй SPEC, DESIGN, PLAN, проверяемую рабочую копию или содержательные замечания. '
            'Не перезаписывай исходный отчёт. '
            f'Новый отчёт запиши в {next_path}, затем kanban_complete.')
        state[role + '_card'] = self.spawn(
            state, role, f'Исправление контракта отчёта плана: {role}', body, workspace,
            f"plan-report-retry:{role}:r{state['plan_round']}:a{attempt}",
            parents=[previous_card])
        state[role + '_report_path'] = str(next_path)
        state['plan_report_retries'][role] = attempt
        self.event(state, f'{role}: некорректный отчёт плана сохранён; создан ограниченный повтор #{attempt}: {error[:800]}.')

    def start_judge(self, state, owner_answer=None):
        self.activate_root(state)
        self.plan_reviews(state)
        out = Path(state['plan_review_path'])
        report_paths = ', '.join(str(state.get(role + '_report_path', out / (role + '.json')))
                                 for role in ('skeptic', 'test-designer'))
        workspace = self.worktree(state, f"plan-judge-r{state['plan_round']}", state['initial_base_sha'])
        body = self.context(state) + (
            f'Разбери пакет {out} и актуальные независимые отчёты: {report_paths}. '
            'Используй ровно эти указанные пути как актуальные отчёты; более ранние попытки, '
            'если они архивированы, не подменяют их. Не голосуй по verdict. '
            'Проверь основание каждого замечания и прими решение с доказательствами. Не меняй план или код. '
            f'Выход {out / "DECISION.json"} по PLAN-DECISION.json: schema_version=1, '
            f"feature_id={state['id']}, plan_sha256={state['plan_sha256']}, assignee=team-judge, "
            'plan_sha256 — SHA256 всего пакета: filename UTF-8 + NUL + file bytes + NUL '
            'для SPEC.md, DESIGN.md, PLAN.json в этом порядке; это не checksum одного файла. '
            'run_id из HERMES_KANBAN_RUN_ID строкой. decision=accept/revise/needs_input, summary содержательный. '
            'resolutions[]: finding_id вида skeptic:F-001 или test-designer:F-001, '
            'disposition=resolved/dismissed/revise/needs_input, reason,evidence. '
            'Перед accept каждый blocking/high/critical finding нужно разобрать: resolved/dismissed '
            'только с конкретным существующим доказательством. Если нужно менять план, decision=revise. '
            'Если необходимо решение владельца — decision=needs_input, создай рядом с DECISION.json '
            'QUESTIONS.json по schema_version=1: questions (1–5 объектов с id, question, example, '
            'options (2–4 объекта id/label/description), recommended_option_id и recommendation_reason). '
            'Сформулируй вопрос по-русски простыми словами, добавь пример из сценария продукта, поясни '
            'последствия вариантов и обоснуй рекомендацию. summary оставь коротким. Не спрашивай владельца '
            'о технических деталях, которые можно выяснить самому. Затем kanban_complete.')
        if owner_answer:
            body += ("\nОтвет владельца на ранее заданный вопрос: " + str(owner_answer)
                     + "\nПереоцени этот вопрос в свете ответа владельца.\n")
        judge_key = f"judge:r{state['plan_round']}"
        if owner_answer:
            judge_key += ":owner:" + hashlib.sha256(str(owner_answer).encode()).hexdigest()[:10]
        state['judge_card'] = self.spawn(state, 'judge', 'Арбитраж плана', body, workspace,
            judge_key, parents=[state['skeptic_card'], state['test-designer_card']])
        state['judge_workspace'] = str(workspace)
        state['stage'] = 'plan_judgment'

    def decide_plan(self, state):
        reports = self.plan_reviews(state)
        decision = read_json(Path(state['plan_review_path']) / 'DECISION.json')
        self.verify_actor(state['judge_card'], decision, 'judge')
        require_clean(state['judge_workspace'])
        if git(state['judge_workspace'], 'rev-parse', 'HEAD') != state['initial_base_sha']:
            raise ContractError('Plan judge changed the inspected revision')
        outcome = validate_plan_decision(decision, *reports, feature_id=state['id'], plan_sha256=state['plan_sha256'])
        if outcome == 'accept':
            self.event(state, 'Арбитр принял план; замечания разобраны с доказательствами.')
            if state.get("workflow_managed"):
                if self.config.get("auto_start_after_judge_accept", False) is True:
                    # Auto-start is deliberately limited to an accepted judge
                    # decision. Preserve the same immutable approvals used by
                    # the explicit owner approval queue path.
                    spec_path = self.path(state) / "artifacts" / "SPEC.md"
                    approved_spec = state.get("approved_spec")
                    approved_spec_sha256 = state.get("approved_spec_sha256")
                    if (not isinstance(approved_spec, str) or not approved_spec_sha256
                            or hashlib.sha256(approved_spec.encode("utf-8")).hexdigest()
                            != approved_spec_sha256):
                        raise ContractError("Auto-start requires a valid owner-approved SPEC snapshot")
                    if (not spec_path.is_file()
                            or hashlib.sha256(spec_path.read_bytes()).hexdigest()
                            != approved_spec_sha256):
                        raise ContractError("Approved requirements changed before plan admission")
                    state["approved_plan_sha256"] = state["plan_sha256"]
                    state["approval"] = None
                    state["stage"] = "queued"
                    state["queue_reason"] = "plan_approved"
                    self.event(state, "Арбитр принял план; реализация поставлена в очередь без отдельного согласования владельца.")
                    self.save(state)
                else:
                    from .human_workflow import _make_gate
                    _make_gate(self, state, "plan",
                               "Проверьте согласованные требования, архитектуру и декомпозицию.",
                               ("SPEC.md", "DESIGN.md", "PLAN.json"))
                    self.save(state)
            else:
                self.accept_plan(state)
        elif outcome == 'needs_input':
            if state.get("workflow_managed"):
                questions_path = Path(state['plan_review_path']) / 'QUESTIONS.json'
                from .human_workflow import _validated_questions
                if questions_path.is_file() and not _validated_questions(questions_path):
                    self.block_owner_question_contract(state, questions_path, 'судья плана')
                    return
                from .human_workflow import _make_gate
                _make_gate(self, state, "plan",
                           "Требуется ваш ответ на вопрос арбитра: " + decision["summary"],
                           ("SPEC.md", "DESIGN.md", "PLAN.json"),
                           include_questions=questions_path.is_file())
                state["owner_question"] = decision["summary"]
                state["approval"]["question"] = decision["summary"]
                state["stage"] = "awaiting_owner_input"
                self.save(state)
            else:
                raise ContractError('Вопрос владельцу от арбитра: ' + decision['summary'])
        else:
            if state['plan_round'] > self.config.get('max_plan_corrections', 2):
                raise ContractError('Лимит уточнений плана исчерпан: ' + decision['summary'])
            state['plan_round'] += 1
            self.event(state, 'План возвращён на уточнение: ' + decision['summary'])
            self.new_planner(state, state['plan_review_path'])

    def block_owner_question_contract(self, state, questions_path, role_label):
        message = (f"{role_label} создал файл вопросов {questions_path.name}, но его формат не прошёл проверку. "
                   "Исправьте QUESTIONS.json: schema_version=1; 1–5 вопросов; у каждого есть id, "
                   "понятный вопрос и пример, 2–4 варианта с пояснениями, а recommended_option_id "
                   "указывает на существующий вариант. Решение владельца пока не требуется.")
        state['stage'] = 'blocked'
        state['blocker'] = message
        try:
            self.board.block_external(state['root_card'], message)
        except Exception:
            pass
        self.event(state, message, role='team-controller')
        self.save(state)

    def accept_plan(self, state):
        self.activate_root(state)
        require_artifact(self.path(state) / "artifacts/DESIGN.md", 200)
        plan = read_json(self.path(state) / "artifacts/PLAN.json")
        ordered = validate_plan(plan, project=state["project"], feature_id=state["id"],
                                base_sha=state["initial_base_sha"],
                                max_tasks=self.config.get("max_plan_tasks", 12))
        require_clean(self.path(state) / "worktrees/planning")
        by_id = {}
        items = []
        for definition in ordered:
            item = {"definition": definition, "round": 1, "status": "planned"}
            task_dir = self.path(state) / "tasks" / definition["id"]
            task_dir.mkdir(parents=True, exist_ok=True)
            workspace = self.path(state) / "worktrees" / definition["id"]
            body = self.context(state) + (
                "Карточка запланирована и удерживается контроллером до готовности зависимостей. "
                f"После активации обязательно прочитай {task_dir / 'TASK.md'}. "
                "Там точный scope, branch/base SHA, критерии и путь IMPLEMENTATION.md. "
                "Не начинай без этого документа. Сделай локальный commit, затем kanban_complete.\n")
            parents = [state["planner_card"]] + [by_id[d] for d in definition["dependencies"]]
            item["card"] = self.spawn(state, definition["assignee_role"], definition["title"], body,
                                      workspace, "dev:" + definition["id"], parents=parents,
                                      priority=PRIORITIES[definition["priority"]], parked=True)
            item["workspace"] = str(workspace)
            item["branch"] = f"codex/team-{state['id']}-{definition['id'].lower()}"
            by_id[definition["id"]] = item["card"]
            items.append(item)
        state['items'] = items
        state.setdefault("accepted_regression_sha", state["current_sha"])
        state.setdefault("staging_sha", state["current_sha"])
        state.setdefault("scheduler_version", 1)
        state.setdefault("scheduler_policy", {
            "version": 1, "max_parallel_tasks": self.max_parallel_tasks()})
        state["stage"] = "implementation"
        self.save(state)
        self.prepare_item(state)
        self.prepare_ready_items(state)
        self.event(state, f"Принят план: {len(ordered)} задач; зависимости проверены.")

    def prepare_item(self, state):
        item = state["items"][state["index"]]
        return self.prepare_specific_item(state, item)

    def prepare_specific_item(self, state, item):
        """Prepare one implementation item; the feature cursor remains a compatibility view."""
        if item.get("status") == "implementation":
            return
        item["base_sha"] = state["current_sha"]
        worktree = self.worktree(state, item["definition"]["id"], state["current_sha"], item["branch"])
        item["workspace"] = str(worktree)
        self.write_task(state, item)
        state.setdefault('task_contexts', {})[item['card']] = {
            **item['project_context'], 'workspace': str(worktree), 'planned': False,
        }
        self.board.add_comment(item['card'], 'team-controller',
                               self.context_handoff(item['project_context'], worktree))
        self.activate_with_quota(state, item["card"])
        item["status"] = "implementation"
        self.event(state, f"В разработке {item['definition']['id']}: {item['definition']['title']}",
                   role="team-" + item["definition"]["assignee_role"])
        item["worker_started_at"] = int(time.time())
        item["worker_phase"] = "implementation"

    def max_parallel_tasks(self, state=None):
        pinned = (state or {}).get("scheduler_policy", {}).get("max_parallel_tasks")
        value = pinned if pinned is not None else self.config.get("max_parallel_tasks", 1)
        return value if type(value) is int and 1 <= value <= 2 else 1

    @staticmethod
    def _dependencies_integrated(state, item):
        by_id = {row.get("definition", {}).get("id"): row for row in state.get("items", [])}
        return all(by_id.get(dep, {}).get("status") == "done"
                   for dep in item.get("definition", {}).get("dependencies", []))

    def prepare_ready_items(self, state):
        """Dispatch a bounded frontier of independent developers, respecting dependencies.

        Only implementation cards consume this scheduler budget. Hermes' quota admission
        still decides whether a configured provider slot is actually available.
        """
        items = state.get("items", [])
        if any(item.get("batch_repair") and item.get("status") in {
                "implementation", "verification", "integration_review", "provisional"}
               for item in items):
            return
        active = 0
        for item in items:
            if item.get("status") != "implementation" or not item.get("card"):
                continue
            if item["card"] in getattr(self, "test_waiting_cards", set()):
                continue
            card = self.board.get_task(item["card"])
            if card and card.get("status") not in {"done", "cancelled"}:
                active += 1
        limit = self.max_parallel_tasks(state)
        if active >= limit:
            return
        for item in items:
            if active >= limit:
                break
            if item.get("status") != "planned" or not self._dependencies_integrated(state, item):
                continue
            if not self._scopes_are_disjoint_from_unaccepted(state, item):
                continue
            self.prepare_specific_item(state, item)
            active += 1

    @staticmethod
    def _scopes_overlap(left, right):
        if not isinstance(left, list) or not left or not isinstance(right, list) or not right:
            return True
        for a in left:
            for b in right:
                if not isinstance(a, str) or not isinstance(b, str) or any(ch in a + b for ch in "*?[]"):
                    return True
                a, b = a.strip("/"), b.strip("/")
                if not a or not b or a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                    return True
        return False

    def _scopes_are_disjoint_from_unaccepted(self, state, candidate):
        active_phases = {"implementation", "verification", "integration_review", "provisional"}
        candidate_paths = candidate.get("definition", {}).get("owned_paths")
        return not any(
            other is not candidate and other.get("status") in active_phases
            and self._scopes_overlap(candidate_paths, other.get("definition", {}).get("owned_paths"))
            for other in state.get("items", []))

    def _refresh_candidate_on_current_base(self, state, item, reason):
        """Request a fresh candidate/review cycle when concurrent work made its base stale."""
        old_base = item.get("base_sha")
        new_base = state.get("current_sha")
        if not new_base or old_base == new_base:
            return False
        item["base_sha"] = new_base
        self.rework(state, item,
                    f"Concurrent integration advanced the feature base from {old_base} to {new_base}. "
                    "Rebase the existing task branch onto the new Base SHA, resolve conflicts only within "
                    "the owned paths, rerun quick checks, update IMPLEMENTATION.md, and commit the refreshed "
                    "candidate. The prior review/QA evidence is stale and will be repeated. " + reason,
                    count_toward_limit=False)
        return True

    def round_path(self, state, item):
        p = self.path(state) / "tasks" / item["definition"]["id"] / f"round-{item['round']}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write_task(self, state, item, changes=None):
        definition = item["definition"]
        out = self.round_path(state, item)
        project = self.project_settings(state)
        snapshot = self.workspace_context(state, item['workspace'])
        item['project_context'] = snapshot
        planned_plan = verification_plan(project["checks"], project.get("execution_policy"),
                                         definition.get("owned_paths"))
        item["planned_verification_plan"] = planned_plan
        content = (
            f"# {definition['id']}: {definition['title']}\n\n"
            f"## Проблема\n{definition['problem']}\n\n"
            f"## Требования\n{json.dumps(definition['requirements'], ensure_ascii=False, indent=2)}\n\n"
            f"## Решение\n{definition['solution']}\n\n"
            f"## Рекомендации для тестирования\n{json.dumps(definition['test_plan'], ensure_ascii=False, indent=2)}\n\n"
            f"## Acceptance criteria\n{json.dumps(definition['acceptance_criteria'], ensure_ascii=False, indent=2)}\n\n"
            f"Scope: {json.dumps(definition['owned_paths'], ensure_ascii=False)}\n"
            f"Приоритет: {definition['priority']}; {definition['priority_reason']}\n"
            f"Зависимости: {definition['dependencies']}\n"
            f"Рабочая копия: {item['workspace']}\nВетка: {item['branch']}\nBase SHA: {item['base_sha']}\n"
            f"Проверки для этой задачи (argv): {json.dumps(planned_plan['selected_checks'], ensure_ascii=False)}\n"
            f"Полные проверки после накопления задач (argv): {json.dumps(planned_plan['deferred_checks'], ensure_ascii=False)}\n"
            "Сначала выполняй выбранные быстрые проверки и укажи их реальные результаты в IMPLEMENTATION.md. "
            "Отложенные полные проверки запускает контроллер пакетом после нескольких независимых задач; "
            "не запускай их в каждой подзадаче без отдельного требования. Если фактические изменённые пути "
            "не покрыты планом, контроллер выберет полный набор проверок.\n"
            f"SPEC и DESIGN: {self.path(state) / 'artifacts'}\n"
            f"Отчёт вне репозитория: {out / 'IMPLEMENTATION.md'}\n"
            "Сделай атомарный локальный commit; не merge/push. Рабочая копия после commit должна "
            "быть чистой. Укажи реальный HEAD в отчёте. Не меняй файлы вне scope.\n")
        content += self.context_handoff(snapshot, item['workspace'])
        if changes:
            content += "\n## Требуемые исправления\n" + changes + "\n"
        task_path = self.path(state) / "tasks" / definition["id"] / "TASK.md"
        task_path.write_text(content)
        (out / "TASK.md").write_text(content)

    def verify_actor(self, card_id, evidence, role):
        if not isinstance(evidence, dict):
            raise ContractError(f'{role} evidence must be an object')
        if evidence.get("assignee") != "team-" + role:
            raise ContractError(f"Unexpected evidence assignee for {role}")
        runs = self.board.get_runs(card_id)
        run_id = str(evidence.get("run_id"))
        if not run_id.isdigit() or not runs:
            raise ContractError('Evidence needs a real numeric native worker run_id')
        matching = [r for r in runs if str(r.get("id", r.get("run_id"))) == run_id]
        if not matching:
            raise ContractError(f"Evidence run_id is not an actual run of card {card_id}")
        latest = max(runs, key=lambda r: int(r.get("id", r.get("run_id"))))
        if str(latest.get("id", latest.get("run_id"))) != run_id:
            raise ContractError("Evidence is from an obsolete native worker attempt")
        profile = matching[0].get("profile") or matching[0].get("assignee")
        if profile != "team-" + role:
            raise ContractError("Worker identity differs from claimed evidence identity")
        if (matching[0].get('status') not in {'done', 'completed'}
                or matching[0].get('outcome') != 'completed'):
            raise ContractError('Evidence run has not completed successfully')

    def start_review(self, state, item):
        require_artifact(self.round_path(state, item) / "IMPLEMENTATION.md", 100)
        require_clean(item["workspace"])
        candidate = validate_candidate(item["workspace"], item["base_sha"], item["branch"])
        amendments = item.get("scope_amendments", [])
        if amendments:
            # Operator-approved extensions grant exact-file ownership only. The
            # original plan scope retains its normal directory-prefix semantics.
            added = {path for row in amendments for path in row.get("added_owned_paths", [])}
            original = amendments[0].get("original_owned_paths", [])

            def owned(path):
                return path in added or path_owned(path, original)
        else:

            def owned(path):
                return path_owned(path, item["definition"]["owned_paths"])
        if not all(owned(path) for path in candidate.changed_paths):
            raise ContractError("Candidate changed files outside the accepted task ownership")
        item["head_sha"] = candidate.head_sha
        item["candidate_changed_paths"] = list(candidate.changed_paths)
        checks = self.project_settings(state)["checks"]
        item["verification_plan"] = verification_plan(
            checks, self.project_settings(state).get("execution_policy"), candidate.changed_paths)
        write_json(self.round_path(state, item) / "VERIFICATION-PLAN.json", item["verification_plan"])
        out = self.round_path(state, item)
        # A new candidate snapshot starts a new integrator attempt series. Keep
        # the prior card/report pointers in history, but never let a retry path
        # from an older round become the output path for this candidate.
        prior_integrator_card = item.get("integrator_card")
        if prior_integrator_card:
            prior_report_path = Path(item.get("integrator_report_path",
                                              self.round_path(state, item) / "INTEGRATION.json"))
            history = item.setdefault("integrator_attempts", [])
            if not any(row.get("card") == prior_integrator_card for row in history
                       if isinstance(row, dict)):
                history.append({"card": prior_integrator_card,
                                "report_path": str(prior_report_path),
                                "attempt": item.get("integrator_retries", 0),
                                "round": item.get("integrator_round", item["round"]),
                                "reason": "candidate review cycle ended"})
        for key in ("integrator_card", "integrator_report_path", "integrator_workspace",
                    "integrator_round", "integrator_retries", "integration_review_started_at"):
            item.pop(key, None)
        # A developer rework creates a new candidate snapshot and therefore a
        # fresh, independent reviewer/QA cycle.
        item['report_retries'] = {}
        for role in ('reviewer', 'qa'):
            item.pop(role + '_report_path', None)
        for role, filename in (("reviewer", "REVIEW.json"), ("qa", "QA.json")):
            suffix = f"{item['definition']['id']}-{role}-r{item['round']}"
            workspace = self.worktree(state, suffix, item["head_sha"])
            item[role + "_workspace"] = str(workspace)
            report_path = out / filename
            item[role + "_report_path"] = str(report_path)
            body = self.context(state) + (
                f"Независимая проверка задачи {item['definition']['id']}. "
                f"Прочитай {out / 'TASK.md'}, {out / 'IMPLEMENTATION.md'}, SPEC и DESIGN. "
                "Не исправляй код и не меняй рабочую копию. Артефакт запиши вне репозитория.\n"
                f"JSON по шаблону {filename}; обязательные точные поля:\n"
                f"task_id={item['card']}, role={role}, assignee=team-{role}, "
                f"base_sha={item['base_sha']}, head_sha={item['head_sha']}. "
                "run_id бери строкой из HERMES_KANBAN_RUN_ID. verdict=pass/changes_requested/blocked; "
                "findings=[{severity:info|low|medium|high|critical,blocking:boolean,description:string}]; "
                + ("checks=[{name:string,status:pass|fail,evidence:string}]. "
                   if role == "qa" else
                   "checks=[{name:string,status:pass|fail|skip,evidence:string}]. ")
                + ("Дополнительные отрицательные проверки (например, убитый мутант) записывай отдельно в "
                   "supplemental_checks=[{kind:mutation_killed,name,command,mutant_id,killed_by,exit_code:1,"
                   "head_sha,scratch_worktree,clean_after_revert:true,evidence}]. Они не заменяют обязательные "
                   "checks: каждый обязательный check должен иметь status=pass и exit_code=0. "
                   "Каждую выбранную быструю команду из VERIFICATION-PLAN.json представь ровно один раз, "
                   "с точными name и command=argv из плана, фактическим exit_code и evidence. "
                   "Никогда не записывай status=skip в checks. Если план mode=quick и есть deferred_checks, "
                   "не запускай их и добавь отдельное поле deferred={evidence_ref:VERIFICATION-PLAN.json, "
                   "policy_sha256:<точно из плана>, checks:[точные объекты deferred_checks из плана]}. "
                   "Это только фиксация отложенных проверок, не pass evidence. Не добавляй deferred "
                   "в full mode, не меняй/объединяй список и не записывай агрегированную строку про них в checks. "
                   if role == "qa" else "") +
                "Непройденные проверки не маскировать. Привяжи проверки к AC и затронутым регрессиям. "
                f"План проверок: {out / 'VERIFICATION-PLAN.json'}. "
                f"Быстрые команды для этой задачи: {json.dumps(selected_argv(item['verification_plan']))}. "
                f"Отложенные полные команды пакета: {json.dumps(item['verification_plan']['deferred_checks'], ensure_ascii=False)}. "
                + ("Для QA фактически выполни каждую быструю команду с её фактическим exit code и независимо "
                   "проверь отрицательные/граничные случаи. Не запускай отложенную полную регрессию: её "
                   "выполнит контроллер один раз на накопленном candidate SHA. " if role == "qa" else
                   "Для review оцени покрытие быстрых команд и полноту отложенной пакетной регрессии; "
                   "не запускай повторно длинную полную регрессию. ")
                + "Если найдены дефекты, всё равно сохрани честный отчёт и заверши этап kanban_complete — "
                "контроллер вернёт реализацию на доработку.\n"
                f"Выход: {report_path}\n")
            item[role + "_card"] = self.spawn(
                state, role, f"{role}: {item['definition']['title']}", body, workspace,
                f"{role}:{item['definition']['id']}:r{item['round']}", parents=[item["card"]])
        state["stage"] = "verification"
        item["status"] = "verification"
        item["verification_started_at"] = int(time.time())
        item["worker_phase"] = "verification"
        self.event(state, f"{item['definition']['id']}: независимые review и QA на {item['head_sha'][:12]}.")

    def read_evidence(self, state, item):
        out = self.round_path(state, item)
        review = read_json(item.get("reviewer_report_path", out / "REVIEW.json"))
        qa = read_json(item.get("qa_report_path", out / "QA.json"))
        for role, evidence in (("reviewer", review), ("qa", qa)):
            self.verify_actor(item[role + "_card"], evidence, role)
            require_clean(item[role + "_workspace"])
            if git(item[role + "_workspace"], "rev-parse", "HEAD") != item["head_sha"]:
                raise ContractError(f"{role} changed the inspected revision")
        return review, qa

    @staticmethod
    def _validate_review_report_shape(report, role, item):
        """Check report contract separately from its review verdict.

        A structurally sound report that finds a defect is a real review result,
        not something to retry as an authoring error.
        """
        if not isinstance(report, dict):
            raise ContractError(f"{role} report must be a JSON object")
        required = {"schema_version", "task_id", "role", "assignee", "run_id", "verdict",
                    "base_sha", "head_sha", "findings", "checks"}
        missing = required - set(report)
        if missing:
            raise ContractError(f"{role} report missing fields: {', '.join(sorted(missing))}")
        if type(report["schema_version"]) is not int or report["schema_version"] != 1:
            raise ContractError(f"{role} report schema_version must be integer 1")
        expected = {"task_id": item["card"], "role": role, "assignee": "team-" + role,
                    "base_sha": item["base_sha"], "head_sha": item["head_sha"]}
        for key, value in expected.items():
            if report[key] != value:
                raise ContractError(f"{role} report {key} does not match the inspected task")
        if not isinstance(report["run_id"], str) or not report["run_id"].isdigit():
            raise ContractError(f"{role} report run_id must be a numeric string")
        if not isinstance(report["verdict"], str) or report["verdict"] not in {"pass", "changes_requested", "blocked"}:
            raise ContractError(f"{role} report has an invalid verdict")
        if not isinstance(report["findings"], list) or not isinstance(report["checks"], list):
            raise ContractError(f"{role} report findings/checks must be arrays")
        for index, finding in enumerate(report["findings"]):
            if (not isinstance(finding, dict)
                    or not isinstance(finding.get("severity"), str)
                    or finding.get("severity") not in {"info", "low", "medium", "high", "critical"}
                    or not isinstance(finding.get("blocking"), bool)
                    or not isinstance(finding.get("description"), str)
                    or not finding["description"].strip()):
                raise ContractError(f"{role} finding {index} has an invalid shape")
        for index, check in enumerate(report["checks"]):
            if (not isinstance(check, dict) or not isinstance(check.get("name"), str)
                    or not check["name"].strip()
                    or not isinstance(check.get("status"), str)
                    or check.get("status") not in {"pass", "fail", "skip"}
                    or ("evidence" in check and not isinstance(check.get("evidence"), str))):
                raise ContractError(f"{role} check {index} has an invalid shape")
            if "command" in check and (not isinstance(check["command"], list)
                                         or any(not isinstance(arg, str) for arg in check["command"])):
                raise ContractError(f"{role} check {index} command must be a string array")
            if "exit_code" in check:
                if type(check["exit_code"]) is not int:
                    raise ContractError(f"{role} check {index} exit_code must be an integer")
                if check["status"] == "pass" and check["exit_code"] != 0:
                    raise ContractError(
                        f"{role} check {check['name']!r} says pass but has non-zero exit_code; "
                        "move an expected killed-mutant result to supplemental_checks"
                    )
        try:
            validate_supplemental_checks(report, role=role, head_sha=item["head_sha"])
        except EvidenceError as exc:
            raise ContractError(str(exc)) from exc
        if (role == "qa" and report.get("verdict") == "pass"
                and item.get("verification_plan") is not None):
            try:
                validate_qa_verification_plan(report, item["verification_plan"])
            except EvidenceError as exc:
                raise ContractError(str(exc)) from exc

    def _latest_valid_worker_run(self, card_id, role):
        runs = self.board.get_runs(card_id)
        if not runs:
            raise ContractError(f"No native worker run exists for {role}")
        try:
            latest = max(runs, key=lambda r: int(r.get("id", r.get("run_id"))))
            run_id = str(latest.get("id", latest.get("run_id")))
        except (TypeError, ValueError, AttributeError):
            raise ContractError(f"Cannot verify native worker run for {role}")
        profile = latest.get("profile") or latest.get("assignee")
        if (not run_id.isdigit() or profile != "team-" + role
                or latest.get("status") not in {"done", "completed"}
                or latest.get("outcome") != "completed"):
            raise ContractError(f"Latest native worker run is not a successful {role} run")
        return run_id

    def retry_invalid_review_report(self, state, item, role, error):
        """Retry one malformed report without changing the reviewed code snapshot."""
        workspace = item[role + "_workspace"]
        require_clean(workspace)
        if git(workspace, "rev-parse", "HEAD") != item["head_sha"]:
            raise ContractError(f"{role} changed the inspected revision; report retry is unsafe")
        # Verify the execution identity from Hermes itself before replacing any
        # report. A retry must never conceal a wrong profile or failed run.
        self._latest_valid_worker_run(item[role + "_card"], role)
        retries = item.setdefault("report_retries", {}).get(role, 0)
        limit = self.config.get("max_report_retries", 1)
        if retries >= limit:
            raise ContractError(f"{role} report retry limit reached: {error}")
        out = self.round_path(state, item)
        old_path = Path(item.get(role + "_report_path", out / ("REVIEW.json" if role == "reviewer" else "QA.json")))
        archive = out / "report-attempts" / role / "source" / f"attempt-{retries + 1}.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        source_report = None
        if old_path.exists():
            shutil.copy2(old_path, archive)
            try:
                source_report = read_json(archive)
            except (json.JSONDecodeError, OSError):
                source_report = None
        else:
            archive.write_text("<report missing>\n")
        next_path = out / "report-attempts" / role / f"attempt-{retries + 2}.json"
        previous_card = item[role + "_card"]
        number = retries + 1
        qa_supplemental_guidance = ""
        if role == "qa":
            qa_supplemental_guidance = (
                "Если исходный отчёт содержит реально проведённые ожидаемые проверки мутантов, перенеси их "
                "отдельно в supplemental_checks по схеме: "
                "[{kind:mutation_killed,name,command,mutant_id,killed_by,exit_code:1,head_sha,scratch_worktree,"
                "clean_after_revert:true,evidence}]. Иначе оставь supplemental_checks пустым или опусти поле. "
                "Для каждой подтверждённой мутации создай отдельную запись; head_sha "
                f"должен точно равняться {item['head_sha']}. Обязательные checks оставь в checks и сохраняй "
                "их фактические status/exit_code: успешные — pass/0, непрошедшие — fail или skip с причиной. "
                "Не исправляй фактический исход команды. Сохрани доказательства исходного отчёта и их provenance: run_id QA.json "
                "должен быть текущим HERMES_KANBAN_RUN_ID этого исправления отчёта, а evidence каждой "
                "дополнительной проверки явно называет исходный run/worktree, где запускался мутант. "
                "Укажи clean_after_revert=true только если чистота scratch worktree подтверждена исходными "
                "доказательствами. Сохрани реальные исходы. Для pass-отчёта разрешено убрать из checks "
                "только агрегированную запись, которая по точным индексам и командам соответствует "
                "отложенным checks действующего quick-плана; вместо неё поставь exact deferred binding "
                "по инструкции, не объявляя команды выполненными. Не переклассифицируй выбранную, неизвестную "
                "или не запущенную обязательную проверку как deferred: сохрани skip и поставь verdict=blocked, "
                "если среда не дала запустить её. Реальные fail оставь в checks и не превращай их в "
                "supplemental evidence. Полный suite повторно не запускай и никаких "
                "результатов не додумывай. "
            )
            if isinstance(source_report, dict):
                prior_negative = [check for check in source_report.get("checks", [])
                                  if isinstance(check, dict)
                                  and check.get("status") == "pass"
                                  and type(check.get("exit_code")) is int
                                  and check["exit_code"] != 0]
                if prior_negative:
                    source_negative_json = json.dumps(prior_negative, ensure_ascii=False)[:8000]
                    qa_supplemental_guidance += (
                        "Переноси только следующие существующие записи из исходного отчёта; "
                        "сохрани каждую мутацию отдельно и evidence/provenance дословно, "
                        "не превращая их в обязательные checks: "
                        + source_negative_json + " "
                    )
                    mutant_ids = sorted(set(re.findall(r"\bM\d+\b", source_negative_json)))
                    if mutant_ids:
                        qa_supplemental_guidance += (
                            "Отдельная supplemental_checks-запись для каждого обнаруженного mutant_id: "
                            + ", ".join(mutant_ids) + ". Не объединяй разные mutants в одну запись. "
                        )
        body = self.context(state) + (
            f"Исправь только контракт отчёта {role} для того же неизменного snapshot. "
            f"Ознакомься с исходным отчётом {archive} и ошибкой валидатора: {error}. "
            "Укажи точные поля: schema_version=1, "
            f"task_id={item['card']}, role={role}, assignee=team-{role}, "
            f"base_sha={item['base_sha']}, head_sha={item['head_sha']}, "
            "run_id — строка из HERMES_KANBAN_RUN_ID текущей карточки; "
            "verdict=pass|changes_requested|blocked; findings — массив "
            "{severity:info|low|medium|high|critical,blocking:boolean,description:string}; "
            "checks — непустой массив {name:string,status:pass|fail|skip,evidence:string}. "
            + qa_supplemental_guidance +
            "Не меняй код, inspected SHA, рабочую копию или содержательные выводы; "
            "не выдумывай проверки. "
            f"Новый отчёт запиши в {next_path}, не изменяй исходный файл. После сохранения "
            "заверши kanban_complete.")
        item[role + "_card"] = self.spawn(
            state, role, f"Исправление отчёта {role}: {item['definition']['title']}",
            body, workspace,
            f"report-retry:{item['definition']['id']}:{role}:r{item['round']}:a{number}",
            parents=[previous_card])
        item[role + "_report_path"] = str(next_path)
        item["report_retries"][role] = number
        self.event(state, f"{role}: некорректный отчёт сохранён; запрошен ограниченный повтор #{number}: {error[:800]}.")

    def validate_pair(self, item, review, qa):
        validate_promotion_evidence(review, qa, task_id=item["card"], base_sha=item["base_sha"],
                                    head_sha=item["head_sha"],
                                    developer_id="team-" + item["definition"]["assignee_role"],
                                    qa_verification_plan=item.get("verification_plan"))

    def rework(self, state, item, reason, *, count_toward_limit=True):
        limit = self.config.get("max_rework_rounds", 2)
        authorized_amendment = False
        if count_toward_limit:
            item.setdefault("code_rework_round", max(0, item.get("round", 1) - 1))
            amendment_hash = item.pop("scope_amendment_permit", None)
            amendment = (item.get("scope_amendments") or [])
            authorized_amendment = (isinstance(amendment_hash, str) and len(amendment_hash) == 64
                                    and any(row.get("sha256") == amendment_hash
                                            for row in amendment if isinstance(row, dict)))
            if item["code_rework_round"] >= limit and not authorized_amendment:
                raise ContractError("Rework limit reached: " + reason)
        else:
            item["candidate_refresh_round"] = item.get("candidate_refresh_round", 0) + 1
            if item["candidate_refresh_round"] > self.config.get("max_candidate_refresh_rounds", 5):
                raise ContractError("Candidate refresh limit reached: " + reason)
        if item.get("round", 1) > limit + self.config.get("max_candidate_refresh_rounds", 5):
            raise ContractError("Rework limit reached: " + reason)
        old_card = item["card"]
        item.setdefault("past_cards", []).append(old_card)
        item["round"] += 1
        if count_toward_limit:
            item["code_rework_round"] += 1
        self.write_task(state, item, reason)
        body = self.context(state) + (
            f"Исправь замечания из {self.path(state) / 'tasks' / item['definition']['id'] / 'TASK.md'}. "
            "Работай в прежней выделенной ветке. Если в замечании указан новый Base SHA, сначала "
            "перебазируй ветку на него, сохранив прежние изменения, затем разреши конфликты только "
            "в Scope. Сделай локальный commit и быстрые проверки. "
            "Не переписывай завершённые независимые отчёты. Новый IMPLEMENTATION.md — "
            f"{self.round_path(state, item) / 'IMPLEMENTATION.md'}. Затем kanban_complete.")
        item["card"] = self.spawn(
            state, item["definition"]["assignee_role"],
            f"Доработка {item['definition']['id']} / {item['round'] - 1}",
            body, item["workspace"], f"rework:{item['definition']['id']}:r{item['round']}",
            parents=[old_card])
        item["status"] = "implementation"
        item["worker_started_at"] = int(time.time())
        state["stage"] = "implementation"
        self.event(state, f"Возврат {item['definition']['id']} на доработку: {reason[:1200]}")

    @staticmethod
    def _scope_amendment_digest(evidence):
        payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def apply_task_scope_amendment(self, state, evidence):
        """Apply a narrowly authorized, append-only scope extension to a blocked task.

        This is a trusted operator action. The evidence object is persisted verbatim
        under the feature's artifacts before the normal rework lifecycle is entered.
        It cannot change approved project requirements or bypass code rework accounting.
        """
        if not isinstance(evidence, dict) or evidence.get("schema_version") != 1:
            raise ContractError("Scope amendment evidence must use schema_version=1")
        if evidence.get("operator_decision") != "approve_scope_extension":
            raise ContractError("Scope amendment requires an explicit operator approval")
        if (not isinstance(evidence.get("operator_actor"), str)
                or not evidence["operator_actor"].strip()
                or not isinstance(evidence.get("user_authorization"), str)
                or len(evidence["user_authorization"].strip()) < 8
                or type(evidence.get("created_at")) is not int
                or evidence["created_at"] <= 0):
            raise ContractError("Scope amendment needs actor, explicit user authorization and Unix created_at")
        evidence_sha = self._scope_amendment_digest(evidence)
        task_id = safe_id(evidence.get("task_id"))
        matches = [row for row in state.get("items", [])
                   if row.get("definition", {}).get("id") == task_id]
        if len(matches) == 1:
            prior = next((row for row in matches[0].get("scope_amendments", [])
                          if row.get("sha256") == evidence_sha), None)
            if prior and prior.get("status") == "applied" and matches[0].get("round") == prior.get("new_round"):
                audit_path = Path(prior.get("audit_path", ""))
                if (not audit_path.is_file() or audit_path.is_symlink()
                        or hashlib.sha256(audit_path.read_bytes()).hexdigest()
                        != prior.get("artifact_sha256")
                        or json.loads(audit_path.read_text(encoding="utf-8")) != evidence):
                    raise ContractError("Applied scope amendment audit artifact is missing or changed")
                self._finish_resume_request(state)
                return matches[0].get("card")
        if state.get("stage") != "blocked" or state.get("resume_stage") != "verification":
            raise ContractError("Scope amendment is only available for a task blocked at verification")
        if evidence.get("feature_id") != state.get("id"):
            raise ContractError("Scope amendment feature_id does not match the active feature")
        if len(matches) != 1:
            raise ContractError("Scope amendment task_id must identify exactly one task")
        item = matches[0]
        if item.get("status") != "verification" or not item.get("verification_blocked_roles"):
            raise ContractError("Task is not blocked by a recorded verification role")

        expected_bindings = {
            "blocked_round": item.get("round"),
            "developer_card_id": item.get("card"),
            "code_rework_round": item.get("code_rework_round", max(0, item.get("round", 1) - 1)),
            "base_sha": item.get("base_sha"),
            "candidate_sha": item.get("head_sha"),
            "approved_plan_sha256": state.get("approved_plan_sha256"),
            "approved_spec_sha256": state.get("approved_spec_sha256"),
            "original_owned_paths": item.get("definition", {}).get("owned_paths"),
        }
        for key, expected in expected_bindings.items():
            if evidence.get(key) != expected:
                raise ContractError(f"Scope amendment {key} is stale or does not match the task")
        if not expected_bindings["approved_plan_sha256"] or not expected_bindings["approved_spec_sha256"]:
            raise ContractError("Feature has no owner-approved SPEC and plan digests")

        spec_path = self.path(state) / "artifacts" / "SPEC.md"
        if (not spec_path.is_file()
                or hashlib.sha256(spec_path.read_bytes()).hexdigest() != state["approved_spec_sha256"]
                or self.plan_digest(state) != state["approved_plan_sha256"]):
            raise ContractError("Approved SPEC or plan changed; scope amendment is unsafe")

        resolution_path = evidence.get("resolution_path")
        resolution_sha = evidence.get("resolution_sha256")
        if resolution_path is not None or resolution_sha is not None:
            artifacts_root = (self.path(state) / "artifacts").resolve()
            resolution_file = Path(resolution_path) if isinstance(resolution_path, str) else Path()
            if (not resolution_file.is_absolute() or resolution_file.is_symlink()
                    or not resolution_file.is_file()
                    or not resolution_file.resolve().is_relative_to(artifacts_root)
                    or not isinstance(resolution_sha, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", resolution_sha)
                    or hashlib.sha256(resolution_file.read_bytes()).hexdigest() != resolution_sha):
                raise ContractError("Requirements resolution path/hash is invalid or outside feature artifacts")

        workspace = Path(item.get("workspace", ""))
        require_clean(workspace)
        if git(workspace, "rev-parse", "HEAD") != item.get("head_sha"):
            raise ContractError("Candidate workspace no longer matches the blocked candidate SHA")
        candidate_paths = item.get("candidate_changed_paths")
        if (not isinstance(candidate_paths, list) or not candidate_paths
                or not all(path_owned(path, item["definition"]["owned_paths"])
                           for path in candidate_paths)):
            raise ContractError("Blocked candidate scope is missing or inconsistent")

        # A blocked reviewer/QA is terminal for scheduling, but every other worker
        # on the feature must also be terminal before a new developer attempt is made.
        relevant_cards = set()
        for row in state.get("items", []):
            for key in ("card", "reviewer_card", "qa_card", "integrator_card"):
                if row.get(key):
                    relevant_cards.add(row[key])
        active_statuses = {"running", "ready", "todo", "triage", "claimed", "in_progress"}
        active = [card_id for card_id in relevant_cards
                  if (self.board.get_task(card_id) or {}).get("status") in active_statuses]
        active_features = [summary["id"] for summary in self.summaries()
                           if summary["id"] != state["id"] and summary["stage"] not in TERMINAL]
        if active_features:
            raise ContractError("Another feature is active: " + ", ".join(active_features))
        if active:
            raise ContractError("Scope amendment requires all feature worker cards to be terminal: "
                                + ", ".join(sorted(active)))
        parent = self.board.get_task(item["card"])
        if not parent or parent.get("status") not in {"done", "completed"}:
            raise ContractError("Previous developer card must be completed before rework")

        added_paths = evidence.get("added_owned_paths")
        if not isinstance(added_paths, list) or not added_paths:
            raise ContractError("Scope amendment must add at least one concrete owned path")
        existing_paths = item["definition"]["owned_paths"]
        if (len(set(added_paths)) != len(added_paths)
                or any(not isinstance(path, str) or any(ch in path for ch in "*?[]")
                       for path in added_paths)):
            raise ContractError("Added scope paths must be unique concrete relative paths")
        for path in added_paths:
            validate_owned_path(path)
            if path.endswith("/") or path in {".", "./"}:
                raise ContractError("Added scope must name concrete files, not directories")
            basename = Path(path).name
            extensionless_files = {"Dockerfile", "Makefile", "Justfile", "LICENSE", "NOTICE",
                                   "Procfile", "Gemfile", "Rakefile", "gradlew", "mvnw",
                                   ".gitignore", ".dockerignore", ".editorconfig"}
            if not Path(path).suffix and basename not in extensionless_files:
                raise ContractError("Added scope must name exact files, not directories or prefixes")
            candidate_path = Path(state["repo"]) / path
            if candidate_path.exists() and candidate_path.is_dir():
                raise ContractError("Added scope must name concrete files, not directories")
            if any(path == existing or path.startswith(existing.rstrip("/") + "/")
                   or existing.startswith(path.rstrip("/") + "/") for existing in existing_paths):
                raise ContractError("Scope amendment may only add paths outside the existing scope")
            for other in state.get("items", []):
                if other is item or other.get("status") in {"done", "cancelled"}:
                    continue
                if self._scopes_overlap([path], other.get("definition", {}).get("owned_paths")):
                    raise ContractError("Added scope overlaps another unaccepted task")

        note = evidence.get("developer_instructions")
        rationale = evidence.get("rationale")
        if not isinstance(note, str) or len(note.strip()) < 20:
            raise ContractError("Scope amendment needs concrete developer_instructions")
        if not isinstance(rationale, str) or len(rationale.strip()) < 10:
            raise ContractError("Scope amendment needs an operator rationale")
        added_criteria = evidence.get("acceptance_criteria")
        added_tests = evidence.get("test_plan")
        if not isinstance(added_criteria, list) or not added_criteria:
            raise ContractError("Scope amendment needs additional acceptance criteria")
        for ac in added_criteria:
            if not isinstance(ac, dict) or not all(isinstance(ac.get(k), str) and ac[k].strip()
                                                   for k in ("id", "given", "when", "then", "evidence")):
                raise ContractError("Added acceptance criteria need id/given/when/then/evidence")
        if not isinstance(added_tests, list) or not added_tests or not all(
                isinstance(value, dict) and all(isinstance(value.get(key), str) and value[key].strip()
                    for key in ("name", "type", "scope", "reason")) for value in added_tests):
            raise ContractError("Scope amendment test_plan entries need name/type/scope/reason")

        if item.get("scope_amendments"):
            raise ContractError("Only one operator scope amendment is allowed per task")
        if item.get("scope_amendment_permit"):
            raise ContractError("Another scope amendment permit is already pending")

        audit_dir = self.path(state) / "artifacts" / "task-scope-amendments"
        audit_dir.mkdir(parents=True, exist_ok=True)
        if audit_dir.is_symlink():
            raise ContractError("Scope amendment audit directory must not be a symlink")
        audit_path = audit_dir / f"{task_id}-r{item['round']}-{evidence_sha}.json"
        serialized = json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if audit_path.is_symlink():
            raise ContractError("Scope amendment audit artifact must not be a symlink")
        try:
            with audit_path.open("x", encoding="utf-8") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if audit_path.read_text(encoding="utf-8") != serialized:
                raise ContractError("Existing scope amendment audit artifact does not match evidence")

        original_paths = copy.deepcopy(existing_paths)
        verification_history = {
            "round": item["round"], "candidate_sha": item["head_sha"],
            "blocked_roles": list(item.get("verification_blocked_roles", [])),
            "cards": {}, "archived_at": int(time.time()),
        }
        for role in ("reviewer", "qa", "integrator"):
            record = {}
            for suffix, key in (("card", role + "_card"),
                                ("workspace", role + "_workspace"),
                                ("report_path", role + "_report_path")):
                if item.get(key):
                    record[suffix] = item[key]
            report_path = item.get(role + "_report_path")
            if report_path and Path(report_path).is_file():
                record["report_sha256"] = hashlib.sha256(Path(report_path).read_bytes()).hexdigest()
            if record:
                verification_history["cards"][role] = record
        item.setdefault("verification_attempt_history", []).append(verification_history)
        item["definition"]["owned_paths"] = original_paths + list(added_paths)
        item["definition"]["acceptance_criteria"] = (
            list(item["definition"].get("acceptance_criteria", [])) + copy.deepcopy(added_criteria))
        item["definition"]["test_plan"] = (
            list(item["definition"].get("test_plan", [])) + copy.deepcopy(added_tests))
        item.setdefault("scope_amendments", []).append({
            "sha256": evidence_sha, "artifact_sha256": hashlib.sha256(
                audit_path.read_bytes()).hexdigest(), "audit_path": str(audit_path),
            "original_owned_paths": original_paths, "added_owned_paths": list(added_paths),
            "old_round": item["round"], "new_round": item["round"] + 1,
            "status": "applied", "applied_at": int(time.time()),
        })
        item["scope_amendment_permit"] = evidence_sha
        for key in ("reviewer_card", "qa_card", "integrator_card",
                    "reviewer_workspace", "qa_workspace", "integrator_workspace",
                    "reviewer_report_path", "qa_report_path", "integrator_report_path",
                    "verification_retries",
                    "verification_blocked_roles"):
            item.pop(key, None)
        state.pop("resume_stage", None)
        state.pop("blocker", None)
        reason = ("Оператор одобрил ограниченное расширение scope. Дополнительные инструкции: "
                  + note.strip() + f" Audit SHA256: {evidence_sha}.")
        self.rework(state, item, reason, count_toward_limit=True)
        item["scope_amendments"][-1]["developer_card_id"] = item["card"]
        state["resume_requested"] = True
        self.save(state)
        self._finish_resume_request(state)
        return item["card"]

    def start_integrator(self, state, item, *, retry_attempt=None, retry_context=None):
        review, qa = self.read_evidence(state, item)
        blocked_roles = [role for role, evidence in (("reviewer", review), ("qa", qa))
                         if evidence.get("verdict") == "blocked"]
        if blocked_roles:
            self.block_verification(state, item, blocked_roles,
                                    {"reviewer": review, "qa": qa})
            return
        try:
            self.validate_pair(item, review, qa)
        except GitGateError as exc:
            self.rework(state, item, str(exc) + "\n" +
                        json.dumps({"review": review, "qa": qa}, ensure_ascii=False))
            return
        out = self.round_path(state, item)
        workspace_name = f"{item['definition']['id']}-integrator-r{item['round']}"
        if retry_attempt is not None:
            workspace_name += f"-retry-{retry_attempt}"
        workspace = self.worktree(state, workspace_name, item["head_sha"])
        item["integrator_workspace"] = str(workspace)
        item["integrator_round"] = item["round"]
        report_path = Path(item.get("integrator_report_path", out / "INTEGRATION.json"))
        report_path.parent.mkdir(parents=True, exist_ok=True)
        item["integrator_report_path"] = str(report_path)
        report_paths = ', '.join(str(item.get(role + '_report_path', out / filename))
                                 for role, filename in (("reviewer", "REVIEW.json"), ("qa", "QA.json")))
        body = self.context(state) + (
            f"Проверь пакет {out}: TASK.md, IMPLEMENTATION.md и актуальные отчёты {report_paths}. "
            "Используй ровно эти пути как действующие отчёты; старые файлы и архивные попытки не подменяют их. "
            "Сверь полноту AC, независимость, миграции, совместимость, rollback и оставшиеся риски. "
            "В этом запуске фактический git merge и обязательные команды выполняет контроллер "
            "после твоего отчёта; сам не merge и не меняй код. "
            f"Подготовь {report_path} по шаблону: feature_id={state['id']}, "
            "assignee=team-integrator, run_id строкой из HERMES_KANBAN_RUN_ID, "
            f"base_sha={item['base_sha']}, head_sha={item['head_sha']}, "
            "decision=ready_for_owner если все предварительные gates готовы, иначе blocked; "
            "gates[].status должен быть pass, fail, blocked или deferred; blocking_items=[] только при отсутствии блокеров. "
            "Заполняй findings и blocking_items только реальными негативными или блокирующими результатами: "
            "при decision=ready_for_owner и полностью pass gates оба массива должны быть пустыми. "
            "Сохрани полезные неблокирующие замечания severity info/low в существующем поле accepted_risks "
            "для владельца; не теряй их и не переклассифицируй реальный блокер как advisory. "
            "Читай действующий VERIFICATION-PLAN.json. Deferred разрешён только если mode=quick, "
            "и только для реально перечисленных в deferred_checks команд. Для такого gate укажи "
            "status=deferred, evidence_ref=VERIFICATION-PLAN.json, policy_sha256 равный точному "
            "policy_sha256 плана и deferred_checks как непустой массив точных объектов из плана "
            "(index, name, argv, sha256). Не помечай deferred как pass и не утверждай, что такие "
            "проверки выполнены. Required/selected команды, неизвестные проверки и full-mode нельзя "
            "откладывать. Decision означает готовность к автоматической ЛОКАЛЬНОЙ интеграции; protected branch "
            "остаётся владельцу. Затем заверши карточку kanban_complete.\n"
            + (retry_context or ""))
        item["integrator_card"] = self.spawn(
            state, "integrator", f"Интеграционный разбор: {item['definition']['title']}",
            body, workspace,
            f"integrator:{item['definition']['id']}:r{item['round']}"
            + (f":retry-{retry_attempt}" if retry_attempt is not None else ""),
            parents=[item["reviewer_card"], item["qa_card"]])
        state["stage"] = "integration_review"
        item["status"] = "integration_review"
        item["worker_phase"] = "integration_review"
        item["integration_review_started_at"] = int(time.time())

    def retry_integrator(self, state, item, reason):
        """Explicitly retry only the integrator report, preserving each prior attempt."""
        attempts = item.get("integrator_retries", 0)
        if type(attempts) is not int or attempts >= 2:
            raise ContractError("Integrator report retry limit reached (2)")
        if not isinstance(reason, str) or not reason.strip():
            raise ContractError("Integrator retry needs a non-empty operator reason")
        if item.get("status") != "integration_review":
            raise ContractError("Integrator retry is only available for an integration_review item")
        old_card = item.get("integrator_card")
        old_report = Path(item.get("integrator_report_path", self.round_path(state, item) / "INTEGRATION.json"))
        if not old_card:
            raise ContractError("Integrator retry has no prior integrator card")
        archive = self.round_path(state, item) / "report-attempts" / "integrator" / "source" / f"attempt-{attempts + 1}.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        report_sha256 = None
        if old_report.is_file():
            shutil.copy2(old_report, archive)
            report_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
        old_run_id = "unknown"
        try:
            old_runs = self.board.get_runs(old_card)
            if old_runs:
                latest = max(old_runs, key=lambda run: int(run.get("id", run.get("run_id"))))
                old_run_id = str(latest.get("id", latest.get("run_id")))
        except (HermesBoardError, TypeError, ValueError, AttributeError):
            pass
        history = item.setdefault("integrator_attempts", [])
        history.append({"card": old_card, "report_path": str(old_report),
                        "archive_path": str(archive) if report_sha256 else None,
                        "report_sha256": report_sha256, "run_id": old_run_id,
                        "reason": reason.strip(),
                        "attempt": attempts + 1,
                        "round": item.get("integrator_round", item["round"])})
        retry = attempts + 1
        item["integrator_retries"] = retry
        item["integrator_report_path"] = str(
            self.round_path(state, item) / "report-attempts" / "integrator" / f"attempt-{retry}.json")
        item.pop("integrator_workspace", None)
        retry_context = (
            f"Повтор интеграционного разбора: предыдущий отчёт находится в {archive if report_sha256 else old_report}"
            + (f" (SHA256={report_sha256})" if report_sha256 else " (архивный файл недоступен)")
            + f"; старая карточка={old_card}, run_id={old_run_id}; head_sha кандидата остаётся {item['head_sha']}. "
            f"Причина повтора: {reason.strip()}. Сначала прочитай этот отчёт и актуальные REVIEW/QA "
            "из путей выше, затем составь новый отчёт для того же SHA. Используй уже завершённые QA/review "
            "и имеющиеся результаты проверок; не перезапускай QA и долгие regression suites, которые "
            "запускает контроллер отдельно. Честно сохрани все реальные блокирующие замечания и failed gates; "
            "не скрывай их ради ready_for_owner. Неблокирующие info/low notes перенеси в accepted_risks.\n"
        )
        self.start_integrator(state, item, retry_attempt=retry, retry_context=retry_context)
        self.event(state, f"Повторный разбор интегратора для {item['definition']['id']} "
                          f"(попытка {retry}/2): {reason.strip()[:500]}", role="team-controller")

    def block_verification(self, state, item, roles, evidence_by_role):
        """Stop on environment/capability blockers without spending code rework."""
        item["verification_blocked_roles"] = sorted(set(roles))
        state["resume_stage"] = "verification"
        state["stage"] = "blocked"
        explanations = []
        for role in ("reviewer", "qa"):
            if role not in item["verification_blocked_roles"]:
                continue
            evidence = evidence_by_role.get(role)
            # Prefer actionable blockers over informational review notes. Keep
            # skipped/failed checks visible: these often explain a blocked
            # verdict even when the report has no explicit blocking finding.
            blocking_findings = []
            failed_checks = []
            if evidence:
                blocking_findings = [finding["description"].strip()
                                     for finding in evidence.get("findings", [])
                                     if isinstance(finding, dict) and finding.get("blocking") is True
                                     and isinstance(finding.get("description"), str)
                                     and finding["description"].strip()]
                failed_checks = []
                for check in evidence.get("checks", []):
                    if not isinstance(check, dict) or check.get("status") not in {"fail", "skip"}:
                        continue
                    name = check.get("name") if isinstance(check.get("name"), str) else "check"
                    status = check.get("status")
                    evidence_text = check.get("evidence")
                    detail = evidence_text.strip() if isinstance(evidence_text, str) else ""
                    failed_checks.append(f"{name} ({status})" + (f": {detail}" if detail else ""))
            detail = ["блокирующее замечание: " + text for text in blocking_findings]
            detail.extend(f"проверка: {text}" for text in failed_checks)
            if not detail and evidence:
                reason = evidence.get("verdict_reason")
                if isinstance(reason, str) and reason.strip():
                    detail.append("причина: " + reason.strip())
            explanation = role + (" — " + "; ".join(detail[:5]) if detail else " — verdict=blocked")
            explanations.append(explanation[:1200])
        state["blocker"] = (
            "Независимая проверка не смогла подтвердить кандидат: " + "; ".join(explanations)
            + ". После устранения причины выполните teamctl resume " + state["id"]
              + " --reason '<что изменилось>' --retry-workers; проверка запустится заново "
                "на той же ревизии, без доработки реализации."
        )
        try:
            self.board.block_external(state["root_card"], state["blocker"])
        except HermesBoardError:
            pass
        self.event(state, state["blocker"])

    def retry_blocked_verification(self, state, item, reason):
        """Create fresh reviewer/QA cards for blocked roles on the same SHA."""
        roles = item.get("verification_blocked_roles") or []
        if not roles:
            raise ContractError("No blocked verification role is recorded for retry")
        candidate = Path(item["workspace"])
        require_clean(candidate)
        if git(candidate, "rev-parse", "HEAD") != item["head_sha"]:
            raise ContractError("Candidate changed while verification was blocked; a new review cycle is required")
        out = self.round_path(state, item)
        sibling_qa_evidence = None
        if "reviewer" in roles and "qa" not in roles:
            # The reviewer may consult already completed QA evidence, but only
            # after binding the exact bytes to the current task, candidate SHAs,
            # and successful native QA run. This evidence never changes the
            # reviewer's verdict or claims that an unrun check passed.
            qa_path = Path(item.get("qa_report_path", out / "QA.json"))
            try:
                qa_bytes = qa_path.read_bytes()
                qa_evidence = json.loads(qa_bytes)
                self._validate_review_report_shape(qa_evidence, "qa", item)
                if qa_evidence.get("verdict") != "pass":
                    raise ContractError("sibling QA report verdict is not pass")
                if any(finding["blocking"] or finding["severity"] in {"high", "critical"}
                       for finding in qa_evidence["findings"]):
                    raise ContractError("sibling QA report contains unresolved findings")
                if any(check["status"] != "pass" or
                       ("exit_code" in check and check["exit_code"] != 0)
                       for check in qa_evidence["checks"]):
                    raise ContractError("sibling QA report contains a failed or skipped check")
                self.verify_actor(item["qa_card"], qa_evidence, "qa")
                if qa_evidence["run_id"] != self._latest_valid_worker_run(item["qa_card"], "qa"):
                    raise ContractError("sibling QA report is not from the latest successful worker run")
                selected_checks = (item.get("verification_plan") or verification_plan(
                    self.project_settings(state)["checks"],
                    self.project_settings(state).get("execution_policy"),
                    item.get("candidate_changed_paths"))).get("selected_checks", [])
                matching_checks = [
                    {"name": check["name"], "argv": check["argv"]}
                    for check in selected_checks
                    if any(row.get("name") == check["name"] and row.get("command") == check["argv"]
                           and row.get("status") == "pass" and row.get("exit_code", 0) == 0
                           for row in qa_evidence["checks"])
                ]
                sibling_qa_evidence = {
                    "path": str(qa_path),
                    "sha256": hashlib.sha256(qa_bytes).hexdigest(),
                    "head_sha": qa_evidence["head_sha"],
                    "run_id": qa_evidence["run_id"],
                    "matching_checks": matching_checks,
                }
            except (OSError, json.JSONDecodeError, ContractError, HermesBoardError, KeyError, TypeError):
                # A bad/stale QA report is not a reason to fail the reviewer
                # retry. Simply do not expose it as reusable evidence.
                sibling_qa_evidence = None
        for role in roles:
            prior_path = Path(item.get(role + "_report_path", out / ("REVIEW.json" if role == "reviewer" else "QA.json")))
            attempt = item.setdefault("verification_retries", {}).get(role, 0) + 1
            archive = out / "report-attempts" / role / "blocked" / f"attempt-{attempt}.json"
            archive.parent.mkdir(parents=True, exist_ok=True)
            if prior_path.exists():
                shutil.copy2(prior_path, archive)
            report_path = out / "report-attempts" / role / f"verification-retry-{attempt}.json"
            previous_card = item[role + "_card"]
            workspace = self.worktree(
                state, f"{item['definition']['id']}-{role}-retry-{attempt}", item["head_sha"])
            retry_review_guidance = ""
            if role == "reviewer":
                retry_review_guidance = (
                    f"Сначала изучи архив предыдущего отчёта reviewer {archive} и доступные через Hermes "
                    f"run details/logs карточки {previous_card}; сопоставь их с точным head_sha={item['head_sha']} "
                    "и командами из плана. Затем изучи проверенный QA-отчёт и его evidence, если он указан ниже. "
                    "Прежде чем повторять дорогую проверку, проверь, есть ли уже надёжное evidence для этой же "
                    "команды и SHA; повторяй её только если evidence отсутствует, неполно или неоднозначно. "
                    "Самостоятельно вынеси новый verdict по проверенным доказательствам и укажи их provenance; "
                    "не копируй прежний verdict автоматически и не выдавай QA evidence за собственный запуск. "
                )
            body = self.context(state) + (
                f"Повторная независимая проверка роли {role} задачи {item['definition']['id']}. "
                f"Предыдущая карточка проверки {previous_card} остаётся в истории; её отчёт с блокером "
                f"сохранён в {archive}. Причина возобновления от владельца: {reason}. "
                f"Проверь {out / 'TASK.md'}, {out / 'IMPLEMENTATION.md'}, SPEC и DESIGN. "
                "Проверь строго тот же snapshot: не меняй код и рабочую копию. "
                f"base_sha={item['base_sha']}, head_sha={item['head_sha']}, task_id={item['card']}, "
                f"role={role}, assignee=team-{role}, run_id — строка из HERMES_KANBAN_RUN_ID. "
                "Сохрани честный отчёт по контракту REVIEW.json/QA.json с verdict=pass, "
                "changes_requested или blocked и непустыми checks. Непройденные проверки не маскируй. "
                + retry_review_guidance +
                f"Для QA быстрые команды по плану: {json.dumps(selected_argv(item.get('verification_plan') or verification_plan(self.project_settings(state)['checks'], self.project_settings(state).get('execution_policy'), item.get('candidate_changed_paths'))))}. "
                + (f"Успешный отчёт QA-соседа для этого же кандидата: {sibling_qa_evidence['path']}; "
                   f"sha256 байтов отчёта={sibling_qa_evidence['sha256']}; "
                   f"head_sha={sibling_qa_evidence['head_sha']}; run_id={sibling_qa_evidence['run_id']}. "
                   "Перед использованием проверь SHA256 файла и совпадение head_sha. Можно учитывать только "
                   "перечисленные ниже проверки, у которых команда совпадает точно; это доказательство QA "
                   "с указанным provenance, а не твой собственный запуск. Не утверждай, что неперечисленные "
                   "или не запущенные проверки пройдены. Совпавшие проверки: "
                   f"{json.dumps(sibling_qa_evidence['matching_checks'], ensure_ascii=False)}. "
                   if role == "reviewer" and sibling_qa_evidence else "") +
                f"Выход: {report_path}. После проверки заверши kanban_complete.\n")
            item[role + "_workspace"] = str(workspace)
            item[role + "_report_path"] = str(report_path)
            item[role + "_card"] = self.spawn(
                state, role, f"Повторная проверка {role}: {item['definition']['title']}",
                body, workspace,
                f"verification-retry:{item['definition']['id']}:{role}:r{item['round']}:a{attempt}",
                parents=[item["card"]])
            item["verification_retries"][role] = attempt
        item.pop("verification_blocked_roles", None)
        item["status"] = "verification"
        state["stage"] = "verification"
        state.pop("resume_stage", None)
        state.pop("blocker", None)

    @staticmethod
    def _validate_integrator_gates(report, plan):
        """Validate gates without treating deferred work as completed evidence."""
        if not isinstance(report.get("gates"), list) or not report["gates"]:
            raise ContractError("Integrator report gates must be a non-empty array")
        blockers = report.get("blocking_items")
        if not isinstance(blockers, list):
            raise ContractError("Integrator report blocking_items must be an array")
        if blockers:
            raise ContractError("Integrator report contains blocking_items")
        findings = report.get("findings", [])
        if not isinstance(findings, list):
            raise ContractError("Integrator report findings must be an array")
        if findings:
            raise ContractError("Integrator report contains negative findings")

        deferred = plan.get("deferred_checks")
        if not isinstance(deferred, list):
            raise ContractError("Controller verification plan has malformed deferred_checks")
        deferred_by_index = {row.get("index"): row for row in deferred
                             if isinstance(row, dict) and type(row.get("index")) is int}
        names: set[str] = set()
        deferred_indexes: set[int] = set()
        for position, gate in enumerate(report["gates"]):
            if not isinstance(gate, dict):
                raise ContractError(f"Integrator gate {position} must be an object")
            name, status = gate.get("name"), gate.get("status")
            if not isinstance(name, str) or not name.strip():
                raise ContractError(f"Integrator gate {position} needs a non-empty name")
            if name in names:
                raise ContractError(f"Integrator gate name is duplicated: {name}")
            names.add(name)
            if gate.get("blocking_items") not in (None, []):
                raise ContractError(f"Integrator gate {name!r} contains blocking items")
            gate_findings = gate.get("findings", [])
            if not isinstance(gate_findings, list):
                raise ContractError(f"Integrator gate {name!r} findings must be an array")
            if gate_findings:
                raise ContractError(f"Integrator gate {name!r} contains negative findings")
            if status == "pass":
                if any(key in gate for key in ("policy_sha256", "deferred_checks")):
                    raise ContractError(f"Passing gate {name!r} cannot carry deferred-check bindings")
                continue
            if status != "deferred":
                raise ContractError(f"Integrator gate {name!r} has unsupported status {status!r}")
            if plan.get("mode") != "quick":
                raise ContractError(f"Integrator gate {name!r} cannot defer checks outside quick mode")
            if gate.get("evidence_ref") != "VERIFICATION-PLAN.json":
                raise ContractError(f"Deferred gate {name!r} must reference VERIFICATION-PLAN.json exactly")
            digest = plan.get("policy_sha256")
            if not isinstance(digest, str) or gate.get("policy_sha256") != digest:
                raise ContractError(f"Deferred gate {name!r} has no matching verification-plan digest")
            supplied = gate.get("deferred_checks")
            if not isinstance(supplied, list) or not supplied:
                raise ContractError(f"Deferred gate {name!r} needs exact deferred_checks bindings")
            indexes = []
            for entry in supplied:
                if not isinstance(entry, dict) or type(entry.get("index")) is not int:
                    raise ContractError(f"Deferred gate {name!r} has a malformed check binding")
                index = entry["index"]
                if index in indexes or index not in deferred_by_index:
                    raise ContractError(f"Deferred gate {name!r} references a non-deferred or duplicate check index {index}")
                if index in deferred_indexes:
                    raise ContractError(f"Deferred check index {index} is repeated across gates")
                if entry != deferred_by_index[index]:
                    raise ContractError(f"Deferred gate {name!r} check {index} does not match the pinned plan")
                indexes.append(index)
                deferred_indexes.add(index)
            if indexes != sorted(indexes):
                raise ContractError(f"Deferred gate {name!r} check bindings must follow plan order")
        if deferred_indexes and deferred_indexes != set(deferred_by_index):
            raise ContractError("Deferred gate bindings must cover every deferred check in the pinned plan")

    def integrate(self, state, item):
        if self._refresh_candidate_on_current_base(state, item, "Перед интеграцией нужна свежая независимая проверка."):
            return
        out = self.round_path(state, item)
        report_path = Path(item.get("integrator_report_path", out / "INTEGRATION.json"))
        report = read_json(report_path)
        if not isinstance(report, dict):
            raise ContractError('INTEGRATION.json must be an object')
        checks = self.project_settings(state)["checks"]
        policy = self.project_settings(state).get("execution_policy")
        plan = verification_plan(checks, policy, item.get("candidate_changed_paths"))
        saved_plan = read_json(out / "VERIFICATION-PLAN.json")
        if saved_plan != plan or item.get("verification_plan") != plan:
            raise ContractError("Current VERIFICATION-PLAN.json does not match the controller-pinned candidate plan")
        self._validate_integrator_gates(report, plan)
        self.verify_actor(item['integrator_card'], report, 'integrator')
        if report.get('feature_id') != state['id']:
            raise ContractError("Integrator report feature_id does not match this request")
        if report.get("base_sha") != item["base_sha"]:
            raise ContractError("Integrator report base_sha does not match the candidate")
        if report.get("head_sha") != item["head_sha"]:
            raise ContractError("Integrator report head_sha does not match the candidate")
        if report.get("decision") != "ready_for_owner":
            raise ContractError("Integrator decision is not ready_for_owner")
        require_clean(item["workspace"])
        require_clean(item["integrator_workspace"])
        if git(item['integrator_workspace'], 'rev-parse', 'HEAD') != item['head_sha']:
            raise ContractError('Integrator changed the inspected revision')
        if git(item["workspace"], "rev-parse", "HEAD") != item["head_sha"]:
            raise ContractError("Candidate changed after review/QA")
        review, qa = self.read_evidence(state, item)
        self.validate_pair(item, review, qa)
        item.setdefault("integration_started_at", int(time.time()))
        quick_commands = selected_argv(plan)
        # Snapshot file makes a successful quick integration recoverable if the daemon dies.
        result_path = out / "GATE-RESULT.json"
        if result_path.exists():
            result = read_json(result_path)
            if result["candidate_sha"] != item["head_sha"] or result["base_sha"] != item["base_sha"]:
                raise ContractError("Existing integration result belongs to a different candidate")
            if git(state["repo"], "rev-parse", result["integration_branch"]) != result["integration_sha"]:
                raise ContractError("Integration branch changed since gate result")
            actual_commands = [row.get("argv") for row in result.get("checks", [])
                               if isinstance(row, dict)]
            expected_quick = quick_commands
            expected_full = [list(command.get("argv", [])) if isinstance(command, dict) else list(command)
                             for command in checks]
            if actual_commands == expected_full:
                plan = verification_plan(checks, policy, item.get("candidate_changed_paths"),
                                         force_full=True)
            elif actual_commands != expected_quick:
                raise ContractError("Recovered integration gate has an unknown check set; "
                                    "cannot bind it to the current verification policy")
        else:
            result = asdict(integrate_candidate(
                state["repo"], task_id=item["card"], base_ref=item["base_sha"], head_sha=item["head_sha"],
                developer_id="team-" + item["definition"]["assignee_role"],
                reviewer_evidence=review, qa_evidence=qa,
                qa_verification_plan=plan,
                trusted_test_commands=quick_commands,
                timeout=self.project_settings(state).get("check_timeout", 180),
                worktree_parent=self.path(state) / "integrations",
                check_progress=lambda progress: self.record_integration_check(state, item, progress),
                check_lock_path=self.root / ".heavy-test.lock"))
        item["status"] = "provisional"
        item["worker_phase"] = "provisional_regression"
        item["provisional_at"] = int(time.time())
        item["integration"] = result
        state["current_sha"] = result["integration_sha"]
        state["staging_sha"] = result["integration_sha"]
        state["candidate_ref"] = result["integration_branch"]
        result["verification_plan"] = plan
        write_json(result_path, result)
        self.event(state, f"{item['definition']['id']} интегрирована: {result['integration_sha'][:12]}; "
                   + ("быстрые проверки пройдены, полная регрессия ожидает пакет."
                      if plan["mode"] == "quick" else
                      "полный набор проверок пройден на накопленном candidate SHA."))
        state.setdefault("pending_regression_items", []).append(item["definition"]["id"])
        if plan["mode"] == "full":
            # Full checks ran on the cumulative merge tree and integrate_candidate
            # committed that exact tree. Reuse this evidence instead of paying for
            # the same full suite a second time in a separate batch worktree.
            pending_ids = list(state["pending_regression_items"])
            batch_id = len(state.get("regression_batches", [])) + 1
            full_batch = {"id": batch_id, "task_ids": pending_ids,
                          "head_sha": result["integration_sha"], "status": "pass",
                          "source": "full_per_item_integration_gate",
                          "started_at": item.get("integration_started_at", item.get("provisional_at")),
                          "completed_at": int(time.time()),
                          "results": result.get("checks", []), "failed_count": 0}
            state.setdefault("regression_batches", []).append(full_batch)
            state["regression_batch"] = full_batch
            for accepted in state["items"]:
                if accepted.get("definition", {}).get("id") in pending_ids:
                    accepted["status"] = "done"
                    accepted["accepted_regression_sha"] = result["integration_sha"]
                    accepted["batch_id"] = batch_id
            state["accepted_regression_sha"] = result["integration_sha"]
            state["pending_regression_items"] = []
            self.event(state, f"Полная интеграционная проверка пакета #{batch_id} уже покрыла "
                       f"{len(pending_ids)} задач на {result['integration_sha'][:12]}; повторный прогон пропущен.")
        state["index"] += 1
        if state["index"] < len(state["items"]):
            state["stage"] = "implementation"
            self.prepare_ready_items(state)
            if self._batch_must_flush(state) or item.get("batch_repair"):
                self.run_pending_regression(state, force=bool(item.get("batch_repair")))
            elif state["items"][state["index"]].get("status") == "planned":
                # No runnable independent task exists until its dependencies are fully accepted.
                self.run_pending_regression(state)
        else:
            self.run_pending_regression(state, force=True)
        self._sync_feature_cursor(state)

    def _sync_feature_cursor(self, state):
        """Keep the legacy scalar cursor aligned with the first not-yet-accepted task."""
        for index, item in enumerate(state.get("items", [])):
            if item.get("status") not in {"done", "provisional"}:
                state["index"] = index
                if state.get("stage") not in {"blocked", "finalizing", "awaiting_delivery"}:
                    state["stage"] = item.get("status") if item.get("status") in {
                        "implementation", "verification", "integration_review"} else "implementation"
                return
        state["index"] = len(state.get("items", []))
        if state.get("pending_regression_items"):
            state["stage"] = "implementation"
        else:
            state["stage"] = "finalizing"
            self.prepare_delivery(state)

    def prepare_delivery(self, state):
        delivery = {"feature_id": state["id"], "status": "ready_for_owner",
                    "base_sha": state["initial_base_sha"], "head_sha": state["accepted_regression_sha"],
                    "candidate_ref": state["candidate_ref"], "protected_branch_modified": False,
                    "regression_sha": state["accepted_regression_sha"],
                    "regression_batches": state.get("regression_batches", []),
                    "tasks": [{"id": i["definition"]["id"], "role": i["definition"]["assignee_role"],
                               "rounds": i["round"], "integration_sha": i["integration"]["integration_sha"]}
                              for i in state["items"] if i.get("integration")]}
        write_json(self.path(state) / "artifacts/DELIVERY.json", delivery)
        try:
            from .efficiency import compute_efficiency
            snapshot = compute_efficiency(state, self.board, repo=state.get("repo"))
            write_json(self.path(state) / "artifacts/EFFICIENCY.json", snapshot)
        except Exception as exc:
            # Metrics are supplementary and must never stop delivery.
            self.event(state, f"Не удалось сформировать необязательную метрику эффективности: {type(exc).__name__}")
            try:
                write_json(self.path(state) / "artifacts/EFFICIENCY.json", {
                    "schema_version": 1, "request_id": state.get("id"),
                    "status": "unavailable", "reason": type(exc).__name__})
            except OSError:
                pass
        if state.get("workflow_managed"):
            from .human_workflow import _make_gate
            _make_gate(self, state, "delivery",
                       "Проверьте результат, локальную ветку-кандидат и доказательства полной регрессии.",
                       ("DELIVERY.json", "EFFICIENCY.json"))
            self.save(state)
            self.event(state, "Результат готов; ожидается приёмка владельца.")
        else:
            self.save(state)
            self.finalize(state)

    def _batch_must_flush(self, state):
        pending = state.get("pending_regression_items", [])
        policy = self.project_settings(state).get("execution_policy") or {}
        batch_size = policy.get("full_after_tasks", 2) if isinstance(policy, dict) else 2
        if len(pending) >= batch_size:
            return True
        if state.get("index", 0) >= len(state.get("items", [])):
            return bool(pending)
        next_item = state["items"][state["index"]]
        dependencies = set(next_item.get("definition", {}).get("dependencies", []))
        if dependencies.intersection(pending):
            return True
        # A provisional task is still unaccepted and therefore still owns its
        # paths. If the cursor task overlaps that scope, the scheduler cannot
        # start it until the pending batch is accepted. Flush now instead of
        # carrying work forward merely because there is no explicit dependency.
        if not self._scopes_are_disjoint_from_unaccepted(state, next_item):
            return True
        return False

    def record_batch_check(self, state, progress):
        visible = {key: progress.get(key) for key in
                   ("event", "index", "total", "name", "argv", "duration_seconds", "status", "exit_code",
                    "elapsed_seconds", "returncode", "result", "timed_out")
                   if key in progress}
        batch = state.setdefault("regression_batch", {})
        batch["progress"] = visible
        batch["updated_at"] = int(time.time())
        self.board.heartbeat(state["root_card"])
        self.save(state)

    def run_pending_regression(self, state, *, force=False):
        pending_ids = list(state.get("pending_regression_items", []))
        if not pending_ids or (not force and not self._batch_must_flush(state)):
            return
        head_sha = state["current_sha"]
        if git(state["repo"], "rev-parse", head_sha) != head_sha:
            raise ContractError("Staging head is not an immutable local revision")
        previous = state.get("regression_batch") or {}
        if previous.get("status") == "running":
            if previous.get("head_sha") != head_sha or previous.get("task_ids") != pending_ids:
                raise ContractError("An interrupted regression batch no longer matches the staging SHA/scope")
            batch = previous
            batch["attempts"] = batch.get("attempts", 1) + 1
            batch["restarted_at"] = int(time.time())
            batch_id = batch["id"]
        else:
            batch_id = len(state.get("regression_batches", [])) + 1
            batch = {"id": batch_id, "task_ids": pending_ids, "head_sha": head_sha,
                     "status": "running", "attempts": 1, "started_at": int(time.time())}
            state.setdefault("regression_batches", []).append(batch)
        batch["status"] = "running"
        state["regression_batch"] = batch
        self.event(state, f"Запущена полная регрессия пакета #{batch_id}: "
                   f"{', '.join(pending_ids)} на {head_sha[:12]}.")
        self.save(state)
        result = run_batch_regression(
            state["repo"], head_sha, self.project_settings(state)["checks"],
            worktree_parent=self.path(state) / "integrations" / "regression-batches",
            timeout=self.project_settings(state).get("check_timeout", 1800),
            check_progress=lambda progress: self.record_batch_check(state, progress),
            check_lock_path=self.root / ".heavy-test.lock")
        batch.update(result)
        batch["completed_at"] = int(time.time())
        history = state.setdefault("regression_batches", [])
        for index, prior in enumerate(history):
            if prior.get("id") == batch_id:
                history[index] = copy.deepcopy(batch)
                break
        if result.get("status") != "pass":
            batch["status"] = "fail"
            self.event(state, f"Полная регрессия пакета #{batch_id} нашла "
                       f"{result.get('failed_count', 0)} проблем; передано на совместную доработку.")
            self.create_batch_repair(state, batch)
            return
        for item in state["items"]:
            if item.get("definition", {}).get("id") in pending_ids:
                item["status"] = "done"
                item["accepted_regression_sha"] = head_sha
                item["batch_id"] = batch_id
        state["accepted_regression_sha"] = head_sha
        state["pending_regression_items"] = []
        self.event(state, f"Полная регрессия пакета #{batch_id} пройдена; "
                   f"{len(pending_ids)} задач приняты на {head_sha[:12]}.")
        self._sync_feature_cursor(state)

    def create_batch_repair(self, state, batch):
        retries = state.get("batch_repair_round", 0) + 1
        if retries > self.config.get("max_rework_rounds", 2):
            raise ContractError("Batch regression rework limit reached; "
                                + json.dumps(batch.get("results", []), ensure_ascii=False)[:5000])
        affected = [item for item in state["items"]
                    if item.get("definition", {}).get("id") in batch["task_ids"]]
        if not affected:
            raise ContractError("Batch regression has no matching task scope")
        paths = sorted({path for item in affected for path in item["definition"].get("owned_paths", [])})
        role = affected[-1]["definition"]["assignee_role"]
        failures = [row for row in batch.get("results", []) if row.get("status") != "pass"]
        repair_text = json.dumps(failures, ensure_ascii=False, indent=2)[:16000]
        existing_numbers = [int(match.group(1)) for row in state["items"]
                            if (match := re.fullmatch(r"T-(\d{3})", row.get("definition", {}).get("id", "")))]
        next_task_number = max(existing_numbers, default=0) + 1
        repair_id = f"T-{next_task_number:03d}"
        definition = {"id": repair_id,
                      "title": f"Совместное исправление регрессии пакета #{batch['id']}",
                      "problem": "Полная регрессия выявила связанные проблемы в накопленных изменениях.",
                      "requirements": ["Исправить все приложенные падения в принадлежащих пакету файлах."],
                      "solution": "Использовать полный сводный отчёт регрессии и исправить причины согласованным пакетом.",
                      "test_plan": ["Повторить быстрые проверки и дождаться полной пакетной регрессии."],
                      "acceptance_criteria": ["Все обязательные полные проверки проходят."],
                      "owned_paths": paths, "priority": "P1",
                      "priority_reason": "Блокирует принятие пакета накопленных задач.",
                      "dependencies": [], "assignee_role": role}
        item = {"definition": definition, "round": retries, "status": "planned",
                "batch_repair": True, "batch_id": batch["id"], "batch_findings": repair_text,
                "branch": f"codex/team-{state['id']}-{repair_id.lower()}-repair-{retries}"}
        workspace = self.path(state) / "worktrees" / repair_id
        parents = [row.get("card") for row in affected if row.get("card")]
        if state.get("planner_card"):
            parents.insert(0, state["planner_card"])
        item["card"] = self.spawn(
            state, role, definition["title"],
            self.context(state) + ("Прочитай новую TASK.md со сводным отчётом полной регрессии. "
                                   "Сделай исправления в scope, локальный commit и обнови IMPLEMENTATION.md."),
            workspace, f"batch-repair:{batch['id']}:r{retries}", parents=parents,
            priority=PRIORITIES["P1"], parked=True)
        state["items"].insert(state["index"], item)
        state["batch_repair_round"] = retries
        state["stage"] = "implementation"
        self.prepare_specific_item(state, item)
        task_path = self.path(state) / "tasks" / definition["id"] / "TASK.md"
        task_path.parent.mkdir(parents=True, exist_ok=True)
        body = task_path.read_text(encoding="utf-8")
        body += ("\n## Сводный отчёт полной пакетной регрессии\n"
                 f"Проверенный SHA: {batch['head_sha']}\n{repair_text}\n")
        task_path.write_text(body, encoding="utf-8")
        out = self.round_path(state, item)
        (out / "TASK.md").write_text(body, encoding="utf-8")
        self.event(state, f"Создана обычная карточка совместной доработки пакета #{batch['id']} "
                   f"в scope: {', '.join(paths)}.")

    def record_integration_check(self, state, item, progress):
        """Persist trusted-check progress and keep the controller claim alive."""
        event = progress.get("event")
        visible = {key: progress.get(key) for key in
                   ("event", "index", "total", "argv", "elapsed_seconds", "returncode", "timed_out", "result")
                   if key in progress}
        visible["task_id"] = item["definition"]["id"]
        visible["updated_at"] = int(time.time())
        state["integration_check"] = visible
        self.save(state)
        # No comment is emitted for periodic heartbeat events. This renews the
        # machine claim while a long trusted command is running.
        self.board.heartbeat(state["root_card"])
        if event in {"started", "completed"}:
            command = progress.get("argv") or []
            executable = Path(command[0]).name if command and isinstance(command[0], str) else "команда"
            if event == "started":
                message = (f"Запущена обязательная проверка {progress.get('index')}/"
                           f"{progress.get('total')}: {executable}.")
            else:
                message = (f"Завершена обязательная проверка {progress.get('index')}/"
                           f"{progress.get('total')}: {progress.get('result', 'unknown')}.")
            self.event(state, message)
            self.save(state)

    def finalize(self, state):
        delivery = read_json(self.path(state) / 'artifacts/DELIVERY.json')
        if self.board.get_task(state['root_card'])['status'] != 'done':
            self.board.machine_card(f"[{state['id']}] {state['title']}",
                                    idempotency_key=self.root_key(state))
            self.board.complete_external(
                state["root_card"], "Фича подготовлена в локальной интеграционной ветке; "
                "review, QA и обязательная регрессия пройдены. Protected branch не изменена.", delivery)
        state['stage'] = 'done'
        self.save(state)

    def tick(self, state):
        if state["stage"] not in {"done", "cancelled"}:
            try:
                self.reconcile_test_waits(state)
                self._continue_budget_exhausted_implementation(state)
            except (ContractError, GitGateError, HermesBoardError, ValueError, OSError,
                    TypeError, KeyError, AttributeError, IndexError) as exc:
                if state.get("stage") != "blocked":
                    state["resume_stage"] = state["stage"]
                state["stage"] = "blocked"
                state["blocker"] = ("Automatic implementation budget continuation needs attention: "
                                    + str(exc))[:3000]
                try:
                    if state.get("root_card"):
                        self.board.block_external(state["root_card"], state["blocker"])
                except HermesBoardError:
                    pass
                self.event(state, "Требуется внимание: " + state["blocker"])
                self.update_efficiency(state)
                self.save(state)
                return
        if state["stage"] in TERMINAL or state["stage"] == "blocked":
            self.reconcile_dispatch(state, activate=False)
            self.update_efficiency(state)
            self.save(state)
            return
        if state["stage"] in {"queued", "awaiting_requirements", "awaiting_plan",
                              "awaiting_delivery", "awaiting_owner_input"}:
            self.update_efficiency(state)
            self.save(state)
            return
        try:
            # Old persisted runs already have a trusted fully-regressed baseline.
            # Establish the new explicit acceptance marker without changing that history.
            state.setdefault("accepted_regression_sha", state.get("current_sha"))
            state.setdefault("staging_sha", state.get("current_sha"))
            state.setdefault("pending_regression_items", [])
            state.setdefault("scheduler_version", 1)
            state.setdefault("scheduler_policy", {
                "version": 1, "max_parallel_tasks": self.max_parallel_tasks()})
            if (state.get("regression_batch", {}).get("status") == "running"
                    and state.get("pending_regression_items")):
                # A daemon restart may interrupt full suites. Re-run against the
                # same immutable head before advancing any dependent task.
                self.run_pending_regression(state, force=True)
            self.prepare_ready_items(state)
            if state['stage'] == 'finalizing':
                self.finalize(state)
                return
            root_card = self.board.get_task(state["root_card"])
            if state.get('resume_requested'):
                self._finish_resume_request(state)
                root_card = self.board.get_task(state['root_card'])
            if root_card["status"] != "running":
                # A controller restart may leave a stale claim. Recover only our machine card.
                self.board.machine_card(f"[{state['id']}] {state['title']}",
                                        idempotency_key=self.root_key(state))
            self.board.heartbeat(state["root_card"])
            self.reconcile_dispatch(state)
            environment_help = self.prerequisites(state)
            state["environment_wait_active"] = any(
                self.board.get_task(entry["card_id"])["status"] == "blocked"
                for entry in environment_help)
            if any(self.board.get_task(entry['card_id'])['status'] == 'blocked'
                   for entry in environment_help):
                raise ContractError('Не хватает компонента среды. Подробности и доступная команда '
                                    'отправляются владельцу в Telegram: '
                                    + '; '.join(entry['reason'] for entry in environment_help))
            if git(state["repo"], "rev-parse", state["base_ref"]) != state["initial_base_sha"]:
                raise ContractError("Target base branch changed; plan and evidence require a fresh baseline")
            stage = state["stage"]
            if stage == "analysis" and state.get("workflow_managed"):
                analyst_task = self.board.get_task(state["analysis_card"])
                if (analyst_task and state['analysis_card'] not in self.waiting_cards
                        and analyst_task.get("status") in {"blocked", "triage"}):
                    from .human_workflow import _make_owner_question_gate, _validated_questions
                    questions_path = self.path(state) / "artifacts" / "QUESTIONS.json"
                    if questions_path.is_file() and not _validated_questions(questions_path):
                        self.block_owner_question_contract(state, questions_path, 'аналитик')
                        return
                    question = (analyst_task.get("blocked_reason")
                                or analyst_task.get("block_reason")
                                or analyst_task.get("reason")
                                or "Аналитик запросил решение или исходные данные владельца.")
                    has_spec = (self.path(state) / "artifacts" / "SPEC.md").is_file()
                    _make_owner_question_gate(self, state, question,
                                              "SPEC.md" if has_spec else "BRIEF.md")
                    self.event(state, "Аналитик ожидает ответа владельца: " + str(question))
                    self.save(state)
                    return
            if stage == "analysis" and self.done(state["analysis_card"]):
                if (state.get("workflow_managed")
                        and state.get("analysis_input_revision", 1) < state.get("brief_revision", 1)):
                    spec = self.path(state) / "artifacts" / "SPEC.md"
                    if spec.exists():
                        archive = self.path(state) / "artifacts" / (
                            f"SPEC-superseded-r{state.get('analysis_round', 1)}.md")
                        shutil.copy2(spec, archive)
                    state["analysis_round"] = state.get("analysis_round", 1) + 1
                    self.launch_analysis(state)
                    self.event(state, "Во время анализа поступило уточнение; аналитик запускает новый раунд.",
                               role="team-analyst")
                elif state.get("workflow_managed"):
                    from .human_workflow import _make_gate
                    spec_text = (self.path(state) / "artifacts" / "SPEC.md").read_text(encoding="utf-8")
                    questions_path = self.path(state) / "artifacts" / "QUESTIONS.json"
                    from .human_workflow import _validated_questions
                    structured_questions = _validated_questions(questions_path) if questions_path.is_file() else []
                    if questions_path.is_file() and not structured_questions:
                        self.block_owner_question_contract(state, questions_path, 'аналитик')
                        return
                    if structured_questions or "**Итог:** blocked" in spec_text:
                        questions = [line.strip() for line in spec_text.splitlines()
                                     if line.strip().startswith("| Q-") and "| yes |" in line.lower()]
                        question = "Нужно ваше решение, чтобы продолжить требования. " + (
                            " ".join(questions) if questions else
                            "Выберите подходящий вариант в карточке задачи.")
                        from .human_workflow import _make_owner_question_gate
                        _make_owner_question_gate(self, state, question, "SPEC.md")
                        self.event(state, "Спецификация содержит блокирующий вопрос; ожидается ответ владельца.")
                        self.save(state)
                        return
                    _make_gate(self, state, "requirements",
                               "Проверьте проектный анализ и согласуйте проверяемые требования.",
                               ("SPEC.md",))
                    self.event(state, "Анализ готов; требуется согласование требований владельцем.")
                else:
                    self.new_planner(state)
            elif stage == "planning" and self.done(state["planner_card"]):
                # Only document contract errors merit an automatic author retry.
                # Unexpected code edits or dispatch failures must remain blocked.
                require_clean(self.path(state) / 'worktrees/planning')
                require_artifact(self.path(state) / 'artifacts/DESIGN.md', 200)
                approved_spec = state.get("approved_spec")
                if approved_spec is not None:
                    spec_path = self.path(state) / "artifacts" / "SPEC.md"
                    current = spec_path.read_text(encoding="utf-8")
                    if hashlib.sha256(current.encode("utf-8")).hexdigest() != state.get("approved_spec_sha256"):
                        archive = self.path(state) / "artifacts" / f"SPEC-planner-edit-r{state.get('plan_round', 1)}.md"
                        shutil.copy2(spec_path, archive)
                        spec_path.write_text(approved_spec, encoding="utf-8")
                        state["plan_round"] = state.get("plan_round", 1) + 1
                        self.new_planner(state, "The planner changed approved SPEC.md. "
                            "The controller restored the owner-approved version. Revise only DESIGN.md "
                            "and PLAN.json to comply with the exact owner-approved requirements.")
                        self.event(state, "Восстановлена утверждённая спецификация; запущен новый раунд плана.")
                        return
                try:
                    validate_plan(read_json(self.path(state) / 'artifacts/PLAN.json'),
                                  project=state['project'], feature_id=state['id'],
                                  base_sha=state['initial_base_sha'],
                                  max_tasks=self.config.get('max_plan_tasks', 12))
                except (ContractError, json.JSONDecodeError) as exc:
                    self.repair_plan_contract(state, str(exc))
                else:
                    self.challenge_plan(state)
            elif stage == 'plan_challenge' and self.done(state['skeptic_card'], state['test-designer_card']):
                retry_scheduled = False
                for role in ('skeptic', 'test-designer'):
                    try:
                        self.preflight_plan_review_report(state, role)
                    except (PlanGateError, json.JSONDecodeError, OSError) as exc:
                        self.retry_invalid_plan_report(state, role, str(exc))
                        retry_scheduled = True
                        break
                if not retry_scheduled:
                    self.start_judge(state)
            elif stage == 'plan_judgment' and self.done(state['judge_card']):
                self.decide_plan(state)
            elif stage == "implementation":
                if state["index"] >= len(state["items"]):
                    if state.get("pending_regression_items"):
                        self.run_pending_regression(state, force=True)
                    return
                item = state["items"][state["index"]]
                if item['status'] == 'planned':
                    self.prepare_item(state)
                elif self.done(item["card"]):
                    if self._refresh_candidate_on_current_base(
                            state, item, "Кандидат ещё не проходил review/QA на актуальной интеграционной базе."):
                        pass
                    else:
                        self.start_review(state, item)
            elif stage == "verification":
                item = state["items"][state["index"]]
                if self.done(item["reviewer_card"], item["qa_card"]):
                    retry_scheduled = False
                    for role in ("reviewer", "qa"):
                        out = self.round_path(state, item)
                        fallback = out / ("REVIEW.json" if role == "reviewer" else "QA.json")
                        report_path = Path(item.get(role + "_report_path", fallback))
                        try:
                            report = read_json(report_path)
                            self._validate_review_report_shape(report, role, item)
                            actual_run_id = self._latest_valid_worker_run(item[role + "_card"], role)
                            if report["run_id"] != actual_run_id:
                                raise ContractError(f"{role} report run_id does not match its native worker run")
                        except (ContractError, json.JSONDecodeError, OSError) as exc:
                            self.retry_invalid_review_report(state, item, role, str(exc))
                            retry_scheduled = True
                            break
                    if not retry_scheduled:
                        if not self._refresh_candidate_on_current_base(
                                state, item, "Review/QA отчёты привязаны к устаревшей базе."):
                            self.start_integrator(state, item)
            elif stage == "integration_review":
                item = state["items"][state["index"]]
                if self.done(item["integrator_card"]):
                    self.integrate(state, item)
        except (ContractError, GitGateError, HermesBoardError, ValueError, OSError,
                TypeError, KeyError, AttributeError, IndexError) as exc:
            state["resume_stage"] = state["stage"]
            state["stage"] = "blocked"
            state["blocker"] = str(exc)[:3000]
            try:
                self.board.block_external(state["root_card"], state["blocker"])
            except HermesBoardError:
                pass  # Preserve blocker durably even if the native claim was reaped.
            self.event(state, "Требуется внимание: " + state["blocker"])
        finally:
            self.update_efficiency(state)
            self.save(state)

    @staticmethod
    def update_efficiency(state):
        from .efficiency import update_efficiency_intervals
        update_efficiency_intervals(state)

    def tick_all(self):
        if self.quota:
            probe_stamp = self.root / '.quota-refreshed'
            if not probe_stamp.exists() or time.time() - probe_stamp.stat().st_mtime > 60:
                from .quota_sources import refresh_snapshots
                refresh_snapshots(self.quota, self.config, self.config['hermes_root'])
                probe_stamp.touch()
        # Shared serialization for submissions, decisions, queue promotion and
        # state updates. Always acquire the feature lock second.
        with file_lock(self.root / ".submit.lock"):
            paths = sorted((self.root / "runs").glob("*/state.json"))
            states = [read_json(path) for path in paths]
            executing = [state for state in states if state.get("stage") in {
                "analysis", "planning", "plan_challenge", "plan_judgment",
                "implementation", "verification", "integration_review", "finalizing"}]
            if not executing:
                queued = sorted((state for state in states if state.get("stage") == "queued"),
                                key=lambda state: (state.get("created_at", 0), state.get("id", "")))
                if queued:
                    queued_state = queued[0]
                    state_path = self.path(queued_state) / "state.json"
                    with file_lock(state_path.parent / ".lock"):
                        state = read_json(state_path)
                        if state.get("stage") == "queued":
                            try:
                                if (state.get("workflow_managed")
                                    and state.get("queue_reason") == "judge_answered"):
                                    self.start_judge(state, state.get("owner_answer"))
                                    self.event(state, "Ответ владельца передан судье плана.",
                                               role="team-judge")
                                elif (state.get("workflow_managed")
                                    and state.get("queue_reason") == "plan_approved"):
                                    if self.plan_digest(state) != state.get("approved_plan_sha256"):
                                        raise ContractError("Approved plan changed before implementation admission")
                                    if (state.get("approved_spec") is not None
                                        and hashlib.sha256((self.path(state) / "artifacts" / "SPEC.md")
                                                           .read_bytes()).hexdigest()
                                        != state.get("approved_spec_sha256")):
                                        raise ContractError("Approved requirements changed before implementation admission")
                                    self.accept_plan(state)
                                elif (state.get("workflow_managed")
                                    and state.get("queue_reason") == "plan_revised"):
                                    self.new_planner(state, state.get("plan_review_path"))
                                    self.event(state, "Владелец запросил пересмотр плана; запущен новый раунд.")
                                elif (state.get("workflow_managed")
                                    and state.get("queue_reason") == "requirements_approved"
                                    and state.get("analysis_card")
                                    and (self.path(state) / "artifacts" / "SPEC.md").is_file()):
                                    if (state.get("approved_spec_sha256")
                                            and hashlib.sha256((self.path(state) / "artifacts" / "SPEC.md")
                                                               .read_bytes()).hexdigest()
                                            != state.get("approved_spec_sha256")):
                                        raise ContractError("Approved requirements changed before planning admission")
                                    self.new_planner(state)
                                    self.event(state, "Владелец утвердил требования; начата декомпозиция.")
                                else:
                                    self.launch_analysis(state)
                                state.pop("queue_reason", None)
                                if state.get("stage") == "analysis":
                                    self.event(state, "Запрос запущен после освобождения слота.",
                                               role="team-analyst")
                                self.save(state)
                            except (ContractError, GitGateError, HermesBoardError, ValueError,
                                    OSError, TypeError, KeyError, AttributeError) as exc:
                                state["resume_stage"] = "queued"
                                state["stage"] = "blocked"
                                state["blocker"] = str(exc)[:3000]
                                try:
                                    if state.get("root_card"):
                                        self.board.block_external(state["root_card"], state["blocker"])
                                except HermesBoardError:
                                    pass
                                self.save(state)
            for path in paths:
                with file_lock(path.parent / ".lock"):
                    self.tick(read_json(path))
            from .human_workflow import reconcile_delivery_followups
            reconcile_delivery_followups(self)

    def summaries(self):
        return [{k: state.get(k) for k in ("id", "project", "title", "stage", "index",
                                          "current_sha", "candidate_ref", "blocker", "root_card",
                                          "integration_check",
                                          "waiting_for_quota")}
                for state in (read_json(p) for p in sorted((self.root / "runs").glob("*/state.json")))]


def main():
    parser = argparse.ArgumentParser(description="Hermes engineering team")
    parser.add_argument("--config", default=os.environ.get("HERMES_TEAM_CONFIG",
                        "/home/alex/.hermes/team/config.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("submit")
    p.add_argument("--project", required=True)
    p.add_argument("--brief", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--id")
    sub.add_parser("status")
    sub.add_parser("tick")
    sub.add_parser("daemon")
    sub.add_parser("stop")
    q = sub.add_parser('quota')
    q.add_argument('action', choices=['status','refresh'])
    p = sub.add_parser("resume")
    p.add_argument("feature")
    p.add_argument("--reason", required=True)
    p.add_argument("--retry-workers", action="store_true",
                   help="Unblock current blocked worker cards after resolving their cause")
    args = parser.parse_args()
    controller = Controller(args.config)
    if args.command == "submit":
        from .human_workflow import submit_request
        result = submit_request(args.config, project=args.project,
                                brief=Path(args.brief).read_text(), title=args.title,
                                request_id=args.id or str(uuid.uuid4()),
                                actor="cli:owner", channel="cli")
        print(json.dumps({"id": result["id"], "stage": result["stage"],
                          "stage_label": result["stage_label"]}, ensure_ascii=False))
    elif args.command == "status":
        print(json.dumps(controller.summaries(), ensure_ascii=False, indent=2))
    elif args.command == 'quota':
        if not controller.quota:
            raise ContractError('Quota policy is not configured')
        if args.action == 'refresh':
            from .quota_sources import refresh_snapshots
            refresh_snapshots(controller.quota, controller.config, controller.config['hermes_root'])
        print(json.dumps(controller.quota.status(), ensure_ascii=False, indent=2))
    elif args.command == "tick":
        with file_lock(controller.root / ".daemon.lock"):
            controller.tick_all()
    elif args.command == 'stop':
        pidfile = controller.root / '.daemon.pid'
        if pidfile.exists():
            record = read_json(pidfile)
            proc = Path('/proc') / str(record['pid'])
            try:
                start = (proc / 'stat').read_text().split()[21]
                cmdline = (proc / 'cmdline').read_bytes()
                if start == record['start'] and b'team_engine.controller\x00daemon' in cmdline:
                    os.kill(record['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass
            except FileNotFoundError:
                pass
    elif args.command == "resume":
        path = controller.root / "runs" / safe_id(args.feature) / "state.json"
        # Serialize admission with submit and use the same lock order everywhere:
        # global admission lock, then the feature state lock.
        with file_lock(controller.root / ".submit.lock"):
            with file_lock(path.parent / ".lock"):
                state = read_json(path)
                controller.resume_blocked(state, args.reason, retry_workers=args.retry_workers)

    else:
        with file_lock(controller.root / ".daemon.lock", blocking=False):
            write_json(controller.root / '.daemon.pid', {
                'pid': os.getpid(), 'start': Path('/proc/self/stat').read_text().split()[21]})
            print("Hermes team controller started", flush=True)
            while True:
                try:
                    # The web editor installs managed profiles then commits
                    # the config under this lock. Do not dispatch into a
                    # partially applied profile/config transaction.
                    with file_lock(str(Path(args.config).resolve()) + '.lock'):
                        controller = Controller(args.config)
                        controller.tick_all()
                except Exception as exc:
                    print(f"Controller tick failed: {type(exc).__name__}: {exc}", flush=True)
                time.sleep(controller.config.get("poll_seconds", 5))


if __name__ == "__main__":
    main()
