# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Step 3: load the baostock HS300 dataset into the ``astock`` TimescaleDB database.

Subcommands (fire)::

    python baostock/run_db.py init            create database + extension + schema
    python baostock/run_db.py load-local      stage 1: local CSV cache -> hypertables (0 API calls)
    python baostock/run_db.py sync-sector     stage 2: baostock industry / index / ST boards
    python baostock/run_db.py verify          check the database against the CSV cache
    python baostock/run_db.py all             init + load-local + sync-sector + verify

Connection parameters come from ``PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE`` (defaults
``localhost:5432``, ``postgres/postgres``, database ``astock``); see ``db/db_config.py``.

Every stage is idempotent and cache-first, so re-running is cheap: ``load-local`` skips symbols
whose ``sync_watermark`` already sits at the CSV frontier, and ``sync-sector`` reuses
``data/sector/*.csv`` instead of calling baostock again.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Board names and stock names are Chinese; a Windows console defaults to a non-UTF-8 code page,
# which would garble (or raise on) the logged tables. Re-point the standard streams at UTF-8 first.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):  # noqa: PERF203
        pass

from loguru import logger  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent          # .../QLib/baostock
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))


# --------------------------------------------------------------------------- #
# Commands. Each wrapper swallows the underlying return value on purpose: fire renders a returned
# dict/object as a command GROUP (listing its keys/attributes) instead of the result, and would also
# exit non-zero. Everything worth reading is already logged by the callee.
# Signatures are spelled out (rather than **kwargs) so `--help` keeps listing the real flags.
# --------------------------------------------------------------------------- #
def init(dbname: str = None, schema_file: str = None, quiet: bool = False) -> None:
    """Create the ``astock`` database, install timescaledb and apply db/schema.sql."""
    from db.init_db import init_db

    init_db(dbname=dbname, schema_file=Path(schema_file) if schema_file else None, quiet=quiet)


def load_local(
    raw_dir: str = None,
    full_refresh: bool = False,
    batch_symbols: int = 50,
    limit: int = None,
    with_calendar: bool = True,
    with_index: bool = True,
    allow_api: bool = True,
    dbname: str = None,
) -> None:
    """Stage 1: load instrument / trade_calendar / daily_bar / index_daily_bar from the CSV cache."""
    from db.load_local import load_local as _load_local

    _load_local(
        raw_dir=Path(raw_dir) if raw_dir else None,
        full_refresh=full_refresh,
        batch_symbols=batch_symbols,
        limit=limit,
        with_calendar=with_calendar,
        with_index=with_index,
        allow_api=allow_api,
        dbname=dbname,
    )


def sync_sector(
    skip_basic: bool = False,
    skip_index_history: bool = False,
    skip_probe: bool = False,
    only: str = None,
    delay: float = 0.15,
    limit: int = None,
    sector_dir: str = None,
    dbname: str = None,
) -> None:
    """Stage 2: collect baostock board data (industry / index membership / ST) into ``stock_board``."""
    from db.collect_sector import sync_sector as _sync_sector

    _sync_sector(
        skip_basic=skip_basic,
        skip_index_history=skip_index_history,
        skip_probe=skip_probe,
        only=only,
        delay=delay,
        limit=limit,
        sector_dir=Path(sector_dir) if sector_dir else None,
        dbname=dbname,
    )


def verify(raw_dir: str = None, sample: int = 10, dbname: str = None, quiet: bool = False) -> None:
    """Check the database against the local CSV cache; exits 1 when an error-level check fails."""
    from db.verify_db import verify_db

    report = verify_db(raw_dir=Path(raw_dir) if raw_dir else None, sample=sample, dbname=dbname, quiet=quiet)
    if report.failures:
        raise SystemExit(1)


def all(  # noqa: A001 - fire subcommand name, mirrors the documented CLI
    raw_dir: str = None,
    full_refresh: bool = False,
    skip_sector: bool = False,
    skip_basic: bool = False,
    skip_verify: bool = False,
    dbname: str = None,
) -> None:
    """Run the whole stage-1 + stage-2 pipeline and verify it at the end."""
    init(dbname=dbname)
    load_local(raw_dir=raw_dir, full_refresh=full_refresh, dbname=dbname)
    if not skip_sector:
        sync_sector(skip_basic=skip_basic, dbname=dbname)
    if not skip_verify:
        verify(raw_dir=raw_dir, dbname=dbname)
    logger.info("run_db all: done")


COMMANDS = {
    "init": init,
    "load-local": load_local,
    "sync-sector": sync_sector,
    "verify": verify,
    "all": all,
}

if __name__ == "__main__":
    import fire

    fire.Fire(COMMANDS)
