#!/usr/bin/env python3
"""Run the fixed CRM backend integration suite in an offline rootless sandbox.

This is a trusted host-side entry point. It intentionally accepts identifiers,
not commands or filesystem paths. The only container socket it exposes is the
dedicated rootless test daemon socket, and only inside bubblewrap.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import re
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import quote
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REQUEST_RE = re.compile(r"req-[0-9a-f]{20}\Z")
TASK_RE = re.compile(r"T-[0-9]{3}\Z")
SHA_RE = re.compile(r"[0-9a-f]{40}\Z")

HOME = Path("/home/alex")
RUNTIME_ROOT = HOME / ".local/share/hermes-test-runtime"
SNAPSHOT_ROOT = RUNTIME_ROOT / "snapshots"
CACHE_ROOT = RUNTIME_ROOT / "cache/gradle"
RESULT_ROOT = RUNTIME_ROOT / "results"
RUN_CACHE_ROOT = RUNTIME_ROOT / "run-cache"
JDK = HOME / ".hermes/toolchains/jdk-21"
BWRAP = HOME / ".hermes/toolchains/bubblewrap/usr/bin/bwrap"
NSENTER = Path("/usr/bin/nsenter")
ROOTLESS_RUNTIME = Path("/run/user/1000/hermes-test-runtime")
ROOTLESS_SOCKET = ROOTLESS_RUNTIME / "docker.sock"
ROOTLESS_STATE = HOME / ".cache/hermes-test-runtime/rootlesskit-state"
# Daemon /run is ephemeral: a namespaced PID must never persist across restarts.
ROOTLESS_PIDFILE = Path("/run/dockerd.pid")
ROOTLESS_DATA = HOME / ".cache/hermes-test-runtime/data"
ROOTLESS_EXEC = HOME / ".cache/hermes-test-runtime/docker-exec"
DAEMON_HIDDEN_HOST_PATHS = (
    HOME / "apps",
    HOME / ".hermes",
    HOME / ".ssh",
    RUNTIME_ROOT / "tools",
    Path("/var/run/docker.sock"),
)
MAX_RUN_SECONDS = 60 * 60


class RunnerError(RuntimeError):
    """Raised when a required isolation invariant cannot be established."""


@dataclass(frozen=True)
class Job:
    request_id: str
    task_id: str
    sha: str
    scope: str


def _require_identifier(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not pattern.fullmatch(value):
        raise RunnerError(f"invalid {label}")
    return value


def parse_job(request_id: str, task_id: str, sha: str, scope: str) -> Job:
    _require_identifier(request_id, REQUEST_RE, "request id")
    _require_identifier(task_id, TASK_RE, "task id")
    _require_identifier(sha, SHA_RE, "commit SHA")
    if scope not in {"salesleads", "full"}:
        raise RunnerError("scope must be 'salesleads' or 'full'")
    return Job(request_id, task_id, sha, scope)


def build_gradle_argv(scope: str) -> list[str]:
    if scope not in {"salesleads", "full"}:
        raise RunnerError("unsupported test scope")
    argv = [
        "./gradlew",
        "--offline",
        "--no-daemon",
        "-Pkotlin.compiler.execution.strategy=in-process",
        ":backend:integrationTest",
    ]
    if scope == "salesleads":
        argv.extend(["--tests", "com.crmplatform.integration.salesleads.*"])
    return argv


def _run_checked(argv: list[str], *, timeout: int = 20) -> str:
    try:
        completed = subprocess.run(
            argv,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env={"PATH": "/usr/bin:/bin", "HOME": str(HOME), "LANG": "C.UTF-8",
                 "LC_ALL": "C.UTF-8", "GIT_CONFIG_NOSYSTEM": "1"},
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f"required isolation check failed: {argv[0]}") from exc
    return completed.stdout.strip()


def _read_proc_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _netns_inode(pid: int) -> int | None:
    try:
        value = os.readlink(f"/proc/{pid}/ns/net")
    except OSError:
        return None
    match = re.fullmatch(r"net:\[(\d+)\]", value)
    return int(match.group(1)) if match else None


def find_rootless_namespace_pid() -> int:
    """Find the dedicated RootlessKit child and reject ambiguous candidates."""
    matches: list[int] = []
    own_netns = _netns_inode(os.getpid())
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        args = _read_proc_cmdline(pid)
        joined = "\0".join(args)
        if (
            "rootlesskit" not in joined
            or f"--state-dir={ROOTLESS_STATE}" not in joined
            or "--net=none" not in joined
            or "--port-driver=none" not in joined
        ):
            continue
        netns = _netns_inode(pid)
        if netns is not None and netns != own_netns:
            matches.append(pid)
    if len(matches) != 1:
        raise RunnerError(
            "expected exactly one dedicated rootless test namespace; "
            f"found {len(matches)}"
        )
    return matches[0]


def validate_dockerd_process(namespace_pid: int) -> int:
    """Bind the API socket to dockerd running inside the exact private netns."""
    expected = {
        f"--host=unix://{ROOTLESS_SOCKET}",
        f"--data-root={ROOTLESS_DATA}",
        f"--exec-root={ROOTLESS_EXEC}",
        f"--pidfile={ROOTLESS_PIDFILE}",
        "--storage-driver=vfs",
    }
    target_ns = _netns_inode(namespace_pid)
    matches: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        args = _read_proc_cmdline(pid)
        if args and Path(args[0]).name == "dockerd" and expected.issubset(set(args[1:])):
            if _netns_inode(pid) == target_ns:
                matches.append(pid)
    if len(matches) != 1:
        raise RunnerError("dedicated rootless dockerd is not uniquely present in its private netns")
    return matches[0]


def _decode_mountinfo_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def verify_daemon_filesystem_sandbox(pid: int) -> dict[str, object]:
    """Require dockerd's private mount/PID namespaces and hide sensitive host paths."""
    try:
        own_mount_ns = os.readlink("/proc/self/ns/mnt")
        daemon_mount_ns = os.readlink(f"/proc/{pid}/ns/mnt")
        own_pid_ns = os.readlink("/proc/self/ns/pid")
        daemon_pid_ns = os.readlink(f"/proc/{pid}/ns/pid")
        mountinfo = Path(f"/proc/{pid}/mountinfo").read_text(encoding="utf-8")
    except OSError as exc:
        raise RunnerError("cannot verify dedicated dockerd namespaces") from exc
    if daemon_mount_ns == own_mount_ns:
        raise RunnerError("dedicated dockerd is not in a separate filesystem namespace")
    if daemon_pid_ns == own_pid_ns:
        raise RunnerError("dedicated dockerd is not in a separate PID namespace")

    mounts: list[tuple[str, str, str]] = []
    for line in mountinfo.splitlines():
        try:
            before, after = line.split(" - ", 1)
            left = before.split()
            right = after.split()
            root = _decode_mountinfo_path(left[3])
            mountpoint = _decode_mountinfo_path(left[4])
            filesystem = right[0]
        except (IndexError, ValueError) as exc:
            raise RunnerError("dedicated dockerd mount table is malformed") from exc
        mounts.append((root, mountpoint, filesystem))

    root_mounts = [filesystem for _root, mountpoint, filesystem in mounts if mountpoint == "/"]
    if len(root_mounts) != 1 or root_mounts[0] != "tmpfs":
        raise RunnerError("dedicated dockerd root is not the private bubblewrap tmpfs")

    for source_root, mountpoint, _filesystem in mounts:
        for forbidden in DAEMON_HIDDEN_HOST_PATHS:
            forbidden_text = str(forbidden)
            for mounted_path in (source_root, mountpoint):
                if mounted_path == forbidden_text or mounted_path.startswith(forbidden_text.rstrip("/") + "/"):
                    raise RunnerError("dedicated dockerd mount table exposes a protected host path")

    process_root = Path(f"/proc/{pid}/root")
    for forbidden in DAEMON_HIDDEN_HOST_PATHS:
        try:
            os.stat(process_root / forbidden.relative_to("/"))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:
            raise RunnerError("cannot confirm a protected host path is hidden from dockerd") from exc
        raise RunnerError("dedicated dockerd can see a protected host path")

    return {
        "verified": True,
        "mount_namespace": daemon_mount_ns,
        "pid_namespace": daemon_pid_ns,
        "root_filesystem": root_mounts[0],
        "hidden_host_paths": [str(path) for path in DAEMON_HIDDEN_HOST_PATHS],
    }


