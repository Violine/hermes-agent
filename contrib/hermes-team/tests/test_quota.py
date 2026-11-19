from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.quota import QuotaError, QuotaManager


NOW = 2_000_000_000


def pool(provider, *, enabled=True, auth_ready=True, max_concurrent=2,
         reserve_percent=20, unknown_policy="wait", max_snapshot_age=180):
    return {"provider": provider, "enabled": enabled, "auth_ready": auth_ready,
            "max_concurrent": max_concurrent, "reserve_percent": reserve_percent,
            "unknown_policy": unknown_policy, "max_snapshot_age": max_snapshot_age}


def snapshot(used_percent=30, *, observed_at=NOW, windows=None, error=None, source="test-probe"):
    return {"source": source, "observed_at": observed_at,
            "windows": windows if windows is not None else [
                {"name": "weekly", "used_percent": used_percent, "reset_at": NOW + 3600}
            ], "error": error}


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock_value = NOW
        self.config = {"pools": {
            "codex": pool("openai-codex"),
            "claude": pool("claude-code"),
            "kimi": pool("kimi-coding"),
            "unknown-ok": pool("unknown-provider", unknown_policy="allow_limited"),
        }}
        self.manager = self.new_manager(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def new_manager(self, config):
        return QuotaManager(config, self.root / "ledger.json", clock=lambda: self.clock_value)

    def role(self, *, provider="openai-codex", model="codex-model", effort="low", pool_id=None):
        value = {"provider": provider, "model": model, "reasoning_effort": effort}
        if pool_id:
            value["pool"] = pool_id
        return value

    def install_snapshots(self, *, codex=30, claude=30, kimi=30):
        for pool_id, used in (("codex", codex), ("claude", claude), ("kimi", kimi)):
            self.manager.update_snapshot(pool_id, snapshot(used))

    def test_default_provider_maps_to_matching_pool_and_lease_is_idempotent(self):
        self.manager.update_snapshot("codex", snapshot(40))
        first = self.manager.reserve("task-1", "developer", self.role(), priority=20)
        second = self.manager.reserve("task-1", "developer", self.role(), priority=20)
        self.assertTrue(first["available"])
        self.assertEqual(first["selection"], {"pool": "codex", "provider": "openai-codex",
                                               "model": "codex-model", "reasoning_effort": "low"})
        self.assertTrue(second["available"])
        self.assertEqual(second["reason"], "existing reservation (idempotent)")
        self.assertEqual(self.manager.status()["pools"]["codex"]["active_leases"], 1)
        self.assertEqual(self.manager.status()["leases"]["task-1"]["priority"], 20)

    def test_reservation_key_cannot_be_reused_for_a_different_role(self):
        self.manager.update_snapshot("codex", snapshot(40))
        self.manager.reserve("shared-task-key", "developer", self.role())
        with self.assertRaisesRegex(QuotaError, "already assigned to role"):
            self.manager.reserve("shared-task-key", "qa", self.role())

    def test_released_key_must_pass_admission_again_before_reactivation(self):
        self.manager.update_snapshot("codex", snapshot(20))
        role = self.role(provider="openai-codex", model="first-model", pool_id="codex")
        first = self.manager.reserve("retry-key", "developer", role)
        self.assertTrue(first["available"])
        self.assertTrue(self.manager.release("retry-key"))

        self.manager.update_snapshot("codex", snapshot(100))
        denied = self.manager.reserve("retry-key", "developer", role)
        self.assertFalse(denied["available"])
        self.assertFalse(self.manager.status()["leases"]["retry-key"]["active"])

        self.manager.update_snapshot("codex", snapshot(30))
        retry_role = self.role(provider="openai-codex", model="second-model", pool_id="codex")
        retried = self.manager.reserve("retry-key", "developer", retry_role)
        self.assertTrue(retried["available"])
        self.assertEqual(retried["selection"]["model"], "second-model")
        lease = self.manager.status()["leases"]["retry-key"]
        self.assertTrue(lease["active"])
        self.assertEqual(lease["generation"], 2)
        self.assertEqual(lease["previous_released_at"], NOW)

    def test_developer_preserves_configured_reserve_but_validation_role_can_use_it(self):
        self.manager.update_snapshot("codex", snapshot(80))
        developer = self.manager.reserve("developer-task", "developer", self.role())
        self.assertFalse(developer["available"])
        self.manager.update_snapshot("codex", snapshot(95))
        qa = self.manager.reserve("qa-task", "qa", self.role())
        self.assertTrue(qa["available"])
        self.manager.update_snapshot("codex", snapshot(100))
        exhausted = self.manager.reserve("qa-task-2", "qa", self.role())
        self.assertFalse(exhausted["available"])

    def test_all_windows_constrain_available_headroom_and_reset_time_is_reported(self):
        self.manager.update_snapshot("codex", snapshot(windows=[
            {"name": "five-hour", "used_percent": 20, "reset_at": NOW + 100},
            {"name": "weekly", "used_percent": 90, "reset_at": NOW + 1000},
        ]))
        result = self.manager.reserve("qa-1", "qa", self.role())
        self.assertTrue(result["available"])
        self.assertIn("10.0% headroom", result["reason"])
        self.assertEqual(result["reset_at"], NOW + 100)

    def test_blocked_result_reports_latest_relevant_window_reset(self):
        self.manager.update_snapshot("codex", snapshot(windows=[
            {"name": "five-hour", "used_percent": 90, "reset_at": NOW + 5000},
            {"name": "weekly", "used_percent": 85, "reset_at": NOW + 3000},
            {"name": "healthy-daily", "used_percent": 50, "reset_at": NOW + 100},
        ]))
        result = self.manager.reserve("quota-block", "developer", self.role())
        self.assertFalse(result["available"])
        self.assertEqual(result["reset_at"], NOW + 5000)

        status = self.manager.status()["pools"]["codex"]
        self.assertEqual(status["reset_at"], NOW + 5000)
        self.assertEqual(status["snapshot_source"], "test-probe")
        self.assertEqual(status["snapshot_observed_at"], NOW)
        self.assertEqual(status["snapshot_age_seconds"], 0)
        self.assertEqual([window["name"] for window in status["snapshot_windows"]],
                         ["five-hour", "weekly", "healthy-daily"])

    def test_unknown_reset_on_a_constraining_window_stays_unknown(self):
        self.manager.update_snapshot("codex", snapshot(windows=[
            {"name": "rolling", "used_percent": 95, "reset_at": None},
            {"name": "weekly", "used_percent": 90, "reset_at": NOW + 5000},
            {"name": "healthy", "used_percent": 10, "reset_at": NOW + 100},
        ]))
        result = self.manager.reserve("no-known-reset", "developer", self.role())
        self.assertFalse(result["available"])
        self.assertIsNone(result["reset_at"])
        self.assertIsNone(self.manager.status()["pools"]["codex"]["reset_at"])

    def test_blocked_candidates_report_earliest_relevant_reset(self):
        self.manager.update_snapshot("codex", snapshot(90))
        self.manager.update_snapshot("claude", snapshot(90))
        # Codex's only constraining window resets first; the fallback candidate
        # remains explicitly configured and has its own later reset.
        self.manager.update_snapshot("codex", snapshot(windows=[
            {"name": "primary", "used_percent": 90, "reset_at": NOW + 1000},
        ]))
        self.manager.update_snapshot("claude", snapshot(windows=[
            {"name": "fallback", "used_percent": 90, "reset_at": NOW + 3000},
        ]))
        result = self.manager.reserve("alternatives-blocked", "developer", {"candidates": [
            {"pool": "codex", "provider": "openai-codex", "model": "codex"},
            {"pool": "claude", "provider": "claude-code", "model": "claude", "tier": 1},
        ]})
        self.assertFalse(result["available"])
        self.assertEqual(result["reset_at"], NOW + 1000)

    def test_unknown_usage_never_means_zero_and_default_policy_waits(self):
        result = self.manager.reserve("dev-unknown", "developer", self.role())
        self.assertFalse(result["available"])
        self.assertIn("quota unknown", result["reason"])
        status = self.manager.status()["pools"]["codex"]
        self.assertIsNone(status["remaining_percent"])
        self.assertEqual(status["snapshot_state"], "unknown")

    def test_explicit_unknown_limited_policy_allows_at_most_one_active_lease(self):
        role = self.role(provider="unknown-provider", pool_id="unknown-ok")
        first = self.manager.reserve("unknown-1", "developer", role)
        self.assertTrue(first["available"])
        self.assertIn("unknown-limited", first["reason"])
        duplicate = self.manager.reserve("unknown-1", "developer", role)
        self.assertTrue(duplicate["available"])
        second = self.manager.reserve("unknown-2", "developer", role)
        self.assertFalse(second["available"])
        self.assertIn("one unknown-limited lease", second["reason"])
        self.assertTrue(self.manager.release("unknown-1"))
        self.assertTrue(self.manager.reserve("unknown-2", "developer", role)["available"])

    def test_unknown_error_snapshot_uses_unknown_policy_without_erasing_fact(self):
        self.manager.update_snapshot("unknown-ok", snapshot(error="probe denied", windows=[]))
        role = self.role(provider="unknown-provider", pool_id="unknown-ok")
        result = self.manager.reserve("error-unknown", "qa", role)
        self.assertTrue(result["available"])
        self.assertIn("probe denied", result["reason"])
        self.assertIsNone(result["reset_at"])

    def test_probe_retry_at_is_stored_and_exposed_in_pool_status(self):
        record = snapshot(error="HTTP 429", windows=[])
        record["probe_retry_at"] = NOW + 2280
        self.manager.update_snapshot("codex", record)

        self.assertEqual(self.manager.snapshot_for_probe("codex")["probe_retry_at"], NOW + 2280)
        self.assertEqual(self.manager.status()["pools"]["codex"]["probe_retry_at"], NOW + 2280)
        # A legacy snapshot without the optional field remains valid.
        self.manager.update_snapshot("codex", snapshot())
        self.assertIsNone(self.manager.status()["pools"]["codex"]["probe_retry_at"])

    def test_probe_snapshot_read_is_detached_and_missing_snapshot_is_none(self):
        self.assertIsNone(self.manager.snapshot_for_probe("codex"))
        self.manager.update_snapshot("codex", snapshot())
        result = self.manager.snapshot_for_probe("codex")
        result["windows"][0]["name"] = "modified outside lock"
        self.assertEqual(self.manager.snapshot_for_probe("codex")["windows"][0]["name"], "weekly")
        self.assertNotIn("credentials", result)
        with self.assertRaisesRegex(QuotaError, "Unknown quota pool"):
            self.manager.snapshot_for_probe("not-configured")

    def test_probe_retry_at_must_be_a_finite_unix_timestamp(self):
        for value in (None, True, "later", float("nan"), float("inf")):
            record = snapshot(error="probe unavailable", windows=[])
            record["probe_retry_at"] = value
            with self.subTest(probe_retry_at=value), self.assertRaisesRegex(QuotaError, "probe_retry_at"):
                self.manager.update_snapshot("codex", record)

    def test_disabled_or_unauthenticated_pools_are_never_selected(self):
        config = {"pools": {
            "disabled": pool("openai-codex", enabled=False),
            "unauth": pool("claude-code", auth_ready=False),
        }}
        manager = self.new_manager(config)
        manager.update_snapshot("disabled", snapshot(20))
        manager.update_snapshot("unauth", snapshot(20))
        candidates = {"candidates": [
            {"pool": "disabled", "provider": "openai-codex", "model": "m1"},
            {"pool": "unauth", "provider": "claude-code", "model": "m2"},
        ]}
        result = manager.reserve("no-pool", "developer", candidates)
        self.assertFalse(result["available"])
        self.assertIn("disabled", result["reason"])

    def test_candidates_are_explicit_failover_and_highest_headroom_wins_within_tier(self):
        self.manager.update_snapshot("codex", snapshot(60))
        self.manager.update_snapshot("claude", snapshot(10))
        role = {"candidates": [
            {"pool": "codex", "provider": "openai-codex", "model": "codex", "tier": 0},
            {"pool": "claude", "provider": "claude-code", "model": "claude", "tier": 0},
            {"pool": "kimi", "provider": "kimi-coding", "model": "kimi", "tier": 1},
        ]}
        selected = self.manager.reserve("balance", "developer", role)
        self.assertEqual(selected["selection"]["pool"], "claude")

    def test_configured_tier_precedes_headroom_and_declaration_order_breaks_ties(self):
        self.manager.update_snapshot("codex", snapshot(90))
        self.manager.update_snapshot("claude", snapshot(10))
        role = {"candidates": [
            {"pool": "codex", "provider": "openai-codex", "model": "codex", "tier": 0},
            {"pool": "claude", "provider": "claude-code", "model": "claude", "tier": 1},
        ]}
        self.assertEqual(self.manager.reserve("tier", "qa", role)["selection"]["pool"], "codex")

        tie = {"candidates": [
            {"pool": "claude", "provider": "claude-code", "model": "claude", "tier": 0},
            {"pool": "kimi", "provider": "kimi-coding", "model": "kimi", "tier": 0},
        ]}
        self.manager.update_snapshot("claude", snapshot(20))
        self.manager.update_snapshot("kimi", snapshot(20))
        self.assertEqual(self.manager.reserve("tie", "qa", tie)["selection"]["pool"], "claude")

    def test_fresh_known_candidate_is_preferred_over_unknown_in_same_tier(self):
        self.manager.update_snapshot("codex", snapshot(30))
        role = {"candidates": [
            {"pool": "unknown-ok", "provider": "unknown-provider", "model": "unknown", "tier": 0},
            {"pool": "codex", "provider": "openai-codex", "model": "codex", "tier": 0},
        ]}
        self.assertEqual(self.manager.reserve("known", "developer", role)["selection"]["pool"], "codex")

    def test_stale_and_future_snapshots_are_unknown_not_zero(self):
        self.manager.update_snapshot("codex", snapshot(0, observed_at=NOW - 181))
        stale = self.manager.reserve("stale", "developer", self.role())
        self.assertFalse(stale["available"])
        self.assertIn("stale", stale["reason"])
        self.manager.update_snapshot("codex", snapshot(0, observed_at=NOW + 1))
        future = self.manager.reserve("future", "developer", self.role())
        self.assertFalse(future["available"])
        self.assertIn("future-dated", future["reason"])

    def test_pool_concurrency_limit_and_release_are_deterministic(self):
        self.manager.update_snapshot("codex", snapshot(20))
        role = self.role()
        self.assertTrue(self.manager.reserve("c1", "qa", role)["available"])
        self.assertTrue(self.manager.reserve("c2", "qa", role)["available"])
        third = self.manager.reserve("c3", "qa", role)
        self.assertFalse(third["available"])
        self.assertIn("max_concurrent", third["reason"])
        self.assertTrue(self.manager.release("c1"))
        self.assertFalse(self.manager.release("c1"))
        self.assertTrue(self.manager.reserve("c3", "qa", role)["available"])

    def test_snapshots_and_candidate_configuration_are_validated(self):
        for bad in (
            {"observed_at": NOW, "windows": [{"name": "week", "used_percent": 101}]},
            {"observed_at": True, "windows": []},
            {"observed_at": NOW, "windows": "not a list"},
        ):
            with self.subTest(snapshot=bad), self.assertRaises(QuotaError):
                self.manager.update_snapshot("codex", bad)
        with self.assertRaisesRegex(QuotaError, "does not match"):
            self.manager.reserve("bad-provider", "developer", {"candidates": [
                {"pool": "codex", "provider": "claude-code", "model": "misconfigured"}
            ]})
        with self.assertRaises(QuotaError):
            self.manager.reserve("empty", "developer", {"candidates": []})

    def test_missing_or_unknown_provider_has_no_implicit_fallback(self):
        result = self.manager.reserve("no-inference", "developer", self.role(provider="unconfigured"))
        self.assertFalse(result["available"])
        self.assertIn("no configured pool", result["reason"])

    def test_active_unknown_lease_survives_snapshot_change_until_explicit_release(self):
        role = self.role(provider="unknown-provider", pool_id="unknown-ok")
        self.assertTrue(self.manager.reserve("lease", "developer", role)["available"])
        self.manager.update_snapshot("unknown-ok", snapshot(10))
        second = self.manager.reserve("second-lease", "developer", role)
        self.assertTrue(second["available"])  # Snapshot is now known; concurrency allows a second task.
        self.assertEqual(self.manager.status()["pools"]["unknown-ok"]["unknown_limited_leases"], 1)
        self.assertTrue(self.manager.release("lease"))


if __name__ == "__main__":
    unittest.main()
