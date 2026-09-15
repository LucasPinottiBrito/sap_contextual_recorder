"""Correlação passiva de eventos, com leitura fora dos callbacks COM."""
from __future__ import annotations

from dataclasses import replace
import json
import logging
import time
from typing import Callable

from .events import SapActionEvent
from .models import SapScreenObservation, SapTransition, new_id
from .object_tree import SapObjectTreeReader, TextSanitizer, default_text_sanitizer
from .screen_reader import SapScreenReader

logger = logging.getLogger(__name__)
ACTION_EVENTS = frozenset({"Change", "ContextMenu", "AutomationFCode", "Hit"})


class SapSnapshotReader:
    """Rejeita Busy, janela ausente e troca de tela durante a captura da árvore."""

    def __init__(self, session: object, *, sanitizer: TextSanitizer = default_text_sanitizer, options=None) -> None:
        self.reader = SapScreenReader(session, sanitizer=sanitizer)
        self.tree_reader = SapObjectTreeReader(session, sanitizer=sanitizer, options=options)

    def capture(self) -> SapScreenObservation | None:
        try:
            before = self.reader.read_state()
            if (before.is_busy is not False or not before.active_window_id
                    or before.program is None or before.screen_number is None):
                return None
            tree = self.tree_reader.capture()
            after = self.reader.read_state()
            if (after.is_busy is not False or before.significant_signature() != after.significant_signature()
                    or before.session_id != after.session_id or not tree.roots
                    or tree.scope_id != after.active_window_id):
                logger.warning("Tela mudou ou ficou indisponível durante a leitura; captura adiada.")
                return None
            return SapScreenObservation(replace(after, ui_tree=tree))
        except Exception:
            logger.exception("Falha transitória na captura da tela; nova tentativa no próximo ciclo.")
            return None


class SapTransitionRecorder:
    """Relaciona a última observação verificada às ações que o SAP informou.

    Não lê COM ao consumir eventos. Se várias requisições atravessarem o pump,
    registra a tela intermediária como desconhecida em vez de atribuir a última
    tela disponível a todas as ações.
    """

    def __init__(self, capture: Callable[[], SapScreenObservation | None],
                 on_transition: Callable[[SapTransition], None], *,
                 on_observation: Callable[[SapScreenObservation], None] = lambda observation: None,
                 on_event: Callable[[SapActionEvent, SapScreenObservation | None], str] = lambda event, before: new_id(),
                 poll_interval: float = 1.0, request_timeout: float = 30.0,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if poll_interval <= 0 or request_timeout <= 0:
            raise ValueError("Intervalos devem ser positivos.")
        self.capture = capture
        self.on_transition = on_transition
        self.on_observation = on_observation
        self.on_event = on_event
        self.poll_interval = poll_interval
        self.request_timeout = request_timeout
        self.monotonic = monotonic
        self.current: SapScreenObservation | None = None
        self.actions: list[SapActionEvent] = []
        self.action_ids: list[str] = []
        self.in_request = False
        self.pending = False
        self.destroyed = False
        self.started_at = 0.0
        self.next_poll = 0.0
        self.interval_observations: list[str] = []
        self.batch_id: str | None = None
        self.batch_events: list[str] = []
        self.request_start_event: str | None = None
        self.request_end_event: str | None = None

    def handle_event(self, event: SapActionEvent) -> None:
        kind = event.event_type
        if self.pending and kind in {"Change", "StartRequest", "EndRequest"}:
            self._emit(None, "incomplete", "intermediate_screen_not_observed")
            self.current = None
        if self.in_request and kind in {"Change", "StartRequest"}:
            # Pela API, Change pertence ao lote anterior ao próximo StartRequest.
            self._emit(None, "incomplete", "missing_end_request")
            self.current = None
            self.in_request = False
        event_id = self.on_event(event, self.current)
        if kind in ACTION_EVENTS or kind in {"StartRequest", "EndRequest", "CaptureError"}:
            if self.batch_id is None:
                self.batch_id = new_id()
            self.batch_events.append(event_id)
        if kind == "StartRequest":
            self.request_start_event = event_id
        if kind == "EndRequest":
            self.request_end_event = event_id
        if kind in ACTION_EVENTS:
            self.actions.append(event)
            self.action_ids.append(event_id)
        if kind == "StartRequest":
            self.in_request = True
            self.started_at = self.monotonic()
        elif kind == "EndRequest":
            self.in_request = False
            self.pending = True
        elif kind == "Destroy":
            self.destroyed = True

    def poll(self, *, force: bool = False) -> None:
        if self.destroyed:
            return
        now = self.monotonic()
        if self.in_request:
            if now - self.started_at < self.request_timeout:
                return
            # Evento EndRequest pode ter sido perdido. Só retomamos com Busy=False.
        if not force and not self.pending and now < self.next_poll:
            return
        self.next_poll = now + self.poll_interval
        observation = self.capture()
        if observation is None:
            return
        if self.in_request:
            self._emit(None, "incomplete", "missing_end_request")
            self.current = None
            self.in_request = False
        if self.current is None and not self.pending and not self.actions:
            self.on_observation(observation)
            self.current = observation
            self.interval_observations = [observation.id]
            return
        changed = (self.current is None or observation.fingerprint != self.current.fingerprint
                   or capture_quality(observation) != capture_quality(self.current))
        if self.pending or self.actions or changed:
            reason = "end_request" if self.pending else "poll"
            status = "complete" if self.current is not None else "incomplete"
            if not self.actions:
                reason = "state_change_without_action" if changed else "request_without_action"
            self._emit(observation, status, reason)
            self.current = observation
            if observation.id not in self.interval_observations:
                self.interval_observations.append(observation.id)

    def finish(self, reason: str) -> None:
        if not self.destroyed and reason not in {"session_disconnected", "consumer_error", "error"}:
            self.poll(force=True)
        if self.actions or self.pending or self.in_request or self.batch_id:
            self._emit(None, "incomplete", reason)

    def _emit(self, after: SapScreenObservation | None, status: str, reason: str) -> None:
        batch = None
        if self.batch_id is not None:
            batch = {"id": self.batch_id, "event_ids": list(self.batch_events),
                     "request_start_event": self.request_start_event,
                     "request_end_event": self.request_end_event,
                     "observation_interval": list(self.interval_observations),
                     "exact_before_known": False, "basis": "received_command_batch"}
        transition = SapTransition(self.current, tuple(self.actions), after,
                                   tuple(self.action_ids), status, reason, batch=batch)
        self.on_transition(transition)
        if batch is not None:
            self.interval_observations = [] if after is None else [after.id]
        self.batch_id = None
        self.batch_events.clear()
        self.request_start_event = self.request_end_event = None
        self.actions.clear()
        self.action_ids.clear()
        self.pending = False


def capture_quality(observation):
    """Uma leitura antes indisponível pode tornar-se útil sem mudar o conteúdo."""
    tree = observation.state.ui_tree
    if tree is None:
        return None
    pending, nodes = list(tree.roots), []
    while pending:
        node = pending.pop()
        pending.extend(node.children)
        nodes.append((node.id or "", node.truncated, node.quality))
    return json.dumps({"nodes": sorted(nodes, key=lambda item: item[0]), "warnings": tree.warnings}, sort_keys=True)
