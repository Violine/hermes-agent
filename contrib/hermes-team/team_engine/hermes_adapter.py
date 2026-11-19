"""Thin, auditable adapter over Hermes' native Kanban SQLite API.

This module intentionally does not implement a second task database. It pins
all operations to one Hermes root and board, delegates state transitions to
``hermes_cli.kanban_db``, and gives the controller a claimed, non-spawnable
machine-owned card for deterministic gates.

Set ``HERMES_SOURCE_ROOT`` before importing this module when Hermes is not
already importable by the controller process. No model calls are made here.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, fields, is_dataclass
import importlib
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any, Iterable, Mapping, Optional


_ROOT_LOCK = threading.RLock()
_VALID_EFFORTS = {
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TASK_ID_RE = re.compile(r"^t_[0-9a-f]{8,}$")


class HermesBoardError(RuntimeError):
    """Raised when Hermes refuses a board operation or ownership check."""


def _as_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "__dict__"):
        return {
            field.name: getattr(value, field.name)
            for field in fields(value)
        } if is_dataclass(value) else dict(vars(value))
    raise TypeError(f"Cannot convert Hermes record {type(value).__name__} to dict")


def _load_kanban_db():
    source_root = os.environ.get("HERMES_SOURCE_ROOT", "").strip()
    if source_root:
        source = str(Path(source_root).expanduser().resolve())
        if source not in sys.path:
            sys.path.insert(0, source)
    try:
        return importlib.import_module("hermes_cli.kanban_db")
    except ImportError as exc:
        raise HermesBoardError(
            "Hermes kanban_db is not importable. Install the Hermes source on "
            "PYTHONPATH or set HERMES_SOURCE_ROOT to its checkout."
        ) from exc


class HermesBoard:
    """Native Kanban API client pinned to ``hermes_root`` and ``board_slug``.

    ``hermes_root`` is Hermes' shared root (normally ``~/.hermes``), not a
    profile subdirectory. The shared root setting is process-wide in Hermes;
    this class refuses to silently redirect an already-configured process.
    """

    CONTROLLER_ASSIGNEE = "team-controller"

    def __init__(
        self,
        board_slug: str,
        hermes_root: str | os.PathLike[str] = "/home/alex/.hermes",
        *,
        controller_id: str = "hermes-team-controller",
        kanban_db: Any = None,
    ) -> None:
        slug = str(board_slug).strip().lower()
        if not _SLUG_RE.fullmatch(slug):
            raise ValueError("board_slug must be lowercase letters, digits, '-' or '_' (1-64 chars)")
        root = Path(hermes_root).expanduser().resolve()
        if not root.is_absolute():
            raise ValueError("hermes_root must be an absolute path")
        current = os.environ.get("HERMES_KANBAN_HOME", "").strip()
        if current and Path(current).expanduser().resolve() != root:
            raise HermesBoardError(
                f"HERMES_KANBAN_HOME already points to {current}; refusing to switch it to {root}"
            )
        os.environ.setdefault("HERMES_KANBAN_HOME", str(root))
        self.board_slug = slug
        self.hermes_root = root
        self.controller_id = self._validate_controller_id(controller_id)
        self._kb = kanban_db if kanban_db is not None else _load_kanban_db()

    @staticmethod
    def _validate_controller_id(value: str) -> str:
        value = str(value).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", value):
            raise ValueError("controller_id contains unsupported characters")
        return value

    @contextmanager
    def _connection(self):
        # Hermes' SQLite connections are thread-affine. Every operation opens
        # and closes its own connection instead of sharing one across workers.
        conn = self._kb.connect(board=self.board_slug)
        try:
            yield conn
        finally:
            conn.close()

    def ensure_board(
        self,
        name: str,
        description: str,
        default_workdir: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create the board idempotently or refresh its display metadata."""
        path = Path(default_workdir).expanduser() if default_workdir else None
        if path is not None and not path.is_absolute():
            raise ValueError("default_workdir must be absolute")
        with _ROOT_LOCK:
            if self._kb.board_exists(self.board_slug):
                meta = self._kb.write_board_metadata(
                    self.board_slug,
                    name=name,
                    description=description,
                    default_workdir=str(path) if path else "",
                )
                self._kb.init_db(board=self.board_slug)
            else:
                meta = self._kb.create_board(
                    self.board_slug,
                    name=name,
                    description=description,
                    default_workdir=str(path) if path else None,
                )
        return dict(meta)

    def create_task(
        self,
        title: str,
        assignee: str,
        body: Optional[str] = None,
        parents: Iterable[str] = (),
        priority: int = 0,
        workspace_path: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        max_runtime_seconds: Optional[int] = 900,
        skills: Optional[Iterable[str]] = None,
        created_by: str = "hermes-team-controller",
        initial_status: str = "running",
        triage: bool = False,
    ) -> dict[str, Any]:
        """Create a normal agent card; native dispatcher handles assignment.

        Tasks are inserted with their parent edges in the same SQLite write
        transaction. Provide a still-open controller root as a parent while a
        multi-call DAG is being built so no child can become ready early.
        """
        if not str(title).strip() or not str(assignee).strip():
            raise ValueError("title and assignee are required")
        if max_runtime_seconds is not None and (
            isinstance(max_runtime_seconds, bool) or int(max_runtime_seconds) < 1
        ):
            raise ValueError("max_runtime_seconds must be a positive integer or None")
        if initial_status not in {"running", "blocked"}:
            raise ValueError("initial_status must be 'running' or 'blocked'")
        if provider and not model:
            raise ValueError("provider requires model")
        if reasoning_effort is not None:
            reasoning_effort = str(reasoning_effort).strip().lower()
            if reasoning_effort not in _VALID_EFFORTS:
                raise ValueError(f"reasoning_effort must be one of {sorted(_VALID_EFFORTS)}")
        if workspace_path:
            path = Path(workspace_path).expanduser()
            if not path.is_absolute():
                raise ValueError("workspace_path must be absolute")
            workspace_kind = "dir"
            workspace_path = str(path)
        else:
            workspace_kind = "scratch"
        with self._connection() as conn:
            task_id = self._kb.create_task(
                conn,
                title=str(title).strip(),
                body=body,
                assignee=str(assignee).strip(),
                parents=tuple(parents),
                priority=int(priority),
                workspace_kind=workspace_kind,
                workspace_path=workspace_path,
                model_override=model,
                provider_override=provider,
                reasoning_effort=reasoning_effort,
                idempotency_key=idempotency_key,
                max_runtime_seconds=(
                    int(max_runtime_seconds) if max_runtime_seconds is not None else None
                ),
                skills=tuple(skills or ()),
                created_by=created_by,
                initial_status=initial_status,
                triage=bool(triage),
                board=self.board_slug,
            )
            task = self._kb.get_task(conn, task_id)
            return _as_dict(task)

    def activate_task(self, task_id: str) -> dict[str, Any]:
        """Release a deliberately parked task through Hermes' parent gate.

        Hermes' native ``unblock_task`` lands the card in ``ready`` only when
        every parent is done; otherwise it remains ``todo``. This prevents a
        controller from accidentally bypassing dependency ordering.
        """
        self._validate_task_id(task_id)
        with self._connection() as conn:
            task = self._kb.get_task(conn, task_id)
            if task is None:
                raise HermesBoardError(f"Unknown task {task_id}")
            if task.status != "blocked":
                raise HermesBoardError(
                    f"Only a blocked task can be activated (task {task_id} is {task.status})"
                )
            if not self._kb.unblock_task(conn, task_id):
                raise HermesBoardError(f"Hermes could not activate task {task_id}")
            activated = self._kb.get_task(conn, task_id)
            if activated is None:
                raise HermesBoardError(f"Task {task_id} disappeared during activation")
            return _as_dict(activated)

    def retire_machine_card(self, task_id: str) -> bool:
        """Archive a resolved controller gate card without marking work done."""
        self._validate_task_id(task_id)
        with self._connection() as conn:
            task = self._kb.get_task(conn, task_id)
            if task is None or task.assignee != self.CONTROLLER_ASSIGNEE:
                raise HermesBoardError(f"Not a controller-owned card: {task_id}")
            return bool(self._kb.archive_task(conn, task_id))

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        self._validate_task_id(task_id)
        with self._connection() as conn:
            task = self._kb.get_task(conn, task_id)
            return _as_dict(task) if task is not None else None

    def list_tasks(
        self,
        *,
        assignee: Optional[str] = None,
        status: Optional[str] = None,
        tenant: Optional[str] = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._connection() as conn:
            tasks = self._kb.list_tasks(
                conn,
                assignee=assignee,
                status=status,
                tenant=tenant,
                include_archived=include_archived,
                limit=int(limit),
            )
            return [_as_dict(task) for task in tasks]

    def get_runs(self, task_id: str) -> list[dict[str, Any]]:
        """Return native attempt rows for evidence-to-run verification."""
        self._validate_task_id(task_id)
        with self._connection() as conn:
            return [_as_dict(run) for run in self._kb.list_runs(conn, task_id)]

    def configure_waiting_task(self, task_id, *, model, provider, reasoning_effort):
        """Pin a quota-selected model before a parked card becomes dispatchable."""
        self._validate_task_id(task_id)
        with self._connection() as conn:
            task = self._kb.get_task(conn, task_id)
            if task is None or task.status != 'blocked':
                raise HermesBoardError('Model selection requires a blocked, unclaimed task')
            self._kb.set_model_override(conn, task_id, model, provider)
            self._kb.set_reasoning_effort(conn, task_id, reasoning_effort)

    def set_waiting_task_runtime(self, task_id, max_runtime_seconds):
        """Refresh a parked task's trusted runtime cap before it can be claimed."""
        self._validate_task_id(task_id)
        if type(max_runtime_seconds) is not int or max_runtime_seconds < 1:
            raise ValueError("max_runtime_seconds must be a positive integer")
        with self._connection() as conn:
            with self._kb.write_txn(conn):
                task = self._kb.get_task(conn, task_id)
                if task is None or task.status != "blocked":
                    raise HermesBoardError("Runtime configuration requires a blocked, unclaimed task")
                conn.execute(
                    "UPDATE tasks SET max_runtime_seconds = ? WHERE id = ?",
                    (max_runtime_seconds, task_id),
                )
                append_event = getattr(self._kb, "_append_event", None)
                if not callable(append_event):
                    raise HermesBoardError("Hermes kanban_db has no audited task-event writer")
                append_event(conn, task_id, "runtime_limit_updated", {
                    "max_runtime_seconds": max_runtime_seconds,
                })
            notify = getattr(self._kb, "notify_task_updated", None)
            if callable(notify):
                notify(conn, task_id, ("max_runtime_seconds",), board=self.board_slug)

    def add_comment(self, task_id: str, author: str, body: str) -> int:
        self._validate_task_id(task_id)
        with self._connection() as conn:
            return int(self._kb.add_comment(conn, task_id, author, body))

    def link_tasks(self, parent_id: str, child_id: str) -> None:
        self._validate_task_id(parent_id)
        self._validate_task_id(child_id)
        with self._connection() as conn:
            self._kb.link_tasks(conn, parent_id, child_id)

    def list_events(self, task_id: str) -> list[dict[str, Any]]:
        self._validate_task_id(task_id)
        with self._connection() as conn:
            return [_as_dict(event) for event in self._kb.list_events(conn, task_id)]

    def machine_card(
        self,
        title: str,
        body: Optional[str] = None,
        *,
        parents: Iterable[str] = (),
        priority: int = 0,
        idempotency_key: Optional[str] = None,
        max_runtime_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        """Create and atomically claim a non-spawnable controller-owned card.

        Hermes gateway dispatch ignores assignees that are not actual
        profiles. This card is explicitly claimed by the controller with a
        stable lock and run row, so its heartbeat/completion are visible and
        stale-claim recovery still applies. No profile named ``team-controller``
        is installed by the companion profile installer.
        """
        # The controller claim is intentionally non-spawnable. If an operator
        # creates a profile with this reserved name, the gateway could race our
        # explicit claim and execute controller work as an ordinary worker.
        if (self.hermes_root / "profiles" / self.CONTROLLER_ASSIGNEE).exists():
            raise HermesBoardError(
                f"Reserved controller profile exists: "
                f"{self.hermes_root / 'profiles' / self.CONTROLLER_ASSIGNEE}"
            )
        task = self.create_task(
            title=title,
            assignee=self.CONTROLLER_ASSIGNEE,
            body=body,
            parents=parents,
            priority=priority,
            idempotency_key=idempotency_key,
            max_runtime_seconds=max_runtime_seconds,
            created_by=self.controller_id,
        )
        task_id = task["id"]
        claimer = self._machine_claimer(task_id)
        with self._connection() as conn:
            current = self._kb.get_task(conn, task_id)
            if current is None:
                raise HermesBoardError(f"Hermes did not persist task {task_id}")
            if current.assignee != self.CONTROLLER_ASSIGNEE:
                raise HermesBoardError(f"Idempotency key {idempotency_key!r} belongs to a non-controller task")
            if current.status == "ready":
                claimed = self._kb.claim_task(conn, task_id, claimer=claimer)
                if claimed is None:
                    current = self._kb.get_task(conn, task_id)
                else:
                    current = claimed
            if current is None or current.status != "running" or current.claim_lock != claimer:
                raise HermesBoardError(
                    f"Controller card {task_id} is not running under this controller's claim "
                    f"(status={getattr(current, 'status', None)!r}, "
                    f"claim_lock={getattr(current, 'claim_lock', None)!r})"
                )
            self._validate_machine_run(conn, task_id, current, claimer)
            return _as_dict(current)

    def complete_external(
        self,
        task_id: str,
        summary: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Complete only the controller's own active run (CAS by run id)."""
        with self._owned_machine_run(task_id) as (conn, task):
            ok = self._kb.complete_task(
                conn,
                task_id,
                summary=summary,
                result=summary,
                metadata=dict(metadata or {}),
                expected_run_id=task.current_run_id,
            )
            return bool(ok)

    def block_external(self, task_id: str, reason: str) -> bool:
        with self._owned_machine_run(task_id) as (conn, task):
            return bool(self._kb.block_task(
                conn,
                task_id,
                reason=reason,
                kind="needs_input",
                expected_run_id=task.current_run_id,
            ))

    def heartbeat(self, task_id: str, note: Optional[str] = None) -> bool:
        """Refresh both claim TTL and human-visible heartbeat event."""
        with self._owned_machine_run(task_id) as (conn, task):
            claim_ok = self._kb.heartbeat_claim(
                conn, task_id, claimer=self._machine_claimer(task_id)
            )
            if not claim_ok:
                return False
            return bool(self._kb.heartbeat_worker(
                conn,
                task_id,
                note=note,
                expected_run_id=task.current_run_id,
            ))

    @contextmanager
    def _owned_machine_run(self, task_id: str):
        self._validate_task_id(task_id)
        conn = self._kb.connect(board=self.board_slug)
        try:
            task = self._kb.get_task(conn, task_id)
            expected_claimer = self._machine_claimer(task_id)
            if (
                task is None
                or task.assignee != self.CONTROLLER_ASSIGNEE
                or task.status != "running"
                or task.claim_lock != expected_claimer
                or not task.current_run_id
            ):
                raise HermesBoardError(
                    f"Controller does not own active run for task {task_id}"
                )
            self._validate_machine_run(conn, task_id, task, expected_claimer)
            yield conn, task
        finally:
            conn.close()

    def _validate_machine_run(self, conn: Any, task_id: str, task: Any, claimer: str) -> None:
        """Verify task-row ownership agrees with Hermes' active run record."""
        run = self._kb.get_run(conn, int(task.current_run_id)) if task.current_run_id else None
        if (
            run is None
            or run.task_id != task_id
            or run.profile != self.CONTROLLER_ASSIGNEE
            or run.status != "running"
            or run.claim_lock != claimer
        ):
            raise HermesBoardError(
                f"Controller run record does not match the active claim for task {task_id}"
            )

    def _machine_claimer(self, task_id: str) -> str:
        return f"{self.controller_id}:{task_id}"

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
            raise ValueError("task_id is not a canonical Hermes task id")
