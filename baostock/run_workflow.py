# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Step 2: QLib train / predict / backtest for the baostock HS300 dataset.

Pipeline (mirrors ``examples/workflow_by_code.py``)::

    qlib.init(provider_uri=data/qlib_bin)
    Alpha158 handler (csi300) + LGBModel  ->  model.fit(dataset)
    SignalRecord   -> pred.pkl / label.pkl
    SigAnaRecord   -> IC / ICIR / Rank-IC
    PortAnaRecord  -> TopkDropoutStrategy backtest vs SH000300

Artifacts written to ``output/``:
    selected_stocks_latest.csv   top-K by score on the latest test date (code + Chinese name)
    pred.csv                     full prediction (score) matrix
    metrics.json                 signal + risk-analysis metrics
    model_performance_*.html     group return / IC / auto-correlation charts
    report_*.html                cumulative return vs benchmark chart

Run as a script (never ``python -c`` from the repo root, which would import the
local ``qlib`` source instead of the installed wheel)::

    conda run -n baostock_qlib python baostock/run_workflow.py         # full run (train/predict/backtest)
    conda run -n baostock_qlib python baostock/run_workflow.py show    # re-print selection only

``show`` re-derives the selection (with names) from the cached ``output/pred.csv``: no training, no
``qlib.init``, no baostock call, so it is the cheap way to refresh the table after a completed run.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# mlflow>=3 flags QLib's default file-store backend ('./mlruns') as being in "maintenance
# mode" and raises at R.start() unless this opt-out is set. It MUST be set before qlib
# imports mlflow, so it lives at the very top of the module.
os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")

# Stock names are Chinese and a Windows console defaults to a non-UTF-8 code page, which would
# garble (or fail on) the logged selection table. Re-point the standard streams at UTF-8 first.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):  # noqa: PERF203
        pass

import numpy as np
import pandas as pd
from loguru import logger

