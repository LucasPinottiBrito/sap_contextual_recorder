"""Histórico SQLite transacional, consolidado por fingerprints e ações."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from .events import SapActionEvent
from .fingerprint import build_structural_payload, normalize_component_id
from .models import SapScreenObservation, SapTransition, new_id, utc_now


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def action_signature(actions: tuple[SapActionEvent, ...]) -> str:
    """Agrupa pela sequência exata de comandos sanitizados, sem timestamp."""
    return encode([{"event_type": a.event_type, "component_id": normalize_component_id(a.component_id),
                    "component_type": a.component_type, "commands": [c.to_dict() for c in a.commands],
                    "details": dict(a.details)} for a in actions])


class SapRepository:
    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        path = Path(path)
        existing = path.is_file() and path.stat().st_size > 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=timeout)
        self.connection.row_factory = sqlite3.Row
        try:
            from .schema import SCHEMA_VERSION
            if self.connection.execute("PRAGMA user_version").fetchone()[0] > SCHEMA_VERSION:
                raise ValueError("Banco criado por versão mais recente do gravador.")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                    session_json TEXT NOT NULL, capabilities_json TEXT, stop_reason TEXT);
                CREATE TABLE IF NOT EXISTS screens (
                    structural_hash TEXT PRIMARY KEY, definition_json TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    structural_hash TEXT NOT NULL REFERENCES screens(structural_hash),
                    content_hash TEXT NOT NULL, timestamp TEXT NOT NULL, state_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    before_observation TEXT REFERENCES observations(id), timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL, is_action INTEGER NOT NULL, event_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS transitions (
                    id TEXT PRIMARY KEY, from_screen TEXT NOT NULL REFERENCES screens(structural_hash),
                    action_json TEXT NOT NULL, to_screen TEXT NOT NULL REFERENCES screens(structural_hash),
                    count INTEGER NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS transition_observations (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    transition_id TEXT REFERENCES transitions(id),
                    before_observation TEXT REFERENCES observations(id),
                    after_observation TEXT REFERENCES observations(id),
                    timestamp TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
                    action_ids_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS observations_run ON observations(run_id);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
                CREATE INDEX IF NOT EXISTS transition_observations_run ON transition_observations(run_id);
            """)
            from .schema import migrate
            self.migration_backup = migrate(self.connection, path, existing=existing)
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> SapRepository:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def start_run(self, session: dict[str, Any], *, metadata=None, case_id=None) -> str:
        run_id = new_id()
        with self.connection:
            if case_id is not None:
                # Serialize case lifecycle changes before validating its state.
                self.connection.execute("UPDATE cases SET outcome=outcome WHERE id=?", (case_id,))
                case = self.connection.execute("SELECT ended_at FROM cases WHERE id=?", (case_id,)).fetchone()
                if case is None or case[0] is not None:
                    raise ValueError("Caso inexistente ou já finalizado.")
                if self.connection.execute("SELECT 1 FROM case_runs cr JOIN runs r ON r.id=cr.run_id WHERE cr.case_id=? AND r.ended_at IS NULL", (case_id,)).fetchone():
                    raise ValueError("O caso já possui uma gravação aberta.")
            self.connection.execute("INSERT INTO runs(id,started_at,session_json) VALUES(?,?,?)",
                                    (run_id, utc_now(), encode(session)))
            self.connection.execute("INSERT INTO run_metadata VALUES(?,?)", (run_id, encode({
                "recorder_version": 2, "parameter_capture_version": 2,
                "structural_fingerprint_version": 2, "content_fingerprint_version": 4,
                **(metadata or {})})))
            if case_id is not None:
                self.connection.execute("INSERT INTO case_runs VALUES(?,?)", (case_id, run_id))
        return run_id

    def _timeline(self, run_id, kind, record_id, timestamp):
        self.connection.execute("""INSERT INTO capture_timeline
            SELECT ?,COALESCE(MAX(sequence),0)+1,?,?,? FROM capture_timeline WHERE run_id=?""",
            (run_id, kind, record_id, timestamp, run_id))

    def create_case(self, name, process, inputs, policy):
        from .value_capture import capture_value
        from .object_tree import SapTextContext
        if not isinstance(name, str) or not name.strip() or not isinstance(process, str) or not process.strip():
            raise ValueError("Nome e processo do caso são obrigatórios.")
        if not isinstance(inputs, dict) or any(not isinstance(key, str) for key in inputs):
            raise ValueError("inputs deve ser um objeto JSON.")
        safe, quality = {}, {}
        # Input names participate in field-based sanitization (installation, CPF, etc.).
        for key, value in inputs.items():
            safe[key], quality[key] = capture_value(value, policy, SapTextContext("CaseInput", key, None, key))
        name, _ = capture_value(name, policy, SapTextContext("CaseInput", "case.name", None, "name"))
        process, _ = capture_value(process, policy, SapTextContext("CaseInput", "case.process", None, "process"))
        if not name or not process:
            raise ValueError("Nome/processo indisponível após sanitização.")
        case_id = new_id()
        with self.connection:
            self.connection.execute("INSERT INTO cases VALUES(?,?,?,?,NULL,'inconclusive',?,?,?,NULL)",
                                    (case_id, name, process, utc_now(), encode(safe), encode(quality), encode(asdict(policy))))
        return case_id

    def case_policy(self, case_id):
        from .sanitization import SanitizationPolicy
        row = self.connection.execute("SELECT policy_json FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise ValueError("Caso não encontrado.")
        return SanitizationPolicy(**json.loads(row[0]))

    def list_cases(self):
        return [dict(row) for row in self.connection.execute(
            "SELECT id,name,process,started_at,ended_at,outcome FROM cases ORDER BY rowid DESC")]

    def mark_case(self, case_id, label, note=None, *, observation_id=None):
        from .object_tree import SapTextContext
        policy = self.case_policy(case_id)
        if not isinstance(label, str) or not label.strip():
            raise ValueError("Informe o nome do marco.")
        label = policy(label, SapTextContext("CaseInput", "case.label", None, "label"))
        note = None if note is None else policy(note, SapTextContext("CaseInput", "case.note", None, "note"))
        with self.connection:
            # This is the last persisted observation, not a simultaneous SAP capture.
            if observation_id is not None:
                row = self.connection.execute("""SELECT o.run_id,o.id FROM observations o
                    JOIN case_runs cr ON cr.run_id=o.run_id WHERE cr.case_id=? AND o.id=?""",
                    (case_id, observation_id)).fetchone()
                if row is None:
                    raise ValueError("Observação não pertence ao caso selecionado.")
            else:
                row = self.connection.execute("""SELECT o.run_id,o.id FROM observations o
                    JOIN case_runs cr ON cr.run_id=o.run_id WHERE cr.case_id=? ORDER BY o.rowid DESC LIMIT 1""", (case_id,)).fetchone()
            self.connection.execute("INSERT INTO case_marks VALUES(?,?,?,?,?,?,?)",
                (new_id(), case_id, utc_now(), label, note, None if row is None else row[0], None if row is None else row[1]))

    def finish_case(self, case_id, outcome, evidence):
        from .object_tree import SapTextContext
        if outcome not in {"confirmed", "failed", "cancelled", "inconclusive"}:
            raise ValueError("Resultado de caso inválido.")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("Descreva a evidência ou motivo do resultado.")
        evidence = self.case_policy(case_id)(evidence, SapTextContext("CaseInput", "case.evidence", None, "evidence"))
        with self.connection:
            self.connection.execute("UPDATE cases SET outcome=outcome WHERE id=?", (case_id,))
            if self.connection.execute("SELECT 1 FROM case_runs cr JOIN runs r ON r.id=cr.run_id WHERE cr.case_id=? AND r.ended_at IS NULL", (case_id,)).fetchone():
                raise ValueError("Encerre a gravação do caso antes de finalizá-lo.")
            self.connection.execute("UPDATE cases SET ended_at=?,outcome=?,evidence=? WHERE id=?", (utc_now(), outcome, evidence, case_id))

    def set_capabilities(self, run_id: str, capabilities: object) -> None:
        with self.connection:
            self.connection.execute("UPDATE runs SET capabilities_json=? WHERE id=?",
                                    (encode(asdict(capabilities)), run_id))

    def finish_run(self, run_id: str, reason: str) -> None:
        with self.connection:
            self.connection.execute("UPDATE runs SET ended_at=?,stop_reason=? WHERE id=?",
                                    (utc_now(), reason, run_id))

    def save_observation(self, run_id: str, observation: SapScreenObservation) -> None:
        with self.connection:
            self._save_observation(run_id, observation)

    def _save_observation(self, run_id: str, observation: SapScreenObservation) -> None:
        if self.connection.execute("SELECT 1 FROM observations WHERE id=?", (observation.id,)).fetchone():
            return
        fingerprint = observation.fingerprint
        self.connection.execute("""INSERT INTO screens VALUES(?,?,?,?,1)
            ON CONFLICT(structural_hash) DO UPDATE SET
            last_seen=MAX(last_seen,excluded.last_seen), count=count+1""",
            (fingerprint.structural_hash, encode(build_structural_payload(observation.state)),
             observation.timestamp, observation.timestamp))
        self.connection.execute("INSERT INTO observations VALUES(?,?,?,?,?,?)",
                                (observation.id, run_id, fingerprint.structural_hash,
                                 fingerprint.content_hash, observation.timestamp, encode(observation.state.to_dict())))
        self._timeline(run_id, "observation", observation.id, observation.timestamp)
        previous_main = None
        if observation.state.has_additional_windows:
            row = self.connection.execute("""SELECT id,timestamp FROM observations
                WHERE run_id=? AND id!=? AND json_extract(state_json,'$.active_window_type')='GuiMainWindow'
                ORDER BY rowid DESC LIMIT 1""", (run_id, observation.id)).fetchone()
            if row:
                previous_main = {"observation_id": row[0], "timestamp": row[1], "basis": "previous_observation"}
        self.connection.execute("INSERT INTO observation_metadata VALUES(?,?)", (observation.id, encode({
            "quality_version": 2, "previous_main_window": previous_main,
            "tree_available": observation.state.ui_tree is not None})))

    def save_event(self, run_id: str, event: SapActionEvent,
                   before: SapScreenObservation | None) -> str:
        from .transition_recorder import ACTION_EVENTS
        event_id = new_id()
        with self.connection:
            self.connection.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                                    (event_id, run_id, None if before is None else before.id,
                                     event.timestamp.isoformat(), event.event_type,
                                     int(event.event_type in ACTION_EVENTS), event.to_json()))
            self._timeline(run_id, "event", event_id, event.timestamp.isoformat())
        return event_id

    def save_transition(self, run_id: str, transition: SapTransition) -> None:
        with self.connection:
            if self.connection.execute("SELECT 1 FROM transition_observations WHERE id=?", (transition.id,)).fetchone():
                return
            before, after = transition.before_state, transition.after_state
            if before is not None:
                self._save_observation(run_id, before)
            if after is not None:
                self._save_observation(run_id, after)
            transition_id = None
            if before is not None and after is not None and transition.status == "complete":
                signature = action_signature(transition.actions)
                from_hash, to_hash = before.fingerprint.structural_hash, after.fingerprint.structural_hash
                transition_id = hashlib.sha256(encode([from_hash, signature, to_hash]).encode("utf-8")).hexdigest()
                self.connection.execute("""INSERT INTO transitions VALUES(?,?,?,?,1,?,?)
                    ON CONFLICT(id) DO UPDATE SET count=count+1,
                    last_seen=MAX(last_seen,excluded.last_seen)""",
                    (transition_id, from_hash, signature, to_hash, transition.timestamp, transition.timestamp))
            self.connection.execute("INSERT INTO transition_observations VALUES(?,?,?,?,?,?,?,?,?)",
                                    (transition.id, run_id, transition_id,
                                     None if before is None else before.id, None if after is None else after.id,
                                     transition.timestamp, transition.status, transition.reason, encode(transition.action_ids)))
            self._timeline(run_id, "transition", transition.id, transition.timestamp)
            if transition.batch is not None:
                self.connection.execute("INSERT INTO request_batches VALUES(?,?,?,?)",
                    (transition.batch["id"], run_id, transition.id, encode(transition.batch)))

    def stats(self) -> dict[str, int]:
        result = {key: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for key, table in (("screens", "screens"), ("observations", "observations"),
                                     ("transitions", "transitions"), ("events", "events"), ("runs", "runs"))}
        result["actions"] = self.connection.execute("SELECT COUNT(*) FROM events WHERE is_action=1").fetchone()[0]
        result["incomplete_transitions"] = self.connection.execute(
            "SELECT COUNT(*) FROM transition_observations WHERE status!='complete'").fetchone()[0]
        result["unfinished_runs"] = self.connection.execute("SELECT COUNT(*) FROM runs WHERE ended_at IS NULL").fetchone()[0]
        result["new_states_last_run"] = self.connection.execute("""
            SELECT COUNT(DISTINCT o.structural_hash) FROM observations o
            WHERE o.run_id=(SELECT id FROM runs ORDER BY started_at DESC LIMIT 1)
              AND NOT EXISTS (SELECT 1 FROM observations p JOIN runs r ON r.id=p.run_id
                WHERE p.structural_hash=o.structural_hash
                  AND r.started_at<(SELECT started_at FROM runs WHERE id=o.run_id))
        """).fetchone()[0]
        result["cases"] = self.connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
        result["request_batches"] = self.connection.execute("SELECT COUNT(*) FROM request_batches").fetchone()[0]
        result["capture_errors"] = self.connection.execute("SELECT COUNT(*) FROM events WHERE event_type='CaptureError'").fetchone()[0]
        result["legacy_runs"] = self.connection.execute("SELECT COUNT(*) FROM runs r LEFT JOIN run_metadata m ON r.id=m.run_id WHERE m.run_id IS NULL").fetchone()[0]
        return result

    def export(self) -> dict[str, Any]:
        result: dict[str, Any] = {"schema_version": 2, "stats": self.stats(), "legacy_note": "Registros sem metadados de captura têm qualidade e ordem global desconhecidas."}
        for key, table in (("states", "screens"), ("observations", "observations"), ("actions", "events"),
                           ("transitions", "transitions"), ("history", "transition_observations"), ("runs", "runs"),
                           ("timeline", "capture_timeline"), ("run_metadata", "run_metadata"),
                           ("observation_metadata", "observation_metadata"), ("request_batches", "request_batches"),
                           ("cases", "cases"), ("case_runs", "case_runs"), ("case_marks", "case_marks")):
            rows = []
            for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY rowid"):
                converted = {}
                for name, value in dict(row).items():
                    converted[name.removesuffix("_json")] = json.loads(value) if name.endswith("_json") and value is not None else value
                rows.append(converted)
            result[key] = rows
        return result


def write_json(path: str | Path, data: object) -> None:
    """Troca atômica do arquivo exportado, preservando o anterior se falhar."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + new_id() + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
