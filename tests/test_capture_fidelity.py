"""Regressões de fidelidade, estado de controles, lotes e banco legado."""
from dataclasses import replace
from contextlib import closing
import json
import io
import main
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.test_events import FakeComponent, NOW
from tests.test_transition_recorder import event, observation
from tests.test_object_tree import PreferredSession, FallbackSession, FakeComponent as UiComponent
from sap_explorer.capture_config import CaptureOptions
from sap_explorer.control_state import ControlStateReader
from sap_explorer.events import normalize_command_array, SapSessionEventSink
from sap_explorer.fingerprint import fingerprint_screen
from sap_explorer.object_tree import SapUiNode, SapUiTree, SapObjectTreeReader
from sap_explorer.repository import SapRepository
from sap_explorer.sanitization import SanitizationPolicy
from sap_explorer.transition_recorder import SapTransitionRecorder
from sap_explorer.value_capture import capture_value
from sap_explorer.object_tree import SapTextContext


class ParameterFidelityTests(unittest.TestCase):
    def test_preserves_empty_whitespace_multiline_long_and_scalar_types(self):
        for value in ("", "  A  ", "a\r\nb\tc", "á" * 5000, 0, False, 1.5, None):
            with self.subTest(value_type=type(value).__name__):
                raw, commands = normalize_command_array(("SP", "Text", value))
                self.assertEqual(value, raw[2])
                self.assertEqual(value, commands[0].parameters[0])
                self.assertIs(type(value), type(commands[0].parameters[0]))
                self.assertTrue(commands[0].quality["parameters_preserved"])

    def test_raw_argument_nesting_and_command_order_survive(self):
        source = (("M", "one", (1, "", False)), ("SP", "Text", "x\ny"))
        raw, commands = normalize_command_array(source)
        self.assertEqual([["M", "one", [1, "", False]], ["SP", "Text", "x\ny"]], raw)
        self.assertEqual(["one", "Text"], [c.member_name for c in commands])
        self.assertEqual("array", commands[0].quality["parameter_layout"])

    def test_sensitive_parameters_are_redacted_with_quality(self):
        component = FakeComponent("wnd[0]/usr/txtCPF", "GuiTextField", "CPF")
        raw, commands = normalize_command_array(("SP", "Text", "12345678901"), component=component,
                                                sanitizer=SanitizationPolicy(field_patterns=("CPF",)))
        self.assertNotIn("12345678901", json.dumps(raw))
        self.assertFalse(commands[0].quality["parameters_preserved"])
        self.assertEqual("redacted", commands[0].quality["issues"][0]["status"])

    def test_unavailable_transformed_and_nested_truncated_values_are_explicit(self):
        raw, commands = normalize_command_array(("SP", "Text", "abc"), sanitizer=lambda v, c: v[:1])
        self.assertEqual("transformed", commands[0].quality["issues"][0]["status"])
        raw, commands = normalize_command_array(("SP", "Text", object()))
        self.assertEqual("unsupported_type", commands[0].quality["issues"][0]["status"])
        nested = "a"
        for _ in range(15):
            nested = [nested]
        raw, commands = normalize_command_array(("SP", "Text", nested))
        self.assertEqual("truncated", commands[0].quality["issues"][0]["status"])

    def test_sanitizer_failure_never_leaks_parameter_or_exception_message(self):
        def broken(value, context):
            raise ValueError("secret=" + value)
        raw, commands = normalize_command_array(("SP", "Text", "private"), sanitizer=broken)
        self.assertNotIn("private", json.dumps([raw, commands[0].to_dict()]))
        self.assertEqual("unavailable", commands[0].quality["issues"][0]["status"])

    def test_normalization_failure_is_delivered_as_persistible_gap(self):
        received = []
        sink = SapSessionEventSink()
        sink.configure(received.append)
        with patch("sap_explorer.events.normalize_sap_event", side_effect=ValueError("private")):
            with self.assertLogs("sap_explorer.events", level="ERROR") as log:
                sink.OnChange(None, None, ())
        self.assertEqual("CaptureError", received[0].event_type)
        self.assertNotIn("private", received[0].to_json() + str(log.output))

    def test_nested_case_input_policy_uses_field_names(self):
        value, issues = capture_value({"person": {"CPF": "private"}}, SanitizationPolicy(field_patterns=("CPF",)),
                                      SapTextContext("CaseInput", "inputs", None, "inputs"))
        self.assertEqual("[REDACTED]", value["person"]["CPF"])
        self.assertTrue(issues)


