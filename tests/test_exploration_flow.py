from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import main
from tests.test_transition_recorder import observation, event
from sap_explorer.action_recorder import SapRecorderCapabilities
from sap_explorer.connector import SapSessionInfo
from sap_explorer.repository import SapRepository
from sap_explorer.sanitization import SanitizationPolicy


class ExplorationFlowTests(unittest.TestCase):
    def test_full_cli_flow_persists_actions_screens_and_reports_without_live_sap(self):
        current = [observation()]
        snapshot_reader = Mock()
        snapshot_reader.capture.side_effect = lambda: current[0]
        class Recorder:
            pending_event_count = 0
            stop_reason = "keyboard_interrupt"
            def __init__(self, session, consumer, **kwargs):
                self.consumer = consumer
                self.idle = kwargs["on_idle"]
            def start(self):
                return SapRecorderCapabilities(True, False, True, True, False)
            def run(self):
                for kind in ("Change", "StartRequest", "EndRequest"):
                    self.consumer(event(kind))
                current[0] = observation(200, popup=True)
                self.idle()
            def stop(self):
                pass
        info = SapSessionInfo(0, 0, "con[0]", "TEST", "ses[0]", "TEST", "000", "TEST", "TEST", "SAPTEST", 100)
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "flow.sqlite3")
            with patch("main.SapSnapshotReader", return_value=snapshot_reader), patch("main.SapActionRecorder", Recorder):
                self.assertEqual(0, main.run_exploration(object(), info, db, SanitizationPolicy(), 1.0))
            with SapRepository(db) as repo:
                data = repo.export()
                self.assertEqual(2, len(data["states"]))
                self.assertEqual(1, len(data["history"]))
                self.assertEqual(1, data["stats"]["actions"])
                self.assertEqual("complete", data["history"][0]["status"])
                self.assertEqual("keyboard_interrupt", data["runs"][0]["stop_reason"])
            with patch("main.SapConnector", side_effect=AssertionError("Offline commands must not connect to SAP")):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(0, main.run("stats", db=db))
                self.assertEqual(2, json.loads(output.getvalue())["screens"])
                export = str(Path(directory) / "export.json")
                self.assertEqual(0, main.run("export", db=db, output=export))
                self.assertEqual(0, main.run("analyze", db=db, artifacts=directory))
                self.assertEqual(1, len(json.loads(Path(export).read_text(encoding="utf-8"))["transitions"]))
                self.assertTrue((Path(directory) / "exploration_report.md").is_file())

    def test_missing_database_does_not_create_an_empty_one(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "missing.sqlite3"
            with self.assertLogs("sap_explorer.cli", level="ERROR"):
                self.assertEqual(1, main.run("stats", db=str(db)))
            self.assertFalse(db.exists())

    def test_cli_rejects_nonfinite_interval(self):
        for value in ("nan", "inf", "0", "-1"):
            with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as error:
                main.main(["explore", "--interval", value])
            self.assertEqual(2, error.exception.code)
