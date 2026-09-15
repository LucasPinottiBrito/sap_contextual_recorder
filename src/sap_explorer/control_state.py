"""Leitura adicional, limitada e sem interação, de controles SAP."""
from dataclasses import asdict
import time

from .capture_config import CaptureOptions
from .object_tree import SapTextContext
from .value_capture import capture_value


def relative_id(value):
    return value[value.index("wnd["):] if value and "wnd[" in value else value


def collection_item(collection, index):
    if isinstance(collection, (list, tuple)):
        return collection[index]
    try:
        return collection.ElementAt(index)
    except (AttributeError, TypeError):
        return collection.Item(index)


def collection_count(collection):
    return len(collection) if isinstance(collection, (list, tuple)) else collection.Count


class ControlStateReader:
    def __init__(self, session, options, sanitizer, *, monotonic=time.monotonic):
        self.session, self.options, self.sanitizer = session, options, sanitizer
        self.monotonic = monotonic

    def enrich(self, tree):
        self.deadline = self.monotonic() + self.options.enrichment_seconds
        self.reads = 0
        pending = list(tree.roots)
        while pending:
            node = pending.pop()
            pending.extend(node.children)
            node.quality.update({key: "captured" if getattr(node, key) is not None else "unavailable"
                                 for key in ("enabled", "visible", "changeable")})
            node.quality["children"] = "truncated" if node.truncated else "captured"
            kind = node.subtype if node.type == "GuiShell" else node.type
            if kind not in {"GuiTextField", "GuiCTextField", "GuiOkCodeField", "GuiPasswordField",
                            "GuiCheckBox", "GuiRadioButton", "GuiComboBox", "GuiTabStrip",
                            "GridView", "Tree", "TextEdit", "HTMLViewer", "GuiTableControl"}:
                continue
            if self.options.control_scopes and not any(
                    relative_id(node.id).startswith(scope) for scope in self.options.control_scopes if node.id):
                node.quality["control_state"] = "omitted_scope"
                continue
            control = self.read(node, "control_lookup", lambda: self.session.FindById(node.id), raw=True)
            if control is None:
                node.quality["control_state"] = node.quality["control_lookup"]
                continue
            if kind in {"GuiTextField", "GuiCTextField", "GuiOkCodeField", "GuiPasswordField", "TextEdit"}:
                self.put(node, "text", lambda: control.Text)
            elif kind in {"GuiCheckBox", "GuiRadioButton"}:
                self.put(node, "selected", lambda: control.Selected)
            elif kind == "GuiComboBox":
                self.put(node, "key", lambda: control.Key)
                count = self.put(node, "entry_count", lambda: control.Entries.Count)
                if type(count) is int and count >= 0:
                    entries = []
                    for i in range(min(count, self.options.max_entries)):
                        entries.append(self.read(node, f"entries[{i}]", lambda i=i: {
                            "key": collection_item(control.Entries, i).Key, "text": collection_item(control.Entries, i).Value}))
                    node.control_state["entries"] = entries
                    node.quality["entries"] = "sampled" if count > len(entries) else "captured"
            elif kind == "GuiTabStrip":
                self.put(node, "selected_tab", lambda: relative_id(control.SelectedTab.Id))
            elif kind == "GridView":
                self.grid(node, control)
            elif kind == "GuiTableControl":
                self.table(node, control)
            elif kind == "Tree":
                self.tree(node, control)
            else:
                # A GuiShell identity does not imply access to the HTML DOM.
                node.quality["html_content"] = "unsupported"
            statuses = [value for key, value in node.quality.items()
                        if key not in {"enabled", "visible", "changeable", "children"}
                        and not key.startswith("preview_")]
            node.quality["control_state"] = (
                "partial" if any(not isinstance(value, str) or value not in {"captured", "sampled"}
                                 for value in statuses) else
                "sampled" if "sampled" in statuses else "captured")
        tree.capture_metadata = {"capture_version": 2, "options": asdict(self.options),
                                 "control_reads": self.reads,
                                 "budget_exhausted": not self.available(),
                                 "scope": "active_window",
                                 "deadline_kind": "cooperative_between_calls"}
        return tree

    def available(self):
        return self.reads < self.options.max_control_reads and self.monotonic() < self.deadline

    def read(self, node, key, call, *, raw=False):
        if not self.available():
            node.quality[key] = "omitted_budget"
            return None
        self.reads += 1
        try:
            value = call()
        except Exception:
            node.quality[key] = "unavailable"
            return None
        if raw:
            node.quality[key] = "captured" if value is not None else "unavailable"
            return value
        value, issues = capture_value(value, self.sanitizer,
                                     SapTextContext("ControlValue", node.id, node.type, node.name))
        node.quality[key] = issues if issues else ("unavailable" if value is None else "captured")
        return value

    def put(self, node, key, call):
        value = self.read(node, key, call)
        node.control_state[key] = value
        return value

    def grid(self, node, control):
        count = self.put(node, "row_count", lambda: control.RowCount)
        first = self.put(node, "first_visible_row", lambda: control.FirstVisibleRow)
        self.put(node, "selected_rows", lambda: control.SelectedRows)
        self.put(node, "current_row", lambda: control.CurrentCellRow)
        self.put(node, "current_column", lambda: control.CurrentCellColumn)
        columns = self.read(node, "columns", lambda: control.ColumnOrder, raw=True)
        if columns is None:
            return
        total = self.read(node, "column_count", lambda: collection_count(columns))
        if type(total) is not int or total < 0:
            return
        names = []
        for i in range(min(total, self.options.max_grid_columns)):
            name = self.read(node, f"column[{i}]", lambda i=i: collection_item(columns, i), raw=True)
            if isinstance(name, str):
                names.append(name)
        node.control_state["columns"] = names
        node.quality["columns"] = "sampled" if len(names) < total else "captured"
        if type(count) is not int or type(first) is not int or count < 0 or first < 0:
            node.quality["rows"] = "unavailable"
            return
        rows = []
        for row in range(first, min(count, first + self.options.max_grid_rows)):
            if not self.available():
                break
            cells = {col: self.read(node, f"cells[{row}].{col}", lambda col=col, row=row: control.GetCellValue(row, col))
                     for col in names}
            rows.append({"index": row, "cells": cells})
        node.control_state["rows"] = rows
        node.quality["rows"] = "sampled" if first != 0 or len(rows) < count else "captured"

    def table(self, node, control):
        count = self.put(node, "row_count", lambda: control.RowCount)
        visible = self.put(node, "visible_row_count", lambda: control.VisibleRowCount)
        first = self.put(node, "first_visible_row", lambda: control.VerticalScrollbar.Position)
        columns = self.put(node, "column_count", lambda: control.Columns.Count)
        if not all(type(v) is int and v >= 0 for v in (count, visible, first, columns)):
            node.quality["rows"] = "unavailable"
            return
        node.control_state["columns"] = [self.read(node, f"column[{i}]", lambda i=i: {
            "index": i, "name": collection_item(control.Columns, i).Name,
            "title": collection_item(control.Columns, i).Title})
            for i in range(min(columns, self.options.max_grid_columns))]
        node.quality["columns"] = "sampled" if columns > self.options.max_grid_columns else "captured"
        rows = []
        for row in range(min(visible, self.options.max_grid_rows, max(0, count - first))):
            if not self.available():
                break
            cells = {str(col): self.read(node, f"cells[{first + row}].{col}",
                                        lambda row=row, col=col: control.GetCell(row, col).Text)
                     for col in range(min(columns, self.options.max_grid_columns))}
            selected = self.read(node, f"row_selected[{first + row}]", lambda row=row: collection_item(control.Rows, row).Selected)
            rows.append({"index": first + row, "cells": cells, "selected": selected})
        node.control_state["rows"] = rows
        node.quality["rows"] = "sampled" if first or len(rows) < count or columns > self.options.max_grid_columns else "captured"

    def tree(self, node, control):
        self.put(node, "selected_node", lambda: control.SelectedNode)
        # GetAllNodeKeys has no bounded variant; returned keys are limited before
        # text reads, and the cooperative budget is checked between COM calls.
        keys = self.read(node, "node_keys", lambda: control.GetAllNodeKeys(), raw=True)
        total = self.read(node, "node_count", lambda: collection_count(keys))
        if type(total) is not int or total < 0:
            node.quality["nodes"] = "unavailable"
            return
        nodes = []
        for i in range(min(total, self.options.max_entries)):
            if not self.available():
                break
            key = self.read(node, f"node_key[{i}]", lambda i=i: collection_item(keys, i), raw=True)
            if not isinstance(key, str):
                continue
            safe_key = self.read(node, "node_key", lambda key=key: key)
            text = self.read(node, f"node_text[{len(nodes)}]", lambda key=key: control.GetNodeTextByKey(key))
            nodes.append({"key": safe_key, "text": text})
        node.control_state["nodes"] = nodes
        node.quality["nodes"] = "sampled" if len(nodes) < total else "captured"
