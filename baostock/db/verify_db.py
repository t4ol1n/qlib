# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Verify the ``astock`` database against the local CSV cache it was loaded from.

Every expectation is DERIVED from the files on disk (symbol counts, per-symbol row counts, the
newest membership snapshot, the last date in the index CSV) rather than hardcoded, so the checks
stay valid after the cache is extended.

Check groups:
    structure   extension / tables / views / continuous aggregates / hypertables / policies
    coverage    row counts, symbol counts, date frontier, per-symbol row counts, value spot-check
    scope       the default views must expose ONLY the current CSI300 members (never the 400 exited)
    boards      industry coverage of the current members (a warning until stage 2 has run)
    aggregate   weekly continuous aggregate vs a manual ``time_bucket`` over the same rows
    storage     chunk counts and on-disk sizes

Returns a list of check dicts and exits non-zero when any ``error``-level check fails.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import List

import pandas as pd
from loguru import logger

CUR_DIR = Path(__file__).resolve().parent            # .../baostock/db
PROJECT_DIR = CUR_DIR.parent                          # .../baostock
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import config  # noqa: E402
from db import db_config as dbc  # noqa: E402
from db.load_local import (  # noqa: E402
    MEMBERSHIP_FNAME,
    build_instruments,
    is_index,
    probe_last_date,
    read_daily_stg,
    to_symbol,
)

EXPECTED_TABLES = [
    "instrument", "daily_bar", "index_daily_bar", "stock_board",
    "trade_calendar", "sync_log", "sync_watermark",
]
EXPECTED_VIEWS = [
    "v_instrument_csi300_now", "v_daily_bar_csi300", "v_daily_bar_adj_csi300",
    "v_instrument_all", "v_daily_bar_all", "v_daily_bar_adj_all", "v_daily_bar_trading",
    "v_industry_latest", "v_board_latest", "v_index_membership_latest",
]
EXPECTED_CAGGS = ["daily_bar_weekly", "daily_bar_monthly"]
EXPECTED_HYPERTABLES = ["daily_bar", "index_daily_bar"]
SAMPLE_SYMBOLS = 10
SAMPLE_SEED = 20240904


class Report:
    """Collects check outcomes and renders them as a table."""

    def __init__(self):
        self.checks: List[dict] = []

    def add(self, group: str, name: str, ok: bool, detail: str = "", level: str = "error") -> bool:
        self.checks.append({"group": group, "name": name, "ok": ok, "detail": detail, "level": level})
        return ok

    def check(self, group: str, name: str, expected, actual, level: str = "error") -> bool:
        ok = expected == actual
        return self.add(group, name, ok, f"expected={expected!r} actual={actual!r}", level)

    @property
    def failures(self) -> List[dict]:
        return [c for c in self.checks if not c["ok"] and c["level"] == "error"]

    @property
    def warnings(self) -> List[dict]:
        return [c for c in self.checks if not c["ok"] and c["level"] == "warn"]

    def render(self) -> str:
        lines = []
        for group in dict.fromkeys(c["group"] for c in self.checks):
            lines.append(f"[{group}]")
            for c in (x for x in self.checks if x["group"] == group):
                mark = "PASS" if c["ok"] else ("WARN" if c["level"] == "warn" else "FAIL")
                lines.append(f"  {mark}  {c['name']}" + (f"  -- {c['detail']}" if c["detail"] and not c["ok"] else ""))
        lines.append(f"total {len(self.checks)} checks, {len(self.failures)} failed, {len(self.warnings)} warning(s)")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Local expectations
# --------------------------------------------------------------------------- #
def _raw_files(raw_dir: Path):
    files = sorted(p for p in raw_dir.glob("*.csv") if not p.name.startswith("_"))
    idx = [p for p in files if is_index(p.stem)]
    stk = [p for p in files if not is_index(p.stem)]
    return idx, stk


