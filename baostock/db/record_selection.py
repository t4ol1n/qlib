# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Record the daily top-K stock selection into the ``astock`` database.

Two tables (see section 9 of ``db/schema.sql``)::

    selection_run    one header row per (signal_date, strategy_key): config fingerprint + metrics
    selection_pick   one row per chosen stock: rank, symbol, name, score, realized returns

``signal_date`` is the trading day the picks are FOR -- the max date of the prediction matrix -- not
the day the job ran. A job run on the morning of D produces signal_date D-1, so the two are stored
separately (``run_at`` / ``updated_at``) and must never be conflated.

Three entry points, all of them local (ZERO baostock calls):

``record_selection``
    Write one ``selected_stocks_latest.csv``-shaped frame. Re-running the same config overwrites the
    same rows; a different topk/model gets its own ``strategy_key`` and coexists.
``backfill_from_pred``
    Expand ``output/pred.csv`` into the top-K of EVERY date it covers, then write through the same
    path. The prediction matrix is already on disk, so this costs no computation.
``refresh_returns``
    Fill ``ret_t1/t5/t20`` and the excess over ``SH000300`` from ``daily_bar`` / ``index_daily_bar``.
    Repeatable: rows whose exit day has no bars yet stay NULL and are picked up on a later run.

Picks are written "delete the range, then COPY" rather than upserted, because a top-K set can
SHRINK (``--topk 50`` then ``--topk 10``) and ``ON CONFLICT DO UPDATE`` would leave ranks 11-50
behind as stale rows.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd
from loguru import logger

CUR_DIR = Path(__file__).resolve().parent            # .../baostock/db
PROJECT_DIR = CUR_DIR.parent                          # .../baostock
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402
from db import db_config as dbc  # noqa: E402

PRED_FNAME = "pred.csv"
SELECTION_FNAME = "selected_stocks_latest.csv"
METRICS_FNAME = "metrics.json"

# The ONLY horizons that exist as columns. Validating against this tuple is also what makes the
# f-string-built column names in refresh_returns safe: nothing outside it can reach the SQL.
HORIZONS = (1, 5, 20)

# The config fields that define "the same strategy". A change to any of them means a new key, so
# variants coexist and stay comparable instead of silently overwriting each other.
KEY_FIELDS = ("experiment_name", "model_class", "market", "topk", "n_drop", "segments")

PICK_COLS = [
    "signal_date", "strategy_key", "rank", "symbol", "code", "code_name",
    "score", "industry", "is_csi300_now",
]


# --------------------------------------------------------------------------- #
# Strategy fingerprint
# --------------------------------------------------------------------------- #
def strategy_key(meta: dict = None) -> str:
    """Stable 16-hex fingerprint of the config that produced a selection.

    Only ``KEY_FIELDS`` participate, so cosmetic changes (a new recorder id per run, a different
    account size) do NOT mint a new key and orphan the previous picks. ``sort_keys`` makes the
    digest independent of dict insertion order, and missing fields hash as ``None`` so the same
    partial meta always yields the same key.
    """
    meta = meta or {}
    payload = {k: meta.get(k) for k in KEY_FIELDS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def normalize_meta(meta: dict = None, metrics: dict = None) -> dict:
    """Merge the meta embedded in ``metrics.json`` with an explicit ``meta``, explicit winning.

    ``run_workflow`` has the real meta object; ``show_selection`` only has ``metrics.json``, whose
    ``meta`` block carries the same fields. Accepting both lets either caller supply whichever it
    has. ``model_class`` is not in either, so it falls back to the configured model.
    """
    merged: dict = {}
    if metrics:
        merged.update(metrics.get("meta") or {})
    if meta:
        merged.update(meta)
    merged.setdefault("model_class", (config.LGB_MODEL or {}).get("class"))
    merged.setdefault("market", config.MARKET)
    merged.setdefault("benchmark", config.BENCHMARK)
    merged.setdefault("topk", config.TOPK)
    merged.setdefault("n_drop", config.N_DROP)
    return merged


def load_meta_from_output(output_dir=None) -> Tuple[dict, dict]:
    """Recover ``(meta, metrics)`` from a previous run's ``metrics.json``.

    Returns empty dicts when the file is missing or unreadable -- recording the picks still matters
    more than the metadata, so callers degrade rather than fail.
    """
    path = Path(output_dir or config.OUTPUT_DIR) / METRICS_FNAME
    if not path.exists():
        logger.warning(f"{path} not found; recording the selection with a reduced meta")
        return {}, {}
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"{path} is unreadable ({exc}); recording with a reduced meta")
        return {}, {}
    return blob.get("meta") or {}, blob


