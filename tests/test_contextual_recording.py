from __future__ import annotations

import ast
from dataclasses import replace
import json
from pathlib import Path
from queue import Queue
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import main
from sap_explorer.action_recorder import SapRecorderCapabilities
from sap_explorer.capture_config import CaptureOptions
from sap_explorer.connector import SapSessionInfo
from sap_explorer.contextual_report import build_contextual_report, command_preview, write_contextual_report
from sap_explorer.events import SapRecordedCommand, normalize_command_array
from sap_explorer.models import SapTransition
from sap_explorer.object_tree import SapUiNode
from sap_explorer.recording_service import record_session
from sap_explorer.repository import SapRepository
from sap_explorer.sanitization import SanitizationPolicy
from sap_explorer.screen_reader import SapScreenReader, SapStatusBar
from tests.test_transition_recorder import observation, event


INFO = SapSessionInfo(0, 0, "con[0]", "TEST", "ses[0]", "TEST", "000", "TEST", "TEST", "SAPTEST", 100)


def recorded(rows, component="wnd[0]/usr/ctxtVALUE"):
    raw, commands = normalize_command_array(rows)
    return replace(event(), component_id=component, commands=commands, raw_commands=raw)


class ContextualReportTests(unittest.TestCase):
    def test_preview_preserves_chain_types_and_escaping(self):
        action = recorded((("M", "getAbsoluteRow", (1,)), ("SP", "selected", True)))
        preview = command_preview(action.to_dict())
        self.assertEqual("session.findById('wnd[0]/usr/ctxtVALUE').getAbsoluteRow(1).selected = True", preview["python"])
        ast.parse(preview["python"])
        value = "  \"quoted\"\n" + "long" * 200
        preview = command_preview(recorded(("SP", "Text", value)).to_dict())
        self.assertEqual(value, ast.literal_eval(ast.parse(preview["python"]).body[0].value))

    def test_preview_requires_lossless_supported_commands(self):
        for action in (recorded(("SP", "Text", "[REDACTED]")),
                       recorded(("X", "Text", "x")), recorded(("M", "__class__")), event(),
                       recorded((("SP", "Text", "x"), ("M", "press")))):
            self.assertIsNone(command_preview(action.to_dict())["python"])

    def test_report_retains_order_context_popups_messages_and_only_selected_run(self):
        with tempfile.TemporaryDirectory() as directory, SapRepository(Path(directory) / "capture.db") as repo:
            case = repo.create_case("Teste", "Teste", {}, SanitizationPolicy())
            run = repo.start_run({}, case_id=case)
            repo.set_capabilities(run, SapRecorderCapabilities(True, False, True, True, False))
            before = observation()
            before.state.ui_tree.roots[0].children.append(SapUiNode(
                id="wnd[0]/usr/ctxtVALUE", type="GuiCTextField", text="DEFAULT", changeable=True))
            after = observation(200, popup=True, status=SapStatusBar("Erro de validação", "E", "Z1", "001"))
            after.state.ui_tree.roots[0].children.append(SapUiNode(id="wnd[1]/usr/txt", type="GuiLabel", text="Informe o valor"))
            repo.save_observation(run, before)
            action = recorded(("SP", "Text", "ABC"))
            event_id = repo.save_event(run, action, before)
            repo.save_transition(run, SapTransition(before, (action,), after, (event_id,), batch={
                "id": "batch1", "event_ids": [event_id], "exact_before_known": False}))
            repo.mark_case(case, "Conferir entrada", "Esperado: mensagem de validação", observation_id=before.id)
            other_run = repo.start_run({"system": "OTHER"})
            repo.save_observation(other_run, observation(999))
            report = build_contextual_report(repo, run)
            step = report["steps"][0]
            self.assertEqual("batch1", step["batch"]["id"])
            self.assertEqual("DEFAULT", step["actions"][0]["control_context"]["text"])
            self.assertEqual("Z1", step["expected_result"]["status_bar"]["message_id"])
            self.assertEqual("unconfirmed", step["expected_result"]["functional_success"])
            self.assertEqual("Informe o valor", step["expected_result"]["popup"]["texts"][0]["text"])
            self.assertEqual(before.id, step["annotations"][0]["observation_id"])
            self.assertEqual(2, len(report["observations"]))
            self.assertEqual([1, 2, 3, 4], [row["sequence"] for row in report["timeline"]])
            paths = write_contextual_report(repo, run, directory)
            loaded = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
            self.assertEqual(action.to_dict()["raw_commands"], loaded["events"][0]["event"]["raw_commands"])
            self.assertIn("Etapa 1", Path(paths["markdown"]).read_text(encoding="utf-8"))

    def test_unobserved_result_orphan_and_capture_failure_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory, SapRepository(Path(directory) / "capture.db") as repo:
            run = repo.start_run({})
            action = recorded(("M", "press"))
            eid = repo.save_event(run, action, None)
            repo.save_event(run, replace(event(), event_type="CaptureError", commands=()), None)
            repo.save_transition(run, SapTransition(None, (action,), None, (eid,), "incomplete", "missing_end_request"))
            repo.save_event(run, action, None)
            report = build_contextual_report(repo, run)
            self.assertEqual("unavailable", report["steps"][0]["expected_result"]["basis"])
            self.assertEqual(1, len(report["unassigned_actions"]))
            self.assertGreaterEqual(len(report["warnings"]), 4)

    def test_note_cannot_bind_to_another_case(self):
        with tempfile.TemporaryDirectory() as directory, SapRepository(Path(directory) / "capture.db") as repo:
            case = repo.create_case("a", "a", {}, SanitizationPolicy())
            run = repo.start_run({})
            screen = observation()
            repo.save_observation(run, screen)
            with self.assertRaises(ValueError):
                repo.mark_case(case, "bad", observation_id=screen.id)

    def test_statusbar_captures_eight_parameters_and_redacts(self):
        bar = SimpleNamespace(Text="OK", MessageType="S", MessageId="Z", MessageNumber="12",
                              MessageAsPopup=False, MessageHasLongText=True,
                              MessageParameter=lambda i: "secret" if i == 0 else str(i))
        session = SimpleNamespace(FindById=lambda *args: bar)
        state = SapScreenReader(session, sanitizer=SanitizationPolicy(text_patterns=("secret",))).read_status_bar()
        self.assertEqual(("[REDACTED]", "1", "2", "3", "4", "5", "6", "7"), state.message_parameters)
        self.assertTrue(state.message_has_long_text)

    def test_default_and_record_launch_gui_without_cli_selection(self):
        for command in (None, "record", "gui"):
            with patch("sap_explorer.recorder_ui.launch_recorder", return_value=0) as launch, patch("main.SapConnector", side_effect=AssertionError):
                self.assertEqual(0, main.run(command))
                launch.assert_called_once()


