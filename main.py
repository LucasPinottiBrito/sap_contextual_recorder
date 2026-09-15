"""Entrada do gravador gráfico e das ferramentas CLI de exploração SAP."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import logging
from pathlib import Path
import sys
import sqlite3
from typing import Sequence


# Permite executar `python main.py` sem instalar o pacote em modo editável.
SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sap_explorer import (  # noqa: E402
    SapActionRecorder,
    SapConnector,
    SapConnectorError,
    SapEventError,
    SapScreenMonitor,
    SapScreenReader,
    SapSessionInfo,
    format_action_event,
    format_screen_state,
    render_tree_summary,
)


logger = logging.getLogger("sap_explorer.cli")

from sap_explorer.analyzer import write_report
from sap_explorer.repository import SapRepository, write_json
from sap_explorer.sanitization import SanitizationPolicy
from sap_explorer.capture_config import CaptureOptions
from sap_explorer.transition_recorder import SapSnapshotReader, SapTransitionRecorder
from sap_explorer.models import SapScreenObservation, SapTransition
from sap_explorer.events import SapActionEvent


def _display(value: object | None) -> str:
    return "-" if value is None or value == "" else str(value)


def log_sessions(sessions: Sequence[SapSessionInfo]) -> None:
    """Registra a lista numerada usada para a seleção no CLI."""

    logger.info("Sessões SAP disponíveis:")
    for selection_index, session in enumerate(sessions):
        logger.info(
            "[%d] conexão=%d sessão=%d | descrição=%s | sistema=%s | "
            "cliente=%s | usuário=%s | transação=%s | programa=%s | tela=%s",
            selection_index,
            session.connection_index,
            session.session_index,
            _display(session.connection_description),
            _display(session.system_name),
            _display(session.client),
            _display(session.user),
            _display(session.transaction),
            _display(session.program),
            _display(session.screen_number),
        )


def read_selection(session_count: int) -> int:
    """Lê e valida o índice mostrado ao usuário."""

    print(f"Selecione uma sessão [0-{session_count - 1}]: ", end="", file=sys.stderr, flush=True)
    raw_value = input().strip()
    try:
        selection = int(raw_value)
    except ValueError as exc:
        raise ValueError("Informe um índice numérico.") from exc

    if not 0 <= selection < session_count:
        raise ValueError(f"O índice deve estar entre 0 e {session_count - 1}.")
    return selection


def run(command: str | None = None, *, json_output: bool = False,
        db: str = "artifacts/exploration.sqlite3", output: str = "artifacts/exploration.json",
        artifacts: str = "artifacts", sanitize_config: str | None = None,
        session_index: int | None = None, interval: float = 1.0,
        max_depth: int = 24, max_nodes: int = 2000, grid_rows: int = 20,
        grid_columns: int = 20, capture_seconds: float = 0.5, control_scope=None,
        case_file: str | None = None, case_id: str | None = None,
        label: str | None = None, note: str | None = None,
        outcome: str | None = None, evidence: str | None = None,
        run_id: str | None = None) -> int:
    capture_options = CaptureOptions(max_depth=max_depth, max_nodes=max_nodes,
                                     max_grid_rows=grid_rows, max_grid_columns=grid_columns,
                                     enrichment_seconds=capture_seconds,
                                     control_scopes=tuple(control_scope or ()))
    if command in {None, "record", "gui"}:
        from sap_explorer.recorder_ui import launch_recorder
        policy = SanitizationPolicy.from_file(sanitize_config) if sanitize_config else SanitizationPolicy()
        return launch_recorder(db=db, directory=artifacts, policy=policy,
                               options=capture_options, interval=interval)
    if command == "context":
        from sap_explorer.contextual_report import write_contextual_report
        if not Path(db).is_file():
            raise ValueError(f"Banco não encontrado: {db}")
        if not run_id:
            raise ValueError("Informe --run-id para exportar o roteiro de uma gravação.")
        with SapRepository(db) as repository:
            paths = write_contextual_report(repository, run_id, artifacts)
            logger.info("Roteiro contextual: %s", paths)
        return 0
    case_spec = None
    if case_file:
        if case_id:
            raise ValueError("Use --case-file ou --case-id, não ambos.")
        case_spec = json.loads(Path(case_file).read_text(encoding="utf-8-sig"))
        if (not isinstance(case_spec, dict) or set(case_spec) - {"name", "process", "inputs"}
                or not isinstance(case_spec.get("name"), str) or not case_spec["name"].strip()
                or not isinstance(case_spec.get("process"), str) or not case_spec["process"].strip()
                or not isinstance(case_spec.get("inputs"), dict)):
            raise ValueError("Arquivo do caso exige name, process e inputs (objeto).")
    if command in {"stats", "export", "analyze", "cases", "case-mark", "case-finish"}:
        if not Path(db).is_file():
            logger.error("Banco não encontrado: %s. Execute explore primeiro.", db)
            return 1
        with SapRepository(db) as repository:
            if repository.migration_backup:
                logger.info("Backup anterior à migração: %s", repository.migration_backup)
            if command == "cases":
                print(json.dumps(repository.list_cases(), ensure_ascii=False, indent=2))
            elif command == "case-mark":
                if not case_id:
                    raise ValueError("Informe --case-id.")
                repository.mark_case(case_id, label, note)
                logger.info("Marco registrado no caso %s.", case_id)
            elif command == "case-finish":
                if not case_id:
                    raise ValueError("Informe --case-id.")
                repository.finish_case(case_id, outcome, evidence)
                logger.info("Caso %s finalizado: %s.", case_id, outcome)
            elif command == "stats":
                print(json.dumps(repository.stats(), ensure_ascii=False, indent=2))
            elif command == "export":
                write_json(output, repository.export())
                logger.info("Mapa e histórico exportados para %s.", output)
            else:
                write_report(repository, artifacts)
                logger.info("Relatórios gerados em %s.", artifacts)
        return 0
    sanitizer = SanitizationPolicy.from_file(sanitize_config) if sanitize_config else SanitizationPolicy()
    connector = SapConnector()
    try:
        sessions = connector.discover_sessions()
        log_sessions(sessions)
        selection = read_selection(len(sessions)) if session_index is None else session_index
        if not 0 <= selection < len(sessions):
            raise ValueError("Índice de sessão inválido.")
        selected_info = sessions[selection]
        selected_session = connector.select_session(
            selected_info.connection_index,
            selected_info.session_index,
        )
    except (SapConnectorError, ValueError, EOFError, KeyboardInterrupt) as exc:
        logger.error("%s", exc)
        return 1

    logger.info("Conexão com a sessão confirmada.")
    logger.info(
        "Sessão selecionada: sistema=%s, cliente=%s, usuário=%s, "
        "transação=%s, programa=%s, tela=%s.",
        _display(selected_info.system_name),
        _display(selected_info.client),
        _display(selected_info.user),
        _display(selected_info.transaction),
        _display(selected_info.program),
        _display(selected_info.screen_number),
    )

    if command == "monitor":
        reader = SapScreenReader(selected_session, sanitizer=sanitizer)
        monitor = SapScreenMonitor(reader, interval_seconds=interval)
        monitor.run(lambda state: logger.info("%s", format_screen_state(state)))
    elif command == "inspect":
        observation = SapSnapshotReader(selected_session, sanitizer=sanitizer, options=capture_options).capture()
        if observation is None:
            logger.error("Tela ocupada, indisponível ou alterada durante a leitura. Execute inspect novamente quando estiver estável.")
            return 1
        enriched_state = screen_state = observation.state
        ui_tree = enriched_state.ui_tree
        assert ui_tree is not None
        if json_output:
            # JSON é destinado a ferramentas consumidoras e, por isso, vai para
            # stdout; os logs continuam no stderr.
            print(enriched_state.to_json())
        else:
            logger.info("Estado atual: %s", screen_state.to_json())
            logger.info(
                "Estrutura: %d componente(s), estratégia=%s, escopo=%s.",
                ui_tree.component_count,
                ui_tree.source,
                _display(ui_tree.scope_id),
            )
            if ui_tree.warnings:
                logger.warning(
                    "A captura estrutural terminou com %d aviso(s).",
                    len(ui_tree.warnings),
                )
            logger.info("Árvore resumida:")
            for line in render_tree_summary(ui_tree):
                logger.info("%s", line)
    elif command == "record-cli":
        recorder = SapActionRecorder(
            selected_session,
            lambda event: logger.info("%s", format_action_event(event)),
            sanitizer=sanitizer,
        )
        try:
            capabilities = recorder.start()
            if capabilities.change_events_expected:
                logger.info(
                    "Listener ativo e modo de gravação disponível; "
                    "eventos Change são esperados."
                )
            else:
                logger.warning(
                    "Listener ativo, mas eventos Change não são esperados: %s",
                    capabilities.recording_warning or "Record não está ativo.",
                )
            logger.info(
                "Use o SAP manualmente. Pressione CTRL+C para encerrar; "
                "nenhuma ação será executada pelo programa."
            )
            recorder.run()
        except SapEventError as exc:
            logger.error("%s", exc)
            return 1
        if recorder.stop_reason in {"session_destroyed", "session_disconnected"}:
            logger.warning("Captura encerrada: a sessão SAP foi fechada ou desconectada.")
    elif command == "explore":
        return run_exploration(selected_session, selected_info, db, sanitizer, interval,
                               capture_options=capture_options, case_spec=case_spec, case_id=case_id,
                               artifacts=artifacts)
    return 0


def run_exploration(session: object, info: SapSessionInfo, db: str,
                    sanitizer: SanitizationPolicy, interval: float, *, capture_options=None,
                    case_spec=None, case_id=None, artifacts=None) -> int:
    capture_options = capture_options or CaptureOptions()
    with SapRepository(db) as repository:
        if repository.migration_backup:
            logger.info("Backup anterior à migração: %s", repository.migration_backup)
        if case_id:
            sanitizer = repository.case_policy(case_id)
        else:
            spec = case_spec or {"name": "Exploração SAP", "process": "exploracao", "inputs": {}}
            case_id = repository.create_case(spec["name"], spec["process"], spec["inputs"], sanitizer)
        run_id = repository.start_run(asdict(info), case_id=case_id, metadata={
            "capture_options": asdict(capture_options), "sanitization_policy": asdict(sanitizer),
            "interval": interval})
        logger.info("Caso %s. Resultado permanece inconclusivo até case-finish.", case_id)

        def log_state(observation: SapScreenObservation) -> None:
            state = observation.state
            logger.info("STATE %s | %s | Busy=%s | popup=%s | sessão=%s",
                        observation.fingerprint.structural_hash, format_screen_state(state),
                        state.is_busy, state.has_additional_windows, state.session_id)
            if state.status_bar is not None:
                logger.info("SAP STATUS [%s] %s | id=%s número=%s", state.status_bar.message_type,
                            state.status_bar.text, state.status_bar.message_id, state.status_bar.message_number)

        def save_observation(observation: SapScreenObservation) -> None:
            repository.save_observation(run_id, observation)
            log_state(observation)

        def save_transition(transition: SapTransition) -> None:
            repository.save_transition(run_id, transition)
            logger.info("TRANSITION %s | %s | %s | ações=%d", transition.id,
                        transition.status, transition.reason, len(transition.actions))
            if transition.after_state is not None:
                log_state(transition.after_state)

        def save_event(event: SapActionEvent, before: SapScreenObservation | None) -> str:
            event_id = repository.save_event(run_id, event, before)
            logger.info("%s", format_action_event(event))
            return event_id

        snapshots = SapSnapshotReader(session, sanitizer=sanitizer, options=capture_options)

        def capture_checked() -> SapScreenObservation | None:
            if recorder.pending_event_count:
                return None
            observation = snapshots.capture()
            if recorder.pending_event_count:
                logger.warning("Eventos chegaram durante a leitura; snapshot descartado para evitar correlação incorreta.")
                return None
            return observation

        transitions = SapTransitionRecorder(capture_checked, save_transition,
                                             on_observation=save_observation, on_event=save_event,
                                             poll_interval=interval)
        recorder = SapActionRecorder(session, transitions.handle_event, sanitizer=sanitizer,
                                     on_idle=transitions.poll, propagate_consumer_errors=True)
        reason = "error"
        try:
            capabilities = recorder.start()
            repository.set_capabilities(run_id, capabilities)
            transitions.poll(force=True)
            logger.info("Exploração %s iniciada. Histórico: %s. CTRL+C encerra.", run_id, db)
            if not capabilities.change_events_expected:
                logger.warning("Ações podem não ser entregues pelo SAP: %s. Estados continuam monitorados.",
                               capabilities.recording_warning)
            logger.info("Acompanhe a sessão selecionada. Ações de scripts externos dependem dos eventos entregues pelo SAP.")
            recorder.run()
            reason = recorder.stop_reason or "stopped"
        except KeyboardInterrupt:
            reason = "keyboard_interrupt"
        except Exception:
            reason = recorder.stop_reason or "error"
            raise
        finally:
            try:
                recorder.stop()
            finally:
                try:
                    transitions.finish(reason)
                finally:
                    repository.finish_run(run_id, reason)
                    if artifacts is not None:
                        from sap_explorer.contextual_report import write_contextual_report
                        paths = write_contextual_report(repository, run_id, artifacts)
                        logger.info("Roteiro contextual gerado: %s", paths)
        logger.info("Exploração encerrada: %s. %s", reason, repository.stats())
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Exploração e registro passivo de sessões SAP GUI.")
    parser.add_argument("command", nargs="?", choices=["gui", "monitor", "inspect", "record", "record-cli", "explore", "context", "stats", "export", "analyze", "cases", "case-mark", "case-finish"])
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--db", default="artifacts/exploration.sqlite3")
    parser.add_argument("--output", default="artifacts/exploration.json")
    parser.add_argument("--artifacts", default="artifacts")
    parser.add_argument("--sanitize-config")
    parser.add_argument("--session-index", type=int)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--max-depth", type=int, default=24)
    parser.add_argument("--max-nodes", type=int, default=2000)
    parser.add_argument("--grid-rows", type=int, default=20)
    parser.add_argument("--grid-columns", type=int, default=20)
    parser.add_argument("--capture-seconds", type=float, default=0.5)
    parser.add_argument("--control-scope", action="append")
    parser.add_argument("--case-file")
    parser.add_argument("--case-id")
    parser.add_argument("--run-id")
    parser.add_argument("--label")
    parser.add_argument("--note")
    parser.add_argument("--outcome", choices=["confirmed", "failed", "cancelled", "inconclusive"])
    parser.add_argument("--evidence")
    options = parser.parse_args(arguments)
    if options.run_id and options.command != "context":
        parser.error("--run-id só é aceito com context.")
    if options.session_index is not None and options.command in {None, "record", "gui"}:
        parser.error("A janela exige seleção explícita da sessão. Use explore ou record-cli para --session-index.")
    if options.case_file and options.command != "explore":
        parser.error("--case-file só é aceito com explore.")
    if options.case_id and options.command not in {"explore", "case-mark", "case-finish"}:
        parser.error("--case-id exige explore, case-mark ou case-finish.")
    if options.case_id and options.sanitize_config:
        parser.error("Casos existentes usam a política de sanitização salva no caso.")
    if options.json_output and options.command != "inspect":
        parser.error("--json só é aceito com inspect.")
    if not 0 < options.interval < float("inf"):
        parser.error("--interval deve ser positivo e finito.")
    try:
        result = run(**vars(options))
    except (OSError, ValueError, sqlite3.Error, SapEventError) as exc:
        logger.error("Operação interrompida: %s", exc, exc_info=True)
        result = 1
    raise SystemExit(result)


def _parse_arguments(arguments: Sequence[str]) -> tuple[str | None, bool]:
    if not arguments:
        return None, False
    if list(arguments) == ["monitor"]:
        return "monitor", False
    if list(arguments) == ["inspect"]:
        return "inspect", False
    if list(arguments) == ["inspect", "--json"]:
        return "inspect", True
    if list(arguments) == ["record"]:
        return "record", False
    if len(arguments) == 1 and arguments[0] in {"explore", "stats", "export", "analyze"}:
        return arguments[0], False
    return "invalid", False


if __name__ == "__main__":
    main()
