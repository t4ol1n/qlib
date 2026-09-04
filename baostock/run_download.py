# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Step 1: download HS300 daily bars (+ CSI300 index) from baostock, normalize to the
QLib schema, and dump to QLib ``.bin``.

    conda run -n baostock_qlib python baostock/run_download.py [--limit-nums N] \
        [--start 2014-01-01] [--end 2024-12-31] [--skip-download] [--skip-dump]

Outputs:
    data/raw/<SYMBOL>.csv          baostock raw bars + derived adjust factor + membership
    data/normalized/<SYMBOL>.csv   QLib-schema adjusted OHLCV
    data/qlib_bin/                 calendars/ instruments/ features/  (provider_uri)
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


def main(
    start: str = None,
    end: str = None,
    limit_nums: int = None,
    delay: float = 0.1,
    max_workers: int = 4,
    with_index: bool = True,
    skip_download: bool = False,
    skip_dump: bool = False,
    universe: str = None,
    redownload: bool = False,
) -> None:
    """Run step 1 (download -> normalize -> dump). Flags let you run parts independently."""
    start = start or config.DOWNLOAD_START
    end = end or config.DOWNLOAD_END
    if end in (None, "", "latest", "auto"):
        from collector.baostock_daily import resolve_latest_data_date

        end = resolve_latest_data_date()
        logger.info(f"resolved download end 'latest' -> {end}")
    universe = universe or config.UNIVERSE

    if not skip_download:
        # Imported lazily so that dump-only runs (and Windows spawn children) never
        # import baostock / the collector unnecessarily.
        from collector.baostock_daily import run_download as _download

        logger.info(f"[1/2] downloading {universe} daily bars {start}..{end} -> {config.RAW_DIR}")
        _download(
            save_dir=config.RAW_DIR,
            start=start,
            end=end,
            delay=delay,
            limit_nums=limit_nums,
            universe=universe,
            with_index=with_index,
            redownload=redownload,
        )
    else:
        logger.info("[1/2] download skipped (--skip-download)")

    if not skip_dump:
        from collector.normalize_dump import run_normalize_dump

        logger.info(f"[2/2] normalize + dump .bin -> {config.QLIB_BIN_DIR}")
        run_normalize_dump(
            raw_dir=config.RAW_DIR,
            normalized_dir=config.NORMALIZED_DIR,
            qlib_bin_dir=config.QLIB_BIN_DIR,
            max_workers=max_workers,
            download_end=end,
        )
    else:
        logger.info("[2/2] dump skipped (--skip-dump)")

    logger.info("step 1 done. Next: python baostock/run_workflow.py")


if __name__ == "__main__":
    import fire

    fire.Fire(main)
