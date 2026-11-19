"""Exercise the operator CLI against real local Git, without models or Hermes."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from team_engine.configure import main
from team_engine.contracts import ContractError


class ProjectContextCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-b', 'main')
        (self.repo / 'AGENTS.md').write_text('Use the existing project patterns.\n')
        self.git('add', 'AGENTS.md')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '-m', 'initial')
        self.path = self.root / 'config.json'
        self.path.write_text(json.dumps({'projects': {'demo': {
            'repo': str(self.repo), 'base_ref': 'main', 'checks': [['true']],
        }}}))

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def invoke(self, *args):
        output = io.StringIO()
        with patch('sys.argv', ['configure', '--config', str(self.path),
                                'project-context', 'demo', *args]), contextlib.redirect_stdout(output):
            main()
        return json.loads(output.getvalue())

    def test_preview_does_not_change_config_or_git_or_run_commands(self):
        original = self.path.read_bytes()
        result = self.invoke()
        self.assertTrue(result['ready'])
        self.assertIsNone(result['manifest'])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertNotIn('pack_path', result)

    def test_write_creates_immutable_package_outside_repo(self):
        result = self.invoke('--write', str(self.root / 'snapshots'))
        self.assertTrue(Path(result['pack_path']).is_file())
        self.assertIn('Use the existing project patterns.', Path(result['pack_path']).read_text())
        self.assertEqual(result, self.invoke('--write', str(self.root / 'snapshots')))

    def test_write_into_checkout_is_rejected(self):
        with self.assertRaisesRegex(ContractError, 'outside'):
            self.invoke('--write', str(self.repo / 'snapshot'))
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_required_missing_context_reports_failure_with_nonzero_exit(self):
        config = json.loads(self.path.read_text())
        config['projects']['demo']['context_required'] = True
        self.path.write_text(json.dumps(config))
        with self.assertRaises(SystemExit) as failure:
            self.invoke()
        self.assertEqual(failure.exception.code, 1)
        with self.assertRaises(SystemExit) as failure:
            self.invoke('--write', str(self.root / 'snapshots'))
        self.assertEqual(failure.exception.code, 1)
        self.assertFalse((self.root / 'snapshots').exists())


if __name__ == '__main__':
    unittest.main()
