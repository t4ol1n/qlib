# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Stage 1 -- load the cached local CSVs into the ``astock`` TimescaleDB database.

Data flow (all of it local; at most ONE baostock call for the trade calendar)::

    data/raw/_hs300_membership.csv  ->  instrument        (700 union symbols + SH000300)
    data/raw/<SYMBOL>.csv           ->  daily_bar         (COPY -> staging -> upsert)
    data/raw/SH000300.csv           ->  index_daily_bar
    query_trade_dates (cached)      ->  trade_calendar
    (derived)                       ->  sync_watermark / sync_log

Idempotency: the staging table is TRUNCATEd, rows are deduplicated with ``DISTINCT ON`` and
written with ``ON CONFLICT DO UPDATE``, so a re-run leaves the row counts unchanged. By default
only rows newer than ``sync_watermark.last_date`` are shipped; ``--full-refresh`` reloads
everything.

Compression is paused for the duration of the load (and the affected chunks decompressed) because
writing into a compressed chunk forces a per-statement decompression; the policy is restored after
the continuous aggregates are refreshed.
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from loguru import logger
from tqdm import tqdm

CUR_DIR = Path(__file__).resolve().parent            # .../baostock/db
PROJECT_DIR = CUR_DIR.parent                          # .../baostock
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402
from db import db_config as dbc  # noqa: E402

MEMBERSHIP_FNAME = "_hs300_membership.csv"
CALENDAR_FNAME = "trade_calendar.csv"
COMPRESSION_AFTER = "90 days"

# Index codes are SH000xxx / SZ399xxx; no listed stock uses those prefixes (SH stocks are 60x/68x/900,
# SZ stocks are 00x/30x), so the daily-bar files can be split by prefix alone.
INDEX_PREFIXES = ("SH000", "SZ399")

DAILY_COLS = [
    "symbol", "trade_date", "open", "high", "low", "close", "preclose",
    "volume", "amount", "vwap", "turn", "pct_chg", "trade_status", "is_st", "factor", "close_adj",
]
INDEX_COLS = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]

FLOAT_COLS = ["open", "high", "low", "close", "preclose", "amount", "turn", "pctChg", "close_adj", "factor"]
INT_COLS = ["volume", "tradestatus", "isST"]


# --------------------------------------------------------------------------- #
# Symbol helpers
# --------------------------------------------------------------------------- #
def to_symbol(code: str) -> str:
    """baostock ``sh.600000`` -> QLib ``SH600000``."""
    return str(code).replace(".", "").upper()


def to_code(symbol: str) -> str:
    """QLib ``SH600000`` -> baostock ``sh.600000``."""
    s = str(symbol).upper()
    return f"{s[:2].lower()}.{s[2:]}"


def is_index(symbol: str) -> bool:
    return str(symbol).upper().startswith(INDEX_PREFIXES)


def derive_exchange_board(symbol: str) -> Tuple[str, str]:
    """Derive ``(exchange, board)`` from the code prefix -- zero API cost.

    60x/00x main board, 300/301 ChiNext (创业板), 688/689 STAR (科创板), 4xx/8xx/92x Beijing (北交所).
    """
    s = str(symbol).upper()
    exch, num = s[:2], s[2:]
    if is_index(s):
        return exch, "指数"
    if num.startswith(("60", "000", "001", "002", "003")):
        return exch, "主板"
    if num.startswith(("300", "301", "302")):
        return exch, "创业板"
    if num.startswith(("688", "689")):
        return exch, "科创板"
    if num.startswith(("43", "83", "87", "88", "92", "920")):
        return exch, "北交所"
    return exch, "其他"


