from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sys
import unittest


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sap_explorer.screen_reader import (  # noqa: E402
    SapScreenMonitor,
    SapScreenReader,
    SapScreenState,
    format_screen_state,
)
from sap_explorer.object_tree import SapUiNode, SapUiTree  # noqa: E402


class FakeInfo:
    Transaction = "VA01"
    Program = "SAPMV45A"
    ScreenNumber = 101
    IsLowSpeedConnection = False
    ScriptingModeReadOnly = True
    ScriptingModeRecordingDisabled = False


class FakeWindow:
    Id = "/app/con[0]/ses[0]/wnd[0]"
    Type = "GuiMainWindow"
    Text = "Criar ordem"


class FakeChildren:
    Count = 1


class FakeSession:
    Busy = False
    IsActive = True
    Id = "/app/con[0]/ses[0]"
    Info = FakeInfo()
    ActiveWindow = FakeWindow()
    Children = FakeChildren()


class MissingProperties:
    def __getattr__(self, name: str) -> object:
        raise OSError(f"COM property unavailable: {name}")


class SequenceReader:
    def __init__(self, *results: SapScreenState | Exception) -> None:
        self._results = iter(results)

    def read_state(self) -> SapScreenState:
        result = next(self._results)
        if isinstance(result, Exception):
            raise result
        return result


class SapScreenStateTests(unittest.TestCase):
    def test_compares_states_and_ignores_diagnostic_only_change(self) -> None:
        first = SapScreenState(transaction="VA01", screen_number=101, is_busy=False)
        equal = SapScreenState(transaction="VA01", screen_number=101, is_busy=False)
        busy_changed = replace(first, is_busy=True)
        screen_changed = replace(first, screen_number=4001)

        self.assertEqual(first, equal)
        self.assertNotEqual(first, busy_changed)
        self.assertFalse(busy_changed.has_significant_change(first))
        self.assertTrue(screen_changed.has_significant_change(first))
        self.assertTrue(first.has_significant_change(None))

    def test_serializes_state_to_dict_and_json(self) -> None:
        state = SapScreenState(
            transaction="VA01",
            program="SAPMV45A",
            screen_number=101,
            active_window_text="Criar ordem",
            has_additional_windows=False,
        )

        serialized = state.to_dict()
        json_value = json.loads(state.to_json())

        self.assertEqual("VA01", serialized["transaction"])
        self.assertEqual(101, json_value["screen_number"])
        self.assertEqual("Criar ordem", json_value["active_window_text"])
        self.assertFalse(json_value["has_additional_windows"])

    def test_serializes_embedded_ui_tree(self) -> None:
        tree = SapUiTree(
            roots=[SapUiNode(id="wnd[0]", type="GuiMainWindow")],
            source="get_object_tree",
        )
        state = SapScreenState(transaction="VA01", ui_tree=tree)

        payload = json.loads(state.to_json())

        self.assertEqual("get_object_tree", payload["ui_tree"]["source"])
        self.assertEqual("wnd[0]", payload["ui_tree"]["roots"][0]["id"])

    def test_formats_a_compact_monitor_line(self) -> None:
        state = SapScreenState(
            transaction="VA01",
            program="SAPMV45A",
            screen_number=101,
            active_window_id="/app/con[0]/ses[0]/wnd[1]",
            active_window_text="Informação",
        )

        line = format_screen_state(state, datetime(2026, 9, 4, 10, 21, 3))

        self.assertEqual(
            "10:21:03  VA01 | SAPMV45A | 0101 | wnd[1] | Informação",
            line,
        )


class SapScreenReaderTests(unittest.TestCase):
    def test_reads_current_screen_state(self) -> None:
        state = SapScreenReader(FakeSession()).read_state()

        self.assertEqual("VA01", state.transaction)
        self.assertEqual("SAPMV45A", state.program)
        self.assertEqual(101, state.screen_number)
        self.assertEqual("GuiMainWindow", state.active_window_type)
        self.assertEqual("Criar ordem", state.active_window_text)
        self.assertEqual(1, state.window_count)
        self.assertFalse(state.has_additional_windows)
        self.assertTrue(state.scripting_mode_read_only)

    def test_handles_all_missing_properties(self) -> None:
        with self.assertLogs("sap_explorer.screen_reader", level="WARNING"):
            state = SapScreenReader(MissingProperties()).read_state()

        self.assertIsNone(state.transaction)
        self.assertIsNone(state.active_window_id)
        self.assertIsNone(state.window_count)
        self.assertIsNone(state.has_additional_windows)
        self.assertIsNone(state.is_busy)

    def test_stops_after_busy_check_to_avoid_blocking_calls(self) -> None:
        class BusySession(MissingProperties):
            Busy = True

        state = SapScreenReader(BusySession()).read_state()

        self.assertTrue(state.is_busy)
        self.assertIsNone(state.transaction)


class SapScreenMonitorTests(unittest.TestCase):
    def test_unavailable_state_does_not_replace_last_observation(self) -> None:
        first = SapScreenState(transaction="TEST", screen_number=100)
        monitor = SapScreenMonitor(SequenceReader(first, SapScreenState()))
        self.assertEqual(first, monitor.poll_once())
        self.assertIsNone(monitor.poll_once())
        self.assertEqual(first, monitor.last_state)

    def test_detects_only_significant_changes(self) -> None:
        first = SapScreenState(transaction="VA01", screen_number=101)
        same = replace(first, is_active=True)
        changed = replace(first, screen_number=4001)
        monitor = SapScreenMonitor(
            SequenceReader(first, same, changed),
            interval_seconds=1.0,
            sleep=lambda _: None,
        )

        self.assertEqual(first, monitor.poll_once())
        self.assertIsNone(monitor.poll_once())
        self.assertEqual(changed, monitor.poll_once())

    def test_transient_exception_does_not_erase_last_state(self) -> None:
        first = SapScreenState(transaction="VA01", screen_number=101)
        changed = replace(first, transaction="VA02")
        monitor = SapScreenMonitor(
            SequenceReader(first, OSError("temporary COM failure"), changed),
            interval_seconds=1.0,
            sleep=lambda _: None,
        )

        self.assertEqual(first, monitor.poll_once())
        with self.assertLogs("sap_explorer.screen_reader", level="ERROR"):
            self.assertIsNone(monitor.poll_once())
        self.assertEqual(first, monitor.last_state)
        self.assertEqual(changed, monitor.poll_once())

    def test_busy_cycle_is_ignored(self) -> None:
        busy = SapScreenState(is_busy=True)
        ready = SapScreenState(transaction="VA01", screen_number=101, is_busy=False)
        monitor = SapScreenMonitor(
            SequenceReader(busy, ready),
            interval_seconds=1.0,
            sleep=lambda _: None,
        )

        self.assertIsNone(monitor.poll_once())
        self.assertIsNone(monitor.last_state)
        self.assertEqual(ready, monitor.poll_once())

    def test_ctrl_c_stops_run_cleanly(self) -> None:
        state = SapScreenState(transaction="VA01", screen_number=101)

        def interrupt(_: float) -> None:
            raise KeyboardInterrupt

        monitor = SapScreenMonitor(
            SequenceReader(state),
            interval_seconds=1.0,
            sleep=interrupt,
        )
        observed: list[SapScreenState] = []

        with self.assertLogs("sap_explorer.screen_reader", level="INFO") as logs:
            monitor.run(observed.append)

        self.assertEqual([state], observed)
        self.assertTrue(any("encerrado pelo usuário" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