class RecordingServiceTests(unittest.TestCase):
    def simulate(self, directory, *, fail=False):
        current = [observation()]
        messages = []
        notes = Queue()
        final_action = recorded(("SP", "Text", "última edição"))
        class Recorder:
            pending_event_count = 0
            stop_reason = "session_destroyed" if fail else "stop_requested"
            stopped = False
            def __init__(self, session, consumer, **kwargs):
                self.consumer = consumer
                self.idle = kwargs["on_idle"]
                self.sanitizer = kwargs["sanitizer"]
            def start(self):
                return SapRecorderCapabilities(True, False, True, True, False)
            def run(self):
                notes.put(("Entrada", "Resultado esperado", current[0].id))
                for kind in ("Change", "StartRequest", "EndRequest"):
                    self.consumer(recorded(("M", "press")) if kind == "Change" else event(kind))
                current[0] = observation(200, popup=True)
                self.idle()
                if fail:
                    self.consumer(event("Destroy"))
                    raise RuntimeError("Session gone")
            def stop(self):
                if not self.stopped:
                    self.stopped = True
                    if not fail:
                        self.consumer(final_action)
            def request_stop(self):
                pass
        db = str(Path(directory) / "record.db")
        with patch("sap_explorer.recording_service.SapActionRecorder", Recorder), patch(
                "sap_explorer.recording_service.SapSnapshotReader") as reader:
            reader.return_value.capture.side_effect = lambda: current[0]
            kwargs = dict(db=db, directory=directory, name="Teste", policy=SanitizationPolicy(),
                          options=CaptureOptions(), interval=0.1, notify=lambda k, p: messages.append((k, p)),
                          stop=Event(), notes=notes)
            if fail:
                with self.assertRaises(RuntimeError):
                    record_session(object(), INFO, **kwargs)
            else:
                record_session(object(), INFO, **kwargs)
        with SapRepository(db) as repo:
            run = repo.export()["runs"][0]
            report = build_contextual_report(repo, run["id"])
        return messages, report

    def test_final_change_is_exported_and_note_stays_on_original_screen(self):
        with tempfile.TemporaryDirectory() as directory:
            messages, report = self.simulate(directory)
            self.assertEqual("última edição", report["steps"][-1]["actions"][0]["event"]["commands"][0]["parameters"][0])
            self.assertEqual(report["observations"][0]["id"], report["annotations"][0]["observation_id"])
            self.assertEqual("files", messages[-1][0])
            self.assertIsNotNone(report["run"]["ended_at"])

    def test_error_still_exports_persisted_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            messages, report = self.simulate(directory, fail=True)
            self.assertEqual("error", report["run"]["stop_reason"])
            self.assertIn("files", [kind for kind, _ in messages])
            self.assertIsNotNone(report["run"]["ended_at"])


if __name__ == "__main__":
    unittest.main()