def _contains_usable_ipv4_default_route(rows: Iterable[str]) -> bool:
    for row in rows:
        fields = row.split()
        if len(fields) < 8:
            raise RunnerError("IPv4 route table contains a malformed row")
        destination, flags, mask = fields[1], fields[3], fields[7]
        try:
            flag_bits = int(flags, 16)
        except ValueError as exc:
            raise RunnerError("IPv4 route table contains invalid route flags") from exc
        is_default = destination == "00000000" and mask == "00000000"
        is_usable = (flag_bits & 0x1) != 0 and (flag_bits & 0x200) == 0  # RTF_UP and not RTF_REJECT
        if is_default and is_usable:
            return True
    return False


def _contains_usable_ipv6_default_route(rows: Iterable[str]) -> bool:
    for row in rows:
        fields = row.split()
        if len(fields) < 10:
            raise RunnerError("IPv6 route table contains a malformed row")
        destination, prefix_length, flags = fields[0], fields[1], fields[8]
        try:
            flag_bits = int(flags, 16)
        except ValueError as exc:
            raise RunnerError("IPv6 route table contains invalid route flags") from exc
        is_default = destination == "0" * 32 and prefix_length == "00"
        is_usable = (flag_bits & 0x1) != 0 and (flag_bits & 0x200) == 0  # RTF_UP and not RTF_REJECT
        if is_default and is_usable:
            return True
    return False


