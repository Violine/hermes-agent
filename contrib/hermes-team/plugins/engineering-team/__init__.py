"""Telegram interface for the durable engineering-team workflow.

All approvals are explicit owner actions. The language model is never in the
authorization or callback decision path.
"""
import os
from pathlib import Path
import sys

# Hermes imports plugin packages outside the bundle's normal Python package
# path. Mirror the dashboard entrypoint so the same runtime copy of team_engine
# is used in both the Telegram gateway and the dashboard process.
_runtime = Path(os.environ.get("HERMES_TEAM_RUNTIME", "/home/alex/.hermes/team/runtime"))
if str(_runtime) not in sys.path:
    sys.path.insert(0, str(_runtime))

from team_engine.telegram_workflow import register

__all__ = ["register"]
