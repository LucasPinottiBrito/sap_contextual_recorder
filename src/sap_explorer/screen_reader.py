"""Leitura defensiva e monitoramento do estado atual de uma sessão SAP GUI.

Somente propriedades administrativas da sessão e das janelas de primeiro nível
são consultadas. Este módulo não percorre a árvore de componentes e não executa
ações na sessão.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import logging
import time
from typing import Any, Callable, Protocol, TypeAlias

from .object_tree import SapUiTree, SapTextContext, TextSanitizer, default_text_sanitizer


logger = logging.getLogger(__name__)

ComObject: TypeAlias = Any


@dataclass(frozen=True, slots=True)
class SapStatusBar:
    text: str | None = None
    message_type: str | None = None
    message_id: str | None = None
    message_number: str | None = None
    message_as_popup: bool | None = None
    message_parameters: tuple[str | None, ...] = ()
    message_has_long_text: bool | None = None


@dataclass(frozen=True, slots=True)
class SapScreenState:
    """Representação imutável do estado observável da sessão SAP."""

    transaction: str | None = None
    program: str | None = None
    screen_number: int | None = None
    active_window_id: str | None = None
    active_window_type: str | None = None
    active_window_text: str | None = None
    window_count: int | None = None
    has_additional_windows: bool | None = None
    session_id: str | None = None
    is_busy: bool | None = None
    is_active: bool | None = None
    is_low_speed_connection: bool | None = None
    scripting_mode_read_only: bool | None = None
    scripting_mode_recording_disabled: bool | None = None
    ui_tree: SapUiTree | None = None
    status_bar: SapStatusBar | None = None

    def significant_signature(
        self,
    ) -> tuple[str | int | bool | None, ...]:
        """Retorna somente os campos que identificam uma mudança de tela.

        Estados de diagnóstico como ``is_busy`` e ``is_active`` ficam fora da
        assinatura para não gerar uma linha a cada comunicação com o servidor.
        """

        return (
            self.transaction,
            self.program,
            self.screen_number,
            self.active_window_id,
            self.active_window_type,
            self.active_window_text,
            self.window_count,
            self.has_additional_windows,
            None if self.status_bar is None else self.status_bar.text,
            None if self.status_bar is None else self.status_bar.message_type,
            None if self.status_bar is None else self.status_bar.message_id,
            None if self.status_bar is None else self.status_bar.message_number,
        )

    def has_significant_change(self, previous: SapScreenState | None) -> bool:
        """Indica se este estado deve produzir uma nova observação."""

        return previous is None or (
            self.significant_signature() != previous.significant_signature()
        )

    def to_dict(self) -> dict[str, Any]:
        """Serializa o estado para um dicionário composto por tipos JSON."""

        return asdict(self)

    def to_json(self) -> str:
        """Serializa o estado como JSON UTF-8 estável e legível."""

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


class ScreenStateReader(Protocol):
    """Contrato mínimo usado pelo monitor e substituível por eventos no futuro."""

    def read_state(self) -> SapScreenState:
        """Lê o estado atual da sessão."""

        ...


class SapScreenReader:
    """Encapsula leituras COM defensivas de uma sessão SAP selecionada."""

    def __init__(self, session: ComObject, *, sanitizer: TextSanitizer = default_text_sanitizer) -> None:
        self._session = session
        self._sanitizer = sanitizer

    def read_state(self) -> SapScreenState:
        """Captura uma fotografia do estado atual sem executar ações.

        ``Busy`` é lido primeiro. Quando verdadeiro, nenhuma outra propriedade
        COM é consultada naquele ciclo, evitando bloquear enquanto o SAP espera
        uma resposta do servidor.
        """

        is_busy = self._read_bool(self._session, "Busy", "session.Busy")
        if is_busy is not False:
            logger.debug("Sessão SAP ocupada ou Busy indisponível; leitura adiada.")
            return SapScreenState(is_busy=is_busy)

        info = self._read_attr(self._session, "Info", "session.Info")
        active_window = self._read_attr(
            self._session, "ActiveWindow", "session.ActiveWindow"
        )
        children = self._read_attr(self._session, "Children", "session.Children")
        window_count = self._read_int(children, "Count", "session.Children.Count")

        return SapScreenState(
            transaction=self._read_text(info, "Transaction", "session.Info.Transaction"),
            program=self._read_text(info, "Program", "session.Info.Program"),
            screen_number=self._read_int(
                info, "ScreenNumber", "session.Info.ScreenNumber"
            ),
            active_window_id=self._read_text(
                active_window, "Id", "session.ActiveWindow.Id"
            ),
            active_window_type=self._read_text(
                active_window, "Type", "session.ActiveWindow.Type"
            ),
            active_window_text=self._sanitize(
                self._read_text(active_window, "Text", "session.ActiveWindow.Text"),
                "ActiveWindowText", "ActiveWindow",
            ),
            window_count=window_count,
            has_additional_windows=(
                None if window_count is None else window_count > 1
            ),
            session_id=self._read_text(self._session, "Id", "session.Id"),
            is_busy=is_busy,
            is_active=self._read_bool(self._session, "IsActive", "session.IsActive"),
            is_low_speed_connection=self._read_bool(
                info,
                "IsLowSpeedConnection",
                "session.Info.IsLowSpeedConnection",
            ),
            scripting_mode_read_only=self._read_bool(
                info,
                "ScriptingModeReadOnly",
                "session.Info.ScriptingModeReadOnly",
            ),
            scripting_mode_recording_disabled=self._read_bool(
                info,
                "ScriptingModeRecordingDisabled",
                "session.Info.ScriptingModeRecordingDisabled",
            ),
            status_bar=self.read_status_bar(),
        )

    def _sanitize(self, value: str | None, property_name: str, node_id: str) -> str | None:
        if value is None:
            return None
        try:
            return self._sanitizer(value, SapTextContext(property_name, node_id, None, None))
        except Exception:
            logger.warning("Sanitização falhou em %s; texto omitido.", property_name)
            return None

    def read_status_bar(self) -> SapStatusBar | None:
        """FindById apenas localiza o controle; não aciona a interface."""
        try:
            bar = self._session.FindById("wnd[0]/sbar", False)
        except Exception as exc:
            logger.debug("Status bar indisponível: %s", exc)
            return None
        if bar is None:
            return None
        return SapStatusBar(
            text=self._sanitize(self._read_text(bar, "Text", "sbar.Text"), "Text", "wnd[0]/sbar"),
            message_type=self._read_text(bar, "MessageType", "sbar.MessageType"),
            message_id=self._read_text(bar, "MessageId", "sbar.MessageId"),
            message_number=self._read_text(bar, "MessageNumber", "sbar.MessageNumber"),
            message_as_popup=self._read_bool(bar, "MessageAsPopup", "sbar.MessageAsPopup"),
            message_parameters=self._message_parameters(bar),
            message_has_long_text=self._optional_long_text(bar),
        )

    def _message_parameters(self, bar: ComObject) -> tuple[str | None, ...]:
        """SAP documenta índices 0..7; None significa leitura indisponível."""
        values = []
        for index in range(8):
            try:
                value = bar.MessageParameter(index)
                values.append(None if value is None else self._sanitizer(
                    str(value), SapTextContext("ControlValue", "wnd[0]/sbar", "GuiStatusbar", None)))
            except Exception:
                values.append(None)
        return tuple(values)

    @staticmethod
    def _optional_long_text(bar: ComObject) -> bool | None:
        try:
            return bool(bar.MessageHasLongText)
        except Exception:
            return None

    @staticmethod
    def _read_attr(obj: ComObject | None, attribute: str, label: str) -> ComObject | None:
        if obj is None:
            return None
        try:
            return getattr(obj, attribute)
        except Exception as exc:
            logger.warning("Não foi possível ler %s: %s", label, exc)
            return None

    @classmethod
    def _read_text(
        cls, obj: ComObject | None, attribute: str, label: str
    ) -> str | None:
        value = cls._read_attr(obj, attribute, label)
        if value is None:
            return None
        try:
            text = str(value).strip()
        except Exception as exc:
            logger.warning("Não foi possível converter %s para texto: %s", label, exc)
            return None
        return text or None

    @classmethod
    def _read_int(
        cls, obj: ComObject | None, attribute: str, label: str
    ) -> int | None:
        value = cls._read_attr(obj, attribute, label)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            logger.warning("Não foi possível converter %s para inteiro: %s", label, exc)
            return None
        except Exception as exc:
            logger.warning("Erro inesperado ao converter %s para inteiro: %s", label, exc)
            return None

    @classmethod
    def _read_bool(
        cls, obj: ComObject | None, attribute: str, label: str
    ) -> bool | None:
        value = cls._read_attr(obj, attribute, label)
        if value is None:
            return None
        try:
            return bool(value)
        except Exception as exc:
            logger.warning("Não foi possível converter %s para booleano: %s", label, exc)
            return None


class SapScreenMonitor:
    """Detecta mudanças significativas por polling moderado e substituível."""

    def __init__(
        self,
        reader: ScreenStateReader,
        *,
        interval_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("O intervalo de monitoramento deve ser positivo.")
        self._reader = reader
        self._interval_seconds = interval_seconds
        self._sleep = sleep
        self._last_state: SapScreenState | None = None

    @property
    def last_state(self) -> SapScreenState | None:
        """Último estado completo aceito pelo monitor."""

        return self._last_state

    def poll_once(self) -> SapScreenState | None:
        """Executa uma leitura e retorna o estado somente quando ele mudou."""

        try:
            current = self._reader.read_state()
        except Exception:
            logger.exception(
                "Erro transitório ao ler a sessão SAP; o monitor tentará novamente."
            )
            return None

        if current.is_busy is True:
            logger.debug("Ciclo de monitoramento ignorado porque a sessão está ocupada.")
            return None
        if all(value is None for value in (current.transaction, current.program,
                                          current.screen_number, current.active_window_id)):
            logger.debug("Estado indisponível; última observação preservada.")
            return None

        changed = current.has_significant_change(self._last_state)
        self._last_state = current
        return current if changed else None

    def run(self, on_change: Callable[[SapScreenState], None]) -> None:
        """Monitora continuamente até receber CTRL+C."""

        logger.info(
            "Monitor iniciado com polling de %.1f s. Pressione CTRL+C para encerrar.",
            self._interval_seconds,
        )
        try:
            while True:
                changed_state = self.poll_once()
                if changed_state is not None:
                    on_change(changed_state)
                self._sleep(self._interval_seconds)
        except KeyboardInterrupt:
            logger.info("Monitor encerrado pelo usuário.")


def format_screen_state(
    state: SapScreenState, observed_at: datetime | None = None
) -> str:
    """Formata uma mudança de estado em uma linha compacta para o CLI."""

    moment = observed_at or datetime.now()
    screen_number = (
        "-" if state.screen_number is None else f"{state.screen_number:04d}"
    )
    window_id = _short_component_id(state.active_window_id)
    return "  ".join(
        (
            moment.strftime("%H:%M:%S"),
            " | ".join(
                (
                    _display(state.transaction),
                    _display(state.program),
                    screen_number,
                    window_id,
                    _display(state.active_window_text),
                )
            ),
        )
    )


def _short_component_id(component_id: str | None) -> str:
    if not component_id:
        return "-"
    return component_id.rsplit("/", maxsplit=1)[-1]


def _display(value: object | None) -> str:
    return "-" if value is None or value == "" else str(value)