def _namespace_route_check(pid: int) -> dict[str, bool]:
    # Linux exposes each process's network namespace routing table through proc.
    route_file = Path(f"/proc/{pid}/net/route")
    try:
        rows = route_file.read_text(encoding="ascii").splitlines()[1:]
    except OSError as exc:
        raise RunnerError("cannot inspect the test namespace routes") from exc
    if _contains_usable_ipv4_default_route(rows):
        raise RunnerError("test namespace has an IPv4 default route")
    try:
        ipv6_rows = Path(f"/proc/{pid}/net/ipv6_route").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise RunnerError("cannot inspect the test namespace IPv6 routes") from exc
    if _contains_usable_ipv6_default_route(ipv6_rows):
        raise RunnerError("test namespace has an IPv6 default route")
    return {"ipv4_default_route_absent": True, "ipv6_default_route_absent": True}


def _docker_api(sock_path: Path, path: str, *, method: str = "GET") -> tuple[int, bytes]:
    """Read a local Docker API response without using a CLI or network proxy."""
    if not path.startswith("/") or "\r" in path or "\n" in path:
        raise RunnerError("invalid internal Docker API path")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect(str(sock_path))
        request = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        client.sendall(request.encode("ascii"))
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise RunnerError("dedicated rootless Docker socket is unavailable") from exc
    finally:
        client.close()
    response = b"".join(chunks)
    try:
        header, body = response.split(b"\r\n\r\n", 1)
        status = header.split(b"\r\n", 1)[0]
        status_code = int(status.split()[1])
        # Docker may use chunked transfer encoding on unix sockets.
        if b"transfer-encoding: chunked" in header.lower():
            body = _decode_chunked(body)
        return status_code, body
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("dedicated Docker daemon returned an invalid response") from exc


def _http_docker_info(sock_path: Path) -> dict:
    status, body = _docker_api(sock_path, "/info")
    if status != 200:
        raise RunnerError("dedicated Docker /info request failed")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("dedicated Docker daemon returned invalid /info") from exc


def _decode_chunked(body: bytes) -> bytes:
    out = bytearray()
    while body:
        line, body = body.split(b"\r\n", 1)
        count = int(line.split(b";", 1)[0], 16)
        if count == 0:
            return bytes(out)
        out.extend(body[:count])
        body = body[count + 2 :]
    raise ValueError("truncated chunked response")


def validate_rootless_daemon(sock_path: Path = ROOTLESS_SOCKET) -> None:
    try:
        st = sock_path.lstat()
    except OSError as exc:
        raise RunnerError("dedicated rootless Docker socket is missing") from exc
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o007:
        raise RunnerError("dedicated Docker socket permissions are unsafe")
    info = _http_docker_info(sock_path)
    security = info.get("SecurityOptions", [])
    if not any("rootless" in str(option).lower() for option in security):
        raise RunnerError("Docker daemon does not report rootless security")
    if info.get("Driver") != "vfs":
        raise RunnerError("test daemon must use the vfs storage driver")
    if Path(str(info.get("DockerRootDir", ""))) != ROOTLESS_DATA:
        raise RunnerError("Docker daemon data root does not match the isolated test runtime")


def validate_postgres_image(sock_path: Path = ROOTLESS_SOCKET) -> None:
    status, _ = _docker_api(sock_path, "/images/postgres:18/json")
    if status != 200:
        raise RunnerError("local postgres:18 image is missing; offline runner will not pull it")