# --------------------------------------------------------------------------- #
# Selection frames
# --------------------------------------------------------------------------- #
def normalize_selection(sel: pd.DataFrame) -> pd.DataFrame:
    """Coerce a ``selected_stocks_latest.csv``-shaped frame to the internal column names.

    Accepts either the CSV/frame spelling (``date`` / ``instrument`` / ``name``) or the internal one
    (``signal_date`` / ``symbol`` / ``code_name``), so both ``record_selection`` and the backfill can
    share it. ``name`` is optional: the authoritative Chinese name is read from ``instrument``.
    """
    if sel is None or not len(sel):
        return pd.DataFrame(columns=PICK_COLS)
    df = sel.rename(columns={"date": "signal_date", "instrument": "symbol", "name": "csv_name"})
    missing = [c for c in ("signal_date", "rank", "symbol") if c not in df.columns]
    if missing:
        raise KeyError(f"selection frame is missing columns {missing}; got {list(df.columns)}")
    df = df.copy()
    df["signal_date"] = pd.to_datetime(df["signal_date"], errors="coerce").dt.date
    df["symbol"] = df["symbol"].astype(str).str.upper()
    # Ranks arrive as float whenever a CSV round-trip introduced a NaN; the column is smallint
    # NOT NULL, so a null here would surface as an opaque COPY error instead of a readable one.
    df["rank"] = pd.to_numeric(df["rank"], errors="coerce").astype("Int64")
    for col in ("signal_date", "rank"):
        bad = int(df[col].isna().sum())
        if bad:
            raise ValueError(f"{bad} selection row(s) have an unusable {col!r}; got {list(df[col].head())}")
    if "score" not in df.columns:
        df["score"] = pd.NA
    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    if "csv_name" not in df.columns:
        df["csv_name"] = ""
    return df


def selection_from_pred(pred: pd.DataFrame, topk: int = None) -> pd.DataFrame:
    """Top-K by score for EVERY date in a prediction matrix (not just the latest).

    ``pred`` is ``output/pred.csv`` read with ``index_col=[0, 1]``, i.e. a (datetime, instrument)
    MultiIndex and a single ``score`` column. Both levels are resolved to POSITIONAL indices, by name
    when the name is present and by elimination otherwise, because either level ordering occurs in
    the wild and an unnamed level (``index_col`` on a headerless CSV) yields ``None`` names.
    """
    topk = int(topk or config.TOPK)
    df = pred.copy()
    if df.shape[1] == 0:
        raise ValueError("prediction frame has no score column")
    df.columns = ["score"]
    names = list(df.index.names or [])
    if "datetime" in names:
        dt_level = names.index("datetime")
    elif "instrument" in names:
        dt_level = 1 - names.index("instrument")   # the other level is the date
    else:
        dt_level = 0                                # unnamed: assume (datetime, instrument)
    inst_level = names.index("instrument") if "instrument" in names else 1 - dt_level
    if dt_level == inst_level:
        raise ValueError(f"cannot tell the date level from the instrument level in {names}")

    df = df.reset_index()
    df = df.rename(columns={df.columns[dt_level]: "signal_date", df.columns[inst_level]: "symbol"})

    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    df = df.dropna(subset=["score", "symbol"])
    # Stable order before head(): a tie must not reshuffle between runs, or the recorded ranks would
    # change on an identical re-run and the idempotency check would be meaningless.
    df = df.sort_values(["signal_date", "score", "symbol"], ascending=[True, False, True])
    out = df.groupby("signal_date", sort=True).head(topk).reset_index(drop=True)
    out["rank"] = out.groupby("signal_date").cumcount() + 1
    out["signal_date"] = pd.to_datetime(out["signal_date"]).dt.date
    out["symbol"] = out["symbol"].astype(str).str.upper()
    return out[["signal_date", "rank", "symbol", "score"]]


def read_selection_csv(path=None) -> pd.DataFrame:
    """Read ``selected_stocks_latest.csv`` (utf-8-sig, so the Chinese names survive Excel)."""
    path = Path(path or config.OUTPUT_DIR / SELECTION_FNAME)
    if not path.exists():
        raise FileNotFoundError(
            f"no selection CSV at {path}; run the workflow first "
            f"(`python baostock/run_workflow.py`) or pass --selection-csv"
        )
    return pd.read_csv(path, encoding="utf-8-sig")


