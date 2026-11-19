"""Semantic gates for independent reviews of a plan before implementation."""
from __future__ import annotations

import re
from typing import Any, Mapping


class PlanGateError(ValueError):
    """A plan review or judge decision is malformed or insufficient."""


_ROLES = {"skeptic", "test-designer"}
_VERDICTS = {"pass", "changes_requested", "blocked"}
_SEVERITIES = {"info", "low", "medium", "high", "critical"}
_DISPOSITIONS = {"resolved", "dismissed", "revise", "needs_input"}
_PLAN_HASH = re.compile(r"^[0-9a-f]{64}$")
_FINDING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _substantive(value: Any) -> bool:
    if not isinstance(value, str) or len(value.strip()) < 12:
        return False
    return value.strip().lower() not in {
        "n/a", "none", "tbd", "todo", "unknown", "not applicable", "no evidence",
    }


def _check_binding(feature_id: str, plan_sha256: str) -> None:
    if not isinstance(feature_id, str) or not feature_id.strip():
        raise PlanGateError("feature_id must be a non-empty string")
    if not isinstance(plan_sha256, str) or not _PLAN_HASH.fullmatch(plan_sha256):
        raise PlanGateError("plan_sha256 must be a lowercase SHA-256 hex digest")


def _validate_review(
    report: Mapping[str, Any], *, role: str, feature_id: str,
    plan_sha256: str, planner_assignee: str,
) -> tuple[str, str, dict[str, dict[str, Any]], str]:
    if not isinstance(report, Mapping):
        raise PlanGateError(f"{role} report must be a JSON object")
    required = {"schema_version", "feature_id", "plan_sha256", "role", "assignee",
                "run_id", "verdict", "findings"}
    missing = required - set(report)
    if missing:
        raise PlanGateError(f"{role} report missing fields: {', '.join(sorted(missing))}")
    if type(report["schema_version"]) is not int or report["schema_version"] != 1:
        raise PlanGateError(f"{role} report schema_version must be integer 1")
    if report["feature_id"] != feature_id or report["plan_sha256"] != plan_sha256:
        raise PlanGateError(f"{role} report is bound to a different feature or plan hash")
    if report["role"] != role:
        raise PlanGateError(f"Expected role {role!r}, got {report['role']!r}")
    expected_assignee = f"team-{role}"
    assignee = report["assignee"]
    run_id = report["run_id"]
    if assignee != expected_assignee:
        raise PlanGateError(f"{role} report assignee must be {expected_assignee}")
    if not isinstance(run_id, str) or not run_id.strip():
        raise PlanGateError(f"{role} report run_id must be a non-empty string")
    if assignee == planner_assignee:
        raise PlanGateError(f"{role} must be independent from the plan author")
    verdict = report["verdict"]
    if not isinstance(verdict, str) or verdict not in _VERDICTS:
        raise PlanGateError(f"{role} report has invalid verdict")
    findings = report["findings"]
    if not isinstance(findings, list):
        raise PlanGateError(f"{role} findings must be a list")
    by_id: dict[str, dict[str, Any]] = {}
    finding_required = {"id", "severity", "blocking", "claim", "evidence", "requested_change"}
    for index, finding in enumerate(findings):
        if not isinstance(finding, Mapping):
            raise PlanGateError(f"{role} finding {index} must be an object")
        missing_finding = finding_required - set(finding)
        if missing_finding:
            raise PlanGateError(f"{role} finding {index} missing fields: "
                                + ", ".join(sorted(missing_finding)))
        finding_id = finding["id"]
        if not isinstance(finding_id, str) or not _FINDING_ID.fullmatch(finding_id):
            raise PlanGateError(f"{role} finding {index} has invalid id")
        if finding_id in by_id:
            raise PlanGateError(f"{role} report has duplicate finding id {finding_id!r}")
        severity = finding["severity"]
        if not isinstance(severity, str) or severity not in _SEVERITIES:
            raise PlanGateError(f"{role} finding {finding_id} has invalid severity")
        if not isinstance(finding["blocking"], bool):
            raise PlanGateError(f"{role} finding {finding_id} blocking must be boolean")
        if not _substantive(finding["claim"]):
            raise PlanGateError(f"{role} finding {finding_id} needs a substantive claim")
        if not _substantive(finding["evidence"]):
            raise PlanGateError(f"{role} finding {finding_id} needs substantive evidence")
        requested_change = finding["requested_change"]
        if not isinstance(requested_change, str):
            raise PlanGateError(f"{role} finding {finding_id} requested_change must be a string")
        if (finding["blocking"] or severity in {"high", "critical"}) and not _substantive(requested_change):
            raise PlanGateError(f"{role} finding {finding_id} needs a substantive requested_change")
        by_id[finding_id] = dict(finding)
    return assignee, run_id, by_id, verdict


