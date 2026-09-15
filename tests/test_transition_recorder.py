from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sap_explorer.events import SapActionEvent, SapRecordedCommand
from sap_explorer.models import SapScreenObservation
from sap_explorer.object_tree import SapUiNode, SapUiTree
from sap_explorer.screen_reader import SapScreenState, SapStatusBar
from sap_explorer.transition_recorder import SapSnapshotReader, SapTransitionRecorder


def observation(screen=100, *, popup=False, status=None, text="Test"):
    window = "wnd[1]" if popup else "wnd[0]"
    return SapScreenObservation(SapScreenState(
        transaction="TEST", program="SAPTEST", screen_number=screen,
        active_window_id=window, active_window_type="GuiModalWindow" if popup else "GuiMainWindow",
        active_window_text=text, window_count=2 if popup else 1, has_additional_windows=popup,
        is_busy=False, session_id="/app/con[0]/ses[0]", status_bar=status,
        ui_tree=SapUiTree([SapUiNode(id=window, type="GuiMainWindow")], "recursive", window)))


def event(kind="Change", member="press", parameter=None):
    commands = (SapRecordedCommand("M", member, () if parameter is None else (parameter,)),) if kind == "Change" else ()
    return SapActionEvent(datetime.now(timezone.utc), kind, "wnd[0]/tbar[1]/btn[8]", "GuiButton", commands=commands)


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.captured = observation()
        self.outputs = []
        self.initial = []
        self.journal = []
        self.now = 0.0
        self.capture = Mock(side_effect=lambda: self.captured)
        def journal(e, before):
            self.journal.append((e, before))
            return str(len(self.journal))
        self.recorder = SapTransitionRecorder(self.capture, self.outputs.append,
            on_observation=self.initial.append, on_event=journal, monotonic=lambda: self.now)
        self.recorder.poll()

    def request(self):
        for kind in ("Change", "StartRequest", "EndRequest"):
            self.recorder.handle_event(event(kind))

    def test_simple_action_correlates_before_and_after(self):
        first = self.captured
        self.request()
        self.captured = observation(200)
        self.recorder.poll()
        transition = self.outputs[0]
        self.assertEqual(first, transition.before_state)
        self.assertEqual(self.captured, transition.after_state)
        self.assertEqual(("1",), transition.action_ids)
        self.assertEqual("complete", transition.status)

    def test_callbacks_never_read_com_and_busy_waits_for_end(self):
        self.recorder.handle_event(event())
        self.recorder.handle_event(event("StartRequest"))
        self.recorder.poll(force=True)
        self.assertEqual(1, self.capture.call_count)
        self.assertFalse(self.outputs)

    def test_multiple_changes_are_preserved_in_order(self):
        self.recorder.handle_event(event(member="text", parameter="42"))
        self.request()
        self.recorder.poll()
        self.assertEqual(["text", "press"], [a.commands[0].member_name for a in self.outputs[0].actions])

    def test_popup_open_and_close_without_events(self):
        self.captured = observation(popup=True)
        self.recorder.poll(force=True)
        self.captured = observation()
        self.recorder.poll(force=True)
        self.assertEqual(2, len(self.outputs))
        self.assertTrue(self.outputs[0].after_state.state.has_additional_windows)
        self.assertEqual("state_change_without_action", self.outputs[0].reason)

    def test_status_only_change_is_detected(self):
        self.captured = observation(status=SapStatusBar("Erro", "E", "ZTEST", "001"))
        self.recorder.poll(force=True)
        self.assertEqual("E", self.outputs[0].to_dict()["status_bar"]["message_type"])
        self.assertEqual(self.initial[0].fingerprint.structural_hash, self.captured.fingerprint.structural_hash)
        self.assertNotEqual(self.initial[0].fingerprint.content_hash, self.captured.fingerprint.content_hash)

    def test_destroy_preserves_pending_actions_as_incomplete(self):
        self.recorder.handle_event(event())
        self.recorder.handle_event(event("Destroy"))
        self.recorder.finish("session_destroyed")
        self.assertEqual("incomplete", self.outputs[0].status)
        self.assertIsNone(self.outputs[0].after_state)
        self.assertEqual(1, self.capture.call_count)

    def test_disconnected_session_is_not_read_at_finish(self):
        self.recorder.handle_event(event())
        self.recorder.finish("session_disconnected")
        self.assertEqual(1, self.capture.call_count)
        self.assertEqual("session_disconnected", self.outputs[0].reason)

    def test_missing_end_recovers_only_after_readable_snapshot(self):
        self.recorder.handle_event(event())
        self.recorder.handle_event(event("StartRequest"))
        self.now = 31
        saved, self.captured = self.captured, None
        self.recorder.poll()
        self.assertFalse(self.outputs)
        self.captured = saved
        self.now = 32
        self.recorder.poll()
        self.assertEqual("missing_end_request", self.outputs[0].reason)
        self.assertEqual("incomplete", self.outputs[0].status)

    def test_multiple_requests_in_one_pump_do_not_invent_intermediate_state(self):
        self.request()
        self.request()
        self.captured = observation(300)
        self.recorder.poll()
        self.assertEqual(2, len(self.outputs))
        self.assertIsNone(self.outputs[0].after_state)
        self.assertIsNone(self.outputs[1].before_state)
        self.assertTrue(all(t.status == "incomplete" for t in self.outputs))

    def test_transient_capture_failure_preserves_actions(self):
        self.request()
        saved, self.captured = self.captured, None
        self.recorder.poll()
        self.assertFalse(self.outputs)
        self.captured = saved
        self.recorder.poll()
        self.assertEqual(1, len(self.outputs[0].actions))

    def test_new_change_after_missing_end_is_not_attributed_to_previous_request(self):
        self.recorder.handle_event(event(member="old"))
        self.recorder.handle_event(event("StartRequest"))
        self.recorder.handle_event(event(member="new"))
        self.assertEqual("old", self.outputs[0].actions[0].commands[0].member_name)
        self.assertEqual("new", self.recorder.actions[0].commands[0].member_name)
        self.assertIsNone(self.journal[-1][1])

    def test_idle_does_not_duplicate_observations(self):
        for _ in range(10):
            self.recorder.poll(force=True)
        self.assertEqual(1, len(self.initial))
        self.assertFalse(self.outputs)

    def test_consumer_failure_does_not_discard_pending_batch(self):
        self.request()
        self.recorder.on_transition = Mock(side_effect=OSError("disk full"))
        with self.assertRaises(OSError):
            self.recorder.poll()
        self.assertEqual(1, len(self.recorder.actions))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = observation()
        self.reader = SapSnapshotReader(SimpleNamespace())
        self.reader.reader = Mock()
        self.reader.reader.read_state.return_value = self.snapshot.state
        self.reader.tree_reader = Mock()
        self.reader.tree_reader.capture.return_value = self.snapshot.state.ui_tree

    def test_stable_screen_is_accepted(self):
        self.assertEqual(self.snapshot.state, self.reader.capture().state)

    def test_busy_or_unknown_busy_never_traverses_components(self):
        for value in (True, None):
            self.reader.reader.read_state.return_value = replace(self.snapshot.state, is_busy=value)
            self.assertIsNone(self.reader.capture())
        self.reader.tree_reader.capture.assert_not_called()

    def test_changed_screen_during_traversal_is_rejected(self):
        self.reader.reader.read_state.side_effect = [self.snapshot.state, observation(200).state]
        with self.assertLogs("sap_explorer.transition_recorder", level="WARNING"):
            self.assertIsNone(self.reader.capture())

    def test_missing_window_is_rejected(self):
        self.reader.reader.read_state.return_value = replace(self.snapshot.state, active_window_id=None)
        self.assertIsNone(self.reader.capture())
        self.reader.tree_reader.capture.assert_not_called()

    def test_com_failure_is_transient(self):
        self.reader.tree_reader.capture.side_effect = OSError("component disappeared")
        with self.assertLogs("sap_explorer.transition_recorder", level="ERROR"):
            self.assertIsNone(self.reader.capture())
