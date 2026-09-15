from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from tests.test_events import FakeComponent, NOW
from sap_explorer.events import SapRawEvent, normalize_sap_event
from sap_explorer.object_tree import SapTextContext
from sap_explorer.sanitization import SanitizationPolicy
from sap_explorer.screen_reader import SapScreenReader


class SanitizationTests(unittest.TestCase):
    def test_field_policy_protects_raw_normalized_and_numeric_parameters(self):
        policy = SanitizationPolicy(field_patterns=("CPF",))
        component = FakeComponent("wnd[0]/usr/txtCPF", "GuiTextField", "CPF")
        for parameter in ("12345678901", 12345678901):
            event = normalize_sap_event(SapRawEvent(NOW, "Change", (None, component, ("SP", "Text", parameter))), sanitizer=policy)
            self.assertNotIn("12345678901", event.to_json())
            self.assertIn("[REDACTED]", event.to_json())
            self.assertEqual("Text", event.commands[0].member_name)

    def test_error_payload_and_details_obey_text_policy(self):
        policy = SanitizationPolicy(text_patterns=(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b",))
        event = normalize_sap_event(SapRawEvent(NOW, "Error", (None, 1, "CPF 123.456.789-00")), sanitizer=policy)
        self.assertNotIn("123.456.789-00", event.to_json())
        self.assertEqual(event.details["descriptions"], event.raw_payload["descriptions"])

    def test_status_bar_is_optional_and_its_text_is_sanitized(self):
        bar = SimpleNamespace(Text="Documento secreto", MessageType="E", MessageId="Z1", MessageNumber="001", MessageAsPopup=True)
        session = SimpleNamespace(FindById=lambda *args: bar)
        state = SapScreenReader(session, sanitizer=SanitizationPolicy(field_patterns=(r"sbar",))).read_status_bar()
        self.assertEqual("[REDACTED]", state.text)
        self.assertEqual("001", state.message_number)
        self.assertTrue(state.message_as_popup)
        self.assertIsNone(SapScreenReader(SimpleNamespace()).read_status_bar())

    def test_invalid_config_fails_before_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps({"field_patterns": "CPF"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                SanitizationPolicy.from_file(path)

    def test_strict_policy_redacts_all_text_but_password_rule_always_applies(self):
        context = SapTextContext("Text", "wnd[0]/usr/pwdPASS", "GuiPasswordField", "PASS")
        self.assertEqual("[REDACTED]", SanitizationPolicy()("secret", context))
        context = SapTextContext("Text", "wnd[0]/usr/txtX", "GuiTextField", "X")
        self.assertEqual("[REDACTED]", SanitizationPolicy(redact_all_text=True)("anything", context))
