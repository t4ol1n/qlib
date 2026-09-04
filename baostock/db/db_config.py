# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Connection helpers for the ``astock`` TimescaleDB sink.

Every connection parameter can be overridden with the standard libpq environment
variables, so the same code runs against a local dev server and a remote one::

    PGHOST=localhost PGPORT=5432 PGUSER=postgres PGPASSWORD=postgres PGDATABASE=astock

Bulk loads go through psycopg 3's native binary-free ``COPY ... FROM STDIN``
(``copy_frame``) rather than ``INSERT`` row-by-row: ~2.1M daily bars load in
minutes instead of tens of minutes.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Iterable, Optional, Sequence

import pandas as pd
import psycopg
from loguru import logger
from psycopg.rows import dict_row

# Maintenance connections (CREATE DATABASE) cannot target the database being created.
ADMIN_DATABASE = "postgres"

DEFAULTS = {
    "host": "localhost",
    "port": "5432",
    "user": "postgres",
    "password": "postgres",
    "dbname": "astock",
}
ENV_KEYS = {
    "host": "PGHOST",
    "port": "PGPORT",
    "user": "PGUSER",
    "password": "PGPASSWORD",
    "dbname": "PGDATABASE",
}


def conn_params(dbname: str = None) -> dict:
    """Return the effective connection parameters (env override -> documented defaults)."""
    params = {k: os.environ.get(ENV_KEYS[k], DEFAULTS[k]) for k in DEFAULTS}
    params["port"] = int(params["port"])
    if dbname:
        params["dbname"] = dbname
    return params


def make_dsn(dbname: str = None, admin: bool = False) -> str:
    """Build a libpq DSN. ``admin=True`` points at the maintenance database (``postgres``)."""
    p = conn_params(dbname)
    if admin:
        p["dbname"] = ADMIN_DATABASE
    auth = " ".join(f"{k}='{v}'" for k, v in p.items() if v is not None)
    return auth


def get_conn(dbname: str = None, admin: bool = False, autocommit: bool = False) -> psycopg.Connection:
    """Open a psycopg 3 connection with dict rows and UTF-8 client encoding.

    ``client_encoding`` is pinned explicitly: the server database is UTF8, but a Windows client
    whose locale is cp936 would otherwise send/receive Chinese ``code_name`` values garbled.
    """
    conn = psycopg.connect(make_dsn(dbname=dbname, admin=admin), autocommit=autocommit, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute("SET client_encoding TO 'UTF8'")
    return conn


@contextmanager
def connection(dbname: str = None, admin: bool = False, autocommit: bool = False):
    """Context manager that commits on success and rolls back on error."""
    conn = get_conn(dbname=dbname, admin=admin, autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except BaseException:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def database_exists(dbname: str = None) -> bool:
    """True if the target database is already present on the server (admin connection)."""
    dbname = dbname or conn_params()["dbname"]
    with connection(admin=True, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        return cur.fetchone() is not None


def scalar(conn: psycopg.Connection, sql: str, params: Sequence = ()) -> Optional[object]:
    """Run a single-value query and return the value (None when there is no row)."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return None if row is None else next(iter(row.values()))


def fetch_rows(conn: psycopg.Connection, sql: str, params: Sequence = ()) -> list:
    """Execute a query and return the raw dict rows."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_df(conn: psycopg.Connection, sql: str, params: Sequence = ()) -> pd.DataFrame:
    """Execute a query and return the result set as a DataFrame (column order preserved)."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        cols = [d.name for d in cur.description] if cur.description else []
    return pd.DataFrame(rows, columns=cols)


def copy_frame(
    conn: psycopg.Connection,
    table: str,
    df: pd.DataFrame,
    columns: Iterable[str] = None,
    chunksize: int = 50_000,
) -> int:
    """``COPY`` a DataFrame into ``table`` (text format) and return the row count written.

    ``NaN``/``NaT`` become SQL NULL and every other value is stringified, which is what the text
    ``COPY`` protocol expects. The target may be a plain table or a staging table; upserting from
    the staging table keeps re-runs idempotent (see ``load_local``).
    """
    if df.empty:
        return 0
    cols = list(columns) if columns is not None else list(df.columns)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise KeyError(f"{table}: dataframe is missing columns {missing}")
    data = df[cols]

    quoted = ", ".join(f'"{c}"' for c in cols)
    copy_sql = f'COPY {table} ({quoted}) FROM STDIN'
    written = 0
    with conn.cursor() as cur:
        with cur.copy(copy_sql) as copy:
            # Rows are fed as tuples; psycopg encodes each value, so numpy scalars and
            # pandas Timestamps are normalised to Python natives first.
            records = data.itertuples(index=False, name=None)
            batch: list = []
            for rec in records:
                batch.append(tuple(_pg_value(v) for v in rec))
                if len(batch) >= chunksize:
                    for row in batch:
                        copy.write_row(row)
                    written += len(batch)
                    batch.clear()
            for row in batch:
                copy.write_row(row)
            written += len(batch)
    # TRACE, not DEBUG: this fires once per symbol, so a full load would emit ~700 lines and drown
    # the tqdm progress bar.
    logger.trace(f"COPY {written} rows -> {table}")
    return written


def _pg_value(v):
    """Convert a pandas/numpy cell into something psycopg's text COPY encoder accepts.

    Deliberately cheap: it runs once per cell over ~2.1M rows x 16 columns, so the common cases are
    tested with ``is``/``isinstance`` before falling back to the generic numpy branch.
    """
    if v is None or v is pd.NaT or v is pd.NA:
        return None
    if isinstance(v, float):                 # includes numpy.float64 (a float subclass)
        return None if v != v else float(v)  # v != v  <=>  NaN
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if hasattr(v, "item"):                   # any remaining numpy scalar
        return v.item()
    return v


def log_sync(
    conn: psycopg.Connection,
    task: str,
    source: str,
    status: str,
    rows_fetched: int = None,
    rows_written: int = None,
    params: dict = None,
    error: str = None,
    started_at=None,
) -> None:
    """Append one row to ``sync_log`` (audit trail for every load / collect step)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_log (task, source, params, rows_fetched, rows_written,
                                  status, error, started_at, finished_at)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, now())
            """,
            (
                task,
                source,
                json.dumps(params or {}, ensure_ascii=False, default=str),
                rows_fetched,
                rows_written,
                status,
                error,
                started_at,
            ),
        )
