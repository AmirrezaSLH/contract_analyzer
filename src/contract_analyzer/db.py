"""SQLite connection factory: loads sqlite-vec, applies the schema, guards the
vector width.

One file (`data/rag.db`) holds the documents, the vectors and the BM25 index,
so hybrid retrieval is a join rather than a second datastore to keep in sync.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import sqlite_vec

from .config import Settings, get_settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Tables the schema is expected to create; used by `describe`.
EXPECTED_TABLES = ("documents", "chunks", "chunks_vec", "chunks_fts")

_VEC_DIM_RE = re.compile(r"embedding\s+FLOAT\s*\[\s*(\d+)\s*\]", re.IGNORECASE)


class SchemaMismatch(RuntimeError):
    """The database on disk was built for a different embedding width."""


def connect(
    db_path: Path | str, *, read_only: bool = False, same_thread: bool = True
) -> sqlite3.Connection:
    """Open a connection with sqlite-vec loaded. Does not touch the schema.

    `same_thread=False` lifts sqlite3's own thread check, which the API needs:
    Starlette runs a sync route, its dependencies and the answer worker in
    *different* threadpool threads, and the default check refuses the second
    one. It is safe there and only there, because the API opens one connection
    per request and hands it to one thread at a time -- the `/chat` generator
    joins its worker before the dependency closes the connection. Concurrent
    use of one connection from two threads is still a bug; this flag only stops
    sqlite3 from catching a bug we do not have.
    """
    db_path = Path(db_path)
    if not read_only:
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, check_same_thread=same_thread)
    conn.row_factory = sqlite3.Row

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        # Leaving extension loading on for the life of the connection turns any
        # SQL injection into arbitrary code execution. Close the door.
        conn.enable_load_extension(False)

    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_schema(conn: sqlite3.Connection, dim: int) -> None:
    """Create the tables if absent, and refuse to proceed on a width mismatch.

    Checked *before* the schema is applied: `CREATE VIRTUAL TABLE IF NOT
    EXISTS` silently keeps the existing definition, so a changed EMBEDDING_DIM
    would otherwise be ignored rather than reported.
    """
    existing = stored_dim(conn)
    if existing is not None and existing != dim:
        raise SchemaMismatch(
            f"{describe_path(conn)} stores {existing}-dim vectors but EMBEDDING_DIM={dim}. "
            "A vec0 table's width is fixed at creation -- either restore the old value "
            "or delete the database and re-ingest."
        )
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8").format(dim=dim))
    conn.commit()


def stored_dim(conn: sqlite3.Connection) -> int | None:
    """The vector width baked into this database, or None if not yet created."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'chunks_vec'"
    ).fetchone()
    if row is None or row["sql"] is None:
        return None
    match = _VEC_DIM_RE.search(row["sql"])
    return int(match.group(1)) if match else None


def describe_path(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return row["file"] if row and row["file"] else ":memory:"


def get_db(settings: Settings | None = None, *, same_thread: bool = True) -> sqlite3.Connection:
    """The normal entry point: an open, schema-applied connection."""
    settings = settings or get_settings()
    settings.validate_embedding_dim()
    conn = connect(settings.db_path, same_thread=same_thread)
    apply_schema(conn, settings.embedding_dim)
    return conn


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


if __name__ == "__main__":  # a two-line smoke check: python -m contract_analyzer.db
    _conn = get_db()
    print(describe_path(_conn), "->", sorted(set(table_names(_conn)) & set(EXPECTED_TABLES)))
