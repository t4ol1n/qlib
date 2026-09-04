# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Create the ``astock`` database and install the TimescaleDB schema (idempotent).

Steps::

    connect to `postgres`  ->  CREATE DATABASE astock (template0, UTF8, LC_COLLATE 'C')
    connect to `astock`    ->  CREATE EXTENSION timescaledb
                             ->  apply db/schema.sql statement by statement

Everything is IF NOT EXISTS / OR CREATE, so re-running is a no-op and is safe after a partial
failure. Nothing outside the new ``astock`` database is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List

from loguru import logger

CUR_DIR = Path(__file__).resolve().parent            # .../baostock/db
PROJECT_DIR = CUR_DIR.parent                          # .../baostock
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from db import db_config as dbc  # noqa: E402

SCHEMA_FILE = CUR_DIR / "schema.sql"

# LC_COLLATE 'C' gives byte-order (deterministic, fast) sorting for `symbol`; template0 avoids
# inheriting anything from the local template database.
CREATE_DB_SQL = (
    'CREATE DATABASE {dbname} '
    "WITH TEMPLATE = template0 ENCODING = 'UTF8' LC_COLLATE = 'C' LC_CTYPE = 'C'"
)


def split_statements(sql: str) -> List[str]:
    """Split a .sql script into statements, honouring quotes, dollar-quoting and comments.

    Statements are executed one at a time (rather than shipping the whole script to the server) so
    a failure names the offending statement instead of aborting the script with an opaque error.
    """
    out: List[str] = []
    buf: List[str] = []
    i, n = 0, len(sql)
    in_line_comment = in_block_comment = False
    quote_char = None          # "'" while inside a string literal
    dollar_tag = None          # e.g. "$fn$" while inside a dollar-quoted body

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            buf.append(ch)
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                in_block_comment, i = False, i + 2
                continue
            i += 1
            continue
        if dollar_tag:
            if sql.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
            else:
                buf.append(ch)
                i += 1
            continue
        if quote_char:
            if ch == quote_char and nxt == quote_char:      # '' escape inside a literal
                buf.append(ch * 2)
                i += 2
                continue
            if ch == quote_char:
                quote_char = None
            buf.append(ch)
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch * 2)
            i += 2
            continue
        if ch == "$":
            end = sql.find("$", i + 1)
            tag = sql[i : end + 1] if end != -1 else None
            if tag and tag[1:-1].replace("_", "").isalnum():
                dollar_tag = tag
                buf.append(tag)
                i += len(tag)
                continue
        if ch in ("'", '"'):
            quote_char = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt and _has_code(stmt):
                out.append(stmt)
            buf.clear()
            i += 1
            continue
        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail and _has_code(tail):
        out.append(tail)
    return out


def _has_code(stmt: str) -> bool:
    """False when a "statement" is only comments/whitespace."""
    body, in_line, in_block = [], False, False
    i, n = 0, len(stmt)
    while i < n:
        ch, nxt = stmt[i], stmt[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
            i += 1
            continue
        if in_block:
            if ch == "*" and nxt == "/":
                in_block, i = False, i + 2
                continue
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line, i = True, i + 2
            continue
        if ch == "/" and nxt == "*":
            in_block, i = True, i + 2
            continue
        body.append(ch)
        i += 1
    return bool("".join(body).strip())


def ensure_database(dbname: str = None) -> bool:
    """Create the target database if missing. Returns True when it was created now."""
    dbname = dbname or dbc.conn_params()["dbname"]
    if dbc.database_exists(dbname):
        logger.info(f"database `{dbname}` already exists; reusing it")
        return False
    logger.info(f"creating database `{dbname}` (template0, UTF8, LC_COLLATE 'C') ...")
    with dbc.connection(admin=True, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(CREATE_DB_SQL.format(dbname=dbname))
    logger.info(f"database `{dbname}` created")
    return True


def ensure_extension(dbname: str = None) -> str:
    """``CREATE EXTENSION IF NOT EXISTS timescaledb`` and return the installed version."""
    with dbc.connection(dbname=dbname) as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
        row = cur.fetchone()
    version = row["extversion"] if row else None
    if not version:
        raise RuntimeError(
            "timescaledb extension is not installed; check that `timescaledb` is listed in "
            "shared_preload_libraries and restart the PostgreSQL service"
        )
    logger.info(f"timescaledb {version} ready in `{dbname or dbc.conn_params()['dbname']}`")
    return version


def apply_schema(dbname: str = None, schema_file: Path = None) -> int:
    """Apply ``schema.sql`` statement by statement; returns the number of statements executed."""
    schema_file = Path(schema_file or SCHEMA_FILE)
    if not schema_file.exists():
        raise FileNotFoundError(f"schema file not found: {schema_file}")
    statements = split_statements(schema_file.read_text(encoding="utf-8"))
    logger.info(f"applying {len(statements)} statements from {schema_file.name} ...")
    with dbc.connection(dbname=dbname) as conn, conn.cursor() as cur:
        for idx, stmt in enumerate(statements, start=1):
            head = " ".join(stmt.split())[:90]
            try:
                cur.execute(stmt)
                # Drain every result set: multi-statement helpers (create_hypertable, policies)
                # leave results that would otherwise raise "no results to fetch" state issues.
                while cur.nextset():
                    pass
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"schema statement {idx}/{len(statements)} failed: {head}\n  -> {e}") from e
            logger.debug(f"[{idx}/{len(statements)}] {head}")
    logger.info("schema applied")
    return len(statements)


def summarize(dbname: str = None) -> dict:
    """Return object counts (tables / views / continuous aggregates / hypertables) for logging."""
    with dbc.connection(dbname=dbname) as conn:
        tables = dbc.fetch_df(
            conn,
            """
            SELECT c.relname AS name, c.relkind
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
            ORDER BY c.relname
            """,
        )
        views = dbc.fetch_df(
            conn,
            """
            SELECT c.relname AS name
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'v'
            ORDER BY c.relname
            """,
        )
        caggs = dbc.fetch_df(
            conn,
            "SELECT view_name AS name FROM timescaledb_information.continuous_aggregates ORDER BY 1"
        )
        hypers = dbc.fetch_df(
            conn,
            "SELECT hypertable_name AS name, num_chunks FROM timescaledb_information.hypertables ORDER BY 1"
        )
        policies = dbc.fetch_df(
            conn,
            """
            SELECT application_name AS name, proc_name
            FROM timescaledb_information.jobs
            WHERE proc_name IN ('policy_compression', 'policy_refresh_continuous_aggregate')
            ORDER BY 1
            """
        )
    return {
        "tables": tables,
        "views": views,
        "continuous_aggregates": caggs,
        "hypertables": hypers,
        "policies": policies,
    }


def init_db(dbname: str = None, schema_file: Path = None, quiet: bool = False) -> dict:
    """Entry point: ensure database -> extension -> schema, then log an inventory."""
    created = ensure_database(dbname)
    version = ensure_extension(dbname)
    n_statements = apply_schema(dbname, schema_file)
    inventory = summarize(dbname)
    if not quiet:
        for key, df in inventory.items():
            names = ", ".join(df["name"].astype(str).tolist()) if not df.empty else "(none)"
            logger.info(f"{key} ({len(df)}): {names}")
    logger.info(
        f"init done: database `{dbname or dbc.conn_params()['dbname']}`, timescaledb {version}, "
        f"{n_statements} statements, created_now={created}"
    )
    return {"dbname": dbname or dbc.conn_params()["dbname"], "timescaledb": version, "created": created,
            "statements": n_statements, "inventory": inventory}


if __name__ == "__main__":
    import fire

    fire.Fire(init_db)
