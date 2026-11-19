from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.plan_gates import PlanGateError, validate_plan_decision, validate_plan_reviews


FEATURE = "feature-42"
PLAN_HASH = "a" * 64


def finding(*, finding_id="F-1", severity="medium", blocking=False):
    return {
        "id": finding_id,
        "severity": severity,
        "blocking": blocking,
        "claim": "The plan has an observable technical gap",
        "evidence": "Task T-2 has no error-path acceptance criterion",
        "requested_change": "Add a measurable failure scenario to its criteria",
    }


def review(role, *, findings=None, verdict="pass", assignee=None, run_id=None,
           feature_id=FEATURE, plan_sha256=PLAN_HASH):
    return {
        "schema_version": 1,
        "feature_id": feature_id,
        "plan_sha256": plan_sha256,
        "role": role,
        "assignee": assignee or f"team-{role}",
        "run_id": run_id or f"run-{role}",
        "verdict": verdict,
        "findings": [] if findings is None else findings,
    }


def decision(*, resolutions=None, value="accept", assignee="team-judge",
             run_id="run-judge", feature_id=FEATURE, plan_sha256=PLAN_HASH,
             summary="The plan is implementable with the accepted evidence and scope"):
    return {
        "schema_version": 1,
        "feature_id": feature_id,
        "plan_sha256": plan_sha256,
        "assignee": assignee,
        "run_id": run_id,
        "decision": value,
        "resolutions": [] if resolutions is None else resolutions,
        "summary": summary,
    }


def resolution(finding_id="skeptic:F-1", *, disposition="resolved", reason=None, evidence=None):
    return {
        "finding_id": finding_id,
        "disposition": disposition,
        "reason": reason or "The correction closes the identified acceptance gap",
        "evidence": evidence or "Updated criterion AC-T2-3 covers the failure case",
    }