def validate_plan_reviews(
    skeptic_review: Mapping[str, Any],
    test_designer_review: Mapping[str, Any],
    *,
    feature_id: str,
    plan_sha256: str,
    planner_assignee: str = "team-planner",
) -> None:
    """Validate both independent review reports and return when structurally sound.

    Verdicts are inputs to a distinct judge; this function does not count votes
    or decide whether the plan is ready to implement.
    """
    _check_binding(feature_id, plan_sha256)
    skeptic = _validate_review(skeptic_review, role="skeptic", feature_id=feature_id,
                               plan_sha256=plan_sha256, planner_assignee=planner_assignee)
    designer = _validate_review(test_designer_review, role="test-designer", feature_id=feature_id,
                                plan_sha256=plan_sha256, planner_assignee=planner_assignee)
    if skeptic[0] == designer[0]:
        raise PlanGateError("Skeptic and test-designer must be different assignees")
    if skeptic[1] == designer[1]:
        raise PlanGateError("Skeptic and test-designer must have distinct run_id values")


def validate_plan_review(
    report: Mapping[str, Any], *, role: str, feature_id: str,
    plan_sha256: str, planner_assignee: str = "team-planner",
) -> None:
    """Validate one independent review before the pair-level consistency gate."""
    _check_binding(feature_id, plan_sha256)
    if role not in _ROLES:
        raise PlanGateError(f"Unsupported plan review role: {role!r}")
    _validate_review(report, role=role, feature_id=feature_id,
                     plan_sha256=plan_sha256, planner_assignee=planner_assignee)


