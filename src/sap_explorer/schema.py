"""Migração aditiva; o histórico legado permanece intacto."""
from pathlib import Path
from contextlib import closing
import sqlite3
from uuid import uuid4


SCHEMA_VERSION = 1


def migrate(connection, path, *, existing=False):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError("Banco criado por versão mais recente do gravador.")
    if version == SCHEMA_VERSION:
        return None
    backup_path = None
    if existing:
        backup_path = Path(path).with_name(Path(path).name + f".pre-v1.{uuid4().hex}.sqlite3")
        with closing(sqlite3.connect(backup_path)) as backup:
            connection.backup(backup)
    try:
        connection.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE capture_timeline (
                run_id TEXT NOT NULL REFERENCES runs(id), sequence INTEGER NOT NULL,
                kind TEXT NOT NULL, record_id TEXT NOT NULL, timestamp TEXT NOT NULL,
                PRIMARY KEY(run_id,sequence), UNIQUE(kind,record_id));
            CREATE TABLE run_metadata (
                run_id TEXT PRIMARY KEY REFERENCES runs(id), metadata_json TEXT NOT NULL);
            CREATE TABLE observation_metadata (
                observation_id TEXT PRIMARY KEY REFERENCES observations(id), metadata_json TEXT NOT NULL);
            CREATE TABLE request_batches (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                transition_observation TEXT UNIQUE NOT NULL REFERENCES transition_observations(id),
                batch_json TEXT NOT NULL);
            CREATE TABLE cases (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, process TEXT NOT NULL,
                started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT NOT NULL,
                inputs_json TEXT NOT NULL, input_quality_json TEXT NOT NULL,
                policy_json TEXT NOT NULL, evidence TEXT);
            CREATE TABLE case_runs (
                case_id TEXT NOT NULL REFERENCES cases(id), run_id TEXT PRIMARY KEY REFERENCES runs(id));
            CREATE TABLE case_marks (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                timestamp TEXT NOT NULL, label TEXT NOT NULL, note TEXT,
                run_id TEXT REFERENCES runs(id), observation_id TEXT REFERENCES observations(id));
            CREATE INDEX case_runs_case ON case_runs(case_id);
            PRAGMA user_version=1;
            COMMIT;
        """)
    except BaseException:
        connection.rollback()
        raise
    return backup_path
