#!/usr/bin/env python3
"""Stage and launch the fixed T-008 demo operator job; accepts IDs only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Iterable

import backend_test_bridge as staging
import isolated_demo_walkthrough as demo


RUNNER = Path(__file__).with_name("isolated_demo_walkthrough.py")
MAX_OUTPUT_BYTES = 128 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_LOG_BYTES = 8 * 1024 * 1024
RESULT_LINE_RE = re.compile(r"^RESULT_JSON=(.+)$", re.MULTILINE)


class _RunnerCancelled(Exception):
    def __init__(self, signum: int):
        self.signum = signum


def _forward_cancel(process: subprocess.Popen[bytes], signum: int, grace_seconds: int = 120) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        staging._terminate_process_tree(process.pid)
        process.wait()


def _drain_after_exit(process: subprocess.Popen[bytes], tail: bytearray) -> None:
    """Collect the runner's final result-path line after graceful signal cleanup."""
    if process.stdout is None:
        return
    while True:
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            return
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        tail.extend(chunk)
        if len(tail) > MAX_OUTPUT_BYTES:
            del tail[:-MAX_OUTPUT_BYTES]


def _finish_cancelled_runner(process: subprocess.Popen[bytes], signum: int,
                            tail: bytearray) -> tuple[int, str]:
    _forward_cancel(process, signum)
    _drain_after_exit(process, tail)
    return process.returncode or 2, tail.decode("utf-8", "replace")


class BridgeError(RuntimeError):
    """A trusted snapshot, runner, or published output could not be verified."""


def build_runner_argv(job: demo.DemoJob) -> list[str]:
    if job != demo.DemoJob(demo.REQUEST_ID, demo.TASK_ID, demo.COMMIT_SHA):
        raise BridgeError("demo job differs from the reviewed T-008 candidate")
    return ["/usr/bin/python3", str(RUNNER), "--request", job.request_id,
            "--task", job.task_id, "--sha", job.sha]


def _run_runner(job: demo.DemoJob) -> tuple[int, str]:
    process: subprocess.Popen[bytes] | None = None
    previous_handlers = {}

    def cancel_handler(signum, _frame):
        raise _RunnerCancelled(signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, cancel_handler)
    try:
        proc = subprocess.Popen(build_runner_argv(job), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True, bufsize=0,
                                env={"PATH":"/usr/bin:/bin","LANG":"C.UTF-8","LC_ALL":"C.UTF-8"})
        process = proc
    except OSError as exc:
        raise BridgeError("isolated demo runner could not be started") from exc
    assert proc.stdout is not None
    tail = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + demo.MAX_DEMO_SECONDS + 300
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _forward_cancel(proc, signal.SIGTERM)
                raise BridgeError("isolated demo exceeded its hard runtime limit")
            for key, _ in selector.select(timeout=min(1.0, remaining)):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                tail.extend(chunk)
                if len(tail) > MAX_OUTPUT_BYTES:
                    del tail[:-MAX_OUTPUT_BYTES]
        return proc.wait(), tail.decode("utf-8", "replace")
    except _RunnerCancelled as cancelled:
        if process is not None:
            # The runner's SIGTERM handler writes a bounded failure result
            # after its own cleanup. Publish that result through the ordinary
            # validation path instead of discarding it here.
            return _finish_cancelled_runner(process, cancelled.signum, tail)
        raise BridgeError(f"isolated demo cancelled by operator (signal {cancelled.signum})")
    except BaseException:
        if process is not None and process.poll() is None:
            staging._terminate_process_tree(process.pid)
            process.wait()
        raise
    finally:
        selector.close()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _result_dir(job: demo.DemoJob, output: str) -> Path:
    matches = RESULT_LINE_RE.findall(output)
    if len(matches) != 1:
        raise BridgeError("demo runner did not return exactly one result path")
    reported = Path(matches[0])
    parent = staging.RESULT_ROOT / job.request_id / job.task_id / job.sha
    if reported.name != "result.json" or reported.parent.parent != parent:
        raise BridgeError("demo result path is outside its fixed run directory")
    run_id = reported.parent.name
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", run_id):
        raise BridgeError("demo result has an invalid run identifier")
    staging._validate_fixed_path(reported, staging.RESULT_ROOT, "demo result JSON")
    return reported.parent


def _copy_file(source: Path, target: Path, limit: int) -> dict[str, object]:
    if source.is_symlink() or not source.is_file() or source.stat().st_size > limit:
        raise BridgeError("demo result file is missing, unsafe, or too large")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as src, target.open("xb") as dst:
        while chunk := src.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise BridgeError("demo result changed while being copied")
            digest.update(chunk)
            dst.write(chunk)
    target.chmod(0o600)
    if total != source.stat().st_size:
        raise BridgeError("demo result changed while being copied")
    return {"name":target.name,"bytes":total,"sha256":digest.hexdigest()}


