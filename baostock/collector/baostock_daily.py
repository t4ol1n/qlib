# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Download HS300 daily bars (+ CSI300 index) from baostock into per-symbol raw CSV.

The collector reuses QLib's ``data_collector.base.BaseCollector`` for the download
loop (login/retry/save-per-symbol) and adds:

* HS300 constituent snapshots (quarter-end) -> the union universe to download plus a
  membership table used later to build ``instruments/csi300.txt``.
* Per-symbol adjustment ``factor`` derived empirically from baostock post-adjusted
  close (``adjustflag="1"``) divided by raw close (``adjustflag="3"``). Storing the
  factor lets the normalize step control all adjustment math explicitly.
* A dedicated CSI300 index download (no adjustment; ``factor=1``).

baostock keeps a single global login session, so downloads MUST run sequentially
(``max_workers=1``); joblib uses the sequential backend when ``n_jobs=1``.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd
import baostock as bs
from loguru import logger
from tqdm import tqdm

CUR_DIR = Path(__file__).resolve().parent           # .../baostock/collector
PROJECT_DIR = CUR_DIR.parent                         # .../baostock
REPO_ROOT = PROJECT_DIR.parent                       # .../QLib
for _p in (str(PROJECT_DIR), str(REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402
from data_collector.base import BaseCollector  # noqa: E402
from qlib.utils import code_to_fname  # noqa: E402

MEMBERSHIP_FNAME = "_hs300_membership.csv"


def resolve_latest_data_date(probe_code: str = None, lookback_days: int = 60) -> str:
    """Return the most recent trading date for which baostock actually has data.

    Probes the CSI300 index over the last ``lookback_days`` and takes the max date that has a
    row (baostock only returns dates that exist), so this is robust to weekends/holidays and to
    the machine clock running ahead of the data frontier. Falls back to today's date on error.
    """
    probe_code = probe_code or config.INDEX_BAOSTOCK_CODE
    today = pd.Timestamp.now().normalize()
    end = today.strftime("%Y-%m-%d")
    start = (today - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    logged_in = False
    try:
        lg = bs.login()
        logged_in = lg.error_code == "0"
        rs = bs.query_history_k_data_plus(
            probe_code, "date,close", start_date=start, end_date=end, frequency="d", adjustflag="3"
        )
        dates = []
        while rs.error_code == "0" and rs.next():
            dates.append(rs.get_row_data()[0])
        if dates:
            latest = pd.to_datetime(dates).max().strftime("%Y-%m-%d")
            logger.info(f"resolved latest baostock data date: {latest} (probe {probe_code}, window {start}..{end})")
            return latest
        logger.warning(f"probe {probe_code} returned no rows over {start}..{end}; falling back to today {end}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"resolve_latest_data_date failed ({e}); falling back to today {end}")
    finally:
        if logged_in:
            try:
                bs.logout()
            except Exception:  # noqa: BLE001
                pass
    return end


class BaostockDailyCollector(BaseCollector):
    """Collect baostock daily (1d) data for the HS300 universe + CSI300 index."""

    def __init__(
        self,
        save_dir,
        start=None,
        end=None,
        interval: str = "1d",
        max_workers: int = 1,
        max_collector_count: int = 2,
        delay: float = 0.1,
        check_data_length: int = None,
        limit_nums: int = None,
        universe: str = "HS300",
        redownload: bool = False,
    ):
        self.universe = universe
        # Reuse the on-disk raw cache to minimise baostock calls: a symbol whose CSV already
        # exists is skipped entirely (see _simple_collector). Set redownload=True to force a
        # refresh of the whole universe.
        self.redownload = redownload
        self._skipped = 0
        # baostock requires a login before any query (including get_instrument_list,
        # which BaseCollector.__init__ calls).
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_code} {lg.error_msg}")
        super().__init__(
            save_dir=save_dir,
            start=start,
            end=end,
            interval=interval,
            max_workers=max_workers,          # keep 1: single baostock session
            max_collector_count=max_collector_count,
            delay=delay,
            check_data_length=check_data_length,
            limit_nums=limit_nums,
        )

    # ------------------------------------------------------------------ #
    # Universe / membership
    # ------------------------------------------------------------------ #
    @property
    def membership_path(self) -> Path:
        return self.save_dir.joinpath(MEMBERSHIP_FNAME)

    def _quarter_end_trade_dates(self, start: str, end: str) -> List[str]:
        """Return the last trading day of each year-quarter in [start, end]."""
        rs = bs.query_trade_dates(start_date=start, end_date=end)
        rows = []
        while (rs.error_code == "0") and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return []
        cal = pd.DataFrame(rows, columns=rs.fields)
        cal = cal[cal["is_trading_day"] == "1"].copy()
        cal["calendar_date"] = pd.to_datetime(cal["calendar_date"])
        cal["yq"] = cal["calendar_date"].dt.year.astype(str) + "Q" + cal["calendar_date"].dt.quarter.astype(str)
        q_ends = cal.groupby("yq")["calendar_date"].max().sort_values()
        return [d.strftime("%Y-%m-%d") for d in q_ends]

    def get_hs300_membership(self) -> Tuple[List[str], pd.DataFrame]:
        """Sample HS300 constituents at quarter-ends -> (union codes, membership df)."""
        start = self.start_datetime.strftime("%Y-%m-%d")
        end = self.end_datetime.strftime("%Y-%m-%d")
        dates = self._quarter_end_trade_dates(start, end)
        logger.info(f"querying HS300 membership on {len(dates)} quarter-end dates ...")
        records = []
        for d in tqdm(dates, desc="hs300 snapshots"):
            rs = bs.query_hs300_stocks(date=d)
            while (rs.error_code == "0") and rs.next():
                row = rs.get_row_data()          # [updateDate, code, code_name]
                records.append({"date": d, "code": row[1], "code_name": row[2] if len(row) > 2 else ""})
            time.sleep(self.delay)
        membership = pd.DataFrame(records, columns=["date", "code", "code_name"])
        codes = sorted(set(membership["code"].tolist()))
        return codes, membership

    def get_instrument_list(self) -> List[str]:
        codes, membership = self.get_hs300_membership()
        membership.to_csv(self.membership_path, index=False)
        logger.info(f"HS300 union universe: {len(codes)} symbols; membership -> {self.membership_path}")
        return codes

    def normalize_symbol(self, symbol: str) -> str:
        # baostock "sh.600000" -> QLib "SH600000"
        return str(symbol).replace(".", "").upper()

    def _raw_path(self, symbol: str) -> Path:
        return self.save_dir.joinpath(f"{code_to_fname(self.normalize_symbol(symbol))}.csv")

    def _cached_last_date(self, symbol: str):
        """Last date already present in a symbol's raw cache, or None if uncached/unreadable."""
        path = self._raw_path(symbol)
        if not (path.exists() and path.stat().st_size > 0):
            return None
        try:
            d = pd.read_csv(path, usecols=["date"])
            return pd.to_datetime(d["date"]).max() if not d.empty else None
        except Exception:  # noqa: BLE001
            return None

    def _simple_collector(self, symbol: str):
        """Extend the raw cache incrementally instead of re-downloading it.

        A symbol whose cache already reaches ``end_datetime`` is skipped entirely (zero baostock
        calls). A stale symbol falls through to ``get_data``, which fetches only the missing tail
        (``last_cached_date + 1 .. end``); ``save_instrument`` then concatenates it onto the
        existing CSV. This keeps the cache authoritative, minimises API calls, and makes an
        interrupted download resumable. Pass ``redownload=True`` to force a full refresh.
        """
        if not self.redownload:
            last = self._cached_last_date(symbol)
            if last is not None and last >= self.end_datetime:
                self._skipped += 1
                return self.NORMAL_FLAG
        return super()._simple_collector(symbol)

    def save_instrument(self, symbol, df: pd.DataFrame):
        """Concatenate onto the cache (BaseCollector), then collapse any duplicate dates.

        Incremental fetches start at ``last_cached_date + 1`` so they never overlap; the dedup is
        a safety net for ``redownload=True`` (a full re-fetch concatenated onto the old cache) and
        for any partial tail left by an interrupted run. Keeps the newest row per date.
        """
        super().save_instrument(symbol, df)
        path = self._raw_path(symbol)
        if path.exists():
            cached = pd.read_csv(path)
            if "date" in cached.columns and cached["date"].duplicated().any():
                cached.drop_duplicates(subset=["date"], keep="last").sort_values("date").to_csv(path, index=False)

    # ------------------------------------------------------------------ #
    # Data retrieval
    # ------------------------------------------------------------------ #
    @staticmethod
    def _query_k(
        code: str, fields: str, start: str, end: str, adjustflag: str = "3", frequency: str = "d", _retries: int = 2
    ) -> pd.DataFrame:
        """Query K-line data with a light re-login retry on session errors."""
        for attempt in range(_retries):
            rs = bs.query_history_k_data_plus(
                code, fields, start_date=start, end_date=end, frequency=frequency, adjustflag=adjustflag
            )
            if rs.error_code == "0":
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    return pd.DataFrame()
                return pd.DataFrame(rows, columns=rs.fields)
            logger.warning(f"query_history_k_data_plus({code}) failed: {rs.error_code} {rs.error_msg}; retry {attempt + 1}")
            try:
                bs.logout()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
            bs.login()
        return pd.DataFrame()

    @staticmethod
    def _merge_factor(raw: pd.DataFrame, adj: pd.DataFrame) -> pd.DataFrame:
        """Merge raw bars with post-adjusted close and derive the per-date factor."""
        df = raw.copy()
        df["date"] = pd.to_datetime(df["date"])
        if adj is not None and not adj.empty:
            adj = adj.rename(columns={"close": "close_adj"})[["date", "close_adj"]].copy()
            adj["date"] = pd.to_datetime(adj["date"])
            df = df.merge(adj, on="date", how="left")
        else:
            df["close_adj"] = np.nan

        num_cols = ["open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pctChg", "close_adj"]
        for c in num_cols:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")

        # factor = post-adjusted close / raw close  (>=1, ==1 at the earliest date).
        # QLib convention: original_price = adjusted_price / factor  ($close/$factor).
        df["factor"] = df["close_adj"] / df["close"]
        df["factor"] = df["factor"].replace([np.inf, -np.inf], np.nan).ffill().fillna(1.0)
        return df.sort_values("date").reset_index(drop=True)

    def get_data(self, symbol: str, interval: str, start_datetime: pd.Timestamp, end_datetime: pd.Timestamp) -> pd.DataFrame:
        # Incremental: fetch only the dates missing from the cache. ``save_instrument`` concatenates
        # the returned rows onto the existing CSV, so returning just the tail extends it in place.
        # Post-adjustment (adjustflag="1") anchors at each symbol's listing date, so the newly
        # fetched factors stay consistent with the cached ones across the join boundary.
        eff_start = start_datetime
        if not self.redownload:
            last = self._cached_last_date(symbol)
            if last is not None:
                eff_start = max(start_datetime, last + pd.Timedelta(days=1))
                if eff_start > end_datetime:
                    return pd.DataFrame()          # cache already covers the requested window
        start = eff_start.strftime("%Y-%m-%d")
        end = end_datetime.strftime("%Y-%m-%d")
        raw = self._query_k(symbol, config.DAILY_FIELDS, start, end, adjustflag="3")
        if raw.empty:
            return raw
        adj = self._query_k(symbol, config.ADJ_CLOSE_FIELDS, start, end, adjustflag="1")
        return self._merge_factor(raw, adj)

    def download_index(self, index_code: str = None) -> None:
        """Download the CSI300 index as SH000300 (benchmark), factor fixed to 1.

        Incremental: if the index cache already exists, only the missing tail is fetched and
        concatenated (indices are not adjusted, so factor stays 1). ``preclose``/``pctChg`` on the
        first tail row are NaN here, but ``normalize_one`` recomputes ``change`` from the full
        concatenated close series, so the boundary is correct after normalization.
        """
        index_code = index_code or config.INDEX_BAOSTOCK_CODE
        start_dt, end_dt = self.start_datetime, self.end_datetime
        if not self.redownload:
            last = self._cached_last_date(index_code)
            if last is not None:
                if last >= end_dt:
                    logger.info(f"index {self.normalize_symbol(index_code)} already current through {last.date()}; skipping")
                    return
                start_dt = max(start_dt, last + pd.Timedelta(days=1))
        start = start_dt.strftime("%Y-%m-%d")
        end = end_dt.strftime("%Y-%m-%d")
        idx = self._query_k(index_code, config.INDEX_FIELDS, start, end, adjustflag="3")
        if idx.empty:
            logger.warning(f"index {index_code} returned empty for {start}..{end}; benchmark may be missing")
            return
        idx["date"] = pd.to_datetime(idx["date"])
        for c in ["open", "high", "low", "close", "volume", "amount"]:
            idx[c] = pd.to_numeric(idx[c], errors="coerce")
        idx = idx.sort_values("date").reset_index(drop=True)
        idx["preclose"] = idx["close"].shift(1)
        idx["pctChg"] = idx["close"].pct_change() * 100
        idx["turn"] = np.nan
        idx["tradestatus"] = "1"
        idx["isST"] = "0"
        idx["close_adj"] = idx["close"]
        idx["factor"] = 1.0
        self.save_instrument(index_code, idx)
        logger.info(f"saved index {self.normalize_symbol(index_code)}: {len(idx)} rows")


def run_download(
    save_dir=None,
    start: str = None,
    end: str = None,
    delay: float = 0.1,
    limit_nums: int = None,
    universe: str = None,
    max_collector_count: int = 2,
    with_index: bool = True,
    redownload: bool = False,
) -> Path:
    """Entry point: download HS300 daily bars + index into ``save_dir`` (raw CSV).

    The cache is extended incrementally: symbols already reaching ``end`` are skipped (zero
    baostock calls), stale symbols fetch only their missing tail, and ``end="latest"`` resolves to
    baostock's most recent trading date. Pass ``redownload=True`` to force a full refresh.
    """
    save_dir = Path(save_dir or config.RAW_DIR).expanduser()
    start = start or config.DOWNLOAD_START
    end = end or config.DOWNLOAD_END
    if end in (None, "", "latest", "auto"):
        end = resolve_latest_data_date()
    universe = universe or config.UNIVERSE
    save_dir.mkdir(parents=True, exist_ok=True)
    try:
        collector = BaostockDailyCollector(
            save_dir=save_dir,
            start=start,
            end=end,
            interval="1d",
            max_workers=1,
            max_collector_count=max_collector_count,
            delay=delay,
            limit_nums=limit_nums,
            universe=universe,
            redownload=redownload,
        )
        collector.collector_data()
        if collector._skipped:
            logger.info(f"reused cache: skipped {collector._skipped} already-downloaded symbols")
        if with_index:
            collector.download_index()
    finally:
        try:
            bs.logout()
        except Exception:  # noqa: BLE001
            pass
    return save_dir


if __name__ == "__main__":
    import fire

    fire.Fire(run_download)