def _docker_json(path: str) -> object:
    status, body = _docker_api(ROOTLESS_SOCKET, path)
    if status != 200:
        raise RunnerError(f"dedicated Docker API request failed: {path}")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError(f"dedicated Docker API returned invalid JSON: {path}") from exc


def _resource_snapshot() -> dict[str, set[str]]:
    containers = _docker_json("/containers/json?all=1")
    networks = _docker_json("/networks")
    volumes_obj = _docker_json("/volumes")
    if not isinstance(containers, list) or not isinstance(networks, list) or not isinstance(volumes_obj, dict):
        raise RunnerError("dedicated Docker API returned an unexpected resource list")
    volumes = volumes_obj.get("Volumes") or []
    return {
        "containers": {str(item.get("Id")) for item in containers if isinstance(item, dict)},
        "networks": {str(item.get("Id")) for item in networks if isinstance(item, dict)},
        "volumes": {str(item.get("Name")) for item in volumes if isinstance(item, dict)},
    }


def _cleanup_created_resources(before: dict[str, set[str]]) -> list[str]:
    errors: list[str] = []
    try:
        after = _resource_snapshot()
    except RunnerError as exc:
        return [str(exc)]
    for container_id in sorted(after["containers"] - before["containers"]):
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            errors.append("unexpected container identifier from Docker API")
            continue
        status, _ = _docker_api(ROOTLESS_SOCKET, f"/containers/{container_id}?force=1&v=1", method="DELETE")
        if status not in {204, 200, 404}:
            errors.append(f"failed to remove test container {container_id[:12]}")
    try:
        after = _resource_snapshot()
    except RunnerError as exc:
        return errors + [str(exc)]
    for network_id in sorted(after["networks"] - before["networks"]):
        if not re.fullmatch(r"[0-9a-f]{12,64}", network_id):
            errors.append("unexpected network identifier from Docker API")
            continue
        status, _ = _docker_api(ROOTLESS_SOCKET, f"/networks/{network_id}", method="DELETE")
        if status not in {204, 200, 404}:
            errors.append(f"failed to remove test network {network_id[:12]}")
    for name in sorted(after["volumes"] - before["volumes"]):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
            errors.append("unexpected volume name from Docker API")
            continue
        status, _ = _docker_api(ROOTLESS_SOCKET, f"/volumes/{quote(name, safe='')}", method="DELETE")
        if status not in {204, 200, 404}:
            errors.append(f"failed to remove test volume {name[:48]}")
    return errors


@contextmanager
def _exclusive_runner_lock():
    lock_path = RUNTIME_ROOT / "runner.lock"
    with lock_path.open("a", encoding="ascii") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _git(snapshot: Path, *args: str) -> str:
    return _run_checked(["/usr/bin/git", "-C", str(snapshot), *args])


def validate_snapshot(job: Job) -> Path:
    expected = SNAPSHOT_ROOT / job.request_id / job.task_id / job.sha / "repo"
    _assert_private_chain(SNAPSHOT_ROOT, "snapshot staging root", RUNTIME_ROOT)
    cursor = SNAPSHOT_ROOT
    for segment in (job.request_id, job.task_id, job.sha, "repo"):
        cursor = cursor / segment
        if cursor.is_symlink():
            raise RunnerError("snapshot path must not contain symlinks")
    try:
        repo = expected.resolve(strict=True)
        root = SNAPSHOT_ROOT.resolve(strict=True)
    except OSError as exc:
        raise RunnerError("committed snapshot is missing") from exc
    if not repo.is_relative_to(root):
        raise RunnerError("snapshot escaped the trusted staging directory")
    if not repo.is_dir() or (repo / ".git").is_symlink() or not (repo / ".git").is_dir():
        raise RunnerError("snapshot must have independent Git metadata")
    git_dir_path = repo / ".git"
    if any(path.is_symlink() for path in git_dir_path.rglob("*")):
        raise RunnerError("snapshot Git metadata contains symlinks")
    if (git_dir_path / "objects/info/alternates").exists():
        raise RunnerError("snapshot Git object store must not use external alternates")
    actual_sha = _git(repo, "rev-parse", "HEAD")
    if actual_sha != job.sha:
        raise RunnerError("snapshot HEAD does not match requested SHA")
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise RunnerError("staged snapshot is not clean")
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir")).resolve(strict=True)
    if not git_dir.is_relative_to(repo):
        raise RunnerError("snapshot Git metadata is external to the staged repository")
    top_level = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top_level != repo:
        raise RunnerError("snapshot Git worktree root does not match the staged repository")
    _assert_private_directory(repo, "snapshot repository")
    git_metadata = (repo / ".git").stat()
    if git_metadata.st_uid != os.getuid() or stat.S_IMODE(git_metadata.st_mode) & 0o022:
        raise RunnerError("snapshot Git metadata must be owned by the runner and not group/world writable")
    _assert_private_chain(expected, "snapshot path", SNAPSHOT_ROOT)
    return repo