def publish_result(job: demo.DemoJob, exit_code: int, result_dir: Path) -> Path:
    result_file = result_dir / "result.json"
    staging._validate_fixed_path(result_file, staging.RESULT_ROOT, "demo result JSON")
    if result_file.stat().st_size > MAX_RESULT_BYTES:
        raise BridgeError("demo result JSON exceeds its size limit")
    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BridgeError("demo result JSON is invalid") from exc
    if (not isinstance(data, dict) or data.get("request_id") != job.request_id
            or data.get("task_id") != job.task_id or data.get("sha") != job.sha
            or type(data.get("exit_code")) is not int or data["exit_code"] != exit_code
            or type(data.get("passed")) is not bool or data["passed"] != (exit_code == 0)):
        raise BridgeError("demo runner result does not match the requested candidate")
    run_id = result_dir.name
    destination = staging.PUBLISHED_RESULT_ROOT / job.request_id / job.task_id / job.sha / f"demo-{run_id}"
    staging._ensure_fixed_directory(destination.parent, staging.PUBLISHED_RESULT_ROOT)
    if destination.exists() or destination.is_symlink():
        raise BridgeError("demo result destination already exists")
    destination.mkdir(mode=0o700)
    try:
        files = [_copy_file(result_file, destination / "result.json", MAX_RESULT_BYTES)]
        log_file = result_dir / "demo.log"
        files.append(_copy_file(log_file, destination / "demo.log", MAX_LOG_BYTES))
        for name in ("api-demo.json", "compose-config.json", "backend-build.log", "image-build.log",
                     "backend-startup.log", "smoke.log"):
            source = result_dir / name
            if source.exists():
                files.append(_copy_file(source, destination / name, MAX_LOG_BYTES))
        manifest = {"schema_version":1,"request_id":job.request_id,"task_id":job.task_id,
                    "sha":job.sha,"run_id":run_id,"kind":"isolated-compose-api-demo",
                    "runner_exit_code":exit_code,"files":sorted(files,key=lambda row:str(row["name"]))}
        manifest_path = destination / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_path.chmod(0o600)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def publish_bridge_failure(job: demo.DemoJob, message: str) -> Path:
    """Publish a truthful bounded artifact when setup failed before the runner."""
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    destination = staging.PUBLISHED_RESULT_ROOT / job.request_id / job.task_id / job.sha / f"demo-{run_id}"
    staging._ensure_fixed_directory(destination.parent, staging.PUBLISHED_RESULT_ROOT)
    destination.mkdir(mode=0o700)
    result = {"request_id":job.request_id,"task_id":job.task_id,"sha":job.sha,
              "run_id":run_id,"mode":"isolated-compose-api-demo","passed":False,
              "exit_code":2,"error":message[:1000],"finished_at_epoch":time.time(),"log":"demo.log"}
    log = f"Bridge stopped before a complete demo result was available: {message[:1000]}\n"
    try:
        files = []
        for name, content in (("result.json", json.dumps(result, indent=2, sort_keys=True) + "\n"),
                              ("demo.log", log)):
            path = destination / name
            path.write_text(content, encoding="utf-8")
            path.chmod(0o600)
            files.append({"name":name,"bytes":path.stat().st_size,
                          "sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
        manifest = {"schema_version":1,"request_id":job.request_id,"task_id":job.task_id,
                    "sha":job.sha,"run_id":run_id,"kind":"isolated-compose-api-demo",
                    "runner_exit_code":2,"files":sorted(files,key=lambda row:row["name"])}
        path = destination / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def run_job(job: demo.DemoJob) -> int:
    # Git bundle staging invokes the trusted backend bridge. Private modes must
    # apply even when the systemd unit or manual caller has a permissive umask.
    previous_umask = os.umask(0o077)
    try:
        stage_job = staging.Job(job.request_id, job.task_id, job.sha, "full")
        export_parent = staging.EXPORT_ROOT / job.request_id / job.task_id / job.sha
        staging._ensure_fixed_directory(staging.EXPORT_ROOT, staging.TEAM_DATA)
        staging._ensure_fixed_directory(export_parent, staging.EXPORT_ROOT)
        bundle = export_parent / f"{job.request_id}-{job.task_id}-{job.sha}-{uuid.uuid4().hex[:16]}.bundle"
        metadata = staging._run_helper(stage_job, bundle)
        staging._stage_bundle(stage_job, bundle, metadata)
        staging.stage_gradle_cache()
        exit_code, output = _run_runner(job)
        result_dir = _result_dir(job, output)
        published = publish_result(job, exit_code, result_dir)
        print(f"PUBLISHED_RESULT_JSON={published / 'result.json'}", flush=True)
        return exit_code
    except BaseException as exc:
        published = publish_bridge_failure(job, f"{exc.__class__.__name__}: {exc}")
        print(f"PUBLISHED_RESULT_JSON={published / 'result.json'}", flush=True)
        return 2
    finally:
        os.umask(previous_umask)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, dest="request_id")
    parser.add_argument("--task", required=True, dest="task_id")
    parser.add_argument("--sha", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        job = demo.parse_job(args.request_id, args.task_id, args.sha)
        return run_job(job)
    except (BridgeError, staging.BridgeError, demo.DemoError, OSError) as exc:
        print(f"demo-walkthrough-bridge: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
