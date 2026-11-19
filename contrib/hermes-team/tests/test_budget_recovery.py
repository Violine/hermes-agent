from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.controller import Controller


class FakeBoard:
    def __init__(self, *, profile="team-backend", status="blocked", kind="transient",
                 elapsed=1159, cap=1290, error="The card’s runtime cap ran out"):
        self.tasks = {
            "root": {"id": "root", "status": "blocked"},
            "dev": {"id": "dev", "assignee": profile, "status": status,
                    "block_kind": kind, "blocked_reason": error,
                    "current_run_id": None, "claim_lock": None,
                    "max_runtime_seconds": cap},
            "sibling": {"id": "sibling", "assignee": "team-backend", "status": "done"},
        }
        self.runs = {"dev": [{"id": 103, "profile": profile, "status": "blocked",
                              "elapsed_seconds": elapsed, "ended_at": 2,
                              "error": error}]}
        self.comments = []
        self.activations = []

    def get_task(self, task_id):
        return self.tasks.get(task_id)

    def get_runs(self, task_id):
        return self.runs.get(task_id, [])

    def add_comment(self, *args):
        self.comments.append(args)

    def set_waiting_task_runtime(self, task_id, runtime):
        self.tasks[task_id]["max_runtime_seconds"] = runtime

    def activate_task(self, task_id):
        self.activations.append(task_id)
        self.tasks[task_id]["status"] = "ready"
        self.tasks[task_id]["current_run_id"] = 104


class BudgetRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.controller = Controller.__new__(Controller)
        self.controller.board = FakeBoard()
        self.controller.quota = None
        self.controller.waiting_cards = set()
        self.controller.config = {"roles": {"team-backend": {"run_budget": 1800}}}
        self.saved = []
        self.controller.save = lambda state: self.saved.append(copy.deepcopy(state))
        self.controller.event = lambda *args, **kwargs: None
        self.state = {
            "id": "feature-1", "stage": "blocked", "resume_stage": "implementation",
            "blocker": "Card dev requires attention: blocked", "root_card": "root", "index": 1,
            "items": [
                {"definition": {"id": "T-004", "assignee_role": "backend"},
                 "card": "sibling", "status": "done", "round": 1},
                {"definition": {"id": "T-005", "assignee_role": "backend"},
                 "card": "dev", "status": "implementation", "round": 1},
            ],
            "dispatch": {},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_auto_continues_budget_exhausted_worker_in_place_and_recovers_root(self):
        result = self.controller._continue_budget_exhausted_implementation(self.state)

        self.assertTrue(result)
        self.assertEqual(self.controller.board.activations, ["dev"])
        self.assertEqual(self.controller.board.tasks["sibling"]["status"], "done")
        self.assertEqual(self.state["stage"], "implementation")
        self.assertNotIn("blocker", self.state)
        self.assertTrue(self.state["resume_requested"])
        self.assertEqual(self.state["budget_continuations"]["dev:r1"]["old_run_id"], "103")
        self.assertIn("сохранением", self.controller.board.comments[0][2].lower())
        self.assertTrue(self.saved)

    def test_needs_input_does_not_auto_continue(self):
        self.controller.board.tasks["dev"]["block_kind"] = "needs_input"
        self.controller.board.tasks["dev"]["blocked_reason"] = "Please clarify the due date"
        self.controller.board.runs["dev"][0]["error"] = "Please clarify the due date"

        self.assertFalse(self.controller._continue_budget_exhausted_implementation(self.state))
        self.assertEqual(self.controller.board.activations, [])
        self.assertNotIn("budget_continuations", self.state)

    def test_second_exhaustion_is_bounded_to_one_continuation_per_item_round(self):
        self.assertTrue(self.controller._continue_budget_exhausted_implementation(self.state))
        task = self.controller.board.tasks["dev"]
        task.update({"status": "blocked", "block_kind": "transient",
                     "current_run_id": None, "claim_lock": None})
        self.controller.board.runs["dev"].append({
            "id": 104, "profile": "team-backend", "status": "blocked",
            "elapsed_seconds": 1700, "ended_at": 4000,
            "max_runtime_seconds": 1890, "error": "runtime cap reached",
        })

        self.assertFalse(self.controller._continue_budget_exhausted_implementation(self.state))
        self.assertEqual(self.controller.board.activations, ["dev"])

    def test_wrong_profile_or_active_claim_is_rejected(self):
        self.controller.board.runs["dev"][0]["profile"] = "team-frontend"
        self.assertFalse(self.controller._continue_budget_exhausted_implementation(self.state))
        self.controller.board.runs["dev"][0]["profile"] = "team-backend"
        self.controller.board.tasks["dev"]["current_run_id"] = 103
        self.assertFalse(self.controller._continue_budget_exhausted_implementation(self.state))
        self.controller.board.tasks["dev"].update({"current_run_id": None, "worker_pid": 412})
        self.assertFalse(self.controller._continue_budget_exhausted_implementation(self.state))
        self.assertEqual(self.controller.board.activations, [])

    def test_generic_timeout_without_budget_marker_is_rejected(self):
        task = self.controller.board.tasks["dev"]
        task["blocked_reason"] = "Test command timed out"
        self.controller.board.runs["dev"][0].update({
            "error": "Test command timed out", "timed_out": True,
        })
        self.assertFalse(self.controller._continue_budget_exhausted_implementation(self.state))
        self.assertEqual(self.controller.board.activations, [])

    def test_quota_wait_keeps_durable_intent_and_uses_fresh_lease_key(self):
        class UnknownQuota:
            def __init__(self):
                self.released = []
                self.reserved = []

            def release(self, key):
                self.released.append(key)
                return True

            def reserve(self, *_args):
                self.reserved.append(_args[0])
                return {"available": False, "reason": "usage snapshot unknown", "selection": None}

        self.controller.quota = UnknownQuota()
        self.state["dispatch"] = {"dev": {
            "role": "backend", "settings": {"run_budget": 1800}, "key": "old-lease",
            "priority": 20, "armed": True, "waiting": False,
        }}
        result = self.controller._continue_budget_exhausted_implementation(self.state)

        self.assertTrue(result)
        key = self.state["dispatch"]["dev"]["key"]
        self.assertNotEqual(key, "old-lease")
        self.assertTrue(self.saved)
        self.assertEqual(self.state["budget_continuations"]["dev:r1"]["status"], "prepared")
        self.assertTrue(self.state["dispatch"]["dev"]["waiting"])
        self.assertEqual(self.controller.board.activations, [])
        self.controller.reconcile_dispatch(self.state)
        self.assertIn("dev", self.controller.waiting_cards)
        self.assertEqual(self.controller.board.tasks["dev"]["status"], "blocked")
        self.assertIn("old-lease", self.controller.quota.released)
        self.assertEqual(self.controller.quota.reserved, [key])

    def test_native_run_must_be_latest_matching_attempt(self):
        self.controller.board.runs["dev"].append({
            "id": 104, "profile": "team-backend", "status": "running", "ended_at": None,
        })
        self.assertFalse(self.controller._continue_budget_exhausted_implementation(self.state))
        self.assertEqual(self.controller.board.activations, [])

    def test_restart_after_native_activation_finishes_saved_intent_idempotently(self):
        self.state["budget_continuations"] = {"dev:r1": {
            "item_id": "T-005", "round": 1, "old_run_id": "103",
            "status": "prepared", "quota_key": "feature-1:T-005:budget-continuation:r1:1",
        }}
        self.controller.board.tasks["dev"].update({
            "status": "running", "current_run_id": 104, "claim_lock": "worker-claim",
        })

        self.assertTrue(self.controller._continue_budget_exhausted_implementation(self.state))
        self.assertEqual(self.state["budget_continuations"]["dev:r1"]["status"], "activated")
        self.assertEqual(self.controller.board.activations, [])

    def test_unrelated_blocked_feature_reason_is_not_auto_recovered(self):
        self.state["blocker"] = "Base branch changed; plan and evidence require a fresh baseline"

        self.assertFalse(self.controller._continue_budget_exhausted_implementation(self.state))
        self.assertEqual(self.controller.board.activations, [])
        self.assertNotIn("budget_continuations", self.state)

    def test_real_native_run_shape_uses_historical_duration_and_runtime_cap(self):
        self.controller.board.tasks["dev"]["max_runtime_seconds"] = 3690
        self.controller.board.runs["dev"] = [{
            "id": 103, "profile": "team-backend", "status": "blocked",
            "started_at": 1793270038, "ended_at": 1793271197,
            "max_runtime_seconds": 1290,
            "summary": "The card's runtime cap ran out (1290 s).",
            "claim_lock": None, "worker_pid": None,
        }]

        self.assertTrue(self.controller._continue_budget_exhausted_implementation(self.state))
        self.assertEqual(self.controller.board.activations, ["dev"])

    def test_prepared_intent_does_not_resume_card_reassigned_to_another_role(self):
        self.state["budget_continuations"] = {"dev:r1": {
            "item_id": "T-005", "round": 1, "old_run_id": "103",
            "status": "prepared",
        }}
        self.controller.board.tasks["dev"].update(status="running", assignee="team-frontend")

        self.assertFalse(self.controller._continue_budget_exhausted_implementation(self.state))
        self.assertEqual(self.state["stage"], "blocked")

    def test_tick_checks_existing_blocked_implementation_before_early_return(self):
        calls = []
        self.controller._continue_budget_exhausted_implementation = (
            lambda state: calls.append(state["stage"]) or False
        )
        self.controller.reconcile_dispatch = lambda *_args, **_kwargs: None
        self.controller.update_efficiency = lambda _state: None

        self.controller.tick(self.state)

        self.assertEqual(calls, ["blocked"])


if __name__ == "__main__":
    unittest.main()
