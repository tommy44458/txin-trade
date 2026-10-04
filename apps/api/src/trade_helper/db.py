"""Embedded SQLite storage shared by the API and local worker processes.

Each short write transaction reserves the writer before reading shared state.
This makes a worker's read-and-claim operation atomic across processes without
requiring a database server. WAL readers remain independent of that writer.
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

SCHEMA_VERSION = 11
BUSY_TIMEOUT_MS = 15_000


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def database_path() -> Path:
    explicit = os.environ.get("APP_DB_PATH")
    if explicit:
        return Path(explicit).expanduser().resolve()
    directory = os.environ.get("APP_DATA_DIR")
    if directory:
        return Path(directory).expanduser().resolve() / "trade_helper.sqlite3"
    return Path(__file__).resolve().parents[4] / "data" / "trade_helper.sqlite3"


class Database:
    """The existing database API, using native SQLite SQL and parameters."""

    def __init__(self, connection: sqlite3.Connection, *, readonly: bool = False):
        self.connection = connection
        self.readonly = readonly

    def execute(self, statement: str, params: tuple | list = ()):
        if not self.connection.in_transaction:
            self.connection.execute("BEGIN" if self.readonly else "BEGIN IMMEDIATE")
        bound = tuple(
            value.replace(tzinfo=UTC).isoformat(timespec="microseconds")
            if isinstance(value, datetime) and value.tzinfo is None
            else value.astimezone(UTC).isoformat(timespec="microseconds")
            if isinstance(value, datetime) else value
            for value in params
        )
        return self.connection.execute(statement, bound)

    def executescript(self, statements: str) -> None:
        # sqlite3.executescript commits an open transaction before executing.
        # Execute complete statements separately so migrations stay atomic.
        pending = ""
        for character in statements:
            pending += character
            if character == ";" and sqlite3.complete_statement(pending):
                self.execute(pending)
                pending = ""
        if pending.strip():
            self.execute(pending)

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()


def _dict_row(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return dict(zip((column[0] for column in cursor.description), row, strict=True))


def _open_connection(*, readonly: bool = False) -> sqlite3.Connection:
    path = database_path()
    if readonly:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro", uri=True,
            timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existed = path.exists()
        connection = sqlite3.connect(
            path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None,
        )
        if not existed and os.name == "posix":
            path.chmod(0o600)
    connection.row_factory = _dict_row
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys=ON")
    if readonly:
        connection.execute("PRAGMA query_only=ON")
    else:
        # FULL sync keeps committed reports durable through a power failure.
        connection.execute("PRAGMA synchronous=FULL")
    return connection


@contextmanager
def connect(*, readonly: bool = False):
    connection = _open_connection(readonly=readonly)
    try:
        connection.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
        yield Database(connection, readonly=readonly)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


_INITIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL, request_json TEXT NOT NULL,
    positions_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL, phase TEXT NOT NULL, report_json TEXT,
    snapshot_json TEXT, shadow_json TEXT,
    error_code TEXT, error_message TEXT, created_at TEXT NOT NULL,
    started_at TEXT, completed_at TEXT, lease_until TEXT,
    v4_status TEXT, v4_json TEXT, v4_lease_until TEXT,
    UNIQUE(user_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS analyses_queue ON analyses(status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS analyses_id_user_unique ON analyses(id,user_id);
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, market_id TEXT NOT NULL,
    version INTEGER NOT NULL, side TEXT NOT NULL,
    leverage INTEGER NOT NULL DEFAULT 1, margin_mode TEXT NOT NULL DEFAULT 'isolated',
    entry_price TEXT NOT NULL, quantity TEXT NOT NULL,
    stop_loss TEXT, take_profit TEXT, previous_stop_loss TEXT, status TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    entry_time TEXT, exchange_liquidation_price TEXT, notes TEXT,
    source TEXT NOT NULL DEFAULT 'manual', external_position_id TEXT,
    exchange_symbol TEXT, contract_type TEXT, synced_at TEXT
);
CREATE INDEX IF NOT EXISTS positions_user ON positions(user_id, status);
CREATE TABLE IF NOT EXISTS market_candles_v4 (
    market TEXT NOT NULL, timeframe TEXT NOT NULL, open_ms INTEGER NOT NULL,
    payload TEXT NOT NULL, sha256 TEXT NOT NULL, received_at TEXT NOT NULL,
    PRIMARY KEY(market,timeframe,open_ms)
);
CREATE TABLE IF NOT EXISTS zone_states_v4 (
    market TEXT NOT NULL, timeframe TEXT NOT NULL, config_hash TEXT NOT NULL,
    payload TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(market,timeframe,config_hash)
);
CREATE TABLE IF NOT EXISTS zone_events_v4 (
    id TEXT PRIMARY KEY, market TEXT NOT NULL, timeframe TEXT NOT NULL,
    payload TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news_documents (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, source_url TEXT NOT NULL,
    title TEXT NOT NULL, body TEXT NOT NULL, published_at TEXT,
    ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f000+00:00','now')),
    market_ids TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(market_ids)),
    metadata TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata)),
    content_hash TEXT NOT NULL,
    embedding TEXT, embedding_model TEXT, embedded_at TEXT,
    UNIQUE(source, source_url, content_hash),
    CHECK ((embedding IS NULL AND embedding_model IS NULL AND embedded_at IS NULL)
        OR (embedding IS NOT NULL AND embedding_model IS NOT NULL AND embedded_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS news_documents_published ON news_documents(published_at DESC);
CREATE INDEX IF NOT EXISTS news_documents_ingested ON news_documents(ingested_at DESC);
CREATE TABLE IF NOT EXISTS news_classifications (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES news_documents(id) ON DELETE CASCADE,
    question_version TEXT NOT NULL, input_hash TEXT NOT NULL,
    model TEXT NOT NULL, answers TEXT NOT NULL CHECK (json_valid(answers)),
    usage TEXT NOT NULL CHECK (json_valid(usage)),
    status TEXT NOT NULL CHECK (status = 'classified_unreviewed'),
    classified_at TEXT NOT NULL,
    UNIQUE(document_id, question_version, input_hash, model)
);
CREATE INDEX IF NOT EXISTS news_classifications_document_time
    ON news_classifications(document_id, classified_at DESC);
CREATE TABLE IF NOT EXISTS news_body_fetches (
    source_url TEXT PRIMARY KEY, status TEXT NOT NULL,
    checked_at TEXT NOT NULL, next_check_at TEXT NOT NULL,
    error_code TEXT
);
CREATE TABLE IF NOT EXISTS news_classification_jobs (
    document_id TEXT NOT NULL REFERENCES news_documents(id) ON DELETE CASCADE,
    question_version TEXT NOT NULL, model_alias TEXT NOT NULL,
    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL, lease_until TEXT,
    error_code TEXT, updated_at TEXT NOT NULL,
    PRIMARY KEY(document_id,question_version,model_alias)
);
CREATE INDEX IF NOT EXISTS news_classification_jobs_queue
    ON news_classification_jobs(status,next_attempt_at);
CREATE TABLE IF NOT EXISTS news_jev_calls (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES news_documents(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL, error_code TEXT,
    input_tokens INTEGER, output_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS news_jev_calls_day ON news_jev_calls(started_at DESC);
CREATE TABLE IF NOT EXISTS news_source_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, status TEXT NOT NULL,
    checked_at TEXT NOT NULL, error_code TEXT
);
CREATE INDEX IF NOT EXISTS news_source_checks_time
    ON news_source_checks(source,checked_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS analysis_embeddings (
    analysis_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL, summary TEXT NOT NULL, content_hash TEXT NOT NULL,
    embedding TEXT, embedding_model TEXT, embedded_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f000+00:00','now')),
    CHECK ((embedding IS NULL AND embedding_model IS NULL AND embedded_at IS NULL)
        OR (embedding IS NOT NULL AND embedding_model IS NOT NULL AND embedded_at IS NOT NULL)),
    FOREIGN KEY (analysis_id,user_id) REFERENCES analyses(id,user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS analysis_embeddings_owner ON analysis_embeddings(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS event_sources (
    source TEXT PRIMARY KEY, status TEXT NOT NULL,
    checked_at TEXT, last_success_at TEXT,
    error_code TEXT
);
CREATE TABLE IF NOT EXISTS economic_events (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, source_uid TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f000+00:00','now')),
    UNIQUE(source,source_uid)
);
CREATE TABLE IF NOT EXISTS economic_event_versions (
    event_id TEXT NOT NULL REFERENCES economic_events(id) ON DELETE CASCADE,
    version INTEGER NOT NULL, ingested_at TEXT NOT NULL,
    content_hash TEXT NOT NULL, payload TEXT NOT NULL CHECK (json_valid(payload)),
    PRIMARY KEY(event_id,version)
);
CREATE INDEX IF NOT EXISTS economic_event_versions_time ON economic_event_versions(ingested_at);
CREATE TABLE IF NOT EXISTS macro_actual_versions (
    source TEXT NOT NULL, metric TEXT NOT NULL, period TEXT NOT NULL,
    version INTEGER NOT NULL, ingested_at TEXT NOT NULL,
    content_hash TEXT NOT NULL, payload TEXT NOT NULL CHECK (json_valid(payload)),
    PRIMARY KEY(source,metric,period,version)
);
CREATE INDEX IF NOT EXISTS macro_actual_versions_time
    ON macro_actual_versions(ingested_at DESC);
CREATE TABLE IF NOT EXISTS macro_actual_source_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, status TEXT NOT NULL,
    checked_at TEXT NOT NULL, error_code TEXT
);
CREATE INDEX IF NOT EXISTS macro_actual_source_checks_time
    ON macro_actual_source_checks(source,checked_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS consensus_provider_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL CHECK (provider = 'jblanked'),
    reserved_at TEXT NOT NULL, next_allowed_at TEXT NOT NULL,
    result TEXT NOT NULL CHECK (json_valid(result)),
    CHECK (julianday(next_allowed_at) >= julianday(reserved_at) + 1.0)
);
CREATE INDEX IF NOT EXISTS consensus_provider_checks_latest
    ON consensus_provider_checks(provider,reserved_at DESC,id DESC);
"""