# --------------------------------------------------------------------------- #
# instrument
# --------------------------------------------------------------------------- #
def build_instruments(membership_path: Path, symbols: Sequence[str]) -> pd.DataFrame:
    """Build the ``instrument`` dimension frame from the membership cache + the raw file list.

    ``code_name`` / ``hs300_first`` / ``hs300_last`` come from the 51 quarter-end snapshots; a
    symbol is ``is_csi300_now`` iff it appears in the NEWEST snapshot. Symbols found on disk but
    absent from the membership file are still inserted (with a warning) so ``instrument`` never
    lags behind ``daily_bar``.
    """
    membership = pd.read_csv(membership_path, dtype={"code": str})
    membership["date"] = pd.to_datetime(membership["date"], format="mixed")
    membership["symbol"] = membership["code"].map(to_symbol)
    # Keep the newest non-empty name per symbol: baostock returns '' for some snapshot rows.
    named = membership[membership["code_name"].fillna("") != ""].sort_values("date")
    latest_snapshot = membership["date"].max()

    grouped = membership.groupby("symbol")["date"]
    agg = pd.DataFrame({"hs300_first": grouped.min(), "hs300_last": grouped.max()})
    agg["code_name"] = named.groupby("symbol")["code_name"].last()
    agg["code"] = membership.drop_duplicates("symbol").set_index("symbol")["code"]
    agg = agg.reset_index()

    have = set(agg["symbol"])
    missing = [s for s in symbols if s not in have]
    if missing:
        logger.warning(f"{len(missing)} symbol(s) on disk are absent from the membership cache "
                       f"(inserted without HS300 dates): {missing[:10]}")
        agg = pd.concat([agg, pd.DataFrame({"symbol": missing})], ignore_index=True)

    # The CSI300 index itself is a dimension row too, flagged so it is excluded from stock queries.
    idx_symbol = config.BENCHMARK
    if idx_symbol not in set(agg["symbol"]):
        agg = pd.concat([agg, pd.DataFrame([{"symbol": idx_symbol, "code": config.INDEX_BAOSTOCK_CODE,
                                             "code_name": "沪深300"}])], ignore_index=True)

    agg["code"] = agg["code"].fillna(agg["symbol"].map(to_code))
    agg["code_name"] = agg["code_name"].fillna("")
    exch_board = agg["symbol"].map(derive_exchange_board)
    agg["exchange"] = [e for e, _ in exch_board]
    agg["board"] = [b for _, b in exch_board]
    agg["is_index"] = agg["symbol"].map(is_index)
    agg["is_csi300_now"] = (~agg["is_index"]) & (agg["hs300_last"] == latest_snapshot)

    out = agg[["symbol", "code", "code_name", "exchange", "board", "is_index",
               "hs300_first", "hs300_last", "is_csi300_now"]].copy()
    for c in ("hs300_first", "hs300_last"):
        out[c] = out[c].map(lambda v: v.date() if isinstance(v, pd.Timestamp) else None)
    logger.info(f"instrument frame: {len(out)} rows, latest HS300 snapshot {latest_snapshot.date()}, "
                f"is_csi300_now={int(out['is_csi300_now'].sum())}")
    return out


def load_instruments(conn, df: pd.DataFrame) -> int:
    """Upsert ``instrument``.

    ``sec_type``/``status``/``ipo_date``/``out_date`` are owned by stage 2 (``collect_sector``), so
    they are deliberately NOT in the column list here -- this loader can never null them out.
    """
    cols = ["symbol", "code", "code_name", "exchange", "board", "is_index",
            "hs300_first", "hs300_last", "is_csi300_now"]
    dbc.copy_frame(conn, "instrument_stg", df, columns=cols)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO instrument AS i (symbol, code, code_name, exchange, board, is_index,
                                         hs300_first, hs300_last, is_csi300_now, updated_at)
            SELECT symbol, code, code_name, exchange, board, is_index,
                   hs300_first, hs300_last, is_csi300_now, now()
            FROM instrument_stg
            ON CONFLICT (symbol) DO UPDATE SET
                code          = EXCLUDED.code,
                code_name     = COALESCE(NULLIF(EXCLUDED.code_name, ''), i.code_name),
                exchange      = EXCLUDED.exchange,
                board         = EXCLUDED.board,
                is_index      = EXCLUDED.is_index,
                hs300_first   = LEAST(i.hs300_first, EXCLUDED.hs300_first),
                hs300_last    = GREATEST(i.hs300_last, EXCLUDED.hs300_last),
                is_csi300_now = EXCLUDED.is_csi300_now,
                updated_at    = now()
            """
        )
        written = cur.rowcount
        cur.execute("TRUNCATE instrument_stg")
    return written


# --------------------------------------------------------------------------- #
# trade_calendar
# --------------------------------------------------------------------------- #
def load_trade_calendar(conn, cache_path: Path, start: str, end: str, allow_api: bool = True) -> int:
    """Load ``trade_calendar`` cache-first: disk cache -> one baostock call -> calendars/day.txt.

    The fallback keeps stage 1 usable with no network at all; it can only mark days that ARE
    trading days (``is_trading_day=1``) because ``day.txt`` lists nothing else.
    """
    df = None
    if cache_path.exists() and cache_path.stat().st_size > 0:
        df = pd.read_csv(cache_path, dtype={"is_trading_day": str})
        logger.info(f"trade calendar: reusing cache {cache_path} ({len(df)} rows)")
    elif allow_api:
        df = _fetch_trade_calendar(start, end)
        if df is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(cache_path, index=False)
            logger.info(f"trade calendar: fetched {len(df)} rows -> cached at {cache_path}")
    if df is None or df.empty:
        df = _calendar_from_day_txt(end)
        logger.warning(f"trade calendar: fell back to calendars/day.txt ({len(df)} trading days only)")

    out = pd.DataFrame({
        "calendar_date": pd.to_datetime(df["calendar_date"], format="mixed").dt.date,
        "is_trading_day": pd.to_numeric(df["is_trading_day"], errors="coerce").fillna(0).astype("Int64"),
    }).drop_duplicates(subset=["calendar_date"])

    dbc.copy_frame(conn, "trade_calendar_stg", out)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trade_calendar (calendar_date, is_trading_day)
            SELECT calendar_date, is_trading_day FROM trade_calendar_stg
            ON CONFLICT (calendar_date) DO UPDATE SET is_trading_day = EXCLUDED.is_trading_day
            """
        )
        written = cur.rowcount
        cur.execute("TRUNCATE trade_calendar_stg")
    return written


