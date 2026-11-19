"""Thin entry point loaded by Hermes' existing dashboard plugin loader."""
import sys
from pathlib import Path

runtime = Path('/home/alex/.hermes/team/runtime')
if str(runtime) not in sys.path:
    sys.path.insert(0, str(runtime))

from team_engine.dashboard_api import router  # noqa: E402,F401