class Collection:
    def __init__(self, values):
        self.values = values
        self.Count = len(values)
    def ElementAt(self, index):
        return self.values[index]


class ControlCaptureTests(unittest.TestCase):
    def enrich(self, kind, control, **limits):
        node = SapUiNode(id="wnd[0]/usr/control", type=kind if kind.startswith("Gui") else "GuiShell",
                         subtype=None if kind.startswith("Gui") else kind)
        tree = SapUiTree([node], "recursive", "wnd[0]")
        reader = ControlStateReader(SimpleNamespace(FindById=lambda _: control),
                                    CaptureOptions(**limits), SanitizationPolicy(), monotonic=lambda: 0)
        reader.enrich(tree)
        return node, tree

    def test_checkbox_radio_combo_and_tab_capture_actual_selection(self):
        for kind in ("GuiCheckBox", "GuiRadioButton"):
            node, _ = self.enrich(kind, SimpleNamespace(Selected=False))
            self.assertIs(False, node.control_state["selected"])
        node, _ = self.enrich("GuiComboBox", SimpleNamespace(Key="02", Entries=Collection([
            SimpleNamespace(Key="01", Value="Um"), SimpleNamespace(Key="02", Value="Dois")])) )
        self.assertEqual("02", node.control_state["key"])
        self.assertEqual("Dois", node.control_state["entries"][1]["text"])
        node, _ = self.enrich("GuiTabStrip", SimpleNamespace(SelectedTab=SimpleNamespace(Id="/app/con[3]/ses[2]/wnd[0]/usr/tabs/tab2")))
        self.assertEqual("wnd[0]/usr/tabs/tab2", node.control_state["selected_tab"])

    def test_full_control_text_preserves_empty_and_multiline(self):
        for value in ("", "a\nb" + "x" * 1000):
            node, _ = self.enrich("GuiTextField", SimpleNamespace(Text=value))
            self.assertEqual(value, node.control_state["text"])

    def test_grid_samples_explicit_rows_and_technical_columns_without_scrolling(self):
        calls = []
        grid = SimpleNamespace(RowCount=10, FirstVisibleRow=3, SelectedRows="4", CurrentCellRow=4,
                               CurrentCellColumn="ID", ColumnOrder=Collection(["ID", "NAME"]),
                               GetCellValue=lambda row, col: calls.append((row, col)) or f"{row}/{col}")
        node, _ = self.enrich("GridView", grid, max_grid_rows=2, max_grid_columns=1)
        self.assertEqual([(3, "ID"), (4, "ID")], calls)
        self.assertEqual("sampled", node.quality["rows"])
        self.assertEqual("sampled", node.quality["columns"])
        self.assertEqual("4", node.control_state["selected_rows"])
        self.assertEqual(3, grid.FirstVisibleRow)

    def test_table_reads_visible_rows_with_absolute_indices_and_selection(self):
        table = SimpleNamespace(RowCount=20, VisibleRowCount=2, VerticalScrollbar=SimpleNamespace(Position=5),
                                Columns=Collection([SimpleNamespace(Name="ID", Title="Registro")]),
                                Rows=Collection([SimpleNamespace(Selected=False), SimpleNamespace(Selected=True)]),
                                GetCell=lambda r, c: SimpleNamespace(Text=f"{r},{c}"))
        node, _ = self.enrich("GuiTableControl", table)
        self.assertEqual([5, 6], [r["index"] for r in node.control_state["rows"]])
        self.assertTrue(node.control_state["rows"][1]["selected"])
        self.assertEqual("ID", node.control_state["columns"][0]["name"])
        self.assertEqual(5, table.VerticalScrollbar.Position)

    def test_tree_supports_native_gui_collection_and_limits_text_reads(self):
        tree = SimpleNamespace(SelectedNode="b", GetAllNodeKeys=lambda: Collection(["a", "b", "c"]),
                               GetNodeTextByKey=lambda key: f"Texto {key}")
        node, _ = self.enrich("Tree", tree, max_entries=2)
        self.assertEqual(["a", "b"], [n["key"] for n in node.control_state["nodes"]])
        self.assertEqual("sampled", node.quality["nodes"])

    def test_missing_properties_remain_unknown(self):
        node, _ = self.enrich("GuiCheckBox", SimpleNamespace())
        self.assertIsNone(node.enabled)
        self.assertIsNone(node.control_state["selected"])
        self.assertEqual("unavailable", node.quality["selected"])

    def test_budget_and_scope_are_explicit(self):
        node, tree = self.enrich("GuiTextField", SimpleNamespace(Text="x"), max_control_reads=1)
        self.assertEqual("omitted_budget", node.quality["text"])
        self.assertTrue(tree.capture_metadata["budget_exhausted"])
        node, _ = self.enrich("GuiTextField", SimpleNamespace(Text="x"), control_scopes=("wnd[1]/usr",))
        self.assertEqual("omitted_scope", node.quality["control_state"])

    def test_time_budget_stops_before_com_call(self):
        calls = []
        clock = iter([0, 2, 2, 2])
        node = SapUiNode(id="wnd[0]/usr/x", type="GuiCheckBox")
        reader = ControlStateReader(SimpleNamespace(FindById=lambda _: calls.append(1)), CaptureOptions(),
                                    SanitizationPolicy(), monotonic=lambda: next(clock))
        reader.enrich(SapUiTree([node], "recursive"))
        self.assertEqual([], calls)
        self.assertEqual("omitted_budget", node.quality["control_state"])

    def test_both_tree_strategies_enrich_controls_and_mark_truncated_preview(self):
        control = UiComponent("wnd[0]", "GuiTextField", text="x" * 500)
        for session in (FallbackSession(control), PreferredSession(control, {"Id": "wnd[0]", "Type": "GuiTextField", "Text": "x" * 500})):
            session.FindById = lambda _: control
            result = SapObjectTreeReader(session).capture().roots[0]
            self.assertEqual("x" * 500, result.control_state["text"])
            self.assertEqual("truncated", result.quality["preview_text"][0]["status"])

    def test_selection_changes_content_but_not_structure(self):
        node, _ = self.enrich("GuiCheckBox", SimpleNamespace(Selected=False))
        state = replace(observation().state, ui_tree=SapUiTree([node], "recursive"))
        before = fingerprint_screen(state)
        node.control_state["selected"] = True
        after = fingerprint_screen(state)
        self.assertEqual(before.structural_hash, after.structural_hash)
        self.assertNotEqual(before.content_hash, after.content_hash)


