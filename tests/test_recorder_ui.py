"""Exercita widgets reais sem SAP e o isolamento do worker COM."""
from dataclasses import asdict
from queue import Queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import main  # adds src to sys.path
from sap_explorer.capture_config import CaptureOptions
from sap_explorer.recording_service import RecordingWorker
from sap_explorer.sanitization import SanitizationPolicy
from tests.test_contextual_recording import INFO
from tests.test_transition_recorder import observation


class WorkerTests(unittest.TestCase):
    def test_discover_select_record_and_com_cleanup_use_one_worker_thread(self):
        calls = []
        def called(label):
            calls.append((label, threading.get_ident()))
        session = object()
        class Connector:
            def discover_sessions(self):
                called("discover")
                return [INFO]
            def select_session(self, connection, index):
                called("select")
                self.assertion = (connection, index)
                return session
        com = SimpleNamespace(CoInitialize=lambda: called("initialize"), CoUninitialize=lambda: called("uninitialize"))
        worker = RecordingWorker(db="unused", directory="unused", policy=SanitizationPolicy(), options=CaptureOptions(), interval=1)
        def capture(selected, info, **kwargs):
            self.assertIs(session, selected)
            called("record")
            worker.close()
        with patch.dict("sys.modules", {"pythoncom": com}), patch(
                "sap_explorer.recording_service.SapConnector", Connector), patch(
                "sap_explorer.recording_service.record_session", side_effect=capture):
            worker.commands.put(("discover", None))
            worker.commands.put(("record", (0, "Teste")))
            worker.thread.start()
            worker.thread.join(5)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(["initialize", "discover", "select", "record", "uninitialize"], [label for label, _ in calls])
        self.assertEqual(1, len({ident for _, ident in calls}))
        self.assertNotEqual(threading.get_ident(), calls[0][1])


class WindowTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from sap_explorer.recorder_ui import RecorderWindow
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.root.withdraw()
        self.worker = SimpleNamespace(settings={"directory": "artifacts"}, commands=Queue(), messages=Queue(),
            notes=Queue(), stop=threading.Event(), shutdown=threading.Event(),
            thread=Mock(is_alive=Mock(return_value=False)), close=Mock())
        self.window = RecorderWindow(self.root, self.worker)
        self.window.handle_message("sessions", [asdict(INFO)])
        self.window.handle_message("idle", "discover")

    def tearDown(self):
        if hasattr(self, "root"):
            # Cancel recurring Tk timers before destroying the interpreter.
            for after_id in self.root.tk.call("after", "info"):
                self.root.after_cancel(after_id)
            self.root.destroy()

    def test_selection_required_live_popup_notes_stop_and_files(self):
        self.assertEqual("disabled", str(self.window.start_button["state"]))
        self.assertEqual("0", str(self.window.sessions.item("0", "values")[0]))
        self.window.sessions.selection_set("0")
        self.window.update_buttons()
        self.window.start()
        self.assertEqual("normal", str(self.window.stop_button["state"]))
        popup = observation(popup=True)
        self.window.handle_message("screen", popup.to_dict())
        self.window.label.set("Conferência")
        self.window.expected.set("Mensagem de confirmação")
        self.window.annotate()
        self.assertEqual(("Conferência", "Mensagem de confirmação", popup.id), self.worker.notes.get_nowait())
        self.assertIn("wnd[1]", self.window.screen.get())
        self.window.stop()
        self.assertTrue(self.worker.stop.is_set())
        self.window.handle_message("files", {"json": "test.json", "markdown": "test.md"})
        self.window.handle_message("idle", "record")
        self.assertEqual("normal", str(self.window.open_button["state"]))
        self.assertEqual("disabled", str(self.window.stop_button["state"]))

    def test_discovery_errors_are_visible_and_retryable(self):
        self.window.handle_message("error", "SAPGUI não disponível")
        self.window.handle_message("idle", "discover")
        self.assertIn("SAPGUI", self.window.status.get())
        self.assertEqual("normal", str(self.window.refresh_button["state"]))
        self.window.discover()
        self.assertEqual((), self.window.sessions.get_children())
        self.assertEqual("disabled", str(self.window.stop_button["state"]))

    def test_close_requests_worker_shutdown_without_blocking_tk(self):
        self.window.close()
        self.worker.close.assert_called_once()
        self.assertTrue(self.window.closing)
        self.assertEqual("disabled", str(self.window.start_button["state"]))


if __name__ == "__main__":
    unittest.main()
