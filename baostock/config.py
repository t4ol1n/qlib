# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Central configuration for the baostock -> QLib test project.

All values here are defaults; the ``run_*.py`` CLIs accept overrides via fire.
Paths are resolved relative to this file so the project is self-contained and
can be executed from any working directory.
"""
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PROJECT_DIR = Path(__file__).resolve().parent          # .../QLib/baostock
REPO_ROOT = PROJECT_DIR.parent                          # .../QLib
SCRIPTS_DIR = REPO_ROOT / "scripts"                     # reuse dump_bin.py + data_collector

DATA_DIR = PROJECT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"                              # baostock raw CSV (one per symbol)
NORMALIZED_DIR = DATA_DIR / "normalized"                # QLib-schema CSV (one per symbol)
QLIB_BIN_DIR = DATA_DIR / "qlib_bin"                    # QLib .bin dataset (provider_uri)
SECTOR_DIR = DATA_DIR / "sector"                        # baostock sector/metadata cache (db step)
OUTPUT_DIR = PROJECT_DIR / "output"                     # reports, charts, selection, metrics

for _d in (RAW_DIR, NORMALIZED_DIR, QLIB_BIN_DIR, SECTOR_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Universe / benchmark
# --------------------------------------------------------------------------- #
UNIVERSE = "HS300"                 # baostock index constituents used as the stock pool
MARKET = "csi300"                  # QLib instruments file name (instruments/csi300.txt)
BENCHMARK = "SH000300"             # QLib benchmark code (CSI300 index)
INDEX_BAOSTOCK_CODE = "sh.000300"  # baostock code for the CSI300 index


# --------------------------------------------------------------------------- #
# Date windows (download a warm-up year before train for Alpha158 rolling ops)
# --------------------------------------------------------------------------- #
DOWNLOAD_START = "2014-01-01"
# "latest" resolves at runtime to baostock's most recent available trading date (see
# collector.baostock_daily.resolve_latest_data_date), so re-running always fills the cache up to
# the newest day -- what a stock-selection task needs. Pin an explicit "YYYY-MM-DD" to override.
DOWNLOAD_END = "latest"

# Retargeted for a stock-SELECTION task on the most recent data: the test segment (hence the
# prediction / top-K selection) runs through the latest dumped trading day, 2026-09-03. Training
# history starts 2018 -- a balanced depth that gives LightGBM enough samples without the multi-hour
# cost of loading the full 2014-2026 window across all 700 union symbols. To trade runtime vs
# history, move HANDLER_START later (faster) or earlier (more history); the segments follow it.
HANDLER_START = "2018-01-01"
HANDLER_END = "2026-09-03"
FIT_START = "2018-01-01"
FIT_END = "2023-12-31"

SEG_TRAIN = ("2018-01-01", "2023-12-31")
SEG_VALID = ("2024-01-01", "2024-12-31")
SEG_TEST = ("2025-01-01", "2026-09-03")

BACKTEST_START = "2025-01-01"
# NOTE: BACKTEST_END must sit several trading days BEFORE the calendar end (2026-09-03). QLib
# settles each trade day using the next calendar step, so a backtest_end equal to the last
# available date raises IndexError. 2026-08-20 leaves a ~2-week buffer.
BACKTEST_END = "2026-08-20"


# --------------------------------------------------------------------------- #
# Strategy / backtest
# --------------------------------------------------------------------------- #
TOPK = 50
N_DROP = 5
ACCOUNT = 1e8
EXCHANGE_KWARGS = {
    "freq": "day",
    "limit_threshold": 0.095,
    "deal_price": "close",
    "open_cost": 0.0005,
    "close_cost": 0.0015,
    "min_cost": 5,
}


# --------------------------------------------------------------------------- #
# baostock request fields
# --------------------------------------------------------------------------- #
# Raw (unadjusted, adjustflag="3") daily bars for stocks.
DAILY_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,pctChg,isST"
# Post-adjusted (adjustflag="1") close only -> used to derive the per-date factor.
ADJ_CLOSE_FIELDS = "date,code,close"
# Index daily bars (no adjustment for indices).
INDEX_FIELDS = "date,code,open,high,low,close,volume,amount"

# Columns written to the normalized CSV and dumped into QLib .bin.
# Alpha158 needs $open/$high/$low/$close/$volume/$vwap; the backtest also uses $factor.
DUMP_INCLUDE_FIELDS = "open,high,low,close,volume,amount,vwap,factor,change"


# --------------------------------------------------------------------------- #
# Model / dataset (mirrors qlib.tests.config.CSI300_GBDT_TASK with our windows)
# --------------------------------------------------------------------------- #
LGB_MODEL = {
    "class": "LGBModel",
    "module_path": "qlib.contrib.model.gbdt",
    "kwargs": {
        "loss": "mse",
        "colsample_bytree": 0.8879,
        "learning_rate": 0.0421,
        "subsample": 0.8789,
        "lambda_l1": 205.6999,
        "lambda_l2": 580.9768,
        "max_depth": 8,
        "num_leaves": 210,
        "num_threads": 8,
    },
}


def build_task_config(market: str = MARKET) -> dict:
    """Return the QLib task config (model + dataset) for Alpha158 + LightGBM."""
    return {
        "model": LGB_MODEL,
        "dataset": {
            "class": "DatasetH",
            "module_path": "qlib.data.dataset",
            "kwargs": {
                "handler": {
                    "class": "Alpha158",
                    "module_path": "qlib.contrib.data.handler",
                    "kwargs": {
                        "start_time": HANDLER_START,
                        "end_time": HANDLER_END,
                        "fit_start_time": FIT_START,
                        "fit_end_time": FIT_END,
                        "instruments": market,
                    },
                },
                "segments": {
                    "train": list(SEG_TRAIN),
                    "valid": list(SEG_VALID),
                    "test": list(SEG_TEST),
                },
            },
        },
    }


def build_port_analysis_config(benchmark: str = BENCHMARK) -> dict:
    """Return the PortAnaRecord config (TopkDropoutStrategy + backtest)."""
    return {
        "strategy": {
            "class": "TopkDropoutStrategy",
            "module_path": "qlib.contrib.strategy.signal_strategy",
            "kwargs": {
                "signal": "<PRED>",
                "topk": TOPK,
                "n_drop": N_DROP,
            },
        },
        "backtest": {
            "start_time": BACKTEST_START,
            "end_time": BACKTEST_END,
            "account": ACCOUNT,
            "benchmark": benchmark,
            "exchange_kwargs": dict(EXCHANGE_KWARGS),
        },
    }
