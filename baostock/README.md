# Baostock → QLib: HS300 Stock-Selection & Backtest Test Project

A self-contained pipeline that downloads **CSI300 (HS300)** daily data from
[baostock](https://baostock.com), converts it to the **QLib** `.bin` format, then trains a
**LightGBM / Alpha158** model, runs a **TopkDropoutStrategy** backtest against the
**SH000300** benchmark, and exports **stock selection + metrics + interactive charts** and a
**reusable local data cache**.

```
baostock  ──download──►  data/raw/*.csv  ──normalize──►  data/normalized/*.csv
          ──dump_bin──►  data/qlib_bin/{calendars,instruments,features}
          ──qlib.init──►  Alpha158 + LightGBM  ──►  TopkDropoutStrategy backtest
          ──►  output/{selected_stocks_latest.csv, pred.csv, metrics.json, *.html}
```

> QLib targets Python 3.8–3.12 and does **not** import on very new interpreters (e.g. 3.14 /
> pandas 3.0). All execution therefore happens in a dedicated conda env (`baostock_qlib`,
> Python 3.11) with `pyqlib` installed from a **prebuilt wheel** (no C compiler needed).

---

## 1. Prerequisites

- **conda** (Miniconda/Anaconda) on `PATH`.
- Internet access (for `conda`/`pip` and for baostock data).
- The QLib repository (this project lives at `<QLib>/baostock/` and reuses
  `<QLib>/scripts/dump_bin.py` + `<QLib>/scripts/data_collector/`).

## 2. Environment setup (one time)

```powershell
# from the QLib repo root
powershell -ExecutionPolicy Bypass -File baostock\setup_env.ps1
```

This creates `baostock_qlib` (Python 3.11), installs `pyqlib` + dependencies, and runs an
import smoke test. Verify manually at any time:

```powershell
conda run -n baostock_qlib python baostock\smoke_env.py     # prints "SMOKE OK"
```

Expected versions validated for this project: `pyqlib 0.9.7`, `numpy 2.x`, `pandas 2.3.x`,
`lightgbm 4.x`, `baostock 0.9.3`, `plotly 5.x (<6)`. The prebuilt `pyqlib` wheel ships the compiled
`qlib.data._libs.rolling` / `expanding` Cython ops, so **no MSVC build tools are required**.
`plotly` is pinned `<6` because `qlib.contrib.report.graph` imports `create_distplot`, which plotly
6.0 removed (see Troubleshooting).

## 3. Usage

Always invoke the entry scripts **by path** (`python baostock\<script>.py`). Running
`python -c "import qlib"` from the repo root would import the local `qlib/` *source* instead of
the installed wheel and fail with `ModuleNotFoundError: setuptools_scm` (see Troubleshooting).

### One-shot (download → dump → train → backtest → charts)

```powershell
# full run: HS300, 2014-01-01 .. 2024-12-31
conda run -n baostock_qlib python baostock\run_all.py

# fast smoke run: 20 symbols, 2018..2020, topk=10 (validates the whole flow in minutes)
conda run -n baostock_qlib python baostock\run_all.py --fast
```

### Step by step

```powershell
# Step 1 — download + normalize + dump to QLib .bin
conda run -n baostock_qlib python baostock\run_download.py
#   useful flags:
#     --start 2014-01-01 --end 2024-12-31   date window
#     --limit-nums 5                        only the first N symbols (debug)
#     --skip-download / --skip-dump         run just one half
#     --max-workers 4                       dump parallelism
#     --redownload                          ignore the raw cache and re-pull from baostock

# Step 2 — train / predict / backtest + selection / metrics / charts
conda run -n baostock_qlib python baostock\run_workflow.py
#   useful flags:
#     --topk 50 --n-drop 5                  strategy params
#     --market csi300 --benchmark SH000300
#     --train 2014-01-01,2019-12-31 --valid ... --test ...   override segments
#     --with-charts=False                   skip HTML chart export (see the fire note in section 8)

# Step 2b — only re-print / re-export the selection table (code + name) from cached pred.csv.
# No training, no qlib.init, no baostock call: seconds instead of the full run.
conda run -n baostock_qlib python baostock\run_workflow.py show
#   useful flags:  --topk 50   --pred_csv path\to\pred.csv   --out_dir path\to\output
```

### Declarative alternative (`qrun`)

`workflow/lgbm_alpha158.yaml` is the YAML equivalent of `run_workflow.py`. It is provided for
reference; note that `qrun` does **not** emit `selected_stocks_latest.csv`, `metrics.json` or the
HTML charts.

```powershell
conda run -n baostock_qlib qrun baostock\workflow\lgbm_alpha158.yaml
```

## 4. Outputs

| Path | Contents |
| --- | --- |
| `data/raw/<SYMBOL>.csv` | baostock raw (unadjusted) bars + post-adjusted close + derived `factor`; `_hs300_membership.csv` holds quarter-end constituent snapshots |
| `data/normalized/<SYMBOL>.csv` | QLib-schema adjusted `open/high/low/close/volume/amount/vwap/factor/change` |
| `data/qlib_bin/` | QLib dataset (`calendars/day.txt`, `instruments/{all,csi300}.txt`, `features/<symbol>/*.bin`) — the `provider_uri` |
| `output/selected_stocks_latest.csv` | top-K stocks by model score on the latest test date (`date,rank,instrument,name,score`); written as UTF-8-with-BOM so Excel shows the Chinese names |
| `output/pred.csv` | full prediction (score) matrix over the test segment — the cache `run_workflow.py show` re-reads |
| `output/metrics.json` | IC / ICIR / Rank-IC + portfolio risk analysis (annualized excess return w/ & w/o cost, information ratio, max drawdown) |
| `output/model_performance_*.html` | interactive plotly: cumulative group return, IC curve/heatmap/hist, score auto-correlation |
| `output/report_*.html` | interactive plotly: cumulative return vs benchmark, drawdown |
| `mlruns/` | MLflow experiment tracking (created by QLib `R.start`) |

`data/`, `output/` and `mlruns/` are git-ignored (large / regenerable). Open the `.html` charts
in any browser (they are self-contained).

Stock **names** are resolved from the already-downloaded `data/raw/_hs300_membership.csv` (baostock
returns `code_name` with each HS300 constituent snapshot), so naming the selection costs **no extra
baostock API call**. A symbol seen in several snapshots keeps its newest name; a symbol missing from
the cache gets an empty `name` rather than failing the export.

## 5. Expected runtime (rough)

| Stage | HS300 full (2014–2024) | `--fast` (20 symbols, 2018–2020) |
| --- | --- | --- |
| Download (baostock, sequential) | ~15–30 min | ~1–2 min |
| Normalize + dump `.bin` | ~1–3 min | ~10–30 s |
| **Alpha158 data load** (step 2) | **~30 min – 2 h+** | ~3–6 min |
| Train + backtest + charts | ~1–5 min | ~10–30 s |

baostock serves one global login session, so downloads are **sequential** (`max_workers=1`) with a
small inter-request delay. In step 2 the dominant cost is **not** training but the Alpha158 handler
materialising 158 features over the whole `(instruments × days)` grid; qlib 0.9.7 keeps no on-disk
feature cache, so this scales with both the universe and the window and the full 2014–2024 HS300
load can be slow on Windows. Use `--fast` to validate the flow in minutes, or narrow the window
(e.g. `--handler_start 2019-01-01 --fit_start 2019-01-01 --train 2019-01-01,2021-12-31
--valid 2022-01-01,2022-12-31 --test 2023-01-01,2024-12-31 --backtest_start 2023-01-01`) to cut it.

## 6. Data adjustment methodology

QLib stores **adjusted** prices with the convention `original_price = $close / $factor`. Rather
than interpreting baostock's adjust-factor event semantics, the collector derives the factor
**empirically** per date:

```
factor = close(adjustflag="1", post-adjusted) / close(adjustflag="3", raw)     # >= 1
open/high/low/close = raw * factor        vwap = (amount / volume) * factor
volume = raw_volume / factor              amount = raw amount (unchanged)
change = close.pct_change()
```

This keeps `price * volume == amount` and produces continuous post-adjusted series across
dividends/splits. The CSI300 **index** is stored unadjusted (`factor = 1`) as `SH000300`.

## 7. Caveats

- **Index membership is approximate.** Constituents are sampled at **quarter-ends**; each symbol
  gets a single `[first_seen, last_seen]` span in `instruments/csi300.txt`. This ignores
  intra-quarter add/remove events and introduces mild survivorship / look-ahead bias. It is
  sufficient for a test project but not for production-grade backtests.
- The last ~2 test dates have `NaN` labels (the label is `Ref($close,-2)/Ref($close,-1)-1`), which
  is expected; selection ranks by **score**, not label.
- **Names are snapshot-based.** `name` comes from the newest quarter-end membership row for that
  symbol, so a stock renamed after it left the index keeps its older name.
- **`show --topk N` rewrites the CSV.** Like `run --topk N`, it re-exports
  `selected_stocks_latest.csv` with N rows (the table is logged either way); re-run plain `show` to
  restore the configured `TOPK`.

## 8. Troubleshooting

- **`ModuleNotFoundError: setuptools_scm` / importing `F:\...\QLib\qlib\__init__.py`** — you ran
  `python -c "import qlib"` (or a script) from the **repo root**, so the local source shadowed the
  installed wheel. Fix: run the entry scripts **by path** (`python baostock\run_workflow.py`),
  which puts `baostock/` (not the repo root) on `sys.path[0]`. `smoke_env.py` prints the
  resolved `qlib.__file__` to confirm the wheel (`...\site-packages\qlib\...`) is used.
- **`AttributeError: module 'baostock' has no attribute ...`, or baostock calls silently returning
  nothing** — this project folder is *itself* named `baostock` and sits at the repo root. That is
  safe **only** because it has no `__init__.py`: Python prefers a regular package
  (`site-packages/baostock`) over a bare namespace directory at *any* `sys.path` position. Adding an
  `__init__.py` here would make it shadow the real library exactly like `qlib/` does.
  `smoke_env.py` prints the resolved `baostock.__file__` and warns if this folder ever wins.
- **`conda run` dies with `UnicodeEncodeError: 'gbk' codec can't encode ...` even though the child
  script succeeded** — `conda run` captures child stdout and re-prints it through the console code
  page. Either add `--no-capture-output`, or call the env's interpreter directly
  (`& $env:USERPROFILE\.conda\envs\baostock_qlib\python.exe baostock\<script>.py`). Note that
  re-redirecting an already-mangled log a second time (`*> file.log`, then piping it back) produces
  the same error with `\ufffd` as the offending character — read such a log from Python with
  `encoding="utf-16"`, not with `Get-Content`.
- **`ERROR: Could not consume arg: --no-with_charts` (or any `--no-<flag>`)** — these CLIs are built
  with `python-fire`, which does **not** synthesise a `--no-` negation for a parameter that defaults
  to `True`. Turn it off by value instead: `--with-charts=False`, `--with-calendar=False`,
  `--with-index=False`. Flags whose default is `False` *are* plain switches (`--full-refresh`,
  `--skip-basic`, `--quiet`). Hyphens and underscores are interchangeable throughout
  (`--skip-index-history` ≡ `--skip_index_history`), but only the value form is accepted for
  negation. `run_db.py <cmd> -- --help` prints the authoritative flag list.

  This one is **dangerous, not merely annoying**: fire calls the function with the *default* value
  first and only then fails on the leftover argument. So `run_db.py load-local --no-with-calendar`
  runs the **entire** load and exits 2 afterwards — it does not reject the flag up front. Always
  check the log body, not just the exit code.
- **`Gym has been unmaintained ... does not support NumPy 2.0`** — a harmless deprecation warning
  from `gym` (a transitive `pyqlib` dep used only for RL, which this project does not use). It does
  not affect LightGBM / Alpha158 / backtest.
- **`ImportError: cannot import name 'create_distplot' from 'plotly.figure_factory'`** — you have
  `plotly>=6`, but `qlib.contrib.report.graph` still imports `create_distplot` (removed in plotly
  6.0). Fix: `pip install "plotly<6"` (already pinned in `requirements.txt`). `_export_charts`
  degrades gracefully — it logs a warning and skips charts rather than aborting — so selection and
  `metrics.json` are still produced even if the chart import fails.
- **`UnicodeEncodeError: 'gbk' codec can't encode character ...`** — the console code page is GBK
  while the selection table now contains Chinese names. `run_workflow.py` re-points `sys.stdout` /
  `sys.stderr` at UTF-8 (`errors="replace"`) at import, which also fixes the `conda run` relay (it
  decodes child output as UTF-8). For your own scripts set `$env:PYTHONIOENCODING="utf-8"` first.
  Reading the CSV back with `Get-Content` still shows mojibake — it is UTF-8-with-BOM, so open it in
  Excel or `pd.read_csv(..., encoding="utf-8-sig")`.
- **baostock returns empty / `error_code != 0`** — transient session or rate-limit issues; the
  collector re-logs-in and retries. If it persists, increase `--delay` (e.g. `0.3`) and re-run
  (downloads are cached per symbol under `data/raw/`).
- **Dump is slow / hangs on Windows** — `dump_bin` uses `ProcessPoolExecutor` (spawn). Lower
  `--max-workers` (e.g. `1`) if constrained; entry modules are import-guarded so spawn is safe.
- **`csi300.txt` empty or backtest has no trades** — you downloaded fewer symbols than the
  membership list (e.g. `--limit-nums`). `csi300.txt` is intersected with the dumped features; use
  `--fast` (which also shrinks `topk`) or download the full universe.
- **Re-running / caching** — downloads are **cached per symbol**: `_simple_collector` skips any
  symbol whose `data/raw/<SYMBOL>.csv` already exists, so re-runs and resumes make **no extra
  baostock calls** for cached symbols (this also avoids `save_instrument` concatenating duplicate
  rows). The cache is window-agnostic — to re-pull a *different / wider* date range pass
  `--redownload` (or delete `data/raw/`). Delete `data/normalized/` + `data/qlib_bin/` to force a
  clean re-dump, and `mlruns/` to reset experiment tracking.

## 9. TimescaleDB sink (`astock`)

Sections 1–8 produce QLib `.bin` files. This section additionally lands the **same data in
PostgreSQL/TimescaleDB**, so it can be queried with plain SQL, joined against board/industry
metadata and inspected without going through QLib's expression engine. It is optional and
independent — the `.bin` workflow above keeps working whether or not the database exists.

### 9.1 Prerequisites

- A reachable PostgreSQL with the **TimescaleDB** extension (developed against PostgreSQL 18.6 +
  TimescaleDB 2.29.2 with `timescaledb.license = timescale`).
- `psycopg[binary]` — already pinned in `requirements.txt`.
- Connection defaults are `localhost:5432`, `postgres/postgres`, database **`astock`**. Each one can
  be overridden with the standard libpq variable, no code edit needed:

```powershell
$env:PGHOST="localhost"; $env:PGPORT="5432"; $env:PGUSER="postgres"
$env:PGPASSWORD="postgres"; $env:PGDATABASE="astock"
```

### 9.2 Usage

```powershell
# from the QLib repo root
conda run -n baostock_qlib python baostock\run_db.py init         # create db + extension + apply db/schema.sql
conda run -n baostock_qlib python baostock\run_db.py load-local   # stage 1: bars from the local CSV cache
conda run -n baostock_qlib python baostock\run_db.py sync-sector  # stage 2: boards/metadata from baostock
conda run -n baostock_qlib python baostock\run_db.py verify       # 36 checks against the CSV cache
conda run -n baostock_qlib python baostock\run_db.py all          # init + stage 1 + stage 2 + verify
```

- **Stage 1 (`load-local`) makes zero baostock calls.** It re-reads `data/raw/*.csv` — the cache the
  section-3 collector already downloaded — so re-running it is free. Useful flags:
  `--full-refresh` (ignore watermarks, reload everything), `--limit N` (first N symbols, debug),
  `--batch-symbols 50`, `--with-calendar=False`, `--with-index=False`.
- **Stage 2 (`sync-sector`) caches every baostock answer under `data/sector/`.** With a complete
  cache it logs in **zero** times and finishes in ~2 s. Useful flags: `--only industry`,
  `--skip-basic`, `--skip-index-history` (newest membership snapshot instead of all quarter-ends),
  `--skip-probe`, `--limit N`, `--delay 0.15`.
- Both stages are **idempotent**: `UNLOGGED` staging table → `COPY` → `DISTINCT ON` (last row wins
  when one file repeats a key) → `INSERT ... ON CONFLICT DO UPDATE`. A re-run never duplicates a bar.
- Compression is handled transparently — the loaders drop the compression policy and decompress the
  chunks they are about to touch, then restore both afterwards.
- `verify` exits **1** when any error-level check fails, so it is usable in CI; warnings do not.

### 9.3 Schema

| Object | Kind | Contents |
|---|---|---|
| `instrument` | table | one row per symbol: `code`, Chinese `code_name`, exchange, board, `ipo_date`/`out_date`, `is_index`, `hs300_first`/`hs300_last`, **`is_csi300_now`** |
| `trade_calendar` | table | trading days |
| `daily_bar` | **hypertable** | OHLCV plus `vwap`, `factor`, `pct_chg`, `turn`, `trade_status`, `is_st`; PK `(symbol, trade_date)`, 1-year chunks |
| `index_daily_bar` | **hypertable** | the CSI300 index itself (`SH000300`), stored **unadjusted** |
| `stock_board` | table | board membership; PK `(board_type, board_code, symbol, snapshot_date)` |
| `sync_log`, `sync_watermark` | tables | per-run bookkeeping, per-symbol high-water marks |
| `*_stg` | UNLOGGED tables | the `COPY` landing zone, truncated on each load |
| `daily_bar_weekly`, `daily_bar_monthly` | **continuous aggregates** | `time_bucket` OHLCV, kept current by policy |
| `v_instrument_csi300_now`, `v_daily_bar_csi300`, `v_daily_bar_adj_csi300` | views | **default** surface — current members only |
| `v_instrument_all`, `v_daily_bar_all`, `v_daily_bar_adj_all`, `v_daily_bar_trading` | views | **full** surface — all 700 union symbols, opt-in |
| `v_industry_latest`, `v_board_latest`, `v_index_membership_latest` | views | newest snapshot per symbol / board |

Both hypertables use `compress_segmentby = 'symbol'` and `compress_orderby = 'trade_date DESC'` with
a 90-day `add_compression_policy`. The continuous aggregates are created `WITH NO DATA`, back-filled
via `refresh_continuous_aggregate(...)` and then maintained by `add_continuous_aggregate_policy(...)`.

### 9.4 The 300-vs-700 scope rule

`daily_bar` deliberately holds **all 700 symbols that were ever in CSI300** during the window, not
just today's 300 — restricting it to current members would bake survivorship bias into every
historical backtest. What narrows to 300 is the **query surface**, via `instrument.is_csi300_now`:

- The default `v_*_csi300*` views expose only the 300 current members. `verify` fails if any of the
  400 non-current symbols leaks into them.
- The `v_*_all` views expose all 700 and must be asked for by name.

Measured split: `instrument` holds 701 rows (700 stocks + the `SH000300` index), of which
`is_csi300_now` = **300**, formally delisted (`out_date IS NOT NULL`) = **26**, and no longer in the
index for any reason = **400**.

### 9.5 Measured state

After `init` + `load-local` + `sync-sector` + `verify` on this machine:

| | |
|---|---|
| `daily_bar` | **1,921,545** rows, 700 symbols, 2014-01-02 … 2026-09-03 |
| `index_daily_bar` | 3,082 rows |
| `instrument` / `trade_calendar` | 701 / 4,629 rows |
| `stock_board` | **58,444** rows across 8 `board_type`s |
| `daily_bar_weekly` / `daily_bar_monthly` | 405,243 / 95,564 rows |
| on disk | `daily_bar` 129 MB in 14 chunks (**14 compressed**); `index_daily_bar` 1632 kB |
| `verify` | **36 checks, 0 failed, 1 warning** |

`stock_board` by type — 51 quarter-end snapshots spanning 2014-03-31 … 2026-09-03 unless noted:

| `board_type` | rows | symbols | snapshots |
|---|---|---|---|
| `index_zz500` | 25,499 | 1,413 | 51 |
| `index_hs300` | 15,300 | 700 | 51 |
| `terminated` | 8,028 | 334 | 51 |
| `starst` | 3,695 | 667 | 51 |
| `index_sz50` | 2,550 | 136 | 51 |
| `st` | 2,504 | 414 | 51 |
| `industry` | 674 | 674 | 1 (2026-08-31) |
| `suspended` | 194 | 45 | 36 (last 2022-12-30) |

Compression was validated with a decompress → re-compress round trip on the oldest chunk: 45,839 rows
with `count(*)`, `sum(volume)` and `min/max(close)` identical before and after, **8472 kB → 3688 kB
(43.5 % of the original)**, and the weekly continuous aggregate still matching a manual
`time_bucket` over the same rows (0 mismatched weeks). Measure chunk sizes with
`chunk_compression_stats('daily_bar')` — `pg_total_relation_size()` on a compressed chunk reports
only the evacuated original relation, which reads as a bogus ~99 % saving.

The one-query smoke test — current members with Chinese name, industry, latest close and
back-adjusted close — returns exactly 300 rows:

```sql
SELECT symbol, code_name, board, industry, last_trade_date, last_close, last_close_adj
FROM v_instrument_csi300_now
ORDER BY symbol;
```

```
SH600000  浦发银行  主板  J66货币金融服务  2026-09-03  close=9.27   adj=123.91
SH600009  上海机场  主板  G56航空运输业    2026-09-03  close=23.65  adj=63.42
SH600010  包钢股份  主板  C31黑色金属冶炼和压延加工业  2026-09-03  close=2.22   adj=16.08
```

41 distinct industries are present, across the 主板 / 创业板 / 科创板 boards, and 295 of the 300
symbols have `last_close_adj != last_close` — i.e. the back-adjust factor really is applied.

### 9.6 Known limitations

- **26 delisted symbols have no industry.** baostock answers with an *empty* `industry` for
  long-withdrawn members (measured: `sh.600005` 武钢股份, delisted 2017), so `stock_board` covers
  674 of the 700 union symbols. That is a data-source limit rather than a pipeline defect:
  `verify` **warns** on union-wide coverage but **fails** only if a *current* member lacks an
  industry — all 300 are covered.
- **`suspended` history stops at 2022-12-30**; the server returns nothing newer.
- **`ame` and `szhk` boards are unavailable** — the server rejects both with `10004020`. The
  collector probes each capability once, caches the verdict under `data/sector/` and never retries a
  rejected endpoint.
- **Industry classification is CSRC (证监会行业)**, e.g. `J66货币金融服务`, not GICS or 申万. It is a
  single current snapshot with no history, so it cannot be used point-in-time.
- **Financial statements are out of scope** at this stage — only bars, calendar, instruments and
  boards are loaded.
- **Membership is quarter-end sampled**, the same approximation as `instruments/csi300.txt` noted in
  section 7, so `stock_board` cannot resolve intra-quarter index changes.
- **Do not add an `__init__.py` to this folder** — see section 8.
