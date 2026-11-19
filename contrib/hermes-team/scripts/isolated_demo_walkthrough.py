#!/usr/bin/env python3
"""Run the fixed T-008 Avito demo in the dedicated offline rootless daemon.

This operator-only entry point accepts identifiers only. It validates and
copies the controller-owned snapshot, compiles its backend offline inside the
existing bubblewrap sandbox, then runs only the Compose services needed for
the documented API demo in the private rootless Docker daemon. It never uses
the host Docker daemon to build or run project code.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import isolated_backend_tests as isolated


REQUEST_ID = "req-0557438ef182e8847164"
TASK_ID = "T-008"
COMMIT_SHA = "38a2cde5bc27fee4690b1dedc8c033ae289e6f45"
TEMURIN_IMAGE_ID = "sha256:a4ab626836123d37d64448578f511211694b118a22936d328d870f8ac0f70825"
PINNED_RUNTIME_IMAGES = {
    "postgres:18": "sha256:a1892662ab56773f96746107054fcfec3d7cd32d39a34fabec4a57f66bd15524",
    "redis:8-alpine": "sha256:3a02d38405dc12092032737fdadd513729a81efca8439d1d7eef883e6e67e26e",
    "minio/minio:RELEASE.2025-09-07T16-13-09Z": "sha256:69b2ec208575b69597784255eec6fa6a2985ee9e1a47f4411a51f7f5fdd193a9",
}
COMPOSE_PLUGIN = Path("/usr/libexec/docker/cli-plugins/docker-compose")
MAX_DEMO_SECONDS = 60 * 60
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
SQL_DIR = Path("specs/avito-sales-leads/first-slice")
SEED_NAME = "demo-seed-base.sql"
ASSIGN_NAME = "demo-assign.sql"
PROBE_NAME = "demo-api-probe.py"


class DemoError(RuntimeError):
    """A demo precondition, assertion, or cleanup step failed."""


class DemoCancelled(DemoError):
    """The operator asked this bounded run to stop; cleanup still runs."""


def _operator_stop(_signum, _frame):
    raise DemoCancelled("operator requested stop")


@dataclass(frozen=True)
class DemoJob:
    request_id: str
    task_id: str
    sha: str


def parse_job(request_id: str, task_id: str, sha: str) -> DemoJob:
    if (request_id, task_id, sha) != (REQUEST_ID, TASK_ID, COMMIT_SHA):
        raise DemoError("only the reviewed T-008 candidate can be demonstrated")
    return DemoJob(request_id, task_id, sha)


def build_compose_override(project: str, backend_port: int,
                           extra_hosts: dict[str, str] | None = None) -> str:
    if not re.fullmatch(r"crm-demo-[a-f0-9]{12}", project):
        raise DemoError("invalid disposable Compose project name")
    if not 20000 <= backend_port <= 29999:
        raise DemoError("backend port is outside the isolated test range")
    extra_hosts = extra_hosts or {}
    if set(extra_hosts) - {"postgres", "redis", "minio"}:
        raise DemoError("runtime DNS override contains an unexpected service")
    if extra_hosts and set(extra_hosts) != {"postgres", "redis", "minio"}:
        raise DemoError("runtime DNS override must include all three demo dependencies")
    for name, address in extra_hosts.items():
        try:
            if ipaddress.ip_address(address).version != 4:
                raise ValueError
        except ValueError as exc:
            raise DemoError(f"runtime DNS override for {name} is not an IPv4 address") from exc
    hosts_yaml = ""
    if extra_hosts:
        hosts_yaml = "    extra_hosts: !override\n" + "".join(
            f'      - "{name}:{extra_hosts[name]}"\n' for name in ("postgres", "redis", "minio"))
    # Compose's !reset merge tag explicitly clears all publication/build
    # settings from the project file. The one backend listener is published
    # on loopback in the dedicated rootless network namespace only.
    return f'''services:
  postgres:
    ports: !reset []
  redis:
    ports: !reset []
  minio:
    ports: !reset []
  backend:
    image: crm-sales-leads-demo-backend:{project}
    build: !reset null
    ports: !override
      - "127.0.0.1:{backend_port}:8080"
    volumes: !reset []
{hosts_yaml}    environment:
      CRM_SALES_LEADS_ENABLED: "true"
      CRM_AVITO_ENABLED: "false"
      CRM_AVITO_WORKER_ENABLED: "false"
    healthcheck:
      disable: true
  frontend:
    profiles: ["operator-disabled"]
    build: !reset null
    ports: !reset []
'''


def build_environment(project: str, backend_port: int) -> str:
    if not re.fullmatch(r"crm-demo-[a-f0-9]{12}", project):
        raise DemoError("invalid disposable Compose project name")
    if not 20000 <= backend_port <= 29999:
        raise DemoError("backend port is outside the isolated test range")
    return "\n".join([
        f"COMPOSE_PROJECT_NAME={project}",
        "CRM_SALES_LEADS_ENABLED=true",
        "CRM_AVITO_ENABLED=false",
        "CRM_AVITO_WORKER_ENABLED=false",
        "POSTGRES_DB=crm_platform",
        "POSTGRES_USER=crm",
        "POSTGRES_PASSWORD=demo-only-password",
        "POSTGRES_HOST_PORT=5432",
        "REDIS_HOST_PORT=6379",
        "MINIO_API_HOST_PORT=9000",
        "MINIO_CONSOLE_HOST_PORT=9001",
        "MINIO_ROOT_USER=demo-only-key",
        "MINIO_ROOT_PASSWORD=demo-only-secret",
        f"CRM_BACKEND_HOST_PORT={backend_port}",
        "CRM_DB_URL=jdbc:postgresql://postgres:5432/crm_platform",
        "CRM_DB_USERNAME=crm",
        "CRM_DB_PASSWORD=demo-only-password",
        "CRM_REDIS_HOST=redis",
        "CRM_REDIS_PORT=6379",
        "CRM_OBJECT_STORAGE_ENDPOINT=http://minio:9000",
        "CRM_OBJECT_STORAGE_ACCESS_KEY=demo-only-key",
        "CRM_OBJECT_STORAGE_SECRET_KEY=demo-only-secret",
        "CRM_OBJECT_STORAGE_BUCKET=crm-platform-private",
        "CRM_OBJECT_STORAGE_CREATE_BUCKET=true",
        "CRM_BOOTSTRAP_ADMIN_EMAIL=",
        "CRM_BOOTSTRAP_ADMIN_PASSWORD=",
        "CRM_CREDENTIALS_VAULT_ENABLED=false",
        "CRM_CREDENTIALS_ENCRYPTION_KEY=",
        "CRM_AVITO_ENCRYPTION_KEY=",
        "CRM_AVITO_WEBHOOK_BASE_URL=",
        "CRM_PUBLIC_BASE_URL=http://127.0.0.1",
        "TZ=UTC",
        "",
    ])


def validate_compose_config(config: object, project: str, port: int,
                            expected_hosts: dict[str, str] | None = None) -> dict[str, object]:
    if not isinstance(config, dict):
        raise DemoError("Compose config is not a JSON object")
    services = config.get("services")
    # Compose omits services and volumes belonging only to an inactive profile.
    expected_names = {"postgres", "redis", "minio", "backend"}
    if not isinstance(services, dict) or set(services) != expected_names:
        raise DemoError("Compose service set differs from the reviewed application")
    fixed_images = {
        "postgres": "postgres:18",
        "redis": "redis:8-alpine",
        "minio": "minio/minio:RELEASE.2025-09-07T16-13-09Z",
        "backend": f"crm-sales-leads-demo-backend:{project}",
    }
    for name, expected in fixed_images.items():
        service = services[name]
        if not isinstance(service, dict) or service.get("image") != expected:
            raise DemoError(f"Compose service {name} has an unexpected image")
        if service.get("build") is not None:
            raise DemoError(f"Compose service {name} must not build an image")
    backend = services["backend"]
    environment = backend.get("environment")
    if not isinstance(environment, dict) or environment.get("CRM_SALES_LEADS_ENABLED") != "true":
        raise DemoError("Compose did not enable CRM_SALES_LEADS_ENABLED for the backend")
    if environment.get("CRM_AVITO_ENABLED") != "false" or environment.get("CRM_AVITO_WORKER_ENABLED") != "false":
        raise DemoError("Compose must keep the real Avito integration disabled")
    expected_backend_storage = {
        "CRM_OBJECT_STORAGE_ENDPOINT":"http://minio:9000",
        "CRM_OBJECT_STORAGE_ACCESS_KEY":"demo-only-key",
        "CRM_OBJECT_STORAGE_SECRET_KEY":"demo-only-secret",
        "CRM_OBJECT_STORAGE_BUCKET":"crm-platform-private",
        "CRM_OBJECT_STORAGE_CREATE_BUCKET":"true",
    }
    if any(environment.get(key) != value for key, value in expected_backend_storage.items()):
        raise DemoError("Compose backend object-storage configuration is not the synthetic demo configuration")
    minio_environment = services["minio"].get("environment")
    if (not isinstance(minio_environment, dict)
            or minio_environment.get("MINIO_ROOT_USER") != "demo-only-key"
            or minio_environment.get("MINIO_ROOT_PASSWORD") != "demo-only-secret"):
        raise DemoError("Compose MinIO credentials do not match the synthetic backend credentials")
    if not isinstance(backend.get("healthcheck"), dict) or backend["healthcheck"].get("disable") is not True:
        raise DemoError("fixed JDK image requires the candidate curl healthcheck to be disabled")
    ports = backend.get("ports")
    if not isinstance(ports, list) or len(ports) != 1:
        raise DemoError("Compose backend must have exactly one isolated loopback port")
    binding = ports[0]
    if (not isinstance(binding, dict) or binding.get("host_ip") != "127.0.0.1"
            or str(binding.get("published")) != str(port) or str(binding.get("target")) != "8080"):
        raise DemoError("Compose backend publication escaped the private loopback port")
    configured_hosts_value = backend.get("extra_hosts") or {}
    expected_hosts = expected_hosts or {}
    if isinstance(configured_hosts_value, dict):
        configured_hosts = configured_hosts_value
    elif isinstance(configured_hosts_value, list):
        configured_hosts = {}
        for entry in configured_hosts_value:
            if not isinstance(entry, str) or not (":" in entry or "=" in entry):
                raise DemoError("Compose backend runtime DNS mapping has an invalid format")
            separator = ":" if ":" in entry else "="
            host, address = entry.split(separator, 1)
            if host in configured_hosts:
                raise DemoError("Compose backend runtime DNS mapping is duplicated")
            configured_hosts[host] = address
    else:
        raise DemoError("Compose backend runtime DNS mapping has an invalid format")
    if configured_hosts != expected_hosts:
        raise DemoError("Compose backend runtime DNS mappings differ from the verified demo network")
    for name, service in services.items():
        if not isinstance(service, dict):
            raise DemoError(f"Compose service {name} is invalid")
        if name != "backend" and service.get("ports"):
            raise DemoError(f"Compose service {name} exposes a host port")
        if service.get("privileged") is True or service.get("network_mode") or service.get("pid") or service.get("ipc"):
            raise DemoError(f"Compose service {name} requests unsafe isolation settings")
        if (service.get("devices") or service.get("cap_add") or service.get("security_opt")
                or service.get("env_file") or service.get("secrets") or service.get("configs")
                or service.get("volumes_from")):
            raise DemoError(f"Compose service {name} requests host access or extra privileges")
        if name in {"postgres", "redis", "minio", "backend"}:
            mounts = service.get("volumes", [])
            if not isinstance(mounts, list):
                raise DemoError(f"Compose service {name} has invalid storage configuration")
            if name == "backend" and mounts:
                raise DemoError("Compose backend must not mount host files")
            if any(not isinstance(mount, dict) or mount.get("type") != "volume"
                   for mount in mounts):
                raise DemoError(f"Compose service {name} has a non-disposable host mount")
            expected_mount = {
                "postgres": ("postgres_data", "/var/lib/postgresql"),
                "redis": ("redis_data", "/data"),
                "minio": ("minio_data", "/data"),
                "backend": (None, None),
            }[name]
            if name != "backend":
                # Compose's rendered mount source is the logical key (e.g.
                # postgres_data); the volume declaration pins its runtime
                # name to this run's unique project-prefixed volume.
                if (len(mounts) != 1 or mounts[0].get("source") != expected_mount[0]
                        or mounts[0].get("target") != expected_mount[1]):
                    raise DemoError(f"Compose service {name} volume is not unique to this run")
    volumes = config.get("volumes")
    expected_volume_keys = {"postgres_data", "redis_data", "minio_data"}
    if not isinstance(volumes, dict) or set(volumes) != expected_volume_keys:
        raise DemoError("Compose volume set differs from the reviewed application")
    for name, volume in volumes.items():
        if not isinstance(volume, dict) or volume.get("external") is True:
            raise DemoError("Compose volume must be private and disposable")
        if volume.get("name") != f"{project}_{name}":
            raise DemoError("Compose volume name must be unique to this run")
    networks = config.get("networks")
    if not isinstance(networks, dict) or set(networks) != {"default"}:
        raise DemoError("Compose network set differs from the reviewed application")
    default_network = networks["default"]
    if not isinstance(default_network, dict) or default_network.get("external") is True:
        raise DemoError("Compose default network must be private to this run")
    if config.get("name") != project:
        raise DemoError("Compose project name does not match the disposable run")
    return {"services": sorted(services), "backend_loopback_port": port,
            "sales_leads_enabled": True, "avito_integration_enabled": False,
            "frontend_started": False, "offline_runtime_dns_overrides": dict(expected_hosts)}


API_PROBE = r'''import http.cookiejar, json, sys, urllib.error, urllib.parse, urllib.request, uuid

base = "http://127.0.0.1:" + sys.argv[1]
y_lead_id = sys.argv[2]
if not __import__("re").fullmatch(r"[0-9a-fA-F-]{36}", y_lead_id):
    raise SystemExit("invalid synthetic Y lead identifier")
password = "demo-sales-leads-2026"
users = ("m", "u", "v", "w", "x", "z")
clients = {}

def client(user=None):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    def request(path, method="GET", body=None, headers=None, expected=(200,)):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
        try:
            response = opener.open(req, timeout=12)
            status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        if status not in expected:
            raise AssertionError(f"{method} {path}: HTTP {status}, expected {expected}: {raw[:500]!r}")
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError as exc:
            raise AssertionError(f"{method} {path}: response was not JSON") from exc
        return status, payload
    if user is not None:
        _, csrf = request("/api/v1/auth/csrf")
        token = csrf.get("token")
        if not isinstance(token, str) or not token:
            raise AssertionError("CSRF endpoint did not return a token")
        request("/api/v1/auth/login", "POST",
                {"email": f"demo-{user}@sales-leads-demo.test", "password": password},
                {"Content-Type": "application/json", "X-XSRF-TOKEN": token})
    return request, opener

def command(req, path, key, version, expected):
    _, csrf = req("/api/v1/auth/csrf")
    token = csrf["token"]
    return req(path, "POST", {"type":"TAKE", "commandId":key, "expectedVersion":version},
               {"Content-Type":"application/json", "X-XSRF-TOKEN":token}, expected)

def need(condition, message):
    if not condition: raise AssertionError(message)

for user in users:
    clients[user] = client(user)[0]
m = clients["m"]
_, settings = m("/api/v1/sales-leads/settings")
need(settings.get("enabled") is True and settings.get("available") is True, "M settings must be enabled and available")
_, mine = m("/api/v1/sales-leads/my")
items = mine.get("items")
need(isinstance(items, list) and len(items) == 1, "M must have exactly one assigned lead")
lead = items[0]
lead_id = lead.get("id")
need(isinstance(lead_id, str) and lead_id, "M lead id is missing")
need(lead.get("stage") == "NEW", "M lead must be NEW before TAKE")
need(lead.get("applicantName") == "Демо Кандидат А", "M must see synthetic applicant A")
_, detail = m(f"/api/v1/sales-leads/{urllib.parse.quote(lead_id, safe='')}")
need("TAKE" in detail.get("allowedActions", []), "M lead must allow TAKE")

for user in ("u", "v"):
    req = clients[user]
    _, own_settings = req("/api/v1/sales-leads/settings")
    need(own_settings.get("available") is True, f"{user.upper()} settings must be available")
    _, own = req("/api/v1/sales-leads/my")
    need(own.get("items") == [] and own.get("total") == 0, f"{user.upper()} must see no assigned leads")
    req(f"/api/v1/sales-leads/{urllib.parse.quote(lead_id, safe='')}", expected=(404,))
for user in ("w", "x", "z"):
    req = clients[user]
    _, own_settings = req("/api/v1/sales-leads/settings")
    need(own_settings.get("available") is False and own_settings.get("reasonCode") == "ROLE_MISSING",
         f"{user.upper()} must be denied for missing sales-manager role")
    req("/api/v1/sales-leads/my", expected=(403,))
    req(f"/api/v1/sales-leads/{urllib.parse.quote(lead_id, safe='')}", expected=(404,))

path = f"/api/v1/sales-leads/{urllib.parse.quote(lead_id, safe='')}/commands"
req_y = f"/api/v1/sales-leads/{urllib.parse.quote(y_lead_id, safe='')}"
# Outsider write attempts happen while the assigned lead is still NEW so a
# later 404 cannot be mistaken for authorization merely because it was taken.
path = f"/api/v1/sales-leads/{urllib.parse.quote(lead_id, safe='')}/commands"
for user in ("u", "v", "w", "x", "z"):
    req = clients[user]
    _, csrf = req("/api/v1/auth/csrf")
    req(path, "POST", {"type":"TAKE", "commandId":str(uuid.uuid4()), "expectedVersion":0},
        {"Content-Type":"application/json", "X-XSRF-TOKEN":csrf["token"]}, expected=(404,))
anon, _ = client()
anon("/api/v1/sales-leads/settings", expected=(401,))
anon("/api/v1/sales-leads/my", expected=(401,))
anon(f"/api/v1/sales-leads/{urllib.parse.quote(lead_id, safe='')}", expected=(401,))
command(anon, f"/api/v1/sales-leads/{urllib.parse.quote(lead_id, safe='')}/commands",
        str(uuid.uuid4()), 0, (401,))
for user in ("u", "v", "w", "x", "z"):
    req = clients[user]
    _, csrf = req("/api/v1/auth/csrf")
    req(f"/api/v1/sales-leads/{urllib.parse.quote(y_lead_id, safe='')}/commands", "POST",
        {"type":"TAKE", "commandId":str(uuid.uuid4()), "expectedVersion":0},
        {"Content-Type":"application/json", "X-XSRF-TOKEN":csrf["token"]}, expected=(404,))
command(anon, f"/api/v1/sales-leads/{urllib.parse.quote(y_lead_id, safe='')}/commands",
        str(uuid.uuid4()), 0, (401,))

take_key = str(uuid.uuid4())
_, taken = command(m, path, take_key, 0, (200,))
need(taken.get("stage") == "CONTACT_IN_PROGRESS" and taken.get("version") == 1
     and taken.get("replayed") is False, "first TAKE must move to version 1")
_, replay = command(m, path, take_key, 0, (200,))
need(replay.get("replayed") is True, "same command must be idempotently replayed")
_, stale = command(m, path, str(uuid.uuid4()), 0, (409,))
need(stale.get("code") == "VERSION_CONFLICT", "stale TAKE must return VERSION_CONFLICT")
_, again = command(m, path, str(uuid.uuid4()), 1, (409,))
need(again.get("code") == "ALREADY_TAKEN", "second TAKE must return ALREADY_TAKEN")
req_y = f"/api/v1/sales-leads/{urllib.parse.quote(y_lead_id, safe='')}"
m(req_y, expected=(404,))
command(m, req_y + "/commands", str(uuid.uuid4()), 0, (404,))
print(json.dumps({"passed":True,"users_tested":list(users),"checks":[
    "manager M sees lead A and TAKE action", "same-department and cross-department managers cannot see A",
    "non-sales roles are denied", "TAKE command, replay, stale version, and repeated TAKE",
    "unauthenticated request denied", "lead Y remains absent from assigned lists"
],"lead_id":lead_id}, ensure_ascii=False, sort_keys=True))
'''


def _capture(argv: list[str], *, timeout: int, input_text: str | None = None) -> str:
    try:
        proc = subprocess.run(argv, input=input_text, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, timeout=timeout,
                              env={"PATH":"/usr/bin:/bin","LANG":"C.UTF-8","LC_ALL":"C.UTF-8"})
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        output = getattr(exc, "stdout", b"") or b""
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        raise DemoError(f"fixed isolated demo command failed: {Path(argv[-1]).name}; {str(output)[-1200:]}") from exc
    if len(proc.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise DemoError("isolated demo command output exceeded its bound")
    return proc.stdout


def _compose_base(workspace: Path, override: Path, env_file: Path, project: str) -> list[str]:
    del workspace, override, env_file  # Paths are deliberately relative inside bubblewrap.
    return ["docker", "compose", "--env-file", ".hermes-demo.env", "--project-name", project,
            "-f", "compose.yaml", "-f", ".hermes-demo-override.yaml"]


def _socket_identity() -> tuple[int, int]:
    try:
        info = isolated.ROOTLESS_SOCKET.lstat()
    except OSError as exc:
        raise DemoError("dedicated rootless Docker socket is missing") from exc
    if not isolated.stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o007:
        raise DemoError("dedicated rootless Docker socket permissions are unsafe")
    return info.st_dev, info.st_ino


def _verify_socket(identity: tuple[int, int]) -> None:
    if _socket_identity() != identity:
        raise DemoError("dedicated rootless Docker socket changed during demo setup")


def _safe_docker_api(identity: tuple[int, int], path: str, *, method: str = "GET") -> tuple[int, bytes]:
    _verify_socket(identity)
    response = isolated._docker_api(isolated.ROOTLESS_SOCKET, path, method=method)
    _verify_socket(identity)
    return response


def _safe_docker_json(identity: tuple[int, int], path: str) -> object:
    status, body = _safe_docker_api(identity, path)
    if status != 200:
        raise DemoError(f"dedicated Docker API request failed: {path}")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DemoError(f"dedicated Docker API returned invalid JSON: {path}") from exc


def _project_service_container(identity: tuple[int, int], project: str,
                               service: str) -> tuple[str, dict[str, object]]:
    filters = {"label":[f"com.docker.compose.project={project}",
                        f"com.docker.compose.service={service}"]}
    query = quote(json.dumps(filters, separators=(",", ":")), safe="")
    rows = _safe_docker_json(identity, f"/containers/json?all=1&filters={query}")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise DemoError(f"expected exactly one rootless Compose container for {service}")
    container_id = rows[0].get("Id")
    if not isinstance(container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise DemoError(f"rootless Compose container id for {service} is invalid")
    inspect = _safe_docker_json(identity, f"/containers/{container_id}/json")
    if not isinstance(inspect, dict):
        raise DemoError(f"rootless Compose inspect for {service} is invalid")
    config = inspect.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    expected_labels = {"com.docker.compose.project":project, "com.docker.compose.service":service}
    if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in expected_labels.items()):
        raise DemoError(f"rootless Compose labels for {service} do not match this demo project")
    return container_id, inspect


def _verified_project_service_ips(identity: tuple[int, int], project: str,
                                  services: tuple[str, ...] = ("postgres", "redis", "minio")) -> dict[str, str]:
    network_name = f"{project}_default"
    network = _safe_docker_json(identity, f"/networks/{quote(network_name, safe='')}")
    if not isinstance(network, dict) or network.get("Name") != network_name:
        raise DemoError("dedicated Compose default network is missing or has an unexpected name")
    labels = network.get("Labels")
    if not isinstance(labels, dict) or labels.get("com.docker.compose.project") != project:
        raise DemoError("dedicated Compose default network is not owned by this run")
    network_id = network.get("Id")
    if not isinstance(network_id, str) or not re.fullmatch(r"[0-9a-f]{64}", network_id):
        raise DemoError("dedicated Compose default network id is invalid")
    members = network.get("Containers")
    if not isinstance(members, dict):
        raise DemoError("dedicated Compose network container list is invalid")
    ipam = network.get("IPAM")
    config = ipam.get("Config") if isinstance(ipam, dict) else None
    subnets = []
    if isinstance(config, list):
        for entry in config:
            if isinstance(entry, dict) and isinstance(entry.get("Subnet"), str):
                try:
                    subnets.append(ipaddress.ip_network(entry["Subnet"], strict=False))
                except ValueError:
                    raise DemoError("dedicated Compose network has an invalid subnet")
    if not subnets:
        raise DemoError("dedicated Compose network has no inspectable subnet")
    resolved: dict[str, str] = {}
    for service in services:
        container_id, inspect = _project_service_container(identity, project, service)
        if inspect.get("State", {}).get("Running") is not True:
            raise DemoError(f"Compose dependency {service} is not running")
        networks = inspect.get("NetworkSettings", {}).get("Networks", {})
        endpoint = networks.get(network_name) if isinstance(networks, dict) else None
        if not isinstance(endpoint, dict) or endpoint.get("NetworkID") != network_id:
            raise DemoError(f"Compose dependency {service} is not attached to this run's default network")
        member = members.get(container_id)
        if not isinstance(member, dict):
            # Docker inspect may return a short ID key in the network endpoint map.
            member = next((value for key, value in members.items()
                           if isinstance(key, str) and key.startswith(container_id[:12])), None)
        if not isinstance(member, dict):
            raise DemoError(f"Compose dependency {service} is not a member of this run's network")
        address = endpoint.get("IPAddress")
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise DemoError(f"Compose dependency {service} has no valid IPv4 address") from exc
        if parsed.version != 4 or not any(parsed in subnet for subnet in subnets if subnet.version == 4):
            raise DemoError(f"Compose dependency {service} address is outside this run's network")
        member_ip = member.get("IPv4Address")
        if not isinstance(member_ip, str) or member_ip.split("/", 1)[0] != str(parsed):
            raise DemoError(f"Compose dependency {service} address does not match the network membership record")
        resolved[service] = str(parsed)
    return resolved


def _capture_backend_startup_log(identity: tuple[int, int], project: str, result_dir: Path) -> str:
    try:
        container_id, _inspect = _project_service_container(identity, project, "backend")
        status, body = _safe_docker_api(identity,
            f"/containers/{container_id}/logs?stdout=1&stderr=1&timestamps=1&tail=200")
        if status != 200:
            content = f"Docker API could not retrieve backend startup log (HTTP {status}).\n".encode()
        else:
            content = body[-256 * 1024:]
    except DemoError as exc:
        content = f"Backend startup log unavailable: {exc}\n".encode("utf-8", "replace")
    path = result_dir / "backend-startup.log"
    path.write_bytes(content)
    path.chmod(0o600)
    return path.name


def _backend_startup_failure(identity: tuple[int, int], project: str) -> str | None:
    try:
        _container_id, inspect = _project_service_container(identity, project, "backend")
    except DemoError:
        return None
    state = inspect.get("State")
    if not isinstance(state, dict):
        return "container has no inspectable state"
    status = state.get("Status")
    if status in {"exited", "dead", "removing"}:
        code = state.get("ExitCode")
        return f"state={status}, exit_code={code}"
    if status == "restarting":
        return "state=restarting after process exit"
    return None


def _safe_resource_snapshot(identity: tuple[int, int]) -> dict[str, set[str]]:
    containers = _safe_docker_json(identity, "/containers/json?all=1")
    networks = _safe_docker_json(identity, "/networks")
    volumes_obj = _safe_docker_json(identity, "/volumes")
    if not isinstance(containers, list) or not isinstance(networks, list) or not isinstance(volumes_obj, dict):
        raise DemoError("dedicated Docker API returned an unexpected resource list")
    volumes = volumes_obj.get("Volumes") or []
    return {
        "containers": {str(item.get("Id")) for item in containers if isinstance(item, dict)},
        "networks": {str(item.get("Id")) for item in networks if isinstance(item, dict)},
        "volumes": {str(item.get("Name")) for item in volumes if isinstance(item, dict)},
    }


def _cleanup_created_resources(identity: tuple[int, int], before: dict[str, set[str]]) -> list[str]:
    errors: list[str] = []
    try:
        after = _safe_resource_snapshot(identity)
    except DemoError as exc:
        return [str(exc)]
    for container_id in sorted(after["containers"] - before["containers"]):
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            errors.append("unexpected container identifier from Docker API")
            continue
        status, _ = _safe_docker_api(identity, f"/containers/{container_id}?force=1&v=1", method="DELETE")
        if status not in {204, 200, 404}:
            errors.append(f"failed to remove demo container {container_id[:12]}")
    try:
        after = _safe_resource_snapshot(identity)
    except DemoError as exc:
        return errors + [str(exc)]
    for network_id in sorted(after["networks"] - before["networks"]):
        if not re.fullmatch(r"[0-9a-f]{12,64}", network_id):
            errors.append("unexpected network identifier from Docker API")
            continue
        status, _ = _safe_docker_api(identity, f"/networks/{network_id}", method="DELETE")
        if status not in {204, 200, 404}:
            errors.append(f"failed to remove demo network {network_id[:12]}")
    for name in sorted(after["volumes"] - before["volumes"]):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name):
            errors.append("unexpected volume name from Docker API")
            continue
        status, _ = _safe_docker_api(identity, f"/volumes/{quote(name, safe='')}", method="DELETE")
        if status not in {204, 200, 404}:
            errors.append(f"failed to remove demo volume {name[:48]}")
    return errors


def _require_stock_image(identity: tuple[int, int]) -> None:
    image = _safe_docker_json(identity, "/images/eclipse-temurin:21-jdk/json")
    if not isinstance(image, dict) or image.get("Id") != TEMURIN_IMAGE_ID:
        raise DemoError("pinned cached Temurin 21 image is missing from the rootless daemon")
    for reference, expected_id in PINNED_RUNTIME_IMAGES.items():
        image = _safe_docker_json(identity, f"/images/{quote(reference, safe=':/')}/json")
        if not isinstance(image, dict) or image.get("Id") != expected_id:
            raise DemoError(f"pinned cached application image is missing or changed: {reference}")


def _remove_demo_image(identity: tuple[int, int], tag: str) -> list[str]:
    """Remove only this run's UUID-tagged image after its containers are gone."""
    status, _ = _safe_docker_api(identity, f"/images/{quote(tag, safe=':')}/json")
    if status == 404:
        return []
    if status != 200:
        return ["could not inspect the disposable demo image during cleanup"]
    status, _ = _safe_docker_api(identity, f"/images/{quote(tag, safe=':')}?force=1&noprune=1",
                                 method="DELETE")
    return [] if status in {200, 404} else ["could not remove the disposable demo image"]


