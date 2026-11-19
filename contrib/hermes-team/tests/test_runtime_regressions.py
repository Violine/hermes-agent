from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from team_engine.contracts import ContractError
from team_engine.controller import Controller


class RuntimeRegressionTests(unittest.TestCase):
    def test_rejects_multi_feature_configuration(self):
        """Global scheduling is unsupported; reject unsafe concurrency at startup."""
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({
                "max_active_features": 2,
                "data_root": str(Path(tmp) / "data"),
            }), encoding="utf-8")

            with self.assertRaisesRegex(ContractError, "max_active_features=1"):
                Controller(config)

            self.assertFalse((Path(tmp) / "data").exists())

    def test_resume_rejects_when_another_feature_is_active_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_root = root / "data"
            runs = data_root / "runs"
            blocked_dir = runs / "f-blocked"
            active_dir = runs / "f-active"
            blocked_dir.mkdir(parents=True)
            active_dir.mkdir(parents=True)
            blocked_state = {
                "id": "f-blocked", "project": "demo", "title": "blocked",
                "stage": "blocked", "resume_stage": "planning", "blocker": "needs fix",
                "root_card": "root-blocked", "index": 0, "items": [],
            }
            active_state = {
                "id": "f-active", "project": "demo", "title": "active",
                "stage": "analysis", "root_card": "root-active",
            }
            blocked_file = blocked_dir / "state.json"
            blocked_file.write_text(json.dumps(blocked_state), encoding="utf-8")
            (active_dir / "state.json").write_text(json.dumps(active_state), encoding="utf-8")

            config = root / "config.json"
            config.write_text(json.dumps({
                "max_active_features": 1,
                "data_root": str(data_root),
                "bundle_root": str(root / "bundle"),
                "board": "test-board",
                "hermes_root": str(root / "hermes"),
            }), encoding="utf-8")

            class Board:
                def __init__(self, *_args, **_kwargs):
                    self.activated = []
                    self.comments = []

                def ensure_board(self, *_args, **_kwargs):
                    pass

                def activate_task(self, card_id):
                    self.activated.append(card_id)

                def add_comment(self, *args):
                    self.comments.append(args)

            board = Board()
            argv = ["team_engine.controller", "--config", str(config),
                    "resume", "f-blocked", "--reason", "cause fixed"]
            with patch("team_engine.controller.HermesBoard", return_value=board), \
                 patch("sys.argv", argv):
                with self.assertRaisesRegex(ContractError, "Active feature limit reached"):
                    from team_engine.controller import main
                    main()

            self.assertEqual(json.loads(blocked_file.read_text(encoding="utf-8")), blocked_state)
            self.assertEqual(board.activated, [])
            self.assertEqual(board.comments, [])
            self.assertFalse((blocked_dir / "events.jsonl").exists())

    def test_resume_accepts_ready_root_card_and_consumes_resume_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_root = root / "data"
            run_dir = data_root / "runs" / "f-blocked"
            run_dir.mkdir(parents=True)
            state = {
                "id": "f-blocked", "project": "demo", "title": "recover plan review",
                "stage": "blocked", "resume_stage": "plan_challenge", "blocker": "restored",
                "root_card": "root-card", "index": 0, "items": [],
            }
            state_file = run_dir / "state.json"
            state_file.write_text(json.dumps(state), encoding="utf-8")
            config = root / "config.json"
            config.write_text(json.dumps({
                "max_active_features": 1,
                "data_root": str(data_root),
                "bundle_root": str(root / "bundle"),
                "board": "test-board",
                "hermes_root": str(root / "hermes"),
            }), encoding="utf-8")

            class Board:
                def __init__(self):
                    self.tasks = {"root-card": {"id": "root-card", "status": "ready"}}
                    self.activated = []
                    self.comments = []

                def ensure_board(self, *_args, **_kwargs):
                    pass

                def get_task(self, task_id):
                    return self.tasks[task_id]

                def activate_task(self, task_id):
                    self.activated.append(task_id)
                    self.tasks[task_id]["status"] = "ready"

                def add_comment(self, *args):
                    self.comments.append(args)

            board = Board()
            argv = ["team_engine.controller", "--config", str(config),
                    "resume", "f-blocked", "--reason", "native root restored"]
            with patch("team_engine.controller.HermesBoard", return_value=board), \
                 patch("sys.argv", argv):
                from team_engine.controller import main
                main()

            resumed = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(resumed["stage"], "plan_challenge")
            self.assertNotIn("resume_requested", resumed)
            self.assertNotIn("blocker", resumed)
            self.assertEqual(board.activated, [])
            self.assertEqual(board.comments, [(
                "root-card", "team-controller", "Владелец возобновил: native root restored"
            )])
            events = (run_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("Владелец возобновил: native root restored", events)


if __name__ == "__main__":
    unittest.main()