def _membership_expectations(raw_dir: Path) -> dict:
    """Derive the scope expectations (union size, current-member size, latest snapshot) from disk."""
    membership_path = raw_dir / MEMBERSHIP_FNAME
    m = pd.read_csv(membership_path, dtype={"code": str})
    m["date"] = pd.to_datetime(m["date"], format="mixed")
    latest = m["date"].max()
    current = set(m.loc[m["date"] == latest, "code"].map(to_symbol))
    union = set(m["code"].map(to_symbol))
    return {
        "latest_snapshot": latest.date(),
        "current_members": current,
        "union": union,
        "exited": sorted(union - current),
    }


# --------------------------------------------------------------------------- #
# Check groups
# --------------------------------------------------------------------------- #
def _check_structure(conn, rep: Report) -> None:
    ext = dbc.scalar(conn, "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    rep.add("structure", "timescaledb extension installed", bool(ext), f"version={ext}")

    tables = {r["name"] for r in dbc.fetch_rows(
        conn,
        """
        SELECT c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
        """,
    )}
    missing = [t for t in EXPECTED_TABLES if t not in tables]
    rep.add("structure", f"all {len(EXPECTED_TABLES)} tables present", not missing, f"missing={missing}")

    views = {r["name"] for r in dbc.fetch_rows(
        conn,
        """
        SELECT c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'v'
        """,
    )}
    missing_v = [v for v in EXPECTED_VIEWS if v not in views]
    rep.add("structure", f"all {len(EXPECTED_VIEWS)} views present", not missing_v, f"missing={missing_v}")

    caggs = {r["view_name"] for r in dbc.fetch_rows(
        conn, "SELECT view_name FROM timescaledb_information.continuous_aggregates")}
    missing_c = [c for c in EXPECTED_CAGGS if c not in caggs]
    rep.add("structure", f"{len(EXPECTED_CAGGS)} continuous aggregates present", not missing_c, f"missing={missing_c}")

    hypers = {r["hypertable_name"]: r for r in dbc.fetch_rows(
        conn,
        "SELECT hypertable_name, num_chunks FROM timescaledb_information.hypertables")}
    missing_h = [h for h in EXPECTED_HYPERTABLES if h not in hypers]
    rep.add("structure", f"{len(EXPECTED_HYPERTABLES)} hypertables present", not missing_h, f"missing={missing_h}")

    compressed = {r["hypertable_name"] for r in dbc.fetch_rows(
        conn, "SELECT hypertable_name FROM timescaledb_information.hypertables WHERE compression_enabled")}
    rep.add("structure", "compression enabled on both hypertables",
            set(EXPECTED_HYPERTABLES) <= compressed, f"enabled={sorted(compressed)}")

    policies = dbc.fetch_rows(
        conn,
        """
        SELECT proc_name, count(*) AS n FROM timescaledb_information.jobs
        WHERE proc_name IN ('policy_compression', 'policy_refresh_continuous_aggregate')
        GROUP BY proc_name
        """,
    )
    by_proc = {r["proc_name"]: r["n"] for r in policies}
    rep.check("structure", "compression policies running", 2, by_proc.get("policy_compression", 0))
    rep.check("structure", "cagg refresh policies running", 2,
              by_proc.get("policy_refresh_continuous_aggregate", 0))


def _check_coverage(conn, rep: Report, raw_dir: Path, expect: dict, stock_files: List[Path],
                    index_files: List[Path], sample: int = SAMPLE_SYMBOLS) -> None:
    rep.check("coverage", "instrument rows (union + index)",
              len(expect["union"]) + len(index_files), dbc.scalar(conn, "SELECT count(*) FROM instrument"))
    rep.check("coverage", "daily_bar distinct symbols",
              len(stock_files), dbc.scalar(conn, "SELECT count(DISTINCT symbol) FROM daily_bar"))
    rep.check("coverage", "index_daily_bar distinct symbols",
              len(index_files), dbc.scalar(conn, "SELECT count(DISTINCT symbol) FROM index_daily_bar"))

    # Date frontier: the newest row in the index cache is the newest row the DB should hold.
    expected_max = max(probe_last_date(p) for p in index_files) if index_files else None
    rep.check("coverage", "max(trade_date) == index cache frontier",
              expected_max, dbc.scalar(conn, "SELECT max(trade_date) FROM daily_bar"))
    rep.check("coverage", "index max(trade_date)",
              expected_max, dbc.scalar(conn, "SELECT max(trade_date) FROM index_daily_bar"))

    rows_db = dbc.scalar(conn, "SELECT count(*) FROM daily_bar")
    rep.add("coverage", "daily_bar not empty", bool(rows_db), f"rows={rows_db}")

    # Suspended days are kept verbatim, so the trading view must be smaller (or equal).
    trading = dbc.scalar(conn, "SELECT count(*) FROM v_daily_bar_trading")
    rep.add("coverage", "v_daily_bar_trading <= daily_bar", trading <= rows_db,
            f"trading={trading} all={rows_db}")

    # Per-symbol row counts against the raw CSV, on a deterministic sample (edges + random middle).
    picks = _sample_files(stock_files, sample)
    mismatched = []
    for path in picks:
        want = len(read_daily_stg(path))
        got = dbc.scalar(conn, "SELECT count(*) FROM daily_bar WHERE symbol = %s", (to_symbol(path.stem),))
        if want != got:
            mismatched.append(f"{path.stem}: csv={want} db={got}")
    rep.add("coverage", f"row counts match raw CSV for {len(picks)} sampled symbols",
            not mismatched, "; ".join(mismatched))

    # Value spot-check: the newest bar of the first stock symbol must equal the CSV exactly.
    spot = stock_files[0]
    df = read_daily_stg(spot)
    last = df.iloc[-1]
    row = dbc.fetch_rows(
        conn,
        "SELECT trade_date, close, volume, factor FROM daily_bar WHERE symbol = %s ORDER BY trade_date DESC LIMIT 1",
        (to_symbol(spot.stem),),
    )
    if row:
        r = row[0]
        same = (r["trade_date"] == last["trade_date"]
                and _close_enough(r["close"], last["close"])
                and _close_enough(r["factor"], last["factor"])
                and int(r["volume"] or 0) == int(last["volume"] or 0))
        rep.add("coverage", f"{spot.stem} latest bar matches raw CSV", bool(same),
                f"db={dict(r)} csv_date={last['trade_date']} csv_close={last['close']}")
    else:
        rep.add("coverage", f"{spot.stem} latest bar matches raw CSV", False, "symbol absent from daily_bar")

    cal_days = dbc.scalar(conn, "SELECT count(*) FROM trade_calendar WHERE is_trading_day = 1")
    rep.add("coverage", "trade_calendar populated", bool(cal_days), f"trading_days={cal_days}")

    wm = dbc.scalar(conn, "SELECT count(*) FROM sync_watermark WHERE dataset = 'daily_bar'")
    rep.check("coverage", "sync_watermark covers every daily_bar symbol", len(stock_files), wm)


def _check_scope(conn, rep: Report, expect: dict) -> None:
    """The default query surface must show exactly the current CSI300 members -- never the 400 exited."""
    latest = expect["latest_snapshot"]
    current, exited = expect["current_members"], expect["exited"]

    inst_now = {r["symbol"] for r in dbc.fetch_rows(conn, "SELECT symbol FROM v_instrument_csi300_now")}
    rep.check("scope", f"v_instrument_csi300_now == current members ({latest})", len(current), len(inst_now))
    rep.add("scope", "v_instrument_csi300_now membership identical", inst_now == current,
            f"extra={sorted(inst_now - current)[:5]} missing={sorted(current - inst_now)[:5]}")

    bar_syms = {r["symbol"] for r in dbc.fetch_rows(conn, "SELECT DISTINCT symbol FROM v_daily_bar_csi300")}
    rep.check("scope", "v_daily_bar_csi300 distinct symbols", len(current), len(bar_syms))

    adj_syms = {r["symbol"] for r in dbc.fetch_rows(conn, "SELECT DISTINCT symbol FROM v_daily_bar_adj_csi300")}
    rep.check("scope", "v_daily_bar_adj_csi300 distinct symbols", len(current), len(adj_syms))

    leaked = sorted((inst_now | bar_syms | adj_syms) & set(exited))
    rep.add("scope", f"no exited symbol leaks into default views ({len(exited)} exited)",
            not leaked, f"leaked={leaked[:10]}")

    # The view has no is_index column (it only ever holds stocks), so test the index prefixes directly.
    no_index = dbc.scalar(
        conn,
        """
        SELECT count(*) FROM v_instrument_csi300_now
        WHERE symbol LIKE 'SH000%%' OR symbol LIKE 'SZ399%%'
        """,
    )
    rep.check("scope", "default instrument view excludes index rows", 0, no_index)

    rep.check("scope", "v_instrument_all covers union + index",
              len(expect["union"]) + 1, dbc.scalar(conn, "SELECT count(*) FROM v_instrument_all"))
    all_syms = dbc.scalar(conn, "SELECT count(DISTINCT symbol) FROM v_daily_bar_all")
    rep.check("scope", "v_daily_bar_all covers the full union", len(expect["union"]), all_syms)

    # The exited symbols must still be in the raw table (they are what makes backtests unbiased).
    kept = dbc.scalar(
        conn, "SELECT count(DISTINCT symbol) FROM daily_bar WHERE symbol = ANY(%s)", (exited,))
    rep.check("scope", "exited symbols retained in daily_bar", len(exited), kept)


def _check_boards(conn, rep: Report, expect: dict) -> None:
    """Industry coverage of the current members; a warning (not a failure) before stage 2 runs."""
    total = dbc.scalar(conn, "SELECT count(*) FROM stock_board")
    if total == 0:
        rep.add("boards", "stock_board populated", False,
                "empty -- run `run_db.py sync-sector` (stage 2)", level="warn")
        return
    rep.add("boards", "stock_board populated", True, f"rows={total}")

    covered = dbc.scalar(
        conn,
        """
        SELECT count(*) FROM instrument i
        WHERE i.is_csi300_now
          AND EXISTS (SELECT 1 FROM stock_board b WHERE b.symbol = i.symbol AND b.board_type = 'industry')
        """,
    )
    rep.check("boards", "every current CSI300 member has an industry", len(expect["current_members"]), covered)

    all_covered = dbc.scalar(
        conn,
        """
        SELECT count(*) FROM instrument i
        WHERE NOT i.is_index
          AND EXISTS (SELECT 1 FROM stock_board b WHERE b.symbol = i.symbol AND b.board_type = 'industry')
        """,
    )
    # baostock answers with an EMPTY industry for some long-delisted members (measured: sh.600005
    # 武钢股份, withdrawn in 2017), so union-wide coverage is a data-source limit rather than a
    # pipeline defect: warn instead of failing the run. The current members checked above are the
    # ones that genuinely must be covered.
    rep.check("boards", "every union symbol has an industry", len(expect["union"]), all_covered, level="warn")

    latest_view = dbc.scalar(conn, "SELECT count(*) FROM v_industry_latest")
    rep.add("boards", "v_industry_latest non-empty", bool(latest_view), f"rows={latest_view}")


def _check_aggregate(conn, rep: Report, stock_files: List[Path]) -> None:
    """The weekly continuous aggregate must equal a manual time_bucket over the same rows."""
    symbol = to_symbol(stock_files[0].stem)
    cagg = dbc.fetch_rows(
        conn,
        """
        SELECT count(*) AS weeks, coalesce(sum(volume), 0) AS vol,
               coalesce(max(high), 0) AS hi, coalesce(min(low), 1e18) AS lo
        FROM daily_bar_weekly WHERE symbol = %s
        """,
        (symbol,),
    )[0]
    manual = dbc.fetch_rows(
        conn,
        """
        SELECT count(*) AS weeks, coalesce(sum(v), 0) AS vol, coalesce(max(hi), 0) AS hi,
               coalesce(min(lo), 1e18) AS lo
        FROM (
            SELECT time_bucket(INTERVAL '1 week', trade_date) AS b,
                   sum(volume) AS v, max(high) AS hi, min(low) AS lo
            FROM daily_bar WHERE symbol = %s GROUP BY 1
        ) t
        """,
        (symbol,),
    )[0]
    same = (cagg["weeks"] == manual["weeks"] and int(cagg["vol"]) == int(manual["vol"])
            and _close_enough(cagg["hi"], manual["hi"]) and _close_enough(cagg["lo"], manual["lo"]))
    rep.add("aggregate", f"daily_bar_weekly == manual time_bucket ({symbol})", bool(same),
            f"cagg={dict(cagg)} manual={dict(manual)}")

    months = dbc.scalar(conn, "SELECT count(*) FROM daily_bar_monthly")
    rep.add("aggregate", "daily_bar_monthly materialised", bool(months), f"rows={months}")


def _check_storage(conn, rep: Report) -> None:
    for table in EXPECTED_HYPERTABLES:
        size = dbc.scalar(conn, f"SELECT pg_size_pretty(hypertable_size('{table}'::regclass))")
        chunks = dbc.scalar(
            conn,
            "SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = %s",
            (table,),
        )
        compressed = dbc.scalar(
            conn,
            "SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name = %s AND is_compressed",
            (table,),
        )
        rep.add("storage", f"{table}: {chunks} chunk(s), {compressed} compressed, {size}", True,
                f"chunks={chunks} compressed={compressed} size={size}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _sample_files(files: List[Path], n: int) -> List[Path]:
    """Deterministic sample: first 3 + last 3 + seeded random from the middle."""
    if len(files) <= n:
        return list(files)
    middle = files[3:-3]
    rng = random.Random(SAMPLE_SEED)
    picks = files[:3] + files[-3:]
    picks += rng.sample(middle, max(0, n - len(picks)))
    return sorted(set(picks), key=lambda p: p.stem)


def _close_enough(a, b, tol: float = 1e-6) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if pd.isna(fa) and pd.isna(fb):
        return True
    return abs(fa - fb) <= tol * max(1.0, abs(fa), abs(fb))


def verify_db(raw_dir=None, sample: int = SAMPLE_SYMBOLS, dbname: str = None, quiet: bool = False) -> Report:
    """Run every check group and return the :class:`Report`."""
    raw_dir = Path(raw_dir or config.RAW_DIR)
    if not raw_dir.exists():
        raise FileNotFoundError(f"raw cache directory not found: {raw_dir}")
    index_files, stock_files = _raw_files(raw_dir)
    if not stock_files:
        raise FileNotFoundError(f"no raw stock CSV in {raw_dir}; run the download step first")
    expect = _membership_expectations(raw_dir)
    logger.info(f"expectations from disk: union={len(expect['union'])}, "
                f"current={len(expect['current_members'])}, exited={len(expect['exited'])}, "
                f"latest_snapshot={expect['latest_snapshot']}")

    rep = Report()
    with dbc.connection(dbname=dbname) as conn:
        _check_structure(conn, rep)
        _check_coverage(conn, rep, raw_dir, expect, stock_files, index_files, sample)
        _check_scope(conn, rep, expect)
        _check_boards(conn, rep, expect)
        _check_aggregate(conn, rep, stock_files)
        _check_storage(conn, rep)

    text = rep.render()
    if not quiet:
        logger.info("verification report:\n" + text)
    for c in rep.failures:
        logger.error(f"FAILED [{c['group']}] {c['name']} -- {c['detail']}")
    for c in rep.warnings:
        logger.warning(f"WARN [{c['group']}] {c['name']} -- {c['detail']}")
    logger.info(f"verify: {len(rep.checks)} checks, {len(rep.failures)} failed, {len(rep.warnings)} warning(s)")
    return rep


if __name__ == "__main__":
    import fire

    def _main(**kwargs):
        rep = verify_db(**kwargs)
        sys.exit(1 if rep.failures else 0)

    fire.Fire(_main)