def _fetch_trade_calendar(start: str, end: str) -> Optional[pd.DataFrame]:
    """One baostock call for the whole trading calendar; None when unavailable."""
    import baostock as bs

    try:
        lg = bs.login()
        if lg.error_code != "0":
            logger.warning(f"baostock login failed ({lg.error_code} {lg.error_msg}); using offline calendar")
            return None
        try:
            rs = bs.query_trade_dates(start_date=start, end_date=end)
            if rs.error_code != "0":
                logger.warning(f"query_trade_dates failed: {rs.error_code} {rs.error_msg}")
                return None
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                return None
            return pd.DataFrame(rows, columns=rs.fields).rename(
                columns={"calendar_date": "calendar_date", "is_trading_day": "is_trading_day"}
            )
        finally:
            try:
                bs.logout()
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"trade calendar fetch raised {e!r}; using offline calendar")
        return None


def _calendar_from_day_txt(end: str) -> pd.DataFrame:
    cal_path = Path(config.QLIB_BIN_DIR) / "calendars" / "day.txt"
    if not cal_path.exists():
        raise FileNotFoundError(f"no trade-calendar cache and no {cal_path}; cannot build trade_calendar")
    days = [d.strip() for d in cal_path.read_text(encoding="utf-8").split() if d.strip()]
    days = [d for d in days if d <= end]
    return pd.DataFrame({"calendar_date": days, "is_trading_day": ["1"] * len(days)})


# --------------------------------------------------------------------------- #
# daily bars
# --------------------------------------------------------------------------- #
def _parse_dates(s: pd.Series) -> pd.Series:
    """Parse the raw cache's mixed date strings ('2014-01-02' and '2026-09-03 00:00:00' coexist).

    The fast ISO path is tried first because ``format='mixed'`` parses element by element and would
    dominate the runtime over ~2.1M rows.
    """
    try:
        return pd.to_datetime(s, format="%Y-%m-%d")
    except (ValueError, TypeError):
        return pd.to_datetime(s, format="mixed")


def probe_last_date(path: Path) -> Optional[dt.date]:
    """Cheapest possible ``max(date)`` of a raw CSV (date column only).

    Lets an incremental run skip a symbol whose watermark already sits at the file's frontier
    without parsing its 16 columns x ~3000 rows -- the difference between seconds and half a minute
    once 700 files are involved.
    """
    try:
        s = pd.read_csv(path, usecols=["date"])["date"]
    except (ValueError, KeyError, pd.errors.EmptyDataError):
        return None
    if s.empty:
        return None
    return _parse_dates(s).max().date()


