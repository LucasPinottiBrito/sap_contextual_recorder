"""Ciclo de vida do listener COM e do modo de gravação de ``GuiSession``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from queue import Empty, Queue
import threading
import time
from typing import Any, Callable, Protocol, TypeAlias

from .events import (
    Clock,
    EventConsumer,
    SapActionEvent,
    SapSessionEventSink,
)
from .object_tree import TextSanitizer, default_text_sanitizer


logger = logging.getLogger(__name__)

ComObject: TypeAlias = Any
EventBinder: TypeAlias = Callable[[ComObject, type[SapSessionEventSink]], Any]
MessagePump: TypeAlias = Callable[[], object]
ComLifecycle: TypeAlias = Callable[[], None]


class ClosableEventSink(Protocol):
    def configure(
        self,
        consumer: EventConsumer,
        *,
        sanitizer: TextSanitizer,
        clock: Clock,
    ) -> None: ...

    def close(self) -> None: ...


class SapEventError(RuntimeError):
    """Erro base da infraestrutura de eventos SAP."""


class SapEventBindingError(SapEventError):
    """A connection point COM de eventos não pôde ser vinculada."""


class SapEventThreadError(SapEventError):
    """A operação COM foi tentada fora da thread proprietária."""


@dataclass(frozen=True, slots=True)
class SapRecorderCapabilities:
    """Resultado explícito da negociação do listener e do modo Record."""

    events_attached: bool
    recording_was_active: bool | None
    recording_activated_by_recorder: bool
    change_events_expected: bool
    recording_disabled_by_server: bool | None
    recording_warning: str | None = None


class SapActionRecorder:
    """Escuta uma sessão SAP sem executar ações dentro dela.

    O vínculo, o message pump e o desligamento devem ocorrer na mesma thread.
    Os callbacks COM apenas normalizam e enfileiram eventos; o consumidor é
    chamado depois que ``PumpWaitingMessages`` retorna.
    """

    def __init__(
        self,
        session: ComObject,
        on_event: EventConsumer,
        *,
        sanitizer: TextSanitizer = default_text_sanitizer,
        pump_interval_seconds: float = 0.05,
        liveness_interval_seconds: float = 1.0,
        max_liveness_failures: int = 3,
        event_binder: EventBinder | None = None,
        message_pump: MessagePump | None = None,
        co_initialize: ComLifecycle | None = None,
        co_uninitialize: ComLifecycle | None = None,
        clock: Clock | None = None,
        on_idle: Callable[[], None] | None = None,
        propagate_consumer_errors: bool = False,
    ) -> None:
        if pump_interval_seconds <= 0:
            raise ValueError("pump_interval_seconds deve ser positivo.")
        if liveness_interval_seconds <= 0:
            raise ValueError("liveness_interval_seconds deve ser positivo.")
        if max_liveness_failures <= 0:
            raise ValueError("max_liveness_failures deve ser positivo.")

        self._session = session
        self._on_event = on_event
        self._sanitizer = sanitizer
        self._pump_interval = pump_interval_seconds
        self._liveness_interval = liveness_interval_seconds
        self._max_liveness_failures = max_liveness_failures
        self._event_binder = event_binder or _default_event_binder
        self._message_pump = message_pump or _default_message_pump
        self._co_initialize = co_initialize or _default_co_initialize
        self._co_uninitialize = co_uninitialize or _default_co_uninitialize
        self._clock = clock or _default_clock
        self._on_idle = on_idle
        self._propagate_consumer_errors = propagate_consumer_errors

        self._events: Queue[SapActionEvent] = Queue()
        self._stop_requested = threading.Event()
        self._event_sink: ClosableEventSink | None = None
        self._owner_thread_id: int | None = None
        self._com_initialized = False
        self._started = False
        self._recording_activated_by_us = False
        self._capabilities: SapRecorderCapabilities | None = None
        self._stop_reason: str | None = None
        self._request_in_progress = False

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def capabilities(self) -> SapRecorderCapabilities | None:
        return self._capabilities

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    @property
    def pending_event_count(self) -> int:
        """Permite rejeitar snapshots durante os quais chegaram eventos COM."""
        return self._events.qsize()

    def start(self) -> SapRecorderCapabilities:
        """Anexa o sink e tenta habilitar ``Record`` de forma reversível."""

        if self._started:
            assert self._capabilities is not None
            return self._capabilities

        self._owner_thread_id = threading.get_ident()
        self._stop_requested.clear()
        self._stop_reason = None
        self._request_in_progress = False

        try:
            self._co_initialize()
            self._com_initialized = True
            sink = self._event_binder(self._session, SapSessionEventSink)
            self._event_sink = sink
            sink.configure(
                self._events.put,
                sanitizer=self._sanitizer,
                clock=self._clock,
            )
        except Exception as exc:
            self._release_partial_start()
            raise SapEventBindingError(
                "Não foi possível conectar o listener aos eventos de GuiSession. "
                f"Detalhe: {type(exc).__name__}: {exc}"
            ) from exc

        self._capabilities = self._configure_recording()
        self._started = True
        return self._capabilities

    def run(self) -> None:
        """Mantém o message pump ativo até CTRL+C, Destroy ou desconexão."""

        if not self._started:
            self.start()
        self._assert_owner_thread()
        next_liveness_check = time.monotonic() + self._liveness_interval
        liveness_failures = 0

        try:
            while not self._stop_requested.is_set():
                try:
                    self._message_pump()
                except Exception as exc:
                    logger.warning(
                        "Falha transitória no message pump COM; tentando novamente: %s",
                        exc,
                    )

                self.drain_events()
                if self._stop_requested.is_set():
                    break
                if self._on_idle is not None:
                    self._on_idle()

                now = time.monotonic()
                if now >= next_liveness_check:
                    if self._session_is_alive():
                        liveness_failures = 0
                    else:
                        liveness_failures += 1
                        logger.warning(
                            "Sessão SAP não respondeu à verificação (%d/%d).",
                            liveness_failures,
                            self._max_liveness_failures,
                        )
                        if liveness_failures >= self._max_liveness_failures:
                            self._stop_reason = "session_disconnected"
                            self._stop_requested.set()
                    next_liveness_check = now + self._liveness_interval

                self._stop_requested.wait(self._pump_interval)
        except KeyboardInterrupt:
            self._stop_reason = "keyboard_interrupt"
            logger.info("Gravação encerrada pelo usuário.")
        finally:
            try:
                self.drain_events()
            finally:
                self.stop()

    def drain_events(self) -> int:
        """Entrega eventos fora do callback COM e retorna a quantidade drenada."""

        delivered = 0
        while True:
            try:
                event = self._events.get_nowait()
            except Empty:
                break
            try:
                self._on_event(event)
            except Exception:
                if self._propagate_consumer_errors:
                    self._stop_reason = "consumer_error"
                    raise
                logger.exception(
                    "O consumidor falhou ao processar %s; a captura continuará.",
                    event.event_type,
                )
            delivered += 1
            if event.event_type == "StartRequest":
                self._request_in_progress = True
            elif event.event_type == "EndRequest":
                self._request_in_progress = False
            elif event.event_type == "Destroy":
                self._stop_reason = "session_destroyed"
                self._stop_requested.set()
        return delivered

    def request_stop(self) -> None:
        """Solicita parada; pode ser chamado com segurança por outra thread."""

        self._stop_reason = self._stop_reason or "stop_requested"
        self._stop_requested.set()

    def stop(self) -> None:
        """Desconecta o sink e restaura Record somente se este objeto o ativou."""

        if not self._started and self._event_sink is None and not self._com_initialized:
            return
        self._assert_owner_thread()
        self._stop_requested.set()

        if self._recording_activated_by_us:
            try:
                setattr(self._session, "Record", False)
            except Exception as exc:
                logger.warning(
                    "Não foi possível desativar session.Record durante a limpeza: %s",
                    exc,
                )
            finally:
                self._recording_activated_by_us = False

        # Record=False pode emitir os últimos Change. O sink ainda precisa existir.
        delivery_error: Exception | None = None
        try:
            self._message_pump()
            self.drain_events()
        except Exception as exc:
            delivery_error = exc
            logger.warning("Falha ao entregar eventos finais: %s", exc)
        sink, self._event_sink = self._event_sink, None
        if sink is not None:
            try:
                sink.close()
            except Exception as exc:
                logger.warning("Falha ao remover o listener COM: %s", exc)

        if self._com_initialized:
            try:
                self._co_uninitialize()
            except Exception as exc:
                logger.warning("Falha ao finalizar o apartment COM: %s", exc)
            finally:
                self._com_initialized = False

        self._started = False
        if delivery_error is not None and self._propagate_consumer_errors:
            raise delivery_error

    def _configure_recording(self) -> SapRecorderCapabilities:
        disabled = _read_recording_disabled(self._session)
        if disabled is True:
            warning = (
                "Recording está desabilitado pelo servidor; eventos SAP GUI "
                "Scripting podem não ser emitidos. Verifique "
                "sapgui/user_scripting_disable_recording."
            )
            logger.warning("%s", warning)
            return SapRecorderCapabilities(
                events_attached=True,
                recording_was_active=None,
                recording_activated_by_recorder=False,
                change_events_expected=False,
                recording_disabled_by_server=True,
                recording_warning=warning,
            )

        original_record = _read_record_state(self._session)
        if original_record is None:
            warning = (
                "A propriedade session.Record não pôde ser lida; o programa não "
                "a alterou para evitar interferir em outro recorder."
            )
            logger.warning("%s", warning)
            return SapRecorderCapabilities(
                events_attached=True,
                recording_was_active=None,
                recording_activated_by_recorder=False,
                change_events_expected=False,
                recording_disabled_by_server=disabled,
                recording_warning=warning,
            )

        if original_record:
            return SapRecorderCapabilities(
                events_attached=True,
                recording_was_active=True,
                recording_activated_by_recorder=False,
                change_events_expected=True,
                recording_disabled_by_server=disabled,
            )

        try:
            setattr(self._session, "Record", True)
        except Exception as exc:
            warning = f"Não foi possível habilitar session.Record: {exc}"
            logger.warning("%s", warning)
            return SapRecorderCapabilities(
                events_attached=True,
                recording_was_active=False,
                recording_activated_by_recorder=False,
                change_events_expected=False,
                recording_disabled_by_server=disabled,
                recording_warning=warning,
            )

        self._recording_activated_by_us = True
        confirmed_record = _read_record_state(self._session)
        if confirmed_record is False:
            self._recording_activated_by_us = False
            warning = (
                "session.Record permaneceu desativado após a tentativa; "
                "eventos Change não são esperados."
            )
            logger.warning("%s", warning)
            return SapRecorderCapabilities(
                events_attached=True,
                recording_was_active=False,
                recording_activated_by_recorder=False,
                change_events_expected=False,
                recording_disabled_by_server=disabled,
                recording_warning=warning,
            )
        return SapRecorderCapabilities(
            events_attached=True,
            recording_was_active=False,
            recording_activated_by_recorder=True,
            change_events_expected=True,
            recording_disabled_by_server=disabled,
        )

    def _session_is_alive(self) -> bool:
        # Não consulta outras propriedades durante um round trip. O SAP pode
        # bloquear chamadas síncronas nesse intervalo, justamente quando a
        # thread precisa continuar bombeando EndRequest.
        try:
            if bool(getattr(self._session, "Busy")):
                return True
            session_id = getattr(self._session, "Id")
            return bool(str(session_id).strip())
        except Exception as exc:
            logger.debug("Falha na verificação de vida da sessão: %s", exc)
            return False

    def _assert_owner_thread(self) -> None:
        if (
            self._owner_thread_id is not None
            and self._owner_thread_id != threading.get_ident()
        ):
            raise SapEventThreadError(
                "O vínculo, o message pump e a remoção dos eventos COM devem "
                "ocorrer na mesma thread."
            )

    def _release_partial_start(self) -> None:
        sink, self._event_sink = self._event_sink, None
        if sink is not None:
            try:
                sink.close()
            except Exception:
                logger.debug("Falha ao limpar sink após erro de vínculo.", exc_info=True)
        if self._com_initialized:
            try:
                self._co_uninitialize()
            except Exception:
                logger.debug("Falha ao desfazer inicialização COM.", exc_info=True)
            self._com_initialized = False


def _read_recording_disabled(session: ComObject) -> bool | None:
    try:
        info = getattr(session, "Info")
        return bool(getattr(info, "ScriptingModeRecordingDisabled"))
    except Exception as exc:
        logger.debug("Indicador de recording desabilitado indisponível: %s", exc)
        return None


def _read_record_state(session: ComObject) -> bool | None:
    try:
        return bool(getattr(session, "Record"))
    except Exception as exc:
        logger.debug("session.Record indisponível: %s", exc)
        return None


def _default_event_binder(
    session: ComObject, handler: type[SapSessionEventSink]
) -> ClosableEventSink:
    from win32com.client import WithEvents

    try:
        return WithEvents(session, handler)
    except Exception as exc:
        logger.warning(
            "WithEvents/makepy falhou (%s: %s); tentando conexão direta de eventos, "
            "sem gerar classes COM.", type(exc).__name__, exc,
        )
        from .com_events import bind_direct_events

        try:
            return bind_direct_events(session, handler)
        except Exception as direct_error:
            raise RuntimeError(
                f"WithEvents falhou ({type(exc).__name__}: {exc}); "
                f"conexão direta também falhou ({type(direct_error).__name__}: {direct_error})."
            ) from direct_error


def _default_message_pump() -> object:
    import pythoncom

    return pythoncom.PumpWaitingMessages()


def _default_co_initialize() -> None:
    import pythoncom

    pythoncom.CoInitialize()


def _default_co_uninitialize() -> None:
    import pythoncom

    pythoncom.CoUninitialize()


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)
