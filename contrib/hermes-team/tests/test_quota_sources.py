from __future__ import annotations

import sys
import tempfile
import unittest
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.quota_sources import (_probe_failure_snapshot, _snapshot_from_hermes,
                                       _bounded_call, parse_kimi_usage, refresh_snapshots)
from team_engine.quota import QuotaManager


class FakeQuotaManager:
    def __init__(self):
        self.snapshots = {}

    def update_snapshot(self, pool_id, snapshot):
        self.snapshots[pool_id] = snapshot

    def snapshot_for_probe(self, pool_id):
        return self.snapshots.get(pool_id)


class UsageHTTPError(Exception):
    def __init__(self, status, retry_after=None):
        super().__init__("secret-token-and-response-body must not persist")
        self.response = SimpleNamespace(status_code=status,
                                        headers={"retry-after": retry_after})


class ProviderUsageFailureTests(unittest.TestCase):
    def test_anthropic_probe_preserves_rate_limit_error_hidden_by_public_helper(self):
        def raw_probe():
            raise UsageHTTPError(429, "2280")

        api = SimpleNamespace(_fetch_anthropic_account_usage=raw_probe,
                              fetch_account_usage=lambda provider: None)
        with self.assertRaises(UsageHTTPError):
            _snapshot_from_hermes("anthropic", api)

    def test_rate_limit_records_retry_after_without_sensitive_error_text(self):
        with patch("team_engine.quota_sources.time.time", return_value=1000):
            snapshot = _probe_failure_snapshot(UsageHTTPError(429, "2280"), "anthropic")
        self.assertEqual(snapshot["probe_retry_at"], 3280)
        self.assertEqual(snapshot["windows"], [])
        self.assertIn("HTTP 429", snapshot["error"])
        self.assertNotIn("secret-token", repr(snapshot))
        self.assertNotIn("response-body", repr(snapshot))

    def test_http_date_retry_after_and_missing_header_cooldown(self):
        with patch("team_engine.quota_sources.time.time", return_value=1000):
            dated = _probe_failure_snapshot(
                UsageHTTPError(503, "Thu, 01 Jan 1970 01:00:00 GMT"), "anthropic")
            missing = _probe_failure_snapshot(UsageHTTPError(429), "anthropic")
        self.assertEqual(dated["probe_retry_at"], 3600)
        self.assertEqual(missing["probe_retry_at"], 1300)

    def test_unauthorized_does_not_claim_subscription_is_exhausted(self):
        snapshot = _probe_failure_snapshot(UsageHTTPError(401), "anthropic")
        self.assertIn("HTTP 401", snapshot["error"])
        self.assertNotIn("probe_retry_at", snapshot)
        self.assertNotIn("secret-token", repr(snapshot))

    def test_invalid_retry_after_uses_bounded_default_without_crashing(self):
        with patch("team_engine.quota_sources.time.time", return_value=1000):
            for value in ("not-a-date", "-10", "1e308", "nan"):
                with self.subTest(value=value):
                    snapshot = _probe_failure_snapshot(UsageHTTPError(429, value), "anthropic")
                    self.assertEqual(snapshot["probe_retry_at"], 1300)

    def test_successful_native_anthropic_probe_retains_real_quota_windows(self):
        observed = datetime(2026, 10, 28, tzinfo=timezone.utc)
        api = SimpleNamespace(_fetch_anthropic_account_usage=lambda: SimpleNamespace(
            fetched_at=observed, source="oauth_usage_api",
            windows=[SimpleNamespace(label="Current week", used_percent=42, reset_at=None)]))
        snapshot = _snapshot_from_hermes("anthropic", api)
        self.assertIsNone(snapshot["error"])
        self.assertEqual(snapshot["windows"][0]["used_percent"], 42)

    def _anthropic_modules(self, pool, env_values=None):
        agent = ModuleType("agent")
        adapter = ModuleType("agent.anthropic_adapter")
        adapter._getenv = lambda name, default="": (env_values or {}).get(name, default)
        credential_pool = ModuleType("agent.credential_pool")
        credential_pool.AUTH_TYPE_OAUTH = "oauth"
        credential_pool.load_pool = lambda provider: pool
        agent.anthropic_adapter = adapter
        return {"agent": agent, "agent.anthropic_adapter": adapter,
                "agent.credential_pool": credential_pool}

    def test_anthropic_probe_refreshes_only_exact_expired_pool_entry(self):
        now = 1_800_000_000
        old = SimpleNamespace(id="entry-7", auth_type="oauth", access_token="old-access",
                              refresh_token="old-refresh", expires_at_ms=now * 1000 - 1)
        fresh = SimpleNamespace(id="entry-7", auth_type="oauth", access_token="new-access",
                                refresh_token="rotated-refresh", expires_at_ms=(now + 3600) * 1000)

        class Pool:
            def entries(self):
                return [old]

            def try_refresh_matching(self, **kwargs):
                self.refresh_kwargs = kwargs
                return fresh

        pool = Pool()
        resolved = iter(("old-access", "new-access"))
        api = SimpleNamespace(
            resolve_anthropic_token=lambda: next(resolved),
            _fetch_anthropic_account_usage=lambda: SimpleNamespace(
                fetched_at=datetime.fromtimestamp(now, timezone.utc), source="oauth_usage_api",
                windows=[SimpleNamespace(label="week", used_percent=40, reset_at=None)]),
        )
        modules = self._anthropic_modules(pool)
        with patch("team_engine.quota_sources.time.time", return_value=now), patch.dict(
                sys.modules, modules):
            snapshot = _snapshot_from_hermes("anthropic", api)

        self.assertEqual(pool.refresh_kwargs, {
            "api_key_hint": "old-access", "credential_id": "entry-7"})
        self.assertEqual(snapshot["windows"][0]["used_percent"], 40)

    def test_anthropic_probe_never_refreshes_any_static_env_override(self):
        now = 1_800_000_000

        class Pool:
            def entries(self):
                raise AssertionError("env override must avoid inspecting or refreshing the pool")

        api = SimpleNamespace(resolve_anthropic_token=lambda: "static-token",
                              _fetch_anthropic_account_usage=lambda: SimpleNamespace(
                                  fetched_at=datetime.fromtimestamp(now, timezone.utc),
                                  windows=[SimpleNamespace(label="week", used_percent=10,
                                                           reset_at=None)]))
        for name in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
            with self.subTest(name=name):
                modules = self._anthropic_modules(Pool(), {name: "static-token"})
                with patch("team_engine.quota_sources.time.time", return_value=now), patch.dict(
                        sys.modules, modules):
                    snapshot = _snapshot_from_hermes("anthropic", api)
                self.assertEqual(snapshot["windows"][0]["used_percent"], 10)

    def test_anthropic_probe_does_not_refresh_fresh_oauth_pool_entry(self):
        now = 1_800_000_000
        fresh = SimpleNamespace(id="entry-fresh", auth_type="oauth", access_token="access",
                                refresh_token="refresh", expires_at_ms=(now + 3600) * 1000)

        class Pool:
            def entries(self):
                return [fresh]

            def try_refresh_matching(self, **kwargs):
                raise AssertionError("a fresh OAuth credential must not be refreshed")

        api = SimpleNamespace(resolve_anthropic_token=lambda: "access",
                              _fetch_anthropic_account_usage=lambda: SimpleNamespace(
                                  fetched_at=datetime.fromtimestamp(now, timezone.utc),
                                  windows=[SimpleNamespace(label="week", used_percent=10,
                                                           reset_at=None)]))
        with patch("team_engine.quota_sources.time.time", return_value=now), patch.dict(
                sys.modules, self._anthropic_modules(Pool())):
            snapshot = _snapshot_from_hermes("anthropic", api)
        self.assertEqual(snapshot["windows"][0]["used_percent"], 10)

    def test_anthropic_probe_requires_one_exact_oauth_token_match(self):
        now = 1_800_000_000
        expired = SimpleNamespace(id="entry", auth_type="oauth", access_token="other-token",
                                  refresh_token="refresh", expires_at_ms=now * 1000 - 1)
        cases = {
            "token mismatch": [expired],
            "duplicate match": [expired, SimpleNamespace(**expired.__dict__)],
        }
        for label, entries in cases.items():
            with self.subTest(label=label):
                # The duplicate case must contain two matching access tokens.
                if label == "duplicate match":
                    entries[0].access_token = "selected-token"
                    entries[1].access_token = "selected-token"

                class Pool:
                    def entries(self):
                        return entries

                    def try_refresh_matching(self, **kwargs):
                        raise AssertionError("ambiguous credentials must never be refreshed")

                api = SimpleNamespace(resolve_anthropic_token=lambda: "selected-token",
                                      _fetch_anthropic_account_usage=lambda: SimpleNamespace(
                                          fetched_at=datetime.fromtimestamp(now, timezone.utc),
                                          windows=[SimpleNamespace(label="week", used_percent=10,
                                                                   reset_at=None)]))
                with patch("team_engine.quota_sources.time.time", return_value=now), patch.dict(
                        sys.modules, self._anthropic_modules(Pool())):
                    snapshot = _snapshot_from_hermes("anthropic", api)
                self.assertEqual(snapshot["windows"][0]["used_percent"], 10)

    def test_anthropic_probe_does_not_refresh_matching_api_key_credential(self):
        now = 1_800_000_000
        api_key = SimpleNamespace(id="entry-api", auth_type="api_key", access_token="selected-token",
                                  refresh_token="refresh", expires_at_ms=now * 1000 - 1)

        class Pool:
            def entries(self):
                return [api_key]

            def try_refresh_matching(self, **kwargs):
                raise AssertionError("API-key credentials are not refreshable OAuth entries")

        api = SimpleNamespace(resolve_anthropic_token=lambda: "selected-token",
                              _fetch_anthropic_account_usage=lambda: SimpleNamespace(
                                  fetched_at=datetime.fromtimestamp(now, timezone.utc),
                                  windows=[SimpleNamespace(label="week", used_percent=10,
                                                           reset_at=None)]))
        with patch("team_engine.quota_sources.time.time", return_value=now), patch.dict(
                sys.modules, self._anthropic_modules(Pool())):
            snapshot = _snapshot_from_hermes("anthropic", api)
        self.assertEqual(snapshot["windows"][0]["used_percent"], 10)

    def test_anthropic_probe_rejects_non_finite_pool_expiry(self):
        now = 1_800_000_000
        for expiry in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(expiry=expiry):
                expired = SimpleNamespace(id="entry", auth_type="oauth", access_token="access",
                                          refresh_token="refresh", expires_at_ms=expiry)

                class Pool:
                    def entries(self):
                        return [expired]

                    def try_refresh_matching(self, **kwargs):
                        raise AssertionError("invalid expiry must not trigger refresh")

                api = SimpleNamespace(resolve_anthropic_token=lambda: "access",
                                      _fetch_anthropic_account_usage=lambda: SimpleNamespace(
                                          fetched_at=datetime.fromtimestamp(now, timezone.utc),
                                          windows=[SimpleNamespace(label="week", used_percent=10,
                                                                   reset_at=None)]))
                with patch("team_engine.quota_sources.time.time", return_value=now), patch.dict(
                        sys.modules, self._anthropic_modules(Pool())):
                    snapshot = _snapshot_from_hermes("anthropic", api)
                self.assertEqual(snapshot["windows"][0]["used_percent"], 10)

    def test_anthropic_probe_fails_closed_when_refresh_result_is_not_same_credential(self):
        now = 1_800_000_000
        expired = SimpleNamespace(id="entry-1", auth_type="oauth", access_token="old-access",
                                  refresh_token="refresh", expires_at_ms=now * 1000 - 1)
        cases = {
            "missing result": None,
            "different credential": SimpleNamespace(id="entry-2", access_token="new-access"),
        }
        for label, refreshed in cases.items():
            with self.subTest(label=label):

                class Pool:
                    def entries(self):
                        return [expired]

                    def try_refresh_matching(self, **kwargs):
                        return refreshed

                api = SimpleNamespace(resolve_anthropic_token=lambda: "old-access",
                                      _fetch_anthropic_account_usage=lambda: self.fail(
                                          "usage endpoint must not run after invalid refresh result"))
                with patch("team_engine.quota_sources.time.time", return_value=now), patch.dict(
                        sys.modules, self._anthropic_modules(Pool())):
                    with self.assertRaisesRegex(RuntimeError, "no matching credential"):
                        _snapshot_from_hermes("anthropic", api)

    def test_anthropic_probe_refresh_failure_stays_unknown_and_redacted(self):
        now = 1_800_000_000
        stale = SimpleNamespace(id="entry-9", auth_type="oauth", access_token="expired-access",
                                refresh_token="refresh", expires_at_ms=now * 1000 - 1000)

        class Pool:
            def entries(self):
                return [stale]

            def try_refresh_matching(self, **kwargs):
                raise RuntimeError("secret refresh response")

        api = SimpleNamespace(resolve_anthropic_token=lambda: "expired-access",
                              _fetch_anthropic_account_usage=lambda: self.fail(
                                  "stale token must not be sent after failed refresh"))
        modules = self._anthropic_modules(Pool())
        with patch("team_engine.quota_sources.time.time", return_value=now), patch.dict(
                sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, "refresh failed") as error:
                _snapshot_from_hermes("anthropic", api)
        snapshot = _probe_failure_snapshot(error.exception, "anthropic")
        self.assertEqual(snapshot["windows"], [])
        self.assertIsNotNone(snapshot["error"])
        self.assertNotIn("secret refresh response", repr(snapshot))

    def test_bounded_probe_preserves_contextvars_in_worker_thread(self):
        scope = ContextVar("test_scope")
        token = scope.set("profile-secret-scope")
        self.addCleanup(scope.reset, token)
        self.assertEqual(_bounded_call(scope.get), "profile-secret-scope")

    def test_malformed_window_cannot_be_hidden_by_another_healthy_window(self):
        for value in (None, "NaN", -1, 101):
            with self.subTest(value=value):
                api = SimpleNamespace(_fetch_anthropic_account_usage=lambda: SimpleNamespace(
                    windows=[SimpleNamespace(label="Session", used_percent=10, reset_at=None),
                             SimpleNamespace(label="Week", used_percent=value, reset_at=None)]))
                with self.assertRaises(RuntimeError):
                    _snapshot_from_hermes("anthropic", api)