def read_daily_stg(path: Path) -> pd.DataFrame:
    """Read one raw CSV into the ``daily_bar_stg`` shape (raw prices + derived raw vwap)."""
    df = pd.read_csv(path, dtype={"code": str, "symbol": str})
    if df.empty:
        return pd.DataFrame(columns=DAILY_COLS)
    symbol = to_symbol(path.stem)
    for c in FLOAT_COLS:
        if c not in df.columns:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in INT_COLS:
        if c not in df.columns:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")

    volume = df["volume"].astype("float64")
    amount = df["amount"].astype("float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = np.where(volume > 0, amount / volume.replace(0, np.nan), np.nan)

    out = pd.DataFrame({
        "symbol": pd.Series(symbol, index=df.index),
        "trade_date": _parse_dates(df["date"]).dt.date,
        "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
        "preclose": df["preclose"],
        "volume": df["volume"], "amount": df["amount"],
        "vwap": pd.Series(vwap, index=df.index),
        "turn": df["turn"], "pct_chg": df["pctChg"],
        "trade_status": df["tradestatus"], "is_st": df["isST"],
        "factor": df["factor"], "close_adj": df["close_adj"],
    })
    return out[DAILY_COLS].dropna(subset=["trade_date"])


def read_index_stg(path: Path) -> pd.DataFrame:
    """Read the index raw CSV into the ``index_daily_bar_stg`` shape."""
    df = read_daily_stg(path)
    if df.empty:
        return pd.DataFrame(columns=INDEX_COLS)
    return df[INDEX_COLS].copy()


def _watermarks(conn, dataset: str) -> Dict[str, dt.date]:
    rows = dbc.fetch_rows(conn, "SELECT symbol, last_date FROM sync_watermark WHERE dataset = %s", (dataset,))
    return {r["symbol"]: r["last_date"] for r in rows}


def _update_watermarks(conn, dataset: str, table: str, symbols: Sequence[str]) -> None:
    """Recompute watermarks from the authoritative table (correct after a full refresh too)."""
    if not symbols:
        return
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO sync_watermark AS w (dataset, symbol, last_date, last_sync_at)
            SELECT %s, symbol, max(trade_date), now()
            FROM {table}
            WHERE symbol = ANY(%s)
            GROUP BY symbol
            ON CONFLICT (dataset, symbol) DO UPDATE SET
                last_date    = GREATEST(w.last_date, EXCLUDED.last_date),
                last_sync_at = now()
            """,
            (dataset, list(symbols)),
        )


def _upsert_sql(table: str, stg: str, cols: Sequence[str], pk: Sequence[str]) -> str:
    col_list = ", ".join(cols)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in pk)
    return f"""
        INSERT INTO {table} ({col_list})
        SELECT DISTINCT ON ({', '.join(pk)}) {col_list}
        FROM {stg}
        ORDER BY {', '.join(pk)}, ctid DESC
        ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {updates}
    """


def _pause_compression(conn, table: str) -> None:
    """Drop the compression policy and decompress chunks that the load is about to write into."""
    with conn.cursor() as cur:
        cur.execute("SELECT remove_compression_policy(%s, if_exists => TRUE)", (table,))


def _decompress_from(conn, table: str, start_date: Optional[dt.date]) -> int:
    """Decompress the compressed chunks a load is about to write into.

    ``start_date=None`` (first ever load, or ``--full-refresh``) means "every chunk". Otherwise the
    earliest watermark is used, which is conservative -- it may decompress a chunk that receives no
    row, but never leaves a target chunk compressed.
    """
    if start_date is None:
        rows = dbc.fetch_rows(
            conn,
            """
            SELECT format('%%I.%%I', chunk_schema, chunk_name) AS chunk
            FROM timescaledb_information.chunks
            WHERE hypertable_name = %s AND is_compressed
            """,
            (table,),
        )
    else:
        rows = dbc.fetch_rows(
            conn,
            """
            SELECT format('%%I.%%I', chunk_schema, chunk_name) AS chunk
            FROM timescaledb_information.chunks
            WHERE hypertable_name = %s AND is_compressed AND (range_end)::text::date > %s
            """,
            (table, start_date),
        )
    with conn.cursor() as cur:
        for r in rows:
            cur.execute("SELECT decompress_chunk(%s, if_compressed => true)", (r["chunk"],))
    if rows:
        logger.info(f"decompressed {len(rows)} chunk(s) of {table} (from {start_date or 'the beginning'})")
    return len(rows)


def _resume_compression(conn, table: str, compress_after: str = COMPRESSION_AFTER) -> None:
    """Re-add the compression policy dropped by :func:`_pause_compression`.

    The interval is bound as a text parameter and cast server-side (``%s::interval``) because
    ``INTERVAL %s`` is not valid SQL once psycopg turns the placeholder into ``$2``.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT add_compression_policy(%s, %s::interval, if_not_exists => TRUE)",
            (table, compress_after),
        )