def _migration_1(db: Database) -> None:
    db.executescript(_INITIAL_SCHEMA)
    # A pre-PostgreSQL SQLite file may have the original positions table.
    # Upgrade it in the same transaction without replacing existing rows.
    existing = {row["name"] for row in db.execute("PRAGMA table_info(positions)")}
    columns = {
        "previous_stop_loss": "TEXT", "entry_time": "TEXT",
        "exchange_liquidation_price": "TEXT", "notes": "TEXT",
        "source": "TEXT NOT NULL DEFAULT 'manual'", "external_position_id": "TEXT",
        "exchange_symbol": "TEXT", "contract_type": "TEXT", "synced_at": "TEXT",
    }
    for name, definition in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE positions ADD COLUMN {name} {definition}")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS positions_bingx_identity
                  ON positions(user_id,source,external_position_id)
                  WHERE external_position_id IS NOT NULL""")


def _migration_2(db: Database) -> None:
    db.executescript("""
    CREATE TABLE app_preferences (
        user_id TEXT PRIMARY KEY,
        value_json TEXT NOT NULL CHECK(json_valid(value_json)),
        updated_at TEXT NOT NULL
    );
    CREATE TABLE auth_metadata (
        provider TEXT PRIMARY KEY,
        value_json TEXT NOT NULL CHECK(json_valid(value_json)),
        updated_at TEXT NOT NULL
    );
    CREATE TABLE credential_key_metadata (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), key_id TEXT NOT NULL
    );
    CREATE TABLE credential_records (
        name TEXT PRIMARY KEY, key_id TEXT NOT NULL,
        nonce BLOB NOT NULL CHECK(length(nonce)=12),
        ciphertext BLOB NOT NULL CHECK(length(ciphertext)>=16),
        version INTEGER NOT NULL DEFAULT 1 CHECK(version=1), updated_at TEXT NOT NULL
    );
    """)


def _migration_3(db: Database) -> None:
    db.executescript("""
    CREATE TABLE macro_interpretations (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        evidence_version TEXT NOT NULL, prompt_version TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
        status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','unconfigured')),
        attempts INTEGER NOT NULL DEFAULT 1 CHECK(attempts > 0),
        claim_token TEXT, lease_until TEXT,
        result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
        execution_json TEXT CHECK(execution_json IS NULL OR json_valid(execution_json)),
        error_code TEXT, error_message TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, generated_at TEXT,
        UNIQUE(user_id,evidence_version,prompt_version,fingerprint)
    );
    CREATE INDEX macro_interpretations_latest
        ON macro_interpretations(user_id,prompt_version,status,generated_at DESC);
    """)


def _migration_4(db: Database) -> None:
    db.executescript("""
    CREATE UNIQUE INDEX macro_interpretations_id_user_unique
        ON macro_interpretations(id,user_id);
    CREATE TABLE discussion_sessions (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        subject_type TEXT NOT NULL CHECK(subject_type IN ('analysis','macro')),
        subject_id TEXT NOT NULL, analysis_id TEXT, macro_id TEXT,
        subject_json TEXT NOT NULL CHECK(json_valid(subject_json)),
        context_json TEXT NOT NULL CHECK(json_valid(context_json)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(user_id,subject_type,subject_id),
        CHECK((subject_type='analysis' AND analysis_id=subject_id AND macro_id IS NULL)
           OR (subject_type='macro' AND macro_id=subject_id AND analysis_id IS NULL)),
        FOREIGN KEY(analysis_id,user_id) REFERENCES analyses(id,user_id) ON DELETE CASCADE,
        FOREIGN KEY(macro_id,user_id) REFERENCES macro_interpretations(id,user_id)
            ON DELETE CASCADE
    );
    CREATE TABLE discussion_messages (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES discussion_sessions(id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL CHECK(sequence>0),
        role TEXT NOT NULL CHECK(role IN ('user','assistant')),
        content TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed')),
        request_id TEXT, reply_to_id TEXT REFERENCES discussion_messages(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL, completed_at TEXT,
        provider TEXT, model TEXT, error_code TEXT, error_message TEXT,
        error_retryable INTEGER CHECK(error_retryable IN (0,1)),
        claim_token TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        UNIQUE(session_id,sequence), UNIQUE(session_id,request_id),
        CHECK((role='user' AND status='completed' AND request_id IS NOT NULL
            AND reply_to_id IS NULL)
           OR (role='assistant' AND request_id IS NULL AND reply_to_id IS NOT NULL))
    );
    CREATE UNIQUE INDEX discussion_messages_one_inflight
        ON discussion_messages(session_id) WHERE status IN ('queued','running');
    CREATE INDEX discussion_messages_queue ON discussion_messages(status,created_at);
    """)



def _migration_5(db: Database) -> None:
    # Keep old ciphertext and its key identity byte-for-byte. The application
    # does not try to unlock or copy the retired external credential store.
    db.executescript("""
    ALTER TABLE credential_records RENAME TO credential_legacy_records;
    ALTER TABLE credential_key_metadata RENAME TO credential_legacy_key_metadata;
    CREATE TABLE credential_local_keys (
        key_id TEXT PRIMARY KEY, master_key BLOB NOT NULL CHECK(length(master_key)=32),
        created_at TEXT NOT NULL
    );
    CREATE TABLE credential_records (
        name TEXT PRIMARY KEY, key_id TEXT NOT NULL REFERENCES credential_local_keys(key_id),
        nonce BLOB NOT NULL CHECK(length(nonce)=12),
        ciphertext BLOB NOT NULL CHECK(length(ciphertext)>=16),
        version INTEGER NOT NULL DEFAULT 2 CHECK(version=2), updated_at TEXT NOT NULL
    );
    DELETE FROM auth_metadata WHERE provider='chatgpt'
        AND EXISTS(SELECT 1 FROM credential_legacy_records WHERE name='chatgpt');
    DELETE FROM auth_metadata WHERE provider='codex'
        AND EXISTS(SELECT 1 FROM app_preferences
            WHERE json_extract(value_json,'$.codex_auth_scope')='application');
    """)


def _migration_6(db: Database) -> None:
    # Instructions are immutable job inputs, rather than a hash of a template
    # that might disappear when the user installs the next application release.
    db.executescript("""
    CREATE TABLE IF NOT EXISTS prompt_artifacts (
        id TEXT PRIMARY KEY, rendered_sha256 TEXT NOT NULL UNIQUE,
        task TEXT NOT NULL, prompt_locale TEXT NOT NULL, response_locale TEXT NOT NULL,
        policy_version TEXT NOT NULL, translation_version TEXT NOT NULL,
        schema_version TEXT NOT NULL, template_sha256 TEXT NOT NULL,
        instructions TEXT NOT NULL,
        metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
        created_at TEXT NOT NULL
    );
    CREATE TRIGGER IF NOT EXISTS prompt_artifacts_immutable BEFORE UPDATE ON prompt_artifacts
        BEGIN SELECT RAISE(ABORT,'Prompt artifacts are immutable'); END;
    CREATE TABLE IF NOT EXISTS macro_interpretation_views (
        id TEXT PRIMARY KEY,
        interpretation_id TEXT NOT NULL, user_id TEXT NOT NULL,
        response_locale TEXT NOT NULL CHECK(response_locale IN ('zh-TW','en-US')),
        render_version TEXT NOT NULL, source_result_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        execution_json TEXT CHECK(execution_json IS NULL OR json_valid(execution_json)),
        prompt_artifact_id TEXT REFERENCES prompt_artifacts(id), created_at TEXT NOT NULL,
        UNIQUE(interpretation_id,response_locale,render_version),
        FOREIGN KEY(interpretation_id,user_id) REFERENCES macro_interpretations(id,user_id)
            ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS macro_translation_jobs (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, interpretation_id TEXT NOT NULL,
        response_locale TEXT NOT NULL CHECK(response_locale IN ('zh-TW','en-US')),
        render_version TEXT NOT NULL, source_result_sha256 TEXT NOT NULL,
        source_result_json TEXT NOT NULL CHECK(json_valid(source_result_json)),
        prompt_artifact_id TEXT REFERENCES prompt_artifacts(id),
        prompt_bundle_json TEXT CHECK(prompt_bundle_json IS NULL OR json_valid(prompt_bundle_json)),
        status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
        attempts INTEGER NOT NULL DEFAULT 0, claim_token TEXT, lease_until TEXT,
        error_code TEXT, error_message TEXT,
        error_retryable INTEGER CHECK(error_retryable IS NULL OR error_retryable IN (0,1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT,
        UNIQUE(interpretation_id,response_locale,render_version),
        FOREIGN KEY(interpretation_id,user_id) REFERENCES macro_interpretations(id,user_id)
            ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS macro_translation_jobs_queue ON macro_translation_jobs(status,created_at);
    """)

    columns = {
        "analyses": {"prompt_artifact_id": "TEXT REFERENCES prompt_artifacts(id)"},
        "discussion_sessions": {"output_locale": "TEXT NOT NULL DEFAULT 'zh-TW' "
                                 "CHECK(output_locale IN ('zh-TW','en-US'))"},
        "discussion_messages": {
            "output_locale": "TEXT NOT NULL DEFAULT 'zh-TW' "
                             "CHECK(output_locale IN ('zh-TW','en-US'))",
            "prompt_artifact_id": "TEXT REFERENCES prompt_artifacts(id)",
            "prompt_bundle_json": "TEXT CHECK(prompt_bundle_json IS NULL "
                                  "OR json_valid(prompt_bundle_json))",
        },
        "macro_interpretations": {
            "output_locale": "TEXT NOT NULL DEFAULT 'zh-TW' "
                             "CHECK(output_locale IN ('zh-TW','en-US'))",
            "prompt_artifact_id": "TEXT REFERENCES prompt_artifacts(id)",
            "prompt_bundle_json": "TEXT CHECK(prompt_bundle_json IS NULL "
                                  "OR json_valid(prompt_bundle_json))",
        },
    }
    for table, definitions in columns.items():
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, definition in definitions.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migration_7(db: Database) -> None:
    # Exchange estimates change with price; they do not change the position
    # facts/version used to determine whether a saved analysis is still current.
    existing = {row["name"] for row in db.execute("PRAGMA table_info(positions)")}
    if "account_scope" not in existing:
        db.execute("ALTER TABLE positions ADD COLUMN account_scope TEXT")
    db.executescript("""
    CREATE INDEX IF NOT EXISTS positions_exchange_scope
        ON positions(user_id,source,account_scope,status);
    CREATE TABLE IF NOT EXISTS position_exchange_estimates (
        position_id TEXT PRIMARY KEY REFERENCES positions(id) ON DELETE CASCADE,
        provider TEXT NOT NULL, account_scope TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS integration_sync_state (
        user_id TEXT NOT NULL, provider TEXT NOT NULL, account_scope TEXT NOT NULL,
        summary_json TEXT NOT NULL CHECK(json_valid(summary_json)),
        last_success_at TEXT NOT NULL,
        PRIMARY KEY(user_id,provider,account_scope)
    );
    """)


def _migration_8(db: Database) -> None:
    existing = {row["name"] for row in db.execute("PRAGMA table_info(discussion_messages)")}
    if "live_market_json" not in existing:
        db.execute("""ALTER TABLE discussion_messages ADD COLUMN live_market_json TEXT
            CHECK(live_market_json IS NULL OR json_valid(live_market_json))""")


def _migration_9(db: Database) -> None:
    # The direct ChatGPT provider was removed. Delete its app-managed OAuth
    # tokens and display cache; retired legacy ciphertext stays untouched.
    db.executescript("""
    DELETE FROM credential_records WHERE name='chatgpt';
    DELETE FROM auth_metadata WHERE provider='chatgpt';
    UPDATE app_preferences SET value_json=json_remove(value_json,'$.model_provider','$.models.chatgpt')
        WHERE json_extract(value_json,'$.model_provider')='chatgpt';
    UPDATE app_preferences SET value_json=json_remove(value_json,'$.models.chatgpt')
        WHERE json_type(value_json,'$.models.chatgpt') IS NOT NULL;
    """)


def _migration_10(db: Database) -> None:
    # Follow-up questions on the fund-flows page: each conversation is anchored on
    # the fund-flow data frozen when it began. SQLite cannot change a CHECK in place,
    # so discussion_sessions is rebuilt; foreign keys are off during migrations so its
    # messages stay attached, and the rebuilt tables' references are checked here.
    db.execute("""CREATE TABLE IF NOT EXISTS fund_flow_snapshots (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        asset TEXT NOT NULL, flow_window TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        created_at TEXT NOT NULL,
        UNIQUE(id,user_id)
    )""")
    current = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='discussion_sessions'",
    ).fetchone()["sql"]
    if "fund_flows" in current:
        return
    db.executescript("""
    CREATE TABLE discussion_sessions_v10 (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        subject_type TEXT NOT NULL CHECK(subject_type IN ('analysis','macro','fund_flows')),
        subject_id TEXT NOT NULL, analysis_id TEXT, macro_id TEXT, fund_flow_id TEXT,
        subject_json TEXT NOT NULL CHECK(json_valid(subject_json)),
        context_json TEXT NOT NULL CHECK(json_valid(context_json)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        output_locale TEXT NOT NULL DEFAULT 'zh-TW' CHECK(output_locale IN ('zh-TW','en-US')),
        UNIQUE(user_id,subject_type,subject_id),
        CHECK((subject_type='analysis' AND analysis_id=subject_id AND macro_id IS NULL AND fund_flow_id IS NULL)
           OR (subject_type='macro' AND macro_id=subject_id AND analysis_id IS NULL AND fund_flow_id IS NULL)
           OR (subject_type='fund_flows' AND fund_flow_id=subject_id AND analysis_id IS NULL AND macro_id IS NULL)),
        FOREIGN KEY(analysis_id,user_id) REFERENCES analyses(id,user_id) ON DELETE CASCADE,
        FOREIGN KEY(macro_id,user_id) REFERENCES macro_interpretations(id,user_id)
            ON DELETE CASCADE,
        FOREIGN KEY(fund_flow_id,user_id) REFERENCES fund_flow_snapshots(id,user_id)
            ON DELETE CASCADE
    );
    INSERT INTO discussion_sessions_v10
        (id,user_id,subject_type,subject_id,analysis_id,macro_id,subject_json,context_json,
         created_at,updated_at,output_locale)
        SELECT id,user_id,subject_type,subject_id,analysis_id,macro_id,subject_json,context_json,
               created_at,updated_at,output_locale FROM discussion_sessions;
    DROP TABLE discussion_sessions;
    ALTER TABLE discussion_sessions_v10 RENAME TO discussion_sessions;
    """)
    for table in ("discussion_sessions", "discussion_messages"):
        if db.execute(f"PRAGMA foreign_key_check({table})").fetchone() is not None:
            raise RuntimeError("Rebuilding discussion sessions broke a reference")


def _migration_11(db: Database) -> None:
    # A running analysis shows each finished step (quote, levels, the AI's sections
    # as written) before its report is complete. Display-only; NULL for older rows.
    columns = {row["name"] for row in db.execute("PRAGMA table_info(analyses)").fetchall()}
    if "progress_json" not in columns:
        db.execute("ALTER TABLE analyses ADD COLUMN progress_json TEXT "
                   "CHECK(progress_json IS NULL OR json_valid(progress_json))")


class DatabaseFromNewerVersion(RuntimeError):
    """The database was upgraded by a newer txinTrade; this version must not touch it."""


def init_db() -> None:
    connection = _open_connection()
    try:
        _enable_wal(connection)
        # Rebuilding a table must not cascade into its children; a migration that
        # rebuilds one checks its references (SQLite's schema-change procedure).
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()["user_version"]
        if version > SCHEMA_VERSION:
            raise DatabaseFromNewerVersion("Local database requires a newer application version")
        db = Database(connection)
        db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
        )""")
        for migration_version, migration in (
            (1, _migration_1), (2, _migration_2), (3, _migration_3), (4, _migration_4),
            (5, _migration_5), (6, _migration_6), (7, _migration_7), (8, _migration_8),
            (9, _migration_9), (10, _migration_10), (11, _migration_11),
        ):
            if version < migration_version:
                migration(db)
                db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                           (migration_version, utc_now()))
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _enable_wal(connection: sqlite3.Connection) -> None:
    # A journal-mode transition can return SQLITE_BUSY immediately instead of
    # invoking SQLite's busy handler when several fresh processes race. Retry
    # only that startup transition, within the same bounded lock wait.
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()["journal_mode"]
            if mode != "wal":
                mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()["journal_mode"]
            if mode != "wal":
                raise RuntimeError("Local database does not support WAL")
            return
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
            remaining = deadline - time.monotonic()
            if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or remaining <= 0:
                raise
            time.sleep(min(0.025, remaining))


def row_dict(row: sqlite3.Row | dict | None) -> dict | None:
    return dict(row) if row is not None else None


def public_job(row: sqlite3.Row | dict) -> dict:
    row = dict(row)
    result = {
        "id": row["id"], "status": row["status"], "phase": row["phase"],
        "created_at": row["created_at"], "completed_at": row["completed_at"],
        "submitted_input": json.loads(row["request_json"]),
        "output_locale": json.loads(row["request_json"]).get("output_locale", "zh-TW"),
        "prompt_artifact_id": row.get("prompt_artifact_id"),
        "error": None,
        "report": json.loads(row["report_json"]) if row["report_json"] else None,
        "v4_status": row["v4_status"],
        "v4_shadow": json.loads(row["v4_json"]) if row["v4_json"] else None,
        # What a running analysis can already show; the finished report replaces it.
        "progress": (json.loads(row["progress_json"]) if row.get("progress_json")
                     and row["status"] in {"queued", "running"} else None),
    }
    if row["error_code"]:
        result["error"] = {"code": row["error_code"], "message": row["error_message"]}
    return result


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"
