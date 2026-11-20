"""FlowPilot AI Asynchronous SQLite Database Manager.

Provides robust connection management, foreign key enforcement, WAL mode,
and idempotent schema initialization using aiosqlite.
"""

from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3
from typing import AsyncGenerator, Optional
import aiosqlite

from ..config import settings
from ..observability.logger import get_logger
from ..safety.exceptions import FlowPilotException

logger = get_logger("flowpilot.storage.database")

SCHEMA_FILE = Path(__file__).parent / "schema.sql"


class DatabaseError(FlowPilotException):
    """Raised when an unrecoverable database error occurs."""
    pass


class Database:
    """Manages SQLite connections, pragmas, and schema migrations."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = str(db_path if db_path is not None else settings.db_path)

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        """Async context manager that yields a configured aiosqlite connection."""
        conn: Optional[aiosqlite.Connection] = None
        try:
            conn = await aiosqlite.connect(self.db_path)
            conn.row_factory = aiosqlite.Row

            # Critical pragmas for data integrity and concurrency
            await conn.execute("PRAGMA foreign_keys = ON;")
            await conn.execute("PRAGMA busy_timeout = 5000;")
            foreign_keys_cursor = await conn.execute("PRAGMA foreign_keys;")
            if (await foreign_keys_cursor.fetchone())[0] != 1:
                raise DatabaseError("SQLite foreign key enforcement could not be enabled.")
            busy_timeout_cursor = await conn.execute("PRAGMA busy_timeout;")
            if (await busy_timeout_cursor.fetchone())[0] != 5000:
                raise DatabaseError("SQLite busy timeout could not be configured.")

            # WAL mode is supported for file-based databases
            if not self.db_path.startswith(":memory:"):
                wal_cursor = await conn.execute("PRAGMA journal_mode = WAL;")
                journal_mode = (await wal_cursor.fetchone())[0]
                if str(journal_mode).lower() != "wal":
                    raise DatabaseError(
                        f"SQLite WAL mode could not be enabled (reported mode: {journal_mode})."
                    )

            yield conn
        except sqlite3.Error as exc:
            logger.error(f"Database error: {exc}", extra={"data": {"db_path": self.db_path}})
            raise DatabaseError(f"Database error: {exc}") from exc
        finally:
            if conn is not None:
                await conn.close()

    async def init_db(self, schema_path: Optional[Path | str] = None) -> None:
        """Initialize database tables and indexes from schema.sql.

        Safe to execute multiple times (idempotent).
        """
        path = Path(schema_path) if schema_path else SCHEMA_FILE
        if not path.is_file():
            raise DatabaseError(f"Schema file not found at: {path}")

        schema_sql = path.read_text(encoding="utf-8")

        async with self.connection() as conn:
            try:
                await conn.executescript(schema_sql)
                await conn.commit()
                await self._migrate_execution_schema(conn)
                await self._migrate_idempotency_schema(conn)
                await conn.commit()
                logger.info("Database schema initialized successfully.", extra={"data": {"db_path": self.db_path}})
            except sqlite3.Error as exc:
                logger.error(f"Failed to execute schema initialization: {exc}")
                raise DatabaseError(f"Failed to initialize schema: {exc}") from exc

    async def _migrate_execution_schema(self, conn: aiosqlite.Connection) -> None:
        """Add execution metadata and CANCELLED atomically after acquiring a write lock."""
        await conn.execute("PRAGMA foreign_keys = OFF;")
        transaction_started = False
        try:
            await conn.execute("BEGIN IMMEDIATE;")
            transaction_started = True
            cursor = await conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks';"
            )
            table_row = await cursor.fetchone()
            if table_row is None:
                raise DatabaseError(
                    "The tasks table is missing after schema initialization."
                )

            cursor = await conn.execute("PRAGMA table_info(tasks);")
            columns = {str(row["name"]) for row in await cursor.fetchall()}
            table_sql = str(table_row["sql"] or "")
            has_cancelled_state = "'CANCELLED'" in table_sql

            if has_cancelled_state:
                migrations = {
                    "idempotency_key": "TEXT",
                    "input_redacted": "BOOLEAN DEFAULT 0",
                    "output_redacted": "BOOLEAN DEFAULT 0",
                    "reconciliation_required": "BOOLEAN NOT NULL DEFAULT 0",
                }
                for name, declaration in migrations.items():
                    if name not in columns:
                        await self._add_execution_column(conn, name, declaration)
                await conn.commit()
                return

            source_idempotency_key = (
                "idempotency_key" if "idempotency_key" in columns else "NULL"
            )
            source_input_redacted = (
                "input_redacted" if "input_redacted" in columns else "0"
            )
            source_output_redacted = (
                "output_redacted" if "output_redacted" in columns else "0"
            )
            source_reconciliation_required = (
                "reconciliation_required"
                if "reconciliation_required" in columns
                else "0"
            )
            await conn.execute("DROP INDEX IF EXISTS idx_tasks_run_status;")
            await conn.execute(
                """
                CREATE TABLE tasks_new (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'PENDING', 'RUNNING', 'PAUSED_APPROVAL', 'COMPLETED',
                        'FAILED', 'CANCELLED', 'SKIPPED'
                    )),
                    requires_approval BOOLEAN DEFAULT 0,
                    depends_on_json TEXT NOT NULL,
                    input_params_json TEXT NOT NULL,
                    idempotency_key TEXT,
                    input_redacted BOOLEAN DEFAULT 0,
                    output_redacted BOOLEAN DEFAULT 0,
                    reconciliation_required BOOLEAN NOT NULL DEFAULT 0,
                    output_data_json TEXT,
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
                );
                """
            )
            await conn.execute(
                f"""
                INSERT INTO tasks_new (
                    id, run_id, task_id, name, tool_name, status,
                    requires_approval, depends_on_json, input_params_json,
                    idempotency_key, input_redacted, output_redacted,
                    reconciliation_required,
                    output_data_json, error_message, retry_count, started_at,
                    completed_at
                )
                SELECT id, run_id, task_id, name, tool_name, status,
                       requires_approval, depends_on_json, input_params_json,
                       {source_idempotency_key}, {source_input_redacted},
                       {source_output_redacted},
                       {source_reconciliation_required}, output_data_json,
                       error_message, retry_count, started_at, completed_at
                FROM tasks;
                """
            )
            await conn.execute("DROP TABLE tasks;")
            await conn.execute("ALTER TABLE tasks_new RENAME TO tasks;")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_run_status ON tasks(run_id, status);"
            )
            await conn.commit()
        except BaseException:
            if transaction_started:
                await conn.rollback()
            raise
        finally:
            await conn.execute("PRAGMA foreign_keys = ON;")

    @staticmethod
    async def _add_execution_column(
        conn: aiosqlite.Connection,
        name: str,
        declaration: str,
    ) -> None:
        await conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration};")

    async def _migrate_idempotency_schema(self, conn: aiosqlite.Connection) -> None:
        """Track whether cached results were redacted before persistence."""
        await conn.execute("BEGIN IMMEDIATE;")
        try:
            cursor = await conn.execute("PRAGMA table_info(idempotency_records);")
            columns = {str(row["name"]) for row in await cursor.fetchall()}
            if "output_redacted" not in columns:
                await conn.execute(
                    "ALTER TABLE idempotency_records "
                    "ADD COLUMN output_redacted BOOLEAN DEFAULT 0;"
                )
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise


# Global database instance
db = Database()
