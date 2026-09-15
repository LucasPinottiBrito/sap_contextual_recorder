from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sap_explorer.object_tree import (  # noqa: E402
    CORE_OBJECT_TREE_PROPERTIES,
    OBJECT_TREE_PROPERTIES,
    SapObjectTreeReader,
    SapTextContext,
    SapUiNode,
    SapUiTree,
    default_text_sanitizer,
    normalize_object_tree_payload,
    render_tree_summary,
)


class FakeCollection:
    def __init__(self, *items: object, broken_indexes: set[int] | None = None) -> None:
        self._items = items
        self._broken_indexes = broken_indexes or set()

    @property
    def Count(self) -> int:
        return len(self._items)

    def Item(self, index: int) -> object:
        if index in self._broken_indexes:
            raise OSError(f"broken child {index}")
        return self._items[index]


class FakeComponent:
    def __init__(
        self,
        component_id: str,
        component_type: str,
        *,
        name: str = "",
        text: str = "",
        children: FakeCollection | None = None,
    ) -> None:
        self.Id = component_id
        self.Type = component_type
        self.Name = name
        self.Text = text
        self.Tooltip = ""
        self.Changeable = False
        self.Enabled = True
        self.Visible = True
        self.Children = children or FakeCollection()


class FallbackSession:
    def __init__(self, active_window: FakeComponent) -> None:
        self.ActiveWindow = active_window


class MinimalComponent:
    Id = "wnd[0]/usr/minimal"
    Children = FakeCollection()


class PreferredSession(FallbackSession):
    def __init__(self, active_window: FakeComponent, payload: object) -> None:
        super().__init__(active_window)
        self._payload = payload
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def GetObjectTree(self, root_id: str, properties: tuple[str, ...]) -> str:
        self.calls.append((root_id, properties))
        return json.dumps(self._payload)


class BrokenPreferredSession(FallbackSession):
    def GetObjectTree(self, _: str, __: tuple[str, ...]) -> str:
        raise OSError("method unavailable in this patch level")


class CoreOnlyPreferredSession(PreferredSession):
    def GetObjectTree(self, root_id: str, properties: tuple[str, ...]) -> str:
        self.calls.append((root_id, properties))
        if properties == OBJECT_TREE_PROPERTIES:
            raise OSError("Visible is not supported")
        return json.dumps(self._payload)


class ObjectTreeNormalizationTests(unittest.TestCase):
    def test_normalizes_nested_tree_and_properties_wrapper(self) -> None:
        payload = {
            "Transaction": "VA01",
            "Tree": {
                "Properties": {
                    "Id": "wnd[0]",
                    "Type": "GuiMainWindow",
                    "Name": "wnd[0]",
                    "Text": "Criar ordem",
                    "Visible": True,
                },
                "Children": [
                    {
                        "Id": "wnd[0]/usr/txtFIELD",
                        "Type": "GuiTextField",
                        "Name": "FIELD",
                        "Text": "valor",
                        "Changeable": 1,
                    }
                ],
            },
        }

        roots = normalize_object_tree_payload(payload)

        self.assertEqual(1, len(roots))
        self.assertEqual("GuiMainWindow", roots[0].type)
        self.assertTrue(roots[0].visible)
        self.assertEqual(1, len(roots[0].children))
        self.assertEqual("valor", roots[0].children[0].text)
        self.assertTrue(roots[0].children[0].changeable)

    def test_normalizes_components_without_expected_properties(self) -> None:
        roots = normalize_object_tree_payload(
            {"Id": "wnd[0]/usr/unknown", "Children": []}
        )

        self.assertEqual(1, len(roots))
        self.assertEqual("wnd[0]/usr/unknown", roots[0].id)
        self.assertIsNone(roots[0].type)
        self.assertIsNone(roots[0].text)

    def test_normalizes_object_wrapper_with_single_child_mapping(self) -> None:
        roots = normalize_object_tree_payload(
            {
                "Object": {"Id": "wnd[0]", "Type": "GuiMainWindow"},
                "Children": {
                    "Component": {
                        "Id": "wnd[0]/usr",
                        "Type": "GuiUserArea",
                    }
                },
            }
        )

        self.assertEqual("wnd[0]", roots[0].id)
        self.assertEqual(1, len(roots[0].children))
        self.assertEqual("wnd[0]/usr", roots[0].children[0].id)

    def test_custom_sanitizer_receives_context(self) -> None:
        contexts: list[SapTextContext] = []

        def sanitizer(value: str, context: SapTextContext) -> str:
            contexts.append(context)
            return value.upper()

        roots = normalize_object_tree_payload(
            {
                "Id": "wnd[0]/usr/txtFIELD",
                "Type": "GuiTextField",
                "Name": "FIELD",
                "Text": "conteúdo",
            },
            sanitizer=sanitizer,
        )

        self.assertEqual("CONTEÚDO", roots[0].text)
        self.assertEqual("Text", contexts[0].property_name)
        self.assertEqual("FIELD", contexts[0].node_name)

    def test_default_sanitizer_redacts_password_like_fields(self) -> None:
        roots = normalize_object_tree_payload(
            {
                "Id": "wnd[0]/usr/pwdRSYST-BCODE",
                "Type": "GuiPasswordField",
                "Name": "PASSWORD",
                "Text": "unexpected-secret",
                "Tooltip": "senha atual",
            }
        )

        self.assertEqual("[REDACTED]", roots[0].text)
        self.assertEqual("[REDACTED]", roots[0].tooltip)