class KimiUsageParserTests(unittest.TestCase):
    def test_parses_verified_string_numeric_fields_and_each_reset(self):
        payload = {
            "usage": {
                "limit": "100", "used": "58", "remaining": "42",
                "resetTime": "2026-10-31T11:18:20.104999Z",
            },
            "limits": [{
                "detail": {
                    "limit": "100", "used": "44", "remaining": "56",
                    "resetTime": "2026-10-27T00:18:20.104999Z",
                },
                "window": {"timeUnit": "TIME_UNIT_MINUTE"},
            }],
            "usages": [],
        }

        snapshot = parse_kimi_usage(payload, observed_at=1_793_048_061.5)

        self.assertEqual(snapshot["source"], "kimi-coding-usages")
        self.assertEqual(snapshot["observed_at"], 1_793_048_061.5)
        self.assertIsNone(snapshot["error"])
        self.assertAlmostEqual(snapshot["windows"][0]["used_percent"], 58.0)
        self.assertAlmostEqual(snapshot["windows"][1]["used_percent"], 44.0)
        self.assertEqual(snapshot["windows"][0]["reset_at"], 1_793_445_500.104999)
        self.assertEqual(snapshot["windows"][1]["reset_at"], 1_793_060_300.104999)

    def test_uses_remaining_when_used_is_omitted_and_rejects_unknown_shape(self):
        snapshot = parse_kimi_usage({"usage": {"limit": 100, "remaining": 80}}, observed_at=100)
        self.assertEqual(snapshot["windows"][0]["used_percent"], 20)
        self.assertIsNone(snapshot["windows"][0]["reset_at"])

        unknown = parse_kimi_usage({"totalQuota": {"limit": 100, "remaining": 99}}, observed_at=100)
        self.assertEqual(unknown["windows"], [])
        self.assertEqual(unknown["error"], "Kimi usage response has no recognised windows")

    def test_rejects_zero_or_missing_limit_without_inventing_quota(self):
        snapshot = parse_kimi_usage({"usage": {"limit": 0, "used": 0}}, observed_at=100)
        self.assertEqual(snapshot["windows"], [])
        self.assertIsNotNone(snapshot["error"])

    def test_malformed_known_limit_fails_closed_even_when_another_window_is_valid(self):
        snapshot = parse_kimi_usage({
            "usage": {"limit": 100, "used": 10, "remaining": 90},
            "limits": [
                {"detail": {"limit": 100, "used": 20, "remaining": 80}},
                {"detail": {"limit": 100, "used": "NaN", "remaining": 90}},
            ],
        }, observed_at=100)
        self.assertEqual(snapshot["windows"], [])
        self.assertEqual(snapshot["error"], "Kimi usage response contains a malformed quota window")

    def test_inconsistent_used_and_remaining_fail_closed(self):
        snapshot = parse_kimi_usage({
            "usage": {"limit": 100, "used": 20, "remaining": 90},
        }, observed_at=100)
        self.assertEqual(snapshot["windows"], [])
        self.assertIsNotNone(snapshot["error"])

    def test_negative_or_over_limit_remaining_never_becomes_available_quota(self):
        for remaining in (-1, 101, "-0.5", "NaN"):
            with self.subTest(remaining=remaining):
                snapshot = parse_kimi_usage({
                    "usage": {"limit": 100, "remaining": remaining},
                }, observed_at=100)
                self.assertEqual(snapshot["windows"], [])
                self.assertIsNotNone(snapshot["error"])

    def test_malformed_limits_collection_or_detail_fails_closed(self):
        malformed_payloads = (
            {"limits": {"detail": {"limit": 100, "remaining": 50}}},
            {"limits": [{"detail": {"limit": 100, "remaining": 50}}, None]},
            {"limits": [{"detail": None}]},
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                snapshot = parse_kimi_usage(payload, observed_at=100)
                self.assertEqual(snapshot["windows"], [])
                self.assertIsNotNone(snapshot["error"])


class RefreshQuotaSnapshotsTests(unittest.TestCase):
    def test_successful_anthropic_probe_is_cached_without_renewing_observation(self):
        config = {"pools": {"claude": {
            "provider": "anthropic", "enabled": True, "auth_ready": True,
            "max_snapshot_age": 600,
        }}}
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "quota.json"
            manager = QuotaManager(config, ledger, clock=lambda: 1000)
            manager.update_snapshot("claude", {
                "source": "real-probe", "observed_at": 1000,
                "windows": [{"name": "weekly", "used_percent": 25}], "error": None,
            })
            restarted = QuotaManager(config, ledger, clock=lambda: 1100)
            with patch("team_engine.quota_sources.time.time", return_value=1100), patch(
                    "team_engine.quota_sources._bounded_call") as probe:
                records = refresh_snapshots(restarted, config, "/tmp/hermes")
            probe.assert_not_called()
            self.assertTrue(records[0]["cached"])
            self.assertEqual(records[0]["status"], "available")
            self.assertEqual(records[0]["next_probe_at"], 1300)
            self.assertEqual(restarted.snapshot_for_probe("claude")["observed_at"], 1000)
            self.assertEqual(restarted.status()["pools"]["claude"]["remaining_percent"], 75)

    def test_cached_snapshot_still_expires_under_stricter_freshness_policy(self):
        config = {"pools": {"claude": {
            "provider": "anthropic", "enabled": True, "auth_ready": True,
            "max_snapshot_age": 180,
        }}}
        with tempfile.TemporaryDirectory() as directory:
            manager = QuotaManager(config, Path(directory) / "quota.json", clock=lambda: 1200)
            manager.update_snapshot("claude", {
                "source": "real-probe", "observed_at": 1000,
                "windows": [{"name": "weekly", "used_percent": 25}], "error": None,
            })
            with patch("team_engine.quota_sources.time.time", return_value=1200), patch(
                    "team_engine.quota_sources._bounded_call") as probe:
                records = refresh_snapshots(manager, config, "/tmp/hermes")
            probe.assert_not_called()
            self.assertEqual(records[0]["status"], "unknown")
            self.assertEqual(manager.status()["pools"]["claude"]["snapshot_state"], "unknown")

    def test_anthropic_generic_error_uses_same_cadence_then_retries(self):
        manager = FakeQuotaManager()
        config = {"pools": {"claude": {
            "provider": "claude-code", "enabled": True, "auth_ready": True,
        }}}
        with patch("team_engine.quota_sources.time.time", return_value=1000), patch(
                "team_engine.quota_sources._bounded_call", side_effect=RuntimeError("secret")):
            refresh_snapshots(manager, config, "/tmp/hermes")
        with patch("team_engine.quota_sources.time.time", return_value=1299), patch(
                "team_engine.quota_sources._bounded_call") as probe:
            refresh_snapshots(manager, config, "/tmp/hermes")
        probe.assert_not_called()
        healthy = {"source": "real-probe", "observed_at": 1300,
                   "windows": [{"name": "weekly", "used_percent": 25}], "error": None}
        with patch("team_engine.quota_sources.time.time", return_value=1300), patch(
                "team_engine.quota_sources._bounded_call", return_value=healthy) as probe:
            records = refresh_snapshots(manager, config, "/tmp/hermes")
        probe.assert_called_once()
        self.assertEqual(records[0]["status"], "available")

    def test_other_provider_is_not_subject_to_anthropic_cadence(self):
        manager = FakeQuotaManager()
        config = {"pools": {"codex": {
            "provider": "openai-codex", "enabled": True, "auth_ready": True,
        }}}
        healthy = {"source": "real-probe", "observed_at": 1000,
                   "windows": [{"name": "weekly", "used_percent": 25}], "error": None}
        manager.update_snapshot("codex", healthy)
        with patch("team_engine.quota_sources.time.time", return_value=1100), patch(
                "team_engine.quota_sources._bounded_call", return_value=healthy) as probe:
            refresh_snapshots(manager, config, "/tmp/hermes")
        probe.assert_called_once()

    def test_rate_limit_cooldown_persists_across_quota_manager_restart(self):
        config = {"pools": {"claude": {
            "provider": "anthropic", "enabled": True, "auth_ready": True,
            "unknown_policy": "wait",
        }}}
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "quota.json"
            manager = QuotaManager(config, ledger, clock=lambda: 1000)
            with patch("team_engine.quota_sources.time.time", return_value=1000), patch(
                    "team_engine.quota_sources._bounded_call", side_effect=UsageHTTPError(429, "2280")):
                refresh_snapshots(manager, config, "/tmp/hermes")
            restarted = QuotaManager(config, ledger, clock=lambda: 1100)
            with patch("team_engine.quota_sources.time.time", return_value=1100), patch(
                    "team_engine.quota_sources._bounded_call") as probe:
                records = refresh_snapshots(restarted, config, "/tmp/hermes")
            probe.assert_not_called()
            self.assertEqual(records[0]["probe_retry_at"], 3280)
            self.assertEqual(restarted.status()["pools"]["claude"]["snapshot_state"], "unknown")

    def test_http429_cooldown_survives_repeated_refresh_and_retries_after_deadline(self):
        manager = FakeQuotaManager()
        config = {"pools": {"claude": {
            "provider": "anthropic", "enabled": True, "auth_ready": True,
        }}}
        with patch("team_engine.quota_sources.time.time", return_value=1000), patch(
                "team_engine.quota_sources._bounded_call", side_effect=UsageHTTPError(429, "2280")):
            initial = refresh_snapshots(manager, config, "/tmp/hermes")
        stored = dict(manager.snapshots["claude"])
        with patch("team_engine.quota_sources.time.time", return_value=1100), patch(
                "team_engine.quota_sources._bounded_call") as probe:
            cached = refresh_snapshots(manager, config, "/tmp/hermes")
        probe.assert_not_called()
        self.assertEqual(initial[0]["status"], "unknown")
        self.assertEqual(cached[0]["probe_retry_at"], 3280)
        self.assertEqual(manager.snapshots["claude"], stored)
        self.assertNotIn("secret-token", repr(cached))

        healthy = {"source": "hermes-account-usage:oauth_usage_api", "observed_at": 3280,
                   "windows": [{"name": "weekly", "used_percent": 25, "reset_at": 4000}],
                   "error": None}
        with patch("team_engine.quota_sources.time.time", return_value=3280), patch(
                "team_engine.quota_sources._bounded_call", return_value=healthy) as probe:
            recovered = refresh_snapshots(manager, config, "/tmp/hermes")
        probe.assert_called_once()
        self.assertEqual(recovered[0]["status"], "available")
        self.assertNotIn("probe_retry_at", manager.snapshots["claude"])

    def test_refresh_updates_enabled_pools_and_never_contacts_disabled_pool(self):
        manager = FakeQuotaManager()
        config = {"quota": {"pools": {
            "codex": {"provider": "openai-codex", "enabled": True, "auth_ready": True},
            "off": {"provider": "openai-codex", "enabled": False, "auth_ready": True},
        }}}
        data = {"source": "hermes-account-usage:usage_api", "observed_at": 1234,
                "windows": [{"name": "weekly", "used_percent": 25, "reset_at": 2000}],
                "error": None}
        with patch("team_engine.quota_sources._bounded_call", return_value=data) as probe:
            records = refresh_snapshots(manager, config, "/tmp/hermes")

        probe.assert_called_once()
        self.assertEqual(manager.snapshots["codex"], data)
        self.assertNotIn("off", manager.snapshots)
        self.assertEqual(records[0]["status"], "available")
        self.assertEqual(records[1]["status"], "skipped")

    def test_probe_failure_replaces_old_snapshot_with_unknown_safe_error(self):
        manager = FakeQuotaManager()
        config = {"pools": {"claude": {
            "provider": "claude-code", "enabled": True, "auth_ready": True,
        }}}
        # A raw exception message may contain an endpoint, header, or body.
        with patch("team_engine.quota_sources._bounded_call", side_effect=RuntimeError("secret-value must not persist")):
            records = refresh_snapshots(manager, config, "/tmp/hermes")

        snapshot = manager.snapshots["claude"]
        self.assertEqual(snapshot["windows"], [])
        self.assertNotIn("secret-value", snapshot["error"])
        self.assertNotIn("secret-value", repr(records))

    def test_unsupported_provider_is_written_as_unknown(self):
        manager = FakeQuotaManager()
        config = {"pools": {"other": {
            "provider": "arbitrary", "enabled": True, "auth_ready": True,
        }}}
        records = refresh_snapshots(manager, config, "/tmp/hermes")
        self.assertEqual(manager.snapshots["other"]["windows"], [])
        self.assertEqual(records[0]["status"], "unknown")

    def test_missing_auth_marks_pool_unknown_without_probing(self):
        manager = FakeQuotaManager()
        config = {"pools": {"kimi": {
            "provider": "kimi-coding", "enabled": True, "auth_ready": False,
        }}}
        with patch("team_engine.quota_sources._bounded_call") as probe:
            records = refresh_snapshots(manager, config, "/tmp/hermes")
        probe.assert_not_called()
        self.assertEqual(records[0]["status"], "unknown")
        self.assertEqual(manager.snapshots["kimi"]["windows"], [])

    def test_duplicate_provider_pools_do_not_reuse_one_account_snapshot(self):
        manager = FakeQuotaManager()
        config = {"pools": {
            "claude-a": {"provider": "anthropic", "enabled": True, "auth_ready": True},
            "claude-b": {"provider": "claude-code", "enabled": True, "auth_ready": True},
        }}
        with patch("team_engine.quota_sources._bounded_call") as probe:
            records = refresh_snapshots(manager, config, "/tmp/hermes")
        probe.assert_not_called()
        self.assertTrue(all(record["status"] == "unknown" for record in records))
        self.assertTrue(all(not manager.snapshots[pool]["windows"] for pool in manager.snapshots))
        self.assertTrue(all("multiple pools" in manager.snapshots[pool]["error"]
                            for pool in manager.snapshots))


if __name__ == "__main__":
    unittest.main()
