"""Thin DB layer that speaks both PostgreSQL and SQLite.

All repository SQL is written with ``?`` placeholders; this module rewrites
them to ``%s`` when the backend is PostgreSQL. That keeps one code path for
both dialects and keeps the SQL readable.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA_DIR = Path(__file__).parent / "schema"


class IntegrityError(Exception):
    """Raised when the storage layer rejects a write (e.g. immutability trigger)."""


class Database:
    def __init__(self, url: str):
        self.url = url
        self.dialect = "postgres" if url.startswith(("postgres://", "postgresql://")) else "sqlite"
        self._conn: Any = None

    # ------------------------------------------------------------- connect
    def connect(self) -> Any:
        if self._conn is not None:
            return self._conn
        if self.dialect == "postgres":
            try:
                import psycopg  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "PostgreSQL backend selected but psycopg is not installed. "
                    "pip install 'psycopg[binary]'"
                ) from exc
            self._conn = psycopg.connect(self.url, autocommit=False)
        else:
            path = self.url.split("sqlite:", 1)[-1] or "./itsar.db"
            if path not in (":memory:", ""):
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    # ---------------------------------------------------------------- init
    def init_schema(self) -> None:
        sql = (SCHEMA_DIR / f"{self.dialect}.sql").read_text(encoding="utf-8")
        conn = self.connect()
        if self.dialect == "sqlite":
            conn.executescript(sql)
        else:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()

    # ------------------------------------------------------------ execute
    def _adapt(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        conn = self.connect()
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute and return the affected rowcount."""
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(self._adapt(sql), tuple(params))
            rc = cur.rowcount
            cur.close()
            if self.dialect == "postgres":
                conn.commit()
            return rc
        except Exception as exc:
            if self.dialect == "postgres":
                conn.rollback()
            raise _translate(exc) from exc

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        conn = self.connect()
        cur = conn.cursor()
        cur.execute(self._adapt(sql), tuple(params))
        cols = [d[0] for d in (cur.description or [])]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()
        return rows

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return default
        return next(iter(row.values()))

    # ------------------------------------------------------------- insert
    def insert(self, table: str, values: dict) -> int:
        """INSERT one row, return its integer primary key."""
        cols = list(values)
        ph = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})"
        conn = self.connect()
        try:
            cur = conn.cursor()
            if self.dialect == "postgres":
                cur.execute(self._adapt(sql) + " RETURNING id", tuple(values[c] for c in cols))
                new_id = cur.fetchone()[0]
                conn.commit()
            else:
                cur.execute(sql, tuple(values[c] for c in cols))
                new_id = cur.lastrowid
            cur.close()
            return int(new_id)
        except Exception as exc:
            if self.dialect == "postgres":
                conn.rollback()
            raise _translate(exc) from exc

    def insert_many(self, table: str, rows: Iterable[dict]) -> None:
        for row in rows:
            self.insert(table, row)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _translate(exc: Exception) -> Exception:
    msg = str(exc)
    if "ITSAR immutability" in msg:
        return IntegrityError(msg)
    if "UNIQUE constraint failed" in msg or "duplicate key value" in msg:
        return IntegrityError(msg)
    return exc


def open_db(url: str) -> Database:
    db = Database(url)
    db.init_schema()
    return db
