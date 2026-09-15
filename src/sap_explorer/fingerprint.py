"""Identidade determinística, estrutural e de conteúdo, para telas SAP GUI.

Decisões de estabilidade:

* a projeção estrutural usa metadados técnicos da tela e dos componentes;
* textos e estados editáveis/visuais pertencem somente à projeção de conteúdo;
* prefixos administrativos de conexão e sessão são removidos dos IDs;
* filhos e raízes são ordenados por sua representação canônica;
* origem da captura, avisos, limites, sessão e estados ``Busy``/``IsActive``
  são metadados transitórios e não participam dos hashes;
* nenhuma regra específica de transação ou de formato de documento é aplicada.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping, Sequence

from .object_tree import SapUiNode, SapUiTree
from .screen_reader import SapScreenState


FINGERPRINT_SCHEMA_VERSION = 2

# GuiComponent.Id pode ser absoluto e incluir índices da instância local de SAP
# GUI. A identidade dentro da sessão começa em wnd[n].
_ADMINISTRATIVE_ID_PREFIX = re.compile(
    r"^(?:/)?app/con\[\d+\]/ses\[\d+\]/",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ScreenFingerprint:
    """Hashes SHA-256 da estrutura e do conteúdo observados."""

    structural_hash: str
    content_hash: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


def fingerprint_screen(state: SapScreenState) -> ScreenFingerprint:
    """Calcula hashes determinísticos para um ``SapScreenState``."""

    structural_payload = build_structural_payload(state)
    structural_hash = _sha256(structural_payload)
    content_payload = build_content_payload(
        state,
        structural_hash=structural_hash,
    )
    return ScreenFingerprint(
        structural_hash=structural_hash,
        content_hash=_sha256(content_payload),
    )


def build_structural_payload(state: SapScreenState) -> dict[str, Any]:
    """Cria a projeção canônica usada pelo structural fingerprint.

    ``Text``, ``Tooltip``, valores e propriedades de estado não são incluídos.
    Números presentes dentro de textos também não precisam ser detectados,
    porque nenhum texto participa desta projeção.
    """

    return {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "screen": {
            "transaction": _normalize_text(state.transaction),
            "program": _normalize_text(state.program),
            "screen_number": state.screen_number,
            "active_window_id": normalize_component_id(state.active_window_id),
            "active_window_type": _normalize_text(state.active_window_type),
            "window_count": state.window_count,
            "has_additional_windows": state.has_additional_windows,
        },
        "tree": _structural_tree(state.ui_tree),
    }


def build_content_payload(
    state: SapScreenState,
    *,
    structural_hash: str | None = None,
) -> dict[str, Any]:
    """Cria a projeção canônica usada pelo content fingerprint.

    O hash estrutural ancora o conteúdo à tela correspondente. Estados de
    comunicação, foco da sessão e detalhes do ambiente continuam excluídos.
    """

    resolved_structural_hash = structural_hash or _sha256(
        build_structural_payload(state)
    )
    return {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "structural_hash": resolved_structural_hash,
        "screen_content": {
            "active_window_text": _normalize_text(state.active_window_text),
            "status_bar": None if state.status_bar is None else asdict(state.status_bar),
        },
        "content_schema_version": 4,
        "tree_content": _content_tree(state.ui_tree),
    }


def normalize_component_id(component_id: str | None) -> str | None:
    """Remove somente o prefixo volátil de aplicação/conexão/sessão."""

    normalized = _normalize_text(component_id)
    if normalized is None:
        return None
    return _ADMINISTRATIVE_ID_PREFIX.sub("", normalized, count=1)


def _structural_tree(tree: SapUiTree | None) -> list[dict[str, Any]] | None:
    if tree is None:
        return None
    roots = [_structural_node(node, ancestors=set()) for node in tree.roots]
    return _sort_canonical(roots)


def _content_tree(tree: SapUiTree | None) -> list[dict[str, Any]] | None:
    if tree is None:
        return None
    roots = [_content_node(node, ancestors=set()) for node in tree.roots]
    return _sort_canonical(roots)


def _structural_node(
    node: SapUiNode,
    *,
    ancestors: set[int],
) -> dict[str, Any]:
    identity = id(node)
    if identity in ancestors:
        return {"cycle": True, "id": normalize_component_id(node.id)}

    next_ancestors = ancestors | {identity}
    children = [
        _structural_node(child, ancestors=next_ancestors)
        for child in node.children
    ]
    return {
        "id": normalize_component_id(node.id),
        "type": _normalize_text(node.type),
        "subtype": _normalize_text(node.subtype),
        "name": _normalize_text(node.name),
        "children": _sort_canonical(children),
    }


def _content_node(
    node: SapUiNode,
    *,
    ancestors: set[int],
) -> dict[str, Any]:
    identity = id(node)
    if identity in ancestors:
        return {"cycle": True, "id": normalize_component_id(node.id)}

    next_ancestors = ancestors | {identity}
    children = [
        _content_node(child, ancestors=next_ancestors)
        for child in node.children
    ]
    return {
        "id": normalize_component_id(node.id),
        "type": _normalize_text(node.type),
        "subtype": _normalize_text(node.subtype),
        "name": _normalize_text(node.name),
        "text": _normalize_text(node.text),
        "tooltip": _normalize_text(node.tooltip),
        "control_state": node.control_state,
        "changeable": node.changeable,
        "enabled": node.enabled,
        "visible": node.visible,
        "children": _sort_canonical(children),
    }


def _sort_canonical(
    values: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(values, key=_canonical_json)


def _normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFC", str(value))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n").strip()
    return normalized or None


def _sha256(payload: Mapping[str, Any]) -> str:
    canonical = _canonical_json(payload).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
