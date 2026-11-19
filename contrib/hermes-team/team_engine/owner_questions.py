"""Validation for concise, user-facing owner decision questions."""
from __future__ import annotations

import re


_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def validate_questions(value):
    """Return a normalized question package or raise ValueError.

    The caller deliberately treats invalid model output as legacy input, so this
    validator is strict and has no best-effort coercions.
    """
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("QUESTIONS.json must use schema_version 1")
    items = value.get("questions")
    if not isinstance(items, list) or not 1 <= len(items) <= 5:
        raise ValueError("QUESTIONS.json must contain 1 to 5 questions")
    normalized = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each question must be an object")
        qid = item.get("id")
        if not isinstance(qid, str) or not _ID.fullmatch(qid) or qid in seen:
            raise ValueError("Question IDs must be unique short identifiers")
        seen.add(qid)
        question = _text(item, "question", 500)
        example = _text(item, "example", 1000)
        options = item.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            raise ValueError("Each question needs 2 to 4 options")
        option_ids = set()
        clean_options = []
        for option in options:
            if not isinstance(option, dict):
                raise ValueError("Each option must be an object")
            oid = option.get("id")
            if not isinstance(oid, str) or not _ID.fullmatch(oid) or oid in option_ids:
                raise ValueError("Option IDs must be unique short identifiers")
            option_ids.add(oid)
            clean_options.append({"id": oid, "label": _text(option, "label", 120),
                                  "description": _text(option, "description", 500)})
        recommended = item.get("recommended_option_id")
        if recommended not in option_ids:
            raise ValueError("The recommended option must exist")
        reason = _text(item, "recommendation_reason", 700)
        normalized.append({"id": qid, "question": question, "example": example,
                           "options": clean_options,
                           "recommended_option_id": recommended,
                           "recommendation_reason": reason, "answer": None})
    return normalized


def _text(item, name, limit):
    value = item.get(name)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{name} must contain 1 to {limit} characters")
    return value.strip()
