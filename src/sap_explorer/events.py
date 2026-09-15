"""Recepção e normalização dos eventos COM de ``GuiSession``.

Os callbacks deste módulo são deliberadamente curtos: copiam dados úteis para
tipos Python e entregam o resultado a um consumidor. Nenhum callback executa
ações no SAP, pois isso poderia disparar novos eventos e criar recursão.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, Callable, Mapping, Sequence, TypeAlias

from .object_tree import SapTextContext, TextSanitizer, default_text_sanitizer


logger = logging.getLogger(__name__)

ComObject: TypeAlias = Any
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
EventConsumer: TypeAlias = Callable[["SapActionEvent"], None]
Clock: TypeAlias = Callable[[], datetime]

# Eventos documentados diretamente na interface de saída de GuiSession. Alguns
# são condicionais: Change requer Record=True, Hit requer o modo de visualização
# e AutomationFCode é específico do SAP Workplace.
GUI_SESSION_EVENTS: tuple[str, ...] = (
    "AbapScriptingEvent",
    "Activated",
    "AutomationFCode",
    "Change",
    "ContextMenu",
    "Destroy",
    "EndRequest",
    "Error",
    "FocusChanged",
    "HistoryOpened",
    "Hit",
    "ProgressIndicator",
    "StartRequest",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class SapRawEvent:
    """Carga original e efêmera recebida pelo callback COM.

    ``arguments`` pode conter objetos COM vivos e, por isso, não é colocado na
    fila do gravador nem serializado. ``normalize_sap_event`` cria uma cópia
    segura antes que o callback retorne.
    """

    timestamp: datetime
    event_type: str
    arguments: tuple[object, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class SapRecordedCommand:
    """Uma linha de ``CommandArray`` sem convertê-la em código executável."""

    command_type: str | None
    member_name: str | None
    parameters: tuple[JsonValue, ...] = ()
    quality: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "command_type": self.command_type,
            "member_name": self.member_name,
            "parameters": list(self.parameters),
            "quality": dict(self.quality),
        }


@dataclass(frozen=True, slots=True)
class SapActionEvent:
    """Evento SAP normalizado e independente de objetos COM vivos."""

    timestamp: datetime
    event_type: str
    component_id: str | None = None
    component_type: str | None = None
    component_name: str | None = None
    commands: tuple[SapRecordedCommand, ...] = ()
    raw_commands: JsonValue = None
    details: Mapping[str, JsonValue] = field(default_factory=dict)
    raw_payload: Mapping[str, JsonValue] = field(default_factory=dict)
    capture_version: int = 2

    def to_dict(self) -> dict[str, JsonValue]:
        """Retorna somente tipos serializáveis, com ordem de comandos intacta."""

        return {
            "timestamp": self.timestamp.isoformat(),
            "capture_version": self.capture_version,
            "event_type": self.event_type,
            "component_id": self.component_id,
            "component_type": self.component_type,
            "component_name": self.component_name,
            "commands": [command.to_dict() for command in self.commands],
            "raw_commands": self.raw_commands,
            "details": dict(self.details),
            "raw_payload": dict(self.raw_payload),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def normalize_command_array(
    command_array: object,
    *,
    component: ComObject | None = None,
    sanitizer: TextSanitizer = default_text_sanitizer,
) -> tuple[JsonValue, tuple[SapRecordedCommand, ...]]:
    """Preserva e normaliza um ``CommandArray`` retornado pelo SAP.

    O pywin32 normalmente converte SAFEARRAYs COM em tuplas. A cópia bruta usa
    listas para ser serializável. A forma normalizada separa tipo, membro e
    parâmetros, mas não interpreta nem executa o comando.
    """

    from .value_capture import capture_value

    component_data = _component_snapshot(component)
    context = _text_context(component_data, "CommandParameter")
    # Keep the original array shape; do not round-trip parameter values through
    # a presentation formatter, or sanitize them twice.
    if not isinstance(command_array, (list, tuple)) or not command_array:
        return None, (SapRecordedCommand(None, None, quality={
            "status": "invalid_command_array", "parameters_preserved": False}),)
    nested = isinstance(command_array[0], (list, tuple))
    rows = command_array if nested else (command_array,)
    safe_rows, commands = [], []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            commands.append(SapRecordedCommand(None, None, quality={
                "status": "invalid_command_array", "parameters_preserved": False}))
            safe_rows.append(None)
            continue
        kind, member = row[:2]
        valid_header = isinstance(kind, str) and isinstance(member, str)
        safe, issues = capture_value(list(row[2:]), sanitizer, context)
        if _is_sensitive_component(component_data):
            safe = _redact_value(safe)
            issues = [{"path": "$", "status": "redacted"}]
        safe_rows.append([kind if isinstance(kind, str) else None,
                          member if isinstance(member, str) else None, *safe])
        # Retain compatibility with SAP/COM argument-array representations.
        # raw_commands preserves the nesting for consumers requiring exact shape.
        params = safe[0] if len(safe) == 1 and isinstance(safe[0], list) else safe
        commands.append(SapRecordedCommand(
            kind if isinstance(kind, str) else None,
            member if isinstance(member, str) else None,
            tuple(params), {"parameters_preserved": not issues and valid_header,
                            "issues": issues,
                            "parameter_layout": "array" if params is not safe else "positional",
                            "status": "captured" if valid_header else "invalid_header"}))
    return (safe_rows if nested else safe_rows[0]), tuple(commands)


def normalize_sap_event(
    raw_event: SapRawEvent,
    *,
    sanitizer: TextSanitizer = default_text_sanitizer,
) -> SapActionEvent:
    """Copia a carga COM para uma representação estável e serializável."""

    event_type = raw_event.event_type
    args = raw_event.arguments
    session: ComObject | None = None
    component: ComObject | None = None
    raw_commands: JsonValue = None
    commands: tuple[SapRecordedCommand, ...] = ()
    details: dict[str, JsonValue] = {}
    raw_payload: dict[str, JsonValue] = {}

    if event_type in {
        "Activated",
        "Destroy",
        "StartRequest",
        "EndRequest",
    }:
        session = _argument(args, 0)
    elif event_type in {"FocusChanged", "HistoryOpened", "ContextMenu"}:
        session = _argument(args, 0)
        component = _argument(args, 1)
    elif event_type == "Change":
        session = _argument(args, 0)
        component = _argument(args, 1)
        raw_commands, commands = normalize_command_array(
            _argument(args, 2), component=component, sanitizer=sanitizer
        )
    elif event_type == "Hit":
        session = _argument(args, 0)
        component = _argument(args, 1)
        raw_payload["inner_object"] = _json_safe(_argument(args, 2))
        details["inner_object"] = _sanitize_scalar(
            _argument(args, 2), sanitizer, component, "InnerObject"
        )
    elif event_type == "Error":
        session = _argument(args, 0)
        raw_payload["error_id"] = _json_safe(_argument(args, 1))
        raw_payload["descriptions"] = [
            _json_safe(value) for value in args[2:6]
        ]
        details["error_id"] = _json_safe(_argument(args, 1))
        descriptions = [
            _sanitize_scalar(value, sanitizer, None, f"Desc{index}")
            for index, value in enumerate(args[2:6], start=1)
        ]
        details["descriptions"] = descriptions
    elif event_type == "AutomationFCode":
        session = _argument(args, 0)
        raw_payload["function_code"] = _json_safe(_argument(args, 1))
        details["function_code"] = _sanitize_scalar(
            _argument(args, 1), sanitizer, None, "FunctionCode"
        )
    elif event_type == "ProgressIndicator":
        raw_payload["percentage"] = _json_safe(_argument(args, 0))
        raw_payload["text"] = _json_safe(_argument(args, 1))
        details["percentage"] = _json_safe(_argument(args, 0))
        details["text"] = _sanitize_scalar(
            _argument(args, 1), sanitizer, None, "Text"
        )
    elif event_type == "AbapScriptingEvent":
        raw_payload["parameter"] = _json_safe(_argument(args, 0))
        details["parameter"] = _sanitize_scalar(
            _argument(args, 0), sanitizer, None, "Parameter"
        )
    else:
        # Mantém compatibilidade com eventos acrescentados por outra versão do
        # type library, sem tentar adivinhar a semântica dos argumentos.
        details["arguments"] = [
            _json_safe(
                value,
                sanitizer=sanitizer,
                context=SapTextContext("Argument", None, None, None),
                sanitize_strings=True,
            )
            for value in args
        ]

    session_data = _component_snapshot(session)
    component_data = _component_snapshot(component)
    if session_data:
        raw_payload["session"] = session_data
    if component_data:
        raw_payload["component"] = component_data
    if raw_commands is not None:
        raw_payload["command_array"] = raw_commands
    for key in ("inner_object", "error_id", "descriptions", "function_code", "percentage", "text", "parameter"):
        if key in raw_payload and key in details:
            raw_payload[key] = details[key]

    return SapActionEvent(
        timestamp=_ensure_aware(raw_event.timestamp),
        event_type=event_type,
        component_id=_dict_text(component_data, "id"),
        component_type=_dict_text(component_data, "type"),
        component_name=_dict_text(component_data, "name"),
        commands=commands,
        raw_commands=raw_commands,
        details=details,
        raw_payload=raw_payload,
    )


class SapSessionEventSink:
    """Handler compatível com ``win32com.client.WithEvents``.

    O construtor precisa ser sem argumentos porque é chamado internamente pelo
    pywin32. ``configure`` é executado logo após o vínculo com a connection
    point COM.
    """

    def __init__(self) -> None:
        self._consumer: EventConsumer | None = None
        self._sanitizer: TextSanitizer = default_text_sanitizer
        self._clock: Clock = _utc_now

    def configure(
        self,
        consumer: EventConsumer,
        *,
        sanitizer: TextSanitizer = default_text_sanitizer,
        clock: Clock = _utc_now,
    ) -> None:
        self._consumer = consumer
        self._sanitizer = sanitizer
        self._clock = clock

    def _emit(self, event_type: str, *arguments: object) -> None:
        consumer = self._consumer
        if consumer is None:
            logger.warning("Evento %s recebido antes de configurar o sink.", event_type)
            return
        try:
            raw_event = SapRawEvent(self._clock(), event_type, tuple(arguments))
            event = normalize_sap_event(raw_event, sanitizer=self._sanitizer)
        except Exception as exc:
            # Persist a gap without copying exception messages/arguments, which
            # may contain values rejected by the sanitizer.
            logger.error("Falha ao normalizar evento SAP %s (%s).", event_type, type(exc).__name__)
            event = SapActionEvent(timestamp=_utc_now(), event_type="CaptureError",
                                   details={"source_event": event_type,
                                            "error_type": type(exc).__name__,
                                            "quality": "unavailable"})
        try:
            consumer(event)
        except Exception:
            logger.exception("Falha ao entregar o evento SAP %s.", event_type)

    def OnAbapScriptingEvent(self, param: object) -> None:  # noqa: N802
        self._emit("AbapScriptingEvent", param)

    def OnActivated(self, session: ComObject) -> None:  # noqa: N802
        self._emit("Activated", session)

    def OnAutomationFCode(  # noqa: N802
        self, session: ComObject, function_code: object
    ) -> None:
        self._emit("AutomationFCode", session, function_code)

    def OnChange(  # noqa: N802
        self, session: ComObject, component: ComObject, command_array: object
    ) -> None:
        self._emit("Change", session, component, command_array)

    def OnContextMenu(  # noqa: N802
        self, session: ComObject, component: ComObject
    ) -> None:
        self._emit("ContextMenu", session, component)

    def OnDestroy(self, session: ComObject) -> None:  # noqa: N802
        self._emit("Destroy", session)

    def OnEndRequest(self, session: ComObject) -> None:  # noqa: N802
        self._emit("EndRequest", session)

    def OnError(  # noqa: N802
        self,
        session: ComObject,
        error_id: object,
        desc1: object,
        desc2: object,
        desc3: object,
        desc4: object,
    ) -> None:
        self._emit("Error", session, error_id, desc1, desc2, desc3, desc4)

    def OnFocusChanged(  # noqa: N802
        self, session: ComObject, new_focused_control: ComObject
    ) -> None:
        self._emit("FocusChanged", session, new_focused_control)

    def OnHistoryOpened(  # noqa: N802
        self, session: ComObject, new_focused_control: ComObject
    ) -> None:
        self._emit("HistoryOpened", session, new_focused_control)

    def OnHit(  # noqa: N802
        self, session: ComObject, component: ComObject, inner_object: object
    ) -> None:
        self._emit("Hit", session, component, inner_object)

    def OnProgressIndicator(  # noqa: N802
        self, percentage: object, text: object
    ) -> None:
        self._emit("ProgressIndicator", percentage, text)

    def OnStartRequest(self, session: ComObject) -> None:  # noqa: N802
        self._emit("StartRequest", session)


def format_action_event(event: SapActionEvent) -> str:
    """Formata uma linha curta; a carga bruta não é impressa por padrão."""

    component = event.component_id or "-"
    if event.component_type:
        component = f"{component} ({event.component_type})"
    suffix = ""
    if event.commands:
        rendered = ", ".join(_format_command(command) for command in event.commands)
        suffix = f" | commands=[{rendered}]"
    elif event.details:
        suffix = " | " + json.dumps(
            dict(event.details), ensure_ascii=False, sort_keys=True
        )
    return (
        f"{event.timestamp.astimezone().strftime('%H:%M:%S.%f')[:-3]} | "
        f"{event.event_type} | {component}{suffix}"
    )


def _format_command(command: SapRecordedCommand) -> str:
    command_type = command.command_type or "?"
    member_name = command.member_name or "?"
    parameters = json.dumps(
        list(command.parameters), ensure_ascii=False, separators=(",", ":")
    )
    if len(parameters) > 200:
        parameters = parameters[:197] + "..."
    return f"{command_type}:{member_name}{parameters}"


def _component_snapshot(component: ComObject | None) -> dict[str, JsonValue]:
    if component is None:
        return {}
    snapshot: dict[str, JsonValue] = {}
    for key, attribute in (("id", "Id"), ("type", "Type"), ("name", "Name")):
        value = _safe_getattr(component, attribute)
        text = _optional_text(value)
        if text is not None:
            snapshot[key] = text
    return snapshot


def _safe_getattr(obj: object, attribute: str) -> object | None:
    try:
        return getattr(obj, attribute)
    except Exception as exc:
        logger.debug("Propriedade COM %s indisponível: %s", attribute, exc)
        return None


def _command_rows(value: JsonValue) -> list[list[JsonValue]]:
    if not isinstance(value, list) or not value:
        return []
    if not isinstance(value[0], list):
        return [value]
    return [item for item in value if isinstance(item, list)]


def _json_safe(
    value: object,
    *,
    sanitizer: TextSanitizer = default_text_sanitizer,
    context: SapTextContext | None = None,
    sanitize_strings: bool = False,
    depth: int = 0,
) -> JsonValue:
    if depth >= 12:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if not sanitize_strings:
            return value
        safe_context = context or SapTextContext("Value", None, None, None)
        try:
            return sanitizer(value, safe_context)
        except Exception as exc:
            logger.warning("Sanitizador falhou; texto omitido: %s", exc)
            return None
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(
                item,
                sanitizer=sanitizer,
                context=context,
                sanitize_strings=sanitize_strings,
                depth=depth + 1,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _json_safe(
                item,
                sanitizer=sanitizer,
                context=context,
                sanitize_strings=sanitize_strings,
                depth=depth + 1,
            )
            for item in value
        ]
    # Objetos COM não são mantidos; registra-se somente sua identidade segura.
    component = _component_snapshot(value)
    if component:
        return component
    try:
        rendered = str(value)
    except Exception:
        return f"<{type(value).__name__}>"
    if sanitize_strings:
        return _sanitize_scalar(rendered, sanitizer, None, "Value")
    return rendered


def _sanitize_json_value(
    value: JsonValue, sanitizer: TextSanitizer, context: SapTextContext
) -> JsonValue:
    if isinstance(value, (bool, int, float)):
        sanitized = _sanitize_json_value(str(value), sanitizer, context)
        return value if sanitized == str(value) else sanitized
    if isinstance(value, str):
        try:
            return sanitizer(value, context)
        except Exception as exc:
            logger.warning("Sanitizador falhou; parâmetro omitido: %s", exc)
            return None
    if isinstance(value, list):
        return [_sanitize_json_value(item, sanitizer, context) for item in value]
    if isinstance(value, dict):
        return {
            key: _sanitize_json_value(item, sanitizer, context)
            for key, item in value.items()
        }
    return value


def _sanitize_scalar(
    value: object,
    sanitizer: TextSanitizer,
    component: ComObject | None,
    property_name: str,
) -> JsonValue:
    if value is None:
        return value
    component_data = _component_snapshot(component)
    try:
        sanitized = sanitizer(str(value), _text_context(component_data, property_name))
        return value if isinstance(value, (bool, int, float)) and sanitized == str(value) else sanitized
    except Exception as exc:
        logger.warning("Sanitizador falhou para %s; valor omitido: %s", property_name, exc)
        return None


def _redact_command_parameters(value: JsonValue) -> JsonValue:
    """Redige apenas parâmetros, mantendo tipo e membro para diagnóstico."""

    if not isinstance(value, list):
        return value
    rows = _command_rows(value)
    redacted_rows: list[JsonValue] = []
    for row in rows:
        redacted = list(row[:2])
        redacted.extend(_redact_value(item) for item in row[2:])
        redacted_rows.append(redacted)
    return redacted_rows[0] if rows == [value] else redacted_rows


def _redact_value(value: JsonValue) -> JsonValue:
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return None if value is None else "[REDACTED]"


def _is_sensitive_component(component: Mapping[str, JsonValue]) -> bool:
    node_type = str(component.get("type") or "").casefold()
    node_id = str(component.get("id") or "").casefold()
    node_name = str(component.get("name") or "").casefold()
    if node_type == "guipasswordfield":
        return True
    if any(part.startswith("pwd") for part in node_id.split("/")):
        return True
    return any(
        marker in node_name
        for marker in ("password", "passwd", "passcode", "senha", "secret", "token")
    )


def _text_context(
    component: Mapping[str, JsonValue], property_name: str
) -> SapTextContext:
    return SapTextContext(
        property_name=property_name,
        node_id=_dict_text(component, "id"),
        node_type=_dict_text(component, "type"),
        node_name=_dict_text(component, "name"),
    )


def _dict_text(values: Mapping[str, JsonValue], key: str) -> str | None:
    value = values.get(key)
    return value if isinstance(value, str) else None


def _argument(arguments: tuple[object, ...], index: int) -> object | None:
    return arguments[index] if index < len(arguments) else None


def _optional_text(value: object | None) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _ensure_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
