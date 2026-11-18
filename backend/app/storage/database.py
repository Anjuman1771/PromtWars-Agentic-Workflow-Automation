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

            # WAL mode is supported for file-based databases
            if not self.db_path.startswith(":memory:"):
                try:
                    await conn.execute("PRAGMA journal_mode = WAL;")
                except Exception:
                    pass

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
                logger.info("Database schema initialized successfully.", extra={"data": {"db_path": self.db_path}})
            except sqlite3.Error as exc:
                logger.error(f"Failed to execute schema initialization: {exc}")
                raise DatabaseError(f"Failed to initialize schema: {exc}") from exc


# Global database instance
db = Database()