def _flush_stg(conn, table: str, stg: str, cols: Sequence[str], pk: Sequence[str]) -> int:
    with conn.cursor() as cur:
        cur.execute(_upsert_sql(table, stg, cols, pk))
        written = cur.rowcount
        cur.execute(f"TRUNCATE {stg}")
    return written


def load_bars(
    conn,
    files: Sequence[Path],
    reader,
    table: str,
    stg: str,
    cols: Sequence[str],
    pk: Sequence[str],
    dataset: str,
    marks: Dict[str, dt.date] = None,
    probe=None,
    batch_symbols: int = 50,
    limit: int = None,
    desc: str = "bars",
) -> dict:
    """COPY a batch of CSVs into staging, then upsert into the hypertable.

    Returns ``{files, rows_read, rows_written, skipped}``. Watermarks (``marks``) make the default
    run incremental: a symbol already loaded through its last date contributes nothing. Pass an
    empty dict for a full refresh.
    """
    files = list(files)[:limit] if limit else list(files)
    if not files:
        return {"files": 0, "rows_read": 0, "rows_written": 0, "skipped": 0}

    marks = _watermarks(conn, dataset) if marks is None else marks
    pending: List[str] = []
    rows_read = rows_written = skipped = 0

    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {stg}")

    def flush() -> None:
        nonlocal rows_written, pending
        if not pending:
            return
        rows_written += _flush_stg(conn, table, stg, cols, pk)
        _update_watermarks(conn, dataset, table, pending)
        conn.commit()
        pending = []

    for path in tqdm(files, desc=desc, unit="sym"):
        symbol = to_symbol(path.stem)
        wm = marks.get(symbol)
        if wm is not None and probe is not None:
            last = probe(path)
            if last is not None and last <= wm:
                skipped += 1
                logger.debug(f"{symbol}: cache already loaded through {wm}; skipped without parsing")
                continue
        df = reader(path)
        if df.empty:
            logger.warning(f"{path.name}: empty; skipped")
            skipped += 1
            continue
        if wm is not None:
            df = df[df["trade_date"] > wm]
            if df.empty:
                skipped += 1
                logger.debug(f"{symbol}: already loaded through {wm}; skipped")
                continue
        rows_read += dbc.copy_frame(conn, stg, df, columns=cols)
        pending.append(symbol)
        if len(pending) >= batch_symbols:
            flush()
    flush()

    return {"files": len(files), "rows_read": rows_read, "rows_written": rows_written, "skipped": skipped}


def refresh_continuous_aggregates(conn, views: Sequence[str] = ("daily_bar_weekly", "daily_bar_monthly")) -> None:
    """Full historical refresh (the policies only cover their bounded ``start_offset`` window).

    ``refresh_continuous_aggregate`` is a procedure that refuses to run inside a transaction block,
    so the connection is switched to autocommit for the duration of the refresh.
    """
    conn.commit()
    previous = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for view in views:
                t0 = time.time()
                cur.execute("CALL refresh_continuous_aggregate(%s, NULL, NULL)", (view,))
                logger.info(f"refreshed continuous aggregate {view} in {time.time() - t0:.1f}s")
    finally:
        conn.autocommit = previous


def _raw_files(raw_dir: Path) -> Tuple[List[Path], List[Path]]:
    """Split ``data/raw/*.csv`` into (index files, stock files), ignoring ``_``-prefixed helpers."""
    files = sorted(p for p in raw_dir.glob("*.csv") if not p.name.startswith("_"))
    if not files:
        raise FileNotFoundError(f"no raw CSV in {raw_dir}; run the download step first")
    idx = [p for p in files if is_index(p.stem)]
    stk = [p for p in files if not is_index(p.stem)]
    return idx, stk


