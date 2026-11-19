"""Deterministic, file-backed model quota reservations for Hermes roles.

This module consumes usage snapshots supplied by trusted local probes. It never
contacts model providers and never invents a paid or credential fallback.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Mapping


VALIDATION_ROLES = {
    "reviewer", "qa", "integrator", "judge", "skeptic", "test-designer",
}


class QuotaError(ValueError):
    """Invalid quota configuration, snapshot, or reservation request."""


class QuotaManager:
    """Select explicit model pools and reserve a concurrency lease per task key."""

    def __init__(self, config: Mapping[str, Any], ledger_path: str | os.PathLike[str],
                 *, clock: Callable[[], float] = time.time) -> None:
        if not isinstance(config, Mapping):
            raise QuotaError("quota config must be an object")
        raw_pools = config.get("pools")
        if not isinstance(raw_pools, Mapping) or not raw_pools:
            raise QuotaError("quota config requires a non-empty pools object")
        self.pools: dict[str, dict[str, Any]] = {}
        for pool_id, raw in raw_pools.items():
            if not isinstance(pool_id, str) or not pool_id.strip() or not isinstance(raw, Mapping):
                raise QuotaError("each pool needs a non-empty id and object config")
            provider = raw.get("provider")
            if not isinstance(provider, str) or not provider.strip():
                raise QuotaError(f"pool {pool_id} requires a provider")
            concurrency = raw.get("max_concurrent", 2)
            reserve = raw.get("reserve_percent", 20)
            age = raw.get("max_snapshot_age", 180)
            unknown_policy = raw.get("unknown_policy", "wait")
            if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
                raise QuotaError(f"pool {pool_id} max_concurrent must be a positive integer")
            if (isinstance(reserve, bool) or not isinstance(reserve, (int, float))
                    or not math.isfinite(reserve) or not 0 <= reserve < 100):
                raise QuotaError(f"pool {pool_id} reserve_percent must be in [0, 100)")
            if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or age < 0:
                raise QuotaError(f"pool {pool_id} max_snapshot_age must be non-negative")
            if not isinstance(unknown_policy, str) or unknown_policy not in {"wait", "allow_limited"}:
                raise QuotaError(f"pool {pool_id} unknown_policy must be wait or allow_limited")
            self.pools[pool_id] = {
                "provider": provider.strip(),
                "enabled": raw.get("enabled") is True,
                "auth_ready": raw.get("auth_ready") is True,
                "max_concurrent": concurrency,
                "reserve_percent": float(reserve),
                "unknown_policy": unknown_policy,
                "max_snapshot_age": float(age),
            }
        self.ledger_path = Path(ledger_path).expanduser().resolve()
        if not self.ledger_path.is_absolute():
            raise QuotaError("ledger_path must be absolute")
        self.lock_path = self.ledger_path.with_name(self.ledger_path.name + ".lock")
        self.clock = clock
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked_ledger() as ledger:
            changed = False
            for pool_id in self.pools:
                if pool_id not in ledger["pools"]:
                    ledger["pools"][pool_id] = {"snapshot": None}
                    changed = True
            if changed:
                self._write(ledger)

    @contextmanager
    def _locked_ledger(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a") as lock_stream:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
            yield self._read()

    def _read(self) -> dict[str, Any]:
        if not self.ledger_path.exists():
            return {"schema_version": 1, "pools": {}, "leases": {}}
        try:
            ledger = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise QuotaError(f"Could not read quota ledger: {exc}") from exc
        if (not isinstance(ledger, dict) or ledger.get("schema_version") != 1
                or not isinstance(ledger.get("pools"), dict)
                or not isinstance(ledger.get("leases"), dict)):
            raise QuotaError("Quota ledger has an invalid schema")
        return ledger

    def _write(self, ledger: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.ledger_path.name}.",
                                         dir=self.ledger_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(ledger, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.ledger_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _validate_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(snapshot, Mapping):
            raise QuotaError("usage snapshot must be an object")
        observed_at = snapshot.get("observed_at")
        if isinstance(observed_at, bool) or not isinstance(observed_at, (int, float)) or not math.isfinite(observed_at):
            raise QuotaError("snapshot observed_at must be a finite Unix timestamp")
        windows = snapshot.get("windows", [])
        if not isinstance(windows, list):
            raise QuotaError("snapshot windows must be a list")
        clean_windows = []
        for index, window in enumerate(windows):
            if not isinstance(window, Mapping) or not isinstance(window.get("name"), str) or not window["name"].strip():
                raise QuotaError(f"snapshot window {index} requires a name")
            used = window.get("used_percent")
            if (isinstance(used, bool) or not isinstance(used, (int, float))
                    or not math.isfinite(used) or not 0 <= used <= 100):
                raise QuotaError(f"snapshot window {index} used_percent must be within 0..100")
            reset_at = window.get("reset_at")
            if reset_at is not None and (isinstance(reset_at, bool) or
                                         not isinstance(reset_at, (int, float)) or not math.isfinite(reset_at)):
                raise QuotaError(f"snapshot window {index} reset_at must be a finite timestamp or null")
            clean_windows.append({"name": window["name"], "used_percent": float(used),
                                  "reset_at": reset_at})
        source = snapshot.get("source", "unknown")
        error = snapshot.get("error")
        if not isinstance(source, str) or (error is not None and not isinstance(error, str)):
            raise QuotaError("snapshot source/error must be strings")
        clean = {"source": source, "observed_at": float(observed_at),
                 "windows": clean_windows, "error": error}
        if "probe_retry_at" in snapshot:
            probe_retry_at = snapshot["probe_retry_at"]
            if (isinstance(probe_retry_at, bool)
                    or not isinstance(probe_retry_at, (int, float))
                    or not math.isfinite(probe_retry_at)):
                raise QuotaError("snapshot probe_retry_at must be a finite Unix timestamp")
            clean["probe_retry_at"] = float(probe_retry_at)
        return clean

    def update_snapshot(self, pool_id: str, snapshot: Mapping[str, Any]) -> None:
        if pool_id not in self.pools:
            raise QuotaError(f"Unknown quota pool: {pool_id}")
        clean = self._validate_snapshot(snapshot)
        with self._locked_ledger() as ledger:
            ledger["pools"].setdefault(pool_id, {})["snapshot"] = clean
            self._write(ledger)

    def snapshot_for_probe(self, pool_id: str) -> dict[str, Any] | None:
        """Return a detached copy of the last safe snapshot for probe caching.

        Snapshots contain only validated usage metadata, never provider
        credentials. Returning a deep copy prevents probe code from mutating
        the ledger's nested windows outside its lock.
        """
        if pool_id not in self.pools:
            raise QuotaError(f"Unknown quota pool: {pool_id}")
        with self._locked_ledger() as ledger:
            pool_ledger = ledger["pools"].get(pool_id, {})
            snapshot = pool_ledger.get("snapshot") if isinstance(pool_ledger, Mapping) else None
            return copy.deepcopy(dict(snapshot)) if isinstance(snapshot, Mapping) else None

    def _candidate_list(self, role_settings: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(role_settings, Mapping):
            raise QuotaError("role settings must be an object")
        if "candidates" in role_settings:
            raw_candidates = role_settings["candidates"]
            if not isinstance(raw_candidates, list) or not raw_candidates:
                raise QuotaError("role candidates must be a non-empty list when configured")
        else:
            raw_candidates = [{
                "pool": role_settings.get("pool"),
                "provider": role_settings.get("provider"),
                "model": role_settings.get("model"),
                "reasoning_effort": role_settings.get("reasoning_effort"),
                "tier": role_settings.get("tier", 0),
            }]
        candidates = []
        for index, raw in enumerate(raw_candidates):
            if not isinstance(raw, Mapping):
                raise QuotaError(f"candidate {index} must be an object")
            provider, model = raw.get("provider"), raw.get("model")
            pool_id = raw.get("pool")
            if pool_id is None and isinstance(provider, str):
                matches = [pid for pid, cfg in self.pools.items() if cfg["provider"] == provider]
                pool_id = matches[0] if matches else None
            tier = raw.get("tier", 0)
            if isinstance(tier, bool) or not isinstance(tier, int) or tier < 0:
                raise QuotaError(f"candidate {index} tier must be a non-negative integer")
            if not isinstance(provider, str) or not provider.strip() or not isinstance(model, str) or not model.strip():
                raise QuotaError(f"candidate {index} requires explicit provider and model")
            if not isinstance(pool_id, str) or pool_id not in self.pools:
                candidates.append({"index": index, "pool": None, "provider": provider.strip(),
                                   "model": model.strip(), "reasoning_effort": raw.get("reasoning_effort"),
                                   "tier": tier})
                continue
            if self.pools[pool_id]["provider"] != provider.strip():
                raise QuotaError(f"candidate provider does not match pool {pool_id}")
            effort = raw.get("reasoning_effort", role_settings.get("reasoning_effort"))
            if effort is not None and (not isinstance(effort, str) or not effort.strip()):
                raise QuotaError(f"candidate {index} reasoning_effort must be a non-empty string or null")
            candidates.append({"index": index, "pool": pool_id, "provider": provider.strip(),
                               "model": model.strip(), "reasoning_effort": effort,
                               "tier": tier})
        return candidates

    def _snapshot_state(self, pool_id: str, pool_config: Mapping[str, Any],
                        pool_ledger: Mapping[str, Any], now: float) -> tuple[str, float | None, float | None, str | None]:
        snapshot = pool_ledger.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return "unknown", None, None, "usage snapshot is missing"
        if snapshot.get("error"):
            return "unknown", None, None, f"usage snapshot error: {snapshot['error']}"
        observed_at = snapshot.get("observed_at")
        windows = snapshot.get("windows")
        if (isinstance(observed_at, bool) or not isinstance(observed_at, (int, float))
                or not isinstance(windows, list) or not windows):
            return "unknown", None, None, "usage snapshot has no valid windows"
        age = now - observed_at
        if age < 0 or age > pool_config["max_snapshot_age"]:
            return "unknown", None, None, f"usage snapshot is stale or future-dated (age={age:.0f}s)"
        remaining = []
        resets = []
        for window in windows:
            if not isinstance(window, Mapping):
                return "unknown", None, None, "usage snapshot contains a malformed window"
            used = window.get("used_percent")
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
                return "unknown", None, None, "usage snapshot contains an invalid used_percent"
            remaining.append(100.0 - float(used))
            reset = window.get("reset_at")
            if isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(reset):
                resets.append(float(reset))
        # All provider windows constrain availability; minimum remaining quota
        # is therefore conservative for the next request.
        return "known", min(remaining), min(resets) if resets else None, None

    def _result(self, available: bool, selection: dict[str, Any] | None,
                reason: str, reset_at: float | None = None) -> dict[str, Any]:
        return {"available": available, "selection": selection, "reason": reason,
                "reset_at": reset_at}

    @staticmethod
    def _relevant_reset(snapshot: Any, threshold: float) -> float | None:
        """Return when all windows blocking this threshold have reset.

        A missing reset on any blocking window keeps the estimate unknown. A
        reset on an unrelated, healthy window must not make a blocked task look
        eligible sooner than the actual constrained windows.
        """
        if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("windows"), list):
            return None
        constrained = []
        for window in snapshot["windows"]:
            if not isinstance(window, Mapping):
                return None
            used = window.get("used_percent")
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used):
                return None
            if 100.0 - float(used) <= threshold:
                reset = window.get("reset_at")
                if (isinstance(reset, bool) or not isinstance(reset, (int, float))
                        or not math.isfinite(reset)):
                    return None
                constrained.append(float(reset))
        return max(constrained) if constrained else None

    def reserve(self, key: str, role: str, role_settings: Mapping[str, Any],
                priority: int = 20) -> dict[str, Any]:
        if not isinstance(key, str) or not key.strip():
            raise QuotaError("reservation key must be a non-empty deterministic task id")
        if not isinstance(role, str) or not role.strip():
            raise QuotaError("role must be a non-empty string")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise QuotaError("priority must be an integer")
        candidates = self._candidate_list(role_settings)
        now = float(self.clock())
        if not math.isfinite(now):
            raise QuotaError("clock must return a finite timestamp")
        with self._locked_ledger() as ledger:
            old = ledger["leases"].get(key)
            if old is not None:
                if old.get("role") != role:
                    raise QuotaError(f"reservation key {key!r} is already assigned to role {old.get('role')!r}")
                if old.get("active") is True:
                    return self._result(True, old["selection"], "existing reservation (idempotent)",
                                        old.get("reset_at"))
            active = [value for value in ledger["leases"].values()
                      if isinstance(value, Mapping) and value.get("active") is True]
            ranked = []
            blocked_reasons = []
            blocked_resets = []
            for candidate in candidates:
                pool_id = candidate["pool"]
                if pool_id is None:
                    blocked_reasons.append(f"no configured pool for provider {candidate['provider']}")
                    continue
                pool_config = self.pools[pool_id]
                if not pool_config["enabled"]:
                    blocked_reasons.append(f"pool {pool_id} is disabled")
                    continue
                if not pool_config["auth_ready"]:
                    blocked_reasons.append(f"pool {pool_id} credentials are not ready")
                    continue
                pool_active = [lease for lease in active if lease.get("pool") == pool_id]
                if len(pool_active) >= pool_config["max_concurrent"]:
                    blocked_reasons.append(f"pool {pool_id} reached max_concurrent")
                    continue
                snapshot_record = ledger["pools"].get(pool_id, {})
                snapshot_state, remaining, reset_at, unknown_reason = self._snapshot_state(
                    pool_id, pool_config, snapshot_record, now)
                unknown_limited = False
                if snapshot_state == "unknown":
                    if pool_config["unknown_policy"] != "allow_limited":
                        blocked_reasons.append(f"pool {pool_id} quota unknown; waiting ({unknown_reason})")
                        continue
                    if any(lease.get("pool") == pool_id and lease.get("unknown_limited") is True
                           for lease in pool_active):
                        blocked_reasons.append(f"pool {pool_id} already has its one unknown-limited lease")
                        continue
                    unknown_limited = True
                else:
                    validation_role = role.removeprefix("team-") in VALIDATION_ROLES
                    required_remaining = 0.0 if validation_role else pool_config["reserve_percent"]
                    if remaining is None or remaining <= required_remaining:
                        blocked_reasons.append(f"pool {pool_id} remaining quota {remaining}% is at/below "
                                               f"the {required_remaining:g}% threshold")
                        relevant_reset = self._relevant_reset(
                            snapshot_record.get("snapshot"), required_remaining)
                        if relevant_reset is not None:
                            blocked_resets.append(relevant_reset)
                        continue
                selection = {
                    "pool": pool_id,
                    "provider": candidate["provider"],
                    "model": candidate["model"],
                    "reasoning_effort": candidate["reasoning_effort"],
                }
                ranked.append({"tier": candidate["tier"], "unknown": unknown_limited,
                               "remaining": remaining if remaining is not None else -1.0,
                               "index": candidate["index"], "pool": pool_id,
                               "selection": selection, "reset_at": reset_at,
                               "unknown_limited": unknown_limited, "unknown_reason": unknown_reason})
            if not ranked:
                reason = blocked_reasons[0] if blocked_reasons else "no eligible model candidate"
                # With explicit alternatives, any one candidate becoming
                # available could unblock dispatch, so report the earliest
                # trustworthy relevant reset among quota-blocked candidates.
                return self._result(False, None, reason,
                                    min(blocked_resets) if blocked_resets else None)
            # Tier defines explicit failover preference. Within one tier prefer
            # a fresh known snapshot and more headroom; declaration order breaks ties.
            chosen = min(ranked, key=lambda item: (item["tier"], item["unknown"],
                                                   -item["remaining"], item["index"]))
            ledger["leases"][key] = {
                "active": True,
                "task_key": key,
                "role": role,
                "generation": (old.get("generation", 1) + 1 if isinstance(old, Mapping) else 1),
                "previous_released_at": (old.get("released_at") if isinstance(old, Mapping) else None),
                "priority": priority,
                "pool": chosen["pool"],
                "selection": chosen["selection"],
                "unknown_limited": chosen["unknown_limited"],
                "reserved_at": now,
                "reset_at": chosen["reset_at"],
            }
            self._write(ledger)
            if chosen["unknown_limited"]:
                reason = "reserved with explicit unknown-limited policy: " + str(chosen["unknown_reason"])
            else:
                reason = f"reserved; {chosen['remaining']:.1f}% headroom in pool {chosen['pool']}"
            return self._result(True, chosen["selection"], reason, chosen["reset_at"])

    def release(self, key: str) -> bool:
        if not isinstance(key, str) or not key.strip():
            raise QuotaError("reservation key must be a non-empty string")
        with self._locked_ledger() as ledger:
            lease = ledger["leases"].get(key)
            if not isinstance(lease, dict) or lease.get("active") is not True:
                return False
            lease["active"] = False
            lease["released_at"] = float(self.clock())
            self._write(ledger)
            return True

    def status(self) -> dict[str, Any]:
        now = float(self.clock())
        with self._locked_ledger() as ledger:
            pools = {}
            for pool_id, config in self.pools.items():
                pool_ledger = ledger["pools"].get(pool_id, {})
                state, remaining, reset_at, reason = self._snapshot_state(
                    pool_id, config, pool_ledger, now)
                snapshot = pool_ledger.get("snapshot") if isinstance(pool_ledger, Mapping) else None
                if state == "known":
                    reset_at = self._relevant_reset(snapshot, config["reserve_percent"])
                clean_windows = []
                if isinstance(snapshot, Mapping) and isinstance(snapshot.get("windows"), list):
                    for window in snapshot["windows"]:
                        if isinstance(window, Mapping):
                            clean_windows.append({
                                "name": window.get("name"),
                                "used_percent": window.get("used_percent"),
                                "reset_at": window.get("reset_at"),
                            })
                leases = [lease for lease in ledger["leases"].values()
                          if isinstance(lease, Mapping) and lease.get("active") is True
                          and lease.get("pool") == pool_id]
                pools[pool_id] = {
                    "provider": config["provider"], "enabled": config["enabled"],
                    "auth_ready": config["auth_ready"], "snapshot_state": state,
                    "remaining_percent": remaining, "reset_at": reset_at,
                    "snapshot_source": snapshot.get("source") if isinstance(snapshot, Mapping) else None,
                    "snapshot_observed_at": snapshot.get("observed_at") if isinstance(snapshot, Mapping) else None,
                    "probe_retry_at": snapshot.get("probe_retry_at") if isinstance(snapshot, Mapping) else None,
                    "snapshot_age_seconds": (now - snapshot["observed_at"]
                                              if isinstance(snapshot, Mapping)
                                              and isinstance(snapshot.get("observed_at"), (int, float))
                                              and not isinstance(snapshot.get("observed_at"), bool) else None),
                    "snapshot_windows": clean_windows,
                    "active_leases": len(leases), "unknown_limited_leases": sum(
                        lease.get("unknown_limited") is True for lease in leases),
                    "max_concurrent": config["max_concurrent"], "reason": reason,
                }
            return {"pools": pools, "leases": dict(ledger["leases"])}
