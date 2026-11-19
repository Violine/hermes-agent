"""Durable model-worker suspension while a separately supervised test job runs.

This is a scheduling boundary, not a passing gate: every job result, including
failure, goes back to the assigned role for interpretation. No verdict is edited.
Only completed native attempts with a matching explicit handoff can suspend.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from .contracts import ContractError

WAITING = {"accounting_pending", "waiting"}
TERMINAL_JOBS = {"completed", "interrupted", "rejected", "failed", "timed_out", "cancelled"}
MAX_WAITS = 12
MAX_WAIT_SECONDS = 24 * 60 * 60
CONFIG = Path("/home/alex/.hermes/team/config.json")


def small_json(path, limit=16384):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ContractError("Test wait artifact must be a small regular file")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ContractError("Test wait artifact exceeds its size limit")
        return json.loads(raw)
    finally:
        os.close(fd)


def read_wait(path):
    value = small_json(path)
    expected = {"schema_version", "card_id", "run_id", "assignee", "kind", "job_id", "sha"}
    if (not isinstance(value, dict) or set(value) != expected
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["kind"] not in {"backend", "checks"}):
        raise ContractError("Unsupported test wait handoff")
    for field, pattern in (("card_id", r"t_[0-9a-f]{8,}"), ("run_id", r"[0-9]+"),
                           ("assignee", r"team-[a-z][a-z0-9-]{0,47}"),
                           ("job_id", r"[0-9a-f]{64}"), ("sha", r"[0-9a-f]{40}")):
        if not isinstance(value[field], str) or not re.fullmatch(pattern, value[field]):
            raise ContractError("Invalid test wait " + field)
    return value


class TestWaitsMixin:
    def test_job_snapshot(self, kind, job_id):
        if kind == "backend":
            from .backend_test_jobs import read_job
            queue = self.root / "backend-test-jobs"
        elif kind == "checks":
            from .check_jobs import read_job
            queue = self.root / "trusted-check-jobs"
        else:
            raise ContractError("Unknown test job kind")
        return read_job(job_id, queue=queue, tail_bytes=0)

    def _test_wait_workspace(self, state, card_id):
        for item in state.get("items", []):
            for key, workspace in (("card", "workspace"), ("reviewer_card", "reviewer_workspace"),
                                   ("qa_card", "qa_workspace"), ("integrator_card", "integrator_workspace")):
                if item.get(key) == card_id:
                    return item, Path(item[workspace])
        raise ContractError("Test wait card is not bound to a current implementation item")

    def _validate_test_job(self, state, handoff):
        from .controller import git, require_clean
        item, workspace = self._test_wait_workspace(state, handoff["card_id"])
        job = self.test_job_snapshot(handoff["kind"], handoff["job_id"])
        if any(job.get(key) != expected for key, expected in (
                ("request", state["id"]), ("task", item["definition"]["id"]),
                ("sha", handoff["sha"]), ("job_id", handoff["job_id"]))):
            raise ContractError("Test job identity differs from the worker handoff")
        if handoff["kind"] == "checks" and (
                job.get("card") != handoff["card_id"] or str(job.get("run")) != handoff["run_id"]):
            raise ContractError("Trusted check job belongs to another native attempt")
        require_clean(workspace)
        if git(workspace, "rev-parse", "HEAD") != handoff["sha"]:
            raise ContractError("Test wait HEAD changed; old test evidence cannot resume this worker")
        if item.get("head_sha") and handoff["card_id"] != item.get("card"):
            if item["head_sha"] != handoff["sha"]:
                raise ContractError("Test wait does not refer to the pinned review candidate")
        return job

    def _charge_test_wait_run(self, state, entry):
        """Charge exact native-loop counters once; never include the parked interval."""
        usage_path = self.root / "worker-budget" / entry["card_id"] / (entry["run_id"] + ".json")
        if usage_path.parent.is_symlink() or usage_path.parent.parent.is_symlink():
            raise ContractError("Unsafe test wait budget directory")
        try:
            usage = small_json(usage_path)
        except FileNotFoundError:
            if time.time() - entry["registered_at"] > 120:
                raise ContractError("Worker budget accounting did not finalize before test wait")
            return False
        if (not isinstance(usage, dict) or usage.get("finalized") is not True
                or any(str(usage.get(key)) != str(entry[key])
                       for key in ("card_id", "run_id", "assignee"))):
            raise ContractError("Test wait budget accounting has invalid native bindings")
        seconds, turns = usage.get("active_seconds"), usage.get("iterations")
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or seconds < 0
                or type(turns) is not int or turns < 1):
            raise ContractError("Test wait budget accounting has invalid counters")
        settings = self.config["roles"][entry["assignee"]]
        snapshot = state.get("assignment_snapshots", {}).get(entry["card_id"], {})
        seconds_limit = usage.get("budget_seconds", snapshot.get("run_budget", settings.get("run_budget", 600)))
        turns_limit = usage.get("max_turns", snapshot.get("max_turns", settings.get("max_turns", 40)))
        if (isinstance(seconds_limit, bool) or not isinstance(seconds_limit, (int, float))
                or not math.isfinite(seconds_limit) or seconds_limit <= 0
                or type(turns_limit) is not int or turns_limit < 1):
            raise ContractError("Test wait native allowance snapshot is invalid")
        book = state.setdefault("test_wait_budgets", {}).setdefault(entry["card_id"], {
            "card_id": entry["card_id"], "assignee": entry["assignee"],
            "budget_seconds": seconds_limit,
            "max_turns": turns_limit, "charged_runs": {},
        })
        book["charged_runs"].setdefault(entry["run_id"], {
            "active_seconds": seconds, "iterations": turns,
            "api_call_count": usage.get("api_call_count"),
        })
        book["used_seconds"] = sum(row["active_seconds"] for row in book["charged_runs"].values())
        book["used_turns"] = sum(row["iterations"] for row in book["charged_runs"].values())
        book["remaining_seconds"] = max(0, math.floor(book["budget_seconds"] - book["used_seconds"]))
        book["remaining_turns"] = max(0, book["max_turns"] - book["used_turns"])
        book["latest_wait_run_id"] = entry["run_id"]
        entry["status"] = "waiting"
        return True

    def reconcile_test_waits(self, state):
        """Register terminal handoffs; wake the same card once its exact job ends."""
        self.test_waiting_cards = set()
        if state.get("stage") in {"done", "cancelled"}:
            return
        waits = state.get("test_waits", {})
        current = set(self.current_worker_cards(state))
        for card_id in current:
            directory = state.get("prerequisite_paths", {}).get(card_id)
            if not directory:
                continue
            directory = Path(directory)
            expected_root = self.path(state) / "prerequisites"
            if (directory.parent != expected_root or directory.is_symlink()
                    or expected_root.is_symlink()):
                raise ContractError("Invalid controller test wait directory")
            path = directory / "TEST-WAIT.json"
            if not path.exists() and not path.is_symlink():
                continue
            handoff = read_wait(path)
            task = self.board.get_task(card_id)
            if handoff["card_id"] != card_id or handoff["assignee"] != task.get("assignee"):
                raise ContractError("Test wait handoff belongs to another card or profile")
            runs = self.board.get_runs(card_id)
            if not runs:
                continue
            latest = max(runs, key=lambda row: int(row.get("id", row.get("run_id"))))
            if str(latest.get("id", latest.get("run_id"))) != handoff["run_id"]:
                continue  # A prior attempt's handoff is never attached to its successor.
            if (task.get("status") != "blocked" or task.get("block_kind") != "transient"
                    or task.get("current_run_id") is not None or task.get("claim_lock")
                    or task.get("worker_pid") or latest.get("profile") != handoff["assignee"]
                    or latest.get("status") != "blocked" or not latest.get("ended_at")):
                continue
            key = card_id + ":" + handoff["run_id"]
            if key in waits:
                continue
            if len([row for row in waits.values() if row["card_id"] == card_id]) >= MAX_WAITS:
                raise ContractError("Too many test suspensions for this card; inspect repeated failures")
            job = self._validate_test_job(state, handoff)
            entry = {**handoff, "status": "accounting_pending", "registered_at": time.time(),
                     "started_at": latest["ended_at"], "job_status": job.get("status"),
                     "updated_at": time.time()}
            waits = state.setdefault("test_waits", {})
            waits[key] = entry
            record = state.get("dispatch", {}).get(card_id)
            if record:
                entry["previous_quota_key"] = record["key"]
                record["armed"] = False
                record["waiting"] = False
            self.save(state)  # Persist the handoff before releasing resources.
            if self.quota and record:
                self.quota.release(entry["previous_quota_key"])
            self.board.add_comment(card_id, "team-controller",
                "Ожидает проверку отдельным заданием: " + entry["kind"] + ":" + entry["job_id"]
                + ". Слот модели освобождён; ожидание не расходует активный бюджет.")

        for entry in waits.values():
            card_id = entry["card_id"]
            if card_id not in current or entry["status"] in {"resumed", "failed"}:
                continue
            task = self.board.get_task(card_id)
            if entry["status"] == "resuming":
                native_runs = self.board.get_runs(card_id)
                latest = max(native_runs, key=lambda row: int(row.get("id", row.get("run_id"))))
                latest_id = str(latest.get("id", latest.get("run_id")))
                if latest_id != entry["run_id"]:
                    entry["status"] = "resumed"
                    entry["resumed_at"] = latest.get("started_at", time.time())
                    dispatch = state.get("dispatch", {}).get(card_id)
                    if dispatch:
                        dispatch["waiting"] = False
                    continue
                if task.get("status") == "blocked" and state.get("stage") != "blocked":
                    job = self._validate_test_job(state, entry)
                    if job.get("status") not in TERMINAL_JOBS:
                        raise ContractError("Saved test resume intent has no terminal test result")
                    if (task.get("block_kind") != "transient" or task.get("current_run_id") is not None
                            or task.get("claim_lock") or task.get("worker_pid")
                            or latest.get("profile") != entry["assignee"]
                            or latest.get("status") != "blocked" or not latest.get("ended_at")):
                        raise ContractError("Saved test resume intent no longer owns the blocked attempt")
                    if not self.quota:
                        self.activate_with_quota(state, card_id)
                        task = self.board.get_task(card_id)
                if task.get("status") in {"ready", "todo", "running", "done"}:
                    entry["status"] = "resumed"
                    entry["resumed_at"] = time.time()
                continue
            if task.get("status") != "blocked":
                raise ContractError("A waiting test worker was reactivated outside its controller")
            self.test_waiting_cards.add(card_id)
            if entry["status"] == "accounting_pending" and not self._charge_test_wait_run(state, entry):
                continue
            if time.time() - entry["registered_at"] > MAX_WAIT_SECONDS:
                raise ContractError("Test wait exceeded its independent 24-hour watchdog")
            job = self._validate_test_job(state, entry)
            entry["job_status"] = job.get("status")
            entry["updated_at"] = time.time()
            if job.get("status") not in TERMINAL_JOBS:
                continue
            book = state["test_wait_budgets"][card_id]
            if book["remaining_seconds"] < 15 or book["remaining_turns"] < 1:
                entry["status"] = "failed"
                raise ContractError("Active worker allowance is exhausted after test suspension; "
                                    "test waiting was excluded, but more active work needs review")
            # Never hide another root blocker or cross an owner approval gate.
            if state.get("stage") == "blocked":
                continue
            entry["status"] = "resuming"
            entry["completed_at"] = job.get("finished_at", time.time())
            entry["exit_code"] = job.get("exit_code")
            record = state.get("dispatch", {}).get(card_id)
            if record:
                record["key"] = f"{state['id']}:{card_id}:test-return:{entry['run_id']}"
                record["armed"] = True
                record["waiting"] = True
            result_path = self.root / ("backend-test-jobs" if entry["kind"] == "backend" else "trusted-check-jobs") / entry["job_id"]
            if not entry.get("result_comment_added"):
                checkpoint = Path(state["prerequisite_paths"][card_id]) / "TEST-CHECKPOINT.md"
                self.board.add_comment(card_id, "team-controller",
                    f"Проверка {entry['kind']}:{entry['job_id']} завершилась: {job.get('status')}, "
                    f"exit_code={job.get('exit_code')}. Результат и логи: {result_path}. "
                    f"Проверялся SHA {entry['sha']}. Продолжи ту же задачу в сохранённой рабочей копии, "
                    f"сначала прочитай назначение, TASK.md (если он есть), {checkpoint} и результат задания. "
                    "Ошибка теста требует разбора/исправления; она не считается успешной проверкой. "
                    "После изменения SHA предыдущие доказательства не засчитываются. "
                    "Обнови итоговый отчёт своим новым HERMES_KANBAN_RUN_ID. "
                    f"Осталось активных секунд: {book['remaining_seconds']}; итераций: {book['remaining_turns']}.")
                entry["result_comment_added"] = True
            self.save(state)  # Save the reduced allowance before native activation.
            self.test_waiting_cards.discard(card_id)
            if not self.quota:
                self.activate_with_quota(state, card_id)
            if self.board.get_task(card_id).get("status") in {"ready", "todo", "running"}:
                entry["status"] = "resumed"
                entry["resumed_at"] = time.time()
        state["waiting_for_tests"] = sorted(self.test_waiting_cards)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Save an explicit native-worker test wait handoff")
    parser.add_argument("--request", required=True)
    parser.add_argument("--kind", choices=("backend", "checks"), required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args(argv)
    from .controller import Controller, write_json
    controller = Controller(CONFIG)
    if not re.fullmatch(r"req-[0-9a-f]{20}", args.request):
        parser.error("Invalid request ID")
    state_path = controller.root / "runs" / args.request / "state.json"
    # State is atomically replaced by the controller. Do not wait on its long
    # regression lock: this command writes only the worker's separate handoff.
    state = small_json(state_path, 4 * 1024 * 1024)
    card_id = os.environ.get("HERMES_KANBAN_TASK", "")
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "")
    if card_id not in controller.current_worker_cards(state):
        raise ContractError("This is not an active implementation/review worker")
    card = controller.board.get_task(card_id)
    if (card.get("status") != "running" or str(card.get("current_run_id")) != run_id):
        raise ContractError("Worker no longer owns this native run")
    job = controller.test_job_snapshot(args.kind, args.job)
    handoff = {"schema_version": 1, "card_id": card_id, "run_id": run_id,
               "assignee": card["assignee"], "kind": args.kind,
               "job_id": args.job, "sha": job.get("sha")}
    controller._validate_test_job(state, handoff)
    directory = Path(state["prerequisite_paths"][card_id])
    path = directory / "TEST-WAIT.json"
    checkpoint = directory / "TEST-CHECKPOINT.md"
    if directory.is_symlink() or path.is_symlink() or checkpoint.is_symlink():
        raise ContractError("Unsafe test wait handoff path")
    if not checkpoint.is_file() or not 80 <= checkpoint.stat().st_size <= 32768:
        raise ContractError("Save a bounded TEST-CHECKPOINT.md before suspending the worker")
    write_json(path, handoff)
    print(json.dumps({"handoff": str(path), "next_action": "kanban_block",
                      "kind": "transient", "reason": "Ожидает проверку " + args.kind + ":" + args.job},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
