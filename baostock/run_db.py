# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Step 3: load the baostock HS300 dataset into the ``astock`` TimescaleDB database.

Subcommands (fire)::

    python baostock/run_db.py init            create database + extension + schema
    python baostock/run_db.py load-local      stage 1: local CSV cache -> hypertables (0 API calls)
    python baostock/run_db.py sync-sector     stage 2: baostock industry / index / ST boards
    python baostock/run_db.py record-selection  stage 3: output/*.csv -> selection_run/pick
    python baostock/run_db.py refresh-returns   stage 3b: realized T+1/T+5/T+20 from daily_bar
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


def _parse_horizons(text) -> tuple:
    """``"1,5,20"`` -> ``(1, 5, 20)``. fire hands a comma-separated string, not a list."""
    if text is None:
        return (1, 5, 20)
    if isinstance(text, (list, tuple)):
        return tuple(int(h) for h in text)
    return tuple(int(p) for p in str(text).replace(" ", "").split(",") if p)


def record_selection(
    backfill: bool = False,
    pred_csv: str = None,
    selection_csv: str = None,
    topk: int = None,
    with_db: bool = True,
    dbname: str = None,
) -> None:
    """Stage 3: write the top-K picks into ``selection_run`` / ``selection_pick`` (0 API calls).

    Without ``--backfill`` the single latest signal date is read from
    ``output/selected_stocks_latest.csv``; with it, EVERY date in ``output/pred.csv`` is expanded to
    its own top-K, which is how the historical daily selections are recovered from a run that
    already happened. Re-running is idempotent either way.

    ``--with-db=False`` is a dry run: it parses the same inputs and logs the signal dates, pick
    count and ``strategy_key`` that WOULD be written, without opening a connection. (Remember fire
    has no ``--no-with-db`` form; see README section 8.)
    """
    from db import record_selection as rs

    if with_db:
        if backfill:
            rs.backfill_from_pred(pred_csv=pred_csv, topk=topk, dbname=dbname)
        else:
            sel = rs.read_selection_csv(selection_csv)
            src = Path(selection_csv) if selection_csv else None
            meta, metrics = rs.load_meta_from_output(src.parent if src else None)
            rs.record_selection(
                sel,
                meta=meta,
                metrics=metrics,
                source="cli",
                pred_csv=src,
                dbname=dbname,
            )
        return

    logger.warning("record-selection --with-db=False: dry run, the database is NOT touched")
    if backfill:
        picks = rs.selection_from_pred(rs.read_pred_csv(pred_csv), topk=topk)
        src = pred_csv or "output/pred.csv"
    else:
        picks = rs.normalize_selection(rs.read_selection_csv(selection_csv))
        src = selection_csv or "output/selected_stocks_latest.csv"
    meta, metrics = rs.load_meta_from_output()
    logger.info(
        f"would write {len(picks)} pick(s) over {picks['signal_date'].nunique()} signal date(s) "
        f"[{picks['signal_date'].min()} .. {picks['signal_date'].max()}] from {src} "
        f"with strategy_key={rs.strategy_key(rs.normalize_meta(meta, metrics))}"
    )


def refresh_returns(horizons: str = "1,5,20", force: bool = False, dbname: str = None) -> None:
    """Stage 3b: fill realized T+N returns and the excess over SH000300 from the stored bars.

    Repeatable and cheap: only rows whose ``ret_tN`` is still NULL are touched, so each horizon is
    computed exactly once, on the first run after its exit day has bars. A non-zero "still_null" for
    recent dates is expected, not an error. ``--force`` recomputes everything (after a bar reload).
    """
    from db.record_selection import refresh_returns as _refresh

    _refresh(horizons=_parse_horizons(horizons), force=force, dbname=dbname)


def all(  # noqa: A001 - fire subcommand name, mirrors the documented CLI
    raw_dir: str = None,
    full_refresh: bool = False,
    skip_sector: bool = False,
    skip_basic: bool = False,
    skip_verify: bool = False,
    with_returns: bool = False,
    dbname: str = None,
) -> None:
    """Run the whole stage-1 + stage-2 pipeline and verify it at the end.

    ``--with-returns`` additionally recomputes the realized selection returns before verifying, so
    ``verify`` can cross-check them. It is opt-in: it needs ``selection_pick`` to be populated, and
    leaving it off keeps this command's behaviour exactly as before.
    """
    init(dbname=dbname)
    load_local(raw_dir=raw_dir, full_refresh=full_refresh, dbname=dbname)
    if not skip_sector:
        sync_sector(skip_basic=skip_basic, dbname=dbname)
    if with_returns:
        refresh_returns(dbname=dbname)
    if not skip_verify:
        verify(raw_dir=raw_dir, dbname=dbname)
    logger.info("run_db all: done")


COMMANDS = {
    "init": init,
    "load-local": load_local,
    "sync-sector": sync_sector,
    "record-selection": record_selection,
    "refresh-returns": refresh_returns,
    "verify": verify,
    "all": all,
}

if __name__ == "__main__":
    import fire

    fire.Fire(COMMANDS)
