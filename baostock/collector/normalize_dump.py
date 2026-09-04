# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Normalize baostock raw CSV into the QLib schema and dump to QLib ``.bin``.

Adjustment math (matches QLib convention ``original = adjusted / factor`` and
``qlib.data_collector.utils.calc_adjusted_price``):

    factor      = post-adjusted close / raw close      (from the collector)
    open/high/low/close = raw * factor                 (adjusted prices)
    vwap        = (amount / volume) * factor           (adjusted vwap)
    volume      = raw_volume / factor                  (keeps price*volume == amount)
    amount      = raw amount (unchanged, real turnover in CNY)
    change      = adjusted close pct_change

The dump step reuses ``scripts/dump_bin.py::DumpDataAll`` which auto-generates
``calendars/day.txt``, ``instruments/all.txt`` and ``features/<symbol>/*.bin``.
``instruments/csi300.txt`` is then written from the HS300 membership table.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

CUR_DIR = Path(__file__).resolve().parent           # .../QLib/baostock/collector
PROJECT_DIR = CUR_DIR.parent                         # .../QLib/baostock
REPO_ROOT = PROJECT_DIR.parent                       # .../QLib
for _p in (str(PROJECT_DIR), str(REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402

OUT_COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "vwap", "factor", "change"]


def normalize_one(raw_path: Path, out_path: Path) -> int:
    """Normalize a single raw symbol CSV; return the number of rows written."""
    df = pd.read_csv(raw_path, dtype={"symbol": str})
    if df.empty:
        return 0
    # The raw cache mixes date-only ("2014-01-02") and datetime ("2026-09-03 00:00:00")
    # strings: the original download wrote date-only rows, while incremental extension
    # tails were concatenated as datetime. pandas>=2 infers a single format from the first
    # row and raises on the mismatch, so parse each element individually.
    df["date"] = pd.to_datetime(df["date"], format="mixed")
    for c in ["open", "high", "low", "close", "volume", "amount", "factor"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # Keep trading rows only (suspended / non-trading days become NaN via the calendar reindex in dump_bin).
    if "tradestatus" in df.columns:
        df = df[pd.to_numeric(df["tradestatus"], errors="coerce") == 1]
    df = df[df["volume"] > 0].dropna(subset=["close"]).sort_values("date")
    df = df.drop_duplicates(subset=["date"], keep="last")   # safety net: never emit duplicate dates
    if df.empty:
        return 0

    factor = df["factor"].replace([np.inf, -np.inf], np.nan).ffill().fillna(1.0)
    close_adj = df["close"] * factor
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap_raw = np.where(df["volume"] > 0, df["amount"] / df["volume"].replace(0, np.nan), np.nan)
    vwap_adj = pd.Series(vwap_raw, index=df.index) * factor
    vwap_adj = vwap_adj.fillna(close_adj)          # fallback when amount missing

    out = pd.DataFrame(
        {
            "symbol": df["symbol"].astype(str).str.upper().values,
            "date": df["date"].dt.strftime("%Y-%m-%d").values,
            "open": (df["open"] * factor).values,
            "high": (df["high"] * factor).values,
            "low": (df["low"] * factor).values,
            "close": close_adj.values,
            "volume": (df["volume"] / factor).values,
            "amount": df["amount"].values,
            "vwap": vwap_adj.values,
            "factor": factor.values,
        }
    )
    out["change"] = out["close"].pct_change().fillna(0.0)
    out = out[OUT_COLUMNS]
    out.to_csv(out_path, index=False)
    return len(out)


def normalize_all(raw_dir=None, normalized_dir=None) -> int:
    """Normalize every raw symbol CSV (skipping helper files like the membership table)."""
    raw_dir = Path(raw_dir or config.RAW_DIR)
    normalized_dir = Path(normalized_dir or config.NORMALIZED_DIR)
    normalized_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in raw_dir.glob("*.csv") if not p.name.startswith("_"))
    if not files:
        raise FileNotFoundError(f"no raw CSV found in {raw_dir}; run the download step first")
    total, ok = 0, 0
    for p in files:
        n = normalize_one(p, normalized_dir.joinpath(p.name))
        if n > 0:
            ok += 1
            total += n
        else:
            logger.warning(f"{p.name}: empty after normalization (skipped)")
    logger.info(f"normalized {ok}/{len(files)} symbols, {total} rows -> {normalized_dir}")
    return ok


def dump_qlib(normalized_dir=None, qlib_bin_dir=None, include_fields=None, max_workers: int = 4) -> Path:
    """Convert normalized CSV to QLib .bin via scripts/dump_bin.py::DumpDataAll."""
    normalized_dir = Path(normalized_dir or config.NORMALIZED_DIR)
    qlib_bin_dir = Path(qlib_bin_dir or config.QLIB_BIN_DIR)
    include_fields = include_fields or config.DUMP_INCLUDE_FIELDS
    from dump_bin import DumpDataAll  # reused from the QLib repo scripts

    logger.info(f"dumping .bin from {normalized_dir} -> {qlib_bin_dir} (fields={include_fields})")
    DumpDataAll(
        data_path=str(normalized_dir),
        qlib_dir=str(qlib_bin_dir),
        include_fields=include_fields,
        max_workers=max_workers,
        date_field_name="date",
        symbol_field_name="symbol",
        file_suffix=".csv",
    ).dump()
    return qlib_bin_dir


def build_csi300_instruments(qlib_bin_dir=None, membership_path=None, download_end: str = None) -> Path:
    """Write instruments/csi300.txt from the HS300 membership snapshots.

    Each symbol gets a single [first_seen, last_seen] span; symbols present in the
    latest snapshot are extended to ``download_end``. This is a pragmatic
    approximation of index membership (see README for the survivorship caveat).
    """
    qlib_bin_dir = Path(qlib_bin_dir or config.QLIB_BIN_DIR)
    membership_path = Path(membership_path or (config.RAW_DIR / "_hs300_membership.csv"))
    # Resolve the instrument end date: prefer the explicit value, else the last dumped calendar
    # date (robust to config.DOWNLOAD_END being the "latest" sentinel, which pd.Timestamp can't parse).
    if download_end in (None, "", "latest", "auto"):
        cal_path = qlib_bin_dir / "calendars" / "day.txt"
        if cal_path.exists():
            download_end = cal_path.read_text(encoding="utf-8").strip().splitlines()[-1]
        else:
            download_end = pd.Timestamp.now().strftime("%Y-%m-%d")
    download_end = pd.Timestamp(download_end)
    if not membership_path.exists():
        logger.warning(f"membership file {membership_path} not found; skipping csi300.txt")
        return None

    membership = pd.read_csv(membership_path, dtype={"code": str})
    membership["date"] = pd.to_datetime(membership["date"])
    last_snapshot = membership["date"].max()

    # Only reference symbols that actually have feature bins, so csi300.txt stays a
    # subset of all.txt. This matters for smoke runs where --limit-nums truncates the
    # download: listing membership symbols without data would break D.features().
    features_dir = qlib_bin_dir / "features"
    available = {p.name.upper() for p in features_dir.glob("*") if p.is_dir()} if features_dir.exists() else set()

    rows = []
    for code, g in membership.groupby("code"):
        symbol = str(code).replace(".", "").upper()
        if available and symbol not in available:
            continue
        start = g["date"].min()
        end = g["date"].max()
        if end >= last_snapshot:
            end = max(end, download_end)
        rows.append((symbol, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")))

    if not rows:
        logger.warning("no membership symbol matched the dumped features; csi300.txt will be empty")

    inst_dir = qlib_bin_dir / "instruments"
    inst_dir.mkdir(parents=True, exist_ok=True)
    out_path = inst_dir / "csi300.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        for sym, s, e in sorted(rows):
            f.write(f"{sym}\t{s}\t{e}\n")
    logger.info(f"wrote {len(rows)} symbols -> {out_path}")
    return out_path


def run_normalize_dump(
    raw_dir=None, normalized_dir=None, qlib_bin_dir=None, max_workers: int = 4, download_end: str = None
) -> Path:
    """Full step 2: normalize -> dump .bin -> build csi300 instruments."""
    normalize_all(raw_dir, normalized_dir)
    qlib_bin_dir = dump_qlib(normalized_dir, qlib_bin_dir, max_workers=max_workers)
    build_csi300_instruments(qlib_bin_dir, download_end=download_end)
    return Path(qlib_bin_dir)


if __name__ == "__main__":
    import fire

    fire.Fire(run_normalize_dump)