def validate_plan_decision(
    decision_report: Mapping[str, Any],
    skeptic_review: Mapping[str, Any],
    test_designer_review: Mapping[str, Any],
    *,
    feature_id: str,
    plan_sha256: str,
    planner_assignee: str = "team-planner",
) -> str:
    """Validate judge decision and explicit handling of material findings.

    ``accept`` is prohibited when either reviewer is blocked or any blocking,
    high, or critical finding is unaccounted for. Each such finding must be
    explicitly resolved or dismissed with substantive reasoning and evidence.
    The judge decides; review verdicts are never counted as votes.
    """
    _check_binding(feature_id, plan_sha256)
    skeptic = _validate_review(skeptic_review, role="skeptic", feature_id=feature_id,
                               plan_sha256=plan_sha256, planner_assignee=planner_assignee)
    designer = _validate_review(test_designer_review, role="test-designer", feature_id=feature_id,
                                plan_sha256=plan_sha256, planner_assignee=planner_assignee)
    if skeptic[0] == designer[0] or skeptic[1] == designer[1]:
        raise PlanGateError("Skeptic and test-designer must have independent assignees and runs")
    if not isinstance(decision_report, Mapping):
        raise PlanGateError("Judge decision must be a JSON object")
    required = {"schema_version", "feature_id", "plan_sha256", "assignee", "run_id",
                "decision", "resolutions", "summary"}
    missing = required - set(decision_report)
    if missing:
        raise PlanGateError("Judge decision missing fields: " + ", ".join(sorted(missing)))
    if type(decision_report["schema_version"]) is not int or decision_report["schema_version"] != 1:
        raise PlanGateError("Judge decision schema_version must be integer 1")
    if decision_report["feature_id"] != feature_id or decision_report["plan_sha256"] != plan_sha256:
        raise PlanGateError("Judge decision is bound to a different feature or plan hash")
    if decision_report["assignee"] != "team-judge":
        raise PlanGateError("Judge decision assignee must be team-judge")
    judge_run = decision_report["run_id"]
    if not isinstance(judge_run, str) or not judge_run.strip():
        raise PlanGateError("Judge run_id must be a non-empty string")
    if judge_run in {skeptic[1], designer[1]}:
        raise PlanGateError("Judge must use a distinct run_id")
    if decision_report["assignee"] in {skeptic[0], designer[0], planner_assignee}:
        raise PlanGateError("Judge must be independent from the plan author and reviewers")
    decision = decision_report["decision"]
    if not isinstance(decision, str) or decision not in {"accept", "revise", "needs_input"}:
        raise PlanGateError("Judge decision must be accept, revise, or needs_input")
    if not _substantive(decision_report["summary"]):
        raise PlanGateError("Judge summary must provide substantive reasoning")

    findings: dict[str, dict[str, Any]] = {}
    for role, result in (("skeptic", skeptic), ("test-designer", designer)):
        for finding_id, finding in result[2].items():
            findings[f"{role}:{finding_id}"] = finding
    resolutions = decision_report["resolutions"]
    if not isinstance(resolutions, list):
        raise PlanGateError("Judge resolutions must be a list")
    by_finding: dict[str, dict[str, Any]] = {}
    for index, resolution in enumerate(resolutions):
        if not isinstance(resolution, Mapping):
            raise PlanGateError(f"Judge resolution {index} must be an object")
        for key in ("finding_id", "disposition", "reason", "evidence"):
            if key not in resolution:
                raise PlanGateError(f"Judge resolution {index} missing {key}")
        finding_id = resolution["finding_id"]
        if not isinstance(finding_id, str) or finding_id not in findings:
            raise PlanGateError(f"Judge resolution refers to unknown finding {finding_id!r}")
        if finding_id in by_finding:
            raise PlanGateError(f"Duplicate resolution for finding {finding_id!r}")
        disposition = resolution["disposition"]
        if not isinstance(disposition, str) or disposition not in _DISPOSITIONS:
            raise PlanGateError(f"Invalid disposition for finding {finding_id!r}")
        if not _substantive(resolution["reason"]):
            raise PlanGateError(f"Resolution {finding_id!r} needs substantive reason")
        if not _substantive(resolution["evidence"]):
            raise PlanGateError(f"Resolution {finding_id!r} needs substantive evidence")
        by_finding[finding_id] = dict(resolution)

    if decision == "accept":
        if skeptic[3] == "blocked" or designer[3] == "blocked":
            raise PlanGateError("A blocked review cannot be accepted")
        for finding_id, finding in findings.items():
            material = finding["blocking"] or finding["severity"] in {"high", "critical"}
            if not material:
                continue
            resolution = by_finding.get(finding_id)
            if resolution is None:
                raise PlanGateError(f"Material finding {finding_id!r} is not accounted for")
            if resolution["disposition"] not in {"resolved", "dismissed"}:
                raise PlanGateError(f"Material finding {finding_id!r} remains unresolved")
    elif decision == "revise":
        if not any(item["disposition"] == "revise" for item in by_finding.values()):
            raise PlanGateError("Revise decision requires at least one finding disposition revise")
    elif decision == "needs_input":
        if not any(item["disposition"] == "needs_input" for item in by_finding.values()):
            raise PlanGateError("needs_input decision requires a finding disposition needs_input")
    return decision
