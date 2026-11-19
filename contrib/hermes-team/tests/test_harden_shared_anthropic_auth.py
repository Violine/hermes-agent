from __future__ import annotations

import ast
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from typing import Any, Dict, Iterable, List, Optional


SCRIPT = Path(__file__).parents[1] / "scripts" / "harden_shared_anthropic_auth.py"
spec = importlib.util.spec_from_file_location("harden_shared_anthropic_auth", SCRIPT)
assert spec and spec.loader
hardener = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hardener
spec.loader.exec_module(hardener)


AUTH_SOURCE = '''
from typing import Any, Dict, Iterable, List, Optional
from pathlib import Path

def _auth_file_path() -> Path:
    return active_path

def read_credential_pool(provider_id: Optional[str] = None) -> Dict[str, Any]:
    """Return the persisted credential pool, or one provider slice.

    In profile mode, the profile's credential pool is authoritative. If a
    provider has no entries in the profile, entries from the global-root
    ``auth.json`` are used as a read-only fallback — so workers spawned in a
    profile can see providers that were only authenticated at global scope.

    Profile entries always win: the global fallback only applies per-provider
    when the profile has zero entries for that provider. Once the user runs
    ``hermes auth add <provider>`` inside the profile, profile entries
    fully shadow global for that provider on the next read.

    Writes always go to the profile (``write_credential_pool`` is unchanged).
    See issue #18594 follow-up.
    """
    auth_store = _load_auth_store()
    pool = auth_store.get("credential_pool")
    if not isinstance(pool, dict):
        pool = {}

    global_pool: Dict[str, Any] = {}
    global_store = _load_global_auth_store()
    maybe_global_pool = global_store.get("credential_pool") if global_store else None
    if isinstance(maybe_global_pool, dict):
        global_pool = maybe_global_pool

    if provider_id is None:
        merged = dict(pool)
        for gp_key, gp_entries in global_pool.items():
            if not isinstance(gp_entries, list) or not gp_entries:
                continue
            # Per-provider shadowing: profile wins whenever it has ANY entries.
            existing = merged.get(gp_key)
            if isinstance(existing, list) and existing:
                continue
            merged[gp_key] = list(gp_entries)
        return merged

    provider_entries = pool.get(provider_id)
    if isinstance(provider_entries, list) and provider_entries:
        return list(provider_entries)
    # Profile has no entries for this provider — fall back to global.
    global_entries = global_pool.get(provider_id)
    return list(global_entries) if isinstance(global_entries, list) else []

def write_credential_pool(
    provider_id: str,
    entries: List[Dict[str, Any]],
    *,
    removed_ids: Optional[Iterable[str]] = None,
) -> Path:
    removed = {rid for rid in (removed_ids or ()) if rid}
    with _auth_store_lock():
        auth_store = _load_auth_store()
        pool = auth_store.get("credential_pool")
        if not isinstance(pool, dict):
            pool = {}
            auth_store["credential_pool"] = pool
        sanitized_entries = [
            sanitize_borrowed_credential_payload(entry, provider_id)
            if isinstance(entry, dict) else entry
            for entry in entries
        ]
        existing = pool.get(provider_id)
        existing_list = existing if isinstance(existing, list) else []
        existing_by_id = {entry.get("id"): entry for entry in existing_list if isinstance(entry, dict) and entry.get("id")}
        merged = list(sanitized_entries)
        pool[provider_id] = merged
        return _save_auth_store(auth_store)
'''

POOL_SOURCE = '''
from hermes_cli.auth import (
    _auth_store_lock,
)

class CredentialPool:
    def _single_use_refresh_lock_timeout(self):
        return 30
    def _refresh_entry(self, entry, *, force):
        if self.provider in ("openai-codex", "xai-oauth", "anthropic"):
            sync_entry = self._sync_anthropic_entry_from_pool_store
            with _auth_store_lock(
                timeout_seconds=self._single_use_refresh_lock_timeout()
            ):
                synced = sync_entry(entry)
                if self.provider == "anthropic" and synced.source == "claude_code":
                    return self._refresh_entry_impl(synced, force=force)
                if synced is not entry:
                    entry = synced
                    if not force and not self._entry_needs_refresh(entry):
                        return entry
                return self._refresh_entry_impl(entry, force=force)
'''


