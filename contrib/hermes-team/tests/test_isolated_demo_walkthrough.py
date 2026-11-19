import sys
import signal
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import isolated_demo_walkthrough as demo
import demo_walkthrough_bridge as bridge


class DemoWalkthroughContractTests(unittest.TestCase):
    def setUp(self):
        self.project = "crm-demo-012345abcdef"
        self.port = 24567

    def test_only_exact_reviewed_t008_candidate_is_accepted(self):
        self.assertEqual(
            demo.parse_job(demo.REQUEST_ID, demo.TASK_ID, demo.COMMIT_SHA),
            demo.DemoJob(demo.REQUEST_ID, demo.TASK_ID, demo.COMMIT_SHA),
        )
        for args in (
            ("req-00000000000000000000", demo.TASK_ID, demo.COMMIT_SHA),
            (demo.REQUEST_ID, "T-009", demo.COMMIT_SHA),
            (demo.REQUEST_ID, demo.TASK_ID, "0" * 40),
        ):
            with self.subTest(args=args), self.assertRaises(demo.DemoError):
                demo.parse_job(*args)

    def test_environment_has_synthetic_credentials_and_disabled_avito(self):
        env = demo.build_environment(self.project, self.port)
        self.assertIn("CRM_SALES_LEADS_ENABLED=true", env)
        self.assertIn("CRM_AVITO_ENABLED=false", env)
        self.assertIn("CRM_AVITO_WORKER_ENABLED=false", env)
        self.assertIn("MINIO_ROOT_USER=demo-only-key", env)
        self.assertIn("MINIO_ROOT_PASSWORD=demo-only-secret", env)
        self.assertIn("CRM_BOOTSTRAP_ADMIN_EMAIL=", env)
        self.assertNotIn("CRM_CREDENTIALS_ENCRYPTION_KEY=some", env)
        self.assertIn("POSTGRES_PASSWORD=demo-only-password", env)

    def test_compose_override_disables_frontend_and_publications(self):
        override = demo.build_compose_override(self.project, self.port)
        self.assertIn("ports: !reset []", override)
        self.assertIn(f'127.0.0.1:{self.port}:8080', override)
        self.assertIn('profiles: ["operator-disabled"]', override)
        self.assertIn('CRM_SALES_LEADS_ENABLED: "true"', override)
        self.assertIn('CRM_AVITO_ENABLED: "false"', override)
        self.assertIn("build: !reset null", override)

    def test_compose_override_adds_only_the_three_verified_runtime_hosts(self):
        hosts = {"postgres":"172.30.0.2", "redis":"172.30.0.3", "minio":"172.30.0.4"}
        override = demo.build_compose_override(self.project, self.port, hosts)
        self.assertIn("extra_hosts: !override", override)
        for name, address in hosts.items():
            self.assertIn(f'"{name}:{address}"', override)
        with self.assertRaises(demo.DemoError):
            demo.build_compose_override(self.project, self.port, {"host.docker.internal":"127.0.0.1"})
        with self.assertRaises(demo.DemoError):
            demo.build_compose_override(self.project, self.port, {"postgres":"not-an-ip"})

    def valid_config(self):
        return {
            "name": self.project,
            "volumes": {name:{"name":f"{self.project}_{name}"} for name in
                        ("postgres_data", "redis_data", "minio_data")},
            "networks": {"default":{"name":f"{self.project}_default"}},
            "services": {
                "postgres": {"image": "postgres:18", "volumes":[{"type":"volume", "source":"postgres_data", "target":"/var/lib/postgresql"}], "ports": []},
                "redis": {"image": "redis:8-alpine", "volumes":[{"type":"volume", "source":"redis_data", "target":"/data"}], "ports": []},
                "minio": {"image": "minio/minio:RELEASE.2025-09-07T16-13-09Z", "environment":{"MINIO_ROOT_USER":"demo-only-key", "MINIO_ROOT_PASSWORD":"demo-only-secret"}, "volumes":[{"type":"volume", "source":"minio_data", "target":"/data"}], "ports": []},
                "backend": {
                    "image": f"crm-sales-leads-demo-backend:{self.project}",
                    "build": None,
                    "volumes": [],
                    "environment": {"CRM_SALES_LEADS_ENABLED":"true", "CRM_AVITO_ENABLED":"false",
                                    "CRM_AVITO_WORKER_ENABLED":"false",
                                    "CRM_OBJECT_STORAGE_ENDPOINT":"http://minio:9000",
                                    "CRM_OBJECT_STORAGE_ACCESS_KEY":"demo-only-key",
                                    "CRM_OBJECT_STORAGE_SECRET_KEY":"demo-only-secret",
                                    "CRM_OBJECT_STORAGE_BUCKET":"crm-platform-private",
                                    "CRM_OBJECT_STORAGE_CREATE_BUCKET":"true"},
                    "ports": [{"host_ip":"127.0.0.1", "published":str(self.port), "target":8080}],
                    "healthcheck":{"disable":True},
                },
            },
        }

    def test_valid_compose_config_reports_isolated_api_variant(self):
        evidence = demo.validate_compose_config(self.valid_config(), self.project, self.port)
        self.assertEqual(evidence["backend_loopback_port"], self.port)
        self.assertFalse(evidence["frontend_started"])
        self.assertFalse(evidence["avito_integration_enabled"])

    def test_default_profile_render_omits_frontend_and_caddy_volumes(self):
        config = self.valid_config()
        self.assertEqual(set(config["services"]), {"postgres", "redis", "minio", "backend"})
        self.assertEqual(set(config["volumes"]), {"postgres_data", "redis_data", "minio_data"})
        self.assertEqual(config["networks"]["default"]["name"], f"{self.project}_default")
        self.assertEqual(config["services"]["postgres"]["volumes"][0]["source"], "postgres_data")
        demo.validate_compose_config(config, self.project, self.port)

    def test_compose_config_requires_exact_verified_extra_hosts(self):
        hosts = {"postgres":"172.30.0.2", "redis":"172.30.0.3", "minio":"172.30.0.4"}
        config = self.valid_config()
        config["services"]["backend"]["extra_hosts"] = hosts
        evidence = demo.validate_compose_config(config, self.project, self.port, hosts)
        self.assertEqual(evidence["offline_runtime_dns_overrides"], hosts)
        with self.assertRaises(demo.DemoError):
            demo.validate_compose_config(config, self.project, self.port,
                                         {**hosts, "postgres":"172.30.0.9"})
        with self.assertRaises(demo.DemoError):
            demo.validate_compose_config(config, self.project, self.port)
        config["services"]["backend"]["extra_hosts"] = [
            f"{key}:{value}" for key, value in hosts.items()]
        demo.validate_compose_config(config, self.project, self.port, hosts)

    def test_backend_startup_exit_is_detected_without_waiting_for_global_deadline(self):
        with patch.object(demo, "_project_service_container", return_value=("a" * 64, {
                "State":{"Status":"exited", "ExitCode":1}})):
            self.assertEqual(demo._backend_startup_failure((1, 2), self.project),
                             "state=exited, exit_code=1")

    def test_runtime_hosts_come_from_exact_project_network_membership(self):
        network_id = "a" * 64
        containers = {name: (str(i) * 64, f"172.30.0.{i}")
                      for i, name in enumerate(("postgres", "redis", "minio"), 2)}
        network = {"Id":network_id,"Name":f"{self.project}_default",
                   "Labels":{"com.docker.compose.project":self.project},
                   "IPAM":{"Config":[{"Subnet":"172.30.0.0/16"}]},
                   "Containers":{cid:{"IPv4Address":f"{addr}/16"} for cid, addr in containers.values()}}
        replies = [network]
        for service, (cid, address) in containers.items():
            replies.extend([[{"Id":cid}], {"Config":{"Labels":{"com.docker.compose.project":self.project,
                                                       "com.docker.compose.service":service}},
                             "State":{"Running":True},
                             "NetworkSettings":{"Networks":{f"{self.project}_default":
                                 {"NetworkID":network_id,"IPAddress":address}}}}])
        with patch.object(demo, "_safe_docker_json", side_effect=replies):
            result = demo._verified_project_service_ips((1, 2), self.project)
        self.assertEqual(result, {name:address for name, (_cid, address) in containers.items()})

        network["Containers"][containers["postgres"][0]]["IPv4Address"] = "192.0.2.4/24"
        cid, address = containers["postgres"]
        with patch.object(demo, "_safe_docker_json", side_effect=[network, [{"Id":cid}],
                {"Config":{"Labels":{"com.docker.compose.project":self.project,
                                     "com.docker.compose.service":"postgres"}},
                 "State":{"Running":True},
                 "NetworkSettings":{"Networks":{f"{self.project}_default":
                     {"NetworkID":network_id,"IPAddress":address}}}}]):
            with self.assertRaises(demo.DemoError):
                demo._verified_project_service_ips((1, 2), self.project, ("postgres",))

    def test_rejects_external_publication(self):
        config = self.valid_config()
        config["services"]["backend"]["ports"][0]["host_ip"] = "0.0.0.0"
        with self.assertRaises(demo.DemoError):
            demo.validate_compose_config(config, self.project, self.port)

    def test_rejects_missing_feature_flag(self):
        config = self.valid_config()
        config["services"]["backend"]["environment"]["CRM_SALES_LEADS_ENABLED"] = "false"
        with self.assertRaises(demo.DemoError):
            demo.validate_compose_config(config, self.project, self.port)

    def test_rejects_project_service_or_build_drift(self):
        config = self.valid_config()
        config["services"]["frontend"] = {"image":"caddy:2.10.2-alpine"}
        with self.assertRaises(demo.DemoError):
            demo.validate_compose_config(config, self.project, self.port)
        config = self.valid_config()
        config["services"]["backend"]["build"] = {"context":"."}
        with self.assertRaises(demo.DemoError):
            demo.validate_compose_config(config, self.project, self.port)
        config = self.valid_config()
        config["services"]["backend"]["healthcheck"] = {"test":["CMD", "curl"]}
        with self.assertRaises(demo.DemoError):
            demo.validate_compose_config(config, self.project, self.port)

    def test_rejects_real_avito_and_wrong_services(self):
        config = self.valid_config()
        config["services"]["backend"]["environment"]["CRM_AVITO_ENABLED"] = "true"
        with self.assertRaises(demo.DemoError):
            demo.validate_compose_config(config, self.project, self.port)
        config = self.valid_config()
        del config["services"]["redis"]
        with self.assertRaises(demo.DemoError):
            demo.validate_compose_config(config, self.project, self.port)

    def test_rejects_host_bind_mounts(self):
        config = self.valid_config()
        config["services"]["postgres"]["volumes"] = [{"type":"bind", "source":"/home/alex/apps", "target":"/tmp"}]
        with self.assertRaises(demo.DemoError):
            demo.validate_compose_config(config, self.project, self.port)

    def test_compose_volume_sources_are_logical_keys_with_unique_declared_names(self):
        config = self.valid_config()
        config["services"]["postgres"]["volumes"][0]["source"] = f"{self.project}_postgres_data"
        with self.assertRaises(demo.DemoError):
            demo.validate_compose_config(config, self.project, self.port)

    def test_runtime_images_are_pinned_by_exact_rootless_image_id(self):
        values = list(demo.PINNED_RUNTIME_IMAGES.values())
        with patch.object(demo, "_safe_docker_json", side_effect=[{"Id":demo.TEMURIN_IMAGE_ID},
                    {"Id": "sha256:wrong"}, *[{"Id":value} for value in values[1:]]]):
            with self.assertRaises(demo.DemoError):
                demo._require_stock_image((1, 2))

    def test_disposable_image_cleanup_uses_only_the_generated_tag(self):
        tag = "crm-sales-leads-demo:crm-demo-012345abcdef"
        identity = (17, 23)
        with patch.object(demo, "_safe_docker_api", side_effect=[(200, b"{}"), (200, b"{}")]) as api:
            self.assertEqual(demo._remove_demo_image(identity, tag), [])
        self.assertEqual(api.call_args_list[0].args[1], "/images/crm-sales-leads-demo:crm-demo-012345abcdef/json")
        self.assertEqual(api.call_args_list[1].args[1], "/images/crm-sales-leads-demo:crm-demo-012345abcdef?force=1&noprune=1")
        self.assertEqual(api.call_args_list[1].kwargs["method"], "DELETE")

    def test_resource_cleanup_deletes_only_ids_created_after_the_baseline(self):
        before = {"containers":{"base-container"}, "networks":{"base-network"}, "volumes":{"base-volume"}}
        after = {"containers":{"base-container", "abcdef123456"},
                 "networks":{"base-network", "abcdefabcdef"},
                 "volumes":{"base-volume", "new_volume"}}
        identity = (17, 23)
        with patch.object(demo, "_safe_resource_snapshot", side_effect=[after, after]), \
             patch.object(demo, "_safe_docker_api", return_value=(204, b"")) as api:
            self.assertEqual(demo._cleanup_created_resources(identity, before), [])
        paths = [call.args[1] for call in api.call_args_list]
        self.assertIn("/containers/abcdef123456?force=1&v=1", paths)
        self.assertIn("/networks/abcdefabcdef", paths)
        self.assertIn("/volumes/new_volume", paths)
        self.assertNotIn("/containers/base-container?force=1&v=1", paths)
        self.assertNotIn("/networks/base-network", paths)
        self.assertNotIn("/volumes/base-volume", paths)

    def test_socket_identity_change_refuses_docker_api_use(self):
        with patch.object(demo, "_verify_socket", side_effect=demo.DemoError("changed")), \
             patch.object(demo.isolated, "_docker_api") as api:
            with self.assertRaises(demo.DemoError):
                demo._safe_docker_api((1, 2), "/info")
        api.assert_not_called()

    def test_runner_term_enters_cleanup_path(self):
        with self.assertRaises(demo.DemoCancelled):
            demo._operator_stop(signal.SIGTERM, None)

    def test_bridge_forwards_cancel_and_allows_cleanup_grace(self):
        class FakeProcess:
            pid = 7654

            def __init__(self, timeout=False):
                self.timeout = timeout
                self.waits = []

            def poll(self):
                return None

            def wait(self, timeout=None):
                self.waits.append(timeout)
                if timeout and self.timeout:
                    raise subprocess.TimeoutExpired("runner", timeout)
                return 0

        process = FakeProcess()
        with patch.object(bridge.os, "killpg") as killpg:
            bridge._forward_cancel(process, signal.SIGTERM)
        killpg.assert_called_once_with(process.pid, signal.SIGTERM)
        self.assertEqual(process.waits, [120])

        process = FakeProcess(timeout=True)
        with patch.object(bridge.os, "killpg"), \
             patch.object(bridge.staging, "_terminate_process_tree") as terminate:
            bridge._forward_cancel(process, signal.SIGTERM)
        terminate.assert_called_once_with(process.pid)
        self.assertEqual(process.waits, [120, None])

    def test_bridge_launches_only_identifier_arguments(self):
        job = demo.DemoJob(demo.REQUEST_ID, demo.TASK_ID, demo.COMMIT_SHA)
        argv = bridge.build_runner_argv(job)
        self.assertEqual(argv[-6:], ["--request", demo.REQUEST_ID, "--task", demo.TASK_ID, "--sha", demo.COMMIT_SHA])
        self.assertNotIn("--command", argv)
        with self.assertRaises(bridge.BridgeError):
            bridge.build_runner_argv(demo.DemoJob(demo.REQUEST_ID, demo.TASK_ID, "0" * 40))

    def test_bridge_cancellation_preserves_child_failure_artifact(self):
        class FakeProcess:
            returncode = 2
        process = FakeProcess()
        tail = bytearray(b"RESULT_JSON=/tmp/result.json\n")
        with patch.object(bridge, "_forward_cancel") as forward, \
             patch.object(bridge, "_drain_after_exit") as drain:
            code, output = bridge._finish_cancelled_runner(process, signal.SIGTERM, tail)
        forward.assert_called_once_with(process, signal.SIGTERM)
        drain.assert_called_once_with(process, tail)
        self.assertEqual(code, 2)
        self.assertIn("RESULT_JSON=/tmp/result.json", output)

    def test_bridge_uses_private_umask_for_staging_and_restores_callers_umask(self):
        job = demo.DemoJob(demo.REQUEST_ID, demo.TASK_ID, demo.COMMIT_SHA)
        original = bridge.os.umask(0)
        bridge.os.umask(original)
        try:
            with patch.object(bridge.staging, "_ensure_fixed_directory"), \
                 patch.object(bridge.staging, "_run_helper", side_effect=lambda *_: self._assert_umask()), \
                 patch.object(bridge.staging, "_stage_bundle"), \
                 patch.object(bridge.staging, "stage_gradle_cache"), \
                 patch.object(bridge, "_run_runner", return_value=(0, "RESULT_JSON=/tmp/result.json\n")), \
                 patch.object(bridge, "_result_dir", return_value=Path("/tmp/demo-run")), \
                 patch.object(bridge, "publish_result", return_value=Path("/tmp/published")):
                bridge.os.umask(0o002)
                self.assertEqual(bridge.run_job(job), 0)
                current = bridge.os.umask(0)
                bridge.os.umask(current)
                self.assertEqual(current, 0o002)
        finally:
            bridge.os.umask(original)

    @staticmethod
    def _assert_umask():
        current = bridge.os.umask(0)
        bridge.os.umask(current)
        if current != 0o077:
            raise AssertionError(f"bridge staging umask was {current:o}, expected 077")
        return {"verified":True}


if __name__ == "__main__":
    unittest.main()