def load_local(
    raw_dir=None,
    full_refresh: bool = False,
    batch_symbols: int = 50,
    limit: int = None,
    with_calendar: bool = True,
    with_index: bool = True,
    allow_api: bool = True,
    dbname: str = None,
) -> dict:
    """Entry point for stage 1: instrument + trade_calendar + daily_bar + index_daily_bar."""
    t_start = time.time()
    raw_dir = Path(raw_dir or config.RAW_DIR)
    membership_path = raw_dir / MEMBERSHIP_FNAME
    if not membership_path.exists():
        raise FileNotFoundError(f"membership cache missing: {membership_path}; run the download step first")

    index_files, stock_files = _raw_files(raw_dir)
    all_symbols = [to_symbol(p.stem) for p in stock_files] + [to_symbol(p.stem) for p in index_files]
    logger.info(f"raw cache: {len(stock_files)} stock file(s), {len(index_files)} index file(s) in {raw_dir}")

    inst_df = build_instruments(membership_path, all_symbols)
    cal_start = config.DOWNLOAD_START
    # hs300_last is an object column mixing datetime.date with None (the index row has no membership
    # window), so reduce it in Python rather than with Series.max (which compares against NaN).
    lasts = [d for d in inst_df["hs300_last"].tolist() if isinstance(d, dt.date)]
    cal_end = max(lasts).isoformat() if lasts else pd.Timestamp.today().strftime("%Y-%m-%d")

    summary = {"full_refresh": full_refresh}
    with dbc.connection(dbname=dbname) as conn:
        started = dt.datetime.now()
        summary["instrument"] = load_instruments(conn, inst_df)
        conn.commit()
        logger.info(f"instrument: upserted {summary['instrument']} rows")

        if with_calendar:
            summary["trade_calendar"] = load_trade_calendar(
                conn, Path(config.SECTOR_DIR) / CALENDAR_FNAME, cal_start, cal_end, allow_api
            )
            conn.commit()
            logger.info(f"trade_calendar: upserted {summary['trade_calendar']} rows")

        # Compression must be OFF while writing: an insert into a compressed chunk forces an
        # implicit decompression of that chunk. Drop the policies, decompress the chunks that are
        # about to receive rows, and restore everything once the continuous aggregates are current.
        daily_marks = {} if full_refresh else _watermarks(conn, "daily_bar")
        index_marks = {} if full_refresh else _watermarks(conn, "index_daily_bar")
        _pause_compression(conn, "daily_bar")
        _decompress_from(conn, "daily_bar", min(daily_marks.values()) if daily_marks else None)
        if with_index:
            _pause_compression(conn, "index_daily_bar")
            _decompress_from(conn, "index_daily_bar", min(index_marks.values()) if index_marks else None)
        conn.commit()

        stats = load_bars(
            conn, stock_files, read_daily_stg, "daily_bar", "daily_bar_stg", DAILY_COLS,
            ("symbol", "trade_date"), "daily_bar", marks=daily_marks, probe=probe_last_date,
            batch_symbols=batch_symbols, limit=limit, desc="daily_bar",
        )
        summary["daily_bar"] = stats
        logger.info(f"daily_bar: read {stats['rows_read']} rows, upserted {stats['rows_written']}, "
                    f"{stats['skipped']} symbol(s) skipped by watermark")

        if with_index:
            istats = load_bars(
                conn, index_files, read_index_stg, "index_daily_bar", "index_daily_bar_stg", INDEX_COLS,
                ("symbol", "trade_date"), "index_daily_bar", marks=index_marks, probe=probe_last_date,
                batch_symbols=batch_symbols, limit=limit, desc="index_daily_bar",
            )
            summary["index_daily_bar"] = istats
            logger.info(f"index_daily_bar: read {istats['rows_read']} rows, upserted {istats['rows_written']}")

        refresh_continuous_aggregates(conn)
        _resume_compression(conn, "daily_bar")
        if with_index:
            _resume_compression(conn, "index_daily_bar")
        dbc.log_sync(
            conn, task="load_local", source="local_csv", status="ok",
            rows_fetched=stats["rows_read"], rows_written=stats["rows_written"],
            params={"raw_dir": str(raw_dir), "full_refresh": full_refresh, "limit": limit,
                    "symbols": len(all_symbols)},
            started_at=started,
        )
        conn.commit()

    summary["elapsed_sec"] = round(time.time() - t_start, 1)
    logger.info(f"load_local done in {summary['elapsed_sec']}s")
    return summary


if __name__ == "__main__":
    import fire

    fire.Fire(load_local)