class ObjectTreeReaderTests(unittest.TestCase):
    def test_prefers_get_object_tree_and_limits_scope_to_active_window(self) -> None:
        window = FakeComponent("/app/con[0]/ses[0]/wnd[1]", "GuiModalWindow")
        session = PreferredSession(
            window,
            {"Id": "wnd[1]", "Type": "GuiModalWindow", "Text": "Informação"},
        )

        tree = SapObjectTreeReader(session).capture()

        self.assertEqual("get_object_tree", tree.source)
        self.assertEqual(1, tree.component_count)
        self.assertEqual("wnd[1]", session.calls[0][0])
        self.assertEqual(OBJECT_TREE_PROPERTIES, session.calls[0][1])

    def test_fallback_builds_nested_tree(self) -> None:
        field = FakeComponent(
            "wnd[0]/usr/txtFIELD", "GuiTextField", name="FIELD", text="abc"
        )
        user_area = FakeComponent(
            "wnd[0]/usr",
            "GuiUserArea",
            children=FakeCollection(field),
        )
        window = FakeComponent(
            "/app/con[0]/ses[0]/wnd[0]",
            "GuiMainWindow",
            children=FakeCollection(user_area),
        )

        tree = SapObjectTreeReader(FallbackSession(window)).capture()

        self.assertEqual("recursive", tree.source)
        self.assertEqual(3, tree.component_count)
        self.assertEqual("abc", tree.roots[0].children[0].children[0].text)

    def test_fallback_continues_after_child_error(self) -> None:
        good = FakeComponent("wnd[0]/usr/lblGOOD", "GuiLabel", text="OK")
        window = FakeComponent(
            "/app/con[0]/ses[0]/wnd[0]",
            "GuiMainWindow",
            children=FakeCollection(object(), good, broken_indexes={0}),
        )

        tree = SapObjectTreeReader(FallbackSession(window)).capture()

        self.assertEqual(2, tree.component_count)
        self.assertEqual("OK", tree.roots[0].children[0].text)
        self.assertTrue(any("Children[0]" in warning for warning in tree.warnings))

    def test_fallback_accepts_component_with_missing_optional_properties(self) -> None:
        tree = SapObjectTreeReader(FallbackSession(MinimalComponent())).capture()

        self.assertEqual(1, tree.component_count)
        self.assertEqual("wnd[0]/usr/minimal", tree.roots[0].id)
        self.assertIsNone(tree.roots[0].type)
        self.assertIsNone(tree.roots[0].text)

    def test_broken_preferred_strategy_uses_fallback(self) -> None:
        window = FakeComponent("wnd[0]", "GuiMainWindow")

        tree = SapObjectTreeReader(BrokenPreferredSession(window)).capture()

        self.assertEqual("recursive", tree.source)

    def test_preferred_strategy_retries_with_core_properties(self) -> None:
        window = FakeComponent("wnd[0]", "GuiMainWindow")
        session = CoreOnlyPreferredSession(
            window,
            {"Id": "wnd[0]", "Type": "GuiMainWindow"},
        )

        tree = SapObjectTreeReader(session).capture()

        self.assertEqual("get_object_tree", tree.source)
        self.assertEqual(OBJECT_TREE_PROPERTIES, session.calls[0][1])
        self.assertEqual(CORE_OBJECT_TREE_PROPERTIES, session.calls[1][1])
        self.assertEqual(1, len(tree.warnings))

    def test_depth_limit_marks_node_as_truncated(self) -> None:
        leaf = FakeComponent("wnd[0]/usr/sub/leaf", "GuiLabel")
        child = FakeComponent(
            "wnd[0]/usr/sub", "GuiSubscreen", children=FakeCollection(leaf)
        )
        root = FakeComponent(
            "wnd[0]/usr", "GuiUserArea", children=FakeCollection(child)
        )

        tree = SapObjectTreeReader(
            FallbackSession(root), max_depth=1
        ).capture()

        self.assertEqual(2, tree.component_count)
        self.assertTrue(tree.roots[0].children[0].truncated)

    def test_fallback_ignores_cycles(self) -> None:
        root = FakeComponent("wnd[0]", "GuiMainWindow")
        root.Children = FakeCollection(root)

        tree = SapObjectTreeReader(FallbackSession(root)).capture()

        self.assertEqual(1, tree.component_count)
        self.assertTrue(any("cíclica" in warning for warning in tree.warnings))

    def test_tree_serialization_and_summary(self) -> None:
        tree = SapUiTree(
            roots=[
                SapUiNode(
                    id="wnd[0]",
                    type="GuiMainWindow",
                    children=[SapUiNode(id="wnd[0]/usr", type="GuiUserArea")],
                )
            ],
            source="get_object_tree",
            scope_id="/app/con[0]/ses[0]/wnd[0]",
        )

        payload = json.loads(tree.to_json())
        summary = render_tree_summary(tree)

        self.assertEqual("get_object_tree", payload["source"])
        self.assertEqual("GuiUserArea", payload["roots"][0]["children"][0]["type"])
        self.assertEqual(2, tree.component_count)
        self.assertTrue(any("GuiMainWindow" in line for line in summary))


class SanitizerUnitTests(unittest.TestCase):
    def test_sanitizer_removes_line_breaks_and_limits_length(self) -> None:
        context = SapTextContext("Text", "wnd[0]/usr/lbl", "GuiLabel", "LABEL")

        sanitized = default_text_sanitizer("a\nb\t" + "x" * 300, context)

        self.assertIsNotNone(sanitized)
        assert sanitized is not None
        self.assertNotIn("\n", sanitized)
        self.assertLessEqual(len(sanitized), 200)


if __name__ == "__main__":
    unittest.main()
