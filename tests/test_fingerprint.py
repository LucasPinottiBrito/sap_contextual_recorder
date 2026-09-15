from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sap_explorer.fingerprint import (  # noqa: E402
    build_content_payload,
    build_structural_payload,
    fingerprint_screen,
    normalize_component_id,
)
from sap_explorer.object_tree import SapUiNode, SapUiTree  # noqa: E402
from sap_explorer.screen_reader import SapScreenState  # noqa: E402


def make_document_state(document_number: str) -> SapScreenState:
    tree = SapUiTree(
        roots=[
            SapUiNode(
                id="/app/con[0]/ses[0]/wnd[0]",
                type="GuiMainWindow",
                name="wnd[0]",
                text="Exibir documento",
                children=[
                    SapUiNode(
                        id="/app/con[0]/ses[0]/wnd[0]/usr/txtDOCUMENT",
                        type="GuiTextField",
                        name="DOCUMENT",
                        text=document_number,
                        changeable=False,
                        enabled=True,
                        visible=True,
                    )
                ],
            )
        ],
        source="get_object_tree",
    )
    return SapScreenState(
        transaction="VA03",
        program="SAPMV45A",
        screen_number=102,
        active_window_id="/app/con[0]/ses[0]/wnd[0]",
        active_window_type="GuiMainWindow",
        active_window_text=f"Documento {document_number}",
        window_count=1,
        has_additional_windows=False,
        ui_tree=tree,
    )


class ScreenFingerprintCases(unittest.TestCase):
    def test_case_a_same_structure_different_document(self) -> None:
        first = fingerprint_screen(make_document_state("12345"))
        second = fingerprint_screen(make_document_state("87654"))

        self.assertEqual(first.structural_hash, second.structural_hash)
        self.assertNotEqual(first.content_hash, second.content_hash)

    def test_case_b_popup_changes_structural_hash(self) -> None:
        base = make_document_state("12345")
        popup = SapScreenState(
            transaction=base.transaction,
            program=base.program,
            screen_number=base.screen_number,
            active_window_id="/app/con[0]/ses[0]/wnd[1]",
            active_window_type="GuiModalWindow",
            active_window_text="Informação",
            window_count=2,
            has_additional_windows=True,
            ui_tree=SapUiTree(
                roots=[
                    SapUiNode(
                        id="/app/con[0]/ses[0]/wnd[1]",
                        type="GuiModalWindow",
                        name="wnd[1]",
                    )
                ],
                source="get_object_tree",
            ),
        )

        self.assertNotEqual(
            fingerprint_screen(base).structural_hash,
            fingerprint_screen(popup).structural_hash,
        )

    def test_case_c_different_screen_changes_structural_hash(self) -> None:
        first = make_document_state("12345")
        second = SapScreenState(
            transaction="VA02",
            program="SAPMV45A",
            screen_number=4001,
            active_window_id="wnd[0]",
            active_window_type="GuiMainWindow",
            ui_tree=first.ui_tree,
        )

        self.assertNotEqual(
            fingerprint_screen(first).structural_hash,
            fingerprint_screen(second).structural_hash,
        )

    def test_case_d_child_order_does_not_change_hashes(self) -> None:
        first_child = SapUiNode(
            id="wnd[0]/usr/txtA", type="GuiTextField", name="A", text="alpha"
        )
        second_child = SapUiNode(
            id="wnd[0]/usr/txtB", type="GuiTextField", name="B", text="beta"
        )
        first = SapScreenState(
            transaction="SE16",
            program="SAPLSETB",
            screen_number=100,
            active_window_id="wnd[0]",
            active_window_type="GuiMainWindow",
            ui_tree=SapUiTree(
                roots=[
                    SapUiNode(
                        id="wnd[0]",
                        type="GuiMainWindow",
                        children=[first_child, second_child],
                    )
                ],
                source="get_object_tree",
            ),
        )
        second = SapScreenState(
            transaction=first.transaction,
            program=first.program,
            screen_number=first.screen_number,
            active_window_id=first.active_window_id,
            active_window_type=first.active_window_type,
            ui_tree=SapUiTree(
                roots=[
                    SapUiNode(
                        id="wnd[0]",
                        type="GuiMainWindow",
                        children=[second_child, first_child],
                    )
                ],
                source="recursive",
            ),
        )

        first_fingerprint = fingerprint_screen(first)
        second_fingerprint = fingerprint_screen(second)

        self.assertEqual(first_fingerprint, second_fingerprint)


class FingerprintNormalizationTests(unittest.TestCase):
    def test_removes_only_administrative_id_prefix(self) -> None:
        self.assertEqual(
            "wnd[0]/usr/txtFIELD",
            normalize_component_id("/app/con[4]/ses[2]/wnd[0]/usr/txtFIELD"),
        )
        self.assertEqual(
            "wnd[0]/usr/txtFIELD[1,2]",
            normalize_component_id("wnd[0]/usr/txtFIELD[1,2]"),
        )

    def test_capture_source_and_warnings_are_transient(self) -> None:
        first = make_document_state("12345")
        assert first.ui_tree is not None
        second_tree = SapUiTree(
            roots=first.ui_tree.roots,
            source="recursive",
            warnings=["temporary COM failure"],
        )
        second = SapScreenState(
            transaction=first.transaction,
            program=first.program,
            screen_number=first.screen_number,
            active_window_id=first.active_window_id,
            active_window_type=first.active_window_type,
            active_window_text=first.active_window_text,
            window_count=first.window_count,
            has_additional_windows=first.has_additional_windows,
            ui_tree=second_tree,
        )

        self.assertEqual(fingerprint_screen(first), fingerprint_screen(second))

    def test_busy_session_and_session_id_do_not_affect_hashes(self) -> None:
        first = make_document_state("12345")
        second = SapScreenState(
            transaction=first.transaction,
            program=first.program,
            screen_number=first.screen_number,
            active_window_id=first.active_window_id,
            active_window_type=first.active_window_type,
            active_window_text=first.active_window_text,
            window_count=first.window_count,
            has_additional_windows=first.has_additional_windows,
            session_id="/app/con[9]/ses[9]",
            is_busy=True,
            is_active=False,
            ui_tree=first.ui_tree,
        )

        self.assertEqual(fingerprint_screen(first), fingerprint_screen(second))

    def test_structural_payload_excludes_text_and_content_includes_it(self) -> None:
        state = make_document_state("12345")

        structural_json = json.dumps(build_structural_payload(state))
        content_json = json.dumps(build_content_payload(state))

        self.assertNotIn("12345", structural_json)
        self.assertIn("12345", content_json)

    def test_hashes_are_sha256_and_serializable(self) -> None:
        fingerprint = fingerprint_screen(make_document_state("12345"))
        payload = json.loads(fingerprint.to_json())

        self.assertEqual(64, len(fingerprint.structural_hash))
        self.assertEqual(64, len(fingerprint.content_hash))
        self.assertEqual(fingerprint.structural_hash, payload["structural_hash"])


if __name__ == "__main__":
    unittest.main()