def _nsenter_argv(rootless_pid: int, identity: tuple[int, int], command: list[str]) -> list[str]:
    # The host Unix socket is bind-mounted into each private bubblewrap launch;
    # verify its identity immediately before constructing that bind mount.
    _verify_socket(identity)
    return isolated.build_nsenter_argv(rootless_pid, command)


def _run_demo_job(job: DemoJob, run_id: str, result_dir: Path) -> int:
    backend_job = isolated.Job(job.request_id, job.task_id, job.sha, "full")
    snapshot = isolated.validate_snapshot(backend_job)
    if not COMPOSE_PLUGIN.is_file() or not os.access(COMPOSE_PLUGIN, os.X_OK):
        raise DemoError("pinned host Compose plugin is unavailable")
    rootless_pid = isolated.find_rootless_namespace_pid()
    route_evidence = isolated._namespace_route_check(rootless_pid)
    dockerd_pid = isolated.validate_dockerd_process(rootless_pid)
    daemon_fs_evidence = isolated.verify_daemon_filesystem_sandbox(dockerd_pid)
    isolated.validate_rootless_daemon()
    socket_identity = _socket_identity()
    _require_stock_image(socket_identity)
    if not isolated.JDK.is_dir() or isolated.JDK.is_symlink():
        raise DemoError("pinned JDK 21 installation is missing")
    if not isolated.BWRAP.is_file() or not os.access(isolated.BWRAP, os.X_OK):
        raise DemoError("pinned bubblewrap executable is missing")

    workspace = isolated._prepare_workspace(snapshot, result_dir)
    run_cache = isolated.RUN_CACHE_ROOT / run_id
    isolated._create_private_tree(isolated.RUN_CACHE_ROOT, "per-run cache directory")
    isolated._copy_cache(run_cache)
    etc = result_dir / "etc"
    isolated._write_minimal_etc(etc)
    base_argv = isolated.build_bwrap_argv(workspace, run_cache, etc)
    project = "crm-demo-" + uuid.uuid4().hex[:12]
    image_tag = f"crm-sales-leads-demo-backend:{project}"
    image_status, _ = _safe_docker_api(socket_identity, f"/images/{quote(image_tag, safe=':')}/json")
    if image_status != 404:
        raise DemoError("unique disposable backend image tag already exists")
    backend_port = 20000 + int(uuid.uuid4().hex[:4], 16) % 10000
    override = workspace / ".hermes-demo-override.yaml"
    env_file = workspace / ".hermes-demo.env"
    override.write_text(build_compose_override(project, backend_port), encoding="utf-8")
    env_file.write_text(build_environment(project, backend_port), encoding="utf-8")
    override.chmod(0o600)
    env_file.chmod(0o600)
    for filename in ("compose.yaml", str(SQL_DIR / SEED_NAME), str(SQL_DIR / ASSIGN_NAME)):
        target = workspace / filename
        if target.is_symlink() or not target.is_file():
            raise DemoError(f"required candidate demo file is missing: {filename}")
    if (workspace / ".env").exists() or (workspace / ".env").is_symlink():
        raise DemoError("snapshot unexpectedly contains a .env file")

    before_resources: dict[str, set[str]] | None = None
    cleanup_errors: list[str] = []
    data: dict[str, object] = {
        "request_id": job.request_id, "task_id": job.task_id, "sha": job.sha,
        "run_id": run_id, "mode": "isolated-compose-api-demo", "passed": False,
        "started_at_epoch": time.time(), "steps": [], "cleanup_errors": cleanup_errors,
        "network_route_evidence": route_evidence,
        "daemon_filesystem_sandbox_verified": daemon_fs_evidence["verified"],
        "daemon_filesystem_sandbox_evidence": daemon_fs_evidence,
    }
    logs: list[str] = []
    start = time.monotonic()
    result_code = 1
    with isolated._exclusive_runner_lock():
        before_resources = _safe_resource_snapshot(socket_identity)
        if before_resources["containers"]:
            raise DemoError("dedicated test daemon has leftover containers; refusing to interfere")
        try:
            # Render and validate the exact operator Compose config before the
            # costly offline Gradle build. This check itself starts no service.
            compose = _compose_base(workspace, override, env_file, project)
            config_text = _capture(_nsenter_argv(rootless_pid, socket_identity,
                                  [*base_argv, *compose, "config", "--format", "json"]), timeout=60)
            config_data = json.loads(config_text)
            data["compose"] = validate_compose_config(config_data, project, backend_port)
            (result_dir / "compose-config.json").write_text(
                json.dumps(data["compose"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
            data["steps"].append({"name":"compose-config-sales-leads-flag", "exit_code":0})

            smoke = _nsenter_argv(rootless_pid, socket_identity, [
                *base_argv, *isolated._smoke_command()])
            smoke_exit, smoke_timed_out = isolated._stream_process(smoke, result_dir / "smoke.log", 30)
            data["isolation_smoke"] = {"exit_code": smoke_exit, "timed_out": smoke_timed_out}
            if smoke_exit != 0 or smoke_timed_out:
                raise DemoError("bubblewrap isolation smoke failed")

            gradle_argv = [*base_argv, "./gradlew", "--offline", "--no-daemon",
                           "-Pkotlin.compiler.execution.strategy=in-process", ":backend:bootJar"]
            code, timed_out = isolated._stream_process(
                _nsenter_argv(rootless_pid, socket_identity, gradle_argv),
                result_dir / "backend-build.log", 1800)
            data["steps"].append({"name":"offline-backend-build", "exit_code":code, "timed_out":timed_out})
            if code != 0 or timed_out:
                raise DemoError("offline backend bootJar build failed")

            jars = sorted((workspace / "backend/build/libs").glob("*.jar"))
            jars = [p for p in jars if p.is_file() and not p.name.endswith(("-plain.jar", "-sources.jar"))]
            if len(jars) != 1 or jars[0].is_symlink():
                raise DemoError("backend build did not produce exactly one boot jar")
            image_context = workspace / ".hermes-demo-image"
            image_context.mkdir(mode=0o700)
            shutil.copyfile(jars[0], image_context / "backend.jar")
            (image_context / "Dockerfile").write_text(
                "FROM eclipse-temurin:21-jdk\nWORKDIR /app\nCOPY backend.jar /app/backend.jar\n"
                'ENTRYPOINT ["java", "-jar", "/app/backend.jar"]\n', encoding="ascii")
            dockerfile = image_context / "Dockerfile"
            image_build = ["docker", "build", "--pull=false", "--network=none", "--tag", image_tag,
                           "--file", ".hermes-demo-image/Dockerfile", ".hermes-demo-image"]
            output = _capture(_nsenter_argv(rootless_pid, socket_identity,
                              [*base_argv, *image_build]), timeout=300)
            (result_dir / "image-build.log").write_text(output[-MAX_OUTPUT_BYTES:], encoding="utf-8")
            data["steps"].append({"name":"rootless-backend-image-build", "exit_code":0})

            up_argv = [*base_argv, *compose, "up", "--detach", "--pull", "never",
                       "postgres", "redis", "minio"]
            output = _capture(_nsenter_argv(rootless_pid, socket_identity, up_argv),
                              timeout=180)
            logs.append("compose dependencies up:\n" + output[-5000:])
            data["steps"].append({"name":"compose-up-private-dependencies", "exit_code":0})

            runtime_hosts = _verified_project_service_ips(socket_identity, project)
            data["runtime_dns_override"] = {
                "mechanism":"per-run backend extra_hosts",
                "reason":"Docker embedded DNS is unavailable in the isolated rootless daemon configuration",
                "scope":"backend container only; no host DNS, network, or firewall changes",
                "services":dict(runtime_hosts),
            }
            override.write_text(build_compose_override(project, backend_port, runtime_hosts), encoding="utf-8")
            override.chmod(0o600)
            config_text = _capture(_nsenter_argv(rootless_pid, socket_identity,
                                  [*base_argv, *compose, "config", "--format", "json"]), timeout=60)
            config_data = json.loads(config_text)
            data["compose"] = validate_compose_config(config_data, project, backend_port, runtime_hosts)
            (result_dir / "compose-config.json").write_text(
                json.dumps(data["compose"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
            data["steps"].append({"name":"compose-config-verified-runtime-dns", "exit_code":0})

            up_backend = [*base_argv, *compose, "up", "--detach", "--pull", "never", "backend"]
            output = _capture(_nsenter_argv(rootless_pid, socket_identity, up_backend), timeout=180)
            logs.append("compose backend up:\n" + output[-5000:])
            data["steps"].append({"name":"compose-up-backend", "exit_code":0})
            # Keep the accepted result-review contract's aggregate step name;
            # it is recorded only after both dependency and backend phases pass.
            data["steps"].append({"name":"compose-up-private-services", "exit_code":0})

            ready_deadline = time.monotonic() + 600
            ready = False
            while time.monotonic() < ready_deadline:
                failure = _backend_startup_failure(socket_identity, project)
                if failure is not None:
                    data["backend_startup_log"] = _capture_backend_startup_log(
                        socket_identity, project, result_dir)
                    data["steps"].append({"name":"backend-readiness", "passed":False,
                                          "container_failure":failure,
                                          "log":data["backend_startup_log"]})
                    raise DemoError(f"isolated Compose backend terminated during startup ({failure}); see backend-startup.log")
                try:
                    readiness_url = f"http://127.0.0.1:{backend_port}/actuator/health/readiness"
                    _capture(_nsenter_argv(rootless_pid, socket_identity,
                        [*base_argv, "/usr/bin/python3", "-c",
                         "import sys,urllib.request; r=urllib.request.urlopen(sys.argv[1],timeout=5); "
                         "print(r.status); sys.exit(0 if r.status == 200 else 1)", readiness_url]), timeout=15)
                    ready = True
                    break
                except DemoError:
                    time.sleep(5)
            if not ready:
                raise DemoError("isolated Compose backend did not become ready")
            data["steps"].append({"name":"backend-readiness", "passed":True})

            seed = (workspace / SQL_DIR / SEED_NAME).read_text(encoding="utf-8")
            output = _capture(_nsenter_argv(rootless_pid, socket_identity,
                [*base_argv, *compose, "exec", "-T", "postgres", "psql", "-U", "crm", "-d",
                 "crm_platform", "-v", "ON_ERROR_STOP=1"]), timeout=60, input_text=seed)
            logs.append("seed:\n" + output[-5000:])
            data["steps"].append({"name":"synthetic-base-seed", "exit_code":0})

            # Wait for the candidate's own intake worker to create the two
            # leads from synthetic applications before running the documented
            # explicit assignment script.
            check_sql = "SELECT count(*) FROM sales_leads l JOIN avito_applications a ON a.id=l.source_application_id WHERE a.apply_id IN ('demo-apply-a','demo-apply-y') AND l.stage='NEW' AND l.assignment_state='UNASSIGNED';"
            intake_deadline = time.monotonic() + 90
            count = "0"
            while time.monotonic() < intake_deadline:
                count = _capture(_nsenter_argv(rootless_pid, socket_identity,
                    [*base_argv, *compose, "exec", "-T", "postgres", "psql", "-At", "-U", "crm",
                     "-d", "crm_platform", "-c", check_sql]), timeout=20).strip()
                if count == "2":
                    break
                time.sleep(5)
            if count != "2":
                raise DemoError("candidate intake did not create the two expected synthetic leads")
            assign = (workspace / SQL_DIR / ASSIGN_NAME).read_text(encoding="utf-8")
            output = _capture(_nsenter_argv(rootless_pid, socket_identity,
                [*base_argv, *compose, "exec", "-T", "postgres", "psql", "-U", "crm", "-d",
                 "crm_platform", "-v", "ON_ERROR_STOP=1"]), timeout=60, input_text=assign)
            logs.append("assignment:\n" + output[-5000:])
            data["steps"].append({"name":"documented-demo-assignment", "exit_code":0})

            y_sql = "SELECT l.id FROM sales_leads l JOIN avito_applications a ON a.id=l.source_application_id WHERE a.apply_id='demo-apply-y';"
            y_lead_id = _capture(_nsenter_argv(rootless_pid, socket_identity,
                [*base_argv, *compose, "exec", "-T", "postgres", "psql", "-At", "-U", "crm",
                 "-d", "crm_platform", "-c", y_sql]), timeout=20).strip()
            if not re.fullmatch(r"[0-9a-fA-F-]{36}", y_lead_id):
                raise DemoError("synthetic unassigned Y lead is missing or invalid")

            probe_file = workspace / PROBE_NAME
            probe_file.write_text(API_PROBE, encoding="utf-8")
            probe_argv = _nsenter_argv(rootless_pid, socket_identity,
                [*base_argv, "/usr/bin/python3", "/workspace/" + PROBE_NAME,
                 str(backend_port), y_lead_id])
            output = _capture(probe_argv, timeout=300)
            probe_data = json.loads(output.strip().splitlines()[-1])
            if not isinstance(probe_data, dict) or probe_data.get("passed") is not True:
                raise DemoError("API demo acceptance probe did not report success")
            data["api_demo"] = probe_data
            (result_dir / "api-demo.json").write_text(json.dumps(probe_data, ensure_ascii=False,
                indent=2, sort_keys=True) + "\n", encoding="utf-8")
            data["steps"].append({"name":"documented-api-walkthrough", "exit_code":0})

            task_query = "SELECT count(*) FROM sales_lead_tasks t JOIN sales_leads l ON l.id=t.lead_id JOIN avito_applications a ON a.id=l.source_application_id JOIN identity_users u ON u.id=t.assignee_user_id WHERE a.apply_id='demo-apply-a' AND t.kind='FIRST_CONTACT' AND t.status='OPEN' AND u.email='demo-m@sales-leads-demo.test' AND t.due_at IS NULL;"
            tasks = _capture(_nsenter_argv(rootless_pid, socket_identity,
                [*base_argv, *compose, "exec", "-T", "postgres", "psql", "-At", "-U", "crm",
                 "-d", "crm_platform", "-c", task_query]), timeout=20).strip()
            if tasks != "1":
                raise DemoError("TAKE did not create exactly one FIRST_CONTACT task with no due date")
            data["steps"].append({"name":"first-contact-task-due-at-null", "exit_code":0})
            result_code = 0
        except (DemoError, isolated.RunnerError, OSError, ValueError, json.JSONDecodeError) as exc:
            data["error"] = str(exc)
        finally:
            if before_resources is not None:
                cleanup_errors.extend(_cleanup_created_resources(socket_identity, before_resources))
            cleanup_errors.extend(_remove_demo_image(socket_identity, image_tag))

    data["duration_seconds"] = round(time.monotonic() - start, 3)
    data["passed"] = result_code == 0 and not cleanup_errors
    data["exit_code"] = 0 if data["passed"] else 1
    for disposable in (workspace, run_cache, etc):
        try:
            if disposable.is_symlink():
                cleanup_errors.append(f"refused symlink cleanup: {disposable.name}")
            elif disposable.exists():
                shutil.rmtree(disposable)
        except OSError as exc:
            cleanup_errors.append(f"failed to remove temporary {disposable.name}: {exc.__class__.__name__}")
    if cleanup_errors:
        data["passed"] = False
        data["exit_code"] = 1
        result_code = 1
    (result_dir / "demo.log").write_text("\n".join(logs)[-MAX_OUTPUT_BYTES:], encoding="utf-8")
    data["log"] = "demo.log"
    data["finished_at_epoch"] = time.time()
    result_path = result_dir / "result.json"
    result_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
    result_path.chmod(0o600)
    print(f"RESULT_JSON={result_path}", flush=True)
    return result_code



def _write_failure_result(job: DemoJob, run_id: str, result_dir: Path, exc: BaseException) -> int:
    errors: list[str] = []
    for disposable in (result_dir / "repo", isolated.RUN_CACHE_ROOT / run_id, result_dir / "etc"):
        try:
            if disposable.is_symlink():
                errors.append(f"refused symlink cleanup: {disposable.name}")
            elif disposable.exists():
                shutil.rmtree(disposable)
        except OSError as cleanup_exc:
            errors.append(f"failed to remove temporary {disposable.name}: {cleanup_exc.__class__.__name__}")
    data = {
        "request_id": job.request_id, "task_id": job.task_id, "sha": job.sha,
        "run_id": run_id, "mode": "isolated-compose-api-demo", "passed": False,
        "exit_code": 2, "error": f"setup or execution failed: {exc.__class__.__name__}: {str(exc)[:1000]}",
        "cleanup_errors": errors, "finished_at_epoch": time.time(),
    }
    try:
        log_path = result_dir / "demo.log"
        if not log_path.exists() and not log_path.is_symlink():
            log_path.write_text(f"Demo failed before a complete run: {exc.__class__.__name__}: {str(exc)[:1000]}\n",
                                encoding="utf-8")
            log_path.chmod(0o600)
        result_path = result_dir / "result.json"
        if not result_path.exists() and not result_path.is_symlink():
            result_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8")
            result_path.chmod(0o600)
        print(f"RESULT_JSON={result_path}", flush=True)
    except OSError:
        return 2
    return 2


def run_job(job: DemoJob) -> int:
    isolated._create_private_tree(isolated.RESULT_ROOT, "results directory")
    run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    result_dir = isolated.RESULT_ROOT / job.request_id / job.task_id / job.sha / run_id
    isolated._create_private_tree(result_dir, "demo run result directory", isolated.RESULT_ROOT)
    try:
        return _run_demo_job(job, run_id, result_dir)
    except BaseException as exc:
        return _write_failure_result(job, run_id, result_dir, exc)

def main(argv: Iterable[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, _operator_stop)
    signal.signal(signal.SIGINT, _operator_stop)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, dest="request_id")
    parser.add_argument("--task", required=True, dest="task_id")
    parser.add_argument("--sha", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return run_job(parse_job(args.request_id, args.task_id, args.sha))
    except (DemoError, isolated.RunnerError, OSError) as exc:
        print(f"isolated-demo-walkthrough: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
