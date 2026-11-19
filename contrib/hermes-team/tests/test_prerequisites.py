import json
import os
from pathlib import Path
import tempfile
import unittest

from team_engine.prerequisites import (
    ARTIFACT_NAME,
    CATALOG,
    PrerequisiteError,
    format_prerequisite_notice,
    load_prerequisites,
)


def artifact(directory: Path, entries=None, **overrides):
    value = {"schema_version": 1, "assignee": "team-backend", "run_id": "run-123",
             "prerequisites": entries if entries is not None else [
                 {"id": "ubuntu_uidmap", "evidence": "На медиасервере отсутствует newuidmap.",
                  "reason": "Для изолированного запуска тестовой базы нужен uidmap."}]}
    value.update(overrides)
    (directory / ARTIFACT_NAME).write_text(json.dumps(value), encoding="utf-8")


class PrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def rejects(self, path=None, **kwargs):
        with self.assertRaises(PrerequisiteError):
            load_prerequisites(path or self.path, **kwargs)

    def test_loads_and_normalizes_trusted_uidmap_catalog_entry(self):
        artifact(self.path)
        items = load_prerequisites(self.path, expected_assignee="team-backend", expected_run_id="run-123")
        self.assertEqual(items[0]["catalog"], CATALOG["ubuntu_uidmap"])
        notice = format_prerequisite_notice(items)
        self.assertIn("ssh -t alex@192.168.1.20 'sudo apt-get install -y uidmap'", notice)
        self.assertIn("вручную", notice)
        self.assertIn("не вводит пароль sudo", notice)
        self.assertIn("нажми «Установил — проверить снова»", notice)

    def test_rejects_artifact_from_another_role_or_retry(self):
        artifact(self.path)
        self.rejects(expected_assignee="team-qa")
        self.rejects(expected_run_id="stale-run")

    def test_missing_artifact_is_empty(self):
        self.assertEqual(load_prerequisites(self.path), [])
        self.assertEqual(format_prerequisite_notice([]), "")

    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        os.mkfifo(self.path / ARTIFACT_NAME)
        self.rejects()

    def test_malformed_or_command_bearing_role_payload_fails_closed(self):
        bad_entries = [
            {"id": "ubuntu_uidmap", "evidence": "ok", "reason": "ok", "command": "sudo rm -rf /"},
            {"id": "ubuntu_uidmap", "evidence": "", "reason": "ok"},
            {"id": "bad id", "evidence": "ok", "reason": "ok"},
            {"id": "unknown_cmd", "evidence": "Выполни sudo apt install anything.", "reason": "Проверка."},
            {"id": "unknown_cmd", "evidence": "Docker CLI отсутствует.", "reason": "docker run postgres"},
            {"id": "unknown_cmd", "evidence": "curl https://example.invalid/install.sh", "reason": "Причина."},
        ]
        for entry in bad_entries:
            with self.subTest(entry=entry):
                artifact(self.path, [entry])
                self.rejects()

    def test_unknown_catalog_id_is_diagnosis_only(self):
        artifact(self.path, [{"id": "host_docker", "evidence": "Проверено: Docker CLI отсутствует в контейнере агента.",
                              "reason": "Проверить доступность штатного инструмента."}])
        notice = format_prerequisite_notice(load_prerequisites(self.path))
        self.assertIn("Нужна диагностика", notice)
        self.assertIn("Проверено: Docker CLI отсутствует в контейнере агента.", notice)
        self.assertNotIn("Команда:", notice)
        self.assertNotIn("sudo", notice)

    def test_unsafe_or_unsupported_artifact_fails_closed(self):
        target = self.path / "source.json"
        target.write_text("{}", encoding="utf-8")
        (self.path / ARTIFACT_NAME).symlink_to(target)
        self.rejects()
        (self.path / ARTIFACT_NAME).unlink()
        (self.path / ARTIFACT_NAME).mkdir()
        self.rejects()
        (self.path / ARTIFACT_NAME).rmdir()
        for mutate in ("oversize", "version", "too_many", "extra_top"):
            with self.subTest(mutate=mutate):
                if mutate == "oversize":
                    (self.path / ARTIFACT_NAME).write_text(" " * 17000, encoding="utf-8")
                elif mutate == "version":
                    artifact(self.path, schema_version=2)
                elif mutate == "too_many":
                    artifact(self.path, [{"id": "unknown_" + str(i), "evidence": "e", "reason": "r"}
                                         for i in range(6)])
                else:
                    artifact(self.path, arbitrary_command="sudo apt install anything")
                self.rejects()

    def test_rejects_symlink_role_directory(self):
        actual = self.path / "actual"
        actual.mkdir()
        artifact(actual)
        link = self.path / "role"
        link.symlink_to(actual, target_is_directory=True)
        self.rejects(link)

    def test_managed_runtime_catalog_never_suggests_user_install_command(self):
        artifact(self.path, [{"id": "managed_jdk", "evidence": "Управляемый JDK не найден.",
                              "reason": "Изолированная среда не может запустить проверки."}])
        notice = format_prerequisite_notice(load_prerequisites(self.path))
        self.assertIn("обслуживания управляемой среды", notice)
        self.assertNotIn("Команда:", notice)
        self.assertNotIn("apt", notice)

    def test_renderer_ignores_caller_supplied_catalog_command(self):
        notice = format_prerequisite_notice([{
            "id": "ubuntu_uidmap", "reason": "Нужно обновление.", "evidence": "Проверено.",
            "catalog": {"title": "unsafe", "where": "unsafe", "command": "sudo rm -rf /"},
        }])
        self.assertIn(CATALOG["ubuntu_uidmap"]["command"], notice)
        self.assertNotIn("sudo rm -rf /", notice)


if __name__ == "__main__":
    unittest.main()
