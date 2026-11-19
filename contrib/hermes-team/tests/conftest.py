"""Keep the bundled Hermes Team suite importable from its overlay location."""

from pathlib import Path
import sys


TEAM_ROOT = Path(__file__).resolve().parents[1]
if str(TEAM_ROOT) not in sys.path:
    sys.path.insert(0, str(TEAM_ROOT))

# Several team tests share fixtures through ``tests.test_controller``. The
# repository also has its own top-level ``tests`` package, so prefer this
# overlay's package path when the tests are run by the upstream test runner.
TEAM_TESTS = str(Path(__file__).resolve().parent)
import tests as _tests_package  # noqa: E402

_test_paths = list(getattr(_tests_package, "__path__", []))
if TEAM_TESTS in _test_paths:
    _test_paths.remove(TEAM_TESTS)
_tests_package.__path__ = [TEAM_TESTS, *_test_paths]