def _assert_plain_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise RunnerError(f"{label} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RunnerError(f"{label} is missing") from exc
    if not resolved.is_dir():
        raise RunnerError(f"{label} is not a directory")
    return resolved


def _assert_private_directory(path: Path, label: str) -> Path:
    resolved = _assert_plain_directory(path, label)
    info = resolved.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RunnerError(f"{label} must be owned by the runner and private (mode 0700)")
    return resolved


def _assert_private_chain(path: Path, label: str, stop: Path = RUNTIME_ROOT) -> Path:
    root = _assert_private_directory(stop, "test runtime root")
    if path == stop:
        return root
    try:
        relative = path.relative_to(stop)
    except ValueError as exc:
        raise RunnerError(f"{label} is outside the private test runtime") from exc
    current = stop
    for part in relative.parts:
        current = current / part
        _assert_private_directory(current, label)
    return current.resolve(strict=True)


def _create_private_tree(path: Path, label: str, stop: Path = RUNTIME_ROOT) -> Path:
    _assert_private_chain(stop, "test runtime root")
    try:
        relative = path.relative_to(stop)
    except ValueError as exc:
        raise RunnerError(f"{label} is outside the private test runtime") from exc
    current = stop
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RunnerError(f"{label} contains a symlink")
        if not current.exists():
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise RunnerError(f"cannot create private {label}") from exc
        _assert_private_directory(current, label)
    return current.resolve(strict=True)


def _copy_cache(destination: Path) -> None:
    _assert_private_chain(CACHE_ROOT, "offline Gradle cache")
    source = _assert_plain_directory(CACHE_ROOT, "offline Gradle cache")
    allowed = {"wrapper", "caches"}
    if not {child.name for child in source.iterdir()} <= allowed:
        raise RunnerError("offline Gradle cache contains unsupported entries")
    for child in source.rglob("*"):
        if child.is_symlink():
            raise RunnerError("offline Gradle cache contains a symlink")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in sorted(allowed):
        item = source / name
        if item.exists():
            if not item.is_dir() or item.is_symlink():
                raise RunnerError("offline Gradle cache layout is invalid")
            shutil.copytree(item, destination / name, symlinks=False)


def _write_minimal_etc(etc: Path) -> None:
    etc.mkdir(mode=0o700)
    values = {
        "passwd": "root:x:0:0:root:/root:/bin/sh\nsandbox:x:1000:1000:Sandbox:/tmp/home:/bin/sh\n",
        "group": "root:x:0:\nsandbox:x:1000:\n",
        "hosts": "127.0.0.1 localhost\n::1 localhost ip6-localhost ip6-loopback\n",
        "hostname": "hermes-test\n",
        "resolv.conf": "nameserver 127.0.0.1\noptions attempts:1 timeout:1\n",
        "nsswitch.conf": "passwd: files\ngroup: files\nhosts: files dns\n",
    }
    for name, contents in values.items():
        target = etc / name
        target.write_text(contents, encoding="ascii")
        target.chmod(0o444)


def build_bwrap_argv(workspace: Path, gradle_home: Path, etc: Path) -> list[str]:
    """Return only the reviewed mount allowlist and a harmless shell smoke."""
    return [
        str(BWRAP),
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--hostname", "hermes-test",
        "--cap-drop", "ALL",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
        "--tmpfs", "/var",
        "--dir", "/var/tmp",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--ro-bind", str(etc), "/etc",
        "--ro-bind", str(JDK), "/opt/jdk",
        "--bind", str(workspace), "/workspace",
        "--ro-bind", str(workspace / ".git"), "/workspace/.git",
        "--bind", str(gradle_home), "/gradle",
        "--bind", str(ROOTLESS_SOCKET), "/run/docker.sock",
        "--chdir", "/workspace",
        "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "HOME", "/tmp/home",
        "--setenv", "JAVA_HOME", "/opt/jdk",
        "--setenv", "GRADLE_USER_HOME", "/gradle",
        "--setenv", "DOCKER_HOST", "unix:///run/docker.sock",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "LC_ALL", "C.UTF-8",
        "--setenv", "TZ", "UTC",
        "--setenv", "NO_PROXY", "*",
        "--",
    ]


def build_nsenter_argv(pid: int, bwrap_argv: list[str]) -> list[str]:
    if pid <= 1:
        raise RunnerError("invalid rootless namespace pid")
    return [
        str(NSENTER),
        "--target", str(pid),
        "--user", "--preserve-credentials",
        "--net", "--mount",
        "--",
        *bwrap_argv,
    ]


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process_group(process: subprocess.Popen[bytes], grace_seconds: float = 5) -> None:
    """Stop every process started in the command session, even if its leader exited."""
    pgid = process.pid  # start_new_session=True makes the child its process-group leader.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    deadline = time.monotonic() + grace_seconds
    while _process_group_exists(pgid) and time.monotonic() < deadline:
        if process.poll() is None:
            try:
                process.wait(timeout=min(0.1, max(0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        else:
            time.sleep(0.05)

    # Descendants may ignore SIGTERM after the group leader has exited.
    if _process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def _stream_process(argv: list[str], log_path: Path, timeout: int) -> tuple[int, bool]:
    started = time.monotonic()
    timed_out = False
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        os.chmod(log_path, 0o600)
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            bufsize=0,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                if time.monotonic() - started > timeout:
                    timed_out = True
                    _terminate_process_group(process)
                    break
                for key, _ in selector.select(timeout=1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    log.write(chunk)
                    log.flush()
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
            return_code = process.wait()
        except BaseException:
            _terminate_process_group(process)
            raise
        finally:
            selector.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
    return return_code, timed_out


def _smoke_command() -> list[str]:
    # Fixed probe only: no user-supplied command or shell text is accepted.
    probe = "\n".join([
        "import os, pathlib, socket",
        "assert pathlib.Path('/run/docker.sock').is_socket()",
        "assert not pathlib.Path('/home/alex/.hermes').exists()",
        "assert not pathlib.Path('/home/alex/.ssh').exists()",
        "assert not pathlib.Path('/home/alex/apps/crm-platform/.env').exists()",
        "assert not pathlib.Path('/home/alex/.cache/hermes-test-runtime').exists()",
        "assert not pathlib.Path('/var/run/docker.sock').exists()",
        "assert not pathlib.Path('/etc/shadow').exists()",
        "probe = pathlib.Path('/workspace/.hermes-isolation-probe')",
        "probe.write_text('ok')",
        "try:",
        "    pathlib.Path('/workspace/.git/.hermes-isolation-probe').write_text('bad')",
        "except OSError:",
        "    pass",
        "else:",
        "    raise SystemExit('Git metadata unexpectedly writable')",
        "try:",
        "    pathlib.Path('/opt/jdk/.hermes-isolation-probe').write_text('bad')",
        "except OSError:",
        "    pass",
        "else:",
        "    raise SystemExit('JDK unexpectedly writable')",
        "cache_probe = pathlib.Path('/gradle/.hermes-isolation-probe')",
        "cache_probe.write_text('ok'); cache_probe.unlink()",
        "s=socket.socket(socket.AF_UNIX); s.settimeout(3); s.connect('/run/docker.sock')",
        "s.sendall(b'GET /_ping HTTP/1.0\\r\\n\\r\\n')",
        "assert b'200 OK' in s.recv(4096); s.close()",
        "for host in ('1.1.1.1', '192.168.1.20', '2606:4700:4700::1111'):",
        "    try:",
        "        s=socket.create_connection((host,443),timeout=2)",
        "    except OSError:",
        "        continue",
        "    else:",
        "        s.close(); raise SystemExit('unexpected network route to '+host)",
        "probe.unlink()",
        "print('ISOLATION_SMOKE_OK')",
    ])
    return [
        "/usr/bin/python3",
        "-c",
        probe,
    ]


def _count_junit_xml(workspace: Path) -> dict[str, int]:
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "reports": 0}
    results = workspace / "backend/build/test-results/integrationTest"
    if not results.is_dir():
        return totals
    for path in results.glob("*.xml"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        totals["reports"] += 1
        cases = root.findall(".//testcase")
        totals["tests"] += len(cases)
        totals["failures"] += sum(len(case.findall("failure")) for case in cases)
        totals["errors"] += sum(len(case.findall("error")) for case in cases)
        totals["skipped"] += sum(len(case.findall("skipped")) for case in cases)
    return totals


def _copy_reports(workspace: Path, result_dir: Path) -> int:
    source = workspace / "backend/build/test-results/integrationTest"
    if not source.is_dir():
        return 0
    destination = result_dir / "junit"
    destination.mkdir(mode=0o700)
    count = 0
    for path in source.glob("*.xml"):
        if path.is_symlink() or not path.is_file():
            continue
        shutil.copyfile(path, destination / path.name)
        os.chmod(destination / path.name, 0o600)
        count += 1
    return count


def _tests_passed(
    *,
    smoke_only: bool,
    smoke_ok: bool,
    git_ok: bool,
    process_exit_code: int | None,
    timed_out: bool,
    counts: dict[str, int],
    copied_reports: int,
    cleanup_errors: list[str],
) -> bool:
    return bool(
        not smoke_only and smoke_ok and git_ok and process_exit_code == 0 and not timed_out
        and counts.get("tests", 0) > 0 and counts.get("reports", 0) > 0
        and counts.get("failures", 0) == 0 and counts.get("errors", 0) == 0
        and copied_reports > 0 and not cleanup_errors
    )


def _effective_exit_code(
    *, smoke_only: bool, smoke_exit_code: int | None, smoke_timeout: bool,
    smoke_ok: bool, process_exit_code: int | None, timed_out: bool, passed: bool,
    cleanup_errors: list[str],
) -> int:
    if smoke_only:
        return 124 if smoke_timeout else int(smoke_exit_code or 0)
    if not smoke_ok:
        return 124 if smoke_timeout else 1
    if process_exit_code == 0 and not passed:
        return 1
    if cleanup_errors and (process_exit_code or 0) == 0:
        return 1
    return 124 if timed_out else int(process_exit_code or 0)


def _prepare_workspace(snapshot: Path, result_dir: Path) -> Path:
    workspace = result_dir / "repo"
    shutil.copytree(snapshot, workspace, symlinks=True)
    # Keep the recorded commit immutable while permitting ordinary build output.
    git_dir = workspace / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise RunnerError("copied worktree does not contain independent Git metadata")
    return workspace


def run_job(job: Job, *, smoke_only: bool = False) -> int:
    snapshot = validate_snapshot(job)
    rootless_pid = find_rootless_namespace_pid()
    route_evidence = _namespace_route_check(rootless_pid)
    dockerd_pid = validate_dockerd_process(rootless_pid)
    daemon_fs_evidence = verify_daemon_filesystem_sandbox(dockerd_pid)
    validate_rootless_daemon()
    if not smoke_only:
        validate_postgres_image()
    if not JDK.is_dir() or JDK.is_symlink():
        raise RunnerError("pinned JDK 21 installation is missing")
    if not BWRAP.is_file() or not os.access(BWRAP, os.X_OK):
        raise RunnerError("pinned bubblewrap executable is missing")

    _create_private_tree(RESULT_ROOT, "results directory")
    _create_private_tree(RUN_CACHE_ROOT, "per-run cache directory")
    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    result_dir = RESULT_ROOT / job.request_id / job.task_id / job.sha / run_id
    _create_private_tree(result_dir, "run result directory", RESULT_ROOT)
    workspace = _prepare_workspace(snapshot, result_dir)
    if not smoke_only:
        gradle_home = RUN_CACHE_ROOT / run_id
        _copy_cache(gradle_home)
    else:
        gradle_home = result_dir / "empty-gradle-home"
        gradle_home.mkdir(mode=0o700)
    etc = result_dir / "etc"
    _write_minimal_etc(etc)

    bwrap_argv = build_bwrap_argv(workspace, gradle_home, etc)
    log_path = result_dir / ("smoke.log" if smoke_only else "integration-tests.log")
    started = time.time()
    smoke_log = log_path if smoke_only else result_dir / "smoke.log"
    smoke_exit: int | None = None
    smoke_timeout = False
    exit_code: int | None = None
    timed_out = False
    command: list[str] = []
    cleanup_errors: list[str] = []
    before_resources: dict[str, set[str]] | None = None
    with _exclusive_runner_lock():
        before_resources = _resource_snapshot()
        if before_resources["containers"]:
            raise RunnerError("dedicated test daemon has leftover containers; refusing to interfere")
        try:
            smoke_argv = build_nsenter_argv(rootless_pid, [*bwrap_argv, *_smoke_command()])
            smoke_exit, smoke_timeout = _stream_process(smoke_argv, smoke_log, 30)
            if smoke_only:
                exit_code, timed_out = smoke_exit, smoke_timeout
                command = _smoke_command()
            elif smoke_exit != 0 or smoke_timeout:
                exit_code, timed_out = smoke_exit, smoke_timeout
                print("Refusing Gradle run because the isolation smoke failed.", file=sys.stderr)
            else:
                command = build_gradle_argv(job.scope)
                argv = build_nsenter_argv(rootless_pid, [*bwrap_argv, *command])
                exit_code, timed_out = _stream_process(argv, log_path, MAX_RUN_SECONDS)
        except KeyboardInterrupt:
            exit_code, timed_out = 130, False
            print("Isolated test runner interrupted; cleaning test daemon resources.", file=sys.stderr)
        finally:
            if before_resources is not None:
                cleanup_errors = _cleanup_created_resources(before_resources)
    if (workspace / ".hermes-isolation-probe").exists():
        (workspace / ".hermes-isolation-probe").unlink()
    smoke_ok = smoke_exit == 0 and not smoke_timeout
    git_ok = False
    if not smoke_only and smoke_ok:
        try:
            git_ok = (
                _git(workspace, "rev-parse", "HEAD") == job.sha
                and not _git(workspace, "status", "--porcelain", "--untracked-files=no")
            )
        except RunnerError:
            git_ok = False
    counts = {} if smoke_only else _count_junit_xml(workspace)
    copied_reports = 0 if smoke_only else _copy_reports(workspace, result_dir)
    for disposable in (workspace, gradle_home, etc):
        try:
            if disposable.is_symlink():
                cleanup_errors.append(f"refused symlink cleanup: {disposable.name}")
            elif disposable.exists():
                shutil.rmtree(disposable)
        except OSError as exc:
            cleanup_errors.append(f"failed to remove temporary {disposable.name}: {exc.__class__.__name__}")
    passed = _tests_passed(
        smoke_only=smoke_only, smoke_ok=smoke_ok, git_ok=git_ok,
        process_exit_code=exit_code, timed_out=timed_out, counts=counts,
        copied_reports=copied_reports, cleanup_errors=cleanup_errors,
    )
    effective_exit_code = _effective_exit_code(
        smoke_only=smoke_only, smoke_exit_code=smoke_exit, smoke_timeout=smoke_timeout,
        smoke_ok=smoke_ok, process_exit_code=exit_code, timed_out=timed_out,
        passed=passed, cleanup_errors=cleanup_errors,
    )
    data = {
        "request_id": job.request_id,
        "task_id": job.task_id,
        "sha": job.sha,
        "scope": "smoke" if smoke_only else job.scope,
        "preflight_smoke_exit_code": smoke_exit,
        "preflight_smoke_timed_out": smoke_timeout,
        "command": command,
        "exit_code": effective_exit_code,
        "process_exit_code": exit_code,
        "passed": passed,
        "tests_ran": bool(not smoke_only and smoke_ok and command),
        "workspace_commit_unchanged": git_ok if not smoke_only and smoke_ok else None,
        "cleanup_errors": cleanup_errors,
        "timed_out": timed_out,
        "started_at_epoch": started,
        "duration_seconds": round(time.time() - started, 3),
        "preflight_smoke_log": smoke_log.name,
        "log": log_path.name if smoke_only or not smoke_ok else "integration-tests.log",
        "junit": counts,
        "junit_files_copied": copied_reports,
        "rootless_namespace_pid": rootless_pid,
        "daemon_filesystem_sandbox_verified": daemon_fs_evidence["verified"],
        "daemon_filesystem_sandbox_evidence": daemon_fs_evidence,
        "network_route_evidence": route_evidence,
        "egress_probe_addresses": ["1.1.1.1", "192.168.1.20", "2606:4700:4700::1111"],
    }
    result_path = result_dir / "result.json"
    result_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result_path.chmod(0o600)
    print(f"RESULT_JSON={result_path}")
    return effective_exit_code


def main(argv: Iterable[str] | None = None) -> int:
    def interrupted(signum, _frame):
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, dest="request_id")
    parser.add_argument("--task", required=True, dest="task_id")
    parser.add_argument("--sha", required=True)
    parser.add_argument("--scope", required=True, choices=("salesleads", "full"))
    parser.add_argument("--smoke-only", action="store_true",
                        help="run only the built-in filesystem/network isolation probe")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        job = parse_job(args.request_id, args.task_id, args.sha, args.scope)
        return run_job(job, smoke_only=args.smoke_only)
    except RunnerError as exc:
        print(f"isolated-backend-tests: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"isolated-backend-tests: host operation failed ({exc.__class__.__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