def read_pred_csv(path=None) -> pd.DataFrame:
    """Read the cached prediction matrix ``output/pred.csv``."""
    path = Path(path or config.OUTPUT_DIR / PRED_FNAME)
    if not path.exists():
        raise FileNotFoundError(
            f"no cached predictions at {path}; run the workflow first "
            f"(`python baostock/run_workflow.py`) or pass --pred-csv"
        )
    return pd.read_csv(path, index_col=[0, 1])


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def _ensure_schema(conn) -> None:
    """Fail with an actionable message when the selection tables have not been created yet."""
    if dbc.scalar(conn, "SELECT to_regclass('public.selection_pick')") is None:
        raise RuntimeError(
            "selection_pick does not exist in this database; apply the schema first: "
            "`python baostock/run_db.py init`"
        )


def _enrich(conn, symbols: Sequence[str]) -> pd.DataFrame:
    """Authoritative ``code`` / ``code_name`` / ``industry`` / ``is_csi300_now`` for ``symbols``.

    The database covers all 700 union symbols, so it beats the ``_hs300_membership.csv`` snapshot
    the workflow uses for names; the CSV value is kept only as a fallback for a symbol the database
    has never seen.
    """
    syms = sorted({str(s).upper() for s in symbols})
    if not syms:
        return pd.DataFrame(columns=["symbol", "code", "code_name", "industry", "is_csi300_now"])
    return dbc.fetch_df(
        conn,
        """
        SELECT i.symbol, i.code, i.code_name, i.is_csi300_now, ind.board_code AS industry
        FROM instrument i
        LEFT JOIN LATERAL (
            SELECT sb.board_code
            FROM stock_board sb
            WHERE sb.symbol = i.symbol AND sb.board_type = 'industry'
            ORDER BY sb.snapshot_date DESC
            LIMIT 1
        ) ind ON TRUE
        WHERE i.symbol = ANY(%s)
        """,
        (syms,),
    )


_RUN_UPSERT = """
    INSERT INTO selection_run (
        signal_date, strategy_key, experiment_name, experiment_id, recorder_id, model_class,
        market, benchmark, topk, n_drop, segments, metrics, n_picks, source, pred_csv,
        run_at, updated_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s, now(), now())
    ON CONFLICT (signal_date, strategy_key) DO UPDATE SET
        experiment_name = EXCLUDED.experiment_name,
        experiment_id   = EXCLUDED.experiment_id,
        recorder_id     = EXCLUDED.recorder_id,
        model_class     = EXCLUDED.model_class,
        market          = EXCLUDED.market,
        benchmark       = EXCLUDED.benchmark,
        topk            = EXCLUDED.topk,
        n_drop          = EXCLUDED.n_drop,
        segments        = EXCLUDED.segments,
        metrics         = EXCLUDED.metrics,
        n_picks         = EXCLUDED.n_picks,
        source          = EXCLUDED.source,
        pred_csv        = EXCLUDED.pred_csv,
        updated_at      = now()
"""
# run_at is deliberately absent from DO UPDATE: it records when this (date, strategy) was FIRST
# seen, which is the only way to tell a fresh pick from a re-recorded one afterwards.


