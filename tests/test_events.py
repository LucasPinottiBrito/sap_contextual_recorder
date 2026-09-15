from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sap_explorer.events import (  # noqa: E402
    SapRawEvent,
    SapSessionEventSink,
    normalize_command_array,
    normalize_sap_event,
)


NOW = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)


class FakeComponent:
    def __init__(
        self,
        component_id: str,
        component_type: str,
        name: str = "",
    ) -> None:
        self.Id = component_id
        self.Type = component_type
        self.Name = name


class BrokenComponent:
    @property
    def Id(self) -> str:
        raise OSError("component destroyed")

    @property
    def Type(self) -> str:
        raise OSError("component destroyed")

    @property
    def Name(self) -> str:
        raise OSError("component destroyed")


class EventNormalizationTests(unittest.TestCase):
    def test_preserves_raw_command_array_and_normalizes_multiple_lines(self) -> None:
        component = FakeComponent("wnd[0]/usr/txtDOC", "GuiTextField", "DOC")
        raw = (
            ("SP", "Text", "Documento 12345"),
            ("M", "SetFocus", ()),
        )

        raw_copy, commands = normalize_command_array(raw, component=component)

        self.assertEqual(
            [["SP", "Text", "Documento 12345"], ["M", "SetFocus", []]],
            raw_copy,
        )
        self.assertEqual("SP", commands[0].command_type)
        self.assertEqual("Text", commands[0].member_name)
        self.assertEqual(("Documento 12345",), commands[0].parameters)
        self.assertEqual((), commands[1].parameters)

    def test_normalizes_flat_single_command(self) -> None:
        raw_copy, commands = normalize_command_array(("M", "Resize", (96, 32, False)))

        self.assertEqual(["M", "Resize", [96, 32, False]], raw_copy)
        self.assertEqual((96, 32, False), commands[0].parameters)

    def test_redacts_password_parameters_in_raw_and_normalized_forms(self) -> None:
        password = FakeComponent("wnd[0]/usr/pwdPASS", "GuiPasswordField", "PASSWORD")

        raw_copy, commands = normalize_command_array(
            ("SP", "Text", "top-secret"), component=password
        )

        self.assertEqual(["SP", "Text", "[REDACTED]"], raw_copy)
        self.assertEqual(("[REDACTED]",), commands[0].parameters)

    def test_missing_component_properties_do_not_break_normalization(self) -> None:
        event = normalize_sap_event(
            SapRawEvent(NOW, "FocusChanged", (FakeComponent("ses[0]", "GuiSession"), BrokenComponent()))
        )

        self.assertIsNone(event.component_id)
        self.assertEqual({}, event.raw_payload.get("component", {}))

    def test_change_event_is_serializable_without_live_com_objects(self) -> None:
        session = FakeComponent("/app/con[0]/ses[0]", "GuiSession")
        component = FakeComponent("wnd[0]/usr/ctxtMATNR", "GuiCTextField", "MATNR")
        event = normalize_sap_event(
            SapRawEvent(NOW, "Change", (session, component, (("SP", "Text", "ABC"),)))
        )

        payload = json.loads(event.to_json())

        self.assertEqual("Change", payload["event_type"])
        self.assertEqual("wnd[0]/usr/ctxtMATNR", payload["component_id"])
        self.assertEqual("ABC", payload["commands"][0]["parameters"][0])
        self.assertEqual(
            "/app/con[0]/ses[0]", payload["raw_payload"]["session"]["id"]
        )

    def test_error_event_handles_missing_fields(self) -> None:
        event = normalize_sap_event(SapRawEvent(NOW, "Error", (None, 622, "wrapper")))

        self.assertEqual(622, event.details["error_id"])
        self.assertEqual(["wrapper"], event.details["descriptions"])


class EventSinkTests(unittest.TestCase):
    def test_simulated_callbacks_are_normalized(self) -> None:
        received = []
        sink = SapSessionEventSink()
        sink.configure(received.append, clock=lambda: NOW)
        session = FakeComponent("ses[0]", "GuiSession")
        control = FakeComponent("wnd[0]/usr/txtA", "GuiTextField", "A")

        sink.OnStartRequest(session)
        sink.OnFocusChanged(session, control)
        sink.OnChange(session, control, ("M", "SetFocus", ()))

        self.assertEqual(
            ["StartRequest", "FocusChanged", "Change"],
            [event.event_type for event in received],
        )
        self.assertEqual("wnd[0]/usr/txtA", received[1].component_id)

    def test_consumer_exception_does_not_escape_com_callback(self) -> None:
        sink = SapSessionEventSink()
        sink.configure(lambda _: (_ for _ in ()).throw(RuntimeError("boom")))

        with self.assertLogs("sap_explorer.events", level="ERROR"):
            sink.OnProgressIndicator(50, "Carregando")


if __name__ == "__main__":
    unittest.main()