def _functions(source: str, names: set[str], namespace: dict[str, Any]) -> dict[str, Any]:
    tree = ast.parse(source)
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    code = compile(ast.Module(body=selected, type_ignores=[]), "<patched Hermes functions>", "exec")
    exec(code, namespace)
    return namespace


class Harness:
    def __init__(self, root: Path, profile="team-planner", stores=None):
        self.root = root.resolve()
        self.active_path = self.root / "profiles" / profile / "auth.json"
        self.profile_home = self.active_path.parent
        self.global_path = self.root / "auth.json"
        self.active_path.parent.mkdir(parents=True, exist_ok=True)
        (self.active_path.parent / ".hermes-team-managed.json").write_text("{}")
        self.global_path.parent.mkdir(parents=True, exist_ok=True)
        self.stores = stores if stores is not None else {
            self.active_path: {"credential_pool": {
                "anthropic": [{"id": "stale"}],
                "kimi": [{"id": "profile-kimi"}],
                "openai-codex": [{"id": "profile-codex"}],
                "custom": [{"id": "profile-custom"}],
            }},
            self.global_path: {"credential_pool": {"anthropic": [{"id": "fresh"}], "kimi": [{"id": "root-kimi"}], "openai-codex": [{"id": "codex"}]}},
        }
        self.lock_targets: list[Path | None] = []
        self.saved: list[tuple[Path | None, dict[str, Any]]] = []

    @contextmanager
    def lock(self, target_path=None, **kwargs):
        self.lock_targets.append(target_path)
        yield

    def namespace(self):
        root = self.root
        harness = self
        constants = types.ModuleType("hermes_constants")
        constants.get_default_hermes_root = lambda: root
        sys.modules["hermes_constants"] = constants
        return {
            "Path": Path,
            "Any": Any,
            "Dict": Dict,
            "Iterable": Iterable,
            "List": List,
            "Optional": Optional,
            "_auth_file_path": lambda: harness.active_path,
            "get_hermes_home": lambda: harness.profile_home,
            "_global_auth_file_path": lambda: harness.global_path,
            "_load_auth_store": lambda path=None: harness.stores.get(path or harness.active_path, {}),
            "_load_global_auth_store": lambda: harness.stores.get(harness.global_path, {}),
            "_auth_store_lock": harness.lock,
            "_save_auth_store": harness.save,
            "sanitize_borrowed_credential_payload": lambda item, provider: dict(item),
            "_merge_disk_cooldown_state": lambda item, disk, provider: item,
            "time": types.SimpleNamespace(time=lambda: 0),
        }

    def save(self, store, target_path=None):
        self.stores[target_path or self.active_path] = store
        self.saved.append((target_path, store))
        return target_path or self.active_path


class SharedAnthropicAuthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = (Path(self.tempdir.name) / "hermes").resolve()
        self.had_hermes_constants = "hermes_constants" in sys.modules
        self.old_hermes_constants = sys.modules.get("hermes_constants")
        self.addCleanup(self._restore_hermes_constants)

    def _restore_hermes_constants(self):
        if self.had_hermes_constants:
            sys.modules["hermes_constants"] = self.old_hermes_constants
        else:
            sys.modules.pop("hermes_constants", None)

    def patched_harness(self, root=None, profile="team-planner", stores=None):
        harness = Harness(root or self.root, profile, stores=stores)
        auth, pool = hardener.patched_sources(AUTH_SOURCE, POOL_SOURCE)
        namespace = harness.namespace()
        _functions(auth, {"_credential_pool_auth_file_path", "read_credential_pool", "write_credential_pool"}, namespace)
        method_node = next(
            node for node in ast.walk(ast.parse(pool))
            if isinstance(node, ast.FunctionDef) and node.name == "_refresh_entry"
        )
        method_namespace = dict(namespace)
        exec(compile(ast.Module(body=[method_node], type_ignores=[]), "<patched refresh>", "exec"), method_namespace)
        harness.refresh_method = method_namespace["_refresh_entry"]
        return harness, auth, pool, namespace

    def test_managed_team_anthropic_reads_canonical_and_provider_list_is_consistent(self):
        harness, auth, _, ns = self.patched_harness()
        self.assertEqual(ns["read_credential_pool"]("anthropic"), [{"id": "fresh"}])
        all_pools = ns["read_credential_pool"]()
        self.assertEqual(all_pools["anthropic"], [{"id": "fresh"}])
        self.assertEqual(all_pools["kimi"], [{"id": "profile-kimi"}])
        self.assertEqual(ns["_credential_pool_auth_file_path"]("anthropic"), harness.global_path)
        self.assertIn(hardener.HELPER_MARKER, auth)

    def test_managed_team_write_targets_canonical_store_and_preserves_other_rows(self):
        harness, _, pool, ns = self.patched_harness()
        result = ns["write_credential_pool"]("anthropic", [{"id": "rotated", "access_token": "new"}])
        self.assertEqual(result, harness.global_path)
        self.assertEqual(harness.lock_targets, [harness.global_path])
        self.assertEqual(harness.saved[-1][0], harness.global_path)
        root_pool = harness.stores[harness.global_path]["credential_pool"]
        self.assertEqual(root_pool["anthropic"], [{"id": "rotated", "access_token": "new"}])
        self.assertEqual(root_pool["kimi"], [{"id": "root-kimi"}])
        self.assertEqual(harness.stores[harness.active_path]["credential_pool"]["anthropic"], [{"id": "stale"}])
        self.assertIn("target_path=_credential_pool_auth_file_path(self.provider)", pool)

    def test_fresh_rotation_from_profile_a_is_adopted_by_profile_b_without_forking(self):
        # Both profiles begin with the same credential ID and stale token pair.
        stale = [{"id": "same-oauth-row", "access_token": "old-a", "refresh_token": "old-r"}]
        stores = {
            self.root / "profiles" / "team-planner" / "auth.json": {"credential_pool": {"anthropic": list(stale), "kimi": [{"id": "planner-kimi"}]}},
            self.root / "profiles" / "team-skeptic" / "auth.json": {"credential_pool": {"anthropic": list(stale), "kimi": [{"id": "skeptic-kimi"}]}},
            self.root / "auth.json": {"credential_pool": {"anthropic": list(stale), "kimi": [{"id": "root-kimi"}]}},
        }
        profile_a, _, _, ns_a = self.patched_harness(profile="team-planner", stores=stores)
        profile_b, _, _, ns_b = self.patched_harness(profile="team-skeptic", stores=stores)
        ns_a["write_credential_pool"]("anthropic", [{"id": "same-oauth-row", "access_token": "fresh-a", "refresh_token": "fresh-r"}])
        self.assertEqual(ns_a["read_credential_pool"]("anthropic"), stores[self.root / "auth.json"]["credential_pool"]["anthropic"])

        from types import SimpleNamespace
        class PoolInstance:
            provider = "anthropic"
            refresh_calls = []
            def _single_use_refresh_lock_timeout(self):
                return 30
            def _sync_anthropic_entry_from_pool_store(self, entry):
                return SimpleNamespace(source="manual:hermes_pkce", **ns_b["read_credential_pool"]("anthropic")[0])
            def _entry_needs_refresh(self, entry):
                return False
            def _refresh_entry_impl(self, entry, *, force):
                self.refresh_calls.append(entry)
                raise AssertionError("fresh canonical credential must be adopted without a token refresh")

        instance = PoolInstance()
        stale_entry = SimpleNamespace(source="manual:hermes_pkce", **stale[0])
        adopted = profile_b.refresh_method(instance, stale_entry, force=False)
        self.assertEqual(adopted.access_token, "fresh-a")
        self.assertEqual(adopted.refresh_token, "fresh-r")
        self.assertEqual(profile_b.lock_targets, [self.root / "auth.json"])
        self.assertEqual(instance.refresh_calls, [])
        self.assertEqual(stores[self.root / "profiles" / "team-skeptic" / "auth.json"]["credential_pool"]["anthropic"], stale)
        self.assertEqual(stores[self.root / "auth.json"]["credential_pool"]["anthropic"][0]["id"], "same-oauth-row")

    def test_only_marked_team_profiles_share_anthropic(self):
        for profile_kind in ("unmarked-team", "nonteam"):
            with self.subTest(profile_kind=profile_kind):
                harness, _, _, ns = self.patched_harness()
                profile_dir = harness.active_path.parent
                if profile_kind == "unmarked-team":
                    (profile_dir / ".hermes-team-managed.json").unlink()
                else:
                    profile_dir.rename(profile_dir.parent / "personal")
                    harness.profile_home = profile_dir.parent / "personal"
                    harness.active_path = harness.profile_home / "auth.json"
                self.assertEqual(ns["_credential_pool_auth_file_path"]("anthropic"), harness.active_path)

    def test_non_anthropic_providers_keep_profile_local_store(self):
        expected_ids = {"kimi": "profile-kimi", "openai-codex": "profile-codex", "custom": "profile-custom"}
        for provider, expected in expected_ids.items():
            with self.subTest(provider=provider):
                harness, _, _, ns = self.patched_harness()
                self.assertEqual(ns["_credential_pool_auth_file_path"](provider), harness.active_path)
                self.assertEqual(ns["read_credential_pool"](provider), [{"id": expected}])

    def test_patch_is_idempotent_and_rejects_partial_or_drifted_sources(self):
        first = hardener.patched_sources(AUTH_SOURCE, POOL_SOURCE)
        self.assertEqual(hardener.patched_sources(*first), first)
        with self.assertRaisesRegex(ValueError, "Partial or duplicate"):
            hardener.patched_sources(AUTH_SOURCE + hardener.HELPER_MARKER, POOL_SOURCE)
        with self.assertRaisesRegex(ValueError, "lock anchor"):
            hardener.patched_sources(AUTH_SOURCE, POOL_SOURCE.replace("_single_use_refresh_lock_timeout()", "other_timeout()"))
        with self.assertRaisesRegex(ValueError, "read_credential_pool differs"):
            hardener.patched_sources(AUTH_SOURCE.replace("global fallback only applies", "changed behavior"), POOL_SOURCE)
        patched_auth, patched_pool = first
        with self.assertRaisesRegex(ValueError, "Partial or duplicate"):
            hardener.patched_sources(patched_auth.replace("_save_auth_store(auth_store, target_path=target_path)", "_save_auth_store(auth_store)"), patched_pool)
        with self.assertRaisesRegex(ValueError, "Partial or duplicate"):
            hardener.patched_sources(patched_auth, patched_pool.replace("target_path=_credential_pool_auth_file_path(self.provider)", ""))

    def test_apply_patch_checks_version_and_keeps_one_time_backups(self):
        root = self.root / "hermes-src"
        (root / "hermes_cli").mkdir(parents=True)
        (root / "agent").mkdir()
        (root / "hermes_cli" / "__init__.py").write_text('__version__ = "0.20.6"\n')
        auth_path, pool_path = root / hardener.AUTH_PATH, root / hardener.POOL_PATH
        auth_path.write_text(AUTH_SOURCE)
        pool_path.write_text(POOL_SOURCE)

        with self.assertRaisesRegex(ValueError, "Expected Hermes 0.20.5"):
            hardener.apply_patch(root, expected_version="0.20.5")
        hardener.apply_patch(root)
        original_auth_backup = auth_path.with_suffix(auth_path.suffix + ".hermes-team-shared-auth.bak")
        original_pool_backup = pool_path.with_suffix(pool_path.suffix + ".hermes-team-shared-auth.bak")
        self.assertEqual(original_auth_backup.read_text(), AUTH_SOURCE)
        self.assertEqual(original_pool_backup.read_text(), POOL_SOURCE)
        patched = (auth_path.read_text(), pool_path.read_text())
        hardener.apply_patch(root)
        self.assertEqual((auth_path.read_text(), pool_path.read_text()), patched)