def record_selection(
    sel: pd.DataFrame,
    meta: dict = None,
    metrics: dict = None,
    source: str = "workflow",
    pred_csv=None,
    dbname: str = None,
) -> int:
    """Write a selection frame into ``selection_run`` + ``selection_pick``; return the pick count.

    One transaction: upsert the per-date header rows (the picks' FK target), delete the pick range
    being replaced, ``COPY`` the new picks, append to ``sync_log``. A re-run therefore leaves the
    row counts identical whether the top-K grew, shrank or stayed the same.
    """
    df = normalize_selection(sel)
    if df.empty:
        logger.warning("record_selection: empty selection, nothing written")
        return 0

    full_meta = normalize_meta(meta, metrics)
    key = strategy_key(full_meta)
    segments = json.dumps(full_meta.get("segments") or {}, ensure_ascii=False, default=str)
    metrics_blob = json.dumps(metrics or {}, ensure_ascii=False, default=str)
    pred_csv_txt = str(pred_csv) if pred_csv else None

    with dbc.connection(dbname=dbname) as conn:
        _ensure_schema(conn)
        info = _enrich(conn, df["symbol"])
        lookup = info.set_index("symbol").to_dict("index") if len(info) else {}

        unknown = sorted(set(df["symbol"]) - set(lookup))
        if unknown:
            logger.warning(
                f"{len(unknown)} picked symbol(s) are not in instrument (falling back to the CSV "
                f"name, no industry): {unknown[:8]}{' ...' if len(unknown) > 8 else ''}"
            )

        # The CSV name is the fallback only: the database name is newer and covers the whole union.
        def _name(sym: str, csv_name) -> Optional[str]:
            db_name = (lookup.get(sym) or {}).get("code_name")
            if db_name:
                return str(db_name)
            text = "" if csv_name is None or csv_name != csv_name else str(csv_name).strip()
            return text or None

        picks = pd.DataFrame(
            {
                "signal_date": df["signal_date"],
                "strategy_key": key,
                "rank": df["rank"],
                "symbol": df["symbol"],
                "code": [ (lookup.get(s) or {}).get("code") for s in df["symbol"] ],
                "code_name": [ _name(s, n) for s, n in zip(df["symbol"], df["csv_name"]) ],
                "score": df["score"],
                "industry": [ (lookup.get(s) or {}).get("industry") for s in df["symbol"] ],
                "is_csi300_now": [ (lookup.get(s) or {}).get("is_csi300_now") for s in df["symbol"] ],
            }
        )

        counts = picks.groupby("signal_date").size().to_dict()
        run_rows = [
            (
                d, key,
                full_meta.get("experiment_name"), full_meta.get("experiment_id"),
                full_meta.get("recorder_id"), full_meta.get("model_class"),
                full_meta.get("market"), full_meta.get("benchmark"),
                full_meta.get("topk"), full_meta.get("n_drop"),
                segments, metrics_blob, counts[d], source, pred_csv_txt,
            )
            for d in sorted(counts)
        ]
        with conn.cursor() as cur:
            cur.executemany(_RUN_UPSERT, run_rows)
            # Delete-then-insert, not upsert: a shrinking top-K would otherwise leave stale ranks.
            cur.execute(
                "DELETE FROM selection_pick WHERE strategy_key = %s AND signal_date = ANY(%s)",
                (key, sorted(counts)),
            )
            deleted = cur.rowcount
        written = dbc.copy_frame(conn, "selection_pick", picks, columns=PICK_COLS)
        dbc.log_sync(
            conn,
            task="record_selection",
            source=source,
            status="ok",
            rows_fetched=len(picks),
            rows_written=written,
            params={"strategy_key": key, "dates": len(counts), "deleted": deleted,
                    "pred_csv": pred_csv_txt, "topk": full_meta.get("topk")},
        )

    logger.info(
        f"record_selection[{source}]: {written} pick(s) over {len(counts)} signal date(s), "
        f"strategy_key={key} (replaced {deleted} existing row(s))"
    )
    return written


def backfill_from_pred(
    pred_csv=None,
    topk: int = None,
    meta: dict = None,
    metrics: dict = None,
    dbname: str = None,
) -> dict:
    """Expand ``pred.csv`` into the top-K of every date it covers and record all of them.

    The prediction matrix is already on disk from a previous run, so this makes no baostock call and
    no model call. It only reaches as far back as ``pred.csv`` does -- the test segment -- which is a
    property of the cached file, not of this function.
    """
    path = Path(pred_csv or config.OUTPUT_DIR / PRED_FNAME)
    pred = read_pred_csv(path)
    picks = selection_from_pred(pred, topk=topk)
    if picks.empty:
        logger.warning(f"backfill: {path} produced no picks")
        return {"dates": 0, "rows": 0}

    if not meta and not metrics:
        meta, metrics = load_meta_from_output(path.parent)
    written = record_selection(
        picks, meta=meta, metrics=metrics, source="backfill", pred_csv=path, dbname=dbname
    )
    dates = picks["signal_date"].nunique()
    logger.info(
        f"backfill: {written} pick(s) over {dates} signal date(s) from {path.name} "
        f"({picks['signal_date'].min()} .. {picks['signal_date'].max()})"
    )
    return {"dates": int(dates), "rows": int(written)}


