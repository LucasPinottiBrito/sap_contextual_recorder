"""Captura e normalização somente leitura da árvore de objetos do SAP GUI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import logging
import re
from typing import Any, Callable, Literal, Mapping, TypeAlias


logger = logging.getLogger(__name__)

ComObject: TypeAlias = Any
TreeSource: TypeAlias = Literal["get_object_tree", "recursive"]

# Conjunto deliberadamente pequeno. SubType é importante para diferenciar
# controles que aparecem genericamente como GuiShell.
OBJECT_TREE_PROPERTIES: tuple[str, ...] = (
    "Id",
    "Type",
    "SubType",
    "Name",
    "Text",
    "Tooltip",
    "Changeable",
    "Enabled",
    "Visible",
)

CORE_OBJECT_TREE_PROPERTIES: tuple[str, ...] = (
    "Id",
    "Type",
    "Name",
    "Text",
    "Tooltip",
    "Changeable",
)


@dataclass(frozen=True, slots=True)
class SapTextContext:
    """Contexto entregue à função configurável de sanitização."""

    property_name: str
    node_id: str | None
    node_type: str | None
    node_name: str | None


TextSanitizer: TypeAlias = Callable[[str, SapTextContext], str | None]


@dataclass(slots=True)
class SapUiNode:
    """Nó normalizado e independente da estratégia usada na captura."""

    id: str | None = None
    type: str | None = None
    subtype: str | None = None
    name: str | None = None
    text: str | None = None
    tooltip: str | None = None
    changeable: bool | None = None
    enabled: bool | None = None
    visible: bool | None = None
    children: list[SapUiNode] = field(default_factory=list)
    truncated: bool = False
    control_state: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SapUiTree:
    """Resultado normalizado de uma captura estrutural."""

    roots: list[SapUiNode]
    source: TreeSource
    scope_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    capture_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def component_count(self) -> int:
        """Conta os nós normalizados sem depender de metadados da origem."""

        count = 0
        pending = list(self.roots)
        while pending:
            node = pending.pop()
            count += 1
            pending.extend(node.children)
        return count

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def default_text_sanitizer(value: str, context: SapTextContext) -> str | None:
    """Redige possíveis segredos e limita textos para saída segura.

    A API documenta que ``GuiPasswordField.Text`` é ilegível e normalmente
    vazio. A verificação adicional protege contra clientes ou objetos atípicos.
    """

    if _is_sensitive_context(context):
        return "[REDACTED]" if value else None

    # Valores destinados a comparação/reprodução não são resumos de interface.
    if context.property_name in {"CommandParameter", "ControlValue", "CaseInput"}:
        return value

    # Impede que textos de controle gerem linhas extras ou dumps muito grandes.
    normalized = re.sub(r"[\r\n\t]+", " ", value).strip()
    if not normalized:
        return None
    return normalized if len(normalized) <= 200 else f"{normalized[:197]}..."


def _is_sensitive_context(context: SapTextContext) -> bool:
    node_type = (context.node_type or "").casefold()
    node_id = (context.node_id or "").casefold()
    node_name = (context.node_name or "").casefold()

    if node_type == "guipasswordfield":
        return True
    if any(part.startswith("pwd") for part in node_id.split("/")):
        return True

    sensitive_names = (
        "password",
        "passwd",
        "passcode",
        "senha",
        "secret",
        "token",
    )
    return any(marker in node_name for marker in sensitive_names)


class SapObjectTreeReader:
    """Captura a árvore usando GetObjectTree e, se necessário, recursão COM."""

    def __init__(
        self,
        session: ComObject,
        *,
        sanitizer: TextSanitizer = default_text_sanitizer,
        max_depth: int = 24,
        max_nodes: int = 2_000,
        options=None,
    ) -> None:
        from .capture_config import CaptureOptions
        self._options = options or CaptureOptions(max_depth=max(1, max_depth), max_nodes=max_nodes)
        if options is not None:
            max_depth, max_nodes = options.max_depth, options.max_nodes
        if max_depth < 0:
            raise ValueError("max_depth deve ser não negativo.")
        if max_nodes <= 0:
            raise ValueError("max_nodes deve ser positivo.")
        self._session = session
        self._sanitizer = sanitizer
        self._max_depth = max_depth
        self._max_nodes = max_nodes

    def capture(self) -> SapUiTree:
        """Captura a janela ativa sem executar interação com a tela."""

        active_window = self._safe_get(
            self._session, "ActiveWindow", "session.ActiveWindow", []
        )
        scope_id = self._safe_text_value(
            self._safe_get(active_window, "Id", "session.ActiveWindow.Id", []),
        )

        preferred = self._try_get_object_tree(scope_id)
        if preferred is not None:
            return self._enrich(preferred)

        logger.info("GetObjectTree indisponível; usando traversal recursivo.")
        root_object = active_window if active_window is not None else self._session
        return self._enrich(self._capture_recursively(root_object, scope_id))

    def _enrich(self, tree):
        from .control_state import ControlStateReader
        return ControlStateReader(self._session, self._options, self._sanitizer).enrich(tree)

    def _try_get_object_tree(self, scope_id: str | None) -> SapUiTree | None:
        try:
            method = getattr(self._session, "GetObjectTree")
        except Exception as exc:
            logger.info("GetObjectTree não está disponível nesta sessão: %s", exc)
            return None

        if not callable(method):
            logger.info("GetObjectTree não é chamável nesta sessão.")
            return None

        warnings: list[str] = []
        request_id = _relative_window_id(scope_id)
        try:
            raw_json = method(request_id, OBJECT_TREE_PROPERTIES)
        except Exception as exc:
            logger.info(
                "GetObjectTree rejeitou alguma propriedade opcional; "
                "tentando o conjunto básico: %s",
                exc,
            )
            try:
                raw_json = method(request_id, CORE_OBJECT_TREE_PROPERTIES)
            except Exception as core_exc:
                logger.warning(
                    "GetObjectTree falhou; será usado o fallback: %s", core_exc
                )
                return None
            warnings.append(
                "GetObjectTree aceitou somente o conjunto básico de propriedades."
            )

        try:
            payload = json.loads(str(raw_json))
            roots = normalize_object_tree_payload(
                payload,
                sanitizer=self._sanitizer,
                max_depth=self._max_depth,
                max_nodes=self._max_nodes,
                warnings=warnings,
            )
        except Exception as exc:
            logger.warning(
                "GetObjectTree retornou JSON inválido; será usado o fallback: %s",
                exc,
            )
            return None

        if not roots:
            logger.warning(
                "GetObjectTree não retornou componentes; será usado o fallback."
            )
            return None

        return SapUiTree(
            roots=roots,
            source="get_object_tree",
            scope_id=scope_id,
            warnings=warnings,
        )

    def _capture_recursively(
        self, root_object: ComObject, scope_id: str | None
    ) -> SapUiTree:
        warnings: list[str] = []
        seen: set[tuple[str, str | int]] = set()
        node_counter = [0]
        root = self._walk_com_object(
            root_object,
            depth=0,
            seen=seen,
            node_counter=node_counter,
            warnings=warnings,
        )
        return SapUiTree(
            roots=[] if root is None else [root],
            source="recursive",
            scope_id=scope_id,
            warnings=warnings,
        )

    def _walk_com_object(
        self,
        component: ComObject,
        *,
        depth: int,
        seen: set[tuple[str, str | int]],
        node_counter: list[int],
        warnings: list[str],
    ) -> SapUiNode | None:
        if node_counter[0] >= self._max_nodes:
            self._warn_once(
                warnings,
                f"Limite de {self._max_nodes} componentes atingido.",
            )
            return None

        component_id = self._read_text(component, "Id", "component.Id", warnings)
        identity: tuple[str, str | int] = (
            ("id", component_id) if component_id else ("object", id(component))
        )
        if identity in seen:
            self._warn_once(
                warnings,
                f"Referência cíclica ignorada: {component_id or '<sem Id>'}.",
            )
            return None
        seen.add(identity)
        node_counter[0] += 1

        node_type = self._read_text(component, "Type", "component.Type", warnings)
        node_name = self._read_text(component, "Name", "component.Name", warnings)
        context_values = {
            "node_id": component_id,
            "node_type": node_type,
            "node_name": node_name,
        }
        value_quality = {}
        node = SapUiNode(
            quality=value_quality,
            id=component_id,
            type=node_type,
            subtype=self._read_text(
                component, "SubType", "component.SubType", warnings
            ),
            name=node_name,
            text=self._read_sanitized_text(
                component,
                "Text",
                "component.Text",
                warnings,
                SapTextContext(property_name="Text", **context_values),
                quality=value_quality,
            ),
            tooltip=self._read_sanitized_text(
                component,
                "Tooltip",
                "component.Tooltip",
                warnings,
                SapTextContext(property_name="Tooltip", **context_values),
                quality=value_quality,
            ),
            changeable=self._read_bool(
                component, "Changeable", "component.Changeable", warnings
            ),
            enabled=self._read_bool(
                component, "Enabled", "component.Enabled", warnings
            ),
            visible=self._read_bool(
                component, "Visible", "component.Visible", warnings
            ),
        )

        children = self._safe_get(component, "Children", "component.Children", warnings)
        if children is None:
            return node
        child_count = self._read_collection_count(children, warnings)
        if child_count is None or child_count == 0:
            return node
        if depth >= self._max_depth:
            node.truncated = True
            self._warn_once(
                warnings,
                f"Limite de profundidade {self._max_depth} atingido em "
                f"{component_id or '<sem Id>'}.",
            )
            return node

        for index in range(child_count):
            if node_counter[0] >= self._max_nodes:
                node.truncated = True
                self._warn_once(
                    warnings,
                    f"Limite de {self._max_nodes} componentes atingido.",
                )
                break
            child = self._read_collection_item(children, index, warnings)
            if child is None:
                continue
            child_node = self._walk_com_object(
                child,
                depth=depth + 1,
                seen=seen,
                node_counter=node_counter,
                warnings=warnings,
            )
            if child_node is not None:
                node.children.append(child_node)
        return node

    def _read_text(
        self,
        obj: ComObject,
        attribute: str,
        label: str,
        warnings: list[str],
    ) -> str | None:
        return self._safe_text_value(
            self._safe_get(
                obj,
                attribute,
                label,
                warnings,
                record_warning=False,
            )
        )

    def _read_sanitized_text(
        self,
        obj: ComObject,
        attribute: str,
        label: str,
        warnings: list[str],
        context: SapTextContext,
        quality=None,
    ) -> str | None:
        value = self._safe_get(obj, attribute, label, warnings, record_warning=False)
        return _sanitize_payload_text(value, self._sanitizer, context, warnings, quality=quality)

    def _read_bool(
        self,
        obj: ComObject,
        attribute: str,
        label: str,
        warnings: list[str],
    ) -> bool | None:
        value = self._safe_get(
            obj,
            attribute,
            label,
            warnings,
            record_warning=False,
        )
        return _to_optional_bool(value)

    @staticmethod
    def _safe_get(
        obj: ComObject | None,
        attribute: str,
        label: str,
        warnings: list[str],
        *,
        record_warning: bool = True,
    ) -> ComObject | None:
        if obj is None:
            return None
        try:
            return getattr(obj, attribute)
        except Exception as exc:
            message = f"Não foi possível ler {label}: {exc}"
            if record_warning:
                warnings.append(message)
            logger.debug("%s", message)
            return None

    @staticmethod
    def _safe_text_value(value: object | None) -> str | None:
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception:
            return None
        return text or None

    @staticmethod
    def _read_collection_count(
        collection: ComObject, warnings: list[str]
    ) -> int | None:
        try:
            count = int(collection.Count)
            return count if count >= 0 else None
        except Exception as exc:
            message = f"Não foi possível ler Children.Count: {exc}"
            warnings.append(message)
            logger.warning("%s", message)
            return None

    @staticmethod
    def _read_collection_item(
        collection: ComObject, index: int, warnings: list[str]
    ) -> ComObject | None:
        try:
            try:
                return collection.Item(index)
            except (AttributeError, TypeError):
                return collection(index)
        except Exception as exc:
            message = f"Não foi possível ler Children[{index}]: {exc}"
            warnings.append(message)
            logger.warning("%s", message)
            return None

    @staticmethod
    def _warn_once(warnings: list[str], message: str) -> None:
        if message not in warnings:
            warnings.append(message)
            logger.warning("%s", message)


def normalize_object_tree_payload(
    payload: object,
    *,
    sanitizer: TextSanitizer = default_text_sanitizer,
    max_depth: int = 12,
    max_nodes: int = 2_000,
    warnings: list[str] | None = None,
) -> list[SapUiNode]:
    """Normaliza JSON decodificado de GetObjectTree para ``SapUiNode``.

    A leitura é tolerante a capitalização, wrappers ``Properties`` e coleções
    representadas como listas ou mapas, preservando apenas o modelo próprio.
    """

    if max_depth < 0:
        raise ValueError("max_depth deve ser não negativo.")
    if max_nodes <= 0:
        raise ValueError("max_nodes deve ser positivo.")

    capture_warnings = warnings if warnings is not None else []
    counter = [0]

    def visit(
        value: object,
        depth: int,
        implied_id: str | None = None,
    ) -> list[SapUiNode]:
        if counter[0] >= max_nodes:
            _append_unique(
                capture_warnings,
                f"Limite de {max_nodes} componentes atingido na normalização.",
            )
            return []
        if isinstance(value, list):
            nodes: list[SapUiNode] = []
            for item in value:
                nodes.extend(visit(item, depth))
                if counter[0] >= max_nodes:
                    break
            return nodes
        if not isinstance(value, Mapping):
            return []

        properties = _first_present(value, "Properties", "Object", "Component")
        node_values: Mapping[object, object] = (
            properties if isinstance(properties, Mapping) else value
        )
        is_node = _looks_like_node(node_values)

        if not is_node:
            nodes = []
            for key, child_value in value.items():
                if _normalized_key(key) in _SESSION_METADATA_KEYS:
                    continue
                nodes.extend(visit(child_value, depth, str(key)))
                if counter[0] >= max_nodes:
                    break
            return nodes

        counter[0] += 1
        node_id = _to_optional_text(_casefold_get(node_values, "Id")) or implied_id
        node_type = _to_optional_text(_casefold_get(node_values, "Type"))
        node_name = _to_optional_text(_casefold_get(node_values, "Name"))
        context_values = {
            "node_id": node_id,
            "node_type": node_type,
            "node_name": node_name,
        }
        value_quality = {}
        node = SapUiNode(
            quality=value_quality,
            id=node_id,
            type=node_type,
            subtype=_to_optional_text(_casefold_get(node_values, "SubType")),
            name=node_name,
            text=_sanitize_payload_text(
                _casefold_get(node_values, "Text"),
                sanitizer,
                SapTextContext(property_name="Text", **context_values),
                capture_warnings,
                quality=value_quality,
            ),
            tooltip=_sanitize_payload_text(
                _casefold_get(node_values, "Tooltip"),
                sanitizer,
                SapTextContext(property_name="Tooltip", **context_values),
                capture_warnings,
                quality=value_quality,
            ),
            changeable=_to_optional_bool(_casefold_get(node_values, "Changeable")),
            enabled=_to_optional_bool(_casefold_get(node_values, "Enabled")),
            visible=_to_optional_bool(_casefold_get(node_values, "Visible")),
        )

        children_value = _first_present(value, "Children", "Nodes", "Objects")
        if children_value is None and node_values is not value:
            children_value = _first_present(
                node_values, "Children", "Nodes", "Objects"
            )
        if children_value is None:
            return [node]
        if depth >= max_depth:
            node.truncated = True
            _append_unique(
                capture_warnings,
                f"Limite de profundidade {max_depth} atingido em "
                f"{node.id or '<sem Id>'}.",
            )
            return [node]

        if isinstance(children_value, Mapping):
            wrapped_child = _first_present(
                children_value, "Properties", "Object", "Component"
            )
            child_node_values = (
                wrapped_child
                if isinstance(wrapped_child, Mapping)
                else children_value
            )
            if _looks_like_node(child_node_values):
                node.children.extend(visit(children_value, depth + 1))
            else:
                for key, child_value in children_value.items():
                    node.children.extend(visit(child_value, depth + 1, str(key)))
                    if counter[0] >= max_nodes:
                        node.truncated = True
                        break
        else:
            node.children.extend(visit(children_value, depth + 1))
            if counter[0] >= max_nodes:
                node.truncated = True
        return [node]

    return visit(payload, 0)


def render_tree_summary(
    tree: SapUiTree,
    *,
    max_depth: int = 5,
    max_children: int = 25,
) -> list[str]:
    """Gera uma visualização resumida, evitando dumps textuais extensos."""

    if max_depth < 0 or max_children <= 0:
        raise ValueError("Limites de resumo inválidos.")
    lines: list[str] = []

    def render(node: SapUiNode, depth: int, branch: str) -> None:
        details = [node.type or "<tipo desconhecido>", node.id or "<sem Id>"]
        if node.subtype:
            details.append(f"subtype={node.subtype}")
        if node.name:
            details.append(f"name={node.name}")
        if node.text:
            details.append(f"text={node.text!r}")
        suffix = " [truncado]" if node.truncated else ""
        lines.append(f"{'  ' * depth}{branch}{' | '.join(details)}{suffix}")

        if depth >= max_depth:
            if node.children:
                lines.append(f"{'  ' * (depth + 1)}... resumo limitado ...")
            return
        visible_children = node.children[:max_children]
        for index, child in enumerate(visible_children):
            marker = "└─ " if index == len(visible_children) - 1 else "├─ "
            render(child, depth + 1, marker)
        hidden_count = len(node.children) - len(visible_children)
        if hidden_count > 0:
            lines.append(
                f"{'  ' * (depth + 1)}... {hidden_count} filho(s) omitido(s) ..."
            )

    for root in tree.roots:
        render(root, 0, "")
    return lines


def _sanitize_payload_text(
    value: object | None, sanitizer: TextSanitizer, context: SapTextContext,
    warnings: list[str], quality=None,
) -> str | None:
    from .value_capture import capture_value
    text = None if value is None else str(value)
    safe, issues = capture_value(text, sanitizer, context)
    if text is not None and isinstance(safe, str) and len(safe) < len(text) and safe.endswith("..."):
        issues = [{"path": "$", "status": "truncated"}]
    if quality is not None:
        quality["preview_" + context.property_name.lower()] = issues or ("unavailable" if text is None else "captured")
    if any(issue["status"] == "unavailable" for issue in issues):
        _append_unique(warnings, f"Sanitizador falhou para {context.property_name}; valor omitido.")
    return safe


def _casefold_get(mapping: Mapping[object, object], key: str) -> object | None:
    expected = key.casefold()
    for current_key, value in mapping.items():
        if str(current_key).casefold() == expected:
            return value
    return None


def _first_present(
    mapping: Mapping[object, object], *keys: str
) -> object | None:
    expected = {key.casefold() for key in keys}
    for current_key, value in mapping.items():
        if str(current_key).casefold() in expected:
            return value
    return None


def _looks_like_node(mapping: Mapping[object, object]) -> bool:
    keys = {_normalized_key(key) for key in mapping}
    return bool(keys.intersection({"id", "type", "name"}))


def _normalized_key(value: object) -> str:
    return str(value).casefold()


_SESSION_METADATA_KEYS = {
    "transaction",
    "program",
    "screennumber",
    "screen_number",
}


def _to_optional_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_optional_bool(value: object | None) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
        return None
    try:
        return bool(value)
    except Exception:
        return None


def _relative_window_id(component_id: str | None) -> str:
    if not component_id:
        return ""
    last_part = component_id.rsplit("/", maxsplit=1)[-1]
    return last_part if last_part.startswith("wnd[") else component_id


def _append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)
