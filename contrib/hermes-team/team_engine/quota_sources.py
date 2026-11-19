"""Provider usage probes that write normalised QuotaManager snapshots.

Secrets remain in memory: this module never serialises credentials, response
bodies, or raw exception messages. Usage queries are read-only; Hermes may
refresh an expiring OAuth credential in its native pool before an Anthropic
query. Provider adapters intentionally fail closed to an unknown snapshot when
a source is unsupported or malformed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import importlib
import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import time
import contextvars
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.request import Request, urlopen


KIMI_CODING_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
PROVIDER_TIMEOUT_SECONDS = 9.0
KIMI_TIMEOUT_SECONDS = 8.0
RATE_LIMIT_RETRY_SECONDS = 300.0
ANTHROPIC_PROBE_INTERVAL_SECONDS = 300.0
ANTHROPIC_POOL_REFRESH_SKEW_MS = 120_000


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _reset_timestamp(value: Any) -> float | None:
    number = _number(value)
    if number is not None:
        # Epoch milliseconds are common in web APIs; current epoch seconds are
        # roughly 1e9 and milliseconds roughly 1e12.
        if number > 100_000_000_000:
            number /= 1000.0
        return number
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _percentage(row: Mapping[str, Any]) -> float | None:
    """Return a quota percentage, rejecting malformed recognized quota data.

    Missing values are only safe when the alternate field is present. A field
    that is present but malformed must not be treated as absent, since that
    could cause a different, healthy window to hide this limiting one.
    """
    if "limit" not in row:
        raise ValueError("missing quota limit")
    limit = _number(row.get("limit"))
    if limit is None or limit <= 0:
        raise ValueError("invalid quota limit")
    has_used = "used" in row and row.get("used") is not None
    has_remaining = "remaining" in row and row.get("remaining") is not None
    used = _number(row.get("used")) if has_used else None
    remaining = _number(row.get("remaining")) if has_remaining else None
    if has_used and used is None:
        raise ValueError("invalid quota used value")
    if has_remaining and remaining is None:
        raise ValueError("invalid quota remaining value")
    if used is None and remaining is None:
        raise ValueError("missing quota usage")
    if used is not None and used < 0:
        raise ValueError("negative quota usage")
    if remaining is not None and (remaining < 0 or remaining > limit):
        raise ValueError("quota remaining is outside its valid range")
    if used is not None and remaining is not None:
        # API numbers may be rounded independently. Permit only a small
        # relative discrepancy; a larger mismatch is ambiguous and unsafe.
        if not math.isclose(used + remaining, limit, rel_tol=1e-3, abs_tol=1e-6):
            raise ValueError("quota used and remaining disagree")
    pct = (used / limit * 100.0) if used is not None else ((limit - remaining) / limit * 100.0)
    # Providers occasionally report an overage. QuotaManager accepts a bounded
    # percentage; treating overage as exhausted is conservative and explicit.
    return min(100.0, pct)


def parse_kimi_usage(payload: Any, *, observed_at: float | None = None) -> dict[str, Any]:
    """Parse the verified Kimi Coding `/usages` shape into a safe snapshot.

    Kimi returns numeric fields as either JSON numbers or decimal strings.
    Both the top-level rolling `usage` and every `limits[].detail` window are
    preserved because each can constrain quota availability.
    """
    timestamp = time.time() if observed_at is None else float(observed_at)
    windows: list[dict[str, Any]] = []
    if not isinstance(payload, Mapping):
        return {"source": "kimi-coding-usages", "observed_at": timestamp,
                "windows": [], "error": "invalid Kimi usage response"}

    top_usage = payload.get("usage")
    try:
        if "usage" in payload:
            if not isinstance(top_usage, Mapping):
                raise ValueError("malformed usage window")
            pct = _percentage(top_usage)
            windows.append({"name": str(top_usage.get("name") or "usage"),
                            "used_percent": pct,
                            "reset_at": _reset_timestamp(top_usage.get("resetTime", top_usage.get("reset_at")))})

        limits = payload.get("limits")
        if "limits" in payload:
            if not isinstance(limits, list):
                raise ValueError("malformed limits collection")
            if len(limits) > 32:
                raise ValueError("limits collection exceeds supported size")
        if isinstance(limits, list):
            for index, item in enumerate(limits):
                if not isinstance(item, Mapping) or not isinstance(item.get("detail"), Mapping):
                    raise ValueError("malformed quota limit detail")
                detail = item["detail"]
                pct = _percentage(detail)
                name = detail.get("name")
                if not isinstance(name, str) or not name.strip():
                    name = f"limit_{index}"
                windows.append({"name": name.strip()[:80], "used_percent": pct,
                                "reset_at": _reset_timestamp(detail.get("resetTime", detail.get("reset_at")))})
    except (ValueError, OverflowError):
        # Fail the whole provider snapshot closed. Never omit an unparseable
        # known window and then use another healthy window as evidence of quota.
        return {"source": "kimi-coding-usages", "observed_at": timestamp,
                "windows": [], "error": "Kimi usage response contains a malformed quota window"}

    # `usages` appeared in the observed response but was empty. Its schema is
    # not inferred until a documented/current response supplies a known shape.
    return {"source": "kimi-coding-usages", "observed_at": timestamp,
            "windows": windows,
            "error": None if windows else "Kimi usage response has no recognised windows"}


def _dotenv_kimi_key(hermes_root: Path) -> str | None:
    """Read a Kimi key from Hermes `.env` without logging or returning it outward."""
    path = hermes_root / ".env"
    if not path.is_file():
        return None
    try:
        from dotenv import dotenv_values

        values = dotenv_values(path, encoding="utf-8-sig")
        for name in ("KIMI_API_KEY", "KIMI_CODING_API_KEY"):
            value = values.get(name)
            if isinstance(value, str) and value.strip().startswith("sk-kimi-"):
                return value.strip()
    except Exception:
        # Error details can include path or parser context; keep the caller's
        # diagnostic generic and never print parser output.
        return None
    return None


def _kimi_snapshot(hermes_root: Path) -> dict[str, Any]:
    token = _dotenv_kimi_key(hermes_root)
    if not token:
        return {"source": "kimi-coding-usages", "observed_at": time.time(),
                "windows": [], "error": "Kimi Coding subscription key is not configured in Hermes .env"}

    request = Request(
        KIMI_CODING_USAGE_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                 "User-Agent": "hermes-team-quota-readonly/1.0"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=KIMI_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                return {"source": "kimi-coding-usages", "observed_at": time.time(),
                        "windows": [], "error": f"Kimi endpoint returned HTTP {response.status}"}
            raw_payload = response.read(1_048_577)
            if len(raw_payload) > 1_048_576:
                return {"source": "kimi-coding-usages", "observed_at": time.time(),
                        "windows": [], "error": "Kimi usage response exceeded the size limit"}
            payload = json.loads(raw_payload)
    except HTTPError as exc:
        return {"source": "kimi-coding-usages", "observed_at": time.time(),
                "windows": [], "error": f"Kimi endpoint returned HTTP {exc.code}"}
    except Exception as exc:
        # Keep a bounded, non-sensitive error vocabulary; never include the
        # URL, headers, response body, or exception string in the ledger.
        label = "timeout" if isinstance(exc, TimeoutError) else "request failed"
        return {"source": "kimi-coding-usages", "observed_at": time.time(),
                "windows": [], "error": f"Kimi endpoint {label} ({type(exc).__name__})"}
    return parse_kimi_usage(payload, observed_at=time.time())


def _import_account_usage(hermes_root: Path):
    try:
        return importlib.import_module("agent.account_usage")
    except ImportError:
        candidates = []
        source_root = os.environ.get("HERMES_SOURCE_ROOT", "").strip()
        if source_root:
            candidates.append(Path(source_root).expanduser())
        candidates.extend((hermes_root / "hermes-agent", Path.home() / ".hermes" / "hermes-agent"))
        for candidate in candidates:
            if (candidate / "agent" / "account_usage.py").is_file():
                source = str(candidate.resolve())
                if source not in sys.path:
                    sys.path.insert(0, source)
                return importlib.import_module("agent.account_usage")
        raise


def _bounded_call(callable_, timeout: float = PROVIDER_TIMEOUT_SECONDS) -> Any:
    """Run Hermes' existing quota resolver with a hard wait bound."""
    result_queue: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    context = contextvars.copy_context()

    def run() -> None:
        try:
            result_queue.put((True, callable_()))
        except BaseException as exc:  # returned to caller, never logged
            result_queue.put((False, exc))

    thread = threading.Thread(target=lambda: context.run(run), name="quota-source-probe", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError("quota source probe exceeded its deadline")
    try:
        ok, value = result_queue.get_nowait()
    except queue.Empty as exc:
        raise RuntimeError("quota source probe returned no result") from exc
    if not ok:
        raise value
    return value


def _refresh_expired_anthropic_pool_credential(account_usage: Any) -> None:
    """Refresh only the expired pool row that the usage resolver selected.

    The ordinary Anthropic resolver is read-only for credential-pool fallback,
    while runtime pool selection refreshes expiring OAuth entries. Align this
    preflight with runtime without refreshing static env/API credentials or
    silently switching to another account. The native pool API owns refresh
    locking and synchronization across processes.
    """
    resolver = getattr(account_usage, "resolve_anthropic_token", None)
    if not callable(resolver):
        return

    from agent import anthropic_adapter
    from agent.credential_pool import AUTH_TYPE_OAUTH, load_pool

    # These sources take precedence in resolve_anthropic_token. In particular,
    # never rotate a pool entry merely because a static API/setup token is set.
    if any(anthropic_adapter._getenv(name, "").strip() for name in (
            "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")):
        return

    selected_token = resolver()
    if not selected_token:
        return
    pool = load_pool("anthropic")
    matches = [entry for entry in pool.entries()
               if getattr(entry, "auth_type", None) == AUTH_TYPE_OAUTH
               and (getattr(entry, "access_token", None) or "").strip() == selected_token]
    if len(matches) != 1:
        return
    entry = matches[0]
    expires_at_ms = _number(getattr(entry, "expires_at_ms", None))
    if expires_at_ms is None or expires_at_ms <= 0:
        return
    if expires_at_ms > time.time() * 1000 + ANTHROPIC_POOL_REFRESH_SKEW_MS:
        return
    if not (getattr(entry, "refresh_token", None) or "").strip():
        raise RuntimeError("Anthropic pool credential is expired and cannot be refreshed")

    credential_id = getattr(entry, "id", None)
    if not isinstance(credential_id, str) or not credential_id:
        raise RuntimeError("Anthropic pool credential has no stable identity")
    try:
        refreshed = pool.try_refresh_matching(
            api_key_hint=selected_token,
            credential_id=credential_id,
        )
    except Exception as exc:
        # Do not retain the exception text: provider errors can include auth
        # details. The caller turns this into an unknown quota snapshot.
        raise RuntimeError("Anthropic pool credential refresh failed") from exc
    refreshed_id = getattr(refreshed, "id", None)
    refreshed_token = (getattr(refreshed, "access_token", None) or "").strip()
    if refreshed is None or refreshed_id != credential_id or not refreshed_token:
        raise RuntimeError("Anthropic pool credential refresh returned no matching credential")

    # Re-resolve through Hermes instead of trusting the returned object alone;
    # this observes the durable pool state, including a concurrent refresh
    # completed while the native cross-process lock was being acquired.
    current_token = resolver()
    if current_token != refreshed_token:
        raise RuntimeError("Anthropic usage resolver did not select the refreshed credential")


def _snapshot_from_hermes(provider: str, account_usage: Any) -> dict[str, Any]:
    # Hermes' public display helper deliberately swallows provider exceptions.
    # Admission control needs the HTTP status/Retry-After to stop polling a
    # rate-limited usage endpoint. Use its existing Anthropic resolver when
    # available; it keeps the same credentials, endpoint and response parser.
    anthropic_fetch = getattr(account_usage, "_fetch_anthropic_account_usage", None)
    if provider == "anthropic" and callable(anthropic_fetch):
        _refresh_expired_anthropic_pool_credential(account_usage)
    snapshot = (anthropic_fetch() if provider == "anthropic" and callable(anthropic_fetch)
                else account_usage.fetch_account_usage(provider))
    observed = time.time()
    if snapshot is None:
        raise RuntimeError("Hermes account usage is unavailable")
    fetched_at = getattr(snapshot, "fetched_at", None)
    if isinstance(fetched_at, datetime):
        observed = fetched_at.timestamp()
    elif isinstance(fetched_at, (int, float)) and not isinstance(fetched_at, bool):
        observed = float(fetched_at)
    windows = []
    for index, window in enumerate(getattr(snapshot, "windows", ())):
        used = _number(getattr(window, "used_percent", None))
        if used is None or not 0 <= used <= 100:
            raise RuntimeError("Hermes usage response contains a malformed quota window")
        reset_at = getattr(window, "reset_at", None)
        reset_ts = reset_at.timestamp() if isinstance(reset_at, datetime) else _reset_timestamp(reset_at)
        label = getattr(window, "label", None) or f"window_{index}"
        windows.append({"name": str(label)[:80], "used_percent": used, "reset_at": reset_ts})
    if not windows:
        raise RuntimeError("Hermes usage response has no recognised quota windows")
    source = getattr(snapshot, "source", None) or provider
    return {"source": f"hermes-account-usage:{source}", "observed_at": observed,
            "windows": windows, "error": None}


def _pool_source_snapshot(provider: str, hermes_root: Path) -> dict[str, Any]:
    normalized = provider.strip().lower()
    if normalized in {"kimi-coding", "kimi"}:
        return _kimi_snapshot(hermes_root)
    if normalized == "openai-codex":
        hermes_provider = "openai-codex"
    elif normalized in {"anthropic", "claude-code"}:
        hermes_provider = "anthropic"
    else:
        return {"source": "unsupported-provider", "observed_at": time.time(),
                "windows": [], "error": "no supported read-only quota source for configured provider"}
    account_usage = _import_account_usage(hermes_root)
    return _snapshot_from_hermes(hermes_provider, account_usage)


def _canonical_auth_source(provider: Any) -> str | None:
    if not isinstance(provider, str):
        return None
    normalized = provider.strip().lower()
    if normalized == "openai-codex":
        return "openai-codex:hermes-global-oauth"
    if normalized in {"anthropic", "claude-code"}:
        return "anthropic:hermes-global-oauth"
    if normalized in {"kimi-coding", "kimi"}:
        return "kimi-coding:hermes-root-dotenv"
    return None


def _retry_after_timestamp(value: Any, now: float) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    seconds = _number(value)
    if seconds is not None:
        deadline = now + seconds
        if seconds < 0 or not math.isfinite(deadline):
            return None
        try:
            datetime.fromtimestamp(deadline, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
        return deadline
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(now, parsed.timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def _probe_failure_snapshot(exc: Exception, provider: str) -> dict[str, Any]:
    """Keep bounded diagnostics; never persist tokens, URLs or response bodies."""
    now = time.time()
    snapshot = {"source": "quota-source", "observed_at": now, "windows": [],
                "error": f"quota source failed ({type(exc).__name__})"}
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        return snapshot
    snapshot["error"] = f"Сервис статистики лимитов {provider} вернул HTTP {status}"
    headers = getattr(response, "headers", {})
    retry_at = _retry_after_timestamp(headers.get("retry-after"), now)
    if status == 429:
        retry_at = retry_at if retry_at is not None else now + RATE_LIMIT_RETRY_SECONDS
    if status in (429, 503) and retry_at is not None:
        snapshot["probe_retry_at"] = retry_at
        retry_label = datetime.fromtimestamp(retry_at, timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")
        snapshot["error"] += f"; повторная проверка после {retry_label}"
    if status == 429:
        snapshot["error"] += ". Это ограничение запроса статистики, остаток подписки пока неизвестен"
    return snapshot


def refresh_snapshots(quota_manager: Any, config: Mapping[str, Any],
                      hermes_root: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Refresh configured pool snapshots and return non-secret status records.

    `config` may be the whole team config (`config['quota']`) or its quota
    section. Each provider probe is read-only and bounded to at most nine
    seconds of scheduler wait. Any failure updates the pool to unknown rather
    than preserving an older apparently-healthy snapshot.
    """
    quota_config = config.get("quota", config) if isinstance(config, Mapping) else None
    pools = quota_config.get("pools") if isinstance(quota_config, Mapping) else None
    if not isinstance(pools, Mapping):
        raise ValueError("quota configuration must contain a pools object")
    root = Path(hermes_root).expanduser().resolve()
    source_pools: dict[str, list[str]] = {}
    for pool_id, raw_pool in pools.items():
        if (isinstance(pool_id, str) and isinstance(raw_pool, Mapping)
                and raw_pool.get("enabled") is True and raw_pool.get("auth_ready") is True):
            source = _canonical_auth_source(raw_pool.get("provider"))
            if source:
                source_pools.setdefault(source, []).append(pool_id)
    duplicated_pools = {
        pool_id
        for pool_ids in source_pools.values() if len(pool_ids) > 1
        for pool_id in pool_ids
    }
    records: list[dict[str, Any]] = []
    for pool_id, raw_pool in pools.items():
        if not isinstance(pool_id, str) or not isinstance(raw_pool, Mapping):
            continue
        if raw_pool.get("enabled") is not True:
            records.append({"pool": pool_id, "status": "skipped", "reason": "pool disabled"})
            continue
        if raw_pool.get("auth_ready") is not True:
            snapshot = {"source": "quota-source", "observed_at": time.time(),
                        "windows": [], "error": "pool credentials are not marked ready"}
        elif pool_id in duplicated_pools:
            snapshot = {"source": "quota-source", "observed_at": time.time(),
                        "windows": [],
                        "error": "multiple pools share one provider auth source; distinct accounts are unsupported"}
        else:
            cached = quota_manager.snapshot_for_probe(pool_id)
            now = time.time()
            retry_at = 0.0
            if isinstance(cached, Mapping):
                if cached.get("error"):
                    retry_at = _number(cached.get("probe_retry_at")) or 0.0
                observed = _number(cached.get("observed_at"))
                if (str(raw_pool.get("provider", "")).strip().lower()
                        in {"anthropic", "claude-code"}
                        and observed is not None and observed <= now):
                    retry_at = max(retry_at, observed + ANTHROPIC_PROBE_INTERVAL_SECONDS)
            if isinstance(cached, Mapping) and retry_at > now:
                # Share the five-minute Anthropic cadence across controller
                # restarts and manual refreshes, including successful probes.
                # Never renew observed_at: admission still rejects stale data.
                observed = _number(cached.get("observed_at"))
                max_age = _number(raw_pool.get("max_snapshot_age", 180)) or 0.0
                fresh = observed is not None and 0 <= now - observed <= max_age
                records.append({"pool": pool_id,
                                "status": "available" if fresh and not cached.get("error") else "unknown",
                                "source": cached.get("source"),
                                "observed_at": cached.get("observed_at"),
                                "windows": cached.get("windows", []),
                                "error": cached.get("error"), "cached": True,
                                "next_probe_at": retry_at})
                if "probe_retry_at" in cached:
                    records[-1]["probe_retry_at"] = cached["probe_retry_at"]
                continue
            try:
                provider = raw_pool.get("provider")
                if not isinstance(provider, str) or not provider.strip():
                    raise ValueError("pool provider is not configured")
                snapshot = _bounded_call(lambda: _pool_source_snapshot(provider, root))
            except TimeoutError:
                snapshot = {"source": "quota-source", "observed_at": time.time(),
                            "windows": [], "error": "quota source timed out"}
            except Exception as exc:
                snapshot = _probe_failure_snapshot(exc, provider)
        quota_manager.update_snapshot(pool_id, snapshot)
        records.append({"pool": pool_id, "status": "unknown" if snapshot.get("error") else "available",
                        "source": snapshot.get("source"), "observed_at": snapshot.get("observed_at"),
                        "windows": snapshot.get("windows", []), "error": snapshot.get("error")})
        if "probe_retry_at" in snapshot:
            records[-1]["probe_retry_at"] = snapshot["probe_retry_at"]
    return records
