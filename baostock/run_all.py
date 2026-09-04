# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Orchestrate the full pipeline: step 1 (download + normalize + dump) then step 2
(train / predict / backtest + selection / metrics / charts).

    conda run -n baostock_qlib python baostock/run_all.py            # full HS300 2014..2024
    conda run -n baostock_qlib python baostock/run_all.py --fast     # small smoke run

``--fast`` downloads a compressed 3-year window for a handful of symbols and shrinks
the model segments / topk accordingly, so the whole flow can be validated in minutes.
"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

PROJECT_DIR = Path(__file__).resolve().parent          # .../baostock
REPO_ROOT = PROJECT_DIR.parent                          # .../QLib
for _p in (str(PROJECT_DIR), str(REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402

# Fast/smoke preset: compressed timeline + small universe + small topk.
FAST = {
    "download_start": "2018-01-01",
    "download_end": "2020-12-31",
    "limit_nums": 20,
    "handler_start": "2018-01-01",
    "handler_end": "2020-12-31",
    "fit_start": "2018-01-01",
    "fit_end": "2018-12-31",
    "train": "2018-01-01,2018-12-31",
    "valid": "2019-01-01,2019-12-31",
    "test": "2020-01-01,2020-12-31",
    "backtest_start": "2020-01-01",
    # Buffer before the calendar end (download_end=2020-12-31): QLib settles each trade day
    # on the next calendar step, so a backtest_end == last date raises IndexError.
    "backtest_end": "2020-12-15",
    "topk": 10,
    "n_drop": 2,
}


def main(
    fast: bool = False,
    limit_nums: int = None,
    download_start: str = None,
    download_end: str = None,
    experiment_name: str = "baostock_hs300",
    with_charts: bool = True,
    skip_download: bool = False,
    max_workers: int = 4,
    redownload: bool = False,
) -> None:
    # Lazy imports keep this module import-light for Windows spawn children.
    import run_download
    import run_workflow

    if fast:
        download_start = download_start or FAST["download_start"]
        download_end = download_end or FAST["download_end"]
        limit_nums = limit_nums or FAST["limit_nums"]
    download_start = download_start or config.DOWNLOAD_START
    download_end = download_end or config.DOWNLOAD_END

    logger.info("=" * 70)
    logger.info(f"STEP 1/2  download + normalize + dump  ({download_start}..{download_end}, limit_nums={limit_nums})")
    logger.info("=" * 70)
    if not skip_download:
        run_download.main(
            start=download_start,
            end=download_end,
            limit_nums=limit_nums,
            max_workers=max_workers,
            redownload=redownload,
        )
    else:
        run_download.main(start=download_start, end=download_end, skip_download=True, max_workers=max_workers)

    logger.info("=" * 70)
    logger.info("STEP 2/2  train / predict / backtest + selection / metrics / charts")
    logger.info("=" * 70)
    if fast:
        run_workflow.run_workflow(
            experiment_name=f"{experiment_name}_fast",
            topk=FAST["topk"],
            n_drop=FAST["n_drop"],
            with_charts=with_charts,
            handler_start=FAST["handler_start"],
            handler_end=FAST["handler_end"],
            fit_start=FAST["fit_start"],
            fit_end=FAST["fit_end"],
            train=FAST["train"],
            valid=FAST["valid"],
            test=FAST["test"],
            backtest_start=FAST["backtest_start"],
            backtest_end=FAST["backtest_end"],
        )
    else:
        run_workflow.run_workflow(experiment_name=experiment_name, with_charts=with_charts)

    logger.info("ALL DONE. See baostock/output/ for selection, metrics and charts.")


if __name__ == "__main__":
    import fire

    fire.Fire(main)
