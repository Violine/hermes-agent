"""Validate role-reported prerequisites and render safe owner instructions.

The role artifact carries evidence, never commands. Action text comes only from
this trusted catalog so worker output cannot turn into a Telegram shell recipe.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat


ARTIFACT_NAME = "PREREQUISITES.json"
MAX_ARTIFACT_BYTES = 16 * 1024
MAX_ITEMS = 5
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
COMMAND_TEXT = re.compile(
    r"(?:^|[\s:])(?:sudo|ssh|apt(?:-get)?|curl|wget|bash|sh|systemctl|rm|chmod|chown|mkdir|cp|mv|ln|tee|export|eval|nohup|kill|reboot|shutdown|service|echo|printf)\s+\S+|"
    r"(?:^|[\s:])docker\s+(?:run|exec|compose|build|pull|push|ps|inspect|rm|stop|start)\b|"
    r"(?:^|[\s:])git\s+(?:clone|checkout|reset|push|clean|config)\b|"
    r"(?:^|[\s:])python(?:3)?\s+(?:-[cm]|[A-Za-z0-9_./~-]+\.py)\b|"
    r"(?:^|[\s:])(?:kill|reboot|shutdown|service)\s+(?:-[A-Za-z]|[A-Za-z0-9])",
    re.IGNORECASE,
)

# Commands here are deliberately fixed and reviewed. Do not add worker-provided
# commands or host Docker/socket, VPN, firewall, or route changes to this list.
CATALOG = {
    "ubuntu_uidmap": {
        "title": "Установить uidmap на медиасервере",
        "where": "Выполни в терминале на Mac; команда подключится к медиасерверу.",
        "command": "ssh -t alex@192.168.1.20 'sudo apt-get install -y uidmap'",
    },
    "managed_jdk": {
        "title": "Восстановить управляемую Java-среду",
        "where": "Это обслуживание изолированной среды Hermes на медиасервере.",
        "command": None,
    },
    "managed_bubblewrap": {
        "title": "Восстановить управляемую изоляцию Bubblewrap",
        "where": "Это обслуживание изолированной среды Hermes на медиасервере.",
        "command": None,
    },
}


class PrerequisiteError(ValueError):
    """Malformed or unsafe role prerequisite artifact."""


def load_prerequisites(role_dir: str | Path | None, *, expected_assignee: str | None = None,
                       expected_run_id: str | None = None) -> list[dict]:
    """Load and normalize the role artifact from its unique run directory.

    A missing artifact means no actionable prerequisite was reported. Invalid
    artifacts raise ``PrerequisiteError``; callers should log/suppress them,
    never fall back to a command from untrusted role text.
    """
    if role_dir is None or str(role_dir).strip() == "":
        return []
    directory = Path(role_dir)
    try:
        directory_stat = directory.lstat()
    except OSError as exc:
        raise PrerequisiteError("Role artifact directory is unavailable") from exc
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        raise PrerequisiteError("Role artifact directory must be a plain directory")

    path = directory / ARTIFACT_NAME
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise PrerequisiteError("Cannot safely open prerequisite artifact") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > MAX_ARTIFACT_BYTES:
            raise PrerequisiteError("Prerequisite artifact must be a small regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise PrerequisiteError("Prerequisite artifact is too large")
    finally:
        os.close(descriptor)

    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PrerequisiteError("Prerequisite artifact is not valid UTF-8 JSON") from exc
    fields = {"schema_version", "assignee", "run_id", "prerequisites"}
    if not isinstance(value, dict) or set(value) != fields:
        raise PrerequisiteError("Prerequisite artifact has an unsupported shape")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PrerequisiteError("Prerequisite artifact schema_version must be 1")
    assignee = _identifier(value["assignee"], "assignee")
    run_id = _identifier(value["run_id"], "run_id")
    if expected_assignee is not None and assignee != expected_assignee:
        raise PrerequisiteError("Prerequisite artifact assignee does not match this role")
    if expected_run_id is not None and run_id != expected_run_id:
        raise PrerequisiteError("Prerequisite artifact run_id does not match this run")
    entries = value["prerequisites"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ITEMS:
        raise PrerequisiteError("Artifact must contain 1 to 5 prerequisites")

    normalized = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"id", "evidence", "reason"}:
            raise PrerequisiteError("Each prerequisite must contain only id, evidence and reason")
        item_id = entry["id"]
        if not isinstance(item_id, str) or not ID_PATTERN.fullmatch(item_id) or item_id in seen:
            raise PrerequisiteError("Prerequisite IDs must be unique safe identifiers")
        seen.add(item_id)
        normalized.append({"id": item_id, "assignee": assignee, "run_id": run_id,
                           "evidence": _plain_text(entry["evidence"], "evidence", 400),
                           "reason": _plain_text(entry["reason"], "reason", 500),
                           "catalog": CATALOG.get(item_id)})
    return normalized


def _identifier(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise PrerequisiteError(f"{field} must be a safe non-empty identifier")
    return value


def _plain_text(value, field, limit):
    if not isinstance(value, str):
        raise PrerequisiteError(f"{field} must be plain text")
    value = value.strip()
    if (not value or len(value) > limit
            or any(ord(char) < 32 for char in value)
            or any(marker in value for marker in ("```", "`", "$(", "|", ";", "&", ">", "<"))
            or COMMAND_TEXT.search(value)):
        raise PrerequisiteError(f"{field} must contain 1 to {limit} plain-text characters")
    return value


def format_prerequisite_notice(items: list[dict]) -> str:
    """Render bounded owner-facing instructions; include only catalog commands."""
    if not items:
        return ""
    sections = ["Команде нужна помощь с подготовкой среды:"]
    for item in items[:MAX_ITEMS]:
        catalog = CATALOG.get(item.get("id")) if isinstance(item, dict) else None
        if not catalog:
            sections.append(
                f"• Нужна диагностика ({item.get('id', 'unknown')}): "
                f"{item.get('reason', 'роль запросила проверку среды')}\n"
                f"Проверено: {item.get('evidence', 'подробности недоступны')}\n"
                "Безопасную команду пока не предлагаю."
            )
            continue
        block = [f"• {catalog['title']}", f"Причина: {item['reason']}",
                 f"Проверено: {item['evidence']}", catalog["where"]]
        if catalog["command"]:
            block.append("Команда: " + catalog["command"])
            block.append("Запусти её вручную; Hermes не вводит пароль sudo и не выполняет sudo за тебя.")
            block.append("После установки нажми «Установил — проверить снова»; Hermes повторит проверку.")
        else:
            block.append("Команду установки не выдаю: передай это Hermes для обслуживания управляемой среды.")
        sections.append("\n".join(block))
    return "\n\n".join(sections)