# --------------------------------------------------------------------------- #
# Realized returns
# --------------------------------------------------------------------------- #
def _horizon_sql(n: int, force: bool, bench: str) -> Tuple[str, tuple]:
    """Build ``(sql, params)`` for the set-based UPDATE filling ``ret_t{n}`` / ``excess_t{n}``.

    Returns the statement together with its two bind parameters (the benchmark symbol, used once for
    the entry leg and once for the exit leg) rather than interpolating them.

    ``n`` is interpolated into the column names, which is safe only because the caller validated it
    against ``HORIZONS`` -- there is no column for any other value to name.

    Entry is the adjusted close ON the signal date and requires ``trade_status = 1``: a stock
    suspended that day could not actually have been bought, so it is left NULL rather than priced
    off a stale bar. Exit is the first tradable close ON OR AFTER the T+N trading day, which absorbs
    a suspension at the exit. The T+N day itself comes from ``trade_calendar`` (not N calendar days)
    so that every pick of one signal date shares a single exit day and stays cross-sectionally
    comparable.
    """
    pending = "" if force else f"AND p.ret_t{n} IS NULL"
    return f"""
        WITH cal AS (
            SELECT calendar_date, row_number() OVER (ORDER BY calendar_date) AS rn
            FROM trade_calendar
            WHERE is_trading_day = 1
        ),
        pend AS (
            SELECT p.signal_date, p.strategy_key, p.symbol, c_exit.calendar_date AS exit_date
            FROM selection_pick p
            JOIN cal c_sig  ON c_sig.calendar_date  = p.signal_date
            JOIN cal c_exit ON c_exit.rn = c_sig.rn + {int(n)}
            WHERE TRUE {pending}
        ),
        priced AS (
            SELECT pend.signal_date, pend.strategy_key, pend.symbol,
                   entry.px  AS entry_px,  exitp.px AS exit_px,
                   bentry.px AS bench_in,  bexit.px AS bench_out
            FROM pend
            LEFT JOIN LATERAL (
                SELECT b.close * b.factor AS px FROM daily_bar b
                WHERE b.symbol = pend.symbol AND b.trade_date = pend.signal_date
                  AND b.trade_status = 1
                LIMIT 1
            ) entry ON TRUE
            LEFT JOIN LATERAL (
                SELECT b.close * b.factor AS px FROM daily_bar b
                WHERE b.symbol = pend.symbol AND b.trade_date >= pend.exit_date
                  AND b.trade_status = 1
                ORDER BY b.trade_date
                LIMIT 1
            ) exitp ON TRUE
            LEFT JOIN LATERAL (
                SELECT ib.close AS px FROM index_daily_bar ib
                WHERE ib.symbol = %s AND ib.trade_date = pend.signal_date
                LIMIT 1
            ) bentry ON TRUE
            LEFT JOIN LATERAL (
                SELECT ib.close AS px FROM index_daily_bar ib
                WHERE ib.symbol = %s AND ib.trade_date >= pend.exit_date
                ORDER BY ib.trade_date
                LIMIT 1
            ) bexit ON TRUE
        )
        UPDATE selection_pick p
        SET ret_t{n}    = round((priced.exit_px / NULLIF(priced.entry_px, 0) - 1)::numeric, 6),
            excess_t{n} = round(((priced.exit_px / NULLIF(priced.entry_px, 0) - 1)
                               - (priced.bench_out / NULLIF(priced.bench_in, 0) - 1))::numeric, 6),
            ret_computed_at = now()
        FROM priced
        WHERE p.signal_date  = priced.signal_date
          AND p.strategy_key = priced.strategy_key
          AND p.symbol       = priced.symbol
          AND priced.entry_px IS NOT NULL
          AND priced.exit_px  IS NOT NULL
          AND priced.bench_in IS NOT NULL
          AND priced.bench_out IS NOT NULL
    """, (bench, bench)


def refresh_returns(
    horizons: Sequence[int] = HORIZONS,
    force: bool = False,
    benchmark: str = None,
    dbname: str = None,
) -> dict:
    """Fill the realized T+N returns and the excess over the benchmark. Returns per-horizon counts.

    Idempotent and repeatable: by default only rows still NULL are touched, so running it daily
    fills each horizon exactly once, when its exit day first has bars. ``force`` recomputes
    everything (useful after a bar reload). A non-zero ``still_null`` is normal for recent dates --
    the exit day simply has not happened yet.
    """
    bad = [h for h in horizons if int(h) not in HORIZONS]
    if bad:
        raise ValueError(
            f"unsupported horizon(s) {bad}: only {HORIZONS} have columns in selection_pick"
        )
    bench = benchmark or config.BENCHMARK
    summary: Dict[str, dict] = {}

    with dbc.connection(dbname=dbname) as conn:
        _ensure_schema(conn)
        for n in horizons:
            n = int(n)
            sql, params = _horizon_sql(n, force, bench)
            with conn.cursor() as cur:
                cur.execute(sql, params)
                updated = cur.rowcount
            remaining = dbc.scalar(conn, f"SELECT count(*) FROM selection_pick WHERE ret_t{n} IS NULL")
            summary[f"t{n}"] = {"updated": int(updated), "still_null": int(remaining)}
            logger.info(f"refresh_returns t{n}: updated={updated} still_null={remaining}")
        dbc.log_sync(
            conn,
            task="refresh_returns",
            source="daily_bar",
            status="ok",
            rows_written=sum(v["updated"] for v in summary.values()),
            params={"horizons": [int(h) for h in horizons], "force": force,
                    "benchmark": bench, "detail": summary},
        )
    return summary