class BatchAndMigrationTests(unittest.TestCase):
    def test_local_changes_remain_in_interval_before_delayed_commands(self):
        first, local, result = observation(), observation(text="aba2"), observation(200)
        current, output = [first], []
        recorder = SapTransitionRecorder(lambda: current[0], output.append)
        recorder.poll(force=True)
        current[0] = local
        recorder.poll(force=True)
        for kind in ("Change", "StartRequest", "EndRequest"):
            recorder.handle_event(event(kind))
        current[0] = result
        recorder.poll(force=True)
        batch = output[-1].batch
        self.assertEqual([first.id, local.id], batch["observation_interval"])
        self.assertFalse(batch["exact_before_known"])
        self.assertEqual(local.id, output[-1].before_state.id)
        self.assertEqual(3, len(batch["event_ids"]))

    def test_duplicate_start_belongs_to_new_batch(self):
        output = []
        recorder = SapTransitionRecorder(observation, output.append)
        recorder.poll(force=True)
        recorder.handle_event(event("StartRequest"))
        recorder.handle_event(event("StartRequest"))
        recorder.handle_event(event("EndRequest"))
        recorder.poll(force=True)
        self.assertEqual(1, len(output[0].batch["event_ids"]))
        self.assertEqual(2, len(output[1].batch["event_ids"]))
        self.assertNotEqual(output[0].batch["id"], output[1].batch["id"])

    def test_quality_improvement_without_value_change_is_observed(self):
        first, second = observation(), observation()
        first.state.ui_tree.roots[0].quality["text"] = "unavailable"
        second.state.ui_tree.roots[0].quality["text"] = "captured"
        current, output = [first], []
        recorder = SapTransitionRecorder(lambda: current[0], output.append)
        recorder.poll(force=True)
        current[0] = second
        recorder.poll(force=True)
        self.assertEqual(1, len(output))

    def test_legacy_migration_is_additive_backed_up_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, session_json TEXT NOT NULL, capabilities_json TEXT, stop_reason TEXT)")
                connection.execute("INSERT INTO runs VALUES('legacy','2026-01-01',NULL,'{}',NULL,NULL)")
                connection.commit()
            with SapRepository(path) as repo:
                self.assertTrue(repo.migration_backup.is_file())
                self.assertEqual(1, repo.stats()["legacy_runs"])
                self.assertEqual([], repo.export()["timeline"])
                with closing(sqlite3.connect(repo.migration_backup)) as backup:
                    self.assertEqual(0, backup.execute("PRAGMA user_version").fetchone()[0])
                    self.assertEqual("legacy", backup.execute("SELECT id FROM runs").fetchone()[0])
            with SapRepository(path) as repo:
                self.assertIsNone(repo.migration_backup)
                self.assertEqual(1, repo.stats()["runs"])

    def test_cases_timeline_marks_and_batch_survive_reopening(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new.sqlite3"
            with SapRepository(path) as repo:
                case_id = repo.create_case("Caso 1", "titularidade", {"instalacao": "001", "CPF": "private"},
                                           SanitizationPolicy(field_patterns=("CPF",)))
                run = repo.start_run({}, case_id=case_id)
                with self.assertRaises(ValueError):
                    repo.start_run({}, case_id=case_id)
                first, last = observation(), observation(200, popup=True)
                current = [first]
                recorder = SapTransitionRecorder(lambda: current[0], lambda t: repo.save_transition(run, t),
                    on_observation=lambda o: repo.save_observation(run, o),
                    on_event=lambda e, b: repo.save_event(run, e, b))
                recorder.poll(force=True)
                for kind in ("Change", "StartRequest", "EndRequest"):
                    recorder.handle_event(event(kind))
                current[0] = last
                recorder.poll(force=True)
                repo.mark_case(case_id, "Conferência", "Dados conferidos")
                with self.assertRaises(ValueError):
                    repo.finish_case(case_id, "confirmed", "Protocolo conferido")
                repo.finish_run(run, "keyboard_interrupt")
                self.assertEqual("inconclusive", repo.export()["cases"][0]["outcome"])
                repo.finish_case(case_id, "confirmed", "Titular e vigência conferidos")
            with SapRepository(path) as repo:
                data = repo.export()
                self.assertEqual([1, 2, 3, 4, 5, 6], [t["sequence"] for t in data["timeline"]])
                self.assertEqual("confirmed", data["cases"][0]["outcome"])
                self.assertEqual("001", data["cases"][0]["inputs"]["instalacao"])
                self.assertNotIn("private", json.dumps(data))
                self.assertEqual(last.id, data["case_marks"][0]["observation_id"])
                self.assertEqual(first.id, data["observation_metadata"][1]["metadata"]["previous_main_window"]["observation_id"])
                self.assertEqual(1, len(data["request_batches"]))
                self.assertEqual([], repo.connection.execute("PRAGMA foreign_key_check").fetchall())
                with self.assertRaises(ValueError):
                    repo.start_run({}, case_id=case_id)

    def test_capture_options_reject_invalid_limits(self):
        for kwargs in ({"max_grid_rows": 0}, {"enrichment_seconds": float("nan")},
                       {"control_scopes": ("/app/con[0]",)}):
            with self.assertRaises(ValueError):
                CaptureOptions(**kwargs)


class CaseCliTests(unittest.TestCase):
    def test_offline_marks_and_results_use_saved_policy_without_sap(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "cases.sqlite3")
            with SapRepository(db) as repo:
                case_id = repo.create_case("Caso", "titularidade", {},
                                           SanitizationPolicy(text_patterns=("private",)))
            with patch("main.SapConnector", side_effect=AssertionError("must remain offline")):
                self.assertEqual(0, main.run("case-mark", db=db, case_id=case_id, label="Revisão", note="private"))
                self.assertEqual(0, main.run("case-finish", db=db, case_id=case_id, outcome="inconclusive", evidence="private"))
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(0, main.run("cases", db=db))
            self.assertNotIn("private", output.getvalue())
            with SapRepository(db) as repo:
                self.assertEqual("[REDACTED]", repo.export()["case_marks"][0]["note"])

    def test_invalid_case_file_is_rejected_before_sap_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"name":"case", "inputs": []}', encoding="utf-8")
            with patch("main.SapConnector", side_effect=AssertionError("must validate first")):
                with self.assertRaises(ValueError):
                    main.run("explore", case_file=str(path))

    def test_case_confirmation_requires_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            with SapRepository(Path(directory) / "case.sqlite3") as repo:
                case_id = repo.create_case("Caso", "titularidade", {}, SanitizationPolicy())
                with self.assertRaises(ValueError):
                    repo.finish_case(case_id, "confirmed", "")
                self.assertIsNone(repo.export()["cases"][0]["ended_at"])
