"""Gravação persistente; todos os objetos SAP e SQLite pertencem ao worker."""
from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from typing import Any, Callable

from .action_recorder import SapActionRecorder
from .capture_config import CaptureOptions
from .connector import SapConnector, SapSessionInfo
from .contextual_report import write_contextual_report
from .models import SapScreenObservation, SapTransition
from .object_tree import SapTextContext
from .repository import SapRepository
from .sanitization import SanitizationPolicy
from .transition_recorder import SapSnapshotReader, SapTransitionRecorder

Notification = Callable[[str, Any], None]


def record_session(session: object, info: SapSessionInfo, *, db: str, directory: str,
                   name: str, policy: SanitizationPolicy, options: CaptureOptions,
                   interval: float, notify: Notification, stop: Event,
                   notes: Queue) -> dict[str, str]:
    """Retorna arquivos somente após drenar Change finais e finalizar o diário."""
    # Valores completos para contextualização; IDs/tipos continuam na política.
    def sanitizer(value: str, context: SapTextContext) -> str | None:
        return policy(value, replace(context, property_name="ControlValue"))

    with SapRepository(db) as repository:
        case_id = repository.create_case(name, "gravacao_contextual", {}, policy)
        run_id = repository.start_run(asdict(info), case_id=case_id, metadata={
            "capture_options": asdict(options), "sanitization_policy": asdict(policy),
            "interval": interval, "full_context_text": True})
        notify("run", {"id": run_id, "case_id": case_id, "db": str(Path(db).resolve())})

        def save_observation(observation: SapScreenObservation) -> None:
            repository.save_observation(run_id, observation)
            notify("screen", observation.to_dict())

        def save_transition(transition: SapTransition) -> None:
            repository.save_transition(run_id, transition)
            notify("transition", {"id": transition.id, "status": transition.status,
                                  "reason": transition.reason, "actions": len(transition.actions)})
            if transition.after_state:
                notify("screen", transition.after_state.to_dict())

        def save_event(event, before):
            event_id = repository.save_event(run_id, event, before)
            notify("event", {"id": event_id, **event.to_dict()})
            return event_id

        snapshots = SapSnapshotReader(session, sanitizer=sanitizer, options=options)

        def capture_checked():
            if recorder.pending_event_count:
                return None
            observation = snapshots.capture()
            return None if recorder.pending_event_count else observation

        transitions = SapTransitionRecorder(capture_checked, save_transition,
            on_observation=save_observation, on_event=save_event, poll_interval=interval)

        def save_notes():
            for _ in range(100):
                try:
                    label, note, observation_id = notes.get_nowait()
                except Empty:
                    break
                # Bind to the observation the operator actually saw in the UI.
                # A queued note must not silently move to a later SAP screen.
                repository.mark_case(case_id, label, note, observation_id=observation_id)
                notify("note", "Etapa/resultado esperado registrado na observação exibida.")

        def idle():
            save_notes()
            if stop.is_set():
                recorder.request_stop()
                return
            transitions.poll()

        recorder = SapActionRecorder(session, transitions.handle_event, sanitizer=sanitizer,
                                     on_idle=idle, propagate_consumer_errors=True)
        reason = "error"
        try:
            capabilities = recorder.start()
            repository.set_capabilities(run_id, capabilities)
            notify("capabilities", asdict(capabilities))
            transitions.poll(force=True)
            notify("started", run_id)
            recorder.run()
            reason = recorder.stop_reason or "stopped"
        finally:
            try:
                recorder.stop()
            finally:
                try:
                    save_notes()
                    transitions.finish(reason)
                finally:
                    repository.finish_run(run_id, reason)
                    repository.finish_case(case_id, "inconclusive", "Gravação encerrada; resultado funcional a revisar.")
                    paths = write_contextual_report(repository, run_id, directory)
                    notify("files", paths)
        return paths


class RecordingWorker:
    """Fila de comandos sem referências COM cruzando a fronteira do Tkinter."""

    def __init__(self, *, db: str, directory: str, policy: SanitizationPolicy,
                 options: CaptureOptions, interval: float):
        self.settings = dict(db=db, directory=directory, policy=policy, options=options, interval=interval)
        self.commands: Queue = Queue()
        self.messages: Queue = Queue()
        self.notes: Queue = Queue()
        self.stop = Event()
        self.shutdown = Event()
        self.thread = Thread(target=self._run, name="sap-recorder-com", daemon=False)

    def notify(self, kind: str, payload: Any) -> None:
        self.messages.put((kind, payload))

    def close(self) -> None:
        self.shutdown.set()
        self.stop.set()
        self.commands.put(("close", None))

    def _run(self) -> None:
        connector = None
        initialized = False
        try:
            import pythoncom
            pythoncom.CoInitialize()
            initialized = True
            connector = SapConnector()
            sessions: list[SapSessionInfo] = []
            while not self.shutdown.is_set():
                command, payload = self.commands.get()
                if command == "close":
                    break
                try:
                    if command == "discover":
                        sessions = []
                        sessions = connector.discover_sessions()
                        self.notify("sessions", [asdict(s) for s in sessions])
                    elif command == "record":
                        index, name = payload
                        if not 0 <= index < len(sessions):
                            raise ValueError("Selecione uma sessão da lista atualizada.")
                        info = sessions[index]
                        session = connector.select_session(info.connection_index, info.session_index)
                        try:
                            record_session(session, info, name=name, **self.settings,
                                           notify=self.notify, stop=self.stop, notes=self.notes)
                        finally:
                            session = None
                except Exception as exc:
                    self.notify("error", f"{type(exc).__name__}: {exc}")
                finally:
                    self.notify("idle", command)
        except Exception as exc:
            self.notify("fatal", f"Não foi possível iniciar o gravador: {exc}")
        finally:
            # Release dispatches before uninitializing their COM apartment.
            connector = None
            if initialized:
                pythoncom.CoUninitialize()
            self.notify("closed", None)
