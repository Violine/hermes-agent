"""Owner-only Telegram intake, approval and notification interface.

The Telegram gateway already authenticates its users. This module applies a
second, workflow-specific owner allowlist and limits commands to private chats.
It imports Telegram lazily so the workflow package remains usable without PTB.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from typing import Any

from . import human_workflow
from .controller import ContractError, file_lock, read_json
from .prerequisites import CATALOG as PREREQUISITE_CATALOG
from .prerequisites import format_prerequisite_notice as _render_prerequisite_notice

log = logging.getLogger(__name__)
CONFIG_DEFAULT = "/home/alex/.hermes/team/config.json"
CALLBACK_PREFIX = "team:"
MAX_MESSAGE = 3500
POLL_SECONDS = 10
TOKEN_TTL = 7 * 24 * 60 * 60
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_STAGED_FILES = 50
MAX_STAGED_BYTES = 30 * 1024 * 1024
_TEAM_COMMAND = re.compile(r"^/team(?:@[A-Za-z0-9_]+)?(?:\s|$)", re.I)


def _config_path() -> Path:
    return Path(os.environ.get("HERMES_TEAM_CONFIG", CONFIG_DEFAULT)).expanduser()


def _config(path: Path | None = None) -> dict[str, Any]:
    value = read_json(path or _config_path())
    if not isinstance(value, dict):
        raise ValueError("Некорректная конфигурация команды")
    return value


def _telegram_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("telegram", {})
    return value if isinstance(value, dict) else {}


def _owner_ids(config: dict[str, Any]) -> set[str]:
    raw = _telegram_config(config).get("allowed_user_ids", [])
    if isinstance(raw, (str, int)):
        raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {str(item).strip() for item in raw if str(item).strip().isdigit()}


def _is_private(update: Any) -> bool:
    chat = getattr(update, "effective_chat", None)
    return bool(chat and str(getattr(chat, "type", "")) == "private")


def _is_forwarded(message: Any) -> bool:
    return any(getattr(message, key, None) is not None for key in (
        "forward_origin", "forward_from", "forward_from_chat", "forward_sender_name",
    ))


def _authorized(update: Any, adapter: Any, *, callback: bool = False) -> bool:
    """Check gateway authorization and the explicit project owner allowlist."""
    if not _is_private(update):
        return False
    user = getattr(update, "effective_user", None)
    if user is None or getattr(user, "is_bot", False):
        return False
    user_id = str(getattr(user, "id", ""))
    try:
        config = _config()
        if _telegram_config(config).get("enabled") is False or user_id not in _owner_ids(config):
            return False
    except (OSError, ValueError, TypeError):
        return False
    if callback:
        query = getattr(update, "callback_query", None)
        message = getattr(query, "message", None)
        if message is None or _is_forwarded(message):
            return False
        # The callback actor (not the bot-authored message carrying the
        # keyboard) must be checked by Hermes' gateway allowlist.
        from types import SimpleNamespace
        message = SimpleNamespace(from_user=user, chat=update.effective_chat,
                                  message_id=getattr(message, "message_id", 0))
    else:
        message = getattr(update, "effective_message", None)
        if message is None or _is_forwarded(message):
            return False
    check = getattr(adapter, "_is_user_authorized_from_message", None)
    if not callable(check):
        return False
    try:
        return bool(check(message))
    except Exception:
        log.exception("Hermes Telegram authorization check failed")
        return False


def _state_root(config: dict[str, Any]) -> Path:
    return Path(config["data_root"]).expanduser() / "telegram-workflow"


def _stage_path(config: dict[str, Any]) -> Path:
    return _state_root(config) / "staged-attachments.json"


def _upload_event_id(update: Any, caption: str) -> str:
    message = update.effective_message
    chat_id = str(update.effective_chat.id)
    digest = hashlib.sha256(caption.encode()).hexdigest()[:16]
    return f"telegram:{chat_id}:{message.message_id}:{digest}"


def _intent_key(chat_id: str, user_id: str, request_id: str) -> str:
    return f"{user_id}:{chat_id}:{request_id}"


def _read_intent(config: dict[str, Any], chat_id: str, user_id: str,
                 request_id: str) -> dict[str, Any] | None:
    path = _stage_path(config)
    with file_lock(path.with_suffix(".lock")):
        data = _load_json(path, {})
    intents = data.get("__intents__", {}) if isinstance(data, dict) else {}
    value = intents.get(_intent_key(chat_id, user_id, request_id)) if isinstance(intents, dict) else None
    return dict(value) if isinstance(value, dict) else None


def _staged(config: dict[str, Any], chat_id: str, user_id: str) -> list[dict[str, Any]]:
    """Return this owner's pending Telegram document references only."""
    path = _stage_path(config)
    with file_lock(path.with_suffix(".lock")):
        data = _load_json(path, {})
    if not isinstance(data, dict):
        return []
    values = data.get(f"{user_id}:{chat_id}", [])
    return [dict(item) for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _stage_attachment(config: dict[str, Any], chat_id: str, user_id: str,
                      item: dict[str, Any]) -> list[dict[str, Any]]:
    path = _stage_path(config)
    with file_lock(path.with_suffix(".lock")):
        data = _load_json(path, {})
        if not isinstance(data, dict):
            data = {}
        key = f"{user_id}:{chat_id}"
        current = data.get(key, [])
        current = [dict(value) for value in current if isinstance(value, dict)] if isinstance(current, list) else []
        if not any(value.get("id") == item.get("id") for value in current):
            if len(current) >= MAX_STAGED_FILES:
                raise human_workflow.WorkflowError("Слишком много ожидающих файлов. Используйте /team clearfiles и загрузите нужные документы заново.")
            total = sum(int(value.get("size", 0)) for value in current)
            if total + int(item.get("size", 0)) > MAX_STAGED_BYTES:
                raise human_workflow.WorkflowError("Общий размер ожидающих файлов превышает 30 МиБ. Создайте задачу с текущими файлами или очистите список командой /team clearfiles.")
            current.append({key: item.get(key) for key in ("id", "name", "size", "mime_type")})
            data[key] = current
            _atomic_json(path, data)
        return current


def _clear_staged(config: dict[str, Any], chat_id: str, user_id: str) -> int:
    path = _stage_path(config)
    with file_lock(path.with_suffix(".lock")):
        data = _load_json(path, {})
        if not isinstance(data, dict):
            return 0
        key = f"{user_id}:{chat_id}"
        current = data.pop(key, [])
        _atomic_json(path, data)
    return len(current) if isinstance(current, list) else 0


def _submit_with_staged(config_path: Path, *, chat_id: str, user_id: str,
                        attach_to: str | None = None, **kwargs) -> dict[str, Any]:
    """Persist an idempotent attachment snapshot before submit/attach."""
    config = _config(config_path)
    path = _stage_path(config)
    with file_lock(path.with_suffix(".lock")):
        data = _load_json(path, {})
        data = data if isinstance(data, dict) else {}
        key = f"{user_id}:{chat_id}"
        request_id = str(kwargs.get("request_id", ""))
        intent_key = _intent_key(chat_id, user_id, request_id)
        intents = data.setdefault("__intents__", {})
        if not isinstance(intents, dict):
            intents = data["__intents__"] = {}
        intent = intents.get(intent_key)
        expected = {"action": "attach" if attach_to else "submit", "feature_id": attach_to}
        if isinstance(intent, dict):
            if intent.get("action") != expected["action"] or intent.get("feature_id") != attach_to:
                raise human_workflow.WorkflowConflict("Telegram update id was already used for another document action.")
            attachment_ids = [str(value) for value in intent.get("attachment_ids", [])]
        else:
            staged = data.get(key, [])
            staged = [dict(item) for item in staged if isinstance(item, dict)] if isinstance(staged, list) else []
            attachment_ids = [str(item["id"]) for item in staged if item.get("id")]
            if attach_to and not attachment_ids:
                raise human_workflow.WorkflowError("Нет ожидающих документов. Сначала отправьте файл с подписью /team upload.")
            intents[intent_key] = {**expected, "attachment_ids": attachment_ids,
                                   "created_at": int(time.time())}
            # Record the chosen files before invoking workflow. A lost reply or
            # process interruption will retry the same immutable file set.
            _atomic_json(path, data)
        if attach_to:
            result = human_workflow.attach_documents(config_path, attach_to,
                attachment_ids=attachment_ids, **kwargs)
        else:
            result = human_workflow.submit_request(config_path, attachments=attachment_ids, **kwargs)
        if attachment_ids:
            staged = data.get(key, [])
            staged = staged if isinstance(staged, list) else []
            data[key] = [item for item in staged if not isinstance(item, dict)
                         or item.get("id") not in set(attachment_ids)]
            _atomic_json(path, data)
        return result


def _attachment_names(config: dict[str, Any], chat_id: str, user_id: str) -> str:
    items = _staged(config, chat_id, user_id)
    if not items:
        return "Нет ожидающих документов. Пришлите файл с подписью /team upload."
    lines = ["Ожидающие документы (они ещё не привязаны к задаче):"]
    lines.extend(f"• {item.get('name', 'Документ')} — {int(item.get('size', 0)) / 1024:.0f} КБ · ID {item.get('id')}" for item in items)
    lines.append("Создайте задачу с ними через /team new … или привяжите к существующей: /team attach <ID>")
    return "\n".join(lines)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _gate_digest(approval: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    for item in approval.get("documents", []):
        digest.update(str(item.get("name", "")).encode("utf-8") + b"\0")
        digest.update(str(item.get("content", "")).encode("utf-8") + b"\0")
    questions = approval.get("questions", [])
    if isinstance(questions, list):
        questions = [{key: value for key, value in item.items() if key != "answer"}
                     if isinstance(item, dict) else item for item in questions]
    digest.update(json.dumps({"question": approval.get("question"),
                              "questions": questions},
                             ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()


def _save_action_token(config: dict[str, Any], *, feature_id: str, action: str,
                       gate_id: str = "", digest: str = "", user_id: str,
                       chat_id: str, question_id: str = "", option_id: str = "") -> str:
    root = _state_root(config)
    path, lock = root / "callbacks.json", root / ".callbacks.lock"
    token = secrets.token_urlsafe(9)
    with file_lock(lock):
        items = _load_json(path, {})
        if not isinstance(items, dict):
            items = {}
        now = int(time.time())
        items = {key: value for key, value in items.items()
                 if isinstance(value, dict) and int(value.get("expires", 0)) > now}
        items[token] = {"feature_id": feature_id, "action": action,
                        "gate_id": gate_id, "user_id": user_id,
                        "chat_id": chat_id, "digest": digest,
                        "question_id": question_id, "option_id": option_id,
                        "expires": now + TOKEN_TTL}
        _atomic_json(path, items)
    return token


def _get_action_token(config: dict[str, Any], token: str) -> dict[str, Any] | None:
    root = _state_root(config)
    path = root / "callbacks.json"
    with file_lock(root / ".callbacks.lock"):
        items = _load_json(path, {})
        item = items.get(token) if isinstance(items, dict) else None
        if not isinstance(item, dict) or int(item.get("expires", 0)) <= int(time.time()):
            return None
        return dict(item)


def _questions(approval: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return unanswered structured owner questions, with a legacy fallback."""
    if not isinstance(approval, dict):
        return []
    raw = approval.get("questions")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict) and not item.get("answer")]
    legacy = approval.get("question")
    if isinstance(legacy, str) and legacy.strip():
        return [{"id": "legacy", "question": legacy.strip(), "options": []}]
    return []


def _has_structured_questions(approval: dict[str, Any] | None) -> bool:
    return isinstance(approval, dict) and isinstance(approval.get("questions"), list)


def _question_text(question: dict[str, Any], index: int = 1) -> str:
    lines = [f"Вопрос {index}: {_safe_label(question.get('question'), 900)}"]
    example = _safe_label(question.get("example"), 700)
    if example:
        lines.append("Пример: " + example)
    recommendation = _safe_label(question.get("recommendation_reason"), 700)
    recommended_id = question.get("recommended_option_id")
    options = question.get("options") if isinstance(question.get("options"), list) else []
    for option in options[:8]:
        if isinstance(option, dict):
            label = _safe_label(option.get("label"), 100)
            description = _safe_label(option.get("description"), 220)
            if label:
                lines.append(f"• {label}" + (f" — {description}" if description else ""))
    recommended = next((item for item in options if isinstance(item, dict)
                        and item.get("id") == recommended_id), None)
    if recommendation:
        prefix = f"Рекомендую «{_safe_label(recommended.get('label'), 100)}»: " if recommended else "Рекомендация: "
        lines.append(prefix + recommendation)
    return "\n".join(lines)


def _question_keyboard(config: dict[str, Any], request: dict[str, Any], question: dict[str, Any],
                       *, user_id: str, chat_id: str) -> Any | None:
    approval = request.get("approval") or {}
    digest = _gate_digest(approval)
    rows: list[list[tuple[str, str]]] = []
    status_token = _save_action_token(config, feature_id=request["id"], action="show",
                                      user_id=user_id, chat_id=chat_id)
    rows.append([("Открыть карточку", f"{CALLBACK_PREFIX}s:{status_token}")])
    options = question.get("options") if isinstance(question.get("options"), list) else []
    for option in options[:8]:
        if not isinstance(option, dict) or not option.get("id"):
            continue
        label = _safe_label(option.get("label"), 52)
        token = _save_action_token(config, feature_id=request["id"], action="answer_option",
            gate_id=str(approval.get("id", "")), digest=digest, user_id=user_id,
            chat_id=chat_id, question_id=str(question.get("id", "")),
            option_id=str(option.get("id", "")))
        rows.append([(label or "Выбрать", f"{CALLBACK_PREFIX}q:{token}")])
    token = _save_action_token(config, feature_id=request["id"], action="answer_custom",
        gate_id=str(approval.get("id", "")), digest=digest, user_id=user_id,
        chat_id=chat_id, question_id=str(question.get("id", "")))
    rows.append([("Ответить своими словами", f"{CALLBACK_PREFIX}q:{token}")])
    return _button_markup(rows)


def _prerequisite_items(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only bounded structured prerequisites from a blocked request.

    Generic blockers (quota, authentication, controller errors) are not enough
    to trigger install advice. The worker must have supplied the validated,
    normalized prerequisite projection and the controller must have blocked.
    """
    if request.get("stage") != "blocked":
        return []
    raw = request.get("prerequisites")
    if not isinstance(raw, list):
        return []
    items = []
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        evidence = item.get("evidence")
        reason = item.get("reason")
        if not isinstance(item_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", item_id):
            continue
        if not isinstance(evidence, str) or not isinstance(reason, str):
            continue
        # The catalog is always resolved locally. Never trust a projected
        # catalog field or a command that might have come from worker output.
        catalog = PREREQUISITE_CATALOG.get(item_id)
        items.append({"id": item_id, "evidence": _safe_label(evidence, 400),
                      "reason": _safe_label(reason, 500), "catalog": catalog})
    return [item for item in items if item["evidence"] and item["reason"]]


def _prerequisite_digest(request: dict[str, Any]) -> str:
    value = request.get("prerequisite_digest")
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) else ""


def _has_actionable_prerequisites(request: dict[str, Any]) -> bool:
    return bool(_prerequisite_digest(request) and _prerequisite_items(request))


def format_prerequisite_notice(request: dict[str, Any],
                               config: dict[str, Any] | None = None) -> str:
    """Render a concise owner notice using only the trusted prerequisite catalog."""
    items = _prerequisite_items(request)
    if not items or not _prerequisite_digest(request):
        return ""
    text = _render_prerequisite_notice(items)
    title = _safe_label(request.get("title"), 160)
    if title:
        text = f"Задача: {title}\n\n" + text
    if config is not None:
        text += "\n\nКарточка задачи: " + _dashboard_link(config, str(request.get("id", "")))
    return text


def _save_reply_prompt(config: dict[str, Any], *, message_id: str,
                       token_data: dict[str, Any]) -> None:
    root = _state_root(config)
    path = root / "question-replies.json"
    with file_lock(root / ".question-replies.lock"):
        items = _load_json(path, {})
        if not isinstance(items, dict):
            items = {}
        now = int(time.time())
        items = {key: value for key, value in items.items()
                 if isinstance(value, dict) and int(value.get("expires", 0)) > now}
        storage_key = f"{token_data.get('chat_id', '')}:{message_id}"
        items[storage_key] = {key: token_data.get(key) for key in (
            "feature_id", "action", "gate_id", "digest", "question_id", "option_id",
            "user_id", "chat_id")}
        items[storage_key]["expires"] = now + TOKEN_TTL
        _atomic_json(path, items)


def _get_reply_prompt(config: dict[str, Any], message_id: str, chat_id: str = "") -> dict[str, Any] | None:
    root = _state_root(config)
    with file_lock(root / ".question-replies.lock"):
        items = _load_json(root / "question-replies.json", {})
        item = items.get(f"{chat_id}:{message_id}") if isinstance(items, dict) else None
        if not isinstance(item, dict) or int(item.get("expires", 0)) <= int(time.time()):
            return None
        return dict(item)


def _button_markup(buttons: list[list[tuple[str, str]]]) -> Any:
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data=data)
                                  for text, data in row] for row in buttons])


def _dashboard_link(config: dict[str, Any], feature_id: str) -> str:
    from urllib.parse import urlencode
    dashboard = str(_telegram_config(config).get("dashboard_url") or
                    "http://192.168.1.20:9119/engineering-team").rstrip("/?")
    return dashboard + "?" + urlencode({"request": feature_id})


def _status_text(request: dict[str, Any], config: dict[str, Any] | None = None) -> str:
    tasks = [task for task in request.get("tasks", []) if isinstance(task, dict)]
    # Cards may be returned in creation order, where an old blocked task can
    # precede the worker currently making progress. Prefer actual execution.
    current = next((task for task in tasks if task.get("status") == "waiting_tests"
                    or isinstance(task.get("test_wait"), dict)), None)
    if current is None:
        current = next((task for task in tasks if task.get("status") == "running"), None)
    if current is None:
        current = next((task for task in tasks if task.get("status") == "ready"), None)
    if current is None:
        current = next((task for task in tasks if task.get("status") == "blocked"), None)
    role_names = {"team-analyst": "аналитик", "team-planner": "планировщик",
                  "team-skeptic": "скептик", "team-judge": "эксперт по проверке",
                  "team-reviewer": "ревьюер", "team-qa": "тестировщик",
                  "team-integrator": "интегратор", "team-coordinator": "координатор",
                  "team-backend": "бэкенд-разработчик", "team-frontend": "фронтенд-разработчик",
                  "team-mobile": "мобильный разработчик", "team-go": "Go-разработчик",
                  "backend-developer": "бэкенд-разработчик",
                  "frontend-developer": "фронтенд-разработчик",
                  "mobile-developer": "мобильный разработчик", "go-developer": "Go-разработчик"}
    status_names = {"running": "работает", "ready": "в очереди", "blocked": "приостановлен",
                    "waiting_tests": "ожидает проверку"}
    role = role_names.get(str(current.get("role")), _safe_label(current.get("role"))) if current else "—"
    worker_state = status_names.get(str(current.get("status")), "ждёт следующего этапа") if current else "ждёт следующего этапа"
    blocker = request.get("blocker")
    stage_names = {"awaiting_requirements": "Согласование требований",
                   "awaiting_plan": "Согласование плана", "awaiting_owner_input": "Нужен ваш ответ",
                   "analysis": "Анализ", "planning": "Подготовка плана",
                   "implementing": "Разработка", "testing": "Проверка",
                   "review": "Проверка изменений", "done": "Завершена", "blocked": "Приостановлена"}
    stage_raw = request.get("stage_label") or request.get("stage")
    stage_label = stage_names.get(str(request.get("stage")), _safe_label(stage_raw, 100))
    result = (f"📌 {_safe_label(request.get('title'), 160) or 'Задача'}\n"
              f"Статус: {stage_label or 'в работе'}")
    if current:
        if current.get("status") == "waiting_tests" or isinstance(current.get("test_wait"), dict):
            result += f"\nПроверку ожидает: {role}. Состояние: {worker_state}."
            result += ("\nПроверка выполняется отдельным заданием. Бюджет агента и слот свободны. "
                       "Действие не требуется; команда продолжит по результату проверки.")
        else:
            result += f"\nСейчас работает: {role}. Состояние: {worker_state}."
        if current.get("model"):
            result += f" Модель: {_assignment_text(current)}."
    if blocker:
        result += f"\nНужно внимание: {_safe_label(blocker, 200)}"
    approval = request.get("approval")
    questions = _questions(approval)
    if questions:
        result += "\nНужен ваш ответ на вопрос ниже."
        result += "\n" + _question_text(questions[0])
        if len(questions) > 1:
            result += f"\nЕщё вопросов: {len(questions) - 1}."
    elif _has_structured_questions(approval):
        result += "\nОтветы на вопросы сохранены. Команда продолжит работу."
    elif approval:
        result += "\nЖдём вашего решения по требованиям или плану."
    if config is not None:
        result += "\nКарточка задачи: " + _dashboard_link(config, str(request.get("id", "")))
    return result


def _safe_label(value: Any, limit: int = 100) -> str:
    """Render only bounded scalar assignment metadata from the public workflow view."""
    if not isinstance(value, (str, int, float)):
        return ""
    return " ".join(str(value).split())[:limit]


def _model_description(item: dict[str, Any], *, forecast: bool = False) -> str:
    parts = []
    provider = _safe_label(item.get("provider"))
    model = _safe_label(item.get("model"), 80)
    effort = _safe_label(item.get("reasoning_effort"), 32)
    if provider:
        parts.append(provider)
    if model:
        parts.append(model)
    if effort:
        parts.append(f"effort {effort}")
    if not parts:
        return "модель пока не определена"
    return ("прогноз: " if forecast else "") + " · ".join(parts)


def _assignment_state_label(item: dict[str, Any]) -> str:
    state = item.get("assignment_state")
    if state == "selected":
        return "назначено"
    if state == "pending":
        return "ожидает назначения при запуске"
    if state == "unknown":
        return "назначение неизвестно"
    # Older workflow records do not have assignment metadata. Avoid implying
    # that their configured model is the model actually used.
    return "сведения о назначении отсутствуют"


def _assignment_text(item: dict[str, Any]) -> str:
    state = item.get("assignment_state")
    label = _assignment_state_label(item)
    if state == "selected":
        return f"{label} · {_model_description(item)}"
    if state == "pending" and any(item.get(key) for key in ("provider", "model", "reasoning_effort")):
        return f"{label} · {_model_description(item, forecast=True)}"
    return label


def _role_line(item: dict[str, Any]) -> str:
    role = _safe_label(item.get("role")) or "роль"
    title = _safe_label(item.get("title"), 100)
    status = _safe_label(item.get("status")) or "—"
    task_label = f"{role}: {status}" + (f" — {title}" if title else "")
    return task_label + f"\n    {_assignment_text(item)}"


def _planned_role_line(item: dict[str, Any]) -> str:
    role = _safe_label(item.get("role")) or "роль"
    title = _safe_label(item.get("title"), 100)
    purpose = _safe_label(item.get("purpose"), 120)
    reason = _safe_label(item.get("selection_reason"), 120)
    detail = f" — {title}" if title else (f" — {purpose}" if purpose else "")
    line = f"{role}{detail}: {_model_description(item, forecast=True)}"
    if reason:
        line += f"; выбор: {reason}"
    return line


def _status_keyboard(config: dict[str, Any], request: dict[str, Any], *, user_id: str,
                     chat_id: str) -> Any | None:
    rows: list[list[tuple[str, str]]] = []
    approval = request.get("approval")
    questions = _questions(approval)
    if questions:
        return _question_keyboard(config, request, questions[0], user_id=user_id, chat_id=chat_id)
    if _has_structured_questions(approval):
        status_token = _save_action_token(config, feature_id=request["id"], action="show",
                                          user_id=user_id, chat_id=chat_id)
        return _button_markup([[('Открыть карточку', f"{CALLBACK_PREFIX}s:{status_token}")]])
    status_token = _save_action_token(config, feature_id=request["id"], action="show",
                                      user_id=user_id, chat_id=chat_id)
    rows.append([("Открыть карточку", f"{CALLBACK_PREFIX}s:{status_token}")])
    prerequisite_items = _prerequisite_items(request)
    prerequisite_digest = _prerequisite_digest(request)
    if (prerequisite_digest and any((item.get("catalog") or {}).get("command")
                                    for item in prerequisite_items)):
        token = _save_action_token(config, feature_id=request["id"],
            action="retry_prerequisites", digest=prerequisite_digest,
            user_id=user_id, chat_id=chat_id)
        rows.append([("Установил — проверить снова", f"{CALLBACK_PREFIX}r:{token}")])
    if approval:
        approve_token = _save_action_token(config, feature_id=request["id"], action="approve",
                                           gate_id=approval["id"], digest=_gate_digest(approval),
                                           user_id=user_id,
                                           chat_id=chat_id)
        rows.append([("Утвердить пакет", f"{CALLBACK_PREFIX}a:{approve_token}")])
    return _button_markup(rows) if rows else None


def _add_watch(config: dict[str, Any], feature_id: str, chat_id: str) -> None:
    root = _state_root(config)
    path = root / "watches.json"
    with file_lock(root / ".watches.lock"):
        watches = _load_json(path, {})
        if not isinstance(watches, dict):
            watches = {}
        watchers = set(map(str, watches.get(feature_id, [])))
        watchers.add(chat_id)
        watches[feature_id] = sorted(watchers)
        _atomic_json(path, watches)


def _split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    parts = []
    remaining = str(text)
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining or not parts:
        parts.append(remaining)
    return parts


def _format_request(request: dict[str, Any], document_name: str | None = None,
                    config: dict[str, Any] | None = None) -> str:
    if document_name:
        approval = request.get("approval") or {}
        available = {doc.get("name"): doc.get("content", "") for doc in approval.get("documents", [])
                     if isinstance(doc, dict)}
        for name, content in (request.get("artifacts") or {}).items():
            available.setdefault(name, content)
        if document_name not in available:
            return f"Документ «{document_name}» не найден. Откройте карточку задачи в панели."
        content = str(available[document_name])
        if len(content) > 10000:
            content = content[:10000] + "\n… Документ обрезан. Полная версия доступна в панели."
        return f"📄 {document_name}\n\n{content}"
    lines = [_status_text(request, config)]
    prerequisite_notice = format_prerequisite_notice(request, config)
    if prerequisite_notice:
        lines.extend(["", prerequisite_notice])
    lines.extend(["", "Кратко о задаче:",
             _safe_label(request.get("brief", ""), 1200) or "Описание не заполнено."]
    )
    source_files = request.get("attachments") or []
    if source_files:
        lines.extend(["", "Исходные файлы:"])
        for item in source_files:
            if isinstance(item, dict):
                lines.append(f"• {item.get('name', 'Документ')} · {int(item.get('size', 0)) / 1024:.0f} КБ")
    approval = request.get("approval")
    if approval:
        lines.extend(["", "На согласовании:", _safe_label(approval.get("summary", ""), 1400)])
        questions = _questions(approval)
        for index, question in enumerate(questions, 1):
            lines.extend(["", _question_text(question, index)])
        if not questions:
            lines.append("Полные требования и план можно посмотреть в карточке задачи в панели.")
    files = [item.get("name") for item in request.get("attachments", []) if isinstance(item, dict)]
    if files:
        lines.extend(["", "Приложенные файлы: " + ", ".join(_safe_label(name, 100) for name in files[:12])])
    rendered = "\n".join(lines)
    if len(rendered) > 5000:
        rendered = rendered[:5000] + "\n\nПолная информация доступна в карточке задачи в панели."
    return rendered


def _help() -> str:
    return ("Команды команды разработки:\n"
            "/team new <проект> <название> — затем новая строка и полное описание задачи; приложит ожидающие документы\n"
            "Отправьте один документ с подписью /team upload, чтобы добавить его к ожидающим файлам\n"
            "Можно отправить документ с подписью /team new <проект> <название> и описанием со следующей строки\n"
            "/team status [ID] — список задач или состояние одной задачи\n"
            "/team show <ID> [имя файла] — требования, пакет и обсуждение; файл можно запросить отдельно\n"
            "/team files — показать документы, ожидающие привязки к задаче\n"
            "/team clearfiles — убрать все ожидающие документы\n"
            "/team attach <ID> — приложить ожидающие документы к задаче до согласования требований\n"
            "/team comment <ID> <текст> — добавить уточнение или комментарий\n"
            "/team answer <ID> <ID вопроса> <ответ> — ответить на вопрос команды; также можно выбрать кнопку в сообщении\n"
            "/team approve <ID> <gate-id> — утвердить показанный пакет\n"
            "/team revise <ID> <gate-id> <что изменить> — вернуть пакет на доработку\n"
            "/team watch <ID> — подписаться на уведомления в личном чате\n"
            "Одного «ок» недостаточно: решение должно содержать ID пакета.")


def _origin(update: Any) -> dict[str, Any]:
    message = update.effective_message
    return {"telegram_chat_id": str(update.effective_chat.id),
            "telegram_user_id": str(update.effective_user.id),
            "telegram_message_id": str(message.message_id),
            "telegram_thread_id": None}


def _notify_chat(config: dict[str, Any], fallback_chat_id: str = "") -> str:
    telegram = _telegram_config(config)
    chat_id = str(telegram.get("notification_chat_id") or fallback_chat_id).strip()
    # Private Telegram owner-chat IDs are positive and match an allowlisted
    # user. This prevents an accidental group/channel notification target.
    return chat_id if chat_id.isdigit() and chat_id in _owner_ids(config) else ""


async def _reply(update: Any, text: str, *, reply_markup: Any = None) -> None:
    message = update.effective_message
    for index, chunk in enumerate(_split_message(text)):
        await message.reply_text(chunk, reply_markup=reply_markup if index == 0 else None)


def _error_text(exc: Exception) -> str:
    try:
        from .attachments import AttachmentError
        if isinstance(exc, AttachmentError):
            return str(exc)[:500]
    except ImportError:
        pass
    if isinstance(exc, human_workflow.WorkflowNotFound):
        return "Не нашёл такую задачу. Проверьте ID через /team status."
    if isinstance(exc, human_workflow.WorkflowConflict):
        return "Пакет изменился или уже обработан. Обновите состояние командой /team show <ID>."
    if isinstance(exc, human_workflow.WorkflowError):
        return str(exc)[:500]
    if isinstance(exc, ContractError):
        return str(exc)[:500]
    return "Не удалось выполнить команду. Проверьте проект, ID и формат; подробности записаны в журнал Hermes."


async def _handle_question_reply(update: Any, context: Any, adapter: Any) -> bool:
    """Consume only a reply to a bot prompt explicitly created for a question."""
    message = getattr(update, "effective_message", None)
    reply = getattr(message, "reply_to_message", None)
    if reply is None or not await asyncio.to_thread(_authorized, update, adapter):
        return False
    config_path = _config_path()
    config = await asyncio.to_thread(_config, config_path)
    prompt = await asyncio.to_thread(_get_reply_prompt, config,
        str(getattr(reply, "message_id", "")), str(update.effective_chat.id))
    if (not prompt or prompt.get("user_id") != str(update.effective_user.id)
            or prompt.get("chat_id") != str(update.effective_chat.id)):
        return False
    answer = str(getattr(message, "text", "") or "").strip()
    if not answer:
        await _reply(update, "Напишите ответ обычным текстом в ответ на сообщение-вопрос.")
        return True
    request_id = f"telegram:answer-reply:{update.effective_chat.id}:{message.message_id}"
    try:
        result = await asyncio.to_thread(human_workflow.answer_question, config_path,
            prompt["feature_id"], gate_id=prompt["gate_id"],
            question_id=prompt["question_id"], text=answer,
            actor=f"telegram:{update.effective_user.id}", channel="telegram",
            request_id=request_id)
        await _reply(update, "Ответ записан.\n" + _status_text(result, config),
            reply_markup=await asyncio.to_thread(_status_keyboard, config, result,
                user_id=str(update.effective_user.id), chat_id=str(update.effective_chat.id)))
    except Exception as exc:
        log.exception("Engineering-team Telegram question reply failed")
        await _reply(update, "Не удалось записать ответ: " + _error_text(exc))
    return True


async def _handle_command(update: Any, context: Any, adapter: Any, command_text: str | None = None) -> None:
    message = update.effective_message
    text = str(command_text if command_text is not None else (getattr(message, "text", "") or ""))
    if not await asyncio.to_thread(_authorized, update, adapter):
        # Consume the scoped command so it is never forwarded to the LLM.
        if _is_private(update):
            await _reply(update, "Команды /team доступны владельцу в личном чате Hermes.")
        return
    args = text.split(maxsplit=1)
    rest = args[1].strip() if len(args) > 1 else ""
    try:
        config_path = _config_path()
        config = await asyncio.to_thread(_config, config_path)
        actor = f"telegram:{update.effective_user.id}"
        chat_id = str(update.effective_chat.id)
        user_id = str(update.effective_user.id)
        request_id = f"telegram:{chat_id}:{message.message_id}:" + hashlib.sha256(text.encode()).hexdigest()[:16]

        if not rest or rest.lower() in {"help", "?"}:
            await _reply(update, _help())
            return
        subcommand, _, payload = rest.partition(" ")
        subcommand = subcommand.lower()
        if subcommand == "new":
            header, separator, brief = payload.partition("\n")
            project, _, title = header.strip().partition(" ")
            title = title.strip()
            brief = brief.strip()
            if not separator or not project or not title or not brief:
                await _reply(update, "Формат: /team new <проект> <название>, затем с новой строки — описание задачи.\n\n" + _help())
                return
            result = await asyncio.to_thread(_submit_with_staged, config_path,
                chat_id=chat_id, user_id=user_id,
                project=project, title=title, brief=brief, request_id=request_id,
                actor=actor, channel="telegram", origin=_origin(update))
            await _reply(update, "Задача принята.\n" + _status_text(result, config),
                         reply_markup=await asyncio.to_thread(_status_keyboard, config, result,
                             user_id=user_id, chat_id=chat_id))
            return
        if subcommand == "upload":
            if getattr(message, "document", None) is None:
                await _reply(update, "Отправьте документ отдельным сообщением с подписью /team upload. Пока файл не загружен.")
            else:
                await _reply(update, "Документ добавлен.\n" + _attachment_names(config, chat_id, user_id))
            return
        if subcommand == "files":
            await _reply(update, _attachment_names(config, chat_id, user_id))
            return
        if subcommand == "clearfiles":
            count = _clear_staged(config, chat_id, user_id)
            await _reply(update, f"Ожидающие документы убраны из списка: {count}. Сами файлы в хранилище не удалены.")
            return
        if subcommand == "attach":
            feature_id = payload.strip()
            if not feature_id:
                await _reply(update, "Формат: /team attach <ID>")
                return
            staged = _staged(config, chat_id, user_id)
            attachment_ids = [str(item["id"]) for item in staged if item.get("id")]
            if not attachment_ids:
                await _reply(update, "Нет ожидающих документов. Сначала отправьте файл с подписью /team upload.")
                return
            result = await asyncio.to_thread(_submit_with_staged, config_path,
                chat_id=chat_id, user_id=user_id, attach_to=feature_id,
                request_id=request_id, actor=actor, channel="telegram")
            await _reply(update, "Документы добавлены к задаче.\n" + _status_text(result, config))
            return
        if subcommand == "status":
            if payload.strip():
                results = [await asyncio.to_thread(human_workflow.get_request, config_path, payload.strip())]
            else:
                results = (await asyncio.to_thread(human_workflow.list_requests, config_path))[-10:]
            if not results:
                await _reply(update, "Задач пока нет. Создайте первую командой /team new <проект> <название>.")
                return
            for result in results:
                await _reply(update, _status_text(result, config),
                    reply_markup=await asyncio.to_thread(_status_keyboard, config, result,
                        user_id=user_id, chat_id=chat_id))
            return
        if subcommand == "show":
            feature_id, _, document_name = payload.strip().partition(" ")
            if not feature_id:
                await _reply(update, "Формат: /team show <ID> [имя файла]")
                return
            result = await asyncio.to_thread(human_workflow.get_request, config_path, feature_id)
            await _reply(update, _format_request(result, document_name.strip() or None, config),
                         reply_markup=await asyncio.to_thread(_status_keyboard, config, result,
                             user_id=user_id, chat_id=chat_id))
            return
        if subcommand == "comment":
            feature_id, separator, comment = payload.strip().partition(" ")
            if not separator or not comment.strip():
                await _reply(update, "Формат: /team comment <ID> <текст>")
                return
            result = await asyncio.to_thread(human_workflow.add_comment, config_path, feature_id,
                comment=comment.strip(), actor=actor, channel="telegram", request_id=request_id)
            await _reply(update, "Комментарий добавлен.\n" + _status_text(result, config))
            return
        if subcommand == "answer":
            feature_id, separator, tail = payload.strip().partition(" ")
            question_id, separator2, answer = tail.partition(" ")
            if not separator or not separator2 or not question_id or not answer.strip():
                await _reply(update, "Формат: /team answer <ID задачи> <ID вопроса> <ваш ответ>")
                return
            request = await asyncio.to_thread(human_workflow.get_request, config_path, feature_id)
            approval = request.get("approval") or {}
            result = await asyncio.to_thread(human_workflow.answer_question, config_path,
                feature_id, gate_id=str(approval.get("id", "")), question_id=question_id,
                text=answer.strip(), actor=actor, channel="telegram", request_id=request_id)
            await _reply(update, "Ответ записан.\n" + _status_text(result, config),
                reply_markup=await asyncio.to_thread(_status_keyboard, config, result,
                    user_id=user_id, chat_id=chat_id))
            return
        if subcommand in {"approve", "revise"}:
            feature_id, separator, tail = payload.strip().partition(" ")
            gate_id, _, comment = tail.partition(" ")
            if not separator or not gate_id or (subcommand == "revise" and not comment.strip()):
                await _reply(update, f"Формат: /team {subcommand} <ID> <gate-id>" +
                    (" <что изменить>" if subcommand == "revise" else ""))
                return
            result = await asyncio.to_thread(human_workflow.decide_request, config_path, feature_id,
                gate_id=gate_id, decision="approve" if subcommand == "approve" else "revise",
                comment=comment.strip(), actor=actor, channel="telegram", request_id=request_id)
            await _reply(update, ("Пакет утверждён." if subcommand == "approve" else "Запрос на доработку отправлен.") +
                         "\n" + _status_text(result, config))
            return
        if subcommand == "watch":
            feature_id = payload.strip()
            if not feature_id:
                await _reply(update, "Формат: /team watch <ID>")
                return
            await asyncio.to_thread(human_workflow.get_request, config_path, feature_id)
            await asyncio.to_thread(_add_watch, config, feature_id, chat_id)
            await _reply(update, f"Подписка на уведомления по {feature_id} включена в этот личный чат.")
            return
        await _reply(update, _help())
    except Exception as exc:
        log.exception("Engineering-team Telegram command failed")
        await _reply(update, "Ошибка команды: " + _error_text(exc))


async def _handle_document(update: Any, context: Any, adapter: Any) -> None:
    """Authorize first, then download a bounded Telegram document and route its caption."""
    message = update.effective_message
    caption = str(getattr(message, "caption", "") or "")
    document = getattr(message, "document", None)
    if document is None or not _TEAM_COMMAND.match(caption):
        return
    if not await asyncio.to_thread(_authorized, update, adapter):
        if _is_private(update):
            await _reply(update, "Загружать документы для команды может только владелец в личном чате Hermes.")
        return
    try:
        config_path = _config_path()
        config = await asyncio.to_thread(_config, config_path)
        # Validate the caption before requesting any bytes from Telegram. A
        # file captioned with an approval command must never turn into an
        # approval while silently discarding the file.
        command_rest = _TEAM_COMMAND.sub("", caption, count=1).strip()
        subcommand, _, payload = command_rest.partition(" ")
        subcommand = subcommand.lower()
        valid_caption = (subcommand == "upload" and not payload.strip())
        if subcommand == "new":
            header, separator, brief = payload.partition("\n")
            project, _, title = header.strip().partition(" ")
            valid_caption = bool(separator and project and title.strip() and brief.strip())
        elif subcommand == "attach":
            valid_caption = bool(payload.strip()) and len(payload.split()) == 1
        if not valid_caption:
            await _reply(update, "Для документа используйте подпись `/team upload`, `/team new <проект> <название>` с описанием со следующей строки или `/team attach <ID>`. Другие команды не принимают файл.")
            return
        if getattr(message, "media_group_id", None) is not None:
            await _reply(update, "Альбомы Telegram не поддерживаются. Отправьте каждый документ отдельным сообщением с подписью /team upload, а затем создайте задачу.")
            return
        user_id = str(update.effective_user.id)
        chat_id = str(update.effective_chat.id)
        actor = f"telegram:{user_id}"
        request_id = _upload_event_id(update, caption)
        # Redelivery after a successful submit/attach must replay the existing
        # idempotent intent, not put its already-consumed file back in staging.
        if await asyncio.to_thread(_read_intent, config, chat_id, user_id, request_id):
            await _handle_command(update, context, adapter, command_text=caption)
            return
        advertised_size = getattr(document, "file_size", None)
        if advertised_size is not None and int(advertised_size) > MAX_UPLOAD_BYTES:
            await _reply(update, "Файл больше 5 МиБ. Разбейте материалы на несколько документов.")
            return
        file_id = str(getattr(document, "file_id", "") or "")
        filename = str(getattr(document, "file_name", "") or "document")
        if not file_id:
            await _reply(update, "Telegram не передал идентификатор файла. Отправьте документ ещё раз.")
            return
        telegram_file = await context.bot.get_file(file_id)
        raw = await telegram_file.download_as_bytearray()
        content = bytes(raw)
        if len(content) > MAX_UPLOAD_BYTES:
            await _reply(update, "Файл больше 5 МиБ. Разбейте материалы на несколько документов.")
            return
        if not content:
            await _reply(update, "Файл пустой; пустые документы не добавлены.")
            return
        stable_file_identity = str(getattr(document, "file_unique_id", "") or file_id)
        upload_key = f"{request_id}:{stable_file_identity}"
        from . import attachments
        metadata = await asyncio.to_thread(attachments.upload_attachment, config_path,
            name=filename, content=content, request_id=upload_key, actor=actor, channel="telegram")
        if not isinstance(metadata, dict) or not metadata.get("id"):
            raise human_workflow.WorkflowError("Хранилище не вернуло идентификатор документа.")
        item = {**metadata, "size": int(metadata.get("size", len(content))),
                "mime_type": getattr(document, "mime_type", None)}
        await asyncio.to_thread(_stage_attachment, config, chat_id, user_id, item)
        # Reuse the regular command path so `/team new` and `/team attach` keep
        # the exact same idempotency and workflow validation rules as text.
        await _handle_command(update, context, adapter, command_text=caption)
    except Exception as exc:
        log.exception("Engineering-team Telegram document intake failed")
        await _reply(update, "Не удалось загрузить документ: " + _error_text(exc))


async def _handle_callback(update: Any, context: Any, adapter: Any) -> None:
    query = update.callback_query
    data = str(getattr(query, "data", "") or "")
    try:
        await query.answer()
        if not await asyncio.to_thread(_authorized, update, adapter, callback=True):
            await query.edit_message_text("Кнопка доступна владельцу в личном чате Hermes.")
            return
        match = re.fullmatch(r"team:([saqr]):([A-Za-z0-9_-]{6,24})", data)
        if not match:
            await query.edit_message_text("Некорректная кнопка. Используйте /team status.")
            return
        config_path = _config_path()
        config = await asyncio.to_thread(_config, config_path)
        item = await asyncio.to_thread(_get_action_token, config, match.group(2))
        if not item or item.get("user_id") != str(update.effective_user.id) or item.get("chat_id") != str(update.effective_chat.id):
            await query.edit_message_text("Кнопка устарела или принадлежит другому чату. Используйте /team status.")
            return
        result = await asyncio.to_thread(human_workflow.get_request, config_path, item["feature_id"])
        if match.group(1) == "r" and item.get("action") == "retry_prerequisites":
            current_digest = _prerequisite_digest(result)
            if (result.get("stage") != "blocked"
                    or not current_digest
                    or current_digest != item.get("digest")
                    or not _has_actionable_prerequisites(result)):
                await query.edit_message_text(
                    "Это уведомление устарело: причина уже изменилась. Откройте актуальную карточку через /team show "
                    + item["feature_id"])
                return
            # The workflow method rechecks digest/stage while holding the
            # feature lock. Telegram never executes the displayed command.
            result = await asyncio.to_thread(human_workflow.retry_prerequisites,
                config_path, item["feature_id"], digest=item["digest"],
                actor=f"telegram:{update.effective_user.id}", channel="telegram")
            await query.edit_message_text(
                "Запрос на повторную проверку сохранён. Hermes проверит среду, когда освободится слот подписки.\n"
                + _status_text(result, await asyncio.to_thread(_config, config_path)))
            return
        if match.group(1) == "s" and item.get("action") == "show":
            keyboard = await asyncio.to_thread(_status_keyboard, config, result,
                user_id=str(update.effective_user.id), chat_id=str(update.effective_chat.id))
            await query.edit_message_text(_format_request(result, config=config)[:MAX_MESSAGE],
                                          reply_markup=keyboard)
            return
        approval = result.get("approval")
        if match.group(1) == "q" and item.get("action") in {"answer_option", "answer_custom"}:
            if (not approval or approval.get("id") != item.get("gate_id")
                    or _gate_digest(approval) != item.get("digest")
                    or not any(isinstance(q, dict) and str(q.get("id")) == item.get("question_id")
                               for q in (approval.get("questions") or _questions(approval)))):
                await query.edit_message_text("Этот вопрос уже обновился. Откройте актуальную карточку задачи в панели.")
                return
            current_question = next(q for q in (approval.get("questions") or _questions(approval))
                                    if isinstance(q, dict) and str(q.get("id")) == item.get("question_id"))
            if current_question.get("answer"):
                keyboard = await asyncio.to_thread(_status_keyboard, config, result,
                    user_id=str(update.effective_user.id), chat_id=str(update.effective_chat.id))
                await query.edit_message_text("Ответ на этот вопрос уже сохранён.\n" + _status_text(result, config),
                                              reply_markup=keyboard)
                return
            if item.get("action") == "answer_custom":
                prompt_text = _question_text(current_question) + "\n\nНапишите свой вариант ответным сообщением."
                try:
                    from telegram import ForceReply
                    reply_markup = ForceReply(selective=True, input_field_placeholder="Ваш ответ")
                except ImportError:
                    reply_markup = None
                sent = await query.message.reply_text(prompt_text, reply_markup=reply_markup)
                await asyncio.to_thread(_save_reply_prompt, config,
                    message_id=str(getattr(sent, "message_id", "")), token_data=item)
                await query.edit_message_text("Жду ваш ответ отдельным сообщением в ответ на мой вопрос.")
                return
            request_id = f"telegram:option:{query.id}:{item['feature_id']}:{item['question_id']}"
            result = await asyncio.to_thread(human_workflow.answer_question, config_path,
                item["feature_id"], gate_id=item["gate_id"], question_id=item["question_id"],
                option_id=item["option_id"], actor=f"telegram:{update.effective_user.id}",
                channel="telegram", request_id=request_id)
            keyboard = await asyncio.to_thread(_status_keyboard, config, result,
                user_id=str(update.effective_user.id), chat_id=str(update.effective_chat.id))
            await query.edit_message_text("Выбор записан.\n" + _status_text(result, config),
                                          reply_markup=keyboard)
            return
        if (match.group(1) != "a" or item.get("action") != "approve" or not approval
                or approval.get("id") != item.get("gate_id")
                or _gate_digest(approval) != item.get("digest")):
            await query.edit_message_text("Пакет устарел. Просмотрите обновлённую версию через /team show " + item["feature_id"])
            return
        request_id = f"telegram:callback:{query.id}:{item['feature_id']}"
        result = await asyncio.to_thread(human_workflow.decide_request, config_path, item["feature_id"],
            gate_id=item["gate_id"], decision="approve", actor=f"telegram:{update.effective_user.id}",
            channel="telegram", request_id=request_id)
        await query.edit_message_text("Пакет утверждён.\n" + _status_text(result, config))
    except Exception as exc:
        log.exception("Engineering-team Telegram callback failed")
        try:
            await query.edit_message_text("Не удалось применить действие. Обновите статус через /team show <ID>.\n" + _error_text(exc))
        except Exception:
            pass


async def _poll_notifications(application: Any, context: Any = None) -> None:
    """Notify the configured owner chat about request status transitions."""
    config_path = _config_path()
    try:
        config, pending = await asyncio.to_thread(_collect_notifications, config_path)
        if config is None:
            return
        for feature_id, signature, request, watches in pending:
            targets = set()
            configured_chat = _notify_chat(config)
            if configured_chat:
                targets.add(configured_chat)
            targets.update(str(value) for value in watches
                           if str(value).isdigit() and str(value) in _owner_ids(config))
            if not targets:
                await asyncio.to_thread(_mark_notification, config, feature_id, signature, True)
                continue
            try:
                approval = request.get("approval") or {}
                if _questions(approval):
                    prefix = "Нужен ваш ответ на вопрос аналитика:\n"
                elif approval:
                    prefix = "Требуется ваше решение по задаче:\n"
                else:
                    prefix = "Изменился статус задачи:\n"
                prerequisite_notice = format_prerequisite_notice(request, config)
                if prerequisite_notice:
                    # Keep install notices focused; do not attach the history
                    # of every role/model assignment to this actionable alert.
                    text = prerequisite_notice
                else:
                    text = prefix + _status_text(request, config)
                for target_chat in sorted(targets):
                    target_user = str(_telegram_config(config).get("owner_user_id") or target_chat)
                    keyboard = await asyncio.to_thread(_status_keyboard, config, request,
                        user_id=target_user, chat_id=target_chat)
                    chunks = _split_message(text)
                    for index, chunk in enumerate(chunks):
                        await application.bot.send_message(chat_id=target_chat,
                            text=chunk,
                            reply_markup=keyboard if index == len(chunks) - 1 else None)
                await asyncio.to_thread(_mark_notification, config, feature_id, signature, True)
            except Exception:
                log.exception("Telegram workflow notification failed for %s", feature_id)
                await asyncio.to_thread(_mark_notification, config, feature_id, signature, False)
    except Exception:
        log.exception("Engineering-team Telegram notification poll failed")


def _collect_notifications(config_path: Path):
    """Blocking workflow reads and durable cursor selection, run in a worker thread."""
    try:
        config = _config(config_path)
        telegram = _telegram_config(config)
        if telegram.get("enabled") is False:
            return None, []
        requests = human_workflow.list_requests(config_path)
        root = _state_root(config)
        watches = _load_json(root / "watches.json", {})
        if not isinstance(watches, dict):
            watches = {}
        cursor_path = root / "notification-cursor.json"
        lock_path = root / ".notification-cursor.lock"
        pending = []
        with file_lock(lock_path):
            cursor = _load_json(cursor_path, {})
            if not isinstance(cursor, dict):
                cursor = {}
            for request in requests:
                feature_id = request.get("id")
                if not isinstance(feature_id, str):
                    continue
                approval = request.get("approval") or {}
                signature = hashlib.sha256(json.dumps({
                    "stage": request.get("stage"),
                    "prerequisites": _prerequisite_items(request),
                    "prerequisite_digest": _prerequisite_digest(request),
                    "approval": approval.get("id"),
                    "approval_digest": _gate_digest(approval) if approval else None,
                    "question_progress": hashlib.sha256(json.dumps([
                        {"id": question.get("id"), "answer": question.get("answer")}
                        for question in (approval.get("questions", []) if isinstance(approval.get("questions", []), list) else [])
                        if isinstance(question, dict)], sort_keys=True, ensure_ascii=False,
                        separators=(",", ":")).encode("utf-8")).hexdigest() if approval else None,
                    "blocker": request.get("blocker"),
                    "tasks": [{key: task.get(key) for key in (
                        "id", "role", "title", "status", "model", "provider",
                        "reasoning_effort", "quota_pool", "assignment_state",
                        "assignment_source", "selection_reason", "plan_task_id")}
                              for task in request.get("tasks", []) if isinstance(task, dict)],
                    "role_plan": [{key: role.get(key) for key in (
                        "role", "title", "purpose", "selection_reason", "selection_source",
                        "model", "provider", "reasoning_effort", "quota_pool",
                        "assignment_state", "plan_task_id")}
                                  for role in request.get("role_plan", []) if isinstance(role, dict)],
                }, sort_keys=True).encode()).hexdigest()
                old = cursor.get(feature_id)
                if isinstance(old, str):  # one-time migration from the first cursor format
                    old = {"signature": old, "failures": 0, "retry_after": 0}
                old_signature = old.get("signature") if isinstance(old, dict) else None
                if (old_signature is None and not approval
                        and not _has_actionable_prerequisites(request)):
                    cursor[feature_id] = {"signature": signature, "failures": 0, "retry_after": 0}
                    continue
                retry_after = float(old.get("retry_after", 0)) if isinstance(old, dict) else 0
                same_signature = old_signature == signature
                if same_signature:
                    failures = int(old.get("failures", 0)) if isinstance(old, dict) else 0
                    if failures == 0 or retry_after > time.time():
                        continue
                pending.append((feature_id, signature, request,
                                watches.get(feature_id, []) if isinstance(watches.get(feature_id, []), list) else []))
            _atomic_json(cursor_path, cursor)
        return config, pending
    except Exception:
        log.exception("Could not prepare engineering-team Telegram notifications")
        return None, []


def _mark_notification(config: dict[str, Any], feature_id: str, signature: str,
                       success: bool) -> None:
    root = _state_root(config)
    cursor_path, lock_path = root / "notification-cursor.json", root / ".notification-cursor.lock"
    with file_lock(lock_path):
        cursor = _load_json(cursor_path, {})
        if not isinstance(cursor, dict):
            cursor = {}
        old = cursor.get(feature_id)
        if isinstance(old, str):
            old = {"signature": old, "failures": 0, "retry_after": 0}
        old = old if isinstance(old, dict) else {}
        if success:
            cursor[feature_id] = {"signature": signature, "failures": 0, "retry_after": 0}
        else:
            failures = int(old.get("failures", 0)) + 1 if old.get("signature") == signature else 1
            delay = min(POLL_SECONDS * (2 ** min(failures - 1, 5)), 300)
            cursor[feature_id] = {"signature": signature, "failures": failures,
                                  "retry_after": time.time() + delay}
        _atomic_json(cursor_path, cursor)


def _install_handlers(application: Any, adapter: Any) -> None:
    from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, MessageHandler, filters

    # PTB Application uses slots; its public bot_data survives handler setup
    # and supports an explicit per-application registration marker.
    state = application.bot_data
    registered = state.get("engineering_team_handlers_registered", False)

    async def command(update: Any, context: Any) -> None:
        message = getattr(update, "effective_message", None)
        if getattr(message, "document", None) is not None and _TEAM_COMMAND.match(
                str(getattr(message, "caption", "") or "")):
            # Regex filters may match captions as well as message text. Route a
            # document caption here too so it cannot be consumed as an empty
            # text command before the document-specific handler gets a chance.
            await _handle_document(update, context, adapter)
        else:
            await _handle_command(update, context, adapter)
        # Prevent the core Hermes text handler from treating scoped commands
        # as ordinary prompts after this local handler returns.
        raise ApplicationHandlerStop

    async def document(update: Any, context: Any) -> None:
        await _handle_document(update, context, adapter)
        raise ApplicationHandlerStop

    async def question_reply(update: Any, context: Any) -> None:
        if await _handle_question_reply(update, context, adapter):
            raise ApplicationHandlerStop

    async def callback(update: Any, context: Any) -> None:
        await _handle_callback(update, context, adapter)

    # Native plugin handlers precede the core gateway handlers. A matching
    # /team command or scoped button is always consumed locally.
    if not registered:
        application.add_handler(MessageHandler(filters.Regex(_TEAM_COMMAND), command), group=0)
        application.add_handler(MessageHandler(
            filters.Document.ALL & filters.CaptionRegex(_TEAM_COMMAND), document), group=0)
        if hasattr(filters, "REPLY") and hasattr(filters, "TEXT"):
            application.add_handler(MessageHandler(filters.REPLY & filters.TEXT,
                                                   question_reply), group=0)
        application.add_handler(CallbackQueryHandler(callback, pattern=r"^team:"), group=0)
        state["engineering_team_handlers_registered"] = True
    queue = getattr(application, "job_queue", None)
    if queue is not None:
        if not registered:
            queue.run_repeating(_poll_notifications, interval=POLL_SECONDS,
                                first=POLL_SECONDS, name="engineering-team-telegram-status")
    else:
        task = state.get("engineering_team_notification_task")
        if task is None or task.done():
            # Hermes wires handlers before Application.start(). A PTB-managed
            # infinite task can prevent stop() from completing. This bounded
            # fallback instead follows the application's actual lifetime.
            state["engineering_team_notification_task"] = asyncio.get_running_loop().create_task(
                _notification_loop(application), name="engineering-team-telegram-status")


async def _notification_loop(application: Any, *, startup_timeout: float = 60) -> None:
    deadline = time.monotonic() + startup_timeout
    while not application.running:
        if time.monotonic() >= deadline:
            log.warning("Telegram application did not start; notification loop stopped")
            return
        await asyncio.sleep(1)
    while application.running:
        await asyncio.sleep(POLL_SECONDS)
        if application.running:
            await _poll_notifications(application)


def register(ctx: Any) -> None:
    """Register as an extension of the already-installed engineering-team plugin."""
    ctx.register_telegram_handler(_install_handlers)