class PlanGateTests(unittest.TestCase):
    def setUp(self):
        self.skeptic = review("skeptic")
        self.designer = review("test-designer")

    def validate_pair(self, skeptic=None, designer=None):
        validate_plan_reviews(skeptic or self.skeptic, designer or self.designer,
                              feature_id=FEATURE, plan_sha256=PLAN_HASH)

    def validate_decision(self, report, skeptic=None, designer=None):
        return validate_plan_decision(report, skeptic or self.skeptic, designer or self.designer,
                                      feature_id=FEATURE, plan_sha256=PLAN_HASH)

    def test_accepts_two_independent_reviews_bound_to_exact_plan(self):
        self.assertIsNone(validate_plan_reviews(self.skeptic, self.designer,
                                                feature_id=FEATURE, plan_sha256=PLAN_HASH))

    def test_rejects_stale_feature_or_plan_hash_and_invalid_schema(self):
        cases = [
            (review("skeptic", feature_id="another-feature"), self.designer),
            (self.skeptic, review("test-designer", plan_sha256="b" * 64)),
            ({**self.skeptic, "schema_version": True}, self.designer),
            ([self.skeptic], self.designer),
        ]
        for skeptic, designer in cases:
            with self.subTest(skeptic=skeptic), self.assertRaises(PlanGateError):
                self.validate_pair(skeptic, designer)

    def test_rejects_non_independent_reviewers_and_author(self):
        cases = [
            (self.skeptic, review("test-designer", assignee="team-skeptic")),
            (review("skeptic", run_id="same-run"), review("test-designer", run_id="same-run")),
            (review("skeptic", assignee="team-planner"), self.designer),
        ]
        for skeptic, designer in cases:
            with self.subTest(skeptic=skeptic), self.assertRaises(PlanGateError):
                self.validate_pair(skeptic, designer)

    def test_rejects_malformed_review_verdict_and_finding_shape(self):
        cases = [
            review("skeptic", verdict="approve"),
            review("skeptic", verdict=["pass"]),
            review("skeptic", findings=[None]),
            review("skeptic", findings=[{**finding(), "blocking": 1}]),
            review("skeptic", findings=[{**finding(), "id": "bad id"}]),
            review("skeptic", findings=[finding(), finding()]),
            review("skeptic", findings=[{**finding(), "evidence": "N/A"}]),
        ]
        for skeptic in cases:
            with self.subTest(skeptic=skeptic), self.assertRaises(PlanGateError):
                self.validate_pair(skeptic, self.designer)

    def test_accept_without_material_findings_is_a_judge_decision_not_a_vote(self):
        skeptic = review("skeptic", verdict="changes_requested")
        designer = review("test-designer", verdict="pass")
        self.assertEqual(self.validate_decision(decision(), skeptic, designer), "accept")

    def test_reviewer_blocked_verdict_cannot_be_accepted(self):
        skeptic = review("skeptic", verdict="blocked")
        with self.assertRaisesRegex(PlanGateError, "blocked review"):
            self.validate_decision(decision(), skeptic, self.designer)

    def test_blocking_and_high_findings_must_be_resolved_or_dismissed_with_evidence(self):
        test_cases = [
            (finding(severity="low", blocking=True), "skeptic:F-1"),
            (finding(severity="high", blocking=False), "skeptic:F-1"),
            (finding(severity="critical", blocking=False), "skeptic:F-1"),
        ]
        for item, qualified_id in test_cases:
            skeptic = review("skeptic", findings=[item])
            with self.subTest(item=item), self.assertRaisesRegex(PlanGateError, "not accounted"):
                self.validate_decision(decision(), skeptic, self.designer)
            self.assertEqual(self.validate_decision(decision(resolutions=[resolution(qualified_id)]),
                                                    skeptic, self.designer), "accept")
            dismissed = resolution(qualified_id, disposition="dismissed")
            self.assertEqual(self.validate_decision(decision(resolutions=[dismissed]),
                                                    skeptic, self.designer), "accept")

    def test_blocking_findings_cannot_be_marked_revise_or_needs_input_and_accepted(self):
        skeptic = review("skeptic", findings=[finding(severity="high")])
        for disposition in ("revise", "needs_input"):
            with self.subTest(disposition=disposition), self.assertRaisesRegex(PlanGateError, "unresolved"):
                self.validate_decision(decision(resolutions=[resolution(disposition=disposition)]),
                                       skeptic, self.designer)

    def test_low_nonblocking_finding_may_remain_open(self):
        skeptic = review("skeptic", findings=[finding(severity="low")])
        self.assertEqual(self.validate_decision(decision(), skeptic, self.designer), "accept")

    def test_rejects_unknown_duplicate_or_unsubstantiated_resolutions(self):
        skeptic = review("skeptic", findings=[finding(severity="critical")])
        cases = [
            [resolution("skeptic:missing")],
            [resolution(), resolution()],
            [resolution(reason="N/A")],
            [resolution(evidence="none")],
        ]
        for resolutions in cases:
            with self.subTest(resolutions=resolutions), self.assertRaises(PlanGateError):
                self.validate_decision(decision(resolutions=resolutions), skeptic, self.designer)

    def test_judge_must_be_independent_and_bound_to_same_plan(self):
        cases = [
            decision(assignee="team-skeptic"),
            decision(run_id="run-skeptic"),
            decision(plan_sha256="b" * 64),
            decision(summary="TBD"),
        ]
        for report in cases:
            with self.subTest(report=report), self.assertRaises(PlanGateError):
                self.validate_decision(report)

    def test_nonaccept_decisions_need_matching_resolution_disposition(self):
        with self.assertRaisesRegex(PlanGateError, "requires at least one finding"):
            self.validate_decision(decision(value="revise"))
        with self.assertRaisesRegex(PlanGateError, "requires a finding disposition"):
            self.validate_decision(decision(value="needs_input"))
        self.assertEqual(self.validate_decision(decision(value="revise", resolutions=[
            resolution("skeptic:F-1", disposition="revise")
        ]), review("skeptic", findings=[finding(severity="low")]), self.designer), "revise")


if __name__ == "__main__":
    unittest.main()