PROJECT_DIR = Path(__file__).resolve().parent          # .../QLib/baostock
REPO_ROOT = PROJECT_DIR.parent                          # .../QLib
for _p in (str(PROJECT_DIR), str(REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402

import qlib  # noqa: E402
from qlib.constant import REG_CN  # noqa: E402
from qlib.utils import init_instance_by_config, flatten_dict  # noqa: E402
from qlib.workflow import R  # noqa: E402
from qlib.workflow.record_temp import SignalRecord, SigAnaRecord, PortAnaRecord  # noqa: E402

# Quarter-end HS300 constituent snapshots written by collector.baostock_daily in the download step.
# They carry baostock's ``code_name``, which is where the stock names below come from.
MEMBERSHIP_FNAME = "_hs300_membership.csv"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _num(v):
    """Convert numpy/pandas scalars to JSON-safe python; NaN/inf -> None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if np.isnan(f) or np.isinf(f):
        return None
    return f


def _slug(text: str, default: str = "fig") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return s[:40] or default


def _save_figures(figs, out_dir: Path, prefix: str) -> list:
    """Write each plotly figure to a self-contained interactive HTML file."""
    paths = []
    for i, fig in enumerate(figs or []):
        try:
            title = fig.layout.title.text or ""
        except Exception:  # noqa: BLE001
            title = ""
        name = f"{prefix}_{i:02d}_{_slug(title)}.html" if title else f"{prefix}_{i:02d}.html"
        p = out_dir / name
        fig.write_html(str(p), include_plotlyjs=True)
        paths.append(p)
        logger.info(f"chart -> {p.name}")
    return paths


def _patch_task(task: dict, handler_start, handler_end, fit_start, fit_end, train, valid, test) -> dict:
    """Optionally override the handler windows / segments (used by smoke runs)."""
    h = task["dataset"]["kwargs"]["handler"]["kwargs"]
    segs = task["dataset"]["kwargs"]["segments"]
    if handler_start:
        h["start_time"] = handler_start
    if handler_end:
        h["end_time"] = handler_end
    if fit_start:
        h["fit_start_time"] = fit_start
    if fit_end:
        h["fit_end_time"] = fit_end
    for name, val in (("train", train), ("valid", valid), ("test", test)):
        if val:
            parts = [x.strip() for x in str(val).split(",")]
            if len(parts) != 2:
                raise ValueError(f"--{name} must be 'START,END', got {val!r}")
            segs[name] = parts
    return task


def _load_stock_names(raw_dir=None) -> dict:
    """Map QLib instrument code -> Chinese stock name from the cached membership table.

    Reads ``data/raw/_hs300_membership.csv`` only, so this costs no baostock API call. A symbol
    present in several quarterly snapshots keeps its newest name (companies do get renamed).
    Returns ``{}`` when the cache is missing, in which case names are simply left blank.
    """
    path = Path(raw_dir or config.RAW_DIR) / MEMBERSHIP_FNAME
    if not path.exists():
        logger.warning(f"membership cache {path} not found; stock names left blank")
        return {}
    m = pd.read_csv(path)
    if not {"code", "code_name"}.issubset(m.columns):
        logger.warning(f"{path} has no code/code_name columns; stock names left blank")
        return {}
    m["instrument"] = m["code"].astype(str).str.replace(".", "", regex=False).str.upper()
    m = m[m["code_name"].notna() & (m["code_name"].astype(str).str.strip() != "")]
    if "date" in m.columns:
        m = m.sort_values("date")
    names = m.drop_duplicates("instrument", keep="last").set_index("instrument")["code_name"].astype(str).to_dict()
    logger.info(f"loaded {len(names)} stock names from {path.name}")
    return names


def _export_selection(pred: pd.DataFrame, out_dir: Path, topk: int, names: dict = None):
    """Rank by score on the latest test date -> selected_stocks_latest.csv (code + name)."""
    pred = pred.copy()
    pred.columns = ["score"]
    # pred.pkl carries a MultiIndex already named (datetime, instrument). Select by level
    # NAME (not position) so this is robust to either ordering; do NOT relabel the index.
    idx_names = list(pred.index.names or [])
    dt_level = "datetime" if "datetime" in idx_names else 0
    latest = pred.index.get_level_values(dt_level).max()
    latest_scores = pred.xs(latest, level=dt_level)["score"].dropna().sort_values(ascending=False)
    top = latest_scores.head(topk)
    instruments = top.index.astype(str)
    sel = pd.DataFrame(
        {
            "date": pd.Timestamp(latest).strftime("%Y-%m-%d"),
            "rank": range(1, len(top) + 1),
            "instrument": instruments,
            "name": [(names or {}).get(i, "") for i in instruments],
            "score": [_num(x) for x in top.values],
        }
    )
    # utf-8-sig so Excel (Chinese Windows defaults to GBK) opens the names correctly.
    sel.to_csv(out_dir / "selected_stocks_latest.csv", index=False, encoding="utf-8-sig")
    return sel, latest


def _log_selection(sel: pd.DataFrame, latest) -> None:
    """Print the selection table (rank / code / name / score) to the log."""
    if sel is None or not len(sel):
        logger.warning("selection is empty")
        return
    logger.info(
        f"selected top-{len(sel)} stocks on {pd.Timestamp(latest).date()}:\n"
        + sel.to_string(index=False)
    )


def _export_metrics(out_dir, ic, ric, port_analysis, report_normal, meta) -> dict:
    """Write metrics.json (signal IC + portfolio risk analysis)."""
    metrics = {"meta": meta, "signal": {}, "port_analysis": {}, "backtest": {}}
    if ic is not None and len(ic):
        metrics["signal"]["IC"] = _num(ic.mean())
        metrics["signal"]["ICIR"] = _num(ic.mean() / ic.std()) if ic.std() else None
    if ric is not None and len(ric):
        metrics["signal"]["Rank IC"] = _num(ric.mean())
        metrics["signal"]["Rank ICIR"] = _num(ric.mean() / ric.std()) if ric.std() else None

    if port_analysis is not None:
        series = port_analysis["risk"] if "risk" in port_analysis.columns else port_analysis.iloc[:, 0]
        for idx, val in series.items():
            grp, metric = (idx if isinstance(idx, tuple) else ("risk", idx))
            metrics["port_analysis"].setdefault(str(grp), {})[str(metric)] = _num(val)

    if report_normal is not None and len(report_normal):
        metrics["backtest"]["n_days"] = int(len(report_normal))
        metrics["backtest"]["start"] = str(pd.Timestamp(report_normal.index.min()).date())
        metrics["backtest"]["end"] = str(pd.Timestamp(report_normal.index.max()).date())
        for col in ("return", "bench", "cost", "turnover"):
            if col in report_normal.columns:
                metrics["backtest"][f"total_{col}"] = _num(report_normal[col].sum())

    with open(out_dir / "metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    logger.info(f"metrics -> {out_dir / 'metrics.json'}")
    return metrics


def _export_charts(pred, label, report_normal, out_dir) -> list:
    """Build model-performance + report charts and save as interactive HTML (best-effort).

    QLib's chart helpers import ``plotly.figure_factory.create_distplot``, which was removed in
    plotly>=6. If that import fails we log a warning and skip the charts rather than aborting
    the whole run (selection + metrics are the critical outputs). Pin ``plotly<6`` to get charts.
    """
    try:
        from qlib.contrib.report.analysis_model.analysis_model_performance import model_performance_graph
        from qlib.contrib.report.analysis_position.report import report_graph
    except Exception as e:  # noqa: BLE001
        logger.warning(f"chart import failed (is plotly<6 installed?): {e}; skipping charts")
        return []

    pred = pred.copy()
    pred.columns = ["score"]
    pred_label = pred
    if label is not None:
        label = label.copy()
        label.columns = ["label"]
        pred_label = pred.join(label, how="left")

    paths = []
    try:
        figs = model_performance_graph(pred_label, show_notebook=False)
        paths += _save_figures(figs, out_dir, "model_performance")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"model_performance_graph failed: {e}")

    if report_normal is not None:
        try:
            figs = report_graph(report_normal, show_notebook=False)
            paths += _save_figures(figs, out_dir, "report")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"report_graph failed: {e}")
    return paths


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def run_workflow(
    experiment_name: str = "baostock_hs300",
    provider_uri=None,
    market: str = None,
    benchmark: str = None,
    topk: int = None,
    n_drop: int = None,
    account: float = None,
    output_dir=None,
    with_charts: bool = True,
    handler_start: str = None,
    handler_end: str = None,
    fit_start: str = None,
    fit_end: str = None,
    train: str = None,
    valid: str = None,
    test: str = None,
    backtest_start: str = None,
    backtest_end: str = None,
) -> dict:
    """Train/predict/backtest and export selection + metrics + charts to ``output/``."""
    provider_uri = str(Path(provider_uri or config.QLIB_BIN_DIR).expanduser())
    market = market or config.MARKET
    benchmark = benchmark or config.BENCHMARK
    topk = topk or config.TOPK
    n_drop = config.N_DROP if n_drop is None else n_drop
    account = account or config.ACCOUNT
    output_dir = Path(output_dir or config.OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not (Path(provider_uri) / "features").exists():
        raise FileNotFoundError(
            f"QLib .bin data not found at {provider_uri}; run step 1 first: "
            f"`python baostock/run_download.py`"
        )

    qlib.init(provider_uri=provider_uri, region=REG_CN)

    task = _patch_task(
        config.build_task_config(market=market),
        handler_start, handler_end, fit_start, fit_end, train, valid, test,
    )
    pa_config = config.build_port_analysis_config(benchmark=benchmark)
    pa_config["strategy"]["kwargs"]["topk"] = topk
    pa_config["strategy"]["kwargs"]["n_drop"] = n_drop
    pa_config["backtest"]["account"] = account
    if backtest_start:
        pa_config["backtest"]["start_time"] = backtest_start
    if backtest_end:
        pa_config["backtest"]["end_time"] = backtest_end

    model = init_instance_by_config(task["model"])
    dataset = init_instance_by_config(task["dataset"])

    logger.info(f"training LightGBM on Alpha158 ({market}); topk={topk} n_drop={n_drop} benchmark={benchmark}")
    with R.start(experiment_name=experiment_name):
        R.log_params(**flatten_dict(task))
        model.fit(dataset)
        R.save_objects(**{"params.pkl": model})
        recorder = R.get_recorder()

        SignalRecord(model, dataset, recorder).generate()
        sig_arts = SigAnaRecord(recorder).generate() or {}
        port_arts = PortAnaRecord(recorder, pa_config, "day").generate() or {}

        pred = recorder.load_object("pred.pkl")
        try:
            label = recorder.load_object("label.pkl")
        except Exception:  # noqa: BLE001
            label = None
        ic = sig_arts.get("ic.pkl")
        ric = sig_arts.get("ric.pkl")
        report_normal = next((v for k, v in port_arts.items() if k.startswith("report_normal_")), None)
        port_analysis = next((v for k, v in port_arts.items() if k.startswith("port_analysis_")), None)
        exp_id, rec_id = recorder.experiment_id, recorder.id

    pred_out = pred.copy()
    pred_out.columns = ["score"]
    pred_out.to_csv(output_dir / "pred.csv")

    sel, latest = _export_selection(pred, output_dir, topk, _load_stock_names())
    _log_selection(sel, latest)
    logger.info(f"selection -> {output_dir / 'selected_stocks_latest.csv'}")

    meta = {
        "experiment_name": experiment_name,
        "experiment_id": exp_id,
        "recorder_id": rec_id,
        "market": market,
        "benchmark": benchmark,
        "topk": topk,
        "n_drop": n_drop,
        "account": account,
        "segments": task["dataset"]["kwargs"]["segments"],
    }
    metrics = _export_metrics(
        output_dir, ic, ric, port_analysis, report_normal,
        {**meta, "backtest_config": {k: pa_config["backtest"][k] for k in ("start_time", "end_time")}},
    )
    if with_charts:
        _export_charts(pred, label, report_normal, output_dir)

    logger.info("step 2 done: selection + metrics + charts written to output/")
    return {"metrics": metrics, "selection": sel, "recorder_id": rec_id}


def show_selection(pred_csv=None, topk: int = None, out_dir=None) -> pd.DataFrame:
    """Re-export + print the latest-date top-K selection (with names) from a cached ``pred.csv``.

    Deliberately free of ``qlib.init`` / training / baostock access: it only re-reads what a previous
    run already produced, so adding the stock names to an existing result never means retraining.
    """
    out_dir = Path(out_dir or config.OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    # The predictions always come from the canonical run output unless --pred_csv says otherwise;
    # --out_dir only redirects where the refreshed selection CSV lands.
    pred_csv = Path(pred_csv) if pred_csv else Path(config.OUTPUT_DIR) / "pred.csv"
    if not pred_csv.exists():
        raise FileNotFoundError(
            f"no cached predictions at {pred_csv}; run the workflow first: "
            f"`python baostock/run_workflow.py`"
        )
    topk = topk or config.TOPK
    pred = pd.read_csv(pred_csv, index_col=[0, 1])
    sel, latest = _export_selection(pred, out_dir, topk, _load_stock_names())
    _log_selection(sel, latest)
    logger.info(f"selection -> {out_dir / 'selected_stocks_latest.csv'}")
    return sel


if __name__ == "__main__":
    import fire

    if len(sys.argv) > 1 and sys.argv[1] == "show":
        def _show(**kwargs):
            # show_selection returns a DataFrame, which fire would render as a command GROUP (listing
            # its attributes) instead of the table. Swallow it: the table is already logged and the
            # CSV already written by show_selection itself.
            show_selection(**kwargs)

        fire.Fire(_show, command=sys.argv[2:])
    else:
        # Default path unchanged: full train/predict/backtest, with its real signature so that
        # `--help` and the documented `--topk/--market/--train/...` flags keep working.
        fire.Fire(run_workflow)
